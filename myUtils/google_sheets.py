"""Google Sheets export using a service account and direct HTTP calls."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None

from myUtils.content_rules import SHEET_COLUMN_ORDER, sheet_rows_to_values

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
GOOGLE_SHEETS_API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"
SERVICE_ACCOUNT_FILE_ENV = "SAU_GOOGLE_SERVICE_ACCOUNT_FILE"
SERVICE_ACCOUNT_JSON_ENV = "SAU_GOOGLE_SERVICE_ACCOUNT_JSON"


@dataclass(frozen=True, slots=True)
class SheetExportResult:
    spreadsheet_id: str
    sheet_title: str
    spreadsheet_url: str
    row_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_service_account_info(
    *,
    service_account_file: str | None = None,
    service_account_json: str | None = None,
) -> dict:
    if service_account_json:
        return json.loads(service_account_json)

    env_json = os.environ.get(SERVICE_ACCOUNT_JSON_ENV, "").strip()
    if env_json:
        return json.loads(env_json)

    file_path = service_account_file or os.environ.get(SERVICE_ACCOUNT_FILE_ENV, "").strip()
    if not file_path:
        raise ValueError(
            "No Google service-account credentials configured. Set "
            "SAU_GOOGLE_SERVICE_ACCOUNT_FILE or SAU_GOOGLE_SERVICE_ACCOUNT_JSON."
        )
    return json.loads(Path(file_path).read_text(encoding="utf-8"))


def build_service_account_jwt(
    service_account_info: dict,
    *,
    scope: str = GOOGLE_SHEETS_SCOPE,
    now: int | None = None,
    lifetime_seconds: int = 3600,
) -> str:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ModuleNotFoundError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError("cryptography is required for Google service-account auth") from exc

    issued_at = int(time.time()) if now is None else now
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": service_account_info["client_email"],
        "scope": scope,
        "aud": GOOGLE_TOKEN_URL,
        "exp": issued_at + lifetime_seconds,
        "iat": issued_at,
    }
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))}."
        f"{_b64url(json.dumps(claims, separators=(',', ':')).encode('utf-8'))}"
    ).encode("utf-8")
    private_key = serialization.load_pem_private_key(
        service_account_info["private_key"].encode("utf-8"),
        password=None,
    )
    signature = private_key.sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{signing_input.decode('utf-8')}.{_b64url(signature)}"


class GoogleSheetsClient:
    def __init__(
        self,
        service_account_info: dict,
        *,
        session=None,
    ) -> None:
        self.service_account_info = service_account_info
        if session is None:
            if requests is None:
                raise RuntimeError("requests is required for Google Sheets export")
            self.session = requests.Session()
        else:
            self.session = session
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

    @classmethod
    def from_env(cls, *, session=None) -> "GoogleSheetsClient":
        return cls(load_service_account_info(), session=session)

    def _authorised_headers(self) -> dict[str, str]:
        token = self._access_token
        if token is None or time.time() >= self._access_token_expires_at:
            token = self._refresh_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _refresh_access_token(self) -> str:
        assertion = build_service_account_jwt(self.service_account_info)
        response = self.session.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        self._access_token = payload["access_token"]
        self._access_token_expires_at = time.time() + int(payload.get("expires_in", 3600)) - 60
        return self._access_token

    def create_spreadsheet(self, title: str) -> tuple[str, str]:
        response = self.session.post(
            GOOGLE_SHEETS_API_ROOT,
            headers=self._authorised_headers(),
            json={
                "properties": {"title": title},
                "sheets": [{"properties": {"title": title}}],
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        spreadsheet_id = payload["spreadsheetId"]
        return spreadsheet_id, f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"

    def get_spreadsheet_metadata(self, spreadsheet_id: str) -> dict:
        response = self.session.get(
            f"{GOOGLE_SHEETS_API_ROOT}/{spreadsheet_id}",
            headers=self._authorised_headers(),
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def ensure_sheet(self, spreadsheet_id: str, sheet_title: str) -> None:
        metadata = self.get_spreadsheet_metadata(spreadsheet_id)
        existing_titles = {
            sheet["properties"]["title"]
            for sheet in metadata.get("sheets", [])
        }
        if sheet_title in existing_titles:
            return
        response = self.session.post(
            f"{GOOGLE_SHEETS_API_ROOT}/{spreadsheet_id}:batchUpdate",
            headers=self._authorised_headers(),
            json={"requests": [{"addSheet": {"properties": {"title": sheet_title}}}]},
            timeout=60,
        )
        response.raise_for_status()

    def write_header_row(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        *,
        headers: list[str] | None = None,
    ) -> None:
        header_values = [headers or SHEET_COLUMN_ORDER]
        encoded_range = urllib.parse.quote(f"'{sheet_title}'!A1:T1", safe="")
        response = self.session.put(
            f"{GOOGLE_SHEETS_API_ROOT}/{spreadsheet_id}/values/{encoded_range}"
            "?valueInputOption=RAW",
            headers=self._authorised_headers(),
            json={"values": header_values},
            timeout=60,
        )
        response.raise_for_status()

    def append_rows(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        rows: list[dict[str, str]],
    ) -> None:
        encoded_range = urllib.parse.quote(f"'{sheet_title}'!A2:T", safe="")
        response = self.session.post(
            f"{GOOGLE_SHEETS_API_ROOT}/{spreadsheet_id}/values/{encoded_range}:append"
            "?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
            headers=self._authorised_headers(),
            json={"values": sheet_rows_to_values(rows)},
            timeout=60,
        )
        response.raise_for_status()

    def export_rows(
        self,
        *,
        sheet_title: str,
        rows: list[dict[str, str]],
        spreadsheet_id: str | None = None,
    ) -> SheetExportResult:
        if spreadsheet_id is None:
            spreadsheet_id, spreadsheet_url = self.create_spreadsheet(sheet_title)
        else:
            spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        self.ensure_sheet(spreadsheet_id, sheet_title)
        self.write_header_row(spreadsheet_id, sheet_title)
        if rows:
            self.append_rows(spreadsheet_id, sheet_title, rows)
        return SheetExportResult(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            spreadsheet_url=spreadsheet_url,
            row_count=len(rows),
        )
