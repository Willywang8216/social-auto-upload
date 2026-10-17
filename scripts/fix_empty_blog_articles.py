#!/usr/bin/env python3
"""Re-generate + re-publish blog articles that were published as near-empty bodies.

Root cause: the blog platform (nw_sw_blog / teaching_blog) went through the
generic social-media prompt, so short clips (no transcript, thin brief) got a
title-only "article". The prompt + a length guard are now fixed in sau_backend.
This script re-generates the full article for each affected campaign and
re-publishes the MDX to the sexualwill_static GitHub repo.

Usage:
  .venv/bin/python scripts/fix_empty_blog_articles.py --dry-run
  .venv/bin/python scripts/fix_empty_blog_articles.py --apply
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myUtils import profiles as profile_registry  # noqa: E402
from sau_backend import (  # noqa: E402
    _build_preview_media_context,
    _generate_account_draft,
    _preview_media_group_stub,
)
from myUtils import prepared_publishers  # noqa: E402

DB = REPO / "db" / "database.db"
BLOG_PLATFORMS = {"nw_sw_blog", "teaching_blog"}
MIN_LEN = 600


def _short_blog_campaigns() -> dict[int, list[int]]:
    """campaign_id -> [campaign_post_ids with a too-short blog message]."""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, campaign_id, draft_json FROM campaign_posts WHERE platform IN (?, ?)",
        tuple(BLOG_PLATFORMS),
    ).fetchall()
    out: dict[int, list[int]] = {}
    for r in rows:
        try:
            draft = json.loads(r["draft_json"] or "{}")
        except Exception:
            continue
        msg = str(draft.get("message") or "")
        if len(msg.strip()) < MIN_LEN:
            out.setdefault(int(r["campaign_id"]), []).append(int(r["id"]))
    con.close()
    return out


def _campaign_meta(campaign_id: int) -> tuple[int, str, str]:
    """(profile_id, brief, title). Title falls back to the existing blog post's
    title (the image filename / original topic) so empty-brief re-generation
    stays on-topic instead of hallucinating a new subject."""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT profile_id, metadata_json FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()
    if row is None:
        con.close()
        raise LookupError(f"campaign {campaign_id} not found")
    meta = json.loads(row["metadata_json"] or "{}")
    req = meta.get("prepRequest") or {}
    brief = str(req.get("brief") or req.get("notes") or "").strip()
    title = str(req.get("title") or "").strip()
    if not title:
        blog = con.execute(
            "SELECT draft_json FROM campaign_posts WHERE campaign_id = ? AND platform IN ('nw_sw_blog','teaching_blog') LIMIT 1",
            (campaign_id,),
        ).fetchone()
        if blog:
            try:
                old = json.loads(blog["draft_json"] or "{}")
                title = str(old.get("title") or "").strip()
            except Exception:
                pass
    con.close()
    return int(row["profile_id"]), brief, title


def _blog_account(profile_id: int):
    for acc in profile_registry.list_accounts(profile_id=profile_id, enabled=True):
        if acc.platform in BLOG_PLATFORMS:
            return acc
    return None


def _transcript(campaign_id: int) -> str:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT metadata_json FROM campaign_artifacts WHERE campaign_id = ? AND artifact_kind = 'transcript'",
        (campaign_id,),
    ).fetchall()
    con.close()
    for r in rows:
        try:
            meta = json.loads(r["metadata_json"] or "{}")
            if meta.get("text"):
                return str(meta["text"])
        except Exception:
            continue
    return ""


def regenerate(profile, account, brief, title, transcript) -> dict:
    media_group = _preview_media_group_stub(name=(title or brief or "post")[:50])
    media_context = _build_preview_media_context(brief)
    if transcript:
        media_context["transcriptText"] = transcript
    request_data = {
        "brief": brief,
        "title": title,
        "notes": brief,
        "useLlm": True,
        "hashtags": [],
        "firstComment": "",
        "contactDetails": "",
        "cta": "",
    }
    return _generate_account_draft(account, profile, media_group, request_data, media_context)


def main() -> int:
    apply = "--apply" in sys.argv
    limit = None
    for a in sys.argv:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
    affected = _short_blog_campaigns()
    items = sorted(affected.items())
    if limit:
        items = items[:limit]
    print(f"affected campaigns: {len(items)} (of {len(affected)})  apply={apply}")
    ok = fail = 0
    for campaign_id, post_ids in items:
        profile_id, brief, title = _campaign_meta(campaign_id)
        account = _blog_account(profile_id)
        if account is None:
            print(f"campaign {campaign_id}: no blog account for profile {profile_id} — skip")
            continue
        profile = profile_registry.get_profile(profile_id)
        transcript = _transcript(campaign_id)
        try:
            draft = regenerate(profile, account, brief, title, transcript)
            new_msg = str(draft.get("message") or "")
        except Exception as exc:
            print(f"campaign {campaign_id}: regenerate FAILED: {exc}")
            fail += 1
            continue
        if len(new_msg.strip()) < MIN_LEN:
            print(f"campaign {campaign_id}: still short ({len(new_msg.strip())} chars) — skip")
            fail += 1
            continue
        # update campaign_posts draft
        if apply:
            con = sqlite3.connect(DB)
            con.row_factory = sqlite3.Row
            for pid in post_ids:
                row = con.execute("SELECT draft_json FROM campaign_posts WHERE id=?", (pid,)).fetchone()
                old = json.loads(row["draft_json"] or "{}")
                old.update({k: v for k, v in draft.items() if v})
                con.execute("UPDATE campaign_posts SET draft_json=? WHERE id=?",
                            (json.dumps(old, ensure_ascii=False), pid))
            con.commit()
            con.close()
        # re-publish the blog MDX
        if apply:
            # Preserve the original title/slug so the re-publish overwrites the
            # existing (empty) MDX file instead of creating a new slug.
            final_draft = dict(draft)
            if title:
                final_draft["title"] = title
            payload = {
                "platform": account.platform,
                "campaignId": campaign_id,
                "message": new_msg,
                "draft": final_draft,
                "artifacts": [],
                "sheetRow": {},
            }
            try:
                prepared_publishers.publish_nw_sw_blog_sync(account, payload)
                print(f"campaign {campaign_id}: republished ({len(new_msg.strip())} chars)")
            except Exception as exc:
                print(f"campaign {campaign_id}: republish FAILED: {exc}")
                fail += 1
                continue
        else:
            print(f"campaign {campaign_id}: would regenerate ({len(new_msg.strip())} chars) "
                  f"brief={brief[:40]!r}")
        ok += 1
    print(f"done ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
