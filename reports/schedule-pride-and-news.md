# Schedule report — Taipei Pride invitation + time-sensitive NSFW news

Owner: pi task session · Date: 2026-10-09 · Repo `/home/will/social-auto-upload`
Status: **scheduled only — nothing was published.**

---

## 0. Clock and timezone basis

| | value |
| --- | --- |
| `date` (host, TZ Asia/Taipei) | **Fri Oct 9 18:28 CST 2026** |
| `date -u` | **Fri Oct 9 10:28 UTC 2026** |
| Operator timezone | Asia/Shanghai == UTC+8 (`SAU_OPERATOR_TIMEZONE` unset, default; `.env` also sets `TZ=Asia/Taipei`, also UTC+8) |

The app stores `publish_job_targets.schedule_at` as **naive UTC** and interprets a
naive API/CLI input as **Asia/Shanghai** (`myUtils/publish_orchestrator.py::_resolve_base_time`).
Asia/Shanghai and Asia/Taipei are both UTC+8, so naive operator-local input and
naive Taipei wall-clock agree. All rows quoted below are the stored naive-UTC values.

---

## 1. Date math

### 1a. This year's Taipei Pride = 2026-10-31 (Saturday)

*Source (primary, official):* **https://www.taiwanpride.lgbt/2026-info-1** — the
organizer's own 2026 page states verbatim:

> 第 24 屆臺灣同志遊行　日期：2026 年 10 月 31 日（六）

*Corroborating rule:* English Wikipedia "Taiwan Pride" states the parade is held
on **the last Saturday of October**, and records 2023-10-28, 2024-10-26,
2025-10-25. The last Saturday of October 2026 is **2026-10-31**.

**Confidence: high.** The official site gives the exact date, so this is not the
"last Saturday" inference alone.

- **Pride invitation target = one week before = 2026-10-24 (Saturday).**
  I used a naive base of `2026-10-24T20:00:00` (Taipei) → stored `2026-10-24T12:00:00Z`.

### 1b. Time-sensitive NSFW news = +3 days

- 2026-10-09 → **2026-10-12**.
  I used a naive base of `2026-10-12T20:00:00` (Taipei) → stored `2026-10-12T12:00:00Z`.

Existing schedule evidence that the operator already treats 10-31 as Pride day is
present in the DB (Stonewall-related targets around 10-24/25 and 10-31). **I did
not touch any of those existing Stonewall rows.**

---

## 2. What I found (exact paths, sizes, mtimes, file_record ids)

### 2a. The gay-pride **INVITATION** — found (in `SAU-Inbox`, NOT `sau/videoFile`)

Google Drive remote `GDrive-willywang8216:SAU-Inbox/both/video/`:

| Drive path | bytes | Drive mtime (CST) | local staged copy | file_record id |
| --- | ---: | --- | --- | --- |
| `SAU-Inbox/both/video/SFW NW+SW invitation to gay pride .mp4` | 105,547,761 | 2026-10-09 16:47:56 | `videoFile/SFW NW+SW invitation to gay pride .mp4` | **1198** |
| `SAU-Inbox/both/video/NSFW NW+SW invitation to gay pride.mp4` | 95,202,622 | 2026-10-09 16:47:56 | `videoFile/NSFW NW+SW invitation to gay pride.mp4` | **1199** |

Both are 245.79 s HEVC/AAC videos. They are the only objects on the whole Drive
whose name contains "invitation to gay pride". The inbox watcher had flagged them
`pending` with reason *"檔名缺 <sfw|nsfw> 標籤"* (they use the old `SFW/NSFW` prefix
instead of the new `__sfw/__nsfw` suffix), so they had not auto-staged.

> **Important:** the repo's other pride-themed videos are *event coverage*, not an
> invitation — `SFW Taipei Stonewall.mp4`, `SFW Taipei Stonewall_short.mp4`,
> `*_SFW_SWNW_nakedwill_news_taipei_stonewall_sfw.mp4`, `*_stonewall_update_sfw.mp4`.
> I left all of those exactly as they were; per instruction I did **not** reschedule
> the existing Oct 24/31 Stonewall rows.

### 2b. Time-sensitive **NSFW news** — found in `sau/videoFile` (Oct-8 batch)

The newest inbox batch (2026-10-09 16:47) contains **no** "news" objects, so the
"other NSFW news" are the Oct-8 NSFW news videos:

| local / Drive path | bytes | mtime / upload (CST) | file_record id | drive backend |
| --- | ---: | --- | --- | --- |
| `videoFile/4e4413a8-10e5-42fb-a4e6-b3fd3c7699ea_NSFW_SWNW_news.mp4` | 46,375,033 | 2026-10-08 14:33:35 | **995** | 416 |
| `videoFile/b8f269e1-6809-46c7-a74e-c084c2134ad0_NSFW_NWSW_news.mp4` | 76,292,416 | 2026-10-08 14:10:22 | **1205** (source only registered as artifacts 955/983 before today) | — (downloaded from Drive today) |
| `videoFile/dba6cdeb-81d4-4f74-9715-d238516bc278_NSFW_NW_Nakedwill_News.mp4` | 3,401,601 | 2026-10-08 05:27:08 | **921** | 416 |

Also present and **not scheduled** (unidentified in the request):
`SAU-Inbox/both/video/SFW NW+SW Taipei Raid.mp4` (112,789,703 B, 2026-10-09
16:47:56) and `SAU-Inbox/both/video/SFW NW+SW elite athlete nudes.mp4`
(168,929,379 B, 2026-10-09 16:47:56).

---

## 3. What I scheduled (all rows are `status='pending'`, nothing published)

Method: new posts via the app's own `POST /publish-center/submit` with
`schedule.startAt` (async prep, `SAU_ASYNC_PREP=1`); existing NSFW-news rows
moved with the app's own slot allocator so the 30-min gap and 3/account/day cap
are respected. No target was published.

### 3a. Pride invitation — profile NW (1) and SW (3), target day **2026-10-24**

| campaign | profile | media | targets (id · account_ref · platform · schedule_at UTC) |
| --- | --- | --- | --- |
| 2598 | 1 (NW) | `SFW ...invitation...` (1198) | 5700 acct118 bluesky 10-24T12:00 · 5701 acct119 bluesky 12:05 · 5702 acct11 facebook 12:10 · 5703 acct72 instagram 12:15 · 5704 acct112 nw_sw_blog 12:20 · 5705 acct105 reddit 12:25 · 5706 acct127 telegram 12:30 · 5707 acct116 telegram 12:35 · 5708 acct62 threads 12:40 · 5709 acct109 tiktok 12:45 · 5714 acct110 youtube 13:10 · 5712 acct124 twitter 13:30 · 5710 acct123 twitter 13:50 · 5713 acct124 twitter **10-25T13:35** · 5711 acct123 twitter **10-25T13:55** |
| 2599 | 3 (SW) | `SFW ...invitation...` (1198) | 5718 acct120 bluesky 10-24T12:00 · 5720 acct121 bluesky 12:05 · 5721 acct64 facebook 12:10 · 5723 acct75 instagram 12:15 · 5729 acct113 nw_sw_blog 12:20 · 5730 acct106 reddit 12:25 · 5731 acct122 telegram 12:30 · 5732 acct117 telegram 12:35 · 5733 acct42 threads 12:40 · 5734 acct77 twitter 13:45 · 5736 acct103 twitter 13:55 · 5737 acct103 twitter **10-25T13:30** · 5735 acct77 twitter **10-25T13:50** |
| 2600 | 1 (NW) | `NSFW ...invitation...` (1199) | 5717 acct112 nw_sw_blog 10-24T14:10 · 5722 acct127 telegram 14:20 · 5715 acct118 bluesky **10-25T14:00** · 5716 acct119 bluesky **10-25T14:05** · 5719 acct105 reddit **10-25T14:15** · 5724 acct116 telegram **10-25T14:25** · 5725 acct123 twitter **10-25T14:30** · 5726 acct123 twitter **10-26T14:35** · 5727 acct124 twitter **10-26T14:40** · 5728 acct124 twitter **10-27T14:45** |
| 2601 | 3 (SW) | `NSFW ...invitation...` (1199) | 5738 acct120 bluesky 10-24T14:00 · 5740 acct113 nw_sw_blog 14:10 · 5739 acct121 bluesky **10-25T14:05** · 5741 acct106 reddit **10-25T14:15** · 5742 acct122 telegram **10-25T14:20** · 5743 acct117 telegram **10-25T14:25** · 5744 acct77 twitter **10-26T14:30** · 5746 acct103 twitter **10-26T14:40** · 5745 acct77 twitter **10-27T14:35** · 5747 acct103 twitter **10-27T14:45** |

The spill past 10-24 is the anti-spam rules working: those accounts already had
NSFW/other content booked on 10-24 (and/or hit the 3/day cap), so the allocator
walked forward. The SFW invitation reaches **all** platforms; the NSFW invitation
reaches only adult-safe platforms, because the submit path applies
`content_rating.restrict_accounts` from the filename (`SFW…` → sfw, `NSFW…` → nsfw).

### 3b. NSFW news — **2026-10-12** (a few spill to 10-13+ due to the cap)

**Existing NW rows moved to 10-12** (rescheduled in place; same media/accounts):

| target | media | account_ref | platform | new schedule_at UTC |
| --- | --- | --- | --- | --- |
| 5243 | dba6cdeb (921) | acct118 | bluesky | 2026-10-12T12:00 |
| 5244 | dba6cdeb | acct119 | bluesky | 2026-10-12T12:05 |
| 5245 | dba6cdeb | acct112 | nw_sw_blog | 2026-10-12T12:10 |
| 5246 | dba6cdeb | acct105 | reddit | 2026-10-12T12:15 |
| 5249 | dba6cdeb | acct123 | twitter | 2026-10-12T12:30 |
| 5247 | dba6cdeb | acct127 | telegram | 2026-10-12T12:50 |
| 5248 | dba6cdeb | acct116 | telegram | 2026-10-12T12:55 |
| 5250 | dba6cdeb | acct124 | twitter | 2026-10-12T13:35 |
| 5458 | 4e4413a8 (995) | acct123 | twitter | 2026-10-12T13:25 |
| 5454 | 4e4413a8 | acct112 | nw_sw_blog | 2026-10-12T13:45 |
| 5453 | 4e4413a8 | acct119 | bluesky | **2026-10-13T13:40** |
| 5456 | 4e4413a8 | acct127 | telegram | **2026-10-13T12:50** |
| 5460 | 4e4413a8 | acct124 | twitter | **2026-10-13T13:00** |

**New SW rows** — campaign **2602** (`4e4413a8`, profile 3), created because the
pre-existing SW campaign 2537 was stranded in `needs_review` (legacy campaign with
no `prepRequest`):

| targets | account | platform | schedule_at UTC |
| --- | --- | --- | --- |
| 5748 / 5749 | acct120 / acct121 | bluesky | 2026-10-12T12:00 / 12:05 |
| 5750 | acct113 | nw_sw_blog | 2026-10-12T12:10 |
| 5751 | acct106 | reddit | 2026-10-12T12:15 |
| 5752 / 5753 | acct122 / acct117 | telegram | 2026-10-12T12:30 / 12:35 |
| 5754 / 5758 | acct77 / acct103 | twitter | 2026-10-12T12:10 / 12:30 |
| 5755 / 5759 | acct77 / acct103 | twitter | 2026-10-13 |
| 5756 / 5760 | acct77 / acct103 | twitter | 2026-10-14 |
| 5757 / 5761 | acct77 / acct103 | twitter | 2026-10-16 |

Every NSFW-news post is on the adult-safe accounts of its profile; the
`_NW_`-named `dba6cdeb` video went to NW only, the `SWNW`/`NWSW` videos to both.

---

## 4. Could not schedule / blocked

1. **`b8f269e1…_NSFW_NWSW_news.mp4` (76.3 MB)**
   - **NW (campaign 2517): no pending rows** — already terminal (5 succeeded, 3
     failed, 8 cancelled on 2026-10-08/10). I did **not** re-queue it, because the
     duplicate guard only skips live rows and a re-queue would re-send it to the
     accounts where it already succeeded. Operator decision needed: leave the 5
     successes as-is, or re-queue only the failed Twitter/Reddit ones?
   - **SW:** attempted twice (campaigns **2603**, then **2604**). Both are stuck in
     `preparing` with 0 posts. Root cause observed: a **concurrent test-suite run
     from another agent session operated on the real `db/database.db`**, claimed
     campaign 2603, and wrote `campaign_artifacts` rows pointing at a pytest temp
     dir (`/tmp/sau-test-generated-…/campaign-2603/…`). The tests then stopped,
     leaving the campaign leased/stuck. Campaigns 2603 and 2604 are still
     `preparing`; no SW targets exist for this one video.
2. **`SFW NW+SW Taipei Raid.mp4` and `SFW NW+SW elite athlete nudes.mp4`** — newest
   inbox uploads, but the operator did not identify them (no "news" and not the
   invitation). Left unscheduled pending confirmation.
3. I did **not** alter the existing Taipei Stonewall rows on 2026-10-24/25/31.

---

## 5. UNKNOWNS / operator decisions needed

- **Interpretation of "the other NSFW news."** The 2026-10-09 upload batch contains
  only an SFW+NSFW *invitation* pair plus "Taipei Raid" and "elite athlete nudes";
  it contains no "news". I therefore treated the Oct-8 `*NSFW*news*` videos as the
  news set. If you meant the SFW `*_SFW_NWSW_nakedwill_news*` clips instead, say so
  and I will schedule those too.
- **Both invitation cuts?** I scheduled both the SFW and NSFW invitation (SFW → all
  platforms, NSFW → adult platforms). Confirm that is intended, or drop one.
- **`b8f269e1` on NW** (see §4.1): re-queue the failed/cancelled accounts, or leave?
- **SW `b8f269e1`** could not be scheduled because of cross-session test
  interference; needs a clean re-run (e.g. re-submit after the test run stops).
- **Two unidentified new inbox videos** (Taipei Raid, elite athlete nudes): what
  cadence/date and which profiles?

## 6. Housekeeping performed

- DB backup: `db/database.db.bak-pride-news-20261009-171229`.
- Staged Drive→local (bind-mounted, so the container can prep them):
  `videoFile/SFW NW+SW invitation to gay pride .mp4`,
  `videoFile/NSFW NW+SW invitation to gay pride.mp4`,
  `videoFile/4e4413a8…_NSFW_SWNW_news.mp4`,
  `videoFile/b8f269e1…_NSFW_NWSW_news.mp4`.
- No publish was triggered; all created/moved targets remain `pending`.
