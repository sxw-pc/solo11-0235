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
CREATE TABLE IF NOT EXISTS rdatasets(id TEXT PRIMARY KEY, origin TEXT NOT NULL, category TEXT NOT NULL, fields TEXT NOT NULL, digest TEXT, producer_job TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rnodes(node TEXT PRIMARY KEY, jurisdiction TEXT NOT NULL, controlled INTEGER NOT NULL, categories TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rlicenses(dataset TEXT NOT NULL, version TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL, signed INTEGER NOT NULL, signers TEXT NOT NULL, required_signers TEXT NOT NULL, granted_at INTEGER, revoked_at INTEGER, revoke_reason TEXT, PRIMARY KEY(dataset,version));
CREATE TABLE IF NOT EXISTS rmanifests(id TEXT PRIMARY KEY, job TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rmanifest_entries(manifest TEXT NOT NULL, dataset TEXT NOT NULL, license_version TEXT NOT NULL, dataset_digest TEXT, PRIMARY KEY(manifest,dataset));
CREATE TABLE IF NOT EXISTS rjob_manifest(job TEXT PRIMARY KEY, manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rattempts(job TEXT NOT NULL, generation INTEGER NOT NULL, manifest TEXT NOT NULL, PRIMARY KEY(job,generation));
CREATE TABLE IF NOT EXISTS rcopies(id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, generation INTEGER NOT NULL, dataset TEXT NOT NULL, location TEXT NOT NULL, jurisdiction TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT, fields TEXT, authorized INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, deleted_at INTEGER, violation TEXT);
CREATE TABLE IF NOT EXISTS rviolations(id TEXT PRIMARY KEY, job TEXT NOT NULL, generation INTEGER NOT NULL, dataset TEXT NOT NULL, location TEXT NOT NULL, jurisdiction TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL, at INTEGER NOT NULL, manifest TEXT, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rtransforms(job TEXT NOT NULL, generation INTEGER NOT NULL, input_dataset TEXT NOT NULL, output_dataset TEXT NOT NULL, output_digest TEXT NOT NULL, ttype TEXT NOT NULL, proof TEXT NOT NULL, verified INTEGER NOT NULL, at INTEGER NOT NULL, PRIMARY KEY(job,generation,output_digest));
CREATE TABLE IF NOT EXISTS regress(job TEXT NOT NULL, generation INTEGER NOT NULL, manifest TEXT NOT NULL, digest TEXT NOT NULL, at INTEGER NOT NULL, licenses TEXT NOT NULL, PRIMARY KEY(job,generation));
CREATE TABLE IF NOT EXISTS regres(id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, generation INTEGER NOT NULL, digest TEXT NOT NULL, recipient TEXT NOT NULL, jurisdiction TEXT NOT NULL, classification TEXT NOT NULL, authorized INTEGER NOT NULL, manifest TEXT, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rdeletion_duties(id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, dataset TEXT NOT NULL, due INTEGER NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, discharged_at INTEGER);
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
