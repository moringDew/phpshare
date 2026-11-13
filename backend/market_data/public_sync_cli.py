from __future__ import annotations

import argparse
import json
from pathlib import Path

from .eastmoney import sync_public_history


def main() -> int:
    parser = argparse.ArgumentParser(description="公开行情研究备用同步")
    parser.add_argument("--output", type=Path, default=Path("data/a_share_public.csv"))
    parser.add_argument("--universe-size", type=int, default=500)
    parser.add_argument("--bars", type=int, default=756)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()
    result = sync_public_history(args.output, args.universe_size, args.bars, args.workers)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
