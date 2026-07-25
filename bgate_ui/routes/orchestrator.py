"""Orchestrator / control-tower endpoints — the DIRECTOR seat's cockpit.

The director seat manages many agents at once. Most of what it needs already
exists (/api/queue, /api/agents, /api/agent-activity); this module adds the
two orchestration-specific endpoints:

  * POST /api/orchestrator/delegate — spawn a director agent that reads a queued
    item, decides whether it's one task or a split across seats, and delegates
    the pieces with queue_add before completing.
  * GET  /api/orchestrator/overview — one call: the queue grouped by seat plus
    the live agent table, so the board can paint in a single round-trip.

Never raises through to the client with a bare stack: LookupError -> 404, bad
input -> 400. Follows the existing router auto-registration contract.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from bgate_core import queue as _queue
from bgate_core import seats as _seats
from bgate_ui import dispatch as _dispatch
from bgate_ui.deps import root

router = APIRouter()


def _delegate_brief(item: dict) -> str:
    """The brief handed to the spawned director agent. It instructs a genuine
    review-and-split: analyze the source item, decide one-task vs. multi-seat,
    and delegate each piece via queue_add, then summarize with queue_complete."""
    seats = ", ".join(_seats.DEFAULT_SEATS)
    return (
        "You are acting as the DIRECTOR orchestrating this game project. Your job "
        "for THIS work item is delegation, not implementation — do not write game "
        "code or assets yourself.\n\n"
        f"SOURCE WORK ITEM #{item['id']} (seat: {item['seat']}, source: "
        f"{item['source']}):\n"
        f"  title: {item['title']}\n"
        f"  brief: {item['brief'] or '(no brief)'}\n\n"
        "Do this, in order:\n"
        f"1. Re-read the source item above. If you need more, call queue_list to "
        "see the rest of the board and avoid duplicating existing work.\n"
        "2. Analyze the ask. Decide: is this a single coherent task for one seat, "
        "or should it be split into pieces across seats?\n"
        f"   Available seats: {seats}.\n"
        "3. For EACH piece you decide on, call queue_add(seat=<seat>, "
        "title=<short imperative title>, brief=<a concrete, self-contained brief "
        "the working agent can act on without more context>). Keep titles crisp; "
        "put the detail in the brief. Do not create fragment tasks — each queued "
        "item must be a real unit of work.\n"
        "4. When every piece is delegated, call queue_complete for THIS item "
        f"(work item id={item['id']} is the delegation task you were dispatched "
        "on — resolve it) with a one-paragraph summary naming exactly which seats "
        "you delegated to and why you split it that way (or why you kept it whole).\n"
    )


@router.post("/api/orchestrator/delegate")
def delegate(payload: dict) -> dict:
    """Review a queued item for delegation: spawn a director agent that splits it
    across seats. Returns the new delegate item id + the spawned agent's pid."""
    r = root()
    try:
        item_id = int(payload.get("item_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "item_id (int) is required")

    try:
        item = _queue.get(r, item_id)
    except LookupError:
        raise HTTPException(404, f"no work item {item_id}")

    title = f"Delegate: {(item.get('title') or '')[:60]}"
    try:
        created = _queue.add(
            r, "director", title=title, brief=_delegate_brief(item),
            source="delegate", source_ref=str(item_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    new_id = int(created["id"])
    result = _dispatch.dispatch(str(r), new_id)
    if not result.get("ok"):
        # The delegate item exists but couldn't be dispatched (e.g. claude CLI
        # missing). Surface the reason without a 500 — the item is still on the
        # board and can be dispatched manually later.
        return {"ok": False, "delegate_item_id": new_id,
                "error": result.get("error", "dispatch failed")}
    return {"ok": True, "delegate_item_id": new_id, "pid": result.get("pid"),
            "source_item_id": item_id}


@router.get("/api/orchestrator/overview")
def overview() -> dict:
    """One-shot control-tower state: the queue grouped by seat + the live agent
    table. Lets the board paint (and refresh) in a single request."""
    r = root()
    grouped: dict[str, list[dict]] = {s: [] for s in _seats.DEFAULT_SEATS}
    for it in _queue.list_items(r):
        grouped.setdefault(it["seat"], []).append(it)
    return {"queue": grouped, "agents": _dispatch.status(str(r))}
