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

import logging
import mimetypes
import os
from pathlib import Path, PurePosixPath

from myUtils import do_spaces, rclone_storage, share_storage
from myUtils.rclone_storage import RemoteArtifact

logger = logging.getLogger(__name__)

BACKENDS_ENV = "SAU_STORAGE_BACKENDS"
DEFAULT_ORDER = ("share", "do_spaces", "rclone")


class RemoteStorageError(RuntimeError):
    """Raised when no configured remote storage backend could store the file."""


def _backend_order() -> list[str]:
    raw = os.environ.get(BACKENDS_ENV, "")
    order = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return order or list(DEFAULT_ORDER)


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
