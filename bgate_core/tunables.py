"""Tunables, with what the playtests actually measured beside them.

THE SEAT'S OWN RULE, WHICH NOTHING IMPLEMENTED: *the measured number sits next
to the knob — read it before you turn it.* A gameplay seat that shows a value of
`0.35` and nothing else invites somebody to make it `0.4` on instinct, and the
whole point of recording playtests was to stop that being the only available
move.

NOTHING NEW IS RECORDED HERE. Every input already exists and was simply never
joined:

  · `iteration.tunables_json` is a snapshot of every `@export`-style constant in
    the game's scripts, captured when the iteration opened (see iterations.py).
  · `playtest_session` rows carry `started_at`, so a session belongs to whatever
    iteration was open when it was recorded.
  · `playtest_event` rows are the telemetry the running game emitted — deaths,
    retries, jumps, whatever the project's own contract names.

So "what the playtests measured about this knob" is: the sessions that ran while
that value was in force, and the events they produced. That is a real answer.

WHAT THIS DELIBERATELY WILL NOT DO. It does not compute a correlation, and it
does not recommend a value. Three sessions at 0.35 and two at 0.4 is not an
experiment, and dressing a difference of means up as a verdict is exactly the
invented number this codebase keeps having to delete. `verdict` says which of
`measured | one sample | not measured` is true, and the caller shows the counts
underneath it. A human reads the two rows and decides.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any

from bgate_core import db


def _iterations(conn) -> list[dict]:
    """Every iteration, newest first, with its captured tunables parsed."""
    out = []
    for row in conn.execute(
            "SELECT id, goal, status, created_at, completed_at, tunables_json "
            "FROM iteration ORDER BY id DESC"):
        try:
            snap = json.loads(row["tunables_json"] or "{}")
        except (TypeError, ValueError):
            snap = {}
        out.append({"id": row["id"], "goal": row["goal"], "status": row["status"],
                    "created_at": row["created_at"], "completed_at": row["completed_at"],
                    "tunables": snap})
    return out


def _flatten(snapshot: dict) -> dict[str, str]:
    """`{file: {name: value}}` → `{file::name: value}`.

    The file is part of the key on purpose: two scripts may both declare
    `SPEED`, and collapsing them would report one script's history against the
    other's knob. `overrides` is the one pseudo-file iterations.py writes
    (.bgate/tunables.json) and it keeps its own namespace for the same reason.
    """
    flat: dict[str, str] = {}
    for path, found in (snapshot or {}).items():
        if not isinstance(found, dict):
            continue
        for name, value in found.items():
            flat[f"{path}::{name}"] = str(value)
    return flat


def _sessions(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, started_at, duration_s, status FROM playtest_session "
        "WHERE status = 'ready' ORDER BY started_at").fetchall()
    return [dict(r) for r in rows]


def _events(conn, session_ids: list[int]) -> dict[int, dict[str, int]]:
    """Event counts per session, by kind. The kinds are the PROJECT's own — a
    fixed list here would report zero for every game that named its events
    anything else."""
    if not session_ids:
        return {}
    marks = ",".join("?" * len(session_ids))
    counts: dict[int, dict[str, int]] = defaultdict(dict)
    for row in conn.execute(
            f"SELECT session_id, kind, count(*) AS n FROM playtest_event "
            f"WHERE session_id IN ({marks}) GROUP BY session_id, kind",
            session_ids):
        counts[row["session_id"]][row["kind"]] = row["n"]
    return counts


def _iteration_for(iterations: list[dict], when: str) -> dict | None:
    """The iteration that was open when `when` happened.

    Iterations are ordered newest-first; the first one that opened at or before
    the session is the one it ran under. A session older than every iteration
    belongs to none — that is a real state (playtests predate the iteration
    feature) and it is reported rather than guessed at.
    """
    for it in iterations:
        if str(it["created_at"] or "") <= str(when or ""):
            return it
    return None


def measured(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Every tunable ever captured, with the sessions recorded at each value.

    Returns ``{"tunables": [...], "iterations": n, "sessions": n}``. Each
    tunable is ``{key, file, name, current, history: [{value, iterations,
    sessions, events}], verdict}``.
    """
    conn = db.connect(root)
    iterations = _iterations(conn)
    sessions = _sessions(conn)
    events = _events(conn, [s["id"] for s in sessions])

    # value → the iterations that held it, and the sessions run under them
    per_key: dict[str, dict[str, dict]] = defaultdict(dict)
    for it in iterations:
        for key, value in _flatten(it["tunables"]).items():
            slot = per_key[key].setdefault(
                value, {"value": value, "iterations": [], "sessions": [], "events": {}})
            slot["iterations"].append(it["id"])

    by_iteration: dict[int, list[dict]] = defaultdict(list)
    for s in sessions:
        it = _iteration_for(iterations, s["started_at"])
        if it:
            by_iteration[it["id"]].append(s)

    for key, values in per_key.items():
        for slot in values.values():
            for it_id in slot["iterations"]:
                for s in by_iteration.get(it_id, []):
                    slot["sessions"].append(
                        {"id": s["id"], "name": s["name"],
                         "duration_s": s["duration_s"], "started_at": s["started_at"]})
                    for kind, n in events.get(s["id"], {}).items():
                        slot["events"][kind] = slot["events"].get(kind, 0) + n

    current = _flatten(iterations[0]["tunables"]) if iterations else {}

    out = []
    for key, values in sorted(per_key.items()):
        path, _, name = key.partition("::")
        history = sorted(values.values(),
                         key=lambda v: (-len(v["sessions"]), v["value"]))
        played = sum(len(v["sessions"]) for v in history)
        # THE VERDICT IS ABOUT EVIDENCE, NOT ABOUT THE VALUE. Nothing here
        # recommends a number; it says whether anyone has played the game at
        # this setting, which is the question "read it before you turn it" asks.
        verdict = ("measured" if played >= 2 and len(history) > 1
                   else "one sample" if played else "not measured")
        out.append({"key": key, "file": path, "name": name,
                    "current": current.get(key), "history": history,
                    "sessions": played, "verdict": verdict})
    return {"tunables": out, "iterations": len(iterations), "sessions": len(sessions)}
