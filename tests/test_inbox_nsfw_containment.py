"""Tests for the inbox NSFW containment guard.

The inbox one-click publish used to pass ``selected_account_ids=None``, which
the orchestrator expands to every enabled account of the profile — quietly
sending nudity to Instagram/Facebook/Threads/YouTube/TikTok. The guard derives
an explicit allowlist for NSFW items instead.
"""
import types
import unittest
from unittest import mock

import sau_backend


class _Acct:
    def __init__(self, id, platform):
        self.id = id
        self.platform = platform


class InboxNsfwContainmentTests(unittest.TestCase):
    def test_sfw_returns_none_so_orchestrator_keeps_default(self):
        out = sau_backend._inbox_selected_account_ids([1], "sfw", db_path=None)
        self.assertIsNone(out)

    def test_missing_flag_returns_none(self):
        out = sau_backend._inbox_selected_account_ids([1], None, db_path=None)
        self.assertIsNone(out)

    def test_nsfw_excludes_restricted_platforms(self):
        accounts = [
            _Acct(118, "bluesky"),
            _Acct(116, "telegram"),
            _Acct(123, "twitter"),
            _Acct(11, "facebook"),
            _Acct(72, "instagram"),
            _Acct(62, "threads"),
            _Acct(110, "youtube"),
            _Acct(109, "tiktok"),
        ]
        with mock.patch.object(
            sau_backend.profile_registry, "list_accounts", return_value=accounts
        ):
            out = sau_backend._inbox_selected_account_ids([1], "nsfw", db_path=None)
        self.assertEqual(sorted(out), [116, 118, 123])

    def test_nsfw_uppercase_flag_still_restricts(self):
        accounts = [_Acct(11, "facebook"), _Acct(118, "bluesky")]
        with mock.patch.object(
            sau_backend.profile_registry, "list_accounts", return_value=accounts
        ):
            out = sau_backend._inbox_selected_account_ids([1], "NSFW", db_path=None)
        self.assertEqual(out, [118])

    def test_nsfw_with_no_safe_account_refuses(self):
        accounts = [_Acct(11, "facebook"), _Acct(72, "instagram")]
        with mock.patch.object(
            sau_backend.profile_registry, "list_accounts", return_value=accounts
        ):
            with self.assertRaises(ValueError):
                sau_backend._inbox_selected_account_ids([1], "nsfw", db_path=None)

    def test_nsfw_spans_multiple_profiles(self):
        per_profile = {
            1: [_Acct(118, "bluesky"), _Acct(11, "facebook")],
            3: [_Acct(120, "bluesky"), _Acct(75, "instagram")],
        }
        with mock.patch.object(
            sau_backend.profile_registry,
            "list_accounts",
            side_effect=lambda *, profile_id, **kw: per_profile[profile_id],
        ):
            out = sau_backend._inbox_selected_account_ids([1, 3], "nsfw", db_path=None)
        self.assertEqual(sorted(out), [118, 120])


if __name__ == "__main__":
    unittest.main(verbosity=2)