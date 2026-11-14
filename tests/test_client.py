import pytest

from conftest import FakeMealie
from mealie_client import IngredientSaveError


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


def test_real_client_uses_a_session_and_timeout():
    from mealie_client import MealieClient
    client = MealieClient(base_url="http://mealie.invalid/", token="abc", timeout=5)
    assert client.session.headers["Authorization"] == "Bearer abc"
    assert (client.base_url, client.timeout) == ("http://mealie.invalid", 5)
