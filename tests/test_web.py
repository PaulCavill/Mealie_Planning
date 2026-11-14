"""The web UI, driven through FastAPI's TestClient against the fake Mealie
server from dev/fake_mealie.py (run on a local port for the test session)."""
import json
import re
import socket
import sys
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dev"))

from core.errors import ImageSearchError  # noqa: E402
from core.settings import load_settings  # noqa: E402
from mealie_client import MealieClient  # noqa: E402

HTMX = {"HX-Request": "true"}
PAGES = ["/", "/recipes", "/generate", "/images", "/maintenance", "/settings"]


@pytest.fixture(scope="session")
def fake_mealie_url():
    import uvicorn
    import fake_mealie

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(fake_mealie.create_app(), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "fake Mealie did not start"
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def env(fake_mealie_url, tmp_path, monkeypatch):
    """A clean fake Mealie, an empty data directory and no optional settings."""
    requests.post(f"{fake_mealie_url}/__reset", timeout=10).raise_for_status()
    monkeypatch.setenv("MEALIE_BASE_URL", fake_mealie_url)
    monkeypatch.setenv("MEALIE_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name in ("APP_PASSWORD", "ANTHROPIC_API_KEY", "MEALIE_USERNAME", "MEALIE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    def no_network(*args, **kwargs):
        raise ImageSearchError("Image search failed: offline in tests")

    monkeypatch.setattr("core.images.search_image_candidates", no_network)
    return SimpleNamespace(url=fake_mealie_url, data_dir=tmp_path)


@pytest.fixture
def web(env):
    from web.app import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def mealie(env):
    return MealieClient(env.url, "test-token")


def finished(location: str) -> dict:
    """Wait for the job a redirect points at."""
    from web import jobs

    job = jobs.wait(location.split("?")[0].rsplit("/", 1)[1], timeout=60)
    assert job is not None and not job["active"], "job did not finish"
    return job


def dinners(mealie, days: list[str]) -> dict:
    entries = mealie.list_meal_plans(date.fromisoformat(min(days)), date.fromisoformat(max(days)))
    return {e["date"]: e for e in entries if e["entryType"] == "dinner"}


# --- pages ---------------------------------------------------------------------

def test_healthz_is_open_and_static_files_are_served(web):
    assert web.get("/healthz").json() == {"status": "ok"}
    assert "htmx" in web.get("/static/htmx.min.js").text
    assert web.get("/static/app.css").status_code == 200


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(web, path):
    response = web.get(path)
    assert response.status_code == 200
    assert "<nav" in response.text and "Traceback" not in response.text
    assert "http://" not in re.sub(r'href="http[^"]*"|127\.0\.0\.1:\d+', "", response.text.split("</head>")[0])


def test_nothing_is_loaded_from_another_host(web):
    for path in PAGES:
        html = web.get(path).text
        assert not re.findall(r'<(?:script|link|img)[^>]+(?:src|href)="(?:https?:)?//', html), path


def test_plan_page_shows_the_calendar_from_mealie(web, mealie):
    html = web.get("/").text
    today = date.today()
    planned = [e for e in mealie.list_meal_plans(today, today) if e["entryType"] == "dinner" and e.get("recipe")]
    assert "Generate a plan" in html and "This week" in html
    for entry in planned:
        assert entry["recipe"]["name"].replace("&", "&amp;") in html


def test_unknown_page_and_unknown_preview_are_friendly_404s(web):
    for path in ("/no-such-page", "/plan/preview/nope", "/jobs/nope"):
        response = web.get(path)
        assert response.status_code == 404 and "<nav" in response.text


def test_recipe_images_are_proxied_with_a_placeholder_fallback(web, mealie):
    recipe = mealie.list_recipes()[0]
    image = web.get(f"/media/recipe/{recipe['id']}?v=abc")
    assert image.status_code == 200 and image.headers["content-type"].startswith("image/")
    assert "max-age" in image.headers["cache-control"]
    for missing in ("/media/recipe/not-a-real-recipe", "/media/recipe/a%20b"):
        fallback = web.get(missing)
        assert fallback.status_code == 200 and "No image" in fallback.text


# --- plan: preview -> reroll -> choose -> save -----------------------------------

def test_plan_preview_reroll_choose_and_commit(web, mealie):
    from web import previews

    created = web.post("/plan/preview", data={"start": "", "shopping_list": "1"}, follow_redirects=False)
    assert created.status_code == 303
    location = created.headers["location"]
    preview_id = location.rsplit("/", 1)[1]

    page = web.get(location)
    assert page.status_code == 200 and "Nothing has been saved yet" in page.text and "Save to Mealie" in page.text

    def stored_days():
        return [d for week in previews.get(preview_id)["data"]["weeks"] for d in week["days"]]

    proposed = [d for d in stored_days() if d["type"] == "recipe"]
    assert proposed, "the seeded fake leaves days to plan"
    all_days = [d["date"] for d in stored_days()]
    before = dinners(mealie, all_days)
    assert not set(before) & {d["date"] for d in proposed}  # the preview wrote nothing

    target = proposed[0]
    fragment = web.post(f"/plan/preview/{preview_id}/reroll", data={"date": target["date"]}, headers=HTMX)
    assert fragment.status_code == 200 and fragment.text.lstrip().startswith('<div id="preview-body"')
    rerolled = next(d for d in stored_days() if d["date"] == target["date"])
    assert rerolled["recipe_id"] != target["recipe_id"]

    picker = web.get(f"/plan/preview/{preview_id}/choose", params={"date": target["date"], "effort": ""}, headers=HTMX)
    assert picker.status_code == 200 and "Choose dinner" in picker.text
    used = {d["recipe_id"] for d in stored_days()}
    choice = next(r for r in mealie.list_dinner_recipes() if r["id"] not in used)
    chosen = web.post(f"/plan/preview/{preview_id}/set", data={"date": target["date"], "recipe_id": choice["id"]},
                      headers=HTMX)
    assert chosen.status_code == 200 and choice["name"].replace("&", "&amp;") in chosen.text
    assert dinners(mealie, all_days).keys() == before.keys()  # still nothing written

    expected = {d["date"]: d["recipe_id"] for d in stored_days() if d["type"] == "recipe"}
    assert expected[target["date"]] == choice["id"]
    lists_before = len(mealie.list_shopping_lists())
    saved = web.post(f"/plan/preview/{preview_id}/commit", data={"shopping_list": "1"}, follow_redirects=False)
    assert saved.status_code == 303
    done = web.get(saved.headers["location"])
    assert "Plan saved" in done.text and f"{len(expected)} dinners added" in done.text

    after = dinners(mealie, all_days)
    assert {day: after[day]["recipe"]["id"] for day in expected} == expected
    assert len(mealie.list_shopping_lists()) == lists_before + len(previews.get(preview_id)["data"]["weeks"])

    # Saving twice (double click, refresh) does not add the dinners again.
    again = web.post(f"/plan/preview/{preview_id}/commit", follow_redirects=False)
    assert again.status_code == 303
    assert len(mealie.list_meal_plans(date.fromisoformat(min(all_days)), date.fromisoformat(max(all_days)))) == \
        len(before) + len(expected)
    # ...and a saved preview can no longer be changed.
    late = web.post(f"/plan/preview/{preview_id}/reroll", data={"date": target["date"]}, headers=HTMX)
    assert "already been saved" in late.text and late.headers["hx-retarget"] == "#toast"


def test_override_preview_lists_deletions_and_discard_removes_it(web, mealie):
    from web import previews

    created = web.post("/plan/preview", data={"start": "", "override": "1"}, follow_redirects=False)
    preview_id = created.headers["location"].rsplit("/", 1)[1]
    record = previews.get(preview_id)
    assert record["data"]["override"] is True and record["data"]["deletions"]
    assert "Saving will remove" in web.get(created.headers["location"]).text

    assert "unsaved plan preview" in web.get("/").text
    assert web.post(f"/plan/preview/{preview_id}/discard", follow_redirects=False).headers["location"] == "/"
    assert previews.get(preview_id) is None
    assert web.get(f"/plan/preview/{preview_id}").status_code == 404


def test_swap_replaces_a_planned_dinner(web, mealie):
    today = date.today()
    entry = next(e for e in mealie.list_meal_plans(today, None) if e["entryType"] == "dinner" and e.get("recipe"))
    picker = web.get("/plan/swap", params={"date": entry["date"]}, headers=HTMX)
    assert picker.status_code == 200 and entry["recipe"]["name"].replace("&", "&amp;") in picker.text

    narrowed = web.get("/plan/swap", params={"date": entry["date"], "q": "zzzz-no-such"},
                       headers={**HTMX, "HX-Target": "picker-results"})
    assert "No dinner recipes match" in narrowed.text and "<form" not in narrowed.text

    planned = {e["recipe"]["id"] for e in mealie.list_meal_plans(today, None) if e.get("recipe")}
    replacement = next(r for r in mealie.list_dinner_recipes() if r["id"] not in planned)
    swapped = web.post("/plan/swap", data={"date": entry["date"], "recipe_id": replacement["id"]}, headers=HTMX)
    assert swapped.status_code == 200 and swapped.text.lstrip().startswith('<section id="calendar"')
    now = dinners(mealie, [entry["date"]])[entry["date"]]
    assert now["recipe"]["id"] == replacement["id"] and now["id"] != entry["id"]


# --- recipes -------------------------------------------------------------------

def test_recipe_filters_and_search(web, mealie):
    everything = mealie.list_recipes()
    not_dinner = [r for r in everything if "dinner" not in {t["name"] for t in r["tags"]}]
    html = web.get("/recipes", params={"filter": "not_dinner"}).text
    assert html.count('class="recipe-row') == len(not_dinner)
    name = everything[0]["name"]
    found = web.get("/recipes", params={"q": name}, headers=HTMX).text
    assert name.replace("&", "&amp;") in found and "<nav" not in found
    assert "No recipes found" in web.get("/recipes", params={"q": "zzzz-no-such"}).text


def test_dinner_and_effort_toggles_save_immediately(web, mealie):
    recipe = next(r for r in mealie.list_recipes() if "dinner" not in {t["name"] for t in r["tags"]})
    slug = recipe["slug"]

    def tags():
        return {t["name"] for t in mealie.get_recipe(slug)["tags"]}

    row = web.post(f"/recipes/{slug}/dinner", data={"value": "1"}, headers=HTMX)
    assert row.status_code == 200 and row.text.lstrip().startswith("<li") and 'aria-checked="true"' in row.text
    assert "dinner" in tags()

    row = web.post(f"/recipes/{slug}/effort", data={"effort": "4"}, headers=HTMX)
    assert row.status_code == 200 and "is-on e4" in row.text
    assert "effort-4" in tags()
    web.post(f"/recipes/{slug}/effort", data={"effort": "2"}, headers=HTMX)
    assert {"effort-2", "dinner"} <= tags() and "effort-4" not in tags()

    row = web.post(f"/recipes/{slug}/effort", data={"effort": ""}, headers=HTMX)  # tapping the active number clears it
    assert "is-on" not in row.text and not any(t.startswith("effort-") for t in tags())
    row = web.post(f"/recipes/{slug}/dinner", data={"value": "0"}, headers=HTMX)
    assert 'aria-checked="false"' in row.text and "dinner" not in tags()


# --- settings ------------------------------------------------------------------

def settings_form(**changes) -> dict:
    from web.routes.settings import _values

    values = _values(load_settings())
    form = {k: str(v) for k, v in values.items() if k not in ("plan_weekdays", "effort_max")}
    form["plan_weekdays"] = values["plan_weekdays"]
    form.update({f"effort_max_{day}": cap for day, cap in values["effort_max"].items()})
    form.update(changes)
    return form


def test_settings_save_round_trips_every_field(web):
    page = web.get("/settings")
    assert "Connected as" in page.text and "test-token" not in page.text

    form = settings_form(adults="2", children="3", child_weight="0.25", plan_weekdays=["0", "3"],
                         week_start_day="0", effort_max_0="", effort_max_3="2",
                         pantry_staples="Salt\r\nrice\r\n\r\nsalt", system_prompt="Cook for {household}.\r\nServes {servings}; skip {existing}")
    saved = web.post("/settings", data=form, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"] == "/settings?saved=1"
    assert "Settings saved" in web.get("/settings?saved=1").text

    settings = load_settings()
    assert (settings.adults, settings.children, settings.child_weight) == (2, 3, 0.25)
    assert settings.plan_weekdays == [0, 3] and settings.week_start_day == 0
    assert settings.day_effort_max == {2: 3, 3: 2}  # Monday's limit removed, Wednesday's kept, Thursday's added
    assert settings.pantry_staples == ["salt", "rice"]
    assert settings.system_prompt == "Cook for {household}.\nServes {servings}; skip {existing}"


def test_settings_validation_errors_are_shown_and_nothing_is_saved(web):
    form = settings_form(adults="lots", child_weight="3", system_prompt="Hello {nobody}")
    form.pop("plan_weekdays")
    rejected = web.post("/settings", data=form)
    assert rejected.status_code == 400
    assert "adults: invalid value" in rejected.text and 'value="lots"' in rejected.text  # input kept for fixing
    assert load_settings().adults == 3

    form["adults"] = "2"
    rejected = web.post("/settings", data=form)
    assert rejected.status_code == 400
    for problem in ("child_weight must be between 0 and 1", "plan_weekdays needs at least one day",
                    "system_prompt may only use the placeholders"):
        assert problem in rejected.text
    assert load_settings().adults == 3


# --- configuration, errors, login --------------------------------------------------

def test_missing_configuration_shows_a_setup_notice_on_every_page(web, monkeypatch):
    monkeypatch.delenv("MEALIE_BASE_URL")
    monkeypatch.delenv("MEALIE_TOKEN")
    for path in PAGES:
        response = web.get(path)
        assert response.status_code == 200, path
        assert "MEALIE_BASE_URL" in response.text and "MEALIE_TOKEN" in response.text, path
    assert "Connect the planner to Mealie" in web.get("/").text
    assert "Not configured" in web.get("/settings").text
    assert web.get("/healthz").status_code == 200
    toast = web.post("/recipes/x/dinner", data={"value": "1"}, headers=HTMX)
    assert toast.headers["hx-retarget"] == "#toast" and "MEALIE_BASE_URL" in toast.text


def test_unreachable_mealie_is_a_friendly_message(web, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    monkeypatch.setenv("MEALIE_BASE_URL", f"http://127.0.0.1:{closed_port}")

    page = web.get("/")
    assert page.status_code == 502
    assert "Could not reach Mealie" in page.text and "Traceback" not in page.text and "<nav" in page.text
    toast = web.post("/recipes/x/effort", data={"effort": "3"}, headers=HTMX)
    assert toast.status_code == 200 and toast.headers["hx-retarget"] == "#toast"
    assert "Could not reach Mealie" in toast.text and "<nav" not in toast.text
    assert "Could not reach Mealie" in web.get("/settings").text
    assert "No image" in web.get("/media/recipe/abc").text


def test_rejected_token_and_core_errors_are_friendly(web, monkeypatch):
    bad_date = web.post("/plan/preview", data={"start": "next tuesday"})
    assert bad_date.status_code == 400 and "is not a date" in bad_date.text and "<nav" in bad_date.text

    def unauthorized(self, path, **params):
        response = requests.Response()
        response.status_code = 401
        raise requests.HTTPError("401 Client Error", response=response)

    monkeypatch.setattr(MealieClient, "get", unauthorized)
    page = web.get("/recipes")
    assert page.status_code == 502 and "rejected the API token" in page.text and "Traceback" not in page.text
    assert "Mealie rejected the token" in web.get("/settings").text


def test_login_is_required_only_when_a_password_is_set(web, monkeypatch):
    assert web.get("/", follow_redirects=False).status_code == 200  # no APP_PASSWORD: open
    assert web.get("/login", follow_redirects=False).headers["location"] == "/"
    assert "Log out" not in web.get("/").text

    monkeypatch.setenv("APP_PASSWORD", "s3cret-dinner")
    origin = {"Origin": "http://testserver"}
    blocked = web.get("/recipes?q=pie", follow_redirects=False)
    assert blocked.status_code == 303 and blocked.headers["location"] == "/login?next=%2Frecipes%3Fq%3Dpie"
    assert web.get("/media/recipe/abc", follow_redirects=False).status_code == 303
    fragment = web.get("/recipes", headers=HTMX, follow_redirects=False)
    assert fragment.status_code == 401 and fragment.headers["hx-redirect"] == "/login"
    assert web.get("/healthz").status_code == 200 and web.get("/static/app.css").status_code == 200
    assert "s3cret-dinner" not in web.get("/login").text

    wrong = web.post("/login", data={"password": "guess", "next": "/recipes"}, headers=origin)
    assert wrong.status_code == 401 and "not right" in wrong.text and "planner_session" not in web.cookies
    cross_site = web.post("/login", data={"password": "s3cret-dinner"}, headers={"Origin": "http://evil.example"})
    assert cross_site.status_code == 403 and "planner_session" not in web.cookies
    assert web.post("/login", data={"password": "s3cret-dinner"}).status_code == 403  # no Origin or Referer at all

    ok = web.post("/login", data={"password": "s3cret-dinner", "next": "//evil.example/x"}, headers=origin,
                  follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert "httponly" in ok.headers["set-cookie"].lower() and "samesite=lax" in ok.headers["set-cookie"].lower()
    page = web.get("/")
    assert page.status_code == 200 and "Log out" in page.text

    # Logged in, but a state-changing request from another site is still refused.
    forged = web.post("/plan/preview", data={"start": ""}, headers={"Origin": "http://evil.example"},
                      follow_redirects=False)
    assert forged.status_code == 403
    by_referer = web.post("/plan/preview", data={"start": ""}, headers={"Referer": "http://testserver/"},
                          follow_redirects=False)
    assert by_referer.status_code == 303

    monkeypatch.setenv("APP_PASSWORD", "changed")  # changing the password ends old sessions
    assert web.get("/", follow_redirects=False).status_code == 303
    monkeypatch.setenv("APP_PASSWORD", "s3cret-dinner")
    assert web.get("/", follow_redirects=False).status_code == 200
    out = web.post("/logout", headers=origin, follow_redirects=False)
    assert out.headers["location"] == "/login"
    assert web.get("/", follow_redirects=False).status_code == 303


def test_tampered_or_expired_session_cookies_are_rejected(env, monkeypatch):
    from web import auth

    monkeypatch.setenv("APP_PASSWORD", "pw")
    token = auth.new_session()
    assert auth.valid_session(token)
    expiry, signature = token.split(".")
    assert not auth.valid_session(f"{int(expiry) + 1}.{signature}")
    assert not auth.valid_session(f"{expiry}.{'0' * len(signature)}")
    monkeypatch.setattr(auth.time, "time", lambda: int(expiry) + 1)
    assert not auth.valid_session(token)
    for junk in (None, "", "abc", "1.2.3"):
        assert not auth.valid_session(junk)


# --- background jobs -----------------------------------------------------------------

def test_parse_ingredients_job_runs_to_completion(web, mealie):
    note_only = next(r for r in mealie.list_recipes() if r["name"] == "Beef Rendang")
    assert not any(i.get("food") for i in mealie.get_recipe(note_only["slug"])["recipeIngredient"])

    started = web.post("/maintenance/parse", follow_redirects=False)
    assert started.status_code == 303 and started.headers["location"].startswith("/jobs/")
    job = finished(started.headers["location"])
    total = len(mealie.list_recipes())
    assert job["status"] == "done" and job["result"]["parsed"] + job["result"]["failed"] == total
    assert (job["progress_done"], job["progress_total"]) == (total, total)
    assert len(job["log"]) == total + 1 and job["duration"] is not None
    assert any(i.get("food") for i in mealie.get_recipe(note_only["slug"])["recipeIngredient"])

    page = web.get(started.headers["location"])
    assert page.status_code == 200 and "hx-trigger" not in page.text  # finished: no more polling
    fragment = web.get(started.headers["location"] + "/fragment", headers=HTMX)
    assert fragment.text.lstrip().startswith('<div id="job"') and "Finished" in fragment.text
    history = web.get("/maintenance").text
    assert "Parse all ingredients" in history and "View log" in history

    retry = web.post(f"/maintenance/parse/{note_only['slug']}", headers=HTMX)
    assert retry.status_code == 200 and "Parsed" in retry.text


class FakeAnthropic:
    """Stands in for anthropic.Anthropic: returns as many recipes as were asked for."""

    def __init__(self):
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        count = int(re.search(r"exactly (\d+)", kwargs["messages"][0]["content"])[1])
        batch = len(self.requests)
        recipes = [{
            "@context": "https://schema.org", "@type": "Recipe",
            "name": f"Test Kitchen Curry {batch}-{i}", "description": "A <b>bold</b> curry.",
            "recipeYield": "4 servings", "totalTime": "PT45M", "recipeCuisine": "Indian",
            "recipeIngredient": ["500 g chicken thighs", "1 cup rice"],
            "recipeInstructions": [{"@type": "HowToStep", "text": "Cook it."}],
            "effort": 2 + i % 2, "url": f"https://example.com/curry-{batch}-{i}",
        } for i in range(count)]
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(recipes))])


def test_generate_review_and_import_with_a_fake_claude(web, mealie, monkeypatch):
    from web import deps

    page = web.get("/generate")
    assert "ANTHROPIC_API_KEY" in page.text and "switched off" in page.text
    refused = web.post("/generate", data={"count": "3"})
    assert refused.status_code == 400 and "ANTHROPIC_API_KEY is not set" in refused.text

    ai = FakeAnthropic()
    monkeypatch.setattr(deps, "ai_client_factory", lambda: ai)
    assert "switched off" not in web.get("/generate").text
    assert web.post("/generate", data={"count": "0"}).status_code == 400

    before = len(mealie.list_recipes())
    started = web.post("/generate", data={"count": "7", "restrictions": "no  fish"}, follow_redirects=False)
    assert started.status_code == 303
    job = finished(started.headers["location"])
    assert job["status"] == "done", job["error"]
    assert len(job["result"]["recipes"]) == 7 and len(ai.requests) == 2  # batches of 5 + 2
    assert "Additional restriction: no fish." in ai.requests[0]["messages"][0]["content"]
    assert len(mealie.list_recipes()) == before  # generating imports nothing

    review = web.get(started.headers["location"]).text
    assert "Review 7 generated recipes" in review and "Test Kitchen Curry 1-0" in review
    assert "A &lt;b&gt;bold&lt;/b&gt; curry." in review and "https://example.com/curry-1-0" in review
    assert "Test Kitchen Curry" in web.get("/generate").text or "Generate 7 recipes" in web.get("/generate").text

    nothing = web.post(f"/generate/{job['id']}/import", data={})
    assert nothing.status_code == 400 and "Tick at least one" in nothing.text

    imported = web.post(f"/generate/{job['id']}/import", data={"pick": ["0", "6"]}, follow_redirects=False)
    assert imported.status_code == 303
    result = finished(imported.headers["location"])
    assert result["status"] == "done", result["error"]
    assert [(r["name"], r["status"]) for r in result["result"]["results"]] == [
        ("Test Kitchen Curry 1-0", "imported"), ("Test Kitchen Curry 2-1", "imported")]
    assert "2 imported" in web.get(imported.headers["location"]).text

    by_name = {r["name"]: r for r in mealie.list_recipes()}
    assert len(by_name) == before + 2
    assert {t["name"] for t in by_name["Test Kitchen Curry 1-0"]["tags"]} >= {"dinner", "effort-2"}
    assert {t["name"] for t in by_name["Test Kitchen Curry 2-1"]["tags"]} >= {"dinner", "effort-3"}


def test_import_from_url_preview_and_real_import(web, mealie):
    before = len(mealie.list_recipes())
    assert web.post("/generate/scrape", data={"urls": "not a url"}).status_code == 400
    assert web.post("/generate/scrape", data={"urls": "  \n"}).status_code == 400

    urls = "https://example.com/recipes/lemon-chicken-traybake\nhttps://fail.example.com/broken\n"
    preview = web.post("/generate/scrape", data={"urls": urls, "dry_run": "1"}, follow_redirects=False)
    job = finished(preview.headers["location"])
    assert job["status"] == "done" and job["result"]["dry_run"] is True
    assert [r["status"] for r in job["result"]["results"]] == ["ok", "error"]
    assert len(mealie.list_recipes()) == before
    assert "nothing was imported" in web.get(preview.headers["location"]).text

    real = web.post("/generate/scrape", data={"urls": urls}, follow_redirects=False)
    job = finished(real.headers["location"])
    assert [r["status"] for r in job["result"]["results"]] == ["ok", "error"]
    assert job["result"]["results"][0]["slug"] and len(mealie.list_recipes()) == before + 1
    assert "1 of 2 imported" in web.get(real.headers["location"]).text


def test_only_one_job_of_a_kind_runs_and_failures_are_recorded(env):
    from web import jobs

    release = threading.Event()

    def slow(ctx):
        ctx.log("waiting")
        release.wait(10)
        return {"ok": True}

    first = jobs.submit("parse", "Slow parse", slow)
    with pytest.raises(jobs.JobBusy) as busy:
        jobs.submit("parse", "Second parse", slow)
    assert busy.value.job_id == first
    other = jobs.submit("scrape", "A different kind", lambda ctx: {"n": ctx.params["n"] + 1}, {"n": 1})
    assert jobs.wait(other)["result"] == {"n": 2}
    release.set()
    assert jobs.wait(first)["status"] == "done"

    def broken(ctx):
        raise RuntimeError("secret internals")

    failed = jobs.wait(jobs.submit("parse", "Broken", broken))
    assert failed["status"] == "failed" and "secret internals" not in failed["error"]
    assert [j["title"] for j in jobs.recent(10)] == ["Broken", "A different kind", "Slow parse"]


def test_a_second_request_is_sent_to_the_running_job(web, monkeypatch):
    from web import jobs

    release = threading.Event()
    running = jobs.submit("parse", "Parse all ingredients", lambda ctx: release.wait(10) and {"results": [], "parsed": 0, "failed": 0})
    try:
        assert "Watch its progress" in web.get("/maintenance").text
        polling = web.get(f"/jobs/{running}")
        assert 'hx-trigger="load delay:1s"' in polling.text
        again = web.post("/maintenance/parse", follow_redirects=False)
        assert again.headers["location"] == f"/jobs/{running}?busy=1"
        assert "already running" in web.get(again.headers["location"]).text
    finally:
        release.set()
    assert jobs.wait(running)["status"] == "done"


def test_jobs_cut_off_by_a_restart_are_marked_failed_at_startup(env):
    from contextlib import closing

    from web import jobs
    from web.app import app

    with closing(jobs._connect()) as conn, conn:
        conn.execute("INSERT INTO web_jobs (id, kind, title, status, created_at, started_at) "
                     "VALUES ('old', 'parse', 'Parse all ingredients', 'running', ?, ?)", (time.time(), time.time()))
    with TestClient(app) as client:  # startup
        job = jobs.get("old")
        assert job["status"] == "failed" and "restarted" in job["error"]
        assert "did not finish" in client.get("/jobs/old").text
        assert client.post("/maintenance/parse", follow_redirects=False).headers["location"] != "/jobs/old?busy=1"


# --- images ------------------------------------------------------------------------

def test_image_page_search_failure_and_setting_an_image(web, mealie, monkeypatch):
    recipe = next(r for r in mealie.list_recipes() if not r.get("image"))
    assert "No image" in web.get("/images", params={"missing": "1"}).text
    detail = web.get(f"/images/{recipe['slug']}")
    assert detail.status_code == 200 and "Find images" in detail.text

    failed = web.get(f"/images/{recipe['slug']}/search", headers=HTMX)
    assert failed.status_code == 200 and "isn&#39;t available right now" in failed.text

    found = [{"url": "https://img.example/a.jpg", "thumbnail": "https://img.example/a_t.jpg", "title": "A",
              "source": "", "width": 800, "height": 600}]
    monkeypatch.setattr("core.images.search_image_candidates", lambda *a, **k: found)
    grid = web.get(f"/images/{recipe['slug']}/search", params={"q": "something else"}, headers=HTMX)
    assert "https://img.example/a_t.jpg" in grid.text and "800×600" in grid.text

    monkeypatch.setattr("core.images._download_image", lambda url, default_content_type="": (b"\x89PNG fake", "image/png"))
    updated = web.post(f"/images/{recipe['slug']}/set", data={"url": "https://img.example/a.jpg"}, headers=HTMX)
    assert "Image updated" in updated.text and mealie.get_recipe(recipe["slug"])["image"]

    monkeypatch.setattr("core.images._download_image", lambda url, default_content_type="": None)
    refused = web.post(f"/images/{recipe['slug']}/set", data={"url": "https://img.example/b.jpg"}, headers=HTMX)
    assert "could not be used" in refused.text
    assert "starting with http" in web.post(f"/images/{recipe['slug']}/set", data={"url": "ftp://x"}, headers=HTMX).text
    assert web.get("/images/no-such-recipe").status_code == 502
