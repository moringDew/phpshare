from __future__ import annotations

import argparse
import json
from pathlib import Path

import pyarrow.dataset as ds
import pyarrow.compute as pc


def main() -> int:
    parser = argparse.ArgumentParser(description="校验BaoStock前复权分区数据")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    files = sorted(args.input.glob("*.parquet"))
    if not files:
        raise SystemExit("没有Parquet文件")
    dataset = ds.dataset([str(path) for path in files], format="parquet")
    table = dataset.to_table(columns=["date", "symbol", "close", "is_st", "suspended"])
    dates = table.column("date")
    symbols = table.column("symbol")
    result = {
        "files": len(files),
        "rows": table.num_rows,
        "symbols": len(pc.unique(symbols)),
        "start_date": pc.min(dates).as_py(),
        "end_date": pc.max(dates).as_py(),
        "historical_st_rows": int(pc.sum(table.column("is_st")).as_py() or 0),
        "suspended_rows": int(pc.sum(table.column("suspended")).as_py() or 0),
        "null_close": int(pc.sum(pc.is_null(table.column("close"))).as_py() or 0),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
