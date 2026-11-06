#!/usr/bin/env python3
"""Plan guarded recovery of failed publish targets; dry-run is the default.

Recent transient failures are spaced with the Publish Center's allocator. Explicit
--target-id / --resolved-target-id selections cover all dates; the latter bypasses
ONLY the historical permanent-error regex, after the operator fixes its cause.
Receipts, partial submissions, retired Reddit, disabled routes, account/intent
mismatches and duplicate deliveries are NEVER overridden. This tool does not
reconcile delivery, inspect credentials, load .env, or contact providers.

Usage::

    python scripts/recover_failed_targets.py --lookback-days 2
    python scripts/recover_failed_targets.py --target-id 123 --resolved-target-id 456
    python scripts/recover_failed_targets.py --platform threads --limit 4 --apply

Plans contain opaque state fingerprints, not account configuration or raw provider
errors. Applying revalidates each target under the same write lock as its reset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myUtils import jobs as job_runtime  # noqa: E402
from myUtils import profiles, publish_orchestrator, sociamonials_fallback  # noqa: E402
from myUtils.account_identity import structured_account_id  # noqa: E402
from myUtils import destination_pacing  # noqa: E402

DB_PATH = Path(os.environ.get("SAU_DB_PATH") or (REPO_ROOT / "db" / "database.db"))

PERMANENT_ERROR_PATTERNS = re.compile(
    r"banned|missing|unsupported|no usable media|requires (an? )?(video|image)|"
    r"no_enabled_accounts|nsfw_no_adult_safe_account|restrict(ed|ion)|"
    r"not allowed|permission denied|forbidden|invalid_grant|"
    r"requires reconnection|reconnect|media\.write|"
    r"requires a local video|no images|SUBMIT_VALIDATION_LINK_WHITELIST|"
    r"\[content-guard\]|"
    r"duration\s+\d+s\s+exceeds|exceeds the\s+\d+\s*(?:s|MB)\s+limit|"
    r"require[s]? Direct Post",
    re.IGNORECASE,
)
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_JOB_PROJECTION = {"status", "total_targets", "completed_targets", "failed_targets", "finished_at"}
_LIVE_JOBS = {"failed", "pending", "running", "blocked", "awaiting_delivery", "needs_review"}


class RecoveryRefused(ValueError):
    """A fixed, credential-free refusal reason, safe for plan/CLI output."""


def _classify(last_error: str | None) -> str:
    if not last_error:
        return "unknown"
    return "permanent" if PERMANENT_ERROR_PATTERNS.search(last_error) else "transient"


def _integer(value, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError(f"{name} must be an integer between {minimum} and {2**63 - 1}")
    return value


def _ids(values: Iterable[int] | None, name: str) -> set[int]:
    if values is None:
        return set()
    if isinstance(values, (str, bytes, dict)):
        raise ValueError(f"{name} must be an iterable of positive target IDs")
    try:
        return {_integer(value, name) for value in values}
    except TypeError:
        raise ValueError(f"{name} must be an iterable of positive target IDs") from None


def _object(text: str, label: str) -> dict:
    try:
        return job_runtime._recovery_json_object(text)
    except ValueError:
        raise RecoveryRefused(f"invalid {label} metadata") from None


def _platform(value: str) -> str:
    text = str(value or "").strip().lower()
    return "twitter" if text == "x" else text


def _route_policy(platform: str, *, account=None, settings=None) -> dict:
    # Never restore a retired route. Managed publishing is allowed only through
    # an enabled, explicitly selected or already configured X fallback route.
    if platform == "reddit":
        raise RecoveryRefused("retired Reddit route")
    if (platform not in profiles.SUPPORTED_PLATFORMS
            or not profiles.platform_supports_direct_publish(platform)):
        raise RecoveryRefused("unsupported publish route")
    names = [f"SAU_{platform.upper()}_ENABLED", "SAU_SOCIAMONIALS_FALLBACK"]
    if platform == "twitter":
        names.append("SAU_X_DIRECT_PUBLISH")
    flags = {name: str(os.environ.get(name, "")).strip().lower() for name in names}
    if any(value and value not in _TRUE | _FALSE for value in flags.values()):
        raise RecoveryRefused("invalid publish route flag")
    if flags[names[0]] in _FALSE:
        raise RecoveryRefused("disabled publish route")
    try:
        managed = sociamonials_fallback.prefers_sociamonials(account, platform=platform)
    except sociamonials_fallback.SociamonialsNotConfigured:
        raise RecoveryRefused("invalid managed publishing route") from None
    if platform == "twitter":
        direct = flags["SAU_X_DIRECT_PUBLISH"]
        managed = managed or direct in _FALSE
    if managed:
        mapping = sociamonials_fallback.resolve_mapping(
            account.get("id") if account else None, account=account, platform=platform, settings=settings)
        if (flags["SAU_SOCIAMONIALS_FALLBACK"] not in _TRUE or not mapping
                or mapping["network"] != sociamonials_fallback._platform_network(platform)
                or len(mapping["profile_refs"]) != 1):
            raise RecoveryRefused("disabled or unconfigured managed publishing route")
        return {"flags": flags, "provider": "sociamonials", "mapping": mapping}
    return {"flags": flags, "provider": "direct"}


def _connect(db_path: Path):
    # Planning reads (including bookings) keep the caller's read-only snapshot.
    return destination_pacing.connect_readonly(db_path)


def _account_refs(conn) -> dict[str, list[str]]:
    """Compatibility wrapper: include current rows even without target history."""
    return destination_pacing.account_refs(conn)


def _identity_for_ref(aliases: dict[str, list[str]], account_ref: str) -> str:
    return destination_pacing.identity_for_ref(aliases, account_ref)


def _booked_slots(
    conn, aliases: dict[str, list[str]], *, now: datetime | str | None = None,
) -> dict[str, list[str]]:
    try:
        return destination_pacing.booked_slots(conn, aliases, now=now if now is not None else job_runtime._now_iso())
    except destination_pacing.PacingRefused as exc:
        raise RecoveryRefused(str(exc)) from None


def effective_daily_cap(requested: int | None = None) -> int:
    """Recovery may tighten the configured pacing, never raise or disable it."""
    return destination_pacing.effective_daily_cap(
        publish_orchestrator.MAX_POSTS_PER_ACCOUNT_PER_DAY, requested,
    )

def _recorded_id(value) -> int | None:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    return value if type(value) is int and 0 < value <= 2**63 - 1 else None


def _media_keys(conn, target, payload: dict) -> set[tuple]:
    """Exact queue refs, or WHOLE target-specific artifact sets; never stems/URLs.

    A job-wide artifact list is ambiguous when a legacy job has multiple file
    refs. In that case only each target's exact file_ref is used. Partial overlap
    (e.g. a shared thumbnail) does not make two different posts duplicates.
    """
    ref = target["file_ref"]
    if not isinstance(ref, str) or not ref.strip():
        raise RecoveryRefused("unresolved media identity")
    keys = {("file_ref", ref)}
    post = re.fullmatch(r"campaign_post:([0-9]+)", ref)
    if post:
        keys.add(("campaign_post", int(post[1])))
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT file_ref) FROM publish_job_targets WHERE job_id=?", (target["job_id"],)
    ).fetchone()[0]
    if distinct != 1 and not (post and _recorded_id(payload.get("campaignPostId")) == int(post[1])):
        return keys
    artifacts = payload.get("artifacts", [])
    if not isinstance(artifacts, list) or any(not isinstance(item, dict) for item in artifacts):
        raise RecoveryRefused("invalid artifact metadata")
    hashes, ids = [], []
    for artifact in artifacts:
        metadata = artifact.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise RecoveryRefused("invalid artifact metadata")
        sha = artifact.get("sha256") or metadata.get("sha256")
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            hashes.append(sha.lower())
        artifact_id = _recorded_id(artifact.get("id"))
        if artifact_id is not None:
            ids.append(artifact_id)
    if artifacts and len(hashes) == len(artifacts):
        keys.add(("artifact_sha256", tuple(sorted(hashes))))
    if artifacts and len(ids) == len(artifacts):
        keys.add(("artifact_ids", tuple(sorted(ids))))
    return keys


def _campaign_context(conn, target, job, payload) -> dict:
    ref = target["file_ref"]
    if not isinstance(ref, str):
        raise RecoveryRefused("unresolved media identity")
    match = re.fullmatch(r"campaign_post:([0-9]+)", ref)
    post_id = _recorded_id(match[1]) if match else None
    recorded = payload.get("campaignPostId")
    if (ref.startswith("campaign_post:") and post_id is None) or (
        recorded is not None and (post_id is None or _recorded_id(recorded) != post_id)
    ):
        raise RecoveryRefused("ambiguous campaign post identity")
    if post_id is None:
        return {}
    post = conn.execute("SELECT * FROM campaign_posts WHERE id=?", (post_id,)).fetchone()
    if post is None or post["last_published_job_id"] != job["id"]:
        raise RecoveryRefused("missing or superseded campaign post intent")
    campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (post["campaign_id"],)).fetchone()
    if campaign is None:
        raise RecoveryRefused("missing campaign intent")
    if (post["status"] in {"cancelled", "succeeded", "published", "draft", "needs_review"}
            or campaign["status"] in {"cancelled", "succeeded", "published", "preparing", "draft", "needs_review"}):
        raise RecoveryRefused("terminal, preparing or unapproved campaign intent")
    if (_platform(post["platform"]) != _platform(job["platform"])
            or campaign["profile_id"] != job["profile_id"]
            or post["workspace_id"] != job["workspace_id"]
            or campaign["workspace_id"] != job["workspace_id"]
            or (payload.get("campaignId") is not None
                and _recorded_id(payload["campaignId"]) != campaign["id"])):
        raise RecoveryRefused("campaign ownership mismatch")
    return {
        "post": {key: post[key] for key in post.keys() if key not in {"status", "updated_at"}},
        "campaign": {key: campaign[key] for key in campaign.keys() if key not in {"status", "published_at"}},
    }


def _check_duplicates(conn, target, keys, identity, aliases):
    for ref in aliases.get(identity, []):
        for other in conn.execute(
            "SELECT t.*, j.payload_json FROM publish_job_targets t "
            "LEFT JOIN publish_jobs j ON j.id=t.job_id WHERE t.account_ref=? AND t.id<>?",
            (ref, target["id"]),
        ):
            protected = other["status"] not in {"failed", "cancelled"} or (
                job_runtime._recovery_has_submission_evidence(other["checkpoint_json"], other["delivery_state"])
                or job_runtime._recovery_has_submission_evidence(other["payload_json"])
            )
            if not protected:
                continue
            try:
                other_keys = _media_keys(conn, other, _object(other["payload_json"], "delivery history"))
            except RecoveryRefused:
                raise RecoveryRefused("unreadable same-account delivery history") from None
            if keys & other_keys:
                reason = "duplicate of a succeeded post" if other["status"] == "succeeded" else "duplicate of a live or unresolved delivery"
                raise RecoveryRefused(reason)


def _snapshot(conn, target_id: int, *, resolved: bool, aliases=None) -> dict:
    target = conn.execute("SELECT * FROM publish_job_targets WHERE id=?", (target_id,)).fetchone()
    if target is None or target["status"] != "failed":
        raise RecoveryRefused("target is missing or no longer failed")
    job = conn.execute("SELECT * FROM publish_jobs WHERE id=?", (target["job_id"],)).fetchone()
    if job is None or job["status"] not in _LIVE_JOBS:
        raise RecoveryRefused("terminal or missing parent intent")
    if any(target[key] is not None for key in ("lease_owner", "lease_token", "lease_expires_at")):
        raise RecoveryRefused("target still has a lease")
    if job_runtime._recovery_has_submission_evidence(target["checkpoint_json"], target["delivery_state"]):
        raise RecoveryRefused("submission evidence requires separate reconciliation")
    payload = _object(job["payload_json"], "job payload")
    if job_runtime._recovery_has_submission_evidence(job["payload_json"]):
        raise RecoveryRefused("persisted delivery evidence requires separate reconciliation")
    if payload.get("platform") is not None and _platform(payload["platform"]) != _platform(job["platform"]):
        raise RecoveryRefused("job payload/platform mismatch")
    account_id = structured_account_id(target["account_ref"])
    if account_id is None:
        raise RecoveryRefused("unresolved account")
    account = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if account is None:
        raise RecoveryRefused("missing account")
    if account["enabled"] != 1:
        raise RecoveryRefused("disabled account")
    if job["profile_id"] is None or account["profile_id"] != job["profile_id"]:
        raise RecoveryRefused("account/profile mismatch")
    profile = conn.execute("SELECT * FROM profiles WHERE id=?", (job["profile_id"],)).fetchone()
    if profile is None:
        raise RecoveryRefused("missing profile")
    profile_settings = _object(profile["settings_json"], "profile")
    if any(row["workspace_id"] != job["workspace_id"] for row in (target, account, profile)):
        raise RecoveryRefused("workspace mismatch")
    if _platform(account["platform"]) != _platform(job["platform"]):
        raise RecoveryRefused("account/platform mismatch")
    config = _object(account["config_json"], "account")  # No decryption or credential validation.
    policy = _route_policy(_platform(job["platform"]), account={"id": account_id, "config": config}, settings=profile_settings)
    if config.get("_needsReconnect") and policy["provider"] != "sociamonials":
        raise RecoveryRefused("account requires reconnect")
    campaign = _campaign_context(conn, target, job, payload)
    if _classify(target["last_error"]) == "permanent" and not resolved:
        raise RecoveryRefused("permanent failure")
    aliases = aliases if aliases is not None else _account_refs(conn)
    identity = _identity_for_ref(aliases, target["account_ref"])
    keys = _media_keys(conn, target, payload)
    _check_duplicates(conn, target, keys, identity, aliases)
    # Recounts/projections change when another sibling in THIS plan is applied.
    # Exclude only those derived fields; terminal intent is always checked above.
    # Raw config (including encrypted token/expiry metadata) is hashed, not emitted.
    return {
        "target": dict(target), "account": dict(account), "profile": dict(profile),
        "job": {key: job[key] for key in job.keys() if key not in _JOB_PROJECTION},
        "job_targets": [dict(row) for row in conn.execute(
            "SELECT id, account_ref, file_ref, workspace_id FROM publish_job_targets "
            "WHERE job_id=? ORDER BY id", (job["id"],),
        )],
        "campaign": campaign, "identity": identity, "policy": policy, "resolved": resolved,
    }


def _fingerprint(snapshot: dict, slot: str, per_day_cap: int | None) -> str:
    blob = json.dumps(
        {"version": 1, "state": snapshot, "slot": slot, "per_day_cap": per_day_cap,
         "min_gap": publish_orchestrator.MIN_GAP_MINUTES,
         "default_cap": publish_orchestrator.MAX_POSTS_PER_ACCOUNT_PER_DAY},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def plan_recovery(
    *, db_path: Path = DB_PATH, lookback_days: int = 2, platform: str | None = None,
    limit: int | None = None, per_day_cap: int | None = None, now: datetime | None = None,
    target_ids: Iterable[int] | None = None, resolved_target_ids: Iterable[int] | None = None,
) -> list[dict]:
    """Read-only plan. Explicit/resolved IDs are a union, independent of lookback.

    Legacy all-NULL workspaces are compatible; mixed NULL/concrete ownership and
    missing job profiles are not. No identity is guessed from a media basename.
    """
    _integer(lookback_days, "lookback_days")
    if limit is not None:
        _integer(limit, "limit")
    per_day_cap = effective_daily_cap(per_day_cap)
    selected = _ids(target_ids, "target_ids")
    resolved = _ids(resolved_target_ids, "resolved_target_ids")
    explicit = target_ids is not None or resolved_target_ids is not None
    selected |= resolved
    if platform is not None:
        if not isinstance(platform, str) or _platform(platform) not in profiles.SUPPORTED_PLATFORMS:
            raise ValueError("platform must name a supported platform")
        platform = _platform(platform)
    if now is not None and not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    now = job_runtime._utc_datetime(now or datetime.now(timezone.utc))
    try:
        cutoff = (now - timedelta(days=lookback_days)).isoformat(timespec="seconds")
        base_time = (now + timedelta(minutes=5)).replace(second=0, microsecond=0)
    except OverflowError:
        raise ValueError("lookback_days or now is outside the supported datetime range") from None
    plan: list[dict] = []
    skipped: Counter[str] = Counter()
    with _connect(db_path) as conn:
        conn.execute("BEGIN")
        if explicit:
            rows = []
            for chunk in job_runtime._chunk(sorted(selected)):
                rows.extend(conn.execute(
                    "SELECT t.id, t.finished_at, j.platform FROM publish_job_targets t "
                    "LEFT JOIN publish_jobs j ON j.id=t.job_id WHERE t.id IN ("
                    + ",".join("?" for _ in chunk) + ")", chunk,
                ).fetchall())
            if {row["id"] for row in rows} != selected:
                raise ValueError("one or more selected target IDs were not found")
        else:
            rows = conn.execute(
                "SELECT t.id, t.finished_at, j.platform FROM publish_job_targets t "
                "LEFT JOIN publish_jobs j ON j.id=t.job_id "
                "WHERE t.status='failed' AND julianday(t.finished_at)>=julianday(?)", (cutoff,),
            ).fetchall()
        rows = sorted(rows, key=lambda row: (row["finished_at"] or "", row["id"]))
        aliases = _account_refs(conn)
        booked = _booked_slots(conn, aliases, now=now)
        reserved: dict[str, set[tuple]] = {}
        for row in rows:
            if platform is not None and _platform(row["platform"]) != platform:
                continue
            try:
                state = _snapshot(conn, row["id"], resolved=row["id"] in resolved, aliases=aliases)
                target, job = state["target"], state["job"]
                identity = state["identity"]
                keys = _media_keys(conn, target, _object(job["payload_json"], "job payload"))
                if keys & reserved.get(identity, set()):
                    raise RecoveryRefused("duplicate within recovery plan")
                account_id = state["account"]["id"]
                slot = publish_orchestrator._next_free_slot(
                    account_id, base_time, len(plan),
                    {account_id: booked.setdefault(identity, [])}, max_per_day=per_day_cap,
                ).isoformat(timespec="seconds")
                reserved.setdefault(identity, set()).update(keys)
                plan.append({
                    "target_id": target["id"], "job_id": target["job_id"],
                    "account_ref": target["account_ref"], "platform": _platform(job["platform"]),
                    "slot": slot, "error": _classify(target["last_error"]) + " failure",
                    "resolved_permanent": state["resolved"], "per_day_cap": per_day_cap,
                    "expected_fingerprint": _fingerprint(state, slot, per_day_cap),
                })
            except RecoveryRefused as exc:
                skipped[str(exc)] += 1
            if limit is not None and len(plan) >= limit:
                break
    if skipped:
        plan.append({"_skipped": dict(skipped)})
    return plan


def _validate_plan(plan: list[dict]) -> None:
    if not isinstance(plan, list):
        raise ValueError("plan must be a list of recovery entries")
    seen = set()
    for item in plan:
        if not isinstance(item, dict):
            raise ValueError("plan entries must be objects")
        if "_skipped" in item:
            if (set(item) != {"_skipped"} or not isinstance(item["_skipped"], dict)
                    or any(not isinstance(k, str) or type(v) is not int or v < 0
                           for k, v in item["_skipped"].items())):
                raise ValueError("invalid skipped summary")
            continue
        required = {"target_id", "job_id", "account_ref", "platform", "slot", "expected_fingerprint",
                    "resolved_permanent", "per_day_cap"}
        if not required <= item.keys():
            raise ValueError("plan is missing guarded recovery fields; regenerate it")
        target_id = _integer(item["target_id"], "target_id")
        _integer(item["job_id"], "job_id")
        if target_id in seen:
            raise ValueError("plan contains duplicate target IDs")
        seen.add(target_id)
        if (structured_account_id(item["account_ref"]) is None
                or not isinstance(item["platform"], str)
                or item["platform"] not in profiles.SUPPORTED_PLATFORMS
                or type(item["resolved_permanent"]) is not bool
                or not isinstance(item["expected_fingerprint"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["expected_fingerprint"])):
            raise ValueError("invalid guarded recovery metadata")
        if not isinstance(item["slot"], str):
            raise ValueError("plan slot must be an ISO datetime")
        job_runtime._utc_datetime(item["slot"])
        if item["per_day_cap"] is not None:
            _integer(item["per_day_cap"], "per_day_cap", minimum=0)


def _revalidate(conn, item: dict) -> str:
    if item["per_day_cap"] != effective_daily_cap(item["per_day_cap"]):
        raise RecoveryRefused("recovery would raise the configured pacing cap")
    state = _snapshot(conn, item["target_id"], resolved=item["resolved_permanent"])
    if (state["target"]["job_id"] != item["job_id"]
            or state["target"]["account_ref"] != item["account_ref"]
            or _platform(state["job"]["platform"]) != item["platform"]):
        raise RecoveryRefused("plan target metadata changed")
    aliases = _account_refs(conn)
    booked = _booked_slots(conn, aliases)
    slot = job_runtime._utc_datetime(item["slot"])
    account_id = state["account"]["id"]
    available = publish_orchestrator._next_free_slot(
        account_id, slot, 0, {account_id: booked.get(state["identity"], [])},
        max_per_day=item["per_day_cap"],
    )
    if available != slot:
        raise RecoveryRefused("planned slot is no longer free; regenerate plan")
    return _fingerprint(state, item["slot"], item["per_day_cap"])


def apply_recovery(plan: list[dict], *, db_path: Path = DB_PATH) -> dict:
    """Apply only still-eligible failed targets. Structural errors abort before writes.

    Refusals are per-target and roll back without clearing retry/delivery evidence.
    Reapplying a plan refuses changed targets; it never resets pending work again.
    """
    _validate_plan(plan)
    applied, errors = 0, []
    for item in plan:
        if "_skipped" in item:
            continue
        try:
            with publish_orchestrator.slot_reservation_lock(db_path):
                job_runtime.reschedule_failed_target(
                    item["target_id"], item["slot"], expected_fingerprint=item["expected_fingerprint"],
                    revalidate=lambda conn: _revalidate(conn, item), db_path=db_path,
                )
            applied += 1
        except Exception as exc:  # noqa: BLE001 - report safely, never abort the batch
            # Never echo arbitrary provider errors, database values or credentials.
            reason = str(exc) if isinstance(exc, RecoveryRefused) else "stale or ineligible recovery plan; regenerate it"
            if isinstance(exc, (sqlite3.Error, OSError)):
                reason = "recovery database unavailable or incompatible"
            errors.append({"target_id": item["target_id"], "error": reason})
    return {"applied": applied, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--lookback-days", type=int, default=2)
    parser.add_argument("--platform", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--target-id", type=int, action="append", default=None)
    parser.add_argument("--resolved-target-id", type=int, action="append", default=None,
                        help="select a target whose historical permanent failure has been resolved")
    parser.add_argument("--per-day-cap", type=int, default=None,
                        help="optional tighter cap; default preserves configured pacing")
    parser.add_argument("--apply", action="store_true", help="write the plan (default: dry-run)")
    args = parser.parse_args(argv)
    db_path = Path(args.db_path)
    try:
        plan = plan_recovery(
            db_path=db_path, lookback_days=args.lookback_days, platform=args.platform,
            limit=args.limit, per_day_cap=args.per_day_cap,
            target_ids=args.target_id, resolved_target_ids=args.resolved_target_id,
        )
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
    except (sqlite3.Error, OSError):
        parser.error("recovery database unavailable or incompatible; no changes made")
    for item in plan:
        if "_skipped" in item:
            print("skipped:", json.dumps(item["_skipped"], ensure_ascii=False))
            continue
        print(f"target {item['target_id']:>6} {item['platform']:>10} "
              f"{item['account_ref']:>12} -> {item['slot']}  ({item['error']})")
    if not args.apply:
        print(f"\nDRY RUN: {sum(1 for i in plan if '_skipped' not in i)} target(s) would be rescheduled.")
        print("Re-run with --apply to write these changes.")
        return 0
    result = apply_recovery(plan, db_path=db_path)
    print(f"\napplied={result['applied']} errors={len(result['errors'])}")
    for err in result["errors"]:
        print("  error:", err)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
