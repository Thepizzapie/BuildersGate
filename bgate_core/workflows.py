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
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterable, Optional

from . import activity, artifacts as _artifacts, db, queue as _queue
from .util import rows

RUN_STATUSES = ("running", "passed", "failed", "cancelled")
NODE_STATUSES = ("pending", "queued", "running", "passed", "failed", "skipped")
KINDS = ("agent", "gate", "consistency", "passive")

# Agents stamp this prefix into BGATE_ACTOR; see bgate_ui.api.current_actor.
AGENT_ACTOR_PREFIX = "agent:"
DEFAULT_THRESHOLD = 80

# Steps that are a human decision point regardless of what the palette calls
# them. 'control.select' ("human picks the best variant") is as much a gate as
# 'control.gate' — both promised a pause and delivered none.
_GATE_TYPES = {"control.gate", "control.select"}
_CONSISTENCY_TYPES = {"control.consistency"}


# ---------------------------------------------------------------------------
# Graph normalisation
# ---------------------------------------------------------------------------

def kind_for(spec: dict) -> str:
    """What a node MEANS to the engine, independent of its display type.

    The client may state it (the step registry knows best); we re-derive it
    anyway so a hand-rolled or older graph still gates correctly.
    """
    declared = str(spec.get("kind") or "").strip()
    if declared in KINDS:
        return declared
    node_type = str(spec.get("type") or "")
    if node_type in _GATE_TYPES:
        return "gate"
    if node_type in _CONSISTENCY_TYPES:
        return "consistency"
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
    if not any(specs[i]["kind"] in ("agent", "consistency") for i in order):
        raise ValueError("this workflow has no agent step to run")

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


def _step_brief(run: dict, snapshot: dict, spec: dict, position: int,
                total: int) -> str:
    """The brief the seat actually receives — the step's own brief plus the run
    context, so a seat knows which run it is inside and can be steered back to
    it. Without the run_id an agent's work is unattributable to the graph."""
    task = _task_text(snapshot)
    head = (f"Workflow run #{run['id']} — \"{run['name']}\", "
            f"step {position} of {total}: {spec['label']}.")
    body = spec.get("brief") or f"Carry out the {spec['label']} step of this workflow."
    tail = (f"\n\nThis step is one node of a persisted workflow run; the run is "
            f"blocked on it. Finish with queue_complete and a summary of what "
            f"this step produced — the run advances on that.")
    if task:
        return f"{head}\n\nTASK / COMPLAINT (the run's north star):\n\"{task}\"\n\n{body}{tail}"
    return f"{head}\n\n{body}{tail}"


def _queue_step(root: str | os.PathLike[str], run: dict, snapshot: dict,
                spec: dict, position: int, total: int, dispatch: bool) -> dict:
    """One agent step -> one queue item, tagged so we can find it again."""
    seat = spec.get("seat") or ""
    run_id = int(run["id"])
    try:
        item = _queue.add(
            root, seat,
            title=f"{run['name']}: {spec['label']}"[:80],
            brief=_step_brief(run, snapshot, spec, position, total),
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
    """Tick the run: absorb finished work, then take the next step it can take.

    Deliberately sequential — one queue item in flight at a time. A workflow the
    user drew as a chain should behave like a chain, and a gate in the middle
    must be able to stop everything after it.
    """
    run = _run_row(root, run_id)
    if run["status"] != "running":
        return get(root, run_id)

    snapshot = json.loads(run["graph_json"] or "{}")
    specs = {n["id"]: n for n in (snapshot.get("nodes") or []) if isinstance(n, dict)}
    order = [i for i in (snapshot.get("order") or list(specs)) if i in specs]
    ups = _upstream(snapshot.get("edges") or [], order)
    if dispatch is None:
        dispatch = bool((snapshot.get("options") or {}).get("dispatch"))

    node_rows = _sync_items(root, run_id, specs, _node_rows(root, run_id), ups)

    failed = False
    waiting = False
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
            break
        # pending — can it start?
        stuck = [u for u in ups.get(node_id, ())
                 if node_rows.get(u) and node_rows[u]["status"] not in ("passed", "skipped")]
        if stuck:
            # Topological order guarantees parents come first, so a parent that
            # is neither done nor skipped here means the graph has a cycle.
            _set_node(root, run_id, node_id, "failed",
                      message=f"this step can never start — it depends on "
                              f"{stuck[0]}, which depends back on it (cycle)")
            failed = True
            break
        spec = specs[node_id]
        kind = spec.get("kind") or "passive"
        if kind == "passive":
            node_rows[node_id] = _set_node(
                root, run_id, node_id, "passed",
                message="no agent work — carried straight through")
            continue
        if kind == "gate":
            node_rows[node_id] = _set_node(
                root, run_id, node_id, "running",
                message="BLOCKED — waiting for a human to approve this gate")
            waiting = True
            break
        node_rows[node_id] = _queue_step(root, run, snapshot, spec, position,
                                         len(order), dispatch)
        if node_rows[node_id]["status"] == "failed":
            failed = True
        else:
            waiting = True
        break

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
    row = _node_rows(root, run_id).get(node_id)
    if row is None:
        raise LookupError(f"run {run_id} has no node {node_id!r}")
    if row["kind"] != "gate":
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
