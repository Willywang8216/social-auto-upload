# Best posting times — 2026 (audience: global EN + Traditional Chinese / Taiwan)

Compiled by session `x-credits` after the `posting-times` subagent errored before
writing its file. Method: live Tavily searches (OpenTweet 50k tweets, Sprout
Social ~2bn engagements, Buffer 3M Bluesky posts, Publora/SocialBu 2026 guides)
plus the audience-overlap arithmetic. Times are **UTC**; the two audiences are a
global English audience (US ET anchor) and Taiwan (UTC+8).

## Audience overlap (the windows that matter)

| UTC window | Taipei | US ET | US PT | Fits |
|---|---|---|---|---|
| 12:00–14:00 | 20:00–22:00 | 08:00–10:00 | 05:00–07:00 | Taiwan evening + US morning |
| 16:00–19:00 | 00:00–03:00 | 12:00–15:00 | 09:00–12:00 | US midday only |
| 22:00–01:00 | 06:00–09:00 | 18:00–21:00 | 15:00–18:00 | US evening + Taiwan morning |
| 04:00–05:00 | 12:00–13:00 | 00:00–01:00 | 21:00–22:00 | Taiwan midday |

## Per-platform recommendation

| Platform | Best UTC windows | Reason |
|---|---|---|
| X / Twitter | 13–15, 23–00 | OpenTweet/Sprout: Tue–Thu 9–11 ET and 12–18 ET peaks |
| Bluesky | 13–15, 22–00 | weekday 08–10 & 17–19 local; Buffer peak Sat 17:00 |
| Facebook | 12–14, 23–00 | Taiwan evening + US morning; midweek strongest |
| Instagram | 12–14, 23–00 | Taiwan 20–22 peak; US evening secondary |
| Threads | 12–14, 23–00 | mirrors Instagram; replies boost reach |
| TikTok | 13–15, 23–00 | Tue/Thu 14–17 local after-work peak |
| YouTube | 14–16 | US business/daytime publishing |
| Reddit | 13–15, 22–23 | US afternoon/evening browse peak |
| Telegram | 12–14, 22 | Taiwan evening + US morning |
| LinkedIn | 13–15 | US business hours, Tue–Thu |
| Pinterest | 12–14 | daytime planning/scroll |

## Algorithm notes that shape the schedule

- **First-hour engagement** decides reach on X, Instagram, TikTok and Threads —
  post when the audience is awake, not when the queue is empty.
- **X links** are penalised and are rejected in the main post via Sociamonials;
  keep them in the first comment.
- **Threads** rewards replies; the first comment pattern is already supported.
- **Bluesky** has a per-account daily video cap (25 videos / 10 GB); the
  scheduler caps at 3 posts/account/day regardless.
- Midweek (Tue–Thu) outperforms weekends on X/LinkedIn; weekends are strong on
  Bluesky. The calendar is daily, so time-of-day is the primary lever here.

## Implementation

`scripts/optimize_schedule.py` moves pending targets into
`PREFERRED_UTC_HOURS` per platform, never earlier than their existing date,
keeping the anti-spam rules (≥30 min between an account's posts, ≤3/day).
Dry-run is the default.

Sources: OpenTweet 2026 X study; Sprout Social 2026 (2bn engagements); Buffer
2026 State of Social (3M Bluesky posts); Publora / SocialBu 2026 timing guides.
