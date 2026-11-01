"""Bounded worker child processes. No HTTP server or scheduler is imported.

The supervisor keeps leases alive. A child owns its process group (including
ffmpeg/browser children), and is killed/reaped before an interrupted operation
can be considered for recovery. Provider-side effects still need reconciliation.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path


async def terminate_group(process) -> None:
    try:
        if os.name == "posix":
            # The group can outlive its root (ffmpeg/browser descendants).
            # Reap it even when process.returncode is already populated.
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


async def run_child(arguments: list[str], *, timeout: float) -> int:
    env = {**os.environ, "SAU_TARGET_CHILD": "1", "SAU_PROCESS_ROLE": "worker"}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-m", "myUtils.target_runner", *arguments,
        cwd=str(Path(__file__).resolve().parents[1]), env=env,
        start_new_session=os.name == "posix",
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=max(1.0, timeout))
    finally:
        await terminate_group(process)


async def run_isolated_target(worker, target, *, timeout: float) -> None:
    from myUtils import delivery, jobs
    from myUtils.job_logging import bind_job_logger
    error = None
    cancelled = False
    try:
        code = await run_child(["--target", str(target.id), "--lease-token", str(target.lease_token),
                                "--db", str(worker._db_path),
                                "--max-attempts", str(worker._config.retry.max_attempts)], timeout=timeout)
        if code:
            error = RuntimeError(f"Target child exited with code {code}")
    except asyncio.CancelledError:
        cancelled = True
        error = RuntimeError("Target child was interrupted")
    except asyncio.TimeoutError:
        error = TimeoutError("Target execution deadline exceeded")
    current = jobs.get_target(target.id, db_path=worker._db_path)
    if current.status == jobs.TARGET_RUNNING and current.lease_token == target.lease_token:
        checkpoint = current.checkpoint or {}
        message = str(error or "Child exited without settling the target")
        if checkpoint.get("provider_receipt"):
            worker._transition(current, jobs.mark_target_awaiting_delivery, checkpoint, delay_seconds=30)
        elif checkpoint.get("submission_started"):
            worker._transition(current, jobs.mark_target_needs_review,
                               message + "; verify provider delivery before resubmitting")
        else:
            log = bind_job_logger(job_id=target.job_id, target_id=target.id,
                                  platform="unknown", account_ref=target.account_ref, attempt=target.attempts)
            await worker._handle_failure(current, error or RuntimeError(message), log, allow_fallback=False)
    if cancelled:
        raise asyncio.CancelledError


async def run_isolated_prep(worker, campaign, *, timeout: float) -> None:
    from myUtils import campaigns
    try:
        code = await run_child(["--campaign", str(campaign.id), "--owner", worker._prep_owner,
                                "--db", str(worker._db_path)], timeout=timeout)
        if code:
            raise RuntimeError(f"Prep child exited with code {code}")
    except (asyncio.TimeoutError, asyncio.CancelledError, RuntimeError) as exc:
        already_queued = campaigns.campaign_has_queued_posts(campaign.id, db_path=worker._db_path)
        campaigns.finish_campaign_prep(
            campaign.id, owner=worker._prep_owner,
            status=campaigns.CAMPAIGN_PUBLISHING if already_queued else campaigns.CAMPAIGN_NEEDS_REVIEW,
            last_error=None if already_queued else "Preparation interrupted/deadline exceeded; inspect before retrying",
            db_path=worker._db_path)
        if isinstance(exc, asyncio.CancelledError):
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--target", type=int)
    group.add_argument("--campaign", type=int)
    parser.add_argument("--lease-token")
    parser.add_argument("--owner")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    os.environ["SAU_TARGET_CHILD"] = "1"
    os.environ["SAU_DB_PATH"] = str(args.db)
    from myUtils import campaign_prep, campaigns, jobs
    from myUtils.worker import PublishWorker, RetryPolicy, WorkerConfig, default_executor
    worker = PublishWorker(default_executor, db_path=args.db,
                           config=WorkerConfig(retry=RetryPolicy(max_attempts=args.max_attempts)),
                           prep_runner=campaign_prep.make_default_prep_runner())
    if args.target is not None:
        target = jobs.get_target(args.target, db_path=args.db)
        if not args.lease_token or target.lease_token != args.lease_token or target.status != jobs.TARGET_RUNNING:
            return 0
        if not jobs.renew_target_lease(target.id, args.lease_token, db_path=args.db):
            return 0
        asyncio.run(worker._run_target_inline(target))
    else:
        if not args.owner:
            parser.error("--owner is required for preparation")
        worker._prep_owner = args.owner
        campaign = campaigns.get_campaign(args.campaign, db_path=args.db)
        if not campaigns.renew_campaign_prep_lease(campaign.id, args.owner, db_path=args.db):
            return 0
        asyncio.run(worker._run_campaign_prep(campaign))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
