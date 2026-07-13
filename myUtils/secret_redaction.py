"""Redact secret values from account/config payloads returned over the API.

Account ``config`` blobs store live OAuth material (access/refresh tokens, app
secrets, bot tokens, cookies). Those must never be echoed back in an API
response — a leaked token is a full account takeover, and workspace isolation
alone does not stop a token from being logged by an intermediary or read by a
compromised session.

The two halves work together:

* :func:`redact_config_secrets` replaces every *non-empty* secret value with
  :data:`REDACTION_SENTINEL` on the way **out**. Empty values stay empty so the
  UI can still tell "configured" from "not configured", and the sentinel is
  non-empty so presence checks (``if config.accessToken``) keep working.
* :func:`strip_redaction_sentinels` removes any key whose value is the sentinel
  on the way **in**. Because the account-update path merges the incoming config
  over the stored one, dropping a round-tripped sentinel leaves the real secret
  untouched — the frontend can safely resubmit a redacted config.

A key is treated as a secret purely by the shape of its name (see
:func:`is_secret_key`); this fails safe for platforms/keys added later without a
central registry to update.
"""

from __future__ import annotations

from typing import Any

REDACTION_SENTINEL = "__redacted__"

# Secret *values* live under keys ending in one of these (case-insensitive).
# Metadata keys deliberately end differently — ``...ExpiresAt`` / ``...UpdatedAt``
# (``at``), ``tokenType`` (``type``), ``clientSecretEnv`` / ``accessTokenEnv``
# (``env``) — so they are never caught here. Bare ``key`` is excluded so
# semi-public identifiers (``consumerKey``) are not redacted.
_SECRET_KEY_SUFFIXES = (
    "token",
    "secret",
    "password",
    "apikey",
    "secretkey",
    "accesskey",
    "privatekey",
)

# Exact (lowercased) secret keys that do not match a suffix above.
_SECRET_KEY_EXACT = frozenset({"cookie", "cookies", "credentials"})


def is_secret_key(key: Any) -> bool:
    """True when ``key`` names a secret value that must not be returned."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered in _SECRET_KEY_EXACT:
        return True
    return lowered.endswith(_SECRET_KEY_SUFFIXES)


def redact_config_secrets(obj: Any) -> Any:
    """Return a deep copy of ``obj`` with every non-empty secret value masked."""
    if isinstance(obj, dict):
        out: dict = {}
        for key, value in obj.items():
            if is_secret_key(key):
                out[key] = (
                    REDACTION_SENTINEL
                    if value not in (None, "", REDACTION_SENTINEL)
                    else value
                )
            else:
                out[key] = redact_config_secrets(value)
        return out
    if isinstance(obj, list):
        return [redact_config_secrets(item) for item in obj]
    return obj


def strip_redaction_sentinels(obj: Any) -> Any:
    """Return a deep copy of ``obj`` with sentinel-valued keys removed.

    Applied to client-supplied config before it is persisted so a resubmitted
    redacted value never overwrites the stored secret (the caller merges the
    result over the existing config, which keeps the real value).
    """
    if isinstance(obj, dict):
        return {
            key: strip_redaction_sentinels(value)
            for key, value in obj.items()
            if value != REDACTION_SENTINEL
        }
    if isinstance(obj, list):
        return [strip_redaction_sentinels(item) for item in obj]
    return obj
