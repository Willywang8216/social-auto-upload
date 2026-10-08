# Account roster (what each prefix actually expands to)

Derived live from the accounts table. Regenerate after adding or disabling an
account:

```bash
sqlite3 -header db/database.db "
SELECT a.profile_id, p.name AS profile, a.id, a.platform, a.account_name
FROM accounts a JOIN profiles p ON p.id = a.profile_id
WHERE a.enabled = 1 ORDER BY a.profile_id, a.platform;"
```

## Profile ids

| id | profile | slug | prefix used in filenames |
|---:|---|---|---|
| 1 | NW (Nakedwill) | `nw` | `SFW NW` / `NSFW NW` |
| 3 | SW (Sexualwill) | `sw` | `SFW SW` / `NSFW SW` |
| 1 + 3 | both personas | — | `SFW NW+SW` / `NSFW NW+SW` |
| 4 | Teaching | `teaching` | `Teaching` |
| 10 | Money Systems Lab | `money-systems-lab` | (not covered by this skill) |

`NW+SW` is one submission with `profileIds: [1, 3]` — `submit_publish` iterates
over every profile id, so there is no need to submit twice.

## Language routing (`audience_language` per account)

Language is a property of the **account**, not the clip. Both profiles publish
an English clip to their English accounts *and* their Traditional Chinese
accounts — they are two sets, both receiving the same media with different copy.

### Nakedwill (profile 1)

| language | accounts |
|---|---|
| `en` | facebook `Nakedwill` · instagram `NW_IG` · nw_sw_blog `NW Blog` · reddit `Nakedwill Reddit` · tiktok `Nakedwill_TK` · threads `NWthreads` · youtube `Itswill_YT` · twitter `NW X (model_will)` · bluesky `NW Bluesky EN` · telegram `NW TG 本人` |
| `zh-Hant` | bluesky `NW Bluesky ZH` · telegram `NW TG 中文` · twitter `NW X (nudeweiwei)` |

### Sexualwill (profile 3)

| language | accounts |
|---|---|
| `en` | facebook `SW-FB` · instagram `SW_IG` · reddit `Sexualwill Reddit OAuth` · threads `SW_threads` · twitter `sexualwill` · bluesky `SW Bluesky EN` · telegram `SW TG 本人` |
| `zh-Hant` | bluesky `SW Bluesky ZH` · telegram `SW TG 中文` · twitter `光光` |
| `en,zh-Hant` | nw_sw_blog `SW Blog` (bilingual — accepts either script) |

### Rules

- Generate copy **in the account's language** before submitting, then humanize
  with the matching skill: `humanizer` (English), `humanizer-zh` (繁中).
- **Traditional characters only** on `zh-Hant` accounts; Simplified is rejected.
- A bilingual account (`en,zh-Hant`) accepts either script.
- The content guard enforces this, so mismatched copy fails fast with
  `[content-guard] copy does not match account language`. Read that error as
  "write this account's language", not as a delivery fault.

## Platforms that reject nudity

These are dropped for any `NSFW` file, on every profile:

```
instagram, facebook, threads, youtube, tiktok
```

Authoritative list: `myUtils.content_rating.NSFW_RESTRICTED_PLATFORMS`.

## `SFW NW` — all NW accounts

bluesky ×2 · facebook · instagram · nw_sw_blog · reddit · telegram ×2 ·
threads · tiktok · twitter ×2 · youtube

## `NSFW NW` — NW minus the restricted platforms

bluesky ×2 · nw_sw_blog · reddit · telegram ×2 · twitter ×2

## `SFW SW` — all SW accounts

bluesky ×2 · facebook · instagram · nw_sw_blog · reddit · telegram ×2 ·
threads · twitter ×2

## `NSFW SW` — SW minus the restricted platforms

bluesky ×2 · nw_sw_blog · reddit · telegram ×2 · twitter ×2

## `Teaching`

bluesky · facebook · instagram · teaching_blog · threads · tiktok · twitter

## Per-platform constraints that change routing

| platform | constraint | consequence |
|---|---|---|
| threads | video ≤ 300 s | drop or cut longer clips |
| twitter (API) | media upload needs API credits; 140 s limit on the API path | falls back to Sociamonials when depleted |
| youtube | requires video | skip image-only items |
| tiktok | requires video, or Direct Post for photos | skip photo items unless enabled |
| instagram | 250 MB cap | publisher shrinks larger files |
| bluesky | 300 MB cap; images downscaled under ~900 KB | automatic |
| reddit | some subs need a flair, or whitelist links | configured to self-post where required |

## Language routing

Covered above in detail; the short version is that language follows the
**account**, and every clip is published to both the English and Traditional
Chinese account sets (not one or the other). `SW Blog` is bilingual.

## Getting media to the app

The app is deployed at `socialupload.iamwillywang.com` (container on the Email
VPS). From any client the route is HTTP; the container/Drive routes below are for
the **deploy host** only.

### From a client (default)

```bash
curl -s -X POST "$SAU_API/upload" \
  -H "Authorization: Bearer $SAU_TOKEN" \
  -F "file=@/path/to/<clip>.mp4"
# -> {"code":200,"data":"<uuid>_<name>.mp4"}
# use "videoFile/<data>" in mediaFilePaths
```

For large files, `POST /upload/direct` + `POST /upload/register`, or the
`/upload/multipart/*` trio.

### On the deploy host (archive route)

Media must be readable **by the container**:

| path | mechanism |
|---|---|
| `/app/videoFile` | bind mount — container writes are visible on the host |
| `/app/rclone-cache.conf` | rclone config with the **`GDrive-willywang8216`** remote, read **and write** |

Verify Drive from inside the container, never from the host (the host shell may
have no rclone config at all, which is expected):

```bash
docker exec social-auto-upload sh -c \
  "RCLONE_CONFIG=/app/rclone-cache.conf rclone lsf 'GDrive-willywang8216:sau/'"
# -> assets/  generated/  uploads/  videoFile/
```

Preferred route, because Drive is already the archive/offload target
(`offload_to_drive.sh` → `GDrive-willywang8216:sau`):

```bash
# 1) local -> Drive archive (survives a re-run; no re-upload)
rclone copy "<local folder>" "GDrive-willywang8216:sau/videoFile/<batch>/" -P

# 2) Drive -> container (single hop, container's own config)
docker exec social-auto-upload sh -c \
  "RCLONE_CONFIG=/app/rclone-cache.conf rclone copy \
   'GDrive-willywang8216:sau/videoFile/<batch>/' /app/videoFile/<batch>/ -P"
```

The container prints `Failed to save config ... device or resource busy` on every
rclone call (read-only bind mount). Harmless — the transfer still runs.

## Transcription

`whisper-1` via the configured LLM pool (`myUtils/llm_client.transcribe_audio`).
It runs on the **submit** path (`_prepare_campaign_media_artifacts`), not on
preview — `_build_preview_media_context` returns an empty `transcriptText`. Enable
in a submit with `options.transcribe: true`; it also runs automatically when an
LLM is configured and no transcript exists.
