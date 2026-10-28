"""Tests for myUtils.pipeline_monitor — the "tell me when it breaks" job.

The 2026-10-17..20 outage survived four days because nothing watched the
pipeline. These tests pin the checks that would have caught it (watcher
heartbeat missing/stale, needs_review campaigns, overdue targets) and the
dedupe that stops a permanent outage from becoming 96 messages a day.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import pipeline_monitor


def _db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY, profile_id INTEGER, status TEXT,
                                last_error TEXT, created_at TEXT, prepared_at TEXT,
                                metadata_json TEXT);
        CREATE TABLE publish_job_targets (id INTEGER PRIMARY KEY, job_id INTEGER, account_ref TEXT,
                                          file_ref TEXT, status TEXT, schedule_at TEXT,
                                          finished_at TEXT, last_error TEXT);
        -- The language-routing audit reads these; a partial schema made the audit
        -- fail and the monitor report a phantom problem on a healthy box.
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, account_name TEXT, platform TEXT,
                               config_json TEXT);
        CREATE TABLE file_records (id INTEGER PRIMARY KEY, filename TEXT);
        CREATE TABLE media_groups (id INTEGER PRIMARY KEY);
        CREATE TABLE media_group_items (media_group_id INTEGER, file_record_id INTEGER);
        CREATE TABLE campaign_artifacts (id INTEGER PRIMARY KEY, campaign_id INTEGER,
                                         artifact_kind TEXT, local_path TEXT, metadata_json TEXT);
        CREATE TABLE campaign_posts (id INTEGER PRIMARY KEY, campaign_id INTEGER, draft_json TEXT);
        """
    )
    conn.commit()
    conn.close()


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        (self.repo / "logs").mkdir(parents=True)
        (self.repo / "logs" / "offload.log").write_text(
            "[2026-10-21T01:17:01Z] offload start\n"
            "[2026-10-21T01:17:05Z] offload done rc=0 local videoFile=12 uploads=0\n",
            encoding="utf-8",
        )
        self.inbox = root / "inbox"
        self.inbox.mkdir()
        (self.inbox / ".watch-health.json").write_text(json.dumps({
            "last_success_epoch": time.time(), "last_success": "2026-10-21T01:00:00+00:00",
        }), encoding="utf-8")
        self.db = root / "db.sqlite"
        _db(self.db)

    def _collect(self, **kwargs):
        # The health and STT checks both hit the network. A test must not depend
        # on a live gateway: when the STT provider really is down (it was, for
        # days in October 2026) the "healthy box" test failed for a reason that
        # had nothing to do with the code under test.
        with patch.object(pipeline_monitor, "_check_healthz", return_value=None), \
             patch.object(pipeline_monitor, "_check_stt", return_value=[]):
            return pipeline_monitor.collect_problems(
                self.db, self.repo, self.inbox, **kwargs
            )

    def test_healthy_box_reports_nothing(self):
        self.assertEqual(self._collect(), {})

    def test_stt_probe_is_silent_when_unconfigured(self):
        with patch.dict(os.environ, {"SAU_STT_BASE_URL": "", "SAU_STT_API_KEY": ""}):
            self.assertEqual(pipeline_monitor._check_stt(), [])

    def test_stt_probe_reports_a_rejected_gateway(self):
        import urllib.error

        error = urllib.error.HTTPError(
            "https://stt.example/v1/chat/completions", 503, "Service Unavailable", None,
            io.BytesIO(b'{"error":{"code":"model_not_found"}}'),
        )
        with patch.dict(os.environ, {"SAU_STT_BASE_URL": "https://stt.example",
                                     "SAU_STT_API_KEY": "k",
                                     "SAU_STT_MODEL": "mimo-v2.6-flash"}), \
             patch("urllib.request.urlopen", side_effect=error):
            problems = pipeline_monitor._check_stt()
        self.assertEqual(len(problems), 1)
        self.assertIn("503", problems[0])
        self.assertIn("model_not_found", problems[0])

    def test_stt_probe_is_silent_on_success(self):
        class _Response:
            def read(self, _n=-1):
                return b'{"choices":[]}'
            def __enter__(self):
                return self
            def __exit__(self, *_exc):
                return False

        with patch.dict(os.environ, {"SAU_STT_BASE_URL": "https://stt.example",
                                     "SAU_STT_API_KEY": "k"}), \
             patch("urllib.request.urlopen", return_value=_Response()):
            self.assertEqual(pipeline_monitor._check_stt(), [])

    def test_stale_watcher_heartbeat_is_reported(self):
        (self.inbox / ".watch-health.json").write_text(json.dumps({
            "last_success_epoch": time.time() - 4 * 3600,
        }), encoding="utf-8")
        problems = self._collect()
        self.assertIn("inbox-watcher", problems)
        self.assertIn("240 min", problems["inbox-watcher"][0])

    def test_missing_heartbeat_file_is_reported(self):
        (self.inbox / ".watch-health.json").unlink()
        self.assertIn("inbox-watcher", self._collect())

    def test_needs_review_campaign_is_reported(self):
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO campaigns (id, profile_id, status, last_error, created_at) "
            "VALUES (42, 1, 'needs_review', 'prep failed: boom', ?)", (now,))
        conn.commit()
        conn.close()
        problems = self._collect()
        self.assertIn("needs-review", problems)
        self.assertIn("42", problems["needs-review"][0])

    def test_overdue_pending_targets_are_reported(self):
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(hours=3)).isoformat(
            timespec="seconds")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status, schedule_at) "
            "VALUES (1, 'account:11', 'campaign_post:1', 'pending', ?)", (past,))
        conn.commit()
        conn.close()
        problems = self._collect(overdue_min=45)
        self.assertIn("overdue", problems)

    def test_operator_disabled_platform_is_not_a_failure(self):
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status, finished_at, last_error) "
            "VALUES (1, 'account:11', 'x', 'failed', ?, 'Reddit publishing is disabled by the operator')",
            (now,))
        conn.commit()
        conn.close()
        self.assertNotIn("publish-failures", self._collect())

    def test_full_disk_is_reported_before_it_breaks_publishing(self):
        """A full disk breaks SQLite/ffmpeg silently, so it must alert early."""
        from collections import namedtuple

        Stats = namedtuple("Stats", "f_blocks f_frsize f_bavail")
        with patch("os.statvfs", return_value=Stats(f_blocks=100 * 10**9 // 4096,
                                                    f_frsize=4096,
                                                    f_bavail=5 * 10**9 // 4096)):
            problems = pipeline_monitor._check_disk(self.repo)
        self.assertTrue(problems)
        self.assertIn("95%", problems[0])
        self.assertIn("failing", problems[0])

    def test_healthy_disk_is_silent(self):
        from collections import namedtuple

        Stats = namedtuple("Stats", "f_blocks f_frsize f_bavail")
        with patch("os.statvfs", return_value=Stats(f_blocks=100 * 10**9 // 4096,
                                                    f_frsize=4096,
                                                    f_bavail=50 * 10**9 // 4096)):
            self.assertEqual(pipeline_monitor._check_disk(self.repo), [])

    def test_repeated_alert_is_deduped_by_cooldown(self):
        state_path = Path(self.tmp.name) / "monitor-state.json"
        sent = []
        past = time.time() - 7200
        from datetime import datetime, timedelta, timezone
        stamp = datetime.fromtimestamp(past).isoformat(timespec="seconds")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO campaigns (id, profile_id, status, last_error, created_at) "
            "VALUES (77, 1, 'needs_review', 'prep failed', ?)", (stamp,))
        conn.commit()
        conn.close()

        with patch.object(pipeline_monitor, "STATE_PATH", state_path), \
             patch.object(pipeline_monitor, "_check_healthz", return_value=None), \
             patch.object(pipeline_monitor, "_check_stt", return_value=[]), \
             patch("myUtils.ops_alerts.send_ops_alert",
                   side_effect=lambda **kw: sent.append(kw) or True):
            argv = ["--db", str(self.db), "--repo", str(self.repo),
                    "--inbox", str(self.inbox), "--review-hours", "48"]
            pipeline_monitor.main(argv)
            pipeline_monitor.main(argv)
        self.assertEqual(len(sent), 1, "second run must be suppressed by the cooldown")


if __name__ == "__main__":
    unittest.main()
