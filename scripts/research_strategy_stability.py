"""Compare fixed strategy variants without changing the live configuration."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import replace
from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from stock_selection.capital import run_capital_backtest
from stock_selection.engine import StockSelector, SelectionConfig, load_bars
from stock_selection.universe import build_pool_map


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/a_share_all.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "output/strategy_stability.json")
    args = parser.parse_args()
    base = SelectionConfig.from_json(ROOT / "configs/stock_selection_optimized.json")
    low_risk = dict(base.weights, momentum_20=.08, momentum_60=.17,
                    low_volatility=.25, low_drawdown=.15)
    variants = {
        "baseline": {},
        "diversify": {"top_n": 8, "maximum_single_position": .075},
        "low_risk": {"weights": low_risk},
        "diversify_low_risk": {"top_n": 8, "maximum_single_position": .075, "weights": low_risk},
        "faster": {"rebalance_days": 10},
        "looser_stop": {"stop_loss_pct": .12},
        "breadth_50": {"minimum_market_breadth": .50},
        "staggered": {},
    }
    bars = load_bars(args.input)
    start = date(2026, 1, 1)
    pools = build_pool_map(bars, base.universe_size, base.universe_lookback,
                           base.allowed_boards, base.minimum_close, start=start)
    selector = StockSelector(bars, base, allowed_by_date=pools)
    breadth = lru_cache(maxsize=None)(selector.market_breadth)
    original_select = selector.select
    max_top_n = max(options.get("top_n", base.top_n) for options in variants.values())

    @lru_cache(maxsize=None)
    def ranked(day, weights):
        # All variants share the same universe, factor definitions and filters.
        config = selector.config
        selector.config = replace(base, top_n=max_top_n, weights=dict(weights))
        try:
            return original_select(day)
        finally:
            selector.config = config

    selector.market_breadth = breadth
    selector.select = lambda day: ranked(day, tuple(selector.config.weights.items()))[: selector.config.top_n]
    periods = {
        "continuous": (start, date(2026, 11, 16)),
        "development": (start, date(2026, 6, 30)),
        "validation": (date(2026, 7, 1), date(2026, 11, 16)),
    }
    rows = []
    for period, (first, last) in periods.items():
        days = [day for day in selector.calendar if first <= day <= last]
        for name, options in variants.items():
            for offset in ((0,) if period == "continuous" else (0, 5)):
                for cost in (.0015, .003):
                    selector.config = replace(base, **options, transaction_cost_rate=cost)
                    offsets = (offset, offset + 10) if name == "staggered" else (offset,)
                    capital = base.initial_capital / len(offsets)
                    runs = [run_capital_backtest(selector, days[i], last, capital) for i in offsets]
                    by_day = [{row["date"]: row for row in run.equity_curve} for run in runs]
                    values = [base.initial_capital]
                    invested = 0
                    peak = values[0]
                    drawdown = 0.0
                    for day in days[offset:]:
                        marks = [curve.get(day.isoformat()) for curve in by_day]
                        equity = sum(mark["equity"] if mark is not None else capital for mark in marks)
                        invested += any(mark is not None and mark["positions"] for mark in marks)
                        values.append(equity)
                        peak = max(peak, equity)
                        drawdown = max(drawdown, 1 - equity / peak)
                    daily = [b / a - 1 for a, b in zip(values, values[1:])]
                    rows.append({
                        "period": period, "strategy": name, "offset": offset, "cost": cost,
                        "start": runs[0].start_date, "end": runs[0].end_date,
                        "return": values[-1] / values[0] - 1, "drawdown": drawdown,
                        "volatility": statistics.pstdev(daily) * 252 ** .5,
                        "trades": sum(run.trade_count for run in runs),
                        "fees": sum(run.fees for run in runs),
                        "invested_fraction": invested / len(daily),
                    })
            print(period, name, flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"input": args.input.name, "bar_count": len(bars),
               "symbols": len(selector.by_symbol), "data_start": min(selector.calendar).isoformat(),
               "data_end": max(selector.calendar).isoformat(), "variants": variants, "results": rows}
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
