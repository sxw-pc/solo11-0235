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
CREATE TABLE IF NOT EXISTS nodes(node TEXT PRIMARY KEY, cpu INTEGER NOT NULL, memory INTEGER NOT NULL, heartbeat INTEGER NOT NULL, healthy INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS leases(job TEXT PRIMARY KEY, slot TEXT NOT NULL, generation INTEGER NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL, debt INTEGER NOT NULL DEFAULT 0, granted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS attempts(job TEXT NOT NULL, generation INTEGER NOT NULL, status TEXT NOT NULL, started INTEGER NOT NULL, ended INTEGER, digest TEXT, PRIMARY KEY(job,generation));
CREATE TABLE IF NOT EXISTS artifacts(job TEXT PRIMARY KEY, digest TEXT NOT NULL, delivered INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS retentions(job TEXT PRIMARY KEY, expires INTEGER NOT NULL, hold INTEGER NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL, decision TEXT NOT NULL, evidence TEXT NOT NULL);
"""

def _migrate(db):
    # Older libraries predate the nodes table and the leases.granted column.
    # Add them in place so old databases and their event history stay open and
    # auditable; existing rows and events are never rewritten.
    db.executescript(
        'CREATE TABLE IF NOT EXISTS nodes(node TEXT PRIMARY KEY, cpu INTEGER NOT NULL, '
        'memory INTEGER NOT NULL, heartbeat INTEGER NOT NULL, healthy INTEGER NOT NULL);')
    cols={r[1] for r in db.execute('PRAGMA table_info(leases)')}
    if 'granted' not in cols:
        db.execute('ALTER TABLE leases ADD COLUMN granted INTEGER NOT NULL DEFAULT 0')

def init(path):
    db=sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
        _migrate(db)
        db.commit()
    finally:
        db.close()

@contextmanager
def transaction(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        db.execute('BEGIN IMMEDIATE')
        _migrate(db)
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
