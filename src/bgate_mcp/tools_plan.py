"""Domain-plan MCP tools.

Same contract as the other domain modules: the plumbing comes back from
server, server star-imports this at its bottom, and the logic lives in
bgate_core.design.domainplan so a dashboard route calls the same functions.

A seat writes its own discipline's plan (art writes art and animation,
gameplay writes gameplay, level and ui); the director and the human write
any. Filing plan rows onto the board is plan_promote, and that is the
director's - the same rule as the rest of the board's scope.
"""
from bgate_core.design import domainplan as _dp
from bgate_mcp.server import (  # noqa: F401
    Optional, _actor, _caller_is_agent, _fail,
    _root, _seat, _tool,
)


@_tool
def plan_cut(name: str, why: str, undo: bool = False) -> dict:
    """Deliberately NOT build a plan row, on the record. DIRECTOR.

    The row reads 'cut': milestones and locks count it as settled, and the
    reason lands on the not-building list. Refused while an item is building
    it. undo=True restores it.
    Full notes: docs/tools.md#plan_cut
    """
    from bgate_core.design import gameplan as _gameplan
    try:
        if _caller_is_agent() and (_seat() or "") != "director":
            return {"ok": False, "refused": "director_only",
                    "error": "cutting scope is the director's call - name the "
                             "row and why in your result note"}
        if undo:
            return _gameplan.uncut(_root(), name)
        return _gameplan.cut(_root(), name, why, by=_actor())
    except (ValueError, LookupError) as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def domain_plan_template(domain: str) -> dict:
    """What a plan for one discipline must decide, and its entry shape.

    domain: gameplay | level | art | animation | audio | narrative |
    cinematic | ui | tech | qa. Read this BEFORE writing a plan: it lists the
    questions the plan has to answer and the fields every deliverable needs
    (a mechanic needs verb, feedback and tunables; a cue needs type and
    trigger).
    Full notes: docs/tools.md#domain_plan_template
    """
    try:
        return {"ok": True, **_dp.template(domain)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def domain_plan_set(domain: str, goal: str = "",
                    done_when: Optional[list[str]] = None,
                    entries: Optional[list[dict]] = None,
                    leaves_dark: Optional[list[str]] = None,
                    open_questions: Optional[list[str]] = None,
                    merge: bool = True) -> dict:
    """Write or revise a discipline's plan: its END STATE and deliverables.

    goal: what this domain's part of the FINISHED game is (>= 40 chars).
    done_when: 1-8 binary checks that settle the whole domain.
    entries: the deliverables, each {name, acceptance, slice, depends_on,
    + the domain's own fields from domain_plan_template}. They compile into
    plan rows, so plan_status and the slice check count them.
    leaves_dark: what the domain deliberately does not do.
    open_questions: what you could not decide - state it, do not guess.
    merge=True (default) upserts entries by name and keeps the rest; pass
    merge=False to replace the entry list. Returns the cross-domain findings
    for this domain (a mechanic nobody teaches, a mechanic with no sound).
    A seat writes its own domain; the director writes any.
    Full notes: docs/tools.md#domain_plan_set
    """
    try:
        seat = _seat() if _caller_is_agent() else ""
        if not _dp.may_write(seat, domain):
            return {"ok": False, "refused": "not_your_domain",
                    "error": f"the {domain} plan belongs to the "
                             f"{_dp.DOMAINS[domain]['seat']} seat (or the "
                             "director). Say what it should change in your "
                             "result note or queue_add it to that seat."}
        plan = {"goal": goal, "done_when": done_when or [],
                "entries": entries or [], "leaves_dark": leaves_dark or [],
                "open_questions": open_questions or []}
        if merge:
            plan = {k: v for k, v in plan.items() if v not in ("", [])}
        return _dp.set_plan(_root(), domain, plan, by=_actor() or seat,
                            merge=bool(merge))
    except (ValueError, LookupError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return _fail(exc)


@_tool
def domain_plan_status(domain: str = "") -> dict:
    """Each discipline's plan against the live build, plus cross-domain findings.

    No domain: every discipline's goal, coverage (entries in the game / total),
    open questions and the rows not yet in the game. With a domain: that
    plan in full, every entry with its live state. `findings` reads the plans
    against each other - read it before deciding what to build next.
    Full notes: docs/tools.md#domain_plan_status
    """
    try:
        return {"ok": True, **_dp.status(_root(), domain)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def plan_promote(names: list[str], priority: int = 5) -> dict:
    """File spec plan rows onto the board, in dependency order. DIRECTOR.

    The next tranche after the slice: each item carries the row's
    acceptance and its domain's goal, with real depends_on links. A row
    whose dependency is still spec must be promoted in the same call.
    Full notes: docs/tools.md#plan_promote
    """
    try:
        if _caller_is_agent() and (_seat() or "") != "director":
            return {"ok": False, "refused": "director_only",
                    "error": "promoting plan rows is the director's call - name "
                             "the rows in your result note instead"}
        return _dp.promote(_root(), list(names or []), priority=int(priority))
    except (ValueError, LookupError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return _fail(exc)


@_tool
def domain_plan_check(domain: str, check: str, verdict: str,
                      evidence: str) -> dict:
    """Record a verdict on one of a domain plan's done_when checks. QA/DIRECTOR.

    check: the check's 0-based index or its exact text. verdict: pass | fail.
    evidence: what you ran and what it showed (tool result, test count,
    screenshot path) - at least 20 chars. A domain is complete only when
    every check passes on evidence recorded AFTER its work last moved; a
    pass recorded earlier reads 'stale'. A fail flags the plan for revision.
    The seat that built a domain does not grade it.
    Full notes: docs/tools.md#domain_plan_check
    """
    try:
        seat = (_seat() or "") if _caller_is_agent() else ""
        if seat and seat not in _dp.CHECKERS:
            return {"ok": False, "refused": "not_a_checker",
                    "error": "done_when verdicts are recorded by qa or the "
                             "director - the seat that built a domain does not "
                             "grade it. Name the evidence in your result note."}
        return _dp.record_check(_root(), domain, check, verdict, evidence,
                                by=_actor() or seat)
    except (ValueError, LookupError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return _fail(exc)


@_tool
def domain_plan_draft(domain: str) -> dict:
    """Start a plan from what the project already holds. Writes nothing.

    Returns the template, the current plan, entries drafted from existing
    material (narrative: quests; cinematic: storyboards; audio: the hooks
    the code already calls; gameplay: the encounter roster and objectives as
    context), and the other disciplines' plans to plan against. Drafted
    entries may have empty fields - those are decisions to make before
    domain_plan_set accepts them.
    Full notes: docs/tools.md#domain_plan_draft
    """
    try:
        return {"ok": True, **_dp.draft(_root(), domain)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
