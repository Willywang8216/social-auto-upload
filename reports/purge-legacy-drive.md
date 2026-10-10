# Purge legacy untiered Drive: classification, quarantine proof, and restore proof

Date: 2026-10-10 · Executed by the purge session, against the live Drive
(`GDrive-willywang8216`) and `db/database.db` at HEAD `262f8d8`.

Companion to `reports/legacy-drive-analysis-2026-10-10.md` (the read-only
analysis). This report **acts** on that analysis: it rebuilds the listing and
reference sets, reclassifies every legacy object, proves the quarantine
mechanism on 5 objects, and proves 5 NEEDED assets still restore.

Tool: `scripts/purge_legacy_drive.py` (new, this session). Dry-run by default.

**Scope note:** the full 208-object apply was **not** run. Only a 5-object
limited run was executed, to prove the mechanism. See
[What is left for the operator](#what-is-left-for-the-operator).

---

## 1. How each object is classified

The classifier is `scripts/purge_legacy_drive.py`:

- `LIVE = publish_job_targets.status IN (pending, retrying, running)` —
  `scripts/purge_legacy_drive.py:95`
- reference sets from the DB, always rebuilt, never cached —
  `load_reference_sets()` at `scripts/purge_legacy_drive.py:208`
- a key is matched by its **Drive key** (path relative to the legacy root);
  `file_records` keys accept both `uploads/<x>` and `<x>` spellings, mirroring
  `scripts/migrate_drive_layout.py` — `_record_key_candidates()` at
  `scripts/purge_legacy_drive.py:191`
- **strong** references: a `file_records` row (key + legacy backend), a live
  payload `artifacts[].local_path`/`.public_url`, or a `campaign_artifacts` row
  whose campaign has ≥1 live target
- **weak** reference: basename match against a live payload/campaign key only —
  `WEAK_PROTECT_LIVE = True` at `scripts/purge_legacy_drive.py:103`
- `needed = strong OR weak` — `scripts/purge_legacy_drive.py:335`
- refuse to run when all reference sets are empty —
  `scripts/purge_legacy_drive.py:522`

The legacy roots come from `myUtils/drive_layout.legacy_endpoint()`
(`myUtils/drive_layout.py:76`); the remote is `drive_layout.DEFAULT_REMOTE`
(`myUtils/drive_layout.py:58`).

### Reproduced operator split

Command (rebuilds the listing live; took ~8 min because `--check-tiered` also
lists 6 tiered trees):

```bash
cd /home/will/social-auto-upload
python3 scripts/purge_legacy_drive.py --report-basenames --check-tiered --list-actions 5
```

Real output:

```
==============================================================================
legacy Drive purge report — remote=GDrive-willywang8216 mode=DRY-RUN
==============================================================================
reference sets (from DB):
  file_records         671
  live_payload_keys    468
  live_campaign_keys   510
  live_campaigns       429
  live_targets         1334

classification per legacy root:
  root          objects   needed   unused    unused bytes
  videoFile         650      445      205  15,593,228,586
  uploads             4        1        3   1,026,159,060
  generated         483      483        0               0
  TOTAL            1137      929      208  16,619,387,646

orphan detail (not mapped by file_records):
  videoFile    orphans=453   live_payload=244   (134,078,735 B) live_campaign=248   (267,825,151 B) weak_only=0
  uploads      orphans=3     live_payload=0     (0 B) live_campaign=0     (0 B) weak_only=0
  generated    orphans=11    live_payload=9     (377,891,399 B) live_campaign=10    (407,712,834 B) weak_only=1
```

This reproduces the operator's split exactly for `videoFile` and `uploads`, and
the 208 total / ≈16.6 GB:

| root | orphans | live payload | live campaign | unused |
| --- | ---: | ---: | ---: | ---: |
| `sau/videoFile` | 453 | 244 (134 MB) | 248 (268 MB) | 205 (15,593 MB) |
| `sau/uploads` | 3 | 0 | 0 | 3 (1,026 MB) |
| `sau/generated` | 11 | 9 + 1 weak (377 MB) | 10 + 1 weak (407 MB) | **0** |
| **total** | 467 | | | **208 (16,619,387,646 B ≈ 16.6 GB)** |

### The one measured discrepancy (explained, not hidden)

The analysis report says `sau/generated` has "11 live payload / 11 live
campaign". The exact-key count is **9 live payload / 10 live campaign**, and the
11th orphan is kept only by the **basename** weak signal. It is:

```
campaigns/campaign-2528/watermarked_video/2fd718ad-c13c-4200-8e46-8f372cebbfd8_NSFW_NWSW_morning_talk_2-watermarked_video.mp4
```

Its basename appears in a live payload/`campaign_artifacts` row, but under a
re-rooted path. Because it is a live reference, `classify()` keeps it
(`needed=True`, `weak=live_campaign_basename`), which is why `generated` unused
is **0** — matching the operator's headline number. The classifier's exact-key
counts are the honest sub-counts.

### Listing artifact: 2 duplicate Drive names

`rclone lsjson -R` returns **485** entries for `sau/generated` but only **483**
distinct paths: two objects appear twice under the same name
(`campaigns/campaign-2464/audio/…wav`, `campaigns/campaign-2462/audio/…wav`).
The classifier deduplicates by path (`scripts/purge_legacy_drive.py`,
`classify()`), and `--apply` refuses to touch a path with a duplicate name
(`skipped_duplicate_name`), because a path-based `copyto`/`deletefile` cannot
name one of two same-named Drive objects unambiguously. Neither duplicate is in
the unused set (both are `file_records`-mapped), so this does not affect the 208.

### 17 unused objects that basename-match a `file_records` key

`--report-basenames` lists 17 unused `videoFile` objects whose basename matches a
`file_records` key under a *different* drive key (possible duplicates). Per the
operator's measured definition these are counted unused and appear in the 208;
they are listed here so the operator can review them before the full apply:

```
basename review: 17 unused object(s) whose basename matches a file_records key under another drive key (counted unused):
  videoFile/_library/SW/20260206033405887(6).jpg  (153,983 B)
  videoFile/_library/SW/20260206033405887(7).jpg  (152,306 B)
  videoFile/_library/SW/20260206033405887(8).jpg  (154,538 B)
  videoFile/_library/SW/20260206033405887.jpg  (100,028 B)
  videoFile/_library/SW/action_20260206033041242_sys.jpg  (116,026 B)
  videoFile/_library/SW/截圖1.jpg  (71,357 B)
  ... (12 more 截圖*.jpg)
```

### NEEDED objects absent from the tiered trees

`--check-tiered` listed `sau/inbox/<root>` and `sau/published/<root>` and
compared them to the 929 NEEDED keys:

```
NEEDED objects absent from sau/inbox|published: 915 (they live in the legacy tree and stay there; restore is unaffected)
```

Only 14 of the 929 NEEDED objects are already in a tiered tree. The other 915
stay where they are — this script never touches a NEEDED object. Promoting them
is `scripts/migrate_drive_layout.py`'s job, not this purge's.

---

## 2. Quarantine mechanism (chosen over delete)

For every UNUSED object, `--apply` performs a **reversible move**, not a delete
(`quarantine()` at `scripts/purge_legacy_drive.py:380`):

1. `rclone copyto <remote>:sau/<root>/<key> <remote>:sau/trash/<UTC-date>/<root>/<key>`
2. `rclone lsjson <destination>` and require `Size == source Size`
3. only then `rclone deletefile <remote>:sau/<root>/<key>`

Rationale (why quarantine, per the operator's instruction and the analysis):
the Drive is shared. `rclone delete` is irreversible; a
`sau/trash/<date>/` prefix can be moved back in one command if "unused" was
wrong. Same-remote `copyto` is a server-side copy, so no bytes are re-uploaded.
The exact command per object is printed in dry-run.

---

## 3. Five-object apply proof

```bash
python3 scripts/purge_legacy_drive.py --apply --limit 5 --list-actions 10
```

Real output (tail):

```
APPLY: selected 5 unused object(s) for quarantine under sau/trash/2026-10-10
  videoFile/07089dab-863a-486a-8beb-fab413d3eb49_NSFW_NWSW.mp4  (2,550,895 B) -> GDrive-willywang8216:sau/trash/2026-10-10/videoFile/07089dab-863a-486a-8beb-fab413d3eb49_NSFW_NWSW.mp4
      $ rclone --config /home/will/.config/rclone/rclone.conf copyto "GDrive-willywang8216:sau/videoFile/07089dab-863a-486a-8beb-fab413d3eb49_NSFW_NWSW.mp4" "GDrive-willywang8216:sau/trash/2026-10-10/videoFile/07089dab-863a-486a-8beb-fab413d3eb49_NSFW_NWSW.mp4"
      $ rclone --config /home/will/.config/rclone/rclone.conf lsjson "GDrive-willywang8216:sau/trash/2026-10-10/videoFile/07089dab-863a-486a-8beb-fab413d3eb49_NSFW_NWSW.mp4"
      $ rclone --config /home/will/.config/rclone/rclone.conf deletefile "GDrive-willywang8216:sau/videoFile/07089dab-863a-486a-8beb-fab413d3eb49_NSFW_NWSW.mp4"
  ... (4 more)
apply results:
  quarantined: 5
```

Destination listing (all 5 present, sizes match the source listing exactly):

```
$ rclone lsjson -R GDrive-willywang8216:sau/trash/2026-10-10
           0  videoFile
  59,760,666  videoFile/312c967e-a381-422f-a683-0976cea432f6_SFW_NWSW_nakedwill_news_2_sfw.mp4
  91,042,314  videoFile/378c5466-d486-4d89-afa8-3a71d84d347a_SFW_NWSW_3_sfw.mp4
   2,550,895  videoFile/07089dab-863a-486a-8beb-fab413d3eb49_NSFW_NWSW.mp4
  42,235,843  videoFile/2fd718ad-c13c-4200-8e46-8f372cebbfd8_NSFW_NWSW_morning_talk_2.mp4
     988,559  videoFile/0b9f1520-9168-4959-828a-49c2d5429666_demo.mp4
```

Size match against the pre-move listing (`/tmp/legacy_videoFile.json` captured
this session):

```
dest=  59,760,666  source=  59,760,666  match=True  312c967e-..._SFW_NWSW_nakedwill_news_2_sfw.mp4
dest=  91,042,314  source=  91,042,314  match=True  378c5466-..._SFW_NWSW_3_sfw.mp4
dest=   2,550,895  source=   2,550,895  match=True  07089dab-..._NSFW_NWSW.mp4
dest=  42,235,843  source=  42,235,843  match=True  2fd718ad-..._NSFW_NWSW_morning_talk_2.mp4
dest=     988,559  source=     988,559  match=True  0b9f1520-..._demo.mp4
```

Source removal verified — `rclone lsf` on each old path returns rc=3 and 0 lines:

```
lsf_rc=3 exists_lines=0 07089dab-863a-486a-8beb-fab413d3eb49_NSFW_NWSW.mp4
lsf_rc=3 exists_lines=0 0b9f1520-9168-4959-828a-49c2d5429666_demo.mp4
lsf_rc=3 exists_lines=0 2fd718ad-c13c-4200-8e46-8f372cebbfd8_NSFW_NWSW_morning_talk_2.mp4
lsf_rc=3 exists_lines=0 312c967e-a381-422f-a683-0976cea432f6_SFW_NWSW_nakedwill_news_2_sfw.mp4
lsf_rc=3 exists_lines=0 378c5466-d486-4d89-afa8-3a71d84d347a_SFW_NWSW_3_sfw.mp4
```

Integrity proof — one moved object downloaded from quarantine and decoded:

```
$ rclone copyto GDrive-willywang8216:sau/trash/2026-10-10/videoFile/378c5466-..._SFW_NWSW_3_sfw.mp4 /tmp/intact_proof.mp4
-rw-rw-r-- 1 will will 91042314 Oct  8 15:25 /tmp/intact_proof.mp4
e8a9b2aead16ec24248a90ed688b961a  /tmp/intact_proof.mp4
format_name=mov,mp4,m4a,3gp,3g2,mj2
duration=201.479021
size=91042314
```

Total quarantined in this proof run: **196,578,277 bytes** (5 objects).

---

## 4. Restore proof for NEEDED objects

Five NEEDED objects across all three legacy roots were fetched with the real
restore path, `myUtils.media_remote_storage.download_from_backend()`
(`myUtils/media_remote_storage.py:154`), which for `provider='rclone'` calls
`myUtils/rclone_storage.py:143 download_artifact()` with
`remote_name=backend.bucket` and `remote_root=backend.endpoint`. The endpoint and
`storage_key` come straight from the `file_records` row; no path was hand-built.

```python
from myUtils import media_remote_storage
media_remote_storage.download_from_backend(backend_row, storage_key, dest)
```

Real output:

```
--- file_record 98: endpoint=sau/videoFile key='_inbox/pilot__sfw__video.mp4'
    RESTORED 1,041,266 bytes  md5=7bd166d1169678cfc7f0372eb6fdf399
--- file_record 930: endpoint=sau/videoFile key='085c3bbf-7722-4f4e-ae04-21819bd24013_SFW_SW_parade.mp4'
    RESTORED 8,591,687 bytes  md5=8f1b03efc3fc9efa263896a1a47bb3b9
--- file_record 83: endpoint=sau/uploads key='b8fac499-c723-4c59-8d6e-502173e75648_vlcsnap-2026-06-30-00h03m21s191.png'
    RESTORED 1,768,386 bytes  md5=df76d51066a109f797d20a290d3b5ae4
--- file_record 1191: endpoint=sau/generated key='campaigns/campaign-2573/watermarked_video/e2235267-d3c5-4d6a-a284-1d427c79df58_SFW_NWSW_3_sfw-watermarked_video.mp4'
    RESTORED 262,192 bytes  md5=eece4fb2756c81d3a7e42a26efd63950
--- file_record 1070: endpoint=sau/generated key='campaigns/campaign-2540/watermarked_video/b8f269e1-6809-46c7-a74e-c084c2134ad0_NSFW_NWSW_news-watermarked_video.mp4'
    RESTORED 786,480 bytes  md5=de781ff18c51319a89bedf849aa40e70
```

Every restored size equals the recorded `file_records.filesize` (record 930 is
also cited by a live target payload `source_file_record_id=930`). These are the
real assets of scheduled posts, and they restore.

---

## 5. What is left for the operator

The script is ready and the mechanism is proven. **The remaining action is the
full quarantining of the 208 unused objects / 16,619,387,646 bytes**:

```bash
cd /home/will/social-auto-upload
# 1. review the plan (no writes)
python3 scripts/purge_legacy_drive.py --report-basenames --list-actions 208
# 2. after approval, quarantine all 208
python3 scripts/purge_legacy_drive.py --apply
```

Recommended pre-flight for the full run: spot-check the 17 basename-review
objects listed in §1 and confirm they may be quarantined. If any should be kept,
add a `--roots` filter (for example, run `--roots uploads` separately) or ask for
a small allowlist rather than editing the classifier. The full run can be
resumed safely: quarantined objects leave the legacy roots, so a re-run simply
finds fewer UNUSED objects.

Nothing NEEDED is moved or deleted by any of the above. Reversing a quarantine
run is `rclone moveto` from `sau/trash/2026-10-10/<root>/<key>` back to
`sau/<root>/<key>`.

---

## 6. Commands run (for audit)

| command | result |
| --- | --- |
| `rclone lsjson -R --files-only GDrive-willywang8216:sau/{videoFile,uploads,generated}` | 650 / 4 / 485 (483 unique) objects, captured to `/tmp/legacy_*.json` |
| `python3 scripts/purge_legacy_drive.py --report-basenames --check-tiered --list-actions 5` | dry-run, 208 unused / 16,619,387,646 B; 915 NEEDED not tiered |
| `python3 scripts/purge_legacy_drive.py --apply --limit 5 --list-actions 10` | 5 quarantined |
| `rclone lsjson -R GDrive-willywang8216:sau/trash/2026-10-10` | 5 destination files, sizes match |
| `rclone lsf GDrive-willywang8216:sau/videoFile/<key>` ×5 | rc=3, 0 lines (sources gone) |
| `rclone copyto …/378c5466-…mp4 /tmp/intact_proof.mp4` + `ffprobe` | 91,042,314 B, valid mp4, 201.5 s |
| `download_from_backend()` ×5 | restored, sizes match records |
