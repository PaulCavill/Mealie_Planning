"""
AI-powered meal suggester.
Has Claude generate full schema.org/Recipe JSON for each meal,
then posts them directly to Mealie via /api/recipes/create/html-or-json.
No URL scraping — 100% reliable imports.
"""
import os
import json
import anthropic
from mealie_client import MealieClient

HOUSEHOLD = "2 adults and 1 child (approx. 8 years old), based in New Zealand"

SYSTEM_PROMPT = """You are a meal planning assistant. Generate detailed dinner recipes in schema.org/Recipe JSON-LD format.

Rules:
- Family-friendly for {household}
- Vary cuisines: Asian, Italian, Mexican, Middle Eastern, Kiwi/Aussie, etc.
- Practical: 30-60 min cook time, ingredients available at NZ supermarkets
- Flavourful but not too spicy — meals kids enjoy
- Realistic ingredient quantities for 4 servings
- Clear, numbered step-by-step instructions
- Avoid recipes already in the library: {existing}

Return ONLY a JSON array of schema.org/Recipe objects. Each must include:
  @context, @type, name, description, recipeYield, prepTime, cookTime, totalTime,
  recipeIngredient (array of strings), recipeInstructions (array of HowToStep objects),
  recipeCategory, recipeCuisine, keywords, effort

effort is an integer 1-5 rating the total time and complexity:
  1 = very quick, ≤20 min, minimal prep (e.g. simple stir-fry, pasta aglio e olio)
  2 = easy, 20-35 min, basic techniques
  3 = moderate, 35-50 min, some prep or multiple components
  4 = involved, 50-75 min, marinating/slow cooking/several steps
  5 = time-intensive, 75+ min or complex technique (e.g. slow braise, homemade pastry)

Example of one item:
{{
  "@context": "https://schema.org",
  "@type": "Recipe",
  "name": "Butter Chicken",
  "description": "A rich, creamy Indian curry the whole family will love.",
  "recipeYield": "4 servings",
  "prepTime": "PT15M",
  "cookTime": "PT30M",
  "totalTime": "PT45M",
  "recipeCuisine": "Indian",
  "recipeCategory": "Dinner",
  "keywords": "chicken, curry, family friendly",
  "recipeIngredient": [
    "600g chicken thighs, cut into chunks",
    "1 cup tomato passata",
    "1 cup cream"
  ],
  "recipeInstructions": [
    {{"@type": "HowToStep", "text": "Marinate chicken in yoghurt and spices for 30 minutes."}},
    {{"@type": "HowToStep", "text": "Cook chicken in a hot pan until golden."}}
  ],
  "effort": 3
}}"""


BATCH_SIZE = 5  # recipes per API call to stay within token limits


def _call_claude(client_ai: anthropic.Anthropic, batch: int, existing: list[str], restrictions: str = "") -> list[dict]:
    existing_str = ", ".join(existing[:30]) if existing else "none"
    user_msg = f"Generate exactly {batch} dinner recipes. Return ONLY the JSON array, no other text."
    if restrictions:
        user_msg += f" Additional restriction: {restrictions}."
    response = client_ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        system=SYSTEM_PROMPT.format(household=HOUSEHOLD, existing=existing_str),
        messages=[{
            "role": "user",
            "content": user_msg,
        }],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def generate_recipes(client_ai: anthropic.Anthropic, count: int, existing: list[str], restrictions: str = "") -> list[dict]:
    print(f"  Asking Claude to generate {count} recipes (in batches of {BATCH_SIZE})...")
    all_recipes = []
    remaining = count
    while remaining > 0:
        batch = min(remaining, BATCH_SIZE)
        exclude = existing + [r["name"] for r in all_recipes]
        batch_recipes = _call_claude(client_ai, batch, exclude, restrictions=restrictions)
        seen = {r["name"].lower() for r in all_recipes}
        for r in batch_recipes:
            if r["name"].lower() not in seen:
                all_recipes.append(r)
                seen.add(r["name"].lower())
        remaining -= batch
        print(f"  ...{len(all_recipes)}/{count} generated")
    return all_recipes


def import_recipe_json(mealie: MealieClient, recipe: dict) -> dict:
    """Post a schema.org/Recipe dict directly to Mealie."""
    slug = mealie.post("/api/recipes/create/html-or-json", json={"data": json.dumps(recipe)})
    slug = slug.strip('"') if isinstance(slug, str) else slug
    return mealie.get_recipe(slug)


def run_suggest(count: int = 14, dry_run: bool = False, restrictions: str = "") -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set in .env")

    ai = anthropic.Anthropic(api_key=api_key)
    mealie = MealieClient()

    existing = [r["name"] for r in mealie.list_recipes(per_page=200)]
    recipes = generate_recipes(ai, count=count, existing=existing, restrictions=restrictions)

    print(f"\nImporting {len(recipes)} recipes into Mealie...\n")
    results = []

    for recipe in recipes:
        name = recipe.get("name", "Unknown")
        cuisine = recipe.get("recipeCuisine", "")

        if dry_run:
            print(f"  [dry-run] {name} ({cuisine})")
            results.append({"name": name, "status": "dry-run"})
            continue

        try:
            imported = import_recipe_json(mealie, recipe)
            slug = imported.get("slug")
            mealie.add_tag(slug, "dinner")
            effort = recipe.get("effort")
            if isinstance(effort, int) and 1 <= effort <= 5:
                mealie.set_effort_tag(slug, effort)
            ingredients = recipe.get("recipeIngredient", [])
            mealie.add_cover_image(slug, name, cuisine, ingredients)
            effort_str = f" effort-{effort}" if isinstance(effort, int) and 1 <= effort <= 5 else ""
            print(f"  OK: '{imported.get('name', name)}' ({cuisine}){effort_str}")
            results.append({"name": imported.get("name", name), "status": "imported", "slug": slug})
        except Exception as e:
            print(f"  FAIL: {name} — {e}")
            results.append({"name": name, "status": "failed", "error": str(e)})

    ok = sum(1 for r in results if r["status"] == "imported")
    fail = sum(1 for r in results if r["status"] == "failed")
    skip = sum(1 for r in results if r["status"] in ("skipped", "dry-run"))
    print(f"\nDone: {ok} imported, {fail} failed, {skip} skipped.")
    return results
