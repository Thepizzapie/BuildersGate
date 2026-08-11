"""The brainstorm room: chat, a writing pad, a drawing pad, and one Deploy.

The cheap sibling of ``routes/console.py``. Read that module first — this one is
defined by what it deliberately does NOT do.

    console.say        -> creates a work item, dispatches a Claude Code session
                          holding the whole MCP tool set, lands on the board.
    brainstorm.message -> writes a row, asks a Claude Code session that has NO
                          tool set at all, writes another row.

WHY A MESSAGE CANNOT DISPATCH, STRUCTURALLY.

A guarantee that rests on nobody adding the wrong line later is not a
guarantee, so this is arranged in three ways that all have to be undone
deliberately:

1. The reply comes from a session spawned WITHOUT THE CAPABILITY. Both rooms
   now run on the same CLI, which is the point — a thinking partner should be
   as good as the agents — so this one is started with an empty built-in tool
   set (``--tools ""``), no MCP server and ``--strict-mcp-config`` so it cannot
   inherit the one registered on the machine, no settings sources to add any of
   it back, and plan mode behind all of that. Not permitted-then-restrained:
   there is no Write, no Bash and no ``queue_add`` in the process. Compare a
   dispatched agent, which holds ``queue_add`` from its first token.
2. The argv is built by ``bgate_ui.runners``, from a table entry that must
   DECLARE a read-only conversational mode before the room will use the runner
   at all. A runner with no such entry is refused rather than run with the
   dispatch flags, which is how "expand later for codex, and local llms" stays
   one row of work instead of one way to lose the guarantee.
3. This module never imports ``bgate_core.queue``. Filing lives in
   ``brainstorm.file_plan``, which :func:`deploy` calls and nothing else here
   does. tests/test_brainstorm.py asserts the absence, which turns "we agreed
   not to" into something CI can check.

WHY THE PARTNER IS NOT DEFINED HERE ANY MORE. There is a second door — the MCP
tools in ``bgate_mcp.server`` — and the parts a copy would drift on (the
session, the synthesis turns, the whole of filing) live in
``bgate_core.brainstorm`` so that both doors run the same code. The names below
are aliases of those, kept so this module reads as it did and so a test can
still stub ``_ask`` here without reaching into the core.

DEPLOY IS TWO CALLS, AND THE FIRST ONE WRITES NOTHING.

    POST .../synthesize   the model reads the session and PROPOSES a plan.
                          Nothing is queued, nothing is stored, the session's
                          status does not move. It is a preview.
    POST .../deploy       takes the plan the human confirmed — sent back
                          verbatim, or edited by them — and files exactly that.

Deploy never re-synthesizes. If it asked the model again, the plan filed would
be a plan nobody read, and the confirmation step would be theatre.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from bgate_core import brainstorm as _bs
from bgate_ui import api
from bgate_ui.deps import root, safe_under

router = APIRouter()

# The shared implementation, under this module's old names. Aliases rather than
# re-exports with a wrapper: `_ask` has to stay a module GLOBAL that the
# endpoints below look up by name, because that is what lets the suite stub the
# model out per test without patching the core for every other caller.
CHAT_TIMEOUT = _bs.CHAT_TIMEOUT
SYNTH_TIMEOUT = _bs.SYNTH_TIMEOUT
USD_PER_CHAT = _bs.USD_PER_CHAT
USD_PER_SYNTH = _bs.USD_PER_SYNTH
_available = _bs.available
_thinker = _bs.thinker
_ask = _bs.ask
_close = _bs.close_partner
_feed = _bs.feed


def _session(project, session_id: int) -> dict:
    try:
        return _bs.read(project, session_id)
    except _bs.Missing as exc:
        raise api.not_found(str(exc), session_id=session_id)


def _writable(session: dict) -> None:
    """An archived session is a record, not a workspace."""
    try:
        _bs.ensure_open(session)
    except _bs.Archived as exc:
        raise api.conflict(str(exc), session_id=session["id"],
                           status=session["status"])


# ---------------------------------------------------------------------------
# The file drawer
# ---------------------------------------------------------------------------

@router.get("/api/brainstorm")
def list_sessions(seat: Optional[str] = None, status: Optional[str] = None,
                  limit: int = 50) -> dict:
    """The index — titles and counts, never the notes or the drawing."""
    project = root()
    if seat and seat not in _bs.SEATS:
        raise api.bad_request(f"seat must be one of {_bs.SEATS}", seat=seat)
    if status and status not in _bs.STATUSES:
        raise api.bad_request(f"status must be one of {_bs.STATUSES}",
                              status=status)
    return api.ok({
        "sessions": _bs.list_sessions(project, seat=seat, status=status,
                                      limit=min(int(limit or 50), 200)),
        "seats": list(_bs.SEATS),
        "model": _available(project),
    })


@router.post("/api/brainstorm")
def create_session(payload: Optional[dict] = None) -> dict:
    body = payload or {}
    try:
        return api.ok(_bs.create(root(), seat=str(body.get("seat") or "director"),
                                 title=str(body.get("title") or "")))
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.get("/api/brainstorm/{session_id:int}")
def read_session(session_id: int) -> dict:
    """One session, whole: messages, notes pad, drawing scene, deploy record.

    Plus ``thinker`` — which runner and model this session's partner is, whether
    a process is live right now, what the conversation has cost, and the path to
    its raw transcript. That is what the workspace header chip reads (it used to
    be hardcoded to a model name this room no longer talks to) and what a
    terminal view of the session will tail.
    """
    project = root()
    return api.ok({**_session(project, session_id),
                   "thinker": _thinker(project, session_id)})


@router.patch("/api/brainstorm/{session_id:int}")
def update_session(session_id: int, payload: dict) -> dict:
    """Rename, or replace the writing pad or the drawing pad.

    ``drawing`` is the pad's structured scene — ``{"elements": [...],
    "appState": {...}}`` — and this is the endpoint anything writes elements
    back through, whether that is the human's pad or a model asked to add a box.
    ``drawing_png`` is a project-relative path to a flattened render kept for
    previews; it is stored, never read for meaning.
    """
    project = root()
    session = _session(project, session_id)
    _writable(session)
    touched = []
    try:
        if payload.get("title") is not None:
            _bs.rename(project, session_id, str(payload["title"]))
            touched.append("title")
        if payload.get("notes") is not None:
            _bs.set_notes(project, session_id, str(payload["notes"]))
            touched.append("notes")
        if "drawing" in payload or "drawing_png" in payload:
            png = payload.get("drawing_png")
            if png is not None:
                # Refuse a path that leaves the project before it is stored, not
                # when something later tries to serve it.
                safe_under(project, str(png))
            _bs.set_drawing(project, session_id, payload.get("drawing"),
                            png=None if png is None else str(png))
            touched.append("drawing")
    except ValueError as exc:
        raise api.bad_request(str(exc), session_id=session_id)
    if not touched:
        raise api.bad_request("nothing to change — send title, notes, drawing "
                              "or drawing_png")
    return api.ok({**_session(project, session_id), "changed": touched})


@router.post("/api/brainstorm/{session_id:int}/archive")
def archive_session(session_id: int, payload: Optional[dict] = None) -> dict:
    """File it away (``{"archived": true}``) or take it back out.

    Nothing is deleted either way, and un-archiving restores the status the
    session earned — a session that filed work stays 'deployed'.
    """
    project = root()
    _session(project, session_id)
    want = (payload or {}).get("archived")
    return api.ok(_bs.archive(project, session_id,
                              archived=True if want is None else bool(want)))


@router.post("/api/brainstorm/{session_id:int}/close")
def close_session(session_id: int) -> dict:
    """END THE RUNNING PARTNER. Keeps the conversation, the notes and the drawing.

    The button behind "I want to be able to confidently close a session". Before
    this the process ended only in ways nobody could see — a 30-minute idle
    reap, an LRU eviction, or the kill switch — so a human who wanted it off had
    no way to make it off and no way to know.

    NOT archive and NOT deployed; see ``brainstorm.close_partner`` for the three
    words side by side. This one is about the process: closed is off, and saying
    something else brings it back where it left off.
    """
    project = root()
    _session(project, session_id)
    return api.ok(_close(project, session_id))


@router.get("/api/brainstorm/{session_id:int}/feed")
def session_feed(session_id: int, cursor: int = 0) -> dict:
    """THE TERMINAL CHANNEL — what the spawned session actually emitted.

    Polled from a byte cursor by the transcript view. Deliberately separate from
    the chat pane: this carries tool calls, their results and run boundaries,
    where the chat pane carries the conversation. Reading this one out loud is
    what the spoken channel must never do.
    """
    project = root()
    _session(project, session_id)
    return api.ok(_feed(project, session_id, cursor=int(cursor or 0)))


@router.delete("/api/brainstorm/{session_id:int}")
def delete_session(session_id: int) -> dict:
    """Really delete the conversation. Work items already filed from it stay on
    the board — they are somebody's job, and they outlive the room they were
    thought up in."""
    project = root()
    _session(project, session_id)
    return api.ok(_bs.delete(project, session_id))


# ---------------------------------------------------------------------------
# The conversation
# ---------------------------------------------------------------------------

@router.post("/api/brainstorm/{session_id:int}/message")
def message(session_id: int, payload: dict) -> dict:
    """Say something, and get an answer. NOTHING ELSE HAPPENS.

    No work item, no agent, no gate — see the module docstring for why that is
    a property of the mechanism rather than a promise about this function.

    The human's message is stored BEFORE the model is called and stays stored if
    the call fails. Losing what somebody typed because a key was missing or a
    request timed out is the worst outcome available here, and it is the one a
    naive "ask, then save both" ordering produces.
    """
    project = root()
    session = _session(project, session_id)
    _writable(session)
    try:
        said = _bs.append_message(project, session_id, "user",
                                  str(payload.get("text") or ""))
    except ValueError as exc:
        raise api.bad_request(str(exc), session_id=session_id)

    # session_id is what makes this a CONVERSATION rather than forty unrelated
    # questions: the spawned session is held open between messages and this turn
    # is its next one. The whole transcript window still goes down; the partner
    # sends only what that process has not already heard (brainsession._delta),
    # so a resumed room after a dashboard restart re-seeds itself and a live one
    # does not pay for its own history twice.
    turns = _bs.transcript(project, session_id)
    answer = _ask(project, _bs.chat_system(session["seat"]), turns,
                  session_id=session_id)
    reply = None
    if answer.get("ok"):
        reply = _bs.append_message(project, session_id, "assistant",
                                   answer["text"][:_bs.MAX_MESSAGE])
    # 200 even when the model failed: the message IS saved, and the client needs
    # to render it next to an error rather than an exception page that loses it.
    return api.ok({"message": said, "reply": reply, "model": answer,
                   "thinker": _thinker(project, session_id)})


# ---------------------------------------------------------------------------
# Deploy, in two halves
# ---------------------------------------------------------------------------

@router.post("/api/brainstorm/{session_id:int}/synthesize")
def synthesize(session_id: int) -> dict:
    """THE PREVIEW. Propose a plan from the session. Writes nothing.

    Not the session's status, not a work item, not even the plan itself — a
    stored plan would be a fourth thing that can be stale, and the human is
    looking at this one right now. What comes back is what ``deploy`` expects
    to be handed back.
    """
    project = root()
    session = _session(project, session_id)
    # The session, plus the world the plan has to fit: the pillars, loop and
    # constraints for a director session, the existing canon for a narrative
    # one. Sent ONLY here,
    # never on a chat turn — see brainstorm.world_context.
    try:
        turns = _bs.synthesis_turns(project, session)
    except ValueError as exc:
        raise api.bad_request(str(exc), session_id=session_id)
    # persist=False: a synthesis is ONE question under a different system
    # prompt. Asking it down the room's own session would put a JSON plan in the
    # middle of the conversation the human is reading and would leave the
    # partner's next reply arguing with its own proposal.
    answer = _ask(project, _bs.synthesis_system(session["seat"]), turns,
                  session_id=session_id, persist=False, tag="synth",
                  timeout=SYNTH_TIMEOUT, usd=USD_PER_SYNTH)
    if not answer.get("ok"):
        # 503 rather than 502 when there is no partner on this machine at all:
        # the difference between "this cannot work here" and "it failed this
        # time" is the difference between a settings link and a retry button.
        # The sentence to match on moved with the mechanism — it used to be a
        # missing OPENAI_API_KEY and it is now a CLI that is not installed.
        code = 503 if "not found on PATH" in str(answer.get("error")) else 502
        raise api.ApiError(code, str(answer.get("error") or "synthesis failed"),
                           code="synthesis_failed")
    plan = _bs.parse_plan(answer["text"], session["seat"])
    return api.ok({
        "session_id": session_id,
        "seat": session["seat"],
        "plan": plan,
        # Said out loud in the payload because the whole design of this step is
        # that it is safe to press.
        "wrote_nothing": True,
        "already_filed": _bs.already_filed(session, plan),
        "model": {k: answer[k] for k in ("model", "runner", "seconds",
                                         "estimated_usd") if k in answer},
    })


@router.post("/api/brainstorm/{session_id:int}/deploy")
def deploy(session_id: int, payload: dict) -> dict:
    """FILE THE CONFIRMED PLAN. Exactly it, and nothing else.

    The plan comes from the caller, not from a second synthesis: the point of
    the preview is that a human read it, and re-asking the model here would file
    something nobody approved.

    The filing itself is ``brainstorm.file_plan`` — the double-file guard, the
    chain-vs-loose decision and the record-what-landed ordering are shared with
    the MCP door rather than written out twice. This endpoint's job is to say
    what each of its refusals means in HTTP.
    """
    project = root()
    session = _session(project, session_id)
    _writable(session)
    if not isinstance(payload, dict) or payload.get("plan") is None:
        raise api.bad_request(
            "deploy takes the plan you confirmed — call synthesize first and "
            "send back the plan you approved (optionally edited)",
            session_id=session_id)
    try:
        return api.ok(_bs.file_plan(project, session, payload.get("plan"),
                                    again=bool(payload.get("again"))))
    except _bs.AlreadyFiled as exc:
        # The double-click guard, which on this endpoint files every item twice.
        raise api.conflict(str(exc), session_id=session_id, deploy=exc.entry)
    except _bs.PartialDeploy as exc:
        raise api.bad_request(str(exc), session_id=session_id,
                              filed=[int(f["id"]) for f in exc.filed])
    except ValueError as exc:
        raise api.bad_request(str(exc), session_id=session_id)
