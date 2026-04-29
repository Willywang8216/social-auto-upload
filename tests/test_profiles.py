"""Tests for the Profile / Account registry."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db.createTable as create_table
from myUtils import profiles


class ProfileRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        create_table.bootstrap(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_and_lookup_profile(self) -> None:
        profile = profiles.create_profile("Acme Corp", db_path=self.db_path)
        self.assertEqual(profile.slug, "acme-corp")
        fetched = profiles.get_profile_by_slug("acme-corp", db_path=self.db_path)
        self.assertEqual(fetched.id, profile.id)
        self.assertEqual(fetched.name, "Acme Corp")

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

    def test_unsupported_platform_rejected(self) -> None:
        profile = profiles.create_profile("Brand", db_path=self.db_path)
        with self.assertRaises(ValueError):
            profiles.add_account(profile.id, "myspace", "alice", db_path=self.db_path)

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


if __name__ == "__main__":
    unittest.main()
