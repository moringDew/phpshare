from __future__ import annotations

import itertools
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from stock_selection.capital import run_capital_backtest  # noqa: E402
from stock_selection.engine import SelectionConfig, StockSelector, load_bars  # noqa: E402

TRAIN_START = date(2024, 2, 8)
TRAIN_END = date(2025, 12, 31)
TEST_START = date(2026, 1, 1)
OUT_JSON = ROOT / "output" / "optimization_topn_20261113.json"
OUT_MD = ROOT / "docs" / "OPTIMIZATION_TOPN.md"


def main() -> int:
    bars = load_bars(ROOT / "data" / "a_share_public.csv")
    base = SelectionConfig.from_json(ROOT / "configs" / "stock_selection.json")
    selector = StockSelector(bars, base)

    top_ns = (5, 8, 10, 15, 20, 30)
    rebalance_days = (10, 20)
    breadths = (0.40, 0.45, 0.50)
    exposures = (0.45, 0.60, 0.75)
    combos = list(itertools.product(top_ns, rebalance_days, breadths, exposures))
    started = time.time()
    results = []

    for index, (top_n, rebalance, breadth, exposure) in enumerate(combos, start=1):
        base.top_n = top_n
        base.rebalance_days = rebalance
        base.minimum_market_breadth = breadth
        base.maximum_equity_exposure = exposure
        base.maximum_single_position = min(0.10, exposure / top_n * 1.35)
        train = run_capital_backtest(selector, TRAIN_START, TRAIN_END)
        score = train.total_return - 1.5 * train.maximum_drawdown
        results.append(
            {
                "params": {
                    "top_n": top_n,
                    "rebalance_days": rebalance,
                    "minimum_market_breadth": breadth,
                    "maximum_equity_exposure": exposure,
                    "maximum_single_position": base.maximum_single_position,
                },
                "train_return": train.total_return,
                "train_drawdown": train.maximum_drawdown,
                "train_trades": train.trade_count,
                "score": score,
            }
        )
        if index % 10 == 0 or index == len(combos):
            print("progress {}/{} elapsed {:.1f}s".format(index, len(combos), time.time() - started), flush=True)

    eligible = [item for item in results if item["train_drawdown"] <= 0.15]
    best = max(eligible or results, key=lambda item: item["score"])

    for key, value in best["params"].items():
        setattr(base, key, value)
    test = run_capital_backtest(selector, TEST_START)

    by_top_n = {}
    for top_n in top_ns:
        subset = [item for item in results if item["params"]["top_n"] == top_n]
        subset_eligible = [item for item in subset if item["train_drawdown"] <= 0.15]
        winner = max(subset_eligible or subset, key=lambda item: item["score"])
        by_top_n[top_n] = winner

    payload = {
        "method": "train_2024_2025_test_2026_expanded_topn",
        "train_range": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
        "test_start": TEST_START.isoformat(),
        "candidate_count": len(combos),
        "grid": {
            "top_n": list(top_ns),
            "rebalance_days": list(rebalance_days),
            "minimum_market_breadth": list(breadths),
            "maximum_equity_exposure": list(exposures),
        },
        "best": best,
        "best_test": {
            "final_equity": test.final_equity,
            "total_return": test.total_return,
            "maximum_drawdown": test.maximum_drawdown,
            "trade_count": test.trade_count,
            "invested_days": test.invested_days,
        },
        "by_top_n": {str(key): value for key, value in by_top_n.items()},
        "all_results": results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 持股数量网格优化报告",
        "",
        "> 训练区间：{} 至 {}；样本外：{} 起".format(TRAIN_START, TRAIN_END, TEST_START),
        "> 训练目标：累计收益 - 1.5 × 最大回撤；要求训练最大回撤 ≤ 15%",
        "> 参数组合：{} 组（持股数 {}）".format(len(combos), "/".join(str(x) for x in top_ns)),
        "",
        "## 各持股数量的最优参数",
        "",
        "| 持股数 | 广度门槛 | 总仓位 | 调仓 | 单票上限 | 训练收益 | 训练回撤 | 得分 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for top_n in top_ns:
        item = by_top_n[top_n]
        params = item["params"]
        lines.append(
            "| {top_n} | {breadth:.0%} | {exposure:.0%} | {rebalance}日 | {single:.2%} | {ret:.2f}% | {dd:.2f}% | {score:.4f} |".format(
                top_n=top_n,
                breadth=params["minimum_market_breadth"],
                exposure=params["maximum_equity_exposure"],
                rebalance=params["rebalance_days"],
                single=params["maximum_single_position"],
                ret=item["train_return"] * 100,
                dd=item["train_drawdown"] * 100,
                score=item["score"],
            )
        )
    best_params = best["params"]
    lines.extend(
        [
            "",
            "## 全局最优",
            "",
            "- 持股数：{}".format(best_params["top_n"]),
            "- 广度门槛：{:.0%}".format(best_params["minimum_market_breadth"]),
            "- 总仓位：{:.0%}".format(best_params["maximum_equity_exposure"]),
            "- 调仓周期：{} 个交易日".format(best_params["rebalance_days"]),
            "- 单票上限：{:.2%}".format(best_params["maximum_single_position"]),
            "- 训练收益：{:.2f}%".format(best["train_return"] * 100),
            "- 训练回撤：{:.2f}%".format(best["train_drawdown"] * 100),
            "",
            "### 2026 样本外",
            "",
            "| 期末资产 | 累计收益 | 最大回撤 | 交易数 | 持仓天数 |",
            "|---:|---:|---:|---:|---:|",
            "| {:,.2f} 元 | {:.2f}% | {:.2f}% | {} | {} |".format(
                test.final_equity, test.total_return * 100, test.maximum_drawdown * 100,
                test.trade_count, test.invested_days),
            "",
            "## 说明",
            "",
            "- 回测使用 100 股整手、单边 0.15% 成本、单票上限随总仓位和持股数调整。",
            "- 训练集最优不代表样本外最优；持股数越极端，交易成本和整手约束的影响越大。",
            "- 该结果基于公开行情备用源，候选池存在幸存者偏差，不构成投资建议。",
            "",
        ]
    )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({"best": best["params"], "train": [best["train_return"], best["train_drawdown"]],
                      "test": [test.total_return, test.maximum_drawdown],
                      "by_top_n": {str(k): [v["params"]["minimum_market_breadth"],
                                            v["params"]["maximum_equity_exposure"],
                                            v["train_return"], v["train_drawdown"]]
                                   for k, v in by_top_n.items()}},
                     ensure_ascii=False, indent=2))
    print("wrote {} and {}".format(OUT_JSON, OUT_MD))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
