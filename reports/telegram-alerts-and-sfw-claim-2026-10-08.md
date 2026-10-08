# Telegram alert triage + the "9 SFW NW+SW not submitted" claim

Date: 2026-10-08 (local CST)
Status: two code bugs fixed, deployed. One operator claim disproven.

## 1. The Telegram alerts were test fixtures - not an incident

The operator forwarded ~11 `[SAU] Publish failed` messages. Every one is a
fixture from the test suite, proven three ways:

| Evidence | Result |
| --- | --- |
| `SELECT * FROM publish_jobs WHERE id=1` | **empty** - the real table starts at job #9 |
| accounts `acct-1`, `fb-bluesky`, `deliv` | **do not exist** in `accounts` |
| error strings `always fails` / `bluesky said no` | grep hits in `tests/test_worker.py` |

Cause: tests deliberately exercise the failure path, several patch nothing, and
the suite loads the repo `.env` - so the alert sender picked up the **live bot
token** and delivered them.

Fixed in `tests/conftest.py`: blank `SAU_ALERT_TELEGRAM_BOT_TOKEN`,
`SAU_ALERT_TELEGRAM_CHAT_ID`, and the TG-review pair for the whole suite.
Verified `send_ops_alert(...)` now returns `delivered: False` in-test.

## 2. Every MTProto Telegram account failed its connection check

All four Telegram accounts answered the connection check with:

    'str' object has no attribute 'get'

`validate_telegram_config_live` returns `chats` as a list of chat-id **strings**
for MTProto, but the caller assumed the bot-API shape
`{"chatId": ..., "result": {...}}` and called `.get('result', {})` on each entry.
Publishing was never affected - only the check - so it stayed invisible.

Fixed in `sau_backend.py`; now handles both shapes.

## 3. Your Telegram profiles are set up correctly

| profile | account | lang | chats |
| --- | --- | --- | --- |
| NW | NW TG 本人 (116) | en | `@nakedwilltgchannel`, `@nakedwill` |
| NW | NW TG 中文 (127) | zh-Hant | `@nakedwillzh`, `@nakedwillzhchat` |
| SW | SW TG 本人 (117) | en | `@sexualwilltgchannel`, `@sexualwill` |
| SW | SW TG 中文 (122) | zh-Hant | `@nakedsexualwei`, `@nakedsexualweiwei` |

Two per profile (EN + zh-Hant), all enabled, all MTProto with a valid session.
The only defect was the check bug above; the routing was already right.

## 4. The "9 SFW NW+SW files not submitted" claim - RIGHT, for the wrong reason

The claim was 9 files "planned and given dates but not yet submitted" because
"the app kept dropping the 24-account submits". **Corrected after checking by
content, not filename** (an earlier draft of this report got this wrong).

What is actually true:

* 23 `SFW_NWSW` campaigns exist, so submits were **not** silently dropped.
* But **22 of the 23 records pointed at file paths that do not exist on disk.**
  Only 1 resolved. The re-uploads had created new UUIDs, and the campaign
  records referenced the retired ones.
* The same videos DO exist on disk under the newer UUIDs:

  ```
  MISSING : <old-uuid>_SFW_NWSW_mroning_sfw.mp4
  ON DISK : 47c6c322-173b-4909-b07b-2e0fa0045dfd_SFW_NWSW_mroning_sfw.mp4
  ```

So a file that "was planned" could not be published: its record pointed nowhere.
That is the same path-resolution failure class as the earlier `/home/will` bug,
in its second form - **stale UUID**, not wrong mount prefix.

### Repaired

Re-pointed the 22 unresolvable records at the on-disk copy with the matching
description (DB backed up first to `db/database.db.bak-20261008-222019`):

```
resolved=1  fixed=22  unresolvable=0
after: 7 OK / 0 MISS  (was 1 OK / 22 MISS)
```

Also repaired `file_record` 1038, whose filename had an external upload tool's
504-retry log baked into it:

```
15:42:37 upload retry 1 for SFW NW+SW sfw.mp4 (error code: 504)
15:44:04 upload retry 2 for SFW NW+SW sfw.mp4 (error code: 504)
20d9394f-..._SFW_NWSW_sfw.mp4
```

### Not a 24-account limit

`maxAccounts` allows up to 500. The `needs_review` campaigns (2530-2597) failed
with **"No publishable posts queued"** because their media could not be prepared -
every account was then filtered out. The account count was never the cause.

## Outstanding operator items

* **Reddit r/gaybrosgonemild** - now fails with a clear, non-retryable message
  (it bans text posts and needs a public URL). Either give the account a public
  storage backend or drop the subreddit. `r/GayBody` banned the account outright.
* **file_record 1038** - rename to the real on-disk file, then re-run 2534/2535.
* **Sociamonials dedupe** - 1,949.5 MB reclaimable, dry-run only, awaiting your
  approval before deleting remote assets.
* **X API credits** - still depleted (402).
* **Account 108** (YT Willy Dev tutor) needs OAuth re-consent.
