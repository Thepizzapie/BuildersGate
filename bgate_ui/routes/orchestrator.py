"""Orchestrator / control-tower endpoints — the DIRECTOR seat's cockpit.

The director seat manages many agents at once. Most of what it needs already
exists (/api/queue, /api/agents, /api/agent-activity); this module adds the
two orchestration-specific endpoints:

  * POST /api/orchestrator/delegate — spawn a director agent that reads a queued
    item, decides whether it's one task or a split across seats, and delegates
    the pieces with queue_add before completing.
  * GET  /api/orchestrator/overview — one call: the queue grouped by seat plus
    the live agent table, so the board can paint in a single round-trip. PAGED
    and brief-free: it is polled every 3 seconds, and shipping every item ever
    created (briefs and all) made the board slower the longer a project lived.
  * GET  /api/orchestrator/lineage/{item_id} — who delegated this, and what came
    out of it. Persisted, not remembered: the parent/child relation used to live
    only in the browser and evaporated on reload.

Never raises through to the client with a bare stack: LookupError -> 404, bad
input -> 400. Follows the existing router auto-registration contract.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from bgate_core import db
from bgate_core import queue as _queue
from bgate_core import seats as _seats
from bgate_ui import api as _api
from bgate_ui import dispatch as _dispatch
from bgate_ui.deps import root

router = APIRouter()

# Lineage marker. work_item.source/source_ref carries the delegate -> source
# link (the workflow-run pattern: source='delegate', source_ref='<item id>'),
# but the pieces the director queues are created by the AGENT through
# queue_add, which stamps only source='seat:director'. So the delegation id is
# written into the first line of each child's brief — durable, greppable, and
# useful to the working agent anyway ("where did this task come from").
DELEGATED_FROM = "DELEGATED-FROM: #"
_DELEGATED_RE = re.compile(re.escape(DELEGATED_FROM) + r"(\d+)")

# Fields the board actually paints. `brief` is deliberately absent — it is the
# largest column in the table and the board renders a card, not a document.
_CARD_FIELDS = ("id", "seat", "title", "status", "priority", "source",
                "source_ref", "attempts", "total_cost_usd",
                "created_at", "updated_at")

_BOARD_ORDER = ("ORDER BY CASE status WHEN 'queued' THEN 0 "
                "WHEN 'dispatched' THEN 1 WHEN 'cancelled' THEN 3 ELSE 2 END, "
                "priority DESC, id")


def _card(row) -> dict:
    item = {k: row[k] for k in _CARD_FIELDS}
    brief = row["brief"] or ""
    item["brief_preview"] = brief[:200]
    item["brief_len"] = len(brief)
    return item


def _delegate_brief(item: dict, delegate_id: int) -> str:
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
        f"   EVERY brief you write MUST START with this exact line, verbatim:\n"
        f"     {DELEGATED_FROM}{delegate_id} (source #{item['id']})\n"
        "   That line is the only durable record of what came from this "
        "delegation — the board reads it to rebuild the tree after a reload. "
        "Write it first, then a blank line, then the real brief.\n"
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
            r, "director", title=title, brief="(preparing delegation brief)",
            source="delegate", source_ref=str(item_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # The brief names the delegate item's own id (children stamp it into their
    # briefs), so it can only be written once the row exists.
    new_id = int(created["id"])
    _queue.update(r, new_id, brief=_delegate_brief(item, new_id))
    result = _dispatch.dispatch(str(r), new_id)
    if not result.get("ok"):
        # The delegate item exists but couldn't be dispatched (e.g. claude CLI
        # missing). Surface the reason without a 500 — the item is still on the
        # board and can be dispatched manually later.
        return {"ok": False, "delegate_item_id": new_id,
                "error": result.get("error", "dispatch failed")}
    return {"ok": True, "delegate_item_id": new_id, "pid": result.get("pid"),
            "source_item_id": item_id}


def _lineage(root_dir) -> dict:
    """parent -> children and child -> parent, read off persisted columns.

    Two sources, both durable: ``source``/``source_ref`` for rows this server
    created (delegate, qa-gate, escalation), and the DELEGATED-FROM line for
    rows a director agent queued through queue_add. The query is narrow — only
    rows that can carry lineage — so this stays cheap on a big board.
    """
    conn = db.connect(root_dir)
    parents: dict[int, int] = {}
    for row in conn.execute(
            "SELECT id, source, source_ref, substr(brief, 1, 200) AS head "
            "FROM work_item WHERE source IN "
            "('delegate', 'qa-gate', 'qa-gate-escalation') "
            "OR brief LIKE ? ORDER BY id DESC LIMIT 500",
            (f"%{DELEGATED_FROM}%",)):
        child = int(row["id"])
        ref = (row["source_ref"] or "").strip()
        if row["source"] in ("delegate", "qa-gate", "qa-gate-escalation") \
                and ref.isdigit():
            parents[child] = int(ref)
            continue
        found = _DELEGATED_RE.search(row["head"] or "")
        if found:
            parents[child] = int(found.group(1))
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    for kids in children.values():
        kids.sort()
    return {"parents": parents, "children": children}


@router.get("/api/orchestrator/overview")
def overview(page: _api.Page = Depends()) -> dict:
    """One-shot control-tower state: the queue grouped by seat + the live agent
    table. Lets the board paint (and refresh) in a single request.

    Bounded on purpose. This is polled every few seconds; an unpaged board sent
    every work item ever created — brief text included — so the payload grew
    without limit while the visible board stayed the same size. Items an agent
    is currently running are always included regardless of the window: the
    agent cards index into this map and would otherwise render as '#41'.
    """
    r = root()
    conn = db.connect(r)
    total = int(conn.execute("SELECT count(*) FROM work_item").fetchone()[0])
    rows = conn.execute(
        f"SELECT * FROM work_item {_BOARD_ORDER} LIMIT ? OFFSET ?",
        (page.limit, page.offset)).fetchall()
    items = [_card(row) for row in rows]

    agents = _dispatch.status(str(r))
    seen = {it["id"] for it in items}
    missing = [int(a["item_id"]) for a in agents
               if a.get("item_id") is not None and int(a["item_id"]) not in seen]
    if missing:
        marks = ", ".join("?" * len(missing))
        items += [_card(row) for row in conn.execute(
            f"SELECT * FROM work_item WHERE id IN ({marks})", missing)]

    grouped: dict[str, list[dict]] = {s: [] for s in _seats.DEFAULT_SEATS}
    for item in items:
        grouped.setdefault(item["seat"], []).append(item)
    by_seat = {row["seat"]: int(row["n"]) for row in conn.execute(
        "SELECT seat, count(*) AS n FROM work_item GROUP BY seat")}

    nxt = page.offset + len(rows)
    return {
        "queue": grouped,
        "agents": agents,
        "lineage": _lineage(r),
        "totals": {"items": total, "by_seat": by_seat},
        "page": {"limit": page.limit, "offset": page.offset, "total": total,
                 "next_offset": nxt if nxt < total else None},
        "note": ("briefs are truncated to brief_preview — fetch the item for the "
                 "full text"),
    }


@router.get("/api/orchestrator/lineage/{item_id}")
def lineage(item_id: int) -> dict:
    """The delegation tree around one item, rebuilt from the database.

    The board used to hold 'this delegate came from that item' in a JS variable,
    so a reload orphaned every card. Everything here is persisted.
    """
    r = root()
    try:
        item = _queue.get(r, item_id)
    except LookupError:
        raise HTTPException(404, f"no work item {item_id}")
    tree = _lineage(r)
    parent_id = tree["parents"].get(item_id)
    child_ids = tree["children"].get(item_id, [])

    conn = db.connect(r)

    def _fetch(ids: list[int]) -> list[dict]:
        if not ids:
            return []
        marks = ", ".join("?" * len(ids))
        return [_card(row) for row in conn.execute(
            f"SELECT * FROM work_item WHERE id IN ({marks}) ORDER BY id", ids)]

    return {
        "item": _card(conn.execute("SELECT * FROM work_item WHERE id = ?",
                                   (item_id,)).fetchone()),
        "parent": (_fetch([parent_id]) or [None])[0],
        "children": _fetch(child_ids),
        "source": item["source"],
        "source_ref": item["source_ref"],
    }
