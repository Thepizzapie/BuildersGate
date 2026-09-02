"""Brainstorm MCP tools - carved out of server.py, verbatim.

server.py held ~226 tools in 12k lines; the domains that never
touch each other now live apart. The contract is unchanged: the
shared plumbing (_tool, _root, the gates) stays in server, each
domain imports it back, and server star-imports this module at its
BOTTOM - by then its globals all exist, which is what makes the
circular import legal - so server.<tool> still answers for every
caller and test.
"""
from bgate_core.design import brainstorm as _bs
from bgate_mcp.server import (  # noqa: F401
    Optional, _actor, _caller_is_agent, _fail,
    _root, _tool,
)

# ---------------------------------------------------------------------------
# Brainstorm - the cheap room, from the other door
# ---------------------------------------------------------------------------
# The dashboard grew this room and the tool list did not, which in a system with
# two front doors means the capability did not exist for half of it: an agent
# could file work on the board but could not see, join or continue the
# conversation the work came out of, and a human who thought out loud in the
# dashboard was invisible to the session they were talking to. These tools call
# bgate_core.design.brainstorm - the same functions bgate_ui.routes.brainstorm calls - # so the two doors cannot drift on what a session IS.
# tests/mcp/test_brainstorm_mcp.py asserts they haven't.
#
# WHAT DOES NOT COME ACROSS FOR FREE IS THE DISPATCH BAN, AND IT IS THE POINT.
# On the web side "a message cannot dispatch" is nearly free: that process holds
# no tools. Here it is the opposite - this module IS the tool set and queue_add
# is a hundred lines down, so "it has no mechanism" stops being true by accident
# and has to be true by construction. It is:
#
#   * brainstorm_say and brainstorm_synthesize reach a thinking partner only
#     through brainstorm.ask, which spawns a CLI session built WITHOUT tools:
#     an empty built-in tool set, no MCP server and --strict-mcp-config so it
#     cannot inherit the one this very process is serving, no settings sources
#     to add any of it back, and plan mode behind that. The partner used to be
#     a bare chat-completions call; it is a real Claude Code session now, and
#     the guarantee moved from "no tools were passed" to "no tools exist" - #     which matters most HERE, where the tool being withheld is the one this
#     module is currently exporting;
#   * brainstorm_deploy is the only function in this section that so much as
#     names the queue, and it is the only one a machine may not call.
@_tool
def brainstorm_list(seat: Optional[str] = None, status: Optional[str] = None,
                    limit: int = 50) -> dict:
    """The brainstorm file drawer - what has been thought about, and what it filed.

    THE CHEAP ROOM. A brainstorm session is where an idea is still an idea:
    conversation, a writing pad and a drawing pad, none of which queue anything.
    The board is the expensive room. Read a session before proposing work in its
    area - half the "new" ideas an agent files were already argued out and cut
    in a room nobody looked in.

    seat: director (what to BUILD) | narrative (what is TRUE).
    status: open | deployed | archived. Archived sorts last whatever you pass.

    Titles and counts only - never the pads, which come one session at a time
    from brainstorm_open, because an index that ships every scratch document is
    an index nobody can afford to poll.
    """
    root = _root()
    if seat and seat not in _bs.SEATS:
        return {"ok": False, "error": f"seat must be one of {_bs.SEATS}"}
    if status and status not in _bs.STATUSES:
        return {"ok": False, "error": f"status must be one of {_bs.STATUSES}"}
    return {"sessions": _bs.list_sessions(root, seat=seat, status=status,
                                          limit=min(int(limit or 50), 200)),
            "seats": list(_bs.SEATS),
            "model": _bs.available(root)}


@_tool
def brainstorm_new(seat: str = "director", title: str = "") -> dict:
    """Open a brainstorm session. Nothing about this reaches the board.

    Use it when the human is thinking rather than asking - "what if the hub had
    weather" is not a work order, and turning it into one costs a spawned
    session per half-thought and leaves a board full of items nobody meant to
    file. Everything said here stays here until a human deploys it.

    seat picks what the room is FOR, and it is enforced at plan time rather than
    trusted to the prompt:
      director   what to BUILD. May propose work for any seat.
      narrative  what is TRUE - canon, lore, the bible. May propose narrative
                 work only.
    """
    return _bs.create(_root(), seat=seat, title=title)


@_tool
def brainstorm_open(session_id: int) -> dict:
    """One session, whole: the conversation, the notes pad, the drawing, what it filed.

    THE DRAWING COMES BACK AS WORDS. `drawing_text` is the pad's elements
    rendered as lines - "rectangle#hub-1 'hub'", "arrow#a1 hub-1 -> shrine-1" - which is the content of the board and is readable without vision. The raw
    `drawing` scene is there too, and the ids in it are what you reuse if you
    write elements back with brainstorm_note. `drawing_png` is a preview path
    the browser renders; it is never the source of truth.

    `deploys` is what this session has already put on the board. Read it before
    proposing more: a session that filed three items last week is not a blank
    page.

    `thinker` is who else is in the room: which runner and model this session's
    partner runs on, whether a process is live right now, what the conversation
    has cost, and the path to its raw transcript. Worth a glance before you pass
    `reply=` to brainstorm_say - if a partner is already live, its answers and
    yours are two voices in one conversation.
    """
    root = _root()
    session = _bs.read(root, int(session_id))
    return {**session, "drawing_text": _bs.drawing_digest(session["drawing"]),
            "thinker": _bs.thinker(root, int(session_id))}


@_tool
def brainstorm_say(session_id: int, text: str, reply: str = "",
                   to: str = "") -> dict:
    """Say something in a brainstorm session. NOTHING ELSE HAPPENS.

    No work item, no dispatched agent, no approval gate: a turn here is two rows
    in a table. queue_add and brainstorm_deploy are the two calls that file
    work, and neither is reachable from this one.

    `reply` IS THE POINT OF THIS DOOR, and it is worth more now than it was.
    YOU are a model and you are already holding the session, so pass your own
    answer and it is stored as the assistant turn - no second session, no CLI,
    no cost, and no key was ever needed for it. Leave it empty and the dashboard's
    partner answers instead, which spawns a real (tool-less, read-only) CLI
    session and bills a turn against the subscription. Between an answer you
    already have and a process you have to start, the answer you already have is
    strictly better.

    So: a MACHINE must pass `reply=`. A caller stamped as an agent that leaves it
    empty is refused rather than allowed to spawn a nested thinking session - an agent already inside a CLI session paying to start another one to think
    for it is a loop with a bill attached, and it was one flag away from being
    the default behaviour of this tool.

    Either way the human's sentence is stored BEFORE anything is asked, so a
    dead partner costs a reply and never what was typed.

    `to` ADDRESSES ONE SEAT that has been invited into the room
    (brainstorm_invite). Leave it empty and everyone present answers - one CLI
    turn each, in invite order, the room's own partner first - which is what you
    want for "what does everybody think" and is four times the cost for "what
    does the art seat think". `to` is ignored when you pass `reply=`, because
    then YOU are the one answering and there is nobody to address.

    Push back in the reply when something does not hold together, and say which
    part. Do not write a task list here - proposing the work is a separate step
    (brainstorm_synthesize) that a human takes when they are ready.
    """
    root = _root()
    session = _bs.read(root, int(session_id))
    _bs.ensure_open(session)
    answered = str(reply or "").strip()[:_bs.MAX_MESSAGE]
    # Resolved BEFORE the sentence is stored: a message addressed to a seat
    # that is not in the room has been said to nobody, and storing it would
    # leave a question in the transcript that nothing will ever answer.
    speaking = ([""] if answered
                else _bs.answerers(root, int(session_id), to))
    said = _bs.append_message(root, int(session_id), "user", text)
    model = {"ok": True, "answered_by": "the caller", "estimated_usd": 0.0}
    replies: list = []
    if not answered and _caller_is_agent():
        model = {"ok": False, "error":
                 "pass reply= - you are a model holding this session "
                 "already, and spawning a second CLI session to think on "
                 "your behalf costs a turn against the subscription for an "
                 "answer you can simply write. The sentence is stored; "
                 "nothing was lost."}
        speaking = []
    elif not answered:
        answers = []

        def _round(discuss: bool) -> int:
            spoke = 0
            for seat in speaking:
                # session_id makes the room a CONVERSATION: one spawned
                # session per voice answers every message rather than a
                # fresh process per turn. An invited seat's process is built
                # by the same read-only spawner as the room's own partner - # it holds an opinion, never a tool.
                system = (_bs.participant_system(root, seat,
                                                 discuss=discuss) if seat
                          else _bs.chat_system(session["seat"],
                                               discuss=discuss, root=root))
                got = _bs.ask(root, system,
                              _bs.transcript(root, int(session_id),
                                             for_seat=seat),
                              session_id=int(session_id), seat=seat)
                got["seat"] = seat
                got["discuss"] = bool(discuss)
                _bs.record_turn(root, int(session_id), seat, got)
                passed = (discuss and got.get("ok")
                          and _bs.is_pass(got.get("text")))
                got["passed"] = bool(passed)
                answers.append(got)
                if got.get("ok") and not passed:
                    spoke += 1
                    replies.append(_bs.append_message(
                        root, int(session_id), "assistant",
                        got["text"][:_bs.MAX_MESSAGE], seat=seat))
            return spoke

        _round(False)
        # FREE DISCUSSION, same three bounds as the dashboard door: the
        # room's own discuss_rounds setting (0 = off, the default), more
        # than one voice present, and a round where everybody passed ends
        # it. Both doors run it because both doors are the same room.
        rounds = _bs.discuss_rounds(session)
        if rounds and len(speaking) > 1:
            for _ in range(rounds):
                if not _round(True):
                    break
        model = answers[0] if answers else {"ok": False,
                                            "error": "nobody answered"}
    wrote = replies[0] if replies else None
    if answered:
        wrote = _bs.append_message(root, int(session_id), "assistant",
                                   answered)
        replies = [wrote]
    out = {"message": said, "reply": wrote, "replies": replies,
           "model": model, "spoke": speaking,
           "session_id": int(session_id)}
    if wrote is None:
        out["note"] = ("the sentence is stored and nothing was lost - pass "
                       "reply= to write the answer yourself, which needs no "
                       "key and spawns nothing")
    return out


@_tool
def brainstorm_invite(session_id: int, seat: str) -> dict:
    """INVITE A SEAT INTO A BRAINSTORM. It arrives WITHOUT ITS TOOLS.

    The room had two voices - the human and the owning seat's thinking partner - and the question a human actually has ("would weather in the hub be cheap or
    a fortnight") is one only the seat that would BUILD it can answer. This is
    how that seat gets asked.

    WHAT AN INVITED SEAT IS. A CLI session spawned by the same read-only path as
    the room's own partner: an empty built-in tool set, --strict-mcp-config so
    it cannot inherit the server you are reading this on, and at most the
    two-tool pad server. It is the seat's JUDGEMENT, not the seat's HANDS. It
    cannot write a file, run a command, claim work or file anything, and nothing
    it says becomes work on its own - a human still reads a synthesis and
    presses Deploy. Compare queue_add, which puts a real agent with real tools
    on the board; that is the other room and this is deliberately not it.

    Refused, each saying which: a seat that is not a seat, a seat this project
    has disabled, a seat already in the room (including the seat that OWNS it,
    whose partner is the room's own voice), a room already at its limit, and a
    runner that has not declared a read-only mode - that last one is refused
    rather than started with dispatch flags, which is the whole guarantee.
    """
    root = _root()
    out = _bs.invite(root, int(session_id), str(seat or ""))
    return {**out,
            "participants": _bs.participants(root, int(session_id))}


@_tool
def brainstorm_leave(session_id: int, seat: str) -> dict:
    """A seat leaves a brainstorm room. Its process stops; its record stays.

    The row and its spend are kept rather than deleted: the messages that seat
    wrote are still in the transcript, and a roster that could not name the seat
    beside them would leave half the conversation attributed to nobody.
    Re-inviting the same seat later reuses the row, so its cost keeps summing
    over the whole session.
    """
    root = _root()
    out = _bs.leave(root, int(session_id), str(seat or ""))
    return {**out,
            "participants": _bs.participants(root, int(session_id))}


@_tool
def brainstorm_note(session_id: int, notes: Optional[str] = None,
                    title: Optional[str] = None,
                    drawing: Optional[dict] = None) -> dict:
    """Write the session's pads - the title, the writing pad, the drawing scene.

    `notes` REPLACES the whole pad. It is one text area a person types into, not
    a patch protocol, so brainstorm_open it and send the whole document back if
    you are adding to somebody's writing - a partial write here deletes the rest
    of their hour.

    `drawing` is the pad's structured scene ({"elements": [...], "appState":
    {...}}), which is the whole reason a text model can work on it: reuse the
    element ids brainstorm_open showed you rather than inventing new ones, or
    the arrows come back unbound. The flattened PNG is not settable here - only the browser renders one.

    Omitted fields are left alone. Nothing here queues anything.
    """
    root = _root()
    session = _bs.read(root, int(session_id))
    _bs.ensure_open(session)
    changed: list[str] = []
    if title is not None:
        _bs.rename(root, int(session_id), str(title))
        changed.append("title")
    if notes is not None:
        _bs.set_notes(root, int(session_id), str(notes))
        changed.append("notes")
    if drawing is not None:
        _bs.set_drawing(root, int(session_id), drawing)
        changed.append("drawing")
    if not changed:
        return {"ok": False,
                "error": "nothing to change - pass notes, title or drawing"}
    after = _bs.read(root, int(session_id))
    return {**after, "changed": changed,
            "drawing_text": _bs.drawing_digest(after["drawing"])}


@_tool
def brainstorm_synthesize(session_id: int) -> dict:
    """THE PREVIEW: what work this session adds up to. WRITES NOTHING.

    Not a work item, not the session's status, not even the plan - a stored plan
    would be a fourth thing that can go stale. Safe to press, and safe to press
    twice.

    What comes back under `plan` is exactly the shape brainstorm_deploy takes:
    {"summary", "chained", "questions", "items": [{"seat", "title", "brief"}]}.
    `plan.notes` lists every repair made to the model's answer (a seat it named
    that a narrative session may not file, an item with no brief) - those are
    corrections a human should see, not silent fixes.

    HAND THE RESULT TO THE HUMAN. You may not deploy it; see brainstorm_deploy
    for why. If there is no thinking partner on this machine the call fails and
    you can write the plan yourself in that same shape - the review step is what
    matters, not which model drafted it.
    """
    root = _root()
    session = _bs.read(root, int(session_id))
    turns = _bs.synthesis_turns(root, session)
    # persist=False: a synthesis is one question under a different system
    # prompt, and its JSON must not land in the middle of the conversation
    # the human is going to read back.
    answer = _bs.ask(root, _bs.synthesis_system(session["seat"]), turns,
                     session_id=int(session_id), persist=False,
                     tag="synth", timeout=_bs.SYNTH_TIMEOUT,
                     usd=_bs.USD_PER_SYNTH)
    if not answer.get("ok"):
        return {"ok": False, "wrote_nothing": True,
                "error": answer.get("error") or "synthesis failed",
                "note": "no model here - draft the plan yourself in the "
                        "shape brainstorm_deploy takes and put it in front "
                        "of the human"}
    plan = _bs.parse_plan(answer["text"], session["seat"])
    return {"session_id": int(session_id), "seat": session["seat"],
            "plan": plan,
            # Said out loud in the payload because the whole design of this
            # step is that it is safe to press.
            "wrote_nothing": True,
            "already_filed": _bs.already_filed(session, plan),
            "model": {k: answer[k]
                      for k in ("model", "runner", "seconds",
                                "estimated_usd") if k in answer}}


@_tool
def brainstorm_deploy(session_id: int, plan: dict, again: bool = False) -> dict:
    """File a confirmed plan onto the board. HUMAN-ONLY, AND THE ONLY ONE HERE.

    WHY A MACHINE IS REFUSED. This whole room exists so a human reads a plan
    before agents are dispatched against it. An agent that can deploy closes
    that loop on itself: it writes the session, synthesizes a plan out of its
    own sentences, files N items, and each of those spawns another agent - a
    work generator with a review step nobody attended. The same reasoning
    already refuses a machine the write lanes in seat_configure and the
    human-only settings in bgate_core.store.settings, and the same fail-closed test is
    used: BGATE_SEAT / BGATE_WORK_ITEM, not the actor string alone, because a
    gate that reads one stamp is disabled by forgetting one line.

    A human's own session - no seat, no work item - deploys normally. Nothing is
    taken away by this rule: that session could already call queue_add, and
    routing through here is what keeps `source=brainstorm` on the items so the
    board can name the conversation they came from.

    `plan` is what brainstorm_synthesize returned, as the human approved it - edits included, since their text is the point. Items are validated strictly
    rather than repaired: quietly rewriting a confirmed plan files something
    other than what was agreed. Set "chained": true ONLY when each item needs
    what the one before it produced; that becomes a real dependency chain rather
    than a priority preference two agents can start in the same tick.

    `again=True` overrides the guard against filing the identical plan twice.
    """
    try:
        if _caller_is_agent():
            raise PermissionError(
                f"{_actor() or 'an agent session'} may not deploy brainstorm "
                f"{session_id} - a brainstorm is filed by the human who read the "
                "plan, and an agent filing its own proposals is the review step "
                "reviewing itself. Leave the plan where they will see it: "
                "brainstorm_synthesize writes nothing and its result is the "
                "thing to hand over, ask_human gets you a decision without "
                "blocking, and queue_add still files the one item you are sure "
                "of under your own seat, where it is attributed to you.")
        root = _root()
        session = _bs.read(root, int(session_id))
        out = _bs.file_plan(root, session, plan, again=bool(again),
                            by=_actor())
        # THE GAME-PLAN BACK HALF. A plan carrying a `manifest` is the
        # premise-to-plan compiler's output: rows land in plan_row (coverage),
        # slice rows land on the board with real dependency links. Behind the
        # same human gate as the items - the manifest IS the plan, at a finer
        # grain. validate_plan ignores the key, so a manifest-free plan is
        # exactly what it always was.
        if isinstance(plan, dict) and plan.get("manifest"):
            from bgate_core.design import gameplan as _gameplan
            out["game_plan"] = _gameplan.ingest(
                root, plan["manifest"], session_id=int(session_id))
        return out
    except _bs.AlreadyFiled as exc:
        return {"ok": False, "error": str(exc), "already_filed": exc.entry}
    except _bs.PartialDeploy as exc:
        # Some rows ARE on the board. Say which, or the caller re-files them.
        return {"ok": False, "error": str(exc),
                "filed": [int(f["id"]) for f in exc.filed]}
    except Exception as exc:
        return _fail(exc)


@_tool
def brainstorm_close(session_id: int) -> dict:
    """End the session's THINKING PARTNER process. Keeps everything it said.

    THREE WORDS THAT ALL SOUND FINAL, kept apart on purpose:
      close     (this) stops the spawned CLI process. The conversation, the
                notes and the drawing are untouched, and the next message
                reopens it - resuming the same CLI session where it left off, or
                replaying the transcript if the CLI no longer has it. It is
                about the PROCESS. Nothing is decided and nothing is lost.
      archive   files the SESSION away as a record: no new turns, notes or
                deploys until reopened. Implies a close.
      deployed  a STATUS, not an ending: this session put work on the board.
                Usually still open, and usually still being talked in.

    Available to a machine, unlike deploy and delete: stopping a process costs
    nobody their work, and an agent that noticed a room has gone quiet should be
    able to stop it paying for a listener. Idempotent.
    """
    return _bs.close_partner(_root(), int(session_id))


@_tool
def brainstorm_discuss(session_id: int, rounds: int) -> dict:
    """How many EXTRA rounds this room talks AMONG ITSELF. 0 turns it off.

    Off by default, and that default is the old behaviour exactly: the human
    says one thing, every voice present answers once, the room stops. With
    rounds set, each voice then reads what the others just said and replies only
    if it has something to add; a round where everybody passes ends it early.

    Every round is one billed turn PER VOICE IN THE ROOM, so four guests at 2
    rounds is ten turns on one sentence. The ceiling is small on purpose - past
    it a human should be steering, not buying more of the same argument.
    """
    return _bs.set_discuss(_root(), int(session_id), int(rounds))


@_tool
def brainstorm_reset(session_id: int, keep_pads: bool = True) -> dict:
    """START THE THREAD OVER in the same room. Stops the partner, drops the transcript.

    THE FOURTH END-STATE, and the one people reach for most: the conversation
    has gone circular or is arguing from a premise that stopped being true, and
    what is wanted is a clean head - NOT a closed process that resumes the same
    dead thread, and NOT a delete that takes an hour of notes and diagram with
    it. brainstorm_close resumes where it left off; this one makes the next
    message the first message.

    The notes and the drawing SURVIVE by default: they are the human's own
    document, not the conversation. `keep_pads=False` clears those too.

    Deploys are never touched - work already on the board outlives the thread
    that thought of it.
    """
    return _bs.reset(_root(), int(session_id), keep_pads=bool(keep_pads))


@_tool
def brainstorm_feed(session_id: int, cursor: int = 0) -> dict:
    """What the session's partner PROCESS actually emitted - the terminal channel.

    Not the conversation (brainstorm_open has that): this is the raw stream the
    spawned CLI wrote - run boundaries, its own `init` event stating the tool
    list it really built, its calls to the two-tool pad server, their results,
    and its prose. Read forward from `cursor`; keep the one you are handed and
    pass it back to get only what is new.

    Worth reading when a human asks what the partner has been doing, or when you
    want to check the room's promise rather than take it on trust: the `init`
    step names every tool the process holds, and there should be exactly two.
    """
    return _bs.feed(_root(), int(session_id), cursor=int(cursor or 0))


@_tool
def brainstorm_archive(session_id: int, archived: bool = True) -> dict:
    """File a session away, or take it back out. NOTHING IS DELETED either way.

    An archived session is a record rather than a workspace: it still reads, but
    it takes no new turns, notes or deploys until it is reopened with
    archived=False. Reopening restores the status it earned - a session that has
    filed work stays 'deployed', because resetting it to 'open' would erase the
    one field saying that work exists.
    """
    return _bs.archive(_root(), int(session_id), archived=bool(archived))


@_tool
def brainstorm_delete(session_id: int) -> dict:
    """Really delete a session and its messages. HUMAN-ONLY. Prefer archiving.

    Refused for a machine on the same reasoning as brainstorm_deploy, pointing
    the other way: what this destroys is the human's own writing - an hour of
    notes and a drawing that exist nowhere else - and no agent has enough
    context to be sure a quiet session is finished. brainstorm_archive is the
    reversible motion and it is available to any caller.

    Work items already filed from the session are NOT touched. They are on the
    board, they are somebody's job, and they outlive the room they were thought
    up in.
    """
    if _caller_is_agent():
        raise PermissionError(
            f"{_actor() or 'an agent session'} may not delete brainstorm "
            f"{session_id} - it holds writing that exists nowhere else. "
            "Use brainstorm_archive, which files it away, deletes nothing "
            "and can be undone.")
    return _bs.delete(_root(), int(session_id))


