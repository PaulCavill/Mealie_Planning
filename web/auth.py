"""Optional single-password login.

When APP_PASSWORD is set every page needs a session cookie, obtained from the
login form. The cookie is "<expiry>.<signature>": an HMAC over the expiry and
a hash of the password, keyed with a random secret stored in the planner
database - so sessions survive a restart and end when the password changes.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from contextlib import closing
from urllib.parse import urlsplit

from fastapi import Request

from core import db

COOKIE = "planner_session"
SESSION_SECONDS = 30 * 24 * 3600
OPEN_PATHS = ("/healthz", "/login", "/favicon.ico")
OPEN_PREFIXES = ("/static/",)

_fallback_secret = secrets.token_bytes(32)  # used only if the database can't be written


def password() -> str:
    return os.environ.get("APP_PASSWORD") or ""


def is_open_path(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def _secret() -> bytes:
    try:
        with closing(db.connect()) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS web_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = conn.execute("SELECT value FROM web_meta WHERE key = 'session_secret'").fetchone()
            if row:
                return bytes.fromhex(row["value"])
            secret = secrets.token_bytes(32)
            conn.execute("INSERT INTO web_meta (key, value) VALUES ('session_secret', ?)", (secret.hex(),))
            return secret
    except Exception:
        return _fallback_secret


def _sign(expiry: str) -> str:
    message = f"{expiry}|{hashlib.sha256(password().encode()).hexdigest()}".encode()
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def check_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode(), password().encode())


def new_session() -> str:
    expiry = str(int(time.time()) + SESSION_SECONDS)
    return f"{expiry}.{_sign(expiry)}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expiry, _, signature = token.partition(".")
    if not expiry.isdigit() or int(expiry) < time.time():
        return False
    return hmac.compare_digest(signature, _sign(expiry))


def logged_in(request: Request) -> bool:
    return valid_session(request.cookies.get(COOKIE))


def same_origin(request: Request) -> bool:
    """True if a state-changing request came from one of our own pages: its
    Origin (or, failing that, Referer) names the host the browser asked for."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return False
    try:
        netloc = urlsplit(source).netloc.lower()
    except ValueError:
        return False
    hosts = {request.headers.get("host", "").lower()}
    forwarded = request.headers.get("x-forwarded-host", "")
    hosts.update(h.strip().lower() for h in forwarded.split(",") if h.strip())
    hosts.discard("")
    return bool(netloc) and netloc in hosts


def safe_next(target: str | None) -> str:
    """Only ever redirect to a path on this site."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"
