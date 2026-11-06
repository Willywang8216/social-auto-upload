"""Offline native/managed union, booking evidence and tightening-only pacing."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from myUtils import account_identity as identity, destination_pacing as pacing

NOW = datetime(2035, 1, 2, 12)


def _mapping(workspace="26985", profile_ref="3917", network="tiktok"):
    return {"network": network, "profile_refs": [profile_ref], "workspace_id": workspace}


def _schema(conn):
    conn.executescript("""
        CREATE TABLE profiles(id INTEGER PRIMARY KEY, settings_json TEXT DEFAULT '{}');
        CREATE TABLE accounts(id INTEGER PRIMARY KEY, profile_id INTEGER, platform TEXT,
                              cookie_path TEXT DEFAULT '', config_json TEXT DEFAULT '{}');
        CREATE TABLE publish_jobs(id INTEGER PRIMARY KEY, status TEXT DEFAULT 'pending');
        CREATE TABLE campaigns(id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE campaign_posts(id INTEGER PRIMARY KEY, campaign_id INTEGER);
        CREATE TABLE publish_job_targets(
            id INTEGER PRIMARY KEY, job_id INTEGER DEFAULT 1, account_ref TEXT, file_ref TEXT DEFAULT 'fixture',
            status TEXT, schedule_at TEXT, started_at TEXT, finished_at TEXT, checkpoint_json TEXT DEFAULT '{}',
            delivery_state TEXT, next_attempt_at TEXT, lease_expires_at TEXT);
        INSERT INTO profiles(id) VALUES(1),(2),(3);
        INSERT INTO publish_jobs(id) VALUES(1);
    """)


def _account(conn, account_id, *, config=None, platform="tiktok", profile=1, cookie=""):
    conn.execute("INSERT INTO accounts VALUES(?,?,?,?,?)",
                 (account_id, profile, platform, cookie, json.dumps(config or {})))
    return f"account:{account_id}"


def _shared_accounts(conn):
    # The existing native backup mapping and a brand-new explicit profile alias.
    _account(conn, 109, config={"openId": "synthetic-native"})
    _account(conn, 451, config={"publishingProvider": "sociamonials", "sociamonials": _mapping()}, profile=2)
    _account(conn, 100, config={"openId": "synthetic-teaching"}, profile=3)


def _target(conn, ref="account:109", *, status="pending", schedule=None, started=None,
            finished=None, receipt=None, checkpoint=None, delivery=None, lease=None):
    if checkpoint is None:
        checkpoint = {"provider_receipt": receipt} if receipt is not None else {}
    return conn.execute(
        "INSERT INTO publish_job_targets(account_ref,status,schedule_at,started_at,finished_at,"
        "checkpoint_json,delivery_state,lease_expires_at) VALUES(?,?,?,?,?,?,?,?)",
        (ref, status, schedule, started, finished, json.dumps(checkpoint), delivery, lease),
    ).lastrowid


class DestinationFixture(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        _schema(self.conn)
        _shared_accounts(self.conn)
        from myUtils import sociamonials_fallback as managed
        self.enterContext(patch.object(managed, "get_workspace_id", return_value="26985"))
        self.enterContext(patch.object(managed, "get_teaching_workspace_id", return_value="34293"))
        # Identity must never acquire credentials or verify a provider destination.
        self.enterContext(patch.object(managed, "resolve_credentials", side_effect=AssertionError("credentials")))
        self.enterContext(patch.object(managed, "get_api_key", side_effect=AssertionError("credentials")))
        self.enterContext(patch.object(managed, "get_teaching_api_key", side_effect=AssertionError("credentials")))


class DestinationIdentityTests(DestinationFixture):
    def test_new_alias_without_any_targets_joins_existing_native_backup(self):
        keys = identity.canonical_account_keys(self.conn)
        self.assertEqual(keys["account:109"], keys["account:451"])
        aliases = pacing.account_refs(self.conn)
        self.assertIn("account:451", aliases[keys["account:109"]])
        self.assertEqual(identity.canonical_account_key(self.conn, "account:451"), keys["account:109"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM publish_job_targets").fetchone()[0], 0)

    def test_native_open_id_bridges_through_backup_to_managed_alias(self):
        _account(self.conn, 452, config={"open_id": "synthetic-native"})
        _account(self.conn, 453, config={"openId": "different"})
        keys = identity.canonical_account_keys(self.conn)
        self.assertEqual(keys["account:109"], keys["account:452"])
        self.assertEqual(keys["account:452"], keys["account:451"])
        self.assertNotEqual(keys["account:453"], keys["account:451"])
        self.assertEqual(identity.external_account_identity({"platform": "tiktok", "config": {"open_id": "one"}}),
                         ("tiktok", "one"))

    def test_workspace_network_and_profile_are_all_required_to_match(self):
        for identifier, mapping, platform in (
            (452, _mapping(workspace="34293"), "tiktok"),
            (453, _mapping(profile_ref="different"), "tiktok"),
            (454, _mapping(network="thrd"), "threads"),
        ):
            _account(self.conn, identifier, config={"sociamonials": mapping}, platform=platform)
        keys = identity.canonical_account_keys(self.conn)
        for ref in ("account:100", "account:452", "account:453", "account:454"):
            self.assertNotEqual(keys["account:451"], keys[ref])
        self.assertEqual(identity.managed_account_identity(
            {"id": 100, "platform": "tiktok", "config": {}}), ("34293", "tiktok", "5800"))

    def test_malformed_explicit_overrides_do_not_use_builtin_or_infer_network(self):
        invalid = [None, {}, {"profile_refs": ["3917"], "workspace_id": "26985"},
                   _mapping(network="thrd"), {**_mapping(), "profile_refs": ["3917", "other"]},
                   {**_mapping(), "workspace_id": "bad"}, {**_mapping(), "workspace_id": 0},
                   {**_mapping(), "workspace_id": False}, {**_mapping(), "profile_refs": [False]}]
        for mapping in invalid:
            with self.subTest(mapping=mapping):
                self.conn.execute("UPDATE accounts SET config_json=? WHERE id=109",
                                  (json.dumps({"openId": "synthetic-native", "sociamonials": mapping}),))
                keys = identity.canonical_account_keys(self.conn)
                self.assertNotEqual(keys["account:109"], keys["account:451"])

    def test_profile_override_and_invalid_profile_override_preserve_precedence(self):
        _account(self.conn, 452, config={}, profile=2)
        self.conn.execute("UPDATE profiles SET settings_json=? WHERE id=2",
                          (json.dumps({"sociamonials": {"accounts": {"452": _mapping()}}}),))
        keys = identity.canonical_account_keys(self.conn)
        self.assertEqual(keys["account:452"], keys["account:109"])
        self.conn.execute("UPDATE profiles SET settings_json=? WHERE id=1",
                          (json.dumps({"sociamonials": {"accounts": {"109": {"network": "tiktok"}}}}),))
        keys = identity.canonical_account_keys(self.conn)
        self.assertNotEqual(keys["account:109"], keys["account:451"])

    def test_names_and_rotating_tokens_are_not_identities_and_native_hash_is_unchanged(self):
        for identifier in (452, 453):
            _account(self.conn, identifier, config={"displayName": "same", "accessToken": "same-fixture-token"})
        keys = identity.canonical_account_keys(self.conn)
        self.assertNotEqual(keys["account:452"], keys["account:453"])
        _account(self.conn, 454, config={"page_id": "123"}, platform="facebook")
        expected = "account-" + hashlib.sha256(b'["provider","facebook","123"]').hexdigest()
        self.assertEqual(identity.canonical_account_key(self.conn, "account:454"), expected)
        self.conn.execute("UPDATE accounts SET config_json=? WHERE id=454",
                          (json.dumps({"page_id": "123", "accessToken": "rotated-fixture"}),))
        self.assertEqual(identity.canonical_account_key(self.conn, "account:454"), expected)

    def test_cookie_and_legacy_aliases_are_lexical_and_platform_scoped(self):
        _account(self.conn, 452, cookie=r"C:\cookies\one.json")
        _account(self.conn, 453, cookie="c:/cookies/one.json")
        _account(self.conn, 454, cookie="c:/cookies/one.json", platform="instagram")
        keys = identity.canonical_account_keys(self.conn, ("C:\\legacy\\one.json", "c:/legacy/one.json"))
        self.assertEqual(keys["account:452"], keys["account:453"])
        self.assertNotEqual(keys["account:452"], keys["account:454"])
        self.assertEqual(keys["C:\\legacy\\one.json"], keys["c:/legacy/one.json"])


class DestinationBookingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        _schema(self.conn)
        _shared_accounts(self.conn)
        # Booking tests consume a fixed union; identity resolution is tested
        # independently above, without conflating provider policy with history.
        self.aliases = {"shared": ["account:109", "account:451"], "teaching": ["account:100"]}

    def _booked(self):
        return pacing.booked_slots(self.conn, self.aliases, now=NOW)

    def test_all_booked_active_and_unknown_work_consumes_capacity(self):
        for status in ("pending", "retrying", "running", "awaiting_delivery", "needs_review"):
            _target(self.conn, status=status)
        _target(self.conn, status="succeeded", finished=NOW.isoformat())
        _target(self.conn, status="failed", schedule=NOW.isoformat())
        _target(self.conn, status="cancelled", schedule=NOW.isoformat())
        key = "shared"
        self.assertEqual(self._booked()[key], [NOW.isoformat()] * 6)

    def test_receipt_time_then_finished_started_schedule_precedence(self):
        receipt = {"provider": "tiktok", "id": "published", "status": "delivered",
                   "delivered_at": "2035-01-02T20:00:00+08:00"}
        _target(self.conn, status="succeeded", schedule="2034-01-01T00:00:00", started="2034-02-01T00:00:00",
                finished="2034-03-01T00:00:00", receipt=receipt)
        _target(self.conn, status="succeeded", finished="2035-01-02T13:00:00", started="2035-01-01T00:00:00")
        _target(self.conn, status="succeeded", started="2035-01-02T14:00:00", schedule="2035-01-01T00:00:00")
        _target(self.conn, status="succeeded", schedule="2035-01-02T15:00:00Z")
        key = "shared"
        self.assertEqual(self._booked()[key], ["2035-01-02T12:00:00", "2035-01-02T13:00:00",
                                             "2035-01-02T14:00:00", "2035-01-02T15:00:00"])

    def test_same_confirmed_receipt_across_aliases_is_only_one_delivery(self):
        receipt = {"provider": "sociamonials", "id": "receipt-1", "status": "delivered",
                   "delivered_at": NOW.isoformat(), "workspace_id": "26985", "network": "tiktok",
                   "profile_refs": ["3917"]}
        _target(self.conn, status="succeeded", receipt=receipt)
        _target(self.conn, "account:451", status="succeeded", receipt=receipt)
        _target(self.conn, "account:100", status="succeeded", receipt={**receipt, "workspace_id": "34293"})
        key = "shared"
        self.assertEqual(self._booked()[key], [NOW.isoformat()])
        self.assertEqual(sum(map(len, self._booked().values())), 2)

    def test_unknown_receipts_are_not_collapsed_and_overdue_work_reserves_now(self):
        receipt = {"provider": "sociamonials", "id": "unknown", "status": "processing"}
        _target(self.conn, status="awaiting_delivery", receipt=receipt, schedule="2034-01-01T00:00:00")
        _target(self.conn, "account:451", status="needs_review", receipt=receipt)
        key = "shared"
        self.assertEqual(self._booked()[key], [NOW.isoformat(), NOW.isoformat()])

    def test_bad_history_never_turns_into_an_empty_calendar(self):
        target = _target(self.conn, schedule="not-a-time")
        with self.assertRaises(pacing.PacingRefused):
            self._booked()
        self.conn.execute("UPDATE publish_job_targets SET schedule_at=?,checkpoint_json=? WHERE id=?",
                          (NOW.isoformat(), '{"provider_receipt":{},"provider_receipt":{}}', target))
        with self.assertRaises(pacing.PacingRefused):
            self._booked()
        self.conn.execute("UPDATE publish_job_targets SET checkpoint_json='[]' WHERE id=?", (target,))
        with self.assertRaises(pacing.PacingRefused):
            self._booked()

    def test_readonly_connection_never_creates_missing_database_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "absent" / "missing.db"
            with self.assertRaises(FileNotFoundError):
                pacing.connect_readonly(missing)
            self.assertFalse(missing.parent.exists())
            path = Path(directory) / "snapshot # 測試.db"
            with closing(sqlite3.connect(path)) as disk:
                self.conn.commit()
                self.conn.backup(disk)
            before = path.read_bytes()
            with pacing.connect_readonly(path) as disk:
                disk.execute("BEGIN")
                pacing.booked_slots(disk, self.aliases, now=NOW)
                with self.assertRaises(sqlite3.OperationalError):
                    disk.execute("DELETE FROM accounts")
            self.assertEqual(path.read_bytes(), before)


    def test_inflight_start_time_wins_over_a_future_legacy_schedule(self):
        for status in ("running", "awaiting_delivery", "needs_review"):
            _target(self.conn, status=status, started=NOW.isoformat(), schedule="2036-01-01T12:00:00")
        self.assertEqual(self._booked()["shared"], [NOW.isoformat()] * 3)

    def test_generic_confirmation_ids_do_not_collapse_distinct_deliveries(self):
        receipt = {"provider": "legacy", "id": "confirmed", "status": "delivered", "delivered_at": NOW.isoformat()}
        _target(self.conn, status="succeeded", receipt=receipt)
        _target(self.conn, "account:451", status="succeeded", receipt=receipt)
        self.assertEqual(self._booked()["shared"], [NOW.isoformat()] * 2)

    def test_invalid_receipt_delivery_timestamp_refuses_even_when_finished_is_valid(self):
        _target(self.conn, status="succeeded", finished=NOW.isoformat(),
                receipt={"provider": "tiktok", "id": "receipt", "status": "delivered", "delivered_at": False})
        with self.assertRaises(pacing.PacingRefused):
            self._booked()

    def test_cancelled_accepted_posts_still_reserve_shared_destination_capacity(self):
        for index in range(3):
            _target(self.conn, status="cancelled", schedule="2036-01-01T12:00:00", started=NOW.isoformat(),
                    receipt={"provider": "tiktok", "id": f"accepted-{index}", "status": "processing",
                             "submitted_at": NOW.isoformat()})
        taken = self._booked()["shared"]
        self.assertEqual(taken, [NOW.isoformat()] * 3)
        slot = pacing.allocate_slot(taken, NOW, 0, configured_gap=30, configured_cap=3)
        self.assertEqual(slot, NOW + timedelta(days=1))

    def test_cancelled_confirmed_alias_receipts_count_once_not_zero(self):
        receipt = {"provider": "sociamonials", "id": "delivered-one", "status": "delivered",
                   "delivered_at": NOW.isoformat(), "workspace_id": "26985", "network": "tiktok",
                   "profile_refs": ["3917"]}
        _target(self.conn, status="cancelled", receipt=receipt)
        _target(self.conn, "account:451", status="cancelled", receipt=receipt)
        self.assertEqual(self._booked()["shared"], [NOW.isoformat()])

    def test_confirmed_terminal_non_delivery_does_not_reserve_a_publication_slot(self):
        _target(self.conn, status="failed", receipt={"provider": "tiktok", "id": "failed-one", "status": "failed",
                                                    "provider_status": "failed", "terminal_failure": True})
        self.assertEqual(self._booked(), {})


class DestinationAllocatorTests(unittest.TestCase):
    def _slot(self, taken, base=NOW, **kwargs):
        return pacing.allocate_slot(taken, base, 0, configured_gap=30, configured_cap=3, **kwargs)

    def test_publish_now_allocates_and_records_instead_of_bypassing_limits(self):
        taken = [NOW.isoformat()]
        slot = self._slot(taken, None, now_fn=lambda: NOW)
        self.assertEqual(slot, NOW + timedelta(minutes=30))
        self.assertEqual(taken[-1], slot.isoformat())
        full = [(NOW + timedelta(minutes=i * 30)).isoformat() for i in range(3)]
        self.assertEqual(self._slot(full, None, now_fn=lambda: NOW), NOW + timedelta(days=1))

    def test_requests_can_only_tighten_positive_cap_and_gap(self):
        full = [(NOW + timedelta(minutes=i * 30)).isoformat() for i in range(3)]
        for cap in (None, 0, 4, 999):
            self.assertEqual(self._slot(full.copy(), requested_cap=cap), NOW + timedelta(days=1))
        for gap in (None, 0, 1, 29):
            self.assertEqual(self._slot([NOW.isoformat()], requested_gap=gap), NOW + timedelta(minutes=30))
        self.assertEqual(self._slot([NOW.isoformat()], requested_gap=60), NOW + timedelta(hours=1))
        self.assertEqual(self._slot([NOW.isoformat()], requested_cap=1), NOW + timedelta(days=1))

    def test_configured_zero_cap_preserves_explicit_unlimited_policy(self):
        self.assertEqual(pacing.effective_daily_cap(0, None), 0)
        self.assertEqual(pacing.effective_daily_cap(0, 2), 2)
        taken = [(NOW + timedelta(minutes=i * 30)).isoformat() for i in range(4)]
        slot = pacing.allocate_slot(taken, NOW, 0, configured_gap=30, configured_cap=0)
        self.assertEqual(slot, NOW + timedelta(hours=2))

    def test_utc_offsets_and_operator_day_budget_are_consistent(self):
        taken = ["2035-01-02T08:00:00+08:00", "2035-01-02T00:30:00Z", "2035-01-02T01:00:00"]
        # Different UTC dates, the SAME operator day (UTC+8).
        base = datetime(2035, 1, 1, 23)
        slot = self._slot(taken, base, operator_timezone=timezone(timedelta(hours=8)))
        self.assertEqual(slot, datetime(2035, 1, 2, 23))
        self.assertIsNone(slot.tzinfo)

    def test_invalid_records_and_exhausted_horizon_refuse_without_mutation(self):
        for invalid in ("garbage", "2035-01-02", "2035-01-02T12:00:00+01:90", None):
            taken = [invalid]
            with self.subTest(invalid=invalid), self.assertRaises(pacing.PacingRefused):
                self._slot(taken)
            self.assertEqual(taken, [invalid])
        taken = [(NOW + timedelta(days=day, minutes=index * 30)).isoformat()
                 for day in range(3) for index in range(3)]
        before = taken.copy()
        with self.assertRaisesRegex(pacing.PacingRefused, "horizon exhausted"):
            self._slot(taken, horizon_days=1)
        self.assertEqual(taken, before)

    def test_allocation_keeps_gap_across_days_and_mutable_alias_lists(self):
        shared = []
        booked = {109: shared, 451: shared}
        slots = [self._slot(booked[ref]) for ref in (109, 451, 109, 451)]
        self.assertEqual(slots, [NOW, NOW + timedelta(minutes=30), NOW + timedelta(minutes=60), NOW + timedelta(days=1)])
        self.assertIs(booked[109], booked[451])

    def test_part_lower_bound_preserves_order_after_midnight_cap_rollover(self):
        taken = [(NOW + timedelta(minutes=30 * index)).isoformat() for index in range(3)]
        base = NOW.replace(hour=23, minute=55)
        first = pacing.allocate_slot(taken, base, 0, configured_gap=30, configured_cap=3)
        tail = pacing.allocate_slot(taken, base, 1, configured_gap=30, configured_cap=3, not_before=first)
        self.assertEqual(first, base + timedelta(days=1))
        self.assertEqual(tail, first + timedelta(minutes=30))


class DestinationQueueUnionTests(DestinationFixture):
    def test_active_native_destination_blocks_managed_alias_but_not_teaching(self):
        from myUtils import jobs
        _target(self.conn, status="running", lease="2036-01-01T00:00:00")
        _target(self.conn, "account:451", status="pending")
        _target(self.conn, "account:100", status="pending")
        self.assertEqual([row["account_ref"] for row in jobs._eligible_targets(self.conn, NOW.isoformat())],
                         ["account:100"])

    def test_batch_exclusion_uses_one_union_build_not_one_per_row(self):
        from myUtils import jobs
        _target(self.conn)
        _target(self.conn, "account:451")
        _target(self.conn, "account:100")
        with patch.object(jobs, "canonical_account_keys", wraps=identity.canonical_account_keys) as build:
            rows = list(jobs._eligible_targets(self.conn, NOW.isoformat(), excluded_accounts=("account:451",)))
        self.assertEqual([row["account_ref"] for row in rows], ["account:100"])
        self.assertEqual(build.call_count, 1)
        rows = list(jobs._eligible_targets(self.conn, NOW.isoformat()))
        self.assertEqual([row["account_ref"] for row in rows], ["account:109", "account:100"])


class DestinationOrchestratorLoaderTests(DestinationFixture):
    def test_loader_shares_new_alias_lists_and_reads_the_same_snapshot_as_recovery(self):
        from myUtils import publish_orchestrator as po
        from scripts import recover_failed_targets as recovery
        _target(self.conn, schedule="2035-01-02T12:00:00")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bookings.db"
            with closing(sqlite3.connect(path)) as disk:
                self.conn.commit()
                self.conn.backup(disk)
            before = path.read_bytes()
            keys = {}
            loaded = po._load_booked_slots(path, account_keys=keys, now=NOW)
            self.assertIs(loaded[109], loaded[451])
            self.assertIsNot(loaded[100], loaded[451])
            with recovery._connect(path) as disk, patch.object(recovery.job_runtime, "_now_iso", return_value=NOW.isoformat()):
                disk.execute("BEGIN")
                aliases = recovery._account_refs(disk)
                self.assertIn("account:451", aliases[keys["account:109"]])
                self.assertEqual(loaded[109], recovery._booked_slots(disk, aliases)[keys["account:109"]])
            loaded[451].append("2035-01-02T13:00:00")
            self.assertEqual(len(loaded[109]), 2)
            self.assertEqual(path.read_bytes(), before)

    def test_loader_missing_or_incompatible_database_fails_closed(self):
        from myUtils import publish_orchestrator as po
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            with self.assertRaises(FileNotFoundError):
                po._load_booked_slots(path)
            self.assertFalse(path.exists())
            with closing(sqlite3.connect(path)) as disk:
                disk.execute("CREATE TABLE unrelated(id INTEGER)")
            before = path.read_bytes()
            with self.assertRaises(sqlite3.Error):
                po._load_booked_slots(path)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
