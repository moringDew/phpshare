import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

from scripts.research_sector_rotation import sector_order


class SectorRotationTests(unittest.TestCase):
    def test_future_snapshot_cannot_change_earlier_candidates(self):
        days = pd.date_range("2025-01-01", periods=63)
        codes = [f"60000{i}" for i in range(10)]
        prices = np.tile(np.linspace(10, 20, len(days))[:, None], (1, 10))
        market = SimpleNamespace(days=days, symbols=pd.Index(code + ".SH" for code in codes),
                                 prices=prices, traded=np.ones(prices.shape, dtype=bool),
                                 order={}, scores={"defensive": np.ones(prices.shape)})
        membership = pd.DataFrame([
            {"snapshot": day, "updateDate": day, "code": "sh." + code, "industry": "test"}
            for day, batch in ((days[60], codes[:5]), (days[62], codes[5:]))
            for code in batch
        ])
        rotated, _ = sector_order(market, membership, np.zeros(len(days)))
        np.testing.assert_array_equal(rotated.order["defensive"][61, :4], [0, 1, 2, 3])
        np.testing.assert_array_equal(rotated.order["defensive"][62, :4], [5, 6, 7, 8])
