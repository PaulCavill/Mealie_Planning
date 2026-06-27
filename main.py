#!/usr/bin/env python3
"""
Mealie helper CLI

Commands:
  suggest             Ask Claude AI to suggest meals and import them into Mealie
  plan                Generate fortnightly meal plans (every 2nd week, starting this Saturday)
  recipes             List all recipes in your Mealie library
  week                Show this week's current meal plan
  tag-dinners         Interactively mark recipes as dinner
  tag-effort          Rate recipes by effort level (1-5)
  replace             Replace a day's meal in the plan
  replace-image       Replace recipe cover images (search DuckDuckGo for new ones)
  scrape              Import a recipe from a URL manually
  parse-ingredients   Parse all recipe ingredients to populate quantity/unit fields
"""
import argparse
import sys
from datetime import date
from env_loader import load_dotenv

load_dotenv()


def cmd_suggest(args):
    from suggester import run_suggest
    run_suggest(count=args.count, dry_run=args.dry_run, restrictions=args.restrict or "")


def cmd_plan(args):
    from mealie_client import MealieClient
    from planner import plan_fortnightly, print_plans, this_saturday

    client = MealieClient()
    start = date.fromisoformat(args.start) if args.start else this_saturday()
    plans = plan_fortnightly(client, start=start, create_shopping_list=args.shopping_list, override_plan=args.override_plan)
    print_plans(plans)


def cmd_recipes(args):
    from mealie_client import MealieClient
    client = MealieClient()
    recipes = client.list_recipes(per_page=100)
    if not recipes:
        print("No recipes found. Run 'suggest' to import some.")
        return
    print(f"\n{len(recipes)} recipes in your Mealie library:\n")
    for r in sorted(recipes, key=lambda x: x["name"]):
        servings = r.get("recipeServings") or "?"
        print(f"  {r['name']:<50} ({servings} servings)  [{r['slug']}]")
    print()


def cmd_week(args):
    from mealie_client import MealieClient
    client = MealieClient()
    entries = client.get_this_week()
    if not entries:
        print("No meal plan entries for this week.")
        return
    print(f"\nThis week's meal plan ({len(entries)} entries):\n")
    for e in sorted(entries, key=lambda x: x.get("date", "")):
        recipe_name = e.get("recipe", {}).get("name") if e.get("recipe") else e.get("title", "(no recipe)")
        print(f"  {e['date']}  [{e.get('entryType', '?'):9}]  {recipe_name}")
    print()


def cmd_tag_dinners(args):
    from mealie_client import MealieClient
    client = MealieClient()
    recipes = client.list_recipes(per_page=200)
    print(f"\n{len(recipes)} total recipes. Tag which ones are dinners.\n")
    print("Press y to tag as dinner, n to skip, q to quit.\n")
    tagged = 0
    for r in sorted(recipes, key=lambda x: x["name"]):
        existing_tags = [t["name"] for t in (r.get("tags") or [])]
        if "dinner" in existing_tags:
            print(f"  [already tagged]  {r['name']}")
            continue
        answer = input(f"  Tag as dinner? {r['name']}  [y/n/q]: ").strip().lower()
        if answer == "q":
            break
        if answer == "y":
            client.add_tag(r["slug"], "dinner")
            print(f"    -> Tagged.")
            tagged += 1
    print(f"\nDone. {tagged} recipes tagged as dinner.")


def cmd_replace(args):
    from mealie_client import MealieClient
    from planner import replace_day
    from datetime import date

    client = MealieClient()
    try:
        target = date.fromisoformat(args.date)
    except ValueError:
        print(f"Invalid date '{args.date}'. Use YYYY-MM-DD format.")
        sys.exit(1)
    effort = args.effort
    if effort is not None and not (1 <= effort <= 5):
        print("--effort must be between 1 and 5.")
        sys.exit(1)
    replace_day(client, target, search=args.search, effort=effort)


def cmd_tag_effort(args):
    from mealie_client import MealieClient
    from planner import tag_effort
    client = MealieClient()
    tag_effort(client)


def cmd_scrape(args):
    from mealie_client import MealieClient
    from scraper import scrape_from_url, scrape_batch
    client = MealieClient()
    if args.file:
        with open(args.file) as f:
            urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        scrape_batch(client, urls, dry_run=args.dry_run)
    elif args.url:
        scrape_from_url(client, args.url, dry_run=args.dry_run)
    else:
        print("Provide a URL or --file.")
        sys.exit(1)


def cmd_parse_ingredients(args):
    from mealie_client import MealieClient

    client = MealieClient()
    recipes = client.list_recipes(per_page=200)

    if not recipes:
        print("No recipes found.")
        return

    print(f"\nParsing ingredients for {len(recipes)} recipes...\n")
    success = 0
    failed = 0
    failed_recipes = []

    for i, recipe in enumerate(sorted(recipes, key=lambda x: x["name"]), 1):
        try:
            client.update_recipe_ingredients(recipe["slug"])
            print(f"  {i:>3}. ✓ {recipe['name']}")
            success += 1
        except Exception as e:
            print(f"  {i:>3}. ✗ {recipe['name']}")
            failed += 1
            failed_recipes.append(recipe['name'])

    print(f"\nDone: {success} parsed, {failed} failed.")
    if failed_recipes:
        print(f"\nFailed recipes (check Mealie UI and parse manually if needed):")
        for name in failed_recipes:
            print(f"  - {name}")


def cmd_replace_image(args):
    from mealie_client import MealieClient

    client = MealieClient()
    recipes = client.list_recipes(per_page=200)

    if not recipes:
        print("No recipes found.")
        return

    recipes_sorted = sorted(recipes, key=lambda x: x["name"])

    if args.search:
        recipes_sorted = [r for r in recipes_sorted if args.search.lower() in r["name"].lower() or args.search.lower() in r["slug"].lower()]
        if not recipes_sorted:
            print(f"No recipes found matching '{args.search}'.")
            return

    image_url = getattr(args, "url", None)

    # When a specific URL is given and search narrows to one recipe, skip prompt
    if image_url and len(recipes_sorted) == 1:
        r = recipes_sorted[0]
        print(f"Uploading image for '{r['name']}' from URL...")
        try:
            success = client.add_cover_image(r["slug"], r["name"], ingredients=r.get("recipeIngredient"), image_url=image_url)
            print(f"  ✓ Image replaced." if success else f"  ✗ Download failed or URL did not return a valid image.")
        except Exception as e:
            print(f"  ✗ Error: {e}")
        return

    print(f"\nFound {len(recipes_sorted)} recipes. Select which to replace images for.\n")
    print("Press y to replace image, n to skip, q to quit.\n")

    replaced = 0
    for r in recipes_sorted:
        answer = input(f"  Replace image for '{r['name']}'? [y/n/q]: ").strip().lower()
        if answer == "q":
            break
        if answer == "y":
            print(f"    {'Downloading from URL' if image_url else 'Searching for image'}...")
            try:
                success = client.add_cover_image(r["slug"], r["name"], ingredients=r.get("recipeIngredient"), image_url=image_url)
                if success:
                    print(f"    ✓ Image replaced.")
                    replaced += 1
                else:
                    print(f"    ✗ Could not find suitable image (no matching results or download failed).")
            except Exception as e:
                print(f"    ✗ Error: {e}")

    print(f"\nDone. {replaced} images replaced.")


def main():
    parser = argparse.ArgumentParser(description="Mealie AI meal planner")
    sub = parser.add_subparsers(dest="command", required=True)

    # suggest
    s = sub.add_parser("suggest", help="Ask Claude AI to suggest and import meals")
    s.add_argument("--count", type=int, default=14, help="Number of meals to suggest (default: 14)")
    s.add_argument("--dry-run", action="store_true", help="Preview suggestions without importing")
    s.add_argument("--restrict", help="Additional constraint for Claude (e.g. 'no chicken')")
    s.set_defaults(func=cmd_suggest)

    # plan
    p = sub.add_parser("plan", help="Generate fortnightly meal plans starting this Saturday")
    p.add_argument("--start", help="Override start Saturday (YYYY-MM-DD)")
    p.add_argument("--shopping-list", action="store_true", help="Also create shopping lists in Mealie")
    p.add_argument("--override-plan", action="store_true", help="Replace existing meal plans for these dates")
    p.set_defaults(func=cmd_plan)

    # recipes
    r = sub.add_parser("recipes", help="List all recipes in Mealie")
    r.set_defaults(func=cmd_recipes)

    # week
    w = sub.add_parser("week", help="Show this week's meal plan")
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
