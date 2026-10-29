"""Run generated copy through the operator's humanizer rules before it ships.

The operator asked for this explicitly: *"remember to use humanizer and
humanizer-zh-tw to humanize all writings beforehand"*. The rules are the
installed skills' own — ``~/.claude/skills/humanizer`` (blader/humanizer v3.1.0,
MIT) and ``~/.claude/skills/humanizer-zh`` (op7418/Humanizer-zh, MIT, 歸藏, plus
the local zh-TW supplement). They are ported rather than reinvented: the regex
table below is the skills' ``scripts/check_tells.py``, which is the distilled
form of ``reference/patterns-en.md`` §1-§8 and ``reference/zh-tw-tells.md``
Z1-Z8.

How it is used
--------------
Two passes, cheapest first:

1. **Prompt** — :data:`HUMANIZE_INSTRUCTION` is appended to every draft prompt,
   so the model does not produce the tells in the first place. This is the pass
   that matters; it costs nothing.
2. **Rewrite** — :func:`humanize_text` checks the finished copy with
   :func:`find_tells` and, only when something is actually wrong, asks the model
   for one rewrite. The rewrite is re-checked; a second failure keeps the
   *better* of the two rather than looping.

Nothing here may block a publish: any failure returns the input unchanged. The
copy that reaches the operator is at worst what it would have been without this
module.
"""

from __future__ import annotations

import logging
import re

LOGGER = logging.getLogger(__name__)

# --- the skills' hard tells (one sighting is enough) ------------------------
EN_HARD: tuple[tuple[str, str], ...] = (
    (r"\bnot just\b[^.]{0,80}\bbut\b", "§1 not-just-but"),
    (r"\bnot only\b[^.]{0,80}\bbut\b", "§1 not-only-but"),
    (r"\bit'?s not\b[^.]{0,60}\bit'?s\b", "§1 it's-not-X-it's-Y"),
    (r"\bthis (?:does not|doesn'?t) mean\b", "§1 split contrast"),
    (r"\bthat'?s the (?:real|whole) \w+", "§2 closer"),
    (r"\bread that again\b", "§2 closer"),
    (r"\blet that sink in\b", "§2 closer"),
    (r"\bthe real question\b", "§3 deep-sounding"),
    (r"\bat its core\b", "§3 deep-sounding"),
    (r"\bwhat really matters\b", "§3 deep-sounding"),
    (r"\bhere'?s (?:what|the thing)\b", "§4 run-up"),
    (r"\blet'?s dive in\b", "§4 run-up"),
    (r"\bthis isn'?t (?:about|mainly)\b", "§5 arguing with no one"),
    (r"\bi'?m not saying\b", "§5 arguing with no one"),
    (r"\bto be clear\b", "§5 arguing with no one"),
)

ZH_HARD: tuple[tuple[str, str], ...] = (
    # The skill's own regex excludes the comma, so 「這不是習慣，而是…」 slipped
    # through its check. A comma is exactly how the contrast is written in
    # Chinese, so only a sentence ender should stop the match.
    (r"不是[^。！？；]{0,30}?而是", "Z2 不是A而是B"),
    (r"不只是[^。！？；]{0,30}?更是", "Z2 不只是更是"),
    (r"與其說[^。]{0,30}不如說", "Z2 與其說不如說"),
    (r"這才是真正的", "Z3 這才是真正的"),
    (r"真正(?:該問|的問題|的關鍵)的?是", "Z3 真正的X"),
    (r"本質上", "Z3 本質上"),
    (r"說到底", "Z3 說到底"),
    (r"值得注意的是", "Z4 空轉總結"),
    (r"這告訴我們", "Z4 空轉總結"),
    (r"這說明了一件事", "Z4 空轉總結"),
    (r"從這個角度看", "Z4 空轉總結"),
    (r"讓我們(?:來)?看", "Z9 導覽語"),
)

# --- mainland vocabulary that pulls a Taiwanese reader out (Z8) -------------
CN_WORDS: dict[str, str] = {
    "視頻": "影片", "信息": "資訊", "網絡": "網路", "軟件": "軟體", "硬件": "硬體",
    "質量": "品質", "默認": "預設", "優化": "最佳化", "屏幕": "螢幕",
    "打印": "列印", "反饋": "回饋", "內存": "記憶體",
}

#: Soft tells: only a *density* problem, never a single sighting.
EN_SOFT = (
    r"\bactually\b", r"\badditionally\b", r"\bcrucial\b", r"\bdelve\b",
    r"\bunderscore[sd]?\b", r"\btestament\b", r"\bpivotal\b", r"\brobust\b",
    r"\bvibrant\b", r"\bintricate\b", r"\bmeticulous\w*\b", r"\bshowcas\w+\b",
    r"\btapestry\b", r"\blandscape\b", r"\bseamless\w*\b", r"\bleverage\b",
    r"\b foster\w*\b", r"\bgarner\w*\b", r"\benhance\w*\b", r"\bholistic\b",
)
ZH_SOFT = (
    r"與此同時", r"不僅", r"首先", r"其次", r"在某種程度上", r"從某種意義上",
    r"無疑", r"顯而易見", r"毋庸置疑", r"隨著", r"進行", r"以及", r"層面",
    r"維度", r"賦能", r"閉環",
)

_CJK = re.compile(r"[\u3400-\u9fff]")
#: §8/Z1 is about *density*, not presence: the skill's own guidance is "a 3000
#: character Chinese piece with more than 5 dashes deserves a look". Social copy
#: is far shorter, so a density-only rule flagged every post containing a single
#: dash (one dash in 15 characters is 66 per 1000). Require both a count and a
#: density before calling it a tell.
_DASHES_PER_1000 = 2.0
_DASH_MIN_COUNT = 3
_SOFT_PER_1000 = 12.0
_SOFT_MIN_COUNT = 4

#: Appended to every draft prompt (pass 1). Deliberately short — a long rule
#: list makes the model write *about* the rules instead of the post.
HUMANIZE_INSTRUCTION = (
    "\n\nWRITE LIKE A PERSON (this is checked):\n"
    "- Vary sentence length. Do not end on a one-line punch.\n"
    "- No 'not just X but Y', no 'it's not X, it's Y', no 'the real question is',\n"
    "  no 'at its core', no 'here's the thing', no 'let's dive in'.\n"
    "- No triads for rhythm, no bold labels on every line, no emoji per item.\n"
    "- Use at most one dash in the whole post.\n"
    "中文：不要「不是A而是B」「不只是A更是B」「與其說…不如說」「這才是真正的」\n"
    "「本質上」「說到底」「值得注意的是」「這告訴我們」。少用破折號，不要條列式排比。\n"
    "台灣用語：影片、資訊、網路、軟體、品質、預設、最佳化、螢幕。\n"
)


def is_chinese(text: str) -> bool:
    return bool(_CJK.search(str(text or "")))


def find_tells(text: str) -> list[str]:
    """The skill's deterministic check. Empty list means the copy is clean."""
    value = str(text or "")
    if not value.strip():
        return []
    tells: list[str] = []
    for pattern, label in EN_HARD:
        hits = re.findall(pattern, value, re.I)
        if hits:
            tells.append(f"{label} x{len(hits)}")
    for pattern, label in ZH_HARD:
        hits = re.findall(pattern, value)
        if hits:
            tells.append(f"{label} x{len(hits)}")
    for mainland, taiwan in CN_WORDS.items():
        if mainland in value:
            tells.append(f"Z8 中國用語 {mainland}->{taiwan} x{value.count(mainland)}")
    body = len(re.sub(r"\s", "", value)) or 1
    dashes = value.count("\u2014") + value.count("\u2013")
    if dashes >= _DASH_MIN_COUNT and dashes * 1000 / body > _DASHES_PER_1000:
        tells.append(f"§8/Z1 破折號 x{dashes} ({dashes * 1000 / body:.1f}/1000)")
    soft_patterns = EN_SOFT if not is_chinese(value) else ZH_SOFT
    soft = sum(len(re.findall(pattern, value, re.I)) for pattern in soft_patterns)
    if soft >= _SOFT_MIN_COUNT and soft * 1000 / body > _SOFT_PER_1000:
        tells.append(f"soft filler x{soft} ({soft * 1000 / body:.1f}/1000)")
    return tells


_REWRITE_SYSTEM = (
    "You are a line editor. Rewrite the text so it reads like one person wrote it "
    "for one reader. Keep every fact, every claim and the language it is written in. "
    "Do not add anything, do not explain, do not translate.\n\n"
    "Fix exactly these AI tells: not-just-but / it's-not-X-it's-Y / split contrast; "
    "one-line closers ('that's the real X', 'read that again', 'let that sink in'); "
    "deep-sounding openers ('the real question', 'at its core', 'what really matters'); "
    "run-ups ('here's what', 'let's dive in'); arguing with nobody ('I'm not saying', "
    "'to be clear'); forced triads; bold labels and emoji on every line; more than one "
    "dash.\n"
    "中文：不要「不是A而是B」「不只是A更是B」「與其說…不如說」「這才是真正的」「本質上」"
    "「說到底」「值得注意的是」「這告訴我們」。少用破折號。\n"
    "台灣用語：影片、資訊、網路、軟體、品質、預設、最佳化、螢幕。\n"
    "Vary sentence length. Return ONLY the rewritten text."
)


def humanize_text(
    text: str,
    *,
    language: str | None = None,
    max_passes: int = 1,
    generate=None,
) -> str:
    """Rewrite ``text`` until the tells are gone (bounded), else return it as-is.

    ``generate`` is injectable for tests; it defaults to
    :func:`myUtils.llm_client.generate_chat_completion` and must accept
    ``(system_prompt, user_prompt, temperature=...)`` and return an object with
    ``.content``.
    """
    original = str(text or "")
    if not original.strip():
        return original
    tells = find_tells(original)
    if not tells:
        return original
    if generate is None:
        try:
            from myUtils import llm_client

            generate = llm_client.generate_chat_completion
        except Exception:  # noqa: BLE001
            return original
    best = original
    best_tells = tells
    current = original
    for attempt in range(max(1, max_passes)):
        prompt = (
            f"Language: {language or ('zh-Hant' if is_chinese(original) else 'en')}\n"
            f"Detected AI tells to remove: {', '.join(best_tells)}\n\n"
            f"--- TEXT ---\n{current}"
        )
        try:
            result = generate(_REWRITE_SYSTEM, prompt, temperature=0.4)
            candidate = str(getattr(result, "content", result) or "").strip()
        except Exception as exc:  # noqa: BLE001 - never block a publish
            LOGGER.warning("humanize rewrite failed: %s", exc)
            break
        if not candidate:
            break
        candidate_tells = find_tells(candidate)
        if len(candidate_tells) < len(best_tells):
            best, best_tells = candidate, candidate_tells
        if not candidate_tells:
            break
        current = candidate
    return best


def humanize_fields(draft: dict, fields: tuple[str, ...] = ("message", "title", "description"),
                    *, language: str | None = None, generate=None) -> dict:
    """Humanize the text fields of a draft dict in place (returns it)."""
    if not isinstance(draft, dict):
        return draft
    for field in fields:
        value = draft.get(field)
        if isinstance(value, str) and value.strip():
            rewritten = humanize_text(value, language=language, generate=generate)
            if rewritten != value:
                draft[field] = rewritten
    return draft
