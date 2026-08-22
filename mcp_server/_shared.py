"""Shared helpers for MCP tools.

Two things every tool needs:

* a DB path resolver — test-friendly so callers can point at a temp DB
  without monkey-patching ``myUtils.profiles.DB_PATH``;
* an exception→error-envelope translator so MCP clients see structured
  ``{"error": "...", "message": "..."}`` payloads instead of raw stack traces.

Payload formatters live next to the tools that use them (e.g. ``account_payload``)
to keep this module dependency-free.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from myUtils import profiles as profile_registry


_DEFAULT_DB_ENV = "SAU_MCP_DB_PATH"


def resolve_db_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Pick a DB path for this tool call.

    Resolution order: explicit argument > ``SAU_MCP_DB_PATH`` env > the
    project default (``myUtils.profiles.DB_PATH``). Tests pass an explicit
    path; production callers leave it unset and let the project default win.
    """
    if explicit is not None and str(explicit) != "":
        return Path(explicit)
    env = os.environ.get(_DEFAULT_DB_ENV)
    if env:
        return Path(env)
    return Path(profile_registry.DB_PATH)


def error_payload(exc: BaseException) -> dict[str, Any]:
    """Translate a raised exception into a structured error envelope.

    MCP clients receive this as the tool result so the agent can branch on
    ``error`` rather than parsing free-form strings.
    """
    if isinstance(exc, LookupError):
        return {"error": "not_found", "message": str(exc) or "not found"}
    if isinstance(exc, ValueError):
        return {"error": "invalid_input", "message": str(exc) or "invalid input"}
    if isinstance(exc, PermissionError):
        return {"error": "forbidden", "message": str(exc) or "forbidden"}
    if isinstance(exc, NotImplementedError):
        return {"error": "unsupported", "message": str(exc) or "not supported"}
    return {
        "error": "internal",
        "message": str(exc) or exc.__class__.__name__,
        "type": exc.__class__.__name__,
    }


def to_dict(maybe_dataclass: Any) -> dict[str, Any]:
    """Return a dict for dataclasses, dicts, or already-dict-like objects."""
    if maybe_dataclass is None:
        return {}
    if isinstance(maybe_dataclass, dict):
        return maybe_dataclass
    to_dict_attr = getattr(maybe_dataclass, "to_dict", None)
    if callable(to_dict_attr):
        return to_dict_attr()
    if hasattr(maybe_dataclass, "__dict__"):
        return dict(maybe_dataclass.__dict__)
    return {"value": maybe_dataclass}
