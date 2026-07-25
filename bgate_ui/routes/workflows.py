"""Workflow-run endpoints — start a run, tick it, resolve its gates.

The canvas paints itself from these. Two rules shape the surface:

  * the polling path (`POST .../advance`) returns node statuses only, never the
    graph — repainting must not re-ship what the browser already has. The graph
    comes back exactly once, from `GET .../runs/{id}?graph=1`, when a reloaded
    page re-attaches to a run it did not start;
  * approving a gate goes through ``api.require_human``. A gate an agent can
    open is decoration, and decoration is what the audit found here.

Advancing is a POST rather than a side effect of the GET on purpose: a tick can
create queue items and (optionally) spawn a session, and GETs skip the token
guard so the browser can load static assets.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from bgate_core import workflows as _workflows
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter(prefix="/api/workflows")


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
