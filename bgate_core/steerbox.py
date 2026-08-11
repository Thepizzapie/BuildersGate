"""The steer inbox — a message channel to a running agent that crosses processes.

Steering works by writing a user turn into a live claude session's stdin, and
that pipe exists only inside the dashboard process that spawned it
(``bgate_ui.dispatch._live``). Anything else that wants to steer — the MCP
server, a CLI command, and above all the DIRECTOR agent, which runs as its own
claude process — has no way to reach it.

So the message is written to disk instead, and the dashboard delivers it. One
small file per message under ``.bgate/steer/``, drained by a pump thread in the
server. The consequences of that shape are the point:

  * The director can steer its own workers. It is the seat that decides who does
    what; being unable to say "not like that, use the pinned ref" while an agent
    is mid-run made it a dispatcher, not a director.
  * A message written while no dashboard is running is not lost — it waits.
  * A message for an item with no live agent is not silently dropped either: the
    pump answers it (see bgate_ui.steerpump) rather than deleting it blind.

Deliberately files rather than a table: the writer may be a process that only
has the project path, the payload is tiny, and a crashed reader must leave the
message where it was rather than half-consumed inside a transaction.

The second half of this module is the OTHER direction — a question from an agent
to the human (``ask_human``) and the answer coming back. It lives here because
the return path for a live asker is this same inbox, and because both the MCP
server and the dashboard have to reach it. See the section header below.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import db, events

MAX_TEXT = 2000
# A message nobody has collected is stale rather than pending: an agent that
# finished twenty minutes ago is not going to read it, and delivering it to the
# NEXT run of the same item would steer a fresh agent with an old complaint.
STALE_S = 15 * 60


def box(root: str | os.PathLike[str]) -> Path:
    return Path(root) / ".bgate" / "steer"


def post(root: str | os.PathLike[str], item_id: int, text: str, *,
         by: str = "", note: str = "") -> dict:
    """Leave a message for whoever is running ``item_id``."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("a steer needs something to say")
    if len(text) > MAX_TEXT:
        raise ValueError(f"a steer is capped at {MAX_TEXT} characters")
    directory = box(root)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": uuid.uuid4().hex[:12],
        "item_id": int(item_id),
        "text": text,
        "by": by or "",
        "note": note or "",
        "at": time.time(),
    }
    # Write beside, then rename: a reader must never see half a message.
    tmp = directory / f".{payload['id']}.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(directory / f"{int(payload['at'] * 1000)}-{payload['id']}.json")
    return payload


# How much of a long correction goes in the steer itself. The rest is cited.
# Sized so the pointer sentence and the excerpt together stay well inside
# MAX_TEXT with room for a long project path.
EXCERPT = 900


def notes_dir(root: str | os.PathLike[str]) -> Path:
    return box(root) / "notes"


def post_long(root: str | os.PathLike[str], item_id: int, text: str, *,
              by: str = "", note: str = "") -> dict:
    """Steer a running agent with a correction too long to be a steer.

    THE CAP IS RIGHT AND THE DEAD END WAS NOT. Refusing a 2000+ character steer
    outright — rather than truncating it, which would hand an agent half a
    sentence and no way to know — is the correct call and it stays. But it left
    no route at all for a genuinely long correction to reach a RUNNING agent:
    the only options were to kill the run and re-pay for it, or to let it carry
    on doing the wrong thing.

    So the long text becomes a FILE and the steer becomes a citation. This is the
    same discipline ``ask_human`` already asks of its callers — cite, do not
    paste — applied to the channel that could not.

    What the agent receives stays an interruption: a head excerpt so it can tell
    immediately whether to stop what it is doing, and the path to read for the
    rest. What it does NOT do is change the brief. A correction that should
    outlive the run belongs in ``queue.update``; this one dies with the process
    it was aimed at, which is the honest lifetime for "no, not like that".

    Returns the posted steer plus ``note_path`` (absolute) and ``excerpted``.
    """
    text = str(text or "").strip()
    if not text:
        raise ValueError("a steer needs something to say")
    if len(text) <= MAX_TEXT:
        return {**post(root, item_id, text, by=by, note=note),
                "note_path": "", "excerpted": False}

    directory = notes_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = directory / f"item-{int(item_id)}-{stamp}-{uuid.uuid4().hex[:6]}.md"
    header = (f"# Steer for item #{int(item_id)}\n\n"
              f"From: {by or 'unknown'}  \nAt: {stamp} UTC\n\n"
              "This is the full text of a mid-run correction. The agent was "
              "given the first paragraph and this path.\n\n---\n\n")
    path.write_text(header + text, encoding="utf-8")

    head = text[:EXCERPT].rstrip()
    posted = post(root, item_id, (
        f"MID-RUN CORRECTION — too long for one steer, so the full text is at "
        f"{path}. READ THAT FILE before your next step; what follows is only "
        f"its opening so you can judge whether to stop now.\n\n{head}\n\n"
        f"[...{len(text) - len(head)} more characters in {path.name}]"
    ), by=by, note=note)
    return {**posted, "note_path": str(path), "excerpted": True}


def pending(root: str | os.PathLike[str],
            item_id: int | None = None) -> list[dict]:
    """Undelivered messages, oldest first. Reads only — nothing is consumed."""
    directory = box(root)
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if item_id is not None and int(data.get("item_id", 0)) != int(item_id):
            continue
        data["_path"] = str(path)
        out.append(data)
    return out


def take(root: str | os.PathLike[str]) -> list[dict]:
    """Claim every pending message. Stale ones are dropped, not delivered."""
    now = time.time()
    claimed: list[dict] = []
    for data in pending(root):
        path = Path(data.pop("_path"))
        try:
            path.unlink()
        except OSError:
            continue  # somebody else got it first
        if now - float(data.get("at") or 0) > STALE_S:
            data["stale"] = True
        claimed.append(data)
    return claimed


# ---------------------------------------------------------------------------
# Questions to the human — ask_human, and the three places an answer can land
# ---------------------------------------------------------------------------
# A QUESTION IS AN EVENT, NOT A WORK ITEM. The tempting shape is a queued row for
# the director's question, and it is wrong in a way that defeats the feature: a
# row is a thing somebody has to dispatch in order to read, so "ask the human"
# becomes "spawn an agent to ask the human" — paid, laned, and still not in front
# of anybody. The event log already reaches the bell, the drawer, the webhook and
# the desktop title, and a question that nobody answers costs nothing.
#
# THE ANSWER PATH DEPENDS ON WHETHER THE ASKER IS STILL ALIVE, which is the whole
# reason this is not one write:
#
#   * still running  -> the steer inbox above, the path a mid-run correction
#     already takes. The agent reads it when its current step ends.
#   * finished       -> a handoff `decision` note, so the next session picks it up
#     from one file read, plus the answer on the question event itself so the
#     drawer and the director debrief see it too. Writing it to the item's brief
#     (the obvious move) is writing to nobody: that item is done.
#   * still open past the window -> ONE reminder. See :func:`remind_stale`.
#
# In every case the answer is attached to the question event FIRST and delivered
# second: a delivery that fails must leave a recorded answer behind, because the
# human is not going to type it again.
QUESTION_KIND = "director.question"
# The reminder rides the existing stalled vocabulary rather than inventing a kind
# nothing filters on — a settings checkbox can only offer kinds events.KINDS lists.
REMINDER_KIND = "chain.stalled"

# One question, not a briefing. The cap is what stops ask_human from becoming a
# second brief channel that bypasses the board.
MAX_QUESTION = 1200
# An answer must fit the steer channel, since that is where a live asker's copy
# goes; a longer answer would be accepted here and refused there.
MAX_ANSWER = MAX_TEXT
MAX_REFS = 12
OPEN_LIMIT = 20
# How far back a scan for open questions goes. Bounded because the console polls
# this every few seconds, and a project that has answered a thousand questions
# must not pay for all of them to draw the two that are open.
# How many UNANSWERED question events are examined. The query already excludes
# answered ones, so this is a cap on how many open questions a project may have
# before the oldest stop being surfaced — and an unanswered question that falls
# out of the scan is invisible to pending_decisions, the console AND the stale
# reminder at once. 200 was reachable on a busy week; a question nobody can see
# is worse than a slower query, and the rows are small.
QUESTION_SCAN = 2000


class AlreadyAnswered(ValueError):
    """A question that already carries an answer was answered again.

    Refused rather than overwritten: the first answer has already been steered
    into a running agent or written to the handoff thread, and a silent second
    one would contradict a message that is already gone. The caller shows the
    existing answer instead — the UI turns this into a 409.
    """

    def __init__(self, seq: int, existing: str):
        super().__init__(
            f"question {seq} was already answered ({existing[:120]!r}) — that "
            "answer has already been delivered, so a second one would "
            "contradict it silently. Ask again if you have changed your mind.")
        self.seq = int(seq)
        self.existing = existing


def _payload(text: str) -> dict:
    """Stored event JSON -> dict, always. A corrupt row reads as an empty
    payload rather than taking the console's whole state response down."""
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _shape(row) -> dict:
    """One event row -> the question shape the console and the debrief read.

    Fixed keys, present even when empty: a card that has to test for the
    existence of `answer` before rendering is a card that renders differently on
    two projects.
    """
    payload = _payload(row["payload"])
    return {
        "event_seq": int(row["id"]),
        "item_id": int(payload.get("item_id") or 0),
        "seat": str(payload.get("seat") or ""),
        "question": str(payload.get("question") or ""),
        "asked_at": str(row["created_at"] or ""),
        "refs": [str(r) for r in (payload.get("refs") or [])][:MAX_REFS],
        "asked_by": str(payload.get("asked_by") or row["actor"] or ""),
        "answer": str(payload.get("answer") or ""),
        "answered_at": str(payload.get("answered_at") or ""),
        "answered_by": str(payload.get("answered_by") or ""),
        "route": str(payload.get("route") or ""),
        "delivered": bool(payload.get("delivered") or False),
        "reminded_at": str(payload.get("reminded_at") or ""),
    }


def _row(root: str | os.PathLike[str], seq: int):
    conn = db.connect(root)
    return conn.execute(
        "SELECT id, kind, ref, actor, payload, created_at FROM event "
        "WHERE id = ?", (int(seq),)).fetchone()


def _rewrite(root: str | os.PathLike[str], seq: int, payload: dict) -> bool:
    """Replace a question event's payload. True if the row was there.

    The event log is append-only for everything else, and this is the one
    deliberate exception: the answer belongs ON the question, so a consumer that
    finds the question — the drawer, the next debrief — finds the answer with it
    instead of having to correlate two rows by id.
    """
    try:
        with db.tx(root) as conn:
            cur = conn.execute(
                "UPDATE event SET payload = ? WHERE id = ? AND kind = ?",
                (_encode(payload), int(seq), QUESTION_KIND))
            return bool(cur.rowcount)
    except Exception:
        return False


def _encode(payload: dict) -> str:
    """Payload -> JSON that fits the column, WITHOUT slicing the JSON.

    A question at its cap plus an answer at its cap is over
    ``events.MAX_PAYLOAD``, and truncating the text of a JSON document produces a
    row that cannot be parsed back — every reader of this question would then see
    an empty payload, which is the answer being lost rather than shortened. So
    the shed happens field by field, newest information last: refs go, then the
    copy of the question, and the answer is what survives.
    """
    cap = getattr(events, "MAX_PAYLOAD", 4000)
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= cap:
        return text
    lean = dict(payload)
    lean["refs"] = []
    lean["_shed"] = "refs"
    text = json.dumps(lean, ensure_ascii=False, default=str)
    if len(text) <= cap:
        return text
    lean["question"] = str(lean.get("question") or "")[:300]
    lean["_shed"] = "refs,question"
    text = json.dumps(lean, ensure_ascii=False, default=str)
    if len(text) <= cap:
        return text
    lean["answer"] = str(lean.get("answer") or "")[:cap // 2]
    lean["_shed"] = "refs,question,answer"
    return json.dumps(lean, ensure_ascii=False, default=str)


def ask(root: str | os.PathLike[str], question: str,
        refs: Optional[list] = None, *, item_id: int = 0, seat: str = "",
        by: str = "") -> dict:
    """Record one question for the human. Returns the event id to answer it by.

    Raises when the question is empty, over :data:`MAX_QUESTION`, or could not be
    recorded at all — an ``ask_human`` that silently wrote nothing leaves an agent
    believing it has asked, which is worse than an error it can read and repeat.

    ``item_id`` is the asker's work item when it has one; it is what decides
    later whether the answer can be steered or has to be written down.
    """
    text = str(question or "").strip()
    if not text:
        raise ValueError("a question with no text cannot be answered")
    if len(text) > MAX_QUESTION:
        raise ValueError(
            f"a question is capped at {MAX_QUESTION} characters — ask one thing; "
            "a briefing belongs in a work item's brief")
    # 160 rather than handoff's 200 so that a question at its own cap plus a full
    # dozen refs still fits events.MAX_PAYLOAD — over that, emit() replaces the
    # WHOLE payload with a truncation marker and the question text is gone.
    clean = [str(r)[:160] for r in (refs or []) if str(r).strip()][:MAX_REFS]
    item = max(0, int(item_id or 0))
    payload = {"question": text, "refs": clean, "item_id": item,
               "seat": str(seat or "")[:40], "asked_by": str(by or "")[:80]}
    seq = events.emit(root, QUESTION_KIND, ref=str(item or ""), payload=payload)
    if not seq:
        # events.emit swallows its own failures by design (see its docstring), so
        # 0 is the only signal that nothing was written — and here it matters:
        # nothing else in this flow would ever show the question.
        raise RuntimeError(
            "the question could not be recorded (the event log refused the "
            "write) — nothing would ever show it to the human, so it was not "
            "asked. Retry, or say it in your result note instead.")
    return {"ok": True, "seq": int(seq), "question": text, "refs": clean,
            "item_id": item, "seat": str(seat or "")}


def question(root: str | os.PathLike[str], seq: int) -> Optional[dict]:
    """One question by its event id, answer and all. None if there is no such
    question — needed because an answer arrives from a page that may have been
    open across a prune."""
    try:
        row = _row(root, seq)
    except Exception:
        return None
    if row is None or row["kind"] != QUESTION_KIND:
        return None
    return _shape(row)


def open_questions(root: str | os.PathLike[str],
                   limit: int = OPEN_LIMIT) -> list[dict]:
    """Unanswered questions, oldest first. Never raises.

    Oldest first because the oldest unanswered question is the one holding
    something up. Read by a direct query rather than off a notification cursor: a
    question stays open until somebody answers it, and a consumer whose cursor
    has already moved past the event would never show it again.
    """
    cap = max(1, min(int(limit or OPEN_LIMIT), OPEN_LIMIT * 5))
    try:
        conn = db.connect(root)
        found = conn.execute(
            # The LIKE is a cheap prefilter, not the test: json1 is not
            # guaranteed to be compiled into the sqlite3 a user's Python ships
            # with, and the authoritative check is the parsed payload below.
            "SELECT id, kind, ref, actor, payload, created_at FROM event "
            "WHERE kind = ? AND payload NOT LIKE '%\"answer\":%' "
            "ORDER BY id DESC LIMIT ?", (QUESTION_KIND, QUESTION_SCAN)).fetchall()
    except Exception:
        # A missing event table (a database from before migration 0016) reads as
        # "no open questions"; the console must still paint.
        return []
    out: list[dict] = []
    for row in found:
        shaped = _shape(row)
        if shaped["answer"]:
            continue
        out.append(shaped)
    out.reverse()
    # Ordered oldest-first, but the CAP keeps the newest: a card list that cannot
    # show the question just asked is the worse failure, because that is the one
    # the human is about to have an opinion about. An old question crowded out
    # this way is not forgotten — remind_stale scans past the cap and pings once.
    return out[-cap:]


def _asker_is_running(root: str | os.PathLike[str], item_id: int) -> bool:
    """Can the asker still be steered? ``dispatched`` is the whole test.

    The same test :func:`bgate_mcp.server.agent_steer` uses, and for the same
    reason: whether a stdin pipe is actually open is known only inside the
    dashboard process, and a core module reaching into ``bgate_ui`` to ask would
    invert the layering for an answer it cannot trust across processes anyway. An
    item that reads dispatched but has already exited gets its answer answered by
    the steer pump in the activity ledger instead of vanishing.
    """
    if int(item_id or 0) <= 0:
        return False
    try:
        row = db.connect(root).execute(
            "SELECT status FROM work_item WHERE id = ?",
            (int(item_id),)).fetchone()
    except Exception:
        return False
    return bool(row) and row["status"] == "dispatched"


def _decision_text(seq: int, asked: str, answer: str) -> str:
    return (f"Human answer to a director question (event {seq}).\n"
            f"Q: {asked}\nA: {answer}")


def answer(root: str | os.PathLike[str], seq: int, text: str, *,
           by: str = "") -> dict:
    """Answer a question and route it to whoever is left to read it.

    Returns ``{ok, seq, route, item_id, delivered, delivery, delivery_error,
    question, answer}`` — ``route`` is ``steer`` when the asker is still running
    and ``handoff`` when it is not, and ``delivered`` says whether that landed.

    Raises :class:`AlreadyAnswered` on a second answer, ``LookupError`` when
    there is no such question, and ``ValueError`` on empty or oversized text.

    WITHOUT THE ROUTING this is a text box that writes to nowhere: an answer put
    on a finished item's brief is read by no one, and a handoff note to an agent
    that is still mid-run arrives after it has already guessed.
    """
    body = str(text or "").strip()
    if not body:
        raise ValueError("an empty answer and an unanswered question are the "
                         "same thing to whoever is waiting on it")
    if len(body) > MAX_ANSWER:
        raise ValueError(f"an answer is capped at {MAX_ANSWER} characters — it "
                         "has to fit the channel that carries it to a live agent")
    try:
        row = _row(root, seq)
    except Exception as exc:
        raise LookupError(f"the event log could not be read: {exc}") from None
    if row is None or row["kind"] != QUESTION_KIND:
        raise LookupError(
            f"no question at event {seq} — it may have been pruned, or the page "
            "asking is older than the log")
    shaped = _shape(row)
    if shaped["answer"]:
        raise AlreadyAnswered(int(seq), shaped["answer"])

    item_id = shaped["item_id"]
    route = "steer" if _asker_is_running(root, item_id) else "handoff"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    payload = dict(_payload(row["payload"]))
    payload.update({"answer": body, "answered_by": str(by or "")[:80],
                    "answered_at": stamp, "route": route, "delivered": False})
    # Durable first, delivery second. A human types an answer once.
    _rewrite(root, int(seq), payload)

    delivered, delivery, failure = False, "", ""
    if route == "steer":
        try:
            post(root, item_id, body, by=by or "human",
                 note=f"answer to question {seq}")
            delivered = True
            delivery = ("left in the steer inbox; the agent reads it when its "
                        "current step ends")
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            route = "handoff"  # a steer that could not be written is not an answer
    if route == "handoff":
        try:
            from . import handoff as _handoff
            _handoff.note(root, "decision",
                          _decision_text(int(seq), shaped["question"], body),
                          refs=(shaped["refs"] +
                                ([f"item {item_id}"] if item_id else []) +
                                [f"event {seq}"]),
                          actor=str(by or "human"))
            delivered = True
            delivery = ("filed as a handoff decision note — the next session "
                        "reads it, and so does the next debrief")
        except Exception as exc:
            failure = (failure + "; " if failure else "") + \
                f"{type(exc).__name__}: {exc}"
            delivery = ("recorded on the question only — the handoff thread "
                        "could not be written")

    # Best-effort second write: the answer is already stored, so failing here
    # costs an accurate route/delivered flag and nothing more.
    payload.update({"route": route, "delivered": delivered})
    _rewrite(root, int(seq), payload)
    return {"ok": True, "seq": int(seq), "route": route, "item_id": item_id,
            "delivered": delivered, "delivery": delivery,
            "delivery_error": failure, "question": shaped["question"],
            "answer": body}


def stale_questions(root: str | os.PathLike[str],
                    hours: float = 12.0) -> list[dict]:
    """Open questions older than ``hours`` that have not had their reminder yet.

    Time-based, because the failure here is an ABSENCE of transitions: a question
    nobody answers emits nothing on its own, so without this the new routing
    grows the same quiet failure mode it was built to fix.
    """
    window = max(0.25, float(hours or 12.0))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window)
              ).strftime("%Y-%m-%d %H:%M:%S")
    return [q for q in open_questions(root, limit=OPEN_LIMIT * 5)
            if q["asked_at"] and q["asked_at"] < cutoff and not q["reminded_at"]]


def _mark_reminded(root: str | os.PathLike[str], seq: int) -> bool:
    """Stamp a question as reminded. False if it already was, or on failure."""
    try:
        row = _row(root, seq)
    except Exception:
        return False
    if row is None or row["kind"] != QUESTION_KIND:
        return False
    payload = dict(_payload(row["payload"]))
    if payload.get("reminded_at"):
        return False
    payload["reminded_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")
    return _rewrite(root, int(seq), payload)


def remind_stale(root: str | os.PathLike[str],
                 hours: Optional[float] = None) -> list[dict]:
    """Emit ONE reminder per unanswered question past the window. Never raises.

    Returns the questions reminded about, which is usually empty — safe to call
    on a tick. The reminder is a `chain.stalled` event naming the question, NOT a
    repeat of the question itself: a ping that re-asks is the thing people mute,
    and after that the second reminder is the only one that would have mattered.

    Idempotency is a stamp on the question, written BEFORE the event is emitted.
    That order is deliberate: a crash between the two costs the reminder, and
    losing one reminder is the cheaper failure than firing it on every tick
    forever.
    """
    if hours is None:
        try:
            from . import settings as _settings
            hours = float(_settings.get(root, "notify.question_stale_h") or 12.0)
        except Exception:
            hours = 12.0
    out: list[dict] = []
    try:
        due = stale_questions(root, hours)
    except Exception:
        return out
    for q in due:
        if not _mark_reminded(root, q["event_seq"]):
            continue
        events.emit(root, REMINDER_KIND,
                    ref=str(q["item_id"] or q["event_seq"]),
                    payload={"reason": "question unanswered",
                             "question_seq": q["event_seq"],
                             "question": q["question"][:400],
                             "item_id": q["item_id"], "seat": q["seat"],
                             "asked_at": q["asked_at"],
                             "hours": round(float(hours), 2)})
        out.append(q)
    return out
