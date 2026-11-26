import json
import sqlite3
from contextlib import contextmanager

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, deadline INTEGER NOT NULL, duration INTEGER NOT NULL, cpu INTEGER NOT NULL, memory INTEGER NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS dependencies(job TEXT NOT NULL, parent TEXT NOT NULL, PRIMARY KEY(job,parent));
CREATE TABLE IF NOT EXISTS cancellations(job TEXT PRIMARY KEY, generation INTEGER NOT NULL, reason TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS slots(id TEXT PRIMARY KEY, cpu INTEGER NOT NULL, memory INTEGER NOT NULL, node TEXT NOT NULL, healthy INTEGER NOT NULL, heartbeat INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS leases(job TEXT PRIMARY KEY, slot TEXT NOT NULL, generation INTEGER NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL, debt INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS attempts(job TEXT NOT NULL, generation INTEGER NOT NULL, status TEXT NOT NULL, started INTEGER NOT NULL, ended INTEGER, digest TEXT, PRIMARY KEY(job,generation));
CREATE TABLE IF NOT EXISTS artifacts(job TEXT PRIMARY KEY, digest TEXT NOT NULL, delivered INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS retentions(job TEXT PRIMARY KEY, expires INTEGER NOT NULL, hold INTEGER NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secret_envelope_versions(envelope_id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, tenant TEXT NOT NULL, key_id TEXT NOT NULL, version INTEGER NOT NULL, sealed_digest TEXT NOT NULL, status TEXT NOT NULL, rotated_from INTEGER, registered_at INTEGER NOT NULL, UNIQUE(job,key_id,version));
CREATE TABLE IF NOT EXISTS secret_revocations(id INTEGER PRIMARY KEY AUTOINCREMENT, key_id TEXT NOT NULL, version INTEGER NOT NULL, tenant TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, at INTEGER NOT NULL, UNIQUE(key_id,version,reason));
CREATE TABLE IF NOT EXISTS secret_mounts(mount_id TEXT PRIMARY KEY, job TEXT NOT NULL, tenant TEXT NOT NULL, key_id TEXT NOT NULL, version INTEGER NOT NULL, lease_generation INTEGER NOT NULL, slot TEXT, issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, ttl INTEGER NOT NULL, status TEXT NOT NULL, revocation_id INTEGER, recovery_id INTEGER, envelope_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kms_revocations(receipt_id TEXT PRIMARY KEY, key_id TEXT NOT NULL, version INTEGER NOT NULL, mount_id TEXT, slot TEXT, action TEXT NOT NULL, confirmed_at INTEGER NOT NULL, duplicated INTEGER NOT NULL DEFAULT 0, claim TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS container_attestations(attestation_id TEXT PRIMARY KEY, slot TEXT NOT NULL, job TEXT NOT NULL, key_id TEXT NOT NULL, version INTEGER NOT NULL, verdict TEXT NOT NULL, issued_at INTEGER NOT NULL, claim TEXT NOT NULL, UNIQUE(slot,job,key_id,version));
CREATE TABLE IF NOT EXISTS rotation_intents(intent_id TEXT PRIMARY KEY, job TEXT NOT NULL, key_id TEXT NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, at INTEGER NOT NULL, decision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_points(recovery_id TEXT PRIMARY KEY, job TEXT NOT NULL, lease_generation INTEGER NOT NULL, slot TEXT NOT NULL, key_id TEXT NOT NULL, version INTEGER NOT NULL, point_digest TEXT NOT NULL, tainted INTEGER NOT NULL, created_at INTEGER NOT NULL, parent_recovery_id TEXT);
CREATE TABLE IF NOT EXISTS attempt_lineage(lineage_seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, generation INTEGER NOT NULL, mount_id TEXT, key_id TEXT, version INTEGER, taint INTEGER NOT NULL DEFAULT 0, recovery_id TEXT, UNIQUE(job,generation,mount_id));
CREATE TABLE IF NOT EXISTS delivery_eligibility(job TEXT PRIMARY KEY, eligible INTEGER NOT NULL, tainted_versions TEXT NOT NULL, evidence TEXT NOT NULL, decided_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS clean_recomputations(job TEXT NOT NULL, generation INTEGER NOT NULL, confirmed_at INTEGER NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(job,generation));
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL, decision TEXT NOT NULL, evidence TEXT NOT NULL);
"""

def init(path):
    db=sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
    finally:
        db.close()

@contextmanager
def transaction(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def row(db,sql,args=()):
    result=db.execute(sql,args).fetchone()
    return dict(result) if result else None

def rows(db,sql,args=()):
    return [dict(x) for x in db.execute(sql,args)]

def record(db,kind,subject,decision,evidence):
    db.execute('INSERT INTO events(kind,subject,decision,evidence) VALUES(?,?,?,?)',(kind,subject,decision,json.dumps(evidence,sort_keys=True,ensure_ascii=False)))
    return {'subject':subject,'decision':decision,'evidence':evidence}

def report(db,subject):
    return rows(db,'SELECT seq,kind,decision,evidence FROM events WHERE subject=? ORDER BY seq',(subject,))
