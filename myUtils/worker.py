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
import contextlib
import copy
import inspect
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from utils.conf_defaults import BASE_DIR
from myUtils import jobs, platform_limits
from myUtils import bundle_social
from myUtils import campaign_prep
from myUtils import campaigns as campaign_store
from myUtils import media_remote_storage
from myUtils import media_pipeline
from myUtils import profiles as profile_registry
from myUtils import prepared_publishers
from myUtils import token_state, token_maintenance
from myUtils import content_rules
from myUtils import sociamonials_fallback
from myUtils import sociamonials_reclaim
from myUtils import failure_classification as _fc
from myUtils import pre_flight
from myUtils import delivery
from myUtils import runtime
from myUtils.sqlite_utils import connect as _db_connection
from myUtils.job_logging import (
    bind_job_logger,
    close_job_sink,
    ensure_job_sink,
)
from utils.concurrency import AccountConcurrency, MAX_CONCURRENT_BROWSERS
from utils.log import worker_logger as _logger

# Credential shapes that must never ride along into an operator alert. Alert
# bodies are delivered to chat/email channels and retained off-box, and an
# uploader exception routinely quotes the failing request URL (``?access_token=``
# etc.), so scrub before sending. Defence in depth, not a guarantee — keep
# error text short and credential-free at the source.
_ALERT_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|"
            r"client[_-]?secret|api[_-]?key|apikey|bot[_-]?token|password|"
            r"secret)\b\s*[=:]\s*[^\s&,;\"')\]}]+"
        ),
        r"\1=<redacted>",
    ),
    (
        re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]+"),
        r"\1 <redacted>",
    ),
)


# A revoked or expired credential is permanent until a human re-authorises the
# account, so it must not burn the retry budget or be re-routed to the fallback.
_CREDENTIAL_FAILURE_RE = re.compile(
    r"(HTTP 401|Unauthorized|Please log in|invalid_grant|refresh_token_revoked|"
    r"credentials? (?:are )?(?:invalid|revoked|expired)|re-?authoris|re-?authoriz)",
    re.IGNORECASE,
)


def _is_credential_failure(message: str) -> bool:
    """True when the failure is a dead credential, not a transient error."""
    return bool(_CREDENTIAL_FAILURE_RE.search(str(message or "")))


TARGET_EXECUTION_TIMEOUT_SECONDS = float(
    os.environ.get("SAU_TARGET_TIMEOUT_SECONDS", "1200") or 1200
)


def _x_direct_publish_enabled() -> bool:
    """Native first unless the operator explicitly selected the managed route."""
    raw = str(os.environ.get("SAU_X_DIRECT_PUBLISH", "")).strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def _reddit_direct_publish_enabled() -> bool:
    """Whether the direct Reddit OAuth path should run before bundle.social.

    Reddit revoked the API credentials for both configured accounts, so every
    direct submit is now a guaranteed 401. When bundle.social is configured and
    this is unset, skip the doomed direct path and hand Reddit straight to
    bundle.social (its own Reddit OAuth app for the same account). Set
    ``SAU_REDDIT_DIRECT_PUBLISH=1`` once the credentials are restored.
    """
    raw = str(os.environ.get("SAU_REDDIT_DIRECT_PUBLISH", "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return not bundle_social.is_enabled()


def _reddit_publishing_enabled() -> bool:
    """Whether Reddit targets may publish at all.

    Default on. Set ``SAU_REDDIT_ENABLED=0`` to pause Reddit entirely; the
    operator did this after the account was restricted at Reddit and neither the
    direct path nor bundle.social could post. A paused Reddit target fails
    immediately instead of burning retries or the bundle.social monthly quota.
    """
    raw = str(os.environ.get("SAU_REDDIT_ENABLED", "")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _content_guard_error(payload: dict, target: "jobs.Target", db_path: Path) -> str | None:
    """Return a reason to refuse publishing this target, or ``None``.

    Last-resort guard so a placeholder caption or a caption in the wrong
    language never reaches a platform, no matter which publisher path runs.
    """
    draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
    if not draft and "message" not in payload:
        # Not a copy-bearing publish (legacy upload payload or a bare job);
        # nothing to validate here.
        return None
    message = str(draft.get("message") or payload.get("message") or "").strip()
    if not message:
        return "empty message"
    if not content_rules.is_usable_copy(message):
        return "placeholder/generic copy"
    language = ""
    try:
        account = _resolve_structured_account(target.account_ref, db_path=db_path)
    except Exception:  # noqa: BLE001 - a missing account is not a content problem
        account = None
    if account is not None:
        config = getattr(account, "config", None) or {}
        language = str(
            config.get("audience_language") or config.get("audienceLanguage") or ""
        ).strip()
    if language and not content_rules.message_matches_language(message, language):
        return f"copy does not match account language '{language}'"
    return None


def _scrub_secrets(text: str) -> str:
    """Best-effort mask of credential-like values in free-form alert text."""

    cleaned = str(text or "")
    for pattern, replacement in _ALERT_REDACT_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    from myUtils.secret_redaction import redact_text
    return redact_text(cleaned)


def _ambiguous_transport_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return isinstance(exc, TimeoutError) or bool(re.search(
        r"timeout|timed out|connection (?:aborted|reset)|HTTP 5\d\d|ConnectionError",
        text, re.IGNORECASE))


def _retry_after_seconds(exc: BaseException) -> float:
    from email.utils import parsedate_to_datetime
    import math
    details = getattr(exc, "details", None) or {}
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    lower_headers = {str(key).lower(): value for key, value in headers.items()}
    value = details.get("retryAfter", lower_headers.get("retry-after", 0)) if isinstance(details, dict) else 0
    if not value:
        reset = details.get("rateLimitReset") if isinstance(details, dict) else None
        reset = reset or lower_headers.get("x-ratelimit-reset") or lower_headers.get("x-rate-limit-reset")
        if reset is not None:
            try:
                value = float(reset) - time.time()
            except (TypeError, ValueError, OverflowError):
                value = 0
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(str(value)).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    # A daily quota reset must not be shortened to one hour.
    return max(0.0, seconds) if math.isfinite(seconds) else 0.0


# A target executor takes (platform, payload, target) and returns an awaitable
# that resolves to None on success or raises on failure.
ExecutorCallable = Callable[[str, dict, jobs.Target], Awaitable[None]]


class Executor(Protocol):
    async def __call__(self, platform: str, payload: dict, target: jobs.Target) -> None:
        ...


# A prep runner takes a claimed campaign + db path and returns the enqueue
# result (or raises). It may be sync (ffmpeg is blocking) or async.
PrepRunnerCallable = Callable[["campaign_store.Campaign", Path], Any]


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
    # Async prep queue: how many campaigns to claim per tick, how many may run
    # concurrently (ffmpeg is heavy, so default 1), and the stale-lease window.
    prep_batch_size: int = 1
    prep_max_concurrent: int = 1
    # How long a claimed prep may hold its lease before the sweep treats it as
    # abandoned. This must exceed the slowest real prep but stay small enough
    # that a crash costs minutes, not hours: a 602 MB source re-encodes in ~15
    # min and the worst case (transcode + split + remote upload) is ~30 min, so
    # 45 min is ~1.5x headroom. The previous 120 min meant a container restart
    # mid-prep stranded the campaign in `preparing` for two hours, which is how
    # five campaigns sat unfinished (they had all been claimed, then killed by a
    # restart, and the sweep was still waiting).
    # Override with SAU_PREP_LEASE_MINUTES when a host is slower.
    prep_lease_minutes: int = 45
    preparation_warm_seconds: float = jobs.PREPARATION_WARM_SECONDS
    prerequisite_max_attempts: int = jobs.PREREQUISITE_MAX_ATTEMPTS

    def __post_init__(self) -> None:
        if self.retry is None:
            self.retry = RetryPolicy()
        self.preparation_warm_seconds = min(jobs._seconds(self.preparation_warm_seconds), jobs.PREPARATION_WARM_SECONDS)
        if type(self.prerequisite_max_attempts) is not int or not 1 <= self.prerequisite_max_attempts <= 100:
            raise ValueError("prerequisite_max_attempts must be a positive bounded integer")
        # Env overrides so an operator can tune prep without a rebuild. Read here
        # rather than as field defaults so an environment value is always
        # honoured. An explicit constructor argument still wins: compare against
        # the class defaults (never construct another WorkerConfig - that
        # recurses into this method).
        for name, env, fallback in (
            ("prep_max_concurrent", "SAU_PREP_MAX_CONCURRENT", 1),
            ("prep_batch_size", "SAU_PREP_BATCH_SIZE", 1),
            ("prep_lease_minutes", "SAU_PREP_LEASE_MINUTES", 45),
        ):
            raw = str(os.environ.get(env, "") or "").strip()
            if not raw or getattr(self, name) != fallback:
                continue
            try:
                setattr(self, name, max(1, int(raw)))
            except ValueError:
                _logger.warning(
                    f"worker config: ignoring invalid {env}={raw!r}"
                )


class PublishWorker:
    # Token refresh runs every N ticks (at 1s poll interval ≈ every 5 min by default)
    _MAINTENANCE_TICK_INTERVAL: int = 300
    # Sweep abandoned `running` targets roughly once a minute (see
    # jobs.requeue_stale_running). Cheap indexed SELECT when nothing is stale.
    _STALE_SWEEP_TICK_INTERVAL: int = 60
    _REFRESHABLE_PLATFORMS: frozenset = frozenset({
        "tiktok", "reddit", "youtube", "threads", "facebook", "instagram", "twitter",
    })
    # Platforms whose primary tracked credential is a long-lived (~60 day) token.
    # They are refreshed with a generous buffer (see sau_backend for rationale)
    # so a restart / transient upstream error near expiry can't strand the
    # account. Short-lived-access-token platforms keep the small 5-min skew.
    _LONG_LIVED_TOKEN_PLATFORMS: frozenset = frozenset({"facebook", "instagram", "threads"})

    # In-app route for the JobsView. The SPA uses hash history, so the fragment
    # below is a self-contained link. ``job`` is the publish_jobs id; it is
    # passed as a query param so the view can open the job when it grows that
    # support. Prefixed with ``SAU_PUBLIC_APP_URL`` when configured — never a
    # guessed domain.
    _JOB_ROUTE_FRAGMENT: str = "#/jobs?job={job_id}"
    try:
        _LONG_LIVED_REFRESH_MARGIN_SECONDS: int = int(
            os.environ.get("SAU_LONG_LIVED_REFRESH_MARGIN_SECONDS", str(7 * 24 * 3600)) or str(7 * 24 * 3600)
        )
    except ValueError:
        _LONG_LIVED_REFRESH_MARGIN_SECONDS = 7 * 24 * 3600

    # After a refresh fails we back off exponentially instead of retrying every
    # tick — a dead credential used to be hammered hundreds of times a day.
    # Base doubles per consecutive failure up to the cap. Only a structured
    # terminal credential/permission verdict requires manual reconnect; an
    # arbitrary number of transport failures must remain retryable.
    _MAINTENANCE_BACKOFF_BASE_SECONDS: int = 30 * 60      # 30 min after 1st failure
    _MAINTENANCE_BACKOFF_CAP_SECONDS: int = 6 * 3600      # cap at 6 h
    _MAINTENANCE_RECONNECT_THRESHOLD: int = 5  # compatibility only; not a revocation verdict

    def __init__(
        self,
        executor: Executor,
        *,
        config: WorkerConfig | None = None,
        db_path: Path | None = None,
        prep_runner: PrepRunnerCallable | None = None,
    ) -> None:
        self._executor = executor
        self._config = config or WorkerConfig()
        self._db_path = db_path or jobs.DB_PATH
        self._concurrency = AccountConcurrency(self._config.max_concurrent)
        self._preparation_slots = asyncio.Semaphore(max(1, self._config.prep_max_concurrent))
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._prep_tasks: set[asyncio.Task] = set()
        self._maintenance_counter: int = 0
        self._stale_sweep_counter: int = 0
        self._prep_sweep_counter: int = 0
        self._maintenance_task: asyncio.Task | None = None
        self._prep_runner = prep_runner
        # Stable identity for the lifetime of this worker; recorded in the
        # campaign's ``_prepLease`` so a terminal transition can verify it
        # still owns the claim.
        self._prep_owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._claimed: dict[int, jobs.Target] = {}
        self._task_by_target: dict[int, asyncio.Task] = {}
        self._payloads: dict[int, dict] = {}
        self._heartbeat_at = 0.0
        self._dispatch_ready = False

    def stop(self) -> None:
        self._stop.set()
        task = self._maintenance_task
        if task is not None and not task.done():
            task.cancel()

    async def drain(self) -> None:
        """Run until the queue is empty AND no in-flight tasks remain.

        Used by the Flask process to pick up jobs synchronously inside a
        request — primarily for test environments and for backward-compatible
        single-shot publish calls.
        """

        while not self._stop.is_set():
            await self._tick()
            if not self._tasks and not self._prep_tasks and not self._has_pending():
                await self._finish_maintenance()
                return
            await asyncio.sleep(self._config.poll_interval)
        await self._finish_shutdown()

    async def run_forever(self) -> None:
        """Long-running variant for a real worker process."""

        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:
                _logger.error(f"worker tick failed; retrying safely: {_scrub_secrets(str(exc))}")
                await asyncio.sleep(min(5.0, max(0.1, self._config.poll_interval)))
            await asyncio.sleep(self._config.poll_interval)
        await self._finish_shutdown()

    async def _finish_maintenance(self) -> None:
        task = self._maintenance_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._maintenance_task = None

    async def _finish_shutdown(self) -> None:
        await self._finish_maintenance()
        while self._tasks or self._prep_tasks:
            pending = tuple(self._tasks | self._prep_tasks)
            if pending:
                self._heartbeat()
                await asyncio.wait(pending, timeout=1.0)
            self._tasks.difference_update(
                {task for task in self._tasks if task.done()}
            )
            self._prep_tasks.difference_update(
                {task for task in self._prep_tasks if task.done()}
            )

    def _heartbeat(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._heartbeat_at < 15:
            return
        # A live process whose dispatch scan is failing must not pass readiness.
        role = "publisher" if self._dispatch_ready else "publisher_unready"
        jobs.heartbeat_worker(self._prep_owner, role=role,
                              draining=self._stop.is_set() or runtime.claims_paused(self._db_path),
                              db_path=self._db_path)
        for target in tuple(self._claimed.values()):
            if not jobs.renew_target_lease(target.id, target.lease_token,
                                          lease_seconds=180, db_path=self._db_path):
                task = self._task_by_target.get(target.id)
                if task is not None and not task.done():
                    task.cancel()
        self._heartbeat_at = time.monotonic()


    def _has_pending(self) -> bool:
        # Only targets that are due *now* count. A target scheduled for later
        # must not keep drain() spinning; the backend's publish scheduler
        # re-triggers a drain once it becomes claimable.
        if jobs.has_claimable_targets(db_path=self._db_path, prepare_ahead_seconds=self._config.preparation_warm_seconds):
            return True
        # A campaign waiting for prep is also pending work; without this a
        # --once drain would exit before preparing it. Only *claimable* ones
        # count, so a campaign leased by another live worker cannot spin this
        # drain forever.
        if self._prep_runner is not None:
            return campaign_store.has_claimable_preparing_campaigns(
                stale_after_minutes=self._config.prep_lease_minutes,
                db_path=self._db_path,
            )
        return False

    # -------------------------------------------------------------------------
    # Self-maintenance — OAuth token refresh for structured accounts
    # Runs on a slow cadence inside the tick loop so the standalone worker
    # does not depend on the Flask maintenance thread.
    # -------------------------------------------------------------------------

    def _is_account_stale(self, account: profile_registry.Account) -> bool:
        """Return True if the account's token is missing or expires within 5 min."""
        import sqlite3
        config = dict(account.config or {})
        platform = account.platform

        # Only structured (profile-based) OAuth accounts are refreshable
        if platform not in self._REFRESHABLE_PLATFORMS:
            return False

        # Managed TikTok uses the existing workspace agent, not a copied
        # native OAuth token. Optional backup leaves native refresh unchanged.
        if (platform == "tiktok"
                and str(config.get("publishingProvider") or "direct").strip().lower() == "sociamonials"
                and sociamonials_fallback.prefers_sociamonials(account, platform=platform)):
            return False

        # A cookie-based account has no refreshable OAuth credential. Compare
        # the normalized mode so "Cookie"/"API" casing and a platform key that
        # lives only on the row's ``auth_type`` column both resolve correctly.
        auth_type = profile_registry.effective_auth_type(
            config, account.auth_type, platform
        )
        if auth_type == "cookie":
            return False  # cookie-based accounts are not refreshable via API

        # Accounts flagged for manual reconnect are waiting on a human — never
        # auto-refresh them. This is what stops a dead credential from being
        # hammered hundreds of times a day. The flag is cleared automatically
        # on the next successful refresh (or when the operator reconnects).
        if config.get("_needsReconnect"):
            return False
        # Respect exponential back-off after consecutive refresh failures.
        if self._in_backoff(config):
            return False

        skew = 300
        if platform in self._LONG_LIVED_TOKEN_PLATFORMS:
            skew = max(skew, self._LONG_LIVED_REFRESH_MARGIN_SECONDS)

        if platform in {"facebook", "instagram"}:
            # Includes periodic READ validation for non-expiring tokens and
            # data-access deadlines; it does not force a token exchange.
            return token_maintenance.meta_maintenance_due(config, margin_seconds=skew)
        if platform == 'threads' and not token_maintenance.threads_refresh_allowed(config):
            return False

        access_token = str(config.get("accessToken") or "").strip()
        if not access_token:
            return True
        if platform == "twitter":
            # X access tokens live ~2h. Stored expiries were historically stamped
            # naive-local, which this helper interprets correctly (and treats an
            # unknown expiry as stale) so a proactive refresh always fires before
            # the token lapses.
            return prepared_publishers._x_access_token_stale(config, skew_seconds=skew)
        expires_at = self._parse_iso_datetime(str(config.get("accessTokenExpiresAt") or ""))
        if expires_at is None:
            # A refreshable token with no recorded expiry: refresh once to set
            # one. Long-lived platforms (threads) renew via the access token
            # itself; the rest only when we actually hold a refresh credential,
            # so genuinely non-refreshable tokens are left untouched.
            if platform in self._LONG_LIVED_TOKEN_PLATFORMS:
                return True
            return bool(str(config.get("refreshToken") or "").strip())
        return expires_at <= (self._utc_now() + timedelta(seconds=skew))

    def _in_backoff(self, config: dict) -> bool:
        """True while the account is inside its post-failure back-off window."""
        nxt = self._parse_iso_datetime(str(config.get("_nextMaintenanceAttemptAt") or ""))
        if nxt is None:
            return False
        return self._utc_now() < nxt

    @staticmethod
    def _utc_now() -> "datetime":
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _parse_iso_datetime(value: str):
        from datetime import datetime, timezone
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
        # Normalise to naive-UTC so comparisons against `_utc_now()` (also
        # naive-UTC) never mix offset-aware and offset-naive datetimes. Stored
        # expiry strings are written inconsistently across callbacks — some tz
        # aware ("+00:00"), some naive — so coerce here rather than at each site.
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    def _refresh_account(self, account: profile_registry.Account) -> None:
        """Refresh a stale account token using prepared_publishers helpers."""
        import sqlite3
        config = dict(account.config or {})
        platform = account.platform
        now = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(timespec="seconds")

        try:
            if platform == "tiktok":
                refresh_token = str(config.get("refreshToken") or "").strip()
                if not refresh_token:
                    return
                from myUtils import tiktok_auth
                token_payload = tiktok_auth.refresh_access_token(refresh_token=refresh_token)
                access_token = str(token_payload.get("access_token") or "")
                user_info = tiktok_auth.fetch_user_info(access_token=access_token) if access_token else {}
                # Apply via the same helper used in publish flow
                new_config = prepared_publishers._apply_tiktok_token_payload(config, token_payload, user_info)
                new_config.update({
                    "openId": token_payload.get("open_id") or config.get("openId") or "",
                    "scope": token_payload.get("scope") or config.get("scope") or "",
                    "displayName": user_info.get("data", {}).get("user", {}).get("display_name") or config.get("displayName") or "",
                    "avatarUrl": user_info.get("data", {}).get("user", {}).get("avatar_url") or config.get("avatarUrl") or "",
                    "lastAutoRefreshAt": now,
                })
                config = new_config

            elif platform == "reddit":
                if profile_registry.effective_auth_type(
                    config, account.auth_type, "reddit"
                ) == "cookie":
                    return
                refreshed = prepared_publishers.refresh_reddit_access_token(config)
                config.update({
                    "accessToken": refreshed["access_token"],
                    "scope": refreshed.get("scope", config.get("scope", "")),
                    "accessTokenUpdatedAt": now,
                    "lastAutoRefreshAt": now,
                })
                expires_in = refreshed.get("expires_in")
                if expires_in:
                    config["accessTokenExpiresAt"] = prepared_publishers._token_expiry_from_payload(
                        {"expires_in": expires_in}, "expires_in"
                    )

            elif platform == "youtube":
                refreshed = prepared_publishers.refresh_youtube_access_token(config)
                config.update({
                    "accessToken": refreshed["access_token"],
                    "accessTokenUpdatedAt": now,
                    "lastAutoRefreshAt": now,
                })
                expires_in = refreshed.get("expires_in")
                if expires_in:
                    config["accessTokenExpiresAt"] = prepared_publishers._token_expiry_from_payload(
                        {"expires_in": expires_in}, "expires_in"
                    )

            elif platform == "threads":
                config.update(token_maintenance.maintain_threads(config, mode='auto'))

            elif platform in {"facebook", "instagram"}:
                config.update(token_maintenance.maintain_meta(
                    config, platform, mode='auto', margin_seconds=self._LONG_LIVED_REFRESH_MARGIN_SECONDS))

            elif platform == "twitter":
                refresh_token = str(config.get("refreshToken") or "").strip()
                if not refresh_token or profile_registry.effective_auth_type(
                    config, account.auth_type, "twitter"
                ) != "api":
                    return

                def _persist_twitter(updated_config: dict) -> None:
                    token_state.require_saved(token_state.commit_success(
                        account, token_state.credential_changes(account.config or {}, updated_config),
                        db_path=self._db_path))

                # Single-flight: the rotated refresh token is persisted inside
                # the lock before another maintenance pass can read the old one.
                updated, refreshed = prepared_publishers.refresh_twitter_token_single_flight(
                    config,
                    account_id=account.id,
                    db_path=self._db_path,
                    persist=_persist_twitter,
                    extra_fields={"lastAutoRefreshAt": now},
                    clear_markers=True,
                )
                if refreshed:
                    _logger.info(
                        f"worker self-maintenance: refreshed twitter account id={account.id}"
                    )
                return

            else:
                return  # unknown platform

            # Cross-process CAS: a reconnect (even with the same token) wins
            # over stale maintenance; route and operator fields merge intact.
            updated = token_state.commit_success(
                account, token_state.credential_changes(account.config or {}, config), db_path=self._db_path)
            if updated is not None:
                _logger.info(f"worker self-maintenance: validated {platform} account id={account.id}")

        except Exception as exc:
            self._handle_refresh_failure(account, config, exc)

    def _handle_refresh_failure(
        self, account: profile_registry.Account, config: dict, exc: Exception
    ) -> None:
        """Only a structured terminal rejection requires reconnect.

        Timeouts/429/5xx/unknown errors back off indefinitely without changing
        scheduled expiry. A failure of an old credential generation is ignored.
        """
        try:
            alert_state = {}
            updated = token_state.commit_failure(
                account, exc, db_path=self._db_path,
                self_check=account.platform in {'facebook', 'instagram'},
                backoff_base=self._MAINTENANCE_BACKOFF_BASE_SECONDS,
                backoff_cap=self._MAINTENANCE_BACKOFF_CAP_SECONDS,
                alert_state=alert_state,
            )
        except Exception as persist_exc:
            _logger.error(f"worker self-maintenance: could not persist failure for account id={account.id}: {token_state.safe_error(persist_exc)}")
            return
        if updated is None:
            return
        merged = updated.config or {}
        failures = int(merged.get('_maintenanceFailures') or 0)
        reason = token_state.safe_error(exc)
        _logger.warning(f"worker self-maintenance: {account.platform} account id={account.id}, attempt {failures}: {reason}")
        if alert_state.get('needed'):
            self._alert_needs_reconnect(account, failures, token_state.CredentialError(reason))

    def _alert_needs_reconnect(
        self, account: profile_registry.Account, failures: int, exc: Exception
    ) -> None:
        """Best-effort operator alert; never raises into the maintenance loop."""
        try:
            from myUtils import ops_alerts
            account_label = str(account.nickname or account.account_name or "unknown")
            ops_alerts.send_ops_alert(
                subject=f"[SAU] {account.platform} {account_label} needs reconnect",
                body=(
                    f"Account {account_label} (account:{account.id}, {account.platform}) "
                    f"failed automatic token "
                    f"refresh {failures} times in a row and has been flagged for "
                    f"manual reconnect. Re-authorise it via the Connect button.\n\n"
                    f"Last error: {str(exc)[:500]}"
                ),
            )
        except Exception as alert_exc:  # noqa: BLE001 — alerting must never break maintenance
            _logger.warning(f"worker self-maintenance: ops alert failed: {alert_exc!r}")

    async def _run_maintenance_tick(self) -> None:
        """Background scan-and-refresh for stale OAuth accounts."""
        try:
            from myUtils import publish_alert_outbox
            await asyncio.to_thread(publish_alert_outbox.flush_due, db_path=self._db_path)
        except Exception as exc:
            _logger.warning(f"worker notification retry failed: {_scrub_secrets(str(exc))}")
        try:
            accounts = await asyncio.to_thread(profile_registry.list_accounts, enabled=True, db_path=self._db_path)
        except Exception as exc:
            _logger.warning(f"worker self-maintenance: could not list accounts: {_scrub_secrets(str(exc))}")
            return

        stale = []
        for account in accounts:
            try:
                if self._is_account_stale(account):
                    stale.append(account)
            except Exception as exc:  # noqa: BLE001 — one bad config must not
                # strand every other account's refresh. Log and skip it.
                _logger.warning(
                    f"worker self-maintenance: staleness check failed for "
                    f"account {getattr(account, 'id', '?')} "
                    f"({getattr(account, 'platform', '?')}): {exc}"
                )
        if not stale:
            return

        _logger.info(f"worker self-maintenance: refreshing {len(stale)} stale accounts")
        for account in stale:
            if self._stop.is_set():
                break
            # Run refresh synchronously in a thread pool so it doesn't block the async loop
            import concurrent.futures
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._refresh_account, account)

    async def _tick(self) -> None:
        was_ready = self._dispatch_ready
        try:
            await self._dispatch_tick()
        except BaseException:
            self._dispatch_ready = False
            try:
                self._heartbeat(force=True)
            except Exception:
                _logger.error("Worker could not record failed dispatch readiness")
            raise
        self._dispatch_ready = True
        self._heartbeat(force=not was_ready)

    async def _dispatch_tick(self) -> None:
        # Reap finished tasks first so their account_refs free up.
        done = {task for task in self._tasks if task.done()}
        for task in done:
            self._tasks.discard(task)
            # Surface unhandled task errors in the log; the target itself has
            # already been transitioned by the task body.
            exc = None if task.cancelled() else task.exception()
            if exc is not None:
                _logger.error(f"worker task crashed: {exc!r}")

        # Reap finished prep tasks the same way. ``_run_campaign_prep`` owns its
        # own campaign transition, so here we only surface a crash.
        done_preps = {task for task in self._prep_tasks if task.done()}
        for task in done_preps:
            self._prep_tasks.discard(task)
            exc = None if task.cancelled() else task.exception()
            if exc is not None:
                _logger.error(f"campaign prep task crashed: {exc!r}")

        # Self-maintenance: scan and refresh stale OAuth accounts on a slow cadence
        # (~every _MAINTENANCE_TICK_INTERVAL ticks = 5 min at 1s poll interval).
        self._maintenance_counter += 1
        if self._maintenance_counter >= self._MAINTENANCE_TICK_INTERVAL:
            self._maintenance_counter = 0
            if not self._stop.is_set() and (
                self._maintenance_task is None or self._maintenance_task.done()
            ):
                self._maintenance_task = asyncio.create_task(self._run_maintenance_tick())

        # Recover targets a dead predecessor left stuck in `running`. Without
        # this, a Gunicorn/container restart mid-upload strands the row
        # forever: the job never finalises and its media stays pinned local
        # by offload_to_drive.sh's in-flight exclusion.
        self._stale_sweep_counter += 1
        if self._stale_sweep_counter >= self._STALE_SWEEP_TICK_INTERVAL:
            self._stale_sweep_counter = 0
            try:
                # Recover targets abandoned by a dead/restarted worker. The
                # executor enforces its own TARGET_EXECUTION_TIMEOUT_SECONDS
                # (20 min default), so a row still `running` well past that is
                # definitionally orphaned - the previous 120 min ceiling meant a
                # restart left rows stuck for two hours before anything looked
                # at them. Sweep at 1.5x the executor timeout so a legitimately
                # slow publish is never cut off.
                requeued = jobs.requeue_stale_running(
                    older_than_minutes=max(
                        1, int(TARGET_EXECUTION_TIMEOUT_SECONDS // 60 * 1.5)
                    ),
                    max_attempts=self._config.retry.max_attempts,
                    db_path=self._db_path,
                )
                if requeued:
                    _logger.warning(
                        f"worker requeued {requeued} stale running target(s)"
                    )
            except Exception:
                _logger.exception("stale-running sweep failed")

        if self._stop.is_set() or runtime.claims_paused(self._db_path):
            return

        # Prep queue: claim campaigns still in ``preparing`` and run their
        # artifact prep + draft/enqueue off the request path.
        if self._prep_runner is not None:
            await self._prep_tick()

        in_flight = self._concurrency.in_flight_accounts()
        slots_free = self._config.max_concurrent - len(self._tasks)
        if slots_free <= 0:
            return

        claimed = jobs.claim_next_targets(
            limit=min(self._config.batch_size, slots_free),
            excluded_accounts=in_flight,
            db_path=self._db_path,
            owner=self._prep_owner,
            lease_seconds=180,
            prepare_ahead_seconds=self._config.preparation_warm_seconds,
        )
        for target in claimed:
            self._claimed[target.id] = target
            task = asyncio.create_task(self._run_target(target))
            self._tasks.add(task)
            self._task_by_target[target.id] = task

    async def _prep_tick(self) -> None:
        """Claim and prepare campaigns left in ``preparing``.

        Kept separate from target claiming: prep is ffmpeg/LLM-heavy, so it is
        bounded by its own ``prep_max_concurrent`` rather than the browser
        concurrency budget. The stale-lease sweep runs on the same cadence as
        the target sweep and is what stops a crashed prep from hanging forever.
        """
        self._prep_sweep_counter += 1
        if self._prep_sweep_counter >= self._STALE_SWEEP_TICK_INTERVAL:
            self._prep_sweep_counter = 0
            try:
                recovered = campaign_store.requeue_stale_preparing(
                    older_than_minutes=self._config.prep_lease_minutes,
                    max_attempts=self._config.retry.max_attempts,
                    db_path=self._db_path,
                )
                if recovered:
                    _logger.warning(
                        f"worker recovered {recovered} stale preparing campaign(s)"
                    )
            except Exception:
                _logger.exception("stale-preparing sweep failed")

        free = self._config.prep_max_concurrent - len(self._prep_tasks)
        if free <= 0:
            return
        for _ in range(min(self._config.prep_batch_size, free)):
            campaign = campaign_store.claim_next_preparing_campaign(
                owner=self._prep_owner,
                stale_after_minutes=self._config.prep_lease_minutes,
                db_path=self._db_path,
            )
            if campaign is None:
                break
            self._prep_tasks.add(
                asyncio.create_task(self._run_campaign_prep(campaign))
            )

    async def _run_campaign_prep(
        self, campaign: "campaign_store.Campaign"
    ) -> None:
        """Share the small preparation cap with prospective raw target children."""
        async with self._preparation_slots:
            await self._run_campaign_prep_inline(campaign)

    async def _run_campaign_prep_inline(self, campaign: "campaign_store.Campaign") -> None:
        """Run one claimed campaign's prep + finalize, then record the outcome."""
        if (os.environ.get("SAU_TARGET_CHILD") != "1" and os.environ.get("SAU_TESTING") != "1"
                and getattr(self._prep_runner, "__module__", "") == "myUtils.campaign_prep"):
            from myUtils.target_runner import run_isolated_prep
            await run_isolated_prep(self, campaign,
                timeout=float(os.environ.get("SAU_PREP_TIMEOUT_SECONDS", "3600")))
            return
        _logger.info(
            f"campaign {campaign.id} claimed for prep (owner={self._prep_owner})"
        )
        try:
            # ffmpeg is blocking; keep it off the event loop. A runner may also
            # be an async callable, in which case to_thread returns the
            # coroutine and we await it here.
            with _prep_lease_heartbeat(
                campaign.id,
                self._prep_owner,
                lease_minutes=self._config.prep_lease_minutes,
                db_path=self._db_path,
            ):
                result = await asyncio.to_thread(
                    self._prep_runner, campaign, self._db_path
                )
                if inspect.isawaitable(result):
                    result = await result
            result = result or {}
            jobs = result.get("jobs") or []
            now = datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(
                timespec="seconds"
            )
            # A prep that queued nothing must not regress a campaign that already
            # has queued posts: leaving it 'publishing' is the truth, while
            # 'needs_review' + "No publishable posts queued" tells the operator to
            # re-submit something that is already scheduled (campaign 2599).
            already_queued = False
            if not jobs:
                try:
                    already_queued = campaign_store.campaign_has_queued_posts(
                        campaign.id, db_path=self._db_path
                    )
                except Exception:  # noqa: BLE001 - status must never break prep
                    already_queued = False
            succeeded = bool(jobs) or already_queued
            transitioned = campaign_store.finish_campaign_prep(
                campaign.id,
                owner=self._prep_owner,
                status=(
                    campaign_store.CAMPAIGN_PUBLISHING
                    if succeeded
                    else campaign_store.CAMPAIGN_NEEDS_REVIEW
                ),
                prepared_at=now,
                published_at=None,
                last_error=(
                    None
                    if succeeded
                    else "No publishable posts queued"
                ),
                db_path=self._db_path,
            )
            if transitioned:
                _logger.info(
                    f"campaign {campaign.id} prepared: {len(jobs)} job(s) queued"
                )
            else:
                _logger.warning(
                    f"campaign {campaign.id} prep finished but its lease was "
                    "lost to another worker; result discarded"
                )
        except Exception as exc:  # noqa: BLE001
            _logger.exception(f"campaign {campaign.id} prep failed")
            # A prep that fails must not regress a campaign that already has
            # queued posts either. Campaigns 2129/2132/2133/2134/2171/2215-2218/
            # 2411 are legacy (no persisted prepRequest); a worker claimed them,
            # raised ValueError, and this handler flipped them to needs_review
            # while their posts were already queued. The success path above
            # already guards this; the failure path must use the same truth.
            already_queued = False
            try:
                already_queued = campaign_store.campaign_has_queued_posts(
                    campaign.id, db_path=self._db_path
                )
            except Exception:  # noqa: BLE001 - status must never break prep
                already_queued = False
            try:
                campaign_store.finish_campaign_prep(
                    campaign.id,
                    owner=self._prep_owner,
                    status=(
                        campaign_store.CAMPAIGN_PUBLISHING
                        if already_queued
                        else campaign_store.CAMPAIGN_NEEDS_REVIEW
                    ),
                    last_error=(
                        None if already_queued else f"prep failed: {exc}"
                    ),
                    db_path=self._db_path,
                )
            except Exception:
                _logger.exception(
                    f"campaign {campaign.id} could not record its prep failure"
                )

    def _transition(self, target, transition, *args, **kwargs) -> bool:
        if transition in (jobs.mark_target_failed, jobs.mark_target_blocked, jobs.mark_target_needs_review):
            kwargs.setdefault("alert_max_attempts", self._config.retry.max_attempts)
        return transition(target.id, *args, lease_token=getattr(target, "lease_token", None),
                          db_path=self._db_path, **kwargs)

    def _target_payload(self, target, job) -> dict:
        payload = copy.deepcopy(job.payload or {})
        checkpoint = copy.deepcopy(getattr(target, "checkpoint", None) or {})
        delivery.publish_routes({"_delivery": checkpoint})
        payload.update(checkpoint.get("payload") or {})
        payload.update(_db_path=str(self._db_path), _telegramDeliveryKey=str(target.id),
                       _delivery=checkpoint)

        def persist(state):
            if not jobs.save_target_checkpoint(target.id, state,
                    lease_token=getattr(target, "lease_token", None), db_path=self._db_path):
                raise delivery.LeaseLost("Target ownership was lost; publishing stopped")

        payload["_save_checkpoint"] = persist
        return payload

    async def _run_target(self, target: jobs.Target) -> None:
        try:
            prospective = (target.checkpoint.get("admission") or {}).get("mode") == jobs.PROSPECTIVE_ADMISSION_MODE
            async with (self._preparation_slots if prospective else contextlib.nullcontext()):
                if (self._executor is default_executor and os.environ.get("SAU_TARGET_CHILD") != "1"
                        and os.environ.get("SAU_TESTING") != "1"):
                    from myUtils.target_runner import run_isolated_target
                    await run_isolated_target(self, target, timeout=TARGET_EXECUTION_TIMEOUT_SECONDS)
                else:
                    await self._run_target_inline(target)
        finally:
            self._claimed.pop(target.id, None)
            self._task_by_target.pop(target.id, None)
            self._payloads.pop(target.id, None)
            self._maybe_close_job_sink(target.job_id)

    async def _reconcile_target(self, target, job, payload, log) -> None:
        """Resume only the receipt's provider; no restoration/readiness/create budget."""
        receipt = delivery.state(payload).get("provider_receipt") or {}
        if receipt.get("status") == "failed":
            raise delivery.ProviderConfirmedFailure(receipt.get("provider"), receipt.get("id"), "Retained provider receipt failed; reconciliation cannot resubmit it")
        if receipt.get("status") == "delivered":
            if delivery.state(payload).get("x_browser_operations") and delivery.state(payload).get("stage") != "browser_delivered":
                raise delivery.DeliveryUnknown("X browser thread still has retained incomplete parts")
            self._transition(target, jobs.mark_target_success)
            return
        async with self._concurrency.slot(target.account_ref):
            if receipt.get("provider") == "sociamonials":
                if not await self._try_sociamonials_fallback(target, "Resume original provider receipt", log):
                    raise delivery.DeliveryUnknown("Original receipt provider is unavailable for reconciliation")
                return
            result = self._executor(job.platform, payload, target)
            outcome = await result if inspect.isawaitable(result) else result
            if isinstance(outcome, (PreparationCompleted, PreparationDeferred)):
                raise delivery.DeliveryUnknown("Receipt polling returned preparation instead of delivery proof")
            if delivery.state(payload).get("provider_receipt", {}).get("status") != "delivered":
                delivery.pending_or_unknown(payload, "Original provider delivery remains unconfirmed")
            delivery.route_outcome(payload, "native", "delivered")
            self._transition(target, jobs.mark_target_success)

    async def _prepare_before_dispatch(self, target, job, payload, *, backup=False):
        from myUtils import media_restore, publish_admission
        current = delivery.state(payload)
        if "provider_budget_baseline" not in current:
            delivery.checkpoint(payload, provider_budget_baseline=0 if current.get("recovery_generation") or current.get("preparation_generation") else max(0, target.attempts - 1))
        try:
            return await asyncio.to_thread(_prepare_target_prerequisites, target, job, payload, db_path=self._db_path, backup=backup)
        except (delivery.LeaseLost, delivery.DeliveryUnknown, media_restore.PrerequisiteUnavailable, MediaRestoreError):
            raise
        except publish_admission.AdmissionError as exc:
            raise MediaRestoreError(str(exc), retryable=False, category=(exc.details or {}).get("reasonCode", "intent")) from exc
        except ValueError as exc:
            raise MediaRestoreError(str(exc), retryable=False, category="explicit_recovery_required") from exc
        except (OSError, TimeoutError) as exc:
            raise media_restore.PrerequisiteUnavailable(media_restore.classify_failure(exc)) from None

    async def _run_target_inline(self, target: jobs.Target) -> None:
        log = bind_job_logger(job_id=target.job_id, target_id=target.id,
                              platform="unknown", account_ref=target.account_ref,
                              attempt=target.attempts)
        payload = {}
        try:
            job = jobs.get_job(target.job_id, db_path=self._db_path)
            payload = self._target_payload(target, job)
            self._payloads[target.id] = payload
            disabled = str(os.environ.get(f"SAU_{job.platform.upper()}_ENABLED", "")).lower().strip()
            if disabled in {"0", "false", "no", "off"}:
                reason = f"{job.platform} publishing is disabled by the operator"
                if self._transition(target, jobs.mark_target_blocked, reason):
                    self._alert_publish_failure(target, reason)
                return
            receipt = delivery.state(payload).get("provider_receipt") or {}
            payload["_publish_max_attempts"] = self._config.retry.max_attempts
            if receipt:
                await self._reconcile_target(target, job, payload, log)
                return
            # Receipts own their provider. A pinned backup without a receipt
            # resumes the SAME key and budget, never native publishing again.
            fallback_only = receipt.get("provider") == "sociamonials"
            if not receipt:
                fallback_only = delivery.active_route(payload) == "sociamonials"
                fallback_only = fallback_only or (job.platform == "twitter" and not _x_direct_publish_enabled())
                if not fallback_only and target.account_ref.startswith("account:"):
                    route_account = pre_flight._resolve_account(target.account_ref, self._db_path)
                    fallback_only = sociamonials_fallback.prefers_sociamonials(route_account, platform=job.platform)
            check = pre_flight.run_pre_flight(
                account_ref=target.account_ref, job_id=job.id, platform=job.platform,
                payload=payload, db_path=self._db_path, validate_credentials=not fallback_only)
            credential_only = (check.checks.get("needs_reconnect") is False
                               or check.checks.get("token_valid") is False
                               or check.checks.get("native_route_available") is False)
            if not check.ok and credential_only and not receipt and sociamonials_fallback.is_enabled():
                route_account = pre_flight._resolve_account(target.account_ref, self._db_path)
                settings = profile_registry.get_profile(job.profile_id, db_path=self._db_path).settings if job.profile_id else None
                mapping = sociamonials_fallback.resolve_mapping(
                    getattr(route_account, "id", None), account=route_account, platform=job.platform, settings=settings)
                if mapping and mapping.get("network") == sociamonials_fallback._platform_network(job.platform):
                    # Skip ONLY native credentials, then repeat every campaign,
                    # destination, ownership, language and content-policy guard.
                    delivery.route_outcome(payload, "native", "credential_unavailable", check.reason)
                    check = pre_flight.run_pre_flight(
                        account_ref=target.account_ref, job_id=job.id, platform=job.platform,
                        payload=payload, db_path=self._db_path, validate_credentials=False)
                    fallback_only = True
            if not check.ok:
                transition = jobs.mark_target_failed if check.category == "permanent" else jobs.mark_target_blocked
                if self._transition(target, transition, check.reason):
                    log.warning(f"pre-flight blocked target: {check.reason}")
                    self._alert_publish_failure(target, check.reason)
                return
            account = _resolve_structured_account(target.account_ref, db_path=self._db_path)
            log = bind_job_logger(job_id=job.id, target_id=target.id, platform=job.platform,
                                  account_ref=target.account_ref, attempt=target.attempts,
                                  account_name=(account.nickname or account.account_name) if account else None)
            if account is not None:
                routing_error = (_account_profile_mismatch(target, account, job)
                                 or _account_unpublishable(account)
                                 or _language_routing_error(target, account, db_path=self._db_path))
                if routing_error:
                    if self._transition(target, jobs.mark_target_blocked, routing_error):
                        self._alert_publish_failure(target, routing_error)
                    return
            async with self._concurrency.slot(target.account_ref):
                for artifact in payload.get("artifacts") or []:
                    if isinstance(artifact, dict) and artifact.get("public_url"):
                        artifact["public_url"] = _normalise_artifact_url(str(artifact["public_url"]))
                if payload.get("artifacts"):
                    _backfill_artifact_public_urls(payload["artifacts"], self._db_path)
                guard_error = _content_guard_error(payload, target, self._db_path)
                if guard_error:
                    reason = f"[content-guard] {guard_error}"
                    if self._transition(target, jobs.mark_target_failed, reason):
                        self._alert_publish_failure(target, reason)
                    return
                if self._executor is default_executor or delivery.state(payload).get("admission") or payload.get("artifacts") or payload.get("thumbnail"):
                    preparation = await self._prepare_before_dispatch(target, job, payload, backup=fallback_only)
                    if isinstance(preparation, (PreparationCompleted, PreparationDeferred)):
                        return
                if fallback_only:
                    if await self._try_sociamonials_fallback(target, "Configured Sociamonials route", log):
                        return
                    raise sociamonials_fallback.SociamonialsNotConfigured("Configured Sociamonials route is unavailable")
                if job.platform == "reddit" and not _reddit_publishing_enabled():
                    self._transition(target, jobs.mark_target_blocked, "Reddit publishing is disabled by the operator")
                    return
                if job.platform == "reddit" and not _reddit_direct_publish_enabled():
                    if await self._try_bundle_social_fallback(target, "Configured bundle.social route", log):
                        return
                    raise _fc.ClassifiedFailure("bundle.social route is unavailable", _fc.PERMANENT)
                cookie_x = job.platform == "twitter" and (profile_registry.effective_auth_type(
                    (account.config or {}) if account else {}, getattr(account, "auth_type", None), "twitter") or "cookie") == "cookie"
                payload["_defer_native_create_budget"] = bool(self._executor is default_executor and cookie_x and _x_browser_adapted())
                if not payload["_defer_native_create_budget"]:
                    _begin_native_publish_attempt(payload, target)
                log.info("target claimed; starting execution")
                result = self._executor(job.platform, payload, target)
                if inspect.isawaitable(result):
                    outcome = await result if self._executor is default_executor else await asyncio.wait_for(
                        result, timeout=TARGET_EXECUTION_TIMEOUT_SECONDS)
                else:
                    outcome = result
                if isinstance(outcome, (PreparationCompleted, PreparationDeferred)):
                    # An owned preparation-only executor has released back to
                    # PENDING/retrying; a normal return is NOT delivery success.
                    if isinstance(outcome, PreparationDeferred) and jobs.get_target(target.id, db_path=self._db_path).status == jobs.TARGET_RUNNING:
                        jobs.defer_target_prerequisite(target.id, outcome.reason, lease_token=target.lease_token,
                            category=outcome.category, delay_seconds=outcome.retry_after_seconds, db_path=self._db_path)
                    return
                delivery.save_payload_progress(payload)
                if isinstance(outcome, dict) and outcome.get("retryableFailure"):
                    raise prepared_publishers.PreparedPublishError(
                        str(outcome.get("error") or "Partial delivery"),
                        details=outcome.get("details"), retryable=True)
                delivery.route_outcome(payload, "native", "delivered")
                if self._transition(target, jobs.mark_target_success):
                    log.success("target succeeded")
        except delivery.LeaseLost:
            log.warning("Target lease lost; obsolete completion discarded")
        except asyncio.CancelledError:
            current = delivery.state(payload)
            if delivery.has_unresolved_submission(payload):
                if self._transition(target, jobs.mark_target_needs_review,
                                 "Execution interrupted after submission began; verify delivery before retrying"):
                    self._alert_publish_failure(target, "Execution interrupted; delivery requires review")
            else:
                self._transition(target, jobs.mark_target_retry, "Cancelled before submission", delay_seconds=5)
            raise
        except Exception as exc:
            if payload:
                try:
                    delivery.save_payload_progress(payload)
                except delivery.LeaseLost:
                    return
            await self._handle_failure(target, exc, log)

    async def _handle_failure(self, target: jobs.Target, exc: BaseException, log,
                              *, allow_fallback: bool = True) -> None:
        message = _scrub_secrets(f"{type(exc).__name__}: {exc}")
        error_details = getattr(exc, "details", None)
        if isinstance(error_details, dict) and error_details:
            message += " | details=" + _scrub_secrets(json.dumps(error_details, ensure_ascii=False, default=str))
        payload = self._payloads.get(target.id, {})
        checkpoint = delivery.state(payload) or getattr(target, "checkpoint", None) or {}
        try:
            route = delivery.active_route(payload or {"_delivery": checkpoint})
        except delivery.DeliveryUnknown:
            if self._transition(target, jobs.mark_target_needs_review, message):
                self._alert_publish_failure(target, message)
            return
        if isinstance(exc, delivery.DeliveryPending):
            delivery.route_outcome(payload, route, "awaiting_delivery", message)
            self._transition(target, jobs.mark_target_awaiting_delivery, delivery.state(payload) or checkpoint,
                             delay_seconds=max(30, _retry_after_seconds(exc)))
            log.info("Provider accepted the post; awaiting confirmed delivery")
            return
        if isinstance(exc, delivery.DeliveryUnknown):
            delivery.route_outcome(payload, route, "needs_review", message)
            if self._transition(target, jobs.mark_target_needs_review, message):
                self._alert_publish_failure(target, message)
            return
        safe_prerequisite = isinstance(error_details, dict) and error_details.get("prerequisiteFailure") is True
        preview_only = (payload.get("_defer_native_create_budget") and not payload.get("_native_route_started")
                        and _ambiguous_transport_error(exc))
        if (safe_prerequisite or preview_only) and not delivery.has_unresolved_submission({"_delivery": checkpoint}):
            category = error_details.get("dependencyCategory", "transport") if safe_prerequisite else "browser_preparing"
            cached_wait = safe_prerequisite and error_details.get("cachedDependencyWait") is True
            delay = max(1, getattr(exc, "retry_after_seconds", 120), _retry_after_seconds(exc))
            attempts = (checkpoint.get("prerequisites") or {}).get("attempts", 0)
            if not cached_wait and type(attempts) is int and attempts >= 0:
                delay = max(delay, min(1800, 120 * 2 ** min(attempts, 4)))
            changed = jobs.defer_target_prerequisite(target.id, message, lease_token=target.lease_token,
                category=category, delay_seconds=delay, retryable=getattr(exc, "retryable", True),
                max_attempts=self._config.prerequisite_max_attempts, db_path=self._db_path, cached_wait=cached_wait,
                stage=error_details.get("stage", "waiting_dependency") if safe_prerequisite else "browser_preparing")
            if changed:
                log.warning(f"Prerequisite deferred/stopped before provider create: {category}")
            return
        if (isinstance(error_details, dict) and error_details.get("intentReviewRequired") is True
                and not delivery.has_unresolved_submission({"_delivery": checkpoint})):
            delivery.route_outcome(payload, route, "blocked_intent", message)
            if self._transition(target, jobs.mark_target_blocked, message):
                log.warning("Original intent needs review; no provider post was created")
                self._alert_publish_failure(target, message)
            return
        if (isinstance(exc, sociamonials_fallback.SociamonialsNotConfigured)
                and not delivery.has_unresolved_submission({"_delivery": checkpoint})):
            delivery.route_outcome(payload, "sociamonials", "unavailable", message)
            native = delivery.publish_routes(payload).get("native") or {}
            transition = jobs.mark_target_failed if native.get("outcome") == "failed" else jobs.mark_target_blocked
            if self._transition(target, transition, message):
                self._alert_publish_failure(target, message)
            return
        if isinstance(exc, delivery.ProviderConfirmedFailure):
            receipt = checkpoint.get("provider_receipt") or {}
            proof = exc.details
            matches = (isinstance(receipt, dict) and receipt.get("provider") == proof["provider"]
                       and receipt.get("id") == proof["receiptId"] and receipt.get("status") == "failed"
                       and receipt.get("terminal_failure") is True)
            if matches:
                delivery.route_outcome(payload, route, "failed", message)
                if route == "native":
                    delivery.route_outcome(payload, "sociamonials", "not_safe_or_not_applicable",
                                           "Retained native receipt requires explicit reconciliation; no second post")
                if self._transition(target, jobs.mark_target_failed, message):
                    self._alert_publish_failure(target, message)
            elif self._transition(target, jobs.mark_target_needs_review,
                                  "Terminal provider failure does not match its stored receipt; reconcile before retrying"):
                self._alert_publish_failure(target, message)
            return
        definite = _fc.proven_non_delivery(exc)
        from myUtils import generic_delivery
        if route == "native" and definite and payload:
            job = jobs.get_job(target.job_id, db_path=self._db_path)
            if job.platform == "twitter":
                generic_delivery.authorize_media_only_handoff(payload, exc, target_id=target.id)
                checkpoint = delivery.state(payload)
        media_reconciled = generic_delivery.media_only_reconciled(checkpoint, target_id=target.id)
        recorded_receipt = (checkpoint.get("provider_receipt") or checkpoint.get("completed_operations")
                            or checkpoint.get("provider_operations") and not media_reconciled
                            or checkpoint.get("telegramCompletedByDelivery")
                            or generic_delivery.x_browser_has_effects(checkpoint))
        if recorded_receipt or (delivery.has_unresolved_submission({"_delivery": checkpoint}) and not definite):
            try:
                delivery.pending_or_unknown({"_delivery": checkpoint}, message)
            except delivery.DeliveryPending:
                delivery.route_outcome(payload, route, "awaiting_delivery", message)
                self._transition(target, jobs.mark_target_awaiting_delivery, delivery.state(payload) or checkpoint,
                                 delay_seconds=max(30, _retry_after_seconds(exc)))
            except delivery.DeliveryUnknown:
                delivery.route_outcome(payload, route, "needs_review", message)
                if self._transition(target, jobs.mark_target_needs_review, message):
                    self._alert_publish_failure(target, message)
            return
        category = _fc.classify_failure(exc, message=message).category
        routes = delivery.publish_routes(payload) if payload else checkpoint.get("publish_routes") or {}
        detail = routes.get(route) or {}
        attempts = detail.get("attempts", target.attempts)
        if type(attempts) is not int or attempts < 0:
            if self._transition(target, jobs.mark_target_needs_review, "Invalid persisted attempt budget"):
                self._alert_publish_failure(target, "Invalid persisted attempt budget")
            return
        stored_budget = detail.get("max_attempts", self._config.retry.max_attempts)
        if type(stored_budget) is not int or stored_budget < 1:
            if self._transition(target, jobs.mark_target_needs_review, "Invalid persisted provider attempt budget"):
                self._alert_publish_failure(target, "Invalid persisted provider attempt budget")
            return
        exhausted = attempts >= min(self._config.retry.max_attempts, stored_budget) or isinstance(exc, delivery.RouteExhausted)
        if definite and payload:
            delivery.record_non_delivery(payload, provider=route, reason=message)
        delivery.route_outcome(payload, route, "failed" if exhausted or category is _fc.PERMANENT else "retrying", message)
        can_backup = (route == "native" and allow_fallback and (category is _fc.PERMANENT or exhausted)
                      and _fc.backup_eligible(exc, checkpoint=checkpoint)
                      and not payload.get("_fallback_attempted"))
        if can_backup:
            reset_delay = _retry_after_seconds(exc)
            if reset_delay > 0 and sociamonials_fallback.is_enabled():
                job = jobs.get_job(target.job_id, db_path=self._db_path)
                route_account = pre_flight._resolve_account(target.account_ref, self._db_path)
                settings = profile_registry.get_profile(job.profile_id, db_path=self._db_path).settings if job.profile_id else None
                mapping = sociamonials_fallback.resolve_mapping(
                    getattr(route_account, "id", None), account=route_account, platform=job.platform, settings=settings)
                if mapping and mapping.get("network") == sociamonials_fallback._platform_network(job.platform):
                    delivery.select_backup(payload)
                    delivery.route_outcome(payload, "sociamonials", "waiting_for_reset", message)
                    self._transition(target, jobs.mark_target_retry, message,
                                     delay_seconds=reset_delay, error_category=category.value)
                    log.warning(f"Backup pinned; provider/account reset retained for {reset_delay:.1f}s")
                    return
            try:
                if await self._try_sociamonials_fallback(target, message, log):
                    return
            except delivery.LeaseLost:
                return
            except Exception as fallback_error:
                await self._handle_failure(target, fallback_error, log, allow_fallback=False)
                return
        if category is _fc.PERMANENT or exhausted:
            if route == "native" and not delivery.publish_routes(payload).get("sociamonials"):
                delivery.route_outcome(payload, "sociamonials", "unavailable" if can_backup else "not_safe_or_not_applicable")
            if self._transition(target, jobs.mark_target_failed, message):
                log.error(f"publishing routes stopped: {message}")
                self._alert_publish_failure(target, message)
            return
        delay = self._config.retry.backoff_for(attempts)
        if category is _fc.RECOVERABLE:
            delay = min(self._config.retry.max_backoff_seconds, delay * 2)
        delay = max(delay, _retry_after_seconds(exc))
        self._transition(target, jobs.mark_target_retry, message,
                         delay_seconds=delay, error_category=category.value)
        log.warning(f"{route} will retry in {delay:.1f}s: {message}")

    async def _try_sociamonials_fallback(
        self, target: jobs.Target, message: str, log, *, only_platform: str | None = None
    ) -> bool:
        """Attempt the configured route; only confirmed delivery is success.

        Errors propagate to the finite retry handler. A saved receipt resumes
        status polling, bypassing upload and create-post entirely.
        """
        if not sociamonials_fallback.is_enabled():
            return False
        job = jobs.get_job(target.job_id, db_path=self._db_path)
        if (only_platform and job.platform != only_platform) or not sociamonials_fallback._platform_network(job.platform):
            return False
        try:
            account = _resolve_structured_account(target.account_ref, db_path=self._db_path)
        except LookupError:
            return False
        if account is None:
            return False
        payload = self._payloads.get(target.id)
        if payload is None:
            payload = self._target_payload(target, job)
            self._payloads[target.id] = payload
        receipt = delivery.state(payload).get("provider_receipt") or {}
        if receipt and receipt.get("provider") != "sociamonials":
            raise delivery.DeliveryUnknown("A native receipt exists; reconcile it before using another provider")
        payload["_fallback_attempted"] = True
        delivery.select_backup(payload)
        settings = profile_registry.get_profile(job.profile_id, db_path=self._db_path).settings if job.profile_id else None
        if not receipt:
            if not payload.get("_prerequisites_checked") or str((account.config or {}).get("publishingProvider") or "direct") != "sociamonials":
                prepared = await self._prepare_before_dispatch(target, job, payload, backup=True)
                if isinstance(prepared, (PreparationCompleted, PreparationDeferred)):
                    return True  # Released for preparation, NOT delivery success.
            delivery.begin_route_attempt(payload, "sociamonials", max_attempts=self._config.retry.max_attempts)
        for artifact in payload.get("artifacts") or []:
            if isinstance(artifact, dict) and artifact.get("public_url"):
                artifact["public_url"] = _normalise_artifact_url(str(artifact["public_url"]))
        try:
            result = await asyncio.to_thread(
                sociamonials_fallback.publish_via_sociamonials,
                platform=job.platform, account=account, payload=payload,
                media_paths=_fallback_media_paths(payload), target_id=target.id,
                settings=settings, idempotency_suffix=None,
            )
        except sociamonials_fallback.SociamonialsNotConfigured:
            delivery.route_outcome(payload, "sociamonials", "unavailable", "Verified backup is unavailable")
            raise
        prior = delivery.receipt_for(payload, "sociamonials") or {}
        post_id = result.get("post_id") or prior.get("id")
        if isinstance(post_id, bool) or not isinstance(post_id, (str, int)) or not str(post_id).strip():
            raise delivery.DeliveryUnknown("Sociamonials returned no usable post receipt; acceptance is unknown")
        if post_id and delivery.receipt_for(payload, "sociamonials") is None:
            delivery.accepted(payload, provider="sociamonials", receipt_id=str(post_id),
                              network=result.get("network"), workspace_id=result.get("workspace_id"),
                              profile_refs=result.get("profile_refs") or [],
                              used_assets=result.get("used_assets") or [])
        state = str(result.get("delivery_state") or "unknown").lower()
        if result.get("requires_approval") or state == "requires_approval":
            raise delivery.DeliveryUnknown("Sociamonials is holding this post for approval; release it at the provider")
        if state == "failed":
            reason = "Sociamonials delivery failed: " + _scrub_secrets(str(result.get("delivery_error") or "provider rejected post"))
            delivery.route_outcome(payload, "sociamonials", "failed", reason)
            if self._transition(target, jobs.mark_target_failed, reason):
                self._alert_publish_failure(target, reason)
            return True
        if result.get("delivered") is not True:
            delivery.pending_or_unknown(payload, "Sociamonials delivery is still unconfirmed",
                                        retry_after=result.get("retry_after"))
        delivery.route_outcome(payload, "sociamonials", "delivered")
        delivery.confirmed(payload, provider="sociamonials", receipt_id=str(post_id))
        transitioned = self._transition(target, jobs.mark_target_success)
        if transitioned:
            log.info(f"Confirmed Sociamonials delivery (post_id={post_id})")
            try:
                sociamonials_reclaim.record_delivered_assets(
                    self._db_path, result.get("used_assets"), workspace_id=result.get("workspace_id"),
                    job_id=target.job_id, target_id=target.id, post_id=post_id,
                    network=str(result.get("network") or ""))
            except Exception as exc:
                log.warning(f"Could not record asset receipt: {_scrub_secrets(str(exc))}")
        return transitioned

    async def _try_bundle_social_fallback(
        self, target: jobs.Target, message: str, log, *, only_platform: str | None = None
    ) -> bool:
        """Publish a Reddit target through bundle.social. Best-effort.

        bundle.social holds its own Reddit OAuth app for the same account, so it
        is how Reddit posts continue while the direct credentials are dead. Every
        subreddit rule is enforced before a submit (see myUtils/bundle_social.py)
        and the organization's monthly post cap is respected. Returns ``True``
        only when at least one post was accepted; never raises into the loop.
        """
        if not bundle_social.is_enabled():
            return False
        try:
            job = jobs.get_job(target.job_id, db_path=self._db_path)
        except Exception:  # noqa: BLE001 - a missing job only costs context
            return False
        if only_platform and str(job.platform or "").strip().lower() != only_platform:
            return False
        payload = dict(job.payload or {})
        payload["_db_path"] = str(self._db_path)

        try:
            sau_account = _resolve_structured_account(
                target.account_ref, db_path=self._db_path
            )
        except Exception as exc:  # noqa: BLE001 - never raise into the loop
            log.info(
                f"bundle.social fallback skipped: account lookup failed for "
                f"{target.account_ref} ({_scrub_secrets(str(exc))})"
            )
            return False
        if sau_account is None:
            log.info("bundle.social fallback skipped: account could not be resolved")
            return False

        # The direct path restores offloaded media in default_executor, but the
        # Reddit shortcut skips the executor, so restore here exactly as the
        # Sociamonials fallback does - and refuse to submit if a declared local
        # artifact is still missing rather than uploading a URL that may be dead.
        restore_error: Exception | None = None
        try:
            await asyncio.to_thread(
                _ensure_artifact_paths_local, payload, db_path=self._db_path
            )
        except Exception as exc:  # noqa: BLE001 - a restore miss is not a crash
            restore_error = exc
            log.info(
                f"bundle.social fallback media restore failed: "
                f"{_scrub_secrets(str(exc))}"
            )
        if restore_error is not None:
            still_missing = [
                str(a.get("local_path"))
                for a in (payload.get("artifacts") or [])
                if isinstance(a, dict)
                and a.get("local_path")
                and not _resolve_media_path(str(a["local_path"])).exists()
            ]
            if still_missing:
                raise MediaRestoreError(
                    "bundle.social fallback could not materialise local media "
                    f"({len(still_missing)} file(s)); retrying",
                    retryable=True,
                ) from restore_error
        for _artifact in payload.get("artifacts") or []:
            if isinstance(_artifact, dict) and _artifact.get("public_url"):
                _artifact["public_url"] = _normalise_artifact_url(
                    str(_artifact["public_url"])
                )

        config = dict(sau_account.config or {})
        draft = payload.get("draft") or {}
        try:
            title = prepared_publishers._message_title(payload)
        except Exception:  # noqa: BLE001 - a fallback title is better than none
            title = str(draft.get("title") or "Campaign post").strip()
        try:
            body = prepared_publishers._payload_message(payload)
        except Exception:  # noqa: BLE001
            body = str(draft.get("message") or "").strip()

        account_subs = prepared_publishers._as_subreddit_list(config.get("subreddits"))
        draft_subs = prepared_publishers._as_subreddit_list(draft.get("subreddits"))
        if account_subs:
            allowed = {s.lower().lstrip("r/") for s in account_subs}
            narrowed = [s for s in draft_subs if s.lower().lstrip("r/") in allowed]
            subreddits = narrowed or account_subs
        else:
            subreddits = draft_subs
        if not subreddits:
            log.info("bundle.social fallback skipped: the account has no subreddits")
            return False

        media = prepared_publishers._extract_media(payload)
        media_kind: str | None = None
        media_path = media_url = None
        item = None
        if media.get("videos"):
            media_kind = "video"
            item = media["videos"][0]
        elif media.get("images"):
            media_kind = "image"
            item = media["images"][0]
        if item:
            media_path = item.get("local_path")
            media_url = item.get("public_url")

        from myUtils import subreddits as _subreddits

        registry = _subreddits.get_registry()
        content_kind = prepared_publishers._reddit_content_kind(media)
        monetised = bool(config.get("monetised") or config.get("adultBrand") or True)
        strict = str(os.environ.get("SAU_SUBREDDIT_STRICT", "1")).strip().lower() not in (
            "0", "false", "no", "off", "",
        )
        try:
            channels = bundle_social.reddit_channels(
                bundle_social.get_reddit_account()
            )
        except bundle_social.BundleSocialError as exc:
            log.warning(
                f"bundle.social fallback failed: {_scrub_secrets(str(exc))}"
            )
            return False

        eligible: list[str] = []
        blocked: list[str] = []
        for sub in subreddits:
            reason = registry.denial_reason(
                sub,
                content_kind=content_kind,
                monetised=monetised,
                title=title,
                require_known=strict,
            )
            if reason:
                blocked.append(f"r/{sub}: {reason}")
                continue
            denial = bundle_social.subreddit_denial(channels, sub, media_kind=media_kind)
            if denial:
                blocked.append(denial)
                continue
            eligible.append(sub)
        if blocked:
            log.info("bundle.social fallback skipped subreddits: " + " | ".join(blocked))
        if not eligible:
            return False

        budget = min(
            bundle_social.max_subreddits_per_target(),
            bundle_social.remaining_posts(),
        )
        if budget <= 0:
            log.warning(
                "bundle.social fallback skipped: monthly post quota reached "
                f"({bundle_social.monthly_post_cap()}/month)"
            )
            return False

        posted: list[dict] = []
        errors: list[str] = []
        for sub in eligible[:budget]:
            flair_raw = prepared_publishers._reddit_flair_id(payload, config, sub)
            flair_id = str(flair_raw).strip() or None
            try:
                result = await asyncio.to_thread(
                    bundle_social.publish_reddit_post,
                    subreddit=sub,
                    title=title,
                    body=body,
                    media_path=media_path,
                    media_url=media_url,
                    media_kind=media_kind,
                    flair_id=flair_id,
                    nsfw=True,
                    reference=bundle_social.reference_key(target.id, sub),
                )
                posted.append(result)
            except bundle_social.BundleSocialQuotaError as exc:
                errors.append(str(exc))
                break
            except bundle_social.BundleSocialRuleError as exc:
                errors.append(f"r/{sub}: {exc}")
                continue
            except bundle_social.BundleSocialError as exc:
                if getattr(exc, "retryable", False):
                    raise MediaRestoreError(
                        "bundle.social fallback could not complete the upload: "
                        f"{_scrub_secrets(str(exc))}",
                        retryable=True,
                    ) from exc
                errors.append(f"r/{sub}: {_scrub_secrets(str(exc))}")
                continue
        if not posted:
            if errors:
                log.warning(
                    "bundle.social fallback posted nothing: " + " | ".join(errors)
                )
            return False

        transitioned = self._transition(target, jobs.mark_target_success)
        if transitioned:
            log.info(
                "delivered via bundle.social fallback (post_ids="
                f"{[p.get('post_id') for p in posted]}, subreddits="
                f"{[p.get('subreddit') for p in posted]}); the direct publish "
                f"had failed with: {message}"
            )
        else:
            log.info(
                "bundle.social fallback delivered, but the target was already "
                "transitioned (cancel race); counters left untouched"
            )
        return transitioned

    @classmethod
    def _publish_failure_deep_link(cls, job_id: int) -> str:
        """Build an in-app deep link for a permanently failed target's job.

        Uses the configured public origin (``SAU_PUBLIC_APP_URL``, else the app's
        ``SAU_PUBLIC_BASE_URL``) when available. Without it we return the bare
        hash-history route fragment rather than inventing a hostname — a guessed
        domain reads as a working link to an operator and silently sends them
        nowhere when it is wrong.
        """

        from myUtils import ops_alerts

        fragment = cls._JOB_ROUTE_FRAGMENT.format(job_id=int(job_id))
        base = ops_alerts.public_app_origin()
        if not base:
            return fragment
        from urllib.parse import urlparse
        parsed = urlparse(base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return fragment
        return f"{base}/{fragment}"

    def _alert_publish_failure(self, target: jobs.Target, error: str) -> None:
        """Persist actionable terminal alerts and attempt immediate delivery.

        Notification transport failures retain the event for maintenance retries;
        they never change the already recorded publishing outcome.
        """

        try:
            job = None
            target_row = target
            try:
                job = jobs.get_job(target.job_id, db_path=self._db_path)
                for candidate in jobs.list_targets(
                    target.job_id, db_path=self._db_path
                ):
                    if candidate.id == target.id:
                        target_row = candidate
                        break
            except Exception as lookup_exc:  # noqa: BLE001
                # Still alert with what the in-memory target carries; a missing
                # job row only costs us platform/profile detail.
                _logger.warning(
                    f"publish-failure alert: could not reload job/target "
                    f"job={target.job_id} target={target.id}: "
                    f"{_scrub_secrets(repr(lookup_exc))}"
                )

            from myUtils import publish_alert_outbox
            generation = (getattr(target_row, "checkpoint", None) or {}).get("recovery_generation", "initial")
            event_key = publish_alert_outbox.stop_event_key(target.id, str(generation))
            with jobs._connect(self._db_path, must_exist=True) as conn:
                existing = conn.execute("SELECT id FROM publish_alert_outbox WHERE event_key=?", (event_key,)).fetchone()
            if existing is not None:
                # The event was committed WITH the terminal outcome. Sending
                # happens after that transaction; a crash here leaves it due.
                publish_alert_outbox.flush_due(db_path=self._db_path, event_key=event_key, limit=1)
                return
            platform = (job.platform if job is not None else "") or "unknown"
            profile_id = job.profile_id if job is not None else None
            account_ref = str(getattr(target_row, "account_ref", "") or "unknown")
            account_name = ""
            try:
                account = _resolve_structured_account(account_ref, db_path=self._db_path)
                if account is not None:
                    account_name = str(account.nickname or account.account_name or "").strip()
            except Exception as lookup_exc:  # noqa: BLE001
                _logger.debug(
                    f"publish-failure alert: account label lookup failed for "
                    f"{account_ref}: {_scrub_secrets(repr(lookup_exc))}"
                )
            attempts = int(getattr(target_row, "attempts", target.attempts) or 0)
            max_attempts = self._config.retry.max_attempts
            safe_error = _scrub_secrets(error)

            status = str(getattr(target_row, "status", "failed"))
            next_action = ("Verify the existing provider receipt before any retry."
                           if status == jobs.TARGET_NEEDS_REVIEW else
                           "Fix the cause, preprocess media and use guarded recovery; no automatic retry.")
            lines = [
                f"Publishing stopped with outcome: {status}. {next_action}",
                "",
                f"Job: #{target.job_id} ({platform})",
                f"Target: #{target.id}",
                f"Account: {account_name} ({account_ref})" if account_name else f"Account: {account_ref}",
                f"Profile: {profile_id if profile_id is not None else 'n/a'}",
                f"Attempts: {attempts}/{max_attempts}",
                f"Error: {safe_error}",
            ]
            link = self._publish_failure_deep_link(target.job_id)
            if link:
                lines.extend(["", f"Open in UI: {link}"])

            # Compatibility for injected executors without a persisted target.
            # Production stop events always come from the atomic job transition.
            publish_alert_outbox.enqueue_alert(
                db_path=self._db_path, event_key=event_key,
                subject=f"[SAU] Publish failed: {platform} target #{target.id} (job #{target.job_id})",
                body="\n".join(lines)[:publish_alert_outbox.MAX_BODY_LENGTH],
            )
            publish_alert_outbox.flush_due(db_path=self._db_path, event_key=event_key, limit=1)
        except Exception as alert_exc:  # noqa: BLE001 — alerting must never
            # change the worker's outcome; the target is already failed.
            _logger.warning(
                f"publish-failure alert failed for job={target.job_id} "
                f"target={target.id}: {_scrub_secrets(repr(alert_exc))}"
            )

    def _maybe_close_job_sink(self, job_id: int) -> None:
        """Close the per-job log sink once the job has reached terminal status."""

        try:
            job = jobs.get_job(job_id, db_path=self._db_path)
        except LookupError:
            return
        if job.status in jobs.JOB_TERMINAL:
            close_job_sink(job_id)


# --------------------------- default platform registry ---------------------------


def _backfill_artifact_public_urls(artifacts: list, db_path) -> None:
    """Refresh ONLY the exact registered path/variant, never same-name/source."""
    from myUtils import media_integrity
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("public_url") or not artifact.get("campaign_id") or not artifact.get("local_path"):
            continue
        try:
            path = _resolve_media_path(artifact["local_path"]).resolve()
            expected = media_integrity.expected_sha256(artifact, required=True)
            with jobs._connect(Path(db_path), must_exist=True) as conn:
                rows = conn.execute("SELECT * FROM campaign_artifacts WHERE campaign_id=? AND local_path IN (" +
                    ",".join("?" for _ in _exact_file_refs(path, artifact["local_path"])) + ")",
                    (artifact["campaign_id"], *_exact_file_refs(path, artifact["local_path"]))).fetchall()
            matches = []
            for row in rows:
                metadata = jobs._recovery_json_object(row["metadata_json"] or "{}")
                if (row["public_url"] and row["source_file_record_id"] == artifact.get("source_file_record_id")
                        and row["artifact_kind"] == artifact.get("artifact_kind")
                        and metadata == (artifact.get("metadata") or {})
                        and media_integrity.expected_sha256({"metadata": metadata}) == expected):
                    matches.append(row)
            urls = {row["public_url"] for row in matches}
            if len(urls) == 1:
                artifact["public_url"] = _normalise_artifact_url(next(iter(urls)))
        except (OSError, ValueError, LookupError):
            continue


def _normalise_artifact_url(value: str) -> str:
    """Percent-encode a stored artifact URL so it stays a valid URI.

    Artifact URLs are built from media filenames and were written to the DB
    unencoded before the upload-side fix, so rows still carry raw spaces
    (e.g. ``.../SFW 20260821094430455_pub_pub.mp4``). A raw space is not a valid
    URI: the R2 object serves 200 for the encoded form and fails for the raw
    one, and URL-fetching platforms (Facebook, Instagram, Threads, TikTok) plus
    the Sociamonials fallback reject it outright. Rows are not rewritten here -
    this only normalises the value at the point of use, so both old and new
    rows behave the same.
    """
    text = str(value or "").strip()
    if not text or "://" not in text:
        return text
    try:
        from urllib.parse import quote, urlsplit, urlunsplit

        parts = urlsplit(text)
        if not parts.scheme or not parts.netloc:
            return text
        # safe="/%" keeps the separators and any already-encoded octets intact,
        # so re-encoding an already-encoded URL is a no-op.
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                quote(parts.path, safe="/%"),
                quote(parts.query, safe="=&%?/"),
                parts.fragment,
            )
        )
    except Exception:  # noqa: BLE001 - a malformed URL is returned untouched
        return text


def _fallback_media_paths(payload: dict) -> list[str]:
    """Local media paths a fallback publisher can attach to a post.

    Prefers the campaign artifacts' ``local_path``; when one is absent it hands
    over the artifact's public URL instead, because Sociamonials accepts either
    a local upload or a direct https file URL. Artifacts whose local file has
    been offloaded are skipped here — the caller reaches this only after the
    direct publish already tried and failed to restore them.
    """

    paths: list[str] = []
    for artifact in payload.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        from myUtils.publish_media_guard import media_kind
        if not media_kind(artifact):
            continue
        raw_local_path = str(artifact.get("local_path") or "").strip()
        if raw_local_path:
            candidate = str(_resolve_media_path(raw_local_path))
        else:
            candidate = _normalise_artifact_url(
                str(artifact.get("public_url") or "")
            )
        if candidate and candidate not in paths:
            paths.append(candidate)
    return paths


def _resolve_structured_account(account_ref: str, *, db_path: Path | None = None):
    if not account_ref.startswith("account:"):
        return None
    account_id = int(account_ref.split(":", 1)[1])
    kwargs = {"db_path": db_path} if db_path is not None else {}
    return profile_registry.get_account(account_id, **kwargs)


def _account_profile_mismatch(target, account, job=None) -> str | None:
    """Reason this target's account no longer belongs to its job's profile.

    A target stores only ``account:<id>``. Accounts can be moved between profiles
    after a job is queued, and the worker re-resolves the account at publish
    time, so without this check an account moved to another profile would be
    handed a campaign it does not belong to - publishing Nakedwill content to a
    Sexualwill account, or a Chinese Teaching post to an English account.

    The submit path already scopes account lookup by ``profile_id``
    (``publish_orchestrator._resolve_accounts`` lists by profile first, then
    intersects the selected ids), so a foreign id cannot enter a NEW job. This
    guards the window between queueing and publishing, which that check cannot
    cover.

    Returns a human-readable reason, or ``None`` when the pairing is valid or
    cannot be judged (no job, no profile on the job, or account/profile missing).
    """
    job_profile = getattr(job, "profile_id", None)
    account_profile = getattr(account, "profile_id", None)
    if job_profile is None or account_profile is None:
        return None
    if int(job_profile) == int(account_profile):
        return None
    return (
        f"account {getattr(account, 'id', '?')} belongs to profile "
        f"{account_profile}, but this job is for profile {job_profile}; refusing "
        "to publish across profiles"
    )


def _language_routing_error(target, account, *, db_path) -> str | None:
    """Reason this target must not publish on language grounds, or ``None``.

    Repeats the queue-time routing decision at publish time. Queue-time is not
    enough on its own: the language may have been unknown when the target was
    created (that is exactly what happened while the STT gateway was down for
    days, leaving ~1 900 English targets aimed at Chinese accounts), and a
    campaign can also be re-prepped with different media afterwards.

    A campaign whose language is still unknown is allowed through — refusing
    everything unclassified would stop the pipeline — but the audit reports
    those separately so they can be reviewed.
    """
    import sqlite3

    from myUtils import content_language, jobs as _jobs
    from myUtils import publish_orchestrator

    file_ref = str(getattr(target, "file_ref", "") or "")
    if not file_ref.startswith("campaign_post:"):
        return None
    try:
        post_id = int(file_ref.split(":", 1)[1])
    except (IndexError, ValueError):
        return None
    try:
        with _db_connection(db_path) as conn:
            row = conn.execute(
                "SELECT c.id, c.metadata_json FROM campaign_posts cp "
                "JOIN campaigns c ON c.id = cp.campaign_id WHERE cp.id = ?",
                (post_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    language, source = content_language.from_metadata(row[1])
    if language is None:
        return None
    profile = getattr(account, "profile", None)
    if profile is None:
        # ``_resolve_structured_account`` returns the account row without its
        # profile; the profile only supplies ``default_language`` as a fallback,
        # so a miss here just means the account's own tag decides.
        try:
            from myUtils import profiles as _profiles

            profile_id = getattr(account, "profile_id", None)
            if profile_id is None:
                with _db_connection(db_path) as conn:
                    found = conn.execute(
                        "SELECT profile_id FROM accounts WHERE id = ?",
                        (getattr(account, "id", -1),),
                    ).fetchone()
                profile_id = found[0] if found else None
            profile = _profiles.get_profile(profile_id, db_path=db_path) if profile_id else None
        except Exception:  # noqa: BLE001
            profile = None
    reason = publish_orchestrator.language_routing_skip_reason(
        language, account, profile, include_zh_for_english=False
    )
    if not reason:
        return None
    return (
        f"{reason} (campaign {row[0]} content language {language!r} via {source or 'unknown'}) "
        "— refusing to publish; re-tag the account or move the content."
    )


@contextlib.contextmanager
def _prep_lease_heartbeat(campaign_id: int, owner: str, *, lease_minutes: int, db_path,
                          min_interval: float = 60.0):
    """Keep a long prep's lease fresh so the stale sweep leaves it alone.

    Renews at a third of the lease window, so up to two renewals may fail before
    the campaign is considered abandoned — a hiccup must not cost an hour of
    transcoding, but a dead worker must still be recovered promptly.
    """
    interval = max(float(min_interval), float(lease_minutes) * 60 / 3)
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval):
            try:
                still_ours = campaign_store.renew_campaign_prep_lease(
                    campaign_id, owner, db_path=db_path
                )
                if not still_ours:
                    _logger.warning(
                        f"campaign {campaign_id} prep lease is no longer ours; "
                        "the work will be discarded when it finishes"
                    )
                    return
            except Exception:  # noqa: BLE001 - a heartbeat must never break prep
                _logger.warning(
                    f"campaign {campaign_id} prep lease renewal failed",
                    exc_info=True,
                )

    thread = threading.Thread(
        target=beat, name=f"prep-heartbeat-{campaign_id}", daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def _account_unpublishable(account) -> str | None:
    """Reason this account must not publish, or ``None`` when it may.

    ``enabled=0`` is honoured when a job is *created* (``_resolve_accounts``
    lists with ``enabled=True``) but ``get_account`` does not filter on it, so a
    target queued before the account was disabled would still publish. An
    operator disables an account to stop it publishing - a scheduled target
    firing anyway is the opposite of what the control promises.
    """
    if getattr(account, "enabled", True) is False:
        return (
            f"account {getattr(account, 'id', '?')} is disabled; refusing to "
            "publish. Re-enable it, or cancel this target."
        )
    return None


# Repo mount points that may appear as a stored media-path prefix. The app
# normally runs in a container where the repo is mounted at ``BASE_DIR``
# (``/app``), but the worker is also run on the host (manual drains, scripts,
# the test suite) and rows written on either side carry the other side's
# absolute prefix. Both shapes live in the DB:
#
#   host mount       /home/will/social-auto-upload/...
#   container mount  /app/...
#
# Every known mount is rewritten onto this process's ``BASE_DIR`` so a stored
# path from either side resolves. Configurable for a repo move, a second host,
# or a non-standard container mount. In the container the container alias maps
# onto itself, so the mapping stays an identity there.
_BASE_DIR_ALIASES_ENV = "SAU_BASE_DIR_ALIASES"
_HOST_BASE_DIRS_ENV = "SAU_HOST_BASE_DIRS"
_CONTAINER_BASE_DIRS_ENV = "SAU_CONTAINER_BASE_DIRS"
_DEFAULT_HOST_BASE_DIRS = ("/home/will/social-auto-upload",)
_DEFAULT_CONTAINER_BASE_DIRS = ("/app",)


def _base_dir_aliases() -> tuple[Path, ...]:
    """Repo mount points to map onto ``BASE_DIR``, in priority order.

    ``SAU_BASE_DIR_ALIASES`` overrides both lists with a single comma-separated
    list. Otherwise ``SAU_HOST_BASE_DIRS`` (host mounts) and
    ``SAU_CONTAINER_BASE_DIRS`` (container mounts) are combined, each falling
    back to its default when unset or empty.
    """
    override = os.environ.get(_BASE_DIR_ALIASES_ENV, "")
    roots = [part.strip() for part in override.split(",") if part.strip()]
    if not roots:
        host_raw = os.environ.get(_HOST_BASE_DIRS_ENV, "")
        roots.extend(part.strip() for part in host_raw.split(",") if part.strip())
        if not roots:
            roots.extend(_DEFAULT_HOST_BASE_DIRS)
        container_raw = os.environ.get(_CONTAINER_BASE_DIRS_ENV, "")
        container_roots = [
            part.strip() for part in container_raw.split(",") if part.strip()
        ]
        if not container_roots:
            container_roots = list(_DEFAULT_CONTAINER_BASE_DIRS)
        roots.extend(container_roots)
    return tuple(Path(root) for root in roots)


def _resolve_media_path(value: str | Path) -> Path:
    """Map a stored media path onto a path this process can actually use.

    This is the single canonical normaliser for every stored path the worker
    turns into a real filesystem path. Rows written by host-side tooling (or
    an earlier deploy) carry the host repo prefix while the worker runs in a
    container that mounts the same directories under ``BASE_DIR``; a host-run
    worker instead sees the container prefix (``/app/...``). Rewrite any known
    repo mount onto ``BASE_DIR``; a relative path (``videoFile/_batch1/x.mp4``)
    and any unrelated absolute path are returned untouched.
    """
    text = str(value or "").strip()
    if not text:
        return Path(text)
    candidate = Path(text)
    if not candidate.is_absolute() and not candidate.root:
        return candidate
    base_dir = Path(BASE_DIR)
    for alias in _base_dir_aliases():
        if candidate == alias:
            return base_dir
        try:
            relative = candidate.relative_to(alias)
        except ValueError:
            continue
        return base_dir / relative
    return candidate


def _resolve_account_path(account_ref: str, *, db_path: Path | None = None) -> Path:
    """Map an ``account_ref`` to a concrete cookie file path.

    The Flask backend stores cookie filenames relative to ``cookiesFile/``.
    The CLI / Profile path stores absolute paths. We accept both.
    """

    structured = _resolve_structured_account(account_ref, db_path=db_path)
    if structured is not None:
        return _resolve_media_path(structured.cookie_path)

    candidate = _resolve_media_path(account_ref)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    legacy = Path(BASE_DIR) / "cookiesFile" / account_ref
    if legacy.exists():
        return legacy
    return candidate  # let the uploader complain with its own error


# Minimal content signatures for the media types the restore path handles. A
# download that claims a media extension but does not carry the signature is a
# placeholder or an error page, not media. This is the guard that stopped 63
# Drive objects that are literally 1-4 bytes (a one-byte ``x`` registered as a
# ``*_pub.mp4``) from restoring "successfully" and being handed to uploaders.
_MEDIA_MAGIC: dict[str, tuple[bytes, ...]] = {
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
    ".webp": (b"RIFF",),
    ".webm": (b"\x1a\x45\xdf\xa3",),
    ".mkv": (b"\x1a\x45\xdf\xa3",),
}
# ISO base media file format (mp4/mov/m4v/3gp) carries a four-char box type at
# offset 4; ``ftyp`` is the usual first box, but free/wide/moov/mdat also occur.
_ISO_BMFF_SUFFIXES = (".mp4", ".mov", ".m4v", ".3gp")
_ISO_BMFF_BOX_TYPES = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip")


def _looks_like_media(path: Path, name: str) -> bool:
    """True when ``path`` plausibly holds the media its extension claims.

    An unknown extension is accepted (fail open): the publish path also
    restores non-media sidecars, and this guard exists to reject the known
    placeholder/error-page case, not to reimplement file(1).
    """
    suffix = Path(name).suffix.lower()
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    if suffix in _ISO_BMFF_SUFFIXES:
        return len(head) >= 8 and head[4:8] in _ISO_BMFF_BOX_TYPES
    signatures = _MEDIA_MAGIC.get(suffix)
    if signatures is None:
        return True
    if suffix == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return any(head.startswith(signature) for signature in signatures)


def _verify_artifact(path: Path, artifact: dict, *, required: bool = False) -> dict:
    """Compatibility facade for the ONE checksum/byte-fact contract."""
    from myUtils import media_integrity
    try:
        return media_integrity.verify_artifact(path, artifact, required=required)
    except media_integrity.MediaIntegrityError as exc:
        raise MediaRestoreError(str(exc), retryable=False, category="integrity") from exc


def _download_atomically(destination: Path, download: Callable[[Path], Any]) -> None:
    """Download into a sibling temporary file and publish it only when complete."""
    import tempfile

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        download(temporary)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("download returned an empty media file")
        if not _looks_like_media(temporary, destination.name):
            raise RuntimeError(
                "download is not the media it claims to be: "
                f"{destination.name} ({temporary.stat().st_size} bytes)"
            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class MediaRestoreError(RuntimeError):
    """No-post media prerequisite; integrity failures are never retryable."""

    def __init__(self, *args, retryable: bool = True, category: str | None = None) -> None:
        super().__init__(*args)
        self.retryable = retryable
        self.retry_after_seconds = 120
        self.details = {"prerequisiteFailure": True, "stage": "waiting_dependency",
                        "dependencyCategory": category or ("transport" if retryable else "missing")}


def _media_prerequisite_error(exc):
    """Keep the legacy facade type without losing scoped prerequisite evidence."""
    error = MediaRestoreError(str(exc), retryable=exc.retryable, category=exc.details["dependencyCategory"])
    error.retry_after_seconds = exc.retry_after_seconds
    error.details = copy.deepcopy(exc.details)
    return error

def _file_record_local_path(value: str) -> Path:
    path = _resolve_media_path(value)
    if not path.is_absolute():
        root = Path(BASE_DIR)
        path = root / path if path.parts and path.parts[0] in {"videoFile", "uploads", "generated"} else root / "videoFile" / path
    return path.resolve()


def _exact_file_refs(path: Path, stored: str = "") -> tuple[str, ...]:
    """Only full path aliases from the maintained root mapping; no basenames."""
    refs = {str(path), path.as_posix()}
    if stored:
        refs.add(stored)
    try:
        relative = path.relative_to(Path(BASE_DIR).resolve()).as_posix()
        refs.add(relative)
        if relative.startswith("videoFile/"):
            refs.add(relative.removeprefix("videoFile/"))
    except ValueError:
        pass
    return tuple(sorted(refs))


def _registered_restore_sources(artifact: dict, path: Path, *, payload: dict, db_path: Path | None = None,
                                conn=None) -> tuple[dict, list[dict]]:
    """Resolve exact variants in the caller's or an isolated read snapshot.

    A source_file_record_id is provenance, NOT a substitute for the selected
    part. It is usable only when its complete registered path is this exact file.
    All alternate descriptors must have their own exact path/variant and byte
    proof. Bounded restoration owns deadlines, staging, dependency holds and CAS.
    """
    from myUtils import media_integrity, media_restore, publish_media_guard as guard
    refs = _exact_file_refs(path, str(artifact.get("local_path") or ""))
    selected = copy.deepcopy(artifact)
    selected["local_path"] = str(path)
    campaign_id = artifact.get("campaign_id", payload.get("campaignId"))
    identifier = artifact.get("id", artifact.get("artifact_id"))
    records, campaign_rows = [], []
    owned_connection = conn is None
    if owned_connection and db_path is None:
        raise ValueError("Exact source resolution requires an existing database or caller snapshot")
    with (jobs._connect(db_path, must_exist=True) if owned_connection else contextlib.nullcontext(conn)) as conn:
        if owned_connection:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
        if campaign_id is not None:
            if payload.get("campaignId") is not None and payload["campaignId"] != campaign_id:
                raise media_restore.PrerequisiteUnavailable("identity")
            if identifier is not None:
                row = conn.execute("SELECT * FROM campaign_artifacts WHERE id=? AND campaign_id=?", (identifier, campaign_id)).fetchone()
                if row is None or _resolve_media_path(str(row["local_path"] or "")).resolve() != path:
                    raise media_restore.PrerequisiteUnavailable("identity")
                campaign_rows = [dict(row)]
            else:
                campaign_rows = [dict(row) for row in conn.execute(
                    "SELECT * FROM campaign_artifacts WHERE campaign_id=? AND local_path IN (" + ",".join("?" for _ in refs) + ")",
                    (campaign_id, *refs))]
                if len(campaign_rows) > 1:
                    raise media_restore.PrerequisiteUnavailable("identity")
        file_id = artifact.get("file_record_id")
        if file_id is not None:
            row = conn.execute("SELECT * FROM file_records WHERE id=?", (file_id,)).fetchone()
            if row is None or _file_record_local_path(row["file_path"]) != path:
                raise media_restore.PrerequisiteUnavailable("identity")
            records = [dict(row)]
        else:
            records = [dict(row) for row in conn.execute(
                "SELECT * FROM file_records WHERE file_path IN (" + ",".join("?" for _ in refs) + ")", refs)]
        records = [row for row in records if _file_record_local_path(row["file_path"]) == path]
        if not records and not campaign_rows and not guard._has_derived_identity(artifact):
            source_id = artifact.get("source_file_record_id")
            if source_id is not None:
                row = conn.execute("SELECT * FROM file_records WHERE id=?", (source_id,)).fetchone()
                if row is not None and _file_record_local_path(row["file_path"]) == path:
                    records = [dict(row)]
        if not records and not campaign_rows:
            raise media_restore.PrerequisiteUnavailable("identity")
        sources, expected = [], media_integrity.expected_sha256(selected)
        known_sizes = [selected.get("size_bytes"), (selected.get("metadata") or {}).get("size_bytes")]
        for row in campaign_rows:
            metadata = jobs._recovery_json_object(row["metadata_json"] or "{}")
            original_metadata = selected.get("metadata") or {}
            # A later executed digest may fill unknown facts, not change variant,
            # parent membership, interval, or the approved artifact kind.
            if (row["artifact_kind"] != selected.get("artifact_kind")
                    or any(original_metadata.get(k) != metadata.get(k) for k in (set(original_metadata) | set(metadata)) - {"sha256", "size_bytes"})
                    or row["source_file_record_id"] != selected.get("source_file_record_id")):
                raise media_restore.PrerequisiteUnavailable("identity")
            digest = media_integrity.expected_sha256({"metadata": metadata})
            if expected and digest and expected != digest:
                raise media_integrity.MediaIntegrityError("Registered artifact checksum conflict")
            expected = expected or digest
            known_sizes.append(metadata.get("size_bytes"))
            selected.update(id=row["id"], campaign_id=campaign_id, metadata=metadata)
        for row in records:
            if payload.get("_workspace_id") is not None and row.get("workspace_id") != payload["_workspace_id"]:
                raise media_restore.PrerequisiteUnavailable("identity")
            digest = media_integrity.expected_sha256({"sha256": row.get("sha256")})
            if expected and digest and expected != digest:
                raise media_integrity.MediaIntegrityError("Registered file checksum conflict")
            expected = expected or digest
            known_sizes.append(row.get("size_bytes"))
        sizes = [size for size in known_sizes if size is not None]
        if any(type(size) is not int or size <= 0 for size in sizes) or len(set(sizes)) > 1:
            raise media_integrity.MediaIntegrityError("Registered byte counts conflict")
        if expected:
            selected["sha256"] = expected
        if sizes:
            selected["size_bytes"] = sizes[0]
        if records and not campaign_rows:
            selected["file_record_id"] = records[0]["id"]
        for row in [*campaign_rows, *records]:
            key = row.get("remote_path") if "campaign_id" in row else row.get("storage_key")
            backend_id = row.get("storage_backend_id")
            scope = {"workspace_id": row.get("workspace_id", payload.get("_workspace_id")),
                     "account_id": payload.get("_restore_account_id"),
                     "expected_size_bytes": sizes[0] if sizes else None}
            metadata = jobs._recovery_json_object(row.get("metadata_json") or "{}")
            proof = metadata.get("remote_checksum")
            if backend_id is not None and key:
                backend = conn.execute("SELECT * FROM storage_backends WHERE id=?", (backend_id,)).fetchone()
                if backend is not None:
                    sources.append({**scope, "backend": dict(backend), "storage_key": key, "remote_checksum": proof})
            url = row.get("public_url") if "campaign_id" in row else row.get("storage_cdn_url")
            if url:
                sources.append({**scope, "public_url": _normalise_artifact_url(url), "remote_checksum": proof})
    # A client URL/remote key is never a restoration registration.
    if not sources:
        raise media_restore.PrerequisiteUnavailable("missing")
    if len(sources) > media_restore.MAX_SOURCES:
        raise media_restore.PrerequisiteUnavailable("identity")
    return selected, sources


def _resolve_file_path(file_ref: str, *, db_path: Path | None = None) -> Path:
    path = _file_record_local_path(file_ref)
    if path.is_file():
        return path
    if db_path is not None:
        restored = _try_download_from_storage(file_ref, db_path)
        if restored is not None:
            return restored
    raise MediaRestoreError("Required media is unavailable locally or in exact registered storage", retryable=False)


def _try_download_from_storage(file_ref: str, db_path: Path) -> Path | None:
    from myUtils import media_restore
    path = _file_record_local_path(file_ref)
    # An unrelated absolute path cannot use this project's same-named file.
    if Path(file_ref).is_absolute() and not path.is_relative_to(Path(BASE_DIR).resolve()):
        return None
    try:
        artifact, sources = _registered_restore_sources({"local_path": str(path)}, path, payload={}, db_path=db_path)
    except media_restore.PrerequisiteUnavailable as exc:
        if exc.details["dependencyCategory"] == "identity":
            return None
        raise _media_prerequisite_error(exc) from exc
    if path.is_file():
        _verify_artifact(path, artifact)
        return path
    try:
        media_restore.restore_artifact(path, artifact, sources, db_path=db_path)
    except media_restore.PrerequisiteUnavailable as exc:
        raise _media_prerequisite_error(exc) from exc
    return path


def _prepared_artifact_local_paths(payload: dict) -> list[Path]:
    from myUtils.publish_media_guard import media_kind
    paths: list[Path] = []
    seen: set[str] = set()
    for artifact in payload.get("artifacts", []) or []:
        if not isinstance(artifact, dict) or not media_kind(artifact):
            continue
        local_path = artifact.get("local_path")
        if not local_path or local_path in seen:
            continue
        seen.add(local_path)
        paths.append(_resolve_media_path(local_path))
    return paths


def _ensure_artifact_paths_local(payload: dict, *, db_path: Path) -> None:
    """Materialise exact artifacts, with bounded alternatives and durable holds.

    Receipt polling never calls this. Local legacy bytes are validation-only;
    missing files require exact registrations and executed checksum evidence.
    """
    from myUtils import media_integrity, media_restore, publish_media_guard as guard
    artifacts = payload.get("artifacts") or []
    if not isinstance(artifacts, list) or any(not isinstance(a, dict) for a in artifacts):
        raise MediaRestoreError("Invalid media artifact set", retryable=False, category="identity")
    deadline = time.monotonic() + media_restore.OVERALL_DEADLINE_SECONDS
    for artifact in artifacts:
        if guard.is_context_artifact(artifact):
            continue
        local = artifact.get("local_path")
        if not local:
            raise MediaRestoreError("Exact local artifact identity is required before publishing", retryable=False, category="identity")
        path = _resolve_media_path(local)
        if not path.is_absolute():
            path = Path(BASE_DIR) / path
        path = path.resolve()
        if path.is_file():
            _verify_artifact(path, artifact)
        else:
            try:
                registered, sources = _registered_restore_sources(artifact, path, payload=payload, db_path=db_path)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise media_restore.PrerequisiteUnavailable("transport")
                facts = media_restore.restore_artifact(path, registered, sources, db_path=db_path, total_deadline=remaining)
                artifact.update(sha256=facts["sha256"], size_bytes=facts["size_bytes"])
                if registered.get("campaign_id") is not None:
                    artifact.update(id=registered["id"], campaign_id=registered["campaign_id"])
                    artifact.setdefault("metadata", {}).update(sha256=facts["sha256"], size_bytes=facts["size_bytes"])
                elif registered.get("file_record_id") is not None:
                    artifact["file_record_id"] = registered["file_record_id"]
                _verify_artifact(path, artifact, required=True)
            except media_integrity.MediaIntegrityError as exc:
                raise MediaRestoreError(str(exc), retryable=False, category="integrity") from exc
            except media_restore.PrerequisiteUnavailable as exc:
                raise _media_prerequisite_error(exc) from exc
        artifact["local_path"] = str(path)


def _now_utc_naive() -> datetime:
    """Current time as a tz-naive UTC datetime.

    Matches the shape ``jobs._now_iso`` writes and ``publish_orchestrator``
    stores in ``publish_job_targets.schedule_at``, so the two compare
    chronologically without mixing offset-aware and offset-naive datetimes.
    """
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def _publish_date_for_target(target: jobs.Target) -> "datetime | int":
    """The ``publish_date`` to hand an uploader for a claimed target.

    A claimed target is, by construction, already due: ``jobs._claimable_clause``
    only hands out targets whose ``schedule_at`` (tz-naive UTC, same shape as
    ``jobs._now_iso``) has arrived. Forwarding that now-past datetime to an
    uploader tells the platform to *schedule* a post in the past, which every
    uploader's ``validate_publish_date`` refuses — X rejects it outright with
    "定时发布时间必须晚于当前时间", which silently failed every staggered X target
    from 2026-09-12 onwards.

    The worker publishes when the time comes, so return the no-scheduling
    sentinel (``0``) and let the post go out now. A target with no
    ``schedule_at``, or one that somehow has not fallen due yet, is passed
    through unchanged.
    """
    schedule_at = target.schedule_at or 0
    if isinstance(schedule_at, str) and schedule_at:
        try:
            schedule_at = datetime.fromisoformat(schedule_at)
        except ValueError:
            schedule_at = 0

    if isinstance(schedule_at, datetime) and schedule_at <= _now_utc_naive():
        return 0
    return schedule_at


async def _run_platform_upload(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account_file: Path,
    file_path: Path | None = None,
    thread_file_paths: list[Path] | None = None,
    delivery_observer=None,
) -> None:
    """Per-platform dispatch.

    Split out from ``default_executor`` so the cookie-decryption wrapper
    lives in one place and the platform router stays purely about argument
    shaping.
    """

    schedule_at = _publish_date_for_target(target)

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
            **({"delivery_observer": delivery_observer} if delivery_observer is not None else {}),
        )
        await app.main()
        return

    if platform == "patreon":
        from uploader.patreon_uploader.main import PatreonPost

        import tempfile, os
        body_text = payload.get("body", "") or title
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            tmp.write(body_text)
            body_file = tmp.name

        try:
            app = PatreonPost(
                title=title,
                body_file=body_file,
                tags=tags,
                publish_date=schedule_at,
                account_file=str(account_file),
            )
            await app.main()
        finally:
            os.unlink(body_file)
        return

    raise ValueError(f"Unsupported publish platform: {platform!r}")


def _persist_rotated_config(account, updated_config, *, payload: dict | None = None) -> None:
    """Persist a publisher's rotated OAuth config to the account's own DB.

    A rotated single-use refresh token written to the process-default database
    instead of the active ``SAU_DB_PATH`` leaves the account stranded: the next
    run reads the stale token and fails with ``invalid_grant``. The task payload
    carries ``_db_path`` (injected by ``_run_target``) so the write lands where
    the account was read from.
    """
    if not (isinstance(updated_config, dict) and updated_config):
        return
    raw = (payload or {}).get('_db_path')
    db_path = Path(raw) if raw else profile_registry.DB_PATH
    changes = token_state.credential_changes(account.config or {}, updated_config)
    if changes:
        # Final publisher writeback may follow an already-persisted rotation
        # or a reconnect. Discard stale state without failing a delivered post.
        token_state.commit_success(account, changes, db_path=db_path)


async def _publish_prepared_twitter(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account=None,
    account_file: Path | None,
) -> None:
    config = dict(account.config or {}) if account else {}
    twitter_auth_type = profile_registry.effective_auth_type(
        config, getattr(account, "auth_type", None), "twitter"
    ) or "cookie"
    artifacts = payload.get("artifacts") or []
    reconciling = twitter_auth_type != "cookie" and bool(delivery.receipt_for(payload, "twitter"))
    if twitter_auth_type == "cookie" and delivery.receipt_for(payload, "twitter"):
        # A browser thread prefix is not replayable/pollable via a fresh composer.
        # Its last receipt alone does not prove all ordered parts completed.
        current = delivery.state(payload)
        if current.get("stage") == "browser_delivered":
            return
        raise delivery.DeliveryUnknown("Retained X browser receipt/partial thread requires reconciliation, not another create")
    file_paths = _prepared_artifact_local_paths(payload)
    if not reconciling:
        if payload.get("campaignId") and not artifacts:
            raise ValueError("Prepared Twitter campaign has no media artifacts")
        if (payload.get("campaignId") or artifacts) and not file_paths:
            raise ValueError("Prepared Twitter media artifacts have no usable local paths")
        for path in file_paths:
            if not path.is_file() or path.stat().st_size <= 0:
                raise FileNotFoundError(f"Prepared Twitter media file is missing or empty: {path}")
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
                raise ValueError(f"Unsupported prepared Twitter media type: {path.suffix}")

    if twitter_auth_type == "cookie":
        if any(not artifact.get("local_path") for artifact in artifacts):
            raise ValueError("Prepared Twitter media artifacts are missing local paths")
        if not account_file:
            raise ValueError("Prepared Twitter publish requires a cookie-backed account")
        if not file_paths:
            if payload.get("campaignId") or artifacts:
                raise ValueError("Prepared Twitter publish requires local media artifacts")
            return
        from myUtils.browser_helper import local_browser
        with local_browser():
            observer = _make_x_browser_delivery_observer(payload, target)
            if observer is None:
                _begin_native_publish_attempt(payload, target)
                delivery.starting(payload, operation="browser-publish:twitter")
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
                delivery_observer=observer,
            )
            if observer is not None and delivery.state(payload).get("stage") != "browser_delivered":
                raise delivery.DeliveryUnknown("Instrumented X browser did not confirm every exact ordered part")
    else:
        if account is None:
            raise ValueError("Prepared Twitter API publish requires a structured account")
        if not reconciling and any(not artifact.get("local_path") for artifact in artifacts):
            raise ValueError("Prepared Twitter media artifacts are missing local paths")
        result = await asyncio.to_thread(
            prepared_publishers.publish_twitter_sync, account, payload
        )
        updated_config = result.get('updated_config') if isinstance(result, dict) else None
        _persist_rotated_config(account, updated_config, payload=payload)
        return result


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
    return await asyncio.to_thread(prepared_publishers.publish_telegram_sync, account, payload)


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
    config = dict(account.config or {})
    reddit_auth_type = profile_registry.effective_auth_type(
        config, getattr(account, "auth_type", None), "reddit"
    ) or "api"
    config.setdefault("redditAuthType", reddit_auth_type)
    artifacts = payload.get("artifacts") or []
    if artifacts and not any(artifact.get("local_path") or artifact.get("public_url") for artifact in artifacts):
        raise ValueError("Prepared Reddit media artifacts have no usable source")

    if reddit_auth_type == "cookie":
        if not account_file:
            raise ValueError("Prepared Reddit cookie publish requires account_file (storage_state)")
        from uploader.reddit_uploader.main import RedditCookieVideo

        # The account config is the source of truth (see publish_reddit_sync);
        # a draft may narrow it, never widen it to a subreddit the account no
        # longer targets.
        draft = payload.get("draft") or {}
        account_subreddits = prepared_publishers._as_subreddit_list(config.get("subreddits"))
        draft_subreddits = prepared_publishers._as_subreddit_list(draft.get("subreddits"))
        if account_subreddits:
            allowed = {name.lower().lstrip("r/") for name in account_subreddits}
            narrowed = [
                name for name in draft_subreddits
                if name.lower().lstrip("r/") in allowed
            ]
            subreddits = narrowed or account_subreddits
        else:
            subreddits = draft_subreddits
        if not subreddits:
            raise ValueError("Reddit cookie publish requires at least one subreddit")
        title = payload.get("message") or payload.get("draft", {}).get("message", "") or ""
        file_paths = _prepared_artifact_local_paths(payload)
        media_path = str(file_paths[0]) if file_paths else ""
        if artifacts and not media_path:
            raise ValueError("Prepared Reddit media is required but no local file is available")
        body_text = payload.get("draft", {}).get("body", "") or ""

        for subreddit in subreddits:
            app = RedditCookieVideo(
                title=title,
                subreddit=subreddit,
                account_file=str(account_file),
                file_path=media_path,
                body_text=body_text,
            )
            await app.main()
    else:
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
    return await asyncio.to_thread(prepared_publishers.publish_youtube_sync, account, payload)


async def _publish_prepared_tiktok(platform, payload, target, *, account, account_file):
    if account is None:
        raise ValueError("Prepared TikTok publish requires a structured account")
    from myUtils import tiktok_delivery
    receipt = delivery.receipt_for(payload, "tiktok")
    if receipt is None:
        result = await asyncio.to_thread(prepared_publishers.publish_tiktok_sync, account, payload)
        _persist_rotated_config(account, result.get("updated_config"), payload=payload)
        publish_id = ((result.get("publish") or {}).get("data") or {}).get("publish_id")
        if not isinstance(publish_id, str) or not publish_id.strip() or len(publish_id) > 64:
            raise delivery.DeliveryUnknown("TikTok returned no usable publish receipt")
        if delivery.receipt_for(payload, "tiktok") is None:
            try:
                intent = tiktok_delivery.request_intent(dict(account.config or {}), payload)
            except ValueError as exc:
                raise delivery.DeliveryUnknown(str(exc)) from exc
            delivery.accepted(payload, provider="tiktok", receipt_id=publish_id, **intent)
        receipt = delivery.receipt_for(payload, "tiktok")
        access_token = result.get("access_token")
    else:
        publish_id = receipt["id"]
        try:
            access_token, updated = await asyncio.to_thread(
                prepared_publishers._ensure_tiktok_access_token, dict(account.config or {}))
        except prepared_publishers.PreparedPublishError as exc:
            raise delivery.DeliveryUnknown(
                "TikTok's stored receipt needs a working credential; reconnect and poll it, do not resubmit") from exc
        _persist_rotated_config(account, updated, payload=payload)
    if not access_token:
        raise delivery.DeliveryUnknown("TikTok receipt exists but its credential is unavailable")
    try:
        response = await asyncio.to_thread(prepared_publishers.fetch_tiktok_publish_status, access_token, publish_id)
    except (delivery.LeaseLost, delivery.DeliveryPending, delivery.DeliveryUnknown):
        raise
    except prepared_publishers.PreparedPublishError as exc:
        if not exc.retryable:
            raise delivery.DeliveryUnknown(
                "TikTok rejected a stored-receipt status check; fix its credential and reconcile without reposting") from exc
        delivery.pending_or_unknown(payload, "TikTok receipt status is temporarily unavailable",
                                    retry_after=_retry_after_seconds(exc))
    except Exception:
        delivery.pending_or_unknown(payload, "TikTok receipt status is temporarily unavailable")
    data = response.get("data") or {}
    try:
        intent = tiktok_delivery.receipt_intent(receipt or {}, payload)
        view = tiktok_delivery.delivery_view(data, intent)
    except ValueError as exc:
        raise delivery.DeliveryUnknown(str(exc)) from exc
    status = view["provider_status"]
    ids = view["public_post_ids"]
    reason = _scrub_secrets(str(data.get("fail_reason") or ""))
    metadata = {"provider_status": status, "public_post_ids": ids, "terminal_failure": view["state"] == "failed"}
    if view.get("publication_visibility"):
        metadata["publication_visibility"] = view["publication_visibility"]
    delivery.accepted(payload, provider="tiktok", receipt_id=publish_id,
                      status="awaiting_public_moderation" if status == "publish_complete" and view["state"] == "pending" else view["state"],
                      **metadata)
    from urllib.parse import urlparse
    platform_url = data.get("platform_url")
    try:
        parsed = urlparse(platform_url) if isinstance(platform_url, str) else None
        if (parsed is None or parsed.scheme != "https" or parsed.hostname not in {"www.tiktok.com", "tiktok.com"}
                or parsed.username or parsed.password):
            platform_url = None
    except ValueError:
        platform_url = None
    username = (receipt or {}).get("creator_username")
    if platform_url is None and ids and isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,32}", username):
        platform_url = f"https://www.tiktok.com/@{username}/video/{ids[0]}"
    db_path = Path(payload["_db_path"]) if payload.get("_db_path") else None
    jobs.upsert_tiktok_publish_status(publish_id, job_id=str(target.job_id), account_id=str(account.id),
        status=status, fail_reason=reason or None,
        post_id=ids[0] if ids else None, platform_url=platform_url, db_path=db_path)
    if view["state"] == "delivered":
        delivery.confirmed(payload, provider="tiktok", receipt_id=publish_id)
        return
    if view["state"] == "failed":
        raise delivery.ProviderConfirmedFailure("tiktok", publish_id,
                                                f"TikTok publish failed: {reason or 'provider rejected post'}")
    if view["state"] == "needs_review":
        raise delivery.DeliveryUnknown(view["reason"])
    delivery.pending_or_unknown(payload, view["reason"])


async def _publish_prepared_facebook(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared Facebook publish requires a structured account")
    result = await asyncio.to_thread(prepared_publishers.publish_facebook_sync, account, payload)
    updated_config = result.get('updated_config') if isinstance(result, dict) else None
    _persist_rotated_config(account, updated_config, payload=payload)
    return result


async def _publish_prepared_instagram(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared Instagram publish requires a structured account")
    result = await asyncio.to_thread(prepared_publishers.publish_instagram_sync, account, payload)
    updated_config = result.get('updated_config') if isinstance(result, dict) else None
    _persist_rotated_config(account, updated_config, payload=payload)
    return result


async def _publish_prepared_threads(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared Threads publish requires a structured account")
    result = await asyncio.to_thread(prepared_publishers.publish_threads_sync, account, payload)
    updated_config = result.get('updated_config') if isinstance(result, dict) else None
    _persist_rotated_config(account, updated_config, payload=payload)
    return result


async def _publish_prepared_discord(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared Discord publish requires a structured account")
    return await asyncio.to_thread(prepared_publishers.publish_discord_sync, account, payload)


async def _publish_prepared_patreon(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if not account_file:
        raise ValueError("Prepared Patreon publish requires a cookie-backed account (storage_state)")

    from uploader.patreon_uploader.main import (
        PatreonPost,
        PATREON_ACCESS_PUBLIC,
        PATREON_PUBLISH_STRATEGY_IMMEDIATE,
    )

    config = dict(account.config or {}) if account else {}
    title = payload.get("message") or payload.get("draft", {}).get("message", "") or ""
    body_text = payload.get("draft", {}).get("body", "") or title
    tags = payload.get("draft", {}).get("hashtags", []) or payload.get("tags", []) or []

    file_paths = _prepared_artifact_local_paths(payload)
    artifacts = payload.get("artifacts") or []
    if payload.get("campaignId") and not artifacts:
        raise ValueError("Prepared Patreon campaign has no media artifacts")
    if artifacts and not file_paths:
        raise ValueError("Prepared Patreon media artifacts have no local paths")
    if any(not artifact.get("local_path") for artifact in artifacts):
        raise ValueError("Prepared Patreon media artifacts are missing local paths")
    if any(not p.is_file() or p.stat().st_size <= 0 for p in file_paths):
        raise FileNotFoundError("One or more prepared Patreon media files are missing or empty")
    attachments = [str(p) for p in file_paths]

    access_mode = str(config.get("accessMode") or PATREON_ACCESS_PUBLIC).lower()
    tier_name = str(config.get("tierName") or "").strip() or None
    publish_strategy = str(config.get("publishStrategy") or PATREON_PUBLISH_STRATEGY_IMMEDIATE).lower()

    import tempfile, os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write(body_text)
        body_file = tmp.name

    try:
        app = PatreonPost(
            title=title or "Campaign post",
            body_file=body_file,
            tags=tags,
            publish_date=0,
            account_file=str(account_file),
            attachments=attachments or None,
            access_mode=access_mode,
            tier_name=tier_name,
            publish_strategy=publish_strategy,
        )
        delivery.starting(payload, operation="browser-publish:patreon")
        return await app.publish()
    finally:
        os.unlink(body_file)


async def _publish_prepared_teaching_blog(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared Teaching Blog publish requires a structured account")
    return await asyncio.to_thread(prepared_publishers.publish_teaching_blog_sync, account, payload)


async def _publish_prepared_nw_sw_blog(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared NW/SW Blog publish requires a structured account")
    return await asyncio.to_thread(prepared_publishers.publish_nw_sw_blog_sync, account, payload)


async def _publish_prepared_bluesky(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared Bluesky publish requires a structured account")
    return await asyncio.to_thread(prepared_publishers.publish_bluesky_sync, account, payload)


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
    "facebook": _publish_prepared_facebook,
    "instagram": _publish_prepared_instagram,
    "reddit": _publish_prepared_reddit,
    "telegram": _publish_prepared_telegram,
    "youtube": _publish_prepared_youtube,
    "tiktok": _publish_prepared_tiktok,
    "threads": _publish_prepared_threads,
    "discord": _publish_prepared_discord,
    "patreon": _publish_prepared_patreon,
    "teaching_blog": _publish_prepared_teaching_blog,
    "nw_sw_blog": _publish_prepared_nw_sw_blog,
    "bluesky": _publish_prepared_bluesky,
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
    # If the campaign post pinned itself to a subset of media files (used
    # by the Publish Center's single-media split), filter the artifact
    # list before handing off to the platform publisher so it doesn't
    # accidentally upload media items the post intentionally excluded.
    original_artifacts = payload.get("artifacts")
    file_record_ids = payload.get("fileRecordIds")
    if isinstance(file_record_ids, list) and file_record_ids:
        allowed = {int(v) for v in file_record_ids if v is not None}
        original = payload.get("artifacts") or []
        kept = []
        for artifact in original:
            source_id = artifact.get("source_file_record_id")
            if source_id is None or int(source_id) in allowed:
                kept.append(artifact)
        payload["artifacts"] = kept
    try:
        if (payload.get("campaignId") and not payload.get("artifacts")
                and platform in {"tiktok", "youtube", "instagram", "twitter"}
                and not delivery.state(payload).get("provider_receipt")):
            raise ValueError(f"Prepared {platform} campaign has no media artifacts")
        return await publisher(platform=platform, payload=payload, target=target, account=account, account_file=account_file)
    finally:
        updated_config = payload.pop("_updated_config", None)
        if account is not None and updated_config:
            _persist_rotated_config(account, updated_config, payload=payload)
        if original_artifacts is not None:
            payload["artifacts"] = original_artifacts


def _public_https_url(value: str) -> bool:
    from ipaddress import ip_address
    from urllib.parse import urlparse

    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return False
    try:
        return ip_address(host).is_global
    except ValueError:
        return "." in host and not host.endswith(".")


def _download_public_artifact(url: str, destination: Path) -> None:
    """Bounded transport facade; publication restoration uses registered proof."""
    from myUtils import outbound_http, media_restore
    try:
        _download_atomically(destination, lambda temporary: outbound_http.download_public_file(
            url, temporary, total_deadline=media_restore.BACKEND_DEADLINE_SECONDS,
            max_bytes=media_restore.MAX_RESTORE_BYTES))
    except outbound_http.OutboundUrlError:
        raise MediaRestoreError("Unsafe public HTTPS media URL", retryable=False, category="identity") from None


def _write_response_chunks(response, destination: Path) -> None:
    with destination.open("wb") as output:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                output.write(chunk)


def _restore_optional_thumbnail(payload: dict, *, db_path: Path) -> None:
    thumbnail_ref = str(payload.get("thumbnail") or "")
    if not thumbnail_ref:
        return
    thumb = _resolve_media_path(thumbnail_ref)
    if not thumb.is_absolute():
        thumb = Path(BASE_DIR) / thumbnail_ref
    if thumb.exists():
        return
    candidates = [thumbnail_ref]
    if "videoFile/" in thumbnail_ref:
        candidates.append(thumbnail_ref.split("videoFile/", 1)[1])
    for ref in candidates:
        try:
            restored = _try_download_from_storage(ref, db_path)
        except MediaRestoreError as exc:
            _logger.warning(f"optional thumbnail restore failed for {thumbnail_ref}: {exc}")
            continue
        if restored is not None and restored.exists():
            payload["thumbnail"] = str(restored)
            return

@dataclass(frozen=True)
class PreparationCompleted:
    target_ids: tuple[int, ...]
    generation: str


@dataclass(frozen=True)
class PreparationDeferred:
    reason: str
    category: str = "transport"
    retry_after_seconds: float = 120


_EXECUTION_PAYLOAD_KEYS = {"_db_path", "_workspace_id", "_restore_account_id", "_telegramDeliveryKey",
    "_delivery", "_save_checkpoint", "_fallback_attempted", "_publish_max_attempts", "_native_route_started",
    "_prerequisites_checked", "_defer_native_create_budget", "_teaching_dispatch_check"}


def _publication_payload(payload: dict) -> dict:
    return copy.deepcopy({key: value for key, value in payload.items() if key not in _EXECUTION_PAYLOAD_KEYS})


def _begin_native_publish_attempt(payload: dict, target: jobs.Target) -> None:
    if payload.get("_native_route_started"):
        return
    current = delivery.state(payload)
    routes = delivery.publish_routes(payload)
    if "native" not in routes:
        baseline = current.get("provider_budget_baseline", 0 if current.get("recovery_generation") or current.get("preparation_generation") else max(0, target.attempts - 1))
        if type(baseline) is not int or baseline < 0:
            raise delivery.DeliveryUnknown("Invalid historical provider budget baseline")
        routes["native"] = {"attempts": baseline}
        delivery.checkpoint(payload, publish_routes=routes)
    delivery.begin_route_attempt(payload, "native", max_attempts=payload.get("_publish_max_attempts", 3))
    payload["_native_route_started"] = True


def _x_browser_adapted() -> bool:
    from uploader.twitter_uploader.main import TwitterThreadVideo
    return "delivery_observer" in inspect.signature(TwitterThreadVideo).parameters


def _make_x_browser_delivery_observer(payload: dict, target: jobs.Target):
    from myUtils import generic_delivery
    if (not _x_browser_adapted() or not callable(payload.get("_save_checkpoint"))
            or str(payload.get("_telegramDeliveryKey")) != str(target.id)):
        return None  # Uninstrumented legacy caller retains the conservative fence.
    observer = generic_delivery.make_x_browser_observer(payload, target_id=target.id,
        account_key=target.account_ref, max_create_attempts=payload.get("_publish_max_attempts", 3), max_media_requests=256)
    def observe(event):
        if event.get("event") == "create_action":
            # The uploader calls this immediately BEFORE the Post-producing
            # action. Preview/navigation timeouts do not spend native attempts.
            _begin_native_publish_attempt(payload, target)
        return observer(event)
    return observe


def _materialise_publish_media(payload: dict, target: jobs.Target, *, db_path: Path) -> None:
    from myUtils import media_integrity, media_restore, publish_media_guard as guard
    if not payload.get("artifacts") and not target.file_ref.startswith("campaign_post:") and target.file_ref not in {"text", "text-only"}:
        refs = payload.get("threadFileRefs") or [target.file_ref]
        artifacts = []
        for ref in refs:
            path = _file_record_local_path(ref)
            aliases = _exact_file_refs(path, ref)
            with jobs._connect(db_path, must_exist=True) as conn:
                rows = conn.execute("SELECT * FROM file_records WHERE file_path IN (" + ",".join("?" for _ in aliases) + ")", aliases).fetchall()
            rows = [row for row in rows if _file_record_local_path(row["file_path"]) == path]
            if len(rows) != 1:
                raise media_restore.PrerequisiteUnavailable("identity")
            row = rows[0]
            artifacts.append({"artifact_kind": "source_image" if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"} else "source_video",
                "local_path": str(path), "file_record_id": row["id"], "source_file_record_id": row["id"],
                "sha256": row["sha256"], "size_bytes": row["size_bytes"]})
        payload["artifacts"] = artifacts
    _ensure_artifact_paths_local(payload, db_path=db_path)
    for artifact in payload.get("artifacts") or []:
        if guard.is_context_artifact(artifact):
            continue
        path = Path(artifact["local_path"])
        actual = _verify_artifact(path, artifact)
        artifact.setdefault("sha256", actual["sha256"])
        if artifact.get("sha256") is None:
            artifact["sha256"] = actual["sha256"]
        artifact.setdefault("size_bytes", actual["size_bytes"])
        if artifact.get("size_bytes") is None:
            artifact["size_bytes"] = actual["size_bytes"]
        # Bind an exact raw file registration, not its source ID or basename.
        if not artifact.get("campaign_id") and not payload.get("campaignId") and not artifact.get("file_record_id") and not guard._has_derived_identity(artifact):
            aliases = _exact_file_refs(path, artifact["local_path"])
            with jobs._connect(db_path, must_exist=True) as conn:
                rows = conn.execute("SELECT id,file_path FROM file_records WHERE file_path IN (" + ",".join("?" for _ in aliases) + ")", aliases).fetchall()
            rows = [row for row in rows if _file_record_local_path(row["file_path"]) == path]
            if len(rows) == 1:
                artifact["file_record_id"] = rows[0]["id"]
    # Approved covers are prerequisites, not optional silently dropped fields.
    if payload.get("thumbnail"):
        cover = {"local_path": str(_file_record_local_path(payload["thumbnail"])), "artifact_kind": "thumbnail", "metadata": {"role": "cover"}}
        _ensure_artifact_paths_local({**payload, "artifacts": [cover]}, db_path=db_path)
        payload["thumbnail"] = cover["local_path"]


def _fresh_preparation_context(account, payload: dict, *, db_path: Path, backup: bool = False) -> dict:
    """Current exact grants/caps with persistent scoped read-only dependency holds."""
    from myUtils import preparation_context, backup_preparation, publish_readiness, media_restore, native_destinations
    if account is None:
        raise media_restore.PrerequisiteUnavailable("configuration")
    settings = profile_registry.get_profile(account.profile_id, db_path=db_path).settings
    context = publish_readiness._context(account, settings)
    if context["provider"] == "direct":
        native_destinations.assert_effective_destination(account.platform, account.config, payload)
    projection = publish_readiness.destination_readiness(account, settings, db_path=db_path)
    scope = publish_readiness.destination_scope_key(account, settings)
    if backup and context["provider"] != "sociamonials":
        scope += ":backup"
        mapping = sociamonials_fallback.resolve_mapping(account.id, account=account, platform=account.platform, settings=settings)
        if not mapping:
            raise media_restore.PrerequisiteUnavailable("configuration")
        fingerprint = jobs._prepared_digest([context["fingerprint"], mapping,
                          sociamonials_fallback.resolve_credentials(mapping)])
    else:
        fingerprint = context["fingerprint"]
    if projection.get("reasonCode") == "dependency_cooldown" and projection.get("nextAttemptAt"):
        delay = max(1, (jobs._utc_datetime(projection["nextAttemptAt"]) - _now_utc_naive()).total_seconds())
        raise media_restore.PrerequisiteUnavailable("transport", retry_after_seconds=delay, scope_key=scope, cached_wait=True)
    managed = backup or context["provider"] == "sociamonials"
    needs_read = managed or not projection["ready"]
    if needs_read:
        media_restore._admit(db_path, scope, fingerprint, "destination_verification")
    try:
        if managed:
            result = backup_preparation.resolve_constraints(platform=account.platform, account=account,
                payload=payload, settings=settings, db_path=db_path, admit_backup=backup and context["provider"] != "sociamonials")
            backup_preparation.validate_intent(payload, context=result, account_config=account.config, prepared=False)
            result["backup_compatible"] = True
        else:
            if not projection["ready"]:
                supported = {"tiktok", "youtube", "facebook", "instagram", "threads"} | native_destinations.SUPPORTED_PLATFORMS
                if projection["reasonCode"] in {"disabled", "revoked", "expired", "native_hold", "configuration_required"} or account.platform not in supported:
                    raise media_restore.PrerequisiteUnavailable("configuration")
                projection = preparation_context.verify_native_destination(account, settings=settings, db_path=db_path, payload=payload)
            if not projection["ready"]:
                raise media_restore.PrerequisiteUnavailable("configuration")
            result = preparation_context.account_constraints(account)
            result["backup_compatible"] = False
        if needs_read:
            media_restore._record_state(db_path, scope, fingerprint, None)
        return result
    except delivery.LeaseLost:
        raise
    except ValueError as exc:
        # A completed identity/grant check which refuses permission is not a
        # transport outage. Do not spend eight retries repeating the same denial.
        if needs_read:
            media_restore._record_state(db_path, scope, fingerprint, "configuration")
        raise MediaRestoreError("Destination authorization requires review: " + _scrub_secrets(str(exc))[:200],
                                retryable=False, category="configuration") from exc
    except Exception as exc:
        if isinstance(exc, media_restore.PrerequisiteUnavailable):
            raise
        category = "configuration" if isinstance(exc, sociamonials_fallback.SociamonialsNotConfigured) else media_restore.classify_failure(exc)
        if needs_read:
            media_restore._record_state(db_path, scope, fingerprint, category)
        raise media_restore.PrerequisiteUnavailable(category, scope_key=scope) from None


def _prepare_target_prerequisites(target: jobs.Target, job: jobs.Job, payload: dict, *, db_path: Path, backup: bool = False):
    from myUtils import publish_admission as admission, publish_media_guard as guard, media_compliance, media_restore
    if delivery.state(payload).get("provider_receipt"):
        return None  # Receipt polls need neither bytes nor create authorization.
    if delivery.has_unresolved_submission(payload):
        raise delivery.DeliveryUnknown("Retained provider/media effects require reconciliation before preparation")
    if 'articleRepurpose' in payload:
        from myUtils.repurpose.dispatch import check as check_teaching_dispatch
        payload['_teaching_dispatch_check'] = lambda: check_teaching_dispatch(payload, target, db_path=db_path)
        payload['_teaching_dispatch_check']()
    account = _resolve_structured_account(target.account_ref, db_path=db_path)
    with jobs._connect(db_path, must_exist=True) as conn:
        row = conn.execute("SELECT workspace_id FROM publish_jobs WHERE id=?", (job.id,)).fetchone()
    payload["_workspace_id"] = row["workspace_id"]
    payload["_restore_account_id"] = getattr(account, "id", None)
    _materialise_publish_media(payload, target, db_path=db_path)
    context = _fresh_preparation_context(account, payload, db_path=db_path, backup=backup)
    verification = context.get("admission_verification")
    backup_verifications = {target.account_ref: verification} if verification is not None else None
    working = _publication_payload(payload)
    working["_preserveApprovedText"] = True
    mode = (delivery.state(payload).get("admission") or {}).get("mode")
    prospective = mode == jobs.PROSPECTIVE_ADMISSION_MODE
    try:
        if prospective:
            sizes = sum(Path(a["local_path"]).stat().st_size for a in working.get("artifacts") or [] if not guard.is_context_artifact(a))
            out_dir = Path(BASE_DIR) / "generated" / "publish-preprocessing" / f"target-{target.id}"
            media_restore.check_capacity(out_dir / "generation", db_path=db_path, input_bytes=sizes)
            parts = guard.prepare_payloads(working, platform=job.platform, account_config=account.config,
                out_dir=out_dir, backup_compatible=context.get("backup_compatible", False), allow_reprepare=False,
                video_limits=context.get("video_limits"), image_limits=context.get("image_limits"))
            sealed = jobs.prepare_owned_publication(target.id, lease_token=target.lease_token,
                source_payload=working, prepared_payloads=parts, db_path=db_path, backup_verifications=backup_verifications)
            installed = jobs.install_owned_publication(sealed, db_path=db_path)
            return PreparationCompleted(tuple(t.id for t in installed), sealed.generation)
        # Scheduled/legacy bytes are immutable: final validation ONLY. Never call
        # ensure_single_payload (even allow_reprepare=False may encode raw media).
        guard.validate_payload(working, platform=job.platform, account_config=account.config,
                               backup_compatible=context.get("backup_compatible", False))
        if (delivery.state(payload).get("admission") or {}).get("version") == admission.ADMISSION_VERSION:
            spec = jobs.JobSpec(platform=job.platform, payload=working, profile_id=job.profile_id,
                               targets=[(target.account_ref, target.file_ref, target.schedule_at)])
            normalized, snapshot = admission.prepare_job_spec(spec, workspace_id=payload["_workspace_id"], db_path=db_path,
                backup_verifications=backup_verifications)
            if not snapshot.ready:
                raise media_restore.PrerequisiteUnavailable("configuration")
            with jobs._transaction(db_path, must_exist=True) as conn:
                if jobs._running_target(conn, target.id, target.lease_token) is None:
                    raise delivery.LeaseLost("Final readiness validation lost its lease")
                admission.validate_admission_snapshot(conn, normalized, snapshot)
        delivery.checkpoint(payload, readiness={"ready": True, "phase": "ready", "stage": "ready", "reasonCode": "ready"})
        payload["_prerequisites_checked"] = True
        return None
    except media_compliance.MediaPreparationError as exc:
        raise MediaRestoreError("Immutable media needs explicit selected preparation/recovery: " + str(exc), retryable=False, category="explicit_recovery_required") from exc



async def default_executor(platform: str, payload: dict, target: jobs.Target) -> None:
    """Default platform router used by the Flask publish endpoints.

    Wraps the per-platform dispatch in
    :func:`myUtils.cookie_storage.decrypted_storage_state` so the uploader
    always sees a plaintext cookie file regardless of how the file is
    stored on disk. When ``SAU_COOKIE_ENCRYPTION_KEY`` is unset the
    helper degrades to a no-op and the uploader uses the canonical path.
    """

    from myUtils.cookie_storage import decrypted_storage_state

    db_path_str = payload.get("_db_path")
    db_path = Path(db_path_str) if db_path_str else None
    # NOTE: ``_db_path`` is intentionally left on ``payload`` for the prepared
    # (API) campaign path below, so a rotated single-use OAuth token is
    # persisted back to the same database the account was read from rather than
    # the process default. ``_run_target`` strips it before persisting the
    # payload, so it never lands in the job row. Browser uploaders receive
    # explicit constructor args and never read it.

    receipt = delivery.state(payload).get("provider_receipt") or {}
    if not receipt and not payload.get("_prerequisites_checked"):
        if db_path is None:
            raise MediaRestoreError("Owned prerequisite context is required before provider dispatch", retryable=False, category="identity")
        job = jobs.get_job(target.job_id, db_path=db_path)
        outcome = await asyncio.to_thread(_prepare_target_prerequisites, target, job, payload, db_path=db_path)
        if isinstance(outcome, (PreparationCompleted, PreparationDeferred)):
            return outcome

    structured_account = _resolve_structured_account(target.account_ref, db_path=db_path)
    canonical_account_file = _resolve_account_path(target.account_ref, db_path=db_path)
    if (payload.get("campaignId") and payload.get("campaignPostId")) or (
            structured_account is not None and platform in PREPARED_PUBLISHER_REGISTRY
            and (payload.get("preparedPart") or payload.get("preparedPost")
                 or any(isinstance(a, dict) and str(a.get("artifact_kind") or "").startswith("prepared_")
                        for a in payload.get("artifacts") or []))):
        if not receipt:
            from myUtils.publish_media_guard import validate_payload
            await asyncio.to_thread(validate_payload, _publication_payload(payload), platform=platform,
                                    account_config=(structured_account.config if structured_account else {}))
        if structured_account is not None and structured_account.cookie_path:
            with decrypted_storage_state(canonical_account_file) as plain_path:
                return await _run_prepared_campaign_upload(
                    platform,
                    payload,
                    target,
                    account=structured_account,
                    account_file=plain_path,
                )
        else:
            return await _run_prepared_campaign_upload(
                platform,
                payload,
                target,
                account=structured_account,
                account_file=None,
            )
        return

    if platform == "twitter":
        thread_file_paths = _prepared_artifact_local_paths(payload)
        if not thread_file_paths:
            raise MediaRestoreError("Twitter dispatch requires a complete validated local media set", retryable=False)
        thread_file_paths = _prepared_artifact_local_paths(payload)
        with decrypted_storage_state(canonical_account_file) as plain_path:
            observer = _make_x_browser_delivery_observer(payload, target)
            if observer is None:
                _begin_native_publish_attempt(payload, target)
                delivery.starting(payload, operation=f"browser-publish:{platform}")
            await _run_platform_upload(
                platform,
                payload,
                target,
                account_file=plain_path,
                thread_file_paths=thread_file_paths,
                delivery_observer=observer,
            )
            if observer is not None and delivery.state(payload).get("stage") != "browser_delivered":
                raise delivery.DeliveryUnknown("Instrumented X browser did not confirm the complete thread")
        return

    file_paths = _prepared_artifact_local_paths(payload)
    if not file_paths:
        raise MediaRestoreError("Dispatch requires validated local media", retryable=False)
    file_path = file_paths[0]
    with decrypted_storage_state(canonical_account_file) as plain_path:
        delivery.starting(payload, operation=f"browser-publish:{platform}")
        await _run_platform_upload(
            platform,
            payload,
            target,
            account_file=plain_path,
            file_path=file_path,
        )


def run_worker_drain(executor: Executor | None = None,
                     *, config: WorkerConfig | None = None,
                     db_path: Path | None = None,
                     prep_runner: PrepRunnerCallable | bool | None = None) -> None:
    """Block-until-empty helper used by the synchronous Flask path.

    Prep is enabled by default: this drain is the off-request worker that takes
    over campaigns left in ``preparing``. Pass ``prep_runner=False`` to disable
    it and only drain already-queued publish targets.
    """

    if prep_runner is None:
        prep_runner = campaign_prep.make_default_prep_runner()
    elif prep_runner is False:
        prep_runner = None
    worker = PublishWorker(
        executor or default_executor,
        config=config,
        db_path=db_path,
        prep_runner=prep_runner,
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
    os.environ["SAU_PROCESS_ROLE"] = "worker"

    config = WorkerConfig(
        poll_interval=args.poll_interval,
        batch_size=args.batch_size,
        max_concurrent=args.max_concurrent,
        retry=RetryPolicy(max_attempts=args.max_attempts),
    )
    worker = PublishWorker(
        default_executor,
        config=config,
        db_path=args.db_path,
        prep_runner=campaign_prep.make_default_prep_runner(),
    )
    runtime.start_worker_maintenance(worker._db_path)

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
