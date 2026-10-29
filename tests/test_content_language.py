"""Content language must come from the media, not from generated copy.

The operator reported "English videos on my Chinese channel" three times while
the audit printed 0 violations. Three separate defects combined:

* the hosted STT gateway died on 2026-10-27, so no transcript existed;
* the language then fell back to the campaign *title*, which draft generation
  writes **in the target account's language** — making the routing comparison a
  tautology that always matched;
* an empty title produced ``None``, which the router treats as "do not filter",
  so the *absence* of information disabled routing entirely.

And a fourth, in the other direction: a language-neutral filename such as
``20261024095109313__nsfw.mp4`` or ``GjTNV3GXAAAxyCq__nsfw.jpg`` was classified
"English" merely for lacking CJK, which made a first sweep report 1 882
"violations" of which most were noise.
"""

from __future__ import annotations

import unittest

from myUtils import content_language as cl


class ClassifyTextTests(unittest.TestCase):
    def test_chinese_is_chinese(self):
        for text in ("月底遊行邀請", "哈囉大家好", "聰明生活經濟學", "繁體中文標題"):
            self.assertEqual(cl.classify_text(text), "zh", text)

    def test_real_english_words_are_english(self):
        for text in ("Why do people have kids", "Naked truth and Pride invite", "postop"):
            self.assertEqual(cl.classify_text(text), "en", text)

    def test_a_timestamp_is_not_a_language(self):
        for text in ("20261024095109313", "20260912", "0", "___"):
            self.assertIsNone(cl.classify_text(text), text)

    def test_a_media_id_is_not_a_language(self):
        for text in ("GjTNV3GXAAAxyCq", "AAAxyCq", "e61472bb267844e5a1b75fe646d01a51"):
            self.assertIsNone(cl.classify_text(text), text)

    def test_too_short_to_say(self):
        for text in ("a", "ab", "abc", ""):
            self.assertIsNone(cl.classify_text(text), text)


class FilenameTests(unittest.TestCase):
    def test_staging_prefix_and_rating_tag_are_stripped(self):
        self.assertEqual(
            cl.clean_filename("e61472bb267844e5a1b75fe646d01a51_The Anatomy of Connection__sfw.mp4"),
            "The Anatomy of Connection",
        )
        self.assertEqual(cl.clean_filename("Pride Parade_SFW_20261027.jpg"), "Pride Parade")

    def test_an_explicit_marker_wins(self):
        self.assertEqual(cl.detect_from_evidence(filenames=["20260912_postop_5wk_zh.mp4"]), ("zh", "filename"))
        self.assertEqual(cl.detect_from_evidence(filenames=["20260912_erotic_novel_en_sfw.mp4"]), ("en", "filename"))

    def test_mixed_names_are_neutral(self):
        language, _ = cl.detect_from_evidence(filenames=["Why do people have kids__sfw.mp4", "月底遊行__sfw.mp4"])
        self.assertIsNone(language)

    def test_agreeing_names_decide(self):
        language, source = cl.detect_from_evidence(
            filenames=["Naked truth and Pride invite__sfw.mp4", "Naked truth and Pride invite 2__nsfw.mp4"]
        )
        self.assertEqual((language, source), ("en", "filename"))


class EvidenceOrderTests(unittest.TestCase):
    def test_a_neutral_filename_lets_the_brief_decide(self):
        self.assertEqual(
            cl.detect_from_evidence(filenames=["__sfw.mp4"], brief="Anti-natalism and parental regret"),
            ("en", "brief"),
        )

    def test_nothing_to_go_on_is_unknown(self):
        self.assertEqual(cl.detect_from_evidence(filenames=["__sfw.mp4"]), (None, None))

    def test_a_strong_source_beats_a_stored_weak_one(self):
        language, source = cl.resolve(
            metadata={cl.CONTENT_LANGUAGE_KEY: "zh", cl.CONTENT_LANGUAGE_SOURCE_KEY: "title"},
            media_context={"language": "en", "languageSource": "transcript"},
        )
        self.assertEqual((language, source), ("en", "transcript"))

    def test_a_stored_strong_source_is_used(self):
        language, source = cl.resolve(
            metadata={cl.CONTENT_LANGUAGE_KEY: "en", cl.CONTENT_LANGUAGE_SOURCE_KEY: "transcript"},
            media_context={"language": "zh"},
        )
        self.assertEqual((language, source), ("en", "transcript"))

    def test_metadata_round_trips(self):
        self.assertEqual(cl.from_metadata('{"contentLanguage": "zh", "contentLanguageSource": "audio"}'),
                         ("zh", "audio"))
        self.assertEqual(cl.from_metadata({"contentLanguage": "EN"}), ("en", None))
        self.assertEqual(cl.from_metadata(None), (None, None))


class RouterIntegrationTests(unittest.TestCase):
    def test_a_neutral_filename_no_longer_disables_the_check(self):
        """The exact failure: no transcript, empty title, real English name."""
        language, source = cl.detect_from_evidence(filenames=["Why do people have kids__sfw.mp4"])
        self.assertEqual(language, "en")
        from myUtils import publish_orchestrator

        class _Account:
            config = {"audience_language": "zh-Hant"}

        reason = publish_orchestrator.language_routing_skip_reason(language, _Account(), None)
        self.assertIsNotNone(reason)
        self.assertIn("Chinese-only", reason)


if __name__ == "__main__":
    unittest.main()
