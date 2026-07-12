"""Google OIDC client — the only part that talks to Google.

Isolated behind a small interface so the login routes can be driven by a fake in
tests (no network, no real credentials). The real implementation performs the
Authorization Code + PKCE exchange and verifies the ID token (signature via
Google's JWKS, plus issuer/audience/expiry/nonce).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
LOGIN_SCOPES = "openid email profile"


class OIDCError(Exception):
    """Raised when a Google login cannot be completed (invalid token, etc.)."""


@dataclass(frozen=True)
class GoogleClaims:
    subject: str
    email: str | None
    email_verified: bool
    name: str | None
    picture: str | None
    raw: dict = field(default_factory=dict)


class GoogleOIDCClient:
    """Real Google OIDC client (used when ``SAU_GOOGLE_LOGIN_ENABLED`` is on)."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret

    def authorization_url(
        self, *, state: str, nonce: str, code_challenge: str, redirect_uri: str
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": LOGIN_SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"

    def fetch_claims(
        self, *, code: str, code_verifier: str, redirect_uri: str, expected_nonce: str
    ) -> GoogleClaims:
        import requests  # local import: only needed for the live path
        from authlib.jose import JsonWebKey, jwt

        token_resp = requests.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            timeout=15,
        )
        if token_resp.status_code != 200:
            raise OIDCError(f"token exchange failed: HTTP {token_resp.status_code}")
        id_token = (token_resp.json() or {}).get("id_token")
        if not id_token:
            raise OIDCError("token response missing id_token")

        try:
            jwks = requests.get(GOOGLE_JWKS_URI, timeout=15).json()
            claims = jwt.decode(id_token, JsonWebKey.import_key_set(jwks))
            claims.validate()  # exp/iat/nbf
        except Exception as exc:  # noqa: BLE001
            raise OIDCError(f"id_token verification failed: {exc}") from exc

        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise OIDCError("id_token has an unexpected issuer")
        if claims.get("aud") != self.client_id:
            raise OIDCError("id_token audience mismatch")
        if claims.get("nonce") != expected_nonce:
            raise OIDCError("id_token nonce mismatch (possible replay)")
        if not claims.get("sub"):
            raise OIDCError("id_token missing sub")

        return GoogleClaims(
            subject=str(claims.get("sub")),
            email=claims.get("email"),
            email_verified=bool(claims.get("email_verified")),
            name=claims.get("name"),
            picture=claims.get("picture"),
            raw=dict(claims),
        )
