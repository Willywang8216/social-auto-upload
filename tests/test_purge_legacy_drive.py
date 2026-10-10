"""Safety tests for the legacy Drive purge classifier.

classify() decides whether a Drive object may be quarantined. Getting it wrong in
the permissive direction destroys the source media of a scheduled post, so the
tests below pin the protective behaviour, not just the happy path.

Context: of 453 `sau/videoFile` objects with no file_records mapping, 310 are
cited elsewhere in the database. A classifier that looked only at the mapping
would have quarantined them.
"""

import importlib.util
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
