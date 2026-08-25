"""Enemy and objective design, graded as INTERACTION rather than inventory.

TWO FAILURES, ONE SHAPE. Both were shipped by Night Shift and both look like
completed design work right up to the moment somebody plays it.

1. THE ENEMY ROSTER WAS A LIST OF STATE MACHINES. "melee / ranged / support"
   was accepted as an enemy design, so three enemies got built that never
   changed each other's threat profile: fighting all three at once was
   arithmetic on the same fight, not a different fight. Nothing in the harness
   asked the question that matters, which is not *what does this enemy do* but
   *what does this enemy do to the player's read of the OTHER enemy*.

2. EVERY QUOTA TASK WAS "STAND HERE FOR N SECONDS". Eight objectives, one
   commitment shape, so the player learned one answer on task one and spent the
   rest of the game re-entering it. A task list can be long, varied in fiction
   and completely uniform in mechanics, and prose review will not catch it
   because the fiction is where the variety is.

So this module holds two declarations and grades both:

``roster``      enemies, each naming how it ALTERS another enemy's threat.
``objectives``  tasks, each naming its COMMITMENT SHAPE from a fixed
                vocabulary, and what that shape takes away from the player.

:func:`production_blockers` is what the stage machine calls — a project does not
leave graybox for production with a roster of isolated machines or a task list
that is one shape eight times.

WHY A FIXED SHAPE VOCABULARY. Free text would let "stand in the marked circle"
and "remain in the zone" read as two designs. The vocabulary is a small closed
set on purpose: the point is to make sameness visible, and you cannot count
distinct shapes in prose.
"""
from __future__ import annotations

import os
import time
from typing import Any

from . import activity, workspace as _ws

SEAT = "director"
DOC_KEY = "encounter"

MAX_TEXT = 1200
MAX_NAME = 80

#: The commitment shapes a task can take. Each one is a different thing the
#: player has to GIVE UP for the duration — which is what makes two tasks
#: different, and what a fiction-level description hides.
SHAPES: dict[str, str] = {
    "dwell": "stay in a place — the player gives up mobility",
    "carry": "hold or haul an object — gives up a capability while carrying",
    "escort": "keep something else alive and moving — gives up pace control",
    "defend": "hold a thing that can be lost — gives up the choice of where "
              "to fight",
    "route": "reach places in an order under pressure — gives up free "
             "navigation",
    "timing": "act inside a window — gives up acting when convenient",
    "disarm": "proceed without the usual attack — gives up the main verb",
    "restrict": "move under a rule (no sprint, no light, one hand) — gives up "
                "a movement option",
    "manipulate": "change the environment to proceed — gives up the direct "
                  "solution",
    "spend": "pay a resource the player wanted for something else — gives up "
             "a future option",
    "gather": "collect scattered things — gives up concentration of force",
}

#: A roster this size or larger must describe interactions. Below it, "the one
#: enemy" is a legitimate design and demanding a combination matrix from it
#: would be the gate refusing a game for being small.
MIN_ROSTER_FOR_INTERACTION = 2

#: At this many objectives or more, the shape spread is graded. Two tasks
#: sharing a shape is a coincidence; eight is a design.
MIN_TASKS_FOR_SPREAD = 3

#: How many distinct shapes a graded task list owes.
MIN_DISTINCT_SHAPES = 3


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _doc(root: str | os.PathLike[str]) -> dict:
    try:
        got = _ws.get(root, SEAT, DOC_KEY, {}) or {}
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def _save(root: str | os.PathLike[str], doc: dict) -> dict:
    clean = {k: v for k, v in doc.items() if k != _ws.VERSION_KEY}
    _ws.set(root, SEAT, DOC_KEY, clean)
    return clean


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


# ── the roster ──────────────────────────────────────────────────────────────

def validate_roster(raw: Any) -> list[dict]:
    """Enemies, or a ValueError naming what makes this a list of machines.

    Each row: ``name``, ``pressure`` (what it does to the player on its own),
    and ``alters`` — a list of ``{enemy, effect}`` saying how this one changes
    another's threat profile. The last field is the whole module.

    ``role`` is accepted and stored, and is deliberately NOT sufficient: a row
    whose only design content is ``role: "melee"`` is exactly what got built
    last time.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("a roster is a non-empty list of enemy rows")
    out: list[dict] = []
    names: list[str] = []
    for i, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            raise ValueError(f"roster row {i} is not an object")
        name = _text(row.get("name"), MAX_NAME)
        if not name:
            raise ValueError(f"roster row {i} has no name")
        if name.lower() in {n.lower() for n in names}:
            raise ValueError(f"roster row {i} duplicates the name {name!r}")
        pressure = _text(row.get("pressure"))
        if len(pressure) < 15:
            raise ValueError(
                f"{name}: no 'pressure' — say what this enemy does to the "
                "player on its own, in a sentence. 'melee' is a category, not "
                "a pressure.")
        alters = []
        for j, alt in enumerate(row.get("alters") or [], 1):
            if not isinstance(alt, dict):
                raise ValueError(f"{name}: alters[{j}] is not an object")
            other = _text(alt.get("enemy"), MAX_NAME)
            effect = _text(alt.get("effect"))
            if not other:
                raise ValueError(f"{name}: alters[{j}] names no other enemy")
            if len(effect) < 15:
                raise ValueError(
                    f"{name}: alters[{j}] ({other}) has no effect — say how "
                    f"{name} changes what {other} threatens, not that they "
                    "appear together")
            if other.lower() == name.lower():
                raise ValueError(
                    f"{name}: an enemy altering itself is its own pressure, "
                    "which is the 'pressure' field")
            alters.append({"enemy": other, "effect": effect})
        names.append(name)
        out.append({"name": name, "role": _text(row.get("role"), MAX_NAME),
                    "pressure": pressure, "alters": alters,
                    "counterplay": _text(row.get("counterplay"))})
    known = {n.lower() for n in names}
    for row in out:
        for alt in row["alters"]:
            if alt["enemy"].lower() not in known:
                raise ValueError(
                    f"{row['name']} alters {alt['enemy']!r}, which is not in "
                    "the roster")
    return out


def roster_findings(rows: list[dict]) -> list[str]:
    """What is wrong with this roster as INTERACTION design. Empty means fine.

    Separate from validation because these are judgements about the whole
    roster rather than the shape of a row, and because the director should be
    able to see them while drafting rather than only when the gate refuses.
    """
    out: list[str] = []
    if len(rows) < MIN_ROSTER_FOR_INTERACTION:
        return out
    touched: set[str] = set()
    for row in rows:
        for alt in row["alters"]:
            touched.add(row["name"].lower())
            touched.add(alt["enemy"].lower())
    for row in rows:
        if row["name"].lower() not in touched:
            out.append(
                f"{row['name']} does not change, and is not changed by, any "
                "other enemy — it is a state machine that happens to be in the "
                "same room. Say what it does to the player's read of another "
                "enemy, or cut it.")
    pairs = {(row["name"].lower(), alt["enemy"].lower())
             for row in rows for alt in row["alters"]}
    if not pairs:
        out.append(
            "no enemy in this roster alters any other — the design is "
            "'melee / ranged / support' with different words. Fighting all of "
            "them at once is arithmetic on the same fight.")
    return out


def set_roster(root: str | os.PathLike[str], raw: Any, by: str = "") -> dict:
    clean = validate_roster(raw)
    doc = _doc(root)
    doc["roster"] = clean
    doc["roster_by"] = by or activity.current_actor()
    doc["roster_at"] = _now()
    _save(root, doc)
    activity.log(root, "encounter", f"enemy roster set: {len(clean)} enemies, "
                 f"{sum(len(r['alters']) for r in clean)} interactions",
                 seat=SEAT)
    return {"roster": clean, "findings": roster_findings(clean)}


def roster(root: str | os.PathLike[str]) -> list[dict]:
    got = _doc(root).get("roster")
    return got if isinstance(got, list) else []


# ── the objectives ──────────────────────────────────────────────────────────

def validate_objectives(raw: Any) -> list[dict]:
    """Tasks, or a ValueError. Each names a shape from :data:`SHAPES`.

    ``costs`` is required and is the field that makes the shape real: a task
    that takes nothing away is a timer with a name on it.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("objectives are a non-empty list of task rows")
    out: list[dict] = []
    seen: set[str] = set()
    for i, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            raise ValueError(f"objective row {i} is not an object")
        name = _text(row.get("name"), MAX_NAME)
        if not name:
            raise ValueError(f"objective row {i} has no name")
        if name.lower() in seen:
            raise ValueError(f"objective row {i} duplicates the name {name!r}")
        seen.add(name.lower())
        shape = _text(row.get("shape"), 40).lower().replace(" ", "_")
        if shape not in SHAPES:
            listing = ", ".join(sorted(SHAPES))
            raise ValueError(
                f"{name}: shape {shape or '(none)'!r} is not one of {listing}. "
                "The vocabulary is closed on purpose — two tasks described in "
                "different fiction are the same task if they are the same "
                "shape, and prose cannot be counted.")
        costs = _text(row.get("costs"))
        if len(costs) < 15:
            raise ValueError(
                f"{name}: no 'costs' — say what the player gives up for the "
                f"duration. {SHAPES[shape]}. A task that takes nothing away "
                "is a timer with a name on it.")
        out.append({"name": name, "shape": shape, "costs": costs,
                    "notes": _text(row.get("notes"))})
    return out


def objective_findings(rows: list[dict]) -> list[str]:
    """What is wrong with this task list as a SPREAD. Empty means fine."""
    out: list[str] = []
    if len(rows) < MIN_TASKS_FOR_SPREAD:
        return out
    shapes = [r["shape"] for r in rows]
    distinct = sorted(set(shapes))
    if len(distinct) < MIN_DISTINCT_SHAPES:
        out.append(
            f"{len(rows)} objectives across {len(distinct)} commitment "
            f"shape(s) ({', '.join(distinct)}) — at least "
            f"{MIN_DISTINCT_SHAPES} distinct shapes, or the player learns one "
            "answer on task one and re-enters it for the rest of the game.")
    dwell = shapes.count("dwell")
    cap = max(1, len(rows) // 3)
    if dwell > cap:
        out.append(
            f"{dwell} of {len(rows)} objectives are 'dwell' (stand here for N "
            f"seconds); at most {cap} should be. This is the shape that "
            "shipped eight times in a row last time, and the fiction on top of "
            "it did not make them different tasks.")
    for shape in distinct:
        count = shapes.count(shape)
        if shape != "dwell" and count > max(2, len(rows) // 2):
            out.append(
                f"{count} of {len(rows)} objectives are {shape!r} — a majority "
                "of one shape is the same uniformity, wearing a different hat")
    return out


def set_objectives(root: str | os.PathLike[str], raw: Any, by: str = "") -> dict:
    clean = validate_objectives(raw)
    doc = _doc(root)
    doc["objectives"] = clean
    doc["objectives_by"] = by or activity.current_actor()
    doc["objectives_at"] = _now()
    _save(root, doc)
    shapes = sorted({r["shape"] for r in clean})
    activity.log(root, "encounter",
                 f"objectives set: {len(clean)} tasks over "
                 f"{len(shapes)} shape(s) ({', '.join(shapes)})", seat=SEAT)
    return {"objectives": clean, "findings": objective_findings(clean)}


def objectives(root: str | os.PathLike[str]) -> list[dict]:
    got = _doc(root).get("objectives")
    return got if isinstance(got, list) else []


# ── the gate ────────────────────────────────────────────────────────────────

def production_blockers(root: str | os.PathLike[str]) -> list[str]:
    """What this module holds against leaving graybox. Empty means go.

    A project with NEITHER a roster nor objectives declared is not blocked:
    plenty of games have no enemies and no quota tasks, and a gate that
    demanded both would be refusing a puzzle game for not being a shooter.
    What is refused is a declared roster of isolated machines, or a declared
    task list that is one shape wearing several names.
    """
    out: list[str] = []
    rows = roster(root)
    if rows:
        out.extend(f"enemy design: {row}" for row in roster_findings(rows))
    tasks = objectives(root)
    if tasks:
        out.extend(f"objective design: {row}" for row in objective_findings(tasks))
    return out


def state(root: str | os.PathLike[str]) -> dict:
    rows, tasks = roster(root), objectives(root)
    return {
        "roster": rows,
        "roster_findings": roster_findings(rows) if rows else [],
        "objectives": tasks,
        "objective_findings": objective_findings(tasks) if tasks else [],
        "shapes": SHAPES,
        "shape_spread": sorted({t["shape"] for t in tasks}),
        "blockers": production_blockers(root),
    }
