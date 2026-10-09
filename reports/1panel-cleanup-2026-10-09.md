# 1Panel cleanup — verified findings and exact actions

Date: 2026-10-09 · read-only investigation against the live panel

## Access note

There is **no 1Panel MCP server configured** on this host (`~/.config/pi` has none,
and 1Panel's own `mcp_servers` table is empty). I reached the panel by reading its
database and API directly instead:

- panel DB: `/opt/1panel/db/1Panel.db`
- panel API: **`http://localhost:25533`** (HTTP, not HTTPS — HTTPS returns `000`)
- the credentials in `settings` are hashed, so API calls need a session/login; the
  database is readable directly, which is how everything below was measured.

---

## 1. DNS accounts

| id | name | credential | live check |
| --- | --- | --- | --- |
| 1 | `Cloudflare-ryanbossomhot` | Global API Key (`ryanbossomhot@yahoo.com.tw`) | **BROKEN** — token verify `401`, global-key zones `400` |
| 2 | `Cloudflare-forsub` | API Token (`forsubscription1993@gmail.com`) | **WORKS** — token verify `200`, "valid and active" |

**Action for id 1:** replace the Global API Key with an API Token scoped to the
zones that account manages. A Global API Key is deprecated and this one no longer
authenticates, so any site still pointing at account id 1 cannot renew or apply
DNS/SSL changes.

**Action for id 6:** you asked to re-sign it. SSL id 6 is `iamwillywang.com` and
currently reports `ready`, but it is the credential the **9 of 10 orphan sites**
reference, so it is worth re-issuing after the orphans are removed (below).

## 2. Website records: 20 total, **10 orphaned**

An orphan = **no vhost file on disk AND no DNS record**. Both were checked per
site, so this is not a guess.

| id | domain | vhost | DNS | verdict |
| ---: | --- | :-: | :-: | --- |
| 16 | 1.iamwillywang.com | yes | yes | OK |
| **17** | **e.iamwillywang.com** | no | no | **orphan** |
| **21** | **api.iamwillywang.com** | no | no | **orphan** |
| **22** | **short.nakedwill.com** | no | no | **orphan** |
| 23 | o.iamwillywang.com | yes | yes | OK |
| **24** | **pics.cometiba.me** | no | no | **orphan** |
| **26** | **pics.iamwillywang.com** | no | no | **orphan** |
| 27 | vault.iamwillywang.com | yes | yes | OK |
| **28** | **tgdown.iamwillywang.com** | no | no | **orphan** |
| **29** | **tts.iamwillywang.com** | no | no | **orphan** |
| 32 | php.nakedwill.com | yes | yes | OK |
| **38** | **test.iamwillywang.com** | no | no | **orphan** |
| 40 | iamwillywang.com | no* | yes | OK — served by `1.iamwillywang.com.conf`; returns 200 |
| **41** | **wisdomin.life** | no | no | **orphan** (whole domain does not resolve) |
| 42 | stage.sexualwill.com | no | yes | **BROKEN, not orphan** — returns **525** |
| 46 | nakedwill.com | yes | yes | OK |
| 47 | sexualwill.com | yes | yes | OK |
| 48 | latex.iamwillywang.com | yes | yes | OK |
| 49 | appointments.sexualwill.com | yes | yes | OK |
| **50** | **contact.iamwillywang.com** | no | no | **orphan** |

\* id 40's vhost is named differently (`1.iamwillywang.com.conf` owns the apex).

**Orphan ids: `17, 21, 22, 24, 26, 28, 29, 38, 41, 50`** (10 — matches your estimate).

Notes that matter before deleting:

- **id 41 `wisdomin.life`** is the only orphan with an SSL record (id **12**), and
  the **domain itself does not resolve**. If the domain is still registered and you
  intend to use it, keep this row; otherwise remove the site row **and** SSL 12.
- **id 24 `pics.cometiba.me`** — `cometiba.me` also does not resolve at all.
- Removing a website row in 1Panel does **not** delete files. Confirm there is no
  legacy `site_dir` you still want before deleting; none of these 10 has an
  `app_install` attached.

## 3. SSL records

All 6 report `ready` — **there are no `applyError` records now**. Your note
mentioned 9; they are not present in this panel's database, so either they were
already cleared or they were from a different panel/session.

| id | domain | status | sites using it |
| ---: | --- | --- | ---: |
| 6 | iamwillywang.com | ready | 12 (incl. 8 orphans) |
| 9 | nakedwill.com | ready | 3 |
| 10 | cometiba.me | ready | 1 (an orphan) |
| 11 | sexualwill.com | ready | 3 |
| 12 | wisdomin.life | ready | 1 (an orphan) |
| 13 | nakedwill.com | ready | 0 — **duplicate of id 9, unused** |

**Possible cleanup:** SSL 13 duplicates SSL 9 (same domain, unused). SSL 10 and 12
only serve orphan sites; remove them with their sites.

## 4. Suggested order

1. Replace DNS account **id 1**'s credential with an API Token.
2. Decide on **wisdomin.life** and **cometiba.me** (keep the domains or not).
3. Remove the **10 orphan website rows**.
4. Remove SSL **10, 12** (orphan-only) and **13** (unused duplicate of 9).
5. Re-sign SSL **6** (`iamwillywang.com`) — after the orphans are gone, so it is
   issued against the sites that remain.
6. Investigate **stage.sexualwill.com (525)** — it has DNS but no vhost, so
   Cloudflare cannot complete the TLS handshake. Either restore its vhost or
   remove the DNS record.

## What I did NOT do

I changed nothing in 1Panel. Every action above is destructive to panel state and
several require a decision only you can make (whether those domains still matter,
and where account id 1's zones actually live). Say which of the six steps you want
and I will carry them out.
