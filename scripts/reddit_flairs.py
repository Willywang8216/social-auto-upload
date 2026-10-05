#!/usr/bin/env python3
"""Discover a subreddit's post-flair ids and write them into an account's config.

Why this exists
---------------
Subreddits that require post flair (r/NudistMen among them) reject every submit
with ``SUBMIT_VALIDATION_FLAIR_REQUIRED`` unless ``flair_id`` is attached, and
those ids are **per-subreddit GUIDs** — they cannot be shared between
subreddits and they cannot be guessed.

The obvious endpoint, ``GET /r/<sub>/api/link_flair_v2``, needs the ``flair``
OAuth scope. The stored refresh tokens for this deployment do **not** carry it
(they have ``account edit submit modconfig read identity history``), so that
endpoint returns 403 for every subreddit. The ``read`` scope is enough to list
recent posts, however, and each post carries its ``link_flair_text`` together
with the ``link_flair_template_id`` that produced it. Reading the newest posts
therefore reveals the ids for every flair actually in use.

Usage
-----
    # inspect (writes nothing)
    python scripts/reddit_flairs.py --account 105

    # inspect every subreddit on the account, including ones with no recent use
    python scripts/reddit_flairs.py --account 105 --all

    # write the four content-category flairs into config.flairIds
    python scripts/reddit_flairs.py --account 105 --apply \
        --subreddit NudistMen --labels "At Home" "Selfie" "In Nature" "Food & Drink"

The ``--apply`` form only writes ids it actually observed for that subreddit, so
a typo in a label fails loudly instead of writing an id that does not exist.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myUtils import prepared_publishers as pp  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "database.db"


def _clean(text: str) -> str:
    return text.replace("&amp;", "&").replace("&quot;", '"').strip()


def _account_config(db_path: Path, account_id: int) -> dict:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT config_json FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    if row is None:
        raise SystemExit(f"no account with id={account_id} in {db_path}")
    return json.loads(row[0])


def discover(db_path: Path, account_id: int) -> dict[str, dict[str, str]]:
    """Return ``{subreddit: {flair_text: flair_id}}`` seen on recent posts."""
    config = _account_config(db_path, account_id)
    http = pp._get_session()
    token = pp._reddit_access_token(config, session=http)
    user_agent = str(
        pp._config_value(config, "userAgent") or "social-auto-upload/0.1 (sau)"
    ).strip()
    headers = {"Authorization": f"Bearer {token}", "User-Agent": user_agent}

    found: dict[str, dict[str, str]] = {}
    for subreddit in config.get("subreddits") or []:
        response = http.get(
            f"https://oauth.reddit.com/r/{subreddit}/new.json?limit=100",
            headers=headers,
            timeout=30,
        )
        if response.status_code != 200:
            print(f"  r/{subreddit}: HTTP {response.status_code} (skipped)")
            continue
        flairs: dict[str, str] = {}
        for child in response.json().get("data", {}).get("children", []):
            data = child.get("data", {})
            text = data.get("link_flair_text")
            template = data.get("link_flair_template_id")
            if text and template:
                flairs.setdefault(_clean(text), template)
        found[subreddit] = flairs
    return found


def apply_flairs(
    db_path: Path,
    account_id: int,
    subreddit: str,
    labels: list[str],
    flairs: dict[str, dict[str, str]],
) -> None:
    available = flairs.get(subreddit) or {}
    chosen: dict[str, str] = {}
    missing: list[str] = []
    for label in labels:
        if label in available:
            chosen[label] = available[label]
        else:
            missing.append(label)
    if missing:
        raise SystemExit(
            f"these labels were not observed on r/{subreddit}: {missing}\n"
            f"observed: {sorted(available)}\n"
            "Refusing to write — an unknown label would produce an invalid id."
        )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT config_json FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        config = json.loads(row[0])
        mapping = config.get("flairIds")
        if not isinstance(mapping, dict):
            mapping = {}
        mapping[subreddit] = chosen
        config["flairIds"] = mapping
        conn.execute(
            "UPDATE accounts SET config_json = ? WHERE id = ?",
            (json.dumps(config), account_id),
        )
        conn.commit()
    print(f"wrote config.flairIds[{subreddit!r}] for account {account_id}:")
    for label, flair_id in chosen.items():
        print(f"    {label:<14} {flair_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", type=int, required=True, help="SAU account id")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--subreddit", default=None, help="subreddit to write (with --apply)")
    parser.add_argument("--labels", nargs="*", default=[], help="flair labels to write (with --apply)")
    parser.add_argument("--apply", action="store_true", help="write the ids into account config")
    args = parser.parse_args()

    flairs = discover(args.db_path, args.account)
    for subreddit, mapping in flairs.items():
        print(f"r/{subreddit}: {len(mapping)} flair(s) observed")
        for label, flair_id in sorted(mapping.items()):
            print(f"    {label:<30} {flair_id}")

    if args.apply:
        if not args.subreddit or not args.labels:
            raise SystemExit("--apply needs --subreddit and --labels")
        apply_flairs(args.db_path, args.account, args.subreddit, args.labels, flairs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
