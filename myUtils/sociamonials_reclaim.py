"""Progressive reclamation of the Sociamonials media library.

The workspace storage allowance is the thing that blocks X publishing: X goes
out through the Sociamonials fallback (the direct API is out of credits), and
every fallback upload deposits a file in the workspace library, where it stays
forever.  Once the pooled allowance fills, ``POST /api/v1/media/uploads`` is
refused with ``storage_quota_exceeded`` and X stops entirely.

The operator's rule is *progressive*: add a new asset, publish it, and only
then add the next one.  This module turns that rule into a policy and a tool:

* **Delivered assets are reclaimable.**  ``sociamonials_delivered_assets``
  records the asset id the fallback attached for each target that delivered.
  An asset is reclaimable only when a post that used it actually delivered.
* **Live targets are protected.**  Any filename referenced by a
  ``pending``/``retrying``/``running`` target is never deleted, regardless of
  how many posts delivered with it.  The next run may still need the bytes.
* **Unknown provenance is kept.**  An asset whose filename no target history
  accounts for is left alone, so the tool can never delete a file it does not
  understand.
* **Deletion is reversible but still live data**, so the tool is dry-run by
  default and only deletes under ``--apply``.

Why a grace window rather than an immediate delete inside the publish path:
a delivered post can still be re-fetched by the destination platform (X pulls
the media again after the post is queued), and the delete is only reversible
for 7 days.  A short grace (``SAU_SOCIAMONIALS_RECLAIM_GRACE_SECONDS``, default
0 for the operator tool) plus live-target protection makes the delete safe; the
quota guard uses a longer grace so it never races a delivery it just made.  A
sweep (the tool, or the pre-upload guard) also batches the API calls instead of
issuing a DELETE in the middle of a publish.

Nothing here makes a network call at import time, and every entry point accepts
an injected ``session`` so tests never touch the live API.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlparse

from myUtils import sociamonials_fallback as sm

logger = logging.getLogger(__name__)

# Target states that still need the bytes. ``TARGET_CLAIMABLE`` plus ``running``
# covers the period from queueing through delivery; a terminal target does not.
LIVE_TARGET_STATUSES: tuple[str, ...] = ("pending", "retrying", "running")
# A target only means "delivered" once it is ``succeeded``. ``failed`` and
# ``cancelled`` never delivered and must never unlock a delete.
DELIVERED_TARGET_STATUSES: tuple[str, ...] = ("succeeded",)

DELIVERED_TABLE = "sociamonials_delivered_assets"
DEFAULT_PAGE_SIZE = 200
DEFAULT_TIMEOUT = 60.0

# A short held-back window after a delivery, so a media refetch in flight is not
# deleted out from under the destination platform.
GRACE_ENV = "SAU_SOCIAMONIALS_RECLAIM_GRACE_SECONDS"
DEFAULT_GRACE_SECONDS = 0.0
# The autonomous pre-upload sweep is more conservative than the operator tool:
# it will not free an asset delivered in the last few minutes, so a destination
# platform that re-fetches the media right after delivery is never raced.
DEFAULT_GUARD_GRACE_SECONDS = 600.0
# The quota guard keeps this much headroom above the file it is about to upload.
SAFETY_MB_ENV = "SAU_SOCIAMONIALS_QUOTA_SAFETY_MB"
DEFAULT_SAFETY_MB = 32

_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v"}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def basename(value: Any) -> str:
    """Return the last path component of a path or URL, percent-decoded.

    The asset identity is ``(filename, size_bytes)`` - exactly what the fallback
    reuse path matches on - so both the library listing and a target's artifact
    paths must reduce to the same filename.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    path = urlparse(raw).path if "://" in raw else raw
    name = path.rsplit("/", 1)[-1]
    return unquote(name)


def artifact_filenames(payload: Mapping[str, Any] | None) -> set[str]:
    """Every filename a job payload's artifacts could be attached under."""
    names: set[str] = set()
    for artifact in (payload or {}).get("artifacts") or []:
        if not isinstance(artifact, Mapping):
            continue
        for key in ("local_path", "public_url", "remote_path"):
            name = basename(artifact.get(key))
            if name:
                names.add(name)
    return names


def _parse_payload(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    try:
        import json

        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _looks_like_filename(value: Any) -> bool:
    name = basename(value)
    return bool(name) and "." in name and ":" not in name


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return (reference - parsed).total_seconds()


def grace_seconds() -> float:
    raw = os.environ.get(GRACE_ENV)
    try:
        return max(0.0, float(str(raw).strip())) if raw not in (None, "") else DEFAULT_GRACE_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_GRACE_SECONDS


def _guard_grace_seconds() -> float:
    """Grace for the autonomous pre-upload sweep (default 10 minutes)."""
    raw = os.environ.get(GRACE_ENV)
    if raw not in (None, ""):
        try:
            return max(0.0, float(str(raw).strip()))
        except (TypeError, ValueError):
            pass
    return DEFAULT_GUARD_GRACE_SECONDS


def safety_bytes() -> int:
    raw = os.environ.get(SAFETY_MB_ENV)
    try:
        mb = float(str(raw).strip()) if raw not in (None, "") else float(DEFAULT_SAFETY_MB)
    except (TypeError, ValueError):
        mb = float(DEFAULT_SAFETY_MB)
    return max(0, int(mb * 1_000_000))


# --------------------------------------------------------------------------- #
# Persistence: which assets delivered
# --------------------------------------------------------------------------- #

def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table(db_path: Path | str) -> None:
    """Create the delivered-asset ledger if a migration has not yet.

    The ledger is owned by Alembic (revision 0022), but the reclaim tool and a
    freshly-migrated worker can both run against a database a beat behind, so
    the ``IF NOT EXISTS`` guard keeps the tool usable without a downgrade. It
    is additive and never rewrites an existing table.
    """
    with connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DELIVERED_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL,
                workspace_registration_id INTEGER NOT NULL DEFAULT 0,
                filename TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                media_type TEXT NOT NULL DEFAULT '',
                job_id INTEGER,
                target_id INTEGER,
                post_id INTEGER,
                network TEXT NOT NULL DEFAULT '',
                delivered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reclaimed_at DATETIME,
                UNIQUE(workspace_registration_id, asset_id)
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_sm_delivered_filename "
            f"ON {DELIVERED_TABLE}(filename)"
        )
        conn.commit()


def record_delivered_assets(
    db_path: Path | str,
    assets: Sequence[Mapping[str, Any]] | None,
    *,
    workspace_id: Any = None,
    job_id: int | None = None,
    target_id: int | None = None,
    post_id: Any = None,
    network: str = "",
    delivered_at: str | None = None,
) -> int:
    """Persist the asset ids the fallback attached for a delivered target.

    Called by the worker right after a target is transitioned to ``succeeded``.
    An asset reused from an earlier run is recorded too (the insert is
    idempotent), so the ledger names every asset that has ever gone out.
    Returns the number of rows written.
    """
    rows = [a for a in (assets or []) if isinstance(a, Mapping) and sm._asset_id(a) is not None]
    if not rows:
        return 0
    ensure_table(db_path)
    stamp = delivered_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    workspace = int(workspace_id) if str(workspace_id or "").strip().lstrip("-").isdigit() else 0
    written = 0
    with connect(db_path) as conn:
        for asset in rows:
            asset_ref = sm._asset_id(asset)
            if asset_ref is None:
                continue
            conn.execute(
                f"""
                INSERT INTO {DELIVERED_TABLE}
                    (asset_id, workspace_registration_id, filename, size_bytes,
                     media_type, job_id, target_id, post_id, network, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_registration_id, asset_id) DO UPDATE SET
                    filename = excluded.filename,
                    size_bytes = excluded.size_bytes,
                    media_type = excluded.media_type,
                    job_id = excluded.job_id,
                    target_id = excluded.target_id,
                    post_id = excluded.post_id,
                    network = excluded.network,
                    delivered_at = excluded.delivered_at
                """,
                (
                    asset_ref,
                    workspace,
                    str(asset.get("filename") or ""),
                    int(asset.get("size_bytes") or 0),
                    str(asset.get("media_type") or ""),
                    job_id,
                    target_id,
                    None if post_id is None else int(post_id),
                    str(network or ""),
                    stamp,
                ),
            )
            written += 1
        conn.commit()
    return written


def mark_reclaimed(
    db_path: Path | str, asset_ids: Sequence[int], *, when: str | None = None
) -> int:
    """Stamp the ledger rows whose assets a sweep actually deleted."""
    ids = [int(asset_id) for asset_id in asset_ids]
    if not ids:
        return 0
    ensure_table(db_path)
    stamp = when or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    with connect(db_path) as conn:
        for asset_id in ids:
            cursor = conn.execute(
                f"UPDATE {DELIVERED_TABLE} SET reclaimed_at=? WHERE asset_id=?",
                (stamp, asset_id),
            )
            written += max(0, int(cursor.rowcount or 0))
        conn.commit()
    return written


def load_delivered_assets(db_path: Path | str) -> dict[int, str]:
    """Return ``{asset_id: delivered_at}`` for every recorded delivery."""
    if not Path(str(db_path)).exists():
        return {}
    try:
        ensure_table(db_path)
        with connect(db_path) as conn:
            rows = conn.execute(
                f"SELECT asset_id, delivered_at FROM {DELIVERED_TABLE}"
            ).fetchall()
    except sqlite3.Error as exc:  # noqa: BLE001 - a missing ledger is not fatal
        logger.warning("could not read the sociamonials delivered-asset ledger: %s", exc)
        return {}
    return {int(row["asset_id"]): str(row["delivered_at"] or "") for row in rows}


# --------------------------------------------------------------------------- #
# Target history
# --------------------------------------------------------------------------- #

def _target_filenames(db_path: Path | str, statuses: Sequence[str]) -> dict[str, set[int]]:
    """Map ``filename -> {target_id}`` for targets in ``statuses``.

    Reads the payload's artifact filenames (the same files ``collect_media``
    would attach) plus any legacy path-shaped ``file_ref``.  A malformed
    payload is skipped rather than aborting the sweep.
    """
    if not Path(str(db_path)).exists() or not statuses:
        return {}
    placeholders = ",".join("?" for _ in statuses)
    try:
        with connect(db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT t.id AS target_id, t.file_ref AS file_ref,
                       j.payload_json AS payload_json
                FROM publish_job_targets t
                JOIN publish_jobs j ON j.id = t.job_id
                WHERE t.status IN ({placeholders})
                """,
                tuple(statuses),
            ).fetchall()
    except sqlite3.Error as exc:  # noqa: BLE001 - no history means nothing is protected
        logger.warning("could not read publish target history: %s", exc)
        return {}
    result: dict[str, set[int]] = {}
    for row in rows:
        names = artifact_filenames(_parse_payload(row["payload_json"]))
        if _looks_like_filename(row["file_ref"]):
            names.add(basename(row["file_ref"]))
        for name in names:
            result.setdefault(name, set()).add(int(row["target_id"]))
    return result


def live_filenames(db_path: Path | str) -> dict[str, set[int]]:
    """Filenames a non-terminal target may still attach."""
    return _target_filenames(db_path, LIVE_TARGET_STATUSES)


def delivered_filenames(db_path: Path | str) -> dict[str, set[int]]:
    """Filenames a target that delivered used."""
    return _target_filenames(db_path, DELIVERED_TARGET_STATUSES)


# --------------------------------------------------------------------------- #
# Planning (pure)
# --------------------------------------------------------------------------- #

def plan_reclaim(
    assets: Sequence[Mapping[str, Any]] | None,
    *,
    live: Mapping[str, Any] | None = None,
    delivered: Mapping[str, Any] | None = None,
    delivered_asset_ids: Mapping[int, str] | None = None,
    now: datetime | None = None,
    grace_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    """Return the assets safe to delete, largest first.

    Each entry is ``{"asset", "reason"}``.  An asset is eligible only when ALL
    of the following hold; anything else is returned to the caller's keep set:

    1. it is not starred (an explicit operator pin);
    2. no live target references its filename;
    3. a delivered post is known to have used it - either the ledger names the
       asset id, or a ``succeeded`` target used the filename and the asset
       reports ``sent_count >= 1``;
    4. it is older than ``grace_seconds`` when a grace is configured.
    """
    live = live or {}
    delivered = delivered or {}
    ledger = delivered_asset_ids or {}
    plan: list[dict[str, Any]] = []
    for asset in assets or []:
        if not isinstance(asset, Mapping):
            continue
        asset_id = sm._asset_id(asset)
        filename = str(asset.get("filename") or "").strip()
        if asset_id is None or not filename:
            continue
        if asset.get("starred"):
            continue
        if filename in live:
            continue
        ledger_at = ledger.get(asset_id)
        if ledger_at is not None:
            delivered_at = ledger_at
        elif filename in delivered and int(asset.get("sent_count") or 0) >= 1:
            delivered_at = str(asset.get("last_sent_at") or asset.get("created_at") or "")
        else:
            continue
        if grace_seconds > 0:
            age = _age_seconds(delivered_at, now=now)
            if age is None or age < grace_seconds:
                continue
        plan.append({"asset": asset, "reason": "delivered"})
    plan.sort(
        key=lambda entry: (
            -(int(entry["asset"].get("size_bytes") or 0)),
            str(entry["asset"].get("filename") or ""),
        )
    )
    return plan


def partition_reasons(
    assets: Sequence[Mapping[str, Any]] | None,
    *,
    live: Mapping[str, Any] | None = None,
    delivered: Mapping[str, Any] | None = None,
    delivered_asset_ids: Mapping[int, str] | None = None,
) -> dict[str, int]:
    """Count assets by keep/reclaim reason, for reporting."""
    live = live or {}
    delivered = delivered or {}
    ledger = delivered_asset_ids or {}
    counts: dict[str, int] = {
        "reclaimable": 0,
        "starred": 0,
        "live_target": 0,
        "not_delivered": 0,
        "unmatched": 0,
    }
    for asset in assets or []:
        if not isinstance(asset, Mapping):
            continue
        asset_id = sm._asset_id(asset)
        filename = str(asset.get("filename") or "").strip()
        if asset_id is None or not filename:
            continue
        if asset.get("starred"):
            counts["starred"] += 1
        elif filename in live:
            counts["live_target"] += 1
        elif asset_id in ledger or (
            filename in delivered and int(asset.get("sent_count") or 0) >= 1
        ):
            counts["reclaimable"] += 1
        elif filename in delivered:
            counts["not_delivered"] += 1
        else:
            counts["unmatched"] += 1
    return counts


# --------------------------------------------------------------------------- #
# Live library HTTP
# --------------------------------------------------------------------------- #

def get_storage(
    session: Any,
    headers: Mapping[str, str],
    workspace_id: Any,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Mapping[str, Any] | None:
    """Read the workspace storage block, or ``None`` when it cannot be read.

    The block lives on ``GET /api/v1/workspaces/{id}/context``.  A read failure
    must never fail a publish: the documentation is explicit that when storage
    accounting is unavailable nothing is refused for storage, so the caller
    treats ``None`` as "no guard".
    """
    workspace = sm._workspace_int(workspace_id)
    if workspace is None:
        return None
    try:
        response = session.get(
            f"{sm.BASE_URL}/api/v1/workspaces/{workspace}/context",
            headers=dict(headers),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable block is not a failure
        logger.warning("sociamonials storage read failed: %s", exc)
        return None
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400 or status == 0:
        logger.info("sociamonials storage read HTTP %s; skipping the quota guard", status)
        return None
    body = sm._json_body(response)
    data = body.get("data") if isinstance(body.get("data"), Mapping) else body
    storage = data.get("storage") if isinstance(data, Mapping) else None
    return storage if isinstance(storage, Mapping) else None


def list_all_assets(
    session: Any,
    headers: Mapping[str, str],
    workspace_id: Any = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Page through the entire workspace media library (max 200 rows/page)."""
    assets: list[dict[str, Any]] = []
    offset = 0
    page_size = max(1, min(int(page_size), 200))
    workspace = sm._workspace_int(workspace_id)
    while True:
        params: dict[str, Any] = {
            "limit": page_size,
            "offset": offset,
            "sort": "date_asc",
        }
        if workspace is not None:
            params["workspace_registration_id"] = workspace
        response = session.get(
            sm.MEDIA_ASSETS_URL, headers=dict(headers), params=params, timeout=timeout
        )
        sm._raise_for_status(response, context="list media assets")
        body = sm._json_body(response)
        rows = body.get("assets")
        if not isinstance(rows, list):
            rows = body.get("results") if isinstance(body.get("results"), list) else []
        for row in rows:
            if isinstance(row, Mapping):
                assets.append(dict(row))
        total = body.get("total")
        if len(rows) < page_size:
            break
        offset += len(rows)
        if isinstance(total, int) and offset >= total:
            break
    return assets


def delete_asset(
    session: Any,
    headers: Mapping[str, str],
    asset_id: int,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Delete one asset; returns ``(deleted, reason)`` and never raises."""
    try:
        response = session.delete(
            f"{sm.MEDIA_ASSETS_URL}/{asset_id}",
            headers=dict(headers),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - one bad delete must not stop the batch
        return False, f"request failed: {exc}"
    status = int(getattr(response, "status_code", 0) or 0)
    if 0 < status < 400:
        return True, ""
    body = sm._json_body(response)
    error = body.get("error")
    if isinstance(error, Mapping):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        return False, code or message or f"HTTP {status}"
    return False, str(error or f"HTTP {status}")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def reclaim(
    *,
    session: Any,
    headers: Mapping[str, str],
    workspace_id: Any,
    db_path: Path | str,
    apply: bool = False,
    grace: float | None = None,
    max_bytes: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """List, plan and (only when ``apply``) delete eligible assets.

    Returns a summary dict; the caller decides how to render it.  The dry run
    and the apply path share the same plan, so an operator can inspect exactly
    what ``--apply`` will delete.  ``max_bytes`` caps how much a single run may
    reclaim, so a first apply can be bounded while the result is observed.
    """
    grace_value = grace_seconds() if grace is None else max(0.0, float(grace))
    assets = list_all_assets(session, headers, workspace_id, timeout=timeout)
    live = live_filenames(db_path)
    delivered = delivered_filenames(db_path)
    ledger = load_delivered_assets(db_path)
    plan = plan_reclaim(
        assets,
        live=live,
        delivered=delivered,
        delivered_asset_ids=ledger,
        grace_seconds=grace_value,
    )
    if max_bytes is not None:
        cap = max(0, int(max_bytes))
        bounded: list[dict[str, Any]] = []
        running = 0
        for entry in plan:
            size = int(entry["asset"].get("size_bytes") or 0)
            if running + size > cap:
                continue
            bounded.append(entry)
            running += size
        plan = bounded
    total_bytes = sum(int(asset.get("size_bytes") or 0) for asset in assets)
    reclaim_bytes = sum(int(entry["asset"].get("size_bytes") or 0) for entry in plan)
    summary: dict[str, Any] = {
        "assets": len(assets),
        "total_bytes": total_bytes,
        "planned": len(plan),
        "reclaim_bytes": reclaim_bytes,
        "plan": plan,
        "reasons": partition_reasons(
            assets, live=live, delivered=delivered, delivered_asset_ids=ledger
        ),
        "grace_seconds": grace_value,
        "max_reclaim_bytes": max_bytes,
        "deleted": 0,
        "failed": 0,
        "failures": [],
        "applied": bool(apply),
    }
    if not apply:
        return summary
    reclaimed_ids: list[int] = []
    for entry in plan:
        asset = entry["asset"]
        asset_id = sm._asset_id(asset)
        if asset_id is None:
            continue
        ok, reason = delete_asset(session, headers, asset_id, timeout=timeout)
        if ok:
            summary["deleted"] += 1
            reclaimed_ids.append(asset_id)
        else:
            summary["failed"] += 1
            summary["failures"].append({"asset_id": asset_id, "reason": reason})
    # Mark the ledger so a delivered asset that has been swept is visibly so.
    try:
        mark_reclaimed(db_path, reclaimed_ids)
    except sqlite3.Error as exc:  # noqa: BLE001 - bookkeeping must not undo a delete
        logger.warning("could not mark reclaimed sociamonials assets: %s", exc)
    return summary


def ensure_headroom(
    *,
    session: Any,
    headers: Mapping[str, str],
    workspace_id: Any,
    db_path: Path | str | None,
    size_bytes: int,
    timeout: float = DEFAULT_TIMEOUT,
    grace: float | None = None,
) -> dict[str, Any]:
    """Make room for one upload, reclaiming delivered assets if needed.

    Returns ``{"checked": False}`` when the storage block cannot be read (the
    upload is attempted anyway, matching the platform's own semantics).  When
    the file does not fit, eligible delivered assets are reclaimed and the block
    is re-read.  If it still does not fit, raises
    :class:`sociamonials_fallback.SociamonialsFallbackError` naming usage,
    allowance, available and needed bytes instead of letting the upload fail
    with an opaque 422.
    """
    if db_path is None:
        return {"checked": False, "reason": "no database"}
    needed = int(size_bytes) + safety_bytes()
    storage = get_storage(session, headers, workspace_id, timeout=timeout)
    if not storage or str(storage.get("status") or "").lower() != "ok":
        return {"checked": False, "reason": "storage unavailable"}
    allowance = storage.get("allowance_bytes")
    available = storage.get("available_bytes")
    if allowance is None or available is None:
        return {"checked": False, "reason": "no allowance reported"}
    available = int(available)
    if available >= needed:
        return {
            "checked": True,
            "reclaimed": 0,
            "available_bytes": available,
            "needed_bytes": needed,
        }
    logger.info(
        "sociamonials quota guard: %s bytes available, %s needed; reclaiming "
        "delivered assets before the upload",
        available,
        needed,
    )
    try:
        reclaimed = reclaim(
            session=session,
            headers=headers,
            workspace_id=workspace_id,
            db_path=db_path,
            apply=True,
            grace=grace if grace is not None else _guard_grace_seconds(),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - a reclaim failure must not block a publish
        # Reclaiming is an optimisation. If the sweep itself fails (a plan
        # without library browsing answers 403 and raises), fall through and
        # let the upload proceed: the platform is the real authority on whether
        # the bytes fit, and its refusal is still surfaced by the caller.
        logger.warning(
            "sociamonials quota guard could not reclaim delivered assets: %s", exc
        )
        return {
            "checked": True,
            "reclaimed": 0,
            "available_bytes": available,
            "needed_bytes": needed,
            "reclaim_error": str(exc),
        }
    refreshed = get_storage(session, headers, workspace_id, timeout=timeout)
    available_after = refreshed.get("available_bytes") if refreshed else None
    if available_after is None:
        # The block became unreadable right after the sweep. The platform does
        # not refuse for storage while accounting is unavailable, so attempt
        # the upload rather than failing on a stale read.
        return {
            "checked": True,
            "reclaimed": reclaimed.get("deleted", 0),
            "available_bytes": available,
            "needed_bytes": needed,
        }
    if int(available_after) >= needed:
        return {
            "checked": True,
            "reclaimed": reclaimed.get("deleted", 0),
            "available_bytes": int(available_after),
            "needed_bytes": needed,
        }
    detail = (
        f"usage={storage.get('usage_bytes')} allowance={allowance} "
        f"available={available if available_after is None else int(available_after)} "
        f"needed={needed} (file={int(size_bytes)} + safety={safety_bytes()})"
    )
    raise sm.SociamonialsFallbackError(
        "Sociamonials storage is full after reclaiming delivered assets: "
        f"{detail}. Free space with scripts/sociamonials_reclaim.py --apply "
        "or upgrade the workspace plan."
    )


def collect_used_assets(
    reference: str,
    *,
    local_path: Path | None = None,
    kind: str = "",
) -> dict[str, Any] | None:
    """Turn an ``asset://<id>`` reference into a ledger row, or ``None``.

    The fallback records the id it attached so the reclaim tool can later tie a
    delivered target to the exact asset, not merely a filename that may be
    shared by an old duplicate.
    """
    text = str(reference or "").strip()
    match = re.match(r"^asset://(\d+)$", text)
    if not match:
        return None
    size = 0
    if local_path is not None:
        try:
            size = int(Path(local_path).stat().st_size)
        except OSError:
            size = 0
    return {
        "asset_id": int(match.group(1)),
        "filename": Path(local_path).name if local_path is not None else "",
        "size_bytes": size,
        "media_type": kind,
    }
