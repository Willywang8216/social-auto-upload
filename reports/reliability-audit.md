# Reliability repair audit and release evidence

Approved scope: **Resume social-auto-upload reliability repairs**, 2026-11-01.

## Frozen audit snapshot and live handoff

This report describes the audit and integrated repair **before release promotion**.
The exact-commit release gates and controlled rollout are recorded separately in
the workspace `HANDOFF-social-auto-upload.md`, mirrored to the VPS's ignored
`logs/reliability-handoff.md`. That handoff is updated after test/deployment milestones
without changing the source commit being tested. A focused test pass is not a release gate.

## Preserved baseline

| Item | Verified baseline |
| --- | --- |
| VPS project | `/home/will/social-auto-upload` on `emailserver` |
| Preserved unpublished source HEAD | `2e30b1bed7bae42e007237dcf85462c1c12e8e0c` |
| Original running revision / origin main | `d051e7b995e83f3cf1e8c6d75c25c03d584bac1d` |
| Original image | `sha256:cf373b254f8f2780d1b3cdd0fdf941e23255cc5c106adf712de4a033081921ee` |
| Production interpreter | Python 3.10.19, Linux aarch64 |
| Isolated VPS candidate | `/home/will/sau-reliability-20261101` |
| Repair branch | `repair/reliability-20261101` |

The Windows checkout began as a source snapshot, not a clone of the original Git history.
Changes are transferred by per-file SHA-256 checks into the VPS worktree descended from
the actual unpublished HEAD. Unexpected content aborts the transfer. The live checkout
was rechecked clean and the original container healthy with zero restarts during implementation.

Only the isolated worktree, source-transfer cache, and disposable test image/container
have been created on the VPS so far. No production database, schedule, credential,
cookie, media, or service changes have been applied.

## Audit findings

The audit reproduced the aware/naive expiry exception, recovery beyond the retry budget,
incorrect aggregation, swallowed HTTP errors, and lost Telegram progress after filtering.
It also found process-local account exclusion, unfenced completions, unsafe operator
provisioning, import-owned background work, ambiguous restoration, and shallow release checks.
Expanded static checks found undefined names in the Douyin and Xiaohongshu adapters.

At approval, recorded target counts were 3,804 pending, 195 failed, 2,133 succeeded,
and 5,068 cancelled. Seventy pending targets referenced deleted accounts (30 Instagram,
40 Threads). Earlier historical inspection counted 969 inconsistent job summaries,
57 empty jobs, and 39,485 FK violations; those historical totals are not current recounts.
**Recorded success is not independent evidence of provider delivery.**

A read-only recheck at 2026-11-01 15:14 UTC found the original checkout still clean
and its container healthy with zero restarts. Normal live activity had changed the
counts to 3,828 pending, 209 failed, 2,185 recorded succeeded and 5,068 cancelled;
the 70 missing-account pending targets were unchanged. No reconciliation was applied.

## Implemented safeguards

- Verified Google identities require explicit operator email/subject allowlisting in
  single/shadow mode, including existing sessions. Missing policy fails closed.
  Bearer-token access is retained. At rollout, Google login will be disabled if no
  explicit allowlist is configured; no identities will be guessed.
- Durable target leases and fencing, canonical provider-account exclusion, delayed
  retries, finite recovery budgets, worker presence, and one aggregation rule.
- Separate `blocked`, `awaiting_delivery`, and `needs_review` outcomes. Provider acceptance
  no longer automatically means delivery. Pollable IDs and per-operation progress survive
  retries; unknown acceptance prevents blind resend.
- Telegram operation checkpoints, TikTok receipt polling, Meta container resumption,
  Sociamonials confirmation, and operation journals for active API publishers. These are
  provider-specific safeguards, **not a provider-independent exactly-once guarantee**.
- Flask-free shared callbacks and atomic asynchronous web/inbox/MCP admission. Stable
  submission IDs replay the original receipt; changed intent returns a conflict. Inbox
  registration is deterministic and network-free; the worker downloads the exact source.
- Preparation enqueueing is transactionally fenced and linked to its campaign post.
  Campaign summaries follow queue outcomes, not admission. Web imports do not launch
  publishing/cleanup loops; production HTTP queue draining is disabled.
- Owned child-process deadlines and process-group cleanup, including descendants whose
  root exited first. Parent retry limits reach child executors.
- Configured database paths and closing connection helpers; exact restoration mappings,
  atomic URL downloads, byte counts/SHA-256 for new artifacts, and retention of unresolved
  provider media even after cancellation. Unreadable live references stop reclamation.
- Rejected frontend transport/server failures, retained credentials on 403, stable client
  submission IDs, non-overlapping cancellable polling, and distinct delivery/action states.
- Read-only database/schema readiness, worker freshness, revision reporting, scoped and
  redacted queue health, due-versus-future scheduling, and monitor cooldowns.
- Matched web/worker compose services; frozen dependency installs; shared release gates;
  online-consistent SQLite backup and isolated restore verification; bounded pause/drain,
  verified image/config baselines, and DB-preserving image rollback tooling.

The bounded release-safety review found three additional issues, now fixed:
resume verifies a sealed recovery bundle and available prior images; missing or
unreadable configuration mount sources fail before service changes; a failed
dispatch scan records an unready worker rather than refreshing a healthy heartbeat.

## Validation performed so far

These runs overlap and must not be summed as a unique test count.

| Check | Result |
| --- | --- |
| Parent integrated queue/submission/preparation/security/media tests, Python 3.11.16 on Windows | **160 passed, 1 Linux-only skipped**, 50.69 seconds |
| Frontend unit suite and production build, Node 24.18.0 | Delegate verified **122 passed**, build passed |
| Provider receipt and adapter regressions, Python 3.11.16 | Delegate verified **247 passed, 31 passing subtests** |
| New publishing health/readiness regressions | Delegate verified **42 passed** |
| Release safety utility tests | Delegate verified **69 passed, 2 Windows symlink skips** after recovery-seal fixes |
| Expanded maintained-source static check | Parent verified **436 files, zero errors** |
| Release script shell syntax and embedded Python | Delegate verified; full Linux release checks still required |
| Full Linux Python 3.10.19 diagnostic container run | **2,201 passed, 152 failed, 2 skipped, 161 passing subtests**, 991.28 seconds; not a release pass |
| Complete media/admission regressions after integration fixes | **164 passed, 22 passing subtests**, 55.80 seconds |
| All 41 changed backend test files on Windows | **1,168 passed, 6 fixture failures, 4 skipped, 56 passing subtests**, 395.43 seconds; final Linux matrix remains required |
| Final corrected offload/service/rclone fixture set | **28 passed**, 4.58 seconds; the six fixture failures above were corrected |

The Linux diagnostic used a source-only image, frozen web/dev dependencies, no network,
no production mounts, a 3 GiB memory limit and two CPUs. Tests use synthetic databases,
media and credentials. Collection refuses a checkout containing production configuration
or its canonical database. Test network guards allow only loopback fixtures. The browser
installation exposed a missing `libcups2t64` dependency in the Debian 13 base; it has been
added to the release Dockerfile and the isolated Python 3.12.14 test image successfully
opened Chromium to `about:blank` with networking disabled. Production-image smoke remains a gate.

## Remaining release gates

1. Classify and fix the full-suite failures, preserving policy assertions and adapting
   tests to the new asynchronous and receipt-confirmation contracts where appropriate.
2. Complete the Linux Python 3.10/3.12 matrix and pinned frontend gates on the exact commit.
3. Validate compose, build the release image, and test schema/readiness/browser startup.
4. Verify online backup and isolated restore; run reconciliation dry-run and apply against
   a protected VPS-side snapshot first. Prove schedules and terminal intent are preserved.
5. Hold the deployer lock, quiesce legacy execution, capture baselines, migrate/reconcile
   conservatively, and deploy matched services only after all gates pass.
6. Observe revision, readiness, heartbeat and normal scheduled progress without test posts.
   Never restore an old DB over newer provider side effects during image rollback.

No credential rotation, mass retries, account guessing, content-policy relaxation,
PostgreSQL migration, or re-enabling retired Reddit publishing is included. Revoked
credentials, provider restrictions/credits, and ambiguous deleted destinations remain
operator-dependent conditions; the application must contain and explain them.

## Protected-copy history gate and dependency notices

The first exact-commit backend gates passed 2,430 tests and 170 subtests on each
of Linux Python 3.10.19 and 3.12.14 (two environment-dependent skips each).
Nevertheless, protected-copy verification rejected that candidate: 878 existing
terminal jobs had unknown finish timestamps, which aggregation would replace
with the reconciliation time. Aggregation now preserves those unknown historical
timestamps and catalogues them as `missing_terminal_timestamp`; actual new terminal
transitions still record their completion time. Seven regression cases cover this.

The correction passed 130 focused tests (one Linux-only skip) and a protected
VPS-side copy check. Logical data from all 42 original tables survived restoration
and migration. Schedules, payloads, credentials, 7,462 terminal targets and 6,656
original terminal parent records were preserved. Safe copy repairs comprised 70
blocked missing-account targets, 1,096 parent corrections, and 38,880 declared
nullable SET NULL references. The remaining 605 historical orphans are catalogued,
not deleted; 1,044 records need review, including unknown timestamps. A second
reconciliation proposed no further safe changes. These are protected-copy results,
not a claim that production reconciliation has already run.

A read-only npm production-dependency audit reported zero critical and nine high
advisories (Axios, Vue/server-renderer and related transitive packages). The full
install also reported development/build-tool advisories. This repair retains the
locked graph; a separate dependency-maintenance pass should assess applicable
advisories and retest upgrades. The audit is not a live exploitability certification.
See the live handoff for the corrected candidate's complete gates and rollout status.

## Data-mount bootstrap packaging gate

The next full matrix passed 2,437 tests per Python version, but an empty-storage
production-image smoke found that the `/app/db` data mount masked the image's
`db/createTable.py`. Container migrations now use image-owned
`python -m myUtils.db_bootstrap`. Source checkouts reuse the canonical bootstrap;
Docker copies that implementation outside the mutable data mount. The legacy
host CLI and the database location remain compatible.

The packaging correction passed 148 focused tests (three platform-dependent
skips) and a 438-file static scan. A non-release preview image passed a
network-disabled, non-root web/worker smoke with empty synthetic storage:
bootstrap ownership, paused/active readiness, bearer access and anonymous
rejection, Chromium startup, matched image/revision, and web restart independent
of the worker. No targets were created and the synthetic containers were removed.
The final candidate must repeat the full gates and image checks after this change.
