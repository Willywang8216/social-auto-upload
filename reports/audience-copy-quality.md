# Audience Copy Quality: the "[content-guard] placeholder/generic copy" failures

Date: 2026-10-10 · Repo: `/home/will/social-auto-upload` · Scope: content
generation + the pre-publish guard. No job was scheduled, published, or
cancelled while producing this report.

---

## 1. The reproduced failure, with the real text

The guard is `_content_guard_error()` in `myUtils/worker.py:95`, which calls
`content_rules.is_usable_copy()` (`myUtils/content_rules.py:163`) and
`content_rules.message_matches_language()` (`myUtils/content_rules.py:203`).

Nine failed targets carried the exact error `[content-guard] placeholder/generic
copy`:

| target | platform | account | campaign | draft message (as stored) |
| --- | --- | --- | --- | --- |
| 92 | twitter | account:107 | 78 | `✨ publish-center-20260709-170824\n\nI Let AI Audit a $5,000/Month Budget — Here's What It Found #socialmedia #content #campaign` |
| 94 | twitter | account:107 | 79 | `✨ publish-center-20260709-170828\n\nAI found 6 money leaks in a $5,000/month budget — and the family had no idea. #socialmedia #content #campaign` |
| 96 | twitter | account:107 | 80 | `✨ publish-center-20260709-190442\n\nI Let AI Audit a $5,000/Month Budget — Here's What It Found #socialmedia #content #campaign` |
| 97 | twitter | account:107 | 81 | `✨ publish-center-20260709-190447\n\nAI found 6 money leaks in a $5,000/month budget — and the family had no idea. #socialmedia #content #campaign` |
| 5225 | bluesky | account:120 | 2500 | `1T — adult, honest, 18+ only.` |
| 5226 | bluesky | account:121 | 2500 | `1T — adult, honest, 18+ only.` |
| 5227 | telegram | account:122 | 2500 | `1T — adult, honest, 18+ only.` |
| 5228 | telegram | account:117 | 2500 | `1T — adult, honest, 18+ only.` |
| 5229 | twitter | account:77 | 2500 | `1T — adult, honest, 18+ only.` |

There were two extra, different guard failures in the same population:

| target | platform | account | error | draft message |
| --- | --- | --- | --- | --- |
| 2528 | telegram | account:117 (`en`) | `copy does not match account language 'en'` | `18+ 成人限定。這裡分享真實、坦誠的性探索與性正向觀點；…` |
| 5218 | bluesky | account:119 (`zh-Hant`) | `copy does not match account language 'zh-Hant'` | `SFW 20260813074703243 — natural, honest, everyday naturist life.` |
| 5236 | telegram | account:117 (`en`) | `copy does not match account language 'en'` | `性慾，不必羞恥。\n\n我是 Will。Sexualwill 不只是品牌…` |

So the guard saw two distinct defects:
1. **a machine label** (`publish-center-<stamp>`, or `SFW <stamp> —`) prefixed to
   the caption, and
2. **the operator's brief echoed as the caption** (`1T — adult, honest, 18+ only.`),
   plus genuinely wrong-language copy on a mono-language account.

Re-checking the nine with the unmodified guard:

```
target 92 platform twitter account account:107
  raw      : "✨ publish-center-20260709-170824\n\nI Let AI Audit a $5,000/Month Budget — Here's What It Found #socialmedia #content #campaign"
  guard    : placeholder/generic copy
target 5225 platform bluesky account account:120
  raw      : '1T — adult, honest, 18+ only.'
  guard    : placeholder/generic copy
```

## 2. Judgement: the guard is right; the generator was wrong

**The guard is correct and should not be weakened.**

* `publish-center-20260709-170824` is the media-group name, not caption text.
  Publishing a caption that opens with a machine name is exactly the visible
  defect the guard exists to prevent.
* `1T — adult, honest, 18+ only.` is the operator's *instruction* to the LLM
  (the brief that `publish_orchestrator._request_data_for_options()` puts in
  `request_data["notes"]`). It is not copy.
* The language mismatches are real: a zh-Hant caption on an `en` account, and
  English copy on a `zh-Hant` account.

The **generator** was wrong in three ways:

1. **Old fallback prepended the media-group name.** `_fallback_generated_draft`
   (in the pre-`8133d53` form) joined `title or media_group.name` with the
   transcript/notes and fell back to `media_group.name`. That is how the four
   July twitter payloads got the `✨ publish-center-…` prefix even though the
   body copy underneath was fine.
2. **The fallback still echoed the brief.** As of commit `8133d53` the
   media-group name was removed from the candidates, but
   `request_data["notes"]` (the brief) was still used. Reproduced on the
   current code before this change:

   ```
   BRIEF: SFW invitation to this year's Taipei Pride (2026-1
     -> FALLBACK PUBLISHED: "SFW invitation to this year's Taipei Pride (2026-10-31). A warm, inviting clip…"
   BRIEF: NSFW invitation to this year's Taipei Pride (2026-
     -> FALLBACK PUBLISHED: "NSFW invitation to this year's Taipei Pride (2026-10-31). Adult, body-positive…"
   BRIEF: 1T — adult, honest, 18+ only.
     -> RAISED: RuntimeError Cannot generate copy: the LLM is unavailable …
   ```

   When the Muyuan gateway was down (Cloudflare 403), every account with an
   `audience_language` already failed loudly, but an account *without* a
   language requirement would silently queue the brief. `is_usable_copy()`
   cannot tell "SFW invitation to …" (a brief) from a caption, so the guard
   accepted it and the placeholder went out. Only the one brief that happened to
   match `_GENERIC_COPY_RE` was caught.
3. **No post-processing removed a prompt echo.** The LLM prompt still contains
   `Media group: <name>` (`_build_generation_prompt`, `sau_backend.py`), so the
   model could repeat it; nothing stripped it back out.

The intended fix is therefore at generation time, not in the guard: never feed
the generator's *instructions* to the audience, and never let a machine label
travel with real copy.

## 3. What changed

| file:line | change |
| --- | --- |
| `myUtils/content_rules.py:134` | New `strip_machine_label_lines()`: drops lines that are nothing but a `publish-center-<stamp>` name, a bare media filename, or a screenshot tag. Real copy on other lines survives; a message made only of labels collapses to `""` and is still rejected. |
| `myUtils/content_rules.py:90` | `normalize_draft_fields()` now runs the strip after the `Title:/Description:` handling, so every generation path (and `regenerate_bad_drafts.py`, which calls the same generator) is cleaned. |
| `myUtils/content_rules.py:362` | `prepare_platform_draft()` returns an empty message when there is no real copy after normalisation, instead of fabricating a caption from the required emoji + hashtags (`✨ #socialmedia #content #campaign`). Requirement checks for contact/CTA still run first. |
| `sau_backend.py:3471` | `_fallback_generated_draft()` no longer considers `request_data["notes"]` (the operator brief). It uses only the transcript or an explicit `title`; otherwise it raises, so the failure surfaces at prepare time instead of queueing a placeholder that can never pass the guard. |
| `sau_backend.py:6223` | The campaign prepare path now writes the LLM's `altText` into the sheet row (`draft.get("altText")`), falling back to the operator option. Previously the generated alt text was discarded and `AltText` shipped empty for every post. |

The Cloudflare header set in `myUtils/llm_client.py:68` (`anthropic-version`,
`anthropic-beta`, `user-agent: claude-cli/...`, `x-app`) is untouched.

Verification on the old payloads (guard unchanged):

```
target 92
  raw      : "✨ publish-center-20260709-170824\n\nI Let AI Audit a $5,000/Month Budget — Here's What It Found #socialmedia #content #campaign"
  guard    : placeholder/generic copy
  normalized: "I Let AI Audit a $5,000/Month Budget — Here's What It Found #socialmedia #content #campaign"
  usable   : True
target 5225
  raw      : '1T — adult, honest, 18+ only.'
  guard    : placeholder/generic copy
  normalized: '1T — adult, honest, 18+ only.'
  usable   : False
```

i.e. the label+real-copy failures are cleaned (their real copy is kept and now
passes), while a caption that is *only* the generic brief is still refused.

## 4. The six scheduled campaigns

Copy below was generated by the live pipeline
(`sau_backend._generate_account_draft`, model `qwen3.8-27b` via
`SAU_LLM_POOL`) with the fixes applied. Read-only probe; nothing was written back
to the DB. `guard` runs the same `_content_guard_error` predicates plus the
platform char cap.

### "Before" — what the LLM-down fallback would have shipped

If the gateway 403'd, the old fallback published each campaign's brief verbatim:

* 2598/2599: `SFW invitation to this year's Taipei Pride (2026-10-31). A warm, inviting clip for the LGBTQ+ community; body-positive, naturist-friendly, respectful. Invite people to show up for Taipei Pride.`
* 2600/2601: `NSFW invitation to this year's Taipei Pride (2026-10-31). Adult, body-positive, naturist-friendly invitation to the LGBTQ+ community. Invite people to show up for Taipei Pride.`
* 2602/2603: `Time-sensitive NSFW news update for this week. Adult, body-positive, naturist/sex-positive tone.`

With the fix, a 403 now raises
`Cannot generate copy: … The operator brief is an instruction, not a caption,
and is never published.` instead.

### "After" — real copy, all `guard=PASS`

**2598 — SFW pride invitation, profile `nw` (scheduled from 2026-10-24 12:00)**

* `NW Bluesky EN` (en), 210 chars:
  `Taipei Pride, Oct 31, 2026. I'll be there — dressed or not, that was never the point. Showing up is. If you've ever felt out of place in your own skin, come stand with us. #TaipeiPride #Naturism #BodyPositivity`
  title: `See You at Taipei Pride 2026`
* `NW Bluesky ZH` (zh-Hant), 106 chars:
  `10月31日，台北同志遊行見。我以天體愛好者的身分走上街，不是為了裸露，而是想說：每個身體都值得被善待，不管你穿不穿。歡迎和我們一起走，把身體的自由過成日常。 #台北同志遊行 #TaipeiPride #天體主義`
  title: `一起走上台北同志遊行`

**2599 — SFW pride invitation, profile `sw` (scheduled from 2026-10-24 12:00)**

* `SW Bluesky EN` (en), 281 chars:
  `Taipei Pride, Oct 31 2026. Come as you are: every body, every gender, every kind of love. Wear what feels good on you. Bring water, sunscreen, and that friend who's never been. March, dance, hold hands. The street belongs to all of us. See you there. #TaipeiPride #LGBTQ #Pride2026`
  title: `Taipei Pride 2026 — Come As You Are`
* `SW Bluesky ZH` (zh-Hant), 116 chars:
  `10月31日，台北街頭見。… #TaipeiPride #台北同志遊行 #性慾不必羞恥`
  title: `10月31日，台北街頭見`

**2600 — NSFW pride invitation, profile `nw` (scheduled from 2026-10-24 14:10)**

* `NW Bluesky EN` (en), 228 chars:
  `Taipei Pride, Oct 31, 2026. I'll be there — the naked, queer, body-positive kind of proud. Naturist hearts welcome. Come as you are, however much skin that means. Adult space, real respect. #TaipeiPride #Naturism #BodyPositivity`
* `NW Bluesky ZH` (zh-Hant), 141 chars:
  `10月31日，台北同志遊行見。裸體對我來說從來不是表演，是誠實。… #台北同志遊行 #裸體即真實 #Naturism`
  title: `2026 台北同志遊行，用最真實的樣子與你相見`

**2601 — NSFW pride invitation, profile `sw` (scheduled from 2026-10-24 14:00)**

* `SW Bluesky EN` (en), 224 chars:
  `Taipei Pride 2026, Oct 31. I'll be there: body-positive, naturist-friendly, proudly queer. Come as you are, dress as loud or as light as you like — and bring consent along with the glitter. 🌈 #TaipeiPride #SexPositive #LGBTQ`
* `SW Bluesky ZH` (zh-Hant), 163 chars:
  `2026年10月31日，台北同志大遊行見。… 天體友善、身體友善，成年限定。… #台北同志遊行 #性慾不必羞恥 #身體自主`

**2602 — time-sensitive NSFW news, profile `sw` (scheduled from 2026-10-12 12:00)**

* `SW Bluesky EN` (en), 226 chars:
  `NSFW news, weekly edition: platform nudity rules keep shifting, payment processors keep deciding what desire is allowed, and naturist spaces keep getting fuller. Shame loses when we talk. #SexPositivity #BodyPositive #Naturism`
  title: `This Week in Body Freedom: Rules Shift, Shame Doesn't Win`
* `SW Bluesky ZH` (zh-Hant), 116 chars:
  `這週成人圈的話題還是繞著審查打轉：平台的規則越收越緊，靠身體和慾望吃飯的創作者往往第一個被掃到。… #身體自主 #性別平權 #性教育`
  title: `這週的成人新聞，我還是想講兩句`

**2603 — time-sensitive NSFW news, profile `sw` (scheduled from 2026-10-12 13:50)**

* `SW Bluesky EN` (en), 273 chars:
  `Watching this week's adult news cycle: more creators talking out loud about nudity, bodies, and consent instead of hiding it. That honesty is the point. Naturism isn't a scandal — it's just people. Stay curious, stay clothed or don't. #SexPositive #Naturism #BodyPositivity`
* `SW Bluesky ZH` (zh-Hant), 116 chars:
  `這個星期成人圈最熱的話題，是幾個平台又收緊了裸露與性教育的審核標準。… #性正向 #身體自主 #天體主義`

All 90 pending targets already queued for these six campaigns also pass the
guard as stored (`2598:15, 2599:13, 2600:10, 2601:10, 2602:14, 2603:28`
→ 0 failures).

### Language handling

`message_matches_language()` is enforced twice: at generation
(`sau_backend._generate_platform_draft`) and again at publish
(`worker._content_guard_error`). Each account is checked against its own
`audience_language` (`accounts.config_json`), not the profile default:

| account | language | fresh copy | result |
| --- | --- | --- | --- |
| 118 NW Bluesky EN | en | English | PASS |
| 119 NW Bluesky ZH | zh-Hant | Traditional Chinese | PASS |
| 120 SW Bluesky EN | en | English | PASS |
| 121 SW Bluesky ZH | zh-Hant | Traditional Chinese | PASS |

The `en,zh-Hant` bilingual SW Blog account (113) is still satisfied by either
script, and a zh-Hant account is still rejected if the model answers in
Simplified Chinese (`contains_simplified_chinese`). Platform caps are the
enforced `myUtils/platform_limits.py` values via `PlatformRule`; every sample
above is under its cap (Bluesky 300, Twitter 280).

## 5. Tests

Added / updated:

* `tests/test_content_rules.py::MachineLabelStrippingTests` — 5 tests: label +
  real copy is cleaned and kept; label-only collapses to empty; a bare filename
  line is stripped; `prepare_platform_draft` never fabricates a caption from
  emoji + hashtags; a label + real copy survives `prepare_platform_draft`.
* `tests/test_content_generator.py::TestFallbackGeneratedDraft` — 4 tests: the
  operator brief is never published; an explicit title and a transcript are
  still allowed; account-language copy requires an LLM.
* `tests/test_campaigns_http.py` — existing
  `test_campaign_prepare_missing_expected_media_context_is_not_an_error` updated
  to supply an explicit title (the old version encoded the brief-as-caption
  bug), plus a new
  `test_campaign_prepare_refuses_to_publish_the_operator_brief`.

Full suite:

```
$ .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1554 passed, 1 skipped, 139 subtests passed in 128.23s (0:02:08)
```

(The stated baseline was 1526 passed, 1 skipped; the tree also contains
uncommitted work from another session in `myUtils/worker.py` /
`tests/test_worker_media_restore.py` — media-path aliasing, unrelated to copy —
which this session did not touch.)

## 6. Unknowns / follow-ups

* **The nine already-failed targets still need regeneration.** The guard is
  unchanged (correctly), so the stored placeholder payloads remain failed until
  `scripts/regenerate_bad_drafts.py --apply` re-runs. This session deliberately
  did not run it (it would write to the DB) and did not schedule/publish
  anything. The regenerate path uses the fixed generator, so re-running is now
  safe and will either produce real copy or fail loudly.
* **`SFW <stamp> —` labels are not stripped.** `_MACHINE_LABEL_LINE_RE` covers
  `publish-center-<stamp>` and bare filenames, not the older `SFW 20260813074703243
  —` shape seen on target 5218. That payload is also a language mismatch, so it
  needs regeneration anyway; no live campaign uses that naming now.
* **Transcription is still blocked.** `/v1/audio/transcriptions` is Cloudflare-403
  on every route/method, so the fallback's transcript branch is only exercised
  when a transcript already exists (e.g. from a prior successful run or an
  imported caption). Best-effort by design; unchanged.
* **`altText` wiring beyond the sheet.** The campaign prepare path now keeps the
  LLM alt text in the sheet row. Publisher-side use of `draft["altText"]`
  (Instagram/Bluesky image alt attributes) was out of scope here; the value is
  persisted in `draft_json` and available if that wiring is wanted later.
* **Model quality is non-deterministic.** The fresh samples pass, but the
  gateway can rotate to `grok-4.7`/`deepseek` endpoints; a future bad response
  that is a plausible-looking but generic caption cannot be caught by a regex.
  The brief is now structurally impossible to publish through the fallback, and
  a pure label is refused, which removes the deterministic placeholder sources.
