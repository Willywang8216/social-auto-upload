#!/usr/bin/env python3
"""Read-only container release probes. Normal readiness NEVER accepts a pause.

--allow-paused is solely for a release's verification window. It requires an
actual pause marker and a fresh draining heartbeat from this container's worker,
not the stale predecessor's heartbeat. The caller also compares Docker image IDs
and OCI revision labels; the heartbeat schema intentionally has no revision field.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.sqlite_backup import read_connection


def utc_timestamp(value: str) -> float:
    # Docker timestamps may carry nanoseconds; Python 3.10 accepts microseconds.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def worker_process() -> tuple[str, float]:
    workers = []
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    ticks = os.sysconf("SC_CLK_TCK")
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = path.read_bytes().split(b"\0")
            # Do not count docker-init/tini's '-- python -m ...' wrapper.
            if args[1:3] != [b"-m", b"myUtils.worker"]:
                continue
            # stat's comm field may contain spaces/parentheses; split after it.
            fields = (path.parent / "stat").read_text().rsplit(")", 1)[1].split()
            started = time.time() - uptime + int(fields[19]) / ticks
            workers.append((path.parent.name, started))
        except (FileNotFoundError, ProcessLookupError):
            continue
    if len(workers) != 1:
        raise ValueError("expected exactly one owned worker process")
    return workers[0]


def worker_snapshot(db_path: Path) -> dict:
    from myUtils import jobs

    # worker_health currently delegates to the legacy RW _connect adapter.
    # Confine that adapter to a mode=ro connection IN THIS PROBE PROCESS ONLY;
    # no monkeypatch enters the running worker/web, no mkdir/bootstrap is run.
    with patch.object(jobs, "_connect", read_connection):
        return jobs.worker_health(db_path=db_path, max_age_seconds=45)


def worker_ready(db_path: Path, *, allow_paused: bool, since: str | None = None) -> bool:
    from myUtils.health_checks import database_ready
    from myUtils.runtime import claims_paused

    if not database_ready(db_path)[1]:
        return False
    paused = claims_paused(db_path)
    if paused != allow_paused:
        return False
    pid, started = worker_process()
    threshold = max(started, utc_timestamp(since) if since else started)
    snapshot = worker_snapshot(db_path)
    owned = [w for w in snapshot["workers"] if w["worker_id"].startswith(pid + "-")
             and w["alive"] and w["role"] == "publisher"
             and utc_timestamp(w["heartbeat_at"]) >= threshold]
    return len(owned) == 1 and owned[0]["draining"] is allow_paused and (
        allow_paused or snapshot["ok"] is True
    )


def web_ready(revision: str, *, allow_paused: bool) -> bool:
    try:
        response = urlopen("http://127.0.0.1:5409/readyz", timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        code = response.code
        body = json.load(response)
    if body.get("revision") != revision:
        return False
    checks = body.get("checks", {})
    if not isinstance(checks, dict) or checks.get("database") != "ok" or "worker" not in checks:
        return False
    if allow_paused:
        # A fresh paused worker is verified separately inside its container.
        # Do not weaken the application's public /readyz or Docker healthcheck.
        return code == 503 and body.get("status") == "not_ready" and all(
            value == "ok" for name, value in checks.items() if name != "worker"
        ) and checks["worker"] == "worker_unavailable"
    return code == 200 and body.get("status") == "ready" and all(value == "ok" for value in checks.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("web", "worker"))
    parser.add_argument("--revision")
    parser.add_argument("--allow-paused", action="store_true")
    parser.add_argument("--since")
    args = parser.parse_args(argv)
    try:
        revision = os.environ.get("SAU_BUILD_REVISION", "")
        if not revision or revision == "unknown" or (args.revision and revision != args.revision):
            return 1
        if os.environ.get("SAU_PROCESS_ROLE") != args.role:
            return 1
        db_path = Path(os.environ["SAU_DB_PATH"])
        if args.role == "worker":
            ok = worker_ready(db_path, allow_paused=args.allow_paused, since=args.since)
        else:
            from myUtils.runtime import claims_paused
            ok = claims_paused(db_path) == args.allow_paused and web_ready(revision, allow_paused=args.allow_paused)
        return 0 if ok else 1
    except Exception:
        # A probe is not a config/debug endpoint; fail closed without secrets.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
