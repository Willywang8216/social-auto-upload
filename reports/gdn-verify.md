# gdn-verify — independent end-to-end verification

Subagent: **D / `gdn-verify`** (spec §7: "end-to-end verification after A–C; no
source edits"). Status: **done**.

**Read-only on source.** No product code was edited. The only file written by
this session is this report. No publish API call, no DB write, no commit.

* Spec: [`reports/gdrive-filename-spec.md`](gdrive-filename-spec.md)
* Verified: [`reports/gdn-watcher.md`](gdn-watcher.md),
  [`reports/gdn-stt.md`](gdn-stt.md), [`reports/gdn-docs.md`](gdn-docs.md)
* Date: 2026-10-12

## Result summary

| # | item | result |
|---|---|---|
| 1 | Watcher parser tests + independent `parse_filename` cases | **PASS** |
| 2 | STT mocked tests + one live call | **PASS** (one minor defect, see Bug 1) |
| 3 | Prep wiring (`transcribe=True` → chat STT, stores text+language) | **PASS** |
| 4 | Docs (convention / schedule / STT fallback / language routing) | **PASS** |
| 5 | Security (STT key not committable) | **PASS** (with note) |
| 6 | Regression suite | **PASS** |

Bugs found: **1** (minor, non-blocking). Final suite: **1676 passed, 1 skipped,
161 subtests passed**.

---

## 1. Watcher parser — PASS

### Suite

```
cd /home/will/sau-inbox && /home/will/social-auto-upload/.venv/bin/python -m pytest tests/ -q
.........................                                                [100%]
25 passed in 0.04s
```

25 = 23 new (`tests/test_filename_parse.py`) + 2 pre-existing
(`tests/test_watch_drive.py`). Matches the gdn-watcher report.

### Independent `parse_filename` exercise

```python
import watch
for c in [...]: print(repr(c), '->', watch.parse_filename(c))
```

```
'My great title space separated_NSFW_20261015'          -> ParsedName(topic='My great title space separated', flag='nsfw', publish_date='2026-10-15')
'Pride invitation_SFW_20261024'                         -> ParsedName(topic='Pride invitation', flag='sfw', publish_date='2026-10-24')
'台北同志遊行邀請_SFW_20261024'                                 -> ParsedName(topic='台北同志遊行邀請', flag='sfw', publish_date='2026-10-24')
'qixi__nsfw'                                            -> ParsedName(topic='qixi', flag='nsfw', publish_date=None)
'News update_NSFW'                                      -> ParsedName(topic='News update', flag='nsfw', publish_date=None)
'X_NSFW_20260230'                                       -> None
'My__title_NSFW'                                        -> ParsedName(topic='My__title', flag='nsfw', publish_date=None)
'Pride invitation_SfW_20261024'                         -> ParsedName(topic='Pride invitation', flag='sfw', publish_date='2026-10-24')
'qixi-30-virgin__nsfw'                                  -> ParsedName(topic='qixi-30-virgin', flag='nsfw', publish_date=None)
'__sfw'                                                 -> ParsedName(topic='', flag='sfw', publish_date=None)
'title_20261015'                                        -> None
'just a title'                                          -> None
'bad__flag'                                             -> None

detect_language('', '台北同志遊行邀請') = zh
detect_language(None, 'English title') = en
detect_language(None, None) = None
```

Every required case is correct:

* space-separated title → `topic='My great title space separated'`, `nsfw`,
  `2026-10-15` ✓
* `Pride invitation_SFW_20261024` → `sfw`, `2026-10-24` ✓
* Chinese title parsed intact ✓
* legacy `qixi__nsfw` → `('qixi','nsfw',None)` ✓
* no-date `News update_NSFW` → `publish_date=None` ✓
* impossible `X_NSFW_20260230` → **rejected** (`None`) ✓

Ready-entry wiring also verified in `watch.py:692-705`: the emitted entry carries
`title`, `sfwFlag`, `publishDate`, `language`, `transcript`, `contentNote`.

## 2. STT — PASS (with Bug 1)

### Mocked suite

```
cd /home/will/social-auto-upload
.venv/bin/python -m pytest tests/test_llm_stt.py -q
.............                                                            [100%]
13 passed in 0.42s
```

13 (the gdn-stt report said 12; one test was added after that report — no
problem).

### One live call

Source of `SAU_STT_*`: the gitignored `.env` (loaded into the process env for
this call only). `ffmpeg -ss 60 -t 20 -i
videoFile/_library/SW/20260912_postop_5wk_zh.mp4 -vn -ac 1 -ar 16000 -c:a
pcm_s16le /tmp/gdn-verify-stt.wav` → 640,078-byte, 20.000 s mono WAV.

```
base: https://wzw.pp.ua
model: mimo-v2.6-flash
key set: True len 51
elapsed_s: 15.6
language: zh
text_len: 99
content_len: 0 reasoning_len: 99
model_used: mimo-v2.6-flash
----- TRANSCRIPT -----
<think>没有生病，然后也没有感冒，可是就是提不起劲来，软软懒懒的这样子。我觉得可能是身体某一方面可能需要休息或一些补充吧，像最近会想去中医调理一下。那大家有什么好的推荐的中医吗？在大台北地区。
----------------------
```

* non-empty transcript ✓
* `language == 'zh'` ✓
* winning field was `message.reasoning_content` (`content_len: 0`) — exactly as
  the spec predicted ✓

**Defect:** the transcript begins with a literal `<think>` tag (Bug 1 below).

## 3. Prep wiring — PASS

```
.venv/bin/python -m pytest tests/test_campaign_media_prep.py tests/test_llm_client.py -q
29 passed, 7 subtests passed in 1.10s
```

Code inspection of `myUtils/campaign_media_prep.py`:

* `artifacts_context` (line 380) initialises `transcriptText` from
  `request_data` and `language` to `None`; a supplied transcript seeds language,
  else the title is classified (lines 391-409).
* `should_transcribe = request_data.transcribe or (no transcript and has_ai)`
  (line 822); when true and there is a video and no `transcriptText`, it runs
  `media_pipeline.extract_video_audio` then
  `llm_client.transcribe_audio_via_chat(...)` (line 845) and stores
  `artifacts_context["transcriptText"] = transcript.text` and
  `artifacts_context["language"] = transcript.language or ...` (lines 859-861),
  plus the `transcript` artifact and DB metadata.
* `_resolve_stt_config` (line 306): `SAU_STT_*` env wins over
  `aiServices[0]`, consistent with the two-gateway design.

The dedicated integration test `CampaignPrepSttWiringTests::
test_transcribe_true_uses_chat_stt_and_stores_language` locks this in.

## 4. Docs — PASS

`docs/gdrive-filename-convention.md` (9 sections) and
`skills/sau-bulk-videos/SKILL.md` ("Drive inbox filename convention (new)")
both document all four required points:

| required | docs/gdrive-filename-convention.md | skills/sau-bulk-videos/SKILL.md |
|---|---|---|
| convention `<Title>_<SFW\|NSFW>_<YYYYMMDD>` + legacy `__` | §1 | "Drive inbox filename convention (new)" |
| `publishDate` → `schedule.startAt` naive `Asia/Taipei`, default 21:00 | §7 (`"2026-10-15"` ⇒ `"2026-10-15T21:00:00"`, line 187-189) | lines 228-230 |
| STT fallback (`/v1/chat/completions`, `input_audio`, CF-1010 headers, `content or reasoning_content`) | §4 | "backend runs STT … agent does not transcribe" |
| language routing (CJK ⇒ `zh` else `en`, then per-account `audience_language`) | §5 + live routing table §8 | "Language is chosen automatically, per account" + bullet |

The live routing table in §8 matches the account data described in
`reports/gdn-docs.md`.

## 5. Security — PASS (with note)

```
KEY=$(grep '^SAU_STT_API_KEY=' .env | cut -d= -f2-)   # len 51
grep -rIn -- "$KEY" myUtils/ tests/ docs/ skills/ reports/   → no output (exit 1)
git grep -I --fixed-strings -- "$KEY" -- .                   → no output (exit 1)
```

* The key appears **nowhere** in `myUtils/ tests/ docs/ skills/ reports/`. ✓
* It appears in **no git-tracked file** at all (`git grep` exit 1). ✓
* `myUtils/llm_client.py:42` deliberately sets `_DEFAULT_STT_API_KEY = ""`
  (not baked in), so the gdn-stt report's claim that "the spec API key is the
  module default" is **stale** — the code is correct/safer than that report
  states.
* `docs/gdrive-filename-convention.md` §4 withholds the keys ("**never** commit
  the key — this repo is public") and `reports/gdrive-filename-spec.md` contains
  no key. ✓
* Note (not a failure): the same 51-char key also exists on disk in the
  gitignored `backups/env-pre-rotation/.env.bak-*` files (where it is the
  `wzw.pp.ua` entry of `SAU_LLM_POOL`), and in the gitignored `.env`. Both
  `backups/` (`.gitignore:251`) and `.env` (`.gitignore:115`) are ignored, so
  neither is committable. Rotation of that key would still be prudent since it
  is dual-use (LLM pool + STT).

## 6. Regression — PASS

```
.venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1676 passed, 1 skipped, 161 subtests passed in 114.22s (0:01:54)
```

Baseline was 1663 passed / 1 skipped. The +13 delta is exactly the 13 new tests
in `tests/test_llm_stt.py`; nothing regressed and the skipped count is unchanged.

---

## Bug 1 (minor) — `<think>` tag leaks into the transcript

**Where:** `myUtils/llm_client.py:185` (`_split_transcript_language`), reached
from `myUtils/llm_client.py:664-667` (`transcribe_audio_via_chat`). Stored
verbatim by `myUtils/campaign_media_prep.py:858-859` and `:875`.

**What:** the gateway's `reasoning_content` is returned as
`<think><transcript…>`; `_split_transcript_language` only strips the trailing
`LANGUAGE=zh|en` line and never removes a leading/inline `<think>` (or paired
`…`) marker. The literal tag therefore lands in `transcriptText`, the
`transcript` artifact, and the draft-generation prompt.

**Repro (live, this session):** transcript began with `<think>没有生病，…`
(see item 2). Deterministic offline repro with a mocked HTTP session:

```
$ .venv/bin/python - <<'PY'
from unittest.mock import MagicMock
from myUtils import llm_client
class FakeResp:
    status_code = 200
    def raise_for_status(self): pass
    def json(self):
        return {"model": "mimo-v2.6-flash", "choices": [{"message": {
            "content": "", "reasoning_content": "<think>你好，這是測試字幕。"}}]}
sess = MagicMock(); sess.post.return_value = FakeResp()
r = llm_client.transcribe_audio_via_chat("/tmp/gdn-verify-stt.wav", session=sess, api_key="x")
print("text repr:", repr(r.text))
print("<think> leaked:", "<think>" in r.text)
PY
text repr: '<think>你好，這是測試字幕。'
<think> leaked: True
```

**Suggested fix (not applied — D is read-only, spec §7 "no source edits"):** in
`_split_transcript_language`, strip a leading/inline `...</think>` and/or a
leading `<think>` before/after the `LANGUAGE=` strip, e.g.

```python
_think = re.compile(r"(?is)<think>.*?(?:</think>|$)", re.DOTALL)
text = _think.sub("", str(raw or ""))
```

and add a regression test asserting `transcript.text == "你好，這是測試字幕。"`.
Impact is cosmetic-to-moderate (a stray token in the caption seed), not a
security or scheduling failure, hence non-blocking.

## Constraints honoured

* No product code edited; no `/home/will/sau-inbox` edit.
* Only `reports/gdn-verify.md` written.
* No publish API call, no DB/campaign write, no commit.
* Exactly one live STT call made (per spec §7).
