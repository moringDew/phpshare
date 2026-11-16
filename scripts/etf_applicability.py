from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import backtest_breadth_contrarian as engine  # noqa: E402
from render_strategy_summary import render_document  # noqa: E402

REPORT_MD = ROOT / "docs" / "md" / "广度逆向策略_ETF适用性.md"
TRADE_MD = ROOT / "docs" / "md" / "广度逆向策略_ETF交易记录.md"
TRADE_CSV = ROOT / "output" / "etf_contrarian_trades.csv"
START_2026 = date(2026, 1, 5)
ENTRY = 0.25
EXIT = 0.55

ETFS = {
    "510050.SH": ("sh510050", "上证50ETF", "A股宽基"),
    "510300.SH": ("sh510300", "沪深300ETF", "A股宽基"),
    "510500.SH": ("sh510500", "中证500ETF", "A股宽基"),
    "512100.SH": ("sh512100", "中证1000ETF", "A股宽基"),
    "159915.SZ": ("sz159915", "创业板ETF", "A股成长"),
    "588000.SH": ("sh588000", "科创50ETF", "A股成长"),
    "510880.SH": ("sh510880", "红利ETF", "A股防御"),
    "512880.SH": ("sh512880", "证券ETF", "A股行业"),
    "512800.SH": ("sh512800", "银行ETF", "A股行业"),
    "512480.SH": ("sh512480", "半导体ETF", "A股行业"),
    "515030.SH": ("sh515030", "新能源车ETF", "A股行业"),
    "159920.SZ": ("sz159920", "恒生ETF", "港股"),
    "513100.SH": ("sh513100", "纳指ETF", "海外"),
    "513500.SH": ("sh513500", "标普500ETF", "海外"),
    "518880.SH": ("sh518880", "黄金ETF", "商品"),
    "511010.SH": ("sh511010", "国债ETF", "债券"),
}

SENSITIVITY = {
    "510300.SH": ("sh510300", "沪深300ETF"),
    "510500.SH": ("sh510500", "中证500ETF"),
    "159915.SZ": ("sz159915", "创业板ETF"),
    "510880.SH": ("sh510880", "红利ETF"),
}


def main() -> int:
    breadth = engine.load_breadth()
    rows = []
    for code, (sina, name, category) in ETFS.items():
        try:
            etf = engine.load_etf(code, sina)
            full = engine.backtest(etf, breadth, ENTRY, EXIT, 1)
            y2026 = engine.backtest(etf, breadth, ENTRY, EXIT, 1, start=START_2026)
            hold_full = engine.buy_hold(etf)
            hold_2026 = engine.buy_hold(etf, START_2026)
            rows.append({
                "code": code, "name": name, "category": category,
                "full": full, "y2026": y2026, "hold_full": hold_full, "hold_2026": hold_2026,
            })
        except Exception as exc:
            print("skip", code, exc, flush=True)

    trade_lines = [
        "# 广度逆向策略：ETF 交易记录",
        "",
        "> 规则：广度 ≤25% 买入，≥55% 卖出，确认 1 天；信号日收盘生成，下一交易日收盘成交。",
        "> 单边成本 0.15%；收益已扣买入和卖出费用；区间 2024-02-08～2026-11-13。",
        "> 全区间每个 ETF 只有约 6 笔交易；样本少，不能把单只 ETF 的结果当成稳定规律。",
        "",
        "| 类别 | ETF | 代码 | 买入信号日 | 买入成交日 | 买入价 | 卖出信号日 | 卖出成交日 | 卖出价 | 持有天数 | 收益 |",
        "|---|---|---|---|---|---:|---|---|---:|---:|---:|",
    ]
    trade_rows_csv = []
    for row in rows:
        for trade in row["full"].get("trade_records", []):
            entry_signal = trade["entry_signal"].isoformat() if trade["entry_signal"] else "—"
            entry_date = trade["entry_date"].isoformat() if trade["entry_date"] else "—"
            exit_signal = trade["exit_signal"].isoformat() if trade["exit_signal"] else "—"
            exit_date = trade["exit_date"].isoformat() if trade["exit_date"] else "—"
            exit_price = "—" if trade["exit_price"] is None else "{:.3f}".format(float(trade["exit_price"]))
            holding = "—" if trade["holding_days"] is None else str(trade["holding_days"])
            trade_lines.append("| {} | {} | {} | {} | {} | {:.3f} | {} | {} | {} | {} | {:+.2f}% |".format(
                row["category"], row["name"], row["code"], entry_signal, entry_date,
                float(trade["entry_price"]), exit_signal, exit_date, exit_price, holding,
                float(trade["return_pct"])))
            trade_rows_csv.append([
                row["category"], row["name"], row["code"], entry_signal, entry_date,
                "{:.4f}".format(float(trade["entry_price"])), exit_signal, exit_date,
                "" if trade["exit_price"] is None else "{:.4f}".format(float(trade["exit_price"])),
                "" if trade["holding_days"] is None else trade["holding_days"],
                "{:.2f}".format(float(trade["return_pct"])),
            ])
    trade_lines.extend([
        "",
        "## 说明",
        "",
        "- 同一广度信号下，所有 ETF 的买入/卖出日期相同，差异来自 ETF 自身走势。",
        "- 当前广度 40.16%，没有触发买入，策略状态为空仓等待。",
        "- 这是历史回测，不是实际成交记录；不构成投资建议。",
        "",
    ])
    TRADE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with TRADE_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["category", "name", "code", "buy_signal", "buy_date", "buy_price",
                         "sell_signal", "sell_date", "sell_price", "holding_days", "return_pct"])
        writer.writerows(trade_rows_csv)
    TRADE_MD.parent.mkdir(parents=True, exist_ok=True)
    TRADE_MD.write_text("\n".join(trade_lines), encoding="utf-8")
    render_document(TRADE_MD)

    lines = [
        "# 广度逆向策略：ETF 适用性",
        "",
        "> 固定参数：广度 ≤25% 买入，≥55% 卖出，确认 1 天；下一交易日收盘成交。",
        "> 成本：单边 0.15%，现金年化 1.5%；区间 2024-02-08～2026-11-13，2026 单独列示。",
        "> 该策略用 A 股市场广度做择时，因此更适合 A 股权益 ETF；黄金、债券、海外 ETF 的逻辑不同，结果只作对照。",
        "",
        "## 各 ETF 结果",
        "",
        "| 类别 | ETF | 代码 | 全区间收益 | 最大回撤 | 2026 收益 | 交易次数 | 持仓时间 | 买入持有全区间 | 买入持有 2026 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        full = row["full"]
        y = row["y2026"]
        hold = row["hold_full"]
        hold_y = row["hold_2026"]
        lines.append("| {} | {} | {} | {:+.2f}% | {:.2f}% | {:+.2f}% | {} | {:.0f}% | {:+.2f}% | {:+.2f}% |".format(
            row["category"], row["name"], row["code"],
            full["return_pct"], full["mdd_pct"], y["return_pct"] if y else 0.0,
            full["trades"], full["in_market_pct"], hold["return_pct"], hold_y["return_pct"]))
    lines.extend([
        "",
        "> 逐笔买卖记录（含日期、价格、持有天数、收益）见 [ETF 交易记录](广度逆向策略_ETF交易记录.md)。",
        "",
        "## 参数敏感性（确认 1 天）",
        "",
        "| ETF | 入场 | 出场 | 全区间收益 | 最大回撤 | 2026 收益 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for code, (sina, name) in SENSITIVITY.items():
        etf = engine.load_etf(code, sina)
        for entry in (0.20, 0.25, 0.30):
            for exit_level in (0.45, 0.50, 0.55):
                full = engine.backtest(etf, breadth, entry, exit_level, 1)
                y = engine.backtest(etf, breadth, entry, exit_level, 1, start=START_2026)
                lines.append("| {} | ≤{:.0f}% | ≥{:.0f}% | {:+.2f}% | {:.2f}% | {:+.2f}% |".format(
                    name, entry * 100, exit_level * 100, full["return_pct"], full["mdd_pct"],
                    y["return_pct"] if y else 0.0))
    lines.extend([
        "",
        "## 结论",
        "",
        "1. **A 股宽基/小盘最适合**：中证1000ETF 全区间 +89.40%、回撤 10.24%、2026 +28.44%；中证500ETF +66.36%、回撤 9.69%、2026 +21.41%。中证1000 收益更高，中证500 更均衡。",
        "2. **创业板/科创50 也能用，但回撤更大**：创业板回撤 12.99%，科创50 回撤 19.99%，适合能承受波动的资金。",
        "3. **红利 ETF 不适合这套信号**：+4.32%，跑输买入持有的 +12.11%；广度逆向没有给它增加价值。",
        "4. **行业 ETF（证券、银行、半导体、新能源车）收益高但分化大**：半导体 +98.06% 但回撤 20.05%，单行业风险高，不建议作为核心。",
        "5. **黄金、债券、海外 ETF 不建议套用**：A 股广度不是它们的定价驱动，回测结果可能是巧合，不能外推。",
        "6. 当前广度 40.16%，不满足 ≤25% 入场条件，策略状态为**空仓等待**。",
        "",
        "> 以上为历史回测，不构成投资建议；参数存在过拟合风险，实盘前需样本外验证。",
        "",
    ])
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    html = render_document(REPORT_MD)
    print("wrote", REPORT_MD, html, flush=True)
    for row in rows:
        print(row["name"], round(row["full"]["return_pct"], 2), round(row["full"]["mdd_pct"], 2),
              round(row["y2026"]["return_pct"], 2) if row["y2026"] else None,
              round(row["hold_full"]["return_pct"], 2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
