"""SQLite persistence for export registrations and snapshot retirement."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot_retirements (
    snapshot_id TEXT PRIMARY KEY,
    retired_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS export_registrations (
    registration_id TEXT PRIMARY KEY,
    snapshot_id     TEXT NOT NULL,
    export_id       TEXT,
    files_json      TEXT NOT NULL,
    registered_at   TEXT NOT NULL,
    released_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_export_active
    ON export_registrations (released_at, snapshot_id);
"""

_initialized_paths: set[str] = set()
_init_lock = threading.Lock()


def _path_key(path: Path) -> str:
    return str(path.expanduser().resolve())


def ensure_db(path: Path) -> None:
    """Create the database and schema once per process."""

    key = _path_key(path)
    with _init_lock:
        if key in _initialized_paths:
            return
        path = path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
        finally:
            connection.close()
        _initialized_paths.add(key)


def reset_initialization_cache() -> None:
    """Testing helper: forget which database files have been initialized."""

    with _init_lock:
        _initialized_paths.clear()


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    ensure_db(path)
    connection = sqlite3.connect(
        str(path.expanduser()), timeout=30, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        yield connection
    finally:
        connection.close()


def begin_immediate(connection: sqlite3.Connection) -> None:
    """Acquire SQLite's write lock before the decision + write.

    Registration, retirement, and cleanup all use this.  SQLite serializes
    writers, so a registration either:
      * commits before retirement and is visible/protected to every later
        cleanup, or
      * starts after retirement has committed and is rejected.
    There is no window in which a "successful" registration can be missed by
    the cleanup planner.
    """

    connection.execute("BEGIN IMMEDIATE")
