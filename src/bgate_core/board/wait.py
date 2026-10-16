"""Wait on the board instead of reading it one event at a time.

MEASURED (GNOMEFALL, 2026-10-16): a director session that followed the board
through a per-event monitor woke ~25 times an hour, and most wakes were
"#NNN qa DONE" lines that changed nothing it had to do. The human noticed the
usage before the director did. What the director actually needs is one wake
when (a) something is waiting on it, (b) the board is empty, or (c) a named
item has finished - and silence otherwise.

`wait` blocks for up to `timeout_s`, polling the project database (never the
dashboard), and returns ONE dict saying why it returned and what changed
since it started. It reads only: nothing here dispatches, claims or edits.

Conditions (`until`):

  attention  something needs the director: a director-seat item, an
             ask-human / human-task / escalation / stage-advance item, a
             failed or parked or review item, or an open director question
             (steerbox). New ones only - whatever was already waiting when the
             call began does not re-fire it, so a director that chose to leave
             a question open can still wait on the rest of the board.
  clear      nothing open at all (no queued/dispatched/held/review/parked).
  change     any item reached a terminal state since the call began, or
             anything that satisfies `attention`.
  items      every id in `item_ids` is terminal (done/failed/cancelled/parked).
  quiet      no agent is running and nothing is dispatched - the board has
             stopped moving, whether or not it is empty (a dead dashboard
             looks like this too, which is the point of asking).

`clear` always ends a wait, whatever `until` says: there is nothing left to
wait for.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Iterable, Optional

from bgate_core.store import db

OPEN = ("queued", "dispatched", "held", "review", "parked")
TERMINAL = ("done", "failed", "cancelled", "parked")
ATTENTION_STATUS = ("failed", "parked", "review")
ATTENTION_SOURCES = ("ask-human", "escalation", "human-task", "stage-advance",
                     "slice-check", "qa-gate-escalation", "failure-escalation",
                     "unblock", "decompose", "chat")
UNTIL = ("attention", "clear", "change", "items", "quiet")

MAX_TIMEOUT_S = 3600.0
MIN_POLL_S = 2.0


def _rows(root) -> list[dict]:
    conn = db.connect(root)
    return [dict(r) for r in conn.execute(
        "SELECT id, seat, status, source, title FROM work_item ORDER BY id")]


def _running_items(root) -> list[int]:
    try:
        from bgate_core.board import agentreg
        return sorted({int(r.get("item_id") or 0) for r in agentreg.open_runs(root)
                       if r.get("item_id")})
    except Exception:                                             # noqa: BLE001
        return []


def _director_questions(root) -> list[dict]:
    try:
        from bgate_core.board import steerbox
        return [{"id": q.get("id"), "item_id": q.get("item_id"),
                 "text": str(q.get("text") or q.get("question") or "")[:160]}
                for q in steerbox.questions_for_director(root)]
    except Exception:                                             # noqa: BLE001
        return []


def _stage(root) -> str:
    try:
        from bgate_core.design import greenlight
        return str(greenlight.stage(root))
    except Exception:                                             # noqa: BLE001
        return ""


def _needs_director(row: dict) -> bool:
    return (row["seat"] == "director"
            or row["status"] in ATTENTION_STATUS
            or (row.get("source") or "") in ATTENTION_SOURCES)


def snapshot(root: str | os.PathLike[str]) -> dict:
    """The board as one dict: counts, open items, what needs the director, who
    is running, the stage. Cheap (one query plus the agent registry)."""
    rows = _rows(root)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    open_rows = [r for r in rows if r["status"] in OPEN]
    # A failed item is terminal for the queue and still the director's problem.
    attention = [{"id": r["id"], "seat": r["seat"], "status": r["status"],
                  "source": r.get("source") or "", "title": r["title"][:90]}
                 for r in rows if (r["status"] in OPEN or r["status"] == "failed")
                 and _needs_director(r)]
    return {
        "counts": counts,
        "open": [r["id"] for r in open_rows],
        "terminal": {r["id"]: r["status"] for r in rows if r["status"] in TERMINAL},
        "attention": attention,
        "questions": _director_questions(root),
        "running": _running_items(root),
        "dispatched": [r["id"] for r in rows if r["status"] == "dispatched"],
        "stage": _stage(root),
        "titles": {r["id"]: r["title"][:90] for r in rows},
    }


def _ids(item_ids: Optional[Iterable[int] | str]) -> list[int]:
    if item_ids is None:
        return []
    if isinstance(item_ids, str):
        parts = [p.strip() for p in item_ids.replace(";", ",").split(",")]
        return [int(p.lstrip("#")) for p in parts if p]
    return [int(i) for i in item_ids]


def evaluate(start: dict, now: dict, until: str, item_ids: list[int]) -> Optional[dict]:
    """Why a wait that began at `start` and sees `now` should return, or None.

    Pure: the loop in `wait` and the tests both call this.
    """
    newly_terminal = {i: s for i, s in now["terminal"].items()
                      if start["terminal"].get(i) != s}
    seen_attention = {a["id"] for a in start["attention"]}
    seen_questions = {q.get("id") for q in start["questions"]}
    new_attention = [a for a in now["attention"] if a["id"] not in seen_attention]
    new_questions = [q for q in now["questions"] if q.get("id") not in seen_questions]
    since = {
        "done": sorted(i for i, s in newly_terminal.items() if s == "done"),
        "failed": sorted(i for i, s in newly_terminal.items() if s == "failed"),
        "cancelled": sorted(i for i, s in newly_terminal.items() if s == "cancelled"),
        "parked": sorted(i for i, s in newly_terminal.items() if s == "parked"),
        "attention": new_attention,
        "questions": new_questions,
    }

    def out(reason: str) -> dict:
        return {"reason": reason, "since": since}

    if not now["open"]:
        return out("clear")
    if until == "items":
        if item_ids and all(i in now["terminal"] for i in item_ids):
            return out("items")
        return None
    if until == "quiet":
        if not now["running"] and not now["dispatched"]:
            return out("quiet")
        return None
    if new_attention or new_questions:
        if until in ("attention", "change"):
            return out("attention")
    if until == "change" and newly_terminal:
        return out("change")
    return None


def wait(root: str | os.PathLike[str], *, until: str = "attention",
         item_ids: Optional[Iterable[int] | str] = None,
         timeout_s: float = 600.0, poll_s: float = 15.0,
         clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> dict:
    """Block until `until` holds (or the board is clear), or `timeout_s` passes.

    Returns {reason, waited_s, since, snapshot}. `reason` is one of the
    conditions, or "timeout". `since` lists what finished and what started
    needing the director while you waited, so the caller need not diff anything.
    """
    if until not in UNTIL:
        raise ValueError(f"until must be one of {UNTIL}, not {until!r}")
    ids = _ids(item_ids)
    if until == "items" and not ids:
        raise ValueError("until='items' needs item_ids")
    timeout_s = max(0.0, min(float(timeout_s), MAX_TIMEOUT_S))
    poll_s = max(MIN_POLL_S, float(poll_s))
    t0 = clock()
    start = snapshot(root)
    now = start
    while True:
        hit = evaluate(start, now, until, ids)
        waited = clock() - t0
        if hit is not None:
            return {**hit, "waited_s": round(waited, 1), "snapshot": _public(now)}
        if waited >= timeout_s:
            return {"reason": "timeout", "waited_s": round(waited, 1),
                    "since": evaluate_since(start, now), "snapshot": _public(now)}
        sleep(min(poll_s, max(0.0, timeout_s - waited)))
        now = snapshot(root)


def evaluate_since(start: dict, now: dict) -> dict:
    """The `since` block alone, for a timeout return."""
    probe = evaluate(start, {**now, "open": []}, "clear", [])
    return probe["since"] if probe else {}


def _public(snap: dict) -> dict:
    """The snapshot without the per-id tables a caller does not need."""
    return {k: v for k, v in snap.items() if k not in ("terminal", "titles")}
