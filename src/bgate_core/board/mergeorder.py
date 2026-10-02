"""The recommended MERGE ORDER: which open work should land first, and why.

The board answers "what can run" (queue.ready) and "what waits on what"
(the dependency graph). Neither answers the question an agent mid-run, or a
human scanning the board, actually needs: what has to be FINISHED first so
everything behind it can move. Ids are filing order and priority is a
preference; neither is that order.

The order is computed, never stored:

  * WAVES are topological levels over the OPEN work only - a finished parent
    is satisfied, so wave 0 is everything that can land now (running, ready,
    in review, waiting on a human). Wave 1 lands after wave 0, and so on.
  * Within a wave, work lands first when it is on the CRITICAL PATH (the
    longest chain of open work still behind it), then when it UNBLOCKS more
    open work, then by priority, then by filing order.
  * HUMAN steps (checkpoints waiting for sign-off, human tasks) are ranked
    like any other step - they are on the path - and flagged, because they
    are the steps no agent can finish.

``rank`` is the position in that order; ``weight`` (critical path, unblocks)
is what queue.ready() sorts ready work by, so dispatch follows the same
order the board recommends.
"""
from __future__ import annotations

import os
from typing import Optional

from ..store import db
from ..store.util import rows

OPEN = ("queued", "dispatched", "review", "parked", "integrating")
HUMAN_SOURCE = "human-task"


def _graph(root: str | os.PathLike[str]):
    conn = db.connect(root)
    items = {int(r["id"]): dict(r) for r in rows(conn.execute(
        "SELECT id, seat, title, status, priority, source, depends_on, "
        "checkpoint, attempts, created_at FROM work_item WHERE status IN "
        "('queued','dispatched','review','parked','integrating')"))}
    ups: dict[int, set[int]] = {i: set() for i in items}
    for i, it in items.items():
        if it.get("depends_on") and int(it["depends_on"]) in items:
            ups[i].add(int(it["depends_on"]))
    try:
        for r in conn.execute(
                "SELECT item_id, depends_on FROM work_item_dep "
                "WHERE cut_at IS NULL OR cut_at = ''"):
            a, b = int(r["item_id"]), int(r["depends_on"])
            if a in items and b in items:
                ups[a].add(b)
    except Exception:                                             # noqa: BLE001
        pass
    downs: dict[int, set[int]] = {i: set() for i in items}
    for c, ps in ups.items():
        for p in ps:
            downs[p].add(c)
    return items, ups, downs


def weights(root: str | os.PathLike[str]) -> dict[int, tuple[int, int]]:
    """item id -> (critical path length behind it, open items it unblocks).

    Both count OPEN work only. Cheap enough to run on every dispatch pick.
    """
    try:
        items, ups, downs = _graph(root)
    except Exception:                                             # noqa: BLE001
        return {}
    path: dict[int, int] = {}

    def longest(i: int, guard: int = 0) -> int:
        if i in path:
            return path[i]
        if guard > 200:
            return 0
        path[i] = 0                       # cycle guard
        best = 1 + max((longest(c, guard + 1) for c in downs[i]), default=0)
        path[i] = best
        return best

    out: dict[int, tuple[int, int]] = {}
    for i in items:
        seen: set[int] = set()
        todo = list(downs[i])
        while todo:
            c = todo.pop()
            if c in seen:
                continue
            seen.add(c)
            todo.extend(downs[c])
        out[i] = (longest(i) - 1, len(seen))
    return out


def order(root: str | os.PathLike[str], seat: str = "") -> dict:
    """The recommended order, wave by wave, with the reason for each place."""
    items, ups, downs = _graph(root)
    w = weights(root)
    wave: dict[int, int] = {}

    def wave_of(i: int, guard: int = 0) -> int:
        if i in wave:
            return wave[i]
        if guard > 200:
            return 0
        wave[i] = 0
        v = 1 + max((wave_of(p, guard + 1) for p in ups[i]), default=-1)
        wave[i] = v
        return v

    for i in items:
        wave_of(i)
    top = max((w[i][0] for i in items), default=0)
    critical = {i for i in items if w[i][0] == top and top > 0}
    # Walk the critical chain down from its heads: the members are the items
    # whose path length drops by exactly one along a child.
    chain: set[int] = set()
    for head in [i for i in critical if not (ups[i] & critical)]:
        cur: Optional[int] = head
        while cur is not None:
            chain.add(cur)
            nxt = [c for c in downs[cur] if w[c][0] == w[cur][0] - 1]
            cur = min(nxt) if nxt else None

    def human(it: dict) -> bool:
        return it.get("source") == HUMAN_SOURCE or (
            it.get("status") == "review" and int(it.get("checkpoint") or 0) == 1)

    ranked = sorted(items, key=lambda i: (
        wave[i], -w[i][0], -w[i][1], -int(items[i].get("priority") or 0), i))
    out = []
    for n, i in enumerate(ranked, 1):
        it = items[i]
        path, unblocks = w[i]
        why = []
        if i in chain:
            why.append("on the critical path")
        if unblocks:
            why.append(f"unblocks {unblocks}")
        if ups[i]:
            why.append("lands after " + ", ".join(f"#{p}" for p in sorted(ups[i])[:4]))
        state = ("running" if it["status"] == "dispatched"
                 else "needs you" if human(it)
                 else "parked" if it["status"] == "parked"
                 else "waiting" if ups[i] else it["status"])
        out.append({
            "rank": n, "wave": wave[i], "id": i, "title": str(it["title"])[:140],
            "seat": it["seat"], "state": state, "human": human(it),
            "critical": i in chain, "path": path, "unblocks": unblocks,
            "after": sorted(ups[i]), "before": sorted(downs[i]),
            "runs": int(it.get("attempts") or 0) + 1,
            "why": " · ".join(why) or "independent",
        })
    mine = [e for e in out if e["seat"] == seat] if seat else out
    return {
        "order": mine if seat else out,
        "waves": max(wave.values(), default=-1) + 1,
        "critical_path": sorted(chain, key=lambda i: wave[i]),
        "human_steps": [e["id"] for e in out if e["human"]],
        "note": ("land work in this order: everything in wave N before wave "
                 "N+1, and within a wave the critical path first, then what "
                 "unblocks the most. Human steps are on the path like any "
                 "other step."),
    }


def brief_block(root: str | os.PathLike[str], seat: str) -> dict:
    """This seat's place in the merge order, for its brief. Never raises."""
    try:
        got = order(root)
    except Exception:                                             # noqa: BLE001
        return {}
    full = got["order"]
    mine = [e for e in full if e["seat"] == seat][:8]
    if not full:
        return {}
    return {
        "first_on_the_board": [{k: e[k] for k in ("rank", "id", "title", "seat", "why")}
                               for e in full[:5]],
        "yours": [{k: e[k] for k in ("rank", "wave", "id", "title", "state", "why")}
                  for e in mine],
        "waiting_on_human": [e["id"] for e in full if e["human"]][:6],
        "rule": ("FINISH IN MERGE ORDER. If your item is behind a lower-ranked "
                 "one you could unblock, say so in your result; never land work "
                 "that a higher-ranked open item will rewrite."),
    }
