"""Recipe library operations: listing, tagging, ingredient parsing, URL import."""
from __future__ import annotations

from typing import Callable

from mealie_client import MealieClient
from mealie_client.recipes import tag_key
from .errors import CoreError

Progress = Callable[[str], None]

DINNER_TAG = "dinner"
EFFORT_TAGS = {f"effort-{i}": i for i in range(1, 6)}


def effort_of(recipe: dict) -> int | None:
    """Effort 1-5 from a recipe's effort-N tag, or None if unrated."""
    for tag in recipe.get("tags") or []:
        effort = EFFORT_TAGS.get(tag_key(tag))
        if effort:
            return effort
    return None


def summarize(recipe: dict) -> dict:
    """The fields the UIs need from a Mealie recipe (summary or full)."""
    tags = [t.get("name", "") for t in recipe.get("tags") or []]
    return {
        "id": recipe.get("id"),
        "slug": recipe.get("slug"),
        "name": recipe.get("name"),
        "servings": recipe.get("recipeServings") or None,
        # Mealie identifies a tag by its slug, so "Dinner" is the dinner tag too.
        "is_dinner": any(tag_key(t) == DINNER_TAG for t in recipe.get("tags") or []),
        "effort": effort_of(recipe),
        "tags": tags,
        "image": recipe.get("image") or None,  # Mealie's image version token; None = no image
    }


def list_recipes(client: MealieClient, dinner_only: bool = False) -> list[dict]:
    """Every recipe in the library as summarize() dicts, sorted by name."""
    recipes = client.list_dinner_recipes() if dinner_only else client.list_recipes()
    return sorted((summarize(r) for r in recipes), key=lambda r: r["name"] or "")


def set_dinner(client: MealieClient, slug: str, is_dinner: bool = True) -> dict:
    """Add or remove the dinner tag. Returns the updated summary."""
    if is_dinner:
        recipe = client.add_tag(slug, DINNER_TAG)
    else:
        recipe = client.remove_tag(slug, DINNER_TAG)
    return summarize(recipe)


def set_effort(client: MealieClient, slug: str, effort: int | None) -> dict:
    """Rate a recipe 1-5, or clear its rating with None. Returns the updated summary."""
    if effort is not None and effort not in EFFORT_TAGS.values():
        raise ValueError("effort must be between 1 and 5")
    client.set_effort_tag(slug, effort)
    return summarize(client.get_recipe(slug))


def parse_ingredients(client: MealieClient, slug: str, name: str | None = None) -> dict:
    """Parse one recipe's free-text ingredient lines into quantity/unit/food.

    Returns {slug, name, status, ingredients, error}. status is "parsed"
    (`ingredients` lines were converted), "unchanged" (nothing needed parsing,
    nothing was written) or "failed" (see `error`).
    """
    try:
        outcome = client.update_recipe_ingredients(slug)
    except Exception as e:
        return {"slug": slug, "name": name or slug, "status": "failed", "ingredients": 0, "error": str(e)}
    return {"slug": slug, "name": outcome.get("name") or name or slug, "status": outcome["status"],
            "ingredients": outcome["parsed"], "error": None}


PARSE_MARKS = {"parsed": "✓", "unchanged": "–", "failed": "✗"}


def parse_all_ingredients(client: MealieClient, progress: Progress | None = None) -> dict:
    """Parse every recipe. Returns {results: [...], parsed: n, unchanged: n, failed: n}."""
    recipes = sorted(client.list_recipes(), key=lambda r: r["name"])
    if progress and recipes:
        progress(f"Parsing ingredients for {len(recipes)} recipes...")
    results = []
    with client.cached_lookups():
        for i, recipe in enumerate(recipes, 1):
            result = parse_ingredients(client, recipe["slug"], recipe["name"])
            results.append(result)
            if progress:
                detail = " (nothing to parse)" if result["status"] == "unchanged" else ""
                progress(f"{i:>3}. {PARSE_MARKS[result['status']]} {recipe['name']}{detail}")
    counts = {status: sum(1 for r in results if r["status"] == status) for status in PARSE_MARKS}
    return {"results": results, **counts}


class ScrapeError(CoreError):
    pass


def scrape_url(client: MealieClient, url: str, dry_run: bool = False) -> dict:
    """Import a recipe from a URL with Mealie's scraper, or just test the scrape.

    Returns {url, dry_run, recipe}: the imported recipe, or with dry_run the
    scraped data Mealie would import. Raises if Mealie can't scrape the URL.
    """
    recipe = client.test_scrape(url) if dry_run else client.scrape_recipe(url)
    # Mealie answers a failed test scrape with HTTP 200 and a sentence, not an error.
    if not isinstance(recipe, dict) or not str(recipe.get("name") or "").strip():
        detail = f" Mealie said: {recipe.strip()[:200]}" if isinstance(recipe, str) and recipe.strip() else ""
        raise ScrapeError(f"Mealie could not find a recipe on that page.{detail}")
    return {"url": url, "dry_run": dry_run, "recipe": recipe}


def scrape_urls(
    client: MealieClient,
    urls: list[str],
    dry_run: bool = False,
    progress: Progress | None = None,
) -> list[dict]:
    """scrape_url() for each URL. Returns [{url, status: "ok"|"error", recipe, error}]."""
    results = []
    for url in urls:
        try:
            recipe = scrape_url(client, url, dry_run=dry_run)["recipe"]
            results.append({"url": url, "status": "ok", "recipe": recipe, "error": None})
            if progress:
                if dry_run:
                    progress(f"[dry-run] Scrape OK: {url}")
                else:
                    progress(f"Imported '{recipe['name']}' ({recipe['slug']}) [source: {url}]")
        except Exception as e:
            results.append({"url": url, "status": "error", "recipe": None, "error": str(e)})
            if progress:
                progress(f"ERROR importing {url}: {e}")
    return results
