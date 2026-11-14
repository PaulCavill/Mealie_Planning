"""Settings: a form over core.settings.Settings, plus connection status."""
from __future__ import annotations

import requests
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from core import db
from core.errors import SettingsError
from core.settings import DEFAULT_SYSTEM_PROMPT, Settings, save_settings
from .. import deps

router = APIRouter()

TEXT_FIELDS = ("adults", "children", "child_weight", "household_description", "week_start_day",
               "week_interval", "weeks_to_plan", "lookback_days", "replace_lookahead_weeks",
               "default_servings", "claude_model", "batch_size", "suggest_count")
ALL_FIELDS = TEXT_FIELDS + ("plan_weekdays", "day_effort_max", "pantry_staples", "system_prompt")


def _values(settings: Settings) -> dict:
    """Settings in the shape the form template reads (and the POST handler rebuilds)."""
    values = {name: getattr(settings, name) for name in TEXT_FIELDS}
    values["plan_weekdays"] = [str(d) for d in settings.plan_weekdays]
    values["effort_max"] = {str(d): str(e) for d, e in settings.day_effort_max.items()}
    values["pantry_staples"] = "\n".join(settings.pantry_staples)
    values["system_prompt"] = settings.system_prompt
    return values


def connection_status() -> dict:
    """What the app is connected to. Never includes a secret value."""
    status = {
        "mealie_url": deps.mealie_url() or None,
        "missing": deps.missing_config(),
        "mealie_ok": False,
        "mealie_message": "Not configured",
        "anthropic": bool(deps.env("ANTHROPIC_API_KEY")),
        "login": deps.auth_enabled(),
        "data_dir": str(db.data_dir().resolve()),
    }
    if status["missing"]:
        return status
    try:
        me = deps.get_client(read_timeout=8).get("/api/users/self")
        name = (me.get("fullName") or me.get("username") or "").strip() if isinstance(me, dict) else ""
        status["mealie_ok"] = True
        status["mealie_message"] = f"Connected{f' as {name}' if name else ''}"
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        status["mealie_message"] = ("Mealie rejected the token" if code in (401, 403)
                                    else f"Mealie answered with an error ({code})")
    except requests.RequestException:
        status["mealie_message"] = "Could not reach Mealie at this address"
    except Exception:
        status["mealie_message"] = "Could not sign in to Mealie"
    return status


def _page(request: Request, values: dict, problems: list[str] | None = None, saved: bool = False,
          status_code: int = 200):
    problems = problems or []
    return deps.render(request, "settings/index.html", {
        "active": "settings",
        "v": values,
        "problems": problems,
        "field_problems": {name: [p for p in problems if name in p] for name in ALL_FIELDS},
        "saved": saved,
        "status": connection_status(),
        "default_prompt": DEFAULT_SYSTEM_PROMPT,
        "prompt_is_default": values["system_prompt"] == DEFAULT_SYSTEM_PROMPT,
    }, status_code=status_code)


@router.get("/settings")
def settings_page(request: Request, saved: str = ""):
    return _page(request, _values(deps.get_settings()), saved=bool(saved))


@router.post("/settings")
async def save(request: Request):
    form = await request.form()

    def text(name: str) -> str:
        return str(form.get(name) or "").replace("\r\n", "\n").strip()

    values = {name: text(name) for name in TEXT_FIELDS}
    values["plan_weekdays"] = [str(d) for d in form.getlist("plan_weekdays")]
    values["effort_max"] = {str(d): text(f"effort_max_{d}") for d in range(7) if text(f"effort_max_{d}")}
    values["pantry_staples"] = text("pantry_staples")
    values["system_prompt"] = text("system_prompt")

    data = {name: values[name] for name in TEXT_FIELDS}
    data.update(plan_weekdays=values["plan_weekdays"], day_effort_max=values["effort_max"],
                pantry_staples=values["pantry_staples"], system_prompt=values["system_prompt"])
    try:
        save_settings(Settings.from_dict(data))
    except SettingsError as exc:
        return await run_in_threadpool(_page, request, values, exc.problems, False, 400)
    return RedirectResponse("/settings?saved=1", status_code=303)
