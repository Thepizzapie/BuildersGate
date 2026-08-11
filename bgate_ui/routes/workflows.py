"""Workflow-run endpoints — start a run, tick it, resolve its gates.

The canvas paints itself from these. Two rules shape the surface:

  * the polling path (`POST .../advance`) returns node statuses only, never the
    graph — repainting must not re-ship what the browser already has. The graph
    comes back exactly once, from `GET .../runs/{id}?graph=1`, when a reloaded
    page re-attaches to a run it did not start;
  * approving a gate goes through ``api.require_human``, and so does starting a
    node that spends. A gate an agent can open is decoration, and decoration is
    what the audit found here — twice: the gate, and then the ▶ two nodes down
    that could buy a video without passing the gate at all.

Advancing is a POST rather than a side effect of the GET on purpose: a tick can
create queue items and (optionally) spawn a session, and GETs skip the token
guard so the browser can load static assets.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from bgate_core import wfnodes as _wfnodes
from bgate_core import workflows as _workflows
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter(prefix="/api/workflows")


@router.get("/nodes")
def node_catalogue() -> dict:
    """The tool-node table, so the palette builds itself from the server.

    The palette used to be forty-five hand-written cards in JavaScript, and
    behind most of them there was no executor at all — the card existed, the
    tool did not run. Now the SAME table that the executor calls with is what
    the browser draws, so a card can only exist for a tool this build really
    has, and an argument on a card is an argument the tool really takes. A typo
    in an argument name would otherwise surface as a 422 in the middle of a paid
    run, discovered by the user.

    ``tools`` are the nodes that call something; ``flow`` are the glue nodes
    that only move values around. They are separate lists because they are
    different promises: one can spend money and fail against a provider, the
    other cannot fail against anything but its own configuration.
    """
    return api.ok({"tools": _wfnodes.catalogue(),
                   "flow": _wfnodes.flow_catalogue()})


def _run_or_404(r, run_id: int, *, include_graph: bool = False) -> dict:
    try:
        return _workflows.get(r, run_id, include_graph=include_graph)
    except LookupError:
        raise api.not_found(f"no workflow run {run_id}", run_id=run_id)


@router.post("/runs")
def start_run(payload: dict, request: Request) -> dict:
    """Start a run of a compiled graph.

    body: ``{workflow:{id,name,category}, nodes:[{id,type,label,seat,brief,
    config}], edges:[...], dispatch?:bool}``. The client compiles seat + brief
    from its step registry; we snapshot that plan so the run stays readable
    after the workflow is edited.
    """
    r = root()
    actor = api.current_actor(request)
    dispatch = bool(payload.get("dispatch", True)) and api.dispatch_enabled()
    try:
        run = _workflows.start(
            r, payload, name=str(payload.get("name") or ""),
            seat=str(payload.get("seat") or ""), actor=actor, dispatch=dispatch)
    except ValueError as exc:
        raise api.bad_request(str(exc))
    return api.ok(run)


@router.get("/runs")
def list_runs(workflow_id: str = "", status: str = "", limit: int = 20) -> dict:
    return api.ok(_workflows.list_runs(root(), workflow_id=workflow_id,
                                       status=status, limit=limit))


@router.get("/runs/latest")
def latest_run(workflow_id: str, running_only: bool = True) -> dict:
    """What a freshly-loaded builder asks: 'is a run of this workflow live?'"""
    if not workflow_id.strip():
        raise api.bad_request("workflow_id is required")
    return api.ok(_workflows.latest_for_workflow(root(), workflow_id.strip(),
                                                 running_only=running_only))


@router.get("/runs/{run_id}")
def get_run(run_id: int, graph: bool = False) -> dict:
    return api.ok(_run_or_404(root(), run_id, include_graph=graph))


@router.post("/runs/{run_id}/advance")
def advance_run(run_id: int) -> dict:
    """The poll. Absorbs finished queue items and takes the next possible step."""
    r = root()
    try:
        return api.ok(_workflows.advance(r, run_id))
    except LookupError:
        raise api.not_found(f"no workflow run {run_id}", run_id=run_id)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, request: Request) -> dict:
    r = root()
    try:
        return api.ok(_workflows.cancel(r, run_id, actor=api.current_actor(request)))
    except LookupError:
        raise api.not_found(f"no workflow run {run_id}", run_id=run_id)


@router.post("/runs/{run_id}/nodes/{node_id}/approve")
def approve_gate(run_id: int, node_id: str, request: Request,
                 payload: Optional[dict] = None) -> dict:
    """Resolve a blocking gate. body: ``{decision:'approve'|'reject', note?}``."""
    payload = payload or {}
    r = root()
    actor = api.current_actor(request)
    api.require_human(actor, "resolving a workflow gate")
    try:
        run = _workflows.approve(
            r, run_id, node_id,
            decision=str(payload.get("decision") or "approve"),
            actor=actor, note=str(payload.get("note") or ""))
    except LookupError as exc:
        raise api.not_found(str(exc), run_id=run_id, node_id=node_id)
    except PermissionError as exc:
        raise api.ApiError(403, str(exc), code="forbidden")
    except ValueError as exc:
        raise api.conflict(str(exc), run_id=run_id, node_id=node_id)
    return api.ok(run)


@router.post("/runs/{run_id}/nodes/{node_id}/run")
def run_one_node(run_id: int, node_id: str, request: Request,
                 payload: Optional[dict] = None) -> dict:
    """Run exactly this node and stop — the ▶ on a node card.

    Nothing cascades: this is the step-by-step mode the multi-model comparison
    needs, where a human fans out, looks, picks, and only then continues. A node
    whose inputs are not satisfied is refused with which parent is missing
    rather than started on nothing.
    """
    payload = payload or {}
    r = root()
    actor = api.current_actor(request)
    dispatch = payload.get("dispatch")
    if dispatch is not None:
        dispatch = bool(dispatch) and api.dispatch_enabled()
    try:
        # ▶ ON A PAID CARD IS A SPENDING DECISION, so it carries the same guard
        # the gate and the pick routes carry. It did not: `actor` was passed
        # through to an engine that ignored it, and this was the only route from
        # which an agent could start a node that bills — a video shot, a music
        # track, a model card — while being refused at the gate beside it.
        if _workflows.spends_money(r, run_id, node_id):
            api.require_human(actor, "running a step that spends money")
        run = _workflows.run_node(r, run_id, node_id, actor=actor,
                                  dispatch=dispatch)
    except LookupError as exc:
        raise api.not_found(str(exc), run_id=run_id, node_id=node_id)
    except PermissionError as exc:
        raise api.ApiError(403, str(exc), code="forbidden")
    except ValueError as exc:
        raise api.conflict(str(exc), run_id=run_id, node_id=node_id)
    return api.ok(run)


@router.post("/runs/{run_id}/reconcile")
def reconcile_run(run_id: int) -> dict:
    """Release steps a dead process left mid-flight, so the run can end.

    The engine tracks in-flight workers in memory, so a restart during a
    generation leaves the node at 'running' with nothing left to finish it. The
    poll then waits for ever and an exclusive step holds everything behind it.
    This is the verb the "wait for it to finish" message points at.
    """
    try:
        return api.ok(_workflows.reconcile(root(), run_id))
    except LookupError:
        raise api.not_found(f"no workflow run {run_id}", run_id=run_id)


@router.post("/reconcile")
def reconcile_all() -> dict:
    """The same sweep across every live run — what a dashboard boot wants."""
    return api.ok(_workflows.reconcile(root()))


@router.get("/runs/{run_id}/nodes/{node_id}/candidates")
def node_candidates(run_id: int, node_id: str) -> dict:
    """What a pick node is choosing between — a picker with nothing to look at
    is a dialog box."""
    try:
        return api.ok(_workflows.candidates(root(), run_id, node_id))
    except LookupError as exc:
        raise api.not_found(str(exc), run_id=run_id, node_id=node_id)


@router.post("/runs/{run_id}/nodes/{node_id}/pick")
def pick_candidate(run_id: int, node_id: str, request: Request,
                   payload: Optional[dict] = None) -> dict:
    """Resolve a pick to a choice. body: ``{artifact_id}`` or ``{reject:true, note?}``.

    Human-only at both layers — here, and again inside the engine. A machine
    choosing which of its own outputs to promote is the failure the art-QA
    router already exists to prevent.
    """
    payload = payload or {}
    r = root()
    actor = api.current_actor(request)
    api.require_human(actor, "picking a workflow candidate")
    reject = bool(payload.get("reject"))
    artifact_id = payload.get("artifact_id")
    if not reject:
        try:
            artifact_id = int(artifact_id)
        except (TypeError, ValueError):
            raise api.bad_request(
                "artifact_id must be the id of one of this node's candidates "
                "(or pass reject=true to refuse them all)")
    try:
        run = _workflows.pick(r, run_id, node_id,
                              artifact_id=None if reject else artifact_id,
                              reject=reject, actor=actor,
                              note=str(payload.get("note") or ""))
    except LookupError as exc:
        raise api.not_found(str(exc), run_id=run_id, node_id=node_id)
    except PermissionError as exc:
        raise api.ApiError(403, str(exc), code="forbidden")
    except ValueError as exc:
        raise api.conflict(str(exc), run_id=run_id, node_id=node_id)
    return api.ok(run)


@router.post("/runs/{run_id}/nodes/{node_id}/observe")
def observe_node(run_id: int, node_id: str, payload: dict,
                 request: Request) -> dict:
    """Record a measured on-model score on a consistency node.

    body: ``{score: 0-100, detail?}``. Agents may report a measurement — the
    threshold, not the reporter, decides whether the run continues.
    """
    r = root()
    try:
        score = float(payload.get("score"))
    except (TypeError, ValueError):
        raise api.bad_request("score must be a number between 0 and 100")
    try:
        run = _workflows.observe(r, run_id, node_id, score=score,
                                 detail=str(payload.get("detail") or ""),
                                 actor=api.current_actor(request))
    except LookupError as exc:
        raise api.not_found(str(exc), run_id=run_id, node_id=node_id)
    except ValueError as exc:
        raise api.conflict(str(exc), run_id=run_id, node_id=node_id)
    return api.ok(run)


@router.get("/gates")
def pending_gates() -> dict:
    """Every gate currently blocking a live run — the approval inbox."""
    return api.ok(_workflows.pending_gates(root()))


@router.get("/picks")
def pending_picks() -> dict:
    """Every pick currently blocking a live run, with its candidates."""
    return api.ok(_workflows.pending_picks(root()))
