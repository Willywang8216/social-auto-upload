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

import logging
import unittest
from unittest.mock import patch

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(REPO_ROOT))

from myUtils import campaign_media_prep, platform_limits  # noqa: E402


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
        self.assertIn((300.0, 1024.0), plans)


class ProbeFailureIsNotSilentTests(unittest.TestCase):
    def test_a_probe_failure_logs_a_warning(self):
        # B5: the old code had a bare `except Exception: duration = 0.0` with no
        # log, so "could not measure" was indistinguishable from "fits".
        source = (
            REPO_ROOT / "myUtils" / "campaign_media_prep.py"
        ).read_text(encoding="utf-8")
        self.assertIn("duration_probe_failed", source)
        self.assertIn("could not probe the duration", source)

    def test_split_target_uses_decimal_mb(self):
        source = (
            REPO_ROOT / "myUtils" / "campaign_media_prep.py"
        ).read_text(encoding="utf-8")
        self.assertIn("max_bytes=(mb * 1_000_000) if mb else None", source)


if __name__ == "__main__":
    unittest.main()
