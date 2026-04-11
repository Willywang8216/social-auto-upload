import json
import sqlite3
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from utils.account_registry import serialize_account_row
from utils.direct_publishers import get_direct_publisher_target, publish_job_to_direct_target, refresh_direct_publish_job_status
from utils.profile_pipeline import (
    apply_intro_outro_if_needed,
    apply_watermark_if_needed,
    append_rows_to_google_sheet,
    build_media_packaging_preview,
    build_google_sheet_rows,
    delete_rows_from_google_sheet,
    generate_profile_batch_content,
    generate_post_for_content_account,
    generate_posts,
    get_material_record,
    get_profile,
    infer_media_kind,
    resolve_effective_intro_outro_settings,
    resolve_effective_watermark_settings,
    upload_media,
)

DIRECT_UPLOAD_PLATFORMS = {"xiaohongshu", "channels", "douyin", "kuaishou", "twitter", "telegram", "reddit", "discord", "bluesky", "line_oa"}
MANAGED_DIRECT_UPLOAD_PLATFORMS = {"facebook", "threads", "youtube", "tiktok"}
SHEET_EXPORT_PLATFORMS = {"threads", "instagram", "facebook", "youtube", "tiktok"}
MANUAL_ONLY_PLATFORMS = {"patreon"}
ACTIVE_JOB_STATUSES = {"queued", "scheduled"}
TERMINAL_JOB_STATUSES = {"published", "exported", "manual_done", "failed", "cancelled"}
SCHEDULER_MIRROR_PLATFORMS = {"twitter"}
ASYNC_DIRECT_STATUS_PLATFORMS = {"threads", "youtube", "tiktok"}


def ensure_publish_job_tables(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                profile_id INTEGER,
                profile_name TEXT NOT NULL DEFAULT '',
                target_kind TEXT NOT NULL,
                account_id INTEGER,
                content_account_id TEXT DEFAULT '',
                platform_key TEXT NOT NULL,
                target_name TEXT NOT NULL DEFAULT '',
                delivery_mode TEXT NOT NULL,
                material_id INTEGER,
                material_name TEXT NOT NULL DEFAULT '',
                media_path TEXT NOT NULL DEFAULT '',
                media_public_url TEXT DEFAULT '',
                title TEXT DEFAULT '',
                message TEXT DEFAULT '',
                hashtags_json TEXT DEFAULT '[]',
                metadata_json TEXT DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'draft',
                scheduled_at DATETIME,
                published_at DATETIME,
                last_error TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_job_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                revision_no INTEGER NOT NULL,
                source TEXT NOT NULL,
                instruction_text TEXT DEFAULT '',
                title TEXT DEFAULT '',
                message TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(job_id, revision_no),
                FOREIGN KEY(job_id) REFERENCES publish_jobs(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()


def generate_publish_batch_drafts(db_path: Path, base_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_publish_job_tables(db_path)
    profile_selections = _normalize_profile_selections(payload)
    if not profile_selections:
        raise ValueError("profileIds or profileSelections is required")

    items = []
    for selection in profile_selections:
        selection_payload = {
            "profileId": selection["profileId"],
            "materialIds": selection["materialIds"],
            "selectedAccountIds": selection["selectedAccountIds"],
            "selectedContentAccountIds": selection["selectedContentAccountIds"],
            "link": selection["link"],
            "writeToSheet": False,
        }
        batch_result = generate_profile_batch_content(db_path, base_dir, selection_payload)
        items.extend(_build_draft_items(db_path, batch_result))

    return {
        "batchId": uuid.uuid4().hex,
        "items": items,
        "summary": {
            "profiles": len({item["profileId"] for item in items if item.get("profileId")}),
            "items": len(items),
            "deliveryModes": _count_by_key(items, "deliveryMode"),
            "platforms": _count_by_key(items, "platformKey"),
        },
    }

def save_publish_jobs(
    db_path: Path,
    base_dir: Path | dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if payload is None:
        payload = base_dir if isinstance(base_dir, dict) else {}
        base_dir = Path(".")
    ensure_publish_job_tables(db_path)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items is required")

    scheduling = _normalize_scheduling(payload)
    batch_id = str(payload.get("batchId") or "").strip() or uuid.uuid4().hex
    saved_ids = []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        prepared_media_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in items:
            normalized_item = _normalize_job_item(item)
            normalized_item = _prepare_job_media_assets(db_path, base_dir, normalized_item, prepared_media_cache)
            for schedule_at in _expand_schedule_values(scheduling):
                status = _resolve_initial_status(scheduling["mode"], schedule_at)
                metadata = dict(normalized_item["metadata"])
                metadata["savedSchedulingMode"] = scheduling["mode"]
                metadata["savedScheduleAt"] = schedule_at or ""
                cursor.execute(
                    """
                    INSERT INTO publish_jobs (
                        batch_id,
                        profile_id,
                        profile_name,
                        target_kind,
                        account_id,
                        content_account_id,
                        platform_key,
                        target_name,
                        delivery_mode,
                        material_id,
                        material_name,
                        media_path,
                        media_public_url,
                        title,
                        message,
                        hashtags_json,
                        metadata_json,
                        status,
                        scheduled_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        batch_id,
                        normalized_item["profileId"],
                        normalized_item["profileName"],
                        normalized_item["targetKind"],
                        normalized_item["accountId"],
                        normalized_item["contentAccountId"],
                        normalized_item["platformKey"],
                        normalized_item["targetName"],
                        normalized_item["deliveryMode"],
                        normalized_item["materialId"],
                        normalized_item["materialName"],
                        normalized_item["mediaPath"],
                        normalized_item["mediaPublicUrl"],
                        normalized_item["title"],
                        normalized_item["message"],
                        json.dumps(normalized_item["hashtags"], ensure_ascii=False),
                        json.dumps(metadata, ensure_ascii=False),
                        status,
                        schedule_at,
                    ),
                )
                job_id = int(cursor.lastrowid)
                _insert_revision(
                    cursor,
                    job_id,
                    "generated",
                    normalized_item["title"],
                    normalized_item["message"],
                    metadata,
                    "",
                )
                saved_ids.append(job_id)
        conn.commit()

    for job_id in saved_ids:
        _sync_scheduler_sheet_copy_for_job(db_path, job_id)

    return {
        "batchId": batch_id,
        "jobIds": saved_ids,
        "count": len(saved_ids),
    }


def list_publish_jobs(db_path: Path, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    ensure_publish_job_tables(db_path)
    filters = filters or {}
    clauses = []
    params = []

    if filters.get("status"):
        clauses.append("j.status = ?")
        params.append(str(filters["status"]).strip())
    if filters.get("deliveryMode"):
        clauses.append("j.delivery_mode = ?")
        params.append(str(filters["deliveryMode"]).strip())
    if filters.get("platformKey"):
        clauses.append("j.platform_key = ?")
        params.append(str(filters["platformKey"]).strip())
    if filters.get("batchId"):
        clauses.append("j.batch_id = ?")
        params.append(str(filters["batchId"]).strip())
    if filters.get("jobId"):
        clauses.append("j.id = ?")
        params.append(int(filters["jobId"]))
    if filters.get("profileId"):
        clauses.append("j.profile_id = ?")
        params.append(int(filters["profileId"]))
    if filters.get("dateFrom"):
        clauses.append("COALESCE(j.scheduled_at, j.created_at) >= ?")
        params.append(str(filters["dateFrom"]).strip())
    if filters.get("dateTo"):
        clauses.append("COALESCE(j.scheduled_at, j.created_at) <= ?")
        params.append(str(filters["dateTo"]).strip())

    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT j.*,
               COALESCE(MAX(r.revision_no), 0) AS revision_count
        FROM publish_jobs j
        LEFT JOIN publish_job_revisions r ON r.job_id = j.id
        {where_clause}
        GROUP BY j.id
        ORDER BY COALESCE(j.scheduled_at, j.created_at) ASC, j.id ASC
    """

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
    return [_serialize_job_row(row) for row in rows]


def get_publish_calendar_entries(
    db_path: Path,
    start_date: str,
    end_date: str,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    filters = dict(filters or {})
    filters["dateFrom"] = start_date
    filters["dateTo"] = end_date
    jobs = list_publish_jobs(db_path, filters)
    grouped = {}
    for job in jobs:
        event_date = _job_event_date(job)
        grouped.setdefault(event_date, [])
        grouped[event_date].append(job)
    return {
        "items": [
            {
                "date": date_key,
                "count": len(day_jobs),
                "jobs": day_jobs,
            }
            for date_key, day_jobs in sorted(grouped.items())
        ]
    }


def update_publish_job_content(db_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_publish_job_tables(db_path)
    job_id = int(payload.get("jobId") or 0)
    if not job_id:
        raise ValueError("jobId is required")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError("publish job not found")

        existing_metadata = _parse_json_object(row["metadata_json"])
        merged_metadata = dict(existing_metadata)
        merged_metadata.update(payload.get("metadata") or {})

        title = str(payload.get("title", row["title"]) or "").strip()
        message = str(payload.get("message", row["message"]) or "").strip()
        source = str(payload.get("source") or "edited").strip() or "edited"
        instruction_text = str(payload.get("instructionText") or "").strip()
        scheduled_at = payload.get("scheduledAt")
        status = str(payload.get("status") or row["status"] or "").strip() or row["status"]
        if scheduled_at is not None and str(scheduled_at).strip():
            scheduled_at = _parse_iso_datetime(str(scheduled_at).strip()).isoformat(timespec="seconds")
            if payload.get("status") is None:
                status = "scheduled"
        elif scheduled_at is not None:
            scheduled_at = None

        cursor.execute(
            """
            UPDATE publish_jobs
            SET title = ?,
                message = ?,
                status = ?,
                scheduled_at = ?,
                metadata_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                title,
                message,
                status,
                scheduled_at if scheduled_at is not None else row["scheduled_at"],
                json.dumps(merged_metadata, ensure_ascii=False),
                job_id,
            ),
        )
        _insert_revision(cursor, job_id, source, title, message, merged_metadata, instruction_text)
        conn.commit()
        cursor.execute(
            """
            SELECT j.*,
                   COALESCE(MAX(r.revision_no), 0) AS revision_count
            FROM publish_jobs j
            LEFT JOIN publish_job_revisions r ON r.job_id = j.id
            WHERE j.id = ?
            GROUP BY j.id
            """,
            (job_id,),
        )
        updated_row = cursor.fetchone()
    return _serialize_job_row(updated_row)


def regenerate_publish_job_content(db_path: Path, base_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_publish_job_tables(db_path)
    job_id = int(payload.get("jobId") or 0)
    if not job_id:
        return _regenerate_draft_payload(db_path, base_dir, payload)
    if not job_id:
        raise ValueError("jobId is required")

    instruction_text = str(payload.get("instructionText") or "").strip()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
    if not row:
        raise ValueError("publish job not found")

    job = _serialize_job_row(row)
    profile = get_profile(db_path, int(job["profileId"]))
    material = get_material_record(db_path, int(job["materialId"])) if job.get("materialId") else None
    if not material:
        raise ValueError("material not found")

    runtime_profile = deepcopy(profile)
    if instruction_text:
        runtime_profile["systemPrompt"] = _merge_instruction(runtime_profile.get("systemPrompt"), instruction_text)

    media_url = job["mediaPublicUrl"]
    transcript = str((job.get("metadata") or {}).get("transcript") or "").strip()

    if job["targetKind"] == "content_account":
        content_account = _find_content_account(profile, job["contentAccountId"])
        if instruction_text:
            content_account = dict(content_account)
            content_account["prompt"] = _merge_instruction(content_account.get("prompt"), instruction_text)
        new_message = generate_post_for_content_account(runtime_profile, content_account, transcript, material, media_url)
    else:
        generated_posts = generate_posts(runtime_profile, transcript, material, media_url)
        new_message = str(generated_posts.get(job["platformKey"]) or _pick_primary_message(generated_posts) or "").strip()

    if not new_message:
        raise RuntimeError("regeneration did not produce any content")

    return update_publish_job_content(
        db_path,
        {
            "jobId": job_id,
            "title": _build_job_title(material, new_message),
            "message": new_message,
            "metadata": {
                "transcript": transcript,
                "lastRegeneratedAt": datetime.now().isoformat(timespec="seconds"),
            },
            "source": "regenerated",
            "instructionText": instruction_text,
        },
    )


def _regenerate_draft_payload(db_path: Path, base_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    draft = payload.get("draft")
    if not isinstance(draft, dict):
        raise ValueError("jobId or draft is required")

    instruction_text = str(payload.get("instructionText") or "").strip()
    draft_item = _normalize_job_item(draft)
    if not draft_item["profileId"]:
        raise ValueError("draft profileId is required")
    if not draft_item["materialId"]:
        raise ValueError("draft materialId is required")

    profile = get_profile(db_path, int(draft_item["profileId"]))
    material = get_material_record(db_path, int(draft_item["materialId"]))
    if not material:
        raise ValueError("material not found")

    runtime_profile = deepcopy(profile)
    if instruction_text:
        runtime_profile["systemPrompt"] = _merge_instruction(runtime_profile.get("systemPrompt"), instruction_text)

    transcript = str((draft_item.get("metadata") or {}).get("transcript") or "").strip()
    media_url = draft_item["mediaPublicUrl"]

    if draft_item["targetKind"] == "content_account":
        content_account = _find_content_account(profile, draft_item["contentAccountId"])
        if instruction_text:
            content_account = dict(content_account)
            content_account["prompt"] = _merge_instruction(content_account.get("prompt"), instruction_text)
        new_message = generate_post_for_content_account(runtime_profile, content_account, transcript, material, media_url)
    else:
        generated_posts = generate_posts(runtime_profile, transcript, material, media_url)
        new_message = str(generated_posts.get(draft_item["platformKey"]) or _pick_primary_message(generated_posts) or "").strip()

    if not new_message:
        raise RuntimeError("regeneration did not produce any content")

    metadata = dict(draft_item["metadata"])
    metadata["lastRegeneratedAt"] = datetime.now().isoformat(timespec="seconds")
    return {
        **draft_item,
        "title": _build_job_title(material, new_message),
        "message": new_message,
        "metadata": metadata,
        "instructionText": instruction_text,
    }


def cancel_publish_job(db_path: Path, job_id: int) -> dict[str, Any]:
    return _update_job_status(db_path, job_id, "cancelled")


def complete_manual_publish_job(db_path: Path, job_id: int) -> dict[str, Any]:
    return _update_job_status(db_path, job_id, "manual_done")


def run_publish_job_now(db_path: Path, base_dir: Path, job_id: int) -> dict[str, Any]:
    ensure_publish_job_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError("publish job not found")
        cursor.execute(
            """
            UPDATE publish_jobs
            SET status = 'queued',
                scheduled_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (job_id,),
        )
        conn.commit()
    results = execute_due_publish_jobs(db_path, base_dir, limit=1, job_ids=[job_id])
    if results["processed"] == 0:
        raise RuntimeError("publish job was not executed")
    return results


def sync_publish_job_statuses(
    db_path: Path,
    base_dir: Path,
    limit: int | None = None,
    job_ids: list[int] | None = None,
) -> dict[str, Any]:
    ensure_publish_job_tables(db_path)
    synced = []
    for job in _list_jobs_for_status_sync(db_path, job_ids=job_ids):
        try:
            target = _resolve_direct_upload_target(db_path, job, base_dir)
            status_result = refresh_direct_publish_job_status(base_dir, job, target)
            applied_status = _apply_publish_status_result(db_path, job, status_result)
            synced.append({
                "id": job["id"],
                "status": applied_status,
                "platform": job["platformKey"],
            })
        except Exception as exc:
            _record_status_sync_error(db_path, job["id"], str(exc))
            synced.append({
                "id": job["id"],
                "status": job["status"],
                "platform": job["platformKey"],
                "error": str(exc),
            })
        if limit and len(synced) >= limit:
            break
    return {
        "processed": len(synced),
        "items": synced,
    }


def execute_due_publish_jobs(
    db_path: Path,
    base_dir: Path,
    limit: int | None = None,
    job_ids: list[int] | None = None,
) -> dict[str, Any]:
    ensure_publish_job_tables(db_path)
    job_ids = [int(job_id) for job_id in (job_ids or []) if job_id]
    processed = []

    while True:
        claimed = _claim_next_due_job(db_path, job_ids=job_ids)
        if not claimed:
            break
        job_ids = None
        try:
            if claimed["deliveryMode"] == "direct_upload":
                receipt = _execute_direct_upload_job(db_path, claimed, base_dir)
                final_status = _apply_direct_publish_receipt(db_path, claimed, receipt)
            elif claimed["deliveryMode"] == "sheet_export":
                _execute_sheet_export_job(db_path, claimed)
                final_status = "exported"
            else:
                _set_job_failure(db_path, claimed["id"], "manual_only jobs require manual completion")
                processed.append({"id": claimed["id"], "status": "failed"})
                if limit and len(processed) >= limit:
                    break
                continue

            if final_status == "processing":
                _set_job_processing(db_path, claimed["id"])
            else:
                _set_job_success(db_path, claimed["id"], final_status)
            processed.append({"id": claimed["id"], "status": final_status})
        except Exception as exc:
            _set_job_failure(db_path, claimed["id"], str(exc))
            processed.append({"id": claimed["id"], "status": "failed", "error": str(exc)})

        if limit and len(processed) >= limit:
            break

    return {
        "processed": len(processed),
        "items": processed,
    }


def _normalize_profile_selections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    material_ids = _normalize_int_list(payload.get("materialIds"))
    raw_selections = payload.get("profileSelections")

    if isinstance(raw_selections, list) and raw_selections:
        selections = []
        for item in raw_selections:
            if not isinstance(item, dict):
                continue
            profile_id = int(item.get("profileId") or 0)
            if not profile_id:
                continue
            item_material_ids = _normalize_int_list(item.get("materialIds")) or material_ids
            if not item_material_ids:
                continue
            selections.append({
                "profileId": profile_id,
                "materialIds": item_material_ids,
                "selectedAccountIds": _normalize_int_list(item.get("selectedAccountIds")),
                "selectedContentAccountIds": _normalize_string_list(item.get("selectedContentAccountIds")),
                "link": str(item.get("link") or payload.get("link") or "").strip(),
            })
        return selections

    profile_ids = _normalize_int_list(payload.get("profileIds"))
    single_profile_id = int(payload.get("profileId") or 0)
    if single_profile_id:
        profile_ids.append(single_profile_id)

    if not material_ids:
        return []

    selections = []
    for profile_id in sorted(set(profile_ids)):
        selections.append({
            "profileId": profile_id,
            "materialIds": material_ids,
            "selectedAccountIds": _normalize_int_list(payload.get("selectedAccountIds")),
            "selectedContentAccountIds": _normalize_string_list(payload.get("selectedContentAccountIds")),
            "link": str(payload.get("link") or "").strip(),
        })
    return selections


def _build_draft_items(db_path: Path, batch_result: dict[str, Any]) -> list[dict[str, Any]]:
    profile = batch_result.get("profile") or {}
    selected_account_ids = _normalize_int_list(batch_result.get("selectedAccountIds"))
    selected_accounts = _get_accounts_by_ids(db_path, selected_account_ids)
    items = []

    for result in batch_result.get("results") or []:
        material = result.get("material") or {}
        processed_media_path = str(result.get("processedMediaPath") or "").strip()
        storage = result.get("storage") or {}
        posts = result.get("posts") or {}
        content_account_results = result.get("contentAccountResults") or []
        transcript = str(result.get("transcript") or "").strip()
        fallback_message = _pick_primary_message(posts)

        for account in selected_accounts:
            platform_key = account["platformKey"]
            linked_content_result = _find_linked_content_account_result(content_account_results, account["id"], platform_key)
            linked_account = (linked_content_result or {}).get("account") or {}
            linked_message = str((linked_content_result or {}).get("content") or "").strip()
            media_kind = storage.get("mediaKind") or infer_media_kind(Path(processed_media_path)) if processed_media_path else "file"
            items.append({
                "profileId": profile.get("id"),
                "profileName": profile.get("name") or "",
                "targetKind": "managed_account",
                "accountId": account["id"],
                "contentAccountId": "",
                "platformKey": platform_key,
                "targetName": account["name"],
                "deliveryMode": _resolve_draft_delivery_mode(
                    platform_key,
                    "managed_account",
                    {"accountId": account["id"], "contentAccountId": linked_account.get("id") or ""},
                ),
                "materialId": material.get("id"),
                "materialName": material.get("filename") or "",
                "mediaPath": processed_media_path,
                "mediaPublicUrl": storage.get("publicUrl") or "",
                "title": _build_job_title(material, linked_message or posts.get(platform_key) or fallback_message),
                "message": linked_message or posts.get(platform_key) or fallback_message,
                "hashtags": [],
                "metadata": {
                    "mediaKind": media_kind,
                    "transcript": transcript,
                    "contentAccountId": linked_account.get("id") or "",
                    "postPreset": linked_account.get("postPreset") or "",
                    "publisherTargetId": linked_account.get("publisherTargetId") or "",
                    "publishingAccountId": linked_account.get("publishingAccountId") or str(account["id"]),
                    "source": "linked_content_account_generation" if linked_message else "managed_account_generation",
                    "brandingPreview": build_media_packaging_preview(profile, media_kind, linked_account or None),
                },
            })

        for content_result in content_account_results:
            account = content_result.get("account") or {}
            platform_key = str(account.get("platform") or "").strip().lower()
            media_kind = storage.get("mediaKind") or infer_media_kind(Path(processed_media_path)) if processed_media_path else "file"
            items.append({
                "profileId": profile.get("id"),
                "profileName": profile.get("name") or "",
                "targetKind": "content_account",
                "accountId": None,
                "contentAccountId": account.get("id") or "",
                "platformKey": platform_key,
                "targetName": account.get("name") or "",
                "deliveryMode": _resolve_draft_delivery_mode(
                    platform_key,
                    "content_account",
                    {
                        "publishingAccountId": account.get("publishingAccountId") or "",
                        "publisherTargetId": account.get("publisherTargetId") or "",
                    },
                ),
                "materialId": material.get("id"),
                "materialName": material.get("filename") or "",
                "mediaPath": processed_media_path,
                "mediaPublicUrl": storage.get("publicUrl") or "",
                "title": _build_job_title(material, content_result.get("content") or fallback_message),
                "message": str(content_result.get("content") or "").strip(),
                "hashtags": [],
                "metadata": {
                    "mediaKind": media_kind,
                    "transcript": transcript,
                    "postPreset": account.get("postPreset") or "",
                    "publishingAccountId": account.get("publishingAccountId") or "",
                    "publisherTargetId": account.get("publisherTargetId") or "",
                    "source": "content_account_generation",
                    "brandingPreview": build_media_packaging_preview(profile, media_kind, account),
                },
            })

    return items


def _normalize_scheduling(payload: dict[str, Any]) -> dict[str, Any]:
    scheduling = payload.get("scheduling") if isinstance(payload.get("scheduling"), dict) else {}
    mode = str(scheduling.get("mode") or payload.get("mode") or "draft").strip().lower()
    start_at = str(
        scheduling.get("startAt")
        or scheduling.get("scheduledAt")
        or payload.get("startAt")
        or payload.get("scheduledAt")
        or ""
    ).strip()
    repeat_count = _coerce_int(
        scheduling.get("repeatCount") or payload.get("repeatCount") or 1,
        1,
    )
    interval_value = _coerce_int(
        scheduling.get("frequencyValue") or payload.get("frequencyValue") or 1,
        1,
    )
    interval_unit = str(
        scheduling.get("frequencyUnit") or payload.get("frequencyUnit") or "hours"
    ).strip().lower()

    if mode in {"schedule", "queue"} and not start_at:
        raise ValueError("scheduledAt or startAt is required for scheduled jobs")
    if interval_unit not in {"minutes", "hours", "days"}:
        raise ValueError("frequencyUnit must be minutes, hours, or days")

    return {
        "mode": mode,
        "startAt": start_at,
        "repeatCount": max(1, repeat_count),
        "frequencyValue": max(1, interval_value),
        "frequencyUnit": interval_unit,
    }


def _prepare_job_media_assets(
    db_path: Path,
    base_dir: Path,
    item: dict[str, Any],
    cache: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, Any]:
    profile_id = item.get("profileId")
    material_id = item.get("materialId")
    if not profile_id or not material_id:
        return item

    profile = get_profile(db_path, int(profile_id))
    material = get_material_record(db_path, int(material_id))
    if not material:
        return item

    raw_file_path = str(material.get("file_path") or "").strip()
    source_path = base_dir / raw_file_path
    if not source_path.exists():
        source_path = base_dir / "videoFile" / Path(raw_file_path).name
    if not source_path.exists():
        return item

    content_account_id = str(item.get("contentAccountId") or (item.get("metadata") or {}).get("contentAccountId") or "").strip()
    cache_key = (profile_id, material_id, content_account_id or "__profile__")
    if cache_key in cache:
        prepared = cache[cache_key]
    else:
        runtime_profile = deepcopy(profile)
        content_account = _find_content_account(profile, content_account_id) if content_account_id else None
        if content_account:
            runtime_profile.setdefault("settings", {})["introOutro"] = resolve_effective_intro_outro_settings(profile, content_account)
            runtime_profile.setdefault("settings", {})["watermark"] = resolve_effective_watermark_settings(profile, content_account)
        media_path = apply_intro_outro_if_needed(source_path, runtime_profile, base_dir)
        media_path = apply_watermark_if_needed(media_path, runtime_profile, base_dir)
        upload_result = upload_media(media_path, runtime_profile)
        media_kind = upload_result.get("mediaKind") or infer_media_kind(media_path)
        prepared = {
            "mediaPath": str(media_path),
            "mediaPublicUrl": upload_result.get("publicUrl") or "",
            "mediaKind": media_kind,
            "brandingPreview": build_media_packaging_preview(profile, media_kind, content_account),
        }
        cache[cache_key] = prepared

    normalized = dict(item)
    normalized["mediaPath"] = prepared["mediaPath"]
    normalized["mediaPublicUrl"] = prepared["mediaPublicUrl"]
    metadata = dict(normalized.get("metadata") or {})
    metadata["mediaKind"] = prepared["mediaKind"]
    metadata["brandingPreview"] = prepared["brandingPreview"]
    normalized["metadata"] = metadata
    return normalized


def _expand_schedule_values(scheduling: dict[str, Any]) -> list[str | None]:
    mode = scheduling["mode"]
    if mode == "draft":
        return [None]
    if mode == "now":
        return [None]

    start_at = _parse_iso_datetime(scheduling["startAt"])
    if mode == "schedule":
        return [start_at.isoformat(timespec="seconds")]

    delta = _build_interval_delta(scheduling["frequencyValue"], scheduling["frequencyUnit"])
    return [
        (start_at + (delta * index)).isoformat(timespec="seconds")
        for index in range(scheduling["repeatCount"])
    ]


def _resolve_initial_status(mode: str, schedule_at: str | None) -> str:
    if mode == "draft":
        return "draft"
    if schedule_at:
        return "scheduled"
    return "queued"


def _normalize_job_item(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("each item must be an object")

    platform_key = str(item.get("platformKey") or "").strip().lower()
    if not platform_key:
        raise ValueError("platformKey is required")

    return {
        "profileId": _coerce_int(item.get("profileId"), None),
        "profileName": str(item.get("profileName") or "").strip(),
        "targetKind": str(item.get("targetKind") or "").strip() or "content_account",
        "accountId": _coerce_int(item.get("accountId"), None),
        "contentAccountId": str(item.get("contentAccountId") or "").strip(),
        "platformKey": platform_key,
        "targetName": str(item.get("targetName") or "").strip(),
        "deliveryMode": str(item.get("deliveryMode") or _delivery_mode_for_platform(platform_key)).strip(),
        "materialId": _coerce_int(item.get("materialId"), None),
        "materialName": str(item.get("materialName") or "").strip(),
        "mediaPath": str(item.get("mediaPath") or "").strip(),
        "mediaPublicUrl": str(item.get("mediaPublicUrl") or "").strip(),
        "title": str(item.get("title") or "").strip(),
        "message": str(item.get("message") or "").strip(),
        "hashtags": item.get("hashtags") if isinstance(item.get("hashtags"), list) else [],
        "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
    }


def _insert_revision(
    cursor: sqlite3.Cursor,
    job_id: int,
    source: str,
    title: str,
    message: str,
    metadata: dict[str, Any],
    instruction_text: str,
) -> None:
    cursor.execute(
        "SELECT COALESCE(MAX(revision_no), 0) FROM publish_job_revisions WHERE job_id = ?",
        (job_id,),
    )
    revision_no = int(cursor.fetchone()[0]) + 1
    cursor.execute(
        """
        INSERT INTO publish_job_revisions (
            job_id,
            revision_no,
            source,
            instruction_text,
            title,
            message,
            metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            revision_no,
            source,
            instruction_text,
            title,
            message,
            json.dumps(metadata, ensure_ascii=False),
        ),
    )


def _serialize_job_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "batchId": row["batch_id"],
        "profileId": row["profile_id"],
        "profileName": row["profile_name"] or "",
        "targetKind": row["target_kind"] or "",
        "accountId": row["account_id"],
        "contentAccountId": row["content_account_id"] or "",
        "platformKey": row["platform_key"] or "",
        "targetName": row["target_name"] or "",
        "deliveryMode": row["delivery_mode"] or "",
        "materialId": row["material_id"],
        "materialName": row["material_name"] or "",
        "mediaPath": row["media_path"] or "",
        "mediaPublicUrl": row["media_public_url"] or "",
        "title": row["title"] or "",
        "message": row["message"] or "",
        "hashtags": _parse_json_list(row["hashtags_json"]),
        "metadata": _parse_json_object(row["metadata_json"]),
        "status": row["status"] or "",
        "scheduledAt": row["scheduled_at"] or "",
        "publishedAt": row["published_at"] or "",
        "lastError": row["last_error"] or "",
        "revisionCount": int(row["revision_count"]) if "revision_count" in row.keys() else 0,
        "createdAt": row["created_at"] or "",
        "updatedAt": row["updated_at"] or "",
    }


def _claim_next_due_job(db_path: Path, job_ids: list[int] | None = None) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        params = []
        query = """
            SELECT *
            FROM publish_jobs
            WHERE status IN ('queued', 'scheduled')
              AND delivery_mode IN ('direct_upload', 'sheet_export')
              AND (
                  scheduled_at IS NULL
                  OR scheduled_at = ''
                  OR scheduled_at <= ?
              )
        """
        params.append(datetime.now().isoformat(timespec="seconds"))
        if job_ids:
            placeholders = ",".join("?" for _ in job_ids)
            query += f" AND id IN ({placeholders})"
            params.extend(job_ids)
        query += " ORDER BY COALESCE(scheduled_at, created_at) ASC, id ASC LIMIT 1"
        cursor.execute(query, params)
        row = cursor.fetchone()
        if not row:
            return None

        cursor.execute(
            """
            UPDATE publish_jobs
            SET status = 'processing',
                last_error = '',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = ?
            """,
            (row["id"], row["status"]),
        )
        if cursor.rowcount == 0:
            conn.commit()
            return None
        conn.commit()
    serialized = _serialize_job_row_with_native_fields(row)
    return serialized


def _serialize_job_row_with_native_fields(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = _parse_json_object(data.get("metadata_json"))
    data["hashtags"] = _parse_json_list(data.get("hashtags_json"))
    return {
        "id": data["id"],
        "profileId": data.get("profile_id"),
        "profileName": data.get("profile_name") or "",
        "targetKind": data.get("target_kind") or "",
        "accountId": data.get("account_id"),
        "contentAccountId": data.get("content_account_id") or "",
        "platformKey": data.get("platform_key") or "",
        "targetName": data.get("target_name") or "",
        "deliveryMode": data.get("delivery_mode") or "",
        "materialId": data.get("material_id"),
        "materialName": data.get("material_name") or "",
        "mediaPath": data.get("media_path") or "",
        "mediaPublicUrl": data.get("media_public_url") or "",
        "title": data.get("title") or "",
        "message": data.get("message") or "",
        "metadata": data.get("metadata") or {},
        "status": data.get("status") or "",
        "scheduledAt": data.get("scheduled_at") or "",
    }


def _execute_direct_upload_job(db_path: Path, job: dict[str, Any], base_dir: Path | None = None) -> dict[str, Any]:
    platform_key = job["platformKey"]

    if platform_key in {"xiaohongshu", "channels", "douyin", "kuaishou"}:
        from myUtils.postVideo import post_video_DouYin, post_video_ks, post_video_tencent, post_video_xhs

        account = _get_account_by_id(db_path, job["accountId"])
        account_file = account["filePath"]
        media_path = job["mediaPath"]
        title = job["title"] or job["materialName"] or "Untitled"
        message = job["message"] or ""

    if platform_key == "xiaohongshu":
        post_video_xhs(title, [media_path], "", [account_file], None, False, 1, None, 0, message)
        return {"platform": platform_key, "finalStatus": "published"}
    if platform_key == "channels":
        post_video_tencent(title, [media_path], "", [account_file], None, False, 1, None, 0, False)
        return {"platform": platform_key, "finalStatus": "published"}
    if platform_key == "douyin":
        post_video_DouYin(title, [media_path], "", [account_file], None, False, 1, None, 0, "", "", "", message)
        return {"platform": platform_key, "finalStatus": "published"}
    if platform_key == "kuaishou":
        post_video_ks(title, [media_path], "", [account_file], None, False, 1, None, 0, message)
        return {"platform": platform_key, "finalStatus": "published"}
    if platform_key in {"twitter", "telegram", "reddit", "discord", "facebook", "threads", "youtube", "tiktok"}:
        if base_dir is None:
            raise ValueError("base_dir is required for direct publisher targets")
        target = _resolve_direct_upload_target(db_path, job, base_dir)
        return publish_job_to_direct_target(base_dir, job, target)
    raise ValueError(f"unsupported direct upload platform: {platform_key}")


def _execute_sheet_export_job(db_path: Path, job: dict[str, Any]) -> None:
    if not job["profileId"]:
        raise ValueError("sheet export job requires profileId")
    profile = get_profile(db_path, int(job["profileId"]))
    material = get_material_record(db_path, int(job["materialId"])) if job.get("materialId") else None
    if material is None:
        material = {"id": job.get("materialId"), "filename": job.get("materialName") or ""}

    upload_result = {
        "publicUrl": job["mediaPublicUrl"],
        "mediaKind": job["metadata"].get("mediaKind") or infer_media_kind(Path(job["mediaPath"])),
    }
    content_account_results = None
    posts = {}
    if job["targetKind"] == "content_account":
        content_account_results = [
            {
                "account": {
                    "id": job["contentAccountId"],
                    "name": job["targetName"],
                    "platform": job["platformKey"],
                    "postPreset": job["metadata"].get("postPreset") or "",
                },
                "content": job["message"],
            }
        ]
    else:
        posts[job["platformKey"]] = job["message"]

    rows = build_google_sheet_rows(
        profile,
        posts,
        upload_result,
        job.get("scheduledAt") or None,
        content_account_results,
    )
    append_rows_to_google_sheet(profile, rows, job.get("scheduledAt") or None)


def _sync_scheduler_sheet_copy_for_job(db_path: Path, job_id: int) -> None:
    job = _get_job_by_id(db_path, job_id)
    if not _should_create_scheduler_sheet_copy(job):
        return
    profile = get_profile(db_path, int(job["profileId"]))
    if not ((profile.get("settings") or {}).get("googleSheet") or {}).get("spreadsheetId"):
        return

    metadata = dict(job.get("metadata") or {})
    scheduler_sheet = metadata.get("schedulerSheet") if isinstance(metadata.get("schedulerSheet"), dict) else {}
    if scheduler_sheet.get("rowNumbers"):
        return

    profile, rows = _build_google_sheet_rows_for_job(db_path, job, job.get("scheduledAt") or None)
    result = append_rows_to_google_sheet(profile, rows, job.get("scheduledAt") or None)
    metadata["schedulerSheet"] = {
        "spreadsheetId": result.get("spreadsheetId") or "",
        "worksheet": result.get("worksheet") or "",
        "rowNumbers": result.get("rowNumbers") or [],
        "scheduledAt": job.get("scheduledAt") or "",
        "state": "active",
        "retryCount": int(scheduler_sheet.get("retryCount") or 0),
    }
    _update_job_metadata(db_path, job["id"], metadata)


def _cleanup_scheduler_sheet_for_job(db_path: Path, job: dict[str, Any]) -> None:
    if not _should_create_scheduler_sheet_copy(job):
        return
    profile = get_profile(db_path, int(job["profileId"])) if job.get("profileId") else None
    if not profile or not (((profile.get("settings") or {}).get("googleSheet") or {}).get("spreadsheetId")):
        return

    metadata = dict(job.get("metadata") or {})
    scheduler_sheet = metadata.get("schedulerSheet") if isinstance(metadata.get("schedulerSheet"), dict) else {}
    row_numbers = scheduler_sheet.get("rowNumbers") or []
    worksheet_name = str(scheduler_sheet.get("worksheet") or "").strip()
    if not row_numbers or not worksheet_name or not job.get("profileId"):
        return

    delete_rows_from_google_sheet(profile, worksheet_name, row_numbers)
    scheduler_sheet["rowNumbers"] = []
    scheduler_sheet["state"] = "deleted"
    metadata["schedulerSheet"] = scheduler_sheet
    _update_job_metadata(db_path, job["id"], metadata)


def _reschedule_scheduler_sheet_retry_for_job(db_path: Path, job: dict[str, Any]) -> None:
    if not _should_create_scheduler_sheet_copy(job):
        return
    profile = get_profile(db_path, int(job["profileId"])) if job.get("profileId") else None
    if not profile or not (((profile.get("settings") or {}).get("googleSheet") or {}).get("spreadsheetId")):
        return

    metadata = dict(job.get("metadata") or {})
    scheduler_sheet = metadata.get("schedulerSheet") if isinstance(metadata.get("schedulerSheet"), dict) else {}
    if scheduler_sheet.get("retryCreatedAt"):
        return

    source_value = job.get("scheduledAt") or job.get("createdAt") or ""
    if not source_value:
        return

    retry_at = (_parse_iso_datetime(source_value) + timedelta(days=7)).isoformat(timespec="seconds")
    profile, rows = _build_google_sheet_rows_for_job(db_path, job, retry_at)
    result = append_rows_to_google_sheet(profile, rows, retry_at)

    old_rows = scheduler_sheet.get("rowNumbers") or []
    old_worksheet = str(scheduler_sheet.get("worksheet") or "").strip()
    if old_rows and old_worksheet:
        delete_rows_from_google_sheet(profile, old_worksheet, old_rows)

    scheduler_sheet.update({
        "spreadsheetId": result.get("spreadsheetId") or scheduler_sheet.get("spreadsheetId") or "",
        "worksheet": result.get("worksheet") or "",
        "rowNumbers": result.get("rowNumbers") or [],
        "scheduledAt": retry_at,
        "state": "retry_active",
        "retryCount": int(scheduler_sheet.get("retryCount") or 0) + 1,
        "retryCreatedAt": datetime.utcnow().isoformat(timespec="seconds"),
    })
    metadata["schedulerSheet"] = scheduler_sheet
    _update_job_metadata(db_path, job["id"], metadata)


def _build_google_sheet_rows_for_job(
    db_path: Path,
    job: dict[str, Any],
    schedule_at: str | None,
) -> tuple[dict[str, Any], list[list[str]]]:
    if not job["profileId"]:
        raise ValueError("scheduler sheet copy requires profileId")

    profile = get_profile(db_path, int(job["profileId"]))
    upload_result = {
        "publicUrl": job["mediaPublicUrl"],
        "mediaKind": job["metadata"].get("mediaKind") or infer_media_kind(Path(job["mediaPath"])),
    }

    if job["targetKind"] == "content_account":
        rows = build_google_sheet_rows(
            profile,
            {},
            upload_result,
            schedule_at,
            [
                {
                    "account": {
                        "id": job["contentAccountId"],
                        "name": job["targetName"],
                        "platform": job["platformKey"],
                        "postPreset": job["metadata"].get("postPreset") or "",
                    },
                    "content": job["message"],
                }
            ],
        )
    else:
        rows = build_google_sheet_rows(
            profile,
            {job["platformKey"]: job["message"]},
            upload_result,
            schedule_at,
            None,
        )
    return profile, rows


def _get_accounts_by_ids(db_path: Path, account_ids: list[int]) -> list[dict[str, Any]]:
    if not account_ids:
        return []
    placeholders = ",".join("?" for _ in account_ids)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM user_info WHERE id IN ({placeholders}) ORDER BY id ASC",
            account_ids,
        )
        rows = cursor.fetchall()
    return [serialize_account_row(row) for row in rows]


def _get_account_by_id(db_path: Path, account_id: int | None) -> dict[str, Any]:
    if not account_id:
        raise ValueError("direct upload job requires accountId")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_info WHERE id = ?", (int(account_id),))
        row = cursor.fetchone()
    if not row:
        raise ValueError("managed account not found")
    return serialize_account_row(row)


def _get_job_by_id(db_path: Path, job_id: int) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT j.*,
                   COALESCE(MAX(r.revision_no), 0) AS revision_count
            FROM publish_jobs j
            LEFT JOIN publish_job_revisions r ON r.job_id = j.id
            WHERE j.id = ?
            GROUP BY j.id
            """,
            (int(job_id),),
        )
        row = cursor.fetchone()
    if not row:
        raise ValueError("publish job not found")
    return _serialize_job_row(row)


def _update_job_metadata(db_path: Path, job_id: int, metadata: dict[str, Any]) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE publish_jobs
            SET metadata_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (json.dumps(metadata, ensure_ascii=False), int(job_id)),
        )
        conn.commit()


def _set_job_processing(db_path: Path, job_id: int) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE publish_jobs
            SET status = 'processing',
                last_error = '',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(job_id),),
        )
        conn.commit()


def _apply_direct_publish_receipt(db_path: Path, job: dict[str, Any], receipt: dict[str, Any] | None) -> str:
    if not isinstance(receipt, dict):
        return "published"
    receipt = receipt or {}
    metadata = dict(job.get("metadata") or {})
    if receipt:
        metadata["publishReceipt"] = {key: value for key, value in receipt.items() if key not in {"details"}}
        if receipt.get("details"):
            metadata["publishDetails"] = receipt.get("details")
        if receipt.get("publishId"):
            metadata["publishId"] = receipt["publishId"]
        if receipt.get("videoId"):
            metadata["videoId"] = receipt["videoId"]
        if receipt.get("threadId"):
            metadata["threadId"] = receipt["threadId"]
        if receipt.get("creationId"):
            metadata["creationId"] = receipt["creationId"]
        if receipt.get("url"):
            metadata["publishedUrl"] = receipt["url"]
        metadata["lastPublishAttemptAt"] = datetime.now().isoformat(timespec="seconds")
        _update_job_metadata(db_path, job["id"], metadata)
    return str(receipt.get("finalStatus") or "published").strip() or "published"


def _list_jobs_for_status_sync(db_path: Path, job_ids: list[int] | None = None) -> list[dict[str, Any]]:
    params: list[Any] = ["processing"]
    query = """
        SELECT j.*,
               COALESCE(MAX(r.revision_no), 0) AS revision_count
        FROM publish_jobs j
        LEFT JOIN publish_job_revisions r ON r.job_id = j.id
        WHERE j.status = ?
          AND j.delivery_mode = 'direct_upload'
          AND j.platform_key IN ('threads', 'youtube', 'tiktok')
    """
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        query += f" AND j.id IN ({placeholders})"
        params.extend(int(job_id) for job_id in job_ids)
    query += """
        GROUP BY j.id
        ORDER BY COALESCE(j.scheduled_at, j.created_at) ASC, j.id ASC
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
    return [_serialize_job_row(row) for row in rows]


def _apply_publish_status_result(db_path: Path, job: dict[str, Any], status_result: dict[str, Any]) -> str:
    status = str(status_result.get("status") or "processing").strip() or "processing"
    metadata = dict(job.get("metadata") or {})
    details = status_result.get("details") if isinstance(status_result.get("details"), dict) else {}
    existing_details = metadata.get("publishDetails") if isinstance(metadata.get("publishDetails"), dict) else {}
    publish_status = {
        "platform": status_result.get("platform") or job.get("platformKey") or "",
        "status": status,
        "checkedAt": datetime.now().isoformat(timespec="seconds"),
    }
    if status_result.get("remoteId"):
        publish_status["remoteId"] = status_result["remoteId"]
    if status_result.get("creationId"):
        metadata["creationId"] = status_result["creationId"]
    if status_result.get("threadId"):
        metadata["threadId"] = status_result["threadId"]
    if status_result.get("videoId"):
        metadata["videoId"] = status_result["videoId"]
    if status_result.get("publishId"):
        metadata["publishId"] = status_result["publishId"]
    if status_result.get("url"):
        publish_status["url"] = status_result["url"]
        metadata["publishedUrl"] = status_result["url"]
    if details:
        publish_status["details"] = details
        metadata["publishDetails"] = {**existing_details, **details}
    if status_result.get("error"):
        publish_status["error"] = status_result["error"]
    metadata["publishStatus"] = publish_status
    metadata["lastStatusCheckAt"] = publish_status["checkedAt"]
    _update_job_metadata(db_path, job["id"], metadata)

    if status == "published":
        _set_job_success(db_path, job["id"], "published")
        return "published"
    if status == "failed":
        _set_job_failure(db_path, job["id"], str(status_result.get("error") or "publish status sync failed"))
        return "failed"
    _set_job_processing(db_path, job["id"])
    return "processing"


def _record_status_sync_error(db_path: Path, job_id: int, error_text: str) -> None:
    job = _get_job_by_id(db_path, job_id)
    metadata = dict(job.get("metadata") or {})
    metadata["lastStatusSyncError"] = str(error_text or "").strip()
    metadata["lastStatusCheckAt"] = datetime.now().isoformat(timespec="seconds")
    _update_job_metadata(db_path, job_id, metadata)


def _update_job_status(db_path: Path, job_id: int, status: str) -> dict[str, Any]:
    ensure_publish_job_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE publish_jobs
            SET status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, int(job_id)),
        )
        if cursor.rowcount == 0:
            raise ValueError("publish job not found")
        conn.commit()
        cursor.execute(
            """
            SELECT j.*,
                   COALESCE(MAX(r.revision_no), 0) AS revision_count
            FROM publish_jobs j
            LEFT JOIN publish_job_revisions r ON r.job_id = j.id
            WHERE j.id = ?
            GROUP BY j.id
            """,
            (int(job_id),),
        )
        row = cursor.fetchone()
    return _serialize_job_row(row)


def _set_job_success(db_path: Path, job_id: int, status: str) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE publish_jobs
            SET status = ?,
                published_at = CURRENT_TIMESTAMP,
                last_error = '',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, int(job_id)),
        )
        conn.commit()
    _cleanup_scheduler_sheet_for_job(db_path, _get_job_by_id(db_path, job_id))


def _set_job_failure(db_path: Path, job_id: int, error_text: str) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE publish_jobs
            SET status = 'failed',
                last_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(error_text or "").strip(), int(job_id)),
        )
        conn.commit()
    _reschedule_scheduler_sheet_retry_for_job(db_path, _get_job_by_id(db_path, job_id))


def _build_job_title(material: dict[str, Any], message: str) -> str:
    filename = str(material.get("filename") or "").strip()
    if filename:
        return Path(filename).stem[:80]
    fallback = str(message or "").strip()
    if not fallback:
        return "Untitled"
    return fallback.splitlines()[0][:80]


def _delivery_mode_for_platform(platform_key: str) -> str:
    normalized = str(platform_key or "").strip().lower()
    if normalized in DIRECT_UPLOAD_PLATFORMS:
        return "direct_upload"
    if normalized in SHEET_EXPORT_PLATFORMS:
        return "sheet_export"
    if normalized in MANUAL_ONLY_PLATFORMS:
        return "manual_only"
    return "manual_only"


def _resolve_draft_delivery_mode(platform_key: str, target_kind: str, metadata: dict[str, Any] | None = None) -> str:
    normalized = str(platform_key or "").strip().lower()
    metadata = metadata or {}
    if normalized in DIRECT_UPLOAD_PLATFORMS:
        return "direct_upload"
    if normalized in MANAGED_DIRECT_UPLOAD_PLATFORMS:
        if target_kind == "managed_account":
            return "direct_upload"
        if str(metadata.get("publishingAccountId") or "").strip() or str(metadata.get("publisherTargetId") or "").strip():
            return "direct_upload"
    return _delivery_mode_for_platform(normalized)


def _should_create_scheduler_sheet_copy(job: dict[str, Any]) -> bool:
    platform_key = str(job.get("platformKey") or "").strip().lower()
    return (
        job.get("deliveryMode") == "direct_upload"
        and platform_key in SCHEDULER_MIRROR_PLATFORMS
        and bool(job.get("profileId"))
        and bool(job.get("message"))
    )


def _pick_primary_message(posts: dict[str, Any]) -> str:
    ordered_platforms = [
        "twitter",
        "bluesky",
        "threads",
        "instagram",
        "facebook",
        "youtube",
        "tiktok",
        "telegram",
        "line_oa",
        "patreon",
        "reddit",
    ]
    for platform in ordered_platforms:
        value = str(posts.get(platform) or "").strip()
        if value:
            return value
    return ""


def _find_content_account(profile: dict[str, Any], content_account_id: str) -> dict[str, Any]:
    settings = profile.get("settings") or {}
    for item in settings.get("contentAccounts") or []:
        if str(item.get("id") or "").strip() == str(content_account_id or "").strip():
            return item
    raise ValueError("content account not found")


def _find_linked_content_account_result(
    content_account_results: list[dict[str, Any]],
    publishing_account_id: int | str,
    platform_key: str,
) -> dict[str, Any] | None:
    publishing_account_id = str(publishing_account_id or "").strip()
    normalized_platform = str(platform_key or "").strip().lower()
    if not publishing_account_id:
        return None

    for result in content_account_results or []:
        account = result.get("account") or {}
        if (
            str(account.get("publishingAccountId") or "").strip() == publishing_account_id
            and str(account.get("platform") or "").strip().lower() == normalized_platform
        ):
            return result
    return None


def _resolve_direct_upload_target(db_path: Path, job: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    target_id = str((job.get("metadata") or {}).get("publisherTargetId") or "").strip()
    if target_id:
        return get_direct_publisher_target(base_dir, target_id)

    if job.get("targetKind") == "managed_account" and job.get("accountId"):
        account = _get_account_by_id(db_path, job["accountId"])
        return _build_direct_target_from_account(account)

    raise ValueError(f"{job['platformKey']} direct upload job requires publisherTargetId or managed account credentials")


def _build_direct_target_from_account(account: dict[str, Any]) -> dict[str, Any]:
    platform = str(account.get("platformKey") or "").strip().lower()
    metadata = account.get("metadata") or {}

    if platform == "twitter":
        return {
            "platform": "twitter",
            "enabled": True,
            "config": {
                "apiKey": str(metadata.get("apiKey") or "").strip(),
                "apiKeySecret": str(metadata.get("apiKeySecret") or "").strip(),
                "accessToken": str(metadata.get("accessToken") or "").strip(),
                "accessTokenSecret": str(metadata.get("accessTokenSecret") or "").strip(),
            },
        }

    if platform == "reddit":
        return {
            "platform": "reddit",
            "enabled": True,
            "config": {
                "clientId": str(metadata.get("clientId") or "").strip(),
                "clientSecret": str(metadata.get("clientSecret") or "").strip(),
                "refreshToken": str(metadata.get("refreshToken") or "").strip(),
                "subreddit": str(metadata.get("subreddit") or "").strip(),
            },
        }

    if platform == "facebook":
        return {
            "platform": "facebook",
            "enabled": True,
            "config": {
                "accessToken": str(metadata.get("accessToken") or "").strip(),
                "pageId": str(metadata.get("pageId") or "").strip(),
            },
        }

    if platform == "threads":
        return {
            "platform": "threads",
            "enabled": True,
            "config": {
                "accessToken": str(metadata.get("accessToken") or "").strip(),
                "userId": str(metadata.get("userId") or "").strip(),
            },
        }

    if platform == "youtube":
        return {
            "platform": "youtube",
            "enabled": True,
            "config": {
                "accessToken": str(metadata.get("accessToken") or "").strip(),
                "privacyStatus": str(metadata.get("privacyStatus") or "").strip() or "private",
                "categoryId": str(metadata.get("categoryId") or "").strip() or "22",
            },
        }

    if platform == "tiktok":
        return {
            "platform": "tiktok",
            "enabled": True,
            "config": {
                "accessToken": str(metadata.get("accessToken") or "").strip(),
                "privacyLevel": str(metadata.get("privacyLevel") or "").strip() or "SELF_ONLY",
                "disableComment": bool(metadata.get("disableComment", False)),
                "disableDuet": bool(metadata.get("disableDuet", False)),
                "disableStitch": bool(metadata.get("disableStitch", False)),
            },
        }

    raise ValueError(f"managed account direct upload is not implemented for {platform}")


def _merge_instruction(existing_text: Any, instruction_text: str) -> str:
    base_text = str(existing_text or "").strip()
    extra_text = str(instruction_text or "").strip()
    if not base_text:
        return extra_text
    if not extra_text:
        return base_text
    return f"{base_text}\n\nAdditional instruction:\n{extra_text}"


def _count_by_key(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts = {}
    for item in items:
        value = str(item.get(key) or "").strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def _normalize_int_list(values: Any) -> list[int]:
    if not values:
        return []
    if isinstance(values, str):
        values = [item for item in values.split(",") if item]
    normalized = []
    seen = set()
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number in seen:
            continue
        seen.add(number)
        normalized.append(number)
    return normalized


def _normalize_string_list(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [item for item in values.split(",") if item]
    normalized = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _coerce_int(value: Any, default: int | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError("scheduledAt/startAt must be a valid ISO datetime string") from exc


def _build_interval_delta(value: int, unit: str) -> timedelta:
    if unit == "minutes":
        return timedelta(minutes=value)
    if unit == "days":
        return timedelta(days=value)
    return timedelta(hours=value)


def _parse_json_object(raw_value: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw_value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_json_list(raw_value: Any) -> list[Any]:
    try:
        data = json.loads(raw_value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _job_event_date(job: dict[str, Any]) -> str:
    source_value = job.get("scheduledAt") or job.get("publishedAt") or job.get("createdAt") or ""
    text = str(source_value).strip()
    if "T" in text:
        return text.split("T", 1)[0]
    if " " in text:
        return text.split(" ", 1)[0]
    return text[:10]
