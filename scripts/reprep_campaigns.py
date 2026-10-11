#!/usr/bin/env python3
"""Re-prep campaigns whose live targets resolve only to placeholder/stub media.

Why this exists
---------------
The offload loop moves published media to Drive and deletes the local copy,
keeping a ``file_records`` mapping so the worker can restore it at claim time.
For a set of campaigns the *prepared* ``*_pub.mp4`` Drive object was replaced by
a 1-4 byte placeholder (a literal ``b"x"``) while the **raw source** stayed
intact on Drive. The worker deliberately refuses to substitute raw-source bytes
for a derived artifact (``myUtils/worker.py`` ``_record_for``, commit 96c3a4b)
and its media-signature guard rejects the placeholder, so those targets can
never publish. The only correct recovery is to re-run the campaign's artifact
pipeline from the raw source.

What it does
------------
1. Resolves every live target (``pending``/``retrying``/``running``) exactly the
   way ``worker._ensure_artifact_paths_local`` does, against a **live** Drive
   listing, and classifies each target as recoverable or not. A target is
   unrecoverable when no artifact it references can be obtained: no local file,
   the resolved Drive object is missing or smaller than ``--min-media-bytes``,
   and every HTTPS fallback (the artifact's own ``public_url`` and the record's
   ``storage_cdn_url``) is dead.
2. Groups the unrecoverable targets by campaign and checks whether the
   campaign's raw source is still on Drive. Campaigns with a missing source
   (dead test fixtures) are reported, never re-prepped.
3. For recoverable campaigns it cancels **only the broken targets** (never a
   ``running`` or ``succeeded`` one), reconstructs the async-prep
   ``prepRequest`` payload the legacy rows never had, and flips the campaign
   back to ``preparing``. The worker (``myUtils/campaigns.claim_next_preparing_campaign``)
   then re-runs artifact prep and enqueues fresh targets.

Safety
------
* DRY-RUN by default; ``--apply`` is required to write anything.
* The DB is snapshotted (SQLite online backup) before the first write.
* Bounded by ``--limit`` campaigns per invocation, so one run cannot run away.
* Resumable: cancelled broken targets drop out of the live set, so a re-run
  simply finds fewer campaigns.
* The six operator campaigns (2598-2603) are hard-excluded and asserted
  unchanged before and after.
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# The six operator campaigns must never be touched by an automated re-prep.
OPERATOR_CAMPAIGN_IDS = frozenset({2598, 2599, 2600, 2601, 2602, 2603})

CANCEL_REASON = (
    "cancelled: superseded by re-prep (prepared media was a placeholder)"
)

# Import the (read-only) verification helpers instead of re-implementing the
# worker's resolution. It is a script, but has no import side effects.
_spec = importlib.util.spec_from_file_location(
    "verify_pipeline_integrity", str(REPO / "scripts" / "verify_pipeline_integrity.py")
)
vpi = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(vpi)


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _https(url: object) -> bool:
    return str(url or "").startswith("https://")


def _iso(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def load_or_fetch_drive_listing(
    path: Path | None, remote: str, *, refresh: bool
) -> tuple[dict[str, int], Path]:
    """Return ``(path -> size)`` for ``<remote>`` and the file it was read from."""
    if path is not None and path.exists() and not refresh:
        return vpi.load_drive_listing(path), path
    target = path or (REPO / "logs" / "reprep-drive-listing.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["rclone", "lsf", remote, "-R", "--files-only", "--format", "ps"]
    print(f"  [drive] {(' '.join(cmd))}")
    with target.open("wb") as handle:
        proc = subprocess.run(cmd, stdout=handle, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise SystemExit(
            f"rclone listing failed ({proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    return vpi.load_drive_listing(target), target


class Resolver:
    """Classify one artifact the way the worker's restore path would."""

    def __init__(self, store: "vpi.Store", drive: dict[str, int], min_bytes: int):
        self.store = store
        self.drive = drive
        self.min_bytes = min_bytes
        self.probe_cache: dict[str, tuple[int, int]] = {}

    def _probe(self, url: str) -> tuple[int, int]:
        if url not in self.probe_cache:
            self.probe_cache[url] = vpi.probe_url(url)
        return self.probe_cache[url]

    def classify(self, artifact: dict) -> tuple[bool, str, int | None]:
        """Return ``(recoverable, detail, drive_bytes)`` for one artifact.

        Mirrors ``worker._ensure_artifact_paths_local``: a local file or a
        real Drive object is authoritative; a placeholder/missing Drive object
        falls through to the artifact's own HTTPS ``public_url`` and the
        record's ``storage_cdn_url``, each of which is probed for real bytes.
        """
        local_path = str(artifact.get("local_path") or "")
        if not local_path:
            return False, "no_local_path", None
        path = vpi._resolve_media_path(local_path)
        if not path.is_absolute():
            path = Path(vpi.BASE_DIR) / path
        if path.exists():
            return True, "local", path.stat().st_size

        row = vpi._record_for(self.store, artifact, path, stored=local_path) or {}
        nbytes: int | None = None
        storage_key = row.get("storage_key")
        backend_id = row.get("storage_backend_id")
        if storage_key and backend_id:
            backend = self.store.backend(backend_id) or {}
            key = vpi._full_drive_key(backend.get("endpoint"), storage_key)
            if key and key in self.drive:
                nbytes = self.drive[key]
                if nbytes >= self.min_bytes:
                    return True, f"drive:{nbytes}", nbytes

        for url in (artifact.get("public_url"), row.get("storage_cdn_url")):
            if not _https(url):
                continue
            code, got = self._probe(str(url))
            if code in (200, 206) and got > 0:
                return True, f"fallback_http_{code}", nbytes
        return False, "missing_or_placeholder", nbytes


def resolve_targets(
    store: "vpi.Store",
    resolver: Resolver,
) -> tuple[dict[int, set[int]], dict[int, set[int]], dict[int, list[dict]]]:
    """Return ``(live, broken, artifacts)`` keyed by campaign id.

    ``live`` is every target id seen; ``broken`` the subset that cannot obtain
    media; ``artifacts`` the payload artifacts of the broken targets.
    """
    rows = store.con.execute(
        """
        SELECT t.id AS tid, t.status, j.payload_json, p.campaign_id
        FROM publish_job_targets t
        JOIN publish_jobs j ON j.id = t.job_id
        JOIN campaign_posts p ON p.id = CAST(substr(t.file_ref, 15) AS INTEGER)
        WHERE t.status IN ('pending','retrying','running')
        ORDER BY t.id
        """
    ).fetchall()
    live: dict[int, set[int]] = collections.defaultdict(set)
    broken: dict[int, set[int]] = collections.defaultdict(set)
    artifacts: dict[int, list[dict]] = collections.defaultdict(list)
    for row in rows:
        cid = int(row["campaign_id"])
        live[cid].add(int(row["tid"]))
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        target_broken = False
        for artifact in payload.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            ok, detail, _bytes = resolver.classify(artifact)
            if not ok:
                target_broken = True
                artifacts[cid].append(
                    {
                        "target_id": int(row["tid"]),
                        "artifact_kind": artifact.get("artifact_kind"),
                        "local_path": artifact.get("local_path"),
                        "detail": detail,
                    }
                )
        if target_broken:
            broken[cid].add(int(row["tid"]))
    return live, broken, artifacts


def raw_source_available(store: "vpi.Store", drive: dict[str, int], campaign_id: int, min_bytes: int) -> tuple[bool, str]:
    """Whether the campaign's media group still has a real raw source on Drive."""
    row = store.con.execute(
        "SELECT media_group_id FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()
    if row is None:
        return False, "campaign_missing"
    items = store.con.execute(
        """
        SELECT mgi.file_record_id, fr.filename, fr.file_path, fr.filesize,
               fr.storage_key, fr.storage_backend_id
        FROM media_group_items mgi
        LEFT JOIN file_records fr ON fr.id = mgi.file_record_id
        WHERE mgi.media_group_id = ?
        ORDER BY mgi.sort_order, mgi.id
        """,
        (row["media_group_id"],),
    ).fetchall()
    if not items:
        return False, "no_media_group_items"
    for item in items:
        if item["file_record_id"] is None:
            continue
        backend = store.backend(item["storage_backend_id"])
        key = vpi._full_drive_key(
            (backend or {}).get("endpoint"), item["storage_key"]
        )
        size = drive.get(key) if key else None
        if size is not None and size >= min_bytes:
            return True, f"{key} ({size} bytes)"
    return False, "raw_source_not_on_drive"


def reconstruct_prep_request(campaign_row: sqlite3.Row, store: "vpi.Store") -> dict:
    """Rebuild the async-prep payload a legacy campaign never persisted.

    The artifact pipeline only reads ``brief``/``options``/``accountDrafts``/
    ``tiktokPostSettings``/``schedule`` (``myUtils/campaign_prep.run_campaign_prep``
    and ``_base_time``); ``mediaFilePaths`` is written for schema parity because
    the worker reloads the media group from the DB.
    """
    metadata = json.loads(campaign_row["metadata_json"] or "{}")
    notes = str(metadata.get("notes") or metadata.get("title") or "").strip()
    if not notes:
        draft = store.con.execute(
            "SELECT draft_json FROM campaign_posts WHERE campaign_id = ? "
            "ORDER BY id LIMIT 1",
            (campaign_row["id"],),
        ).fetchone()
        if draft is not None:
            try:
                notes = str(
                    json.loads(draft["draft_json"] or "{}").get("message") or ""
                ).strip()
            except (TypeError, ValueError):
                notes = ""

    # Preserve the intended wall clock. publish_job_targets.schedule_at is a
    # UTC-naive instant; submit's naive startAt is read as operator-local, so
    # convert back through the same zone before persisting.
    schedule: dict | None = None
    earliest = store.con.execute(
        """
        SELECT MIN(t.schedule_at) AS first_at
        FROM publish_job_targets t
        JOIN campaign_posts p ON p.id = CAST(substr(t.file_ref, 15) AS INTEGER)
        WHERE p.campaign_id = ?
        """,
        (campaign_row["id"],),
    ).fetchone()
    first_at = _iso(earliest["first_at"]) if earliest is not None else None
    if first_at:
        try:
            utc_dt = datetime.fromisoformat(first_at)
            local_dt = (
                utc_dt.replace(tzinfo=timezone.utc)
                .astimezone(ZoneInfo("Asia/Shanghai"))
                .replace(tzinfo=None)
            )
            schedule = {"startAt": local_dt.isoformat(timespec="seconds")}
        except ValueError:
            schedule = None

    media_paths: list[str] = []
    items = store.con.execute(
        """
        SELECT fr.filename, fr.file_path
        FROM media_group_items mgi
        JOIN file_records fr ON fr.id = mgi.file_record_id
        WHERE mgi.media_group_id = ?
        ORDER BY mgi.sort_order, mgi.id
        """,
        (campaign_row["media_group_id"],),
    ).fetchall()
    for item in items:
        path = str(item["file_path"] or "").strip()
        if not path:
            path = f"videoFile/{item['filename']}"
        media_paths.append(path)

    return {
        "brief": notes,
        "options": {"useLlm": True},
        "schedule": schedule,
        "accountDrafts": {},
        "tiktokPostSettings": {},
        "mediaFilePaths": media_paths,
    }


def snapshot_operator_campaigns(store: "vpi.Store") -> dict[int, tuple]:
    placeholders = ",".join("?" for _ in OPERATOR_CAMPAIGN_IDS)
    rows = store.con.execute(
        f"SELECT id, status, metadata_json, last_error, prepared_at, published_at "
        f"FROM campaigns WHERE id IN ({placeholders}) ORDER BY id",
        tuple(sorted(OPERATOR_CAMPAIGN_IDS)),
    ).fetchall()
    return {int(r["id"]): tuple(r) for r in rows}


def snapshot_operator_targets(store: "vpi.Store") -> dict[int, int]:
    placeholders = ",".join("?" for _ in OPERATOR_CAMPAIGN_IDS)
    rows = store.con.execute(
        f"""
        SELECT p.campaign_id AS cid, t.status AS status, COUNT(*) AS n
        FROM publish_job_targets t
        JOIN campaign_posts p ON p.id = CAST(substr(t.file_ref, 15) AS INTEGER)
        WHERE p.campaign_id IN ({placeholders})
        GROUP BY p.campaign_id, t.status
        """,
        tuple(sorted(OPERATOR_CAMPAIGN_IDS)),
    ).fetchall()
    out: dict[int, int] = {}
    for r in rows:
        out.setdefault(int(r["cid"]), 0)
        out[int(r["cid"])] += int(r["n"])
    return out


def backup_db(db_path: Path) -> Path:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = db_path.with_name(f"{db_path.name}.bak-pre-reprep-{stamp}")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def apply_campaign(
    db_path: Path,
    campaign_row: sqlite3.Row,
    broken_ids: set[int],
) -> tuple[int, str]:
    """Cancel the broken targets and requeue the campaign for prep.

    Returns ``(cancelled_count, summary)``.
    """
    now = _utc_now()
    cancelled = 0
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        for target_id in sorted(broken_ids):
            cur = con.execute(
                """
                UPDATE publish_job_targets
                SET status = 'cancelled', last_error = ?, finished_at = ?
                WHERE id = ? AND status IN ('pending','retrying')
                """,
                (CANCEL_REASON, now, target_id),
            )
            cancelled += cur.rowcount

        metadata = json.loads(campaign_row["metadata_json"] or "{}")
        metadata["prepRequest"] = reconstruct_prep_request(campaign_row, _STORE)
        metadata["_reprepAt"] = now
        metadata["_reprepReason"] = "placeholder_media"
        metadata.pop("_prepLease", None)
        metadata["_prepAttempts"] = 0

        con.execute(
            """
            UPDATE campaigns
            SET status = 'preparing', metadata_json = ?, last_error = NULL
            WHERE id = ? AND status != 'preparing'
            """,
            (json.dumps(metadata, ensure_ascii=False), campaign_row["id"]),
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return cancelled, f"campaign {campaign_row['id']} -> preparing"


_STORE: "vpi.Store" = None  # type: ignore[assignment]  # set in main()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(REPO / "db" / "database.db"))
    ap.add_argument(
        "--drive-listing",
        default="",
        help="reuse a snapshot from `rclone lsf ... --format ps` instead of listing live",
    )
    ap.add_argument(
        "--refresh-drive-listing",
        action="store_true",
        help="re-list Drive even when --drive-listing exists",
    )
    ap.add_argument("--remote", default="GDrive-willywang8216:sau")
    ap.add_argument(
        "--min-media-bytes",
        type=int,
        default=256,
        help="a Drive object smaller than this is a placeholder (default 256)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=5,
        help="maximum campaigns to re-prep in this invocation (default 5)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually cancel targets and requeue campaigns (default: dry run)",
    )
    ap.add_argument(
        "--campaign",
        type=int,
        action="append",
        default=[],
        help="force a specific campaign id (repeatable); still refuses operator campaigns",
    )
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    global _STORE
    _STORE = vpi.Store(db_path)

    listing_path = Path(args.drive_listing) if args.drive_listing else None
    drive, used_listing = load_or_fetch_drive_listing(
        listing_path, args.remote, refresh=args.refresh_drive_listing
    )
    print(f"  [drive] {len(drive)} objects from {used_listing}")

    resolver = Resolver(_STORE, drive, args.min_media_bytes)
    live, broken, artifacts = resolve_targets(_STORE, resolver)
    live.pop(None, None)

    # Operator campaigns are never candidates, even if a payload looks broken.
    for cid in OPERATOR_CAMPAIGN_IDS:
        broken.pop(cid, None)

    candidates = sorted(broken)
    if args.campaign:
        for cid in args.campaign:
            if cid in OPERATOR_CAMPAIGN_IDS:
                raise SystemExit(f"refusing: campaign {cid} is an operator campaign")
            broken.setdefault(cid, set())
            live.setdefault(cid, set())
            candidates = sorted(set(candidates) | {cid})

    print(
        f"\n  live targets        : {sum(len(v) for v in live.values())} "
        f"across {len(live)} campaigns"
    )
    print(
        f"  unrecoverable       : {sum(len(broken[c]) for c in candidates)} "
        f"targets across {len(candidates)} campaigns"
    )

    rows = {
        int(r["id"]): r
        for r in _STORE.con.execute("SELECT * FROM campaigns").fetchall()
    }

    recoverable: list[tuple[int, set[int], list[dict], str]] = []
    fixtures: list[tuple[int, set[int], str]] = []
    for cid in candidates:
        row = rows.get(cid)
        if row is None:
            continue
        if row["status"] == "preparing":
            print(f"  skip {cid}: already preparing")
            continue
        ok, detail = raw_source_available(_STORE, drive, cid, args.min_media_bytes)
        if ok:
            recoverable.append((cid, broken[cid], artifacts.get(cid, []), detail))
        else:
            fixtures.append((cid, broken[cid], detail))

    print(f"\n  recoverable (raw source on Drive): {len(recoverable)}")
    for cid, ids, _arts, detail in recoverable:
        print(f"    {cid}: {len(ids)} target(s)  src={detail[:70]}")

    print(f"\n  no raw source (cannot re-prep / fixtures): {len(fixtures)}")
    for cid, ids, detail in fixtures:
        print(f"    {cid}: {len(ids)} target(s)  ({detail})")

    selected = recoverable[: max(0, args.limit)]
    if args.campaign:
        forced = set(args.campaign)
        selected = [entry for entry in recoverable if entry[0] in forced][
            : max(0, args.limit)
        ]

    if not args.apply:
        print(
            f"\n  DRY RUN: would re-prep {len(selected)} campaign(s) "
            "(pass --apply to write). No changes made."
        )
        payload = {
            "dry_run": True,
            "listing": str(used_listing),
            "live_targets": sum(len(v) for v in live.values()),
            "candidates": candidates,
            "recoverable": [
                {"campaign_id": cid, "targets": sorted(ids), "source": detail}
                for cid, ids, _a, detail in recoverable
            ],
            "fixtures": [
                {"campaign_id": cid, "targets": sorted(ids), "detail": detail}
                for cid, ids, detail in fixtures
            ],
            "selected": [cid for cid, _i, _a, _d in selected],
        }
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(payload, indent=2))
        return 0

    if not selected:
        print("\n  nothing to do (no recoverable campaigns selected)")
        return 0

    operator_before = snapshot_operator_campaigns(_STORE)
    operator_targets_before = snapshot_operator_targets(_STORE)

    backup = backup_db(db_path)
    print(f"\n  [backup] {backup}")
    print(f"  applying to {len(selected)} campaign(s)")

    applied: list[dict] = []
    for cid, ids, _arts, detail in selected:
        row = _STORE.con.execute(
            "SELECT * FROM campaigns WHERE id = ?", (cid,)
        ).fetchone()
        cancelled, summary = apply_campaign(db_path, row, ids)
        print(f"    {summary}: cancelled {cancelled} broken target(s)")
        applied.append(
            {
                "campaign_id": cid,
                "targets": sorted(ids),
                "cancelled": cancelled,
                "source": detail,
            }
        )

    # Re-open the store so the verification below sees the committed rows.
    _STORE.con.close()
    _STORE = vpi.Store(db_path)
    operator_after = snapshot_operator_campaigns(_STORE)
    operator_targets_after = snapshot_operator_targets(_STORE)
    if operator_before != operator_after or operator_targets_before != operator_targets_after:
        raise SystemExit(
            "FATAL: an operator campaign (2598-2603) changed during re-prep; "
            "investigate before continuing"
        )
    print("  operator campaigns 2598-2603: UNCHANGED")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "dry_run": False,
                    "backup": str(backup),
                    "applied": applied,
                    "fixtures": [
                        {"campaign_id": cid, "targets": sorted(ids), "detail": detail}
                        for cid, ids, detail in fixtures
                    ],
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
