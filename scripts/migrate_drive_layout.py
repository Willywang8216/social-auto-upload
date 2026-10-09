#!/usr/bin/env python3
"""Migrate legacy ``sau/<root>`` media into the tiered Drive layout.

Dry-run by default. It never deletes or moves anything unless ``--apply`` is
passed, and it refuses to apply while any record is unresolved or its recorded
byte size disagrees with the object on Drive.

The layout and the mapping rules live in :mod:`myUtils.drive_layout`; this
script is only the rclone I/O plus the database bookkeeping. A record is
promoted by *moving the Drive object to a tiered root and repointing
``file_records.storage_backend_id`` at the matching ``storage_backends`` row*.
``file_records.storage_key`` is deliberately left untouched: it is the key
relative to the media root and the restore code joins
``endpoint + "/" + storage_key``, so preserving the key is what makes a
half-finished migration safe.

Usage::

    python scripts/migrate_drive_layout.py                 # dry-run, prints plan
    python scripts/migrate_drive_layout.py --json          # machine-readable plan
    python scripts/migrate_drive_layout.py --apply         # operator-approved

``--apply`` additionally creates the tiered ``storage_backends`` rows when they
are missing (additive; legacy rows are kept so un-migrated records still
restore).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sqlite3
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from myUtils import drive_layout  # noqa: E402

REMOTE = drive_layout.DEFAULT_REMOTE


def _run_rclone(config: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["rclone", "--config", str(config), "--tpslimit", "1", "--tpslimit-burst", "1", *args],
        capture_output=True,
        text=True,
    )


def list_remote_sizes(
    config: Path, remote: str, root: str, *, attempts: int = 3
) -> dict[str, int]:
    """Return ``{relative_key: bytes}`` for one root, with bounded retries."""
    endpoint = drive_layout.legacy_endpoint(root)
    last = ""
    for attempt in range(attempts):
        completed = _run_rclone(
            config, "lsjson", "-R", "--files-only", f"{remote}:{endpoint}"
        )
        if completed.returncode == 0:
            try:
                entries = json.loads(completed.stdout or "[]")
            except ValueError as exc:  # pragma: no cover - rclone emits valid JSON
                last = f"could not parse rclone output: {exc}"
                entries = []
            else:
                return {
                    entry["Path"]: int(entry["Size"])
                    for entry in entries
                    if entry.get("Path") is not None
                }
        else:
            last = completed.stderr.strip() or f"rc={completed.returncode}"
        if attempt < attempts - 1:
            time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"remote listing failed for {endpoint}: {last}")


def _canonical_key(root: str, storage_key: str, all_keys: set[str]) -> str | None:
    """Resolve the stored key to an object that actually exists.

    Legacy rows disagree about whether an ``uploads/`` key carries its root
    prefix (``uploads/<name>``) or not (``<name>``); both are seen in
    production. Return whichever of the obvious spellings is present so the
    migration moves real bytes instead of reporting a false "missing".
    """
    stripped = str(storage_key or "").strip("/")
    candidates = [stripped]
    if root == "uploads":
        if stripped.startswith("uploads/"):
            candidates.append(stripped[len("uploads/") :])
        else:
            candidates.append(f"uploads/{stripped}")
    for candidate in candidates:
        if candidate in all_keys:
            return candidate
    return None


def build_plan(
    conn: sqlite3.Connection,
    remote_sizes: dict[str, dict[str, int]],
    remote: str,
) -> dict:
    conn.row_factory = sqlite3.Row
    target_rows = conn.execute(
        "SELECT t.status AS status, j.payload_json AS payload_json "
        "FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id"
    ).fetchall()
    published_ids, published_keys = drive_layout.published_refs_from_payloads(
        [(row["status"], row["payload_json"]) for row in target_rows]
    )

    backends = {
        row["id"]: row
        for row in conn.execute("SELECT * FROM storage_backends").fetchall()
    }

    moves: list[dict] = []
    unresolved: list[dict] = []
    already: list[dict] = []
    for row in conn.execute(
        "SELECT id, file_path, filesize, storage_key, storage_backend_id "
        "FROM file_records WHERE storage_key IS NOT NULL ORDER BY id"
    ).fetchall():
        endpoint = None
        backend = backends.get(row["storage_backend_id"])
        if backend is not None:
            endpoint = backend["endpoint"]
        if not endpoint or backend["provider"] != "rclone":
            # Not a Drive object; S3/share rows are out of scope.
            continue
        if drive_layout.is_archive(endpoint):
            continue
        tier, root = drive_layout.split_endpoint(endpoint)
        if root is None:
            unresolved.append(
                {
                    "id": row["id"],
                    "file_path": row["file_path"],
                    "reason": "unrecognised rclone endpoint",
                    "endpoint": endpoint,
                }
            )
            continue

        split = drive_layout.split_media_path(row["file_path"])
        path_root = split[0] if split else None
        if path_root is None or path_root != root:
            unresolved.append(
                {
                    "id": row["id"],
                    "file_path": row["file_path"],
                    "reason": "file_path does not match endpoint root",
                    "endpoint": endpoint,
                }
            )
            continue

        if tier is not None:
            already.append({"id": row["id"], "endpoint": endpoint})
            continue

        keys = remote_sizes.get(root, {})
        key = _canonical_key(root, row["storage_key"], set(keys))
        if key is None:
            unresolved.append(
                {
                    "id": row["id"],
                    "file_path": row["file_path"],
                    "reason": "no such object under legacy endpoint",
                    "endpoint": endpoint,
                    "storage_key": row["storage_key"],
                }
            )
            continue
        remote_size = keys[key]
        recorded = row["filesize"]
        if recorded is not None and int(round(float(recorded))) != remote_size:
            unresolved.append(
                {
                    "id": row["id"],
                    "file_path": row["file_path"],
                    "reason": "recorded size does not match remote object",
                    "endpoint": endpoint,
                    "storage_key": key,
                    "db_size": int(round(float(recorded))),
                    "remote_size": remote_size,
                }
            )
            continue

        desired_tier = drive_layout.tier_for_media(
            row["id"],
            row["file_path"],
            published_ids=published_ids,
            published_keys=published_keys,
        )
        source_spec, dest_spec = drive_layout.object_move(remote, endpoint, key, desired_tier)
        moves.append(
            {
                "id": row["id"],
                "file_path": row["file_path"],
                "tier": desired_tier,
                "root": root,
                "storage_key": key,
                "required_key_fix": key != str(row["storage_key"]).strip("/"),
                "bytes": remote_size,
                "source": source_spec,
                "destination": dest_spec,
                "from_endpoint": endpoint,
                "to_endpoint": drive_layout.migrate_endpoint(endpoint, desired_tier),
            }
        )

    return {
        "remote": remote,
        "published_source_ids": len(published_ids),
        "published_media_keys": len(published_keys),
        "moves": moves,
        "unresolved": unresolved,
        "already_tiered": already,
        "summary": {
            "planned": len(moves),
            "published": sum(1 for m in moves if m["tier"] == "published"),
            "inbox": sum(1 for m in moves if m["tier"] == "inbox"),
            "already_tiered": len(already),
            "unresolved": len(unresolved),
            "bytes_to_move": sum(m["bytes"] for m in moves),
        },
    }


def ensure_tiered_backends(conn: sqlite3.Connection, remote: str) -> dict[tuple[str, str], int]:
    """Create any missing tiered rclone backend rows; return ``{(tier,root): id}``."""
    ids: dict[tuple[str, str], int] = {}
    for tier in drive_layout.TIERS:
        for root in drive_layout.MEDIA_ROOTS:
            endpoint = drive_layout.tier_endpoint(tier, root)
            row = conn.execute(
                "SELECT id FROM storage_backends WHERE provider='rclone' "
                "AND bucket=? AND endpoint=?",
                (remote, endpoint),
            ).fetchone()
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO storage_backends "
                    "(slug,label,provider,bucket,region,endpoint,access_key,secret_key,cdn_url,is_default,enabled) "
                    "VALUES (?,?,?,?,?,?,?,?,?,0,1)",
                    (
                        f"gdrive-{tier}-{root}",
                        f"GDrive {tier} {root}",
                        "rclone",
                        remote,
                        "",
                        endpoint,
                        "",
                        "",
                        "",
                    ),
                )
                ids[(tier, root)] = int(cursor.lastrowid)
            else:
                ids[(tier, root)] = int(row[0])
    conn.commit()
    return ids


def apply_plan(
    conn: sqlite3.Connection,
    plan: dict,
    *,
    remote: str,
    config: Path,
    dry_run_io: bool = False,
) -> list[dict]:
    """Move each planned object and repoint its record. Returns applied moves."""
    backend_ids = ensure_tiered_backends(conn, remote)
    applied: list[dict] = []
    for move in plan["moves"]:
        if not dry_run_io:
            completed = _run_rclone(config, "moveto", move["source"], move["destination"])
            if completed.returncode != 0:
                raise RuntimeError(
                    f"rclone moveto failed for {move['source']}: {completed.stderr.strip()}"
                )
        backend_id = backend_ids[(move["tier"], move["root"])]
        conn.execute(
            "UPDATE file_records SET storage_key=?, storage_backend_id=? WHERE id=?",
            (move["storage_key"], backend_id, move["id"]),
        )
        applied.append({**move, "backend_id": backend_id})
    conn.commit()
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=BASE_DIR / "db" / "database.db")
    parser.add_argument(
        "--rclone-config",
        type=Path,
        default=Path.home() / ".config/rclone/rclone.conf",
    )
    parser.add_argument("--remote", default=REMOTE)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the moves and database updates (default: dry-run)",
    )
    parser.add_argument("--json", action="store_true", help="emit the plan as JSON")
    parser.add_argument(
        "--roots",
        default=",".join(drive_layout.MEDIA_ROOTS),
        help="comma-separated media roots to include",
    )
    args = parser.parse_args()

    roots = [r.strip() for r in args.roots.split(",") if r.strip()]
    for root in roots:
        drive_layout.normalise_root(root)  # fail fast on a typo

    remote_sizes: dict[str, dict[str, int]] = {}
    try:
        for root in roots:
            remote_sizes[root] = list_remote_sizes(args.rclone_config, args.remote, root)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        # Restrict the plan to the requested roots.
        plan = build_plan(conn, remote_sizes, args.remote)
        plan["moves"] = [m for m in plan["moves"] if m["root"] in roots]
        plan["summary"]["planned"] = len(plan["moves"])
        plan["summary"]["published"] = sum(1 for m in plan["moves"] if m["tier"] == "published")
        plan["summary"]["inbox"] = sum(1 for m in plan["moves"] if m["tier"] == "inbox")
        plan["summary"]["bytes_to_move"] = sum(m["bytes"] for m in plan["moves"])

        if args.apply:
            if plan["unresolved"]:
                print(
                    "Refusing --apply: unresolved records must be fixed first "
                    f"({len(plan['unresolved'])} found).",
                    file=sys.stderr,
                )
                print(json.dumps(plan, indent=2, ensure_ascii=False))
                return 3
            applied = apply_plan(conn, plan, remote=args.remote, config=args.rclone_config)
            plan["applied"] = applied
            plan["mode"] = "apply"
            print(f"Applied {len(applied)} object moves.")
        else:
            plan["mode"] = "dry-run"

        if args.json:
            print(json.dumps(plan, indent=2, ensure_ascii=False))
        else:
            _print_human(plan)
        return 0 if not plan["unresolved"] or not args.apply else 3
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        print(f"migration failed: {exc}", file=sys.stderr)
        return 4
    finally:
        conn.close()


def _print_human(plan: dict) -> None:
    summary = plan["summary"]
    print(f"Mode: {plan['mode']}  remote: {plan['remote']}")
    print(
        "Planned: {planned} moves ({published} published, {inbox} inbox), "
        "{bytes_to_move} bytes; already_tiered={already_tiered}; unresolved={unresolved}".format(
            **summary
        )
    )
    print()
    print("Planned moves (first 200):")
    for move in plan["moves"][:200]:
        fix = " [key-normalised]" if move["required_key_fix"] else ""
        print(f"  [{move['tier']:9s}] {move['source']}  ->  {move['destination']}{fix}")
    if len(plan["moves"]) > 200:
        print(f"  ... {len(plan['moves']) - 200} more")
    if plan["unresolved"]:
        print()
        print(f"Unresolved ({len(plan['unresolved'])}) — apply will refuse:")
        for item in plan["unresolved"][:100]:
            print(f"  id={item['id']} {item.get('reason')}: {item.get('file_path')}")
    print()
    print("Dry-run only; nothing moved. Pass --apply after reviewing and backing up the DB.")


if __name__ == "__main__":
    raise SystemExit(main())
