"""Tests for the inbox NSFW containment guard.

The inbox one-click publish used to pass ``selected_account_ids=None``, which
the orchestrator expands to every enabled account of the profile — quietly
sending nudity to Instagram/Facebook/Threads/YouTube/TikTok.

The rating now comes from ``myUtils.content_rating``: only a filename that
starts or ends with ``sfw`` is SFW, everything else (including unlabelled) is
NSFW, and the item's ``sfwFlag`` may tighten but never loosen that. These tests
cover the inbox helper's wiring to that module; the rule itself is tested in
``test_content_rating``.
"""
import unittest
from unittest import mock

import sau_backend


class _Acct:
    def __init__(self, id, platform):
        self.id = id
        self.platform = platform


SFW_FILE = "SFW 20260813074703243.mp4"
NSFW_FILE = "20260722155038425.mp4"


class InboxNsfwContainmentTests(unittest.TestCase):
    def test_sfw_filename_returns_none_so_orchestrator_keeps_default(self):
        out = sau_backend._inbox_selected_account_ids(
            [1], "sfw", db_path=None, media_file_paths=[SFW_FILE],
        )
        self.assertIsNone(out)

    def test_unlabelled_filename_is_nsfw_even_with_no_flag(self):
        # The default that was backwards before: no marker means NSFW.
        accounts = [_Acct(118, "bluesky"), _Acct(11, "facebook")]
        with mock.patch.object(
            sau_backend.profile_registry, "list_accounts", return_value=accounts
        ):
            out = sau_backend._inbox_selected_account_ids(
                [1], None, db_path=None, media_file_paths=[NSFW_FILE],
            )
        self.assertEqual(out, [118])

    def test_sfw_flag_cannot_launder_an_nsfw_filename(self):
        accounts = [_Acct(118, "bluesky"), _Acct(11, "facebook")]
        with mock.patch.object(
            sau_backend.profile_registry, "list_accounts", return_value=accounts
        ):
            out = sau_backend._inbox_selected_account_ids(
                [1], "sfw", db_path=None, media_file_paths=[NSFW_FILE],
            )
        self.assertEqual(out, [118])

    def test_nsfw_flag_tightens_an_sfw_filename(self):
        accounts = [_Acct(118, "bluesky"), _Acct(11, "facebook")]
        with mock.patch.object(
            sau_backend.profile_registry, "list_accounts", return_value=accounts
        ):
            out = sau_backend._inbox_selected_account_ids(
                [1], "nsfw", db_path=None, media_file_paths=[SFW_FILE],
            )
        self.assertEqual(out, [118])

    def test_nsfw_excludes_restricted_platforms(self):
        accounts = [
            _Acct(118, "bluesky"), _Acct(116, "telegram"), _Acct(123, "twitter"),
            _Acct(11, "facebook"), _Acct(72, "instagram"), _Acct(62, "threads"),
            _Acct(110, "youtube"), _Acct(109, "tiktok"),
        ]
        with mock.patch.object(
            sau_backend.profile_registry, "list_accounts", return_value=accounts
        ):
            out = sau_backend._inbox_selected_account_ids(
                [1], "nsfw", db_path=None, media_file_paths=[NSFW_FILE],
            )
        self.assertEqual(sorted(out), [116, 118, 123])

    def test_nsfw_with_no_safe_account_refuses(self):
        accounts = [_Acct(11, "facebook"), _Acct(72, "instagram")]
        with mock.patch.object(
            sau_backend.profile_registry, "list_accounts", return_value=accounts
        ):
            with self.assertRaises(ValueError):
                sau_backend._inbox_selected_account_ids(
                    [1], "nsfw", db_path=None, media_file_paths=[NSFW_FILE],
                )

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
            out = sau_backend._inbox_selected_account_ids(
                [1, 3], "nsfw", db_path=None, media_file_paths=[NSFW_FILE],
            )
        self.assertEqual(sorted(out), [118, 120])

    def test_restricted_constant_is_reexported_from_content_rating(self):
        from myUtils import content_rating
        self.assertIs(sau_backend.NSFW_RESTRICTED_PLATFORMS, content_rating.NSFW_RESTRICTED_PLATFORMS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
