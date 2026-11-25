"""Strict helpers for an explicitly reviewed, pre-submission recovery only.

A JSON manifest is evidence, not review authority. The execution boundary must
receive the frozen selected IDs again. Ordinary failed retry APIs stay unchanged.
"""
from __future__ import annotations

import copy

from myUtils import delivery, media_compliance, media_integrity

# Configuration, permission, content-policy and ambiguous delivery blocks are not
# resolved by making compliant media. Only maintained no-post prerequisite holds.
RESOLVABLE_CATEGORIES = {"integrity", "verification", "transport", "timeout", "missing",
                         "capacity", "cooldown", "interrupted"}


def assert_prerequisite(row, payload: str) -> None:
    from myUtils import jobs
    if row["status"] != jobs.TARGET_BLOCKED:
        raise ValueError("Reviewed prerequisite target is no longer blocked")
    checkpoint = jobs._recovery_json_object(row["checkpoint_json"])
    prior = checkpoint.get("prerequisites")
    category = str(row["error_category"] or "").removeprefix("prerequisite_")
    if (row["error_category"] != "prerequisite_" + category or category not in RESOLVABLE_CATEGORIES
            or not isinstance(prior, dict) or prior.get("category") != category
            or type(prior.get("attempts")) is not int or prior["attempts"] < 1
            or type(prior.get("max_attempts")) is not int or prior["max_attempts"] < 1
            or prior.get("exhausted") is not True):
        raise ValueError("Only genuine maintained pre-submission prerequisite blocks can be reviewed")
    if any(row[k] is not None for k in ("lease_owner", "lease_token", "lease_expires_at")):
        raise ValueError("Reviewed prerequisite target still has a lease")
    if (jobs._recovery_has_submission_evidence(row["checkpoint_json"], row["delivery_state"])
            or jobs._recovery_has_submission_evidence(payload)):
        raise ValueError("Retained submission evidence requires reconciliation, not recovery")
    assert_budget(checkpoint)


def assert_restored_dependency(row, payload: str, *, conn) -> dict:
    """Read-only proof for an explicitly reviewed *restored* quota wait.

    Not generic quota recovery: exact registered bytes must already exist and
    the current configured dependency must be healthy. All SQL reads, including
    source resolution, use the caller's snapshot. No probe, hold reset or
    provider call is permitted here, including inside BEGIN IMMEDIATE.
    """
    from myUtils import jobs, media_restore, publish_media_guard as guard, worker
    from myUtils.account_identity import structured_account_id

    checkpoint = jobs._recovery_json_object(row["checkpoint_json"])
    prior = checkpoint.get("prerequisites")
    if (row["status"] not in {jobs.TARGET_BLOCKED, jobs.TARGET_RETRYING}
            or row["error_category"] != "prerequisite_quota"
            or not isinstance(prior, dict) or prior.get("category") != "quota"
            or type(row["attempts"]) is not int or row["attempts"] < 1
            or type(prior.get("attempts")) is not int or prior["attempts"] < 1
            or type(prior.get("max_attempts")) is not int or not 1 <= prior["max_attempts"] <= 100
            or prior["attempts"] > min(prior["max_attempts"], row["attempts"])):
        raise ValueError("Only genuine bounded prerequisite quota waits can be reviewed as restored")
    exhausted = row["status"] == jobs.TARGET_BLOCKED
    if (prior.get("exhausted") is not exhausted
            or (prior["attempts"] == prior["max_attempts"]) is not exhausted):
        raise ValueError("Restored dependency wait has inconsistent retained counters or status")
    if any(row[k] is not None for k in ("lease_owner", "lease_token", "lease_expires_at")):
        raise ValueError("Restored dependency wait still has a lease")
    if (jobs._recovery_has_submission_evidence(row["checkpoint_json"], row["delivery_state"])
            or jobs._recovery_has_submission_evidence(payload)):
        raise ValueError("Retained submission evidence requires reconciliation, not recovery")
    assert_budget(checkpoint)
    baseline = checkpoint.get("provider_budget_baseline", 0)
    if type(baseline) is not int or baseline > row["attempts"]:
        raise ValueError("Invalid historical provider baseline or baseline exceeds retained attempts")

    working = jobs._recovery_json_object(payload)
    override = checkpoint.get("payload", {})
    if not isinstance(override, dict):
        raise ValueError("Invalid restored selected-media payload progress")
    working = {**working, **copy.deepcopy(override)}
    account_id = structured_account_id(row["account_ref"])
    if account_id is None:
        raise ValueError("Restored dependency review requires the exact structured destination")
    working.update(_workspace_id=row["workspace_id"], _restore_account_id=account_id)
    artifacts = working.get("artifacts")
    if (not isinstance(artifacts, list) or not artifacts
            or any(not isinstance(a, dict) for a in artifacts)):
        raise ValueError("Restored dependency review requires exact registered publishing media")
    records = collect_media_records(conn, working)
    working = bind_media_payload(working, records)
    lookup = {record["id"]: record for record in records if record.get("registration_kind") != "file"}

    def healthy(state, fingerprint, kind):
        return (state is not None and state["config_fingerprint"] == fingerprint
                and state["kind"] == kind and state["status"] == "healthy"
                and state["reason"] is None and state["blocked_until"] is None
                and isinstance(state["last_checked_at"], str) and bool(state["last_checked_at"]))

    media = []
    for artifact in working["artifacts"]:
        if guard.is_context_artifact(artifact):
            continue
        if not isinstance(artifact.get("local_path"), str) or not artifact["local_path"]:
            raise ValueError("Restored selected-media path is missing")
        path = worker._resolve_media_path(artifact["local_path"]).resolve()
        try:
            selected, sources = worker._registered_restore_sources(artifact, path, payload=working, conn=conn)
        except media_restore.PrerequisiteUnavailable as exc:
            raise ValueError("Exact restored selected-media registration is unavailable") from exc
        record = lookup.get(artifact.get("id", artifact.get("artifact_id")))
        if record:
            metadata = jobs._recovery_json_object(record["metadata_json"] or "{}")
            digest = media_integrity.expected_sha256({"metadata": metadata}, required=True)
            size = metadata.get("size_bytes")
        else:
            # Only the resolver's exact file registration, never raw provenance.
            record = conn.execute("SELECT * FROM file_records WHERE id=?",
                                  (selected.get("file_record_id"),)).fetchone()
            if record is None:
                raise ValueError("Restored media has no exact persisted byte proof")
            digest = media_integrity.expected_sha256({"sha256": record["sha256"]}, required=True)
            size = record["size_bytes"]
        if type(size) is not int or size <= 0:
            raise ValueError("Restored media has no exact recorded byte count")
        facts = media_integrity.verify_artifact(path, selected, required=True)
        if facts != {"sha256": digest, "size_bytes": size}:
            raise ValueError("Restored media does not match its exact persisted byte proof")
        dependency = None
        for source in sources:
            backend = source.get("backend") or {}
            if backend and backend.get("enabled") != 1:
                continue
            scope, fingerprint = media_restore.dependency_identity(source)
            state = conn.execute("SELECT * FROM publish_dependency_states WHERE scope_key=?", (scope,)).fetchone()
            if not healthy(state, fingerprint, "media_restore"):
                continue
            object_scope, object_fingerprint = media_restore._object_identity(source)
            object_state = conn.execute("SELECT * FROM publish_dependency_states WHERE scope_key=?", (object_scope,)).fetchone()
            if (object_state is not None and object_state["config_fingerprint"] == object_fingerprint
                    and not healthy(object_state, object_fingerprint, "media_object")):
                continue
            dependency = {"scope_key": scope, "config_fingerprint": fingerprint,
                          "object_scope_key": object_scope, "object_fingerprint": object_fingerprint}
            break
        if dependency is None:
            raise ValueError("Exact restored media needs a matching healthy configured dependency without a hold")
        # Stable executed proof is hash-pinned; refresh timestamps are not authority.
        media.append({"artifact_id": selected.get("id"), "file_record_id": selected.get("file_record_id"),
                      "local_path": str(path), **facts, "dependency": dependency})
    if not media or len(media) > media_compliance.MAX_PARTS:
        raise ValueError("Restored review needs a bounded complete exact-media proof")
    return {"category": "quota", "media": media}


def assert_budget(checkpoint: dict) -> None:
    """Validate retained counters; never turn malformed/spent credit into zero."""
    baseline = checkpoint.get("provider_budget_baseline")
    if baseline is not None and (type(baseline) is not int or baseline < 0):
        raise ValueError("Invalid historical provider budget baseline")
    routes = delivery.publish_routes({"_delivery": checkpoint})
    delivery.active_route({"_delivery": checkpoint})
    for name in ("native", "sociamonials"):
        if name not in routes:
            continue
        route = routes[name]
        attempts, maximum = route.get("attempts", 0), route.get("max_attempts")
        if (type(attempts) is not int or attempts < 0 or type(maximum) is not int or maximum < 1
                or attempts >= maximum or route.get("outcome") in {"attempting", "unknown"}):
            raise ValueError("Invalid, uncertain or exhausted historical provider budget")


def legacy_selection(artifact: dict, path, *, duration: float | None = None, probe: bool = False) -> dict:
    """The coordinate system is the exact APPROVED SELECTED FILE, never raw video.

    Legacy part membership is retained verbatim. Missing absolute raw offsets
    stay missing; local [0, measured duration] covers every byte/frame of the
    selected clip without claiming a full raw source or inventing its SHA.
    """
    from myUtils import publish_media_guard as guard
    metadata = artifact.get("metadata") or {}
    if not guard._has_derived_identity(artifact) or any(k in metadata for k in ("source_start_seconds", "source_end_seconds")):
        raise ValueError("Reviewed local coordinates are only for legacy selected parts without raw offsets")
    index, count = metadata.get("part_index"), metadata.get("part_count")
    if type(index) is not int or type(count) is not int or not 1 <= index <= count <= media_compliance.MAX_PARTS:
        raise ValueError("Legacy selected media needs exact original parent part membership")
    facts = media_integrity.verify_artifact(path, artifact, required=True)
    if probe:
        duration = media_compliance._source_metadata(path)["duration"]
        media_compliance.decode_video(path, duration=duration)
        media_compliance.verify_checksum(path, facts["sha256"])
    duration = media_compliance._number(duration, "complete selected-file duration")
    return {"coordinate_system": "approved_selected_file", "path": str(path), **facts,
            "duration_seconds": duration, "source_file_record_id": artifact.get("source_file_record_id"),
            "original_metadata": copy.deepcopy(metadata)}


def predecessor_confirmed(conn, row) -> bool:
    """A scheduled/submitted predecessor is not delivery; malformed links close."""
    from myUtils import jobs
    # Receipt polling is independent of admission/ordering and must always drain.
    if row["status"] == jobs.TARGET_AWAITING_DELIVERY:
        return True
    try:
        checkpoint = jobs._recovery_json_object(row["checkpoint_json"])
    except ValueError:
        return True  # Existing worker corruption/review handling, not a new silent hold.
    if "recovery_predecessor_target_id" not in checkpoint:
        return True
    previous = checkpoint["recovery_predecessor_target_id"]
    if type(previous) is not int or previous < 1 or previous == row["id"]:
        return False
    predecessor = conn.execute("SELECT * FROM publish_job_targets WHERE id=?", (previous,)).fetchone()
    if (predecessor is None or predecessor["status"] != jobs.TARGET_SUCCEEDED
            or predecessor["delivery_state"] != "delivered"
            or predecessor["workspace_id"] != row["workspace_id"]
            or predecessor["account_ref"] != row["account_ref"]):
        return False
    try:
        current = jobs._recovery_json_object(predecessor["checkpoint_json"])
        delivery.publish_routes({"_delivery": current})  # Strict maintained receipt/type checks.
        receipt = current.get("provider_receipt")
        if current.get("x_browser_operations") and current.get("stage") != "browser_delivered":
            return False
        return (isinstance(receipt, dict) and receipt.get("status") == "delivered"
                and type(receipt.get("id")) in {str, int}
                and str(receipt["id"]).strip() not in {"", "confirmed", "0"})
    except (ValueError, delivery.DeliveryUnknown):
        return False


def collect_media_records(conn, payload: dict) -> list[dict]:
    """Exact artifacts or explicit exact file registrations, NEVER raw provenance."""
    from myUtils import jobs, publish_media_guard as guard
    from myUtils.worker import _file_record_local_path, _resolve_media_path
    records = []
    for index, artifact in enumerate(payload.get("artifacts") or []):
        if not isinstance(artifact, dict) or guard.is_context_artifact(artifact):
            continue
        identifier = artifact.get("id", artifact.get("artifact_id"))
        file_id = artifact.get("file_record_id")
        if identifier is None and file_id is not None:
            if type(file_id) is not int or file_id < 1:
                raise ValueError("Selected file registration requires an exact integer identity")
            row = conn.execute("SELECT * FROM file_records WHERE id=?", (file_id,)).fetchone()
            if (row is None or not artifact.get("local_path")
                    or _file_record_local_path(row["file_path"]) != _resolve_media_path(artifact["local_path"]).resolve()
                    or ("_workspace_id" in payload and row["workspace_id"] != payload["_workspace_id"])):
                raise ValueError("Selected file registration changed exact path or workspace")
            records.append({**dict(row), "registration_kind": "file", "artifact_index": index})
            continue
        if type(identifier) is not int or identifier < 1:
            continue
        row = conn.execute("SELECT * FROM campaign_artifacts WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Exact selected artifact registration disappeared")
        record = dict(row)
        if (record["campaign_id"] != payload.get("campaignId")
                or record["source_file_record_id"] != artifact.get("source_file_record_id")
                or record["artifact_kind"] != artifact.get("artifact_kind")
                or record["public_url"] != artifact.get("public_url")):
            raise ValueError("Selected artifact registration changed identity or variant")
        if _resolve_media_path(record["local_path"]).resolve() != _resolve_media_path(artifact.get("local_path") or "").resolve():
            raise ValueError("Selected artifact registration changed exact path")
        recorded = jobs._recovery_json_object(record["metadata_json"] or "{}")
        approved = artifact.get("metadata") or {}
        byte_fields = {"sha256", "checksum", "size_bytes"}
        if ({k: v for k, v in recorded.items() if k not in byte_fields}
                != {k: v for k, v in approved.items() if k not in byte_fields}):
            raise ValueError("Selected artifact registration changed approved provenance")
        records.append(record)
    return records


def bind_media_payload(payload: dict, records: list[dict]) -> dict:
    """Bind persisted executed byte facts without changing approved metadata.

    Records come from collect_media_records under fresh SQL ownership guards,
    including the atomic install transaction. Plans cannot supply these records.
    Explicit file registrations are keyed by artifact position, not the unrelated
    campaign id namespace or a raw source_file_record_id.
    """
    from myUtils import jobs, publish_media_guard as guard
    result = copy.deepcopy(payload)
    lookup = {row["id"]: row for row in records if row.get("registration_kind") != "file"}
    files = {row["artifact_index"]: row for row in records if row.get("registration_kind") == "file"}
    for index, artifact in enumerate(result.get("artifacts") or []):
        if not isinstance(artifact, dict) or guard.is_context_artifact(artifact):
            continue
        declared = media_integrity.expected_sha256(artifact)
        row = lookup.get(artifact.get("id", artifact.get("artifact_id"))) or files.get(index)
        if row:
            if row.get("registration_kind") == "file":
                recorded = media_integrity.expected_sha256({"sha256": row["sha256"]})
                size = row["size_bytes"]
            else:
                metadata = jobs._recovery_json_object(row["metadata_json"] or "{}")
                recorded = media_integrity.expected_sha256({"metadata": metadata})
                size = metadata.get("size_bytes")
            if declared and recorded and declared != recorded:
                raise ValueError("Approved and recorded selected-media checksums conflict")
            if recorded:
                artifact["sha256"] = recorded
                if type(size) is not int or size <= 0:
                    raise ValueError("Verified selected media has no recorded byte count")
                if artifact.get("size_bytes") is not None and artifact["size_bytes"] != size:
                    raise ValueError("Approved and recorded selected-media byte counts conflict")
                artifact["size_bytes"] = size
        media_integrity.expected_sha256(artifact, required=True)
    return result
