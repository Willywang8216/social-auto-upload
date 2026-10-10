from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from scripts import archive_drive as archive  # noqa: E402


def entry(path: str, size: int = 10, modtime: str = "2026-10-07T17:46:59.808Z") -> dict:
    return {"Path": path, "Size": size, "ModTime": modtime}


class ParseYearMonthTests(unittest.TestCase):
    def test_parses_compact_year_month_from_bundle_name(self):
        self.assertEqual(archive.parse_year_month("my-compressed-2026-10"), (2026, 10))

    def test_parses_full_date(self):
        self.assertEqual(archive.parse_year_month("2026-10-07"), (2026, 10))

    def test_ignores_non_dates_and_impossible_months(self):
        self.assertIsNone(archive.parse_year_month("originals"))
        self.assertIsNone(archive.parse_year_month("clip-2026-13"))
        self.assertIsNone(archive.parse_year_month(None))

    def test_modtime_fallback(self):
        self.assertEqual(
            archive.year_month_from_modtime("2026-10-07T17:46:59.808Z"), (2026, 10)
        )
        self.assertIsNone(archive.year_month_from_modtime("not-a-date"))
        self.assertIsNone(archive.year_month_from_modtime(None))


class StructuredDetectionTests(unittest.TestCase):
    def test_detects_date_prefix(self):
        self.assertTrue(archive.is_structured("2026/10/my-compressed-2026-10/a.mp4"))

    def test_rejects_flat_bundle(self):
        self.assertFalse(archive.is_structured("my-compressed-2026-10/originals/a.mp4"))

    def test_partial_date_prefix_is_not_structured(self):
        self.assertFalse(archive.is_structured("2026/my-compressed-2026-10/a.mp4"))

    def test_layout_builds_date_prefixed_path_and_preserves_key(self):
        from myUtils import drive_layout

        self.assertEqual(drive_layout.archive_prefix(), "sau/archive")
        self.assertEqual(
            drive_layout.structured_archive_path(
                "my-compressed-2026-10/originals/nw/a.mp4", 2026, 10
            ),
            "sau/archive/2026/10/my-compressed-2026-10/originals/nw/a.mp4",
        )
        # Only the date prefix is added; the original key is untouched.
        self.assertTrue(
            drive_layout.structured_archive_path("bundle/x.mp4", 2026, 1).endswith(
                "/bundle/x.mp4"
            )
        )
        with self.assertRaises(ValueError):
            drive_layout.structured_archive_path("bundle/x.mp4", 2026, 13)
        with self.assertRaises(ValueError):
            drive_layout.structured_archive_path("", 2026, 1)


class PlanMovesTests(unittest.TestCase):
    def test_maps_flat_bundle_under_year_month(self):
        plan = archive.plan_moves(
            [entry("my-compressed-2026-10/originals/nw/a.mp4", size=99)],
            remote="drive",
            archive_prefix="sau/archive",
        )
        self.assertEqual(plan["summary"]["planned"], 1)
        move = plan["moves"][0]
        self.assertEqual(
            move["source"], "drive:sau/archive/my-compressed-2026-10/originals/nw/a.mp4"
        )
        self.assertEqual(
            move["destination"],
            "drive:sau/archive/2026/10/my-compressed-2026-10/originals/nw/a.mp4",
        )
        # The original key after the date prefix is preserved byte-for-byte.
        self.assertTrue(move["destination"].endswith(move["source"].split("sau/archive/", 1)[1]))

    def test_already_structured_objects_are_skipped(self):
        plan = archive.plan_moves(
            [entry("2026/10/bundle/a.mp4")], remote="drive", archive_prefix="sau/archive"
        )
        self.assertEqual(plan["moves"], [])
        self.assertEqual(plan["summary"]["already_structured"], 1)

    def test_undated_bundle_is_unresolved_without_default(self):
        plan = archive.plan_moves(
            [entry("kickoff/originals/a.mp4", modtime="not-a-date")],
            remote="drive",
            archive_prefix="sau/archive",
        )
        self.assertEqual(plan["moves"], [])
        self.assertEqual(plan["summary"]["unresolved"], 1)
        self.assertIn("--date", plan["unresolved"][0]["reason"])

    def test_default_date_resolves_undated_bundle(self):
        plan = archive.plan_moves(
            [entry("kickoff/originals/a.mp4", modtime="not-a-date")],
            default_year_month=(2026, 9),
            remote="drive",
            archive_prefix="sau/archive",
        )
        self.assertEqual(plan["moves"][0]["destination"], "drive:sau/archive/2026/09/kickoff/originals/a.mp4")

    def test_bundle_that_would_be_split_is_refused(self):
        # No date in the bundle name and two different ModTimes: without a
        # --date default the bundle would be scattered across two months, so
        # every object in it is reported unresolved instead.
        plan = archive.plan_moves(
            [
                entry("bundle/originals/a.mp4", modtime="2026-09-30T00:00:00Z"),
                entry("bundle/originals/b.mp4", modtime="2026-11-01T00:00:00Z"),
            ],
            remote="drive",
            archive_prefix="sau/archive",
        )
        self.assertEqual(plan["moves"], [])
        self.assertEqual(plan["summary"]["unresolved"], 2)
        self.assertIn("spans multiple months", plan["unresolved"][0]["reason"])

    def test_different_bundles_do_not_conflict(self):
        plan = archive.plan_moves(
            [
                entry("bundle-a/originals/a.mp4", modtime="2026-09-30T00:00:00Z"),
                entry("bundle-b/originals/b.mp4", modtime="2026-11-01T00:00:00Z"),
            ],
            remote="drive",
            archive_prefix="sau/archive",
        )
        self.assertEqual(plan["summary"]["unresolved"], 0)
        self.assertEqual(plan["summary"]["planned"], 2)


class ApplyPlanTests(unittest.TestCase):
    def test_dry_run_apply_touches_nothing(self):
        plan = {
            "moves": [
                {
                    "source": "drive:sau/archive/b/a.mp4",
                    "destination": "drive:sau/archive/2026/10/b/a.mp4",
                    "bytes": 10,
                }
            ]
        }
        with mock.patch.object(archive, "_run_rclone") as runner, mock.patch.object(
            archive, "_remote_size_bytes"
        ) as sizer:
            applied = archive.apply_plan(plan, config=Path("/tmp/rclone.conf"), dry_run_io=True)
        self.assertEqual(len(applied), 1)
        runner.assert_not_called()
        sizer.assert_not_called()

    def test_apply_verifies_before_and_after_moveto(self):
        plan = {
            "moves": [
                {
                    "source": "drive:sau/archive/b/a.mp4",
                    "destination": "drive:sau/archive/2026/10/b/a.mp4",
                    "bytes": 10,
                }
            ]
        }
        sizes = {"drive:sau/archive/b/a.mp4": 10, "drive:sau/archive/2026/10/b/a.mp4": 10}
        calls: list[str] = []
        with mock.patch.object(archive, "_remote_size_bytes", side_effect=lambda c, s: sizes[s]), \
             mock.patch.object(
                 archive,
                 "_run_rclone",
                 side_effect=lambda c, *a: calls.append(" ".join(a)) or subprocess.CompletedProcess([], 0, "", ""),
             ):
            applied = archive.apply_plan(plan, config=Path("/tmp/rclone.conf"))
        self.assertEqual(len(applied), 1)
        self.assertEqual(calls, ["moveto drive:sau/archive/b/a.mp4 drive:sau/archive/2026/10/b/a.mp4"])

    def test_apply_refuses_move_when_source_size_disagrees(self):
        plan = {
            "moves": [
                {
                    "source": "drive:sau/archive/b/a.mp4",
                    "destination": "drive:sau/archive/2026/10/b/a.mp4",
                    "bytes": 10,
                }
            ]
        }
        with mock.patch.object(archive, "_remote_size_bytes", return_value=9), mock.patch.object(
            archive, "_run_rclone"
        ) as runner:
            with self.assertRaises(RuntimeError):
                archive.apply_plan(plan, config=Path("/tmp/rclone.conf"))
        runner.assert_not_called()

    def test_apply_moves_back_when_destination_size_is_wrong(self):
        plan = {
            "moves": [
                {
                    "source": "drive:sau/archive/b/a.mp4",
                    "destination": "drive:sau/archive/2026/10/b/a.mp4",
                    "bytes": 10,
                }
            ]
        }
        sizes = {"drive:sau/archive/b/a.mp4": 10, "drive:sau/archive/2026/10/b/a.mp4": 7}
        calls: list[str] = []
        with mock.patch.object(archive, "_remote_size_bytes", side_effect=lambda c, s: sizes[s]), \
             mock.patch.object(
                 archive,
                 "_run_rclone",
                 side_effect=lambda c, *a: calls.append(" ".join(a)) or subprocess.CompletedProcess([], 0, "", ""),
             ):
            with self.assertRaises(RuntimeError):
                archive.apply_plan(plan, config=Path("/tmp/rclone.conf"))
        self.assertEqual(
            calls,
            [
                "moveto drive:sau/archive/b/a.mp4 drive:sau/archive/2026/10/b/a.mp4",
                "moveto drive:sau/archive/2026/10/b/a.mp4 drive:sau/archive/b/a.mp4",
            ],
        )


class CliDryRunTests(unittest.TestCase):
    def test_cli_dry_run_lists_and_plans_without_applying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "rclone"
            listing = json.dumps(
                [
                    {"Path": "bundle-2026-10/originals/a.mp4", "Size": 12, "ModTime": "2026-10-01T00:00:00Z"},
                    {"Path": "2026/10/bundle-2026-10/originals/b.mp4", "Size": 34, "ModTime": "2026-10-01T00:00:00Z"},
                ]
            )
            fake.write_text(f'#!/usr/bin/env bash\necho \'{listing}\'\n')
            fake.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            result = subprocess.run(
                [
                    sys.executable,
                    str(BASE_DIR / "scripts" / "archive_drive.py"),
                    "--json",
                    "--remote",
                    "drive",
                    "--prefix",
                    "sau/archive",
                ],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["mode"], "dry-run")
            self.assertEqual(plan["summary"]["planned"], 1)
            self.assertEqual(plan["summary"]["already_structured"], 1)
            self.assertEqual("applied" in plan, False)


if __name__ == "__main__":
    unittest.main()
