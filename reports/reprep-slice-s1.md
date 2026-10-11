# Reprep Slice s1 — Verification Report (READ-ONLY)

Campaigns: 2455 2458 2459 2460 2461
Log: `logs/reprep/slice-s1.txt` (append-only)
Polling: every 60s, stopped after 45 minutes (2700s) elapsed.
Result: only **1/5** campaigns produced a `jobs=` line within the window.

## Raw slice-s1.txt at end of poll window

```
2458: cancelled 8 broken target(s); running production prep...
[browser_helper] Patched patchright chromium.launch -> connect_over_cdp(ws://browserless:3000)
2455: cancelled 0 broken target(s); running production prep...
[browser_helper] Patched patchright chromium.launch -> connect_over_cdp(ws://browserless:3000)
Audio extraction / transcription failed for campaign 2455: Command '['ffmpeg', '-y', '-i', '/app/videoFile/_inbox_cache/b53d7db1688d44c1842a52722ccf1c1f_20260907083916866__nsfw.mp4', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '/app/generated/campaigns/campaign-2455/audio/b53d7db1688d44c1842a52722ccf1c1f_20260907083916866__nsfw-audio.wav']' returned non-zero exit status 254.
2455: jobs=13 succeeded=True lease_ok=True (2558s)
2458: cancelled 0 broken target(s); running production prep...
```

## Raw verify command output

Command: `.venv/bin/python logs/reprep/verify_reprep.py 2455 2458 2459 2460 2461`

```
{"campaign": 2455, "status": "publishing", "live": 13, "artifact": {"target": 6055, "target_status": "pending", "artifact_kind": "local", "source": "local", "size": 76035977, "head12": "000000206674797069736f6d", "ftyp": true, "ok": true}}
{"campaign": 2458, "status": "preparing", "live": 0, "artifact": null}
{"campaign": 2459, "status": "publishing", "live": 8, "artifact": null}
{"campaign": 2460, "status": "publishing", "live": 8, "artifact": null}
{"campaign": 2461, "status": "publishing", "live": 8, "artifact": null}
```

## Per-campaign results

| campaign | status | pending target count | artifact source | size | ftyp ok | jobs= line | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2455 | publishing | 13 (`live`) | local | 76,035,977 | true | yes (`jobs=13 succeeded=True lease_ok=True (2558s)`) | OK |
| 2458 | preparing | 0 (`live`) | none (`artifact: null`) | n/a | n/a | **missing** | **FAILED** |
| 2459 | publishing | 8 (`live`) | none (`artifact: null`) | n/a | n/a | **missing** | **FAILED** |
| 2460 | publishing | 8 (`live`) | none (`artifact: null`) | n/a | n/a | **missing** | **FAILED** |
| 2461 | publishing | 8 (`live`) | none (`artifact: null`) | n/a | n/a | **missing** | **FAILED** |

## Summary

**FAILED:** 2458, 2459, 2460, 2461 — each is missing a `jobs=` line (verify also returned `artifact: null`, so no `ok: true` proof).

**PASS:** 2455 — `jobs=13 succeeded=True lease_ok=True (2558s)`, live=13, artifact local MP4 76,035,977 bytes, `ftyp` header present, `ok: true`.

Per the read-only mandate: STOPPED. No fix attempted, no prep launched/relaunched, no targets/campaigns mutated, nothing committed.
