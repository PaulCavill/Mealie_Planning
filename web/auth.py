"""Optional single-password login.

When APP_PASSWORD is set every page needs a session cookie, obtained from the
login form. The cookie is "<expiry>.<signature>": an HMAC over the expiry and
a hash of the password, keyed with a random secret stored in the planner
database - so sessions survive a restart and end when the password changes.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
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

def password() -> str:
    return os.environ.get("APP_PASSWORD") or ""


def is_open_path(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


_secrets: dict[str, bytes] = {}  # per database file; read once, then kept in memory


def _secret() -> bytes:
    """The signing secret, created on first use. If the database can't be
    written this raises: the app then shows its storage notice instead of
    quietly issuing sessions that would die with the process."""
    key = str(db.db_path())
    if key not in _secrets:
        _secrets[key] = _load_secret()
    return _secrets[key]


def _load_secret() -> bytes:
    with closing(db.connect()) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS web_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Two first requests at once: only one insert wins, and both read that one back.
        conn.execute("INSERT OR IGNORE INTO web_meta (key, value) VALUES ('session_secret', ?)",
                     (secrets.token_bytes(32).hex(),))
        row = conn.execute("SELECT value FROM web_meta WHERE key = 'session_secret'").fetchone()
    return bytes.fromhex(row["value"])


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
    # isascii: str.isdigit() also accepts characters such as '²' that int() refuses.
    if not (expiry.isascii() and expiry.isdigit() and len(expiry) <= 12) or int(expiry) < time.time():
        return False
    return hmac.compare_digest(signature.encode(), _sign(expiry).encode())


def logged_in(request: Request) -> bool:
    return valid_session(request.cookies.get(COOKIE))


def _source_host(request: Request) -> str | None:
    """Host of the page a request came from, per its Origin (or, failing that,
    Referer) header. None if the request carries neither; "" if the header is
    there but names no usable host (e.g. `Origin: null`)."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return None
    try:
        return urlsplit(source).netloc.lower()
    except ValueError:
        return ""


def _own_hosts(request: Request) -> set[str]:
    hosts = {request.headers.get("host", "").lower()}
    forwarded = request.headers.get("x-forwarded-host", "")
    hosts.update(h.strip().lower() for h in forwarded.split(",") if h.strip())
    hosts.discard("")
    return hosts


def same_origin(request: Request) -> bool:
    """True if a state-changing request came from one of our own pages: its
    Origin (or, failing that, Referer) names the host the browser asked for."""
    source = _source_host(request)
    return bool(source) and source in _own_hosts(request)


def cross_origin(request: Request) -> bool:
    """True if a request says it came from somewhere else: an Origin/Referer
    that is not this app's host, or a browser's `Sec-Fetch-Site: cross-site`.
    A request with none of these headers (curl, a script) is not a browser
    being tricked by another site, so it is not cross-origin."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return True
    source = _source_host(request)
    return source is not None and source not in _own_hosts(request)


_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain", ".localhost")


def _hostname(host_header: str) -> str:
    """The name part of a Host header: no port, no IPv6 brackets, lower case."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if ":" in host else host


def allowed_hosts() -> set[str]:
    """Extra names from ALLOWED_HOSTS (comma separated) this app may be reached by."""
    return {_hostname(h) for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}


def trusted_host(request: Request) -> bool:
    """True if the name the browser used for this app can only be a local one.

    Without a login, the only thing separating another website's script from
    this app is the browser's same-origin rule, and DNS rebinding gets round
    it: a public domain is re-pointed at this machine's address, after which
    its page and this app share an origin. Such a request still carries the
    attacker's domain in Host, so it is refused unless the name is an IP
    address, a bare or local-only name, or listed in ALLOWED_HOSTS. With a
    login the attacker's origin has no session cookie, so this is not needed.
    """
    name = _hostname(request.headers.get("host", ""))
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if "." not in name or name == "localhost" or name.endswith(_LOCAL_SUFFIXES):
        return True
    extra = allowed_hosts()
    return "*" in extra or name in extra


def safe_next(target: str | None) -> str:
    """Only ever redirect to a path on this site."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"
