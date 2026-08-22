"""Discovery tools: platform registry + workspace / auth introspection.

These are the cheapest calls and should always run first when an agent
starts a session — `whoami` tells the agent which workspace it's bound to
in multi-tenant mode, and `supported_platforms` lets the agent reason
about which `accounts_*` tool inputs are valid.
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP

from mcp_server._shared import resolve_db_path


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="whoami",
        description=(
            "Return the active workspace and transport metadata. In the default "
            "(open / single-tenant) deployment `workspaceId` is null and "
            "`tenantMode` is 'single'. In Google OIDC enforced mode the "
            "caller's workspace_id is included so the agent can scope its "
            "queries."
        ),
    )
    def whoami(db_path: str | None = None) -> dict[str, Any]:
        # Workspace scoping lives in the Flask layer today; the MCP layer is
        # workspace-agnostic. We surface env hints so a multi-tenant operator
        # can verify the right process is wired up.
        return {
            "server": "sau-mcp",
            "version": "0.1.0",
            "workspaceId": os.environ.get("SAU_MCP_WORKSPACE_ID"),
            "tenantMode": os.environ.get("SAU_MCP_TENANCY_MODE", "single"),
            "dbPath": str(resolve_db_path(db_path)),
        }

    @mcp.tool(
        name="supported_platforms",
        description=(
            "Return every platform slug the server recognises, plus the "
            "helper booleans `requiresCookie`, `defaultsToOauth`, and "
            "`supportsDirectPublish` for each. Use this to validate inputs "
            "before calling `accounts_create` or `publish_submit`."
        ),
    )
    def supported_platforms() -> dict[str, list[dict[str, Any]]]:
        from myUtils import profiles as profile_registry

        rows: list[dict[str, Any]] = []
        for slug in profile_registry.SUPPORTED_PLATFORMS:
            try:
                rows.append(
                    {
                        "platform": slug,
                        "requiresCookie": profile_registry.platform_requires_cookie(slug),
                        "defaultsToOauth": profile_registry.platform_defaults_to_oauth(slug),
                        "supportsDirectPublish": profile_registry.platform_supports_direct_publish(slug),
                        "supportsSheetExport": profile_registry.platform_supports_sheet_export(slug),
                    }
                )
            except ValueError:
                # Skip anything that fails validation (defensive — the
                # SUPPORTED_PLATFORMS tuple is curated so this shouldn't fire).
                continue
        return {"platforms": rows}
