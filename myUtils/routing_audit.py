"""Audit language routing across the queue: is any content going to the wrong audience?

The rule (``docs/gdrive-filename-convention.md`` §8): Chinese content goes only to
Chinese-audience accounts; English content goes to English-audience accounts and
to Chinese ones **only** when the caller explicitly opts in with
``options.includeZhForEnglish``.

Three ways it has been broken, all invisible until the operator looked at their
timeline:

  * submissions that passed ``includeZhForEnglish: true`` — the copy was
    localised to Chinese, so the draft *looked* right while the video was
    English (83 queued targets, 8 already published);
  * STT **refusal** text stored as a transcript ("The request was rejected
    because it was considered high risk"), which then decided the content
    language — in both directions;
  * and, worst, **no transcript at all**. The hosted STT gateway died on
    2026-10-27 and the language fell back to the campaign title, which draft
    generation writes *in the target account's language*. The comparison became
    a tautology, the audit printed "0 violations" every run, and ~1 900 English
    targets sat aimed at the Chinese accounts.

The fix is that the language is now decided once, by
:mod:`myUtils.content_language`, ranked by how much each piece of evidence
actually says about the *media* (the audio first), stored on the campaign, and
re-checked at publish time. This module reads that stored value; a campaign whose
language is still genuinely unknown is reported by
:func:`unknown_language_targets` rather than silently passing.

Used by the audit CLI (``scripts/audit_language_routing.py``) and by
``myUtils.pipeline_monitor``, so a future misroute is reported instead of
published.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from myUtils import content_language as content_language_module
from myUtils import llm_client

_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _campaign_evidence(conn: sqlite3.Connection, campaign_id: int) -> dict:
    """Everything known about a campaign's content, in one query round."""
    metadata: dict = {}
    row = conn.execute(
        "SELECT metadata_json FROM campaigns WHERE id=?", (campaign_id,)
    ).fetchone()
    if row:
        try:
            metadata = json.loads(row[0] or "{}")
        except ValueError:
            metadata = {}
    filenames = [
        str(item[0] or "")
        for item in conn.execute(
            """
            SELECT fr.filename FROM media_group_items mgi
            JOIN file_records fr ON fr.id = mgi.file_record_id
            JOIN campaigns c ON c.media_group_id = mgi.media_group_id
            WHERE c.id = ?
            """,
            (campaign_id,),
        )
    ]
    transcript = ""
    for artifact in conn.execute(
        "SELECT local_path, metadata_json FROM campaign_artifacts "
        "WHERE campaign_id=? AND artifact_kind='transcript' ORDER BY id DESC",
        (campaign_id,),
    ):
        try:
            transcript = Path(str(artifact[0] or "")).read_text(encoding="utf-8", errors="replace")
        except OSError:
            try:
                transcript = json.loads(artifact[1] or "{}").get("text") or ""
            except ValueError:
                transcript = ""
        if llm_client.usable_transcript(transcript).strip():
            break
    return {"metadata": metadata, "filenames": filenames, "transcript": transcript}


def content_language(
    conn: sqlite3.Connection, campaign_id: int, title: str = "", *, evidence: dict | None = None
) -> tuple[str | None, str]:
    """``(language, source)`` for a campaign.

    Order: the language stored on the campaign (the audio's verdict, persisted at
    prep time), then a real transcript, then the media's own name/brief. Returns
    ``(None, "unknown")`` when nothing actually says — a language-neutral
    filename must not be guessed at, in either direction.
    """
    evidence = evidence or _campaign_evidence(conn, campaign_id)
    stored_language, stored_source = content_language_module.from_metadata(evidence["metadata"])
    if stored_language:
        return stored_language, stored_source or "stored"
    transcript = llm_client.usable_transcript(evidence["transcript"])
    if transcript.strip():
        detected = llm_client.detect_language(transcript)
        if detected in {"zh", "en"}:
            return detected, "transcript"
    brief = str((evidence["metadata"].get("prepRequest") or {}).get("brief") or "")
    detected, source = content_language_module.detect_from_evidence(
        filenames=evidence["filenames"], brief=brief, title=title,
        has_video="video" in media_kinds(conn, campaign_id),
    )
    if detected:
        return detected, source or "filename"
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


def _pending_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
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


def _titles(conn: sqlite3.Connection) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in conn.execute("SELECT id, metadata_json FROM campaigns"):
        try:
            out[row[0]] = json.loads(row[1] or "{}").get("title") or ""
        except ValueError:
            out[row[0]] = ""
    return out


def pending_violations(conn: sqlite3.Connection) -> list[dict]:
    """Every pending target whose content language the account does not accept."""
    titles = _titles(conn)
    cache: dict[int, tuple[str | None, str]] = {}
    evidence_cache: dict[int, dict] = {}
    violations: list[dict] = []
    for row in _pending_rows(conn):
        campaign_id = int(row["campaign_id"])
        if campaign_id not in cache:
            evidence_cache[campaign_id] = _campaign_evidence(conn, campaign_id)
            cache[campaign_id] = content_language(
                conn, campaign_id, titles.get(campaign_id, ""),
                evidence=evidence_cache[campaign_id],
            )
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


def unknown_language_targets(conn: sqlite3.Connection) -> list[dict]:
    """Pending targets whose content language nothing could establish.

    These are *not* violations — the pipeline cannot know, and refusing to
    publish on no evidence would stop it. They are reported because they are
    where a misroute hides: while STT was down this list was the entire queue,
    and treating it as "fine" is what let English media reach Chinese accounts
    for days.
    """
    titles = _titles(conn)
    cache: dict[int, tuple[str | None, str]] = {}
    out: list[dict] = []
    for row in _pending_rows(conn):
        campaign_id = int(row["campaign_id"])
        if campaign_id not in cache:
            cache[campaign_id] = content_language(conn, campaign_id, titles.get(campaign_id, ""))
        language, _source = cache[campaign_id]
        if language is not None:
            continue
        out.append({
            "target_id": row["target_id"],
            "campaign_id": campaign_id,
            "account_name": row["account_name"],
            "platform": row["platform"],
            "schedule_at": row["schedule_at"],
        })
    return out
