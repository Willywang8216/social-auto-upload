# Subreddits for gay-nudist content — deep research

Date: 2026-10-14 · engines: Exa + Tavily (16 queries → 206 candidate subreddits)
· cross-referenced against the live bundle.social channels (52 of our account's),
`post-requirements` (flair), and `myUtils/subreddits.py` (our own fit registry).

Method scripts (kept for re-runs):
`logs/reprep/reddit_subreddit_research.py`, `reddit_subreddit_validate.py`,
`reddit_subreddit_profiles.py`.
Raw data: `logs/reprep/reddit-subreddit-research.json`,
`reddit-subreddit-validation.json`.

## Tier 1 — genuine gay-nudist fit (the answer)

| subreddit | connected | video | image | gallery | flair | what it is / key rule |
| --- | --- | --- | --- | --- | --- | --- |
| **r/GayPlusNudists** | ✅ | ✗ | ✅ | ✅ | no | *"Live Life. Naked."* For **all non-straight** people who like being naked; club is mainly on Discord. **Best single fit.** |
| **r/NudistMen** | ✅ | ✗ | ✅ | ✅ | **required** | *"NO DICK PICS, NO BULLS\*\*T … this sub is not about your penis."* Rules: no asshole/erection shots, no headless photos. 55k. |
| **r/nakedinnature** | ✅ | ✗ | ✅ | ✅ | no | *"beauties of nature … total or partial nudity. WITHOUT THE PORN. No flashing or dick pics."* |

## Tier 2 — naturism / nudism (SFW, large, strict on promo)

| subreddit | connected | video | image | gallery | flair | note |
| --- | --- | --- | --- | --- | --- | --- |
| r/nudism | ✅ | ✗ | ✅ | ✅ | required | general nudism discussion |
| r/naturism | ✅ | ✗ | ✅ | ✅ | required | philosophy/community |
| **r/anarcho_naturism** | ✅ | **✅** | ✅ | ✅ | required | **native video allowed** |
| **r/body_freedom** | ✅ | **✅** | ✅ | ✅ | required | ethical naturism/feminism; 1.9k |
| **r/ryanbossom** | ✅ | **✅** | ✅ | ✗ | no | the account's own sub — safest sandbox |
| r/NudeBeach | ✅ | ✗ | ✅ | ✅ | required | *"No porn, exhibitionists, swingers, or OnlyFans advertising."* |
| r/allNudist | ✅ | ✗ | ✅ | ✅ | required | 48k; **1 post/day max** |
| r/TrueYou | ✅ | ✗ | ✅ | ✅ | no | *"nudist/naturist environment (not explicit)"*; 14k |
| r/homenudist / r/youngNudists / r/nudistmenmeetup / r/skinnydipping / r/nakedbutnotafraid | not a channel | — | — | — | flair | can still be posted to via `sr`, but not in the connected set |

## Tier 3 — gay gone-wild / explicit (best for the **Sexualwill** brand)

Connected channels: `r/gaybrosgonewild` (709k), `r/GaybrosGoneWild`, `r/gay`
(425k, **video**), `r/gayporndaily`, `r/GayPorn_NSFW`, `r/gayporn`, `r/manass`,
`r/PublicBoys`, `r/Gayexhibitionist`, `r/TwinkPits`, `r/boypussy`,
`r/NakedAdventures`, `r/NakedYoga`, `r/twinks`, `r/gaysian`, `r/gaysiansgonewild`,
`r/Gay_Porn_video`. These tolerate creator content (some require verification /
account-age gates) but are explicit-only.

## Blocked by our own registry (a monetised brand is refused)

`r/gaybros` (account **banned**), `r/nudists`, `r/lgbt`, `r/gaymen`,
`r/askgaybros`, `r/softies`, `r/gaybody` (account **banned**).
Do not add these — every submit is a wasted attempt and another strike.

## The rule that decides everything

The SFW nudism/naturism subs above (Tier 1–2) **explicitly ban porn, OnlyFans
and self-promotion**. The NW/SW accounts promote a **paid** adult brand, so:

* Tasteful, non-promotional, non-explicit nudity → fine for Tier 1–2.
* Anything explicit or with a paid-link/watermark → will be pulled, and repeat
  posting is what got `r/gaybros` and `r/gaybody` to ban the account.

So: **Tier 1–2 for Nakedwill (naturist), Tier 3 for Sexualwill (explicit)** —
and keep promotional links out of the Tier 1–2 posts.

## Recommended target lists

**Nakedwill (NW / naturism)** — nudist-first, video only where allowed:
```
NudistMen, GayPlusNudists, nakedinnature, nudism, naturism,
TrueYou, allNudist, NudeBeach, anarcho_naturism, body_freedom, ryanbossom
```

**Sexualwill (SW / sex-positive)**:
```
GayPlusNudists, gaybrosgonewild, GaybrosGoneWild, gayporndaily,
GayPorn_NSFW, manass, PublicBoys, Gayexhibitionist, TwinkPits, boypussy,
NakedAdventures, NakedYoga, twinks, gaysian, gaysiansgonewild
```

Video: only `r/gay`, `r/anarcho_naturism`, `r/body_freedom`, `r/ryanbossom` accept
**native video** among the connected channels. Everything else is **image or
gallery** — so a 1-frame image or a gallery has to be generated from the video
for the rest.

## Blocker: the `ryanbossom` account is restricted at Reddit

Live tests on 2026-10-14 (via bundle.social, `/api/v1/organization/usage/posts`
4/20 used):

| attempt | Reddit's answer |
| --- | --- |
| image → r/NudistMen | `RD:403` "Could not upload media" |
| image → r/ryanbossom | `RD:403` |
| text → r/ryanbossom | `RD:SUBREDDIT_NOTALLOWED` "trusted members only" |
| text → **u/ryanbossom (own profile)** | `RD:SUBREDDIT_NOTALLOWED` |

Failing on the account's **own profile** + the direct path's `401` = the account
is suspended/restricted at Reddit (it worked on 10-12). No code change can fix
that. See `reports/reddit-bundle-social.md`.
