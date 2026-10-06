# Publish Center skip audit — why selected platforms/accounts sometimes get no post

**Generated:** 2026-10-06 05:20 UTC  
**Database:** `db/database.db` (read-only)  
**Media roots:** `videoFile/`, `generated/`, `uploads/` (container `/app/` == repo root)  
**Scope:** submit-time skips in `sau_backend.publish_center_submit` -> `myUtils/publish_orchestrator.submit_publish`, plus post-queue target failures. No code, DB, container or schedule changed.

## 0. Headline

- **228 campaigns produced zero posts**, so *every* selected platform was dropped: 173 are one fixed code bug (`name 'logger' is not defined`), 50 are missing source media, 5 are interrupted submits.
- **222 of 228** zero-post campaigns still have a registered remote copy on the GDrive cache backend (`storage_backends.id=416`). **220** are recoverable by resubmitting the same media path (the pipeline restores missing relative-path locals via `_download_file_from_storage`); **2407/2409** also have the copy but their stored `file_path` is absolute `/app/...`, which the restore branch skips, so they need one manual `rclone copy` first. Only **6 are permanent**.
- **98 failed targets** (queued but never published). Cause mix: 74 missing generated artifacts, 7 TikTok dev-mode attempts, 4 Reddit config refusals, plus smaller groups. 12 of the 98 also reference an account that no longer exists. See `logs/failed-recovery-plan.md` for the stricter reschedule-only treatment.
- **A campaign mixing a large-cap platform with a small-cap one can be split on the wrong platform's parts**: with {youtube, instagram}, a 12600 s source under YouTube's 12 h cap is still cut into 17 Instagram-sized parts for YouTube (see §5c #3).
- **The split feature has never run in production** (`0` part artifacts in the DB). The code is in the running container, but no campaign has been submitted since it landed. The selector also ignores split parts for **TikTok, Telegram and Discord**, so those caps cannot be satisfied by splitting even once it runs.
- Per-account draft-generation skips and account-resolution skips leave **no DB footprint** (the `skipped` list is only returned to the caller). No completed campaign shows one; the 11 partially-posted campaigns are all `preparing` (interrupted), not draft failures.

## 1. Where a selected platform can drop out

| # | Failure mode | Code reference | Effect | DB count |
|---|---|---|---|---|
| M1 | Artifact-prep exception aborts the whole campaign | `myUtils/publish_orchestrator.py:355-371` (`except` -> `last_error="artifact prep failed: ..."`, `continue`) | **0 posts for every selected platform** | **228** zero-post campaigns (223 `needs_review` + 5 `preparing`) |
| M1a | -> `name 'logger' is not defined` in storage helpers | `sau_backend.py` pre-fix; fixed by `81805e8` 2026-09-21 | whole campaign skipped when remote host/download ran | 173 |
| M1b | -> missing source media (`FileNotFoundError`) | same handler | whole campaign skipped | 50 |
| M1c | -> interrupted submit (row created, status left `preparing`, empty error) | `submit_publish` never reached its final `update_campaign` | partly/fully unpublished | 5 zero-post + 11 posts-only |
| M2 | Per-account draft-generation failure | `publish_orchestrator.py:419-455` | only that account skipped | 0 recorded (not persisted) |
| M3 | Account resolution: no enabled accounts / NSFW gate | `publish_orchestrator.py:180-190` (`no_enabled_accounts`), `:236-244`; `myUtils/content_rating.py:71` (`nsfw_no_adult_safe_account`) | profile skipped, no campaign row | 0 recorded (not persisted) |
| M4 | Platform compatibility skip (YouTube needs video; TikTok photo needs Direct Post) | `publish_orchestrator.py:309-326` | account excluded before posts | 0 recorded (not persisted) |
| M5 | Unsupported publisher at execution | `myUtils/worker.py:2170-2180`; `:1703` | post queued, target fails forever | 0 now; latent for `linkedin`,`pinterest`,`douyin`,`kuaishou`,`xiaohongshu`,`tencent` |
| M6 | Single-media multi-file fan-out branch | `publish_orchestrator.py:468` else `:508-529` | not a skip, but bypasses long-video splitting | see §5 |
| M7 | Post queued, target fails at execution | `myUtils/worker.py:1362-1552`; `MediaRestoreError` `:1281-1290` | platform never gets the post | **98 failed targets** |
| M8 | Backlog: target `pending`, not yet claimed | worker drain / schedule | not published yet | 3763 pending |

### M1 breakdown (zero-post campaigns)

| Cause | Campaigns | Source on disk | Source on GDrive 416 (recoverable) | Permanent |
|---|---:|---:|---:|---:|
| logger NameError | 173 | 0 | 173 | 0 |
| missing source media | 50 | 0 | 45 | 5 |
| interrupted submit | 5 | 0 | 4 | 1 |
| **Total** | **228** | 0 | 222 | 6 |

> Of the 222 with a registered GDrive copy, **220 use a relative `videoFile/...` `file_path`** and restore automatically on a plain resubmit. **2407 and 2409 store an absolute `/app/videoFile/...` path**, and `_prepare_campaign_media_artifacts` (`sau_backend.py:3633-3643`) only calls `_download_file_from_storage` inside `if not raw_path.is_absolute()`, so those two need the object copied back manually before resubmitting.

## 2. Recovery list

> `recoverable = yes` for a zero-post campaign means the source media is registered on the GDrive cache backend (`storage_backends.id=416`); resubmitting the same media path restores it via `_download_file_from_storage` and regenerates artifacts. **Caveat:** the auto-restore only runs for relative `file_path`s; campaigns 2407/2409 have an absolute `/app/...` path and need a manual `rclone copy` first. Verify one restore live before bulk resubmission. The rclone cache config is root-only and the container holds it.

### 2a. Zero-post campaigns (no platform got a post)

| Campaign | Status | Cause | Recoverable | Action |
|---:|---|---|:--:|---|
| 60 | needs_review | source media missing: /app/uploads/ec718bf0-42ee-4304-a868-50186dfe54fb_111_1hour_plus.mp4 | no | permanent: no source bytes; re-upload master |
| 61 | needs_review | source media missing: /app/uploads/ec718bf0-42ee-4304-a868-50186dfe54fb_111_1hour_plus.mp4 | no | permanent: no source bytes; re-upload master |
| 62 | needs_review | source media missing: /app/uploads/ec718bf0-42ee-4304-a868-50186dfe54fb_111_1hour_plus.mp4 | no | permanent: no source bytes; re-upload master |
| 63 | needs_review | source media missing: /app/test.mp4 | no | permanent: no source bytes; re-upload master |
| 1896 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260813074703243_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1897 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260821094430455_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1898 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260821101256820_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1899 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260822082414627_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1900 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260822153123648_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1901 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260827123526568_part1_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1902 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260827123526568_part2_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1903 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260827123526568_part3_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1904 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260830112542557_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1905 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260903141942402_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1906 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260827154825_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1907 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260907103730_part1_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1908 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260907103730_part2_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1909 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260907103730_part3_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1910 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1911 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1912 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1913 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1914 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1915 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1916 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1917 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1918 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1919 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1920 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1921 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1922 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1923 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1924 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1925 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1926 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1927 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1928 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1929 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1930 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1931 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1932 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1933 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1934 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1935 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1936 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1937 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1938 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1939 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1940 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1941 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1942 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1943 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1944 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1945 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1946 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1947 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1948 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1949 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1950 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1951 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1952 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1953 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1954 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1955 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1956 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1958 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260813074703243_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1959 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260821094430455_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1960 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260821101256820_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1961 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260822082414627_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1962 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260822153123648_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1963 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260827123526568_part1_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1964 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260827123526568_part2_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1965 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260827123526568_part3_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1966 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260830112542557_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1967 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260903141942402_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1968 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260827154825_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1969 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260907103730_part1_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1970 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260907103730_part2_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1971 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260907103730_part3_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 1972 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1973 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1974 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1975 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1976 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1977 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1978 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1979 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1980 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1981 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1982 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1983 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1984 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1985 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1986 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1987 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1988 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1989 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1990 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1991 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1992 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1993 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1994 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1995 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1996 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1997 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1998 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 1999 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2000 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2001 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2002 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2003 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2004 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2005 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2006 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2007 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2008 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2009 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2010 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2011 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2012 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2013 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2014 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2015 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2016 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2017 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2018 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2019 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2020 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2021 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2022 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2023 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2024 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2025 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2026 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2027 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2028 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2029 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2030 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2031 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2032 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2033 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2034 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2035 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2036 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2037 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2038 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2039 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2040 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2041 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2042 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2043 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2044 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2045 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2046 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2047 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2048 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2049 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2050 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2051 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2052 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2053 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2054 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2055 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2056 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2057 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2058 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2059 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2060 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2061 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2062 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2063 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2064 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2065 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2066 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2067 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2068 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2069 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2070 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2071 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2072 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2073 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2074 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2075 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2076 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2077 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2078 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2079 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2080 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2081 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2082 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2083 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2084 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2085 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2086 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2087 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2088 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2089 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2090 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2091 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2092 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2093 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2094 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2095 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2096 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2097 | needs_review | artifact prep NameError (code bug, fixed 81805e8) | yes | restore source from GDrive backend 416, then resubmit |
| 2136 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260813074703243_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2147 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260813074703243_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2148 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260821094430455_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2149 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260821101256820_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2150 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260822082414627_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2151 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260822153123648_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2152 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260827123526568_part1_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2153 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260827123526568_part2_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2154 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260827123526568_part3_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2155 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260830112542557_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2156 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW 20260903141942402_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2157 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260827154825_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2158 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260907103730_part1_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2159 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260907103730_part2_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2160 | needs_review | source media missing: /app/videoFile/_batch1/sfw/SFW lv_0_20260907103730_part3_pub.mp4 | yes | restore source from GDrive backend 416, then resubmit |
| 2169 | preparing | interrupted before posts (empty error) | yes | restore source from GDrive backend 416, then resubmit |
| 2172 | preparing | interrupted before posts (empty error) | yes | restore source from GDrive backend 416, then resubmit |
| 2264 | preparing | interrupted before posts (empty error) | yes | restore source from GDrive backend 416, then resubmit |
| 2365 | preparing | interrupted before posts (empty error) | yes | restore source from GDrive backend 416, then resubmit |
| 2395 | needs_review | source media missing: /app/data/publish/sfw-teaching-least-squares-proof-calculus.png | no | permanent: no source bytes; re-upload master |
| 2407 | needs_review | source media missing: /app/videoFile/_inbox_cache/b9d637575ccb46d6b87cdb8095ffd218_SFW.mp4 | yes | registered on GDrive 416 but path is absolute: `rclone copy` the object into `videoFile/_inbox_cache/`, then resubmit |
| 2408 | preparing | interrupted before posts (empty error) | no | permanent: no source bytes; re-upload master |
| 2409 | needs_review | source media missing: /app/videoFile/_inbox_cache/b9d637575ccb46d6b87cdb8095ffd218_SFW.mp4 | yes | registered on GDrive 416 but path is absolute: `rclone copy` the object into `videoFile/_inbox_cache/`, then resubmit |

### 2b. `preparing` campaigns (interrupted submit)

| Campaign | Profile | Platform(s) queued | Cause | Recoverable | Action |
|---:|---:|---|---|:--:|---|
| 2129 | 1 | bluesky/telegram | interrupted mid-submit after queueing 4 post(s) | yes | finalize campaign status; targets exist (pending=4); re-queue missing accounts |
| 2132 | 1 | bluesky/telegram | interrupted mid-submit after queueing 3 post(s) | yes | finalize campaign status; targets exist (pending=3); re-queue missing accounts |
| 2133 | 1 | bluesky/telegram | interrupted mid-submit after queueing 4 post(s) | yes | finalize campaign status; targets exist (pending=4); re-queue missing accounts |
| 2134 | 1 | bluesky/telegram | interrupted mid-submit after queueing 4 post(s) | yes | finalize campaign status; targets exist (pending=4); re-queue missing accounts |
| 2169 | 3 | - | interrupted before any post was queued | yes | resubmit the media |
| 2171 | 3 | bluesky | interrupted mid-submit after queueing 2 post(s) | yes | finalize campaign status; targets exist (succeeded=2); re-queue missing accounts |
| 2172 | 3 | - | interrupted before any post was queued | yes | resubmit the media |
| 2215 | 3 | bluesky/facebook/instagram/telegram | interrupted mid-submit after queueing 6 post(s) | yes | finalize campaign status; targets exist (pending=1, succeeded=5); re-queue missing accounts |
| 2216 | 3 | bluesky | interrupted mid-submit after queueing 2 post(s) | yes | finalize campaign status; targets exist (succeeded=2); re-queue missing accounts |
| 2217 | 3 | bluesky/facebook/instagram | interrupted mid-submit after queueing 4 post(s) | yes | finalize campaign status; targets exist (succeeded=4); re-queue missing accounts |
| 2218 | 3 | bluesky/facebook | interrupted mid-submit after queueing 3 post(s) | yes | finalize campaign status; targets exist (succeeded=3); re-queue missing accounts |
| 2264 | 1 | - | interrupted before any post was queued | yes | resubmit the media |
| 2365 | 3 | - | interrupted before any post was queued | yes | resubmit the media |
| 2408 | 3 | - | interrupted before any post was queued | no | permanent: source bytes gone |
| 2411 | 1 | bluesky/nw_sw_blog/reddit/telegram | interrupted mid-submit after queueing 5 post(s) | yes | finalize campaign status; targets exist (succeeded=5); re-queue missing accounts |
| 2439 | 3 | bluesky | interrupted mid-submit after queueing 2 post(s) | yes | finalize campaign status; targets exist (pending=2); re-queue missing accounts |

### 2c. Failed targets (platform queued, never published)

| Target | Platform | Account | Cause | Recoverable | Action |
|---:|---|---|---|:--:|---|
| 10 | tiktok | account:85 (deleted) | TikTok empty media payload | no | account no longer exists |
| 11 | tiktok | account:87 (deleted) | TikTok media not hosted publicly | no | account no longer exists |
| 12 | tiktok | account:87 (deleted) | TikTok API 403 (access/scope) | no | account no longer exists |
| 13 | tiktok | account:87 (deleted) | TikTok API 403 (access/scope) | no | account no longer exists |
| 14 | tiktok | account:87 (deleted) | TikTok app still in development mode | no | account no longer exists |
| 15 | tiktok | account:87 (deleted) | TikTok app still in development mode | no | account no longer exists |
| 16 | tiktok | account:88 (deleted) | TikTok app still in development mode | no | account no longer exists |
| 17 | tiktok | account:88 (deleted) | TikTok app still in development mode | no | account no longer exists |
| 18 | tiktok | account:88 (deleted) | TikTok app still in development mode | no | account no longer exists |
| 19 | tiktok | account:88 (deleted) | TikTok app still in development mode | no | account no longer exists |
| 20 | tiktok | account:89 (deleted) | TikTok app still in development mode | no | account no longer exists |
| 21 | tiktok | account:90 (deleted) | TikTok upload HTTP 416 | no | account no longer exists |
| 38 | facebook | teaching-fb [facebook] | generated artifact missing/unrestorable | no | permanent: artifact gone and source not registered |
| 71 | reddit | Nakedwill Reddit [reddit] | Reddit subreddit/config rejection | no | operator: fix subreddit list/whitelist, then retry |
| 72 | reddit | Nakedwill Reddit [reddit] | API auth 401 | no | permanent: artifact gone and source not registered |
| 73 | reddit | Nakedwill Reddit [reddit] | TypeError: get_browser_options() takes 0 positional arguments but 1 was given | no | permanent: artifact gone and source not registered |
| 75 | reddit | Nakedwill Reddit [reddit] | FileNotFoundError: [Errno 2] No such file or directory: '/app/uploads/9b97ff89-d | no | permanent: artifact gone and source not registered |
| 76 | reddit | Nakedwill Reddit [reddit] | FileNotFoundError: [Errno 2] No such file or directory: '/app/uploads/3b7c2e94-7 | no | permanent: artifact gone and source not registered |
| 77 | reddit | Nakedwill Reddit [reddit] | RuntimeError: Reddit API error: Please log in to do that. | no | permanent: artifact gone and source not registered |
| 100 | youtube | Willy Dev tutor [youtube] | YouTube missing media | no | permanent: artifact gone and source not registered |
| 2524 | bluesky | SW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2527 | telegram | SW TG 中文 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2528 | telegram | SW TG 本人 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2531 | twitter | sexualwill [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2891 | telegram | NW TG 本人 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2892 | twitter | NW X (model_will) [twitter] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2896 | telegram | NW TG 本人 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2897 | twitter | NW X (model_will) [twitter] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2899 | bluesky | NW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2901 | telegram | NW TG 本人 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2902 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2904 | bluesky | NW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2906 | telegram | NW TG 本人 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2907 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2909 | bluesky | NW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2911 | telegram | NW TG 本人 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2912 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 2917 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3091 | telegram | NW TG 本人 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3092 | twitter | NW X (model_will) [twitter] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3096 | telegram | NW TG 本人 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3097 | twitter | NW X (model_will) [twitter] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3099 | bluesky | NW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3101 | telegram | NW TG 本人 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3102 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3104 | bluesky | NW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3106 | telegram | NW TG 本人 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3107 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3109 | bluesky | NW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3111 | telegram | NW TG 本人 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3112 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3117 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3357 | telegram | NW TG 本人 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3359 | twitter | NW X (model_will) [twitter] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3365 | telegram | NW TG 本人 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3367 | twitter | NW X (model_will) [twitter] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3399 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3449 | bluesky | SW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3450 | bluesky | SW Bluesky ZH [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3451 | telegram | SW TG 中文 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3452 | telegram | SW TG 本人 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3453 | twitter | sexualwill [twitter] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3457 | telegram | SW TG 中文 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3458 | telegram | SW TG 本人 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3459 | twitter | sexualwill [twitter] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3461 | bluesky | SW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3463 | telegram | SW TG 中文 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3464 | telegram | SW TG 本人 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3465 | twitter | sexualwill [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3471 | twitter | sexualwill [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3473 | bluesky | SW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3475 | telegram | SW TG 中文 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3476 | telegram | SW TG 本人 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3477 | twitter | sexualwill [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3483 | twitter | sexualwill [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3765 | telegram | SW TG 中文 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3766 | telegram | SW TG 本人 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3768 | twitter | sexualwill [twitter] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3774 | telegram | SW TG 中文 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3775 | telegram | SW TG 本人 [telegram] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3777 | twitter | sexualwill [twitter] | generated artifact missing/empty | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3834 | reddit | Sexualwill Reddit OAuth [reddit] | Reddit subreddit/config rejection | no | operator: fix subreddit list/whitelist, then retry |
| 3850 | youtube | Itswill_YT [youtube] | YouTube missing media | no | permanent: artifact gone and source not registered |
| 3856 | reddit | Sexualwill Reddit OAuth [reddit] | Reddit subreddit/config rejection | no | operator: fix subreddit list/whitelist, then retry |
| 3872 | nw_sw_blog | NW Blog [nw_sw_blog] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3874 | reddit | Nakedwill Reddit [reddit] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3876 | telegram | NW TG 本人 [telegram] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3877 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3888 | reddit | Sexualwill Reddit OAuth [reddit] | Reddit subreddit/config rejection | no | operator: fix subreddit list/whitelist, then retry |
| 3896 | reddit | Nakedwill Reddit [reddit] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3903 | reddit | Sexualwill Reddit OAuth [reddit] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3906 | twitter | sexualwill [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3907 | twitter | 光光 [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3920 | bluesky | NW Bluesky EN [bluesky] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3922 | nw_sw_blog | NW Blog [nw_sw_blog] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3923 | reddit | Nakedwill Reddit [reddit] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 3925 | twitter | NW X (model_will) [twitter] | generated artifact missing/unrestorable | yes | restore source from GDrive 416, regenerate artifacts, re-queue |
| 4515 | reddit | Nakedwill Reddit [reddit] | Reddit link-whitelist validation (SUBMIT_VALIDATION_LINK_WHITELIST) | no | operator: change the CTA/link; retry sends the same rejected link |

**Failed-target totals:** generated artifact missing/unrestorable=50, generated artifact missing/empty=24, TikTok app still in development mode=7, Reddit subreddit/config rejection=4, TikTok API 403 (access/scope)=2, YouTube missing media=2, TikTok empty media payload=1, TikTok media not hosted publicly=1, TikTok upload HTTP 416=1, API auth 401=1, TypeError: get_browser_options() takes 0 positional arguments but 1 was given=1, FileNotFoundError: [Errno 2] No such file or directory: '/app/uploads/9b97ff89-d=1, FileNotFoundError: [Errno 2] No such file or directory: '/app/uploads/3b7c2e94-7=1, RuntimeError: Reddit API error: Please log in to do that.=1, PreparedPublishError: Reddit submit failed for r/NudistMen: [['SUBMIT_VALIDATION=1. Recoverable: 73 yes / 25 no.

## 3. Pending backlog (not skips, but platforms not published yet)

| Platform | Pending targets | Earliest | Latest |
|---|---:|---|---|
| telegram | 1236 | 2026-10-06T13:30:00 | 2027-06-04T13:30:00 |
| twitter | 1195 | 2026-10-06T07:10:00 | 2027-06-01T14:20:00 |
| bluesky | 1069 | 2026-10-06T13:40:00 | 2027-06-04T13:40:00 |
| reddit | 95 | 2026-10-06T13:25:00 | 2026-11-22T14:25:00 |
| nw_sw_blog | 77 | 2026-10-12T13:10:00 | 2026-12-12T13:10:00 |
| facebook | 40 | 2026-10-06T12:20:00 | 2026-11-02T12:05:00 |
| tiktok | 20 | 2026-10-07T13:00:00 | 2026-11-02T13:45:00 |
| threads | 17 | 2026-10-06T23:10:00 | 2026-11-02T12:10:00 |
| instagram | 11 | 2026-10-07T12:10:00 | 2026-11-02T12:10:00 |
| youtube | 2 | 2026-10-07T14:00:00 | 2026-11-02T14:00:00 |

## 4. Recommended smallest fix: per-platform artifact isolation (NOT implemented)

**Problem.** `_prepare_campaign_media_artifacts` (`sau_backend.py:3559`) prepares all media for all selected platforms in one call. Any uncaught exception propagates to the single `try/except` at `myUtils/publish_orchestrator.py:355-371`, which marks the whole campaign `needs_review` and creates no posts. One bad artifact for one platform drops every other platform in the submit.

**Smallest change.** Call `prepare_artifacts` once **per platform** and isolate failures:

1. In `submit_publish`, after `grouped_accounts` is built (`publish_orchestrator.py:375-377`), move the `prepare_artifacts(...)` call (`:355`) inside the `for platform, platform_accounts in grouped_accounts.items():` loop (`:392`), wrapped in its own `try/except`. Pass `selected_platforms={platform}`.

2. On failure, append `{profileId, platform, reason: "artifact prep failed: ..."}` to `skipped` and `continue` that platform only. Keep the campaign in `preparing`/`publishing` as long as at least one platform queued; set `needs_review` only if every platform failed.

3. The drafts are regenerated per account anyway, so a finer variant is to guard the per-media body inside `_prepare_campaign_media_artifacts` (`sau_backend.py:3631-3985`) and return a `failed_sources` map; that keeps a single prep call and lets the healthy platforms proceed.

Both options preserve the existing `skipped` list semantics. The per-platform call is the smaller and safer diff because it needs no new return contract.

## 5. Splitting verification against the real caps

### 5a. Required parts for a 12600 s / 5000 MB source

| Platform | Max seconds | Max MB | `_split_count` | sec/part | MB/part |
|---|---:|---:|---:|---:|---:|
| tiktok | 3600.0 | 4096 | 4 | 3150.0 | 1250.0 |
| youtube | 43200.0 | 262144 | 1 | 12600.0 | 5000.0 |
| instagram | 900.0 | 300 | 17 | 741.2 | 294.1 |
| facebook | 14460.0 | 4096 | 2 | 6300.0 | 2500.0 |
| threads | 300.0 | 1024 | 42 | 300.0 | 119.0 |
| twitter | 140.0 | 512 | 90 | 140.0 | 55.6 |
| bluesky | 600.0 | 300 | 21 | 600.0 | 238.1 |
| reddit | 900.0 | 1000 | 14 | 900.0 | 357.1 |
| telegram | None | 2000 | 3 | 4200.0 | 1666.7 |
| discord | None | 25 | 200 | 63.0 | 25.0 |

### 5b. Does the selector actually use the split parts?

| Platform | Video artifacts selected | `part_index` values | Split used? |
|---|---|---|---|
| tiktok | 1 | [None] | **NO** |
| youtube | 2 | [1, 2] | YES |
| instagram | 17 | [1, 2, 3, 4, 5, 6]... | YES |
| facebook | 2 | [1, 2] | YES |
| threads | 42 | [1, 2, 3, 4, 5, 6]... | YES |
| twitter | 90 | [1, 2, 3, 4, 5, 6]... | YES |
| bluesky | 21 | [1, 2, 3, 4, 5, 6]... | YES |
| reddit | 14 | [1, 2, 3, 4, 5, 6]... | YES |
| telegram | 1 | [None] | **NO** |
| discord | 1 | [None] | **NO** |

> In this 5b fixture every platform's plan is present at once. YouTube reports `[1,2]` even though its own `_split_count` is 1: those are the **facebook** plan's parts (6300 s / 2500 MB), which happen to fit YouTube's larger caps. A focused {youtube, instagram} case (below) additionally illustrates the contamination.

### 5c. Gaps found

1. **TikTok (3600 s / 4096 MB) — split ignored.** `_artifact_payloads_for_platform` (`sau_backend.py:2157-2191`) has a TikTok-only branch that always prefers the raw un-watermarked artifact (`raw_remote_upload`/`raw_local`), so the part artifacts are dropped and a >3600 s or >4096 MB video goes out whole. A TikTok-only campaign also bypasses the pre-publish `_shrink_for_publish` for the raw path.
2. **Telegram (>2000 MB) and Discord (>25 MB) — size-only split ignored.** `_select_videos_for_platform` (`sau_backend.py:2224-2226`) returns `[items[0]]` as soon as `video_max_seconds(platform) is None`. Telegram and Discord have no duration cap, so their size splits are never selected.
3. **Cross-platform part contamination.** Part metadata records only the part's own estimated `max_duration_seconds`/`max_media_mb`, never which platform cap generated the plan. A part created for a small-cap platform therefore "fits" a larger-cap platform and is selected there. Verified: a campaign targeting **{youtube, instagram}** with a 12600 s / 5000 MB source (under YouTube's 12 h cap) returns **17 Instagram-sized parts for YouTube** instead of the single full video; `_select_videos_for_platform` fails to prefer the full artifact when a lower-cap sibling's parts fit. In the 5b fixture, YouTube's `[1,2]` are likewise the facebook plan's parts.
4. **Mixed single-media platforms with >1 source file never split.** The `if supports_multi_media or len(publishable_files) <= 1` branch (`publish_orchestrator.py:468`) sends single-media platforms with multiple files down the per-file fan-out (`:508`), which never calls `artifact_part_groups_for_platform`. Two long videos on TikTok/YouTube/Telegram are posted whole.
5. **No production evidence.** `campaign_artifacts` contains `0` rows with `part_index`, so despite the cap math the split path has not produced a part in this database.

### 5d. Cap coverage summary

| Platform | Cap | Split computed? | Selected for posting? | Verdict |
|---|---|---|---|---|
| tiktok | 3600 s / 4096 MB | yes | no (raw branch) | cannot satisfy |
| youtube | 43200 s / 262144 MB | yes (not needed below 12 h) | yes | OK alone; **wrong in mixed campaigns** (picks a sibling's finer parts — §5c #3) |
| instagram | 900 s / 300 MB | yes | yes | OK |
| facebook | 14460 s / 4096 MB | yes | yes | OK |
| threads | 300 s / 1024 MB | yes | yes | OK |
| twitter | 140 s / 512 MB | yes | yes | OK |
| bluesky | 600 s / 300 MB | yes | yes | OK |
| reddit | 900 s / 1000 MB | yes | yes | OK |
| telegram | none / 2000 MB | yes | no (None-seconds short-circuit) | cannot satisfy |
| discord | none / 25 MB | yes | no (None-seconds short-circuit) | cannot satisfy |

## 6. Method

- Counts are read-only SQL over `db/database.db`: `campaigns`, `campaign_posts`, `campaign_artifacts`, `publish_jobs`, `publish_job_targets`, `media_group_items`, `file_records`, `storage_backends`, `accounts`.
- Local media existence maps container path `/app/X` to repo `./X` (compose mounts `./videoFile`, `./uploads`, `./generated`).
- Split verification imports the live pure functions (`myUtils.platform_limits`, `myUtils.media_prep._split_count`, `sau_backend._artifact_payloads_for_platform`) and runs synthetic 12600 s / 5000 MB artifacts through them.
- The `skipped` list returned by `submit_publish` is not persisted, so M2/M3/M4 counts are `0 recorded`; they are structural and confirmed by code, not by data.
