"""Alembic environment for social-auto-upload.

The project doesn't use SQLAlchemy models, so this file is much simpler
than what ``alembic init`` would generate: each version script issues raw
SQL via ``op.execute`` and we just hand Alembic an Engine pointed at the
right SQLite file.

The DB URL is resolved with the following precedence:

1. ``-x url=<sqlalchemy-url>`` on the alembic command line
2. ``SAU_DB_URL`` environment variable
3. The ``sqlalchemy.url`` value from ``alembic.ini``

That precedence lets tests run migrations against a per-test temp file by
exporting ``SAU_DB_URL`` without disturbing the canonical config.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No declarative metadata: this project is pre-ORM. Alembic still happily
# runs raw-SQL migrations with target_metadata=None.
target_metadata = None


def _resolve_url() -> str:
    cli_args = context.get_x_argument(as_dictionary=True)
    if "url" in cli_args:
        return cli_args["url"]
    env_url = os.environ.get("SAU_DB_URL")
    if env_url:
        return env_url
    ini_url = config.get_main_option("sqlalchemy.url")
    if not ini_url:
        raise RuntimeError(
            "No SQLAlchemy URL configured. Set sqlalchemy.url in alembic.ini, "
            "export SAU_DB_URL, or pass `-x url=...` on the command line."
        )
    return ini_url


def run_migrations_offline() -> None:
    """Render SQL without connecting to a live database."""

    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite-friendly ALTER TABLE
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live SQLite file."""

    url = _resolve_url()
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
