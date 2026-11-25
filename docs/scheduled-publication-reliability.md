# Scheduled publication reliability

This change repairs prerequisite acquisition and the publish-center projection. It does not approve drafts, reset attempts, or turn submission into delivery confirmation.

## Failure mechanisms

- Configured Telegram MTProto, Bluesky app-password and GitHub blog routes could not acquire destination evidence through the automatic verifier. The verifier now checks the current exact sender, destination and effective grants without publishing or mutating credentials.
- Default environment identities and credentials were not represented consistently in the destination fingerprint. Effective configuration is now fingerprint-bound; configuration changes invalidate old evidence.
- A paced, exact registered Drive metadata lookup can exceed 20 seconds. A single probe now has 45 seconds within the unchanged 240-second aggregate restore bound, 90-second download bound and 512 MiB media cap.
- A public alternative without a trustworthy checksum incorrectly became permanent corruption and could mask the exact backend alternative. Evidence acquisition is now a finite retryable verification hold. No download is permitted without trustworthy checksum evidence. Known checksum, size and media conflicts remain permanent.
- Cached destination cooldowns could spend another prerequisite attempt. Cached waits no longer spend an attempt, and completed grant denial requires configuration/review rather than a transport retry loop.
- Destination evidence could expire between preparation and complete-group installation. Installation refreshes missing/expired evidence outside queue writer locks, then retains current source, approved intent, provider, member order, schedule, slot and atomic installation fences.
- Entity rollups and calendars could hide blocked/awaiting-delivery work or pending siblings after partial failures. Projection now preserves per-target lifecycle states, bounded pagination, authoritative cancellation snapshots and usable legacy details.

## Boundaries preserved

Telegram post-level chat overrides cannot borrow proof for a different configured chat. Bot sender IDs and GitHub's effective branch are checked. GitHub credentials without independently verifiable write authority remain unverified.

Readiness and provider receipts are distinct. UI diagnostics use allowlisted/redacted fields. Public projections are authoritative, including empty summaries; only legacy responses without a public projection may read the restricted readiness portion of `checkpoint_json`. Raw provider requests/responses, config, signed URLs and lease capabilities are never rendered.

Recovery must use the maintained reviewed-recovery and complete-group APIs with exact approved originals, fresh grants/media evidence, duplicate checks, attempt budgets, schedule intent, leases and receipt reconciliation. Never release a multipart tail, refund attempts, replay uncertain delivery, change approved copy/media, or reenable an operator-disabled destination. Future reviewed preparations and predecessor-held chains remain unchanged.

## Verification and rollout

Focused Linux regressions cover native destination grants, fingerprint drift, Telegram overrides, cached waits, media evidence/timeouts, complete-group refresh, entity states and HTTP projections. Frontend regressions cover pagination, partial calendar siblings, cancellation, legacy details and private diagnostic exclusions.

A deploy additionally requires the maintained full frontend and Linux Python 3.12/3.13 receipts, source/image binding, non-root smoke, CVE policy and native pause/drain/rollback controls. Focused tests or a successful image build are not substitutes for those gates. See [ci-watch.md](ci-watch.md) and [vps-operations-layout.md](vps-operations-layout.md).
