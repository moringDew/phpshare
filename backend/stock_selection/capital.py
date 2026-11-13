from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence, Set

from .engine import StockSelector


@dataclass
class CapitalBacktestResult:
    start_date: str
    end_date: str
    initial_capital: float
    final_equity: float
    profit: float
    total_return: float
    maximum_drawdown: float
    fees: float
    trade_count: int
    invested_days: int
    final_cash: float
    final_positions: Dict[str, int]
    equity_curve: List[Dict[str, object]]
    trades: List[Dict[str, object]]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def run_capital_backtest(
    selector: StockSelector,
    start_date: date,
    end_date: Optional[date] = None,
    initial_capital: Optional[float] = None,
) -> CapitalBacktestResult:
    config = selector.config
    capital = config.initial_capital if initial_capital is None else initial_capital
    calendar = [day for day in selector.calendar if day >= start_date and (end_date is None or day <= end_date)]
    if not calendar:
        raise ValueError("指定区间没有行情")
    cash = float(capital)
    shares: Dict[str, int] = {}
    average_cost: Dict[str, float] = {}
    stop_pending: Set[str] = set()
    pending_symbols: Optional[List[str]] = None
    pending_signal: Optional[date] = None
    trades: List[Dict[str, object]] = []
    curve: List[Dict[str, object]] = []
    invested_days = 0

    def equity(on_date: date) -> float:
        return cash + sum(
            quantity * selector.by_date[on_date][symbol].close
            for symbol, quantity in shares.items()
            if symbol in selector.by_date[on_date]
        )

    def targets(on_date: date, symbols: Sequence[str], total_equity: float) -> Dict[str, int]:
        if not symbols:
            return {}
        total_limit = total_equity * config.maximum_equity_exposure
        single_limit = total_equity * config.maximum_single_position
        equal_target = min(total_limit / len(symbols), single_limit)
        quantities: Dict[str, int] = {}
        amounts: Dict[str, float] = {}
        for symbol in symbols:
            price = selector.by_date[on_date][symbol].close
            quantity = int(equal_target // (price * config.lot_size)) * config.lot_size
            quantities[symbol] = quantity
            amounts[symbol] = quantity * price
        invested = sum(amounts.values())
        while True:
            eligible = []
            for rank, symbol in enumerate(symbols):
                price = selector.by_date[on_date][symbol].close
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

    daily_cash_rate = config.cash_yield_annual / 252.0
    for index, current_date in enumerate(calendar):
        if index > 0:
            cash *= 1.0 + daily_cash_rate
        stopped_today: Set[str] = set()
        if stop_pending:
            for symbol in sorted(stop_pending):
                if symbol not in shares:
                    stop_pending.discard(symbol)
                    continue
                bar = selector.by_date.get(current_date, {}).get(symbol)
                if bar is None:
                    continue
                quantity = shares[symbol]
                price = bar.close
                gross = quantity * price
                fee = gross * config.transaction_cost_rate
                cash += gross - fee
                trades.append({
                    "date": current_date.isoformat(),
                    "signal_date": calendar[index - 1].isoformat() if index > 0 else current_date.isoformat(),
                    "side": "SELL", "symbol": symbol, "shares": quantity,
                    "price": price, "fee": fee, "reason": "stop",
                })
                shares.pop(symbol, None)
                average_cost.pop(symbol, None)
                stopped_today.add(symbol)
            stop_pending -= stopped_today
        if pending_symbols is not None:
            target = targets(current_date, pending_symbols, equity(current_date))
            for symbol in sorted(set(shares) | set(target)):
                old = shares.get(symbol, 0)
                new = target.get(symbol, 0)
                if new >= old:
                    continue
                quantity = old - new
                price = selector.by_date[current_date][symbol].close
                gross = quantity * price
                fee = gross * config.transaction_cost_rate
                cash += gross - fee
                trades.append({
                    "date": current_date.isoformat(),
                    "signal_date": pending_signal.isoformat(),
                    "side": "SELL", "symbol": symbol, "shares": quantity,
                    "price": price, "fee": fee, "reason": "rebalance",
                })
                if new:
                    shares[symbol] = new
                else:
                    shares.pop(symbol, None)
                    average_cost.pop(symbol, None)
            for symbol in sorted(target):
                if symbol in stopped_today:
                    continue
                old = shares.get(symbol, 0)
                new = target[symbol]
                if new <= old:
                    continue
                quantity = new - old
                price = selector.by_date[current_date][symbol].close
                gross = quantity * price
                fee = gross * config.transaction_cost_rate
                if gross + fee <= cash + 1e-9:
                    cash -= gross + fee
                    old_cost = average_cost.get(symbol, 0.0) * old
                    shares[symbol] = new
                    average_cost[symbol] = (old_cost + gross) / new
                    trades.append({
                        "date": current_date.isoformat(),
                        "signal_date": pending_signal.isoformat(),
                        "side": "BUY", "symbol": symbol, "shares": quantity,
                        "price": price, "fee": fee, "reason": "rebalance",
                    })
            pending_symbols = None
        if index % config.rebalance_days == 0:
            risk_on = selector.market_breadth(current_date) >= config.minimum_market_breadth
            picks = selector.select(current_date) if risk_on else []
            pending_symbols = [item.symbol for item in picks]
            pending_signal = current_date
        elif config.stop_loss_pct and shares:
            for symbol in list(shares):
                bar = selector.by_date.get(current_date, {}).get(symbol)
                if bar is None:
                    continue
                cost = average_cost.get(symbol, bar.close)
                if bar.close / cost - 1.0 <= -config.stop_loss_pct:
                    stop_pending.add(symbol)
        if shares:
            invested_days += 1
        curve.append({"date": current_date.isoformat(), "equity": equity(current_date), "cash": cash, "positions": len(shares)})

    peak = capital
    maximum_drawdown = 0.0
    for row in curve:
        peak = max(peak, float(row["equity"]))
        maximum_drawdown = max(maximum_drawdown, 1.0 - float(row["equity"]) / peak)
    final_equity = float(curve[-1]["equity"])
    return CapitalBacktestResult(
        start_date=calendar[0].isoformat(),
        end_date=calendar[-1].isoformat(),
        initial_capital=capital,
        final_equity=final_equity,
        profit=final_equity - capital,
        total_return=final_equity / capital - 1.0,
        maximum_drawdown=maximum_drawdown,
        fees=sum(float(trade["fee"]) for trade in trades),
        trade_count=len(trades),
        invested_days=invested_days,
        final_cash=cash,
        final_positions=dict(shares),
        equity_curve=curve,
        trades=trades,
    )

