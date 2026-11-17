from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .engine import Bar, SelectionConfig, StockSelector, infer_board

DEFAULT_TOP_N = 500
DEFAULT_LOOKBACK = 20
DEFAULT_MIN_PRICE = 2.0


def average_amount(bars: Sequence[Bar], lookback: int) -> Optional[float]:
    """近 lookback 个交易日平均成交额；历史不足返回 None。"""
    window = bars[-lookback:]
    if len(window) < lookback:
        return None
    return sum(
        bar.amount if bar.amount is not None else bar.close * bar.volume
        for bar in window
    ) / len(window)


def select_liquidity_pool(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    day: date,
    top_n: int = DEFAULT_TOP_N,
    lookback: int = DEFAULT_LOOKBACK,
    allowed_boards: Iterable[str] = ("main", "chinext"),
    min_price: float = DEFAULT_MIN_PRICE,
) -> List[str]:
    """按截至 day 的近 lookback 日平均成交额，从全市场选出前 top_n 只。

    过滤规则与选股引擎一致：只看主板/创业板，剔除 ST、停牌、价格低于
    min_price 和当天没有行情的标的。当天无行情说明停牌或数据缺失，
    不能让它进入候选池，否则下一步选股会拿到旧价格。
    """
    boards = set(allowed_boards)
    scored: Dict[str, float] = {}
    for symbol, items in bars_by_symbol.items():
        if infer_board(symbol) not in boards:
            continue
        history = [bar for bar in items if bar.trade_date <= day][-lookback:]
        if len(history) < lookback:
            continue
        last = history[-1]
        if last.trade_date != day or last.is_st or last.suspended or last.close < min_price:
            continue
        average = average_amount(history, lookback)
        if average is None or average <= 0:
            continue
        scored[symbol] = average
    return [symbol for symbol, _ in sorted(scored.items(), key=lambda item: (-item[1], item[0]))[:top_n]]


def build_pool_map(
    bars: Sequence[Bar],
    top_n: int = DEFAULT_TOP_N,
    lookback: int = DEFAULT_LOOKBACK,
    allowed_boards: Iterable[str] = ("main", "chinext"),
    min_price: float = DEFAULT_MIN_PRICE,
    start: Optional[date] = None,
) -> Dict[date, Set[str]]:
    """为每个交易日生成当天的流动性候选池。

    线上和回测共用同一口径：每天按当时的成交额重新筛前 top_n，
    避免用今天的名单回看历史造成前视偏差。
    """
    by_symbol: Dict[str, List[Bar]] = {}
    for bar in bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)
    for items in by_symbol.values():
        items.sort(key=lambda item: item.trade_date)
    dates_by_symbol: Dict[str, List[date]] = {
        symbol: [bar.trade_date for bar in items] for symbol, items in by_symbol.items()
    }

    calendar = sorted({bar.trade_date for bar in bars})
    pool_map: Dict[date, Set[str]] = {}
    for day in calendar:
        if start is not None and day < start:
            continue
        pool_map[day] = set(
            _select_fast(
                by_symbol, dates_by_symbol, day, top_n, lookback,
                set(allowed_boards), min_price,
            )
        )
    return pool_map


def _select_fast(
    by_symbol: Mapping[str, Sequence[Bar]],
    dates_by_symbol: Mapping[str, Sequence[date]],
    day: date,
    top_n: int,
    lookback: int,
    boards: Set[str],
    min_price: float,
) -> List[str]:
    """与 select_liquidity_pool 同口径，用二分定位窗口，供逐日批量调用。"""
    scored: Dict[str, float] = {}
    for symbol, items in by_symbol.items():
        if infer_board(symbol) not in boards:
            continue
        dates = dates_by_symbol[symbol]
        # bisect_right 返回 day 之后的第一条，减一得到当天的索引
        index = _bisect_right(dates, day) - 1
        if index < lookback - 1 or dates[index] != day:
            continue
        last = items[index]
        if last.is_st or last.suspended or last.close < min_price:
            continue
        window = items[index - lookback + 1 : index + 1]
        average = sum(
            bar.amount if bar.amount is not None else bar.close * bar.volume
            for bar in window
        ) / lookback
        if average <= 0:
            continue
        scored[symbol] = average
    return [symbol for symbol, _ in sorted(scored.items(), key=lambda item: (-item[1], item[0]))[:top_n]]


def _bisect_right(dates: Sequence[date], target: date) -> int:
    from bisect import bisect_right
    return bisect_right(dates, target)


def build_selector(bars: Sequence[Bar], config: SelectionConfig) -> StockSelector:
    """按配置的池参数生成逐日候选池并返回选股器。

    所有入口（日报、盘中预演、回测）都走这里，避免各自维护一套池口径。
    """
    pool_map = build_pool_map(
        bars,
        top_n=config.universe_size,
        lookback=config.universe_lookback,
        allowed_boards=config.allowed_boards,
        min_price=config.minimum_close,
    )
    return StockSelector(bars, config, allowed_by_date=pool_map)
