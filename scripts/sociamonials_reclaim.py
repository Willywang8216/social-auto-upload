#!/usr/bin/env python3
"""Reclaim Sociamonials library assets whose posts have delivered.

The workspace allowance is what blocks X publishing through the Sociamonials
fallback.  Every fallback upload stays in the library forever, so the allowance
fills and the next upload is refused::

    media upload grant failed (HTTP 422): storage_quota_exceeded

This tool enforces the operator's progressive rule - add an asset, publish it,
then add the next one - by deleting the assets whose posts have **delivered**
and which no still-live target needs.  It never deletes:

* an asset referenced by a ``pending``/``retrying``/``running`` target;
* an asset whose post has not delivered (no ``succeeded`` target used it);
* a starred asset;
* an asset of unknown provenance that no target history accounts for.

Deletes free the bytes immediately and are reversible for 7 days via
``POST /api/v1/media/assets/{id}/restore``, but this is still live third-party
data: the default is a dry run and nothing is deleted until ``--apply``.

Usage::

    python scripts/sociamonials_reclaim.py                 # dry run (default)
    python scripts/sociamonials_reclaim.py --apply
    python scripts/sociamonials_reclaim.py --grace-seconds 600 --dry-run

Credentials come from ``SOCIAMONIALS_API_KEY`` / ``SOCIAMONIALS_SECRETS_FILE``
exactly as the fallback resolves them; nothing is printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myUtils import sociamonials_fallback as sm  # noqa: E402
from myUtils import sociamonials_reclaim as reclaim  # noqa: E402


def _mb(size_bytes: int) -> str:
    return f"{int(size_bytes) / 1_000_000:.1f} MB"


def _gb(size_bytes: int) -> str:
    return f"{int(size_bytes) / 1_000_000_000:.2f} GB"


def render_plan(plan: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for entry in plan:
        asset = entry["asset"]
        lines.append(
            f"  DELETE asset {sm._asset_id(asset)}"
            f"  {_mb(int(asset.get('size_bytes') or 0)):>12}"
            f"  {asset.get('filename')}"
            f"  (created {asset.get('created_at') or '?'}"
            f", sent {asset.get('sent_count') or 0}"
            f", last_sent {asset.get('last_sent_at') or '?'})"
        )
    return "\n".join(lines)


def render(
    *,
    workspace_id: Any,
    summary: Mapping[str, Any],
    storage_before: Mapping[str, Any] | None,
    storage_after: Mapping[str, Any] | None = None,
) -> None:
    print(f"workspace:        {workspace_id}")
    if storage_before:
        print(
            "storage before:   "
            f"used {_gb(storage_before.get('usage_bytes') or 0)} / "
            f"{_gb(storage_before.get('allowance_bytes') or 0)} allowance, "
            f"{_mb(storage_before.get('available_bytes') or 0)} free "
            f"({storage_before.get('percent_used')}%)"
        )
    print(f"assets listed:    {summary['assets']}")
    print(f"library total:    {summary['total_bytes']} bytes ({_gb(summary['total_bytes'])})")
    reasons = summary.get("reasons") or {}
    print(
        "not reclaimable:  "
        f"{reasons.get('starred', 0)} starred, "
        f"{reasons.get('live_target', 0)} live target, "
        f"{reasons.get('not_delivered', 0)} no delivered post, "
        f"{reasons.get('unmatched', 0)} unknown provenance"
    )
    if summary.get("grace_seconds"):
        print(f"grace:            {summary['grace_seconds']:.0f}s")
    print(f"to delete:        {summary['planned']} asset(s)")
    print(f"reclaimable:      {summary['reclaim_bytes']} bytes ({_mb(summary['reclaim_bytes'])})")
    print(
        "projected usage:  "
        f"{_gb(max(0, summary['total_bytes'] - summary['reclaim_bytes']))} after cleanup"
    )
    if storage_before and summary["reclaim_bytes"]:
        projected = max(
            0,
            int(storage_before.get("available_bytes") or 0) + summary["reclaim_bytes"],
        )
        print(f"projected free:   {_mb(projected)}")
    print()
    if summary.get("plan"):
        print("Planned deletions (delivered, no live target needs them):")
        print(render_plan(summary["plan"]))
        print()
    if not summary.get("applied"):
        print("DRY RUN - nothing was deleted. Re-run with --apply to delete.")
        return
    print("Applying deletions...")
    for entry in summary["failures"]:
        print(f"  SKIPPED asset {entry['asset_id']}: {entry['reason']}")
    print(
        f"Done: {summary['deleted']} deleted, {summary['failed']} skipped."
    )
    if storage_after:
        print(
            "storage after:    "
            f"used {_gb(storage_after.get('usage_bytes') or 0)} / "
            f"{_gb(storage_after.get('allowance_bytes') or 0)} allowance, "
            f"{_mb(storage_after.get('available_bytes') or 0)} free "
            f"({storage_after.get('percent_used')}%)"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reclaim Sociamonials library assets whose posts delivered "
            "(dry-run by default)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete the eligible assets (default is a dry run)",
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
        "--db",
        default=None,
        help="SAU SQLite path (default: the configured database)",
    )
    parser.add_argument(
        "--grace-seconds",
        type=float,
        default=None,
        help=(
            "hold back assets delivered more recently than this many seconds "
            "(default: SAU_SOCIAMONIALS_RECLAIM_GRACE_SECONDS or 0)"
        ),
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help="cap the total bytes reclaimed in one run (a bounded apply)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=reclaim.DEFAULT_TIMEOUT,
        help=f"per-request timeout in seconds (default: {reclaim.DEFAULT_TIMEOUT:g})",
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
    workspace_id = args.workspace_id or sm.get_workspace_id()
    if args.db:
        db_path = Path(args.db)
    else:
        from myUtils import jobs

        db_path = jobs.DB_PATH
    import requests

    session = requests.Session()
    try:
        headers = sm._headers(api_key)
        storage_before = reclaim.get_storage(
            session, headers, workspace_id, timeout=float(args.timeout)
        )
        summary = reclaim.reclaim(
            session=session,
            headers=headers,
            workspace_id=workspace_id,
            db_path=db_path,
            apply=apply,
            grace=args.grace_seconds,
            max_bytes=args.max_bytes,
            timeout=float(args.timeout),
        )
        storage_after = None
        if apply:
            storage_after = reclaim.get_storage(
                session, headers, workspace_id, timeout=float(args.timeout)
            )
        render(
            workspace_id=workspace_id,
            summary=summary,
            storage_before=storage_before,
            storage_after=storage_after,
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
