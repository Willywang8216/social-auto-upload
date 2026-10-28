"""Give rclone a config file it is actually allowed to rewrite.

rclone saves its config when it refreshes an OAuth token, and it writes the temp
file *next to* the config (``<config><pid>``), then renames it. The container
mounts a single file at ``/app/rclone-cache.conf`` inside the root-owned ``/app``,
so the file itself is writable but its *directory* is not. Every refresh
therefore failed::

    Failed to save config after 10 tries: failed to create temp file for new
    config: open /app/rclone-cache.conf409909120: permission denied

and the Drive download failed with it, taking the publish down. On 2026-10-29 the
oldest inbox item burned a retry that way ("Could not download inbox media from
Google Drive: ... permission denied").

The config content is fine and should stay read-only (it holds one live Drive
token); what it needs is a writable directory. Copy it once into ``$HOME`` (the
container sets ``HOME=/tmp``) and hand rclone that copy. The copy lives only as
long as the container, and the mounted file is refreshed daily by
``scripts/push_credentials_vault.py``, so nothing is lost.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ENV_VAR = "RCLONE_CONFIG"
_COPY_NAME = "rclone-writable.conf"


def writable_rclone_config(
    source: str | os.PathLike[str] | None = None,
    *,
    home: str | os.PathLike[str] | None = None,
) -> str | None:
    """Path to a writable copy of the rclone config, or ``None``.

    ``None`` means "leave rclone alone": either no config is configured, or the
    configured one already sits in a writable directory.
    """
    raw = str(source or os.environ.get(ENV_VAR) or "").strip()
    if not raw:
        return None
    original = Path(raw)
    if not original.is_file():
        return None
    if os.access(original.parent, os.W_OK):
        return None  # rclone can already write its temp file here
    home_dir = Path(home or os.environ.get("HOME") or "/tmp")
    target = home_dir / _COPY_NAME
    try:
        home_dir.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.stat().st_mtime < original.stat().st_mtime:
            shutil.copyfile(original, target)
            target.chmod(0o600)
    except OSError:
        return None
    return str(target)


def rclone_argv(*args: object, config: str | os.PathLike[str] | None = None) -> list[str]:
    """``["rclone", "--config", <writable>, ...]`` for a fresh invocation."""
    argv = ["rclone"]
    writable = writable_rclone_config(config)
    if writable:
        argv += ["--config", writable]
    argv += [str(arg) for arg in args]
    return argv


def with_writable_config(command: list[str], *, config: str | os.PathLike[str] | None = None) -> list[str]:
    """Rewrite an existing ``["rclone", ...]`` argv to use a writable config."""
    if not command or "rclone" not in Path(str(command[0])).name:
        return command
    if "--config" in command:
        return command
    writable = writable_rclone_config(config)
    if not writable:
        return command
    return [command[0], "--config", writable, *command[1:]]
