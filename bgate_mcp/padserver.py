"""The brainstorm room's own small MCP server — the pads, the board, and this world's canon.

WHY THIS EXISTS AS ITS OWN SERVER. The brainstorm partner is a spawned Claude
Code session that is deliberately built with no capability: an empty built-in
tool set, and ``--strict-mcp-config`` so it cannot inherit the builders-gate
server registered on the machine. That server is ~150 tools including
``queue_add``, ``blender_run`` and generators that spend real money, and an
allowlist naming eight safe ones out of it would rest the whole promise on
nobody ever mistyping an entry.

But a thinking partner that cannot SEE the writing pad or the drawing the human
is making beside it is answering with one eye shut. So the answer is not "let
some of the big server through", it is "build the small server": every tool
here reaches this project's own database, this ONE brainstorm session, or the
game directory READ-ONLY. It holds no generator and no subprocess, and it can
write nothing outside this project's own tables. Read the imports and
TOOL_NAMES — that is the whole surface area.

THE ROOM CAN LOOK AT THE GAME, AND ONLY LOOK. project_files, file_read and
scene_tree exist because a room whose only evidence was the transcript, the
canon and a list of past CLAIMS re-derived the board's answer forever — the tech
seat diagnosed that itself, in the room, in those words. They resolve every path
under the project and refuse anything that leaves it, including the two shapes a
lstrip() does not catch and that a security pass found reaching outside:

    an ANCHORED path      "C:pyproject.toml" is DRIVE-RELATIVE on Windows, and
                          joining it DISCARDS the base — it resolved against
                          this process's working directory, outside the game.
    a CLIMBING GLOB       "../../*" is accepted by pathlib and walks straight
                          out of the directory.

Both are refused, every glob hit is re-checked against the project, and
tests/test_brainstorm_pads.py parametrises the whole family so the next edit to
path handling has to keep them refused.

WHAT IT IS ALLOWED TO WRITE, in one line, because the list grew and the line did
not move: canon (bible sections, lore entities, facts, links), the sketch, and
messages into this room. NOT the board, NOT a file in the game. Work is filed
when a human presses Deploy and by no other path.

WHY board_read WAS ADDED, AND WHY IT IS NOT THE CRACK IT LOOKS LIKE. The seat
holding these tools is the DIRECTOR — the seat whose entire brief is arbitrating
priority and canon. Asked "has everything from the last three days been wired
up", it correctly answered that it could not see the board, and then asked the
human to paste the board in by hand. That is not a safe boundary, it is the
director doing its one job blind: it was reasoning about priorities with no
access to the list of work, so every answer about sequencing was guesswork
dressed as advice, and the human became a copy-paste transport for data the
process could already reach.

The promise this room makes is NOT "the partner knows nothing". It is "NO WORK
IS FILED AND NO PROJECT FILE IS WRITTEN until a human presses Deploy". Reading
the queue files nothing and writes nothing. The write tools — queue_add,
set_status, dispatch — are still not here and must not be added: if a later
change makes this server able to MOVE an item, the promise is gone and the
docstring above stops being true.

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
from bgate_core import queue as _queue

# How many elements one call may write. A diagram is a diagram; a model that
# decides to lay out four hundred boxes has misunderstood the room, and the pad
# has a byte ceiling of its own that would refuse the write anyway — later, and
# less clearly.
MAX_ELEMENTS = 60

mcp = FastMCP(
    "brainstorm-pads",
    instructions=(
        "You are a voice in a Builders Gate brainstorm — either the room's own "
        "thinking partner or a seat that was invited into it. These tools are "
        "your ONLY tools. They reach this project's database and this one "
        "room; none of them files work, dispatches an agent, runs a command or "
        "writes a file in the game.\n\n"
        "canon_read BEFORE you assert what this world is. The bible and the "
        "lore graph are the record; this conversation is not. A seat that "
        "answers from the transcript alone re-invents things the project named "
        "months ago under a different word.\n\n"
        "bible_write / lore_write / lore_fact / lore_link when the room settles "
        "something, or when you are asked to write it down. DO IT rather than "
        "describing what should be written — the human asking you to record a "
        "decision is asking for the record, not for a summary of it. Amend what "
        "already covers the subject instead of adding a near-duplicate.\n\n"
        "dialogue_list / dialogue_read to see the actual lines before you have "
        "an opinion about them. They are read-only here: a dialogue tree is a "
        "file in the game. Quote what you would change and to what.\n\n"
        "room_post to say something to the room without waiting for your turn — "
        "hand another seat a concept, flag a collision between two seats' "
        "plans, answer a seat that named you. Everyone here reads it.\n\n"
        "pad_read before you answer whenever the conversation refers to 'the "
        "diagram', 'the notes' or 'this' — you cannot see their screen and the "
        "pads change while you talk. pad_draw when a shape says it better than "
        "a paragraph; reuse the ids pad_read showed you when amending.\n\n"
        "board_read when they ask what got built, what is in flight, or whether "
        "something was wired up. Never answer that from memory and never ask "
        "them to paste the board in. IT IS NEWEST-FIRST BY DEFAULT: 'the last "
        "ten tasks' is board_read(limit=10), not a full dump you then sort by "
        "eye, and `offset` pages back from there. A 'done' row is an agent's "
        "CLAIM that it finished, not proof; say which one you are relying on.\n\n"
        "project_files / file_read / scene_tree are GROUND TRUTH — the actual "
        "repository, not what an item claimed about it. Reach for them the "
        "moment the question is whether something exists, whether it landed, or "
        "what a file or scene really contains. A board row and the file on disk "
        "disagreeing is the most useful thing you can report, and you can only "
        "find it by reading both. Read-only: propose the change, do not expect "
        "to make it.\n\n"
        "dialogue_list / dialogue_read to see the actual lines before you have "
        "an opinion about them. Read-only for the same reason.\n\n"
        "Every canon write lands in this room's transcript under your name, so "
        "the human reads what you changed and the other seats can argue with it."
    )
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


def _seat() -> str:
    """WHICH SEAT IS HOLDING THESE TOOLS, for stamping what it writes.

    The spawner strips BGATE_SEAT on purpose — a seat in this room answers AS
    its craft but does not HOLD the seat, so nothing enforces a lane against it
    (see brainsession's env block). What it does stamp is BGATE_ACTOR, as
    ``brainstorm:<id>[:<seat>]``, and that is what every canon write below is
    attributed to. Unstamped means the room's own partner, which is honest: that
    voice is nobody's seat.
    """
    explicit = (os.environ.get("BGATE_BRAINSTORM_SEAT") or "").strip()
    if explicit:
        return explicit
    actor = (os.environ.get("BGATE_ACTOR") or "").strip()
    parts = actor.split(":")
    return parts[2].strip() if len(parts) >= 3 else ""


def _who() -> str:
    """How a canon row records where it came from."""
    seat = _seat()
    room = f"brainstorm {_session_id()}"
    return f"{room} · {seat}" if seat else room


def _announce(text: str) -> None:
    """Write what just happened into the room's own transcript.

    EVERY CANON WRITE FROM THIS SERVER LANDS IN THE CONVERSATION, and that is
    the whole reason writing is allowed here at all. A seat that can change the
    bible silently is a seat that changed the bible without the human reading
    it; a line in the transcript is what turns "it edited canon" into something
    you scroll past and can argue with. It is also what the OTHER seats in the
    room see — the same relabelled transcript they read each other through — so
    a canon edit is a thing the room can respond to rather than a private act.

    Never raises: a note that will not store must not undo a write that already
    happened, or the transcript would claim less than the database holds.
    """
    try:
        _bs.append_message(_root(), _session_id(), "assistant",
                           text[:_bs.MAX_MESSAGE], seat=_seat())
    except Exception:
        pass


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


def config(root: str, session_id: int, python: Optional[str] = None,
           seat: str = "") -> dict:
    """The ``--mcp-config`` document that registers THIS server and only this one.

    Built here rather than in the spawner so the server and its registration
    cannot disagree about the module path or the variables it needs. The
    interpreter is the caller's absolute path for the same reason the install
    docs insist on one: a bare `python` resolves differently under a spawned CLI
    than in a shell, and the failure reads as "server not connected" with
    nothing pointing at the interpreter.
    """
    import sys

    # The seat rides in the config rather than being read off the inherited
    # environment, because whether an MCP child inherits the spawner's env is a
    # property of the CLI, not something this module can promise. Every canon
    # row this server writes is attributed with it (see _seat), so an
    # unattributed write would be a write nobody can trace back to a voice.
    env = {"BGATE_ROOT": str(root),
           "BGATE_BRAINSTORM_SESSION": str(int(session_id))}
    if str(seat or "").strip():
        env["BGATE_BRAINSTORM_SEAT"] = str(seat).strip()
    return {"mcpServers": {"pads": {
        "command": python or sys.executable,
        "args": ["-m", "bgate_mcp.padserver"],
        "env": env,
    }}}


@mcp.tool()
def board_read(status: str = "", seat: str = "", limit: int = 40,
               order: str = "recent", offset: int = 0) -> dict:
    """READ the project's work board. Read-only: this cannot file, move or dispatch anything.

    Call it whenever the human asks what got built, what is in flight, what is
    blocked, what a seat is working on, or whether something was "wired up".
    Answering those from memory is guessing — the board is the only record of
    what was actually queued and what actually finished.

    `order` decides what "first" means, and it is the whole difference between
    answering a question and guessing at one:

        recent  (default) NEWEST FIRST, by id. This is what answers "the last
                ten tasks", "what has landed today", "what changed since we
                spoke" — the questions people actually ask a board.
        live    the board's own working order: queued, then running, then in
                review, then finished, then cancelled. Use it for "what is in
                flight", not for "what is recent".

    `offset` pages through the rest. THESE TWO EXIST BECAUSE THE ROOM COULD NOT
    ANSWER "the last 15" WITHOUT READING ALL 435 ROWS AND BLOWING ITS CONTEXT,
    so seats fell back to whatever slice happened to be in the conversation
    already and answered about work from weeks ago. If you need the tail, ask
    for the tail: `board_read(limit=15)` is now exactly that.

    `status` filters to one of queued/dispatched/review/done/failed/cancelled;
    `seat` filters to one seat. Each item carries its id, seat, title, status,
    priority, chain id, when it was last touched, and the tail of its result,
    which is what a completed item claims it did.

    A `done` row is a CLAIM, not proof. It means an agent reported finishing,
    not that the work is wired into the game — say so when the distinction
    matters rather than reading the board back as if it were verification.
    """
    try:
        root = _root()
        items = _queue.list_items(root, status=status or None, seat=seat or None)
        # list_items sorts for the BOARD (live work first, then by priority),
        # which is the right order for a queue panel and the wrong one for
        # "what happened lately" — priority buckets scramble time completely.
        if str(order or "recent").lower() != "live":
            items = sorted(items, key=lambda it: int(it.get("id") or 0),
                           reverse=True)
        cap = max(1, min(int(limit or 40), 200))
        start = max(0, int(offset or 0))
        window = items[start:start + cap]
        rows = [{
            "id": it.get("id"),
            "seat": it.get("seat") or "",
            "title": it.get("title") or "",
            "status": it.get("status") or "",
            "priority": it.get("priority"),
            "chain_id": it.get("chain_id") or "",
            "updated_at": it.get("updated_at") or "",
            "result": str(it.get("result") or "")[:400],
        } for it in window]
        counts: dict[str, int] = {}
        for it in items:
            key = str(it.get("status") or "")
            counts[key] = counts.get(key, 0) + 1
        return {"ok": True, "total": len(items), "showing": len(rows),
                "order": "live" if str(order or "").lower() == "live" else "recent",
                "offset": start,
                "more": start + len(rows) < len(items),
                "counts": counts, "items": rows}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# CANON — what this world already says, and what this room may add to it
# ---------------------------------------------------------------------------
# THE BOUNDARY MOVED, DELIBERATELY, AND HERE IS EXACTLY HOW FAR.
#
# The room's promise has always been two things said as one: no work is filed,
# and no project file is written, until a human presses Deploy. Those are still
# both true and both still enforced by construction — there is no queue_add
# here, no dispatch, no subprocess, no filesystem.
#
# What changed is CANON. A seat invited to think about this world could not read
# what the world already says, so it argued from the conversation alone and
# confidently contradicted the bible; and it could not write down the one thing
# it was asked for, so a decision reached in the room died there unless the
# human retyped it somewhere else. The canon tables are this project's own
# database — bible sections, lore entities, canon facts — not the game's source,
# not the board. A room that can write them can record what it decided; it still
# cannot make anything happen.
#
# THREE THINGS KEEP THAT HONEST:
#   * every write is stamped with the seat that made it (see _seat)
#   * every write announces itself in the room's transcript (see _announce), so
#     the human reads it and the other seats can argue with it
#   * nothing here deletes. Sections and entities are added or amended; the
#     removal doors are on the big server, where a human opens them.

@mcp.tool()
def canon_read(q: str = "", kind: str = "", limit: int = 30) -> dict:
    """READ this world's canon: the design bible and the lore graph.

    Call it BEFORE answering anything about what this world is, what a character
    or place is, what the pillars are, or whether an idea contradicts something
    already established. The conversation in this room is not the record — this
    is. Answering from the transcript alone is how a room re-invents a thing the
    project named eight months ago under a different word.

    `q` filters by a word in a title, name or summary; `kind` narrows to one
    bible kind (pillar/loop/constraint/…) or one lore kind. Returns the bible
    sections and the lore entities with their locked facts, which are the two
    halves of "what is already true".
    """
    try:
        from bgate_core import bible as _bible, lore as _lore

        root = _root()
        cap = max(1, min(int(limit or 30), 120))
        needle = str(q or "").strip().lower()

        def hit(*fields: Any) -> bool:
            if not needle:
                return True
            return any(needle in str(f or "").lower() for f in fields)

        sections = [{
            "id": s["id"], "kind": s["kind"], "title": s["title"],
            "body": str(s["body"] or "")[:600],
            # WHAT AN AMEND HAS TO QUOTE BACK. Two seats in one round can both
            # decide the room settled something and both write the same
            # section; without this the second one's whole-body write silently
            # erases the first, and the room reads as if it agreed with itself.
            "version": _bible.version_of(s),
        } for s in _bible.list_sections(root, kind or None)
            if hit(s["title"], s["body"])][:cap]

        entities = []
        for e in _lore.list_entities(root, kind or None):
            if not hit(e["name"], e.get("summary")):
                continue
            facts = _lore.facts_of(root, e["id"])
            entities.append({
                "id": e["id"], "slug": e["slug"], "kind": e["kind"],
                "name": e["name"], "status": e.get("status") or "",
                "summary": str(e.get("summary") or "")[:400],
                "facts": [{"statement": f["statement"], "locked": bool(f["locked"])}
                          for f in facts[:12]],
            })
            if len(entities) >= cap:
                break
        return {"ok": True, "sections": sections, "entities": entities,
                "note": "a LOCKED fact is settled canon — contradict it and say "
                        "so out loud rather than quietly writing over it"}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def bible_write(title: str, body: str, kind: str = "note",
                section_id: int = 0, expect: str = "") -> dict:
    """WRITE a section of the design bible: add a new one, or amend one by id.

    This is the door for "write that down" — a pillar the room settled, a
    constraint it agreed to, the shape of a system it just designed. Read
    `canon_read` first: amending the section that already covers this is almost
    always right, and a second section on the same subject is how a bible stops
    being usable.

    AMENDING TAKES `expect` — the `version` canon_read gave you for that
    section — and is REFUSED without it, or with a stale one. A section body is
    written whole, so two voices amending the same one in the same round means
    the second silently erases the first: that is not a hypothetical, it is what
    happened to constraint #41 when the gameplay seat wrote it and the room's
    partner amended it seconds later. A refusal hands you the current version
    and body; re-read, fold your change into what is actually there, and write
    again. Losing somebody's edit is worse than taking a second turn.

    Say what you wrote in your reply too. The write lands in the transcript
    under your seat, but the human is reading your sentence, not a log line.
    """
    try:
        from bgate_core import bible as _bible

        root = _root()
        text = str(title or "").strip()
        if not text:
            raise ValueError("a section needs a title")
        if section_id:
            if not str(expect or "").strip():
                current = _bible.get(root, int(section_id))
                return {"ok": False, "code": "version_required",
                        "error": "amending needs `expect` — the version "
                                 "canon_read gave you for this section. A body "
                                 "is written whole, so a blind amend erases "
                                 "whatever another voice wrote since you read "
                                 "it.",
                        "section": {"id": current["id"],
                                    "title": current["title"],
                                    "body": current["body"],
                                    "version": _bible.version_of(current)}}
            try:
                out = _bible.update(root, int(section_id), title=text,
                                    body=str(body or ""),
                                    expected_version=str(expect).strip())
            except _bible.StaleWrite:
                current = _bible.get(root, int(section_id))
                return {"ok": False, "code": "stale",
                        "error": "this section moved since you read it — "
                                 "another voice wrote it while you were "
                                 "thinking. Fold your change into the body "
                                 "below and write again with the new version.",
                        "section": {"id": current["id"],
                                    "title": current["title"],
                                    "body": current["body"],
                                    "version": _bible.version_of(current)}}
            verb = "amended"
        else:
            out = _bible.add(root, str(kind or "note"), text, str(body or ""))
            verb = "wrote"
        _announce(f"[canon] {verb} bible section #{out['id']} "
                  f"({out['kind']}) — {out['title']}")
        return {"ok": True, "action": verb,
                "section": {"id": out["id"], "kind": out["kind"],
                            "title": out["title"],
                            # The new version, so a second amend in the same
                            # turn does not have to go back through canon_read.
                            "version": _bible.version_of(out)}}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def lore_write(name: str, kind: str = "concept", summary: str = "",
               body: str = "", entity: str = "") -> dict:
    """WRITE the lore graph: add an entity, or amend one by slug or id.

    An entity is a character, a place, a faction, an item, an event — whatever
    this world has that a fact can be said about. Pass `entity` to amend one
    that exists rather than making a second one under a near-identical name;
    `canon_read` is how you find out which.
    """
    try:
        from bgate_core import lore as _lore

        root = _root()
        if entity:
            # NOT the name: an entity's slug is derived from it and is how every
            # fact, link and reference finds it, so renaming through this door
            # would quietly orphan them. Renaming is a human's call on the big
            # server; here, `name` is ignored when amending.
            out = _lore.update_entity(root, entity,
                                      summary=str(summary or "") or None,
                                      body=str(body or "") or None)
            verb = "amended"
        else:
            out = _lore.add_entity(root, str(kind or "concept"), str(name or ""),
                                   str(summary or ""), str(body or ""))
            verb = "added"
        _announce(f"[canon] {verb} lore entity {out['slug']} "
                  f"({out['kind']}) — {out['name']}")
        return {"ok": True, "entity": {"id": out["id"], "slug": out["slug"],
                                       "kind": out["kind"], "name": out["name"]},
                "action": verb}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def lore_fact(entity: str, statement: str) -> dict:
    """Assert ONE atomic fact about an entity, sourced to this room.

    Atomic: "the tower has thirty-one floors" is a fact; a paragraph about the
    tower is a body, and belongs on the entity. Facts written here are NOT
    locked — locking is settled canon and that is a human's call, made on the
    big server. The source records the room and the seat, so a fact can always
    be traced back to the conversation that produced it.
    """
    try:
        from bgate_core import lore as _lore

        root = _root()
        said = str(statement or "").strip()
        if not said:
            raise ValueError("a fact needs a statement")
        out = _lore.add_fact(root, entity, said, source=_who())
        _announce(f"[canon] fact on {entity}: {said[:160]}")
        return {"ok": True, "fact": {"id": out["id"], "statement": out["statement"]}}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def lore_link(src: str, rel: str, dst: str) -> dict:
    """Relate two entities: `src` --rel--> `dst`, by slug or id.

    The graph is what makes lore answerable rather than just written: "who works
    for the KPI" is a link question, and a world where every entity is an island
    cannot answer it.
    """
    try:
        from bgate_core import lore as _lore

        root = _root()
        out = _lore.link(root, src, str(rel or "").strip(), dst)
        _announce(f"[canon] linked {out['src']} --{out['rel']}--> {out['dst']}")
        return {"ok": True, "link": out}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def dialogue_list() -> dict:
    """Every dialogue tree in this project, by name, with its node count.

    THE FILES ARE NOT REACHABLE FROM THIS PROCESS and that is deliberate — it
    holds no filesystem. This reads through the project's own dialogue store,
    which knows the one directory trees live in and nothing else. A narrative
    seat asked about a floor's dialogue could otherwise only say "paste it to
    me", which is the human being a transport for data the room could read.
    """
    try:
        from bgate_core import dialogue as _dlg

        root = _root()
        rows = _dlg.list_dialogues(root)
        return {"ok": True, "count": len(rows), "dialogues": [{
            "name": d.get("name") or "",
            "nodes": d.get("nodes") if isinstance(d.get("nodes"), int)
                     else len(d.get("nodes") or []),
            "start": d.get("start") or "",
        } for d in rows]}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def dialogue_read(name: str) -> dict:
    """READ one dialogue tree: its nodes, its lines and where each choice goes.

    Read-only, and that line is where the room's remaining promise sits. Canon
    is a row in this project's database and this room may write it; a dialogue
    tree is a FILE IN THE GAME, and no file is written from here until a human
    presses Deploy on a plan. Propose the rewrite in your reply — quote the line
    you would change and say what you would change it to — rather than asking
    for a tool that would make this room able to edit the game.
    """
    try:
        from bgate_core import dialogue as _dlg

        doc = _dlg.read(_root(), str(name or ""))
        nodes = doc.get("nodes") or []
        return {"ok": True, "name": doc.get("name") or name,
                "start": doc.get("start") or "", "count": len(nodes),
                "nodes": nodes,
                "note": "read-only here — a dialogue tree is a file in the game, "
                        "and this room writes none"}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# GROUND TRUTH — what the project actually contains, not what somebody claimed
# ---------------------------------------------------------------------------
# THE ROOM WAS ARGUING FROM HEARSAY, AND SAID SO ITSELF. The tech seat, four
# turns into being told the same fact by the human, wrote: "none of the tools in
# this room can actually see the filesystem or scene tree — canon_read and
# board_read only surface what agents CLAIMED in past task results, not ground
# truth. If the human is looking at the actual project and saying floor_0 is the
# sandbox, that outranks a board item saying done."
#
# That is the correct diagnosis and it is a design fault, not a seat being lazy.
# A room whose only inputs are the transcript, the canon and a list of past
# claims will re-derive the board's answer forever, because the board's answer
# is the only evidence it has. So these three read the repository.
#
# READ ONLY, AND SCOPED TO THE PROJECT. Every path is resolved under the project
# root and refused if it escapes (see _under). There is no write, no delete, no
# rename, no exec — the room still cannot change a file in the game, and the
# promise that no project file is written until a human presses Deploy is
# untouched. What changed is that the room can now LOOK.

# How much of a file one call may return. A model that asks for a 4,000-line
# scene gets the head of it and a note saying so, rather than a truncated blob
# it will quietly reason about as if it were whole.
MAX_FILE_LINES = 400
MAX_LIST = 200


def _project_root():
    """The GAME directory, or the project root when a project has no game yet."""
    from pathlib import Path

    from bgate_core import project as _project

    root = Path(_root())
    game = _project.game_dir(root)
    return Path(game) if game else root


# WHAT NO SEAT MAY READ, EVEN INSIDE THE PROJECT.
#
# The containment check answers "is this in the game", and that turned out not
# to be the whole question: a project's own root is where `bgate key set` writes
# .env — OPENAI_API_KEY, KREA_API_KEY, a chat bot token — and `game_dir()`
# returns the ROOT itself for every project that keeps project.godot at the top,
# which is what `bgate init` and `bgate adopt` produce. So "inside the project"
# included the credentials file.
#
# THE SEAT HOLDING THESE TOOLS IS SEMI-TRUSTED. It reads dialogue, scenes, board
# results and other seats' messages — all of which are things an attacker can
# influence — so "it would have to decide to" is not a control. And it holds
# room_post, bible_write and lore_fact, which would carry anything it read into
# the transcript and the project's own database.
#
# Refused by NAME and by DIRECTORY, in _under, so file_read and scene_tree
# inherit it rather than each remembering.
SECRET_NAMES = (".env",)
SECRET_DIRS = (".git", ".bgate", ".claude", ".ssh", ".venv")


def _is_secret(rel_parts) -> bool:
    parts = [str(p) for p in rel_parts]
    if any(part in SECRET_DIRS for part in parts):
        return True
    leaf = parts[-1].lower() if parts else ""
    return leaf in SECRET_NAMES or leaf.startswith(".env")


def _inside(base, target) -> bool:
    """Is ``target`` the project itself or something under it?

    Resolved on both sides, so a symlink pointing out of the project is caught
    rather than followed.
    """
    return base == target or base in target.parents


def _under(rel: str):
    """Resolve ``rel`` under the project and refuse anything that leaves it.

    Refused rather than clamped: a path that escaped and was silently rewritten
    would answer a question about a different file than the one asked for, and
    the model would have no way to tell.
    """
    from pathlib import PureWindowsPath

    raw = str(rel or "")
    # ANCHORED PATHS ARE REFUSED BEFORE THEY ARE JOINED, and this is the bit
    # that a lstrip() cannot do. Joining an anchored path DISCARDS the base:
    # `Path(project) / "C:pyproject.toml"` is just "C:pyproject.toml", which is
    # DRIVE-RELATIVE on Windows and resolves against this process's working
    # directory — outside the project entirely. Measured: it read a file two
    # directories above the game. "/etc/passwd" and r"\server\share" are the
    # same family. PureWindowsPath is used for the test on every platform so a
    # Windows-shaped path is recognised even when the check runs on Linux.
    probe = PureWindowsPath(raw)
    if probe.drive or probe.root or probe.is_absolute():
        raise ValueError(f"{rel!r} is an absolute or drive-relative path — this "
                         "room reads paths INSIDE the game, relative to it")
    base = _project_root().resolve()
    target = (base / raw.lstrip("/\\")).resolve()
    if not _inside(base, target):
        raise ValueError(f"{rel!r} is outside the project — this room reads the "
                         "game and nothing else on the machine")
    if _is_secret(target.relative_to(base).parts if target != base else ()):
        raise ValueError(
            f"{rel!r} is off limits: .env holds this project's API keys, and "
            f"{', '.join(SECRET_DIRS)} are its private state. Refused rather "
            "than returned empty so you know it exists and is not yours.")
    return target


@mcp.tool()
def project_files(pattern: str = "**/*", limit: int = 80) -> dict:
    """LIST what is actually in the game project. Ground truth, not a claim.

    `pattern` is a glob relative to the game directory — "scenes/**/*.tscn",
    "data/*.json", "**/*.gd". Use it before you assert that something exists,
    that it does not, or that a past item "landed": a board row saying done is
    an agent's report, and this is the directory.

    Returns paths relative to the game, with sizes and modified times, newest
    first, so "what changed recently" is answerable without asking the human.
    """
    try:
        base = _project_root().resolve()
        rows = []
        # A PATTERN CAN CLIMB, so every hit is re-checked against the project.
        # `Path.glob("../../*")` is accepted by pathlib and walks straight out of
        # the game directory — measured, not assumed. Without this the escape
        # reached the machine's own tree, and even though relative_to() then
        # raised rather than returning the names, the error carried an absolute
        # path outside the project back into the room. Skipped rather than
        # refused: a pattern that happens to sweep wide should return what it
        # legitimately matched.
        for path in sorted(base.glob(str(pattern or "**/*")))[:5000]:
            if not path.is_file() or ".godot" in path.parts:
                continue
            try:
                resolved = path.resolve()
                if not _inside(base, resolved):
                    continue
                # Listed is disclosed: a seat that sees `.env` in a file list
                # knows the key is there and where. Same rule as _under, applied
                # here because a glob never goes through it.
                if _is_secret(resolved.relative_to(base).parts):
                    continue
            except (OSError, ValueError):
                continue
            stat = path.stat()
            rows.append({"path": path.relative_to(base).as_posix(),
                         "bytes": stat.st_size, "modified": int(stat.st_mtime)})
        rows.sort(key=lambda r: r["modified"], reverse=True)
        cap = max(1, min(int(limit or 80), MAX_LIST))
        return {"ok": True, "root": str(base), "total": len(rows),
                "showing": min(cap, len(rows)), "files": rows[:cap]}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def file_read(path: str, start: int = 1, lines: int = 200) -> dict:
    """READ a file in the game project. The actual bytes on disk.

    This is what settles a disagreement between two seats, or between the room
    and the board: read the file and quote it. `start` and `lines` window a long
    one — the response says how many lines the file really has, so a partial
    read is never mistaken for the whole thing.

    Read-only. To CHANGE what you just read, say what should change and let the
    human file the work; nothing in this room writes to the game.
    """
    try:
        target = _under(path)
        if not target.is_file():
            return {"ok": False, "error": f"no such file in the project: {path}"}
        body = target.read_text(encoding="utf-8", errors="replace").splitlines()
        first = max(1, int(start or 1))
        want = max(1, min(int(lines or 200), MAX_FILE_LINES))
        window = body[first - 1:first - 1 + want]
        return {"ok": True, "path": str(path), "total_lines": len(body),
                "start": first, "shown": len(window),
                "truncated": first - 1 + len(window) < len(body),
                "text": "\n".join(window)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def scene_tree(scene: str, match: str = "", limit: int = 120) -> dict:
    """READ a Godot scene's node tree: paths, types, scripts, resources.

    The other half of ground truth. A seat asked whether the mimic is in
    floor_0, or which node holds a script, is guessing unless it reads the
    scene — and "the board says it landed" is not the same claim.

    `scene` is relative to the game directory ("scenes/floor_0.tscn").
    `match` filters node paths by substring, which a baked floor with fifteen
    hundred nodes needs.
    """
    try:
        from bgate_core import scenewire as _scenewire

        target = _under(scene)
        if not target.is_file():
            return {"ok": False, "error": f"no such scene: {scene}"}
        nodes = _scenewire.outline(
            target.read_text(encoding="utf-8", errors="replace"))
        needle = str(match or "").lower()
        if needle:
            nodes = [n for n in nodes
                     if needle in str(n.get("path", "")).lower()
                     or needle in str(n.get("type", "")).lower()]
        cap = max(1, min(int(limit or 120), MAX_LIST))
        return {"ok": True, "scene": str(scene), "total": len(nodes),
                "showing": min(cap, len(nodes)), "nodes": nodes[:cap]}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def room_post(text: str) -> dict:
    """SAY something to the room without waiting to be asked.

    Everything else in this room is turn-taking: the human says one thing, each
    voice answers once. That is a survey, not a collaboration — and it is why a
    seat that noticed halfway through a task that the art plan contradicts the
    tech plan had nowhere to put it until its next turn came round.

    This posts a message into the transcript under your seat, right now. The
    human sees it, and so does every other seat in the room, because they read
    the same transcript with each other's turns labelled. Use it to hand
    somebody a concept you just worked out, to flag a collision, or to answer a
    seat that named you.

    It is still only an opinion: nothing here queues work or dispatches anyone.
    Do not narrate with it — one post that says a thing beats four that
    say you are about to.
    """
    try:
        said = str(text or "").strip()
        if not said:
            raise ValueError("nothing to post")
        row = _bs.append_message(_root(), _session_id(), "assistant",
                                 said[:_bs.MAX_MESSAGE], seat=_seat())
        return {"ok": True, "message_id": row.get("id"), "seat": _seat()}
    except Exception as exc:
        return _fail(exc)


# The tool names as the CLI will report them in its `system/init` event. Used by
# the spawner's own verification and by the tests: the point of a small server
# is worth nothing if nobody checks how small it still is.
#
# Every name here is a READ of this project's database, a WRITE to its canon
# tables, or a message into this one room. There is no name here that files
# work, runs a command or touches a file in the game, and adding one would end
# the promise this whole module exists to keep.
TOOL_NAMES = ("mcp__pads__pad_read", "mcp__pads__pad_draw",
              "mcp__pads__board_read", "mcp__pads__canon_read",
              "mcp__pads__bible_write", "mcp__pads__lore_write",
              "mcp__pads__lore_fact", "mcp__pads__lore_link",
              "mcp__pads__dialogue_list", "mcp__pads__dialogue_read",
              "mcp__pads__project_files", "mcp__pads__file_read",
              "mcp__pads__scene_tree", "mcp__pads__room_post")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
