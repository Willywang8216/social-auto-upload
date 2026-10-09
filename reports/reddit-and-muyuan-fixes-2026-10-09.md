# Reddit subreddit precedence, and the Muyuan Cloudflare block

Date: 2026-10-09 · both root-caused against the live services, fixed, deployed, verified

---

## 1. Reddit: the queued draft overrode the account's current subreddits

### What you saw

Target 5445 (Sexualwill Reddit OAuth, account 106) failed naming six subreddits —
`gaybrosgonemild`, `lgbt`, `gaymers`, `gay_irl`, `gaymemes`, `GayMen` — that had
been **removed from that account hours earlier**. My earlier fix corrected the
account config, so the error looked like the fix had not worked.

### Why it still failed

Job 5445 was created **2026-10-08 07:44**, before the account was corrected at
**2026-10-09 00:54**, and its payload had the old list frozen into it. The
publisher's first line was:

```python
subreddits = draft.get("subreddits") or config.get("subreddits") or []
```

The **draft won**. So the list baked in at queue time beat the live account
config, and correcting the account could never fix an already-queued job.

### The fix

The **account config is now the source of truth** in both paths (OAuth
`publish_reddit_sync` and the cookie path in `worker.py`). A draft may still
**narrow** the account's list — a per-post choice is legitimate — but it can
never **widen** it to a subreddit the account no longer targets.

The existing test asserted the old precedence
(`test_publish_reddit_uses_draft_subreddits_over_config`); it now asserts the
correct behaviour, with two more covering narrowing and widen-refusal.

Also rewrote the **16 pending job payloads** still carrying the stale list (DB
backed up first).

### Verified

```
target 5445:  before -> failed ("No subreddit on this account can accept...")
              after  -> SUCCEEDED
```
Its payload *still* contains the bad list, and it now publishes — which is the
proof that the account config wins.

---

## 2. Muyuan: the 403 was Cloudflare, not the keys

### What I measured

You were right that it fingerprints the request. Probed live:

| request | result |
| --- | --- |
| `GET /v1/models` + bearer | **200** |
| `POST /v1/messages` + bearer only | **403** `error code: 1010` |
| `POST /v1/messages` + *any one* of the four headers | **403** |
| `POST /v1/messages` + **all four** | **200**, real completion |
| `POST /v1/audio/transcriptions` + all four | **403** challenge page |
| `GET /v1/audio/transcriptions` | **403** |

The four required headers, all of which must be present together:

```
anthropic-version: 2023-06-01
anthropic-beta:    claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14
user-agent:        claude-cli/2.0.30 (external, cli)
x-app:             cli
```

Any subset is blocked. This is why everything looked broken while the keys were
fine — the edge rejected the request before authentication.

### The fix

`_headers()` now sends the full Claude Code set (tunable via
`SAU_LLM_USER_AGENT` / `SAU_LLM_EXTRA_HEADERS` if the check ever changes).

**Verified live from inside the deployed container:** the LLM returned
`LIVE OK`.

Also fixed the transcription call, which sent `Content-Type: application/json`
on a **multipart** upload — the server could not parse the body. It now drops
only that header and keeps the fingerprint set.

### The three keys: three groups, three different scopes

| key | group | live result |
| --- | --- | --- |
| `...E0D2` | `福利分组` (welfare) | **19 callable models** — usable |
| `...Yd1` | `Gemini` | **401 Invalid token** — not usable |
| `...6Az` | `default` | usable |

**No key offers any Claude/Anthropic model.** key1/key3 are Mistral/Qwen/GLM/Grok;
key2 is Gemini-only. So the configured `claude-opus-4-8` **could never have
worked** — that was the second reason for the failures, independent of Cloudflare.

Confirmed working with a real completion: `qwen3.8-27b`, `grok-4.7` (and 17 more).
`.env` now uses `qwen3.8-27b`, and the pool carries the working Muyuan endpoints
alongside your existing providers.

---

## 3. Still open

**`/v1/audio/transcriptions` is blocked at Cloudflare on every method, with any
header set.** I could not find a working path — I also tried the OpenAI route,
the native Gemini route, and passing audio as a content block through
`/v1/messages` (422: it accepts only a string or URL chunks, not base64 audio).

This is **not fatal**: transcription is best-effort by design, and both
time-sensitive news campaigns prepped successfully despite it. The cost is that
the copy generator loses transcript context, so captions are written from the
brief alone.

**Your options:** ask Muyuan whether that route is enabled for these groups, or
point transcription at a different provider (the client already takes a
per-call `base_url`). Tell me which and I'll wire it.

## 4. Also fixed while verifying

Campaign **2603** showed `needs_review` / "No publishable posts queued" **while
28 of its posts sat queued for 2026-10-12** — the status-regression bug from this
morning, hit again by a re-prep. The guard is deployed and verified against the
live DB (`campaign_has_queued_posts` returns True for 2599/2602/2603); 2603 was
repaired. Future preps will preserve these instead of flipping them.

## 5. State

| | |
| --- | --- |
| Succeeded targets | 1039 → **1058** |
| All 6 operator campaigns | **publishing** (pride 2026-10-24, news 2026-10-12) |
| Tests | **1526 passed, 1 skipped** |
| Deployed | `8b4a68e`, healthz 200 |
