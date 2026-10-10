# Verify scheduled publishing — end-to-end dry simulation and safety audit

Date: **2026-10-10** (host `TZ=Asia/Taipei`, `date -u` = 2026-10-10T05:37Z)
Repo: `/home/will/social-auto-upload`
Operator campaigns in scope: **2598, 2599, 2600, 2601, 2602, 2603** (90 targets, all `pending`)
Whole queue: **1334 pending targets**

**Verdict: after two data fixes, all 90 in-scope targets are READY.** No target was
published, resubmitted or cancelled. The only writes were 27 `publish_jobs.payload_json`
rows and 9 `campaign_artifacts.local_path` rows (plus a DB backup).

---

## 0. TL;DR

| # | finding | severity | action |
|---|---|---|---|
| 1 | 27 payload artifacts pointed at `/tmp/sau-test-generated-*` paths that only exist on the host test harness, not in the running container. The worker's `_record_for` fell back to `source_file_record_id` and would have restored the **raw source** (wrong bytes) instead of the prepared/watermarked `_pub` file. | HIGH | fixed 27 payloads |
| 2 | Campaign 2599's payload `public_url`s were `https://1drv.ms/...` links returning **HTTP 403**. Facebook (prefers URL), Instagram (requires URL) and Threads (requires URL) would have failed or posted a dead link. | HIGH | fixed 13 payloads to the working R2 URLs |
| 3 | Message bodies over the platform cap (27 targets; e.g. 469 chars on a 280-char X account). | LOW | no fix needed — every publisher truncates (`prepared_publishers._enforce_message_limit`, `sociamonials_fallback.compose_message_with_links`, Bluesky's own 297+… cut) |
| 4 | Campaign 2603's first slot is 2026-10-12T13:50Z (21:50 Taipei), ~1h50m after its 20:00 Taipei base. | LOW | noted; still on 2026-10-12 |
| 5 | The worker is the **in-process scheduler thread** inside the single Gunicorn worker, not a separate process. | INFO | confirmed alive; max concurrency 3; 1334 targets will not pile up |

---

## 1. Method (read-only trace, one target per campaign then all 90)

For each target I read `publish_jobs.payload_json` and its `publish_job_targets` row, then:

* **Content guard** — called the real `myUtils.worker._content_guard_error`
  (`myUtils/worker.py:95`), which itself calls `content_rules.is_usable_copy`
  (`myUtils/content_rules.py:163`) and `content_rules.message_matches_language`
  (`myUtils/content_rules.py:203`). A `[content-guard] placeholder/generic copy`
  failure would have permanently failed the target at 12:00 on 10-12.
* **Language** — compared the draft to `accounts.config_json.audience_language`
  (`en`, `zh-Hant`, `en,zh-Hant`) via the same function.
* **Account routing** — `myUtils.worker._resolve_structured_account` +
  `myUtils.profiles.get_account`: account exists, `enabled=1`, and its
  `profile_id` matches the campaign's `profile_id`.
* **Media restorable** — dry-ran `_ensure_artifact_paths_local`
  (`myUtils/worker.py:1931`) with `media_remote_storage.download_from_backend`
  monkey-patched, to observe *which* object the worker would actually fetch
  (`myUtils/media_remote_storage.py:154` → `myUtils/rclone_storage.py:143`, which
  composes `bucket + ":" + endpoint + "/" + storage_key` at
  `myUtils/rclone_storage.py:160`). Then verified the real object on Drive with
  `rclone lsjson` against the container's `rclone-cache.conf` remote
  `GDrive-willywang8216`.
* **Platform fits** — decimal size and probed duration versus
  `myUtils/platform_limits.py`, using the same semantics as
  `myUtils/prepared_publishers._enforce_video_limits` (`:101`) plus each
  platform's own guard (Bluesky `:4654`, Threads `:2049`, TikTok, YouTube).
  Durations were probed from the real Drive objects with `ffprobe` (streaming the
  faststart header); sizes come from `rclone lsjson`.

The container was also inspected directly:

```
$ docker exec social-auto-upload ls -la /tmp
total 8
drwxrwxrwt 1 root root 4096 Oct 10 13:06 .
drwxr-xr-x 1 root root 4096 Oct 10 13:06 ..
$ docker exec social-auto-upload ls -la /app/generated/campaigns/campaign-2598
... audio          # no video artifacts, they were offloaded to Drive
```

This is what exposed finding #1: the payload referenced host `/tmp` test dirs
that do not exist in the container.

---

## 2. The 90-target table

`guard` = `worker._content_guard_error` result (PASS = `None`);
`language` = caption matches `audience_language`;
`media-restorable` = the exact `_pub` Drive object exists at the composed
`endpoint/key` (or local); `platform-fits` = duration/size vs the platform cap.
Every row is `READY`.

| campaign | account | platform | guard | language | media-restorable | platform-fits | verdict |
|---|---|---|---|---|---|---|---|
| 2598 | account:118 () | bluesky | PASS | PASS | Drive OK | ok (246s/9.9MB vs 600.5s/300MB) | READY |
| 2598 | account:119 () | bluesky | PASS | PASS | Drive OK | ok (246s/9.9MB vs 600.5s/300MB) | READY |
| 2598 | account:11 (Nakedwill) | facebook | PASS | PASS | Drive OK | ok (246s/9.9MB vs 14460.0s/4096MB) | READY |
| 2598 | account:72 (NW_IG) | instagram | PASS | PASS | Drive OK | ok (246s/9.9MB vs 900.0s/300MB) | READY |
| 2598 | account:112 (NW Blog) | nw_sw_blog | PASS | PASS | Drive OK | ok (246s/9.9MB vs ∞s/∞MB) | READY |
| 2598 | account:105 (Nakedwill Reddit) | reddit | PASS | PASS | Drive OK | ok (246s/9.9MB vs 900.0s/1000MB) | READY |
| 2598 | account:127 () | telegram | PASS | PASS | Drive OK | ok (246s/9.9MB vs ∞s/2000MB) | READY |
| 2598 | account:116 () | telegram | PASS | PASS | Drive OK | ok (246s/9.9MB vs ∞s/2000MB) | READY |
| 2598 | account:62 (NWthreads) | threads | PASS | PASS | Drive OK | ok (246s/9.9MB vs 300.0s/1000MB) | READY |
| 2598 | account:109 (Nakedwill_TK) | tiktok | PASS | PASS | Drive OK | ok (246s/105.5MB vs 3600.0s/4096MB) | READY |
| 2598 | account:110 (Itswill_YT (both naked+sexual)) | youtube | PASS | PASS | Drive OK | ok (246s/9.9MB vs 43200.0s/262144MB) | READY |
| 2598 | account:124 () | twitter | PASS | PASS | Drive OK | ok (123s/4.9MB vs 140.0s/512MB) | READY |
| 2598 | account:123 () | twitter | PASS | PASS | Drive OK | ok (123s/4.9MB vs 140.0s/512MB) | READY |
| 2598 | account:124 () | twitter | PASS | PASS | Drive OK | ok (123s/5.0MB vs 140.0s/512MB) | READY |
| 2598 | account:123 () | twitter | PASS | PASS | Drive OK | ok (123s/5.0MB vs 140.0s/512MB) | READY |
| 2599 | account:120 () | bluesky | PASS | PASS | Drive OK | ok (246s/9.9MB vs 600.5s/300MB) | READY |
| 2599 | account:121 () | bluesky | PASS | PASS | Drive OK | ok (246s/9.9MB vs 600.5s/300MB) | READY |
| 2599 | account:64 (SW-FB) | facebook | PASS | PASS | Drive OK | ok (246s/9.9MB vs 14460.0s/4096MB) | READY |
| 2599 | account:75 (SW_IG) | instagram | PASS | PASS | Drive OK | ok (246s/9.9MB vs 900.0s/300MB) | READY |
| 2599 | account:113 (SW Blog) | nw_sw_blog | PASS | PASS | Drive OK | ok (246s/9.9MB vs ∞s/∞MB) | READY |
| 2599 | account:106 (Sexualwill Reddit OAuth) | reddit | PASS | PASS | Drive OK | ok (246s/9.9MB vs 900.0s/1000MB) | READY |
| 2599 | account:122 () | telegram | PASS | PASS | Drive OK | ok (246s/9.9MB vs ∞s/2000MB) | READY |
| 2599 | account:117 () | telegram | PASS | PASS | Drive OK | ok (246s/9.9MB vs ∞s/2000MB) | READY |
| 2599 | account:42 (SW_threads) | threads | PASS | PASS | Drive OK | ok (246s/9.9MB vs 300.0s/1000MB) | READY |
| 2599 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (123s/4.9MB vs 140.0s/512MB) | READY |
| 2599 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (123s/4.9MB vs 140.0s/512MB) | READY |
| 2599 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (123s/5.0MB vs 140.0s/512MB) | READY |
| 2599 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (123s/5.0MB vs 140.0s/512MB) | READY |
| 2600 | account:112 (NW Blog) | nw_sw_blog | PASS | PASS | Drive OK | ok (246s/12.5MB vs ∞s/∞MB) | READY |
| 2600 | account:127 () | telegram | PASS | PASS | Drive OK | ok (246s/12.5MB vs ∞s/2000MB) | READY |
| 2600 | account:118 () | bluesky | PASS | PASS | Drive OK | ok (246s/12.5MB vs 600.5s/300MB) | READY |
| 2600 | account:119 () | bluesky | PASS | PASS | Drive OK | ok (246s/12.5MB vs 600.5s/300MB) | READY |
| 2600 | account:105 (Nakedwill Reddit) | reddit | PASS | PASS | Drive OK | ok (246s/12.5MB vs 900.0s/1000MB) | READY |
| 2600 | account:116 () | telegram | PASS | PASS | Drive OK | ok (246s/12.5MB vs ∞s/2000MB) | READY |
| 2600 | account:123 () | twitter | PASS | PASS | Drive OK | ok (123s/6.0MB vs 140.0s/512MB) | READY |
| 2600 | account:123 () | twitter | PASS | PASS | Drive OK | ok (123s/6.3MB vs 140.0s/512MB) | READY |
| 2600 | account:124 () | twitter | PASS | PASS | Drive OK | ok (123s/6.0MB vs 140.0s/512MB) | READY |
| 2600 | account:124 () | twitter | PASS | PASS | Drive OK | ok (123s/6.3MB vs 140.0s/512MB) | READY |
| 2601 | account:120 () | bluesky | PASS | PASS | Drive OK | ok (246s/12.5MB vs 600.5s/300MB) | READY |
| 2601 | account:113 (SW Blog) | nw_sw_blog | PASS | PASS | Drive OK | ok (246s/12.5MB vs ∞s/∞MB) | READY |
| 2601 | account:121 () | bluesky | PASS | PASS | Drive OK | ok (246s/12.5MB vs 600.5s/300MB) | READY |
| 2601 | account:106 (Sexualwill Reddit OAuth) | reddit | PASS | PASS | Drive OK | ok (246s/12.5MB vs 900.0s/1000MB) | READY |
| 2601 | account:122 () | telegram | PASS | PASS | Drive OK | ok (246s/12.5MB vs ∞s/2000MB) | READY |
| 2601 | account:117 () | telegram | PASS | PASS | Drive OK | ok (246s/12.5MB vs ∞s/2000MB) | READY |
| 2601 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (123s/6.0MB vs 140.0s/512MB) | READY |
| 2601 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (123s/6.0MB vs 140.0s/512MB) | READY |
| 2601 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (123s/6.3MB vs 140.0s/512MB) | READY |
| 2601 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (123s/6.3MB vs 140.0s/512MB) | READY |
| 2602 | account:120 () | bluesky | PASS | PASS | Drive OK | ok (254s/46.4MB vs 600.5s/300MB) | READY |
| 2602 | account:121 () | bluesky | PASS | PASS | Drive OK | ok (254s/46.4MB vs 600.5s/300MB) | READY |
| 2602 | account:113 (SW Blog) | nw_sw_blog | PASS | PASS | Drive OK | ok (254s/46.4MB vs ∞s/∞MB) | READY |
| 2602 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (127s/40.4MB vs 140.0s/512MB) | READY |
| 2602 | account:106 (Sexualwill Reddit OAuth) | reddit | PASS | PASS | Drive OK | ok (254s/46.4MB vs 900.0s/1000MB) | READY |
| 2602 | account:122 () | telegram | PASS | PASS | Drive OK | ok (254s/46.4MB vs ∞s/2000MB) | READY |
| 2602 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (127s/40.4MB vs 140.0s/512MB) | READY |
| 2602 | account:117 () | telegram | PASS | PASS | Drive OK | ok (254s/46.4MB vs ∞s/2000MB) | READY |
| 2602 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (127s/40.4MB vs 140.0s/512MB) | READY |
| 2602 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (127s/40.4MB vs 140.0s/512MB) | READY |
| 2602 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (127s/43.1MB vs 140.0s/512MB) | READY |
| 2602 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (127s/43.1MB vs 140.0s/512MB) | READY |
| 2602 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (127s/43.1MB vs 140.0s/512MB) | READY |
| 2602 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (127s/43.1MB vs 140.0s/512MB) | READY |
| 2603 | account:113 (SW Blog) | nw_sw_blog | PASS | PASS | Drive OK | ok (600s/76.3MB vs ∞s/∞MB) | READY |
| 2603 | account:120 () | bluesky | PASS | PASS | Drive OK | ok (300s/65.2MB vs 600.5s/300MB) | READY |
| 2603 | account:121 () | bluesky | PASS | PASS | Drive OK | ok (300s/65.2MB vs 600.5s/300MB) | READY |
| 2603 | account:106 (Sexualwill Reddit OAuth) | reddit | PASS | PASS | Drive OK | ok (600s/76.3MB vs 900.0s/1000MB) | READY |
| 2603 | account:122 () | telegram | PASS | PASS | Drive OK | ok (600s/76.3MB vs ∞s/2000MB) | READY |
| 2603 | account:117 () | telegram | PASS | PASS | Drive OK | ok (600s/76.3MB vs ∞s/2000MB) | READY |
| 2603 | account:120 () | bluesky | PASS | PASS | Drive OK | ok (300s/76.6MB vs 600.5s/300MB) | READY |
| 2603 | account:121 () | bluesky | PASS | PASS | Drive OK | ok (300s/76.6MB vs 600.5s/300MB) | READY |
| 2603 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (120s/26.1MB vs 140.0s/512MB) | READY |
| 2603 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (120s/26.1MB vs 140.0s/512MB) | READY |
| 2603 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (120s/26.1MB vs 140.0s/512MB) | READY |
| 2603 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (120s/25.9MB vs 140.0s/512MB) | READY |
| 2603 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (120s/26.1MB vs 140.0s/512MB) | READY |
| 2603 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (120s/25.9MB vs 140.0s/512MB) | READY |
| 2603 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (120s/25.9MB vs 140.0s/512MB) | READY |
| 2603 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (120s/27.9MB vs 140.0s/512MB) | READY |
| 2603 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (120s/25.9MB vs 140.0s/512MB) | READY |
| 2603 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (120s/27.9MB vs 140.0s/512MB) | READY |
| 2603 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (120s/27.9MB vs 140.0s/512MB) | READY |
| 2603 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (120s/27.9MB vs 140.0s/512MB) | READY |
| 2603 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (120s/31.0MB vs 140.0s/512MB) | READY |
| 2603 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (120s/31.0MB vs 140.0s/512MB) | READY |
| 2603 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (120s/31.0MB vs 140.0s/512MB) | READY |
| 2603 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (120s/31.0MB vs 140.0s/512MB) | READY |
| 2603 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (120s/30.9MB vs 140.0s/512MB) | READY |
| 2603 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (120s/30.9MB vs 140.0s/512MB) | READY |
| 2603 | account:103 (光光) | twitter | PASS | PASS | Drive OK | ok (120s/30.9MB vs 140.0s/512MB) | READY |
| 2603 | account:77 (sexualwill) | twitter | PASS | PASS | Drive OK | ok (120s/30.9MB vs 140.0s/512MB) | READY |

---

## 3. Fixes applied

### Fix 1 (HIGH) — 27 payloads restored the wrong media

`publish_jobs.payload_json` for 13 campaign-2599 targets and 14 campaign-2603
targets carried an artifact `local_path` under `/tmp/sau-test-generated-*`
(created when prep ran under the test harness against the production DB — the
BUG-1 shape described in `reports/pride-scheduling-and-fixes-2026-10-09.md`).
Those directories do not exist in the container. Because the path had no
`/generated/` marker, `_record_for` (`myUtils/worker.py:1954`) skipped the
generated-artifact branch and took the `if source_id:` fallback
(`myUtils/worker.py:2011`), which returns the **raw source** `file_records` row.

Proven with a dry trace (`/tmp` dirs temporarily hidden):

```
5718 ok | [('rclone','GDrive-willywang8216','sau/inbox/videoFile','SFW NW+SW invitation to gay pride .mp4')]
5766 ok | [('rclone','GDrive-willywang8216','sau/inbox/videoFile','b8f269e1-..._NSFW_NWSW_news.mp4')]
```

Fix: rewrite each affected artifact `local_path` to its canonical
`/app/generated/campaigns/campaign-<id>/<name>`. The worker then matches the
generated `file_records` row exactly (which points at the Drive
`sau/inbox/generated/...` object). Exact targets changed (job id → artifact):

| campaign | jobs | artifact(s) |
|---|---|---|
| 2599 | 5718, 5720, 5721, 5723, 5729, 5730, 5731, 5732, 5733 | `SFW NW+SW invitation to gay pride _pub.mp4` |
| 2599 | 5734, 5736 | `..._pub_part1of2_pub.mp4` |
| 2599 | 5735, 5737 | `..._pub_part2of2_pub.mp4` |
| 2603 | 5766, 5767, 5768, 5769 | `b8f269e1-..._NSFW_NWSW_news_pub.mp4` |
| 2603 | 5770, 5780 | `..._pub_part1of5_pub.mp4` |
| 2603 | 5772, 5782 | `..._pub_part2of5_pub.mp4` |
| 2603 | 5774, 5784 | `..._pub_part3of5_pub.mp4` |
| 2603 | 5776, 5786 | `..._pub_part4of5_pub.mp4` |
| 2603 | 5778, 5788 | `..._pub_part5of5_pub.mp4` |

The 9 `campaign_artifacts` rows with the same `/tmp` paths (`2534,2539,2540`
for 2599; `2547-2552` for 2603) were repointed to the canonical path too, so a
future re-prep cannot reintroduce the temp path.

### Fix 2 (HIGH) — campaign 2599 public URLs were dead OneDrive links

2599's payload artifacts carried `public_url=https://1drv.ms/...`. HEAD requests:

```
ERR 403 HTTPError 403: Forbidden .../IQBgxujscmfrSYDBttH9yxeyAZ92oM5k-7IIhRFE_x7tNc4
ERR 403 HTTPError 403: Forbidden .../IQBm05SyStPQSI8Qj_7KRktpAembriWyIUAW_D5hKhZzaKY
ERR 403 HTTPError 403: Forbidden .../IQARGmqaN0XCQ60lt0lhCQ3_AQk8S2fKPznaqeQLyhqfdOw
```

`publish_facebook_sync` prefers `public_url` over the local file
(`myUtils/prepared_publishers.py:1587`), and `publish_instagram_sync` /
`publish_threads_sync` **require** a public URL (`:1827`, `:2049`). A 403 from
the operator's OneDrive share would have failed those three Facebook/Instagram/
Threads targets (and the duplicates).

Fix: point each 2599 artifact at the already-registered, working R2 object in
`campaign_artifacts` (rows `2553,2555,2556`), verified with HTTP 200 and the
exact byte counts:

```
200 9886238 https://pub-9915b1494003455c9ab872fd7094e64e.r2.dev/campaigns/2599/videos/SFW%20NW%2BSW%20invitation%20to%20gay%20pride%20_pub.mp4
200 4851889 ..._pub_part1of2_pub.mp4
200 5035080 ..._pub_part2of2_pub.mp4
```

For campaign 2603's full `_pub` I also replaced the stale
`getFile?filename=<basename>` URL with the canonical
`getFile?filename=campaigns/campaign-2603/<basename>` form; the plain
`campaigns/...` form is what `/getFile` resolves against the `generated/` root
(`sau_backend.py:979`).

### Verification after the fixes

```
$ .venv/bin/python /tmp/trace_final.py
targets: 90 errors: 0 placeholder files cleaned: 24
```

Real (not monkey-patched) restore of one prepared file per campaign, byte size
compared to the Drive object, then deleted:

```
camp 2598 job 5700: restored=True bytes=9886238  path=SFW NW+SW invitation to gay pride _pub.mp4
camp 2599 job 5718: restored=True bytes=9886238  path=SFW NW+SW invitation to gay pride _pub.mp4
camp 2600 job 5715: restored=True bytes=12503454 path=NSFW NW+SW invitation to gay pride_pub.mp4
camp 2601 job 5738: restored=True bytes=12503454 path=NSFW NW+SW invitation to gay pride_pub.mp4
camp 2602 job 5748: restored=True bytes=46375033 path=4e4413a8-..._NSFW_SWNW_news_pub.mp4
camp 2603 job 5762: restored=True bytes=65177473 path=b8f269e1-..._pub_part1of2_pub.mp4
```

DB integrity after the writes: `PRAGMA integrity_check;` → `ok`. Target and
campaign statuses are unchanged: 90 `pending`, campaigns 2598-2603 all
`publishing`, and the global pending count is still **1334**.

---

## 4. Timezone — verified end to end

`publish_job_targets.schedule_at` is **tz-naive UTC**. A naive schedule input is
interpreted as the operator zone (`myUtils/publish_orchestrator.py:172`
`_operator_timezone`, default `Asia/Shanghai`; `:194` `_resolve_base_time`). The
container has `TZ=Asia/Taipei`; both are **UTC+8**, so naive operator-local input
and Taipei wall-clock agree.

| stored (UTC) | Asia/Taipei | campaign | metadata `startAt` |
|---|---|---|---|
| 2026-10-24T12:00:00 | 2026-10-24 20:00 CST | 2598/2599 | `2026-10-24T20:00:00` |
| 2026-10-24T14:10:00 | 2026-10-24 22:10 CST | 2600 (first) | `2026-10-24T22:00:00` |
| 2026-10-24T14:00:00 | 2026-10-24 22:00 CST | 2601 (first) | `2026-10-24T22:00:00` |
| 2026-10-12T12:00:00 | 2026-10-12 20:00 CST | 2602 (first) | `2026-10-12T20:00:00` |
| 2026-10-12T13:50:00 | 2026-10-12 21:50 CST | 2603 (first) | `2026-10-12T20:00:00` |

**Yes:** `2026-10-24T12:00:00Z` really is **20:00 Taipei**, and Taipei Pride is
2026-10-31, so **2026-10-24 is exactly 7 days (one week) before Pride**. That is
the intended slot. The 2603 first slot is 1h50m later than its base (the
re-prep's slot allocator avoided conflicts), but it is still on 2026-10-12.

---

## 5. Will the worker actually claim them? (throughput)

* The "worker" is the **in-process publish scheduler** started at import:
  `sau_backend.py:10042` → `_maybe_start_publish_scheduler` (`:7054`) spawns the
  `publish-scheduler` daemon thread running `_publish_scheduler_loop` (`:7034`)
  every `SAU_PUBLISH_SCHEDULER_INTERVAL_SECONDS=60`. Each tick checks
  `job_runtime.has_claimable_targets()` and, when due work exists, spawns a
  drain thread that runs `PublishWorker.drain()`. The container is up and the
  scheduler starts on boot; no scheduler errors in `docker logs`.
* `claim_next_targets` (`myUtils/jobs.py:563`) only claims rows whose
  `schedule_at <= now` (`_claimable_clause`, `:165`), ordered by `id`, skipping
  accounts already in flight.
* **Concurrency:** `SAU_MAX_CONCURRENT_BROWSERS` is unset → `MAX_CONCURRENT_BROWSERS = 3`
  (`utils/concurrency.py`). `batch_size` defaults to 4; per-account locks
  serialise same-account posts.
* **Load:** the busiest pending minute in the whole queue is **12 targets**
  (e.g. `2026-10-24T13:00Z`); the busiest day is 58 targets. At 3 concurrent and
  a 5–10 minute nominal spacing, 12 simultaneous posts drain in tens of minutes,
  not hours. The 1334 pending targets are spread across ~10 months, so nothing
  piles up into an unbounded backlog.
* Container restart risk: the in-process thread dies if Gunicorn/the container
  is recreated (Watchtower only acts on a new image). Any target left `running`
  is requeued by the stale-running sweep (`jobs.requeue_stale_running`), so a
  restart costs a retry, not a lost post.

---

## 6. Test suite

```
$ .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1569 passed, 1 skipped, 139 subtests passed in 114.31s (0:01:54)
```

Baseline stated in the task was 1559 passed, 1 skipped; the current tree has 10
more passing tests (added by other work on `main`) and **no failures**.

---

## 7. Residual risks / at-risk items (exact reasons)

1. **X targets ride the Sociamonials fallback, not the direct X API.**
   `SAU_SOCIAMONIALS_FALLBACK=1` and `SAU_X_DIRECT_PUBLISH` unset make
   `_x_direct_publish_enabled()` return `False`, so every `twitter` target goes
   through `_try_sociamonials_fallback`. The fallback truncates the caption to
   280 chars (`sociamonials_fallback.py:1092`) and delivered/scheduled in the
   observed log. This is by design, but it means the direct Twitter publisher's
   140s `_enforce_video_limits` is not exercised; the fallback's own "Video URL
   not accessible after retries" remains the failure mode. All 5 campaign-2603
   and 4 campaign-2602 X parts are 120–127s and verified present on Drive.
2. **Over-cap captions are truncated, not rejected.** 27 in-scope targets have a
   draft longer than the platform cap (X 317–469/280, Bluesky 326–351/300,
   Threads 685/794 vs 500). The body loses trailing hashtags/CTA. No runtime
   failure; purely a copy-quality observation.
3. **`/getFile` URLs depend on the artifact being restored locally first.**
   `sau_backend.py:979` serves from `generated/`, `videoFile/` or `uploads/` and
   only falls back to a CDN lookup that does not match for these rows. The worker
   restores the file in `default_executor` before the publisher runs, so the URL
   resolves at post time; a future direct fetch of these URLs without a prior
   restore would 404.
4. **2603's full `_pub` is 600.0s (600.003646s).** It is only used by
   nw_sw_blog (no cap), reddit (900s cap) and telegram (no cap), so it fits. It
   is *not* used by Bluesky (that uses the 300s split parts) — worth keeping that
   way, since 600.0036s would sit within Bluesky's +0.5s tolerance
   (`prepared_publishers.py:4654`) but with no headroom.
5. **Latent code risk (not fixed, out of scope):** `_record_for`
   (`myUtils/worker.py:2011`) silently substitutes `source_file_record_id` when a
   path is not under a generated root. This is the mechanism that turned a temp
   path into the wrong media. Fixing the 27 payloads removes the immediate risk;
   a code-level guard (prefer the matching campaign artifact over a raw source
   record) would prevent recurrence if a payload is ever built under a temp root
   again. This task's constraints limited changes to payloads/config, so it is
   reported rather than changed.

---

## 8. Exact commands / artefacts

* Fix script: `/tmp/fix_payloads.py` (dry-run then `--apply`; 27 payloads, 9
  `campaign_artifacts` rows).
* Restore dry trace: `/tmp/trace_final.py` (90/90 resolved, 0 errors).
* Real restore proof: `/tmp/real_restore.py` (6/6 byte-exact).
* Table generator: `/tmp/make_table.py` → `/tmp/audit_table.md`.
* DB backup: `db/database.db.bak-pre-verify-scheduled-20261010-133738`.
* Guard sample:
  `camp 2598 job 5700 ... guard=None`, `camp 2603 job 5762 ... guard=None`
  (all 90 return `None`).
