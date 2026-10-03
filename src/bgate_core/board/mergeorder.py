"""The recommended MERGE ORDER: which open work should land first, and why.

The board answers "what can run" (queue.ready) and "what waits on what"
(the dependency graph). Neither answers the question an agent mid-run, or a
human scanning the board, actually needs: what has to be FINISHED first so
everything behind it can move. Ids are filing order and priority is a
preference; neither is that order.

The order is computed, never stored, GOAL BY GOAL:

  * a GOAL is open work nothing else waits on. Each goal is emitted with its
    whole open upstream in post-order - what it waits on first (deepest
    chain first), then the goal - so the work feeding a target sits directly
    above it and a reader never chases a line off the screen;
  * the goal on the CRITICAL PATH (the longest chain of open work) goes
    first, then goals with the most work behind them; goals with nothing
    behind them are the independent tail;
  * WAVES (topological levels over open work; a finished parent is
    satisfied) are reported per item: wave 0 can land now;
  * HUMAN steps (checkpoints waiting for sign-off, human tasks) are ranked
    like any other step - they are on the path - and flagged.

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
    """The recommended order, goal by goal, with the reason for each place."""
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

    # GOAL BY GOAL. A goal is open work nothing else waits on (a sink). Each
    # goal's whole upstream is emitted in post-order - what it waits on first,
    # deepest chain first - and then the goal itself, so the work feeding a
    # target sits directly above it. Dependencies are still respected (every
    # item comes after everything it waits on); work shared by two goals lands
    # with the first goal that needs it. The goal on the critical path goes
    # first, then the goals with the most work behind them; goals with nothing
    # behind them are the independent tail.
    def upstream(i: int) -> set[int]:
        seen: set[int] = set()
        todo = list(ups[i])
        while todo:
            c = todo.pop()
            if c not in seen:
                seen.add(c)
                todo.extend(ups[c])
        return seen

    sinks = [i for i in items if not downs[i]]
    size = {i: len(upstream(i)) for i in sinks}
    sinks.sort(key=lambda i: (0 if i in chain else 1, 0 if size[i] else 1,
                              -wave[i], -size[i], i))
    ranked: list[int] = []
    goal_of: dict[int, int] = {}
    emitted: set[int] = set()

    def visit(i: int, goal: int, guard: int = 0) -> None:
        if i in emitted or guard > 400:
            return
        emitted.add(i)                    # cycle guard; position set below
        for p in sorted(ups[i], key=lambda p: (-w[p][0], -w[p][1], p)):
            visit(p, goal, guard + 1)
        ranked.append(i)
        goal_of[i] = goal

    for g in sinks:
        visit(g, g)
    for i in sorted(items):               # anything a cycle hid from the sinks
        visit(i, i)
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
            "rank": n, "wave": wave[i], "goal": goal_of[i],
            "goal_title": str(items[goal_of[i]]["title"])[:140],
            "solo": not ups[goal_of[i]] and goal_of[i] == i,
            "id": i, "title": str(it["title"])[:140],
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
        "note": ("land work in this order, goal by goal: the goal on the "
                 "critical path first, and inside each goal everything it "
                 "waits on before the goal itself. `wave` says how many "
                 "steps of open work stand in front of an item (0 = it can "
                 "land now). Human steps are in the order like any other."),
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


def history(root: str | os.PathLike[str], hours: float = 0, seat: str = "",
            limit: int = 500) -> dict:
    """What already LANDED, in the order it landed, with its commits.

    The order view showed only open work, so a board that had finished sixty
    tickets read as three running rows. This is the rest of the log: every
    finished ticket in the window, oldest first so it flows into the open
    order below it, each with the harness commits that carried it (one
    ``git log`` for all of them) and its dependencies on other landed work.
    ``hours`` 0 means all of it."""
    import re
    from . import gitwork as _git

    conn = db.connect(root)
    done = {int(r["id"]): dict(r) for r in rows(conn.execute(
        "SELECT id, seat, title, status, source, depends_on, attempts, updated_at "
        "FROM work_item WHERE status = 'done'"))}
    commits: dict[int, list[dict]] = {}
    ok, out, _ = _git._run(root, ["log", "--reverse", "--grep=^bgate: item",
                                  "--shortstat", "--format=\x1e%H\x1f%cI\x1f%s"],
                           timeout=30)
    if ok:
        for chunk in out.split("\x1e"):
            lines = [x for x in chunk.strip("\n").splitlines() if x.strip()]
            if not lines or "\x1f" not in lines[0]:
                continue
            sha, at, subject = lines[0].split("\x1f", 2)
            head = subject.split("[", 1)[0]
            files = re.search(r"(\d+) files? changed", lines[1]) if len(lines) > 1 else None
            for n in re.findall(r"#(\d+)", head):
                commits.setdefault(int(n), []).append(
                    {"sha": sha[:8], "at": at, "files": int(files.group(1)) if files else 0})
    ups: dict[int, set[int]] = {i: set() for i in done}
    for i, it in done.items():
        if it.get("depends_on") and int(it["depends_on"]) in done:
            ups[i].add(int(it["depends_on"]))
    try:
        for r in conn.execute("SELECT item_id, depends_on FROM work_item_dep "
                              "WHERE cut_at IS NULL OR cut_at = ''"):
            a, b = int(r["item_id"]), int(r["depends_on"])
            if a in done and b in done:
                ups[a].add(b)
    except Exception:                                             # noqa: BLE001
        pass

    def landed_at(i: int) -> str:
        made = commits.get(i)
        if made:
            return made[0]["at"]
        return str(done[i].get("updated_at") or "").replace(" ", "T") + "Z"

    from datetime import datetime, timedelta, timezone

    def parse(s: str):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=float(hours))) if hours else None
    picked = []
    for i in done:
        t = parse(landed_at(i))
        if cutoff and t and t < cutoff:
            continue
        if seat and done[i]["seat"] != seat:
            continue
        picked.append((t or datetime.min.replace(tzinfo=timezone.utc), i))
    picked.sort()
    picked = picked[-int(limit):]
    keep = {i for _, i in picked}
    out_rows = []
    for t, i in picked:
        it = done[i]
        out_rows.append({
            "id": i, "title": str(it["title"])[:140], "seat": it["seat"],
            "landed_at": landed_at(i), "commits": commits.get(i, []),
            "after": sorted(p for p in ups[i] if p in keep),
            "runs": int(it.get("attempts") or 0) + 1,
            "human": it.get("source") == HUMAN_SOURCE,
            "qa": str(it["title"]).startswith("QA gate"),
        })
    return {"landed": out_rows, "count": len(out_rows)}
