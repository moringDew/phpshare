import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.research_stock_forecast20 import LAG, panel, recalibrate
from scripts.research_forecast_features import context_features


class ForecastDataTests(unittest.TestCase):
    def test_industry_uses_prior_snapshot_and_excludes_the_stock_itself(self):
        days = pd.bdate_range("2025-01-01", periods=25)
        symbols = [f"60000{i}.SH" for i in range(6)]
        prices = pd.DataFrame(10., index=days, columns=symbols)
        prices.iloc[21, 0] = 20.
        indices = pd.DataFrame(100., index=days, columns=["sh.000300", "sh.000905"])
        members = pd.DataFrame({"snapshot": days[20], "industry": "group",
                                "code": ["sh." + s[:6] for s in symbols]})
        result = context_features(prices, prices.notna(), indices, members)
        self.assertTrue(np.isnan(result["industry_peer20"].iloc[20, 0]))
        self.assertAlmostEqual(result["industry_peer20"].iloc[21, 0], 0.)
        self.assertAlmostEqual(result["industry_peer20"].iloc[21, 1], .2)

    def test_calibration_waits_until_label_matures(self):
        days = pd.bdate_range("2025-01-01", periods=5)
        observations = pd.DataFrame({
            "date": days[:4], "maturity": days[1:], "actual": [.1, -.1, .1, -.1],
            "probability": [.5] * 4,
        })
        base, _ = recalibrate(observations, .5, minimum=1)
        observations.loc[1, "actual"] = .1
        changed, _ = recalibrate(observations, .5, minimum=1)
        np.testing.assert_allclose(base.iloc[:3], changed.iloc[:3])
        self.assertGreater(changed.iloc[3], base.iloc[3])

    def test_later_prices_change_label_without_changing_formation_features(self):
        days = pd.bdate_range("2025-01-01", periods=90)
        rows = pd.DataFrame({"date": days, "symbol": "600001.SH", "close": 10 + np.arange(len(days)) / 100,
                             "volume": 5_000_000, "adj_factor": 1, "is_st": 0, "suspended": 0})
        signal = 65
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "data.csv"
            rows.to_csv(path, index=False)
            X, meta, _ = panel(path, days[-1])
            rows.loc[signal + 2:, "close"] *= 1.1
            rows.to_csv(path, index=False)
            modified, new_meta, _ = panel(path, days[-1])
        ix = meta.index[meta.date.eq(days[signal])][0]
        np.testing.assert_allclose(X[ix], modified[ix])
        self.assertEqual(meta.at[ix, "maturity"], days[signal + LAG])
        self.assertGreater(new_meta.at[ix, "actual"], meta.at[ix, "actual"])
