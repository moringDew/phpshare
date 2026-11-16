"""Local 15-minute and live-quote cache. Fetch once, reuse without re-requesting."""
from __future__ import annotations

import csv
import json
import urllib.request
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "intraday"
BAR_DIR = DATA / "15m"
QUOTE_DIR = DATA / "quotes"
NAME_CACHE = ROOT / "output" / "name_cache.json"

SINA_URL = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
            "?symbol={symbol}&scale=15&ma=no&datalen={count}")
BAR_FIELDS = ["date", "time", "open", "high", "low", "close", "volume", "amount"]
QUOTE_FIELDS = ["slot", "symbol", "name", "price", "previous_close", "pct_change",
                "volume", "amount", "limit_up", "limit_down", "quote_date", "quote_time"]


def _read_rows(path: Path, fields: Sequence[str]) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, fields: Sequence[str], rows: Iterable[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def load_15m(symbol: str) -> Dict[Tuple[str, str], Dict[str, float]]:
    path = BAR_DIR / "{}.csv".format(symbol)
    rows = _read_rows(path, BAR_FIELDS)
    result: Dict[Tuple[str, str], Dict[str, float]] = {}
    for row in rows:
        try:
            result[(row["date"], row["time"])] = {
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row["volume"]), "amount": float(row["amount"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return result


def save_15m(symbol: str, bars: Mapping[Tuple[str, str], Mapping[str, float]]) -> None:
    merged = load_15m(symbol)
    for key, value in bars.items():
        merged[key] = dict(value)
    path = BAR_DIR / "{}.csv".format(symbol)
    rows = [
        {"date": day, "time": slot, **{field: value[field] for field in ("open", "high", "low", "close", "volume", "amount")}}
        for (day, slot), value in sorted(merged.items())
    ]
    _write_rows(path, BAR_FIELDS, rows)


def covers(symbol: str, start: date, end: date) -> bool:
    bars = load_15m(symbol)
    if not bars:
        return False
    days = {day for day, _ in bars}
    return min(days) <= start.isoformat() and max(days) >= end.isoformat()


def fetch_sina_15m(symbol: str, count: int = 1000) -> Dict[Tuple[str, str], Dict[str, float]]:
    code, exchange = symbol.split(".")
    sina_symbol = ("sh" if exchange == "SH" else "sz") + code
    url = SINA_URL.format(symbol=sina_symbol, count=count)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 quant-research/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("新浪 15 分钟数据格式异常")
    result: Dict[Tuple[str, str], Dict[str, float]] = {}
    for item in payload:
        stamp = str(item.get("day", ""))
        if len(stamp) < 16:
            continue
        day, slot = stamp[:10], stamp[11:16]
        try:
            result[(day, slot)] = {
                "open": float(item["open"]), "high": float(item["high"]),
                "low": float(item["low"]), "close": float(item["close"]),
                "volume": float(item["volume"]), "amount": float(item["amount"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return result


def get_15m(symbol: str, start: date, end: date, count: int = 1000) -> Dict[Tuple[str, str], Dict[str, float]]:
    """Return local bars for the range; fetch from Sina only if the cache is incomplete."""
    if covers(symbol, start, end):
        return load_15m(symbol)
    fetched = fetch_sina_15m(symbol, count=count)
    if fetched:
        save_15m(symbol, fetched)
    return load_15m(symbol)


def save_quotes(day: str, slot: str, quotes: Mapping[str, object]) -> Path:
    """Append one live quote snapshot per symbol into data/intraday/quotes/YYYY-MM-DD.csv."""
    path = QUOTE_DIR / "{}.csv".format(day)
    rows = _read_rows(path, QUOTE_FIELDS)
    merged = {(row["slot"], row["symbol"]): row for row in rows}
    for symbol, quote in quotes.items():
        merged[(slot, symbol)] = {
            "slot": slot, "symbol": symbol, "name": str(getattr(quote, "name", symbol)),
            "price": getattr(quote, "price", 0.0),
            "previous_close": getattr(quote, "previous_close", 0.0),
            "pct_change": getattr(quote, "pct_change", 0.0),
            "volume": getattr(quote, "volume", 0.0),
            "amount": getattr(quote, "price", 0.0) * getattr(quote, "volume", 0.0) * 100.0,
            "limit_up": getattr(quote, "limit_up", 0.0),
            "limit_down": getattr(quote, "limit_down", 0.0),
            "quote_date": getattr(quote, "quote_date", ""),
            "quote_time": getattr(quote, "quote_time", ""),
        }
    ordered = [merged[key] for key in sorted(merged)]
    _write_rows(path, QUOTE_FIELDS, ordered)
    return path


def load_name_cache() -> Dict[str, str]:
    if not NAME_CACHE.exists():
        return {}
    try:
        return json.loads(NAME_CACHE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_name_cache(cache: Mapping[str, str]) -> None:
    NAME_CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary = NAME_CACHE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(dict(cache), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(NAME_CACHE)
