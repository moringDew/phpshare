import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import midday_check


def make_payload(price, previous, limit_up, limit_down, ask1=0, volume=1000,
                 timestamp="20261114113000", name="测试股", code="600977"):
    parts = [""] * 88
    parts[1] = name
    parts[2] = code
    parts[3] = str(price)
    parts[4] = str(previous)
    parts[6] = str(volume)
    parts[20] = str(ask1)
    parts[30] = timestamp
    parts[32] = "{:.2f}".format((price / previous - 1.0) * 100.0) if previous else "0"
    parts[33] = str(price)
    parts[34] = str(previous)
    parts[47] = str(limit_up)
    parts[48] = str(limit_down)
    return "~".join(parts)


class MiddayCheckTest(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 11, 14)

    def test_parse_quote(self):
        quote = midday_check.parse_quote(
            "600977.SH",
            make_payload(17.05, 15.50, 17.05, 13.95, ask1=0),
        )
        self.assertIsNotNone(quote)
        self.assertEqual(quote.symbol, "600977.SH")
        self.assertEqual(quote.name, "测试股")
        self.assertAlmostEqual(quote.price, 17.05)
        self.assertAlmostEqual(quote.limit_up, 17.05)
        self.assertEqual(quote.quote_date, "2026-11-14")
        self.assertEqual(quote.quote_time, "11:30:00")

    def test_buy_at_limit_up_is_skipped(self):
        order = {"side": "BUY", "symbol": "600977.SH", "shares": 800, "reference_price": 15.50}
        quote = midday_check.parse_quote("600977.SH", make_payload(17.05, 15.50, 17.05, 13.95, ask1=0))
        row = midday_check.classify_order(order, quote, self.today)
        self.assertEqual(row["action"], "skip")
        self.assertEqual(row["label"], "涨停封板")
        self.assertIn("不追涨停", row["advice"])

    def test_sell_at_limit_down_is_deferred(self):
        order = {"side": "SELL", "symbol": "600977.SH", "shares": 800, "reference_price": 15.50}
        quote = midday_check.parse_quote("600977.SH", make_payload(13.95, 15.50, 17.05, 13.95))
        row = midday_check.classify_order(order, quote, self.today)
        self.assertEqual(row["action"], "defer")
        self.assertIn("延后", row["advice"])

    def test_normal_buy_and_limit_up_sell_are_executable(self):
        order = {"side": "BUY", "symbol": "600977.SH", "shares": 800, "reference_price": 15.50}
        quote = midday_check.parse_quote("600977.SH", make_payload(15.80, 15.50, 17.05, 13.95))
        row = midday_check.classify_order(order, quote, self.today)
        self.assertEqual(row["action"], "execute")
        self.assertEqual(row["label"], "正常")

        sell = {"side": "SELL", "symbol": "600977.SH", "shares": 800, "reference_price": 15.50}
        limit_up_quote = midday_check.parse_quote("600977.SH", make_payload(17.05, 15.50, 17.05, 13.95))
        sell_row = midday_check.classify_order(sell, limit_up_quote, self.today)
        self.assertEqual(sell_row["action"], "execute")
        self.assertEqual(sell_row["label"], "涨停")

    def test_stale_quote_is_not_executable(self):
        order = {"side": "BUY", "symbol": "600977.SH", "shares": 800, "reference_price": 15.50}
        quote = midday_check.parse_quote(
            "600977.SH",
            make_payload(17.05, 15.50, 17.05, 13.95, timestamp="20261113161452"),
        )
        row = midday_check.classify_order(order, quote, self.today)
        self.assertEqual(row["action"], "no_quote")
        self.assertEqual(row["state"], "no_quote")

    def test_build_result_and_markdown(self):
        alert = {
            "signal_date": "2026-11-13",
            "orders": [
                {"side": "BUY", "symbol": "600977.SH", "shares": 800, "reference_price": 15.50},
                {"side": "SELL", "symbol": "600887.SH", "shares": 400, "reference_price": 28.00},
            ],
        }
        quotes = {
            "600977.SH": midday_check.parse_quote(
                "600977.SH", make_payload(17.05, 15.50, 17.05, 13.95, name="中国电影")),
            "600887.SH": midday_check.parse_quote(
                "600887.SH", make_payload(28.10, 27.80, 30.58, 25.02, name="伊利股份")),
        }
        result = midday_check.build_result(alert, quotes, self.today)
        self.assertEqual(result["summary"]["buy"], 1)
        self.assertEqual(result["summary"]["sell"], 1)
        self.assertEqual(result["summary"]["skip"], 1)
        self.assertEqual(result["summary"]["execute"], 1)
        text = midday_check.format_markdown(result)
        self.assertIn("午间执行复核 2026-11-14", text)
        self.assertIn("中国电影", text)
        self.assertIn("跳过", text)
        self.assertIn("伊利股份", text)


if __name__ == "__main__":
    unittest.main()
