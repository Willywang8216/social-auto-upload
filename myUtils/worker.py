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
from myUtils import profiles as profile_registry
from myUtils import prepared_publishers
from myUtils.job_logging import (
    bind_job_logger,
    close_job_sink,
    ensure_job_sink,
)
from utils.concurrency import AccountConcurrency, MAX_CONCURRENT_BROWSERS
from utils.log import worker_logger as _logger

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
        log = bind_job_logger(
            job_id=target.job_id,
            target_id=target.id,
            platform=job.platform,
            account_ref=target.account_ref,
            attempt=target.attempts,
        )
        log.info("target claimed; starting execution")

        try:
            async with self._concurrency.slot(target.account_ref):
                result = self._executor(job.platform, job.payload, target)
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            log.warning("target cancelled mid-run; queued for retry")
            jobs.mark_target_retry(
                target.id,
                "cancelled mid-run; will retry",
                db_path=self._db_path,
            )
            self._maybe_close_job_sink(target.job_id)
            raise
        except Exception as exc:  # noqa: BLE001 — we want the message regardless
            await self._handle_failure(target, exc, log)
        else:
            transitioned = jobs.mark_target_success(
                target.id, db_path=self._db_path
            )
            if transitioned:
                log.success("target succeeded")
            else:
                # The success was a no-op because the target was cancelled
                # while we were running. Record the fact at INFO so a human
                # auditing the per-job log can see the race resolution.
                log.info(
                    "target finished after the parent job was cancelled; "
                    "executor result discarded"
                )
            self._maybe_close_job_sink(target.job_id)

    async def _handle_failure(
        self, target: jobs.Target, exc: BaseException, log
    ) -> None:
        message = f"{type(exc).__name__}: {exc}"
        attempts = target.attempts  # already incremented when claimed
        if attempts >= self._config.retry.max_attempts:
            transitioned = jobs.mark_target_failed(
                target.id, message, db_path=self._db_path
            )
            if transitioned:
                log.error(
                    f"target failed permanently after {attempts} attempts: {message}"
                )
            else:
                log.info(
                    f"target failed permanently but parent job was already "
                    f"cancelled; transition skipped: {message}"
                )
            self._maybe_close_job_sink(target.job_id)
            return

        delay = self._config.retry.backoff_for(attempts)
        log.warning(
            f"target failed on attempt {attempts}; retrying in {delay:.1f}s: {message}"
        )
        # Sleep BEFORE flipping back to retrying so the worker can't pick the
        # row up again immediately on the next tick.
        await asyncio.sleep(delay)
        jobs.mark_target_retry(target.id, message, db_path=self._db_path)

    def _maybe_close_job_sink(self, job_id: int) -> None:
        """Close the per-job log sink once the job has reached terminal status."""

        try:
            job = jobs.get_job(job_id, db_path=self._db_path)
        except LookupError:
            return
        if job.status in jobs.JOB_TERMINAL:
            close_job_sink(job_id)


# --------------------------- default platform registry ---------------------------


def _resolve_structured_account(account_ref: str):
    if not account_ref.startswith("account:"):
        return None
    account_id = int(account_ref.split(":", 1)[1])
    return profile_registry.get_account(account_id)


def _resolve_account_path(account_ref: str) -> Path:
    """Map an ``account_ref`` to a concrete cookie file path.

    The Flask backend stores cookie filenames relative to ``cookiesFile/``.
    The CLI / Profile path stores absolute paths. We accept both.
    """

    structured = _resolve_structured_account(account_ref)
    if structured is not None:
        return Path(structured.cookie_path)

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


def _prepared_artifact_local_paths(payload: dict) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for artifact in payload.get("artifacts", []) or []:
        local_path = artifact.get("local_path")
        if not local_path or local_path in seen:
            continue
        seen.add(local_path)
        paths.append(Path(local_path))
    return paths


async def _run_platform_upload(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account_file: Path,
    file_path: Path | None = None,
    thread_file_paths: list[Path] | None = None,
) -> None:
    """Per-platform dispatch.

    Split out from ``default_executor`` so the cookie-decryption wrapper
    lives in one place and the platform router stays purely about argument
    shaping.
    """

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

    if platform == "twitter":
        from uploader.twitter_uploader.main import TwitterThreadVideo

        ordered_thread_files = thread_file_paths or ([file_path] if file_path else [])
        if not ordered_thread_files:
            raise ValueError("Twitter target requires at least one thread file")

        app = TwitterThreadVideo(
            title=title,
            file_paths=[str(path) for path in ordered_thread_files],
            tags=tags,
            account_file=str(account_file),
            publish_date=schedule_at,
        )
        await app.main()
        return

    raise ValueError(f"Unsupported publish platform: {platform!r}")


async def _publish_prepared_twitter(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    file_paths = _prepared_artifact_local_paths(payload)
    if not account_file:
        raise ValueError("Prepared Twitter publish requires a cookie-backed account")
    if not file_paths:
        raise ValueError("Prepared Twitter publish requires local media artifacts")

    await _run_platform_upload(
        "twitter",
        {
            "title": payload.get("message") or payload.get("draft", {}).get("message", ""),
            "tags": payload.get("draft", {}).get("hashtags", []) or [],
            "threadFileRefs": [str(path) for path in file_paths],
        },
        target,
        account_file=account_file,
        thread_file_paths=file_paths,
    )


async def _publish_prepared_telegram(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared Telegram publish requires a structured account")
    await asyncio.to_thread(prepared_publishers.publish_telegram_sync, account, payload)


async def _publish_prepared_reddit(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared Reddit publish requires a structured account")
    await asyncio.to_thread(prepared_publishers.publish_reddit_sync, account, payload)


async def _publish_prepared_youtube(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared YouTube publish requires a structured account")
    await asyncio.to_thread(prepared_publishers.publish_youtube_sync, account, payload)


async def _publish_prepared_not_implemented(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    raise NotImplementedError(
        f"Prepared campaign publisher not implemented for {platform!r}. "
        "The campaign payload is queued correctly, but this platform still "
        "needs a runtime publisher integration."
    )


PREPARED_PUBLISHER_REGISTRY: dict[str, Callable[..., Awaitable[None]]] = {
    "twitter": _publish_prepared_twitter,
    "facebook": _publish_prepared_not_implemented,
    "instagram": _publish_prepared_not_implemented,
    "reddit": _publish_prepared_reddit,
    "telegram": _publish_prepared_telegram,
    "youtube": _publish_prepared_youtube,
    "tiktok": _publish_prepared_not_implemented,
    "threads": _publish_prepared_not_implemented,
    "discord": _publish_prepared_not_implemented,
}


async def _run_prepared_campaign_upload(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    publisher = PREPARED_PUBLISHER_REGISTRY.get(platform)
    if publisher is None:
        raise ValueError(f"Unsupported prepared publish platform: {platform!r}")
    await publisher(platform=platform, payload=payload, target=target, account=account, account_file=account_file)


async def default_executor(platform: str, payload: dict, target: jobs.Target) -> None:
    """Default platform router used by the Flask publish endpoints.

    Wraps the per-platform dispatch in
    :func:`myUtils.cookie_storage.decrypted_storage_state` so the uploader
    always sees a plaintext cookie file regardless of how the file is
    stored on disk. When ``SAU_COOKIE_ENCRYPTION_KEY`` is unset the
    helper degrades to a no-op and the uploader uses the canonical path.
    """

    from myUtils.cookie_storage import decrypted_storage_state

    structured_account = _resolve_structured_account(target.account_ref)
    canonical_account_file = _resolve_account_path(target.account_ref)
    if payload.get("campaignId") and payload.get("campaignPostId"):
        if structured_account is not None and structured_account.cookie_path:
            with decrypted_storage_state(canonical_account_file) as plain_path:
                await _run_prepared_campaign_upload(
                    platform,
                    payload,
                    target,
                    account=structured_account,
                    account_file=plain_path,
                )
        else:
            await _run_prepared_campaign_upload(
                platform,
                payload,
                target,
                account=structured_account,
                account_file=None,
            )
        return

    if platform == "twitter":
        thread_file_refs = payload.get("threadFileRefs") or [target.file_ref]
        thread_file_paths = [_resolve_file_path(file_ref) for file_ref in thread_file_refs]
        with decrypted_storage_state(canonical_account_file) as plain_path:
            await _run_platform_upload(
                platform,
                payload,
                target,
                account_file=plain_path,
                thread_file_paths=thread_file_paths,
            )
        return

    file_path = _resolve_file_path(target.file_ref)
    with decrypted_storage_state(canonical_account_file) as plain_path:
        await _run_platform_upload(
            platform,
            payload,
            target,
            account_file=plain_path,
            file_path=file_path,
        )


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


# --------------------------- standalone process entry point ---------------------------


def _build_arg_parser():
    import argparse

    # ``WorkerConfig`` and ``RetryPolicy`` are @dataclass(slots=True) so the
    # *class*-level attribute access ``WorkerConfig.batch_size`` returns a
    # slot member descriptor, not the default int. Read the defaults from a
    # freshly-instantiated dataclass instead so argparse sees real numbers.
    config_defaults = WorkerConfig()
    retry_defaults = RetryPolicy()

    parser = argparse.ArgumentParser(
        prog="python -m myUtils.worker",
        description=(
            "Drain the publish_jobs queue. Without --once the worker runs "
            "until it receives SIGINT/SIGTERM, draining in-flight targets "
            "gracefully before exiting."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain the queue and exit (default: run forever).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=config_defaults.poll_interval,
        help="Seconds between worker ticks.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=config_defaults.batch_size,
        help="Maximum targets claimed per tick.",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=MAX_CONCURRENT_BROWSERS,
        help="Maximum concurrent browsers / executor calls.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=retry_defaults.max_attempts,
        help="Retry budget per target before failing permanently.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help=(
            "Override the SQLite database path. Defaults to "
            "the value resolved by myUtils.jobs.DB_PATH."
        ),
    )
    return parser


async def _serve(worker: PublishWorker, *, run_once: bool) -> None:
    """Drive a worker honouring SIGINT/SIGTERM for graceful shutdown.

    On first signal we ask the worker to stop polling for new work; the
    inner loop finishes any in-flight tasks before returning. A second
    signal would surface as KeyboardInterrupt — we don't trap it twice
    so the operator can force-exit if a stuck task refuses to drain.
    """

    import signal

    loop = asyncio.get_running_loop()
    stopping = False

    def _on_signal(signum: int) -> None:
        nonlocal stopping
        if stopping:
            return  # second signal — let the default handler run
        stopping = True
        _logger.info(
            f"received signal {signal.Signals(signum).name}; "
            "draining in-flight targets before exiting"
        )
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except NotImplementedError:
            # Windows event loops only support a subset of signals; fall
            # back to the default handler there.
            pass

    if run_once:
        await worker.drain()
        return

    # In long-running mode, ``run_forever`` exits as soon as ``stop`` is
    # set, but we still want any in-flight tasks to finish cleanly. The
    # _tick loop already reaps them on each pass, so we explicitly wait
    # for the in-flight set to empty before returning.
    await worker.run_forever()
    while worker._tasks:  # noqa: SLF001 — drain is intentionally on the inside
        await asyncio.sleep(worker._config.poll_interval)
        await worker._tick()  # noqa: SLF001


def _cli(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    config = WorkerConfig(
        poll_interval=args.poll_interval,
        batch_size=args.batch_size,
        max_concurrent=args.max_concurrent,
        retry=RetryPolicy(max_attempts=args.max_attempts),
    )
    worker = PublishWorker(default_executor, config=config, db_path=args.db_path)

    mode = "once" if args.once else "forever"
    _logger.info(
        f"publish worker starting (mode={mode}, batch_size={config.batch_size}, "
        f"max_concurrent={config.max_concurrent}, "
        f"max_attempts={config.retry.max_attempts})"
    )
    try:
        asyncio.run(_serve(worker, run_once=args.once))
    except KeyboardInterrupt:
        _logger.warning("worker interrupted by KeyboardInterrupt")
        return 130
    _logger.info("publish worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
