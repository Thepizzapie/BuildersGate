"""The polling surface for long engine operations.

A Godot import or a headless bot match takes up to ninety seconds. Those used to
hold the HTTP request (and one of uvicorn's threadpool workers) open for the
whole duration, on a timeout the caller supplied in the request body — so the UI
had nothing to render but a spinner and no way to bound the wait.

The slow endpoints now also accept ``?async=1``: they start a job and answer 202
``{job_id}``. This module is where the UI watches it — ``{state, progress, stage,
result, error}`` — and where it gives up on one.

Cancellation is cooperative and honest about its limits. A running job is a
blocking engine subprocess on a daemon thread; killing it mid-import would leave
a half-written .godot cache. So cancel records intent, the work function checks
it at stage boundaries, and a job already inside the engine call runs to
completion — the response says which of the two happened rather than pretending.
"""
from __future__ import annotations

import json
import threading
from typing import Optional

from fastapi import APIRouter, Depends, Request

from bgate_core import db, jobs
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

# Cancel intent, process-local. The DB has no column for "asked to stop but not
# stopped yet", and a restart cancels every running job anyway (the threads die
# with the process), so this deliberately does not persist.
#
# Keyed by project too: job ids come from a per-project database, so a bare id
# would let a cancel in one project stop an unrelated job in another.
_CANCEL_REQUESTED: set[tuple[str, int]] = set()
_CANCEL_LOCK = threading.Lock()
_CANCEL_MAX = 512


def _key(job_id: int) -> tuple[str, int]:
    try:
        project = str(root())
    except Exception:
        project = ""
    return (project, int(job_id))


def request_cancel(job_id: int) -> None:
    with _CANCEL_LOCK:
        if len(_CANCEL_REQUESTED) >= _CANCEL_MAX:
            # Oldest ids are the least likely to still be running.
            for stale in sorted(_CANCEL_REQUESTED)[:_CANCEL_MAX // 2]:
                _CANCEL_REQUESTED.discard(stale)
        _CANCEL_REQUESTED.add(_key(job_id))


def is_cancelled(job_id: int) -> bool:
    """Called from inside a work function at every stage boundary."""
    with _CANCEL_LOCK:
        return _key(job_id) in _CANCEL_REQUESTED


def cancelled_result(stage: str = "") -> dict:
    """The shape a work function returns when it notices it was cancelled.

    ``jobs.run_in_background`` always finishes a returning function as ``done``;
    this marker is what :func:`view` reads to report — and persist — ``cancelled``
    instead, without a work function having to fake a failure to be heard.
    """
    return {"ok": False, "cancelled": True, "stage": stage,
            "error": "cancelled" + (f" during {stage}" if stage else "")}


def view(root_dir, job: dict) -> dict:
    """One job in the shape the UI polls for."""
    result = job.get("result") or {}
    state = job.get("status", "queued")
    if state == "done" and isinstance(result, dict) and result.get("cancelled"):
        # Reconcile once so history reads correctly after a restart, when the
        # in-memory cancel set is gone.
        state = "cancelled"
        try:
            with db.tx(root_dir) as conn:
                conn.execute("UPDATE job SET status = 'cancelled' WHERE id = ?",
                             (job["id"],))
        except Exception:
            pass
    return {
        "id": job.get("id"),
        "kind": job.get("kind", ""),
        "state": state,
        "terminal": state in jobs.TERMINAL,
        "progress": round(float(job.get("progress") or 0.0), 3),
        "stage": job.get("stage", ""),
        "result": result,
        "error": job.get("error", ""),
        "actor": job.get("actor", ""),
        "request": job.get("request") or {},
        "cancel_requested": is_cancelled(int(job.get("id") or 0)),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }


@router.get("/api/jobs")
def list_jobs(page: api.Page = Depends(), kind: str = "", status: str = "") -> dict:
    """Recent jobs, newest first. Counted in SQL so 'load more' can be honest."""
    root_dir = root()
    where, params = "", []
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    if status:
        where += " AND status = ?"
        params.append(status)
    conn = db.connect(root_dir)
    total = conn.execute(
        f"SELECT COUNT(*) FROM job WHERE 1=1{where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM job WHERE 1=1{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, page.limit, page.offset]).fetchall()
    return page.envelope([view(root_dir, _hydrate(r)) for r in rows], total)


@router.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    root_dir = root()
    job = jobs.get(root_dir, job_id)
    if job is None:
        raise api.not_found(f"no job {job_id}", job_id=job_id)
    return api.ok(view(root_dir, job))


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int, request: Request) -> dict:
    """Ask a job to stop.

    A queued job stops for certain. A running job stops at its next stage
    boundary — if it is already blocked in the engine call it will finish, and
    ``stopped: false`` says so rather than leaving the caller to guess.
    """
    root_dir = root()
    job = jobs.get(root_dir, job_id)
    if job is None:
        raise api.not_found(f"no job {job_id}", job_id=job_id)
    if job["status"] in jobs.TERMINAL:
        raise api.conflict(f"job {job_id} already {job['status']}",
                           job_id=job_id, state=job["status"])

    request_cancel(job_id)
    stopped = job["status"] == "queued"
    if stopped:
        jobs.finish(root_dir, job_id, status="cancelled",
                    result=cancelled_result("queued"),
                    error=f"cancelled by {api.current_actor(request)} before it started")
        job = jobs.get(root_dir, job_id) or job
    return api.ok({
        "stopped": stopped,
        "job": view(root_dir, job),
        "note": "" if stopped else
                "cancel requested — the job stops at its next stage boundary; "
                "an engine call already in flight runs to completion",
    })


def _hydrate(row) -> dict:
    """sqlite3.Row -> the dict shape jobs.get() returns (JSON columns decoded)."""
    out = dict(row)
    for field in ("request_json", "result_json"):
        try:
            out[field[:-5]] = json.loads(out.pop(field) or "{}")
        except Exception:
            out[field[:-5]] = {}
    return out


def start(kind: str, work, *, request_body: Optional[dict] = None,
          request: Optional[Request] = None) -> dict:
    """Start a background job and return the 202 body every async route shares."""
    job_id = jobs.run_in_background(root(), kind, work,
                                    request=request_body or {},
                                    actor=api.current_actor(request))
    return {"ok": True, "job_id": job_id, "kind": kind, "state": "queued",
            "poll": f"/api/jobs/{job_id}"}


def wants_async(payload: Optional[dict], flag: int | str = 0) -> bool:
    """``?async=1`` on the query string, or ``{"async": true}`` in the body.

    Two spellings because the callers differ: the seat JS posts a body, and a
    hand-driven curl or an agent reaches for the query string.
    """
    if str(flag).strip().lower() in {"1", "true", "yes", "on"}:
        return True
    raw = (payload or {}).get("async")
    return raw is not None and str(raw).strip().lower() in {"1", "true", "yes", "on"}
