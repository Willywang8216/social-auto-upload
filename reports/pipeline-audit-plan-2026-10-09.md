# Pipeline audit — consolidated plan

Date: 2026-10-09
Method: four parallel read-only audits (offload, platform caps, metadata/language,
scheduling/publish/fallback) plus my own DB and live-service checks.

Every finding below cites evidence that was actually executed. Severity is set by
what happens to a real publish, not by how interesting the bug is.

---

## Part A — Pipeline status by stage

| Stage | Verdict | Evidence |
| --- | --- | --- |
| **1. Offload / download-back** | Works, with one structural gap now fixed | `offload_to_drive.sh` verifies every byte with `rclone check` before unlinking, and keeps anything without a `file_records` row local. Restore has 5 fallbacks (`worker.py:1880-1967`). |
| **2. Platform restrictions** | Encoded, but 12 defects (B1-B12) | `platform_limits.py`; over-cap behaviour is split (a) or refuse (c) — except TikTok, which never splits. |
| **3. Video adjustment** | Split works; size math wrong; aspect unenforced | 38 twitter splits exist; TikTok branch drops all split parts; MiB-vs-decimal mismatch. |
| **4. Metadata / LANGUAGE** | Generated **once per campaign**, then language-checked per account | `worker.py:108-122` guard; 3 enforcement points. 9 defects. |
| **5. Scheduling** | Correct shape, two real holes | Stagger works per account (`_next_free_slot`, 30-min gap, 3/day). Collisions exist (S1); naive-local-as-UTC (S2). |
| **6. Publishing** | Correct per platform | 1029 succeeded targets. |
| **7. Sociamonials fallback** | Works, but fires when it must not | `should_attempt_fallback` is **never called** (dead code) — code contradicts its own docstring. |
| **8. Account routing** | Enqueue is correct; publish was unguarded | `_resolve_accounts` scopes by profile. Publish-time re-check was missing → **fixed** (R1). |

Live queue at audit time: 1282 pending, 1029 succeeded, 166 failed, **0 mismatched**
profile pairings, **0 running**.

---

## Part B — Bugs, prioritised

### Already fixed in this session

| # | Bug | Fix |
| --- | --- | --- |
| **R1** | No profile re-validation at publish time — an account moved between profiles after queueing would publish the wrong profile's campaign | `_account_profile_mismatch` + early non-retryable refusal in `_run_target` (`3dc5832`, tested) |
| **A1** | `add_campaign_artifact` never created a `file_records` row → 1831/2509 artifacts unrestorable, 241 pending targets doomed | registration at creation, idempotent, path-shape aware (`3dc5832`, 5 tests) |

### P0 — must fix (silent wrongness or burnt retries)

| # | Bug | Why it matters |
| --- | --- | --- |
| **B1** | Over-cap refusals raise `retryable=True` | A correctly-detected 600s-on-Threads failure retries 3× (target 5626 did exactly this). One line each in 4 guards. |
| **B2** | TikTok never gets a split — its branch ignores every `part_index` artifact | Work is done then thrown away; 601-3600s TikTok videos can never publish. |
| **B3** | Split plan uses MiB, caps and publisher use decimal MB | 305MB vs Instagram's 300MB: prep says "fits", publisher refuses. Split silently never happens. |
| **B5** | A duration-probe failure silently sets `duration=0` | Disables duration splitting for **every** platform, with no log. |
| **F1** | Fallback fires on permanent failures; `should_attempt_fallback` is dead code | Directly contradicts the module docstring: a banned subreddit or missing media gets re-routed to another account. |
| **S2** | Publish Center naive-local time treated as UTC | An operator picking 07:00 CST gets a post at 15:00 CST. Latent but severe. |

### P1 — should fix

| # | Bug | Why it matters |
| --- | --- | --- |
| **S1** | Non-atomic slot reservation → same-account collisions | 9 live collision groups (account:120 ×3 at one minute). Cosmetic-to-harmful depending on platform. |
| **R2** | Disabled accounts still publish | `_resolve_accounts(enabled=True)` at enqueue, no re-check at publish. |
| **B4** | The Threads split backfill creates artifacts but no posts/jobs | Campaign 2555's 3 parts are orphaned; my earlier report overstated the fix. |
| **B7** | TikTok cap 3600s vs API 600s | Depends on B2. |
| **B10** | Telegram Bot API 50MB cap not encoded; 4xx forced retryable | Burns retries on a deterministic refusal. |
| **M1** | `contains_simplified_chinese` misses common Simplified characters | Simplified text can reach a zh-Hant account. |
| **M3** | Trimming is code-point based: drops hashtags, splits words, ignores X weighted CJK | Posts can lose their tags or cut mid-word. |
| **M5** | The generated Reddit title is ignored | Titles are a core Reddit ranking input. |
| **M8** | `/campaigns/prepare` has no per-account error isolation | One bad account fails the whole prepare. |

### P2 — hygiene

**B6** two ffmpeg paths bypass `SAU_ENCODE_CONCURRENCY` (`twitter_uploader/main.py:149,307`;
`tg_review.py:239`). **B8** Threads 1024 vs 1000 MB. **B9** no aspect-ratio
enforcement (a ≤1080-wide landscape clip is never normalised to 9:16). **B11**
Reddit's native-video cap applied to a link-only path → unnecessary multi-posting.
**B12** `_select_videos_for_platform` can return a non-fitting part. **F2**
"accepted but unconfirmed" marked succeeded. **F3** Sociamonials reuse lookup is
single-page (200) → re-upload inflates the quota. **P1** lease loss does not roll
back enqueued jobs → possible double-enqueue. **M2/M4/M6/M7/M9** metadata
hygiene.

---

## Part C — Implementation order

Each step is independently shippable, with tests, and ordered so later fixes do
not depend on earlier ones.

**Step 1 — retryability and probes (B1, B5, B10)**
Smallest, highest ratio. Make over-cap and oversize refusals `retryable=False`;
a probe failure raises (or logs loudly) instead of implying "fits"; Telegram's
Bot-API cap derives from the resolved transport. Tests assert the flag, not just
that it raises.

**Step 2 — fallback policy (F1, F2, F3)**
Wire `should_attempt_fallback` into `_handle_failure` so permanent failures are
not re-routed; reconcile the test that asserts the opposite; treat
`requires_approval`/timed-out `pending` as not-delivered; paginate the reuse
lookup.

**Step 3 — scheduling integrity (S1, S2, R2)**
Atomic slot reservation in one `BEGIN IMMEDIATE`; convert the Publish Center's
naive-local time from the operator timezone (reject or convert, never assume
UTC); re-check `enabled` at claim time.

**Step 4 — platform caps correctness (B2, B3, B4, B7)**
TikTok considers split parts; decimal MB everywhere; TikTok cap 600s; make the
split backfill create posts/jobs (or refuse without them).

**Step 5 — metadata and language (M1, M3, M5, M8)**
Fix the Simplified-character set; trim on grapheme/word boundaries keeping
hashtags; honour the generated Reddit title; isolate per-account prepare errors.

**Step 6 — hygiene (B6, B8, B9, B11, B12, P1, remaining M*)**
Route the two stray ffmpeg calls through `encode_slot`; align Threads MB; add
aspect normalisation; exempt Reddit's link path; make lease loss roll back.

---

## Part D — Verification standard

Every fix ships with a test that fails before it and passes after, using the real
DB shape where possible. After each step: full suite green (currently **1435
passed, 1 skipped**), then a live re-check of the affected targets. No DB rows
mutated except additive repairs, and any migration is reversible.

Operator decisions still needed: Threads 1,000 vs 1,024 MB; whether an
unverified YouTube >15 min should be refused proactively; and whether the 241
legacy at-risk targets should be cancelled or re-prepped.
