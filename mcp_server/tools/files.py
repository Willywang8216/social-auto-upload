"""File / media MCP tools.

Currently exposes ``upload_register`` which idempotently inserts a row
into ``file_records`` for a path that lives under ``videoFile/``. Upload
itself (multipart bytes) is the responsibility of the web UI today;
the MCP layer focuses on registering already-on-disk files so the publish
pipeline can reference them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from mcp_server._shared import error_payload, resolve_db_path


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="upload_register",
        description=(
            "Register an existing file under ``videoFile/`` with the publish "
            "pipeline. Returns the new (or pre-existing) file_record id. "
            "Path is sandboxed to ``videoFile/`` to prevent directory "
            "traversal."
        ),
    )
    def upload_register(file_path: str, db_path: str | None = None) -> dict[str, Any]:
        resolved_db = resolve_db_path(db_path)
        from sau_backend import _ensure_file_record_for_path  # local import: shared helper

        try:
            record_id = _ensure_file_record_for_path(file_path, db_path=resolved_db)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

        # Re-check the resolved path so the caller knows the file actually
        # exists on disk; the helper silently records a row for missing
        # files (size=None) which is the wrong surface for an agent.
        try:
            from utils.conf_defaults import BASE_DIR

            base = (Path(BASE_DIR) / "videoFile").resolve()
            absolute = (base / file_path).resolve()
            try:
                absolute.relative_to(base)
            except ValueError:
                return error_payload(
                    ValueError(f"file_path escapes videoFile root: {file_path!r}")
                )
            exists = absolute.exists()
        except Exception:  # noqa: BLE001
            exists = None

        return {
            "fileRecordId": int(record_id),
            "filePath": file_path,
            "exists": exists,
        }
