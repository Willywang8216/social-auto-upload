"""Exact, integrity-backed restoration before publication admission.

Only descriptors supplied by the caller are considered. This module does not
search by basename/source ID, create provider objects, or write queue state.
Dependency state requires the additive migration and an already-existing DB.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from myUtils import media_integrity, media_remote_storage, outbound_http

DEFAULT_RESERVE_BYTES = 512 * 1024 * 1024
MAX_RESTORE_BYTES = 512 * 1024 * 1024
OVERALL_DEADLINE_SECONDS = 240.0
BACKEND_DEADLINE_SECONDS = 90.0
# A paced Drive stat can spend >20s resolving an exact registered path before
# returning its checksum. Keep one attempt, inside the SAME 240s total budget.
PROBE_DEADLINE_SECONDS = 45.0
MAX_SOURCES = 8
COOLDOWNS = {"quota": 1800, "auth": 900, "missing": 900, "transport": 120,
             "integrity": 3600, "identity": 3600, "capacity": 60, "configuration": 300,
             "verification": 300}


class PrerequisiteUnavailable(RuntimeError):
    """Safe no-post prerequisite failure; never contains URLs or credentials."""

    def __init__(self, category: str, *, retry_after_seconds: float | None = None,
                 scope_key: str | None = None, cached_wait: bool = False):
        self.retryable = category not in {"missing", "integrity", "identity"}
        # A fractional remaining hold must never schedule a probe before expiry.
        self.retry_after_seconds = max(1, math.ceil(retry_after_seconds or COOLDOWNS.get(category, 120)))
        self.details = {"prerequisiteFailure": True, "dependencyCategory": category,
                        "stage": "waiting_capacity" if category == "capacity" else "waiting_dependency"}
        if scope_key:
            self.details["dependencyScope"] = scope_key
        if cached_wait:
            self.details["cachedDependencyWait"] = True
        reason = ": checksum evidence is not yet available" if category == "verification" else ""
        super().__init__(f"media prerequisite unavailable: {category}{reason}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    default=str).encode("utf-8")).hexdigest()


def rclone_configuration_evidence(backend: dict) -> dict:
    """Read stable configured client selectors only; never refresh/create OAuth.

    An explicit client is configuration evidence, not ownership or authorization
    health claim. Token refreshes do not invalidate quota holds. No raw client
    IDs, secrets, tokens, remote names or paths are returned or persisted.
    """
    selector = os.environ.get("RCLONE_CONFIG", "")
    remote = str(backend.get("bucket") or "")
    facts = {"config_selector": selector, "remote": remote}
    owned = False
    if selector and remote:
        try:
            parser = configparser.RawConfigParser()
            with Path(selector).open("r", encoding="utf-8") as stream:
                text = stream.read(1024 * 1024 + 1)
            if len(text) > 1024 * 1024:
                raise ValueError("configuration size limit")
            parser.read_string(text)
            if parser.has_section(remote):
                fields = {key: parser.get(remote, key, fallback="") for key in
                          ("type", "client_id", "client_secret", "scope", "root_folder_id",
                           "team_drive", "service_account_file")}
                facts.update(fields)
                owned = bool(fields["client_id"] and fields["client_secret"])
        except (OSError, ValueError, configparser.Error):
            facts["unreadable"] = True
    return {"config_fingerprint": _hash(facts), "explicit_oauth_client_configured": owned}


def dependency_identity(source: dict) -> tuple[str, str]:
    """Provider/remote/credential-selector/account/workspace scope + hashed config.

    Credentials affect the fingerprint but neither raw secrets nor URLs enter
    the persistence table. A public URL scope does not share a Drive hold.
    """
    backend = dict(source.get("backend") or {})
    if backend:
        provider = str(backend.get("provider") or "").lower()
        selector = backend.get("credential_selector") or backend.get("credentialSelector") or backend.get("slug") or backend.get("id")
        scope = {key: backend.get(key) for key in ("bucket", "endpoint", "account_id", "workspace_id")}
        scope.update(provider=provider, selector=selector or _hash(backend.get("access_key", "")))
        config = dict(backend)
        if provider == "rclone":
            config.update(rclone_configuration_evidence(backend))
    else:
        provider = "public_https"
        try:
            host = urlsplit(str(source.get("public_url") or "")).hostname
        except ValueError:
            raise PrerequisiteUnavailable("identity") from None
        scope = {"provider": provider, "host": host,
                 "account_id": source.get("account_id"), "workspace_id": source.get("workspace_id")}
        config = scope  # URL tokens/individual objects must not reset a host hold.
    return f"{provider}:{_hash(scope)}", _hash(config)


def _object_identity(source: dict) -> tuple[str, str]:
    scope, fingerprint = dependency_identity(source)
    return scope + ":object:" + _hash(source.get("storage_key") or source.get("public_url")), _hash(
        [fingerprint, source.get("remote_checksum"), source.get("expected_size_bytes")])


@contextmanager
def _database(db_path: Path, *, readonly=False):
    """URI rw/ro prevents sqlite's implicit empty-production-DB creation."""
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() +
                                     ("?mode=ro" if readonly else "?mode=rw"),
                                     uri=True, timeout=2.0)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
    except sqlite3.Error:
        raise PrerequisiteUnavailable("configuration") from None


def dependency_status(source: dict, *, db_path: Path) -> dict:
    """Read-only projection, with no provider calls or schema creation."""
    scope, fingerprint = dependency_identity(source)
    with _database(db_path, readonly=True) as conn:
        row = conn.execute("SELECT * FROM publish_dependency_states WHERE scope_key=?", (scope,)).fetchone()
    if row is None or row["config_fingerprint"] != fingerprint:
        return {"scope_key": scope, "status": "unknown", "config_fingerprint": fingerprint}
    return dict(row)


def _admit(db_path: Path, scope: str, fingerprint: str, kind: str) -> bool:
    """CAS configuration reset; claim a single bounded probe after a hold."""
    now = _now()
    with _database(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""INSERT INTO publish_dependency_states
            (scope_key,config_fingerprint,kind,status,updated_at) VALUES (?,?,?,'unknown',?)
            ON CONFLICT(scope_key) DO UPDATE SET config_fingerprint=excluded.config_fingerprint,
            kind=excluded.kind,status='unknown',reason=NULL,blocked_until=NULL,
            last_checked_at=NULL,updated_at=excluded.updated_at
            WHERE publish_dependency_states.config_fingerprint != excluded.config_fingerprint""",
                     (scope, fingerprint, kind, _iso(now)))
        row = conn.execute("SELECT * FROM publish_dependency_states WHERE scope_key=?", (scope,)).fetchone()
        if row["status"] in {"blocked", "probing"}:
            try:
                until = datetime.fromisoformat(row["blocked_until"])
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                until = now + timedelta(seconds=COOLDOWNS.get(row["reason"], 120))
            if until > now:
                raise PrerequisiteUnavailable(row["reason"] or "transport", cached_wait=True,
                                              retry_after_seconds=(until - now).total_seconds(), scope_key=scope)
            conn.execute("""UPDATE publish_dependency_states SET status='probing',blocked_until=?,updated_at=?
                WHERE scope_key=? AND config_fingerprint=?""",
                         (_iso(now + timedelta(seconds=PROBE_DEADLINE_SECONDS + 5)), _iso(now), scope, fingerprint))
            return True
    return False


def _record_state(db_path: Path, scope: str, fingerprint: str, category: str | None) -> bool:
    """An old in-flight configuration cannot overwrite a newer configuration."""
    now = _now()
    until = _iso(now + timedelta(seconds=COOLDOWNS[category])) if category else None
    with _database(db_path) as conn:
        changed = conn.execute("""UPDATE publish_dependency_states SET status=?,reason=?,
            blocked_until=?,last_checked_at=?,updated_at=? WHERE scope_key=? AND config_fingerprint=?
            AND (? IS NOT NULL OR status != 'blocked')""",
                               ("blocked" if category else "healthy", category, until, _iso(now),
                                _iso(now), scope, fingerprint, category)).rowcount
    return bool(changed)


def classify_failure(exc: Exception) -> str:
    """Inspect provider evidence internally; persist only a fixed reason code."""
    if isinstance(exc, PrerequisiteUnavailable):
        return exc.details["dependencyCategory"]
    if isinstance(exc, media_integrity.MediaIntegrityError):
        return "integrity"
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    code = ""
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "").lower()
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    text = (str(getattr(exc, "stderr", "") or "") + " " + str(exc) + " " + code).lower()
    if status == 429 or any(word in text for word in ("quota", "ratelimit", "rate limit", "dailylimit", "too many requests")):
        return "quota"
    if status in {401, 403} or any(word in text for word in ("unauthorized", "accessdenied", "access denied", "invalid_grant", "authentication", "invalid token")):
        return "auth"
    if status == 404 or isinstance(exc, FileNotFoundError) or any(word in text for word in ("not found", "nosuchkey", "objectnotfound", "directory not found")):
        return "missing"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "capacity"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or "budget exhausted" in text or "time budget" in text:
        return "transport"
    if any(word in text for word in ("incomplete", "empty", "byte limit", "invalid content-length")):
        return "integrity"
    if isinstance(exc, outbound_http.OutboundUrlError):
        return "identity"
    return "transport"


def _failure_is_object_scoped(source: dict, exc: Exception, category: str) -> bool:
    """Only explicit file-quota evidence narrows a quota hold; ambiguity stays shared.

    Drive downloadQuotaExceeded applies to the selected file. HTTP 429 and
    account/client rate limits must still block every object on that remote.
    Inspect stderr only, never the CalledProcessError command/path (an object
    named like an error code is not provider evidence).
    """
    if category in {"missing", "identity", "integrity", "verification"}:
        return True
    if (category != "quota" or (source.get("backend") or {}).get("provider") != "rclone"
            or not isinstance(exc, subprocess.CalledProcessError)):
        return False
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(response, dict):
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    text = str(exc.stderr or "").lower()
    shared = (r"\b(?:quotaexceeded|dailylimitexceeded|userratelimitexceeded|ratelimitexceeded|sharingratelimitexceeded)\b"
              r"|\brate[\s_-]*limit\b|\btoo many requests\b"
              r"|\b(?:http(?:/\d(?:\.\d)?)?\s*[:=]?\s*|error\s+)429\b")
    return (str(status) != "429" and bool(re.search(r"\bdownloadquotaexceeded\b", text))
            and not re.search(shared, text))


def check_capacity(destination: Path, *, db_path: Path, input_bytes: int,
                   reserve_bytes: int = DEFAULT_RESERVE_BYTES) -> None:
    """Reserve input/staging/derivatives plus SQLite and release headroom.

    Admission is conservative, not a cross-process disk reservation allocator;
    callers must retain their preparation concurrency limit.
    """
    if input_bytes < 0 or reserve_bytes < 0:
        raise PrerequisiteUnavailable("identity")
    parent = Path(destination).parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    database = Path(db_path)
    if not database.is_file():
        raise PrerequisiteUnavailable("configuration")
    sqlite_headroom = max(64 * 1024 * 1024, database.stat().st_size * 2)
    required = input_bytes * 3 + reserve_bytes + sqlite_headroom
    volume = [parent.stat().st_dev, str(parent.resolve().anchor)]
    scope = "capacity:" + _hash(volume)
    fingerprint = _hash([volume, reserve_bytes, "restore-v1"])
    _admit(db_path, scope, fingerprint, "capacity")
    if (shutil.disk_usage(parent).free < required or
            shutil.disk_usage(database.parent).free < sqlite_headroom):
        _record_state(db_path, scope, fingerprint, "capacity")
        raise PrerequisiteUnavailable("capacity", scope_key=scope)
    _record_state(db_path, scope, fingerprint, None)


def probe_source(source: dict, *, total_deadline: float = PROBE_DEADLINE_SECONDS) -> dict:
    """One exact bounded read-only availability probe; no fuzzy fallback."""
    if source.get("backend") and source.get("storage_key"):
        return media_remote_storage.stat_from_backend(source["backend"], source["storage_key"],
                                                      total_deadline=total_deadline)
    if source.get("public_url") and not source.get("backend"):
        return outbound_http.probe_public_url(source["public_url"], total_deadline=total_deadline)
    raise PrerequisiteUnavailable("identity")


def source_availability(source: dict, *, db_path: Path,
                        total_deadline: float = PROBE_DEADLINE_SECONDS) -> dict:
    """Hold-aware bounded read-only provider probe, NOT a publish-ready claim.

    For health without provider calls use ``dependency_status`` instead.
    """
    scope, fingerprint = dependency_identity(source)
    object_scope, object_fingerprint = _object_identity(source)
    try:
        _admit(db_path, scope, fingerprint, "media_restore")
        _admit(db_path, object_scope, object_fingerprint, "media_object")
        facts = probe_source(source, total_deadline=min(total_deadline, PROBE_DEADLINE_SECONDS))
        if not _record_state(db_path, scope, fingerprint, None):
            raise PrerequisiteUnavailable("configuration")
        _record_state(db_path, object_scope, object_fingerprint, None)
        return {"available": True, "integrity_verified": False, **facts}
    except Exception as exc:
        category = classify_failure(exc)
        target, token = (object_scope, object_fingerprint) if _failure_is_object_scoped(source, exc, category) else (scope, fingerprint)
        if not isinstance(exc, PrerequisiteUnavailable):
            _record_state(db_path, target, token, category)
        failure = exc if isinstance(exc, PrerequisiteUnavailable) else PrerequisiteUnavailable(category, scope_key=target)
        return {"available": False, "integrity_verified": False, "retryable": failure.retryable,
                "retry_after_seconds": failure.retry_after_seconds, **failure.details}


def _accepted_media(path: Path, content_type: str = "") -> None:
    """Fail closed on HTML/JSON or unrecognized media after hashing bytes."""
    kind = str(content_type).lower().split(";", 1)[0].strip()
    if kind and not kind.startswith(("video/", "image/", "audio/", "application/octet-stream")):
        raise media_integrity.MediaIntegrityError("restored response is not media")
    with path.open("rb") as stream:
        header = stream.read(32)
    accepted = (header.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a",
                                   b"\x1aE\xdf\xa3", b"OggS", b"ID3", b"BM")) or
                (len(header) >= 12 and header[4:8] == b"ftyp") or
                (header.startswith(b"RIFF") and header[8:12] in {b"WEBP", b"AVI ", b"WAVE"}))
    if not accepted:
        raise media_integrity.MediaIntegrityError("restored bytes have no accepted media signature")


def _assert_recorded_facts(record: dict, digest: str, size: int) -> None:
    expected = media_integrity.expected_sha256(record, required=False)
    if expected is not None and expected != digest:
        raise media_integrity.MediaIntegrityError("registered checksum conflict")
    for fields in (record, record.get("metadata") or {}):
        known = fields.get("size_bytes")
        if known is not None and (type(known) is not int or known != size):
            raise media_integrity.MediaIntegrityError("registered byte count conflict")


def _persist_verified(db_path: Path, destination: Path, artifact: dict, digest: str, size: int,
                      source: dict) -> None:
    """Record actual facts on exact registered rows, never on a raw parent ID."""
    with _database(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        registered = False
        columns = {row[1] for row in conn.execute("PRAGMA table_info(file_records)")}

        def exact_remote_rows():
            backend = source.get("backend") or {}
            if {"storage_backend_id", "storage_key"} <= columns and backend.get("id") is not None:
                return conn.execute("SELECT * FROM file_records WHERE storage_backend_id=? AND storage_key=?",
                                    (backend["id"], source.get("storage_key"))).fetchall()
            if "storage_cdn_url" in columns and source.get("public_url"):
                return conn.execute("SELECT * FROM file_records WHERE storage_cdn_url=?",
                                    (source["public_url"],)).fetchall()
            return []

        file_id = artifact.get("file_record_id")
        if file_id is not None:
            row = conn.execute("SELECT * FROM file_records WHERE id=?", (file_id,)).fetchone()
            if row is None or (row["file_path"] not in {str(destination), str(destination.resolve())} and
                               file_id not in {matched["id"] for matched in exact_remote_rows()}):
                raise PrerequisiteUnavailable("identity")
            _assert_recorded_facts({"sha256": row["sha256"], "size_bytes": row["size_bytes"]}, digest, size)
            conn.execute("UPDATE file_records SET sha256=?,size_bytes=? WHERE id=?", (digest, size, file_id))
            registered = True
        else:
            rows = conn.execute("SELECT * FROM file_records WHERE file_path IN (?,?)",
                                (str(destination), str(destination.resolve()))).fetchall()
            if not rows:
                rows = exact_remote_rows()
            for row in rows:
                _assert_recorded_facts({"sha256": row["sha256"], "size_bytes": row["size_bytes"]}, digest, size)
                conn.execute("UPDATE file_records SET sha256=?,size_bytes=? WHERE id=?", (digest, size, row["id"]))
                registered = True
        if artifact.get("id") is not None and artifact.get("campaign_id") is not None:
            row = conn.execute("SELECT * FROM campaign_artifacts WHERE id=? AND campaign_id=?",
                               (artifact["id"], artifact["campaign_id"])).fetchone()
            if row is None or row["local_path"] not in {str(destination), str(destination.resolve())}:
                raise PrerequisiteUnavailable("identity")
            metadata = json.loads(row["metadata_json"] or "{}")
            _assert_recorded_facts({"metadata": metadata}, digest, size)
            metadata.update(sha256=digest, size_bytes=size)
            conn.execute("UPDATE campaign_artifacts SET metadata_json=? WHERE id=? AND metadata_json=?",
                         (json.dumps(metadata, sort_keys=True), row["id"], row["metadata_json"]))
            registered = True
        if not registered:
            raise PrerequisiteUnavailable("identity")


def restore_artifact(destination: Path, artifact: dict, sources: list[dict], *, db_path: Path,
                     reserve_bytes: int = DEFAULT_RESERVE_BYTES,
                     required_checksum: bool = False,
                     total_deadline: float = OVERALL_DEADLINE_SECONDS,
                     probe_deadline_seconds: float = PROBE_DEADLINE_SECONDS) -> dict:
    """Verify an exact registered source in bounded staging, record, then expose.

    Restoration always needs an artifact SHA or explicit provider checksum;
    size/availability alone never become identity proof. ``required_checksum``
    also applies the shared strict artifact contract when no provider proof is
    supplied. ``file_record_id`` means this exact file, NOT its raw source.
    Otherwise an exact registered path/remote record or (id,campaign_id,local_path) row is required
    for recording executed verification before installation.
    """
    # Pace the same configured client within a finite single-probe bound. This
    # never enlarges the overall deadline, adds provider attempts, clears a live
    # cooldown or skips integrity/registration/CAS checks.
    if type(probe_deadline_seconds) not in {int, float} or not 0 < probe_deadline_seconds <= 45:
        raise PrerequisiteUnavailable("identity")
    destination = Path(destination)
    deadline = time.monotonic() + total_deadline
    expected = media_integrity.expected_sha256(artifact, required=False)
    if not sources or len(sources) > MAX_SOURCES or total_deadline <= 0:
        raise PrerequisiteUnavailable("identity")
    failures = []
    seen = set()
    if not expected:
        # An exact registered backend can provide an algorithm-labelled hash;
        # an unhashed public link cannot. Try stronger evidence first, but every
        # alternative must still pass the SAME identity/hash/size checks.
        sources = sorted(sources, key=lambda source: (
            0 if source.get("remote_checksum") else 1 if source.get("backend") else 2))
    for source in sources:
        if time.monotonic() >= deadline:
            failures.append(PrerequisiteUnavailable("transport"))
            break
        scope, fingerprint = dependency_identity(source)
        object_scope, object_fingerprint = _object_identity(source)
        if (object_scope, object_fingerprint) in seen:
            continue
        seen.add((object_scope, object_fingerprint))
        temporary = None
        try:
            if bool(source.get("backend")) == bool(source.get("public_url")) or (
                    source.get("backend") and not source.get("storage_key")):
                raise PrerequisiteUnavailable("identity")
            backend = source.get("backend") or {}
            if backend and (backend.get("provider") not in {"rclone", "share", "do_spaces", "s3", "r2"} or
                            (backend.get("provider") == "rclone" and not backend.get("bucket"))):
                raise PrerequisiteUnavailable("identity")
            proof = source.get("remote_checksum")
            sizes = [value for value in (source.get("expected_size_bytes"), artifact.get("size_bytes"),
                                        (artifact.get("metadata") or {}).get("size_bytes")) if value is not None]
            if any(type(value) is not int or value <= 0 for value in sizes) or len(set(sizes)) > 1:
                raise media_integrity.MediaIntegrityError("registered byte counts conflict")
            check_capacity(destination, db_path=db_path,
                           input_bytes=sizes[0] if sizes else MAX_RESTORE_BYTES, reserve_bytes=reserve_bytes)
            needs_probe = _admit(db_path, scope, fingerprint, "media_restore")
            object_probe = _admit(db_path, object_scope, object_fingerprint, "media_object")
            if needs_probe or object_probe or (source.get("backend", {}).get("provider") == "rclone"):
                facts = probe_source(source, total_deadline=min(probe_deadline_seconds, deadline - time.monotonic()))
                if facts.get("size_bytes") is not None:
                    sizes.append(facts["size_bytes"])
                proof = proof or facts.get("remote_checksum")
                _record_state(db_path, scope, fingerprint, None)
                _record_state(db_path, object_scope, object_fingerprint, None)
            if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in sizes) or len(set(sizes)) > 1:
                raise media_integrity.MediaIntegrityError("registered byte counts conflict")
            if not expected and not proof:
                # Missing proof can be retried, but NEVER mask a known byte
                # conflict or authorize unverified content to be downloaded.
                _record_state(db_path, object_scope, object_fingerprint, "verification")
                raise PrerequisiteUnavailable("verification", scope_key=object_scope)
            size_bound = min(sizes[0] if sizes else MAX_RESTORE_BYTES, MAX_RESTORE_BYTES)
            check_capacity(destination, db_path=db_path, input_bytes=size_bound, reserve_bytes=reserve_bytes)
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".restore", dir=destination.parent)
            os.close(fd)
            temporary = Path(name)
            received = {}
            remaining = min(BACKEND_DEADLINE_SECONDS, deadline - time.monotonic())
            if source.get("backend"):
                media_remote_storage.download_from_backend(source["backend"], source["storage_key"], temporary,
                                                           total_deadline=remaining, max_bytes=size_bound, facts=received)
            else:
                outbound_http.download_public_file(source["public_url"], temporary, total_deadline=remaining,
                                                   max_bytes=size_bound, facts=received)
            before = temporary.stat()
            stamp = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            size = before.st_size
            verified = media_integrity.verify_artifact(temporary, artifact, required=required_checksum and not proof)
            if proof:
                provider_facts = media_integrity.verify_provider_checksum(temporary, proof)
                if provider_facts != verified:
                    raise media_integrity.MediaIntegrityError("restored bytes changed between integrity checks")
            digest = verified["sha256"]
            if verified["size_bytes"] != size or (sizes and size != sizes[0]):
                raise media_integrity.MediaIntegrityError("restored byte count mismatch")
            _accepted_media(temporary, received.get("content_type", ""))
            if time.monotonic() > deadline:
                raise TimeoutError("restoration budget exhausted")
            # Persist against the destination's identity while verifying the staged bytes.
            if not _record_state(db_path, scope, fingerprint, None):
                raise PrerequisiteUnavailable("configuration")
            if not _record_state(db_path, object_scope, object_fingerprint, None):
                raise PrerequisiteUnavailable("configuration")
            _persist_verified(db_path, destination, artifact, digest, size, source)
            after = temporary.stat()
            if stamp != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise media_integrity.MediaIntegrityError("restored bytes changed before installation")
            with temporary.open("r+b") as stream:  # Windows fsync requires a writable handle.
                os.fsync(stream.fileno())
            if time.monotonic() > deadline:
                raise TimeoutError("restoration budget exhausted")
            temporary.replace(destination)
            artifact.update(sha256=digest, size_bytes=size)
            return {"path": destination, "sha256": digest, "size_bytes": size, "source": source}
        except Exception as exc:
            category = classify_failure(exc)
            target, token = (object_scope, object_fingerprint) if _failure_is_object_scoped(source, exc, category) else (scope, fingerprint)
            if not isinstance(exc, PrerequisiteUnavailable):
                _record_state(db_path, target, token, category)
            failures.append(exc if isinstance(exc, PrerequisiteUnavailable) else PrerequisiteUnavailable(category, scope_key=target))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    # Permanent failures win; an actual alternative failure must not be hidden
    # by an earlier cached hold and thereby escape the finite prerequisite budget.
    permanent = [failure for failure in failures if not failure.retryable]
    attempted = [failure for failure in failures if not failure.details.get("cachedDependencyWait")]
    failure = (permanent or attempted or failures)[0]
    raise failure from None
