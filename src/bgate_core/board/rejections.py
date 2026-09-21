"""ITEM 9b — a human's REJECTION is a signal, not just a status flip.

MEASURED at EXIT 67: art agents stitched sheets, ran their own gates, read
green, imported, and reported PASS on the gnome, the diesel dog, the forecourt
turret, the flyer, and the sniper deaths. Every correction came from the human
opening the sheet by hand. A reopen from that correction produced ANOTHER
mixed sheet and ANOTHER self-reported PASS — nothing in the loop noticed that
the same tool's output kept failing the same human, three, four, five times in
a row.

This module is the counter that notices. It answers one question: has a HUMAN
rejected this tool's output three times for one project inside a day? When the
answer is yes, the tool is refused for dispatched seats until a human clears
it (:func:`clear`) — the harness stops buying another round of the same
mistake and makes a person look at it.

Deliberately narrow: an AGENT'S OWN self-QA rejection (art_qa_verdict('fail'),
a QA gate FAIL) does not count here — that is the pipeline working. Only a
HUMAN saying no counts, because the failure this exists for is specifically
"the human keeps having to say no and nothing upstream notices."
"""
from __future__ import annotations

import os
from typing import Optional

from ..store import db

#: three human rejections of the same tool, on one project, within a day.
THRESHOLD = 3
WINDOW_HOURS = 24


def record(root: str | os.PathLike[str], tool: str, *, seat: str = "",
           item_id: Optional[int] = None, reason: str = "",
           by: str = "") -> dict:
    """Log one HUMAN rejection of `tool`'s output. Call this from the place a
    human's 'no' is recorded — artifacts.review(status='rejected', actor=
    human), queue.reject() — never from an agent's own self-QA fail.
    """
    tool = (tool or "").strip()
    if not tool:
        return {"ok": False, "error": "no tool name given"}
    with db.tx(root) as conn:
        conn.execute(
            "INSERT INTO human_rejection (tool, seat, item_id, reason, by) "
            "VALUES (?, ?, ?, ?, ?)",
            (tool[:120], (seat or "")[:60], item_id, (reason or "")[:500],
             (by or "")[:120]))
    return {"ok": True, "tool": tool, **count(root, tool)}


def count(root: str | os.PathLike[str], tool: str) -> dict:
    """How many uncleared human rejections `tool` has within WINDOW_HOURS."""
    tool = (tool or "").strip()
    row = db.connect(root).execute(
        "SELECT COUNT(*) AS n FROM human_rejection WHERE tool = ? "
        "AND cleared_at IS NULL "
        "AND created_at >= datetime('now', ?)",
        (tool, f"-{WINDOW_HOURS} hours")).fetchone()
    n = int(row["n"]) if row else 0
    return {"count": n, "blocked": n >= THRESHOLD}


def blocked(root: str | os.PathLike[str], tool: str) -> bool:
    """True when `tool` has hit THRESHOLD uncleared human rejections."""
    if not tool:
        return False
    try:
        return bool(count(root, tool)["blocked"])
    except Exception:                                             # noqa: BLE001
        return False       # fail-open: a broken counter must never itself block


def recent(root: str | os.PathLike[str], tool: str, limit: int = 3) -> list[dict]:
    """The most recent uncleared rejections for `tool`, newest first — what a
    seat brief's STOP AND ASK line names."""
    rows = db.connect(root).execute(
        "SELECT * FROM human_rejection WHERE tool = ? AND cleared_at IS NULL "
        "AND created_at >= datetime('now', ?) "
        "ORDER BY created_at DESC LIMIT ?",
        (tool, f"-{WINDOW_HOURS} hours", int(limit))).fetchall()
    return [dict(r) for r in rows]


def blocked_tools(root: str | os.PathLike[str], seat: str = "") -> list[dict]:
    """Every tool currently blocked, for the seat brief / dispatch prompt.

    `seat` narrows to rejections recorded against that seat (a blank seat on
    the row, from a source that did not know it, still counts — dropping it
    would let an unattributed rejection go unseen by every seat)."""
    if seat:
        rows = db.connect(root).execute(
            "SELECT tool, COUNT(*) AS n FROM human_rejection "
            "WHERE cleared_at IS NULL AND created_at >= datetime('now', ?) "
            "AND (seat = ? OR seat = '') GROUP BY tool HAVING n >= ?",
            (f"-{WINDOW_HOURS} hours", seat, THRESHOLD)).fetchall()
    else:
        rows = db.connect(root).execute(
            "SELECT tool, COUNT(*) AS n FROM human_rejection "
            "WHERE cleared_at IS NULL AND created_at >= datetime('now', ?) "
            "GROUP BY tool HAVING n >= ?",
            (f"-{WINDOW_HOURS} hours", THRESHOLD)).fetchall()
    return [{"tool": r["tool"], "count": int(r["n"])} for r in rows]


def clear(root: str | os.PathLike[str], tool: str, *, by: str = "") -> dict:
    """A human clearing the block — the only way past it. Marks every
    uncleared rejection for `tool` as cleared; does not delete the history."""
    tool = (tool or "").strip()
    if not tool:
        return {"ok": False, "error": "no tool name given"}
    with db.tx(root) as conn:
        cur = conn.execute(
            "UPDATE human_rejection SET cleared_at = datetime('now') "
            "WHERE tool = ? AND cleared_at IS NULL", (tool,))
        n = cur.rowcount
    return {"ok": True, "tool": tool, "cleared": int(n or 0)}
