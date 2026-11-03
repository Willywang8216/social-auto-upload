"""FastMCP server entry point.

Builds the MCP server, registers every tool, and exposes ``main()`` for the
``sau-mcp`` console script. All tool implementations live under
``mcp_server/tools`` and are wired in here so this module stays the single
boot file.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import FastMCP

from mcp_server.tools import (
    accounts,
    discovery,
    files,
    inbox,
    jobs,
    profiles,
    publish,
    templates,
)


SERVER_NAME = "sau-mcp"
SERVER_VERSION = "0.1.0"
SERVER_INSTRUCTIONS = (
    "social-auto-upload MCP server. Manage profiles, accounts, publish "
    "templates, publish jobs, and the SAU-Inbox phone-first queue for a "
    "multi-platform social media uploader. Use `whoami` first to confirm the "
    "workspace, then drive CRUD via `accounts_*`, `profiles_*`, `publish_*`, "
    "`jobs_*`, and `inbox_*` tools."
)


def build_server() -> FastMCP:
    """Construct a fresh FastMCP server with every tool registered.

    A factory (not a module-level singleton) so tests can build isolated
    servers with patched dependencies.
    """
    mcp = FastMCP(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
    )
    profiles.register(mcp)
    accounts.register(mcp)
    templates.register(mcp)
    files.register(mcp)
    jobs.register(mcp)
    publish.register(mcp)
    inbox.register(mcp)
    discovery.register(mcp)
    return mcp


def _is_loopback(host: str) -> bool:
    """True for a host that is only reachable from this machine."""
    if host in {"localhost", "::1"}:
        return True
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _remote_bind_allowed() -> bool:
    return str(os.environ.get("SAU_MCP_ALLOW_REMOTE", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def main(argv: list[str] | None = None) -> None:
    """Console-script entrypoint: ``sau-mcp``.

    Selects transport from ``SAU_MCP_TRANSPORT`` (default ``stdio``). When
    ``http``, reads ``SAU_MCP_HOST`` / ``SAU_MCP_PORT`` (default
    ``127.0.0.1:8765``) and binds there.

    A non-loopback HTTP bind is refused unless ``SAU_MCP_ALLOW_REMOTE=1``:
    this server exposes the full account/media/job surface and has no
    authentication of its own, so publishing it on a network interface is a
    deliberate operator decision, not a default.
    """
    transport = os.environ.get("SAU_MCP_TRANSPORT", "stdio").strip().lower()
    kwargs: dict[str, Any] = {}
    if transport in ("http", "streamable-http", "sse"):
        host = os.environ.get("SAU_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
        if not _is_loopback(host) and not _remote_bind_allowed():
            raise SystemExit(
                f"sau-mcp: refusing to bind {host!r}: this server has no "
                "authentication. Bind a loopback address, or set "
                "SAU_MCP_ALLOW_REMOTE=1 to accept the exposure explicitly."
            )
        port_raw = os.environ.get("SAU_MCP_PORT", "8765").strip() or "8765"
        try:
            port = int(port_raw)
        except ValueError:
            logging.warning(
                "sau-mcp: invalid SAU_MCP_PORT=%r, falling back to 8765", port_raw
            )
            port = 8765
        kwargs["host"] = host
        kwargs["port"] = port
        # FastMCP aliases ``streamable-http`` for the modern HTTP transport;
        # keep the legacy ``http`` value working by mapping it explicitly.
        if transport == "http":
            transport = "streamable-http"

    server = build_server()
    # FastMCP's `run` is a blocking call; it returns when the client
    # disconnects (stdio) or the HTTP server stops.
    server.run(transport=transport, **kwargs)


if __name__ == "__main__":  # pragma: no cover
    main()
