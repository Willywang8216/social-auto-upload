"""Application factory for social-auto-upload.

The historical entrypoint ``sau_backend.py`` builds a module-global Flask
``app`` and registers ~158 routes against it. Rewriting that monolith in one
step is not a sensible trade, so this factory **replays** the monolith's route
table and its two request hooks onto a NEW ``Flask`` instance.

That is enough for real independence because every handler reads
``current_app.config`` rather than the module global: two applications can
coexist in one process, each with its own configuration, without seeing each
other's.

``wsgi.py`` keeps serving the monolith's global app, so production behaviour is
unchanged; this is an additive capability, not a replacement.
"""
from __future__ import annotations

from typing import Mapping

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

#: Config keys describing the *global* app's initialisation, which this
#: instance must resolve for itself rather than inherit.
_RESET_KEYS = ("SAU_EXTENSIONS_INITIALIZED", "SAU_APP_CONFIG")


def replay_routes(source: Flask, app: Flask) -> int:
    """Copy ``source``'s URL rules onto ``app``; return the number copied.

    Blueprint-qualified endpoints are skipped: the monolith registers its
    routes directly on the app, while every blueprint (``sau_health`` and the
    auth blueprint) is registered freshly by ``init_extensions``. Replaying
    those too would double-register them.
    """
    copied = 0
    for rule in source.url_map.iter_rules():
        if rule.endpoint == "static" or "." in rule.endpoint:
            continue
        view = source.view_functions.get(rule.endpoint)
        if view is None:
            continue
        app.add_url_rule(
            rule.rule,
            endpoint=rule.endpoint,
            view_func=view,
            methods=sorted(rule.methods - {"HEAD", "OPTIONS"}),
            defaults=rule.defaults,
            strict_slashes=rule.strict_slashes,
        )
        copied += 1
    return copied


def create_app(environ: Mapping[str, str] | None = None) -> Flask:
    """Return a fully wired, independent application instance.

    ``environ`` overrides the environment used to resolve configuration, which
    is what lets a caller build two differently-configured instances in one
    process (and what the independence test relies on).
    """
    import sau_backend as legacy

    source = legacy.app
    app = Flask(
        legacy.__name__,
        static_folder=source.static_folder,
        static_url_path=source.static_url_path,
        template_folder=source.template_folder,
    )
    app.config.update(source.config)
    app.secret_key = source.secret_key
    for key in _RESET_KEYS:
        app.config.pop(key, None)

    # Extensions first: they install the request-id, rate-limit, tenancy and
    # authorization hooks. The monolith's own auth gate is registered AFTER, so
    # ordering matches production exactly — a flood is throttled before it
    # earns a cheap 401, and a permission failure is a 403 rather than a 401.
    init_extensions(app, environ=environ)

    # The bearer policy is derived from the environment, so it is resolved for
    # THIS instance instead of inherited from the global app.
    from myUtils.security import load_policy

    app.config["SECURITY_POLICY"] = load_policy(
        None if environ is None else dict(environ)
    )

    replay_routes(source, app)
    app.before_request(legacy._enforce_auth)
    app.after_request(legacy._redact_credentials_in_error_bodies)
    return app
