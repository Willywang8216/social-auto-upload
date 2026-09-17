"""Notion REST accessor for the SAU long-form scheduler.

Talks to the Notion API directly (version 2022-06-28) with an integration
token, so the pipeline does not depend on the MCP transport.

Two operations are needed:

* ``fetch_pending_rows(db_id)`` — pages whose publish column == 完成未發布
* ``mark_published(page_id)`` — set that column to 完成已發布

The publish column's *type* (select / status / rich_text) is discovered per
database, so the accessor keeps working if the operator changes it.

Token resolution order:
1. explicit ``token=`` argument
2. ``NOTION_TOKEN`` env var
3. ``NOTION_TOKEN_FILE`` env var (a file containing the token, or the
   ``{"access_token": ...}`` JSON the self-hosted bridge writes)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

FINISHED_UNPUBLISHED = "完成未發布"
FINISHED_PUBLISHED = "完成已發布"

_PUBLISH_PROPERTY_CANDIDATES = ("publish", "Publish", "發布", "狀態", "Status")


class NotionError(RuntimeError):
    """Any Notion API failure, carrying the API's own message."""


def _read_token(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get("NOTION_TOKEN", "").strip()
    if env:
        return env
    path = os.environ.get("NOTION_TOKEN_FILE", "").strip()
    if path:
        raw = Path(path).read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            return str(data.get("access_token") or data.get("accessToken") or "").strip()
        return raw
    return ""


class NotionAccessor:
    """Thin Notion API client: query a database, patch a page property."""

    def __init__(self, token: str | None = None, *, timeout: int = 20):
        self._token = _read_token(token)
        self._timeout = timeout

    # ---- transport ---------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self._token:
            raise NotionError(
                "No Notion token: pass token=, set NOTION_TOKEN, or NOTION_TOKEN_FILE")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{NOTION_API}{path}", data=data, method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise NotionError(f"Notion API {exc.code} on {method} {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise NotionError(f"Notion API unreachable: {exc}") from exc

    # ---- schema ------------------------------------------------------
    def publish_property(self, db_id: str) -> tuple[str, str]:
        """Return (property_name, type) for the database's publish column."""
        db = self._request("GET", f"/databases/{db_id}")
        props = db.get("properties") or {}
        for name in _PUBLISH_PROPERTY_CANDIDATES:
            if name in props:
                return name, str(props[name].get("type") or "")
        # Fall back to any property that carries the 完成未發布 option.
        for name, spec in props.items():
            ptype = str(spec.get("type") or "")
            options = (spec.get(ptype) or {}).get("options") if ptype else None
            for option in options or []:
                if str(option.get("name")) == FINISHED_UNPUBLISHED:
                    return name, ptype
        raise NotionError(
            f"No publish column found in database {db_id}; "
            f"properties: {sorted(props)}")

    # ---- rows --------------------------------------------------------
    def fetch_pending_rows(self, db_id: str) -> list[dict]:
        """Pages whose publish column equals 完成未發布."""
        name, ptype = self.publish_property(db_id)
        condition = {
            "property": name,
            "select": {"equals": FINISHED_UNPUBLISHED},
        }
        if ptype == "status":
            condition = {"property": name, "status": {"equals": FINISHED_UNPUBLISHED}}
        elif ptype == "rich_text":
            condition = {"property": name, "rich_text": {"equals": FINISHED_UNPUBLISHED}}

        rows: list[dict] = []
        cursor: str | None = None
        while True:
            body: dict = {"filter": condition, "page_size": 50}
            if cursor:
                body["start_cursor"] = cursor
            page = self._request("POST", f"/databases/{db_id}/query", body)
            for result in page.get("results", []):
                rows.append({
                    "page_id": result.get("id"),
                    "title": _extract_title(result.get("properties") or {}),
                    "content": _extract_body(result.get("properties") or {}),
                    "publish_status": FINISHED_UNPUBLISHED,
                    "url": result.get("url"),
                })
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
        return rows

    def mark_published(self, page_id: str, db_id: str | None = None) -> dict:
        """Set the publish column to 完成已發布."""
        if db_id is None:
            page = self._request("GET", f"/pages/{page_id}")
            parent = page.get("parent") or {}
            db_id = parent.get("database_id")
            if not db_id:
                raise NotionError(f"page {page_id} has no database parent")
        name, ptype = self.publish_property(db_id)
        if ptype == "status":
            value = {"status": {"name": FINISHED_PUBLISHED}}
        elif ptype == "rich_text":
            value = {"rich_text": [{"text": {"content": FINISHED_PUBLISHED}}]}
        else:
            value = {"select": {"name": FINISHED_PUBLISHED}}
        return self._request("PATCH", f"/pages/{page_id}", {"properties": {name: value}})


def _extract_title(properties: dict) -> str:
    for spec in properties.values():
        if spec.get("type") == "title":
            return "".join(part.get("plain_text", "") for part in spec.get("title") or [])
    return ""


def _extract_body(properties: dict) -> str:
    """Best-effort long-form body: the first rich_text property that is not the
    publish column, else the page's title (page content is fetched separately
    by the CLI when needed)."""
    for name, spec in properties.items():
        if spec.get("type") == "rich_text" and name.lower() not in ("publish", "發布", "狀態"):
            text = "".join(part.get("plain_text", "") for part in spec.get("rich_text") or [])
            if text.strip():
                return text
    return ""
