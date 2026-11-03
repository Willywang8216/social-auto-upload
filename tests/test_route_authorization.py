"""Route authorization coverage (Phase 1 hardening).

Two claims are checked here:

1. **Every non-public mutating route resolves to a permission.** The route→
   permission table is exhaustive, so adding a new write endpoint without
   classifying it fails this suite rather than silently shipping ungated.
2. **Every non-public route rejects an unauthenticated caller** when the
   bearer policy is configured, and role permissions are actually enforced for
   session callers (viewer cannot write, editor can).
"""
from __future__ import annotations

import re

import pytest

_PUBLIC = None  # resolved lazily from the app's own policy


def _app():
    import sau_backend

    return sau_backend.app


def _concrete(path: str) -> str:
    """Replace Flask converters with a value so the rule is requestable."""
    return re.sub(r"<[^<>]+>", "1", path)


def _rules():
    app = _app()
    policy = app.config["SECURITY_POLICY"]
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if path.startswith("/static"):
            continue
        methods = {
            m for m in rule.methods
            if m in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        }
        if not methods:
            continue
        yield rule, path, methods, policy.is_public_path(path)


# --------------------------------------------------------------------------- #
# 1. The permission table is exhaustive
# --------------------------------------------------------------------------- #

def test_every_non_public_mutating_route_has_a_permission():
    from sau_app.security.authorization import required_permission

    unmapped: list[str] = []
    checked = 0
    for _rule, path, methods, public in _rules():
        if public:
            continue
        for method in sorted(methods):
            if method not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            checked += 1
            if required_permission(method, _concrete(path)) is None:
                unmapped.append(f"{method} {path}")

    assert checked > 0, "route enumeration found no mutating routes to check"
    assert unmapped == [], (
        "these non-public mutating routes have no permission mapping and would "
        "be callable by any authenticated session role: " + ", ".join(sorted(unmapped))
    )


def test_installer_skips_public_paths():
    """A public path stays reachable for a session caller of any role."""
    from flask import Flask, g, jsonify

    from myUtils.security import SecurityPolicy
    from sau_app.auth.permissions import permissions_for
    from sau_app.db.identity_models import ROLE_VIEWER
    from sau_app.security.authorization import install_authorization
    from sau_app.tenancy.context import AuthContext, METHOD_GOOGLE_SESSION

    app = Flask(__name__)
    app.config["SECURITY_POLICY"] = SecurityPolicy(
        tokens=frozenset({"synthetic"}), cors_origins=()
    )

    @app.before_request
    def _inject():  # noqa: ANN202
        g.auth_ctx = AuthContext(
            authenticated=True,
            auth_method=METHOD_GOOGLE_SESSION,
            permissions=frozenset(permissions_for(ROLE_VIEWER)),
        )

    @app.route("/getFile")
    def public_file():  # noqa: ANN202
        return jsonify({"ok": True})

    install_authorization(app)
    assert app.test_client().get("/getFile").status_code == 200


# --------------------------------------------------------------------------- #
# 2. Unauthenticated callers are rejected on every non-public route
# --------------------------------------------------------------------------- #

def test_every_non_public_route_rejects_an_unauthenticated_caller(monkeypatch):
    import sau_backend
    from myUtils.security import SecurityPolicy

    app = _app()
    monkeypatch.setitem(
        app.config, "SECURITY_POLICY",
        SecurityPolicy(tokens=frozenset({"synthetic-operator-token"}), cors_origins=()),
    )
    client = app.test_client()

    failures: list[str] = []
    checked = 0
    for _rule, path, methods, public in _rules():
        if public:
            continue
        target = _concrete(path)
        for method in sorted(methods):
            checked += 1
            response = client.open(target, method=method, json={})
            if response.status_code not in {401, 403}:
                failures.append(f"{method} {target} -> {response.status_code}")

    assert checked > 0
    assert failures == [], (
        "these non-public routes did not reject an anonymous caller: "
        + ", ".join(sorted(failures)[:20])
    )


def test_a_valid_bearer_token_still_reaches_the_operator_path(monkeypatch):
    """The single-tenant operator path must be unchanged by this work."""
    import sau_backend
    from myUtils.security import SecurityPolicy

    app = _app()
    monkeypatch.setitem(
        app.config, "SECURITY_POLICY",
        SecurityPolicy(tokens=frozenset({"synthetic-operator-token"}), cors_origins=()),
    )
    response = app.test_client().get(
        "/whoami", headers={"Authorization": "Bearer synthetic-operator-token"}
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# 3. Role permissions are enforced for session callers
# --------------------------------------------------------------------------- #

def _role_app(role: str):
    from flask import Flask, g, jsonify

    from sau_app.auth.permissions import permissions_for
    from sau_app.security.authorization import install_authorization
    from sau_app.tenancy.context import AuthContext, METHOD_GOOGLE_SESSION

    app = Flask(__name__)

    @app.before_request
    def _inject_context():  # noqa: ANN202
        g.auth_ctx = AuthContext(
            authenticated=True,
            auth_method=METHOD_GOOGLE_SESSION,
            user_id="u1",
            workspace_id="w1",
            role=role,
            permissions=frozenset(permissions_for(role)),
        )

    @app.route("/getFiles", methods=["GET"])
    def list_files():  # noqa: ANN202
        return jsonify({"ok": True})

    @app.route("/deleteFile", methods=["DELETE"])
    def delete_file():  # noqa: ANN202
        return jsonify({"ok": True})

    @app.route("/upload", methods=["POST"])
    def upload():  # noqa: ANN202
        return jsonify({"ok": True})

    @app.route("/profiles", methods=["POST"])
    def create_profile():  # noqa: ANN202
        return jsonify({"ok": True})

    @app.route("/downloadCookie", methods=["GET"])
    def cookie():  # noqa: ANN202
        return jsonify({"ok": True})

    install_authorization(app)
    return app.test_client()


def test_viewer_can_read_but_not_write():
    from sau_app.db.identity_models import ROLE_VIEWER

    client = _role_app(ROLE_VIEWER)
    assert client.get("/getFiles").status_code == 200
    assert client.delete("/deleteFile?id=1").status_code == 403
    assert client.post("/upload").status_code == 403
    assert client.post("/profiles").status_code == 403


def test_viewer_cannot_export_credentials():
    from sau_app.db.identity_models import ROLE_VIEWER

    assert _role_app(ROLE_VIEWER).get("/downloadCookie").status_code == 403


def test_editor_can_write_but_not_export_credentials():
    from sau_app.db.identity_models import ROLE_EDITOR

    client = _role_app(ROLE_EDITOR)
    assert client.delete("/deleteFile?id=1").status_code == 200
    assert client.post("/upload").status_code == 200
    assert client.post("/profiles").status_code == 200
    # credentials:export is owner-only
    assert client.get("/downloadCookie").status_code == 403


def test_credentials_export_is_owner_only():
    """Decrypted cookie export is the most sensitive read: owners only.

    Cross-workspace isolation is a separate guarantee — a foreign account must
    resolve as 404 rather than 403, which the tenant-isolation suite pins.
    """
    from sau_app.db.identity_models import ROLE_ADMIN, ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER

    for role in (ROLE_VIEWER, ROLE_EDITOR, ROLE_ADMIN):
        assert _role_app(role).get("/downloadCookie").status_code == 403, role
    assert _role_app(ROLE_OWNER).get("/downloadCookie").status_code == 200


def test_permission_denial_is_403_not_401():
    """A permission failure must not look like a missing login."""
    from sau_app.db.identity_models import ROLE_VIEWER

    response = _role_app(ROLE_VIEWER).post("/upload")
    assert response.status_code == 403
    assert "missing permission" in response.get_json()["msg"]


# --------------------------------------------------------------------------- #
# 4. Mapping details
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "method,path,expected_area",
    [("GET", "/getFiles", "media"),
     ("DELETE", "/deleteFile", "media"),
     ("POST", "/uploadSave", "media"),
     ("POST", "/postVideo", "publish"),
     ("GET", "/jobs", "publish"),
     ("POST", "/campaigns/1/publish", "publish"),
     ("GET", "/analytics/videos", "analytics"),
     ("GET", "/profiles", "profiles"),
     ("POST", "/oauth/meta/start", "accounts"),
     ("GET", "/admin/oauth/status", "admin"),
     ("GET", "/downloadCookie", "credentials")],
)
def test_area_mapping(method, path, expected_area):
    from sau_app.security.authorization import area_for

    assert area_for(path) == expected_area


def test_read_and_write_map_to_different_permissions_where_it_matters():
    from sau_app.security.authorization import required_permission

    assert required_permission("GET", "/profiles") == "profiles:read"
    assert required_permission("POST", "/profiles") == "profiles:write"
    assert required_permission("GET", "/deleteFile") == "media:read"
    assert required_permission("DELETE", "/deleteFile") == "media:write"


def test_legacy_token_is_fully_trusted():
    from sau_app.tenancy.context import AuthContext

    ctx = AuthContext.legacy_token()
    assert ctx.authenticated is True
    assert ctx.has_permission("credentials:export") is True


def test_committed_route_matrix_matches_the_live_app():
    """``dump_route_matrix.py --check`` must pass.

    The committed matrix is the reviewed record of what each route exposes.
    If it drifts from the application, that record is lying — so this is a
    release gate, not a convenience.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-X", "utf8",
         str(root / "scripts" / "audit" / "dump_route_matrix.py"), "--check"],
        capture_output=True, text=True, cwd=str(root), timeout=180,
    )
    assert proc.returncode == 0, (
        "route matrix is stale; run scripts/audit/refresh_route_matrix.py\n"
        + (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
    )
