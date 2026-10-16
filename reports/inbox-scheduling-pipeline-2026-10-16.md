# Inbox scheduling pipeline — completed

Session `inbox-scheduling-pipeline`. 2026-10-16/17 (host TZ Asia/Taipei).

## Confirmed facts

- **Taipei Pride 2026** = Sat **2026-10-31** (24th Taiwan Pride). Already scheduled
  (campaigns 2598–2601).
- **Kaohsiung Pride 2026** = Sat **2026-12-05** (17th 高雄同志大遊行, 大勇路×河西路口).
  All **pride-invitation copy mentions Kaohsiung Pride on Dec 5**, per operator.
- Rating is filename-driven (`myUtils/content_rating.py`): `…__sfw` → SFW (all
  platforms), `…__nsfw` → NSFW (adult-safe platforms only).

## What was done

### 1. Scheduled the inbox batch (29 videos → 58 campaigns)

Every video in `SAU-Inbox/both/` (root, which the watcher ignores) was:
transcribed (chat-audio STT `mimo-v2.6-flash`), given a human title + polished
summary, staged to `videoFile/`, and submitted through `/publish-center/submit`
with `profileIds:[1,3]` and `options.useLlm` (per-account LLM copy from the
summary). Schedule spread Oct 18 → Nov 15 (pride invites first, before Taipei
Pride).

| scene | title | flag |
|---|---|---|
| 月底遊行 | 月底遊行邀請 | nsfw (zh) |
| timestamp 01 | Taiwan Pride invitation | nsfw + sfw |
| timestamp 02 | Why do people have kids | nsfw + sfw |
| timestamp 03 | Does the right person fix your kinks | nsfw + sfw |
| timestamp 04 | Why I refuse to exercise | nsfw + sfw |
| timestamp 05 | Skinny fat and visceral fat | nsfw + sfw |
| timestamp 06 | Political tribalism and the mental body | nsfw + sfw |
| timestamp 07 | Internet trolls part 1 | nsfw + sfw |
| timestamp 08 | Internet trolls part 2 | nsfw + sfw |
| timestamp 09 | Sex is for pleasure | nsfw + sfw |
| timestamp 10 | Fear of being exposed | nsfw + sfw |
| timestamp 11 | Naked truth and Pride invite | 2×nsfw + sfw |
| named | Why do people raise a child | nsfw + sfw |
| both/video | Face exercises funny video | sfw |
| both/video | Queer spaces under attack (Taipei Raid) | sfw |
| both/video | Elite athletes on OnlyFans | sfw |

The 4 operator-written `.docx` transcripts (am I lovable, Marriage, 月底遊行,
痔瘡遊行) were polished (fixing transcription errors) and their summaries are in
`sau-inbox/polished_summaries.md`.

### 2. Fixed "published video in sau-inbox"

Already-published/scheduled sources were moved out of the Drive inbox to
`sau/published/videoFile/` (server-side `rclone moveto`, never deleted):
`SFW Taipei Stonewall.mp4` (campaign 2490), `SFW/NSFW NW+SW invitation to gay
pride` (campaigns 2598–2601), and the two old `both/video/` test clips
`NSFW.mp4`/`SFW.mp4`. `state.json` `pending` is now **0**; the two published
invitations were also removed from `pending`/`processed`.

A duplicate `月底遊行` submission (campaigns 2607/2608, from a test run) was
cancelled — 5 targets cancelled, campaigns marked `cancelled`.

### 3. Skill pipeline — archive rename + metadata

`skills/sau-bulk-videos/SKILL.md` gained section **6b "Archive + metadata"**:
after publish, rename the video in the Drive archive to
`<Title>_<SFW|NSFW>_<YYYYMMDD>` and write a same-name `.txt` summary beside it.
Applied live: all 29 videos are archived in `sau/published/videoFile/` with a
same-name `.txt` (the polished summary) beside each.

## State

- 58 campaigns created (29 videos × 2 profiles). 11 `publishing`, 47 `preparing`
  (worker preps asynchronously — transcode, watermark, per-account drafts), 2
  `cancelled` (the duplicate).
- 91 pending targets already queued, scheduled `2026-10-18 → 2026-11-23`
  (respecting ≥30-min gap / ≤3 per-account-per-day via the allocator).
- Drive inbox is clean: `SAU-Inbox/both/` holds only the README + the 4 `.docx`
  transcripts; `both/video/` is empty.

## Notes / caveats

- The deploy container (`social-auto-upload`) was recreated several times during
  the run (watchtower / ci-watch), which interrupted staging/archive once; all
  failures were re-run and completed.
- Some campaign preps auto-transcribe and intermittently hit `520` from the STT
  gateway; the summary `brief` is the fallback, so copy still generates.
- `nw/video/` and `sw/video/` still hold the older Sept backlog (already
  `processed` in `state.json`, out of scope for this session).
- Working scripts left in `/home/will/sau-inbox/` for re-runs: `batch_stt.py`,
  `stt_video.py`, `schedule.py`, `archive_originals.py`, `upload_all_metadata.py`,
  `build_plan.py`, `schedule_plan.json`.
