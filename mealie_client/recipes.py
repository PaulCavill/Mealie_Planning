from __future__ import annotations
from typing import TYPE_CHECKING
import re

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



class IngredientSaveError(RuntimeError):
    """Mealie refused to save a recipe after its ingredients were parsed."""


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

    def add_tag(self: "MealieClient", slug: str, tag_name: str) -> dict:
        """Add a tag to a recipe by name, creating the tag if needed."""
        tag = self._get_or_create_tag(tag_name)
        recipe = self.get_recipe(slug)
        existing_tags = recipe.get("tags") or []
        if any(t["name"] == tag_name for t in existing_tags):
            return recipe
        existing_tags.append({"id": tag["id"], "name": tag["name"], "slug": tag["slug"]})
        recipe["tags"] = existing_tags
        self.put(f"/api/recipes/{slug}", json=recipe)
        return self.get_recipe(slug)

    def remove_tag(self: "MealieClient", slug: str, tag_name: str) -> dict:
        """Remove a tag from a recipe by name. No-op if the recipe doesn't have it."""
        recipe = self.get_recipe(slug)
        existing_tags = recipe.get("tags") or []
        kept_tags = [t for t in existing_tags if t["name"] != tag_name]
        if len(kept_tags) == len(existing_tags):
            return recipe
        recipe["tags"] = kept_tags
        self.put(f"/api/recipes/{slug}", json=recipe)
        return self.get_recipe(slug)

    def set_effort_tag(self: "MealieClient", slug: str, effort: int | None) -> None:
        """Set the effort-N tag (1-5) on a recipe, removing any existing effort tags.

        Passing None just removes the existing effort tags.
        """
        effort_names = {f"effort-{i}" for i in range(1, 6)}
        tag = self._get_or_create_tag(f"effort-{effort}") if effort is not None else None
        recipe = self.get_recipe(slug)
        kept_tags = [t for t in (recipe.get("tags") or []) if t["name"] not in effort_names]
        if tag:
            kept_tags.append({"id": tag["id"], "name": tag["name"], "slug": tag["slug"]})
        recipe["tags"] = kept_tags
        self.put(f"/api/recipes/{slug}", json=recipe)

    def _get_or_create_tag(self: "MealieClient", name: str) -> dict:
        return self._get_or_create_named("/api/organizers/tags", name, {"name": name})

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

    def update_recipe_ingredients(self: "MealieClient", slug: str) -> dict:
        """Parse and populate ingredient quantity/unit/food fields using Mealie's NLP parser.

        Steps:
        1. Parse ingredient notes using Mealie's NLP parser
        2. For each parsed food with id=null, create it in the database
        3. Set quantity, unit, food (with valid id), and prep note on ingredient
        4. PUT recipe back

        Skips ingredients with empty notes (they can't be parsed).
        """
        recipe = self.get_recipe(slug)
        ingredients = recipe.get("recipeIngredient", [])

        # Separate ingredients into parseable and non-parseable
        parseable_indices = []
        parseable_notes = []

        for i, ing in enumerate(ingredients):
            note = ing.get("note", "").strip()
            if note:  # Only parse non-empty notes
                parseable_indices.append(i)
                parseable_notes.append(note)

        if not parseable_notes:
            return recipe  # Nothing to parse

        # Parse all non-empty ingredients at once using Mealie's NLP parser
        try:
            parse_results = self.post(
                "/api/parser/ingredients",
                json={"parser": "nlp", "ingredients": parseable_notes}
            )
            # Parser returns a list of {input, confidence, ingredient} objects
            parsed_ings = [r.get("ingredient", {}) for r in (parse_results if isinstance(parse_results, list) else [])]
        except Exception:
            return recipe  # Return unchanged if parsing fails

        # Update only the parseable ingredients with their parsed data
        for idx, parsed_ing in zip(parseable_indices, parsed_ings):
            if not parsed_ing:
                continue

            orig_ing = ingredients[idx]

            # Set quantity from parser
            orig_ing["quantity"] = parsed_ing.get("quantity", 0)

            # Handle unit: if parser returned a unit with id=null, create it first
            unit_obj = parsed_ing.get("unit")
            if unit_obj and isinstance(unit_obj, dict):
                unit_name = unit_obj.get("name", "")
                unit_id = unit_obj.get("id")

                # If unit has no id, create it
                if unit_name and not unit_id:
                    created_unit = self.create_or_get_unit(unit_name)
                    orig_ing["unit"] = created_unit
                else:
                    # Unit already exists, use as-is
                    orig_ing["unit"] = unit_obj
            else:
                orig_ing["unit"] = unit_obj

            # Handle food: if parser returned a food with id=null, create it first
            food_obj = parsed_ing.get("food")
            if food_obj and isinstance(food_obj, dict):
                food_name = food_obj.get("name", "")
                food_id = food_obj.get("id")

                # If food has no id, create it
                if food_name and not food_id:
                    created_food = self.create_or_get_food(food_name)
                    orig_ing["food"] = created_food
                else:
                    # Food already exists or is incomplete, use as-is
                    orig_ing["food"] = food_obj

            # Set note to just the prep instructions (parser separates quantity/unit/food/note)
            orig_ing["note"] = parsed_ing.get("note", "")

        # PUT the updated recipe back
        try:
            self.put(f"/api/recipes/{slug}", json=recipe)
        except Exception as e:
            raise IngredientSaveError(
                f"Parsed the ingredients for '{recipe.get('name')}' ({slug}) but Mealie rejected the save: {e}. "
                "Check the recipe in the Mealie UI and parse it there if needed."
            ) from e
        return recipe
