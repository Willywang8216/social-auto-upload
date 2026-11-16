#!/usr/bin/env python3
"""Explicitly reviewed recovery of complete failed/prerequisite publication groups.

Requires --review-resolved-prerequisites AND frozen --target-id values in BOTH
phases. A saved manifest never supplies that authority. Preparation restores only
registered selected media, validates current destinations/grants/options and
encodes every exact selected interval. Apply revalidates under the same SQLite
write transaction, preserves counters/history, and installs confirmed-delivery
predecessor links. Neither command itself submits a provider publication.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import timedelta
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from myUtils import (jobs, profiles, media_compliance, publish_media_guard as guard,
                     backup_preparation, preparation_context, publish_admission, reviewed_recovery)
from myUtils.secret_redaction import redact_text
from scripts import recover_failed_targets as recovery, preprocess_failed_targets as prep


def _selection(target_ids, reviewed: bool) -> list[int]:
    if reviewed is not True or target_ids is None:
        raise ValueError("Explicit prerequisite review and frozen target IDs are required at execution")
    ids = sorted(recovery._ids(target_ids, "target_ids"))
    if not ids or len(ids) > media_compliance.MAX_PARTS:
        raise ValueError("Reviewed recovery needs a bounded nonempty explicit target selection")
    return ids


def _payload(state) -> dict:
    value = jobs._recovery_json_object(state["job"]["payload_json"])
    override = jobs._recovery_json_object(state["target"]["checkpoint_json"]).get("payload", {})
    if not isinstance(override, dict):
        raise recovery.RecoveryRefused("Invalid selected payload progress")
    return {**value, **override}


def _part_key(target, job, payload, identity):
    # Immutable original job labels take precedence over an older checkpoint's
    # local re-encode label (e.g. original part 1/2 re-encoded to local 1/1).
    original = jobs._recovery_json_object(job["payload_json"])
    label = original.get("publicationGroup") or {}
    index = original.get("partIndex", payload.get("partIndex", 1))
    count = original.get("partCount", payload.get("partCount", 1))
    if label:
        if not isinstance(label, dict) or not isinstance(label.get("id"), str) or not label["id"]:
            raise recovery.RecoveryRefused("Invalid declared publication group")
        index, count = label.get("index"), label.get("count")
    if type(index) is not int or type(count) is not int or not 1 <= index <= count <= media_compliance.MAX_PARTS:
        raise recovery.RecoveryRefused("Invalid original ordered part membership")
    if count == 1:
        return ("single", target["id"]), index, count
    sources = sorted({a.get("source_file_record_id") for a in original.get("artifacts") or []
                      if isinstance(a, dict) and type(a.get("source_file_record_id")) is int})
    if label:
        key = ("declared", identity, job["platform"], label["id"])
    elif type(original.get("campaignId")) is int and sources:
        key = ("legacy", identity, job["platform"], original["campaignId"], tuple(sources), count)
    else:
        raise recovery.RecoveryRefused("Ordered media has no exact parent group identity")
    return key, index, count


def _snapshot(conn, target_id: int, selected: list[int]) -> dict:
    if target_id not in selected:
        raise recovery.RecoveryRefused("Target is outside the explicit frozen review")
    row = conn.execute("SELECT status FROM publish_job_targets WHERE id=?", (target_id,)).fetchone()
    if row is None:
        raise recovery.RecoveryRefused("Selected target disappeared")
    state = recovery._snapshot(conn, target_id, resolved=True,
                               reviewed_prerequisite=row["status"] == "blocked")
    reviewed_recovery.assert_budget(jobs._recovery_json_object(state["target"]["checkpoint_json"]))
    key, index, count = _part_key(state["target"], state["job"], _payload(state), state["identity"])
    members = [(index, target_id, jobs._unstarted_pending_identity(state["target"], state["job"]["payload_json"]))]
    if count > 1:
        members = []
        aliases = recovery._account_refs(conn)
        for ref in aliases.get(state["identity"], [state["target"]["account_ref"]]):
            for target in conn.execute("SELECT * FROM publish_job_targets WHERE account_ref=? ORDER BY id", (ref,)):
                job = conn.execute("SELECT * FROM publish_jobs WHERE id=?", (target["job_id"],)).fetchone()
                if job is None:
                    raise recovery.RecoveryRefused("Unreadable same-destination ordered history")
                root = jobs._recovery_json_object(job["payload_json"])
                own = jobs._recovery_json_object(state["job"]["payload_json"])
                if job["platform"] != state["job"]["platform"]:
                    continue
                if key[0] == "legacy" and root.get("campaignId") != own.get("campaignId"):
                    continue
                if key[0] == "declared" and (root.get("publicationGroup") or {}).get("id") != key[-1]:
                    continue
                candidate_key, position, size = _part_key(target, job, {}, state["identity"])
                if candidate_key != key:
                    continue
                if size != count or target["id"] not in selected:
                    raise recovery.RecoveryRefused("Complete ordered recovery requires explicit review of its exact siblings")
                members.append((position, target["id"], jobs._unstarted_pending_identity(target, job["payload_json"])))
        members.sort()
        if [part[0] for part in members] != list(range(1, count + 1)):
            raise recovery.RecoveryRefused("Original group has missing, duplicated or inconsistent ordered members")
    state["review"] = {"selected_target_ids": selected, "group_key": list(key),
                       "index": index, "count": count, "members": members}
    state["media_records"] = reviewed_recovery.collect_media_records(conn, _payload(state))
    return state


def _state(db_path: Path, target_id: int, selected: list[int]) -> dict:
    with recovery._connect(db_path) as conn:
        conn.execute("BEGIN")
        return _snapshot(conn, target_id, selected)


def _fresh_context(state, db_path: Path):
    account = profiles._row_to_account(state["account"])
    settings = profiles._row_to_profile(state["profile"]).settings
    backup = state["policy"]["provider"] == "sociamonials"
    context = None
    if backup:
        context = backup_preparation.resolve_constraints(platform=state["job"]["platform"],
            account=account, payload=_payload(state), settings=settings, db_path=db_path)
        backup_preparation.validate_intent(_payload(state), context=context, account_config=account.config, prepared=False)
    else:
        preparation_context.verify_native_destination(account, settings=settings, db_path=db_path)
    publish_admission.validate_intent(_payload(state), platform=state["job"]["platform"],
        effective_provider=state["policy"]["provider"], account_config=account.config)
    native = preparation_context.account_constraints(account)
    return backup, context, native


def _guard(state, original_media):
    return {"review": state["review"], "original_media": original_media,
            "original_status": state["target"]["status"]}


def _fingerprint(state, item):
    return prep._fingerprint(state, item["prepared_payloads"], item["part_schedule_ats"], item["per_day_cap"],
        item["backup_compatible"], item["backup_context"], item["account_constraints"],
        _guard(state, item["original_media"]))


def _group_fingerprint(entries):
    return prep._digest([(item["target_id"], item["expected_fingerprint"]) for item in entries])


def _intent(state):
    # Restoration may add executed SHA/size facts to the exact artifact row.
    # collect_media_records still rejects ALL other identity/provenance changes;
    # the full records are pinned after preparation by the final fingerprint.
    return prep._preparation_intent({k: v for k, v in state.items() if k != "media_records"})


def prepare_plan(*, db_path: Path, target_ids, review_resolved_prerequisites: bool = False,
                 out_dir: Path | None = None, per_day_cap: int | None = None, now=None) -> list[dict]:
    selected = _selection(target_ids, review_resolved_prerequisites)
    cap = min(recovery.effective_daily_cap(per_day_cap) or 3, 3)
    prepared, intents = {}, {}
    # Entire source groups are validated BEFORE any encode/restore. No sibling
    # is inferred from a filename or silently omitted from the review boundary.
    states = {target_id: _state(db_path, target_id, selected) for target_id in selected}
    for target_id in selected:
        state = states[target_id]
        intents[target_id] = _intent(state)
        working = copy.deepcopy(_payload(state))
        from myUtils.worker import _ensure_artifact_paths_local, _resolve_file_path
        if not working.get("artifacts") and not state["target"]["file_ref"].startswith("campaign_post:"):
            working["artifacts"] = [{"local_path": str(_resolve_file_path(state["target"]["file_ref"], db_path=db_path))}]
        working["artifacts"] = [a for a in working.get("artifacts") or []
                                if isinstance(a, dict) and not guard.is_context_artifact(a)]
        if not working["artifacts"]:
            raise recovery.RecoveryRefused("Reviewed recovery requires exact approved publishing media")
        _ensure_artifact_paths_local(working, db_path=db_path)
        restored_state = _state(db_path, target_id, selected)
        working = reviewed_recovery.bind_media_payload(_payload(restored_state), restored_state["media_records"])
        working["artifacts"] = [a for a in working.get("artifacts") or [] if not guard.is_context_artifact(a)]
        _ensure_artifact_paths_local(working, db_path=db_path)  # validation-only, with independent recorded proofs
        working = {k: v for k, v in working.items() if k not in prep.RUNTIME_FIELDS}
        original_media = prep._pending_media(working)
        backup, context, native = _fresh_context(state, db_path)
        account = profiles._row_to_account(state["account"])
        limits = media_compliance.tighten_limits(native["video_limits"], (context or {}).get("video_limits"))
        parts = guard.prepare_payloads(working, platform=state["job"]["platform"], account_config=account.config,
            out_dir=out_dir, backup_compatible=backup, allow_reprepare=True, reviewed_selected_content=True,
            video_limits=limits, image_limits=(context or {}).get("image_limits"))
        fresh = _state(db_path, target_id, selected)
        prep._check_pending_media(original_media)
        if _intent(fresh) != intents[target_id]:
            raise recovery.RecoveryRefused("Reviewed intent changed during preparation")
        prep._validate_prepared(parts, fresh, backup, context, reviewed_selected_content=True)
        prepared[target_id] = {"target_id": target_id, "job_id": state["target"]["job_id"],
            "account_ref": state["target"]["account_ref"], "platform": state["job"]["platform"],
            "original_status": state["target"]["status"], "prepared_payloads": parts, "original_media": original_media,
            "backup_compatible": backup, "backup_context": context, "account_constraints": native,
            "per_day_cap": cap, "selected_target_ids": selected,
            "group_target_ids": [member[1] for member in state["review"]["members"]]}
    # Fresh provider reads/decodes outside the write transaction, then hash-pin
    # all current SQL/config/membership/media facts for the atomic application.
    for target_id, item in prepared.items():
        fresh = _state(db_path, target_id, selected)
        if _intent(fresh) != intents[target_id]:
            raise recovery.RecoveryRefused("Reviewed intent changed before allocation")
        backup, context, native = _fresh_context(fresh, db_path)
        if backup != item["backup_compatible"] or prep._digest(context) != prep._digest(item["backup_context"]):
            raise recovery.RecoveryRefused("Reviewed destination or effective limits changed")
        if native["video_limits"] != item["account_constraints"]["video_limits"]:
            raise recovery.RecoveryRefused("Reviewed native limits changed")
        prep._check_pending_media(item["original_media"])
        prep._validate_prepared(item["prepared_payloads"], fresh, backup, context, reviewed_selected_content=True)
    current = jobs._utc_datetime(now if now is not None else jobs._now_iso())
    base = (current + timedelta(minutes=5)).replace(second=0, microsecond=0)
    ordered_groups = sorted({tuple(item["group_target_ids"]) for item in prepared.values()})
    result = []
    with recovery._connect(db_path) as conn:
        conn.execute("BEGIN")
        aliases = recovery._account_refs(conn)
        booked = recovery._booked_slots(conn, aliases, now=current)
        for group in ordered_groups:
            previous = base
            for target_id in group:
                item = prepared[target_id]
                state = _snapshot(conn, target_id, selected)
                if _intent(state) != intents[target_id]:
                    raise recovery.RecoveryRefused("Reviewed group changed during slot allocation")
                slots = []
                for _part in item["prepared_payloads"]:
                    slot = recovery.publish_orchestrator._next_free_slot(state["account"]["id"], previous, 0,
                        {state["account"]["id"]: booked.setdefault(state["identity"], [])}, max_per_day=cap,
                        min_gap_minutes=30, now_fn=lambda: current)
                    slots.append(slot.isoformat(timespec="seconds"))
                    previous = slot + timedelta(minutes=30)
                item["part_schedule_ats"] = slots
                item["expected_fingerprint"] = _fingerprint(state, item)
                result.append(item)
    return result


def _validate_plan(plan, selected):
    if not isinstance(plan, list) or not plan or any(not isinstance(item, dict) for item in plan):
        raise ValueError("A complete prepared reviewed plan is required")
    seen = set()
    for item in plan:
        target_id = item.get("target_id")
        if (type(target_id) is not int or target_id in seen or target_id not in selected
                or item.get("selected_target_ids") != selected
                or item.get("original_status") not in {"failed", "blocked"}
                or type(item.get("per_day_cap")) is not int or not 1 <= item["per_day_cap"] <= 3
                or type(item.get("backup_compatible")) is not bool
                or not isinstance(item.get("account_constraints"), dict)
                or not isinstance(item.get("original_media"), list)
                or not isinstance(item.get("group_target_ids"), list)
                or not item["group_target_ids"] or set(item["group_target_ids"]) - set(selected)
                or not isinstance(item.get("expected_fingerprint"), str)):
            raise ValueError("Invalid frozen reviewed recovery manifest")
        if (not re.fullmatch(r"[0-9a-f]{64}", item["expected_fingerprint"])
                or type(item.get("job_id")) is not int or item["job_id"] < 1
                or any(type(value) is not int or value < 1 for value in item["group_target_ids"])
                or not isinstance(item.get("prepared_payloads"), list)
                or not 1 <= len(item["prepared_payloads"]) <= media_compliance.MAX_PARTS
                or any(not isinstance(part, dict) for part in item["prepared_payloads"])
                or not isinstance(item.get("part_schedule_ats"), list)
                or len(item["part_schedule_ats"]) != len(item["prepared_payloads"])
                or not isinstance(item["account_constraints"].get("video_limits"), dict)
                or (item["backup_compatible"] and not isinstance(item.get("backup_context"), dict))
                or (not item["backup_compatible"] and item.get("backup_context") is not None)
                or not 1 <= len(item["original_media"]) <= media_compliance.MAX_PARTS):
            raise ValueError("Invalid complete prepared media, slots or fresh constraint evidence")
        for slot in item["part_schedule_ats"]:
            if not isinstance(slot, str):
                raise ValueError("Reviewed recovery slots must be ISO datetime strings")
            jobs._utc_datetime(slot)
        for original in item["original_media"]:
            if (not isinstance(original, dict) or set(original) != {"local_path", "sha256"}
                    or not isinstance(original["local_path"], str) or not original["local_path"].strip()
                    or not isinstance(original["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", original["sha256"])):
                raise ValueError("Invalid immutable original selected-media proof")
        seen.add(target_id)
    if seen != set(selected):
        raise ValueError("Prepared plan does not cover the complete explicit review selection")
    by_id = {item["target_id"]: item for item in plan}
    for item in plan:
        group = item["group_target_ids"]
        if len(group) != len(set(group)) or any(by_id[value]["group_target_ids"] != group for value in group):
            raise ValueError("Incomplete or inconsistent reviewed group")


def _revalidate_group(conn, entries, selected):
    aliases = recovery._account_refs(conn)
    booked = recovery._booked_slots(conn, aliases)
    fingerprints = []
    for item in entries:
        if item["per_day_cap"] != min(recovery.effective_daily_cap(item["per_day_cap"]) or 3, 3):
            raise recovery.RecoveryRefused("Reviewed plan would raise configured destination pacing")
        state = _snapshot(conn, item["target_id"], selected)
        if [member[1] for member in state["review"]["members"]] != item["group_target_ids"]:
            raise recovery.RecoveryRefused("Original ordered group changed")
        prep._check_pending_media(item["original_media"])
        for value in item["part_schedule_ats"]:
            slot = jobs._utc_datetime(value)
            if not recovery._reserve_pending_slot(booked.setdefault(state["identity"], []), slot, item["per_day_cap"]):
                raise recovery.RecoveryRefused("Reviewed future slot is no longer free")
        fingerprints.append((item["target_id"], _fingerprint(state, item)))
    return prep._digest(fingerprints)


def apply_plan(plan, *, db_path: Path, target_ids=None, review_resolved_prerequisites: bool = False):
    selected = _selection(target_ids, review_resolved_prerequisites)
    _validate_plan(plan, selected)
    by_id = {item["target_id"]: item for item in plan}
    applied, queued, errors = [], [], []
    for group in sorted({tuple(item["group_target_ids"]) for item in plan}):
        entries = [by_id[value] for value in group]
        try:
            for item in entries:
                state = _state(db_path, item["target_id"], selected)
                backup, context, native = _fresh_context(state, db_path)
                if (backup != item["backup_compatible"] or prep._digest(context) != prep._digest(item["backup_context"])
                        or native["video_limits"] != item["account_constraints"]["video_limits"]):
                    raise recovery.RecoveryRefused("Fresh reviewed destinations/grants or constraints changed")
                prep._check_pending_media(item["original_media"])
                prep._validate_prepared(item["prepared_payloads"], state, backup, context, reviewed_selected_content=True)
            with recovery.publish_orchestrator.slot_reservation_lock(db_path):
                installed = jobs.recover_reviewed_group(entries, reviewed_target_ids=selected,
                    expected_fingerprint=_group_fingerprint(entries),
                    revalidate=lambda conn: _revalidate_group(conn, entries, selected), db_path=db_path)
            applied.extend(group)
            queued.extend(target.id for target in installed)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, recovery.RecoveryRefused) else "Changed state, media, budget or slot; retain evidence and regenerate"
            errors.append({"target_ids": list(group), "reason": redact_text(reason)[:200]})
    return {"applied_target_ids": applied, "queued_target_ids": queued, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=recovery.DB_PATH)
    parser.add_argument("--target-id", action="append", type=int, required=True)
    parser.add_argument("--review-resolved-prerequisites", action="store_true")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--per-day-cap", type=int)
    parser.add_argument("--plan-file", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        selected = _selection(args.target_id, args.review_resolved_prerequisites)
        if args.prepare:
            plan = prepare_plan(db_path=args.db_path, target_ids=selected, review_resolved_prerequisites=True,
                                out_dir=args.out_dir, per_day_cap=args.per_day_cap)
            prep._write_private(args.plan_file, {"version": 1, "selected_target_ids": selected, "plan": plan})
            result = {"prepared_target_ids": selected, "parts": sum(len(item["prepared_payloads"]) for item in plan)}
        else:
            document = jobs._recovery_json_object(args.plan_file.read_text(encoding="utf-8"))
            if document.get("version") != 1 or document.get("selected_target_ids") != selected:
                raise ValueError("Application review does not match the frozen prepared selection")
            result = apply_plan(document["plan"], db_path=args.db_path, target_ids=selected,
                                review_resolved_prerequisites=True)
        print(json.dumps(result, ensure_ascii=True))
        return 1 if result.get("errors") else 0
    except (ValueError, OSError, KeyError) as exc:
        parser.error(redact_text(str(exc))[:300])


if __name__ == "__main__":
    raise SystemExit(main())
