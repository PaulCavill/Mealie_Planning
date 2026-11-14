"""Maintenance: parse every recipe's ingredients, and the job history."""
from __future__ import annotations

import re

from fastapi import APIRouter, Request

from core import recipes as recipe_ops
from .. import deps, jobs
from .generate import start

router = APIRouter()

JOB_SECTION = {"generate": "generate", "import": "generate", "scrape": "generate", "parse": "maintenance"}
KIND_LABELS = {"generate": "Generate recipes", "import": "Import recipes", "scrape": "Import from URL",
               "parse": "Parse ingredients"}


def _run_parse(ctx: jobs.JobContext) -> dict:
    done = 0

    def progress(message: str) -> None:
        nonlocal done
        ctx.log(message.strip())
        total = re.match(r"Parsing ingredients for (\d+) recipes", message)
        if total:
            ctx.progress(0, int(total[1]))
        else:
            done += 1
            ctx.progress(done)

    summary = recipe_ops.parse_all_ingredients(deps.get_client(read_timeout=120), progress=progress)
    if not summary["results"]:
        ctx.log("There are no recipes in Mealie yet.")
    return summary


@router.get("/maintenance")
def maintenance_page(request: Request):
    return deps.render(request, "maintenance/index.html", {
        "active": "maintenance",
        "running": jobs.active("parse"),
        "history": jobs.recent(25),
        "kind_labels": KIND_LABELS,
        "mealie_ready": not deps.missing_config(),
    })


@router.post("/maintenance/parse")
def start_parse(request: Request):
    if deps.missing_config():
        raise deps.ConfigMissing(deps.missing_config())
    return start("parse", "Parse all ingredients", _run_parse)


@router.post("/maintenance/parse/{slug}")
def retry_parse(request: Request, slug: str):
    result = recipe_ops.parse_ingredients(deps.get_client(read_timeout=120), slug)
    return deps.render(request, "jobs/_parse_failure.html", {"r": result, "retried": True})


# --- job pages (shared by Generate and Maintenance) ----------------------------

def _job_context(job: dict) -> dict:
    percent = None
    if job["progress_total"]:
        percent = min(100, round(100 * (job["progress_done"] or 0) / job["progress_total"]))
    return {"job": job, "percent": percent, "kind_labels": KIND_LABELS,
            "active": JOB_SECTION.get(job["kind"], "maintenance")}


@router.get("/jobs/{job_id}")
def job_page(request: Request, job_id: str, busy: str = ""):
    job = jobs.get(job_id)
    if job is None:
        return deps.render(request, "error.html", {
            "active": "maintenance", "title": "Job not found",
            "message": "That job is no longer in the history. Only the most recent jobs are kept.",
        }, status_code=404)
    return deps.render(request, "jobs/job.html", {**_job_context(job), "busy": bool(busy)})


@router.get("/jobs/{job_id}/fragment")
def job_fragment(request: Request, job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return deps.toast(request, "That job is no longer in the history.")
    return deps.render(request, "jobs/_job.html", _job_context(job))
