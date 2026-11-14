"""Images: find or paste a new cover image for a recipe."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request

from core import images as image_ops, recipes as recipe_ops
from core.errors import ImageSearchError
from .. import deps
from .recipes import search

router = APIRouter()

LIST_LIMIT = 60
MAX_CANDIDATES = 12


@router.get("/images")
def images_page(request: Request, q: str = "", missing: str = ""):
    everything = recipe_ops.list_recipes(deps.get_client())
    without = [r for r in everything if not r["image"]]
    found = search(without if missing else everything, q)
    context = {
        "active": "images", "q": q, "missing": bool(missing),
        "recipes": found[:LIST_LIMIT], "total": len(found), "more": max(0, len(found) - LIST_LIMIT),
        "missing_count": len(without), "library_size": len(everything),
    }
    return deps.render(request, "images/_results.html" if deps.is_htmx(request) else "images/index.html", context)


def _detail(client, slug: str) -> dict:
    recipe = client.get_recipe(slug)
    return {"id": recipe.get("id"), "slug": recipe.get("slug") or slug, "name": recipe.get("name") or slug,
            "image": recipe.get("image") or None, "ingredients": recipe.get("recipeIngredient") or []}


@router.get("/images/{slug}")
def image_detail(request: Request, slug: str):
    recipe = _detail(deps.get_client(), slug)
    return deps.render(request, "images/detail.html", {"active": "images", "recipe": recipe, "message": None})


@router.get("/images/{slug}/search")
def image_search(request: Request, slug: str, q: str = ""):
    recipe = _detail(deps.get_client(), slug)
    words = " ".join(q.split())
    error, candidates = None, []
    try:
        if words and words.lower() != recipe["name"].lower():
            candidates = image_ops.search_image_candidates(words, max_results=MAX_CANDIDATES)
        else:
            candidates = image_ops.search_image_candidates(recipe["name"], recipe["ingredients"],
                                                           max_results=MAX_CANDIDATES)
    except (ImageSearchError, ImportError):
        error = ("The image search isn't available right now — the search service could not be reached. "
                 "Try again later, or paste an image address below.")
    return deps.render(request, "images/_candidates.html",
                       {"recipe": recipe, "candidates": candidates, "error": error, "searched": True})


@router.post("/images/{slug}/set")
def image_set(request: Request, slug: str, url: str = Form("")):
    client = deps.get_client()
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        message = ("error", "Paste the full address of an image, starting with http:// or https://.")
    elif image_ops.set_image_from_url(client, slug, url):
        message = ("ok", "Image updated.")
    else:
        message = ("error", "That image could not be used: the download failed, it isn't an image, "
                            "or Mealie refused it. Try another one.")
    recipe = _detail(client, slug)
    context = {"active": "images", "recipe": recipe, "message": message}
    if deps.is_htmx(request):
        return deps.render(request, "images/_current.html", context)
    return deps.render(request, "images/detail.html", context)
