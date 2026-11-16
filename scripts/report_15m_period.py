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
import intraday_store  # noqa: E402
import replay_realtime_week as replay  # noqa: E402
from render_strategy_summary import render_document  # noqa: E402
from stock_selection.engine import SelectionConfig, StockSelector, load_bars  # noqa: E402

STORE = ROOT / "data" / "a_share_public_latest.csv"
CONFIG = ROOT / "configs" / "stock_selection_optimized.json"
START = date(2026, 8, 12)
END = date(2026, 11, 13)
THRESHOLD = 0.50
MD_PATH = ROOT / "docs" / "md" / "实时策略年报_2026_50门槛_15分钟记录.md"
CSV_PATH = ROOT / "output" / "realtime_15m_2026-08-12_2026-11-13_50.csv"


def worker(payload: Tuple[str, str, Sequence[date], date, date]):
    store, config_path, days, start, end = payload
    all_bars = load_bars(Path(store))
    config = SelectionConfig.from_json(Path(config_path))
    base_day = max(day for day in {bar.trade_date for bar in all_bars} if day < start)
    universe = sorted({bar.symbol for bar in all_bars if bar.trade_date == base_day})
    symbol_bars = {symbol: intraday_store.load_15m(symbol) for symbol in universe}
    trading_days = [day for day in sorted({bar.trade_date for bar in all_bars}) if start <= day <= end]
    rows: List[Dict[str, object]] = []
    for day in days:
        history = [bar for bar in all_bars if bar.trade_date < day]
        last_by_symbol: Dict[str, object] = {}
        for bar in history:
            current = last_by_symbol.get(bar.symbol)
            if current is None or bar.trade_date > current.trade_date:
                last_by_symbol[bar.symbol] = bar
        for slot in replay.SLOT_TIMES:
            bars = replay.build_provisional(history, last_by_symbol, symbol_bars, day, slot, universe)
            selector = StockSelector(bars, config)
            breadth = selector.market_breadth(day)
            if breadth <= THRESHOLD:
                continue
            picks = selector.select(day)
            pick_symbols = [item.symbol for item in picks]
            pct_changes = {}
            for symbol in pick_symbols:
                current = symbol_bars.get(symbol, {}).get((day.isoformat(), slot))
                previous = last_by_symbol.get(symbol)
                if current and previous and previous.close:
                    pct_changes[symbol] = current["close"] / previous.close - 1.0
            forward = replay.slot_return(pick_symbols, day, slot, trading_days, symbol_bars)
            rows.append({
                "date": day.isoformat(), "time": slot, "breadth": breadth,
                "picks": pick_symbols, "pct_changes": pct_changes,
                "forward": forward,
            })
    return rows


def main() -> int:
    all_bars = load_bars(STORE)
    days = [day for day in sorted({bar.trade_date for bar in all_bars}) if START <= day <= END]
    workers = min(6, os.cpu_count() or 4)
    chunks = [days[index::workers] for index in range(workers)]
    payloads = [(str(STORE), str(CONFIG), chunk, START, END) for chunk in chunks if chunk]
    rows: List[Dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=len(payloads)) as executor:
        for result in executor.map(worker, payloads):
            rows.extend(result)
            print("chunk", len(result), flush=True)
    rows.sort(key=lambda row: (row["date"], row["time"]))
    print("records", len(rows), flush=True)

    unique = sorted({symbol for row in rows for symbol in row["picks"]})
    names = fetch_names(unique) if unique else {}
    previous: List[str] = []
    previous_label = ""
    for row in rows:
        picks = row["picks"]
        row["added"] = sorted(set(picks) - set(previous))
        row["removed"] = sorted(set(previous) - set(picks))
        row["previous_time"] = previous_label
        previous = picks
        previous_label = "{} {}".format(row["date"], row["time"])

    cumulative = 1.0
    for row in rows:
        if row["forward"] is not None:
            cumulative *= 1.0 + float(row["forward"])
    adds = sum(len(row["added"]) for row in rows)
    removes = sum(len(row["removed"]) for row in rows)
    cost = (adds + removes) * (0.40 / 5.0) * 0.0015 * 100.0
    net = (cumulative - 1.0) * 100.0 - cost

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "time", "breadth_pct", "picks", "pct_changes", "added", "removed", "next_slot_return_pct"])
        for row in rows:
            writer.writerow([
                row["date"], row["time"], "{:.2f}".format(float(row["breadth"]) * 100),
                ",".join(row["picks"]),
                ",".join("{} {:+.2f}%".format(s, v * 100.0) for s, v in sorted(row["pct_changes"].items())),
                ",".join(row["added"]), ",".join(row["removed"]),
                "" if row["forward"] is None else "{:.4f}".format(float(row["forward"]) * 100.0),
            ])

    by_day: Dict[str, List[Dict[str, object]]] = {}
    for row in rows:
        by_day.setdefault(row["date"], []).append(row)
    lines = [
        "# 实时策略年报（>50% 门槛）15 分钟建议记录",
        "",
        "> 区间：2026-08-12 至 2026-11-13（本地 15 分钟数据可用区间）。",
        "> **1 月至 7 月没有可获取的 15 分钟历史数据，无法补出 15 分钟记录；本表只覆盖有数据的 8/12–11/13。**",
        "> 规则：广度严格 >50% 才建议；每 15 分钟一条；信号与下一时点收益口径见说明。",
        "",
        "## 汇总",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        "| 15 分钟时点 | 930 |",
        "| 广度 >50% 的记录 | {} |".format(len(rows)),
        "| 涉及股票 | {} |".format(len(unique)),
        "| 新增 / 剔除笔数 | {} / {} |".format(adds, removes),
        "| 下一时点等权累计 | {:+.2f}% |".format((cumulative - 1.0) * 100.0),
        "| 估算换手成本 | -{:.2f}% |".format(cost),
        "| 估算净收益 | **{:+.2f}%** |".format(net),
        "",
        "## 建议记录",
        "",
        "| 日期 | 时间 | 广度 | 目标（5 只，含当日涨幅） | 新增 | 剔除 | 下一时点收益 |",
        "|---|---|---:|---|---|---|---:|",
    ]
    for row in rows:
        target_parts = []
        for symbol in row["picks"]:
            text = "{} {}".format(symbol, names.get(symbol, "")).strip()
            pct = row["pct_changes"].get(symbol)
            if pct is not None:
                text += " ({:+.2f}%)".format(float(pct) * 100.0)
            target_parts.append(text)
        forward = "—" if row["forward"] is None else "{:+.2f}%".format(float(row["forward"]) * 100.0)
        lines.append("| {} | {} | {:.2f}% | {} | {} | {} | {} |".format(
            row["date"], row["time"], float(row["breadth"]) * 100,
            "、".join(target_parts), "、".join(row["added"]) or "—",
            "、".join(row["removed"]) or "—", forward))
    lines.extend([
        "",
        "## 说明",
        "",
        "- 这是历史回放，不是实际推送记录；实际推送从 2026-11-16 09:45 开始。",
        "- 15 分钟数据来自新浪 K 线，已缓存在 `data/intraday/15m/`；生成报告不再重复请求。",
        "- 1–7 月无 15 分钟数据，年度日线汇总见 [实时策略年报（>50% 门槛）](实时策略年报_2026_50门槛.md)。",
        "- 下一时点收益为等权目标、下一 15 分钟收盘卖出，未乘 40% 仓位；成本按每笔换仓 8% 仓位、单边 0.15% 估算。",
        "- 以上为量化规则回放，不构成投资建议。",
        "",
    ])
    MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    MD_PATH.write_text("\n".join(lines), encoding="utf-8")
    html_path = render_document(MD_PATH)
    print("wrote", MD_PATH, html_path, "records", len(rows), "cum", round((cumulative - 1) * 100, 2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
