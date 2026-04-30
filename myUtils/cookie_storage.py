"""At-rest encryption for cookie / Playwright storage_state files.

Design
------

A cookie file holds a logged-in session for one of the social platforms we
drive — losing one is equivalent to losing the password. Until this module
existed, every cookie was written to disk in plaintext under
``cookiesFile/`` and ``cookies/{platform}/{profile}/``, which made any
host-level breach a credential-disclosure event.

Threat model
~~~~~~~~~~~~

We protect against an attacker who has read access to the workspace
filesystem but not to the live Python process / environment. They cannot
read the bearer-protected backend (Phase 3) and cannot recover the cookies
from disk because the encryption key lives in ``SAU_COOKIE_ENCRYPTION_KEY``,
sourced from the deployment's secret store. We do **not** defend against an
attacker who can read the running process's memory or who controls the env
the worker runs in — those are out of scope for at-rest encryption.

File format
~~~~~~~~~~~

Encrypted files start with the 5-byte magic ``b"SAU1\\n"`` followed by a
12-byte AES-GCM nonce and the ciphertext (which has the 16-byte GCM tag
appended by ``AESGCM.encrypt``). Files without the magic header are treated
as legacy plaintext. This makes the on-disk migration painless: existing
deployments keep working unchanged until a key is configured, and once a key
is configured each file is converted on first write-back.

Key source
~~~~~~~~~~

``SAU_COOKIE_ENCRYPTION_KEY`` — base64 (urlsafe or standard) encoded 16/24/32
byte key. 32-byte (AES-256) is recommended. When the env var is unset, the
module operates in plaintext mode: ``write_cookie`` writes the bytes
verbatim, ``read_cookie`` returns the bytes verbatim, and the decrypted
tempfile context manager simply yields the canonical path without making a
copy. This preserves the legacy behaviour bit-for-bit for any deployment
that hasn't opted in yet.

The decrypted-tempfile path
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Every uploader passes a filesystem path to Playwright's
``new_context(storage_state=…)``. Playwright reads the file directly, so we
can't hand it ciphertext. The :func:`decrypted_storage_state` async context
manager does the right thing for both modes:

- Encryption disabled → yield the canonical path; do nothing on exit.
- Encryption enabled  → write decrypted plaintext to a unique tempfile with
  ``0o600`` perms, yield that path. On clean exit, re-encrypt whatever
  Playwright wrote back into the tempfile and atomically rename it into
  the canonical location. On error, just shred the tempfile.

This means existing uploader code continues to do
``await context.storage_state(path=str(path))`` and gets encryption for
free.
"""

from __future__ import annotations

import base64
import contextlib
import os
import secrets
import tempfile
from pathlib import Path
from typing import Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"SAU1\n"
NONCE_SIZE = 12  # AES-GCM standard
ENCRYPTION_KEY_ENV = "SAU_COOKIE_ENCRYPTION_KEY"


class CookieEncryptionError(Exception):
    """Raised for malformed ciphertext, bad tags, or missing/invalid keys."""


# --------------------------- key resolution ---------------------------


def _decode_key(encoded: str) -> bytes:
    """Accept a base64 (urlsafe or standard) string and return the raw key bytes."""

    encoded = encoded.strip()
    if not encoded:
        raise CookieEncryptionError(
            f"{ENCRYPTION_KEY_ENV} is empty; provide a base64-encoded key or unset it"
        )

    last_error: Exception | None = None
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = decoder(encoded + "=" * (-len(encoded) % 4))
        except Exception as exc:  # noqa: BLE001 — we want every decoder error
            last_error = exc
            continue
        if len(raw) in (16, 24, 32):
            return raw
        last_error = ValueError(
            f"key must decode to 16/24/32 bytes, got {len(raw)}"
        )
    raise CookieEncryptionError(
        f"{ENCRYPTION_KEY_ENV} must be base64-encoded 16/24/32 bytes "
        f"(got error: {last_error})"
    )


def get_active_key(env: dict[str, str] | None = None) -> bytes | None:
    """Return the raw AES key bytes, or None when encryption is disabled.

    Resolved from the environment on every call so tests can monkeypatch
    ``os.environ`` without forcing a module reload. The function is cheap
    enough that the per-call overhead is negligible.
    """

    env = env if env is not None else os.environ
    encoded = env.get(ENCRYPTION_KEY_ENV)
    if not encoded:
        return None
    return _decode_key(encoded)


def is_encryption_enabled(env: dict[str, str] | None = None) -> bool:
    return get_active_key(env=env) is not None


# --------------------------- low-level encrypt/decrypt ---------------------------


def _aad_for(path: Path) -> bytes:
    """Use the basename as additional authenticated data.

    Including the filename in the AEAD context means an attacker can't swap
    one encrypted cookie file for another of the same shape and have it
    decrypt successfully. The basename only — not the full path — so a
    cookie that's moved between dev and prod machines still validates.
    """

    return path.name.encode("utf-8")


def encrypt_bytes(plaintext: bytes, *, path: Path, key: bytes | None = None) -> bytes:
    """Encrypt ``plaintext`` for the file at ``path``.

    Returns the on-disk envelope: ``MAGIC || nonce || ciphertext+tag``.
    Raises ``CookieEncryptionError`` when no key is configured.
    """

    key = key if key is not None else get_active_key()
    if key is None:
        raise CookieEncryptionError(
            "encryption requested but no key is configured"
        )
    nonce = secrets.token_bytes(NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, _aad_for(path))
    return MAGIC + nonce + ciphertext


def decrypt_bytes(blob: bytes, *, path: Path, key: bytes | None = None) -> bytes:
    """Inverse of :func:`encrypt_bytes`.

    Raises ``CookieEncryptionError`` if the magic header is wrong, the blob
    is truncated, the GCM tag fails, or no key is configured. Pass an
    explicit ``key`` to test rotation scenarios.
    """

    if not blob.startswith(MAGIC):
        raise CookieEncryptionError(
            f"missing magic header on {path}; not an encrypted SAU file"
        )
    if len(blob) < len(MAGIC) + NONCE_SIZE + 16:  # 16 = AES-GCM tag size
        raise CookieEncryptionError(f"ciphertext truncated for {path}")

    key = key if key is not None else get_active_key()
    if key is None:
        raise CookieEncryptionError(
            f"cannot decrypt {path}: no key configured; "
            f"set {ENCRYPTION_KEY_ENV}"
        )

    body = blob[len(MAGIC):]
    nonce, ciphertext = body[:NONCE_SIZE], body[NONCE_SIZE:]
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ciphertext, _aad_for(path))
    except InvalidTag as exc:
        raise CookieEncryptionError(
            f"authentication failed for {path}; key mismatch or tampering"
        ) from exc


def looks_encrypted(blob: bytes) -> bool:
    """Cheap header check so callers can branch without raising."""

    return blob.startswith(MAGIC)


# --------------------------- file-level read/write ---------------------------


def write_cookie(path: Path, payload: bytes) -> None:
    """Write a cookie file, encrypting if a key is configured.

    Uses an atomic temp-file + rename so a crash mid-write cannot leave a
    half-written cookie on disk for the uploader to choke on.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if is_encryption_enabled():
        on_disk = encrypt_bytes(payload, path=path)
    else:
        on_disk = payload

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(on_disk)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        # Filesystems that don't support POSIX perms (e.g. Windows FAT)
        # silently swallow the chmod; the rename below still succeeds.
        pass
    tmp_path.replace(path)


def read_cookie(path: Path) -> bytes:
    """Return the plaintext cookie bytes, decrypting if necessary.

    A file without the SAU1 magic is returned verbatim, which is what makes
    the rollout migration painless: every legacy plaintext cookie keeps
    working until a write-back encrypts it.
    """

    blob = path.read_bytes()
    if looks_encrypted(blob):
        return decrypt_bytes(blob, path=path)
    return blob


@contextlib.contextmanager
def decrypted_storage_state(path: Path) -> Iterator[Path]:
    """Yield a path containing plaintext storage_state, re-encrypt on exit.

    Usage::

        with decrypted_storage_state(account_file) as plain_path:
            await context.new_context(storage_state=str(plain_path))
            ...
            await context.storage_state(path=str(plain_path))

    On clean exit, ``plain_path``'s contents are re-encrypted (if a key is
    configured) and atomically renamed back to the canonical ``path``.
    On exception, the tempfile is shredded; ``path`` is left untouched.
    """

    if not is_encryption_enabled():
        # Plaintext mode: skip the tempfile and let callers read/write the
        # canonical path directly. This keeps the legacy code path's I/O
        # behaviour identical for deployments that haven't opted in yet.
        yield path
        return

    # Pre-decrypt if the file already exists so the uploader sees the
    # current session. Brand-new accounts may not yet have a file.
    plain_payload = b""
    if path.exists():
        plain_payload = read_cookie(path)

    fd, tmp_name = tempfile.mkstemp(prefix="sau-cookie-", suffix=".json")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(plain_payload)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass

        yield tmp_path

        # The uploader may or may not have written back. If the tempfile is
        # still there and non-empty, encrypt it back to the canonical path.
        if tmp_path.exists():
            updated = tmp_path.read_bytes()
            if updated:
                write_cookie(path, updated)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


# --------------------------- bulk migration helper ---------------------------


def encrypt_existing_files(paths: list[Path]) -> dict[str, str]:
    """Encrypt any plaintext cookies in ``paths`` in place.

    Skipped silently when encryption is disabled. Idempotent: files that
    already start with the SAU1 magic are left alone. Returns a small
    summary dict ``{path: outcome}`` where outcome is one of ``encrypted``,
    ``already_encrypted``, ``skipped_missing``, or ``error: <reason>``.
    """

    if not is_encryption_enabled():
        return {str(path): "skipped_no_key" for path in paths}

    outcome: dict[str, str] = {}
    for path in paths:
        key = str(path)
        if not path.exists():
            outcome[key] = "skipped_missing"
            continue
        try:
            blob = path.read_bytes()
            if looks_encrypted(blob):
                outcome[key] = "already_encrypted"
                continue
            write_cookie(path, blob)
            outcome[key] = "encrypted"
        except Exception as exc:  # noqa: BLE001
            outcome[key] = f"error: {exc}"
    return outcome
