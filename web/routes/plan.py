"""Plan: the calendar, generating a plan (preview -> adjust -> save) and
swapping one day's dinner."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from core import planning, recipes as recipe_ops
from core.errors import PlanningError
from core.settings import Settings, WEEKDAY_NAMES
from .. import deps, previews

router = APIRouter()

CALENDAR_WEEKS = 4
PICKER_LIMIT = 100


def _parse_date(value: str | None) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        raise PlanningError(f"'{value}' is not a date. Use the date picker, or type it as YYYY-MM-DD.") from None


def _parse_effort(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() and 1 <= int(value) <= 5 else None


def _week_floor(settings: Settings, day: date) -> date:
    """The configured week start day on or before `day`."""
    return day - timedelta(days=(day.weekday() - settings.week_start_day) % 7)


def _plan_summary(settings: Settings) -> str:
    """One line describing what 'Generate plan' will fill, from the settings."""
    ordered = sorted(settings.plan_weekdays, key=lambda d: (d - settings.week_start_day) % 7)
    days = ", ".join(WEEKDAY_NAMES[d][:3] for d in ordered)
    weeks = f"{settings.weeks_to_plan} week{'s' if settings.weeks_to_plan != 1 else ''}"
    if settings.weeks_to_plan > 1 and settings.week_interval > 1:
        ordinal = {2: "2nd", 3: "3rd"}.get(settings.week_interval, f"{settings.week_interval}th")
        weeks += f", every {ordinal} week"
    return f"Dinners for {days} · {weeks}"


# --- calendar ------------------------------------------------------------------

def _calendar(client, settings: Settings, first: date, today: date) -> dict:
    last = first + timedelta(days=7 * CALENDAR_WEEKS - 1)
    by_date: dict[str, list[dict]] = {}
    for entry in planning.get_plan_range(client, first, last):
        by_date.setdefault(entry["date"], []).append(entry)

    weeks = []
    for w in range(CALENDAR_WEEKS):
        week_start = first + timedelta(weeks=w)
        days = []
        for i in range(7):
            day = week_start + timedelta(days=i)
            entries = by_date.get(day.isoformat(), [])
            dinner = next((e for e in entries if e["entry_type"] == planning.DINNER), None)
            days.append({
                "date": day,
                "iso": day.isoformat(),
                "is_today": day == today,
                "is_past": day < today,
                "plan_day": day.weekday() in settings.plan_weekdays,
                "dinner": dinner,
                "others": [e for e in entries if e is not dinner],
            })
        weeks.append({
            "start": week_start,
            "end": week_start + timedelta(days=6),
            "is_current": week_start <= today <= week_start + timedelta(days=6),
            "days": days,
            "planned": sum(1 for d in days if d["dinner"]),
        })
    current = _week_floor(settings, today)
    return {
        "weeks": weeks,
        "start": first.isoformat(),
        "earlier": (first - timedelta(weeks=CALENDAR_WEEKS)).isoformat(),
        "later": (first + timedelta(weeks=CALENDAR_WEEKS)).isoformat(),
        "at_today": first == current,
        "empty": not by_date,
    }


def _calendar_start(settings: Settings, start: str | None, today: date) -> date:
    return _week_floor(settings, _parse_date(start) if start else today)


@router.get("/")
def plan_home(request: Request, start: str | None = None):
    client = deps.get_client()
    settings = deps.get_settings()
    today = date.today()
    return deps.render(request, "plan/index.html", {
        "active": "plan",
        "cal": _calendar(client, settings, _calendar_start(settings, start, today), today),
        "default_start": planning.next_week_start(settings, today).isoformat(),
        "plan_summary": _plan_summary(settings),
        "draft": previews.latest_draft(),
    })


# --- swap one planned day ----------------------------------------------------------

_SWAP_EMPTY = {
    "no_dinner_recipes": "There are no dinner recipes yet. Mark some on the Recipes page, or generate new ones.",
    "no_search_match": "No dinner recipes match that search.",
    "no_effort_match": "No dinner recipes are rated at or below that effort. Rate more on the Recipes page.",
    "all_planned": "Every matching recipe is already in the upcoming plan. Widen the search.",
}


@router.get("/plan/swap")
def swap_picker(request: Request, date: str, q: str = "", effort: str = "", start: str = ""):
    client = deps.get_client()
    settings = deps.get_settings()
    target = _parse_date(date)
    options = planning.replacement_options(client, settings, target, search=q.strip() or None,
                                           effort=_parse_effort(effort))
    current = options["current"]
    note = None
    if options["hidden"] and not options["empty_reason"]:
        note = f"{options['hidden']} already in the upcoming plan {'is' if options['hidden'] == 1 else 'are'} hidden."
    context = {
        "title": f"{'Swap' if current else 'Add'} dinner · {deps._day_long(target)}",
        "subtitle": f"Currently: {current['recipe_name']}" if current else "Nothing is planned for this day yet.",
        "search_url": "/plan/swap",
        "pick_url": "/plan/swap",
        "pick_target": "#calendar",
        "hidden_fields": {"date": target.isoformat(), "start": start},
        "q": q, "effort": _parse_effort(effort),
        "recipes": options["recipes"][:PICKER_LIMIT],
        "more": max(0, len(options["recipes"]) - PICKER_LIMIT),
        "empty_message": _SWAP_EMPTY.get(options["empty_reason"]),
        "note": note,
    }
    partial = request.headers.get("hx-target") == "picker-results"
    return deps.render(request, "plan/_picker_results.html" if partial else "plan/_picker.html", context)


@router.post("/plan/swap")
def swap(request: Request, date: str = Form(...), recipe_id: str = Form(...), start: str = Form("")):
    client = deps.get_client()
    settings = deps.get_settings()
    target = _parse_date(date)
    planning.replace_day(client, target, recipe_id)
    if not deps.is_htmx(request):
        return RedirectResponse("/" + (f"?start={start}" if start else ""), status_code=303)
    today = _today()
    return deps.render(request, "plan/_calendar.html", {
        "cal": _calendar(client, settings, _calendar_start(settings, start or None, today), today),
        "changed": target.isoformat(),
    })


def _today() -> date:
    return date.today()


# --- generate: preview ---------------------------------------------------------

@router.post("/plan/preview")
def create_preview(request: Request, start: str = Form(""), override: str | None = Form(None),
                   shopping_list: str | None = Form(None)):
    client = deps.get_client()
    settings = deps.get_settings()
    start_date = _parse_date(start) if start.strip() else planning.next_week_start(settings)
    preview = planning.preview_plan(client, settings, start_date, override=bool(override))
    preview_id = previews.create(preview, shopping_list=bool(shopping_list))
    return RedirectResponse(f"/plan/preview/{preview_id}", status_code=303)


def _preview_context(record: dict, changed: str | None = None) -> dict:
    preview = record["data"]
    by_date: dict[str, list[str]] = {}
    for warning in preview.get("warnings", []):
        by_date.setdefault(warning.get("date"), []).append(warning["message"])
    days = [d for week in preview["weeks"] for d in week["days"]]
    return {
        "active": "plan",
        "pid": record["id"],
        "preview": preview,
        "shopping_list": record["shopping_list"],
        "general_warnings": by_date.get(None, []),
        "day_warnings": by_date,
        "proposed": sum(1 for d in days if d["type"] == "recipe"),
        "kept": sum(1 for d in days if d["type"] != "recipe"),
        "changed": changed,
    }


def _draft(preview_id: str) -> dict:
    record = previews.get(preview_id)
    if record is None:
        raise PlanningError("That plan preview no longer exists. Generate a new one from the Plan page.")
    if record["status"] != "draft":
        raise PlanningError("That plan has already been saved to Mealie.")
    return record


@router.get("/plan/preview/{preview_id}")
def show_preview(request: Request, preview_id: str):
    record = previews.get(preview_id)
    if record is None:
        return deps.render(request, "error.html", {
            "active": "plan", "title": "Preview not found",
            "message": "That plan preview no longer exists. It may have been discarded, or it expired.",
        }, status_code=404)
    if record["status"] == "saved":
        return deps.render(request, "plan/saved.html", {"active": "plan", "result": record["result"],
                                                        "shopping_list": record["shopping_list"]})
    return deps.render(request, "plan/preview.html", _preview_context(record))


def _after_change(request: Request, preview_id: str, preview: dict, changed: date):
    previews.update(preview_id, preview)
    if not deps.is_htmx(request):
        return RedirectResponse(f"/plan/preview/{preview_id}", status_code=303)
    record = previews.get(preview_id) or _draft(preview_id)
    return deps.render(request, "plan/_preview_body.html", _preview_context(record, changed.isoformat()))


@router.post("/plan/preview/{preview_id}/reroll")
def reroll(request: Request, preview_id: str, date: str = Form(...)):
    record = _draft(preview_id)
    target = _parse_date(date)
    preview = planning.reroll_day(deps.get_client(), deps.get_settings(), record["data"], target)
    return _after_change(request, preview_id, preview, target)


@router.get("/plan/preview/{preview_id}/choose")
def choose_picker(request: Request, preview_id: str, date: str, q: str = "", effort: str | None = None):
    record = _draft(preview_id)
    target = _parse_date(date)
    days = [d for week in record["data"]["weeks"] for d in week["days"]]
    day = next((d for d in days if d["date"] == target.isoformat()), None)
    if day is None:
        raise PlanningError(f"{target.isoformat()} is not part of this plan.")
    # First open: start from the day's effort cap. Afterwards the select decides.
    cap = day.get("max_effort") if effort is None else _parse_effort(effort)
    in_plan = {d["recipe_id"] for d in days if d["date"] != day["date"] and d.get("recipe_id")}

    recipes = recipe_ops.list_recipes(deps.get_client(), dinner_only=True)
    empty_message = None
    if not recipes:
        empty_message = _SWAP_EMPTY["no_dinner_recipes"]
    term = q.strip().lower()
    if term:
        recipes = [r for r in recipes if term in (r["name"] or "").lower()]
        if not recipes and not empty_message:
            empty_message = _SWAP_EMPTY["no_search_match"]
    if cap is not None:
        recipes = [r for r in recipes if (r["effort"] or 99) <= cap]
        if not recipes and not empty_message:
            empty_message = "No matching dinner recipes are rated at or below that effort. Choose “Any effort” to see the rest."
    for recipe in recipes:
        recipe["in_plan"] = recipe["id"] in in_plan
        recipe["current"] = recipe["id"] == day.get("recipe_id")

    context = {
        "title": f"Choose dinner · {deps._day_long(target)}",
        "subtitle": f"Proposed: {day['recipe_name']}",
        "search_url": f"/plan/preview/{preview_id}/choose",
        "pick_url": f"/plan/preview/{preview_id}/set",
        "pick_target": "#preview-body",
        "hidden_fields": {"date": target.isoformat()},
        "q": q, "effort": cap,
        "recipes": recipes[:PICKER_LIMIT],
        "more": max(0, len(recipes) - PICKER_LIMIT),
        "empty_message": empty_message,
        "note": None,
    }
    partial = request.headers.get("hx-target") == "picker-results"
    return deps.render(request, "plan/_picker_results.html" if partial else "plan/_picker.html", context)


@router.post("/plan/preview/{preview_id}/set")
def set_day(request: Request, preview_id: str, date: str = Form(...), recipe_id: str = Form(...)):
    record = _draft(preview_id)
    target = _parse_date(date)
    preview = planning.set_preview_day(deps.get_client(), deps.get_settings(), record["data"], target, recipe_id)
    return _after_change(request, preview_id, preview, target)


@router.post("/plan/preview/{preview_id}/commit")
def commit(request: Request, preview_id: str, shopping_list: str | None = Form(None)):
    record = previews.get(preview_id)
    if record is None:
        raise PlanningError("That plan preview no longer exists. Generate a new one from the Plan page.")
    if record["status"] == "saved" or not previews.claim(preview_id):
        return RedirectResponse(f"/plan/preview/{preview_id}", status_code=303)  # saved already, or being saved
    try:
        result = planning.commit_plan(deps.get_client(read_timeout=120), deps.get_settings(), record["data"],
                                      create_shopping_list=bool(shopping_list))
    except Exception:
        previews.release(preview_id)
        raise
    previews.finish(preview_id, result, bool(shopping_list))
    return RedirectResponse(f"/plan/preview/{preview_id}", status_code=303)


@router.post("/plan/preview/{preview_id}/discard")
def discard(preview_id: str):
    record = previews.get(preview_id)
    if record and record["status"] == "draft":
        previews.delete(preview_id)
    return RedirectResponse("/", status_code=303)
