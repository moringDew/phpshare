from __future__ import annotations

import csv
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from stock_selection.engine import infer_board


API_URL = "https://api.tushare.pro"


class TushareError(RuntimeError):
    pass


class TushareClient:
    def __init__(self, token: str, minimum_interval: float = 1.3, timeout: int = 30):
        if not token.strip():
            raise ValueError("Tushare Token 不能为空")
        self.token = token.strip()
        self.minimum_interval = max(0.0, minimum_interval)
        self.timeout = timeout
        self._last_request = 0.0

    @classmethod
    def from_environment(cls) -> "TushareClient":
        token = os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            raise TushareError("未设置环境变量 TUSHARE_TOKEN")
        return cls(token)

    def query(self, api_name: str, params: Optional[Mapping[str, object]] = None, fields: Sequence[str] = ()) -> List[Dict[str, object]]:
        delay = self.minimum_interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        payload = json.dumps(
            {"api_name": api_name, "token": self.token, "params": dict(params or {}), "fields": ",".join(fields)},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(API_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TushareError("Tushare 请求失败: {}".format(exc)) from exc
        finally:
            self._last_request = time.monotonic()
        if body.get("code") != 0:
            raise TushareError("Tushare 返回错误 {}: {}".format(body.get("code"), body.get("msg")))
        data = body.get("data") or {}
        names = data.get("fields") or []
        return [dict(zip(names, item)) for item in (data.get("items") or [])]


@dataclass(frozen=True)
class Security:
    ts_code: str
    name: str
    list_date: date
    board: str


class AShareDailySync:
    def __init__(self, client: TushareClient, allowed_boards: Sequence[str] = ("main", "chinext")):
        self.client = client
        self.allowed_boards = set(allowed_boards)

    def securities(self) -> Dict[str, Security]:
        rows = self.client.query(
            "stock_basic",
            {"exchange": "", "list_status": "L"},
            ("ts_code", "name", "list_date"),
        )
        result: Dict[str, Security] = {}
        for row in rows:
            ts_code = str(row["ts_code"]).upper()
            board = infer_board(ts_code)
            if board not in self.allowed_boards:
                continue
            raw_date = str(row.get("list_date") or "")
            if len(raw_date) != 8:
                continue
            result[ts_code] = Security(
                ts_code=ts_code,
                name=str(row.get("name") or ""),
                list_date=datetime.strptime(raw_date, "%Y%m%d").date(),
                board=board,
            )
        return result

    def open_dates(self, start: date, end: date) -> List[date]:
        rows = self.client.query(
            "trade_cal",
            {"exchange": "SSE", "start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d"), "is_open": "1"},
            ("cal_date", "is_open"),
        )
        return sorted(datetime.strptime(str(row["cal_date"]), "%Y%m%d").date() for row in rows if str(row.get("is_open")) == "1")

    def rows_for_date(self, trade_date: date, securities: Mapping[str, Security]) -> List[Dict[str, object]]:
        day = trade_date.strftime("%Y%m%d")
        daily = self.client.query("daily", {"trade_date": day}, ("ts_code", "trade_date", "close", "vol"))
        factors = self.client.query("adj_factor", {"trade_date": day}, ("ts_code", "adj_factor"))
        factor_by_symbol = {str(row["ts_code"]).upper(): float(row["adj_factor"]) for row in factors}
        result: List[Dict[str, object]] = []
        for row in daily:
            symbol = str(row["ts_code"]).upper()
            security = securities.get(symbol)
            factor = factor_by_symbol.get(symbol)
            if security is None or factor is None or trade_date < security.list_date:
                continue
            result.append(
                {
                    "date": trade_date.isoformat(),
                    "symbol": symbol,
                    "close": float(row["close"]),
                    "adj_factor": factor,
                    "volume": float(row["vol"]) * 100.0,
                    "listing_days": (trade_date - security.list_date).days,
                    "is_st": int("ST" in security.name.upper()),
                    "suspended": 0,
                }
            )
        return result

    def sync(self, start: date, end: date, output: Path) -> Dict[str, object]:
        if end < start:
            raise ValueError("结束日期不能早于开始日期")
        securities = self.securities()
        dates = self.open_dates(start, end)
        output.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=("date", "symbol", "close", "volume", "adj_factor", "listing_days", "is_st", "suspended"),
            )
            writer.writeheader()
            for trade_date in dates:
                rows = self.rows_for_date(trade_date, securities)
                writer.writerows(rows)
                count += len(rows)
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "trading_days": len(dates),
            "securities": len(securities),
            "rows": count,
            "output": str(output),
        }

