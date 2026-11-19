import json
import sqlite3
from datetime import datetime, timezone


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def migrate(web):
    with web.db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_documents(
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL,
          payload BLOB NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_jobs(
          id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
          status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_items(
          id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
          payload BLOB NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
          error TEXT NOT NULL DEFAULT '', lease_until REAL, lease_owner TEXT,
          attempted_at TEXT, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS v2_pending ON v2_items(status,updated_at);
        CREATE INDEX IF NOT EXISTS v2_job_items ON v2_items(job_id);
        CREATE TABLE IF NOT EXISTS v2_workers(name TEXT PRIMARY KEY, seen_at TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_inventory(
          profile TEXT PRIMARY KEY, account_id TEXT, partition TEXT, fetched_at TEXT NOT NULL,
          items TEXT NOT NULL, errors TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_account_settings(profile TEXT PRIMARY KEY, group_name TEXT NOT NULL, enabled INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_reports(name TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_rule_state(
          rule_id TEXT NOT NULL, target_key TEXT NOT NULL, streak INTEGER NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 0, last_bucket TEXT, last_trigger TEXT,
          cycle INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(rule_id,target_key));
        CREATE TABLE IF NOT EXISTS v2_rule_flags(rule_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_events(
          id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, target TEXT NOT NULL, created_at TEXT NOT NULL,
          value REAL NOT NULL, job_id TEXT, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v2_deliveries(
          id TEXT PRIMARY KEY, message TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0);
        ''')
        c.execute('INSERT OR IGNORE INTO schema_versions VALUES(1,?)', (stamp(),))
        for row in c.execute("SELECT id,payload FROM v2_documents WHERE kind='rule'").fetchall():
            payload = json.loads(web.vault().decrypt(row['payload']))
            c.execute('INSERT OR IGNORE INTO v2_rule_flags VALUES(?,?)', (row['id'], int(payload['enabled'])))


def heartbeat(web, name, detail=''):
    with web.db() as c:
        c.execute('INSERT OR REPLACE INTO v2_workers VALUES(?,?,?)', (name, stamp(), detail))


def documents(web, kind):
    with web.db() as c:
        rows = c.execute('SELECT * FROM v2_documents WHERE kind=? ORDER BY updated_at DESC', (kind,)).fetchall()
    return [dict(r) | {'payload': json.loads(web.vault().decrypt(r['payload']))} for r in rows]


def document(web, identifier, kind=None):
    from fastapi import HTTPException
    with web.db() as c:
        row = c.execute('SELECT * FROM v2_documents WHERE id=?', (identifier,)).fetchone()
    if row is None or (kind and row['kind'] != kind):
        raise HTTPException(404, 'Configuration not found')
    return dict(row) | {'payload': json.loads(web.vault().decrypt(row['payload']))}
