#!/usr/bin/env python3
"""Audit a Reddit account's configured subreddits against their live rules.

Why this exists
---------------
We were banned from r/GayBros and r/GayBody, refused by r/gaybrosgonemild with
NO_SELFS, and rate-limited for retrying it - all against subreddits that publish
rules we never read. This script reads them.

It reports, for each subreddit an account is configured to post to:

  * the live facts (subscribers, submission mode, NSFW flag, flair),
  * the published rules,
  * whether the registry in ``myUtils/subreddits.py`` says our content fits,
  * and, if it does not, the reason - plus subreddits that *would* accept it.

It writes nothing. Changing the registry is a deliberate act, done by hand from
what this prints.

Usage
-----
    python scripts/subreddit_audit.py --account 105
    python scripts/subreddit_audit.py --account 106 --content nudity
    python scripts/subreddit_audit.py --account 105 --recommend
"""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from myUtils import subreddits as sr  # noqa: E402

USER_AGENT = "sau-subreddit-audit/1.0"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_ROOT = "https://oauth.reddit.com"


def _db_path() -> Path:
    import os

    configured = os.environ.get("SAU_DB_PATH")
    if configured:
        return Path(configured)
    return REPO_ROOT / "db" / "database.db"


def _access_token(config: dict) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": config["refreshToken"],
        }
    ).encode()
    basic = base64.b64encode(
        f"{config['clientId']}:{config['clientSecret']}".encode()
    ).decode()
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Authorization": f"Basic {basic}", "User-Agent": USER_AGENT},
    )
    return json.loads(urllib.request.urlopen(request, timeout=60).read())["access_token"]


def _api(token: str, path: str, **params):
    url = API_ROOT + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    )
    return json.loads(urllib.request.urlopen(request, timeout=60).read())


def _account(db: Path, account_id: int) -> dict:
    connection = sqlite3.connect(str(db))
    try:
        row = connection.execute(
            "SELECT config_json FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SystemExit(f"no account with id {account_id} in {db}")
    return json.loads(row[0])


def _karma(token: str) -> int:
    me = _api(token, "/api/v1/me")
    return int(me.get("link_karma") or 0) + int(me.get("comment_karma") or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", type=int, required=True)
    parser.add_argument(
        "--content",
        default=sr.NUDITY,
        choices=[sr.NUDITY, sr.EXPLICIT, sr.SEX_POSITIVE, sr.SUGGESTIVE, sr.SFW],
    )
    parser.add_argument(
        "--recommend",
        action="store_true",
        help="also list subreddits that would accept this content",
    )
    parser.add_argument(
        "--live", action="store_true", help="fetch each subreddit's live about+rules"
    )
    args = parser.parse_args()

    config = _account(_db_path(), args.account)
    subreddits = config.get("subreddits") or []
    if not subreddits:
        raise SystemExit("account has no configured subreddits")

    karma = None
    token = None
    if config.get("refreshToken"):
        try:
            token = _access_token(config)
            karma = _karma(token)
        except Exception as exc:  # pragma: no cover - network dependent
            print(f"! could not read account karma: {exc}", file=sys.stderr)

    print(f"account {args.account}  content={args.content}  karma={karma}")
    print("=" * 78)

    registry = sr.get_registry()
    rows = registry.audit(subreddits, content_kind=args.content, karma=karma)
    for name, reason in rows:
        profile = registry.get(name)
        verdict = "OK      " if reason is None else "REFUSED "
        reach = f"{profile.subscribers:,}" if profile else "unknown"
        print(f"{verdict} r/{name:<24} {reach:>10} subs")
        if reason:
            print(f"          {reason}")
        if args.live and token and profile:
            try:
                about = _api(token, f"/r/{name}/about")["data"]
                print(
                    f"          live: submission={about.get('submission_type')} "
                    f"over18={about.get('over18')} "
                    f"subs={about.get('subscribers')} "
                    f"flair={about.get('link_flair_enabled')}"
                )
                for rule in _api(token, f"/r/{name}/about/rules")["rules"][:4]:
                    text = (rule.get("description") or "").replace("\n", " ")
                    print(f"            - {rule.get('short_name')}: {text[:88]}")
            except Exception as exc:  # pragma: no cover
                print(f"          live lookup failed: {exc}")
            time.sleep(0.8)

    usable = [name for name, reason in rows if reason is None]
    print()
    print(f"usable: {len(usable)} of {len(rows)}  ->  {', '.join(usable) or 'none'}")

    if args.recommend:
        print()
        print(f"subreddits that would accept {args.content} (largest first):")
        for profile in registry.recommendations(content_kind=args.content):
            print(
                f"  r/{profile.name:<24} {profile.subscribers:>10,} subs  "
                f"selfpromo={profile.self_promotion}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
