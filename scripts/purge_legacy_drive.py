#!/usr/bin/env python3
"""Quarantine legacy untiered Google Drive media that nothing references.

Background
----------
The offload pipeline used to put every object directly under ``sau/<root>``
(``sau/videoFile``, ``sau/uploads``, ``sau/generated``).  It now writes into
``sau/inbox/<root>`` (material awaiting its first publish) and
``sau/published/<root>`` (already published history); the tiering rule lives in
:mod:`myUtils.drive_layout`, and ``scripts/migrate_drive_layout.py`` promotes
old objects into it.  This script finishes the job for the objects the
migration never promoted: it finds the ones that *nothing* references and
moves them aside, while leaving every object that a live scheduled post still
needs exactly where it is.

What is "needed"
----------------
An object is matched by its **drive key** (the path relative to its legacy
root).  A key is NEEDED when either signal says so:

*strong* (exact ``(root, key)``):
  1. a ``file_records`` row maps it (``storage_backend_id`` points at a legacy
     rclone backend, and ``storage_key`` is that key), or
  2. a LIVE ``publish_job_targets`` payload names it
     (``payload_json.artifacts[].local_path`` / ``.public_url``), or
  3. it is a ``campaign_artifacts`` row whose campaign has at least one LIVE
     target.

*weak* (basename only, against the LIVE references):
  4. its basename matches the basename of a LIVE target payload key, or of a
     campaign_artifact key for a LIVE campaign.  This is a safety net for a
     path that was re-rooted while the last path segment stayed the same; it
     only ever *keeps* an object, never moves one.

LIVE means ``publish_job_targets.status IN (pending, retrying, running)``.

Reproducing the operator's split
--------------------------------
With the definitions above the classifier reports, per root::

    videoFile  needed=445  unused=205  (orphans referenced by live payload=244,
                                        by live campaign=248)
    uploads    needed=1    unused=3
    generated  needed=483  unused=0

i.e. 208 unused objects / ~16.6 GB.  ``--report-basenames`` additionally lists
the unused objects whose *basename* matches a ``file_records`` key under a
different drive key (possible duplicates); they are still counted unused, as
measured, but the note is printed so the operator can review them.

Safety
------
* Dry-run by default.  ``--apply`` writes.
* **Never deletes.**  An unused object is *quarantined*: copied to
  ``sau/trash/<UTC date>/<root>/<key>``, verified there by ``rclone lsjson``
  (size must match the source), and only then removed from its old path with
  ``rclone deletefile``.  A reversible move is used instead of ``rclone delete``
  because the remote is shared: a later "wait, that was live" must be fixable
  by moving the object back, not by restoring from a backup that may not exist.
* Refuses to run when the reference sets are empty, because that means the DB
  read failed and every object would look unused.
* ``--limit N`` operates on the first N unused objects only.

Usage::

    python scripts/purge_legacy_drive.py                    # dry-run, full report
    python scripts/purge_legacy_drive.py --report-basenames
    python scripts/purge_legacy_drive.py --check-tiered
    python scripts/purge_legacy_drive.py --apply --limit 5  # prove the mechanism
    python scripts/purge_legacy_drive.py --apply            # operator-approved, all

Restoring a quarantine
---------------------
``--restore`` is the exact inverse: it lists a trash tree
(``--quarantine-prefix``, default ``sau/trash/<UTC date>``) and moves every
object back to ``sau/<root>/<key>``, verifying the size before removing the
trash copy.  It never overwrites an existing legacy object.  Dry-run by
default; add ``--apply`` to write::

    python scripts/purge_legacy_drive.py --restore                      # plan
    python scripts/purge_legacy_drive.py --restore --quarantine-prefix sau/trash/2026-10-10 --apply
    python scripts/purge_legacy_drive.py --restore --restore-key videoFile/a.mp4 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from myUtils import drive_layout  # noqa: E402

RCLONE = "rclone"
DEFAULT_RCLONE_CONFIG = Path.home() / ".config/rclone/rclone.conf"
REMOTE = drive_layout.DEFAULT_REMOTE
MEDIA_ROOTS = drive_layout.MEDIA_ROOTS
LIVE_STATUSES = ("pending", "retrying", "running")
LEGACY_ENDPOINTS = {root: drive_layout.legacy_endpoint(root) for root in MEDIA_ROOTS}
ENDPOINT_TO_ROOT = {endpoint: root for root, endpoint in LEGACY_ENDPOINTS.items()}

# Basename matches against file_records (as opposed to a LIVE reference) are
# reported but deliberately do not protect an object: the measured split the
# operator asked for counts them as unused.  A basename match against a LIVE
# reference does protect, because that is a scheduled post's real asset.
WEAK_PROTECT_LIVE = True


def _run_rclone(config: Path, *args: str, attempts: int = 3) -> subprocess.CompletedProcess:
    """Run rclone with bounded retries. ``args`` are placed after the config."""
    command = [RCLONE, "--config", str(config), *args]
    last: subprocess.CompletedProcess | None = None
    for attempt in range(attempts):
        last = subprocess.run(command, capture_output=True, text=True)
        if last.returncode == 0:
            return last
        if attempt < attempts - 1:
            time.sleep(10 * (attempt + 1))
    assert last is not None
    return last


def list_remote_objects(
    config: Path, remote: str, endpoint: str, *, attempts: int = 3, allow_missing: bool = False
) -> list[dict]:
    """Return the live listing for one root as ``[{path, size, isdir}, ...]``.

    Always rebuilt from rclone (no cached plan file): a stale listing is exactly
    what would make a "safe" purge unsafe.  When ``allow_missing`` is set, an
    rclone "directory not found" (rc=3) is treated as an empty listing rather
    than an error, so restoring a never-existed/empty trash tree is a no-op.
    """
    completed = _run_rclone(
        config,
        "lsjson",
        "-R",
        "--files-only",
        f"{remote}:{endpoint}",
        attempts=attempts,
    )
    if completed.returncode != 0:
        if allow_missing and completed.returncode == 3:
            return []
        raise RuntimeError(
            f"rclone listing failed for {remote}:{endpoint}: "
            f"{completed.stderr.strip() or completed.returncode}"
        )
    try:
        entries = json.loads(completed.stdout or "[]")
    except ValueError as exc:  # pragma: no cover - rclone emits valid JSON
        raise RuntimeError(f"could not parse rclone output for {endpoint}: {exc}") from exc
    return [
        {
            "path": str(entry.get("Path") or "").strip("/"),
            "size": int(entry.get("Size") or 0),
            "isdir": bool(entry.get("IsDir")),
        }
        for entry in entries
        if entry.get("Path")
    ]


def _basename(key: str) -> str:
    return PurePosixPath(str(key or "")).name


def _artifact_keys(value: str | None) -> tuple[tuple[str, str] | None, str | None]:
    """Map one payload/artifact reference to a ``(root, key)`` pair.

    ``local_path`` is taken verbatim.  A ``public_url`` is only used when its
    ``filename=`` query parameter contains an explicit media-root marker, so a
    bare query value (``filename=campaigns/...``) is never mis-attributed to
    ``videoFile`` by :func:`drive_layout.split_media_path`'s bare-path default.
    """
    raw = str(value or "").strip()
    if not raw:
        return None, None
    if raw.lower().startswith(("http://", "https://")):
        try:
            query = parse_qs(urlparse(raw).query)
        except ValueError:
            return None, None
        for candidate in query.get("filename") or []:
            text = unquote(candidate)
            marker = next(
                (f"{root}/" for root in MEDIA_ROOTS if f"{root}/" in text), ""
            )
            if not marker:
                continue
            split = drive_layout.split_media_path(text)
            if split is not None:
                return split, text
        return None, None
    split = drive_layout.split_media_path(raw)
    return split, raw


def _record_key_candidates(root: str, storage_key: str) -> set[str]:
    """The spellings a ``file_records`` key may legitimately carry.

    Legacy ``uploads/`` rows disagree about whether the key includes its root
    prefix; both spellings exist in production, and
    ``scripts/migrate_drive_layout.py`` accepts both for the same reason.
    """
    key = str(storage_key or "").strip("/")
    candidates = {key}
    if root == "uploads":
        if key.startswith("uploads/"):
            candidates.add(key[len("uploads/") :])
        else:
            candidates.add(f"uploads/{key}")
    return candidates


def load_reference_sets(conn: sqlite3.Connection) -> dict:
    """Build every reference set from the database. Raises on a failed read."""
    conn.row_factory = sqlite3.Row

    backends = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM storage_backends")}

    file_record_keys: dict[str, set[str]] = {root: set() for root in MEDIA_ROOTS}
    file_record_basenames: dict[str, set[str]] = {root: set() for root in MEDIA_ROOTS}
    for row in conn.execute("SELECT id, storage_key, storage_backend_id FROM file_records"):
        backend = backends.get(row["storage_backend_id"])
        if not backend:
            continue
        root = ENDPOINT_TO_ROOT.get(str(backend.get("endpoint") or "").strip("/"))
        if root is None:
            continue
        for candidate in _record_key_candidates(root, row["storage_key"]):
            if candidate:
                file_record_keys[root].add(candidate)
                file_record_basenames[root].add(_basename(candidate))

    live_payload_keys: set[tuple[str, str]] = set()
    live_payload_basenames: set[tuple[str, str]] = set()
    live_campaign_ids: set[int] = set()
    live_target_count = 0
    query = (
        "SELECT t.status AS status, j.payload_json AS payload_json "
        "FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id "
        f"WHERE t.status IN ({','.join('?' for _ in LIVE_STATUSES)})"
    )
    for row in conn.execute(query, LIVE_STATUSES):
        live_target_count += 1
        try:
            data = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        campaign_id = data.get("campaignId") or data.get("campaign_id")
        if campaign_id:
            try:
                live_campaign_ids.add(int(campaign_id))
            except (TypeError, ValueError):
                pass
        for artifact in data.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            artifact_campaign = artifact.get("campaign_id")
            if artifact_campaign:
                try:
                    live_campaign_ids.add(int(artifact_campaign))
                except (TypeError, ValueError):
                    pass
            for field in ("local_path", "public_url"):
                split, _raw = _artifact_keys(artifact.get(field))
                if split is not None:
                    live_payload_keys.add(split)
                    live_payload_basenames.add((split[0], _basename(split[1])))

    live_campaign_keys: set[tuple[str, str]] = set()
    live_campaign_basenames: set[tuple[str, str]] = set()
    for row in conn.execute(
        "SELECT campaign_id, local_path, public_url FROM campaign_artifacts"
    ):
        try:
            campaign_id = int(row["campaign_id"])
        except (TypeError, ValueError):
            continue
        if campaign_id not in live_campaign_ids:
            continue
        for field in ("local_path", "public_url"):
            split, _raw = _artifact_keys(row[field])
            if split is not None:
                live_campaign_keys.add(split)
                live_campaign_basenames.add((split[0], _basename(split[1])))

    return {
        "file_record_keys": file_record_keys,
        "file_record_basenames": file_record_basenames,
        "live_payload_keys": live_payload_keys,
        "live_payload_basenames": live_payload_basenames,
        "live_campaign_keys": live_campaign_keys,
        "live_campaign_basenames": live_campaign_basenames,
        "live_campaign_ids": live_campaign_ids,
        "live_target_count": live_target_count,
        "counts": {
            "file_records": sum(len(v) for v in file_record_keys.values()),
            "live_payload_keys": len(live_payload_keys),
            "live_campaign_keys": len(live_campaign_keys),
            "live_campaigns": len(live_campaign_ids),
            "live_targets": live_target_count,
        },
    }


def classify(
    *,
    root: str,
    entries: list[dict],
    refs: dict,
) -> list[dict]:
    """Classify each unique drive key under one root."""
    sizes: dict[str, int] = {}
    duplicate_keys: set[str] = set()
    for entry in entries:
        key = entry["path"]
        if key in sizes:
            duplicate_keys.add(key)
        sizes[key] = sizes.get(key, 0) + entry["size"]

    file_keys = refs["file_record_keys"][root]
    file_basenames = refs["file_record_basenames"][root]
    results: list[dict] = []
    for key in sorted(sizes):
        strong_reasons: list[str] = []
        weak_reasons: list[str] = []
        if key in file_keys:
            strong_reasons.append("file_records")
        if (root, key) in refs["live_payload_keys"]:
            strong_reasons.append("live_payload")
        if (root, key) in refs["live_campaign_keys"]:
            strong_reasons.append("live_campaign")
        basename = _basename(key)
        if (root, basename) in refs["live_payload_basenames"]:
            weak_reasons.append("live_payload_basename")
        if (root, basename) in refs["live_campaign_basenames"]:
            weak_reasons.append("live_campaign_basename")

        needed = bool(strong_reasons) or (WEAK_PROTECT_LIVE and bool(weak_reasons))
        review_basename = basename in file_basenames
        results.append(
            {
                "root": root,
                "key": key,
                "bytes": sizes[key],
                "needed": needed,
                "strong": strong_reasons,
                "weak": weak_reasons,
                "file_record_basename_only": review_basename
                and not strong_reasons
                and not weak_reasons,
                "duplicate": key in duplicate_keys,
            }
        )
    return results


def summarize(rows: list[dict]) -> dict:
    per_root: dict[str, dict] = {}
    for row in rows:
        bucket = per_root.setdefault(
            row["root"],
            {"total": 0, "needed": 0, "unused": 0, "bytes": 0, "unused_bytes": 0},
        )
        bucket["total"] += 1
        bucket["bytes"] += row["bytes"]
        if row["needed"]:
            bucket["needed"] += 1
        else:
            bucket["unused"] += 1
            bucket["unused_bytes"] += row["bytes"]
    return per_root


def _destination_spec(remote: str, quarantine_prefix: str, row: dict) -> tuple[str, str]:
    source = drive_layout.remote_spec(remote, LEGACY_ENDPOINTS[row["root"]], row["key"])
    dest_key = str(
        PurePosixPath(quarantine_prefix, row["root"], row["key"])
    )
    destination = f"{remote}:{dest_key}"
    return source, destination


def quarantine(
    config: Path, remote: str, quarantine_prefix: str, row: dict, *, dry_run: bool
) -> dict:
    """Quarantine one unused object; verify the copy before dropping the source."""
    source, destination = _destination_spec(remote, quarantine_prefix, row)
    actions = [
        f"{RCLONE} --config {config} copyto {json.dumps(source)} {json.dumps(destination)}",
        f"{RCLONE} --config {config} lsjson {json.dumps(destination)}",
        f"{RCLONE} --config {config} deletefile {json.dumps(source)}",
    ]
    outcome = {
        "root": row["root"],
        "key": row["key"],
        "bytes": row["bytes"],
        "source": source,
        "destination": destination,
        "actions": actions,
        "status": "planned" if dry_run else "pending",
    }
    if dry_run:
        return outcome

    copied = _run_rclone(config, "copyto", source, destination, attempts=2)
    if copied.returncode != 0:
        outcome["status"] = "copy_failed"
        outcome["error"] = copied.stderr.strip() or f"rc={copied.returncode}"
        return outcome

    verified = _run_rclone(config, "lsjson", destination, attempts=3)
    destination_size: int | None = None
    if verified.returncode == 0:
        try:
            listing = json.loads(verified.stdout or "[]")
            if listing:
                destination_size = int(listing[0].get("Size") or 0)
        except (ValueError, TypeError, IndexError):
            destination_size = None
    if verified.returncode != 0 or destination_size != row["bytes"]:
        outcome["status"] = "verify_failed"
        outcome["destination_size"] = destination_size
        outcome["error"] = (
            verified.stderr.strip()
            or f"destination size {destination_size} != source size {row['bytes']}"
        )
        return outcome

    removed = _run_rclone(config, "deletefile", source, attempts=3)
    if removed.returncode != 0:
        outcome["status"] = "delete_failed"
        outcome["error"] = removed.stderr.strip() or f"rc={removed.returncode}"
        return outcome

    outcome["status"] = "quarantined"
    outcome["verified_size"] = destination_size
    return outcome


def _write_files_from(keys: list[str]) -> str:
    """Write an rclone ``--files-from`` list to a private temp file."""
    fd, path = tempfile.mkstemp(prefix="sau-purge-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key in keys:
            handle.write(str(key).strip("\n") + "\n")
    return path


def _batch_outcome(
    remote: str, quarantine_prefix: str, row: dict, status: str, **extra
) -> dict:
    source, destination = _destination_spec(remote, quarantine_prefix, row)
    outcome = {
        "root": row["root"],
        "key": row["key"],
        "bytes": row["bytes"],
        "source": source,
        "destination": destination,
        "status": status,
    }
    outcome.update(extra)
    return outcome


def quarantine_batch(
    config: Path,
    remote: str,
    quarantine_prefix: str,
    rows: list[dict],
    *,
    dry_run: bool,
) -> list[dict]:
    """Batch equivalent of :func:`quarantine` for many objects.

    Per legacy root it performs three rclone invocations instead of three per
    object: one ``copy --files-from`` (server-side), one recursive ``lsjson``
    that verifies every destination exists at the expected size, and one
    ``delete --files-from`` for only the verified sources.  No source is removed
    until its destination has been verified, exactly as in the per-object path.
    """
    if dry_run:
        return [
            quarantine(config, remote, quarantine_prefix, row, dry_run=True)
            for row in rows
        ]

    by_root: dict[str, list[dict]] = {}
    for row in rows:
        by_root.setdefault(row["root"], []).append(row)

    outcomes: list[dict] = []
    for root in sorted(by_root):
        root_rows = by_root[root]
        source_root = f"{remote}:{LEGACY_ENDPOINTS[root]}"
        destination_root = f"{remote}:{PurePosixPath(quarantine_prefix, root)}"

        copy_list = _write_files_from([row["key"] for row in root_rows])
        try:
            copied = _run_rclone(
                config,
                "copy",
                "--files-from",
                copy_list,
                source_root,
                destination_root,
                attempts=2,
            )
        finally:
            os.unlink(copy_list)

        verified = _run_rclone(
            config, "lsjson", "-R", "--files-only", destination_root, attempts=3
        )
        destination_sizes: dict[str, int] = {}
        if verified.returncode == 0:
            try:
                for entry in json.loads(verified.stdout or "[]"):
                    path = str(entry.get("Path") or "").strip("/")
                    if path:
                        destination_sizes[path] = int(entry.get("Size") or 0)
            except (ValueError, TypeError):
                destination_sizes = {}

        verified_rows: list[dict] = []
        for row in root_rows:
            if copied.returncode != 0:
                outcomes.append(
                    _batch_outcome(
                        remote,
                        quarantine_prefix,
                        row,
                        "copy_failed",
                        error=copied.stderr.strip() or f"rc={copied.returncode}",
                    )
                )
                continue
            destination_size = destination_sizes.get(row["key"])
            if destination_size == row["bytes"]:
                verified_rows.append(row)
            else:
                outcomes.append(
                    _batch_outcome(
                        remote,
                        quarantine_prefix,
                        row,
                        "verify_failed",
                        destination_size=destination_size,
                        error=(
                            f"destination size {destination_size} != "
                            f"source size {row['bytes']}"
                        ),
                    )
                )

        if verified_rows:
            delete_list = _write_files_from([row["key"] for row in verified_rows])
            try:
                removed = _run_rclone(
                    config,
                    "delete",
                    "--files-from",
                    delete_list,
                    source_root,
                    attempts=3,
                )
            finally:
                os.unlink(delete_list)
            for row in verified_rows:
                if removed.returncode == 0:
                    outcomes.append(
                        _batch_outcome(
                            remote,
                            quarantine_prefix,
                            row,
                            "quarantined",
                            verified_size=row["bytes"],
                        )
                    )
                else:
                    outcomes.append(
                        _batch_outcome(
                            remote,
                            quarantine_prefix,
                            row,
                            "delete_failed",
                            error=removed.stderr.strip() or f"rc={removed.returncode}",
                        )
                    )
    return outcomes


def parse_trash_entries(entries: list[dict]) -> list[dict]:
    """Turn a ``sau/trash/<date>`` listing into ``{root, key, bytes}`` rows.

    Each remote path is ``<root>/<key...>``; the first segment names the legacy
    media root and everything after it is the original drive key.  Paths whose
    first segment is not a known media root are dropped (fail closed: the
    restore path would not know where to put them).
    """
    rows: list[dict] = []
    for entry in entries:
        key = str(entry.get("path") or "").strip("/")
        if not key:
            continue
        parts = key.split("/", 1)
        root = parts[0]
        if root not in MEDIA_ROOTS or len(parts) != 2 or not parts[1]:
            continue
        rows.append({"root": root, "key": parts[1], "bytes": int(entry.get("size") or 0)})
    return rows


def _restore_spec(remote: str, quarantine_prefix: str, root: str, key: str) -> tuple[str, str]:
    """Return ``(trash_source, legacy_destination)`` for one quarantined object."""
    source_key = str(PurePosixPath(quarantine_prefix, root, key))
    source = f"{remote}:{source_key}"
    destination = drive_layout.remote_spec(remote, LEGACY_ENDPOINTS[root], key)
    return source, destination


def restore_object(
    config: Path,
    remote: str,
    quarantine_prefix: str,
    row: dict,
    *,
    dry_run: bool,
) -> dict:
    """Move one quarantined object back to its legacy path.

    Symmetric to :func:`quarantine`: copy to the legacy path, verify the size
    with ``rclone lsjson``, and only then remove the trash copy.  If the legacy
    path already exists it is never overwritten: an equal-size object is
    reported ``already_present`` and a different-size object is a
    ``destination_conflict``.
    """
    source, destination = _restore_spec(remote, quarantine_prefix, row["root"], row["key"])
    actions = [
        f"{RCLONE} --config {config} copyto {json.dumps(source)} {json.dumps(destination)}",
        f"{RCLONE} --config {config} lsjson {json.dumps(destination)}",
        f"{RCLONE} --config {config} deletefile {json.dumps(source)}",
    ]
    outcome = {
        "root": row["root"],
        "key": row["key"],
        "bytes": row["bytes"],
        "source": source,
        "destination": destination,
        "actions": actions,
        "status": "planned" if dry_run else "pending",
    }
    if dry_run:
        return outcome

    existing = _run_rclone(config, "lsjson", destination, attempts=2)
    destination_size: int | None = None
    if existing.returncode == 0:
        try:
            listing = json.loads(existing.stdout or "[]")
            if listing:
                destination_size = int(listing[0].get("Size") or 0)
        except (ValueError, TypeError, IndexError):
            destination_size = None
    # Only a *non-empty* listing means the legacy path is occupied; an empty
    # listing (or rc=3 "not found") means the slot is free and we may copy back.
    if destination_size is not None:
        if destination_size == row["bytes"]:
            outcome["status"] = "already_present"
            outcome["destination_size"] = destination_size
            return outcome
        outcome["status"] = "destination_conflict"
        outcome["destination_size"] = destination_size
        outcome["error"] = (
            f"legacy path already holds a different object "
            f"({destination_size} B != expected {row['bytes']} B); refusing to overwrite"
        )
        return outcome

    copied = _run_rclone(config, "copyto", source, destination, attempts=2)
    if copied.returncode != 0:
        outcome["status"] = "copy_failed"
        outcome["error"] = copied.stderr.strip() or f"rc={copied.returncode}"
        return outcome

    verified = _run_rclone(config, "lsjson", destination, attempts=3)
    restored_size: int | None = None
    if verified.returncode == 0:
        try:
            listing = json.loads(verified.stdout or "[]")
            if listing:
                restored_size = int(listing[0].get("Size") or 0)
        except (ValueError, TypeError, IndexError):
            restored_size = None
    if verified.returncode != 0 or restored_size != row["bytes"]:
        outcome["status"] = "verify_failed"
        outcome["destination_size"] = restored_size
        outcome["error"] = (
            verified.stderr.strip()
            or f"restored size {restored_size} != trash size {row['bytes']}"
        )
        return outcome

    removed = _run_rclone(config, "deletefile", source, attempts=3)
    if removed.returncode != 0:
        outcome["status"] = "delete_failed"
        outcome["error"] = removed.stderr.strip() or f"rc={removed.returncode}"
        return outcome

    outcome["status"] = "restored"
    outcome["verified_size"] = restored_size
    return outcome


def list_tiered_keys(config: Path, remote: str) -> set[tuple[str, str]]:
    """Every ``(root, key)`` already present under ``sau/inbox`` / ``sau/published``."""
    tiered: set[tuple[str, str]] = set()
    for tier in drive_layout.TIERS:
        for root in MEDIA_ROOTS:
            endpoint = drive_layout.tier_endpoint(tier, root)
            try:
                entries = list_remote_objects(config, remote, endpoint)
            except RuntimeError:
                continue
            for entry in entries:
                tiered.add((root, entry["path"]))
    return tiered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=BASE_DIR / "db" / "database.db")
    parser.add_argument(
        "--rclone-config", type=Path, default=DEFAULT_RCLONE_CONFIG
    )
    parser.add_argument("--remote", default=REMOTE)
    parser.add_argument(
        "--roots",
        default=",".join(MEDIA_ROOTS),
        help="comma-separated legacy roots to inspect",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the quarantine moves (default: dry-run)",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help=(
            "with --apply, quarantine each root with copy-all/verify-all/"
            "delete-verified instead of three rclone calls per object"
        ),
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help=(
            "move a trash tree back to the legacy roots instead of quarantining; "
            "use --quarantine-prefix to name the tree and --apply to write"
        ),
    )
    parser.add_argument(
        "--restore-key",
        default="",
        help=(
            "comma-separated <root>/<key> pairs to restore (default: everything "
            "in the trash tree); only meaningful with --restore"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="operate on at most N unused objects (0 = all)",
    )
    parser.add_argument(
        "--quarantine-prefix",
        default="",
        help="override the quarantine prefix (default sau/trash/<UTC date>)",
    )
    parser.add_argument(
        "--report-basenames",
        action="store_true",
        help="list unused objects whose basename matches a file_records key under another key",
    )
    parser.add_argument(
        "--check-tiered",
        action="store_true",
        help="list inbox/published and report NEEDED objects missing there",
    )
    parser.add_argument(
        "--list-actions",
        type=int,
        default=50,
        help="how many planned actions to print (default 50)",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    return parser


def run_restore_mode(args, roots: list[str]) -> int:
    """List a trash tree and move its objects back to the legacy roots."""
    prefix = args.quarantine_prefix or (
        "sau/trash/" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    wanted: set[tuple[str, str]] = set()
    for item in (args.restore_key or "").split(","):
        item = item.strip().strip("/")
        if not item:
            continue
        parts = item.split("/", 1)
        if len(parts) != 2 or parts[0] not in MEDIA_ROOTS or not parts[1]:
            print(f"ignoring malformed --restore-key {item!r}", file=sys.stderr)
            continue
        wanted.add((parts[0], parts[1]))

    print(f"listing {args.remote}:{prefix} ...", file=sys.stderr, flush=True)
    try:
        entries = list_remote_objects(
            args.rclone_config, args.remote, prefix, allow_missing=True
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    rows = [row for row in parse_trash_entries(entries) if row["root"] in roots]
    if wanted:
        rows = [row for row in rows if (row["root"], row["key"]) in wanted]

    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    duplicates: list[tuple[str, str]] = []
    for row in rows:
        ident = (row["root"], row["key"])
        if ident in seen:
            duplicates.append(ident)
            continue
        seen.add(ident)
        uniq.append(row)

    selected = uniq[: args.limit] if args.limit and args.limit > 0 else uniq
    planned = [
        restore_object(
            args.rclone_config,
            args.remote,
            prefix,
            row,
            dry_run=not args.apply,
        )
        for row in selected
    ]

    report = {
        "remote": args.remote,
        "trash_prefix": prefix,
        "restore": True,
        "apply": bool(args.apply),
        "limit": args.limit,
        "filter": sorted(f"{r}/{k}" for r, k in wanted),
        "totals": {
            "in_trash": len(rows),
            "restorable": len(uniq),
            "selected": len(selected),
            "duplicates_skipped": len(duplicates),
        },
        "selected": planned,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print("=" * 78)
    print(
        f"legacy Drive restore — remote={args.remote} from={prefix} "
        f"mode={'APPLY' if args.apply else 'DRY-RUN'}"
    )
    print("=" * 78)
    print(f"  objects in trash tree : {len(rows)}")
    print(f"  distinct restorable   : {len(uniq)}")
    print(f"  duplicate names skipped: {len(duplicates)}")
    for root in roots:
        bucket = [row for row in uniq if row["root"] == root]
        if bucket:
            print(
                f"  {root:<12} {len(bucket):>5} object(s)  "
                f"{sum(r['bytes'] for r in bucket):>16,} B"
            )
    for ident in duplicates:
        print(f"  SKIP duplicate-name {ident[0]}/{ident[1]}")

    print()
    print(
        f"{'RESTORE' if args.apply else 'DRY-RUN'}: {len(selected)} object(s) "
        f"selected from {prefix}"
    )
    for row in planned[: args.list_actions]:
        print(
            f"  {row['root']}/{row['key']}  ({row['bytes']:,} B) -> {row['destination']}"
        )
        for action in row.get("actions", []):
            print(f"      $ {action}")
    if len(planned) > args.list_actions:
        print(f"  ... and {len(planned) - args.list_actions} more")

    if args.apply:
        statuses: dict[str, int] = {}
        restored_bytes = 0
        for row in planned:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            if row["status"] == "restored":
                restored_bytes += row["bytes"]
        print()
        print("restore results:")
        for status, count in sorted(statuses.items()):
            print(f"  {status}: {count}")
        print(f"  restored bytes: {restored_bytes:,}")
        return 0 if not any(
            s in {"copy_failed", "verify_failed", "delete_failed", "destination_conflict"}
            for s in statuses
        ) else 1

    print()
    print("dry-run only; nothing changed. Re-run with --restore --apply to move back.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = [root.strip() for root in args.roots.split(",") if root.strip()]
    for root in roots:
        if root not in MEDIA_ROOTS:
            print(f"unknown legacy root: {root!r}", file=sys.stderr)
            return 2

    if args.restore:
        return run_restore_mode(args, roots)

    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"could not open database {args.db}: {exc}", file=sys.stderr)
        return 2
    try:
        refs = load_reference_sets(conn)
    except sqlite3.Error as exc:
        print(f"database read failed; refusing to run: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    counts = refs["counts"]
    if counts["file_records"] == 0 and counts["live_payload_keys"] == 0 and counts["live_campaign_keys"] == 0:
        print(
            "reference sets are empty (file_records=0, live_payload=0, "
            "live_campaign=0); the database read failed, refusing to run",
            file=sys.stderr,
        )
        return 2

    all_rows: list[dict] = []
    for root in roots:
        endpoint = LEGACY_ENDPOINTS[root]
        print(f"listing {args.remote}:{endpoint} ...", file=sys.stderr, flush=True)
        try:
            entries = list_remote_objects(args.rclone_config, args.remote, endpoint)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        all_rows.extend(classify(root=root, entries=entries, refs=refs))

    per_root = summarize(all_rows)
    unused = [row for row in all_rows if not row["needed"]]
    needed = [row for row in all_rows if row["needed"]]
    if args.limit and args.limit > 0:
        selected = unused[: args.limit]
    else:
        selected = unused

    quarantine_prefix = args.quarantine_prefix or (
        "sau/trash/" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )

    planned: list[dict] = []
    if args.apply:
        queued: list[dict] = []
        for row in selected:
            if row["duplicate"]:
                planned.append(
                    {
                        "root": row["root"],
                        "key": row["key"],
                        "bytes": row["bytes"],
                        "status": "skipped_duplicate_name",
                    }
                )
                continue
            queued.append(row)
        if args.batch:
            planned.extend(
                quarantine_batch(
                    args.rclone_config,
                    args.remote,
                    quarantine_prefix,
                    queued,
                    dry_run=False,
                )
            )
        else:
            for row in queued:
                planned.append(
                    quarantine(
                        args.rclone_config,
                        args.remote,
                        quarantine_prefix,
                        row,
                        dry_run=False,
                    )
                )
    else:
        for row in selected:
            source, destination = _destination_spec(
                args.remote, quarantine_prefix, row
            )
            planned.append(
                {
                    "root": row["root"],
                    "key": row["key"],
                    "bytes": row["bytes"],
                    "source": source,
                    "destination": destination,
                    "status": "planned",
                }
            )

    report = {
        "remote": args.remote,
        "quarantine_prefix": quarantine_prefix,
        "apply": bool(args.apply),
        "limit": args.limit,
        "reference_counts": counts,
        "per_root": per_root,
        "totals": {
            "total": len(all_rows),
            "needed": len(needed),
            "unused": len(unused),
            "unused_bytes": sum(row["bytes"] for row in unused),
            "selected": len(selected),
        },
        "selected": planned,
        "basename_review": [
            {"root": row["root"], "key": row["key"], "bytes": row["bytes"]}
            for row in unused
            if row["file_record_basename_only"]
        ],
    }

    if args.check_tiered:
        print("listing tiered trees for the NEEDED cross-check ...", file=sys.stderr, flush=True)
        tiered = list_tiered_keys(args.rclone_config, args.remote)
        missing = [
            {"root": row["root"], "key": row["key"], "bytes": row["bytes"]}
            for row in needed
            if (row["root"], row["key"]) not in tiered
        ]
        report["needed_missing_from_tiered"] = {
            "count": len(missing),
            "sample": missing[:25],
        }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print("=" * 78)
    print(f"legacy Drive purge report — remote={args.remote} mode={'APPLY' if args.apply else 'DRY-RUN'}")
    print("=" * 78)
    print("reference sets (from DB):")
    for name, value in counts.items():
        print(f"  {name:20s} {value}")
    if not any(counts.get(k) for k in ("file_records", "live_payload_keys", "live_campaign_keys")):
        print("  WARNING: all reference sets are empty")
    print()
    print("classification per legacy root:")
    header = f"  {'root':<12}{'objects':>9}{'needed':>9}{'unused':>9}{'unused bytes':>16}"
    print(header)
    for root in roots:
        bucket = per_root.get(root, {"total": 0, "needed": 0, "unused": 0, "unused_bytes": 0})
        print(
            f"  {root:<12}{bucket['total']:>9}{bucket['needed']:>9}"
            f"{bucket['unused']:>9}{bucket['unused_bytes']:>16,}"
        )
    totals = report["totals"]
    print(
        f"  {'TOTAL':<12}{totals['total']:>9}{totals['needed']:>9}"
        f"{totals['unused']:>9}{totals['unused_bytes']:>16,}"
    )

    # Orphan detail: unused-by-file_records objects that LIVE references still name.
    fr_all: dict[str, set[str]] = {
        root: refs["file_record_keys"][root] for root in roots
    }
    orphans = [row for row in all_rows if row["key"] not in fr_all.get(row["root"], set())]
    print()
    print("orphan detail (not mapped by file_records):")
    for root in roots:
        r_orphans = [row for row in orphans if row["root"] == root]
        live_payload = [row for row in r_orphans if "live_payload" in row["strong"]]
        live_campaign = [row for row in r_orphans if "live_campaign" in row["strong"]]
        weak = [row for row in r_orphans if row["weak"] and not row["strong"]]
        print(
            f"  {root:<12} orphans={len(r_orphans):<5}"
            f" live_payload={len(live_payload):<5} ({sum(r['bytes'] for r in live_payload):,} B)"
            f" live_campaign={len(live_campaign):<5} ({sum(r['bytes'] for r in live_campaign):,} B)"
            f" weak_only={len(weak)}"
        )

    if args.report_basenames and report["basename_review"]:
        print()
        print(
            f"basename review: {len(report['basename_review'])} unused object(s) whose basename "
            "matches a file_records key under another drive key (counted unused):"
        )
        for row in report["basename_review"]:
            print(f"  {row['root']}/{row['key']}  ({row['bytes']:,} B)")

    if args.check_tiered:
        info = report.get("needed_missing_from_tiered", {})
        print()
        print(
            f"NEEDED objects absent from sau/inbox|published: {info.get('count', 0)}"
            " (they live in the legacy tree and stay there; restore is unaffected)"
        )
        for row in info.get("sample", [])[:10]:
            print(f"  {row['root']}/{row['key']}")

    print()
    print(
        f"{'APPLY' if args.apply else 'DRY-RUN'}: selected {len(selected)} unused object(s) "
        f"for quarantine under {quarantine_prefix}"
    )
    for row in planned[: args.list_actions]:
        if row.get("status") == "skipped_duplicate_name":
            print(f"  SKIP duplicate-name {row['root']}/{row['key']}")
        else:
            print(f"  {row['root']}/{row['key']}  ({row['bytes']:,} B) -> {row['destination']}")
            for action in row.get("actions", []):
                print(f"      $ {action}")
    if len(planned) > args.list_actions:
        print(f"  ... and {len(planned) - args.list_actions} more")

    if args.apply:
        statuses: dict[str, int] = {}
        for row in planned:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        print()
        print("apply results:")
        for status, count in sorted(statuses.items()):
            print(f"  {status}: {count}")
        return 0 if not any(
            s in {"copy_failed", "verify_failed", "delete_failed"} for s in statuses
        ) else 1

    print()
    print(
        "dry-run only; nothing changed. Re-run with --apply to quarantine. "
        "Leave the full run for operator approval."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
