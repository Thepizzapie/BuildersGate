"""Brainstorm sessions — the cheap room, where an idea is still only an idea.

THE CONSOLE IS THE EXPENSIVE ROOM. Every sentence typed into
``bgate_ui.routes.console`` becomes a work item, is dispatched to a real Claude
Code session holding the entire MCP tool set, and lands on the board. That is
the right shape for "do this" and the wrong shape for "what if": thinking out
loud there bills a spawned session per half-thought and leaves a board full of
items nobody meant to file. There was no surface in the product for the twenty
minutes BEFORE you know what you want built.

So this is a separate store, and the separation is the feature. Nothing in the
conversational half of this module queues, dispatches, spawns or touches the
approval gate. There is exactly ONE function here that can reach the board —
:func:`file_plan` — and it imports :mod:`bgate_core.queue` inside its own body,
so the module still has no ``queue`` attribute for anything else to reach for.
tests/test_brainstorm.py asserts that absence, which turns "we agreed not to"
into something CI can check. Work is filed exactly once, by that function, from
a plan a human has already read.

TWO FRONT DOORS, ONE IMPLEMENTATION. The dashboard
(``bgate_ui.routes.brainstorm``) and the MCP server (``bgate_mcp.server``) both
open this room, so the parts a copy would have drifted on live here rather than
in either door: the thinking partner (:func:`ask`, which spawns a real CLI
session built without the ability to write — see below), the turns a synthesis
is asked over (:func:`synthesis_turns`), and the whole of filing — the
double-file guard, the chain-vs-loose decision, and the record-what-landed
ordering that survives a queue refusal half way through (:func:`file_plan`).
The MCP door is why that matters: it runs inside a process that already holds
``queue_add``, so "a brainstorm cannot file work" has to be a property of this
code rather than of that process's tool list.

THE PARTNER IS A SPAWNED SESSION, AND IT STILL CANNOT WRITE. It used to be a
bare chat-completions call, where "no tools" was almost free. It is now a real
Claude Code session — the same thing the board dispatches — spawned with an
empty tool set, no MCP server and no way to acquire either. The guarantee moved
from an argument allowlist to the process's own construction, which is stronger,
and the details of it are in :mod:`bgate_ui.brainsession` next to the evidence.

THE DRAWING IS A SCENE, NOT A PICTURE. A flattened PNG is something a model can
only look at, and only if it has vision — it cannot tell you the box in the
corner is called "shrine", and it certainly cannot add one. So the pad's
elements are stored as structured JSON (Excalidraw-shaped: ``{"elements": [...],
"appState": {...}}``) and :func:`drawing_digest` renders them as lines a text
model reads for free. A PNG path rides alongside for previews and for a vision
call; it is never the source of truth.

THE TWO SEATS. A ``director`` session is about what to BUILD and may propose
work for any seat. A ``narrative`` session is about what is TRUE — canon, lore,
the bible — and may only propose narrative work; see ALLOWED_SEATS. Same
machinery, different target, parameterised rather than forked, because the
moment they are two modules they start drifting on which one remembered to bump
``updated_at``.
"""
from __future__ import annotations

import json
import os
import time as _time
from typing import Any, Optional

from . import activity, db, seats as _seats
from .util import rows

# Which seats can own a session. Not every seat: a brainstorm is a conversation
# with somebody who decides, and only these two decide anything.
SEATS = ("director", "narrative")

# open      -> being thought about; the only state Deploy accepts
# deployed  -> at least one plan has been filed from it; still readable, still
#              writable, because the conversation usually continues afterwards
# archived  -> filed away. Nothing is deleted by archiving; see `archive`.
STATUSES = ("open", "deployed", "archived")

# The role names the model API itself uses, on purpose: the transcript maps 1:1
# onto a messages array with no translation table in between, and a translation
# table is where the off-by-one that puts the model's words in the human's mouth
# lives.
ROLES = ("user", "assistant")

# WHAT A SESSION OF EACH SEAT IS ALLOWED TO PROPOSE.
#
# A narrative session that proposes an art item is not a narrative session, it
# is the director's console with a different title — and the human asked for
# these to be different things ("narrative, same idea but for updating the
# narrative not a full game dev deployment"). Enforced at plan time rather than
# trusted to the prompt, because a model told to stay in its lane will leave it
# roughly one time in twenty and the failure is silent.
ALLOWED_SEATS: dict[str, tuple[str, ...]] = {
    "director": tuple(_seats.DEFAULT_SEATS),
    "narrative": ("narrative",),
}

# Same cap as the console's. A single message longer than this is a document,
# and it belongs in the notes pad, which is what the notes pad is for.
MAX_MESSAGE = 4_000
# The writing pad. Generous — it is a scratch document a person types into for
# an hour — but bounded, because it is read whole into the synthesis prompt.
MAX_NOTES = 100_000
# The drawing scene. An Excalidraw board with a hundred elements is tens of KB;
# a megabyte is a runaway paste (or an embedded image) rather than a diagram.
MAX_DRAWING = 1_000_000
MAX_TITLE = 120

# How much of the conversation reaches the model. Bounded because a session is
# meant to be long: an hour of back-and-forth resent in full on every turn is
# how a cheap conversation stops being cheap.
TRANSCRIPT_WINDOW = 40
# How many drawing elements are described to the model. Past this the digest is
# noise the summary has to read around.
DIGEST_ELEMENTS = 120

# How much of the world reaches a SYNTHESIS. Bounded because these are read
# whole into one prompt and a project with four hundred lore entities would turn
# the preview step into the most expensive call in the product — which is the
# exact cost this feature exists to avoid.
WORLD_SECTIONS = 12
WORLD_ENTITIES = 60
WORLD_FACTS = 40


class Missing(LookupError):
    """No such session. Its own type so routes map it to 404 without guessing."""


class Archived(Exception):
    """The session is filed away, and a record is not a workspace."""


class AlreadyFiled(Exception):
    """This exact plan has already been filed from this session.

    Carries the earlier deploy in ``entry`` so a caller can name the items it
    became rather than only refusing.
    """

    def __init__(self, entry: dict):
        self.entry = entry
        ids = ", ".join(f"#{i['id']}" for i in entry.get("items") or [])
        super().__init__(
            "this exact plan was already filed from this session as "
            f"{ids or '(no items recorded)'} — pass again to file a second copy")


class PartialDeploy(ValueError):
    """The queue refused an item PART WAY THROUGH, and some are already on it.

    ``filed`` is what landed. Those rows are recorded on the session before this
    is raised — a partially filed deploy the session does not remember is the
    one state nobody can reconstruct afterwards.
    """

    def __init__(self, message: str, filed: list[dict]):
        self.filed = filed
        super().__init__(message)


def ensure_open(session: dict) -> None:
    """Refuse to add to an archived session. Both doors ask this, one answer."""
    if session.get("status") == "archived":
        raise Archived(f"brainstorm {session.get('id')} is archived — reopen it "
                       "before adding to it")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _now_title(seat: str) -> str:
    return "narrative brainstorm" if seat == "narrative" else "brainstorm"


def create(root: str | os.PathLike[str], seat: str = "director",
           title: str = "") -> dict:
    if seat not in SEATS:
        raise ValueError(f"seat must be one of {SEATS}; got {seat!r}")
    title = (title or "").strip()[:MAX_TITLE] or _now_title(seat)
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO brainstorm_session (seat, title) VALUES (?, ?)",
            (seat, title))
        session_id = int(cur.lastrowid)
    activity.log(root, "brainstorm", f"opened {seat} brainstorm: {title[:80]}",
                 seat=seat, ref=str(session_id))
    return get(root, session_id)


def get(root: str | os.PathLike[str], session_id: int) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM brainstorm_session WHERE id = ?",
        (int(session_id),)).fetchone()
    if row is None:
        raise Missing(f"no brainstorm session {session_id}")
    return _hydrate(dict(row))


def _hydrate(row: dict) -> dict:
    """The row with its JSON columns decoded.

    A blob that will not parse becomes an empty scene rather than an exception:
    the session's TEXT — the messages and the notes — is the part nobody can
    afford to lose, and a corrupt drawing must not be able to make the whole
    conversation unopenable.
    """
    row["drawing"] = _loads(row.pop("drawing_json", ""), {})
    row["deploys"] = _loads(row.pop("deploys_json", ""), [])
    if not isinstance(row["deploys"], list):
        row["deploys"] = []
    return row


def _loads(text: Any, fallback: Any) -> Any:
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return fallback
    return value if value is not None else fallback


def list_sessions(root: str | os.PathLike[str], *, seat: Optional[str] = None,
                  status: Optional[str] = None, limit: int = 50) -> list[dict]:
    """The index. Deliberately WITHOUT the notes, drawing or messages — a list
    that ships every session's whole scratch document is a list nobody can
    afford to poll."""
    sql = ("SELECT s.id, s.seat, s.title, s.status, s.created_at, s.updated_at, "
           "       length(s.notes) AS notes_len, "
           "       (SELECT count(*) FROM brainstorm_message m "
           "        WHERE m.session_id = s.id) AS messages "
           "FROM brainstorm_session s WHERE 1=1")
    params: list = []
    if seat:
        sql += " AND s.seat = ?"
        params.append(seat)
    if status:
        sql += " AND s.status = ?"
        params.append(status)
    # Archived last whatever the sort: it is filed away, not current.
    sql += (" ORDER BY CASE s.status WHEN 'archived' THEN 1 ELSE 0 END, "
            "s.updated_at DESC, s.id DESC LIMIT ?")
    params.append(max(1, int(limit)))
    return rows(db.connect(root).execute(sql, params))


def _touch(conn, session_id: int) -> None:
    conn.execute("UPDATE brainstorm_session SET updated_at = datetime('now') "
                 "WHERE id = ?", (int(session_id),))


def rename(root: str | os.PathLike[str], session_id: int, title: str) -> dict:
    get(root, session_id)
    title = (title or "").strip()[:MAX_TITLE]
    if not title:
        raise ValueError("a session needs a title — send one, or leave it alone")
    with db.tx(root) as conn:
        conn.execute("UPDATE brainstorm_session SET title = ?, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (title, int(session_id)))
    return get(root, session_id)


def set_notes(root: str | os.PathLike[str], session_id: int, notes: str) -> dict:
    """Replace the writing pad. Whole-document write, no patching.

    The pad is one text area a person types into; a diff protocol over it would
    be a merge algorithm nobody asked for. Concurrent editing of a private
    scratch document is not a case this has, so last-write-wins is honest.
    """
    get(root, session_id)
    text = str(notes or "")
    if len(text) > MAX_NOTES:
        raise ValueError(f"notes are {len(text)} characters — the pad holds "
                         f"{MAX_NOTES}")
    with db.tx(root) as conn:
        conn.execute("UPDATE brainstorm_session SET notes = ?, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (text, int(session_id)))
    return get(root, session_id)


def set_drawing(root: str | os.PathLike[str], session_id: int,
                scene: Optional[dict], png: Optional[str] = None) -> dict:
    """Replace the drawing scene, and optionally the rendered preview path.

    ``scene`` is the pad's own structured document — elements, not pixels — so
    that :func:`drawing_digest` can hand the model something it can read, and so
    that anything (the UI, or a model asked to add a box) can write elements
    back through this same call. ``png`` is a project-relative path to a
    flattened render and is never read for meaning.
    """
    get(root, session_id)
    if scene is None:
        scene = {}
    if not isinstance(scene, dict):
        raise ValueError("a drawing scene is a JSON object "
                         "({'elements': [...], 'appState': {...}})")
    blob = json.dumps(scene, separators=(",", ":"))
    if len(blob) > MAX_DRAWING:
        raise ValueError(f"that scene serialises to {len(blob)} bytes — the "
                         f"pad holds {MAX_DRAWING}")
    sets = ["drawing_json = ?"]
    params: list = [blob]
    if png is not None:
        sets.append("drawing_png = ?")
        params.append(str(png)[:500])
    params.append(int(session_id))
    with db.tx(root) as conn:
        conn.execute(f"UPDATE brainstorm_session SET {', '.join(sets)}, "
                     "updated_at = datetime('now') WHERE id = ?", params)
    return get(root, session_id)


def archive(root: str | os.PathLike[str], session_id: int,
            archived: bool = True) -> dict:
    """File it away, or take it back out. NOTHING IS DELETED either way.

    Un-archiving restores the status the session earned rather than a fixed one:
    a session that has already filed work is 'deployed' forever after, and
    resetting it to 'open' would erase the one field saying that work exists.
    """
    session = get(root, session_id)
    if archived:
        status = "archived"
        # A room nobody may speak in must not still be paying somebody to
        # listen. Archiving used to leave the spawned partner running with its
        # pipe open until an idle reap noticed half an hour later — invisible
        # from the browser, and exactly the "is it actually off?" doubt the
        # explicit close button exists to answer.
        close_partner(root, session_id)
    else:
        status = "deployed" if session["deploys"] else "open"
    with db.tx(root) as conn:
        conn.execute("UPDATE brainstorm_session SET status = ?, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (status, int(session_id)))
    activity.log(root, "brainstorm",
                 f"{'archived' if archived else 'reopened'} brainstorm "
                 f"{session_id}: {session['title'][:80]}",
                 seat=session["seat"], ref=str(session_id))
    return get(root, session_id)


def delete(root: str | os.PathLike[str], session_id: int) -> dict:
    """Really delete it, messages and all (the rows CASCADE).

    Archive is the everyday motion and this is not — but a pad you cannot throw
    away is a pad people stop using. Work items already filed from the session
    are NOT touched: they are on the board, they are somebody's job, and
    deleting the conversation that produced them must not delete them.
    """
    session = get(root, session_id)
    # The process first: deleting the rows out from under a live partner leaves
    # it answering questions about a session that no longer exists, and its next
    # pad_read would be the thing that discovered the deletion.
    close_partner(root, session_id)
    with db.tx(root) as conn:
        conn.execute("DELETE FROM brainstorm_session WHERE id = ?",
                     (int(session_id),))
    activity.log(root, "brainstorm",
                 f"deleted brainstorm {session_id}: {session['title'][:80]}",
                 seat=session["seat"], ref=str(session_id))
    return {"deleted": int(session_id), "title": session["title"]}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def append_message(root: str | os.PathLike[str], session_id: int, role: str,
                   text: str) -> dict:
    """One turn of the conversation. A row, and nothing else.

    Compare console.console_say, which creates a work item and dispatches it.
    This writes a message. There is no third thing it could do — the module
    cannot reach the queue from here.
    """
    get(root, session_id)
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}; got {role!r}")
    text = str(text or "").strip()
    if not text:
        raise ValueError("an empty message has nothing in it to think about")
    if len(text) > MAX_MESSAGE:
        raise ValueError(f"that message is {len(text)} characters — one turn "
                         f"holds {MAX_MESSAGE}; the notes pad holds a document")
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO brainstorm_message (session_id, role, text) "
            "VALUES (?, ?, ?)", (int(session_id), role, text))
        message_id = int(cur.lastrowid)
        _touch(conn, session_id)
    row = db.connect(root).execute(
        "SELECT * FROM brainstorm_message WHERE id = ?", (message_id,)).fetchone()
    return dict(row)


def messages(root: str | os.PathLike[str], session_id: int,
             limit: int = 500) -> list[dict]:
    return rows(db.connect(root).execute(
        "SELECT * FROM brainstorm_message WHERE session_id = ? "
        "ORDER BY id LIMIT ?", (int(session_id), max(1, int(limit)))))


def read(root: str | os.PathLike[str], session_id: int) -> dict:
    """A session and everything in it — what opening one from the file returns."""
    session = get(root, session_id)
    session["messages"] = messages(root, session_id)
    return session


def transcript(root: str | os.PathLike[str], session_id: int,
               window: int = TRANSCRIPT_WINDOW) -> list[dict]:
    """The conversation in the shape the model API takes.

    The tail, not the whole thing: see TRANSCRIPT_WINDOW.
    """
    return [{"role": m["role"], "content": m["text"]}
            for m in messages(root, session_id)[-max(1, int(window)):]]


# ---------------------------------------------------------------------------
# The drawing, as words
# ---------------------------------------------------------------------------

def elements(scene: Any) -> list[dict]:
    if not isinstance(scene, dict):
        return []
    found = scene.get("elements")
    return [e for e in found if isinstance(e, dict)] if isinstance(found, list) else []


def drawing_digest(scene: Any, limit: int = DIGEST_ELEMENTS) -> str:
    """The pad's elements as lines a text model can read.

    This is the whole reason the scene is stored structured. A PNG says "there
    is a diagram"; this says the box at the top is called "hub", the arrow out
    of it points at "shrine", and somebody wrote "optional?" beside it — which
    is the content of the drawing, and it is what the synthesis is supposed to
    be reasoning over.

    Element ids are included and kept short so a model asked to ADD to the
    drawing can refer to what is already there by name.
    """
    found = elements(scene)
    if not found:
        return ""
    lines: list[str] = []
    for el in found[:max(1, int(limit))]:
        kind = str(el.get("type") or "shape")
        ref = str(el.get("id") or "")[:8]
        label = str(el.get("text") or el.get("label") or "").strip()
        if isinstance(el.get("label"), dict):
            label = str(el["label"].get("text") or "").strip()
        label = label.replace("\n", " ")[:120]
        if kind in ("arrow", "line"):
            src = _bound(el.get("startBinding"))
            dst = _bound(el.get("endBinding"))
            arrow = f"{src or '?'} -> {dst or '?'}"
            lines.append(f"{kind}#{ref} {arrow}"
                         + (f' "{label}"' if label else ""))
            continue
        where = ""
        try:
            where = (f" at {int(float(el.get('x', 0)))},{int(float(el.get('y', 0)))}"
                     f" {int(float(el.get('width', 0)))}x"
                     f"{int(float(el.get('height', 0)))}")
        except (TypeError, ValueError):
            where = ""
        lines.append(f"{kind}#{ref}" + (f' "{label}"' if label else "") + where)
    if len(found) > limit:
        lines.append(f"… and {len(found) - limit} more elements")
    return "\n".join(lines)


def _bound(binding: Any) -> str:
    if isinstance(binding, dict):
        return str(binding.get("elementId") or "")[:8]
    return ""


# ---------------------------------------------------------------------------
# Prompts. Pure string building — no network, no client, no key.
# ---------------------------------------------------------------------------

_CHAT_COMMON = (
    "You are talking to the human who owns this game project. This is a "
    "BRAINSTORM, not a work order: nothing you say queues anything, dispatches "
    "anyone or spends anything, and you have no tools. Think WITH them.\n"
    "- Short. Two or three paragraphs at most, plain sentences.\n"
    "- Push back when something does not hold together, and say which part.\n"
    "- Ask the one question that would change the answer, not five.\n"
    "- Do not write a plan or a task list unless they ask for one; the plan is "
    "a separate step they take when they are ready."
)

_CHAT_SEAT = {
    "director": (
        "You hold the DIRECTOR seat: the pillars, the core loop and the "
        "priorities. You care what gets built, what it costs, and what is "
        "deliberately left out."),
    "narrative": (
        "You hold the NARRATIVE seat: canon, lore, character and the world's "
        "internal consistency. You care what is TRUE in this world and whether "
        "a new idea contradicts something already established."),
}


def chat_system(seat: str) -> str:
    """The system prompt for one conversational turn."""
    return f"{_CHAT_SEAT.get(seat, _CHAT_SEAT['director'])}\n\n{_CHAT_COMMON}"


_SYNTH_COMMON = (
    "Read the whole session — the conversation, the notes pad and the drawing "
    "— and propose the work it adds up to.\n\n"
    "Answer with JSON and nothing else, in exactly this shape:\n"
    '{"summary": "what this session decided, in a short paragraph",\n'
    ' "chained": true|false,\n'
    ' "questions": ["anything you need confirmed before this is filed"],\n'
    ' "items": [{"seat": "...", "title": "short imperative", '
    '"brief": "a self-contained brief the working agent can act on with no '
    'other context"}]}\n\n'
    "Rules:\n"
    "- Every brief must stand alone. The agent that reads it will not see this "
    "conversation, the notes or the drawing.\n"
    "- Set \"chained\": true ONLY when the items must run IN THE ORDER GIVEN "
    "because each needs what the one before it produced. Order alone is not a "
    "reason; a dependency is.\n"
    "- Propose what the session actually decided. Do not pad the list, and do "
    "not split one coherent task into fragments.\n"
    "- If the session decided nothing worth filing, return an empty items list "
    "and say so in the summary."
)

# THE GAME-PLAN HALF, and only the director gets it.
#
# `items` answers "what should happen next"; a game needs an answer to "what
# does this thing CONSIST of", and nothing produced one — so after a
# decomposition the board was the only record and an empty queue was
# indistinguishable from a finished game. The manifest is that second answer:
# enumerable, seat-tagged, dependency-aware, and the thing plan_status
# measures the build against forever after.
#
# ASKED FOR ONLY WHEN THE SESSION IS ABOUT A GAME OR A FEATURE, because a
# brainstorm about a bug fix or a pipeline change has no manifest and inventing
# one fills the coverage table with fiction that never gets built.
_SYNTH_MANIFEST = (
    "\n\nIF THIS SESSION DESIGNED A GAME, A FEATURE OR A CHUNK OF CONTENT, also "
    'return "manifest": a list of everything the thing CONSISTS of — one row '
    "per buildable piece:\n"
    '  {"kind": "entity|asset|scene|system|sound|dialogue|level",\n'
    '   "name": "unique_snake_case_name",\n'
    '   "seat": "which seat builds it",\n'
    '   "acceptance": "the test that settles whether it is done",\n'
    '   "slice": true|false,\n'
    '   "depends_on": ["names of rows this one needs first"]}\n'
    "Rules for the manifest:\n"
    "- MARK THE VERTICAL SLICE. slice:true is the smallest set that is "
    "actually PLAYABLE — one scene, one character, one loop, end to end. Those "
    "rows go on the board immediately; everything else is recorded and waits. "
    "A slice of everything is not a slice.\n"
    "- Names are unique and stable; they are how the row is matched forever.\n"
    "- acceptance is a TEST, not a restatement of the name: 'boots headless "
    "and the player spawns', not 'the hub room is done'.\n"
    "- depends_on names other rows, never items or files. A scene that needs "
    "the sprite AND the sound names both — dependencies are a graph now, not a "
    "line.\n"
    "- Cover the whole thing, including the parts nobody will build this week. "
    "The manifest is what makes 'what is left' answerable; rows you leave out "
    "are work nothing will ever notice is missing.\n"
    "- Omit the key entirely if this session was not about building something."
)

_SYNTH_SEAT = {
    "director": (
        "You are the DIRECTOR turning a brainstorm into work for the board. "
        f"Seats you may file for: {', '.join(ALLOWED_SEATS['director'])}."),
    "narrative": (
        "You are the NARRATIVE seat turning a brainstorm into CANON WORK. This "
        "is not a game-dev dispatch: each item updates the world — a lore "
        "entity, a canon fact, a relationship, a bible section — and every item "
        "you propose is for the 'narrative' seat. Say in each brief exactly "
        "what should be written and what it must not contradict."),
}


def synthesis_system(seat: str) -> str:
    base = f"{_SYNTH_SEAT.get(seat, _SYNTH_SEAT['director'])}\n\n{_SYNTH_COMMON}"
    # The narrative seat files canon, not buildable pieces; a manifest there
    # would be a coverage table of lore entries nobody can mark 'wired'.
    return base + (_SYNTH_MANIFEST if seat == "director" else "")


def session_context(session: dict, msgs: list[dict]) -> str:
    """Everything the session contains, as one readable block.

    The notes and the drawing ride into the SYNTHESIS but not into every chat
    turn: they are a scratch document that changes constantly, and resending a
    hundred-kilobyte pad on each message is the cost this whole feature exists
    to avoid.
    """
    parts = [f"SESSION: {session.get('title') or 'untitled'} "
             f"({session.get('seat')})"]
    if msgs:
        lines = [f"{m['role'].upper()}: {m['text']}" for m in msgs]
        parts.append("CONVERSATION\n" + "\n\n".join(lines))
    notes = str(session.get("notes") or "").strip()
    if notes:
        parts.append("NOTES PAD (the human's own writing)\n" + notes)
    digest = drawing_digest(session.get("drawing"))
    if digest:
        parts.append(
            "DRAWING PAD (the elements on the board, not a picture)\n" + digest)
    return "\n\n----\n\n".join(parts)


def world_context(root: str | os.PathLike[str], seat: str) -> str:
    """What is already settled, so a proposal does not contradict it.

    A synthesis without this proposes work the project has already ruled out and
    canon that already exists under another name — and it does it CONFIDENTLY,
    which is worse than refusing, because the human is reading a plan that looks
    informed.

    Each seat gets what its own decisions are made against and nothing else:

        director   the pillars, the core loop and the constraints. A plan that
                   contradicts one of those is wrong in a way the conversation
                   cannot see, and finding that out after a human confirmed it
                   is finding out too late.
        narrative  the canon that is already written. "Does this contradict
                   something established" is not a question that can be answered
                   from the conversation alone.

    Fully guarded and returns "" on any failure: a bible that will not read is a
    thinner prompt, never a failed synthesis. This is the SYNTHESIS's input only
    — a chat turn does not pay for it (see session_context).
    """
    try:
        if seat == "narrative":
            return _narrative_world(root)
        return _director_world(root)
    except Exception:
        return ""


def synthesis_turns(root: str | os.PathLike[str], session: dict) -> list[dict]:
    """The turns a SYNTHESIS is asked over: the world first, then the session.

    Shared so both doors ask the model the same question — a preview that
    differed by which door pressed it would make the human's review of one say
    nothing about the other.

    Raises ValueError on an empty session rather than asking: a synthesis over a
    title and nothing else bills for a model inventing a plan out of the air,
    and the plan looks exactly as confident as a real one.
    """
    msgs = session.get("messages")
    if msgs is None:
        msgs = messages(root, int(session["id"]))
    context = session_context(session, msgs)
    if not str(context or "").strip() or not msgs:
        raise ValueError("there is nothing in this session to synthesize yet — "
                         "say something, write a note, or draw")
    world = world_context(root, session["seat"])
    turns = [{"role": "user", "content": world}] if world else []
    turns.append({"role": "user", "content": context})
    return turns


def _director_world(root: str | os.PathLike[str]) -> str:
    from . import bible as _bible

    view = _bible.overview(root)
    parts: list[str] = []
    pillars = [f"- {s['title']}: {str(s['body'] or '')[:200]}"
               for s in view["pillars"][:WORLD_SECTIONS]]
    if pillars:
        parts.append("PILLARS\n" + "\n".join(pillars))
    # This used to add IN SCOPE and BELOW THE CUT LINE off the tier list, which
    # was the sharpest half of the block: a tier the project had DECIDED against
    # reads to a model like an obvious gap it should fill. The tiers are gone,
    # so the loop and the constraints carry it — they are what a proposal has to
    # not contradict, and unlike the tier list, projects actually write them.
    if view["loop"]:
        parts.append("CORE LOOP\n" + "\n".join(
            f"- {s['title']}: {str(s['body'] or '')[:200]}"
            for s in view["loop"][:WORLD_SECTIONS]))
    if view["constraints"]:
        parts.append("CONSTRAINTS — a proposal that breaks one of these is wrong\n"
                     + "\n".join(f"- {s['title']}: {str(s['body'] or '')[:200]}"
                                 for s in view["constraints"][:WORLD_SECTIONS]))
    return ("WHAT IS ALREADY SETTLED\n\n" + "\n\n".join(parts)) if parts else ""


def _narrative_world(root: str | os.PathLike[str]) -> str:
    from . import lore as _lore

    parts: list[str] = []
    entities = [e for e in _lore.list_entities(root) if e["status"] != "retired"]
    if entities:
        parts.append("ESTABLISHED ENTITIES (slug — kind — status)\n" + "\n".join(
            f"- {e['slug']} — {e['kind']} — {e['status']}: "
            f"{str(e['summary'] or '')[:160]}"
            for e in entities[:WORLD_ENTITIES]))
    facts = _lore.all_facts(root)
    if facts:
        # Locked facts first: those are the ones a proposal may not contradict
        # at all, as opposed to the ones it may propose revising.
        facts.sort(key=lambda f: (0 if f.get("locked") else 1, f.get("id") or 0))
        parts.append("CANON FACTS\n" + "\n".join(
            f"- {'[LOCKED] ' if f.get('locked') else ''}{str(f['statement'])[:200]}"
            for f in facts[:WORLD_FACTS]))
    return ("EXISTING CANON\n\n" + "\n\n".join(parts)) if parts else ""


# ---------------------------------------------------------------------------
# The thinking partner. ONE implementation, because the guarantee is INSIDE it.
# ---------------------------------------------------------------------------
# This lived in bgate_ui.routes.brainstorm while the dashboard was the only door,
# then here once there were two. It is still here for the same reason: the copy
# that would have drifted is the one running inside the MCP server, where tools
# are already in the room.
#
# WHAT IT USED TO BE, AND WHY THAT IS GONE. The partner was a bare OpenAI
# chat-completions call and the no-write guarantee was nearly free — that call
# is a messages array and a reply, and a kwarg allowlist kept `tools=` out of
# it. The human asked for a spawned Claude Code session instead ("this needs to
# spawn a claude code session like i said, not a gpt api"), which is the same
# thing the rest of this product runs on and a far better thinking partner —
# and which arrives holding Write, Edit, Bash and every MCP server registered on
# the machine, builders-gate and its queue_add included.
#
# THE GUARANTEE IS THEREFORE RE-ESTABLISHED, NOT REWORDED. It now rests on the
# argv the session is spawned with: an EMPTY built-in tool set, no MCP config
# and --strict-mcp-config so none is inherited, no settings sources to add any
# of it back, no slash commands, and plan mode behind all of that. See
# bgate_ui.brainsession, which owns it and records what was measured rather than
# assumed. That is a STRONGER guarantee than the allowlist it replaces: the
# allowlist dropped an argument that would have granted a capability, where this
# removes the capability.
#
# THIS MODULE STILL CANNOT REACH THE BOARD. file_plan imports the queue in its
# own body and nothing else here does — unchanged, and still asserted.

# A conversational turn is a person waiting at a text box; a synthesis is a
# session reading an hour of conversation. Different patience.
CHAT_TIMEOUT = 90.0
SYNTH_TIMEOUT = 240.0

# What the CALLER is told a turn costs when the runner does not report a price.
# Every runner that reports one (claude does, per turn, as total_cost_usd)
# overrides these with the real figure and writes it to the spend ledger — which
# the old chat-completions path never did, on the reasoning that fractions of a
# cent would bury the image bills. A spawned session is not fractions of a cent:
# one trivial measured turn was $0.06, so brainstorm turns are now LEDGERED.
USD_PER_CHAT = 0.0
USD_PER_SYNTH = 0.0


def _partner():
    """The spawned-session implementation.

    Imported lazily and in one place: core must not need the UI package at
    import time (bgate_core.workflows does the same for the dispatcher, with the
    same one-line reason), and a project whose dashboard package is broken must
    still be able to READ its brainstorms.
    """
    from bgate_ui import brainsession

    return brainsession


def available(root: Any = None) -> dict:
    """Can we talk at all, and to what? No spawn, no spend, to find out.

    Rides in the sessions index, which is polled, so it must stay cheap: it asks
    the runner table which CLI this project brainstorms on and whether that CLI
    is on disk. It no longer asks about OPENAI_API_KEY, because there is no
    longer an API call to make — the answer to "can this room think" became
    "is the CLI installed".
    """
    try:
        return _partner().available(root)
    except Exception as exc:
        # A dashboard package that will not import must not make the file
        # drawer un-openable; the index renders with the room marked unusable.
        return {"available": False, "runner": "", "model": "", "label": "",
                "readonly": False, "cost_tracked": False,
                "reason": f"no thinking partner here ({type(exc).__name__}: "
                          f"{exc})"[:300]}


def close_partner(root: Any, session_id: int) -> dict:
    """END THE RUNNING PROCESS. The conversation is not touched.

    THE THREE END-STATES, because three words that all sound final is worse than
    one and the human asked to be able to close a session CONFIDENTLY:

        close     (this) ends the CLI process. The transcript, the notes and the
                  drawing are rows in a table and are untouched. Saying anything
                  else reopens — resuming the same CLI conversation where it left
                  off, or replaying it if the CLI no longer has it. Nothing is
                  lost and nothing is decided. It is about the PROCESS.
        archive   files the SESSION away as a record: it takes no new turns, no
                  notes and no deploys until it is reopened. Implies a close,
                  because a room nobody may speak in must not still be paying
                  for somebody to listen. Reversible; deletes nothing.
        deployed  a STATUS, not an action: this session has put work on the
                  board. It is not an ending — the session stays open and can
                  take another batch later — but it now IMPLIES a close, because
                  a room that still answers is a room the next idea goes into,
                  and that is how one thread quietly accumulates three plans.
                  Speak in it and it reopens.

    Idempotent, and safe on a session that never had a partner.
    """
    try:
        return {**_partner().stop(root, int(session_id)),
                "session_id": int(session_id),
                "thinker": thinker(root, session_id)}
    except Exception as exc:
        return {"ok": False, "stopped": False,
                "error": f"{type(exc).__name__}: {exc}"[:300]}


def feed(root: Any, session_id: int, cursor: int = 0) -> dict:
    """THE TERMINAL CHANNEL: what the spawned session actually emitted.

    Everything — run boundaries, the CLI's own init (which is where the tool
    list it really built is stated), pad tool calls, their results, assistant
    prose. Read forward from a byte cursor so a live view can poll it.

    Distinct from the SPOKEN channel, which is the final assistant prose alone
    and is what lands in the transcript and goes to text-to-speech. The two come
    off the same turn and conflating them would either read tool JSON out loud
    or make this view a duplicate of the chat pane.
    """
    try:
        return _partner().feed(root, int(session_id), cursor=int(cursor or 0))
    except Exception as exc:
        return {"events": [], "cursor": int(cursor or 0), "size": 0,
                "error": f"{type(exc).__name__}: {exc}"[:300]}


def thinker(root: Any, session_id: int) -> dict:
    """What THIS session's partner is: runner, model, live or not, what it has
    cost, and where its raw transcript is on disk.

    Both doors put this in the session payload. It is what replaced the
    workspace header's `gpt-4o-mini` chip, and it is what a terminal view of the
    session will read to find the log to tail.
    """
    try:
        return _partner().thinker(root, int(session_id))
    except Exception as exc:
        return {**available(root), "live": False, "turns": 0,
                "spent_usd": 0.0, "cli_session_id": "",
                "session_id": int(session_id),
                "reason": f"{type(exc).__name__}: {exc}"[:300]}


def ask(root: Any, system: str, turns: list[dict], *, session_id: int = 0,
        persist: bool = True, timeout: float = CHAT_TIMEOUT,
        usd: float = USD_PER_CHAT, tag: str = "") -> dict:
    """One turn with the thinking partner, in the adapters' shared result shape.

    ``{ok, text, model, seconds, estimated_usd}`` or ``{ok: False, error}`` — a
    failure is RETURNED rather than raised because the caller has usually
    already stored the human's message and must not lose it to a CLI that would
    not start.

    ``session_id`` with ``persist`` is what makes a brainstorm a CONVERSATION:
    one process is held open for the room and each message is its next turn.
    A synthesis passes ``persist=False`` — a single question under a different
    system prompt, whose answer must not land in the middle of the transcript
    the human is reading.

    The session is handed a system prompt and text. It has no tools, no MCP
    server and no permission to write: not as a policy it is asked to respect,
    but as a process built without any of them. Compare a dispatched agent,
    which holds ``queue_add`` from its first token.
    """
    started = _time.monotonic()
    try:
        answer = _partner().ask(root, int(session_id or 0), system, turns,
                                persist=bool(persist and session_id),
                                timeout=timeout, tag=tag)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400],
                "seconds": round(_time.monotonic() - started, 2),
                "estimated_usd": 0.0}
    # `usd` survives as the floor for a runner that reports no price of its own
    # (codex reports tokens and no dollars). A real figure always wins.
    if answer.get("ok") and not answer.get("estimated_usd"):
        answer["estimated_usd"] = usd
    return answer


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def _json_block(text: str) -> Optional[dict]:
    """The first JSON object in a model reply, fences and preamble and all.

    Models wrap JSON in ```json despite being told not to, and occasionally put
    a sentence in front of it. Refusing those replies would mean a failed
    synthesis and a second bill for the same question.
    """
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw[3:]
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_plan(text: str, seat: str) -> dict:
    """A model reply -> the proposed plan, normalised and made safe to show.

    LENIENT ON PURPOSE, AND IT WRITES NOTHING. This produces the preview a human
    reads before confirming, so a model that named a seat that does not exist
    should cost them a visible correction, not a failed synthesis and a second
    bill. Every repair is recorded in ``notes`` so the correction is on screen
    rather than silent. :func:`validate_plan` is the strict half, and it runs on
    the way IN to the queue.
    """
    allowed = ALLOWED_SEATS.get(seat, ALLOWED_SEATS["director"])
    fallback = seat if seat in allowed else allowed[0]
    parsed = _json_block(text)
    if parsed is None:
        return {"summary": str(text or "").strip()[:2000], "items": [],
                "chained": False, "questions": [],
                "notes": ["the model did not answer with JSON — its reply is "
                          "the summary, and there is nothing to file"]}
    notes: list[str] = []
    items: list[dict] = []
    raw_items = parsed.get("items")
    for i, raw in enumerate(raw_items if isinstance(raw_items, list) else [], 1):
        if not isinstance(raw, dict):
            notes.append(f"item {i} was not an object and was dropped")
            continue
        title = str(raw.get("title") or "").strip()[:MAX_TITLE]
        if not title:
            notes.append(f"item {i} had no title and was dropped")
            continue
        want = str(raw.get("seat") or "").strip()
        if want not in allowed:
            notes.append(f"item {i} asked for seat {want or '(none)'!r}, which a "
                         f"{seat} session cannot file — moved to {fallback!r}")
            want = fallback
        brief = str(raw.get("brief") or "").strip()
        if not brief:
            brief = title
            notes.append(f"item {i} had no brief — its title is standing in, "
                         "which the agent that picks it up will feel")
        items.append({"seat": want, "title": title, "brief": brief,
                      "priority": _int(raw.get("priority"))})
    chained = bool(parsed.get("chained")) and len(items) > 1
    if bool(parsed.get("chained")) and len(items) < 2:
        notes.append("a chain needs two links — this will be filed as a single "
                     "item")
    questions = [str(q).strip()[:400] for q in parsed.get("questions") or []
                 if str(q).strip()][:8]
    return {
        "summary": str(parsed.get("summary") or "").strip()[:4000],
        "items": items,
        "chained": chained,
        "questions": questions,
        "notes": notes,
    }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def validate_plan(plan: Any, seat: str) -> dict:
    """The strict half — everything the queue is about to be handed.

    Raises rather than repairing. This runs on a plan a human has CONFIRMED, and
    quietly rewriting one at that point means filing something other than what
    they approved, which is the one thing a confirmation step must never do.
    """
    if not isinstance(plan, dict):
        raise ValueError("a plan is an object with an 'items' list")
    allowed = ALLOWED_SEATS.get(seat, ALLOWED_SEATS["director"])
    raw_items = plan.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("that plan has no items — there is nothing to file")
    items: list[dict] = []
    for i, raw in enumerate(raw_items, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"plan item {i} is not an object")
        title = str(raw.get("title") or "").strip()[:MAX_TITLE]
        if not title:
            raise ValueError(f"plan item {i} has no title")
        want = str(raw.get("seat") or "").strip()
        if want not in allowed:
            raise ValueError(
                f"plan item {i} names seat {want or '(none)'!r}; a {seat} "
                f"brainstorm may file for {', '.join(allowed)}")
        items.append({"seat": want, "title": title,
                      "brief": str(raw.get("brief") or "").strip() or title,
                      "priority": _int(raw.get("priority"))})
    return {
        "summary": str(plan.get("summary") or "").strip()[:4000],
        "items": items,
        "chained": bool(plan.get("chained")) and len(items) > 1,
    }


def plan_fingerprint(plan: dict) -> str:
    """What makes two plans the same plan, for the double-file guard.

    Seat + title only: the brief is prose a human may have edited between the
    preview and the confirm, and treating a reworded brief as a different plan
    would make the guard useless exactly when somebody is iterating.
    """
    return json.dumps([[i["seat"], i["title"]] for i in plan.get("items") or []],
                      separators=(",", ":"))


def record_deploy(root: str | os.PathLike[str], session_id: int, plan: dict,
                  filed: list[dict], *, chain_id: str = "",
                  by: str = "") -> dict:
    """Remember what this session put on the board.

    Both directions are recorded and both are needed. Each work item carries
    ``source='brainstorm'`` and ``source_ref=<session id>``, so an item on the
    board can name the conversation it came from; this list is the other way
    round, so a session reopened next month can say what it produced without
    scanning the queue for a source_ref that a deleted item no longer has.
    """
    session = get(root, session_id)
    entry = {
        "at": _stamp(root),
        "by": (by or activity.current_actor())[:120],
        "chain_id": chain_id,
        "summary": str(plan.get("summary") or "")[:2000],
        "fingerprint": plan_fingerprint(plan),
        "items": [{"id": int(f["id"]), "seat": f["seat"],
                   "title": str(f["title"])[:200]} for f in filed],
    }
    deploys = list(session["deploys"]) + [entry]
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE brainstorm_session SET deploys_json = ?, status = 'deployed', "
            "updated_at = datetime('now') WHERE id = ?",
            (json.dumps(deploys, separators=(",", ":")), int(session_id)))
    activity.log(root, "brainstorm",
                 f"deployed brainstorm {session_id}: "
                 + ", ".join(f"#{f['id']}[{f['seat']}]" for f in filed)[:300],
                 seat=session["seat"], ref=str(session_id))
    return get(root, session_id)


def already_filed(session: dict, plan: dict) -> Optional[dict]:
    """The deploy that already filed this exact plan, if there is one.

    The guard against the double-click, which on this endpoint files every item
    twice. Deliberately not a blanket "one deploy per session": a session people
    keep talking in legitimately produces a second batch later, and refusing
    that would push them into opening a throwaway session to get around it.
    """
    mark = plan_fingerprint(plan)
    for entry in session.get("deploys") or []:
        if entry.get("fingerprint") == mark:
            return entry
    return None


def _stamp(root: str | os.PathLike[str]) -> str:
    """SQLite's clock, not Python's, so every timestamp in the project agrees."""
    return str(db.connect(root).execute(
        "SELECT datetime('now') AS now").fetchone()["now"])


def file_plan(root: str | os.PathLike[str], session: dict, plan: Any, *,
              again: bool = False, by: str = "") -> dict:
    """FILE THE CONFIRMED PLAN. Exactly it, and nothing else.

    THE ONLY FUNCTION IN THIS MODULE THAT CAN REACH THE BOARD. ``queue`` is
    imported in this body and nowhere else, so the module carries no queue
    attribute for the conversational half to reach for — see the module
    docstring, and tests/test_brainstorm.py, which asserts the absence.

    ``plan`` comes from the caller, never from a second synthesis: the point of
    the preview is that a human read it, and re-asking the model here would file
    something nobody approved. :func:`validate_plan` runs on it — strict, not
    lenient, because quietly repairing a plan at this point means filing
    something other than what was confirmed.

    Chained plans go through ``queue.add_chain``, which is what makes ordering
    real. Priority is a preference among things that are all ready, so two items
    where the second needs the first's output would otherwise be dispatched in
    the same tick and the second would write against a file that does not exist.

    Raises :class:`Archived`, :class:`AlreadyFiled` (the double-file guard, which
    ``again=True`` overrides), :class:`PartialDeploy` (some items landed, then
    the queue refused one — out of scope, most often) or a plain ValueError from
    validation. Each door maps those to its own vocabulary; the decisions behind
    them are made once, here.
    """
    from . import queue as _queue

    ensure_open(session)
    clean = validate_plan(plan, session["seat"])
    prior = already_filed(session, clean)
    if prior and not again:
        # Not "one deploy per session" — a session people keep talking in
        # legitimately produces a second batch later, and refusing that would
        # push them into opening a throwaway session to get around it. This
        # exact plan, though, has been filed, and a repeat is never what the
        # second press meant.
        raise AlreadyFiled(prior)

    session_id = int(session["id"])
    links = [{"seat": i["seat"], "title": i["title"], "brief": i["brief"],
              "priority": i["priority"]} for i in clean["items"]]
    filed: list[dict] = []
    chain_id = ""
    try:
        if clean["chained"] and len(links) > 1:
            filed = _queue.add_chain(root, links, source="brainstorm",
                                     source_ref=str(session_id))
            chain_id = str(filed[0].get("chain_id") or "")
        else:
            for link in links:
                filed.append(_queue.add(
                    root, link["seat"], title=link["title"],
                    brief=link["brief"], priority=link["priority"],
                    source="brainstorm", source_ref=str(session_id)))
    except ValueError as exc:
        # Out-of-scope is a ValueError subclass and reads as a real sentence.
        # Whatever DID land is recorded before we raise.
        if filed:
            record_deploy(root, session_id, clean, filed, chain_id=chain_id,
                          by=by)
        raise PartialDeploy(
            f"{exc} — {len(filed)} of {len(links)} item(s) were filed before "
            "this failed", filed) from exc

    updated = record_deploy(root, session_id, clean, filed, chain_id=chain_id,
                            by=by)
    # DEPLOYING ENDS THE PARTNER PROCESS. This used to leave it running, on the
    # reasoning that a deployed session is one people keep talking in — which is
    # true of the SESSION and was the wrong conclusion about the PROCESS. A room
    # that still answers looks exactly like a room that should be spoken to, so
    # the next idea goes into the same conversation as the one already on the
    # board, and the thread grows a second and third plan behind a transcript
    # nobody re-reads. Observed, on this project's own director seat: a chat
    # opened the next day was still the previous day's conversation, and requests
    # had been stacking in it unnoticed.
    #
    # Nothing is lost and nothing is decided — this is `close`, whose whole
    # contract is that the transcript, the notes and the drawing are rows in a
    # table and stay exactly as they are. Saying anything in the room reopens it,
    # resuming where it left off. The only thing that changes is that continuing
    # is now a thing somebody chose rather than a thing that did not stop.
    closed = None
    try:
        closed = close_partner(root, session_id)
    except Exception as exc:                                    # noqa: BLE001
        # The work is on the board. A partner that would not shut down is worth
        # reporting and is not worth failing a deploy over — the items are filed
        # and re-running the deploy would file them twice.
        closed = {"ok": False,
                  "note": f"the plan is on the board, but the thinking partner "
                          f"could not be shut down ({type(exc).__name__}: {exc})"
                          " — close it from the session view."}
    return {
        "session": updated,
        "chain_id": chain_id,
        "chained": bool(chain_id),
        "closed": closed,
        "filed": [{"id": int(f["id"]), "seat": f["seat"], "title": f["title"],
                   "status": f["status"], "chain_pos": f.get("chain_pos") or 0,
                   "depends_on": f.get("depends_on")} for f in filed],
    }
