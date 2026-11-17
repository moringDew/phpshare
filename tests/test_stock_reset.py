import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

from scripts.research_stock_reset import Rule, simulate


class ResetExecutionTests(unittest.TestCase):
    def test_hold_buffer_avoids_replacing_a_position_still_inside_buffer(self):
        shape = (4, 3)
        market = SimpleNamespace(
            days=pd.date_range("2026-01-05", periods=4),
            symbols=["600001.SH", "600002.SH", "600003.SH"],
            prices=np.full(shape, 10.0),
            order={"defensive": np.array([[0, 1, 2], [1, 0, 2], [1, 2, 0], [1, 2, 0]])},
            scores={"defensive": np.ones(shape)},
            buyable=np.ones(shape, dtype=bool),
            sellable=np.ones(shape, dtype=bool),
        )
        rule = Rule("defensive", 1, 1, False)
        dates = ("2026-01-05", "2026-01-08")
        value, trading = simulate(market, rule, *dates, .002, hold_rank=2)
        self.assertEqual(trading["trades"], 3)
        self.assertEqual(value[1], value[2])
        self.assertLess(value[3], value[2])
    def test_ma10_waits_for_crossing_holds_on_equality_and_sells_before_rebalance(self):
        market = SimpleNamespace(
            days=pd.date_range("2026-01-05", periods=6),
            symbols=["600001.SH"],
            prices=np.array([[10.0], [10.0], [10.0], [10.0], [10.0], [9.0]]),
            order={"defensive": np.zeros((6, 1), dtype=int)},
            scores={"defensive": np.ones((6, 1))},
            traded=np.ones((6, 1), dtype=bool),
            buyable=np.ones((6, 1), dtype=bool),
            sellable=np.ones((6, 1), dtype=bool),
        )
        distance = np.array([[-1.0], [1.0], [1.0], [0.0], [-1.0], [-1.0]])
        equity, trading = simulate(
            market, Rule("defensive", 10, 1, False), "2026-01-05", "2026-01-10", .002,
            ma10=distance,
        )
        np.testing.assert_allclose(equity, [100000, 100000, 99830, 99830, 99830, 91177])
        self.assertEqual(trading["trades"], 2)

    def test_next_close_fill_and_blocked_exit(self):
        market = SimpleNamespace(
            days=pd.date_range("2026-01-05", periods=4),
            symbols=["600001.SH"],
            prices=np.array([[10.0], [10.0], [8.0], [9.0]]),
            order={"defensive": np.zeros((4, 1), dtype=int)},
            scores={"defensive": np.array([[1.0], [-np.inf], [-np.inf], [-np.inf]])},
            buyable=np.array([[True], [True], [False], [True]]),
            sellable=np.array([[True], [True], [False], [True]]),
        )
        equity, trade_stats = simulate(
            market, Rule("defensive", 1, 1, False), "2026-01-05", "2026-01-08", .002
        )
        np.testing.assert_allclose(equity, [100000, 99830, 82830, 91177])
        self.assertEqual(trade_stats["trades"], 2)
        self.assertAlmostEqual(trade_stats["fees"], 323)
