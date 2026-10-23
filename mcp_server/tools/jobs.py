"""Publish-job MCP tools.

Wraps ``myUtils.jobs`` for list / get / cancel / synchronous drain.
The publish pipeline itself lives in ``tools/publish.py`` (orchestrator).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from mcp_server._shared import error_payload, resolve_db_path


def _platform_path_candidates(
    *,
    env_var: str,
    relative: str,
    posix_fallbacks: tuple[str, ...],
) -> list[str]:
    """Absolute paths to probe, portable across POSIX and Windows.

    These probes used to be hardcoded ``/home/will/...`` and ``/app/...``
    literals. On Windows neither exists, so ``jobs_system_health`` reported an
    empty inbox and a null offload block with nothing to indicate the lookup
    had simply missed — a silent wrong answer, which is worse than an error.

    Order: an explicit environment override, then ``BASE_DIR``-relative (the
    checkout root on either OS), then the historical POSIX locations so the
    Linux and Docker deployments keep behaving byte-for-byte as before.
    """
    candidates: list[str] = []
    override = str(os.environ.get(env_var, "") or "").strip()
    if override:
        candidates.append(override)
    try:
        from utils.conf_defaults import BASE_DIR

        candidates.append(str(Path(BASE_DIR) / relative))
    except Exception:  # noqa: BLE001 - a missing BASE_DIR must not break health
        pass
    candidates.extend(posix_fallbacks)
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _inbox_state_candidates() -> list[str]:
    """State files for the phone-first SAU-Inbox queue.

    ``SAU_WATCH_STATE`` is the same override ``myUtils/inbox_archive.py``
    already honours, so pointing the watcher elsewhere also moves this probe.
    """
    return _platform_path_candidates(
        env_var="SAU_WATCH_STATE",
        relative=os.path.join("sau-inbox", "state.json"),
        posix_fallbacks=("/home/will/sau-inbox/state.json", "/app/sau-inbox/state.json"),
    )


def _offload_log_candidates() -> list[str]:
    """Logs written by the Drive offload pass (``offload_to_drive.sh``)."""
    return _platform_path_candidates(
        env_var="SAU_OFFLOAD_LOG",
        relative=os.path.join("logs", "offload.log"),
        posix_fallbacks=(
            "/home/will/social-auto-upload/logs/offload.log",
            "/app/logs/offload.log",
        ),
    )


def _job_to_payload(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "idempotencyKey": job.idempotency_key,
        "platform": job.platform,
        "profileId": job.profile_id,
        "status": job.status,
        "totalTargets": job.total_targets,
        "completedTargets": job.completed_targets,
        "failedTargets": job.failed_targets,
        "createdAt": job.created_at,
        "startedAt": job.started_at,
        "finishedAt": job.finished_at,
        "payload": job.payload,
    }


def _target_to_payload(target: Any) -> dict[str, Any]:
    return {
        "id": target.id,
        "jobId": target.job_id,
        "accountRef": target.account_ref,
        "fileRef": target.file_ref,
        "scheduleAt": target.schedule_at,
        "status": target.status,
        "attempts": target.attempts,
        "lastError": target.last_error,
        "startedAt": target.started_at,
        "finishedAt": target.finished_at,
    }


def register(mcp: FastMCP) -> None:
    from myUtils import jobs as job_runtime

    @mcp.tool(
        name="jobs_list",
        description=(
            "List recent publish jobs. Optional filters: `status` "
            "(pending/running/succeeded/failed/cancelled), `platform`, "
            "`limit` (default 50)."
        ),
    )
    def jobs_list(
        status: str | None = None,
        platform: str | None = None,
        limit: int = 50,
        db_path: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            items = job_runtime.list_jobs(
                status=status,
                platform=platform,
                limit=int(limit),
                db_path=resolve_db_path(db_path),
            )
            out = []
            for j in items:
                payload_payload = _job_to_payload(j)
                payload_payload["title"] = job_runtime._action_title(j.payload)
                out.append(payload_payload)
            return out
        except Exception as exc:  # noqa: BLE001
            return [error_payload(exc)]  # type: ignore[list-item]

    @mcp.tool(
        name="jobs_calendar",
        description=(
            "List scheduled publish targets for a calendar grid. Optional "
            "filters: `month` (YYYY-MM), `platform` (slug), `status` "
            "(comma-separated, default pending,retrying,failed), `limit` "
            "(default 500, max 2000). Returns items with targetId, jobId, "
            "platform, profileId, title, accountRef, fileRef, scheduleAt, "
            "status, attempts, lastError."
        ),
    )
    def jobs_calendar(
        month: str | None = None,
        platform: str | None = None,
        status: str | None = None,
        limit: int = 500,
        db_path: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            rows = job_runtime.list_scheduled_targets(
                month=month,
                platform=platform,
                status=status,
                limit=int(limit),
                db_path=resolve_db_path(db_path),
            )
            out = []
            for target, job_meta in rows:
                out.append(
                    {
                        "targetId": target["id"],
                        "jobId": target["job_id"],
                        "platform": job_meta.get("platform"),
                        "profileId": job_meta.get("profile_id"),
                        "title": job_meta.get("title") or "Untitled",
                        "accountRef": target["account_ref"],
                        "fileRef": target["file_ref"],
                        "scheduleAt": target["schedule_at"],
                        "status": target["status"],
                        "attempts": target["attempts"],
                        "lastError": target["last_error"],
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            return [error_payload(exc)]  # type: ignore[list-item]

    @mcp.tool(
        name="jobs_target_reschedule",
        description=(
            "Move a pending/retrying job target to a new scheduled time. "
            "`scheduleAt` is an ISO-8601 datetime string. Clears the "
            "target's previous errors and attempts. Rejects running/succeeded "
            "targets."
        ),
    )
    def jobs_target_reschedule(
        target_id: int,
        schedule_at: str,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            target = job_runtime.reschedule_target(
                int(target_id), schedule_at, db_path=resolve_db_path(db_path))
            return _target_to_payload(target)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="jobs_target_cancel",
        description=(
            "Cancel a pending/retrying job target. Running targets are "
            "untouched (the in-flight upload cannot be aborted). The owning "
            "job's counters/status are recomputed."
        ),
    )
    def jobs_target_cancel(target_id: int, db_path: str | None = None) -> dict[str, Any]:
        try:
            target = job_runtime.cancel_target(
                int(target_id), db_path=resolve_db_path(db_path))
            return _target_to_payload(target)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="jobs_target_resubmit",
        description=(
            "Re-queue a failed or cancelled job target. If its original "
            "schedule time is still in the future it is kept; otherwise it "
            "is cleared so the worker picks it up immediately. Errors/attempts "
            "are reset."
        ),
    )
    def jobs_target_resubmit(target_id: int, db_path: str | None = None) -> dict[str, Any]:
        try:
            target = job_runtime.resubmit_target(
                int(target_id), db_path=resolve_db_path(db_path))
            return _target_to_payload(target)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="jobs_get",
        description="Fetch one job (plus its targets) by id.",
    )
    def jobs_get(job_id: int, db_path: str | None = None) -> dict[str, Any]:
        try:
            job = job_runtime.get_job(int(job_id), db_path=resolve_db_path(db_path))
            targets = job_runtime.list_targets(int(job_id), db_path=resolve_db_path(db_path))
            body = _job_to_payload(job)
            body["targets"] = [_target_to_payload(t) for t in targets]
            return body
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="jobs_cancel",
        description=(
            "Cancel a queued or running job. Targets already in flight "
            "complete naturally; the job transitions to `cancelled` once "
            "they finish."
        ),
    )
    def jobs_cancel(job_id: int, db_path: str | None = None) -> dict[str, Any]:
        try:
            job = job_runtime.cancel_job(int(job_id), db_path=resolve_db_path(db_path))
            return _job_to_payload(job)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="jobs_run",
        description=(
            "Drain the publish_jobs queue synchronously in the current "
            "process. Convenience tool for dev/single-instance deployments. "
            "Production should run the dedicated worker via "
            "``python -m myUtils.worker`` and skip this tool."
        ),
    )
    def jobs_run(db_path: str | None = None) -> dict[str, Any]:
        try:
            from sau_backend import default_executor, run_worker_drain
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)
        try:
            run_worker_drain(default_executor)
            return {"drained": True}
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="jobs_system_health",
        description=(
            "SAU system health overview. Returns publish-jobs and target "
            "counts by status plus inbox watcher status (ready/pending/"
            "quarantined) and the last offload-to-Drive run (exit code, "
            "files, time). All read-only."
        ),
    )
    def jobs_system_health(db_path: str | None = None) -> dict[str, Any]:
        try:
            from myUtils import jobs as _j
            import sqlite3, json, os
            resolved = resolve_db_path(db_path)
            counts = {}
            if resolved.exists():
                with sqlite3.connect(str(resolved)) as conn:
                    counts = {
                        "targets": dict(conn.execute(
                            "SELECT status, COUNT(*) FROM publish_job_targets GROUP BY status"
                        ).fetchall()),
                        "jobs": dict(conn.execute(
                            "SELECT status, COUNT(*) FROM publish_jobs GROUP BY status"
                        ).fetchall()),
                    }

            inbox = {"ready": 0, "pending": 0, "quarantined": 0, "items": {}, "statePath": ""}
            offload = {"lastRun": None, "exitCode": None, "localFiles": None, "healthy": None}
            for sp in _inbox_state_candidates():
                if os.path.exists(sp):
                    try:
                        raw = json.loads(open(sp, encoding="utf-8").read())
                        inbox = {
                            "ready": len(raw.get("ready", [])),
                            "pending": len(raw.get("pending", [])),
                            "quarantined": len(raw.get("quarantined", [])),
                            "items": {k: raw.get(k, []) for k in ("ready", "pending", "quarantined")},
                            "statePath": sp,
                        }
                    except Exception:
                        inbox = {"ready": 0, "pending": 0, "quarantined": 0,
                                 "items": {}, "statePath": sp, "error": "unreadable"}
                    break
            for logp in _offload_log_candidates():
                if os.path.exists(logp):
                    for line in reversed(open(logp, encoding="utf-8", errors="replace").read().splitlines()):
                        if "offload done" in line and "local videoFile=" in line:
                            import re
                            m = re.search(r"rc=(\d+)", line)
                            m2 = re.search(r"local videoFile=(\d+)", line)
                            offload = {
                                "lastRun": line[:25],
                                "exitCode": int(m.group(1)) if m else None,
                                "localFiles": int(m2.group(1)) if m2 else None,
                                "healthy": (int(m.group(1)) == 0) if m else None,
                            }
                            break
                    break
            return {
                "publish": counts,
                "inbox": inbox,
                "offload": offload,
            }
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)
