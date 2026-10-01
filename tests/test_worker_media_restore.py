from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db.createTable as create_table
from myUtils import worker


class WorkerMediaRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "restore.db"
        create_table.bootstrap(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path, storage_key) VALUES (?, ?, ?, ?)",
                ("clip.mp4", 1.0, "clip.mp4", "videoFile/clip.mp4"),
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolve_file_path_raises_clear_error_when_remote_row_missing(self) -> None:
        with patch.object(worker, "BASE_DIR", self.root):
            with self.assertRaisesRegex(worker.MediaRestoreError, "unavailable"):
                worker._resolve_file_path("missing.mp4", db_path=self.db_path)

    def test_backend_restore_failure_is_explicit_and_does_not_leave_partial_file(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO storage_backends (slug, label, provider, bucket, region, endpoint, access_key, secret_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         ("test-drive", "Test Drive", "rclone", "drive", "auto", "sau/videoFile", "", ""))
            backend_id = conn.execute("SELECT id FROM storage_backends").fetchone()[0]
            conn.execute("UPDATE file_records SET storage_backend_id=?", (backend_id,))
        destination = self.root / "videoFile" / "clip.mp4"

        def partial_then_fail(_backend, _key, temporary_path):
            Path(temporary_path).write_bytes(b"partial")
            raise OSError("connection dropped")

        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker.media_remote_storage, "download_from_backend", side_effect=partial_then_fail
        ):
            with self.assertRaisesRegex(worker.MediaRestoreError, "connection dropped"):
                worker._resolve_file_path("clip.mp4", db_path=self.db_path)

        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob("*.part")), [])

    def test_missing_optional_thumbnail_does_not_raise(self) -> None:
        payload = {"thumbnail": "videoFile/missing-thumbnail.jpg"}
        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker, "_try_download_from_storage",
            side_effect=worker.MediaRestoreError("storage unavailable"),
        ):
            worker._restore_optional_thumbnail(payload, db_path=self.db_path)
        self.assertEqual(payload["thumbnail"], "videoFile/missing-thumbnail.jpg")

    def test_explicit_video_file_prefix_restores_to_media_root(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path, storage_key) VALUES (?, ?, ?, ?)",
                ("prefixed.mp4", 1.0, "videoFile/prefixed.mp4", "prefixed.mp4"),
            )
            conn.execute("INSERT INTO storage_backends (slug, label, provider, bucket, region, endpoint, access_key, secret_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         ("test-drive", "Test Drive", "rclone", "drive", "auto", "sau/videoFile", "", ""))
            backend_id = conn.execute("SELECT id FROM storage_backends").fetchone()[0]
            conn.execute("UPDATE file_records SET storage_backend_id=? WHERE file_path='videoFile/prefixed.mp4'", (backend_id,))
        destination = self.root / "videoFile" / "prefixed.mp4"

        def write_media(_backend, _key, temporary_path):
            Path(temporary_path).write_bytes(b"restored media")

        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker.media_remote_storage, "download_from_backend", side_effect=write_media
        ):
            resolved = worker._resolve_file_path("videoFile/prefixed.mp4", db_path=self.db_path)

        self.assertEqual(resolved, destination)
        self.assertEqual(destination.read_bytes(), b"restored media")

    def test_artifact_recorded_with_the_video_file_prefix_is_found(self) -> None:
        """file_records carries two path conventions for the same media.

        A record stored as ``videoFile/_batch1/x.mp4`` was invisible to a lookup
        that only tried the bare ``_batch1/x.mp4`` form, so those runs failed
        permanently with MediaRestoreError even though the bytes were registered
        on a backend the whole time.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path, storage_key) VALUES (?, ?, ?, ?)",
                ("x.mp4", 1.0, "videoFile/_batch1/x.mp4", "_batch1/x.mp4"),
            )
            conn.execute(
                "INSERT INTO storage_backends (slug, label, provider, bucket, region, endpoint, access_key, secret_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test-drive", "Test Drive", "rclone", "drive", "auto", "sau/videoFile", "", ""),
            )
            backend_id = conn.execute("SELECT id FROM storage_backends").fetchone()[0]
            conn.execute(
                "UPDATE file_records SET storage_backend_id=? WHERE file_path='videoFile/_batch1/x.mp4'",
                (backend_id,),
            )

        payload = {"artifacts": [{"local_path": str(self.root / "videoFile" / "_batch1" / "x.mp4")}]}

        def write_media(_backend, _key, temporary_path):
            Path(temporary_path).write_bytes(b"restored via prefix")

        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker.media_remote_storage, "download_from_backend", side_effect=write_media
        ):
            worker._ensure_artifact_paths_local(payload, db_path=self.db_path)

        self.assertEqual(
            (self.root / "videoFile" / "_batch1" / "x.mp4").read_bytes(),
            b"restored via prefix",
        )


    def test_generated_campaign_artifact_restores_from_public_url(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO storage_backends (slug, label, provider, bucket, region, endpoint, access_key, secret_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         ("test-drive", "Test Drive", "rclone", "drive", "auto", "sau/videoFile", "", ""))
            backend_id = conn.execute("SELECT id FROM storage_backends").fetchone()[0]
            conn.execute("UPDATE file_records SET storage_backend_id=?", (backend_id,))
        generated_root = self.root / "generated" / "campaigns"
        generated_path = generated_root / "campaign-4" / "clip_pub.mp4"
        payload = {"artifacts": [{
            "local_path": str(generated_path),
            "public_url": "https://cdn.example/clip_pub.mp4",
            "source_file_record_id": 1,
        }]}

        def write_source(_backend, _key, temporary_path):
            Path(temporary_path).write_bytes(b"source media")

        class Response:
            headers = {}
            is_redirect = False
            is_permanent_redirect = False
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def raise_for_status(self): return None
            def iter_content(self, chunk_size):
                assert chunk_size == 1024 * 1024
                yield b"prepared media"

        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker.media_pipeline, "GENERATED_MEDIA_ROOT", generated_root
        ), patch.object(worker.media_remote_storage, "download_from_backend", side_effect=write_source), patch(
            "requests.get", return_value=Response()
        ) as get:
            worker._ensure_artifact_paths_local(payload, db_path=self.db_path)

        self.assertEqual(generated_path.read_bytes(), b"prepared media")
        get.assert_called_once_with(
            "https://cdn.example/clip_pub.mp4", timeout=(10, 120),
            stream=True, allow_redirects=False,
        )

    def test_resolve_local_upload_reference_before_any_remote_download(self) -> None:
        uploads = self.root / "uploads" / "local.mp4"
        uploads.parent.mkdir(parents=True)
        uploads.write_bytes(b"local media")
        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker, "_try_download_from_storage", side_effect=AssertionError("must not download")
        ):
            resolved = worker._resolve_file_path("uploads/local.mp4", db_path=self.db_path)
        self.assertEqual(resolved, uploads)

    def test_generated_artifact_rejects_private_redirect_target(self) -> None:
        destination = self.root / "generated" / "campaigns" / "campaign-5" / "clip.mp4"

        class Response:
            headers = {"Location": "http://127.0.0.1/private"}
            is_redirect = True
            is_permanent_redirect = False
            def __enter__(self): return self
            def __exit__(self, *_args): return None

        with patch("requests.get", return_value=Response()) as get:
            with self.assertRaisesRegex(worker.MediaRestoreError, "public HTTPS"):
                worker._download_public_artifact("https://cdn.example/clip.mp4", destination)

        self.assertEqual(get.call_count, 1)
        self.assertFalse(destination.exists())

    def test_missing_generated_artifact_rejects_localhost_url_without_network(self) -> None:
        generated_root = self.root / "generated" / "campaigns"
        payload = {"artifacts": [{
            "local_path": str(generated_root / "campaign-1" / "clip.mp4"),
            "public_url": "http://localhost:5409/getFile?filename=clip.mp4",
        }]}
        with patch.object(worker.media_pipeline, "GENERATED_MEDIA_ROOT", generated_root), patch(
            "requests.get", side_effect=AssertionError("unsafe localhost URL must not be fetched")
        ):
            with self.assertRaisesRegex(worker.MediaRestoreError, "safe public HTTPS"):
                worker._ensure_artifact_paths_local(payload, db_path=self.db_path)

    def test_cdn_empty_response_is_rejected_and_tempfile_removed(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE file_records SET storage_cdn_url=?", ("https://cdn.example/clip.mp4",))
        destination = self.root / "artifacts" / "clip.mp4"

        class Response:
            content = b""
            headers = {}
            is_redirect = False
            is_permanent_redirect = False
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def raise_for_status(self):
                return None
            def iter_content(self, chunk_size):
                yield b""

        payload = {"artifacts": [{"local_path": str(destination), "source_file_record_id": 1}]}
        with patch("requests.get", return_value=Response()):
            with self.assertRaisesRegex(worker.MediaRestoreError, "empty"):
                worker._ensure_artifact_paths_local(payload, db_path=self.db_path)

        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
