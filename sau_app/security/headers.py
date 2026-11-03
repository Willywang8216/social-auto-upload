"""Response security headers (Phase 1 hardening).

Adds the baseline hardening headers every browser-facing service should send,
and a Content-Security-Policy on HTML responses only.

Design notes
------------
* ``X-Content-Type-Options: nosniff`` is applied to **every** response. It is
  what stops a browser from re-interpreting a served user file (an upload
  named ``.jpg`` that is really HTML) as active content.
* The CSP is applied only to ``text/html`` responses, because a JSON API gains
  nothing from it and a wrong policy there is pure noise.
* The policy is deliberately pragmatic: this SPA is a Vite build using Element
  Plus, and the OAuth callback pages are small self-contained HTML documents
  with an inline ``<script>``. Both need ``'unsafe-inline'`` for styles and
  scripts, so the policy focuses on what still helps — ``frame-ancestors``,
  ``object-src``, ``base-uri`` and ``form-action`` — rather than shipping a
  policy that breaks the product.
* Everything is overridable from the environment so an operator can tighten it
  without a code change.
"""
from __future__ import annotations

import os

from flask import Flask, request

#: Headers applied to every response.
BASELINE_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
}

#: Default policy for HTML documents. Override with ``SAU_CSP``.
DEFAULT_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' blob: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' https:; "
    "worker-src 'self' blob:"
)

_TRUTHY = {"1", "true", "yes", "on"}


def _enabled(environ: dict | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return str(env.get("SAU_SECURITY_HEADERS", "1")).strip().lower() in _TRUTHY


def csp_policy(environ: dict | None = None) -> str:
    env = environ if environ is not None else os.environ
    configured = str(env.get("SAU_CSP", "") or "").strip()
    return configured or DEFAULT_CSP


def install_security_headers(app: Flask, *, environ: dict | None = None) -> None:
    """Attach baseline headers (and an HTML CSP) to every response."""
    if not _enabled(environ):
        return
    policy = csp_policy(environ)

    @app.after_request
    def _apply_security_headers(response):  # noqa: ANN001
        for name, value in BASELINE_HEADERS.items():
            response.headers.setdefault(name, value)
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if content_type.startswith("text/html") and "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = policy
        return response


def wants_html() -> bool:
    """True when the current request is a top-level HTML navigation."""
    return "text/html" in str(request.headers.get("Accept") or "")
