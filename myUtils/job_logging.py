"""Structured logging helpers for the publish worker.

Every log record emitted by the worker carries a small set of correlation
fields in Loguru's ``extra`` dict:

- ``job_id``        — the publish_jobs row id, when known
- ``target_id``     — the publish_job_targets row id, when known
- ``platform``      — platform slug for the job, when known
- ``account_ref``   — account reference for the target, when known
- ``attempt``       — 1-based attempt counter for the current target run

Two sinks are configured at import time:

1. A console + ``logs/worker.log`` sink (already created by ``utils.log``)
   that scopes records via ``record["extra"]["business_name"] == 'worker'``.
2. A **per-job** file sink, created lazily on the first log line for a job
   and cleaned up when the job reaches a terminal status. Files land at
   ``logs/jobs/job-<id>.log`` and are pruned by Loguru's rotation policy.

The structured payload is also serialised as JSON when the
``SAU_JSON_LOGS`` env var is set, which is useful for ingesting into a log
aggregator. The default text format is human-readable.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from conf import BASE_DIR
from utils.log import loguru_logger, worker_logger

JOB_LOG_DIR = Path(BASE_DIR) / "logs" / "jobs"
JSON_LOGS = os.environ.get("SAU_JSON_LOGS") == "1"

# loguru handler ids keyed by job_id so we can ``logger.remove(handler_id)``
# when the job is finalised. Guarded by a lock because the worker can run
# multiple targets for the same job concurrently and would otherwise race
# on the first record.
_JOB_SINKS: dict[int, int] = {}
_JOB_SINKS_LOCK = threading.Lock()


def _record_filter_for_job(job_id: int):
    """Build a Loguru filter that only matches records bound to this job."""

    def _filter(record):
        return record["extra"].get("job_id") == job_id

    return _filter


def _format_text(record) -> str:
    extra = record["extra"]
    parts = []
    for key in ("job_id", "target_id", "platform", "account_ref", "attempt"):
        value = extra.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    correlation = " ".join(parts)
    timestamp = record["time"].strftime("%Y-%m-%d %H:%M:%S")
    level = record["level"].name
    message = record["message"]
    if correlation:
        return f"{timestamp} | {level} | [{correlation}] {message}\n"
    return f"{timestamp} | {level} | {message}\n"


def _format_json(record) -> str:
    payload: dict[str, Any] = {
        "ts": record["time"].isoformat(timespec="seconds"),
        "level": record["level"].name,
        "msg": record["message"],
    }
    extra = record["extra"]
    for key in ("job_id", "target_id", "platform", "account_ref", "attempt",
                "business_name"):
        value = extra.get(key)
        if value is not None:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _job_log_format(record) -> str:
    return _format_json(record) if JSON_LOGS else _format_text(record)


def _build_file_sink(path: Path):
    """Return a callable sink that writes formatted records to ``path``.

    Loguru's ``format=callable`` option applies ``.format_map`` to the
    returned string, which means curly braces in JSON output get treated as
    format placeholders. We sidestep that by writing through a callable
    sink instead, which receives the message verbatim.
    """

    def _write(message) -> None:
        line = _job_log_format(message.record)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    return _write


def ensure_job_sink(job_id: int) -> int:
    """Open a per-job log file sink if one does not yet exist.

    Returns the loguru handler id. Safe to call from any thread.
    """

    with _JOB_SINKS_LOCK:
        existing = _JOB_SINKS.get(job_id)
        if existing is not None:
            return existing

        JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
        sink_path = JOB_LOG_DIR / f"job-{job_id}.log"
        handler_id = loguru_logger.add(
            _build_file_sink(sink_path),
            filter=_record_filter_for_job(job_id),
            level="DEBUG",
            enqueue=True,
            # Loguru's built-in rotation/retention only applies to file
            # paths, not callable sinks. Per-job logs are short-lived and
            # cleaned up when the job finalises, so we don't lose anything
            # by skipping rotation here. Long-running operators can prune
            # ``logs/jobs/`` on a schedule if they need to.
        )
        _JOB_SINKS[job_id] = handler_id
        return handler_id


def close_job_sink(job_id: int) -> None:
    """Tear down the per-job sink, typically on terminal status."""

    with _JOB_SINKS_LOCK:
        handler_id = _JOB_SINKS.pop(job_id, None)
    if handler_id is not None:
        try:
            loguru_logger.remove(handler_id)
        except (KeyError, ValueError):
            # Already removed by another caller — that's fine.
            pass


def job_log_path(job_id: int) -> Path:
    return JOB_LOG_DIR / f"job-{job_id}.log"


def bind_job_logger(
    *,
    job_id: int | None = None,
    target_id: int | None = None,
    platform: str | None = None,
    account_ref: str | None = None,
    attempt: int | None = None,
):
    """Return a Loguru-style logger bound with correlation fields.

    All fields are optional so the helper can be reused at any depth in the
    worker — top-level loop calls bind only with ``job_id`` while a
    per-target task adds ``target_id`` and ``account_ref``.
    """

    extras: dict[str, Any] = {"business_name": "worker"}
    if job_id is not None:
        extras["job_id"] = job_id
        ensure_job_sink(job_id)
    if target_id is not None:
        extras["target_id"] = target_id
    if platform is not None:
        extras["platform"] = platform
    if account_ref is not None:
        extras["account_ref"] = account_ref
    if attempt is not None:
        extras["attempt"] = attempt
    return worker_logger.bind(**extras)
