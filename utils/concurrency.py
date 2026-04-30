"""Concurrency primitives for the publish pipeline.

The existing uploaders (``DouYinVideo``, ``KSVideo``, ``XiaoHongShuVideo`` …)
each open their own Playwright instance and run a full upload in one async
call. Today the Flask backend runs them strictly serially in nested loops,
which is the dominant wall-clock cost of any batch publish.

This module gives the worker two simple guarantees:

1. A global **launch semaphore** that caps the number of concurrent Playwright
   browsers. Default is ``MAX_CONCURRENT_BROWSERS`` (env-overridable). This is
   the ceiling that keeps the host machine sane.

2. A per-account **lock** so two uploads against the same account_file can
   never run concurrently. Same-account concurrency tends to break session
   state on the platforms we drive, so we serialise within an account but
   parallelise across accounts.

The pool is intentionally minimal — no context reuse yet, since the existing
uploaders insist on owning their own ``async_playwright()`` block. Reuse is
a future optimisation that requires uploader refactors.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


MAX_CONCURRENT_BROWSERS = _read_int_env("SAU_MAX_CONCURRENT_BROWSERS", 3)


class AccountConcurrency:
    """Bounded global concurrency + per-account exclusive lock."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_BROWSERS) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self.max_concurrent = max_concurrent

    async def _get_lock(self, account_ref: str) -> asyncio.Lock:
        # Lock-creation needs its own guard; otherwise two coroutines can
        # both observe a missing key and create separate locks for the same
        # account, defeating the point.
        async with self._locks_guard:
            lock = self._locks.get(account_ref)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[account_ref] = lock
            return lock

    @asynccontextmanager
    async def slot(self, account_ref: str) -> AsyncIterator[None]:
        """Acquire a global slot AND the per-account lock.

        Order matters: take the per-account lock *first* so two tasks for the
        same account queue up behind it without burning a global slot. Then
        take the global semaphore, so the work inside the ``async with`` block
        runs only when both are free.
        """

        lock = await self._get_lock(account_ref)
        async with lock:
            async with self._semaphore:
                yield

    def in_flight_accounts(self) -> set[str]:
        """Return the set of accounts currently holding their per-account lock.

        Used by the worker to compute the ``excluded_accounts`` set when
        claiming new targets so the same account is never double-claimed.
        """

        return {ref for ref, lock in self._locks.items() if lock.locked()}
