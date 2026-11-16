from __future__ import annotations

import csv
import json
import urllib.request
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
import sys
for path in (ROOT / "scripts", ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from render_strategy_summary import render_document  # noqa: E402
from stock_selection.engine import SelectionConfig, StockSelector, load_bars  # noqa: E402

STORE = ROOT / "data" / "a_share_public_latest.csv"
CONFIG = ROOT / "configs" / "stock_selection_optimized.json"
ETF_DIR = ROOT / "data" / "etf_daily"
BREADTH_CSV = ROOT / "output" / "breadth_series.csv"
REPORT_MD = ROOT / "docs" / "md" / "广度逆向策略回测.md"
START = date(2024, 2, 8)
END = date(2026, 11, 13)
SINA_URL = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
            "?symbol={symbol}&scale=240&ma=no&datalen=1000")

ETFS = {
    "510300.SH": ("sh510300", "沪深300ETF"),
    "510500.SH": ("sh510500", "中证500ETF"),
    "159915.SZ": ("sz159915", "创业板ETF"),
    "510880.SH": ("sh510880", "红利ETF"),
    "518880.SH": ("sh518880", "黄金ETF"),
}


def load_etf(code: str, sina_symbol: str) -> Dict[date, float]:
    path = ETF_DIR / "{}.csv".format(code)
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return {date.fromisoformat(row["date"]): float(row["close"]) for row in csv.DictReader(handle)}
    url = SINA_URL.format(symbol=sina_symbol)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 quant-research/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("ETF 数据格式异常: " + code)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in payload:
        rows.append({
            "date": item["day"], "open": item["open"], "high": item["high"],
            "low": item["low"], "close": item["close"], "volume": item["volume"],
        })
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)
    return {date.fromisoformat(row["date"]): float(row["close"]) for row in rows}


def load_breadth() -> Dict[date, float]:
    if BREADTH_CSV.exists():
        with BREADTH_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
            return {date.fromisoformat(row["date"]): float(row["breadth"]) for row in csv.DictReader(handle)}
    bars = load_bars(STORE)
    selector = StockSelector(bars, SelectionConfig.from_json(CONFIG))
    result = {}
    for day in selector.calendar:
        if START <= day <= END:
            result[day] = selector.market_breadth(day)
    BREADTH_CSV.parent.mkdir(parents=True, exist_ok=True)
    with BREADTH_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "breadth"])
        for day in sorted(result):
            writer.writerow([day.isoformat(), "{:.6f}".format(result[day])])
    return result


def backtest(etf: Dict[date, float], breadth: Dict[date, float], entry: float, exit_level: float,
             confirm_days: int, cost: float = 0.0015, cash_yield: float = 0.015,
             start: date = START) -> Dict[str, float]:
    days = [day for day in sorted(breadth) if day >= start and day in etf]
    if len(days) < 2:
        return {}
    cash = 100_000.0
    units = 0.0
    pending: Optional[str] = None
    pending_signal: Optional[date] = None
    below = 0
    trades = []
    trade_records = []
    entry_price = 0.0
    entry_date: Optional[date] = None
    entry_signal: Optional[date] = None
    equity_curve = []
    in_market_days = 0
    for index, day in enumerate(days):
        # Execute yesterday's signal at today's close.
        price = etf[day]
        if pending == "BUY" and units == 0:
            units = cash / (price * (1.0 + cost))
            entry_price = price
            entry_date = day
            entry_signal = pending_signal
            cash = 0.0
        elif pending == "SELL" and units > 0:
            cash = units * price * (1.0 - cost)
            trade_return = price / entry_price - 1.0 - 2.0 * cost
            trades.append(trade_return)
            trade_records.append({
                "entry_signal": entry_signal, "entry_date": entry_date, "entry_price": entry_price,
                "exit_signal": pending_signal, "exit_date": day, "exit_price": price,
                "return_pct": trade_return * 100.0,
                "holding_days": (day - entry_date).days if entry_date else None,
            })
            units = 0.0
            entry_price = 0.0
            entry_date = None
            entry_signal = None
        pending = None
        pending_signal = None
        if units == 0:
            cash *= 1.0 + cash_yield / 252.0
        else:
            in_market_days += 1
        value = cash + units * price
        equity_curve.append(value)
        # Generate tomorrow's signal from today's close/breadth.
        current = breadth[day]
        if current <= entry:
            below += 1
        else:
            below = 0
        if units == 0 and below >= confirm_days and index + 1 < len(days):
            pending = "BUY"
            pending_signal = day
        elif units > 0 and current >= exit_level and index + 1 < len(days):
            pending = "SELL"
            pending_signal = day
    if units > 0 and entry_date is not None:
        last_price = etf[days[-1]]
        trade_records.append({
            "entry_signal": entry_signal, "entry_date": entry_date, "entry_price": entry_price,
            "exit_signal": None, "exit_date": None, "exit_price": last_price,
            "return_pct": (last_price / entry_price - 1.0) * 100.0,
            "holding_days": (days[-1] - entry_date).days,
        })
    final = equity_curve[-1] if equity_curve else cash
    peak = equity_curve[0] if equity_curve else final
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, 1.0 - value / peak)
    return {
        "final": final,
        "return_pct": (final / 100_000.0 - 1.0) * 100.0,
        "mdd_pct": max_drawdown * 100.0,
        "trades": len(trades),
        "win_rate": (sum(1 for value in trades if value > 0) / len(trades) * 100.0) if trades else 0.0,
        "in_market_pct": in_market_days / len(days) * 100.0,
        "days": len(days),
        "trade_records": trade_records,
    }


def buy_hold(etf: Dict[date, float], start: date = START) -> Dict[str, float]:
    days = [day for day in sorted(etf) if day >= start]
    if len(days) < 2:
        return {}
    first, last = days[0], days[-1]
    value = 100_000.0 * (etf[last] / etf[first]) * (1.0 - 0.0015)
    peak = 0.0
    max_drawdown = 0.0
    for day in days:
        value_curve = 100_000.0 * etf[day] / etf[first]
        peak = max(peak, value_curve)
        max_drawdown = max(max_drawdown, 1.0 - value_curve / peak)
    return {"final": value, "return_pct": (value / 100_000.0 - 1.0) * 100.0,
            "mdd_pct": max_drawdown * 100.0, "days": len(days)}


def main() -> int:
    breadth = load_breadth()
    print("breadth days", len(breadth), flush=True)
    etf_data = {}
    for code, (sina, name) in ETFS.items():
        etf_data[code] = load_etf(code, sina)
        print("etf", code, len(etf_data[code]), flush=True)

    results = []
    for code, (_, name) in ETFS.items():
        for entry in (0.20, 0.25, 0.30):
            for exit_level in (0.45, 0.50, 0.55):
                for confirm in (1, 2):
                    full = backtest(etf_data[code], breadth, entry, exit_level, confirm)
                    y2026 = backtest(etf_data[code], breadth, entry, exit_level, confirm, start=date(2026, 1, 5))
                    if full:
                        results.append({
                            "code": code, "name": name, "entry": entry, "exit": exit_level,
                            "confirm": confirm, "full": full, "y2026": y2026,
                        })
    results.sort(key=lambda row: (row["full"]["return_pct"], -row["full"]["mdd_pct"]), reverse=True)
    benchmark = {code: buy_hold(etf_data[code]) for code in ETFS}

    lines = [
        "# 广度逆向策略回测",
        "",
        "> 规则：广度连续 N 天低于入场线时买入 ETF，广度回到出场线以上时卖出；其余时间持现金。",
        "> 信号日收盘生成，下一交易日收盘成交；单边成本 0.15%；现金年化 1.5%。",
        "> 区间：2024-02-08 至 2026-11-13；2026 单独列示。",
        "",
        "## 基准：买入持有",
        "",
        "| ETF | 代码 | 全区间收益 | 最大回撤 | 2026 数据 |",
        "|---|---|---:|---:|---:|",
    ]
    for code, (_, name) in ETFS.items():
        b = benchmark[code]
        y = buy_hold(etf_data[code], date(2026, 1, 5))
        lines.append("| {} | {} | {:+.2f}% | {:.2f}% | {:+.2f}% |".format(
            name, code, b["return_pct"], b["mdd_pct"], y["return_pct"]))
    lines.extend([
        "",
        "## 参数组合排名（按全区间收益）",
        "",
        "| 排名 | ETF | 入场 | 出场 | 确认天数 | 全区间收益 | 最大回撤 | 交易次数 | 持仓时间 | 2026 收益 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for rank, row in enumerate(results[:20], 1):
        full = row["full"]
        y = row["y2026"]
        lines.append("| {} | {} | ≤{:.0f}% | ≥{:.0f}% | {} | {:+.2f}% | {:.2f}% | {} | {:.0f}% | {:+.2f}% |".format(
            rank, row["name"], row["entry"] * 100, row["exit"] * 100, row["confirm"],
            full["return_pct"], full["mdd_pct"], full["trades"], full["in_market_pct"],
            y["return_pct"] if y else 0.0))
    lines.extend([
        "",
        "## 观察",
        "",
        "- 广度极低时买入、回到 50% 附近卖出，本质是左侧抄底；收益高度依赖入场后是否继续下跌。",
        "- 如果某个参数只在 2026 年好、2024–2025 差，属于过拟合，不应直接采用。",
        "- 该策略只持有一只 ETF，波动和回撤通常低于单票，但可能长期跑输买入持有。",
        "- 当前广度 40.16%，不满足“极低”入场条件，因此当前状态是空仓/持现金。",
        "",
        "> 以上为历史回测，不构成投资建议。",
        "",
    ])
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    html = render_document(REPORT_MD)
    print("wrote", REPORT_MD, html, flush=True)
    for row in results[:5]:
        print(row["name"], row["entry"], row["exit"], row["confirm"], round(row["full"]["return_pct"], 2), round(row["full"]["mdd_pct"], 2), round(row["y2026"]["return_pct"], 2) if row["y2026"] else None, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
