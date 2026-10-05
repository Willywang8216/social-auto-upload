# X (Twitter) credits + Sociamonials fallback — operational note

Written: 2026-10-05 by session `x-credits` (bb6304cf). Read-only investigation;
no code changed. Evidence: `accounts`/`publish_job_targets` in `db/database.db`,
`logs/jobs/*.log`, `logs/worker.log`, live read-only Sociamonials GETs.

## TL;DR

- The X developer account is **out of API credits**. Media upload returns
  `HTTP 402 {"detail":"credits depleted"}`. Re-auth does **not** fix this; the
  tokens are valid. Only an X billing top-up restores direct X media posting.
- The Sociamonials fallback map covers **3 of the 5** X accounts. **103 and 107
  are NOT mapped.**
- **103 ("光光" / `nakedhappylife`) is the actionable gap**: Sociamonials already
  has a connected, publishable X profile for it (`tw 14099 nakedhappylife`), so
  a one-line map entry would give it a fallback. It needs the code owner.
- **107 ("Willy Dev tutor" / `willytutordev`) has no matching Sociamonials X
  profile at all**, so it cannot fall back. It has had no targets since
  2026-07-09, so it is not currently exercised.
- Important caveat: the worker marks a fallback target **succeeded when
  Sociamonials accepts the post, not when it is delivered**. Three video
  fallbacks were accepted and then failed delivery on the Sociamonials side
  because the R2 video URL was unreachable. See "Delivery is not success".

## X accounts and fallback coverage

| SAU id | account_name | X handle | auth | Sociamonials `tw` ref | fallback? |
|-------:|--------------|----------|------|-----------------------|-----------|
| 77 | sexualwill | will_sexual | oauth | 14100 `will_sexual` | **YES** (mapped, proven) |
| 123 | NW X (model_will) | model_will | **cookie** | 13425 `model_will` | **YES** (mapped) |
| 124 | NW X (nudeweiwei) | nudeweiwei | oauth | 13426 `nudeweiwei` | **YES** (mapped, proven) |
| 103 | 光光 | nakedhappylife | oauth | **14099 `nakedhappylife` exists but UNMAPPED** | **NO** |
| 107 | Willy Dev tutor | willytutordev | oauth | none | **NO** |

Source of truth for the mapping is `SAU_ACCOUNT_TO_SOCIAMONIALS` in
`myUtils/sociamonials_fallback.py` (held by `sau-repost-and-fallback`; not
edited by this session). The running container at
`ghcr.io/willywang8216/social-auto-upload:latest` (image id `2eb14419…`) only
contains 77/123/124 for `tw`.

Live profile list (`GET /api/v1/workspaces/26985/social-profiles`) confirms the
`tw` refs and that all six are `connected=true`, `agent_can_publish_to=true`:
`13425 model_will`, `13426 nudeweiwei`, `13547 digihuntingpro`,
`13683 willywangdata`, `14099 nakedhappylife`, `14100 will_sexual`.

### Account 123 (cookie mode) still falls back

The fallback path does not use the SAU account's token. `resolve_mapping()`
keys only on account id + platform, and `_resolve_structured_account()` loads
the row regardless of `auth_type`. So cookie-mode account 123 resolves to
Sociamonials profile `13425`, which is connected. Cookie mode is not a blocker.

### Account 103 ("光光") — why it has not fallen back

Two separate reasons, both true on the current code:

1. **Historical "requires reconnection" failures were classified permanent.**
   `_maybe_refresh_twitter_token()` raises `PreparedPublishError(..., retryable=False)`
   when `config["_needsReconnect"]` is set. In `worker._handle_failure()`, a
   `retryable is False` error goes straight to `mark_target_failed()` and
   **returns before the fallback branch**, on attempt 1. Affected targets:
   `3838, 3892, 1814, 2840, 3805, 1815, 2536, 2843` (last at 2026-10-05
   04:50 UTC). Two earlier `X OAuth 2.0 access token could not be refreshed`
   failures (`3869, 3861`) hit the same early return.
2. **Even now that the token is healthy, 103 is unmapped.** Account 103's
   `_needsReconnect` marker is gone — `lastAutoRefreshAt` is
   `2026-10-05T14:10:50` and its config no longer carries the marker. From now
   on a credits-exhausted failure is `HTTP 402` and is classified
   **retryable=True**, so the worker reaches the retry budget and *does* call
   the fallback — which then raises `no Sociamonials profile is mapped for
   account 103`. The target is marked permanently failed anyway.

**Is skipping the fallback on a non-retryable error the right call for a
credits-exhausted account?** The non-retryable skip does **not** apply to
credits exhaustion: 402 is retryable, so the fallback is attempted. The skip
only explains the older reconnect failures. That said, a *dead SAU credential*
is exactly the case where Sociamonials is most useful, because Sociamonials
holds its own independent OAuth connection — so "retryable=False → never fall
back" is arguably too blunt for `X account requires reconnection`. That is a
policy change in `myUtils/worker.py` (held by pi-2650); report, don't patch.

**Fix for 103 (report-only):** add
`103: {"network": "tw", "profile_refs": ["14099"], "name": "nakedhappylife"},`
to `SAU_ACCOUNT_TO_SOCIAMONIALS`.

## Idempotency — no double-post

`publish_via_sociamonials()` sets:

- `body["idempotency_key"] = f"sau-target-{target_id}"` when `target_id` is not
  `None` (`myUtils/sociamonials_fallback.py`, post-create).
- media uploads carry `f"sau-target-{target_id}-media-{filename}"`.

`worker._try_sociamonials_fallback()` always passes `target_id=target.id`, so
the key is deterministic per target row. Crucially, `jobs.resubmit_target()`
**re-queues the same row** (`UPDATE publish_job_targets ... WHERE id = ?`); it
does not insert a new target, so the id — and therefore the idempotency key —
is stable across operator resubmits. A retried/resubmitted target cannot create
a second Sociamonials post. No duplicate fallback posts were observed in
`published-messages`.

Caveats:
- The key only dedupes on the **Sociamonials** side. A direct X retry that
  timed out *after* X created the tweet could still double-post; not observed
  here, because the 402 fails at media upload before tweet creation.
- Called without `target_id` (e.g. ad-hoc CLI use) there is no key. The worker
  always supplies one.

## Delivery is not success (important)

`worker._try_sociamonials_fallback()` calls `jobs.mark_target_success()` as soon
as `POST /api/v1/posts` returns, *before* Sociamonials actually delivers.
`GET /api/v1/posts/<id>` shows:

| post_id | SAU target / account | post media | Sociamonials result |
|--------:|----------------------|------------|---------------------|
| 10689013 | 1562 / account 124 | photo | **delivered** — `platform_post_id=2106966972328222789` |
| 10689041 | 2842 / account 77 | video | **failed** — "Video URL not accessible after retries (possible NFS sync delay)": R2 `campaigns/2222/videos/...mp4` |
| 10689111 | account 124 | video | **failed** — same, R2 `campaigns/2326/videos/...mp4` |
| 10689117 | account 77 | video | **failed** — same, R2 `campaigns/2389/videos/...mp4` |

So the earlier claim that job 2842 delivered is **not accurate** — the post was
accepted then failed at delivery. Only 10689013 is a genuine delivery (the photo
case). The video fallbacks relied on a direct R2 URL that Sociamonials could not
fetch; the host-side `_verify_remote_media` HEAD fails open, so an R2 object that
is not yet visible to Sociamonials is still handed over. A robust fix would
prefer uploading the local bytes to Sociamonials (or only trust the R2 URL after
a fresh sync check) instead of trusting the public URL. That lives in
`myUtils/sociamonials_fallback.py` (held) — report only.

## What the operator must do

1. **Top up X API credits** if direct X media posting should resume. Nothing
   else (re-auth, refresh) will help — the failure is billing, not tokens.
2. **Or accept Sociamonials-only for X.** Then:
   - Add the 103 → `14099 nakedhappylife` map entry (code owner).
   - Accept that **107 has no X fallback** — it has no Sociamonials X profile.
   - Watch Sociamonials `needs_attention` for video posts whose R2 URL failed,
     and consider fixing video media transfer (prefer upload over R2 URL).
3. Note the current pending X backlog that will flow through the fallback as it
   drains: account 103 = 131 pending (no fallback), 123 = 422 + 1 running,
   124 = 422, 77 = 130.
