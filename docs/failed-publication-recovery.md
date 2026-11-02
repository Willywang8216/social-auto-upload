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

A saved receipt continues with its original provider, regardless of a later
configuration change. A Sociamonials receipt does not require a valid direct
Threads/Meta token to be polled. A direct Meta receipt is not permission to create
a second post through Sociamonials. Account enablement, ownership, media, language
and content-policy checks still apply.

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
