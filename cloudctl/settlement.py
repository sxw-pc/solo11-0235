"""Settle issued invoices across prepaid, org-guaranteed and bank funding.

同一段计算只开一笔费用:计量行按(实体机器,执行代次,计数区间)去重,失败作业不计费。
发票出具后,每个资金来源先建立不可变预留(同一发票的结算身份),捕获合计不得超过发票额,
任何账户不得为负。银行结果以请求号/事务号核实,提交边界可恢复:超时只留未知态,
核实前绝不把发票标为已付,未扣款才幂等补发。撤销与冲正只追加补偿分录,不改写历史。
"""
import sqlite3

from . import store

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL, decision TEXT NOT NULL, evidence TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS usage_dedup(node TEXT NOT NULL, job TEXT NOT NULL, generation INTEGER NOT NULL, counter_start INTEGER NOT NULL, counter_end INTEGER NOT NULL, tenant TEXT NOT NULL, ticks INTEGER NOT NULL, page_id TEXT NOT NULL, line_no INTEGER NOT NULL, invoice_id TEXT, PRIMARY KEY(node,job,generation,counter_start,counter_end))",
    "CREATE TABLE IF NOT EXISTS invoices(id TEXT PRIMARY KEY, tenant TEXT, ticks INTEGER)",
    "CREATE TABLE IF NOT EXISTS invoice_lines(invoice_id TEXT NOT NULL, node TEXT NOT NULL, job TEXT NOT NULL, generation INTEGER NOT NULL, counter_start INTEGER NOT NULL, counter_end INTEGER NOT NULL, ticks INTEGER NOT NULL, PRIMARY KEY(invoice_id,node,job,generation,counter_start,counter_end))",
    "CREATE TABLE IF NOT EXISTS prepaid_accounts(tenant TEXT PRIMARY KEY, balance INTEGER)",
    "CREATE TABLE IF NOT EXISTS org_accounts(org TEXT PRIMARY KEY, balance INTEGER, held INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS guarantees(id TEXT PRIMARY KEY, org TEXT NOT NULL, tenant TEXT NOT NULL, max_amount INTEGER NOT NULL, expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, revoked_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, source TEXT NOT NULL, amount INTEGER NOT NULL, ref TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL, compensated INTEGER NOT NULL DEFAULT 0, UNIQUE(invoice_id,source,ref))",
    "CREATE TABLE IF NOT EXISTS bank_requests(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, amount INTEGER NOT NULL, invoice_id TEXT, status TEXT NOT NULL, bank_txn TEXT, updated INTEGER)",
    "CREATE TABLE IF NOT EXISTS ledger(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, invoice_id TEXT, source TEXT, amount INTEGER NOT NULL, ref TEXT, at INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS receivables(seq INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, amount INTEGER NOT NULL, reason TEXT NOT NULL, at INTEGER NOT NULL)",
]

CAPTURE_KINDS = ('prepaid_debit', 'org_transfer', 'bank_capture')


class SettlementError(ValueError):
    pass


class BankTimeout(Exception):
    pass


class BankError(Exception):
    pass


def ensure_schema(db):
    # DDL runs inside the caller's transaction; never executescript (it would commit).
    for statement in SCHEMA:
        db.execute(statement)
    if 'status' not in [c[1] for c in db.execute('PRAGMA table_info(invoices)')]:
        db.execute("ALTER TABLE invoices ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
    if 'held' not in [c[1] for c in db.execute('PRAGMA table_info(prepaid_accounts)')]:
        db.execute('ALTER TABLE prepaid_accounts ADD COLUMN held INTEGER NOT NULL DEFAULT 0')


# ---------------------------------------------------------------- metering

def ingest_meter_page(db, page_id, rows):
    """Ingest counter rows {node,tenant,job,generation,counter_start,counter_end}; ticks=end-start.

    同一(机器,代次,区间)因迁移、失败重试或重复扫描页再次出现时只记一次;
    键相同而租户不同即冲突。ticks 若给出必须与计数区间一致。
    """
    ensure_schema(db)
    required = {'node', 'tenant', 'job', 'generation', 'counter_start', 'counter_end'}
    ingested = duplicates = 0
    for line_no, r in enumerate(rows, 1):
        if not isinstance(r, dict) or not required.issubset(r):
            raise SettlementError('invalid_counter_row')
        start, end, generation = r['counter_start'], r['counter_end'], r['generation']
        if not all(isinstance(v, int) for v in (start, end, generation)) or generation < 0 or not 0 <= start < end:
            raise SettlementError('invalid_counter_row')
        ticks = end - start
        if 'ticks' in r and r['ticks'] != ticks:
            raise SettlementError('invalid_counter_row')
        key = (r['node'], r['job'], generation, start, end)
        old = store.row(db, 'SELECT tenant FROM usage_dedup WHERE node=? AND job=? AND generation=? AND counter_start=? AND counter_end=?', key)
        if old:
            if old['tenant'] != r['tenant']:
                raise SettlementError('counter_conflict')
            duplicates += 1
            continue
        db.execute('INSERT INTO usage_dedup VALUES(?,?,?,?,?,?,?,?,?,NULL)', key + (r['tenant'], ticks, page_id, line_no))
        ingested += 1
    decision = 'captured' if ingested else 'replayed'
    store.record(db, 'meter', page_id, decision, {'ingested': ingested, 'duplicates': duplicates})
    return {'status': decision, 'ingested': ingested, 'duplicates': duplicates}


def _billable_lines(db, tenant):
    return store.rows(db,
        'SELECT u.node,u.job,u.generation,u.counter_start,u.counter_end,u.ticks FROM usage_dedup u '
        'JOIN jobs j ON j.id=u.job AND j.tenant=u.tenant '
        'WHERE u.tenant=? AND u.invoice_id IS NULL AND EXISTS('
        "  SELECT 1 FROM attempts a WHERE a.job=u.job AND a.status='success') "
        'ORDER BY u.node,u.job,u.generation,u.counter_start', (tenant,))


def issue_invoice(db, invoice_id, tenant, at):
    """合计某租户未开票且已成功交付的计量行写入发票;失败作业的行留在未开票池。"""
    ensure_schema(db)
    old = store.row(db, 'SELECT * FROM invoices WHERE id=?', (invoice_id,))
    if old:
        if old['tenant'] != tenant:
            raise SettlementError('invoice_conflict')
        return {'id': invoice_id, 'tenant': tenant, 'ticks': old['ticks'], 'status': 'replayed'}
    lines = _billable_lines(db, tenant)
    if not lines:
        raise SettlementError('nothing_billable')
    amount = sum(l['ticks'] for l in lines)
    db.execute('INSERT INTO invoices(id,tenant,ticks,status) VALUES(?,?,?,?)', (invoice_id, tenant, amount, 'open'))
    for l in lines:
        db.execute('INSERT INTO invoice_lines VALUES(?,?,?,?,?,?,?)',
                   (invoice_id, l['node'], l['job'], l['generation'], l['counter_start'], l['counter_end'], l['ticks']))
        db.execute('UPDATE usage_dedup SET invoice_id=? WHERE node=? AND job=? AND generation=? AND counter_start=? AND counter_end=?',
                   (invoice_id, l['node'], l['job'], l['generation'], l['counter_start'], l['counter_end']))
    store.record(db, 'invoice', invoice_id, 'issued', {'tenant': tenant, 'ticks': amount, 'lines': len(lines), 'at': at})
    return {'id': invoice_id, 'tenant': tenant, 'ticks': amount, 'status': 'issued', 'lines': len(lines)}


# ---------------------------------------------------------------- funding setup

def open_prepaid(db, tenant, balance):
    ensure_schema(db)
    old = store.row(db, 'SELECT balance FROM prepaid_accounts WHERE tenant=?', (tenant,))
    if old:
        if old['balance'] != balance:
            raise SettlementError('account_conflict')
        return {'tenant': tenant, 'balance': balance, 'status': 'replayed'}
    db.execute('INSERT INTO prepaid_accounts(tenant,balance,held) VALUES(?,?,0)', (tenant, balance))
    return {'tenant': tenant, 'balance': balance, 'status': 'opened'}


def open_org(db, org, balance):
    ensure_schema(db)
    old = store.row(db, 'SELECT balance FROM org_accounts WHERE org=?', (org,))
    if old:
        if old['balance'] != balance:
            raise SettlementError('account_conflict')
        return {'org': org, 'balance': balance, 'status': 'replayed'}
    db.execute('INSERT INTO org_accounts VALUES(?,?,0)', (org, balance))
    return {'org': org, 'balance': balance, 'status': 'opened'}


def grant_guarantee(db, grant_id, org, tenant, max_amount, expires, at):
    """组织担保授权:写清受益租户、最高额度与有效期。"""
    ensure_schema(db)
    if not isinstance(max_amount, int) or max_amount <= 0:
        raise SettlementError('invalid_guarantee')
    old = store.row(db, 'SELECT * FROM guarantees WHERE id=?', (grant_id,))
    if old:
        same = old['org'] == org and old['tenant'] == tenant and old['max_amount'] == max_amount and old['expires'] == expires
        if not same:
            raise SettlementError('guarantee_conflict')
        return {'id': grant_id, 'status': 'replayed'}
    if not store.row(db, 'SELECT org FROM org_accounts WHERE org=?', (org,)):
        raise SettlementError('org_missing')
    db.execute('INSERT INTO guarantees VALUES(?,?,?,?,?,0,NULL)', (grant_id, org, tenant, max_amount, expires))
    store.record(db, 'guarantee', grant_id, 'granted', {'org': org, 'tenant': tenant, 'max_amount': max_amount, 'expires': expires, 'at': at})
    return {'id': grant_id, 'status': 'granted'}


def revoke_guarantee(db, grant_id, at):
    """撤销只拦尚未提交的划转;已执行的捕获只能走补偿分录(compensate_guarantee)。"""
    ensure_schema(db)
    g = store.row(db, 'SELECT * FROM guarantees WHERE id=?', (grant_id,))
    if not g:
        raise SettlementError('guarantee_missing')
    if g['revoked']:
        return {'id': grant_id, 'decision': 'replayed'}
    db.execute('UPDATE guarantees SET revoked=1, revoked_at=? WHERE id=?', (at, grant_id))
    held = _guarantee_usage(db, grant_id, ('held',))
    executed = _guarantee_usage(db, grant_id, ('captured',))
    decision = 'revoked_late' if executed else 'revoked'
    store.record(db, 'guarantee', grant_id, decision, {'held': held, 'executed': executed, 'at': at})
    return {'id': grant_id, 'decision': decision, 'held': held, 'executed': executed}


def _guarantee_usage(db, grant_id, statuses=('held', 'captured')):
    marks = ','.join('?' * len(statuses))
    return store.row(db, "SELECT COALESCE(SUM(amount),0) AS total FROM reservations WHERE source='org' AND ref=? AND status IN (%s)" % marks,
                     (grant_id,) + tuple(statuses))['total']


# ---------------------------------------------------------------- reservations

def _invoice(db, invoice_id):
    inv = store.row(db, 'SELECT * FROM invoices WHERE id=?', (invoice_id,))
    if not inv:
        raise SettlementError('invoice_missing')
    return inv


def _active_total(db, invoice_id):
    return store.row(db, "SELECT COALESCE(SUM(amount),0) AS total FROM reservations WHERE invoice_id=? AND status IN ('held','captured')", (invoice_id,))['total']


def reserve(db, invoice_id, source, amount, ref, at):
    """为一个资金来源建立不可变预留;同一(发票,来源,引用)即结算身份,幂等。

    预留立即扣减对应账户的可用额(held),共享信用在同一事务内对他人可见地减少。
    """
    ensure_schema(db)
    inv = _invoice(db, invoice_id)
    if inv['status'] != 'open':
        raise SettlementError('invoice_not_open')
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise SettlementError('invalid_amount')
    rid = '%s:%s:%s' % (invoice_id, source, ref)
    old = store.row(db, 'SELECT * FROM reservations WHERE id=?', (rid,))
    if old:
        if old['amount'] != amount or old['status'] == 'reversed':
            raise SettlementError('reservation_conflict')
        if old['status'] in ('held', 'captured'):
            return {'reservation': rid, 'status': 'replayed'}
        # released:同一结算身份可重新建立预留
    if _active_total(db, invoice_id) + amount > inv['ticks']:
        raise SettlementError('over_invoice')
    if source == 'prepaid':
        if ref != inv['tenant']:
            raise SettlementError('prepaid_tenant_mismatch')
        updated = db.execute('UPDATE prepaid_accounts SET held=held+? WHERE tenant=? AND balance-held>=?', (amount, ref, amount))
        if not updated.rowcount:
            raise SettlementError('insufficient_prepaid')
    elif source == 'org':
        g = store.row(db, 'SELECT * FROM guarantees WHERE id=?', (ref,))
        if not g:
            raise SettlementError('guarantee_missing')
        if g['tenant'] != inv['tenant']:
            raise SettlementError('guarantee_tenant_mismatch')
        if g['revoked']:
            raise SettlementError('guarantee_revoked')
        if at > g['expires']:
            raise SettlementError('guarantee_expired')
        if _guarantee_usage(db, ref) + amount > g['max_amount']:
            raise SettlementError('guarantee_cap')
        updated = db.execute('UPDATE org_accounts SET held=held+? WHERE org=? AND balance-held>=?', (amount, g['org'], amount))
        if not updated.rowcount:
            raise SettlementError('insufficient_org_credit')
    elif source == 'bank':
        req = store.row(db, 'SELECT * FROM bank_requests WHERE id=?', (ref,))
        if req:
            if req['amount'] != amount or req['tenant'] != inv['tenant'] or req['invoice_id'] != invoice_id or req['status'] == 'frozen':
                raise SettlementError('bank_request_conflict')
            if req['status'] == 'cancelled':
                db.execute("UPDATE bank_requests SET status='pending', updated=? WHERE id=?", (at, ref))
        else:
            db.execute('INSERT INTO bank_requests VALUES(?,?,?,?,?,?,?)', (ref, inv['tenant'], amount, invoice_id, 'pending', None, at))
    else:
        raise SettlementError('unknown_source')
    if old:
        db.execute("UPDATE reservations SET status='held', created=? WHERE id=?", (at, rid))
    else:
        db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?,?,0)', (rid, invoice_id, source, amount, ref, 'held', at))
    store.record(db, 'reserve', rid, 'held', {'invoice': invoice_id, 'source': source, 'amount': amount, 'at': at})
    return {'reservation': rid, 'status': 'held'}


def release(db, reservation_id, at):
    """释放尚未捕获的预留。切换付款办法时必须先释放,共享信用立即可供其他子租户。"""
    ensure_schema(db)
    r = store.row(db, 'SELECT * FROM reservations WHERE id=?', (reservation_id,))
    if not r:
        raise SettlementError('reservation_missing')
    if r['status'] == 'released':
        return {'reservation': reservation_id, 'status': 'replayed'}
    if r['status'] != 'held':
        raise SettlementError('reservation_not_held')
    _unhold(db, r)
    db.execute("UPDATE reservations SET status='released' WHERE id=?", (reservation_id,))
    store.record(db, 'release', reservation_id, 'released', {'amount': r['amount'], 'at': at})
    return {'reservation': reservation_id, 'status': 'released'}


def _unhold(db, r):
    if r['source'] == 'prepaid':
        db.execute('UPDATE prepaid_accounts SET held=held-? WHERE tenant=? AND held>=?', (r['amount'], r['ref'], r['amount']))
    elif r['source'] == 'org':
        g = store.row(db, 'SELECT org FROM guarantees WHERE id=?', (r['ref'],))
        db.execute('UPDATE org_accounts SET held=held-? WHERE org=? AND held>=?', (r['amount'], g['org'], r['amount']))
    elif r['source'] == 'bank':
        req = store.row(db, 'SELECT status FROM bank_requests WHERE id=?', (r['ref'],))
        if req and req['status'] in ('in_flight', 'confirmed'):
            raise SettlementError('bank_in_flight')
        db.execute("UPDATE bank_requests SET status='cancelled' WHERE id=? AND status='pending'", (r['ref'],))


def capture(db, reservation_id, at):
    """把预留转为真实资金移动并追加入账;捕获合计等于发票额时发票结清。"""
    ensure_schema(db)
    r = store.row(db, 'SELECT * FROM reservations WHERE id=?', (reservation_id,))
    if not r:
        raise SettlementError('reservation_missing')
    if r['status'] == 'captured':
        return {'reservation': reservation_id, 'status': 'replayed'}
    if r['status'] != 'held':
        raise SettlementError('reservation_not_held')
    inv = _invoice(db, r['invoice_id'])
    if inv['status'] != 'open':
        raise SettlementError('invoice_not_open')
    amount = r['amount']
    if r['source'] == 'prepaid':
        updated = db.execute('UPDATE prepaid_accounts SET held=held-?, balance=balance-? WHERE tenant=? AND held>=? AND balance>=?',
                             (amount, amount, r['ref'], amount, amount))
        if not updated.rowcount:
            raise SettlementError('insufficient_prepaid')
        kind, txn = 'prepaid_debit', None
    elif r['source'] == 'org':
        g = store.row(db, 'SELECT * FROM guarantees WHERE id=?', (r['ref'],))
        if g['revoked']:
            raise SettlementError('guarantee_revoked')
        updated = db.execute('UPDATE org_accounts SET held=held-?, balance=balance-? WHERE org=? AND held>=? AND balance>=?',
                             (amount, amount, g['org'], amount, amount))
        if not updated.rowcount:
            raise SettlementError('insufficient_org_credit')
        kind, txn = 'org_transfer', None
    else:
        req = store.row(db, 'SELECT * FROM bank_requests WHERE id=?', (r['ref'],))
        if not req or req['status'] != 'confirmed':
            raise SettlementError('bank_unconfirmed')
        kind, txn = 'bank_capture', req['bank_txn']
    db.execute("UPDATE reservations SET status='captured' WHERE id=?", (reservation_id,))
    db.execute('INSERT INTO ledger(kind,invoice_id,source,amount,ref,at) VALUES(?,?,?,?,?,?)', (kind, r['invoice_id'], r['source'], amount, r['ref'], at))
    store.record(db, 'capture', reservation_id, 'captured', {'kind': kind, 'amount': amount, 'bank_txn': txn, 'at': at})
    captured = store.row(db, "SELECT COALESCE(SUM(amount),0) AS total FROM reservations WHERE invoice_id=? AND status='captured'", (r['invoice_id'],))['total']
    settled = False
    if captured == inv['ticks']:
        db.execute("UPDATE invoices SET status='settled' WHERE id=?", (r['invoice_id'],))
        store.record(db, 'invoice', r['invoice_id'], 'settled', {'ticks': inv['ticks'], 'at': at})
        settled = True
    return {'reservation': reservation_id, 'status': 'captured', 'settled': settled}


# ---------------------------------------------------------------- bank boundary

def _load_request(db, request_id):
    req = store.row(db, 'SELECT * FROM bank_requests WHERE id=?', (request_id,))
    if not req:
        raise SettlementError('bank_request_missing')
    return req


def submit_bank(db_path, bank, request_id, at, mode=None):
    """提交银行扣款:先本地提交 in_flight,再调用银行;超时只留未知态,绝不先标已付。"""
    with store.transaction(db_path) as db:
        ensure_schema(db)
        req = _load_request(db, request_id)
        if req['status'] == 'confirmed':
            return {'status': 'confirmed', 'bank_txn': req['bank_txn'], 'replayed': True}
        if req['status'] != 'pending':
            raise SettlementError('bank_not_submittable')
        db.execute("UPDATE bank_requests SET status='in_flight', updated=? WHERE id=?", (at, request_id))
        store.record(db, 'bank', request_id, 'submitted', {'amount': req['amount'], 'at': at})
        tenant, amount = req['tenant'], req['amount']
    try:
        receipt = bank.debit(request_id, tenant, amount, at, mode=mode)
    except BankTimeout:
        return {'status': 'unknown', 'request': request_id}
    return _confirm_bank(db_path, request_id, receipt['bank_txn'], at)


def reconcile_bank(db_path, bank, request_id, at):
    """断电/超时恢复:先按银行事务号或请求号核实外部结果;确认未扣款才幂等补发,绝不盲重试。"""
    with store.transaction(db_path) as db:
        ensure_schema(db)
        req = _load_request(db, request_id)
        if req['status'] in ('frozen', 'cancelled'):
            raise SettlementError('bank_' + req['status'])
        tenant, amount, known_txn = req['tenant'], req['amount'], req['bank_txn']
    if known_txn:
        ans = bank.inquire(bank_txn=known_txn)
        if ans['status'] != 'debited':
            raise SettlementError('bank_txn_mismatch')
        return {'status': 'confirmed', 'bank_txn': known_txn, 'replayed': True}
    ans = bank.inquire(request_id=request_id)
    if ans['status'] == 'debited':
        return _confirm_bank(db_path, request_id, ans['bank_txn'], at)
    if ans['status'] == 'voided':
        with store.transaction(db_path) as db:
            ensure_schema(db)
            db.execute("UPDATE bank_requests SET status='frozen', updated=? WHERE id=?", (at, request_id))
        return {'status': 'frozen', 'request': request_id}
    try:
        receipt = bank.debit(request_id, tenant, amount, at)
    except BankTimeout:
        return {'status': 'unknown', 'request': request_id}
    return _confirm_bank(db_path, request_id, receipt['bank_txn'], at)


def _confirm_bank(db_path, request_id, bank_txn, at):
    with store.transaction(db_path) as db:
        ensure_schema(db)
        db.execute("UPDATE bank_requests SET status='confirmed', bank_txn=?, updated=? WHERE id=?", (bank_txn, at, request_id))
        store.record(db, 'bank', request_id, 'confirmed', {'bank_txn': bank_txn, 'at': at})
    return {'status': 'confirmed', 'bank_txn': bank_txn}


def freeze_bank(db_path, bank, request_id, at):
    """经银行核实未扣款后冻结该请求并释放对应预留;已扣款则只能捕获或补偿,不得冻结。"""
    ans = bank.inquire(request_id=request_id)
    if ans['status'] == 'debited':
        raise SettlementError('bank_already_debited')
    try:
        bank.void(request_id, at)
    except BankError:
        raise SettlementError('bank_already_debited')
    with store.transaction(db_path) as db:
        ensure_schema(db)
        req = _load_request(db, request_id)
        if req['status'] == 'confirmed':
            raise SettlementError('bank_already_debited')
        db.execute("UPDATE bank_requests SET status='frozen', updated=? WHERE id=?", (at, request_id))
        res = store.row(db, "SELECT id FROM reservations WHERE invoice_id=? AND source='bank' AND ref=? AND status='held'", (req['invoice_id'], request_id))
        if res:
            db.execute("UPDATE reservations SET status='released' WHERE id=?", (res['id'],))
        store.record(db, 'bank', request_id, 'frozen', {'at': at})
    return {'status': 'frozen', 'request': request_id}


# ---------------------------------------------------------------- compensating entries

def reverse_invoice(db, invoice_id, at, reason, bank=None):
    """冲正已发错账:释放未捕获预留;已捕获的一律追加补偿分录(银行端真实退款)。

    计量行回到未开票池,可从原件重开正确发票。银行退款幂等,中途断电可安全重入。
    """
    ensure_schema(db)
    inv = _invoice(db, invoice_id)
    if inv['status'] == 'reversed':
        return {'invoice': invoice_id, 'status': 'replayed'}
    for r in store.rows(db, 'SELECT * FROM reservations WHERE invoice_id=?', (invoice_id,)):
        if r['status'] == 'held':
            _unhold(db, r)
        elif r['status'] == 'captured':
            amount = r['amount']
            if r['source'] == 'prepaid':
                db.execute('UPDATE prepaid_accounts SET balance=balance+? WHERE tenant=?', (amount, r['ref']))
            elif r['source'] == 'org':
                g = store.row(db, 'SELECT org FROM guarantees WHERE id=?', (r['ref'],))
                db.execute('UPDATE org_accounts SET balance=balance+? WHERE org=?', (amount, g['org']))
            else:
                if bank is None:
                    raise SettlementError('bank_required')
                bank.refund(r['ref'], at)
            db.execute('INSERT INTO ledger(kind,invoice_id,source,amount,ref,at) VALUES(?,?,?,?,?,?)', ('reversal', invoice_id, r['source'], amount, r['ref'], at))
        if r['status'] in ('held', 'captured'):
            db.execute("UPDATE reservations SET status='reversed' WHERE id=?", (r['id'],))
    db.execute("UPDATE invoices SET status='reversed' WHERE id=?", (invoice_id,))
    db.execute('UPDATE usage_dedup SET invoice_id=NULL WHERE invoice_id=?', (invoice_id,))
    store.record(db, 'invoice', invoice_id, 'reversed', {'reason': reason, 'at': at})
    return {'invoice': invoice_id, 'status': 'reversed'}


def compensate_guarantee(db, grant_id, at, reason='late_revocation'):
    """撤销迟到:对已执行的组织划转追加补偿分录——退回组织、向受益租户追偿。

    只追租户预付可用额,不足部分记应收;任何账户不得为负。幂等。
    """
    ensure_schema(db)
    g = store.row(db, 'SELECT * FROM guarantees WHERE id=?', (grant_id,))
    if not g:
        raise SettlementError('guarantee_missing')
    if not g['revoked']:
        raise SettlementError('guarantee_active')
    executed = store.rows(db, "SELECT * FROM reservations WHERE source='org' AND ref=? AND status='captured' AND compensated=0", (grant_id,))
    total = recovered = 0
    for r in executed:
        amount = r['amount']
        total += amount
        db.execute('UPDATE org_accounts SET balance=balance+? WHERE org=?', (amount, g['org']))
        db.execute('INSERT INTO ledger(kind,invoice_id,source,amount,ref,at) VALUES(?,?,?,?,?,?)', ('compensation', r['invoice_id'], 'org', amount, grant_id, at))
        account = store.row(db, 'SELECT balance, held FROM prepaid_accounts WHERE tenant=?', (g['tenant'],))
        take = 0
        if account:
            take = min(amount, max(0, account['balance'] - account['held']))
            if take:
                db.execute('UPDATE prepaid_accounts SET balance=balance-? WHERE tenant=? AND balance>=?', (take, g['tenant'], take))
        if amount - take:
            db.execute('INSERT INTO receivables(tenant,amount,reason,at) VALUES(?,?,?,?)', (g['tenant'], amount - take, reason, at))
        recovered += take
        db.execute('UPDATE reservations SET compensated=1 WHERE id=?', (r['id'],))
    store.record(db, 'compensate', grant_id, 'compensated',
                 {'total': total, 'recovered_prepaid': recovered, 'receivable': total - recovered, 'reason': reason, 'at': at})
    return {'grant': grant_id, 'compensated': total, 'recovered_prepaid': recovered, 'receivable': total - recovered}


# ---------------------------------------------------------------- queries & replay

def invoice_status(db, invoice_id):
    ensure_schema(db)
    inv = store.row(db, 'SELECT * FROM invoices WHERE id=?', (invoice_id,))
    if not inv:
        return None
    held = store.row(db, "SELECT COALESCE(SUM(amount),0) AS t FROM reservations WHERE invoice_id=? AND status='held'", (invoice_id,))['t']
    captured = store.row(db, "SELECT COALESCE(SUM(amount),0) AS t FROM reservations WHERE invoice_id=? AND status='captured'", (invoice_id,))['t']
    return {'id': inv['id'], 'tenant': inv['tenant'], 'ticks': inv['ticks'], 'status': inv['status'],
            'held': held, 'captured': captured, 'outstanding': inv['ticks'] - held - captured}


def available_credit(db, tenant, at):
    """新作业信用检查:预付可用额 + 各有效担保下(额度余量,组织可用额)的较小者。"""
    ensure_schema(db)
    account = store.row(db, 'SELECT balance, held FROM prepaid_accounts WHERE tenant=?', (tenant,))
    prepaid = max(0, account['balance'] - account['held']) if account else 0
    org_credit = 0
    for g in store.rows(db, 'SELECT * FROM guarantees WHERE tenant=? AND revoked=0 AND expires>=?', (tenant, at)):
        org = store.row(db, 'SELECT balance, held FROM org_accounts WHERE org=?', (g['org'],))
        org_available = max(0, org['balance'] - org['held']) if org else 0
        org_credit += max(0, min(g['max_amount'] - _guarantee_usage(db, g['id']), org_available))
    return {'tenant': tenant, 'prepaid': prepaid, 'org': org_credit, 'total': prepaid + org_credit}


def verify_invoice(db, invoice_id):
    """从原件复演:计量行、作业租户、成功尝试、资金预留与银行回单必须互相印证。"""
    ensure_schema(db)
    inv = store.row(db, 'SELECT * FROM invoices WHERE id=?', (invoice_id,))
    if not inv:
        return {'invoice': invoice_id, 'consistent': False, 'reason': 'missing'}
    lines = store.rows(db, 'SELECT * FROM invoice_lines WHERE invoice_id=?', (invoice_id,))
    checks = {
        'amount_matches_lines': inv['ticks'] == sum(l['ticks'] for l in lines),
        'lines_from_meter': all(store.row(db, 'SELECT ticks FROM usage_dedup WHERE node=? AND job=? AND generation=? AND counter_start=? AND counter_end=? AND ticks=?',
                                          (l['node'], l['job'], l['generation'], l['counter_start'], l['counter_end'], l['ticks'])) for l in lines),
        'tenant_matches_jobs': all((store.row(db, 'SELECT tenant FROM jobs WHERE id=?', (l['job'],)) or {}).get('tenant') == inv['tenant'] for l in lines),
        'jobs_succeeded': all(store.row(db, "SELECT job FROM attempts WHERE job=? AND status='success'", (l['job'],)) for l in lines),
    }
    captured = store.row(db, 'SELECT COALESCE(SUM(amount),0) AS t FROM ledger WHERE invoice_id=? AND kind IN (?,?,?)', (invoice_id,) + CAPTURE_KINDS)['t']
    reversed_ = store.row(db, "SELECT COALESCE(SUM(amount),0) AS t FROM ledger WHERE invoice_id=? AND kind='reversal'", (invoice_id,))['t']
    if inv['status'] == 'settled':
        checks['settled_fully_captured'] = captured == inv['ticks'] and reversed_ == 0
    elif inv['status'] == 'reversed':
        checks['fully_compensated'] = captured == reversed_
    else:
        checks['within_total'] = captured <= inv['ticks']
    bank_refs = store.rows(db, "SELECT DISTINCT ref FROM ledger WHERE invoice_id=? AND kind='bank_capture'", (invoice_id,))
    checks['bank_receipts_recorded'] = all((store.row(db, 'SELECT bank_txn FROM bank_requests WHERE id=?', (b['ref'],)) or {}).get('bank_txn') for b in bank_refs)
    return {'invoice': invoice_id, 'status': inv['status'], 'consistent': all(checks.values()), 'checks': checks}


def check_invariants(db):
    """全局不变量:账户不为负、预留不超发票、担保不超额、已结清即足额捕获。"""
    ensure_schema(db)
    problems = []
    for a in store.rows(db, 'SELECT * FROM prepaid_accounts'):
        if a['balance'] < 0 or a['held'] < 0 or a['balance'] - a['held'] < 0:
            problems.append(('prepaid_negative', a['tenant']))
    for o in store.rows(db, 'SELECT * FROM org_accounts'):
        if o['balance'] < 0 or o['held'] < 0 or o['balance'] - o['held'] < 0:
            problems.append(('org_negative', o['org']))
    for inv in store.rows(db, 'SELECT * FROM invoices'):
        if _active_total(db, inv['id']) > inv['ticks']:
            problems.append(('over_invoice', inv['id']))
        if inv['status'] == 'settled':
            captured = store.row(db, "SELECT COALESCE(SUM(amount),0) AS t FROM reservations WHERE invoice_id=? AND status='captured'", (inv['id'],))['t']
            if captured != inv['ticks']:
                problems.append(('settled_mismatch', inv['id']))
    for g in store.rows(db, 'SELECT * FROM guarantees'):
        if _guarantee_usage(db, g['id']) > g['max_amount']:
            problems.append(('guarantee_overrun', g['id']))
    return problems


# ---------------------------------------------------------------- simulated bank

class MockBank:
    """本地模拟银行:独立持久化文件,与主库分属不同故障域;按请求号幂等。

    mode='timeout_before' 模拟尚未发起即超时;mode='timeout_after' 模拟已扣款但回执丢失。
    """

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            'CREATE TABLE IF NOT EXISTS requests(request_id TEXT PRIMARY KEY, tenant TEXT, amount INTEGER, status TEXT NOT NULL, txn TEXT);'
            'CREATE TABLE IF NOT EXISTS txns(txn TEXT PRIMARY KEY, request_id TEXT, tenant TEXT, amount INTEGER NOT NULL, kind TEXT NOT NULL, at INTEGER NOT NULL);')

    def close(self):
        self.db.close()

    def debit(self, request_id, tenant, amount, at, mode=None):
        req = self.db.execute('SELECT status, txn FROM requests WHERE request_id=?', (request_id,)).fetchone()
        if req:
            if req[0] == 'voided':
                raise BankError('request_voided')
            return {'bank_txn': req[1], 'amount': amount, 'request': request_id, 'replayed': True}
        if mode == 'timeout_before':
            raise BankTimeout(request_id)
        txn = 'txn-' + request_id
        self.db.execute('INSERT INTO requests VALUES(?,?,?,?,?)', (request_id, tenant, amount, 'debited', txn))
        self.db.execute('INSERT INTO txns VALUES(?,?,?,?,?,?)', (txn, request_id, tenant, amount, 'debit', at))
        self.db.commit()
        if mode == 'timeout_after':
            raise BankTimeout(request_id)
        return {'bank_txn': txn, 'amount': amount, 'request': request_id, 'replayed': False}

    def inquire(self, request_id=None, bank_txn=None):
        if bank_txn is not None:
            t = self.db.execute("SELECT txn, amount FROM txns WHERE txn=? AND kind='debit'", (bank_txn,)).fetchone()
            return {'status': 'debited', 'bank_txn': t[0], 'amount': t[1]} if t else {'status': 'not_found'}
        req = self.db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
        if not req:
            return {'status': 'not_found'}
        if req[3] == 'voided':
            return {'status': 'voided'}
        return {'status': 'debited', 'bank_txn': req[4], 'amount': req[2], 'refunded': req[3] == 'refunded'}

    def void(self, request_id, at):
        req = self.db.execute('SELECT status FROM requests WHERE request_id=?', (request_id,)).fetchone()
        if req and req[0] in ('debited', 'refunded'):
            raise BankError('already_debited')
        if not req:
            self.db.execute('INSERT INTO requests VALUES(?,?,?,?,?)', (request_id, None, None, 'voided', None))
            self.db.commit()
        return {'status': 'voided'}

    def refund(self, request_id, at):
        req = self.db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
        if not req or req[3] == 'voided':
            raise BankError('not_debited')
        txn = 'txn-' + request_id + '-refund'
        if req[3] == 'refunded':
            return {'bank_txn': txn, 'replayed': True}
        self.db.execute('INSERT INTO txns VALUES(?,?,?,?,?,?)', (txn, request_id, req[1], req[2], 'refund', at))
        self.db.execute("UPDATE requests SET status='refunded' WHERE request_id=?", (request_id,))
        self.db.commit()
        return {'bank_txn': txn}

    def ledger(self):
        return [dict(zip(('txn', 'request_id', 'tenant', 'amount', 'kind', 'at'), t))
                for t in self.db.execute('SELECT * FROM txns ORDER BY rowid')]

    def net(self, tenant):
        debit = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM txns WHERE tenant=? AND kind='debit'", (tenant,)).fetchone()[0]
        refund = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM txns WHERE tenant=? AND kind='refund'", (tenant,)).fetchone()[0]
        return debit - refund
