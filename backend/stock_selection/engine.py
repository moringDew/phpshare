from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class Bar:
    trade_date: date
    symbol: str
    close: float
    volume: float
    adj_factor: float = 1.0
    amount: Optional[float] = None
    is_st: bool = False
    suspended: bool = False
    listing_days: Optional[int] = None
    roe: Optional[float] = None
    revenue_growth: Optional[float] = None
    debt_ratio: Optional[float] = None


@dataclass
class SelectionConfig:
    allowed_boards: Tuple[str, ...] = ("main", "chinext")
    top_n: int = 10
    rebalance_days: int = 20
    minimum_history: int = 121
    minimum_listing_days: int = 120
    minimum_close: float = 2.0
    minimum_average_turnover: float = 20_000_000.0
    transaction_cost_rate: float = 0.0015
    momentum_windows: Tuple[int, int, int] = (20, 60, 120)
    volatility_window: int = 60
    drawdown_window: int = 60
    minimum_momentum_120: float = 0.0
    maximum_annualized_volatility: float = 0.65
    maximum_current_drawdown: float = 0.25
    market_breadth_lookback: int = 60
    # 流动性候选池：每交易日从全市场按近 universe_lookback 日平均成交额取前 universe_size
    universe_size: int = 500
    universe_lookback: int = 20
    minimum_market_breadth: float = 0.50
    maximum_equity_exposure: float = 0.45
    initial_capital: float = 100_000.0
    cash_yield_annual: float = 0.0
    strategy_start: Optional[str] = None
    stop_loss_pct: Optional[float] = None
    skip_limit_up_buys: bool = True
    skip_limit_down_sells: bool = True
    cash_reentry_breadth: Optional[float] = None
    cash_deploy_breadth: Optional[float] = None
    lot_size: int = 100
    maximum_single_position: float = 0.06
    weights: Dict[str, float] = field(
        default_factory=lambda: {
            "momentum_20": 0.18,
            "momentum_60": 0.24,
            "momentum_120": 0.12,
            "trend": 0.10,
            "low_volatility": 0.14,
            "low_drawdown": 0.10,
            "liquidity": 0.04,
            "roe": 0.04,
            "revenue_growth": 0.03,
            "low_debt": 0.01,
        }
    )
    minimum_annual_return: float = 0.12
    maximum_drawdown: float = 0.15
    minimum_period_win_rate: float = 0.55

    @classmethod
    def from_json(cls, path: Path) -> "SelectionConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "momentum_windows" in raw:
            raw["momentum_windows"] = tuple(raw["momentum_windows"])
        if "allowed_boards" in raw:
            raw["allowed_boards"] = tuple(raw["allowed_boards"])
        return cls(**raw)


@dataclass(frozen=True)
class Candidate:
    symbol: str
    score: float
    factors: Mapping[str, float]


@dataclass
class BacktestResult:
    start_date: str
    end_date: str
    observations: int
    final_equity: float
    total_return: float
    annual_return: float
    maximum_drawdown: float
    sharpe_ratio: float
    period_win_rate: float
    rebalance_periods: int
    average_turnover: float
    passed: bool
    checks: Dict[str, bool]
    latest_selection: List[Dict[str, object]]
    equity_curve: List[Dict[str, object]]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _optional_float(value: Optional[str]) -> Optional[float]:
    if value is None or value.strip() == "":
        return None
    return float(value)


def _bool(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def load_bars(path: Path) -> List[Bar]:
    required = {"date", "symbol", "close", "volume"}
    bars: List[Bar] = []
    seen = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError("CSV 缺少字段: " + ", ".join(sorted(missing)))
        for line_number, row in enumerate(reader, start=2):
            try:
                bar = Bar(
                    trade_date=date.fromisoformat(row["date"]),
                    symbol=row["symbol"].strip().upper(),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    adj_factor=float(row.get("adj_factor") or 1.0),
                    amount=_optional_float(row.get("amount")),
                    is_st=_bool(row.get("is_st")),
                    suspended=_bool(row.get("suspended")),
                    listing_days=int(row["listing_days"]) if row.get("listing_days") else None,
                    roe=_optional_float(row.get("roe")),
                    revenue_growth=_optional_float(row.get("revenue_growth")),
                    debt_ratio=_optional_float(row.get("debt_ratio")),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("CSV 第 {} 行数据无效: {}".format(line_number, exc)) from exc
            if not bar.symbol or bar.close <= 0 or bar.volume < 0 or bar.adj_factor <= 0:
                raise ValueError("CSV 第 {} 行代码、价格或成交量无效".format(line_number))
            key = (bar.trade_date, bar.symbol)
            if key in seen:
                raise ValueError("存在重复行情: {} {}".format(*key))
            seen.add(key)
            bars.append(bar)
    if not bars:
        raise ValueError("CSV 没有行情记录")
    return sorted(bars, key=lambda item: (item.trade_date, item.symbol))


def _percentile_ranks(values: Mapping[str, float], higher_is_better: bool = True) -> Dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    if count == 1:
        return {ordered[0][0]: 1.0}
    result = {symbol: index / (count - 1) for index, (symbol, _) in enumerate(ordered)}
    if not higher_is_better:
        result = {symbol: 1.0 - rank for symbol, rank in result.items()}
    return result


def infer_board(symbol: str) -> Optional[str]:
    """Classify supported A-share symbols without accepting STAR/BSE/B shares."""
    normalized = symbol.upper()
    code, dot, exchange = normalized.partition(".")
    if not dot or not code.isdigit():
        return None
    if exchange == "SH" and code.startswith(("600", "601", "603", "605")):
        return "main"
    if exchange == "SZ" and code.startswith(("000", "001", "002", "003")):
        return "main"
    if exchange == "SZ" and code.startswith(("300", "301")):
        return "chinext"
    return None


def limit_up_rate(symbol: str, is_st: bool = False) -> Optional[float]:
    """Return the daily price-limit ratio; None for unsupported boards."""
    board = infer_board(symbol)
    if board == "main":
        return 0.05 if is_st else 0.10
    if board == "chinext":
        return 0.20
    return None


class StockSelector:
    def __init__(self, bars: Sequence[Bar], config: Optional[SelectionConfig] = None,
                 allowed_by_date: Optional[Mapping[date, Set[str]]] = None):
        self.config = config or SelectionConfig()
        self.allowed_by_date = allowed_by_date
        self.by_symbol: Dict[str, List[Bar]] = {}
        self.index: Dict[str, Dict[date, int]] = {}
        self.by_date: Dict[date, Dict[str, Bar]] = {}
        for bar in bars:
            self.by_symbol.setdefault(bar.symbol, []).append(bar)
            self.by_date.setdefault(bar.trade_date, {})[bar.symbol] = bar
        for symbol, items in self.by_symbol.items():
            items.sort(key=lambda item: item.trade_date)
            self.index[symbol] = {bar.trade_date: i for i, bar in enumerate(items)}
        self.calendar = sorted(self.by_date)

    def _is_allowed(self, symbol: str, on_date: date) -> bool:
        if self.allowed_by_date is None:
            return True
        allowed = self.allowed_by_date.get(on_date)
        return allowed is None or symbol in allowed

    def in_universe(self, symbol: str, on_date: date) -> bool:
        """该股票当天是否属于流动性候选池；未配置候选池时全部算在内。"""
        return self._is_allowed(symbol, on_date)

    def daily_change(self, symbol: str, on_date: date) -> Optional[float]:
        """Adjusted close-to-close change, using the previous available bar."""
        idx = self.index.get(symbol, {}).get(on_date)
        if idx is None or idx < 1:
            return None
        items = self.by_symbol[symbol]
        current, previous = items[idx], items[idx - 1]
        previous_price = previous.close * previous.adj_factor
        current_price = current.close * current.adj_factor
        if previous_price <= 0:
            return None
        return current_price / previous_price - 1.0

    def is_limit_up(self, symbol: str, on_date: date) -> bool:
        """Whether the stock closed at its limit-up price on this date."""
        idx = self.index.get(symbol, {}).get(on_date)
        if idx is None or idx < 1:
            return False
        items = self.by_symbol[symbol]
        current, previous = items[idx], items[idx - 1]
        rate = limit_up_rate(symbol, current.is_st)
        if rate is None or current.suspended:
            return False
        if current.adj_factor == previous.adj_factor:
            # Same adjustment basis: exchange limit price, rounded half-up to 0.01.
            limit_price = math.floor(previous.close * (1.0 + rate) * 100.0 + 0.5) / 100.0
            return current.close >= limit_price - 0.005
        change = self.daily_change(symbol, on_date)
        if change is None:
            return False
        # Different adjustment basis: fall back to adjusted return with a 0.3pp margin.
        return change >= rate - 0.003

    def is_limit_down(self, symbol: str, on_date: date) -> bool:
        """Whether the stock closed at its limit-down price on this date."""
        idx = self.index.get(symbol, {}).get(on_date)
        if idx is None or idx < 1:
            return False
        items = self.by_symbol[symbol]
        current, previous = items[idx], items[idx - 1]
        rate = limit_up_rate(symbol, current.is_st)
        if rate is None or current.suspended:
            return False
        if current.adj_factor == previous.adj_factor:
            limit_price = math.floor(previous.close * (1.0 - rate) * 100.0 + 0.5) / 100.0
            return current.close <= limit_price + 0.005
        change = self.daily_change(symbol, on_date)
        if change is None:
            return False
        return change <= -rate + 0.003

    def strategy_start_index(self) -> int:
        floor = self.config.minimum_history - 1
        if not self.config.strategy_start:
            return floor
        target = date.fromisoformat(self.config.strategy_start)
        for index, day in enumerate(self.calendar):
            if day >= target:
                return max(floor, index)
        return len(self.calendar) - 1

    def _raw_factors(self, symbol: str, on_date: date) -> Optional[Dict[str, float]]:
        if infer_board(symbol) not in self.config.allowed_boards:
            return None
        idx = self.index.get(symbol, {}).get(on_date)
        if idx is None or idx < self.config.minimum_history - 1:
            return None
        items = self.by_symbol[symbol]
        current = items[idx]
        if current.is_st or current.suspended or current.close < self.config.minimum_close:
            return None
        if current.listing_days is not None and current.listing_days < self.config.minimum_listing_days:
            return None
        windows = self.config.momentum_windows
        if idx < max(windows):
            return None
        turnover_window = items[max(0, idx - 19) : idx + 1]
        average_turnover = sum(
            bar.amount if bar.amount is not None else bar.close * bar.volume
            for bar in turnover_window
        ) / len(turnover_window)
        if average_turnover < self.config.minimum_average_turnover:
            return None
        closes = [bar.close * bar.adj_factor for bar in items]
        returns = [closes[i] / closes[i - 1] - 1.0 for i in range(idx - self.config.volatility_window + 1, idx + 1)]
        volatility = statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
        recent = closes[idx - self.config.drawdown_window + 1 : idx + 1]
        drawdown = 1.0 - current.close / max(recent)
        sma20 = sum(closes[idx - 19 : idx + 1]) / 20
        sma60 = sum(closes[idx - 59 : idx + 1]) / 60
        factors = {
            "momentum_20": closes[idx] / closes[idx - windows[0]] - 1.0,
            "momentum_60": closes[idx] / closes[idx - windows[1]] - 1.0,
            "momentum_120": closes[idx] / closes[idx - windows[2]] - 1.0,
            "trend": 1.0 if closes[idx] > sma20 > sma60 else 0.0,
            "low_volatility": volatility,
            "low_drawdown": drawdown,
            "liquidity": math.log1p(average_turnover),
        }
        if factors["momentum_120"] < self.config.minimum_momentum_120:
            return None
        if volatility > self.config.maximum_annualized_volatility:
            return None
        if drawdown > self.config.maximum_current_drawdown:
            return None
        if any(name in self.config.weights for name in ("rsi_14", "volume_ratio", "breakout_20")):
            gains = 0.0
            losses = 0.0
            for offset in range(idx - 13, idx + 1):
                change = closes[offset] - closes[offset - 1]
                if change >= 0:
                    gains += change
                else:
                    losses -= change
            average_gain = gains / 14.0
            average_loss = losses / 14.0
            factors["rsi_14"] = (100.0 if average_loss == 0 else
                                 100.0 - 100.0 / (1.0 + average_gain / average_loss))
            volume_5 = sum(bar.volume for bar in items[idx - 4 : idx + 1]) / 5.0
            volume_20 = sum(bar.volume for bar in items[idx - 19 : idx + 1]) / 20.0
            factors["volume_ratio"] = volume_5 / volume_20 if volume_20 else 1.0
            factors["breakout_20"] = closes[idx] / max(closes[idx - 19 : idx + 1])
        if current.roe is not None:
            factors["roe"] = current.roe
        if current.revenue_growth is not None:
            factors["revenue_growth"] = current.revenue_growth
        if current.debt_ratio is not None:
            factors["low_debt"] = current.debt_ratio
        return factors

    def select(self, on_date: date) -> List[Candidate]:
        raw: Dict[str, Dict[str, float]] = {}
        for symbol in self.by_date.get(on_date, {}):
            if not self._is_allowed(symbol, on_date):
                continue
            factors = self._raw_factors(symbol, on_date)
            if factors is not None:
                raw[symbol] = factors
        if not raw:
            return []
        ranks: Dict[str, Dict[str, float]] = {}
        all_factors = sorted({name for values in raw.values() for name in values})
        for factor in all_factors:
            values = {symbol: item[factor] for symbol, item in raw.items() if factor in item}
            lower_better = factor in {"low_volatility", "low_drawdown", "low_debt"}
            ranks[factor] = _percentile_ranks(values, higher_is_better=not lower_better)
        candidates: List[Candidate] = []
        for symbol, factors in raw.items():
            available = [(name, weight) for name, weight in self.config.weights.items() if symbol in ranks.get(name, {})]
            total_weight = sum(weight for _, weight in available)
            score = sum(ranks[name][symbol] * weight for name, weight in available) / total_weight
            candidates.append(Candidate(symbol=symbol, score=score, factors=factors))
        candidates.sort(key=lambda item: (-item.score, item.symbol))
        return candidates[: self.config.top_n]

    def market_breadth(self, on_date: date) -> float:
        above = 0
        available = 0
        lookback = self.config.market_breadth_lookback
        for symbol in self.by_date.get(on_date, {}):
            if not self._is_allowed(symbol, on_date):
                continue
            if infer_board(symbol) not in self.config.allowed_boards:
                continue
            idx = self.index.get(symbol, {}).get(on_date)
            if idx is None or idx < lookback - 1:
                continue
            current = self.by_symbol[symbol][idx]
            if current.is_st or current.suspended:
                continue
            prices = [bar.close * bar.adj_factor for bar in self.by_symbol[symbol][idx - lookback + 1 : idx + 1]]
            available += 1
            above += prices[-1] > (sum(prices) / len(prices))
        return above / available if available else 0.0

    def build_order_plan(self, on_date: date, capital: Optional[float] = None, respect_market_filter: bool = True) -> Dict[str, object]:
        available_capital = self.config.initial_capital if capital is None else capital
        if available_capital <= 0:
            raise ValueError("本金必须大于零")
        breadth = self.market_breadth(on_date)
        risk_on = breadth >= self.config.minimum_market_breadth
        if respect_market_filter and not risk_on:
            return {
                "date": on_date.isoformat(),
                "capital": available_capital,
                "risk_on": False,
                "market_breadth": breadth,
                "orders": [],
                "invested": 0.0,
                "cash": available_capital,
                "skipped_limit_up": [],
            }
        selected = self.select(on_date)
        skipped_limit_up = [
            item.symbol for item in selected
            if self.config.skip_limit_up_buys and self.is_limit_up(item.symbol, on_date)
        ]
        selected = [item for item in selected if item.symbol not in skipped_limit_up]
        if not selected:
            return {
                "date": on_date.isoformat(), "capital": available_capital, "risk_on": risk_on,
                "market_breadth": breadth, "orders": [], "invested": 0.0, "cash": available_capital,
                "skipped_limit_up": skipped_limit_up,
            }
        total_limit = available_capital * self.config.maximum_equity_exposure
        single_limit = available_capital * self.config.maximum_single_position
        target = min(total_limit / len(selected), single_limit)
        orders: List[Dict[str, object]] = []
        for candidate in selected:
            bar = self.by_date[on_date][candidate.symbol]
            lot_cost = bar.close * self.config.lot_size
            lots = int(target // lot_cost)
            orders.append({
                "symbol": candidate.symbol,
                "score": candidate.score,
                "price": bar.close,
                "shares": lots * self.config.lot_size,
                "amount": lots * lot_cost,
                "lot_cost": lot_cost,
            })
        invested = sum(float(order["amount"]) for order in orders)
        while True:
            eligible = [
                order for order in orders
                if float(order["amount"]) + float(order["lot_cost"]) <= single_limit + 1e-9
                and invested + float(order["lot_cost"]) <= total_limit + 1e-9
            ]
            if not eligible:
                break
            order = min(eligible, key=lambda item: (float(item["amount"]) / single_limit, -float(item["score"])))
            order["shares"] = int(order["shares"]) + self.config.lot_size
            order["amount"] = float(order["amount"]) + float(order["lot_cost"])
            invested += float(order["lot_cost"])
        for order in orders:
            order.pop("lot_cost", None)
            order["weight"] = float(order["amount"]) / available_capital
        return {
            "date": on_date.isoformat(),
            "capital": available_capital,
            "risk_on": risk_on,
            "market_breadth": breadth,
            "orders": orders,
            "invested": invested,
            "cash": available_capital - invested,
            "skipped_limit_up": skipped_limit_up,
        }

    def backtest(self) -> BacktestResult:
        if len(self.calendar) < self.config.minimum_history + 2:
            raise ValueError("行情长度不足，至少需要 {} 个交易日".format(self.config.minimum_history + 2))
        equity = 1.0
        curve: List[Tuple[date, float]] = []
        active: Dict[str, float] = {}
        pending: Optional[Dict[str, float]] = None
        turnovers: List[float] = []
        period_returns: List[float] = []
        period_start_equity: Optional[float] = None
        start = self.strategy_start_index()
        for offset, current_date in enumerate(self.calendar[start:], start=0):
            calendar_idx = start + offset
            if calendar_idx > 0:
                previous_date = self.calendar[calendar_idx - 1]
                portfolio_return = 0.0
                for symbol, weight in active.items():
                    current = self.by_date.get(current_date, {}).get(symbol)
                    previous = self.by_date.get(previous_date, {}).get(symbol)
                    if current is not None and previous is not None and not current.suspended:
                        current_adjusted = current.close * current.adj_factor
                        previous_adjusted = previous.close * previous.adj_factor
                        portfolio_return += weight * (current_adjusted / previous_adjusted - 1.0)
                cash_weight = max(0.0, 1.0 - sum(active.values()))
                portfolio_return += cash_weight * self.config.cash_yield_annual / 252.0
                equity *= 1.0 + portfolio_return
            if pending is not None:
                execution = dict(pending)
                if self.config.skip_limit_up_buys:
                    for symbol in set(execution) | set(active):
                        if (execution.get(symbol, 0.0) > active.get(symbol, 0.0)
                                and self.is_limit_up(symbol, current_date)):
                            execution[symbol] = active.get(symbol, 0.0)
                    execution = {symbol: weight for symbol, weight in execution.items() if weight > 0}
                equity_change = sum(abs(execution.get(s, 0.0) - active.get(s, 0.0)) for s in set(execution) | set(active))
                cash_change = abs((1.0 - sum(execution.values())) - (1.0 - sum(active.values())))
                turnover = 0.5 * (equity_change + cash_change)
                equity *= max(0.0, 1.0 - turnover * self.config.transaction_cost_rate)
                turnovers.append(turnover)
                if period_start_equity is not None and active:
                    period_returns.append(equity / period_start_equity - 1.0)
                period_start_equity = equity
                active = execution
                pending = None
            if offset % self.config.rebalance_days == 0:
                selected = self.select(current_date) if self.market_breadth(current_date) >= self.config.minimum_market_breadth else []
                if selected and self.config.skip_limit_up_buys:
                    selected = [
                        item for item in selected
                        if item.symbol in active or not self.is_limit_up(item.symbol, current_date)
                    ]
                if selected:
                    weight = min(
                        self.config.maximum_equity_exposure / len(selected),
                        self.config.maximum_single_position,
                    )
                    pending = {item.symbol: weight for item in selected}
                else:
                    pending = {}
            curve.append((current_date, equity))
        if period_start_equity is not None and active:
            period_returns.append(equity / period_start_equity - 1.0)
        daily_returns = [curve[i][1] / curve[i - 1][1] - 1.0 for i in range(1, len(curve))]
        peak = 0.0
        max_drawdown = 0.0
        for _, value in curve:
            peak = max(peak, value)
            if peak:
                max_drawdown = max(max_drawdown, 1.0 - value / peak)
        years = max(len(daily_returns) / 252.0, 1.0 / 252.0)
        annual_return = equity ** (1.0 / years) - 1.0
        daily_std = statistics.pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
        sharpe = statistics.mean(daily_returns) / daily_std * math.sqrt(252) if daily_std else 0.0
        win_rate = sum(value > 0 for value in period_returns) / len(period_returns) if period_returns else 0.0
        checks = {
            "annual_return": annual_return >= self.config.minimum_annual_return,
            "maximum_drawdown": max_drawdown <= self.config.maximum_drawdown,
            "period_win_rate": win_rate >= self.config.minimum_period_win_rate,
        }
        latest = self.select(self.calendar[-1])
        return BacktestResult(
            start_date=curve[0][0].isoformat(),
            end_date=curve[-1][0].isoformat(),
            observations=len(curve),
            final_equity=equity,
            total_return=equity - 1.0,
            annual_return=annual_return,
            maximum_drawdown=max_drawdown,
            sharpe_ratio=sharpe,
            period_win_rate=win_rate,
            rebalance_periods=len(period_returns),
            average_turnover=statistics.mean(turnovers) if turnovers else 0.0,
            passed=all(checks.values()),
            checks=checks,
            latest_selection=[{"symbol": item.symbol, "score": item.score, "factors": dict(item.factors)} for item in latest],
            equity_curve=[{"date": day.isoformat(), "equity": value} for day, value in curve],
        )

