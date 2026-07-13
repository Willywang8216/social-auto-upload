"""Unit tests for myUtils.config_crypto (opt-in at-rest config encryption)."""

from __future__ import annotations

import base64
import importlib.util
import os
import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import config_crypto
from myUtils.config_crypto import (
    CONFIG_ENCRYPTION_KEY_ENV,
    ENC_PREFIX,
    decrypt_config_secrets,
    encrypt_config_secrets,
)

sqlalchemy_available = importlib.util.find_spec("sqlalchemy") is not None


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


class DisabledByDefaultTests(unittest.TestCase):
    def test_no_key_is_noop(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(CONFIG_ENCRYPTION_KEY_ENV, None)
            cfg = {"accessToken": "abc", "channelTitle": "x"}
            self.assertEqual(encrypt_config_secrets(cfg), cfg)
            self.assertEqual(decrypt_config_secrets(cfg), cfg)


class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict("os.environ", {CONFIG_ENCRYPTION_KEY_ENV: _key()})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    def test_secret_values_encrypted_and_recovered(self) -> None:
        cfg = {
            "accessToken": "real-access",
            "refreshToken": "real-refresh",
            "channelTitle": "My Channel",
            "accessTokenExpiresAt": "2026-01-01",
        }
        enc = encrypt_config_secrets(cfg)
        # Secrets are masked with the marker prefix; metadata untouched.
        self.assertTrue(enc["accessToken"].startswith(ENC_PREFIX))
        self.assertTrue(enc["refreshToken"].startswith(ENC_PREFIX))
        self.assertEqual(enc["channelTitle"], "My Channel")
        self.assertEqual(enc["accessTokenExpiresAt"], "2026-01-01")
        # Ciphertext differs per field even for identical plaintext.
        self.assertNotEqual(enc["accessToken"], enc["refreshToken"])
        # Round-trips back to plaintext.
        self.assertEqual(decrypt_config_secrets(enc), cfg)

    def test_empty_and_sentinel_not_encrypted(self) -> None:
        cfg = {"accessToken": "", "refreshToken": "__redacted__"}
        enc = encrypt_config_secrets(cfg)
        self.assertEqual(enc["accessToken"], "")
        self.assertEqual(enc["refreshToken"], "__redacted__")

    def test_encrypt_is_idempotent(self) -> None:
        cfg = {"accessToken": "real"}
        once = encrypt_config_secrets(cfg)
        twice = encrypt_config_secrets(once)
        self.assertEqual(once["accessToken"], twice["accessToken"])

    def test_nested_secrets(self) -> None:
        cfg = {"pages": [{"pageId": "1", "pageAccessToken": "tok"}]}
        enc = encrypt_config_secrets(cfg)
        self.assertEqual(enc["pages"][0]["pageId"], "1")
        self.assertTrue(enc["pages"][0]["pageAccessToken"].startswith(ENC_PREFIX))
        self.assertEqual(decrypt_config_secrets(enc), cfg)

    def test_field_binding_rejects_swapped_ciphertext(self) -> None:
        enc = encrypt_config_secrets({"accessToken": "a", "refreshToken": "b"})
        swapped = {"accessToken": enc["refreshToken"], "refreshToken": enc["accessToken"]}
        # AAD is the field name, so decrypting a moved ciphertext fails.
        from myUtils.cookie_storage import CookieEncryptionError
        with self.assertRaises(CookieEncryptionError):
            decrypt_config_secrets(swapped)


class WrongKeyTests(unittest.TestCase):
    def test_decrypt_with_wrong_key_raises(self) -> None:
        k1, k2 = _key(), _key()
        with patch.dict("os.environ", {CONFIG_ENCRYPTION_KEY_ENV: k1}):
            enc = encrypt_config_secrets({"accessToken": "real"})
        from myUtils.cookie_storage import CookieEncryptionError
        with patch.dict("os.environ", {CONFIG_ENCRYPTION_KEY_ENV: k2}):
            with self.assertRaises(CookieEncryptionError):
                decrypt_config_secrets(enc)


@unittest.skipUnless(sqlalchemy_available, "sqlalchemy not installed")
class ProfilesIntegrationTests(unittest.TestCase):
    """End-to-end through myUtils.profiles: encrypt on write, decrypt on read."""

    def setUp(self) -> None:
        import db.createTable as create_table
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "t.db"
        create_table.bootstrap(self.db_path)

    def tearDown(self) -> None:
        os.environ.pop(CONFIG_ENCRYPTION_KEY_ENV, None)
        self._tmp.cleanup()

    def _raw_config_json(self, account_id: int) -> str:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT config_json FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return row[0]

    def test_encrypted_at_rest_plaintext_via_store(self) -> None:
        from myUtils import profiles
        with patch.dict("os.environ", {CONFIG_ENCRYPTION_KEY_ENV: _key()}):
            p = profiles.create_profile(name="brand", db_path=self.db_path)
            acct = profiles.add_account(
                p.id, "youtube", "yt",
                config={"accessToken": "secret-tok", "channelTitle": "Chan"},
                db_path=self.db_path,
            )
            raw = self._raw_config_json(acct.id)
            self.assertIn(ENC_PREFIX, raw)
            self.assertNotIn("secret-tok", raw)  # plaintext never touches disk
            got = profiles.get_account(acct.id, db_path=self.db_path)
            self.assertEqual(got.config["accessToken"], "secret-tok")
            self.assertEqual(got.config["channelTitle"], "Chan")

    def test_plaintext_at_rest_when_no_key(self) -> None:
        os.environ.pop(CONFIG_ENCRYPTION_KEY_ENV, None)
        from myUtils import profiles
        p = profiles.create_profile(name="brand", db_path=self.db_path)
        acct = profiles.add_account(
            p.id, "youtube", "yt", config={"accessToken": "secret-tok"}, db_path=self.db_path
        )
        raw = self._raw_config_json(acct.id)
        self.assertIn("secret-tok", raw)  # byte-identical to pre-encryption behavior
        self.assertNotIn(ENC_PREFIX, raw)

    def test_partial_update_preserves_encrypted_secret(self) -> None:
        from myUtils import profiles
        with patch.dict("os.environ", {CONFIG_ENCRYPTION_KEY_ENV: _key()}):
            p = profiles.create_profile(name="brand", db_path=self.db_path)
            acct = profiles.add_account(
                p.id, "youtube", "yt", config={"accessToken": "tok1"}, db_path=self.db_path
            )
            # config not passed -> update_account reuses the decrypted current
            # config and re-encrypts; the stored secret must survive intact.
            profiles.update_account(acct.id, account_name="renamed", db_path=self.db_path)
            got = profiles.get_account(acct.id, db_path=self.db_path)
            self.assertEqual(got.config["accessToken"], "tok1")
            self.assertIn(ENC_PREFIX, self._raw_config_json(acct.id))


if __name__ == "__main__":
    unittest.main()
