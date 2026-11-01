"""Byte identity for newly prepared media, before it enters the durable queue."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=256)
def _sha256(path: str, size: int, mtime_ns: int, ctime_ns: int) -> str:
    candidate = Path(path)
    digest = hashlib.sha256()
    with candidate.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = candidate.stat()
    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (size, mtime_ns, ctime_ns):
        raise ValueError("Prepared media changed while recording its integrity")
    return digest.hexdigest()


def artifact_metadata(local_path: str | None, metadata: dict | None) -> dict:
    result = dict(metadata or {})
    if not local_path:
        return result
    path = Path(local_path)
    if not path.is_file():
        return result  # legacy/offloaded rows may have only a remote identity
    before = path.stat()
    digest = _sha256(str(path.resolve()), before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    result.setdefault("size_bytes", before.st_size)
    result.setdefault("sha256", digest)
    return result
