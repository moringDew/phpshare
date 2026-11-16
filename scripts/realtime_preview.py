from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BACKEND = ROOT / "backend"
for path in (SCRIPTS, BACKEND):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from daily_breadth_report import load_push_config, log, send_pushplus, send_serverchan  # noqa: E402
from intraday_store import save_quotes  # noqa: E402
from midday_check import Quote, fetch_quotes, quote_state  # noqa: E402
from render_strategy_summary import render_document  # noqa: E402
from stock_selection.engine import Bar, SelectionConfig, StockSelector, load_bars  # noqa: E402
from stock_selection.real_account import snapshot  # noqa: E402

STORE = ROOT / "data" / "a_share_public_latest.csv"
CONFIG = ROOT / "configs" / "stock_selection_optimized.json"
LATEST = ROOT / "output" / "realtime_preview_latest.json"
PUSH_CONFIG = ROOT / ".secrets" / "push_config.json"
LAST_PUSH = ROOT / "output" / "last_realtime_push.json"
MD_DIR = ROOT / "docs" / "md" / "实时预演"
LEDGER = ROOT / "data" / "real_account" / "trades.sqlite3"
SEPARATE_SYMBOL = "600869.SH"

MORNING_OPEN = time(9, 30)
# Allow a few minutes for the 11:30 task trigger, while quotes still reflect 11:30.
MORNING_CLOSE = time(11, 35)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)


def is_trading_time(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    current = now.time()
    return (MORNING_OPEN <= current <= MORNING_CLOSE) or (AFTERNOON_OPEN <= current <= AFTERNOON_CLOSE)


def build_provisional_bars(history: Sequence[Bar], last_rows: Mapping[str, Bar],
                           quotes: Mapping[str, Quote], today: date) -> List[Bar]:
    bars = list(history)
    for symbol, last in last_rows.items():
        quote = quotes.get(symbol)
        if quote is not None and quote.quote_date == today.isoformat() and quote.price > 0 and quote.volume > 0:
            volume = quote.volume * 100.0
            bars.append(Bar(
                trade_date=today, symbol=symbol, close=quote.price, volume=volume,
                adj_factor=1.0, amount=quote.price * volume, is_st=last.is_st,
                suspended=False, listing_days=(last.listing_days or 0) + 1,
            ))
        else:
            bars.append(Bar(
                trade_date=today, symbol=symbol, close=last.close, volume=0.0,
                adj_factor=last.adj_factor, amount=0.0, is_st=last.is_st,
                suspended=True, listing_days=(last.listing_days or 0) + 1,
            ))
    return bars


def diff_picks(previous: Sequence[str], current: Sequence[str]) -> Tuple[List[str], List[str]]:
    before, after = set(previous), set(current)
    return sorted(after - before), sorted(before - after)


def load_history(today: date, store: Path = STORE) -> Tuple[List[Bar], Dict[str, Bar]]:
    bars = [bar for bar in load_bars(store) if bar.trade_date < today]
    if not bars:
        raise ValueError("行情文件没有可用的历史数据")
    last_date = max(bar.trade_date for bar in bars)
    last_rows = {bar.symbol: bar for bar in bars if bar.trade_date == last_date}
    if not last_rows:
        raise ValueError("行情文件最新日期没有股票记录")
    return bars, last_rows


def load_previous(today: date) -> Dict[str, object]:
    if not LATEST.exists():
        return {}
    try:
        data = json.loads(LATEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if data.get("date") == today.isoformat() else {}


def separate_holding(quotes: Dict[str, Quote], today: date) -> Optional[Dict[str, object]]:
    cost = 13.48
    shares = 0
    try:
        if LEDGER.exists():
            account = snapshot(LEDGER)
            holding = next((row for row in account["holdings"] if row["symbol"] == SEPARATE_SYMBOL), None)
            if holding:
                cost = float(holding["average_cost"])
                shares = int(holding["shares"])
    except Exception as exc:
        log("读取真实持仓失败：{}".format(exc))
    if SEPARATE_SYMBOL not in quotes:
        quotes.update(fetch_quotes([SEPARATE_SYMBOL]))
    quote = quotes.get(SEPARATE_SYMBOL)
    if quote is None:
        return dict(symbol=SEPARATE_SYMBOL, name="远东股份", shares=shares, cost=cost,
                    price=None, pct_change=None, quote_time="", state="no_quote")
    stop = round(cost * 0.92, 2)
    distance = (quote.price - stop) / quote.price * 100.0 if quote.price else None
    return dict(symbol=SEPARATE_SYMBOL, name=quote.name, shares=shares, cost=cost,
                stop=stop, price=round(quote.price, 2), pct_change=round(quote.pct_change, 2),
                distance_to_stop_pct=round(distance, 2) if distance is not None else None,
                quote_time=quote.quote_time, state=quote_state(quote, today))


def describe_picks(selector: StockSelector, picks, quotes: Mapping[str, Quote], today: date) -> List[Dict[str, object]]:
    rows = []
    for candidate in picks:
        quote = quotes.get(candidate.symbol)
        state = quote_state(quote, today)
        if state == "limit_up":
            label = "涨停，不追"
        elif state == "limit_down":
            label = "跌停"
        elif state == "no_quote":
            label = "无行情/停牌"
        else:
            label = "正常"
        rows.append({
            "symbol": candidate.symbol,
            "name": quote.name if quote is not None else candidate.symbol,
            "score": round(candidate.score, 4),
            "price": round(quote.price, 2) if quote is not None else None,
            "pct_change": round(quote.pct_change, 2) if quote is not None else None,
            "state": state,
            "label": label,
        })
    return rows


def format_message(result: Dict[str, object]) -> str:
    lines = [
        "**实时预演 {} {}**".format(result["date"], result["time"]),
        "",
        "广度 {:.2f}%｜{}（门槛 >{:.0f}%）".format(
            float(result["breadth"]) * 100,
            "风险开" if result["risk_on"] else "空仓",
            float(result.get("threshold", 0.5)) * 100,
        ),
    ]
    rows = result["picks"]
    if not rows:
        lines.append("目标：空仓，不买。")
    else:
        lines.append("目标（每日重算 {} 只）：".format(len(rows)))
        for row in rows:
            price = "—" if row["price"] is None else "{:.2f}".format(float(row["price"]))
            change = "—" if row["pct_change"] is None else "{:+.2f}%".format(float(row["pct_change"]))
            lines.append("- {} {}｜现价 {}｜涨跌幅 {}｜{}".format(
                row["symbol"], row["name"], price, change, row["label"]))
    added, removed = result["added"], result["removed"]
    if not result["previous_time"]:
        lines.append("首次预演，无上次对比")
    elif added or removed:
        parts = []
        if added:
            parts.append("新增 " + "、".join(added))
        if removed:
            parts.append("剔除 " + "、".join(removed))
        lines.append("较上次（{}）：{}".format(result["previous_time"], "；".join(parts)))
    else:
        lines.append("较上次：无变化")
    separate = result.get("separate")
    if separate:
        price = "—" if separate.get("price") is None else "{:.2f}".format(float(separate["price"]))
        stop = separate.get("stop")
        distance = separate.get("distance_to_stop_pct")
        lines.append("")
        if stop is not None:
            distance_text = "—" if distance is None else "{:.1f}%".format(float(distance))
            lines.append("{} {}（单独）：现价 {}｜成本 {:.2f}｜止损 {:.2f}｜距止损 {}".format(
                separate["symbol"], separate["name"], price, float(separate["cost"]),
                float(stop), distance_text))
        else:
            lines.append("{} {}（单独）：现价 {}".format(separate["symbol"], separate["name"], price))
    lines.extend([
        "",
        "日频建议，独立于 20 日调仓策略；盘中价不是收盘价，收盘后需再复核；不自动下单。",
    ])
    return "\n".join(lines)


def write_history(result: Dict[str, object]) -> Path:
    OUTPUT = ROOT / "output"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / "realtime_preview_{}.csv".format(result["date"])
    rows: List[Dict[str, str]] = []
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    rows = [row for row in rows if row.get("time") != result["time"]]
    rows.append({
        "time": str(result["time"]),
        "breadth_pct": "{:.2f}".format(float(result["breadth"]) * 100),
        "risk_on": "1" if result["risk_on"] else "0",
        "picks": ",".join(str(row["symbol"]) for row in result["picks"]),
        "added": ",".join(result["added"]),
        "removed": ",".join(result["removed"]),
    })
    rows.sort(key=lambda row: row["time"])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["time", "breadth_pct", "risk_on", "picks", "added", "removed"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_report(result: Dict[str, object], history_path: Path) -> Tuple[Path, Path]:
    MD_DIR.mkdir(parents=True, exist_ok=True)
    path = MD_DIR / "{}.md".format(result["date"])
    rows: List[Dict[str, str]] = []
    if history_path.exists():
        with history_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    lines = [
        "# 实时预演 {}".format(result["date"]),
        "",
        "> 最新时间：{}".format(result["time"]),
        "> 行情来源：腾讯实时行情，盘中价作为今日临时收盘价",
        "> 口径：每日重算、5 只、总仓位 40%、广度门槛 >{:.0f}%、涨停不追".format(
            float(result.get("threshold", 0.5)) * 100),
        "> 用途：日频建议，独立于 20 日调仓策略；不构成投资建议",
        "",
        "## 最新状态",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        "| 市场广度 | {:.2f}% |".format(float(result["breadth"]) * 100),
        "| 广度门槛 | >{:.0f}% |".format(float(result.get("threshold", 0.5)) * 100),
        "| 风险状态 | {} |".format("风险开" if result["risk_on"] else "空仓"),
        "| 目标数量 | {} |".format(len(result["picks"])),
        "",
    ]
    if result["picks"]:
        lines.extend([
            "## 目标",
            "",
            "| 代码 | 名称 | 得分 | 现价 | 涨跌幅 | 状态 |",
            "|---|---|---:|---:|---:|---|",
        ])
        for row in result["picks"]:
            price = "—" if row["price"] is None else "{:.2f}".format(float(row["price"]))
            change = "—" if row["pct_change"] is None else "{:+.2f}%".format(float(row["pct_change"]))
            lines.append("| {} | {} | {:.4f} | {} | {} | {} |".format(
                row["symbol"], row["name"], float(row["score"]), price, change, row["label"]))
        lines.append("")
    else:
        lines.extend(["当前目标为空仓。", ""])
    lines.extend(["## 今日盘中历史", "", "| 时间 | 广度 | 状态 | 目标 | 新增 | 剔除 |", "|---|---:|---|---|---|---|"])
    for row in rows:
        lines.append("| {} | {}% | {} | {} | {} | {} |".format(
            row.get("time", ""), row.get("breadth_pct", ""),
            "风险开" if row.get("risk_on") == "1" else "空仓",
            row.get("picks", "") or "—", row.get("added", "") or "—", row.get("removed", "") or "—"))
    lines.append("")
    separate = result.get("separate")
    if separate:
        lines.extend([
            "## 单独跟踪：600869 远东股份",
            "",
            "| 项目 | 数值 |",
            "|---|---:|",
            "| 股数 | {} |".format(separate.get("shares", 0)),
            "| 成本 | {:.2f} |".format(float(separate.get("cost", 0.0))),
            "| 现价 | {} |".format("—" if separate.get("price") is None else "{:.2f}".format(float(separate["price"]))),
            "| 止损价 | {} |".format("—" if separate.get("stop") is None else "{:.2f}".format(float(separate["stop"]))),
            "| 距止损 | {} |".format("—" if separate.get("distance_to_stop_pct") is None else "{:.1f}%".format(float(separate["distance_to_stop_pct"]))),
            "",
            "该股票不进入策略目标；跌破止损价时单独提示。",
            "",
        ])
    lines.extend([
        "## 说明",
        "",
        "- 盘中用当前价代替今日收盘价重算，14:45 前的结果可能与收盘数据不同。",
        "- 本预演是日频重算；20:00 的 20 日调仓信号是另一条策略，不互相替代。",
        "- 涨停股不追；停牌/无行情不买。",
        "- 本报告只生成建议，不自动下单。",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path, render_document(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="A股盘中实时预演：每15分钟用实时价重算广度和目标")
    parser.add_argument("--send", action="store_true", help="推送到已配置的微信渠道")
    parser.add_argument("--force", action="store_true", help="忽略同一时间槽的推送记录")
    parser.add_argument("--push-mode", choices=["always", "change"], default="always",
                        help="always=每次都推；change=目标变化时推（14:45 仍推）")
    parser.add_argument("--breadth-threshold", type=float, default=0.50,
                        help="广度门槛，严格大于该值才建议；默认 0.50")
    parser.add_argument("--config", type=Path, default=CONFIG, help="策略配置文件")
    parser.add_argument("--store", type=Path, default=STORE, help="历史行情 CSV")
    args = parser.parse_args()

    now = datetime.now()
    if not is_trading_time(now):
        log("{} 非交易时段，跳过".format(now.strftime("%Y-%m-%d %H:%M")))
        return 0
    today = now.date()
    try:
        history, last_rows = load_history(today, args.store)
    except Exception as exc:
        log("读取历史行情失败：{}".format(exc))
        return 1
    quotes = fetch_quotes(sorted(last_rows))
    if not any(quote.quote_date == today.isoformat() for quote in quotes.values()):
        log("今日无实时行情（休市或数据延迟），跳过")
        return 0
    try:
        save_quotes(today.isoformat(), now.strftime("%H:%M"), quotes)
    except Exception as exc:
        log("保存本地行情快照失败：{}".format(exc))
    bars = build_provisional_bars(history, last_rows, quotes, today)
    config = SelectionConfig.from_json(args.config)
    threshold = args.breadth_threshold
    selector = StockSelector(bars, config)
    breadth = selector.market_breadth(today)
    risk_on = breadth > threshold
    picks = selector.select(today) if risk_on else []
    previous = load_previous(today)
    previous_picks = previous.get("picks") or []
    added, removed = diff_picks(previous_picks, [item.symbol for item in picks])
    result = {
        "date": today.isoformat(),
        "time": now.strftime("%H:%M"),
        "generated_at": now.isoformat(timespec="seconds"),
        "breadth": round(breadth, 6),
        "threshold": round(threshold, 6),
        "risk_on": risk_on,
        "picks": describe_picks(selector, picks, quotes, today),
        "added": added,
        "removed": removed,
        "previous_time": previous.get("time") if previous else None,
        "separate": separate_holding(dict(quotes), today),
    }
    history_path = write_history(result)
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path, html_path = write_report(result, history_path)
    message = format_message(result)
    print("\n" + message + "\n", flush=True)
    log("实时预演完成：{}｜{}｜{}".format(md_path, html_path, result["picks"]))

    if not args.send:
        log("未加 --send，仅生成报告")
        return 0
    if args.push_mode == "change" and not added and not removed and result["time"] != "14:45":
        log("目标无变化，跳过推送")
        return 0

    slot = "{} {}".format(result["date"], result["time"])
    if LAST_PUSH.exists() and not args.force:
        try:
            last = json.loads(LAST_PUSH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            last = {}
        if last.get("slot") == slot:
            log("{} 已推送过，跳过；需要重发请加 --force".format(slot))
            return 0

    config = load_push_config(PUSH_CONFIG)
    provider = config.get("provider")
    if provider == "pushplus":
        token = config.get("pushplus_token")
        topic = config.get("pushplus_topic") or None
    elif provider == "serverchan":
        token = config.get("serverchan_sendkey")
        topic = None
    else:
        token = ""
        topic = None
    if not provider or not token:
        log("未配置推送渠道或 token：请在 {} 中填写".format(PUSH_CONFIG))
        return 1

    title = "实时预演 {} {}".format(result["date"], result["time"])
    if provider == "pushplus":
        ok, detail = send_pushplus(token, title, message, topic)
    else:
        ok, detail = send_serverchan(token, title, message)
    log("推送结果 provider={} ok={} {}".format(provider, ok, detail))
    if not ok:
        return 1
    LAST_PUSH.write_text(json.dumps({"slot": slot, "provider": provider}, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
