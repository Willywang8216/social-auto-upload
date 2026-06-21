"""Self-hosted "share" / TG-State media host uploader.

Uploads a local file to the self-hosted tgstate instance (Telegram-channel
backed storage) and returns a :class:`rclone_storage.RemoteArtifact` whose
``public_url`` points at the publicly-fetchable ``/d/<id>`` endpoint
(served by the download-proxy with mp4 moov-fix, ``video/mp4`` content-type
and HTTP Range support — exactly what TikTok/Meta need to pull media).

Auth: tgstate protects ``/api/upload`` behind a session cookie named
``tgstate_session``.  We send the stored server session token
(``SAU_SHARE_SESSION_TOKEN``) as that cookie rather than performing a
password login, so no plaintext password is needed.

Configuration (all optional; ``is_configured()`` is False unless the
session token is present):

* ``SAU_SHARE_ENABLED``        — ``1``/``true`` to enable (default off).
* ``SAU_SHARE_SESSION_TOKEN``  — value of tgstate's ``app_settings.session_token``.
* ``SAU_SHARE_UPLOAD_BASE``    — base URL used to upload, reachable from the
  backend/worker container (default ``http://share-web`` on 1panel-network).
* ``SAU_SHARE_PUBLIC_BASE``    — public base used to build the returned URL
  (default ``https://share.iamwillywang.com``).
* ``SAU_SHARE_TIMEOUT``        — per-request timeout seconds (default 30).
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

from myUtils.rclone_storage import RemoteArtifact

ENABLED_ENV = "SAU_SHARE_ENABLED"
SESSION_TOKEN_ENV = "SAU_SHARE_SESSION_TOKEN"
UPLOAD_BASE_ENV = "SAU_SHARE_UPLOAD_BASE"
PUBLIC_BASE_ENV = "SAU_SHARE_PUBLIC_BASE"
TIMEOUT_ENV = "SAU_SHARE_TIMEOUT"

DEFAULT_UPLOAD_BASE = "http://share-web"
DEFAULT_PUBLIC_BASE = "https://share.iamwillywang.com"
SESSION_COOKIE = "tgstate_session"


class ShareUploadError(RuntimeError):
    """Raised when the share/tgstate upload cannot complete."""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_configured() -> bool:
    """True when the share backend is enabled and has a session token."""
    return _truthy(os.environ.get(ENABLED_ENV)) and bool(
        os.environ.get(SESSION_TOKEN_ENV, "").strip()
    )


def _upload_base() -> str:
    return (os.environ.get(UPLOAD_BASE_ENV) or DEFAULT_UPLOAD_BASE).rstrip("/")


def _public_base() -> str:
    return (os.environ.get(PUBLIC_BASE_ENV) or DEFAULT_PUBLIC_BASE).rstrip("/")


def _timeout() -> float:
    try:
        return float(os.environ.get(TIMEOUT_ENV) or 30)
    except (TypeError, ValueError):
        return 30.0


def _extract_id(payload: dict) -> str | None:
    """Pull the public id from a tgstate upload response.

    Mirrors the frontend's ``fUrl()``: prefer ``short_id`` then ``file_id``,
    searching nested dicts because tgstate wraps results under ``data``.
    """

    def find(node, keys):
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if value and str(value).lower() != "none":
                    return value
            for value in node.values():
                found = find(value, keys)
                if found:
                    return found
        return None

    short = find(payload, ["short_id"])
    if short:
        return str(short)
    file_id = find(payload, ["file_id"])
    return str(file_id) if file_id else None


def upload_artifact(
    local_path: str | Path,
    *,
    campaign_id: int,
    artifact_subdir: str | None = None,  # accepted for API parity
    session=None,
) -> RemoteArtifact:
    """Upload ``local_path`` to the share host and return a RemoteArtifact."""
    if not is_configured():
        raise ShareUploadError(
            "share backend not configured (set SAU_SHARE_ENABLED=1 and "
            "SAU_SHARE_SESSION_TOKEN)"
        )

    source = Path(local_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    http = session or requests.Session()
    cookies = {SESSION_COOKIE: os.environ.get(SESSION_TOKEN_ENV, "").strip()}
    url = f"{_upload_base()}/api/upload"
    try:
        with source.open("rb") as handle:
            response = http.post(
                url,
                files={"file": (source.name, handle)},
                cookies=cookies,
                timeout=_timeout(),
            )
    except requests.RequestException as exc:
        raise ShareUploadError(f"share upload request failed: {exc}") from exc

    if response.status_code >= 400:
        raise ShareUploadError(
            f"share upload HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ShareUploadError(
            f"share upload returned non-JSON: {response.text[:200]}"
        ) from exc

    if isinstance(payload, dict) and payload.get("status") == "error":
        detail = payload.get("detail") or payload
        message = detail.get("message") if isinstance(detail, dict) else detail
        raise ShareUploadError(f"share upload failed: {message}")

    file_id = _extract_id(payload)
    if not file_id:
        raise ShareUploadError(
            f"share upload response missing file id: {str(payload)[:200]}"
        )

    public_url = f"{_public_base()}/d/{file_id}"
    return RemoteArtifact(
        local_path=str(source),
        remote_name="share",
        remote_path=str(file_id),
        public_url=public_url,
    )
