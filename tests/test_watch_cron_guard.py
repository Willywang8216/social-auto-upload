"""The watcher must notice when the operator's crontab stops scheduling the pipeline.

On 2026-10-28 the whole user crontab was replaced with a single bisync line. The
pipeline monitors, the deployer, the Drive offload, the auto-archive/auto-publish
and the daily backups all stopped, and nothing said a word for a day and a half —
because every job that would have raised the alarm was one of the jobs that
disappeared. The watcher itself is scheduled from ``/etc/cron.d/sau-inbox-watch``
(a different file), so it is the one process that can still speak up.
"""

from __future__ import annotations

import importlib.util
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def _load_watch():
    """Import the repo copy of the watcher with its sibling modules importable."""
    sys.path.insert(0, str(REPO / "sau_inbox"))
    spec = importlib.util.spec_from_file_location("watch_under_test", REPO / "sau_inbox" / "watch.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["watch_under_test"] = module
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


class CronGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.watch = _load_watch()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.health = Path(self.tmp.name) / "health.json"
        self.alerts: list[tuple[str, str]] = []

    def _run(self, crontab_text: str | None, *, returncode: int = 0):
        def runner(*_args, **_kwargs):
            if crontab_text is None:
                raise FileNotFoundError("crontab")
            return _Result(crontab_text, returncode)

        with patch.object(self.watch, "HEALTH_PATH", self.health), \
             patch.object(self.watch, "notify", lambda title, body: self.alerts.append((title, body)) or True):
            return self.watch.check_cron_guard(runner=runner)

    def test_a_full_crontab_is_healthy_and_silent(self):
        text = "\n".join(
            f"*/5 * * * * /bin/bash /home/will/social-auto-upload/{marker}"
            for _, marker in self.watch.CRON_GUARD_MARKERS
        )
        self.assertEqual(self._run(text), [])
        self.assertEqual(self.alerts, [])

    def test_a_wiped_crontab_alerts_with_every_missing_job(self):
        # the exact 2026-10-28 state: one bisync line and nothing else
        missing = self._run("0 * * * * /home/will/social-auto-upload/scripts/bisync_onedrive.sh\n")
        self.assertIn("pipeline monitor", missing)
        self.assertIn("ci-watch", missing)
        self.assertIn("offload", missing)
        self.assertEqual(len(self.alerts), 1)
        title, body = self.alerts[0]
        self.assertIn("排程", title)
        self.assertIn("ci-watch", body)
        self.assertIn("crontab-keep", body)  # tells the operator where the backup is

    def test_commented_out_lines_do_not_count_as_scheduled(self):
        text = "# */5 * * * * /home/will/social-auto-upload/scripts/ci-watch.sh\n"
        self.assertIn("ci-watch", self._run(text))

    def test_the_check_is_rate_limited(self):
        text = "0 * * * * /bin/bash bisync_onedrive.sh\n"
        self._run(text)
        self.alerts.clear()
        # a second call inside the interval must not re-read or re-alert
        self.assertEqual(self._run(text), [])
        self.assertEqual(self.alerts, [])
        state = json.loads(self.health.read_text(encoding="utf-8"))
        self.assertIn("cron_guard_at", state)

    def test_a_host_without_crontab_is_left_alone(self):
        self.assertEqual(self._run(None), [])
        self.assertEqual(self.alerts, [])

    def test_a_failing_crontab_command_is_left_alone(self):
        self.assertEqual(self._run("", returncode=1), [])
        self.assertEqual(self.alerts, [])

    def test_missing_jobs_are_recorded_in_the_health_file(self):
        self._run("0 * * * * bisync_onedrive.sh\n")
        state = json.loads(self.health.read_text(encoding="utf-8"))
        self.assertIn("pipeline monitor", state["cron_guard_missing"])


if __name__ == "__main__":
    unittest.main()
