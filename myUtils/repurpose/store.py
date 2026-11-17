"""Additive repurpose persistence; no publish-table resets and no import-time work."""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .contracts import HASH, digest, json_bytes, loads, require, text, validate_deliveries, validate_source
from .files import private_path

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS repurpose_kits (
        version TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, config_json TEXT NOT NULL, created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS repurpose_tasks (
        id INTEGER PRIMARY KEY, workspace_id TEXT NOT NULL, request_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, request_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0, reason_code TEXT, lease_token TEXT, lease_expires_at TEXT,
        package_version TEXT, manifest_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(workspace_id,request_id))''',
    '''CREATE TABLE IF NOT EXISTS repurpose_deliveries (
        release_id TEXT NOT NULL, delivery_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        package_version TEXT NOT NULL, request_json TEXT NOT NULL, state TEXT NOT NULL,
        receipt_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(release_id,delivery_id))''',
    'CREATE INDEX IF NOT EXISTS repurpose_task_queue ON repurpose_tasks(state,id)',
    '''CREATE TABLE IF NOT EXISTS repurpose_assets (
        task_id INTEGER NOT NULL REFERENCES repurpose_tasks(id) ON DELETE RESTRICT,
        asset_name TEXT NOT NULL,
        media_asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT,
        file_record_id INTEGER NOT NULL REFERENCES file_records(id) ON DELETE RESTRICT,
        media_group_id INTEGER NOT NULL REFERENCES media_groups(id) ON DELETE RESTRICT,
        PRIMARY KEY(task_id,asset_name))''',
    '''CREATE TABLE IF NOT EXISTS repurpose_releases (
        release_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
        task_id INTEGER NOT NULL REFERENCES repurpose_tasks(id) ON DELETE RESTRICT,
        review_version TEXT NOT NULL, package_version TEXT NOT NULL,
        manifest_json TEXT NOT NULL, media_root TEXT NOT NULL, public_base_url TEXT NOT NULL,
        account_workspace_json TEXT NOT NULL, created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS repurpose_dispatch_grants (
        release_id TEXT PRIMARY KEY REFERENCES repurpose_releases(release_id) ON DELETE RESTRICT,
        workspace_id TEXT NOT NULL, ticket_sha256 TEXT NOT NULL, ticket_path TEXT NOT NULL,
        observed_at TEXT NOT NULL, created_at TEXT NOT NULL)''',
)


def install_schema(conn):
    for statement in SCHEMA:
        conn.execute(statement)


def utc(value=None):
    value = value or datetime.now(timezone.utc)
    require(isinstance(value, datetime) and value.tzinfo is not None, 'aware_clock_required')
    return value.astimezone(timezone.utc)


def stamp(value=None):
    return utc(value).isoformat(timespec='microseconds')


class Store:
    def __init__(self, db_path, root):
        self.db_path = Path(db_path)
        self.root = private_path(root)
        require(self.db_path.is_file() and self.root.is_dir(), 'repurpose_storage_not_configured')

    @contextmanager
    def connect(self, *, write=False):
        conn = sqlite3.connect(self.db_path if write else self.db_path.resolve().as_uri() + '?mode=ro', uri=not write, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA busy_timeout=15000')
            if write:
                conn.execute('BEGIN IMMEDIATE')
            else:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
            yield conn
            if write:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_kit(self, workspace, config, *, now=None):
        from .brand import validate_kit
        config = validate_kit(config)
        version = digest(config)
        with self.connect(write=True) as conn:
            old = conn.execute('SELECT * FROM repurpose_kits WHERE version=?', (version,)).fetchone()
            if old:
                require(old['workspace_id'] == workspace and loads(old['config_json']) == config, 'kit_scope_conflict')
            else:
                conn.execute('INSERT INTO repurpose_kits VALUES(?,?,?,?)', (version, workspace, json_bytes(config).decode(), stamp(now)))
        return version

    def kit(self, workspace, version):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM repurpose_kits WHERE workspace_id=? AND version=?', (workspace, version)).fetchone()
            require(row is not None, 'kit_not_found')
            return loads(row['config_json'])

    def kits(self, workspace):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT version,created_at,json_extract(config_json,'$.theme') AS theme FROM repurpose_kits WHERE workspace_id=? ORDER BY created_at DESC LIMIT 100", (workspace,))]

    def enqueue(self, workspace, request_id, source, kit_version, *, deliveries=None, now=None):
        require(text(workspace, 200) and text(request_id, 100) and HASH.fullmatch(str(kit_version)), 'preparation_identity')
        self.kit(workspace, kit_version)
        request = {'source': validate_source(source), 'kit_version': kit_version,
                   'deliveries': validate_deliveries(deliveries) if deliveries is not None else validate_deliveries(['43:cards', '43:video', '58:cards', '58:video', '61:cards', '125:cards'])}
        identity = digest(request)
        with self.connect(write=True) as conn:
            old = conn.execute('SELECT * FROM repurpose_tasks WHERE workspace_id=? AND request_id=?', (workspace, request_id)).fetchone()
            if old:
                require(old['request_sha256'] == identity, 'request_id_material_changed')
                return dict(old)
            moment = stamp(now)
            cursor = conn.execute('''INSERT INTO repurpose_tasks(workspace_id,request_id,request_sha256,request_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?)''', (workspace, request_id, identity, json_bytes(request).decode(), moment, moment))
            return dict(conn.execute('SELECT * FROM repurpose_tasks WHERE id=?', (cursor.lastrowid,)).fetchone())

    def task(self, workspace, task_id):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM repurpose_tasks WHERE workspace_id=? AND id=?', (workspace, task_id)).fetchone()
            require(row is not None, 'task_not_found')
            return dict(row)

    def tasks(self, workspace):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM repurpose_tasks WHERE workspace_id=? ORDER BY id DESC LIMIT 100', (workspace,))]

    def claim(self, *, now=None, lease_seconds=2100):
        require(type(lease_seconds) is int and 60 <= lease_seconds <= 3600, 'lease_bound')
        moment = utc(now)
        with self.connect(write=True) as conn:
            # A crashed preparation is not automatically rerun under the same files/credits.
            conn.execute("UPDATE repurpose_tasks SET state='held',reason_code='preparation_interrupted',lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE state='running' AND lease_expires_at<=?", (stamp(moment), stamp(moment)))
            if conn.execute("SELECT 1 FROM repurpose_tasks WHERE state='running'").fetchone():
                return None
            row = conn.execute("SELECT * FROM repurpose_tasks WHERE state='queued' AND attempts<3 ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            conn.execute("UPDATE repurpose_tasks SET state='running',attempts=attempts+1,lease_token=?,lease_expires_at=?,updated_at=? WHERE id=? AND state='queued'", (token, stamp(moment + timedelta(seconds=lease_seconds)), stamp(moment), row['id']))
            return dict(conn.execute('SELECT * FROM repurpose_tasks WHERE id=?', (row['id'],)).fetchone())

    def finish(self, task, *, manifest_path=None, reason=None, now=None):
        manifest = None
        if manifest_path is not None:
            from .files import verify_package
            manifest = verify_package(manifest_path)
            require(private_path(manifest_path, root=self.root).is_file(), 'package_scope')
            request = loads(task['request_json'])
            require(manifest['material']['source'] == request['source'] and manifest['material']['kit_version'] == request['kit_version']
                    and [d['id'] for d in manifest['material']['deliveries']] == request['deliveries'], 'prepared_material_changed')
        require((manifest is not None) != (reason is not None), 'finish_outcome')
        moment = stamp(now)
        with self.connect(write=True) as conn:
            row = conn.execute('SELECT * FROM repurpose_tasks WHERE id=?', (task['id'],)).fetchone()
            require(row is not None and row['state'] == 'running' and row['lease_token'] == task['lease_token'] and row['lease_expires_at'] > moment, 'lease_lost')
            require(row['workspace_id'] == task['workspace_id'] and row['request_sha256'] == task['request_sha256'], 'lease_lost')
            if manifest is not None:
                from .registration import register_package
                require(register_package(conn, task, manifest_path, moment) == manifest, 'prepared_files_changed')
            conn.execute('''UPDATE repurpose_tasks SET state=?,reason_code=?,package_version=?,manifest_path=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?''',
                         ('prepared' if manifest else 'held', reason, manifest['package_version'] if manifest else None, str(manifest_path) if manifest else None, moment, task['id']))

    def cancel(self, workspace, task_id, *, now=None):
        with self.connect(write=True) as conn:
            row = conn.execute('SELECT * FROM repurpose_tasks WHERE workspace_id=? AND id=?', (workspace, task_id)).fetchone()
            require(row is not None and row['state'] in {'queued', 'running', 'held'}, 'cannot_cancel_prepared_or_missing')
            conn.execute("UPDATE repurpose_tasks SET state='cancelled',reason_code='cancelled',lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?", (stamp(now), task_id))

    def delivery_intent(self, workspace, release_id, package_version, delivery, *, now=None):
        """Trusted adapter only. Persist before possible external enqueue; not an approval API."""
        require(text(release_id, 100) and HASH.fullmatch(str(package_version)), 'release_identity')
        with self.connect(write=True) as conn:
            old = conn.execute('SELECT * FROM repurpose_deliveries WHERE release_id=? AND delivery_id=?', (release_id, delivery['id'])).fetchone()
            request = json_bytes(delivery).decode()
            if old:
                require(old['workspace_id'] == workspace and old['package_version'] == package_version and old['request_json'] == request, 'release_identity_changed')
                return False, dict(old)
            moment = stamp(now)
            conn.execute('INSERT INTO repurpose_deliveries VALUES(?,?,?,?,?,?,?,?,?)', (release_id, delivery['id'], workspace, package_version, request, 'intent', None, moment, moment))
            return True, dict(conn.execute('SELECT * FROM repurpose_deliveries WHERE release_id=? AND delivery_id=?', (release_id, delivery['id'])).fetchone())

    def record_receipt(self, release_id, delivery_id, receipt, *, now=None):
        with self.connect(write=True) as conn:
            row = conn.execute('SELECT * FROM repurpose_deliveries WHERE release_id=? AND delivery_id=?', (release_id, delivery_id)).fetchone()
            require(row is not None, 'unknown_delivery_intent')
            raw = json_bytes(receipt).decode()
            require(row['receipt_json'] is None or row['receipt_json'] == raw, 'immutable_delivery_receipt')
            conn.execute("UPDATE repurpose_deliveries SET state='submitted',receipt_json=?,updated_at=? WHERE release_id=? AND delivery_id=?", (raw, stamp(now), release_id, delivery_id))

    def delivery_rows(self, workspace, release_id):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM repurpose_deliveries WHERE workspace_id=? AND release_id=? ORDER BY delivery_id', (workspace, release_id))]


    def assert_lease(self, task, *, now=None):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM repurpose_tasks WHERE id=?',(task['id'],)).fetchone()
            require(row is not None and row['state']=='running' and row['workspace_id']==task['workspace_id']
                    and row['request_sha256']==task['request_sha256'] and row['lease_token']==task['lease_token']
                    and row['lease_expires_at']>stamp(now),'lease_lost')
