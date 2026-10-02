"""The board as a source-control history - the orchestration page's graph.

Read-only plus the checkpoint detail. Approving and rejecting a checkpoint go
through the existing /api/queue/{id}/approve and /reject routes: a checkpoint
IS an item waiting in review, and one sign-off path is the whole point.

Auto-registers via routes/__init__.py. Envelope and errors per bgate_ui/api.py.
"""
from __future__ import annotations

from bgate_core.board import lifecycle as _lifecycle
from fastapi import APIRouter

from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/lifecycle")
def lifecycle_graph(hours: float = 48, limit: int = 300) -> dict:
    """Tickets as commits, chains as lanes, dependencies as merge lines."""
    return api.ok(_lifecycle.graph(root(), hours=max(0.0, float(hours)),
                                   limit=max(1, min(int(limit), 1500))))


@router.get("/api/lifecycle/checkpoint/{item_id}")
def checkpoint(item_id: int) -> dict:
    """What approving this checkpoint signs off: everything since the last."""
    try:
        return api.ok(_lifecycle.checkpoint_detail(root(), item_id))
    except LookupError as exc:
        raise api.not_found(str(exc))
