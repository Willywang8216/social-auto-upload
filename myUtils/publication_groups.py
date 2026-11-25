"""Normal publication finalization: a complete destination group, or no writes.

Both request-time and off-request preparation use this implementation. All draft,
provider/media/provenance and byte verification precedes the reservation. A short
owned SQLite transaction reloads the calendar, checks the sealed admission facts,
and installs posts, jobs, targets and links together. Recovery is a separate API.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from myUtils import account_identity, campaigns, content_rules, destination_pacing, jobs
from myUtils import media_compliance, media_groups, media_integrity, platform_capabilities
from myUtils import publish_admission, publish_media_guard, publish_readiness

MAX_CALENDAR_RECHECKS = 3


class PublicationGroupConflict(ValueError):
    """Existing partial/changed history must use explicit guarded review/recovery."""


class DuplicatePublication(ValueError):
    """A different approved group already owns these media at this destination."""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _object(value) -> dict:
    return account_identity.json_object(value or "{}")


def _stamp(path: str) -> tuple:
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _artifact_intent(artifact: dict) -> dict:
    # SQL row IDs, output locations and a newly encoded image generation's UUID
    # are registrations, not new approvals. Exact bytes, ALL other metadata,
    # source/variant/parent membership and preparation policy remain in the ID.
    result = copy.deepcopy(artifact)
    for key in ("id", "artifact_id", "campaign_id", "created_at", "local_path",
                "public_url", "remote_path", "storage_backend_id"):
        result.pop(key, None)
    metadata = result.get("metadata") or {}
    metadata.pop("preparation_group", None)
    result["metadata"] = metadata
    result["sha256"] = media_integrity.expected_sha256(artifact, required=True)
    return result


def _member_intent(payload: dict) -> dict:
    result = copy.deepcopy(payload)
    for key in (*publish_admission.RUNTIME_PAYLOAD_KEYS, "campaignId", "campaignPostId", "publicationGroup"):
        result.pop(key, None)
    result["artifacts"] = [_artifact_intent(a) for a in result.get("artifacts") or []]
    return result


@dataclass(frozen=True)
class PublicationGroup:
    campaign: campaigns.Campaign
    account_id: int
    workspace_id: str | None
    identity: str
    provider: str
    configuration: str
    profile_facts: str
    identifier: str
    payloads: tuple[dict, ...]
    file_ids: tuple[list[int] | None, ...]
    base_time: datetime | None
    offset_index: int
    source_rows: tuple[tuple[int, str], ...]
    source_items: str
    source_files: tuple[tuple[str, tuple], ...]
    saved_post_ids: tuple[int, ...] = ()

    @property
    def account_ref(self) -> str:
        return f"account:{self.account_id}"


def _fence(conn, campaign: campaigns.Campaign, *, installing: bool = False):
    from myUtils.delivery import LeaseLost

    row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign.id,)).fetchone()
    if row is None or row["profile_id"] != campaign.profile_id or row["media_group_id"] != campaign.media_group_id:
        raise PublicationGroupConflict("Campaign/source ownership changed before finalization")
    metadata = _object(row["metadata_json"])
    old = campaign.metadata or {}
    expected, observed = old.get(campaigns.PREP_LEASE_KEY) or {}, metadata.get(campaigns.PREP_LEASE_KEY) or {}
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        raise LeaseLost("Invalid preparation ownership")
    fence = None
    if expected or observed:
        fence = (str(expected.get("owner") or ""), int(old.get(campaigns.PREP_ATTEMPTS_KEY) or 0))
        current = (str(observed.get("owner") or ""), int(metadata.get(campaigns.PREP_ATTEMPTS_KEY) or 0))
        if (not fence[0] or fence != current or row["status"] != campaigns.CAMPAIGN_PREPARING
                or expected.get("claimedAt") != observed.get("claimedAt")):
            raise LeaseLost("Preparation ownership changed before finalization")
    elif installing and row["status"] != campaigns.CAMPAIGN_PREPARING:
        raise PublicationGroupConflict("Campaign is not preparing; explicit recovery is required")
    if (json.loads(row["selected_account_ids_json"] or "[]") != campaign.selected_account_ids
            or metadata.get(campaigns.PREP_REQUEST_KEY) != old.get(campaigns.PREP_REQUEST_KEY)):
        raise PublicationGroupConflict("Approved campaign request changed before finalization")
    return row, fence


def _context(conn, campaign, profile, account):
    parent, _ = _fence(conn, campaign)
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (account.id,)).fetchone()
    profile_row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile.id,)).fetchone()
    workspace = parent["workspace_id"]
    if (row is None or profile_row is None or profile.id != campaign.profile_id
            or row["profile_id"] != profile.id or row["workspace_id"] != workspace
            or profile_row["workspace_id"] != workspace or not row["enabled"]
            or account.id not in campaign.selected_account_ids):
        raise publish_admission.AdmissionError("Destination/campaign profile or workspace ownership changed", "ownership_changed")
    context = publish_readiness._context(row, _object(profile_row["settings_json"]))
    supplied = publish_readiness._context(account, profile.settings or {})
    if context["fingerprint"] != supplied["fingerprint"]:
        raise publish_admission.AdmissionError("Destination configuration changed during preparation", "configuration_changed")
    keys = account_identity.canonical_account_keys(conn, (f"account:{account.id}",))
    return workspace, keys[f"account:{account.id}"], context, _json(dict(profile_row))


def _source_evidence(db_path, campaign, media_files, post_groups, platform):
    """Whole-generation provenance (also count=1) and exact selected source facts."""
    with closing(destination_pacing.connect_readonly(db_path)) as conn:
        conn.execute("BEGIN")
        parent, _ = _fence(conn, campaign)
        workspace = parent["workspace_id"]
        media_group = conn.execute("SELECT workspace_id FROM media_groups WHERE id=?", (campaign.media_group_id,)).fetchone()
        items = [dict(row) for row in conn.execute(
            "SELECT id,file_record_id,role,sort_order,workspace_id FROM media_group_items "
            "WHERE media_group_id=? ORDER BY sort_order,id", (campaign.media_group_id,))]
        selected = [(item["file_record_id"], item["role"]) for item in items]
        if (media_group is None or media_group[0] != workspace
                or any(item["workspace_id"] != workspace for item in items)
                or selected != [(item["file_record_id"], item["role"]) for item in media_files]):
            raise PublicationGroupConflict("Selected source ownership/membership/order changed during preparation")
        records = []
        for media in media_files:
            row = conn.execute("SELECT * FROM file_records WHERE id=?", (media["file_record_id"],)).fetchone()
            if row is None or row["file_path"] != media["file_path"] or row["workspace_id"] != workspace:
                raise PublicationGroupConflict("Selected source registration/ownership changed during preparation")
            records.append(dict(row))
    # Close the read transaction BEFORE any hashing, probing or filesystem work.
    rows = [(row["id"], _json(row)) for row in records]
    paths = {}

    def verify(path, artifact):
        resolved = str(Path(path).resolve())
        before = _stamp(resolved)
        media_integrity.verify_artifact(resolved, artifact, required=True)
        if before != _stamp(resolved) or paths.setdefault(resolved, before) != before:
            raise publish_admission.AdmissionError("Source bytes changed during finalization", "artifact_changed")

    from myUtils.campaign_media_prep import _resolve_video_file_path_safely
    for row in records:
        registered_path = Path(row["file_path"])
        # These are exact tenant-owned SQL registrations, not user-supplied
        # path guesses. Preserve absolute restored/source paths without a basename fallback.
        path = registered_path if registered_path.is_absolute() else _resolve_video_file_path_safely(row["file_path"])
        if path is None:
            raise PublicationGroupConflict("Exact registered source path is unavailable; preparation is required")
        expected = {key: row[key] for key in ("sha256", "size_bytes") if row[key] is not None}
        if row["filesize"] is not None and row["filesize"] > 0:
            # The legacy SQL column is REAL, but represents an integral byte count.
            if not float(row["filesize"]).is_integer() or row["filesize"] >= 2**63:
                raise PublicationGroupConflict("Selected source has an invalid registered byte count")
            expected["filesize"] = int(row["filesize"])
        # Measure legacy source facts locally; never invent/persist a registry SHA.
        facts = media_integrity.verify_artifact(path, expected)
        verify(path, facts)
    videos = [a for artifacts, _ in post_groups for a in artifacts if publish_media_guard.media_kind(a) == "video"]
    sources = list(dict.fromkeys(a.get("source_file_record_id") for a in videos))
    for source_id in sources:
        members = [a for a in videos if a.get("source_file_record_id") == source_id]
        ordered = media_compliance.select_prepared_parts(members, platform)
        if ordered != members:
            raise PublicationGroupConflict("Publication must contain every unique ordered source part")
        metadata = ordered[0]["metadata"]
        input_path, input_sha = metadata.get("input_path"), metadata.get("input_sha256")
        if not input_path or not input_sha:
            raise PublicationGroupConflict("Prepared parts lack exact input byte provenance; reprepare required")
        verify(input_path, {"sha256": input_sha})
        if metadata.get("source_is_complete") is True:
            measured = media_compliance._source_metadata(Path(input_path))["duration"]
            interval = float(metadata["source_interval_end_seconds"]) - float(metadata["source_interval_start_seconds"])
            if abs(measured - interval) > 0.000001:
                raise PublicationGroupConflict("Prepared parts do not cover the complete measured input")
        else:
            parent = metadata.get("source_parent")
            if (metadata.get("source_is_complete") is not False or not isinstance(parent, dict)
                    or type(parent.get("part_index")) is not int or type(parent.get("part_count")) is not int
                    or not 1 <= parent["part_index"] <= parent["part_count"]
                    or not parent.get("sha256")
                    or parent.get("source_start_seconds") is None or parent.get("source_end_seconds") is None):
                raise PublicationGroupConflict("Prepared segment lost its parent membership or original interval")
        origin, checksum = metadata.get("origin_source_path"), metadata.get("origin_source_sha256")
        if origin and checksum:
            verify(origin, {"sha256": checksum})
    return tuple(rows), _json(items), tuple(paths.items())


def _saved_draft(conn, campaign, account_id):
    posts = [row for row in conn.execute("SELECT * FROM campaign_posts WHERE campaign_id=? ORDER BY id", (campaign.id,))
             if account_id in json.loads(row["account_ids_json"] or "[]")]
    if not posts:
        return None
    drafts = [_object(row["draft_json"]) for row in posts]
    if any(draft != drafts[0] for draft in drafts):
        raise PublicationGroupConflict("Existing destination group has conflicting approved drafts")
    return copy.deepcopy(drafts[0])


def _draft(account, profile, media_group, request_data, media_context, options, override, saved, kind, provider, generate):
    if override is not None:
        if not isinstance(override, dict):
            raise publish_admission.AdmissionError("Approved account draft must be an object")
        return copy.deepcopy(override)  # NEVER normalize, regenerate or drop approved fields.
    if saved is not None:
        return saved
    language = str((account.config or {}).get("audience_language") or (account.config or {}).get("audienceLanguage")
                   or (profile.settings or {}).get("default_language") or (profile.settings or {}).get("defaultLanguage") or "").strip()
    request = dict(request_data)
    if account.platform == "twitter" and len(language.replace(",", " ").replace("+", " ").split()) > 1:
        raise ValueError("X requires one language per account; use distinct account settings")
    if language:
        request["_accountLanguage"] = language
    try:
        draft = dict(generate(account, profile, media_group, request, media_context))
    except Exception as exc:  # noqa: BLE001 - fallback is only for not-yet-approved copy
        if language:
            raise ValueError(f"language-specific draft generation failed: {exc}") from exc
        brief = str(request_data.get("notes") or "")
        if not content_rules.is_usable_copy(brief):
            raise ValueError(f"draft generation failed and no usable brief: {exc}") from exc
        draft = {"message": brief.strip(), "hashtags": [], "error": str(exc)}
    if not options.get("linkInFirstComment"):
        comment = draft.get("firstComment") or ""
        if comment and comment not in (draft.get("message") or ""):
            draft["message"] = ((draft.get("message") or "").rstrip() + "\n\n" + comment).strip()
        draft.pop("firstComment", None)
    # This API shapes ONLY autogenerated prospective options before approval.
    return publish_admission.prospective_draft_options(draft, account.platform, kind, provider)


def _base_time(campaign, base_time):
    from myUtils import publish_orchestrator as po
    request = (campaign.metadata or {}).get(campaigns.PREP_REQUEST_KEY) or {}
    schedule = request.get("schedule")
    if schedule is not None and not isinstance(schedule, dict):
        raise destination_pacing.PacingRefused("Requested schedule must be an object")
    if schedule and not schedule.get("publishNow"):
        resolved = po._resolve_base_time(schedule)
        if resolved is None:
            raise destination_pacing.PacingRefused("Requested schedule is invalid; timer cannot be ignored")
        if base_time is not None and destination_pacing.utc_datetime(base_time) != resolved:
            raise destination_pacing.PacingRefused("Requested schedule changed during preparation")
        return resolved
    return destination_pacing.utc_datetime(base_time) if base_time is not None else None


def _key(group, index):
    return f"publication-group:{group.identifier}:{index}"


def _existing(conn, group):
    """Return the SAME whole group; incomplete/conflicting history is not a skip."""
    prefix = f"publication-group:{group.identifier}:"
    matched = []
    for row in conn.execute("SELECT * FROM publish_jobs WHERE idempotency_key LIKE ? OR payload_json LIKE ? ORDER BY id",
                            (prefix + "%", "%" + group.identifier + "%")):
        payload = _object(row["payload_json"])
        if row["idempotency_key"].startswith(prefix) or (payload.get("publicationGroup") or {}).get("id") == group.identifier:
            matched.append((row, payload))
    posts = [row for row in conn.execute("SELECT * FROM campaign_posts WHERE sheet_row_json LIKE ? ORDER BY id",
                                        ("%" + group.identifier + "%",))
             if (_object(row["sheet_row_json"]).get("publicationGroup") or {}).get("id") == group.identifier]
    if not matched and not posts:
        return None
    count = len(group.payloads)
    if len(matched) != count or len(posts) != count:
        raise PublicationGroupConflict("Existing publication group is incomplete; missing tail requires guarded recovery")
    by_post = {row["id"]: row for row in posts}
    indices, result, slots, campaign_ids = set(), {}, {}, set()
    for row, payload in matched:
        identifier, index, total = jobs._calendar_part(payload)
        if (identifier != group.identifier or total != count or index in indices
                or row["idempotency_key"] != _key(group, index)
                or row["workspace_id"] != group.workspace_id or row["profile_id"] != group.campaign.profile_id
                or row["platform"] != group.payloads[0]["platform"] or row["total_targets"] != 1
                or _member_intent(payload) != _member_intent(group.payloads[index - 1])):
            raise PublicationGroupConflict("Existing publication group has conflicting indices or approved media/draft intent")
        indices.add(index)
        post = by_post.get(payload.get("campaignPostId"))
        if (post is None or post["campaign_id"] != payload.get("campaignId")
                or post["platform"] != row["platform"] or post["workspace_id"] != group.workspace_id
                or post["last_published_job_id"] != row["id"]
                or json.loads(post["account_ids_json"] or "[]") != [group.account_id]
                or _object(post["draft_json"]) != payload.get("draft")
                or (json.loads(post["file_record_ids_json"]) if post["file_record_ids_json"] else None) != group.file_ids[index - 1]
                or _object(post["sheet_row_json"]).get("publicationGroup") != payload["publicationGroup"]):
            raise PublicationGroupConflict("Existing publication group has conflicting post ownership or links")
        parent = conn.execute("SELECT profile_id,workspace_id FROM campaigns WHERE id=?", (post["campaign_id"],)).fetchone()
        if parent is None or parent["profile_id"] != row["profile_id"] or parent["workspace_id"] != group.workspace_id:
            raise PublicationGroupConflict("Existing publication group campaign ownership changed")
        targets = conn.execute("SELECT * FROM publish_job_targets WHERE job_id=? ORDER BY id", (row["id"],)).fetchall()
        if (len(targets) != 1 or targets[0]["account_ref"] != group.account_ref
                or targets[0]["file_ref"] != f"campaign_post:{post['id']}" or targets[0]["workspace_id"] != group.workspace_id
                or not targets[0]["schedule_at"]):
            raise PublicationGroupConflict("Existing publication group targets are incomplete or unlinked")
        if post["status"] in {campaigns.CAMPAIGN_POST_DRAFT, campaigns.CAMPAIGN_POST_READY}:
            raise PublicationGroupConflict("Existing publication post was not completely linked/queued")
        # Labels are NEVER permission to reset a receipt or create another job.
        jobs._recovery_has_submission_evidence(targets[0]["checkpoint_json"], targets[0]["delivery_state"])
        slots[index] = destination_pacing.utc_datetime(targets[0]["schedule_at"])
        campaign_ids.add(post["campaign_id"])
        result[index] = jobs._row_to_job(row)
    if indices != set(range(1, count + 1)) or len(campaign_ids) != 1:
        raise PublicationGroupConflict("Existing publication group is incomplete or mixed across campaigns")
    _validate_slots(group, [slots[index] for index in range(1, count + 1)])
    return [result[index] for index in range(1, count + 1)], [slots[index] for index in range(1, count + 1)]


def _historical_group(conn, group, post, payload):
    """Use the SAME completeness/link validator for conflicting group history."""
    old_id, _index, count = jobs._calendar_part(payload)
    if not old_id or count > media_compliance.MAX_PARTS:
        raise PublicationGroupConflict("Legacy publication lacks provable complete-group membership; explicit review required")
    members = []
    for row in conn.execute(
            "SELECT j.payload_json,p.file_record_ids_json FROM publish_jobs j "
            "LEFT JOIN campaign_posts p ON p.last_published_job_id=j.id WHERE j.idempotency_key LIKE ?",
            (f"publication-group:{old_id}:%",)):
        member = _object(row["payload_json"])
        identifier, index, total = jobs._calendar_part(member)
        if identifier != old_id or total != count:
            raise PublicationGroupConflict("Conflicting publication history has inconsistent membership")
        members.append((index, member, json.loads(row["file_record_ids_json"]) if row["file_record_ids_json"] else None))
    if len(members) != count or {index for index, _, _ in members} != set(range(1, count + 1)):
        raise PublicationGroupConflict("Conflicting publication history is incomplete; missing tail requires guarded recovery")
    members.sort(key=lambda entry: entry[0])
    account_ids = json.loads(post["account_ids_json"] or "[]")
    if len(account_ids) != 1:
        raise PublicationGroupConflict("Conflicting publication history has ambiguous destination ownership")
    campaign = campaigns._row_to_campaign(conn.execute("SELECT * FROM campaigns WHERE id=?", (post["campaign_id"],)).fetchone())
    historical = replace(group, campaign=campaign, account_id=account_ids[0], identifier=old_id,
        payloads=tuple(member for _, member, _ in members), file_ids=tuple(ids for _, _, ids in members),
        base_time=_base_time(campaign, None))
    if _existing(conn, historical) is None:
        raise PublicationGroupConflict("Conflicting publication history is incomplete")


def _duplicates(conn, group, keys):
    refs = [ref for ref, key in keys.items() if key == group.identity]
    placeholders = ",".join("?" for _ in refs)
    candidates = conn.execute(
        "SELECT DISTINCT c.id,c.media_group_id FROM campaigns c JOIN campaign_posts p ON p.campaign_id=c.id "
        "LEFT JOIN publish_job_targets t ON t.file_ref=('campaign_post:' || p.id) "
        f"WHERE t.account_ref IN ({placeholders}) OR c.media_group_id=? "
        "UNION SELECT id,media_group_id FROM campaigns WHERE id=?",
        (*refs, group.campaign.media_group_id, group.campaign.id)).fetchall()
    selected = {row[0] for row in conn.execute("SELECT file_record_id FROM media_group_items WHERE media_group_id=?",
                                             (group.campaign.media_group_id,))}
    account_ids = {account_identity.structured_account_id(ref) for ref in refs}
    duplicate = False
    verified_history = set()
    for candidate in candidates:
        original = {row[0] for row in conn.execute("SELECT file_record_id FROM media_group_items WHERE media_group_id=?",
                                                 (candidate["media_group_id"],))}
        if candidate["media_group_id"] != group.campaign.media_group_id and (not selected or original != selected):
            continue
        for post in conn.execute("SELECT * FROM campaign_posts WHERE campaign_id=?", (candidate["id"],)):
            if candidate["id"] == group.campaign.id and post["id"] in group.saved_post_ids:
                continue  # Exact saved rows are fenced again before the first write.
            if not account_ids.intersection(json.loads(post["account_ids_json"] or "[]")):
                continue
            own = (_object(post["sheet_row_json"]).get("publicationGroup") or {}).get("id")
            if own == group.identifier:
                continue  # _existing verified every member/link, not just this prefix.
            if post["last_published_job_id"] is None:
                raise PublicationGroupConflict("Existing unlinked campaign post conflicts with complete-group finalization")
            previous = conn.execute("SELECT payload_json FROM publish_jobs WHERE id=?", (post["last_published_job_id"],)).fetchone()
            if previous is None:
                raise PublicationGroupConflict("Existing publication history has a missing job/link")
            payload = _object(previous[0])
            old_id, _, _ = jobs._calendar_part(payload)
            if old_id not in verified_history:
                _historical_group(conn, group, post, payload)
                verified_history.add(old_id)
            duplicate = True
    if duplicate:
        raise DuplicatePublication("this media is already queued for this account or an alias of the same publishing destination; explicit recovery/review required")


def _calendar(conn, group, now):
    keys = account_identity.canonical_account_keys(conn, (group.account_ref,))
    if keys.get(group.account_ref) != group.identity:
        raise PublicationGroupConflict("Canonical destination identity changed during preparation")
    aliases = {}
    for ref, key in keys.items():
        aliases.setdefault(key, []).append(ref)
    booked = destination_pacing.booked_slots(conn, aliases, now=now)
    return keys, booked.get(group.identity, [])


def _slots(group, taken, now):
    from myUtils import publish_orchestrator as po
    result, previous = [], None
    for index in range(len(group.payloads)):
        offset = index if group.base_time is not None else group.offset_index + index
        slot = po._next_free_slot(group.account_id, group.base_time or now, offset,
            {group.account_id: taken}, min_gap_minutes=30, max_per_day=3, not_before=previous, now_fn=lambda: now)
        result.append(slot)
        previous = slot
    _validate_slots(group, result)
    return result


def _validate_slots(group, slots):
    from myUtils import publish_orchestrator as po
    gap = timedelta(minutes=max(30, po.MIN_GAP_MINUTES))
    cap = destination_pacing.effective_daily_cap(po.MAX_POSTS_PER_ACCOUNT_PER_DAY, 3)
    if (len(slots) != len(group.payloads) or any(right - left < gap for left, right in zip(slots, slots[1:]))
            or (group.base_time is not None and slots[0] < group.base_time)
            or any(count > cap for count in Counter(slot.replace(tzinfo=timezone.utc).astimezone(po._operator_timezone()).date()
                                                   for slot in slots).values())):
        raise PublicationGroupConflict("Existing/requested publication slots conflict with ordered destination pacing")


def _validate_sources(conn, group):
    for identifier, facts in group.source_rows:
        row = conn.execute("SELECT * FROM file_records WHERE id=?", (identifier,)).fetchone()
        if row is None or _json(dict(row)) != facts:
            raise publish_admission.AdmissionError("Source registration changed after verification", "artifact_changed")
    items = [dict(row) for row in conn.execute(
        "SELECT id,file_record_id,role,sort_order,workspace_id FROM media_group_items "
        "WHERE media_group_id=? ORDER BY sort_order,id", (group.campaign.media_group_id,))]
    if _json(items) != group.source_items:
        raise publish_admission.AdmissionError("Source ownership/order changed after verification", "ownership_changed")
    for path, stamp in group.source_files:
        try:
            if _stamp(path) != stamp:
                raise publish_admission.AdmissionError("Verified source bytes changed before installation", "artifact_changed")
        except OSError as exc:
            raise publish_admission.AdmissionError("Verified source disappeared before installation", "artifact_changed") from exc
    profile = conn.execute("SELECT * FROM profiles WHERE id=?", (group.campaign.profile_id,)).fetchone()
    if profile is None or _json(dict(profile)) != group.profile_facts:
        raise publish_admission.AdmissionError("Profile policy changed after preparation", "configuration_changed")


def _refresh_group_destination(group: PublicationGroup, db_path: Path) -> None:
    """One fresh grant check per complete group, never under the queue lock."""
    from myUtils import profiles, preparation_context
    from myUtils.secret_redaction import redact_text
    account = profiles.get_account(group.account_id, db_path=db_path)
    profile = profiles.get_profile(group.campaign.profile_id, db_path=db_path)
    context = publish_readiness._context(account, profile.settings or {})
    if context["fingerprint"] != group.configuration or context["provider"] != group.provider:
        raise publish_admission.AdmissionError("Destination configuration changed after preparation", "configuration_changed")
    if publish_readiness.destination_readiness(account, profile.settings, db_path=db_path)["ready"]:
        return
    try:
        preparation_context.ensure_destination_ready(account, settings=profile.settings, db_path=db_path,
                                                     payload=group.payloads[0])
    except Exception as exc:
        raise publish_admission.AdmissionError(
            "Complete publication needs current verified destination/media evidence: " + redact_text(str(exc))[:200],
            "destination_verification_required") from exc


def install_group(group: PublicationGroup, *, db_path: Path, now: datetime) -> list[jobs.Job]:
    """Shared real commit boundary. Admission work is always outside the lock/TX.

    Schedules are admission-bound too: an optimistic calendar change releases the
    reservation and prepares new snapshots, WITHOUT regenerating any copy/media.
    The finite recheck bound refuses a persistently changing calendar.
    """
    from myUtils import publish_orchestrator as po
    now = destination_pacing.utc_datetime(now)
    for _attempt in range(MAX_CALENDAR_RECHECKS):
        with closing(destination_pacing.connect_readonly(db_path)) as conn:
            conn.execute("BEGIN")
            _fence(conn, group.campaign)
            keys, booked = _calendar(conn, group, now)
            existing = _existing(conn, group)
            _duplicates(conn, group, keys)
            slots = existing[1] if existing else _slots(group, booked, now)
        _refresh_group_destination(group, db_path)
        admitted = []
        for index, (payload, slot) in enumerate(zip(group.payloads, slots), 1):
            existing_payload = existing[0][index - 1].payload if existing else payload
            post_ref = f"campaign_post:{existing_payload['campaignPostId']}"
            spec = jobs.JobSpec(platform=payload["platform"], payload=copy.deepcopy(existing_payload),
                profile_id=group.campaign.profile_id, targets=[(group.account_ref, post_ref, slot)],
                idempotency_key=_key(group, index))
            try:
                spec, snapshot = publish_admission.prepare_job_spec(spec, workspace_id=group.workspace_id, db_path=db_path)
            except publish_admission.AdmissionError as exc:
                if existing is not None:
                    raise PublicationGroupConflict("Existing publication group media/intent registration changed; guarded recovery required") from exc
                raise
            if not snapshot.ready:
                raise publish_admission.AdmissionError("Complete publication needs current verified destination/media evidence", "destination_verification_required")
            facts = json.loads(snapshot.facts_json)
            if any(fact.get("fingerprint") != group.configuration or fact.get("provider") != group.provider for fact in facts["accounts"]):
                raise publish_admission.AdmissionError("Destination configuration changed after draft preparation", "configuration_changed")
            admitted.append((spec, snapshot))
        retry_calendar = False
        with po.slot_reservation_lock(db_path), campaigns._connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _, fence = _fence(conn, group.campaign)
                keys, booked = _calendar(conn, group, now)
                current = _existing(conn, group)
                _duplicates(conn, group, keys)
                _validate_sources(conn, group)
                # Validate EVERY member before the first campaign-post insert.
                for spec, snapshot in admitted:
                    publish_admission.validate_admission_snapshot(conn, spec, snapshot)
                if current and existing is None:
                    # A concurrent exact finalizer won. Release the transaction,
                    # then verify its ACTUAL registered bytes/post bindings outside TX.
                    retry_calendar = True
                    result = []
                elif current:
                    if current[1] != slots:
                        raise PublicationGroupConflict("Existing group schedules changed after admission")
                    result = current[0]  # receipt/status/attempt history is untouched
                elif _slots(group, booked, now) != slots:
                    retry_calendar = True
                    result = []
                else:
                    _fence(conn, group.campaign, installing=True)
                    result = []
                    for index, (spec, snapshot) in enumerate(admitted):
                        payload = copy.deepcopy(spec.payload)
                        post = campaigns.add_campaign_post_in_transaction(conn, group.campaign.id, spec.platform,
                            account_ids=[group.account_id], draft=payload["draft"], file_record_ids=group.file_ids[index],
                            sheet_row={"publicationGroup": payload["publicationGroup"]},
                            status=campaigns.CAMPAIGN_POST_READY, workspace_id=group.workspace_id)
                        payload["campaignPostId"] = post.id
                        bound = replace(spec, payload=payload,
                            targets=[(group.account_ref, f"campaign_post:{post.id}", slots[index])])
                        result.append(jobs.enqueue_job_in_transaction(conn, bound, workspace_id=group.workspace_id,
                            campaign_post_id=post.id, prep_fence=fence, admission_snapshot=snapshot, calendar_now=now))
                    # A missing link/target (including a trigger suppressing one)
                    # is a rollback, not a successfully installed queued prefix.
                    installed = _existing(conn, group)
                    if installed is None:
                        raise PublicationGroupConflict("Publication group installation is incomplete")
                    result = installed[0]
                if retry_calendar:
                    conn.rollback()
                else:
                    conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if not retry_calendar:
            return result
    raise destination_pacing.PacingRefused("Destination calendar kept changing; retry the unchanged complete request")


def finalize_campaign(
    campaign: campaigns.Campaign, *, profile, accounts, media_context: dict, media_files: list[dict],
    request_data: dict, options: dict, account_drafts: dict, tiktok_post_settings: dict, db_path: Path,
    artifact_payloads_for_platform: Callable, generate_account_draft: Callable,
    artifact_part_groups_for_platform: Callable | None = None, job_to_payload: Callable,
    base_time: datetime | None = None, offset_index: int = 0, now_fn: Callable = lambda: datetime.now(timezone.utc),
) -> dict:
    """Prepare ALL destination payloads first, then atomically install each group.

    Returns the existing jobs/skipped envelope. Campaign status completion remains
    with the caller (the async caller still owns its prep-completion CAS).
    """
    from myUtils import publish_orchestrator as po
    if not accounts:
        return {"jobs": [], "skipped": []}
    base = _base_time(campaign, base_time)
    now = destination_pacing.utc_datetime(now_fn())
    media_group = media_groups.get_media_group(campaign.media_group_id, db_path=db_path)
    artifacts = [a.to_dict() for a in campaigns.list_campaign_artifacts(campaign.id, db_path=db_path)]
    groups, skipped = [], []
    for account in accounts:
        label = {"profileId": profile.id, "accountId": account.id,
                 "accountName": account.nickname or account.account_name, "platform": account.platform}
        reason = po.language_routing_skip_reason(po._content_language(media_context, request_data, campaign.metadata),
            account, profile, include_zh_for_english=bool(options.get("includeZhForEnglish")))
        if reason:
            skipped.append({**label, "reason": reason})
            continue
        try:
            with closing(destination_pacing.connect_readonly(db_path)) as conn:
                conn.execute("BEGIN")
                workspace, identity, context, profile_facts = _context(conn, campaign, profile, account)
                saved = _saved_draft(conn, campaign, account.id)
            post_groups = po._prepared_post_groups(artifacts, account.platform, media_files,
                supports_multi_media=platform_capabilities.platform_supports_multi_media(account.platform),
                artifact_payloads_for_platform=artifact_payloads_for_platform,
                artifact_part_groups_for_platform=artifact_part_groups_for_platform, account_config=context["config"],
                backup_compatible=context["provider"] == "sociamonials" or account.platform in (request_data.get("_backupCompatiblePlatforms") or []),
                image_limits=(request_data.get("_imagePreparationLimits") or {}).get(account.platform),
                video_limits=(request_data.get("_preparationLimits") or {}).get(account.platform))
            if not post_groups or len(post_groups) > media_compliance.MAX_PARTS:
                raise PublicationGroupConflict("Publication requires a bounded complete ordered part set")
            source_rows, source_items, source_files = _source_evidence(db_path, campaign, media_files, post_groups, account.platform)
            kinds = {publish_media_guard.media_kind(a) for values, _ in post_groups for a in values
                     if not publish_media_guard.is_support_artifact(a) and not publish_media_guard.is_context_artifact(a)}
            kind = next(iter(kinds)) if len(kinds) == 1 else None
            override = account_drafts.get(str(account.id), account_drafts.get(account.id))
            draft = _draft(account, profile, media_group, request_data, media_context, options, override, saved,
                           kind, context["provider"], generate_account_draft)
            settings = tiktok_post_settings.get(str(account.id), tiktok_post_settings.get(account.id))
            if settings is not None and (not isinstance(settings, dict) or account.platform != "tiktok"):
                raise publish_admission.AdmissionError("Approved TikTok post settings do not match this destination", "unsupported_option")
            if "tiktokDirectPost" in options and type(options["tiktokDirectPost"]) is not bool:
                raise publish_admission.AdmissionError("tiktokDirectPost must be a literal boolean", "unsupported_option")
            payloads, file_ids = [], []
            for index, (values, ids) in enumerate(post_groups, 1):
                payload = po._build_payload(campaign, SimpleNamespace(id=0), copy.deepcopy(draft), copy.deepcopy(values), account.platform,
                    tiktok_direct_post=bool(options.get("tiktokDirectPost", True)), tiktok_post_settings=copy.deepcopy(settings))
                if ids is not None:
                    payload["fileRecordIds"] = list(ids)
                payload["partIndex"], payload["partCount"] = index, len(post_groups)
                for key in ("networkOptions", "network_options", "altText", "alt_text", "firstComment", "first_comment", "cover"):
                    if key in options:
                        payload[key] = copy.deepcopy(options[key])
                publish_admission.validate_intent(payload, platform=account.platform,
                    effective_provider=context["provider"], account_config=context["config"])
                payloads.append(payload)
                file_ids.append(ids)
            intent = {"version": 1, "destination": identity, "profile": campaign.profile_id, "workspace": workspace,
                      "provider": context["provider"], "options": options, "requestedBase": base.isoformat() if base else None,
                      "members": [_member_intent(p) for p in payloads]}
            identifier = hashlib.sha256(_json(intent).encode("utf-8")).hexdigest()
            for index, payload in enumerate(payloads, 1):
                payload["publicationGroup"] = {"id": identifier, "index": index, "count": len(payloads)}
            group = PublicationGroup(campaign, account.id, workspace, identity, context["provider"], context["fingerprint"],
                profile_facts, identifier, tuple(payloads), tuple(file_ids), base, offset_index,
                source_rows, source_items, source_files)
            groups.append((group, label))
            offset_index += len(payloads)
        except PublicationGroupConflict:
            raise  # A queued prefix is NOT an ordinary already-queued skip.
        except (ValueError, OSError) as exc:
            skipped.append({**label, "reason": f"prepared publication validation failed: {exc}",
                            **getattr(exc, "details", {})})
    queued = []
    for group, label in groups:
        try:
            queued.extend(job_to_payload(job) for job in install_group(group, db_path=db_path, now=now))
        except DuplicatePublication as exc:
            skipped.append({**label, "reason": str(exc)})
    return {"jobs": queued, "skipped": skipped}


def publish_saved_campaign(campaign, *, db_path, load_media_files, artifact_payloads_for_platform,
                           job_to_payload, now_fn=lambda: datetime.now(timezone.utc)):
    """Admit the exact saved drafts and complete media, then atomically link all posts.

    This legacy prepared-campaign entry point never generates copy or creates
    replacement posts. A shared post can keep multiple destinations only when
    every destination resolves to the same complete ordered artifact members.
    Partial existing links require guarded recovery, not automatic tail filling.
    """
    from myUtils import profiles, publish_orchestrator as po

    now = destination_pacing.utc_datetime(now_fn())
    with closing(destination_pacing.connect_readonly(db_path)) as conn:
        conn.execute("BEGIN")
        parent, _ = _fence(conn, campaign)
        parent_facts = _json(dict(parent))
        workspace = parent["workspace_id"]
        saved = [dict(row) for row in conn.execute(
            "SELECT * FROM campaign_posts WHERE campaign_id=? ORDER BY id", (campaign.id,))]
        linked = [row for row in saved if row["last_published_job_id"] is not None]
        publishable = [row for row in saved if profiles.platform_supports_direct_publish(row["platform"])]
        if linked:
            if any(row["last_published_job_id"] is None for row in publishable):
                raise PublicationGroupConflict("Existing campaign publication is incomplete; guarded recovery required")
            result = []
            membership = {}
            for post in publishable:
                row = conn.execute("SELECT * FROM publish_jobs WHERE id=?", (post["last_published_job_id"],)).fetchone()
                if row is None:
                    raise PublicationGroupConflict("Existing campaign job link is missing")
                payload = _object(row["payload_json"])
                targets = conn.execute("SELECT * FROM publish_job_targets WHERE job_id=? ORDER BY id", (row["id"],)).fetchall()
                expected = {f"account:{identifier}" for identifier in json.loads(post["account_ids_json"] or "[]")}
                if (row["workspace_id"] != workspace or row["profile_id"] != campaign.profile_id
                        or row["platform"] != post["platform"] or payload.get("campaignId") != campaign.id
                        or payload.get("campaignPostId") != post["id"] or payload.get("draft") != _object(post["draft_json"])
                        or payload.get("sheetRow") != _object(post["sheet_row_json"])
                        or len(targets) != len(expected) or {t["account_ref"] for t in targets} != expected
                        or any(t["file_ref"] != f"campaign_post:{post['id']}" or t["workspace_id"] != workspace for t in targets)):
                    raise PublicationGroupConflict("Existing campaign links/approved intent conflict; guarded review required")
                group_id, index, count = jobs._calendar_part(payload)
                if not group_id or count > media_compliance.MAX_PARTS:
                    raise PublicationGroupConflict("Existing saved campaign lacks complete ordered group evidence")
                for target in targets:
                    membership.setdefault((group_id, target["account_ref"]), []).append(
                        (index, count, destination_pacing.utc_datetime(target["schedule_at"])))
                result.append(job_to_payload(jobs._row_to_job(row)))
            for members in membership.values():
                members.sort()
                count = members[0][1]
                slots = [slot for _index, _count, slot in members]
                if (len(members) != count or [index for index, _count, _slot in members] != list(range(1, count + 1))
                        or any(total != count for _index, total, _slot in members)
                        or any(right - left < timedelta(minutes=max(30, po.MIN_GAP_MINUTES)) for left, right in zip(slots, slots[1:]))
                        or any(total > destination_pacing.effective_daily_cap(po.MAX_POSTS_PER_ACCOUNT_PER_DAY, 3)
                               for total in Counter(slot.replace(tzinfo=timezone.utc).astimezone(po._operator_timezone()).date()
                                                    for slot in slots).values())
                        or (_base_time(campaign, None) is not None and slots[0] < _base_time(campaign, None))):
                    raise PublicationGroupConflict("Existing saved campaign group is incomplete or its slots conflict")
            return {"jobs": result, "skipped": []}
        if parent["status"] != campaigns.CAMPAIGN_PREPARED:
            raise PublicationGroupConflict("Campaign must be completely prepared before publication")
        if (campaign.metadata or {}).get(campaigns.PREP_LEASE_KEY):
            raise PublicationGroupConflict("Campaign preparation is still owned by a worker")
    if not publishable:
        return {"jobs": [], "skipped": [{"platform": row["platform"], "reason": "content_only"} for row in saved]}
    profile = profiles.get_profile(campaign.profile_id, db_path=db_path)
    media_files = load_media_files(campaign.media_group_id, db_path=db_path)
    artifacts = [artifact.to_dict() for artifact in campaigns.list_campaign_artifacts(campaign.id, db_path=db_path)]
    by_account = {}
    for post in publishable:
        if post["workspace_id"] != workspace or post["status"] not in {campaigns.CAMPAIGN_POST_DRAFT, campaigns.CAMPAIGN_POST_READY}:
            raise PublicationGroupConflict("Saved post ownership/state is not publishable")
        ids = json.loads(post["account_ids_json"] or "[]")
        if not ids or len(set(ids)) != len(ids):
            raise PublicationGroupConflict("Saved post has missing/ambiguous destinations")
        for identifier in ids:
            by_account.setdefault(identifier, []).append(post)
    groups, member_artifacts, account_slots = [], {}, {}
    for identifier, posts in by_account.items():
        account = profiles.get_account(identifier, db_path=db_path)
        if any(post["platform"] != account.platform for post in posts):
            raise PublicationGroupConflict("Saved post platform does not match its destination")
        with closing(destination_pacing.connect_readonly(db_path)) as conn:
            conn.execute("BEGIN")
            workspace, identity, context, profile_facts = _context(conn, campaign, profile, account)
        prepared = po._prepared_post_groups(artifacts, account.platform, media_files,
            supports_multi_media=platform_capabilities.platform_supports_multi_media(account.platform),
            artifact_payloads_for_platform=artifact_payloads_for_platform, account_config=context["config"],
            backup_compatible=context["provider"] == "sociamonials")
        if len(prepared) != len(posts) or not 1 <= len(prepared) <= media_compliance.MAX_PARTS:
            raise PublicationGroupConflict("Saved drafts do not cover the complete ordered media set; preparation/review required")
        rows, items, files = _source_evidence(db_path, campaign, media_files, prepared, account.platform)
        payloads = []
        for index, (post, (values, file_ids)) in enumerate(zip(posts, prepared), 1):
            registered_ids = json.loads(post["file_record_ids_json"]) if post["file_record_ids_json"] else None
            if registered_ids is not None and registered_ids != file_ids:
                raise PublicationGroupConflict("Saved post source membership/order changed")
            previous = member_artifacts.setdefault(post["id"], values)
            if previous != values:
                raise PublicationGroupConflict("Shared saved post resolves to conflicting destination media variants")
            draft = _object(post["draft_json"])
            payload = {"campaignId": campaign.id, "campaignPostId": post["id"], "platform": post["platform"],
                       "draft": draft, "message": draft.get("message", ""), "sheetRow": _object(post["sheet_row_json"]),
                       "artifacts": copy.deepcopy(values), "partIndex": index, "partCount": len(posts)}
            if registered_ids is not None:
                payload["fileRecordIds"] = registered_ids
            publish_admission.validate_intent(payload, platform=account.platform,
                effective_provider=context["provider"], account_config=context["config"])
            payloads.append(payload)
        group_id = hashlib.sha256(_json({"savedCampaign": campaign.id,
            "postIds": [post["id"] for post in posts], "members": [_member_intent(payload) for payload in payloads]}).encode()).hexdigest()
        for index, payload in enumerate(payloads, 1):
            payload["publicationGroup"] = {"id": group_id, "index": index, "count": len(payloads)}
        group = PublicationGroup(campaign, identifier, workspace, identity, context["provider"], context["fingerprint"],
            profile_facts, group_id, tuple(payloads), tuple(None for _ in posts), _base_time(campaign, None), 0, rows, items, files,
            tuple(post["id"] for post in publishable))
        groups.append((group, posts))
    for _attempt in range(MAX_CALENDAR_RECHECKS):
        planned_bookings = {}
        with closing(destination_pacing.connect_readonly(db_path)) as conn:
            conn.execute("BEGIN")
            for group, posts in groups:
                keys, booked = _calendar(conn, group, now)
                _duplicates(conn, group, keys)
                taken = planned_bookings.setdefault(group.identity, booked)
                slots = _slots(group, taken, now)
                for post, slot in zip(posts, slots):
                    account_slots[(group.account_id, post["id"])] = slot
        for group, _posts in groups:
            _refresh_group_destination(group, db_path)
        specs = []
        for post in publishable:
            owners = [(group, posts) for group, posts in groups if any(row["id"] == post["id"] for row in posts)]
            payload = next(payload for payload in owners[0][0].payloads if payload["campaignPostId"] == post["id"])
            if any(next(p for p in group.payloads if p["campaignPostId"] == post["id"]) != payload for group, _ in owners):
                raise PublicationGroupConflict("Shared post has incompatible ordered group membership")
            spec = jobs.JobSpec(platform=post["platform"], payload=copy.deepcopy(payload), profile_id=campaign.profile_id,
                targets=[(f"account:{identifier}", f"campaign_post:{post['id']}", account_slots[(identifier, post["id"])])
                         for identifier in json.loads(post["account_ids_json"])],
                idempotency_key=f"campaign-{campaign.id}-post-{post['id']}")
            spec, snapshot = publish_admission.prepare_job_spec(spec, workspace_id=workspace, db_path=db_path)
            if not snapshot.ready:
                raise publish_admission.AdmissionError("Saved publication requires current verified destination/media evidence",
                                                       "destination_verification_required")
            account_facts = {fact["account_ref"]: fact for fact in json.loads(snapshot.facts_json)["accounts"]}
            if any(account_facts[group.account_ref].get("fingerprint") != group.configuration
                   or account_facts[group.account_ref].get("provider") != group.provider for group, _posts in owners):
                raise publish_admission.AdmissionError("Destination configuration changed after media planning", "configuration_changed")
            specs.append((post, spec, snapshot))
        retry_calendar = False
        with po.slot_reservation_lock(db_path), campaigns._connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                parent, _ = _fence(conn, campaign)
                current = [dict(row) for row in conn.execute("SELECT * FROM campaign_posts WHERE campaign_id=? ORDER BY id", (campaign.id,))]
                if _json(dict(parent)) != parent_facts or current != saved:
                    raise PublicationGroupConflict("Saved campaign/approved drafts changed after admission")
                checked = {}
                for group, posts in groups:
                    _validate_sources(conn, group)
                    keys, booked = _calendar(conn, group, now)
                    _duplicates(conn, group, keys)
                    slots = _slots(group, checked.setdefault(group.identity, booked), now)
                    if slots != [account_slots[(group.account_id, post["id"])] for post in posts]:
                        retry_calendar = True
                for _post, spec, snapshot in specs:
                    publish_admission.validate_admission_snapshot(conn, spec, snapshot)
                if retry_calendar:
                    conn.rollback()
                else:
                    result = [jobs.enqueue_job_in_transaction(conn, spec, workspace_id=workspace,
                        campaign_post_id=post["id"], admission_snapshot=snapshot, calendar_now=now)
                        for post, spec, snapshot in specs]
                    for post, job in zip(publishable, result):
                        row = conn.execute("SELECT status,last_published_job_id FROM campaign_posts WHERE id=?", (post["id"],)).fetchone()
                        count = conn.execute("SELECT COUNT(*) FROM publish_job_targets WHERE job_id=?", (job.id,)).fetchone()[0]
                        if row is None or tuple(row) != (campaigns.CAMPAIGN_POST_QUEUED, job.id) or count != job.total_targets:
                            raise PublicationGroupConflict("Saved campaign installation/link is incomplete")
                    conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if not retry_calendar:
            return {"jobs": [job_to_payload(job) for job in result], "skipped": []}
    raise destination_pacing.PacingRefused("Destination calendar kept changing; retry the unchanged saved request")
