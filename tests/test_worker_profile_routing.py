"""A target must never be published by an account of another profile.

A target stores only ``account:<id>`` and the worker re-resolves that account at
publish time. The submit path scopes account lookup by profile
(``publish_orchestrator._resolve_accounts`` lists by profile id first), so a
foreign id cannot enter a NEW job - but nothing checked the window between
queueing and publishing. An account moved to another profile in that window
would be handed the wrong profile's campaign: Nakedwill content onto a
Sexualwill account, or a Chinese Teaching post onto an English account.
"""

import unittest
from types import SimpleNamespace

from myUtils import worker


def _target(account_ref="account:11"):
    return SimpleNamespace(id=1, job_id=1, account_ref=account_ref, attempts=1)


class AccountProfileRoutingTests(unittest.TestCase):
    def test_matching_profile_is_allowed(self):
        account = SimpleNamespace(id=11, profile_id=1)
        job = SimpleNamespace(profile_id=1)
        self.assertIsNone(worker._account_profile_mismatch(_target(), account, job))

    def test_mismatched_profile_is_refused_with_both_ids_named(self):
        account = SimpleNamespace(id=11, profile_id=3)
        job = SimpleNamespace(profile_id=1)
        reason = worker._account_profile_mismatch(_target(), account, job)
        self.assertIsNotNone(reason)
        self.assertIn("profile 3", reason)
        self.assertIn("profile 1", reason)

    def test_null_job_profile_is_not_judged(self):
        # A legacy job with no profile_id cannot establish a mismatch; refusing
        # it would strand history that is otherwise publishable.
        account = SimpleNamespace(id=11, profile_id=3)
        job = SimpleNamespace(profile_id=None)
        self.assertIsNone(worker._account_profile_mismatch(_target(), account, job))

    def test_null_account_profile_is_not_judged(self):
        account = SimpleNamespace(id=11, profile_id=None)
        job = SimpleNamespace(profile_id=1)
        self.assertIsNone(worker._account_profile_mismatch(_target(), account, job))

    def test_missing_job_is_not_judged(self):
        account = SimpleNamespace(id=11, profile_id=1)
        self.assertIsNone(worker._account_profile_mismatch(_target(), account, None))


class ProfileRoutingDatabaseTests(unittest.TestCase):
    """The invariant holds for the live queue shape, not just the helper."""

    def test_string_and_int_profile_ids_compare_equal(self):
        # Row values can arrive as int or str depending on the reader.
        account = SimpleNamespace(id=11, profile_id="1")
        job = SimpleNamespace(profile_id=1)
        self.assertIsNone(worker._account_profile_mismatch(_target(), account, job))


if __name__ == "__main__":
    unittest.main()


class DisabledAccountTests(unittest.TestCase):
    """A disabled account must not publish.

    ``enabled=0`` is honoured at job creation (``_resolve_accounts`` lists with
    ``enabled=True``) but ``get_account`` does not filter on it, so a target
    queued before the account was disabled would still fire. Disabling an account
    is the control an operator uses to stop it publishing; a scheduled target
    going out anyway defeats it.
    """

    def test_enabled_account_is_allowed(self):
        self.assertIsNone(worker._account_unpublishable(SimpleNamespace(id=1, enabled=True)))

    def test_disabled_account_is_refused_with_the_id(self):
        reason = worker._account_unpublishable(SimpleNamespace(id=42, enabled=False))
        self.assertIsNotNone(reason)
        self.assertIn("42", reason)
        self.assertIn("disabled", reason)

    def test_missing_enabled_flag_is_treated_as_enabled(self):
        # Legacy rows without the flag must not be blocked.
        self.assertIsNone(worker._account_unpublishable(SimpleNamespace(id=1)))
        self.assertIsNone(worker._account_unpublishable(SimpleNamespace(id=1, enabled=None)))
