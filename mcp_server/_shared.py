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


def _testing() -> bool:
    """Tests legitimately point tools at a throwaway database."""
    return str(os.environ.get("SAU_TESTING", "")).strip().lower() in {"1", "true", "yes"}


def _allowed_db_roots() -> tuple[Path, ...]:
    """Directories an explicit ``db_path`` may resolve inside.

    Defaults to the directory holding the project's own database. An operator
    who genuinely needs another location can widen this with
    ``SAU_MCP_ALLOWED_DB_ROOTS`` (``os.pathsep``-separated) instead of the
    server accepting any path a caller names.
    """
    roots = [Path(profile_registry.DB_PATH).parent]
    for raw in (os.environ.get("SAU_MCP_ALLOWED_DB_ROOTS") or "").split(os.pathsep):
        candidate = raw.strip()
        if candidate:
            roots.append(Path(candidate))
    return tuple(root.resolve() for root in roots)


def resolve_db_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Pick a DB path for this tool call.

    Resolution order: explicit argument > ``SAU_MCP_DB_PATH`` env > the project
    default (``myUtils.profiles.DB_PATH``).

    An explicit/env path is a **capability**, not a convenience: without a
    guard, an MCP caller could aim every tool at an arbitrary SQLite file on
    the host. Outside the test suite a requested path must therefore resolve
    inside :func:`_allowed_db_roots`, or the call is refused.
    """
    requested: Path | None = None
    if explicit is not None and str(explicit) != "":
        requested = Path(explicit)
    else:
        env = os.environ.get(_DEFAULT_DB_ENV)
        if env:
            requested = Path(env)
    if requested is None:
        return Path(profile_registry.DB_PATH)
    if _testing():
        return requested
    resolved = requested.resolve()
    if not any(resolved.is_relative_to(root) for root in _allowed_db_roots()):
        raise PermissionError(
            "db_path must resolve inside the configured database directory; "
            "set SAU_MCP_ALLOWED_DB_ROOTS to widen the allowed roots"
        )
    return resolved


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
