"""Provisional tenant invoice from captured ticks."""

def issue(db,invoice_id,tenant):
    db.execute('CREATE TABLE IF NOT EXISTS invoices (id TEXT PRIMARY KEY,tenant TEXT,ticks INTEGER)')
    value=db.execute('SELECT COALESCE(SUM(ticks),0) FROM meter_rows WHERE tenant=?',(tenant,)).fetchone()[0]
    with db:db.execute('INSERT INTO invoices VALUES (?,?,?)',(invoice_id,tenant,value))
    return {'id':invoice_id,'tenant':tenant,'ticks':value}
