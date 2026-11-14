import pytest

from conftest import FakeMealie
from mealie_client import IngredientParseError, IngredientSaveError


@pytest.mark.parametrize("pages_key", ["total_pages", "totalPages", None])
def test_get_all_walks_every_page(pages_key):
    mealie = FakeMealie(pages_key=pages_key)
    for i in range(45):
        mealie.add_recipe(f"R{i:02d}")
    items = mealie.get_all("/api/recipes", per_page=20)
    assert [r["name"] for r in items] == [f"R{i:02d}" for i in range(45)]
    # 3 pages; without a page count an exact multiple needs one extra empty page.
    assert mealie.count("GET", "/api/recipes") == 3
    mealie.calls.clear()
    assert len(mealie.get_all("/api/recipes", per_page=15)) == 45
    assert mealie.count("GET", "/api/recipes") == (3 if pages_key else 4)


def test_get_all_on_empty_and_filtered_lists(mealie):
    assert mealie.get_all("/api/recipes") == []
    mealie.add_recipe("Dinner")
    mealie.add_recipe("Porridge", dinner=False)
    assert [r["name"] for r in mealie.get_all("/api/recipes", tags="dinner")] == ["Dinner"]


def test_recipe_lists_are_not_truncated(mealie):
    for i in range(450):
        mealie.add_recipe(f"R{i:03d}", dinner=i % 2 == 0)
    assert len(mealie.list_recipes()) == 450
    assert len(mealie.list_dinner_recipes()) == 225


def test_meal_plans_are_not_truncated(mealie):
    from conftest import START
    for _ in range(120):
        mealie.add_plan(START, None, entry_type="side", title="x")
    assert len(mealie.list_meal_plans(START, START)) == 120


def test_tag_food_and_unit_lookups_search_past_the_first_page(mealie):
    for store, prefix in ((mealie.tags, "tag"), (mealie.foods, "food"), (mealie.units, "unit")):
        store.extend({"id": f"{prefix}-{i}", "name": f"{prefix} {i}", "slug": f"{prefix}-{i}"} for i in range(450))
    assert mealie._get_or_create_tag("Tag 449")["id"] == "tag-449"
    assert mealie.create_or_get_food("FOOD 449")["id"] == "food-449"
    assert mealie.create_or_get_unit("unit 449")["id"] == "unit-449"
    assert mealie.writes() == []
    assert mealie.create_or_get_food("brand new")["name"] == "brand new"
    assert len(mealie.foods) == 451


def test_lookups_are_cached_only_inside_cached_lookups(mealie):
    mealie.create_or_get_food("rice")
    mealie.create_or_get_food("rice")
    assert mealie.count("GET", "/api/foods") == 2 and len(mealie.foods) == 1

    mealie.calls.clear()
    with mealie.cached_lookups():
        for name in ("rice", "beans", "Beans", "rice", "corn"):
            mealie.create_or_get_food(name)
        mealie.create_or_get_unit("g")
        mealie.create_or_get_unit("g")
    assert mealie.count("GET", "/api/foods") == 1 and mealie.count("GET", "/api/units") == 1
    assert [f["name"] for f in mealie.foods] == ["rice", "beans", "corn"]

    mealie.calls.clear()
    mealie.create_or_get_food("rice")
    assert mealie.count("GET", "/api/foods") == 1  # cache dropped on exit


def test_update_recipe_ingredients_creates_foods_and_units_once(mealie):
    mealie.add_recipe("Stew", ingredients=["500 g beef", "200 g carrots", "salt"])
    mealie.update_recipe_ingredients("stew")
    ings = mealie.recipes["stew"]["recipeIngredient"]
    assert [(i["quantity"], (i["unit"] or {}).get("name"), i["food"]["name"]) for i in ings] == [
        (500.0, "g", "beef"), (200.0, "g", "carrots"), (0, None, "salt")]
    assert all(i["food"]["id"] for i in ings)
    assert [u["name"] for u in mealie.units] == ["g"]


def test_failed_save_raises_a_clear_error_without_printing(mealie, capsys):
    mealie.add_recipe("Stew", ingredients=["500 g beef"])
    mealie.fail_put_slugs.add("stew")
    with pytest.raises(IngredientSaveError, match="Stew"):
        mealie.update_recipe_ingredients("stew")
    assert capsys.readouterr().out == ""


def test_already_structured_ingredients_are_left_alone(mealie):
    potatoes = mealie.structured(1, "kg", "potatoes", "halved")
    mealie.add_recipe("Roast", ingredients=[
        potatoes,
        "2 g salt",
        {"note": "chopped", "originalText": "3 g onions", "referenceId": "ref-onion"},
        {"note": "", "title": "For the gravy"},
    ])
    outcome = mealie.update_recipe_ingredients("roast")
    assert outcome == {"name": "Roast", "parsed": 2, "status": "parsed"}
    # Only the free-text lines went to the parser, as originally typed where Mealie kept that.
    assert mealie.parser_inputs == ["2 g salt", "3 g onions"]
    ings = mealie.recipes["roast"]["recipeIngredient"]
    assert ings[0] == potatoes
    assert [(i["quantity"], i["food"]["name"]) for i in ings[1:3]] == [(2.0, "salt"), (3.0, "onions")]
    assert ings[3] == {"note": "", "title": "For the gravy"}
    assert "halved" not in {f["name"] for f in mealie.foods}


def test_nothing_to_parse_writes_nothing(mealie):
    mealie.add_recipe("Roast", ingredients=[mealie.structured(1, "kg", "potatoes", "halved")])
    before = mealie.get_recipe("roast")
    assert mealie.update_recipe_ingredients("roast") == {"name": "Roast", "parsed": 0, "status": "unchanged"}
    assert mealie.writes() == [] and mealie.get_recipe("roast") == before
    # Running the parse twice is a no-op the second time.
    mealie.add_recipe("Stew", ingredients=["500 g beef"])
    assert mealie.update_recipe_ingredients("stew")["status"] == "parsed"
    mealie.calls.clear()
    assert mealie.update_recipe_ingredients("stew")["status"] == "unchanged" and mealie.writes() == []


def test_a_parser_failure_is_an_error_not_a_success(mealie):
    mealie.add_recipe("Stew", ingredients=["500 g beef"])
    mealie.fail_parser = True
    with pytest.raises(IngredientParseError, match="Stew"):
        mealie.update_recipe_ingredients("stew")
    assert mealie.recipes["stew"]["recipeIngredient"] == [{"note": "500 g beef"}]


def test_a_tag_changed_while_the_parser_runs_is_not_reverted(mealie):
    mealie.add_recipe("Stew", dinner=False, ingredients=["500 g beef", "2 g salt"])

    def someone_edits_the_recipe():
        mealie.recipes["stew"]["tags"].append(mealie._tag("dinner"))  # tagged in the UI meanwhile
        mealie.recipes["stew"]["recipeIngredient"][1]["note"] = "a pinch of salt"  # and a line rewritten

    mealie.on_parse = someone_edits_the_recipe
    assert mealie.update_recipe_ingredients("stew")["parsed"] == 1
    stew = mealie.recipes["stew"]
    assert [t["name"] for t in stew["tags"]] == ["dinner"]
    assert stew["recipeIngredient"][0]["food"]["name"] == "beef"
    assert stew["recipeIngredient"][1] == {"note": "a pinch of salt"}  # the rewritten line is not clobbered
    assert mealie.count("PUT", "/api/recipes/stew") == 0  # only the ingredient list was sent


def test_tag_changes_send_only_the_tags(mealie):
    mealie.add_recipe("Stew", dinner=False, ingredients=["500 g beef"])
    sent = []
    real_patch = mealie.patch
    mealie.patch = lambda path, json: sent.append(json) or real_patch(path, json)
    mealie.add_tag("stew", "dinner")
    mealie.set_effort_tag("stew", 3)
    mealie.remove_tag("stew", "dinner")
    assert [sorted(body) for body in sent] == [["tags"]] * 3
    assert [t["name"] for t in mealie.recipes["stew"]["tags"]] == ["effort-3"]
    assert mealie.count("PUT", "/api/recipes/stew") == 0


def test_a_mealie_without_patch_falls_back_to_a_fresh_put(mealie):
    import requests

    mealie.add_recipe("Stew", dinner=False)

    def no_patch(path, json):
        raise requests.HTTPError("405", response=type("R", (), {"status_code": 405})())

    mealie.patch = no_patch
    mealie.add_tag("stew", "dinner")
    assert [t["name"] for t in mealie.recipes["stew"]["tags"]] == ["dinner"]
    assert mealie.count("PUT", "/api/recipes/stew") == 1


def test_tags_are_matched_by_slug_like_mealie_does(mealie):
    """A tag someone named "Dinner" has the slug "dinner": it is the dinner tag."""
    from core import recipes

    mealie.add_recipe("Pie", dinner_tag="Dinner")
    mealie.recipes["pie"]["tags"].append(mealie._tag("Effort-4"))
    assert [r["name"] for r in mealie.list_dinner_recipes()] == ["Pie"]
    summary = recipes.list_recipes(mealie)[0]
    assert summary["is_dinner"] is True and summary["effort"] == 4

    assert recipes.set_dinner(mealie, "pie", True)["tags"] == ["Dinner", "Effort-4"]  # no duplicate added
    assert mealie.writes() == [] and [t["name"] for t in mealie.tags] == ["Dinner", "Effort-4"]
    assert recipes.set_effort(mealie, "pie", 2)["tags"] == ["Dinner", "effort-2"]  # old effort tag replaced
    assert recipes.set_dinner(mealie, "pie", False)["is_dinner"] is False


def test_real_client_uses_a_session_and_timeout():
    from mealie_client import MealieClient
    client = MealieClient(base_url="http://mealie.invalid/", token="abc", timeout=5)
    assert client.session.headers["Authorization"] == "Bearer abc"
    assert (client.base_url, client.timeout) == ("http://mealie.invalid", 5)
