#!/usr/bin/env python3
"""Fixed, non-root cron jobs. No scheduler, Docker socket or arbitrary command.

--check reads mount/config availability only; it never invokes an application,
contacts a provider, writes production state or logs credential values.
"""
from __future__ import annotations

import argparse
import json
import configparser
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

JOBS = {
    "watcher": ("python", "/app/sau_inbox/watch.py", "--once"),
    "inbox-monitor": ("python", "/app/sau_inbox/inbox_monitor.py"),
    "pipeline-monitor": ("python", "-m", "myUtils.pipeline_monitor", "--watch-stale-min", "30", "--inbox", "/app/sau-inbox"),
    "inbox-archive": ("python", "-m", "myUtils.inbox_archive", "--from-drive", "--include-scheduled", "--limit", "60"),
    "inbox-autopublish": ("python", "-m", "myUtils.inbox_autopublish"),
    "vault-credentials": ("python", "/app/scripts/push_credentials_vault.py"),
    "vault-llm-stt": ("python", "/app/scripts/push_llm_stt_vault.py"),
}
INBOX_JOBS = {"watcher", "inbox-monitor", "pipeline-monitor", "inbox-archive", "inbox-autopublish"}
DRIVE_JOBS = {"watcher", "inbox-archive"}
VAULT_JOBS = {"vault-credentials", "vault-llm-stt"}


def read_values(path: Path) -> dict[str, str]:
    """Use the native jobs' literal KEY=VALUE convention, not shell expansion."""
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        values[key] = value.strip().strip('"').strip("'")
    return values


def internal_url(value: str, default: str, host: str, port: int) -> str:
    """Translate only the known host loopback service; keep custom routes intact."""
    parsed = urlsplit(value or default)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port == port:
        if parsed.username or parsed.password:
            raise ValueError("loopback operations URLs must not embed credentials")
        parsed = parsed._replace(netloc=f"{host}:{port}")
    return urlunsplit(parsed)


def environment(job: str, root: Path, inherited: dict[str, str]) -> dict[str, str]:
    if job not in JOBS:
        raise ValueError("unsupported operations job")
    env = dict(inherited)
    # The original autopublisher prefers inbox notification credentials, then
    # application credentials. Watcher/watchdog never receive application env.
    files = []
    if job in {"watcher", "inbox-monitor", "inbox-autopublish"}:
        files.append(root / "sau-inbox" / ".env")
    if job not in {"watcher", "inbox-monitor"}:
        files.append(root / ".env")
    for path in files:
        for key, value in read_values(path).items():
            env.setdefault(key, value)
    env.update({
        "HOME": "/tmp/sau-ops", "XDG_CACHE_HOME": "/tmp/sau-ops/.cache",
        "PYTHONDONTWRITEBYTECODE": "1", "SAU_PROCESS_ROLE": "web",
        "SAU_DB_PATH": str(root / "db" / "database.db"),
        "SAU_PAUSE_CLAIMS_FILE": str(root / "db" / "publish.pause"),
        "SAU_INBOX": str(root / "sau-inbox"),
        "SAU_WATCH_HOME": str(root / "sau-inbox"),
        "SAU_WATCH_STATE": str(root / "sau-inbox" / "state.json"),
        "SAU_WATCH_HEALTH": str(root / "sau-inbox" / ".watch-health.json"),
    })
    if job in DRIVE_JOBS | VAULT_JOBS:
        env["RCLONE_CONFIG"] = "/run/rclone/rclone.conf"
        env["SAU_INBOX_RCLONE_CONFIG"] = env["RCLONE_CONFIG"]
    if job == "watcher":
        env["SAU_INBOX_RCLONE_MODE"] = "container"
    if job in VAULT_JOBS:
        env["SAU_VAULT_ENV_FILE"] = str(root / ".env")
    if job == "pipeline-monitor":
        env["SAU_HEALTH_URL"] = internal_url(env.get("SAU_HEALTH_URL", ""), "http://127.0.0.1:5409/readyz", "social-auto-upload", 5409)
        if env.get("SAU_STT_HOST_BASE_URL"):
            env["SAU_STT_HOST_BASE_URL"] = internal_url(env["SAU_STT_HOST_BASE_URL"], "", "sau-stt", 9099)
    if job == "inbox-autopublish":
        env["SAU_INBOX_PUBLISH_URL"] = internal_url(env.get("SAU_INBOX_PUBLISH_URL", ""), "http://127.0.0.1:5409/api/inbox/items/{item_id}/publish", "social-auto-upload", 5409)
    return env


def check_mounts(job: str, root: Path, env: dict[str, str]) -> None:
    paths = []
    if job in INBOX_JOBS:
        paths.append(root / "sau-inbox" / "state.json")
    if job in {"pipeline-monitor", "inbox-archive", "vault-credentials"}:
        paths.append(root / "db" / "database.db")
    if job in DRIVE_JOBS | VAULT_JOBS:
        paths.append(Path(env["RCLONE_CONFIG"]))
    if any(not path.is_file() or not os.access(path, os.R_OK) for path in paths):
        raise RuntimeError("a required operations mount is absent or unreadable")
    if job in DRIVE_JOBS | VAULT_JOBS:
        config = configparser.RawConfigParser()
        config.read(env["RCLONE_CONFIG"], encoding="utf-8")
        if job in DRIVE_JOBS:
            expected = env.get("SAU_INBOX_DRIVE_REMOTE", "GDrive-willywang8216")
        else:
            remote = env.get("SAU_VAULT_REMOTE", "Onedrive-Yahooforsub-Tao:credentials") if job == "vault-credentials" else "Onedrive-Yahooforsub-Tao:credentials"
            expected = remote.split(":", 1)[0]
        if config.sections() != [expected]:
            raise RuntimeError("operations rclone config must contain only its required remote")
    if Path("/var/run/docker.sock").exists():
        raise RuntimeError("operations containers must not receive the Docker socket")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=tuple(JOBS))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        raise RuntimeError("operations jobs require a non-root uid")
    root = Path("/app")
    env = environment(args.job, root, dict(os.environ))
    check_mounts(args.job, root, env)
    if args.check:
        print(json.dumps({"job": args.job, "check": "mounts-readable", "uid": os.geteuid(), "command": JOBS[args.job]}))
        return 0
    if env.get("SAU_TESTING") == "1":
        raise RuntimeError("real operations jobs must not execute under tests")
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    os.execvpe(JOBS[args.job][0], JOBS[args.job], env)
    return 1  # exec never returns


if __name__ == "__main__":
    raise SystemExit(main())
