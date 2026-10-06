# Stonewall recovery + failed-post re-publish (2026-10-06)

## What was wrong

| Symptom | Root cause | Fix |
| --- | --- | --- |
| YouTube "SFW Taipei Stonewall" uploaded but invisible | API upload defaulted to `private`; account config had no `privacyStatus` and nothing verified the result | set accounts 110/108 `privacyStatus=public`; new `_verify_youtube_visibility()` reads the status back after upload and fails loudly |
| Instagram NW never published (containers errored `2207053`) | the raw **602 MB HEVC / 813 s** original went on the wire (pre-publish re-encode failed; old best-effort shrink returned the original) | `sociamonials_fallback` prefers the **local prepared** file and enforces the per-network size cap; prepared artifacts now use the 206 MB H.264 `_pub.mp4` |
| Threads Stonewall failed (`duration 813s > 300s`) | media prep enforced dimensions/fps/size but **not duration** | `media_prep.trim_to_seconds()` + per-platform duration variants (`_pub_<sec>s.mp4`); `_artifact_payloads_for_platform` picks the variant that fits (Threads 300 s, X 140 s) while IG/YouTube keep the full cut |
| YouTube visibility verified | | `eG-NzyGR934` → `public` / `processed` (PT13M34S) |
| Non-X permanent failures never reached the fallback | `only_platform="twitter"` | `worker._handle_failure` now attempts the fallback for **every mapped platform** |
| Facebook `HTTP 400 Unable to fetch video file from URL` | stored URL with a literal space | `_extract_media` percent-encodes stored URLs |

## Re-published / verified

Direct publishes (no fallback) after the fixes:

- Instagram NW — https://www.instagram.com/reel/DeIhHd-j-V8/ (target 4513, 206 MB `_pub.mp4`)
- Threads NW — https://www.threads.com/@nakedwill8/post/DeIfoS9lOxb (target 4517, 295 s cut)
- Threads SW — https://www.threads.com/@sexualwill8/post/DeIgLxjjKYI (target 4530)
- YouTube — `eG-NzyGR934`, public/processed
- X (account 103) — target 2522, translated to zh-Hant
- Threads duration failures 1919, 1930, 2817, 3374 — re-cut to ≤300 s, published
- Facebook 2828 — published after the URL-encode fix

Language-guard repairs: 4513 (dropped the trailing Chinese tagline from the English copy), 2029 + 2522 (translated English copy → Taiwan Traditional Chinese, scheduled).

## Remaining queue

- `2029` (X, account 124) — scheduled 2026-10-07 00:59 UTC.
- `3785` (Threads, account 42) — scheduled 2026-10-07 01:26 UTC.
- Reddit 74 / 3865 / 4527 and X 1994 / 2241 (X fallback) — scheduled 2026-10-06 05:11–05:51 UTC.
- The remaining **~236 failed targets are permanent**: generated artifacts genuinely gone (`MediaRestoreError`), accounts needing operator reconnection, deterministic refusals (TikTok app-in-development / domain-ownership, `photo posts require Direct Post`), or duplicate posts.
- `3387` (Facebook, account 11) still carries placeholder copy (`publish-center-…`); needs regenerated copy, not a media fix.

## Status counts (after work)

pending 3637 · succeeded 939 · cancelled 350 · failed 236
