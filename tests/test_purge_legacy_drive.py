"""Safety tests for the legacy Drive purge classifier.

classify() decides whether a Drive object may be quarantined. Getting it wrong in
the permissive direction destroys the source media of a scheduled post, so the
tests below pin the protective behaviour, not just the happy path.

Context: of 453 `sau/videoFile` objects with no file_records mapping, 310 are
cited elsewhere in the database. A classifier that looked only at the mapping
would have quarantined them.

The second half tests the inverse direction: `--restore` must only ever move a
trash object back after verifying it, and must never clobber a live legacy
object that reappeared at the same path.
"""

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "purge_legacy_drive", REPO_ROOT / "scripts" / "purge_legacy_drive.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


def _refs(*, file_keys=(), live_payload=(), live_campaign=(),
          live_payload_basenames=(), live_campaign_basenames=()):
    """A minimal `refs` dict with only the videoFile root populated."""
    return {
        "file_record_keys": {"videoFile": set(file_keys)},
        "file_record_basenames": {"videoFile": set()},
        "live_payload_keys": set(live_payload),
        "live_campaign_keys": set(live_campaign),
        "live_payload_basenames": set(live_payload_basenames),
        "live_campaign_basenames": set(live_campaign_basenames),
    }


def _classify(key, **kwargs):
    rows = mod.classify(
        root="videoFile",
        entries=[{"path": key, "size": 10}],
        refs=_refs(**kwargs),
    )
    return rows[0]


class LegacyPurgeClassifierTests(unittest.TestCase):
    def test_a_mapped_object_is_needed(self):
        row = _classify("a.mp4", file_keys=["a.mp4"])
        self.assertTrue(row["needed"])
        self.assertIn("file_records", row["strong"])

    def test_an_object_a_live_payload_needs_is_needed(self):
        # The case that matters: no file_records mapping, but a pending target
        # names it.
        row = _classify("a.mp4", live_payload=[("videoFile", "a.mp4")])
        self.assertTrue(row["needed"])
        self.assertIn("live_payload", row["strong"])

    def test_an_object_a_live_campaign_needs_is_needed(self):
        row = _classify("a.mp4", live_campaign=[("videoFile", "a.mp4")])
        self.assertTrue(row["needed"])

    def test_a_basename_match_alone_still_protects(self):
        # A re-rooted path: the same file under a different drive key. Cannot be
        # matched exactly, so the weak signal must keep it.
        row = _classify(
            "sub/dir/a.mp4",
            live_payload_basenames=[("videoFile", "a.mp4")],
        )
        self.assertTrue(row["needed"])
        self.assertEqual(row["strong"], [])
        self.assertIn("live_payload_basename", row["weak"])

    def test_an_uncited_object_is_not_needed(self):
        # Only this direction may be quarantined.
        row = _classify("orphan.mp4")
        self.assertFalse(row["needed"])
        self.assertEqual(row["strong"], [])
        self.assertEqual(row["weak"], [])

    def test_a_file_record_basename_collision_is_flagged_for_review(self):
        refs = _refs()
        refs["file_record_basenames"] = {"videoFile": {"a.mp4"}}
        row = mod.classify(
            root="videoFile", entries=[{"path": "sub/a.mp4", "size": 1}], refs=refs
        )[0]
        # Not automatically needed (no exact or live reference), but surfaced so a
        # human reviews the possible duplicate.
        self.assertFalse(row["needed"])
        self.assertTrue(row["file_record_basename_only"])

    def test_duplicate_keys_are_marked(self):
        rows = mod.classify(
            root="videoFile",
            entries=[{"path": "a.mp4", "size": 10}, {"path": "a.mp4", "size": 20}],
            refs=_refs(),
        )
        self.assertEqual(len(rows), 1, "the same key must not be listed twice")
        self.assertTrue(rows[0]["duplicate"])

    def test_weak_matches_do_not_leak_across_roots(self):
        # A basename cited for uploads must not protect a videoFile object.
        row = _classify("a.png", live_payload_basenames=[("uploads", "a.png")])
        self.assertFalse(row["needed"])


class LegacyPurgeGuardsTests(unittest.TestCase):
    def test_the_weak_protection_default_is_on(self):
        # Defaulting off would silently make re-rooted live assets quarantinable.
        self.assertTrue(mod.WEAK_PROTECT_LIVE)

    def test_the_quarantine_prefix_keeps_objects_recoverable(self):
        # A reversible move (sau/trash/<date>/...) is required; an in-place delete
        # on a shared Drive would be irreversible.
        row = {"root": "videoFile", "key": "a.mp4"}
        source, dest = mod._destination_spec("GDrive-x", "sau/trash/2026-10-10", row)
        self.assertIn("sau/trash/2026-10-10", dest)
        self.assertIn("a.mp4", dest)
        self.assertIn("sau/videoFile", source)


def _completed(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class _FakeRclone:
    """A stateful stand-in for ``_run_rclone`` used by the restore tests.

    ``existing_size`` is what the *pre-copy* ``lsjson`` reports (``None`` = the
    legacy path is empty).  After ``copyto`` runs, ``lsjson`` reports
    ``verify_size`` instead, so the copy-then-verify-then-delete ordering can be
    asserted without touching Drive.
    """

    def __init__(
        self, *, row_bytes=100, existing_size=None, verify_size=None,
        copy_rc=0, delete_rc=0,
    ):
        self.row_bytes = row_bytes
        self.existing_size = existing_size
        self.verify_size = row_bytes if verify_size is None else verify_size
        self.copy_rc = copy_rc
        self.delete_rc = delete_rc
        self.calls: list[tuple] = []
        self.copied = False

    def __call__(self, config, *args, attempts=3):
        self.calls.append(args)
        command = args[0]
        if command == "copyto":
            self.copied = True
            if self.copy_rc:
                return _completed(rc=self.copy_rc, stderr="copy boom")
            return _completed()
        if command == "lsjson":
            size = self.verify_size if self.copied else self.existing_size
            if size is None:
                return _completed(stdout="[]")
            return _completed(stdout=json.dumps([{"Size": size}]))
        if command == "deletefile":
            if self.delete_rc:
                return _completed(rc=self.delete_rc, stderr="delete boom")
            return _completed()
        raise AssertionError(f"unexpected rclone command: {command}")

    def command_names(self):
        return [call[0] for call in self.calls]


class LegacyPurgeRestoreTests(unittest.TestCase):
    def test_restore_spec_is_the_inverse_of_the_quarantine_spec(self):
        row = {"root": "uploads", "key": "sub/dir/x.png"}
        q_source, q_dest = mod._destination_spec("R", "sau/trash/2026-10-10", row)
        r_source, r_dest = mod._restore_spec("R", "sau/trash/2026-10-10", row["root"], row["key"])
        # The quarantine destination is exactly the restore source, and vice versa.
        self.assertEqual(q_dest, r_source)
        self.assertEqual(q_source, r_dest)
        self.assertEqual(r_source, "R:sau/trash/2026-10-10/uploads/sub/dir/x.png")
        self.assertEqual(r_dest, "R:sau/uploads/sub/dir/x.png")

    def test_parse_trash_entries_keeps_known_roots_and_nested_keys(self):
        entries = [
            {"path": "videoFile/a.mp4", "size": 5},
            {"path": "uploads/b/c.png", "size": 7},
            {"path": "unknown/x", "size": 1},
            {"path": "videoFile", "size": 0},
            {"path": "", "size": 0},
        ]
        self.assertEqual(
            mod.parse_trash_entries(entries),
            [
                {"root": "videoFile", "key": "a.mp4", "bytes": 5},
                {"root": "uploads", "key": "b/c.png", "bytes": 7},
            ],
        )

    def test_dry_run_restore_plans_only(self):
        row = {"root": "videoFile", "key": "a.mp4", "bytes": 100}
        out = mod.restore_object(
            Path("/cfg"), "R", "sau/trash/2026-10-10", row, dry_run=True
        )
        self.assertEqual(out["status"], "planned")
        self.assertEqual(len(out["actions"]), 3)
        self.assertIn("copyto", out["actions"][0])
        self.assertIn("deletefile", out["actions"][2])

    def test_restore_copies_verifies_then_deletes(self):
        row = {"root": "videoFile", "key": "a.mp4", "bytes": 100}
        fake = _FakeRclone(row_bytes=100, existing_size=None)
        with mock.patch.object(mod, "_run_rclone", fake):
            out = mod.restore_object(
                Path("/cfg"), "R", "sau/trash/2026-10-10", row, dry_run=False
            )
        self.assertEqual(out["status"], "restored")
        self.assertEqual(fake.command_names(), ["lsjson", "copyto", "lsjson", "deletefile"])
        self.assertEqual(out["verified_size"], 100)

    def test_restore_never_overwrites_an_equal_size_legacy_object(self):
        row = {"root": "videoFile", "key": "a.mp4", "bytes": 100}
        fake = _FakeRclone(row_bytes=100, existing_size=100)
        with mock.patch.object(mod, "_run_rclone", fake):
            out = mod.restore_object(
                Path("/cfg"), "R", "sau/trash/2026-10-10", row, dry_run=False
            )
        self.assertEqual(out["status"], "already_present")
        self.assertEqual(fake.command_names(), ["lsjson"])

    def test_restore_refuses_to_overwrite_a_different_legacy_object(self):
        row = {"root": "videoFile", "key": "a.mp4", "bytes": 100}
        fake = _FakeRclone(row_bytes=100, existing_size=999)
        with mock.patch.object(mod, "_run_rclone", fake):
            out = mod.restore_object(
                Path("/cfg"), "R", "sau/trash/2026-10-10", row, dry_run=False
            )
        self.assertEqual(out["status"], "destination_conflict")
        self.assertEqual(out["destination_size"], 999)
        self.assertEqual(fake.command_names(), ["lsjson"])

    def test_restore_keeps_the_trash_copy_when_the_verification_fails(self):
        row = {"root": "videoFile", "key": "a.mp4", "bytes": 100}
        fake = _FakeRclone(row_bytes=100, existing_size=None, verify_size=50)
        with mock.patch.object(mod, "_run_rclone", fake):
            out = mod.restore_object(
                Path("/cfg"), "R", "sau/trash/2026-10-10", row, dry_run=False
            )
        self.assertEqual(out["status"], "verify_failed")
        self.assertEqual(fake.command_names(), ["lsjson", "copyto", "lsjson"])

    def test_restore_keeps_the_trash_copy_when_the_copy_fails(self):
        row = {"root": "videoFile", "key": "a.mp4", "bytes": 100}
        fake = _FakeRclone(row_bytes=100, existing_size=None, copy_rc=1)
        with mock.patch.object(mod, "_run_rclone", fake):
            out = mod.restore_object(
                Path("/cfg"), "R", "sau/trash/2026-10-10", row, dry_run=False
            )
        self.assertEqual(out["status"], "copy_failed")
        self.assertEqual(fake.command_names(), ["lsjson", "copyto"])

    def test_list_remote_objects_missing_tree_is_empty_only_when_allowed(self):
        fake = mock.Mock(
            return_value=_completed(rc=3, stderr="directory not found")
        )
        with mock.patch.object(mod, "_run_rclone", fake):
            self.assertEqual(
                mod.list_remote_objects(
                    Path("/cfg"), "R", "sau/trash/nope", allow_missing=True
                ),
                [],
            )
        with mock.patch.object(mod, "_run_rclone", fake):
            with self.assertRaises(RuntimeError):
                mod.list_remote_objects(Path("/cfg"), "R", "sau/trash/nope")


def _batch_list(args):
    path = args[args.index("--files-from") + 1]
    return [line.strip() for line in open(path) if line.strip()]


class _FakeBatchRclone:
    """Stand-in for ``_run_rclone`` that models copy -> lsjson -> delete."""

    def __init__(self, *, sizes, wrong=None, omit=(), copy_rc=0, delete_rc=0):
        self.sizes = sizes
        self.wrong = wrong or {}
        self.omit = set(omit)
        self.copy_rc = copy_rc
        self.delete_rc = delete_rc
        self.dest_keys: list[str] = []
        self.deleted: list[str] = []
        self.calls: list[tuple] = []

    def __call__(self, config, *args, attempts=3):
        self.calls.append(args)
        command = args[0]
        if command == "copy":
            self.dest_keys = _batch_list(args)
            if self.copy_rc:
                return _completed(rc=self.copy_rc, stderr="copy boom")
            return _completed()
        if command == "lsjson":
            entries = []
            for key in self.dest_keys:
                if key in self.omit:
                    continue
                size = self.wrong.get(key, self.sizes.get(key))
                entries.append({"Path": key, "Size": size})
            return _completed(stdout=json.dumps(entries))
        if command == "delete":
            keys = _batch_list(args)
            if self.delete_rc == 0:
                self.deleted.extend(keys)
            else:
                return _completed(rc=self.delete_rc, stderr="delete boom")
            return _completed()
        raise AssertionError(f"unexpected rclone command: {command}")

    def command_names(self):
        return [call[0] for call in self.calls]


class LegacyPurgeBatchTests(unittest.TestCase):
    def _rows(self):
        return [
            {"root": "videoFile", "key": "a.mp4", "bytes": 10},
            {"root": "videoFile", "key": "sub/b.mp4", "bytes": 20},
        ]

    def test_batch_quarantines_everything_it_verified(self):
        sizes = {"a.mp4": 10, "sub/b.mp4": 20}
        fake = _FakeBatchRclone(sizes=sizes)
        with mock.patch.object(mod, "_run_rclone", fake):
            out = mod.quarantine_batch(
                Path("/cfg"), "R", "sau/trash/2026-10-10", self._rows(), dry_run=False
            )
        self.assertEqual({r["status"] for r in out}, {"quarantined"})
        self.assertEqual(fake.deleted, ["a.mp4", "sub/b.mp4"])
        self.assertEqual(fake.command_names(), ["copy", "lsjson", "delete"])

    def test_batch_never_deletes_a_source_that_failed_verification(self):
        sizes = {"a.mp4": 10, "sub/b.mp4": 20}
        fake = _FakeBatchRclone(sizes=sizes, wrong={"sub/b.mp4": 999})
        with mock.patch.object(mod, "_run_rclone", fake):
            out = mod.quarantine_batch(
                Path("/cfg"), "R", "sau/trash/2026-10-10", self._rows(), dry_run=False
            )
        statuses = {r["key"]: r["status"] for r in out}
        self.assertEqual(statuses["a.mp4"], "quarantined")
        self.assertEqual(statuses["sub/b.mp4"], "verify_failed")
        self.assertEqual(fake.deleted, ["a.mp4"])

    def test_batch_deletes_nothing_when_the_copy_fails(self):
        sizes = {"a.mp4": 10, "sub/b.mp4": 20}
        fake = _FakeBatchRclone(sizes=sizes, copy_rc=1)
        with mock.patch.object(mod, "_run_rclone", fake):
            out = mod.quarantine_batch(
                Path("/cfg"), "R", "sau/trash/2026-10-10", self._rows(), dry_run=False
            )
        self.assertEqual({r["status"] for r in out}, {"copy_failed"})
        self.assertEqual(fake.deleted, [])
        self.assertEqual(fake.command_names(), ["copy", "lsjson"])

    def test_batch_marks_delete_failure(self):
        sizes = {"a.mp4": 10, "sub/b.mp4": 20}
        fake = _FakeBatchRclone(sizes=sizes, delete_rc=1)
        with mock.patch.object(mod, "_run_rclone", fake):
            out = mod.quarantine_batch(
                Path("/cfg"), "R", "sau/trash/2026-10-10", self._rows(), dry_run=False
            )
        self.assertEqual({r["status"] for r in out}, {"delete_failed"})
        self.assertEqual(fake.deleted, [])

    def test_batch_dry_run_makes_no_rclone_calls(self):
        with mock.patch.object(mod, "_run_rclone") as fake:
            out = mod.quarantine_batch(
                Path("/cfg"), "R", "sau/trash/2026-10-10", self._rows(), dry_run=True
            )
        self.assertEqual({r["status"] for r in out}, {"planned"})
        fake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
