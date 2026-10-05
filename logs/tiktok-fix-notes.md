# TikTok publish failure — job 4518 / target 4518 / account 109 (Nakedwill_TK)

Author: tiktok-fix agent
Date: 2026-10-05
Files changed:
- `myUtils/prepared_publishers.py`
- `tests/test_prepared_publishers.py`

## Symptom

The target failed on its first attempt, classified non-retryable:

```
PreparedPublishError: TikTok API error (invalid_params): The total chunk count is invalid
```

It failed at the `/v2/post/publish/video/init/` call (the whole attempt
took ~0.6 s), so the request body's `source_info` was rejected before any
bytes were uploaded.

## Root cause

`publish_tiktok_sync` computed the FILE_UPLOAD chunk count with **ceiling**
division and `_tiktok_file_upload` sent `ceil(video_size / chunk_size)`
chunks (each `chunk_size` bytes with the trailing remainder as its own final
chunk).

TikTok's documented rule is **floor** division, with the trailing remainder
merged into the final chunk:

> * The value of `total_chunk_count` should be equal to `video_size` divided by
>   `chunk_size`, rounded down to the nearest integer.
> * Each chunk must be at least 5 MB but no greater than 64 MB, except for the
>   final chunk, which can be greater than `chunk_size` (up to 128 MB) to
>   accommodate any trailing bytes.
>
> — TikTok, *Media Transfer Guide*
> (https://developers.tiktok.com/docs/en/content-posting-api-media-transfer-guide)

### Which bytes were actually uploaded

The job payload's artifact `local_path` is `/app/videoFile/SFW Taipei
Stonewall.mp4` (the 602 MB / 813 s original), not the re-encoded short cut;
the same finding was recorded in `logs/threads-fix-notes.md`. The R2
`public_url` prefix is not domain-verified, so the publisher fell back from
`PULL_FROM_URL` to `FILE_UPLOAD` on the 602 MB source.

For that file (`file_size = 602,285,541`, `chunk_size = 64 MiB = 67,108,864`):

| formula | result |
|---|---|
| old `ceil()` sent | **9** |
| TikTok expects `floor()` | **8** |

The old code therefore declared 9 chunks; TikTok rejected the count. The
trailing bytes (`602,285,541 - 7 * 67,108,864 = 132,523,493`, ~126 MiB) are
meant to ride along in the 8th (final) chunk, which TikTok accepts up to
128 MB. Files of ~47 MB are unaffected (single chunk, `floor = 1`), which is
why the failure only showed up when the publisher used the full-length
source.

## Fix

`myUtils/prepared_publishers.py`

1. New `_tiktok_chunk_plan(file_size) -> (chunk_size, total_chunk_count)`:
   - `file_size <= 64 MB`: `(file_size, 1)` (whole-file upload; also covers
     files under 5 MB).
   - `file_size > 64 MB`: `total = floor(video_size / chunk_size)` with
     `chunk_size = min(64 MB, file_size // 2)`. Capping at half the file size
     keeps `total >= 2`, because TikTok requires files larger than 64 MB to be
     uploaded in multiple chunks.
2. `_tiktok_file_upload` now takes `total_chunks` and sends exactly that many
   requests: the first `total_chunks - 1` carry `chunk_size` bytes and the
   final request reads every remaining byte (the documented merge of the
   trailing remainder). The declared count and the actual chunk boundaries can
   no longer diverge.

`TIKTOK_FILE_UPLOAD_CHUNK_SIZE` (5 MB) is retained as the documented minimum
and is asserted in the tests.

## Regression tests

Added to `tests/test_prepared_publishers.py` (`PreparedPublisherTests`):

- `test_tiktok_chunk_plan_floors_the_chunk_count` — the exact job 4518 case
  (602,285,541 bytes): asserts `chunk_size == 64 MiB`, `total == floor == 8`
  (not 9), declared bytes never overshoot the file, and the merged final chunk
  is within the 128 MB allowance.
- `test_tiktok_chunk_plan_forces_multiple_chunks_over_64mb` — boundary just
  over 64 MB, 100 MB, 128 MB−1, and 4 GB: always `>= 2` chunks, `chunk_size`
  within [5 MB, 64 MB], `total == floor`.
- `test_tiktok_chunk_plan_single_chunk_for_small_and_medium_files` — 1 B,
  4 MB, 5 MB, 47 MB stay single-chunk with `chunk_size == file_size`.
- `test_tiktok_file_upload_merges_remainder_into_final_chunk` — a 10-byte file
  with `chunk_size=4, total_chunks=2` issues two PUTs, the second carrying the
  6 trailing bytes (`Content-Range: bytes 4-9/10`), proving the uploader
  matches the declared count.

## Verification

```
.venv/bin/python -m pytest tests/ -q -k 'tiktok or publish or prepared'
273 passed, 934 deselected, 20 subtests passed

.venv/bin/python -m pytest tests/test_prepared_publishers.py -q
127 passed, 20 subtests passed
```

## Operator note

This fixes the chunk-count rejection for the 602 MB source. It does not add a
re-encode; the underlying prep bug (the artifact pointing at the 813 s source
instead of the short cut) is tracked separately in
`logs/threads-fix-notes.md`. With this change a future FILE_UPLOAD of the full
602 MB file will initialize correctly as 8 chunks.
