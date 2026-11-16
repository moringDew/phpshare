import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import realtime_preview
from stock_selection.engine import Bar


def make_bar(day, symbol, close, volume=1_000_000, suspended=False):
    return Bar(trade_date=day, symbol=symbol, close=close, volume=volume,
               adj_factor=1.0, amount=close * volume, is_st=False,
               suspended=suspended, listing_days=300)


def make_quote(symbol, price, volume=1000, quote_date="2026-11-16", quote_time="10:30:00"):
    return realtime_preview.Quote(
        symbol=symbol, name=symbol, price=price, previous_close=price / 1.01,
        pct_change=1.0, high=price, low=price, limit_up=round(price * 1.1, 2),
        limit_down=round(price * 0.9, 2), ask1_volume=100, volume=volume,
        quote_date=quote_date, quote_time=quote_time,
    )


class RealtimePreviewTest(unittest.TestCase):
    def test_trading_time(self):
        self.assertTrue(realtime_preview.is_trading_time(datetime(2026, 11, 16, 10, 0)))
        self.assertTrue(realtime_preview.is_trading_time(datetime(2026, 11, 16, 11, 30)))
        self.assertFalse(realtime_preview.is_trading_time(datetime(2026, 11, 16, 12, 0)))
        self.assertTrue(realtime_preview.is_trading_time(datetime(2026, 11, 16, 14, 45)))
        self.assertFalse(realtime_preview.is_trading_time(datetime(2026, 11, 16, 15, 1)))
        self.assertFalse(realtime_preview.is_trading_time(datetime(2026, 11, 14, 10, 0)))

    def test_build_provisional_bars_and_suspended(self):
        today = date(2026, 11, 16)
        history = [
            make_bar(date(2026, 11, 13), "600000.SH", 10.0),
            make_bar(date(2026, 11, 13), "600001.SH", 8.0),
        ]
        last_rows = {bar.symbol: bar for bar in history}
        quotes = {"600000.SH": make_quote("600000.SH", 11.0, volume=1000)}
        bars = realtime_preview.build_provisional_bars(history, last_rows, quotes, today)
        by_symbol = {bar.symbol: bar for bar in bars if bar.trade_date == today}
        self.assertEqual(by_symbol["600000.SH"].close, 11.0)
        self.assertEqual(by_symbol["600000.SH"].volume, 100_000)
        self.assertFalse(by_symbol["600000.SH"].suspended)
        self.assertTrue(by_symbol["600001.SH"].suspended)
        self.assertEqual(by_symbol["600001.SH"].volume, 0)

    def test_diff_picks(self):
        added, removed = realtime_preview.diff_picks(["A", "B"], ["B", "C"])
        self.assertEqual(added, ["C"])
        self.assertEqual(removed, ["A"])

    def test_format_message(self):
        result = {
            "date": "2026-11-16",
            "time": "10:30",
            "breadth": 0.512,
            "threshold": 0.5,
            "risk_on": True,
            "picks": [
                {"symbol": "600887.SH", "name": "伊利股份", "price": 31.8,
                 "pct_change": 0.2, "label": "正常"},
            ],
            "added": ["601398.SH"],
            "removed": [],
            "previous_time": "10:15",
            "separate": {"symbol": "600869.SH", "name": "远东股份", "cost": 13.48,
                         "stop": 12.4, "price": 13.48, "distance_to_stop_pct": 8.0},
        }
        text = realtime_preview.format_message(result)
        self.assertIn("实时预演 2026-11-16 10:30", text)
        self.assertIn("广度 51.20%", text)
        self.assertIn("门槛 >50%", text)
        self.assertIn("新增 601398.SH", text)
        self.assertIn("600869.SH", text)
        self.assertIn("止损 12.40", text)

    def test_empty_target_message(self):
        result = {
            "date": "2026-11-16", "time": "10:00", "breadth": 0.35, "risk_on": False,
            "picks": [], "added": [], "removed": [], "previous_time": "09:45",
            "separate": None,
        }
        text = realtime_preview.format_message(result)
        self.assertIn("空仓", text)
        self.assertIn("无变化", text)


if __name__ == "__main__":
    unittest.main()
