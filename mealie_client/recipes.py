from __future__ import annotations
from typing import TYPE_CHECKING
import re

import requests

if TYPE_CHECKING:
    from .client import MealieClient


def parse_ingredient_note(note: str) -> dict:
    """Parse ingredient note into Mealie's structured format: {quantity, unit, food, note}.

    Examples:
    - "500g chicken breast, diced" → {quantity: 500.0, unit: {name: "g"}, food: {name: "chicken breast"}, note: "diced"}
    - "2 tbsp olive oil" → {quantity: 2.0, unit: {name: "tbsp"}, food: {name: "olive oil"}, note: ""}
    - "1/2 teaspoon salt" → {quantity: 0.5, unit: {name: "teaspoon"}, food: {name: "salt"}, note: ""}
    """
    note = (note or "").strip()
    if not note:
        return {"quantity": 0, "unit": None, "food": None, "note": ""}

    quantity = 0
    unit = None
    food = None
    notes = ""

    # Match "amount unit food [, notes]" pattern
    match = re.match(r'^([\d.]+(?:/[\d.]+)?)\s*([a-zA-Z]*)\s+(.+?)(?:\s*,\s*(.+))?$', note)
    if match:
        amount_str, unit_str, rest, notes = match.groups()
        try:
            if '/' in match.group(1):
                parts = match.group(1).split('/')
                quantity = float(parts[0]) / float(parts[1])
            else:
                quantity = float(amount_str)
        except (ValueError, ZeroDivisionError):
            quantity = 1
        if unit_str:
            unit = {"name": unit_str.strip()}
        if rest:
            food = {"name": rest.strip()}
        notes = notes.strip() if notes else ""
    else:
        food = {"name": note}

    return {"quantity": quantity, "unit": unit, "food": food, "note": notes or ""}



class IngredientParseError(RuntimeError):
    """Mealie's ingredient parser could not be used for a recipe."""


class IngredientSaveError(RuntimeError):
    """Mealie refused to save a recipe after its ingredients were parsed."""


def tag_key(tag) -> str:
    """What identifies a tag: its slug, as Mealie's own recipe filter uses
    (falling back to the lower-cased name). Accepts a tag dict or a name."""
    if isinstance(tag, dict):
        return str(tag.get("slug") or tag.get("name") or "").strip().lower()
    return str(tag or "").strip().lower()


EFFORT_TAG_KEYS = frozenset(f"effort-{i}" for i in range(1, 6))


def _tag_ref(tag: dict) -> dict:
    return {"id": tag["id"], "name": tag["name"], "slug": tag.get("slug")}


def _ingredient_source_text(ingredient: dict) -> str:
    """The line the parser should read: what was originally typed if Mealie
    kept it, otherwise the note."""
    return str(ingredient.get("originalText") or "").strip() or str(ingredient.get("note") or "").strip()


class RecipesMixin:
    """Recipe operations mixed into MealieClient."""

    def list_recipes(self: "MealieClient", tag: str = None, per_page: int = 200) -> list[dict]:
        """Every recipe summary (all pages), optionally filtered by tag."""
        params = {"tags": tag} if tag else {}
        return self.get_all("/api/recipes", per_page=per_page, **params)

    def list_dinner_recipes(self: "MealieClient") -> list[dict]:
        return self.list_recipes(tag="dinner")

    def get_recipe(self: "MealieClient", slug: str) -> dict:
        return self.get(f"/api/recipes/{slug}")

    def scrape_recipe(self: "MealieClient", url: str) -> dict:
        """Import a recipe directly from a URL into Mealie."""
        result = self.post("/api/recipes/create/url", json={"url": url, "includeTags": True})
        slug = result if isinstance(result, str) else result.get("slug", result)
        return self.get_recipe(slug)

    def test_scrape(self: "MealieClient", url: str) -> dict:
        """Preview a recipe scrape without importing it."""
        return self.post("/api/recipes/test-scrape-url", json={"url": url})

    def create_recipe(self: "MealieClient", name: str) -> dict:
        result = self.post("/api/recipes", json={"name": name})
        slug = result if isinstance(result, str) else result.get("slug", result)
        return self.get_recipe(slug)

    def update_recipe_fields(self: "MealieClient", slug: str, fields: dict) -> None:
        """Change only `fields` of a recipe (PATCH), so that whatever else was
        edited in the meantime - by a person in Mealie or by another job here -
        is not overwritten with a stale copy.

        A Mealie without PATCH support (405) gets a fresh GET + full PUT.
        """
        try:
            self.patch(f"/api/recipes/{slug}", json=fields)
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 405:
                raise
            recipe = self.get_recipe(slug)
            recipe.update(fields)
            self.put(f"/api/recipes/{slug}", json=recipe)

    def add_tag(self: "MealieClient", slug: str, tag_name: str) -> dict:
        """Add a tag to a recipe by name, creating the tag if needed."""
        tag = self._get_or_create_tag(tag_name)
        recipe = self.get_recipe(slug)
        existing_tags = recipe.get("tags") or []
        wanted = {tag_key(tag_name), tag_key(tag)}
        if any(tag_key(t) in wanted or t.get("id") == tag["id"] for t in existing_tags):
            return recipe
        self.update_recipe_fields(slug, {"tags": existing_tags + [_tag_ref(tag)]})
        return self.get_recipe(slug)

    def remove_tag(self: "MealieClient", slug: str, tag_name: str) -> dict:
        """Remove a tag from a recipe by name. No-op if the recipe doesn't have it."""
        recipe = self.get_recipe(slug)
        existing_tags = recipe.get("tags") or []
        kept_tags = [t for t in existing_tags if tag_key(t) != tag_key(tag_name)]
        if len(kept_tags) == len(existing_tags):
            return recipe
        self.update_recipe_fields(slug, {"tags": kept_tags})
        return self.get_recipe(slug)

    def set_effort_tag(self: "MealieClient", slug: str, effort: int | None) -> None:
        """Set the effort-N tag (1-5) on a recipe, removing any existing effort tags.

        Passing None just removes the existing effort tags.
        """
        tag = self._get_or_create_tag(f"effort-{effort}") if effort is not None else None
        recipe = self.get_recipe(slug)
        existing_tags = recipe.get("tags") or []
        kept_tags = [t for t in existing_tags if tag_key(t) not in EFFORT_TAG_KEYS]
        if tag:
            kept_tags.append(_tag_ref(tag))
        if [t.get("id") for t in kept_tags] == [t.get("id") for t in existing_tags]:
            return
        self.update_recipe_fields(slug, {"tags": kept_tags})

    def _get_or_create_tag(self: "MealieClient", name: str) -> dict:
        # A tag called "Dinner" has the slug "dinner" and is the same tag to Mealie.
        path = "/api/organizers/tags"
        index = self._name_index(path)
        if name.lower() in index:
            return index[name.lower()]
        for tag in index.values():
            if tag_key(tag) == tag_key(name):
                return tag
        index[name.lower()] = self.post(path, json={"name": name})
        return index[name.lower()]

    def upload_recipe_image(self: "MealieClient", slug: str, content: bytes, content_type: str) -> None:
        """Set a recipe's cover image from raw image bytes."""
        ext = "png" if "png" in content_type else "jpg"
        self.put_multipart(
            f"/api/recipes/{slug}/image",
            files={
                "image": (f"cover.{ext}", content, content_type),
                "extension": (None, ext),
            },
        )

    def search_recipes(self: "MealieClient", query: str) -> list[dict]:
        data = self.get("/api/recipes", search=query, perPage=20)
        return data.get("items", [])

    def _existing_food_or_unit(self: "MealieClient", parsed, create) -> dict | None:
        """The parser's food/unit as a row that exists in Mealie (created if new)."""
        if not isinstance(parsed, dict):
            return None
        if parsed.get("id"):
            return parsed
        name = str(parsed.get("name") or "").strip()
        return create(name) if name else None

    def update_recipe_ingredients(self: "MealieClient", slug: str) -> dict:
        """Parse a recipe's free-text ingredient lines into quantity/unit/food
        with Mealie's NLP parser.

        Only lines that have no food yet are parsed; ingredients that are
        already structured are left exactly as they are (their note is just
        preparation text such as "halved"). Returns

            {"name": <recipe name>, "parsed": n, "status": "parsed" | "unchanged"}

        "unchanged" means there was nothing to do and nothing was written.
        Raises IngredientParseError if the parser can't be used and
        IngredientSaveError if Mealie refuses the result.

        The recipe is re-read just before saving and only its ingredient list
        is sent, so a tag changed while the parser was running is not undone.
        """
        recipe = self.get_recipe(slug)
        name = recipe.get("name") or slug
        ingredients = recipe.get("recipeIngredient") or []
        todo = [(i, _ingredient_source_text(ing)) for i, ing in enumerate(ingredients) if not ing.get("food")]
        todo = [(i, text) for i, text in todo if text]
        unchanged = {"name": name, "parsed": 0, "status": "unchanged"}
        if not todo:
            return unchanged

        try:
            answer = self.post("/api/parser/ingredients",
                               json={"parser": "nlp", "ingredients": [text for _, text in todo]})
        except Exception as e:
            raise IngredientParseError(f"Mealie's ingredient parser failed for '{name}' ({slug}): {e}") from e
        if not isinstance(answer, list) or len(answer) != len(todo):
            raise IngredientParseError(
                f"Mealie's ingredient parser gave an unexpected answer for '{name}' ({slug}); nothing was changed.")

        # Work out every change (creating new foods/units) before touching the recipe.
        changes = []
        for (index, text), item in zip(todo, answer):
            parsed = item.get("ingredient") if isinstance(item, dict) else None
            if not isinstance(parsed, dict):
                continue
            food = self._existing_food_or_unit(parsed.get("food"), self.create_or_get_food)
            if not food:
                continue  # the parser found no food in this line: leave it as text
            changes.append({
                "index": index, "text": text, "reference": ingredients[index].get("referenceId"),
                "fields": {
                    "quantity": parsed.get("quantity") or 0,
                    "unit": self._existing_food_or_unit(parsed.get("unit"), self.create_or_get_unit),
                    "food": food,
                    "note": parsed.get("note") or "",
                    "originalText": text,
                },
            })
        if not changes:
            return unchanged

        # Apply them to the recipe as it is *now*, not as it was before the parser ran.
        fresh = self.get_recipe(slug).get("recipeIngredient") or []
        by_reference = {ing.get("referenceId"): ing for ing in fresh if ing.get("referenceId")}
        applied = 0
        for change in changes:
            if change["reference"]:
                target = by_reference.get(change["reference"])
            else:
                target = fresh[change["index"]] if change["index"] < len(fresh) else None
            # Skip a line that was removed, rewritten or parsed by someone else meanwhile.
            if target is None or target.get("food") or _ingredient_source_text(target) != change["text"]:
                continue
            target.update(change["fields"])
            applied += 1
        if not applied:
            return unchanged

        try:
            self.update_recipe_fields(slug, {"recipeIngredient": fresh})
        except Exception as e:
            raise IngredientSaveError(
                f"Parsed the ingredients for '{name}' ({slug}) but Mealie rejected the save: {e}. "
                "Check the recipe in the Mealie UI and parse it there if needed."
            ) from e
        return {"name": name, "parsed": applied, "status": "parsed"}
