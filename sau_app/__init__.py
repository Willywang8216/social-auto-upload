"""Application-factory layer for social-auto-upload (Phase 1).

The historical entrypoint ``sau_backend.py`` builds a module-global Flask
``app`` and registers 137 routes against it. Rather than rewrite that monolith
in one step, Phase 1 introduces a thin, well-tested factory around it:

- ``init_extensions(app)`` installs cross-cutting concerns (request/correlation
  IDs, structured logging, a standard JSON error schema) and registers the
  health blueprint. It is **idempotent** — safe to call from both the monolith
  (at import) and from ``create_app()``.
- ``create_app()`` returns the fully wired application for a production WSGI
  server (Gunicorn via ``wsgi:app``), importing the existing monolith lazily so
  there is no import cycle.

Behavior of the existing 137 routes is unchanged; the factory only *adds*
capabilities.
"""

from __future__ import annotations

from flask import Flask

from .config import AppConfig, ConfigError, load_config
from .extensions import init_extensions, register_readiness_check

__all__ = [
    "AppConfig",
    "ConfigError",
    "create_app",
    "init_extensions",
    "load_config",
    "register_readiness_check",
]


def create_app() -> Flask:
    """Return the production application.

    Imports the legacy monolith lazily (it creates the module-global ``app``
    and calls ``init_extensions`` itself at import time). Calling
    ``init_extensions`` again here is a no-op thanks to the idempotency guard,
    so this function is safe whether or not the monolith wired itself up.
    """

    import sau_backend  # noqa: PLC0415 — lazy to avoid an import cycle

    app = sau_backend.app
    init_extensions(app)
    return app
