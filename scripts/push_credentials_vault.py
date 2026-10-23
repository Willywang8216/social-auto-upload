#!/usr/bin/env python3
"""Refresh the rclone credential vault.

The vault lives at ``Onedrive-Yahooforsub-Tao:credentials/``. It is the
human/agent-readable backup of every credential the deployment needs, separate
from the nightly DB tarball (which is opaque). Historically it was written by
hand, so it went stale and drifted out of sync with the database — that is why an
account could be connected and still be missing from the vault.

This script regenerates it from the live database and the live cookie files, then
uploads with rclone. It is idempotent and safe to run from cron.

Written to the vault:
  * ``sau-x-twitter-credentials.json``      — every X account's OAuth tokens
  * ``sau-publish-platforms-credentials.json`` — the env secrets + every other
    platform's account config
  * ``x-<username>.storage_state.json``     — browser sessions (Playwright
    storage_state) for the cookie-backed X accounts
  * ``_manifest.json``                      — what was written and when

Usage:
    python3 scripts/push_credentials_vault.py [--dry-run] [--remote REMOTE]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULT_REMOTE = "Onedrive-Yahooforsub-Tao:credentials"
DEMO_ENV_KEYS = (
    "SAU_NW_SW_BLOG_GITHUB_TOKEN",
    "SAU_TELEGRAM_STRING_SESSION",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "SOCIAMONIALS_API_KEY",
    "SOCIAMONIALS_TEACHING_API_KEY",
    "BUNDLE_SOCIAL_API_KEY",
    "BUNDLE_SOCIAL_TEAM_ID",
)
# Config keys that are tokens/secrets rather than descriptive metadata. Copied
# verbatim so the vault can restore an account, but never logged.
SECRET_HINTS = (
    "token", "secret", "password", "cookie", "apikey", "api_key", "credential",
    "session", "clientid", "clientsecret", "refresh",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        values[key] = raw.strip().strip('"').strip("'")
    return values


def _accounts() -> list:
    from myUtils import profiles as profile_registry

    return list(profile_registry.list_accounts())


def build_x_vault(accounts: list, db_path: Path) -> dict:
    """Every X account's OAuth credential set (mirrors the old vault format)."""
    out: dict[str, dict] = {}
    for account in accounts:
        if str(account.platform) != "twitter":
            continue
        config = dict(account.config or {})
        out[str(account.id)] = {
            "account_name": account.account_name,
            "twitterUserName": config.get("twitterUserName"),
            "twitterAuthType": config.get("twitterAuthType"),
            "audience_language": config.get("audience_language"),
            "scope": config.get("scope"),
            "accessToken": config.get("accessToken"),
            "refreshToken": config.get("refreshToken"),
            "tokenType": config.get("tokenType"),
            "accessTokenExpiresAt": config.get("accessTokenExpiresAt"),
            "accessTokenUpdatedAt": config.get("accessTokenUpdatedAt"),
            "twitterUserId": config.get("twitterUserId"),
            "twitterDisplayName": config.get("twitterDisplayName"),
            "connectedAt": config.get("connectedAt"),
            "auth_type": account.auth_type,
        }
    return {
        "_note": (
            "social-auto-upload X/Twitter account credentials. OAuth2 user tokens "
            "per SAU account id; refresh_token is the durable part (auto-refreshed "
            "by the app's maintenance loop). Store in the vault only - never in "
            "git. Companion of sau-publish-platforms-credentials.json."
        ),
        "service": "https://x.com",
        "api": "https://api.x.com/2",
        "sync": ":credentials/",
        "written_at": _now_iso(),
        "db": str(db_path),
        "accounts": out,
    }


def build_platforms_vault(accounts: list, env: dict[str, str], db_path: Path) -> dict:
    """Env secrets + every non-X account, so the vault restores the whole app."""
    secrets = {key: env.get(key) for key in DEMO_ENV_KEYS if env.get(key)}
    per_platform: dict[str, list[dict]] = {}
    for account in accounts:
        platform = str(account.platform)
        if platform == "twitter":
            continue
        per_platform.setdefault(platform, []).append(
            {
                "id": account.id,
                "account_name": account.account_name,
                "auth_type": account.auth_type,
                "cookie_path": str(account.cookie_path) if account.cookie_path else None,
                "config": dict(account.config or {}),
            }
        )
    return {
        "_note": (
            "SAU non-X publish-platform credentials: the .env secrets plus every "
            "account row. X/Twitter lives in sau-x-twitter-credentials.json. Vault "
            "only - never commit."
        ),
        "sync": ":credentials/",
        "written_at": _now_iso(),
        "db": str(db_path),
        "env_secrets": secrets,
        "accounts": per_platform,
    }


def _alias(name: str) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def collect_cookie_files(workdir: Path, accounts: list) -> list[dict]:
    """Copy every live storage_state file into the vault workspace."""
    written: list[dict] = []
    # A cookie file is named after either the account row or the X handle, and
    # those differ (account "sexualwill" is @will_sexual; account "NW X
    # (model_will)" is @model_will), so index both spellings.
    by_alias: dict[str, object] = {}
    for account in accounts:
        by_alias.setdefault(_alias(account.account_name), account)
        handle = dict(account.config or {}).get("twitterUserName")
        if handle:
            by_alias.setdefault(_alias(handle), account)

    roots = [REPO / "cookies", REPO / "cookiesFile"]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            if path.stat().st_size == 0:
                continue
            stem = path.stem
            account = by_alias.get(_alias(stem))
            is_x = "twitter" in str(path).lower() or (
                account is not None and str(account.platform) == "twitter"
            )
            if is_x:
                handle = None
                if account is not None:
                    handle = dict(account.config or {}).get("twitterUserName")
                name = f"x-{handle or stem}.storage_state.json"
            elif account is not None:
                name = f"{account.platform}-{stem}.storage_state.json"
            else:
                rel = path.relative_to(root)
                name = "cookie-" + str(rel).replace("/", "__")
            try:
                json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:  # noqa: BLE001 - a non-JSON cookie is still worth keeping
                name = name.replace(".json", ".txt")
            destination = workdir / name
            shutil.copyfile(path, destination)
            written.append(
                {
                    "file": name,
                    "source": str(path.relative_to(REPO)),
                    "bytes": destination.stat().st_size,
                }
            )
    return written


def _rclone(remote: str, workdir: Path, *, dry_run: bool) -> int:
    command = [
        "rclone", "copy", str(workdir), remote,
        "--include", "*.json", "--include", "*.txt",
    ]
    if dry_run:
        print("would run:", " ".join(command))
        return 0
    return subprocess.call(command)


def _read_remote_manifest(remote: str) -> dict:
    """Best-effort read of the manifest already in the vault.

    Used to carry forward the per-file provenance that was established by hand.
    Any failure returns {}, so this can never stop the vault being refreshed.
    """
    try:
        out = subprocess.run(
            ["rclone", "cat", f"{remote}/_manifest.json"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return {}
        data = json.loads(out.stdout)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default=os.environ.get("SAU_VAULT_REMOTE", DEFAULT_REMOTE))
    parser.add_argument("--db-path", default=str(REPO / "db" / "database.db"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    env = _load_env(REPO / ".env")
    accounts = _accounts()

    workdir = Path(tempfile.mkdtemp(prefix="sau-vault-"))
    try:
        x_vault = build_x_vault(accounts, db_path)
        platforms_vault = build_platforms_vault(accounts, env, db_path)
        (workdir / "sau-x-twitter-credentials.json").write_text(
            json.dumps(x_vault, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        (workdir / "sau-publish-platforms-credentials.json").write_text(
            json.dumps(platforms_vault, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        cookie_files = collect_cookie_files(workdir, accounts)
        manifest = {
            "written_at": _now_iso(),
            "remote": args.remote,
            "x_accounts": len(x_vault["accounts"]),
            "platform_accounts": sum(len(v) for v in platforms_vault["accounts"].values()),
            "cookie_files": cookie_files,
        }

        # Carry forward the inventory this manifest used to lose.
        #
        # Until now the manifest was rebuilt from scratch every night, so the
        # per-file record of where each credential came from (which staging
        # directory, and which files could not be traced) survived less than a
        # day. Keep what is already in the vault, and make sure every file this
        # run generated is represented.
        existing = _read_remote_manifest(args.remote)
        cookie_names = {c.get("file") for c in cookie_files}
        carried = [e for e in (existing.get("credential_files") or [])
                   if isinstance(e, dict) and e.get("file")
                   and e["file"] not in cookie_names]
        listed = {e["file"] for e in carried}
        # The two files this run regenerates are credentials in their own right,
        # so record them even when the vault had no inventory to carry forward.
        for name in ("sau-x-twitter-credentials.json",
                     "sau-publish-platforms-credentials.json"):
            if name not in listed and name not in cookie_names:
                carried.append({"file": name,
                                "source": "scripts/push_credentials_vault.py"})
                listed.add(name)
        if carried:
            manifest["credential_files"] = carried
            manifest["credential_file_count"] = len(carried)
        if existing.get("retired"):
            manifest["retired"] = existing["retired"]

        (workdir / "_manifest.json").write_text(
            json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8"
        )

        print(f"x accounts: {manifest['x_accounts']}")
        for account_id, row in x_vault["accounts"].items():
            print(f"  {account_id} {row['account_name']} @{row['twitterUserName']} "
                  f"type={row['twitterAuthType']} auth={row['auth_type']}")
        print(f"platform account rows: {manifest['platform_accounts']}")
        for name, rows in platforms_vault["accounts"].items():
            print(f"  {name}: {len(rows)}")
        print(f"cookie files: {len(cookie_files)}")
        for item in cookie_files:
            print(f"  {item['file']}  <- {item['source']}")

        rc = _rclone(args.remote, workdir, dry_run=args.dry_run)
        if rc != 0:
            print(f"rclone failed with rc={rc}", file=sys.stderr)
            return rc
        print("uploaded to", args.remote)
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
