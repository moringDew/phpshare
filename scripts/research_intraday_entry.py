"""Paired comparison of entry timing; never places orders."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median

import numpy as np

if __package__:
    from . import intraday_store
    from .research_stock_reset import Market, ROOT
else:
    import intraday_store
    from research_stock_reset import Market, ROOT


def session_slots():
    return tuple(
        (datetime.strptime(start, "%H:%M") + timedelta(minutes=15 * k)).strftime("%H:%M")
        for start in ("09:30", "13:00") for k in range(1, 9)
    )


SLOTS = session_slots()


def buyable(price, reference, limit):
    return -limit < price / reference - 1 < limit


def rebound_entry(bars, reference, limit):
    """Signal uses the completed bar; fill uses only the next bar's open."""
    low = float("inf")
    for i, bar in enumerate(bars[:-1]):
        low = min(low, bar["low"])
        if i < 1 or SLOTS[i] > "14:30":
            continue
        close = bar["close"]
        if low > reference * .99 or close < low * 1.006 or close <= bars[i - 1]["close"]:
            continue
        price = bars[i + 1]["open"]
        if buyable(price, reference, limit) and price <= close * 1.005:
            return {"signal": SLOTS[i], "fill_bar_end": SLOTS[i + 1], "price": price}
    return None


def load_symbol(symbol, fetch):
    cached = intraday_store.load_15m(symbol)
    if not fetch:
        return cached, None
    try:
        bars = intraday_store.fetch_sina_15m(symbol)
        if bars:
            intraday_store.save_15m(symbol, bars)
            cached.update(bars)
        return cached, None if bars else "no data"
    except (OSError, ValueError) as exc:
        return cached, str(exc)


def summarize(rows, extra_slippage):
    if not rows:
        return None
    triggered = [r for r in rows if r["signal"]]
    by_date = defaultdict(list)
    returns = []
    skips = []
    opens = []
    for row in rows:
        net = row["rebound_return"] - row["close_return"]
        if row["signal"]:
            net -= extra_slippage
        by_date[row["entry_date"]].append(net)
        returns.append(net)
        skips.append((row["rebound_return"] - extra_slippage if row["signal"] else 0)
                     - row["close_return"])
        if row["open_return"] is not None:
            opens.append(net + row["close_return"] - row["open_return"])
    return {
        "orders": len(rows), "trade_days": len(by_date), "triggered": len(triggered),
        "entry_saving_when_triggered": mean(r["price_saving"] for r in triggered) if triggered else None,
        "mean_return_gain_vs_close": mean(returns), "median_gain_vs_close": median(returns),
        "mean_return_gain_vs_open": mean(opens) if opens else None, "open_pairs": len(opens),
        "mean_gain_skip_if_no_signal": mean(skips),
        "days_better": sum(mean(v) > 0 for v in by_date.values()),
        "days_worse": sum(mean(v) < 0 for v in by_date.values()),
        "by_date": {day: mean(v) for day, v in by_date.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="Fetch public Sina 15-minute quotes")
    parser.add_argument("--start", default="2026-08-17")
    parser.add_argument("--end", default="2026-11-13")
    parser.add_argument("--output", type=Path, default=ROOT / "output/intraday_entry.json")
    args = parser.parse_args()
    market = Market(ROOT / "data/a_share_all.csv")
    lo, hi = market.days.searchsorted([args.start, args.end])
    if hi < len(market.days) and market.days[hi].strftime("%Y-%m-%d") == args.end:
        hi += 1
    orders = []
    for signal in range(lo - 1, hi - 6, 5):
        if signal < 0:
            continue
        for k in market.order["defensive"][signal, :15]:
            if np.isfinite(market.scores["defensive"][signal, k]):
                orders.append((signal + 1, k))
    symbols = sorted({market.symbols[k] for _, k in orders})
    print(f"{len(orders)} fixed entry opportunities, {len(symbols)} symbols", flush=True)
    errors = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        loaded = {}
        for symbol, (bars, error) in zip(symbols, pool.map(lambda s: load_symbol(s, args.fetch), symbols)):
            loaded[symbol] = bars
            if error:
                errors[symbol] = error

    rows = []
    excluded = defaultdict(int)
    for i, k in orders:
        symbol = market.symbols[k]
        series = loaded[symbol]
        previous, entry, exit_day = (market.days[j].strftime("%Y-%m-%d") for j in (i - 1, i, i + 5))
        prev_bar = series.get((previous, "15:00"))
        bars = [series.get((entry, slot)) for slot in SLOTS]
        if (not prev_bar or prev_bar["close"] <= 0 or
                not all(b and b["low"] > 0 and b["volume"] > 0 for b in bars)):
            excluded["missing_or_incomplete_intraday"] += 1
            continue
        limit = .195 if symbol.startswith("30") else .095
        if market.risk_flag[i, k] or market.risk_flag[i - 1, k]:
            excluded["risk_flag"] += 1
            continue
        reference = prev_bar["close"]
        close = bars[-1]["close"]
        if not market.traded[i, k] or not buyable(close, reference, limit):
            excluded["close_unavailable_or_near_limit"] += 1
            continue
        if not market.sellable[i + 5, k]:
            excluded["common_exit_unavailable"] += 1
            continue
        # Rebase the raw intraday entry to the adjusted entry-day close for common exit accounting.
        exit_ratio = float(market.prices[i + 5, k] / market.prices[i, k])
        net = lambda price: exit_ratio * (close / price) * .998 / 1.002 - 1
        pick = rebound_entry(bars, reference, limit)
        price = pick["price"] if pick else close
        first = bars[0]["open"]
        rows.append({"symbol": symbol, "entry_date": entry, "exit_date": exit_day,
                     "signal": pick["signal"] if pick else None,
                     "fill_bar_end": pick["fill_bar_end"] if pick else None,
                     "entry_price": price, "close_price": close, "price_saving": 1 - price / close,
                     "close_return": net(close), "rebound_return": net(price),
                     "open_return": net(first) if buyable(first, reference, limit) else None})
    payload = {"start": args.start, "end": args.end, "planned": len(orders), "symbols": len(symbols),
               "errors": errors, "excluded": dict(excluded), "base": summarize(rows, 0),
               "stress": summarize(rows, .001),
               "early": summarize([r for r in rows if r["entry_date"] < "2026-10-01"], 0),
               "late": summarize([r for r in rows if r["entry_date"] >= "2026-10-01"], 0),
               "orders": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"{len(rows)}/{len(orders)} matched; {len(errors)} download errors", flush=True)


if __name__ == "__main__":
    main()
