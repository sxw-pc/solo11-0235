"""Register a tenant sealed envelope and issue its current job mount."""
from .store import row, record


def register(db, job, key_id, version, sealed_digest):
    if not row(db, 'SELECT id FROM jobs WHERE id=?', (job,)):
        return record(db, 'secret', job, 'missing_job', {})
    db.execute('CREATE TABLE IF NOT EXISTS secret_envelopes (job TEXT PRIMARY KEY, key_id TEXT NOT NULL, version INTEGER NOT NULL, sealed_digest TEXT NOT NULL)')
    old = row(db, 'SELECT * FROM secret_envelopes WHERE job=?', (job,))
    if old:
        return record(db, 'secret', job, 'idempotent' if old['key_id']==key_id and old['version']==version and old['sealed_digest']==sealed_digest else 'conflict', {})
    db.execute('INSERT INTO secret_envelopes VALUES (?,?,?,?)', (job, key_id, version, sealed_digest))
    return record(db, 'secret', job, 'registered', {'key_id':key_id,'version':version})


def mount(db, job, generation, at):
    db.execute('CREATE TABLE IF NOT EXISTS secret_envelopes (job TEXT PRIMARY KEY, key_id TEXT NOT NULL, version INTEGER NOT NULL, sealed_digest TEXT NOT NULL)')
    envelope = row(db, 'SELECT * FROM secret_envelopes WHERE job=?', (job,))
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (job,))
    if not envelope or not lease or lease['status']!='active' or lease['generation']!=generation or at>lease['expires']:
        return record(db, 'secret_mount', job, 'unavailable', {'generation':generation})
    return record(db, 'secret_mount', job, 'issued', {'key_id':envelope['key_id'],'version':envelope['version'],'sealed_digest':envelope['sealed_digest'],'generation':generation})
