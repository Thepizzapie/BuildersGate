"""Work history — what finished, how it finished, and who checked it.

THE GAP THIS FILLS. The Overview could say what is running and what just
happened, and nothing at all about what has been COMPLETED. 341 work items on
this project, 306 of them finished, and the only way to read the outcome of any
one of them was to page the queue by hand.

WHAT "VERDICT" MEANS HERE, precisely, because inventing one would be worse than
having none:

  A verdict is an INDEPENDENT judgement recorded against a work item. There are
  exactly three ways one gets written in this system, and this module reads all
  three rather than guessing:

  1. THE AGENT GATE (``bgate_ui.qa_gate``). A completed maker-seat item spawns a
     child work item with ``source='qa-gate'`` and ``source_ref`` = the reviewed
     item's id. That reviewer is instructed to write ``VERDICT: PASS`` or
     ``VERDICT: FAIL`` into its own result (qa_gate._brief_for). Only that marker
     is a verdict — a gate run that finished without writing one decided
     NOTHING, and reporting it as a pass is a gate that does not gate. The same
     rule is applied in the QA workspace (static/seats/qa.js); it is duplicated
     here rather than shared because the two read different shapes, but the
     regex and the UNKNOWN case are deliberately identical.
  2. THE ROUND CAP. Past ``qa.max_rounds`` the gate stops reviewing and files one
     ``source='qa-gate-escalation'`` item for the director. That is not a pass or
     a fail; it is "two agents could not agree and a human owes a call".
  3. THE BUILDER'S GATE. The human approves, which stamps ``approved_by`` and
     moves the item 'review' -> 'done' (queue.approve). An item still sitting in
     'review' has no verdict yet — it is waiting on the person reading this page.

  EVERYTHING ELSE HAS NO VERDICT, and this module says so in those words. Under
  ``gate.mode = none`` — which is what this project is set to — an agent's own
  word closes its item and nothing independent ever looks at it. The honest
  rendering of that is not a blank cell and it is certainly not a green tick: it
  is "closed on the agent's own word", with the evidence for that claim
  (``closed_by``, ``gate_skip``) attached. A UI that implies verification which
  did not happen is the exact failure the gate module exists to prevent.

WHY A ROUTER AND NOT app.py. Two reasons. Auto-registration (routes/__init__)
means no edit to a file three other people are in, and the log reader below
keeps its own index — it must NOT reuse ``dispatch.read_activity``, whose
``_feed_lock`` is held by the live console's 3-second poll. A historical log here
is 60MB (item-53); parsing one under that lock would stall every live agent
feed on the page.
"""
from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from bgate_core import db
from bgate_ui.deps import root

router = APIRouter()

# The gate protocol's marker. Same expression as static/seats/qa.js:783.
_VERDICT_RE = re.compile(r"VERDICT:\s*(PASS|FAIL)", re.IGNORECASE)

GATE_SOURCE = "qa-gate"
ESCALATION_SOURCE = "qa-gate-escalation"

# What counts as history: finished, or held awaiting a human. 'queued' and
# 'dispatched' are the present tense — "Running now" already owns those.
OUTCOMES = ("done", "failed", "cancelled", "review")

# How much of the item's own result note travels with a list row. The full note
# comes back with the log payload; a list of 50 rows carrying 20KB results each
# is a 1MB response to draw a table.
_RESULT_PREVIEW = 400


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------
def _verdicts_for(conn, ids: list[int]) -> dict[int, dict]:
    """One query for the whole page's gate children. Never N+1.

    Returns ``{item_id: verdict}``. The LAST gate child wins: a FAIL reopens the
    original, the fix round spawns a fresh gate, and the standing verdict is
    whatever the most recent completed round said — showing the first one would
    report an item as failed forever after it was fixed.
    """
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, source, source_ref, status, result, updated_at "
        f"FROM work_item WHERE source IN ('{GATE_SOURCE}', '{ESCALATION_SOURCE}') "
        f"AND CAST(source_ref AS INTEGER) IN ({marks}) ORDER BY id",
        tuple(ids)).fetchall()
    gates: dict[int, list] = {}
    escalated: set[int] = set()
    for row in rows:
        try:
            ref = int(row["source_ref"])
        except (TypeError, ValueError):
            continue
        if row["source"] == ESCALATION_SOURCE:
            escalated.add(ref)
        else:
            gates.setdefault(ref, []).append(row)
    out: dict[int, dict] = {}
    for ref, runs in gates.items():
        # Prefer the newest run that actually DECIDED something. A cancelled or
        # still-queued latest round must not erase the pass that came before it.
        decided = [r for r in runs
                   if r["status"] == "done" and _VERDICT_RE.search(r["result"] or "")]
        pick = decided[-1] if decided else runs[-1]
        out[ref] = {**_gate_verdict(pick), "rounds": len(runs),
                    "escalated": ref in escalated}
    for ref in escalated:
        if ref not in out:
            out[ref] = {"kind": "escalated", "label": "escalated",
                        "short": "a human was asked to arbitrate",
                        "why": "the QA round cap was hit and a human was asked "
                               "to arbitrate — no pass or fail was ever settled",
                        "gate_item": None, "detail": "", "rounds": 0,
                        "escalated": True}
    return out


def _gate_verdict(gate_row) -> dict:
    """Read one gate run's outcome the way the protocol defines it."""
    result = gate_row["result"] or ""
    match = _VERDICT_RE.search(result)
    base = {"gate_item": int(gate_row["id"]), "detail": result[:600],
            "at": gate_row["updated_at"]}
    if match:
        passed = match.group(1).upper() == "PASS"
        return {**base,
                "kind": "pass" if passed else "fail",
                "label": "PASS" if passed else "FAIL",
                "short": ("an independent QA agent checked it"
                          if passed else "an independent QA agent rejected it"),
                "why": ("an independent QA agent verified the claim against the "
                        "real artefact and it held"
                        if passed else
                        "an independent QA agent rejected the claim and "
                        "reopened the item with a nitpick list")}
    if gate_row["status"] == "failed":
        return {**base, "kind": "error", "label": "gate errored",
                "short": "the QA run itself died",
                "why": "the QA run itself died — nothing was decided"}
    if gate_row["status"] == "cancelled":
        return {**base, "kind": "none", "label": "gate cancelled",
                "short": "the QA round never ran to a decision",
                "why": "the QA round was cancelled before it decided anything"}
    if gate_row["status"] == "done":
        return {**base, "kind": "unknown", "label": "no verdict written",
                "short": "the gate run decided nothing",
                "why": "the QA run finished without writing a VERDICT line — "
                       "nothing was decided here, and calling that a pass "
                       "would be a gate that does not gate"}
    return {**base, "kind": "reviewing", "label": "under review",
            "short": "a QA agent is checking this now",
            "why": "a QA agent is checking this now"}


def _ungated(row) -> dict:
    """No independent check exists. Say which flavour of that this is.

    Everything below is read off the row, not off the CURRENT gate mode: an item
    closed months ago was closed under whatever mode was set then, and the
    dashboard cannot know what that was. What it CAN prove is that no QA round,
    no escalation and no approval was ever recorded against this item — so the
    claim made here is exactly that, and no more.
    """
    status = row["status"]
    closed_by = str(row["closed_by"] or "")
    stub = {"rounds": 0, "escalated": False, "gate_item": None, "detail": ""}
    if str(row["approved_by"] or ""):
        return {**stub, "kind": "approved", "label": "approved",
                "short": f"signed off by {row['approved_by']}",
                "why": f"the builder's gate: {row['approved_by']} approved this "
                       "by hand before it counted as done"}
    if status == "failed":
        return {**stub, "kind": "na", "label": "not verified",
                "short": "the run failed — nothing to check",
                "why": "the run failed — there was no deliverable to check"}
    if status == "cancelled":
        return {**stub, "kind": "na", "label": "not verified",
                "short": "cancelled before it produced anything",
                "why": "the item was cancelled before it produced anything"}
    if status in ("queued", "dispatched"):
        # Only reachable through the log endpoint: an item that WAS in history
        # and has since been reopened. Saying "closed on the agent's own word"
        # about work that is running again would be wrong in both directions.
        return {**stub, "kind": "reviewing", "label": "back in the queue",
                "short": "reopened — this round is not finished",
                "why": "this item was reopened after it closed, so the log "
                       "below is the previous round and nothing has been "
                       "judged yet"}
    if status == "review":
        return {**stub, "kind": "awaiting", "label": "awaiting you",
                "short": "the builder's gate is holding this",
                "why": "the builder's gate is holding this — it is NOT closed, "
                       "and anything chained behind it is waiting on your yes"}
    # The agent's own close is checked FIRST and beats gate_skip. It is the more
    # specific fact — "the thing that did the work said it was finished" — and
    # queue.complete only sets gate_skip on a close a machine did NOT make, so
    # the two disagreeing means the row was edited by hand afterwards.
    if closed_by.startswith("agent:"):
        return {**stub, "kind": "ungated", "label": "no gate",
                "short": "closed on the agent's own word",
                "why": "closed on the agent's own word — no independent check "
                       "was ever filed against it"}
    if row["gate_skip"]:
        return {**stub, "kind": "ungated", "label": "no gate",
                "short": "hand-closed, gate deliberately skipped",
                "why": f"hand-closed{' by ' + closed_by if closed_by else ''} "
                       "with the gate deliberately skipped — no QA round was filed"}
    if closed_by:
        return {**stub, "kind": "ungated", "label": "no gate",
                "short": f"hand-closed by {closed_by}",
                "why": f"hand-closed by {closed_by} — no QA round was filed "
                       "against it"}
    return {**stub, "kind": "ungated", "label": "no gate",
            "short": "closed on the agent's own word",
            "why": "closed on the agent's own word — no independent check was "
                   "ever filed against it"}


def _log_bytes(root_dir: Path, item_id: int) -> int:
    try:
        return (root_dir / ".bgate" / "agents" / f"item-{item_id}.log").stat().st_size
    except OSError:
        return 0


@router.get("/api/history")
def history(limit: int = Query(40, ge=1, le=200), offset: int = Query(0, ge=0),
            seat: Optional[str] = None, outcome: Optional[str] = None,
            q: Optional[str] = None, gate_runs: bool = False) -> dict:
    """Finished work, newest first, each row carrying its verdict.

    Paged in SQL rather than in the browser: a project with a thousand items
    must cost the same to draw as one with fifty.
    """
    root_dir = root()
    conn = db.connect(root_dir)

    where: list[str] = []
    params: list = []
    if outcome and outcome in OUTCOMES:
        where.append("status = ?")
        params.append(outcome)
    else:
        where.append("status IN ({})".format(", ".join("?" * len(OUTCOMES))))
        params += list(OUTCOMES)
    if not gate_runs:
        # The gate's own runs are rendered AS the verdict of the item they
        # reviewed. Listing them again doubles every reviewed row and buries the
        # work under the reviewing of the work.
        where.append("source NOT IN (?, ?)")
        params += [GATE_SOURCE, ESCALATION_SOURCE]
    if seat:
        where.append("seat = ?")
        params.append(seat)
    if q and q.strip():
        where.append("(title LIKE ? OR result LIKE ?)")
        like = f"%{q.strip()}%"
        params += [like, like]
    clause = " AND ".join(where)

    total = conn.execute(
        f"SELECT count(*) FROM work_item WHERE {clause}", tuple(params)).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM work_item WHERE {clause} "
        "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset)).fetchall()

    verdicts = _verdicts_for(conn, [int(r["id"]) for r in rows])
    items = []
    for row in rows:
        item_id = int(row["id"])
        verdict = verdicts.get(item_id) or _ungated(row)
        items.append({
            "id": item_id,
            "seat": row["seat"],
            "title": row["title"],
            "status": row["status"],
            "source": row["source"],
            "result": (row["result"] or "")[:_RESULT_PREVIEW],
            "result_len": len(row["result"] or ""),
            "attempts": int(row["attempts"] or 0),
            "closed_by": row["closed_by"] or "",
            "stopped_by": row["stopped_by"] or "",
            "approved_by": row["approved_by"] or "",
            "cost_usd": float(row["total_cost_usd"] or 0),
            "turns": int(row["num_turns"] or 0),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "log_bytes": _log_bytes(root_dir, item_id),
            "verdict": verdict,
        })

    return {
        "ok": True,
        "items": items,
        "page": {"limit": limit, "offset": offset, "total": int(total),
                 "next_offset": (offset + len(items)
                                 if offset + len(items) < total else None)},
        "facets": _facets(conn, gate_runs, seat or "",
                          outcome if outcome in OUTCOMES else "", q or ""),
        "gate": _gate_state(root_dir),
    }


def _facets(conn, gate_runs: bool, seat: str, outcome: str, q: str) -> dict:
    """Counts for the filter controls, over ALL of history rather than the page.

    Each facet excludes ITS OWN filter and honours the others — the standard
    rule, and the one that makes the chips usable: an outcome chip reading 76
    while the search is narrowed to 29 rows is a number about a list nobody is
    looking at, and clicking "failed" from there lands on a count that does not
    match what it promised.
    """
    base = ["status IN ({})".format(", ".join("?" * len(OUTCOMES)))]
    args: list = list(OUTCOMES)
    if not gate_runs:
        base.append("source NOT IN (?, ?)")
        args += [GATE_SOURCE, ESCALATION_SOURCE]
    if q and q.strip():
        base.append("(title LIKE ? OR result LIKE ?)")
        like = f"%{q.strip()}%"
        args += [like, like]

    seat_where, seat_args = list(base), list(args)
    if outcome in OUTCOMES:
        seat_where.append("status = ?")
        seat_args.append(outcome)
    seats = conn.execute(
        f"SELECT seat, count(*) n FROM work_item WHERE {' AND '.join(seat_where)} "
        "GROUP BY seat ORDER BY n DESC", tuple(seat_args)).fetchall()

    out_where, out_args = list(base), list(args)
    if seat:
        out_where.append("seat = ?")
        out_args.append(seat)
    outcomes = conn.execute(
        f"SELECT status, count(*) n FROM work_item WHERE {' AND '.join(out_where)} "
        "GROUP BY status", tuple(out_args)).fetchall()

    return {"seats": [{"seat": r["seat"], "n": int(r["n"])} for r in seats],
            "outcomes": {r["status"]: int(r["n"]) for r in outcomes}}


def _gate_state(root_dir: Path) -> dict:
    """Who is signing off RIGHT NOW, in the gate module's own words.

    The panel prints this verbatim. Under 'none' that sentence is the whole
    honesty story: it tells the reader, before they read a single row, that
    nothing here was independently checked and nothing new will be.
    """
    try:
        from bgate_core import gates as _gates
        state = _gates.state(root_dir)
        return {"mode": state.get("mode"),
                "label": (state.get("labels") or {}).get(state.get("mode"), ""),
                "env_override": state.get("env_override") or ""}
    except Exception:
        return {"mode": "", "label": "", "env_override": ""}


# ---------------------------------------------------------------------------
# The log, windowed
# ---------------------------------------------------------------------------
# One parse per file, cached by (size, mtime) so a reopened log is instant and a
# growing one re-indexes. Small cap: these hold a summary per step and the
# biggest log on this project has 3258 of them.
_INDEX: "OrderedDict[str, dict]" = OrderedDict()
_INDEX_MAX = 3
_INDEX_LOCK = threading.Lock()

# Per-step text kept in the index. Also the searchable span — reported to the
# client as ``text_cap`` so the search box can say what it actually searched
# rather than implying it read the whole 40KB tool result.
TEXT_CAP = 1200

_QUIET_SYSTEM = {"thinking_tokens", "hook_started", "hook_response",
                 "task_started", "task_notification", "task_updated",
                 "background_tasks_changed"}
_STEER_MARKER = "STEER FROM THE DIRECTOR (act on this now): "

# Tools that PRODUCE, and tools that merely LOOK. An agent reads an order of
# magnitude more than it writes, and a list that conflates the two is noise —
# which is the whole reason "what did this run make" was unanswerable from a
# transcript in the first place.
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_READ_TOOLS = {"Read", "Glob", "Grep", "NotebookRead"}

# Bounded and non-backtracking on purpose: this runs over every step of a log
# that can be 60MB, and a naive `.*?\.png` pattern took minutes on one file
# before it was replaced. One character class, one bounded repetition, linear.
_IMG_RE = re.compile(r"[^\s\"'<>|*?,\[\]{}()]{1,200}\.(?:png|jpe?g|webp|svg)",
                     re.IGNORECASE)

# Caps so an index cannot grow without limit on a pathological run.
_MAX_TRACKED_PATHS = 400
_MAX_IMAGE_PATHS = 300
# Only the head of a tool result is scanned for image paths. The capture tools
# put their output path in the first object of their JSON reply; the rest is
# usually a wall of file contents that cannot contain a path we did not already
# see somewhere cheaper.
_RESULT_SCAN = 4000


def _blocks(event: dict) -> list:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in (content or []) if isinstance(b, dict)]


def _step(steps: list, run: int, offset: int, kind: str, **rest) -> None:
    text = str(rest.pop("text", "") or "")
    steps.append({"i": len(steps), "run": run, "off": offset, "kind": kind,
                  "text": text[:TEXT_CAP], "full": len(text), **rest})


def _parse(path: Path) -> dict:
    """Index one log: a compact step per interesting line, plus run boundaries.

    Streamed line by line — the file is opened in binary and never held whole in
    memory, because item-53.log is 60MB and read_text() on it is a 60MB spike
    per request.
    """
    steps: list = [];  run = 0;  finals: dict = {};  offset = 0
    # Collected on the same single pass as the steps, because the alternative is
    # a second walk of the same 60MB to answer "what did this run make".
    wrote: "OrderedDict[str, str]" = OrderedDict()   # raw path -> tool
    read: set = set()
    images: "OrderedDict[str, bool]" = OrderedDict()  # raw path -> seen

    def _note(inp: dict, tool: str) -> None:
        target = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
        if not target or not isinstance(target, str):
            return
        if tool in _WRITE_TOOLS:
            if len(wrote) < _MAX_TRACKED_PATHS:
                wrote.setdefault(target, tool)
        elif tool in _READ_TOOLS and len(read) < _MAX_TRACKED_PATHS:
            read.add(target)

    def _images(text: str) -> None:
        if len(images) >= _MAX_IMAGE_PATHS or not text:
            return
        for hit in _IMG_RE.findall(text[:_RESULT_SCAN]):
            images.setdefault(hit, True)
            if len(images) >= _MAX_IMAGE_PATHS:
                return

    with open(path, "rb") as handle:
        for raw in handle:
            here, offset = offset, offset + len(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "bgate_run_start":
                run += 1
                _step(steps, run, here, "run",
                      text=f"run {run} started",
                      commit=str(event.get("base_commit") or "")[:12])
            elif etype == "assistant":
                for block in _blocks(event):
                    if block.get("type") == "text" and str(block.get("text", "")).strip():
                        _step(steps, run, here, "say", text=str(block["text"]).strip())
                    elif block.get("type") == "tool_use":
                        inp = block.get("input")
                        inp = inp if isinstance(inp, dict) else {}
                        raw_name = str(block.get("name", "?"))
                        name = raw_name.replace("mcp__builders-gate__", "")
                        hint = (inp.get("path") or inp.get("file_path")
                                or inp.get("command") or inp.get("title")
                                or inp.get("query") or inp.get("pattern")
                                or inp.get("prompt") or "")
                        _note(inp, raw_name)
                        _images(str(hint))
                        _step(steps, run, here, "tool", text=str(hint),
                              name=name)
            elif etype == "user":
                for block in _blocks(event):
                    if block.get("type") == "tool_result":
                        body = block.get("content")
                        text = body if isinstance(body, str) else (
                            body[0].get("text", "")
                            if isinstance(body, list) and body
                            and isinstance(body[0], dict) else "")
                        text = str(text).strip()
                        if text:
                            # The capture tools answer with JSON whose first
                            # object carries the file they just wrote. That
                            # reply is the ONLY record linking a shot to the run
                            # that took it — nothing writes it to the database.
                            _images(text)
                            _step(steps, run, here, "result", text=text,
                                  error=bool(block.get("is_error")))
                    elif block.get("type") == "text":
                        text = str(block.get("text", ""))
                        if _STEER_MARKER in text:
                            _step(steps, run, here, "steer",
                                  text=text.split(_STEER_MARKER, 1)[1].strip())
            elif etype == "result":
                _step(steps, run, here, "final",
                      text=str(event.get("result", "")),
                      subtype=str(event.get("subtype") or ""),
                      cost=event.get("total_cost_usd"),
                      turns=event.get("num_turns"))
                finals[run] = steps[-1]["i"]
            elif etype == "system" and event.get("subtype") not in _QUIET_SYSTEM:
                _step(steps, run, here, "sys",
                      text=str(event.get("subtype") or "system"))
            elif etype == "item.completed":
                # codex-shaped runs. Only the completion, so one step per action.
                node = event.get("item") if isinstance(event.get("item"), dict) else {}
                kind = str(node.get("type") or "")
                if kind == "agent_message" and str(node.get("text") or "").strip():
                    _step(steps, run, here, "say", text=str(node["text"]).strip())
                elif kind == "command_execution":
                    _step(steps, run, here, "tool", name="Bash",
                          text=str(node.get("command") or ""))
            elif etype == "thread.started":
                run += 1
                _step(steps, run, here, "run", text=f"run {run} started", commit="")
    if run == 0 and steps:
        for item in steps:
            item["run"] = 1
        run = 1
    return {"steps": steps, "runs": max(run, 0), "finals": finals,
            "wrote": dict(wrote), "read": sorted(read), "images": list(images)}


def _index(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        raise HTTPException(404, f"no agent log for this item at {path.name}")
    sig = (stat.st_size, int(stat.st_mtime))
    key = str(path)
    with _INDEX_LOCK:
        hit = _INDEX.get(key)
        if hit and hit["sig"] == sig:
            _INDEX.move_to_end(key)
            return hit
    built = {"sig": sig, "bytes": stat.st_size, **_parse(path)}
    with _INDEX_LOCK:
        _INDEX[key] = built
        _INDEX.move_to_end(key)
        while len(_INDEX) > _INDEX_MAX:
            _INDEX.popitem(last=False)
    return built


@router.get("/api/history/{item_id}/log")
def history_log(item_id: int, offset: int = Query(-1), limit: int = Query(60, ge=1, le=300),
                run: int = Query(0, ge=0), q: Optional[str] = None) -> dict:
    """One window of a run's transcript, plus the item's brief and result.

    ``offset = -1`` (the default) means the END — which is where the answer is.
    Opening a 3000-step log at step 0 shows the agent reading its own brief.

    ``q`` returns the indices of every matching step so the client can walk
    matches without re-fetching, and windows onto the first one. The search is
    over the first ``text_cap`` characters of each step; that limit is returned
    so the UI can say so instead of implying it searched 60MB of tool output.
    """
    root_dir = root()
    conn = db.connect(root_dir)
    row = conn.execute("SELECT * FROM work_item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no work item {item_id}")
    item = dict(row)

    path = root_dir / ".bgate" / "agents" / f"item-{item_id}.log"
    payload = {
        "ok": True,
        "item": {k: item.get(k) for k in
                 ("id", "seat", "title", "brief", "status", "result", "source",
                  "source_ref", "attempts", "closed_by", "approved_by",
                  "stopped_by", "total_cost_usd", "num_turns", "created_at",
                  "updated_at")},
        # THE VERDICT TRAVELS WITH THE LOG. The drawer used to read it off the
        # list row it was opened from, which is gone the moment the list
        # refreshes or the item is reopened out of history — and the drawer then
        # rendered an empty verdict, which is the one thing this panel must
        # never do.
        "verdict": _verdicts_for(conn, [item_id]).get(item_id) or _ungated(row),
        "text_cap": TEXT_CAP,
    }
    if not path.is_file():
        return {**payload, "steps": [], "total": 0, "runs": 0, "run": 0,
                "offset": 0, "limit": limit, "matches": [], "bytes": 0,
                "note": "no agent log on disk for this item — it was never "
                        "dispatched, or the log was cleaned up"}

    index = _index(path)
    steps = index["steps"]
    if run:
        steps = [s for s in steps if s["run"] == run]

    matches: list[int] = []
    if q and q.strip():
        needle = q.strip().lower()
        matches = [n for n, s in enumerate(steps)
                   if needle in (s["text"] or "").lower()
                   or needle in str(s.get("name") or "").lower()]

    total = len(steps)
    if offset < 0:
        start = matches[0] if matches else max(0, total - limit)
    else:
        start = min(offset, max(0, total - 1))
    window = steps[start:start + limit]
    return {**payload, "steps": window, "total": total, "runs": index["runs"],
            "run": run, "offset": start, "limit": limit, "matches": matches,
            "bytes": index["bytes"]}


@router.get("/api/history/{item_id}/log/step")
def history_log_step(item_id: int, off: int = Query(..., ge=0)) -> dict:
    """One log line in full, by byte offset — what a truncated step expands to.

    Seeks; it does not scan. That is the whole reason the index carries a byte
    offset per step: expanding one 40KB tool result out of a 60MB log must not
    cost a re-read of the log.
    """
    root_dir = root()
    path = root_dir / ".bgate" / "agents" / f"item-{item_id}.log"
    if not path.is_file():
        raise HTTPException(404, "no agent log for this item")
    try:
        with open(path, "rb") as handle:
            handle.seek(off)
            raw = handle.readline()
    except OSError as exc:
        raise HTTPException(500, f"could not read the log: {exc}")
    text = ""
    try:
        event = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        event = None
    if isinstance(event, dict):
        chunks: list[str] = []
        for block in _blocks(event):
            if block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_result":
                body = block.get("content")
                chunks.append(body if isinstance(body, str) else (
                    body[0].get("text", "") if isinstance(body, list) and body
                    and isinstance(body[0], dict) else ""))
            elif block.get("type") == "tool_use":
                chunks.append(json.dumps(block.get("input") or {}, indent=1)[:200_000])
        if event.get("type") == "result":
            chunks.append(str(event.get("result") or ""))
        text = "\n\n".join(c for c in chunks if c)
    if not text:
        text = raw.decode("utf-8", "replace")
    # 200KB is well past what anyone reads and well short of what wedges a tab.
    return {"ok": True, "text": text[:200_000], "truncated": len(text) > 200_000}


# ---------------------------------------------------------------------------
# What the run actually MADE
# ---------------------------------------------------------------------------
# The complaint this answers, verbatim: "on overview agent work specifically
# art, i cant see the work generated". For an art item the deliverable is a
# picture, and the drawer showed the verdict, the prose and the transcript —
# everything except the picture.
#
# FOUR SOURCES, IN DESCENDING ORDER OF TRUST. None of them is invented storage;
# all four already existed and none was being read.
#
#   1. artifact_revision.work_item_id — the registry. Richest when present:
#      logical name, revision, review status, and the art path's machine verdict
#      in metadata.qa_review. Present for 11 items on the reference project and
#      NOT for the one that prompted this, which is why it cannot be the only
#      source.
#   2. bgate_core.writelog — `.bgate/writes/item-<id>.jsonl`, written by the
#      PreToolUse hook. THE HARNESS OBSERVED THESE; the agent does not get a
#      vote. It also already separates the project's own files from Builders
#      Gate's `.bgate/` bookkeeping, which is exactly the produced/incidental
#      split this panel needs and would otherwise have had to guess at.
#   3. The transcript's write-tool calls — the fallback for runs that predate
#      the write log. UNTRUSTED: these strings are agent output, so every one is
#      resolved and contained before it is allowed anywhere near a URL.
#   4. Captures under `.bgate_out/` — screenshots of the running game. Nothing
#      writes these to the database, so the only link back to the run is the
#      capture path appearing in that run's transcript, plus the naming
#      convention (`godot_screenshot` names files after the agent's own label).
#      Both are used and the UI says which one found a given frame.
#
# READS ARE COUNTED, NEVER LISTED. An agent reads an order of magnitude more
# than it writes and a combined list is noise — "distinguish produced from
# merely touched" is the whole point.
_CAPTURE_DIRS = (".bgate_out/shots", ".bgate_out/art", ".bgate_out/renders")
_CAPTURE_SCAN_CAP = 4000        # dir entries walked before giving up
_CAPTURE_HIT_CAP = 48
_PRODUCED_CAP = 200
_PREVIEWABLE = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}


def _work_base(root_dir: Path, item_id: int) -> Path:
    """Where this run's files live: its worktree if it had one, else the project.

    Mirrors app._peek_base deliberately — /api/preview resolves the same way for
    the same ``item_id``, so a rel this module hands out is a rel that endpoint
    can serve. Getting these two out of step is how thumbnails 404'd for
    worktree files the last time.
    """
    try:
        worktree = str(_queue_get(root_dir, item_id).get("worktree") or "")
    except Exception:
        worktree = ""
    if worktree and Path(worktree).is_dir():
        return Path(worktree).resolve()
    return root_dir.resolve()


def _queue_get(root_dir: Path, item_id: int) -> dict:
    from bgate_core import queue as _queue
    return _queue.get(root_dir, item_id)


def _contained(base: Path, raw: str) -> Optional[str]:
    """A project-relative rel for an UNTRUSTED path, or None. Never raises.

    Everything reaching this comes out of an agent transcript, so it is treated
    as hostile input: length-capped, resolved, and required to still be inside
    the base afterwards. `relative_to` AFTER `resolve()` on both sides is the
    check /api/preview and deps.safe_under already use; anything that escapes
    returns None and is dropped rather than reported, because a path this
    refuses is not something a reviewer should even see named.
    """
    text = str(raw or "").strip().strip("\"'`,;")
    if not text or len(text) > 400:
        return None
    try:
        supplied = Path(text)
        target = (supplied if supplied.is_absolute() else base / supplied).resolve()
        target.relative_to(base)
    except (ValueError, OSError):
        return None
    if not target.is_file():
        return None
    return str(target.relative_to(base)).replace("\\", "/")


def _file_row(base: Path, rel: str, origin: str, **extra) -> dict:
    absolute = base / rel
    try:
        size = absolute.stat().st_size
    except OSError:
        size = 0
    suffix = absolute.suffix.lower()
    return {"rel": rel, "name": absolute.name, "ext": suffix.lstrip("."),
            "bytes": size, "image": suffix in _PREVIEWABLE,
            "origin": origin, **extra}


def _registered(root_dir: Path, base: Path, item_id: int) -> list[dict]:
    """Artifacts the run registered, with their review state and QA verdict."""
    from bgate_core import artifacts as _artifacts
    try:
        # Queried by work_item_id rather than listed and filtered: list_revisions
        # takes no such filter, and pulling 500 rows to keep three is a waste
        # that grows with the project.
        rows = [_artifacts._decode(dict(r)) for r in db.connect(root_dir).execute(
            "SELECT * FROM artifact_revision WHERE work_item_id = ? "
            "ORDER BY revision, id", (item_id,)).fetchall()]
    except Exception:
        return []
    out = []
    for art in rows:
        meta = art.get("metadata") or {}
        # The per-revision archived preview, not the logical path: every
        # generation overwrites <name>_sheet.png, so the live path shows the
        # NEWEST render for every revision. seats/art.js learned this the hard
        # way and its reasoning is quoted here so the two cannot drift.
        rel = _contained(base, meta.get("preview") or "") \
            or _contained(base, art.get("path") or "")
        if not rel:
            continue
        review = meta.get("qa_review") or {}
        out.append(_file_row(
            base, rel, "artifact",
            artifact_id=art.get("id"), logical_name=art.get("logical_name"),
            revision=art.get("revision"), status=art.get("status"),
            producer=art.get("producer") or "", model=art.get("model") or "",
            qa=({"verdict": review.get("verdict"), "score": review.get("score"),
                 "note": str(review.get("note") or "")[:300]} if review else None)))
    return out


def _observed(root_dir: Path, base: Path, item_id: int) -> tuple[list, list, int]:
    """The harness's own write record. Returns (project rows, harness rels, gone)."""
    try:
        from bgate_core import writelog
        entries = writelog.entries(root_dir, f"item-{item_id}")
    except Exception:
        return [], [], 0
    project, harness, gone = [], [], 0
    seen: set = set()
    for entry in entries:
        raw = str(entry.get("path") or "")
        if not raw or raw in seen:
            continue
        seen.add(raw)
        if raw.startswith(".bgate/"):
            harness.append(raw)
            continue
        rel = _contained(base, raw)
        if rel is None:
            # Recorded as written and not there now: deleted, moved, or reverted.
            # Counted rather than listed — a dead path is not something to click.
            gone += 1
            continue
        project.append(_file_row(base, rel, "harness",
                                 tool=str(entry.get("tool") or ""),
                                 at=str(entry.get("t") or "")))
    return project, harness, gone


def _stamp(text: str) -> float:
    """A sqlite `YYYY-MM-DD HH:MM:SS` (UTC) as an epoch float, or 0."""
    from datetime import datetime, timezone
    try:
        parsed = datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
        return parsed.replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


# Slack around the run's recorded span. `created_at` is when the row was filed,
# which can be well before dispatch, and `updated_at` is stamped at close — but
# a capture written in the same second as the close must not fall outside by a
# rounding error.
_WINDOW_BEFORE = 120
_WINDOW_AFTER = 600


def _captures(root_dir: Path, base: Path, item_id: int, from_log: list[str],
              window: tuple[float, float]) -> list[dict]:
    """Frames this run RENDERED — not the reference art it looked at.

    THE BUG THIS SHAPE EXISTS TO AVOID, found on item #300: scanning the
    transcript for image paths finds every frame the agent READ as well as
    every frame it made, and #300's drawer duly offered another item's backdrop
    studies as its own output. A transcript mention proves contact, not
    authorship.

    So a frame has to clear one of two independent tests, and the UI says which:

      by name  — `godot_screenshot` names its file after the agent's label and
                 the seats tag it with the item (`item334_b_tut_carry`). The
                 digits carry a negative lookahead so item 33 cannot claim item
                 334's captures.
      by clock — the path is in this run's transcript AND the file was written
                 inside the run's own span. A reference image predates the run
                 by definition; a frame the run rendered cannot.
    """
    start, end = window
    found: "OrderedDict[str, dict]" = OrderedDict()
    # `item334_b_tut_carry` (the godot_screenshot convention) and `i300_ai_
    # coworker_A` (the art seat's) are both real on this project. The leading
    # boundary stops `hi300` matching and the trailing lookahead stops item 33
    # claiming item 334's frames — the collision a bare substring test gets
    # wrong, and the reason this is a compiled pattern rather than an `in`.
    tag = re.compile(rf"(?<![A-Za-z0-9])(?:item[-_]?|i)0*{int(item_id)}(?!\d)",
                     re.IGNORECASE)

    def _in_window(path: Path) -> bool:
        if not (start or end):
            return False        # no usable span: fall back to the name test only
        try:
            written = path.stat().st_mtime
        except OSError:
            return False
        return (start - _WINDOW_BEFORE) <= written <= (end + _WINDOW_AFTER)

    for raw in from_log:
        rel = _contained(base, raw)
        if not rel or not rel.startswith(".bgate_out/"):
            continue
        named = bool(tag.search(Path(rel).name))
        if not named and not _in_window(base / rel):
            continue            # the run read this frame, it did not make it
        found.setdefault(rel, _file_row(base, rel, "capture", in_log=True,
                                        by_name=named))

    scanned = 0
    for folder in _CAPTURE_DIRS:
        directory = base / folder
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            scanned += 1
            if scanned > _CAPTURE_SCAN_CAP or len(found) >= _CAPTURE_HIT_CAP:
                break
            if path.suffix.lower() not in _PREVIEWABLE or not path.is_file():
                continue
            if not tag.search(path.name):
                continue
            rel = _contained(base, str(path))
            if rel and rel not in found:
                found[rel] = _file_row(base, rel, "capture", in_log=False,
                                       by_name=True)
        if scanned > _CAPTURE_SCAN_CAP or len(found) >= _CAPTURE_HIT_CAP:
            break
    # Name-tagged frames lead: those are linked to this item explicitly, while
    # the clock-matched ones only share a span with it. Strongest evidence at
    # the top is the same ordering rule the verdict column uses.
    return sorted(found.values(),
                  key=lambda row: (not row["by_name"], row["name"]))


@router.get("/api/history/{item_id}/work")
def history_work(item_id: int) -> dict:
    """The artifacts a work item produced, as artifacts rather than as prose.

    Resolved when a row is OPENED, never in the list payload: this touches the
    filesystem once per file and walks a capture directory, which is fine for
    one item and would be four hundred stat calls on a page of forty.
    """
    root_dir = root()
    row = db.connect(root_dir).execute(
        "SELECT id, seat, status, base_commit, worktree, created_at, updated_at "
        "FROM work_item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no work item {item_id}")
    base = _work_base(root_dir, item_id)
    window = (_stamp(row["created_at"]), _stamp(row["updated_at"]))

    produced: "OrderedDict[str, dict]" = OrderedDict()
    for art in _registered(root_dir, base, item_id):
        produced[art["rel"]] = art
    observed, harness, gone = _observed(root_dir, base, item_id)
    for entry in observed:
        # A registered artifact is the richer record of the same file; the
        # write-log row must not overwrite its review state.
        produced.setdefault(entry["rel"], entry)

    log_images: list[str] = []
    reads = 0
    log_path = root_dir / ".bgate" / "agents" / f"item-{item_id}.log"
    if log_path.is_file():
        try:
            index = _index(log_path)
        except HTTPException:
            index = {}
        log_images = list(index.get("images") or [])
        reads = len(index.get("read") or [])
        for raw, tool in (index.get("wrote") or {}).items():
            if len(produced) >= _PRODUCED_CAP:
                break
            rel = _contained(base, raw)
            if rel and rel not in produced and not rel.startswith(".bgate/"):
                produced[rel] = _file_row(base, rel, "transcript", tool=tool)

    captures = _captures(root_dir, base, item_id, log_images, window)
    rows = list(produced.values())[:_PRODUCED_CAP]
    # Images first: on an art item they are the answer, and making a reviewer
    # scroll past seven .tscn rows to reach the render is the same failure in a
    # smaller form.
    rows.sort(key=lambda entry: (not entry["image"], entry["rel"]))

    return {
        "ok": True,
        "item_id": item_id,
        "seat": row["seat"],
        # /api/preview resolves the same base for this id, so the client passes
        # it straight through rather than guessing at the worktree.
        "preview_item_id": item_id if str(row["worktree"] or "") else 0,
        "produced": rows,
        "captures": captures,
        "harness": {"count": len(harness), "paths": harness[:20]},
        "read_only": {"count": reads},
        "missing": gone,
        "counts": {
            "artifacts": sum(1 for r in rows if r["origin"] == "artifact"),
            "observed": sum(1 for r in rows if r["origin"] == "harness"),
            "transcript": sum(1 for r in rows if r["origin"] == "transcript"),
            "images": sum(1 for r in rows if r["image"]),
        },
        # The code payload is a diff, and computing one costs a git process —
        # so this only says whether there IS one. The client fetches
        # /api/queue/{id}/diff when the reader asks for it.
        "diff": {"available": bool(row["base_commit"]),
                 "base": str(row["base_commit"] or "")[:12]},
    }


# Exposed for tests: clearing the cache is the only way to assert a re-index.
def _reset_cache() -> None:
    with _INDEX_LOCK:
        _INDEX.clear()


__all__ = ["router", "OUTCOMES", "TEXT_CAP"]
