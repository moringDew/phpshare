from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _parquet_glob(root: Path, relative: str) -> str:
    return _sql_path(root / relative / "**" / "*.parquet")


def export_selection_csv(
    data_root: Path,
    output: Path,
    start: str | None = None,
    end: str | None = None,
) -> Dict[str, Any]:
    """Export CNEquity's point-in-time lake into the selector CSV contract."""
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - depends on the data environment
        raise RuntimeError("缺少 duckdb；请使用安装 CNEquity 的 Python 环境运行") from exc

    bars_dir = data_root / "curated" / "daily_bars"
    instruments_file = data_root / "curated" / "instruments" / "part-merged.parquet"
    factors_dir = data_root / "derived" / "adj_factors"
    status_dir = data_root / "curated" / "trading_status"
    if not any(bars_dir.rglob("*.parquet")):
        raise RuntimeError("日线数据尚未完成发布，请先运行 cne init")
    if not instruments_file.exists():
        raise RuntimeError("证券主数据不存在，请先运行 cne init")
    if not any(factors_dir.rglob("*.parquet")):
        raise RuntimeError("复权因子不存在，请等待 cne init 的 adj_factors 阶段完成")

    has_status = status_dir.exists() and any(status_dir.rglob("*.parquet"))
    date_filters = []
    parameters = []
    if start:
        date_filters.append("CAST(d.trade_date AS DATE) >= CAST(? AS DATE)")
        parameters.append(start)
    if end:
        date_filters.append("CAST(d.trade_date AS DATE) <= CAST(? AS DATE)")
        parameters.append(end)
    date_sql = " AND " + " AND ".join(date_filters) if date_filters else ""

    status_cte = (
        "status AS (SELECT * FROM read_parquet('"
        + _parquet_glob(data_root, "curated/trading_status")
        + "', hive_partitioning=false))"
        if has_status
        else "status AS (SELECT CAST(NULL AS VARCHAR) symbol, CAST(NULL AS DATE) trade_date, "
        "CAST(NULL AS BOOLEAN) is_trading, CAST(NULL AS BOOLEAN) risk_warning WHERE false)"
    )
    query = f"""
        WITH
        bars AS (
            SELECT * REPLACE (CAST(trade_date AS DATE) AS trade_date)
            FROM read_parquet('{_parquet_glob(data_root, 'curated/daily_bars')}', hive_partitioning=false)
        ),
        instruments AS (
            SELECT * FROM read_parquet('{_sql_path(instruments_file)}')
        ),
        factors AS (
            SELECT symbol, CAST(trade_date AS DATE) AS trade_date, factor
            FROM read_parquet('{_parquet_glob(data_root, 'derived/adj_factors')}', hive_partitioning=false)
            WHERE adjust_type = 'hfq'
        ),
        {status_cte}
        SELECT
            strftime(d.trade_date, '%Y-%m-%d') AS date,
            d.symbol,
            d.close,
            d.volume,
            d.amount,
            f.factor AS adj_factor,
            COALESCE(s.risk_warning, false) AS is_st,
            NOT COALESCE(s.is_trading, true) AS suspended,
            CASE WHEN i.list_date IS NULL THEN NULL
                 ELSE date_diff('day', i.list_date, d.trade_date) END AS listing_days
        FROM bars d
        JOIN instruments i USING (symbol)
        JOIN factors f ON f.symbol = d.symbol AND f.trade_date = d.trade_date
        LEFT JOIN status s ON s.symbol = d.symbol AND CAST(s.trade_date AS DATE) = d.trade_date
        WHERE (
            (d.symbol LIKE '600%.SH' OR d.symbol LIKE '601%.SH' OR
             d.symbol LIKE '603%.SH' OR d.symbol LIKE '605%.SH')
            OR
            (d.symbol LIKE '000%.SZ' OR d.symbol LIKE '001%.SZ' OR
             d.symbol LIKE '002%.SZ' OR d.symbol LIKE '003%.SZ' OR
             d.symbol LIKE '300%.SZ' OR d.symbol LIKE '301%.SZ')
        ){date_sql}
    """

    output.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        stats = connection.execute(
            "SELECT COUNT(*) rows, COUNT(DISTINCT symbol) symbols, MIN(date), MAX(date), "
            "SUM(CASE WHEN is_st THEN 1 ELSE 0 END) st_rows FROM (" + query + ")",
            parameters,
        ).fetchone()
        escaped_output = _sql_path(output)
        connection.execute(
            f"COPY ({query}) TO '{escaped_output}' (HEADER, DELIMITER ',', QUOTE '\"')",
            parameters,
        )
    finally:
        connection.close()

    return {
        "output": str(output),
        "rows": stats[0],
        "symbols": stats[1],
        "start": str(stats[2]) if stats[2] else None,
        "end": str(stats[3]) if stats[3] else None,
        "historical_st_rows": int(stats[4] or 0),
        "historical_status_available": has_status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 CNEquity 免费数据湖供选股引擎使用")
    parser.add_argument("--data-root", type=Path, default=Path("data/cnequity"))
    parser.add_argument("--output", type=Path, default=Path("data/a_share_cnequity.csv"))
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    result = export_selection_csv(args.data_root, args.output, args.start, args.end)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
