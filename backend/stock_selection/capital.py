from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
    pending_symbols: Optional[List[str]] = None
    pending_stop_symbols: List[str] = field(default_factory=list)
    average_cost: Dict[str, float] = field(default_factory=dict)
    pending_limit_up_symbols: List[str] = field(default_factory=list)
    limit_up_skips: List[Dict[str, object]] = field(default_factory=list)
    limit_down_skips: List[Dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def target_shares(selector: StockSelector, on_date: date, symbols: Sequence[str], total_equity: float) -> Dict[str, int]:
    config = selector.config
    bars = selector.by_date.get(on_date, {})
    symbols = [symbol for symbol in symbols if symbol in bars and not bars[symbol].suspended]
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
    last_close: Dict[str, float] = {}
    stop_pending: Set[str] = set()
    pending_symbols: Optional[List[str]] = None
    pending_signal: Optional[date] = None
    pending_topup: Dict[str, int] = {}
    last_target_symbols: List[str] = []
    pending_limit_up_symbols: List[str] = []
    limit_up_skips: List[Dict[str, object]] = []
    stop_since_rebalance = False
    stop_blocked: Set[str] = set()
    skip_limit_up = getattr(config, "skip_limit_up_buys", True)
    skip_limit_down = getattr(config, "skip_limit_down_sells", True)
    is_limit_down = getattr(selector, "is_limit_down", None)
    cash_reentry_breadth = getattr(config, "cash_reentry_breadth", None)
    cash_deploy_breadth = getattr(config, "cash_deploy_breadth", None)
    limit_down_skips: List[Dict[str, object]] = []
    is_limit_up = getattr(selector, "is_limit_up", None)
    trades: List[Dict[str, object]] = []
    curve: List[Dict[str, object]] = []
    invested_days = 0

    def blocked_by_limit_up(symbol: str, on_date: date) -> bool:
        return bool(skip_limit_up and is_limit_up is not None and is_limit_up(symbol, on_date))

    def position_value(on_date: date) -> float:
        bars = selector.by_date[on_date]
        return sum(
            quantity * (bars[symbol].close if symbol in bars else last_close[symbol])
            for symbol, quantity in shares.items()
        )

    def equity(on_date: date) -> float:
        return cash + position_value(on_date)

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
                if bar is None or bar.suspended:
                    continue
                if skip_limit_down and is_limit_down is not None and is_limit_down(symbol, current_date):
                    limit_down_skips.append({
                        "date": current_date.isoformat(), "symbol": symbol,
                        "shares": shares[symbol], "price": bar.close,
                        "reason": "stop_limit_down",
                    })
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
                stop_blocked.add(symbol)
            stop_pending -= stopped_today
            if stopped_today:
                stop_since_rebalance = True
        if pending_symbols is not None:
            target = target_shares(selector, current_date, pending_symbols, equity(current_date))
            for symbol in sorted(set(shares) | set(target)):
                old = shares.get(symbol, 0)
                new = target.get(symbol, 0)
                if new >= old:
                    continue
                bar = selector.by_date.get(current_date, {}).get(symbol)
                if bar is None or bar.suspended:
                    continue
                if skip_limit_down and is_limit_down is not None and is_limit_down(symbol, current_date):
                    limit_down_skips.append({
                        "date": current_date.isoformat(), "symbol": symbol,
                        "shares": old - new, "price": bar.close,
                        "reason": "rebalance_limit_down",
                    })
                    continue
                quantity = old - new
                price = bar.close
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
                if blocked_by_limit_up(symbol, current_date):
                    limit_up_skips.append({
                        "date": current_date.isoformat(),
                        "signal_date": pending_signal.isoformat(),
                        "symbol": symbol,
                        "shares": quantity,
                        "price": selector.by_date[current_date][symbol].close,
                        "reason": "limit_up",
                    })
                    continue
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
            pending_limit_up_symbols = []
        if pending_topup:
            exposure_limit = equity(current_date) * config.maximum_equity_exposure
            current_exposure = position_value(current_date)
            for symbol, quantity in sorted(pending_topup.items()):
                if symbol in stop_blocked:
                    continue
                bar = selector.by_date.get(current_date, {}).get(symbol)
                if bar is None or bar.suspended or blocked_by_limit_up(symbol, current_date):
                    continue
                price = bar.close
                affordable = int(cash // (price * config.lot_size)) * config.lot_size
                qty = min(int(quantity), affordable)
                if qty <= 0:
                    continue
                gross = qty * price
                fee = gross * config.transaction_cost_rate
                if gross + fee > cash + 1e-9:
                    continue
                if current_exposure + gross > exposure_limit + 1e-9:
                    continue
                cash -= gross + fee
                current_exposure += gross
                old = shares.get(symbol, 0)
                old_cost = average_cost.get(symbol, 0.0) * old
                shares[symbol] = old + qty
                average_cost[symbol] = (old_cost + gross) / (old + qty)
                trades.append({
                    "date": current_date.isoformat(),
                    "signal_date": current_date.isoformat(),
                    "side": "BUY", "symbol": symbol, "shares": qty,
                    "price": price, "fee": fee, "reason": "cash_deploy",
                })
            pending_topup = {}
        if index % config.rebalance_days == 0:
            risk_on = selector.market_breadth(current_date) >= config.minimum_market_breadth
            picks = selector.select(current_date) if risk_on else []
            blocked = [
                item.symbol for item in picks
                if item.symbol not in shares and blocked_by_limit_up(item.symbol, current_date)
            ]
            pending_limit_up_symbols = blocked
            pending_symbols = [item.symbol for item in picks if item.symbol not in blocked]
            last_target_symbols = [item.symbol for item in picks]
            pending_signal = current_date
            stop_since_rebalance = False
            stop_blocked.clear()
        elif config.stop_loss_pct and shares:
            for symbol in list(shares):
                bar = selector.by_date.get(current_date, {}).get(symbol)
                if bar is None:
                    continue
                cost = average_cost.get(symbol, bar.close)
                if bar.close / cost - 1.0 <= -config.stop_loss_pct:
                    stop_pending.add(symbol)
        elif (cash_reentry_breadth is not None and not shares
              and pending_symbols is None and not stop_pending and not stop_since_rebalance):
            if selector.market_breadth(current_date) >= cash_reentry_breadth:
                picks = selector.select(current_date)
                if picks:
                    blocked = [
                        item.symbol for item in picks
                        if blocked_by_limit_up(item.symbol, current_date)
                    ]
                    pending_limit_up_symbols = blocked
                    pending_symbols = [item.symbol for item in picks if item.symbol not in blocked]
                    pending_signal = current_date
        elif (cash_deploy_breadth is not None and cash > 0 and pending_symbols is None
              and not stop_pending):
            current_exposure = position_value(current_date)
            target_exposure = equity(current_date) * config.maximum_equity_exposure
            if (current_exposure < target_exposure * 0.95
                    and last_target_symbols
                    and selector.market_breadth(current_date) >= cash_deploy_breadth):
                target = target_shares(selector, current_date, last_target_symbols, equity(current_date))
                buys = {}
                for symbol, target_quantity in target.items():
                    if symbol in stop_blocked:
                        continue
                    old = shares.get(symbol, 0)
                    if target_quantity > old:
                        buys[symbol] = target_quantity - old
                if buys:
                    pending_topup = buys
        if shares:
            invested_days += 1
        last_close = {
            symbol: selector.by_date[current_date][symbol].close
            if symbol in selector.by_date[current_date] else last_close[symbol]
            for symbol in shares
        }
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
        pending_symbols=pending_symbols,
        pending_stop_symbols=sorted(stop_pending),
        average_cost=dict(average_cost),
        pending_limit_up_symbols=pending_limit_up_symbols,
        limit_up_skips=limit_up_skips,
        limit_down_skips=limit_down_skips,
    )

