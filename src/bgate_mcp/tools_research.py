"""Pre-production research MCP tools.

Same contract as the other domain modules: the plumbing comes back from
server, server star-imports this at its bottom, and the logic lives in
bgate_core.design.research so a dashboard route calls the same functions.

Every tool here writes only the research tables and the draft plan, with ONE
exception: research_plan_adopt writes the bible, the not-building list, the
decision log and the thesis, and it is the only one a machine may not call -
the brainstorm_deploy rule, for the same reason. A plan an agent drafted and
then adopted is the review step reviewing itself.
"""
from bgate_core.design import research as _research
from bgate_mcp.server import (  # noqa: F401
    Optional, _actor, _caller_is_agent, _fail,
    _root, _tool,
)


def _err(exc: Exception) -> dict:
    return {"ok": False, "error": str(exc)}


@_tool
def research_status() -> dict:
    """Where pre-production research stands, and the next step.

    The comparables and their status (proposed/confirmed/dropped/researched),
    how many findings each has, whether a plan is drafted or adopted, and what
    to do next. Start here.
    Full notes: docs/tools.md#research_status
    """
    return _research.status(_root())


@_tool
def research_suggest() -> dict:
    """Propose 5-8 comparable games for this project, from the pitch and brief.

    Spawns a research agent with web search (and no other tools). The games
    land as PROPOSED: confirm or drop each with research_comp_set before any
    teardown is spent on it - show the list to the human first. Takes a
    minute or more.
    Full notes: docs/tools.md#research_suggest
    """
    try:
        return _research.suggest(_root())
    except (ValueError, RuntimeError) as exc:
        return _err(exc)


@_tool
def research_comp_add(title: str, why: str = "", relation: str = "loop") -> dict:
    """Add a comparable game by hand, already confirmed.

    relation: loop | setting | audience | art | cautionary | other - WHY it is
    comparable. A title already on the list is returned as it is.
    Full notes: docs/tools.md#research_comp_add
    """
    try:
        return _research.add_comp(_root(), title, why, relation,
                                  status="confirmed", by=_actor())
    except ValueError as exc:
        return _err(exc)


@_tool
def research_comp_set(comp_id: int, status: str) -> dict:
    """Confirm or drop a comparable: status = confirmed | dropped | proposed.

    Only confirmed comparables can be torn down. Dropped ones leave the grid
    and the findings search but are kept, so they are not re-suggested.
    Full notes: docs/tools.md#research_comp_set
    """
    try:
        return _research.set_status(_root(), comp_id, status)
    except (ValueError, LookupError) as exc:
        return _err(exc)


@_tool
def research_teardown(comp_id: int) -> dict:
    """Research ONE confirmed comparable, system by system, from real sources.

    Spawns a web-search research agent that judges each system (core loop,
    progression, economy, onboarding, art style, ...) as worked / failed /
    mixed, with a confidence and the sources. Replaces that comparable's
    earlier findings. Several minutes per game; run one per call.
    Full notes: docs/tools.md#research_teardown
    """
    try:
        return _research.teardown(_root(), comp_id)
    except (ValueError, LookupError, RuntimeError) as exc:
        return _err(exc)


@_tool
def research_findings(system: str = "", comp_id: int = 0, verdict: str = "",
                      query: str = "", limit: int = 100) -> dict:
    """Cited findings from the comparables research. Ask it mid-production too.

    "How did the comparables handle meta-progression?" is
    research_findings(system="progression"). verdict: worked | failed | mixed.
    query is a substring over the claim and evidence. Each row carries its
    sources and a confidence - treat "low" as a lead, not a fact.
    Full notes: docs/tools.md#research_findings
    """
    try:
        rows = _research.findings(_root(), system=system, comp_id=comp_id or None,
                                  verdict=verdict, query=query, limit=limit)
    except ValueError as exc:
        return _err(exc)
    return {"findings": rows, "count": len(rows)}


@_tool
def research_grid() -> dict:
    """Systems x comparables: each cell is a verdict, the last column is ours.

    The comparison the plan is drawn from. "ours" is filled once a plan is
    drafted: keep / avoid / twist, and which comparable taught us.
    Full notes: docs/tools.md#research_grid
    """
    return _research.grid(_root())


@_tool
def research_plan_draft(notes: str = "") -> dict:
    """Turn the pitch, the brief and the findings into a proposed plan. A DRAFT.

    Pillars, core loop, setting, art direction, a stance per system, what not
    to build, the open decisions only the human can answer, and a thesis if
    the pitch supports one. Saved as the draft (research_plan) and NOTHING
    else is written - hand it to the human, who adopts it. notes: steer from
    the human ("keep it cozy", "no crafting").
    Full notes: docs/tools.md#research_plan_draft
    """
    try:
        return _research.draft_plan(_root(), notes)
    except (ValueError, RuntimeError) as exc:
        return _err(exc)


@_tool
def research_plan() -> dict:
    """The current research plan: drafted, adopted, or empty.
    Full notes: docs/tools.md#research_plan
    """
    return {"plan": _research.plan(_root()),
            "state": _research.status(_root())["plan"]}


@_tool
def research_plan_adopt(plan: Optional[dict] = None, again: bool = False) -> dict:
    """Write the approved plan into the bible. HUMAN-ONLY.

    Pillars, core loop, setting and art direction become bible sections
    (upserted by title), not_building entries are recorded, decisions are
    filed OPEN for the human to settle, and a valid thesis is set. `plan` is
    the draft as the human edited it; omit it to adopt the draft unchanged.
    A machine is refused. again=True writes a re-adopted plan's not-building
    entries and decisions a second time.
    Full notes: docs/tools.md#research_plan_adopt
    """
    if _caller_is_agent():
        return _err(PermissionError(
            f"{_actor() or 'an agent session'} may not adopt the research "
            "plan - it becomes the bible, and the human whose game it is "
            "reads it first. Hand over research_plan's draft instead; "
            "ask_human gets you an answer without blocking."))
    try:
        return {"ok": True, **_research.adopt(_root(), plan, by=_actor() or "human",
                                              again=bool(again))}
    except ValueError as exc:
        return _err(exc)
