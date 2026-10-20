# New Drive uploads — transcribed, summarised, scheduled before Pride

Session: `gdrive-pipeline-metadata` · 2026-10-20 (host TZ Asia/Taipei) · repo
`/home/will/social-auto-upload` · inbox host `/home/will/sau-inbox` · Drive
`GDrive-willywang8216:SAU-Inbox`.

## 0. The short answers

| question | answer |
| --- | --- |
| Are new uploads scheduled right away? | **No.** Two reasons, both fixed below: (1) the inbox watcher was crash-looping since 2026-10-17, so nothing new reached `ready` at all; (2) even when it runs, a submission only gets the *next free slot per account*, and the queue is months deep (see §5). |
| Is the Pride invitation before 10/30? | **Yes, now.** 14 Pride targets that had been queued for 10/31–11/24 were swapped back into 10/22–10/30, and the new 光光 parade clip lands 2026-10-28. Zero Pride invitations remain after 10-30. |
| Transcribed + summarised + rich metadata? | **Yes** for all 5 new videos (chunked STT, human summary, per-platform copy generated from it) and 11 new photos (human-described frames). |
| Bugs found? | 8, two of them serious: **the pipeline was publishing the extracted STT audio instead of the clip** (7 posts already went out that way on 10-18/19, 357 more were queued), and the **inbox watcher had been crash-looping for three days** so nothing new was being queued at all. Plus `.docx` transcripts ignored, the offload deleting live-prep sources, duplicate `campaign_posts`, an 11-month queue horizon. Details in §6. |
| Stale files purged? | 6 byte-identical duplicate photos moved out of the inbox, 60 duplicate targets cancelled, 22 stale `ready` entries cleared, scratch dirs cleaned. Nothing deleted; everything moved to `sau/purged/2026-10-20-duplicates/` (§7). |

## 1. What the Drive inbox actually contained

New since the last session (`rclone lsl`, mtime):

| Drive path | size | mtime (CST) | duration | rating |
| --- | ---: | --- | ---: | --- |
| `sw/video/光光吸雞雞大家來遊行__nsfw.mp4` | 8.1 MB | 10-20 13:22 | 19.8 s | NSFW |
| `both/video/morning__nsfw.mp4` | 57.5 MB | 10-20 13:28 | 149.1 s | NSFW |
| `both/video/morning__sfw4` | 57.3 MB | 10-20 13:29 | 149.1 s | SFW (unitended ext) |
| `both/video/putting-my-life-out__sfw4` | 139.0 MB | 10-20 13:35 | 347.5 s | SFW (unitended ext) |
| `both/video/Nakedwill-news-20261016__sfw.mp4` (+ `.docx`) | 783.4 MB | 10-17 11:18 | 627.8 s | SFW |

Plus 17 images the watcher had never ingested (`sw/img`, `both/img`, uploaded
10-17 15:10–15:21).

**Two files arrived without a usable extension** (`…__sfw4`). Both are MP4
(`ftypisom…mp41` header verified) — apparently the upload dropped the `.mp4` and
left a stray `4`. Repaired with a server-side rename so the watcher can parse
them:

```
both/video/morning__sfw4            -> both/video/morning__sfw.mp4
both/video/putting-my-life-out__sfw4 -> both/video/putting-my-life-out__sfw.mp4
```

(The `.sfw4` name is also why they would have landed in `pending` with "檔名缺
<sfw|nsfw> 標籤" even after the PIL fix — the parser keys on the extension.)

## 2. Transcription

`_work/stt_local.py` — chunked chat-audio STT (`mimo-v2.6-flash`, 120 s windows,
3 retries per chunk), transcripts in `_work/transcripts/`.

* `morning` (149 s) — 3 chunks, 2 206 chars. The monologue about Greek/Roman
  sculpture vs a living body.
* `putting-my-life-out` (348 s) — 3 chunks, 5 809 chars. Four years after
  quitting the corporate/PhD track, and still lost.
* `Nakedwill-news` (628 s) — 6 chunks, 5 443 chars. Ep.006: hacked French
  supermarket fridges, IMF 3.1 %, Black Sea drones, Taiwan flu/COVID rollout,
  the STI test a urine screen misses.
* `光光…遊行` (20 s) — the whole-clip request is rejected by the gateway
  ("considered high risk"), so it was transcribed in 4 s windows. Reconstructed:
  「光光又來了，真的好色喔。天氣好好，記得大家十月三十號要來遊行喔！…分享超過一百個，
  就分享光光吸雞雞的樣子。」

**Operator-supplied `.docx`**: `Nakedwill-news-20261016__sfw.docx` held the
Ep.006 description plus ~20 source links. Until today the watcher ignored
`.docx` entirely (only `.txt`/`.md`/`.content.txt`), so that material never
reached the copy. Fixed (§6.2); the links went into the brief and the archive
note.

## 3. What was submitted

Rich metadata = `options.title` + a structured `brief` (summary, key facts,
hashtags, CTA, per-platform guidance), with `useLlm` on so each account writes in
its own audience language.

| clip | title | profileIds | requested `startAt` |
| --- | --- | --- | --- |
| 光光吸雞雞大家來遊行 (NSFW, zh) | 光光邀你十月三十號一起來遊行 | 3 | 2026-10-21T21:00 |
| morning NSFW (en) | Marble doesnt breathe | 1+3 | 2026-10-22T21:00 |
| morning SFW (en) | Marble doesnt breathe | 1+3 | 2026-10-23T21:00 |
| putting-my-life-out (en) | Four years after I quit | 1+3 | 2026-10-24T21:00 |
| Nakedwill News Ep.006 (en) | Nakedwill News Ep.006 - hacked fridges and the STI test you skipped | 1+3 | 2026-10-21T20:00 |

Campaigns **2967–2975**. The SFW `morning` cut was verified frame-by-frame to be
a genuine crop (head/torso, no genitals) rather than the NSFW take mislabelled —
full-frontal in the NSFW file, waist-up in the SFW file.

11 new photos (campaigns **2976–2992**), each with a human-written description of
the actual frame (night riverside boardwalk, webcam/bedroom shots, the cat),
`profileIds` 1+3 for `both/img` and 3 for `sw/img`.

## 4. Scheduling: what the allocator returned vs what was asked

Requested 2026-10-21/22/23/24. Actual, per account (UTC):

```
morning NSFW (2968, profile 1)
  telegram  NW TG 中文        2026-10-22T13:50
  bluesky   NW Bluesky ZH     2026-10-22T14:05
  nw_sw_blog NW Blog          2026-10-22T14:10
  bluesky   NW Bluesky EN     2026-11-06T13:30
  reddit    Nakedwill Reddit  2026-11-06T13:45
  telegram  NW TG 本人        2026-11-06T13:55
  twitter   NW X (nudeweiwei) 2027-04-11  (x2)
  twitter   NW X (model_will) 2027-04-15  (x2)
```

The X accounts are booked solid through April 2027, so the clip goes out this
week on Telegram/blog/Bluesky and in **six months** on X. This is not a bug in
the submit — it is the ≥30 min / ≤3-per-day allocator faithfully walking forward
through a queue that is far too deep (§5). The Pride clip did not get this
treatment: it was swapped into 10/28 instead of waiting for 11/14.

### Pride deadline fix (14 swaps)

`_work/fix_pride_schedule.py` — for each Pride *invitation* target queued after
10-30, swap its `schedule_at` with an evergreen target on the same account inside
2026-10-21…10-30. Because the times are permuted within one account, the set of
booked minutes (and hence the gap/cap invariants) does not change.

| account | Pride targets moved | new slots |
| --- | ---: | --- |
| `光光` (103) | 5 (incl. the new clip, was 11-14) | 10-28 13:35 → 10-30 13:00 |
| `sexualwill` (77) | 5 | 10-28 13:00 → 10-29 23:00 |
| `NW X (nudeweiwei)` (124) | 4 | 10-29 23:00 → 10-30 14:10 |

Result: **0** pending targets of campaigns 2601/2609–2618/2967 after 2026-10-31;
the new parade clip posts **2026-10-28 21:35 Taipei**, three days before the
10-31 parade.

Not touched: campaigns 2559/2560 (Taipei Stonewall *event coverage*) and 2661
(queer-space raid news). They match on the word "Pride" but are not dated
invitations, and their overflow (62 targets in Feb–Mar 2027) is a separate
capacity problem — see §6.5.

## 5. Why nothing was being scheduled at all (the actual outage)

`/etc/cron.d/sau-inbox-watch` runs `watch.py --once` **every minute** as `will`
with `/usr/bin/python3`. Since 2026-10-17 that interpreter had no Pillow, and the
nsfw thumbnail branch was the one code path without a try/except:

```
File "/home/will/sau-inbox/watch.py", line 550, in build_thumb
    thumb.make_titlecard(...)
ModuleNotFoundError: No module named 'PIL'
```

`build_thumb` is called inside the per-item loop **before** `save_state()`, so the
exception aborted `process_paths()` before any state was written. Every minute
for three days: download the first new file, crash, forget it. `watch.log` was
781 KB of nothing but this traceback. That is why the 10-17 and 10-20 uploads sat
untouched and no Telegram "已備好" ever fired.

Fixes (§6.1): guarded the nsfw branch, wrapped the whole call site, and installed
Pillow 13.0.0 for the system interpreter **without root** (the wheel unpacked into
`~/.local/lib/python3.14/site-packages`, which `/usr/bin/python3` already has on
`sys.path`). The watcher now ingests normally (`0 支進 ready` per tick, thumbnails
render).

## 6. Bugs / defects found

0. **Publish payload published the STT audio instead of the clip (fixed, and it
   already happened 7 times).** `sau_backend._artifact_payloads_for_platform()`
   puts every artifact without `metadata.role == "video"` into a `passthrough`
   list that comes **before** the video part, and the worker's
   `_prepared_artifact_local_paths()` hands the uploader every path in order.
   The prep pipeline auto-transcribes whenever the submit carried no transcript
   — so the first path was the extracted `…-audio.wav` (and the
   `…-transcript.txt` next).

   * 7 targets **already succeeded** with `artifacts[0].artifact_kind = audio`:
     bluesky/telegram/nw_sw_blog on 2026-10-18/10-19 (campaigns 2605, 2606,
     2609, 2610 — the Pride invitations). Those posts went out as audio
     attachments, not video.
   * **357 pending targets** had the same shape.
   * Twitter fails loudly instead (`Unsupported prepared Twitter media type:
     .wav`), which is how the class was noticed.

   Fixed in `_artifact_payloads_for_platform` (audio/transcript are context
   artifacts, never publishable) with a regression test covering every
   platform, and the 358 already-queued job payloads were repaired in place
   (`sau-work/current/strip_audio_artifacts.py`, DB snapshot first). Nothing is
   left in the queue that could publish a WAV. Note the fix only ships to the
   container on the next image build; the DB repair is what protects today's
   queue.

1. **nsfw thumbnail crash loop (fixed).** `watch.py::build_thumb` — unguarded
   `thumb.make_titlecard()` aborted the scan before `save_state()`. Now guarded,
   and the call site is wrapped so *no* thumbnail failure can lose a ready item.
2. **`.docx` transcripts ignored (fixed).** `_sidecar_candidates()` /
   `_read_sibling_note()` only knew `.txt`/`.md`/`.content.txt`, but the operator
   writes Word files (`both/*.docx`, `Nakedwill-news-*.docx`). Added stdlib docx
   text extraction (no `python-docx` in the cron interpreter) + 2 new tests.
   `tests/` → 28 passing.
3. **Orphan noise (fixed).** `_`-prefixed *directories* (`.sau-inbox`-style
   scratch, `_pub`, `_batch`) were reported as orphans every minute; files were
   already exempt. Now directories are too.
4. **Duplicate `campaign_posts` after a re-prep (mitigated).** Campaign 2544 had
   **14** twitter posts for 7 parts — the prep ran twice and *appended* a second
   set instead of replacing the first, so the same asset + copy was queued twice
   to the same account. 60 such exact duplicates were cancelled (keep earliest),
   freeing 60 slots. Root cause (append-vs-replace in the prep/`reprep_campaigns`
   path) still needs a code-level fix; the dedupe check is on
   `(account_ref, artifacts[0].local_path)`.
5. **The queue horizon contradicts the documented policy.** SKILL.md §5 says
   "horizon 30 days — never schedule beyond it", but `_next_free_slot()` walks
   forward without a ceiling:

   | account | pending | last slot |
   | --- | ---: | --- |
   | `NW TG 中文` (127) | 613 | **2027-09-10** |
   | `NW X (model_will)` (123) | 553 | 2027-04-15 |
   | `NW X (nudeweiwei)` (124) | 532 | 2027-04-11 |

   Either the policy or the allocator has to change: a 5 000-target queue at
   3/account/day cannot drain. Worth an operator decision (raise the cap, drop
   the backlog, or enforce the 30-day horizon and queue the rest as
   `needs_review`).
6. **Deploy restarts strand preps for up to 45 min.** The 16:47 container
   recreate (ci-watch) killed three in-flight preps; `_prepLease` keeps them
   "claimed" until `SAU_PREP_LEASE_MINUTES` (45) expires, then
   `requeue_stale_preparing` re-claims. Observed, self-healed, no data lost.
7. **Same-name duplicates on Drive are invisible.** Google Drive allows two
   objects with the same path; the watcher's dedupe is path-keyed, so the second
   copy is never ingested and never cleaned (`sw/img/截圖1-12` each exist twice).
   Harmless today (the first copy is already scheduled) but it accumulates.
7b. **A long clip's split parts are separate posts, not a thread.** There are
   920 queued jobs with `partCount > 1` and **zero** jobs using
   `threadFileRefs` — but `worker._run_platform_upload` explicitly supports a
   reply chain (`TwitterThreadVideo(file_paths=[...])`). So a 10-minute clip
   becomes N independent posts, each 30 minutes apart and subject to the 3/day
   cap. Usually the parts land on the same day (only 5 of 322 part-groups are
   spread over 7 days), but on the saturated X accounts they fragment across
   months: campaign 2974 (the news episode, 5 parts) has part 1 on 2026-10-22
   and parts 2-5 on `NW X (model_will)` in **February 2027**, and all five parts
   on `NW X (nudeweiwei)` land in **March–April 2027**. Those fragments are
   meaningless posted alone. Fix direction: emit one twitter target carrying
   `threadFileRefs` (the worker already handles it) instead of N part targets,
   or teach the allocator that parts of one submit are a single unit. Not fixed
   here — it needs the prep/enqueue path changed, and the queue repair would be
   a large re-time.

8. **The offload cron deleted a source out from under a live prep (fixed).**
   `offload_to_drive.sh` protects files that a *target* still needs (running,
   retrying, due-soon, or pending without a restore mapping) — but an
   async-prep campaign has **no targets until prep ends**, so its raw source was
   unprotected. At 16:47:19 campaign 2973 claimed
   `videoFile/Four years after I quit__sfw.mp4` for prep; at 16:47:55 the
   half-hourly offload copied it to Drive and unlinked it; at 16:53:04 prep died
   with `FileNotFoundError` in `media_remote_storage.upload_artifact` and the
   campaign went to `needs_review`. Campaign 2971 and 2972 lost their sources the
   same way. Fixed by excluding every `preparing` campaign's
   `metadata_json.prepRequest.mediaFilePaths` from the offload move (verified:
   the exclude list now contains all six staged sources); the three files were
   restored from `sau/inbox/videoFile/` and the failed campaign re-queued.
   The same hole existed one level down: a preparing campaign's *generated*
   artifacts (`campaign_artifacts.local_path`) are equally unreferenced by any
   target, so the 17:47 offload moved campaign 2971/2972's watermarked parts to
   Drive mid-encode. Both are now excluded too (verified: 379 kept-local
   entries including `campaigns/campaign-2971/…-watermarked_video.mp4`), and the
   files were copied back so the in-flight preps could finish.

## 7. Purge / cleanup (nothing deleted)

* **6 byte-identical duplicate photos** (md5 verified) moved from the inbox to
  `sau/purged/2026-10-20-duplicates/` with a README explaining each pair. The
  named originals stay in `SAU-Inbox` and are already scheduled.
* **60 duplicate pending targets** cancelled (same account + same media part +
  same copy).
* **22 stale `ready` entries** cleared after the media was archived; all moved to
  `state.json.processed` under their Drive identity so they cannot re-enter.
* Scratch (`_work/`, `.stt-tmp`, `transcripts*`, `metadata_batch/`, the 2026-10-17
  one-off scripts) moved out of the watched inbox to `/home/will/sau-work/`.

## 8. Verification

* `tests/` (watcher): 28 pass.
* Watcher ticks after the fix: `0 支進 ready`, no tracebacks; new files ingested.
* Campaigns 2967–2992 created and prepped; targets carry real `schedule_at`.
* Pride: `select count(*) … campaign_id in (…invitations…) and schedule_at >=
  '2026-10-31'` → **0**.
* Archive: every clip/image listed under `sau/published/videoFile/` with a
  same-name `.txt` (see §9).

## 9. Archive

Server-side `rclone moveto` (never a re-encode, never a delete) into
`sau/published/videoFile/`, each with a same-name `.txt` that holds the summary,
the brief and **the exact per-platform copy that was queued** (pulled from
`campaign_posts.draft_json`), e.g.

```
Marble doesnt breathe_NSFW_20261022.mp4      + .txt (23.7 KB)
Marble doesnt breathe_SFW_20261023.mp4       + .txt (28.2 KB)
Four years after I quit_SFW_20261024.mp4     + .txt (43.9 KB)
Nakedwill News Ep.006 …_SFW_20261021.mp4     + .txt (31.0 KB) + the operator's .docx
光光邀你十月三十號一起來遊行_NSFW_20261021.mp4  + .txt
Night boardwalk, full body_NSFW_20261102.jpg + .txt
… 10 more photo archives, same pattern
```

Drive's server-side move is rate-limited (`rateLimitExceeded` → rclone reports
the confusing "directory not found"): the archive helper now runs rclone with
`--tpslimit 2 --retries 8 --low-level-retries 20`, and the video moves were
re-run individually with the same pacing.

`SAU-Inbox/both/video/` and `sw/video/` are now empty of today's uploads; the
6 duplicate photos are in `sau/purged/2026-10-20-duplicates/` with a README.

## 10. Files moved out of the watched inbox

The inbox root had accumulated a dozen one-off scripts, 839 MB of leftover
audio/video in `.stt-tmp/`, an old transcripts tree and the 10-17 batch plans.
All of it is now under `/home/will/sau-work/` (`current/` = this session's
scripts, `archive-2026-10-17/` = the previous batch's), which is outside
`SAU_INBOX` so the watcher no longer scans it. Kept in the inbox: `watch.py`,
`thumb.py`, `manual_platforms.py`, `gen_title.py`, `state.json`, `.env`, the
READMEs, `tests/`, the persona folders — exactly what a scan needs.

## 11. Final state (verified 2026-10-20 22:00 CST)

| check | result |
| --- | --- |
| campaigns 2967–2992 | **all 26 `publishing`** with live targets (2975 took four attempts; it finally prepared 23 jobs at 01:22 on 2026-10-21 — see §11.2). |
| Pride invitations after 2026-10-31 | **0** |
| Pride invitations window | 2026-10-20 21:30 → 2026-10-30 23:05 Taipei |
| `SAU-Inbox` video folders | empty (all published sources archived) |
| watcher state | `ready=0 pending=0 quarantined=0` |
| `tests/` (inbox watcher) | 28 pass |
| `tests/` (repo) | 1816 pass, 1 skipped |
| pending targets with audio/transcript as media | **0** |

### What to watch tomorrow

1. **X threads for the news episode.** `campaign_posts` for 2974 has 5 parts on
   each X account because the clip is split for the 140 s cap; part 1 posts on
   2026-10-22 (model_will), parts 2-5 sit in 2027. Nothing is broken at the API
   level, but those fragments are meaningless alone — see §6.7b for the fix
   direction (`threadFileRefs`).
2. **2975** (news on the SW profile) needed **four attempts** before it stuck.
   Its prep takes ~30-60 min (783 MB source, split + watermark + STT + drafts for 11 accounts) and the
   container was recreated **six** times today (watchtower hourly pull + two
   ci-watch deploys from this session), each recreate killing the in-flight
   encode. Attempts were reset three times; the lease/attempts are reset again
   (`status=preparing`, `_prepAttempts=0`, no lease) so it retries.
   If it lands in `needs_review` again, the one-liner is:

   ```sql
   UPDATE campaigns SET status='preparing', last_error=NULL,
     metadata_json=json_remove(json_set(metadata_json,'$._prepAttempts',0),'$._prepLease')
   WHERE id=2975;
   ```

   It did finish (23 jobs at 01:22) once a restart-free window appeared.
   The structural fix is the one `wsgi.py` already documents: run the worker as
   a **separate process/container** (`python -m myUtils.worker`) so a web-image
   deploy does not decapitate a 30-minute ffmpeg run. The attempts also leave
   extra part sets in `campaign_artifacts` (5-, 3- and 2-part plans); that is
   harmless because `_select_videos_for_platform` keeps exactly one plan per
   platform (largest parts that fit), but it is ~100 MB of dead files per
   retry.
3. **Pride reminder.** Everything dated is now inside 10-30, but the operator
   may still want a *fresh* reminder on 10-29/10-30 — the 光光 clip itself says
   "十月三十號要來遊行" and posts 10-28 21:35.
