"""The decision register and the no-list, over HTTP.

The director workspace's central panel is the design's headline idea and it has
been empty since it shipped, because the two lists it draws had no backend: they
were read out of the generic per-seat document store, which no tool can append
to. ``bgate_core.design.decisions`` is the store now; this is the door the dashboard
comes through. Auto-registers via routes/__init__.py; envelope and errors per
bgate_ui/api.py.

WHO MAY WRITE WHAT, AND WHY IT IS NOT ONE RULE FOR THE WHOLE FILE.

``routes/project.py`` gates creation with ``require_human`` because creating a
studio's project is wholly a human act. Applying that same blanket rule here
would be wrong, and would defeat the feature: the no-list exists precisely so
that agents READ it before building the no, and a register only agents are
locked out of is one they will not populate and therefore not consult. So the
gate is placed per TRANSITION, not per endpoint — which is the shape
``routes/world.py`` already uses, where anyone may add a lore entity but only a
human may declare one canon:

  read anything            open. This is the entire point. An agent that cannot
                           read the no-list will build the no.
  POST a decision, open    open. A proposal is an agent saying "this needs a
                           ruling"; it lands in Awaiting a ruling and binds
                           nobody until a human settles it.
  POST a decision, settled HUMAN. A settled decision binds every other seat.
                           An agent that can settle its own decisions is an
                           agent that can authorise its own work, which is the
                           same hole the seat-lane gate exists to close.
  settle / supersede       HUMAN. Same reason.
  POST a refusal           HUMAN, and this one is the least obvious call in the
                           file. A refusal is READ AS BINDING by every agent
                           that lists it, and it has no acceptance test anybody
                           could use to check it was right — so an agent-written
                           no is an unreviewable instruction to every future
                           agent, filed by something that cannot be held
                           accountable for it. An agent that wants to refuse
                           something files a decision with state='open' saying
                           so, and a human turns it into a line on the rail.
  DELETE a refusal         HUMAN. Lifting a no releases work that was stopped.

An agent hitting a human-only endpoint gets a 403 whose message names the verb
it should have used instead, because a permission error that does not say what
to do next produces an agent that retries the same call.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from bgate_core.design import decisions as _decisions
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


def _int(payload: dict, key: str) -> Optional[int]:
    """An optional numeric link off a JSON body. A blank form field posts "",
    which int() would raise on — that is a missing link, not a bad request."""
    raw = payload.get(key)
    if raw in (None, "", 0):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise api.bad_request(f"{key} must be a number", **{key: raw})


# ---------------------------------------------------------------------------
# Reads. Open to everyone, deliberately.
# ---------------------------------------------------------------------------

@router.get("/api/decisions/overview")
def decisions_overview() -> dict:
    """Both lists and the open proposals, in one request.

    DECLARED BEFORE ``/api/decisions/{decision_id}``, and the ordering is
    load-bearing: FastAPI matches in declaration order, so the wildcard would
    otherwise take "overview" as an id, fail to parse it as an int and answer
    422 for a route that exists.

    NOT PAGINATED, unlike the flat list below. This is the director panel's
    single read and the panel is meaningless in pieces — a register that stops
    at row 100 is one where the reader cannot tell whether the thing they are
    about to build was refused on row 140. If a project ever files enough
    rulings for that to hurt, the fix is a filter, not a truncation.
    """
    return api.ok(_decisions.overview(root()))


@router.get("/api/decisions")
def decisions_list(page: api.Page = Depends(), state: str = Query(""),
                   work_item_id: Optional[int] = None) -> dict:
    """The register, newest first. ``state`` filters to one of settled | open |
    superseded; ``work_item_id`` to the rulings about one piece of work."""
    try:
        rows = _decisions.list_decisions(root(), state=state,
                                         work_item_id=work_item_id)
    except ValueError as exc:
        raise api.bad_request(str(exc), state=state)
    return page.apply(rows)


@router.get("/api/decisions/{decision_id}")
def decision_read(decision_id: int) -> dict:
    try:
        return api.ok(_decisions.get(root(), decision_id))
    except LookupError as exc:
        raise api.not_found(str(exc), decision_id=decision_id)


@router.get("/api/not-building")
def not_building_list(page: api.Page = Depends(), tag: str = Query("")) -> dict:
    """What this project has said no to. Read it before filing work."""
    return page.apply(_decisions.list_not_building(root(), tag=tag))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

@router.post("/api/decisions")
def decision_create(request: Request, payload: dict) -> dict:
    """File a decision. ``state`` is 'settled' (a ruling) or 'open' (a proposal).

    The human gate is on the STATE, not on the endpoint — see the module
    docstring. An agent may propose all day; only a human may settle.
    """
    state = (payload.get("state") or "settled").strip().lower()
    if state not in _decisions.STATES:
        raise api.bad_request(
            f"state must be one of {' | '.join(_decisions.STATES)}", state=state)
    if state == "superseded":
        # Reachable only by a caller typing it into the body. There is no
        # decision to supersede this one, so the row would claim it was replaced
        # by nothing — a state the panel cannot draw honestly.
        raise api.bad_request(
            "a decision cannot be filed as superseded — file it, then "
            "POST /api/decisions/{id}/supersede naming what replaced it")
    actor = api.current_actor(request)
    if state == "settled":
        api.require_human(actor, "settle a decision")

    try:
        return api.ok(_decisions.add(
            root(),
            payload.get("title", ""),
            payload.get("acceptance", ""),
            payload.get("leaves_dark", ""),
            state=state,
            work_item_id=_int(payload, "work_item_id"),
            session_id=_int(payload, "session_id"),
            actor=actor,
        ))
    except ValueError as exc:
        # decisions.add's messages say what the missing field is FOR, which is
        # the only useful thing to put in front of whoever left it blank.
        raise api.bad_request(str(exc))


@router.post("/api/decisions/{decision_id}/settle")
def decision_settle(request: Request, decision_id: int) -> dict:
    """Turn a proposal into a ruling. The one act that binds the other seats."""
    api.require_human(api.current_actor(request), "settle a decision")
    try:
        return api.ok(_decisions.settle(root(), decision_id,
                                        actor=api.current_actor(request)))
    except LookupError as exc:
        raise api.not_found(str(exc), decision_id=decision_id)
    except ValueError as exc:
        # A superseded decision cannot be revived. Not a 404 — the row is right
        # there — and not a 400 either, because the request was well formed and
        # the state is what refused it.
        raise api.conflict(str(exc), decision_id=decision_id)


@router.post("/api/decisions/{decision_id}/supersede")
def decision_supersede(request: Request, decision_id: int, payload: dict) -> dict:
    """Record that this decision was replaced by another. Keeps both rows."""
    api.require_human(api.current_actor(request), "supersede a decision")
    by_id = _int(payload, "by_id")
    if by_id is None:
        raise api.bad_request(
            "by_id is required — superseding names what replaced this "
            "decision, and a replacement nobody named is a deletion")
    try:
        return api.ok(_decisions.supersede(root(), decision_id, by_id,
                                           actor=api.current_actor(request)))
    except LookupError as exc:
        raise api.not_found(str(exc), decision_id=decision_id, by_id=by_id)
    except ValueError as exc:
        raise api.bad_request(str(exc), decision_id=decision_id, by_id=by_id)


@router.post("/api/not-building")
def not_building_create(request: Request, payload: dict) -> dict:
    """Add a line to the no-list. Human only — see the module docstring."""
    actor = api.current_actor(request)
    if not api.is_human(actor):
        # Not api.require_human: the standard message says the action needs a
        # human and stops there, and an agent told only that will try again.
        # This one names the door that IS open to it.
        raise api.forbidden(
            f"{actor or 'an unidentified caller'} is an agent — the no-list is "
            "read as binding by every agent that lists it and has no acceptance "
            "test anybody can check it against, so only a human writes one. "
            "POST /api/decisions with state='open' to propose the refusal; it "
            "lands in Awaiting a ruling.",
            actor=actor, propose="/api/decisions")
    try:
        return api.ok(_decisions.refuse(
            root(),
            payload.get("text", ""),
            payload.get("reason", ""),
            tag=payload.get("tag", ""),
            decision_id=_int(payload, "decision_id"),
            actor=actor,
        ))
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.delete("/api/not-building/{not_building_id}")
def not_building_delete(request: Request, not_building_id: int) -> dict:
    """Lift a refusal. Human only: this releases work that was stopped."""
    api.require_human(api.current_actor(request), "lift a refusal")
    try:
        return api.ok(_decisions.unrefuse(root(), not_building_id,
                                          actor=api.current_actor(request)))
    except LookupError as exc:
        raise api.not_found(str(exc), not_building_id=not_building_id)
