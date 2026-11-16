from __future__ import annotations

import csv
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from daily_breadth_report import fetch_names  # noqa: E402
from render_strategy_summary import render_document  # noqa: E402
from stock_selection.capital import run_capital_backtest  # noqa: E402
from stock_selection.engine import SelectionConfig, StockSelector, load_bars  # noqa: E402

STORE = ROOT / "data" / "a_share_public_latest.csv"
CONFIG = ROOT / "configs" / "stock_selection_optimized.json"
REPORT_CONFIG = ROOT / "configs" / "stock_selection.json"
MD_PATH = ROOT / "docs" / "md" / "实时策略年报_2026_50门槛.md"
DAILY_RECORDS_PATH = ROOT / "docs" / "md" / "实时策略年报_2026_50门槛_日线记录.md"
TRADES_40_PATH = ROOT / "docs" / "md" / "实时策略年报_2026_40门槛_交易记录.md"
TRADES_40_CSV = ROOT / "output" / "yearly_trades_2026_40.csv"
REALTIME_15M_CSV = ROOT / "output" / "realtime_15m_2026-08-12_2026-11-13_50.csv"
START = date(2026, 1, 1)
END = date(2026, 11, 13)


def worker(payload: Tuple[str, str, Sequence[date]]) -> List[Dict[str, object]]:
    store, config_path, days = payload
    bars = load_bars(Path(store))
    config = SelectionConfig.from_json(Path(config_path))
    selector = StockSelector(bars, config)
    calendar_index = {day: index for index, day in enumerate(selector.calendar)}
    rows: List[Dict[str, object]] = []
    for day in days:
        breadth = selector.market_breadth(day)
        picks: List[str] = []
        forward = None
        pct_changes: Dict[str, float] = {}
        index = calendar_index[day]
        if breadth > 0.4:
            candidates = selector.select(day)
            picks = [item.symbol for item in candidates]
            if picks and index > 0:
                previous_day = selector.calendar[index - 1]
                for symbol in picks:
                    current = selector.by_date.get(day, {}).get(symbol)
                    previous = selector.by_date.get(previous_day, {}).get(symbol)
                    if current and previous and previous.close:
                        pct_changes[symbol] = current.close / previous.close - 1.0
            if picks and index + 2 < len(selector.calendar):
                # The signal is generated after day's close, so it is executed at the
                # next session's close and held to the following session's close.
                execution_day = selector.calendar[index + 1]
                exit_day = selector.calendar[index + 2]
                returns = []
                for symbol in picks:
                    entry = selector.by_date.get(execution_day, {}).get(symbol)
                    following = selector.by_date.get(exit_day, {}).get(symbol)
                    if entry and following:
                        returns.append(following.close / entry.close - 1.0)
                if returns:
                    forward = sum(returns) / len(returns)
        rows.append({"date": day.isoformat(), "breadth": breadth, "picks": picks,
                     "pct_changes": pct_changes, "forward": forward})
    return rows


def evaluate(rows: Sequence[Dict[str, object]], threshold: float) -> Dict[str, object]:
    equity = 1.0
    curve = []
    previous: List[str] = []
    changes = 0
    adds = 0
    removes = 0
    unique = set()
    risk_days = 0
    returns = []
    for row in rows:
        picks = row["picks"] if float(row["breadth"]) > threshold else []
        if picks:
            risk_days += 1
        unique.update(picks)
        if previous and picks != previous:
            changes += 1
        adds += len(set(picks) - set(previous))
        removes += len(set(previous) - set(picks))
        forward = row["forward"] if picks else None
        if forward is not None:
            returns.append(float(forward))
            equity *= 1.0 + float(forward)
        curve.append(equity)
        previous = picks
    peak = 1.0
    max_drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, 1.0 - value / peak)
    cost_pct = (adds + removes) * (0.40 / 5.0) * 0.0015 * 100.0
    return {
        "threshold": threshold,
        "risk_days": risk_days,
        "changes": changes,
        "adds": adds,
        "removes": removes,
        "unique": len(unique),
        "total_return": (equity - 1.0) * 100.0,
        "max_drawdown": max_drawdown * 100.0,
        "average": (sum(returns) / len(returns) * 100.0) if returns else 0.0,
        "cost": cost_pct,
        "net": (equity - 1.0) * 100.0 - cost_pct,
    }


def main() -> int:
    all_bars = load_bars(STORE)
    calendar = [day for day in sorted({bar.trade_date for bar in all_bars}) if START <= day <= END]
    print("days", len(calendar), "first", calendar[0], "last", calendar[-1], flush=True)
    workers = min(6, os.cpu_count() or 4)
    chunks = [calendar[index::workers] for index in range(workers)]
    payloads = [(str(STORE), str(CONFIG), chunk) for chunk in chunks if chunk]
    rows: List[Dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=len(payloads)) as executor:
        for result in executor.map(worker, payloads):
            rows.extend(result)
            print("chunk done", len(result), flush=True)
    rows.sort(key=lambda row: row["date"])
    if len(rows) != len(calendar):
        raise RuntimeError("并发结果不完整：{} / {}".format(len(rows), len(calendar)))

    result50 = evaluate(rows, 0.50)
    result40 = evaluate(rows, 0.40)
    over50 = [row for row in rows if float(row["breadth"]) > 0.50]
    over40 = [row for row in rows if float(row["breadth"]) > 0.40]
    max_row = max(rows, key=lambda row: float(row["breadth"]))

    optimized = SelectionConfig.from_json(CONFIG)
    report_config = SelectionConfig.from_json(REPORT_CONFIG)
    optimized_backtest = run_capital_backtest(StockSelector(all_bars, optimized), date(2026, 1, 1), END)
    report_backtest = run_capital_backtest(StockSelector(all_bars, report_config), date(2026, 1, 1), END)

    by_month: Dict[str, List[Dict[str, object]]] = {}
    for row in rows:
        by_month.setdefault(str(row["date"])[:7], []).append(row)
    month_rows = []
    for month in sorted(by_month):
        items = by_month[month]
        month_rows.append({
            "month": month,
            "days": len(items),
            "over50": sum(1 for item in items if float(item["breadth"]) > 0.50),
            "max": max(float(item["breadth"]) for item in items) * 100.0,
        })

    unique50 = sorted({symbol for row in over50 for symbol in row["picks"]})
    names = fetch_names(unique50) if unique50 else {}
    recommendation_rows = []
    previous_picks: List[str] = []
    for row in over50:
        picks = row["picks"]
        recommendation_rows.append({
            "date": row["date"],
            "breadth": float(row["breadth"]) * 100.0,
            "picks": [(symbol, names.get(symbol, "")) for symbol in picks],
            "pct_changes": row.get("pct_changes", {}),
            "added": sorted(set(picks) - set(previous_picks)),
            "removed": sorted(set(previous_picks) - set(picks)),
            "forward": row["forward"],
        })
        previous_picks = picks

    daily_lines = [
        "# 实时策略年报（>50% 门槛）日线级建议记录",
        "",
        "> 1–7 月没有可获取的 15 分钟历史数据，以下为日线级记录，仅用于覆盖无分钟数据的期间。",
        "> 8/12–11/13 的 15 分钟记录见 [实时策略年报 15 分钟记录](实时策略年报_2026_50门槛_15分钟记录.md)。",
        "",
        "| 日期 | 广度 | 目标（5 只，含当日涨幅） | 新增 | 剔除 | 执行后 1 日收益 |",
        "|---|---:|---|---|---|---:|",
    ]
    for rec in recommendation_rows:
        target_parts = []
        for symbol, name in rec["picks"]:
            text = "{} {}".format(symbol, name) if name else symbol
            pct = rec.get("pct_changes", {}).get(symbol)
            if pct is not None:
                text += " (当日 {:+.2f}%)".format(float(pct) * 100.0)
            target_parts.append(text)
        daily_lines.append("| {} | {:.2f}% | {} | {} | {} | {} |".format(
            rec["date"], rec["breadth"], "、".join(target_parts),
            "、".join(rec["added"]) or "—", "、".join(rec["removed"]) or "—",
            "—" if rec["forward"] is None else "{:+.2f}%".format(float(rec["forward"]) * 100.0)))
    DAILY_RECORDS_PATH.write_text("\n".join(daily_lines) + "\n", encoding="utf-8")
    render_document(DAILY_RECORDS_PATH)

    unique40 = sorted({symbol for row in over40 for symbol in row["picks"]})
    names40 = fetch_names(unique40) if unique40 else {}
    rows40 = []
    previous40: List[str] = []
    for row in over40:
        picks = row["picks"]
        rows40.append({
            "date": row["date"], "breadth": float(row["breadth"]) * 100.0,
            "picks": [(symbol, names40.get(symbol, "")) for symbol in picks],
            "pct_changes": row.get("pct_changes", {}),
            "added": sorted(set(picks) - set(previous40)),
            "removed": sorted(set(previous40) - set(picks)),
            "forward": row["forward"],
        })
        previous40 = picks

    trades_lines = [
        "# 实时策略年报（>40% 门槛）交易记录",
        "",
        "> 规则：广度严格 >40% 时持有 5 只目标；否则空仓。信号日收盘生成，次日收盘执行。",
        "> 数据：2026-01-05 至 2026-11-13 日线；共 136 个风险开交易日。",
        "",
        "## 汇总",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        "| 风险开天数 | {} |".format(result40["risk_days"]),
        "| 目标变化 | {} |".format(result40["changes"]),
        "| 逐笔买入 / 卖出 | {} / {} |".format(result40["adds"], result40["removes"]),
        "| 涉及股票 | {} |".format(result40["unique"]),
        "| 等权累计 | {:+.2f}% |".format(result40["total_return"]),
        "| 最大回撤 | {:.2f}% |".format(result40["max_drawdown"]),
        "| 平均单期 | {:+.2f}% |".format(result40["average"]),
        "| 估算成本 | -{:.2f}% |".format(result40["cost"]),
        "| 估算净收益 | **{:+.2f}%** |".format(result40["net"]),
        "",
        "## 每日交易记录（136 条）",
        "",
        "| 日期 | 广度 | 目标（5 只，含当日涨幅） | 新增 | 剔除 | 执行后 1 日收益 |",
        "|---|---:|---|---|---|---:|",
    ]
    for rec in rows40:
        target_parts = []
        for symbol, name in rec["picks"]:
            text = "{} {}".format(symbol, name) if name else symbol
            pct = rec.get("pct_changes", {}).get(symbol)
            if pct is not None:
                text += " (当日 {:+.2f}%)".format(float(pct) * 100.0)
            target_parts.append(text)
        trades_lines.append("| {} | {:.2f}% | {} | {} | {} | {} |".format(
            rec["date"], rec["breadth"], "、".join(target_parts),
            "、".join(rec["added"]) or "—", "、".join(rec["removed"]) or "—",
            "—" if rec["forward"] is None else "{:+.2f}%".format(float(rec["forward"]) * 100.0)))
    trades_lines.extend([
        "",
        "## 说明",
        "",
        "- 等权收益指 5 只目标各 20%，不是 40% 总仓位后的账户收益。",
        "- 信号日收盘后生成，次日收盘买入，持有到再下一交易日收盘；收益未计成本。",
        "- 逐笔买入/卖出 CSV：`output/yearly_trades_2026_40.csv`。",
        "- 这是历史回放，不是实际推送记录；实际 15 分钟任务从 2026-11-16 09:45 开始。",
        "- 以上为量化规则回放，不构成投资建议。",
        "",
    ])
    TRADES_40_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRADES_40_PATH.write_text("\n".join(trades_lines), encoding="utf-8")
    render_document(TRADES_40_PATH)

    close_by = {(bar.symbol, bar.trade_date): bar.close for bar in all_bars}
    calendar_index = {day: index for index, day in enumerate(calendar)}
    previous_target: List[str] = []
    with TRADES_40_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "side", "symbol", "name", "pct_change", "breadth_pct", "reason"])
        for row in rows:
            day = date.fromisoformat(row["date"])
            breadth = float(row["breadth"])
            target = row["picks"] if breadth > 0.40 else []
            for symbol in sorted(set(target) - set(previous_target)):
                index = calendar_index.get(day, 0)
                previous_day = calendar[index - 1] if index > 0 else None
                pct = None
                if previous_day is not None:
                    a = close_by.get((symbol, previous_day))
                    b = close_by.get((symbol, day))
                    if a and b:
                        pct = b / a - 1.0
                writer.writerow([row["date"], "BUY", symbol, names40.get(symbol, ""),
                                 "" if pct is None else "{:+.2f}%".format(pct * 100.0),
                                 "{:.2f}".format(breadth * 100.0), "risk_on"])
            for symbol in sorted(set(previous_target) - set(target)):
                index = calendar_index.get(day, 0)
                previous_day = calendar[index - 1] if index > 0 else None
                pct = None
                if previous_day is not None:
                    a = close_by.get((symbol, previous_day))
                    b = close_by.get((symbol, day))
                    if a and b:
                        pct = b / a - 1.0
                writer.writerow([row["date"], "SELL", symbol, names40.get(symbol, ""),
                                 "" if pct is None else "{:+.2f}%".format(pct * 100.0),
                                 "{:.2f}".format(breadth * 100.0), "risk_off" if not target else "rebalance"])
            previous_target = target

    realtime_rows = []
    if REALTIME_15M_CSV.exists():
        with REALTIME_15M_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
            realtime_rows = list(csv.DictReader(handle))
        realtime_symbols = sorted({symbol for row in realtime_rows for symbol in row["picks"].split(",") if symbol})
        realtime_names = fetch_names(realtime_symbols) if realtime_symbols else {}
        for row in realtime_rows:
            pct_map = {}
            for part in (row.get("pct_changes") or "").split(","):
                part = part.strip()
                if " " in part:
                    symbol, pct = part.rsplit(" ", 1)
                    pct_map[symbol] = pct
            row["_pct_map"] = pct_map
            row["_names"] = realtime_names

    lines = [
        "# 实时策略年报（>50% 门槛）2026-01-05 至 2026-11-13",
        "",
        "> 规则：只有市场广度**严格大于 50%** 才建议买入；否则空仓。",
        "> 数据：日线收盘，2026 年共有 208 个交易日；收益为 5 只目标等权、信号次日收盘买入、再下一交易日收盘卖出（与资本回测口径一致），未计 40% 仓位缩放。",
        "> 用途：年度阈值对比与规则复盘，不构成投资建议。",
        "",
        "## 一、广度概览",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        "| 交易日 | {} |".format(len(rows)),
        "| 广度 >50% 天数 | {} |".format(len(over50)),
        "| 广度 >40% 天数 | {} |".format(len(over40)),
        "| 年内最高广度 | {:.2f}%（{}） |".format(float(max_row["breadth"]) * 100.0, max_row["date"]),
        "",
        "### 每月 >50% 天数",
        "",
        "| 月份 | 交易日 | >50% 天数 | 当月最高广度 |",
        "|---|---:|---:|---:|",
    ]
    for item in month_rows:
        lines.append("| {} | {} | {} | {:.2f}% |".format(item["month"], item["days"], item["over50"], item["max"]))
    lines.extend([
        "",
        "## 二、日频重算（等权目标，未计成本）",
        "",
        "| 门槛 | 风险开天数 | 目标变化 | 涉及股票 | 等权累计 | 最大回撤 | 平均单期 | 估算成本 | 估算净收益 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for label, result in ((">50%", result50), (">40%", result40)):
        lines.append("| {} | {} | {} | {} | {:+.2f}% | {:.2f}% | {:+.2f}% | -{:.2f}% | **{:+.2f}%** |".format(
            label, result["risk_days"], result["changes"], result["unique"],
            result["total_return"], result["max_drawdown"], result["average"],
            result["cost"], result["net"]))
    lines.extend([
        "",
        "> 等权收益指 5 只目标各 20% 的组合，不是 40% 总仓位后的账户收益；估算成本按每笔换仓 8% 仓位、单边 0.15% 计算。",
        "> >40% 门槛的 136 条逐日交易记录见 [交易记录](实时策略年报_2026_40门槛_交易记录.md)。",
        "",
        "## 三、20 日调仓资本回测（含 -8% 止损，40% 总仓位）",
        "",
        "| 配置 | 累计收益 | 最大回撤 | 交易数 |",
        "|---|---:|---:|---:|",
        "| 报告版，50% 门槛 | {:+.2f}% | {:.2f}% | {} |".format(
            report_backtest.total_return * 100, report_backtest.maximum_drawdown * 100, report_backtest.trade_count),
        "| 优化版，40% 门槛 | {:+.2f}% | {:.2f}% | {} |".format(
            optimized_backtest.total_return * 100, optimized_backtest.maximum_drawdown * 100, optimized_backtest.trade_count),
        "",
        "## 四、600869 远东股份（单独跟踪）",
        "",
    ])
    first_869 = next((bar for bar in all_bars if bar.symbol == "600869.SH" and bar.trade_date >= date(2026, 1, 1)), None)
    last_869 = next((bar for bar in reversed(all_bars) if bar.symbol == "600869.SH" and bar.trade_date <= END), None)
    if first_869 and last_869:
        lines.append("- 2026 首个交易日 {}：{:.2f}".format(first_869.trade_date, first_869.close))
        lines.append("- 11-13：{:.2f}".format(last_869.close))
        lines.append("- 年内涨跌：**{:+.2f}%**".format((last_869.close / first_869.close - 1.0) * 100.0))
    lines.extend([
        "- 成本 13.48，-8% 止损 **12.40**；不进入策略目标，不补仓、不摊平。",
        "",
        "## 五、结论",
        "",
        "1. 今年广度并不低：{} 个交易日中有 {} 天 >50%，最高 {:.2f}%（{}），所以 >50% 规则并非全年空仓。".format(
            len(rows), len(over50), float(max_row["breadth"]) * 100.0, max_row["date"]),
        "2. >50% 的日频等权毛收益 **{:+.2f}%**，估算净收益 **{:+.2f}%**；>40% 毛收益 **{:+.2f}%**，净 **{:+.2f}%**。".format(
            result50["total_return"], result50["net"], result40["total_return"], result40["net"]),
        "3. 20 日调仓 50% 门槛资本收益 **{:+.2f}%**；40% 门槛 **{:+.2f}%**。".format(
            report_backtest.total_return * 100, optimized_backtest.total_return * 100),
        "4. 50% 门槛更保守，是否更好要看你的目标是降低回撤还是追求收益；日频等权收益未乘 40% 仓位，不能直接当账户收益。",
        "",
        "## 附：15 分钟建议记录（8/12–11/13，{} 条）".format(len(realtime_rows)),
        "",
        "> **1–7 月没有可获取的 15 分钟历史数据，无法生成 15 分钟记录。** 这些月份的日线级记录见 [日线记录](实时策略年报_2026_50门槛_日线记录.md)。",
        "> 下表覆盖 2026-08-12 至 2026-11-13；每 15 分钟一条，广度严格 >50% 才出现。",
        "> 详细版（含名称和完整汇总）：[实时策略年报 15 分钟记录](实时策略年报_2026_50门槛_15分钟记录.md)。",
        "",
        "| 日期 | 时间 | 广度 | 目标（5 只，含当日涨幅） | 新增 | 剔除 | 下一时点收益 |",
        "|---|---|---:|---|---|---|---:|",
    ])
    for row in realtime_rows:
        target_parts = []
        for symbol in (row.get("picks") or "").split(","):
            symbol = symbol.strip()
            if not symbol:
                continue
            text = "{} {}".format(symbol, row["_names"].get(symbol, "")).strip()
            pct = row["_pct_map"].get(symbol)
            if pct:
                text += " ({})".format(pct)
            target_parts.append(text)
        lines.append("| {} | {} | {}% | {} | {} | {} | {} |".format(
            row.get("date", ""), row.get("time", ""), row.get("breadth_pct", ""),
            "、".join(target_parts), row.get("added") or "—", row.get("removed") or "—",
            "—" if not row.get("next_slot_return_pct") else "{:+.2f}%".format(float(row["next_slot_return_pct"]))))
    lines.extend([
        "",
        "## 六、说明",
        "",
        "- 年度汇总使用日线收盘；附录的 15 分钟记录只覆盖数据可用的 8/12–11/13（免费接口最多约 1000 根），1–7 月无法补出 15 分钟记录。",
        "- 15 分钟数据已缓存在 `data/intraday/15m/`，生成报告不再重复请求；日频等权收益未计个股止损、涨跌停无法成交、滑点和 40% 仓位缩放。",
        "- 以上为历史数据规则复盘，不构成投资建议。",
        "",
    ])
    MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    MD_PATH.write_text("\n".join(lines), encoding="utf-8")
    html_path = render_document(MD_PATH)
    print("wrote", MD_PATH, html_path, flush=True)
    print("50", result50, flush=True)
    print("40", result40, flush=True)
    print("20d 50", round(report_backtest.total_return * 100, 2), "40", round(optimized_backtest.total_return * 100, 2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
