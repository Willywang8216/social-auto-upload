#!/usr/bin/env python3
"""Create the platform-sized video splits that prep never produced.

The problem
-----------
A video longer than a platform's cap is meant to be split into equal parts, one
post each. For Threads (300 s cap) this has **never** happened:

    splits tagged split_for=twitter : 38
    splits tagged split_for=threads : 0

The splitter itself is fine - it produces three ~200 s parts for the 600 s
source, and the plan builder correctly derives a ``(300s, 1024MB) -> threads``
plan. The earlier prep runs simply never persisted it, and every already-prepped
campaign carries a payload without it. So any video over 300 s cannot publish to
Threads even though a valid split is one call away.

What this does
--------------
For each campaign whose video exceeds a target platform's cap and has no split
tagged for that platform, it:

  1. splits the source with ``media_prep.split_to_seconds``,
  2. uploads each part to remote storage (Threads fetches by URL),
  3. records one ``remote_upload`` artifact per part, tagged
     ``split_for=[<platform>]`` with the duration/size/count metadata the
     publisher reads to pick parts.

It is **dry-run by default**. ``--apply`` performs the work.

Safety
------
* Only ADDS artifacts; it never deletes or mutates an existing row, so a bad run
  can be undone by removing the artifacts it created (they are listed).
* Skips a campaign that already has a split for the platform.
* Refuses to run for a platform whose split already exists, or when the source
  file is missing.
* ``--platform`` defaults to threads.

Usage
-----
    # see what would happen
    python scripts/backfill_platform_splits.py --campaign 2555

    # every campaign needing a threads split
    python scripts/backfill_platform_splits.py --all --platform threads

    # do it
    python scripts/backfill_platform_splits.py --campaign 2555 --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from myUtils import media_pipeline, media_prep, media_remote_storage, platform_limits  # noqa: E402


def _db_path() -> Path:
    import os

    configured = os.environ.get("SAU_DB_PATH")
    return Path(configured) if configured else REPO_ROOT / "db" / "database.db"


def _resolve(path) -> Path:
    """Resolve a stored path however it was written.

    Rows carry one of three shapes: an absolute host path
    (``/home/will/social-auto-upload/...``), an absolute *container* path
    (``/app/...``, which does not exist on the host), or a repo-relative path
    (``videoFile/...``). The worker normalises these before use; this script must
    do the same or it sees "source missing" for a file that is right there.
    """
    raw = str(path)
    candidate = Path(raw).expanduser()
    if candidate.is_file():
        return candidate
    # Strip whichever known root prefix the row carries and re-anchor it here.
    for prefix in ("/app", "/home/will/social-auto-upload"):
        if raw == prefix:
            return REPO_ROOT
        if raw.startswith(prefix + "/"):
            relative = raw[len(prefix) + 1 :]
            mapped = REPO_ROOT / relative
            if mapped.is_file():
                return mapped
            return mapped
    return REPO_ROOT / candidate


def _campaign_ids(connection: sqlite3.Connection, args) -> list[int]:
    if args.campaign:
        return [args.campaign]
    rows = connection.execute(
        "SELECT DISTINCT campaign_id FROM campaign_artifacts "
        "WHERE artifact_kind = 'watermarked_video' ORDER BY campaign_id"
    ).fetchall()
    return [int(r[0]) for r in rows]


def _already_split(connection: sqlite3.Connection, campaign_id: int, platform: str) -> bool:
    rows = connection.execute(
        "SELECT metadata_json FROM campaign_artifacts "
        "WHERE campaign_id = ? AND metadata_json LIKE '%part_index%'",
        (campaign_id,),
    ).fetchall()
    for (raw,) in rows:
        try:
            meta = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            continue
        if platform in (meta.get("split_for") or []):
            return True
    return False


def _source_for(connection: sqlite3.Connection, campaign_id: int) -> str | None:
    """The best available local copy of the campaign's video.

    The offload cron uploads ``generated/`` to Google Drive and removes the local
    file to reclaim disk, so a ``watermarked_video`` row often points at a path
    that is no longer on this host - 231 of 304 campaign directories are empty
    for exactly that reason, and it is by design. Prefer any artifact whose
    ``local_path`` still resolves, else fall back to a ``remote_upload`` row whose
    public URL can be fetched.
    """
    rows = connection.execute(
        "SELECT artifact_kind, local_path, public_url FROM campaign_artifacts "
        "WHERE campaign_id = ? AND local_path IS NOT NULL AND local_path != '' "
        "ORDER BY CASE artifact_kind WHEN 'watermarked_video' THEN 0 "
        "WHEN 'remote_upload' THEN 1 ELSE 2 END, id",
        (campaign_id,),
    ).fetchall()
    for _kind, local_path, _url in rows:
        resolved = _resolve(local_path)
        if resolved.is_file():
            return str(resolved)
    # Nothing local: hand back the URL of a hosted copy so the caller can fetch.
    for _kind, _local, url in rows:
        if str(url or "").startswith("https://"):
            return str(url)
    return str(rows[0][1]) if rows else None


def _fetch_to_local(url: str, campaign_id: int) -> Path | None:
    """Download a hosted artifact so it can be probed and split."""
    try:
        import urllib.request

        target = media_pipeline.build_campaign_workspace(campaign_id) / "offload_source.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            print(f"  fetching missing local copy from {url}")
            urllib.request.urlretrieve(url, target)
        return target
    except Exception as exc:  # noqa: BLE001
        print(f"  could not fetch {url}: {exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=int)
    parser.add_argument("--all", action="store_true", help="every campaign needing a split")
    parser.add_argument("--platform", default="threads")
    parser.add_argument("--apply", action="store_true", help="perform the work")
    args = parser.parse_args()
    if not args.campaign and not args.all:
        parser.error("pass --campaign N or --all")
    if args.campaign and args.all:
        parser.error("pass only one of --campaign / --all")

    db = _db_path()
    connection = sqlite3.connect(str(db))
    max_seconds = platform_limits.video_max_seconds(args.platform)
    max_mb = platform_limits.media_max_mb(args.platform)
    if not max_seconds and not max_mb:
        raise SystemExit(f"{args.platform} has no duration or size cap; nothing to split")

    print(
        f"{'APPLY' if args.apply else 'DRY RUN'}  platform={args.platform} "
        f"cap={max_seconds}s / {max_mb}MB  db={db}"
    )
    print("=" * 74)

    planned: list[tuple[int, Path, list[Path]]] = []
    for campaign_id in _campaign_ids(connection, args):
        if _already_split(connection, campaign_id, args.platform):
            continue
        stored = _source_for(connection, campaign_id)
        if not stored:
            continue
        if str(stored).startswith("https://"):
            source = _fetch_to_local(stored, campaign_id)
        else:
            source = _resolve(stored)
        if source is None or not Path(source).is_file():
            print(f"campaign {campaign_id}: source unavailable -> {stored}")
            continue
        source = Path(source)
        try:
            duration = float((media_prep.probe(str(source)) or {}).get("duration") or 0)
        except Exception as exc:  # noqa: BLE001
            print(f"campaign {campaign_id}: probe failed: {exc}")
            continue
        size_bytes = source.stat().st_size
        over_time = bool(max_seconds and duration > float(max_seconds))
        over_size = bool(max_mb and size_bytes > float(max_mb) * 1024 * 1024)
        if not (over_time or over_size):
            continue  # already fits; a split is unnecessary
        print(
            f"campaign {campaign_id}: {duration:.1f}s / {size_bytes / 1e6:.0f}MB "
            f"exceeds cap -> needs a {args.platform} split"
        )
        if not args.apply:
            planned.append((campaign_id, source, []))
            continue
        out_dir = media_pipeline.build_campaign_workspace(campaign_id)
        parts = media_prep.split_to_seconds(
            str(source),
            out_dir,
            max_seconds,
            max_bytes=(max_mb * 1024 * 1024) if max_mb else None,
        )
        if len(parts) <= 1:
            print(f"  skipping: split produced {len(parts)} part(s)")
            continue
        created = []
        for index, part in enumerate(parts, start=1):
            artifact = media_remote_storage.upload_artifact(
                part, campaign_id=campaign_id, artifact_subdir="videos"
            )
            part_duration = float((media_prep.probe(str(part)) or {}).get("duration") or 0)
            connection.execute(
                "INSERT INTO campaign_artifacts "
                "(campaign_id, source_file_record_id, artifact_kind, local_path, "
                " public_url, remote_path, metadata_json, created_at) "
                "SELECT ?, source_file_record_id, 'remote_upload', ?, ?, ?, ?, "
                "       CURRENT_TIMESTAMP "
                "FROM campaign_artifacts "
                "WHERE campaign_id = ? AND artifact_kind = 'watermarked_video' "
                "ORDER BY id LIMIT 1",
                (
                    campaign_id,
                    str(part),
                    artifact.public_url,
                    getattr(artifact, "remote_path", None),
                    json.dumps(
                        {
                            "role": "video",
                            "max_duration_seconds": part_duration,
                            "max_media_mb": part.stat().st_size / (1024 * 1024),
                            "part_index": index,
                            "part_count": len(parts),
                            "split_for": [args.platform],
                        }
                    ),
                    campaign_id,
                ),
            )
            created.append(part)
        connection.commit()
        print(f"  created {len(created)} part(s):")
        for part in created:
            print(f"    {part.name}")

    connection.close()
    if not args.apply:
        print()
        print(f"{len(planned)} campaign(s) would be split. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
