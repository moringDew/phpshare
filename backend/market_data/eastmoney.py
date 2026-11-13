from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from stock_selection.engine import infer_board


LIST_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


@dataclass(frozen=True)
class PublicSecurity:
    symbol: str
    name: str
    amount: float
    price: float


def _get_json(url: str, params: Dict[str, object], timeout: int = 30, retries: int = 3) -> Dict[str, object]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(url + "?" + query, headers={"User-Agent": "Mozilla/5.0 quant-research/1.0"})
    error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("rc") != 0:
                raise RuntimeError("行情接口返回 rc={}".format(payload.get("rc")))
            return payload
        except Exception as exc:  # network and upstream errors share retry policy
            error = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("公开行情请求失败: {}".format(error))


def fetch_liquid_universe(limit: int = 500) -> List[PublicSecurity]:
    securities: List[PublicSecurity] = []
    page = 1
    while len(securities) < limit:
        payload = _get_json(
            LIST_URL,
            {
                "pn": page,
                "pz": 100,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f6",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2",
                "fields": "f12,f14,f2,f6",
            },
        )
        rows = ((payload.get("data") or {}).get("diff") or [])
        if not rows:
            break
        for row in rows:
            code = str(row.get("f12") or "")
            exchange = "SH" if code.startswith(("600", "601", "603", "605")) else "SZ"
            symbol = "{}.{}".format(code, exchange)
            name = str(row.get("f14") or "")
            if infer_board(symbol) not in {"main", "chinext"} or "ST" in name.upper() or "退" in name:
                continue
            try:
                amount = float(row.get("f6") or 0)
                price = float(row.get("f2") or 0)
            except (TypeError, ValueError):
                continue
            if amount <= 0 or price <= 0:
                continue
            securities.append(PublicSecurity(symbol, name, amount, price))
            if len(securities) >= limit:
                break
        page += 1
    securities.sort(key=lambda item: (-item.amount, item.symbol))
    return securities[:limit]


def fetch_history(security: PublicSecurity, bars: int = 756) -> List[Dict[str, object]]:
    code, exchange = security.symbol.split(".")
    secid = ("1." if exchange == "SH" else "0.") + code
    payload = _get_json(
        KLINE_URL,
        {
            "secid": secid,
            "klt": 101,
            "fqt": 1,
            "lmt": bars,
            "end": 20500101,
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
    )
    klines = ((payload.get("data") or {}).get("klines") or [])
    parsed: List[Dict[str, object]] = []
    first_date: Optional[date] = None
    for raw in klines:
        parts = str(raw).split(",")
        if len(parts) < 7:
            continue
        trade_date = date.fromisoformat(parts[0])
        first_date = first_date or trade_date
        parsed.append(
            {
                "date": trade_date.isoformat(),
                "symbol": security.symbol,
                "close": float(parts[2]),
                "volume": float(parts[5]) * 100.0,
                "adj_factor": 1.0,
                "listing_days": (trade_date - first_date).days,
                "is_st": 0,
                "suspended": 0,
            }
        )
    return parsed


def sync_public_history(output: Path, universe_size: int = 500, bars: int = 756, workers: int = 12) -> Dict[str, object]:
    securities = fetch_liquid_universe(universe_size)
    all_rows: List[Dict[str, object]] = []
    failures: List[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_history, security, bars): security for security in securities}
        for future in as_completed(futures):
            security = futures[future]
            try:
                all_rows.extend(future.result())
            except Exception:
                failures.append(security.symbol)
    all_rows.sort(key=lambda row: (str(row["date"]), str(row["symbol"])))
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = ("date", "symbol", "close", "volume", "adj_factor", "listing_days", "is_st", "suspended")
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)
    dates = [str(row["date"]) for row in all_rows]
    return {
        "source": "public_eastmoney_research_fallback",
        "requested_securities": len(securities),
        "successful_securities": len(securities) - len(failures),
        "failed_securities": failures,
        "rows": len(all_rows),
        "start_date": min(dates) if dates else None,
        "end_date": max(dates) if dates else None,
        "output": str(output),
    }

