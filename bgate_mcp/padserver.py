"""The brainstorm pads, and NOTHING ELSE — a two-tool MCP server for one session.

WHY THIS EXISTS AS ITS OWN SERVER. The brainstorm partner is a spawned Claude
Code session that is deliberately built with no capability: an empty built-in
tool set, and ``--strict-mcp-config`` so it cannot inherit the builders-gate
server registered on the machine. That server is ~150 tools including
``queue_add``, ``blender_run`` and generators that spend real money, and an
allowlist naming eight safe ones out of it would rest the whole promise on
nobody ever mistyping an entry.

But a thinking partner that cannot SEE the writing pad or the drawing the human
is making beside it is answering with one eye shut. So the answer is not "let
some of the big server through", it is "build the small server": this module
exposes exactly two tools, over exactly one brainstorm session, and there is no
third thing it could do. It holds no queue, no repo, no filesystem, no
generator, no subprocess. Read the imports — that is the whole surface area.

WRITING A SKETCH ELEMENT IS NOT A VIOLATION OF THE ROOM'S PROMISE, AND THE NEXT
READER SHOULD NOT "FIX" IT. The promise the brainstorm room makes is that NO
WORK IS FILED AND NO PROJECT FILE IS WRITTEN until a human presses Deploy on a
plan they have read. A rectangle labelled "shrine" in somebody's scratch diagram
is neither. It is the same act as the partner typing a sentence into the
conversation — a contribution to the human's thinking, stored in the same row of
the same table as the rest of the session, deleted whenever they delete it. The
drawing is stored as structured Excalidraw-shaped JSON rather than a PNG
precisely so that a text model can take part in it; a pad the partner can only
look at is the design this storage format was chosen to avoid.

WHAT IT STILL CANNOT DO, so the boundary is legible:
  * the WRITING pad is read-only here. It is the human's own document, an hour
    of their typing, and a whole-document write from a model that read a stale
    copy deletes the rest of it. brainstorm_note on the big server is the door
    for that, and it is a door a human opens.
  * no element is DELETED. pad_draw merges by id — it can add, move, relabel or
    restyle, and it cannot make anything of the human's disappear. Removing
    things from your own diagram is your own business.
  * nothing outside this one session. The session id comes from the environment
    the dashboard spawned this process with, not from a tool argument, so a
    partner cannot reach another room's pads by asking.

CONCURRENCY. The human may be drawing in the pad at the moment the partner
writes to it. Two defences, and neither is a lock: writes MERGE BY ID rather
than replacing the scene, so nothing of theirs is dropped by construction; and
``rev`` is a fingerprint of the stored scene that pad_read hands out and
pad_draw will honour if it is passed back — a partner that read, thought, and
wrote into a scene that moved underneath it is told so and re-reads instead of
landing on top. The browser polls the same rev and reloads the pad when it
changes, so the human sees the addition rather than saving over it.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from bgate_core import brainstorm as _bs

# How many elements one call may write. A diagram is a diagram; a model that
# decides to lay out four hundred boxes has misunderstood the room, and the pad
# has a byte ceiling of its own that would refuse the write anyway — later, and
# less clearly.
MAX_ELEMENTS = 60

mcp = FastMCP(
    "brainstorm-pads",
    instructions=(
        "You are the thinking partner in a Builders Gate brainstorm. These two "
        "tools are your ONLY tools and they reach one thing: the human's pads "
        "for THIS session.\n\n"
        "pad_read before you answer whenever the conversation refers to "
        "'the diagram', 'the notes', 'what I wrote' or 'this' — you cannot see "
        "their screen and the pads change while you talk.\n\n"
        "pad_draw when a shape says it better than a paragraph, or when they "
        "ask you to add something. Reuse the element ids pad_read showed you "
        "when you are amending, or you will create a duplicate instead of "
        "moving the original. Arrows bind to boxes by id.\n\n"
        "Nothing here queues work, dispatches anyone or writes a project file. "
        "Proposing work is a separate step a human takes when they are ready."
    ),
)


class _NoSession(RuntimeError):
    pass


def _root() -> str:
    root = (os.environ.get("BGATE_ROOT") or "").strip()
    if not root:
        raise _NoSession("this pad server was started without BGATE_ROOT")
    return root


def _session_id() -> int:
    """WHICH session, from the environment rather than from an argument.

    The dashboard spawns one of these per brainstorm and stamps the id on the
    process. Taking it as a tool parameter would make "which room am I in" a
    thing the model states, and a model that states it can state a different
    one — which is how a partner ends up drawing in somebody else's session.
    """
    raw = (os.environ.get("BGATE_BRAINSTORM_SESSION") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise _NoSession("this pad server was started without a session id")
    return value


def _rev(scene: Any) -> str:
    """A short fingerprint of the scene as stored.

    Not a version counter, because nothing here owns a counter — this is
    computed from the bytes, so it is correct across the dashboard, this
    process, and a session reopened next week.
    """
    blob = json.dumps(scene or {}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _fail(exc: Exception) -> dict:
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}


@mcp.tool()
def pad_read() -> dict:
    """READ the human's pads for this brainstorm: what they wrote and what they drew.

    Call it before answering anything that refers to the diagram, the notes, or
    "this" — you cannot see their screen, and both pads change while you talk.

    `notes` is their writing pad verbatim. `drawing_text` is the drawing rendered
    as readable lines — `rectangle#hub-1 "hub"`, `arrow#a1 hub-1 -> shrine-1` —
    which is the CONTENT of the board rather than a picture of it. `elements` is
    the same scene structurally, and the ids in it are what you pass back to
    pad_draw when you are amending something rather than adding to it.

    `rev` fingerprints the drawing as it is right now. Hand it back to pad_draw
    and your write is refused if the human changed the pad while you were
    thinking, which is the difference between joining their diagram and landing
    on top of it.
    """
    try:
        root, sid = _root(), _session_id()
        session = _bs.get(root, sid)
        scene = session.get("drawing") or {}
        return {
            "session_id": sid,
            "title": session.get("title") or "",
            "notes": str(session.get("notes") or ""),
            "drawing_text": _bs.drawing_digest(scene),
            "elements": _bs.elements(scene),
            "rev": _rev(scene),
        }
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def pad_draw(elements: list, rev: str = "") -> dict:
    """ADD TO or AMEND the human's drawing pad. Merges by id; deletes nothing.

    Each element is an Excalidraw-shaped object. The minimum that draws:

        {"id": "shrine-1", "type": "rectangle", "x": 320, "y": 40,
         "width": 140, "height": 70, "text": "shrine"}
        {"id": "a1", "type": "arrow",
         "startBinding": {"elementId": "hub-1"},
         "endBinding": {"elementId": "shrine-1"}, "text": "walk"}

    A NEW id adds a shape. An id pad_read already showed you REPLACES that one
    shape — which is how you move it, relabel it or rebind an arrow. Every other
    element the human has is left exactly as it was: this call cannot delete
    anything, by construction, so you can never cost them work by writing.

    Pass `rev` from pad_read and the write is refused if the pad changed while
    you were thinking; the refusal comes back with the current drawing so you
    can look again rather than guess.

    Say in the conversation what you drew and why. A shape that appears on
    somebody's board with no sentence attached reads as a glitch.
    """
    try:
        root, sid = _root(), _session_id()
        session = _bs.get(root, sid)
        scene = session.get("drawing") or {}
        current = _rev(scene)
        if rev and rev != current:
            return {"ok": False, "rev": current,
                    "error": "the pad changed while you were thinking — nothing "
                             "was written. Here is the drawing as it is now; "
                             "read it and draw again.",
                    "drawing_text": _bs.drawing_digest(scene),
                    "elements": _bs.elements(scene)}
        clean = _clean(elements)
        if not clean:
            return {"ok": False, "rev": current,
                    "error": "no drawable element in that call — each one needs "
                             "at least an id and a type"}
        merged, added, amended = _merge(_bs.elements(scene), clean)
        scene = dict(scene) if isinstance(scene, dict) else {}
        scene["elements"] = merged
        _bs.set_drawing(root, sid, scene)
        after = _bs.get(root, sid).get("drawing") or {}
        return {"session_id": sid, "added": added, "amended": amended,
                "elements_total": len(_bs.elements(after)),
                "rev": _rev(after),
                "drawing_text": _bs.drawing_digest(after)}
    except Exception as exc:
        return _fail(exc)


def _clean(raw: Any) -> list[dict]:
    """The elements that are actually drawable, with an id guaranteed.

    Lenient rather than strict: a model that omitted a width has made a
    correctable mistake, and refusing the whole call over it costs a turn to
    learn something the default could have said. An element with no usable
    TYPE is dropped, because a shape whose kind nobody knows renders as nothing
    and would read as the tool silently failing.
    """
    out: list[dict] = []
    for i, el in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(el, dict):
            continue
        kind = str(el.get("type") or "").strip()
        if not kind:
            continue
        made = dict(el)
        made["type"] = kind
        made["id"] = str(el.get("id") or "").strip() or f"partner-{i + 1}"
        out.append(made)
        if len(out) >= MAX_ELEMENTS:
            break
    return out


def _merge(existing: list[dict], incoming: list[dict]) -> tuple[list, int, int]:
    """Upsert by id, PRESERVING ORDER. Never removes.

    Order is the z-order the pad paints in, so rebuilding the list from a dict
    would reshuffle the human's diagram every time the partner touched it —
    a change nobody asked for that looks exactly like a bug.
    """
    by_id = {str(e.get("id") or ""): e for e in incoming}
    merged: list[dict] = []
    amended = 0
    for el in existing:
        key = str(el.get("id") or "")
        if key and key in by_id:
            merged.append(by_id.pop(key))
            amended += 1
        else:
            merged.append(el)
    added = len(by_id)
    merged.extend(by_id.values())
    return merged, added, amended


def config(root: str, session_id: int, python: Optional[str] = None) -> dict:
    """The ``--mcp-config`` document that registers THIS server and only this one.

    Built here rather than in the spawner so the server and its registration
    cannot disagree about the module path or the variables it needs. The
    interpreter is the caller's absolute path for the same reason the install
    docs insist on one: a bare `python` resolves differently under a spawned CLI
    than in a shell, and the failure reads as "server not connected" with
    nothing pointing at the interpreter.
    """
    import sys

    return {"mcpServers": {"pads": {
        "command": python or sys.executable,
        "args": ["-m", "bgate_mcp.padserver"],
        "env": {"BGATE_ROOT": str(root),
                "BGATE_BRAINSTORM_SESSION": str(int(session_id))},
    }}}


# The tool names as the CLI will report them in its `system/init` event. Used by
# the spawner's own verification and by the tests: the point of a two-tool
# server is worth nothing if nobody checks that it is still two tools.
TOOL_NAMES = ("mcp__pads__pad_read", "mcp__pads__pad_draw")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
