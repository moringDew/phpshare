import sys
import unittest
import tempfile
import io
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from stock_selection.capital import run_capital_backtest
from stock_selection.alerts import build_trade_alert, format_trade_alert
from stock_selection.engine import SelectionConfig, StockSelector, load_bars
import daily_breadth_report
import csv


class FixtureSelector:
    def __init__(self, prices, rebalance=20, breadth=1.0, limit_up=()):
        self.calendar = [date(2026, 1, 5) + timedelta(days=i) for i in range(len(prices))]
        self.limit_up = set(limit_up)
        self.by_date = {day: {'AAA': SimpleNamespace(close=price, suspended=False)}
                        for day, price in zip(self.calendar, prices)}
        self.config = SimpleNamespace(initial_capital=100000, maximum_equity_exposure=.4,
                                      maximum_single_position=.1, lot_size=100,
                                      cash_yield_annual=.015, transaction_cost_rate=.0015,
                                      rebalance_days=rebalance, minimum_market_breadth=.4,
                                      stop_loss_pct=.08, skip_limit_up_buys=True)
        self.breadth = breadth

    def market_breadth(self, day):
        return self.breadth if day == self.calendar[0] else 0.1

    def select(self, day):
        return [SimpleNamespace(symbol='AAA')]

    def is_limit_up(self, symbol, day):
        return symbol in self.limit_up


class TradeAlertTests(unittest.TestCase):
    def alert(self, selector, index):
        day = selector.calendar[index]
        result = run_capital_backtest(selector, selector.calendar[0], day)
        return result, build_trade_alert(selector, result, day)

    def test_buy_signal_precedes_simulated_execution(self):
        selector = FixtureSelector([10, 10])
        result, alert = self.alert(selector, 0)
        self.assertEqual(result.trades, [])
        self.assertEqual([(o['side'], o['shares']) for o in alert['orders']], [('BUY', 1000)])
        executed, tomorrow = self.alert(selector, 1)
        self.assertEqual(executed.trades[0]['shares'], 1000)
        self.assertFalse(tomorrow['actionable'])

    def test_stop_sell_and_no_daily_breadth_exit(self):
        selector = FixtureSelector([10, 10, 9, 9])
        _, hold = self.alert(selector, 1)
        self.assertFalse(hold['actionable'])
        _, alert = self.alert(selector, 2)
        self.assertEqual([(o['side'], o['reason'], o['shares']) for o in alert['orders']],
                         [('SELL', 'stop', 1000)])
        result, _ = self.alert(selector, 3)
        self.assertEqual(result.final_positions, {})

    def test_missing_quote_keeps_position_value_without_allowing_sale(self):
        selector = FixtureSelector([10, 10, 10, 10, 10], rebalance=2)
        selector.config.cash_yield_annual = 0
        selector.by_date[selector.calendar[3]].clear()
        result = run_capital_backtest(selector, selector.calendar[0])
        self.assertEqual(result.final_positions, {'AAA': 1000})
        self.assertEqual([trade['side'] for trade in result.trades], ['BUY'])
        self.assertEqual(result.equity_curve[3]['equity'], result.equity_curve[2]['equity'])
        self.assertLess(result.maximum_drawdown, .001)

    def test_suspended_quote_cannot_execute_buy(self):
        selector = FixtureSelector([10, 10])
        selector.by_date[selector.calendar[1]]['AAA'].suspended = True
        result = run_capital_backtest(selector, selector.calendar[0])
        self.assertEqual(result.trades, [])

    def test_rebalance_clearance(self):
        selector = FixtureSelector([10, 10, 10], rebalance=2)
        _, alert = self.alert(selector, 2)
        self.assertTrue(alert['rebalance'])
        self.assertEqual([(o['side'], o['reason']) for o in alert['orders']], [('SELL', 'rebalance')])

    def test_future_price_does_not_change_reminder(self):
        first = self.alert(FixtureSelector([10, 1000]), 0)[1]
        second = self.alert(FixtureSelector([10, 1]), 0)[1]
        self.assertEqual(first, second)

    def test_unaffordable_stock_not_reported_as_buy(self):
        _, alert = self.alert(FixtureSelector([200]), 0)
        self.assertEqual(alert['orders'], [])
        self.assertTrue(alert['rebalance'])

    def test_message_labels_estimates_and_simulation(self):
        _, alert = self.alert(FixtureSelector([10]), 0)
        title, message = format_trade_alert(alert)
        self.assertIn('买入 1', title)
        self.assertIn('模拟盘', message)
        self.assertIn('预计 1000 股', message)
        self.assertNotIn('开盘', message)

    def test_limit_up_signal_is_not_reported_as_buy(self):
        result, alert = self.alert(FixtureSelector([10, 10], limit_up={'AAA'}), 0)
        self.assertEqual(result.pending_limit_up_symbols, ['AAA'])
        self.assertEqual(alert['orders'], [])
        self.assertEqual(alert['skipped_limit_up'], ['AAA'])
        _, message = format_trade_alert(alert)
        self.assertIn('涨停', message)

    def test_historical_send_rejected_before_network(self):
        with patch.object(sys, 'argv', ['report', '--send', '--date', '2000-01-01']), \
             patch.object(daily_breadth_report, 'update_store') as update:
            with self.assertRaisesRegex(ValueError, '历史回放'):
                daily_breadth_report.main()
            update.assert_not_called()

    def test_partial_download_is_not_committed_and_can_retry(self):
        rows = [dict(date='2026-01-05', symbol=symbol, close='10', volume='100',
                     adj_factor='1', listing_days='500', is_st='0', suspended='0')
                for symbol in ['000001.SZ', '600000.SH']]
        def fetch(symbol):
            data = [['2026-01-05', '10', '10', '10', '10', '100'],
                    ['2026-01-06', '10', '11', '11', '10', '100']]
            return symbol, data, None
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            base = Path(directory) / 'base.csv'
            store = Path(directory) / 'store.csv'
            daily_breadth_report.write_rows(base, rows)
            with patch.object(daily_breadth_report, 'fetch_symbol',
                              side_effect=lambda s: fetch(s) if s.startswith('0') else (s, [], 'error')):
                daily_breadth_report.update_store(base, store)
            self.assertEqual(len(daily_breadth_report.read_rows(store)), 2)
            with patch.object(daily_breadth_report, 'fetch_symbol', side_effect=fetch):
                daily_breadth_report.update_store(base, store)
            self.assertEqual(len(daily_breadth_report.read_rows(store)), 4)

    def _write_limit_bars(self, path, days=162, jump_index=None, rate=0.10):
        with path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['date', 'symbol', 'close', 'volume', 'listing_days', 'is_st', 'suspended'])
            start = date(2024, 1, 1)
            for i in range(days):
                price = 10.0 * (1.0 + rate) if i == jump_index else 10.0
                writer.writerow([start + timedelta(days=i), '600000.SH', price, 5_000_000, 300 + i, 0, 0])

    def _limit_selector(self, path):
        config = SelectionConfig(top_n=1, minimum_history=121, minimum_average_turnover=1.0,
                                 minimum_market_breadth=0.0, rebalance_days=160)
        return StockSelector(load_bars(path), config)

    def test_limit_up_on_execution_day_blocks_buy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bars.csv'
            self._write_limit_bars(path, jump_index=161)
            selector = self._limit_selector(path)
            result = run_capital_backtest(selector, selector.calendar[0], selector.calendar[-1])
            self.assertEqual(result.final_positions, {})
            self.assertEqual([row['symbol'] for row in result.limit_up_skips], ['600000.SH'])
            self.assertEqual(result.limit_up_skips[0]['reason'], 'limit_up')

    def test_limit_up_on_signal_day_blocks_new_position(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bars.csv'
            self._write_limit_bars(path, jump_index=160)
            selector = self._limit_selector(path)
            result = run_capital_backtest(selector, selector.calendar[0], selector.calendar[160])
            self.assertEqual(result.pending_limit_up_symbols, ['600000.SH'])
            self.assertEqual(result.pending_symbols, [])
            self.assertEqual(result.final_positions, {})
            self.assertEqual(result.trades, [])

    def test_flat_market_still_executes_buy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bars.csv'
            self._write_limit_bars(path)
            selector = self._limit_selector(path)
            result = run_capital_backtest(selector, selector.calendar[0], selector.calendar[-1])
            self.assertEqual(list(result.final_positions), ['600000.SH'])
            self.assertEqual(result.limit_up_skips, [])


if __name__ == '__main__':
    unittest.main()
