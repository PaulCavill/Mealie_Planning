import json
import random
from datetime import date, timedelta

import pytest

from core import planning
from core.errors import PlanningError
from core.settings import Settings
from conftest import START

SETTINGS = Settings()


def days_of(preview):
    return [d for w in preview["weeks"] for d in w["days"]]


def preview(mealie, seed=0, **kwargs):
    return planning.preview_plan(mealie, SETTINGS, START, rng=random.Random(seed), **kwargs)


def test_default_shape_is_two_saturday_to_wednesday_weeks(library):
    p = preview(library)
    assert [w["week_start"] for w in p["weeks"]] == ["2026-11-14", "2026-11-28"]
    for week in p["weeks"]:
        assert [d["day"] for d in week["days"]] == ["Saturday", "Sunday", "Monday", "Tuesday", "Wednesday"]
    assert p["weeks"][0]["days"][-1]["date"] == "2026-11-18"
    assert p["warnings"] == [] and p["deletions"] == []


def test_preview_writes_nothing_and_is_json_serialisable(library):
    p = preview(library)
    assert library.writes() == []
    assert json.loads(json.dumps(p)) == p


def test_effort_caps_on_monday_and_wednesday(library):
    # Enough easy recipes that the uncapped days can never use them all up first.
    for i in range(12):
        library.add_recipe(f"Hard {i}", effort=4 + i % 2)
        library.add_recipe(f"Easy {i}", effort=1 + i % 3)
    for seed in range(30):
        p = preview(library, seed)
        assert p["warnings"] == []
        for day in days_of(p):
            if day["day"] in ("Monday", "Wednesday"):
                assert day["max_effort"] == 3
                assert day["effort"] is None or day["effort"] <= 3  # unrated is allowed
            else:
                assert day["max_effort"] is None
    efforts = {d["effort"] for seed in range(30) for d in days_of(preview(library, seed)) if d["day"] == "Saturday"}
    assert efforts & {4, 5}  # uncapped days do get harder recipes


def test_warns_when_no_recipe_fits_the_effort_cap(mealie):
    for i in range(12):
        mealie.add_recipe(f"Hard {i}", effort=5)
    p = preview(mealie)
    capped = [d for d in days_of(p) if d["day"] in ("Monday", "Wednesday")]
    assert all(d["effort"] == 5 for d in capped)
    assert {w["date"] for w in p["warnings"]} == {d["date"] for d in capped}
    assert p["warnings"][0]["message"] == "no effort ≤ 3 recipes free for Monday, using any."


def test_no_duplicates_across_the_fortnight(library):
    for seed in range(30):
        ids = [d["recipe_id"] for d in days_of(preview(library, seed))]
        assert len(ids) == 10 and len(set(ids)) == 10


def test_recipes_eaten_within_lookback_are_excluded(library):
    recipes = list(library.recipes.values())
    recent, old = recipes[0], recipes[1]
    library.add_plan(START - timedelta(days=40), recent)
    library.add_plan(START - timedelta(days=41), old)
    library.add_plan(START - timedelta(days=3), recipes[2], entry_type="lunch")  # only dinners count
    seen = set()
    for seed in range(40):
        week1, week2 = preview(library, seed)["weeks"]
        assert recent["id"] not in {d["recipe_id"] for d in week1["days"]}
        seen |= {d["recipe_id"] for d in week1["days"] + week2["days"]}
    assert old["id"] in seen and recipes[2]["id"] in seen


def test_existing_dinners_are_kept_and_not_reused(library):
    kept = library.recipes["recipe-00"]
    entry = library.add_plan(START + timedelta(days=1), kept)
    library.add_plan(START + timedelta(days=1), None, entry_type="lunch", title="Leftovers")
    for seed in range(20):
        p = preview(library, seed)
        sunday = p["weeks"][0]["days"][1]
        assert sunday["type"] == "existing"
        assert (sunday["entry_id"], sunday["recipe_name"], sunday["scale_factor"]) == (entry["id"], "Recipe 00", None)
        assert [d["recipe_id"] for d in days_of(p)].count(kept["id"]) == 1
    assert sum(d["type"] == "recipe" for d in days_of(p)) == 9


def test_override_lists_deletions_and_replans_those_days(library):
    entry = library.add_plan(START + timedelta(days=1), library.recipes["recipe-00"])
    outside = library.add_plan(START + timedelta(days=5), library.recipes["recipe-01"])  # Thursday: not planned
    p = preview(library, override=True)
    assert [d["entry_id"] for d in p["deletions"]] == [entry["id"]]
    assert all(d["type"] == "recipe" for d in days_of(p))
    assert library.writes() == []

    result = planning.commit_plan(library, SETTINGS, p)
    assert [d["entry_id"] for d in result["deleted"]] == [entry["id"]]
    assert result["created"] == 10
    assert entry["id"] not in {e["id"] for e in library.mealplans}
    assert outside["id"] in {e["id"] for e in library.mealplans}


def test_scale_factor_uses_household_and_recipe_servings(mealie):
    mealie.add_recipe("Four", servings=4)
    mealie.add_recipe("Two", servings=2)
    mealie.add_recipe("Unknown", servings=None)
    assert SETTINGS.effective_servings == 3.5
    factors = {d["recipe_name"]: d["scale_factor"] for d in days_of(preview(mealie))}
    assert factors == {"Four": 0.88, "Two": 1.75, "Unknown": 0.88}
    assert Settings(adults=2, children=2, child_weight=0.5).scale_factor(6) == 0.5


def test_small_library_repeats_with_a_warning(mealie):
    for i in range(3):
        mealie.add_recipe(f"Only {i}", effort=1)
    p = preview(mealie)
    assert len(days_of(p)) == 10
    assert any("may repeat" in w["message"] for w in p["warnings"])


def test_no_dinner_recipes_is_an_error(mealie):
    mealie.add_recipe("Porridge", dinner=False)
    with pytest.raises(PlanningError):
        preview(mealie)


def test_settings_change_the_days_and_weeks(library):
    settings = Settings(week_start_day=0, plan_weekdays=[0, 4], week_interval=1, weeks_to_plan=3,
                        day_effort_max={4: 1})
    start = date(2026, 11, 16)  # Monday
    p = planning.preview_plan(library, settings, start, rng=random.Random(1))
    assert [w["week_start"] for w in p["weeks"]] == ["2026-11-16", "2026-11-23", "2026-11-30"]
    assert [d["day"] for d in days_of(p)] == ["Monday", "Friday"] * 3
    assert all(d["effort"] in (None, 1) for d in days_of(p) if d["day"] == "Friday")
    assert planning.next_week_start(settings, today=date(2026, 11, 14)) == start
    assert planning.next_week_start(SETTINGS, today=START) == START
    assert planning.next_week_start(SETTINGS, today=START + timedelta(days=1)) == START + timedelta(days=7)


def test_commit_round_trips_through_json_and_writes_the_preview(library):
    p = json.loads(json.dumps(preview(library)))
    messages = []
    result = planning.commit_plan(library, SETTINGS, p, progress=messages.append)
    assert result["created"] == 10 and result["warnings"] == []
    written = {e["date"]: e["recipe"]["id"] for e in library.mealplans}
    assert written == {d["date"]: d["recipe_id"] for d in days_of(p)}
    assert all(e["entryType"] == "dinner" for e in library.mealplans)
    assert all(e["entry_id"] for w in result["weeks"] for e in w["entries"])
    assert "shopping_list_id" not in result["weeks"][0]
    assert messages == ["Writing week of 14 Nov 2026...", "Writing week of 28 Nov 2026..."]


def test_commit_does_not_double_book_a_day_planned_since_the_preview(library):
    p = preview(library)
    library.add_plan(START, library.recipes["recipe-11"])
    result = planning.commit_plan(library, SETTINGS, p)
    assert result["created"] == 9
    assert [w["date"] for w in result["warnings"]] == [START.isoformat()]
    assert result["weeks"][0]["entries"][0]["type"] == "existing"
    assert sum(e["date"] == START.isoformat() for e in library.mealplans) == 1


def test_second_run_keeps_everything(library):
    planning.commit_plan(library, SETTINGS, preview(library))
    again = preview(library, seed=5)
    assert all(d["type"] == "existing" for d in days_of(again))
    assert planning.commit_plan(library, SETTINGS, again)["created"] == 0


def test_shopping_list_is_scaled_and_pantry_staples_are_removed(mealie):
    for i in range(10):
        mealie.add_recipe(f"Meal {i}", ingredients=[f"thing {i}", "Sea Salt", "olive oil"])
    mealie.shopping_lists.append({"id": "old", "name": "Week of 14 Nov 2026"})
    mealie.shopping_items.append({"id": "other", "shoppingListId": "someone-elses", "note": "salt", "food": None})
    messages = []
    result = planning.commit_plan(mealie, SETTINGS, preview(mealie), create_shopping_list=True,
                                  progress=messages.append)

    week = result["weeks"][0]
    assert week["shopping_list_name"] == "Week of 14 Nov 2026" and week["staples_removed"] == 10
    assert [sl["name"] for sl in mealie.shopping_lists] == ["Week of 14 Nov 2026", "Week of 28 Nov 2026"]
    assert "old" not in {sl["id"] for sl in mealie.shopping_lists}
    mine = [i for i in mealie.shopping_items if i["shoppingListId"] == week["shopping_list_id"]]
    assert sorted(i["food"]["name"] for i in mine) == sorted(
        f"thing {d['recipe_name'][-1]}" for d in week["entries"])
    assert {i["scale"] for i in mine} == {0.88}
    assert "other" in {i["id"] for i in mealie.shopping_items}  # another list's salt is untouched
    assert "Removed 10 pantry staple(s) from shopping list." in messages
    assert "Deleted existing shopping list: Week of 14 Nov 2026" in messages


def test_pantry_staples_come_from_settings(mealie):
    mealie.shopping_items += [
        {"id": "a", "shoppingListId": "L", "note": "Butter ", "food": None},
        {"id": "b", "shoppingListId": "L", "note": "", "food": {"name": "salt"}},
    ]
    assert planning.remove_pantry_staples(mealie, Settings(pantry_staples=["butter"]), "L") == 1
    assert [i["id"] for i in mealie.shopping_items] == ["b"]


def test_reroll_changes_one_day_and_respects_the_rules(library):
    p = preview(library)
    monday = date(2026, 11, 16)
    for seed in range(20):
        new = planning.reroll_day(library, SETTINGS, p, monday, rng=random.Random(seed))
        old_day, new_day = p["weeks"][0]["days"][2], new["weeks"][0]["days"][2]
        assert new_day["date"] == old_day["date"] and new_day["recipe_id"] != old_day["recipe_id"]
        assert new_day["effort"] is None or new_day["effort"] <= 3
        ids = [d["recipe_id"] for d in days_of(new)]
        assert len(set(ids)) == 10
        assert [d for d in days_of(new) if d["date"] != old_day["date"]] == \
               [d for d in days_of(p) if d["date"] != old_day["date"]]
    assert p["weeks"][0]["days"][2] == old_day  # the original preview is untouched
    assert library.writes() == []


def test_reroll_refuses_kept_days_and_unknown_dates(library):
    library.add_plan(START, library.recipes["recipe-00"])
    p = preview(library)
    with pytest.raises(PlanningError):
        planning.reroll_day(library, SETTINGS, p, START)
    with pytest.raises(PlanningError):
        planning.reroll_day(library, SETTINGS, p, date(2026, 11, 20))


def test_reroll_with_nothing_else_to_offer(mealie):
    mealie.add_recipe("Only one")
    p = preview(mealie)
    with pytest.raises(PlanningError):
        planning.reroll_day(mealie, SETTINGS, p, START)


def test_set_preview_day(library):
    p = preview(library)
    chosen = library.recipes["recipe-10"]
    new = planning.set_preview_day(library, SETTINGS, p, START, chosen["id"])
    assert new["weeks"][0]["days"][0]["recipe_name"] == "Recipe 10"
    with pytest.raises(PlanningError):
        planning.set_preview_day(library, SETTINGS, p, START, library.recipes["porridge"]["id"])


def test_get_plan_range(library):
    library.add_plan(START + timedelta(days=1), library.recipes["recipe-02"])
    library.add_plan(START, None, entry_type="lunch", title="Leftovers")
    library.add_plan(START + timedelta(days=9), library.recipes["recipe-03"])
    entries = planning.get_plan_range(library, START, START + timedelta(days=6))
    assert [(e["date"], e["day"], e["entry_type"], e["recipe_name"], e["effort"]) for e in entries] == [
        ("2026-11-14", "Saturday", "lunch", "Leftovers", None),
        ("2026-11-15", "Sunday", "dinner", "Recipe 02", 3),
    ]


def test_replacement_options_and_replace_day(library):
    target = START + timedelta(days=2)
    current = library.add_plan(target, library.recipes["recipe-00"])
    library.add_plan(START + timedelta(days=3), library.recipes["recipe-01"])
    library.add_plan(START + timedelta(weeks=7), library.recipes["recipe-02"])  # beyond the look-ahead

    options = planning.replacement_options(library, SETTINGS, target, today=START)
    names = [r["name"] for r in options["recipes"]]
    assert options["current"]["entry_id"] == current["id"]
    assert options["hidden"] == 1 and "Recipe 01" not in names
    assert "Recipe 00" in names and "Recipe 02" in names and "Porridge" not in names
    assert names == sorted(names) and options["empty_reason"] is None

    easy = planning.replacement_options(library, SETTINGS, target, effort=2, today=START)
    assert [r["effort"] for r in easy["recipes"]] and all(r["effort"] <= 2 for r in easy["recipes"])
    assert planning.replacement_options(library, SETTINGS, target, search="zzz", today=START)["empty_reason"] == "no_search_match"
    assert planning.replacement_options(library, SETTINGS, target, search="recipe 01", today=START)["empty_reason"] == "all_planned"
    assert planning.replacement_options(library, SETTINGS, START, today=START)["current"] is None

    chosen = options["recipes"][-1]
    result = planning.replace_day(library, target, chosen["id"])
    assert result["replaced"]["recipe_name"] == "Recipe 00"
    on_day = [e for e in library.mealplans if e["date"] == target.isoformat()]
    assert [e["recipe"]["id"] for e in on_day] == [chosen["id"]]


def test_shopping_list_scales_kept_dinners_for_the_household(mealie):
    for i in range(10):
        mealie.add_recipe(f"Meal {i}", ingredients=[f"thing {i}"])
    kept = mealie.add_recipe("Big Roast", servings=2, ingredients=["roast"])
    mealie.add_plan(START, kept)

    result = planning.commit_plan(mealie, SETTINGS, preview(mealie), create_shopping_list=True)

    week = result["weeks"][0]
    assert week["entries"][0]["type"] == "existing" and week["entries"][0]["scale_factor"] is None
    scales = {i["food"]["name"]: i["scale"] for i in mealie.shopping_items
              if i["shoppingListId"] == week["shopping_list_id"]}
    assert scales["roast"] == SETTINGS.scale_factor(2) == 1.75  # not 1.0
    assert {v for k, v in scales.items() if k != "roast"} == {0.88}
