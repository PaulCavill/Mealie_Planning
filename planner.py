"""
Weekly meal planner.

Rules:
- Weeks run Saturday → Thursday
- Sat/Sun/Mon/Tue/Wed: dinner from Mealie recipe library
- Plan every second week starting this Saturday
- Household: 3 adults + 1 child (servings scaled to 3.5)
- Effort rules: Mon/Wed capped at effort ≤ 3; Sat/Sun/Tue unrestricted
"""
import random
from datetime import date, timedelta
from mealie_client import MealieClient

HOUSEHOLD = {"adults": 3, "children": 1}
EFFECTIVE_SERVINGS = HOUSEHOLD["adults"] + HOUSEHOLD["children"] * 0.5  # 2.5

SATURDAY = 5

# Max effort allowed per weekday (Mon=0 … Sun=6). Omitted = no restriction.
DAY_EFFORT_MAX = {
    0: 3,  # Monday
    2: 3,  # Wednesday
}

EFFORT_NAMES = {f"effort-{i}" for i in range(1, 6)}


def _get_effort(recipe: dict) -> int | None:
    for tag in (recipe.get("tags") or []):
        name = tag.get("name", "")
        if name in EFFORT_NAMES:
            return int(name[-1])
    return None


def this_saturday() -> date:
    today = date.today()
    days_ahead = SATURDAY - today.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


def planned_saturdays(start: date = None, num_weeks: int = 4) -> list[date]:
    """Return Saturdays of weeks to plan (every second week)."""
    if start is None:
        start = this_saturday()
    return [start + timedelta(weeks=i) for i in range(0, num_weeks, 2)]


def scale_factor(recipe_servings: int) -> float:
    return round(EFFECTIVE_SERVINGS / (recipe_servings or 4), 2)


def days_needing_recipes(week_start: date) -> list[date]:
    """Sat/Sun/Mon/Tue/Wed — the 5 days that get a recipe."""
    return [week_start + timedelta(days=i) for i in range(5)]  # Sat=0 … Wed=4


def plan_week(
    client: MealieClient,
    week_start: date,
    used_slugs: set,
    create_shopping_list: bool = False,
    override_plan: bool = False,
) -> dict:
    recipes = client.list_dinner_recipes()
    if not recipes:
        raise ValueError("No recipes tagged 'dinner' in Mealie. Run 'suggest' or 'tag-dinners' first.")

    # Fetch existing meal plans for this week to prevent duplicates
    week_end = week_start + timedelta(days=4)
    existing_entries = client.list_meal_plans(week_start, week_end)
    existing_by_date = {e.get("date"): e for e in existing_entries if e.get("entryType") == "dinner"}

    # If override_plan is set, delete existing entries
    if override_plan:
        for day_iso, entry in existing_by_date.items():
            client.delete_meal_plan_entry(entry["id"])
            print(f"    Deleted existing {entry.get('recipe', {}).get('name', entry.get('title', '(unknown)'))} from {day_iso}")
        existing_by_date.clear()

    # Exclude recipes used in the last 40 days
    lookback_start = week_start - timedelta(days=40)
    recent_meals = client.list_meal_plans(lookback_start, week_start - timedelta(days=1))
    recent_recipe_ids = {
        e.get("recipe", {}).get("id")
        for e in recent_meals
        if e.get("entryType") == "dinner" and e.get("recipe")
    }

    entries = []

    # --- 5 recipe days (Sat → Wed) ---
    for day in days_needing_recipes(week_start):
        day_iso = day.isoformat()

        # Skip if a dinner entry already exists for this day
        if day_iso in existing_by_date:
            existing_entry = existing_by_date[day_iso]
            recipe_name = existing_entry.get("recipe", {}).get("name") if existing_entry.get("recipe") else existing_entry.get("title", "(unknown)")
            print(f"    Skipped {day.strftime('%A')} — already planned: {recipe_name}")
            entries.append({
                "date": day_iso,
                "day": day.strftime("%A"),
                "type": "existing",
                "recipe_name": recipe_name,
                "recipe_id": existing_entry.get("recipe", {}).get("id"),
                "entry_id": existing_entry.get("id"),
            })
            continue

        available = [r for r in recipes if r["slug"] not in used_slugs and r["id"] not in recent_recipe_ids]
        if not available:
            used_slugs.clear()
            available = [r for r in recipes if r["id"] not in recent_recipe_ids]
        if not available:
            available = recipes

        # Apply per-day effort cap
        max_effort = DAY_EFFORT_MAX.get(day.weekday())
        if max_effort is not None:
            effort_pool = [r for r in available if (_get_effort(r) or max_effort) <= max_effort]
            if effort_pool:
                available = effort_pool
            else:
                print(f"    Warning: no effort ≤ {max_effort} recipes free for {day.strftime('%A')}, using any.")

        recipe = random.choice(available)
        used_slugs.add(recipe["slug"])

        sf = scale_factor(recipe.get("recipeServings") or 4)
        entry = client.add_meal_plan_entry(
            plan_date=day,
            entry_type="dinner",
            recipe_id=recipe["id"],
        )
        entries.append({
            "date": day_iso,
            "day": day.strftime("%A"),
            "type": "recipe",
            "recipe_name": recipe["name"],
            "recipe_slug": recipe["slug"],
            "recipe_id": recipe["id"],
            "entry_id": entry.get("id"),
            "scale_factor": sf,
            "effort": _get_effort(recipe),
        })

    result = {"week_start": week_start.isoformat(), "entries": entries}

    if create_shopping_list:
        name = f"Week of {week_start.strftime('%d %b %Y')}"

        # Delete existing shopping list with the same name if it exists
        existing_lists = client.list_shopping_lists()
        for existing in existing_lists:
            if existing.get("name") == name:
                client.delete_shopping_list(existing["id"])
                print(f"    Deleted existing shopping list: {name}")
                break

        sl = client.create_shopping_list(name)
        list_id = sl["id"]
        for e in entries:
            recipe_id = e.get("recipe_id")
            if recipe_id:
                scale = e.get("scale_factor", 1.0)
                client.add_recipe_to_list(list_id, recipe_id, scale=scale)
        result["shopping_list_id"] = list_id
        result["shopping_list_name"] = name

    return result


def plan_fortnightly(
    client: MealieClient,
    start: date = None,
    create_shopping_list: bool = False,
    override_plan: bool = False,
) -> list[dict]:
    """Plan every second week for 4 weeks (2 plans total)."""
    saturdays = planned_saturdays(start)
    used_slugs: set = set()
    plans = []
    for sat in saturdays:
        print(f"  Planning week of {sat.strftime('%d %b %Y')}...")
        plan = plan_week(client, sat, used_slugs, create_shopping_list=create_shopping_list, override_plan=override_plan)
        plans.append(plan)
    return plans


def replace_day(client: MealieClient, target_date: date, search: str = None, effort: int = None) -> None:
    """Interactively replace the dinner entry for a given date."""
    entries = client.list_meal_plans(target_date, target_date)
    dinner_entries = [e for e in entries if e.get("entryType") == "dinner"]

    if not dinner_entries:
        print(f"No dinner entry found for {target_date.strftime('%A %d %B %Y')}.")
        return

    current = dinner_entries[0]
    current_name = (
        current["recipe"]["name"] if current.get("recipe") else current.get("title", "(no recipe)")
    )
    print(f"\nCurrent meal for {target_date.strftime('%A %d %B %Y')}: {current_name}")

    # Collect recipe IDs already in the upcoming plan (excluding the day being replaced)
    lookahead = date.today() + timedelta(weeks=6)
    upcoming = client.list_meal_plans(date.today(), lookahead)
    already_planned_ids = {
        e["recipe"]["id"]
        for e in upcoming
        if e.get("recipe") and e.get("date") != target_date.isoformat()
    }

    recipes = client.list_dinner_recipes()
    if not recipes:
        print("No dinner recipes available. Run 'suggest' or 'tag-dinners' first.")
        return

    if search:
        term = search.lower()
        recipes = [r for r in recipes if term in r["name"].lower()]
        if not recipes:
            print(f"No dinner recipes matching '{search}'.")
            return

    if effort is not None:
        recipes = [r for r in recipes if (_get_effort(r) or 99) <= effort]
        if not recipes:
            print(f"No dinner recipes rated effort {effort} or below. Run 'tag-effort' to rate recipes.")
            return

    recipes = sorted(recipes, key=lambda r: r["name"])
    effort_label = f" (effort ≤ {effort})" if effort is not None else ""
    print(f"\nAvailable recipes ({len(recipes)}){effort_label}:\n")
    for i, r in enumerate(recipes, 1):
        e = _get_effort(r)
        effort_str = f"  [effort-{e}]" if e else ""
        flag = "  [already planned]" if r["id"] in already_planned_ids else ""
        print(f"  {i:>3}. {r['name']}{effort_str}{flag}")

    print()
    while True:
        choice = input("Pick a number (or q to quit): ").strip()
        if choice.lower() == "q":
            print("Cancelled.")
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(recipes):
                chosen = recipes[idx]
                if chosen["id"] in already_planned_ids:
                    print(f"  '{chosen['name']}' is already in the upcoming plan — pick a different recipe.")
                    continue
                break
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(recipes)}.")

    client.delete_meal_plan_entry(current["id"])
    client.add_meal_plan_entry(plan_date=target_date, entry_type="dinner", recipe_id=chosen["id"])
    print(f"\n  {target_date.strftime('%A %d %B')} updated: {chosen['name']}")


def tag_effort(client: MealieClient) -> None:
    """Interactively rate all recipes with an effort level 1-5."""
    recipes = client.list_recipes(per_page=200)
    print(f"\n{len(recipes)} recipes. Rate effort 1-5, s to skip, q to quit.")
    print("  1=very quick  2=easy  3=moderate  4=involved  5=time-intensive\n")
    rated = 0
    for r in sorted(recipes, key=lambda x: x["name"]):
        current = _get_effort(r)
        current_str = f" [currently effort-{current}]" if current else " [unrated]"
        answer = input(f"  {r['name']}{current_str}  [1-5/s/q]: ").strip().lower()
        if answer == "q":
            break
        if answer in ("s", ""):
            continue
        if answer in "12345":
            client.set_effort_tag(r["slug"], int(answer))
            print(f"    -> effort-{answer}")
            rated += 1
        else:
            print("    Unrecognised input, skipping.")
    print(f"\nDone. {rated} recipes rated.")


def print_plans(plans: list[dict]) -> None:
    print(f"\nMeal Plans — {HOUSEHOLD['adults']} adults + {HOUSEHOLD['children']} child\n")
    for plan in plans:
        print(f"  Week of {date.fromisoformat(plan['week_start']).strftime('%A %d %B %Y')}")
        print(f"  {'─' * 50}")
        for e in sorted(plan["entries"], key=lambda x: x["date"]):
            sf_str = f"  (x{e['scale_factor']})" if e.get("scale_factor") else ""
            effort_str = f"  [effort-{e['effort']}]" if e.get("effort") else ""
            print(f"    {e['day']:<12} {e['recipe_name']}{effort_str}{sf_str}")
        if "shopping_list_name" in plan:
            print(f"\n    Shopping list: '{plan['shopping_list_name']}'")
        print()
