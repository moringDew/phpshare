"""Test whether lower turnover or a soft industry score closes the 20% gap."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

if __package__:
    from .research_stock_reset import CAPITAL, ROOT, Market, Rule, metrics, simulate
else:
    from research_stock_reset import CAPITAL, ROOT, Market, Rule, metrics, simulate


def industry_addition(market, memberships):
    # Apply each dated classification only after the snapshot date.
    output = np.full(market.prices.shape, .5)
    prices = market.prices
    for snap, frame in memberships.groupby("snapshot", sort=True):
        symbols = frame.code.str[3:] + "." + frame.code.str[:2].str.upper()
        groups = pd.Series(frame.industry.to_numpy(), index=symbols).reindex(market.symbols)
        first = market.days.searchsorted(snap, side="right")
        last = market.days.searchsorted(snap + pd.Timedelta(days=100), side="right")
        # Later snapshots overwrite overlapping dates. No stale snapshot is carried beyond 100 days.
        for day in range(max(63, first), last):
            moves = (prices[day] / prices[day - 20] + prices[day] / prices[day - 60]) / 2 - 1
            points = pd.Series(np.where(market.traded[day], moves, np.nan), index=market.symbols)
            stats = points.groupby(groups).agg(["mean", "count"])
            ranks = stats["mean"].where(stats["count"] >= 5).rank(pct=True)
            output[day] = groups.map(ranks).fillna(.5).to_numpy()
    return output


def main():
    memberships = pd.read_csv(ROOT / "data/rotation/industry.csv", parse_dates=["snapshot", "updateDate"])
    if (memberships.updateDate > memberships.snapshot).any() or memberships.duplicated(["snapshot", "code"]).any():
        raise ValueError("invalid dated industry assignments")
    varieties = {
        "baseline": ("defensive", 5, None),
        "buffer30": ("defensive", 5, 30),
        "buffer60": ("defensive", 5, 60),
        "buffer30_slower": ("defensive", 10, 30),
        "soft_industry": ("soft", 5, None),
        "soft_and_buffer": ("soft", 5, 30),
        "soft_buffer_slower": ("soft", 10, 30),
    }
    windows = {
        "800": ("ths_daily_combined_800.csv", [
            ("2023-2024", "2023-01-01", "2024-12-31"),
            ("2025", "2025-01-01", "2025-12-31"),
            ("2026", "2026-01-01", "2026-11-13"),
            ("2025-2026", "2025-01-01", "2026-11-13"),
            ("2026-matched", "2026-01-26", "2026-11-13"),
        ]),
        "4420": ("a_share_all.csv", [("2026-matched", "2026-01-26", "2026-11-13")]),
    }
    rows = []
    for universe, (file, periods) in windows.items():
        market = Market(ROOT / "data" / file)
        market.scores["soft"] = .85 * market.scores["defensive"] + .15 * industry_addition(market, memberships)
        for family in ("defensive", "soft"):
            market.order[family] = np.argsort(-market.scores[family], axis=1, kind="stable")[:, :60]
        for period, start, end in periods:
            for name, (family, interval, hold_rank) in varieties.items():
                rule = Rule(family, interval, 15, False)
                for fee in (.002, .004):
                    for phase in range(interval):
                        values, trades = simulate(market, rule, start, end, fee, phase, hold_rank=hold_rank)
                        rows.append(dict(universe=universe, period=period, name=name, fee=fee, phase=phase,
                                         **metrics(values, CAPITAL), **trades))
            print(universe, period, flush=True)
    out = ROOT / "output/target20_research.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
