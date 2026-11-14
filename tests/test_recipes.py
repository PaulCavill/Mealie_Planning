import sys
import types
from types import SimpleNamespace

import pytest

from core import generate, images, recipes
from core.errors import GenerationError, ImageSearchError
from core.settings import Settings


def test_list_recipes_extracts_dinner_flag_and_effort(library):
    listed = recipes.list_recipes(library)
    assert len(listed) == 13 and [r["name"] for r in listed] == sorted(r["name"] for r in listed)
    by_name = {r["name"]: r for r in listed}
    assert by_name["Porridge"]["is_dinner"] is False and by_name["Porridge"]["effort"] is None
    assert (by_name["Recipe 03"]["is_dinner"], by_name["Recipe 03"]["effort"], by_name["Recipe 03"]["servings"]) == (True, 4, 4)
    assert by_name["Recipe 10"]["effort"] is None
    assert len(recipes.list_recipes(library, dinner_only=True)) == 12


def test_set_and_clear_dinner_and_effort(library):
    assert recipes.set_dinner(library, "porridge", True)["is_dinner"] is True
    assert recipes.set_dinner(library, "porridge", True)["tags"] == ["dinner"]
    assert recipes.set_dinner(library, "porridge", False)["is_dinner"] is False

    assert recipes.set_effort(library, "recipe-00", 4)["effort"] == 4
    assert recipes.set_effort(library, "recipe-00", 2)["tags"] == ["dinner", "effort-2"]
    assert recipes.set_effort(library, "recipe-00", None)["tags"] == ["dinner"]
    with pytest.raises(ValueError):
        recipes.set_effort(library, "recipe-00", 6)
    assert len([t for t in library.tags if t["name"] == "dinner"]) == 1


def test_parse_all_ingredients_reports_each_recipe(mealie):
    mealie.add_recipe("Apple pie", ingredients=["3 g apples", "100 g flour"])
    mealie.add_recipe("Bread", ingredients=["500 g flour"])
    mealie.add_recipe("Curry", ingredients=["1 tin tomatoes"])
    mealie.fail_put_slugs.add("bread")
    messages = []
    summary = recipes.parse_all_ingredients(mealie, progress=messages.append)
    assert (summary["parsed"], summary["failed"]) == (2, 1)
    assert [(r["name"], r["status"]) for r in summary["results"]] == [
        ("Apple pie", "parsed"), ("Bread", "failed"), ("Curry", "parsed")]
    assert "Bread" in summary["results"][1]["error"]
    assert messages == ["Parsing ingredients for 3 recipes...", "  1. ✓ Apple pie", "  2. ✗ Bread", "  3. ✓ Curry"]
    assert mealie.count("GET", "/api/foods") == 1 and mealie.count("GET", "/api/units") == 1
    assert sorted(f["name"] for f in mealie.foods) == ["apples", "flour", "tomatoes"]


def test_scrape_dry_run_and_import(mealie, monkeypatch):
    monkeypatch.setattr(mealie, "test_scrape", lambda url: {"name": "Scraped"})
    monkeypatch.setattr(mealie, "scrape_recipe", lambda url: {"name": "Scraped", "slug": "scraped"})
    assert recipes.scrape_url(mealie, "http://x", dry_run=True) == {
        "url": "http://x", "dry_run": True, "recipe": {"name": "Scraped"}}
    assert recipes.scrape_url(mealie, "http://x")["recipe"]["slug"] == "scraped"

    def boom(url):
        raise RuntimeError("no recipe found")
    monkeypatch.setattr(mealie, "scrape_recipe", boom)
    results = recipes.scrape_urls(mealie, ["http://bad"])
    assert results == [{"url": "http://bad", "status": "error", "recipe": None, "error": "no recipe found"}]


class FakeClaude:
    """Stands in for anthropic.Anthropic: returns numbered recipes, one repeated per batch."""

    def __init__(self):
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        n = int(kwargs["messages"][0]["content"].split()[2])
        base = (len(self.requests) - 1) * 10
        items = [{"name": f"Dish {base + i}", "effort": 2, "recipeCuisine": "Test"} for i in range(n)]
        items.append({"name": "DISH 0"})  # a repeat after the first batch
        import json
        return SimpleNamespace(content=[SimpleNamespace(text="```json\n" + json.dumps(items) + "\n```")])


def test_generate_recipes_batches_and_dedupes():
    ai, messages = FakeClaude(), []
    out = generate.generate_recipes(Settings(), ["Butter Chicken"], count=7, restrictions="no fish",
                                    progress=messages.append, ai_client=ai)
    assert [r["name"] for r in out] == [f"Dish {i}" for i in range(5)] + ["DISH 0"][:0] + ["Dish 10", "Dish 11"]
    assert [r["model"] for r in ai.requests] == ["claude-haiku-4-5-20251001"] * 2
    assert "Generate exactly 5 dinner recipes" in ai.requests[0]["messages"][0]["content"]
    assert ai.requests[1]["messages"][0]["content"].endswith("Additional restriction: no fish.")
    assert "library: Butter Chicken, Dish 0, Dish 1" in ai.requests[1]["system"]
    assert messages == ["Asking Claude to generate 7 recipes (in batches of 5)...",
                        "...5/7 generated", "...7/7 generated"]


def test_generate_recipes_errors(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(GenerationError, match="ANTHROPIC_API_KEY"):
        generate.generate_recipes(Settings(), [], count=1)
    bad = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **k: SimpleNamespace(content=[SimpleNamespace(text="Sorry, I can't")])))
    with pytest.raises(GenerationError, match="valid JSON"):
        generate.generate_recipes(Settings(), [], count=1, ai_client=bad)


def test_import_recipes_imports_skips_and_reports_failures(mealie, monkeypatch):
    searched = []
    monkeypatch.setattr(images, "auto_set_image", lambda client, slug, name, ings: searched.append(slug) or True)
    mealie.add_recipe("Butter Chicken")
    mealie.fail_put_slugs.add("broken-stew")  # tagging this one fails
    generated = [
        {"name": "butter chicken", "effort": 3},
        {"name": "Fish Tacos", "effort": 2, "recipeCuisine": "Mexican", "url": "https://example.com/t",
         "recipeIngredient": ["400 g fish", "8 small tortillas"]},
        {"name": "Fish Tacos", "effort": 2},
        {"name": "Broken Stew", "effort": 9},
    ]
    messages = []
    results = generate.import_recipes(mealie, Settings(), generated, progress=messages.append)
    assert [r["status"] for r in results] == ["skipped", "imported", "skipped", "failed"]
    assert results[1]["slug"] == "fish-tacos" and results[3]["slug"] == "broken-stew" and results[3]["error"]
    assert messages[1] == "OK: 'Fish Tacos' (Mexican) effort-2 [source: https://example.com/t]"
    tacos = mealie.recipes["fish-tacos"]
    assert [t["name"] for t in tacos["tags"]] == ["dinner", "effort-2"]
    assert [i["food"]["name"] for i in tacos["recipeIngredient"]] == ["fish", "tortillas"]
    assert searched == ["fish-tacos"]


def test_image_query_and_candidates(monkeypatch):
    assert images.image_query("Butter Chicken") == "Butter Chicken food recipe"
    assert images.image_query("Butter Chicken", ["600g chicken thighs, cut", {"food": {"name": "tomato passata"}}, "x"]) \
        == "Butter Chicken thighs passata food recipe"
    assert images.image_query("Soup", ["", {"food": None}]) == "Soup food recipe"

    seen = {}

    class FakeDDGS:
        def images(self, query, max_results):
            seen.update(query=query, max_results=max_results)
            return [
                {"image": "https://a/1.jpg", "thumbnail": "https://t/1.jpg", "title": "One", "url": "https://a",
                 "width": 800, "height": 600},
                {"image": "data:image/png;base64,xx"},
                {"image": "https://b/2.png"},
            ]

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    found = images.search_image_candidates("Soup", ["2 leeks"], max_results=3)
    assert seen == {"query": "Soup leeks food recipe", "max_results": 3}
    assert [(c["url"], c["thumbnail"]) for c in found] == [("https://a/1.jpg", "https://t/1.jpg"), ("https://b/2.png", None)]

    class BrokenDDGS:
        def images(self, *a, **k):
            raise RuntimeError("rate limited")

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=BrokenDDGS))
    with pytest.raises(ImageSearchError):
        images.search_image_candidates("Soup")


def test_set_image_and_auto_pick(mealie, monkeypatch):
    mealie.add_recipe("Soup")
    pages = {
        "https://ok/big.png": ("image/png", b"x" * 30_000),
        "https://ok/small.jpg": ("image/jpeg", b"x" * 100),
        "https://ok/page": ("text/html", b"<html>"),
    }

    def fake_get(url, **kwargs):
        if url not in pages:
            raise RuntimeError("connection refused")
        ctype, body = pages[url]
        return SimpleNamespace(headers={"content-type": ctype}, content=body, raise_for_status=lambda: None)

    monkeypatch.setattr(images.requests, "get", fake_get)
    assert images.set_image_from_url(mealie, "soup", "https://ok/small.jpg") is True  # no size check for explicit URLs
    assert mealie.images["soup"][0] == "cover.jpg"
    assert images.set_image_from_url(mealie, "soup", "https://ok/page") is False
    assert images.set_image_from_url(mealie, "soup", "https://down/x.jpg") is False
    assert images.set_image_from_url(mealie, "soup", "file:///etc/passwd") is False

    monkeypatch.setattr(images, "search_image_candidates", lambda name, ings=None: [
        {"url": u} for u in ("https://down/x.jpg", "https://ok/page", "https://ok/small.jpg", "https://ok/big.png")])
    assert images.auto_set_image(mealie, "soup", "Soup") is True
    assert mealie.images["soup"][0] == "cover.png" and len(mealie.images["soup"][1]) == 30_000
    monkeypatch.setattr(images, "search_image_candidates", lambda name, ings=None: [{"url": "https://ok/small.jpg"}])
    assert images.auto_set_image(mealie, "soup", "Soup") is False


def test_summarize_exposes_the_image_version_token():
    assert recipes.summarize({"id": "1", "name": "Soup", "image": "ab12"})["image"] == "ab12"
    assert recipes.summarize({"id": "1", "name": "Soup", "image": None})["image"] is None
    assert recipes.summarize({"id": "1", "name": "Soup"})["image"] is None
