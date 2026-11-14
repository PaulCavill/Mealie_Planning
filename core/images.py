"""Recipe cover images: DuckDuckGo search and upload to Mealie."""
from __future__ import annotations

import requests

from mealie_client import MealieClient
from .errors import ImageSearchError

MIN_AUTO_IMAGE_BYTES = 20_000  # auto-pick skips tiny/placeholder images
_DOWNLOAD_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _ingredient_text(ingredient) -> str:
    if isinstance(ingredient, dict):
        food = ingredient.get("food")
        return food.get("name", "") if isinstance(food, dict) else str(food or "")
    return str(ingredient)


def image_query(recipe_name: str, ingredients: list | None = None) -> str:
    """Search phrase: the recipe name plus a keyword from each of the first two ingredients."""
    hints = []
    for ingredient in (ingredients or [])[:2]:
        words = _ingredient_text(ingredient).split(",")[0].split()
        if words:
            hints.append(words[-1])
    hint = " " + " ".join(hints) if hints else ""
    return f"{recipe_name}{hint} food recipe"


def search_image_candidates(
    recipe_name: str,
    ingredients: list | None = None,
    max_results: int = 10,
) -> list[dict]:
    """Search DuckDuckGo for cover images without downloading or uploading anything.

    `ingredients` may be strings or Mealie ingredient dicts. Returns
    [{url, thumbnail, title, source, width, height}]; thumbnail may be None.
    Raises ImageSearchError if the search itself fails.
    """
    from ddgs import DDGS

    try:
        results = DDGS().images(image_query(recipe_name, ingredients), max_results=max_results)
    except Exception as e:
        raise ImageSearchError(f"Image search failed: {e}") from e

    candidates = []
    for result in results or []:
        url = result.get("image") or ""
        if not url.startswith("http"):
            continue
        candidates.append({
            "url": url,
            "thumbnail": result.get("thumbnail") or None,
            "title": result.get("title") or "",
            "source": result.get("url") or result.get("source") or "",
            "width": result.get("width"),
            "height": result.get("height"),
        })
    return candidates


def _download_image(url: str, default_content_type: str = "") -> tuple[bytes, str] | None:
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        resp = requests.get(url, allow_redirects=True, timeout=15, headers=_DOWNLOAD_HEADERS)
        resp.raise_for_status()
    except Exception:
        return None
    content_type = resp.headers.get("content-type", default_content_type)
    if not content_type.startswith("image/"):
        return None
    return resp.content, content_type


def set_image_from_url(client: MealieClient, slug: str, url: str) -> bool:
    """Download `url` and make it the recipe's cover image.

    Returns False if the download fails, the URL isn't an image, or the upload fails.
    """
    image = _download_image(url, default_content_type="image/jpeg")
    if image is None:
        return False
    try:
        client.upload_recipe_image(slug, *image)
    except Exception:
        return False
    return True


def auto_set_image(
    client: MealieClient,
    slug: str,
    recipe_name: str,
    ingredients: list | None = None,
) -> bool:
    """Search and upload the first usable result (a real image of at least 20 KB).

    Returns False if the search fails or nothing usable is found.
    """
    try:
        candidates = search_image_candidates(recipe_name, ingredients)
    except Exception:
        return False
    for candidate in candidates:
        image = _download_image(candidate["url"])
        if image is None or len(image[0]) < MIN_AUTO_IMAGE_BYTES:
            continue
        try:
            client.upload_recipe_image(slug, *image)
        except Exception:
            continue
        return True
    return False
