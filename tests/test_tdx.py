import struct
import unittest
from datetime import date

from market_data.tdx import DAY_RECORD, eligible_day_entry, parse_day_records


class TdxTest(unittest.TestCase):
    def test_parse_day_record(self):
        payload = DAY_RECORD.pack(20261110, 1001, 1050, 990, 1025, 123456.0, 78900, 0)
        row = list(parse_day_records(payload))[0]
        self.assertEqual(row.trade_date, date(2026, 11, 10))
        self.assertEqual(row.open, 10.01)
        self.assertEqual(row.close, 10.25)
        self.assertEqual(row.volume, 78900)

    def test_only_main_and_chinext_are_accepted(self):
        self.assertEqual(eligible_day_entry("sh/lday/sh600000.day"), ("600000.SH", "main"))
        self.assertEqual(eligible_day_entry("sz/lday/sz300001.day"), ("300001.SZ", "chinext"))
        self.assertIsNone(eligible_day_entry("sh/lday/sh688001.day"))
        self.assertIsNone(eligible_day_entry("bj/lday/bj830001.day"))


if __name__ == "__main__":
    unittest.main()
