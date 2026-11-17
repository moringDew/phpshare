"""Ablation of index timing, industry rotation, and stock ranking."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd

if __package__:
    from .research_stock_reset import CAPITAL, ROOT, Market, Rule, metrics, simulate
else:
    from research_stock_reset import CAPITAL, ROOT, Market, Rule, metrics, simulate

DATA = ROOT / "data" / "rotation"


def index_signals(market, close):
    indices = close[["sh.000300", "sh.000905"]].sort_index()
    moving = indices.rolling(60).mean()
    exposure = (indices > moving).sum(axis=1) * .425
    reference = ((indices / indices.shift(20) - 1).mean(axis=1)
                 + (indices / indices.shift(60) - 1).mean(axis=1)) / 2
    exposure = exposure.where(moving.notna().all(axis=1)).reindex(market.days)
    return exposure.to_numpy(), reference.reindex(market.days).to_numpy()


def sector_order(market, memberships, reference):
    table = memberships.copy()
    table["snapshot"] = pd.to_datetime(table.snapshot)
    table["updateDate"] = pd.to_datetime(table.updateDate)
    if (table.updateDate > table.snapshot).any():
        raise ValueError("industry record dated after its snapshot")
    table["symbol"] = table.code.str[3:] + "." + table.code.str[:2].str.upper()
    if table.duplicated(["snapshot", "symbol"]).any():
        raise ValueError("duplicate industry assignment")
    groups = {
        snap: [(name, np.flatnonzero(market.symbols.isin(df.symbol)))
               for name, df in group.groupby("industry", sort=True)]
        for snap, group in table.groupby("snapshot", sort=True)
    }
    dates = pd.DatetimeIndex(sorted(groups))
    membership_coverage = {}
    order = np.full((len(market.days), 15), -1, dtype=int)
    defensive = market.scores["defensive"]
    for day in range(60, len(market.days)):
        day_stamp = market.days[day]
        snap = dates.searchsorted(day_stamp, side="right") - 1
        if snap < 0 or not np.isfinite(reference[day]):
            continue
        membership = groups[dates[snap]]
        membership_coverage[str(dates[snap].date())] = sum(len(cols) for _, cols in membership)
        prices = market.prices
        ranked = []
        for name, cols in membership:
            cols = cols[(market.traded[day, cols]) & np.isfinite(prices[day - 60, cols])]
            if len(cols) < 5:
                continue
            moves = (prices[day, cols] / prices[day - 20, cols]
                     + prices[day, cols] / prices[day - 60, cols]) / 2 - 1
            excess = float(np.mean(moves)) - reference[day]
            eligible = cols[np.isfinite(defensive[day, cols])]
            if len(eligible) and excess > 0:
                ranked.append((-excess, name, eligible))
        top = sorted(ranked, key=lambda item: item[:2])[:4]
        buckets = [cols[np.argsort(-defensive[day, cols], kind="stable")][:4] for _, _, cols in top]
        picks = [bucket[rank] for rank in range(4) for bucket in buckets if rank < len(bucket)][:15]
        order[day, :len(picks)] = picks
    # The simulator discards non-finite scores, so point unused slots to a non-tradable sentinel.
    rotated = copy.copy(market)
    rotated.order = market.order | {"defensive": np.where(order < 0, len(market.symbols), order)}
    rotated.scores = market.scores | {
        "defensive": np.pad(defensive, ((0, 0), (0, 1)), constant_values=-np.inf)
    }
    return rotated, membership_coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DATA / "index_close.csv")
    full = DATA / "industry.csv"
    parser.add_argument("--industry", type=Path,
                        default=full if full.exists() else DATA / "industry_recent.csv")
    parser.add_argument("--index-only", action="store_true", help="Run index and stock controls without industry data")
    parser.add_argument("--window", help="Restrict to one named research window")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    close = pd.read_csv(args.index, parse_dates=["date"]).pivot(index="date", columns="code", values="close")
    memberships = None if args.index_only else pd.read_csv(args.industry)
    snapshots = (pd.DatetimeIndex(pd.to_datetime(memberships.snapshot.unique())).sort_values()
                 if memberships is not None else pd.DatetimeIndex([]))
    rule = Rule("defensive", 5, 15, False)
    rows, coverage = [], {}
    windows = {
        "800": ("ths_daily_combined_800.csv", [
            ("2023_2024", "2023-01-01", "2024-12-31"),
            ("2025", "2025-01-01", "2025-12-31"),
            ("2026", "2026-01-01", "2026-11-13"),
            ("continuous", "2025-01-01", "2026-11-13"),
        ]),
        "4420": ("a_share_all.csv", [("2026", "2026-01-26", "2026-11-13")]),
    }
    for universe, (file, groups) in windows.items():
        tested = 0
        market = Market(ROOT / "data" / file)
        exposure, reference = index_signals(market, close)
        modes = [("stocks", market, None), ("index_stocks", market, exposure),
                 ("half_stocks", market, np.full(len(market.days), .425))]
        if memberships is not None:
            rotated, coverage[universe] = sector_order(market, memberships, reference)
            modes += [("sector_stocks", rotated, None), ("index_sector_stocks", rotated, exposure)]
        for name, start, end in groups:
            if args.window and name != args.window:
                continue
            lo, hi = market.days.searchsorted([start, end])
            if len(snapshots) and market.days[lo - 1] < snapshots[0]:
                print(f"Skipping {name}: starts before industry history", flush=True)
                continue
            if not np.isfinite(exposure[lo - 1:hi + 1]).all():
                raise ValueError(f"{name}: missing index data on stock trading days")
            if len(snapshots):
                days = market.days[lo - 1:hi + 1]
                prior = snapshots[snapshots.searchsorted(days, side="right") - 1]
                if (days - prior).days.max() > 100:
                    raise ValueError(f"{name}: industry snapshot gap exceeds 100 calendar days")
            tested += 1
            for mode, selected, timing in modes:
                for fee in (.002, .004):
                    for phase in range(5):
                        curve, trades = simulate(selected, rule, start, end, fee, phase, exposure_by_signal=timing)
                        rows.append({"universe": universe, "window": name, "mode": mode, "phase": phase,
                                     "fee": fee, **metrics(curve, CAPITAL), **trades})
        print(f"{universe}: {tested} windows", flush=True)
    if not rows:
        raise ValueError("no research window matched")
    out = args.output or ROOT / "output" / ("index_timing_control.json" if args.index_only else "sector_rotation.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"coverage": coverage, "runs": rows}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
