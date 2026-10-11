# Re-prep slice s4 — verification report

**Slice:** campaigns 2484, 2486, 2487, 2488, 2501
**Role:** read-only verification owner (no prep launched/relaunched; nothing mutated)
**Log polled:** `logs/reprep/slice-s4.txt` (append-only), every 60s
**Result:** ❌ FAILED — 4 of 5 campaigns never emitted a `jobs=` line within the 45-minute window

## 1. Poll result (45 minutes elapsed)

Poll window: 2026-10-11 22:48:23 → 23:32:24 CST (45 iterations, 60s each).

`jobs=` lines present in `logs/reprep/slice-s4.txt`:

```
$ grep -nE "^(2484|2486|2487|2488|2501): jobs=" logs/reprep/slice-s4.txt
6:2484: jobs=7 succeeded=True lease_ok=True (1084s)
```

Only **2484** produced a terminal `jobs=` line. **2486, 2487, 2488, 2501 did not.**
The log stopped receiving writes at 23:05 CST; no further progress through the end
of the window.

Relevant slice-s4 log excerpt:

```
2484: cancelled 8 broken target(s); running production prep...
2484: cancelled 0 broken target(s); running production prep...
Audio extraction / transcription failed for campaign 2484: ffmpeg ... returned non-zero exit status 254.
2484: jobs=7 succeeded=True lease_ok=True (1084s)
2486: cancelled 8 broken target(s); running production prep...
tg_review: media send failed for /app/generated/campaigns/campaign-2484/..._pub.mp4
requests.exceptions.ConnectionError: ('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))
```

## 2. Raw verify command output

Command (run exactly as specified, read-only):

```
$ .venv/bin/python logs/reprep/verify_reprep.py 2484 2486 2487 2488 2501
{"campaign": 2484, "status": "publishing", "live": 7, "artifact": null}
{"campaign": 2486, "status": "preparing", "live": 0, "artifact": null}
{"campaign": 2487, "status": "publishing", "live": 8, "artifact": null}
{"campaign": 2488, "status": "publishing", "live": 8, "artifact": null}
{"campaign": 2501, "status": "publishing", "live": 5, "artifact": null}
EXIT=0
```

## 3. Per-campaign report

| campaign | `jobs=` line | campaign status | pending targets (live) | artifact source | size | ftyp ok | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2484 | yes (`jobs=7 succeeded=True lease_ok=True (1084s)`) | publishing | 7 | none (`artifact: null`) | — | — | **FAILED** (no artifact proven) |
| 2486 | **missing** | preparing | 0 | none | — | — | **FAILED** |
| 2487 | **missing** | publishing | 8 | none | — | — | **FAILED** |
| 2488 | **missing** | publishing | 8 | none | — | — | **FAILED** |
| 2501 | **missing** | publishing | 5 | none | — | — | **FAILED** |

No campaign yielded a resolvable artifact (local or drive): every `artifact` is
`null`, so no size and no `ftyp` check could be performed for any of them.
Note that even 2484, which did reach the `jobs=` terminal line, produced no
provable artifact under `verify_reprep.py`.

## 4. FAILED list (STOP — no fix attempted)

- **2486** — no `jobs=` line within 45 min
- **2487** — no `jobs=` line within 45 min
- **2488** — no `jobs=` line within 45 min
- **2501** — no `jobs=` line within 45 min
- **2484** — `jobs=` line present, but verify resolved no artifact (`artifact: null`)

Per role constraints, no prep was launched/relaunched, no target/campaign was
cancelled or mutated, `pytest` was not run, campaigns 2598–2603 were not touched,
and nothing was committed. Stopping here as instructed.
