#!/usr/bin/env python3
"""Record the LLM + STT endpoints in the rclone vault.

The vault already carried the *platform* credentials, but not the inference
infrastructure this deployment now depends on: the LLM rotation pool (including
the 12 AgentRouter accounts serving ``gpt-6.1-sol``) and the local STT sidecar's
token. Losing ``.env`` would have meant re-deriving all of it by hand, which is
exactly the drift the vault exists to prevent.

Writes ``sau-llm-stt-endpoints.json`` (readable, no secrets in the *names*) and
uploads it. Safe to re-run.

    python scripts/push_llm_stt_vault.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULT_REMOTE = "Onedrive-Yahooforsub-Tao:credentials"
RCLONE_CONFIG = "/home/will/.config/rclone/rclone.conf"
ENV = REPO / ".env"


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"')
    return out


def _mask(value: str) -> str:
    text = str(value or "")
    return f"{text[:8]}…{text[-4:]}" if len(text) > 16 else ("<set>" if text else "")


def build() -> dict:
    env = _env()
    pool: list[dict] = []
    try:
        pool = json.loads(env.get("SAU_LLM_POOL") or "[]")
    except ValueError:
        pool = []
    endpoints = []
    for entry in pool:
        endpoints.append({
            "base_url": entry.get("base_url"),
            "model": entry.get("model"),
            "protocol": entry.get("protocol") or "openai",
            "api_key": entry.get("api_key"),
        })
    agentrouter = [e for e in endpoints if "agentrouter" in str(e.get("base_url"))]
    return {
        "_comment": (
            "LLM rotation pool + local STT for social-auto-upload. The pool is set "
            "as SAU_LLM_POOL (JSON array) in ~/social-auto-upload/.env; the client "
            "rotates in order and stops at the first success. AgentRouter entries "
            "MUST carry protocol=anthropic: that gateway rejects "
            "/v1/chat/completions outright and only speaks /v1/messages."
        ),
        "written_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "llm": {
            "env_var": "SAU_LLM_POOL",
            "pool_size": len(endpoints),
            "primary_model": env.get("SAU_LLM_MODEL"),
            "legacy_base_url": env.get("SAU_LLM_API_BASE_URL"),
            "legacy_api_key": env.get("SAU_LLM_API_KEY"),
            "min_interval_seconds": env.get("SAU_LLM_MIN_INTERVAL_SECONDS"),
            "endpoints": endpoints,
        },
        "agentrouter": {
            "_comment": (
                "12 of the 13 vault accounts answer gpt-6.1-sol via /v1/messages. "
                "The token is the ANTHROPIC_AUTH_TOKEN from the operator's "
                "cc-providers.json in this same vault."
            ),
            "base_url": "https://agentrouter.org",
            "protocol": "anthropic",
            "models": ["gpt-6.1-sol", "gpt-6.1-sol-fast"],
            "working_accounts": len(agentrouter),
            "api_keys": [e["api_key"] for e in agentrouter],
        },
        "stt": {
            "_comment": (
                "Local faster-whisper sidecar (docker/stt/Dockerfile, service 'stt', "
                "container 'sau-stt'). The app reaches it as http://sau-stt:9099 on "
                "the 1panel-network; host-side tooling uses the published loopback "
                "port. It detects the spoken language AND transcribes. The hosted "
                "gateway is kept only as a fallback."
            ),
            "local": {
                "base_url_in_container": env.get("SAU_STT_BASE_URL"),
                "base_url_on_host": env.get("SAU_STT_HOST_BASE_URL"),
                "model": env.get("SAU_STT_MODEL"),
                "api_key": env.get("SAU_STT_API_KEY"),
                "whisper_model": env.get("SAU_STT_LOCAL_MODEL") or "tiny",
                "token_file_on_host": "/home/will/.config/sau-stt.env",
                "rebuild": "cd ~/social-auto-upload && docker compose up -d stt",
            },
            "fallback": {
                "base_url": env.get("SAU_STT_FALLBACK_BASE_URL"),
                "model": env.get("SAU_STT_FALLBACK_MODEL"),
                "api_key": env.get("SAU_STT_FALLBACK_API_KEY"),
                "note": "was the primary until it returned 503 model_not_found on 2026-10-27",
            },
        },
        "images": {
            "_comment": "image-generation endpoints, not part of the text pool",
            "freeapi.dgbmc.top": "grok-imagine-image",
            "wzw.pp.ua": "grok-imagine-image-2.0",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remote", default=DEFAULT_REMOTE)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    payload = build()
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    print(f"pool: {payload['llm']['pool_size']} endpoints "
          f"({payload['agentrouter']['working_accounts']} AgentRouter)")
    print(f"STT: {payload['stt']['local']['base_url_in_container']} "
          f"({payload['stt']['local']['model']})")
    if args.dry_run:
        print("(dry run) would upload sau-llm-stt-endpoints.json:")
        print(text[:600])
        return 0

    with tempfile.TemporaryDirectory(prefix="sau-llm-vault-") as tmp:
        path = Path(tmp) / "sau-llm-stt-endpoints.json"
        path.write_text(text, encoding="utf-8")
        result = subprocess.run(
            ["rclone", "--config", RCLONE_CONFIG, "copy", str(path), args.remote,
             "--include", "*.json"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("upload failed:", (result.stderr or result.stdout)[-400:], file=sys.stderr)
            return result.returncode
    print("uploaded sau-llm-stt-endpoints.json to", args.remote)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
