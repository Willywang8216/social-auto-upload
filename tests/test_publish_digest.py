"""Tests for the daily publish-schedule digest (myUtils/publish_digest.py).

No alert channel is ever configured: every test injects a recording ``sender``
so delivery, reservation and idempotence can be asserted directly.
"""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from myUtils import publish_digest


SCHEMA = """
CREATE TABLE publish_jobs (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE publish_job_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    account_ref TEXT NOT NULL,
    file_ref TEXT NOT NULL,
    schedule_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE media_groups (
    id INTEGER PRIMARY KEY,
    name TEXT
);
CREATE TABLE campaigns (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER,
    media_group_id INTEGER
);
"""

# Noon in Shanghai on 2026-09-28 == 04:00 UTC.
NOW_UTC = datetime(2026, 9, 28, 4, 0, 0)  # naive -> treated as UTC
LOCAL_DAY = date(2026, 9, 28)


class RecordingSender:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, *, subject: str, body: str):
        self.calls.append({"subject": subject, "body": body})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@contextlib.contextmanager
def no_app_url():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SAU_PUBLIC_APP_URL", None)
        yield


class DigestTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "database.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    # -- DB helpers -------------------------------------------------------- #

    def add_job(self, job_id, platform, payload=None):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO publish_jobs (id, platform, payload_json) VALUES (?, ?, ?)",
                (job_id, platform, _json(payload or {})),
            )

    def add_target(self, job_id, account_ref, schedule_at, status="pending"):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO publish_job_targets "
                "(job_id, account_ref, file_ref, schedule_at, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, account_ref, f"campaign_post:{job_id}", schedule_at, status),
            )

    def add_campaign(self, campaign_id, media_group_id, name=None):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO media_groups (id, name) VALUES (?, ?)", (media_group_id, name))
            conn.execute(
                "INSERT INTO campaigns (id, profile_id, media_group_id) VALUES (?, ?, ?)",
                (campaign_id, 1, media_group_id),
            )

    def digest_row(self, day="2026-09-28"):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"SELECT * FROM {publish_digest.DIGEST_TABLE} WHERE local_date = ?", (day,)
            ).fetchone()

    def run_digest(self, sender, **kwargs):
        with no_app_url():
            return publish_digest.send_daily_digest(
                now=kwargs.pop("now", NOW_UTC),
                db_path=self.db,
                sender=sender,
                **kwargs,
            )

    # -- tests ------------------------------------------------------------- #

    def test_timezone_window_is_shanghai_local_day(self):
        # All four times sit around the Shanghai-day boundaries; schedule_at is
        # stored as naive UTC.
        self.add_job(1, "douyin")
        self.add_target(1, "account:in-start", "2026-09-27T16:00:00")   # 00:00 local, in
        self.add_target(1, "account:before", "2026-09-27T15:59:59")     # 23:59 prev, out
        self.add_target(1, "account:in-end", "2026-09-28T15:59:59")     # 23:59 local, in
        self.add_target(1, "account:after", "2026-09-28T16:00:00")      # 00:00 next, out

        sender = RecordingSender()
        result = self.run_digest(sender)

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["target_count"], 2)
        body = sender.calls[0]["body"]
        self.assertIn("account:in-start", body)
        self.assertIn("account:in-end", body)
        self.assertNotIn("account:before", body)
        self.assertNotIn("account:after", body)
        self.assertIn("00:00 · douyin · account:in-start", body)
        self.assertIn("23:59 · douyin · account:in-end", body)

    def test_campaign_targets_group_by_media_group_and_legacy_by_job(self):
        self.add_campaign(campaign_id=5, media_group_id=42, name="Morning reel")
        self.add_job(10, "tiktok", payload={"campaignId": 5})
        self.add_job(11, "instagram", payload={"campaignId": "5"})  # string id too
        self.add_target(10, "account:12", "2026-09-28T01:00:00")
        self.add_target(11, "account:5", "2026-09-28T01:30:00")

        self.add_job(12, "douyin", payload={})  # legacy: no campaignId
        self.add_target(12, "account:3", "2026-09-28T02:00:00")

        sender = RecordingSender()
        result = self.run_digest(sender, app_url="https://app.example.com")

        self.assertEqual(result["group_count"], 2)
        self.assertEqual(result["target_count"], 3)

        body = sender.calls[0]["body"]
        self.assertIn("[media-group:42] Morning reel (instagram, tiktok)", body)
        self.assertIn(
            "link: https://app.example.com/#/publish/calendar?entity=media-group:42", body
        )
        self.assertIn("[job:12] job #12 (douyin)", body)
        self.assertIn("link: https://app.example.com/#/publish/queue?job=12", body)
        # The two campaign jobs are folded together, not listed per job.
        self.assertEqual(body.count("[media-group:42]"), 1)
        self.assertNotIn("[job:10]", body)
        self.assertNotIn("[job:11]", body)

    def test_links_only_ever_come_from_public_app_url(self):
        self.add_job(1, "douyin")
        self.add_target(1, "account:3", "2026-09-28T02:00:00")

        # No app url configured: no link at all, and certainly no invented host.
        with no_app_url():
            digest = publish_digest.build_daily_digest(LOCAL_DAY, db_path=self.db)
        self.assertNotIn("http", digest["body"])
        self.assertIsNone(digest["groups"][0]["url"])

        # The env var is the source of truth when no override is passed.
        with patch.dict(os.environ, {"SAU_PUBLIC_APP_URL": "https://sau.example.net/"}):
            digest = publish_digest.build_daily_digest(LOCAL_DAY, db_path=self.db)
        self.assertEqual(
            digest["groups"][0]["url"],
            "https://sau.example.net/#/publish/queue?job=1",
        )

    def test_send_is_idempotent_per_local_day(self):
        self.add_job(1, "douyin")
        self.add_target(1, "account:3", "2026-09-28T02:00:00")

        sender = RecordingSender()
        first = self.run_digest(sender)
        second = self.run_digest(sender)

        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "skipped")
        self.assertFalse(second["sent"])
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(self.digest_row()["status"], publish_digest.STATE_SENT)
        # A different local day is a fresh slot.
        next_day = self.run_digest(sender, now=datetime(2026, 9, 29, 4, 0, 0))
        self.assertEqual(next_day["status"], "sent")
        self.assertEqual(len(sender.calls), 2)

    def test_failed_send_releases_reservation_for_retry(self):
        self.add_job(1, "douyin")
        self.add_target(1, "account:3", "2026-09-28T02:00:00")

        failing = RecordingSender(result=False)
        result = self.run_digest(failing)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["sent"])
        row = self.digest_row()
        self.assertEqual(row["status"], publish_digest.STATE_FAILED)

        # A raised sender is treated the same: released, not marked sent.
        raising = RecordingSender(result=RuntimeError("boom"))
        result = self.run_digest(raising)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.digest_row()["status"], publish_digest.STATE_FAILED)

        # The released slot is retakeable by the next run.
        good = RecordingSender()
        result = self.run_digest(good)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.digest_row()["status"], publish_digest.STATE_SENT)

    def test_only_pending_and_retrying_targets_are_scheduled(self):
        self.add_job(1, "douyin")
        self.add_target(1, "account:pending", "2026-09-28T02:00:00", status="pending")
        self.add_target(1, "account:retrying", "2026-09-28T03:00:00", status="retrying")
        for status in ("succeeded", "failed", "cancelled", "running"):
            self.add_target(1, f"account:{status}", "2026-09-28T04:00:00", status=status)

        sender = RecordingSender()
        result = self.run_digest(sender)

        self.assertEqual(result["target_count"], 2)
        body = sender.calls[0]["body"]
        self.assertIn("account:pending", body)
        self.assertIn("account:retrying", body)
        for status in ("succeeded", "failed", "cancelled", "running"):
            self.assertNotIn(f"account:{status}", body)

    def test_empty_schedule_still_sends_a_heartbeat(self):
        sender = RecordingSender()
        result = self.run_digest(sender)

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["target_count"], 0)
        self.assertEqual(result["group_count"], 0)
        self.assertIn("nothing scheduled", sender.calls[0]["subject"])
        self.assertIn("No publishes scheduled", sender.calls[0]["body"])

    def test_default_sender_delegates_to_ops_alerts(self):
        self.add_job(1, "douyin")
        self.add_target(1, "account:3", "2026-09-28T02:00:00")

        with patch.object(publish_digest.ops_alerts, "send_ops_alert", return_value=True) as alert:
            with no_app_url():
                result = publish_digest.send_daily_digest(now=NOW_UTC, db_path=self.db)

        self.assertTrue(result["sent"])
        alert.assert_called_once()
        _, kwargs = alert.call_args
        self.assertIn("2026-09-28", kwargs["subject"])
        self.assertIn("account:3", kwargs["body"])

    def test_cli_dry_run_previews_without_sending_or_reserving(self):
        self.add_job(1, "douyin")
        self.add_target(1, "account:3", "2026-09-28T02:00:00")

        stdout = io.StringIO()
        with patch.object(publish_digest.ops_alerts, "send_ops_alert") as alert:
            with no_app_url(), contextlib.redirect_stdout(stdout):
                code = publish_digest.main(["--dry-run", "--db-path", str(self.db), "--date", "2026-09-28"])

        self.assertEqual(code, 0)
        alert.assert_not_called()
        self.assertIn("account:3", stdout.getvalue())
        # Dry run must not claim the day's slot.
        self.assertFalse(publish_digest.DIGEST_TABLE in {row[0] for row in sqlite3.connect(self.db).execute("SELECT name FROM sqlite_master WHERE type='table'")})


def _json(value) -> str:
    import json

    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
