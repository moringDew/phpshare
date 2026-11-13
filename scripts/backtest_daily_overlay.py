from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from stock_selection.engine import SelectionConfig, StockSelector, load_bars  # noqa: E402

STORE = ROOT / "data" / "a_share_public_latest.csv"
CONFIG = ROOT / "configs" / "stock_selection_optimized.json"
START = date(2024, 2, 8)
END = date(2026, 11, 13)


@dataclass
class Result:
    label: str
    total_return: float
    annual_return: float
    maximum_drawdown: float
    trades: int
    fees: float
    final_equity: float


def target_quantities(selector: StockSelector, config: SelectionConfig, day: date,
                      picks: List[str], equity: float) -> Dict[str, int]:
    if not picks:
        return {}
    total_limit = equity * config.maximum_equity_exposure
    single_limit = equity * config.maximum_single_position
    equal_target = min(total_limit / len(picks), single_limit)
    quantities: Dict[str, int] = {}
    amounts: Dict[str, float] = {}
    for symbol in picks:
        price = selector.by_date[day][symbol].close
        quantity = int(equal_target // (price * config.lot_size)) * config.lot_size
        quantities[symbol] = quantity
        amounts[symbol] = quantity * price
    invested = sum(amounts.values())
    while True:
        eligible = []
        for rank, symbol in enumerate(picks):
            price = selector.by_date[day][symbol].close
            lot_cost = price * config.lot_size
            if amounts[symbol] + lot_cost <= single_limit + 1e-9 and invested + lot_cost <= total_limit + 1e-9:
                eligible.append((amounts[symbol] / single_limit, rank, symbol, lot_cost))
        if not eligible:
            break
        _, _, symbol, lot_cost = min(eligible)
        quantities[symbol] += config.lot_size
        amounts[symbol] += lot_cost
        invested += lot_cost
    return {symbol: quantity for symbol, quantity in quantities.items() if quantity > 0}


def simulate(selector: StockSelector, config: SelectionConfig, calendar: List[date],
             breadth: Dict[date, float], picks_by_day: Dict[date, List[str]],
             breadth_exit: bool, stop_loss: Optional[float], label: str) -> Result:
    cash = float(config.initial_capital)
    shares: Dict[str, int] = {}
    average_cost: Dict[str, float] = {}
    pending: Optional[Dict[str, int]] = None
    trades: List[Dict[str, object]] = []
    curve: List[Tuple[date, float]] = []
    daily_rate = config.cash_yield_annual / 252.0

    for index, day in enumerate(calendar):
        if index > 0:
            cash *= 1.0 + daily_rate

        if pending is not None:
            target = pending
            for symbol in sorted(set(shares) | set(target)):
                old = shares.get(symbol, 0)
                new = target.get(symbol, 0)
                if new >= old:
                    continue
                quantity = old - new
                bar = selector.by_date.get(day, {}).get(symbol)
                if bar is None:
                    continue
                price = bar.close
                gross = quantity * price
                fee = gross * config.transaction_cost_rate
                cash += gross - fee
                trades.append({"date": day, "side": "SELL", "symbol": symbol, "shares": quantity,
                               "price": price, "fee": fee, "reason": "rebalance"})
                if new:
                    shares[symbol] = new
                else:
                    shares.pop(symbol, None)
                    average_cost.pop(symbol, None)
            for symbol in sorted(target):
                old = shares.get(symbol, 0)
                new = target[symbol]
                if new <= old:
                    continue
                quantity = new - old
                bar = selector.by_date.get(day, {}).get(symbol)
                if bar is None:
                    continue
                price = bar.close
                gross = quantity * price
                fee = gross * config.transaction_cost_rate
                if gross + fee <= cash + 1e-9:
                    cash -= gross + fee
                    old_cost = average_cost.get(symbol, 0.0) * old
                    shares[symbol] = new
                    average_cost[symbol] = (old_cost + gross) / new
                    trades.append({"date": day, "side": "BUY", "symbol": symbol, "shares": quantity,
                                   "price": price, "fee": fee, "reason": "rebalance"})
            pending = None

        if index % config.rebalance_days == 0:
            if breadth.get(day, 0.0) >= config.minimum_market_breadth:
                pending = target_quantities(
                    selector, config, day, picks_by_day.get(day, []),
                    cash + sum(quantity * selector.by_date[day][symbol].close
                               for symbol, quantity in shares.items() if symbol in selector.by_date[day]),
                )
            else:
                pending = {}
        elif pending is None:
            target: Optional[Dict[str, int]] = None
            if breadth_exit and breadth.get(day, 0.0) < config.minimum_market_breadth and shares:
                target = {}
            elif stop_loss is not None and shares:
                kept = dict(shares)
                for symbol, quantity in shares.items():
                    bar = selector.by_date.get(day, {}).get(symbol)
                    if bar is None:
                        continue
                    price = bar.close
                    cost = average_cost.get(symbol, price)
                    if price / cost - 1.0 <= -stop_loss:
                        kept.pop(symbol, None)
                if len(kept) != len(shares):
                    target = kept
            if target is not None:
                pending = target

        equity = cash + sum(
            quantity * selector.by_date.get(day, {})[symbol].close
            for symbol, quantity in shares.items()
            if symbol in selector.by_date.get(day, {})
        )
        curve.append((day, equity))

    peak = float(config.initial_capital)
    maximum_drawdown = 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, 1.0 - equity / peak)
    final_equity = curve[-1][1]
    years = max(len(curve) / 252.0, 1.0 / 252.0)
    annual = final_equity / config.initial_capital
    annual = annual ** (1.0 / years) - 1.0 if annual > 0 else -1.0
    return Result(
        label=label,
        total_return=final_equity / config.initial_capital - 1.0,
        annual_return=annual,
        maximum_drawdown=maximum_drawdown,
        trades=len(trades),
        fees=sum(float(trade["fee"]) for trade in trades),
        final_equity=final_equity,
    )


def main() -> int:
    config = SelectionConfig.from_json(CONFIG)
    config.strategy_start = None
    selector = StockSelector(load_bars(STORE), config)
    calendar = [day for day in selector.calendar if START <= day <= END]
    breadth = {day: selector.market_breadth(day) for day in calendar}
    picks_by_day = {
        day: [candidate.symbol for candidate in selector.select(day)]
        for index, day in enumerate(calendar)
        if index % config.rebalance_days == 0 and breadth[day] >= config.minimum_market_breadth
    }

    variants = [
        ("baseline", False, None),
        ("breadth", True, None),
        ("stop8", False, 0.08),
        ("stop10", False, 0.10),
        ("stop15", False, 0.15),
        ("breadth+stop10", True, 0.10),
    ]
    results = []
    for label, breadth_exit, stop_loss in variants:
        result = simulate(selector, config, calendar, breadth, picks_by_day, breadth_exit, stop_loss, label)
        results.append(result)
        print("{:<16} total {:>7.2f}% annual {:>6.2f}% mdd {:>6.2f}% trades {:>3d} fees {:>8.0f} final {:>10.2f}".format(
            label, result.total_return * 100, result.annual_return * 100,
            result.maximum_drawdown * 100, result.trades, result.fees, result.final_equity), flush=True)

    output = {
        "start": START.isoformat(),
        "end": END.isoformat(),
        "config": {
            "top_n": config.top_n,
            "exposure": config.maximum_equity_exposure,
            "single": config.maximum_single_position,
            "breadth": config.minimum_market_breadth,
            "cash_yield": config.cash_yield_annual,
            "rebalance_days": config.rebalance_days,
        },
        "results": [
            {
                "label": item.label,
                "total_return_pct": round(item.total_return * 100, 2),
                "annual_return_pct": round(item.annual_return * 100, 2),
                "maximum_drawdown_pct": round(item.maximum_drawdown * 100, 2),
                "trades": item.trades,
                "fees": round(item.fees, 2),
                "final_equity": round(item.final_equity, 2),
            }
            for item in results
        ],
    }
    path = ROOT / "output" / "daily_overlay_backtest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote {}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
