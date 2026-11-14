"""Generate: ask Claude for recipes, review them, import the chosen ones; and
import recipes from URLs with Mealie's scraper."""
from __future__ import annotations

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from core import generate as generation, recipes as recipe_ops
from core.errors import GenerationError
from .. import deps, jobs

router = APIRouter()

MAX_URLS = 25


def job_redirect(job_id: str, busy: bool = False) -> RedirectResponse:
    return RedirectResponse(f"/jobs/{job_id}" + ("?busy=1" if busy else ""), status_code=303)


def start(kind: str, title: str, func, params=None) -> RedirectResponse:
    """Start a job and send the browser to its page - or, when a job of that
    kind is already running, to that one."""
    try:
        return job_redirect(jobs.submit(kind, title, func, params))
    except jobs.JobBusy as busy:
        return job_redirect(busy.job_id, busy=True)


# --- job functions -----------------------------------------------------------

def _run_generate(ctx: jobs.JobContext) -> dict:
    params = ctx.params
    settings = deps.get_settings()
    existing = [r["name"] for r in deps.get_client().list_recipes()]
    ctx.progress(0, params["count"])

    def progress(message: str) -> None:
        ctx.log(message)
        match = re.match(r"\.\.\.(\d+)/(\d+) generated", message)
        if match:
            ctx.progress(min(int(match[1]), int(match[2])), int(match[2]))

    factory = deps.ai_client_factory
    recipes = generation.generate_recipes(
        settings, existing, count=params["count"], restrictions=params["restrictions"],
        progress=progress, ai_client=factory() if factory else None,
    )
    if not recipes:
        raise GenerationError("Claude did not return any usable recipes. Try again, or loosen the restrictions.")
    ctx.progress(params["count"], params["count"])
    return {"recipes": recipes, "requested": params["count"], "restrictions": params["restrictions"]}


def _run_import(ctx: jobs.JobContext) -> dict:
    recipes = ctx.params["recipes"]
    done = 0
    ctx.progress(0, len(recipes))
    ctx.log(f"Importing {len(recipes)} recipe(s) into Mealie...")

    def progress(message: str) -> None:
        nonlocal done
        ctx.log(message)
        if message.startswith(("OK:", "SKIP:", "FAIL:")):
            done += 1
            ctx.progress(done)

    results = generation.import_recipes(deps.get_client(), deps.get_settings(), recipes, progress=progress)
    return {"results": results, "source_job": ctx.params.get("source_job")}


def _ingredient_text(ingredient) -> str:
    if isinstance(ingredient, dict):
        return str(ingredient.get("display") or ingredient.get("originalText") or ingredient.get("note") or "").strip()
    return str(ingredient).strip()


def _scraped_summary(recipe) -> dict:
    """The few fields the result list shows, from an imported Mealie recipe or
    from the raw data a test scrape returns."""
    if not isinstance(recipe, dict):
        return {"name": None, "slug": None, "ingredients": [], "steps": 0}
    ingredients = [_ingredient_text(i) for i in recipe.get("recipeIngredient") or []]
    steps = recipe.get("recipeInstructions") or []
    return {
        "name": recipe.get("name"),
        "slug": recipe.get("slug"),
        "ingredients": [i for i in ingredients if i][:60],
        "steps": len(steps) if isinstance(steps, list) else 0,
    }


def _run_scrape(ctx: jobs.JobContext) -> dict:
    urls, dry_run = ctx.params["urls"], ctx.params["dry_run"]
    client = deps.get_client(read_timeout=120)
    results = []
    ctx.progress(0, len(urls))
    for i, url in enumerate(urls, 1):
        ctx.log(f"{'Testing' if dry_run else 'Importing'} {url}")
        result = recipe_ops.scrape_urls(client, [url], dry_run=dry_run, progress=ctx.log)[0]
        results.append({"url": url, "status": result["status"], "error": result["error"],
                        **_scraped_summary(result["recipe"])})
        ctx.progress(i)
    return {"results": results, "dry_run": dry_run}


# --- pages -------------------------------------------------------------------

def _page(request: Request, status_code: int = 200, **extra):
    settings = deps.get_settings()
    context = {
        "active": "generate",
        "anthropic_ready": deps.anthropic_ready(),
        "default_count": settings.suggest_count,
        "model": settings.claude_model,
        "recent": jobs.recent(6, kind="generate"),
        "form": {"count": settings.suggest_count, "restrictions": "", "urls": "", "dry_run": False},
        "errors": {},
    }
    context.update(extra)
    return deps.render(request, "generate/index.html", context, status_code=status_code)


@router.get("/generate")
def generate_page(request: Request):
    if deps.missing_config():
        raise deps.ConfigMissing(deps.missing_config())
    return _page(request)


@router.post("/generate")
def start_generate(request: Request, count: str = Form(""), restrictions: str = Form("")):
    if deps.missing_config():
        raise deps.ConfigMissing(deps.missing_config())
    restrictions = " ".join(restrictions.split())[:500]
    form = {"count": count, "restrictions": restrictions, "urls": "", "dry_run": False}
    if not deps.anthropic_ready():
        return _page(request, 400, form=form,
                     errors={"generate": "ANTHROPIC_API_KEY is not set, so recipes can't be generated."})
    if not count.strip().isdigit() or not 1 <= int(count) <= 50:
        return _page(request, 400, form=form, errors={"generate": "Choose how many recipes: a number from 1 to 50."})
    return start("generate", f"Generate {int(count)} recipe{'s' if int(count) != 1 else ''}", _run_generate,
                 {"count": int(count), "restrictions": restrictions})


@router.post("/generate/{job_id}/import")
async def start_import(request: Request, job_id: str):
    form = await request.form()
    job = jobs.get(job_id)
    if not job or job["kind"] != "generate" or job["status"] != "done":
        raise GenerationError("Those generated recipes are no longer available. Generate a new batch.")
    recipes = job["result"]["recipes"]
    chosen = sorted({int(i) for i in form.getlist("pick") if i.isdigit() and int(i) < len(recipes)})
    if not chosen:
        raise GenerationError("Tick at least one recipe to import.")
    selected = [recipes[i] for i in chosen]
    return start("import", f"Import {len(selected)} generated recipe{'s' if len(selected) != 1 else ''}",
                 _run_import, {"recipes": selected, "source_job": job_id})


@router.post("/generate/scrape")
def start_scrape(request: Request, urls: str = Form(""), dry_run: str | None = Form(None)):
    if deps.missing_config():
        raise deps.ConfigMissing(deps.missing_config())
    wanted = list(dict.fromkeys(line.strip() for line in urls.splitlines()
                                if line.strip() and not line.strip().startswith("#")))
    form = {"urls": urls, "dry_run": bool(dry_run)}
    settings = deps.get_settings()
    form.update(count=settings.suggest_count, restrictions="")
    bad = [u for u in wanted if not u.lower().startswith(("http://", "https://"))]
    if not wanted:
        return _page(request, 400, form=form, errors={"scrape": "Paste at least one recipe web address."})
    if bad:
        return _page(request, 400, form=form,
                     errors={"scrape": f"Web addresses must start with http:// or https:// — check “{bad[0][:80]}”."})
    if len(wanted) > MAX_URLS:
        return _page(request, 400, form=form, errors={"scrape": f"That's {len(wanted)} addresses; import up to {MAX_URLS} at a time."})
    noun = f"{len(wanted)} URL{'s' if len(wanted) != 1 else ''}"
    return start("scrape", f"{'Preview' if dry_run else 'Import'} {noun}", _run_scrape,
                 {"urls": wanted, "dry_run": bool(dry_run)})
