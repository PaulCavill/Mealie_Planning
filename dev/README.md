# Fake Mealie (dev only)

`fake_mealie.py` is a single-file, in-memory imitation of the parts of the
Mealie v2/v3 REST API this project uses. Use it for local development, automated
tests and screenshots when no real Mealie instance is around.

## Run

```bash
pip install -r dev/requirements.txt
python dev/fake_mealie.py --port 9925              # 56 seeded recipes
python dev/fake_mealie.py --port 9925 --recipes 450   # big library, for pagination
```

Point the tool at it:

```bash
MEALIE_BASE_URL=http://127.0.0.1:9925 MEALIE_TOKEN=dev python main.py ...
```

- Any non-empty `Authorization: Bearer ...` token is accepted; without one `/api/*` returns 401.
  (`POST /api/auth/token` with any username/password also hands out a token.)
- `POST /__reset` (no auth) restores the seed state. Nothing is persisted to disk.
- In-process for tests: `from fake_mealie import create_app` then `TestClient(create_app())`
  (`create_app(recipes=450)` for the large library).
- `python dev/smoke_test.py` drives a running server through the real `mealie_client`.
- Interactive docs: `http://127.0.0.1:9925/__docs`.
- Unknown `/api/...` routes return **501** and are logged as `UNIMPLEMENTED`, so gaps are obvious.

## Seed data

Deterministic (fixed RNG seed), apart from dates, which are relative to the day the server starts/resets.

- 50 dinner recipes tagged `dinner`; 44 also have an `effort-1..5` tag, 6 are unrated. Some also carry
  `kid-friendly` / `vegetarian`.
- 6 non-dinner recipes (Overnight Oats, Anzac Biscuits, ...) without the `dinner` tag.
- Most recipes have structured ingredients (quantity / unit / food) plus the odd note-only line
  ("Salt and pepper, to taste"). 10 recipes are fully note-only, like a fresh un-parsed import
  (e.g. Vegetable Lasagne, Beef Rendang, Chicken Katsu Curry) - good targets for `update_recipe_ingredients`.
- 3 recipes have `image: null` (Pikelets, Venison Sausage Cassoulet, Smoked Fish Kedgeree).
- Meal plan: a dinner on each of the last 21 days (two are recipe-less: "Takeaways", "Leftovers"; two extra
  lunch entries), plus 3 dinners on the Saturday on/after today and the following Monday and Wednesday
  (the planner's Sat-Wed block), so "already planned" paths trigger.
- One shopping list, "Weekly shop": two recipes' ingredients, three free-text items, one checked item.
- `--recipes N`: the base set is repeated with name prefixes ("Quick ...", "Slow-Cooker ...") up to N.

## Endpoints

| Area | Endpoints |
|---|---|
| Auth | `POST /api/auth/token` |
| Recipes | `GET /api/recipes` (`page`, `perPage`, `tags`, `requireAllTags`, `search`, `orderBy`, `orderDirection`), `GET/PUT/PATCH/DELETE /api/recipes/{slug}`, `POST /api/recipes`, `POST /api/recipes/create/url`, `POST /api/recipes/test-scrape-url`, `POST /api/recipes/create/html-or-json`, `PUT /api/recipes/{slug}/image` |
| Images | `GET /api/media/recipes/{id}/images/{original,min-original,tiny-original}.webp` (no auth) |
| Organizers | `GET/POST /api/organizers/tags`, `GET /api/organizers/tags/{id}` |
| Foods / units | `GET/POST /api/foods`, `GET/POST /api/units` |
| Parser | `POST /api/parser/ingredients`, `POST /api/parser/ingredient` |
| Meal plans | `GET/POST /api/households/mealplans` (`start_date`, `end_date`), `GET/PUT/DELETE /api/households/mealplans/{id}`, `POST /api/households/mealplans/random`, `GET /api/households/mealplans/today` |
| Shopping | `GET/POST /api/households/shopping/lists`, `GET/PUT/DELETE /api/households/shopping/lists/{id}`, `POST /api/households/shopping/lists/{id}/recipe`, `GET/POST /api/households/shopping/items` (`shoppingListId`), `GET/PUT/DELETE /api/households/shopping/items/{id}` |
| Misc | `GET /api/app/about` (no auth), `GET /api/users/self`, `POST /__reset` |

All list endpoints return Mealie's envelope (`page`, `per_page`, `total`, `total_pages`, `items`, `next`,
`previous`) and honour `page` / `perPage` (default 50, `-1` = everything).

## Where it differs from real Mealie (known or guessed)

- **Images** are SVG placeholders served under the `.webp` names with `Content-Type: image/svg+xml`; an uploaded
  image is served byte-for-byte with its own content type for all three sizes (no resizing / webp conversion).
  Real Mealie returns 404 for a recipe with no image; the fake always serves a placeholder, even when `image` is null.
- **Scraping** has no network: `create/url` and `test-scrape-url` invent a recipe named after the last URL path
  segment. A URL containing `fail` in host or path returns 400. `create/html-or-json` understands JSON (or JSON-LD
  embedded in HTML) only.
- **Ingredient parser** is a regex, not Mealie's NLP/CRF model: `<qty> [unit] <food>[, note]`, fractions, ranges,
  parentheses. Confidence numbers are made up.
- **PUT recipe** rejects (400) an ingredient whose `food`/`unit` object has no known `id`. Real Mealie also fails
  here, but the exact status/body is a guess. Unknown tags with a `name` are created on the fly (real Mealie is stricter).
- **Duplicate names** on `POST /api/foods|units|organizers/tags` return 400 (real Mealie has unique constraints;
  the error body shape is a guess).
- **`GET /households/shopping/items?shoppingListId=`** filters by list. Real Mealie filters via
  `queryFilter=shoppingListId=<id>` and may ignore the bare parameter (returning every list's items). The fake accepts both.
- **`POST /households/shopping/items`** returns `{createdItems, updatedItems, deletedItems}` and merges into an
  existing unchecked item with the same food+unit (or same note when there is no food). Merge rules for
  `lists/{id}/recipe` are the same; real Mealie's rules for combining notes are subtler.
- **`POST /households/mealplans/random`** accepts an empty body (defaults: today, `dinner`); real Mealie requires
  `date` and `entryType` and applies the household's meal plan rules.
- **Search** is a plain substring match on name, description and ingredients (real Mealie is fuzzy/tokenised).
  `queryFilter` is ignored on every endpoint except the shopping items one above.
- Meal plan entries are returned sorted by date; shopping items by checked, then position.
- Not implemented at all: categories, tools, cookbooks, labels, comments, timeline events, households/groups admin,
  users, backups, bulk actions, exports, recipe assets, shopping item bulk endpoints, API-token management.
