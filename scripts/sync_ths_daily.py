"""Download THS (同花顺) qfq daily bars once and cache them locally."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
BASE_CSV = ROOT / "data" / "a_share_public_latest.csv"
CACHE_DIR = ROOT / "data" / "ths_daily"
COMBINED = ROOT / "data" / "ths_daily_combined.csv"
URL = "https://d.10jqka.com.cn/v6/line/hs_{code}/01/all.js"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://stockpage.10jqka.com.cn/"}


def fetch_one(symbol: str) -> Tuple[str, List[Dict[str, object]], Optional[str]]:
    code = symbol.split(".")[0]
    url = URL.format(code=code)
    error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=20) as response:
                text = response.read().decode("utf-8", "replace")
            match = re.search(r"\((.*)\)\s*;?\s*$", text, re.S)
            if not match:
                raise ValueError("JSONP 格式异常")
            payload = json.loads(match.group(1))
            dates = str(payload["dates"]).split(",")
            prices = str(payload["price"]).split(",")
            volumes = str(payload["volumn"]).split(",")
            years = payload["sortYear"]
            rows = []
            cursor = 0
            index = 0
            for year, count in years:
                for month_day in dates[cursor : cursor + count]:
                    group = prices[index * 4 : index * 4 + 4]
                    volume = volumes[index] if index < len(volumes) else ""
                    index += 1
                    if len(group) < 4 or not group[0] or not volume:
                        continue
                    try:
                        low = float(group[0]) / 100.0
                        open_price = low + float(group[1]) / 100.0
                        high = low + float(group[2]) / 100.0
                        close = low + float(group[3]) / 100.0
                        rows.append({
                            "date": "{}-{}-{}".format(year, month_day[:2], month_day[2:]),
                            "open": "{:.4f}".format(open_price),
                            "high": "{:.4f}".format(high),
                            "low": "{:.4f}".format(low),
                            "close": "{:.4f}".format(close),
                            "volume": "{:.0f}".format(float(volume)),
                        })
                    except ValueError:
                        continue
                cursor += count
            if rows:
                return symbol, rows, None
            error = "没有解析出数据"
        except Exception as exc:
            error = "{}: {}".format(type(exc).__name__, exc)
        time.sleep(0.4 * (attempt + 1))
    return symbol, [], error


def read_universe() -> List[str]:
    rows = list(csv.DictReader(BASE_CSV.open("r", encoding="utf-8-sig")))
    last_date = max(row["date"] for row in rows)
    return sorted({row["symbol"] for row in rows if row["date"] == last_date})


def main() -> int:
    parser = argparse.ArgumentParser(description="同花顺前复权日线本地缓存")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--force", action="store_true", help="忽略已有缓存，重新下载")
    args = parser.parse_args()

    universe = read_universe()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    missing = [symbol for symbol in universe if args.force or not (CACHE_DIR / "{}.csv".format(symbol)).exists()]
    print("universe", len(universe), "missing", len(missing), flush=True)

    failures = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch_one, symbol): symbol for symbol in missing}
        for future in as_completed(futures):
            symbol, rows, error = future.result()
            if rows:
                path = CACHE_DIR / "{}.csv".format(symbol)
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=["date", "open", "high", "low", "close", "volume"])
                    writer.writeheader()
                    writer.writerows(rows)
                done += 1
            else:
                failures.append("{} {}".format(symbol, error))
            if done % 50 == 0:
                print("done", done, "failures", len(failures), flush=True)

    files = sorted(CACHE_DIR.glob("*.csv"))
    print("cached", len(files), "failed", len(failures), flush=True)
    if failures:
        print("first failures", failures[:10], flush=True)

    # Combined selector CSV, keeping enough history for a 4-year backtest.
    with COMBINED.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "symbol", "close", "volume", "adj_factor", "listing_days", "is_st", "suspended"])
        for path in files:
            symbol = path.stem
            with path.open("r", encoding="utf-8-sig", newline="") as source:
                for row in csv.DictReader(source):
                    if row["date"] < "2021-12-01":
                        continue
                    writer.writerow([row["date"], symbol, row["close"], row["volume"],
                                     "1.0", "2000", "0", "0"])
    print("combined", COMBINED, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
