"""Shared plumbing for the web UI: configuration, Mealie clients, templates
and turning exceptions into messages a person can act on."""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import requests
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core import db
from core.errors import CoreError
from core.settings import Settings, WEEKDAY_NAMES, load_settings
from mealie_client import MealieClient

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
STATIC_VERSION = str(int(time.time()))  # cache-buster for our own CSS/JS

CONNECT_TIMEOUT = 5  # seconds; fail fast when Mealie is down

# Tests (or a future offline mode) can set this to a zero-argument callable
# returning an object with the anthropic.Anthropic interface.
ai_client_factory: Callable | None = None

EFFORT_LABELS = {1: "very quick", 2: "easy", 3: "moderate", 4: "involved", 5: "time-intensive"}

NAV = [
    ("plan", "/", "Plan"),
    ("recipes", "/recipes", "Recipes"),
    ("generate", "/generate", "Generate"),
    ("images", "/images", "Images"),
    ("maintenance", "/maintenance", "Maintenance"),
    ("settings", "/settings", "Settings"),
]


class ConfigMissing(Exception):
    """MEALIE_BASE_URL / MEALIE_TOKEN are not set."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("Missing configuration: " + ", ".join(missing))


# --- configuration -----------------------------------------------------------

def env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def missing_config() -> list[str]:
    """Names of the required environment variables that are not set."""
    missing = []
    if not env("MEALIE_BASE_URL"):
        missing.append("MEALIE_BASE_URL")
    if not env("MEALIE_TOKEN") and not (env("MEALIE_USERNAME") and env("MEALIE_PASSWORD")):
        missing.append("MEALIE_TOKEN")
    return missing


def mealie_url() -> str:
    """MEALIE_BASE_URL, safe to display (any user:password@ part removed)."""
    raw = env("MEALIE_BASE_URL").rstrip("/")
    try:
        parts = urlsplit(raw)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host += f":{parts.port}"
            return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    except ValueError:
        pass
    return raw


def anthropic_ready() -> bool:
    return bool(env("ANTHROPIC_API_KEY")) or ai_client_factory is not None


def auth_enabled() -> bool:
    return bool(os.environ.get("APP_PASSWORD"))


# --- storage -----------------------------------------------------------------

STORAGE_FIX = ("Make that folder writable by that user (for Docker: `chown` the host folder or dataset mounted "
               "at /data to that user id, or set PUID and PGID to the folder's owner and recreate the container). "
               "The app carries on by itself once the folder is writable.")

_storage_ok_for: str | None = None  # the database path that last passed the check


def storage_problem(recheck: bool = False) -> str | None:
    """Why the planner database can't be used, or None when it can.

    A passing check is remembered (per database path), so this costs nothing
    on ordinary requests; a failing one is re-tested every time, so the notice
    clears as soon as the folder is fixed.
    """
    global _storage_ok_for
    path = str(db.db_path())
    if _storage_ok_for == path and not recheck:
        return None
    problem = db.storage_problem()
    _storage_ok_for = None if problem else path
    return problem


def get_client(read_timeout: float | None = None) -> MealieClient:
    """A fresh MealieClient. Make one per request or per background job; they
    are cheap and not safe to share between threads."""
    missing = missing_config()
    if missing:
        raise ConfigMissing(missing)
    read = read_timeout or float(os.environ.get("MEALIE_TIMEOUT") or 60)
    return MealieClient(timeout=(CONNECT_TIMEOUT, read))


def get_settings() -> Settings:
    return load_settings()


# --- errors ------------------------------------------------------------------

def small_int(value) -> int | None:
    """A non-negative whole number from form or query text, or None.
    `str.isdigit()` alone accepts characters such as '²' that int() refuses."""
    text = str(value or "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 9 else None


def describe_error(exc: BaseException) -> str:
    """A message for the person using the app; never a stack trace."""
    if isinstance(exc, ConfigMissing):
        return f"The app is not connected to Mealie yet. Set {' and '.join(exc.missing)} and restart."
    if isinstance(exc, CoreError):
        return str(exc)
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status in (401, 403):
            return (f"Mealie rejected the API token ({status}). Create a new token in Mealie under "
                    "Profile → API Tokens, set it as MEALIE_TOKEN and restart the app.")
        if status == 404:
            return "Mealie could not find that item (404). It may have been renamed or deleted."
        detail = _response_detail(exc.response)
        return f"Mealie returned an error ({status})." + (f" {detail}" if detail else "")
    if isinstance(exc, requests.Timeout):
        return f"Mealie at {mealie_url()} took too long to answer. Try again in a moment."
    if isinstance(exc, requests.RequestException):
        return (f"Could not reach Mealie at {mealie_url()}. Check that Mealie is running and that "
                "MEALIE_BASE_URL is correct.")
    return f"Something went wrong ({type(exc).__name__}). The details are in the app's log."


def _response_detail(response: requests.Response) -> str:
    try:
        detail = response.json().get("detail")
    except Exception:
        return ""
    if isinstance(detail, dict):
        detail = detail.get("message")
    return str(detail)[:200] if isinstance(detail, str) and detail else ""


# --- templates ---------------------------------------------------------------

def _context(request: Request) -> dict:
    return {
        "nav": NAV,
        "auth_enabled": auth_enabled(),
        "config_missing": missing_config(),
        "static_v": STATIC_VERSION,
    }


templates = Jinja2Templates(directory=str(WEB_DIR / "templates"), context_processors=[_context])


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _day_short(value) -> str:
    d = _as_date(value)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _day_long(value) -> str:
    d = _as_date(value)
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"


def _clock(timestamp) -> str:
    if not timestamp:
        return "—"
    moment = datetime.fromtimestamp(float(timestamp))
    return f"{moment.day} {moment.strftime('%b')}, {moment.strftime('%H:%M')}"


def _elapsed(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s"


_ISO_DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?$", re.I)


def _cook_time(value) -> str:
    """'PT1H15M' -> '1 h 15 min'. Anything else is shown as given."""
    match = _ISO_DURATION.match(str(value or "").strip())
    if not match or not (match[1] or match[2]):
        return str(value or "")
    parts = []
    if match[1]:
        parts.append(f"{int(match[1])} h")
    if match[2]:
        parts.append(f"{int(match[2])} min")
    return " ".join(parts)


def thumb_url(recipe_id, version=None, size: str = "min") -> str:
    """URL of a recipe image, proxied through this app."""
    if not recipe_id:
        return f"/static/placeholder.svg?v={STATIC_VERSION}"
    url = f"/media/recipe/{recipe_id}"
    params = []
    if size != "min":
        params.append(f"size={size}")
    if version:
        params.append(f"v={version}")
    return url + ("?" + "&".join(params) if params else "")


templates.env.filters.update(day_short=_day_short, day_long=_day_long, clock=_clock,
                             elapsed=_elapsed, cook_time=_cook_time)
# Globals (unlike context values) are also visible inside imported macros.
templates.env.globals.update(thumb_url=thumb_url, effort_labels=EFFORT_LABELS, weekday_names=WEEKDAY_NAMES)


def is_htmx(request: Request) -> bool:
    """True for a fragment request; False when HTMX is restoring a whole page
    from history (it then needs the full document)."""
    headers = request.headers
    return headers.get("hx-request") == "true" and headers.get("hx-history-restore-request") != "true"


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200,
           headers: dict | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code, headers=headers)


def toast(request: Request, message: str, kind: str = "error") -> HTMLResponse:
    """An HTMX response that shows `message` in the page's toast area and
    leaves whatever the request was aimed at untouched."""
    return render(request, "_toast.html", {"message": message, "kind": kind},
                  headers={"HX-Retarget": "#toast", "HX-Reswap": "innerHTML"})
