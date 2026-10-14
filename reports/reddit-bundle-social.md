# Reddit publishing via bundle.social

Date: 2026-10-14 · Account: `u/ryanbossom` ("Nakedwill") · Team `2b97be09-…`

## Why bundle.social

Reddit revoked the API credentials for both configured accounts, so the direct
path is a guaranteed `401` ("Please log in to do that."). bundle.social holds its
**own** Reddit OAuth app for the same account, so it is now the Reddit publishing
route — the same pattern as X → Sociamonials.

## What was built (`myUtils/bundle_social.py`, commits `f531c46`, `4a0fd97`, `dcbac99`)

A rules-first client (auth `x-api-key`, base `https://api.bundle.social`):

| concern | how it is enforced |
| --- | --- |
| subreddit rules | `GET /misc/reddit/post-requirements` before every submit |
| required flairs | account `flairIds` first, then `GET /misc/reddit/subreddit-flairs`; **refuses to post** when a required flair cannot be resolved |
| media kind | refuses native video / image / gallery the destination channel does not allow (live channel metadata) |
| our own subreddit registry | `myUtils/subreddits.py` fit check still runs (bans, self-promo, explicit-content subs) |
| duplicate posts | `referenceKey = sau-reddit-<target>-<sub>` + a by-reference-key lookup before creating |
| 20 posts/month cap | `GET /organization/usage/posts` checked before each create; `SAU_BUNDLE_SOCIAL_MONTHLY_POST_CAP` (default 20) also enforced |
| delivery | polls `GET /post/{id}` until `POSTED`/`ERROR`; a rejected post **raises**, never a phantom success |

The worker routes Reddit straight to it while the direct credentials are dead
(`SAU_REDDIT_DIRECT_PUBLISH=1` restores direct-first). 24 new tests; full suite
**1790 passed**.

## Live result — the blocker is the Reddit account, not the code

One real target reached the API end-to-end (`_run_target` → fallback → upload →
create), and **bundle.social then refused it**:

| attempt | result |
| --- | --- |
| image → `r/NudistMen` | `ERROR` `RD:403` "Could not upload media" |
| JPEG → `r/ryanbossom` (own sub) | `ERROR` `RD:403` |
| text → `r/ryanbossom` | `ERROR` `RD:SUBREDDIT_NOTALLOWED` "trusted members only" |
| text → `u/ryanbossom` (own profile) | `ERROR` `RD:SUBREDDIT_NOTALLOWED` |

Posting to the account's **own profile** returning `SUBREDDIT_NOTALLOWED`, plus the
direct path's `401`, means **`u/ryanbossom` is currently restricted/suspended at
Reddit**. It worked as recently as 2026-10-12 (15 Reddit targets succeeded), then
stopped on ~10-13 — consistent with an account-level action.

### Operator action required

1. Log in as `ryanbossom` and open <https://www.reddit.com/user/ryanbossom> — look
   for a suspension banner, or check messages for a ban notice.
2. If suspended, appeal; if the account is unusable, connect a **healthy** Reddit
   account to bundle.social (team `Gtrre`) and the pipeline will use it with no
   code change.
3. Until then, do **not** resubmit Reddit targets: each attempt consumes one of
   the 20 monthly posts (4 were spent proving this).

## Note on the 20-post cap

4/20 were used on the diagnostics above (all failed at Reddit). 16 remain. A
healthy account posting one subreddit per target gets ~16 Reddit posts this month;
`SAU_BUNDLE_SOCIAL_SUBREDDITS_PER_TARGET` raises or lowers that.

## Content-fit caveat

Even with a healthy account, most target subreddits (incl. `r/NudistMen`) do not
allow **native video**, and the NW/SW media is video. Native-video-capable
naturism subs on this account: `r/body_freedom`, `r/anarcho_naturism`,
`r/ryanbossom`. Either add those to the account's subreddits, or generate an
image/gallery from the video before scheduling.
