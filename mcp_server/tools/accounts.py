"""Account CRUD MCP tools.

Wraps ``myUtils.profiles`` Account CRUD + ``accounts_check`` (live cookie
probe) + ``accounts_health`` (counts per platform). All tools accept
``db_path`` so tests can point at a temp DB.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from mcp_server._shared import error_payload, resolve_db_path, to_dict
from myUtils import profiles as profile_registry
from myUtils import secret_redaction


def _account_payload(account: profile_registry.Account) -> dict[str, Any]:
    """MCP-friendly account dict.

    Mirrors ``sau_backend._account_payload``: secret values are redacted
    and ``nickname`` / ``account_group`` are surfaced under both snake and
    camelCase keys for backward compatibility with the Flask layer.
    """
    payload = to_dict(account)
    if isinstance(payload.get("config"), (dict, list)):
        payload["config"] = secret_redaction.redact_config_secrets(payload["config"])
    payload["nickname"] = account.nickname or ""
    payload["account_group"] = account.account_group or ""
    payload["accountGroup"] = payload["account_group"]
    return payload


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="accounts_list",
        description=(
            "List accounts, with optional filters: `profile_id`, `platform`, "
            "`enabled` (true/false/null=any), and `group` (operator-defined "
            "group label; empty string lists ungrouped accounts)."
        ),
    )
    def accounts_list(
        profile_id: int | None = None,
        platform: str | None = None,
        enabled: bool | None = None,
        group: str | None = None,
        db_path: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            rows = profile_registry.list_accounts(
                profile_id=profile_id,
                platform=platform,
                enabled=enabled,
                account_group=group,
                db_path=resolve_db_path(db_path),
            )
            return [_account_payload(a) for a in rows]
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="accounts_get",
        description="Fetch one account by id. Returns 404-shaped error envelope when missing.",
    )
    def accounts_get(account_id: int, db_path: str | None = None) -> dict[str, Any]:
        try:
            return _account_payload(
                profile_registry.get_account(int(account_id), db_path=resolve_db_path(db_path))
            )
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="accounts_groups",
        description=(
            "Return the distinct, non-empty operator-defined groups present "
            "in the active workspace. Useful for populating filter UIs "
            "before calling `accounts_list`."
        ),
    )
    def accounts_groups(db_path: str | None = None) -> list[str]:
        try:
            return profile_registry.list_account_groups(db_path=resolve_db_path(db_path))
        except Exception as exc:  # noqa: BLE001
            return [error_payload(exc)]  # type: ignore[list-item]

    @mcp.tool(
        name="accounts_create",
        description=(
            "Create a new account under a profile. `platform` must be one "
            "of the values returned by `supported_platforms`. `cookie_path` "
            "is auto-resolved when omitted (cookies/<platform>/<slug>/<account>.json)."
        ),
    )
    def accounts_create(
        profile_id: int,
        platform: str,
        account_name: str,
        nickname: str = "",
        account_group: str = "",
        auth_type: str = "cookie",
        config: dict[str, Any] | None = None,
        enabled: bool = True,
        cookie_path: str | None = None,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            account = profile_registry.add_account(
                int(profile_id),
                platform,
                account_name,
                nickname=nickname,
                account_group=account_group,
                auth_type=auth_type,
                config=config,
                enabled=enabled,
                cookie_path=cookie_path,
                db_path=resolve_db_path(db_path),
            )
            return _account_payload(account)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="accounts_update",
        description=(
            "Update one or more account fields. Only fields explicitly "
            "provided are mutated; pass null/omit for fields to leave "
            "unchanged. Supports nickname, accountGroup, account_name, "
            "profile_id, auth_type, config, enabled, status."
        ),
    )
    def accounts_update(
        account_id: int,
        account_name: str | None = None,
        profile_id: int | None = None,
        nickname: str | None = None,
        account_group: str | None = None,
        auth_type: str | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool | None = None,
        status: int | None = None,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            updated = profile_registry.update_account(
                int(account_id),
                profile_id=profile_id,
                account_name=account_name,
                nickname=nickname,
                account_group=account_group,
                auth_type=auth_type,
                config=config,
                enabled=enabled,
                status=status,
                db_path=resolve_db_path(db_path),
            )
            return _account_payload(updated)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="accounts_delete",
        description="Delete an account by id. Returns a deleted/true envelope on success.",
    )
    def accounts_delete(account_id: int, db_path: str | None = None) -> dict[str, Any]:
        try:
            profile_registry.delete_account(int(account_id), db_path=resolve_db_path(db_path))
            return {"deleted": True, "id": int(account_id)}
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="accounts_check",
        description=(
            "Probe an account's live connection. For cookie-auth platforms "
            "the saved cookie file is loaded and validated; for OAuth "
            "platforms the configured provider is queried. Returns the "
            "post-check account payload with refreshed status."
        ),
    )
    def accounts_check(account_id: int, db_path: str | None = None) -> dict[str, Any]:
        from sau_backend import _run_account_connection_check  # local import: pulls Flask globals

        resolved = resolve_db_path(db_path)
        try:
            # Mirrors /api/accounts/<id>/check: 404 when missing,
            # ok envelope when probe succeeds, error envelope otherwise.
            profile_registry.get_account(int(account_id), db_path=resolved)
        except LookupError as exc:
            return error_payload(exc)
        try:
            updated = _run_account_connection_check(
                account_id=int(account_id), db_path=resolved
            )
            return {"ok": True, "account": _account_payload(updated)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, **error_payload(exc)}

    @mcp.tool(
        name="accounts_health",
        description=(
            "Per-platform readiness summary: returns `{platform, total, "
            "ready, pct}` rows. Ready counts `enabled AND status in (0,1)`."
        ),
    )
    def accounts_health(db_path: str | None = None) -> list[dict[str, Any]]:
        try:
            rows = profile_registry.list_accounts(db_path=resolve_db_path(db_path))
        except Exception as exc:  # noqa: BLE001
            return [error_payload(exc)]  # type: ignore[list-item]
        agg: dict[str, dict[str, Any]] = {}
        for a in rows:
            row = agg.setdefault(a.platform, {"platform": a.platform, "ready": 0, "total": 0})
            row["total"] += 1
            if a.enabled and a.status in (0, 1):
                row["ready"] += 1
        for row in agg.values():
            row["pct"] = round(100 * row["ready"] / row["total"]) if row["total"] else 0
        return list(agg.values())
