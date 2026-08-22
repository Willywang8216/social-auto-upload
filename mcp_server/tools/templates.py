"""Publish-template MCP tools.

Thin wrappers around ``myUtils.publish_templates``. Templates are saved
Publish Center presets: a bundle of profile + account selectors plus
processing options (watermark, intro/outro, screenshots, schedule).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from mcp_server._shared import error_payload, resolve_db_path, to_dict
from myUtils import publish_templates as template_store


def _template_payload(template: template_store.PublishTemplate) -> dict[str, Any]:
    return {
        "id": template.id,
        "name": template.name,
        "slug": template.slug,
        "description": template.description,
        "config": template.config or {},
        "includedSettings": template.included_settings or [],
        "createdAt": template.created_at,
        "updatedAt": template.updated_at,
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="publish_templates_list",
        description="List every Publish Center template in the workspace.",
    )
    def publish_templates_list(db_path: str | None = None) -> list[dict[str, Any]]:
        try:
            rows = template_store.list_templates(db_path=resolve_db_path(db_path))
            return [_template_payload(t) for t in rows]
        except Exception as exc:  # noqa: BLE001
            return [error_payload(exc)]  # type: ignore[list-item]

    @mcp.tool(
        name="publish_templates_get",
        description="Fetch one template by id.",
    )
    def publish_templates_get(template_id: int, db_path: str | None = None) -> dict[str, Any]:
        try:
            return _template_payload(
                template_store.get_template(int(template_id), db_path=resolve_db_path(db_path))
            )
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="publish_templates_create",
        description=(
            "Create a new template. `name` is required (used to derive a "
            "unique slug). `included_settings` controls which Publish "
            "Center fields the template overrides on load — leave unset to "
            "override all."
        ),
    )
    def publish_templates_create(
        name: str,
        description: str = "",
        config: dict[str, Any] | None = None,
        included_settings: list[str] | None = None,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            template = template_store.create_template(
                name=name,
                description=description,
                config=config,
                included_settings=included_settings,
                db_path=resolve_db_path(db_path),
            )
            return _template_payload(template)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="publish_templates_update",
        description=(
            "Update one or more template fields. Only fields explicitly "
            "provided are mutated; pass null/omit to leave unchanged."
        ),
    )
    def publish_templates_update(
        template_id: int,
        name: str | None = None,
        description: str | None = None,
        config: dict[str, Any] | None = None,
        included_settings: list[str] | None = None,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            template = template_store.update_template(
                int(template_id),
                name=name,
                description=description,
                config=config,
                included_settings=included_settings,
                db_path=resolve_db_path(db_path),
            )
            return _template_payload(template)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="publish_templates_delete",
        description="Delete a template by id.",
    )
    def publish_templates_delete(
        template_id: int, db_path: str | None = None
    ) -> dict[str, Any]:
        try:
            template_store.delete_template(int(template_id), db_path=resolve_db_path(db_path))
            return {"deleted": True, "id": int(template_id)}
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)
