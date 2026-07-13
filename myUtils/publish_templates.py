"""Publish Center template persistence.

A ``PublishTemplate`` is a saved bundle of Publish Center settings — the
target profiles/accounts, processing options (watermark, intro, outro,
link-in-first-comment, screenshots), and an optional default schedule —
that a user can load back to skip re-entering everything.

The ``included_settings`` list controls which fields the template
actually overrides when loaded; everything else is left at the page's
current value. This lets a single template fix only the bits the user
cares about (e.g. "always use these two profiles, leave watermark
toggle alone").
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from utils.conf_defaults import BASE_DIR


DB_PATH = Path(BASE_DIR) / "db" / "database.db"

ALL_SETTINGS: tuple[str, ...] = (
    "profileIds",
    "accountIds",
    "watermark",
    "intro",
    "outro",
    "linkInFirstComment",
    "screenshots",
    "schedule",
)

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    cleaned = _SLUG_PATTERN.sub("-", value.strip().lower()).strip("-")
    if not cleaned:
        raise ValueError(f"Cannot derive slug from value: {value!r}")
    return cleaned


@dataclass(slots=True)
class PublishTemplate:
    id: int
    name: str
    slug: str
    description: str = ""
    config: dict | None = None
    included_settings: list[str] | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _resolve_db_path(db_path: Path | None) -> Path:
    return db_path if db_path is not None else DB_PATH


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    resolved = _resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _row_to_template(row: sqlite3.Row) -> PublishTemplate:
    return PublishTemplate(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        description=row["description"] or "",
        config=json.loads(row["config_json"] or "{}"),
        included_settings=json.loads(row["included_settings_json"] or "[]"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _ensure_slug_unique(slug: str, *, db_path: Path | None, exclude_id: int | None = None) -> str:
    candidate = slug
    suffix = 2
    with _connect(db_path) as conn:
        while True:
            row = conn.execute(
                "SELECT id FROM publish_templates WHERE slug = ?",
                (candidate,),
            ).fetchone()
            if row is None or (exclude_id is not None and row["id"] == exclude_id):
                return candidate
            candidate = f"{slug}-{suffix}"
            suffix += 1


def list_templates(
    *, workspace_id: str | None = None, db_path: Path | None = None
) -> list[PublishTemplate]:
    with _connect(db_path) as conn:
        if workspace_id is not None:
            rows = conn.execute(
                "SELECT * FROM publish_templates WHERE workspace_id = ? "
                "ORDER BY updated_at DESC, id DESC",
                (workspace_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM publish_templates ORDER BY updated_at DESC, id DESC"
            ).fetchall()
    return [_row_to_template(row) for row in rows]


def get_template(
    template_id: int, *, workspace_id: str | None = None, db_path: Path | None = None
) -> PublishTemplate:
    """Fetch a template. When ``workspace_id`` is given, a template owned by
    another workspace is treated as not found (tenant isolation)."""
    with _connect(db_path) as conn:
        if workspace_id is not None:
            row = conn.execute(
                "SELECT * FROM publish_templates WHERE id = ? AND workspace_id = ?",
                (template_id, workspace_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM publish_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
    if row is None:
        raise LookupError(f"Publish template not found: id={template_id}")
    return _row_to_template(row)


def create_template(
    *,
    name: str,
    description: str = "",
    config: dict | None = None,
    included_settings: list[str] | None = None,
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> PublishTemplate:
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValueError("Template name is required")
    # Slug uniqueness stays global: the DB enforces a UNIQUE(slug) constraint
    # today. The per-workspace composite unique is a later "constrain" step.
    slug = _ensure_slug_unique(slugify(cleaned_name), db_path=db_path)
    settings = [str(item) for item in (included_settings or list(ALL_SETTINGS)) if item]
    with _connect(db_path) as conn:
        if workspace_id is not None:
            cursor = conn.execute(
                """
                INSERT INTO publish_templates (
                    name, slug, description, config_json, included_settings_json,
                    workspace_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cleaned_name,
                    slug,
                    (description or "").strip(),
                    json.dumps(config or {}, ensure_ascii=False),
                    json.dumps(settings, ensure_ascii=False),
                    workspace_id,
                ),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO publish_templates (
                    name, slug, description, config_json, included_settings_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    cleaned_name,
                    slug,
                    (description or "").strip(),
                    json.dumps(config or {}, ensure_ascii=False),
                    json.dumps(settings, ensure_ascii=False),
                ),
            )
        conn.commit()
        template_id = cursor.lastrowid
    return get_template(template_id, workspace_id=workspace_id, db_path=db_path)


_UNSET = object()


def update_template(
    template_id: int,
    *,
    name: str | object = _UNSET,
    description: str | object = _UNSET,
    config: dict | None | object = _UNSET,
    included_settings: list[str] | None | object = _UNSET,
    db_path: Path | None = None,
) -> PublishTemplate:
    current = get_template(template_id, db_path=db_path)
    next_name = current.name if name is _UNSET else (str(name or "").strip() or current.name)
    next_slug = current.slug
    if name is not _UNSET and next_name != current.name:
        next_slug = _ensure_slug_unique(
            slugify(next_name), db_path=db_path, exclude_id=template_id
        )
    next_description = (
        current.description
        if description is _UNSET
        else (description or "").strip()
    )
    next_config = current.config if config is _UNSET else (config or {})
    next_included = (
        current.included_settings
        if included_settings is _UNSET
        else [str(item) for item in (included_settings or []) if item]
    )
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE publish_templates
            SET name = ?, slug = ?, description = ?,
                config_json = ?, included_settings_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_name,
                next_slug,
                next_description,
                json.dumps(next_config or {}, ensure_ascii=False),
                json.dumps(next_included or [], ensure_ascii=False),
                _now_iso(),
                template_id,
            ),
        )
        conn.commit()
    return get_template(template_id, db_path=db_path)


def delete_template(template_id: int, *, db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM publish_templates WHERE id = ?", (template_id,)
        )
        conn.commit()
