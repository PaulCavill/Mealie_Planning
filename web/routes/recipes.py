"""Recipes: the library with dinner and effort controls that save at once."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request

from core import recipes as recipe_ops
from .. import deps

router = APIRouter()

PAGE_SIZE = 60
FILTERS = [("all", "All"), ("dinner", "Dinner"), ("not_dinner", "Not dinner"), ("unrated", "Unrated dinners")]


def _matches(recipe: dict, which: str) -> bool:
    if which == "dinner":
        return recipe["is_dinner"]
    if which == "not_dinner":
        return not recipe["is_dinner"]
    if which == "unrated":
        return recipe["is_dinner"] and recipe["effort"] is None
    return True


def search(recipes: list[dict], term: str) -> list[dict]:
    words = term.lower().split()
    return [r for r in recipes if all(w in (r["name"] or "").lower() for w in words)]


@router.get("/recipes")
def recipes_page(request: Request, q: str = "", filter: str = "all", page: int = 1):
    if filter not in dict(FILTERS):
        filter = "all"
    everything = recipe_ops.list_recipes(deps.get_client())
    found = search(everything, q)
    counts = {key: sum(1 for r in found if _matches(r, key)) for key, _ in FILTERS}
    shown = [r for r in found if _matches(r, filter)]
    page = max(1, page)
    context = {
        "active": "recipes",
        "q": q, "filter": filter, "filters": FILTERS, "counts": counts,
        "recipes": shown[(page - 1) * PAGE_SIZE: page * PAGE_SIZE],
        "total": len(shown), "library_size": len(everything),
        "page": page, "has_more": page * PAGE_SIZE < len(shown),
    }
    if deps.is_htmx(request):
        # "Show more" appends rows; searching or filtering replaces the results.
        return deps.render(request, "recipes/_rows.html" if page > 1 else "recipes/_results.html", context)
    return deps.render(request, "recipes/index.html", context)


@router.post("/recipes/{slug}/dinner")
def set_dinner(request: Request, slug: str, value: str = Form("")):
    recipe = recipe_ops.set_dinner(deps.get_client(), slug, value == "1")
    return deps.render(request, "recipes/_row.html", {"r": recipe, "saved": True})


@router.post("/recipes/{slug}/effort")
def set_effort(request: Request, slug: str, effort: str = Form("")):
    level = deps.small_int(effort)
    level = level if level is not None and 1 <= level <= 5 else None
    recipe = recipe_ops.set_effort(deps.get_client(), slug, level)
    return deps.render(request, "recipes/_row.html", {"r": recipe, "saved": True})
