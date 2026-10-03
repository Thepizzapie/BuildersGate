"""The board as a source-control history - the orchestration page's graph.

Read-only plus the checkpoint detail. Approving and rejecting a checkpoint go
through the existing /api/queue/{id}/approve and /reject routes: a checkpoint
IS an item waiting in review, and one sign-off path is the whole point.

Auto-registers via routes/__init__.py. Envelope and errors per bgate_ui/api.py.
"""
from __future__ import annotations

from bgate_core.board import lifecycle as _lifecycle
from typing import Optional

from fastapi import APIRouter, Body

from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/lifecycle")
def lifecycle_graph(hours: float = 48, limit: int = 300) -> dict:
    """Tickets as commits, chains as lanes, dependencies as merge lines."""
    return api.ok(_lifecycle.graph(root(), hours=max(0.0, float(hours)),
                                   limit=max(1, min(int(limit), 1500))))


@router.get("/api/lifecycle/merge-order")
def merge_order(seat: str = "", hours: float = -1) -> dict:
    """What has to land first: waves, the critical path, the human steps.
    With ``hours`` >= 0 it also carries what already landed in that window
    (0 = all of it), oldest first, so the view reads as one history."""
    from bgate_core.board import mergeorder as _mergeorder
    got = _mergeorder.order(root(), seat=seat)
    if hours >= 0:
        got["landed"] = _mergeorder.history(root(), hours=hours, seat=seat)["landed"]
    return api.ok(got)


@router.post("/api/queue/{item_id}/human-done")
def human_done(item_id: int, payload: Optional[dict] = None) -> dict:
    """The human finished a human step; the work waiting on it is released."""
    from bgate_core.board import queue as _queue
    try:
        return api.ok(_queue.human_done(root(), item_id,
                                        note=str((payload or {}).get("note") or ""),
                                        by="human"))
    except LookupError as exc:
        raise api.not_found(str(exc))
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.post("/api/queue/{item_id}/checkpoint")
def checkpoint_toggle(item_id: int, payload: dict = Body(...)) -> dict:
    """Make an item a human checkpoint, or stop it being one. Human-only by
    being an HTTP route: the agents' path (queue_update) refuses removal."""
    from bgate_core.board import queue as _queue
    try:
        return api.ok(_queue.set_checkpoint(root(), item_id, bool(payload.get("on")),
                                            note=str(payload.get("note") or "")))
    except LookupError as exc:
        raise api.not_found(str(exc))
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.post("/api/queue/{item_id}/human-before")
def human_before(item_id: int, payload: dict = Body(...)) -> dict:
    """File a human step this item waits on."""
    from bgate_core.board import queue as _queue
    try:
        return api.ok(_queue.add_human_task(root(), str(payload.get("title") or ""),
                                            why=str(payload.get("why") or ""),
                                            blocks=[item_id], by="human"))
    except LookupError as exc:
        raise api.not_found(str(exc))
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.get("/api/lifecycle/checkpoint/{item_id}")
def checkpoint(item_id: int) -> dict:
    """What approving this checkpoint signs off: everything since the last."""
    try:
        return api.ok(_lifecycle.checkpoint_detail(root(), item_id))
    except LookupError as exc:
        raise api.not_found(str(exc))


@router.get("/api/lifecycle/proof/{item_id}")
def proof(item_id: int) -> dict:
    """What the harness recorded for a ticket: commits, test runs, check-tool
    verdicts, images. The agent's own note comes back labelled as a claim."""
    from bgate_core.board import proof as _proof
    try:
        return api.ok(_proof.proof(root(), item_id))
    except LookupError as exc:
        raise api.not_found(str(exc))


@router.get("/api/lifecycle/proof/{item_id}/diff")
def proof_diff(item_id: int, sha: str = "") -> dict:
    from bgate_core.board import proof as _proof
    try:
        return api.ok({"sha": sha, "diff": _proof.diff(root(), sha)})
    except ValueError as exc:
        raise api.bad_request(str(exc))
    except LookupError as exc:
        raise api.not_found(str(exc))


@router.get("/api/lifecycle/proof-image")
def proof_image(path: str = ""):
    from fastapi.responses import FileResponse
    from bgate_core.board import proof as _proof
    try:
        return FileResponse(_proof.image_path(root(), path))
    except ValueError as exc:
        raise api.bad_request(str(exc))
    except LookupError as exc:
        raise api.not_found(str(exc))


@router.post("/api/lifecycle/proof/{item_id}/run")
def proof_run(item_id: int) -> dict:
    """Run the project's test suite NOW, from the dashboard, and record it
    against this ticket - the one result no agent wrote."""
    from bgate_core.board import proof as _proof
    from bgate_core.runtime import enginetests as _et
    got = _et.run(root(), actor=_proof.human_actor(item_id), mode="summary")
    return api.ok({k: got.get(k) for k in (
        "ok", "no_tests", "scripts_run", "scripts_failed", "assertions_passed",
        "assertions_failed", "seconds", "error", "why", "log")})


@router.post("/api/lifecycle/proof/{item_id}/shot")
def proof_shot(item_id: int) -> dict:
    """Photograph the running game now and file it against this ticket."""
    from bgate_core.board import proof as _proof
    return api.ok(_proof.capture(root(), item_id, by="human"))
