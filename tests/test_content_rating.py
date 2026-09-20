"""Tests for content_rating — the single NSFW gate every publish path uses.

The operator's rule: a file is SFW only when its name starts or ends with
``sfw``. Everything else is NSFW, including unlabelled files. These tests pin
that default because getting it backwards sends nudity to Instagram.
"""
import unittest

from myUtils import content_rating as cr


class _Acct:
    def __init__(self, id, platform):
        self.id = id
        self.platform = platform


class RatingForFilenameTests(unittest.TestCase):
    def test_prefix_sfw_is_sfw(self):
        self.assertEqual(cr.rating_for_filename("SFW 20260813074703243.mp4"), "sfw")

    def test_suffix_sfw_is_sfw(self):
        self.assertEqual(cr.rating_for_filename("clip sfw.mp4"), "sfw")

    def test_case_insensitive(self):
        self.assertEqual(cr.rating_for_filename("sfw_morning.mp4"), "sfw")
        self.assertEqual(cr.rating_for_filename("Morning SFW.MP4"), "sfw")

    def test_unlabelled_is_nsfw(self):
        # The default that matters: no marker means NSFW, not "unknown".
        self.assertEqual(cr.rating_for_filename("20260722155038425.mp4"), "nsfw")
        self.assertEqual(cr.rating_for_filename("1T.mp4"), "nsfw")

    def test_sfw_in_the_middle_is_not_sfw(self):
        # Rule says start OR end. A token buried in the middle doesn't count.
        self.assertEqual(cr.rating_for_filename("clip_sfw_final_v2.mp4"), "nsfw")

    def test_pipeline_suffixes_are_stripped_before_the_edge_check(self):
        self.assertEqual(cr.rating_for_filename("SFW clip_part2_pub.mp4"), "sfw")
        self.assertEqual(cr.rating_for_filename("clip sfw_pub.mp4"), "sfw")
        self.assertEqual(cr.rating_for_filename("clip_part1_pub.mp4"), "nsfw")

    def test_directory_named_sfw_does_not_launder_the_file(self):
        self.assertEqual(cr.rating_for_filename("/sfw_batch/20260722.mp4"), "nsfw")
        self.assertEqual(cr.rating_for_filename("C:\\sfw\\raw.mp4"), "nsfw")

    def test_word_boundary_prevents_false_positives(self):
        # "sfwx..." or "...xsfw" is not the token.
        self.assertEqual(cr.rating_for_filename("sfwxyz.mp4"), "nsfw")
        self.assertEqual(cr.rating_for_filename("xyzsfw.mp4"), "nsfw")


class RatingForMediaTests(unittest.TestCase):
    def test_all_sfw_is_sfw(self):
        self.assertEqual(cr.rating_for_media(["SFW a.mp4", "b sfw.jpg"]), "sfw")

    def test_one_nsfw_file_taints_the_batch(self):
        self.assertEqual(cr.rating_for_media(["SFW a.mp4", "b.mp4"]), "nsfw")

    def test_empty_is_nsfw(self):
        self.assertEqual(cr.rating_for_media([]), "nsfw")

    def test_explicit_nsfw_flag_overrides_sfw_filename(self):
        # A caller can make it stricter.
        self.assertEqual(cr.rating_for_media(["SFW a.mp4"], explicit="nsfw"), "nsfw")

    def test_explicit_sfw_flag_cannot_launder_nsfw_filename(self):
        # A caller can never make it looser.
        self.assertEqual(cr.rating_for_media(["a.mp4"], explicit="sfw"), "nsfw")

    def test_explicit_flag_is_case_insensitive(self):
        self.assertEqual(cr.rating_for_media(["SFW a.mp4"], explicit="NSFW"), "nsfw")


class RestrictAccountsTests(unittest.TestCase):
    def _profile(self):
        return [
            _Acct(118, "bluesky"), _Acct(116, "telegram"), _Acct(123, "twitter"),
            _Acct(105, "reddit"), _Acct(11, "facebook"), _Acct(72, "instagram"),
            _Acct(62, "threads"), _Acct(110, "youtube"), _Acct(109, "tiktok"),
        ]

    def test_sfw_keeps_every_account(self):
        out = cr.restrict_accounts(self._profile(), "sfw")
        self.assertEqual([a.id for a in out], [118, 116, 123, 105, 11, 72, 62, 110, 109])

    def test_nsfw_drops_every_restricted_platform(self):
        out = cr.restrict_accounts(self._profile(), "nsfw")
        self.assertEqual([a.id for a in out], [118, 116, 123, 105])
        self.assertFalse({a.platform for a in out} & cr.NSFW_RESTRICTED_PLATFORMS)

    def test_nsfw_with_only_restricted_accounts_raises_instead_of_returning_empty(self):
        only_restricted = [_Acct(11, "facebook"), _Acct(72, "instagram")]
        with self.assertRaises(ValueError) as ctx:
            cr.restrict_accounts(only_restricted, "nsfw")
        self.assertIn("refusing", str(ctx.exception))

    def test_platform_match_is_case_and_whitespace_insensitive(self):
        out = cr.restrict_accounts([_Acct(1, " Instagram "), _Acct(2, "bluesky")], "nsfw")
        self.assertEqual([a.id for a in out], [2])

    def test_restricted_list_is_the_documented_five(self):
        self.assertEqual(
            cr.NSFW_RESTRICTED_PLATFORMS,
            frozenset({"instagram", "facebook", "threads", "youtube", "tiktok"}),
        )


if __name__ == "__main__":
    unittest.main()
