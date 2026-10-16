"""The stage advance: the follow-up that re-enters the director after a gate.

NOTHING RE-ENTERED THE DIRECTOR AFTER THE SLICE CHECK. The slice check files
itself when the last slice row lands (gameplan.open_slice_check), the feel
check runs, the key risks sit named on the thesis - and then the board stood
at 'slice' until a human opened the console and said "retire the risks and
advance". Measured on the gnomefall kickoff (2026-10-15): every other step
of the one-shot had been automated and that one was still a hand on the
wheel.

This module is the due-check and the filing. It is OFF unless the project
says otherwise (``autopilot.stage_advance``, human-only; ``bgate oneshot
--auto-advance`` turns it on), because a stage move is the human's call and
switching it on is how they make it.

Two boundaries, one shape:

* slice -> production, when a SLICE CHECK item is done with ``VERDICT:
  PASS`` and the feel check has run (feel.blockers is empty: passed, or no
  bot can play this project);
* production -> release, when the presentation check has no unmet row.

The item goes to the DIRECTOR seat and is held from every auto-dispatcher
(queue.HELD_SOURCES): greenlight_advance refuses a forward move from any
agent, so a dispatched director-seat agent could not take it. The router
(bgate_ui.agents.followup) hands it to the console's director session, the
same way it hands a failure escalation; with no session known, it waits for
the dashboard, visibly.

IDEMPOTENT. One open stage-advance item per project, and never a second for
the same evidence: the ref carries the slice check's id, so a re-run slice
check that passes again earns a fresh item and an unchanged one does not.
"""
from __future__ import annotations

import os
from typing import Optional

from . import activity
from . import queue as _queue
from ..store import db
from ..store.util import rows

SOURCE = _queue.STAGE_ADVANCE_SOURCE
SETTING = "autopilot.stage_advance"
PRIORITY = 9
_LIVE = ("queued", "dispatched", "review")


def enabled(root: str | os.PathLike[str]) -> bool:
    try:
        from ..store import settings as _settings

        return bool(_settings.get(root, SETTING))
    except Exception:                                             # noqa: BLE001
        return False


def _items(root) -> list[dict]:
    try:
        return rows(db.connect(root).execute(
            "SELECT id, status, source_ref FROM work_item WHERE source = ? "
            "ORDER BY id DESC", (SOURCE,)))
    except Exception:                                             # noqa: BLE001
        return []


def _passed_slice_check(root) -> Optional[int]:
    """The id of the latest DONE slice check that ended 'VERDICT: PASS'."""
    from ..design import gameplan as _gameplan

    try:
        last = db.connect(root).execute(
            "SELECT id, result FROM work_item WHERE source = ? AND "
            "status = 'done' ORDER BY id DESC LIMIT 1",
            (_gameplan.SLICE_CHECK_SOURCE,)).fetchone()
    except Exception:                                             # noqa: BLE001
        return None
    if last is None or "VERDICT: PASS" not in str(last["result"] or ""):
        return None
    return int(last["id"])


def due(root: str | os.PathLike[str]) -> dict:
    """Should a stage-advance item be filed now? ``{due, why, frm, to, ref}``."""
    from ..design import greenlight as _gl

    if not enabled(root):
        return {"due": False, "why": f"{SETTING} is off"}
    existing = _items(root)
    live = [e for e in existing if e["status"] in _LIVE]
    if live:
        return {"due": False, "why": "a stage-advance item is already open",
                "open": int(live[0]["id"])}
    at = _gl.stage(root)
    if at == _gl.SLICE:
        check = _passed_slice_check(root)
        if check is None:
            return {"due": False, "why": "no slice check has passed"}
        try:
            from ..qa import feel as _feel

            held = _feel.blockers(root)
        except Exception as exc:                                  # noqa: BLE001
            held = [f"the feel check could not run ({type(exc).__name__})"]
        if held:
            return {"due": False, "why": "the feel check holds: " + held[0][:200]}
        ref = f"{_gl.SLICE}->{_gl.PRODUCTION}#{check}"
        to = _gl.PRODUCTION
    elif at == _gl.PRODUCTION:
        try:
            unmet = _gl.presentation_check(root).get("unmet") or []
        except Exception as exc:                                  # noqa: BLE001
            unmet = [f"the presentation check could not run ({type(exc).__name__})"]
        if unmet:
            return {"due": False, "why": f"{len(unmet)} presentation row(s) unmet"}
        ref = f"{_gl.PRODUCTION}->{_gl.RELEASE}"
        to = _gl.RELEASE
    else:
        return {"due": False, "why": f"the project is at {at!r}; nothing to advance on"}
    if any(str(e["source_ref"] or "") == ref for e in existing
           if e["status"] != "cancelled"):
        return {"due": False, "why": "an advance item was already filed for "
                                     "this evidence", "ref": ref}
    return {"due": True, "why": f"the {at} gate has cleared", "frm": at,
            "to": to, "ref": ref}


def brief_for(root, frm: str, to: str) -> str:
    from ..design import greenlight as _gl

    lines = [f"ADVANCE THE STAGE: {frm} -> {to}. You are the director; this "
             "item is held for you and nobody else can take it.", ""]
    if to == _gl.PRODUCTION:
        risks = _gl.risks(root)
        lines.append(
            "1. RETIRE EVERY KEY RISK ON THE BOARD'S EVIDENCE. For each risk "
            "below call greenlight_risk_retire(index, outcome, evidence) "
            "with outcome 'confirmed' or 'modified' and evidence that cites "
            "the item, test, feel-check result or screenshot that settled "
            "it (queue_list / queue_get, playtest_feel_check results, the "
            "slice check's result note). NEVER 'accepted' - that is the "
            "human's call; a risk the evidence does not settle gets "
            "ask_human, not a guess.")
        if risks:
            for i, r in enumerate(risks):
                lines.append(f"   risk {i} [{r.get('status', 'open')}]: "
                             f"{r.get('hypothesis', '')}"
                             + (f" (retire by: {r['retire_by']})"
                                if r.get("retire_by") else ""))
        else:
            lines.append("   (no key risks are named - greenlight_risks_set "
                         "with 1-4 the slice settled, then retire them)")
        lines.append(
            "2. greenlight_advance('production'). If it refuses, the refusal "
            "lists what is owed; do that or file it, do not argue with it.")
        lines.append(
            "3. plan_promote EVERY remaining spec row (plan_status lists "
            "them; domain_plan_status per domain) so production fans out "
            "without another hand. Dependencies that are still spec go in "
            "the same call.")
    else:
        lines.append(
            "1. greenlight_status('presentation') - every row must read met; "
            "this gate takes no waiver.")
        lines.append(
            "2. greenlight_advance('release'). If it refuses, the refusal "
            "lists the rows still owed; file the work, do not argue with it.")
    lines.append("")
    lines.append("Your result note is the record: name each risk's outcome and "
                 "the stage you left the project at.")
    return "\n".join(lines)


def file_item(root: str | os.PathLike[str], state: dict) -> dict:
    frm, to, ref = state["frm"], state["to"], state["ref"]
    item = _queue.add(root, "director", f"ADVANCE: {frm} -> {to}",
                      brief=brief_for(root, frm, to), priority=PRIORITY,
                      source=SOURCE, source_ref=ref,
                      acceptance=f"greenlight_status reads stage={to}")
    activity.log(root, "greenlight",
                 f"stage advance filed as #{item['id']}: {state['why']}",
                 seat="director", ref=str(item["id"]))
    return item


def sweep(root: str | os.PathLike[str]) -> Optional[dict]:
    """File the advance item if it is due. Returns it, or None."""
    state = due(root)
    if not state.get("due"):
        return None
    return file_item(root, state)


def prompt_for(root, item: dict) -> str:
    """What the director session is told when it takes the item."""
    return (f"{item.get('title', '')}\n\n{item.get('brief', '')}\n\n----\n\n"
            "You are handling this as the director session: the item is on "
            f"the board as #{int(item.get('id') or 0)} and your reply closes "
            "it. Act with the tools; do not only describe what should happen.")
