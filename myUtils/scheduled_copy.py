from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    from opencc import OpenCC
except ImportError:  # pragma: no cover
    OpenCC = None


def _pending_generated_posts(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT DISTINCT p.id, p.draft_json, j.payload_json, j.id
        FROM campaign_posts p
        JOIN publish_job_targets t ON t.file_ref = printf('campaign_post:%d', p.id)
        JOIN publish_jobs j ON j.id = t.job_id
        WHERE t.schedule_at IS NOT NULL AND t.status IN ('pending', 'retrying')
        """
    ).fetchall()


def preview_scheduled_copy(db_path: Path) -> dict:
    """Preview OpenCC changes to active scheduled campaign drafts."""
    if OpenCC is None:
        raise RuntimeError("Install opencc-python-reimplemented to convert queued Chinese copy")
    converter = OpenCC("s2t")
    grouped = {}
    with sqlite3.connect(db_path) as conn:
        rows = _pending_generated_posts(conn)
    for post_id, raw, payload_raw, job_id in rows:
        draft = json.loads(raw or "{}")
        payload = json.loads(payload_raw or "{}")
        if not isinstance(draft, dict):
            continue
        if isinstance(payload.get("draft"), dict) and payload["draft"] != draft:
            continue
        payload_draft = payload.get("draft")
        if not isinstance(payload_draft, dict):
            continue
        # Pending targets must publish the exact converted copy shown in the
        # calendar; do not convert when the legacy top-level message diverges.
        if payload.get("message") and payload["message"] != draft.get("message"):
            continue
        source = json.dumps(draft, ensure_ascii=False)
        converted = converter.convert(source)
        if converted != source:
            item = grouped.setdefault(int(post_id), {"postId": int(post_id), "before": draft, "after": json.loads(converted), "jobIds": []})
            if int(job_id) not in item["jobIds"]:
                item["jobIds"].append(int(job_id))
    items = list(grouped.values())
    return {"eligible": len(rows), "changed": len(items), "items": items}


def apply_scheduled_copy(db_path: Path, *, confirm: bool = False) -> dict:
    """Apply reviewed conversions only to scheduled copy still matching its queued draft."""
    if not confirm:
        raise ValueError("confirm=True is required to mutate scheduled copy")
    preview = preview_scheduled_copy(db_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS scheduled_copy_conversion_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            job_ids_json TEXT NOT NULL,
            original_draft_json TEXT NOT NULL,
            converted_draft_json TEXT NOT NULL,
            converted_at TEXT NOT NULL
        )""")
        for item in preview["items"]:
            post_id = item["postId"]
            current = conn.execute("SELECT draft_json FROM campaign_posts WHERE id = ?", (post_id,)).fetchone()
            if not current or json.loads(current[0] or "{}") != item["before"]:
                raise RuntimeError(f"Draft changed for post {post_id}; transaction aborted")
            active = conn.execute("""SELECT DISTINCT j.id, j.payload_json
                FROM publish_job_targets t JOIN publish_jobs j ON j.id=t.job_id
                WHERE t.file_ref=? AND t.schedule_at IS NOT NULL
                  AND t.status IN ('pending','retrying')""", (f"campaign_post:{post_id}",)).fetchall()
            if {row[0] for row in active} != set(item["jobIds"]):
                raise RuntimeError(f"Scheduled targets changed for post {post_id}; transaction aborted")
            draft_json = json.dumps(item["after"], ensure_ascii=False)
            conn.execute("UPDATE campaign_posts SET draft_json=?,updated_at=? WHERE id=?", (draft_json, now, post_id))
            conn.execute("INSERT INTO scheduled_copy_conversion_audit(post_id,job_ids_json,original_draft_json,converted_draft_json,converted_at) VALUES(?,?,?,?,?)", (post_id, json.dumps(item["jobIds"]), json.dumps(item["before"], ensure_ascii=False), draft_json, now))
            for job_id, raw_payload in active:
                payload = json.loads(raw_payload or "{}")
                if isinstance(payload.get("draft"), dict): payload["draft"] = item["after"]
                payload["message"] = item["after"].get("message", payload.get("message", ""))
                conn.execute("UPDATE publish_jobs SET payload_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), job_id))
            conn.execute(
                "UPDATE scheduled_copy_conversion_audit SET converted_draft_json=? WHERE post_id=? AND converted_at=?",
                (draft_json, post_id, now),
            )
        conn.commit()
    return {"eligible": preview["eligible"], "changed": preview["changed"], "appliedAt": now}
