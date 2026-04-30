"""Tests for the concurrency primitives used by the publish worker."""

from __future__ import annotations

import asyncio
import unittest

from utils.concurrency import AccountConcurrency


class AccountConcurrencyTests(unittest.TestCase):
    def test_global_cap_limits_concurrent_holders(self) -> None:
        sentinel = AccountConcurrency(max_concurrent=2)

        peak = 0
        active = 0
        lock = asyncio.Lock()

        async def task(account: str) -> None:
            nonlocal peak, active
            async with sentinel.slot(account):
                async with lock:
                    nonlocal_active = active = active + 1
                    if nonlocal_active > peak:
                        peak = nonlocal_active
                await asyncio.sleep(0.05)
                async with lock:
                    active = active - 1

        async def go() -> None:
            await asyncio.gather(*(task(f"acct-{i}") for i in range(8)))

        asyncio.run(go())
        self.assertLessEqual(peak, 2)

    def test_per_account_lock_serialises_same_account(self) -> None:
        sentinel = AccountConcurrency(max_concurrent=4)
        observed: list[str] = []

        async def task(name: str) -> None:
            async with sentinel.slot("only-account"):
                observed.append(f"start-{name}")
                await asyncio.sleep(0.02)
                observed.append(f"end-{name}")

        async def go() -> None:
            await asyncio.gather(task("a"), task("b"), task("c"))

        asyncio.run(go())
        # Because all three contended on the same account, they must have
        # executed serially: each end-X is immediately preceded by the matching
        # start-X.
        for index in range(0, len(observed), 2):
            start = observed[index]
            end = observed[index + 1]
            self.assertEqual(start.split("-")[1], end.split("-")[1])

    def test_in_flight_accounts_reflects_held_locks(self) -> None:
        sentinel = AccountConcurrency(max_concurrent=4)
        gate = asyncio.Event()
        proceed = asyncio.Event()

        async def hold(account: str) -> None:
            async with sentinel.slot(account):
                gate.set()
                await proceed.wait()

        async def go() -> None:
            task = asyncio.create_task(hold("acct-x"))
            await gate.wait()
            self.assertIn("acct-x", sentinel.in_flight_accounts())
            proceed.set()
            await task
            # After the slot is released the account is no longer in flight.
            self.assertNotIn("acct-x", sentinel.in_flight_accounts())

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
