"""A small in-process background job runner.

Each job runs on its own thread and is recorded in the planner database
(table web_jobs) so its progress and result can be polled by the browser and
shown in the job history after a restart. Only one job of a kind runs at a
time. A job still marked queued/running with no thread behind it was cut off
(by a restart, or because its ending could not be written) and is marked
failed the next time jobs are looked at.

A job function takes a JobContext and returns JSON-serialisable data:

    def work(ctx):
        ctx.log("starting")
        ctx.progress(1, 10)
        return {"answer": 42}

    job_id = submit("parse", "Parse all ingredients", work)
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Callable

from core import db
from . import deps

log = logging.getLogger("web.jobs")

ACTIVE = ("queued", "running")
KEEP_JOBS = 60

_SCHEMA = """CREATE TABLE IF NOT EXISTS web_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    params TEXT,
    log TEXT NOT NULL DEFAULT '',
    progress_done INTEGER,
    progress_total INTEGER,
    result TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
)"""

_FINISH_ATTEMPTS = 5
_FINISH_RETRY_SECONDS = 0.5
_INTERRUPTED = "Interrupted: the app restarted, or could not record how this job ended."

_submit_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}


class JobBusy(Exception):
    """A job of this kind is already queued or running."""

    def __init__(self, job_id: str, kind: str):
        self.job_id = job_id
        self.kind = kind
        super().__init__(f"A {kind} job is already running.")


def _connect(path: Path | None = None):
    conn = db.connect(path)
    conn.execute(_SCHEMA)
    return conn


class JobContext:
    """Handed to the job function: its parameters, and ways to report back."""

    def __init__(self, job_id: str, params, path: Path):
        self.job_id = job_id
        self.params = params
        self._path = path

    def log(self, message: str) -> None:
        with closing(_connect(self._path)) as conn, conn:
            conn.execute("UPDATE web_jobs SET log = log || ? WHERE id = ?",
                         (str(message).rstrip("\n") + "\n", self.job_id))

    def progress(self, done: int, total: int | None = None) -> None:
        with closing(_connect(self._path)) as conn, conn:
            if total is None:
                conn.execute("UPDATE web_jobs SET progress_done = ? WHERE id = ?", (done, self.job_id))
            else:
                conn.execute("UPDATE web_jobs SET progress_done = ?, progress_total = ? WHERE id = ?",
                             (done, total, self.job_id))


def submit(kind: str, title: str, func: Callable[[JobContext], object], params=None) -> str:
    """Start `func` on a background thread and return the job id.

    Raises JobBusy (carrying the running job's id) if a job of the same kind
    is already queued or running.
    """
    path = db.db_path()  # fixed now, so the thread never follows a later DATA_DIR change
    job_id = secrets.token_hex(6)
    with _submit_lock:
        with closing(_connect(path)) as conn, conn:
            _reap(conn)
            row = conn.execute(
                "SELECT id FROM web_jobs WHERE kind = ? AND status IN ('queued', 'running') LIMIT 1", (kind,)
            ).fetchone()
            if row:
                raise JobBusy(row["id"], kind)
            conn.execute(
                "INSERT INTO web_jobs (id, kind, title, status, params, created_at) VALUES (?, ?, ?, 'queued', ?, ?)",
                (job_id, kind, title, json.dumps(params) if params is not None else None, time.time()),
            )
            conn.execute(
                "DELETE FROM web_jobs WHERE status NOT IN ('queued', 'running') AND id NOT IN "
                "(SELECT id FROM web_jobs ORDER BY created_at DESC LIMIT ?)", (KEEP_JOBS,),
            )
        thread = threading.Thread(target=_run, args=(job_id, func, params, path),
                                  name=f"job-{kind}-{job_id}", daemon=True)
        _threads[job_id] = thread
        thread.start()
    return job_id


def _run(job_id: str, func: Callable, params, path: Path) -> None:
    try:
        with closing(_connect(path)) as conn, conn:
            conn.execute("UPDATE web_jobs SET status = 'running', started_at = ? WHERE id = ?",
                         (time.time(), job_id))
        try:
            result = func(JobContext(job_id, params, path))
            payload = json.dumps(result, default=str)
        except Exception as exc:  # the job failed; record why, in plain words
            log.exception("job %s failed", job_id)
            _finish(path, job_id, "failed", error=deps.describe_error(exc))
        else:
            _finish(path, job_id, "done", result=payload)
    except Exception:  # the database itself is unavailable
        log.exception("job %s could not be recorded", job_id)
    finally:
        _threads.pop(job_id, None)


def _finish(path: Path, job_id: str, status: str, result: str | None = None, error: str | None = None) -> None:
    """Record how a job ended. A busy or briefly unavailable database is
    retried; if it still fails, _reap() clears the row once the thread is gone."""
    for attempt in range(_FINISH_ATTEMPTS):
        try:
            with closing(_connect(path)) as conn, conn:
                conn.execute("UPDATE web_jobs SET status = ?, result = ?, error = ?, finished_at = ? WHERE id = ?",
                             (status, result, error, time.time(), job_id))
            return
        except sqlite3.Error:
            if attempt == _FINISH_ATTEMPTS - 1:
                raise
            time.sleep(_FINISH_RETRY_SECONDS * (attempt + 1))


def _reap(conn) -> int:
    """Fail every job the database calls queued or running that has no thread
    in this process: its final state was never recorded (the app restarted, or
    the database could not be written at the time). Without this such a row
    would block every later job of its kind. Call with _submit_lock held, so a
    job being submitted right now is not mistaken for a dead one."""
    rows = conn.execute("SELECT id FROM web_jobs WHERE status IN ('queued', 'running')").fetchall()
    dead = [row["id"] for row in rows if row["id"] not in _threads]
    for job_id in dead:
        conn.execute("UPDATE web_jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                     (time.time(), _INTERRUPTED, job_id))
    return len(dead)


def _job(row, with_payload: bool = True) -> dict:
    job = {key: row[key] for key in ("id", "kind", "title", "status", "error", "progress_done",
                                     "progress_total", "created_at", "started_at", "finished_at")}
    job["active"] = job["status"] in ACTIVE
    start = job["started_at"]
    job["duration"] = ((job["finished_at"] or time.time()) - start) if start else None
    if with_payload:
        job["log"] = row["log"].splitlines()
        job["params"] = json.loads(row["params"]) if row["params"] else None
        job["result"] = json.loads(row["result"]) if row["result"] else None
    return job


def _reap_now() -> None:
    """Best effort before a read, so a dead job is not shown as running."""
    try:
        with _submit_lock, closing(_connect()) as conn, conn:
            _reap(conn)
    except sqlite3.Error:
        log.warning("could not check for interrupted jobs", exc_info=True)


def get(job_id: str) -> dict | None:
    if job_id not in _threads:
        _reap_now()
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM web_jobs WHERE id = ?", (job_id,)).fetchone()
    return _job(row) if row else None


def recent(limit: int = 25, kind: str | None = None) -> list[dict]:
    """Newest first, without logs and results."""
    query = ("SELECT id, kind, title, status, error, progress_done, progress_total, created_at, "
             "started_at, finished_at FROM web_jobs")
    args: tuple = ()
    if kind:
        query += " WHERE kind = ?"
        args = (kind,)
    with closing(_connect()) as conn:
        rows = conn.execute(query + " ORDER BY created_at DESC LIMIT ?", args + (limit,)).fetchall()
    return [_job(row, with_payload=False) for row in rows]


def active(kind: str) -> dict | None:
    _reap_now()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM web_jobs WHERE kind = ? AND status IN ('queued', 'running') LIMIT 1", (kind,)
        ).fetchone()
    return _job(row, with_payload=False) if row else None


def mark_interrupted() -> int:
    """Call once at startup. Returns how many jobs were marked failed."""
    with _submit_lock, closing(_connect()) as conn, conn:
        return _reap(conn)


def wait(job_id: str, timeout: float = 30) -> dict | None:
    """Block until the job finishes (for tests). Returns the job record."""
    thread = _threads.get(job_id)
    if thread:
        thread.join(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = get(job_id)
        if job is None or not job["active"]:
            return job
        time.sleep(0.02)
    return get(job_id)
