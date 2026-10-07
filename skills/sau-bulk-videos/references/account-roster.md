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
| 4 | Teaching | `teaching` | `Teaching` |
| 10 | Money Systems Lab | `money-systems-lab` | (not covered by this skill) |

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

Language follows the **account**, not the file:

- `* Bluesky EN`, `Nakedwill`, `NW_IG`, … → English
- `* Bluesky ZH`, `SW TG 中文`, … → Traditional Chinese

The content guard refuses copy that does not match an account's configured
audience language, so generate each language separately and run the matching
humanizer (`humanizer` for English, `humanizer-zh` for Chinese).
