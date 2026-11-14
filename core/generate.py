"""Claude recipe generation, and importing the generated recipes into Mealie.

Claude writes full schema.org/Recipe JSON for each meal, which Mealie imports
directly via /api/recipes/create/html-or-json — no URL scraping involved.
"""
from __future__ import annotations

import json
import os
from typing import Callable

from mealie_client import MealieClient
from . import images
from .errors import GenerationError
from .settings import Settings

Progress = Callable[[str], None]

# The prompt lists recipes to avoid: everything generated so far in this run
# (always), then as many library names as fit under this cap.
MAX_LIBRARY_NAMES_IN_PROMPT = 150

# Room for a full batch (settings allow up to 10 recipes per request).
MAX_OUTPUT_TOKENS = 16000


def _valid_effort(recipe: dict) -> int | None:
    effort = recipe.get("effort")
    return effort if isinstance(effort, int) and 1 <= effort <= 5 else None


def _parse_recipe_array(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise GenerationError(f"Claude did not return valid JSON: {e}") from e
    if not isinstance(data, list):
        raise GenerationError("Claude did not return a JSON array of recipes")
    return [r for r in data if isinstance(r, dict) and r.get("name")]


def _generate_batch(ai_client, settings: Settings, batch: int, existing: list[str], restrictions: str) -> list[dict]:
    existing_str = ", ".join(existing) if existing else "none"
    user_msg = f"Generate exactly {batch} dinner recipes. Return ONLY the JSON array, no other text."
    if restrictions:
        user_msg += f" Additional restriction: {restrictions}."
    try:
        response = ai_client.messages.create(
            model=settings.claude_model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=settings.render_system_prompt(existing_str),
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        raise GenerationError(_describe_api_error(e, settings)) from e
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise GenerationError(
            f"Claude's answer was cut off: {batch} recipes did not fit in one reply. "
            "Lower 'Recipes per request' in Settings and try again.")
    text = next((getattr(block, "text", None) for block in (response.content or [])
                 if getattr(block, "text", None)), None)
    if not text:
        raise GenerationError("Claude returned an empty answer.")
    return _parse_recipe_array(text)


def _describe_api_error(error: Exception, settings: Settings) -> str:
    """Plain words for a failed Claude call. Matched on the SDK's class names
    so this module does not need the SDK just to describe an error."""
    names = {cls.__name__ for cls in type(error).__mro__}
    if "AuthenticationError" in names or "PermissionDeniedError" in names:
        return "Claude rejected the API key. Check ANTHROPIC_API_KEY and restart the app."
    if "NotFoundError" in names:
        return (f"Claude does not know the model '{settings.claude_model}'. "
                "Check the model name in Settings.")
    if "RateLimitError" in names:
        return "Claude is rate limiting this API key. Wait a minute and try again."
    if "APITimeoutError" in names or "APIConnectionError" in names:
        return "Could not reach Claude. Check the app's internet connection and try again."
    if "APIStatusError" in names:
        status = getattr(error, "status_code", "")
        return f"Claude returned an error ({status}). Try again in a moment."
    return f"The request to Claude failed ({type(error).__name__})."


def generate_recipes(
    settings: Settings,
    existing_names: list[str],
    count: int | None = None,
    restrictions: str = "",
    progress: Progress | None = None,
    ai_client=None,
) -> list[dict]:
    """Ask Claude for `count` dinner recipes (default settings.suggest_count).

    Returns schema.org/Recipe dicts, each with an extra integer `effort` 1-5.
    Nothing is written to Mealie. May return fewer than `count` if Claude
    repeats itself, or if a later batch fails: the recipes from earlier
    batches are kept and the failure is reported through `progress`. A
    failure before anything was generated raises GenerationError. `ai_client` is an anthropic.Anthropic instance; by default
    one is created from ANTHROPIC_API_KEY.
    """
    count = settings.suggest_count if count is None else count
    if ai_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise GenerationError("ANTHROPIC_API_KEY is not set")
        import anthropic
        ai_client = anthropic.Anthropic(api_key=api_key)

    if progress:
        progress(f"Asking Claude to generate {count} recipes (in batches of {settings.batch_size})...")
    recipes: list[dict] = []
    seen: set[str] = set()
    remaining = count
    while remaining > 0:
        batch = min(remaining, settings.batch_size)
        exclude = [r["name"] for r in recipes] + list(existing_names)[:MAX_LIBRARY_NAMES_IN_PROMPT]
        try:
            generated = _generate_batch(ai_client, settings, batch, exclude, restrictions)
        except GenerationError as e:
            if not recipes:
                raise
            if progress:
                progress(f"Stopped early, keeping the {len(recipes)} recipes generated so far. {e}")
            break
        for recipe in generated:
            if recipe["name"].lower() not in seen:
                recipes.append(recipe)
                seen.add(recipe["name"].lower())
        remaining -= batch
        if progress:
            progress(f"...{len(recipes)}/{count} generated")
    return recipes


def _create_from_json(client: MealieClient, recipe: dict) -> dict:
    slug = client.post("/api/recipes/create/html-or-json", json={"data": json.dumps(recipe)})
    slug = slug.strip('"') if isinstance(slug, str) else slug
    return client.get_recipe(slug)


def import_recipes(
    client: MealieClient,
    settings: Settings,
    recipes: list[dict],
    progress: Progress | None = None,
) -> list[dict]:
    """Import generated recipes into Mealie: create, tag dinner + effort, find a
    cover image, parse ingredients. Recipes whose name already exists are skipped.

    Returns one dict per recipe: {name, status: "imported"|"skipped"|"failed",
    slug, cuisine, effort, source_url, error}. A "failed" recipe with a slug was
    created in Mealie but a later step (e.g. ingredient parsing) failed.
    """
    existing = {r["name"].lower() for r in client.list_recipes()}
    results = []
    with client.cached_lookups():
        for recipe in recipes:
            name = recipe.get("name", "Unknown")
            cuisine = recipe.get("recipeCuisine", "")
            effort = _valid_effort(recipe)
            source_url = recipe.get("url")
            result = {"name": name, "status": "imported", "slug": None, "cuisine": cuisine,
                      "effort": effort, "source_url": source_url, "error": None}
            results.append(result)

            if name.lower() in existing:
                result["status"] = "skipped"
                if progress:
                    progress(f"SKIP: '{name}' — already exists in Mealie")
                continue

            try:
                imported = _create_from_json(client, recipe)
                slug = result["slug"] = imported.get("slug")
                result["name"] = imported.get("name", name)
                existing.add(name.lower())
                client.add_tag(slug, "dinner")
                if effort:
                    client.set_effort_tag(slug, effort)
                images.auto_set_image(client, slug, name, recipe.get("recipeIngredient", []))
                client.update_recipe_ingredients(slug)
                if progress:
                    effort_str = f" effort-{effort}" if effort else ""
                    source_note = f" [source: {source_url}]" if source_url else ""
                    progress(f"OK: '{result['name']}' ({cuisine}){effort_str}{source_note}")
            except Exception as e:
                result["status"] = "failed"
                result["error"] = str(e)
                if progress:
                    progress(f"FAIL: {name} — {e}")
    return results
