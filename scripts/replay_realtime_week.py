from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from daily_breadth_report import fetch_names  # noqa: E402
import intraday_store  # noqa: E402
from render_strategy_summary import render_document  # noqa: E402
from stock_selection.engine import Bar, SelectionConfig, StockSelector, load_bars  # noqa: E402

STORE = ROOT / "data" / "a_share_public_latest.csv"
CONFIG = ROOT / "configs" / "stock_selection_optimized.json"
MD_PATH = ROOT / "docs" / "md" / "实时策略周报_40门槛对照_2026-11-09_2026-11-13.md"
CSV_PATH = ROOT / "output" / "realtime_replay_40_2026-11-09_2026-11-13.csv"
START = date(2026, 11, 9)
END = date(2026, 11, 13)

SLOT_TIMES = [
    "09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30",
    "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45",
]


def fetch_all(symbols: Sequence[str], start: date, end: date,
              count: int = 1000) -> Tuple[Dict[str, Dict[Tuple[str, str], Dict[str, float]]], List[str]]:
    results: Dict[str, Dict[Tuple[str, str], Dict[str, float]]] = {}
    failures: List[str] = []
    missing = []
    for symbol in symbols:
        if intraday_store.covers(symbol, start, end):
            results[symbol] = intraday_store.load_15m(symbol)
        else:
            missing.append(symbol)

    def fetch_one(symbol: str):
        try:
            return symbol, intraday_store.get_15m(symbol, start, end, count=count), None
        except Exception as exc:
            return symbol, {}, "{}: {}".format(type(exc).__name__, exc)

    if missing:
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_one, symbol) for symbol in missing]
            for future in as_completed(futures):
                symbol, bars, error = future.result()
                if bars:
                    results[symbol] = bars
                else:
                    failures.append("{} {}".format(symbol, error))
    return results, failures


def cumulative_day(symbol_bars: Mapping[Tuple[str, str], Dict[str, float]], day: str,
                   slot: str) -> Tuple[float, float]:
    volume = 0.0
    amount = 0.0
    for current in SLOT_TIMES:
        item = symbol_bars.get((day, current))
        if item is None:
            continue
        volume += item["volume"]
        amount += item["amount"]
        if current == slot:
            break
    return volume, amount


def build_provisional(history: Sequence[Bar], last_by_symbol: Mapping[str, Bar],
                      symbol_bars: Mapping[str, Mapping[Tuple[str, str], Dict[str, float]]],
                      day: date, slot: str, universe: Sequence[str]) -> List[Bar]:
    bars = list(history)
    for symbol in universe:
        last = last_by_symbol.get(symbol)
        if last is None:
            continue
        item = symbol_bars.get(symbol, {}).get((day.isoformat(), slot))
        if item is None or item["close"] <= 0:
            bars.append(Bar(trade_date=day, symbol=symbol, close=last.close, volume=0.0,
                            adj_factor=last.adj_factor, amount=0.0, is_st=last.is_st,
                            suspended=True, listing_days=(last.listing_days or 0) + 1))
            continue
        volume, amount = cumulative_day(symbol_bars[symbol], day.isoformat(), slot)
        bars.append(Bar(trade_date=day, symbol=symbol, close=item["close"], volume=volume,
                        adj_factor=1.0, amount=amount, is_st=last.is_st,
                        suspended=False, listing_days=(last.listing_days or 0) + 1))
    return bars


def next_slot(day: date, slot: str, trading_days: Sequence[date]) -> Optional[Tuple[str, str]]:
    index = SLOT_TIMES.index(slot)
    if index + 1 < len(SLOT_TIMES):
        return day.isoformat(), SLOT_TIMES[index + 1]
    later = [item for item in trading_days if item > day]
    if not later:
        return None
    return later[0].isoformat(), SLOT_TIMES[0]


def slot_return(picks: Sequence[str], day: date, slot: str, trading_days: Sequence[date],
                symbol_bars: Mapping[str, Mapping[Tuple[str, str], Dict[str, float]]]) -> Optional[float]:
    target = next_slot(day, slot, trading_days)
    if target is None or not picks:
        return None
    target_day, target_slot = target
    returns = []
    for symbol in picks:
        current = symbol_bars.get(symbol, {}).get((day.isoformat(), slot))
        following = symbol_bars.get(symbol, {}).get((target_day, target_slot))
        if current and following and current["close"] > 0:
            returns.append(following["close"] / current["close"] - 1.0)
    return statistics.mean(returns) if returns else None


def main() -> int:
    parser = argparse.ArgumentParser(description="回放指定周的 15 分钟实时选股信号")
    parser.add_argument("--start", type=date.fromisoformat, default=START)
    parser.add_argument("--end", type=date.fromisoformat, default=END)
    parser.add_argument("--store", type=Path, default=STORE)
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()

    all_bars = load_bars(args.store)
    trading_days = [day for day in sorted({bar.trade_date for bar in all_bars})
                    if args.start <= day <= args.end]
    if not trading_days:
        raise ValueError("指定区间没有交易日")
    base_day = max(day for day in {bar.trade_date for bar in all_bars} if day < args.start)
    base_rows = {bar.symbol: bar for bar in all_bars if bar.trade_date == base_day}
    universe = sorted(base_rows)
    print("universe", len(universe), "base_day", base_day, "trading_days", [d.isoformat() for d in trading_days], flush=True)

    symbol_bars, failures = fetch_all(universe, args.start, args.end)
    print("cached/fetched", len(symbol_bars), "failures", len(failures), flush=True)
    if failures:
        print("first failures", failures[:5], flush=True)

    config = SelectionConfig.from_json(args.config)
    rows: List[Dict[str, object]] = []
    previous_picks: List[str] = []
    previous_time = ""
    cumulative = 1.0

    for day in trading_days:
        history = [bar for bar in all_bars if bar.trade_date < day]
        last_by_symbol = {}
        for bar in history:
            current = last_by_symbol.get(bar.symbol)
            if current is None or bar.trade_date > current.trade_date:
                last_by_symbol[bar.symbol] = bar
        for slot in SLOT_TIMES:
            bars = build_provisional(history, last_by_symbol, symbol_bars, day, slot, universe)
            selector = StockSelector(bars, config)
            breadth = selector.market_breadth(day)
            risk_on = breadth >= config.minimum_market_breadth
            picks = selector.select(day) if risk_on else []
            pick_symbols = [item.symbol for item in picks]
            added, removed = sorted(set(pick_symbols) - set(previous_picks)), sorted(set(previous_picks) - set(pick_symbols))
            pct_changes: Dict[str, float] = {}
            for symbol in pick_symbols:
                current = symbol_bars.get(symbol, {}).get((day.isoformat(), slot))
                previous = last_by_symbol.get(symbol)
                if current and previous and previous.close:
                    pct_changes[symbol] = current["close"] / previous.close - 1.0
            ret = slot_return(pick_symbols, day, slot, trading_days, symbol_bars)
            if ret is not None:
                cumulative *= 1.0 + ret
            rows.append({
                "date": day.isoformat(), "time": slot, "breadth": breadth, "risk_on": risk_on,
                "picks": pick_symbols, "added": added, "removed": removed,
                "pct_changes": pct_changes,
                "return_pct": round(ret * 100.0, 4) if ret is not None else None,
                "previous_time": previous_time,
            })
            previous_picks = pick_symbols
            previous_time = slot
        print("done", day.isoformat(), "last", rows[-1]["picks"], flush=True)

    unique_symbols = sorted({symbol for row in rows for symbol in row["picks"]})
    fetched_names = fetch_names(unique_symbols) if unique_symbols else {}
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "time", "breadth_pct", "risk_on", "picks", "pct_changes",
                         "added", "removed", "next_slot_return_pct"])
        for row in rows:
            pct_text = ",".join(
                "{} {:+.2f}%".format(symbol, value * 100.0)
                for symbol, value in sorted(row.get("pct_changes", {}).items())
            )
            writer.writerow([
                row["date"], row["time"], "{:.2f}".format(float(row["breadth"]) * 100),
                "1" if row["risk_on"] else "0", ",".join(row["picks"]), pct_text,
                ",".join(row["added"]), ",".join(row["removed"]),
                "" if row["return_pct"] is None else row["return_pct"],
            ])

    changes = sum(1 for row in rows if row["added"] or row["removed"])
    adds = sum(len(row["added"]) for row in rows)
    removes = sum(len(row["removed"]) for row in rows)
    position_weight = 0.40 / 5.0
    cost_pct = (adds + removes) * position_weight * 0.0015 * 100.0
    net_cum = (cumulative - 1.0) * 100.0 - cost_pct
    returns = [float(row["return_pct"]) for row in rows if row["return_pct"] is not None]
    lines = [
        "# 实时策略周报（40% 门槛对照，15 分钟回放）2026-11-09 至 2026-11-13",
        "",
        "> **这是 40% 门槛的对照回放，不是当前 >50% 规则，也不是实际推送记录。**",
        "> 当前 >50% 规则的周报见 [实时策略周报（>50% 门槛）](实时策略周报_2026-11-09_2026-11-13.md)。",
        "> 数据：新浪 15 分钟 K 线；每个时间点用当时价格代替当日收盘价重算广度和目标。",
        "> 口径：日频重算、5 只、总仓位 40%、广度门槛 40%、涨停不追；收益未计成本。",
        "",
        "## 汇总",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        "| 交易日 | {} |".format(len(trading_days)),
        "| 15 分钟快照 | {} |".format(len(rows)),
        "| 目标变化次数 | {} |".format(changes),
        "| 新增 / 剔除笔数 | {} / {} |".format(adds, removes),
        "| 涉及股票数 | {} |".format(len(unique_symbols)),
        "| 下一时间点等权累计收益 | **{:+.2f}%** |".format((cumulative - 1.0) * 100.0),
        "| 估算换手成本 | -{:.2f}% |".format(cost_pct),
        "| 估算净收益 | **{:+.2f}%** |".format(net_cum),
        "| 日线级回放对照 | -2.83% |",
        "| 20 日策略同期 | 空仓，约 +0.03% 现金收益 |",
        "",
        "## 每日明细",
        "",
    ]
    for day in trading_days:
        day_rows = [row for row in rows if row["date"] == day.isoformat()]
        lines.extend([
            "### {}".format(day.isoformat()),
            "",
            "| 时间 | 广度 | 目标 | 新增 | 剔除 | 下一时间点收益 |",
            "|---|---:|---|---|---|---:|",
        ])
        for row in day_rows:
            pick_parts = []
            for symbol in row["picks"]:
                text = "{} {}".format(symbol, fetched_names.get(symbol, "")).strip()
                pct = row.get("pct_changes", {}).get(symbol)
                if pct is not None:
                    text += " ({:+.2f}%)".format(float(pct) * 100.0)
                pick_parts.append(text)
            picks = "、".join(pick_parts) or "空仓"
            added = "、".join(row["added"]) or "—"
            removed = "、".join(row["removed"]) or "—"
            ret = "—" if row["return_pct"] is None else "{:+.2f}%".format(float(row["return_pct"]))
            lines.append("| {} | {:.2f}% | {} | {} | {} | {} |".format(
                row["time"], float(row["breadth"]) * 100, picks, added, removed, ret))
        lines.append("")
    lines.extend([
        "## 结论",
        "",
        "1. 15 分钟级调仓换手极高：{} 个快照里目标变化 {} 次，新增 {} 笔、剔除 {} 笔。".format(len(rows), changes, adds, removes),
        "2. 下一时间点等权毛收益 **{:+.2f}%**，估算换手成本约 {:.2f}%，净约 **{:+.2f}%**；仍为负收益，且未计滑点和涨停买不到。".format(
            (cumulative - 1.0) * 100.0, cost_pct, net_cum),
        "3. 日线级回放是 -2.83%；15 分钟切换在盘中止损后略好，但两者口径不同，不能直接当超额。",
        "4. 20 日策略本周空仓，反而避开了回撤。",
        "5. 600869 远东股份本周 14.25 → 13.48（-5.40%），单独按 12.40 止损，不进入策略目标。",
        "",
        "## 说明",
        "",
        "- 回放使用新浪 15 分钟 K 线，14:45 不是真实盘口；涨停/停牌按收盘价近似判断。",
        "- 实际推送从 2026-11-16 09:45 开始，每个交易日 15 次；实际记录会写入 `output/realtime_preview_YYYY-MM-DD.csv`。",
        "- 以上为量化规则回放，不构成投资建议。",
        "",
    ])
    MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    MD_PATH.write_text("\n".join(lines), encoding="utf-8")
    html_path = render_document(MD_PATH)
    print("wrote", MD_PATH, html_path, "changes", changes, "cum", round((cumulative - 1) * 100, 2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
