"""Generated copy must go through the operator's humanizer rules.

The operator asked for this by name: "remember to use humanizer and
humanizer-zh-tw to humanize all writings beforehand". The rules are ported from
the installed skills (`~/.claude/skills/humanizer`, blader/humanizer v3.1.0 MIT,
and `~/.claude/skills/humanizer-zh`, op7418/Humanizer-zh MIT, plus the local
zh-TW supplement) rather than invented, so a change to the skills' list should
be mirrored here.

Two things are pinned: the detector finds the tells the skills document, and a
failed rewrite can never make the copy worse or block a publish.
"""

from __future__ import annotations

import unittest

from myUtils import humanize


class FindTellsTests(unittest.TestCase):
    def test_english_hard_tells_are_found(self):
        cases = {
            "It is not just a habit but a whole identity.": "not-just-but",
            "It's not a diet, it's a relationship with food.": "it's-not-X",
            "That's the real question.": "closer",
            "At its core, this is about trust.": "deep-sounding",
            "Here's what nobody tells you.": "run-up",
            "I'm not saying you should quit.": "arguing with no one",
        }
        for text, fragment in cases.items():
            tells = humanize.find_tells(text)
            self.assertTrue(any(fragment in tell for tell in tells), f"{text!r} -> {tells}")

    def test_chinese_hard_tells_are_found(self):
        cases = {
            "這不是習慣，而是整個人的認同。": "Z2",
            "這不只是省錢，更是重新定義生活。": "Z2",
            "與其說他懶，不如說他怕。": "Z2",
            "這才是真正的自由。": "Z3",
            "本質上，這是一場練習。": "Z3",
            "說到底，問題還是在自己。": "Z3",
            "值得注意的是，他沒有回應。": "Z4",
        }
        for text, fragment in cases.items():
            tells = humanize.find_tells(text)
            self.assertTrue(any(fragment in tell for tell in tells), f"{text!r} -> {tells}")

    def test_mainland_vocabulary_is_flagged_with_the_taiwan_word(self):
        tells = humanize.find_tells("這部視頻的質量很好。")
        self.assertTrue(any("視頻->影片" in tell for tell in tells), tells)
        self.assertTrue(any("質量->品質" in tell for tell in tells), tells)

    def test_ordinary_prose_is_clean(self):
        for text in (
            "I went for a walk. It rained. I came home.",
            "今天走了三十分鐘，回來洗個澡就睡了。",
            "",
        ):
            self.assertEqual(humanize.find_tells(text), [], text)

    def test_dash_density_is_a_density_rule(self):
        self.assertEqual(humanize.find_tells("One — dash is fine."), [])
        dense = "a—b—c—d—e—f—g—h—i—j—k—l—m—n—o—p—q—r—s—t"
        self.assertTrue(any("破折號" in tell or "dash" in tell.lower() for tell in humanize.find_tells(dense)))


class HumanizeTextTests(unittest.TestCase):
    def test_clean_text_is_never_rewritten(self):
        calls = []

        def generate(*args, **kwargs):
            calls.append(args)
            return type("R", (), {"content": "SHOULD NOT BE USED"})()

        text = "I went for a walk. It rained."
        self.assertEqual(humanize.humanize_text(text, generate=generate), text)
        self.assertEqual(calls, [], "a clean draft must not cost an extra LLM call")

    def test_a_rewrite_that_removes_the_tells_is_kept(self):
        def generate(system, prompt, **kwargs):
            self.assertIn("line editor", system)
            return type("R", (), {"content": "I run because it clears my head."})()

        text = "It's not just exercise, it's a lifestyle."
        self.assertEqual(humanize.humanize_text(text, generate=generate), "I run because it clears my head.")

    def test_a_worse_rewrite_is_discarded(self):
        def generate(*args, **kwargs):
            return type("R", (), {"content": "It is not just a habit but a whole identity, at its core."})()

        text = "It is not just a habit but a whole identity."
        # The rewrite has no fewer tells, so the original stands.
        self.assertEqual(humanize.humanize_text(text, generate=generate), text)

    def test_a_failing_model_returns_the_input_unchanged(self):
        def generate(*args, **kwargs):
            raise RuntimeError("gateway down")

        text = "That's the real question."
        self.assertEqual(humanize.humanize_text(text, generate=generate), text)

    def test_an_empty_rewrite_is_ignored(self):
        def generate(*args, **kwargs):
            return type("R", (), {"content": "   "})()

        text = "That's the real question."
        self.assertEqual(humanize.humanize_text(text, generate=generate), text)

    def test_fields_are_humanized_in_place(self):
        def generate(*args, **kwargs):
            return type("R", (), {"content": "A clean rewrite."})()

        draft = {"message": "That's the real question.", "title": "", "tags": ["x"]}
        humanize.humanize_fields(draft, generate=generate)
        self.assertEqual(draft["message"], "A clean rewrite.")
        self.assertEqual(draft["title"], "")
        self.assertEqual(draft["tags"], ["x"])

    def test_the_prompt_instruction_is_always_offered(self):
        self.assertIn("WRITE LIKE A PERSON", humanize.HUMANIZE_INSTRUCTION)
        self.assertIn("不是A而是B", humanize.HUMANIZE_INSTRUCTION)
        self.assertIn("台灣用語", humanize.HUMANIZE_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
