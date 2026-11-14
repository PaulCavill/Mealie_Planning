"""SQLite storage at $DATA_DIR/planner.db.

DATA_DIR defaults to the `data` folder next to the code, not to the current
directory, so the CLI finds the same settings wherever it is run from.

Each module creates the tables it owns with CREATE TABLE IF NOT EXISTS;
`settings` belongs to core.settings.
"""
import os
import sqlite3
from pathlib import Path

DB_FILENAME = "planner.db"


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parent.parent / "data")


def db_path() -> Path:
    return data_dir() / DB_FILENAME


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the planner database, creating the directory and file if needed.

    Open one connection per unit of work and close it afterwards; connections
    are not shared between threads.
    """
    target = Path(path) if path else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def storage_problem(path: str | Path | None = None) -> str | None:
    """None if the planner database can be opened and written; otherwise a
    sentence saying what is wrong, naming the folder and the user the process
    runs as (the usual cause is a Docker volume owned by somebody else)."""
    target = Path(path) if path else db_path()
    try:
        folder = target.parent.resolve()
    except OSError:
        folder = target.parent
    try:
        conn = connect(target)
        try:
            # A read-only file or folder only fails once something is written.
            conn.execute("CREATE TABLE IF NOT EXISTS storage_check (id INTEGER PRIMARY KEY, checked_at TEXT)")
            conn.execute("INSERT OR REPLACE INTO storage_check (id, checked_at) VALUES (1, datetime('now'))")
            conn.commit()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as e:
        who = f"user id {os.getuid()}, group id {os.getgid()}" if hasattr(os, "getuid") else "the current user"
        return (f"The data folder {folder} cannot be written to by this app, which runs as {who} "
                f"({type(e).__name__}: {e}). Settings, plan previews and job history are stored there.")
    return None
