#!/usr/bin/env python3
"""
Mealie helper CLI

Commands:
  suggest             Ask Claude AI to suggest meals and import them into Mealie
  plan                Generate fortnightly meal plans (every 2nd week, starting this Saturday)
  recipes             List all recipes in your Mealie library
  week                Show this week's current meal plan (or the next N days)
  tag-dinners         Interactively mark recipes as dinner
  tag-effort          Rate recipes by effort level (1-5)
  replace             Replace a day's meal in the plan
  replace-image       Replace recipe cover images (search DuckDuckGo for new ones)
  scrape              Import a recipe from a URL manually
  parse-ingredients   Parse all recipe ingredients to populate quantity/unit fields

All logic lives in core/; this file only parses arguments, prompts and prints.
"""
import argparse
import sys
from datetime import date, timedelta
from env_loader import load_dotenv

load_dotenv()


def _client():
    from mealie_client import MealieClient
    return MealieClient()


def _settings():
    from core.settings import load_settings
    return load_settings()


def _indented(prefix: str):
    return lambda message: print(f"{prefix}{message}")


def _fail(message: str) -> None:
    print(message)
    sys.exit(1)


def cmd_suggest(args):
    from core import generate
    from core.errors import CoreError

    settings = _settings()
    client = _client()
    count = args.count if args.count is not None else settings.suggest_count
    existing = [r["name"] for r in client.list_recipes()]
    try:
        recipes = generate.generate_recipes(
            settings, existing, count=count, restrictions=args.restrict or "", progress=_indented("  "),
        )
    except CoreError as e:
        _fail(f"Error: {e}")

    print(f"\nImporting {len(recipes)} recipes into Mealie...\n")
    if args.dry_run:
        for recipe in recipes:
            print(f"  [dry-run] {recipe.get('name', 'Unknown')} ({recipe.get('recipeCuisine', '')})")
        print(f"\nDone: 0 imported, 0 failed, {len(recipes)} skipped.")
        return

    results = generate.import_recipes(client, settings, recipes, progress=_indented("  "))
    ok = sum(1 for r in results if r["status"] == "imported")
    fail = sum(1 for r in results if r["status"] == "failed")
    skip = sum(1 for r in results if r["status"] == "skipped")
    print(f"\nDone: {ok} imported, {fail} failed, {skip} skipped.")


def _print_plans(settings, weeks: list[dict], days_key: str) -> None:
    child_word = "child" if settings.children == 1 else "children"
    print(f"\nMeal Plans — {settings.adults} adults + {settings.children} {child_word}\n")
    for week in weeks:
        print(f"  Week of {date.fromisoformat(week['week_start']).strftime('%A %d %B %Y')}")
        print(f"  {'─' * 50}")
        for e in sorted(week[days_key], key=lambda x: x["date"]):
            sf_str = f"  (x{e['scale_factor']})" if e.get("scale_factor") else ""
            effort_str = f"  [effort-{e['effort']}]" if e.get("effort") else ""
            print(f"    {e['day']:<12} {e['recipe_name']}{effort_str}{sf_str}")
        if "shopping_list_name" in week:
            print(f"\n    Shopping list: '{week['shopping_list_name']}'")
        print()


def cmd_plan(args):
    from core import planning
    from core.errors import CoreError

    settings = _settings()
    client = _client()
    try:
        start = date.fromisoformat(args.start) if args.start else planning.next_week_start(settings)
    except ValueError:
        _fail(f"Invalid date '{args.start}'. Use YYYY-MM-DD format.")

    try:
        preview = planning.preview_plan(client, settings, start, override=args.override_plan)
    except CoreError as e:
        _fail(f"Error: {e}")

    for week in preview["weeks"]:
        week_dates = {d["date"] for d in week["days"]}
        print(f"  Planning week of {date.fromisoformat(week['week_start']).strftime('%d %b %Y')}...")
        if args.dry_run:  # a real run reports each deletion as it happens
            for entry in preview["deletions"]:
                if entry["date"] in week_dates:
                    print(f"    Would delete existing {entry['recipe_name']} from {entry['date']}")
        for day in week["days"]:
            if day["type"] == "existing":
                print(f"    Skipped {day['day']} — already planned: {day['recipe_name']}")
        for warning in preview["warnings"]:
            if warning["date"] in week_dates:
                print(f"    Warning: {warning['message']}")
    for warning in preview["warnings"]:
        if warning["date"] is None:
            print(f"  Warning: {warning['message']}")

    if args.dry_run:
        _print_plans(settings, preview["weeks"], "days")
        print("Dry run — nothing was written to Mealie.")
        return

    try:
        result = planning.commit_plan(
            client, settings, preview, create_shopping_list=args.shopping_list, progress=_indented("    "),
        )
    except CoreError as e:
        _fail(f"Error: {e}")
    for warning in result["warnings"]:
        print(f"    Warning: {warning['message']}")
    _print_plans(settings, result["weeks"], "entries")
    if result["failed"]:
        print(f"  {result['created']} dinner(s) added, {len(result['failed'])} could not be added (see the warnings above).")


def cmd_recipes(args):
    from core import recipes as recipe_ops

    recipes = recipe_ops.list_recipes(_client())
    if not recipes:
        print("No recipes found. Run 'suggest' to import some.")
        return
    print(f"\n{len(recipes)} recipes in your Mealie library:\n")
    for r in recipes:
        print(f"  {r['name']:<50} ({r['servings'] or '?'} servings)  [{r['slug']}]")
    print()


def cmd_week(args):
    from core import planning

    client = _client()
    today = date.today()
    days = args.days
    if days is not None:
        if days < 1:
            _fail("days must be 1 or more.")
        entries = planning.get_plan_range(client, today, today + timedelta(days=days - 1))
        heading = f"Meal plan for the next {days} days"
        empty_msg = f"No meal plan entries in the next {days} days."
    else:
        monday = today - timedelta(days=today.weekday())
        entries = planning.get_plan_range(client, monday, monday + timedelta(days=6))
        heading = "This week's meal plan"
        empty_msg = "No meal plan entries for this week."

    if not entries:
        print(empty_msg)
        return

    print(f"\n{heading} ({len(entries)} entries):\n")
    last_week = None
    for e in entries:
        entry_date = date.fromisoformat(e["date"])
        week = entry_date.isocalendar()[:2]
        if last_week is not None and week != last_week:
            print()
        last_week = week
        print(f"  {e['date']}  {entry_date.strftime('%a')}  [{e['entry_type'] or '?':9}]  {e['recipe_name']}")
    print()


def cmd_tag_dinners(args):
    from core import recipes as recipe_ops

    client = _client()
    recipes = recipe_ops.list_recipes(client)
    print(f"\n{len(recipes)} total recipes. Tag which ones are dinners.\n")
    print("Press y to tag as dinner, n to skip, q to quit.\n")
    tagged = 0
    for r in recipes:
        if r["is_dinner"]:
            print(f"  [already tagged]  {r['name']}")
            continue
        answer = input(f"  Tag as dinner? {r['name']}  [y/n/q]: ").strip().lower()
        if answer == "q":
            break
        if answer == "y":
            recipe_ops.set_dinner(client, r["slug"], True)
            print("    -> Tagged.")
            tagged += 1
    print(f"\nDone. {tagged} recipes tagged as dinner.")


def cmd_tag_effort(args):
    from core import recipes as recipe_ops

    client = _client()
    recipes = recipe_ops.list_recipes(client)
    print(f"\n{len(recipes)} recipes. Rate effort 1-5, s to skip, q to quit.")
    print("  1=very quick  2=easy  3=moderate  4=involved  5=time-intensive\n")
    rated = 0
    for r in recipes:
        current_str = f" [currently effort-{r['effort']}]" if r["effort"] else " [unrated]"
        answer = input(f"  {r['name']}{current_str}  [1-5/s/q]: ").strip().lower()
        if answer == "q":
            break
        if answer in ("s", ""):
            continue
        if answer in ("1", "2", "3", "4", "5"):
            recipe_ops.set_effort(client, r["slug"], int(answer))
            print(f"    -> effort-{answer}")
            rated += 1
        else:
            print("    Unrecognised input, skipping.")
    print(f"\nDone. {rated} recipes rated.")


def cmd_replace(args):
    from core import planning

    try:
        target = date.fromisoformat(args.date)
    except ValueError:
        _fail(f"Invalid date '{args.date}'. Use YYYY-MM-DD format.")
    effort = args.effort
    if effort is not None and not (1 <= effort <= 5):
        _fail("--effort must be between 1 and 5.")

    client = _client()
    options = planning.replacement_options(client, _settings(), target, search=args.search, effort=effort)

    if options["current"] is None:
        print(f"No dinner entry found for {target.strftime('%A %d %B %Y')}.")
        return
    print(f"\nCurrent meal for {target.strftime('%A %d %B %Y')}: {options['current']['recipe_name']}")

    hidden = options["hidden"]
    empty_messages = {
        "no_dinner_recipes": "No dinner recipes available. Run 'suggest' or 'tag-dinners' first.",
        "no_search_match": f"No dinner recipes matching '{args.search}'.",
        "no_effort_match": f"No dinner recipes rated effort {effort} or below. Run 'tag-effort' to rate recipes.",
        "all_planned": f"\nAll {hidden} matching recipe(s) are already in the upcoming plan. "
                       f"Widen the filters or run 'suggest' to add more.",
    }
    if options["empty_reason"]:
        print(empty_messages[options["empty_reason"]])
        return

    recipes = options["recipes"]
    effort_label = f" (effort ≤ {effort})" if effort is not None else ""
    hidden_label = f", {hidden} already planned and hidden" if hidden else ""
    print(f"\nAvailable recipes ({len(recipes)}){effort_label}{hidden_label}:\n")
    for i, r in enumerate(recipes, 1):
        effort_str = f"  [effort-{r['effort']}]" if r["effort"] else ""
        print(f"  {i:>3}. {r['name']}{effort_str}")

    print()
    while True:
        choice = input("Pick a number (or q to quit): ").strip()
        if choice.lower() == "q":
            print("Cancelled.")
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(recipes):
                chosen = recipes[idx]
                break
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(recipes)}.")

    planning.replace_day(client, target, chosen["id"])
    print(f"\n  {target.strftime('%A %d %B')} updated: {chosen['name']}")


def cmd_scrape(args):
    from core import recipes as recipe_ops

    if args.file:
        with open(args.file) as f:
            urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    elif args.url:
        urls = [args.url]
    else:
        _fail("Provide a URL or --file.")

    client = _client()
    for url in urls:
        print(f"  [dry-run] Testing scrape: {url}" if args.dry_run else f"  Importing: {url}")
        result = recipe_ops.scrape_urls(client, [url], dry_run=args.dry_run)[0]
        if result["status"] == "error":
            print(f"  ERROR importing {url}: {result['error']}")
            if not args.file:
                sys.exit(1)
        elif not args.dry_run:
            recipe = result["recipe"]
            print(f"  -> Imported '{recipe['name']}' ({recipe['slug']}) [source: {url}]")


def cmd_parse_ingredients(args):
    from core import recipes as recipe_ops

    def show(message: str) -> None:
        # The first message is the heading; the rest are one line per recipe.
        print(f"\n{message}\n" if message.startswith("Parsing") else f"  {message}")

    summary = recipe_ops.parse_all_ingredients(_client(), progress=show)
    if not summary["results"]:
        print("No recipes found.")
        return

    print(f"\nDone: {summary['parsed']} parsed, {summary['unchanged']} already up to date, {summary['failed']} failed.")
    failed = [r for r in summary["results"] if r["status"] == "failed"]
    if failed:
        print("\nFailed recipes (check Mealie UI and parse manually if needed):")
        for r in failed:
            print(f"  - {r['name']}: {r['error']}")


def cmd_replace_image(args):
    from core import images, recipes as recipe_ops
    from core.errors import ImageDownloadError

    client = _client()
    recipes = recipe_ops.list_recipes(client)
    if not recipes:
        print("No recipes found.")
        return

    if args.search:
        term = args.search.lower()
        recipes = [r for r in recipes if term in r["name"].lower() or term in r["slug"].lower()]
        if not recipes:
            print(f"No recipes found matching '{args.search}'.")
            return

    image_url = args.url

    def replace(recipe: dict) -> bool:
        if image_url:
            try:
                return images.set_image_from_url(client, recipe["slug"], image_url)
            except ImageDownloadError as e:
                print(f"    {e}")
                return False
        return images.auto_set_image(client, recipe["slug"], recipe["name"])

    # When a specific URL is given and search narrows to one recipe, skip prompt
    if image_url and len(recipes) == 1:
        r = recipes[0]
        print(f"Uploading image for '{r['name']}' from URL...")
        print("  ✓ Image replaced." if replace(r) else "  ✗ Download failed or URL did not return a valid image.")
        return

    print(f"\nFound {len(recipes)} recipes. Select which to replace images for.\n")
    print("Press y to replace image, n to skip, q to quit.\n")

    replaced = 0
    for r in recipes:
        answer = input(f"  Replace image for '{r['name']}'? [y/n/q]: ").strip().lower()
        if answer == "q":
            break
        if answer == "y":
            print(f"    {'Downloading from URL' if image_url else 'Searching for image'}...")
            if replace(r):
                print("    ✓ Image replaced.")
                replaced += 1
            else:
                print("    ✗ Could not find suitable image (no matching results or download failed).")

    print(f"\nDone. {replaced} images replaced.")


def main():
    parser = argparse.ArgumentParser(description="Mealie AI meal planner")
    sub = parser.add_subparsers(dest="command", required=True)

    # suggest
    s = sub.add_parser("suggest", help="Ask Claude AI to suggest and import meals")
    s.add_argument("--count", type=int, help="Number of meals to suggest (default: 14)")
    s.add_argument("--dry-run", action="store_true", help="Preview suggestions without importing")
    s.add_argument("--restrict", help="Additional constraint for Claude (e.g. 'no chicken')")
    s.set_defaults(func=cmd_suggest)

    # plan
    p = sub.add_parser("plan", help="Generate fortnightly meal plans starting this Saturday")
    p.add_argument("--start", help="Override start Saturday (YYYY-MM-DD)")
    p.add_argument("--shopping-list", action="store_true", help="Also create shopping lists in Mealie")
    p.add_argument("--override-plan", action="store_true", help="Replace existing meal plans for these dates")
    p.add_argument("--dry-run", action="store_true", help="Show the plan that would be created without writing it")
    p.set_defaults(func=cmd_plan)

    # recipes
    r = sub.add_parser("recipes", help="List all recipes in Mealie")
    r.set_defaults(func=cmd_recipes)

    # week
    w = sub.add_parser("week", help="Show this week's meal plan, or the next N days")
    w.add_argument("days", nargs="?", type=int, metavar="DAYS",
                   help="Show the next N days starting today (e.g. 21 for 3 weeks)")
    w.set_defaults(func=cmd_week)

    # tag-dinners
    td = sub.add_parser("tag-dinners", help="Interactively tag existing recipes as dinner")
    td.set_defaults(func=cmd_tag_dinners)

    # replace
    rp = sub.add_parser("replace", help="Replace the dinner for a specific date")
    rp.add_argument("date", help="Date to replace (YYYY-MM-DD)")
    rp.add_argument("--search", help="Filter recipe list by name")
    rp.add_argument("--effort", type=int, metavar="N", help="Only show recipes with effort ≤ N (1-5)")
    rp.set_defaults(func=cmd_replace)

    # tag-effort
    te = sub.add_parser("tag-effort", help="Interactively rate recipes with an effort level 1-5")
    te.set_defaults(func=cmd_tag_effort)

    # scrape
    sc = sub.add_parser("scrape", help="Manually import a recipe from a URL")
    sc.add_argument("url", nargs="?", help="Recipe URL to import")
    sc.add_argument("--file", help="File with one URL per line")
    sc.add_argument("--dry-run", action="store_true", help="Preview without importing")
    sc.set_defaults(func=cmd_scrape)

    # parse-ingredients
    pi = sub.add_parser("parse-ingredients", help="Parse ingredients in all recipes to populate quantity/unit")
    pi.set_defaults(func=cmd_parse_ingredients)

    # replace-image
    ri = sub.add_parser("replace-image", help="Interactively replace recipe cover images")
    ri.add_argument("--search", help="Filter recipe list by name or slug")
    ri.add_argument("--url", help="Image URL to download and use (skips DuckDuckGo search)")
    ri.set_defaults(func=cmd_replace_image)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
