import unittest
from datetime import date

from market_data.tushare import AShareDailySync, Security


class FakeClient:
    def query(self, api_name, params=None, fields=()):
        if api_name == "daily":
            return [
                {"ts_code": "600000.SH", "trade_date": "20240102", "close": 10.0, "vol": 100.0},
                {"ts_code": "688001.SH", "trade_date": "20240102", "close": 20.0, "vol": 200.0},
            ]
        if api_name == "adj_factor":
            return [
                {"ts_code": "600000.SH", "adj_factor": 2.0},
                {"ts_code": "688001.SH", "adj_factor": 1.0},
            ]
        raise AssertionError(api_name)


class MarketDataTest(unittest.TestCase):
    def test_adjusts_price_and_keeps_authorized_universe(self):
        sync = AShareDailySync(FakeClient())
        securities = {
            "600000.SH": Security("600000.SH", "浦发银行", date(1999, 11, 10), "main")
        }
        rows = sync.rows_for_date(date(2024, 1, 2), securities)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "600000.SH")
        self.assertEqual(rows[0]["close"], 10.0)
        self.assertEqual(rows[0]["adj_factor"], 2.0)
        self.assertEqual(rows[0]["volume"], 10000.0)


if __name__ == "__main__":
    unittest.main()
