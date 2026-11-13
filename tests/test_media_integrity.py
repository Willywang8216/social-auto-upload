"""Executed checksum facts; no network/provider or non-fixture DB writes."""
from __future__ import annotations

import hashlib
import io
import sqlite3
import sys
from pathlib import Path

import pytest

from myUtils import media_integrity as integrity


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "bytes.bin"
    path.write_bytes(b"approved media bytes" * 100)
    return path


@pytest.mark.parametrize("scope", ["top", "metadata"])
@pytest.mark.parametrize("key", ["checksum", "sha256"])
def test_every_sha_field_verifies_actual_bytes(media, scope, key):
    sha = integrity.sha256_file(media)
    artifact = {key: sha.upper()}
    if scope == "metadata":
        artifact = {"metadata": artifact}
    assert integrity.expected_sha256(artifact, required=True) == sha
    assert integrity.verify_artifact(media, artifact) == {"sha256": sha, "size_bytes": media.stat().st_size}
    media.write_bytes(b"wrong")
    with pytest.raises(integrity.MediaIntegrityError, match="checksum mismatch") as error:
        integrity.verify_artifact(media, artifact)
    assert error.value.retryable is False


@pytest.mark.parametrize("value", ["", "a" * 63, "a" * 65, "z" * 64, " " + "a" * 64, 42, False, [], {}])
@pytest.mark.parametrize("scope", ["top", "metadata"])
@pytest.mark.parametrize("key", ["checksum", "sha256"])
def test_malformed_fields_never_hidden_by_valid_field(media, value, scope, key):
    sha = integrity.sha256_file(media)
    artifact = {"sha256": sha, "metadata": {"sha256": sha}}
    fields = artifact if scope == "top" else artifact["metadata"]
    fields[key] = value
    with pytest.raises(integrity.MediaIntegrityError, match="Invalid"):
        integrity.verify_artifact(media, artifact)


def test_all_fields_agree_or_conflict(media):
    sha = integrity.sha256_file(media)
    artifact = {"checksum": sha, "sha256": sha.upper(), "metadata": {"checksum": sha, "sha256": sha}}
    assert integrity.expected_sha256(artifact) == sha
    for scope in (artifact, artifact["metadata"]):
        for key in ("sha256", "checksum"):
            previous = scope[key]
            scope[key] = "0" * 64
            with pytest.raises(integrity.MediaIntegrityError, match="Conflicting"):
                integrity.expected_sha256(artifact)
            scope[key] = previous
    assert integrity.expected_sha256({"sha256": None, "metadata": {"checksum": None}}) is None
    with pytest.raises(integrity.MediaIntegrityError, match="required"):
        integrity.verify_artifact(media, {}, required=True)
    # Input/source hashes do not describe final bytes.
    assert integrity.expected_sha256({"metadata": {"source_sha256": "0" * 64, "input_sha256": "1" * 64}}) is None


@pytest.mark.parametrize("scope", ["top", "metadata"])
@pytest.mark.parametrize("key", ["size_bytes", "filesize", "size", "byte_count"])
def test_all_size_facts_checked(media, scope, key):
    size = media.stat().st_size
    artifact = {key: str(size)}
    if scope == "metadata":
        artifact = {"metadata": artifact}
    integrity.verify_artifact(media, artifact)
    media.write_bytes(b"short")
    with pytest.raises(integrity.MediaIntegrityError, match="byte count mismatch"):
        integrity.verify_artifact(media, artifact)


@pytest.mark.parametrize("value", [True, -1, 2.5, "2.5", "", "nan"])
def test_bad_or_conflicting_sizes(media, value):
    with pytest.raises(integrity.MediaIntegrityError, match="Invalid"):
        integrity.verify_artifact(media, {"size_bytes": value})
    with pytest.raises(integrity.MediaIntegrityError, match="Conflicting"):
        integrity.verify_artifact(media, {"size_bytes": media.stat().st_size, "metadata": {"filesize": 4}})


@pytest.mark.parametrize("algorithm", ["SHA256", "MD5", "SHA1"])
def test_provider_whole_file_evidence(media, algorithm):
    digest = hashlib.new(algorithm.lower(), media.read_bytes()).hexdigest()
    proof = {"algorithm": algorithm, "value": digest.upper()}
    assert integrity.verify_provider_checksum(media, proof)["sha256"] == integrity.sha256_file(media)
    media.write_bytes(b"substitute")
    with pytest.raises(integrity.MediaIntegrityError, match="mismatch"):
        integrity.verify_provider_checksum(media, proof)


def test_multipart_layout_is_explicit_not_whole_md5(media):
    data, part_size = media.read_bytes(), 117
    pieces = [hashlib.md5(data[start:start + part_size]).digest() for start in range(0, len(data), part_size)]
    expected = hashlib.md5(b"".join(pieces)).hexdigest() + f"-{len(pieces)}"
    assert integrity.multipart_etag_file(media, part_size) == expected
    proof = {"algorithm": "s3-multipart-md5", "etag": f'"{expected}"', "part_size": part_size}
    assert integrity.verify_provider_checksum(media, proof)["size_bytes"] == len(data)
    for invalid in ({"etag": expected}, {"algorithm": "MD5", "value": expected},
                    {"algorithm": "s3-multipart-md5", "etag": expected},
                    {**proof, "part_size": 118}, {**proof, "part_size": True},
                    {**proof, "etag": expected[:-1] + "1"}, {**proof, "value": "0" * 32}):
        with pytest.raises(integrity.MediaIntegrityError):
            integrity.verify_provider_checksum(media, invalid)
    one = integrity.multipart_etag_file(media, 100000)
    assert one.endswith("-1") and one != hashlib.md5(data).hexdigest()


@pytest.mark.parametrize("proof", [{"algorithm": "crc32", "value": "1234"},
    {"algorithm": "sha256", "value": "bad"}, {"algorithm": "sha1"},
    {"algorithm": "md5", "value": "a" * 32, "checksum": "b" * 32},
    {"algorithm": "sha256", "value": "a" * 64, "part_size": 42}])
def test_unsupported_provider_evidence_refused(media, proof):
    with pytest.raises(integrity.MediaIntegrityError):
        integrity.verify_provider_checksum(media, proof)


def test_hashes_are_incremental_even_for_large_multipart_parts(monkeypatch):
    data = b"x" * (integrity.HASH_CHUNK_BYTES * 3 + 123)
    reads = []
    class Bounded(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= integrity.HASH_CHUNK_BYTES
            reads.append(size)
            return super().read(size)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: Bounded(data))
    assert integrity.sha256_file("virtual") == hashlib.sha256(data).hexdigest()
    assert len(reads) >= 4
    reads.clear()
    expected = hashlib.md5(hashlib.md5(data).digest()).hexdigest() + "-1"
    assert integrity.multipart_etag_file("virtual", 100 * len(data)) == expected
    assert len(reads) >= 4


def test_verification_rejects_source_changes_and_missing_files(media, monkeypatch):
    original = integrity.sha256_file
    def changed(path):
        sha = original(path)
        path.write_bytes(b"changed")
        return sha
    monkeypatch.setattr(integrity, "sha256_file", changed)
    with pytest.raises(integrity.MediaIntegrityError, match="changed"):
        integrity.verify_artifact(media, {})
    with pytest.raises(integrity.MediaIntegrityError, match="unreadable"):
        integrity.verify_artifact(media.parent / "missing", {})


def test_registration_facade_never_overwrites_wrong_existing_facts(media):
    assert integrity.artifact_metadata(str(media), {})["sha256"] == integrity.sha256_file(media)
    with pytest.raises(integrity.MediaIntegrityError):
        integrity.artifact_metadata(str(media), {"sha256": "0" * 64})


@pytest.mark.parametrize("columns", [True, False])
@pytest.mark.parametrize("wrong_sha", [False, True])
def test_offload_registration_heredoc_is_atomic_and_executed(tmp_path, monkeypatch, columns, wrong_sha):
    """Run only the registration Python body, never the actual offload shell."""
    media = tmp_path / "generated" / "nested" / "clip.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"verified remote bytes")
    db = tmp_path / "isolated.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE storage_backends (id INTEGER,provider TEXT,bucket TEXT,endpoint TEXT,enabled INTEGER)")
        conn.execute("INSERT INTO storage_backends VALUES (7,'rclone','drive','sau/generated',1)")
        extra = ",sha256 TEXT,size_bytes INTEGER" if columns else ""
        conn.execute("CREATE TABLE file_records (id INTEGER,file_path TEXT,filesize INTEGER,storage_key TEXT,storage_backend_id INTEGER" + extra + ")")
        conn.execute("INSERT INTO file_records (id,file_path,filesize) VALUES (1,'generated/nested/clip.mp4',0)")
    shell = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
    body = shell.split("register_verified_source() {", 1)[1].split("<<'PY'", 1)[1].split("\nPY\n", 1)[0]
    sha = "0" * 64 if wrong_sha else integrity.sha256_file(media)
    monkeypatch.setattr(sys, "argv", ["offload-registration", str(db), "drive:sau", str(tmp_path / "generated"),
        "nested/clip.mp4", str(media.stat().st_size), "sau/generated", sha, str(media)])
    if not columns or wrong_sha:
        with pytest.raises((sqlite3.OperationalError, SystemExit)):
            exec(compile(body, "offload-registration-heredoc", "exec"), {})
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT filesize,storage_key,storage_backend_id FROM file_records").fetchone() == (0, None, None)
    else:
        exec(compile(body, "offload-registration-heredoc", "exec"), {})
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT filesize,storage_key,storage_backend_id,sha256,size_bytes FROM file_records").fetchone() == (
                media.stat().st_size, "nested/clip.mp4", 7, sha, media.stat().st_size)
    assert media.read_bytes() == b"verified remote bytes"
