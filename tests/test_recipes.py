import ipaddress
import sys
import types
from types import SimpleNamespace

import pytest

from core import generate, images, recipes
from core.errors import GenerationError, ImageDownloadError, ImageSearchError
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


def test_parse_all_distinguishes_parsed_unchanged_and_failed(mealie):
    mealie.add_recipe("Apple pie", ingredients=["3 g apples"])
    mealie.add_recipe("Bread", ingredients=[mealie.structured(500, "g", "flour", "sifted")])
    mealie.add_recipe("Curry", ingredients=["1 tin tomatoes"])
    mealie.fail_put_slugs.add("curry")
    messages = []
    summary = recipes.parse_all_ingredients(mealie, progress=messages.append)
    assert (summary["parsed"], summary["unchanged"], summary["failed"]) == (1, 1, 1)
    assert [(r["status"], r["ingredients"]) for r in summary["results"]] == [("parsed", 1), ("unchanged", 0), ("failed", 0)]
    assert messages[1:] == ["  1. ✓ Apple pie", "  2. – Bread (nothing to parse)", "  3. ✗ Curry"]
    assert mealie.recipes["bread"]["recipeIngredient"][0]["note"] == "sifted"

    mealie.fail_parser = True  # the parser being down is a failure, not "parsed"
    mealie.add_recipe("Dal", ingredients=["200 g lentils"])
    result = recipes.parse_ingredients(mealie, "dal")
    assert result["status"] == "failed" and "parser" in result["error"]


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

    # Real Mealie answers a failed *test* scrape with HTTP 200 and a sentence.
    monkeypatch.setattr(mealie, "test_scrape", lambda url: "recipe_scrapers was unable to scrape this URL")
    for answer in ("recipe_scrapers was unable to scrape this URL", {"name": ""}, None):
        monkeypatch.setattr(mealie, "test_scrape", lambda url, answer=answer: answer)
        [result] = recipes.scrape_urls(mealie, ["http://x"], dry_run=True)
        assert result["status"] == "error" and result["recipe"] is None
        assert "could not find a recipe" in result["error"]


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
    assert "library: Dish 0, Dish 1, Dish 2, Dish 3, Dish 4, Butter Chicken\n" in ai.requests[1]["system"]
    assert messages == ["Asking Claude to generate 7 recipes (in batches of 5)...",
                        "...5/7 generated", "...7/7 generated"]


def test_later_batches_are_told_what_this_run_already_made_even_with_a_big_library():
    ai = FakeClaude()
    library = [f"Library {i}" for i in range(400)]
    generate.generate_recipes(Settings(), library, count=10, ai_client=ai)
    avoid = ai.requests[1]["system"].split("library: ")[1].split("\n")[0].split(", ")
    assert avoid[:5] == [f"Dish {i}" for i in range(5)]  # this run's names come first, never cut off
    assert avoid[5:] == library[:generate.MAX_LIBRARY_NAMES_IN_PROMPT] and len(avoid) == 155


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


class FakeResponse:
    """The parts of a streamed requests.Response the downloader uses."""

    def __init__(self, url, status=200, ctype="image/png", body=b"", location=None, declared=None):
        self.url, self.status_code, self.body, self.closed = url, status, body, False
        self.headers = {"content-type": ctype} if ctype else {}
        if location:
            self.headers["location"] = location
        if declared is not None:
            self.headers["content-length"] = str(declared)
        self.raw = None

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def close(self):
        self.closed = True


@pytest.fixture
def internet(monkeypatch):
    """A pretend internet: `pages` maps URL -> FakeResponse kwargs, `dns` maps host -> addresses."""
    net = SimpleNamespace(pages={}, dns={}, fetched=[], responses=[])

    def fake_get(url, **kwargs):
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True and kwargs["timeout"]
        net.fetched.append(url)
        if url not in net.pages:
            raise RuntimeError("connection refused")
        net.responses.append(FakeResponse(url, **net.pages[url]))
        return net.responses[-1]

    def fake_dns(host, port):
        try:
            ipaddress.ip_address(host)
            return [host]  # a literal address needs no lookup
        except ValueError:
            return net.dns.get(host, ["93.184.216.34"])

    monkeypatch.setattr(images.requests, "get", fake_get)
    monkeypatch.setattr(images, "_host_addresses", fake_dns)
    monkeypatch.delenv("ALLOW_PRIVATE_IMAGE_HOSTS", raising=False)
    return net


def test_set_image_and_auto_pick(mealie, internet, monkeypatch):
    mealie.add_recipe("Soup")
    internet.pages = {
        "https://ok/big.png": dict(ctype="image/png", body=b"x" * 30_000),
        "https://ok/small.jpg": dict(ctype="image/jpeg", body=b"x" * 100),
        "https://ok/page": dict(ctype="text/html", body=b"<html>"),
        "https://ok/gone.png": dict(status=404),
    }
    assert images.set_image_from_url(mealie, "soup", "https://ok/small.jpg") is True  # no size check for explicit URLs
    assert mealie.images["soup"][0] == "cover.jpg" and all(r.closed for r in internet.responses)
    for url, why in (("https://ok/page", "not an image"), ("https://down/x.jpg", "did not answer"),
                     ("https://ok/gone.png", "404"), ("file:///etc/passwd", "http://"), ("ftp://ok/a.png", "http://")):
        with pytest.raises(ImageDownloadError, match=why):
            images.set_image_from_url(mealie, "soup", url)

    candidates = [{"url": u} for u in ("https://down/x.jpg", "https://ok/page", "https://ok/small.jpg",
                                       "http://127.0.0.1/a.png", "https://ok/big.png")]
    monkeypatch.setattr(images, "search_image_candidates", lambda name, ings=None: candidates)
    assert images.auto_set_image(mealie, "soup", "Soup") is True
    assert mealie.images["soup"][0] == "cover.png" and len(mealie.images["soup"][1]) == 30_000
    candidates[:] = [{"url": "https://ok/small.jpg"}]
    assert images.auto_set_image(mealie, "soup", "Soup") is False


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9000/api/x.png",          # this machine
    "http://localhost/x.png",
    "http://192.168.1.10/x.png",                # the home network
    "http://10.0.0.5/x.png",
    "http://172.16.3.4/x.png",
    "http://169.254.169.254/latest/meta-data",  # link-local / cloud metadata
    "http://0.0.0.0/x.png",
    "http://224.0.0.1/x.png",                   # multicast
    "http://[::1]/x.png",
    "http://[fe80::1]/x.png",
    "http://[fd00::1]/x.png",
    "http://[::ffff:127.0.0.1]/x.png",          # IPv4 in IPv6 clothing
    "http://nas.internal.example/x.png",        # a public-looking name that resolves inward
])
def test_images_are_not_fetched_from_private_or_local_addresses(internet, url):
    internet.dns = {"localhost": ["127.0.0.1", "::1"], "nas.internal.example": ["93.184.216.34", "192.168.1.20"]}
    internet.pages = {url: dict(body=b"secret")}
    with pytest.raises(ImageDownloadError, match="private or local network"):
        images._download_image(url)
    assert internet.fetched == []  # refused before any request was made


def test_image_redirects_are_followed_by_hand_and_rechecked(internet):
    internet.pages = {
        "https://cdn.example/a": dict(status=302, location="/b"),
        "https://cdn.example/b": dict(status=301, location="https://img.example/final.png"),
        "https://img.example/final.png": dict(body=b"png"),
        "https://cdn.example/inward": dict(status=302, location="http://192.168.1.1/admin.png"),
        "http://192.168.1.1/admin.png": dict(body=b"router"),
        "https://cdn.example/scheme": dict(status=302, location="file:///etc/passwd"),
        "https://cdn.example/loop": dict(status=302, location="https://cdn.example/loop"),
    }
    assert images._download_image("https://cdn.example/a") == (b"png", "image/png")
    with pytest.raises(ImageDownloadError, match="private or local network"):
        images._download_image("https://cdn.example/inward")
    assert "http://192.168.1.1/admin.png" not in internet.fetched
    with pytest.raises(ImageDownloadError, match="http://"):
        images._download_image("https://cdn.example/scheme")
    internet.fetched.clear()
    with pytest.raises(ImageDownloadError, match="too many redirects"):
        images._download_image("https://cdn.example/loop")
    assert len(internet.fetched) == images.MAX_REDIRECTS + 1


def test_image_downloads_are_capped_in_size_and_time(internet, monkeypatch):
    assert images.MAX_IMAGE_BYTES == 15 * 1024 * 1024
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 2 * 1024 * 1024)
    internet.pages = {
        "https://ok/declared.png": dict(body=b"x", declared=50 * 1024 * 1024),
        "https://ok/endless.png": dict(body=b"x" * (3 * 1024 * 1024)),  # no Content-Length: cut off while streaming
        "https://ok/fits.png": dict(body=b"x" * (1024 * 1024)),
    }
    for url in ("https://ok/declared.png", "https://ok/endless.png"):
        with pytest.raises(ImageDownloadError, match="larger than 2 MB"):
            images._download_image(url)
    assert len(images._download_image("https://ok/fits.png")[0]) == 1024 * 1024

    ticks = iter(range(0, 10_000, 20))  # every look at the clock is 20 s later
    monkeypatch.setattr(images.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ImageDownloadError, match="too long"):
        images._download_image("https://ok/fits.png")


def test_private_image_hosts_can_be_allowed_for_development_only(internet, monkeypatch):
    internet.pages = {"http://127.0.0.1:9925/img.png": dict(body=b"dev")}
    monkeypatch.setenv("ALLOW_PRIVATE_IMAGE_HOSTS", "1")
    assert images._download_image("http://127.0.0.1:9925/img.png") == (b"dev", "image/png")
    with pytest.raises(ImageDownloadError):  # still http(s) only
        images._download_image("file:///etc/passwd")


def test_summarize_exposes_the_image_version_token():
    assert recipes.summarize({"id": "1", "name": "Soup", "image": "ab12"})["image"] == "ab12"
    assert recipes.summarize({"id": "1", "name": "Soup", "image": None})["image"] is None
    assert recipes.summarize({"id": "1", "name": "Soup"})["image"] is None
