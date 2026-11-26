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
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL, decision TEXT NOT NULL, evidence TEXT NOT NULL);
-- Claim adjudication and remedy subsystem (cloudctl.claims)
CREATE TABLE IF NOT EXISTS claims(
  id TEXT PRIMARY KEY,
  tenant TEXT,
  job TEXT NOT NULL,
  job_version INTEGER,
  deadline INTEGER,
  contract_version TEXT,
  contract_json TEXT NOT NULL DEFAULT '{}',
  promise_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  liability TEXT,
  liability_reason TEXT,
  remedy_kind TEXT,
  remedy_id TEXT,
  evidence_version INTEGER NOT NULL DEFAULT 0,
  decided_version INTEGER,
  idempotency_key TEXT,
  created_at INTEGER,
  decided_at INTEGER,
  remedied_at INTEGER,
  revoked_at INTEGER,
  revoke_reason TEXT,
  raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS claim_evidence(
  claim TEXT NOT NULL,
  eid TEXT NOT NULL,
  version INTEGER NOT NULL,
  kind TEXT NOT NULL,
  event_at INTEGER,
  received_at INTEGER,
  verifiable INTEGER NOT NULL DEFAULT 0,
  verified INTEGER NOT NULL DEFAULT 0,
  digest TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  raw TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(claim,eid)
);
CREATE TABLE IF NOT EXISTS remedies(
  id TEXT PRIMARY KEY,
  claim TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  option_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  slot TEXT,
  lease_generation INTEGER,
  rerun_job TEXT,
  new_deadline INTEGER,
  credit_id TEXT,
  idempotency_key TEXT UNIQUE,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER,
  updated_at INTEGER,
  fulfilled_at INTEGER,
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS credits(
  id TEXT PRIMARY KEY,
  tenant TEXT NOT NULL,
  owner TEXT NOT NULL,
  claim TEXT,
  amount INTEGER NOT NULL,
  unit TEXT NOT NULL DEFAULT 'time',
  transferable INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  source TEXT,
  redeemed_by_job TEXT,
  idempotency_key TEXT UNIQUE,
  issued_at INTEGER,
  redeemed_at INTEGER,
  raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS credit_transfers(
  id TEXT PRIMARY KEY,
  credit TEXT NOT NULL,
  from_tenant TEXT,
  to_tenant TEXT NOT NULL,
  at INTEGER,
  idempotency_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS time_balances(
  tenant TEXT PRIMARY KEY,
  purchased INTEGER NOT NULL DEFAULT 0,
  used INTEGER NOT NULL DEFAULT 0,
  reserved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS slot_reservations(
  slot TEXT NOT NULL,
  claim TEXT NOT NULL,
  job TEXT,
  status TEXT NOT NULL,
  at INTEGER,
  expires INTEGER,
  PRIMARY KEY(slot,claim)
);
CREATE UNIQUE INDEX IF NOT EXISTS slot_reservations_one_held ON slot_reservations(slot) WHERE status='held';
CREATE TABLE IF NOT EXISTS deadline_revisions(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  job TEXT NOT NULL,
  claim TEXT,
  old_deadline INTEGER,
  new_deadline INTEGER,
  old_version INTEGER,
  new_version INTEGER,
  at INTEGER,
  reason TEXT
);
CREATE VIEW IF NOT EXISTS vouchers AS SELECT id AS voucher_id,id AS credit_id,tenant,owner,claim,amount,unit,transferable,status,redeemed_by_job AS job,issued_at,redeemed_at FROM credits;
CREATE VIEW IF NOT EXISTS claim_proofs AS SELECT claim,eid AS evidence_id,version,kind,event_at AS at,received_at,verifiable,verified,digest,payload_json AS payload,raw FROM claim_evidence;
CREATE VIEW IF NOT EXISTS claim AS SELECT * FROM claims;
CREATE VIEW IF NOT EXISTS appeal AS SELECT * FROM claims;
CREATE VIEW IF NOT EXISTS appeals AS SELECT * FROM claims;
CREATE VIEW IF NOT EXISTS complaint AS SELECT * FROM claims;
CREATE VIEW IF NOT EXISTS evidence AS SELECT claim,eid AS id,eid AS evidence_id,version,kind,event_at AS at,received_at,verifiable,verified,digest,payload_json AS payload,raw FROM claim_evidence;
CREATE VIEW IF NOT EXISTS proofs AS SELECT * FROM claim_proofs;
CREATE VIEW IF NOT EXISTS remedy AS SELECT * FROM remedies;
CREATE VIEW IF NOT EXISTS claim_remedies AS SELECT * FROM remedies;
CREATE VIEW IF NOT EXISTS remediations AS SELECT * FROM remedies;
CREATE VIEW IF NOT EXISTS credit_vouchers AS SELECT *, id AS voucher_id FROM credits;
CREATE VIEW IF NOT EXISTS credit_ledger AS SELECT id,claim,tenant,owner,amount,unit,status,issued_at AS at,'issue' AS entry FROM credits;
CREATE VIEW IF NOT EXISTS claim_events AS SELECT seq,subject AS claim_id,subject AS claim,kind,decision,evidence FROM events WHERE kind IN ('claim','evidence','adjudicate','remedy','remedy_select','credit','credit_redeem','credit_transfer','revoke','reschedule','time');
"""

def init(path):
    db=sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
    finally:
        db.close()

@contextmanager
def transaction(path):
    db=sqlite3.connect(path,timeout=30.0)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
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
