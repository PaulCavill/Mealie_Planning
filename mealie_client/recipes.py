from __future__ import annotations
from typing import TYPE_CHECKING
import requests

if TYPE_CHECKING:
    from .client import MealieClient



class RecipesMixin:
    """Recipe operations mixed into MealieClient."""

    def list_recipes(self: "MealieClient", page: int = 1, per_page: int = 50, tag: str = None) -> list[dict]:
        params = {"page": page, "perPage": per_page}
        if tag:
            params["tags"] = tag
        data = self.get("/api/recipes", **params)
        return data.get("items", [])

    def list_dinner_recipes(self: "MealieClient", per_page: int = 200) -> list[dict]:
        return self.list_recipes(per_page=per_page, tag="dinner")

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
        # Ensure tag exists
        tag = self._get_or_create_tag(tag_name)
        recipe = self.get_recipe(slug)
        existing_tags = recipe.get("tags") or []
        if any(t["name"] == tag_name for t in existing_tags):
            return recipe
        existing_tags.append({"id": tag["id"], "name": tag["name"], "slug": tag["slug"]})
        recipe["tags"] = existing_tags
        self.put(f"/api/recipes/{slug}", json=recipe)
        return self.get_recipe(slug)

    def set_effort_tag(self: "MealieClient", slug: str, effort: int) -> None:
        """Set the effort-N tag (1-5) on a recipe, removing any existing effort tags."""
        effort_names = {f"effort-{i}" for i in range(1, 6)}
        tag = self._get_or_create_tag(f"effort-{effort}")
        recipe = self.get_recipe(slug)
        kept_tags = [t for t in (recipe.get("tags") or []) if t["name"] not in effort_names]
        kept_tags.append({"id": tag["id"], "name": tag["name"], "slug": tag["slug"]})
        recipe["tags"] = kept_tags
        self.put(f"/api/recipes/{slug}", json=recipe)

    def _get_or_create_tag(self: "MealieClient", name: str) -> dict:
        data = self.get("/api/organizers/tags", perPage=100)
        for t in data.get("items", []):
            if t["name"].lower() == name.lower():
                return t
        return self.post("/api/organizers/tags", json={"name": name})

    def add_cover_image(self: "MealieClient", slug: str, recipe_name: str,
                        cuisine: str = "", ingredients: list[str] | None = None) -> bool:
        """Search DuckDuckGo for a food photo and upload it as the recipe cover."""
        from ddgs import DDGS
        ingredient_hint = ""
        if ingredients:
            def _ingredient_str_local(i):
                if isinstance(i, dict):
                    food = i.get("food")
                    return food.get("name", "") if isinstance(food, dict) else str(food or "")
                return str(i)
            hints = [_ingredient_str_local(i).split(",")[0].split()[-1]
                     for i in ingredients[:2] if _ingredient_str_local(i)]
            if hints:
                ingredient_hint = " " + " ".join(hints)
        query = f"{recipe_name}{ingredient_hint} food recipe"
        try:
            results = DDGS().images(query, max_results=10)
        except Exception:
            return False

        for result in results:
            image_url = result.get("image", "")
            if not image_url.startswith("http"):
                continue
            try:
                img = requests.get(image_url, allow_redirects=True, timeout=15,
                                   headers={"User-Agent": "Mozilla/5.0"})
                img.raise_for_status()
                content_type = img.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    continue
                if len(img.content) < 20_000:  # skip tiny/placeholder images
                    continue
                ext = "png" if "png" in content_type else "jpg"
                self.put_multipart(
                    f"/api/recipes/{slug}/image",
                    files={
                        "image": (f"cover.{ext}", img.content, content_type),
                        "extension": (None, ext),
                    },
                )
                return True
            except Exception:
                continue
        return False

    def search_recipes(self: "MealieClient", query: str) -> list[dict]:
        data = self.get("/api/recipes", search=query, perPage=20)
        return data.get("items", [])
