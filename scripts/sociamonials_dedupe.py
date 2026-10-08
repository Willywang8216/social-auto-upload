#!/usr/bin/env python3
"""De-duplicate the Sociamonials workspace media library.

``myUtils/sociamonials_fallback.py`` uploads a target's media into the
workspace library on every publish. Because the post idempotency key is per
*target* (``sau-target-<id>``) and the upload grant carried no file-level key,
a retried target re-uploaded the identical file and the library filled with
copies. That is what pushed workspace 26985 to 99.57% of its 4 GB allowance;
the next fallback then failed with::

    media upload grant failed (HTTP 422): storage_quota_exceeded

The fallback now reuses an identical library asset instead of re-uploading
(see ``select_reusable_asset``); this script cleans up the copies that were
already made.

It lists every asset (``GET /api/v1/media/assets``, 200/page), groups them by
``(filename, size_bytes)`` - so two genuinely different files are never
collapsed - keeps one per group and deletes the rest with
``DELETE /api/v1/media/assets/{id}``. The delete is reversible for 7 days and
frees the bytes from the allowance immediately, but it is still live
third-party data: the default is a dry run and nothing is deleted until
``--apply`` is passed.

Usage::

    python scripts/sociamonials_dedupe.py                 # dry run (default)
    python scripts/sociamonials_dedupe.py --apply
    python scripts/sociamonials_dedupe.py --workspace-id 26985 --dry-run

Credentials come from ``SOCIAMONIALS_API_KEY`` / ``SOCIAMONIALS_SECRETS_FILE``
exactly as the fallback resolves them; nothing is printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myUtils import sociamonials_fallback as sm  # noqa: E402

DEFAULT_PAGE_SIZE = 200
DEFAULT_TIMEOUT = 60.0


# --------------------------------------------------------------------------- #
# Library listing / grouping / planning (pure, injectable)
# --------------------------------------------------------------------------- #

def _asset_id(asset: Mapping[str, Any]) -> int | None:
    try:
        return int(asset.get("asset_id"))
    except (TypeError, ValueError):
        return None


def _created_at(asset: Mapping[str, Any]) -> str:
    return str(asset.get("created_at") or asset.get("created") or "")


def group_duplicates(
    assets: Sequence[Mapping[str, Any]] | None,
) -> dict[tuple[str, int], list[Mapping[str, Any]]]:
    """Return only the ``(filename, size_bytes)`` groups with more than one copy."""
    groups: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for asset in assets or []:
        if not isinstance(asset, Mapping):
            continue
        filename = str(asset.get("filename") or "").strip()
        try:
            size_bytes = int(asset.get("size_bytes"))
        except (TypeError, ValueError):
            continue
        if not filename or size_bytes <= 0:
            continue
        groups[(filename, size_bytes)].append(asset)
    return {key: rows for key, rows in groups.items() if len(rows) > 1}


def choose_keeper(group: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Pick the copy to keep: a starred one if any, else the oldest upload."""
    starred = [asset for asset in group if asset.get("starred")]
    pool = starred or list(group)
    return min(pool, key=lambda asset: (_created_at(asset), _asset_id(asset) or 0))


def build_deletion_plan(
    assets: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """One entry per asset that would be deleted, with its group's keeper.

    Each entry is ``{asset, keeper}``. A group always keeps exactly one copy,
    so the last copy of a filename is never in the plan.
    """
    plan: list[dict[str, Any]] = []
    for (filename, size_bytes), group in group_duplicates(assets).items():
        keeper = choose_keeper(group)
        keeper_id = _asset_id(keeper)
        for asset in group:
            if asset is keeper or _asset_id(asset) == keeper_id:
                continue
            plan.append({"asset": asset, "keeper": keeper})
    # Largest reclaim first, so the operator sees the wins at the top.
    plan.sort(
        key=lambda entry: (
            -(int(entry["asset"].get("size_bytes") or 0)),
            str(entry["asset"].get("filename") or ""),
        )
    )
    return plan


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def list_all_assets(
    session: Any,
    headers: Mapping[str, str],
    workspace_id: str | None = None,
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
# Reporting / CLI
# --------------------------------------------------------------------------- #

def _mb(size_bytes: int) -> str:
    return f"{size_bytes / 1_000_000:.1f} MB"


def _gb(size_bytes: int) -> str:
    return f"{size_bytes / 1_000_000_000:.2f} GB"


def render_plan(plan: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for entry in plan:
        asset = entry["asset"]
        keeper = entry["keeper"]
        lines.append(
            f"  DELETE asset {_asset_id(asset)}  {_mb(int(asset.get('size_bytes') or 0)):>12}"
            f"  {asset.get('filename')}"
            f"  (keep {_asset_id(keeper)}"
            f"{', starred' if keeper.get('starred') else ''}"
            f", created {_created_at(keeper) or '?'})"
        )
    return "\n".join(lines)


def run(
    *,
    session: Any,
    api_key: str,
    workspace_id: str | None,
    apply: bool,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """List, plan and (only when ``apply``) delete; returns a summary dict."""
    headers = sm._headers(api_key)
    assets = list_all_assets(session, headers, workspace_id, timeout=timeout)
    plan = build_deletion_plan(assets)
    total_bytes = sum(int(asset.get("size_bytes") or 0) for asset in assets)
    reclaim_bytes = sum(int(entry["asset"].get("size_bytes") or 0) for entry in plan)

    print(f"workspace:        {workspace_id or sm.get_workspace_id()}")
    print(f"assets listed:    {len(assets)}")
    print(
        "library total:    "
        f"{total_bytes} bytes ({_gb(total_bytes)})"
    )
    unique_groups = len(group_duplicates(assets))
    print(f"duplicate groups: {unique_groups}")
    print(f"to delete:        {len(plan)} asset(s)")
    print(f"reclaimable:      {reclaim_bytes} bytes ({_mb(reclaim_bytes)})")
    print(f"projected usage:  {_gb(max(0, total_bytes - reclaim_bytes))} after cleanup")
    print()

    summary: dict[str, Any] = {
        "assets": len(assets),
        "groups": unique_groups,
        "planned": len(plan),
        "reclaim_bytes": reclaim_bytes,
        "deleted": 0,
        "failed": 0,
        "failures": [],
    }

    if plan:
        print("Planned deletions (one copy of each filename is kept):")
        print(render_plan(plan))
        print()

    if not apply:
        print("DRY RUN - nothing was deleted. Re-run with --apply to delete.")
        return summary

    print("Applying deletions...")
    for entry in plan:
        asset = entry["asset"]
        asset_id = _asset_id(asset)
        if asset_id is None:
            continue
        ok, reason = delete_asset(session, headers, asset_id, timeout=timeout)
        if ok:
            summary["deleted"] += 1
            print(f"  deleted asset {asset_id} ({asset.get('filename')})")
        else:
            summary["failed"] += 1
            summary["failures"].append({"asset_id": asset_id, "reason": reason})
            print(f"  SKIPPED asset {asset_id} ({asset.get('filename')}): {reason}")
    print()
    print(
        f"Done: {summary['deleted']} deleted, {summary['failed']} skipped. "
        f"Bytes reclaimed: {summary['reclaim_bytes'] if summary['failed'] == 0 else 'partial'}."
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="De-duplicate the Sociamonials media library (dry-run by default)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete the duplicate assets (default is a dry run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and delete nothing (the default)",
    )
    parser.add_argument(
        "--workspace-id",
        default=None,
        help="Sociamonials workspace_registration_id (default: configured workspace)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key (default: SOCIAMONIALS_API_KEY / secrets file)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"per-request timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply = bool(args.apply) and not bool(args.dry_run)
    api_key = str(args.api_key or sm.get_api_key()).strip()
    if not api_key:
        print(
            "No Sociamonials API key found (SOCIAMONIALS_API_KEY or "
            "SOCIAMONIALS_SECRETS_FILE).",
            file=sys.stderr,
        )
        return 2
    import requests

    session = requests.Session()
    try:
        run(
            session=session,
            api_key=api_key,
            workspace_id=args.workspace_id,
            apply=apply,
            timeout=float(args.timeout),
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
