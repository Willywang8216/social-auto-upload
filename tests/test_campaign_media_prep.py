"""The extracted campaign media prep must run with no Flask request context.

``sau_backend._prepare_campaign_media_artifacts`` used to reach for
``flask.request.host_url`` to mint a local ``/getFile`` URL. That made the
~900 s transcode/watermark/split/upload work impossible to run from a worker
process. The logic now lives in :mod:`myUtils.campaign_media_prep`, which is
Flask-free and takes the origin explicitly as ``public_base_url``.

These tests pin:
  * the module imports and runs without Flask (no request context),
  * an explicit ``public_base_url`` is honoured,
  * an unset origin leaves the URL ``None`` instead of inventing a hostname,
  * the configured public origin still wins over the passed host (the ordering
    the request path relied on).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if "conf" not in sys.modules:
    import types as _types

    conf_module = _types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module

from flask import has_request_context

from myUtils import campaign_media_prep
from myUtils import profiles as profile_registry


def _profile() -> profile_registry.Profile:
    return profile_registry.Profile(id=1, name="Test", slug="test", settings={})


class CampaignMediaPrepNoFlaskContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = Path(self.tmp.name) / "pic.jpg"
        self.image.write_bytes(b"not really a jpeg")
        self.db_path = Path(self.tmp.name) / "db.db"
        self.monkey = mock.patch.object(
            campaign_media_prep.campaign_store,
            "add_campaign_artifact",
            return_value=None,
        )
        self.add_campaign_artifact = self.monkey.start()
        self.addCleanup(self.monkey.stop)

    def _call(self, *, public_base_url):
        media_files = [
            {"file_record_id": 7, "role": "image", "file_path": str(self.image)}
        ]
        return campaign_media_prep.prepare_campaign_media_artifacts(
            1,
            _profile(),
            media_files,
            {"title": "hello"},
            selected_platforms=set(),
            db_path=self.db_path,
            public_base_url=public_base_url,
        )

    def test_module_is_flask_free(self) -> None:
        # No Flask import in the extracted module's source, and no bound name.
        import inspect

        source = inspect.getsource(campaign_media_prep)
        self.assertNotIn("import flask", source)
        self.assertNotIn("from flask", source)
        self.assertFalse(hasattr(campaign_media_prep, "flask"))

    def test_runs_with_no_request_context_and_explicit_base_url(self) -> None:
        self.assertFalse(has_request_context())

        with mock.patch.object(
            campaign_media_prep.ops_alerts, "public_app_origin", return_value=""
        ):
            context = self._call(public_base_url="https://cdn.example.com")

        self.assertEqual(context["imageLocalPaths"], [str(self.image)])
        self.assertEqual(
            context["imageUrls"],
            ["https://cdn.example.com/getFile?filename=pic.jpg"],
        )
        self.add_campaign_artifact.assert_called()
        self.assertEqual(
            self.add_campaign_artifact.call_args.kwargs["public_url"],
            "https://cdn.example.com/getFile?filename=pic.jpg",
        )

    def test_no_origin_leaves_the_url_unset(self) -> None:
        self.assertFalse(has_request_context())

        with mock.patch.object(
            campaign_media_prep.ops_alerts, "public_app_origin", return_value=""
        ):
            context = self._call(public_base_url=None)

        self.assertEqual(context["imageUrls"], [])
        self.assertEqual(context["imageLocalPaths"], [str(self.image)])
        self.assertIsNone(
            self.add_campaign_artifact.call_args.kwargs["public_url"]
        )

    def test_unreachable_origin_is_suppressed_for_any_platform(self) -> None:
        """A localhost ``/getFile`` URL must never be stored as a public_url.

        Targets #3449/#3450 handed Bluesky ``http://localhost:5409/getFile`` and
        got a 404 when the local file it fell back to was missing. The origin is
        unreachable from any platform, so the URL is suppressed for byte-upload
        platforms too, not only for the URL-fetch set.
        """
        self.assertFalse(has_request_context())

        for base in (
            "http://localhost:5409",
            "https://localhost:5409",
            "http://127.0.0.1:5409",
        ):
            with self.subTest(base=base):
                self.add_campaign_artifact.reset_mock()
                with mock.patch.object(
                    campaign_media_prep.ops_alerts,
                    "public_app_origin",
                    return_value="",
                ):
                    context = self._call(public_base_url=base)

                self.assertEqual(context["imageUrls"], [])
                self.assertIsNone(
                    self.add_campaign_artifact.call_args.kwargs["public_url"]
                )

    def test_public_origin_helper_rejects_loopback_and_http(self) -> None:
        self.assertFalse(campaign_media_prep._is_public_base_url("http://cdn.example.com"))
        self.assertFalse(campaign_media_prep._is_public_base_url("https://localhost"))
        self.assertFalse(campaign_media_prep._is_public_base_url("https://127.0.0.1:5409"))
        self.assertFalse(campaign_media_prep._is_public_base_url(""))
        self.assertTrue(campaign_media_prep._is_public_base_url("https://cdn.example.com"))
        # A "localhost" path segment on a real host is not the loopback host.
        self.assertTrue(
            campaign_media_prep._is_public_base_url("https://cdn.example.com/localhost")
        )

    def test_configured_public_origin_wins_over_passed_host(self) -> None:
        with mock.patch.object(
            campaign_media_prep.ops_alerts,
            "public_app_origin",
            return_value="https://public.example.com",
        ):
            context = self._call(public_base_url="http://127.0.0.1:5409")

        self.assertEqual(
            context["imageUrls"],
            ["https://public.example.com/getFile?filename=pic.jpg"],
        )


if __name__ == "__main__":
    unittest.main()
