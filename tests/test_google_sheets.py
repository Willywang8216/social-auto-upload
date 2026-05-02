"""Tests for Google Sheets export helpers."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from myUtils import google_sheets

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    serialization = None
    rsa = None


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url == google_sheets.GOOGLE_TOKEN_URL:
            return _FakeResponse({"access_token": "token-123", "expires_in": 3600})
        if url == google_sheets.GOOGLE_SHEETS_API_ROOT:
            return _FakeResponse({"spreadsheetId": "sheet-123"})
        return _FakeResponse({})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _FakeResponse({"sheets": [{"properties": {"title": "2026-05-02-brand"}}]})

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return _FakeResponse({})


@unittest.skipUnless(serialization is not None and rsa is not None, "cryptography is not installed")
class GoogleSheetsTests(unittest.TestCase):
    def _service_account_info(self) -> dict:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        return {
            "type": "service_account",
            "client_email": "robot@example.iam.gserviceaccount.com",
            "private_key": pem,
        }

    def test_build_service_account_jwt_returns_three_segments(self) -> None:
        token = google_sheets.build_service_account_jwt(
            self._service_account_info(),
            now=1_700_000_000,
        )
        self.assertEqual(len(token.split(".")), 3)

    def test_export_rows_creates_spreadsheet_and_appends_values(self) -> None:
        session = _RecordingSession()
        info = self._service_account_info()
        client = google_sheets.GoogleSheetsClient(info, session=session)
        result = client.export_rows(
            sheet_title="2026-05-02-brand",
            rows=[{"Message": "Hello", "Link": "https://example.com"}],
        )

        self.assertEqual(result.spreadsheet_id, "sheet-123")
        self.assertEqual(result.row_count, 1)
        methods = [method for method, _, _ in session.calls]
        self.assertEqual(methods[:4], ["POST", "POST", "GET", "PUT"])
        self.assertEqual(methods[-1], "POST")

    def test_load_service_account_from_env_json(self) -> None:
        info = self._service_account_info()
        with patch.dict(
            os.environ,
            {"SAU_GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(info)},
            clear=False,
        ):
            loaded = google_sheets.load_service_account_info()
        self.assertEqual(loaded["client_email"], info["client_email"])


if __name__ == "__main__":
    unittest.main()
