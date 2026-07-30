"""Time-based events — the failures that are an ABSENCE of transitions.

THE HOLE THIS FILLS. The event bus is transition-driven: something moves, an
event is written, a subscriber acts. But half of what the follow-up router was
built to fix is nothing moving at all — an item parked in ``review`` that nobody
approves, a chain whose next link never became ready because the link in front
of it is waiting on a human, a board with autopilot off and forty queued rows.
None of those write a transition, so a purely reactive router reintroduces the
exact quiet failure one layer up: the machinery is working perfectly and the
work is not moving, and nothing says so.

So there is ONE producer that emits from elapsed time rather than from change,
with two rules:

    chain.stalled   the head of a chain has not moved for notify.stall_hours
    item.aging      a 'review' item has not moved for the same window
    chain.stalled   (again, via steerbox) an ask_human question nobody answered
                    for notify.question_stale_h

Everything downstream already handles both, because they are ordinary events on
the ordinary bus — no new channel, no new subscriber, no second timer. It runs
on the follow-up router's existing tick (:func:`bgate_ui.followup.tick`) rather
than owning a thread: a fourth daemon loop with its own idea of "recent" is the
copying that made ``qa_gate``'s startup cutoff a bug nobody noticed for months.

ONCE PER SUBJECT PER STALL, RESET ON MOVEMENT. A rule that fires every tick is a
rule that gets muted, and one that fires once and never again is a rule that
misses the second stall. The mark stored per subject is the subject's own
``updated_at`` at the moment it was reported: while that value is unchanged the
subject has not moved and stays quiet, and the moment it changes the mark is
dropped, so the NEXT full window of silence reports again.

FAIL-SAFE. Emitting is best-effort (``events.emit`` never raises) and the state
doc is written unversioned: a lost mark costs a duplicate line in a drawer, and
a raise here would take the router's tick down with it.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from bgate_core import db, events as _events, settings as _settings, \
    workspace as _ws

# Where the marks live. A director doc, alongside director/gate and
# director/settings — deliberately NOT under events.CURSOR_SEAT, because that
# seat's keys are enumerated as event-bus consumers and a heartbeat is a
# producer; listing it there would show a subscriber permanently stuck at 0.
STATE_SEAT = "director"
STATE_KEY = "heartbeat"

# What a stall window falls back to when the registry will not read. Same number
# as the notify.stall_hours default, so an unreadable settings doc changes the
# timing of nothing.
DEFAULT_STALL_H = 2.0

# Ceiling on subjects examined per tick. A board with thousands of rows must not
# turn the dashboard's tick into a table scan with a Python loop on top.
MAX_SUBJECTS = 300

# A chain head with a live agent on it is not stalled — the run has its own
# watchdog (dispatch.HARD_RUNTIME_S / STALL_S) and reporting it here would
# double-report every long run as a problem.
RUNNING = "dispatched"
CLOSED = ("done", "cancelled")


def window_h(root: str | os.PathLike[str]) -> float:
    """How long silence has to last before it counts as a stall.

    Read every tick from the registry rather than captured at startup: a studio
    that turns the window down because it is watching the board now must not
    have to restart the dashboard to make that true.
    """
    try:
        return max(0.05, float(_settings.get(root, "notify.stall_hours")))
    except Exception:
        return DEFAULT_STALL_H


def _load(root) -> dict:
    """The marks doc, shaped ``{"chain": {...}, "item": {...}}``. Never raises."""
    try:
        doc = _ws.get(root, STATE_SEAT, STATE_KEY, {})
    except Exception:
        doc = {}
    chain = doc.get("chain")
    item = doc.get("item")
    return {"chain": dict(chain) if isinstance(chain, dict) else {},
            "item": dict(item) if isinstance(item, dict) else {}}


def _save(root, state: dict) -> None:
    """Store the marks. Unversioned and best-effort on purpose.

    There is one producer per project, so there is nobody to lose an update to;
    and a mark that fails to land costs one repeated notice on the next tick,
    which is cheaper than a raise inside the router's loop.
    """
    try:
        _ws.set(root, STATE_SEAT, STATE_KEY,
                {"chain": state.get("chain") or {},
                 "item": state.get("item") or {},
                 "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())},
                if_version="")
    except Exception:
        pass


def _rows(root, sql: str, params: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in db.connect(root).execute(sql, params).fetchall()]
    except Exception:
        return []      # an unreadable board emits nothing; it does not raise


# age_s is computed by SQLite, not Python: updated_at is written by
# datetime('now') at second resolution in UTC, and every attempt to reproduce
# that in local time somewhere else in this tree has been a bug.
_CHAIN_SQL = (
    "SELECT id, chain_id, chain_pos, seat, title, status, updated_at, depends_on, "
    "(strftime('%s', 'now') - strftime('%s', updated_at)) AS age_s "
    "FROM work_item WHERE chain_id <> '' ORDER BY chain_id, chain_pos, id")

_REVIEW_SQL = (
    "SELECT id, chain_id, chain_pos, seat, title, status, attempts, updated_at, "
    "(strftime('%s', 'now') - strftime('%s', updated_at)) AS age_s "
    "FROM work_item WHERE status = 'review' ORDER BY updated_at, id LIMIT ?")


def _head_of(links: list[dict]) -> Optional[dict]:
    """The link a chain is currently waiting on, or None if the chain is finished.

    "Head" is the first link that is not closed — which is the only link whose
    silence means the chain is stuck. Judging the chain by its last row instead
    reports a finished chain as stalled forever.
    """
    for link in links:
        if str(link.get("status") or "") not in CLOSED:
            return link
    return None


def _reason(root, head: dict) -> tuple[str, Optional[dict]]:
    """Why the head is not moving, in words a notification can carry.

    "Stalled" with no antecedent is the least actionable thing a reminder can
    say — the fix for "waiting on you in review" and the fix for "ready and
    nobody is dispatching" are different actions by different parties.
    """
    status = str(head.get("status") or "")
    if status == "review":
        return "waiting for your approval in review", None
    if status == "failed":
        return "failed and nobody has reopened or cut it", None
    if status == "queued":
        try:
            from bgate_core import queue as _queue

            held = _queue.blocker(root, int(head["id"]))
        except Exception:
            held = None
        if held:
            return (f"blocked on #{held['id']} [{held['seat']}] which is "
                    f"{held['status']}"), held
        return ("ready to run and nothing has dispatched it — auto-deploy may "
                "be off"), None
    return f"sitting in {status or 'an unknown status'}", None


def tick(root: str | os.PathLike[str]) -> dict:
    """One pass. Emits chain.stalled / item.aging for whatever has gone quiet.

    Returns ``{"window_h": float, "stalled": [...], "aging": [...],
    "cleared": int, "reminded": int}`` — ``stalled``/``aging`` are the payloads
    that were emitted (empty on a healthy board), ``cleared`` counts marks dropped
    because the subject moved, ``reminded`` counts stale-question reminders. Safe
    to call directly, which is how it is tested and how the router drives it; it
    never raises.
    """
    try:
        return _tick(root)
    except Exception:
        # THE DOCSTRING'S PROMISE, ACTUALLY KEPT. This runs on the follow-up
        # router's thread, so an exception here does not just lose the heartbeat
        # — it aborts the rest of that tick, which is the notification path. A
        # quiet heartbeat is a missing reminder; a raising one stops the routing
        # this whole surface exists to do. `_rows` guards its own queries, but
        # everything between them (the settings read, the state doc, an emit)
        # can still throw.
        return {"window_h": DEFAULT_STALL_H, "stalled": [], "aging": [],
                "cleared": 0, "reminded": 0}


def _tick(root: str | os.PathLike[str]) -> dict:
    hours = window_h(root)
    threshold_s = int(hours * 3600)
    state = _load(root)
    marks_chain = state["chain"]
    marks_item = state["item"]
    before = len(marks_chain) + len(marks_item)

    grouped: dict[str, list[dict]] = {}
    for row in _rows(root, _CHAIN_SQL):
        grouped.setdefault(str(row.get("chain_id") or ""), []).append(row)

    stalled: list[dict] = []
    fresh_chain: dict[str, dict] = {}
    # Item ids a chain.stalled already speaks for this tick. Without this a
    # 'review' chain head produces BOTH events for one situation, which is two
    # pings for one thing to do — and two pings for one thing is how a
    # notification channel earns its mute.
    covered: set[int] = set()

    for chain_id, links in list(grouped.items())[:MAX_SUBJECTS]:
        head = _head_of(links)
        if head is None:
            continue                     # finished chain: its mark is dropped
        mark = marks_chain.get(chain_id) or {}
        moved = str(mark.get("mark") or "") != str(head.get("updated_at") or "")
        if str(head.get("status") or "") == RUNNING:
            continue                     # an agent is on it; not a stall
        if int(head.get("age_s") or 0) < threshold_s:
            continue                     # still inside the window
        covered.add(int(head["id"]))
        if not moved:
            # Already reported and nothing has changed since — stay quiet, but
            # keep the mark so it survives this tick's rewrite of the doc.
            fresh_chain[chain_id] = mark
            continue
        why, held = _reason(root, head)
        payload = {
            "chain_id": chain_id,
            "count": len(links),
            "done": sum(1 for row in links if str(row.get("status")) == "done"),
            "head": {"item": int(head["id"]), "seat": head.get("seat") or "",
                     "title": str(head.get("title") or "")[:200],
                     "status": head.get("status") or "",
                     "chain_pos": int(head.get("chain_pos") or 0)},
            "reason": why,
            "blocked_by": held,
            "idle_min": int(int(head.get("age_s") or 0) / 60),
            "window_h": hours,
        }
        _events.emit(root, "chain.stalled", ref=chain_id, payload=payload)
        stalled.append(payload)
        fresh_chain[chain_id] = {"mark": str(head.get("updated_at") or ""),
                                 "at": _stamp()}

    aging: list[dict] = []
    fresh_item: dict[str, dict] = {}
    for row in _rows(root, _REVIEW_SQL, (MAX_SUBJECTS,)):
        item_id = int(row["id"])
        if int(row.get("age_s") or 0) < threshold_s:
            continue
        key = str(item_id)
        mark = marks_item.get(key) or {}
        moved = str(mark.get("mark") or "") != str(row.get("updated_at") or "")
        if item_id in covered:
            # The chain rule already said this one out loud. Keep any existing
            # mark so it does not fire the moment the chain closes around it.
            if mark:
                fresh_item[key] = mark
            continue
        if not moved:
            fresh_item[key] = mark
            continue
        payload = {
            "item": item_id,
            "seat": row.get("seat") or "",
            "title": str(row.get("title") or "")[:200],
            "status": row.get("status") or "review",
            "chain_id": row.get("chain_id") or "",
            "chain_pos": int(row.get("chain_pos") or 0),
            "attempts": int(row.get("attempts") or 0),
            "idle_min": int(int(row.get("age_s") or 0) / 60),
            "window_h": hours,
            "reason": "finished and waiting for your approval — the work is on "
                      "disk and does not count yet",
        }
        _events.emit(root, "item.aging", ref=str(item_id), payload=payload)
        aging.append(payload)
        fresh_item[key] = {"mark": str(row.get("updated_at") or ""),
                           "at": _stamp()}

    # Rewriting the doc from the marks we still believe in is what implements
    # "reset on movement": a subject that moved, closed, or dropped off the
    # board is simply absent from the new doc, so its next silence reports.
    _save(root, {"chain": fresh_chain, "item": fresh_item})
    after = len(fresh_chain) + len(fresh_item)
    return {"window_h": hours, "stalled": stalled, "aging": aging,
            "cleared": max(0, before - after), "reminded": _remind(root)}


def _remind(root) -> int:
    """The unanswered-question reminder, on this tick. Returns how many fired.

    It also rides ``/api/console/state``, and it has to be HERE as well: that
    endpoint only runs while a browser is polling it, and "nobody is looking" is
    exactly the condition an unanswered question is stuck in. Idempotent by a
    stamp on the question itself (``steerbox._mark_reminded``), so both callers
    running costs one query and cannot double-ping.
    """
    try:
        from bgate_core import steerbox as _steerbox

        return len(_steerbox.remind_stale(root))
    except Exception:
        return 0


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def reset(root: str | os.PathLike[str]) -> None:
    """Forget every mark, so the next quiet subject reports again.

    For a human who has just drained the review queue and wants the next stall
    reported rather than suppressed by a mark from before. Tests use it too.
    """
    _save(root, {"chain": {}, "item": {}})
