from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from stock_selection.capital import run_capital_backtest  # noqa: E402
from stock_selection.engine import SelectionConfig, StockSelector, load_bars  # noqa: E402

STORE = ROOT / "data" / "ths_daily_combined.csv"
CONFIG = ROOT / "configs" / "stock_selection_multifactor.json"


def main() -> int:
    bars = load_bars(STORE)
    config = SelectionConfig.from_json(CONFIG)
    periods = [
        ("4Y", date(2022, 11, 14), date(2026, 11, 13)),
        ("2023", date(2023, 1, 1), date(2023, 12, 31)),
        ("2024", date(2024, 1, 1), date(2024, 12, 31)),
        ("2025", date(2025, 1, 1), date(2025, 12, 31)),
        ("2026", date(2026, 1, 1), date(2026, 11, 13)),
    ]
    for label, start, end in periods:
        selector = StockSelector(bars, replace(config, strategy_start=start.isoformat()))
        result = run_capital_backtest(selector, start, end)
        print("{}: final {:.2f} ret {:+.2f}% mdd {:.2f}% trades {} fees {:.0f}".format(
            label, result.final_equity, result.total_return * 100,
            result.maximum_drawdown * 100, result.trade_count, result.fees), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
