"""Forecast the next 20 trading sessions using historical closes and volume."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import roc_auc_score
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[1]
HOLD = 20
LAG = HOLD + 1


def panel(path, cutoff, context=None):
    raw = pd.read_csv(path, parse_dates=["date"])
    raw = raw.loc[(raw.date <= cutoff) & raw.symbol.str.fullmatch(
        r"(?:60[0135]\d{3}\.SH|00[0123]\d{3}\.SZ|30[01]\d{3}\.SZ)"
    )].set_index(["date", "symbol"]).sort_index()
    volume = raw.volume.unstack()
    close = (raw.close * raw.adj_factor).unstack().where((raw.suspended.unstack() == 0) & (volume > 0))
    trade = close.notna()
    price = close.ffill()
    change = price.pct_change(fill_method=None)
    turnover = (raw.close.unstack() * volume).rolling(20, min_periods=18).mean()
    included = trade & (raw.close.unstack() >= 3) & (turnover >= 30_000_000) & (raw.is_st.unstack() == 0)
    included &= trade.rolling(65).sum() >= 61
    feats = {}
    for length in (1, 5, 10, 20, 60):
        ret = price / price.shift(length) - 1
        feats[f"return{length}"] = ret
        if length != 1:
            feats[f"relative{length}"] = ret.where(included).rank(pct=True, axis=1)
    for length in (10, 20, 60):
        feats[f"ma_gap{length}"] = price / price.rolling(length).mean() - 1
    feats["volatility20"] = change.rolling(20).std()
    feats["drawdown60"] = price / price.rolling(60).max() - 1
    feats["volume5_20"] = np.log(volume.rolling(5).mean() / volume.rolling(20).mean())
    feats["volume1_20"] = np.log(volume.where(volume > 0) / volume.shift(1).rolling(20).mean())
    feats["volume_direction"] = ((np.sign(change) * volume).rolling(20).sum() / volume.rolling(20).sum())
    feats["liquidity_rank"] = turnover.where(included).rank(pct=True, axis=1)
    if context is not None:
        feats.update(context(price, trade))
    # Use only actual quoted closes at both endpoints, never a carried price as a label.
    actual = close.shift(-LAG) / close.shift(-1) - 1
    rows, cols = np.where(included.to_numpy())
    X = np.stack([f.to_numpy()[rows, cols] for f in feats.values()], axis=1).astype("float32")
    X[~np.isfinite(X)] = np.nan
    maturity = pd.Series(close.index, index=close.index).shift(-LAG)
    meta = pd.DataFrame({"date": close.index[rows], "symbol": close.columns[cols],
                         "maturity": maturity.to_numpy()[rows], "actual": actual.to_numpy()[rows, cols],
                         "close": raw.close.unstack().to_numpy()[rows, cols]})
    return X, meta, list(feats)


def fit(X, meta, first_prediction):
    train = (meta.maturity < first_prediction) & meta.actual.notna()
    if not train.any():
        raise ValueError("no fully observed training labels before the first prediction")
    X, y = X[train], meta.actual[train].to_numpy()
    args = dict(max_iter=100, learning_rate=.06, max_leaf_nodes=15, min_samples_leaf=150,
                l2_regularization=2, early_stopping=False, random_state=42)
    clf = HistGradientBoostingClassifier(**args).fit(X, y > 0)
    reg = HistGradientBoostingRegressor(**args).fit(X, np.clip(y, -.25, .25))
    return clf, reg, float(np.mean(y > 0)), int(train.sum())


def recalibrate(out, training_rate, window=60, minimum=20):
    """Adjust the prior from already-settled labels; never fit on an unmatured outcome."""
    labeled = out[out.actual.notna()].assign(up=lambda rows: rows.actual.gt(0).astype(int))
    settled = labeled.groupby("maturity", sort=True).agg(n=("up", "size"), up=("up", "sum"),
                                                         estimated=("probability", "sum"))
    days = pd.DatetimeIndex(out.date.unique()).sort_values()
    logits = {}
    priors = {}
    for day in days:
        right = settled.index.searchsorted(day, side="left")
        if right < minimum:
            logits[day], priors[day] = 0.0, training_rate
            continue
        counts = settled.iloc[max(0, right - window):right].sum()
        actual = np.clip(counts.up / counts.n, .01, .99)
        predicted = np.clip(counts.estimated / counts.n, .01, .99)
        logits[day] = float(np.clip(np.log(actual / (1 - actual)) - np.log(predicted / (1 - predicted)), -1, 1))
        priors[day] = float(actual)
    p = out.probability.clip(.001, .999)
    adjusted = 1 / (1 + np.exp(-(np.log(p / (1 - p)) + out.date.map(logits))))
    return adjusted, out.date.map(priors)


def direction_metrics(out):
    y = out.actual > 0
    metrics = {}
    for col in ("probability", "adjusted", "prior"):
        p = out[col]
        signal = p >= .5
        metrics[col] = {
            "accuracy": float((signal == y).mean()),
            "balanced_accuracy": float((signal[y].mean() + (~signal[~y]).mean()) / 2),
            "brier": float(np.mean((p - y) ** 2)),
        }
    return metrics


def evaluate(X, meta, model, start, end, cutoff):
    clf, reg, baseline, n_train = model
    valid = meta.date.between(start, end) & (meta.maturity <= cutoff)
    out = meta.loc[valid, ["date", "maturity", "actual"]].copy()
    out["probability"] = clf.predict_proba(X[valid])[:, 1]
    out["estimate"] = reg.predict(X[valid])
    out["adjusted"], out["prior"] = recalibrate(out, baseline)
    labeled = out[out.actual.notna()]
    y = labeled.actual > 0
    predicted = labeled.probability >= .5
    up_recall = float(predicted[y].mean())
    down_recall = float((~predicted[~y]).mean())
    daily = []
    for day, group in out.groupby("date"):
        top_all = group.nlargest(max(1, len(group) // 10), "estimate")
        top = top_all[top_all.actual.notna()]
        daily.append({"date": day.date().isoformat(),
                      "ic": group.estimate.corr(group.actual, method="spearman"),
                      "top": top.actual.mean(), "all": group.actual.mean(), "top_up": (top.actual > 0).mean(),
                      "observed": group.actual.notna().mean(), "top_observed": len(top) / len(top_all)})
    scores = pd.DataFrame(daily)
    lifts = scores.top - scores["all"]
    phase = [lifts.iloc[i::LAG] for i in range(LAG)]
    calibration = []
    for low, high in zip((0, .4, .5, .6, .7), (.4, .5, .6, .7, 1.00001)):
        bucket = labeled[(labeled.probability >= low) & (labeled.probability < high)]
        if not bucket.empty:
            calibration.append({"from": low, "count": len(bucket),
                                "model": float(bucket.probability.mean()),
                                "observed": float((bucket.actual > 0).mean())})
    return {"n_train": n_train, "scores": len(labeled), "signal_days": len(scores),
            "first": scores.date.iloc[0], "last": scores.date.iloc[-1],
            "auc": roc_auc_score(y, labeled.probability),
            "accuracy": float((predicted == y).mean()),
            "always_down_accuracy": float((~y).mean()),
            "balanced_accuracy": (up_recall + down_recall) / 2,
            "up_recall": up_recall, "down_recall": down_recall,
            "brier": float(np.mean((labeled.probability - y) ** 2)),
            "brier_base": float(np.mean((baseline - y) ** 2)), "average_up": float(y.mean()),
            "ic": float(scores.ic.mean()), "top_return": float(scores.top.mean()),
            "all_return": float(scores["all"].mean()), "top_up": float(scores.top_up.mean()),
            "lift": float(lifts.mean()), "phases_with_positive_lift": int(sum(x.mean() > 0 for x in phase)),
            "phases_total": LAG, "observed": float(scores.observed.mean()),
            "top_observed": float(scores.top_observed.mean()), "calibration": calibration, "daily": daily,
            "direction_compare": direction_metrics(labeled),
            "direction_halves": {name: direction_metrics(group) for name, group in (
                ("first_half", labeled[labeled.date.dt.month <= 6]),
                ("second_half", labeled[labeled.date.dt.month > 6])) if not group.empty}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default="2026-11-16")
    parser.add_argument("--symbol", help="Show one covered symbol, such as 600519.SH")
    args = parser.parse_args()
    cutoff = pd.Timestamp(args.as_of)
    X, meta, feats = panel(ROOT / "data/ths_daily_combined_800.csv", cutoff)
    broad_X, broad, _ = panel(ROOT / "data/a_share_all.csv", cutoff)
    validations = {}
    with threadpool_limits(limits=4):
        model_25 = fit(X, meta, pd.Timestamp("2025-01-01"))
        validations["800_2025"] = evaluate(X, meta, model_25, "2025-01-01", "2025-12-31", pd.Timestamp("2025-12-31"))
        model = fit(X, meta, pd.Timestamp("2026-01-01"))
        validations["800_2026"] = evaluate(X, meta, model, "2026-01-26", args.as_of, cutoff)
        validations["4420_2026"] = evaluate(broad_X, broad, model, "2026-01-26", args.as_of, cutoff)
        last = broad.date.max()
        latest = broad.date.eq(last)
        result = broad.loc[latest, ["symbol", "close"]].copy()
        result["probability_up"] = model[0].predict_proba(broad_X[latest])[:, 1]
        result["return_score"] = model[1].predict(broad_X[latest])
    result = result.sort_values("return_score", ascending=False)
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    result.to_csv(output / "forecast20_stocks.csv", index=False)
    payload = {"as_of": last.date().isoformat(), "stocks": len(result), "features": feats,
               "validations": validations, "top": result.head(10).to_dict("records"),
               "bottom": result.tail(10).iloc[::-1].to_dict("records")}
    (output / "forecast20_validation.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.symbol:
        print(result.loc[result.symbol == args.symbol.upper()].to_string(index=False))
    else:
        print(f"{len(result)} forecasts on {last.date()}")


if __name__ == "__main__":
    main()
