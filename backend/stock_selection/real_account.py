"""User-confirmed executions. Exact decimal accounting; no broker orders."""
import sqlite3
import uuid
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

SHANGHAI = timezone(timedelta(hours=8))


def number(value, label, positive=False):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(label + '必须是数字')
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(label + '必须是有效的非负数字' + ('且大于零' if positive else ''))
    return result


def normalize_symbol(value):
    value = value.strip().upper()
    if re.fullmatch(r'\d{6}', value):
        value += '.SH' if value.startswith('6') else '.BJ' if value[0] in '489' else '.SZ'
    if not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', value):
        raise ValueError('请提供六位股票代码，例如 600000 或 600000.SH')
    return value


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('''CREATE TABLE IF NOT EXISTS trades (
        id TEXT PRIMARY KEY, traded_at TEXT NOT NULL, symbol TEXT NOT NULL,
        name TEXT NOT NULL, side TEXT NOT NULL, shares INTEGER NOT NULL,
        price TEXT NOT NULL, gross TEXT NOT NULL, fee TEXT,
        created_at TEXT NOT NULL, voided_at TEXT, void_reason TEXT)''')
    connection.commit()
    return connection


def rows(connection, as_of=None):
    records = [dict(row) for row in connection.execute(
        'SELECT * FROM trades WHERE voided_at IS NULL ORDER BY traded_at, rowid')]
    if as_of:
        records = [row for row in records if row['traded_at'][:10] <= str(as_of)]
    return records


def calculate(records):
    positions = {}
    realized = Decimal('0')
    cash_flow = Decimal('0')
    unknown_fees = 0
    executions = []
    for row in records:
        symbol = row['symbol']
        position = positions.setdefault(symbol, dict(symbol=symbol, name=row['name'], shares=0,
                                                     cost=Decimal('0'), first_buy=None))
        if row['name']:
            position['name'] = row['name']
        quantity = row['shares']
        gross = Decimal(row['gross'])
        fee = Decimal(row['fee']) if row['fee'] is not None else Decimal('0')
        unknown_fees += row['fee'] is None
        pnl = None
        if row['side'] == 'BUY':
            if not position['shares']:
                position['first_buy'] = row['traded_at']
            position['shares'] += quantity
            position['cost'] += gross + fee
            cash_flow -= gross + fee
        else:
            if quantity > position['shares']:
                raise ValueError('{} 卖出 {} 股超过当时已录入持仓 {} 股，请先补齐买入记录'.format(
                    symbol, quantity, position['shares']))
            basis = position['cost'] * quantity / position['shares']
            pnl = gross - fee - basis
            realized += pnl
            cash_flow += gross - fee
            position['shares'] -= quantity
            position['cost'] -= basis
        executions.append(dict(row, realized_pnl=str(pnl) if pnl is not None else None))
    holdings = []
    for position in positions.values():
        if position['shares']:
            holdings.append(dict(position, cost=str(position['cost']),
                                 average_cost=str(position['cost'] / position['shares'])))
    return dict(holdings=sorted(holdings, key=lambda row: row['symbol']),
                realized_pnl=str(realized), cash_flow=str(cash_flow),
                unknown_fee_count=unknown_fees, trades=executions,
                trade_count=len(records), account_cash=None)


def snapshot(path, as_of=None):
    connection = connect(path)
    try:
        return calculate(rows(connection, as_of))
    finally:
        connection.close()


def add_trade(path, *, side, symbol, traded_at, price, amount, shares=None, fee=None,
              amount_includes_fees=False, name='', trade_id=None):
    side = side.upper()
    if side not in ('BUY', 'SELL'):
        raise ValueError('方向必须为 BUY 或 SELL')
    symbol = normalize_symbol(symbol)
    if trade_id is not None and not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', trade_id):
        raise ValueError('成交记录 ID 只能包含字母、数字、点、横线和下划线，最长64位')
    if not re.search(r'[T ]\d{2}:\d{2}', traded_at):
        raise ValueError('请提供包含时分的成交时间，例如 2026-11-13 14:50')
    moment = datetime.fromisoformat(traded_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SHANGHAI)
    moment = moment.astimezone(SHANGHAI)
    if moment > datetime.now(SHANGHAI):
        raise ValueError('不能将未来的计划记录为已成交')
    if any(char in name for char in '\r\n|`[]<>'):
        raise ValueError('股票名称含不支持的字符')
    price = number(price, '成交价', True)
    gross = number(amount, '成交金额', True)
    fee = number(fee, '手续费') if fee is not None else None
    if amount_includes_fees:
        if fee is None:
            raise ValueError('金额含费用时必须提供手续费，才能还原成交额')
        gross = gross - fee if side == 'BUY' else gross + fee
    if gross <= 0:
        raise ValueError('扣除费用后的成交额必须大于零')
    if shares is None:
        quantity = gross / price
        if quantity != quantity.to_integral_value():
            raise ValueError('金额除以价格不是整数，请提供实际成交股数，并核对费用口径')
        shares = int(quantity)
    if not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0:
        raise ValueError('成交股数必须为正整数')
    if abs(price * shares - gross) > Decimal('0.01'):
        raise ValueError('成交股数、价格与金额不一致，请提供更精确的成交均价或核对记录')
    if side == 'SELL' and fee is not None and fee > gross:
        raise ValueError('卖出费用不能超过成交额')
    connection = connect(path)
    try:
        connection.execute('BEGIN IMMEDIATE')
        values = dict(id=trade_id or uuid.uuid4().hex[:12], traded_at=moment.isoformat(timespec='seconds'),
                      symbol=symbol, name=name, side=side, shares=shares, price=str(price), gross=str(gross),
                      fee=str(fee) if fee is not None else None, created_at=datetime.now(SHANGHAI).isoformat())
        if not trade_id:
            for row in rows(connection):
                if all(row[key] == values[key] for key in ('traded_at', 'symbol', 'side', 'shares')) and Decimal(row['gross']) == gross:
                    raise ValueError('疑似重复成交，原记录 ID：' + row['id'])
        connection.execute('''INSERT INTO trades
            (id,traded_at,symbol,name,side,shares,price,gross,fee,created_at)
            VALUES (:id,:traded_at,:symbol,:name,:side,:shares,:price,:gross,:fee,:created_at)''', values)
        calculate(rows(connection))
        connection.commit()
        return values['id']
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def void_trade(path, trade_id, reason):
    if not reason.strip():
        raise ValueError('请填写作废原因')
    connection = connect(path)
    try:
        connection.execute('BEGIN IMMEDIATE')
        cursor = connection.execute('UPDATE trades SET voided_at=?,void_reason=? WHERE id=? AND voided_at IS NULL',
                                    (datetime.now(SHANGHAI).isoformat(), reason, trade_id))
        if cursor.rowcount != 1:
            raise ValueError('记录不存在或已经作废')
        calculate(rows(connection))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reconcile_message(account, alert):
    """Describe recorded quantities only; never imply simulated orders were filled."""
    if not account['trade_count']:
        return ''
    positions = {row['symbol']: row for row in account['holdings']}
    lines = ['**真实持仓核对（手工成交台账）**']
    for row in account['holdings']:
        lines.append('- {} {}：{} 股，记录成本 {:.4f} 元/股'.format(
            row['symbol'], row['name'], row['shares'], Decimal(row['average_cost'])))
    if not positions:
        lines.append('已录入成交计算结果：当前空仓。')
    for order in alert['orders']:
        held = positions.get(order['symbol'], {}).get('shares', 0)
        if order['side'] == 'SELL':
            lines.append('- 策略拟卖 {} {} 股；真实台账持有 {} 股，请按实际账户核对，不能照搬模拟股数。'.format(
                order['symbol'], order['shares'], held))
        else:
            lines.append('- 策略拟买 {}；真实台账已持有 {} 股，实际可用现金尚未录入。'.format(order['symbol'], held))
    if account['unknown_fee_count']:
        lines.append('部分手续费未提供，成本与盈亏暂未计入这些费用。')
    lines.append('未连接券商；仅反映你已告知的成交，不自动改写真实持仓。')
    return '\n'.join(lines)
