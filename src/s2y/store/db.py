"""SQLite connection setup and schema application."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 2


def _load_schema() -> str:
    # importlib.resources works both from source and from an installed wheel.
    return resources.files("s2y.store").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the s2y database and ensure the schema exists.

    Returns a connection with ``Row`` factory, foreign keys enforced, and WAL
    journaling. Pass ``":memory:"`` for tests.
    """
    is_memory = str(path) == ":memory:"
    if not is_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False: the engine connection is created on the UI thread
    # but used inside the worker's daemon thread (see s2y.tui.threads). Python's
    # sqlite3 is built serialized (threadsafety=3), so a single connection is safe
    # across threads as long as we don't use it concurrently — which we don't (one
    # worker at a time touches a given connection).
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not is_memory:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    conn.executescript(_load_schema())
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn
