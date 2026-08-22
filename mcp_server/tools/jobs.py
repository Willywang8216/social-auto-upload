"""Publish-job MCP tools.

Wraps ``myUtils.jobs`` for list / get / cancel / synchronous drain.
The publish pipeline itself lives in ``tools/publish.py`` (orchestrator).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from mcp_server._shared import error_payload, resolve_db_path


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
            return [_job_to_payload(j) for j in items]
        except Exception as exc:  # noqa: BLE001
            return [error_payload(exc)]  # type: ignore[list-item]

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
