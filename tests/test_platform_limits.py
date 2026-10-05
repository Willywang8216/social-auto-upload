"""Tests for the single platform-limits table and its enforcement."""

from __future__ import annotations

import unittest

from myUtils import platform_limits as pl


class TableTests(unittest.TestCase):
    def test_supported_platforms_are_fully_mapped(self) -> None:
        for platform in pl.SUPPORTED_PLATFORMS:
            self.assertIn(platform, pl.MESSAGE_MAX_CHARS, platform)
            self.assertIn(platform, pl.MEDIA_MAX_MB, platform)
            self.assertIn(platform, pl.MAX_VIDEOS, platform)

    def test_tiktok_caption_is_the_api_limit(self) -> None:
        # The shared table must agree with the publisher constant (2200), not
        # the stale 150 the sheet importer used.
        self.assertEqual(pl.message_max_chars("tiktok"), 2200)

    def test_bluesky_current_limits(self) -> None:
        self.assertEqual(pl.media_max_mb("bluesky"), 300)
        self.assertEqual(pl.video_max_seconds("bluesky"), 600.0)
        self.assertEqual(pl.max_images("bluesky"), 10)

    def test_x_current_limits(self) -> None:
        self.assertEqual(pl.message_max_chars("twitter"), 280)
        self.assertEqual(pl.video_max_seconds("twitter"), 140.0)
        self.assertEqual(pl.max_images("twitter"), 4)

    def test_rows_and_publishers_agree(self) -> None:
        from myUtils import content_rules
        from myUtils import prepared_publishers as pp

        self.assertEqual(
            content_rules.get_platform_rule("tiktok").max_chars,
            pp.TIKTOK_MAX_CAPTION_CHARS,
        )
        self.assertEqual(
            content_rules.get_platform_rule("threads").max_chars,
            pp.THREADS_MAX_TEXT_CHARS,
        )
        self.assertEqual(pp.THREADS_MAX_VIDEO_BYTES, 1024 * 1_000_000)

    def test_network_lookup(self) -> None:
        self.assertEqual(pl.limits_for_network("tw")["message_max_chars"], 280)
        self.assertEqual(pl.limits_for_network("blsk")["video_max_seconds"], 600.0)


class EnforceMessageLimitTests(unittest.TestCase):
    def test_truncates_over_limit(self) -> None:
        from myUtils import prepared_publishers as pp

        text = "x" * 400
        out = pp._enforce_message_limit(text, "twitter")
        self.assertLessEqual(len(out), 280)

    def test_leaves_within_limit_untouched(self) -> None:
        from myUtils import prepared_publishers as pp

        self.assertEqual(pp._enforce_message_limit("hello", "twitter"), "hello")

    def test_long_form_is_not_trimmed(self) -> None:
        from myUtils import prepared_publishers as pp

        text = "y" * 9000
        self.assertEqual(pp._enforce_message_limit(text, "nw_sw_blog"), text)


class NormalizePublicUrlTests(unittest.TestCase):
    def test_encodes_spaces_in_path_and_query(self) -> None:
        from myUtils import prepared_publishers as pp

        out = pp._normalize_public_url(
            "https://cdn.example/campaigns/1/SFW clip.mp4?name=a b"
        )
        self.assertEqual(
            out, "https://cdn.example/campaigns/1/SFW%20clip.mp4?name=a%20b"
        )

    def test_leaves_already_encoded_url_untouched(self) -> None:
        from myUtils import prepared_publishers as pp

        url = "https://cdn.example/a%20b/c.mp4"
        self.assertEqual(pp._normalize_public_url(url), url)

    def test_non_url_is_returned_as_is(self) -> None:
        from myUtils import prepared_publishers as pp

        self.assertEqual(pp._normalize_public_url("not a url"), "not a url")


class EnforceVideoLimitTests(unittest.TestCase):
    def test_rejects_video_over_duration(self) -> None:
        import tempfile
        from unittest.mock import patch

        from myUtils import media_pipeline
        from myUtils import prepared_publishers as pp

        with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
            with patch.object(media_pipeline, "probe_video_duration", return_value=999.0):
                with self.assertRaises(pp.PreparedPublishError):
                    pp._enforce_video_limits(handle.name, "twitter")

    def test_accepts_within_limits(self) -> None:
        import tempfile
        from unittest.mock import patch

        from myUtils import media_pipeline
        from myUtils import prepared_publishers as pp

        with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
            with patch.object(media_pipeline, "probe_video_duration", return_value=30.0):
                pp._enforce_video_limits(handle.name, "twitter")


class FallbackNetworkCoverageTests(unittest.TestCase):
    def test_every_mapped_network_gets_its_message_limit(self) -> None:
        from myUtils import sociamonials_fallback as sm

        for code, platform in pl.NETWORK_TO_PLATFORM.items():
            expected = pl.message_max_chars(platform)
            if expected is None:
                continue
            self.assertEqual(sm.NETWORK_MAX_MESSAGE_CHARS.get(code), expected, code)

    def test_instagram_and_threads_video_caps_present(self) -> None:
        from myUtils import sociamonials_fallback as sm

        self.assertEqual(sm.NETWORK_MAX_VIDEO_SECONDS.get("in"), 900.0)
        self.assertEqual(sm.NETWORK_MAX_VIDEO_SECONDS.get("thrd"), 300.0)
        self.assertEqual(sm.NETWORK_MAX_VIDEO_SECONDS.get("tw"), 140.0)


if __name__ == "__main__":
    unittest.main()
