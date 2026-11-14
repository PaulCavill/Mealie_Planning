"""Plan previews, kept server-side between requests.

A preview (the dict from core.planning.preview_plan) is stored as JSON in the
planner database under a short random id that appears in the URL. Status moves
draft -> saving -> saved; a saved row keeps commit_plan's result so the
confirmation page survives a refresh.
"""
from __future__ import annotations

import json
import secrets
import time
from contextlib import closing

from core import db

KEEP_SECONDS = 14 * 24 * 3600

_SCHEMA = """CREATE TABLE IF NOT EXISTS web_plan_previews (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    shopping_list INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    result TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)"""


def _connect():
    conn = db.connect()
    conn.execute(_SCHEMA)
    return conn


def _record(row) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "data": json.loads(row["data"]),
        "shopping_list": bool(row["shopping_list"]),
        "status": row["status"],
        "result": json.loads(row["result"]) if row["result"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create(preview: dict, shopping_list: bool = False) -> str:
    preview_id = secrets.token_urlsafe(9)
    now = time.time()
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM web_plan_previews WHERE updated_at < ?", (now - KEEP_SECONDS,))
        conn.execute(
            "INSERT INTO web_plan_previews (id, data, shopping_list, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (preview_id, json.dumps(preview), int(shopping_list), now, now),
        )
    return preview_id


def get(preview_id: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM web_plan_previews WHERE id = ?", (preview_id,)).fetchone()
    return _record(row)


def latest_draft() -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM web_plan_previews WHERE status = 'draft' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return _record(row)


def update(preview_id: str, preview: dict) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE web_plan_previews SET data = ?, updated_at = ? WHERE id = ? AND status = 'draft'",
            (json.dumps(preview), time.time(), preview_id),
        )


def claim(preview_id: str) -> bool:
    """Mark a draft as being saved. False if it is not a draft (any more), so
    a double-clicked Save can't commit the plan twice."""
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "UPDATE web_plan_previews SET status = 'saving', updated_at = ? WHERE id = ? AND status = 'draft'",
            (time.time(), preview_id),
        )
        return cursor.rowcount == 1


def release(preview_id: str) -> None:
    """Saving failed: make the preview a draft again."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE web_plan_previews SET status = 'draft' WHERE id = ? AND status = 'saving'",
                     (preview_id,))


def finish(preview_id: str, result: dict, shopping_list: bool) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE web_plan_previews SET status = 'saved', result = ?, shopping_list = ?, updated_at = ? WHERE id = ?",
            (json.dumps(result), int(shopping_list), time.time(), preview_id),
        )


def delete(preview_id: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM web_plan_previews WHERE id = ?", (preview_id,))


def reset_stuck() -> None:
    """At startup: a preview left 'saving' by a crash becomes a draft again."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE web_plan_previews SET status = 'draft' WHERE status = 'saving'")
