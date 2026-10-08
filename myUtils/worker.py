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
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from utils.conf_defaults import BASE_DIR
from myUtils import jobs
from myUtils import campaign_prep
from myUtils import campaigns as campaign_store
from myUtils import media_remote_storage
from myUtils import media_pipeline
from myUtils import profiles as profile_registry
from myUtils import prepared_publishers
from myUtils import content_rules
from myUtils import sociamonials_fallback
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


TARGET_EXECUTION_TIMEOUT_SECONDS = float(
    os.environ.get("SAU_TARGET_TIMEOUT_SECONDS", "1200") or 1200
)


def _x_direct_publish_enabled() -> bool:
    """Whether the direct X (API/browser) path should run before the fallback.

    The X developer account is out of credits, so direct X media uploads fail
    deterministically and the cookie/browser path can hang. When the
    Sociamonials fallback is on and this is unset, skip the direct path and
    hand X straight to Sociamonials. Set ``SAU_X_DIRECT_PUBLISH=1`` after
    topping up credits to restore direct-first behaviour.
    """
    raw = str(os.environ.get("SAU_X_DIRECT_PUBLISH", "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return not sociamonials_fallback.is_enabled()


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
    return cleaned


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

    def __post_init__(self) -> None:
        if self.retry is None:
            self.retry = RetryPolicy()
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
    # Base doubles per consecutive failure up to the cap; once the failure
    # count crosses the threshold the account is flagged for manual reconnect
    # (``_needsReconnect``) and skipped entirely until a human re-authorises it.
    _MAINTENANCE_BACKOFF_BASE_SECONDS: int = 30 * 60      # 30 min after 1st failure
    _MAINTENANCE_BACKOFF_CAP_SECONDS: int = 6 * 3600      # cap at 6 h
    _MAINTENANCE_RECONNECT_THRESHOLD: int = 5             # give up → flag for human

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
            await self._tick()
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
                await asyncio.gather(*pending, return_exceptions=True)
            self._tasks.difference_update(
                {task for task in self._tasks if task.done()}
            )
            self._prep_tasks.difference_update(
                {task for task in self._prep_tasks if task.done()}
            )


    def _has_pending(self) -> bool:
        # Only targets that are due *now* count. A target scheduled for later
        # must not keep drain() spinning; the backend's publish scheduler
        # re-triggers a drain once it becomes claimable.
        if jobs.has_claimable_targets(db_path=self._db_path):
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
            meta_user_token = str(config.get("metaUserAccessToken") or "").strip()
            if not meta_user_token:
                return False
            access_token = str(config.get("accessToken") or "").strip()
            if not access_token:
                return True
            expires_at = self._parse_iso_datetime(
                str(config.get("metaUserAccessTokenExpiresAt") or config.get("accessTokenExpiresAt") or "")
            )
            if expires_at is None:
                # No expiry recorded (legacy/imported connect) — refresh once to
                # establish one, otherwise the 60-day long-lived token silently
                # dies because nothing ever renews it. Back-off keeps a failing
                # refresh from looping.
                return True
            return expires_at <= (self._utc_now() + timedelta(seconds=skew))

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
                access_token = str(config.get("accessToken") or "").strip()
                if not access_token:
                    return
                from myUtils import threads_auth
                refreshed = threads_auth.refresh_long_lived_token(access_token=access_token)
                config.update({
                    "accessToken": str(refreshed.get("access_token") or access_token),
                    "accessTokenUpdatedAt": now,
                    "lastAutoRefreshAt": now,
                })
                expires_in = refreshed.get("expires_in")
                if expires_in:
                    config["accessTokenExpiresAt"] = prepared_publishers._token_expiry_from_payload(
                        {"expires_in": expires_in}, "expires_in"
                    )

            elif platform in {"facebook", "instagram"}:
                meta_user_token = str(config.get("metaUserAccessToken") or "").strip()
                if not meta_user_token:
                    return
                from myUtils import meta_auth
                if platform == "instagram":
                    refreshed = meta_auth.refresh_instagram_user_token(access_token=meta_user_token)
                else:
                    refreshed = meta_auth.exchange_for_long_lived_token(access_token=meta_user_token)
                new_user_token = str(refreshed.get("access_token") or meta_user_token)
                config["metaUserAccessToken"] = new_user_token
                # Persist a truthful expiry. Some Meta apps issue never-expiring
                # tokens (debug_token ``expires_at == 0``) and omit ``expires_in``
                # from every response; the old 60-day model then left a stale/past
                # expiry that read as "expired / reconnect required" forever.
                resolved_expiry = meta_auth.resolve_access_token_expiry(
                    access_token=new_user_token,
                    expires_in=refreshed.get("expires_in"),
                )
                config["metaUserAccessTokenExpiresAt"] = resolved_expiry["expires_at_iso"]
                config["metaTokenExpiryMode"] = resolved_expiry["mode"]
                config["metaTokenExpiresAtEpoch"] = resolved_expiry["expires_at_epoch"]

                # Re-derive the page-level access_token from the freshly-refreshed
                # user token. Page tokens inherit the user token's lifecycle, so
                # without this publishing breaks ~60 days after connect.
                try:
                    pages_payload = meta_auth.fetch_managed_pages(access_token=new_user_token)
                    pages = pages_payload.get("data", []) if isinstance(pages_payload, dict) else []
                    if isinstance(pages, list) and pages:
                        target_page = None
                        if platform == "facebook":
                            wanted = str(config.get("pageId") or "").strip()
                            if wanted:
                                target_page = next(
                                    (p for p in pages if str(p.get("id") or "") == wanted),
                                    None,
                                )
                            if target_page is None:
                                target_page = pages[0]
                        else:  # instagram
                            wanted_ig = str(config.get("igUserId") or "").strip()
                            for p in pages:
                                ig = p.get("instagram_business_account") if isinstance(p, dict) else None
                                if not isinstance(ig, dict):
                                    continue
                                if wanted_ig and str(ig.get("id") or "") == wanted_ig:
                                    target_page = p
                                    break
                                if not wanted_ig and target_page is None:
                                    target_page = p
                        if target_page is not None:
                            page_token = str(target_page.get("access_token") or "").strip()
                            if page_token:
                                config["accessToken"] = page_token
                            if platform == "instagram" and target_page.get("id"):
                                config["pageId"] = str(target_page["id"])
                except Exception as exc:
                    _logger.warning(
                        f"worker self-maintenance: failed to re-derive page token "
                        f"for {platform} account id={account.id}: {exc}"
                    )

                config["accessTokenUpdatedAt"] = now
                config["lastAutoRefreshAt"] = now

            elif platform == "twitter":
                refresh_token = str(config.get("refreshToken") or "").strip()
                if not refresh_token or profile_registry.effective_auth_type(
                    config, account.auth_type, "twitter"
                ) != "api":
                    return

                def _persist_twitter(updated_config: dict) -> None:
                    profile_registry.update_account(
                        account.id,
                        config=updated_config,
                        auth_type="oauth",
                        status=1,
                        db_path=self._db_path,
                    )

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

            # Success: clear any prior failure / back-off / reconnect markers so
            # the account returns to the normal refresh cadence.
            for _key in (
                "_maintenanceFailures", "_nextMaintenanceAttemptAt",
                "_lastMaintenanceError", "_lastMaintenanceAttemptAt",
                "_needsReconnect", "_reconnectAlertedAt",
            ):
                config.pop(_key, None)
            # Persist updated config back to the database
            profile_registry.update_account(
                account.id,
                config=config,
                auth_type="oauth",
                status=1,
                db_path=self._db_path,
            )
            _logger.info(f"worker self-maintenance: refreshed {platform} account id={account.id}")

        except Exception as exc:
            self._handle_refresh_failure(account, config, exc)

    def _handle_refresh_failure(
        self, account: profile_registry.Account, config: dict, exc: Exception
    ) -> None:
        """Record a failed refresh: bump the failure count, schedule an
        exponential back-off, and after ``_MAINTENANCE_RECONNECT_THRESHOLD``
        consecutive failures flag the account for manual reconnect and alert
        the operator (once). All state lives in ``config_json`` so no schema
        migration is needed."""
        now_dt = self._utc_now()
        now_iso = now_dt.isoformat(timespec="seconds")
        # Base the failure state on the account's CURRENT row, not the snapshot
        # read before the refresh attempt. A single-use refresh token is rotated
        # by the token endpoint, so if a publish-path refresh succeeded while
        # this attempt was in flight, writing our stale snapshot back would
        # discard the fresh token and strand the account permanently. Re-read
        # and only overlay the failure markers.
        try:
            latest = profile_registry.get_account(account.id, db_path=self._db_path)
            merged = dict(latest.config or {})
        except Exception:  # noqa: BLE001 - fall back to the snapshot we have
            merged = dict(config)
        failures = int(merged.get("_maintenanceFailures") or 0) + 1
        backoff = min(
            self._MAINTENANCE_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)),
            self._MAINTENANCE_BACKOFF_CAP_SECONDS,
        )
        merged["_maintenanceFailures"] = failures
        merged["_lastMaintenanceError"] = str(exc)[:300]
        merged["_lastMaintenanceAttemptAt"] = now_iso
        merged["_nextMaintenanceAttemptAt"] = (
            now_dt + timedelta(seconds=backoff)
        ).isoformat(timespec="seconds")

        credential_rejected = False
        if account.platform == "twitter":
            # A rejected X refresh token is terminal: no number of retries can
            # recover it, so surface "reconnect required" immediately instead of
            # waiting for the (much larger) failure threshold.
            credential_rejected = prepared_publishers._twitter_refresh_requires_reconnect(exc)
        needs_reconnect = (
            failures >= self._MAINTENANCE_RECONNECT_THRESHOLD or credential_rejected
        )
        should_alert = needs_reconnect and not merged.get("_reconnectAlertedAt")
        if needs_reconnect:
            merged["_needsReconnect"] = True
        if should_alert:
            merged["_reconnectAlertedAt"] = now_iso

        try:
            profile_registry.update_account(
                account.id, config=merged, db_path=self._db_path,
            )
        except Exception as persist_exc:  # noqa: BLE001
            _logger.error(
                f"worker self-maintenance: could not persist failure state for "
                f"account id={account.id}: {persist_exc!r}"
            )

        _logger.warning(
            f"worker self-maintenance: failed to refresh {account.platform} "
            f"{account.nickname or account.account_name} (account:{account.id}) "
            f"(attempt {failures}, retry in {backoff}s"
            f"{', FLAGGED for reconnect' if needs_reconnect else ''}): {exc}"
        )
        if should_alert:
            self._alert_needs_reconnect(account, failures, exc)

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
            accounts = profile_registry.list_accounts(enabled=True, db_path=self._db_path)
        except Exception as exc:
            _logger.warning(f"worker self-maintenance: could not list accounts: {exc}")
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
        # Reap finished tasks first so their account_refs free up.
        done = {task for task in self._tasks if task.done()}
        for task in done:
            self._tasks.discard(task)
            # Surface unhandled task errors in the log; the target itself has
            # already been transitioned by the task body.
            exc = task.exception()
            if exc is not None:
                _logger.error(f"worker task crashed: {exc!r}")

        # Reap finished prep tasks the same way. ``_run_campaign_prep`` owns its
        # own campaign transition, so here we only surface a crash.
        done_preps = {task for task in self._prep_tasks if task.done()}
        for task in done_preps:
            self._prep_tasks.discard(task)
            exc = task.exception()
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
                requeued = jobs.requeue_stale_running(
                    max_attempts=self._config.retry.max_attempts,
                    db_path=self._db_path,
                )
                if requeued:
                    _logger.warning(
                        f"worker requeued {requeued} stale running target(s)"
                    )
            except Exception:
                _logger.exception("stale-running sweep failed")

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
        )
        for target in claimed:
            task = asyncio.create_task(self._run_target(target))
            self._tasks.add(task)

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
        """Run one claimed campaign's prep + finalize, then record the outcome."""
        _logger.info(
            f"campaign {campaign.id} claimed for prep (owner={self._prep_owner})"
        )
        try:
            # ffmpeg is blocking; keep it off the event loop. A runner may also
            # be an async callable, in which case to_thread returns the
            # coroutine and we await it here.
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
            transitioned = campaign_store.finish_campaign_prep(
                campaign.id,
                owner=self._prep_owner,
                status=(
                    campaign_store.CAMPAIGN_PUBLISHING
                    if jobs
                    else campaign_store.CAMPAIGN_NEEDS_REVIEW
                ),
                prepared_at=now,
                published_at=now if jobs else None,
                last_error=None if jobs else "No publishable posts queued",
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
            try:
                campaign_store.finish_campaign_prep(
                    campaign.id,
                    owner=self._prep_owner,
                    status=campaign_store.CAMPAIGN_NEEDS_REVIEW,
                    last_error=f"prep failed: {exc}",
                    db_path=self._db_path,
                )
            except Exception:
                _logger.exception(
                    f"campaign {campaign.id} could not record its prep failure"
                )

    async def _run_target(self, target: jobs.Target) -> None:
        job = jobs.get_job(target.job_id, db_path=self._db_path)
        payload_state = {"payload": dict(job.payload)}
        account_name = ""
        try:
            account = _resolve_structured_account(target.account_ref, db_path=self._db_path)
            if account is not None:
                account_name = str(account.nickname or account.account_name or "").strip()
        except Exception:  # noqa: BLE001 — logging must not block publishing
            pass
        log = bind_job_logger(
            job_id=target.job_id,
            target_id=target.id,
            platform=job.platform,
            account_ref=target.account_ref,
            account_name=account_name or None,
            attempt=target.attempts,
        )
        log.info("target claimed; starting execution")

        try:
            async with self._concurrency.slot(target.account_ref):
                # Inject db_path into payload so executors can resolve remote files
                payload = {
                    **payload_state["payload"],
                    "_db_path": str(self._db_path),
                    "_telegramDeliveryKey": str(target.id),
                }
                # Stored artifact URLs predate the upload-side percent-encoding
                # fix, so some rows still carry raw spaces (e.g.
                # ".../SFW 20260821094430455_pub_pub.mp4"). A raw space is not a
                # valid URI: URL-fetching platforms and the fallback reject it
                # while the encoded form serves 200. Normalise once here, at the
                # single point the payload is assembled, so every platform path
                # and the fallback see a valid URL regardless of the row's age.
                if isinstance(payload.get("artifacts"), list):
                    for artifact in payload["artifacts"]:
                        if not isinstance(artifact, dict):
                            continue
                        url = artifact.get("public_url")
                        if url:
                            artifact["public_url"] = _normalise_artifact_url(
                                str(url)
                            )
                guard_error = _content_guard_error(payload, target, self._db_path)
                if guard_error:
                    reason = f"[content-guard] {guard_error}"
                    log.error(f"refusing to publish: {reason}")
                    if jobs.mark_target_failed(target.id, reason, db_path=self._db_path):
                        self._alert_publish_failure(target, reason)
                    self._maybe_close_job_sink(target.job_id)
                    return
                if job.platform == "twitter" and not _x_direct_publish_enabled():
                    # X is carried by Sociamonials (its own OAuth connection),
                    # and the direct path is currently guaranteed to fail (out
                    # of API credits) or to hang (cookie browser). Go straight
                    # to the fallback instead of burning the retry budget.
                    log.info(
                        "X direct publish disabled; using Sociamonials fallback"
                    )
                    if await self._try_sociamonials_fallback(
                        target, "X direct publish disabled", log
                    ):
                        self._maybe_close_job_sink(target.job_id)
                        return
                    message = (
                        "X direct publish disabled and the Sociamonials fallback "
                        "failed"
                    )
                    if jobs.mark_target_failed(
                        target.id, message, db_path=self._db_path
                    ):
                        log.error(f"target failed: {message}")
                        self._alert_publish_failure(target, message)
                    self._maybe_close_job_sink(target.job_id)
                    return
                result = self._executor(job.platform, payload, target)
                if inspect.isawaitable(result):
                    outcome = await asyncio.wait_for(
                        result, timeout=TARGET_EXECUTION_TIMEOUT_SECONDS
                    )
                else:
                    outcome = result
                updated_payload = {
                    key: value for key, value in payload.items()
                    if key not in {"_db_path", "_telegramDeliveryKey"}
                }
                if updated_payload != job.payload:
                    payload_state["payload"] = updated_payload
                    jobs.update_job_payload(
                        target.job_id, updated_payload, db_path=self._db_path
                    )
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
            if "payload" in locals():
                updated_payload = {
                    key: value for key, value in payload.items()
                    if key not in {"_db_path", "_telegramDeliveryKey"}
                }
                if updated_payload != job.payload:
                    payload_state["payload"] = updated_payload
                    try:
                        jobs.update_job_payload(
                            target.job_id, updated_payload, db_path=self._db_path
                        )
                    except Exception:
                        log.exception("could not persist target recovery state")
            await self._handle_failure(target, exc, log)
        else:
            outcome = locals().get("outcome")
            if isinstance(outcome, dict) and outcome.get("retryableFailure"):
                error = str(outcome.get("error") or "publisher reported partial delivery")
                jobs.update_job_payload(
                    target.job_id, payload_state["payload"], db_path=self._db_path
                )
                await self._handle_failure(
                    target,
                    prepared_publishers.PreparedPublishError(
                        error,
                        details=outcome.get("details") if isinstance(outcome.get("details"), dict) else None,
                        retryable=True,
                    ),
                    log,
                )
                return
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
        error_details = getattr(exc, "details", None)
        if isinstance(error_details, dict) and error_details:
            message += f" | details={json.dumps(error_details, ensure_ascii=False, separators=(',', ':'))}"
        attempts = target.attempts  # already incremented when claimed
        if getattr(exc, "retryable", True) is False:
            # A permanent failure on ANY platform may still be deliverable by
            # Sociamonials, which holds its own OAuth connections. Let the
            # fallback decide: an unmapped account or bad media makes it return
            # False and the target is marked failed exactly as before, so this
            # is not a silent re-route.
            if await self._try_sociamonials_fallback(target, message, log):
                self._maybe_close_job_sink(target.job_id)
                return
            transitioned = jobs.mark_target_failed(
                target.id, message, db_path=self._db_path
            )
            if transitioned:
                log.error(f"target failed without retry: {message}")
                self._alert_publish_failure(target, message)
            self._maybe_close_job_sink(target.job_id)
            return
        if attempts >= self._config.retry.max_attempts:
            # The retry budget is exhausted. Before declaring the target dead,
            # hand it to Sociamonials, which already holds OAuth connections for
            # most of the same brand accounts. This is what turns the recurring
            # deterministic failures (X media-upload 402 / needs-reconnect,
            # Bluesky 413) into delivered posts instead of permanent failures.
            # Opt-in via SAU_SOCIAMONIALS_FALLBACK; see sociamonials_fallback.
            if await self._try_sociamonials_fallback(target, message, log):
                self._maybe_close_job_sink(target.job_id)
                return
            transitioned = jobs.mark_target_failed(
                target.id, message, db_path=self._db_path
            )
            if transitioned:
                log.error(
                    f"target failed permanently after {attempts} attempts: {message}"
                )
                # Only a genuine permanent transition alerts. ``transitioned``
                # is False when the parent job was cancelled mid-run, and the
                # retry branch below is deliberately silent — operators should
                # only hear about targets that will never go out on their own.
                self._alert_publish_failure(target, message)
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

    async def _try_sociamonials_fallback(
        self, target: jobs.Target, message: str, log, *, only_platform: str | None = None
    ) -> bool:
        """Publish an exhausted target through Sociamonials. Best-effort.

        Returns ``True`` only when the fallback actually delivered the post and
        the target was transitioned to succeeded. Every other outcome — flag
        off, no mapped profile, media unavailable, API error — returns ``False``
        so the caller falls through to the normal permanent-failure path. This
        method must never raise into the worker loop.

        ``only_platform`` restricts the attempt to one platform; the caller
        uses it to let a permanently-failed X target fall back while leaving
        permanent content failures on other platforms alone.
        """

        if not sociamonials_fallback.is_enabled():
            return False

        try:
            job = jobs.get_job(target.job_id, db_path=self._db_path)
        except Exception:  # noqa: BLE001 — a missing job only costs context
            return False
        if only_platform and str(job.platform or "").strip().lower() != only_platform:
            return False
        payload = dict(job.payload or {})
        payload["_db_path"] = str(self._db_path)

        # ``_resolve_structured_account`` raises LookupError for an account row
        # that no longer exists (a deleted account whose targets are still
        # queued). That is a "cannot fall back", not a crash: the caller must
        # still be allowed to mark the target permanently failed.
        try:
            account = _resolve_structured_account(
                target.account_ref, db_path=self._db_path
            )
        except Exception as exc:  # noqa: BLE001 — never raise into the loop
            log.info(
                f"sociamonials fallback skipped: account lookup failed for "
                f"{target.account_ref} ({_scrub_secrets(str(exc))})"
            )
            return False
        if account is None:
            log.info("sociamonials fallback skipped: account could not be resolved")
            return False

        profile_settings = None
        try:
            if job.profile_id is not None:
                profile_settings = profile_registry.get_profile(
                    job.profile_id, db_path=self._db_path
                ).settings
        except Exception:  # noqa: BLE001 — settings are optional
            profile_settings = None

        # Restore any offloaded artifacts before handing them to the fallback.
        # The direct path normally does this in ``default_executor``, but the
        # X-skip shortcut in ``_run_target`` never runs the executor, and a
        # target whose direct path failed may have had only a partial restore.
        # Without this the fallback dies with "local media missing" even though
        # the bytes are one rclone call away.
        try:
            await asyncio.to_thread(
                _ensure_artifact_paths_local, payload, db_path=self._db_path
            )
        except Exception as exc:  # noqa: BLE001 — a restore miss is not a crash
            log.info(
                "sociamonials fallback media restore failed: "
                f"{_scrub_secrets(str(exc))}"
            )

        media_paths = _fallback_media_paths(payload)

        try:
            result = await asyncio.to_thread(
                sociamonials_fallback.publish_via_sociamonials,
                platform=job.platform,
                account=account,
                payload=payload,
                media_paths=media_paths,
                target_id=target.id,
                settings=profile_settings,
            )
        except sociamonials_fallback.SociamonialsNotConfigured as exc:
            log.info(f"sociamonials fallback unavailable: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 — never raise into the loop
            log.warning(
                f"sociamonials fallback failed; the target will be marked "
                f"permanently failed. original error: {message} | "
                f"fallback error: {_scrub_secrets(str(exc))}"
            )
            return False

        delivery_state = str(result.get("delivery_state") or "").strip().lower()
        if delivery_state == "failed":
            # Sociamonials accepted the post and then failed to deliver it (its
            # own platform hand-off could not fetch the media). Treating the
            # submit as success hid that the post never landed, so record the
            # failure. The target is still ``running`` here, which is the state
            # mark_target_failed requires.
            reason = str(result.get("delivery_error") or "").strip()
            message = (
                "Sociamonials accepted the post but delivery failed"
                + (f": {reason}" if reason else "")
            )
            transitioned = jobs.mark_target_failed(
                target.id, message, db_path=self._db_path
            )
            if transitioned:
                log.error(
                    f"sociamonials fallback delivered nothing (post_id="
                    f"{result.get('post_id')}, network={result.get('network')}); "
                    f"marked failed: {_scrub_secrets(message)}"
                )
                self._alert_publish_failure(target, message)
            return True

        transitioned = jobs.mark_target_success(target.id, db_path=self._db_path)
        if transitioned:
            note = (
                "" if delivery_state in {"delivered", ""}
                else f" (delivery {delivery_state or 'unconfirmed'})"
            )
            log.info(
                f"delivered via Sociamonials fallback (post_id={result.get('post_id')}, "
                f"network={result.get('network')}, status={result.get('status')}){note}; "
                f"the direct publish had failed with: {message}"
            )
        else:
            log.info(
                "sociamonials fallback delivered, but the target was already "
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
        """Alert the operator that a target has permanently failed.

        Best-effort by contract: the target already transitioned in the DB, so
        an alerting problem must never propagate back into the worker. Every
        failure path is swallowed and logged. The alert carries the ids,
        platform, account, profile, attempt count and error needed to act, and
        a deep link when one can be built without guessing a hostname.
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

            lines = [
                "A publish target exhausted its retry budget and failed "
                "permanently. It will not be retried automatically.",
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

            from myUtils import ops_alerts

            ops_alerts.send_ops_alert(
                subject=(
                    f"[SAU] Publish failed: {platform} target #{target.id} "
                    f"(job #{target.job_id})"
                ),
                body="\n".join(lines),
            )
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
        if str(artifact.get("artifact_kind") or "") in {"watermarked_image", "watermarked_video"}:
            continue
        candidate = str(artifact.get("local_path") or "").strip()
        if not candidate:
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
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class MediaRestoreError(RuntimeError):
    """A required publish media file could not be restored locally."""


def _resolve_file_path(file_ref: str, *, db_path: Path | None = None) -> Path:
    candidate = Path(file_ref)
    if candidate.is_absolute() and candidate.is_file():
        return candidate

    if file_ref.startswith("uploads/"):
        local_path = Path(BASE_DIR) / file_ref
        storage_ref = file_ref
    else:
        relative = file_ref.removeprefix("videoFile/")
        local_path = Path(BASE_DIR) / "videoFile" / relative
        storage_ref = file_ref
    if local_path.is_file():
        return local_path
    if db_path is not None:
        downloaded = _try_download_from_storage(storage_ref, db_path)
        if downloaded is not None and downloaded.is_file():
            return downloaded
    raise MediaRestoreError(f"Required media is unavailable locally or in storage: {file_ref}")


def _try_download_from_storage(file_ref: str, db_path: Path) -> Path | None:
    """Look up file_ref in file_records. If stored remotely, download to local."""
    import sqlite3
    try:
        if file_ref.startswith("uploads/"):
            lookup_refs = (file_ref,)
        else:
            relative = file_ref.removeprefix("videoFile/")
            lookup_refs = (file_ref, relative)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = None
            for lookup_ref in lookup_refs:
                row = conn.execute(
                    "SELECT storage_key, storage_backend_id FROM file_records WHERE file_path = ? AND storage_key IS NOT NULL",
                    (lookup_ref,),
                ).fetchone()
                if row:
                    break
        if not row:
            return None
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            backend = conn.execute(
                "SELECT * FROM storage_backends WHERE id = ?", (row["storage_backend_id"],)
            ).fetchone()
        if not backend:
            return None
        # Mirror file_records' on-disk layout: paths under uploads/ live in
        # BASE_DIR/uploads, everything else in BASE_DIR/videoFile — the same
        # roots offload_to_drive.sh moves and the storage_backends endpoint
        # points at.
        if file_ref.startswith("uploads/"):
            local_path = Path(BASE_DIR) / file_ref
        else:
            local_path = Path(BASE_DIR) / "videoFile" / file_ref.removeprefix("videoFile/")
        if local_path.is_file():
            return local_path
        _download_atomically(
            local_path,
            lambda temporary: media_remote_storage.download_from_backend(
                dict(backend), row["storage_key"], temporary
            ),
        )
        _logger.info(
            f"restored {file_ref} from {backend['provider']} storage"
        )
        return local_path
    except Exception as exc:
        # A known remote record that cannot be restored is a publishing failure,
        # not a missing file that should be delegated to the platform uploader.
        _logger.warning(
            f"could not restore {file_ref} from remote storage: {exc!r}"
        )
        raise MediaRestoreError(f"Remote restore failed for {file_ref}: {exc}") from exc


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


def _ensure_artifact_paths_local(payload: dict, *, db_path: Path) -> None:
    """Download missing artifact files from remote storage before publish.

    This is the read half of the offload loop: ``offload_to_drive.sh`` moves
    files the queue no longer needs onto Drive, and this restores them at
    claim time. Every failure path logs — a silent miss here surfaces only
    as the uploader's generic "file not found" halfway through a publish.
    """
    import sqlite3

    def _generated_record_by_name(path: Path) -> sqlite3.Row | None:
        """Find one unambiguous generated mapping from older offload records."""
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT storage_key,storage_backend_id,storage_cdn_url,file_path "
                "FROM file_records WHERE filename=? AND storage_key IS NOT NULL "
                "AND storage_backend_id IS NOT NULL",
                (path.name,),
            ).fetchall()
        mappings = {(row["storage_key"], row["storage_backend_id"]) for row in rows}
        return rows[0] if len(mappings) == 1 else None

    def _record_for(artifact: dict, path: Path) -> sqlite3.Row | None:
        source_id = artifact.get("source_id") or artifact.get("source_file_record_id")
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            raw = str(path).replace("\\\\", "/")
            # Generated artifacts have their own Drive object and mapping; that
            # exact artifact record must win over the original source record.
            for marker in ("/generated/", "generated/"):
                if marker in raw:
                    rel = raw.split(marker, 1)[1]
                    generated_refs = ("generated/" + rel, "/app/generated/" + rel)
                    for file_ref in generated_refs:
                        generated_row = conn.execute(
                            "SELECT storage_key, storage_backend_id, storage_cdn_url, file_path "
                            "FROM file_records WHERE file_path=?",
                            (file_ref,),
                        ).fetchone()
                        if generated_row:
                            # The transformed artifact's own Drive row outranks
                            # the original upload's source_file_record_id.
                            return generated_row
                    campaign_row = conn.execute(
                        "SELECT remote_path AS storage_key, storage_backend_id, public_url AS storage_cdn_url, local_path AS file_path "
                        "FROM campaign_artifacts WHERE local_path=? AND remote_path IS NOT NULL AND storage_backend_id IS NOT NULL",
                        (str(path),),
                    ).fetchone()
                    if campaign_row:
                        return campaign_row
                    # Never substitute original source bytes into a generated target.
                    return None
            if source_id:
                row = conn.execute(
                    "SELECT storage_key, storage_backend_id, storage_cdn_url, file_path FROM file_records WHERE id = ?",
                    (int(source_id),),
                ).fetchone()
                if row:
                    return row
            # No usable source id: fall back to the path convention shared
            # with offload_to_drive.sh — the segment after /videoFile/ or
            # /uploads/ is file_records.file_path.
            raw = str(path)
            for marker in ("/videoFile/", "/uploads/"):
                if marker in raw:
                    rel = raw.split(marker, 1)[1]
                    if marker == "/uploads/":
                        rel = "uploads/" + rel
                    select = (
                        "SELECT storage_key, storage_backend_id, storage_cdn_url, file_path "
                        "FROM file_records WHERE file_path = ?"
                    )
                    row = conn.execute(select, (rel,)).fetchone()
                    if row is None:
                        # file_records use two conventions for the same media: a
                        # bare media-relative path (_library/..., 307 rows) and
                        # the same path carrying the videoFile/ prefix
                        # (videoFile/_batch1/..., 88 rows). Only the bare form was
                        # looked up, so every record stored with the prefix
                        # answered "no file record" and its targets failed
                        # permanently with MediaRestoreError even though the bytes
                        # were sitting on Drive and registered.
                        row = conn.execute(select, ("videoFile/" + rel,)).fetchone()
                    return row
        return None

    for artifact in payload.get("artifacts", []) or []:
        local_path = artifact.get("local_path")
        if not local_path:
            continue
        p = Path(local_path)
        if p.exists():
            continue
        generated_root = media_pipeline.GENERATED_MEDIA_ROOT.resolve()
        try:
            is_generated_artifact = p.resolve().is_relative_to(generated_root)
        except (OSError, ValueError):
            is_generated_artifact = False
        try:
            row = _record_for(artifact, p)
            if row is None and is_generated_artifact:
                row = _generated_record_by_name(p)
        except Exception as exc:
            raise MediaRestoreError(f"Could not find artifact source record for {local_path}: {exc}") from exc
        try:
            if row is None and is_generated_artifact:
                row = {"storage_key": None, "storage_backend_id": None, "storage_cdn_url": None, "file_path": str(p)}
            if row is None:
                raise MediaRestoreError(
                    f"Artifact {local_path} is missing locally and has no file record"
                )

            p.parent.mkdir(parents=True, exist_ok=True)
            downloaded = False
            last_error: Exception | None = None

            # Try 1: Download via the backend that stored it (S3 or rclone)
            if row["storage_key"] and row["storage_backend_id"]:
                try:
                    with sqlite3.connect(db_path) as conn:
                        conn.row_factory = sqlite3.Row
                        backend = conn.execute(
                            "SELECT * FROM storage_backends WHERE id = ?", (row["storage_backend_id"],)
                        ).fetchone()
                    if backend:
                        _download_atomically(
                            p,
                            lambda temporary: media_remote_storage.download_from_backend(
                                dict(backend), row["storage_key"], temporary
                            ),
                        )
                        downloaded = True
                except Exception as exc:
                    last_error = exc
                    _logger.warning(
                        f"backend restore failed for {local_path}: {exc!r}"
                    )

            # Try 2: Restore generated artifacts from their registered Drive
            # mapping first; only then use a validated public HTTPS fallback.
            if not downloaded and is_generated_artifact:
                public_url = _normalise_artifact_url(
                    str(artifact.get("public_url") or "")
                )
                if _public_https_url(public_url):
                    try:
                        _download_public_artifact(public_url, p)
                        downloaded = True
                    except Exception as exc:
                        last_error = exc
                        _logger.warning(f"generated public URL restore failed for {local_path}: {exc!r}")
            # Try 3: Download via public CDN URL (R2 public bucket)
            if not downloaded and row["storage_cdn_url"]:
                try:
                    _download_public_artifact(row["storage_cdn_url"], p)
                    downloaded = True
                except Exception as exc:
                    last_error = exc
                    _logger.warning(
                        f"CDN restore failed for {local_path}: {exc!r}"
                    )

            # Try 3: Check media_assets table for R2 public_url
            if not downloaded:
                try:
                    with sqlite3.connect(db_path) as conn:
                        conn.row_factory = sqlite3.Row
                        asset = conn.execute(
                            "SELECT public_url FROM media_assets WHERE local_original_path LIKE ? AND public_url IS NOT NULL",
                            (f"%{p.name}%",),
                        ).fetchone()
                    if asset and asset["public_url"]:
                        _download_public_artifact(asset["public_url"], p)
                        downloaded = True
                except Exception as exc:
                    last_error = exc

            if not downloaded:
                if is_generated_artifact and not _public_https_url(str(artifact.get("public_url") or "")):
                    raise MediaRestoreError(
                        f"Generated artifact is missing and has no safe public HTTPS recovery URL: {local_path}"
                    )
                raise MediaRestoreError(
                    f"Could not restore artifact {local_path} "
                    f"(file_record={row['file_path']}): {last_error or 'no usable storage source'}"
                )

        except MediaRestoreError:
            raise
        except Exception as exc:
            _logger.exception(f"artifact restore failed for {local_path}")
            raise MediaRestoreError(f"Artifact restore failed for {local_path}: {exc}") from exc


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
    db_path = None
    raw = (payload or {}).get("_db_path")
    if raw:
        db_path = Path(raw)
    kwargs = {"db_path": db_path} if db_path is not None else {}
    profile_registry.update_account(
        account.id, config=updated_config, auth_type="oauth", **kwargs
    )


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
    if payload.get("campaignId") and not artifacts:
        raise ValueError("Prepared Twitter campaign has no media artifacts")
    file_paths = _prepared_artifact_local_paths(payload)
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
    else:
        if account is None:
            raise ValueError("Prepared Twitter API publish requires a structured account")
        if payload.get("campaignId") and not artifacts:
            raise ValueError("Prepared Twitter campaign has no media artifacts")
        if any(not artifact.get("local_path") for artifact in artifacts):
            raise ValueError("Prepared Twitter media artifacts are missing local paths")
        result = await asyncio.to_thread(
            prepared_publishers.publish_twitter_sync, account, payload
        )
        updated_config = result.get('updated_config') if isinstance(result, dict) else None
        _persist_rotated_config(account, updated_config, payload=payload)


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

        # Check payload draft for subreddits override, then fall back to account config
        draft = payload.get("draft") or {}
        subreddits = draft.get("subreddits") or config.get("subreddits") or []
        if isinstance(subreddits, str):
            subreddits = [s.strip() for s in subreddits.split(",") if s.strip()]
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
    await asyncio.to_thread(prepared_publishers.publish_youtube_sync, account, payload)


async def _publish_prepared_tiktok(
    platform: str,
    payload: dict,
    target: jobs.Target,
    *,
    account,
    account_file: Path | None,
) -> None:
    if account is None:
        raise ValueError("Prepared TikTok publish requires a structured account")
    result = await asyncio.to_thread(prepared_publishers.publish_tiktok_sync, account, payload)
    updated_config = result.get('updated_config') if isinstance(result, dict) else None
    _persist_rotated_config(account, updated_config, payload=payload)

    # Seed analytics so the video appears in the dashboard immediately.
    # Private-account videos never show up in /v2/video/list/, so without
    # this the analytics table stays empty for private TikTok accounts.
    publish_data = (result.get('publish') or {}).get('data') or {}
    publish_id = str(publish_data.get('publish_id') or '').strip()
    if publish_id:
        try:
            from datetime import datetime, timezone
            from myUtils import analytics_store
            now = datetime.now(timezone.utc).isoformat()
            title = str((payload.get('title') or payload.get('message') or '')[:200])
            video_id = f"pub:{publish_id}"
            analytics_store.upsert_video(
                account_id=account.id,
                platform='tiktok',
                platform_video_id=video_id,
                title=title,
                published_at=now,
            )
            analytics_store.record_snapshot(
                account_id=account.id,
                platform='tiktok',
                platform_video_id=video_id,
                views=0, likes=0, comments=0, shares=0,
                watch_time_seconds=0, engagement_rate=0.0,
                title=title,
                published_at=now,
            )
        except Exception:
            pass  # non-critical, don't fail the publish

    # Poll TikTok publish status until terminal or timeout. A terminal
    # ``failed`` status MUST raise: returning normally marked the target
    # succeeded even though TikTok definitively rejected the post.
    access_token = result.get('access_token') if isinstance(result, dict) else None
    if publish_id and access_token:
        _TIKTOK_POLL_INTERVAL = 15  # seconds
        _TIKTOK_POLL_ATTEMPTS = 20  # 20 × 15s = 5 min max

        def _record(status_val, *, fail_reason=None, post_id=None, platform_url=None):
            # Status bookkeeping is best-effort; it must never mask the
            # publish outcome.
            try:
                jobs.upsert_tiktok_publish_status(
                    publish_id,
                    job_id=str(target.job_id),
                    account_id=str(account.id),
                    status=status_val,
                    fail_reason=fail_reason,
                    post_id=post_id,
                    platform_url=platform_url,
                )
            except Exception:
                _logger.debug("tiktok status persistence failed", exc_info=True)

        _record('processing')
        terminal_failure: str | None = None
        for _ in range(_TIKTOK_POLL_ATTEMPTS):
            await asyncio.sleep(_TIKTOK_POLL_INTERVAL)
            try:
                status_resp = await asyncio.to_thread(
                    prepared_publishers.fetch_tiktok_publish_status,
                    access_token, publish_id,
                )
            except Exception:
                continue  # transient network error, keep polling
            status_data = (status_resp.get('data') or {})
            status_val = str(status_data.get('status') or 'processing').lower()
            fail_reason = str(status_data.get('fail_reason') or '').strip() or None
            post_id = str(
                status_data.get('publicaly_available_post_id')
                or status_data.get('post_id') or ''
            ).strip() or None
            platform_url = str(status_data.get('platform_url') or '').strip() or None
            _record(
                status_val,
                fail_reason=fail_reason,
                post_id=post_id,
                platform_url=platform_url,
            )
            if status_val == 'publish_complete':
                return
            if status_val == 'failed':
                terminal_failure = fail_reason or 'TikTok rejected the post'
                break

        if terminal_failure is not None:
            # TikTok explicitly rejected the post. Re-sending the identical
            # payload is not expected to help and risks a duplicate, so this
            # is terminal — the operator gets the failure alert instead.
            raise prepared_publishers.PreparedPublishError(
                f"TikTok publish failed: {terminal_failure}",
                retryable=False,
            )
        # The poll budget ran out before a terminal status. The post may still
        # be processing, so the outcome is unknown: do NOT report success.
        raise prepared_publishers.PreparedPublishError(
            "TikTok publish status did not reach a terminal state within the "
            "polling window; delivery outcome is unknown",
            retryable=True,
        )


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
    await asyncio.to_thread(prepared_publishers.publish_discord_sync, account, payload)


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
        await app.publish()
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
    await asyncio.to_thread(prepared_publishers.publish_teaching_blog_sync, account, payload)


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
    await asyncio.to_thread(prepared_publishers.publish_nw_sw_blog_sync, account, payload)


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
    await asyncio.to_thread(prepared_publishers.publish_bluesky_sync, account, payload)


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
    file_record_ids = payload.get("fileRecordIds")
    if isinstance(file_record_ids, list) and file_record_ids:
        allowed = {int(v) for v in file_record_ids if v is not None}
        original = payload.get("artifacts") or []
        kept = []
        for artifact in original:
            source_id = artifact.get("source_file_record_id")
            if source_id is None or int(source_id) in allowed:
                kept.append(artifact)
        payload = {**payload, "artifacts": kept}
    if payload.get("campaignId") and not (payload.get("artifacts") or []):
        raise ValueError(f"Prepared {platform} campaign has no media artifacts")
    await publisher(platform=platform, payload=payload, target=target, account=account, account_file=account_file)


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
    import requests
    from urllib.parse import urljoin

    current = url
    for redirect_count in range(6):
        if not _public_https_url(current):
            raise MediaRestoreError("Artifact URL must use a public HTTPS host")
        with requests.get(
            current, timeout=(10, 120), stream=True, allow_redirects=False
        ) as response:
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if not location or redirect_count == 5:
                    raise MediaRestoreError("Artifact URL redirect limit exceeded")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            _download_atomically(
                destination,
                lambda temporary: _write_response_chunks(response, temporary),
            )
            return
    raise MediaRestoreError("Artifact URL redirect limit exceeded")


def _write_response_chunks(response, destination: Path) -> None:
    with destination.open("wb") as output:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                output.write(chunk)


def _restore_optional_thumbnail(payload: dict, *, db_path: Path) -> None:
    thumbnail_ref = str(payload.get("thumbnail") or "")
    if not thumbnail_ref:
        return
    thumb = Path(thumbnail_ref)
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

    # Thumbnails offload like any other asset — restore before the uploader
    # opens the path, otherwise a scheduled post fails on a missing cover.
    if db_path is not None:
        _restore_optional_thumbnail(payload, db_path=db_path)

    structured_account = _resolve_structured_account(target.account_ref)
    canonical_account_file = _resolve_account_path(target.account_ref)
    if payload.get("campaignId") and payload.get("campaignPostId"):
        # Ensure artifact files are available locally
        if db_path:
            _ensure_artifact_paths_local(payload, db_path=db_path)
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
        thread_file_paths = [_resolve_file_path(file_ref, db_path=db_path) for file_ref in thread_file_refs]
        with decrypted_storage_state(canonical_account_file) as plain_path:
            await _run_platform_upload(
                platform,
                payload,
                target,
                account_file=plain_path,
                thread_file_paths=thread_file_paths,
            )
        return

    file_path = _resolve_file_path(target.file_ref, db_path=db_path)
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
