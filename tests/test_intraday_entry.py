import unittest

from scripts.research_intraday_entry import rebound_entry


class IntradayExecutionTests(unittest.TestCase):
    def test_rebound_fills_next_open_without_seeing_later_low(self):
        bars = [
            {"low": 98.5, "close": 98.7, "open": 100.0},
            {"low": 98.4, "close": 99.1, "open": 98.7},
            {"low": 94.0, "close": 94.5, "open": 99.2},
        ]
        self.assertEqual(
            rebound_entry(bars, 100, .095),
            {"signal": "10:00", "fill_bar_end": "10:15", "price": 99.2},
        )
        self.assertIsNone(rebound_entry(bars[:2], 100, .095))

    def test_open_above_order_limit_does_not_fill(self):
        bars = [
            {"low": 98.5, "close": 98.7},
            {"low": 98.4, "close": 99.1},
            {"low": 98.0, "close": 98.2, "open": 100.5},
        ]
        self.assertIsNone(rebound_entry(bars, 100, .095))
