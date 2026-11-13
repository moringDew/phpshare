import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from stock_selection.engine import SelectionConfig, StockSelector, infer_board, load_bars


class StockSelectionTest(unittest.TestCase):
    def _write_fixture(self, path: Path, days: int = 150) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "symbol", "close", "volume", "listing_days", "is_st", "suspended"])
            start = date(2024, 1, 1)
            for i in range(days):
                day = start + timedelta(days=i)
                writer.writerow([day, "000001.SZ", 10.0 * (1.002 ** i), 5_000_000, 300 + i, 0, 0])
                writer.writerow([day, "600000.SH", 10.0 * (1.0002 ** i), 5_000_000, 300 + i, 0, 0])
                writer.writerow([day, "600001.SH", 10.0 * (1.004 ** i), 5_000_000, 300 + i, 1, 0])

    def test_selector_filters_st_and_ranks_momentum(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            self._write_fixture(path)
            bars = load_bars(path)
            config = SelectionConfig(top_n=1, minimum_average_turnover=1.0)
            selector = StockSelector(bars, config)
            selected = selector.select(selector.calendar[-1])
            self.assertEqual([item.symbol for item in selected], ["000001.SZ"])

    def test_backtest_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            self._write_fixture(path, 180)
            bars = load_bars(path)
            config = SelectionConfig(top_n=1, minimum_average_turnover=1.0, rebalance_days=10)
            first = StockSelector(bars, config).backtest()
            second = StockSelector(bars, config).backtest()
            self.assertAlmostEqual(first.final_equity, second.final_equity, places=12)
            self.assertLessEqual(first.maximum_drawdown, 0.001)

    def test_duplicate_rows_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            path.write_text("date,symbol,close,volume\n2024-01-01,AAA.SZ,10,1\n2024-01-01,AAA.SZ,10,1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重复行情"):
                load_bars(path)

    def test_future_prices_do_not_change_past_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            self._write_fixture(path, 150)
            bars = load_bars(path)
            config = SelectionConfig(top_n=1, minimum_average_turnover=1.0)
            selector = StockSelector(bars, config)
            target_date = selector.calendar[130]
            before = [item.symbol for item in selector.select(target_date)]
            changed = [
                type(bar)(
                    trade_date=bar.trade_date,
                    symbol=bar.symbol,
                    close=(bar.close * 100 if bar.trade_date > target_date and bar.symbol == "600000.SH" else bar.close),
                    volume=bar.volume,
                    adj_factor=bar.adj_factor,
                    is_st=bar.is_st,
                    suspended=bar.suspended,
                    listing_days=bar.listing_days,
                    roe=bar.roe,
                    revenue_growth=bar.revenue_growth,
                    debt_ratio=bar.debt_ratio,
                )
                for bar in bars
            ]
            after = [item.symbol for item in StockSelector(changed, config).select(target_date)]
            self.assertEqual(before, after)

    def test_board_permissions(self):
        self.assertEqual(infer_board("600000.SH"), "main")
        self.assertEqual(infer_board("002001.SZ"), "main")
        self.assertEqual(infer_board("300001.SZ"), "chinext")
        self.assertIsNone(infer_board("688001.SH"))
        self.assertIsNone(infer_board("830001.BJ"))

    def test_order_plan_respects_lots_and_position_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            self._write_fixture(path, 150)
            config = SelectionConfig(top_n=2, minimum_average_turnover=1.0, minimum_market_breadth=0.0)
            selector = StockSelector(load_bars(path), config)
            plan = selector.build_order_plan(selector.calendar[-1], 100_000)
            self.assertTrue(plan["risk_on"])
            self.assertLessEqual(plan["invested"], 45_000)
            for order in plan["orders"]:
                self.assertEqual(order["shares"] % 100, 0)
                self.assertLessEqual(order["amount"], 6_000)


if __name__ == "__main__":
    unittest.main()
