#!/usr/bin/env python3
"""Humanize existing pending drafts (no media, no full regeneration).

The generation prompt now carries humanizer + Taiwan-Mandarin rules, but drafts
written before that still read like AI output (and some are Simplified Chinese).
This rewrites the existing caption and rich metadata in place with a single
text-only LLM pass — it never touches the media, so it is fast and cheap enough
to run over the whole calendar.

For a zh-Hant target it enforces Traditional Chinese (Taiwan) and rejects a
Simplified answer. Dry-run is the default; ``--apply`` writes to
``campaign_posts.draft_json`` and the queued job payload.

Usage::

    python scripts/humanize_drafts.py --within-days 60        # dry run
    python scripts/humanize_drafts.py --within-days 60 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myUtils import content_rules  # noqa: E402
from myUtils import llm_client  # noqa: E402
from myUtils import platform_limits  # noqa: E402
from myUtils import profiles as profile_registry  # noqa: E402
from myUtils import publish_orchestrator  # noqa: E402

DB_PATH = Path(os.environ.get("SAU_DB_PATH") or (REPO_ROOT / "db" / "database.db"))

HUMANIZER = (
    "Rewrite the supplied social-media copy so it reads like a real person "
    "wrote it, keeping the same meaning, facts and hashtags. Vary sentence "
    "length, prefer concrete detail, use contractions. NEVER use these AI "
    "tells: delve, in today's fast-paced world, unleash, elevate, embark, "
    "testament to, tapestry, navigate the landscape, game-changer, seamlessly, "
    "leverage, utilize, \"it's not just X, it's Y\", \"whether you're a X or a "
    "Y\", in conclusion, furthermore, moreover. No tidy tricolon, no emoji spam."
)
TAIWAN = (
    " Write Traditional Chinese exactly as used in Taiwan, never Simplified. "
    "Use Taiwan vocabulary and full-width punctuation: 影片 (not 视频), 網路 "
    "(not 网络), 資訊 (not 信息), 軟體 (not 软件), 品質 (not 质量), 專案 (not "
    "项目), 透過 (not 通过). Do not write English sentences inside the Chinese."
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _language(account) -> str:
    config = getattr(account, "config", None) or {}
    return str(
        config.get("audience_language") or config.get("audienceLanguage") or ""
    ).strip()


def find_targets(
    *,
    db_path: Path = DB_PATH,
    within_days: int | None = None,
    platform: str | None = None,
    limit: int | None = None,
    force: bool = False,
) -> list[dict]:
    cutoff = None
    if within_days:
        cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=within_days)
        ).isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        query = (
            "SELECT t.id AS target_id, t.job_id, t.account_ref, t.schedule_at,"
            " j.platform, j.payload_json "
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
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            continue
        draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
        message = str(draft.get("message") or payload.get("message") or "").strip()
        if not content_rules.is_usable_copy(message):
            continue
        if draft.get("_humanizedAt") and not force:
            # Already humanized; a re-run should only pick up the ones that
            # failed the first time (usually a transient LLM 503).
            continue
        out.append(
            {
                "target_id": int(row["target_id"]),
                "job_id": int(row["job_id"]),
                "account_ref": row["account_ref"],
                "platform": row["platform"],
                "schedule_at": row["schedule_at"],
                "draft": draft,
            }
        )
        if limit and len(out) >= limit:
            break
    return out


def humanize_one(row: dict, *, db_path: Path = DB_PATH) -> dict:
    try:
        account = profile_registry.get_account(
            int(str(row["account_ref"]).split(":", 1)[1]), db_path=db_path
        )
    except Exception as exc:  # noqa: BLE001
        return {**row, "ok": False, "error": f"account: {exc}"}
    language = _language(account)
    profile = profile_registry.get_profile(account.profile_id, db_path=db_path)
    system_prompt = str((profile.settings or {}).get("systemPrompt") or "").strip() or (
        "You write natural, human social-media copy."
    )
    instruction = HUMANIZER + (TAIWAN if "zh" in language.lower() else "")
    rule = content_rules.get_platform_rule(row["platform"])
    draft = row["draft"]
    source = {
        "message": draft.get("message") or "",
        "title": draft.get("title") or "",
        "summary": draft.get("summary") or "",
        "description": draft.get("description") or "",
        "altText": draft.get("altText") or "",
        "firstComment": draft.get("firstComment") or "",
        "hashtags": draft.get("hashtags") or [],
    }
    user_prompt = (
        f"{instruction}\n\n"
        f"Audience language: {language or 'English'}.\n"
        f"Max message characters: {rule.max_chars if rule.max_chars is not None else 'none'}.\n"
        f"Keep the hashtags unchanged.\n\n"
        f"Original JSON:\n{json.dumps(source, ensure_ascii=False)}\n\n"
        "Return ONLY a JSON object with the same keys (message, title, summary, "
        "description, altText, firstComment, hashtags)."
    )
    try:
        result = llm_client.generate_chat_completion(
            system_prompt, user_prompt, temperature=0.7, response_json=True, timeout_seconds=120
        )
        rewritten = result.parsed_json or llm_client.coerce_json_object(result.content) or {}
    except Exception as exc:  # noqa: BLE001
        return {**row, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    message = str(rewritten.get("message") or "").strip()
    if not content_rules.is_usable_copy(message):
        return {**row, "ok": False, "error": "rewrite is empty/placeholder"}
    if language and not content_rules.message_matches_language(message, language):
        return {**row, "ok": False, "error": f"rewrite language mismatch ({language})"}
    merged = dict(draft)
    for key in ("message", "title", "summary", "description", "altText", "firstComment"):
        if rewritten.get(key):
            merged[key] = rewritten[key]
    if rewritten.get("hashtags"):
        merged["hashtags"] = rewritten["hashtags"]
    prepared = content_rules.prepare_platform_draft(
        row["platform"],
        merged,
        contact_details=str(draft.get("contactDetails") or ""),
        cta=str(draft.get("cta") or ""),
        default_hashtags=draft.get("hashtags") or [],
    )
    prepared["_humanizedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {**row, "ok": True, "draft": prepared, "new_message": prepared["message"]}


def apply_one(result: dict, *, db_path: Path = DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM publish_jobs WHERE id = ?", (result["job_id"],)
        ).fetchone()
        payload = json.loads(row["payload_json"] or "{}")
        post_id = payload.get("campaignPostId")
        payload["draft"] = result["draft"]
        payload["message"] = result["new_message"]
        with conn:
            conn.execute(
                "UPDATE publish_jobs SET payload_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), result["job_id"]),
            )
            if post_id is not None:
                conn.execute(
                    "UPDATE campaign_posts SET draft_json = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (json.dumps(result["draft"], ensure_ascii=False), post_id),
                )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--within-days", type=int, default=60)
    parser.add_argument("--platform", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--redo", action="store_true", help="re-humanize even drafts already done")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    targets = find_targets(
        db_path=db_path, within_days=args.within_days, platform=args.platform,
        limit=args.limit, force=args.redo,
    )
    print(f"pending drafts to humanize: {len(targets)}")
    if not args.apply:
        for row in targets[:10]:
            print(f"  target {row['target_id']:>6} {row['platform']:10} {row['account_ref']:12} @ {row['schedule_at']}")
        print("\nDRY RUN: re-run with --apply to rewrite.")
        return 0
    ok = errors = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(humanize_one, row, db_path=db_path) for row in targets]
        for future in as_completed(futures):
            result = future.result()
            if result.get("ok"):
                try:
                    apply_one(result, db_path=db_path)
                    ok += 1
                    if ok % 25 == 0:
                        print(f"  humanized {ok}...")
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    print(f"  WRITE FAILED {result['target_id']}: {exc}")
            else:
                errors += 1
                if errors <= 15:
                    print(f"  FAILED {result['target_id']}: {result.get('error')}")
    print(f"\nhumanized={ok} failed={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
