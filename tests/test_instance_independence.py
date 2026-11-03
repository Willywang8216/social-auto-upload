"""The application factory must return genuinely independent instances.

The monolith registers ~158 routes on a module-global Flask app. This pins the
property that ``create_app()`` builds a *separate* application whose handlers
read that instance's configuration — not the global one — so two apps can
coexist in one process without leaking state into each other.

It also pins the import direction for the modules this hardening pass added:
routes → services → repositories/adapters, never back up into the monolith.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_ENV_A = {
    "APP_ENV": "testing",
    "SAU_API_TOKENS": "synthetic-token-a",
    "SAU_GOOGLE_LOGIN_ENABLED": "0",
    "SAU_AUTH_MODE": "legacy",
    "SAU_TENANCY_MODE": "single",
}
_ENV_B = {**_ENV_A, "SAU_API_TOKENS": "synthetic-token-b"}


def test_create_app_returns_a_distinct_instance():
    from sau_app import create_app

    first = create_app(_ENV_A)
    second = create_app(_ENV_A)

    assert first is not second
    assert first.config["SECURITY_POLICY"] is not second.config["SECURITY_POLICY"]


def test_two_apps_keep_their_own_configuration():
    from sau_app import create_app

    first = create_app(_ENV_A)
    second = create_app(_ENV_B)

    assert first.config["SECURITY_POLICY"].tokens == frozenset({"synthetic-token-a"})
    assert second.config["SECURITY_POLICY"].tokens == frozenset({"synthetic-token-b"})


def test_each_app_enforces_its_own_bearer_token():
    """The decisive check: one app's token must not satisfy the other."""
    from sau_app import create_app

    first = create_app(_ENV_A)
    second = create_app(_ENV_B)

    assert first.test_client().get("/whoami").status_code == 401
    assert second.test_client().get("/whoami").status_code == 401

    assert first.test_client().get(
        "/whoami", headers={"Authorization": "Bearer synthetic-token-a"}
    ).status_code == 200
    assert second.test_client().get(
        "/whoami", headers={"Authorization": "Bearer synthetic-token-b"}
    ).status_code == 200

    # cross-app: A's token is not valid on B
    assert second.test_client().get(
        "/whoami", headers={"Authorization": "Bearer synthetic-token-a"}
    ).status_code == 401
    assert first.test_client().get(
        "/whoami", headers={"Authorization": "Bearer synthetic-token-b"}
    ).status_code == 401


def test_a_built_app_carries_the_full_route_table():
    from sau_app import create_app

    app = create_app(_ENV_A)
    rules = [rule for rule in app.url_map.iter_rules() if rule.endpoint != "static"]

    assert len(rules) > 100
    paths = {str(rule) for rule in rules}
    for expected in ("/whoami", "/getFiles", "/deleteFile", "/upload/register",
                     "/publish-entities", "/api/inbox"):
        assert expected in paths, f"{expected} missing from the built app"


def test_the_global_app_is_left_alone():
    """Building an instance must not mutate the monolith's global app."""
    import sau_backend
    from sau_app import create_app

    before_rules = len(list(sau_backend.app.url_map.iter_rules()))
    before_policy = sau_backend.app.config["SECURITY_POLICY"]

    create_app(_ENV_B)

    assert len(list(sau_backend.app.url_map.iter_rules())) == before_rules
    assert sau_backend.app.config["SECURITY_POLICY"] is before_policy


# --------------------------------------------------------------------------- #
# Import direction
# --------------------------------------------------------------------------- #

#: Modules added/extended by the hardening pass, with the layers they may
#: depend on. None of them may import the Flask monolith: that is the
#: dependency inversion this refactor exists to prevent.
_LAYERED = (
    "myUtils/asset_lifecycle.py",
    "myUtils/media_serving.py",
    "myUtils/outbound_http.py",
    "sau_app/security/authorization.py",
    "sau_app/security/headers.py",
    "sau_app/security/rate_limit.py",
)


@pytest.mark.parametrize("relative", _LAYERED)
def test_hardening_modules_do_not_import_the_monolith(relative):
    path = REPO_ROOT / relative
    assert path.is_file(), f"{relative} is missing"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    offenders = {
        name for name in imported
        if name == "sau_backend" or name.startswith("sau_backend.")
    }
    assert not offenders, f"{relative} imports the monolith: {sorted(offenders)}"


def test_flask_free_services_stay_flask_free():
    """The pure policy modules must be importable without Flask."""
    for relative in ("myUtils/media_serving.py", "myUtils/asset_lifecycle.py",
                     "myUtils/outbound_http.py"):
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8-sig"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert "flask" not in imported, f"{relative} must stay Flask-free"
