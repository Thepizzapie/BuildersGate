"""Live chat, feedback sessions, and the handoff to the director.

READ THE TWO THINGS THIS ROUTER DOES NOT DO FIRST, because they are what make
the rest of it safe.

IT NEVER RETURNS A CREDENTIAL. The token lives in the project's gitignored
``.env`` and is written through ``/api/providers/twitch/key``, which is the
existing human-only path with its own no-echo discipline (see
``routes/providers.py``). Nothing here reads it. The channel NAME is written
here — it is not a secret, but it is the dev's identity and it must not be in
the repository either — and it comes back only as itself, which is a fact anyone
watching the stream already knows.

IT NEVER QUEUES WORK. ``POST /api/chat/session/{id}/stop`` closes a session,
builds a digest and opens a DIRECTOR BRAINSTORM with it. That is the end. The
two things the human asked to be able to do from there — keep thinking, or
dispatch a team off the notes — are the two buttons the brainstorm room already
has, and both of them run through ``brainstorm.synthesize`` (which writes
nothing) and then ``brainstorm.deploy`` (which files the plan a human read and
confirmed). There is deliberately no endpoint here that shortens that, because
the confirm step is the only thing standing between a stranger's sentence and an
agent with write access.

    GET    /api/chat                     state, config, who is capturing
    POST   /api/chat/connect             open the socket
    POST   /api/chat/disconnect          close it
    GET    /api/chat/messages?since=N    the live feed, cursor-polled
    POST   /api/chat/config              write the channel to .env (human only)
    POST   /api/chat/session             start a feedback session
    POST   /api/chat/session/{id}/stop   close it, and open the brainstorm
    GET    /api/chat/session/{id}        one session and its items
    POST   /api/chat/item/{id}           promote / dismiss one captured remark
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from bgate_core import chatfeedback as _fb
from bgate_core import chatlink as _chat
from bgate_core import envfile as _envfile
from bgate_core import providers as _providers
from bgate_ui import api
from bgate_ui import chatpump as _pump
from bgate_ui.deps import root

router = APIRouter()


def _default_capture(project) -> str:
    """The project's own 'all' vs 'marked' preference, per session overridable.

    A setting rather than a constant because the right answer depends on the
    size of the channel, and the dev is the only one who knows that. A refusal
    to read it must not stop a session starting.
    """
    from bgate_core import settings as _settings
    try:
        return str(_settings.get(project, "chat.capture") or "all")
    except Exception:
        return "all"


def _platforms(project) -> list[dict]:
    """Every platform, whether it is set up, and what is missing.

    Built from the registry so a second platform is one entry in
    ``chatlink.PLATFORMS`` and no change here. The credential is described
    through ``providers.status_for``, which reports presence and a last-4 and
    has no code path that widens to a value.
    """
    out = []
    for one in _chat.PLATFORMS:
        config, why = _chat.config(project, one.id)
        try:
            key = _providers.status_for(project, one.provider_id)
        except Exception:
            key = {"configured": False, "last4": "", "source": "unset"}
        out.append({
            "id": one.id, "label": one.label,
            "channel_env": one.channel_env, "token_env": one.token_env,
            "setup_url": one.setup_url, "help": one.help,
            "supports_anonymous": one.anonymous,
            "anonymous_limits": one.anonymous_limits,
            "configured": config is not None,
            "channel": config.channel if config else "",
            "anonymous": config.anonymous if config else True,
            "reason": why,
            # Presence and fingerprint only, from the module that owns that rule.
            "token_set": bool(key.get("configured")),
            "token_last4": key.get("last4") or "",
            "token_source": key.get("source") or "unset",
        })
    return out


@router.get("/api/chat")
def chat_view() -> dict:
    """Everything the Community panel renders, in one poll.

    Includes ``capture`` — which of the two mechanisms owns chat right now and
    why — because that is the fact a dev must never have to infer. See
    ``chatfeedback.owner``.
    """
    project = root()
    got = _pump.link(project)
    return api.ok({
        "connection": got.status(),
        "platforms": _platforms(project),
        "capture": _fb.owner(project),
        "feedback": _fb.view(project),
        "privacy": _pump.redaction_advice(project),
        "env_gitignored": _providers.env_is_ignored(project),
        "states": _chat.STATES,
    })


@router.post("/api/chat/connect")
def chat_connect() -> dict:
    """Open the connection. Returns immediately — the handshake is on a thread."""
    return api.ok(_pump.link(root()).start())


@router.post("/api/chat/disconnect")
def chat_disconnect() -> dict:
    """Close the socket. Captured feedback is untouched; this is the connection."""
    return api.ok(_pump.link(root()).stop())


@router.get("/api/chat/messages")
def chat_messages(since: int = 0, limit: int = 100) -> dict:
    """The live feed, forward from a sequence cursor.

    ``missed`` is true when the caller's cursor fell off the back of the ring
    buffer — an honest "you missed some" beats a silent gap in a log somebody is
    reading to decide what their audience thinks.
    """
    got = _pump.link(root())
    return api.ok({**got.messages(since=int(since or 0),
                                  limit=min(int(limit or 100), 200)),
                   "connection": got.status()})


@router.post("/api/chat/config")
def chat_config(request: Request, payload: dict) -> dict:
    """Write a platform's CHANNEL into the project's .env. Human only.

    WHY THIS IS A WRITE ENDPOINT AND NOT A SETTING. The channel name is the
    dev's own identity and this is a public tool: it must not be in the
    repository, in a default, or in a database somebody might copy between
    machines. ``.env`` is the file this project already gitignores and already
    writes credentials into, so the channel goes there through the same atomic,
    rest-of-the-file-preserving writer.

    Human-only for the same reason setting a key is: an agent that can point the
    dashboard at a channel can point it at any channel.
    """
    api.require_human(api.current_actor(request), "set the chat channel")
    body = payload if isinstance(payload, dict) else {}
    try:
        one = _chat.platform(str(body.get("platform") or ""))
    except ValueError as exc:
        raise api.bad_request(str(exc))
    value = body.get("channel")
    if not isinstance(value, str):
        raise api.bad_request('send {"channel": "..."} — your channel name')
    channel = value.strip().lstrip("#").lower()
    project = root()
    try:
        if channel:
            action = _envfile.write_var(project, one.channel_env, channel)
        else:
            action = "removed" if _envfile.remove_var(
                project, one.channel_env) else "absent"
    except _envfile.EnvWriteError as exc:
        raise api.bad_request(str(exc), platform=one.id)
    except OSError as exc:
        raise api.unavailable(
            f"could not write the project's .env: {type(exc).__name__}: {exc}",
            platform=one.id)
    _envfile.reset_cache()
    # Live in this process too, the same half `providers.set_key` documents:
    # load_project_env refuses to overwrite a name already in os.environ, so
    # without this the second save of a channel would never take effect and the
    # panel would look broken.
    import os as _os
    if channel:
        _os.environ[one.channel_env] = channel
    else:
        _os.environ.pop(one.channel_env, None)
    return api.ok({"platforms": _platforms(project),
                   "connection": _pump.link(project).status()}, write=action)


# ---------------------------------------------------------------------------
# Feedback sessions
# ---------------------------------------------------------------------------

@router.post("/api/chat/session")
def session_start(request: Request, payload: Optional[dict] = None) -> dict:
    """Open a feedback session. Chat's messages start being captured NOW.

    Refused while a playtest is recording: chat is already leaving notes on that
    recording, with a clock and a frame, and a second mechanism collecting the
    same messages is how one remark becomes two work items. The refusal says
    where to look instead.
    """
    body = payload or {}
    project = root()
    try:
        session = _fb.start(
            project,
            platform=str(body.get("platform") or ""),
            channel=_pump.link(project).status().get("channel") or "",
            title=str(body.get("title") or ""),
            prompt=str(body.get("prompt") or ""),
            capture=str(body.get("capture") or _default_capture(project)),
            actor=api.current_actor(request))
    except _fb.AlreadyOpen as exc:
        raise api.conflict(str(exc), session=exc.session)
    except _fb.Recording as exc:
        raise api.conflict(str(exc), playtest_session_id=int(exc.playtest["id"]),
                           capture=_fb.owner(project))
    except ValueError as exc:
        raise api.bad_request(str(exc))
    # Tell chat, if we can. An anonymous connection cannot post, and the payload
    # says which happened rather than letting the dev assume their viewers were
    # told something they were not.
    announced = _pump.link(project).say(_fb.announcement(session, "start"))
    return api.ok({"session": session, "capture": _fb.owner(project),
                   "announced": announced,
                   "announce_note": "" if announced else
                   "not announced in chat — this connection is read-only, so "
                   "tell your viewers out loud that you are collecting feedback"})


@router.get("/api/chat/session/{session_id:int}")
def session_read(session_id: int) -> dict:
    try:
        return api.ok(_fb.read(root(), session_id))
    except _fb.Missing as exc:
        raise api.not_found(str(exc), session_id=session_id)


@router.post("/api/chat/session/{session_id:int}/stop")
def session_stop(session_id: int, request: Request,
                 payload: Optional[dict] = None) -> dict:
    """Close the session and hand what chat said to the director.

    WHAT THIS RETURNS IS A ROOM, NOT A RESULT. ``brainstorm_id`` is an open
    director brainstorm with the fenced digest in its notes pad and one opening
    turn in its transcript. Nothing has been synthesised, nothing has been
    queued, no model has been called and no agent has been spawned —
    ``queued_nothing`` says so in the payload, because the whole design of this
    step is that it is safe to press.

    From that room the human can keep talking (the thinking partner has no
    tools) or press Synthesize, which PROPOSES a plan and writes nothing, and
    then Deploy, which files exactly the plan they read. Both routes to the
    board are the same route, and it has a human in it.
    """
    project = root()
    body = payload or {}
    try:
        out = _fb.stop(project, session_id,
                       to_brainstorm=body.get("brainstorm") is not False,
                       actor=api.current_actor(request))
    except _fb.Missing as exc:
        raise api.not_found(str(exc), session_id=session_id)
    kept = int((out.get("counts") or {}).get("total") or 0)
    _pump.link(project).say(_fb.announcement(out, "stop", kept))
    out["capture"] = _fb.owner(project)
    return api.ok(out)


@router.post("/api/chat/item/{item_id:int}")
def item_status(item_id: int, payload: dict) -> dict:
    """Promote or dismiss one captured remark.

    Promoting does NOT file work — it marks the item as one the dev wants
    carried into the digest with weight. The same disposition playtest feedback
    has, meaning the same thing: 'new' is a candidate nobody has judged.
    """
    body = payload if isinstance(payload, dict) else {}
    try:
        return api.ok(_fb.set_item_status(root(), item_id,
                                          str(body.get("status") or "")))
    except _fb.Missing as exc:
        raise api.not_found(str(exc), item_id=item_id)
    except ValueError as exc:
        raise api.bad_request(str(exc), item_id=item_id)
