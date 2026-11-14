# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

The repository has no git tags yet. The version numbers below are labels for the two eras of the project: the command-line tool (1.0.0) and the web app (2.0.0).

## [Unreleased]

Nothing yet.

## [2.0.0] - 2026-11-14

The command-line scripts became a web app that runs in Docker. The CLI is still there and shares its logic and settings with the web app.

This release has been run against the bundled fake Mealie only. The Docker image was built and run only in the development environment, never against a real Mealie, and nothing has been tested on TrueNAS. See "What has and has not been tested" in the [README](README.md#what-has-and-has-not-been-tested).

### Added

- **Web app** on port 8080 with six pages:
  - **Plan**: a four-week calendar of what is in Mealie; generate a plan, preview it, re-roll a day, choose a specific recipe for a day, then save. Swap the dinner on any upcoming day, or add one to an empty day.
  - **Recipes**: search and filter the library; switch the dinner tag on or off and set or clear the effort rating with one click.
  - **Generate**: ask Claude for 1–50 recipes with an optional restriction, review them and import only the ones you tick. Import up to 25 recipes at a time from web addresses, with a preview-only option.
  - **Images**: list recipes without a cover image, search the web for candidates and pick one, or paste an image address.
  - **Maintenance**: parse every recipe's ingredients, retry individual failures, and see the history of background jobs with their logs.
  - **Settings**: edit every planning and generation rule, and check the connection to Mealie.
- **Plan preview.** A plan is shown before anything is written to Mealie. An unsaved preview is kept for 14 days and offered again on the Plan page. The preview warns when saving would replace a shopping list that already exists in Mealie.
- **Settings stored in a database** instead of in the code: household size and child portion, household description, planned weekdays, week start day, weeks per plan, week interval, effort limit per weekday, repeat gap, swap look-ahead, default servings, pantry staples, Claude model, batch sizes and the system prompt. Defaults match the old hard-coded values.
- **Background jobs** for generating, importing, URL import and ingredient parsing, with progress and a log. A job keeps running if you leave the page.
- **Optional login.** Set `APP_PASSWORD` to require a password on every page. Sessions last 30 days.
- **Docker packaging**: a `Dockerfile` (runs as user and group id 568, data in `/data`, health check on `/healthz`), a `docker-compose.yml` (`PUID`/`PGID` change the user), and a GitHub workflow that runs the tests and publishes an amd64 and arm64 image to `ghcr.io/paulcavill/mealie_planning` on pushes to `main` and on `v*` tags.
- A "can't save anything" page, and an unhealthy `/healthz`, when the `/data` folder is not writable, naming the folder and the user id to fix.
- **New environment variables**: `APP_PASSWORD`, `DATA_DIR`, `MEALIE_TIMEOUT`, and in the compose file `TZ`, `PUID` and `PGID`.
- `plan --dry-run` in the CLI: prints the plan that would be created, including what `--override-plan` would delete, and writes nothing.
- A set-up page that says which of `MEALIE_BASE_URL` and `MEALIE_TOKEN` is missing, and plain-language error messages when Mealie is unreachable or rejects the token.
- Recipe images are fetched through the app, so they show even when the browser cannot reach Mealie directly.
- Phone layout and dark mode.
- `dev/fake_mealie.py`, an in-memory imitation of the Mealie API with seed data, for development without a real Mealie.
- A test suite (`pytest`).
- How-to guides with screenshots under `docs/howto/`.

### Changed

- **Plan days are chosen by weekday.** A plan fills the configured weekdays within the seven days from each week's start date. Before, `--start` planned five consecutive days from whatever date it was given, so a non-Saturday start put dinners on the wrong days. A start date that is not the configured week-start day now produces a warning.
- **`--override-plan` (and "Replace existing dinners") deletes every dinner on a planned date.** Before, only one dinner per date was deleted, so a date with two dinners kept one.
- **Dinners already on the plan count as used.** A recipe that is kept on one day is no longer proposed for another day in the same plan.
- **Saving re-checks the plan.** If a day gained a dinner in Mealie after the preview was made, that day is left alone and reported instead of being double-booked.
- **Replacing a dinner adds the new one first.** With `--override-plan`, "Replace existing dinners" and Swap, the old dinner is removed only after the new one is in Mealie, so a failure part-way never leaves a day empty. Before, the old dinners were deleted first. A dinner Mealie refuses is reported and the rest of the plan is still saved.
- **Kept dinners go onto shopping lists at the household scale.** Before, a dinner that was already planned was added to the list at a scale of 1.0, while newly planned ones were scaled.
- **All list calls page through everything in Mealie.** Before, recipes were read 50–200 at a time, foods and units 200, tags 100, with no second page. Larger libraries had recipes that were never planned or listed, and foods, units or tags beyond the first page were created again as duplicates.
- **Pantry-staple removal uses an editable list** (Settings → Pantry staples) instead of a fixed list in the code. The default list is unchanged.
- **Image addresses must be public.** A pasted or found image address is only downloaded if it is `http(s)` on the public internet and at most 15 MB; addresses on the local network are refused.
- `suggest` without `--count` uses the "Recipes per batch" setting (default 14, as before). The Claude model, batch size and system prompt also come from Settings.
- When the library is too small to avoid repeating a recipe, the plan now carries a warning for that day. The existing warning for "no recipe fits this day's effort limit" is shown in the preview as well as printed by the CLI.
- `main.py` only parses arguments, prompts and prints. All logic moved to the `core/` package, which the web app shares.
- The README was rewritten for the web app.

### Fixed

- Recipe generation tells Claude about every recipe generated earlier in the same run. Before, the exclusion list was cut to its first 30 names, so with a library of 30 or more recipes later batches could repeat earlier ones. Up to 150 library names are now included as well.
- Requests to Mealie now time out (5 seconds to connect in the web app, 60 seconds to answer by default) instead of hanging forever when Mealie is down.
- Pantry-staple removal can no longer touch items on other shopping lists if Mealie ignores the list filter.
- Tag, food and unit lookups during a bulk import or parse are read once per job instead of once per ingredient.
- An effort rating can be cleared, not only changed.

### Removed

- `planner.py`, `suggester.py` and `scraper.py`. Their logic is now in `core/planning.py`, `core/generate.py`, `core/images.py` and `core/recipes.py`.
- The hard-coded household, weekday and effort rules in the code. They are settings now.
- The 100- and 200-recipe limits described in the old README.

## [1.0.0] - 2026-08-09

The command-line era, 28 May to 9 August 2026. Summarised from the git history; there was no formal release.

### Added

- `suggest`: Claude generates dinner recipes as schema.org JSON in batches of five; each is imported into Mealie, tagged `dinner`, given an `effort-1` to `effort-5` tag and a cover image from a DuckDuckGo search. Recipes whose name already exists are skipped. `--count`, `--restrict` and `--dry-run` options.
- `plan`: fortnightly dinner plans for Saturday to Wednesday, with effort limited to 3 on Mondays and Wednesdays, no repeats within 40 days, and servings scaled for 3 adults and 1 child. Dates that already have a dinner are skipped. `--start`, `--shopping-list` and `--override-plan` options.
- Shopping lists named `Week of <date>`, rebuilt on each run, with pantry staples (salt, pepper and similar) removed.
- `replace`: swap a planned dinner interactively, with `--search` and `--effort` filters, hiding recipes already planned in the next six weeks.
- `week`: show the current week, or the next N days.
- `recipes`, `tag-dinners`, `tag-effort`: list recipes and tag them interactively.
- `parse-ingredients`: parse ingredient text into quantity, unit and food with Mealie's parser, creating missing foods and units.
- `replace-image`: replace cover images from a web search, or from a given address with `--url`.
- `scrape`: import recipes from URLs, singly or from a file, with `--dry-run`.
