# Reddit strategy: which subreddits to post to, and why

Date: 2026-10-08
Method: live audit of `/r/<sub>/about` and `/about/rules` via the account's own
OAuth token (`scripts/subreddit_audit.py`). Nothing here is guessed.

## The finding that matters

**Six of the nine subreddits you were posting SW content to cannot accept it.**
They are meme or discussion communities that ban NSFW imagery *and* ban
self-promotion. Our content is nude male-body video and sex-positive talk, and
the account promotes an adult brand. Every submit there was refused, removed, or
a step toward a ban.

## Account health (the real ceiling on growth)

| account | karma | age | recent submissions | verdict |
| --- | --- | --- | --- | --- |
| u/ryanbossom (NW) | 412 | 6.6 y | normal | **healthy** |
| u/sexualwill (SW) | 2 | 1.9 y | **10 of 10 removed** (`content_takedown`) | **spam-filtered** |

u/sexualwill is not suspended, but Reddit's spam filter removes everything it
posts. **No subreddit choice fixes a 2-karma account.** Posting more will not
build an audience from this state; it deepens the filter. Karma has to be earned
by participation first.

## Where to post now

### NW / u/ryanbossom (naturist nude male-body) - 4 of 4 usable

| subreddit | subscribers | notes |
| --- | --- | --- |
| r/gaybears | 242,452 | new; largest fit |
| r/GayChubs | 197,551 | new |
| r/NudistMen | 67,065 | kept; self post required (link domain not whitelisted) |
| r/BareMenPositivity | 3,561 | kept; full-body, non-sexual framing |

Dropped: **r/GayBody** - this account is banned there.

### SW / u/sexualwill (sex-positive adult brand) - 3 of 3 usable

| subreddit | subscribers | notes |
| --- | --- | --- |
| r/gaybears | 242,452 | accepts adult male nudity, no brand bar |
| r/GayChubs | 197,551 | same |
| r/gaysiansgonewild | 98,217 | same |

Dropped, all six: r/gaybrosgonemild, r/lgbt, r/gaymers, r/gay_irl, r/gaymemes,
r/GayMen - every one bans a monetised brand or NSFW media.

## How to actually grow

1. **Fix the SW account first.** Post genuine comments in r/gaybears and
   r/GayChubs for a few weeks. 2 karma is the whole problem; ~100+ comment karma
   stops the auto-removal. Do not publish media until then.
2. **NW is ready.** u/ryanbossom at 412 karma can publish immediately across the
   four subs above - roughly 500k combined reach.
3. **Follow each sub's rules**, now enforced in code:
   - r/NudistMen: flaccid only, no genital focus, self post (link domain is not
     whitelisted). Flair required and already configured.
   - r/BareMenPositivity: full body, no sexual framing, must have substance.
   - r/nudists (294k) is the largest naturist sub but **bans monetised accounts
     and all promotion** - worth reading, never worth publishing to.
4. **Do not** retry a subreddit that refused you. That is what produced the
   RATELIMIT on r/gaybrosgonemild. The publisher now refuses locally.
5. **Content that works** on these subs is nudist/body-positive, not promo copy.
   Titles that read as advertising get removed even where the media is fine.

## How the code now helps

- `myUtils/subreddits.py` - the rule registry. `denial_reason()` explains why a
  subreddit must not be attempted.
- `scripts/subreddit_audit.py --account N --recommend` - re-verify live rules
  and list subreddits that would accept the content. Read-only.
- Publishing to an **unverified** subreddit is refused by default, because that
  is how the bans happened. `SAU_SUBREDDIT_STRICT=0` relaxes only that check.
- The guard runs before any network call, so a doomed submit cannot spend a
  retry or provoke a rate limit.
