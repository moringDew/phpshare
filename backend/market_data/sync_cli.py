from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .tushare import AShareDailySync, TushareClient


def main() -> int:
    parser = argparse.ArgumentParser(description="同步沪深主板和创业板日线")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output", type=Path, default=Path("data/a_share_daily.csv"))
    args = parser.parse_args()
    result = AShareDailySync(TushareClient.from_environment()).sync(args.start, args.end, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
