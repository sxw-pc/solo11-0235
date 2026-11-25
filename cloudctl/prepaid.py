"""Debit a tenant prepaid account for a written invoice."""

def open_account(db,tenant,balance):
    db.execute('CREATE TABLE IF NOT EXISTS prepaid_accounts (tenant TEXT PRIMARY KEY,balance INTEGER)')
    with db:db.execute('INSERT INTO prepaid_accounts VALUES (?,?)',(tenant,balance))

def debit(db,invoice_id):
    row=db.execute('SELECT tenant,ticks FROM invoices WHERE id=?',(invoice_id,)).fetchone()
    if not row:raise ValueError('invoice_missing')
    tenant,amount=row
    with db:
        updated=db.execute('UPDATE prepaid_accounts SET balance=balance-? WHERE tenant=? AND balance>=?',(amount,tenant,amount))
        if not updated.rowcount:raise ValueError('insufficient_prepaid')
    balance=db.execute('SELECT balance FROM prepaid_accounts WHERE tenant=?',(tenant,)).fetchone()[0]
    return {'tenant':tenant,'balance':balance}
