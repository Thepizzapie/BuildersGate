"""The director chat — a Claude Code session, in a pane.

Four endpoints and no cleverness. A message is a message: it is appended to the
project's transcript and answered by the persistent session in
bgate_ui.agents.directorsession, which is a full `claude` in the game directory with
the builders-gate MCP server attached. It files work by calling queue_add like
any other session would; nothing here files anything on its behalf.

What this replaced: every message became a work item with a fenced brief, a
DELEGATED-FROM stamp, a dispatch row, a lineage entry and an archive record —
a conversation modelled as a job queue.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from bgate_ui import api as _api
from bgate_ui.agents import directorsession as _director
from bgate_ui.deps import root

router = APIRouter()

# One message. Long enough for a paragraph and a pasted error, short enough
# that a runaway paste is refused rather than written to disk.
MAX_CHARS = 8000


@router.get("/api/director/chat")
def director_chat(after: int = 0) -> dict:
    """The conversation since message ``after`` — poll it with the last `n`
    you saw and you get only what is new."""
    return _director.history(str(root()), after=after)


@router.post("/api/director/say")
def director_say(payload: dict) -> dict:
    """Say one thing. Returns immediately; the reply streams into the
    transcript as the session produces it."""
    text = str(payload.get("text") or "").strip()
    if not text:
        raise _api.bad_request("say what?  an empty message has nothing to act on")
    if len(text) > MAX_CHARS:
        raise _api.bad_request(
            f"that message is too long — {MAX_CHARS} characters is the cap",
            length=len(text))
    return _director.send(str(root()), text)


@router.post("/api/director/new")
def director_new() -> dict:
    """Start a fresh conversation. The old transcript is archived beside the
    new one on disk, never deleted."""
    return _director.reset(str(root()))


@router.put("/api/director/config")
def director_config(payload: dict) -> dict:
    """Select the native CLI and one of the models that CLI currently offers."""
    try:
        return _director.configure(
            str(root()), str(payload.get("runner") or ""),
            str(payload.get("model") or ""))
    except ValueError as exc:
        raise _api.bad_request(str(exc))


@router.post("/api/director/usage-bridge")
def director_usage_connect(request: Request) -> dict:
    """Opt in to Claude's local, credential-free quota status feed."""
    _api.require_human(_api.current_actor(request), "connect Claude usage")
    from bgate_ui.agents import claudeusage
    try:
        return {"ok": True, **claudeusage.install()}
    except ValueError as exc:
        raise _api.bad_request(str(exc))


@router.delete("/api/director/usage-bridge")
def director_usage_disconnect(request: Request) -> dict:
    """Remove the bridge, its quota snapshot, and restore the prior status line."""
    _api.require_human(_api.current_actor(request), "disconnect Claude usage")
    from bgate_ui.agents import claudeusage
    return {"ok": True, **claudeusage.uninstall()}


@router.post("/api/director/stop")
def director_stop() -> dict:
    """End the session process. The conversation resumes on the next message."""
    return _director.stop(str(root()))
