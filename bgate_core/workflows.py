"""Workflow runs — the node graph stops being a drawing and starts being work.

The builder let a non-engineer wire a process together and press Run, and Run
was fire-and-forget: one director item carrying a prose plan. Nothing persisted,
nothing reported, and the canvas never learned what happened. Worse, the steps
drawn as HARD GATES enforced nothing — a "review gate" with no approval surface,
a "consistency check" whose threshold was interpolated into a paragraph and then
ignored. A gate that does not gate is a lie the tool tells its user.

This module is the run engine. A run is:

  * a persisted snapshot of the graph (``workflow_run.graph_json``) plus one
    ``workflow_run_node`` row per step, so a page reload — or a different
    machine — can repaint exactly where the run got to;
  * driven forward one step at a time by :func:`advance`, which the dashboard
    ticks while it polls. No daemon: the run's state lives in SQLite, so a tick
    is idempotent and a crash loses nothing;
  * agent steps become ONE queue item each, carrying ``source_ref =
    run:<run_id>:<node_id>``, which is the thread we pull to map an item's
    outcome back onto a node;
  * gate steps genuinely BLOCK. The node sits at 'running' until a human calls
    :func:`approve`. Agents are refused by identity, here and at the route —
    a gate a robot can open is not a gate;
  * consistency steps genuinely EVALUATE. When the QA item finishes we collect
    the real scores (recorded verdicts on the artifacts the upstream step
    produced, or a score posted straight at the node) and compare them to the
    node's threshold. Below it, the node fails and the run fails. With no
    evidence at all it also fails: a check that cannot see anything must not
    certify anything.

WHAT RUNS IN PARALLEL, AND WHY ONLY THAT
----------------------------------------
:func:`advance` is sequential for agent work and deliberately so: an agent step
is a Claude session with write access to the game repo, and two of them editing
the same scene is last-write-wins. One queue item in flight, always.

A ``generate`` node is not that. It calls an image provider directly — no
session, no repo write — and everything it touches is new: fresh candidate files
under ``.bgate_out/art/workflow/`` and fresh artifact rows. Nothing it does can
collide with a sibling. And the entire point of the node is the comparison: the
same prompt into three models AT ONCE, because a comparison you have to wait
ninety seconds between arms of is one nobody runs.

So the rule is split by kind, not by graph shape:

  * generate nodes start as soon as THEIR OWN inputs are satisfied — siblings
    run concurrently, bounded by the budget's ``max_concurrent``;
  * agent and consistency steps still take the line one at a time, and a gate
    or a pick still stops everything behind it.

Generation runs on a worker thread and the node is claimed ('pending' ->
'running' under a conditional UPDATE) before the thread starts, so a second
poll — or a page reload mid-generation — sees work already in flight rather
than starting it twice.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait as _wait
from typing import Any, Iterable, Optional

from . import (activity, artifacts as _artifacts, db, generate as _generate,
               queue as _queue, spend as _spend, wfnodes as _wfnodes)
from .util import rows

RUN_STATUSES = ("running", "passed", "failed", "cancelled")
NODE_STATUSES = ("pending", "queued", "running", "passed", "failed", "skipped")
# 'tool' is the generic executor added in bgate_core.wfnodes: a node that calls
# ONE named MCP tool with arguments mapped off its card and its wires. It is a
# kind of its own rather than another _GENERATE_TYPES entry because a tool node
# can legitimately be exclusive — a Godot or Blender node drives the game repo,
# and two of those at once is last-write-wins — while a generate node never is.
KINDS = ("agent", "gate", "consistency", "passive", "generate", "pick", "tool")

# Agents stamp this prefix into BGATE_ACTOR; see bgate_ui.api.current_actor.
AGENT_ACTOR_PREFIX = "agent:"
DEFAULT_THRESHOLD = 80

# Steps that are a human decision point regardless of what the palette calls
# them. A gate promised a pause and delivered none.
_GATE_TYPES = {"control.gate"}
# 'control.select' ("human picks the best variant") blocked like a gate but
# resolved to nothing — approving it told the run a human was happy and told the
# next step nothing about WHICH candidate. It is a pick: same block, real output.
_PICK_TYPES = {"control.select", "control.pick"}
_CONSISTENCY_TYPES = {"control.consistency"}
# Nodes that call a provider themselves. The palette's art.* steps stay agent
# steps (they carry a seat); these are the model-is-the-node types.
#
# ONLY 'model.image' IS DRAWN TODAY (wf_steps_model.js is the only file that
# emits one). The other two are kept as LEGACY names because a run stores a
# SNAPSHOT of the graph and a saved workflow is JSON on disk: drop a type from
# this set and a node that used to generate falls through kind_for to 'passive',
# which paints it green without generating anything — the silent success this
# engine exists to remove. 'model.generate' is the pre-rename name and is still
# what this module's own tests draw with; 'llm.prompt' was a real card, removed
# from the palette on purpose (the note in wf_steps_model.js says why) but not
# removable from graphs already saved.
#
# 'image.generate' and 'gen.image' were dropped: no palette, template, test,
# doc or fixture in this repository has ever emitted either name, so they were
# guarding nothing. The tool-node table's 'tool.image.generate' is a different
# type entirely and is unaffected.
_GENERATE_TYPES = {"model.image", "model.generate", "llm.prompt"}
# Passive inputs whose config text IS their output — the head of a prompt wire.
_TEXT_TYPES = {"input.task", "input.text", "input.prompt"}
# World context nodes: the bible and the lore graph, on a prompt wire.
_CONTEXT_TYPES = {"input.bible", "input.lore"}

# Kinds a human can run one at a time from the canvas. A gate/pick is resolved,
# not run; that is a different verb with a different guard.
RUNNABLE_KINDS = ("generate", "agent", "consistency", "passive", "tool")

# Concurrency ceiling for generate fan-out when the budget states none.
DEFAULT_MAX_CONCURRENT = 4

# In-flight generate workers, keyed (run_id, node_id). Only used to make the
# fan-out joinable — the DB, not this dict, is the source of truth for status,
# so losing it (a restart) costs nothing but the ability to wait.
_INFLIGHT: dict[tuple[int, str], Future] = {}
_INFLIGHT_LOCK = threading.Lock()
_POOL: Optional[ThreadPoolExecutor] = None


# ---------------------------------------------------------------------------
# Graph normalisation
# ---------------------------------------------------------------------------

def kind_for(spec: dict) -> str:
    """What a node MEANS to the engine, independent of what it calls itself.

    TYPE WINS over the client's declared kind for anything that blocks. The
    client's step registry is the nicer source of truth for behaviour, but it
    lives in the browser: a graph POSTed by hand could declare a
    ``control.select`` to be 'passive' and walk straight through the human's
    decision. So the blocking types are derived here first and the declared
    kind only fills in what the type leaves open.
    """
    node_type = str(spec.get("type") or "")
    # The tool table first. A tool node is derived here, not trusted from the
    # client, for the same reason a gate is: the registry says which tool a type
    # calls and whether that tool touches the game repo, and a graph POSTed by
    # hand must not be able to relabel a Godot write as a 'passive' step that
    # walks straight past the single-file rule.
    if _wfnodes.is_tool_node(node_type):
        return "tool"
    if _wfnodes.is_flow_node(node_type):
        # Glue is passive: it calls nothing, costs nothing and finishes inline.
        return "passive"
    if node_type in _GATE_TYPES:
        return "gate"
    if node_type in _PICK_TYPES:
        return "pick"
    if node_type in _CONSISTENCY_TYPES:
        return "consistency"
    if node_type in _GENERATE_TYPES:
        return "generate"
    declared = str(spec.get("kind") or "").strip()
    if declared in KINDS:
        return declared
    if str(spec.get("seat") or "").strip():
        return "agent"
    return "passive"


def _spec(node: dict) -> dict:
    """One node of the compiled plan the client hands us.

    The step *behaviour* (seat, brief text) is authored in the JS step registry,
    so the client compiles it and we snapshot the result. That snapshot is the
    record of what this run was actually told to do — briefs edited afterwards
    do not rewrite history.
    """
    node_id = str(node.get("id") or "").strip()
    if not node_id:
        raise ValueError("every workflow node needs an id")
    spec = {
        "id": node_id,
        "type": str(node.get("type") or "")[:120],
        "label": str(node.get("label") or node.get("title") or node.get("type") or node_id)[:120],
        "seat": str(node.get("seat") or "").strip(),
        "brief": str(node.get("brief") or ""),
        "config": node.get("config") if isinstance(node.get("config"), dict) else {},
    }
    spec["kind"] = kind_for(dict(node, **spec))
    if spec["kind"] == "consistency" and not spec["seat"]:
        spec["seat"] = "qa"  # the check is run by QA, never by the artist
    return spec


def _edge_pair(edge: Any, known: Iterable[str]) -> Optional[tuple[str, str]]:
    """(from_node, to_node) for an edge the canvas drew, or None if it dangles."""
    known = set(known)
    try:
        src, dst = edge["from"], edge["to"]
        a = src[0] if isinstance(src, (list, tuple)) else src
        b = dst[0] if isinstance(dst, (list, tuple)) else dst
    except (KeyError, TypeError, IndexError):
        return None
    a, b = str(a), str(b)
    if a in known and b in known and a != b:
        return (a, b)
    return None


def _topo(node_ids: list[str], edges: list[dict]) -> list[str]:
    """Kahn order. Anything left over (a cycle) is appended so it still gets a
    row — :func:`advance` fails such a run honestly instead of hanging."""
    indeg = {i: 0 for i in node_ids}
    adj: dict[str, list[str]] = {i: [] for i in node_ids}
    for edge in edges:
        pair = _edge_pair(edge, node_ids)
        if pair:
            adj[pair[0]].append(pair[1])
            indeg[pair[1]] += 1
    queue_ = [i for i in node_ids if not indeg[i]]
    out: list[str] = []
    while queue_:
        cur = queue_.pop(0)
        out.append(cur)
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue_.append(nxt)
    out.extend(i for i in node_ids if i not in out)
    return out


def _cycle_nodes(node_ids: list[str], edges: list[dict]) -> set[str]:
    """Nodes Kahn could never reach — i.e. the ones inside (or fed by) a cycle.

    :func:`advance` used to infer a cycle from "a parent that is not finished
    yet", which only held while exactly one node could be in flight. With
    generate siblings running concurrently an unfinished parent is usually just
    a parent still working, so the cycle test has to be structural.
    """
    indeg = {i: 0 for i in node_ids}
    adj: dict[str, list[str]] = {i: [] for i in node_ids}
    for edge in edges:
        pair = _edge_pair(edge, node_ids)
        if pair:
            adj[pair[0]].append(pair[1])
            indeg[pair[1]] += 1
    queue_ = [i for i in node_ids if not indeg[i]]
    settled: set[str] = set()
    while queue_:
        cur = queue_.pop(0)
        settled.add(cur)
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue_.append(nxt)
    return set(node_ids) - settled


def _upstream(edges: list[dict], node_ids: Iterable[str]) -> dict[str, list[str]]:
    ups: dict[str, list[str]] = {i: [] for i in node_ids}
    for edge in edges:
        pair = _edge_pair(edge, ups)
        if pair:
            ups[pair[1]].append(pair[0])
    return ups


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def _run_row(root: str | os.PathLike[str], run_id: int) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM workflow_run WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise LookupError(f"no workflow run {run_id}")
    return dict(row)


def _node_rows(root: str | os.PathLike[str], run_id: int) -> dict[str, dict]:
    return {r["node_id"]: r for r in rows(db.connect(root).execute(
        "SELECT * FROM workflow_run_node WHERE run_id = ? ORDER BY id", (run_id,)))}


def _info(row: dict) -> dict:
    """``detail`` carries a JSON blob: a human message plus whatever evidence the
    step produced (score, threshold, approver). Old/plain text degrades to the
    message."""
    raw = row.get("detail") or ""
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {"message": str(loaded)}
    except (TypeError, ValueError):
        return {"message": raw}


def _set_node(root: str | os.PathLike[str], run_id: int, node_id: str,
              status: str, *, message: str = "", work_item_id: Optional[int] = None,
              info: Optional[dict] = None, merge: bool = True) -> dict:
    if status not in NODE_STATUSES:
        raise ValueError(f"node status must be one of {NODE_STATUSES}")
    current = _node_rows(root, run_id).get(node_id)
    if current is None:
        raise LookupError(f"run {run_id} has no node {node_id!r}")
    blob = dict(_info(current)) if merge else {}
    if message:
        blob["message"] = message
    if info:
        blob.update(info)
    sets = ["status = ?", "detail = ?", "updated_at = datetime('now')"]
    params: list = [status, json.dumps(blob)]
    if work_item_id is not None:
        sets.insert(1, "work_item_id = ?")
        params.insert(1, int(work_item_id))
    params.extend([run_id, node_id])
    with db.tx(root) as conn:
        conn.execute(
            f"UPDATE workflow_run_node SET {', '.join(sets)} "
            "WHERE run_id = ? AND node_id = ?", params)
    return _node_rows(root, run_id)[node_id]


def _output(row: dict) -> dict:
    """What this node PRODUCED — text, candidate artifacts, a human's choice.

    Separate from ``detail`` on purpose: detail is the story of the node for a
    human to read, output is the value the next node consumes. Mixing them
    means a message edit changes a downstream input.
    """
    raw = (row or {}).get("output_json") or ""
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except (TypeError, ValueError):
        return {}


def _set_output(root: str | os.PathLike[str], run_id: int, node_id: str,
                output: dict) -> None:
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE workflow_run_node SET output_json = ?, "
            "updated_at = datetime('now') WHERE run_id = ? AND node_id = ?",
            (json.dumps(output), run_id, node_id))


def _claim(root: str | os.PathLike[str], run_id: int, node_id: str,
           *, was: str = "pending", now: str = "running") -> bool:
    """Take a node from ``was`` to ``now``, once. True only for the winner.

    The conditional UPDATE is the whole guarantee: two overlapping polls (or a
    poll racing a per-node Run) both read 'pending', and without this both
    would spend money generating the same node twice.
    """
    with db.tx(root) as conn:
        cur = conn.execute(
            "UPDATE workflow_run_node SET status = ?, updated_at = datetime('now') "
            "WHERE run_id = ? AND node_id = ? AND status = ?",
            (now, run_id, node_id, was))
        return bool(cur.rowcount)


def _set_run(root: str | os.PathLike[str], run_id: int, status: str) -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f"run status must be one of {RUN_STATUSES}")
    with db.tx(root) as conn:
        conn.execute("UPDATE workflow_run SET status = ?, "
                     "updated_at = datetime('now') WHERE id = ?", (status, run_id))


# ---------------------------------------------------------------------------
# Starting a run
# ---------------------------------------------------------------------------

def start(root: str | os.PathLike[str], graph: dict, *, name: str = "",
          seat: str = "", actor: str = "", dispatch: bool = False) -> dict:
    """Persist a run of ``graph`` and take the first step.

    ``graph`` is the compiled plan: ``{workflow:{id,name,category}, nodes:[
    {id,type,label,seat,brief,config}], edges:[{from:[n,p],to:[n,p]}]}``.
    """
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    if not nodes:
        raise ValueError("a workflow run needs at least one step")
    specs: dict[str, dict] = {}
    for node in nodes:
        spec = _spec(node)
        specs[spec["id"]] = spec
    edges = [e for e in (graph.get("edges") or []) if _edge_pair(e, specs)]
    order = _topo(list(specs), edges)
    if not any(specs[i]["kind"] in ("agent", "consistency", "generate", "tool")
               for i in order):
        raise ValueError(
            "this workflow has no step that does anything — add a generate, "
            "tool or agent step. Inputs and glue nodes carry values; something "
            "has to consume them.")

    workflow = graph.get("workflow") if isinstance(graph.get("workflow"), dict) else {}
    run_name = (name or workflow.get("name") or "workflow").strip()[:120]
    snapshot = {
        "workflow": workflow,
        "nodes": [specs[i] for i in order],
        "edges": [{"from": list(e["from"]), "to": list(e["to"])} for e in edges],
        "order": order,
        "options": {"dispatch": bool(dispatch)},
    }
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO workflow_run (name, seat, graph_json, status, actor) "
            "VALUES (?, ?, ?, 'running', ?)",
            (run_name, seat, json.dumps(snapshot), actor))
        run_id = int(cur.lastrowid)
        for node_id in order:
            spec = specs[node_id]
            conn.execute(
                "INSERT INTO workflow_run_node (run_id, node_id, kind, label, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (run_id, node_id, spec["kind"], spec["label"]))
    activity.log(root, "workflow", f"run {run_id} started: {run_name} ({len(order)} steps)",
                 ref=str(run_id))
    return advance(root, run_id)


# ---------------------------------------------------------------------------
# Driving a run
# ---------------------------------------------------------------------------

def _task_text(snapshot: dict) -> str:
    for node in snapshot.get("nodes") or []:
        if node.get("type") == "input.task":
            text = str((node.get("config") or {}).get("text") or "").strip()
            if text:
                return text
    return ""


def _wire_context(inputs: Optional[dict]) -> str:
    """What the upstream nodes handed this step, spelled out in the brief.

    A seat cannot open the run's database rows; if a human picked candidate #7
    the only way the agent learns that is by being told, in words, in the brief.
    """
    inputs = inputs or {}
    lines = []
    for chosen in inputs.get("picked") or []:
        lines.append(f"- SELECTED by a human: {chosen.get('path') or ''} "
                     f"(artifact #{chosen.get('artifact_id')}"
                     + (f", {chosen['model']}" if chosen.get("model") else "") + ")")
    for cand in (inputs.get("candidates") or [])[:12]:
        lines.append(f"- candidate artifact #{cand.get('artifact_id')}: "
                     f"{cand.get('path') or ''}"
                     + (f" ({cand['model']})" if cand.get("model") else ""))
    if not lines:
        return ""
    return "\n\nINPUTS FROM THE PREVIOUS STEPS:\n" + "\n".join(lines)


def _step_brief(run: dict, snapshot: dict, spec: dict, position: int,
                total: int, inputs: Optional[dict] = None) -> str:
    """The brief the seat actually receives — the step's own brief plus the run
    context, so a seat knows which run it is inside and can be steered back to
    it. Without the run_id an agent's work is unattributable to the graph."""
    task = _task_text(snapshot)
    head = (f"Workflow run #{run['id']} — \"{run['name']}\", "
            f"step {position} of {total}: {spec['label']}.")
    body = spec.get("brief") or f"Carry out the {spec['label']} step of this workflow."
    tail = ("\n\nThis step is one node of a persisted workflow run; the run is "
            "blocked on it. Finish with queue_complete and a summary of what "
            "this step produced — the run advances on that.")
    wire = _wire_context(inputs)
    if task:
        return (f"{head}\n\nTASK / COMPLAINT (the run's north star):\n\"{task}\"\n\n"
                f"{body}{wire}{tail}")
    return f"{head}\n\n{body}{wire}{tail}"


def _queue_step(root: str | os.PathLike[str], run: dict, snapshot: dict,
                spec: dict, position: int, total: int, dispatch: bool,
                inputs: Optional[dict] = None) -> dict:
    """One agent step -> one queue item, tagged so we can find it again."""
    seat = spec.get("seat") or ""
    run_id = int(run["id"])
    try:
        item = _queue.add(
            root, seat,
            title=f"{run['name']}: {spec['label']}"[:80],
            brief=_step_brief(run, snapshot, spec, position, total, inputs),
            priority=4, source="workflow",
            source_ref=f"run:{run_id}:{spec['id']}")
    except ValueError as exc:
        # An unrunnable step (unknown seat) must fail the node loudly, not be
        # silently skipped — silent skipping is how the old Run lied.
        return _set_node(root, run_id, spec["id"], "failed",
                         message=f"cannot queue this step: {exc}")
    node = _set_node(root, run_id, spec["id"], "queued",
                     work_item_id=item["id"],
                     message=f"queued as work item #{item['id']} for the {seat} seat",
                     info={"seat": seat})
    if dispatch:
        try:
            from bgate_ui import dispatch as _dispatch  # lazy: core must not need the UI

            result = _dispatch.dispatch(str(root), int(item["id"]))
            if not result.get("ok"):
                node = _set_node(root, run_id, spec["id"], "queued",
                                 message=f"queued as work item #{item['id']} — "
                                         f"not auto-dispatched: {result.get('error')}")
        except Exception as exc:  # dispatch is optional; a run must not die with it
            node = _set_node(root, run_id, spec["id"], "queued",
                             message=f"queued as work item #{item['id']} — "
                                     f"dispatch unavailable ({type(exc).__name__})")
    return node


# ---------------------------------------------------------------------------
# Data on a wire
# ---------------------------------------------------------------------------

def _inputs(root: str | os.PathLike[str], run_id: int, node_id: str,
            ups: dict[str, list[str]], node_rows: dict[str, dict]) -> dict:
    """What this node's DIRECT parents produced.

    Direct parents only, deliberately: an edge is the user saying "this feeds
    that", and inheriting a grandparent's output through a node that chose not
    to pass it on would make the wire a lie.

      * ``text``    — the first upstream text output (a task node's text, or an
                      agent step's result: the "LLM writes the prompt" step);
      * ``candidates`` — every candidate a parent generate node registered,
                      which is exactly what a pick chooses between;
      * ``picked``  — artifacts a parent pick node resolved to, which a
                      downstream generate uses as its style reference;
      * ``refs``    — pinned anchors a parent Reference node names. These are
                      the style references the user WIRED IN, and leaving them
                      out made that wire decorative;
      * ``paths``   — raw files a parent tool node produced that were never
                      registered as artifacts (a screenshot, a .glb, an ffmpeg
                      cut). Without this a tool that makes a FILE could not feed
                      a tool that takes one, which is most of the engine side;
      * ``data``    — a parent tool node's structured payload (a level plan, a
                      scene outline), so a step can be driven by what the
                      previous step LEARNED and not only by what it made.
    """
    text = ""
    candidates: list[dict] = []
    picked: list[dict] = []
    refs: list[dict] = []
    paths: list[str] = []
    data: list[Any] = []
    for parent in ups.get(node_id, ()):
        out = _output(node_rows.get(parent) or {})
        ref = out.get("ref")
        if isinstance(ref, dict) and ref.get("path"):
            refs.append(dict(ref, node_id=parent))
        if not text and str(out.get("text") or "").strip():
            text = str(out["text"]).strip()
        for cand in out.get("artifacts") or []:
            if isinstance(cand, dict) and cand.get("artifact_id"):
                candidates.append(dict(cand, node_id=parent))
        chosen = out.get("picked")
        if isinstance(chosen, dict) and chosen.get("artifact_id"):
            picked.append(dict(chosen, node_id=parent))
        for path in out.get("paths") or []:
            if isinstance(path, str) and path.strip() and path not in paths:
                paths.append(path)
        if out.get("data") is not None:
            data.append(out["data"])
    # `paths` is deliberately NOT folded into `candidates`. A candidate is a
    # thing a human can pick, and `pick()` resolves a choice by artifact id — an
    # entry with no id in that list would make the picker offer an option the
    # engine then refuses. Tool nodes read `paths` directly.
    return {"text": text, "candidates": candidates, "picked": picked,
            "refs": refs, "paths": paths, "data": data}


def _prompt_for(spec: dict, inputs: dict) -> str:
    """The prompt a generate node actually sends.

    Upstream text wins over the node's own config — the wire is the point of
    the feature. ``config.prompt`` with an ``{input}`` placeholder composes the
    two instead of choosing, which is how a fixed style suffix survives an
    LLM-authored subject.
    """
    text = str(inputs.get("text") or "").strip()
    template = str((spec.get("config") or {}).get("prompt") or "").strip()
    if template and "{input}" in template:
        return template.replace("{input}", text).strip()
    if text:
        return text
    return template or str(spec.get("brief") or "").strip()


def _style_refs_for(root: str | os.PathLike[str], spec: dict,
                    inputs: dict) -> list[tuple[str, float]]:
    """Anchors this generation conditions on: whatever an upstream pick chose,
    plus any paths pinned on the node itself."""
    config = spec.get("config") or {}
    try:
        strength = float(config.get("ref_strength", 0.5))
    except (TypeError, ValueError):
        strength = 0.5
    out: list[tuple[str, float]] = []
    # What the user wired in comes FIRST — an explicit anchor outranks anything
    # inherited, and the providers cap how many references they accept.
    for wired in inputs.get("refs") or []:
        path = str(wired.get("path") or "")
        if path:
            out.append((path, float(wired.get("strength", strength))))
    for chosen in inputs.get("picked") or []:
        path = str(chosen.get("path") or "")
        if path:
            out.append((str(os.path.join(str(root), path)), strength))
    for entry in config.get("style_refs") or []:
        if isinstance(entry, dict):
            path, ref_strength = entry.get("path"), entry.get("strength", strength)
        else:
            path, ref_strength = entry, strength
        if path:
            out.append((str(os.path.join(str(root), str(path))), float(ref_strength)))
    return out


def _context_output(root: str | os.PathLike[str], spec: dict) -> dict:
    """World-bible and lore context, as text on a wire.

    The bible's locked art direction is appended to every generation already
    (bgate_core.artdirection), but that is a floor, not a way to SAY THINGS. A
    workflow needs to put specific world context into a specific step: this
    entity's canon facts, that pillar, the tone guide — chosen on the canvas,
    resolved at run time so editing the bible changes the run instead of baking
    a copy into the graph.

    Output is `{"text": ...}` so it rides the existing prompt wire: anything
    that accepts a prompt accepts world context, with no new plumbing.
    """
    node_type = str(spec.get("type") or "")
    config = spec.get("config") or {}
    parts: list[str] = []

    if node_type == "input.bible":
        from bgate_core import bible as _bible
        want_kind = str(config.get("section_kind") or "").strip()
        want_ids = {str(i) for i in (config.get("section_ids") or []) if str(i).strip()}
        one = str(config.get("section_id") or "").strip()
        if one:
            want_ids.add(one)
        try:
            sections = _bible.list_sections(root, want_kind or None)
        except Exception as exc:
            return {"context_error": f"could not read the bible: {exc}"}
        for section in sections:
            if want_ids and str(section.get("id")) not in want_ids:
                continue
            body = " ".join(str(section.get("body") or "").split())
            if body:
                parts.append(f"{section.get('title')}: {body}")
        if not parts:
            return {"context_error":
                    "no bible section matched — pick one, or a kind that exists"}
        head = "FROM THE DESIGN BIBLE"

    elif node_type == "input.lore":
        from bgate_core import lore as _lore
        slug = str(config.get("entity") or config.get("slug") or "").strip()
        if not slug:
            return {"context_error": "pick a lore entity"}
        try:
            entity = _lore.get_entity(root, slug)
        except Exception:
            return {"context_error": f"no lore entity {slug!r}"}
        if not entity:
            return {"context_error": f"no lore entity {slug!r}"}
        summary = " ".join(str(entity.get("summary") or "").split())
        parts.append(f"{entity.get('name')} ({entity.get('kind')}): {summary}")
        if config.get("include_facts", True):
            try:
                for fact in _lore.facts_of(root, slug):
                    statement = " ".join(str(fact.get("statement") or "").split())
                    if statement:
                        # A locked fact is one the world has committed to; say so,
                        # because "prefer" and "must" are different instructions.
                        lock = "MUST: " if fact.get("locked") else ""
                        parts.append(f"- {lock}{statement}")
            except Exception:
                pass
        head = "WORLD CANON — this content must not contradict it"
    else:
        return {}

    text = head + "\n" + "\n".join(parts)
    limit = 1400
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …"
    return {"text": text}


def _resolve_ref_source(root: str | os.PathLike[str], name: str) -> str:
    """Anything in the project that can act as a visual reference.

    Pinned refs were the only accepted source, which made the most obvious
    anchors in a game unusable: the sprite sheets already sitting in
    game/assets. `refs.resolve` takes a pin name or an ABSOLUTE path, so a
    sheet meant typing C:\\… by hand. Four sources now, cheapest first:

      1. a pinned reference (optionally "name@r2")
      2. a path relative to the project — a sprite sheet, a gear layer, a tile
      3. an absolute path inside the project
      4. an artifact's logical name, resolving to its newest revision
    """
    name = str(name or "").strip()
    if not name:
        return ""
    base = Path(str(root))

    try:
        from bgate_core import refs as _refs
        resolved = _refs.resolve(root, name)
        path = resolved if isinstance(resolved, str) else str(
            (resolved or {}).get("path") or "")
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass  # not a pin; keep looking rather than reporting "no such pin"

    candidate = (base / name.replace("\\", "/")).resolve()
    try:
        candidate.relative_to(base.resolve())   # never escape the project
        if candidate.is_file():
            return str(candidate)
    except (ValueError, OSError):
        pass

    try:
        from bgate_core import artifacts as _a
        for rev in _a.list_revisions(root, logical_name=name, limit=1):
            path = str(rev.get("path") or "")
            if path and os.path.isfile(path):
                return path
    except Exception:
        pass
    return ""


def _ref_output(root: str | os.PathLike[str], spec: dict) -> dict:
    """A reference node's output is the FILE it names.

    Without this a Reference node was decoration: the wire was drawn, typed REF
    and connected, and `_style_refs_for` never looked at it — it read only an
    upstream pick or paths pinned on the generate node itself. So a graph that
    visibly anchored two models to the project's style anchors generated
    whatever the model felt like, which is the opposite of the point.
    """
    config = spec.get("config") or {}
    name = str(config.get("ref") or config.get("name") or "").strip()
    if not name:
        return {}
    path = _resolve_ref_source(root, name)
    if not path:
        return {"ref_error":
                f"could not resolve reference {name!r} — expected a pinned ref "
                "name, a path inside the project (e.g. "
                "game/assets/characters/test_player/pm_paladin_idle.png), or an "
                "artifact's logical name"}
    try:
        strength = float(config.get("strength", 0.5))
    except (TypeError, ValueError):
        strength = 0.5
    return {"ref": {"name": name, "path": path, "strength": strength}}


def _text_output(spec: dict) -> dict:
    """A passive input node's config text is its output — the head of a wire."""
    config = spec.get("config") or {}
    text = str(config.get("text") or config.get("prompt") or "").strip()
    if text and (spec.get("type") in _TEXT_TYPES or config.get("text")):
        return {"text": text}
    return {}


def _passive_output(root: str | os.PathLike[str], spec: dict,
                    inputs: Optional[dict] = None) -> dict:
    """Everything a passive node can produce, resolved in one place.

    There were two copies of this expression — one in :func:`advance`, one in
    :func:`run_node` — and they had already drifted apart (one of them called
    ``_ref_output`` twice). A glue node has to be added to both or it works from
    the poll and not from the ▶, which is the least debuggable kind of bug.

    Glue is tried FIRST because a flow node is the only passive kind that reads
    its inputs: everything else here is the head of a wire, not a bend in one.
    """
    if _wfnodes.is_flow_node(str(spec.get("type") or "")):
        return _wfnodes.flow_output(root, spec, inputs or {})
    return (_text_output(spec) or _context_output(root, spec)
            or _ref_output(root, spec))


# Three writers, one meaning: this passive node could not do its job. Only
# `flow_error` was ever inspected, so a bible section that did not exist and a
# reference that did not resolve both painted the node GREEN and sent an empty
# wire on — and a generate node behind that wire then billed for a prompt with
# no subject and no style anchor. The whole promise of a Reference node is the
# anchor; failing to resolve it is not a detail to carry through.
_PASSIVE_ERROR_KEYS = ("flow_error", "context_error", "ref_error")


def _passive_problem(produced: dict) -> str:
    for key in _PASSIVE_ERROR_KEYS:
        if produced.get(key):
            return str(produced[key])
    return ""


# ---------------------------------------------------------------------------
# Generate nodes — the only thing in this engine that runs in parallel
# ---------------------------------------------------------------------------

def _pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=16,
                                   thread_name_prefix="bgate-generate")
    return _POOL


def _concurrency(root: str | os.PathLike[str]) -> int:
    """How many generations may be in the air at once. The budget already owns
    'how much parallel spend is acceptable' for agent sessions — reuse it here
    rather than inventing a second, disagreeing knob."""
    try:
        cap = int(_spend.budget(root).get("max_concurrent") or 0)
    except Exception:
        cap = 0
    return max(1, cap or DEFAULT_MAX_CONCURRENT)


def _live_generate_count(node_rows: dict[str, dict], specs: dict[str, dict]) -> int:
    return sum(1 for nid, row in node_rows.items()
               if row["status"] == "running"
               and (specs.get(nid) or {}).get("kind") == "generate")


def _generate_worker(root: str | os.PathLike[str], run_id: int, spec: dict,
                     prompt: str, style_refs: list[tuple[str, float]],
                     cascade: bool) -> None:
    """One generate node, off the request thread.

    Writes only new files and new artifact rows, which is exactly why several
    of these may run at once (see the module docstring).
    """
    node_id = spec["id"]
    try:
        result = _generate.run(
            root, run_id=run_id, node_id=node_id, label=spec.get("label", ""),
            config=spec.get("config") or {}, prompt=prompt,
            style_refs=style_refs)
    except Exception as exc:  # a crashed worker must not leave a node 'running'
        result = {"ok": False, "artifacts": [],
                  "error": f"the generation crashed: {type(exc).__name__}: {exc}"}
    try:
        if result.get("ok"):
            _set_output(root, run_id, node_id,
                        {"artifacts": result.get("artifacts") or [],
                         "provider": result.get("provider", ""),
                         "model": result.get("model", ""),
                         "prompt": prompt,
                         "logical_name": result.get("logical_name", "")})
            note = (f" (stopped early: {result['stopped']})"
                    if result.get("stopped") else "")
            _set_node(root, run_id, node_id, "passed",
                      message=f"{len(result.get('artifacts') or [])} candidate(s) "
                              f"from {result.get('provider')}/{result.get('model')} "
                              f"— ~${result.get('usd', 0):.3f}{note}",
                      info={"provider": result.get("provider", ""),
                            "model": result.get("model", ""),
                            "usd": result.get("usd", 0),
                            "candidates": len(result.get("artifacts") or [])})
        else:
            _set_node(root, run_id, node_id, "failed",
                      message=str(result.get("error") or "generation failed"))
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop((int(run_id), node_id), None)
    if cascade:
        # Chained generate steps should keep moving without a poll; a per-node
        # Run deliberately passes cascade=False so it stops where it was asked to.
        try:
            advance(root, run_id)
        except Exception:
            pass


def _start_generate(root: str | os.PathLike[str], run_id: int, spec: dict,
                    prompt: str, style_refs: list[tuple[str, float]],
                    *, cascade: bool) -> bool:
    """Claim the node and put it on a worker. False if someone else got it."""
    node_id = spec["id"]
    if not _claim(root, run_id, node_id):
        return False
    _set_node(root, run_id, node_id, "running",
              message="generating candidates…",
              info={"prompt": prompt[:400]})
    future = _pool().submit(_generate_worker, root, run_id, spec, prompt,
                            style_refs, cascade)
    with _INFLIGHT_LOCK:
        _INFLIGHT[(int(run_id), node_id)] = future
    return True


# ---------------------------------------------------------------------------
# Tool nodes — one named MCP tool per node, on the same worker machinery
# ---------------------------------------------------------------------------
#
# Deliberately built on _start_generate's bones rather than beside them. The
# claim-then-submit dance, the _INFLIGHT registry, join(), the cascade — all of
# that is the concurrency contract this engine already has, and a second
# mechanism would be a second set of races to find. The only thing a tool node
# adds is EXCLUSIVITY: a Godot or Blender node drives the game repo and takes
# the line like an agent step, while a generation tool fans out like a model
# card because everything it touches is new.

def _tool_exclusive(spec: dict) -> bool:
    """Does this tool node have to take the line?

    Read from the registry, never from the graph. A hand-POSTed workflow that
    declared a scene write non-exclusive would run two of them at once and the
    loser's edit would vanish — the same reasoning that puts kind derivation in
    :func:`kind_for` rather than trusting the client's declared kind.
    """
    entry = _wfnodes.spec_for(str(spec.get("type") or ""))
    return True if entry is None else bool(entry.exclusive)


def _tool_paid(spec: dict) -> bool:
    """Does running this node call a provider that bills? From the registry."""
    entry = _wfnodes.spec_for(str(spec.get("type") or ""))
    return bool(entry and entry.paid)


def _node_spends(spec: dict) -> bool:
    """Does starting this node reach something that charges the user?

    Two kinds do, and only one of them was ever asked. The paid-tool rule was
    written for the tool table and left the generate kind out — but a generate
    node IS an image provider call by definition (bgate_core.generate does
    nothing else), so a canvas that opened a run to host one ▶ press was firing
    every other model card whose inputs happened to be satisfied. One predicate,
    both kinds, so the money gate cannot be half-applied again.
    """
    kind = str(spec.get("kind") or "")
    if kind == "generate":
        return True
    return kind == "tool" and _tool_paid(spec)


def spends_money(root: str | os.PathLike[str], run_id: int, node_id: str) -> bool:
    """Would pressing ▶ on this node bill the user? For the route's human gate.

    The route cannot answer this from the request — the graph lives in the run's
    snapshot — and it must answer it BEFORE calling :func:`run_node`, because
    "an agent may not spend money" is a 403 about the caller, not a 409 about
    the node's state.
    """
    _, specs, _, _ = _graph_of(root, run_id)
    spec = specs.get(node_id)
    if spec is None:
        raise LookupError(f"run {run_id} has no node {node_id!r}")
    return _node_spends(spec)


def _tool_worker(root: str | os.PathLike[str], run_id: int, spec: dict,
                 inputs: dict, cascade: bool) -> None:
    """One tool node, off the request thread.

    Godot boots, Blender renders and provider calls all run for minutes; doing
    any of it on the poll's thread would block the dashboard for every other
    seat, which is the exact failure the MCP server's own docstring says its
    thread hop exists to avoid.
    """
    node_id = spec["id"]
    try:
        result = _wfnodes.run(
            root, run_id=run_id, node_id=node_id, label=spec.get("label", ""),
            node_type=str(spec.get("type") or ""),
            config=spec.get("config") or {}, inputs=inputs)
    except Exception as exc:  # a crashed worker must not leave a node 'running'
        result = {"ok": False, "artifacts": [],
                  "error": f"the tool call crashed: {type(exc).__name__}: {exc}"}
    try:
        if result.get("ok"):
            _set_output(root, run_id, node_id, result.get("output") or {})
            _set_node(root, run_id, node_id, "passed",
                      message=str(result.get("message") or "the tool finished"),
                      info={"tool": (result.get("output") or {}).get("tool", ""),
                            "usd": result.get("usd", 0),
                            "candidates": len(result.get("artifacts") or [])})
        else:
            # The payload is kept even on failure: "godot refused to open the
            # project" is a fact the next person needs, and throwing it away
            # leaves a red node with a one-line summary of a ten-line reason.
            if result.get("output"):
                _set_output(root, run_id, node_id, result["output"])
            _set_node(root, run_id, node_id, "failed",
                      message=str(result.get("error") or "the tool failed"))
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop((int(run_id), node_id), None)
    if cascade:
        try:
            advance(root, run_id)
        except Exception:
            pass


def _start_tool(root: str | os.PathLike[str], run_id: int, spec: dict,
                inputs: dict, *, cascade: bool) -> bool:
    """Claim the node and put it on a worker. False if someone else got it."""
    node_id = spec["id"]
    entry = _wfnodes.spec_for(str(spec.get("type") or ""))
    if not _claim(root, run_id, node_id):
        return False
    _set_node(root, run_id, node_id, "running",
              message=f"calling {entry.tool}…" if entry else "calling the tool…",
              info={"tool": entry.tool if entry else "", })
    future = _pool().submit(_tool_worker, root, run_id, spec, inputs, cascade)
    with _INFLIGHT_LOCK:
        _INFLIGHT[(int(run_id), node_id)] = future
    return True


def join(run_id: Optional[int] = None, timeout: float = 120.0) -> None:
    """Block until in-flight generations finish. For tests and for a CLI that
    wants a run to be settled before it reports — the engine itself never waits."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        with _INFLIGHT_LOCK:
            futures = [f for (rid, _), f in _INFLIGHT.items()
                       if (run_id is None or rid == int(run_id)) and not f.done()]
        left = deadline - time.monotonic()
        if not futures or left <= 0:
            return
        # A finished worker can cascade into the next generate node, so re-read
        # the registry rather than waiting once on a snapshot of it.
        _wait(futures, timeout=min(left, 1.0))


def _upstream_item_ids(node_rows: dict[str, dict], ups: dict[str, list[str]],
                       node_id: str) -> list[int]:
    out = []
    for parent in ups.get(node_id, ()):
        row = node_rows.get(parent)
        if row and row.get("work_item_id"):
            out.append(int(row["work_item_id"]))
    return out


def _consistency_scores(root: str | os.PathLike[str], node_row: dict,
                        item_ids: list[int]) -> list[dict]:
    """Every real on-model score this check can see.

    Two sources, both produced by the independent QA path: a score posted
    straight at the node (:func:`observe`), and the ``metadata.qa_review``
    verdicts ``art_qa_verdict`` writes onto the artifacts the graded steps
    produced. No source is invented — if both are empty the check has seen
    nothing and says so.
    """
    found: list[dict] = []
    info = _info(node_row)
    if info.get("observed_score") is not None:
        found.append({"source": "observed", "score": float(info["observed_score"]),
                      "detail": info.get("observed_detail", "")})
    if item_ids:
        wanted = set(item_ids)
        for art in _artifacts.list_revisions(root, limit=500):
            if art.get("work_item_id") not in wanted:
                continue
            review = (art.get("metadata") or {}).get("qa_review") or {}
            if not isinstance(review, dict) or review.get("score") is None:
                continue
            found.append({
                "source": f"artifact:{art['id']}",
                "logical_name": art.get("logical_name"),
                "verdict": review.get("verdict"),
                "score": float(review.get("score") or 0),
                "detail": str(review.get("reasons") or "")[:200],
            })
    return found


def _evaluate_consistency(root: str | os.PathLike[str], run_id: int, spec: dict,
                          node_row: dict, item_ids: list[int]) -> dict:
    """Enforce the threshold the node has been drawing on its card all along."""
    try:
        threshold = float((spec.get("config") or {}).get("threshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        threshold = float(DEFAULT_THRESHOLD)
    scores = _consistency_scores(root, node_row, item_ids)
    if not scores:
        return _set_node(
            root, run_id, spec["id"], "failed",
            message=(f"no on-model score was recorded, so nothing can be certified "
                     f"against the {threshold:g}% threshold — the reviewer must call "
                     f"art_qa_verdict on each candidate, or post a score to this node"),
            info={"threshold": threshold, "scores": []})
    # The strictest reading, deliberately: one off-model frame in a sheet is an
    # off-model sheet.
    worst = min(scores, key=lambda s: s["score"])
    if worst["score"] < threshold:
        return _set_node(
            root, run_id, spec["id"], "failed",
            message=(f"consistency {worst['score']:g}% is below the "
                     f"{threshold:g}% threshold ({worst['source']}"
                     + (f": {worst['detail']}" if worst.get("detail") else "") + ")"),
            info={"threshold": threshold, "score": worst["score"], "scores": scores})
    return _set_node(
        root, run_id, spec["id"], "passed",
        message=f"consistency {worst['score']:g}% ≥ {threshold:g}% across "
                f"{len(scores)} candidate(s)",
        info={"threshold": threshold, "score": worst["score"], "scores": scores})


def _sync_items(root: str | os.PathLike[str], run_id: int, specs: dict[str, dict],
                node_rows: dict[str, dict], ups: dict[str, list[str]]) -> dict[str, dict]:
    """Pull each in-flight step's queue item outcome onto its node."""
    for node_id, row in list(node_rows.items()):
        if row["status"] not in ("queued", "running") or not row.get("work_item_id"):
            continue
        try:
            item = _queue.get(root, int(row["work_item_id"]))
        except LookupError:
            node_rows[node_id] = _set_node(
                root, run_id, node_id, "failed",
                message=f"work item #{row['work_item_id']} no longer exists")
            continue
        spec = specs.get(node_id) or {}
        status = item["status"]
        result = str(item.get("result") or "")[:400]
        if status == "dispatched" and row["status"] != "running":
            node_rows[node_id] = _set_node(
                root, run_id, node_id, "running",
                message=f"work item #{item['id']} is running on the {item['seat']} seat")
        elif status == "done":
            if spec.get("kind") == "consistency":
                node_rows[node_id] = _evaluate_consistency(
                    root, run_id, spec, row,
                    [int(row["work_item_id"])] + _upstream_item_ids(node_rows, ups, node_id))
            else:
                # An agent step's summary IS its output. That is what makes an
                # "LLM writes the prompt" step wireable into a generate node:
                # the thing it reported is the thing the next node consumes.
                if result:
                    _set_output(root, run_id, node_id,
                                {"text": result, "work_item_id": int(item["id"])})
                node_rows[node_id] = _set_node(
                    root, run_id, node_id, "passed",
                    message=result or f"work item #{item['id']} completed")
        elif status in ("failed", "cancelled"):
            node_rows[node_id] = _set_node(
                root, run_id, node_id, "failed",
                message=f"work item #{item['id']} {status}"
                        + (f": {result}" if result else ""))
    return node_rows


def advance(root: str | os.PathLike[str], run_id: int,
            dispatch: Optional[bool] = None) -> dict:
    """Tick the run: absorb finished work, then take every step it can take.

    Sequential for anything that touches the game repo or a human — one queue
    item in flight, a gate or a pick stops everything behind it. Generate nodes
    are the one exception and the module docstring says why: they collide with
    nothing, and comparing models means running them at the same time.
    """
    run = _run_row(root, run_id)
    if run["status"] != "running":
        return get(root, run_id)

    snapshot = json.loads(run["graph_json"] or "{}")
    specs = {n["id"]: n for n in (snapshot.get("nodes") or []) if isinstance(n, dict)}
    order = [i for i in (snapshot.get("order") or list(specs)) if i in specs]
    edges = snapshot.get("edges") or []
    ups = _upstream(edges, order)
    cycles = _cycle_nodes(order, edges)
    if dispatch is None:
        dispatch = bool((snapshot.get("options") or {}).get("dispatch"))

    node_rows = _sync_items(root, run_id, specs, _node_rows(root, run_id), ups)

    failed = False
    waiting = False
    # The 'line': agent/consistency/gate/pick work is single-file, so the first
    # one of those that is in flight (or blocking) stops any further one from
    # starting. Generate nodes ignore it — they are not on that line.
    line_held = False
    slots = max(0, _concurrency(root) - _live_generate_count(node_rows, specs))

    for position, node_id in enumerate(order, start=1):
        row = node_rows.get(node_id)
        if row is None:
            continue
        status = row["status"]
        if status in ("passed", "skipped"):
            continue
        if status == "failed":
            failed = True
            break
        if status in ("queued", "running"):
            waiting = True
            spec_live = specs.get(node_id) or {}
            kind_live = spec_live.get("kind")
            # A generate node is never on the line. A tool node is on it only
            # when the registry says it touches the game repo or an engine
            # process — see _tool_exclusive.
            if kind_live == "generate":
                pass
            elif kind_live == "tool" and not _tool_exclusive(spec_live):
                pass
            else:
                line_held = True
            continue
        # pending — can it start?
        parents = [u for u in ups.get(node_id, ())
                   if node_rows.get(u) and node_rows[u]["status"] not in ("passed", "skipped")]
        if parents:
            if node_id in cycles:
                _set_node(root, run_id, node_id, "failed",
                          message=f"this step can never start — it depends on "
                                  f"{parents[0]}, which depends back on it (cycle)")
                failed = True
                break
            continue  # a parent is simply still working
        spec = specs[node_id]
        kind = spec.get("kind") or "passive"
        inputs = _inputs(root, run_id, node_id, ups, node_rows)

        # A NODE THAT SPENDS MONEY IS NEVER STARTED BY A TICK.
        #
        # The canvas opens a run just to HOST a single-node execution (wf.js
        # `_ensureRun`, dispatch off). Without this, pressing ▶ on one card also
        # fires every other paid node whose inputs happen to be satisfied — a
        # video shot, a music track, a variant grid, the other two arms of a
        # model comparison — none of which the user asked for and all of which
        # are real money. So a paid node waits for a person to press ▶ on IT,
        # unless the whole workflow was started with Run (dispatch on), which is
        # the explicit "yes, do all of this" act.
        #
        # This used to guard tool nodes only, which left the kind that is paid
        # BY DEFINITION — generate — starting itself on every poll.
        if _node_spends(spec) and not dispatch:
            waiting = True
            if not _info(row).get("awaiting_human"):
                node_rows[node_id] = _set_node(
                    root, run_id, node_id, "pending",
                    message="this step spends money — press run on this "
                            "card when you want it, or use Run workflow",
                    info={"awaiting_human": True})
            continue

        if kind == "generate":
            if slots <= 0:
                waiting = True  # over the concurrency cap; the next tick takes it
                continue
            prompt = _prompt_for(spec, inputs)
            if _start_generate(root, run_id, spec, prompt,
                               _style_refs_for(root, spec, inputs), cascade=True):
                slots -= 1
                waiting = True
                node_rows[node_id] = _node_rows(root, run_id)[node_id]
            continue
        if kind == "tool":
            exclusive = _tool_exclusive(spec)
            if exclusive:
                if line_held:
                    waiting = True
                    continue
            elif slots <= 0:
                waiting = True  # over the concurrency cap; the next tick takes it
                continue
            if _start_tool(root, run_id, spec, inputs, cascade=True):
                waiting = True
                if exclusive:
                    line_held = True
                else:
                    slots -= 1
                node_rows[node_id] = _node_rows(root, run_id)[node_id]
            continue
        if line_held:
            waiting = True
            continue
        if kind == "passive":
            produced = _passive_output(root, spec, inputs)
            # A passive node that could not do its job FAILS the run rather than
            # passing an empty wire on. "the filter matched nothing", "no bible
            # section matched", "could not resolve reference" arriving as a
            # green node is how a graph silently produces nothing at the end.
            problem = _passive_problem(produced)
            if problem:
                node_rows[node_id] = _set_node(
                    root, run_id, node_id, "failed", message=problem)
                failed = True
                break
            if produced:
                _set_output(root, run_id, node_id, produced)
            node_rows[node_id] = _set_node(
                root, run_id, node_id, "passed",
                message="no agent work — carried straight through")
            continue
        if kind in ("gate", "pick"):
            waiting_for = ("a human to approve this gate" if kind == "gate"
                           else f"a human to pick one of "
                                f"{len(inputs['candidates'])} candidate(s)")
            node_rows[node_id] = _set_node(
                root, run_id, node_id, "running",
                message=f"BLOCKED — waiting for {waiting_for}",
                info={"candidates": len(inputs["candidates"])} if kind == "pick" else None)
            waiting = True
            line_held = True
            continue
        node_rows[node_id] = _queue_step(root, run, snapshot, spec, position,
                                         len(order), dispatch, inputs=inputs)
        if node_rows[node_id]["status"] == "failed":
            failed = True
            break
        waiting = True
        line_held = True

    if failed:
        for node_id, row in node_rows.items():
            if row["status"] == "pending":
                _set_node(root, run_id, node_id, "skipped",
                          message="skipped — an earlier step failed")
        _set_run(root, run_id, "failed")
        activity.log(root, "workflow", f"run {run_id} failed: {run['name']}",
                     ref=str(run_id))
    elif not waiting:
        _set_run(root, run_id, "passed")
        activity.log(root, "workflow", f"run {run_id} passed: {run['name']}",
                     ref=str(run_id))
    return get(root, run_id)


# ---------------------------------------------------------------------------
# Human decisions
# ---------------------------------------------------------------------------

def is_agent_actor(actor: str) -> bool:
    return str(actor or "").startswith(AGENT_ACTOR_PREFIX)


def approve(root: str | os.PathLike[str], run_id: int, node_id: str, *,
            decision: str = "approve", actor: str = "", note: str = "") -> dict:
    """Resolve a blocking gate. Humans only — that is the whole point of a gate.

    The route layer also calls ``api.require_human``; this second check exists
    because the engine, not the transport, owns the guarantee.
    """
    decision = (decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    if is_agent_actor(actor):
        raise PermissionError(
            f"a gate can only be resolved by a human — {actor} is an agent")
    run = _run_row(root, run_id)
    if run["status"] != "running":
        raise ValueError(f"workflow run {run_id} is {run['status']}, not running")
    node_rows = _node_rows(root, run_id)
    row = node_rows.get(node_id)
    if row is None:
        raise LookupError(f"run {run_id} has no node {node_id!r}")
    if row["kind"] == "pick":
        # A pick that has candidates must be answered WITH one — approving it
        # would tell the run a human was happy and tell the next step nothing.
        # With nothing to choose between it degrades to a plain gate, which is
        # what every 'control.select' drawn before picks existed relied on.
        if candidates(root, run_id, node_id):
            raise ValueError(
                f"node {node_id!r} is a pick, not a gate — resolve it with "
                f"pick(artifact_id=...) so the downstream step knows WHICH "
                f"candidate was chosen")
    elif row["kind"] != "gate":
        raise ValueError(f"node {node_id!r} is a {row['kind']} step, not a gate")
    if row["status"] != "running":
        raise ValueError(f"gate {node_id!r} is {row['status']}, not waiting for approval")
    who = actor or "unknown"
    if decision == "approve":
        _set_node(root, run_id, node_id, "passed",
                  message=f"approved by {who}" + (f": {note}" if note else ""),
                  info={"approved_by": who, "note": note[:400]})
    else:
        _set_node(root, run_id, node_id, "failed",
                  message=f"rejected by {who}" + (f": {note}" if note else ""),
                  info={"rejected_by": who, "note": note[:400]})
    activity.log(root, "workflow",
                 f"run {run_id} gate {node_id} {decision}d by {who}", ref=str(run_id))
    return advance(root, run_id)


def _graph_of(root: str | os.PathLike[str], run_id: int) -> tuple[dict, dict, list[str], dict]:
    """(snapshot, specs, order, upstream-map) for a run."""
    run = _run_row(root, run_id)
    snapshot = json.loads(run["graph_json"] or "{}")
    specs = {n["id"]: n for n in (snapshot.get("nodes") or []) if isinstance(n, dict)}
    order = [i for i in (snapshot.get("order") or list(specs)) if i in specs]
    return snapshot, specs, order, _upstream(snapshot.get("edges") or [], order)


def candidates(root: str | os.PathLike[str], run_id: int, node_id: str) -> list[dict]:
    """Everything a pick node is choosing between — the upstream generate
    nodes' registered candidates, in the order they were produced."""
    _, specs, order, ups = _graph_of(root, run_id)
    node_rows = _node_rows(root, run_id)
    if node_id not in node_rows:
        raise LookupError(f"run {run_id} has no node {node_id!r}")
    return _inputs(root, run_id, node_id, ups, node_rows)["candidates"]


def pick(root: str | os.PathLike[str], run_id: int, node_id: str, *,
         artifact_id: Optional[int] = None, reject: bool = False,
         actor: str = "", note: str = "") -> dict:
    """Resolve a pick node to a CHOICE. Humans only.

    This is the node that makes a multi-model fan-out worth drawing: the run
    holds, a person looks at what three models actually produced, and the one
    they choose becomes this node's output — so the next step consumes THAT
    candidate rather than "whatever the last step happened to write last".

    Rejecting everything is a real answer and fails the node: three bad
    candidates should stop the run, not quietly promote the least bad one.

    Guarded twice, exactly like :func:`approve`: the route calls
    ``api.require_human`` and the engine refuses an agent actor itself, because
    the guarantee belongs to the engine and not to the transport.
    """
    if is_agent_actor(actor):
        raise PermissionError(
            f"a pick can only be made by a human — {actor} is an agent")
    run = _run_row(root, run_id)
    if run["status"] != "running":
        raise ValueError(f"workflow run {run_id} is {run['status']}, not running")
    row = _node_rows(root, run_id).get(node_id)
    if row is None:
        raise LookupError(f"run {run_id} has no node {node_id!r}")
    if row["kind"] != "pick":
        raise ValueError(f"node {node_id!r} is a {row['kind']} step, not a pick")
    if row["status"] != "running":
        raise ValueError(f"pick {node_id!r} is {row['status']}, not waiting for a choice")

    who = actor or "unknown"
    options = candidates(root, run_id, node_id)
    if reject:
        _set_output(root, run_id, node_id, {})
        _set_node(root, run_id, node_id, "failed",
                  message=f"all {len(options)} candidate(s) rejected by {who}"
                          + (f": {note}" if note else ""),
                  info={"rejected_by": who, "note": note[:400],
                        "candidates": len(options)})
        activity.log(root, "workflow",
                     f"run {run_id} pick {node_id} rejected every candidate ({who})",
                     ref=str(run_id))
        return advance(root, run_id)

    if artifact_id is None:
        raise ValueError("a pick needs an artifact_id (or reject=True)")
    chosen = next((c for c in options if int(c["artifact_id"]) == int(artifact_id)), None)
    if chosen is None:
        raise ValueError(
            f"artifact {artifact_id} is not one of this pick's candidates — "
            f"choose from {[c['artifact_id'] for c in options] or 'nothing yet'}")
    _set_output(root, run_id, node_id,
                {"picked": chosen, "artifacts": [chosen]})
    _set_node(root, run_id, node_id, "passed",
              message=f"{who} picked artifact #{chosen['artifact_id']}"
                      + (f" ({chosen['model']})" if chosen.get("model") else "")
                      + f" of {len(options)}" + (f": {note}" if note else ""),
              info={"picked_by": who, "artifact_id": chosen["artifact_id"],
                    "path": chosen.get("path", ""), "model": chosen.get("model", ""),
                    "candidates": len(options), "note": note[:400]})
    activity.log(root, "workflow",
                 f"run {run_id} pick {node_id}: artifact #{chosen['artifact_id']} "
                 f"chosen by {who}", ref=str(run_id))
    return advance(root, run_id)


def run_node(root: str | os.PathLike[str], run_id: int, node_id: str, *,
             actor: str = "", dispatch: Optional[bool] = None) -> dict:
    """Run EXACTLY this node, then stop. The ▶ on a node card.

    The contract, in full:

      * the run must be live and the node must be pending;
      * its inputs must be satisfied — every parent passed or skipped — and it
        says which parent is not, rather than starting on missing input;
      * gates and picks are not runnable: they are resolved by a person, which
        is :func:`approve` / :func:`pick`, a different verb with its own guard;
      * a node that SPENDS is human-only, exactly like a gate or a pick. This
        is what ``actor`` is for; it was accepted here and then never read, so
        an agent holding the dashboard token could POST run on a paid tool node
        or a model card and bill the account, while the same agent was refused
        at the gate two nodes upstream;
      * an agent step still respects the single-file rule — if another queue
        item from this run is in flight, this one refuses;
      * NOTHING cascades. Downstream nodes stay pending even if this one
        passes; the human is the scheduler here, and the next step is theirs to
        press. (A poll of :func:`advance` will of course carry on as normal —
        stepping is a way to drive a run by hand, not a mode that disables it.)
    """
    run = _run_row(root, run_id)
    if run["status"] != "running":
        raise ValueError(f"workflow run {run_id} is {run['status']}, not running")
    snapshot, specs, order, ups = _graph_of(root, run_id)
    # Absorb finished work first — without it a step whose parent finished a
    # second ago is refused for an input that is actually satisfied. Absorbing
    # is not advancing: nothing new is started by this.
    node_rows = _sync_items(root, run_id, specs, _node_rows(root, run_id), ups)
    row = node_rows.get(node_id)
    if row is None:
        raise LookupError(f"run {run_id} has no node {node_id!r}")
    spec = specs.get(node_id)
    if spec is None:
        raise LookupError(f"run {run_id} has no step {node_id!r} in its graph")
    kind = spec.get("kind") or "passive"
    # What the user called this step on the canvas, not its internal id.
    label = f"'{spec.get('label') or node_id}'"
    if kind not in RUNNABLE_KINDS:
        raise ValueError(
            f"a {kind} step is not run, it is resolved by a human — "
            f"use {'pick' if kind == 'pick' else 'approve'} on {node_id!r}")
    # Guarded twice, exactly like approve() and pick(): the route calls
    # api.require_human, and the engine refuses an agent actor itself, because
    # the guarantee belongs to the engine and not to the transport. Asked before
    # the status checks — whether a robot may spend is not a question about what
    # the node happens to be doing right now.
    if _node_spends(spec) and is_agent_actor(actor):
        raise PermissionError(
            f"{label} spends money, so only a human can start it — "
            f"{actor} is an agent")
    if row["status"] != "pending":
        # Say what happened and what to do about it. "is queued, not pending"
        # is this module's vocabulary, not the user's, and it was the only
        # feedback a second click produced.
        said = {
            "queued": f"{label} is already queued and waiting to start",
            # 'running' names :func:`reconcile` because a node whose worker died
            # with its process is ALSO 'running' and waiting for it never ends —
            # this message used to be the last thing such a run ever said.
            "running": f"{label} is already running — wait for it to finish, or "
                       "reconcile the run if Builders Gate was restarted while "
                       "it was working",
            "passed": f"{label} has already run; start the workflow again to "
                      "do it over",
            "failed": f"{label} already failed — start the workflow again to "
                      "retry it",
            "skipped": f"{label} was skipped because an earlier step failed",
        }.get(row["status"], f"{label} is {row['status']} and cannot be started")
        raise ValueError(said)
    unmet = [u for u in ups.get(node_id, ())
             if node_rows.get(u) and node_rows[u]["status"] not in ("passed", "skipped")]
    if unmet:
        raise ValueError(
            f"{node_id!r} cannot run yet — it takes input from "
            f"{', '.join(repr(u) for u in unmet)}, which "
            f"{'has' if len(unmet) == 1 else 'have'} not finished")

    inputs = _inputs(root, run_id, node_id, ups, node_rows)
    if kind == "generate":
        prompt = _prompt_for(spec, inputs)
        if not _start_generate(root, run_id, spec, prompt,
                               _style_refs_for(root, spec, inputs), cascade=False):
            raise ValueError(f"{node_id!r} was already claimed by another tick")
        return get(root, run_id)
    if kind == "tool":
        # An exclusive tool node still respects the single-file rule: it drives
        # the game repo or an engine process, and two of those at once is
        # last-write-wins exactly as it is for an agent session.
        if _tool_exclusive(spec):
            busy_line = [nid for nid, r in node_rows.items()
                         if r["status"] in ("queued", "running")
                         and (r.get("work_item_id")
                              or (specs.get(nid, {}).get("kind") == "tool"
                                  and _tool_exclusive(specs[nid])))]
            busy_line = [nid for nid in busy_line if nid != node_id]
            if busy_line:
                raise ValueError(
                    f"{label} writes into the game project, so it cannot start "
                    f"while {busy_line[0]!r} is still in flight — two of those "
                    "at once is last-write-wins")
        if not _start_tool(root, run_id, spec, inputs, cascade=False):
            raise ValueError(f"{node_id!r} was already claimed by another tick")
        return get(root, run_id)
    if kind == "passive":
        produced = _passive_output(root, spec, inputs)
        problem = _passive_problem(produced)
        if problem:
            # Refused rather than failed: the human is standing at the card and
            # can fix the template/index/ref name and press run again, which is
            # a better afternoon than a node stuck in 'failed'.
            raise ValueError(f"{label}: {problem}")
        if produced:
            _set_output(root, run_id, node_id, produced)
        _set_node(root, run_id, node_id, "passed",
                  message="no agent work — carried straight through")
        return get(root, run_id)

    # agent / consistency: one queue item at a time, still.
    busy = [nid for nid, r in node_rows.items()
            if r["status"] in ("queued", "running") and r.get("work_item_id")]
    if busy:
        raise ValueError(
            f"{node_id!r} cannot start while {busy[0]!r} still has work in "
            "flight — agent steps run one at a time, because two sessions "
            "editing the same repo is last-write-wins")
    if dispatch is None:
        dispatch = bool((snapshot.get("options") or {}).get("dispatch"))
    position = order.index(node_id) + 1 if node_id in order else 1
    _queue_step(root, run, snapshot, spec, position, len(order), bool(dispatch),
                inputs=inputs)
    return get(root, run_id)


def observe(root: str | os.PathLike[str], run_id: int, node_id: str, *,
            score: float, detail: str = "", actor: str = "") -> dict:
    """Record a measured on-model score against a consistency node.

    The reviewer that grades candidates without producing artifacts (a headless
    consistency_check run, a human eyeballing a sheet) needs somewhere to put a
    number. Agents may call this — reporting a measurement is not approving it.
    """
    row = _node_rows(root, run_id).get(node_id)
    if row is None:
        raise LookupError(f"run {run_id} has no node {node_id!r}")
    if row["kind"] != "consistency":
        raise ValueError(f"node {node_id!r} is a {row['kind']} step; only a "
                         "consistency check takes a score")
    value = max(0.0, min(100.0, float(score)))
    _set_node(root, run_id, node_id, row["status"],
              info={"observed_score": value, "observed_detail": detail[:400],
                    "observed_by": actor})
    return advance(root, run_id)


def reconcile(root: str | os.PathLike[str],
              run_id: Optional[int] = None) -> dict:
    """Release nodes a dead process left at 'running'. The exit that was missing.

    :data:`_INFLIGHT` is in-memory and dies with the process. A generate or tool
    node claimed by a worker that never came back — the dashboard restarted
    mid-generation, the machine slept, ``bgate serve`` was Ctrl-C'd — keeps the
    row it was claimed with for ever. Nothing else in this module can move it:
    :func:`advance` reads 'running' as work in flight and an exclusive one holds
    the whole line behind it, :func:`_sync_items` only looks at nodes that have
    a queue item, and :func:`run_node` answers the second press with "already
    running — wait for it to finish". There was no wait that ended.

    Only worker-owned kinds are touched. A gate or a pick sits at 'running'
    BECAUSE it is waiting for a human; an agent or consistency step's status
    belongs to its queue item, which :func:`_sync_items` already reconciles
    against the queue. Failing either of those here would be this function
    inventing an outage.

    Deliberate and caller-driven, never automatic: this process cannot tell a
    worker that died from one belonging to a second live dashboard on the same
    project, so the honest guard is that a person asks for it. A future this
    process still holds and has not finished is left alone regardless.
    """
    if run_id is None:
        ids = [int(r["id"]) for r in rows(db.connect(root).execute(
            "SELECT id FROM workflow_run WHERE status = 'running' ORDER BY id"))]
    else:
        _run_row(root, run_id)      # LookupError -> 404, like every other verb
        ids = [int(run_id)]

    released: list[dict] = []
    for rid in ids:
        _, specs, _, _ = _graph_of(root, rid)
        for node_id, row in _node_rows(root, rid).items():
            if row["status"] != "running" or row.get("work_item_id"):
                continue
            kind = str((specs.get(node_id) or {}).get("kind") or row["kind"])
            if kind not in ("generate", "tool"):
                continue
            with _INFLIGHT_LOCK:
                future = _INFLIGHT.get((rid, node_id))
            if future is not None and not future.done():
                continue
            _set_node(root, rid, node_id, "failed",
                      message="this step was still running when Builders Gate "
                              "stopped, so its result can never arrive — "
                              "nothing was recorded for it. Start the workflow "
                              "again to retry it.",
                      info={"reconciled": True})
            released.append({"run_id": rid, "node_id": node_id,
                             "label": row["label"], "kind": kind})
    for rid in sorted({r["run_id"] for r in released}):
        activity.log(root, "workflow",
                     f"run {rid} reconciled: "
                     f"{len([r for r in released if r['run_id'] == rid])} "
                     f"step(s) released from a dead worker", ref=str(rid))
        # A failed node is a failed run, and advance is what says so — the same
        # path a provider failure takes. It starts nothing: it breaks at the
        # first failed node.
        advance(root, rid)

    touched = ids if run_id is not None else sorted({r["run_id"] for r in released})
    return {"released": released, "runs": [get(root, i) for i in touched]}


def cancel(root: str | os.PathLike[str], run_id: int, *, actor: str = "") -> dict:
    run = _run_row(root, run_id)
    if run["status"] != "running":
        return get(root, run_id)
    for node_id, row in _node_rows(root, run_id).items():
        if row["status"] in ("pending", "queued", "running"):
            _set_node(root, run_id, node_id, "skipped",
                      message=f"run cancelled by {actor or 'a human'}")
    _set_run(root, run_id, "cancelled")
    activity.log(root, "workflow", f"run {run_id} cancelled: {run['name']}",
                 ref=str(run_id))
    return get(root, run_id)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _node_public(row: dict) -> dict:
    info = _info(row)
    return {
        "node_id": row["node_id"],
        "kind": row["kind"],
        "label": row["label"],
        "status": row["status"],
        "work_item_id": row["work_item_id"],
        "detail": info.get("message", ""),
        "info": {k: v for k, v in info.items() if k != "message"},
        # What this node produced, so the canvas can paint candidates under a
        # generate node and the chosen one under a pick without a second call.
        "output": _output(row),
        "updated_at": row["updated_at"],
    }


def get(root: str | os.PathLike[str], run_id: int, *,
        include_graph: bool = False) -> dict:
    """The poll payload: run status + one small record per node.

    The graph itself is only sent when asked for (a reload restoring a run),
    never on the polling path — repainting statuses must not re-ship the graph.
    """
    run = _run_row(root, run_id)
    node_rows = _node_rows(root, run_id)
    nodes = [_node_public(r) for r in node_rows.values()]
    counts: dict[str, int] = {}
    for node in nodes:
        counts[node["status"]] = counts.get(node["status"], 0) + 1
    snapshot = json.loads(run["graph_json"] or "{}")
    out = {
        "id": run["id"],
        "name": run["name"],
        "seat": run["seat"],
        "status": run["status"],
        "actor": run["actor"],
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "workflow_id": (snapshot.get("workflow") or {}).get("id", ""),
        "nodes": nodes,
        "counts": counts,
        "gates": [n["node_id"] for n in nodes
                  if n["kind"] == "gate" and n["status"] == "running"],
        # Kept apart from `gates`: a gate is answered yes/no, a pick is answered
        # with an artifact id. One list for both would let a UI offer the wrong
        # button, and the engine would then refuse it.
        "picks": [n["node_id"] for n in nodes
                  if n["kind"] == "pick" and n["status"] == "running"],
    }
    if include_graph:
        out["graph"] = snapshot
    return out


def list_runs(root: str | os.PathLike[str], *, workflow_id: str = "",
              status: str = "", limit: int = 20) -> list[dict]:
    sql = "SELECT * FROM workflow_run WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit) * (4 if workflow_id else 1), 200)))
    out = []
    for row in rows(db.connect(root).execute(sql, params)):
        snapshot = json.loads(row["graph_json"] or "{}")
        wid = (snapshot.get("workflow") or {}).get("id", "")
        if workflow_id and wid != workflow_id:
            continue
        out.append({"id": row["id"], "name": row["name"], "status": row["status"],
                    "actor": row["actor"], "workflow_id": wid,
                    "created_at": row["created_at"], "updated_at": row["updated_at"]})
        if len(out) >= limit:
            break
    return out


def latest_for_workflow(root: str | os.PathLike[str], workflow_id: str,
                        *, running_only: bool = True) -> Optional[dict]:
    """The run a reopened builder should re-attach to — this is what makes a run
    survive a page reload without the browser holding any state."""
    for run in list_runs(root, workflow_id=workflow_id, limit=10):
        if not running_only or run["status"] == "running":
            return run
    return None


def pending_gates(root: str | os.PathLike[str]) -> list[dict]:
    """Every gate blocking a live run — the surface that makes a gate answerable."""
    return [dict(r) | {"detail": _info(dict(r)).get("message", "")} for r in rows(
        db.connect(root).execute(
            "SELECT n.run_id, n.node_id, n.kind, n.label, n.status, n.detail, "
            "       n.updated_at, r.name AS run_name, r.actor AS run_actor "
            "FROM workflow_run_node n JOIN workflow_run r ON r.id = n.run_id "
            "WHERE n.kind = 'gate' AND n.status = 'running' AND r.status = 'running' "
            "ORDER BY n.updated_at"))]


def pending_picks(root: str | os.PathLike[str]) -> list[dict]:
    """Every pick blocking a live run, each with what it is choosing between.

    Separate from :func:`pending_gates` because the answer is a different shape
    — an artifact id, not a yes — and a caller that cannot see the candidates
    cannot give that answer.
    """
    out = []
    for row in rows(db.connect(root).execute(
            "SELECT n.run_id, n.node_id, n.kind, n.label, n.status, n.detail, "
            "       n.updated_at, r.name AS run_name, r.actor AS run_actor "
            "FROM workflow_run_node n JOIN workflow_run r ON r.id = n.run_id "
            "WHERE n.kind = 'pick' AND n.status = 'running' AND r.status = 'running' "
            "ORDER BY n.updated_at")):
        entry = dict(row) | {"detail": _info(dict(row)).get("message", "")}
        try:
            entry["candidates"] = candidates(root, int(row["run_id"]), row["node_id"])
        except LookupError:
            entry["candidates"] = []
        out.append(entry)
    return out


def for_work_item(root: str | os.PathLike[str], item_id: int) -> Optional[dict]:
    """Which run/node a queue item belongs to, read off its source_ref."""
    try:
        item = _queue.get(root, item_id)
    except LookupError:
        return None
    ref = str(item.get("source_ref") or "")
    if not ref.startswith("run:"):
        return None
    parts = ref.split(":", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    return {"run_id": int(parts[1]), "node_id": parts[2]}
