#!/usr/bin/env python3
"""Reorganise the flat ``sau/archive`` tree into a date-structured layout.

The archive used to be a flat keyspace keyed only by the bundle name::

    sau/archive/my-compressed-2026-10/originals/nakedwill-sfw/SFW NW.mp4

which is easy to *write* but impossible to *manage*: a retention sweep, a
monthly audit or a "what did we archive in October?" query has to know every
bundle name and cannot be expressed as one prefix. This script migrates the
existing objects into::

    sau/archive/<YYYY>/<MM>/<bundle>/<original key...>

so the archive becomes browsable and policy-addressable by date, while each
bundle's files stay together and the original key is preserved byte-for-byte.

Design notes
------------
* The date is taken from the bundle name when it carries one
  (``my-compressed-2026-10`` -> ``2026/10``), otherwise from the object's
  ``ModTime`` on Drive, otherwise from ``--date``. Every object in one bundle
  must resolve to the same month; a bundle that would be split is reported as
  unresolved and ``--apply`` refuses rather than scattering a bundle.
* Objects already under ``<YYYY>/<MM>/...`` are skipped, so the migration is
  idempotent and resumable.
* This operates on the *physical* archive tree only. Archive objects are not
  rows in ``file_records`` (the archive endpoint is not a media root and the
  restore code never composes it), so there is no database bookkeeping here —
  unlike ``scripts/migrate_drive_layout.py``.
* Bytes are verified against the planned size immediately before each move, and
  again at the destination afterwards, using server-side ``rclone moveto`` (a
  rename, never a re-upload or delete). A size that disagrees stops that move.
  Nothing here ever issues a delete.

Usage::

    python scripts/archive_drive.py                 # dry-run, prints the plan
    python scripts/archive_drive.py --json          # machine-readable plan
    python scripts/archive_drive.py --date 2026-10  # default for undated bundles
    python scripts/archive_drive.py --apply         # operator-approved moves
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from myUtils import drive_layout  # noqa: E402

REMOTE = drive_layout.DEFAULT_REMOTE
DEFAULT_ARCHIVE_PREFIX = f"{drive_layout.SAU_PREFIX}/{drive_layout.ARCHIVE_TIER}"

_YEAR_RE = re.compile(r"^\d{4}$")
_MONTH_RE = re.compile(r"^\d{2}$")
# A date inside a bundle/file name: 2026-10, 2026-10-07, my-compressed-2026-10.
_DATE_IN_NAME_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})(?:-(\d{1,2}))?(?!\d)")


def parse_year_month(text: str | None) -> tuple[int, int] | None:
    """Extract the first ``(year, month)`` date from ``text`` or return None."""
    match = _DATE_IN_NAME_RE.search(str(text or ""))
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12):
        return None
    return year, month


def year_month_from_modtime(modtime: str | None) -> tuple[int, int] | None:
    """Parse the RFC3339 ``ModTime`` rclone emits (``2026-10-07T...Z``)."""
    raw = str(modtime or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.year, parsed.month


def is_structured(relative_path: str) -> bool:
    """True when the archive-relative path already starts ``<YYYY>/<MM>/``."""
    parts = [p for p in str(relative_path or "").strip("/").split("/") if p]
    return len(parts) >= 2 and bool(_YEAR_RE.match(parts[0])) and bool(_MONTH_RE.match(parts[1]))


def _bundle_of(relative_path: str) -> str:
    return str(relative_path or "").strip("/").split("/", 1)[0]


def _normalise_entry(entry) -> dict:
    """Accept rclone's lsjson dict shape (Path/Size/ModTime)."""
    if isinstance(entry, dict):
        return {
            "path": str(entry.get("Path") or entry.get("path") or "").strip("/"),
            "size": int(entry.get("Size", entry.get("size", 0)) or 0),
            "modtime": entry.get("ModTime") or entry.get("modtime"),
        }
    raise TypeError(f"unsupported archive entry: {entry!r}")


def plan_moves(
    entries,
    *,
    default_year_month: tuple[int, int] | None = None,
    remote: str = REMOTE,
    archive_prefix: str = DEFAULT_ARCHIVE_PREFIX,
) -> dict:
    """Build the move plan for a listing of archive objects.

    ``entries`` is any iterable of rclone ``lsjson`` dicts (or dicts with the
    same ``Path``/``Size``/``ModTime`` keys). Returns a plan dict with
    ``moves``, ``already_structured`` and ``unresolved`` lists.
    """
    prefix = str(archive_prefix).strip("/")
    moves: list[dict] = []
    already: list[dict] = []
    unresolved: list[dict] = []
    bundle_months: dict[str, set[tuple[int, int]]] = {}

    prepared = [_normalise_entry(entry) for entry in entries]
    for item in prepared:
        path = item["path"]
        if not path:
            continue
        if is_structured(path):
            already.append({"path": path, "bytes": item["size"]})
            continue
        bundle = _bundle_of(path)
        year_month = (
            parse_year_month(bundle)
            or parse_year_month(path)
            or default_year_month
            or year_month_from_modtime(item["modtime"])
        )
        if year_month is None:
            unresolved.append(
                {
                    "path": path,
                    "reason": "no date in bundle name or ModTime; pass --date YYYY-MM",
                }
            )
            continue
        bundle_months.setdefault(bundle, set()).add(year_month)
        year, month = year_month
        structured = drive_layout.structured_archive_path(
            path, year, month, prefix=prefix
        )
        moves.append(
            {
                "path": path,
                "bundle": bundle,
                "year": year,
                "month": month,
                "bytes": item["size"],
                "source": f"{remote}:{prefix}/{path}",
                "destination": f"{remote}:{structured}",
            }
        )

    # Keep bundles intact: if one bundle maps to more than one month, refuse to
    # move any of it so a bundle is never scattered across the date tree.
    conflicted = {b for b, months in bundle_months.items() if len(months) > 1}
    if conflicted:
        kept: list[dict] = []
        for move in moves:
            if move["bundle"] in conflicted:
                unresolved.append(
                    {
                        "path": move["path"],
                        "reason": f"bundle {move['bundle']!r} spans multiple months; pass --date",
                    }
                )
            else:
                kept.append(move)
        moves = kept

    return {
        "remote": remote,
        "archive_prefix": prefix,
        "moves": moves,
        "already_structured": already,
        "unresolved": unresolved,
        "summary": {
            "planned": len(moves),
            "already_structured": len(already),
            "unresolved": len(unresolved),
            "bytes_to_move": sum(m["bytes"] for m in moves),
        },
    }


def _run_rclone(config: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["rclone", "--config", str(config), *args],
        capture_output=True,
        text=True,
    )


def _remote_size_bytes(config: Path, spec: str) -> int | None:
    """Size of a single remote object, or None if it cannot be read."""
    completed = _run_rclone(config, "lsjson", "--files-only", spec)
    if completed.returncode != 0:
        return None
    try:
        entries = json.loads(completed.stdout or "[]")
    except ValueError:
        return None
    if len(entries) != 1:
        return None
    try:
        return int(entries[0]["Size"])
    except (KeyError, TypeError, ValueError):
        return None


def _list_archive(config: Path, remote: str, prefix: str, *, attempts: int = 3):
    """List every object under the archive prefix, with bounded retries."""
    import time

    last = ""
    for attempt in range(attempts):
        completed = _run_rclone(
            config, "lsjson", "-R", "--files-only", f"{remote}:{prefix}"
        )
        if completed.returncode == 0:
            try:
                return json.loads(completed.stdout or "[]")
            except ValueError as exc:  # pragma: no cover - rclone emits valid JSON
                last = f"could not parse rclone output: {exc}"
        else:
            last = completed.stderr.strip() or f"rc={completed.returncode}"
        if attempt < attempts - 1:
            time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"archive listing failed for {remote}:{prefix}: {last}")


def apply_plan(
    plan: dict,
    *,
    config: Path,
    dry_run_io: bool = False,
) -> list[dict]:
    """Move each planned object, verifying size before and after.

    ``rclone moveto`` is a server-side rename: it does not re-upload and it does
    not delete the bytes. A size that disagrees with the plan, before or after,
    stops that object (and moves it back if the destination is wrong) so a
    failed verification can never lose or misplace a file.
    """
    applied: list[dict] = []
    for move in plan["moves"]:
        source, destination, expected = move["source"], move["destination"], move["bytes"]
        if not dry_run_io:
            actual = _remote_size_bytes(config, source)
            if actual != expected:
                raise RuntimeError(
                    f"refusing to move {source}: source size {actual} != planned {expected}"
                )
            completed = _run_rclone(config, "moveto", source, destination)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"rclone moveto failed for {source}: {completed.stderr.strip()}"
                )
            landed = _remote_size_bytes(config, destination)
            if landed != expected:
                # Bytes are not where we expected them; put them back rather
                # than leave an inconsistent archive. Never delete.
                _run_rclone(config, "moveto", destination, source)
                raise RuntimeError(
                    f"destination size {landed} != planned {expected} for {destination}; "
                    "moved back to source"
                )
        applied.append(move)
    return applied


def _print_human(plan: dict) -> None:
    summary = plan["summary"]
    print(f"Mode: {'apply' if plan.get('mode') == 'apply' else 'dry-run'}  remote: {plan['remote']}")
    print(
        "Planned: {planned} moves, {bytes_to_move} bytes; "
        "already_structured={already_structured}; unresolved={unresolved}".format(**summary)
    )
    print()
    for move in plan["moves"]:
        print(f"  {move['source']}")
        print(f"    -> {move['destination']}  ({move['bytes']} bytes)")
    if plan.get("applied"):
        print(f"\nApplied {len(plan['applied'])} moves.")
    if plan["unresolved"]:
        print()
        print(f"Unresolved ({len(plan['unresolved'])}) — apply will refuse:")
        for item in plan["unresolved"][:100]:
            print(f"  {item.get('reason')}: {item.get('path')}")
    print()
    print("Nothing is ever deleted; --apply uses rclone moveto (server-side rename).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rclone-config",
        type=Path,
        default=Path.home() / ".config/rclone/rclone.conf",
    )
    parser.add_argument("--remote", default=REMOTE)
    parser.add_argument("--prefix", default=DEFAULT_ARCHIVE_PREFIX)
    parser.add_argument(
        "--date",
        default=None,
        help="default YYYY-MM for bundles with no date in the name (or ModTime)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the moves (default: dry-run, nothing changes)",
    )
    parser.add_argument("--json", action="store_true", help="emit the plan as JSON")
    args = parser.parse_args()

    default_year_month = None
    if args.date:
        default_year_month = parse_year_month(args.date)
        if default_year_month is None:
            print(f"ERROR: --date must look like YYYY-MM, got {args.date!r}", file=sys.stderr)
            return 2

    try:
        entries = _list_archive(args.rclone_config, args.remote, args.prefix)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    plan = plan_moves(
        entries,
        default_year_month=default_year_month,
        remote=args.remote,
        archive_prefix=args.prefix,
    )

    if args.apply:
        if plan["unresolved"]:
            print(
                f"Refusing --apply: {len(plan['unresolved'])} unresolved object(s) "
                "must be resolved first.",
                file=sys.stderr,
            )
            print(json.dumps(plan, indent=2, ensure_ascii=False))
            return 3
        try:
            plan["applied"] = apply_plan(plan, config=args.rclone_config)
        except Exception as exc:  # noqa: BLE001
            print(f"archive migration failed: {exc}", file=sys.stderr)
            return 4
        plan["mode"] = "apply"
    else:
        plan["mode"] = "dry-run"

    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        _print_human(plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
