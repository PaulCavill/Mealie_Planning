"""SQLite storage at $DATA_DIR/planner.db (DATA_DIR defaults to ./data).

Each module creates the tables it owns with CREATE TABLE IF NOT EXISTS;
`settings` belongs to core.settings.
"""
import os
import sqlite3
from pathlib import Path

DB_FILENAME = "planner.db"


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or "./data")


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
