"""Subreddit requirement registry: refuse a submit that cannot succeed."""

import unittest

from myUtils import subreddits as sr


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.reg = sr.get_registry()

    def test_known_lookup_ignores_prefix_and_case(self):
        for key in ("NudistMen", "nudistmen", "r/NudistMen", " r/nudistmen "):
            with self.subTest(key=key):
                self.assertTrue(self.reg.known(key))
        self.assertFalse(self.reg.known("definitely-not-a-real-sub"))

    def test_unknown_subreddit_is_refused_not_allowed(self):
        # The r/GayBros / r/gaybrosgonemild failures came from publishing to
        # subreddits nobody had checked. An unverified sub must be a refusal.
        reason = self.reg.denial_reason("someNewSub", content_kind=sr.NUDITY)
        self.assertIsNotNone(reason)
        self.assertIn("no verified requirement profile", reason)

    def test_banned_subreddit_is_refused_and_names_the_ban(self):
        reason = self.reg.denial_reason("GayBody", content_kind=sr.NUDITY)
        self.assertIn("banned", reason.lower())
        self.assertIn("GayBody", reason)

    def test_monetised_brand_is_refused_where_that_is_banned(self):
        for sub in ("gaybrosgonemild", "lgbt", "gay_irl", "gaymemes", "GayMen"):
            with self.subTest(sub=sub):
                reason = self.reg.denial_reason(
                    sub, content_kind=sr.NUDITY, monetised=True
                )
                self.assertIsNotNone(reason)

    def test_nudity_refused_where_only_sfw_allowed(self):
        reason = self.reg.denial_reason(
            "gaymersgonemild", content_kind=sr.NUDITY, monetised=False
        )
        self.assertIsNotNone(reason)

    def test_nudistmen_accepts_nudity(self):
        self.assertTrue(
            self.reg.fits("NudistMen", content_kind=sr.NUDITY, monetised=True)
        )

    def test_self_post_refused_where_only_links_allowed(self):
        # r/gaybrosgonemild answered a self post with NO_SELFS.
        reason = self.reg.denial_reason(
            "gaybrosgonemild", content_kind=sr.SFW, monetised=False,
            submission_kind="self",
        )
        self.assertIn("NO_SELFS", reason)

    def test_title_pattern_is_enforced(self):
        bad = self.reg.denial_reason(
            "gay_irl", content_kind=sr.SFW, monetised=False, title="hello world"
        )
        self.assertIsNotNone(bad)
        self.assertIn("title format", bad.lower())
        # A conforming title must clear the title gate itself. r/gay_irl still
        # refuses a brand publish for self-promotion, which is a separate rule -
        # so assert the *reason* changed rather than that nothing is returned.
        good = self.reg.denial_reason(
            "gay_irl", content_kind=sr.SFW, monetised=False, title="gay when irl"
        )
        self.assertNotIn("title format", good or "")

    def test_karma_gate(self):
        # Force a gate to prove the comparison, independent of table values.
        profile = sr.SubredditProfile(name="gateTest", min_karma=100)
        reg = sr.SubredditRegistry((profile,))
        self.assertIsNotNone(reg.denial_reason("gateTest", karma=5))
        self.assertIsNone(reg.denial_reason("gateTest", karma=500))

    def test_recommendations_exclude_banned_and_are_largest_first(self):
        recs = self.reg.recommendations(content_kind=sr.NUDITY, monetised=True)
        names = [p.name for p in recs]
        self.assertNotIn("GayBody", names)     # banned
        self.assertNotIn("gaybros", names)     # banned
        self.assertNotIn("softies", names)     # monetised accounts refused
        self.assertIn("NudistMen", names)
        subs = [p.subscribers for p in recs]
        self.assertEqual(subs, sorted(subs, reverse=True))

    def test_audit_pairs_every_configured_subreddit_with_a_reason(self):
        names = ["NudistMen", "GayBody", "lgbt"]
        pairs = self.reg.audit(names, content_kind=sr.NUDITY, monetised=True)
        self.assertEqual([n for n, _ in pairs], names)
        self.assertIsNone(dict(pairs)["NudistMen"])
        self.assertIsNotNone(dict(pairs)["GayBody"])


class ContentKindTests(unittest.TestCase):
    def test_nsfw_media_classifies_as_explicit(self):
        from myUtils import prepared_publishers as pp

        media = {"videos": [{"local_path": "/x/foo_nsfw.mp4", "public_url": ""}]}
        self.assertEqual(pp._reddit_content_kind(media), sr.EXPLICIT)

    def test_naked_sfw_media_classifies_as_nudity(self):
        from myUtils import prepared_publishers as pp

        media = {"videos": [{"local_path": "/x/nakedwill_sfw.mp4", "public_url": ""}]}
        self.assertEqual(pp._reddit_content_kind(media), sr.NUDITY)

    def test_unknown_media_defaults_to_suggestive_not_sfw(self):
        # Must not default to SFW: sending adult media to an SFW sub risks a ban.
        from myUtils import prepared_publishers as pp

        self.assertEqual(
            pp._reddit_content_kind({"videos": [{"local_path": "/x/a.mp4"}]}),
            sr.SUGGESTIVE,
        )
        self.assertEqual(pp._reddit_content_kind({}), sr.SUGGESTIVE)
