"""Compatibility wrappers for the original one-shot envelope API.

The real logic lives in :mod:`cloudctl.secret_rotation`, which keeps the
versioned envelopes, evidence-gated short-lived mounts and delivery
eligibility.  These wrappers preserve the historic call shapes while routing
through the same gates, so legacy callers cannot bypass rotation controls.
"""
from . import secret_rotation as sr
from .store import record, row


def register(db, job, key_id, version, sealed_digest):
    sr._ensure_tables(db)
    result = sr.register_envelope(db, job, key_id, version, sealed_digest, 0)
    # Keep the legacy one-row table for any remaining direct readers.
    if result['decision'] in ('registered', 'idempotent'):
        db.execute('CREATE TABLE IF NOT EXISTS secret_envelopes (job TEXT PRIMARY KEY, key_id TEXT NOT NULL,'
                   ' version INTEGER NOT NULL, sealed_digest TEXT NOT NULL)')
        db.execute('INSERT OR REPLACE INTO secret_envelopes VALUES(?,?,?,?)',
                   (job, key_id, version, sealed_digest))
    return result


def mount(db, job, generation, at):
    sr._ensure_tables(db)
    if not row(db, "SELECT 1 FROM secret_envelope_versions WHERE job=?", (job,)):
        return record(db, 'secret_mount', job, 'unavailable', {'generation': generation})
    return sr.issue_mount(db, job, generation, at)