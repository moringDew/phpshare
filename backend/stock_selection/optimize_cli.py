from __future__ import annotations

import argparse
import itertools
import json
from copy import deepcopy
from datetime import date
from pathlib import Path

from .capital import run_capital_backtest
from .engine import SelectionConfig, StockSelector, load_bars


def main() -> int:
    parser = argparse.ArgumentParser(description="使用训练集选择策略参数，并在样本外区间验证")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--train-start", type=date.fromisoformat, default=date(2024, 2, 8))
    parser.add_argument("--train-end", type=date.fromisoformat, default=date(2025, 12, 31))
    parser.add_argument("--test-start", type=date.fromisoformat, default=date(2026, 1, 1))
    args = parser.parse_args()
    bars = load_bars(args.input)
    base = SelectionConfig.from_json(args.config)
    selector = StockSelector(bars, base)
    candidates = []
    for top_n, rebalance, breadth, exposure in itertools.product(
        (8, 10), (10, 20), (0.40, 0.45, 0.50), (0.45, 0.60, 0.75)
    ):
        base.top_n = top_n
        base.rebalance_days = rebalance
        base.minimum_market_breadth = breadth
        base.maximum_equity_exposure = exposure
        base.maximum_single_position = min(0.10, exposure / top_n * 1.35)
        train = run_capital_backtest(selector, args.train_start, args.train_end)
        score = train.total_return - 1.5 * train.maximum_drawdown
        candidates.append({
            "params": {"top_n": top_n, "rebalance_days": rebalance, "minimum_market_breadth": breadth, "maximum_equity_exposure": exposure, "maximum_single_position": base.maximum_single_position},
            "train_return": train.total_return,
            "train_drawdown": train.maximum_drawdown,
            "score": score,
        })
    eligible = [item for item in candidates if item["train_drawdown"] <= 0.15]
    best = max(eligible or candidates, key=lambda item: item["score"])
    for key, value in best["params"].items():
        setattr(base, key, value)
    test = run_capital_backtest(selector, args.test_start)
    payload = {"method": "train_2024_2025_test_2026", "best": best, "test": test.to_dict(), "candidate_count": len(candidates)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"best": best, "test": {"return": test.total_return, "drawdown": test.maximum_drawdown, "final_equity": test.final_equity, "trades": test.trade_count}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
