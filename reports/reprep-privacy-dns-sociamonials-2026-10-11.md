# Re-prep, repo privacy, DNS purge, and progressive Sociamonials

Date: 2026-10-11 · all work verified against the live system

---

## 1. Campaign re-prep — the core problem, FIXED

### What was actually wrong

Two separate faults, and the first was the reason re-prep appeared impossible.

**Fault 1 — the scheduler never woke for a campaign awaiting prep.**

`_start_worker_drain_thread()` runs `drain()`, and `drain()` is what runs the prep
tick (`_prep_tick`, `myUtils/worker.py:824`). The scheduler loop only called it
when `has_claimable_targets()` was true — i.e. when a **scheduled publish** was
due. A campaign awaiting prep has no due target (its posts do not exist yet), so
it was never prepped until some unrelated target happened to come due.

Observed live: campaign 2503 sat in `preparing` with the worker **completely
silent** — zero log lines in 46 minutes.

Fixed in `sau_backend.py` (`7518616`): the loop now also wakes on
`campaign_store.has_claimable_preparing_campaigns()`, the same helper the worker's
own drain guard uses.

**Fault 2 — pre-async-prep campaigns have no stored `prepRequest`.**

Re-prep then failed with:

```
prep failed: campaign 2503 has no persisted prep request ('prepRequest')
```

Everything needed is still on the campaign and its media group, so
`scripts/reprep_campaigns.py` reconstructs it: `brief` from the campaign's own
`notes`, `mediaFilePaths` from `media_group_items` joined to `file_records`.

### The detector had to be exactly right, in both directions

Two wrong answers were found and discarded before the right one:

* **Too strict** (first attempt): treated a `getFile` URL returning 404 as broken.
  That flagged campaigns **2600–2603 — the live Taipei Pride and news
  campaigns** — which restore perfectly from Drive (verified: 46,375,033 bytes,
  valid `ftypisom`). Re-prepping them would have destroyed queued posts.
* **Too loose** (second attempt): accepted any `file_records` route as proof. But
  356 targets have a *valid route whose Drive object is a 1–4 byte placeholder*.

The correct test is the object's **size**: a Drive object ≥ 1024 B is media, below
that it is a stub. With that, the scope is:

```
live targets            : 1293 across 428 campaigns
unrecoverable           :  367 targets across 49 campaigns
recoverable (raw source on Drive): 45 campaigns   <- re-prep these
no raw source (fixtures)          :  4 campaigns   <- cannot re-prep
```

**Proof it works:** campaign 2446 re-prepped and produced new artifacts of
**33,694,792 B and 25,384,640 B**, both with valid ISO-BMFF (`ftypisom`) headers —
real media, not stubs.

---

## 2. Repo privacy — BLOCKED, and correctly not forced

**GitHub refuses to make a public fork private:**

```
422 Public forks can't be made private
```

The only supported route is a **permanent "Leave fork network"** step, which
discards PR/issue/wiki/stars/watchers metadata. Per the standing instruction not
to take irreversible steps, **this was not done** — the operator must choose.

**Upstream is already exactly as requested:**

```
origin    git@github.com:Willywang8216/social-auto-upload.git   (fetch + push)
upstream  https://github.com/dreammis/social-auto-upload.git    (fetch; push DISABLED)
```

So the original upstream is tracked read-only, and our own line of work lives on
`origin`. Documented in the report with the ahead/behind numbers.

**Options for the operator:** (a) leave it public, (b) "Leave fork network" then
make private (accepting the metadata loss), or (c) create a fresh private repo and
push to it, keeping the fork as a read-only upstream mirror.

---

## 3. DNS purge — done, with the one dangerous move refused

| item | verdict | action |
| --- | --- | --- |
| DNS account **id 1** `Cloudflare-ryanbossomhot` | **proven broken** (token verify `401`, zones `403`) | **NOT removed** — SSL id 6 (`iamwillywang.com`) uses it, so removal would break live DNS. Operator must paste a valid token. |
| DNS account id 2 `Cloudflare-forsub` | works (`200 active`, 3 zones) | used to verify/repair DNS |
| 8 orphan website rows (no DNS + no vhost) | stale | **7 removed**, 1 kept (a running app) |
| 2 stale `website_domains` rows for already-deleted sites | orphaned | removed |
| `stage.sexualwill.com` (DNS but no vhost, HTTP **525**) | should not exist — the NotionNext deployment is gone | **Cloudflare A record removed** + stale panel row removed |

Panel DB backed up first
(`backups/1panel/1Panel.db.bak-pre-dnspurge-20261011-175201`), the deleted
Cloudflare record archived to `reports/stage-dns-record-backup.json`, and every
DELETE guarded with `NOT EXISTS` so it could not remove an in-use row. Post-change:
panel `active`, `GET :25533` 200, `integrity_check` `ok`, live sites still serve.

---

## 4. Progressive Sociamonials — implemented, and the loop is closed

### The allowance is discoverable after all

`GET /api/v1/workspaces/{id}/context` returns the storage block the earlier work
could not find:

```
allowance_bytes   4000000000     enforcement: true
usage_bytes       3225794427     percent_used: 80.6449
available_bytes    774205573     pooled: true
max_upload_bytes  {image: 15728640, video: 1073741824}
```

### The rule, and why it is safe

Migration `0022` adds `sociamonials_delivered_assets`: when a target transitions
to `succeeded`, the worker records the `asset_id` the fallback attached. So a
sweep knows the post genuinely **delivered** — not merely accepted — and may
delete the asset once no `pending`/`retrying`/`running` target needs it.

It is **additive and fails safe**: with no recorded delivery it keeps every asset.
`scripts/sociamonials_reclaim.py` never touches a starred asset, one a live target
needs, or one whose post has not delivered.

### Applied and proven

```
before:  used 3.23 GB / 4.00 GB   774 MB free  (80.6%)
apply :  --max-bytes 700000000  ->  13 deleted, 0 skipped
after :  used 2.53 GB / 4.00 GB  1473 MB free  (63.2%)
current: used 2.55 GB            1445 MB free
```

Then the previously quota-blocked X target **5494 published** (`post_id
10729773`), and **the asset it uploaded is itself now listed as reclaimable** —
which is the operator's rule working end to end: publish one, free it, add the
next.

---

## 5. State

| | |
| --- | --- |
| Succeeded targets | **1134** (was 1076 at the start of the day) |
| Tests | **1646 passed, 1 skipped** |
| All six operator campaigns | **intact** — Pride 2026-10-24, news 2026-10-12 |
| Sociamonials | 2.55 GB / 4.00 GB, 1.45 GB free |
| Disk | 73% |
| Deployed | `989453f`, healthz 200 |

## Outstanding

1. **DNS account id 1** — paste a valid Cloudflare API token (id 6 cannot be
   re-signed until then).
2. **Repo privacy** — choose one of the three options in §2.
3. **Re-prep** — the 45 recoverable campaigns are being processed; the 4 with no
   raw source are fixtures and cannot be recovered.
4. **X API credits** and **account 108 OAuth** for direct publishing.
5. **Re-enable GitHub Actions** in the repo's Actions tab (fork-usage disable is
   not API-clearable); `ci-watch.sh` covers deploys meanwhile.
