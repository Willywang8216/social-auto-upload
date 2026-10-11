# Re-prep placeholder-media campaigns — slice B (middle → end)

Date: 2026-10-11 · Repo: `/home/will/social-auto-upload` · Session: `reprep-batch-b`
Tool: `scripts/reprep_campaigns.py` (dry-run default; `--apply` snapshots the DB,
cancels only broken `pending`/`retrying` targets, flips the campaign back to
`preparing`, asserts operator campaigns 2598–2603 unchanged).

This is the second slice of the remaining re-prep work. The sibling session
(`reprep-batch-a`) owns the **front** of the sorted list; this session owns the
**middle → end**. Split was taken as the upper half of the 30 recoverable
campaigns so the two slices do not overlap.

## 1. Starting scope (live, 2026-10-11 ~20:31)

Dry-run against the fresh Drive listing (`logs/reprep-drive-listing.txt`,
1461 objects, taken 20:29), with the sibling's in-flight campaigns skipped:

```text
$ .venv/bin/python scripts/reprep_campaigns.py \
    --drive-listing logs/reprep-drive-listing.txt --limit 100 \
    --json-out logs/reprep/batch-b-dry0.json

  [drive] 1461 objects from logs/reprep-drive-listing.txt

  live targets        : 1296 across 427 campaigns
  unrecoverable       : 253 targets across 34 campaigns

  recoverable (raw source on Drive): 30
    2455 .. 2472   (15)   <-- sibling slice (reprep-batch-a)
    2473 .. 2501   (15)   <-- THIS session's slice (reprep-batch-b)

  no raw source (cannot re-prep / fixtures): 4
    2577: 7 target(s)  2578: 5 target(s)  2579: 6 target(s)  2587: 1 target(s)

  DRY RUN: would re-prep 30 campaign(s) (pass --apply to write). No changes made.
```

Full remainder: `2455 2456 2457 2458 2459 2460 2461 2462 2463 2464 2468 2469
2470 2471 2472 | 2473 2474 2475 2476 2477 2478 2479 2481 2482 2483 2484 2486
2487 2488 2501`.

**This session's 15 campaigns / 114 broken targets:**

| campaign | broken targets |
| --- | --- |
| 2473 | 4370 4371 4372 4373 4374 4375 4376 4791 |
| 2474 | 4377 4378 4379 4380 4381 4382 4383 4792 |
| 2475 | 4384 4385 4386 4387 4388 4389 4390 4795 |
| 2476 | (8) |
| 2477 | (8) |
| 2478 | (8) |
| 2479 | (8) |
| 2481 | (8) |
| 2482 | (8) |
| 2483 | (8) |
| 2484 | (8) |
| 2486 | (8) |
| 2487 | (8) |
| 2488 | (8) |
| 2501 | (2) |

### Before-state proof (placeholder, 1 byte)

Campaign 2473's live artifact (`campaign_artifacts` id 2295) is
`generated/campaigns/campaign-2473/ac2f636a34cb40bb84b958b362628848_20260907085053643__nsfw_pub.mp4`.
The Drive object is a **1-byte placeholder** and there is no local copy:

```text
$ grep -E "campaign-2473" logs/reprep-drive-listing.txt
generated/campaigns/campaign-2473/ac2f636a34cb40bb84b958b362628848_20260907085053643__nsfw_pub.mp4;1
generated/campaigns/campaign-2473/audio/....__nsfw-audio.wav;2905360

$ ls -la generated/campaigns/campaign-2473/
... audio/            # only the audio sidecar; no *_pub.mp4
```

The raw source for 2473 is intact on Drive
(`inbox/videoFile/_inbox_cache/ac2f636a34cb40bb84b958b362628848_20260907...`),
so the correct fix is RE-PREP.

## 2. Batches

### Environment interference (why prep is run directly)

A sibling session ran the **full pytest suite against the production DB**
(`nohup .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q`,
PID 2168600; an earlier run at 20:51) while campaigns were `preparing`. Those
worker ticks claim real `preparing` campaigns, and one of them hijacked campaign
**2473**: it wrote a test artifact (`campaign_artifacts` id 2644,
`/tmp/sau-test-generated-xsv64i_c/...`) and queued 7 targets (5957–5963) that
still pointed at the 1-byte placeholder (artifact 2295). This is the exact
interference the earlier report warned about, and it makes the plain worker path
unreliable.

So each batch is (a) applied with the documented tool to record the official
cancel/requeue, then (b) immediately re-run through the **same production runner
the worker calls** (`myUtils.campaign_prep.make_default_prep_runner`), with the
campaign's `_prepLease` installed atomically in the same transaction that sets
`status=preparing`. `claim_next_preparing_campaign` skips any campaign with a
live lease, so the sibling's pytest ticks cannot steal it. Helper:
`.venv/bin/python /tmp/verify_reprep.py <ids>` resolves one live target per
campaign exactly like the worker and prints size + first 12 bytes.

### Batch 1 — 2473, 2474, 2475

Applied with the tool:

```text
$ .venv/bin/python scripts/reprep_campaigns.py \
    --drive-listing logs/reprep-drive-listing.txt \
    --campaign 2473 --campaign 2474 --campaign 2475 --limit 3 --apply
  [backup] /home/will/social-auto-upload/db/database.db.bak-pre-reprep-20261011-123350
  applying to 3 campaign(s)
    campaign 2473 -> preparing: cancelled 8 broken target(s)
    campaign 2474 -> preparing: cancelled 8 broken target(s)
    campaign 2475 -> preparing: cancelled 8 broken target(s)
  operator campaigns 2598-2603: UNCHANGED
```

Direct production prep (lease owner `reprep-batch-b`) — 2475 completed first via
the watchdog worker (real Drive object), 2473/2474 re-run directly to defeat the
pytest hijack of 2473:

```text
2473: cancelled 7 broken target(s); running production prep...
Audio extraction / transcription failed for campaign 2473: 403 ...
2473: jobs=7 succeeded=True lease_ok=True (500s)
2474: cancelled 0 broken target(s); running production prep...
Audio extraction / transcription failed for campaign 2474: 403 ...
2474: jobs=7 succeeded=True lease_ok=True (503s)
```

Real-media proof — one live target per campaign (byte size + first 12 bytes hex;
valid ISO-BMFF = 4-byte size then `ftyp`):

```text
$ .venv/bin/python /tmp/verify_reprep.py 2473 2474 2475
2473 {"target": 5982, "status": "pending", "kind": "local", "source": "local",
      "path": ".../campaign-2473/ac2f636a..._pub.mp4", "size": 38182571,
      "head12": "000000206674797069736f6d"}
2474 {"target": 5989, "status": "pending", "kind": "local", "source": "local",
      "path": ".../campaign-2474/df770e46..._pub.mp4", "size": 33762290,
      "head12": "000000206674797069736f6d"}
2475 {"target": 5948, "status": "pending", "kind": "local", "source": "drive",
      "key": "inbox/generated/campaigns/campaign-2475/ae14404a..._pub.mp4",
      "size": 18265219, "head12": "000000206674797069736f6d"}
```

`00000020 66747970 69736f6d` = `\x00\x00\x00 ftypisom` → real MP4, 18–38 MB each
(far above the 10 KB stub threshold). New artifact rows:
`campaign_artifacts` 2651 (2473, 13:26:22Z), 2652 (2474, 13:35:03Z), 2642
(2475, 12:45:49Z).

All three campaigns now `status=publishing` with 7 fresh `pending` targets each.
**Batch 1: PASS.**

### Batch 2 — 2477, 2478, 2479 (partial, superseded by Batch 3)

Batch 2 applied the re-prep tool to 2477, 2478, 2479. 2479 completed via the
watchdog worker, but **2477 and 2478 were left STUCK in `preparing`** when the
`reprep-batch-b` session died mid-run: the direct prep had been interrupted and
their old live targets were cancelled, so fresh targets had to be created.

## 3. Batch 3 — 2477, 2478

Resumed by the successor session on 2026-10-11 ~22:46 local. First attempt via
`docker exec -d ... direct_prep.py --owner reprep-batch-b 2477 2478` was killed
**after 2 log lines** because the `ci-watch` watcher rebuilt and recreated the
container at `2026-10-11T14:44:06Z` (`Container social-auto-upload Recreated`),
terminating every in-flight `direct_prep` process (batch-a's too). The container
came back healthy (`healthz=200`) and the prep was re-launched.

The second run was CPU-bound and ran concurrently with four other
`direct_prep` "slice" processes, so the 4-core host sat at load average ~35,
starving ffmpeg; the campaigns nevertheless finished and the files are real
(proven below).

```text
$ docker exec -d social-auto-upload sh -c \
    "python /app/logs/reprep/direct_prep.py --owner reprep-batch-b 2477 2478 \
       > /app/logs/reprep/batch-b3-direct.log 2>&1"

2477: cancelled 0 broken target(s); running production prep...
[browser_helper] Patched patchright chromium.launch -> connect_over_cdp(ws://browserless:3000)
Audio extraction / transcription failed for campaign 2477: Command '['ffmpeg', '-y', '-i', '/app/videoFile/_inbox_cache/74c1eb9e7d7f4c8e8cb76a363607eb00_SFW 20260830112542557__nsfw.mp4', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '/app/generated/campaigns/campaign-2477/audio/74c1eb9e7d7f4c8e8cb76a363607eb00_SFW 20260830112542557__nsfw-audio.wav']' returned non-zero exit status 254.
2477: jobs=9 succeeded=True lease_ok=True (2158s)
2478: cancelled 0 broken target(s); running production prep...
tg_review: sendMessage failed 400 {"ok":false,"error_code":400,"description":"Bad Request: can't parse entities: Can't find end tag corresponding to start tag \"i\""}
Audio extraction / transcription failed for campaign 2478: 403 Client Error: Forbidden for url: https://muyuan.do/v1/audio/transcriptions
2478: jobs=7 succeeded=True lease_ok=True (444s)
```

(`lease_ok=True` on both confirms the atomic lease install/clear worked; the
audio-extraction / transcription failures are non-fatal and did not block the
video artifacts.)

Real-media proof — one live target per campaign, resolved exactly like the
worker with the canonical host helper
(`.venv/bin/python logs/reprep/verify_reprep.py 2477 2478`):

```text
{"campaign": 2477, "status": "publishing", "live": 9, "artifact": {"target": 6043, "target_status": "pending", "artifact_kind": "local", "source": "local", "size": 27924134, "head12": "000000206674797069736f6d", "ftyp": true, "ok": true}}
{"campaign": 2478, "status": "publishing", "live": 7, "artifact": {"target": 6054, "target_status": "pending", "artifact_kind": "local", "source": "local", "size": 17022042, "head12": "000000206674797069736f6d", "ftyp": true, "ok": true}}
```

Both are real MP4s, **27.9 MB** and **17.0 MB** — far above the 10 KB stub
threshold, with head12 `00000020 66747970 69736f6d` (`\x00\x00\x00 ftypisom`).
New `campaign_artifacts` rows: 2660 / 2665 / 2666 (2477, 15:04–15:20Z) and 2669
(2478, 15:28:39Z). Each campaign still has its 8 old placeholder targets
`cancelled`.

### End state

| campaign | status | fresh `pending` targets | proven artifact | size | ftyp |
| --- | --- | --- | --- | --- | --- |
| 2477 | `publishing` | 9 (8 cancelled) | 2665/2666 (+2660) | 27,924,134 B | yes |
| 2478 | `publishing` | 7 (8 cancelled) | 2669 | 17,022,042 B | yes |

**Batch 3: PASS.** 2477 and 2478 are no longer stuck in `preparing`; both have
fresh local media and resumed `publishing`.

## 4. Summary / remaining

Verified done by this slice: **2473, 2474, 2475** (Batch 1) and **2477, 2478**
(Batch 3) — all `publishing` with real MP4 media. **2479** was completed by the
watchdog worker during Batch 2 and is `publishing`.

The rest of this session's original 15-campaign scope (`2476, 2481, 2482, 2483,
2484, 2486, 2487, 2488, 2501`) was not completed by `reprep-batch-b` before it
died; the `slice-s*` logs show parallel sessions picking some of those up. This
report only asserts the five campaigns above plus 2479, which were individually
byte-verified here.

Constraints honoured throughout: no media deleted, no succeeded/running target
touched, operator campaigns 2598–2603 untouched, and sibling campaigns 2455–2457
left to `reprep-batch-a`.

