"""Remote media storage dispatcher.

A single entry point used by the publish pipeline to upload a media file to
a *publicly fetchable* HTTPS location and get back a
:class:`rclone_storage.RemoteArtifact`.

It tries a configurable, ordered list of backends and returns the first
success:

1. ``share``     — self-hosted tgstate host (:mod:`myUtils.share_storage`).
2. ``do_spaces`` — S3-compatible object storage / Cloudflare R2
   (:mod:`myUtils.do_spaces`), reading credentials from the environment.
3. ``rclone``    — legacy rclone remote (:mod:`myUtils.rclone_storage`).

Order is controlled by ``SAU_STORAGE_BACKENDS`` (comma-separated; default
``share,do_spaces,rclone``).  If every configured backend fails, a
:class:`RemoteStorageError` is raised so the caller fails loudly with an
actionable message instead of silently emitting an unreachable URL.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import tempfile
from pathlib import Path, PurePosixPath

from myUtils import do_spaces, rclone_storage, share_storage
from myUtils.rclone_storage import RemoteArtifact

logger = logging.getLogger(__name__)

BACKENDS_ENV = "SAU_STORAGE_BACKENDS"
DEFAULT_ORDER = ("share", "do_spaces", "rclone")

# When enabled, an artifact whose public URL serves an HTML page (or an error)
# instead of the raw file is rejected so the next backend is tried.  The Graph
# API's "Unable to fetch video file from URL" is exactly this failure, and a
# page-serving link is never a usable media URL for it.  Off by default so the
# behaviour is unchanged until an operator asks for the stricter check.
VERIFY_MEDIA_URL_ENV = "SAU_VERIFY_MEDIA_URL"
VERIFY_MEDIA_URL_TIMEOUT = 10.0

# Hosts that serve a viewer page rather than the raw bytes.  ``usercontent``
# (the actual Drive download host) and object-storage/CDN hosts are not here.
_PAGE_HOSTS = (
    "drive.google.com",
    "docs.google.com",
    "photos.google.com",
    "www.google.com",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
)
_MEDIA_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp4", ".mov", ".webm", ".m4v",
)


class RemoteStorageError(RuntimeError):
    """Raised when no configured remote storage backend could store the file."""


def _backend_order() -> list[str]:
    raw = os.environ.get(BACKENDS_ENV, "")
    order = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return order or list(DEFAULT_ORDER)


def _verify_media_url_enabled() -> bool:
    return str(os.environ.get(VERIFY_MEDIA_URL_ENV, "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def is_direct_file_url(url: str | None) -> bool:
    """Best-effort check that ``url`` is a raw https file URL, not a viewer.

    This is pure string logic (no network), so it is safe to call from any
    publish path.  It rejects the known page-serving hosts and the Google Drive
    ``/file/d/`` viewer shape; a direct download host such as
    ``drive.usercontent.google.com`` and object-storage/CDN URLs pass.
    """
    from urllib.parse import urlparse

    raw = str(url or "").strip()
    if not raw.lower().startswith("https://"):
        return False
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not host or host in _PAGE_HOSTS:
        return False
    if "/file/d/" in parsed.path:
        return False
    return True


def verify_media_url(url: str, *, session=None, timeout: float = VERIFY_MEDIA_URL_TIMEOUT) -> bool:
    """HEAD ``url`` and reject only an explicit HTML/error response.

    A network error or a server that refuses HEAD (405/501) fails open: it is
    not proof the URL is bad, and blocking a good upload is worse than letting
    the platform retry.  An HTML content type or a 4xx/5xx status fails closed.
    """
    if session is None:
        try:
            import requests

            session = requests.Session()
        except Exception:  # noqa: BLE001 - requests is a runtime dep; fail open
            return True
    try:
        response = session.head(url, timeout=timeout, allow_redirects=True)
    except Exception:  # noqa: BLE001
        logger.warning("could not verify media url %s with HEAD", url)
        return True
    status = int(getattr(response, "status_code", 0) or 0)
    if status in {405, 501}:
        return True
    if status >= 400 or status == 0:
        return False
    content_type = str(
        (getattr(response, "headers", {}) or {}).get("Content-Type") or ""
    ).lower()
    if content_type.startswith(("image/", "video/", "application/octet-stream")):
        return True
    return not content_type.startswith("text/html")


def _do_spaces_configured() -> bool:
    return bool(
        os.environ.get("DO_SPACES_BUCKET", "").strip()
        and os.environ.get("DO_SPACES_KEY", "").strip()
        and os.environ.get("DO_SPACES_SECRET", "").strip()
    )


def _rclone_configured() -> bool:
    return bool(os.environ.get(rclone_storage.DEFAULT_REMOTE_ENV, "").strip())


def is_backend_configured(name: str) -> bool:
    name = name.lower()
    if name == "share":
        return share_storage.is_configured()
    if name == "do_spaces":
        return _do_spaces_configured()
    if name == "rclone":
        return _rclone_configured()
    return False


def _bounded_s3_client(backend: dict, *, timeout: float):
    """An isolated read client; never ensure/create a bucket or alter defaults."""
    from botocore.config import Config

    configured = do_spaces.client_from_row(backend)
    return do_spaces.boto3.client(
        "s3", endpoint_url=configured.endpoint, aws_access_key_id=configured.access_key,
        aws_secret_access_key=configured.secret_key, region_name=configured.region,
        config=Config(connect_timeout=min(10.0, timeout), read_timeout=min(30.0, timeout),
                      retries={"total_max_attempts": 1}, s3={"addressing_style": "virtual"}),
    )


def stat_from_backend(backend: dict, storage_key: str, *, total_deadline: float = 20.0) -> dict:
    """Read-only probe of this exact registered key, with fresh provider evidence."""
    provider = str(backend.get("provider") or "").lower()
    if total_deadline <= 0:
        raise TimeoutError("remote stat budget exhausted")
    if provider == "rclone":
        return rclone_storage.stat_artifact(
            storage_key, remote_name=str(backend.get("bucket") or "").strip() or None,
            remote_root=str(backend.get("endpoint") or "").strip(),
            total_deadline=total_deadline,
        )
    if provider == "share":
        from myUtils.outbound_http import probe_public_url
        return probe_public_url(_share_restore_url(backend, storage_key), total_deadline=total_deadline)
    from myUtils.outbound_http import bounded_receive
    client = _bounded_s3_client(backend, timeout=total_deadline / 2)

    def read_stat(sink, stopped, register_response):
        try:
            result = client.head_object(Bucket=backend["bucket"], Key=storage_key, ChecksumMode="ENABLED")
            # ETags are opaque without explicit algorithm/part-layout proof.
            proof = None
            if result.get("ChecksumType") == "FULL_OBJECT" and result.get("ChecksumSHA256"):
                value = base64.b64decode(result["ChecksumSHA256"], validate=True)
                if len(value) != 32:
                    raise ValueError("invalid full-object SHA256 proof")
                proof = {"algorithm": "sha256", "value": value.hex()}
            return {"size_bytes": result.get("ContentLength"), "remote_checksum": proof}
        finally:
            client.close()

    return bounded_receive(read_stat, None, total_deadline=total_deadline)


def _share_restore_url(backend: dict, storage_key: str) -> str:
    if storage_key.lower().startswith("https://"):
        return storage_key
    url = str(backend.get("cdn_url") or backend.get("endpoint") or "").strip()
    if not url:
        raise RemoteStorageError("share backend download needs an exact registered URL")
    return url


def download_from_backend(backend: dict, storage_key: str, local_path: str | Path, *,
                          total_deadline: float = 120.0, max_bytes: int = 512 * 1024 * 1024,
                          facts: dict | None = None) -> Path:
    """Bounded, single-attempt exact registered read; no provider writes."""
    provider = str(backend.get("provider") or "").strip().lower()
    destination = Path(local_path)
    if total_deadline <= 0 or max_bytes <= 0:
        raise TimeoutError("remote download budget exhausted")
    if provider == "rclone":
        return rclone_storage.download_artifact(
            storage_key, destination,
            remote_name=str(backend.get("bucket") or "").strip() or None,
            remote_root=str(backend.get("endpoint") or "").strip(),
            total_deadline=total_deadline, max_bytes=max_bytes,
        )
    if provider == "share":
        return download_public_url(_share_restore_url(backend, storage_key), destination,
                                   total_deadline=total_deadline, max_bytes=max_bytes, facts=facts)
    from myUtils.outbound_http import bounded_receive
    client = _bounded_s3_client(backend, timeout=total_deadline / 2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part", dir=destination.parent)
    temporary = Path(name)

    def read_object(sink, stopped, register_response):
        body = None
        try:
            result = client.get_object(Bucket=backend["bucket"], Key=storage_key)
            body = result["Body"]
            register_response(getattr(body, "_raw_stream", body))
            declared = result.get("ContentLength")
            if declared is not None and declared > max_bytes:
                raise RemoteStorageError("remote object exceeds byte limit")
            received = 0
            digest = hashlib.sha256()
            for chunk in body.iter_chunks(chunk_size=256 * 1024):
                received += len(chunk)
                if received > max_bytes:
                    raise RemoteStorageError("remote object exceeds byte limit")
                sink.write(chunk)
                digest.update(chunk)
            if not received or (declared is not None and received != declared):
                raise RemoteStorageError("remote object was empty or incomplete")
            return {"size_bytes": received, "sha256": digest.hexdigest(),
                    "content_type": result.get("ContentType", "")}
        finally:
            if body is not None:
                body.close()
            client.close()

    try:
        with os.fdopen(fd, "wb") as sink:
            received = bounded_receive(read_object, sink, total_deadline=total_deadline)
            sink.flush()
            os.fsync(sink.fileno())
        if facts is not None:
            facts.update(received)
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def download_public_url(url: str, destination: Path, **kwargs) -> Path:
    """Fetch a stored object from its public HTTPS URL onto local disk.

    Used for backends that register a URL rather than an S3/R2 key (the share
    backend), where there is no key for an object client to fetch.

    Delegates to :mod:`myUtils.outbound_http` so every caller shares ONE
    policy: HTTPS only, every resolved address must be globally routable,
    every redirect hop is re-validated, and the transfer is bounded by size
    and total time. This previously used a bare ``urllib`` request, which
    would happily fetch a loopback or private host named by stored data.
    """
    from myUtils import outbound_http

    try:
        return outbound_http.download_public_file(url, Path(destination), **kwargs)
    except outbound_http.OutboundUrlError as exc:
        raise RemoteStorageError("public URL restore refused") from exc


def is_any_backend_configured() -> bool:
    """True when at least one remote storage backend is usable."""
    return any(is_backend_configured(name) for name in _backend_order())


def configured_backends() -> list[str]:
    return [name for name in _backend_order() if is_backend_configured(name)]


def _spaces_key(local_path: Path, *, campaign_id: int, artifact_subdir: str | None) -> str:
    parts = ["campaigns", str(campaign_id)]
    if artifact_subdir:
        parts.append(artifact_subdir)
    parts.append(local_path.name)
    return str(PurePosixPath(*parts))


def _upload_do_spaces(
    local_path: Path, *, campaign_id: int, artifact_subdir: str | None
) -> RemoteArtifact:
    client = do_spaces.client_from_env()
    key = _spaces_key(local_path, campaign_id=campaign_id, artifact_subdir=artifact_subdir)
    content_type = mimetypes.guess_type(local_path.name)[0] or ""
    public_url = client.upload_file(local_path, key, content_type)
    return RemoteArtifact(
        local_path=str(local_path),
        remote_name="do_spaces",
        remote_path=key,
        public_url=public_url,
    )


def upload_artifact(
    local_path: str | Path,
    *,
    campaign_id: int,
    artifact_subdir: str | None = None,
    db_path=None,  # accepted for API parity; env-based clients ignore it
) -> RemoteArtifact:
    """Upload ``local_path`` via the first working backend.

    Raises :class:`RemoteStorageError` if every configured backend fails.
    """
    source = Path(local_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    errors: list[str] = []
    attempted = False
    for name in _backend_order():
        if not is_backend_configured(name):
            continue
        attempted = True
        try:
            if name == "share":
                artifact = share_storage.upload_artifact(
                    source, campaign_id=campaign_id, artifact_subdir=artifact_subdir
                )
            elif name == "do_spaces":
                artifact = _upload_do_spaces(
                    source, campaign_id=campaign_id, artifact_subdir=artifact_subdir
                )
            elif name == "rclone":
                artifact = rclone_storage.upload_artifact(
                    source, campaign_id=campaign_id, artifact_subdir=artifact_subdir
                )
            else:  # pragma: no cover - guarded by is_backend_configured
                continue
            if not (artifact.public_url or "").lower().startswith("https://"):
                raise RemoteStorageError(
                    f"backend {name} returned a non-HTTPS url: {artifact.public_url!r}"
                )
            if _verify_media_url_enabled() and (
                not is_direct_file_url(artifact.public_url)
                or not verify_media_url(artifact.public_url)
            ):
                raise RemoteStorageError(
                    f"backend {name} returned a non-direct media url: {artifact.public_url!r}"
                )
            if name != _backend_order()[0]:
                logger.info("media upload used fallback backend %s", name)
            return artifact
        except Exception as exc:  # noqa: BLE001 - aggregate and try next backend
            errors.append(f"{name}: {exc}")
            logger.warning("remote media upload via %s failed: %s", name, exc)

    if not attempted:
        raise RemoteStorageError(
            "No remote storage backend is configured. Set SAU_SHARE_* or "
            "DO_SPACES_* (or SAU_DEFAULT_RCLONE_REMOTE) so media can be served "
            "from a public HTTPS URL."
        )
    raise RemoteStorageError("all remote storage backends failed: " + "; ".join(errors))
