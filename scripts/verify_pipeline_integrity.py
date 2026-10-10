#!/usr/bin/env python3
"""Independent verification: can every live publish target get its media?

This mirrors the *actual* worker resolution path in
``myUtils/worker.py::_ensure_artifact_paths_local`` (campaign payloads) and
``_try_download_from_storage`` (non-campaign), WITHOUT downloading every file.

For each live (pending/retrying/running) target artifact it computes which
recovery source the worker would use and whether that source is provably
present:

  * local file exists on disk
  * a file_records row (generated-own / source id / path convention) with a
    storage_key+backend whose ``endpoint/storage_key`` object exists on Drive
  * a campaign_artifacts row with remote_path+backend / public_url
  * the artifact's own public_url
  * media_assets public_url
  * storage_cdn_url

Drive membership is checked against a listing snapshot produced separately by
``rclone lsf GDrive-willywang8216:sau -R --files-only --format ps``.

Read-only. Exit code is non-zero if any live target is unresolvable.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myUtils.drive_layout import MEDIA_ROOTS  # noqa: E402
from myUtils.worker import _normalise_artifact_url, _public_https_url, _resolve_media_path  # noqa: E402

BASE_DIR = REPO


def load_drive_listing(path: Path) -> dict[str, int]:
    """Map ``sau-relative path -> size`` from ``rclone lsf --format ps``."""
    objects: dict[str, int] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            # ``rclone lsf --format ps`` => "<path>;<size>" (semicolon)
            rel, _, size_text = line.partition(";")
            try:
                size = int(float((size_text or "0").replace(",", "")))
            except ValueError:
                size = -1
            objects[rel.strip()] = size
    return objects


def _full_drive_key(endpoint: str | None, storage_key: str | None) -> str | None:
    endpoint = str(endpoint or "").strip().strip("/")
    # The listing snapshot is taken from ``<remote>:sau`` so its paths are
    # relative to ``sau``; strip that leading segment before joining.
    if endpoint.startswith("sau/"):
        endpoint = endpoint[len("sau/"):]
    key = str(storage_key or "").strip().strip("/")
    if not key:
        return None
    return "/".join(part for part in (endpoint, key) if part)


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        self.con = con
        self.backends = {
            r["id"]: dict(r) for r in con.execute("SELECT * FROM storage_backends")
        }
        self.file_records_by_id = {
            r["id"]: dict(r) for r in con.execute("SELECT * FROM file_records")
        }
        self.file_records_by_path: dict[str, list[dict]] = {}
        for rec in self.file_records_by_id.values():
            self.file_records_by_path.setdefault(str(rec["file_path"]), []).append(rec)
        self.campaign_artifacts_by_path: dict[str, list[dict]] = {}
        for r in con.execute("SELECT * FROM campaign_artifacts"):
            self.campaign_artifacts_by_path.setdefault(str(r["local_path"]), []).append(dict(r))

    def backend(self, backend_id):
        try:
            return self.backends.get(int(backend_id))
        except (TypeError, ValueError):
            return None


def _record_for(store: Store, artifact: dict, path: Path, *, stored: str | None) -> dict | None:
    """Faithful port of worker._ensure_artifact_paths_local._record_for."""
    source_id = artifact.get("source_id") or artifact.get("source_file_record_id")
    kind = str(artifact.get("artifact_kind") or "").strip().lower()
    derived = kind in {"watermarked_video", "watermarked_image", "generated"}

    raw = str(path).replace("\\\\", "/")
    for marker in ("/generated/", "generated/"):
        if marker in raw:
            rel = raw.split(marker, 1)[1]
            base_generated = str(Path(BASE_DIR) / "generated") + "/"
            generated_refs = ["generated/" + rel]
            if base_generated + rel not in generated_refs:
                generated_refs.append(base_generated + rel)
            for file_ref in generated_refs:
                rows = store.file_records_by_path.get(file_ref)
                if rows:
                    return rows[0]
            campaign_refs = [str(path)]
            if stored and str(stored) not in campaign_refs:
                campaign_refs.append(str(stored))
            for campaign_ref in campaign_refs:
                for row in store.campaign_artifacts_by_path.get(campaign_ref, []):
                    if row.get("public_url") is not None or row.get("remote_path") is not None:
                        return {
                            "storage_key": row.get("remote_path"),
                            "storage_backend_id": row.get("storage_backend_id"),
                            "storage_cdn_url": row.get("public_url"),
                            "file_path": row.get("local_path"),
                        }
            return None

    if source_id and not derived:
        rec = store.file_records_by_id.get(int(source_id))
        if rec:
            return rec

    raw = str(path)
    for marker in ("/videoFile/", "/uploads/"):
        if marker in raw:
            rel = raw.split(marker, 1)[1]
            if marker == "/uploads/":
                rel = "uploads/" + rel
            rows = store.file_records_by_path.get(rel)
            if rows:
                return rows[0]
            rows = store.file_records_by_path.get("videoFile/" + rel)
            if rows:
                return rows[0]
            return None
    return None


def classify_artifact(store: Store, artifact: dict, drive: dict[str, int], local_paths: dict[str, bool]):
    """Return (status, source, detail, bytes) for one artifact.

    status in {local, remote, cdn, public_url, none}
    """
    local_path = str(artifact.get("local_path") or "")
    if not local_path:
        return ("none", "no_local_path", local_path, None)

    p = _resolve_media_path(local_path)
    if not p.is_absolute():
        p = Path(BASE_DIR) / p

    if local_paths.get(str(p)) or p.exists():
        return ("local", "on_disk", str(p), None)

    kind = str(artifact.get("artifact_kind") or "").strip().lower()
    is_generated = kind in {"watermarked_video", "watermarked_image", "generated"}
    try:
        if p.resolve().is_relative_to((Path(BASE_DIR) / "generated").resolve()):
            is_generated = True
    except (OSError, ValueError):
        pass

    row = _record_for(store, artifact, p, stored=local_path)
    synthetic = False
    if row is None and is_generated:
        row = {"storage_key": None, "storage_backend_id": None, "storage_cdn_url": None, "file_path": str(p)}
        synthetic = True
    if row is None:
        # Worker raises MediaRestoreError right here, before trying public_url.
        return ("none", "no_file_record", local_path, None)

    # Try 1: backend (endpoint + storage_key)
    if row.get("storage_key") and row.get("storage_backend_id"):
        backend = store.backend(row["storage_backend_id"])
        if backend:
            key = _full_drive_key(backend.get("endpoint"), row["storage_key"])
            if key and key in drive:
                return ("remote", f"drive:{backend.get('provider')}:{backend.get('endpoint')}", key, drive[key])
            return ("none", f"drive_missing:{key}", local_path, None)

    # Try 2: artifact public_url
    public_url = str(artifact.get("public_url") or "")
    if _public_https_url(public_url):
        return ("public_url", "artifact_public_url", public_url, None)

    # Try 3: storage_cdn_url
    if row.get("storage_cdn_url"):
        return ("cdn", "storage_cdn_url", str(row["storage_cdn_url"]), None)

    return ("none", "no_recovery_source", local_path, None)


def probe_url(url: str) -> tuple[int, int]:
    """Fetch the first KiB of ``url``; return ``(status, bytes)``.

    A 200/206 proves the URL serves bytes. Any other status is a failure the
    worker would hit when it tries the same URL at publish time.
    """
    import urllib.error
    import urllib.request

    # Rows still carry raw spaces; the worker normalises before fetching, so
    # the probe must too or it reports a spurious InvalidURL.
    url = _normalise_artifact_url(url)
    request = urllib.request.Request(
        url, headers={"Range": "bytes=0-1023", "User-Agent": "sau-verify/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read(1024)
            return int(getattr(response, "status", 200) or 200), len(data)
    except urllib.error.HTTPError as exc:
        return int(exc.code), 0
    except Exception:  # noqa: BLE001
        return 0, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO / "db" / "database.db"))
    ap.add_argument("--drive-listing", default="/tmp/sau_drive_listing.txt")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--probe-public-url", action="store_true")
    ap.add_argument(
        "--min-media-bytes",
        type=int,
        default=0,
        help="treat a resolved Drive object smaller than this as a placeholder and "
             "fall through to the artifact public_url (mirrors the worker fix)",
    )
    args = ap.parse_args()

    store = Store(Path(args.db))
    drive = load_drive_listing(Path(args.drive_listing))

    rows = store.con.execute(
        """
        SELECT t.id AS tid, t.job_id, t.account_ref, t.file_ref, t.status,
               j.platform, j.payload_json
        FROM publish_job_targets t
        JOIN publish_jobs j ON j.id = t.job_id
        WHERE t.status IN ('pending','retrying','running')
        ORDER BY t.id
        """
    ).fetchall()

    import collections

    cats = collections.Counter()
    reasons = collections.Counter()
    per_platform = collections.defaultdict(collections.Counter)
    unresolved = []
    results = []
    local_paths: dict[str, bool] = {}

    for r in rows:
        payload = json.loads(r["payload_json"] or "{}")
        artifacts = payload.get("artifacts") or []
        target_cats = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            status, source, detail, nbytes = classify_artifact(store, artifact, drive, local_paths)
            if (
                args.min_media_bytes
                and status == "remote"
                and nbytes is not None
                and 0 <= nbytes < args.min_media_bytes
            ):
                # The worker now rejects a download that does not carry the
                # media signature; a placeholder Drive object must fall through
                # to the artifact's own public URL instead.
                fallback_url = str(artifact.get("public_url") or "")
                if _public_https_url(fallback_url):
                    status, source, detail, nbytes = (
                        "public_url",
                        "artifact_public_url_after_placeholder",
                        fallback_url,
                        None,
                    )
                else:
                    status, source, detail, nbytes = (
                        "none",
                        "placeholder_no_fallback",
                        str(artifact.get("local_path") or ""),
                        None,
                    )
            if status == "public_url" and args.probe_public_url:
                code, got = probe_url(detail)
                if code not in (200, 206) or got == 0:
                    status, source = "none", f"public_url_dead_http_{code}"
            cats[status] += 1
            reasons[source] += 1
            per_platform[r["platform"]][status] += 1
            target_cats.append((status, source, detail, nbytes))
            results.append({
                "target_id": r["tid"], "job_id": r["job_id"], "platform": r["platform"],
                "account_ref": r["account_ref"], "status": status, "source": source,
                "detail": detail, "bytes": nbytes,
                "local_path": artifact.get("local_path"),
            })
        ok = all(c[0] != "none" for c in target_cats)
        if not ok:
            unresolved.append({
                "target_id": r["tid"], "job_id": r["job_id"], "platform": r["platform"],
                "account_ref": r["account_ref"], "status": r["status"],
                "artifacts": [
                    {"local_path": a.get("local_path"), "artifact_kind": a.get("artifact_kind"),
                     "source_file_record_id": a.get("source_file_record_id"),
                     "public_url": a.get("public_url")}
                    for a in artifacts
                ],
                "resolution": target_cats,
            })

    print(f"live targets (pending/retrying/running): {len(rows)}")
    print(f"artifact resolution categories: {dict(cats)}")
    print(f"recovery reasons: {dict(reasons)}")
    print("per-platform:")
    for plat in sorted(per_platform):
        print(f"  {plat:12s} {dict(per_platform[plat])}")
    print(f"unresolved targets: {len(unresolved)}")
    for u in unresolved[:50]:
        print("  UNRESOLVED", u)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "live_targets": len(rows),
            "categories": dict(cats),
            "reasons": dict(reasons),
            "per_platform": {k: dict(v) for k, v in per_platform.items()},
            "unresolved": unresolved,
            "results": results,
        }, indent=2))
        print(f"wrote {args.json_out}")

    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
