#!/usr/bin/env python3
"""Online SQLite snapshots and offline-only restores; no application imports.

A snapshot is published without clobbering an existing destination, together
with a SHA256 sidecar. Sources are always opened with mode=ro (not immutable,
which would miss committed WAL pages). Restore NEVER overwrites an existing
file, accepts only a verified standalone snapshot, and requires --offline.
The shell restore wrapper additionally checks that all writers are stopped.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time


class BackupError(RuntimeError):
    """An unsafe or unverifiable backup/restore request."""


def _source(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size < 100:
        raise BackupError("source must be an existing, nonempty regular SQLite file")
    return path.resolve(strict=True)


@contextmanager
def read_connection(path: Path):
    path = _source(path)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def quick_check(conn: sqlite3.Connection) -> None:
    if [tuple(row) for row in conn.execute("PRAGMA quick_check")] != [("ok",)]:
        raise BackupError("SQLite quick_check failed")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checksum_path(path: Path) -> Path:
    return Path(str(path) + ".sha256")


def _no_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise BackupError("refusing a destination/snapshot with SQLite sidecars")


def _destination(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if path.exists() or path.is_symlink():
        raise BackupError("destination already exists; never overwrite a live or unknown file")
    for parent in path.parents:
        if parent.is_symlink():
            raise BackupError("destination parent must not be a symlink")
    _no_sidecars(path)
    if checksum_path(path).exists() or checksum_path(path).is_symlink():
        raise BackupError("destination checksum already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _publish(temp: Path, destination: Path) -> None:
    # Same-filesystem hard-link publication is atomic AND no-clobber. Unlike
    # replace(), a concurrently created destination cannot be overwritten.
    # Windows FlushFileBuffers requires a writable handle; this is ONLY the
    # newly created output tempfile, never the read-only SQLite source.
    with temp.open("r+b") as stream:
        os.fsync(stream.fileno())
    os.link(temp, destination)
    temp.unlink()
    _sync_directory(destination.parent)


def snapshot(source: Path, destination: Path, *, timeout: float = 120) -> dict:
    source = _source(source)
    destination = _destination(destination)
    if timeout <= 0:
        raise BackupError("backup timeout must be positive")
    fd, name = tempfile.mkstemp(prefix=".sqlite-backup-", dir=destination.parent)
    os.close(fd)
    temp = Path(name)
    manifest_temp = None
    published = False
    started = time.monotonic()

    def progress(_status, _remaining, _total):
        if time.monotonic() - started > timeout:
            raise BackupError("online backup timed out")

    try:
        os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
        with read_connection(source) as src:
            target = sqlite3.connect(temp)
            try:
                src.backup(target, pages=256, progress=progress, sleep=0.05)
                target.commit()
                target.execute("PRAGMA journal_mode = DELETE")
                quick_check(target)
            finally:
                target.close()
        _no_sidecars(temp)
        digest = sha256(temp)
        fd, name = tempfile.mkstemp(prefix=".sqlite-sha256-", dir=destination.parent)
        manifest_temp = Path(name)
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as stream:
            stream.write(f"{digest}  {destination.name}\n")
            stream.flush()
            os.fsync(stream.fileno())
        _publish(temp, destination)
        published = True
        _publish(manifest_temp, checksum_path(destination))
        return {"sha256": digest, "bytes": destination.stat().st_size, "quick_check": "ok"}
    except BaseException:
        # A partially published pair is not a usable backup. Only unlink our
        # newly published file, never a pre-existing destination.
        if published:
            destination.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)
        if manifest_temp is not None:
            manifest_temp.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(temp) + suffix).unlink(missing_ok=True)


def verify_snapshot(path: Path) -> dict:
    path = _source(path)
    _no_sidecars(path)
    manifest = checksum_path(path)
    if manifest.is_symlink() or not manifest.is_file():
        raise BackupError("snapshot SHA256 sidecar is required")
    fields = manifest.read_text(encoding="ascii").strip().split(maxsplit=1)
    if len(fields) != 2 or fields[1] != path.name or len(fields[0]) != 64:
        raise BackupError("invalid snapshot checksum sidecar")
    digest = sha256(path)
    if digest != fields[0]:
        raise BackupError("snapshot SHA256 mismatch")
    with read_connection(path) as conn:
        quick_check(conn)
    return {"sha256": digest, "bytes": path.stat().st_size, "quick_check": "ok"}


def verify_restore(source: Path, *, temp_parent: Path | None = None) -> dict:
    before = verify_snapshot(source)
    with tempfile.TemporaryDirectory(prefix="sau-restore-check-", dir=temp_parent) as temp:
        restored = Path(temp) / "restored.db"
        snapshot(source, restored)
        result = verify_snapshot(restored)
    if verify_snapshot(source) != before:
        raise BackupError("snapshot changed during verification")
    return {"source_sha256": before["sha256"], "restored_sha256": result["sha256"],
            "quick_check": "ok", "restore_verified": True}


def restore(source: Path, destination: Path, *, offline: bool = False,
            dry_run: bool = False) -> dict:
    if dry_run:
        return verify_restore(source)
    if not offline:
        raise BackupError("restore requires --offline and all database writers stopped")
    _destination(destination)
    before = verify_snapshot(source)
    result = snapshot(source, destination)
    if verify_snapshot(source) != before:
        raise BackupError("source changed during restore; leave services stopped")
    verify_snapshot(destination)
    return result


def activity(path: Path, *, legacy: bool = False) -> dict:
    """Conservative drain evidence. Bad/missing schema or JSON is an error.

    Retry-wait and unclaimed prep requests are not active work in the split
    worker. Never ignore a leased prep merely because its timestamp is old.
    Legacy workers have no reliable pause/lease contract: count all preps and
    retries until the operator has quiesced/stopped that release.
    """
    with read_connection(path) as conn:
        conn.execute("BEGIN")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(publish_job_targets)")}
        if "status" not in columns:
            raise BackupError("publish target schema is missing")
        if legacy:
            predicate = "status IN ('running', 'retrying')"
        else:
            if "lease_token" not in columns:
                raise BackupError("legacy schema requires explicit legacy rollout")
            predicate = "status = 'running' OR lease_token IS NOT NULL"
        targets = conn.execute(f"SELECT COUNT(*) FROM publish_job_targets WHERE {predicate}").fetchone()[0]
        preps = 0
        for row in conn.execute("SELECT metadata_json FROM campaigns WHERE status = 'preparing'"):
            metadata = json.loads(row[0] or "{}")
            if not isinstance(metadata, dict):
                raise BackupError("invalid prep metadata; drain cannot be established")
            if legacy or metadata.get("_prepLease") or not metadata.get("prepRequest"):
                preps += 1
    return {"active_targets": targets, "active_preps": preps, "drained": targets == 0 and preps == 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("snapshot")
    backup.add_argument("source", type=Path)
    backup.add_argument("destination", type=Path)
    backup.add_argument("--timeout", type=float, default=120)
    verify = sub.add_parser("verify")
    verify.add_argument("source", type=Path)
    verify.add_argument("--restore-check", action="store_true")
    recovery = sub.add_parser("restore")
    recovery.add_argument("source", type=Path)
    recovery.add_argument("destination", type=Path)
    recovery.add_argument("--offline", action="store_true")
    recovery.add_argument("--dry-run", action="store_true")
    drain = sub.add_parser("activity")
    drain.add_argument("source", type=Path)
    drain.add_argument("--legacy", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = snapshot(args.source, args.destination, timeout=args.timeout)
        elif args.command == "verify":
            result = verify_restore(args.source) if args.restore_check else verify_snapshot(args.source)
        elif args.command == "restore":
            result = restore(args.source, args.destination, offline=args.offline, dry_run=args.dry_run)
        else:
            result = activity(args.source, legacy=args.legacy)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("drained", True) else 3
    except (BackupError, OSError, sqlite3.Error, ValueError):
        # Never print source rows, config or exception strings from SQLite.
        print("SQLite safety check failed; destination not approved for use", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
