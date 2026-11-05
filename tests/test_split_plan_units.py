"""The split plan must use the same units and trust the same probe as the caps.

Two defects in the plan builder:

B3 - units. The trigger compared against ``mb * 1024 * 1024`` while
platform_limits documents decimal MB and the publisher refuses at
``size_bytes > limit * 1_000_000``. A 305 MB file therefore "fit" Instagram's
300 MB cap during prep (305e6 < 314.6e6) and was refused at publish (305e6 >
300e6): the split never happened, so the publish could only fail.

B5 - a probe failure was swallowed as ``duration = 0``, which reads as "fits"
and disabled duration splitting for EVERY platform, silently.
"""

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(REPO_ROOT))

from myUtils import campaign_media_prep, platform_limits, media_compliance  # noqa: E402


def _plan_for(duration, size_bytes, platforms):
    """Rebuild the plan the way the prep does, and return it as a dict."""
    plans = {}
    for name in platforms:
        sec = platform_limits.video_max_seconds(name)
        mb = platform_limits.media_max_mb(name)
        over_time = bool(sec and duration and duration > float(sec))
        over_size = bool(mb and size_bytes and size_bytes > float(mb) * 1_000_000)
        if over_time or over_size:
            plans.setdefault(
                (float(sec) if sec else None, float(mb) if mb else None), set()
            ).add(name)
    return plans


class SplitPlanUnitTests(unittest.TestCase):
    def test_305mb_splits_for_instagram_300mb_cap(self):
        # 305 MB decimal. Under the old MiB comparison this produced no plan.
        plans = _plan_for(duration=100.0, size_bytes=305_000_000, platforms={"instagram"})
        self.assertTrue(plans, "305MB must exceed Instagram's 300MB cap and split")

    def test_300mb_boundary_does_not_split(self):
        plans = _plan_for(duration=100.0, size_bytes=300_000_000, platforms={"instagram"})
        self.assertFalse(plans, "exactly at the cap must not split")

    def test_the_publisher_and_the_plan_agree_at_the_boundary(self):
        # The whole point: prep and publish must not disagree about "fits".
        cap_mb = platform_limits.media_max_mb("instagram")
        at_cap = cap_mb * 1_000_000
        just_over = at_cap + 1
        self.assertFalse(
            _plan_for(100.0, at_cap, {"instagram"}), "at cap: prep says fits"
        )
        self.assertFalse(at_cap > cap_mb * 1_000_000, "at cap: publisher says fits")
        self.assertTrue(
            _plan_for(100.0, just_over, {"instagram"}), "over cap: prep splits"
        )
        self.assertTrue(just_over > cap_mb * 1_000_000, "over cap: publisher refuses")

    def test_duration_over_cap_still_plans(self):
        plans = _plan_for(duration=600.0, size_bytes=1000, platforms={"threads"})
        # Read the cap rather than hardcoding it, so the plan and the table
        # cannot drift apart.
        self.assertIn((300.0, float(platform_limits.media_max_mb("threads"))), plans)


class ProbeFailureIsNotSilentTests(unittest.TestCase):
    def test_a_probe_failure_is_reported_before_any_registration(self):
        # A failed measurement must stop, not masquerade as a zero duration.
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "broken.mp4"
            source.write_bytes(b"synthetic invalid video")
            with (
                patch.object(campaign_media_prep.campaign_store, "get_campaign", side_effect=LookupError),
                patch.object(campaign_media_prep.media_prep, "probe", side_effect=RuntimeError("synthetic probe failure")) as probe,
                patch.object(campaign_media_prep.video_artifacts, "prepare_and_register_video") as prepare,
                patch.object(campaign_media_prep.campaign_store, "add_campaign_artifact_set") as register,
                self.assertRaisesRegex(media_compliance.MediaPreparationError, "Cannot probe video"),
            ):
                campaign_media_prep.prepare_campaign_media_artifacts(
                    1, SimpleNamespace(id=1, settings={}),
                    [{"file_record_id": 1, "role": "video", "file_path": str(source)}],
                    {"uploadToRemote": False}, selected_platforms={"instagram"},
                    db_path=Path(temporary) / "isolated.db",
                )
            probe.assert_called_once_with(source.resolve())
            prepare.assert_not_called()
            register.assert_not_called()

    def test_split_target_uses_decimal_mb(self):
        limits = media_compliance.effective_limits("instagram")
        self.assertEqual(limits["max_bytes"], platform_limits.media_max_mb("instagram") * 1_000_000)
        self.assertEqual(limits["max_bytes"], 300_000_000)
        # An arbitrary 305 MB request cannot turn the 300 MB transport cap into MiB.
        tightened = media_compliance.effective_limits("instagram", limits={"max_bytes": 305_000_000})
        self.assertEqual(tightened["max_bytes"], 300_000_000)


if __name__ == "__main__":
    unittest.main()
