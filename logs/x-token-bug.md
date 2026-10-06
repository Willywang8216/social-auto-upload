# X/Twitter OAuth2 access-token expiry / refresh bug — root cause and fix

Date: 2026-10-06
Scope: `myUtils/prepared_publishers.py`, `myUtils/worker.py`, `sau_backend.py`
Investigated account: **124 "NW X (nudeweiwei)"**

## TL;DR

Three defects combined to kill account 124's X connection:

1. **Mixed timestamp bases.** `accessTokenUpdatedAt` / `accessTokenExpiresAt`
   were written with `datetime.now()` (naive **server-local**) in some paths and
   `datetime.now(timezone.utc)` (aware UTC) in others, while every reader assumed
   a naive value meant UTC. A 2-hour X token written naive-local is therefore
   read as expiring one UTC offset (8h in this container) too late, so the
   proactive refresh fires long after the token is already dead.
2. **Refresh coverage / staleness holes.** The publish path refreshes before
   use, but this deployment routes X through Sociamonials (`SAU_X_DIRECT_PUBLISH`
   unset), so publish-path refresh never runs. Proactive maintenance *does* exist
   (backend loop + in-process worker tick), but X's "unknown expiry" was treated
   as "not stale" and the naive-local mis-parse mis-timed the window.
3. **Refresh-token rotation race (the fatal one).** X rotates and invalidates the
   refresh token on every successful refresh. There were **two independent
   refreshers** (the backend maintenance thread and the in-process
   `PublishWorker` maintenance tick) plus the publish path, all doing
   read → refresh → **whole-config replace** (`profiles.update_account`) with **no
   shared lock**. Two refreshers could burn the same single-use token; a stale
   snapshot write then stores a dead token, and every later refresh fails with
   `invalid_request`/`invalid_grant` until a human reconnects.

The fix uses one UTC base everywhere, treats X's expiry defensively
(naive ⇒ server-local, unknown ⇒ stale), refreshes proactively, and serialises
refresh **per account** so the rotated refresh token is persisted inside the
lock and never lost. "Reconnect required" is now raised immediately only when X
rejects the refresh credential itself.

## Account 124 timeline (evidence)

`db/database.db` → `accounts.id=124`, and `account_events` (times UTC):

| when (UTC) | what | evidence |
| --- | --- | --- |
| 2026-10-02 14:29:27 | OAuth connect `@nudeweiwei` | `account_events.oauth_callback` |
| 2026-10-02 15:39→18:39 | hourly refresh OK | `account_events refresh_token ok` |
| 2026-10-02 21:09 → 10-03 03:30 | refresh errors `400 Bad Request`, every 10 min | `account_events refresh_token error` |
| 2026-10-03 09:22:54 | reconnected | `account_events.oauth_callback` |
| 2026-10-03 10:28 → 10-05 04:39:59 | hourly refresh OK (3 days) | `account_events refresh_token ok` |
| **2026-10-05 04:39:59** | **last successful refresh** | `account_events`; config `accessTokenUpdatedAt=2026-10-05T12:39:59` (naive local = 04:39:59 UTC), `accessTokenExpiresAt=2026-10-05T06:39:59+00:00` (= +2h, correct) |
| 2026-10-05 05:41:13 → 08:31:17 | refresh rejected `HTTP 400 (invalid_request)`, escalating back-off | `account_events refresh_token error` |
| now (2026-10-06 ~05:05 UTC) | access token expired ~22h ago; `_needsReconnect=True`, `_maintenanceFailures=6` | `accounts.config_json` |

The `accessTokenUpdatedAt` naive (`12:39:59`) + `accessTokenExpiresAt` aware
(`06:39:59+00:00`) pair is the exact signature of the **backend refresh route**
writing one field with `datetime.now()` and the other with
`datetime.now(timezone.utc)`. The 04:39 refresh succeeded, then the very next
attempt one hour later was rejected with the persisted refresh token — i.e. the
newly issued single-use refresh token was already invalid. The retained logs do
not contain the sub-second interleaving, but the code at the time allowed exactly
this: two unsynchronised refreshers and a whole-config replace that can discard a
freshly rotated token.

For contrast, accounts **77** and **103** (`accessTokenUpdatedAt=2026-10-06T12:56:22`
naive local, `accessTokenExpiresAt=2026-10-06T06:56:22+00:00` aware UTC) were
refreshed on the same code path and are still valid — the difference is that 124
lost its refresh token to a rejected rotation and every later attempt used the
dead token.

## Defects (pre-fix code references)

### D1 — mixed time bases
* `sau_backend.py::_run_account_token_refresh` (pre-fix ~L2443): `now = datetime.now().isoformat(timespec='seconds')` (naive local) for `accessTokenUpdatedAt`, but `datetime.now(timezone.utc) + expires_in` (aware UTC) for `accessTokenExpiresAt`.
* `sau_backend.py::twitter_oauth_callback` (pre-fix ~L5261): same mismatch on connect.
* `myUtils/prepared_publishers.py::_maybe_refresh_twitter_token` (pre-fix ~L2685): both fields `datetime.now()` (naive local).
* `myUtils/worker.py::_refresh_account` twitter branch (pre-fix ~L525): `accessTokenUpdatedAt = now` (naive UTC) but `accessTokenExpiresAt = datetime.now() + …` (naive local).
* Readers (`prepared_publishers._parse_iso_datetime`, `worker._parse_iso_datetime`) attach/assume **UTC** for naive values, so a naive-local expiry is read up to one offset too late.

### D2 — refresh coverage / staleness
* `myUtils/prepared_publishers.py::_maybe_refresh_twitter_token` used
  `now = datetime.now(timezone.utc) if exp.tzinfo else datetime.now()` — internally
  consistent, but only for that one call.
* `myUtils/worker.py::_is_account_stale` and
  `sau_backend.py::_is_refreshable_account_stale` treated a **missing** X expiry
  as `not stale` (`if expires_at is None: return False` for X), so a token whose
  expiry was lost/unparseable was never proactively refreshed.
* Proactive refresh itself existed: `PublishWorker._run_maintenance_tick`
  (`myUtils/worker.py`) and `_account_maintenance_loop`
  (`sau_backend.py`, `SAU_ACCOUNT_MAINTENANCE_INTERVAL_SECONDS=600`) both refresh
  stale accounts. The bug is not the absence of a scheduler but the unreliable
  staleness signal and the two schedulers racing (D3).

### D3 — refresh-token rotation race
* X invalidates the old refresh token on every successful refresh.
* `myUtils/worker.py::_refresh_account` and
  `sau_backend.py::_run_account_token_refresh` could both refresh the same account
  concurrently (the in-process worker tick is driven by the publish scheduler,
  `SAU_PUBLISH_SCHEDULER_INTERVAL_SECONDS=60`, during long drains).
* Neither held a lock; `profiles.update_account` (`myUtils/profiles.py::update_account`)
  **replaces `config_json` wholesale**, so a loser's stale snapshot can overwrite
  the winner's rotated refresh token.
* A prior fix (`36799da`) made the worker's *failure* path re-read the row before
  overlaying failure markers, but the *success* path and the backend route still
  wrote from a pre-refresh snapshot, and there was no single-flight.

### D4 — reconnect classification
* The worker flagged `_needsReconnect` only after `_MAINTENANCE_RECONNECT_THRESHOLD`
  (5) consecutive failures, even when X had definitively rejected the credential
  (`invalid_grant`), while the publish path surfaced the clear message
  immediately. Mixed signal.

## Fix

All edits are in the working tree (not committed).

### `myUtils/prepared_publishers.py`
* New `_parse_token_expiry()` (L413) — defensive parser; a **naive** value is
  interpreted as **server-local** (recovering the legacy writers' intent) and
  converted to UTC. A naive-UTC legacy value then only ever looks *staler*,
  which refreshes early rather than late.
* New `_x_access_token_stale()` (L441) — missing/unparseable/unknown expiry ⇒
  **stale**, so proactive refresh always fires before a 2h token lapses.
* New `_apply_twitter_token_payload()` (L454) — single source for
  `accessTokenUpdatedAt`/`accessTokenExpiresAt`, always aware UTC.
* New `twitter_refresh_lock()` (L402) and
  `refresh_twitter_token_single_flight()` (L501) — per-account
  read-check-refresh-**persist** under one lock; a losing caller re-reads the
  winner's fresh token and skips the network call.
* `_maybe_refresh_twitter_token()` (L2799) now takes `account_id`/`db_path`, goes
  through the single-flight helper, and distinguishes reconnect-required
  (`invalid_grant`/`invalid_token`/`refresh_token_revoked`) from retryable
  transport errors.
* `_maybe_refresh_meta_token` (L1407) and `_maybe_refresh_threads_token` (L1968)
  token timestamps switched to the same UTC helpers.
* `publish_twitter_sync` passes the account id + `_db_path` into the refresh.

### `myUtils/worker.py`
* `_is_account_stale` (L280) delegates X to `_x_access_token_stale` (naive-local
  and unknown-expiry handled correctly).
* `_refresh_account` (L380) uses aware-UTC `now`; the reddit/youtube/threads
  expiry writes use `_token_expiry_from_payload`; the twitter branch (L521) calls
  `refresh_twitter_token_single_flight` and returns after persisting (no second,
  race-prone write).
* `_handle_refresh_failure` (L577) flags `_needsReconnect` **immediately** when X
  rejects the refresh credential (instead of waiting for 5 failures).

### `sau_backend.py`
* `_run_account_token_refresh` (L2440) uses aware-UTC `now` and the single-flight
  helper for the Twitter branch (L2698), with the persist inside the lock.
* `twitter_oauth_callback` (L5181) writes aware-UTC token timestamps.
* `_is_refreshable_account_stale` (L2964) delegates X to `_x_access_token_stale`.

## Tests

New `tests/test_x_token_refresh.py` (15 tests) covers:
* expiry math: aware expired/fresh, missing/blank/unparseable ⇒ stale,
  **naive legacy value read as local** (`_parse_token_expiry`),
  naive-local already-expired ⇒ stale, and the payload writer producing one
  aware-UTC base;
* proactive refresh: an expired token is refreshed before use and the rotated
  refresh token is persisted; a fresh authoritative row skips the network;
* worker proactive staleness for legacy naive-local and missing expiries;
* **single-flight**: two concurrent refreshers make exactly one network call and
  the store keeps the rotated token;
* reconnect classification: `invalid_grant` ⇒ reconnect required; transport
  error ⇒ retryable.

Result:

```
.venv/bin/python -m pytest tests/ -q
1300 passed, 1 skipped, 83 subtests passed
```

## Operator action

Account **124 must be reconnected manually** via the Connect button — its stored
refresh token is dead and this code change cannot resurrect it (by design: only a
fresh OAuth round-trip clears `_needsReconnect`). No reconnect was attempted.
Accounts 77/103 and any other X account that still holds a valid refresh token
will now be refreshed before expiry by the single-flight maintenance path.

## Not changed / verified safe

* Docker container was **not** restarted and nothing was committed or pushed.
* Cookie-based X accounts are unaffected (still skipped by refresh logic).
* Non-X platform behaviour is preserved; only the token timestamp base and
  expiry helper changed for Meta/Threads (long-lived tokens, early refresh is
  harmless).
