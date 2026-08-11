"""The world surface: the design bible, the lore graph, canon.

The audit's verdict was that the dashboard is a viewer over a store only agents
can write — bible.add/update/remove and lore.link/add_fact/canon.check were
MCP-only, so the producer authored the design document by asking the thing it
constrains to write it. These are the write paths, and they are gates rather
than pass-throughs:

* bible mutations require a human actor. An agent must not be able to edit the
  document that bounds it.
* every narrative write runs canon.check first. A ``conflict`` verdict is a 409
  carrying the flags; a human may pass ``override`` after reading them, an agent
  may not. Checking and then writing anyway is what made the old gate a
  formality.

/api/scope, /api/scope/check and /api/scope/assign lived here until 2026-08-10,
feeding the three cut-line panels of the World view. The gate behind them never
refused an item, so the panels and the routes went together.

Auto-registers via routes/__init__.py. Errors are the api.py envelope: ValueError
from core is a 400, LookupError a 404.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request

from bgate_core import bible as _bible
from bgate_core import canon as _canon
from bgate_core import lore as _lore
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

# Long enough for a scene, short enough that a runaway paste cannot pin the
# checker on an O(facts x sentences) scan.
MAX_TEXT = 20_000


def _entity(ref: str) -> dict:
    try:
        return _lore.get_entity(root(), int(ref) if ref.isdigit() else ref)
    except LookupError:
        raise api.not_found(f"no lore entity {ref!r}", ref=ref)


def _section(section_id: int) -> dict:
    try:
        return _bible.get(root(), section_id)
    except LookupError:
        raise api.not_found(f"no bible section {section_id}", section_id=section_id)


# ---------------------------------------------------------------------------
# The design bible
# ---------------------------------------------------------------------------

@router.get("/api/bible")
def bible_read(kind: Optional[str] = None) -> dict:
    """The grouped overview plus the flat, rank-ordered section list the editor
    reorders against."""
    r = root()
    if kind:
        if kind not in _bible.KINDS:
            raise api.bad_request(f"kind must be one of {_bible.KINDS}", kind=kind)
        return api.ok({"kind": kind, "sections": _bible.list_sections(r, kind)})
    return api.ok({**_bible.overview(r), "sections": _bible.list_sections(r),
                   "kinds": list(_bible.KINDS)})


@router.post("/api/bible")
def bible_add(request: Request, payload: dict) -> dict:
    api.require_human(api.current_actor(request), "write the design bible")
    title = (payload.get("title") or "").strip()
    if not title:
        raise api.bad_request("a section needs a title")
    try:
        return api.ok(_bible.add(root(), (payload.get("kind") or "").strip(), title,
                                 body=payload.get("body", ""),
                                 rank=int(payload.get("rank") or 0)))
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.patch("/api/bible/{section_id}")
def bible_update(request: Request, section_id: int, payload: dict) -> dict:
    api.require_human(api.current_actor(request), "edit the design bible")
    _section(section_id)
    fields = {k: payload[k] for k in ("title", "body", "rank")
              if k in payload and payload[k] is not None}
    if not fields:
        raise api.bad_request("nothing to change — send title, body or rank")
    if "rank" in fields:
        fields["rank"] = int(fields["rank"])
    try:
        return api.ok(_bible.update(root(), section_id,
                                    expected_version=payload.get("version"),
                                    **fields))
    except _bible.StaleWrite as exc:
        # Every read hands out a version; a writer that sends a stale one is
        # about to erase somebody's edit. 409 with both sides, not a 500.
        raise api.conflict(str(exc), expected=exc.expected, actual=exc.actual)
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.delete("/api/bible/{section_id}")
def bible_remove(request: Request, section_id: int) -> dict:
    """Delete a section.

    This used to take ``reassign_to``/``force`` and 409 while work items were
    filed under the section. Only scope tiers were ever pointed at, and that
    column is gone, so there is nothing left to negotiate.
    """
    api.require_human(api.current_actor(request), "delete a design bible section")
    _section(section_id)
    try:
        return api.ok(_bible.remove(root(), section_id))
    except LookupError as exc:
        raise api.not_found(str(exc))


@router.post("/api/bible/reorder")
def bible_reorder(request: Request, payload: dict) -> dict:
    """Rewrite one kind's ranks to the given id order, atomically — a
    half-applied reorder is a bible that argues with itself. See bible.reorder."""
    api.require_human(api.current_actor(request), "reorder the design bible")
    order = payload.get("order")
    if not isinstance(order, list) or not order:
        raise api.bad_request("order must be a non-empty list of section ids")
    try:
        return api.ok(_bible.reorder(root(), (payload.get("kind") or "").strip(),
                                     [int(i) for i in order]))
    except (TypeError, ValueError) as exc:
        raise api.bad_request(str(exc))


# ---------------------------------------------------------------------------
# Canon — the gate itself
# ---------------------------------------------------------------------------

@router.post("/api/canon/check")
def canon_check(payload: dict) -> dict:
    """Run the deterministic lexical checks over ``text``. Same semantics as the
    MCP tool: ok / review / conflict, plus the canon that was consulted."""
    text = payload.get("text") or ""
    if not text.strip():
        raise api.bad_request("text is required")
    if len(text) > MAX_TEXT:
        raise api.bad_request(f"text is {len(text)} chars; the limit is {MAX_TEXT}",
                              limit=MAX_TEXT)
    entities = payload.get("entities") or None
    if entities is not None and not isinstance(entities, list):
        raise api.bad_request("entities must be a list of slugs, names or ids")
    return api.ok(_canon.check(root(), text, entities))


def _gated(request: Request, text: str, payload: dict,
           entities: Optional[list] = None) -> dict:
    """Run the canon gate over a narrative write and refuse a hard conflict.

    ``override`` exists because the checks are lexical and will occasionally be
    wrong — but only a human may use it, and only after the flags have been
    returned to them once.
    """
    if len(text) > MAX_TEXT:
        raise api.bad_request(f"text is {len(text)} chars; the limit is {MAX_TEXT}",
                              limit=MAX_TEXT)
    verdict = _canon.check(root(), text, entities)
    if verdict["verdict"] != "conflict":
        return verdict
    if payload.get("override"):
        api.require_human(api.current_actor(request), "override a canon conflict")
        verdict["overridden"] = True
        return verdict
    raise api.conflict("this write conflicts with established canon — read the "
                       "flags, then fix it or override",
                       **verdict)


# ---------------------------------------------------------------------------
# Lore
# ---------------------------------------------------------------------------

@router.get("/api/lore")
def lore_list(page: api.Page = Depends(), kind: Optional[str] = None,
              status: Optional[str] = None, graph: bool = True) -> dict:
    """The entity list, plus the ``{nodes, edges}`` graph a node canvas renders.

    The graph is whole-view on purpose: paginating a graph produces edges into
    nodes that are not there. ``page`` bounds the flat list only.
    """
    r = root()
    if kind and kind not in _lore.KINDS:
        raise api.bad_request(f"kind must be one of {_lore.KINDS}", kind=kind)
    if status and status not in _lore.STATUSES:
        raise api.bad_request(f"status must be one of {_lore.STATUSES}", status=status)
    entities = _lore.list_entities(r, kind=kind, status=status)
    body = page.envelope(page.slice(entities), len(entities))
    if graph:
        body["graph"] = _lore.graph(r, kind=kind, status=status)
    return body


@router.get("/api/lore/{ref}")
def lore_brief(ref: str) -> dict:
    """Everything about one entity — the record, its facts, its edges."""
    return api.ok(_lore.brief(root(), _entity(ref)["id"]))


@router.post("/api/lore")
def lore_add(request: Request, payload: dict) -> dict:
    """Create an entity. The summary and body pass the canon gate first."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise api.bad_request("name is required")
    summary, body = payload.get("summary", ""), payload.get("body", "")
    status = payload.get("status", "draft")
    if status in ("canon", "retired"):
        api.require_human(api.current_actor(request), f"declare an entity {status}")
    gate = _gated(request, f"{summary}\n{body}".strip(), payload)
    try:
        entity = _lore.add_entity(root(), (payload.get("kind") or "").strip(), name,
                                  summary=summary, body=body, status=status)
    except ValueError as exc:
        # A duplicate slug is a conflict, not a malformed request.
        if "already exists" in str(exc):
            raise api.conflict(str(exc), name=name)
        raise api.bad_request(str(exc))
    return api.ok(entity, canon=gate)


@router.patch("/api/lore/{ref}")
def lore_update(request: Request, ref: str, payload: dict) -> dict:
    """Edit prose or move an entity's status. Promoting to canon (or retiring it)
    is a human call — status is what canon_check reads to refuse a write."""
    entity = _entity(ref)
    status = payload.get("status")
    if status is not None and status != entity["status"]:
        api.require_human(api.current_actor(request),
                          f"change {entity['slug']} to {status}")
    fields = {k: payload[k] for k in ("summary", "body", "status")
              if k in payload and payload[k] is not None}
    if not fields:
        raise api.bad_request("nothing to change — send summary, body or status")
    prose = "\n".join(str(fields[k]) for k in ("summary", "body") if k in fields)
    gate = _gated(request, prose.strip(), payload,
                  entities=[entity["slug"]]) if prose.strip() else None
    try:
        return api.ok(_lore.update_entity(root(), entity["id"], **fields), canon=gate)
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.post("/api/lore/{ref}/facts")
def lore_add_fact(request: Request, ref: str, payload: dict) -> dict:
    """Assert one atomic fact. This is the write canon_check exists to protect:
    a statement that contradicts a locked fact is refused here."""
    entity = _entity(ref)
    statement = (payload.get("statement") or "").strip()
    if not statement:
        raise api.bad_request("statement is required")
    if payload.get("locked"):
        api.require_human(api.current_actor(request), "lock a fact as immovable canon")
    gate = _gated(request, statement, payload, entities=[entity["slug"]])
    fact = _lore.add_fact(root(), entity["id"], statement,
                          source=payload.get("source", ""),
                          locked=bool(payload.get("locked")))
    return api.ok(fact, canon=gate)


@router.get("/api/lore/{ref}/facts")
def lore_facts(ref: str) -> dict:
    return api.ok(_lore.facts_of(root(), _entity(ref)["id"]))


@router.post("/api/lore/link")
def lore_link(payload: dict) -> dict:
    """Draw an edge. Idempotent on (src, rel, dst) — re-linking updates the note
    rather than duplicating the edge, which is what a canvas drag will do."""
    src, dst = (payload.get("src") or "").strip(), (payload.get("dst") or "").strip()
    rel = (payload.get("rel") or "").strip()
    if not (src and dst and rel):
        raise api.bad_request("src, rel and dst are required")
    a, b = _entity(src), _entity(dst)
    if a["id"] == b["id"]:
        raise api.bad_request("an entity cannot link to itself", src=a["slug"])
    return api.ok(_lore.link(root(), a["id"], rel, b["id"],
                             note=payload.get("note", "")))
