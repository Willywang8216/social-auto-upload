# Failed-publication recovery

This is an explicit operator recovery workflow, not a blanket retry loop. The
worker's ordinary lease, delivery-checkpoint and finite retry rules remain in
force. A failed database label alone must not erase a provider receipt.

## Before resuming

1. Freeze the intended failed target IDs and take an online-consistent SQLite
   backup; verify its checksum and an isolated restore. Keep production data and
   credentials on the server.
2. Inspect the original failure, the target's provider receipt/operation journal,
   current account identity, exact media and same-account publication history.
   Successful/cancelled intent and uncertain acceptance are not eligible for a
   blind retry. Retired Reddit publishing stays excluded.
3. Repair only established causes. A website login does not validate an API token.
   Use live read-only identity checks and existing configured integrations. Never
   guess replacement destinations or cross brands/workspaces/languages.
4. An obsolete payload can use an existing artifact only when its campaign,
   source identity, variant, part index/count and media integrity match. Do not
   substitute a similarly named file, a full video for one segment, or an adult
   artifact for a censored variant.

## Explicit managed route

An account may deliberately use an already verified Sociamonials connection
instead of its direct API credentials:

```json
{
  "publishingProvider": "sociamonials",
  "sociamonials": {
    "network": "thrd",
    "profile_refs": ["VERIFIED_PROFILE_REF"],
    "workspace_id": "VERIFIED_WORKSPACE_ID"
  }
}
```

These are placeholders, not usable destinations. Obtain the exact destination
from the connected-profile listing and confirm the intended brand/account. A
primary managed route requires one explicit, matching-platform destination;
missing/invalid routes block rather than silently fall back to a different
account. Teaching uses its separate workspace and API-key configuration.
`SAU_SOCIAMONIALS_FALLBACK` must remain enabled for this route to execute.

Authorization requires the current exact workspace permission map and a fresh
`GET /api/v1/workspaces/{workspaceId}/social-profiles` response containing exactly
one matching network/profile reference, `connected=true`, literal
`agent_can_publish_to=true`, no unavailable reason, and the required capabilities.
The identity endpoint's assignment summary may be empty or absent; this is never
a wildcard grant and cannot substitute for the per-profile decision. See the
[provider reference](https://www.sociamonials.com/developers.php). Standalone text
requires the `text` capability; an image/video caption uses its media capability
and must still preserve approved text and pass the provider's strict intent and
caption validation.

A saved receipt continues with its original provider, regardless of a later
configuration change. A Sociamonials receipt does not require a valid direct
Threads/Meta token to be polled. A direct Meta receipt is not permission to create
a second post through Sociamonials. Account enablement, ownership, media, language
and content-policy checks still apply.

## Preprocess before scheduling

Use `scripts/preprocess_failed_targets.py` for repaired media and complete ordered
parts. With neither `--prepare` nor `--apply`, it is an offline, read-only queue
dry-run. An explicit frozen selection avoids silently expanding the batch to
new failures that appeared during implementation.

```bash
# A server-side, private inventory supplies {"target_ids": [123, 456]}.
python scripts/preprocess_failed_targets.py --selection-file /private/frozen.json

# Restore/encode/validate all selected eligible targets; no queue writes.
python scripts/preprocess_failed_targets.py --selection-file /private/frozen.json \
  --prepare --out-dir /private/prepared --plan-file /private/prepared-plan.json

# Consume the exact saved plan; revalidate and atomically queue every part.
python scripts/preprocess_failed_targets.py --apply \
  --plan-file /private/prepared-plan.json
```

Paths and IDs above are placeholders. Plans/reports stay on the server, use
exclusive 0600 file creation, contain fingerprints rather than account secrets,
and are not committed or copied into test fixtures. `--prepare` isolates each
target's failure so a bad file cannot suppress unrelated safe targets. Neither
phase publishes. Encoding/probing/final decode validation occur outside queue
write transactions; application performs only guarded SQL, policy and hash checks.

Use `--backup-account-id ID` only for a currently verified effective destination;
fresh grants/storage constraints tighten the common preparation profile. An
explicit managed primary also requires that verification. This does not authorize
a new account, a guessed mapping, or a different workspace's credential. Lower
current entitlements and unsupported original options must stop before queueing.

All final video/image outputs carry immutable checksums and complete ordered
part/group metadata. Recovery preserves approved text, links, privacy/options,
variant, framing, alt text and covers. A checksum-verified older prepared part
may create a new generation while retaining its original source interval. It
cannot overwrite scheduled/published artifacts, drop tails, or become text-only.

When an artifact supplies top-level `checksum` or `sha256`, preprocessing binds
those fields to the final output bytes too. Original source/input hashes remain
in provenance metadata; retaining them as output checksums would incorrectly
reject a valid derivative or identify different ordered parts as the same media.

Only after **all** preparation completes are future slots allocated. Production's
default is **three posts per canonical destination per day with a 30-minute
minimum gap**. A requested cap may tighten, never raise or disable the configured
cap. Shared native/managed aliases, confirmed delivery history, existing bookings,
and unresolved/in-flight work consume capacity. Do not force an early canary past
pacing merely to claim a delivered result.

Application revalidates target/intent/account fingerprints, current route/policy,
all final hashes, receipt/duplicate state and every reserved slot under the guarded
write transaction. Every part for one original target is installed **atomically**.
The original job intent and previous failure/checkpoint evidence remain recorded;
tail parts have unique identities and the recovery generation is durable. A stale
plan or second application cannot reset/duplicate it. Failed campaigns resume only
after their exact recovery validates; cancelled/superseded campaigns do not.

Successful, cancelled, duplicate, deleted-account, disabled, retired, uncertain,
receipt-bearing and irreparable targets remain unchanged. `--resolved-target-id`
requires evidence of an actual repair, not an assumption that a fresh web login
fixed an API credential. Unsupported backup copy/settings stay exceptions.

## Explicit review of resolved prerequisite blocks

The ordinary retry/preprocessing APIs remain failed-only. After the operator
approves compliant representations and the exact frozen recovery scope, use the
separate `scripts/recover_reviewed_publications.py` boundary for maintained
pre-submission prerequisite blocks. Configuration, permission, content-policy,
cancelled/review/running states, leases and retained/unknown provider effects are
never overridden. Missing media must be restored and verified first; rate-limit
errors do not prove permanent loss or authorize a purge.

```bash
# IDs/paths are placeholders. Repeat the explicit review and SAME IDs at apply.
python scripts/recover_reviewed_publications.py --review-resolved-prerequisites \
  --target-id 123 --target-id 456 --prepare \
  --out-dir /app/generated/PRIVATE_TASK_ID --plan-file /private/reviewed-plan.json
python scripts/recover_reviewed_publications.py --review-resolved-prerequisites \
  --target-id 123 --target-id 456 --apply --plan-file /private/reviewed-plan.json
```

A JSON manifest cannot grant review authority. All declared original siblings
must be included in the frozen selection; unrelated/confirmed posts are not
resent. A legacy selected clip without absolute raw offsets may be represented
using **measured local coordinates of that exact approved file**, retaining its
checksum, source-file identity and original parent membership. Absolute raw
offsets and an unknown raw-source checksum remain unknown. Never substitute the
whole raw video for that selected clip.

If an old payload has no checksum field, reviewed preparation may bind the
SHA256/byte count persisted by verified restoration on that **exact campaign
artifact**. Its campaign, path, kind, source-file membership and other provenance
must still match the approved payload. The full row is fingerprinted and re-read
under the install transaction; missing/conflicting facts refuse recovery. A raw
source-file checksum is never borrowed for a derivative.

Fresh destination/grant/option/byte/provenance/budget/duplicate/slot checks precede
the same-transaction fingerprint/CAS. Each complete original group is installed
atomically. Active provider/prerequisite counters and the full old checkpoint,
errors, status, scheduling and dispatch history survive. New ordered tail targets
inherit the original counters rather than creating artificial zero-attempt credit.
Later parts require the predecessor's **confirmed** provider receipt, not merely
a queued/submitted/succeeded-without-receipt label. Receipt polling still drains.

rclone exact reads use one bounded subprocess, one retry/low-level attempt, a
1-request/second limiter with burst 1 and a 2-second Drive pacer with its separate
burst disabled. Pacing and I/O share the hard wall-clock deadline; the normal
probe deadline remains unchanged. Explicit maintenance may use a probe deadline
up to 45 seconds, still within the overall restore deadline and existing live
cooldown/admission/integrity/CAS guards. No account/client/credential rotation occurs.

## Opt-in repair of unstarted pending targets

The default preprocessing selection remains **failed-only**. A separately frozen
pending audit may opt in with `--include-unstarted-pending`, together with explicit
`--target-id` values or a private `--selection-file`. Pending application must
repeat that opt-in and the same selection; the plain failed-recovery utility
cannot requeue or repair pending targets.

```bash
# Inspect only the frozen pending selection; already-valid media is a no-op.
python scripts/preprocess_failed_targets.py --include-unstarted-pending \
  --selection-file /private/pending-selection.json

# Prepare persistent, worker-visible media; keep the saved plan private.
python scripts/preprocess_failed_targets.py --include-unstarted-pending \
  --selection-file /private/pending-selection.json --prepare \
  --out-dir /app/generated/PRIVATE_TASK_ID \
  --plan-file /private/pending-prepared-plan.json

python scripts/preprocess_failed_targets.py --include-unstarted-pending \
  --selection-file /private/pending-selection.json --apply \
  --plan-file /private/pending-prepared-plan.json
```

These paths are placeholders. A Docker worker must see the final media through a
persistent runtime mount; `/app/backups` is not a media mount. Never put private
plans, credentials or row snapshots in watched inbox/media directories.

Eligibility requires status `pending`, zero attempts, no start/finish timestamps,
lease, retry history, checkpoint, receipt, delivery or earlier recovery history,
and an unchanged frozen intent. Already-valid targets keep their generation and
schedule. Invalid targets require every ordered part to validate before one atomic
installation; a compatible future schedule is retained. Fresh identity, route,
duplicate, reservation and slot checks still apply. Changed or uncertain targets
remain unchanged rather than being forced through a failed-to-pending reset.

## Dry-run and guarded application

The recovery utility defaults to a dry run. Explicit IDs cover the selected
historical targets rather than silently restricting them to the default lookback.

```bash
python scripts/recover_failed_targets.py --target-id 123 --target-id 456
python scripts/recover_failed_targets.py --target-id 123 --target-id 456 --apply
```

Use `--resolved-target-id ID` only after the historical cause has actually been
fixed. It bypasses the historical permanent-error regex, **not** account identity,
policy, current state, duplicate, receipt or ownership safeguards.

Plans contain a fingerprint of the observed target, intent, account configuration,
media identity and route. Application revalidates under the same write transaction
as the failed-to-pending change. A changed account/token, superseded campaign,
newly published duplicate, active lease or stale plan refuses recovery. Planning
never creates a missing database. Target-specific media sets use exact references,
artifact IDs or recorded SHA256, never lossy filename stems.

A validated recovery receives a durable `recovery_generation`. Sociamonials uses
that generation in its idempotency key: an operator-approved new recovery no longer
gets stuck replaying an old failed provider post, while transport retries within
the recovery retain the same key. Reapplying a plan cannot reset a target again.
Posts with declared media cannot silently become text-only posts when attachment
fails.

Targets with receipts or partial operations require separate, evidence-backed
reconciliation before this utility can consider them. Preserve the original row,
receipt and provider result in private recovery evidence. A provider-confirmed
terminal failure is different from an unknown response or an approval hold; do not
clear all checkpoints with SQL just to make a batch pass.

## Diagnostics and observation

Meta errors retain error code, subcode, safe user diagnostics, trace ID and the
explicit `is_transient` flag. An explicit transient HTTP 400 may retry with backoff;
revoked-token code 190 remains non-retryable. Terminal media-container states are
recorded with non-delivery diagnostics instead of only an opaque HTTP string.

Place recoveries into collision-free, paced slots using the existing allocator.
Respect existing schedules and account/day limits. Observe the first eligible
recoveries, retain their receipts, and distinguish queued, accepted, awaiting
provider delivery, delivered and failed results. Do not report an accepted or
scheduled post as delivered. Record unavailable accounts, policy/content problems,
ambiguous history and irreparable media as explicit exceptions rather than
repeating them indefinitely.

## Durable operator notification

Terminal failed, blocked and needs-review transitions enqueue their stop event in
the **same database transaction**. A failed enqueue rolls back the transition.
Delivery occurs outside that transaction through the configured operator channel,
Telegram by default when configured. Events are deduplicated by target/recovery
generation, retain sent receipts, and independently retry unsent notifications
after restart. At-least-once delivery can resend after a lost acknowledgement but
can never resubmit the publication. No blanket historical alert backfill runs.

The notice includes occurrence time, account/platform, target/job/part, separate
native and backup outcomes, redacted reasons, a UI link and the safe next action.
Confirmed failure, unavailable backup, partial delivery and approval/manual review
are labelled separately. Notification transport failures do not disable unrelated
worker maintenance. After a gated deployment, send a clearly labelled real channel
test and verify its provider acknowledgement, not a fixture alert.

## Release prerequisites

Use the existing clean-commit release helper and matching gate receipts: Linux
Python **3.12 and 3.13**, pinned uv/Node, all backend tests, frontend tests/build,
and the exact image/runtime checks. Tests use isolated databases/media and mocked
external services without production credentials. Drain via the release-owned
pause and SIGTERM; never escalate over active work. Verify an online SQLite backup
and isolated restore, apply additive Alembic migrations with writers stopped, then
verify matched web/worker images, schema and held readiness. Database rollback over
newer delivery receipts is not allowed. See [release gates](ci-watch.md).

## Shared reservations, ordered parts, and confirmed TikTok failure

Normal synchronous/asynchronous finalizers and both guarded recovery applications
take the same database-side slot reservation **before** queue writes. A busy or
unavailable reservation refuses the operation; it never proceeds
unlocked. Recovery still revalidates its saved plan in the main write transaction,
and media probing/encoding and external capability reads stay outside it.

Every next part has the previous allocated part as its lower bound, including
midnight and daily-cap rollover. A tail cannot obtain an earlier free date merely
because its initial stagger crossed midnight. Cancelling a target does not erase
its accepted/unknown or confirmed delivery evidence from destination capacity.
Unreadable delivery history refuses allocation rather than becoming empty space.

A TikTok tracking receipt preserves its original mode and chosen visibility.
Public completion with no moderated public IDs remains pending; explicit private
completion is labelled non-public, and inbox handoff needs creator action. A
provider-confirmed `FAILED` result finalizes its failure and durable stop event
immediately while retaining the receipt, rather than polling that terminal result
for another 24 hours. It is not permission to create another native/backup post.

An already held release must retain its verified recovery bundle and pause
ownership. Do not clear a foreign pause to force deployment, or restore an older
database over newer receipts. Any explicit superseding rollout must first verify
the held seal, stop all writers gracefully under the deployment lock, preserve
the old hold evidence, and use the normal gated helper for the new held candidate.
