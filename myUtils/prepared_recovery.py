"""Validate and install ALL prepared recovery parts in the guarded transaction."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from myUtils import media_compliance

_MEDIA_FIELDS = {"artifacts", "preparedPart", "preparedPost", "partIndex", "partCount", "preparedImageBatch", "_preserveApprovedText"}


def validate_parts(conn, target, parts: list[dict], schedules: list[str]) -> None:
    from myUtils import jobs, publish_media_guard

    if (not isinstance(parts, list) or not 1 <= len(parts) <= media_compliance.MAX_PARTS
            or not isinstance(schedules, list) or len(parts) != len(schedules)
            or any(not isinstance(part, dict) for part in parts)):
        raise ValueError("Recovery requires a bounded complete list of payloads and schedules")
    moments = [jobs._utc_datetime(value) for value in schedules]
    if any(left >= right for left, right in zip(moments, moments[1:])):
        raise ValueError("Recovery parts must have strictly ordered future slots")
    original = jobs._recovery_json_object(conn.execute(
        "SELECT payload_json FROM publish_jobs WHERE id=?", (target["job_id"],)).fetchone()[0])
    original.update(jobs._recovery_json_object(target["checkpoint_json"]).get("payload") or {})
    artifacts = []
    for index, part in enumerate(parts, 1):
        if part.get("_preserveApprovedText") is not True:
            raise ValueError("Recovery must preserve approved text without truncation")
        if jobs._recovery_has_submission_evidence(json.dumps(part, allow_nan=False)):
            raise ValueError("Prepared payload carries submission evidence")
        if any(part.get(key) != original.get(key) for key in (set(part) | set(original)) - _MEDIA_FIELDS):
            raise ValueError("Preprocessing changed the original publishing intent")
        media = part.get("artifacts") or []
        if not isinstance(media, list) or any(not isinstance(a, dict) for a in media):
            raise ValueError("Prepared recovery artifacts must be objects")
        for artifact in media:
            metadata = artifact.get("metadata") or {}
            if publish_media_guard.is_context_artifact(artifact):
                raise ValueError("Internal support artifacts cannot become publishing media")
            if not isinstance(metadata, dict):
                raise ValueError("Prepared media metadata must be an object")
            path = Path(str(artifact.get("local_path") or ""))
            if (not path.is_file() or not metadata.get("sha256")
                    or media_compliance.sha256_file(path) != metadata["sha256"]):
                raise ValueError("Prepared recovery media changed or is missing")
            if artifact.get("artifact_kind") == "prepared_video":
                artifacts.append({**artifact, "id": index})
        if len(parts) > 1:
            label = part.get("preparedPost") or part.get("preparedPart") or {}
            if not isinstance(label, dict) or label.get("index") != index or label.get("count") != len(parts):
                raise ValueError("Prepared recovery parts are incomplete or unordered")
    if len(parts) > 1 and artifacts:
        platform = conn.execute("SELECT platform FROM publish_jobs WHERE id=?", (target["job_id"],)).fetchone()[0]
        groups = {}
        original_groups = {}
        for original_artifact in original.get("artifacts") or []:
            metadata = original_artifact.get("metadata") or {}
            if metadata.get("preparation_group"):
                original_groups.setdefault(metadata["preparation_group"], set()).add(metadata.get("part_index"))
        for artifact in artifacts:
            metadata = artifact["metadata"]
            groups.setdefault(metadata.get("preparation_group"), []).append(artifact)
        for group, members in groups.items():
            indices = {a["metadata"].get("part_index") for a in members}
            if group in original_groups and indices == original_groups[group]:
                continue  # the original intent already selected these immutable parts
            selected = media_compliance.select_prepared_parts(members, platform)
            if len(selected) != len(members):
                raise ValueError("Recovery did not contain the complete video part set")


def _checkpoint(jobs, value) -> str:
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(text.encode("utf-8")) > jobs.CHECKPOINT_MAX_BYTES:
        raise ValueError("Prepared recovery checkpoint exceeds its byte budget")
    return text


def install_parts(conn, target, parts: list[dict], schedules: list[str], generation: str) -> None:
    from myUtils import jobs

    original_job = conn.execute("SELECT * FROM publish_jobs WHERE id=?", (target["job_id"],)).fetchone()
    previous = {"attempts": target["attempts"], "last_error": target["last_error"],
                "finished_at": target["finished_at"], "schedule_at": target["schedule_at"],
                "checkpoint": jobs._decode_checkpoint(target["checkpoint_json"])}
    conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (
        _checkpoint(jobs, {"recovery_generation": generation, "previous_failure": previous,
                          "payload": parts[0]}), target["id"]))
    original_post_id = parts[0].get("campaignPostId")
    original_post = None
    if type(original_post_id) is int:
        original_post = conn.execute("SELECT * FROM campaign_posts WHERE id=?", (original_post_id,)).fetchone()
        if original_post is None:
            raise ValueError("Original campaign post disappeared")
        conn.execute("UPDATE campaign_posts SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (original_post_id,))
        # A target retry from an actually failed campaign must be executable.
        # Draft/review/cancelled campaigns were refused before this transaction.
        conn.execute("UPDATE campaigns SET status='publishing' WHERE id=? AND status='failed'",
                     (original_post["campaign_id"],))
    for index, (part, schedule) in enumerate(zip(parts[1:], schedules[1:]), 2):
        payload = copy.deepcopy(part)
        # Different ordered parts must not all masquerade as the original file
        # when a later duplicate/recovery check evaluates their identities.
        media = payload.get("artifacts") or []
        video = next((a for a in media if a.get("artifact_kind") == "prepared_video"), None)
        file_ref = str((video or (media[0] if media else {})).get("local_path") or target["file_ref"])
        post_id = None
        if original_post is not None:
            cursor = conn.execute(
                "INSERT INTO campaign_posts (campaign_id, platform, account_ids_json, draft_json, "
                "file_record_ids_json, sheet_row_json, workspace_id, status) VALUES (?,?,?,?,?,?,?,'queued')",
                (original_post["campaign_id"], original_post["platform"],
                 json.dumps([int(target["account_ref"].split(":", 1)[1])]), original_post["draft_json"],
                 original_post["file_record_ids_json"], original_post["sheet_row_json"], original_post["workspace_id"]))
            post_id = int(cursor.lastrowid)
            payload["campaignPostId"] = post_id
            file_ref = f"campaign_post:{post_id}"
        key = f"prepared-recovery:{target['id']}:{generation}:{index}"
        spec = jobs.JobSpec(platform=original_job["platform"], payload=payload,
                            profile_id=original_job["profile_id"],
                            targets=[(target["account_ref"], file_ref, schedule)], idempotency_key=key)
        cursor = conn.execute(
            "INSERT INTO publish_jobs (idempotency_key, profile_id, platform, payload_json, status, "
            "total_targets, workspace_id, intent_hash) VALUES (?,?,?,?,'pending',1,?,?)",
            (key, original_job["profile_id"], original_job["platform"],
             json.dumps(payload, ensure_ascii=False, allow_nan=False), original_job["workspace_id"],
             jobs._intent_hash(spec, original_job["workspace_id"])))
        job_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO publish_job_targets (job_id,account_ref,file_ref,schedule_at,status,workspace_id,checkpoint_json) "
            "VALUES (?,?,?,?,'pending',?,?)",
            (job_id, target["account_ref"], file_ref, schedule, original_job["workspace_id"],
             _checkpoint(jobs, {"recovery_generation": generation, "recovery_origin_target_id": target["id"]})))
        if post_id is not None:
            conn.execute("UPDATE campaign_posts SET last_published_job_id=? WHERE id=?", (job_id, post_id))
