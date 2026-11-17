"""Stocks-only research: select rules on 2023–2024, then freeze for 2025–2026."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CAPITAL = 100_000.0
FAMILIES = ("pullback", "reversal", "breakout", "defensive", "blend")


@dataclass(frozen=True)
class Rule:
    family: str
    interval: int
    positions: int
    regime_filter: bool


class Market:
    def __init__(self, path: Path):
        rows = pd.read_csv(path)
        rows = rows[rows.symbol.str.fullmatch(r"(?:60[0135]\d{3}\.SH|00[0123]\d{3}\.SZ|30[01]\d{3}\.SZ)")]
        rows["date"] = pd.to_datetime(rows.date)
        rows = rows.sort_values("date").set_index(["date", "symbol"])
        close = rows.close.unstack()
        volume = rows.volume.unstack()
        quotes = close.where((close > 0) & (volume > 0) & (rows.suspended.unstack() == 0))
        prices = quotes.ffill()
        change = quotes / prices.shift() - 1
        self.days = close.index
        self.symbols = close.columns
        self.prices = prices.to_numpy()
        self.traded = quotes.notna().to_numpy()
        self.risk_flag = (rows.is_st.unstack().fillna(0) != 0).to_numpy()
        # A conservative buffer around approximate limit prices; quotes are adjusted.
        limit = np.where(close.columns.str.startswith("30"), .195, .095)
        limit = np.where(self.risk_flag, .045, limit)
        self.buyable = self.traded & ~self.risk_flag & (change.to_numpy() < limit)
        self.sellable = self.traded & (change.to_numpy() > -limit)
        # If the previous quote is missing because a security just appeared, no order can be sized.
        liquid = (quotes * volume).rolling(20, min_periods=18).mean() >= 30_000_000
        history = quotes.notna().rolling(126, min_periods=126).sum() >= 120
        eligible = liquid & history & (quotes >= 3) & (rows.is_st.unstack() == 0)
        self.benchmark = change.where(liquid.shift(fill_value=False)).mean(axis=1).fillna(0)
        index = (1 + self.benchmark).cumprod()
        self.risk_on = (index >= index.rolling(100).mean()).to_numpy()
        weekly = prices / prices.shift(5) - 1
        month = prices / prices.shift(20) - 1
        relative_strength = prices.shift(5) / prices.shift(63) - 1
        low_risk = -change.rolling(63, min_periods=58).std()
        near_high = prices / prices.rolling(126, min_periods=126).max()
        rank = lambda frame: frame.where(eligible).rank(axis=1, pct=True)
        reversal, trend, defensive, high, monthly = map(rank, (-weekly, relative_strength, low_risk, near_high, month))
        self.scores = {
            "pullback": (.55 * reversal + .30 * trend + .15 * defensive).where(relative_strength > 0),
            "reversal": .7 * reversal + .3 * defensive,
            "breakout": .45 * trend + .35 * high + .20 * monthly,
            "defensive": .7 * defensive + .3 * trend,
            "blend": .40 * reversal + .35 * trend + .25 * defensive,
        }
        self.order = {}
        for family, scores in self.scores.items():
            clean = scores.fillna(-np.inf).to_numpy()
            self.order[family] = np.argsort(-clean, axis=1, kind="stable")[:, :15]
            self.scores[family] = clean


def simulate(market: Market, rule: Rule, start: str, end: str, fee: float, phase: int = 0,
             ma10: np.ndarray | None = None, exposure_by_signal: np.ndarray | None = None,
             hold_rank: int | None = None):
    lo, hi = market.days.searchsorted([start, end], side="left")
    if hi < len(market.days) and market.days[hi] == pd.Timestamp(end):
        hi += 1
    cash = CAPITAL
    shares = np.zeros(len(market.symbols), dtype=int)
    values = []
    fees = turnover = 0.0
    trades = 0
    chosen = np.empty(0, dtype=int)
    exposures = []
    for day in range(lo, hi):
        price = np.nan_to_num(market.prices[day], nan=0.0)
        equity = cash + shares @ price
        signal = day - 1
        rebalances = day > lo and (day - lo - 1 - phase) % rule.interval == 0
        if rebalances:
            ranked = market.order[rule.family][signal]
            ranked = ranked[np.isfinite(market.scores[rule.family][signal, ranked])]
            if hold_rank is None:
                chosen = ranked[:rule.positions]
            else:
                kept = ranked[:hold_rank]
                kept = kept[shares[kept] > 0][:rule.positions]
                additions = ranked[shares[ranked] == 0][:rule.positions - len(kept)]
                chosen = np.r_[kept, additions]
        if rebalances or (ma10 is not None and day > lo and (len(chosen) or shares.any())):
            exposure = (.85 if not rule.regime_filter or market.risk_on[signal] else .35
                        ) if exposure_by_signal is None else exposure_by_signal[signal]
            candidates = chosen
            if ma10 is not None:
                candidates = chosen[ma10[signal, chosen] >= 0]
                crossed = ((ma10[signal - 1, candidates] <= 0) & (ma10[signal, candidates] > 0)
                           & market.traded[signal, candidates]) if signal > 0 else np.zeros(len(candidates), dtype=bool)
                new = candidates[crossed & (shares[candidates] == 0)]
                kept = candidates[shares[candidates] > 0]
                candidates = np.r_[kept, new]
            prior_equity = values[-1]
            target = np.zeros_like(shares)
            # Size at signal close; current close is only used for fills and cash/risk limits.
            signal_prices = market.prices[signal, candidates]
            target[candidates] = (prior_equity * exposure / rule.positions / signal_prices // 100).astype(int) * 100
            if hold_rank is not None and exposure > 0:
                held = candidates[shares[candidates] > 0]
                cap = (prior_equity * exposure * 1.5 / rule.positions
                       / market.prices[signal, held] // 100).astype(int) * 100
                target[held] = np.minimum(shares[held], cap)
            if ma10 is not None and not rebalances:
                target[kept] = shares[kept]
            sales = np.where(market.sellable[day], np.maximum(shares - target, 0), 0)
            gross = sales @ price
            cash += gross * (1 - fee)
            shares -= sales
            fees += gross * fee
            turnover += gross
            trades += np.count_nonzero(sales)
            room = max(0.0, equity * exposure - shares @ price)
            for symbol in candidates:
                if not market.buyable[day, symbol]:
                    continue
                lot = price[symbol] * 100
                qty = min(max(target[symbol] - shares[symbol], 0),
                          int(min(room, cash / (1 + fee)) // lot) * 100)
                if not qty:
                    continue
                gross = qty * price[symbol]
                shares[symbol] += qty
                room -= gross
                cash -= gross * (1 + fee)
                fees += gross * fee
                turnover += gross
                trades += 1
        values.append(float(cash + shares @ price))
        exposures.append(float(shares @ price) / values[-1])
    return np.array(values), {"trades": int(trades), "fees": float(fees),
                              "turnover": float(turnover / CAPITAL), "average_exposure": float(np.mean(exposures))}


def metrics(values: np.ndarray, initial: float):
    full = np.r_[initial, values]
    returns = full[1:] / full[:-1] - 1
    return {
        "total": full[-1] / initial - 1,
        "annual": (full[-1] / initial) ** (252 / len(values)) - 1,
        "drawdown": float(np.max(1 - full / np.maximum.accumulate(full))),
        "volatility": float(returns.std() * np.sqrt(252)),
    }


def test_window(market, rule, start, end, fee=.002, phase=0):
    values, trading = simulate(market, rule, start, end, fee, phase)
    return metrics(values, CAPITAL) | trading


def compare_prices(history: Market, broad: Market):
    def available(market):
        quoted = np.where(market.traded, market.prices, np.nan)
        return pd.DataFrame(quoted, index=market.days, columns=market.symbols).stack()

    overlap = pd.concat([available(history), available(broad)], axis=1, join="inner")
    moves = overlap.groupby(level=1).pct_change(fill_method=None).dropna()
    moves = moves.loc["2026-01-26":"2026-11-13"]
    gap = (moves[0] - moves[1]).abs()
    return {"observations": len(moves), "symbols": moves.index.get_level_values(1).nunique(),
            "p99_abs_daily_gap": float(gap.quantile(.99))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=ROOT / "data/ths_daily_combined_800.csv")
    parser.add_argument("--broad", type=Path, default=ROOT / "data/a_share_all.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "output/stock_reset.json")
    args = parser.parse_args()
    history = Market(args.history)
    train = []
    for params in product(FAMILIES, (5, 10, 20), (8, 15), (False, True)):
        rule = Rule(*params)
        runs = [test_window(history, rule, "2023-01-01", "2024-12-31", phase=p)
                for p in (0, rule.interval // 2)]
        # Favor the weaker phase, penalize drawdown; the future windows are not used here.
        score = min(r["annual"] - .75 * r["drawdown"] for r in runs)
        train.append({"rule": asdict(rule), "score": score, "runs": runs})
    train.sort(key=lambda row: -row["score"])
    best = {fam: next(row for row in train if row["rule"]["family"] == fam) for fam in FAMILIES}
    print("Training complete; frozen winner:", train[0]["rule"], flush=True)
    broad = Market(args.broad)
    windows = {
        "2023": (history, "2023-01-01", "2023-12-31"),
        "2024": (history, "2024-01-01", "2024-12-31"),
        "2025": (history, "2025-01-01", "2025-12-31"),
        "2026_long": (history, "2026-01-01", "2026-11-13"),
        "2026_broad": (broad, "2026-01-26", "2026-11-13"),
        "2026_same_dates": (history, "2026-01-26", "2026-11-13"),
        "holdout_joined": (history, "2025-01-01", "2026-11-13"),
    }
    validated = {}
    winner = Rule(**train[0]["rule"])
    for name, (market, start, end) in windows.items():
        stats = {}
        for family, row in best.items():
            rule = Rule(**row["rule"])
            stats[family] = [test_window(market, rule, start, end, fee, phase)
                             | {"fee": fee, "phase": phase}
                             for fee, phase in product((.002, .004), (0, rule.interval // 2))]
        benchmark = (1 + market.benchmark.loc[start:end]).cumprod()
        phase_sweep = [test_window(market, winner, start, end, fee, phase)
                       | {"phase": phase, "fee": fee}
                       for fee, phase in product((.002, .004), range(winner.interval))]
        validated[name] = {"sessions": len(benchmark), "benchmark": metrics(benchmark.to_numpy(), 1),
                           "strategies": stats, "winner_all_phases": phase_sweep}
    payload = {"winner": train[0]["rule"], "family_winners": best, "train": train, "windows": validated,
               "overlap": compare_prices(history, broad),
               "sources": {p.name: {"symbols": len(m.symbols), "start": str(m.days[0].date()),
                                    "end": str(m.days[-1].date())}
                           for p, m in ((args.history, history), (args.broad, broad))}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
