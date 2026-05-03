"""Minimal repo-local .env loader.

This project already uses environment variables heavily, but many local runs keep
those values in a repo-root `.env` file rather than exporting them in the shell.
We intentionally avoid a new dependency such as `python-dotenv` here: the format
we need is small and stable.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"
_LOADED_PATHS: set[Path] = set()


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].strip()
    if "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = _strip_quotes(value.strip())
    return key, value


def load_repo_env(*, env_path: str | Path | None = None, override: bool = False) -> Path | None:
    target = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    target = target.expanduser().resolve()
    if not target.exists():
        return None
    if target in _LOADED_PATHS and not override:
        return target

    for raw_line in target.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value

    _LOADED_PATHS.add(target)
    return target
