import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager

BASE_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL, decision TEXT NOT NULL, evidence TEXT NOT NULL);
"""

# The extension tables are additive: databases created by older versions of the
# control plane keep every original row and gain these tables in place.
EXTENSION_DDL = (
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);",
    "CREATE TABLE IF NOT EXISTS lineage_seals(seq INTEGER PRIMARY KEY AUTOINCREMENT, child TEXT NOT NULL, parent TEXT NOT NULL, sealed_at INTEGER NOT NULL, payload TEXT NOT NULL, signature TEXT NOT NULL);",
    "CREATE INDEX IF NOT EXISTS idx_lineage_seals_edge ON lineage_seals(child,parent,seq);",
    "CREATE TABLE IF NOT EXISTS decisions(seq INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, payload TEXT NOT NULL, prev TEXT NOT NULL, digest TEXT NOT NULL);",
    "CREATE TABLE IF NOT EXISTS deliveries(job TEXT PRIMARY KEY, digest TEXT NOT NULL, at INTEGER NOT NULL, generation INTEGER NOT NULL, snapshot TEXT NOT NULL);",
    "CREATE TABLE IF NOT EXISTS purges(job TEXT PRIMARY KEY, at INTEGER NOT NULL, decision TEXT NOT NULL, evidence TEXT NOT NULL);",
)

SCHEMA = BASE_SCHEMA + '\n'.join(EXTENSION_DDL)


def _seed_key(db):
    db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('lineage_key',?)", (secrets.token_hex(32),))


def init(path):
    db = sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
        _seed_key(db)
        db.commit()
    finally:
        db.close()


def ensure(db):
    """Create additive tables when opening a database produced by an older build."""
    for statement in EXTENSION_DDL:
        db.execute(statement)
    _seed_key(db)


def lineage_key(db):
    current = row(db, 'SELECT value FROM meta WHERE key=?', ('lineage_key',))
    if not current:
        _seed_key(db)
        current = row(db, 'SELECT value FROM meta WHERE key=?', ('lineage_key',))
    return current['value']


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def signature(db, payload_text):
    """Detached keyed signature over the exact persisted payload bytes."""
    key = lineage_key(db).encode('utf-8')
    return 'hmac-sha256:' + hmac.new(key, payload_text.encode('utf-8'), hashlib.sha256).hexdigest()


@contextmanager
def transaction(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        db.execute('BEGIN IMMEDIATE')
        ensure(db)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def row(db, sql, args=()):
    result = db.execute(sql, args).fetchone()
    return dict(result) if result else None


def rows(db, sql, args=()):
    return [dict(x) for x in db.execute(sql, args)]


def record(db, kind, subject, decision, evidence):
    db.execute(
        'INSERT INTO events(kind,subject,decision,evidence) VALUES(?,?,?,?)',
        (kind, subject, decision, json.dumps(evidence, sort_keys=True, ensure_ascii=False)),
    )
    return {'subject': subject, 'decision': decision, 'evidence': evidence}


def report(db, subject):
    return rows(db, 'SELECT seq,kind,decision,evidence FROM events WHERE subject=? ORDER BY seq', (subject,))


def decision(db, at, kind, subject, payload):
    """Append a hash-chained, recoverable decision record.

    The previous digest is copied into this row, so a restart or a concurrent
    delivery can only extend the log.  Nothing in this module rewrites it.
    """
    at = 0 if at is None else int(at)
    previous = row(db, 'SELECT digest FROM decisions ORDER BY seq DESC LIMIT 1')
    prev = previous['digest'] if previous else 'genesis'
    body = canonical_json({'at': at, 'kind': kind, 'subject': subject, 'payload': payload, 'prev': prev})
    digest = 'sha256:' + hashlib.sha256(body.encode('utf-8')).hexdigest()
    cursor = db.execute(
        'INSERT INTO decisions(at,kind,subject,payload,prev,digest) VALUES(?,?,?,?,?,?)',
        (at, kind, subject, canonical_json(payload), prev, digest),
    )
    return {'seq': cursor.lastrowid, 'kind': kind, 'subject': subject, 'prev': prev, 'digest': digest}


def verify_decisions(db):
    errors = []
    expected_prev = 'genesis'
    count = 0
    head = None
    for item in rows(db, 'SELECT seq,at,kind,subject,payload,prev,digest FROM decisions ORDER BY seq'):
        count += 1
        if item['prev'] != expected_prev:
            errors.append({'seq': item['seq'], 'reason': 'broken_prev'})
        try:
            parsed = json.loads(item['payload'])
        except ValueError:
            errors.append({'seq': item['seq'], 'reason': 'invalid_payload'})
            expected_prev = item['digest']
            head = item['digest']
            continue
        body = canonical_json({
            'at': item['at'],
            'kind': item['kind'],
            'subject': item['subject'],
            'payload': parsed,
            'prev': item['prev'],
        })
        digest = 'sha256:' + hashlib.sha256(body.encode('utf-8')).hexdigest()
        if digest != item['digest']:
            errors.append({'seq': item['seq'], 'reason': 'digest_mismatch'})
        expected_prev = item['digest']
        head = item['digest']
    return {'valid': not errors, 'count': count, 'head': head, 'errors': errors}
