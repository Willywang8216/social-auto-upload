"""Async worker that drains the publish_jobs queue.

The worker is a long-running coroutine. On each tick it:

1. Computes the set of accounts currently in flight (held by other tasks).
2. Claims up to ``batch_size`` more targets that don't conflict with that set.
3. Spawns one task per claimed target, each guarded by the
   ``AccountConcurrency`` slot.
4. Each task calls a pluggable ``Executor`` to actually drive the platform.
5. On success/failure the target is transitioned via ``myUtils.jobs``.

The executor is pluggable so tests can run the full job lifecycle without
launching real Playwright browsers. The default executor maps platforms to
uploader classes via a small registry.

Retry policy: a target retries up to ``max_attempts`` times. Each retry is
delayed by exponential backoff capped at ``max_backoff_seconds``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from conf import BASE_DIR
from myUtils import jobs
from utils.concurrency import AccountConcurrency, MAX_CONCURRENT_BROWSERS
from utils.log import douyin_logger as _logger  # reuse a styled sink

# A target executor takes (platform, payload, target) and returns an awaitable
# that resolves to None on success or raises on failure.
ExecutorCallable = Callable[[str, dict, jobs.Target], Awaitable[None]]


class Executor(Protocol):
    async def __call__(self, platform: str, payload: dict, target: jobs.Target) -> None:
        ...


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_backoff_seconds: float = 5.0
    max_backoff_seconds: float = 120.0

    def backoff_for(self, attempts_so_far: int) -> float:
        # attempts_so_far is the number of attempts already executed when we
        # consider whether to retry. Exponential: 5s, 10s, 20s, 40s, ...
        delay = self.base_backoff_seconds * (2 ** max(0, attempts_so_far - 1))
        return min(delay, self.max_backoff_seconds)


@dataclass(slots=True)
class WorkerConfig:
    poll_interval: float = 1.0
    batch_size: int = 4
    max_concurrent: int = MAX_CONCURRENT_BROWSERS
    retry: RetryPolicy = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.retry is None:
            self.retry = RetryPolicy()


class PublishWorker:
    def __init__(
        self,
        executor: Executor,
        *,
        config: WorkerConfig | None = None,
        db_path: Path | None = None,
    ) -> None:
        self._executor = executor
        self._config = config or WorkerConfig()
        self._db_path = db_path or jobs.DB_PATH
        self._concurrency = AccountConcurrency(self._config.max_concurrent)
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    def stop(self) -> None:
        self._stop.set()

    async def drain(self) -> None:
        """Run until the queue is empty AND no in-flight tasks remain.

        Used by the Flask process to pick up jobs synchronously inside a
        request — primarily for test environments and for backward-compatible
        single-shot publish calls.
        """

        while not self._stop.is_set():
            await self._tick()
            if not self._tasks and not self._has_pending():
                return
            await asyncio.sleep(self._config.poll_interval)

    async def run_forever(self) -> None:
        """Long-running variant for a real worker process."""

        while not self._stop.is_set():
            await self._tick()
            await asyncio.sleep(self._config.poll_interval)

    def _has_pending(self) -> bool:
        # Cheap existence check; jobs.list_targets is more informative but heavier.
        import sqlite3

        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM publish_job_targets WHERE status IN (?, ?) LIMIT 1",
                (jobs.TARGET_PENDING, jobs.TARGET_RETRYING),
            ).fetchone()
        return row is not None

    async def _tick(self) -> None:
        # Reap finished tasks first so their account_refs free up.
        done = {task for task in self._tasks if task.done()}
        for task in done:
            self._tasks.discard(task)
            # Surface unhandled task errors in the log; the target itself has
            # already been transitioned by the task body.
            exc = task.exception()
            if exc is not None:
                _logger.error(f"worker task crashed: {exc!r}")

        in_flight = self._concurrency.in_flight_accounts()
        slots_free = self._config.max_concurrent - len(self._tasks)
        if slots_free <= 0:
            return

        claimed = jobs.claim_next_targets(
            limit=min(self._config.batch_size, slots_free),
            excluded_accounts=in_flight,
            db_path=self._db_path,
        )
        for target in claimed:
            task = asyncio.create_task(self._run_target(target))
            self._tasks.add(task)

    async def _run_target(self, target: jobs.Target) -> None:
        job = jobs.get_job(target.job_id, db_path=self._db_path)
        try:
            async with self._concurrency.slot(target.account_ref):
                result = self._executor(job.platform, job.payload, target)
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            jobs.mark_target_retry(target.id, "cancelled mid-run; will retry", db_path=self._db_path)
            raise
        except Exception as exc:  # noqa: BLE001 — we want the message regardless
            await self._handle_failure(target, exc)
        else:
            jobs.mark_target_success(target.id, db_path=self._db_path)

    async def _handle_failure(self, target: jobs.Target, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {exc}"
        attempts = target.attempts  # already incremented when claimed
        if attempts >= self._config.retry.max_attempts:
            jobs.mark_target_failed(target.id, message, db_path=self._db_path)
            _logger.error(
                f"target {target.id} (job {target.job_id}, account {target.account_ref}) "
                f"failed permanently after {attempts} attempts: {message}"
            )
            return

        delay = self._config.retry.backoff_for(attempts)
        _logger.warning(
            f"target {target.id} (job {target.job_id}, account {target.account_ref}) "
            f"failed on attempt {attempts}; retrying in {delay:.1f}s: {message}"
        )
        # Sleep BEFORE flipping back to retrying so the worker can't pick the
        # row up again immediately on the next tick.
        await asyncio.sleep(delay)
        jobs.mark_target_retry(target.id, message, db_path=self._db_path)


# --------------------------- default platform registry ---------------------------


def _resolve_account_path(account_ref: str) -> Path:
    """Map an ``account_ref`` to a concrete cookie file path.

    The Flask backend stores cookie filenames relative to ``cookiesFile/``.
    The CLI / Profile path stores absolute paths. We accept both.
    """

    candidate = Path(account_ref)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    legacy = Path(BASE_DIR) / "cookiesFile" / account_ref
    if legacy.exists():
        return legacy
    return candidate  # let the uploader complain with its own error


def _resolve_file_path(file_ref: str) -> Path:
    candidate = Path(file_ref)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    legacy = Path(BASE_DIR) / "videoFile" / file_ref
    if legacy.exists():
        return legacy
    return candidate


async def default_executor(platform: str, payload: dict, target: jobs.Target) -> None:
    """Default platform router used by the Flask publish endpoints.

    The mapping mirrors what ``myUtils/postVideo.py`` did, but on a single
    target (not the full N·M product), so the worker can parallelise.
    """

    account_file = _resolve_account_path(target.account_ref)
    file_path = _resolve_file_path(target.file_ref)
    schedule_at = target.schedule_at or 0
    if isinstance(schedule_at, str) and schedule_at:
        from datetime import datetime
        schedule_at = datetime.fromisoformat(schedule_at)

    title = payload.get("title", "")
    tags = payload.get("tags", []) or []
    category = payload.get("category")
    is_draft = payload.get("isDraft", False)
    thumbnail_path = payload.get("thumbnail", "") or ""
    product_link = payload.get("productLink", "") or ""
    product_title = payload.get("productTitle", "") or ""

    if platform == "xiaohongshu":
        from uploader.xiaohongshu_uploader.main import XiaoHongShuVideo

        app = XiaoHongShuVideo(
            title=title,
            file_path=str(file_path),
            tags=tags,
            publish_date=schedule_at,
            account_file=str(account_file),
        )
        await app.main()
        return

    if platform == "tencent":
        from uploader.tencent_uploader.main import TencentVideo

        app = TencentVideo(title, str(file_path), tags, schedule_at,
                           str(account_file), category, is_draft)
        await app.main()
        return

    if platform == "douyin":
        from uploader.douyin_uploader.main import DouYinVideo

        app = DouYinVideo(
            title=title,
            file_path=str(file_path),
            tags=tags,
            publish_date=schedule_at,
            account_file=str(account_file),
            thumbnail_landscape_path=thumbnail_path or None,
            productLink=product_link,
            productTitle=product_title,
        )
        await app.douyin_upload_video()
        return

    if platform == "kuaishou":
        from uploader.ks_uploader.main import KSVideo

        app = KSVideo(title=title, file_path=str(file_path), tags=tags,
                      publish_date=schedule_at, account_file=str(account_file))
        await app.main()
        return

    raise ValueError(f"Unsupported publish platform: {platform!r}")


def run_worker_drain(executor: Executor | None = None,
                     *, config: WorkerConfig | None = None,
                     db_path: Path | None = None) -> None:
    """Block-until-empty helper used by the synchronous Flask path."""

    worker = PublishWorker(
        executor or default_executor,
        config=config,
        db_path=db_path,
    )
    asyncio.run(worker.drain())
