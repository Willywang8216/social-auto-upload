#!/usr/bin/env python3
"""Regenerate pending drafts that are placeholders or in the wrong language.

The content guard blocks these before publishing, but blocking is not a fix:
the copy still needs to exist, in the account's language, and roughly matching
the media. This walks the pending targets whose draft is unusable (placeholder
or language mismatch) and regenerates it with the same per-account generator
the Publish Center uses — including the image(s)/video frame so the LLM can
describe what is actually shown.

Dry-run is the default. ``--apply`` writes the new copy to both the
``campaign_posts.draft_json`` and the queued job payload. Always back up the DB
first.

Usage::

    python scripts/regenerate_bad_drafts.py --within-days 30        # dry run
    python scripts/regenerate_bad_drafts.py --within-days 30 --apply
    python scripts/regenerate_bad_drafts.py --platform twitter --limit 20 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myUtils import content_rules  # noqa: E402
from myUtils import media_groups as media_group_store  # noqa: E402
from myUtils import media_pipeline  # noqa: E402
from myUtils import profiles as profile_registry  # noqa: E402
from myUtils import publish_orchestrator  # noqa: E402
from myUtils import worker  # noqa: E402

DB_PATH = Path(os.environ.get("SAU_DB_PATH") or (REPO_ROOT / "db" / "database.db"))


def _connect(db_path: Path) -> sqlite3.Connection:
    # The worker writes the same SQLite file in another process; a busy timeout
    # avoids "database is locked" when the two collide.
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _account_language(config: dict) -> str:
    return str(
        config.get("audience_language") or config.get("audienceLanguage") or ""
    ).strip()


# An LLM safety refusal must never be written back as copy.
_REFUSAL_RE = re.compile(
    r"^\s*(?:i can'?t|i cannot|i'?m unable|i am unable|i won'?t|i will not|"
    r"i'?m sorry,? but|as an ai|i must decline|cannot assist|can'?t assist|"
    r"sorry,? (?:but )?i)",
    re.IGNORECASE,
)


def _bad_reason(message: str, language: str) -> str | None:
    if not content_rules.is_usable_copy(message):
        return "placeholder/generic"
    if _REFUSAL_RE.match(message) and len(message) < 400:
        return "llm refusal"
    if language and not content_rules.message_matches_language(message, language):
        return f"language mismatch (want {language})"
    return None


def find_bad_targets(
    *,
    db_path: Path = DB_PATH,
    within_days: int | None = None,
    platform: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    cutoff = None
    if within_days:
        cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=within_days)
        ).isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        langs = {
            row["id"]: json.loads(row["config_json"] or "{}")
            for row in conn.execute("SELECT id, config_json FROM accounts")
        }
        query = (
            "SELECT t.id AS target_id, t.job_id, t.account_ref, t.schedule_at,"
            " j.profile_id, j.platform, j.payload_json "
            "FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id "
            "WHERE t.status = 'pending'"
        )
        params: list[object] = []
        if cutoff:
            query += " AND (t.schedule_at IS NULL OR t.schedule_at <= ?)"
            params.append(cutoff)
        if platform:
            query += " AND j.platform = ?"
            params.append(platform)
        query += " ORDER BY t.schedule_at IS NULL, t.schedule_at, t.id"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            continue
        draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
        message = str(draft.get("message") or payload.get("message") or "")
        try:
            account_id = int(str(row["account_ref"]).split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        language = _account_language(langs.get(account_id) or {})
        reason = _bad_reason(message, language)
        if not reason:
            continue
        out.append(
            {
                "target_id": int(row["target_id"]),
                "job_id": int(row["job_id"]),
                "account_ref": row["account_ref"],
                "platform": row["platform"],
                "profile_id": row["profile_id"],
                "schedule_at": row["schedule_at"],
                "reason": reason,
                "old_message": message[:120],
            }
        )
        if limit and len(out) >= limit:
            break
    return out


def _media_context(payload: dict, db_path: Path, workdir: Path) -> dict:
    """Build the media context (local images + a video frame) for the LLM."""
    context = {
        "imageLocalPaths": [],
        "videoLocalPath": "",
        "transcriptText": "",
        "imageUrls": [],
        "videoUrl": "",
    }
    video_path = ""
    for artifact in payload.get("artifacts") or []:
        role = str((artifact.get("metadata") or {}).get("role") or "").lower()
        local = str(artifact.get("local_path") or "")
        public = str(artifact.get("public_url") or "")
        if role == "image" and local:
            context["imageLocalPaths"].append(local)
            if public:
                context["imageUrls"].append(public)
        elif role == "video" and local:
            context["videoLocalPath"] = local
            if public:
                context["videoUrl"] = public
            video_path = local
    # A video has no image for the LLM; grab one frame as a visual proxy so the
    # copy describes what is on screen instead of inventing.
    if not context["imageLocalPaths"] and video_path and Path(video_path).is_file():
        try:
            frames = media_pipeline.extract_video_screenshots(
                video_path, workdir, count=1, seed=7
            )
            context["imageLocalPaths"] = [str(frame) for frame in frames]
        except Exception:  # noqa: BLE001 - a missing frame is not fatal
            pass
    return context


def regenerate_one(row: dict, *, db_path: Path = DB_PATH) -> dict:
    """Regenerate one target's draft, returning a result dict (never raises)."""
    import sau_backend  # imported lazily; builds the Flask app once

    conn = _connect(db_path)
    try:
        payload_row = conn.execute(
            "SELECT payload_json FROM publish_jobs WHERE id = ?", (row["job_id"],)
        ).fetchone()
        payload = json.loads(payload_row["payload_json"] or "{}")
        account = profile_registry.get_account(
            int(str(row["account_ref"]).split(":", 1)[1]), db_path=db_path
        )
        profile = profile_registry.get_profile(row["profile_id"], db_path=db_path)
        campaign_id = payload.get("campaignId")
        campaign = conn.execute(
            "SELECT media_group_id, metadata_json, notes FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
    finally:
        conn.close()

    if campaign is None:
        return {**row, "ok": False, "error": "campaign not found"}
    media_group = media_group_store.get_media_group(
        campaign["media_group_id"], db_path=db_path
    )
    metadata = {}
    try:
        metadata = json.loads(campaign["metadata_json"] or "{}")
    except ValueError:
        metadata = {}
    brief = str(metadata.get("notes") or campaign["notes"] or "")
    request_data = publish_orchestrator._request_data_for_options(
        brief=brief, options={}, profile=profile
    )
    request_data["title"] = str(metadata.get("title") or "")

    work_payload = {**payload, "_db_path": str(db_path)}
    restored_paths = [
        str(a.get("local_path"))
        for a in (work_payload.get("artifacts") or [])
        if a.get("local_path") and not Path(str(a["local_path"])).is_file()
    ]
    try:
        worker._ensure_artifact_paths_local(work_payload, db_path=db_path)
    except Exception as exc:  # noqa: BLE001 - still try with whatever is local
        restore_error = str(exc)[:160]
    else:
        restore_error = ""

    try:
        with tempfile.TemporaryDirectory(prefix="regen-") as workdir:
            media_context = _media_context(work_payload, db_path, Path(workdir))
            try:
                draft = sau_backend._generate_account_draft(
                    account,
                    profile,
                    media_group,
                    request_data,
                    media_context,
                    regenerate=True,
                )
            except Exception as exc:  # noqa: BLE001 - report, never abort the batch
                return {**row, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:220]}
    finally:
        # The frame is already extracted; delete the videos this run pulled back
        # from Drive so a long regeneration does not fill the disk.
        for path in restored_paths:
            try:
                Path(path).unlink()
            except OSError:
                pass
    message = str(draft.get("message") or "").strip()
    reason = _bad_reason(message, _account_language(account.config or {}))
    if reason:
        return {**row, "ok": False, "error": f"regenerated copy still {reason}"}
    return {**row, "ok": True, "draft": draft, "new_message": message}


def apply_one(result: dict, *, db_path: Path = DB_PATH) -> None:
    """Persist a regenerated draft to the campaign post and the queued job."""
    conn = _connect(db_path)
    try:
        payload_row = conn.execute(
            "SELECT payload_json FROM publish_jobs WHERE id = ?", (result["job_id"],)
        ).fetchone()
        payload = json.loads(payload_row["payload_json"] or "{}")
        campaign_post_id = payload.get("campaignPostId")
        payload["draft"] = result["draft"]
        payload["message"] = result["new_message"]
        with conn:
            conn.execute(
                "UPDATE publish_jobs SET payload_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), result["job_id"]),
            )
            if campaign_post_id is not None:
                conn.execute(
                    "UPDATE campaign_posts SET draft_json = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (json.dumps(result["draft"], ensure_ascii=False), campaign_post_id),
                )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--within-days", type=int, default=30)
    parser.add_argument("--platform", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    targets = find_bad_targets(
        db_path=db_path,
        within_days=args.within_days,
        platform=args.platform,
        limit=args.limit,
    )
    print(f"bad pending drafts to regenerate: {len(targets)}")
    for row in targets[:10]:
        print(f"  target {row['target_id']:>6} {row['platform']:10} {row['account_ref']:12} "
              f"@ {row['schedule_at']}  <- {row['reason']}")
    if len(targets) > 10:
        print(f"  ... and {len(targets) - 10} more")
    if not args.apply:
        print("\nDRY RUN: re-run with --apply to regenerate and write.")
        return 0

    ok = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(regenerate_one, row, db_path=db_path) for row in targets]
        for future in as_completed(futures):
            result = future.result()
            if result.get("ok"):
                try:
                    apply_one(result, db_path=db_path)
                    ok += 1
                    print(f"  regenerated target {result['target_id']}: {result['new_message'][:70]}")
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    print(f"  WRITE FAILED target {result['target_id']}: {exc}")
            else:
                errors += 1
                print(f"  FAILED target {result['target_id']}: {result.get('error')}")
    print(f"\nregenerated={ok} failed={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
