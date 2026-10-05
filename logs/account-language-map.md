# Account language map (source of truth: `accounts.config_json.audience_language`)

Every account's content language is the `audienceLanguage` field the app shows
(`en` or `zh-Hant`). Traditional Chinese (Taiwan) is **`zh-Hant`** — a bare `zh`
is read as *Simplified* and must not be used. Account 124 was the only bare `zh`
and has been corrected to `zh-Hant`.

## Profile 1 — NW (Nakedwill), default `en`

| Account | Name | Language |
|---|---|---|
| 11 | Nakedwill (Facebook) | en |
| 62 | NWthreads | en |
| 72 | NW_IG | en |
| 105 | Nakedwill Reddit | en |
| 109 | Nakedwill_TK | en |
| 110 | Itswill_YT | en |
| 112 | NW Blog | en |
| 116 | NW TG 本人 | en |
| 118 | NW Bluesky EN | en |
| 123 | NW X (model_will) | en |
| 119 | NW Bluesky ZH | **zh-Hant** |
| 124 | NW X (nudeweiwei) | **zh-Hant** (was `zh`/Simplified; fixed) |

## Profile 3 — SW (Sexualwill), default `en`

| Account | Name | Language |
|---|---|---|
| 42 | SW_threads | en |
| 64 | SW-FB | en |
| 75 | SW_IG | en |
| 77 | sexualwill (X) | en |
| 106 | Sexualwill Reddit OAuth | en |
| 117 | SW TG 本人 | en |
| 120 | SW Bluesky EN | en |
| 121 | SW Bluesky ZH | **zh-Hant** |
| 122 | SW TG 中文 | **zh-Hant** |
| 103 | 光光 (X) | **zh-Hant** |
| 113 | SW Blog | **bilingual** `en,zh-Hant` |

## Profile 4 — Teaching, default `zh-Hant`

| Account | Name | Language |
|---|---|---|
| 43 | Teaching_IG | **zh-Hant** |
| 58 | teaching-fb | **zh-Hant** |
| 61 | teaching-threads | **zh-Hant** |
| 100 | Teaching_TK | **zh-Hant** |
| 114 | Teaching Blog | **zh-Hant** |
| 125 | Teaching_BSKY | **zh-Hant** |
| 107 | Willy Dev tutor (X) | en (override) |

## Profile 10 — Money Systems Lab, default `en`

| Account | Name | Language |
|---|---|---|
| 108 | Willy Dev tutor (YouTube) | en |
| 115 | MSL Blog | en |

## Telegram channels / groups (verified live via `/api/telegram/available-targets`)

| Account | Language | Channel | Group |
|---|---|---|---|
| 116 NW TG 本人 | en | `@nakedwilltgchannel` (Nakedwill — Naturism & Nudism (EN)) | `@nakedwill` (Nakedwill Chat (EN) 🌿) |
| 117 SW TG 本人 | en | `@sexualwilltgchannel` (Sexualwill 🔞 EN (18+)) | `@sexualwill` (Sexualwill Chat 🔞 EN (18+)) |
| 122 SW TG 中文 | zh-Hant | `@nakedsexualwei` (光光 nakedhappylife 🔞 中文) | `@nakedsexualweiwei` (光光討論群 🔞 中文) |
| **127 NW TG 中文** (new) | **zh-Hant** | `@nakedwillzh` (Nakedwill 中文 🌿) | `@nakedwillzhchat` (Nakedwill 中文討論群 🌿) |

The NW Chinese channel/group (`@nakedwillzh` / `@nakedwillzhchat`) was created on
2026-10-06 from the operator's Telegram session; account **127** publishes to it.
New campaigns for profile 1 now include it automatically, and 661 already-queued
NW campaigns were backfilled to it from the zh-Hant siblings (119/124) with
`scripts/backfill_account_targets.py`.

There is **no NW (nakedwill) Chinese Telegram channel/group** in the session's
dialog list (only EN). NW Chinese is covered by Bluesky 119 and X 124.

## Rules enforced in code

- `content_rules.message_matches_language`: a `zh*` target must contain CJK and
  must **not** contain any Simplified-only character; a non-zh target must not
  contain CJK.
- `worker._content_guard_error` refuses to publish a placeholder or a
  wrong-language draft; `sau_backend._generate_platform_draft` refuses to queue
  one.
- The generation prompt carries HUMANIZER + TAIWAN MANDARIN rules, and the
  humanizer pass normalises zh-Hant output through OpenCC `s2twp`.

## Open question

Accounts **116 / 117 ("… TG 本人")** are `en` and their recent published copy
is English, but older queued copy was Chinese. If those personal Telegram
channels should be Chinese, set `audienceLanguage = zh-Hant` on them and re-run
`scripts/humanize_drafts.py` / `scripts/regenerate_bad_drafts.py`.
