"""Validate and install ALL prepared recovery parts in the guarded transaction."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from myUtils import media_compliance, media_integrity

_MEDIA_FIELDS = {"artifacts", "preparedPart", "preparedPost", "partIndex", "partCount", "preparedImageBatch", "_preserveApprovedText"}


def validate_parts(conn, target, parts: list[dict], schedules: list[str], *,
                   reviewed_selected_content: bool = False) -> None:
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
    if reviewed_selected_content:
        from myUtils.reviewed_recovery import bind_media_payload, collect_media_records
        original = bind_media_payload(original, collect_media_records(conn, original))
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
            media_integrity.verify_artifact(path, artifact, required=True)
            if publish_media_guard.media_kind(artifact) == "video":
                artifacts.append({**artifact, "id": index})
        if len(parts) > 1:
            label = part.get("preparedPost") or part.get("preparedPart") or {}
            if not isinstance(label, dict) or label.get("index") != index or label.get("count") != len(parts):
                raise ValueError("Prepared recovery parts are incomplete or unordered")
    source_payload = copy.deepcopy(original)
    if not source_payload.get("artifacts") and not str(target["file_ref"]).startswith("campaign_post:"):
        source_payload["artifacts"] = [{"local_path": target["file_ref"]}]
    if artifacts or any(publish_media_guard.media_kind(a) == "video" for a in source_payload.get("artifacts") or []):
        platform = conn.execute("SELECT platform FROM publish_jobs WHERE id=?", (target["job_id"],)).fetchone()[0]
        validate_video_provenance(source_payload, artifacts, platform=platform,
                                  reviewed_selected_content=reviewed_selected_content)


def _exact_path(value) -> Path:
    from myUtils.worker import _resolve_media_path
    return _resolve_media_path(str(value or "")).expanduser().resolve()


def _span(metadata: dict, prefix: str = "source") -> tuple[float, float]:
    start = media_compliance._number(metadata.get(prefix + "_start_seconds"), "original source interval start", positive=False)
    end = media_compliance._number(metadata.get(prefix + "_end_seconds"), "original source interval end")
    if end <= start:
        raise ValueError("Invalid original source interval")
    return start, end


def validate_video_provenance(original: dict, artifacts: list[dict], *, platform: str,
                              probe_sources: bool = False, reviewed_selected_content: bool = False) -> None:
    """Bind EVERY output (including one) to exact approved source membership.

    Selected existing parts may remain a subset of their parent's generation.
    New derivatives must cover each selected input span, not the full raw video.
    Raw inputs are rehashed; their measured full duration is checked outside
    transactions with probe_sources=True, then pinned by the plan fingerprint.
    """
    from myUtils import publish_media_guard as guard
    sources = [a for a in original.get("artifacts") or [] if guard.media_kind(a) == "video"]
    if not sources:
        raise ValueError("Recovery video has no exact approved source")
    membership = {index: [] for index in range(len(sources))}
    previous_source = -1
    identity_keys = ("source_sha256", "source_path", "content_variant", "unwatermarked",
                     "origin_source_path", "origin_source_sha256", "source_identity", "source_parent",
                     "original_source_interval_start_seconds", "original_source_interval_end_seconds")
    for output in artifacts:
        media_integrity.verify_artifact(output.get("local_path") or "", output, required=True)
        metadata = output.get("metadata") or {}
        parent = metadata.get("reprepared_from") or {}
        if not isinstance(metadata, dict) or not isinstance(parent, dict):
            raise ValueError("Invalid recovery provenance")
        candidates = []
        for index, source in enumerate(sources):
            input_path = parent.get("path") or metadata.get("input_path")
            if source.get("local_path"):
                path = _exact_path(source["local_path"])
                unchanged = _exact_path(output.get("local_path")) == path
                matched = unchanged or (input_path and _exact_path(input_path) == path)
            else:
                # A materialised exact registered URL is allowed; never match
                # a basename or infer a different raw/full source.
                unchanged = False
                matched = bool(input_path and source.get("public_url")
                               and metadata.get("input_public_url") == source["public_url"])
            if matched:
                candidates.append((index, unchanged))
        if len(candidates) != 1:
            raise ValueError("Recovery output has ambiguous or substituted source identity")
        index, unchanged = candidates[0]
        if index < previous_source:
            raise ValueError("Recovery reordered approved source membership")
        previous_source = index
        source, selected = sources[index], sources[index].get("metadata") or {}
        path = _exact_path(source.get("local_path") or metadata.get("input_path"))
        facts = media_integrity.verify_artifact(path, source)
        if source.get("source_file_record_id") != output.get("source_file_record_id"):
            raise ValueError("Recovery changed the source file record identity")
        for key in ("content_variant", "unwatermarked", "origin_source_path", "origin_source_sha256"):
            if source.get(key) != output.get(key):
                raise ValueError("Recovery changed the approved source variant")
            # TikTok's existing raw-source preparation explicitly labels the
            # selected watermark-free output; all other variant facts are exact.
            if key == "unwatermarked" and platform == "tiktok" and selected.get(key) is None:
                continue
            if selected.get(key) != metadata.get(key):
                raise ValueError("Recovery changed the approved source variant")
        if unchanged:
            for key in (*identity_keys, "part_index", "part_count", "preparation_group",
                        "source_start_seconds", "source_end_seconds",
                        "source_interval_start_seconds", "source_interval_end_seconds"):
                if selected.get(key) != metadata.get(key):
                    raise ValueError("Recovery relabelled an existing selected part")
            membership[index].append((output, True))
            continue
        if metadata.get("input_sha256") != facts["sha256"]:
            raise ValueError("Recovery source bytes differ from approved input")
        derived = guard._has_derived_identity(source)
        if derived:
            legacy = "source_start_seconds" not in selected and "source_end_seconds" not in selected
            if legacy:
                if not reviewed_selected_content or not isinstance(metadata.get("selected_content_origin"), dict):
                    raise ValueError("Legacy selected part has no original source interval; explicit frozen review required")
                from myUtils.reviewed_recovery import legacy_selection
                proof = legacy_selection(source, path,
                    duration=metadata["selected_content_origin"].get("duration_seconds"), probe=probe_sources)
                if proof != metadata["selected_content_origin"]:
                    raise ValueError("Recovery changed exact selected file bytes or original membership")
                interval = (0.0, proof["duration_seconds"])
            else:
                # Absence is not evidence that a legacy segment was the full raw source.
                interval = _span(selected)
                if metadata.get("selected_content_origin") != selected.get("selected_content_origin"):
                    raise ValueError("Recovery changed reviewed source provenance")
            if (not isinstance(parent, dict) or parent.get("sha256") != facts["sha256"]
                    or any(parent.get(key) != selected.get(key) for key in
                           ("preparation_group", "part_index", "part_count"))
                    or _span(parent) != interval):
                raise ValueError("Recovery lost parent part membership or exact selected interval")
            if (metadata.get("source_sha256") != selected.get("source_sha256")
                    or metadata.get("source_parent") != (selected.get("source_parent") or parent)):
                raise ValueError("Recovery changed original source hash or parent membership")
            for key in identity_keys:
                if selected.get(key) is not None and metadata.get(key) != selected[key]:
                    raise ValueError("Recovery changed original source provenance or variant")
            expected_identity = guard._source_identity(source, path, selected.get("source_sha256"))
            if metadata.get("source_identity") != expected_identity:
                raise ValueError("Recovery changed exact original source identity")
            if metadata.get("source_is_complete") is not False:
                raise ValueError("A selected prepared segment cannot become a full raw source")
        else:
            duration = media_compliance._number(metadata.get("source_duration_seconds"), "complete raw duration")
            interval = (0.0, duration)
            if (parent or metadata.get("source_is_complete") is not True
                    or metadata.get("source_sha256") != facts["sha256"]
                    or metadata.get("source_identity") != guard._source_identity(source, path, facts["sha256"])):
                raise ValueError("Recovery did not verify the complete exact raw source")
            if probe_sources:
                media_compliance.decode_video(path, duration=duration)
                measured = media_compliance._source_metadata(path)["duration"]
                if abs(measured - duration) > 0.000001:
                    raise ValueError("Recovery raw source interval is not complete")
        if _span(metadata, "source_interval") != interval:
            raise ValueError("Recovery changed the approved complete source span")
        membership[index].append((output, False))
    for members in membership.values():
        if not members:
            raise ValueError("Recovery dropped an approved video source or selected part")
        if any(unchanged for _, unchanged in members):
            if len(members) != 1:
                raise ValueError("Recovery duplicated a selected existing part")
            continue
        outputs = [a for a, _ in members]
        groups = {a["metadata"].get("preparation_group") for a in outputs}
        if len(groups) != 1:
            raise ValueError("Recovery mixed derivative generations")
        selected = media_compliance.select_prepared_parts(outputs, platform)
        if len(selected) != len(outputs):
            raise ValueError("Recovery did not contain the complete unique video part set")
        if selected != outputs:
            raise ValueError("Recovery reordered complete video source intervals")


def _checkpoint(jobs, value) -> str:
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(text.encode("utf-8")) > jobs.CHECKPOINT_MAX_BYTES:
        raise ValueError("Prepared recovery checkpoint exceeds its byte budget")
    return text


def install_parts(conn, target, parts: list[dict], schedules: list[str], generation: str) -> list[int]:
    from myUtils import jobs

    original_job = conn.execute("SELECT * FROM publish_jobs WHERE id=?", (target["job_id"],)).fetchone()
    prior = jobs._recovery_json_object(target["checkpoint_json"])
    # Keep ACTIVE counters as well as the full immutable old checkpoint/history.
    # A new generation must not make worker defaults refund spent provider credit.
    base = {**copy.deepcopy(prior), "recovery_generation": generation,
            "provider_budget_baseline": prior.get("provider_budget_baseline", target["attempts"]),
            "previous_failure": jobs._prior_target_history(target)}
    conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (
        _checkpoint(jobs, {**base, "payload": parts[0]}), target["id"]))
    installed = [target["id"]]
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
        cursor = conn.execute(
            "INSERT INTO publish_job_targets (job_id,account_ref,file_ref,schedule_at,status,attempts,workspace_id,checkpoint_json) "
            "VALUES (?,?,?,?,'pending',?,?,?)",
            (job_id, target["account_ref"], file_ref, schedule, target["attempts"], original_job["workspace_id"],
             _checkpoint(jobs, {**copy.deepcopy(base), "payload": payload,
                               "recovery_origin_target_id": target["id"]})))
        installed.append(int(cursor.lastrowid))
        if post_id is not None:
            conn.execute("UPDATE campaign_posts SET last_published_job_id=? WHERE id=?", (job_id, post_id))
    return installed
