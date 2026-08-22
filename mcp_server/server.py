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
    jobs,
    profiles,
    publish,
    templates,
)


SERVER_NAME = "sau-mcp"
SERVER_VERSION = "0.1.0"
SERVER_INSTRUCTIONS = (
    "social-auto-upload MCP server. Manage profiles, accounts, publish "
    "templates, and publish jobs for a multi-platform social media uploader. "
    "Use `whoami` first to confirm the workspace, then drive CRUD via "
    "`accounts_*`, `profiles_*`, and `publish_*` tools."
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
    discovery.register(mcp)
    return mcp


def main(argv: list[str] | None = None) -> None:
    """Console-script entrypoint: ``sau-mcp``.

    Selects transport from ``SAU_MCP_TRANSPORT`` (default ``stdio``). When
    ``http``, reads ``SAU_MCP_HOST`` / ``SAU_MCP_PORT`` (default
    ``127.0.0.1:8765``) and binds there.
    """
    transport = os.environ.get("SAU_MCP_TRANSPORT", "stdio").strip().lower()
    kwargs: dict[str, Any] = {}
    if transport in ("http", "streamable-http", "sse"):
        host = os.environ.get("SAU_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
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
