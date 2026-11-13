from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from daily_breadth_report import load_push_config, log, send_pushplus, send_serverchan  # noqa: E402
from render_strategy_summary import render_document  # noqa: E402

ALERT_JSON = ROOT / "output" / "breadth_latest.json"
OUTPUT_JSON = ROOT / "output"
MD_DIR = ROOT / "docs" / "md" / "午间复核"
PUSH_CONFIG = ROOT / ".secrets" / "push_config.json"
LAST_PUSH = ROOT / "output" / "last_midday_push.json"

QUOTE_URL = "https://qt.gtimg.cn/q={codes}"


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: float
    previous_close: float
    pct_change: float
    high: float
    low: float
    limit_up: float
    limit_down: float
    ask1_volume: float
    volume: float
    quote_date: str
    quote_time: str


def _float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_quote(symbol: str, payload: str) -> Optional[Quote]:
    """Parse one Tencent quote payload (the part inside the double quotes)."""
    parts = payload.split("~")
    if len(parts) < 49:
        return None
    timestamp = parts[30]
    quote_date = "{}-{}-{}".format(timestamp[:4], timestamp[4:6], timestamp[6:8]) if len(timestamp) >= 8 else ""
    quote_time = "{}:{}:{}".format(timestamp[8:10], timestamp[10:12], timestamp[12:14]) if len(timestamp) >= 14 else ""
    return Quote(
        symbol=symbol,
        name=parts[1] or symbol,
        price=_float(parts[3]),
        previous_close=_float(parts[4]),
        pct_change=_float(parts[32]),
        high=_float(parts[33]),
        low=_float(parts[34]),
        limit_up=_float(parts[47]),
        limit_down=_float(parts[48]),
        ask1_volume=_float(parts[20]),
        volume=_float(parts[6]),
        quote_date=quote_date,
        quote_time=quote_time,
    )


def fetch_quotes(symbols: Sequence[str]) -> Dict[str, Quote]:
    quotes: Dict[str, Quote] = {}
    unique = sorted(set(symbols))
    for offset in range(0, len(unique), 50):
        batch = [symbol for symbol in unique[offset : offset + 50] if symbol.count(".") == 1]
        if not batch:
            continue
        code_to_symbol = {symbol.split(".")[0]: symbol for symbol in batch}
        codes = [("sh" if symbol.split(".")[1] == "SH" else "sz") + symbol.split(".")[0]
                 for symbol in batch]
        url = QUOTE_URL.format(codes=",".join(codes))
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 quant-research/1.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                text = response.read().decode("gbk", "replace")
        except Exception as exc:
            log("实时行情获取失败：{}".format(exc))
            continue
        for segment in text.strip().split(";"):
            if "=" not in segment:
                continue
            payload = segment.split("=", 1)[1].strip().strip('"')
            parts = payload.split("~")
            if len(parts) < 3:
                continue
            symbol = code_to_symbol.get(parts[2])
            if symbol is None:
                continue
            quote = parse_quote(symbol, payload)
            if quote is not None:
                quotes[symbol] = quote
    return quotes


def quote_state(quote: Optional[Quote], today: date) -> str:
    if quote is None or quote.price <= 0 or quote.volume <= 0:
        return "no_quote"
    if quote.quote_date != today.isoformat():
        return "no_quote"
    if quote.limit_up > 0 and quote.price >= quote.limit_up - 0.001:
        return "limit_up"
    if quote.limit_down > 0 and quote.price <= quote.limit_down + 0.001:
        return "limit_down"
    return "normal"


def classify_order(order: Dict[str, object], quote: Optional[Quote], today: date) -> Dict[str, object]:
    side = str(order.get("side", "")).upper()
    reference = _float(str(order.get("reference_price", 0.0)))
    state = quote_state(quote, today)
    price = quote.price if quote is not None else None
    change = (price / reference - 1.0) * 100.0 if price and reference else None
    name = quote.name if quote is not None else str(order.get("symbol", ""))

    if state == "no_quote":
        action = "no_quote"
        label = "无行情（停牌/休市）"
        advice = "无法卖出" if side == "SELL" else "不执行"
    elif side == "BUY":
        if state == "limit_up":
            action = "skip"
            label = "涨停封板" if quote is not None and quote.ask1_volume <= 0 else "涨停未封死"
            advice = "跳过，不追涨停"
        elif state == "limit_down":
            action = "execute"
            label = "跌停"
            advice = "可买但风险高，需谨慎"
        else:
            action = "execute"
            label = "正常"
            advice = "可买入"
    else:
        if state == "limit_down":
            action = "defer"
            label = "跌停"
            advice = "延后，跌停可能卖不出"
        elif state == "limit_up":
            action = "execute"
            label = "涨停"
            advice = "可卖出"
        else:
            action = "execute"
            label = "正常"
            advice = "可卖出"

    return {
        "side": side,
        "symbol": str(order.get("symbol", "")),
        "name": name,
        "shares": int(order.get("shares", 0) or 0),
        "reference_price": reference,
        "price": round(price, 4) if price is not None else None,
        "change_vs_reference_pct": round(change, 2) if change is not None else None,
        "pct_change": round(quote.pct_change, 2) if quote is not None else None,
        "limit_up": round(quote.limit_up, 2) if quote is not None else None,
        "limit_down": round(quote.limit_down, 2) if quote is not None else None,
        "quote_time": quote.quote_time if quote is not None else "",
        "state": state,
        "action": action,
        "label": label,
        "advice": advice,
    }


def build_result(alert: Dict[str, object], quotes: Dict[str, Quote], today: date) -> Dict[str, object]:
    orders = [item for item in alert.get("orders", []) if isinstance(item, dict)]
    rows = [classify_order(order, quotes.get(str(order.get("symbol", ""))), today) for order in orders]
    buys = [row for row in rows if row["side"] == "BUY"]
    sells = [row for row in rows if row["side"] == "SELL"]
    summary = {
        "buy": len(buys),
        "sell": len(sells),
        "execute": sum(row["action"] == "execute" for row in rows),
        "skip": sum(row["action"] == "skip" for row in rows),
        "defer": sum(row["action"] == "defer" for row in rows),
        "no_quote": sum(row["action"] == "no_quote" for row in rows),
    }
    quote_times = sorted({row["quote_time"] for row in rows if row["quote_time"]})
    return {
        "date": today.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "signal_date": alert.get("signal_date"),
        "quote_time": quote_times[-1] if quote_times else None,
        "rows": rows,
        "summary": summary,
        "actionable": bool(rows),
    }


def _row_text(row: Dict[str, object]) -> str:
    change = row.get("change_vs_reference_pct")
    change_text = "{:+.2f}%".format(float(change)) if change is not None else "—"
    price = row.get("price")
    price_text = "{:.2f}".format(float(price)) if price is not None else "—"
    return "{} {}：现价 {}（较参考 {}）｜{}｜{}｜{}".format(
        row["symbol"], row["name"], price_text, change_text,
        row["label"], row["advice"], "{} 股".format(row["shares"]),
    )


def format_markdown(result: Dict[str, object]) -> str:
    summary = result["summary"]
    lines = [
        "**午间执行复核 {}**".format(result["date"]),
        "",
        "信号日：{}｜行情时间：{}".format(result["signal_date"] or "—", result["quote_time"] or "—"),
        "可执行 {}｜跳过 {}｜延后 {}｜无行情 {}".format(
            summary["execute"], summary["skip"], summary["defer"], summary["no_quote"]),
    ]
    if not result["rows"]:
        lines.append("今日无待执行买卖清单。")
        return "\n".join(lines)

    for side, title in (("BUY", "**买入**"), ("SELL", "**卖出**")):
        rows = [row for row in result["rows"] if row["side"] == side]
        if not rows:
            continue
        lines.extend(["", title])
        for row in rows:
            lines.append("- " + _row_text(row))
    lines.extend([
        "",
        "11:30–13:00 休市，不能成交；请在 13:00 开盘后按实际价格再确认。",
        "涨停买入跳过，跌停卖出延后；本提醒不自动下单。",
    ])
    return "\n".join(lines)


def write_report(result: Dict[str, object]) -> Tuple[Path, Path]:
    OUTPUT_JSON.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_JSON / "midday_check_{}.json".format(result["date"])
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    MD_DIR.mkdir(parents=True, exist_ok=True)
    md_path = MD_DIR / "{}.md".format(result["date"])
    summary = result["summary"]
    lines = [
        "# 午间执行复核 {}".format(result["date"]),
        "",
        "> 生成时间：{}".format(result["generated_at"]),
        "> 信号日期：{}".format(result["signal_date"] or "—"),
        "> 行情时间：{}".format(result["quote_time"] or "—"),
        "> 依据：前一晚 `output/breadth_latest.json` 的模拟盘买卖清单；不自动下单。",
        "",
        "## 汇总",
        "",
        "| 项目 | 数量 |",
        "|---|---:|",
        "| 买入 | {} |".format(summary["buy"]),
        "| 卖出 | {} |".format(summary["sell"]),
        "| 可执行 | {} |".format(summary["execute"]),
        "| 跳过 | {} |".format(summary["skip"]),
        "| 延后 | {} |".format(summary["defer"]),
        "| 无行情 | {} |".format(summary["no_quote"]),
        "",
    ]
    if not result["rows"]:
        lines.extend(["今日无待执行买卖清单。", ""])
    for side, title in (("BUY", "## 买入"), ("SELL", "## 卖出")):
        rows = [row for row in result["rows"] if row["side"] == side]
        if not rows:
            continue
        lines.extend([
            title,
            "",
            "| 代码 | 名称 | 信号股数 | 参考价 | 现价 | 较参考 | 状态 | 建议 |",
            "|---|---|---:|---:|---:|---:|---|---|",
        ])
        for row in rows:
            price = "—" if row["price"] is None else "{:.2f}".format(float(row["price"]))
            change = "—" if row["change_vs_reference_pct"] is None else "{:+.2f}%".format(float(row["change_vs_reference_pct"]))
            lines.append("| {} | {} | {} | {:.2f} | {} | {} | {} | {} |".format(
                row["symbol"], row["name"], row["shares"], float(row["reference_price"]),
                price, change, row["label"], row["advice"]))
        lines.append("")
    lines.extend([
        "## 说明",
        "",
        "- A 股 11:30–13:00 休市，午间不能成交；本表用于 13:00 开盘后的执行复核。",
        "- 计划买入的股票若已涨停，按规则跳过；计划卖出的股票若跌停，延后。",
        "- 现价为 11:30 行情，13:00 后可能变化，下单前请再次核对。",
        "- 本报告基于模拟盘清单，不读取券商真实持仓，也不自动下单。",
        "",
    ])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    html_path = render_document(md_path)
    return md_path, html_path


def main() -> int:
    parser = argparse.ArgumentParser(description="A股午间执行复核：检查前一晚买卖清单的涨跌停状态")
    parser.add_argument("--send", action="store_true", help="推送到已配置的微信渠道")
    parser.add_argument("--force", action="store_true", help="忽略当日已推送记录")
    parser.add_argument("--alert", type=Path, default=ALERT_JSON, help="买卖提醒 JSON 路径")
    parser.add_argument("--config", type=Path, default=PUSH_CONFIG, help="推送配置文件")
    parser.add_argument("--provider", choices=["pushplus", "serverchan"], help="覆盖推送渠道")
    parser.add_argument("--token", help="覆盖 token / sendkey")
    parser.add_argument("--topic", help="PushPlus 群组编码")
    args = parser.parse_args()

    if not args.alert.exists():
        log("找不到买卖提醒文件：{}".format(args.alert))
        return 1
    data = json.loads(args.alert.read_text(encoding="utf-8"))
    alert = data.get("trade_alert") or {}
    orders = alert.get("orders") or []
    today = datetime.now().date()
    signal_date = alert.get("signal_date")
    if signal_date:
        try:
            if date.fromisoformat(str(signal_date)) >= today:
                log("信号日期 {} 尚未到执行日，跳过".format(signal_date))
                return 0
        except ValueError:
            log("信号日期格式无效：{}".format(signal_date))
            return 1

    if not orders:
        log("今日无待执行买卖清单")
        return 0

    quotes = fetch_quotes([str(item.get("symbol", "")) for item in orders if isinstance(item, dict)])
    if not any(quote.quote_date == today.isoformat() for quote in quotes.values()):
        log("今日无实时行情（休市或数据延迟），不生成执行建议")
        return 0

    result = build_result(alert, quotes, today)
    md_path, html_path = write_report(result)
    message = format_markdown(result)
    print("\n" + message + "\n", flush=True)
    log("午间复核完成：{}｜{}｜{}".format(md_path, html_path, result["summary"]))
    if not args.send:
        log("未加 --send，仅生成报告")
        return 0

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
    if not provider or not token:
        log("未配置推送渠道或 token：请在 {} 中填写 provider 与对应 token".format(args.config))
        return 1

    if LAST_PUSH.exists() and not args.force:
        try:
            last = json.loads(LAST_PUSH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            last = {}
        if last.get("date") == today.isoformat():
            log("{} 已推送过，跳过；需要重发请加 --force".format(today))
            return 0

    if provider == "pushplus":
        ok, detail = send_pushplus(token, "午间执行复核 {}".format(today), message, topic)
    else:
        ok, detail = send_serverchan(token, "午间执行复核 {}".format(today), message)
    log("推送结果 provider={} ok={} {}".format(provider, ok, detail))
    if not ok:
        return 1
    LAST_PUSH.write_text(
        json.dumps({"date": today.isoformat(), "provider": provider, "signal_date": signal_date}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
