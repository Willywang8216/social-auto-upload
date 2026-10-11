# Re-prep verification report — slice s2

**Campaigns:** 2462, 2464, 2468, 2469, 2470
**Mode:** READ-ONLY. No prep launched/relaunched, no target/campaign mutated, no commit.
**Poll window:** 20:48:17 → 23:33:18 (waited the full 45 minutes) — `logs/reprep/slice-s2.txt`.
**Verdict:** **FAILED** — 4 of 5 campaigns never produced a `jobs=` line.

## Step 1 — poll `logs/reprep/slice-s2.txt`

Waited 45 min (no early exit): only **2462** gained a `jobs=` line. Final slice log:

```
2463: cancelled 8 broken target(s); running production prep...
[browser_helper] Patched patchright chromium.launch -> connect_over_cdp(ws://browserless:3000)
Audio extraction / transcription failed for campaign 2463: 403 Client Error: Forbidden for url: https://muyuan.do/v1/audio/transcriptions
2463: jobs=7 succeeded=True lease_ok=True (1224s)
2464: cancelled 8 broken target(s); running production prep...
2462: cancelled 8 broken target(s); running production prep...
[browser_helper] Patched patchright chromium.launch -> connect_over_cdp(ws://browserless:3000)
Audio extraction / transcription failed for campaign 2462: Command '['ffmpeg', '-y', '-i', '/app/videoFile/_inbox_cache/d38c344400e343c082c0372c2057b4af_20260830143915195__nsfw.mp4', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '/app/generated/campaigns/campaign-2462/audio/d38c344400e343c082c0372c2057b4af_20260830143915195__nsfw-audio.wav']' returned non-zero exit status 254.
2462: jobs=9 succeeded=True lease_ok=True (2611s)
2464: cancelled 0 broken target(s); running production prep...
```

`jobs=` line present: **2462** only. Still running / never started: **2464, 2468, 2469, 2470**.

## Step 2 — `.venv/bin/python logs/reprep/verify_reprep.py 2462 2464 2468 2469 2470`

Raw output:

```
{"campaign": 2462, "status": "publishing", "live": 9, "artifact": {"target": 6059, "target_status": "pending", "artifact_kind": "local", "source": "local", "size": 68037785, "head12": "000000206674797069736f6d", "ftyp": true, "ok": true}}
{"campaign": 2464, "status": "preparing", "live": 0, "artifact": null}
{"campaign": 2468, "status": "publishing", "live": 8, "artifact": null}
{"campaign": 2469, "status": "publishing", "live": 8, "artifact": null}
{"campaign": 2470, "status": "publishing", "live": 8, "artifact": null}
```

Exit code: 0

## Step 3 — per-campaign summary

| campaign | status | pending/live targets | artifact source | size (bytes) | ftyp | ok | `jobs=` line |
|---|---|---|---|---|---|---|---|
| 2462 | publishing | 9 | local | 68,037,785 | true | true | yes |
| 2464 | preparing | 0 | — (null) | — | — | — | **missing** |
| 2468 | publishing | 8 | — (null) | — | — | — | **missing** |
| 2469 | publishing | 8 | — (null) | — | — | — | **missing** |
| 2470 | publishing | 8 | — (null) | — | — | — | **missing** |

## FAILED

- **2464** — no `jobs=` line within the 45-min window; verify shows `artifact: null` (`ok` not true). Status still `preparing`.
- **2468** — no `jobs=` line; verify shows `artifact: null`.
- **2469** — no `jobs=` line; verify shows `artifact: null`.
- **2470** — no `jobs=` line; verify shows `artifact: null`.

Only **2462** passed (`jobs=` line present, artifact proven real: local MP4 68,037,785 bytes with `ftyp` at offset 4, `ok: true`).

Per instructions: STOP. No fix attempted. No prep launched/relaunched, no pytest, no campaign/target mutation, no commit.
