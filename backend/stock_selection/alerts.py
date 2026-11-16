"""Next-session reminders from the backtest's pending signals, never orders."""
from .capital import target_shares


def build_trade_alert(selector, result, day):
    config = selector.config
    positions = dict(result.final_positions)
    cash = result.final_cash
    orders = []
    skip_limit_up = getattr(config, 'skip_limit_up_buys', True)
    is_limit_up = getattr(selector, 'is_limit_up', None)
    skipped_limit_up = list(getattr(result, 'pending_limit_up_symbols', []))

    def change_pct(symbol):
        change = getattr(selector, 'daily_change', None)
        value = change(symbol, day) if change is not None else None
        return round(value * 100.0, 2) if value is not None else None

    def sell(symbol, quantity, reason):
        nonlocal cash
        price = selector.by_date[day][symbol].close
        cash += quantity * price * (1 - config.transaction_cost_rate)
        positions[symbol] -= quantity
        orders.append(dict(side='SELL', symbol=symbol, shares=quantity,
                           reference_price=price, change_pct=change_pct(symbol), reason=reason))

    for symbol in result.pending_stop_symbols:
        if positions.get(symbol) and symbol in selector.by_date[day]:
            sell(symbol, positions[symbol], 'stop')

    rebalance = result.pending_symbols is not None
    targets = None
    if rebalance:
        targets = target_shares(selector, day, result.pending_symbols, result.final_equity)
        for symbol in sorted(positions):
            quantity = positions[symbol] - targets.get(symbol, 0)
            if quantity > 0:
                sell(symbol, quantity, 'rebalance')
        for symbol in sorted(targets):
            if symbol in result.pending_stop_symbols:
                continue
            quantity = targets[symbol] - positions.get(symbol, 0)
            price = selector.by_date[day][symbol].close
            if quantity > 0 and skip_limit_up and is_limit_up is not None and is_limit_up(symbol, day):
                skipped_limit_up.append(symbol)
                continue
            if quantity > 0 and quantity * price * (1 + config.transaction_cost_rate) <= cash + 1e-9:
                cash -= quantity * price * (1 + config.transaction_cost_rate)
                orders.append(dict(side='BUY', symbol=symbol, shares=quantity,
                                   reference_price=price, change_pct=change_pct(symbol), reason='rebalance'))
    return dict(signal_date=day.isoformat(), basis='simulated_portfolio',
                execution='next_session_close_reference', estimates=True,
                rebalance=rebalance, actionable=rebalance or bool(orders),
                targets=targets, orders=orders,
                skipped_limit_up=sorted(set(skipped_limit_up)))


def format_trade_alert(alert, names=None):
    names = names or {}
    orders = alert['orders']
    buys = sum(row['side'] == 'BUY' for row in orders)
    sells = sum(row['side'] == 'SELL' for row in orders)
    title = '策略提醒 {}｜买入 {} / 卖出 {}'.format(alert['signal_date'], buys, sells)
    lines = ['**下一交易日买卖提醒**', '', '信号日期：' + alert['signal_date'],
             '依据：策略模拟盘，尚未读取你的券商实际持仓。',
             '时间口径：收盘后发信号；回测按下一交易日收盘价模拟成交。',
             '以下股数按信号日收盘参考价估算，执行前需核对实际持仓、可用资金与当日价格。', '']
    for side, label in [('SELL', '卖出'), ('BUY', '买入')]:
        rows = [row for row in orders if row['side'] == side]
        if not rows:
            continue
        lines.append('**' + label + '**')
        for row in rows:
            reason = '个股止损' if row['reason'] == 'stop' else '调仓'
            change_text = ''
            if row.get('change_pct') is not None:
                change_text = '｜当日 {:+.2f}%'.format(float(row['change_pct']))
            lines.append('- {} {}：预计 {} 股｜参考价 {:.2f}{}｜{}'.format(
                row['symbol'], names.get(row['symbol'], ''), row['shares'],
                row['reference_price'], change_text, reason))
    if alert.get('skipped_limit_up'):
        names_text = '、'.join(
            '{} {}'.format(symbol, names.get(symbol, '')) for symbol in alert['skipped_limit_up']
        )
        lines.append('**未买入（信号日收盘涨停）**')
        lines.append('- {}：按规则本次调仓不追涨停，暂不买入；下一交易日若仍涨停同样无法成交。'.format(names_text))
    if alert['rebalance'] and not orders and not alert.get('skipped_limit_up'):
        lines.append('今日为调仓信号日，按当前参考价暂无买卖差额；下一交易日仍需复核目标仓位。')
    if not alert['actionable']:
        lines.append('今日无待执行买卖信号。')
    lines.extend(['', '仅提醒，不自动下单；参考价不保证能够成交。'])
    return title, '\n'.join(lines)
