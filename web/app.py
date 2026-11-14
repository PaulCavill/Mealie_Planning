"""The web app: `uvicorn web.app:app --host 0.0.0.0 --port 8080`.

Environment: MEALIE_BASE_URL, MEALIE_TOKEN (required), ANTHROPIC_API_KEY
(recipe generation), APP_PASSWORD (turns the login on), DATA_DIR (SQLite file
with settings, plan previews and job history; must be writable).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

import requests
from fastapi import FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from env_loader import load_dotenv

load_dotenv()  # local runs; in Docker the variables come from the container environment

from core import db  # noqa: E402
from core.errors import CoreError  # noqa: E402
from . import auth, deps, jobs, previews  # noqa: E402
from .routes import generate, images, maintenance, plan, recipes, settings  # noqa: E402

log = logging.getLogger("web")

PLACEHOLDER = deps.STATIC_DIR / "placeholder.svg"
_RECIPE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_IMAGE_FILES = {"min": "min-original.webp", "tiny": "tiny-original.webp", "original": "original.webp"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    problem = deps.storage_problem(recheck=True)
    if problem:
        log.error("%s %s", problem, deps.STORAGE_FIX)
    else:
        _tidy_after_start()
    yield


_tidied: set[str] = set()  # database files whose leftovers from a previous run are cleared


def _tidy_after_start() -> None:
    """Once per database file, as soon as it is usable: fail jobs and release
    plan previews that a previous run left half-done. Normally that is at
    startup; if the data folder was not writable then, it is the first
    request after it has been fixed."""
    key = str(db.db_path())
    if key in _tidied:
        return
    _tidied.add(key)
    try:
        interrupted = jobs.mark_interrupted()
        previews.reset_stuck()
    except Exception:
        _tidied.discard(key)
        log.warning("could not clear leftovers from the previous run", exc_info=True)
        return
    if interrupted:
        log.warning("%d background job(s) were interrupted by the restart", interrupted)


app = FastAPI(title="Mealie Planner", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(deps.STATIC_DIR)), name="static")
for module in (plan, recipes, generate, images, maintenance, settings):
    app.include_router(module.router)


# --- login and CSRF ------------------------------------------------------------

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _storage_notice(request: Request, problem: str):
    if deps.is_htmx(request):
        return deps.toast(request, f"{problem} {deps.STORAGE_FIX}")
    return deps.render(request, "storage.html", {"active": None, "problem": problem, "fix": deps.STORAGE_FIX},
                       status_code=503)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if not deps.auth_enabled() and path != "/healthz" and not auth.trusted_host(request):
        host = request.headers.get("host", "")
        return _problem(request, f"This app does not answer to the name '{host}'. Open it by its IP address or "
                                 "local name, or add that name to ALLOWED_HOSTS (or set APP_PASSWORD) and restart.",
                        403)
    # Cross-site request forgery: checked whether or not a login is required,
    # since a page on another site can reach this app through the user's browser.
    if request.method not in _SAFE_METHODS:
        if auth.cross_origin(request) or (deps.auth_enabled() and not auth.same_origin(request)):
            return _problem(request, "That request did not come from this app's own page, so it was refused. "
                                     "Reload the page and try again.", 403)
    if not auth.is_open_path(path) or path == "/login":
        problem = deps.storage_problem()
        if problem:
            return _storage_notice(request, problem)
        _tidy_after_start()
    if not deps.auth_enabled() or request.method == "OPTIONS":
        return await call_next(request)
    if auth.is_open_path(path) or auth.logged_in(request):
        return await call_next(request)
    if deps.is_htmx(request):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    target = path + (f"?{request.url.query}" if request.url.query else "")
    suffix = f"?next={quote(target, safe='')}" if request.method == "GET" and target != "/" else ""
    return RedirectResponse(f"/login{suffix}", status_code=303)


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    if not deps.auth_enabled() or auth.logged_in(request):
        return RedirectResponse(auth.safe_next(next), status_code=303)
    return deps.render(request, "login.html", {"next": auth.safe_next(next), "error": None})


@app.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/")):
    target = auth.safe_next(next)
    if not deps.auth_enabled():
        return RedirectResponse(target, status_code=303)
    if not auth.check_password(password):
        time.sleep(0.6)  # slows down guessing
        return deps.render(request, "login.html", {"next": target, "error": "That password is not right."},
                           status_code=401)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(auth.COOKIE, auth.new_session(), max_age=auth.SESSION_SECONDS, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https")
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login" if deps.auth_enabled() else "/", status_code=303)
    response.delete_cookie(auth.COOKIE)
    return response


# --- always-open endpoints -----------------------------------------------------

@app.get("/healthz")
def healthz():
    problem = deps.storage_problem(recheck=True)
    if problem:
        return JSONResponse({"status": "error", "reason": f"{problem} {deps.STORAGE_FIX}"}, status_code=503)
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(deps.STATIC_DIR / "icon.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})


# --- recipe images, proxied from Mealie ------------------------------------------

def _placeholder(cache: str = "no-store") -> Response:
    return FileResponse(PLACEHOLDER, media_type="image/svg+xml", headers={"Cache-Control": cache})


@app.get("/media/recipe/{recipe_id}")
def recipe_image(recipe_id: str, size: str = "min", v: str | None = None):
    """The browser may not be able to reach Mealie itself (it can be an
    internal Docker hostname), so recipe images are fetched here. A recipe
    without an image, or an unreachable Mealie, gets a placeholder."""
    if deps.missing_config() or not _RECIPE_ID.match(recipe_id):
        return _placeholder()
    url = (f"{deps.env('MEALIE_BASE_URL').rstrip('/')}/api/media/recipes/{recipe_id}/images/"
           f"{_IMAGE_FILES.get(size, _IMAGE_FILES['min'])}")
    headers = {"Authorization": f"Bearer {deps.env('MEALIE_TOKEN')}"} if deps.env("MEALIE_TOKEN") else {}
    try:
        upstream = requests.get(url, headers=headers, timeout=(deps.CONNECT_TIMEOUT, 20))
    except requests.RequestException:
        return _placeholder()
    content_type = upstream.headers.get("content-type", "")
    if upstream.status_code == 404:
        return _placeholder("private, max-age=60")
    if upstream.status_code != 200 or not content_type.startswith("image/"):
        return _placeholder()
    # With a version token in the URL the image can be cached for a long time.
    cache = "private, max-age=604800, immutable" if v else "private, max-age=300"
    return Response(upstream.content, media_type=content_type,
                    headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


# --- friendly errors -------------------------------------------------------------

def _problem(request: Request, message: str, status_code: int, title: str = "That didn't work"):
    """An error as a toast (HTMX), JSON-free full page (browser) - never a trace."""
    if deps.is_htmx(request):
        return deps.toast(request, message)
    return deps.render(request, "error.html", {"title": title, "message": message}, status_code=status_code)


@app.exception_handler(deps.ConfigMissing)
async def config_missing(request: Request, exc: deps.ConfigMissing):
    if deps.is_htmx(request):
        return deps.toast(request, deps.describe_error(exc))
    return deps.render(request, "setup.html", {"active": None})


@app.exception_handler(CoreError)
async def core_error(request: Request, exc: CoreError):
    return _problem(request, str(exc), 400)


@app.exception_handler(requests.RequestException)
async def mealie_error(request: Request, exc: requests.RequestException):
    log.warning("Mealie request failed: %s", exc)
    return _problem(request, deps.describe_error(exc), 502, title="Mealie isn't answering")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return _problem(request, "That page doesn't exist (any more).", 404, title="Not found")
    return _problem(request, str(exc.detail), exc.status_code)


@app.exception_handler(RequestValidationError)
async def bad_form(request: Request, exc: RequestValidationError):
    return _problem(request, "That request was missing something it needs. Go back, reload the page and try again.", 400)


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):
    if isinstance(exc, (sqlite3.Error, OSError)):  # did the data folder stop being writable?
        problem = deps.storage_problem(recheck=True)
        if problem:
            log.error("%s %s", problem, deps.STORAGE_FIX)
            return _storage_notice(request, problem)
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return _problem(request, deps.describe_error(exc), 500, title="Something went wrong")
