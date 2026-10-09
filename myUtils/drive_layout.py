"""Google Drive media layout: inbox vs published vs archive.

The offload pipeline copies local media onto a Google Drive remote. Before
this module existed everything landed directly under ``sau/<root>`` where
``<root>`` is one of the local media roots (``videoFile``, ``uploads``,
``generated``): newly-uploaded material awaiting its first publish sat in the
same tree as assets that had already been published, so the operator could not
tell "current material" from "already-published history".

This module is the single source of truth for the resulting layout::

    sau/inbox/{videoFile,uploads,generated}/<relative key>     NEW material
    sau/published/{videoFile,uploads,generated}/<relative key> ALREADY published
    sau/archive/<campaign>/...                                 superseded originals
    sau/assets/...                                             brand assets

Why a *tier* is a path prefix above the existing root, not a change to the
relative key
---------------------------------------------------------------------------
The restore path composes a Drive object from exactly two database columns::

    <storage_backends.endpoint>/<file_records.storage_key>

(see ``myUtils/rclone_storage.download_artifact`` and
``myUtils.media_remote_storage.download_from_backend``). Keeping the
``storage_key`` semantics identical — it stays the path *relative to its media
root*, exactly as today — means the only column the migration has to change is
``storage_backend_id`` (to point at the tiered backend row). A physical object
move on Drive is then reflected purely by the endpoint, never by the key, so a
half-applied migration cannot silently produce a ``endpoint/key`` pair that
points at the wrong bytes.

Legacy ``sau/<root>`` objects remain valid and are treated as a third, implicit
tier for as long as their rows point at the legacy backend. Nothing here
deletes anything: :func:`route_endpoint` only decides where *new* offloads go,
and ``scripts/migrate_drive_layout.py`` reports (then optionally applies) the
promotion of legacy objects to the tiered tree.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

# Local media roots that the offloader moves. ``assets`` (brand intros/outros)
# and ``archive`` (superseded originals) are deliberately not part of this set:
# they are not part of the per-publish inbox/published lifecycle.
MEDIA_ROOTS: tuple[str, ...] = ("videoFile", "uploads", "generated")

# Lifecycle tiers a media object can live in on Drive.
TIERS: tuple[str, ...] = ("inbox", "published")

# The archive tree is campaign-structured and managed by hand; it is named
# here only so callers can recognise it, not to route into it automatically.
ARCHIVE_TIER = "archive"

SAU_PREFIX = "sau"
DEFAULT_REMOTE = "GDrive-willywang8216"


def normalise_root(root: str) -> str:
    """Return ``root`` as a known media root or raise ``ValueError``."""
    value = str(root or "").strip().strip("/")
    if value not in MEDIA_ROOTS:
        raise ValueError(f"unknown media root: {root!r}")
    return value


def normalise_tier(tier: str) -> str:
    value = str(tier or "").strip().strip("/")
    if value not in TIERS:
        raise ValueError(f"unknown media tier: {tier!r}")
    return value


def legacy_endpoint(root: str) -> str:
    """The original, untiered endpoint (``sau/videoFile``)."""
    return f"{SAU_PREFIX}/{normalise_root(root)}"


def tier_endpoint(tier: str, root: str) -> str:
    """The tiered endpoint a *new* offload is written to."""
    return f"{SAU_PREFIX}/{normalise_tier(tier)}/{normalise_root(root)}"


def route_endpoint(published: bool, root: str) -> str:
    """Endpoint for an offload: published assets vs material awaiting publish."""
    return tier_endpoint("published" if published else "inbox", root)


def split_endpoint(endpoint: str | None) -> tuple[str | None, str | None]:
    """Split an rclone endpoint into ``(tier, root)``.

    ``sau/videoFile`` -> ``(None, "videoFile")`` (legacy, untiered)
    ``sau/inbox/generated`` -> ``("inbox", "generated")``
    Anything else -> ``(None, None)`` so callers fail closed rather than
    guessing a destination.
    """
    parts = str(endpoint or "").strip().strip("/").split("/")
    if len(parts) == 2 and parts[0] == SAU_PREFIX and parts[1] in MEDIA_ROOTS:
        return None, parts[1]
    if (
        len(parts) == 3
        and parts[0] == SAU_PREFIX
        and parts[1] in TIERS
        and parts[2] in MEDIA_ROOTS
    ):
        return parts[1], parts[2]
    return None, None


def split_media_path(file_path: str | None) -> tuple[str, str] | None:
    """Normalise any stored/absolute media path to ``(root, relative_key)``.

    ``file_records.file_path`` carries several historical shapes:

    * host absolute: ``/home/will/social-auto-upload/videoFile/_batch/x.mp4``
    * container absolute: ``/app/uploads/foo.png``
    * rooted relative: ``videoFile/_batch/x.mp4`` or ``generated/campaigns/...``
    * bare relative: ``_homealone/x.mp4`` (implicitly under ``videoFile``)

    Returning a canonical ``(root, key)`` pair lets the classifier compare a
    payload's ``local_path`` against a record's ``file_path`` regardless of
    which shape each was written in.
    """
    raw = str(file_path or "").strip().replace("\\", "/")
    if not raw:
        return None
    candidates = [raw]
    # Strip a host/container prefix down to the first media-root marker.
    for root in MEDIA_ROOTS:
        for prefix in (f"/{root}/", f"{root}/"):
            if prefix in raw:
                tail = raw.split(prefix, 1)[1]
                candidates.append(f"{root}/{tail}")
                break

    for candidate in candidates:
        text = candidate.strip("/")
        for root in MEDIA_ROOTS:
            if text == root:
                return root, ""
            if text.startswith(root + "/"):
                return root, text[len(root) + 1 :]
    # A bare path is stored under videoFile by convention.
    return "videoFile", raw.strip("/")


def is_archive(endpoint: str | None) -> bool:
    return str(endpoint or "").strip("/").startswith(f"{SAU_PREFIX}/{ARCHIVE_TIER}/")


def full_remote_path(endpoint: str, storage_key: str) -> str:
    """``endpoint`` + ``storage_key`` exactly as the restore code joins them."""
    return "/".join(
        part
        for part in (str(endpoint or "").strip("/"), str(storage_key or "").strip("/"))
        if part
    )


def remote_spec(remote: str, endpoint: str, storage_key: str) -> str:
    return f"{remote}:{full_remote_path(endpoint, storage_key)}"


def published_refs_from_payloads(
    rows,
) -> tuple[set[int], set[tuple[str, str]]]:
    """Collect published media references from succeeded publish target payloads.

    ``rows`` is any iterable of ``(status, payload_json)`` pairs (typically the
    ``publish_job_targets`` join against ``publish_jobs``). Only ``succeeded``
    targets count: a file that is merely referenced by a pending or failed
    target is *new material awaiting publish*, not published history.

    Returns ``(source_file_record_ids, media_keys)`` where a media key is the
    canonical ``(root, relative_key)`` tuple from :func:`split_media_path`.
    """
    ids: set[int] = set()
    keys: set[tuple[str, str]] = set()
    for status, payload_json in rows:
        if str(status or "").strip().lower() != "succeeded":
            continue
        try:
            data = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for artifact in data.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            source_id = artifact.get("source_file_record_id")
            if source_id:
                try:
                    ids.add(int(source_id))
                except (TypeError, ValueError):
                    pass
            local_path = artifact.get("local_path")
            if local_path:
                key = split_media_path(local_path)
                if key is not None:
                    keys.add(key)
    return ids, keys


def tier_for_media(
    record_id: int | None,
    file_path: str | None,
    *,
    published_ids: set[int] | frozenset[int] = frozenset(),
    published_keys: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
) -> str:
    """Classify one media record as ``"published"`` or ``"inbox"``.

    A record is published when a succeeded target names it (by file-record id
    or by path). Anything else — pending, failed, cancelled, or never
    referenced — is ``inbox``: it is material whose first successful publish
    has not been recorded, so it must not be mixed into published history.
    """
    if record_id is not None:
        try:
            if int(record_id) in published_ids:
                return "published"
        except (TypeError, ValueError):
            pass
    key = split_media_path(file_path)
    if key is not None and key in published_keys:
        return "published"
    return "inbox"


def migrate_endpoint(endpoint: str | None, tier: str) -> str:
    """Endpoint a legacy object should move to for ``tier``.

    Already-tiered endpoints are returned unchanged; unknown endpoints raise so
    a migration never invents a destination.
    """
    current_tier, root = split_endpoint(endpoint)
    if current_tier is not None:
        return str(endpoint).strip("/")
    if root is None:
        raise ValueError(f"not a recognised sau media endpoint: {endpoint!r}")
    return tier_endpoint(tier, root)


def object_move(
    remote: str,
    endpoint: str,
    storage_key: str,
    tier: str,
) -> tuple[str, str]:
    """Return ``(source_spec, destination_spec)`` for a record promotion.

    Only the endpoint changes: the storage key is preserved byte-for-byte so a
    destination can never be computed that disagrees with ``file_records``.
    """
    destination_endpoint = migrate_endpoint(endpoint, tier)
    source = remote_spec(remote, endpoint, storage_key)
    destination = remote_spec(remote, destination_endpoint, storage_key)
    return source, destination


__all__ = [
    "MEDIA_ROOTS",
    "TIERS",
    "ARCHIVE_TIER",
    "SAU_PREFIX",
    "DEFAULT_REMOTE",
    "normalise_root",
    "normalise_tier",
    "legacy_endpoint",
    "tier_endpoint",
    "route_endpoint",
    "split_endpoint",
    "split_media_path",
    "is_archive",
    "full_remote_path",
    "remote_spec",
    "published_refs_from_payloads",
    "tier_for_media",
    "migrate_endpoint",
    "object_move",
]
