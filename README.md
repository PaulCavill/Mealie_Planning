# Mealie AI Meal Planner

An AI-powered meal planning tool for [Mealie](https://mealie.io). Uses Claude to generate family-friendly dinner recipes, import them into your Mealie instance, and build fortnightly meal plans — with effort-based rules so weeknight meals stay quick.

## Features

- **Recipe generation** — Claude generates full recipes (ingredients, steps, times) and imports them directly into Mealie. No URL scraping.
- **Cover images** — DuckDuckGo image search finds a relevant photo for each recipe automatically.
- **Effort rating** — Recipes are tagged `effort-1` (quick) through `effort-5` (time-intensive). Claude rates new recipes on import; existing ones can be rated interactively or auto-rated in bulk.
- **Smart planning** — Fortnightly dinner plans with per-day effort rules: easy meals on Monday/Wednesday, anything goes on Saturday/Sunday/Tuesday.
- **Day replacement** — Replace any planned meal interactively, with duplicate checking across the full upcoming plan.

## Household rules

| Day | Rule |
|-----|------|
| Saturday, Sunday, Monday, Tuesday, Wednesday | Dinner recipe from Mealie library |
| Thursday | Takeaways (fixed note) |
| Friday | Make your own meals (fixed note) |

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
# Clone and enter the project
cd Mealie

# Create virtualenv and install dependencies
uv venv .venv
uv pip install -r requirements.txt
uv pip install anthropic ddgs
```

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```ini
MEALIE_BASE_URL=http://your-mealie-instance:9090
MEALIE_TOKEN=your_long_lived_api_token
ANTHROPIC_API_KEY=sk-ant-...
```

> Get a Mealie API token from **Profile → API Tokens** in the Mealie UI. Long-lived tokens are recommended.

## Usage

All commands are run via `main.py`:

```bash
.venv/bin/python main.py <command> [options]
```

### `suggest` — Generate and import recipes

Ask Claude to generate dinner recipes and import them into Mealie. Each recipe is automatically tagged `dinner`, rated for effort, and given a cover photo.

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

### `plan` — Create a fortnightly meal plan

Picks dinner recipes from your library and creates entries in Mealie for the next two fortnights. Respects effort rules per day and avoids repeating recipes within a plan.

```bash
.venv/bin/python main.py plan

# Override the start date
.venv/bin/python main.py plan --start 2026-06-07

# Also create shopping lists in Mealie
.venv/bin/python main.py plan --shopping-list
```

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

### `scrape` — Import a recipe from a URL

```bash
.venv/bin/python main.py scrape https://example.com/recipe

# Import from a file of URLs (one per line)
.venv/bin/python main.py scrape --file urls.txt
```

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
└── mealie_client/       # Mealie API client package
    ├── __init__.py
    ├── client.py        # Base client (auth, get/post/put/delete, multipart upload)
    ├── recipes.py       # Recipe CRUD, tagging, effort tags, cover images
    ├── meal_plans.py    # Meal plan CRUD
    └── shopping.py      # Shopping list operations
```

## How recipe generation works

1. Claude Haiku generates `N` recipes as schema.org/Recipe JSON, batched in groups of 5 to stay within token limits.
2. Each recipe includes an `effort` rating (1–5) assigned by Claude based on complexity.
3. Recipes are posted directly to Mealie via `POST /api/recipes/create/html-or-json` — no URL scraping, 100% import success rate.
4. Each recipe is tagged `dinner` and `effort-N`.
5. A cover photo is fetched from DuckDuckGo image search using the recipe name and key ingredients.

## Notes

- Mealie does not preserve `prepTime`/`cookTime`/`totalTime` from schema.org JSON after import. Effort is rated by Claude from the recipe content, not parsed from durations.
- The `dinner` tag is used to filter the recipe library for planning. Non-dinner recipes (oats, sauces, biscuits) won't appear in meal plans.
- The `replace` command checks the full 6-week upcoming plan before confirming a swap, to avoid duplicating a recipe already planned on another day.
