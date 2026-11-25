"""One prospective admission boundary. No encoding, provider writes or schema DDL.

prepare_job_spec performs expensive byte/media checks BEFORE enqueue's SQL
transaction. validate_admission_snapshot only rereads SQL facts and stats. The
process-local sealed snapshot is deliberately not a JSON publish authorization.
Unknown prerequisites may be stored, but never dispatched to provider creation.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from myUtils import account_identity as identity, media_integrity, publish_readiness, text_limits

ADMISSION_VERSION = 1
# ONLY these new runtime keys are excluded from immutable intent hashing. Do not
# exclude draft/options/artifacts/SHA/provenance or existing generation fields.
RUNTIME_PAYLOAD_KEYS = frozenset({"publishReadiness", "_publishAdmissionVersion"})
_SEAL_KEY = secrets.token_bytes(32)


class AdmissionError(ValueError):
    retryable = False

    def __init__(self, reason, code="invalid_intent"):
        super().__init__(reason)
        self.details = {"reasonCode": code, "textReviewRequired": code == "unsupported_option",
                        "retrySafe": True}


@dataclass(frozen=True)
class AdmissionSnapshot:
    """Opaque local capability; safe digests/stat facts, never credentials."""
    intent_digest: str
    workspace_id: str | None
    facts_json: str
    files: tuple
    seal: str

    @property
    def ready(self):
        return json.loads(self.facts_json)["ready"]


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _payload_intent(payload):
    return {k: v for k, v in payload.items() if k not in RUNTIME_PAYLOAD_KEYS}


def _spec_intent(spec, *, placeholder=False):
    payload = copy.deepcopy(_payload_intent(spec.payload))
    targets = [list(t) for t in spec.targets]
    if placeholder:
        payload["campaignPostId"] = 0
        targets = [[a, "campaign_post:0", s] for a, _, s in targets]
    # Dates are normalized independently of runtime freshness.
    from myUtils.jobs import _schedule_canonical
    targets = [[a, f, _schedule_canonical(s)] for a, f, s in targets]
    return {"platform": spec.platform, "profile_id": spec.profile_id, "payload": payload,
            "targets": targets, "extra_canonical": spec.extra_canonical}


def _seal(intent, workspace, facts, files):
    return hmac.new(_SEAL_KEY, _json([intent, workspace, facts, files]).encode(), hashlib.sha256).hexdigest()


def _stat(path):
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def supports_alt_text(platform, media_kind, provider):
    if media_kind != "image":
        return False
    if provider == "sociamonials":
        return platform in {"twitter", "facebook", "instagram", "threads", "bluesky", "pinterest"}
    # Bluesky has an actual native image-alt mapper; current X API/browser routes do not.
    return platform == "bluesky"


def supports_first_comment(platform, provider):
    if provider == "sociamonials":
        from myUtils.sociamonials_policy import FIRST_COMMENT_NETWORKS
        from myUtils.sociamonials_fallback import _platform_network
        return _platform_network(platform) in FIRST_COMMENT_NETWORKS
    return platform in {"facebook", "instagram", "youtube"}


def prospective_draft_options(draft, platform, media_kind, effective_provider):
    """Shape generator output only; approved overrides go through rejection."""
    from myUtils.content_generator import prospective_draft_options as shape
    return shape(draft, platform=platform, media_kind=media_kind, effective_provider=effective_provider)


def _approved_text_aliases(sources, keys):
    """Resolve one approved value, never precedence-select conflicting aliases."""
    values = []
    for source in sources:
        if not isinstance(source, dict):
            raise AdmissionError("Approved text source must be an object", "unsupported_option")
        for key in keys:
            if key not in source:
                continue
            value = source[key]
            if not isinstance(value, str):
                raise AdmissionError(f"{key} must be text", "unsupported_option")
            text_limits._text(value)  # Reject invalid Unicode without normalizing copy.
            values.append(value)
    if values and any(value != values[0] for value in values[1:]):
        raise AdmissionError(f"{keys[0]} has conflicting approved aliases; review required", "unsupported_option")
    return values[0] if values else None


def approved_first_comment(payload):
    from myUtils.publish_media_guard import is_context_artifact, is_support_artifact
    main = [a for a in payload.get("artifacts") or []
            if not is_context_artifact(a) and not is_support_artifact(a)]
    return _approved_text_aliases(
        [payload, payload.get("draft") or {}, *main, *[a.get("metadata") or {} for a in main]],
        ("firstComment", "first_comment"))


def approved_alt_text(payload, artifact=None):
    """Post/draft aliases define a default; each artifact has its own override.

    Different images may have different descriptions. Artifact/metadata aliases
    represent the same image value and must agree, including explicit empties.
    """
    default = _approved_text_aliases([payload, payload.get("draft") or {}], ("altText", "alt_text"))
    if artifact is None:
        return default
    value = _approved_text_aliases([artifact, artifact.get("metadata") or {}], ("altText", "alt_text"))
    return value if value is not None else default


def _validate_approved_aliases(payload, main):
    comment = approved_first_comment(payload)
    approved_alt_text(payload)  # Validate the default even when all images override it.
    aliases = {}
    for artifact in main:
        alt = approved_alt_text(payload, artifact)
        key = artifact.get("local_path") or artifact.get("public_url")
        if key:
            if key in aliases and aliases[key] != alt:
                raise AdmissionError("altText has conflicting approved media aliases; review required", "unsupported_option")
            aliases[key] = alt
    return comment


_NATIVE_OPTIONS = {
    # Native publishers do NOT consume arbitrary networkOptions. Only the
    # actual per-post TikTok settings object has a maintained native mapper.
    "tiktok": {"title", "privacyLevel", "disableComment", "disableDuet", "disableStitch",
               "consentChecked", "contentDisclosure"},
}


def validate_intent(payload, *, platform, effective_provider="direct", account_config=None):
    """Typed copy/accessibility/options gate, even when dependencies are unknown."""
    from myUtils import publish_media_guard as guard
    if not isinstance(payload, dict):
        raise AdmissionError("Publishing payload must be an object")
    text_limits.field_limit(platform)
    if effective_provider not in {None, "direct", "sociamonials"}:
        raise AdmissionError("Effective publishing provider is unsupported", "unsupported_provider")
    draft = payload.get("draft") or {}
    if not isinstance(draft, dict):
        raise AdmissionError("draft must be an object")
    if platform == "telegram" and effective_provider == "direct" and account_config is not None:
        from myUtils import native_destinations
        try:
            native_destinations.assert_effective_destination(platform, account_config, payload)
        except ValueError as exc:
            raise AdmissionError(str(exc), "destination_changed") from exc
    artifacts = payload.get("artifacts") or []
    if not isinstance(artifacts, list) or any(not isinstance(a, dict) for a in artifacts):
        raise AdmissionError("artifacts must be a list of objects")
    for artifact in artifacts:
        if artifact.get("metadata") is not None and not isinstance(artifact["metadata"], dict):
            raise AdmissionError("Artifact metadata must be an object")
        media_integrity.expected_sha256(artifact)
    main = [a for a in artifacts if not guard.is_context_artifact(a) and not guard.is_support_artifact(a)]
    kinds = {guard.media_kind(a) for a in main}
    if None in kinds:
        raise AdmissionError("Artifact media kind is unsupported")
    kind = next(iter(kinds)) if len(kinds) == 1 else None
    comment = _validate_approved_aliases(payload, main)
    if comment is not None:
        text_limits.validate_message(comment, platform, field="first_comment")
    text_limits.validate_post_text(payload, platform, media_kind=kind)
    sources = [payload, draft, *[a.get("metadata") or {} for a in main], *main]
    for source in sources:
        for key in ("altText", "alt_text"):
            if key in source:
                if not isinstance(source[key], str):
                    raise AdmissionError(f"{key} must be text", "unsupported_option")
                if source[key] and effective_provider is not None and not supports_alt_text(platform, kind, effective_provider):
                    raise AdmissionError(f"{platform}/{effective_provider} {kind or 'text'} cannot preserve approved {key}; review required", "unsupported_option")
        for key in ("firstComment", "first_comment"):
            if source.get(key) and effective_provider is not None and not supports_first_comment(platform, effective_provider):
                raise AdmissionError(f"{platform}/{effective_provider} cannot preserve approved {key}; review required", "unsupported_option")
    if effective_provider == "sociamonials":
        from myUtils import sociamonials_fallback as adapter, sociamonials_policy as policy
        policy.map_intent(payload, network=adapter._platform_network(platform),
                          message=text_limits.composed_message(payload, platform),
                          account_config=account_config, media_kind=kind, media_count=len(main))
    elif effective_provider == "direct":
        allowed = _NATIVE_OPTIONS.get(platform, set())
        for source in (payload, draft):
            for key in ("networkOptions", "network_options", "tiktokPostSettings"):
                if key not in source:
                    continue
                options = source[key]
                if not isinstance(options, dict):
                    raise AdmissionError(f"{key} must be an object", "unsupported_option")
                if platform in options:
                    if set(options) != {platform} or not isinstance(options[platform], dict):
                        raise AdmissionError(f"{key} selects a different destination", "unsupported_option")
                    options = options[platform]
                if key != "tiktokPostSettings" and options:
                    raise AdmissionError(f"Native {platform} does not consume {key}; approved options need review", "unsupported_option")
                if set(options) - allowed:
                    raise AdmissionError(f"{key} unsupported on {platform}: {sorted(set(options) - allowed)}", "unsupported_option")
                for name, value in options.items():
                    if name in {"disableComment", "disableDuet", "disableStitch", "isAigc", "brandContentToggle",
                                "brandOrganicToggle", "madeForKids", "notifySubscribers", "containsSyntheticMedia", "consentChecked"} and type(value) is not bool:
                        raise AdmissionError(f"{key}.{name} must be a literal boolean", "unsupported_option")
    return kind


def _row(conn, table, identifier):
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None


@dataclass(frozen=True)
class BackupVerification:
    """Process-local, read-bound secondary authorization; never a JSON flag."""
    account_id: int
    primary_fingerprint: str
    effective_fingerprint: str
    proof_json: str
    seal: str


def _backup_context(account, settings):
    from myUtils.sociamonials_fallback import resolve_mapping
    config = publish_readiness._config(account)
    shadow = {key: publish_readiness._value(account, key) for key in
              ("id", "profile_id", "workspace_id", "platform", "enabled", "cookie_path")}
    selected = resolve_mapping(shadow["id"], platform=shadow["platform"], account={"config": config}, settings=settings)
    shadow["config"] = {**config, "publishingProvider": "sociamonials", "sociamonials": selected}
    return publish_readiness._context(shadow, settings)


def _backup_seal(identifier, primary, effective, proof):
    return hmac.new(_SEAL_KEY, _json(["verified-backup", identifier, primary, effective, proof]).encode(), hashlib.sha256).hexdigest()


def _issue_backup_verification(account, settings, verified, before):
    """Only backup_preparation's actual provider-read boundary calls this."""
    if before != publish_readiness._context(account, settings):
        raise AdmissionError("Backup configuration changed during verification", "configuration_changed")
    if verified.get("verified") is not True or (verified.get("permissions") or {}).get("posts.publish_direct") is not True:
        raise AdmissionError("Fresh exact backup authorization is unverified", "destination_verification_required")
    effective = _backup_context(account, settings)
    proof = {**verified, "live_check": True, "source": "provider_verification",
             "config_fingerprint": effective["fingerprint"],
             "permissions": {**verified["permissions"], "posts.read": True, "posts.create": True, "profiles.read": True}}
    proof_json = _json(publish_readiness._validate_proof(effective, proof, max_age_seconds=publish_readiness.DEFAULT_MAX_AGE_SECONDS))
    identifier = publish_readiness._value(account, "id")
    return BackupVerification(identifier, before["fingerprint"], effective["fingerprint"], proof_json,
        _backup_seal(identifier, before["fingerprint"], effective["fingerprint"], proof_json))


def _checked_backup(account, settings, verification):
    if not isinstance(verification, BackupVerification) or not hmac.compare_digest(verification.seal,
            _backup_seal(verification.account_id, verification.primary_fingerprint, verification.effective_fingerprint, verification.proof_json)):
        raise AdmissionError("Backup admission requires authentic read-bound verification", "snapshot_invalid")
    primary = publish_readiness._context(account, settings)
    effective = _backup_context(account, settings)
    if (verification.account_id != publish_readiness._value(account, "id")
            or verification.primary_fingerprint != primary["fingerprint"]
            or verification.effective_fingerprint != effective["fingerprint"]):
        raise AdmissionError("Exact backup route/credential selection changed", "configuration_changed")
    proof = publish_readiness._validate_proof(effective, json.loads(verification.proof_json),
        max_age_seconds=publish_readiness.DEFAULT_MAX_AGE_SECONDS)
    return effective, proof


def _backup_verifications(snapshot):
    if not isinstance(snapshot, AdmissionSnapshot) or not hmac.compare_digest(snapshot.seal,
            _seal(snapshot.intent_digest, snapshot.workspace_id, snapshot.facts_json, snapshot.files)):
        raise AdmissionError("Admission requires an authentic local snapshot", "snapshot_invalid")
    return {ref: BackupVerification(**value) for ref, value in json.loads(snapshot.facts_json).get("backup_verifications", {}).items()}


def _account_facts(conn, ref, spec, workspace, backup_verification=None):
    identifier = identity.structured_account_id(ref)
    account = _row(conn, "accounts", identifier) if identifier else None
    if account is None:
        validate_intent(spec.payload, platform=spec.platform)
        return None, None, {"account_ref": ref, "account_id": identifier, "account_digest": None,
                            "reason": "configuration_required", "ready": False}
    if workspace != account.get("workspace_id") or (spec.profile_id is not None and spec.profile_id != account.get("profile_id")):
        raise AdmissionError("Destination workspace/profile ownership does not match intent", "ownership_changed")
    if identity._platform(account) != spec.platform:
        raise AdmissionError("Destination platform does not match intent", "ownership_changed")
    profile = _row(conn, "profiles", account.get("profile_id"))
    if profile is None or profile.get("workspace_id") != workspace:
        raise AdmissionError("Destination profile/workspace no longer exists", "ownership_changed")
    settings = identity.json_object(profile.get("settings_json") or "{}")
    # Only digests go into the snapshot; selected credentials never leave context.
    try:
        context = publish_readiness._context(account, settings)
    except (ValueError, TypeError, KeyError):
        validate_intent(spec.payload, platform=spec.platform,
                        effective_provider=str((identity._config(account) or {}).get("publishingProvider") or "direct"))
        return account, settings, {"account_ref": ref, "account_id": identifier,
                "account_digest": _digest(account), "profile_digest": _digest(profile),
                "fingerprint": None, "reason": "configuration_required", "ready": False}
    if backup_verification is not None:
        context, backup_proof = _checked_backup(account, settings, backup_verification)
    validate_intent(spec.payload, platform=spec.platform, effective_provider=context["provider"], account_config=context["config"])
    proof_row = None
    dependency = None
    try:
        proof_row = conn.execute("SELECT * FROM publish_destination_readiness WHERE account_id=?", (identifier,)).fetchone()
        scope = publish_readiness.destination_scope_key(account, settings)
        if backup_verification is not None:
            scope += ":backup"
        dependency = conn.execute("SELECT * FROM publish_dependency_states WHERE scope_key=?", (scope,)).fetchone()
    except sqlite3.OperationalError:
        pass
    if backup_verification is None:
        readiness = publish_readiness._project(account, context, publish_readiness._native_health(account, context["config"]), proof_row, dependency)
    else:
        if (dependency and dependency["status"] in {"blocked", "cooldown", "waiting_dependency", "held"}
                and publish_readiness._time(dependency["blocked_until"])
                and publish_readiness._time(dependency["blocked_until"]) > publish_readiness._now()):
            raise AdmissionError("Exact backup readiness is held; no provider create allowed", "destination_verification_required")
        proof_row = {"workspace_id": workspace, "provider": "sociamonials", "config_fingerprint": context["fingerprint"],
            "status": "ready", "evidence_json": _json(backup_proof), "checked_at": backup_proof["verified_at"], "expires_at": backup_proof["expires_at"]}
        readiness = publish_readiness._project(account, context, publish_readiness._native_health(account, context["config"]), proof_row)
    from myUtils import publish_media_guard as guard
    main = [a for a in spec.payload.get("artifacts") or [] if not guard.is_context_artifact(a) and not guard.is_support_artifact(a)]
    required = {guard.media_kind(a) for a in main} or {"text"}
    if sum(guard.media_kind(a) == "image" for a in main) > 1:
        required.add("carousel")
    if approved_first_comment(spec.payload):
        required.add("first_comment")
    if readiness["ready"] and not required <= set(readiness["evidence"]["capabilities"]):
        raise AdmissionError("Fresh route proof lacks required intent capabilities", "capability_missing")
    return account, settings, {"account_ref": ref, "account_id": identifier,
        "account_digest": _digest(account), "profile_digest": _digest(profile), "fingerprint": context["fingerprint"],
        "provider": context["provider"], "ready": readiness["ready"], "reason": readiness["reasonCode"]} | (
        {"backup_dependency_digest": _digest(dict(dependency)) if dependency else None} if backup_verification is not None else {})


def _registry_facts(conn, payload, workspace):
    """Exact registered artifacts + variant/provenance. No basename matching."""
    from myUtils import publish_media_guard as guard
    facts, complete = [], True
    campaign_id = payload.get("campaignId")
    if campaign_id is not None:
        campaign = _row(conn, "campaigns", campaign_id)
        if not campaign or campaign.get("workspace_id") != workspace:
            raise AdmissionError("Campaign workspace ownership changed", "ownership_changed")
        facts.append(["campaigns", campaign_id, _digest(campaign)])
    for artifact in payload.get("artifacts") or []:
        if guard.is_context_artifact(artifact):
            continue
        identifier = artifact.get("id", artifact.get("artifact_id"))
        registered = _row(conn, "campaign_artifacts", identifier) if identifier else None
        if not registered and campaign_id is not None and artifact.get("local_path"):
            matches = conn.execute("SELECT * FROM campaign_artifacts WHERE campaign_id=? AND local_path=?",
                                   (campaign_id, artifact["local_path"])).fetchall()
            if len(matches) == 1:
                registered, identifier = dict(matches[0]), matches[0]["id"]
        if registered:
            if campaign_id != registered.get("campaign_id"):
                raise AdmissionError("Registered artifact belongs to another campaign", "ownership_changed")
            metadata = identity.json_object(registered.get("metadata_json") or "{}")
            # All original metadata includes part membership, source-vs-output
            # hashes, variant and source intervals. Never compare just filename.
            for key in ("artifact_kind", "local_path", "public_url", "source_file_record_id"):
                if artifact.get(key) != registered.get(key):
                    raise AdmissionError(f"Registered artifact {key} changed", "artifact_changed")
            if (artifact.get("metadata") or {}) != metadata:
                raise AdmissionError("Registered artifact variant/provenance changed", "artifact_changed")
            media_integrity.expected_sha256({**artifact, "metadata": metadata}, required=True)
            facts.append(["campaign_artifacts", identifier, _digest(registered)])
            if guard.media_kind(artifact) == "video" and guard._has_derived_identity(artifact):
                group = metadata.get("preparation_group")
                if not group:
                    raise AdmissionError("Legacy ordered parts lack a provable group/interval; explicit recovery required", "incomplete_parts")
                rows = conn.execute("SELECT * FROM campaign_artifacts WHERE campaign_id=?", (campaign_id,)).fetchall()
                members = []
                for row in rows:
                    md = identity.json_object(row["metadata_json"] or "{}")
                    if md.get("preparation_group") == group:
                        members.append({**dict(row), "metadata": md})
                        facts.append(["campaign_artifacts", row["id"], _digest(dict(row))])
                from myUtils.media_compliance import select_prepared_parts
                selected = select_prepared_parts(members, payload.get("platform") or identity._value(artifact, "platform") or "")
                if len(selected) != metadata["part_count"]:
                    raise AdmissionError("Registered ordered part set is incomplete", "incomplete_parts")
        else:
            # Direct file-record intent: no derivative may masquerade as source.
            record_id = artifact.get("file_record_id", artifact.get("source_file_record_id"))
            record = _row(conn, "file_records", record_id) if record_id else None
            if not record:
                complete = False
                continue
            if record.get("workspace_id") != workspace:
                raise AdmissionError("Registered file belongs to another workspace", "ownership_changed")
            from conf import BASE_DIR
            registered_path = Path(record["file_path"])
            if not registered_path.is_absolute():
                registered_path = Path(BASE_DIR) / "videoFile" / registered_path
            if not artifact.get("local_path") or registered_path.resolve() != Path(artifact["local_path"]).resolve():
                raise AdmissionError("Registered file is not the selected artifact/variant", "artifact_changed")
            if guard._has_derived_identity(artifact):
                raise AdmissionError("Prepared variant requires exact campaign artifact registration", "artifact_changed")
            expected = media_integrity.expected_sha256(artifact)
            if record.get("sha256") and expected and expected != record["sha256"]:
                raise AdmissionError("Artifact SHA differs from the approved registered file", "artifact_changed")
            if not record.get("sha256") or expected != record["sha256"] or artifact.get("size_bytes", (artifact.get("metadata") or {}).get("size_bytes")) != record.get("size_bytes"):
                complete = False
            facts.append(["file_records", record_id, _digest(record)])
    return facts, complete


def _post_binding(conn, spec, *, placeholder=False):
    post_id = spec.payload.get("campaignPostId")
    if post_id is None:
        if any(str(f).startswith("campaign_post:") for _, f, _ in spec.targets):
            raise AdmissionError("Campaign post reference has no bound campaignPostId")
        return
    if type(post_id) is not int or post_id < 0 or any(f != f"campaign_post:{post_id}" for _, f, _ in spec.targets):
        raise AdmissionError("Campaign post identity/reference mismatch")
    if post_id == 0:
        if not spec.payload.get("campaignId"):
            raise AdmissionError("Planned post requires an exact campaign")
        return
    post = _row(conn, "campaign_posts", post_id)
    if not post or post.get("campaign_id") != spec.payload.get("campaignId") or post.get("platform") != spec.platform:
        raise AdmissionError("Actual SQL post does not belong to the planned campaign", "ownership_changed")
    account_ids = json.loads(post.get("account_ids_json") or "[]")
    if any(identity.structured_account_id(a) not in account_ids for a, _, _ in spec.targets):
        raise AdmissionError("Actual SQL post does not belong to the planned account", "ownership_changed")
    if identity.json_object(post.get("draft_json") or "{}") != (spec.payload.get("draft") or {}):
        raise AdmissionError("Actual SQL post changed approved draft/options", "intent_changed")


def prepare_job_spec(spec, *, workspace_id=None, db_path=None, backup_verifications=None):
    """Return normalized JobSpec + sealed outside-TX evidence, never trust ready JSON.

    Bare store-only references retain queue semantics, with waiting_configuration.
    Registered local bytes must validate; missing/raw media waits for owned fenced
    preparation. Already-prepared invalid media cannot silently become raw input.
    """
    from myUtils import jobs, publish_media_guard as guard, media_compliance
    payload = copy.deepcopy(spec.payload)
    for key in RUNTIME_PAYLOAD_KEYS:
        payload.pop(key, None)
    normalized = replace(spec, platform=identity._platform({"platform": spec.platform}), payload=payload)
    validate_intent(payload, platform=normalized.platform, effective_provider=None)
    if not normalized.targets or any(not isinstance(a, str) or not a or not isinstance(f, str) or not f for a, f, _ in normalized.targets):
        raise AdmissionError("A publication requires exact non-empty target references")
    path = jobs._resolve_db_path(db_path)
    facts = {"accounts": [], "registry": [], "ready": False, "placeholder": payload.get("campaignPostId") == 0}
    backup_verifications = backup_verifications or {}
    refs = {ref for ref, _file, _slot in normalized.targets}
    if (not isinstance(backup_verifications, dict) or set(backup_verifications) - refs
            or any(not isinstance(value, BackupVerification) for value in backup_verifications.values())):
        raise AdmissionError("Backup admission cannot use caller JSON authorization", "snapshot_invalid")
    if backup_verifications:
        facts["backup_verifications"] = {ref: dict(vars(value)) for ref, value in backup_verifications.items()}
    conn = None
    try:
        conn = publish_readiness._read_connection(path)
        conn.execute("BEGIN")
        for ref in dict.fromkeys(a for a, _, _ in normalized.targets):
            account, settings, account_fact = _account_facts(conn, ref, normalized, workspace_id, backup_verifications.get(ref))
            facts["accounts"].append(account_fact)
        _post_binding(conn, normalized)
        facts["registry"], registered = _registry_facts(conn, {**payload, "platform": normalized.platform}, workspace_id)
    except (sqlite3.OperationalError, OSError):
        validate_intent(payload, platform=normalized.platform)
        registered = False
    finally:
        if conn:
            conn.close()
    files, media_ready = [], registered
    artifacts = [a for a in payload.get("artifacts") or [] if not guard.is_context_artifact(a)]
    # A raw reference without artifacts is preparation work, not a text post.
    if not artifacts and any(f not in {"", "text", "text-only"} and not f.startswith("campaign_post:") for _, f, _ in normalized.targets):
        media_ready = False
    for artifact in artifacts:
        local = artifact.get("local_path")
        if not local or not Path(local).is_file():
            media_ready = False
            continue
        before = _stat(local)
        actual = media_integrity.verify_artifact(local, artifact)
        files.append((str(Path(local).resolve()), _stat(local), actual["sha256"], actual["size_bytes"]))
        if before != files[-1][1]:
            raise AdmissionError("Media changed during admission", "artifact_changed")
    if media_ready:
        for af in facts["accounts"]:
            account = None
            if af.get("account_digest"):
                read = publish_readiness._read_connection(path)
                try:
                    account = _row(read, "accounts", af["account_id"])
                finally:
                    read.close()
            if account:
                try:
                    guard.validate_payload({**payload, "_preserveApprovedText": True}, platform=normalized.platform,
                        account_config=publish_readiness._config(account), backup_compatible=af.get("provider") == "sociamonials")
                except media_compliance.MediaPreparationRequired:
                    if any(guard._has_derived_identity(a) for a in artifacts):
                        raise AdmissionError("Prepared generation is invalid; explicit selected recovery required", "invalid_prepared_generation")
                    media_ready = False
        for path_value, stamp, _, _ in files:
            if _stat(path_value) != stamp:
                raise AdmissionError("Media changed during probe", "artifact_changed")
    facts["ready"] = bool(facts["accounts"]) and all(a["ready"] for a in facts["accounts"]) and media_ready
    phase = "ready" if facts["ready"] else "waiting_configuration" if not facts["accounts"] or any(not a["ready"] for a in facts["accounts"]) else "preparing"
    payload["_publishAdmissionVersion"] = ADMISSION_VERSION
    payload["publishReadiness"] = {"ready": facts["ready"], "phase": phase,
        "reasonCode": "ready" if facts["ready"] else "media_preparation_required" if phase == "preparing" else "destination_verification_required"}
    digest = _digest(_spec_intent(normalized))
    facts_json, file_tuple = _json(facts), tuple(files)
    snapshot = AdmissionSnapshot(digest, workspace_id, facts_json, file_tuple,
                                 _seal(digest, workspace_id, facts_json, file_tuple))
    return normalized, snapshot


def validate_admission_snapshot(conn, spec, snapshot):
    """SQL/stat/CAS-only install check; NEVER provider reads, hashing or probing."""
    if not isinstance(snapshot, AdmissionSnapshot) or not hmac.compare_digest(snapshot.seal,
            _seal(snapshot.intent_digest, snapshot.workspace_id, snapshot.facts_json, snapshot.files)):
        raise AdmissionError("Admission requires an authentic local snapshot", "snapshot_invalid")
    facts = json.loads(snapshot.facts_json)
    if _digest(_spec_intent(spec, placeholder=facts["placeholder"])) != snapshot.intent_digest:
        raise AdmissionError("Approved intent changed after admission", "intent_changed")
    if spec.payload.get("_publishAdmissionVersion") != ADMISSION_VERSION or spec.payload.get("publishReadiness", {}).get("ready") is not facts["ready"]:
        raise AdmissionError("Runtime readiness does not match authoritative admission", "snapshot_invalid")
    _post_binding(conn, spec)
    for table, identifier, digest in facts["registry"]:
        row = _row(conn, table, identifier)
        if row is None or _digest(row) != digest:
            raise AdmissionError("Registered media/campaign ownership changed after verification", "artifact_changed")
    backup_verifications = _backup_verifications(snapshot)
    for old in facts["accounts"]:
        _, _, current = _account_facts(conn, old["account_ref"], spec, snapshot.workspace_id, backup_verifications.get(old["account_ref"]))
        if current != old:
            raise AdmissionError("Destination configuration/grants/proof changed; prepare again", "configuration_changed")
    for path, stamp, _, _ in snapshot.files:
        try:
            if _stat(path) != tuple(stamp):
                raise AdmissionError("Verified bytes changed before installation", "artifact_changed")
        except OSError as exc:
            raise AdmissionError("Verified bytes disappeared before installation", "artifact_changed") from exc


def validate_retry_plan(job, target, plan, *, db_path, workspace_id=None):
    """Original working payload + current failure + real fresh route/bytes gate.

    A client's resolved_permanent flag is never evidence. Application still MUST
    go through preprocess_failed_targets.apply_plan's fingerprint/receipt/CAS and
    complete-part provenance gates, scoped to precisely this failed target.
    """
    from myUtils import jobs, publish_media_guard as guard, prepared_recovery
    if not isinstance(plan, dict) or plan.get("target_id") != target.id or target.job_id != job.id:
        raise AdmissionError("Retry requires one maintained plan for the original target")
    if target.status != "failed" or plan.get("original_status", "failed") != "failed":
        raise AdmissionError("Retry admission is failed-only; cancelled/blocked need explicit review")
    if not plan.get("expected_fingerprint") or not isinstance(plan.get("prepared_payloads"), list) or not plan["prepared_payloads"]:
        raise AdmissionError("Retry requires a maintained complete preprocessing plan")
    # Reload authoritative SQL, not caller-provided Job/Target objects.
    with publish_readiness._read_connection(db_path) as conn:
        live_job = jobs.get_job(job.id, workspace_id=workspace_id, db_path=db_path)
        live_target = jobs.get_target(target.id, db_path=db_path)
        if live_target != target or live_job != job:
            raise AdmissionError("Original target/failure changed; regenerate plan", "intent_changed")
        row = conn.execute("SELECT * FROM publish_job_targets WHERE id=?", (target.id,)).fetchone()
        jobs._check_resubmission(row, {"failed"})
    original = copy.deepcopy(job.payload)
    original.update((target.checkpoint or {}).get("payload") or {})
    parts = plan["prepared_payloads"]
    mutable = {"artifacts", "preparedPart", "preparedPost", "partIndex", "partCount", "preparedImageBatch", "_preserveApprovedText"}
    for part in parts:
        if any(part.get(k) != original.get(k) for k in (set(part) | set(original)) - mutable - RUNTIME_PAYLOAD_KEYS):
            raise AdmissionError("Retry changed approved copy/options", "intent_changed")
        spec = jobs.JobSpec(platform=job.platform, payload=part,
                            targets=[(target.account_ref, target.file_ref, target.schedule_at)], profile_id=job.profile_id)
        normalized, snapshot = prepare_job_spec(spec, workspace_id=workspace_id, db_path=db_path)
        if not snapshot.ready:
            raise AdmissionError("Retry cause is not resolved by current destination/media evidence", "retry_cause_unresolved")
    outputs = [a for p in parts for a in p.get("artifacts") or [] if guard.media_kind(a) == "video"]
    if outputs:
        prepared_recovery.validate_video_provenance(original, outputs, platform=job.platform, probe_sources=True)
