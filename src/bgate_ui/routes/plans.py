"""Domain plans, over HTTP - the dashboard's Plan tab on every seat.

Read-only plus one write: promoting spec rows to the board, which is the
director's act and the human's at the dashboard. Plans themselves are written
through domain_plan_set (or a deployed brainstorm) so the seat scope and the
validator are one code path; an HTTP editor for them would be a second door.

Auto-registers via routes/__init__.py. Envelope and errors per bgate_ui/api.py.
"""
from __future__ import annotations

from bgate_core.design import domainplan as _dp
from fastapi import APIRouter, Body

from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/plans")
def plans_index(domain: str = "") -> dict:
    """Every discipline's plan against the live build, with findings."""
    try:
        out = _dp.status(root(), domain)
    except ValueError as exc:
        raise api.bad_request(str(exc))
    out["held"] = sorted(_dp.planning_held(root()))
    out["seat_domains"] = {d: s["seat"] for d, s in _dp.DOMAINS.items()}
    return api.ok(out)


@router.post("/api/plans/promote")
def plans_promote(payload: dict = Body(...)) -> dict:
    """File the named spec rows onto the board, in dependency order."""
    names = payload.get("names") if isinstance(payload, dict) else None
    if not isinstance(names, list) or not names:
        raise api.bad_request("promote takes {names: [plan row names]}")
    try:
        return api.ok(_dp.promote(root(), [str(n) for n in names]))
    except (ValueError, LookupError) as exc:
        raise api.bad_request(str(exc))
