"""Compare a strict MA10 crossover overlay with the frozen low-volatility rule."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

if __package__:
    from .research_stock_reset import CAPITAL, ROOT, Market, Rule, metrics, simulate
else:
    from research_stock_reset import CAPITAL, ROOT, Market, Rule, metrics, simulate


def ma10_distance(market: Market) -> np.ndarray:
    distance = np.full(market.prices.shape, np.nan)
    for column in range(market.prices.shape[1]):
        traded = market.traded[:, column]
        prices = market.prices[traded, column]
        distance[traded, column] = prices - pd.Series(prices).rolling(10).mean().to_numpy()
    return pd.DataFrame(distance).ffill().to_numpy()


def main():
    rule = Rule("defensive", 5, 15, False)
    cases = {
        "800": ("ths_daily_combined_800.csv", [
            ("2025", "2025-01-01", "2025-12-31"),
            ("2026", "2026-01-01", "2026-11-13"),
            ("joined", "2025-01-01", "2026-11-13"),
        ]),
        "4420": ("a_share_all.csv", [("2026", "2026-01-26", "2026-11-13")]),
    }
    rows = []
    for universe, (path, windows) in cases.items():
        market = Market(ROOT / "data" / path)
        ma10 = ma10_distance(market)
        for label, first, last in windows:
            for mode, distance in (("baseline", None), ("ma10", ma10)):
                for fee in (.002, .004):
                    for phase in range(rule.interval):
                        values, trading = simulate(market, rule, first, last, fee, phase, ma10=distance)
                        rows.append(dict(universe=universe, window=label, fee=fee, phase=phase, mode=mode,
                                         **metrics(values, CAPITAL), **trading))
    path = ROOT / "output/ma10_research.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} comparisons")


if __name__ == "__main__":
    main()
