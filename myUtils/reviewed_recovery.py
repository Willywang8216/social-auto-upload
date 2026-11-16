"""Strict helpers for an explicitly reviewed, pre-submission recovery only.

A JSON manifest is evidence, not review authority. The execution boundary must
receive the frozen selected IDs again. Ordinary failed retry APIs stay unchanged.
"""
from __future__ import annotations

import copy

from myUtils import delivery, media_compliance, media_integrity

# Configuration, permission, content-policy and ambiguous delivery blocks are not
# resolved by making compliant media. Only maintained no-post prerequisite holds.
RESOLVABLE_CATEGORIES = {"integrity", "transport", "timeout", "missing", "capacity", "cooldown", "interrupted"}


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
    """Exact artifact registrations only. NEVER use a raw source's row/checksum."""
    from myUtils import jobs, publish_media_guard as guard
    records = []
    for artifact in payload.get("artifacts") or []:
        if not isinstance(artifact, dict) or guard.is_context_artifact(artifact):
            continue
        identifier = artifact.get("id", artifact.get("artifact_id"))
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
        from myUtils.worker import _resolve_media_path
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
    """Bind only persisted executed byte facts, keeping approved metadata intact.

    Records come from collect_media_records under fresh SQL ownership guards,
    including the atomic install transaction. Plans cannot supply these records.
    """
    from myUtils import jobs, publish_media_guard as guard
    result = copy.deepcopy(payload)
    lookup = {row["id"]: row for row in records}
    for artifact in result.get("artifacts") or []:
        if not isinstance(artifact, dict) or guard.is_context_artifact(artifact):
            continue
        declared = media_integrity.expected_sha256(artifact)
        row = lookup.get(artifact.get("id", artifact.get("artifact_id")))
        if row:
            metadata = jobs._recovery_json_object(row["metadata_json"] or "{}")
            recorded = media_integrity.expected_sha256({"metadata": metadata})
            if declared and recorded and declared != recorded:
                raise ValueError("Approved and recorded selected-media checksums conflict")
            if recorded:
                artifact["sha256"] = recorded
                size = metadata.get("size_bytes")
                if type(size) is not int or size <= 0:
                    raise ValueError("Verified selected media has no recorded byte count")
                if artifact.get("size_bytes") is not None and artifact["size_bytes"] != size:
                    raise ValueError("Approved and recorded selected-media byte counts conflict")
                artifact["size_bytes"] = size
        media_integrity.expected_sha256(artifact, required=True)
    return result
