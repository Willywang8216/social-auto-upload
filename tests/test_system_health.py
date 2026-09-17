"""Tests for myUtils.system_health.collect().

Seeds a scratch publish DB, a scratch SAU-Inbox state (1 ready item), and a
fake offload log (one completed run with rc=0), then asserts collect() surfaces
all three. The DB is bootstrapped with db.createTable.bootstrap() and one job
is enqueued via myUtils.jobs; inbox_ops is re-imported (via importlib.reload,
mirroring how the module resolves INBOX_DIR/STATE_PATH from env).

All pure, no network.
"""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import db.createTable as create_table
from myUtils import jobs


class SystemHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)

        # Publish DB.
        self._db_path = self._root / "system.db"
        create_table.bootstrap(self._db_path)

        # SAU-Inbox state with one ready item.
        self._inbox_dir = self._root / "inbox"
        self._inbox_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._inbox_dir / "state.json"
        self._state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "processed": {},
                    "ready": [
                        {
                            "id": "nw:pilot:2026-09-10T10:00:00",
                            "persona": "nw",
                            "profileIds": [1],
                            "topic": "pilot",
                            "sfwFlag": "sfw",
                            "kind": "video",
                            "sourcePath": str(self._inbox_dir / "nw/video/pilot_sfw.mp4"),
                            "brief": "first pipeline test",
                            "status": "ready",
                            "createdAt": "2026-09-10T10:00:00",
                        }
                    ],
                    "pending": [],
                    "quarantined": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # Fake offload log: one completed run (rc=0, 369 files) + a failed
        # older run. collect() must pick the last one. Timestamp prefixes are
        # padded to exactly 25 chars so the line[:25] contract holds.
        self._offload_log = self._root / "offload.log"
        self._offload_log.write_text(
            "2026-09-10T10:00:00+00:00 offload started\n"
            "2026-09-10T10:05:00+00:00 offload done rc=1 local videoFile=42\n"
            "2026-09-10T11:05:00+00:00 offload started\n"
            "2026-09-10T11:10:00+00:00 offload done rc=0 local videoFile=369\n",
            encoding="utf-8",
        )

        # Fake digest log (digest.log next to offload.log).
        self._digest_log = self._root / "digest.log"
        self._digest_log.write_text(
            "2026-09-10T08:00:00+00:00 digest sent=ok\n", encoding="utf-8"
        )

        os.environ["SAU_INBOX"] = str(self._inbox_dir)
        os.environ["SAU_WATCH_STATE"] = str(self._state_path)
        os.environ["SAU_OFFLOAD_LOG"] = str(self._offload_log)
        os.environ["SAU_SYSTEM_DB"] = str(self._db_path)

        # Track env bindings so the build-time env really picks them up.
        import myUtils.inbox_ops as inbox_ops
        importlib.reload(inbox_ops)
        import myUtils.system_health as system_health
        self._system_health = importlib.reload(system_health)

        # One enqueued (pending) publish job with two pending targets.
        spec = jobs.JobSpec(
            platform="douyin",
            payload={"title": "health"},
            targets=[("acct-1", "file-1", None), ("acct-2", "file-2", None)],
            idempotency_key="system-health-test-1",
        )
        jobs.enqueue_job(spec, db_path=self._db_path)

    def _close_db_handles(self) -> None:
        """Close any live sqlite3.Connection pointing at the scratch DB.

        ``db.createTable.bootstrap`` opens its connections with ``with
        sqlite3.connect(...)`` which commits but never explicitly closes, and
        the Connection objects can outlive bootstrap()'s own frame (pytest /
        gc machinery keeps them referenced). On Windows an open handle blocks
        TemporaryDirectory.cleanup() with WinError 32, so we defensively close
        every live handle bound to our scratch file.
        """
        import gc

        import sqlite3 as _sqlite3

        target = str(self._db_path.resolve())
        for obj in gc.get_objects():
            if not isinstance(obj, _sqlite3.Connection):
                continue
            try:
                rows = obj.execute("PRAGMA database_list").fetchall()
            except _sqlite3.ProgrammingError:
                continue  # already closed
            if any(str(r[2]) == target for r in rows):
                try:
                    obj.close()
                except _sqlite3.Error:
                    pass

    def tearDown(self) -> None:
        for key in (
            "SAU_INBOX",
            "SAU_WATCH_STATE",
            "SAU_OFFLOAD_LOG",
            "SAU_SYSTEM_DB",
        ):
            os.environ.pop(key, None)
        self._close_db_handles()
        self._tmp.cleanup()

    def _collect(self) -> dict:
        return self._system_health.collect()

    def test_collect_offload_healthy(self) -> None:
        out = self._collect()
        offload = out["offload"]
        self.assertIs(offload["healthy"], True)
        self.assertEqual(offload["exitCode"], 0)
        self.assertEqual(offload["localFiles"], 369)
        self.assertEqual(offload["lastRun"], "2026-09-10T11:10:00+00:00")

    def test_collect_inbox_ready(self) -> None:
        out = self._collect()
        inbox = out["inbox"]
        self.assertEqual(inbox["ready"], 1)
        self.assertEqual(inbox["pending"], 0)
        self.assertEqual(inbox["quarantined"], 0)

    def test_collect_publish_targets_pending(self) -> None:
        out = self._collect()
        self.assertEqual(out["publish"]["targetsByStatus"].get("pending"), 2)
        self.assertEqual(out["publish"]["jobsByStatus"].get("pending"), 1)

    def test_collect_digest(self) -> None:
        out = self._collect()
        self.assertEqual(out["digest"]["lastSent"], "2026-09-10T08:00:00+00:00")

    def test_collect_missing_db_returns_safe_defaults(self) -> None:
        prev = os.environ.get("SAU_SYSTEM_DB")
        os.environ["SAU_SYSTEM_DB"] = str(self._root / "does-not-exist.db")
        try:
            out = self._collect()
            self.assertEqual(out["publish"]["targetsByStatus"], {})
            self.assertEqual(out["publish"]["jobsByStatus"], {})
        finally:
            if prev is None:
                os.environ.pop("SAU_SYSTEM_DB", None)
            else:
                os.environ["SAU_SYSTEM_DB"] = prev


if __name__ == "__main__":
    unittest.main()