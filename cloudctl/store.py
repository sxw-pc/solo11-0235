import json
import sqlite3
from contextlib import contextmanager

# Map live connections back to their database path so adapters (e.g. the
# license gateway) can bind per-database state without subclassing sqlite3.
_PATHS = {}


def db_path(db):
    return _PATHS.get(id(db))

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
CREATE TABLE IF NOT EXISTS entitlements(id TEXT PRIMARY KEY, product TEXT NOT NULL, version TEXT NOT NULL, region TEXT NOT NULL, tenant TEXT, kind TEXT NOT NULL, seats INTEGER NOT NULL, borrowed_from INTEGER NOT NULL, borrowed_until INTEGER NOT NULL, status TEXT NOT NULL, cancelled_at INTEGER);
CREATE TABLE IF NOT EXISTS windows(entitlement TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL, PRIMARY KEY(entitlement,start_ts));
CREATE TABLE IF NOT EXISTS job_license(job TEXT PRIMARY KEY, product TEXT NOT NULL, version TEXT NOT NULL, region TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY, entitlement TEXT NOT NULL, job TEXT NOT NULL, generation INTEGER NOT NULL, tenant TEXT NOT NULL, responsibility TEXT NOT NULL, request_id TEXT NOT NULL, token TEXT, status TEXT NOT NULL, acquired_at INTEGER, expires_at INTEGER, released_at INTEGER, return_until INTEGER, remote_cancelled INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS grant_windows(grant TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL, entitlement TEXT NOT NULL, PRIMARY KEY(grant,start_ts));
CREATE TABLE IF NOT EXISTS license_receipts(seq INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL, op TEXT NOT NULL, grant TEXT, entitlement TEXT, token TEXT, status TEXT NOT NULL, payload TEXT NOT NULL, received_at INTEGER NOT NULL, stale INTEGER NOT NULL DEFAULT 0, UNIQUE(receipt_id,op));
CREATE TABLE IF NOT EXISTS license_corrections(seq INTEGER PRIMARY KEY AUTOINCREMENT, correction_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, grant TEXT, entitlement TEXT, reference TEXT NOT NULL, at INTEGER NOT NULL, payload TEXT NOT NULL, superseded_by TEXT);
CREATE TABLE IF NOT EXISTS attempt_seats(job TEXT NOT NULL, generation INTEGER NOT NULL, grant TEXT NOT NULL, token TEXT NOT NULL, PRIMARY KEY(job,generation));
CREATE TABLE IF NOT EXISTS delivery_evidence(job TEXT PRIMARY KEY, grant TEXT NOT NULL, token TEXT NOT NULL, receipt_id TEXT NOT NULL, at INTEGER NOT NULL);
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
    _PATHS[id(db)]=path
    db.execute('PRAGMA foreign_keys=ON')
    try:
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        _PATHS.pop(id(db),None)
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
