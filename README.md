# Mealie AI Meal Planner

An AI-powered meal planning tool for [Mealie](https://mealie.io). Uses Claude to generate family-friendly dinner recipes, import them into your Mealie instance, and build fortnightly meal plans — with effort-based rules so weeknight meals stay quick.

## Features

- **Recipe generation** — Claude generates full recipes (ingredients, steps, times) and imports them directly into Mealie. No URL scraping.
- **Cover images** — DuckDuckGo image search finds a relevant photo for each recipe automatically. Refresh unrelated images anytime with `replace-image`.
- **Effort rating** — Recipes are tagged `effort-1` (quick) through `effort-5` (time-intensive). Claude rates new recipes on import; existing ones can be rated interactively or auto-rated in bulk.
- **Smart planning** — Fortnightly dinner plans with per-day effort rules: easy meals on Monday/Wednesday, anything goes on Saturday/Sunday/Tuesday.
- **Day replacement** — Replace any planned meal interactively, with duplicate checking across the full upcoming plan.
- **Image refresh** — Replace recipe cover images when they don't match the meal, with one command.

## Household rules

| Day | Rule |
|-----|------|
| Saturday, Sunday, Monday, Tuesday, Wednesday | Dinner recipe from Mealie library |

**Effort constraints:**
- Monday, Wednesday → effort ≤ 3
- Saturday, Sunday, Tuesday → unrestricted

**Household:** 2 adults + 1 child (8yo), based in New Zealand. Servings scaled to 2.5. Plans run fortnightly (every 2nd week), starting the next Saturday.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- A running [Mealie](https://mealie.io) instance
- An [Anthropic API key](https://console.anthropic.com)

## Setup

```bash
# Enter the project directory
cd Mealie

# Create virtualenv and install dependencies
uv venv .venv
uv pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Get a Mealie API token from **Profile → API Tokens** in the Mealie UI (long-lived tokens recommended), and an Anthropic API key from [console.anthropic.com](https://console.anthropic.com).

## Usage

All commands are run via `main.py`:

```bash
.venv/bin/python main.py <command> [options]
```

### `suggest` — Generate and import recipes

Ask Claude to generate dinner recipes and import them into Mealie. Each recipe is automatically tagged `dinner`, rated for effort, and given a cover photo. Duplicates are prevented — if a recipe with the same name already exists, it will be skipped.

```bash
# Generate 14 recipes (default)
.venv/bin/python main.py suggest

# Generate a custom number
.venv/bin/python main.py suggest --count 5

# Add a constraint (e.g. no chicken, vegetarian only)
.venv/bin/python main.py suggest --restrict "no chicken"

# Preview without importing
.venv/bin/python main.py suggest --dry-run
```

**Notes:**
- Claude generates recipes with real, publicly accessible source URLs
- Recipes with ingredient parsing failures are still imported; use `parse-ingredients` to retry
- Duplicate recipes are skipped automatically, even if a previous import partially failed

### `plan` — Create a fortnightly meal plan

Picks dinner recipes from your library and creates entries in Mealie for the next two fortnights. Respects effort rules per day, avoids repeating recipes within 40 days, and prevents duplicate entries when run multiple times.

```bash
.venv/bin/python main.py plan

# Override the start date
.venv/bin/python main.py plan --start 2026-06-07

# Also create shopping lists in Mealie
.venv/bin/python main.py plan --shopping-list

# Replace existing meals (deletes old plan and creates new one)
.venv/bin/python main.py plan --start 2026-06-07 --override-plan
```

**Behavior:**
- Skips dates that already have planned dinners (prevents duplicates when run multiple times)
- Excludes recipes used in the last 40 days (ensures variety across planning cycles)
- With `--shopping-list`: deletes old shopping list for that week and creates a fresh one
- With `--shopping-list`: automatically removes pantry staples (salt, pepper) from the generated list

### `replace` — Replace a day's meal

Interactively swap out a planned dinner. Shows what's currently planned, lists available recipes (marking already-planned ones), and commits the change.

```bash
.venv/bin/python main.py replace 2026-05-30

# Only show easy meals (effort ≤ 2)
.venv/bin/python main.py replace 2026-05-30 --effort 2

# Filter the list by name
.venv/bin/python main.py replace 2026-05-30 --search pasta
```

### `week` — Show this week's plan

```bash
.venv/bin/python main.py week
```

### `recipes` — List all recipes

```bash
.venv/bin/python main.py recipes
```

### `tag-dinners` — Tag existing recipes as dinner

Walks through all recipes interactively so you can mark which ones are dinners. Only dinner-tagged recipes appear in the planner.

```bash
.venv/bin/python main.py tag-dinners
```

### `tag-effort` — Rate recipes by effort

Walks through all recipes and lets you set an effort level 1–5. New recipes from `suggest` are auto-rated by Claude; use this to review or correct them.

```bash
.venv/bin/python main.py tag-effort
```

| Level | Meaning |
|-------|---------|
| 1 | Very quick — ≤20 min, minimal prep |
| 2 | Easy — 20–35 min, basic techniques |
| 3 | Moderate — 35–50 min, some prep or components |
| 4 | Involved — 50–75 min, marinating or multiple steps |
| 5 | Time-intensive — 75+ min or complex technique |

### `replace-image` — Replace recipe cover images

Interactively replace recipe cover images by searching DuckDuckGo for better-matching photos. Useful when auto-generated images don't match the recipe or are unrelated.

```bash
# Replace images for all recipes (interactive)
.venv/bin/python main.py replace-image

# Filter by recipe name or slug
.venv/bin/python main.py replace-image --search pork

# Use a specific image URL instead of searching (no prompt when one recipe matches)
.venv/bin/python main.py replace-image --search pork-mince-pasta --url https://example.com/image.jpg

# Batch mode with automation
echo "y" | .venv/bin/python main.py replace-image --search pasta
```

**Options:**
- `--search` — Filter by recipe name or slug
- `--url` — Download and use a specific image URL instead of searching DuckDuckGo. When combined with `--search` that matches exactly one recipe, uploads immediately with no interactive prompt.

**Behavior:**
- Searches DuckDuckGo for images matching the recipe name and key ingredients
- Validates images (minimum size, correct content-type) to avoid placeholders
- Replaces the current cover image with the best match found
- Skips recipes if no suitable image is found

### `scrape` — Import a recipe from a URL

```bash
.venv/bin/python main.py scrape https://example.com/recipe

# Import from a file of URLs (one per line)
.venv/bin/python main.py scrape --file urls.txt
```

### `parse-ingredients` — Parse recipe ingredients

Parses all recipe ingredient notes (e.g., `"500g chicken"`) into structured fields (quantity, unit, food) for better shopping list support in Mealie. Missing units (e.g., "tin", "can") and foods are created automatically during parsing.

```bash
.venv/bin/python main.py parse-ingredients
```

This is called automatically after `suggest` imports recipes, but can be run manually on older recipes.

**Behavior:**
- Uses Mealie's NLP parser to extract quantity, unit, food, and prep notes
- Creates missing units and foods on-the-fly (e.g., "tin" unit is created if missing)
- Sets quantity/unit/food/note fields for proper shopping list ingredient combination
- Skips recipes with data issues and continues batch processing

## Project structure

```
Mealie/
├── main.py              # CLI entry point
├── suggester.py         # Claude recipe generation + Mealie import
├── planner.py           # Fortnightly planner, replace-day, tag-effort
├── scraper.py           # URL-based recipe import
├── env_loader.py        # Minimal .env parser (no external deps)
├── requirements.txt
├── .env.example
├── CLAUDE.md            # Project documentation for Claude
├── README.md            # This file
└── mealie_client/       # Mealie API client package
    ├── __init__.py
    ├── client.py        # Base client (auth, get/post/put/delete, multipart upload)
    ├── recipes.py       # Recipe CRUD, tagging, effort tags, cover images, ingredient parsing
    ├── meal_plans.py    # Meal plan CRUD
    ├── shopping.py      # Shopping list operations
    └── foods.py         # Food & unit creation (on-demand during ingredient parsing)
```

## How recipe generation works

1. Claude Haiku generates `N` recipes as schema.org/Recipe JSON, batched in groups of 5 to stay within token limits.
2. Each recipe includes an `effort` rating (1–5) assigned by Claude based on complexity.
3. Recipes are posted directly to Mealie via `POST /api/recipes/create/html-or-json` — no URL scraping, 100% import success rate.
4. Each recipe is tagged `dinner` and `effort-N`.
5. A cover photo is fetched from DuckDuckGo image search using the recipe name and key ingredients.

## Notes

- **Mealie time fields** — Mealie does not preserve `prepTime`/`cookTime`/`totalTime` from schema.org JSON after import. Effort is rated by Claude from the recipe content, not parsed from durations.
- **Dinner filtering** — The `dinner` tag filters the recipe library for planning. Non-dinner recipes (oats, sauces, biscuits) won't appear in meal plans.
- **Recipe rotation** — The planner excludes recipes used in the last 40 days to ensure variety across planning cycles.
- **Duplicate prevention** — Running `plan` multiple times safely skips dates that already have meals. Use `--override-plan` to replace existing plans. The `suggest` command skips recipes already in your library.
- **Shopping lists** — Old shopping lists for the same week are deleted and recreated when `--shopping-list` is used, ensuring they match the current meal plan. Pantry staples (salt, pepper) are automatically removed from the generated list.
- **Ingredient parsing** — All recipes have ingredient notes (e.g., `"500g chicken"`) automatically parsed into structured quantity/unit fields. Missing units and foods are created on-the-fly during parsing, preventing 500 errors. The `parse-ingredients` command runs automatically after `suggest` imports recipes and can be run manually on older recipes.
- **Source URLs** — Claude-generated recipes include real, publicly accessible source URLs (e.g., allrecipes.com, bbc.co.uk/food). These are stored in the recipe data and printed during import.
