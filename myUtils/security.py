"""Auth + CORS policy for the Flask backend.

The legacy backend was wide open — `CORS(app)` allowed every origin and no
endpoint ever validated the `Authorization` header. This module replaces both
with an environment-driven policy so the same code can run safely in
production while staying out of the way in solo-developer mode.

Configuration
-------------

``SAU_API_TOKENS``
    Comma- or whitespace-separated list of accepted bearer tokens. When set,
    every endpoint outside ``PUBLIC_PATHS`` rejects requests that do not
    present one of these tokens. When unset, the backend logs a single
    ``[security] running in open mode`` warning at startup and lets requests
    through. Empty entries are ignored so ``"a, ,b"`` parses cleanly.

``SAU_CORS_ORIGINS``
    Comma- or whitespace-separated list of allowed origins for browser
    requests. Defaults to ``http://localhost:5173, http://localhost:4173``
    (the Vite dev and preview servers). The wildcard ``*`` is honoured but
    refuses to be combined with credentialed requests, matching the browser
    rule.

The policy intentionally exposes a small functional surface so unit tests can
exercise it without spinning up Flask.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Iterable

# Routes that the frontend SPA must reach unauthenticated. Anything else is
# protected. The login SSE endpoint is special-cased below because the
# browser ``EventSource`` API cannot attach custom headers — it accepts an
# auth token via query string instead.
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/",
    "/favicon.ico",
    "/vite.svg",
    "/oauth/tiktok/callback",
    "/oauth/tiktok/callback/",
    "/oauth/reddit/callback",
    "/oauth/reddit/callback/",
    "/oauth/youtube/callback",
    "/oauth/youtube/callback/",
    "/webhooks/tiktok",
    "/webhooks/tiktok/",
})

# Path prefixes that are public (e.g. static asset folders). Kept as a tuple
# so ``str.startswith`` accepts it directly.
PUBLIC_PREFIXES: tuple[str, ...] = (
    "/assets/",
    "/static/",
)

# The login endpoint is protected, but auth is read from a query-string
# parameter rather than the Authorization header because EventSource cannot
# send custom headers. The same secret is accepted via either mechanism.
SSE_AUTH_QUERY_PARAM = "auth"


@dataclass(frozen=True)
class SecurityPolicy:
    tokens: frozenset[str]
    cors_origins: tuple[str, ...]

    @property
    def open_mode(self) -> bool:
        return not self.tokens

    def is_public_path(self, path: str) -> bool:
        if path in PUBLIC_PATHS:
            return True
        return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)

    def token_is_valid(self, presented: str | None) -> bool:
        if not presented:
            return False
        # Constant-time comparison against every accepted token. The token
        # set is small (typically one), so iterating is fine.
        return any(
            secrets.compare_digest(presented, token) for token in self.tokens
        )


def _split_env(raw: str | None) -> list[str]:
    if not raw:
        return []
    pieces = []
    for chunk in raw.replace(",", " ").split():
        chunk = chunk.strip()
        if chunk:
            pieces.append(chunk)
    return pieces


def load_policy(env: dict[str, str] | None = None) -> SecurityPolicy:
    """Build a SecurityPolicy from environment variables."""

    env = env if env is not None else os.environ
    tokens = frozenset(_split_env(env.get("SAU_API_TOKENS")))
    cors_raw = env.get("SAU_CORS_ORIGINS")
    if cors_raw is None:
        cors_origins: tuple[str, ...] = (
            "http://localhost:5173",
            "http://localhost:4173",
        )
    else:
        cors_origins = tuple(_split_env(cors_raw)) or ("http://localhost:5173",)
    return SecurityPolicy(tokens=tokens, cors_origins=cors_origins)


# --------------------------- Cookie / storage_state validation ---------------------------


# Playwright's storage_state file is a JSON object with at least one of
# ``cookies`` and ``origins`` arrays. We require that shape so a malformed
# upload doesn't silently overwrite a working cookie file. We do NOT require
# both keys — Playwright will read whichever is present.
class CookieValidationError(ValueError):
    """Raised when an uploaded JSON file is not a valid Playwright storage_state."""


# Cap the size of validated input so a multi-megabyte JSON cannot exhaust
# memory in the validator. The Flask endpoint already enforces
# MAX_CONTENT_LENGTH=160MB at the HTTP layer; this is a tighter ceiling for
# something that should never exceed a few KB.
MAX_COOKIE_FILE_BYTES = 1 * 1024 * 1024


def validate_storage_state(raw: bytes | str) -> dict:
    """Return the parsed storage_state dict, or raise CookieValidationError.

    Validation rules (kept deliberately small so legitimate Playwright files
    from any platform pass):

    - Body parses as JSON.
    - Top-level value is an object.
    - At least one of ``cookies`` / ``origins`` keys is present.
    - When present, ``cookies`` is a list of objects with ``name`` and
      ``value`` string fields.
    - When present, ``origins`` is a list of objects with an ``origin``
      string field.
    """

    import json

    if isinstance(raw, bytes):
        if len(raw) > MAX_COOKIE_FILE_BYTES:
            raise CookieValidationError(
                f"Cookie file is too large: {len(raw)} bytes "
                f"(max {MAX_COOKIE_FILE_BYTES})"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CookieValidationError(f"Cookie file is not valid UTF-8: {exc}") from exc
    else:
        text = raw
        if len(text.encode("utf-8")) > MAX_COOKIE_FILE_BYTES:
            raise CookieValidationError(
                f"Cookie file is too large (max {MAX_COOKIE_FILE_BYTES} bytes)"
            )

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CookieValidationError(f"Cookie file is not valid JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise CookieValidationError("Cookie file must be a JSON object")

    cookies = parsed.get("cookies")
    origins = parsed.get("origins")
    if cookies is None and origins is None:
        raise CookieValidationError(
            "Cookie file must contain at least one of 'cookies' or 'origins'"
        )

    if cookies is not None:
        if not isinstance(cookies, list):
            raise CookieValidationError("'cookies' must be a list")
        for index, cookie in enumerate(cookies):
            if not isinstance(cookie, dict):
                raise CookieValidationError(
                    f"cookies[{index}] must be an object"
                )
            for key in ("name", "value"):
                if key not in cookie or not isinstance(cookie[key], str):
                    raise CookieValidationError(
                        f"cookies[{index}].{key} must be a string"
                    )

    if origins is not None:
        if not isinstance(origins, list):
            raise CookieValidationError("'origins' must be a list")
        for index, origin in enumerate(origins):
            if not isinstance(origin, dict):
                raise CookieValidationError(
                    f"origins[{index}] must be an object"
                )
            if "origin" not in origin or not isinstance(origin["origin"], str):
                raise CookieValidationError(
                    f"origins[{index}].origin must be a string"
                )

    return parsed


def extract_bearer_token(
    headers: dict | None,
    query_args: dict | None = None,
    *,
    is_sse: bool = False,
) -> str | None:
    """Return the bearer token from a request, or None if absent.

    Headers take precedence; the query-string fallback only runs for SSE
    paths so a stray ``?auth=…`` on a regular request does not bypass the
    header check.
    """

    if headers:
        # Flask headers behave like a case-insensitive dict.
        auth_header = headers.get("Authorization") or headers.get("authorization")
        if auth_header:
            scheme, _, value = auth_header.partition(" ")
            if scheme.lower() == "bearer" and value.strip():
                return value.strip()

    if is_sse and query_args:
        candidate = query_args.get(SSE_AUTH_QUERY_PARAM)
        if candidate:
            return candidate

    return None
