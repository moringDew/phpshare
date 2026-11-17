"""Measure incremental directional skill from market and industry context."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from threadpoolctl import threadpool_limits

if __package__:
    from .research_stock_forecast20 import ROOT, panel
else:
    from research_stock_forecast20 import ROOT, panel


def context_features(price, trade, indices, memberships):
    feats = {}
    for name, code in (("large", "sh.000300"), ("mid", "sh.000905")):
        idx = indices[code]
        indicators = {
            "ret5": idx / idx.shift(5) - 1,
            "ret20": idx / idx.shift(20) - 1,
            "ma60": idx / idx.rolling(60).mean() - 1,
            "vol20": idx.pct_change(fill_method=None).rolling(20).std(),
        }
        for key, values in indicators.items():
            vals = values.reindex(price.index).to_numpy()
            feats[f"market_{name}_{key}"] = pd.DataFrame(np.broadcast_to(vals[:, None], price.shape),
                                                         index=price.index, columns=price.columns)

    names = ("industry_peer5", "industry_peer20", "industry_relative20", "industry_breadth20")
    output = {name: np.full(price.shape, np.nan, dtype=np.float32) for name in names}
    ret5 = (price / price.shift(5) - 1).where(trade).to_numpy()
    ret20 = (price / price.shift(20) - 1).where(trade).to_numpy()
    groups = list(memberships.groupby("snapshot", sort=True))
    for i, (snap, frame) in enumerate(groups):
        start = price.index.searchsorted(snap, side="right")
        end = min(price.index.searchsorted(snap + pd.Timedelta(days=100), side="right"),
                  price.index.searchsorted(groups[i + 1][0], side="right") if i + 1 < len(groups) else len(price))
        if start >= end:
            continue
        for _, industry in frame.groupby("industry"):
            symbols = industry.code.str[3:] + "." + industry.code.str[:2].str.upper()
            cols = np.flatnonzero(price.columns.isin(symbols))
            if len(cols) < 6:
                continue
            columns = (slice(start, end), cols)
            r5, r20 = ret5[columns], ret20[columns]

            def peer_mean(values, observed):
                n = observed.sum(axis=1, keepdims=True) - observed
                values = np.where(observed, values, 0.)
                return np.divide(values.sum(axis=1, keepdims=True) - values, n,
                                 out=np.full_like(values, np.nan, dtype=float), where=n >= 5)

            p5 = peer_mean(r5, np.isfinite(r5))
            p20 = peer_mean(r20, np.isfinite(r20))
            breadth = peer_mean(r20 > 0, np.isfinite(r20))
            for name, values in zip(names, (p5, p20, r20 - p20, breadth)):
                output[name][columns] = values
    feats.update({name: pd.DataFrame(values, index=price.index, columns=price.columns)
                  for name, values in output.items()})
    return feats


def metrics(actual, p):
    up = actual > 0
    pred = p >= .5
    return {"accuracy": float((up == pred).mean()),
            "balanced": float((pred[up].mean() + (~pred[~up]).mean()) / 2),
            "auc": float(roc_auc_score(up, p)), "brier": float(np.mean((p - up) ** 2)),
            "n": len(up)}


def evaluate(model, X, meta, cols, start, end, maturity):
    mask = meta.date.between(start, end) & (meta.maturity <= maturity) & meta.actual.notna()
    y = meta.loc[mask, "actual"].to_numpy()
    prediction = model.predict_proba(X[mask][:, cols])[:, 1]
    dates = meta.loc[mask, "date"].reset_index(drop=True)
    results = {"whole": metrics(y, prediction)}
    for label, sub in (("first_half", dates.dt.month <= 6), ("second_half", dates.dt.month > 6)):
        if sub.any():
            results[label] = metrics(y[sub], prediction[sub])
    # Separate pooled direction skill from same-day ability to rank individual stocks.
    ranked = pd.DataFrame({"day": dates, "actual": y, "p": prediction})
    by_day = [group.p.corr(group.actual, method="spearman") for _, group in ranked.groupby("day")
              if group.p.nunique() > 1]
    results["whole"]["ic"] = float(np.mean(by_day)) if by_day else 0.0
    return results


def main():
    data = ROOT / "data"
    cutoff = pd.Timestamp("2026-11-16")
    indices = pd.read_csv(data / "rotation/index_close.csv", parse_dates=["date"]).pivot(
        index="date", columns="code", values="close").sort_index()
    memberships = pd.read_csv(data / "rotation/industry.csv", parse_dates=["snapshot", "updateDate"])
    if (memberships.updateDate > memberships.snapshot).any() or memberships.duplicated(["snapshot", "code"]).any():
        raise ValueError("invalid historical industry snapshots")
    context = lambda price, trade: context_features(price, trade, indices, memberships)
    X, meta, names = panel(data / "ths_daily_combined_800.csv", cutoff, context)
    broad_X, broad, broad_names = panel(data / "a_share_all.csv", cutoff, context)
    assert names == broad_names
    base = [i for i, n in enumerate(names) if not n.startswith(("market_", "industry_"))]
    idx = [i for i, n in enumerate(names) if n.startswith("market_")]
    ind = [i for i, n in enumerate(names) if n.startswith("industry_")]
    configs = {"stock": base, "market_only": idx, "stock_market": base + idx,
               "stock_industry": base + ind, "stock_both": base + idx + ind}
    results = {}
    with threadpool_limits(limits=4):
        for name, cols in configs.items():
            periods = {}
            for year in ("2025", "2026"):
                train_until = pd.Timestamp(year + "-01-01")
                train = (meta.date >= pd.Timestamp("2023-01-03")) & (meta.maturity < train_until) & meta.actual.notna()
                model = HistGradientBoostingClassifier(
                    max_iter=100, learning_rate=.06, max_leaf_nodes=15, min_samples_leaf=150,
                    l2_regularization=2, early_stopping=False, random_state=42,
                ).fit(X[train][:, cols], meta.loc[train, "actual"] > 0)
                start = year + ("-01-01" if year == "2025" else "-01-26")
                end = "2025-12-31" if year == "2025" else str(cutoff.date())
                periods[f"800_{year}"] = evaluate(model, X, meta, cols, start, end, pd.Timestamp(end))
                if year == "2026":
                    periods["4420_2026"] = evaluate(model, broad_X, broad, cols, start, end, cutoff)
            results[name] = periods
            print(name, flush=True)
    out = ROOT / "output/forecast20_feature_ablation.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"features": {k: [names[i] for i in v] for k, v in configs.items()},
                               "results": results}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
