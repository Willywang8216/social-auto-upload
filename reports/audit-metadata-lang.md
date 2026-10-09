# Audit: metadata generation, content language, and per-platform limits

**Repo:** `/home/will/social-auto-upload`
**Mode:** read-only (no code changed)
**Date of DB snapshot:** `db/database.db` (mtime 2026-10-09 09:07)
**Scope:** `myUtils/content_rules.py`, the draft generator, `myUtils/worker.py`,
`myUtils/campaign_prep.py`, `myUtils/publish_orchestrator.py`,
`scripts/reddit_flairs.py`, `myUtils/reddit_review.py`, `myUtils/platform_limits.py`,
`myUtils/prepared_publishers.py`.

---

## 0. Executive answer

1. **Drafts are generated per account, not once per campaign.** The production
   funnel calls `sau_backend._generate_account_draft(account, ...)` inside a
   per-account loop in every path, and that function injects
   `_accountLanguage` from the account into the prompt. An `en` and a
   `zh-Hant` account on the same profile therefore each get their own LLM call
   with their own language requirement. See §1.
2. **The profile-level `default_language` column is never consulted** (bug #1)
   because `_account_audience_language` reads `profile.settings` (a dict that is
   always truthy) and never falls through to `profile.default_language`. An
   account with no `audience_language` on a profile whose *column* says
   `zh-Hant` gets **no language constraint at all** — Chinese or English is
   accepted blindly. The live DB happens to give every one of the 33 accounts an
   explicit `audience_language`, so this is latent today, but it is exactly the
   account-108 scenario in the brief (see §2/§3).
3. **The Simplified-Chinese guard has holes** (bug #2): `contains_simplified_chinese`
   returns `False` for `中国人`, `为什么`, `美国`, so those are accepted on a
   `zh-Hant` account.
4. **Per-platform max-length is enforced as code points, not platform units and
   not grapheme clusters** (bug #3). Twitter CJK is weighted ×2 by X but counted
   ×1 here; trimming can cut a word or an emoji-ZWJ cluster; and appended
   hashtags are trimmed off while the `hashtags` field still claims 3.
5. **Hashtag/emoji/CTA rules are only enforced for twitter, bluesky and threads**
   (bug #4/#7). Everything else is prompt-only, and the *override* path used by
   the normal Publish Center flow skips `prepare_platform_draft` entirely.
6. **Simplified can leak into a zh-Hant account** because of #3. The generator
   also adds a "TAIWAN MANDARIN RULES" instruction, but the deterministic
   backstop is the incomplete character set.

---

## 1. Trace: is the draft once per campaign or per account?

**It is per account.** The single choke point is
`sau_backend._generate_account_draft` (`sau_backend.py:3689`). It builds a
per-account request copy and sets the language:

```python
# sau_backend.py:3722-3725
extended_data["_accountContext"] = "\n".join(account_context_lines) + nonce_suffix
extended_data["_accountLanguage"] = _account_audience_language(account, profile)
return _generate_platform_draft(account.platform, profile, media_group, extended_data, media_context)
```

`_build_generation_prompt` (`sau_backend.py:3505`) then appends the per-account
language block (`sau_backend.py:3554-3583`):

```python
language = str(request_data.get("_accountLanguage") or "").strip()
if language:
    labels = content_generator._parse_languages(language)
    if any("Chinese" in label for label in labels) or "zh" in language.lower():
        user_lines.extend((... "TAIWAN MANDARIN RULES ...",))
    if len(labels) == 1:
        user_lines.extend((... "LANGUAGE REQUIREMENT (highest priority): Write the entire output ... in {labels[0]} ...",))
```

And `_generate_platform_draft` (`sau_backend.py:3591`) re-checks after the LLM
returns (`sau_backend.py:3668-3675`):

```python
if account_language and not content_rules.message_matches_language(message, account_language):
    raise RuntimeError(f"Generated copy does not match the account language '{account_language}'; refusing to queue it")
```

**All production call sites are inside a per-account loop:**

| Path | Call site | Loop |
|---|---|---|
| `/campaigns/prepare` | `sau_backend.py:6187` | `for platform, platform_accounts ... for account in platform_accounts` (`sau_backend.py:6184-6187`) |
| sync submit | `myUtils/publish_orchestrator.py:530` | `for account in platform_accounts` (`:503`) |
| async prep | `myUtils/campaign_prep.py:151` | same loop (`:125`) |
| preview | `sau_backend.py:6824` | `for account in accounts` (`:6818`) |
| regenerate | `sau_backend.py:6872` | single account (endpoint takes `accountId`) |
| MCP preview/regenerate | `mcp_server/tools/publish.py:154,215` | per account |
| repair script | `scripts/regenerate_bad_drafts.py:242` | per account |

Frontend confirmation: `PublishCenter.vue:1446` `buildAccountDraftsPayload()`
builds `out[account.accountId] = account.draft` from the per-account preview
(`PublishCenter.vue:1446-1453`). So even the "override" submission is keyed per
account, and `finalize_campaign` looks up
`account_drafts.get(str(account.id))` (`campaign_prep.py:129`,
`publish_orchestrator.py:505`).

**Conclusion:** an `en` account and a `zh-Hant` account on one campaign are
correct because they are two separate LLM calls; the only shared objects are
`request_data` and `media_context` (media, brief, contact/CTA), and language is
injected per account afterwards. The test
`tests/test_publish_center.py::test_preview_generates_account_specific_language_drafts`
asserts exactly this (it passed in the run in §5).

> Caveat (bug #1): "per account" is only as good as the language that is
> computed. If the account has no `audience_language`, the computed language is
> `""` and no language is enforced at all, even when the profile column says
> otherwise.

---

## 2. Per-account table

Effective language = account `config.audience_language` (all 33 accounts have
one in the live DB). Because the code ignores the `profiles.default_language`
column, this equals the *only* language signal that currently works. Copy
language is what the generator is instructed to produce and what the guard will
accept.

| account | profile | platform | config language | profile column | generated copy language | correct? |
|---|---|---|---|---|---|---|
| 11 | NW | facebook | en | en | English | yes |
| 62 | NW | threads | en | en | English (+contact+CTA enforced) | yes |
| 72 | NW | instagram | en | en | English | yes |
| 105 | NW | reddit | en | en | English | yes |
| 109 | NW | tiktok | en | en | English | yes |
| 110 | NW | youtube | en | en | English | yes |
| 112 | NW | nw_sw_blog | en | en | English | yes |
| 116 | NW | telegram | en | en | English | yes |
| 118 | NW | bluesky | en | en | English (3 hashtags enforced) | yes |
| **119** | NW | bluesky | **zh-Hant** | en | Traditional Chinese | yes |
| 123 | NW | twitter | en | en | English (emoji+3 tags enforced) | yes |
| **124** | NW | twitter | **zh-Hant** | en | Traditional Chinese | yes (see bug #3/#4) |
| **127** | NW | telegram | **zh-Hant** | en | Traditional Chinese | yes |
| 42 | SW | threads | en | en | English | yes |
| 64 | SW | facebook | en | en | English | yes |
| 75 | SW | instagram | en | en | English | yes |
| 77 | SW | twitter | en | en | English | yes |
| **103** | SW | twitter | **zh-Hant** | en | Traditional Chinese | yes (see bug #3/#4) |
| 106 | SW | reddit | en | en | English | yes |
| **113** | SW | nw_sw_blog | **en,zh-Hant** | en | bilingual (EN then `---` then zh-Hant) | yes |
| 117 | SW | telegram | en | en | English | yes |
| 120 | SW | bluesky | en | en | English | yes |
| **121** | SW | bluesky | **zh-Hant** | en | Traditional Chinese | yes |
| **122** | SW | telegram | **zh-Hant** | en | Traditional Chinese | yes |
| 43 | Teaching | instagram | zh-Hant | zh-Hant | Traditional Chinese | yes |
| 58 | Teaching | facebook | zh-Hant | zh-Hant | Traditional Chinese | yes |
| 61 | Teaching | threads | zh-Hant | zh-Hant | Traditional Chinese (+contact+CTA) | yes |
| 100 | Teaching | tiktok | zh-Hant | zh-Hant | Traditional Chinese | yes |
| **107** | Teaching | twitter | **en** | zh-Hant | English only; Chinese output is refused and the account is **skipped** (not silently published) | explicit override is intentional; but see bug #7 |
| 114 | Teaching | teaching_blog | zh-Hant | zh-Hant | Traditional Chinese | yes |
| 125 | Teaching | bluesky | zh-Hant | zh-Hant | Traditional Chinese | yes |
| **108** | Money Systems Lab | youtube | **en** (DB) | en | English | yes as configured — **the live DB does have `audience_language="en"`**, contrary to the brief |
| 115 | Money Systems Lab | teaching_blog | en | en | English | yes |

**DB discrepancy to flag:** the brief says *"account 108 has NO
audience_language set"*. The live DB, and all four local backups I checked, show
`{"audience_language": "en"}` for account 108:

```
$ sqlite3 db/database.db "SELECT id, json_extract(config_json,'$.audience_language') FROM accounts WHERE id=108;"
108|en
$ for f in db/database.db.bak-20261008-222019 db/database.db.bak-subfix-20261009-005424 db/database.db.before-oldjob-cancel-20261007220904.bak; do \
    echo "$f"; sqlite3 "$f" "SELECT id, json_extract(config_json,'$.audience_language') FROM accounts WHERE id=108;"; done
... 108|en   (all three)
```

The brief is also slightly off on NW ("2 zh-Hant accounts: 119,124,127" lists
three). So the account-108 scenario is a **latent code path**, not the current
data state; §3 bug #1 simulates it and shows the failure.

**What account 107 actually gets.** `_account_audience_language` returns `en`
from the account config, `_build_generation_prompt` appends an English
`LANGUAGE REQUIREMENT`, and `message_matches_language(message, "en")` forbids
CJK. But the Teaching profile's `settings_json.systemPrompt` (DB, profile 4)
still says *"Use Traditional Chinese with Taiwan usage by default."* If the
model obeys the system prompt and answers in Chinese, `_generate_platform_draft`
raises and the account is **skipped** (`campaign_prep.py:156-163` /
`publish_orchestrator.py:537-544`), rather than publishing the wrong language.
So 107 never publishes Chinese — but it may frequently publish nothing. The
`/campaigns/prepare` path has no per-account try/except (§3 bug #8), so there it
fails the whole request instead.

---

## 3. BUGS

### BUG 1 — profile `default_language` column is ignored (latent blind-accept)

**Where:** `sau_backend.py:3678-3686` (`_account_audience_language`); the same
mistake is duplicated in the language resolution of
`myUtils/publish_orchestrator.py:519-527`, `myUtils/campaign_prep.py:133-141`,
and (account-only, no profile at all) `myUtils/worker.py:108-117`
(`_content_guard_error`).

```python
# sau_backend.py:3678
def _account_audience_language(account, profile) -> str:
    config = getattr(account, "config", None) or {}
    language = config.get("audience_language") or config.get("audienceLanguage")
    settings = getattr(profile, "settings", None) or {}
    if isinstance(settings, dict):
        language = language or settings.get("default_language") or settings.get("defaultLanguage")
    else:
        language = language or getattr(profile, "default_language", "")   # <- dead branch
    return str(language or "").strip()
```

`Profile.settings` is always a dict (`_row_to_profile`, `profiles.py:171`), so
the `else` is dead and `profiles.default_language` (a real column, set to
`zh-Hant` for Teaching) is never read. `settings_json` for all four profiles
contains **no** `default_language` key:

```
$ sqlite3 -header -column db/database.db "SELECT p.id, p.name, p.default_language, json_extract(p.settings_json,'$.default_language') AS s_default_lang FROM profiles p;"
1  NW                 en
3  SW                 en
4  Teaching           zh-Hant
10 Money Systems Lab  en
```

**Evidence (simulating the brief's account-108 case, i.e. blank account lang on
MSL whose column is `en`):**

```
$ .venv/bin/python - <<'PY'
from pathlib import Path
from myUtils import profiles as pr
import sau_backend
p = pr.get_profile(10, db_path=Path('db/database.db'))
a = pr.Account(id=999, profile_id=10, platform='youtube', account_name='blank-lang', cookie_path='', config={})
lang = sau_backend._account_audience_language(a, p)
print("MSL column:", p.default_language, "| blank account inherits ->", repr(lang))
sp, up = sau_backend._build_generation_prompt('youtube', p, type('G',(),{'name':'x'})(), {'_accountLanguage': lang}, {})
print("LANGUAGE REQUIREMENT present:", "LANGUAGE REQUIREMENT" in up)
PY
MSL column: en | blank account inherits -> ''
LANGUAGE REQUIREMENT present: False
```

A blank account therefore generates with no language constraint, and the
generation guard (`sau_backend.py:3668`) and the worker guard
(`worker.py:108-117`) both skip the check. Chinese or English copy is accepted
blindly. **This is a real bug even though no current account triggers it.**

**Fix:** in the dict branch, add the column as the final fallback:

```python
language = (
    language
    or settings.get("default_language")
    or settings.get("defaultLanguage")
    or getattr(profile, "default_language", "")
)
```

Apply the same precedence in `publish_orchestrator.py:519-527` and
`campaign_prep.py:133-141`, and make `worker._content_guard_error` load the
target account's profile and use `_account_audience_language` (or the same
resolver) instead of `account.config` only.

**Test:** create a profile with `default_language="zh-Hant"` and an account with
`config={}`; assert `_account_audience_language(account, profile) == "zh-Hant"`,
assert the built prompt contains the language requirement, and assert the worker
guard rejects an English message for that target.

---

### BUG 2 — `contains_simplified_chinese` misses common Simplified characters

**Where:** `myUtils/content_rules.py:149-156` (`_SIMPLIFIED_ONLY_CHARS`,
`contains_simplified_chinese`). The frozenset has 164 hand-picked characters
and is missing many unambiguous Simplified-only forms (`为`, `国`, `个`, `应`,
`头`, `实`, `读`, `话`, `请`, `谢`, `错`, `没`, `给`, `经`, `结`, `统`, `计`,
`划`, `图`, `馆`, `业`, `组`, `织`, `级`, `纪`, `约`, …).

**Evidence:**

```
$ .venv/bin/python - <<'PY'
from myUtils import content_rules as cr
for s in ["中国人", "为什么", "美国", "这个", "图书馆"]:
    print(f"{s!r:10} contains_simplified={cr.contains_simplified_chinese(s)!s:5} matches_zh-Hant={cr.message_matches_language(s,'zh-Hant')}")
PY
'中国人'    contains_simplified=False matches_zh-Hant=True
'为什么'    contains_simplified=False matches_zh-Hant=True
'美国'      contains_simplified=False matches_zh-Hant=True
'这个'      contains_simplified=True  matches_zh-Hant=False
'图书馆'    contains_simplified=True  matches_zh-Hant=False
```

`message_matches_language` is used by the generation guard
(`sau_backend.py:3668`), the worker guard (`worker.py:121-122`) and for
bilingual accounts, so all three accept `中国人` on a zh-Hant account.

**Historical-DB caveat:** I scanned `campaign_posts.draft_json` for zh-Hant
accounts and OpenCC-normalised them, but OpenCC also rewrites legitimate
Traditional variants (`床→牀`, `斗→鬥`, `托→託`, `污→汙`, `岩→巖`, `准→準`), so I
could **not** produce a clean, non-false-positive count of published leaks. The
code-level false negative above is unambiguous regardless.

**Fix:** OpenCC is already a declared dependency
(`pyproject.toml:21`, `requirements.txt:47`,
`opencc-python-reimplemented>=0.1.7`). Replace the curated set with a real
conversion check, e.g.:

```python
import opencc
_T2S = opencc.OpenCC("t2s")

def contains_simplified_chinese(text: str | None) -> bool:
    s = str(text or "")
    return bool(s) and _T2S.convert(s) != s
```

(For Taiwan output use `s2tw`/`tw2s` semantics deliberately and add tests for
the ambiguous variants so legitimate Traditional text is not rejected.)

**Test:** parametrise `contains_simplified_chinese` over at least
`中国人/为什么/美国/这个/图书馆` and assert `message_matches_language(..., "zh-Hant")`
is `False` for all of them; add a positive control of pure Traditional
(`台灣`, `為什麼`, `身體`) that must stay `True`.

---

### BUG 3 — trimming is code-point based; drops hashtags; splits words/graphemes; ignores X weighted CJK

**Where:** `myUtils/content_rules.py:267-274` (`trim_to_max_length`), used at
`content_rules.py:358` (`prepare_platform_draft`); the same logic in
`myUtils/prepared_publishers.py:81-96` (`_enforce_message_limit`).

```python
# content_rules.py:267
def trim_to_max_length(message: str, max_chars: int | None) -> str:
    text = (message or "").strip()
    if max_chars is None or len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1].rstrip() + "…"
```

Two concrete defects:

**(a) Appended hashtags are cut off, while the `hashtags` field still reports
three.** In `prepare_platform_draft`, hashtags are appended (`content_rules.py:341-342`) and
the whole string is trimmed (`content_rules.py:362`), with no re-validation:

```
$ .venv/bin/python - <<'PY'
from myUtils import content_rules as cr
d = cr.prepare_platform_draft("twitter", {"message": "x"*300, "hashtags": ["#alpha","#beta","#gamma"]})
print("len", len(d["message"]), "hashtags field", d["hashtags"])
print("tail", repr(d["message"][-40:]))
PY
len 280 hashtags field ['#alpha', '#beta', '#gamma']
tail 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx…'
```

The platform rule promised 3 hashtags (`PLATFORM_RULES["twitter"]`,
`content_rules.py:242`); the published tweet contains none. `validate_post`
(`content_generator.py:717`) checks the 3-hashtag rule but is not called on this
path. Same for bluesky (`content_rules.py:257`).

**(b) X counts CJK as weight 2, this code counts code points as 1.** A
`zh-Hant` tweet trimmed to 280 code points can be ~560 weighted and rejected by
the API. The local check is `len(text) <= limit` only. (The comment in
`platform_limits.py:24` even documents the URL-as-23 rule but not the CJK
weight.) The four zh-Hant twitter jobs I found in the DB with >140 CJK chars
were all delivered by the Sociamonials fallback
(`publish_job_targets.last_error` begins `delivered via Sociamonials X …`), so
the direct X path has not yet exercised this, but it is a latent failure.

**(c) `text[: max_chars - 1]` slices Python code points, so it can cut an emoji
ZWJ sequence or a combining mark in half, and it can cut a Latin word in the
middle.** No grapheme or word boundary is considered.

**Fix:** budget the body separately from the rule-required suffix
(hashtags/contact/CTA) so required affordances survive; trim on grapheme
clusters (the `regex` module `\X`, or `grapheme`); and for twitter use X's
weighted length (CJK = 2) instead of `len`. Re-run `validate_post` (or an
equivalent) after trimming so a dropped required hashtag is an error, not a
silent publish.

**Test:** long twitter draft must still contain all three hashtags and start with
the emoji; a 280-code-point CJK tweet must be detected as over the X weighted
limit; a message ending in a ZWJ emoji must not be split mid-cluster.

---

### BUG 4 — the "require emoji" check mistakes CJK for emoji

**Where:** `myUtils/content_rules.py:314-320` (`ensure_emoji_prefix`):

```python
if any(ord(char) > 10000 for char in stripped[:2]):
    return stripped
```

CJK code points are > 10000 (`這` U+9019), so a tweet starting with Chinese
never gets the required emoji. Evidence:

```
$ .venv/bin/python -c "from myUtils import content_rules as cr; print(repr(cr.ensure_emoji_prefix('這是測試')))"
'這是測試'
$ .venv/bin/python - <<'PY'
from myUtils import content_rules as cr
d = cr.prepare_platform_draft("twitter", {"message":"這是測試"*3, "hashtags":["#a","#b","#c"]})
print("starts_with:", d["message"][:3])
PY
starts_with: 這是測
```

Affects zh-Hant twitter account 124 and 103. The same `ord() > 10000` idea
appears in `content_generator.validate_post` (`content_generator.py:740-745`),
which excludes the CJK range but still misses many emoji.

**Fix:** test the actual Unicode emoji property (e.g. a compiled regex over
`Emoji_Presentation` / `Extended_Pictographic`) instead of a code-point
threshold.

**Test:** a twitter draft whose message starts with a CJK character must still be
prefixed with the profile emoji.

---

### BUG 5 — the generated Reddit title is ignored

**Where:** `myUtils/prepared_publishers.py:348-350` (`_message_title`) used by
`publish_reddit_sync` at `prepared_publishers.py:3460`:

```python
def _message_title(payload, *, fallback="Campaign post"):
    raw = _payload_message(payload) or fallback
    return raw.splitlines()[0].strip()[:100] or fallback
```

The generator asks the LLM for `"title"` and `"message"`
(`content_rules`/`sau_backend._build_generation_prompt`) and
`normalize_draft_fields` preserves `draft["title"]` (`content_rules.py:46`), and
`_build_payload` puts `draft` into the payload (`publish_orchestrator.py:773`).
But Reddit's publisher derives the title from the first line of the *body* and
truncates to 100 chars, silently dropping the generated title (and the 300-char
Reddit title limit from `platform_limits.REDDIT_TITLE_MAX_CHARS` is never used).
YouTube, by contrast, does read `draft.title` (`prepared_publishers.py:3730`).

**Fix:** `_message_title` should prefer `payload["draft"]["title"]` when present.

**Test:** a reddit payload with `draft.title` should submit that title, not the
first body line.

---

### BUG 6 — YouTube tag/title limits not enforced; constants are dead

**Where:** `myUtils/prepared_publishers.py:3768` uses `tags[:500]` — that caps
the *number of tags*, not the *total characters* (`YOUTUBE_TAGS_MAX_CHARS = 500`
is a character cap), and `title = raw_title[:100]` hardcodes the title limit
instead of using `YOUTUBE_TITLE_MAX_CHARS`. `grep` shows the four constants in
`platform_limits.py:44-48` are never imported anywhere:

```
$ grep -rn "YOUTUBE_TAGS_MAX_CHARS\|YOUTUBE_TITLE_MAX_CHARS\|REDDIT_TITLE_MAX_CHARS\|TELEGRAM_CAPTION_MAX_CHARS\|BLUESKY_BYTE_MAX" --include=*.py . | grep -v .claude/worktrees
./myUtils/platform_limits.py:44:TELEGRAM_CAPTION_MAX_CHARS = 1024
./myUtils/platform_limits.py:45:YOUTUBE_TITLE_MAX_CHARS = 100
./myUtils/platform_limits.py:46:YOUTUBE_TAGS_MAX_CHARS = 500
./myUtils/platform_limits.py:47:REDDIT_TITLE_MAX_CHARS = 300
./myUtils/platform_limits.py:48:BLUESKY_BYTE_MAX = 3000
```

**Fix:** enforce the character total (`sum(len(t)) + len(tags) - 1 <= 500`) and
use the constants. Same for the Reddit 300-char title and Bluesky 3000-byte cap
(the Bluesky publisher trims to 300 *code points*, not graphemes/bytes,
`prepared_publishers.py:4539-4540`).

**Test:** a draft with many/long tags must be trimmed so the comma-joined length
is ≤ 500; a Bluesky message with multi-byte graphemes must stay within 300
graphemes / 3000 bytes.

---

### BUG 7 — hashtag/emoji/CTA requirements are only real for 3 platforms; overrides skip them

**Where:** `content_rules.PLATFORM_RULES` (`content_rules.py:241-258`) only sets
`hashtag_count` for twitter and bluesky, `require_emoji` for twitter, and
`require_contact_details`/`require_cta` for threads. The per-platform prompt
builders (`content_generator.py:225-560`) *ask* for hashtags/CTA on
instagram/facebook/tiktok/reddit/etc., but nothing enforces them. Example:

```
$ .venv/bin/python -c "from myUtils import content_rules as cr; print(cr.prepare_platform_draft('instagram', {'message':'no hashtags here'})['hashtags'])"
[]
```

Worse, the normal Publish Center submit path passes `accountDrafts` and
`finalize_campaign` takes the override branch
(`campaign_prep.py:129-131`, `publish_orchestrator.py:505-513`), which runs only
`normalize_draft_fields` and **not** `prepare_platform_draft`. The preview did
apply the rules before the operator saw the draft, so an unedited override is
fine, but any edit can introduce a rule violation that neither the generator nor
the worker guard re-checks (the worker only checks usability + language,
`worker.py:95-122`).

**Fix:** centralise the per-platform rule application in a function that both
generation and the override path call (re-run `prepare_platform_draft` on the
override, or at least validate required counts/CTA). Add `hashtag_count` to the
platforms whose documented rules demand it, or explicitly document that only
twitter/bluesky are enforced.

---

### BUG 8 — `/campaigns/prepare` has no per-account error isolation

**Where:** `sau_backend.py:6187` sits inside the single route-wide
`try/except Exception` (`sau_backend.py:6090`, `:6258-6260`). Unlike
`publish_orchestrator.py:531-544` and `campaign_prep.py:152-163`, there is no
per-account `try`. One account whose generated copy fails the language guard
(e.g. the account-107 conflict) fails the **entire** prepare request with a 400
instead of skipping that account.

**Fix:** mirror the orchestrator's per-account fallback/skip handling.

---

### BUG 9 — worker last-ditch guard is incomplete by construction

**Where:** `myUtils/worker.py:95-122` (`_content_guard_error`). It checks
`is_usable_copy` and `message_matches_language`, using only
`account.config["audience_language"]`. It does **not** consult the profile
default (bug #1), does not check the platform max length, and does not check
required hashtags/emoji/CTA. So the "last-resort guard so a caption in the wrong
language never reaches a platform" cannot catch a Simplified character missed by
bug #2 or a blank-language account.

**Fix:** after fixing #1/#2, also run the platform rule check here.

---

### Minor / notes

- `is_usable_copy`'s refusal regex is English-only
  (`content_rules.py:116-122`), so a Chinese LLM refusal could pass.
- `SHEET_MESSAGE_MAX_CHARS` (`content_rules.py:196-201`) only covers
  facebook/instagram/twitter/tiktok; youtube/threads/reddit are sheet-exportable
  (`profiles.py:SHEET_EXPORT_PLATFORMS`) but get no cell-level trim.
- `_telegram_caption_chunks` hardcodes `1024` (`prepared_publishers.py:657`)
  instead of `platform_limits.TELEGRAM_CAPTION_MAX_CHARS`.
- **`myUtils/reddit_review.py` is Reddit OAuth request persistence, not
  title/flair rules.** The actual Reddit title/flair logic is
  `myUtils/prepared_publishers.py` (`_message_title:348`, `_reddit_flair_id:3246`,
  `_reddit_content_flair_label:3153`) and `myUtils/subreddits.py`
  (`title_pattern` at `:76`, `denial_reason` at `:341`).
  `scripts/reddit_flairs.py` only discovers and writes flair IDs into
  `config.flairIds`; it does not generate titles.

---

## 4. Test run and gaps

Command run exactly as requested:

```
$ .venv/bin/python -m pytest tests/ -q -k "content_rules or language or draft"
..........................................                            [100%]
42 passed, 1395 deselected, 3 subtests passed in 3.55s
```

(`python` is not on `PATH`; the repo venv interpreter is
`.venv/bin/python`.)

**What is covered:** `test_content_rules.py` (twitter emoji/3-hashtags on a
*short* message, threads contact+CTA+500-trim, tiktok limit, normalization,
usable-copy, `message_matches_language` for zh/en/bilingual), and
`test_content_generator.py::TestLanguageRouting`, and
`test_publish_center.py::test_preview_generates_account_specific_language_drafts`
(proves per-account dispatch), and
`test_campaigns_http.py::test_campaign_prepare_builds_drafts_per_account_language`.

**Gaps directly tied to the bugs above:**

1. No test that `_account_audience_language` falls back to the
   `profile.default_language` **column** (bug #1). The existing
   `test_campaign_prepare_builds_drafts_per_account_language` puts
   `audienceLanguage` on *both* accounts, so it never exercises the fallback.
2. No test for `worker._content_guard_error` at all, and in particular none with
   a profile-level language.
3. `contains_simplified_chinese` is tested only with `网络视频质量` (caught) and
   `網路影片品質` (clean); no test for the missed forms (`中国人`, `为什么`,
   `美国`) (bug #2).
4. `test_prepare_twitter_draft_enforces_emoji_and_three_hashtags` uses a short
   message, so it never exercises trim-after-append (bug #3a); no test for CJK
   weight, grapheme splitting, or emoji-vs-CJK (bug #4).
5. No test for Reddit title selection (bug #5) or YouTube tag/title limits
   (bug #6).
6. No test that a single account's generation failure is isolated in
   `/campaigns/prepare` (bug #8).

---

## 5. UNKNOWNS / not proven here

- **Historical Simplified leaks in the live DB:** OpenCC's `s2t`/`s2tw` configs
  rewrite legitimate Traditional variants (`床→牀`, `斗→鬥`, `托→託`, `污→汙`,
  `岩→巖`, `准→準`), so my DB scans could not separate a genuine Simplified leak
  from a variant normalisation without false positives. The deterministic
  unit-level false negative (`中国人`/`为什么`/`美国`) is the solid evidence;
  the *observed* leak count is unknown. A clean follow-up would compare against a
  full Simplified→Traditional mapping restricted to non-ambiguous pairs.
- **`last_published_job_id` is not a publish flag.** `_enqueue_post`
  (`publish_orchestrator.py:804`) sets it at queue time, so counting "published"
  rows with it is misleading. Any future leak scan must join
  `publish_job_targets.status`.
- **Semantics of a pass-through `message_matches_language` for `zh`/`zh-Hans`:**
  a Simplified (`zh`/`zh-Hans`) account is treated as "any CJK is fine"
  (`content_rules.py:188-190`), so Traditional copy is accepted on a Simplified
  account. That may or may not be intended; the current accounts only use
  `zh-Hant`, so it is untested in practice.
- **X weighted-length rule and current API behaviour:** I did not call the X API.
  The claim that CJK counts ×2 is standard `twitter-text` behaviour; the fix
  should be verified against X's current docs. The only long-CJK zh tweets in
  the DB were delivered through the Sociamonials fallback, so the direct path is
  untested.
- **Whether `content_generator.build_generation_context` (the older
  `/content/*`/prepared-posts route, `sau_backend.py:9684`) is still reachable**
  in production. It carries the same per-language logic but a different prompt
  builder than `sau_backend._build_generation_prompt`; I audited the latter as
  the live path. There are 0 rows in `prepared_posts`, which suggests the route
  is dormant.
- **Landing of edits:** per the task, no code was changed; all fixes above are
  proposed only.
