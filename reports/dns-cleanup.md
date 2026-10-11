# DNS / 1Panel + Cloudflare cleanup

Date: 2026-10-11 (CST) · host `social-auto-upload` · operator request: *"purge that broken dns."*

Panel: `1panel-core` at `http://localhost:25533` (HTTP only; `https://localhost:25533` → `000`).
Panel DB: `/opt/1panel/db/1Panel.db` (SQLite, **WAL** mode, root-only writes).

> This report paste is real command output. Where a value would expose a credential it is
> omitted or reduced to a length/hash. No secret token is printed anywhere.

---

## 0. TL;DR

| Item | Verdict | Action taken |
| --- | --- | --- |
| DNS account **id 1** `Cloudflare-ryanbossomhot` | **PROVEN BROKEN** — token verify `401 Invalid API Token`, zones `403 Invalid access token` | **NOT removed** — it is in use by SSL id 6 (`iamwillywang.com`). Operator must paste a valid token. |
| DNS account **id 2** `Cloudflare-forsub` | **WORKS** — verify `200 active`, 3 zones visible | Used to verify/repair DNS. |
| 8 orphan website rows (no DNS + no vhost) | Provably stale | **7 removed**, 1 kept (running app), see §3 |
| 2 leftover `website_domains` rows for sites already deleted (`pics.cometiba.me`, `wisdomin.life`) | Provably orphaned (parent gone) | **Removed** |
| `stage.sexualwill.com` (DNS exists, no vhost, HTTP 525) | Should **not** exist (NotionNext deployment gone) | **Cloudflare A record removed** + stale panel row removed, see §4 |
| Zones for domains we no longer own | None in the working account | Nothing removed; `2003.free.hr` left (actively used by mailserver) |

Backup: **`backups/1panel/1Panel.db.bak-pre-dnspurge-20261011-175201`** (online SQLite backup, 12,005,376 bytes).
Deleted Cloudflare record archived: **`reports/stage-dns-record-backup.json`**.

Post-change health: `1panel-core` = `active`, `GET http://localhost:25533/` = `200`,
`PRAGMA integrity_check` = `ok`, live sites still serve (see §7).

---

## 1. Access notes (re-verified)

- Panel API is **plain HTTP** on `25533`; HTTPS gives `000`.
- The DB is readable directly, but even a read-only `sqlite3` open fails as user `will`
  (`attempt to write a readonly database`) because it is **WAL** mode and the `-shm` needs
  write access. All reads *and* writes were therefore done as root in a throwaway container:
  ```bash
  docker run --rm -i -v /opt/1panel/db:/db python:3.12-slim python3 - <<'PY'
  import sqlite3; ...
  PY
  ```
  (Equivalent to the documented `alpine + apk add sqlite` method; no network needed.)
- No schema triggers exist (`SELECT name FROM sqlite_master WHERE type='trigger'` → `[]`) and
  `PRAGMA foreign_keys` is `0`, so guarded deletes are the only safety net — every delete below
  carries an explicit `EXISTS` / `NOT EXISTS` predicate.

Credential shape (without printing values): both DNS accounts store JSON
`{"email": ..., "apiKey": ...}`; the `apiKey` is a **40-char Cloudflare API Token** in both
cases (not a global key — sending it as `X-Auth-Key` gives
`6103 Invalid format for X-Auth-Key header`, which is why the earlier "global-key zones 400"
observation happened).

---

## 2. DNS account verification (authoritative)

```
--- id=1 Cloudflare-ryanbossomhot (token_len=40) ---
  /user/tokens/verify status=401 success=False
    errors= [{'code': 1000, 'message': 'Invalid API Token'}]
  /zones status=403 success=False
    errors= [{'code': 9109, 'message': 'Invalid access token'}]
--- id=2 Cloudflare-forsub (token_len=40) ---
  /user/tokens/verify status=200 success=True
    result.id=40317a6594003292e8b29548b873befc status=active
  /zones status=200 success=True
    zone 2003.free.hr      status=active
    zone nakedwill.com     status=active
    zone sexualwill.com    status=active
```

**Account 1 is referenced, so it cannot be deleted.** The reference is
`website_ssls.dns_account_id` (there is no `websites.website_dns_id` column in this schema):

```
=== dns account refs ===
(1, 'Cloudflare-ryanbossomhot', 1)   # SSL id 6  iamwillywang.com
(2, 'Cloudflare-forsub', 2)          # SSL id 9, 11
```

SSL id 6 covers `*.iamwillywang.com,email.newsletter.iamwillywang.com,email.mg.iamwillywang.com`
with `auto_renew=1`. Deleting account 1 would permanently break renewal for the live
`iamwillywang.com` site and its email subdomains. **Left in place.**

### Operator action you CANNOT delegate to me

Paste a valid Cloudflare **API Token** for the `iamwillywang.com` Cloudflare account into
this one panel field:

1. Open the panel UI → **网站 / Websites → DNS账户 / DNS Accounts**.
2. Edit **`Cloudflare-ryanbossomhot`** (id 1).
3. Replace the token with a Cloudflare API token that has **Zone:Read** and **DNS:Edit**
   for the `iamwillywang.com` zone (create at Cloudflare → My Profile → API Tokens →
   "Edit zone DNS" template).
4. Save. The token will then verify `200` and SSL id 6 auto-renew can work again.

If you prefer the API, the panel's own endpoint is `POST /api/v1/websites/dns/update`
(body `{id:1, name, type:"CloudFlare", authorization:<json>}`), but the UI is safer because it
validates the credential.

Side note (not changed): `website_ssls` id 6 has a stale `expire_date` of `2025-12-27`
while the served certificate is valid until `Dec 4 2026`, and SSL id 11 carries an old
`message` about `zone could not be found` even though account 2 now sees `sexualwill.com`.
Those are stale metadata rows, not DNS breakage — re-check after fixing account 1.

---

## 3. Orphan website rows

Definition used (and verified per row): **public DNS does not resolve AND no vhost file
exists** in the running OpenResty config (`/opt/1panel/www/conf.d`, bind-mounted to
`/usr/local/openresty/nginx/conf/conf.d` inside the `openresty` container).

Before: **18** `websites`, 20 `website_domains`.

| id | domain | public DNS | vhost | site dir | app | verdict |
| ---: | --- | :-: | :-: | :-: | :-: | --- |
| 17 | e.iamwillywang.com | NXDOMAIN | no | leftover ssl/proxy | – | **removed** |
| 21 | api.iamwillywang.com | NXDOMAIN | no | none | – | **removed** |
| 22 | short.nakedwill.com | NXDOMAIN | no | leftover ssl | – | **removed** |
| 26 | pics.iamwillywang.com | NXDOMAIN | no | none | – | **removed** |
| 28 | tgdown.iamwillywang.com | NXDOMAIN | no | none | – | **removed** |
| 29 | tts.iamwillywang.com | NXDOMAIN | no | none | – | **removed** |
| 50 | contact.iamwillywang.com | NXDOMAIN | no | none | – | **removed** |
| 38 | test.iamwillywang.com | NXDOMAIN | no | none | **app_install 32 RUNNING** | **kept** (in use) |
| 42 | stage.sexualwill.com | resolves | no | none | – | handled in §4 |

Removal of a panel row does **not** delete files or containers. The two removed rows with
leftover directories (`e.iamwillywang.com`, `short.nakedwill.com`) keep their files on disk;
only the zombie panel/DNS entry was purged. `e.iamwillywang.com` was an old reverse-proxy
to SnappyMail (superseded by `webmail.iamwillywang.com.conf`); `short.nakedwill.com` held
only cert files. Neither had a vhost, so nothing served them.

`test.iamwillywang.com` (id 38) was deliberately **not** removed: it has
`app_install_id=32` (`testiamwillywangcom`, container `1Panel-php8-5Mkn`, Running) and
`runtime_id=3`. Removing the website row directly in SQL would leave a running app with no
owning site — exactly the "in use" case we must not touch. Remove it from the panel UI
(which also cleans the app) if it is truly unwanted.

### Exact guarded SQL executed

```sql
BEGIN IMMEDIATE;

-- (a) leftover domain rows whose parent website is already gone
DELETE FROM website_domains
WHERE id IN (25,44)
  AND NOT EXISTS (SELECT 1 FROM websites w WHERE w.id = website_domains.website_id);

-- (b) domain rows belonging to the orphan websites being removed
DELETE FROM website_domains
WHERE website_id IN (17,21,22,26,28,29,50,42)
  AND EXISTS (SELECT 1 FROM websites w
              WHERE w.id = website_domains.website_id
                AND w.primary_domain IN ('e.iamwillywang.com','api.iamwillywang.com',
                    'short.nakedwill.com','pics.iamwillywang.com','tgdown.iamwillywang.com',
                    'tts.iamwillywang.com','contact.iamwillywang.com','stage.sexualwill.com'));

-- (c) the orphan websites themselves (no app, no runtime, no domains left)
DELETE FROM websites
WHERE id IN (17,21,22,26,28,29,50,42)
  AND app_install_id = 0
  AND runtime_id = 0
  AND primary_domain IN ('e.iamwillywang.com','api.iamwillywang.com','short.nakedwill.com',
        'pics.iamwillywang.com','tgdown.iamwillywang.com','tts.iamwillywang.com',
        'contact.iamwillywang.com','stage.sexualwill.com')
  AND NOT EXISTS (SELECT 1 FROM website_domains d WHERE d.website_id = websites.id);

COMMIT;
```

Output:

```
BEFORE websites=18 website_domains=20
DELETED orphan website_domains (parent missing): 2
DELETED website_domains of orphan sites: 8
DELETED orphan websites: 8
AFTER websites=10 website_domains=10
remaining orphan website_domains (parent missing): []
remaining website ids: [16, 23, 27, 32, 38, 40, 46, 47, 48, 49]
```

Remaining (all resolve + serve, except the flagged id 38):

```
16 1.iamwillywang.com         (SSL 6)   23 o.iamwillywang.com        (SSL 6)
27 vault.iamwillywang.com     (SSL 6)   32 php.nakedwill.com         (SSL 9)
38 test.iamwillywang.com      (SSL 6, app 32 — kept, stale) 
40 iamwillywang.com           (SSL 6)   46 nakedwill.com             (SSL 9)
47 sexualwill.com             (SSL 11)  48 latex.iamwillywang.com    (SSL 6)
49 appointments.sexualwill.com(SSL 11)
```

No `website_ssls` or `website_dns_accounts` row lost its last reference: SSL 6 still has 6
sites, SSL 9 has 2, SSL 11 has 2; DNS account 1 still referenced by SSL 6, account 2 by
SSL 9 + 11.

---

## 4. `stage.sexualwill.com`

Evidence collected:

- **Panel website**: id 42, `proxy`, `website_ssl_id=11`, `app_install_id=0`, `runtime_id=0`.
- **Vhost**: none. `grep -rl stage.sexualwill /opt/1panel/www` finds only the *old*
  OpenResty `error.log`; the running `conf.d` has no `stage.sexualwill.com.conf`. The
  `sexualwill.com.conf` vhost `server_name` list does not include `stage`.
- **Site dir**: none (`/opt/1panel/www/sites/stage.sexualwill.com` absent).
- **Backend**: no container/process/image named `stage` or `notionnext`; only stale
  Docker buildx cache references. The record's own comment was **`NotionNextSite`**,
  i.e. it pointed at a NotionNext blog deployment that no longer exists here.
- **HTTP**: `https://stage.sexualwill.com` → **525** (Cloudflare TLS handshake failure,
  because no origin vhost/SNI exists).
- **References**: only panel row 42, SSL 11's wildcard `*.sexualwill.com`, and historical
  logs/reports. Nothing consumes it.

Cloudflare record (archived to `reports/stage-dns-record-backup.json`):

```
id=31a2faa09bf499cb816f206d7c99382e
A stage.sexualwill.com -> 129.150.36.167  proxied=true ttl=1
comment="NotionNextSite"  created 2024-03-26  modified 2024-11-22
```

**Decision: it should not exist.** Restoring a vhost would mean inventing a backend (there
is no NotionNext deployment to proxy to). I removed the **Cloudflare A record** instead,
then removed the now-orphaned panel row 42 (and its `website_domains` row 45) under the
orphan rule in §3.

```
DELETE https://api.cloudflare.com/client/v4/zones/71f475ab.../dns_records/31a2faa0...
DELETE status=200 success=True errors=[]
AFTER stage A records: []
```

Verification (authoritative nameservers `lex.ns.cloudflare.com`, `veda.ns.cloudflare.com`):

```
$ dig +short A stage.sexualwill.com @lex.ns.cloudflare.com   -> (empty)
$ dig +short A stage.sexualwill.com @veda.ns.cloudflare.com  -> (empty)
$ getent hosts stage.sexualwill.com                          -> no result
```

(Immediately after the delete, `dig @1.1.1.1` briefly still returned cached Cloudflare
anycast IPs; a second authoritative query returned empty and the system resolver returned
nothing. The record is gone from the zone — the API reports 38 records now vs 39 before,
with zero `stage`/wildcard records.)

---

## 5. Zones / domains we no longer own

Working token (account 2) sees exactly three zones, all legitimately in use:

| zone | status | evidence of use | action |
| --- | --- | --- | --- |
| `nakedwill.com` | active | live site + mail (MX/SPF/DKIM/DMARC) | left |
| `sexualwill.com` | active | live site + mail + subdomains | left (only stage removed) |
| `2003.free.hr` | active | **actively used by the local mailserver**: `postfix/vhost` contains `2003.free.hr`, an account `twitter001@2003.free.hr` exists, `SRS_EXCLUDE_DOMAINS` lists it, plus MX/SPF/DKIM/DMARC records | left |

The two dead domains from the earlier cleanup, `cometiba.me` and `wisdomin.life`, now have
**no NS and no SOA** (not registered/delegated) and are **not** in the working account's
zones. Their leftover panel `website_domains` rows were removed (§3a). They cannot be
inspected in account 1 (that token is broken), but with no registration there is nothing
live to purge from Cloudflare via the working token.

No zone record inside a live zone points at a domain we do not own.

---

## 6. Backup

```
BACKUP_WRITTEN /backup/1Panel.db.bak-pre-dnspurge-20261011-175201 12005376
-rw-r--r-- 1 root root 12005376 Oct 11 17:52 \
  backups/1panel/1Panel.db.bak-pre-dnspurge-20261011-175201
```

Taken with SQLite's online backup API (`src.backup(dst)`), so it is consistent even with
the panel connected. Pre-existing backups `...pre-purge-20261010-091734` and
`...pre-ssl13-20261010-130436` are untouched.

---

## 7. Post-change verification

```
=== panel health ===
active
panel / -> HTTP 200

=== live sites ===
https://iamwillywang.com     HTTP 200
https://nakedwill.com        HTTP 200
https://sexualwill.com       HTTP 307

=== stage after ===
stage no longer resolves (expected)

=== DB ===
integrity_check: ok
dangling website_domains: 0
websites pointing at missing ssl: 0
```

---

## 8. Deliberately left alone

- **DNS account id 1** — in use by SSL 6; needs a new token from you (§2).
- **`test.iamwillywang.com` (website id 38)** — NXDOMAIN + no vhost, but a running
  `app_install` (32) depends on it. Remove via the panel UI, not SQL.
- **`2003.free.hr` Cloudflare zone** — active mail domain for the local mailserver.
- **All live sites**: `iamwillywang.com`, `1/o/vault/latex.iamwillywang.com`,
  `nakedwill.com`, `php.nakedwill.com`, `sexualwill.com`, `appointments.sexualwill.com`.
- **Manually-managed OpenResty vhosts without panel rows** (e.g. `home`, `share`, `tab`,
  `voc`, `webmail`, `allapi`, `teamtime`, `up`, `socialupload`, `willywangdata.*`) — not
  part of the panel DB and out of scope.

### Additional observations (not actioned — worth a follow-up)

These are other `sexualwill.com` A-records that currently have no matching vhost, so they
present the same 525/edge-error pattern as `stage` did. I did **not** touch them because
they are not panel websites and I cannot prove they are unwanted:

- `new.sexualwill.com` (`comment="new sexualwill site"`, site dir with certs, no vhost)
- `go.sexualwill.com` (`comment="asking out team go"`, no vhost)
- `contacts.sexualwill.com` → `192.0.2.1` (`comment="Linktree"`; TEST-NET-1 address)

Decide per domain whether to add a vhost or delete the record.
