"""Canonical byte-integrity boundary (no provider, media probe or DB I/O).

``verify_artifact`` returns executed local ``sha256``/``size_bytes`` facts.
Null optional fields mean unknown; populated malformed/conflicting fields fail.
Source/origin/input hashes are provenance, NOT checksums of the final artifact.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

HASH_CHUNK_BYTES = 1024 * 1024


class MediaIntegrityError(ValueError):
    """Permanent integrity failure; retrying must not permit substituted bytes."""

    retryable = False


def _metadata(artifact: Mapping) -> Mapping:
    if not isinstance(artifact, Mapping):
        raise MediaIntegrityError("Artifact must be an object")
    metadata = artifact.get("metadata")
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise MediaIntegrityError("Artifact metadata must be an object")
    return metadata


def _hex(value, length: int, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value):
        raise MediaIntegrityError(f"Invalid {label} checksum")
    return value.lower()


def expected_sha256(artifact: Mapping, *, required: bool = False) -> str | None:
    """Read top-level and metadata checksum/sha256; never choose precedence."""
    metadata = _metadata(artifact)
    values = []
    for label, fields in (("artifact", artifact), ("metadata", metadata)):
        for key in ("checksum", "sha256"):
            if key in fields and fields[key] is not None:
                values.append(_hex(fields[key], 64, f"{label}.{key}"))
    if len(set(values)) > 1:
        raise MediaIntegrityError("Conflicting artifact SHA256 checksum facts")
    if not values and required:
        raise MediaIntegrityError("Artifact has no required SHA256 checksum")
    return values[0] if values else None


def _digest_file(path: str | Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: str | Path) -> str:
    """Incremental whole-file SHA256; memory is bounded to HASH_CHUNK_BYTES."""
    return _digest_file(path, "sha256")


def _stamp(stat) -> tuple:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def verify_artifact(path: str | Path, artifact: Mapping, *, required: bool = False) -> dict:
    """Check every known byte count and checksum against stable actual bytes.

    Supported byte counts: size_bytes, filesize, size, byte_count at the top
    level or in metadata. Only non-negative integer counts (or decimal integer
    strings used by legacy records) are accepted; MB/float guesses are not.
    """
    expected = expected_sha256(artifact, required=required)
    sizes = []
    for label, fields in (("artifact", artifact), ("metadata", _metadata(artifact))):
        for key in ("size_bytes", "filesize", "size", "byte_count"):
            value = fields.get(key)
            if value is None:
                continue
            if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
                value = int(value)
            if type(value) is not int or value < 0:
                raise MediaIntegrityError(f"Invalid {label}.{key} byte count")
            sizes.append(value)
    if len(set(sizes)) > 1:
        raise MediaIntegrityError("Conflicting artifact byte count facts")
    try:
        path = Path(path)
        before = path.stat()
        if not path.is_file():
            raise MediaIntegrityError("Artifact is not a regular file")
        if sizes and before.st_size != sizes[0]:
            raise MediaIntegrityError("Artifact byte count mismatch")
        actual = sha256_file(path)
        after = path.stat()
    except OSError as exc:
        raise MediaIntegrityError("Artifact is missing or unreadable") from exc
    if _stamp(before) != _stamp(after):
        raise MediaIntegrityError("Artifact changed during checksum verification")
    if expected is not None and actual != expected:
        raise MediaIntegrityError("Artifact checksum mismatch; refusing substituted media")
    return {"sha256": actual, "size_bytes": after.st_size}


def multipart_etag_file(path: str | Path, part_size: int) -> str:
    """S3 multipart-MD5 ETag for an EXPLICIT uniform part layout.

    Returns md5(concatenated binary per-part MD5s)-part_count, including '-1'
    for a one-part multipart upload. Not whole-file MD5. No layout inference;
    no support for variable part sizes, encrypted/opaque ETags or empty uploads.
    Reads even very large parts incrementally rather than buffering a part.
    """
    if type(part_size) is not int or part_size <= 0:
        raise MediaIntegrityError("Multipart checksum needs an explicit positive integer part_size")
    combined, count = hashlib.md5(), 0
    with Path(path).open("rb") as stream:
        while True:
            digest, remaining, read = hashlib.md5(), part_size, 0
            while remaining:
                chunk = stream.read(min(remaining, HASH_CHUNK_BYTES))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
                read += len(chunk)
            if not read:
                break
            combined.update(digest.digest())
            count += 1
    if not count:
        raise MediaIntegrityError("Empty file has no multipart upload checksum proof")
    return f"{combined.hexdigest()}-{count}"


def verify_provider_checksum(path: str | Path, proof: Mapping) -> dict:
    """Verify explicit provider proof, then return executed local SHA256 facts.

    Whole-file proof: {'algorithm': 'SHA256' | 'MD5' | 'SHA1', 'value': hex}.
    'checksum'/'digest' are supported value aliases and must agree if supplied.
    Multipart proof: {'algorithm': 's3-multipart-md5', 'etag': 'hex-N',
                      'part_size': integer}. Quotes around ETags are allowed.
    Algorithm aliases 'multipartMD5'/'s3-multipartMD5' are also accepted.
    Bare ETags, base64, unknown algorithms, missing/guessed part layouts fail.
    """
    if not isinstance(proof, Mapping) or not isinstance(proof.get("algorithm"), str):
        raise MediaIntegrityError("Provider checksum needs an explicit algorithm")
    algorithm = re.sub(r"[-_]", "", proof["algorithm"]).lower()
    multipart = algorithm in {"s3multipartmd5", "multipartmd5"}
    try:
        path = Path(path)
        before = path.stat()
        if multipart:
            etag = proof.get("etag")
            if not isinstance(etag, str):
                raise MediaIntegrityError("Multipart proof needs an explicit ETag")
            if len(etag) >= 2 and etag[0] == etag[-1] == '"':
                etag = etag[1:-1]
            if not re.fullmatch(r"[0-9a-fA-F]{32}-[1-9][0-9]*", etag):
                raise MediaIntegrityError("Invalid multipart MD5 ETag")
            actual = multipart_etag_file(path, proof.get("part_size"))
            expected = etag.lower()
            if any(proof.get(key) is not None for key in ("value", "checksum", "digest")):
                raise MediaIntegrityError("Multipart proof must use etag, not a whole-file digest")
        else:
            lengths = {"sha256": 64, "md5": 32, "sha1": 40}
            if algorithm not in lengths or proof.get("etag") is not None or proof.get("part_size") is not None:
                raise MediaIntegrityError("Unsupported or ambiguous provider checksum proof")
            values = [_hex(proof[key], lengths[algorithm], f"provider {algorithm}")
                      for key in ("value", "checksum", "digest") if proof.get(key) is not None]
            if not values or len(set(values)) != 1:
                raise MediaIntegrityError("Missing or conflicting provider checksum digest")
            expected = values[0]
            actual = _digest_file(path, algorithm)
        if actual != expected:
            raise MediaIntegrityError("Provider checksum mismatch")
        facts = verify_artifact(path, {})
        if _stamp(before) != _stamp(path.stat()):
            raise MediaIntegrityError("Artifact changed during provider checksum verification")
        return facts
    except OSError as exc:
        raise MediaIntegrityError("Provider checksum file is missing or unreadable") from exc


def artifact_metadata(local_path: str | None, metadata: dict | None) -> dict:
    """Registration facade: retain remote-only legacy facts; verify local facts.

    Existing populated checksums/counts must agree before filling unknown facts.
    Never cache a digest as evidence for bytes which have not been read now.
    """
    result = dict(metadata or {})
    if not local_path or not Path(local_path).is_file():
        expected_sha256({"metadata": result})
        return result
    facts = verify_artifact(local_path, {"metadata": result})
    result.update(facts)
    return result
