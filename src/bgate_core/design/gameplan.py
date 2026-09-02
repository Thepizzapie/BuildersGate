"""The game plan — a premise compiled into an enumerable manifest.

The brainstorm room is the compiler's FRONT END: the human and the director
talk a premise into pillars, a core loop, and a plan. What was missing is the
back half — after approval, everything the game CONSISTS OF was implicit in
prose, so "what remains to build" had no answer and an empty queue after a bad
decomposition looked exactly like a finished game. (Measured: a director
decomposed one ask into a 9-item chain with the objective ranked last; six
items died superseded and nothing anywhere could say the game was not built.)

This module is the back half. ``ingest`` writes the approved manifest as
``plan_row`` rows — one per entity/asset/scene/system/sound/dialogue/level —
and compiles the VERTICAL SLICE rows onto the board with real dependency
links. Non-slice rows stay 'spec' on purpose: the board holds the slice, the
manifest holds the game, and ``status`` is the reconciliation between them.

Status past 'spec' is DERIVED from the linked work item, never stored — a
second status column would be a cache that lies the first time a reopen moves
the item and nothing moves the row.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from ..board import activity
from ..store import db
from ..store.util import rows

KINDS = ("entity", "asset", "scene", "system", "sound", "dialogue", "level")

MAX_NAME = 120
MAX_ACCEPTANCE = 2000


def validate_manifest(manifest: Any) -> list[dict]:
    """Strict, like brainstorm.validate_plan, and for the same reason: this
    runs on a manifest a human confirmed, and repairing it here files
    something other than what they approved. Raises ValueError, always with
    the row number."""
    from ..board import seats as _seats

    if not isinstance(manifest, list) or not manifest:
        raise ValueError("a manifest is a non-empty list of rows")
    names: set[str] = set()
    out: list[dict] = []
    for i, raw in enumerate(manifest, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"manifest row {i} is not an object")
        name = str(raw.get("name") or "").strip()[:MAX_NAME]
        if not name:
            raise ValueError(f"manifest row {i} has no name")
        if name in names:
            raise ValueError(f"manifest row {i} duplicates the name {name!r}")
        names.add(name)
        kind = str(raw.get("kind") or "asset").strip()
        if kind not in KINDS:
            raise ValueError(
                f"manifest row {i} ({name}): kind must be one of {KINDS}")
        seat = str(raw.get("seat") or "").strip()
        if seat not in _seats.DEFAULT_SEATS:
            raise ValueError(
                f"manifest row {i} ({name}): unknown seat {seat!r}")
        deps = raw.get("depends_on") or []
        if not isinstance(deps, list):
            raise ValueError(
                f"manifest row {i} ({name}): depends_on must be a list of row names")
        out.append({
            "kind": kind, "name": name, "seat": seat,
            "acceptance": str(raw.get("acceptance") or "").strip()[:MAX_ACCEPTANCE],
            "slice": bool(raw.get("slice")),
            "depends_on": [str(d).strip() for d in deps if str(d).strip()],
        })
    # Dependencies must name rows that exist — a dep on a fiction would compile
    # to nothing and the item it gates would dispatch immediately, which is the
    # exact same-tick failure queue_add_chain exists to stop.
    for row in out:
        for dep in row["depends_on"]:
            if dep not in names:
                raise ValueError(
                    f"{row['name']!r} depends on {dep!r}, which is not a "
                    "manifest row")
    return out


def _ordered(rows_in: list[dict]) -> list[dict]:
    """Dependency-respecting order (Kahn's), stable within a rank.

    A cycle is a ValueError with the members named: a human wrote this
    manifest and the fix is theirs to make, not ours to guess.
    """
    by_name = {r["name"]: r for r in rows_in}
    placed: list[dict] = []
    done: set[str] = set()
    remaining = list(rows_in)
    while remaining:
        progress = [r for r in remaining
                    if all(d in done or d not in by_name
                           for d in r["depends_on"])]
        if not progress:
            stuck = ", ".join(r["name"] for r in remaining)
            raise ValueError(f"the manifest has a dependency cycle among: {stuck}")
        for r in progress:
            placed.append(r)
            done.add(r["name"])
        remaining = [r for r in remaining if r["name"] not in done]
    return placed


def ingest(root: str | os.PathLike[str], manifest: Any,
           session_id: Optional[int] = None, file_slice: bool = True) -> dict:
    """Write the approved manifest and put its VERTICAL SLICE on the board.

    One row per thing the game needs. Slice rows are compiled to work items
    with real depends_on links (in dependency order, so every link names an
    item that already exists); everything else stays 'spec' — visible in
    ``status``, deliberately not queued, because a board holding the whole
    game at once is a board nobody can read.

    Idempotent on names: a row that already exists is updated in place rather
    than duplicated, and a row that already has a live work item keeps it.
    Call this only from a HUMAN-approved path (brainstorm_deploy) — the same
    review-step reasoning that refuses a machine there applies here.
    """
    from ..board import queue as _queue

    clean = _ordered(validate_manifest(manifest))
    filed: list[dict] = []
    kept = 0
    item_for: dict[str, int] = {}
    conn = db.connect(root)
    for row in clean:
        prior = conn.execute("SELECT * FROM plan_row WHERE name = ?",
                             (row["name"],)).fetchone()
        with db.tx(root) as tx:
            if prior:
                tx.execute(
                    "UPDATE plan_row SET kind=?, seat=?, acceptance=?, slice=?, "
                    "depends_on_names=?, session_id=COALESCE(?, session_id) "
                    "WHERE name=?",
                    (row["kind"], row["seat"], row["acceptance"],
                     1 if row["slice"] else 0, json.dumps(row["depends_on"]),
                     session_id, row["name"]))
                kept += 1
                if prior["work_item_id"]:
                    item_for[row["name"]] = int(prior["work_item_id"])
                    continue
            else:
                tx.execute(
                    "INSERT INTO plan_row (kind, name, seat, acceptance, slice, "
                    "depends_on_names, session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row["kind"], row["name"], row["seat"], row["acceptance"],
                     1 if row["slice"] else 0, json.dumps(row["depends_on"]),
                     session_id))
        if not (file_slice and row["slice"]):
            continue
        # The slice goes on the board. Deps compile to depends_on links; the
        # ordering above guarantees every named dep already has its item (a
        # dep on a non-slice row is left to the manifest — queueing the whole
        # closure would defeat the slice).
        dep_items = [item_for[d] for d in row["depends_on"] if d in item_for]
        # EVERY parent, not just the first. A scene that needs the sprite AND
        # the sound AND the script used to be able to express one of the three;
        # the rest raced it. The first rides the column (so chains and every
        # existing query still see it), the others go in work_item_dep.
        acceptance = row["acceptance"] or ("(none written — ask the director "
                                           "via queue_add before building)")
        brief = (f"[game plan: {row['kind']} '{row['name']}' — vertical slice]\n"
                 f"ACCEPTANCE: {acceptance}")
        item = _queue.add(root, row["seat"], f"{row['kind']}: {row['name']}",
                          brief=brief, priority=5, source="game-plan",
                          source_ref=row["name"],
                          depends_on=dep_items[0] if dep_items else None)
        item_for[row["name"]] = int(item["id"])
        for extra in dep_items[1:]:
            _queue.add_dependency(root, int(item["id"]), extra)
        with db.tx(root) as tx:
            tx.execute("UPDATE plan_row SET work_item_id = ? WHERE name = ?",
                       (int(item["id"]), row["name"]))
        filed.append({"id": int(item["id"]), "seat": row["seat"],
                      "name": row["name"],
                      "depends_on": dep_items[0] if dep_items else None})
    activity.log(root, "game-plan",
                 f"manifest ingested: {len(clean)} row(s), {len(filed)} slice "
                 f"item(s) filed, {kept} updated in place",
                 seat="director", ref=str(session_id or ""))
    return {"rows": len(clean), "slice_filed": filed, "updated": kept}


# The five states a plan row can be in, weakest first. They are DERIVED on
# every read — a stored status column would be a cache that lies the first
# time a reopen moves the item and nothing moves the row.
#
#   spec      nobody is building it. The board does not hold this.
#   on_board  a work item exists and has not finished.
#   lost      the item failed or was cancelled; the NEED survived the attempt.
#   built     the item reached done. This is where the old ladder stopped, and
#             it is the state that flatters: a sprite on disk that no scene
#             references is not in the game.
#   wired     something in the project actually REFERENCES it. Measured by
#             reading the scenes, not by asking the agent that made it.
#   verified  a QA gate passed on the item that built it.
STATES = ("spec", "on_board", "lost", "built", "wired", "verified")


def _referenced_names(root: str | os.PathLike[str]) -> set[str]:
    """Every resource path referenced by a scene or resource in the project.

    THE WIRED TEST, and it is deliberately a text scan rather than an engine
    load: `.tscn` and `.tres` name their dependencies in ext_resource lines,
    the harness already treats those files as the truth elsewhere, and a scan
    costs nothing and cannot be fooled by an agent's own report. What it
    answers is narrow and honest — "is this asset's name mentioned by
    something the game loads" — which is exactly the gap between an asset on
    disk and an asset in the game.
    """
    out: set[str] = set()
    # NOT `root / "game"`. Two entrypoints disagree about the layout — bgate
    # init scaffolds into <root>, godot_scaffold into <root>/game — and a
    # hardcode here reports every row of a root-layout project as unwired
    # forever, which is a coverage table that lies in the one direction nobody
    # checks. game_dir() is asked first because it is the project's own answer;
    # when there is no project.godot yet (a plan can be ingested before the
    # engine project exists) both candidates are scanned rather than neither.
    from pathlib import Path

    from ..store import project as _project

    try:
        found = _project.game_dir(root)
    except Exception:
        found = None
    roots = [found] if found else [Path(root) / "game", Path(root)]
    for game in roots:
        if not game.is_dir():
            continue
        out |= _scan_dir(game)
    return out


def _scan_dir(game) -> set[str]:
    out: set[str] = set()
    for pattern in ("**/*.tscn", "**/*.tres"):
        for path in game.glob(pattern):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line in text.splitlines():
                if "path=" not in line and "ExtResource" not in line:
                    continue
                out.add(line)
    return out


def _is_wired(name: str, blob: str) -> bool:
    """Does the project reference this row's name at all?

    Substring on the stem, because a manifest name ('hero_sheet') becomes a
    family of files ('hero_sheet.png', 'hero_sheet_frames.tres'). It answers
    'mentioned' rather than 'correctly wired' and the docstring says so: a
    cheap true negative is worth more than an expensive maybe, and QA's own
    checklist is what judges correctness.
    """
    stem = name.strip().lower()
    return bool(stem) and stem in blob


def _verdicts(root: str | os.PathLike[str]) -> set[int]:
    """Work item ids whose QA gate returned PASS.

    Reads the same 'VERDICT: PASS' marker the dashboard's history renderer
    parses — one marker, one meaning, in both places. A gate that finished
    without the marker is UNKNOWN and deliberately does not count as verified.
    """
    passed: set[int] = set()
    try:
        gates = rows(db.connect(root).execute(
            "SELECT source_ref, result FROM work_item "
            "WHERE source = 'qa-gate' AND status = 'done'"))
    except Exception:
        return passed
    for gate in gates:
        ref = str(gate["source_ref"] or "")
        if ref.isdigit() and "VERDICT: PASS" in (gate["result"] or ""):
            passed.add(int(ref))
    return passed


def digest(root: str | os.PathLike[str], hours: int = 12) -> dict:
    """WHAT HAPPENED WHILE YOU WERE AWAY — the morning report.

    There was no such surface. A board could run for eight hours and the only
    way to reconstruct it was to read the activity ledger by hand: autodeploy
    keeps one refusal (the LAST one), notices collapse past three into "11
    items finished", and the heartbeat reports stalled chains rather than "the
    board floored at 23:02 and never dispatched again". So the single most
    common question after an overnight run — what got built, what broke, why
    did it stop — had no answer anywhere.

    Everything here is read from the board and the ledger; nothing is inferred
    and nothing is spent. ``blocked`` is the important field: an empty board
    with items still queued is the signature of a floor refusal, and naming it
    is the difference between "it is working" and "it stopped six hours ago".
    """
    from ..board import queue as _queue
    from ..board import spend as _spend

    window = f"-{max(1, int(hours))} hour"
    conn = db.connect(root)
    moved = rows(conn.execute(
        "SELECT id, seat, title, status, source, attempts, total_cost_usd, "
        "updated_at, result FROM work_item "
        "WHERE updated_at >= datetime('now', ?) ORDER BY updated_at DESC",
        (window,)))
    done = [r for r in moved if r["status"] == "done"]
    failed = [r for r in moved if r["status"] == "failed"]
    review = [r for r in moved if r["status"] == "review"]
    try:
        still_queued = _queue.list_items(root, status="queued") or []
    except Exception:
        still_queued = []
    try:
        running = _queue.list_items(root, status="dispatched") or []
    except Exception:
        running = []

    # WHY IT STOPPED, if it did. Queued work with nothing running is either a
    # dead dashboard or a floor refusal, and both look identical from the board.
    blocked = ""
    if still_queued and not running:
        try:
            from ..board import gitwork as _git
            state = _git.dirty(root)
            if state.get("available") and state.get("dirty"):
                blocked = (f"{len(state['paths'])} uncommitted change(s) in the "
                           "tree — dispatch refuses a dirty tree, and that "
                           "refusal stops the WHOLE board, not one item. "
                           "Commit or stash and it resumes.")
        except Exception:
            pass
        if not blocked:
            blocked = ("work is queued and nothing is running — either the "
                       "dashboard is down (`bgate serve`), autopilot is off, "
                       "or a budget ceiling is refusing every dispatch.")

    try:
        money = _spend.totals(root)
    except Exception:
        money = {}
    return {
        "window_hours": int(hours),
        "finished": [{"id": r["id"], "seat": r["seat"], "title": r["title"][:90],
                      "cost_usd": round(float(r["total_cost_usd"] or 0), 3)}
                     for r in done[:25]],
        "failed": [{"id": r["id"], "seat": r["seat"], "title": r["title"][:90],
                    "attempts": int(r["attempts"] or 0),
                    "why": str(r["result"] or "")[:200]} for r in failed[:25]],
        "awaiting_you": [{"id": r["id"], "seat": r["seat"],
                          "title": r["title"][:90]} for r in review[:25]],
        "counts": {"finished": len(done), "failed": len(failed),
                   "in_review": len(review), "still_queued": len(still_queued),
                   "running": len(running)},
        "blocked": blocked,
        "spend": {"today_usd": money.get("today_usd"),
                  "week_usd": money.get("week_usd"),
                  "agent_runs": money.get("agent_runs"),
                  # THE COMBINED FIGURE, FIRST. `today_usd` on its own silently
                  # excluded 84 credits across 11 kie calls on the benchmark
                  # board - invisible to every spend figure AND to the budget
                  # ceilings. A total that omits a channel reads as complete,
                  # which is worse than no total. spend.spend_line is the one
                  # formatter, so no surface has to remember the footnote.
                  "line": money.get("spend_line"),
                  "today_line": money.get("today_spend_line"),
                  "complete": money.get("complete", True),
                  # Real charges with no dollar figure (unpriced kie credits).
                  # NOT inside today_usd/week_usd — see spend.record_unpriced.
                  "unaccounted": money.get("unaccounted")},
        # PREMISE REFUTATIONS, on the morning report. The most valuable thing
        # agents did in the benchmark and the one that died with the item.
        "premise_refuted": _refutations(root),
        "coverage": status(root)["slice"] if _has_plan(root) else None,
    }


def _refutations(root: str | os.PathLike[str]) -> list[dict]:
    """Briefs whose measured premise an agent DISPROVED. Never raises."""
    try:
        from ..board import queue as _queue

        return [{"item": r.get("item"), "claim": str(r.get("claim") or "")[:200],
                 "measured": str(r.get("measured") or "")[:200],
                 "did_instead": str(r.get("did_instead") or "")[:200],
                 "at": r.get("at")}
                for r in _queue.refutations(root, limit=10)]
    except Exception:                                             # noqa: BLE001
        return []


def _has_plan(root: str | os.PathLike[str]) -> bool:
    try:
        return bool(db.connect(root).execute(
            "SELECT 1 FROM plan_row LIMIT 1").fetchone())
    except Exception:
        return False


SLICE_CHECK_SOURCE = "slice-check"


def slice_check_due(root: str | os.PathLike[str]) -> dict:
    """Should a SLICE CHECK be filed right now? {due, why, open}.

    THE GAME IS NOT REVIEWED BY ANYONE. The QA gate reviews items — did this
    sprite land, did this scene get written — and a board of green items can
    sit on top of a build that does not boot. Every ingredient of a real check
    exists (godot_check_project, headless godot_run, screenshots, coverage);
    nothing composed them.

    Due when the slice's rows are all built (or better) and no check is open
    for the current state of it. Idempotent by the same rule as the QA gate:
    one open check at a time, and never a second for a slice that has not
    moved since the last one.
    """
    state = status(root)
    if not state["slice"]["rows"]:
        return {"due": False, "why": "this project has no vertical slice rows",
                "open": 0}
    try:
        existing = rows(db.connect(root).execute(
            "SELECT id, status, source_ref FROM work_item "
            "WHERE source = ? ORDER BY id DESC", (SLICE_CHECK_SOURCE,)))
    except Exception:
        return {"due": False, "why": "the board is unreadable", "open": 0}
    live = [e for e in existing if e["status"] in ("queued", "dispatched")]
    if live:
        return {"due": False, "why": "a slice check is already open",
                "open": int(live[0]["id"])}
    built = state["slice"]["in_game"] + sum(
        1 for r in state["remaining"] if r["slice"] and r["state"] == "built")
    if built < state["slice"]["rows"]:
        return {"due": False,
                "why": (f"{built} of {state['slice']['rows']} slice rows are "
                        "built — the slice is not ready to be played yet"),
                "open": 0}
    # The fingerprint is what stops a re-check of an unchanged slice: same
    # counts, same check. A slice that gained a row, lost one, or had one
    # wired since the last check reads differently and earns a fresh look.
    ref = (f"{state['slice']['rows']}/{state['slice']['in_game']}/"
           f"{state['built']}/{state['verified']}")
    if any(str(e["source_ref"] or "") == ref for e in existing):
        return {"due": False, "why": "the slice has not changed since the last "
                                     "check", "open": 0, "ref": ref}
    return {"due": True, "why": "every slice row is built and unchecked",
            "open": 0, "ref": ref}


def open_slice_check(root: str | os.PathLike[str]) -> dict:
    """File ONE qa item that reviews THE GAME rather than an item."""
    from ..board import queue as _queue

    due = slice_check_due(root)
    if not due["due"]:
        return {"ok": False, "why": due["why"]}
    state = status(root)
    unwired = [r["name"] for r in state["remaining"] if r["slice"]]
    brief = (
        "SLICE CHECK — review THE GAME, not one item.\n\n"
        f"Every row of the vertical slice ({state['slice']['rows']}) is built. "
        "This is the check nothing else in the harness performs: the QA gate "
        "reviews deliverables one at a time, so a board of green items can sit "
        "on top of a build that does not boot.\n\n"
        "Do all four, in order, and put the evidence in your result:\n"
        "1. godot_check_project — the project parses and imports.\n"
        "2. godot_run headless — the slice scene BOOTS. A parse error looks "
        "exactly like a hang, so print and quit early if it does not.\n"
        "3. godot_screenshot the slice scene at 640x360 and LOOK at it next to "
        "the pinned refs. A black frame is a fail, not a screenshot.\n"
        "4. plan_status — report the coverage line, and name anything built "
        "but not referenced by any scene.\n\n"
        + (f"BUILT BUT NOT REFERENCED BY ANY SCENE: {', '.join(unwired[:20])}. "
           "Each of those is an asset that exists on disk and is not in the "
           "game; say which are genuinely missing wiring and file "
           "queue_add(<the seat that owns the scene>, ...) for them.\n\n"
           if unwired else "")
        + "VERDICT: PASS only if the game boots and the slice is playable. "
          "Otherwise VERDICT: FAIL with the ranked list, and queue_add the "
          "fixes — a failing slice check that dispatches nothing is a "
          "complaint.")
    item = _queue.add(root, "qa", "SLICE CHECK: does the game actually play?",
                      brief=brief, priority=9, source=SLICE_CHECK_SOURCE,
                      source_ref=str(due.get("ref") or ""))
    activity.log(root, "game-plan",
                 f"slice check filed as #{item['id']} — every slice row is built",
                 seat="qa", ref=str(item["id"]))
    return {"ok": True, "item": int(item["id"]), "ref": due.get("ref") or ""}


def status(root: str | os.PathLike[str]) -> dict:
    """Coverage: what the game consists of vs what is actually IN it.

    THE answer to "what remains to build", and it deliberately does not stop
    at 'built': a generated sprite no scene references, and a scene no
    reviewer ever passed, are both work that looks finished from the board and
    is not in the game. See STATES.
    """
    joined = rows(db.connect(root).execute(
        "SELECT p.kind, p.name, p.seat, p.slice, p.work_item_id, "
        "w.status AS item_status "
        "FROM plan_row p LEFT JOIN work_item w ON w.id = p.work_item_id "
        "ORDER BY p.slice DESC, p.id"))
    blob = "\n".join(_referenced_names(root)).lower()
    passed = _verdicts(root)
    counts = {state: 0 for state in STATES}
    remaining: list[dict] = []
    for r in joined:
        if not r["work_item_id"] or r["item_status"] is None:
            state = "spec"
        elif r["item_status"] in ("failed", "cancelled"):
            state = "lost"
        elif r["item_status"] != "done":
            state = "on_board"
        elif int(r["work_item_id"]) in passed:
            state = "verified"
        elif _is_wired(r["name"], blob):
            state = "wired"
        else:
            state = "built"
        counts[state] += 1
        r["state"] = state
        if state not in ("wired", "verified"):
            remaining.append({"kind": r["kind"], "name": r["name"],
                              "seat": r["seat"], "slice": bool(r["slice"]),
                              "state": state,
                              "item": r["work_item_id"]})
    slice_rows = [r for r in joined if r["slice"]]
    in_game = ("wired", "verified")
    slice_in = sum(1 for r in slice_rows if r["state"] in in_game)
    return {
        "rows": len(joined),
        **counts,
        "in_game": counts["wired"] + counts["verified"],
        "slice": {"rows": len(slice_rows), "in_game": slice_in,
                  "complete": bool(slice_rows) and slice_in == len(slice_rows)},
        "remaining": remaining[:60],
        "note": ("no game plan ingested yet — brainstorm one and deploy it "
                 "with a manifest" if not joined else
                 "an empty queue is NOT a finished game, and 'built' is not "
                 "'in the game': 'remaining' lists every row no scene "
                 "references or no QA pass covers"),
    }
