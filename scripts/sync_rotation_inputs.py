"""Download public index closes and dated industry snapshots via BaoStock."""
from __future__ import annotations

import argparse
import socket
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import baostock as bs
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "rotation"


def frame(result):
    if result.error_code != "0":
        raise RuntimeError(result.error_msg)
    return result.get_data()


def connect():
    socket.setdefaulttimeout(12)
    session = bs.login()
    if session.error_code != "0":
        raise RuntimeError(session.error_msg)


def fetch_snapshot(day):
    file = OUT / "snapshots" / f"{day}.csv"
    if file.exists():
        return day
    for attempt in range(3):
        query_date = (pd.Timestamp(day) - pd.Timedelta(days=attempt)).strftime("%Y-%m-%d")
        try:
            connect()
            try:
                result = frame(bs.query_stock_industry(date=query_date))
            finally:
                bs.logout()
            df = result[["updateDate", "code", "industry"]]
            valid = df.updateDate.str.fullmatch(r"\d{4}-\d{2}-\d{2}") & df.industry.ne("")
            df = df[valid & (df.updateDate <= query_date)].copy()
            if len(df) < 1000:
                raise ValueError(f"{day}: dated stock industry data missing or too sparse ({len(df)})")
            df["snapshot"] = query_date
            df.to_csv(file, index=False)
            print(day, query_date, len(df), df.updateDate.max(), flush=True)
            return day
        except RuntimeError as error:
            if attempt == 2:
                raise RuntimeError(f"{day}: unable to retrieve a dated snapshot: {error}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--recent", action="store_true", help="Download quarterly snapshots from 2024 year end")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    cache = OUT / "snapshots"
    cache.mkdir(parents=True, exist_ok=True)
    start, end = "2022-01-01", "2026-11-16"
    if not (OUT / "index_close.csv").exists() or args.probe:
        connect()
        try:
            indices = pd.concat([
                frame(bs.query_history_k_data_plus(code, "date,code,close", start_date=start, end_date=end))
                for code in ("sh.000300", "sh.000905")
            ])
        finally:
            bs.logout()
        print("Index observations:", indices.groupby("code").size().to_dict(), flush=True)
        if not args.probe:
            indices.to_csv(OUT / "index_close.csv", index=False)
    first = "2024-12-31" if args.recent else "2022-12-31"
    dates = ["2022-12-30", "2026-09-30"] if args.probe else [
        d.strftime("%Y-%m-%d") for d in pd.date_range(first, end, freq="QE-DEC")
    ]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        fetched = list(pool.map(fetch_snapshot, dates))
    if not args.probe:
        snapshots = [pd.read_csv(cache / f"{day}.csv") for day in fetched]
        target = "industry_recent.csv" if args.recent else "industry.csv"
        pd.concat(snapshots).to_csv(OUT / target, index=False)


if __name__ == "__main__":
    main()
