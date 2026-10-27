# SAU-Inbox watcher (versioned copy)

`/home/will/sau-inbox/` is the **live** deployment: cron runs `watch.py --once`
every minute as the `will` user, `inbox_monitor.py` every 10 minutes, and the
state (`state.json`) plus credentials (`.env`) live beside them. That directory is
not a git repository, so until 2026-10-27 the watcher's source had **no history,
no review and no tests in CI** — which is how a missing `timedelta` import
crashed every scan for days without anything failing a build.

This directory is the versioned copy. Keep it byte-identical to the live files.

| file | role |
| --- | --- |
| `watch.py` | the scanner: list Drive → parse name → thumbnail → `state.json` → Telegram |
| `thumb.py` | thumbnail / NSFW title-card rendering (Pillow) |
| `manual_platforms.py` | per-persona "post manually" notes appended to the Telegram card |
| `gen_title.py` | optional LLM title suggestion for untitled uploads |
| `inbox_monitor.py` | watchdog: alerts when the watcher stops succeeding, or when an item sits unapproved |

## Deploying a change

```bash
# repo -> live (after reviewing the diff and running the tests)
cp sau_inbox/*.py /home/will/sau-inbox/
python -m pytest tests/test_inbox_watcher_sync.py -q     # must pass

# live -> repo (after an emergency edit on the host)
cp /home/will/sau-inbox/*.py sau_inbox/
```

`tests/test_inbox_watcher_sync.py` fails when the two copies drift (it is skipped
when the live directory is absent, e.g. inside a CI container), and it runs the
undefined-name checker over this directory so a missing import is caught by the
build instead of by a silent crash loop.

## What the live directory also contains (never commit these)

`state.json` (the queue), `.watch-health.json`, `.monitor-state.json`, `.env`
(Telegram credentials), `watch.log*`, and the persona folders
(`nw/ sw/ both/ teaching/ msl/`) with the downloaded thumbnails.
