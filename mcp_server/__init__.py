"""sau-mcp: Model Context Protocol server for social-auto-upload.

This package exposes every CRUD + publish/schedule operation as MCP tools so
AI agents (Cursor, Claude Desktop, etc.) can manage accounts, schedule posts,
and read publish-job state without direct database or filesystem access.

Two transports are supported:

* ``stdio``  — local agents (Claude Desktop, ``mcp`` CLI). Default.
* ``http``   — remote agents. Bind address from ``SAU_MCP_HOST`` /
  ``SAU_MCP_PORT`` (defaults ``127.0.0.1:8765``).

The console script ``sau-mcp`` boots the server; ``build_server()`` is the
factory used by tests and embedders.
"""

from __future__ import annotations

from mcp_server.server import build_server, main

__all__ = ["build_server", "main"]
