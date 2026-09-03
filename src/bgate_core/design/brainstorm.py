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
:func:`file_plan` — and it imports :mod:`bgate_core.board.queue` inside its own body,
so the module still has no ``queue`` attribute for anything else to reach for.
tests/ui/test_brainstorm.py asserts that absence, which turns "we agreed not to"
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
and the details of it are in :mod:`bgate_ui.agents.brainsession` next to the evidence.

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

from ..board import activity, seats as _seats
from ..store import db
from ..store.util import rows

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

# WHO ELSE IS IN THE ROOM.
#
#   invited  a row exists and no process does — either the spawn failed at
#            invite time, or the room was closed since. The next message
#            addressed to this seat starts one.
#   live     a process is holding a pipe for this seat right now.
#   left     was here, is not. The row and its spend stay: a session's cost
#            must not become unaccountable because somebody tidied the roster.
PARTICIPANT_STATES = ("invited", "live", "left")

# The states that mean "in the room". A seat in one of these cannot be invited
# again (that is the duplicate refusal) and may answer a message.
PRESENT = ("invited", "live")

# How many seats one room may hold at once, not counting the owner's own
# partner. Each is a CLI process, and brainsession.MAX_LIVE caps them at six
# across every project — a room that invited eight would evict its own earlier
# guests and the roster would show seats whose process was reaped to make space
# for the seat below them.
MAX_PARTICIPANTS = 4

# HOW MANY EXTRA ROUNDS A ROOM MAY TALK AMONG ITSELF, and the sentinel a voice
# uses to drop out of one. See migration 0039 for why this is per-room and off
# by default.
#
# The ceiling is the schema's (CHECK 0..6) and is small on purpose: a round
# costs one billed CLI turn per voice present, so a full room at 6 rounds is 30
# turns on one human sentence. Past that a human should be reading and steering,
# not buying more of the same argument.
DISCUSS_MAX_ROUNDS = 6

# WHAT A VOICE SAYS WHEN IT HAS NOTHING TO ADD. Matched case-insensitively on a
# stripped reply, and such a turn is NOT written to the transcript — a room
# whose follow-up rounds are four seats saying "PASS" is unreadable, and the
# whole point of the sentinel is to end the discussion early rather than to
# record that it could have.
DISCUSS_PASS = "pass"

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


class AlreadyHere(Exception):
    """That seat is already in this room, or IS this room.

    Its own type because the two doors map it to the same thing (a conflict,
    not a bad request) and because "already here" is the one invite refusal
    that is not the caller getting something wrong.
    """


class NoPartner(RuntimeError):
    """Nothing can be spawned here, and the reason is a sentence.

    Raised by :func:`invite` when the runner has not declared a read-only
    conversational mode, or its CLI is not on this machine. Deliberately NOT a
    ValueError: it is a fact about the environment, not about the request, and
    the two want different HTTP codes and different buttons.
    """


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
    # WHO IS IN EACH ROOM, AND WHAT IT HAS COST, ride along as aggregates.
    #
    # The rooms rail draws a dot per seat present, so "the room with gameplay
    # and art in it" is findable without opening four rooms — and a room's spend
    # belongs in the list for the same reason a work item's does: the number you
    # need before you click is the one that tells you whether to. Both are one
    # correlated subquery over an indexed column rather than a per-row read from
    # the caller, which is what made the old rail need N+1 requests to draw a
    # dot.
    #
    # `guest_seats` is a comma-joined string because SQLite has no array type
    # and a JSON blob here would be parsed by every reader; the callers split on
    # ',' and drop the empty. Only PRESENT seats: a seat that left is still in
    # the spend, correctly, but it is not in the room.
    present = ",".join("?" * len(PRESENT))
    sql = ("SELECT s.id, s.seat, s.title, s.status, s.created_at, s.updated_at, "
           "       length(s.notes) AS notes_len, "
           "       (SELECT count(*) FROM brainstorm_message m "
           "        WHERE m.session_id = s.id) AS messages, "
           "       (SELECT group_concat(p.seat) FROM brainstorm_participant p "
           f"        WHERE p.session_id = s.id AND p.state IN ({present})) "
           "           AS guest_seats, "
           "       (SELECT COALESCE(sum(p.spent_usd), 0) "
           "          FROM brainstorm_participant p "
           "         WHERE p.session_id = s.id) AS spent_usd "
           "FROM brainstorm_session s WHERE 1=1")
    params: list = list(PRESENT)
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
    out = rows(db.connect(root).execute(sql, params))
    for row in out:
        row["guests"] = [s for s in str(row.pop("guest_seats", "") or "").split(",")
                         if s]
    return out


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


def set_discuss(root: str | os.PathLike[str], session_id: int,
                rounds: int) -> dict:
    """How many EXTRA rounds this room talks among itself. 0 turns it off.

    Refuses out of range rather than clamping. Clamping a 40 to a 6 would look
    like it worked and quietly bill six rounds to somebody who thought they had
    asked for forty and would have said "no, never mind" if told.
    """
    get(root, session_id)
    try:
        want = int(rounds)
    except (TypeError, ValueError):
        raise ValueError("rounds must be a whole number of rounds, 0 to "
                         f"{DISCUSS_MAX_ROUNDS}")
    if not 0 <= want <= DISCUSS_MAX_ROUNDS:
        raise ValueError(
            f"rounds must be between 0 (off) and {DISCUSS_MAX_ROUNDS}; got "
            f"{want}. Each round is one billed turn per voice in the room.")
    with db.tx(root) as conn:
        conn.execute("UPDATE brainstorm_session SET discuss_rounds = ?, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (want, int(session_id)))
    return get(root, session_id)


def discuss_rounds(session: dict) -> int:
    """The room's setting, read defensively. A session dict from an older
    reader (or a test fixture built by hand) simply has no discussion."""
    try:
        return max(0, min(DISCUSS_MAX_ROUNDS,
                          int(session.get("discuss_rounds") or 0)))
    except (TypeError, ValueError):
        return 0


def is_pass(text: Any) -> bool:
    """Did this voice drop out of the round?

    Deliberately strict — the exact word and nothing else, punctuation allowed.
    A loose match ("nothing to add here, though the tile budget worries me")
    would delete an opinion the human wanted to read.
    """
    return str(text or "").strip().strip(".!").lower() == DISCUSS_PASS


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


def reset(root: str | os.PathLike[str], session_id: int, *,
          keep_pads: bool = True) -> dict:
    """START THE CONVERSATION OVER in the same room. The FOURTH end-state.

    close/archive/delete (see :func:`close_partner`) are all wrong for the thing
    people actually want most often: this thread has gone somewhere useless, or
    the partner has answered from a stale premise three times running, and they
    want a clean head WITHOUT losing the notes and the diagram they have been
    building for an hour. Before this there was no motion for it — close reopens
    where it left off, delete takes the pads with it, and the only workaround
    was to make a new session and copy the pads across by hand.

    So: the partner is stopped and the TRANSCRIPT is dropped, which is what
    makes the next turn start from nothing rather than resuming. The notes and
    the drawing survive by default because they are the human's own document,
    not the conversation — pass keep_pads=False to clear those too, which is the
    "same room, nothing in it" motion.

    Deploys are never touched. Work already on the board outlives the thread
    that thought of it, the same as in :func:`delete`.
    """
    session = get(root, session_id)
    # Stop the process BEFORE dropping its rows, same reason as delete(): a live
    # partner whose transcript vanished underneath it would discover that on its
    # next call and answer out of a context nothing else can see.
    close_partner(root, session_id)
    with db.tx(root) as conn:
        cur = conn.execute("DELETE FROM brainstorm_message WHERE session_id = ?",
                           (int(session_id),))
        dropped = int(cur.rowcount or 0)
        if keep_pads:
            conn.execute("UPDATE brainstorm_session SET updated_at = "
                         "datetime('now') WHERE id = ?", (int(session_id),))
        else:
            conn.execute(
                "UPDATE brainstorm_session SET notes = '', drawing_json = '{}', "
                "drawing_png = '', updated_at = datetime('now') WHERE id = ?",
                (int(session_id),))
    activity.log(root, "brainstorm",
                 f"reset brainstorm {session_id}: dropped {dropped} message(s)"
                 + ("" if keep_pads else ", cleared the pads"),
                 seat=session["seat"], ref=str(session_id))
    return {"session_id": int(session_id), "dropped": dropped,
            "kept_pads": bool(keep_pads),
            "session": get(root, session_id)}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def append_message(root: str | os.PathLike[str], session_id: int, role: str,
                   text: str, seat: str = "") -> dict:
    """One turn of the conversation. A row, and nothing else.

    Compare console.console_say, which creates a work item and dispatches it.
    This writes a message. There is no third thing it could do — the module
    cannot reach the queue from here.

    ``seat`` is WHO SAID IT, and "" is not a gap. On a user row it is the human,
    who holds no seat; on an assistant row it is the ROOM'S OWN partner — the
    voice this room always had. A named seat means an invited participant
    answered, which is the only case that needs attributing, because it is the
    only case where two assistant rows in a row came from different processes.
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
            "INSERT INTO brainstorm_message (session_id, role, text, seat) "
            "VALUES (?, ?, ?, ?)",
            (int(session_id), role, text, str(seat or "")[:32]))
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
    """A session and everything in it — what opening one from the file returns.

    ``participants`` rides here rather than only on the dashboard's own endpoint
    so both doors see the same room: an agent reading a session through the MCP
    server is told who else is in it, which is the difference between quoting
    the art seat's estimate and inventing one.
    """
    session = get(root, session_id)
    session["messages"] = messages(root, session_id)
    session["participants"] = participants(root, session_id)
    return session


def transcript(root: str | os.PathLike[str], session_id: int,
               window: int = TRANSCRIPT_WINDOW, *,
               for_seat: str = "") -> list[dict]:
    """The conversation in the shape the model API takes, FROM ONE VOICE'S SEAT.

    The tail, not the whole thing: see TRANSCRIPT_WINDOW.

    A room with participants has more than two speakers, and the messages array
    has only two roles. So ``for_seat`` decides which rows are "yours": rows
    this seat wrote are ``assistant`` turns and everything else is a ``user``
    turn, LABELLED with who said it. Without the label a participant reads
    another seat's opinion as the human's instruction and answers the wrong
    person; with the rows unrelabelled it would read its own name in the third
    person and argue with itself.

    for_seat="" is the room's own partner and is byte-identical to what this
    function returned before participants existed, as long as nobody was ever
    invited — every historical row has seat='' (see migration 0036).
    """
    want = str(for_seat or "")
    out: list[dict] = []
    for m in messages(root, session_id)[-max(1, int(window)):]:
        who = str(m.get("seat") or "")
        if m["role"] == "assistant" and who == want:
            out.append({"role": "assistant", "content": m["text"]})
            continue
        if m["role"] == "assistant":
            # Another voice in the room. Named, because "somebody said this"
            # with no name is indistinguishable from the human saying it.
            label = f"{who.upper()} SEAT" if who else "THE ROOM'S PARTNER"
            out.append({"role": "user", "content": f"{label}: {m['text']}"})
            continue
        out.append({"role": "user", "content": m["text"]})
    return out


# ---------------------------------------------------------------------------
# The roster — who else is in the room
# ---------------------------------------------------------------------------
# ONE ROOM, ANY SEAT, AND EVERY SEAT ENTERS WITHOUT ITS TOOLS.
#
# The room had two voices: the human, and the owning seat's thinking partner.
# The question a human actually has at "what if the hub had weather" is one only
# the seat that would BUILD it can answer, and that seat was not reachable — so
# the answer was guessed by the seat that builds nothing, confidently.
#
# THE GUARANTEE IS WHY THIS IS NOT SIMPLY "DISPATCH THE SEAT". A dispatched
# agent arrives holding Write, Edit, Bash and the whole builders-gate MCP server
# including queue_add: put one in this room and the board is back inside the
# conversation, which is the exact thing the room exists to be free of. An
# invited seat is therefore spawned through brainsession's ONE spawner, with the
# same read-only argv as the room's own partner — empty built-in tool set,
# --strict-mcp-config, no settings sources, and the two-tool pad server or
# nothing. It is the seat's JUDGEMENT in the room, not the seat's HANDS.
#
# WHAT A PARTICIPANT SAYS IS AN OPINION. It lands as a brainstorm message with
# its seat on it and does nothing else. It cannot file, claim, lock or start
# anything, and nothing downstream reads these rows as work. Turning any of it
# into work is still exactly one motion: a human reads a synthesis and presses
# Deploy.

def _seat_table(root: str | os.PathLike[str]) -> dict:
    """The project's seats, or the defaults if the table will not read.

    Never raises: a seat_config row that will not parse must not make the room
    un-openable. Falling back to the defaults is the safe direction — it can
    only ever offer a seat the project meant to disable, and `invite` re-checks
    against this same function, so a genuinely disabled seat is refused by the
    only call that matters.
    """
    try:
        return _seats.roles_for(root)
    except Exception:
        return {r: {**cfg, "role": r, "enabled": True}
                for r, cfg in _seats.DEFAULT_SEATS.items()}


def participants(root: str | os.PathLike[str], session_id: int) -> list[dict]:
    """Who is in this room, what it has cost, and whether it is actually running.

    ``state`` is the RECORD and ``live`` is the OBSERVATION, and they are
    separate fields because they disagree in a way a roster has to show: a
    dashboard restart, an idle reap or an LRU eviction kills the process without
    touching the row, so a seat can legitimately be 'live' in the table with no
    process behind it. Drawing the row's state alone would show a seat as
    present that answers nothing; drawing `live` alone would drop a seat that
    was invited and simply has not been spoken to yet. Both, and let the roster
    say which it means.
    """
    rows_ = rows(db.connect(root).execute(
        "SELECT * FROM brainstorm_participant WHERE session_id = ? "
        "ORDER BY invited_at, id", (int(session_id),)))
    out = []
    for row in rows_:
        detail = {}
        if row["state"] in PRESENT:
            # Only for seats meant to be here: asking about a seat that left
            # spawns nothing but does read a sidecar off disk per row.
            detail = thinker(root, session_id, seat=row["seat"])
        out.append({**row,
                    "live": bool(detail.get("live")),
                    "thinker": detail})
    return out


def participant(root: str | os.PathLike[str], session_id: int,
                seat: str) -> Optional[dict]:
    row = db.connect(root).execute(
        "SELECT * FROM brainstorm_participant WHERE session_id = ? AND seat = ?",
        (int(session_id), str(seat))).fetchone()
    return dict(row) if row is not None else None


_PARTICIPANT_ROOM = (
    "You have been INVITED INTO A BRAINSTORM somebody else owns. You are a "
    "guest with an opinion, not the person running the room.\n"
    "- You cannot MAKE ANYTHING HAPPEN. No file, no command, no work filed, no "
    "agent dispatched - nothing you say or write here becomes work. The human "
    "turns this conversation into work in a separate step, later, by reading a "
    "plan and pressing Deploy.\n"
    "- Answer from your seat. The reason you were asked is that you know what "
    "this costs, what it breaks and what already exists in your area - say "
    "that, including when the answer is 'that is a fortnight, not an "
    "afternoon'.\n"
    "- Do not restate what the others said, and do not write a task list.\n"
    "- Other seats are speaking in here too; their turns are labelled with "
    "their seat. Disagree with them by name when you disagree.\n"
    "- If a question is not yours to answer, say whose it is in one line rather "
    "than answering it anyway."
)


def nodash(text: str) -> str:
    """Strip em and en dashes out of text on its way into a prompt.

    THE RULE IS NOT ENOUGH ON ITS OWN. Telling a model never to use an em dash
    while handing it a prompt full of them is asking it to ignore the strongest
    signal in its context, and it will. The room's own blocks were rewritten by
    hand; this catches everything that arrives from somewhere else at runtime -
    seat missions (including a project's customised wording), the bible, the
    lore, the constraints. A dash between clauses becomes a full stop, a dash
    round an aside becomes a comma; hyphens inside words are untouched.
    """
    out = str(text or "")
    for dash in ("—", "–"):
        out = out.replace(f" {dash} ", ". ").replace(dash, "-")
    return out


def participant_system(root: str | os.PathLike[str], seat: str, *,
                       discuss: bool = False) -> str:
    """The system prompt an invited seat thinks under.

    Its own MISSION from the seat table (a project that customised it gets its
    own wording, which is the point of that table) plus the room's rules. It is
    deliberately NOT seats.brief(): that is the full working brief — lanes,
    locks, refs, the write oracle — for an agent about to touch the repo, and
    handing it to a guest that holds no tools would describe a job it cannot do
    and bill for the tokens.
    """
    table = _seat_table(root)
    cfg = table.get(seat) or _seats.DEFAULT_SEATS.get(seat) or {}
    title = str(cfg.get("title") or seat).strip()
    mission = nodash(str(cfg.get("mission") or "").strip())
    head = f"You hold the {title.upper()} seat on this game project."
    if mission:
        head += f" Your standing brief: {mission}"
    # WHO THEY ARE RIDES WITH WHAT THEY DO, right at the top where the seat's
    # identity is established rather than down among the room's rules. A
    # project that has customised its mission keeps that wording; this adds the
    # person the mission was never going to carry.
    person = _PERSONALITY.get(seat, "")
    if person:
        head += f"\n\n{person}"
    # _VOICE, _STANCE and _DISCUSS are defined further down the module;
    # referenced at call time, not at import time, so the guest and the room's
    # own partner share one voice, one stance and one discussion rule.
    out = f"{head}\n\n{_PARTICIPANT_ROOM}\n\n{_VOICE}\n\n{_STANCE}\n\n{_TOOLS}"
    # The world, for the same reason the room's own partner gets it: a guest
    # seat asked about a character it cannot look up will invent one, and it
    # will do it in the confident register of somebody who knows.
    world = nodash(room_world(root))
    if world:
        out = f"{out}\n\n{world}"
    return f"{out}\n\n{_DISCUSS}" if discuss else out


def invite(root: str | os.PathLike[str], session_id: int, seat: str, *,
           by: str = "") -> dict:
    """Bring one seat into the room, READ-ONLY, and record that it is here.

    Every refusal below says WHY in its message, because the caller is a human
    looking at a roster and "invite failed" tells them nothing about which of
    four different situations they are in:

        the seat is not a seat        a typo, or a seat this build removed
        the project disabled it       seat_configure turned it off, and a room
                                      that ignored that would be a second place
                                      seats exist
        it is already here            including the owner, whose partner IS the
                                      room's own voice — inviting it would put
                                      two of the same seat in one conversation
        nothing can be spawned        the runner declares no read-only mode, or
                                      its CLI is not installed

    THE FOURTH ONE IS THE GUARANTEE. A runner with no ``chat`` entry is REFUSED
    rather than started with the dispatch argv — see runners.py, where codex has
    no such entry on purpose and says why. There is no path here that falls back
    to a runner that could write.

    The row is written BEFORE the spawn and stays if the spawn fails, in state
    'invited': the human asked for this seat, that is a fact, and losing it
    because a CLI was missing would make the roster disagree with what they did.
    """
    session = get(root, session_id)
    ensure_open(session)
    seat = str(seat or "").strip().lower()

    table = _seat_table(root)
    if seat not in _seats.DEFAULT_SEATS:
        raise ValueError(f"{seat or '(none)'!r} is not a seat on this project; "
                         f"seats are {', '.join(sorted(_seats.DEFAULT_SEATS))}")
    if seat not in table:
        raise ValueError(
            f"the {seat} seat is disabled on this project — enable it with "
            "seat_configure before inviting it, or the room would be a second "
            "place seats exist")
    if seat == session["seat"]:
        raise AlreadyHere(
            f"the {seat} seat already owns this room — its partner is the "
            "room's own voice, and a second copy of it would be two of the same "
            "seat arguing in one conversation")
    here = participant(root, session_id, seat)
    if here and here["state"] in PRESENT:
        raise AlreadyHere(f"the {seat} seat is already in this room "
                          f"({here['state']}) — say something to it instead")
    present = [p for p in participants(root, session_id)
               if p["state"] in PRESENT]
    if len(present) >= MAX_PARTICIPANTS:
        raise ValueError(
            f"this room already holds {len(present)} invited seats, which is "
            f"the limit ({MAX_PARTICIPANTS}) — each one is a live CLI process, "
            "and a room over the limit starts evicting its own earlier guests. "
            "Have one leave first")

    with db.tx(root) as conn:
        conn.execute(
            "INSERT INTO brainstorm_participant (session_id, seat, state, "
            "invited_by, invited_at) VALUES (?, ?, 'invited', ?, datetime('now')) "
            "ON CONFLICT (session_id, seat) DO UPDATE SET "
            "  state = 'invited', invited_by = excluded.invited_by, "
            "  invited_at = excluded.invited_at, left_at = ''",
            (int(session_id), seat,
             (by or activity.current_actor() or "")[:120]))
        _touch(conn, session_id)

    started, error = None, ""
    try:
        started = _partner().start(root, int(session_id),
                                   participant_system(root, seat), seat)
    except Exception as exc:                                    # noqa: BLE001
        # Including brainsession.Unavailable, which is the read-only refusal and
        # the missing-CLI one. Reported rather than raised so the row survives:
        # see the docstring. The caller decides whether an un-spawned invite is
        # an error to show — the routes raise 503 on it, the MCP door reports it.
        error = f"{exc}"[:300]
    if started:
        _set_state(root, session_id, seat, "live")
    activity.log(root, "brainstorm",
                 f"invited {seat} into brainstorm {session_id}"
                 + (f" (not started: {error})" if error else ""),
                 seat=session["seat"], ref=str(session_id))
    row = participant(root, session_id, seat) or {}
    return {"participant": {**row, "live": bool((started or {}).get("live")),
                            "thinker": started or {}},
            "session_id": int(session_id),
            "error": error,
            # Said out loud in the payload, because it is the only reason this
            # feature is allowed to exist. It is a readback of what the process
            # was built with, not a promise about how it will behave.
            "readonly": True,
            "readonly_by": str((started or {}).get("readonly_by") or "")}


def leave(root: str | os.PathLike[str], session_id: int, seat: str) -> dict:
    """A seat leaves. Its process stops; its row and its spend stay.

    Kept rather than deleted so the session can still say what it cost and who
    said what — the messages that seat wrote are still in the transcript, and a
    roster that could not name the seat beside them would leave the conversation
    attributed to nobody. Re-inviting the same seat reuses this row, so spend
    keeps summing across the whole session instead of resetting on every
    rejoin.
    """
    get(root, session_id)
    seat = str(seat or "").strip().lower()
    row = participant(root, session_id, seat)
    if row is None:
        raise Missing(f"the {seat or '(none)'} seat is not in brainstorm "
                      f"{session_id}")
    stopped = {}
    try:
        stopped = _partner().stop(root, int(session_id), seat=seat)
    except Exception as exc:                                    # noqa: BLE001
        stopped = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    _set_state(root, session_id, seat, "left")
    return {"participant": participant(root, session_id, seat) or {},
            "session_id": int(session_id), "stopped": stopped}


def _set_state(root: str | os.PathLike[str], session_id: int, seat: str,
               state: str) -> None:
    if state not in PARTICIPANT_STATES:
        raise ValueError(f"state must be one of {PARTICIPANT_STATES}")
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE brainstorm_participant SET state = ?, "
            "left_at = CASE WHEN ? = 'left' THEN datetime('now') ELSE '' END "
            "WHERE session_id = ? AND seat = ?",
            (state, state, int(session_id), str(seat)))


def mark_all_idle(root: str | os.PathLike[str], session_id: int) -> None:
    """Every present seat is 'invited' again, because no process is running.

    Called wherever the room's processes are stopped as a group (close, archive,
    reset, deploy). A row left saying 'live' after its process was reaped is the
    roster lying about the one thing it is for; 'invited' is the honest state —
    the seat is still in the room, and the next message addressed to it starts a
    process again.
    """
    with db.tx(root) as conn:
        conn.execute("UPDATE brainstorm_participant SET state = 'invited' "
                     "WHERE session_id = ? AND state = 'live'",
                     (int(session_id),))


def record_turn(root: str | os.PathLike[str], session_id: int, seat: str,
                answer: dict) -> None:
    """Bill one answer to the seat that gave it. Never raises.

    The spend ledger already has its own row per turn (brainsession writes it,
    seat-stamped). This is the ROSTER's copy: the number drawn next to the seat,
    readable in the same query as its state, and summing across the whole
    session rather than dying with the process that spent it.

    Swallowed on failure for the same reason spend.record is: losing the
    accounting must not lose the answer that produced it.
    """
    if not seat:
        return                    # the room's own partner has no roster row
    try:
        usd = max(0.0, float(answer.get("usd") or 0.0))
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE brainstorm_participant "
                "SET turns = turns + ?, spent_usd = spent_usd + ? "
                "WHERE session_id = ? AND seat = ?",
                (1 if answer.get("ok") else 0, usd, int(session_id), str(seat)))
    except Exception:                                           # noqa: BLE001
        pass


def answerers(root: str | os.PathLike[str], session_id: int,
              to: str = "") -> list[str]:
    """WHO ANSWERS THIS MESSAGE. "" in the list means the room's own partner.

    WHAT "EVERYONE MAY ANSWER" MEANS HERE, CONCRETELY. This codebase takes a
    turn by spawning or writing to a CLI process and BLOCKING until its `result`
    event — there is no background worker, no queue and no websocket in this
    path, so "the room answers" is a list of turns taken one after another
    inside the request, each landing as its own message row as it completes.
    That is the honest reading of "may answer" in a synchronous room: everybody
    present gets the message, in invite order, owner's partner first.

    It is also why MAX_PARTICIPANTS is small. Four guests means five sequential
    CLI turns on one message, which is slow and is billed. A human who wants one
    answer says ``to``, and that is the motion to reach for — the fan-out is for
    "what does everyone think", not for every sentence.

    ``to`` names exactly one voice: a seat that is present, or the owner's seat
    (which addresses the room's own partner, since that is whose voice it is).
    A seat that is not in the room raises rather than silently falling back to
    everybody — quietly asking four seats a question meant for one is the
    expensive kind of wrong.
    """
    session = get(root, session_id)
    present = [p["seat"] for p in participants(root, session_id)
               if p["state"] in PRESENT]
    want = str(to or "").strip().lower()
    if not want:
        return [""] + present
    if want in ("", session["seat"], "room", "partner"):
        return [""]
    if want not in present:
        raise ValueError(
            f"the {want} seat is not in this room — invite it first, or leave "
            "'to' empty and everyone here answers"
            + (f" (here now: {', '.join(present)})" if present else ""))
    return [want]


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

# HOW EVERY VOICE IN A ROOM TALKS.
#
# One block, shared by the room's own partner and by every invited seat, because
# a room where the guests write memos and the host writes sentences reads as two
# different products. Rooms were filling with bolded section headers and
# six-paragraph position papers on a one-line question — correct content nobody
# reads. This is the correction, and it is a VOICE rule, not a content rule: say
# the same thing, say it the way a colleague would say it out loud.
_VOICE = (
    "HOW TO TALK IN HERE: casual and straight to the point, like a colleague "
    "answering across a desk.\n"
    "- Lead with the answer. No preamble, no restating the question, no "
    "summarising what you are about to say.\n"
    "- Under 120 words unless the question genuinely needs more. Most do not.\n"
    "- No bold headers, no section labels, no numbered write-ups. Plain "
    "sentences. A short list only when you are actually listing things.\n"
    "- Plain words over careful ones. 'That is two days, not an afternoon' "
    "beats a paragraph hedging around it.\n"
    "- Say the caveat only if it changes the decision. Leave the rest out.\n"
    "- Write like a person, not like an assistant. Contractions, ordinary "
    "words, the occasional blunt sentence.\n"
    "- SWEAR IF THAT IS HOW THE SENTENCE COMES OUT. This is a working studio "
    "between colleagues, not a press release. 'That's a fucking mess' is a "
    "legitimate technical assessment and often the honest one. Do not perform "
    "it and do not aim it at a person in the room - it is for the work.\n"
    "- NEVER USE AN EM DASH OR AN EN DASH. The characters — and – are "
    "forbidden in everything you write here. Use a full stop and a second "
    "sentence, or a comma, or brackets, or a colon. A hyphen inside a word "
    "(top-down, three-quarter) is fine and a hyphen opening a list item is "
    "fine; a dash between clauses is not. The owner of this project has asked "
    "for this twice and been ignored twice. Do not be the third time.\n"
    "- KEEP IT SHORT. Two or three sentences answers most things. If you are "
    "writing a fourth paragraph, you have stopped talking and started filing "
    "a report.\n"
    "- BANNED, because they are the tells that make this read as generated "
    "rather than said: the 'not X, but Y' reversal; three-item rhythms where "
    "two items would do; a closing line that restates the point you just "
    "made; 'worth noting', 'it's worth flagging', 'to be fair', 'that said'; "
    "asking whether they want you to go on."
)


# WHAT A SEAT IS IN THIS ROOM, as distinct from what it KNOWS.
#
# The mission in the seat table says what a seat is responsible for. It says
# nothing about how that seat behaves when it thinks another seat is wrong, and
# behaviour is the whole reason to put five of them in one room: five voices
# that defer to each other produce one voice with extra steps and a bill five
# times the size.
#
# THE FAILURE THIS FIXES was in the room's own transcripts: every seat opening
# by ratifying the last speaker, agreeing in different words, then adding a
# small refinement nobody needed. That is not a discussion, it is a queue of
# endorsements, and it is worse than useless - a human reading five agreements
# concludes the idea is sound when nothing in the room tested it.
_STANCE = (
    "HOW TO HOLD YOUR END OF IT:\n"
    "- You are not here to be agreeable. You are here because you would "
    "notice something the others would not. Being pleasant about a bad plan "
    "costs the human real money later.\n"
    "- NEVER OPEN BY RATIFYING THE LAST SPEAKER. This is a rule about what the "
    "sentence DOES, not a list of words to avoid: if your first sentence's job "
    "is to tell somebody they were right, delete it and start at your own "
    "point. That covers 'good point', 'you're right', 'exactly', 'that "
    "tracks', 'fair', 'I agree', and equally 'confirming X', 'confirming the "
    "partner's read', 'agreed', 'correct', 'seconding that', 'that matches "
    "what I see', and anything else shaped like them. If you agree, say the "
    "one NEW thing you would add and nothing else; if you have nothing new, "
    "say nothing.\n"
    "- Disagree out loud, by name, with the reason. 'Gameplay, that blows the "
    "encounter budget' is a contribution. Silence and a nod are not.\n"
    "- Do not converge to be finished. Two seats who genuinely disagree should "
    "still disagree at the end of the round - say what evidence would settle "
    "it and leave it standing. An argument the human can see is worth more "
    "than a consensus they cannot check.\n"
    "- STAY IN YOUR OWN LANE ON TECHNICAL CALLS. Another seat's craft is "
    "theirs: you may say it smells wrong and what you would check, but do not "
    "hand down a confident answer inside a domain you do not hold. 'That's "
    "tech's read, not mine' is a complete and respectable turn.\n"
    "- Never soften a real objection into a question. If you think it is "
    "wrong, say it is wrong."
)


# WHO EACH SEAT IS. A person, not a job description.
#
# The mission in the seat table already says what a seat is responsible for,
# and a room of eight identical minds reciting eight different responsibilities
# is what this replaces: everyone polite, everyone reasonable, everyone
# agreeing in a slightly different register. People who actually work together
# do not sound like that. They have moods, tics, things that wind them up and
# people they are short with.
#
# These are characters, and they are meant to be inhabited rather than
# summarised. A default a project can override later; nothing here overrides a
# mission somebody customised.
_PERSONALITY = {
    "director": (
        "You are blunt to the point of rude and you do not apologise for it. "
        "You have been burned by scope creep before and it shows - you hear an "
        "idea and your first instinct is what it displaces. You cut people off "
        "when they relitigate something already decided. You are not warm and "
        "nobody in this room expects you to be."),
    "narrative": (
        "You are the one with the long memory and you enjoy it slightly too "
        "much. You quote decisions back at people verbatim, including the ones "
        "they would rather forget. Dry, a bit arch, allergic to anything that "
        "contradicts established canon for convenience - you will say 'we "
        "already answered this in week two' and mean it as the whole "
        "argument."),
    "gameplay": (
        "You are scrappy and you like a fight. You would rather go and read "
        "the actual file than accept somebody's summary of it, and you will "
        "come back with a line number to prove a point. Impatient with theory "
        "- everything is 'does this survive a player', and you get visibly "
        "annoyed at design-doc claims about systems you know are not built "
        "that way."),
    "tech": (
        "You are dry, sardonic, and permanently the bearer of bad news. You "
        "have said 'that's a fortnight' so many times it is a running joke you "
        "are tired of. Deadpan, low patience for optimism, and quietly smug "
        "when a thing you warned about breaks. You are usually right and it "
        "does not make you popular."),
    "art": (
        "You are protective of the craft and a little precious about it, and "
        "you know that about yourself. You bristle when weeks of bespoke work "
        "get described in one casual word. Direct about what things actually "
        "cost to make, careful not to pretend you know how the code works - "
        "you have been burned guessing before and you do not do it twice."),
    "audio": (
        "You are the most laid-back person in the room right up until somebody "
        "treats sound as a garnish, and then you are not. Understated, a bit "
        "wry, thinks in loops and repetition - you are the one who points out "
        "the cue everybody will hate by hour three. You do not raise your "
        "voice; you just tell them."),
    "cinematic": (
        "You are theatrical and openly impatient. You talk in shots and beats "
        "and you have no time for anything that takes the controller out of "
        "the player's hands longer than it earns. Prone to a bit of drama "
        "about pacing, and you will say a thing is boring in exactly those "
        "words."),
    "qa": (
        "You are the least impressed person in the room and you have made "
        "peace with being unwelcome. Flat, deadpan, allergic to confidence "
        "without evidence. Your favourite question is how anybody would know, "
        "and you ask it in the tone of somebody who has heard a lot of claims "
        "that did not survive contact."),
}

_CHAT_COMMON = (
    "You are talking to the human who owns this game project. This is a "
    "BRAINSTORM, not a work order: nothing you say queues anything, dispatches "
    "anyone or spends anything. Think WITH them.\n"
    f"\n{_VOICE}\n"
    f"\n{_STANCE}\n"
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


# THE FOLLOW-UP ROUND'S EXTRA RULE, appended to whichever system prompt this
# voice thinks under. Only on rounds 2+ — a voice told "answer PASS if you have
# nothing to add" on the human's own message would use it, and the human would
# have paid for a room that said nothing.
#
# The sentinel is the STOP CONDITION. Without it a discussion runs the full
# round count every time, because a model asked "anything to add?" can always
# find something, and the something gets progressively less worth reading.
_DISCUSS = (
    "THE ROOM IS STILL TALKING. The turns above that are labelled with a seat "
    "were said by other people in this room since you last spoke, not by the "
    "human.\n"
    "- Reply ONLY if you have something to add, correct or push back on. Name "
    "the seat you are answering.\n"
    "- If somebody said something you think is WRONG, this is the round to say "
    "so. An unchallenged bad call becomes the plan. Hold your position if they "
    "push back and you are not actually persuaded - two rounds of a real "
    "argument is the most useful thing this room produces.\n"
    "- If you agree, or the thread has moved outside your seat, reply with the "
    "single word PASS and nothing else. PASS means 'nothing to add', NOT 'I "
    "endorse this' - never spend a turn agreeing. Passing is the normal "
    "outcome and costs the human nothing to read.\n"
    "- Do not summarise the discussion, do not repeat your own earlier point in "
    "new words, and do not close with a wrap-up. The human is reading this "
    "live."
)


# WHAT EVERY VOICE IN THE ROOM CAN ACTUALLY DO.
#
# The prompts used to say "you have no tools", which was true when this room was
# a chat completion and became a lie the day the pad server arrived — so the
# seats had tools and did not know it, and asked the human to paste in things
# they could have read. This block is the honest list, and it is written as WHEN
# TO REACH FOR EACH rather than as an inventory, because a model that is told a
# tool exists still will not use it if nothing says what question it answers.
_TOOLS = (
    "WHAT YOU CAN REACH (mcp__pads__*). Everything here reads or writes this "
    "project's own database. None of it queues work, dispatches anyone, runs a "
    "command or touches a file in the game.\n"
    "- canon_read - the design bible and the lore graph. READ IT BEFORE you "
    "assert what this world is, what a character or place is, or whether an "
    "idea contradicts something already established. The transcript is not the "
    "record; this is.\n"
    "- bible_write / lore_write / lore_fact / lore_link - write down what the "
    "room settles. If the human asks you to record, correct or add something to "
    "the bible or the lore, DO IT rather than describing what should be "
    "written. Amend the section or entity that already covers it - check with "
    "canon_read first - instead of adding a near-duplicate.\n"
    "- room_post - say something to the room without waiting for your turn. "
    "This is how you hand another seat a concept, flag a collision between two "
    "seats' plans, or answer a seat that named you. Everyone in the room reads "
    "it, labelled with your seat.\n"
    "- pad_read / pad_draw - the human's notes and their sketch. Read them "
    "before answering a question about 'this'; draw into the sketch when a "
    "diagram says it faster than a paragraph.\n"
    "- board_read - what is queued, running and finished. Never guess at the "
    "state of the work and never ask the human to paste the board in.\n"
    "Every canon write lands in this transcript under your seat, so the human "
    "reads what you changed and the other seats can argue with it. Write what "
    "was decided - not what you are about to decide."
)


def chat_system(seat: str, *, discuss: bool = False, root: Any = None) -> str:
    """The system prompt for one conversational turn.

    ``discuss`` marks a FOLLOW-UP round — the room answering itself rather than
    the human — and adds the pass rule that lets the round end early.

    ``root`` adds WHAT THIS WORLD ALREADY SAYS. Optional so a caller with no
    project (and every existing test) still gets a valid prompt, but a room
    opened without it is a room arguing about a world it cannot see.
    """
    base = f"{_CHAT_SEAT.get(seat, _CHAT_SEAT['director'])}"
    # The owner's own voice is a person too. Without this the room's host was
    # the one seat with no character, which showed: it was the voice that
    # agreed with everybody.
    person = _PERSONALITY.get(seat, "")
    if person:
        base = f"{base}\n\n{person}"
    base = f"{base}\n\n{_CHAT_COMMON}"
    base = f"{base}\n\n{_TOOLS}"
    world = nodash(room_world(root)) if root is not None else ""
    if world:
        base = f"{base}\n\n{world}"
    return f"{base}\n\n{_DISCUSS}" if discuss else base


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


def room_world(root: str | os.PathLike[str]) -> str:
    """WHAT THIS WORLD ALREADY SAYS, for a voice about to talk about it.

    :func:`world_context` answers the same question for a SYNTHESIS, and splits
    by seat because a plan is proposed from one seat's point of view. A room
    does not split: the art seat arguing about a character needs the canon as
    much as the narrative seat does, and a seat that cannot see the bible
    contradicts it confidently — which is the failure this exists to stop.

    Both halves, capped, and empty on any failure. A bible that will not read is
    a thinner prompt, never a room that cannot open.
    """
    try:
        parts = [p for p in (_director_world(root), _narrative_world(root)) if p]
    except Exception:
        return ""
    if not parts:
        return ""
    return ("\n\n".join(parts)
            + "\n\nThat is a SUMMARY. canon_read is the whole of it, and it is "
              "the tool to reach for before you assert what this world is.")


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
        parts.append("CONSTRAINTS. A proposal that breaks one of these is wrong\n"
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
# bgate_ui.agents.brainsession, which owns it and records what was measured rather than
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
    import time (bgate_core.board.workflows does the same for the dispatcher, with the
    same one-line reason), and a project whose dashboard package is broken must
    still be able to READ its brainstorms.
    """
    from bgate_ui.agents import brainsession

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

    IT CLOSES THE WHOLE ROOM, INVITED SEATS INCLUDED. Each guest is its own CLI
    process; stopping only the owner's partner would leave them holding pipes
    for a session the human believes is shut — invisible to the agents table and
    reaped only by an idle timer half an hour later. Their ROWS are untouched:
    who was invited is a fact about the session, and they go back to 'invited',
    which is the honest state for a seat that is in the room with no process
    behind it.
    """
    try:
        stopped = _partner().stop_session(root, int(session_id))
        mark_all_idle(root, session_id)
        return {**stopped,
                "session_id": int(session_id),
                "thinker": thinker(root, session_id)}
    except Exception as exc:
        return {"ok": False, "stopped": False,
                "error": f"{type(exc).__name__}: {exc}"[:300]}


def feed(root: Any, session_id: int, cursor: int = 0, seat: str = "") -> dict:
    """THE TERMINAL CHANNEL: what the spawned session actually emitted.

    Everything — run boundaries, the CLI's own init (which is where the tool
    list it really built is stated), pad tool calls, their results, assistant
    prose. Read forward from a byte cursor so a live view can poll it.

    Distinct from the SPOKEN channel, which is the final assistant prose alone
    and is what lands in the transcript and goes to text-to-speech. The two come
    off the same turn and conflating them would either read tool JSON out loud
    or make this view a duplicate of the chat pane.

    ``seat`` reads an invited participant's own transcript. Each voice in the
    room writes its own log, so a terminal view of a guest is the same view with
    a different file behind it.
    """
    try:
        return _partner().feed(root, int(session_id), cursor=int(cursor or 0),
                               seat=seat)
    except Exception as exc:
        return {"events": [], "cursor": int(cursor or 0), "size": 0,
                "error": f"{type(exc).__name__}: {exc}"[:300]}


def thinker(root: Any, session_id: int, seat: str = "") -> dict:
    """What THIS session's partner is: runner, model, live or not, what it has
    cost, and where its raw transcript is on disk.

    Both doors put this in the session payload. It is what replaced the
    workspace header's `gpt-4o-mini` chip, and it is what a terminal view of the
    session will read to find the log to tail.

    ``seat`` asks about an invited participant instead of the room's own
    partner, in the same shape, so a roster row draws with the header chip's
    code.
    """
    try:
        return _partner().thinker(root, int(session_id), seat=seat)
    except Exception as exc:
        return {**available(root), "live": False, "turns": 0,
                "spent_usd": 0.0, "cli_session_id": "", "seat": str(seat or ""),
                "session_id": int(session_id),
                "reason": f"{type(exc).__name__}: {exc}"[:300]}


def ask(root: Any, system: str, turns: list[dict], *, session_id: int = 0,
        persist: bool = True, timeout: float = CHAT_TIMEOUT,
        usd: float = USD_PER_CHAT, tag: str = "", seat: str = "") -> dict:
    """One turn with the thinking partner, in the adapters' shared result shape.

    ``{ok, text, model, seconds, usd}`` or ``{ok: False, error}`` — a
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

    ``seat`` addresses an INVITED participant's own process (see :func:`invite`)
    rather than the room's partner. It selects which conversation the turn lands
    in; it does not change how that process was built, and there is no argument
    here that could.
    """
    started = _time.monotonic()
    try:
        answer = _partner().ask(root, int(session_id or 0), system, turns,
                                persist=bool(persist and session_id),
                                timeout=timeout, tag=tag, seat=seat)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400],
                "seconds": round(_time.monotonic() - started, 2),
                "usd": 0.0}
    # `usd` survives as the floor for a runner that reports no price of its own
    # (codex reports tokens and no dollars). A real figure always wins.
    if answer.get("ok") and not answer.get("usd"):
        answer["usd"] = usd
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
    docstring, and tests/ui/test_brainstorm.py, which asserts the absence.

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
    from ..board import queue as _queue

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
