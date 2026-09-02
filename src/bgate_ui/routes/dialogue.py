"""Dialogue trees, over HTTP.

WHY THIS EXISTS. `dialogue_list`, `dialogue_read` and `dialogue_write` have only
ever been MCP tools, so an agent could see the trees and the dashboard could
not. That was tolerable while the UI had nothing to say about them; the
narrative seat page (frontend/src/shell/narrative/) makes the GRAPH the editor,
because the three things dialogue.validate refuses a write for — a choice
pointing nowhere, a node nothing reaches, a node with no ending beyond it — are
invisible in the file and obvious on a graph. A screen that cannot read the
trees can only say so.

READ ONLY, DELIBERATELY. `write` is where canon_check runs, where the QA gate
and the lane rules live, and where a bad tree gets refused with a reason. That
belongs to the MCP door and to the seat that holds the lane; adding an HTTP
write here would be a second, quieter path to the same files with none of that
around it. The page proposes; an agent (or a human through the MCP tool) still
writes.

Auto-registers via routes/__init__.py. Envelope and errors per bgate_ui/api.py.
"""
from __future__ import annotations

from bgate_core.design import dialogue as _dialogue
from fastapi import APIRouter

from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/dialogue")
def dialogue_index() -> dict:
    """Every tree in the lane, with its own verdict.

    `list_dialogues` already marks a file that no longer validates — the listing
    is where a broken tree is cheapest to notice, and the page shows that mark
    before you open anything.
    """
    return api.ok({"dialogues": _dialogue.list_dialogues(root())})


@router.get("/api/dialogue/{name}")
def dialogue_read(name: str) -> dict:
    """One tree, whole: nodes, choices, start, and the paths it lives at.

    The 404 carries `list_dialogues`' own sentence about where to look, rather
    than a bare status — a name that does not exist is nearly always a typo or a
    tree somebody moved, and both are answered by the listing.
    """
    try:
        return api.ok(_dialogue.read(root(), name))
    except LookupError as exc:
        raise api.not_found(str(exc))
    except _dialogue.DialogueError as exc:
        # Unreadable is not missing: the file is there and its JSON is broken,
        # which is a different fix and deserves a different status.
        raise api.bad_request(str(exc))


@router.get("/api/dialogue/{name}/validate")
def dialogue_validate(name: str) -> dict:
    """The refusal `write` WOULD print, without writing anything.

    `validate` RAISES DialogueError naming the node at fault — it is built to
    stop a write, not to report on one. Letting that propagate would 500 the
    endpoint on exactly the trees this exists to describe: the broken ones. So
    the refusal is caught and returned AS DATA, with `ok` saying which it is
    and `problem` carrying the sentence the writer would have printed, node
    name and all.

    The page computes the same three checks client-side to draw them on the
    graph as you edit; this is the authority it reconciles against, so a tree
    the dashboard calls clean and the writer refuses cannot happen quietly.
    """
    try:
        doc = _dialogue.read(root(), name)
    except LookupError as exc:
        raise api.not_found(str(exc))
    try:
        checked = _dialogue.validate(doc.get("nodes") or [],
                                     str(doc.get("start") or ""))
    except _dialogue.DialogueError as exc:
        return api.ok({"ok": False, "problem": str(exc)})
    return api.ok({"ok": True, "problem": "",
                   "start": checked["start"], "ends": checked["ends"]})
