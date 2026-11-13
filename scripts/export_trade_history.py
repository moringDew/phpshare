from __future__ import annotations

import csv
import json
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from stock_selection.capital import run_capital_backtest  # noqa: E402
from stock_selection.engine import SelectionConfig, StockSelector, load_bars  # noqa: E402

STORE = ROOT / "data" / "a_share_public_latest.csv"
CONFIG = ROOT / "configs" / "stock_selection_optimized.json"
STRATEGY_START = date(2026, 10, 8)
RESEARCH_START = date(2024, 2, 8)
END = date(2026, 11, 13)
OUT_STRATEGY_CSV = ROOT / "output" / "trades_strategy_20261113.csv"
OUT_RESEARCH_CSV = ROOT / "output" / "trades_research_20261113.csv"
OUT_MD = ROOT / "docs" / "TRADE_HISTORY.md"


def fetch_names(symbols: List[str]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for offset in range(0, len(symbols), 50):
        batch = symbols[offset : offset + 50]
        codes = []
        for symbol in batch:
            code, exchange = symbol.split(".")
            codes.append(("sh" if exchange == "SH" else "sz") + code)
        url = "https://qt.gtimg.cn/q=" + ",".join(codes)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                text = response.read().decode("gbk", "replace")
            code_to_symbol = {symbol.split(".")[0]: symbol for symbol in batch}
            for segment in text.strip().split(";"):
                if "=" not in segment:
                    continue
                parts = segment.split("=", 1)[1].strip().strip('"').split("~")
                if len(parts) >= 3 and parts[2] in code_to_symbol:
                    names[code_to_symbol[parts[2]]] = parts[1]
        except Exception:
            continue
    return names


@dataclass
class Analysis:
    label: str
    start: date
    end: date
    trades: List[Dict[str, object]]
    positions: List[Dict[str, object]]
    cash: float
    equity: float
    total_return: float
    maximum_drawdown: float
    fees: float
    realized_pnl: float
    unrealized_pnl: float


def analyze(label: str, config: SelectionConfig, start: date, end: date, names: Dict[str, str]) -> Analysis:
    selector = StockSelector(load_bars(STORE), config)
    result = run_capital_backtest(selector, start, end)
    positions_state: Dict[str, Dict[str, float]] = {}
    rows: List[Dict[str, object]] = []
    realized_total = 0.0

    for trade in result.trades:
        symbol = str(trade["symbol"])
        shares = int(trade["shares"])
        price = float(trade["price"])
        fee = float(trade["fee"])
        amount = shares * price
        state = positions_state.setdefault(symbol, {"shares": 0.0, "cost": 0.0})
        realized = None
        if trade["side"] == "BUY":
            state["shares"] += shares
            state["cost"] += amount + fee
            cash_flow = -(amount + fee)
        else:
            average_cost = state["cost"] / state["shares"] if state["shares"] else price
            realized = (price - average_cost) * shares - fee
            realized_total += realized
            state["cost"] -= average_cost * shares
            state["shares"] -= shares
            cash_flow = amount - fee
        rows.append(
            {
                "date": trade["date"],
                "signal_date": trade["signal_date"],
                "side": trade["side"],
                "symbol": symbol,
                "name": names.get(symbol, symbol),
                "shares": shares,
                "price": price,
                "amount": amount,
                "fee": fee,
                "cash_flow": cash_flow,
                "realized_pnl": realized,
                "reason": trade.get("reason", "rebalance"),
            }
        )

    positions: List[Dict[str, object]] = []
    unrealized_total = 0.0
    for symbol, state in positions_state.items():
        shares = int(state["shares"])
        if shares <= 0:
            continue
        average_cost = state["cost"] / shares
        bar = selector.by_date.get(end, {}).get(symbol)
        if bar is None:
            continue
        price = bar.close * bar.adj_factor
        market_value = shares * price
        unrealized = market_value - state["cost"]
        unrealized_total += unrealized
        positions.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, symbol),
                "shares": shares,
                "cost": average_cost,
                "price": price,
                "market_value": market_value,
                "unrealized_pnl": unrealized,
            }
        )
    positions.sort(key=lambda item: str(item["symbol"]))

    return Analysis(
        label=label,
        start=start,
        end=end,
        trades=rows,
        positions=positions,
        cash=result.final_cash,
        equity=result.final_equity,
        total_return=result.total_return,
        maximum_drawdown=result.maximum_drawdown,
        fees=result.fees,
        realized_pnl=realized_total,
        unrealized_pnl=unrealized_total,
    )


def write_trades_csv(path: Path, analysis: Analysis) -> None:
    fields = ["date", "signal_date", "side", "reason", "symbol", "name", "shares", "price", "amount", "fee", "cash_flow", "realized_pnl"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in analysis.trades:
            item = dict(row)
            item["price"] = "{:.2f}".format(float(item["price"]))
            item["amount"] = "{:.2f}".format(float(item["amount"]))
            item["fee"] = "{:.2f}".format(float(item["fee"]))
            item["cash_flow"] = "{:.2f}".format(float(item["cash_flow"]))
            item["realized_pnl"] = "" if item["realized_pnl"] is None else "{:.2f}".format(float(item["realized_pnl"]))
            writer.writerow(item)


def trades_table(analysis: Analysis, limit: Optional[int] = None) -> List[str]:
    rows = analysis.trades[-limit:] if limit else analysis.trades
    lines = [
        "| 日期 | 方向 | 原因 | 代码 | 名称 | 股数 | 成交价 | 成交额 | 手续费 | 现金流 | 已实现盈亏 |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        realized = "—" if row["realized_pnl"] is None else "{:+,.2f}".format(float(row["realized_pnl"]))
        reason = "止损" if row.get("reason") == "stop" else "调仓"
        lines.append(
            "| {date} | {side} | {reason} | {symbol} | {name} | {shares} | {price:.2f} | {amount:,.0f} | {fee:,.2f} | {cash:+,.0f} | {realized} |".format(
                date=row["date"], side=row["side"], reason=reason, symbol=row["symbol"], name=row["name"],
                shares=row["shares"], price=float(row["price"]), amount=float(row["amount"]),
                fee=float(row["fee"]), cash=float(row["cash_flow"]), realized=realized,
            )
        )
    return lines


def positions_table(analysis: Analysis) -> List[str]:
    lines = [
        "| 代码 | 名称 | 股数 | 成本 | 现价 | 市值 | 浮动盈亏 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in analysis.positions:
        lines.append(
            "| {symbol} | {name} | {shares} | {cost:.2f} | {price:.2f} | {value:,.0f} | {pnl:+,.0f} |".format(
                symbol=row["symbol"], name=row["name"], shares=row["shares"],
                cost=float(row["cost"]), price=float(row["price"]),
                value=float(row["market_value"]), pnl=float(row["unrealized_pnl"]),
            )
        )
    return lines


def main() -> int:
    bars_symbols = set()
    for bar in load_bars(STORE):
        if bar.trade_date >= RESEARCH_START:
            bars_symbols.add(bar.symbol)
    names = fetch_names(sorted(bars_symbols))

    strategy_config = SelectionConfig.from_json(CONFIG)
    strategy = analyze("策略模拟盘", strategy_config, STRATEGY_START, END, names)

    research_config = SelectionConfig.from_json(CONFIG)
    research_config.strategy_start = None
    research = analyze("研究回测", research_config, RESEARCH_START, END, names)

    write_trades_csv(OUT_STRATEGY_CSV, strategy)
    write_trades_csv(OUT_RESEARCH_CSV, research)

    lines = [
        "# 交割历史单汇总",
        "",
        "> 数据截至：2026-11-13",
        "> 当前配置：5 只 / 40% 总仓位 / 单票 10% / 现金年化 1.5%",
        "",
        "## 一、策略模拟盘（2026-10-08 起）",
        "",
        "策略起点设为 2026-10-01，国庆休市后首个交易日为 10-08。首次调仓 10-08 因广度不足空仓，11-05 信号、11-06 成交买入 5 只。",
        "",
        "### 汇总",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        "| 交易笔数 | {} |".format(len(strategy.trades)),
        "| 止损卖出 | {} 笔 |".format(sum(1 for row in strategy.trades if row.get("reason") == "stop")),
        "| 手续费合计 | {:,.2f} 元 |".format(strategy.fees),
        "| 已实现盈亏 | {:+,.2f} 元 |".format(strategy.realized_pnl),
        "| 浮动盈亏 | {:+,.2f} 元 |".format(strategy.unrealized_pnl),
        "| 现金 | {:,.2f} 元 |".format(strategy.cash),
        "| 总资产 | {:,.2f} 元 |".format(strategy.equity),
        "| 累计收益 | {:+.2f}% |".format(strategy.total_return * 100),
        "| 最大回撤 | {:.2f}% |".format(strategy.maximum_drawdown * 100),
        "",
        "### 成交明细",
        "",
    ]
    lines.extend(trades_table(strategy))
    lines.extend(["", "### 当前持仓", ""])
    lines.extend(positions_table(strategy))
    lines.extend(
        [
            "",
            "完整明细：`output/trades_strategy_20261113.csv`",
            "",
            "## 二、研究回测（2024-02-08 起）",
            "",
            "这一部分是策略在完整研究区间的历史成交，不代表当前模拟盘持仓。",
            "",
            "### 汇总",
            "",
            "| 项目 | 数值 |",
            "|---|---:|",
            "| 交易笔数 | {} |".format(len(research.trades)),
            "| 止损卖出 | {} 笔 |".format(sum(1 for row in research.trades if row.get("reason") == "stop")),
            "| 手续费合计 | {:,.2f} 元 |".format(research.fees),
            "| 已实现盈亏 | {:+,.2f} 元 |".format(research.realized_pnl),
            "| 浮动盈亏 | {:+,.2f} 元 |".format(research.unrealized_pnl),
            "| 期末资产 | {:,.2f} 元 |".format(research.equity),
            "| 累计收益 | {:+.2f}% |".format(research.total_return * 100),
            "| 最大回撤 | {:.2f}% |".format(research.maximum_drawdown * 100),
            "",
            "### 最近 20 笔",
            "",
        ]
    )
    lines.extend(trades_table(research, limit=20))
    lines.extend(
        [
            "",
            "完整明细：`output/trades_research_20261113.csv`",
            "",
            "## 说明",
            "",
            "- 成交价采用回测的实际成交价；信号日与成交日可能相差一个交易日。",
            "- 已实现盈亏按移动加权平均成本计算，包含卖出费用；买入费用计入成本。",
            "- 未考虑分红税、滑点、涨跌停无法成交等实盘因素。",
            "- 本表为量化回测记录，不构成投资建议。",
            "",
        ]
    )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(
        {
            "strategy": {
                "trades": len(strategy.trades),
                "stop_trades": sum(1 for row in strategy.trades if row.get("reason") == "stop"),
                "fees": round(strategy.fees, 2),
                "realized": round(strategy.realized_pnl, 2),
                "unrealized": round(strategy.unrealized_pnl, 2),
                "equity": round(strategy.equity, 2),
                "return": round(strategy.total_return * 100, 2),
            },
            "research": {
                "trades": len(research.trades),
                "stop_trades": sum(1 for row in research.trades if row.get("reason") == "stop"),
                "fees": round(research.fees, 2),
                "realized": round(research.realized_pnl, 2),
                "unrealized": round(research.unrealized_pnl, 2),
                "equity": round(research.equity, 2),
                "return": round(research.total_return * 100, 2),
            },
            "md": str(OUT_MD),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
