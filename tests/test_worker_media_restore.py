from __future__ import annotations

import os
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

        def write_source(_backend, _key, _temporary_path):
            raise OSError("backend object unavailable; use safe public URL fallback")

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

    def test_generated_artifact_never_falls_back_to_source_record(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO storage_backends (slug,label,provider,bucket,region,endpoint,access_key,secret_key) VALUES (?,?,?,?,?,?,?,?)",
                ("source-drive", "Source Drive", "rclone", "drive", "", "sau/videoFile", "", ""),
            )
            backend_id = conn.execute("SELECT id FROM storage_backends").fetchone()[0]
            conn.execute(
                "UPDATE file_records SET storage_backend_id=? WHERE id=1",
                (backend_id,),
            )
            conn.execute(
                "INSERT INTO file_records (filename,filesize,file_path) VALUES (?,?,?)",
                ("clip_pub.mp4", 14, "generated/campaigns/campaign-4/clip_pub.mp4"),
            )
        generated_root = self.root / "generated" / "campaigns"
        payload = {"artifacts": [{
            "local_path": str(generated_root / "campaign-4" / "clip_pub.mp4"),
            "source_file_record_id": 1,
        }]}
        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker.media_pipeline, "GENERATED_MEDIA_ROOT", generated_root
        ), patch.object(
            worker.media_remote_storage, "download_from_backend",
            side_effect=AssertionError("unregistered generated record must not use source bytes"),
        ):
            with self.assertRaisesRegex(worker.MediaRestoreError, "safe public HTTPS"):
                worker._ensure_artifact_paths_local(payload, db_path=self.db_path)

    def test_legacy_generated_artifact_restores_only_from_unique_registered_mapping(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO storage_backends (slug,label,provider,bucket,region,endpoint,access_key,secret_key) VALUES (?,?,?,?,?,?,?,?)",
                ("gdrive-generated", "Generated Drive", "rclone", "drive", "", "sau/generated", "", ""),
            )
            backend_id = conn.execute("SELECT id FROM storage_backends").fetchone()[0]
            conn.execute(
                "INSERT INTO file_records (filename,filesize,file_path,storage_key,storage_backend_id) VALUES (?,?,?,?,?)",
                ("clip_pub.mp4", 14, "legacy-generated/clip_pub.mp4", "campaigns/campaign-2236/clip_pub.mp4", backend_id),
            )
        generated_root = self.root / "generated" / "campaigns"
        generated_path = generated_root / "campaign-2236" / "clip_pub.mp4"
        payload = {"artifacts": [{"local_path": str(generated_path), "source_file_record_id": 1}]}
        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker.media_pipeline, "GENERATED_MEDIA_ROOT", generated_root
        ), patch.object(
            worker.media_remote_storage, "download_from_backend",
            side_effect=lambda _backend, _key, destination: Path(destination).write_bytes(b"restored bytes"),
        ) as download:
            worker._ensure_artifact_paths_local(payload, db_path=self.db_path)
        self.assertEqual(generated_path.read_bytes(), b"restored bytes")
        self.assertEqual(download.call_args.args[1], "campaigns/campaign-2236/clip_pub.mp4")

    def test_generated_artifact_prefers_registered_remote_backend(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO storage_backends (slug,label,provider,bucket,region,endpoint,access_key,secret_key) VALUES (?,?,?,?,?,?,?,?)",
                ("gdrive-generated", "Generated Drive", "rclone", "drive", "", "sau/generated", "", ""),
            )
            backend_id = conn.execute("SELECT id FROM storage_backends").fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO file_records (filename,filesize,file_path,storage_key,storage_backend_id) VALUES (?,?,?,?,?)",
                ("clip_pub.mp4", 14, "generated/campaigns/campaign-4/clip_pub.mp4", "campaigns/campaign-4/clip_pub.mp4", backend_id),
            )
            source_id = cursor.lastrowid
        generated_root = self.root / "generated" / "campaigns"
        generated_path = generated_root / "campaign-4" / "clip_pub.mp4"
        payload = {"artifacts": [{
            "local_path": str(generated_path),
            "source_file_record_id": source_id,
        }]}
        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker.media_pipeline, "GENERATED_MEDIA_ROOT", generated_root
        ), patch.object(
            worker.media_remote_storage, "download_from_backend",
            side_effect=lambda _backend, _key, destination: Path(destination).write_bytes(b"restored bytes"),
        ) as download, patch(
            "requests.get", side_effect=AssertionError("Drive mapping should be preferred")
        ):
            worker._ensure_artifact_paths_local(payload, db_path=self.db_path)
        self.assertEqual(generated_path.read_bytes(), b"restored bytes")
        self.assertEqual(download.call_args.args[1], "campaigns/campaign-4/clip_pub.mp4")

    def test_resolve_local_upload_reference_before_any_remote_download(self) -> None:
        uploads = self.root / "uploads" / "local.mp4"
        uploads.parent.mkdir(parents=True)
        uploads.write_bytes(b"local media")
        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker, "_try_download_from_storage", side_effect=AssertionError("must not download")
        ):
            resolved = worker._resolve_file_path("uploads/local.mp4", db_path=self.db_path)
        self.assertEqual(resolved, uploads)

    # --- canonical stored-path normaliser ---------------------------------

    def test_resolve_media_path_rewrites_host_prefix_to_base_dir(self) -> None:
        """A host absolute prefix maps onto this process's BASE_DIR.

        This is the live bug: the artifact row stored
        ``/home/will/social-auto-upload/videoFile/...`` while the worker ran in
        a container where the repo is mounted at ``/app``. Writing the raw host
        path surfaced as ``Permission denied: '/home/will'``.
        """
        container_root = self.root / "app"
        with patch.object(worker, "BASE_DIR", container_root):
            resolved = worker._resolve_media_path(
                "/home/will/social-auto-upload/videoFile/_inbox_cache/demo.mp4"
            )
        self.assertEqual(
            resolved,
            container_root / "videoFile" / "_inbox_cache" / "demo.mp4",
        )

    def test_resolve_media_path_rewrites_container_prefix_to_base_dir(self) -> None:
        """A host-run worker maps the container mount onto BASE_DIR.

        The container runs the worker with ``BASE_DIR=/app`` and host-run
        workers (manual drains, scripts, this suite) with the host checkout.
        A stored ``/app/...`` path left untouched on the host pointed at the
        root-owned ``/app`` directory, so media restore died with EACCES while
        creating ``/app/videoFile`` even though the record and the Drive
        object existed.
        """
        host_root = self.root / "social-auto-upload"
        with patch.object(worker, "BASE_DIR", host_root):
            resolved = worker._resolve_media_path("/app/videoFile/x.mp4")
        self.assertEqual(resolved, host_root / "videoFile" / "x.mp4")

    def test_resolve_media_path_keeps_container_prefix_identity_in_container(self) -> None:
        """In the container ``/app`` is BASE_DIR, so the mapping is an identity."""
        with patch.object(worker, "BASE_DIR", Path("/app")):
            self.assertEqual(
                worker._resolve_media_path("/app/videoFile/x.mp4"),
                Path("/app/videoFile/x.mp4"),
            )

    def test_base_dir_alias_env_override(self) -> None:
        with patch.dict(os.environ, {"SAU_BASE_DIR_ALIASES": "/mnt/repo"}, clear=False), patch.object(
            worker, "BASE_DIR", self.root
        ):
            resolved = worker._resolve_media_path("/mnt/repo/videoFile/x.mp4")
        self.assertEqual(resolved, self.root / "videoFile" / "x.mp4")

    def test_resolve_media_path_leaves_relative_path_untouched(self) -> None:
        with patch.object(worker, "BASE_DIR", self.root):
            self.assertEqual(
                worker._resolve_media_path("videoFile/_batch1/x.mp4"),
                Path("videoFile/_batch1/x.mp4"),
            )

    def test_resolve_media_path_leaves_unrelated_absolute_path_untouched(self) -> None:
        with patch.object(worker, "BASE_DIR", self.root):
            self.assertEqual(
                worker._resolve_media_path("/tmp/x.mp4"), Path("/tmp/x.mp4")
            )

    def test_host_prefixed_artifact_restores_under_base_dir(self) -> None:
        """End-to-end: a host-prefixed local_path is written under BASE_DIR.

        The stored row names the host path, but the restore must land on the
        container-equivalent path this process can actually use.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path, storage_key) VALUES (?, ?, ?, ?)",
                ("demo.mp4", 1.0, "videoFile/_inbox_cache/demo.mp4", "_inbox_cache/demo.mp4"),
            )
            conn.execute(
                "INSERT INTO storage_backends (slug, label, provider, bucket, region, endpoint, access_key, secret_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test-drive", "Test Drive", "rclone", "drive", "auto", "sau/videoFile", "", ""),
            )
            backend_id = conn.execute("SELECT id FROM storage_backends").fetchone()[0]
            conn.execute(
                "UPDATE file_records SET storage_backend_id=? WHERE file_path='videoFile/_inbox_cache/demo.mp4'",
                (backend_id,),
            )

        container_root = self.root / "app"
        payload = {"artifacts": [{
            "local_path": "/home/will/social-auto-upload/videoFile/_inbox_cache/demo.mp4",
        }]}

        def write_media(_backend, _key, temporary_path):
            Path(temporary_path).write_bytes(b"restored via host map")

        with patch.object(worker, "BASE_DIR", container_root), patch.object(
            worker.media_remote_storage, "download_from_backend", side_effect=write_media
        ):
            worker._ensure_artifact_paths_local(payload, db_path=self.db_path)

        self.assertEqual(
            (container_root / "videoFile" / "_inbox_cache" / "demo.mp4").read_bytes(),
            b"restored via host map",
        )

    def test_container_prefixed_video_artifact_restores_under_base_dir(self) -> None:
        """End-to-end: ``/app/videoFile/...`` lands under this process's BASE_DIR.

        This is the path shape behind the ``Permission denied: '/app/videoFile'``
        failures: the record and the bytes were fine, the destination could not
        be created because the container prefix was never mapped.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path, storage_key) VALUES (?, ?, ?, ?)",
                ("demo2.mp4", 1.0, "videoFile/_inbox_cache/demo2.mp4", "_inbox_cache/demo2.mp4"),
            )
            conn.execute(
                "INSERT INTO storage_backends (slug, label, provider, bucket, region, endpoint, access_key, secret_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test-drive", "Test Drive", "rclone", "drive", "auto", "sau/videoFile", "", ""),
            )
            backend_id = conn.execute("SELECT id FROM storage_backends").fetchone()[0]
            conn.execute(
                "UPDATE file_records SET storage_backend_id=? WHERE file_path='videoFile/_inbox_cache/demo2.mp4'",
                (backend_id,),
            )

        payload = {"artifacts": [{"local_path": "/app/videoFile/_inbox_cache/demo2.mp4"}]}
        destination = self.root / "videoFile" / "_inbox_cache" / "demo2.mp4"

        def write_media(_backend, _key, temporary_path):
            Path(temporary_path).write_bytes(b"restored via container map")

        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker.media_remote_storage, "download_from_backend", side_effect=write_media
        ):
            worker._ensure_artifact_paths_local(payload, db_path=self.db_path)

        self.assertEqual(destination.read_bytes(), b"restored via container map")

    def test_container_prefixed_generated_artifact_restores_from_registered_drive(self) -> None:
        """A ``/app/generated`` artifact resolves its own generated record.

        Before the container prefix was mapped, ``is_generated_artifact`` was
        false and the by-name generated fallback was skipped, so the artifact
        was declared to have no file record even though a Drive mapping existed.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO storage_backends (slug, label, provider, bucket, region, endpoint, access_key, secret_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("gdrive-generated", "Generated Drive", "rclone", "drive", "auto", "sau/generated", "", ""),
            )
            backend_id = conn.execute("SELECT id FROM storage_backends").fetchone()[0]
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path, storage_key, storage_backend_id)"
                " VALUES (?, ?, ?, ?, ?)",
                ("clip_pub.mp4", 14, "generated/campaigns/campaign-9/clip_pub.mp4", "campaigns/campaign-9/clip_pub.mp4", backend_id),
            )

        generated_root = self.root / "generated" / "campaigns"
        payload = {
            "artifacts": [
                {
                    "local_path": "/app/generated/campaigns/campaign-9/clip_pub.mp4",
                    "source_file_record_id": 1,
                }
            ]
        }
        destination = generated_root / "campaign-9" / "clip_pub.mp4"

        with patch.object(worker, "BASE_DIR", self.root), patch.object(
            worker.media_pipeline, "GENERATED_MEDIA_ROOT", generated_root
        ), patch.object(
            worker.media_remote_storage,
            "download_from_backend",
            side_effect=lambda _backend, _key, dest: Path(dest).write_bytes(b"restored generated"),
        ) as download:
            worker._ensure_artifact_paths_local(payload, db_path=self.db_path)

        self.assertEqual(destination.read_bytes(), b"restored generated")
        self.assertEqual(download.call_args.args[1], "campaigns/campaign-9/clip_pub.mp4")

    def test_missing_artifact_after_normalisation_raises_clear_message(self) -> None:
        payload = {"artifacts": [{
            "local_path": "/home/will/social-auto-upload/videoFile/_inbox_cache/gone.mp4",
        }]}
        with patch.object(worker, "BASE_DIR", self.root / "app"):
            with self.assertRaisesRegex(worker.MediaRestoreError, "no file record"):
                worker._ensure_artifact_paths_local(payload, db_path=self.db_path)

    def test_permission_error_on_missing_path_is_reported_as_path_problem(self) -> None:
        """EACCES while preparing a missing path is a path-resolution failure.

        Before normalisation this leaked as a bare ``Permission denied`` on an
        ancestor (``/home/will``), which reads like a permissions bug. With a
        record present the restore proceeds to create the destination parent;
        an unmappable destination must fail with the resolved-path message.
        """
        payload = {"artifacts": [{
            "local_path": "/home/will/social-auto-upload/videoFile/other/x.mp4",
            "source_file_record_id": 1,
        }]}
        real_mkdir = Path.mkdir

        def deny_mkdir(self_path, *args, **kwargs):
            if self_path.name == "other":
                raise PermissionError(13, "Permission denied", str(self_path))
            return real_mkdir(self_path, *args, **kwargs)

        with patch.object(worker, "BASE_DIR", self.root / "app"), patch(
            "pathlib.Path.mkdir", deny_mkdir
        ):
            with self.assertRaisesRegex(worker.MediaRestoreError, "after normalisation"):
                worker._ensure_artifact_paths_local(payload, db_path=self.db_path)

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
