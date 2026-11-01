"""Consistent SQLite connections for legacy services.

The stdlib connection context commits/rolls back but does not close the handle.
This subclass also closes it, avoiding leaked read locks and Windows file locks.
"""
from __future__ import annotations
import sqlite3


class Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(database, timeout: float = 15.0, **kwargs) -> sqlite3.Connection:
    kwargs.setdefault("factory", Connection)
    conn = sqlite3.connect(database, timeout=timeout, **kwargs)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
    except Exception:
        conn.close()
        raise
    return conn
