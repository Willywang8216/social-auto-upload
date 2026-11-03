"""MCP containment: the db_path capability and the HTTP bind default.

The MCP server exposes the whole account/media/job surface. Two things must
therefore not be ambient: the ability to aim every tool at an arbitrary SQLite
file, and the ability to publish the server on a network interface.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_db_path_override_is_refused_outside_the_allowed_roots(monkeypatch, tmp_path):
    from mcp_server import _shared

    monkeypatch.delenv("SAU_TESTING", raising=False)
    monkeypatch.delenv("SAU_MCP_DB_PATH", raising=False)
    monkeypatch.delenv("SAU_MCP_ALLOWED_DB_ROOTS", raising=False)

    with pytest.raises(PermissionError):
        _shared.resolve_db_path(str(tmp_path / "somewhere-else.db"))


def test_db_path_env_override_is_also_guarded(monkeypatch, tmp_path):
    from mcp_server import _shared

    monkeypatch.delenv("SAU_TESTING", raising=False)
    monkeypatch.delenv("SAU_MCP_ALLOWED_DB_ROOTS", raising=False)
    monkeypatch.setenv("SAU_MCP_DB_PATH", str(tmp_path / "env-elsewhere.db"))

    with pytest.raises(PermissionError):
        _shared.resolve_db_path()


def test_db_path_allows_the_configured_database_directory(monkeypatch):
    from mcp_server import _shared
    from myUtils import profiles

    monkeypatch.delenv("SAU_TESTING", raising=False)
    monkeypatch.delenv("SAU_MCP_ALLOWED_DB_ROOTS", raising=False)

    default = Path(profiles.DB_PATH)
    assert _shared.resolve_db_path(str(default)) == default.resolve()


def test_db_path_allowed_roots_widens_the_capability(monkeypatch, tmp_path):
    from mcp_server import _shared

    monkeypatch.delenv("SAU_TESTING", raising=False)
    monkeypatch.setenv("SAU_MCP_ALLOWED_DB_ROOTS", str(tmp_path))

    target = tmp_path / "custom.db"
    assert _shared.resolve_db_path(str(target)) == target.resolve()


def test_db_path_default_is_used_when_nothing_is_requested(monkeypatch):
    from mcp_server import _shared
    from myUtils import profiles

    monkeypatch.delenv("SAU_TESTING", raising=False)
    monkeypatch.delenv("SAU_MCP_DB_PATH", raising=False)
    assert _shared.resolve_db_path(None) == Path(profiles.DB_PATH)


def test_db_path_is_permissive_under_the_test_suite(monkeypatch, tmp_path):
    """The suite legitimately points tools at throwaway databases."""
    from mcp_server import _shared

    monkeypatch.setenv("SAU_TESTING", "1")
    outside = tmp_path / "throwaway.db"
    assert _shared.resolve_db_path(str(outside)) == outside


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #

def _stub_server(monkeypatch):
    from mcp_server import server

    recorded: dict = {}

    class _Stub:
        def run(self, **kwargs):  # noqa: ANN003
            recorded.update(kwargs)

    monkeypatch.setattr(server, "build_server", lambda: _Stub())
    return server, recorded


def test_non_loopback_http_bind_is_refused(monkeypatch):
    server, recorded = _stub_server(monkeypatch)
    monkeypatch.setenv("SAU_MCP_TRANSPORT", "http")
    monkeypatch.setenv("SAU_MCP_HOST", "0.0.0.0")
    monkeypatch.delenv("SAU_MCP_ALLOW_REMOTE", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        server.main()

    assert "refusing to bind" in str(excinfo.value)
    assert recorded == {}, "the server must not start before the guard passes"


def test_loopback_http_bind_is_allowed(monkeypatch):
    server, recorded = _stub_server(monkeypatch)
    monkeypatch.setenv("SAU_MCP_TRANSPORT", "http")
    monkeypatch.setenv("SAU_MCP_HOST", "127.0.0.1")
    monkeypatch.delenv("SAU_MCP_ALLOW_REMOTE", raising=False)

    server.main()

    assert recorded["host"] == "127.0.0.1"
    assert recorded["transport"] == "streamable-http"


def test_remote_bind_is_allowed_only_with_the_explicit_flag(monkeypatch):
    server, recorded = _stub_server(monkeypatch)
    monkeypatch.setenv("SAU_MCP_TRANSPORT", "http")
    monkeypatch.setenv("SAU_MCP_HOST", "10.0.0.5")
    monkeypatch.setenv("SAU_MCP_ALLOW_REMOTE", "1")

    server.main()

    assert recorded["host"] == "10.0.0.5"


def test_stdio_transport_is_unaffected(monkeypatch):
    server, recorded = _stub_server(monkeypatch)
    monkeypatch.setenv("SAU_MCP_TRANSPORT", "stdio")

    server.main()

    assert recorded == {"transport": "stdio"}


@pytest.mark.parametrize(
    "host,expected",
    [("127.0.0.1", True), ("::1", True), ("localhost", True),
     ("0.0.0.0", False), ("10.0.0.5", False), ("example.invalid", False)],
)
def test_is_loopback(host, expected):
    from mcp_server.server import _is_loopback

    assert _is_loopback(host) is expected
