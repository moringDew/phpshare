"""Record confirmed fills and export the separate real-position ledger."""
import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'scripts'))
from stock_selection.real_account import add_trade, snapshot, void_trade
from render_strategy_summary import render_document

LEDGER = ROOT / 'data' / 'real_account' / 'trades.sqlite3'
REPORT = ROOT / 'docs' / 'md' / '真实持仓.md'


def export_report(ledger=LEDGER, report=REPORT):
    account = snapshot(ledger)
    lines = ['# 真实持仓与成交台账', '',
             '> 来源：用户确认的实际成交；与模拟盘独立，不连接券商自动下单。', '',
             '## 汇总', '',
             '- 已录入成交：{} 笔'.format(account['trade_count']),
             '- 当前持有：{} 只'.format(len(account['holdings'])),
             '- 已实现盈亏：{:+,.2f} 元'.format(Decimal(account['realized_pnl'])),
             '- 累计交易现金流：{:+,.2f} 元（不是账户现金余额）'.format(Decimal(account['cash_flow'])),
             '- 手续费未知：{} 笔；这些费用尚未计入成本及盈亏。'.format(account['unknown_fee_count']),
             '- 未录入初始资金、转入转出及分红，因此暂不计算账户总收益率。',
             '- 未接入真实未复权报价，暂不展示市值与浮动盈亏；不使用回测复权价格估值。', '',
             '## 当前真实持仓', '',
             '| 股票代码 | 名称 | 股数 | 平均成本 | 剩余成本金额 | 本轮首次买入时间 |',
             '|---|---|---:|---:|---:|---|']
    for row in account['holdings']:
        lines.append('| {} | {} | {} | {:.4f} | {:,.2f} | {} |'.format(
            row['symbol'], row['name'] or '未提供', row['shares'], Decimal(row['average_cost']),
            Decimal(row['cost']), row['first_buy']))
    if not account['trade_count']:
        lines.extend(['', '尚未录入真实成交，不能据此认定你的券商账户空仓。'])
    elif not account['holdings']:
        lines.extend(['', '根据已录入记录，当前无持仓。'])
    lines.extend(['', '## 成交记录', '',
                  '| 记录 ID | 成交时间 | 方向 | 股票 | 名称 | 股数 | 成交价 | 成交额（不含费用） | 手续费 | 本笔已实现盈亏 |',
                  '|---|---|---|---|---|---:|---:|---:|---:|---:|'])
    for row in account['trades']:
        lines.append('| {} | {} | {} | {} | {} | {} | {} | {:,.2f} | {} | {} |'.format(
            row['id'], row['traded_at'], '买入' if row['side'] == 'BUY' else '卖出', row['symbol'],
            row['name'] or '未提供', row['shares'], row['price'], Decimal(row['gross']),
            '{:,.2f}'.format(Decimal(row['fee'])) if row['fee'] is not None else '未知',
            '{:+,.2f}'.format(Decimal(row['realized_pnl'])) if row['realized_pnl'] is not None else '—'))
    lines.extend(['', '成本采用移动加权平均法，已知买入费用计入成本，卖出费用扣减已实现盈亏。',
                  '', '[录入与更正说明](真实持仓说明.md)', ''])
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('\n'.join(lines), encoding='utf-8')
    return render_document(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, default=LEDGER)
    sub = parser.add_subparsers(dest='command', required=True)
    record = sub.add_parser('add', help='录入已确认成交')
    record.add_argument('--side', choices=['BUY', 'SELL'], required=True)
    record.add_argument('--symbol', required=True)
    record.add_argument('--name', default='')
    record.add_argument('--time', dest='traded_at', required=True)
    record.add_argument('--price', required=True)
    record.add_argument('--amount', required=True)
    record.add_argument('--shares', type=int)
    record.add_argument('--fee')
    record.add_argument('--amount-includes-fees', action='store_true')
    record.add_argument('--trade-id')
    cancel = sub.add_parser('void', help='保留审计记录并作废误录成交')
    cancel.add_argument('--trade-id', required=True)
    cancel.add_argument('--reason', required=True)
    sub.add_parser('show')
    sub.add_parser('export')
    args = parser.parse_args()
    if args.command == 'add':
        values = vars(args).copy()
        values.pop('command')
        values.pop('ledger')
        trade_id = add_trade(args.ledger, **values)
        print('Recorded: ' + trade_id)
    elif args.command == 'void':
        void_trade(args.ledger, args.trade_id, args.reason)
        print('Voided: ' + args.trade_id)
    elif args.command == 'show':
        print(json.dumps(snapshot(args.ledger), ensure_ascii=False, indent=2))
        return
    print(export_report(args.ledger))


if __name__ == '__main__':
    main()
