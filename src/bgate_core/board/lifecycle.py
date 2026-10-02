"""The board as a source-control history: every ticket a commit, every chain a
branch, every dependency a merge line.

The orchestration page drew a delegation TREE (who asked for what). What it
could not show was the thing an operator actually reasons about: which work is
a line (a chain), which runs beside it (independent work), where lines fork
and join (multi-parent dependencies), what is running right now, and what
landed to get here. That is a git graph, and this module lays one out:

  * rows are TOPOLOGICAL then chronological - a ticket is always drawn after
    everything it depends on, ties broken by when it was filed - and the
    client draws them newest first, so open work sits on top like a working
    tree over its history;
  * lanes are assigned the way a git graph assigns them: a ticket continues
    the lane of the parent it is the first child of, any other child of that
    parent branches into the lowest free lane, and a lane is released when
    its last ticket has no visible child. Independent tickets therefore reuse
    a handful of lanes instead of each taking a column;
  * every node carries its RUNS (agent_runs: start, end, status), so a ticket
    that took four runs looks like one, and a running ticket carries its
    elapsed time.

Read-only and never raises on a single bad row: the graph is a reading of the
board, and a reading that crashes takes the page with it.
"""
from __future__ import annotations

import os
import time
from typing import Optional
from ..store import db
from ..store.util import rows

OPEN = ("queued", "dispatched", "review", "parked")


def _runs(root, ids: list[int]) -> dict[int, list[dict]]:
    if not ids:
        return {}
    out: dict[int, list[dict]] = {}
    try:
        marks = ",".join("?" * len(ids))
        for r in rows(db.connect(root).execute(
                f"SELECT item_id, started_at, ended_at, status, seat, runner "
                f"FROM agent_runs WHERE item_id IN ({marks}) "
                f"ORDER BY started_at", ids)):
            out.setdefault(int(r["item_id"]), []).append({
                "started": r["started_at"], "ended": r["ended_at"],
                "status": r["status"], "runner": r["runner"]})
    except Exception:                                             # noqa: BLE001
        return {}
    return out


def _hold_reason(root, item: dict, held_seats: set, unplanned: set) -> str:
    from . import queue as _queue
    if item.get("status") != "queued":
        return ""
    seat = str(item.get("seat") or "")
    if seat in held_seats:
        try:
            from ..design import greenlight as _gl
            return _gl.allows(root, seat)[1]
        except Exception:                                         # noqa: BLE001
            return "held by the production stage"
    if seat in unplanned and item.get("source") != "domain-plan":
        return f"held: the {seat} discipline has no domain plan yet"
    try:
        return _queue.slice_hold_reason(root, item)
    except Exception:                                             # noqa: BLE001
        return ""


SPAWN_SOURCES = ("qa-gate", "qa-gate-escalation", "decompose", "delegate",
                 "failure-escalation", "slice-check")


def _spawned_by(item: dict, items: dict) -> Optional[int]:
    """The ticket that SPAWNED this one (a gate on the item it verifies, a
    split of the ticket it replaces) - a softer line than a dependency."""
    ref = str(item.get("source_ref") or "")
    if item.get("source") in SPAWN_SOURCES and ref.isdigit() and int(ref) in items:
        return int(ref)
    if item.get("split_of") and int(item["split_of"]) in items:
        return int(item["split_of"])
    return None


def graph(root: str | os.PathLike[str], *, hours: float = 48,
          limit: int = 300) -> dict:
    """Nodes with lanes and rows, edges, and a summary. See the module doc.

    ``hours`` bounds the HISTORY (finished tickets updated within the window);
    open tickets are always included. 0 means all history up to ``limit``.
    """
    from . import queue as _queue

    conn = db.connect(root)
    window = "" if not hours else f"-{float(hours)} hours"
    sql = ("SELECT * FROM work_item WHERE status IN ('queued','dispatched',"
           "'review','parked')")
    params: list = []
    if window:
        sql += " OR updated_at >= datetime('now', ?)"
        params.append(window)
    else:
        sql += " OR 1=1"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    items = {int(r["id"]): dict(r) for r in rows(conn.execute(sql, params))}

    parents_of: dict[int, list[int]] = {}
    hidden_parents: dict[int, int] = {}
    for i in items:
        try:
            ups = [int(p) for p in _queue.parents(root, i)]
        except Exception:                                         # noqa: BLE001
            ups = []
        parents_of[i] = [p for p in ups if p in items]
        hidden_parents[i] = len(ups) - len(parents_of[i])
    children_of: dict[int, list[int]] = {i: [] for i in items}
    for c, ups in parents_of.items():
        for p in ups:
            children_of[p].append(c)

    # Topological, ties by filing order (id is filing order).
    pending = {i: len(parents_of[i]) for i in items}
    frontier = sorted(i for i, n in pending.items() if n == 0)
    order: list[int] = []
    while frontier:
        node = frontier.pop(0)
        order.append(node)
        for c in sorted(children_of[node]):
            pending[c] -= 1
            if pending[c] == 0:
                frontier.append(c)
        frontier.sort()
    order += sorted(i for i in items if i not in order)        # cycles, last
    row_of = {i: n for n, i in enumerate(order)}
    for p in children_of:
        children_of[p].sort(key=lambda c: row_of[c])

    # Lanes, git-style.
    lane_of: dict[int, int] = {}
    reserved: dict[int, int] = {}          # parent id -> lane kept for its 1st child
    busy: set[int] = set()

    def free_lane() -> int:
        n = 0
        while n in busy:
            n += 1
        return n

    for i in order:
        lane = None
        for p in parents_of[i]:
            if p in reserved and children_of[p] and children_of[p][0] == i:
                lane = reserved.pop(p)
                break
        if lane is None:
            lane = free_lane()
            busy.add(lane)
        lane_of[i] = lane
        if children_of[i]:
            reserved[i] = lane             # the lane runs on to the first child
        else:
            busy.discard(lane)

    try:
        from ..design import greenlight as _gl
        held_seats = set(_gl.held_seats(root))
    except Exception:                                             # noqa: BLE001
        held_seats = set()
    try:
        from ..design import domainplan as _dp
        unplanned = _dp.planning_held(root)
    except Exception:                                             # noqa: BLE001
        unplanned = set()
    runs = _runs(root, list(items))
    now = time.time()

    nodes = []
    counts: dict[str, int] = {}
    for i in order:
        it = items[i]
        status = str(it.get("status") or "")
        unresolved = [p for p in parents_of[i]
                      if str(items[p].get("status")) != "done"]
        hold = _hold_reason(root, it, held_seats, unplanned)
        if status == "dispatched":
            state = "running"
        elif status == "review":
            state = "checkpoint" if int(it.get("checkpoint") or 0) else "review"
        elif status in ("done", "failed", "cancelled", "parked"):
            state = status
        elif hold:
            state = "held"
        elif any(str(items[p].get("status")) in ("failed", "cancelled")
                 for p in unresolved):
            state = "blocked"
        elif unresolved or (hidden_parents[i] and _queue.blocker(root, i)):
            state = "waiting"
        else:
            state = "ready"
        counts[state] = counts.get(state, 0) + 1
        its_runs = runs.get(i, [])
        open_run = next((r for r in reversed(its_runs) if not r["ended"]), None)
        nodes.append({
            "id": i, "title": str(it.get("title") or "")[:140],
            "seat": it.get("seat") or "", "status": status, "state": state,
            "lane": lane_of[i], "row": row_of[i],
            "parents": parents_of[i], "hidden_parents": hidden_parents[i],
            "chain_id": it.get("chain_id") or "",
            "kind": it.get("kind") or "", "severity": it.get("severity") or "",
            "size": it.get("size") or "",
            "checkpoint": bool(int(it.get("checkpoint") or 0)),
            "checkpoint_note": it.get("checkpoint_note") or "",
            "split_of": it.get("split_of"),
            "spawned_by": _spawned_by(it, items),
            "source": it.get("source") or "",
            "runs": _queue.runs_of(it),
            "run_log": its_runs[-8:],
            "elapsed_s": int(now - open_run["started"]) if open_run else None,
            "created_at": it.get("created_at"), "updated_at": it.get("updated_at"),
            "acceptance": str(it.get("acceptance") or "")[:240],
            "hold": hold,
            "waiting_on": unresolved,
            "result": str(it.get("result") or "")[-280:],
        })
    edges = [{"from": p, "to": c} for c, ups in parents_of.items() for p in ups]
    return {
        "nodes": nodes,
        "edges": edges,
        "lanes": (max(lane_of.values()) + 1) if lane_of else 0,
        "counts": counts,
        "window_hours": hours,
        "checkpoints_waiting": [n["id"] for n in nodes if n["state"] == "checkpoint"],
        "multi_run": [n["id"] for n in nodes if n["runs"] >= 2
                      and n["status"] in OPEN + ("failed",)],
    }


def checkpoint_detail(root: str | os.PathLike[str], item_id: int) -> dict:
    """What signing this checkpoint covers."""
    from . import queue as _queue
    item = _queue.get(root, int(item_id))
    return {"item": {k: item.get(k) for k in ("id", "title", "seat", "status",
                                              "checkpoint_note", "result")},
            "covers": _queue.checkpoint_digest(root, int(item_id))}
