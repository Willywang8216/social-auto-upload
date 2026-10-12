# gdn-watcher — Drive filename parser + ready-entry enrichment

Subagent: **A / `gdn-watcher`**
Scope (only): `/home/will/sau-inbox/watch.py`, `/home/will/sau-inbox/tests/`
Spec: `reports/gdrive-filename-spec.md` (approved-for-implementation)
Status: **done** — parser, ready fields, language heuristic, tests all green.
No STT run, no publish API call, no `state.json` mutation, no
`/home/will/social-auto-upload` source touched (only this report written).

## Files changed

| file | change |
|---|---|
| `/home/will/sau-inbox/watch.py` | new `parse_filename()` (v2+v3); `parse_name()` kept as back-compat wrapper; `detect_language()` + `_has_cjk()`; `_read_sibling_note()` now also reads `.md`; `_sidecar_candidates()`/`_download_sidecars()`/`_chown_scan_root()`; `process_paths()` sets `title`/`publishDate`/`language`/`transcript`; nsfw branch fetches text notes (still no source download/frame); pending/orphan message text updated. |
| `/home/will/sau-inbox/tests/test_filename_parse.py` | **new** — 23 parser/date/language/ready-entry tests. |
| `/home/will/social-auto-upload/reports/gdn-watcher.md` | this report. |

`tests/test_watch_drive.py` (pre-existing, 2 tests) still passes untouched.

### Signature / caller audit

`parse_name()` had exactly two callers, both inside `watch.py` (former lines
306 and 549). Both now call `parse_filename()`. `parse_name(stem) ->
(topic, flag) | None` is retained unchanged as the documented back-compat
wrapper, so any out-of-tree caller keeps working. `parse_filename()` is the
authoritative parser and additionally returns `publish_date`.

```python
class ParsedName(NamedTuple):
    topic: str
    flag: str                    # "sfw" | "nsfw"
    publish_date: str | None     # "YYYY-MM-DD" | None

def parse_filename(stem) -> ParsedName | None   # accepts v2 and v3
def parse_name(stem) -> tuple[str, str] | None   # back-compat wrapper
def detect_language(note, title) -> "zh"|"en"|None
```

## Parser rules

1. **v2 legacy** `<topic?>__<sfw|nsfw>` — checked first. `qixi__nsfw`, `__sfw`,
   and bare `nsfw`/`sfw` all still parse. If the token after `__` is not a flag,
   parsing falls through to v3 instead of hard-failing.
2. **v3 new** `<Title with spaces>_<SFW|NSFW>[_<YYYYMMDD>]`. Title is everything
   before the final `_`-separated flag; title may contain spaces **and**
   underscores. Flag is case-insensitive.
3. **Date** is optional; when the last token is 8 digits it is treated as a date
   and validated (`strptime %Y%m%d`), normalised to `YYYY-MM-DD`. A date-shaped
   but impossible value (`20260230`, `20261301`) rejects the whole filename
   (lands in `pending`), rather than being silently swallowed into the title.

### Exact parse examples

```
qixi-30-virgin__nsfw                              -> topic="qixi-30-virgin" flag=nsfw  date=None
__sfw                                             -> topic=""              flag=sfw   date=None
nsfw                                              -> topic=""              flag=nsfw  date=None
My great title space separated_NSFW_20261015      -> topic="My great title space separated" flag=nsfw date="2026-10-15"
Pride invitation_SFW_20261024                     -> topic="Pride invitation" flag=sfw  date="2026-10-24"
Pride invitation_SfW_20261024                     -> topic="Pride invitation" flag=sfw  date="2026-10-24"   # case-insensitive
News update_NSFW                                  -> topic="News update"   flag=nsfw   date=None
my_video_title_sfw_20260101                       -> topic="my_video_title" flag=sfw   date="2026-01-01"
My__title_NSFW                                    -> topic="My__title"     flag=nsfw   date=None
Bad_NSFW_20260230                                 -> None  (reject: impossible date)
title_20261015 / "just a title" / "" / bad__flag  -> None  (reject: no flag)
```

`parse_name` wrapper: `topic__nsfw -> ("topic","nsfw")`;
`My title_NSFW_20261015 -> ("My title","nsfw")`;
`My title_SFW -> ("My title","sfw")`; `nope -> None`.

## Ready-entry enrichment (spec §6)

`process_paths()` now writes, alongside the existing fields:

```jsonc
{
  "topic":       "<Title>",          // unchanged
  "title":       "<Title>",          // new alias of topic
  "sfwFlag":     "sfw"|"nsfw",       // unchanged
  "publishDate": "YYYY-MM-DD"|null,  // new
  "language":    "zh"|"en"|null,     // new
  "transcript":  "<sibling note>"|null, // new
  "contentNote": "<sibling note>"|null  // unchanged behaviour
}
```

* **language** — CJK ideograph in the transcript note ⇒ `zh`, else `en`; when
  no note exists it falls back to the title (spec §4). Both empty ⇒ `null`.
  CJK match covers U+3400–U+9FFF (Ext-A + Unified) and U+F900–U+FAFF
  (compatibility); full-width punctuation alone is not counted as Chinese.
* **transcript** — the same-name sibling note (`.txt`, `.md`, or
  `<stem>.content.txt`), or `null` when absent (backend STT fills the gap).
* **publishDate** — from the filename only; the watcher does not schedule. The
  backend maps it to `schedule.startAt` per spec §5.

## Safety rules preserved / touched

* **teaching isolation** — unchanged; still checks `flag == "nsfw"` plus
  `TEACHING_BAD_MARKERS` against `topic + note`, quarantines, notifies, never
  reaches `ready`. New titles are parsed correctly, so the marker scan sees the
  real title.
* **orphan detection** — untouched.
* **thumbnails** — `build_thumb()` untouched; uses `topic` and `sfwFlag`, which
  are populated exactly as before. `title` is an additive alias.
* **nsfw no-frame policy** — preserved: the nsfw branch still skips the video
  download entirely and generates a text-only title card. The only new nsfw
  download is the **plain-text sibling note** (for `transcript`), which contains
  no source pixels. `sfw` still downloads the source for its real frame thumb.
* The old single-suffix `.txt` sidecar fetch was generalised to
  `.txt`/`.md`/`.content.txt` for both branches; notes live only in the
  temporary scan tree and are removed by `_cleanup_scan_sources`.

## Tests

```
cd /home/will/sau-inbox && /home/will/social-auto-upload/.venv/bin/python -m pytest tests/ -q
25 passed in 0.04s

cd /home/will/sau-inbox && python3 -m unittest discover -s tests
Ran 25 tests in 0.008s
OK
```

25 tests total = 23 new (`tests/test_filename_parse.py`) + 2 pre-existing
(`tests/test_watch_drive.py`). System `python3` has no `pytest` module, so the
project venv
(`/home/will/social-auto-upload/.venv/bin/python`) was used for the pytest run;
`unittest` was used with system python as the fallback.

Coverage in the new file: legacy v2 forms (double underscore, empty topic, bare
flag, case-insensitive), v3 with/without date, title-with-spaces, title-with-
underscores, date normalisation, impossible-date rejection, missing-flag
rejection, double-underscore fall-through, `parse_name` back-compat wrapper,
CJK/en/fallback/none language cases, sidecar candidate list, and four
`process_paths()` ready-entry integration tests (nsfw + CJK `.txt`, sfw + `.md`
English, legacy null date/transcript, v3-no-date).

## Constraints honoured

* No STT call, no publish API call, no `state.json` write (tests patch
  `save_state`/`load_state`/`_state_lock`), no `/home/will/social-auto-upload`
  source edit.
* Only `watch.py` and `tests/` (my scope) plus this report were written.
