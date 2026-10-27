"""Audit language routing across the queue: is any content going to the wrong audience?

The rule (``docs/gdrive-filename-convention.md`` §8): Chinese content goes only to
Chinese-audience accounts; English content goes to English-audience accounts and
to Chinese ones **only** when the caller explicitly opts in with
``options.includeZhForEnglish``.

Two ways it was broken in October 2026, both invisible until the operator looked
at their timeline:

  * submissions that passed ``includeZhForEnglish: true`` — the copy was
    localised to Chinese, so the draft *looked* right while the video was
    English (83 queued targets, 8 already published);
  * STT **refusal** text stored as a transcript ("The request was rejected
    because it was considered high risk", "这段音频纯为器乐演奏…"), which then
    decided the content language — in both directions.

This module is the single implementation used by the audit CLI
(`scripts/audit_language_routing.py`) and by `myUtils.pipeline_monitor`, so a
future misroute is reported instead of published.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from myUtils import llm_client

_CJK = re.compile(r"[\u3400-\u9fff]")
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _has_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def content_language(
    conn: sqlite3.Connection, campaign_id: int, title: str = ""
) -> tuple[str | None, str]:
    """``(language, source)`` for a campaign: real transcript first, else the title.

    Returns ``(None, "unknown")`` when neither exists — an unknown language is
    never filtered by the router, so it is not a violation.
    """
    for row in conn.execute(
        "SELECT local_path, metadata_json FROM campaign_artifacts "
        "WHERE campaign_id=? AND artifact_kind='transcript' ORDER BY id DESC",
        (campaign_id,),
    ):
        text = ""
        try:
            text = Path(str(row["local_path"] or "")).read_text(encoding="utf-8", errors="replace")
        except OSError:
            try:
                text = json.loads(row["metadata_json"] or "{}").get("text") or ""
            except ValueError:
                text = ""
        text = llm_client.usable_transcript(text)
        if text.strip():
            return ("zh" if _has_cjk(text) else "en"), "transcript"
    title = str(title or "").strip()
    if title:
        return ("zh" if _has_cjk(title) else "en"), "title"
    return None, "unknown"


def media_kinds(conn: sqlite3.Connection, campaign_id: int) -> set[str]:
    kinds: set[str] = set()
    for row in conn.execute(
        """
        SELECT fr.filename FROM media_group_items mgi
        JOIN file_records fr ON fr.id = mgi.file_record_id
        JOIN campaigns c ON c.media_group_id = mgi.media_group_id
        WHERE c.id = ?
        """,
        (campaign_id,),
    ):
        suffix = Path(str(row["filename"] or "")).suffix.lower()
        kinds.add(
            "video" if suffix in _VIDEO_SUFFIXES
            else "image" if suffix in _IMAGE_SUFFIXES
            else "other"
        )
    return kinds


def _account_tokens(raw: str | None) -> set[str]:
    return {
        token for token in str(raw or "").lower().replace(",", " ").replace("+", " ").split()
        if token
    }


def accepts(content_language_value: str, tokens: set[str]) -> bool:
    """Whether an account with these audience-language tokens may receive the content."""
    if not tokens:
        return True  # unclassified account: never filtered
    if content_language_value == "zh":
        return any(token.startswith("zh") for token in tokens)
    if content_language_value == "en":
        return any(token.startswith("en") for token in tokens)
    return True


def pending_violations(conn: sqlite3.Connection) -> list[dict]:
    """Every pending target whose content language the account does not accept."""
    conn.row_factory = sqlite3.Row
    titles = {
        row["id"]: (json.loads(row["metadata_json"] or "{}").get("title") or "")
        for row in conn.execute("SELECT id, metadata_json FROM campaigns")
    }
    rows = conn.execute(
        """
        SELECT jt.id AS target_id, jt.schedule_at, cp.campaign_id,
               a.account_name, a.platform,
               json_extract(a.config_json, '$.audience_language') AS audience_language
        FROM publish_job_targets jt
        JOIN campaign_posts cp ON cp.id = CAST(substr(jt.file_ref, 15) AS INTEGER)
        JOIN accounts a ON 'account:' || a.id = jt.account_ref
        WHERE jt.status = 'pending'
        ORDER BY jt.schedule_at
        """
    ).fetchall()
    cache: dict[int, tuple[str | None, str]] = {}
    violations: list[dict] = []
    for row in rows:
        campaign_id = int(row["campaign_id"])
        if campaign_id not in cache:
            cache[campaign_id] = content_language(conn, campaign_id, titles.get(campaign_id, ""))
        language, source = cache[campaign_id]
        if language not in {"zh", "en"}:
            continue
        tokens = _account_tokens(row["audience_language"])
        if accepts(language, tokens):
            continue
        kinds = media_kinds(conn, campaign_id)
        violations.append({
            "target_id": row["target_id"],
            "campaign_id": campaign_id,
            "account_name": row["account_name"],
            "platform": row["platform"],
            "schedule_at": row["schedule_at"],
            "content_language": language,
            "source": source,
            "media_kind": "video" if "video" in kinds else "image" if "image" in kinds else "other",
            "title": titles.get(campaign_id, ""),
        })
    return violations
