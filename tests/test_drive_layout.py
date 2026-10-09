from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import db.createTable as create_table  # noqa: E402
from myUtils import drive_layout  # noqa: E402
from scripts import migrate_drive_layout as migrate  # noqa: E402


class EndpointMappingTests(unittest.TestCase):
    def test_route_endpoint_separates_published_from_new(self):
        self.assertEqual(drive_layout.route_endpoint(False, "videoFile"), "sau/inbox/videoFile")
        self.assertEqual(
            drive_layout.route_endpoint(True, "generated"), "sau/published/generated"
        )
        self.assertNotEqual(
            drive_layout.route_endpoint(False, "uploads"),
            drive_layout.route_endpoint(True, "uploads"),
        )

    def test_legacy_and_tiered_endpoints_are_distinct(self):
        self.assertEqual(drive_layout.legacy_endpoint("videoFile"), "sau/videoFile")
        self.assertEqual(
            drive_layout.tier_endpoint("inbox", "videoFile"), "sau/inbox/videoFile"
        )

    def test_split_endpoint_round_trips(self):
        self.assertEqual(drive_layout.split_endpoint("sau/videoFile"), (None, "videoFile"))
        self.assertEqual(
            drive_layout.split_endpoint("sau/inbox/generated"), ("inbox", "generated")
        )
        self.assertEqual(
            drive_layout.split_endpoint("sau/published/uploads"), ("published", "uploads")
        )

    def test_split_endpoint_rejects_unknown_shapes(self):
        self.assertEqual(drive_layout.split_endpoint("sau/archive/my-compressed"), (None, None))
        self.assertEqual(drive_layout.split_endpoint("sau/assets"), (None, None))
        self.assertEqual(drive_layout.split_endpoint("other/videoFile"), (None, None))
        self.assertEqual(drive_layout.split_endpoint(None), (None, None))

    def test_is_archive_detects_archive_tree(self):
        self.assertTrue(drive_layout.is_archive("sau/archive/my-compressed-2026-10"))
        self.assertFalse(drive_layout.is_archive("sau/published/videoFile"))

    def test_unknown_roots_and_tiers_fail_fast(self):
        with self.assertRaises(ValueError):
            drive_layout.normalise_root("assets")
        with self.assertRaises(ValueError):
            drive_layout.normalise_tier("archive")


class StorageKeyTests(unittest.TestCase):
    def test_full_remote_path_matches_restore_join(self):
        # The restore code does endpoint + "/" + key; nothing may change that.
        self.assertEqual(
            drive_layout.full_remote_path("sau/published/generated", "campaigns/x/clip.mp4"),
            "sau/published/generated/campaigns/x/clip.mp4",
        )
        self.assertEqual(
            drive_layout.remote_spec("drive", "sau/inbox/videoFile", "_batch/x.mp4"),
            "drive:sau/inbox/videoFile/_batch/x.mp4",
        )

    def test_object_move_preserves_storage_key_and_only_changes_root(self):
        source, destination = drive_layout.object_move(
            "drive", "sau/videoFile", "_batch/x.mp4", "published"
        )
        self.assertEqual(source, "drive:sau/videoFile/_batch/x.mp4")
        self.assertEqual(destination, "drive:sau/published/videoFile/_batch/x.mp4")
        # Same key on both sides: an interrupted migration cannot point a key at
        # the wrong bytes, because endpoint+key is always internally consistent.
        self.assertTrue(source.endswith("/_batch/x.mp4"))
        self.assertTrue(destination.endswith("/_batch/x.mp4"))

    def test_object_move_is_idempotent_for_already_tiered_endpoint(self):
        source, destination = drive_layout.object_move(
            "drive", "sau/inbox/generated", "campaigns/x.mp4", "published"
        )
        self.assertEqual(destination, source)

    def test_migrate_endpoint_rejects_unknown(self):
        with self.assertRaises(ValueError):
            drive_layout.migrate_endpoint("sau/assets", "published")


class MediaPathTests(unittest.TestCase):
    def test_split_media_path_normalises_every_historical_shape(self):
        cases = {
            "/home/will/social-auto-upload/videoFile/_batch/x.mp4": ("videoFile", "_batch/x.mp4"),
            "/app/uploads/foo.png": ("uploads", "foo.png"),
            "videoFile/_batch/x.mp4": ("videoFile", "_batch/x.mp4"),
            "generated/campaigns/c1/clip.mp4": ("generated", "campaigns/c1/clip.mp4"),
            "uploads/foo.png": ("uploads", "foo.png"),
            "_homealone/x.mp4": ("videoFile", "_homealone/x.mp4"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(drive_layout.split_media_path(raw), expected)

    def test_split_media_path_returns_none_for_empty(self):
        self.assertIsNone(drive_layout.split_media_path(""))


class ClassificationTests(unittest.TestCase):
    def test_only_succeeded_targets_mark_media_published(self):
        payload = (
            '{"artifacts":[{"source_file_record_id":41,'
            '"local_path":"/app/generated/campaigns/c1/clip.mp4"}]}'
        )
        ids, keys = drive_layout.published_refs_from_payloads(
            [("pending", payload), ("failed", payload), ("succeeded", payload)]
        )
        self.assertEqual(ids, {41})
        self.assertEqual(keys, {("generated", "campaigns/c1/clip.mp4")})

    def test_malformed_payload_is_ignored(self):
        ids, keys = drive_layout.published_refs_from_payloads(
            [("succeeded", "not-json"), ("succeeded", None), ("succeeded", "[]")]
        )
        self.assertEqual(ids, set())
        self.assertEqual(keys, set())

    def test_tier_for_media_prefers_published_then_defaults_inbox(self):
        self.assertEqual(
            drive_layout.tier_for_media(
                7, "videoFile/x.mp4", published_ids={7}
            ),
            "published",
        )
        self.assertEqual(
            drive_layout.tier_for_media(
                99,
                "videoFile/x.mp4",
                published_keys={("videoFile", "x.mp4")},
            ),
            "published",
        )
        self.assertEqual(
            drive_layout.tier_for_media(99, "videoFile/new.mp4"), "inbox"
        )


class MigrationPlanTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "layout.db"
        create_table.bootstrap(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO storage_backends "
                "(slug,label,provider,bucket,region,endpoint,access_key,secret_key,enabled) "
                "VALUES ('legacy','Legacy','rclone','drive','','sau/videoFile','','',1)"
            )
            self.videofile_backend = conn.execute(
                "SELECT id FROM storage_backends WHERE endpoint='sau/videoFile'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO file_records (filename,filesize,file_path,storage_key,storage_backend_id) "
                "VALUES ('pub.mp4',100,'videoFile/pub.mp4','pub.mp4',?)",
                (self.videofile_backend,),
            )
            conn.execute(
                "INSERT INTO file_records (filename,filesize,file_path,storage_key,storage_backend_id) "
                "VALUES ('new.mp4',50,'videoFile/new.mp4','new.mp4',?)",
                (self.videofile_backend,),
            )
            pub_id = conn.execute(
                "SELECT id FROM file_records WHERE file_path='videoFile/pub.mp4'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO publish_jobs (idempotency_key,platform,status,payload_json) VALUES (?,?,?,?)",
                ("test-job-1", "tiktok", "succeeded", '{"artifacts":[{"source_file_record_id":%d}]}' % pub_id),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO publish_job_targets (job_id,account_ref,file_ref,status) VALUES (?,?,?,?)",
                (job_id, "acct", "campaign_post:1", "succeeded"),
            )

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_plan_classifies_by_publish_status_and_keeps_key(self):
        conn = sqlite3.connect(self.db_path)
        try:
            plan = migrate.build_plan(
                conn,
                {"videoFile": {"pub.mp4": 100, "new.mp4": 50}},
                "drive",
            )
        finally:
            conn.close()
        by_path = {m["file_path"]: m for m in plan["moves"]}
        self.assertEqual(by_path["videoFile/pub.mp4"]["tier"], "published")
        self.assertEqual(by_path["videoFile/new.mp4"]["tier"], "inbox")
        self.assertEqual(by_path["videoFile/pub.mp4"]["storage_key"], "pub.mp4")
        self.assertEqual(
            by_path["videoFile/pub.mp4"]["destination"],
            "drive:sau/published/videoFile/pub.mp4",
        )
        self.assertEqual(plan["summary"]["unresolved"], 0)

    def test_build_plan_flags_size_mismatch_as_unresolved(self):
        conn = sqlite3.connect(self.db_path)
        try:
            plan = migrate.build_plan(
                conn, {"videoFile": {"pub.mp4": 999, "new.mp4": 50}}, "drive"
            )
        finally:
            conn.close()
        reasons = {u.get("reason") for u in plan["unresolved"]}
        self.assertIn("recorded size does not match remote object", reasons)
        # The mismatch is never silently promoted.
        self.assertTrue(all(m["file_path"] != "videoFile/pub.mp4" for m in plan["moves"]))

    def test_build_plan_flags_missing_remote_object(self):
        conn = sqlite3.connect(self.db_path)
        try:
            plan = migrate.build_plan(conn, {"videoFile": {"new.mp4": 50}}, "drive")
        finally:
            conn.close()
        reasons = {u.get("reason") for u in plan["unresolved"]}
        self.assertIn("no such object under legacy endpoint", reasons)

    def test_apply_creates_tiered_backends_and_repoints_records(self):
        conn = sqlite3.connect(self.db_path)
        try:
            plan = migrate.build_plan(
                conn,
                {"videoFile": {"pub.mp4": 100, "new.mp4": 50}},
                "drive",
            )
            applied = migrate.apply_plan(
                conn,
                plan,
                remote="drive",
                config=self.root / "rclone.conf",
                dry_run_io=True,
            )
            self.assertEqual(len(applied), 2)
            rows = dict(
                conn.execute(
                    "SELECT file_path, storage_backend_id FROM file_records"
                ).fetchall()
            )
            pub_backend = conn.execute(
                "SELECT id FROM storage_backends WHERE endpoint='sau/published/videoFile'"
            ).fetchone()[0]
            inbox_backend = conn.execute(
                "SELECT id FROM storage_backends WHERE endpoint='sau/inbox/videoFile'"
            ).fetchone()[0]
            self.assertEqual(rows["videoFile/pub.mp4"], pub_backend)
            self.assertEqual(rows["videoFile/new.mp4"], inbox_backend)
            # storage_key is preserved so endpoint+key still names the object.
            keys = dict(
                conn.execute("SELECT file_path, storage_key FROM file_records").fetchall()
            )
            self.assertEqual(keys["videoFile/pub.mp4"], "pub.mp4")
        finally:
            conn.close()

    def test_ensure_tiered_backends_is_idempotent(self):
        conn = sqlite3.connect(self.db_path)
        try:
            first = migrate.ensure_tiered_backends(conn, "drive")
            second = migrate.ensure_tiered_backends(conn, "drive")
            self.assertEqual(first, second)
            count = conn.execute(
                "SELECT COUNT(*) FROM storage_backends WHERE endpoint LIKE 'sau/inbox/%' "
                "OR endpoint LIKE 'sau/published/%'"
            ).fetchone()[0]
            self.assertEqual(count, len(drive_layout.TIERS) * len(drive_layout.MEDIA_ROOTS))
        finally:
            conn.close()

    def test_canonical_key_handles_uploads_prefix_inconsistency(self):
        self.assertEqual(
            migrate._canonical_key("uploads", "uploads/foo.png", {"uploads/foo.png"}),
            "uploads/foo.png",
        )
        self.assertEqual(
            migrate._canonical_key("uploads", "uploads/foo.png", {"foo.png"}),
            "foo.png",
        )
        self.assertIsNone(migrate._canonical_key("uploads", "gone.png", {"other.png"}))


if __name__ == "__main__":
    unittest.main()
