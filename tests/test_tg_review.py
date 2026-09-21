"""Tests for the Telegram pre-publish review cards.

The network is never touched: ``notify_posts`` talks through ``_send_media`` /
``_send_text``, which the tests patch, and the reply path is exercised through
``handle_reply`` with the job-runtime callables injected — that is exactly the
seam the backend uses, so these cover the real behaviour.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from myUtils import tg_review


SCHEMA = """
CREATE TABLE campaign_posts (
    id INTEGER PRIMARY KEY,
    draft_json TEXT,
    status TEXT,
    updated_at TEXT
);
CREATE TABLE publish_jobs (
    id INTEGER PRIMARY KEY,
    payload_json TEXT
);
CREATE TABLE publish_job_targets (
    id INTEGER PRIMARY KEY,
    job_id INTEGER,
    account_ref TEXT,
    schedule_at TEXT,
    status TEXT
);
CREATE TABLE tg_review_messages (
    message_id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL DEFAULT '',
    campaign_id INTEGER,
    post_id INTEGER,
    job_id INTEGER,
    profile_id INTEGER,
    media_path TEXT NOT NULL DEFAULT '',
    copy_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE tg_review_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class TgReviewFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO campaign_posts (id, draft_json, status) VALUES (7, ?, 'ready')",
            (json.dumps({"message": "original copy"}),),
        )
        payload = {
            "campaignId": 3,
            "campaignPostId": 7,
            "message": "original copy",
            "draft": {"message": "original copy"},
        }
        conn.execute(
            "INSERT INTO publish_jobs (id, payload_json) VALUES (42, ?)",
            (json.dumps(payload),),
        )
        conn.execute(
            "INSERT INTO publish_job_targets (id, job_id, account_ref, schedule_at, status) "
            "VALUES (100, 42, 'account:118', '2026-10-05T13:00:00', 'pending')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def _draft(self) -> str:
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT draft_json FROM campaign_posts WHERE id = 7").fetchone()
        conn.close()
        return json.loads(row[0])["message"]

    def _payload_message(self) -> str:
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT payload_json FROM publish_jobs WHERE id = 42").fetchone()
        conn.close()
        return json.loads(row[0])["message"]


class ParseCommandTest(unittest.TestCase):
    def test_plain_text_is_a_copy_replacement(self):
        self.assertEqual(
            tg_review.parse_command("Fresh words for the feed"),
            ("edit", "Fresh words for the feed"),
        )

    def test_verbs(self):
        self.assertEqual(tg_review.parse_command("OK"), ("ack", ""))
        self.assertEqual(tg_review.parse_command("pause"), ("pause", ""))
        self.assertEqual(tg_review.parse_command("CANCEL"), ("cancel", ""))
        self.assertEqual(tg_review.parse_command("SHOW"), ("show", ""))
        self.assertEqual(
            tg_review.parse_command("TIME 2026-10-06T09:00:00"),
            ("time", "2026-10-06T09:00:00"),
        )
        self.assertEqual(tg_review.parse_command("EDIT new words"), ("edit", "new words"))


class ApplyCopyTest(TgReviewFixture):
    def test_reply_rewrites_both_the_post_and_the_queued_payload(self):
        changed = tg_review.apply_copy(7, "rewritten copy", db_path=self.db)
        self.assertEqual(changed, 2)  # campaign_posts + publish_jobs
        self.assertEqual(self._draft(), "rewritten copy")
        # The worker sends payload_json, so that one is what really matters.
        self.assertEqual(self._payload_message(), "rewritten copy")


class HandleReplyTest(TgReviewFixture):
    def _record(self, message_id: int = 500) -> None:
        tg_review._remember_message(
            db_path=self.db, chat_id="1", message_id=message_id,
            campaign_id=3, post_id=7, job_id=42, profile_id=1,
            media_path="", copy_text="original copy",
        )

    def test_reply_to_a_tracked_card_edits_the_copy(self):
        self._record()
        outcome = tg_review.handle_reply(500, "brand new sentence", db_path=self.db)
        self.assertIn("copy updated", outcome)
        self.assertEqual(self._draft(), "brand new sentence")

    def test_reply_to_an_unknown_message_is_ignored(self):
        self.assertEqual(tg_review.handle_reply(999, "hello", db_path=self.db), "")
        self.assertEqual(self._draft(), "original copy")

    def test_reply_ok_changes_nothing(self):
        self._record()
        outcome = tg_review.handle_reply(500, "OK", db_path=self.db)
        self.assertIn("left as-is", outcome)
        self.assertEqual(self._draft(), "original copy")

    def test_reply_pause_cancels_the_queued_targets(self):
        self._record()
        cancelled: list[int] = []
        outcome = tg_review.handle_reply(
            500, "PAUSE", db_path=self.db, cancel_target=cancelled.append
        )
        self.assertIn("paused", outcome)
        self.assertEqual(cancelled, [100])
        self.assertEqual(self._draft(), "original copy")

    def test_reply_time_moves_the_queued_targets(self):
        self._record()
        moves: list[tuple[int, str]] = []
        outcome = tg_review.handle_reply(
            500, "TIME 2026-10-09T08:00:00", db_path=self.db,
            reschedule_target=lambda target_id, when: moves.append((target_id, when)),
        )
        self.assertIn("moved to 2026-10-09T08:00:00", outcome)
        self.assertEqual(moves, [(100, "2026-10-09T08:00:00")])


class NotifyPostsTest(TgReviewFixture):
    def test_sends_media_card_and_records_the_message(self):
        media = Path(self._tmp.name) / "clip.mp4"
        media.write_bytes(b"x" * 1024)
        sent_media: list[dict] = []
        sent_text: list[str] = []

        with patch.dict("os.environ", {
            "SAU_TG_REVIEW_BOT_TOKEN": "token",
            "SAU_TG_REVIEW_CHAT_ID": "8633483147",
        }), patch.object(tg_review, "_send_media",
                         side_effect=lambda chat, m, cap: (sent_media.append({"cap": cap, "media": m})
                                                           or {"message_id": 777})), \
                patch.object(tg_review, "_send_text",
                             side_effect=lambda chat, text: (sent_text.append(text) or {"message_id": 778})):
            count = tg_review.notify_posts(cards=[{
                "job_id": 42, "campaign_id": 3, "post_id": 7, "profile_id": 1,
                "profile_label": "nakedwill (#1)",
                "schedule_label": "2026-10-05T13:00:00 UTC",
                "account_labels": ["bluesky — Nakedwill EN"],
                "copy_text": "short copy",
                "media_path": str(media),
                "public_url": "https://example.test/clip.mp4",
            }], db_path=self.db)

        self.assertEqual(count, 1)
        self.assertEqual(sent_media[0]["media"], media)
        # The single card carries the destination and the copy.
        self.assertIn("nakedwill (#1)", sent_media[0]["cap"])
        self.assertIn("bluesky — Nakedwill EN", sent_media[0]["cap"])
        self.assertIn("short copy", sent_media[0]["cap"])
        self.assertIn("https://example.test/clip.mp4", sent_media[0]["cap"])
        # No separate copy message for a short copy.
        self.assertEqual(sent_text, [])

        record = tg_review.lookup_message(777, db_path=self.db)
        self.assertEqual(record["post_id"], 7)
        self.assertEqual(record["job_id"], 42)

    def test_long_copy_also_goes_out_as_its_own_message(self):
        long_copy = "y" * 900
        with patch.dict("os.environ", {
            "SAU_TG_REVIEW_BOT_TOKEN": "token",
            "SAU_TG_REVIEW_CHAT_ID": "1",
        }), patch.object(tg_review, "_send_media", return_value={"message_id": 1}), \
                patch.object(tg_review, "_send_text", return_value={"message_id": 2}) as text_send:
            tg_review.notify_posts(cards=[{
                "job_id": 42, "post_id": 7, "profile_id": 1,
                "profile_label": "p", "schedule_label": "now",
                "account_labels": [], "copy_text": long_copy,
                "media_path": None, "public_url": "",
            }], db_path=self.db)
        self.assertEqual(text_send.call_count, 1)
        self.assertIn(long_copy, text_send.call_args[0][1])

    def test_nothing_is_sent_when_telegram_is_not_configured(self):
        with patch.dict("os.environ", {
            "SAU_TG_REVIEW_BOT_TOKEN": "",
            "SAU_ALERT_TELEGRAM_BOT_TOKEN": "",
        }):
            self.assertEqual(tg_review.notify_posts(cards=[{"post_id": 1}], db_path=self.db), 0)


class CaptionTest(unittest.TestCase):
    def test_caption_stays_within_the_telegram_limit(self):
        caption = tg_review.build_caption(
            profile_label="p", schedule_label="s", account_labels=["a"],
            media_name="m.mp4", media_size_mb=1.5, public_url="",
            copy_text="z" * 4000, copy_in_caption=True,
        )
        self.assertLessEqual(len(caption), tg_review.CAPTION_LIMIT)


if __name__ == "__main__":
    unittest.main()
