"""Profile CRUD MCP tools.

Maps 1:1 onto ``myUtils.profiles`` Profile CRUD functions. Tools accept
``db_path`` so tests can route to a temp DB; production callers leave it
unset to use the project default.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from mcp_server._shared import error_payload, resolve_db_path, to_dict
from myUtils import profiles as profile_registry


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="profiles_list",
        description="List every profile in the active workspace.",
    )
    def profiles_list(db_path: str | None = None) -> list[dict[str, Any]]:
        try:
            rows = profile_registry.list_profiles(db_path=resolve_db_path(db_path))
            return [to_dict(p) for p in rows]
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="profiles_get",
        description="Fetch one profile by id. Returns 404-shaped error envelope when missing.",
    )
    def profiles_get(profile_id: int, db_path: str | None = None) -> dict[str, Any]:
        try:
            return to_dict(
                profile_registry.get_profile(int(profile_id), db_path=resolve_db_path(db_path))
            )
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="profiles_create",
        description=(
            "Create a new profile. `name` is required and is slugified "
            "for the underlying DB constraint; description and settings "
            "are optional."
        ),
    )
    def profiles_create(
        name: str,
        description: str = "",
        settings: dict[str, Any] | None = None,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            return to_dict(
                profile_registry.create_profile(
                    name=name,
                    description=description,
                    settings=settings,
                    db_path=resolve_db_path(db_path),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="profiles_update",
        description=(
            "Update one or more profile fields. Only fields explicitly "
            "provided are mutated; pass null/omit for fields to leave "
            "unchanged. Extended fields (default_language, timezone, "
            "system_prompt, writing_style_prompt, contact_details, "
            "default_cta, default_hashtags, default_link, "
            "google_sheet_folder_id) are forwarded to the DB column when set."
        ),
    )
    def profiles_update(
        profile_id: int,
        name: str | None = None,
        description: str | None = None,
        settings: dict[str, Any] | None = None,
        default_language: str | None = None,
        timezone: str | None = None,
        system_prompt: str | None = None,
        writing_style_prompt: str | None = None,
        contact_details: str | None = None,
        default_cta: str | None = None,
        default_hashtags: str | None = None,
        default_link: str | None = None,
        google_sheet_folder_id: str | None = None,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            extra = {
                k: v
                for k, v in {
                    "default_language": default_language,
                    "timezone": timezone,
                    "system_prompt": system_prompt,
                    "writing_style_prompt": writing_style_prompt,
                    "contact_details": contact_details,
                    "default_cta": default_cta,
                    "default_hashtags": default_hashtags,
                    "default_link": default_link,
                    "google_sheet_folder_id": google_sheet_folder_id,
                }.items()
                if v is not None
            }
            return to_dict(
                profile_registry.update_profile(
                    int(profile_id),
                    name=name,
                    description=description,
                    settings=settings,
                    db_path=resolve_db_path(db_path),
                    **extra,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="profiles_delete",
        description=(
            "Delete a profile. Returns an error envelope when the profile "
            "is missing or still owns accounts (the DB enforces FK cleanup)."
        ),
    )
    def profiles_delete(profile_id: int, db_path: str | None = None) -> dict[str, Any]:
        try:
            profile_registry.delete_profile(int(profile_id), db_path=resolve_db_path(db_path))
            return {"deleted": True, "id": int(profile_id)}
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)
