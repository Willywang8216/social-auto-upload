"""Tests for myUtils.cookie_storage."""

from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import cookie_storage


def _make_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


class KeyResolutionTests(unittest.TestCase):
    def test_unset_means_disabled(self) -> None:
        self.assertFalse(cookie_storage.is_encryption_enabled(env={}))
        self.assertIsNone(cookie_storage.get_active_key(env={}))

    def test_empty_string_raises(self) -> None:
        with self.assertRaises(cookie_storage.CookieEncryptionError):
            cookie_storage.get_active_key(env={"SAU_COOKIE_ENCRYPTION_KEY": "  "})

    def test_invalid_length_rejected(self) -> None:
        # 8 bytes — not a valid AES key size.
        bad = base64.urlsafe_b64encode(b"\x01" * 8).decode()
        with self.assertRaises(cookie_storage.CookieEncryptionError):
            cookie_storage.get_active_key(env={"SAU_COOKIE_ENCRYPTION_KEY": bad})

    def test_urlsafe_and_standard_b64_both_accepted(self) -> None:
        raw = secrets.token_bytes(32)
        for encoded in (
            base64.urlsafe_b64encode(raw).decode(),
            base64.b64encode(raw).decode(),
        ):
            with self.subTest(encoded=encoded):
                key = cookie_storage.get_active_key(
                    env={"SAU_COOKIE_ENCRYPTION_KEY": encoded}
                )
                self.assertEqual(key, raw)


class RoundtripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = _make_key()
        self._patch = patch.dict(
            os.environ, {"SAU_COOKIE_ENCRYPTION_KEY": self.key}
        )
        self._patch.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "cookie.json"

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_write_then_read_returns_plaintext(self) -> None:
        payload = json.dumps({"cookies": [{"name": "a", "value": "b"}]}).encode()
        cookie_storage.write_cookie(self.path, payload)

        on_disk = self.path.read_bytes()
        self.assertTrue(cookie_storage.looks_encrypted(on_disk))
        self.assertNotIn(b'"name"', on_disk)
        self.assertEqual(cookie_storage.read_cookie(self.path), payload)

    def test_read_plaintext_is_returned_verbatim(self) -> None:
        payload = b'{"cookies":[]}'
        self.path.write_bytes(payload)
        # Even with a key configured, files without the magic header are
        # treated as legacy plaintext for the rollout migration.
        self.assertEqual(cookie_storage.read_cookie(self.path), payload)

    def test_tampered_ciphertext_fails_with_clear_error(self) -> None:
        payload = b'{"cookies":[]}'
        cookie_storage.write_cookie(self.path, payload)

        blob = self.path.read_bytes()
        tampered = bytearray(blob)
        # Flip a byte in the ciphertext region (after magic + nonce).
        tampered[-1] ^= 0xFF
        self.path.write_bytes(bytes(tampered))

        with self.assertRaises(cookie_storage.CookieEncryptionError):
            cookie_storage.read_cookie(self.path)

    def test_aad_binds_to_filename(self) -> None:
        payload = b'{"cookies":[]}'
        cookie_storage.write_cookie(self.path, payload)

        # Move the encrypted blob to a different filename and try to read
        # — the AAD is the basename so this must fail.
        moved = self.path.with_name("other.json")
        self.path.rename(moved)
        with self.assertRaises(cookie_storage.CookieEncryptionError):
            cookie_storage.read_cookie(moved)

    def test_decrypt_without_key_raises(self) -> None:
        payload = b'{"cookies":[]}'
        cookie_storage.write_cookie(self.path, payload)
        # Drop the key from the env.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAU_COOKIE_ENCRYPTION_KEY", None)
            with self.assertRaises(cookie_storage.CookieEncryptionError):
                cookie_storage.read_cookie(self.path)


class DecryptedStorageStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "cookie.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_plaintext_mode_yields_canonical_path(self) -> None:
        # No env key set, so the helper must yield the path unchanged.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAU_COOKIE_ENCRYPTION_KEY", None)
            with cookie_storage.decrypted_storage_state(self.path) as p:
                self.assertEqual(p, self.path)

    def test_encrypted_mode_yields_tempfile_and_writes_back(self) -> None:
        key = _make_key()
        with patch.dict(os.environ, {"SAU_COOKIE_ENCRYPTION_KEY": key}):
            payload = b'{"cookies":[{"name":"a","value":"b"}]}'
            cookie_storage.write_cookie(self.path, payload)

            with cookie_storage.decrypted_storage_state(self.path) as plain_path:
                self.assertNotEqual(plain_path, self.path)
                self.assertEqual(plain_path.read_bytes(), payload)
                # Simulate Playwright writing back updated state.
                plain_path.write_bytes(b'{"cookies":[{"name":"a","value":"updated"}]}')

            # On exit the canonical file is re-encrypted with the new content.
            on_disk = self.path.read_bytes()
            self.assertTrue(cookie_storage.looks_encrypted(on_disk))
            self.assertEqual(
                cookie_storage.read_cookie(self.path),
                b'{"cookies":[{"name":"a","value":"updated"}]}',
            )

    def test_encrypted_mode_handles_missing_canonical_file(self) -> None:
        # Brand-new account: no cookie file yet. The context manager must
        # still produce a tempfile so the login flow can write into it.
        key = _make_key()
        with patch.dict(os.environ, {"SAU_COOKIE_ENCRYPTION_KEY": key}):
            with cookie_storage.decrypted_storage_state(self.path) as plain_path:
                self.assertNotEqual(plain_path, self.path)
                self.assertTrue(plain_path.exists())
                plain_path.write_bytes(b'{"cookies":[]}')
            self.assertTrue(self.path.exists())
            self.assertEqual(cookie_storage.read_cookie(self.path), b'{"cookies":[]}')


class BulkMigrationTests(unittest.TestCase):
    def test_encrypt_existing_files_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = _make_key()
            with patch.dict(os.environ, {"SAU_COOKIE_ENCRYPTION_KEY": key}):
                a = Path(tmp) / "a.json"
                b = Path(tmp) / "b.json"
                missing = Path(tmp) / "missing.json"
                a.write_bytes(b'{"cookies":[]}')
                b.write_bytes(b'{"cookies":[]}')

                first = cookie_storage.encrypt_existing_files([a, b, missing])
                self.assertEqual(first[str(a)], "encrypted")
                self.assertEqual(first[str(b)], "encrypted")
                self.assertEqual(first[str(missing)], "skipped_missing")

                second = cookie_storage.encrypt_existing_files([a, b])
                self.assertEqual(second[str(a)], "already_encrypted")
                self.assertEqual(second[str(b)], "already_encrypted")

    def test_skipped_when_no_key_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SAU_COOKIE_ENCRYPTION_KEY", None)
                a = Path(tmp) / "a.json"
                a.write_bytes(b'{"cookies":[]}')
                outcome = cookie_storage.encrypt_existing_files([a])
                self.assertEqual(outcome[str(a)], "skipped_no_key")
                # And the file is untouched on disk.
                self.assertEqual(a.read_bytes(), b'{"cookies":[]}')


if __name__ == "__main__":
    unittest.main()
