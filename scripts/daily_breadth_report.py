from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from render_strategy_summary import render_document
from real_positions import LEDGER as REAL_LEDGER, export_report as export_real_report
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from stock_selection.capital import run_capital_backtest  # noqa: E402
from stock_selection.alerts import build_trade_alert, format_trade_alert
from stock_selection.real_account import snapshot as real_snapshot, reconcile_message
from stock_selection.engine import SelectionConfig, StockSelector, infer_board, load_bars  # noqa: E402

BASE_CSV = ROOT / "data" / "a_share_public_20261113.csv"
STORE_CSV = ROOT / "data" / "a_share_public_latest.csv"
OPT_CONFIG = ROOT / "configs" / "stock_selection_optimized.json"
REPORT_CONFIG = ROOT / "configs" / "stock_selection.json"
PUSH_CONFIG = ROOT / ".secrets" / "push_config.json"
HISTORY_CSV = ROOT / "output" / "breadth_history.csv"
LATEST_JSON = ROOT / "output" / "breadth_latest.json"
LAST_PUSH = ROOT / "output" / "last_breadth_push.json"
LAST_TRADE_PUSH = ROOT / "output" / "last_trade_signal_push.json"
REPORT_DIR = ROOT / "docs" / "md" / "市场广度日报"

FIELDS = ["date", "symbol", "close", "volume", "adj_factor", "listing_days", "is_st", "suspended"]
TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,12,qfq"


def log(message: str) -> None:
    print("[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def read_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def fetch_symbol(symbol: str) -> Tuple[str, List[List[str]], Optional[str]]:
    code, exchange = symbol.split(".")
    prefix = "sh" if exchange == "SH" else "sz"
    url = TENCENT_URL.format(prefix=prefix, code=code)
    error = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 quant-research/1.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            node = payload["data"][prefix + code]
            klines = node.get("qfqday") or node.get("day") or []
            return symbol, [line for line in klines if len(line) >= 6], None
        except Exception as exc:  # network / upstream
            error = "{}: {}".format(type(exc).__name__, exc)
            time.sleep(0.5 * (attempt + 1))
    return symbol, [], error


def update_store(base: Path, store: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    if store.exists():
        rows = read_rows(store)
        source = store
    else:
        rows = read_rows(base)
        source = base
    if not rows:
        raise RuntimeError("行情文件为空: {}".format(source))

    last_date = max(row["date"] for row in rows)
    last_rows = [row for row in rows if row["date"] == last_date]
    universe = sorted({row["symbol"] for row in last_rows})
    last_close = {row["symbol"]: float(row["close"]) for row in last_rows}
    listing = {row["symbol"]: int(row["listing_days"]) for row in last_rows}
    log("基础数据 {}，最后交易日 {}，候选池 {} 只".format(source.name, last_date, len(universe)))

    results: Dict[str, List[List[str]]] = {}
    failures: List[str] = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_symbol, symbol) for symbol in universe]
        for future in as_completed(futures):
            symbol, klines, error = future.result()
            if klines:
                results[symbol] = klines
            else:
                failures.append(symbol)

    new_rows: List[Dict[str, str]] = []
    scale_values: List[float] = []
    for symbol in universe:
        klines = results.get(symbol, [])
        by_date = {line[0]: line for line in klines}
        anchor = by_date.get(last_date)
        if anchor is None or float(anchor[2]) <= 0:
            continue
        scale = last_close[symbol] / float(anchor[2])
        scale_values.append(scale)
        for line in klines:
            trade_date = line[0]
            if trade_date <= last_date:
                continue
            new_rows.append(
                {
                    "date": trade_date,
                    "symbol": symbol,
                    "close": "{:.4f}".format(float(line[2]) * scale),
                    "volume": "{:.1f}".format(float(line[5]) * 100.0),
                    "adj_factor": "1.0",
                    "listing_days": str(listing.get(symbol, 0) + (date.fromisoformat(trade_date) - date.fromisoformat(last_date)).days),
                    "is_st": "0",
                    "suspended": "0",
                }
            )

    complete_rows = []
    for day in sorted({row['date'] for row in new_rows}):
        daily_rows = [row for row in new_rows if row['date'] == day]
        if {row['symbol'] for row in daily_rows} != set(universe):
            log("{} 行情不完整，暂不写入该日及后续日期，等待重试".format(day))
            break
        complete_rows.extend(daily_rows)
    new_rows = complete_rows
    if new_rows:
        rows = rows + new_rows
        rows.sort(key=lambda row: (row["date"], row["symbol"]))
        write_rows(store, rows)
        log("已更新 {}，新增 {} 条，最新日期 {}".format(store.name, len(new_rows), max(row["date"] for row in new_rows)))
    else:
        if not store.exists():
            write_rows(store, rows)
        log("没有新交易日数据，保持 {}".format(last_date))
    if failures:
        log("抓取失败 {} 只（不影响已有数据）：{}".format(len(failures), ", ".join(failures[:10])))
    if scale_values:
        log("复权衔接比例中位数 {:.4f}，异常(>5%) {} 只".format(
            sorted(scale_values)[len(scale_values) // 2],
            sum(1 for value in scale_values if abs(value - 1.0) > 0.05),
        ))
    return rows, failures


def breadth_details(selector: StockSelector, config: SelectionConfig, day: date) -> Tuple[int, int, float]:
    above = 0
    available = 0
    lookback = config.market_breadth_lookback
    for symbol in selector.by_date.get(day, {}):
        if infer_board(symbol) not in config.allowed_boards:
            continue
        index = selector.index.get(symbol, {}).get(day)
        if index is None or index < lookback - 1:
            continue
        current = selector.by_symbol[symbol][index]
        if current.is_st or current.suspended:
            continue
        prices = [bar.close * bar.adj_factor for bar in selector.by_symbol[symbol][index - lookback + 1 : index + 1]]
        available += 1
        above += prices[-1] > (sum(prices) / len(prices))
    value = above / available if available else 0.0
    return above, available, value


def zone_text(value: float, optimized_threshold: float, report_threshold: float) -> str:
    if value >= report_threshold:
        return "两版门槛内"
    if value >= optimized_threshold:
        return "仅优化版门槛内"
    return "低于两版门槛"


def fetch_names(symbols: List[str]) -> Dict[str, str]:
    if not symbols:
        return {}
    codes = []
    for symbol in symbols:
        code, exchange = symbol.split(".")
        codes.append(("sh" if exchange == "SH" else "sz") + code)
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            text = response.read().decode("gbk", "replace")
        code_to_symbol = {symbol.split(".")[0]: symbol for symbol in symbols}
        names: Dict[str, str] = {}
        for segment in text.strip().split(";"):
            if "=" not in segment:
                continue
            payload = segment.split("=", 1)[1].strip().strip('"')
            parts = payload.split("~")
            if len(parts) >= 3 and parts[2] in code_to_symbol:
                names[code_to_symbol[parts[2]]] = parts[1]
        return names
    except Exception as exc:
        log("股票名称获取失败：{}".format(exc))
        return {}


def latest_close(selector: StockSelector, symbol: str, target: date) -> Optional[float]:
    items = selector.by_symbol.get(symbol, [])
    for bar in reversed(items):
        if bar.trade_date <= target:
            return bar.close * bar.adj_factor
    return None


def current_portfolio(selector: StockSelector, config: SelectionConfig, target_index: int) -> Dict[str, object]:
    calendar = selector.calendar
    start = selector.strategy_start_index()
    target = calendar[target_index]
    result = run_capital_backtest(selector, calendar[start], target)
    positions = {str(symbol): int(shares) for symbol, shares in result.final_positions.items() if int(shares) > 0}

    holdings: List[Dict[str, object]] = []
    for symbol, shares in positions.items():
        bar = selector.by_date.get(target, {}).get(symbol)
        current_close = bar.close if bar else None
        if current_close is None:
            continue
        cost = result.average_cost.get(symbol, current_close)
        amount = shares * cost
        market_value = shares * current_close
        holdings.append(
            {
                "symbol": symbol,
                "name": symbol,
                "shares": shares,
                "cost": cost,
                "current": current_close,
                "amount": amount,
                "market_value": market_value,
                "pnl": market_value - amount,
                "return_pct": (current_close / cost - 1.0) * 100.0,
                "weight": amount / config.initial_capital,
            }
        )
    holdings.sort(key=lambda item: str(item["symbol"]))

    rebalance_indices = list(range(start, len(calendar), config.rebalance_days))
    past = [item for item in rebalance_indices if item <= target_index]
    rebalance_date = calendar[past[-1]] if past else None
    breadth = selector.market_breadth(rebalance_date) if rebalance_date else None
    risk_on = bool(breadth is not None and breadth >= config.minimum_market_breadth)

    market_total = sum(float(item["market_value"]) for item in holdings)
    cash = float(result.final_cash)
    equity = float(result.final_equity)
    total_return = float(result.total_return) * 100.0
    return {
        "rebalance_date": rebalance_date.isoformat() if rebalance_date else None,
        "breadth_at_rebalance": breadth,
        "risk_on": risk_on,
        "holdings": holdings,
        "skipped": [],
        "invested": sum(float(item["amount"]) for item in holdings),
        "cash": cash,
        "cash_yield_annual": config.cash_yield_annual,
        "market_value": market_total,
        "equity": equity,
        "return_pct": total_return,
        "capital_impact": total_return,
        "maximum_drawdown": float(result.maximum_drawdown) * 100.0,
        "exposure": config.maximum_equity_exposure,
        "trades": len(result.trades),
        "trade_alert": build_trade_alert(selector, result, target),
    }


def holding_risk_flags(selector: StockSelector, holdings: List[Dict[str, object]], target: date) -> List[str]:
    flags: List[str] = []
    for holding in holdings:
        symbol = str(holding["symbol"])
        items = selector.by_symbol.get(symbol, [])
        index = None
        for offset in range(len(items) - 1, -1, -1):
            if items[offset].trade_date <= target:
                index = offset
                break
        if index is None or index < 120:
            continue
        closes = [bar.close * bar.adj_factor for bar in items]
        momentum_120 = closes[index] / closes[index - 120] - 1.0
        recent = closes[max(0, index - 59) : index + 1]
        drawdown_60 = 1.0 - closes[index] / max(recent)
        if momentum_120 < 0:
            flags.append("{} {} 120 日动量为负，下次调仓可能被剔除".format(symbol, holding["name"]))
        elif drawdown_60 > 0.25:
            flags.append("{} {} 60 日回撤 {:.0f}%，下次调仓可能被剔除".format(
                symbol, holding["name"], drawdown_60 * 100))
    return flags


def daily_advice(portfolio: Dict[str, object], target_index: int, rebalance_indices: List[int],
                 breadth: float, threshold: float, remaining: Optional[int]) -> str:
    if target_index in rebalance_indices:
        if breadth >= threshold:
            return "今日为调仓日，策略信号为换仓/建仓；回测按下一交易日收盘价模拟成交，详见买卖提醒。"
        return "今日为调仓日，广度低于 {:.0f}% 门槛，策略信号为转空仓。".format(threshold * 100)
    if breadth < threshold:
        return "今日不操作；广度已低于 {:.0f}% 门槛，下次调仓将转空仓（还剩 {} 个交易日）。".format(
            threshold * 100, remaining if remaining is not None else "未知")
    if portfolio["risk_on"] and portfolio["holdings"]:
        return "今日不操作，继续持有；距下次调仓 {} 个交易日。".format(remaining if remaining is not None else "未知")
    return "今日不操作，保持空仓；距下次调仓 {} 个交易日。".format(remaining if remaining is not None else "未知")


def build_message(target: date, value: float, above: int, available: int, delta: float,
                  window: List[Tuple[date, float]], optimized_threshold: float,
                  report_threshold: float, remaining_rebalances: Optional[int],
                  portfolio: Dict[str, object], advice: str, risk_notes: List[str],
                  report_in_market: bool, track_start: Optional[date] = None) -> Tuple[str, str]:
    zone = zone_text(value, optimized_threshold, report_threshold)
    if value >= report_threshold:
        zone_detail = "广度 ≥ {:.0f}%".format(report_threshold * 100)
    elif value >= optimized_threshold:
        zone_detail = "{:.0f}% ≤ 广度 < {:.0f}%".format(optimized_threshold * 100, report_threshold * 100)
    else:
        zone_detail = "广度 < {:.0f}%".format(optimized_threshold * 100)
    title = "A股广度 {}｜{:.2f}% {}".format(target.strftime("%m-%d"), value * 100, zone)
    lines = [
        "**A股市场广度 · {}**".format(target.isoformat()),
        "",
        "今日广度：**{:.2f}%**（{} / {}）".format(value * 100, above, available),
        "较前一日：**{:+.2f}pp**".format(delta * 100),
        "广度区间：**{}**（{}）".format(zone, zone_detail),
        "",
    ]

    holdings = portfolio.get("holdings") or []
    if holdings:
        lines.append("策略状态：持仓 {} 只".format(len(holdings)))
    else:
        rebalance_date = portfolio.get("rebalance_date")
        rebalance_breadth = portfolio.get("breadth_at_rebalance")
        if rebalance_date and rebalance_breadth is not None:
            lines.append("策略状态：空仓（{} 调仓时广度 {:.2f}% < {:.0f}%）".format(
                rebalance_date, float(rebalance_breadth) * 100, optimized_threshold * 100))
        else:
            lines.append("策略状态：空仓")
    if portfolio.get("risk_on") and holdings:
        equity = float(portfolio.get("equity", 1.0))
        market_value = float(portfolio.get("market_value", 0.0))
        lines.append("**当前持股**（{} 调仓，当前仓位 {:.1f}%）".format(
            portfolio.get("rebalance_date"), market_value / equity * 100 if equity else 0.0))
        for holding in holdings:
            lines.append("- {} {}｜持仓 {:,.0f} 元｜成本 {:.2f}｜现价 {:.2f}｜{:+.1f}%".format(
                holding["symbol"], holding["name"], float(holding["market_value"]),
                float(holding["cost"]), float(holding["current"]), float(holding["return_pct"])))
        lines.append("持仓市值 **{:,.0f}** 元｜现金 **{:,.0f}** 元｜总资产 **{:,.0f}** 元｜策略自 {} 累计 **{:+.2f}%**".format(
            market_value, float(portfolio.get("cash", 0.0)), equity,
            track_start.isoformat() if track_start else "", float(portfolio.get("return_pct", 0.0))))
        for item in portfolio.get("skipped", []):
            lines.append("· {} {} 未买入：100 股需 {:,.0f} 元，超过单票上限 {:,.0f} 元".format(
                item["symbol"], item["name"], float(item["lot_cost"]), float(item["single_limit"])))
    else:
        lines.append("现金 **{:,.0f}** 元｜策略自 {} 累计 **{:+.2f}%**".format(
            float(portfolio.get("cash", 100000.0)),
            track_start.isoformat() if track_start else "",
            float(portfolio.get("return_pct", 0.0))))

    lines.extend(["", "**今日建议**：{}".format(advice)])
    for note in risk_notes:
        lines.append("· {}".format(note))
    lines.extend(["", "近5个交易日："])
    for day, day_value in window:
        lines.append("- {}：{:.2f}%".format(day.strftime("%m-%d"), day_value * 100))
    lines.extend(
        [
            "",
            "门槛：报告版 {:.0f}% · 优化版 {:.0f}%".format(report_threshold * 100, optimized_threshold * 100),
            "对照：报告版 50% —— {}".format("在场" if report_in_market else "空仓"),
        ]
    )
    if remaining_rebalances is not None:
        lines.append("距下次调仓：{} 个交易日".format(remaining_rebalances))
    lines.extend(["", "以上为量化策略规则信号，不构成投资建议。"])
    return title, "\n".join(lines)


def send_pushplus(token: str, title: str, content: str, topic: Optional[str] = None) -> Tuple[bool, str]:
    payload = {"token": token, "title": title, "content": content, "template": "markdown"}
    if topic:
        payload["topic"] = topic
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "quant-research/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    ok = payload.get("code") == 200
    return ok, json.dumps(payload, ensure_ascii=False)


def send_serverchan(sendkey: str, title: str, content: str) -> Tuple[bool, str]:
    if sendkey.startswith("sctp"):
        match = re.match(r"^sctp(\d+)t", sendkey)
        if not match:
            return False, "无法从 SendKey 解析 uid"
        url = "https://{}.push.ft07.com/send/{}.send".format(match.group(1), sendkey)
    else:
        url = "https://sctapi.ftqq.com/{}.send".format(sendkey)
    body = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "quant-research/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    ok = payload.get("code") == 0
    return ok, json.dumps(payload, ensure_ascii=False)


def load_push_config(path: Path) -> Dict[str, str]:
    config: Dict[str, str] = {}
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
    for env_name, key in (("PUSHPLUS_TOKEN", "pushplus_token"), ("SERVERCHAN_SENDKEY", "serverchan_sendkey")):
        if os.environ.get(env_name):
            config[key] = os.environ[env_name]
    return config


def update_history(target: date, value: float, above: int, available: int, delta: float,
                   zone: str) -> None:
    rows: List[Dict[str, str]] = []
    if HISTORY_CSV.exists():
        rows = read_rows(HISTORY_CSV)
    record = {
        "date": target.isoformat(),
        "breadth_pct": "{:.2f}".format(value * 100),
        "above_ma60": str(above),
        "available": str(available),
        "delta_pp": "{:+.2f}".format(delta * 100),
        "zone": zone,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    rows = [row for row in rows if row.get("date") != record["date"]]
    rows.append(record)
    rows.sort(key=lambda row: row["date"])
    HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    temporary = HISTORY_CSV.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["date", "breadth_pct", "above_ma60", "available", "delta_pp", "zone", "generated_at"],
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(HISTORY_CSV)


def write_snapshot(target: date, title: str, message: str, window: List[Tuple[date, float]]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 市场广度日报 {}".format(target.isoformat()),
        "",
        "> 自动生成于 {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "",
        message,
        "",
        "| 日期 | 广度 |",
        "|---|---:|",
    ]
    for day, value in window:
        lines.append("| {} | {:.2f}% |".format(day.isoformat(), value * 100))
    lines.extend(
        [
            "",
            "口径：收盘价（前复权）高于过去 60 个交易日收盘均线的股票数 ÷ 可计算股票数；样本固定为 500 只候选池，剔除 ST、停牌及历史不足 60 个交易日的标的。",
            "",
        ]
    )
    report_path = REPORT_DIR / "{}.md".format(target.isoformat())
    report_path.write_text("\n".join(lines), encoding="utf-8")
    render_document(report_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="每个交易日生成市场广度报告并可推送到微信")
    parser.add_argument("--send", action="store_true", help="实际发送；不加则只打印")
    parser.add_argument("--signals-only", action="store_true", help="仅在有买卖或调仓复核信号时推送")
    parser.add_argument("--force", action="store_true", help="忽略当日已推送记录")
    parser.add_argument("--date", type=date.fromisoformat, help="指定日期，默认今天")
    parser.add_argument("--no-update", action="store_true", help="不抓取新行情")
    parser.add_argument("--provider", choices=["pushplus", "serverchan"], help="覆盖配置中的推送渠道")
    parser.add_argument("--token", help="覆盖配置中的 token / sendkey")
    parser.add_argument("--topic", help="PushPlus 群组编码，填写后推送到群组")
    parser.add_argument("--config", type=Path, default=PUSH_CONFIG)
    parser.add_argument("--base", type=Path, default=BASE_CSV)
    parser.add_argument("--store", type=Path, default=STORE_CSV)
    args = parser.parse_args()

    if args.send and (args.date and args.date != datetime.now().date()):
        raise ValueError("历史回放不能发送实时提醒，请移除 --send")
    if args.send and datetime.now().hour < 15:
        raise ValueError("收盘前不能发送日线交易信号")

    if not args.no_update:
        update_store(args.base, args.store)

    store = args.store if args.store.exists() else args.base
    bars = load_bars(store)
    optimized = SelectionConfig.from_json(OPT_CONFIG)
    report = SelectionConfig.from_json(REPORT_CONFIG)
    selector = StockSelector(bars, optimized)
    calendar = selector.calendar
    if not calendar:
        raise RuntimeError("没有可用行情")

    target = args.date or datetime.now().date()
    if target not in calendar:
        if args.date is None:
            log("{} 没有行情（周末/休市或数据源未更新），跳过推送".format(target))
            return 0
        raise RuntimeError("指定日期 {} 不在行情日历中".format(target))

    if args.send:
        expected = set(selector.by_symbol)
        if not expected.issubset(selector.by_date[target]):
            raise RuntimeError("当日行情不完整，停止发送买卖信号，请检查数据源后重试")

    index = calendar.index(target)
    above, available, value = breadth_details(selector, optimized, target)
    engine_value = selector.market_breadth(target)
    if abs(engine_value - value) > 1e-9:
        log("警告：手工计算广度 {:.6f} 与引擎 {:.6f} 不一致".format(value, engine_value))
    previous = calendar[index - 1] if index > 0 else None
    previous_value = breadth_details(selector, optimized, previous)[2] if previous else value
    delta = value - previous_value

    window_days = calendar[max(0, index - 4) : index + 1]
    window = [(day, breadth_details(selector, optimized, day)[2]) for day in window_days]

    start = selector.strategy_start_index()
    rebalance_indices = list(range(start, len(calendar), optimized.rebalance_days))
    past = [item for item in rebalance_indices if item <= index]
    remaining = (past[-1] + optimized.rebalance_days - index) if past else None

    optimized_threshold = optimized.minimum_market_breadth
    report_threshold = report.minimum_market_breadth
    zone = zone_text(value, optimized_threshold, report_threshold)

    portfolio = current_portfolio(selector, optimized, index)
    selector_report = StockSelector(bars, report)
    report_portfolio = current_portfolio(selector_report, report, index)
    symbols_to_name = [str(holding["symbol"]) for holding in portfolio["holdings"]]
    symbols_to_name += [str(item["symbol"]) for item in portfolio.get("skipped", [])]
    symbols_to_name += [row['symbol'] for row in portfolio['trade_alert']['orders']]
    names = fetch_names(sorted(set(symbols_to_name)))
    for holding in portfolio["holdings"]:
        holding["name"] = names.get(str(holding["symbol"]), str(holding["symbol"]))
    for item in portfolio.get("skipped", []):
        item["name"] = names.get(str(item["symbol"]), str(item["symbol"]))

    stop_watch = []
    if optimized.stop_loss_pct and index not in rebalance_indices:
        for holding in portfolio["holdings"]:
            if float(holding["return_pct"]) <= -optimized.stop_loss_pct * 100.0:
                stop_watch.append(holding)

    advice = daily_advice(portfolio, index, rebalance_indices, value, optimized_threshold, remaining)
    if stop_watch:
        advice = "触发止损：下一交易日卖出 {}；其余继续持有。".format(
            "、".join("{} {}".format(holding["symbol"], holding["name"]) for holding in stop_watch))

    risk_notes: List[str] = []
    for holding in stop_watch:
        risk_notes.append("{} {} {:.1f}%，已跌破 -{:.0f}% 止损线".format(
            holding["symbol"], holding["name"], float(holding["return_pct"]),
            optimized.stop_loss_pct * 100))
    if value < optimized_threshold:
        risk_notes.append("广度已低于 {:.0f}% 门槛，下次调仓将转空仓".format(optimized_threshold * 100))
    elif value - optimized_threshold < 0.01:
        risk_notes.append("广度距 {:.0f}% 门槛仅 {:.2f}pp，下次调仓存在转空仓风险".format(
            optimized_threshold * 100, (value - optimized_threshold) * 100))
    risk_notes.extend(holding_risk_flags(selector, portfolio["holdings"], target))
    report_in_market = bool(report_portfolio["risk_on"] and report_portfolio["holdings"])

    track_start = None
    if optimized.strategy_start:
        configured_start = date.fromisoformat(optimized.strategy_start)
        track_start = next((day for day in calendar if day >= configured_start), calendar[0])

    title, message = build_message(
        target, value, above, available, delta, window, optimized_threshold, report_threshold,
        remaining, portfolio, advice, risk_notes, report_in_market, track_start,
    )
    alert_title, alert_message = format_trade_alert(portfolio['trade_alert'], names)
    message += '\n\n' + alert_message
    real_account = real_snapshot(REAL_LEDGER, target) if REAL_LEDGER.exists() else None
    if real_account:
        reconciliation = reconcile_message(real_account, portfolio['trade_alert'])
        if reconciliation:
            alert_message += '\n\n' + reconciliation
        if target == datetime.now().date():
            export_real_report()
    if args.signals_only:
        title = alert_title

    update_history(target, value, above, available, delta, zone)
    latest = {
        "date": target.isoformat(),
        "breadth": round(value, 6),
        "above_ma60": above,
        "available": available,
        "delta_pp": round(delta * 100, 4),
        "zone": zone,
        "optimized_threshold": optimized_threshold,
        "report_threshold": report_threshold,
        "last_5_days": [{"date": day.isoformat(), "breadth": round(item, 6)} for day, item in window],
        "next_rebalance_in_trading_days": remaining,
        "portfolio_optimized": {
            "rebalance_date": portfolio.get("rebalance_date"),
            "breadth_at_rebalance": (
                round(float(portfolio["breadth_at_rebalance"]), 6)
                if portfolio.get("breadth_at_rebalance") is not None else None
            ),
            "risk_on": bool(portfolio.get("risk_on")),
            "holdings": [
                {
                    "symbol": holding["symbol"],
                    "name": holding["name"],
                    "shares": int(holding["shares"]),
                    "cost": round(float(holding["cost"]), 2),
                    "current": round(float(holding["current"]), 2),
                    "amount": round(float(holding["amount"]), 2),
                    "market_value": round(float(holding["market_value"]), 2),
                    "pnl": round(float(holding["pnl"]), 2),
                    "weight": round(float(holding["weight"]), 4),
                    "return_pct": round(float(holding["return_pct"]), 2),
                }
                for holding in portfolio.get("holdings", [])
            ],
            "skipped": [
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "price": round(float(item["price"]), 2),
                    "lot_cost": round(float(item["lot_cost"]), 2),
                    "single_limit": round(float(item["single_limit"]), 2),
                }
                for item in portfolio.get("skipped", [])
            ],
            "invested": round(float(portfolio.get("invested", 0.0)), 2),
            "cash": round(float(portfolio.get("cash", 0.0)), 2),
            "cash_yield_annual": float(portfolio.get("cash_yield_annual", 0.0)),
            "market_value": round(float(portfolio.get("market_value", 0.0)), 2),
            "equity": round(float(portfolio.get("equity", 0.0)), 2),
            "return_pct": round(float(portfolio.get("return_pct", 0.0)), 2),
            "maximum_drawdown_pct": round(float(portfolio.get("maximum_drawdown", 0.0)), 2),
            "capital_impact_pct": round(float(portfolio.get("capital_impact", 0.0)), 2),
        },
        "report_50_in_market": report_in_market,
        "advice": advice,
        "trade_alert": portfolio['trade_alert'],
        "real_account": real_account,
        "risk_notes": risk_notes,
        "strategy_start": optimized.strategy_start,
        "stop_loss_pct": optimized.stop_loss_pct,
        "stop_watch": [
            {
                "symbol": holding["symbol"],
                "name": holding["name"],
                "return_pct": round(float(holding["return_pct"]), 2),
            }
            for holding in stop_watch
        ],
        "strategy_track": {
            "start_date": track_start.isoformat() if track_start else None,
            "total_return_pct": round(float(portfolio.get("return_pct", 0.0)), 2),
            "maximum_drawdown_pct": round(float(portfolio.get("maximum_drawdown", 0.0)), 2),
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_source": str(store),
    }
    LATEST_JSON.parent.mkdir(parents=True, exist_ok=True)
    LATEST_JSON.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_snapshot(target, title, message, window)

    log("{} 广度 {:.2f}%（{} / {}），较前一日 {:+.2f}pp，状态：{}".format(
        target.isoformat(), value * 100, above, available, delta * 100, zone))
    print("\n" + message + "\n", flush=True)

    if not args.send:
        log("未加 --send，仅生成报告")
        return 0

    if args.signals_only and not portfolio['trade_alert']['actionable']:
        log("无买卖或调仓复核信号，仅保存报告，不推送")
        return 0

    last_push_path = LAST_TRADE_PUSH if args.signals_only else LAST_PUSH
    if args.signals_only:
        message = alert_message
    elif real_account:
        reconciliation = reconcile_message(real_account, portfolio['trade_alert'])
        if reconciliation:
            message += '\n\n' + reconciliation

    config = load_push_config(args.config)
    provider = args.provider or config.get("provider")
    topic = None
    if provider == "pushplus":
        token = args.token or config.get("pushplus_token")
        topic = args.topic or config.get("pushplus_topic") or None
    elif provider == "serverchan":
        token = args.token or config.get("serverchan_sendkey")
    else:
        token = ""

    if topic and real_account and real_account['trade_count']:
        raise ValueError("真实持仓核对仅发送个人微信，不发送群组")

    if last_push_path.exists() and not args.force:
        try:
            last = json.loads(last_push_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            last = {}
        if last.get("date") == target.isoformat():
            log("{} 已推送过（{}），跳过；需要重发请加 --force".format(target, last.get("provider")))
            return 0

    if not provider or not token:
        log("未配置推送渠道或 token：请在 {} 中填写 provider 与对应 token".format(args.config))
        return 1

    if provider == "pushplus":
        ok, detail = send_pushplus(token, title, message, topic)
    else:
        ok, detail = send_serverchan(token, title, message)
    log("推送结果 provider={} ok={} {}".format(provider, ok, detail))
    if not ok:
        return 1
    last_push_path.parent.mkdir(parents=True, exist_ok=True)
    last_push_path.write_text(
        json.dumps(
            {"date": target.isoformat(), "provider": provider, "sent_at": datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
