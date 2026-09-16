"""SAU-Inbox MCP tools: list / approve / reject the phone-first queue.

Wraps ``myUtils.inbox_ops``. Read-only list + approve/reject (approve is the
MCP side of the publish-center flow; the actual publish still goes through
``publish_submit``).
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP

from mcp_server._shared import error_payload


def _resolve_inbox_env() -> dict[str, str]:
    env = {}
    if os.environ.get("SAU_INBOX"):
        env["SAU_INBOX"] = os.environ["SAU_INBOX"]
    if os.environ.get("SAU_WATCH_STATE"):
        env["SAU_WATCH_STATE"] = os.environ["SAU_WATCH_STATE"]
    return env


def register(mcp: FastMCP) -> None:
    import myUtils.inbox_ops as inbox

    @mcp.tool(
        name="inbox_list",
        description=(
            "List the SAU-Inbox phone-first queue. Returns ready, pending and "
            "quarantined items. Each item carries persona, kind, sfw/nsfw flag, "
            "topic, sourcePath and brief, so an agent can pick one and feed it "
            "to publish_submit."
        ),
    )
    def inbox_list(db_path: str | None = None) -> dict[str, Any]:
        try:
            lists = inbox.list_items()
            return {k: [inbox.item_payload(i) for i in v] for k, v in lists.items()}
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="inbox_approve",
        description=(
            "Mark a ready inbox item approved (move to processed trail). "
            "Posts are then published through publish_submit with the item's "
            "persona/profileIds/brief/sourcePath."
        ),
    )
    def inbox_approve(item_id: str, db_path: str | None = None) -> dict[str, Any]:
        try:
            entry = inbox.approve(item_id)
            return inbox.item_payload(entry)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="inbox_reject",
        description=(
            "Move a ready inbox item to the quarantined list with an optional "
            "reason. Use when the media must NOT be published."
        ),
    )
    def inbox_reject(item_id: str, reason: str | None = None,
                     db_path: str | None = None) -> dict[str, Any]:
        try:
            entry = inbox.reject(item_id, reason=reason)
            return inbox.item_payload(entry)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)
