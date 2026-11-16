from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Set

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from stock_selection.capital import run_capital_backtest  # noqa: E402
from stock_selection.engine import Bar, SelectionConfig, StockSelector, load_bars  # noqa: E402

STORE = ROOT / "data" / "ths_daily_combined_800.csv"
CONFIG = ROOT / "configs" / "stock_selection_multifactor.json"
START = date(2022, 11, 14)
END = date(2026, 11, 13)
TOP_N = 500
LOOKBACK = 20


def build_allowed(bars: List[Bar], start: date, end: date, top_n: int = TOP_N) -> Dict[date, Set[str]]:
    by_date: Dict[date, Dict[str, Bar]] = {}
    by_symbol: Dict[str, List[Bar]] = {}
    for bar in bars:
        by_date.setdefault(bar.trade_date, {})[bar.symbol] = bar
        by_symbol.setdefault(bar.symbol, []).append(bar)
    for items in by_symbol.values():
        items.sort(key=lambda item: item.trade_date)
    calendar = sorted(by_date)
    start_index = next(i for i, day in enumerate(calendar) if day >= start)
    allowed: Dict[date, Set[str]] = {}
    rebalance_indices = list(range(start_index, len(calendar), 20))
    for position, index in enumerate(rebalance_indices):
        day = calendar[index]
        next_index = rebalance_indices[position + 1] if position + 1 < len(rebalance_indices) else len(calendar)
        scores = {}
        for symbol, items in by_symbol.items():
            if len(items) < LOOKBACK:
                continue
            window = items[-LOOKBACK:] if items[-1].trade_date <= day else [b for b in items if b.trade_date <= day][-LOOKBACK:]
            if len(window) < LOOKBACK:
                continue
            if any(bar.close < 2.0 or bar.suspended for bar in window[-1:]):
                continue
            average = sum(bar.close * bar.volume for bar in window) / len(window)
            scores[symbol] = average
        chosen = {symbol for symbol, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_n]}
        for current in calendar[index:next_index]:
            allowed[current] = chosen
    return allowed


def main() -> int:
    bars = load_bars(STORE)
    config = SelectionConfig.from_json(CONFIG)
    allowed = build_allowed(bars, START, END)
    print("allowed days", len(allowed), "first", min(allowed), "last", max(allowed), flush=True)
    selector = StockSelector(bars, config, allowed_by_date=allowed)
    result = run_capital_backtest(selector, START, END)
    print("point-in-time top500: final {:.2f} ret {:+.2f}% mdd {:.2f}% trades {}".format(
        result.final_equity, result.total_return * 100,
        result.maximum_drawdown * 100, result.trade_count), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
