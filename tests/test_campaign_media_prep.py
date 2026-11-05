"""Campaign preparation is Flask-free, immutable and uses exact serving paths."""
from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from flask import has_request_context
from PIL import Image

from myUtils import campaign_media_prep as prep, image_compliance, media_compliance, video_artifacts
from myUtils import profiles


def _profile():
    return profiles.Profile(id=1, name="Test", slug="test", settings={})


class CampaignMediaPrepNoFlaskContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "videoFile").mkdir()
        self.image = self.base / "videoFile" / "pic.jpg"
        Image.new("RGB", (64, 64), "red").save(self.image)
        self.db_path = self.base / "unused.db"
        for module in (prep, video_artifacts):
            patcher = mock.patch.object(module, "BASE_DIR", self.base)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(prep.media_pipeline, "GENERATED_MEDIA_ROOT", self.base / "generated" / "campaigns")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(prep.campaign_store, "get_campaign", return_value=SimpleNamespace(selected_account_ids=[]))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(prep.profile_registry, "list_accounts", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(prep.campaign_store, "add_campaign_artifact_set", return_value=[])
        self.register = patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, *, public_base_url, extra=None, files=None):
        return prep.prepare_campaign_media_artifacts(1, _profile(), files or [
            {"file_record_id": 7, "role": "image", "file_path": str(self.image)}],
            {"title": "hello", "uploadToRemote": False, **(extra or {})},
            selected_platforms={"bluesky"}, db_path=self.db_path, public_base_url=public_base_url)

    def _expected_url(self, context, origin):
        path = Path(context["imageLocalPaths"][0])
        relative = path.relative_to(self.base / "generated").as_posix()
        return f"{origin}/getFile?filename={quote(relative, safe='/')}"

    def test_module_is_flask_free(self):
        source = inspect.getsource(prep)
        self.assertNotIn("import flask", source)
        self.assertNotIn("from flask", source)
        self.assertFalse(hasattr(prep, "flask"))

    def test_runs_with_no_request_context_and_explicit_base_url(self):
        self.assertFalse(has_request_context())
        with mock.patch.object(prep.ops_alerts, "public_app_origin", return_value=""):
            context = self._call(public_base_url="https://cdn.example.com")
        self.assertNotEqual(context["imageLocalPaths"], [str(self.image)])
        url = self._expected_url(context, "https://cdn.example.com")
        self.assertEqual(context["imageUrls"], [url])
        self.register.assert_called_once()
        row = self.register.call_args.args[1][0]
        self.assertEqual(row["public_url"], url)
        self.assertEqual(row["metadata"]["origin_source_path"], str(self.image))
        image_compliance.validate_image(row["local_path"], platform="bluesky", expected_sha256=row["metadata"]["sha256"])

    def test_no_origin_leaves_the_url_unset(self):
        with mock.patch.object(prep.ops_alerts, "public_app_origin", return_value=""):
            context = self._call(public_base_url=None)
        self.assertEqual(context["imageUrls"], [])
        self.assertTrue(Path(context["imageLocalPaths"][0]).is_file())
        self.assertIsNone(self.register.call_args.args[1][0]["public_url"])

    def test_unreachable_origin_is_suppressed_for_any_platform(self):
        for origin in ("http://localhost:5409", "https://localhost:5409", "http://127.0.0.1:5409"):
            with self.subTest(base=origin), mock.patch.object(prep.ops_alerts, "public_app_origin", return_value=""):
                self.register.reset_mock()
                context = self._call(public_base_url=origin)
                self.assertEqual(context["imageUrls"], [])
                self.assertIsNone(self.register.call_args.args[1][0]["public_url"])

    def test_public_origin_helper_rejects_loopback_and_http(self):
        for origin in ("http://cdn.example.com", "https://localhost", "https://127.0.0.1:5409", ""):
            self.assertFalse(prep._is_public_base_url(origin))
        self.assertTrue(prep._is_public_base_url("https://cdn.example.com"))
        self.assertTrue(prep._is_public_base_url("https://cdn.example.com/localhost"))

    def test_configured_public_origin_wins_over_passed_host(self):
        with mock.patch.object(prep.ops_alerts, "public_app_origin", return_value="https://public.example.com"):
            context = self._call(public_base_url="http://127.0.0.1:5409")
        self.assertEqual(context["imageUrls"], [self._expected_url(context, "https://public.example.com")])

    def test_invalid_image_prevents_any_set_registration(self):
        invalid = self.base / "videoFile" / "bad.jpg"
        invalid.write_bytes(b"not a jpeg")
        with self.assertRaises(media_compliance.MediaPreparationError):
            self._call(public_base_url=None, files=[
                {"file_record_id": 7, "role": "image", "file_path": str(self.image)},
                {"file_record_id": 8, "role": "image", "file_path": str(invalid)}])
        self.register.assert_not_called()

    def test_all_images_registered_only_after_full_generation_passes(self):
        second = self.base / "videoFile" / "other.jpg"
        Image.new("RGB", (64, 64), "blue").save(second)
        context = self._call(public_base_url=None, files=[
            {"file_record_id": 7, "role": "image", "file_path": str(self.image)},
            {"file_record_id": 8, "role": "image", "file_path": str(second)}])
        rows = self.register.call_args.args[1]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows, context["preparedArtifacts"])
        self.assertEqual([row["metadata"]["image_index"] for row in rows], [1, 2])
        self.assertEqual(rows[0]["metadata"]["preparation_group"], rows[1]["metadata"]["preparation_group"])


if __name__ == "__main__":
    unittest.main()
