from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import SelectionConfig, StockSelector, load_bars


def main() -> int:
    parser = argparse.ArgumentParser(description="A 股多因子选股与回测")
    parser.add_argument("--input", required=True, type=Path, help="日线 CSV")
    parser.add_argument("--config", type=Path, help="策略 JSON 配置")
    parser.add_argument("--output", type=Path, help="输出 JSON；未指定则打印")
    args = parser.parse_args()
    config = SelectionConfig.from_json(args.config) if args.config else SelectionConfig()
    result = StockSelector(load_bars(args.input), config).backtest().to_dict()
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

