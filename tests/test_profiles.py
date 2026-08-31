"""Tests for the Profile / Account registry."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

import db.createTable as create_table

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module

from myUtils import profiles


class ProfileRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        create_table.bootstrap(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_and_lookup_profile(self) -> None:
        profile = profiles.create_profile(
            "Acme Corp",
            settings={"systemPrompt": "formal", "ctaText": "Contact us"},
            db_path=self.db_path,
        )
        self.assertEqual(profile.slug, "acme-corp")
        fetched = profiles.get_profile_by_slug("acme-corp", db_path=self.db_path)
        self.assertEqual(fetched.id, profile.id)
        self.assertEqual(fetched.name, "Acme Corp")
        self.assertEqual(
            fetched.settings,
            {"systemPrompt": "formal", "ctaText": "Contact us"},
        )

    def test_slug_collision_raises(self) -> None:
        profiles.create_profile("Acme", db_path=self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            profiles.create_profile("Acme", db_path=self.db_path)

    def test_profile_can_have_many_accounts_per_platform(self) -> None:
        profile = profiles.create_profile("Brand", db_path=self.db_path)
        a1 = profiles.add_account(profile.id, profiles.PLATFORM_MEDIUM, "alice", db_path=self.db_path)
        a2 = profiles.add_account(profile.id, profiles.PLATFORM_MEDIUM, "bob", db_path=self.db_path)
        a3 = profiles.add_account(profile.id, profiles.PLATFORM_SUBSTACK, "alice", db_path=self.db_path)
        self.assertNotEqual(a1.id, a2.id)
        self.assertNotEqual(a2.id, a3.id)

        accounts = profiles.list_accounts(profile_id=profile.id, db_path=self.db_path)
        self.assertEqual(len(accounts), 3)

        medium_only = profiles.list_accounts(
            profile_id=profile.id, platform=profiles.PLATFORM_MEDIUM, db_path=self.db_path
        )
        self.assertEqual({a.account_name for a in medium_only}, {"alice", "bob"})

    def test_duplicate_account_in_same_profile_platform_rejected(self) -> None:
        profile = profiles.create_profile("Brand", db_path=self.db_path)
        profiles.add_account(profile.id, profiles.PLATFORM_MEDIUM, "alice", db_path=self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            profiles.add_account(profile.id, profiles.PLATFORM_MEDIUM, "alice", db_path=self.db_path)

    def test_ensure_account_is_idempotent(self) -> None:
        profile = profiles.create_profile("Brand", db_path=self.db_path)
        first = profiles.ensure_account(profile.id, profiles.PLATFORM_MEDIUM, "alice", db_path=self.db_path)
        again = profiles.ensure_account(profile.id, profiles.PLATFORM_MEDIUM, "alice", db_path=self.db_path)
        self.assertEqual(first.id, again.id)

    def test_resolve_cookie_path_layout(self) -> None:
        path = profiles.resolve_cookie_path(profiles.PLATFORM_MEDIUM, "acme-corp", "alice")
        self.assertIn("medium", path.parts)
        self.assertIn("acme-corp", path.parts)
        self.assertEqual(path.suffix, ".json")

    def test_twitter_platform_is_registered_with_canonical_path(self) -> None:
        self.assertIn(profiles.PLATFORM_TWITTER, profiles.SUPPORTED_PLATFORMS)
        self.assertEqual(profiles.LEGACY_PLATFORM_CODE_TO_SLUG[7], profiles.PLATFORM_TWITTER)

        path = profiles.resolve_cookie_path(profiles.PLATFORM_TWITTER, "acme-corp", "x handle")
        self.assertIn("twitter", path.parts)
        self.assertIn("acme-corp", path.parts)
        self.assertEqual(path.name, "x-handle.json")

    def test_new_platform_helpers_cover_api_publish_targets(self) -> None:
        self.assertIn(profiles.PLATFORM_FACEBOOK, profiles.SUPPORTED_PLATFORMS)
        self.assertTrue(profiles.platform_supports_direct_publish(profiles.PLATFORM_THREADS))
        self.assertTrue(profiles.platform_supports_sheet_export(profiles.PLATFORM_INSTAGRAM))
        self.assertFalse(profiles.platform_supports_sheet_export(profiles.PLATFORM_TELEGRAM))
        self.assertFalse(profiles.platform_supports_direct_publish(profiles.PLATFORM_PATREON))
        self.assertFalse(profiles.platform_requires_cookie(profiles.PLATFORM_REDDIT))

    def test_add_account_supports_structured_config(self) -> None:
        profile = profiles.create_profile("Brand", db_path=self.db_path)
        account = profiles.add_account(
            profile.id,
            profiles.PLATFORM_REDDIT,
            "brand-main",
            auth_type="oauth",
            config={"subreddits": ["a", "b"]},
            db_path=self.db_path,
        )
        self.assertEqual(account.auth_type, "oauth")
        self.assertEqual(account.config, {"subreddits": ["a", "b"]})
        self.assertEqual(account.cookie_path, "")
        self.assertTrue(account.enabled)

    def test_add_account_supports_nickname_and_group(self) -> None:
        profile = profiles.create_profile("NicknameBrand", db_path=self.db_path)
        account = profiles.add_account(
            profile.id,
            profiles.PLATFORM_REDDIT,
            "brand-main",
            auth_type="oauth",
            nickname="Acme Main",
            account_group="Daily drivers",
            db_path=self.db_path,
        )
        self.assertEqual(account.nickname, "Acme Main")
        self.assertEqual(account.account_group, "Daily drivers")

        # Empty defaults
        plain = profiles.add_account(
            profile.id, profiles.PLATFORM_MEDIUM, "plain", db_path=self.db_path
        )
        self.assertEqual(plain.nickname, "")
        self.assertEqual(plain.account_group, "")

    def test_update_account_changes_nickname_and_group(self) -> None:
        profile = profiles.create_profile("UpdateBrand", db_path=self.db_path)
        account = profiles.add_account(
            profile.id, profiles.PLATFORM_MEDIUM, "alice", db_path=self.db_path
        )
        updated = profiles.update_account(
            account.id,
            nickname="Alice (Ops)",
            account_group="Demos",
            db_path=self.db_path,
        )
        self.assertEqual(updated.nickname, "Alice (Ops)")
        self.assertEqual(updated.account_group, "Demos")

    def test_list_accounts_filters_by_group(self) -> None:
        profile = profiles.create_profile("FilterBrand", db_path=self.db_path)
        a1 = profiles.add_account(
            profile.id, profiles.PLATFORM_MEDIUM, "alice",
            account_group="Daily", db_path=self.db_path,
        )
        profiles.add_account(
            profile.id, profiles.PLATFORM_MEDIUM, "bob",
            account_group="Demos", db_path=self.db_path,
        )
        profiles.add_account(
            profile.id, profiles.PLATFORM_MEDIUM, "carol",
            db_path=self.db_path,
        )
        daily = profiles.list_accounts(
            profile_id=profile.id, account_group="Daily", db_path=self.db_path
        )
        self.assertEqual([a.id for a in daily], [a1.id])

    def test_list_account_groups_excludes_empty(self) -> None:
        profile = profiles.create_profile("GroupsBrand", db_path=self.db_path)
        profiles.add_account(
            profile.id, profiles.PLATFORM_MEDIUM, "a",
            account_group="Daily", db_path=self.db_path,
        )
        profiles.add_account(
            profile.id, profiles.PLATFORM_MEDIUM, "b",
            account_group="Demos", db_path=self.db_path,
        )
        profiles.add_account(
            profile.id, profiles.PLATFORM_MEDIUM, "c", db_path=self.db_path
        )
        groups = profiles.list_account_groups(db_path=self.db_path)
        self.assertEqual(groups, ["Daily", "Demos"])

    def test_list_accounts_search_matches_nickname_and_account_name(self) -> None:
        profile = profiles.create_profile("SearchBrand", db_path=self.db_path)
        profiles.add_account(
            profile.id, profiles.PLATFORM_DOUYIN, "official_handle",
            nickname="Acme Main", db_path=self.db_path,
        )
        profiles.add_account(
            profile.id, profiles.PLATFORM_DOUYIN, "alt_handle",
            nickname="Backup Brand", db_path=self.db_path,
        )
        profiles.add_account(
            profile.id, profiles.PLATFORM_DOUYIN, "untouched",
            db_path=self.db_path,
        )

        # Case-insensitive substring against the nickname
        matched_nick = profiles.list_accounts(
            profile_id=profile.id, search="acme", db_path=self.db_path,
        )
        self.assertEqual([a.account_name for a in matched_nick], ["official_handle"])

        # Substring against the underlying account_name
        matched_name = profiles.list_accounts(
            profile_id=profile.id, search="alt_", db_path=self.db_path,
        )
        self.assertEqual([a.account_name for a in matched_name], ["alt_handle"])

        # Blank search returns every account under the profile
        all_rows = profiles.list_accounts(
            profile_id=profile.id, search="   ", db_path=self.db_path,
        )
        self.assertEqual(len(all_rows), 3)

        # No match returns an empty list (not an error)
        self.assertEqual(
            profiles.list_accounts(
                profile_id=profile.id, search="ghost", db_path=self.db_path,
            ),
            [],
        )

        # Search combines with platform filter via AND
        profiles.add_account(
            profile.id, profiles.PLATFORM_MEDIUM, "another_acme",
            nickname="Acme Blog", db_path=self.db_path,
        )
        douyin_acme = profiles.list_accounts(
            profile_id=profile.id,
            search="acme",
            platform=profiles.PLATFORM_DOUYIN,
            db_path=self.db_path,
        )
        self.assertEqual([a.account_name for a in douyin_acme], ["official_handle"])


    def test_iter_accounts_for_publish_named(self) -> None:
        profile = profiles.create_profile("Brand", db_path=self.db_path)
        profiles.add_account(profile.id, profiles.PLATFORM_MEDIUM, "alice", db_path=self.db_path)
        profiles.add_account(profile.id, profiles.PLATFORM_MEDIUM, "bob", db_path=self.db_path)

        chosen = profiles.iter_accounts_for_publish(
            profile.id, profiles.PLATFORM_MEDIUM, ["alice"], db_path=self.db_path
        )
        self.assertEqual([a.account_name for a in chosen], ["alice"])

        with self.assertRaises(LookupError):
            profiles.iter_accounts_for_publish(
                profile.id, profiles.PLATFORM_MEDIUM, ["ghost"], db_path=self.db_path
            )

    def test_delete_profile_cascades_accounts(self) -> None:
        profile = profiles.create_profile("Brand", db_path=self.db_path)
        profiles.add_account(profile.id, profiles.PLATFORM_MEDIUM, "alice", db_path=self.db_path)
        profiles.delete_profile(profile.id, db_path=self.db_path)
        self.assertEqual(
            profiles.list_accounts(profile_id=profile.id, db_path=self.db_path), []
        )

    def test_list_accounts_joins_profile_metadata(self) -> None:
        """list_accounts() should attach profile name + slug via JOIN so the
        UI doesn't have to fall back to "default" everywhere.

        Previously this returned Account rows with empty profile_name, which
        caused the Accounts tab to render "Profile: default" for every card
        regardless of the actual owner.
        """
        brand = profiles.create_profile("Brand", db_path=self.db_path)
        teaching = profiles.create_profile("Teaching", db_path=self.db_path)
        profiles.add_account(brand.id, profiles.PLATFORM_MEDIUM, "alice", db_path=self.db_path)
        profiles.add_account(teaching.id, profiles.PLATFORM_MEDIUM, "bob", db_path=self.db_path)

        rows = profiles.list_accounts(db_path=self.db_path)
        by_name = {row.account_name: row for row in rows}
        self.assertEqual(set(by_name), {"alice", "bob"})
        self.assertEqual(by_name["alice"].profile_name, "Brand")
        self.assertEqual(by_name["alice"].profile_slug, "brand")
        self.assertEqual(by_name["alice"].profile_id, brand.id)
        self.assertEqual(by_name["bob"].profile_name, "Teaching")
        self.assertEqual(by_name["bob"].profile_id, teaching.id)

    def test_get_account_joins_profile_metadata(self) -> None:
        brand = profiles.create_profile("Brand", db_path=self.db_path)
        acc = profiles.add_account(
            brand.id, profiles.PLATFORM_MEDIUM, "alice", db_path=self.db_path
        )
        fetched = profiles.get_account(acc.id, db_path=self.db_path)
        self.assertEqual(fetched.profile_name, "Brand")
        self.assertEqual(fetched.profile_slug, "brand")

    def test_list_account_profiles_returns_counts(self) -> None:
        """The Accounts tab profile filter needs per-profile account counts."""
        brand = profiles.create_profile("Brand", db_path=self.db_path)
        teaching = profiles.create_profile("Teaching", db_path=self.db_path)
        # Empty profile should NOT show up in the filter.
        empty = profiles.create_profile("Empty", db_path=self.db_path)
        profiles.add_account(brand.id, profiles.PLATFORM_MEDIUM, "a", db_path=self.db_path)
        profiles.add_account(brand.id, profiles.PLATFORM_SUBSTACK, "a", db_path=self.db_path)
        profiles.add_account(teaching.id, profiles.PLATFORM_MEDIUM, "b", db_path=self.db_path)

        rows = profiles.list_account_profiles(db_path=self.db_path)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(set(by_id), {brand.id, teaching.id})
        self.assertEqual(by_id[brand.id]["count"], 2)
        self.assertEqual(by_id[teaching.id]["count"], 1)
        self.assertNotIn(empty.id, by_id)


if __name__ == "__main__":
    unittest.main()
