#!/usr/bin/env python3
"""Fetch a representative sample of live campaign media through the real
restore path (``myUtils.media_remote_storage.download_from_backend``).

Picks one target per (platform, backed-tier) pair from the independent
classification, plus a couple of larger files, downloads each to a temp dir,
and records the byte count and sha256. Read-only against the DB; writes only
under ``/tmp``.

Usage: .venv/bin/python scripts/sample_pipeline_restore.py --min 25
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import verify_pipeline_integrity as vpi  # noqa: E402
from myUtils import media_remote_storage  # noqa: E402


def tier_of(drive_key: str) -> str:
    return "legacy" if drive_key.split("/", 1)[0] in {"videoFile", "uploads", "generated"} else "tiered"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO / "db" / "database.db"))
    ap.add_argument("--listing", default="/tmp/sau_drive_listing.txt")
    ap.add_argument("--results", default="/tmp/verify_results_fixed.json")
    ap.add_argument("--min", type=int, default=25)
    ap.add_argument("--max-bytes", type=int, default=120_000_000)
    ap.add_argument("--out", default="/tmp/sample_restore_results.json")
    args = ap.parse_args()

    sizes: dict[str, int] = {}
    for line in Path(args.listing).read_text(encoding="utf-8", errors="replace").splitlines():
        path, _, size = line.rpartition(";")
        if path:
            sizes[path] = int(float(size))

    results = json.loads(Path(args.results).read_text())["results"]
    remote = [r for r in results if r["status"] == "remote"]

    # One candidate per (platform, tier): the smallest object that is still a
    # real media file, so the sample stays fast but covers every platform/tier.
    by_platform_tier: dict[tuple[str, str], dict] = {}
    for r in remote:
        key = (r["platform"], tier_of(r["detail"]))
        if sizes.get(r["detail"], 0) > args.max_bytes:
            continue
        current = by_platform_tier.get(key)
        if current is None or sizes[r["detail"]] < sizes[current["detail"]]:
            by_platform_tier[key] = r

    chosen: list[dict] = sorted(by_platform_tier.values(), key=lambda r: (r["platform"], r["detail"]))
    # A couple of larger files to prove multi-megabyte restores work too.
    large = sorted(
        (r for r in remote if args.max_bytes >= sizes.get(r["detail"], 0) > 10_000_000),
        key=lambda r: -sizes[r["detail"]],
    )
    for candidate in large:
        if len(chosen) >= args.min:
            break
        if candidate not in chosen:
            chosen.append(candidate)
    # Fill to the requested minimum deterministically.
    for candidate in remote:
        if len(chosen) >= args.min:
            break
        if candidate not in chosen and sizes.get(candidate["detail"], 0) <= args.max_bytes:
            chosen.append(candidate)

    store = vpi.Store(Path(args.db))
    out_dir = Path("/tmp/sau_restore_sample")
    out_dir.mkdir(parents=True, exist_ok=True)

    records = []
    total = 0
    for r in chosen:
        tid = r["target_id"]
        row = store.con.execute(
            "SELECT j.payload_json FROM publish_job_targets t JOIN publish_jobs j ON j.id=t.job_id WHERE t.id=?",
            (tid,),
        ).fetchone()
        artifact = json.loads(row["payload_json"])["artifacts"][0]
        local_path = str(artifact.get("local_path") or "")
        p = vpi._resolve_media_path(local_path)
        if not p.is_absolute():
            p = REPO / p
        record = vpi._record_for(store, artifact, p, stored=local_path)
        if not record or not record.get("storage_key") or not record.get("storage_backend_id"):
            print(f"SKIP target {tid}: no backend mapping")
            continue
        backend = store.backend(record["storage_backend_id"])
        dest = out_dir / f"{tid}_{Path(record['storage_key']).name}"
        started = time.time()
        media_remote_storage.download_from_backend(dict(backend), record["storage_key"], dest)
        elapsed = time.time() - started
        actual = dest.stat().st_size
        expected = sizes.get(r["detail"], -1)
        records.append({
            "target_id": tid,
            "platform": r["platform"],
            "tier": tier_of(r["detail"]),
            "endpoint": backend.get("endpoint"),
            "storage_key": record["storage_key"],
            "bytes": actual,
            "expected_bytes": expected,
            "match": actual == expected,
            "sha256": sha256(dest),
            "seconds": round(elapsed, 2),
        })
        total += actual
        print(
            f"{r['platform']:11s} {tier_of(r['detail']):7s} "
            f"target={tid:<5d} {actual:>12,d} B  match={actual == expected}  "
            f"{record['storage_key'][-60:]}"
        )
        dest.unlink(missing_ok=True)

    print(f"\nsampled {len(records)} objects, {total:,} bytes total")
    Path(args.out).write_text(json.dumps(records, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
