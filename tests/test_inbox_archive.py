"""Tests for myUtils.inbox_archive — the automatic Drive-inbox archiver.

Context: the archive step used to be manual, so `SAU-Inbox/` accumulated 404
objects, most of them published weeks earlier, and the operator's drop box
stopped telling them what was still pending. These tests pin the three rules
that make the automatic version safe:

  1. only media that a campaign has *taken responsibility for* is moved,
  2. the archived name follows ``<Title>_<SFW|NSFW>_<YYYYMMDD>`` and the note
     carries the campaign's copy,
  3. nothing is deleted when a move fails and the queue entry is only dropped
     once the move succeeded.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import inbox_archive


def _db(path: Path) -> None:
    """Minimal schema slice: campaigns → media group → file record → posts/targets."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, account_name TEXT);
        CREATE TABLE file_records (id INTEGER PRIMARY KEY, filename TEXT, file_path TEXT);
        CREATE TABLE media_groups (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE media_group_items (id INTEGER PRIMARY KEY, media_group_id INTEGER, file_record_id INTEGER);
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY, media_group_id INTEGER, status TEXT,
                                metadata_json TEXT, created_at TEXT, prepared_at TEXT);
        CREATE TABLE campaign_posts (id INTEGER PRIMARY KEY, campaign_id INTEGER, platform TEXT, draft_json TEXT);
        CREATE TABLE publish_job_targets (id INTEGER PRIMARY KEY, job_id INTEGER, account_ref TEXT,
                                          file_ref TEXT, status TEXT, schedule_at TEXT, finished_at TEXT,
                                          last_error TEXT);
        INSERT INTO accounts (id, account_name) VALUES (11, 'Nakedwill');
        """
    )
    conn.commit()
    conn.close()


def _campaign(db: Path, *, campaign_id: int, filename: str, title: str,
              statuses: list[str], schedule: str = "2026-10-22T13:00:00") -> None:
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO file_records (id, filename, file_path) VALUES (?, ?, ?)",
                 (campaign_id * 10, filename, f"videoFile/{filename}"))
    conn.execute("INSERT INTO media_groups (id, name) VALUES (?, ?)", (campaign_id, "g"))
    conn.execute("INSERT INTO media_group_items (media_group_id, file_record_id) VALUES (?, ?)",
                 (campaign_id, campaign_id * 10))
    conn.execute(
        "INSERT INTO campaigns (id, media_group_id, status, metadata_json) VALUES (?, ?, 'publishing', ?)",
        (campaign_id, campaign_id, json.dumps({"title": title, "notes": "brief text"})),
    )
    conn.execute(
        "INSERT INTO campaign_posts (id, campaign_id, platform, draft_json) VALUES (?, ?, 'twitter', ?)",
        (campaign_id, campaign_id, json.dumps({"message": "hello copy"}, ensure_ascii=False)),
    )
    for index, status in enumerate(statuses):
        conn.execute(
            "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status, schedule_at, finished_at) "
            "VALUES (?, 'account:11', ?, ?, ?, ?)",
            (campaign_id, f"campaign_post:{campaign_id}", status, schedule,
             "2026-10-22T13:05:00" if status == "succeeded" else None),
        )
    conn.commit()
    conn.close()


class PublishedMediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "db.sqlite"
        _db(self.db)

    def test_only_finished_campaigns_count_by_default(self):
        _campaign(self.db, campaign_id=5, filename="Clip__nsfw.mp4",
                  title="Clip", statuses=["succeeded", "pending"])
        published = inbox_archive.published_media(self.db)
        self.assertNotIn("Clip__nsfw.mp4", published)

        # A fully scheduled campaign is not "published" either…
        _campaign(self.db, campaign_id=6, filename="Other__sfw.mp4",
                  title="Other", statuses=["pending", "pending"])
        self.assertNotIn("Other__sfw.mp4", inbox_archive.published_media(self.db))
        # …unless the caller opts in (the operator wants an uncluttered inbox).
        scheduled = inbox_archive.published_media(self.db, include_scheduled=True)
        self.assertIn("Other__sfw.mp4", scheduled)
        self.assertFalse(scheduled["Other__sfw.mp4"]["published"])

    def test_succeeded_campaign_is_published(self):
        _campaign(self.db, campaign_id=7, filename="Done__sfw.mp4",
                  title="Done", statuses=["succeeded", "cancelled"])
        published = inbox_archive.published_media(self.db)
        self.assertIn("Done__sfw.mp4", published)
        self.assertTrue(published["Done__sfw.mp4"]["published"])

    def test_uuid_prefixed_upload_names_still_match(self):
        "Web-UI uploads store `<32hex>_<name>`; the inbox keeps `<name>`."
        _campaign(self.db, campaign_id=8,
                  filename="447a65987c8348e9b93c492f4d173f8e_Title 33__nsfw.jpg",
                  title="Title 33", statuses=["succeeded"])
        published = inbox_archive.published_media(self.db)
        self.assertIn("Title 33__nsfw.jpg", published)


class ArchiveNameTests(unittest.TestCase):
    def test_name_follows_the_convention_and_uses_the_first_slot(self):
        campaign = {"metadata": {"title": "Marble doesnt breathe"},
                    "first_success": "2026-10-22T13:40:00",
                    "first_schedule": "2026-10-22T13:40:00",
                    "published": True, "succeeded": 3, "campaign_id": 12}
        name, date_token = inbox_archive.archive_name(
            "both/video/morning__nsfw.mp4", {}, campaign
        )
        self.assertEqual(name, "Marble doesnt breathe_NSFW_20261022.mp4")
        self.assertEqual(date_token, "20261022")

    def test_unsafe_characters_are_neutralised(self):
        campaign = {"metadata": {"title": "a/b:c*d?"}, "first_schedule": "2026-11-02T21:00:00",
                    "published": False, "succeeded": 0, "campaign_id": 1}
        name, _ = inbox_archive.archive_name("nw/img/x__sfw.png", {}, campaign)
        self.assertEqual(name, "a-b-c-d-_SFW_20261102.png")

    def test_falls_back_to_the_filename_when_the_campaign_has_no_title(self):
        campaign = {"metadata": {}, "first_schedule": "2026-10-30T21:00:00",
                    "published": False, "succeeded": 0, "campaign_id": 1}
        name, _ = inbox_archive.archive_name("sw/img/截圖3__nsfw.jpg", {}, campaign)
        self.assertEqual(name, "截圖3_NSFW_20261030.jpg")


class PlanAndApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "db.sqlite"
        self.state = Path(self.tmp.name) / "state.json"
        _db(self.db)
        _campaign(self.db, campaign_id=9, filename="Published clip__sfw.mp4",
                  title="Published clip", statuses=["succeeded"])
        self.state.write_text(json.dumps({
            "version": 1,
            "processed": {},
            "ready": [{
                "id": "both:Published clip__sfw:2026-10-21T01:00:00+00:00",
                "persona": "both",
                "profileIds": [1, 3],
                "topic": "Published clip",
                "sfwFlag": "sfw",
                "remotePath": "both/video/Published clip__sfw.mp4",
                "sourcePath": "/home/will/sau-inbox/both/video/Published clip__sfw.mp4",
                "brief": "the brief",
            }],
            "pending": [],
            "quarantined": [],
        }), encoding="utf-8")

    def test_plan_contains_the_entry_and_its_note(self):
        plan = inbox_archive.plan_archives(self.db, self.state)
        self.assertEqual(len(plan), 1)
        item = plan[0]
        self.assertEqual(item["archived"], "Published clip_SFW_20261022.mp4")
        self.assertIn("Published clip", item["note"])
        self.assertIn("hello copy", item["note"])

    def test_failed_move_keeps_the_queue_entry_and_reports(self):
        plan = inbox_archive.plan_archives(self.db, self.state)
        failed = type("Result", (), {"returncode": 1, "stderr": "boom", "stdout": ""})()
        with patch.object(inbox_archive, "_rclone", return_value=failed):
            error = inbox_archive.apply_archive(plan[0], self.state)
        self.assertIsNotNone(error)
        self.assertIn("move failed", error)
        state = json.loads(self.state.read_text())
        self.assertEqual(len(state["ready"]), 1)

    def test_successful_move_drops_the_entry_and_marks_it_processed(self):
        plan = inbox_archive.plan_archives(self.db, self.state)
        ok = type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        with patch.object(inbox_archive, "_rclone", return_value=ok):
            error = inbox_archive.apply_archive(plan[0], self.state)
        self.assertIsNone(error)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["ready"], [])
        self.assertIn("drive:both/video/Published clip__sfw.mp4", state["processed"])


if __name__ == "__main__":
    unittest.main()
