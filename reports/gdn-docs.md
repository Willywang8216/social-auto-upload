# gdn-docs — Drive filename convention, publishDate→schedule, language routing

Status: **done** (2026-10-12). Subagent `gdn-docs` (feature spec:
[`reports/gdrive-filename-spec.md`](gdrive-filename-spec.md)).

## Scope

Owned files only: `skills/sau-bulk-videos/SKILL.md` and `docs/`. No code, no
`/home/will/sau-inbox` edits.

## Files changed

| file | change |
|---|---|
| `docs/gdrive-filename-convention.md` | **new** — operator + agent reference (convention, layout, transcript/STT, language, ready-entry contract, `publishDate`→`schedule.startAt`, live routing table, checklist) |
| `skills/sau-bulk-videos/SKILL.md` | front-matter `metadata.trigger` extended with new-convention triggers; new subsection **"Drive inbox filename convention (new)"** in §1 linking the doc |

`git status --short skills/ docs/`:

```
 M skills/sau-bulk-videos/SKILL.md
?? docs/gdrive-filename-convention.md
```

## What was documented (spec → doc)

1. **Naming convention** (`docs/... §1`)
   * Primary `<Title with spaces>_<SFW|NSFW>_<YYYYMMDD>.<ext>`; Title = text
     before the final two `_` tokens, spaces allowed; flag case-insensitive;
     date optional, local `Asia/Taipei`; no date → schedule from now.
   * Legacy `<topic?>__<sfw|nsfw>.<ext>` explicitly kept working (spec §1).
   * Video/img decided by extension; missing flag ⇒ `pending` + TG.
2. **Layout** (`§2`) — Drive root `SAU-Inbox/` with `nw/ sw/ both/ teaching/ msl/`,
   each `video/ img/ thumb/`; brand folder = persona, so `both/` →
   `profileIds: [1, 3]`. Verified live with `rclone lsf
   GDrive-willywang8216:SAU-Inbox/`.
3. **Transcript** (`§3`) — same-name sibling `.txt` / `.md` / `<stem>.content.txt`;
   absent ⇒ backend STT (agent does not transcribe).
4. **STT fallback** (`§4`) — base `https://wzw.pp.ua`, model `mimo-v2.6-flash`,
   `/v1/chat/completions` + `input_audio` (not `/v1/audio/transcriptions`),
   `myUtils/llm_client._headers()` for the Cloudflare 1010 workaround, read
   `content or reasoning_content`, ffmpeg pcm_s16le 16 kHz prep. **API keys
   omitted from the doc** — the repo is public; the doc points to the app/vault
   config instead.
5. **Language** (`§5`) — CJK ⇒ `zh` else `en`, fall back to title, stored on the
   ready entry.
6. **Ready-entry contract** (`§6`) — `title`, `sfwFlag`, `publishDate`,
   `language`, `transcript`, `contentNote` (spec §6).
7. **publishDate → submit** (`§7`) — `schedule.startAt = "YYYY-MM-DDT21:00:00"`
   naive `Asia/Taipei`, default hour 21:00; collision handling delegated to
   `_next_free_slot`: ≥30 min same-account gap, ≤3/account/day, plus the
   orchestrator's 5-min fan-out stagger; pinned date is an anchor.
8. **Routing** (`§8`) — live `audience_language` table read from
   `db/database.db` (`config_json.audience_language`), plus a verbatim-restated
   version of the existing SKILL.md per-account table (NW EN 10 / NW ZH 3 /
   SW EN 7 / SW ZH 3 / bilingual `SW Blog` `en,zh-Hant`; Teaching profile 4 and
   MSL profile 10 also listed for completeness).
9. **Skill front-matter** — added triggers: `Drive inbox, SAU-Inbox, filename
   convention, Title_SFW_YYYYMMDD, Title_NSFW_YYYYMMDD, transcript .txt, STT
   fallback, publishDate, language routing`.

## Verification

* Read the spec in full and confirmed each claim traces to a spec section.
* Confirmed live Drive root layout via `rclone lsf` inside the container.
* Confirmed live routing data via `sqlite3 db/database.db` (`accounts`,
  `profiles`).
* Confirmed current inbox parser still accepts only the legacy `__` form
  (`watch.py: parse_name`) — the doc flags the new form as additive, consistent
  with gdn-watcher (A) owning the parser change.
* Link paths checked: `docs/gdrive-filename-convention.md` ↔ `skills/sau-bulk-videos/SKILL.md`.

## Not done / notes

* No STT source edits (owned by `gdn-stt`, B) — STT documented from the spec;
  `myUtils/llm_client.py` had no `wzw.pp.ua` / `mimo-v2.6` code at the time of
  writing, so the doc describes the approved design.
* No live publishes, no DB writes (per spec §7 ownership rules).
