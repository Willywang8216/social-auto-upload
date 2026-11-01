"""Explicit process roles and worker-owned maintenance startup."""
from __future__ import annotations

import os
from pathlib import Path


def process_role() -> str:
    role = os.environ.get("SAU_PROCESS_ROLE", "web").strip().lower()
    if role not in {"web", "worker", "embedded"}:
        raise ValueError("SAU_PROCESS_ROLE must be web, worker, or embedded")
    if role == "embedded" and os.environ.get("APP_ENV", "").lower() == "production":
        raise ValueError("Production must use a separate worker")
    return role


def embedded_enabled() -> bool:
    return process_role() == "embedded" and os.environ.get("SAU_TESTING") != "1"


def claims_paused(db_path: Path) -> bool:
    configured = os.environ.get("SAU_PAUSE_CLAIMS_FILE")
    return Path(configured or str(Path(db_path).parent / "publish.pause")).exists()


def start_worker_maintenance(db_path: Path) -> None:
    """Called once by the worker CLI, never during module import."""
    from myUtils import jobs, tg_review
    try:
        interval = int(os.environ.get("SAU_TG_REVIEW_POLL_SECONDS", "15"))
    except ValueError:
        interval = 15
    if interval > 0 and tg_review.is_configured():
        tg_review.start_poller(
            interval_seconds=interval, db_path=db_path,
            cancel_target=lambda target: jobs.cancel_target(target, db_path=db_path),
            reschedule_target=lambda target, when: jobs.reschedule_target(target, when, db_path=db_path),
        )
