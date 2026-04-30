"""Canonical defaults for the project's runtime configuration.

This module is the **source of truth** for every setting that used to live
inside ``conf.py``. ``conf.py`` is a user-editable file — production
deployments often bind-mount a stripped-down version into the container —
which makes ``from conf import …`` brittle: every consumer site assumes the
attribute is defined, and a missing one raises ``ImportError`` at import
time, taking the whole process down.

The fix is to give the project a fallback that:

1. Defines every expected attribute exactly once, here.
2. Lets ``conf.py`` opt in by ``from conf_defaults import *`` (the new
   ``conf.example.py`` does this).
3. Falls back transparently when ``conf.py`` is missing an attribute, via
   :func:`apply_conf_defaults` which is invoked once at process start by
   the entry points (``sau_backend.py``, ``sau_cli.py``,
   ``myUtils/worker.py``) before any other module imports ``conf``.

Every value here can also be overridden through an environment variable so
operators can run the whole project without touching any ``.py`` file at
all — handy for container deployments.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Repository root. Resolved relative to this file so the project keeps
# working regardless of the caller's CWD.
BASE_DIR: Path = Path(
    os.environ.get("SAU_BASE_DIR", Path(__file__).parent.resolve())
).resolve()

# XHS local sign server (used only by the legacy direct-API XHS path).
XHS_SERVER: str = os.environ.get("XHS_SERVER", "http://127.0.0.1:11901")

# Optional path to a locally-installed Chrome. When set, Playwright /
# patchright launches use this binary instead of the bundled one. Empty
# string means "let the driver pick the default channel".
LOCAL_CHROME_PATH: str = os.environ.get("LOCAL_CHROME_PATH", "")

# Default headless behaviour for the whole project.
LOCAL_CHROME_HEADLESS: bool = _env_bool("LOCAL_CHROME_HEADLESS", True)

# Verbose logging across the uploaders.
DEBUG_MODE: bool = _env_bool("DEBUG_MODE", True)


_EXPECTED_ATTRS = (
    "BASE_DIR",
    "XHS_SERVER",
    "LOCAL_CHROME_PATH",
    "LOCAL_CHROME_HEADLESS",
    "DEBUG_MODE",
)


def apply_conf_defaults(target: ModuleType | None = None) -> None:
    """Backfill ``target`` (default: the live ``conf`` module) with defaults.

    Idempotent. Existing attributes on ``target`` are preserved, so a user's
    explicit override always wins. Run this once near the top of every
    entry point so a partial ``conf.py`` (e.g. a stripped-down
    container-mounted version that defines only ``LOCAL_CHROME_PATH``)
    still satisfies ``from conf import BASE_DIR`` everywhere downstream.

    If ``conf.py`` is missing entirely **or** raises during import, we
    synthesise an empty ``conf`` module with the defaults attached so the
    rest of the codebase still works. The original import error is
    surfaced as a warning so the operator notices.
    """

    if target is None:
        target = sys.modules.get("conf")
        if target is None:
            try:
                import conf as _conf  # noqa: WPS433 — intentional lazy import
                target = _conf
            except Exception as exc:  # noqa: BLE001
                # Either conf.py is missing, has a syntax error, or its
                # body raised. Don't take the whole process down — log
                # and synthesise a fresh module so callers can still
                # ``from conf import BASE_DIR``.
                import logging

                logging.getLogger(__name__).warning(
                    "Failed to import conf.py (%s); using defaults from "
                    "conf_defaults.py. Set the SAU_* env vars or fix "
                    "conf.py to silence this warning.",
                    exc,
                )
                target = ModuleType("conf")
                sys.modules["conf"] = target

    defaults = sys.modules[__name__]
    for attr in _EXPECTED_ATTRS:
        if not hasattr(target, attr):
            setattr(target, attr, getattr(defaults, attr))
