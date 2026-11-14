"""An in-memory Mealie: the real MealieClient with its HTTP transport replaced."""
import itertools
import json as jsonlib
import re
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mealie_client import MealieClient  # noqa: E402

START = date(2026, 11, 14)  # a Saturday


class FakeMealie(MealieClient):
    """Serves the Mealie endpoints the app uses from dicts. No network."""

    def __init__(self, pages_key="total_pages"):
        super().__init__(base_url="http://mealie.invalid", token="test")
        self.session = None  # any real HTTP attempt fails loudly
        self.pages_key = pages_key  # "total_pages", "totalPages" or None
        self._ids = itertools.count(1)
        self.recipes = {}  # slug -> full recipe
        self.mealplans = []
        self.tags, self.foods, self.units = [], [], []
        self.shopping_lists = []
        self.shopping_items = []
        self.images = {}
        self.calls = []  # (method, path) of every request
        self.fail_put_slugs = set()

    # --- test helpers ----------------------------------------------------

    def _id(self, prefix):
        return f"{prefix}-{next(self._ids)}"

    def _tag(self, name):
        for t in self.tags:
            if t["name"] == name:
                return t
        tag = {"id": self._id("tag"), "name": name, "slug": name}
        self.tags.append(tag)
        return tag

    def add_recipe(self, name, effort=None, dinner=True, servings=4, ingredients=()):
        slug = name.lower().replace(" ", "-")
        tags = [self._tag("dinner")] if dinner else []
        if effort:
            tags.append(self._tag(f"effort-{effort}"))
        recipe = {
            "id": self._id("recipe"), "slug": slug, "name": name, "recipeServings": servings,
            "tags": [dict(t) for t in tags],
            "recipeIngredient": [{"note": i} for i in ingredients],
        }
        self.recipes[slug] = recipe
        return recipe

    def add_plan(self, day, recipe=None, entry_type="dinner", title=""):
        entry = {"id": self._id("plan"), "date": day.isoformat(), "entryType": entry_type,
                 "title": title, "recipe": recipe}
        self.mealplans.append(entry)
        return entry

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def count(self, method, path):
        return self.calls.count((method, path))

    # --- transport -------------------------------------------------------

    def _page(self, items, params):
        page, per_page = int(params.get("page", 1)), int(params.get("perPage", 50))
        chunk = items[(page - 1) * per_page: page * per_page]
        data = {"items": jsonlib.loads(jsonlib.dumps(chunk)), "page": page}
        if self.pages_key:
            data[self.pages_key] = max(1, -(-len(items) // per_page))
        return data

    def get(self, path, **params):
        self.calls.append(("GET", path))
        if path == "/api/recipes":
            items = list(self.recipes.values())
            if params.get("tags"):
                items = [r for r in items if any(t["name"] == params["tags"] for t in r["tags"])]
            summaries = [{k: v for k, v in r.items() if k != "recipeIngredient"} for r in items]
            return self._page(summaries, params)
        if path.startswith("/api/recipes/"):
            return jsonlib.loads(jsonlib.dumps(self.recipes[path.rsplit("/", 1)[1]]))
        if path == "/api/households/mealplans":
            items = [e for e in self.mealplans
                     if params.get("start_date", "") <= e["date"] <= params.get("end_date", "9999")]
            return self._page(items, params)
        if path == "/api/organizers/tags":
            return self._page(self.tags, params)
        if path == "/api/foods":
            return self._page(self.foods, params)
        if path == "/api/units":
            return self._page(self.units, params)
        if path == "/api/households/shopping/lists":
            return self._page(self.shopping_lists, params)
        if path == "/api/households/shopping/items":
            # Deliberately ignores the shoppingListId filter, like a careless server.
            return self._page(self.shopping_items, params)
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, json):
        self.calls.append(("POST", path))
        if path == "/api/households/mealplans":
            recipe = next((r for r in self.recipes.values() if r["id"] == json.get("recipeId")), None)
            entry = self.add_plan(date.fromisoformat(json["date"]), recipe, json["entryType"])
            return dict(entry)
        if path in ("/api/organizers/tags", "/api/foods", "/api/units"):
            store = {"/api/organizers/tags": self.tags, "/api/foods": self.foods, "/api/units": self.units}[path]
            item = {"id": self._id("item"), "name": json["name"], "slug": json["name"]}
            store.append(item)
            return dict(item)
        if path == "/api/parser/ingredients":
            out = []
            for note in json["ingredients"]:
                m = re.match(r"([\d.]+)\s*(\w+)\s+(.*)", note)
                qty, unit, food = (float(m[1]), m[2], m[3]) if m else (0, None, note)
                out.append({"input": note, "ingredient": {
                    "quantity": qty, "note": "",
                    "unit": {"id": None, "name": unit} if unit else None,
                    "food": {"id": None, "name": food},
                }})
            return out
        if path == "/api/recipes/create/html-or-json":
            data = jsonlib.loads(json["data"])
            recipe = self.add_recipe(data["name"], dinner=False, ingredients=data.get("recipeIngredient", ()))
            return recipe["slug"]
        if path == "/api/households/shopping/lists":
            sl = {"id": self._id("list"), "name": json["name"]}
            self.shopping_lists.append(sl)
            return dict(sl)
        m = re.fullmatch(r"/api/households/shopping/lists/([^/]+)/recipe", path)
        if m:
            for req in json:
                recipe = next(r for r in self.recipes.values() if r["id"] == req["recipeId"])
                for ing in recipe["recipeIngredient"]:
                    self.shopping_items.append({
                        "id": self._id("item"), "shoppingListId": m[1], "note": "",
                        "food": {"name": ing["note"]}, "scale": req["recipeIncrementQuantity"],
                    })
            return {}
        raise AssertionError(f"unexpected POST {path}")

    def put(self, path, json):
        self.calls.append(("PUT", path))
        slug = path.rsplit("/", 1)[1]
        if slug in self.fail_put_slugs:
            raise RuntimeError("500 Server Error")
        self.recipes[slug] = jsonlib.loads(jsonlib.dumps(json))
        return json

    def put_multipart(self, path, files):
        self.calls.append(("PUT", path))
        self.images[path.split("/")[3]] = files["image"]
        return {}

    def delete(self, path):
        self.calls.append(("DELETE", path))
        item_id = path.rsplit("/", 1)[1]
        for store in (self.mealplans, self.shopping_lists, self.shopping_items):
            for item in store:
                if item["id"] == item_id:
                    store.remove(item)
                    return
        raise RuntimeError(f"404 Not Found: {path}")


@pytest.fixture
def mealie():
    return FakeMealie()


@pytest.fixture
def library(mealie):
    """Twelve dinner recipes: effort 1-5 twice over, plus two unrated."""
    for i in range(10):
        mealie.add_recipe(f"Recipe {i:02d}", effort=i % 5 + 1)
    mealie.add_recipe("Recipe 10")
    mealie.add_recipe("Recipe 11")
    mealie.add_recipe("Porridge", dinner=False)
    return mealie
