"""Meal planning: preview a plan, adjust it, then commit it to Mealie.

A preview is a plain JSON-serialisable dict, so it can round-trip through a
browser or session between preview_plan() and commit_plan():

    {
      "start": "2026-11-14",
      "override": false,
      "weeks": [
        {"week_start": "2026-11-14", "days": [<day>, ...]},
        ...
      ],
      "warnings": [{"date": "2026-11-16", "message": "..."}],
      "deletions": [<entry>, ...]      # existing dinners an override will delete
    }

    <day>   = {"date", "day" (weekday name), "type": "existing" | "recipe",
               "recipe_id", "recipe_slug", "recipe_name", "effort" (1-5 or null),
               "scale_factor" (null for existing), "max_effort" (the day's cap or null),
               "entry_id" (existing only)}
    <entry> = {"entry_id", "date", "day", "entry_type", "recipe_id", "recipe_slug",
               "recipe_name", "effort"}

"existing" days are already in Mealie and are kept; "recipe" days are proposals.
"""
from __future__ import annotations

import copy
import random
from datetime import date, timedelta
from typing import Callable

from mealie_client import MealieClient
from .errors import PlanningError
from .recipes import effort_of
from .settings import Settings, WEEKDAY_NAMES

Progress = Callable[[str], None]

DINNER = "dinner"


# --- dates -----------------------------------------------------------------

def next_week_start(settings: Settings, today: date | None = None) -> date:
    """The next occurrence of the configured week start day (today counts)."""
    today = today or date.today()
    return today + timedelta(days=(settings.week_start_day - today.weekday()) % 7)


def week_starts(settings: Settings, start: date) -> list[date]:
    """Start dates of the weeks one planning run covers."""
    return [start + timedelta(weeks=i * settings.week_interval) for i in range(settings.weeks_to_plan)]


def week_days(settings: Settings, week_start: date) -> list[date]:
    """The days in the 7 days from week_start that get a dinner planned."""
    days = (week_start + timedelta(days=i) for i in range(7))
    return [d for d in days if d.weekday() in settings.plan_weekdays]


# --- reading the plan ------------------------------------------------------

def _entry_view(entry: dict) -> dict:
    recipe = entry.get("recipe") or {}
    return {
        "entry_id": entry.get("id"),
        "date": entry["date"],
        "day": date.fromisoformat(entry["date"]).strftime("%A"),
        "entry_type": entry.get("entryType"),
        "recipe_id": recipe.get("id"),
        "recipe_slug": recipe.get("slug"),
        "recipe_name": recipe.get("name") or entry.get("title") or "(no recipe)",
        "effort": effort_of(recipe),
    }


def get_plan_range(client: MealieClient, start: date, end: date) -> list[dict]:
    """All meal plan entries (every meal type) from start to end inclusive, by date."""
    entries = [_entry_view(e) for e in client.list_meal_plans(start, end)]
    return sorted(entries, key=lambda e: e["date"])


def _dinners(client: MealieClient, start: date, end: date) -> list[dict]:
    return [e for e in client.list_meal_plans(start, end) if e.get("entryType") == DINNER]


# --- choosing recipes ------------------------------------------------------

def _choose(
    recipes: list[dict],
    day: date,
    settings: Settings,
    used: set,
    recent: set,
    rng,
    exclude: frozenset = frozenset(),
) -> tuple[dict | None, list[str], bool]:
    """Pick a recipe for `day`. Returns (recipe, warnings, exhausted).

    Preference order: not yet used in this plan and not eaten recently; then
    anything not eaten recently; then anything. `exhausted` is True when the
    first tier was empty. The day's effort cap is applied to whichever tier is
    used, and waived with a warning if nothing in it fits.
    """
    day_name = day.strftime("%A")
    pool = [r for r in recipes if r["id"] not in exclude]
    warnings = []

    available = [r for r in pool if r["id"] not in used and r["id"] not in recent]
    exhausted = not available
    if not available:
        available = [r for r in pool if r["id"] not in recent]
        if available:
            warnings.append(f"Not enough unused recipes for {day_name}; a recipe may repeat within this plan.")
    if not available:
        available = pool
        if available:
            warnings.append(
                f"Every dinner recipe was eaten in the last {settings.lookback_days} days; "
                f"{day_name} may repeat a recent meal."
            )
    if not available:
        return None, warnings, exhausted

    max_effort = settings.day_effort_max.get(day.weekday())
    if max_effort is not None:
        # Unrated recipes are allowed on capped days.
        effort_pool = [r for r in available if (effort_of(r) or max_effort) <= max_effort]
        if effort_pool:
            available = effort_pool
        else:
            warnings.append(f"no effort ≤ {max_effort} recipes free for {day_name}, using any.")

    return rng.choice(available), warnings, exhausted


def _proposed_day(day: date, recipe: dict, settings: Settings) -> dict:
    return {
        "date": day.isoformat(),
        "day": day.strftime("%A"),
        "type": "recipe",
        "recipe_id": recipe["id"],
        "recipe_slug": recipe["slug"],
        "recipe_name": recipe["name"],
        "effort": effort_of(recipe),
        "scale_factor": settings.scale_factor(recipe.get("recipeServings")),
        "max_effort": settings.day_effort_max.get(day.weekday()),
    }


def _existing_day(day: date, entry: dict, settings: Settings) -> dict:
    view = _entry_view(entry)
    return {
        "date": view["date"],
        "day": view["day"],
        "type": "existing",
        "recipe_id": view["recipe_id"],
        "recipe_slug": view["recipe_slug"],
        "recipe_name": view["recipe_name"],
        "effort": view["effort"],
        "scale_factor": None,
        "max_effort": settings.day_effort_max.get(day.weekday()),
        "entry_id": view["entry_id"],
    }


def _recent_ids(history: list[tuple[date, str]], week_start: date, settings: Settings) -> set:
    first = week_start - timedelta(days=settings.lookback_days)
    return {recipe_id for day, recipe_id in history if first <= day < week_start}


# --- preview ---------------------------------------------------------------

def preview_plan(
    client: MealieClient,
    settings: Settings,
    start: date,
    override: bool = False,
    rng: random.Random | None = None,
) -> dict:
    """Work out the plan for the weeks starting at `start` without writing to Mealie.

    Days that already have a dinner are kept, unless `override`, in which case
    they are listed under "deletions" and re-planned. Raises PlanningError if
    there are no dinner recipes. Pass `rng` for reproducible picks.
    """
    rng = rng or random
    recipes = client.list_dinner_recipes()
    if not recipes:
        raise PlanningError("No recipes tagged 'dinner' in Mealie. Generate some recipes or tag existing ones as dinner first.")

    starts = week_starts(settings, start)
    days_by_week = [week_days(settings, ws) for ws in starts]
    planned = [d for days in days_by_week for d in days]
    if not planned:
        raise PlanningError("No weekdays are selected for planning.")

    warnings = []
    if start.weekday() != settings.week_start_day:
        warnings.append({
            "date": None,
            "message": f"The plan starts on a {start.strftime('%A')}, "
                       f"not a {WEEKDAY_NAMES[settings.week_start_day]} (the configured week start).",
        })

    dinners = _dinners(client, starts[0] - timedelta(days=settings.lookback_days), max(planned))
    planned_dates = {d.isoformat() for d in planned}

    deletions = []
    if override:
        deletions = [_entry_view(e) for e in dinners if e["date"] in planned_dates]
        dinners = [e for e in dinners if e["date"] not in planned_dates]

    existing_by_date = {e["date"]: e for e in dinners if e["date"] in planned_dates}
    # (date, recipe id) of every dinner known so far, for the lookback rule.
    history = [(date.fromisoformat(e["date"]), e["recipe"]["id"]) for e in dinners if e.get("recipe")]
    # Recipes already in this plan: kept entries now, proposals as they are made.
    used = {e["recipe"]["id"] for e in existing_by_date.values() if e.get("recipe")}

    weeks = []
    for week_start, days in zip(starts, days_by_week):
        recent = _recent_ids(history, week_start, settings)
        week = {"week_start": week_start.isoformat(), "days": []}
        for day in days:
            existing = existing_by_date.get(day.isoformat())
            if existing:
                week["days"].append(_existing_day(day, existing, settings))
                continue
            recipe, day_warnings, exhausted = _choose(recipes, day, settings, used, recent, rng)
            if exhausted:
                used.clear()  # start a fresh rotation through the library
            used.add(recipe["id"])
            history.append((day, recipe["id"]))
            week["days"].append(_proposed_day(day, recipe, settings))
            warnings += [{"date": day.isoformat(), "message": m} for m in day_warnings]
        weeks.append(week)

    return {
        "start": start.isoformat(),
        "override": override,
        "weeks": weeks,
        "warnings": warnings,
        "deletions": deletions,
    }


def _preview_days(preview: dict) -> list[dict]:
    try:
        return [day for week in preview["weeks"] for day in week["days"]]
    except (KeyError, TypeError) as e:
        raise PlanningError("That plan preview is not valid.") from e


def _find_day(preview: dict, target_date: date) -> dict:
    for day in _preview_days(preview):
        if day.get("date") == target_date.isoformat():
            return day
    raise PlanningError(f"{target_date.isoformat()} is not part of this plan.")


def _week_start_of(preview: dict, target_date: date) -> date:
    for week in preview["weeks"]:
        if any(d.get("date") == target_date.isoformat() for d in week["days"]):
            return date.fromisoformat(week["week_start"])
    raise PlanningError(f"{target_date.isoformat()} is not part of this plan.")


def _replace_day_in_preview(preview: dict, target_date: date, new_day: dict, warnings: list[str]) -> dict:
    updated = copy.deepcopy(preview)
    iso = target_date.isoformat()
    for week in updated["weeks"]:
        week["days"] = [new_day if d.get("date") == iso else d for d in week["days"]]
    updated["warnings"] = [w for w in updated.get("warnings", []) if w.get("date") != iso]
    updated["warnings"] += [{"date": iso, "message": m} for m in warnings]
    return updated


def reroll_day(
    client: MealieClient,
    settings: Settings,
    preview: dict,
    target_date: date,
    rng: random.Random | None = None,
) -> dict:
    """Return a copy of `preview` with a different recipe proposed for one day.

    Follows the same rules as preview_plan() and avoids every recipe already in
    the preview. Only proposed days can be re-rolled, not kept existing entries.
    Raises PlanningError if there is no other recipe to offer.
    """
    rng = rng or random
    current = _find_day(preview, target_date)
    if current.get("type") != "recipe":
        raise PlanningError(f"{current.get('day', target_date.isoformat())} already has a dinner in Mealie; replace it instead.")

    week_start = _week_start_of(preview, target_date)
    others = [d for d in _preview_days(preview) if d is not current and d.get("recipe_id")]
    deleted_ids = {d.get("entry_id") for d in preview.get("deletions", [])}

    dinners = _dinners(client, week_start - timedelta(days=settings.lookback_days), week_start - timedelta(days=1))
    history = [
        (date.fromisoformat(e["date"]), e["recipe"]["id"])
        for e in dinners if e.get("recipe") and e.get("id") not in deleted_ids
    ]
    history += [(date.fromisoformat(d["date"]), d["recipe_id"]) for d in others]

    recipe, warnings, _ = _choose(
        client.list_dinner_recipes(),
        target_date,
        settings,
        used={d["recipe_id"] for d in others},
        recent=_recent_ids(history, week_start, settings),
        rng=rng,
        exclude=frozenset({current.get("recipe_id")}),
    )
    if recipe is None:
        raise PlanningError(f"There is no other dinner recipe to offer for {current.get('day', target_date.isoformat())}.")
    return _replace_day_in_preview(preview, target_date, _proposed_day(target_date, recipe, settings), warnings)


def set_preview_day(
    client: MealieClient,
    settings: Settings,
    preview: dict,
    target_date: date,
    recipe_id: str,
) -> dict:
    """Return a copy of `preview` with a specific dinner recipe proposed for one day."""
    current = _find_day(preview, target_date)
    if current.get("type") != "recipe":
        raise PlanningError(f"{current.get('day', target_date.isoformat())} already has a dinner in Mealie; replace it instead.")
    recipe = next((r for r in client.list_dinner_recipes() if r["id"] == recipe_id), None)
    if recipe is None:
        raise PlanningError("That recipe is not in the dinner library.")
    return _replace_day_in_preview(preview, target_date, _proposed_day(target_date, recipe, settings), [])


# --- commit ----------------------------------------------------------------

def remove_pantry_staples(client: MealieClient, settings: Settings, list_id: str) -> int:
    """Delete shopping list items that are pantry staples. Returns how many were removed."""
    staples = {s.strip().lower() for s in settings.pantry_staples}
    removed = 0
    for item in client.get_list_items(list_id):
        food = item.get("food") or {}
        label = (food.get("name") or item.get("note") or "").strip().lower()
        if label in staples:
            client.delete_list_item(item["id"])
            removed += 1
    return removed


def shopping_list_name(week_start: date) -> str:
    return f"Week of {week_start.strftime('%d %b %Y')}"


def shopping_lists_to_replace(client: MealieClient, preview: dict) -> list[str]:
    """Names of shopping lists already in Mealie that saving this preview with
    "create shopping lists" would delete and rebuild - losing anything ticked
    off or added to them by hand."""
    try:
        wanted = [shopping_list_name(date.fromisoformat(week["week_start"])) for week in preview["weeks"]]
    except (KeyError, TypeError, ValueError) as e:
        raise PlanningError("That plan preview is not valid.") from e
    existing = {sl.get("name") for sl in client.list_shopping_lists()}
    return [name for name in wanted if name in existing]


def _build_shopping_list(
    client: MealieClient,
    settings: Settings,
    week_start: date,
    entries: list[dict],
    progress: Progress | None,
    kept_scales: dict[str, float] | None = None,
) -> dict:
    """`kept_scales` maps a date to the scale factor of the dinner already in
    Mealie on that day; kept days carry no scale_factor of their own."""
    kept_scales = kept_scales or {}
    name = shopping_list_name(week_start)
    for existing in client.list_shopping_lists():
        if existing.get("name") == name:
            client.delete_shopping_list(existing["id"])
            if progress:
                progress(f"Deleted existing shopping list: {name}")
            break

    list_id = client.create_shopping_list(name)["id"]
    for entry in entries:
        if entry.get("recipe_id"):
            scale = entry.get("scale_factor") or kept_scales.get(entry.get("date")) or settings.scale_factor(None)
            client.add_recipe_to_list(list_id, entry["recipe_id"], scale=scale)

    removed = remove_pantry_staples(client, settings, list_id)
    if removed and progress:
        progress(f"Removed {removed} pantry staple(s) from shopping list.")
    return {"shopping_list_id": list_id, "shopping_list_name": name, "staples_removed": removed}


def _reason(error: Exception) -> str:
    return str(error).strip() or type(error).__name__


def commit_plan(
    client: MealieClient,
    settings: Settings,
    preview: dict,
    create_shopping_list: bool = False,
    progress: Progress | None = None,
) -> dict:
    """Write a preview to Mealie, one day at a time: add the proposed dinner,
    then (for an override) delete the dinner it replaces - so a day never ends
    up empty because a later step failed. Optionally builds one shopping list
    per week from what was actually written.

    A day that fails is reported and the rest carry on. A proposed day that
    has gained a dinner in Mealie since the preview was made is left alone.
    Returns:

        {"weeks": [{"week_start", "entries": [<day> + "entry_id"],
                    "shopping_list_id", "shopping_list_name", "staples_removed"}],
         "created": n,                       # dinners added
         "failed": [<day> + "error"],        # proposed dinners Mealie refused
         "deleted": [<entry>],               # overridden dinners removed
         "warnings": [{"date", "message"}]}

    "entries" are the dinners now in Mealie for that week (added or kept); a
    failed day appears there only if its previous dinner is still in place.
    The shopping list keys are present only when a list was created. Raises
    PlanningError if every single dinner was refused.
    """
    days = _preview_days(preview)
    if not days:
        raise PlanningError("That plan preview has no days in it.")
    warnings = []

    to_delete: dict[str, list[dict]] = {}
    for entry in preview.get("deletions", []):
        to_delete.setdefault(entry.get("date"), []).append(entry)
    doomed_ids = {e.get("entry_id") for entries in to_delete.values() for e in entries}

    # Re-read the plan so a stale preview can't double-book a day.
    dates = [date.fromisoformat(d["date"]) for d in days]
    in_mealie = _dinners(client, min(dates), max(dates))
    present_ids = {e.get("id") for e in in_mealie}
    doomed_by_date = {e["date"]: e for e in in_mealie if e.get("id") in doomed_ids}
    current_by_date = {e["date"]: e for e in in_mealie if e.get("id") not in doomed_ids}
    # Kept dinners are scaled for the household too, from their recipe's servings.
    kept_scales = {
        day_iso: settings.scale_factor(e["recipe"].get("recipeServings"))
        for day_iso, e in {**doomed_by_date, **current_by_date}.items() if e.get("recipe")
    }

    created = 0
    failed = []
    deleted = []
    weeks = []
    for week in preview["weeks"]:
        week_start = date.fromisoformat(week["week_start"])
        if progress:
            progress(f"Writing week of {week_start.strftime('%d %b %Y')}...")
        entries = []
        for day in week["days"]:
            day_date = date.fromisoformat(day["date"])
            current = current_by_date.get(day["date"])
            if day.get("type") != "recipe":
                entries.append(dict(day))
                continue
            if current:
                kept = _existing_day(day_date, current, settings)
                entries.append(kept)
                warnings.append({"date": day["date"],
                                 "message": f"{day['day']} already has a dinner planned ({kept['recipe_name']}); left as is."})
                continue

            try:
                entry = client.add_meal_plan_entry(plan_date=day_date, entry_type=DINNER, recipe_id=day["recipe_id"])
            except Exception as e:
                failed.append({**day, "error": _reason(e)})
                message = f"Could not add {day.get('recipe_name')} on {day['day']} {day['date']}: {_reason(e)}"
                still_there = doomed_by_date.get(day["date"])
                if still_there:
                    kept = _existing_day(day_date, still_there, settings)
                    entries.append(kept)
                    message += f" The dinner already planned ({kept['recipe_name']}) was left in place."
                warnings.append({"date": day["date"], "message": message})
                continue
            entries.append({**day, "entry_id": entry.get("id")})
            created += 1

            # Only now that the new dinner exists is the one it replaces removed.
            for old in to_delete.get(day["date"], []):
                if old.get("entry_id") not in present_ids:
                    continue  # already gone from Mealie
                try:
                    client.delete_meal_plan_entry(old["entry_id"])
                except Exception as e:
                    warnings.append({"date": old.get("date"),
                                     "message": f"Could not delete {old.get('recipe_name')} from {old.get('date')}, "
                                                f"so that day now has two dinners: {_reason(e)}"})
                    continue
                deleted.append(old)
                if progress:
                    progress(f"Deleted existing {old.get('recipe_name')} from {old.get('date')}")
        weeks.append({"week_start": week["week_start"], "entries": entries})

    if failed and not created:
        raise PlanningError(
            f"Nothing was saved: Mealie refused {'the dinner' if len(failed) == 1 else f'all {len(failed)} dinners'} "
            f"({failed[0]['error']}). The plan in Mealie is unchanged; this preview is still here to try again."
        )

    if create_shopping_list:
        for result in weeks:
            week_start = date.fromisoformat(result["week_start"])
            try:
                result.update(_build_shopping_list(client, settings, week_start, result["entries"], progress, kept_scales))
            except Exception as e:
                warnings.append({"date": None,
                                 "message": f"The shopping list for the week of {week_start.strftime('%d %b %Y')} "
                                            f"could not be built ({_reason(e)}). The dinners are saved; check the "
                                            "list in Mealie, or generate a plan for that week again to rebuild it."})

    return {"weeks": weeks, "created": created, "failed": failed, "deleted": deleted, "warnings": warnings}


# --- replacing one day -----------------------------------------------------

def replacement_options(
    client: MealieClient,
    settings: Settings,
    target_date: date,
    search: str | None = None,
    effort: int | None = None,
    today: date | None = None,
) -> dict:
    """What could replace the dinner on `target_date`.

    Returns:
        {"date", "current": <entry> or None,
         "recipes": [{"id", "slug", "name", "effort"}],   # sorted by name
         "hidden": n,          # matching recipes hidden because they are already planned
         "empty_reason": None | "no_dinner_recipes" | "no_search_match"
                         | "no_effort_match" | "all_planned"}

    `search` filters by name; `effort` keeps recipes rated at or below it
    (unrated ones are excluded). Recipes planned on another day between today
    and settings.replace_lookahead_weeks ahead are hidden.
    """
    today = today or date.today()
    dinners = _dinners(client, target_date, target_date)
    result = {
        "date": target_date.isoformat(),
        "current": _entry_view(dinners[0]) if dinners else None,
        "recipes": [],
        "hidden": 0,
        "empty_reason": None,
    }

    recipes = client.list_dinner_recipes()
    if not recipes:
        result["empty_reason"] = "no_dinner_recipes"
        return result
    if search:
        recipes = [r for r in recipes if search.lower() in r["name"].lower()]
        if not recipes:
            result["empty_reason"] = "no_search_match"
            return result
    if effort is not None:
        recipes = [r for r in recipes if (effort_of(r) or 99) <= effort]
        if not recipes:
            result["empty_reason"] = "no_effort_match"
            return result

    upcoming = client.list_meal_plans(today, today + timedelta(weeks=settings.replace_lookahead_weeks))
    planned_ids = {
        e["recipe"]["id"] for e in upcoming
        if e.get("recipe") and e.get("date") != target_date.isoformat()
    }
    free = [r for r in recipes if r["id"] not in planned_ids]
    result["hidden"] = len(recipes) - len(free)
    if not free:
        result["empty_reason"] = "all_planned"
        return result

    result["recipes"] = [
        {"id": r["id"], "slug": r["slug"], "name": r["name"], "effort": effort_of(r)}
        for r in sorted(free, key=lambda r: r["name"])
    ]
    return result


def replace_day(client: MealieClient, target_date: date, recipe_id: str) -> dict:
    """Put `recipe_id` on `target_date` as dinner, replacing the current dinner if any.

    The new dinner is added first and the old one removed only once that has
    worked, so a failure never leaves the day empty. Raises PlanningError if
    `recipe_id` is not a dinner recipe.

    Returns {"date", "replaced": <entry> or None, "entry_id", "recipe_id"}.
    """
    if not any(r["id"] == recipe_id for r in client.list_dinner_recipes()):
        raise PlanningError("That recipe is not in the dinner library.")
    dinners = _dinners(client, target_date, target_date)
    replaced = _entry_view(dinners[0]) if dinners else None
    entry = client.add_meal_plan_entry(plan_date=target_date, entry_type=DINNER, recipe_id=recipe_id)
    if dinners:
        try:
            client.delete_meal_plan_entry(dinners[0]["id"])
        except Exception as e:
            try:  # put things back as they were rather than leave two dinners
                client.delete_meal_plan_entry(entry.get("id"))
            except Exception:
                raise PlanningError(
                    f"The new dinner was added, but {replaced['recipe_name']} could not be removed ({_reason(e)}). "
                    "That day now has two dinners; remove one in Mealie."
                ) from e
            raise PlanningError(
                f"{replaced['recipe_name']} could not be removed ({_reason(e)}), so the day was left as it was."
            ) from e
    return {
        "date": target_date.isoformat(),
        "replaced": replaced,
        "entry_id": entry.get("id"),
        "recipe_id": recipe_id,
    }
