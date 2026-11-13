import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from stock_selection.real_account import add_trade, snapshot, void_trade, reconcile_message


class RealAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'trades.sqlite3'
        self.addCleanup(self.temp.cleanup)

    def record(self, **overrides):
        values = dict(side='BUY', symbol='600000', traded_at='2026-01-05 14:50', price='10', amount='10000', fee='5')
        values.update(overrides)
        return add_trade(self.path, **values)

    def test_weighted_cost_and_partial_sale(self):
        self.record()
        self.record(traded_at='2026-01-06 14:50', price='12', amount='6000')
        self.record(side='SELL', traded_at='2026-01-07 14:50', price='15', amount='9000')
        account = snapshot(self.path)
        self.assertEqual(account['holdings'][0]['shares'], 900)
        self.assertEqual(Decimal(account['holdings'][0]['cost']), Decimal('9606'))
        self.assertEqual(Decimal(account['realized_pnl']), Decimal('2591'))
        self.assertIsNone(account['account_cash'])

    def test_fee_inclusive_amount_and_unknown_fees(self):
        self.record(amount='10005', amount_includes_fees=True)
        account = snapshot(self.path)
        self.assertEqual(account['holdings'][0]['shares'], 1000)
        self.assertEqual(Decimal(account['holdings'][0]['average_cost']), Decimal('10.005'))
        self.record(side='SELL', traded_at='2026-01-06 14:50', fee=None)
        account = snapshot(self.path)
        self.assertEqual(account['unknown_fee_count'], 1)
        self.assertEqual(account['holdings'], [])

    def test_duplicate_rejected(self):
        self.record()
        with self.assertRaisesRegex(ValueError, '重复'):
            self.record()
        self.assertEqual(snapshot(self.path)['trade_count'], 1)

    def test_invalid_amount_or_oversell_never_saved(self):
        with self.assertRaises(ValueError):
            self.record(price='12', amount='10000')
        with self.assertRaises(ValueError):
            self.record(side='SELL')
        self.assertEqual(snapshot(self.path)['trade_count'], 0)

    def test_void_retains_valid_inventory(self):
        trade_id = self.record()
        sell_id = self.record(side='SELL', traded_at='2026-01-06 14:50')
        with self.assertRaises(ValueError):
            void_trade(self.path, trade_id, '误录')
        void_trade(self.path, sell_id, '误录')
        void_trade(self.path, trade_id, '误录')
        self.assertEqual(snapshot(self.path)['trade_count'], 0)

    def test_historical_snapshot_does_not_include_future_fills(self):
        self.record()
        self.record(side='SELL', traded_at='2026-01-06 14:50')
        self.assertEqual(snapshot(self.path, '2026-01-05')['holdings'][0]['shares'], 1000)

    def test_reconciliation_does_not_create_real_orders(self):
        self.record()
        account = snapshot(self.path)
        text = reconcile_message(account, {'orders': [dict(side='SELL', symbol='600000.SH', shares=1500)]})
        self.assertIn('真实台账持有 1000 股', text)
        self.assertEqual(snapshot(self.path)['holdings'][0]['shares'], 1000)

    def test_future_execution_and_fee_ambiguity_rejected(self):
        with self.assertRaisesRegex(ValueError, '未来'):
            self.record(traded_at='2099-01-01 09:30')
        with self.assertRaisesRegex(ValueError, '手续费'):
            self.record(amount_includes_fees=True, fee=None)
        with self.assertRaises(ValueError):
            self.record(price='NaN')


if __name__ == '__main__':
    unittest.main()
