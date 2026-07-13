"""Opt-in at-rest encryption for the secret values inside account ``config``.

Account ``config_json`` stores live OAuth material (access/refresh tokens, app
secrets, bot tokens). Phase 7 stopped echoing those in API responses; this adds
the at-rest half: when ``SAU_CONFIG_ENCRYPTION_KEY`` is set, the secret *values*
(identified by :func:`myUtils.secret_redaction.is_secret_key`) are encrypted
with the same AES-GCM envelope used for cookie files
(:mod:`myUtils.cookie_storage`) before they hit the database, and decrypted
transparently on read.

Design:

* **Opt-in and independent.** With the key unset every function is a no-op, so
  the stored ``config_json`` is byte-identical to before. Enabling cookie
  encryption does *not* implicitly enable this (separate env var).
* **Per-value, not per-blob.** Only secret values are encrypted; non-secret
  fields (``channelTitle``, ``accessTokenExpiresAt``, …) stay readable, which
  keeps the JSON debuggable and lets the existing row-mapper / redaction layer
  work unchanged.
* **Field-bound.** The AES-GCM AAD is the secret's key name, so a ciphertext
  cannot be moved from ``refreshToken`` to ``accessToken``.
* **Idempotent.** Already-encrypted values (``sau:enc:v1:`` prefix) are left
  alone on write, so re-saving an account never double-encrypts.

The single choke point is ``myUtils.profiles`` (encrypt in ``add_account`` /
``update_account``; decrypt in ``_row_to_account``); every account read in the
app goes through the ``Account`` dataclass, so consumers always see plaintext.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from myUtils import cookie_storage
from myUtils.secret_redaction import REDACTION_SENTINEL, is_secret_key

CONFIG_ENCRYPTION_KEY_ENV = "SAU_CONFIG_ENCRYPTION_KEY"
ENC_PREFIX = "sau:enc:v1:"


def get_active_key(env: dict[str, str] | None = None) -> bytes | None:
    """Return the raw AES key bytes, or None when config encryption is disabled.

    Resolved from the environment on every call (cheap; lets tests monkeypatch
    ``os.environ`` without a module reload).
    """
    env = env if env is not None else os.environ
    encoded = env.get(CONFIG_ENCRYPTION_KEY_ENV)
    if not encoded:
        return None
    return _decode_key(encoded)


def is_encryption_enabled(env: dict[str, str] | None = None) -> bool:
    return get_active_key(env=env) is not None


def _decode_key(encoded: str) -> bytes:
    encoded = encoded.strip()
    if not encoded:
        raise ValueError(f"{CONFIG_ENCRYPTION_KEY_ENV} is empty; unset it or provide a base64 key")
    last_error: Exception | None = None
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = decoder(encoded + "=" * (-len(encoded) % 4))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        if len(raw) in (16, 24, 32):
            return raw
        last_error = ValueError(f"key must decode to 16/24/32 bytes, got {len(raw)}")
    raise ValueError(
        f"{CONFIG_ENCRYPTION_KEY_ENV} must be base64-encoded 16/24/32 bytes (got: {last_error})"
    )


def _encrypt_value(value: str, *, field: str, key: bytes) -> str:
    blob = cookie_storage.encrypt_bytes(value.encode("utf-8"), path=Path(field), key=key)
    return ENC_PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")


def _decrypt_value(value: str, *, field: str, key: bytes) -> str:
    raw = base64.urlsafe_b64decode(value[len(ENC_PREFIX):].encode("ascii"))
    return cookie_storage.decrypt_bytes(raw, path=Path(field), key=key).decode("utf-8")


def _should_encrypt(field: str, value: Any) -> bool:
    return (
        is_secret_key(field)
        and isinstance(value, str)
        and value not in ("", REDACTION_SENTINEL)
        and not value.startswith(ENC_PREFIX)
    )


def encrypt_config_secrets(config: Any, *, key: bytes | None = None) -> Any:
    """Deep-copy ``config`` with secret string values encrypted at rest.

    A no-op (returns ``config`` unchanged) when no key is configured.
    """
    if key is None:
        key = get_active_key()
    if key is None:
        return config
    return _walk_encrypt(config, key)


def decrypt_config_secrets(config: Any, *, key: bytes | None = None) -> Any:
    """Deep-copy ``config`` with ``sau:enc:v1:`` values decrypted.

    A no-op when no key is configured; encrypted values are then left as-is
    (the operator removed the key — an error state, not a silent leak).
    """
    if key is None:
        key = get_active_key()
    if key is None:
        return config
    return _walk_decrypt(config, key)


def _walk_encrypt(obj: Any, key: bytes, *, field: str | None = None) -> Any:
    if isinstance(obj, dict):
        return {k: _walk_encrypt(v, key, field=k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_encrypt(item, key, field=field) for item in obj]
    if field is not None and _should_encrypt(field, obj):
        return _encrypt_value(obj, field=field, key=key)
    return obj


def _walk_decrypt(obj: Any, key: bytes, *, field: str | None = None) -> Any:
    if isinstance(obj, dict):
        return {k: _walk_decrypt(v, key, field=k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_decrypt(item, key, field=field) for item in obj]
    if (
        field is not None
        and is_secret_key(field)
        and isinstance(obj, str)
        and obj.startswith(ENC_PREFIX)
    ):
        return _decrypt_value(obj, field=field, key=key)
    return obj
