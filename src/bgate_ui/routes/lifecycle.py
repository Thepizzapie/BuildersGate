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
def merge_order(seat: str = "") -> dict:
    """What has to land first: waves, the critical path, the human steps."""
    from bgate_core.board import mergeorder as _mergeorder
    return api.ok(_mergeorder.order(root(), seat=seat))


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
