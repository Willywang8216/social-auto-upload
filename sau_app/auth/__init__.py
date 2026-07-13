"""Google OpenID Connect application login (Phase 3 transport).

Flag-gated (``SAU_GOOGLE_LOGIN_ENABLED``, default off): when disabled nothing is
registered and the legacy bearer-token flow is untouched. When enabled, the
blueprint adds server-side-session Google login on top of the identity/workspace
provisioning from the Phase 3 foundation. The Google network calls live behind
an injectable client (``app.config['SAU_OIDC_CLIENT']``) so the whole flow is
testable against mocked Google endpoints without real credentials.
"""

from __future__ import annotations

from .routes import register_auth

__all__ = ["register_auth"]
