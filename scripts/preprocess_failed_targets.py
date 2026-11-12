#!/usr/bin/env python3
"""Prepare exact failed media, then atomically schedule every ordered part.

No flag: offline/read-only queue dry-run. --prepare: restore/encode/validate and
optionally save a private plan, with no queue writes. --apply: consume that plan
(or prepare first), revalidate state/hashes/slots, and queue all parts per target.
Neither phase publishes a post. An unsafe target cannot abort the other targets.
Pending targets are never selected automatically. --include-unstarted-pending
requires frozen --target-id/--selection-file IDs at preparation AND application;
already-valid media is left untouched, including its schedule and generation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from myUtils import jobs, profiles, platform_limits, media_compliance, publish_media_guard, backup_preparation, preparation_context
from myUtils.secret_redaction import is_secret_key, redact_text
from scripts import recover_failed_targets as recovery

MEDIA_FAILURE = re.compile(
    r"duration.{0,60}(?:exceeds|limit)|exceeds.{0,40}(?:MB|limit)|needs preprocessing|"
    r"requires \d+ ordered posts|video encoder|video.{0,30}codec|"
    r"unsupported_format|file_too_large|video_too_large|invalid_(?:duration|frame_rate|bit_rate|aspect_ratio)", re.I)
RUNTIME_FIELDS = {"_db_path", "_delivery", "_save_checkpoint", "_telegramDeliveryKey", "_fallback_attempted"}
# Refreshes during a long encode are not a changed publishing intent. AFTER
# planning the full config is fingerprinted, so a later token change is stale.
_REFRESH_FIELDS = {"accesstokenexpiresat", "tokenexpiresat", "lastautorefreshat", "lastrefreshat",
                   "lastrefresherror", "reconnectalertedat", "tokenupdatedat", "refreshtokenexpiresat"}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _preparation_intent(state: dict) -> str:
    if state["target"]["status"] == "pending":
        return _digest(state)  # No account/intent changes are tolerated for pending repair.
    account = dict(state["account"])
    config = jobs._recovery_json_object(account.pop("config_json"))
    account["config_policy"] = {k: v for k, v in config.items()
        if not is_secret_key(k) and re.sub(r"[^a-z0-9]", "", k.lower()) not in _REFRESH_FIELDS
        and not k.startswith("_maintenance")}
    return _digest({**state, "account": account})


def _state(db_path: Path, target_id: int, resolved: bool, *, unstarted_pending: bool = False) -> dict:
    with recovery._connect(db_path) as conn:
        conn.execute("BEGIN")
        return recovery._snapshot(conn, target_id, resolved=resolved, unstarted_pending=unstarted_pending)


def _fresh_backup_context(state: dict, expected: dict, db_path: Path) -> dict:
    account = profiles._row_to_account(state["account"])
    settings = profiles.get_profile(account.profile_id, db_path=db_path).settings
    current = backup_preparation.resolve_constraints(platform=state["job"]["platform"], account=account,
                                                    settings=settings)
    if _digest(current) != _digest(expected):
        raise recovery.RecoveryRefused("Backup destination or effective preparation constraints changed; regenerate")
    return current


def _fresh_account_limits(state: dict, expected: dict) -> dict:
    current = preparation_context.account_constraints(profiles._row_to_account(state["account"]))
    if _digest(current["video_limits"]) != _digest(expected["video_limits"]):
        raise recovery.RecoveryRefused("Fresh account preparation constraints changed; regenerate")
    return current


def _fingerprint(state: dict, parts: list[dict], slots: list[str], cap: int, backup: bool,
                 backup_context: dict | None = None, account_constraints: dict | None = None,
                 pending_guard: dict | None = None) -> str:
    cfg = profiles._row_to_account(state["account"]).config
    pending = {"pending_guard": pending_guard} if pending_guard is not None else {}
    return _digest({"state": state, "parts": parts, "slots": slots, "cap": cap, "backup": backup, **pending,
        "policy_version": platform_limits.PREPARATION_POLICY_VERSION, "backup_context": backup_context,
        "account_constraints": account_constraints,
        "limits": media_compliance.effective_limits(state["job"]["platform"], account_config=cfg,
                                                      backup_compatible=backup)})


def _validate_prepared(parts: list[dict], state: dict, backup: bool, context: dict | None = None) -> None:
    """Run final probe/validation OUTSIDE a queue write transaction."""
    cfg = profiles._row_to_account(state["account"]).config
    if backup and not context:
        raise recovery.RecoveryRefused("Backup preparation needs fresh destination/constraint evidence")
    for part in parts:
        validated = publish_media_guard.prepare_payloads(part, platform=state["job"]["platform"],
            account_config=cfg, backup_compatible=backup, allow_reprepare=False,
            video_limits=(context or {}).get("video_limits"), image_limits=(context or {}).get("image_limits"))
        if len(validated) != 1 or validated[0] != part:
            raise recovery.RecoveryRefused("Prepared output changed during final validation")
        if context:
            backup_preparation.validate_intent(part, context=context, account_config=cfg)


def _pending_guard(item: dict) -> dict | None:
    if item.get("original_status") != "pending":
        return None
    return {key: item[key] for key in ("include_unstarted_pending", "original_status",
        "original_state_identity", "selected_target_ids", "original_media")} | {
        "min_gap_minutes": max(30, recovery.publish_orchestrator.MIN_GAP_MINUTES)}


def _pending_media(payload: dict) -> list[dict]:
    """Pin the exact restored inputs, including covers, without changing the payload."""
    result = []
    for artifact in payload.get("artifacts") or []:
        path = publish_media_guard._local_source(artifact)
        expected = artifact.get("sha256") or (artifact.get("metadata") or {}).get("sha256")
        if expected is not None:
            media_compliance.verify_checksum(path, expected)
        result.append({"local_path": str(path), "sha256": media_compliance.sha256_file(path)})
    return result


def _check_pending_media(original_media: list[dict]) -> None:
    for original in original_media:
        try:
            media_compliance.verify_checksum(original["local_path"], original["sha256"])
        except media_compliance.MediaPreparationError:
            raise recovery.RecoveryRefused("Original pending media changed or is unavailable; regenerate") from None


def _validate_existing_pending(payload: dict, state: dict, backup: bool, context: dict | None,
                               video_limits: dict) -> None:
    """Validation/probing only. Valid raw or prepared media must NOT get a generation."""
    cfg = profiles._row_to_account(state["account"]).config
    platform = state["job"]["platform"]
    working = publish_media_guard._working_payload(payload, platform)
    working["_preserveApprovedText"] = True
    publish_media_guard.validate_payload(working, platform=platform, account_config=cfg,
                                         backup_compatible=backup)
    for artifact in working["artifacts"]:
        kind = publish_media_guard.media_kind(artifact)
        if kind:
            publish_media_guard._validate_artifact(artifact, platform, cfg, backup,
                (context or {}).get("image_limits") if kind == "image" else video_limits)
        elif publish_media_guard.is_support_artifact(artifact):
            publish_media_guard._local_source(artifact)
    if context:
        backup_preparation.validate_intent(working, context=context, account_config=cfg)


def prepare_plan(*, db_path: Path, target_ids: list[int], per_day_cap: int | None = None,
                 out_dir: Path | None = None, now=None, resolved_target_ids=None,
                 backup_account_ids=None, isolate_failures: bool = False,
                 include_unstarted_pending: bool = False) -> list[dict]:
    """Finish preprocessing before allocating slots; pending opt-in requires frozen IDs."""
    if type(include_unstarted_pending) is not bool:
        raise ValueError("include_unstarted_pending must be a boolean")
    selected = sorted(recovery._ids(target_ids, "target_ids"))
    if include_unstarted_pending and (target_ids is None or not selected):
        raise ValueError("Pending preprocessing requires an explicit nonempty frozen target selection")
    resolved = recovery._ids(resolved_target_ids, "resolved_target_ids")
    backup_ids = recovery._ids(backup_account_ids, "backup_account_ids")
    cap = recovery.effective_daily_cap(per_day_cap)
    prepared, intents, backups, contexts, backup_exceptions, native_limits = {}, {}, {}, {}, {}, {}
    pending_ids, unchanged, original_media = set(), set(), {}
    skipped = Counter()
    for target_id in selected:
        try:
            with recovery._connect(db_path) as conn:
                conn.execute("BEGIN")
                row = conn.execute("SELECT status,last_error FROM publish_job_targets WHERE id=?", (target_id,)).fetchone()
                if row is None:
                    raise ValueError("Selected target does not exist")
                media_failure = bool(MEDIA_FAILURE.search(row["last_error"] or ""))
                unstarted = include_unstarted_pending and row["status"] == "pending"
                state = recovery._snapshot(conn, target_id, resolved=media_failure or target_id in resolved,
                                           unstarted_pending=unstarted)
            intent = _preparation_intent(state)
            payload = jobs._recovery_json_object(state["job"]["payload_json"])
            payload.update(jobs._recovery_json_object(state["target"]["checkpoint_json"]).get("payload") or {})
            working = copy.deepcopy(payload)
            account = profiles.get_account(state["account"]["id"], db_path=db_path)
            backup = account.id in backup_ids or state["policy"]["provider"] == "sociamonials"
            from myUtils.worker import _ensure_artifact_paths_local, _resolve_file_path
            if not working.get("artifacts") and not state["target"]["file_ref"].startswith("campaign_post:"):
                source = _resolve_file_path(state["target"]["file_ref"], db_path=db_path)
                working["artifacts"] = [{"local_path": str(source)}]
            if working.get("artifacts"):
                working["artifacts"] = [a for a in working["artifacts"]
                    if isinstance(a, dict) and not publish_media_guard.is_context_artifact(a)]
                if not working["artifacts"]:
                    raise media_compliance.MediaPreparationError("Only internal support artifacts were supplied")
                _ensure_artifact_paths_local(working, db_path=db_path)
            working = {key: value for key, value in working.items() if key not in RUNTIME_FIELDS}
            context = None
            if backup:
                try:
                    settings = profiles.get_profile(account.profile_id, db_path=db_path).settings
                    context = backup_preparation.resolve_constraints(platform=state["job"]["platform"],
                                                                     account=account, payload=working, settings=settings)
                    backup_preparation.validate_intent(working, context=context, account_config=account.config,
                                                      prepared=False)
                except Exception as exc:
                    if state["policy"]["provider"] == "sociamonials":
                        raise
                    # An unavailable optional backup is not permission to change
                    # approved native intent; use the native-only profile instead.
                    backup, context = False, None
                    backup_exceptions[target_id] = redact_text(str(exc))[:200]
            native = preparation_context.account_constraints(account)
            video_limits = media_compliance.tighten_limits(native["video_limits"], (context or {}).get("video_limits"))
            if unstarted:
                if target_id in resolved:
                    raise recovery.RecoveryRefused("Pending preprocessing cannot use a resolved-failure override")
                original_media[target_id] = _pending_media(working)
                try:
                    _validate_existing_pending(working, state, backup, context, video_limits)
                except media_compliance.MediaPreparationError:
                    pass  # Only invalid media reaches the existing complete preparation pipeline.
                else:
                    fresh = _state(db_path, target_id, False, unstarted_pending=True)
                    _check_pending_media(original_media[target_id])
                    if _preparation_intent(fresh) != intent:
                        raise recovery.RecoveryRefused("Publishing intent changed during pending validation")
                    unchanged.add(target_id)
                    continue
                pending_ids.add(target_id)
            parts = publish_media_guard.prepare_payloads(working, platform=state["job"]["platform"],
                account_config=account.config, out_dir=out_dir, backup_compatible=backup, allow_reprepare=True,
                video_limits=video_limits, image_limits=(context or {}).get("image_limits"))
            fresh = _state(db_path, target_id, state["resolved"], unstarted_pending=unstarted)
            if unstarted:
                _check_pending_media(original_media[target_id])
            # Covers remain support, not extra images; hash-pin their exact
            # restored bytes before the manifest reaches the write transaction.
            for part in parts:
                for artifact in part.get("artifacts") or []:
                    if publish_media_guard.is_support_artifact(artifact):
                        path = Path(str(artifact.get("local_path") or ""))
                        if not path.is_file():
                            raise media_compliance.MediaPreparationError("Exact cover is unavailable")
                        metadata = dict(artifact.get("metadata") or {})
                        metadata["sha256"] = media_compliance.sha256_file(path)
                        artifact["metadata"] = metadata
            if _preparation_intent(fresh) != intent:
                raise recovery.RecoveryRefused("Publishing intent changed during preprocessing")
            _validate_prepared(parts, fresh, backup, context)
            prepared[target_id], intents[target_id], backups[target_id], contexts[target_id] = parts, intent, backup, context
            native_limits[target_id] = native
            if media_failure:
                resolved.add(target_id)
        except Exception as exc:
            if not isolate_failures:
                raise
            reason = str(exc) if isinstance(exc, recovery.RecoveryRefused) else f"media preparation failed: {type(exc).__name__}"
            skipped[redact_text(reason)[:200]] += 1
    # Recheck all targets after preparation. No old credential/config snapshot
    # is carried across the long encode phase into the queue write transaction.
    for target_id in list(prepared):
        try:
            fresh = _state(db_path, target_id, target_id in resolved, unstarted_pending=target_id in pending_ids)
            if target_id in pending_ids:
                _check_pending_media(original_media[target_id])
            if _preparation_intent(fresh) != intents[target_id]:
                raise recovery.RecoveryRefused("Publishing intent changed before slot allocation")
            native_limits[target_id] = _fresh_account_limits(fresh, native_limits[target_id])
            if backups[target_id]:
                contexts[target_id] = _fresh_backup_context(fresh, contexts[target_id], db_path)
            _validate_prepared(prepared[target_id], fresh, backups[target_id], contexts[target_id])
        except Exception as exc:
            if not isolate_failures:
                raise
            skipped[redact_text(str(exc))[:200] if isinstance(exc, recovery.RecoveryRefused)
                    else f"final media validation failed: {type(exc).__name__}"] += 1
            del prepared[target_id]
    if not prepared:
        return ([{"_skipped": dict(skipped)}] if skipped else []) + (
            [{"_unchanged_pending": sorted(unchanged)}] if unchanged else [])
    plan = recovery.plan_recovery(db_path=db_path, target_ids=list(prepared),
                                  resolved_target_ids=resolved & prepared.keys(), per_day_cap=cap, now=now,
                                  unstarted_pending_target_ids=pending_ids & prepared.keys())
    with recovery._connect(db_path) as conn:
        conn.execute("BEGIN")
        aliases = recovery._account_refs(conn)
        booked = recovery._booked_slots(conn, aliases, now=now, replaced_unstarted_target_ids=[
            item["target_id"] for item in plan if item.get("original_status") == "pending"])
        # Reserve ALL first slots before allocating any target's tail parts.
        for item in plan:
            if "_skipped" not in item:
                identity = recovery._identity_for_ref(aliases, item["account_ref"])
                booked.setdefault(identity, []).append(item["slot"])
        for item in plan:
            if "_skipped" in item:
                continue
            unstarted = item.get("original_status") == "pending"
            state = recovery._snapshot(conn, item["target_id"], resolved=item["resolved_permanent"],
                                       unstarted_pending=unstarted)
            if _preparation_intent(state) != intents[item["target_id"]]:
                raise recovery.RecoveryRefused("Publishing intent changed during allocation; regenerate")
            parts = prepared[item["target_id"]]
            slots = [item["slot"]]
            account_id = state["account"]["id"]
            for _ in parts[1:]:
                gap = max(30, recovery.publish_orchestrator.MIN_GAP_MINUTES) if unstarted else recovery.publish_orchestrator.MIN_GAP_MINUTES
                base = jobs._utc_datetime(slots[-1]) + timedelta(minutes=gap)
                slot = recovery.publish_orchestrator._next_free_slot(
                    account_id, base, 0, {account_id: booked.setdefault(state["identity"], [])},
                    max_per_day=item["per_day_cap"], min_gap_minutes=30 if unstarted else None)
                slots.append(slot.isoformat(timespec="seconds"))
            backup = backups[item["target_id"]]
            item.update(prepared_payloads=parts, part_schedule_ats=slots, backup_compatible=backup,
                        backup_context=contexts[item["target_id"]], account_constraints=native_limits[item["target_id"]])
            if unstarted:
                item.update(selected_target_ids=selected, original_media=original_media[item["target_id"]])
            if item["target_id"] in backup_exceptions:
                item["backup_unavailable_reason"] = backup_exceptions[item["target_id"]]
            item["expected_fingerprint"] = _fingerprint(state, parts, slots, item["per_day_cap"], backup,
                contexts[item["target_id"]], native_limits[item["target_id"]], _pending_guard(item))
    if skipped:
        plan.append({"_skipped": dict(skipped)})
    if unchanged:
        plan.append({"_unchanged_pending": sorted(unchanged)})
    return plan


def _revalidate_prepared(conn, item: dict) -> str:
    """Only SQL, policy calculation and hash checks; never encode or probe here."""
    if item["per_day_cap"] != recovery.effective_daily_cap(item["per_day_cap"]):
        raise recovery.RecoveryRefused("Recovery would raise the configured pacing cap")
    unstarted = item.get("original_status") == "pending"
    state = recovery._snapshot(conn, item["target_id"], resolved=item["resolved_permanent"],
                               unstarted_pending=unstarted)
    if unstarted:
        if jobs._unstarted_pending_identity(state["target"], state["job"]["payload_json"]) != item["original_state_identity"]:
            raise recovery.RecoveryRefused("Original pending state changed; regenerate the frozen plan")
        _check_pending_media(item["original_media"])
    aliases = recovery._account_refs(conn)
    booked = recovery._booked_slots(conn, aliases,
        replaced_unstarted_target_ids=[item["target_id"]] if unstarted else None)
    account_id = state["account"]["id"]
    for slot_text in item["part_schedule_ats"]:
        slot = jobs._utc_datetime(slot_text)
        taken = booked.setdefault(state["identity"], [])
        if unstarted:
            available = slot if recovery._reserve_pending_slot(taken, slot, item["per_day_cap"]) else None
        else:
            available = recovery.publish_orchestrator._next_free_slot(
                account_id, slot, 0, {account_id: taken}, max_per_day=item["per_day_cap"])
        if available != slot:
            raise recovery.RecoveryRefused("Prepared recovery slot is no longer free")
    from myUtils.prepared_recovery import validate_parts
    validate_parts(conn, state["target"], item["prepared_payloads"], item["part_schedule_ats"])
    return _fingerprint(state, item["prepared_payloads"], item["part_schedule_ats"],
        item["per_day_cap"], item["backup_compatible"], item.get("backup_context"), item.get("account_constraints"),
        _pending_guard(item))


def _validate_prepared_plan(plan: list[dict], *, include_unstarted_pending: bool,
                            target_ids=None) -> tuple[list[dict], set[int]]:
    if type(include_unstarted_pending) is not bool:
        raise ValueError("include_unstarted_pending must be a boolean")
    if not isinstance(plan, list) or any(not isinstance(item, dict) for item in plan):
        raise ValueError("plan must be a list of prepared recovery entries")
    entries = [item for item in plan if "_unchanged_pending" not in item]
    recovery._validate_plan(entries)
    unchanged = set()
    for item in plan:
        if "_unchanged_pending" in item:
            if set(item) != {"_unchanged_pending"} or not isinstance(item["_unchanged_pending"], list):
                raise ValueError("Invalid unchanged-pending summary")
            ids = recovery._ids(item["_unchanged_pending"], "unchanged_pending_target_ids")
            if len(ids) != len(item["_unchanged_pending"]) or unchanged & ids:
                raise ValueError("Duplicate unchanged pending target IDs")
            unchanged.update(ids)
    selected = recovery._ids(target_ids, "target_ids")
    pending = [item for item in entries if item.get("original_status") == "pending"]
    if pending or unchanged or include_unstarted_pending:
        if not include_unstarted_pending or target_ids is None or not selected:
            raise ValueError("Pending preprocessing application requires explicit opt-in and the frozen target selection")
        if unchanged - selected:
            raise ValueError("Unchanged pending targets are outside the frozen selection")
    if unchanged & {item["target_id"] for item in entries if "_skipped" not in item}:
        raise ValueError("An unchanged pending target cannot also be queued")
    for item in entries:
        if "_skipped" in item:
            continue
        if not isinstance(item.get("original_status", "failed"), str) or item.get("original_status", "failed") not in {"pending", "failed"}:
            raise ValueError("Invalid original target status")
        if item.get("include_unstarted_pending", False) is not False and item.get("original_status") != "pending":
            raise ValueError("Pending opt-in requires the original pending identity")
    for item in pending:
        frozen = item.get("selected_target_ids")
        media = item.get("original_media")
        if (item.get("include_unstarted_pending") is not True or item["resolved_permanent"] is not False
                or not isinstance(frozen, list) or frozen != sorted(recovery._ids(frozen, "selected_target_ids"))
                or frozen != sorted(selected)
                or item["target_id"] not in selected
                or not isinstance(item.get("original_state_identity"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["original_state_identity"])
                or type(item["per_day_cap"]) is not int or not 1 <= item["per_day_cap"] <= 3
                or not isinstance(media, list) or not 1 <= len(media) <= media_compliance.MAX_PARTS):
            raise ValueError("Invalid frozen pending preprocessing guard; regenerate")
        for original in media:
            if (not isinstance(original, dict) or set(original) != {"local_path", "sha256"}
                    or not isinstance(original["local_path"], str) or not original["local_path"].strip()
                    or not isinstance(original["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", original["sha256"])):
                raise ValueError("Invalid original pending media identity")
    return entries, unchanged


def apply_plan(plan: list[dict], *, db_path: Path, include_unstarted_pending: bool = False,
               target_ids=None) -> dict:
    entries, _unchanged = _validate_prepared_plan(plan,
        include_unstarted_pending=include_unstarted_pending, target_ids=target_ids)
    for item in entries:
        if "_skipped" in item:
            continue
        if (type(item.get("backup_compatible")) is not bool
                or (item.get("backup_compatible") is True and not isinstance(item.get("backup_context"), dict))
                or not isinstance(item.get("account_constraints"), dict)
                or not isinstance(item.get("prepared_payloads"), list)
                or not 1 <= len(item["prepared_payloads"]) <= media_compliance.MAX_PARTS
                or not isinstance(item.get("part_schedule_ats"), list)
                or len(item["part_schedule_ats"]) != len(item["prepared_payloads"])):
            raise ValueError("Invalid prepared recovery plan; regenerate")
    applied, queued_parts, errors, queued = 0, 0, [], []
    for item in entries:
        if "_skipped" in item:
            continue
        try:
            unstarted = item.get("original_status") == "pending"
            state = _state(db_path, item["target_id"], item["resolved_permanent"], unstarted_pending=unstarted)
            _fresh_account_limits(state, item["account_constraints"])
            if item["backup_compatible"]:
                _fresh_backup_context(state, item["backup_context"], db_path)
            with recovery.publish_orchestrator.slot_reservation_lock(db_path):
                options = {"db_path": db_path, "expected_fingerprint": item["expected_fingerprint"],
                    "revalidate": lambda conn, item=item: _revalidate_prepared(conn, item),
                    "prepared_payloads": item["prepared_payloads"], "part_schedule_ats": item["part_schedule_ats"]}
                if unstarted:
                    result = jobs.repair_unstarted_pending_target(item["target_id"], item["slot"],
                        include_unstarted_pending=include_unstarted_pending,
                        original_state_identity=item["original_state_identity"], **options)
                else:
                    result = jobs.reschedule_failed_target(item["target_id"], item["slot"], **options)
            applied += 1
            queued_parts += len(item["prepared_payloads"])
            queued.append(result.id)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, recovery.RecoveryRefused) else "Changed state, media or slot; keep original evidence and regenerate"
            errors.append({"target_id": item["target_id"], "reason": redact_text(reason)[:200]})
    # Keep the established API shape; exact queued IDs are available in the DB.
    return {"applied": applied, "queued_parts": queued_parts, "errors": errors}


def _write_private(path: Path, data: dict) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=True, indent=2, allow_nan=False)
        stream.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=recovery.DB_PATH)
    parser.add_argument("--target-id", type=int, action="append")
    parser.add_argument("--selection-file", type=Path, help="Private frozen inventory with target_ids")
    parser.add_argument("--include-unstarted-pending", action="store_true",
                        help="Repair only explicitly selected zero-attempt unstarted pending targets; valid media is unchanged")
    parser.add_argument("--resolved-target-id", type=int, action="append")
    parser.add_argument("--backup-account-id", type=int, action="append", help="Currently verified backup destination")
    parser.add_argument("--lookback-days", type=int, default=2)
    parser.add_argument("--per-day-cap", type=int, default=None)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--plan-file", type=Path, help="Write with --prepare; read with --apply")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        selected = args.target_id
        if args.selection_file:
            frozen = jobs._recovery_json_object(args.selection_file.read_text(encoding="utf-8"))
            selected = sorted(recovery._ids(frozen.get("target_ids"), "target_ids") | recovery._ids(selected, "target_ids"))
        if args.include_unstarted_pending:
            if not (args.target_id or args.selection_file) or not recovery._ids(selected, "target_ids"):
                raise ValueError("--include-unstarted-pending requires nonempty --target-id or --selection-file IDs")
            if not args.prepare and not args.apply:
                raise ValueError("Pending preprocessing requires --prepare or --apply; default dry-run remains failed-only")
        if not args.prepare and not args.apply:
            options = ["--db-path", str(args.db_path), "--lookback-days", str(args.lookback_days)]
            if args.per_day_cap is not None:
                options += ["--per-day-cap", str(args.per_day_cap)]
            options += [arg for target in selected or [] for arg in ("--target-id", str(target))]
            options += [arg for target in args.resolved_target_id or [] for arg in ("--resolved-target-id", str(target))]
            return recovery.main(options)
        if args.apply and args.plan_file:
            document = jobs._recovery_json_object(args.plan_file.read_text(encoding="utf-8"))
            if document.get("version") != 1:
                raise ValueError("Unsupported prepared recovery manifest")
            saved_opt_in = document.get("include_unstarted_pending", False)
            if type(saved_opt_in) is not bool or saved_opt_in != args.include_unstarted_pending:
                raise ValueError("Saved pending opt-in does not match application flags; regenerate or use the original opt-in")
            if saved_opt_in and recovery._ids(selected, "target_ids") != recovery._ids(document.get("selected_target_ids"), "selected_target_ids"):
                raise ValueError("Application selection does not match the frozen pending plan")
            plan = document["plan"]
            selected = document.get("selected_target_ids", [])
        else:
            if selected is None:
                recovery._integer(args.lookback_days, "lookback_days")
                with recovery._connect(args.db_path) as conn:
                    selected = [row[0] for row in conn.execute(
                        "SELECT id FROM publish_job_targets WHERE status='failed' "
                        "AND julianday(finished_at)>=julianday('now',?) ORDER BY finished_at,id",
                        (f"-{args.lookback_days} days",))]
            plan = prepare_plan(db_path=args.db_path, target_ids=selected,
                per_day_cap=args.per_day_cap, out_dir=args.out_dir,
                resolved_target_ids=args.resolved_target_id, backup_account_ids=args.backup_account_id,
                isolate_failures=True, include_unstarted_pending=args.include_unstarted_pending)
            if args.plan_file:
                _write_private(args.plan_file, {"version": 1, "include_unstarted_pending": args.include_unstarted_pending,
                    "selected_target_ids": sorted(recovery._ids(selected, "target_ids")), "plan": plan})
        entries, unchanged = _validate_prepared_plan(plan,
            include_unstarted_pending=args.include_unstarted_pending, target_ids=selected)
        eligible = {item["target_id"] for item in entries if "_skipped" not in item}
        summary = {"mode": "apply" if args.apply else "prepare", "prepared_targets": len(eligible),
                   "parts": sum(len(item.get("prepared_payloads", [])) for item in plan),
                   "unchanged_pending_target_ids": sorted(unchanged),
                   "refused_targets": sorted(set(selected or []) - eligible - unchanged),
                   "skipped": [item["_skipped"] for item in plan if "_skipped" in item]}
        if args.apply:
            summary.update(apply_plan(plan, db_path=args.db_path,
                include_unstarted_pending=args.include_unstarted_pending, target_ids=selected))
        print(json.dumps(summary, ensure_ascii=True))
        return 1 if summary.get("errors") or summary["refused_targets"] else 0
    except (ValueError, OSError, KeyError) as exc:
        parser.error(redact_text(str(exc))[:300])


if __name__ == "__main__":
    raise SystemExit(main())
