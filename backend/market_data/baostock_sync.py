from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from .tdx import DAY_RECORD, eligible_day_entry


@dataclass(frozen=True)
class SyncItem:
    symbol: str
    listing_date: date


def symbols_from_tdx(zip_path: Path) -> List[SyncItem]:
    items: List[SyncItem] = []
    with zipfile.ZipFile(str(zip_path), "r") as archive:
        for entry in archive.infolist():
            identity = eligible_day_entry(entry.filename)
            if identity is None or entry.file_size < DAY_RECORD.size:
                continue
            symbol, _ = identity
            with archive.open(entry, "r") as source:
                first = source.read(DAY_RECORD.size)
            raw_date = DAY_RECORD.unpack(first)[0]
            try:
                listing_date = datetime.strptime(str(raw_date), "%Y%m%d").date()
            except ValueError:
                continue
            items.append(SyncItem(symbol, listing_date))
    return sorted(items, key=lambda item: item.symbol)


def _baostock_code(symbol: str) -> str:
    code, exchange = symbol.split(".")
    return "{}.{}".format(exchange.lower(), code)


def _sync_worker(args: Tuple[int, Sequence[SyncItem], str, str]) -> Dict[str, object]:
    worker_id, items, output_text, start_date = args
    import baostock as bs

    output = Path(output_text)
    login = None
    for attempt in range(5):
        login = bs.login()
        if login.error_code == "0":
            break
        time.sleep(2.0 * (attempt + 1))
    if login is None or login.error_code != "0":
        raise RuntimeError("BaoStock 登录失败: {}".format(login.error_msg if login else "无响应"))
    completed = 0
    empty = 0
    failed: List[Dict[str, str]] = []
    rows = 0
    try:
        for item in items:
            target = output / (item.symbol.replace(".", "_") + ".parquet")
            marker = output / (item.symbol.replace(".", "_") + ".empty")
            if target.exists() or marker.exists():
                completed += 1
                continue
            query = bs.query_history_k_data_plus(
                _baostock_code(item.symbol),
                "date,code,close,volume,amount,adjustflag,tradestatus,isST",
                start_date=start_date,
                end_date="",
                frequency="d",
                adjustflag="2",
            )
            if query.error_code != "0":
                failed.append({"symbol": item.symbol, "error": query.error_msg})
                continue
            values = []
            while query.next():
                row = query.get_row_data()
                if not row[2] or not row[3]:
                    continue
                trade_date = date.fromisoformat(row[0])
                values.append(
                    {
                        "date": trade_date.isoformat(),
                        "symbol": item.symbol,
                        "close": float(row[2]),
                        "volume": float(row[3]),
                        "amount": float(row[4]) if row[4] else None,
                        "adj_factor": 1.0,
                        "listing_days": (trade_date - item.listing_date).days,
                        "is_st": int(row[7] == "1"),
                        "suspended": int(row[6] != "1"),
                    }
                )
            if not values:
                marker.write_text("no rows\n", encoding="utf-8")
                empty += 1
            else:
                frame = pd.DataFrame(values)
                temporary = target.with_suffix(".parquet.tmp")
                frame.to_parquet(temporary, index=False, compression="zstd")
                os.replace(str(temporary), str(target))
                rows += len(frame)
            completed += 1
    finally:
        bs.logout()
    return {"worker": worker_id, "completed": completed, "empty": empty, "rows": rows, "failed": failed}


def sync_baostock(
    tdx_zip: Path,
    output: Path,
    start_date: date = date(2023, 1, 1),
    workers: int = 4,
    limit: Optional[int] = None,
) -> Dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    items = symbols_from_tdx(tdx_zip)
    if limit is not None:
        items = items[:limit]
    worker_count = max(1, workers)
    shards = [items[index::worker_count] for index in range(worker_count)]
    tasks = [(index, shard, str(output), start_date.isoformat()) for index, shard in enumerate(shards)]
    if worker_count == 1:
        results = [_sync_worker(tasks[0])]
    else:
        with mp.get_context("spawn").Pool(worker_count) as pool:
            results = pool.map(_sync_worker, tasks)
    failures = [failure for result in results for failure in result["failed"]]
    parquet_files = list(output.glob("*.parquet"))
    empty_files = list(output.glob("*.empty"))
    return {
        "source": "baostock_free",
        "price_adjustment": "front_adjusted",
        "historical_st": True,
        "start_date": start_date.isoformat(),
        "requested_securities": len(items),
        "parquet_files": len(parquet_files),
        "empty_securities": len(empty_files),
        "new_rows": sum(int(result["rows"]) for result in results),
        "failures": failures,
        "output": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="同步免费前复权A股行情和历史ST状态")
    parser.add_argument("--tdx-zip", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2023, 1, 1))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    result = sync_baostock(args.tdx_zip, args.output, args.start, args.workers, args.limit)
    manifest = args.output.parent / "manifest.json"
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["failures"] else 2


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
