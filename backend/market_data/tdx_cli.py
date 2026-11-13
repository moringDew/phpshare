from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .tdx import convert_tdx_zip


def main() -> int:
    parser = argparse.ArgumentParser(description="解析通达信官方日线完整包")
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2023, 1, 1))
    args = parser.parse_args()
    print(json.dumps(convert_tdx_zip(args.zip, args.output, args.start), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

