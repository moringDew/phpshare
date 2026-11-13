from __future__ import annotations

import csv
import io
import re
import struct
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from stock_selection.engine import infer_board


DAY_RECORD = struct.Struct("<IIIIIfII")
DAY_PATH = re.compile(r"^(sh|sz)/lday/(sh|sz)(\d{6})\.day$", re.IGNORECASE)


@dataclass(frozen=True)
class TdxDay:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    amount: float
    volume: int


def parse_day_records(payload: bytes) -> Iterator[TdxDay]:
    if len(payload) % DAY_RECORD.size:
        raise ValueError("通达信 day 文件长度不是32字节的整数倍")
    for values in DAY_RECORD.iter_unpack(payload):
        raw_date, raw_open, raw_high, raw_low, raw_close, amount, volume, _ = values
        text = str(raw_date)
        if len(text) != 8:
            continue
        trade_date = datetime.strptime(text, "%Y%m%d").date()
        if min(raw_open, raw_high, raw_low, raw_close) <= 0:
            continue
        yield TdxDay(
            trade_date=trade_date,
            open=raw_open / 100.0,
            high=raw_high / 100.0,
            low=raw_low / 100.0,
            close=raw_close / 100.0,
            amount=float(amount),
            volume=int(volume),
        )


def eligible_day_entry(path: str) -> Optional[Tuple[str, str]]:
    normalized = path.replace("\\", "/")
    match = DAY_PATH.match(normalized)
    if not match:
        return None
    exchange, _, code = match.groups()
    symbol = "{}.{}".format(code, exchange.upper())
    board = infer_board(symbol)
    if board not in {"main", "chinext"}:
        return None
    return symbol, board


def convert_tdx_zip(zip_path: Path, output: Path, start_date: date = date(2023, 1, 1)) -> Dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    securities = 0
    rows = 0
    earliest: Optional[date] = None
    latest: Optional[date] = None
    fields = ("date", "symbol", "close", "volume", "adj_factor", "listing_days", "is_st", "suspended")
    with zipfile.ZipFile(str(zip_path), "r") as archive, output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for entry in archive.infolist():
            identity = eligible_day_entry(entry.filename)
            if identity is None:
                continue
            symbol, _ = identity
            records = list(parse_day_records(archive.read(entry)))
            if not records:
                continue
            securities += 1
            listing_date = records[0].trade_date
            for record in records:
                if record.trade_date < start_date:
                    continue
                writer.writerow(
                    {
                        "date": record.trade_date.isoformat(),
                        "symbol": symbol,
                        "close": record.close,
                        "volume": record.volume,
                        "adj_factor": 1.0,
                        "listing_days": (record.trade_date - listing_date).days,
                        "is_st": 0,
                        "suspended": 0,
                    }
                )
                rows += 1
                earliest = record.trade_date if earliest is None else min(earliest, record.trade_date)
                latest = record.trade_date if latest is None else max(latest, record.trade_date)
    return {
        "source": "tdx_official_personal_daily_package",
        "zip": str(zip_path),
        "output": str(output),
        "securities": securities,
        "rows": rows,
        "start_date": earliest.isoformat() if earliest else None,
        "end_date": latest.isoformat() if latest else None,
        "adjusted": False,
    }

