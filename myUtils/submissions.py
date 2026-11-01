"""Atomic, durable admission to the asynchronous campaign preparation queue.

No draft generation, media processing or provider calls happen here. A single
SQLite transaction owns the submission receipt and every newly created row;
retrying an intent never resets an existing campaign or publish target.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from myUtils import campaign_media_prep, content_rating
from myUtils import campaigns as campaign_store
from myUtils import media_groups as media_group_store
from myUtils import profiles as profile_registry
from myUtils import publish_orchestrator

LEGACY_SCOPE = "legacy"


class SubmissionConflict(ValueError):
    """An existing durable receipt belongs to a different intent."""


@contextmanager
def _connect(db_path: Path):
    # The caller must supply a migrated database; never bootstrap a live DB as
    # a side effect of submitting. Explicit transactions also work on 3.11.
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _unique_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result and result[key] != value:
            raise ValueError(f"Conflicting JSON keys: {key!r}")
        result[key] = value
    return result


def _mapping(value, name: str, *, nullable: bool = False) -> dict | None:
    if value is None:
        return None if nullable else {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    try:
        # Account-keyed maps arrive with either integer or JSON string keys.
        # Convert before sorting, reject ambiguous keys, and detach all nested
        # values so a caller cannot mutate the intent after it is hashed.
        return json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False),
            object_pairs_hook=_unique_object,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain unambiguous JSON values: {exc}") from exc


def _ids(values, name: str) -> list[int]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a list of account/profile IDs")
    result = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"Invalid {name} ID: {value!r}")
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError(f"Invalid {name} ID: {value!r}") from exc
        if number <= 0:
            raise ValueError(f"Invalid {name} ID: {value!r}")
        result.append(number)
    return sorted(set(result))


def _path_text(value) -> str:
    if not isinstance(value, (str, Path)) or not str(value).strip() or "\x00" in str(value):
        raise ValueError("Media paths must be non-empty file paths")
    return str(value).strip()


def _workspace(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("workspace_id must be a non-empty string or None")
    return value.strip()


def _normalize_request(*, profile_ids, selected_account_ids, media_file_paths,
                       brief, options, schedule, account_drafts,
                       tiktok_post_settings, source, workspace_id) -> dict:
    profiles = _ids(profile_ids, "profile_ids")
    if not profiles:
        raise ValueError("At least one profile must be selected")
    if not isinstance(media_file_paths, (list, tuple)) or not media_file_paths:
        raise ValueError("At least one media file is required")
    if brief is not None and not isinstance(brief, str):
        raise ValueError("brief must be a string")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    return {
        "profileIds": profiles,
        "selectedAccountIds": (
            _ids(selected_account_ids, "selected_account_ids") or None
            if selected_account_ids is not None else None
        ),
        # File order affects carousels and the primary video. Only exact
        # duplicates are collapsed (media_group_items is unique per file).
        "mediaFilePaths": list(dict.fromkeys(_path_text(p) for p in media_file_paths)),
        "brief": brief or "",
        "options": _mapping(options, "options"),
        "schedule": _mapping(schedule, "schedule", nullable=True) or None,
        "accountDrafts": _mapping(account_drafts, "account_drafts"),
        "tiktokPostSettings": _mapping(tiktok_post_settings, "tiktok_post_settings"),
        "source": source.strip(),
        "workspaceId": _workspace(workspace_id),
    }


def _saved_result(conn, scope: str, submission_id: str, request_hash: str):
    row = conn.execute(
        "SELECT request_hash, result_json FROM publish_submissions "
        "WHERE scope = ? AND submission_id = ?",
        (scope, submission_id),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise SubmissionConflict("submission_id is already used for a different publish intent")
    return publish_orchestrator.SubmitResult(**json.loads(row["result_json"]))


def _registered_file(conn, path: str, workspace_id: str | None):
    query = "SELECT id FROM file_records WHERE file_path = ?"
    params = [path]
    if workspace_id is not None:
        query += " AND workspace_id = ?"
        params.append(workspace_id)
    row = conn.execute(query + " ORDER BY id LIMIT 1", params).fetchone()
    if row is None and workspace_id is not None:
        if conn.execute(
            "SELECT 1 FROM file_records WHERE file_path = ? LIMIT 1", (path,)
        ).fetchone():
            raise ValueError(f"Media file is not available in this workspace: {path!r}")
    return int(row["id"]) if row is not None else None


@dataclass(frozen=True)
class _FileSpec:
    path: str
    registered_id: int | None
    filesize: float | None = None


def _has_restore_mapping(conn, file_id: int) -> bool:
    row = conn.execute(
        "SELECT f.storage_key, f.storage_cdn_url, b.id AS backend_id "
        "FROM file_records f LEFT JOIN storage_backends b ON b.id=f.storage_backend_id WHERE f.id=?",
        (file_id,),
    ).fetchone()
    if row is None:
        return False
    if row["backend_id"] is not None and str(row["storage_key"] or "").strip():
        return True
    # A registered public URL is also an exact restoration mapping. Admission
    # only validates its shape; it must never fetch the remote media itself.
    from ipaddress import ip_address
    from urllib.parse import urlsplit
    try:
        url = urlsplit(str(row["storage_cdn_url"] or ""))
        host = (url.hostname or "").lower().rstrip(".")
        if url.scheme != "https" or not host or url.username or url.password:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        try:
            return ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def _inspect_file(conn, path: str, workspace_id: str | None) -> _FileSpec:
    existing = _registered_file(conn, path, workspace_id)
    restorable = existing is not None and _has_restore_mapping(conn, existing)
    if path.startswith("https://") and restorable:
        return _FileSpec(path, existing)
    resolved = campaign_media_prep._resolve_video_file_path_safely(path)
    if resolved is None:
        raise ValueError(f"Unsafe media path: {path!r}")
    try:
        if resolved.is_file():
            size = resolved.stat().st_size
            if size <= 0:
                raise ValueError(f"Media file is empty: {path!r}")
            return _FileSpec(path, existing, round(float(size) / (1024 * 1024), 2))
        if resolved.exists():
            raise ValueError(f"Media path is not a file: {path!r}")
    except OSError as exc:
        raise ValueError(f"Cannot read media file: {path!r}") from exc
    if restorable:
        return _FileSpec(path, existing)
    raise ValueError(f"Media file is not registered or present locally with a usable restore mapping: {path!r}")


def _insert_file(conn, spec: _FileSpec, workspace_id: str | None) -> int:
    # BEGIN IMMEDIATE serializes this lookup+insert even though legacy
    # file_records has no unique constraint on file_path. Never update old rows.
    existing = _registered_file(conn, spec.path, workspace_id)
    if existing is not None:
        return existing
    if spec.registered_id is not None:
        raise ValueError(f"Registered media changed during submission: {spec.path!r}")
    cursor = conn.execute(
        "INSERT INTO file_records (filename, filesize, file_path, workspace_id) "
        "VALUES (?, ?, ?, ?)",
        (Path(spec.path).name, spec.filesize, spec.path, workspace_id),
    )
    return int(cursor.lastrowid)


def ensure_file_record_for_path(file_path: str, *, db_path: Path,
                                workspace_id: str | None = None) -> int:
    """Register a safe local path, or reuse a known remote/local file record."""
    path = _path_text(file_path)
    workspace_id = _workspace(workspace_id)
    with _connect(db_path) as conn:
        spec = _inspect_file(conn, path, workspace_id)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return _insert_file(conn, spec, workspace_id)


def _campaign_plans(conn, request: dict, rating: str, roles: list[str]):
    """Validate every destination before any inserts, using the writer snapshot.

    Selection matches the orchestrator's _resolve_accounts: enabled accounts,
    optionally intersected with selected IDs, ordered by platform/account name.
    SQL stays on this connection rather than using committing store helpers.
    """
    workspace_id = request["workspaceId"]
    profiles = []
    for profile_id in request["profileIds"]:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None or (workspace_id is not None and row["workspace_id"] != workspace_id):
            raise ValueError(f"Profile not found: id={profile_id}")
        profiles.append(profile_registry._row_to_profile(row))

    selected = set(request["selectedAccountIds"] or [])
    for account_id in selected:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None or (workspace_id is not None and row["workspace_id"] != workspace_id):
            raise ValueError(f"Account not found: id={account_id}")
        if row["profile_id"] not in request["profileIds"]:
            raise ValueError(f"Account {account_id} does not belong to a selected profile")
        if row["platform"] not in profile_registry.SUPPORTED_PLATFORMS:
            raise ValueError(f"Unsupported platform: {row['platform']!r}")

    has_video = media_group_store.ROLE_VIDEO in roles
    has_image = media_group_store.ROLE_IMAGE in roles
    if not has_video and not has_image:
        raise ValueError("Selected media contains no supported image or video files")
    plans = []
    skipped = []
    for profile in profiles:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE profile_id = ? AND enabled = 1 "
            "ORDER BY platform, account_name", (profile.id,),
        ).fetchall()
        accounts = []
        for row in rows:
            if selected and row["id"] not in selected:
                continue
            if workspace_id is not None and row["workspace_id"] != workspace_id:
                raise ValueError(f"Account not found in this workspace: id={row['id']}")
            if row["platform"] not in profile_registry.SUPPORTED_PLATFORMS:
                raise ValueError(f"Unsupported platform: {row['platform']!r}")
            accounts.append(profile_registry._row_to_account(row))
        if not accounts:
            skipped.append({"profileId": profile.id, "reason": "no_enabled_accounts"})
            continue
        try:
            accounts = content_rating.restrict_accounts(accounts, rating)
        except ValueError:
            skipped.append({"profileId": profile.id, "reason": "nsfw_no_adult_safe_account"})
            continue

        compatible_accounts = []
        for account in accounts:
            reason = None
            if account.platform == "youtube" and not has_video:
                reason = "YouTube publishing requires video media; this selection contains no video."
            elif (account.platform == "tiktok" and not has_video
                  and not bool(request["options"].get("tiktokDirectPost"))):
                reason = "TikTok photo publishing requires Direct Post; enable it or remove this destination."
            if reason:
                skipped.append({
                    "profileId": profile.id,
                    "accountId": account.id,
                    "accountName": account.nickname or account.account_name,
                    "platform": account.platform,
                    "reason": reason,
                })
            else:
                compatible_accounts.append(account)
        if not compatible_accounts:
            continue
        request_data = publish_orchestrator._request_data_for_options(
            brief=request["brief"], options=request["options"], profile=profile,
        )
        plans.append((profile, compatible_accounts, request_data))
    return plans, skipped


def submit_publish(
    *,
    profile_ids: list[int],
    selected_account_ids: list[int] | None,
    media_file_paths: list[str],
    brief: str,
    options: dict,
    schedule: dict | None,
    account_drafts: dict | None,
    tiktok_post_settings: dict | None = None,
    db_path: Path,
    submission_id: str | None = None,
    source: str = "publish-center",
    workspace_id: str | None = None,
) -> publish_orchestrator.SubmitResult:
    """Atomically admit an intent, returning preparing campaign IDs and jobs=[].

    The receipt is durable across retries/restarts. Reusing an explicit ID with
    different intent raises ValueError. Without an ID, canonical full-request
    SHA256 is the ID: deliberately publishing the same intent again requires a
    new explicit ID. Profile/account sets and JSON key order are normalized;
    media order and all copy, language, rating, schedule and media options remain
    significant. ``workspace_id=None`` retains legacy unscoped entity reads but
    uses its own non-null receipt scope, distinct from every named workspace.
    """
    request = _normalize_request(
        profile_ids=profile_ids, selected_account_ids=selected_account_ids,
        media_file_paths=media_file_paths, brief=brief, options=options,
        schedule=schedule, account_drafts=account_drafts,
        tiktok_post_settings=tiktok_post_settings, source=source,
        workspace_id=workspace_id,
    )
    request_json = _json(request)
    request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    if submission_id is None:
        submission_id = f"sha256:{request_hash}"
    elif not isinstance(submission_id, str) or not submission_id.strip():
        raise ValueError("submission_id must be a non-empty string or None")
    else:
        submission_id = submission_id.strip()
    if len(submission_id) > 256:
        raise ValueError("submission_id must not exceed 256 characters")
    workspace_id = request["workspaceId"]
    scope = LEGACY_SCOPE if workspace_id is None else f"workspace:{workspace_id}"
    paths = request["mediaFilePaths"]
    roles = [publish_orchestrator._media_role_for_path(path) for path in paths]
    rating = content_rating.rating_for_media(paths, explicit=request["options"].get("sfwFlag"))

    with _connect(db_path) as conn:
        saved = _saved_result(conn, scope, submission_id, request_hash)
        if saved is not None:
            return saved
        # Filesystem inspection happens before taking the writer lock. Known
        # remote files require no stat, download, ffprobe, or provider request.
        files = [_inspect_file(conn, path, workspace_id) for path in paths]
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            # A concurrent identical request may have committed while we waited.
            saved = _saved_result(conn, scope, submission_id, request_hash)
            if saved is not None:
                return saved
            plans, skipped = _campaign_plans(conn, request, rating, roles)
            file_ids = [_insert_file(conn, spec, workspace_id) for spec in files]
            primary_video = next(
                (fid for fid, role in zip(file_ids, roles) if role == media_group_store.ROLE_VIDEO),
                None,
            )
            cursor = conn.execute(
                "INSERT INTO media_groups (name, notes, primary_video_file_id, workspace_id) "
                "VALUES (?, ?, ?, ?)",
                (f"publish-center-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
                 request["brief"][:255].strip(), primary_video, workspace_id),
            )
            media_group_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO media_group_items "
                "(media_group_id, file_record_id, role, sort_order, workspace_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [(media_group_id, fid, role, index, workspace_id)
                 for index, (fid, role) in enumerate(zip(file_ids, roles))],
            )
            campaign_ids = []
            for profile, accounts, request_data in plans:
                selected_ids = [account.id for account in accounts]
                prep_request = dict(request)
                prep_request.update(selectedAccountIds=selected_ids,
                                    contentRating=rating, submissionId=submission_id)
                metadata = {
                    "title": request_data.get("title", ""),
                    "notes": request_data.get("notes", ""),
                    "requestedPlatforms": sorted({account.platform for account in accounts}),
                    "source": request["source"],
                    "contentRating": rating,
                    "submissionId": submission_id,
                    campaign_store.PREP_REQUEST_KEY: prep_request,
                }
                cursor = conn.execute(
                    "INSERT INTO campaigns "
                    "(profile_id, media_group_id, status, selected_account_ids_json, "
                    "metadata_json, workspace_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (profile.id, media_group_id, campaign_store.CAMPAIGN_PREPARING,
                     _json(selected_ids), _json(metadata), workspace_id),
                )
                campaign_ids.append(int(cursor.lastrowid))
            result = publish_orchestrator.SubmitResult(campaign_ids=campaign_ids, jobs=[], skipped=skipped)
            conn.execute(
                "INSERT INTO publish_submissions "
                "(scope, submission_id, workspace_id, request_hash, request_json, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (scope, submission_id, workspace_id, request_hash, request_json, _json(asdict(result))),
            )
        return result
