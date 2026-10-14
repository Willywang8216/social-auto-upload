# X long-form Articles + Reddit paused

Date: 2026-10-14

## Reddit: stopped

`SAU_REDDIT_ENABLED=0` (in `.env`, present in the container). The worker now
fails a Reddit target immediately with *"Reddit publishing is disabled by the
operator (SAU_REDDIT_ENABLED=0)"* instead of burning retries or the bundle.social
monthly quota. Reason: `u/ryanbossom` is restricted at Reddit — posts fail even to
its own profile (`RD:SUBREDDIT_NOTALLOWED`) and media uploads get `RD:403`, while
the direct API tokens are dead. Nothing in our code can fix an account
restriction. Re-enable with `SAU_REDDIT_ENABLED=1` once a healthy account is
connected.

## X Articles: working

The X Articles **API** (`POST /2/articles/draft` → `/2/articles/{id}/publish`)
needs a user-context OAuth token. Every stored token for these accounts is dead:
OAuth2 refresh returns `invalid_request` (`_needsReconnect=True`), and the
OAuth 1.0a env pair 401s. The accounts are X **Premium** (which is what gates the
editor), so Articles are published through the browser with the saved session —
the same mechanism the other platforms here already use.

**Command**
```bash
sau x article --cookie cookies/twitter/sw/nakedhappylife.json \
  --file article.md --caption "…"
# or: --account <cookie-stem> --profile <slug>
```
`article.md`'s first line (or `# Title`) is the title; blank-line-separated blocks
become paragraphs.

**Verified live:** <https://x.com/nakedhappylife/status/2110282205369094200>
(published 2026-10-14, article card + caption).

### How it works (`uploader/twitter_uploader/article.py`)

1. `https://x.com/compose/articles` → click **Write**
2. fill the title textarea, type the body into the DraftJS editor
3. click **Publish** → X opens the post composer with the article attached
4. the composer's Publish stays disabled **until the post has text**, so a caption
   is required; click it and the `CreateTweet` call creates the post

Selectors are locale-agnostic (`撰寫`/`Write`, `發佈`/`Publish`) because the
accounts run a Chinese UI.

## Account status (2026-10-14)

| account | cookie | Premium | Articles |
| --- | --- | --- | --- |
| `will_sexual` (sexualwill) | ✅ `cookies/twitter/sw/sexualwill.json` | ✅ | ✅ |
| `nakedhappylife` | ✅ `cookies/twitter/sw/nakedhappylife.json` | ✅ | ✅ |
| `model_will` | ✅ `cookies/twitter/nw/model_will.json` | ✅ | ✅ |
| `willytutordev` | ❌ none | (OAuth only) | needs a login |

The cookie files were **root-owned mode 600**, unreadable by the worker (uid
1000); ownership was fixed to `1000:1000`. If they go root-owned again the
Articles command (and every cookie platform) will fail with a permission error.

## Cleanup TODO

A test post created while mapping the flow is still live and should be deleted:
`https://x.com/will_sexual/status/2110279404459033052` ("SAU test draft (ignore)").
Also `https://x.com/nakedhappylife/status/2110282205369094200` is the real proof
article — keep or delete as preferred.

## Follow-ups worth doing

* Reconnect OAuth 2.0 (PKCE) for these accounts so the **API** path can be used —
  it is simpler and supports scheduling (the browser flow publishes immediately).
* Rich formatting (headings/bold/images) in the browser publisher: the editor has
  a toolbar (`btn-bold`, `btn-ul`, …) and supports cover images via
  `input[data-testid='fileInput']`; v1 writes title + paragraphs.
* `willytutordev` needs a cookie login before it can publish.
