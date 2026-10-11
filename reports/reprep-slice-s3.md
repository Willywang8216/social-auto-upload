# Re-prep slice s3 — verification (READ-ONLY)

Slice campaigns: 2471 2472 2481 2482 2483
Poller: logs/reprep/slice-s3.txt (append-only), 60s interval, 45-minute budget.
Result: 45 minutes elapsed with **4/5** `jobs=` lines (2471, 2472, 2481, 2482).
2483 never emitted a `jobs=` line.

## Raw verify command

Command (exact):
```
.venv/bin/python logs/reprep/verify_reprep.py 2471 2472 2481 2482 2483
```

Output:
```json
{"campaign": 2471, "status": "publishing", "live": 7, "artifact": null}
{"campaign": 2472, "status": "publishing", "live": 7, "artifact": null}
{"campaign": 2481, "status": "publishing", "live": 7, "artifact": {"target": 6031, "target_status": "pending", "artifact_kind": "local", "source": "local", "size": 32272061, "head12": "000000206674797069736f6d", "ftyp": true, "ok": true}}
{"campaign": 2482, "status": "publishing", "live": 7, "artifact": {"target": 6047, "target_status": "pending", "artifact_kind": "local", "source": "local", "size": 18094287, "head12": "000000206674797069736f6d", "ftyp": true, "ok": true}}
{"campaign": 2483, "status": "preparing", "live": 0, "artifact": null}
```
Exit code: 0

## Per-campaign report

| campaign | status | live/pending targets | artifact source | size | ftyp ok | verdict |
|---|---|---|---|---|---|---|
| 2471 | publishing | 7 | none (artifact null) | — | — | FAILED (artifact unresolved) |
| 2472 | publishing | 7 | none (artifact null) | — | — | FAILED (artifact unresolved) |
| 2481 | publishing | 7 | local | 32,272,061 | true | PASS |
| 2482 | publishing | 7 | local | 18,094,287 | true | PASS |
| 2483 | preparing | 0 | none (artifact null) | — | — | FAILED (no `jobs=` line) |

## `jobs=` lines observed in logs/reprep/slice-s3.txt

```
2471: jobs=7 succeeded=True lease_ok=True (721s)
2472: jobs=7 succeeded=True lease_ok=True (487s)
2481: jobs=7 succeeded=True lease_ok=True (804s)
2482: jobs=7 succeeded=True lease_ok=True (368s)
```
(no `2483: jobs=` line)

## Verdict

**FAILED — stopping per instructions (verify owner is read-only, no fix attempted).**

- 2483: missing `jobs=` line (still `preparing`, 0 live targets).
- 2471: verify returned `artifact: null` — no live artifact could be resolved/proven.
- 2472: verify returned `artifact: null` — no live artifact could be resolved/proven.
- 2481, 2482: verified — local MP4, ftyp ok.

No prep was launched, no target/campaign was mutated, no pytest, no commit.
