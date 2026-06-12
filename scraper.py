"""
Recipe scraper — imports recipes from URLs into Mealie.
Supports single URLs, batch lists, and a few curated seed sources.
"""
from mealie_client import MealieClient


SEED_SOURCES = [
    # Add your favourite recipe site URLs here
]


def scrape_from_url(client: MealieClient, url: str, dry_run: bool = False) -> dict:
    if dry_run:
        print(f"  [dry-run] Testing scrape: {url}")
        return client.test_scrape(url)

    print(f"  Importing: {url}")
    recipe = client.scrape_recipe(url)
    print(f"  -> Imported '{recipe['name']}' ({recipe['slug']}) [source: {url}]")
    return recipe


def scrape_batch(client: MealieClient, urls: list[str], dry_run: bool = False) -> list[dict]:
    results = []
    for url in urls:
        try:
            recipe = scrape_from_url(client, url, dry_run=dry_run)
            results.append({"url": url, "status": "ok", "recipe": recipe})
        except Exception as e:
            print(f"  ERROR importing {url}: {e}")
            results.append({"url": url, "status": "error", "error": str(e)})
    return results
