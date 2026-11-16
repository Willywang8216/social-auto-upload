"""Isolated fault injection for bounded exact restoration and additive schema."""
from __future__ import annotations

import base64
import hashlib
import importlib
import time
import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from db import createTable
from myUtils import media_integrity, media_remote_storage, media_restore, outbound_http, rclone_storage

MEDIA = b"\x00\x00\x00\x18ftypmp42" + b"m" * 128
SHA = hashlib.sha256(MEDIA).hexdigest()
URL = "https://cdn.example/exact/part-1.mp4?token=not-a-real-token"
DRIVE = {"backend": {"provider": "rclone", "bucket": "owned-drive", "endpoint": "registered-root",
                     "id": 7, "workspace_id": "one", "secret_key": "synthetic-secret"},
         "storage_key": "registered/part-1.mp4", "expected_size_bytes": len(MEDIA)}
PUBLIC = {"public_url": URL, "expected_size_bytes": len(MEDIA)}


@pytest.fixture
def registered(tmp_path):
    db = tmp_path / "isolated.db"
    destination = tmp_path / "exact-part.mp4"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(createTable.FILE_RECORDS)
        conn.execute(createTable.PUBLISH_DEPENDENCY_STATES)
        conn.execute("CREATE TABLE queue_evidence(id INTEGER PRIMARY KEY, payload TEXT)")
        conn.execute("INSERT INTO queue_evidence VALUES (57, 'accepted:receipt:unchanged')")
        conn.execute("INSERT INTO file_records(filename,file_path,filesize) VALUES (?,?,?)",
                     (destination.name, str(destination), 1.25))  # legacy MB is not a byte fact
    return db, destination


def _download(monkeypatch, payload=MEDIA, content_type="video/mp4"):
    calls = []

    def download(url, destination, **kwargs):
        calls.append((url, kwargs))
        Path(destination).write_bytes(payload)
        kwargs.get("facts", {}).update(content_type=content_type)
        return Path(destination)

    monkeypatch.setattr(outbound_http, "download_public_file", download)
    return calls


def _facts(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT sha256,size_bytes FROM file_records").fetchone()


def _failure(db, destination, sources, artifact=None):
    with pytest.raises(media_restore.PrerequisiteUnavailable) as caught:
        media_restore.restore_artifact(destination, artifact if artifact is not None else {"sha256": SHA},
                                      sources, db_path=db, reserve_bytes=0, required_checksum=True)
    return caught.value


def test_quota_then_healthy_registered_alternative_and_restart_hold(registered, monkeypatch):
    db, destination = registered
    probes = []

    def quota(source, **kwargs):
        probes.append(source)
        raise subprocess.CalledProcessError(1, ["rclone"], stderr="403 quotaExceeded URL?secret=hidden")

    monkeypatch.setattr(media_restore, "probe_source", quota)
    downloads = _download(monkeypatch)
    result = media_restore.restore_artifact(destination, {"sha256": SHA}, [DRIVE, PUBLIC], db_path=db,
                                          reserve_bytes=0, required_checksum=True)
    assert result == {"path": destination, "sha256": SHA, "size_bytes": len(MEDIA), "source": PUBLIC}
    assert _facts(db) == (SHA, len(MEDIA))
    assert len(probes) == len(downloads) == 1
    importlib.reload(media_restore)
    monkeypatch.setattr(media_restore, "probe_source", quota)
    failure = _failure(db, destination, [DRIVE])
    assert failure.retryable and failure.details["dependencyCategory"] == "quota"
    assert failure.retry_after_seconds <= 1800
    assert len(probes) == 1
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT * FROM queue_evidence").fetchall() == [(57, "accepted:receipt:unchanged")]
        stored = repr(conn.execute("SELECT * FROM publish_dependency_states").fetchall())
    assert "synthetic-secret" not in stored and "hidden" not in stored and URL not in stored
    assert not list(destination.parent.glob(".*.restore"))


@pytest.mark.parametrize("field", ["sha256", "checksum", "metadata_sha256", "metadata_checksum"])
def test_artifact_hash_spellings_are_verified(registered, monkeypatch, field):
    db, destination = registered
    _download(monkeypatch)
    artifact = {field: SHA.upper()} if not field.startswith("metadata_") else {"metadata": {field[9:]: SHA.upper()}}
    result = media_restore.restore_artifact(destination, artifact, [PUBLIC], db_path=db, reserve_bytes=0)
    assert result["sha256"] == SHA


@pytest.mark.parametrize("payload", [MEDIA[:-1], MEDIA[:-1] + b"z", b"<html>not video</html>"])
def test_wrong_truncated_and_page_bytes_never_replace_existing(registered, monkeypatch, payload):
    db, destination = registered
    destination.write_bytes(b"existing")
    _download(monkeypatch, payload)
    failure = _failure(db, destination, [PUBLIC])
    assert not failure.retryable and failure.details["dependencyCategory"] == "integrity"
    assert destination.read_bytes() == b"existing"
    assert _facts(db) == (None, None)
    assert not list(destination.parent.glob(".*.restore"))


def test_wrong_content_type_rejected_even_with_matching_hash(registered, monkeypatch):
    db, destination = registered
    _download(monkeypatch, content_type="text/html")
    assert _failure(db, destination, [PUBLIC]).details["dependencyCategory"] == "integrity"
    assert not destination.exists()


@pytest.mark.parametrize("algorithm", ["SHA-256", "MD5", "SHA1"])
def test_explicit_whole_provider_checksum_proves_bytes(registered, monkeypatch, algorithm):
    db, destination = registered
    _download(monkeypatch)
    proof = {"algorithm": algorithm, "value": hashlib.new(algorithm.lower().replace("-", ""), MEDIA).hexdigest()}
    source = dict(PUBLIC, remote_checksum=proof)
    result = media_restore.restore_artifact(destination, {}, [source], db_path=db, reserve_bytes=0,
                                          required_checksum=True)
    assert result["sha256"] == SHA and _facts(db) == (SHA, len(MEDIA))


def test_known_s3_multipart_layout_verified_without_opaque_guessing(registered, monkeypatch, tmp_path):
    db, destination = registered
    payload = MEDIA + b"v" * (9 * 1024 * 1024)
    sample = tmp_path / "proof.bin"
    sample.write_bytes(payload)
    etag = media_integrity.multipart_etag_file(sample, 8388608)
    _download(monkeypatch, payload)
    source = {"public_url": URL, "expected_size_bytes": len(payload),
              "remote_checksum": {"algorithm": "s3-multipart-md5", "etag": etag, "part_size": 8388608}}
    result = media_restore.restore_artifact(destination, {}, [source], db_path=db, reserve_bytes=0,
                                          required_checksum=True)
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("proof", [None, {"etag": "opaque-2"}, {"algorithm": "md5", "etag": "f" * 32},
                                   {"algorithm": "s3-multipart-md5", "etag": "f" * 32 + "-2"}])
def test_size_or_opaque_etag_never_becomes_integrity(registered, monkeypatch, proof):
    db, destination = registered
    calls = _download(monkeypatch)
    failure = _failure(db, destination, [dict(PUBLIC, remote_checksum=proof)], artifact={})
    assert failure.details["dependencyCategory"] == "integrity"
    assert _facts(db) == (None, None) and not destination.exists()
    if proof is None:
        assert not calls


def test_fresh_exact_rclone_stat_hash_used_as_provider_proof(registered, monkeypatch):
    db, destination = registered
    stat_calls = []

    def runner(command, **kwargs):
        stat_calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(
            {"Size": len(MEDIA), "IsDir": False, "Hashes": {"MD5": hashlib.md5(MEDIA).hexdigest(),
                                                             "SHA-1": hashlib.sha1(MEDIA).hexdigest()}}))

    def stat(source, **kwargs):
        return rclone_storage.stat_artifact(source["storage_key"], remote_name=source["backend"]["bucket"],
                                           remote_root=source["backend"]["endpoint"], runner=runner, **kwargs)

    def download(backend, key, destination, **kwargs):
        assert key == DRIVE["storage_key"]
        Path(destination).write_bytes(MEDIA)
        return Path(destination)

    monkeypatch.setattr(media_restore, "probe_source", stat)
    monkeypatch.setattr(media_remote_storage, "download_from_backend", download)
    result = media_restore.restore_artifact(destination, {}, [DRIVE], db_path=db, reserve_bytes=0,
                                          required_checksum=True)
    assert result["sha256"] == SHA
    command, kwargs = stat_calls[0]
    assert command[:5] == ["rclone", "lsjson", "owned-drive:registered-root/registered/part-1.mp4", "--stat", "--hash"]
    assert kwargs["timeout"] <= 20 and command[command.index("--retries") + 1] == "1"
    assert "link" not in command and "copyto" not in command


def test_timeout_then_exact_alternative_no_blind_retry(registered, monkeypatch):
    db, destination = registered
    calls = []

    def timeout(source, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(["rclone"], kwargs["total_deadline"])

    monkeypatch.setattr(media_restore, "probe_source", timeout)
    _download(monkeypatch)
    media_restore.restore_artifact(destination, {"sha256": SHA}, [DRIVE, DRIVE, PUBLIC], db_path=db, reserve_bytes=0)
    assert len(calls) == 1 and calls[0]["total_deadline"] <= 20
    assert _failure(db, destination, [DRIVE]).details["dependencyCategory"] == "transport"
    assert len(calls) == 1


def test_hold_expires_into_single_readonly_probe_and_failed_probe_renews_hold(registered, monkeypatch):
    db, destination = registered
    scope, fingerprint = media_restore.dependency_identity(DRIVE)
    media_restore._admit(db, scope, fingerprint, "media_restore")
    media_restore._record_state(db, scope, fingerprint, "quota")
    future = media_restore._now() + timedelta(seconds=1801)
    monkeypatch.setattr(media_restore, "_now", lambda: future)
    probes = []

    def quota(source, **kwargs):
        probes.append(source)
        # Concurrent contender cannot send another probe.
        with pytest.raises(media_restore.PrerequisiteUnavailable):
            media_restore._admit(db, scope, fingerprint, "media_restore")
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(media_restore, "probe_source", quota)
    assert _failure(db, destination, [DRIVE]).details["dependencyCategory"] == "quota"
    assert _failure(db, destination, [DRIVE]).details["dependencyCategory"] == "quota"
    assert len(probes) == 1


def test_config_change_invalidates_hold_and_old_fingerprint_cannot_overwrite(registered, monkeypatch):
    db, destination = registered
    scope, old = media_restore.dependency_identity(DRIVE)
    media_restore._admit(db, scope, old, "media_restore")
    media_restore._record_state(db, scope, old, "auth")
    changed = dict(DRIVE, backend=dict(DRIVE["backend"], secret_key="rotated-synthetic"))
    new_scope, new = media_restore.dependency_identity(changed)
    assert new_scope == scope and new != old
    assert media_restore.dependency_status(changed, db_path=db)["status"] == "unknown"
    assert not media_restore._admit(db, scope, new, "media_restore")
    assert not media_restore._record_state(db, scope, old, "quota")
    assert media_restore._record_state(db, scope, new, "auth")
    assert not media_restore._record_state(db, scope, new, None)  # no stale success clears a newer hold


def test_scopes_isolate_remote_workspace_and_public_r2(registered):
    scope, _ = media_restore.dependency_identity(DRIVE)
    for changes in ({"bucket": "other"}, {"workspace_id": "two"}, {"id": 8}):
        assert media_restore.dependency_identity(dict(DRIVE, backend=dict(DRIVE["backend"], **changes)))[0] != scope
    assert media_restore.dependency_identity(PUBLIC)[0] != scope


def test_capacity_defers_before_provider_reads_or_downloads(registered, monkeypatch):
    db, destination = registered
    monkeypatch.setattr(media_restore.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024))
    monkeypatch.setattr(media_restore, "probe_source", lambda *a, **k: pytest.fail("capacity must precede provider read"))
    calls = _download(monkeypatch)
    failure = _failure(db, destination, [DRIVE, PUBLIC])
    assert failure.retryable and failure.details["stage"] == "waiting_capacity"
    assert not destination.exists() and not calls and _facts(db) == (None, None)


def test_actual_digest_is_durable_before_final_replace(registered, monkeypatch):
    db, destination = registered
    destination.write_bytes(b"old")
    _download(monkeypatch)
    replace = Path.replace
    exposed = []

    def check_replace(path, target):
        if Path(target) == destination:
            assert _facts(db) == (SHA, len(MEDIA))
            assert destination.read_bytes() == b"old"
            exposed.append(True)
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", check_replace)
    media_restore.restore_artifact(destination, {"sha256": SHA}, [PUBLIC], db_path=db, reserve_bytes=0)
    assert exposed == [True]


def test_cancellation_removes_only_owned_temporary(registered, monkeypatch):
    db, destination = registered
    unrelated = destination.parent / ".someone-else.restore"
    unrelated.write_bytes(b"keep")

    def cancel(url, temporary, **kwargs):
        Path(temporary).write_bytes(MEDIA[:20])
        raise KeyboardInterrupt()

    monkeypatch.setattr(outbound_http, "download_public_file", cancel)
    with pytest.raises(KeyboardInterrupt):
        media_restore.restore_artifact(destination, {"sha256": SHA}, [PUBLIC], db_path=db, reserve_bytes=0)
    assert list(destination.parent.glob(".*.restore")) == [unrelated]
    assert _facts(db) == (None, None) and not destination.exists()


def test_missing_database_and_readonly_health_never_create_schema(tmp_path):
    missing = tmp_path / "must-not-create.db"
    with pytest.raises(media_restore.PrerequisiteUnavailable):
        media_restore.dependency_status(DRIVE, db_path=missing)
    assert not missing.exists()
    existing = tmp_path / "empty.db"
    with closing(sqlite3.connect(existing)):
        pass
    with pytest.raises(media_restore.PrerequisiteUnavailable):
        media_restore.dependency_status(DRIVE, db_path=existing)
    with closing(sqlite3.connect(existing)) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master").fetchall()


def test_raw_source_id_is_never_used_to_record_derivative_facts(registered, monkeypatch):
    db, destination = registered
    _download(monkeypatch)
    other_destination = destination.with_name("unregistered-derivative.mp4")
    failure = _failure(db, other_destination, [PUBLIC], {"sha256": SHA, "source_file_record_id": 1})
    assert failure.details["dependencyCategory"] == "identity"
    assert _facts(db) == (None, None) and not other_destination.exists()


class Response:
    is_redirect = False
    is_permanent_redirect = False

    def __init__(self, chunks, *, declared=None, content_type="video/mp4"):
        self.chunks = chunks
        self.headers = {"Content-Type": content_type}
        if declared is not None:
            self.headers["Content-Length"] = str(declared)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield from self.chunks


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        return self.response


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(outbound_http.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])


def test_http_file_really_streams_and_hashes_to_exclusive_sibling(tmp_path, monkeypatch, public_dns):
    target = tmp_path / "stream.mp4"
    target.write_bytes(b"old")
    unrelated = tmp_path / ".stream.mp4.part"
    unrelated.write_bytes(b"keep")
    first = b"x" * (256 * 1024)

    def chunks():
        yield first
        owned = [path for path in tmp_path.glob(".*.part") if path != unrelated]
        assert len(owned) == 1 and owned[0].stat().st_size == len(first)
        assert target.read_bytes() == b"old"
        yield b"last"

    monkeypatch.setattr(outbound_http, "fetch_public_bytes", lambda *a, **k: pytest.fail("file API must not buffer"))
    facts = {}
    outbound_http.download_public_file(URL, target, session=Session(Response(chunks())), facts=facts)
    assert facts["sha256"] == hashlib.sha256(first + b"last").hexdigest()
    assert facts["size_bytes"] == len(first) + 4
    assert list(tmp_path.glob(".*.part")) == [unrelated]


@pytest.mark.parametrize("chunks,declared,max_bytes", [([b"short"], 10, 20), ([b"overflow"], None, 2), ([], 0, 20)])
def test_http_truncation_overflow_and_empty_cleanup(tmp_path, public_dns, chunks, declared, max_bytes):
    target = tmp_path / "protected.mp4"
    target.write_bytes(b"old")
    with pytest.raises(outbound_http.OutboundUrlError):
        outbound_http.download_public_file(URL, target, session=Session(Response(chunks, declared=declared)), max_bytes=max_bytes)
    assert target.read_bytes() == b"old" and not list(tmp_path.glob(".*.part"))


def test_http_verifier_and_total_deadline_prevent_exposure(tmp_path, monkeypatch, public_dns):
    target = tmp_path / "protected.mp4"
    with pytest.raises(ValueError):
        outbound_http.download_public_file(URL, target, session=Session(Response([MEDIA])),
                                          verifier=lambda *a: (_ for _ in ()).throw(ValueError("bad checksum")))
    assert not target.exists() and not list(tmp_path.glob(".*.part"))
    def dribbling_chunks():
        time.sleep(0.1)  # no complete chunk; read timeout alone is insufficient
        yield MEDIA

    session = Session(Response(dribbling_chunks()))
    started = time.monotonic()
    with pytest.raises(outbound_http.OutboundUrlError):
        outbound_http.download_public_file(URL, target, session=session, total_deadline=0.03)
    assert time.monotonic() - started < 0.08
    assert session.calls[0]["timeout"] <= 0.03
    time.sleep(0.1)  # abandoned read cannot write after owning file cleanup
    assert not target.exists() and not list(tmp_path.glob(".*.part"))


def test_rclone_timeout_preserves_final_and_cleans_sibling(tmp_path):
    target = tmp_path / "protected.mp4"
    target.write_bytes(b"old")

    def runner(command, **kwargs):
        Path(command[3]).write_bytes(b"partial")
        assert kwargs["timeout"] <= 0.2
        assert "--contimeout" in command and "--timeout" in command
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(subprocess.TimeoutExpired):
        rclone_storage.download_artifact("exact/key", target, remote_name="owned", remote_root="", runner=runner, total_deadline=0.2)
    assert target.read_bytes() == b"old" and not list(tmp_path.glob(".*.part"))


@pytest.mark.parametrize("hashes,algorithm", [({"SHA-256": SHA}, "sha256"), ({"SHA-1": "a" * 40}, "sha1"),
                                            ({"MD5": "b" * 32}, "md5"), ({"CRC32": "opaque"}, None)])
def test_rclone_hash_format_normalization(hashes, algorithm):
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"Size": 12, "IsDir": False, "Hashes": hashes}))
    result = rclone_storage.stat_artifact("exact", remote_name="owned", remote_root="", runner=runner)
    assert (result["remote_checksum"] or {}).get("algorithm") == algorithm


def test_s3_read_is_bounded_and_never_uses_etag_as_proof(tmp_path, monkeypatch):
    calls = []
    body = SimpleNamespace(iter_chunks=lambda **k: iter([MEDIA]), close=lambda: calls.append("body_close"))
    client = SimpleNamespace(get_object=lambda **k: {"Body": body, "ContentLength": len(MEDIA), "ContentType": "video/mp4"},
                             head_object=lambda **k: {"ContentLength": len(MEDIA), "ETag": "opaque-2"},
                             close=lambda: calls.append("client_close"))
    monkeypatch.setattr(media_remote_storage, "_bounded_s3_client", lambda *a, **k: client)
    backend = {"provider": "r2", "bucket": "registered"}
    target = tmp_path / "s3.mp4"
    media_remote_storage.download_from_backend(backend, "exact/key", target)
    assert target.read_bytes() == MEDIA
    assert media_remote_storage.stat_from_backend(backend, "exact/key")["remote_checksum"] is None
    client.head_object = lambda **k: {"ContentLength": len(MEDIA), "ChecksumType": "FULL_OBJECT",
                                    "ChecksumSHA256": base64.b64encode(bytes.fromhex(SHA)).decode()}
    assert media_remote_storage.stat_from_backend(backend, "exact/key")["remote_checksum"] == {"algorithm": "sha256", "value": SHA}
    assert calls == ["body_close", "client_close", "client_close", "client_close"]


def test_additive_migration_no_backfill_or_queue_receipt_rewrite(tmp_path):
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = importlib.import_module("migrations.versions.0027_publish_readiness_and_integrity")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE file_records(id INTEGER PRIMARY KEY, filename TEXT, filesize REAL)")
            conn.exec_driver_sql("INSERT INTO file_records VALUES(1,'legacy.mp4',1.25)")
            conn.exec_driver_sql("CREATE TABLE accounts(id INTEGER PRIMARY KEY)")
            conn.exec_driver_sql("CREATE TABLE publish_job_targets(id INTEGER PRIMARY KEY,checkpoint TEXT)")
            conn.exec_driver_sql("INSERT INTO publish_job_targets VALUES(57,'accepted:receipt:immutable')")
            before_columns = conn.exec_driver_sql("PRAGMA table_info(publish_job_targets)").fetchall()
            original = module.op
            module.op = Operations(MigrationContext.configure(conn))
            try:
                module.upgrade()
                module.upgrade()
                assert conn.exec_driver_sql("SELECT * FROM file_records").fetchall() == [(1, "legacy.mp4", 1.25, None, None)]
                assert [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(publish_dependency_states)")] == [
                    "scope_key", "config_fingerprint", "kind", "status", "reason", "blocked_until", "last_checked_at", "updated_at"]
                assert [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(publish_destination_readiness)")] == [
                    "account_id", "workspace_id", "provider", "config_fingerprint", "status", "reason", "evidence_json", "checked_at", "expires_at"]
                assert conn.exec_driver_sql("PRAGMA foreign_key_list(publish_destination_readiness)").fetchone()[2:5] == ("accounts", "account_id", "id")
                conn.exec_driver_sql("UPDATE file_records SET sha256=?,size_bytes=? WHERE id=1", (SHA, len(MEDIA)))
                module.downgrade()
                module.downgrade()
                assert conn.exec_driver_sql("SELECT sha256,size_bytes FROM file_records").fetchone() == (SHA, len(MEDIA))
                assert conn.exec_driver_sql("PRAGMA table_info(publish_job_targets)").fetchall() == before_columns
                assert conn.exec_driver_sql("SELECT * FROM publish_job_targets").fetchall() == [(57, "accepted:receipt:immutable")]
                module.upgrade()
            finally:
                module.op = original
    finally:
        engine.dispose()


@pytest.mark.parametrize("message,category,retryable", [
    ("quotaExceeded", "quota", True), ("accessDenied", "auth", True),
    ("object not found", "missing", False), ("connection reset", "transport", True),
])
def test_provider_failure_categories_are_safe_and_scoped(registered, monkeypatch, message, category, retryable):
    db, destination = registered
    monkeypatch.setattr(media_restore, "probe_source", lambda *a, **k: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, ["rclone"], stderr=message + " secret=do-not-persist")))
    failure = _failure(db, destination, [DRIVE])
    assert failure.retryable is retryable and failure.details["dependencyCategory"] == category
    assert "do-not-persist" not in str(failure) and "do-not-persist" not in repr(failure.details)
    with closing(sqlite3.connect(db)) as conn:
        state = conn.execute("SELECT scope_key FROM publish_dependency_states WHERE reason=?", (category,)).fetchone()
    assert (":object:" in state[0]) is (category == "missing")


def test_configuration_evidence_is_readonly_and_token_refresh_does_not_reset_hold(tmp_path, monkeypatch):
    config = tmp_path / "synthetic-rclone.conf"
    first = "[owned-drive]\ntype=drive\nclient_id=owned-client\nclient_secret=synthetic\nscope=drive\ntoken=old\n"
    config.write_text(first, encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(config))
    evidence = media_restore.rclone_configuration_evidence(DRIVE["backend"])
    assert evidence["explicit_oauth_client_configured"] is True
    config.write_text(first.replace("token=old", "token=new"), encoding="utf-8")
    assert media_restore.rclone_configuration_evidence(DRIVE["backend"]) == evidence
    config.write_text(first.replace("owned-client", "replacement-client"), encoding="utf-8")
    assert media_restore.rclone_configuration_evidence(DRIVE["backend"])["config_fingerprint"] != evidence["config_fingerprint"]
    assert "synthetic" not in repr(evidence) and "owned-client" not in repr(evidence)


def test_s3_dribbling_body_has_hard_deadline_and_no_late_file_writes(tmp_path, monkeypatch):
    def slow_chunks(**kwargs):
        time.sleep(0.1)
        yield MEDIA

    body = SimpleNamespace(iter_chunks=slow_chunks, close=lambda: None)
    client = SimpleNamespace(get_object=lambda **k: {"Body": body, "ContentLength": len(MEDIA)}, close=lambda: None)
    monkeypatch.setattr(media_remote_storage, "_bounded_s3_client", lambda *a, **k: client)
    target = tmp_path / "s3.mp4"
    target.write_bytes(b"old")
    with pytest.raises(outbound_http.OutboundUrlError):
        media_remote_storage.download_from_backend({"provider": "r2", "bucket": "registered"}, "exact", target, total_deadline=0.03)
    time.sleep(0.1)
    assert target.read_bytes() == b"old" and not list(tmp_path.glob(".*.part"))


def test_remote_only_registration_uses_exact_key_not_raw_source_id(registered, monkeypatch):
    db, old_destination = registered
    destination = old_destination.with_name("warming-destination.mp4")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("ALTER TABLE file_records ADD COLUMN storage_cdn_url TEXT")
        conn.execute("UPDATE file_records SET storage_cdn_url=? WHERE id=1", (URL,))
    _download(monkeypatch)
    media_restore.restore_artifact(destination, {"sha256": SHA}, [PUBLIC], db_path=db, reserve_bytes=0)
    assert _facts(db) == (SHA, len(MEDIA)) and destination.read_bytes() == MEDIA


def test_exact_alternative_after_integrity_failure_does_not_block_host(registered, monkeypatch):
    db, destination = registered
    bad = dict(PUBLIC, public_url="https://cdn.example/exact/bad-part.mp4")

    def download(url, temporary, **kwargs):
        Path(temporary).write_bytes(MEDIA if url == URL else MEDIA[:-1] + b"wrong")
        kwargs.get("facts", {}).update(content_type="video/mp4")

    monkeypatch.setattr(outbound_http, "download_public_file", download)
    result = media_restore.restore_artifact(destination, {"sha256": SHA}, [bad, PUBLIC], db_path=db, reserve_bytes=0)
    assert result["source"] == PUBLIC and result["sha256"] == SHA
    assert media_restore.dependency_status(PUBLIC, db_path=db)["status"] == "healthy"


def test_provider_and_artifact_facts_must_agree_before_exposure(registered, monkeypatch):
    db, destination = registered
    _download(monkeypatch)
    monkeypatch.setattr(media_integrity, "verify_provider_checksum", lambda *a, **k: {"sha256": "f" * 64, "size_bytes": len(MEDIA)})
    source = dict(PUBLIC, remote_checksum={"algorithm": "sha256", "value": SHA})
    failure = _failure(db, destination, [source])
    assert failure.details["dependencyCategory"] == "integrity"
    assert not destination.exists() and _facts(db) == (None, None)


def test_source_availability_suppresses_provider_during_durable_hold(registered, monkeypatch):
    db, destination = registered
    scope, fingerprint = media_restore.dependency_identity(DRIVE)
    media_restore._admit(db, scope, fingerprint, "media_restore")
    media_restore._record_state(db, scope, fingerprint, "quota")
    monkeypatch.setattr(media_restore, "probe_source", lambda *a, **k: pytest.fail("hold must not call provider"))
    result = media_restore.source_availability(DRIVE, db_path=db)
    assert not result["available"] and not result["integrity_verified"]
    assert result["dependencyCategory"] == "quota" and result["retryable"]


@pytest.mark.parametrize("stage", ["dns", "headers"])
def test_http_dns_and_header_stalls_obey_hard_deadline(tmp_path, monkeypatch, stage):
    def resolver(*args, **kwargs):
        if stage == "dns":
            time.sleep(0.1)
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    class SlowSession(Session):
        def get(self, url, **kwargs):
            if stage == "headers":
                time.sleep(0.1)
            return super().get(url, **kwargs)

    monkeypatch.setattr(outbound_http.socket, "getaddrinfo", resolver)
    target = tmp_path / "stall.mp4"
    with pytest.raises(outbound_http.OutboundUrlError):
        outbound_http.download_public_file(URL, target, session=SlowSession(Response([MEDIA])), total_deadline=0.03)
    time.sleep(0.1)
    assert not target.exists() and not list(tmp_path.glob(".*.part"))


def test_http_timeout_clamps_rounding_to_requested_total_budget(monkeypatch, public_dns):
    from types import SimpleNamespace

    budget = 0.03
    clock = 1_000_000.0
    assert (clock + budget) - clock > budget  # Floating-point subtraction rounds up.
    monkeypatch.setattr(outbound_http, "time", SimpleNamespace(monotonic=lambda: clock))
    session = Session(Response([MEDIA]))
    assert outbound_http.fetch_public_bytes(URL, session=session, total_deadline=budget) == MEDIA
    assert len(session.calls) == 1
    assert session.calls[0]["timeout"] <= budget
