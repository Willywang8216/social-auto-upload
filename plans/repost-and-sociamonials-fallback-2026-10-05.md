# Plan — repost today's failures, publish the Stonewall video, add a Sociamonials fallback

Owner: pi-2650 · Started 2026-10-05 · Repo `/home/will/social-auto-upload`

## 0. What the operator asked for

1. Publish the Drive file `SFW Taipei Stonewall.mp4`
   (`1aMTpGMJwRGogkl6TUBB0HBRGE4bpFAy_`, ~2.0 MB, Drive root, public) to
   **Nakedwill + Sexualwill, all platforms, today**, with generated
   title/summary/description/rich metadata.
2. Repost the failed publishes from today.
3. Find and fix the code problems behind "almost all of them" failing.
4. After **3 failed attempts** a target must fall back to **Sociamonials MCP**
   instead of just failing.

## 1. Findings (evidence)

### 1.1 The "today" in the request is 2026-10-04, not the DB's job date

The last real publish flood ran on the **night of 2026-10-04** into the small
hours of 2026-10-05:

| Evidence | Value |
|---|---|
| `logs/worker.log` | 2026-10-04 23:0x → 2026-10-05 09:46 |
| `publish_job_targets.finished_at` | 36 failed / 49 succeeded since `2026-10-04 16:00` (00:00 Taipei 10-05) |
| `publish_jobs.created_at` | all `2026-10-02` because the **publish scheduler enqueues 3 days ahead** (08:0x UTC daily) |

So "today's failures" = the targets *executed* in that window.

36 failed targets, by platform:

| platform | failed |
|---|---|
| twitter | 23 |
| bluesky | 8 |
| facebook | 3 |
| reddit | 2 |

### 1.2 Root causes (distinct, each fixable independently)

| # | Symptom | Root cause | Location |
|---|---|---|---|
| A | `twitter` "X media upload (OAuth 2.0) request failed (HTTP 402)" | Account needs a paid X API tier for media upload, **or** the OAuth token lacks `media.write`. Fix already landed (`db9ca3f`) but **existing accounts must re-consent** — scopes only apply to a new consent. | `myUtils/x_auth.py` `DEFAULT_SCOPES` |
| B | `twitter` account 123/124 `MediaRestoreError: Generated artifact is missing` / `TimeoutError: X did not render all requested media attachments` | account 123/124 are `twitterAuthType: cookie` → browser automation path, which does not run the prepared (API) media pipeline. | `docs/agent-bootstrap.md` (10th handoff) |
| C | `bluesky` `HTTP 413 PayloadTooLarge` | Bluesky's blob limit (`~1 MB` default / `~976 KB` observed) — media is sent at full size. | `myUtils/prepared_publishers.py` `_bluesky_upload_blob` |
| D | `facebook` `HTTP 400 Unable to fetch video file from URL` | Graph fetches the artifact URL; the URL must be a directly-GET-able MP4. Fixed partly by `57e61bd` (public `generated/`), still fragile. | `myUtils/media_remote_storage.py`, `sau_backend.py:/getFile` |
| E | `reddit` `SUBREDDIT_NOTALLOWED_BANNED` / `SUBMIT_VALIDATION_FLAIR_REQ` | Account is banned in `r/GayBros`; `r/NudistMen` now requires a post flair. **Config-level, not a code bug.** | `tools/reddit_schedule.py` |
| F | `HTTP 402` / `401` / `invalid_grant` on twitter | Dead or under-scoped OAuth tokens. | `myUtils/x_auth.py` |

### 1.3 Nothing schedules in the past

`jobs._claimable_clause` only hands out targets whose `schedule_at` is empty or
already due. Every one of the 36 failed targets has `schedule_at` in the past,
so **a plain resubmit will run immediately** — no schedule rewriting needed.

`jobs.resubmit_target()` already does exactly this (`failed|cancelled → pending`,
`attempts=0`, clears `schedule_at` when it has passed) and is exposed over HTTP
at `POST /jobs/targets/<id>/resubmit`.

### 1.4 Auth for driving the API

`.env` has `SAU_API_TOKENS` (42 chars) and `SAU_AUTH_MODE=hybrid`, but
`SAU_GOOGLE_LOGIN_ENABLED=true`. A **bearer token** satisfies the gate
(`_enforce_auth`), and CSRF is only enforced for session-cookie requests — so

```bash
curl -X POST http://localhost:5409/... -H "Authorization: Bearer $SAU_API_TOKENS"
```

works from the host. Verified: `POST /publish-center/submit {}` → `400 profileIds is required`,
and without the header → `401`.

### 1.5 Sociamonials fallback: reachable, but not wired

* REST works from the host today:
  `GET https://www.sociamonials.com/api/v1/workspaces` → 200.
  Key: `~/.claude/secrets.json → SOCIAMONIALS_API_KEY` (`sm_agent_…`),
  workspace `superwill1` = **26985**, adult brands (NW + SW).
* **The MCP connector is NOT registered on this machine.**
  `claude mcp list` shows 16 servers and no `social-media-14cf3286`;
  `~/.claude.json.mcpServers` has no `_source: "manual"` entry, and
  `~/.claude.json.bak-normalize` does not exist — `normalize-config.cjs` has
  never run here. The skill at
  `/home/will/claude-settings-backup/skills/social-media-workflow-14cf3286/`
  documents the connector but cannot create it.
* Therefore: implement the fallback as a **module that speaks the same API the
  MCP wraps** (`POST /api/v1/posts`), because the worker runs headless in a
  container and an MCP HTTP handshake there is strictly worse. Document the
  mapping to MCP tool names (`create_social_post`, `import_media_from_url`,
  `list_social_profiles`).

### 1.6 Sociamonials profile map (discovered, live)

```
fb   19784|175913  Nakedwill2          ← SAU account 11  (facebook)
fb   30186|255814  Sexualwill          ← SAU account 64  (facebook)
tw   13425         model_will          ← SAU account 123 (twitter)
tw   13426         nudeweiwei          ← SAU account 124 (twitter)
tw   14100         will_sexual         ← SAU account 77  (twitter, sexualwill)
blsk 1280          nakedwill.bsky.social    ← SAU account 118/119 (bluesky NW)
blsk 1281          sexualwill.bsky.social   ← SAU account 120/121 (bluesky SW)
thrd 361           nakedwill8          ← SAU account 62  (threads NW)
thrd 1939          sexualwill8         ← SAU account 42  (threads SW)
tiktok 3917        Nakedwill           ← SAU account 109 (tiktok)
yt   13223         itsnakedwill        ← SAU account 110 (youtube)
```
Not mapped: `reddit`, `telegram`, `nw_sw_blog` (no Sociamonials network).

Media transfer: upload with `POST /api/v1/media/uploads` → PUT bytes → complete,
or `POST /api/v1/media/imports` from a public https URL. An external
`video_url` is also accepted if it is a **direct file URL** (Google Drive share
links are explicitly rejected).

## 2. Plan

### Phase 1 — Repost today's failures (no code change)

1. Snapshot the DB (`db/database.db.before-repost-<ts>.bak`).
2. Resubmit the 36 failed targets whose window is 2026-10-04 16:00 → now, via
   `POST /jobs/targets/<id>/resubmit` with the bearer token.
3. Rerun `newly resubmitted` once the fixes in Phase 3 land, in this order:
   **bluesky → reddit(config) → facebook → twitter** (cheapest/most-likely first).
4. Report per-platform outcome.

### Phase 2 — Publish the Stonewall video today

1. Put the file where `file_records` can resolve it — it must end up under
   `videoFile/` inside the container. Host `videoFile/` is root-owned, so use
   `docker cp` into the container and let the host bind mount show it.
   Keep the name containing the `SFW` token (already does) so
   `content_rating` rates it **sfw** (verified: `rating_for_filename("SFW Taipei Stonewall.mp4") == "sfw"`).
2. `POST /publish-center/preview` with `profileIds: [1, 3]`, the media path and
   `options` to generate per-account drafts (title/summary/description/hashtags
   via the configured LLM).
3. Capture the returned drafts verbatim.
4. **Send the generated metadata to the operator for review before publishing**
   (the app routes every post through the Telegram review card; the operator
   asked for rich metadata, which is the one thing worth eyeballing).
5. `POST /publish-center/submit` with `schedule: {"publishNow": true}`,
   `profileIds: [1, 3]` and the approved `accountDrafts`.
   Accounts 72/62 (NW IG/threads) and 75/42 (SW IG/threads) will be **skipped**
   by nothing here (SFW is allowed on them) — expect the full platform set.

> Note: `sfw` names are eligible for IG/FB/Threads/YT, so this file goes to
> **all** platforms for both personas.

### Phase 3 — Fix the causes, so the reposts succeed

| Fix | File | Change |
|---|---|---|
| C (bluesky 413) | `myUtils/prepared_publishers.py` | Downscale/re-encode an image or video in-memory (or via `media_pipeline`) until it is under Bluesky's blob limit before `_bluesky_upload_blob`; log the original and final size. |
| D (facebook URL fetch) | `myUtils/media_remote_storage.py` | Guarantee the artifact URL is a **direct file URL** the Graph API can GET, and verify with a HEAD before submit; prefer DO Spaces/CDN over any page-serving host. |
| A (twitter media.write) | `myUtils/x_auth.py`, `sau_backend.py` | Already landed; add a one-time migration banner + a `check-connections` signal that says "re-consent required" for accounts whose stored scope predates `media.write`. |
| E (reddit) | `tools/reddit_schedule.py` | Drop `r/GayBros` for account 106; add `flair_id` support for `r/NudistMen` (or switch the subreddit). Config-only. |
| F (token refresh) | `myUtils/worker.py` | Surface `invalid_grant` / `402` as a **non-retryable** failure that also flags the account for reconnect (partly present; verify it short-circuits the retry budget so the fallback fires sooner). |

### Phase 4 — Sociamonials MCP fallback after 3 attempts

New module `myUtils/sociamonials_fallback.py`:

* `is_configured()` — needs `SOCIAMONIALS_API_KEY` (+ optional
  `SOCIAMONIALS_WORKSPACE_ID`, default 26985).
* `SAU_ACCOUNT_TO_SOCIAMONIALS` — the map in §1.6, overridable from
  `profiles.settings["sociamonials"]` so an operator can re-point an account
  without a code change.
* `publish_via_sociamonials(*, platform, account, payload, media_paths) -> dict`
  — `POST /api/v1/posts` with `mode: "publish_now"`, `message` from the draft,
  `image_urls` / `video_url` from the artifacts, and
  `networks: {<network>: {profile_refs: [<ref>]}}`,
  `idempotency_key` = `sau-target-<target_id>`.
* Media: prefer an existing public artifact URL; otherwise
  `POST /api/v1/media/imports` (or the 3-step upload) to get a hosted URL.

Worker change (`myUtils/worker.py::_handle_failure`):

```
attempts >= max_attempts
  → if not retryable-by-definition: skip
  → try sociamonials_fallback.publish_via_sociamonials(...)
       success → mark_target_success(); log "delivered via Sociamonials fallback"
       failure/not-configured/no-mapping → mark_target_failed() + alert (as today)
```

* Guarded by `SAU_SOCIAMONIALS_FALLBACK=1` (opt-in, default off) so the existing
  behaviour is unchanged until the operator turns it on.
* Never fires for a target that is non-retryable for a *permanent* reason
  (banned subreddit, missing media) — only after a genuine 3-attempt exhaustion.
* Fully unit-tested with an injected HTTP session; no live call in tests.

### Phase 5 — Verification

* `pytest tests/ -k "worker or publish or offload or backend or content_rating"`
  must stay green.
* New tests: `tests/test_sociamonials_fallback.py`,
  bluesky-downscale test, reddit-config test.
* End-to-end: resubmit one bluesky + one twitter target and read the job log
  (`logs/jobs/job-<id>.log`).

## 3. Open questions for the operator

1. **Twitter re-consent** — accounts 77/103/123/124 need a fresh OAuth consent
   (browser, one per account) before Twitter can carry media again. Can this be
   done now, or should the fallback carry Twitter for today?
2. **`Facebook fetch-from-URL`** — if the Graph API still refuses the artifact
   URL, is it acceptable for the fallback (Sociamonials) to publish Facebook
   instead?
3. **Bluesky media limit** — downscaling changes what gets published
   (e.g. 1080p → ~720p). Acceptable, or should oversized media skip Bluesky?
4. **Reddit** — update `r/GayBros` → another subreddit for account 106, and add
   the required flair for `r/NudistMen`, or leave Reddit out today?
5. **Fallback default** — turn `SAU_SOCIAMONIALS_FALLBACK` on once implemented?
