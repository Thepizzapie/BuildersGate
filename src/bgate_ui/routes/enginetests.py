"""What the engine itself was asked to prove — the history, and the run button.

The QA seat's Tests tab drew an explanatory empty state forever, because
``godot_test_run`` handed its verdict to whoever called it and stored nothing.
These endpoints give the tab a real read: the scripts that exist, every recorded
run, and a way to produce one.

THE RUN IS A JOB. A headless Godot boot is seconds per script and a suite is
tens of seconds — long enough that a synchronous POST looks like a hung
dashboard at exactly the moment somebody is deciding whether to ship.

Auto-registers via routes/__init__.py — no edit to app.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from bgate_core.runtime import enginetests as _tests
from bgate_ui import api
from bgate_ui.deps import root
from bgate_ui.routes import jobs as _jobs

router = APIRouter()

RUN_JOB = "engine_tests"


@router.get("/api/engine-tests")
def engine_tests(limit: int = 20) -> dict:
    """The suite on disk and the runs recorded against it.

    Both halves, because "no runs recorded" and "this project has no tests" are
    different states and a panel that cannot tell them apart tells the reader
    to go write tests that are already written.
    """
    project = root()
    runs = _tests.history(project, limit=max(1, min(int(limit), 200)))
    return api.ok({
        **_tests.discover(project),
        "runs": runs,
        "last": runs[0] if runs else None,
    })


@router.post("/api/engine-tests/run")
def engine_tests_run(request: Request, payload: Optional[dict] = None) -> dict:
    """Run the suite headless and record the verdict. 202 + {job_id}."""
    body = payload or {}
    project = str(root())
    raw = body.get("paths")
    paths = [str(p) for p in raw] if isinstance(raw, list) and raw else None
    timeout = max(10, min(int(body.get("timeout") or 180), 900))
    actor = api.current_actor(request)

    def work(job_id: int) -> dict:
        try:
            return _tests.run(project, paths=paths, timeout=timeout,
                              actor=actor)
        except Exception as exc:                                 # noqa: BLE001
            return {"ok": False, "error": api.safe_error(exc)}

    if str(body.get("async", "")).strip().lower() in {"0", "false", "no"}:
        return api.ok(work(0))
    return api.ok(_jobs.start(RUN_JOB, work, request_body={"paths": paths or []},
                              request=request))
