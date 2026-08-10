"""The brainstorm room's thinking partner: a real CLI session that cannot write.

WHAT CHANGED AND WHY IT MATTERS. The room used to talk to a bare
chat-completions endpoint, and "a message cannot dispatch" was nearly free
there: that call is a messages array and a reply, with no tools to leave out.
The human asked for a spawned Claude Code session instead — a thinking partner
that is the same thing the rest of this product runs on — and a Claude Code
session has Write, Edit and Bash from its first token and inherits every MCP
server registered on the machine, INCLUDING builders-gate and its queue_add.

So the guarantee had to be re-established on a new basis rather than reworded.
It is re-established by REMOVING THE CAPABILITY, not by asking the session not
to use it: see runners._CLAUDE_READONLY, which is the argv, and
runners.CLAUDE_READONLY_BY, which is the sentence shown to a human who asks why
they should believe it. Both were checked against the CLI's own `system/init`
event — the one place that reports the tool list and the MCP servers the process
actually constructed. It said `"tools":[]` and `"mcp_servers":[]`, and a session
asked to write a file wrote none. The model's own account of its tools is NOT
evidence and was observed not to be: asked to list them at a session whose init
said `"tools":[]`, it recited the standard fifteen from memory.

THE BUILDERS-GATE MCP SERVER IS DELIBERATELY ABSENT, AND A TWO-TOOL ONE IS NOT.
bible_read, lore_list and scope_check would each make this a better partner, and
registering the server is one call to runners.mcp_overrides(). It is not made,
because that helper registers the WHOLE server: queue_add, bible_add,
lore_update, image_generate (real money) and blender_run (executes a script). An
allowlist naming eight readers out of a hundred and fifty tools would put the
room's entire promise on nobody ever mistyping one entry. What those readers
would have supplied is supplied instead by brainstorm.world_context(), which
renders the pillars, the cut line and the established canon as prose.

What could NOT be supplied that way is the human's own pads, which change while
the conversation is happening — a partner that cannot see the diagram beside it
is answering with one eye shut. So the answer is a SMALL server rather than a
filtered big one: bgate_mcp.padserver, two tools, one session, no queue and no
filesystem. Registered through --mcp-config with --strict-mcp-config, which
together are an exhaustive statement rather than a preference.

ONE PROCESS PER SESSION, HELD OPEN BETWEEN MESSAGES, AND RESUMED WHEN IT IS NOT.
A brainstorm is a conversation, so each message is a turn in the same session.
That is `prompt_via == "stream"`: stdin stays open, one `result` event comes back
per turn, and the process waits for the next one. When the process is gone — the
dashboard restarted, the room was closed, an idle reap — reopening resumes the
CLI's OWN session by id rather than replaying a transcript into a blank one.
Re-seeding is still there and still correct, as the FALLBACK for every way a
resume can legitimately fail; see _resume_failed.

TWO CHANNELS OFF ONE TURN, and keeping them apart is what stops the terminal
view and the voice feature fighting:

    the TERMINAL channel   every event the CLI emitted, raw NDJSON on disk,
                           served by feed() for the transcript view. Tool calls,
                           results, run boundaries, the lot.
    the SPOKEN channel     the turn's final assistant prose and nothing else —
                           `result.text` from ask(). It is what is stored as the
                           assistant message and what text-to-speech is handed.

They come from the same `result` event and they are not the same thing. Speaking
the terminal channel would read tool JSON out loud; showing only the spoken
channel would make the transcript view a duplicate of the chat pane.

NOTHING HERE IMPORTS bgate_ui.dispatch, on purpose. The two modules spawn
processes the same way and share nothing else: dispatch's whole job is to give
an agent the tool set this one exists to withhold, and the two small parsers
below are copied rather than imported so that the brainstorm path cannot be one
refactor away from holding the dispatcher.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from bgate_core import settings as _settings
from bgate_core import spend as _spend
from bgate_ui import runners as _runners

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# (project key, session id) -> the live process and what it has been told.
_live: dict[tuple[str, int], dict] = {}
_lock = threading.Lock()

# A session nobody has spoken to for this long is not a conversation, it is a
# process holding a pipe. The transcript is in the DB either way, so reaping one
# costs the next turn a respawn and loses nothing a human typed.
IDLE_S = int(os.environ.get("BGATE_BRAINSTORM_IDLE_S") or 30 * 60)

# How many thinking sessions may be alive at once, across every project. Each is
# a CLI process; a dashboard left open on a dozen brainstorms should not be
# holding a dozen of them. The oldest idle one is reaped to make room.
MAX_LIVE = 6

# How long one turn may take before the session is treated as wedged. Generous:
# a synthesis reads an hour of conversation. A turn that blows this kills the
# process rather than leaving it, because a wedged session answers every
# subsequent message with the same silence.
TURN_TIMEOUT_S = 180.0

# How often the turn loop looks at the log. The reply is read by tailing the
# file the process is writing rather than by reading its pipe, for the same
# reason dispatch does: a blocking readline cannot be given a deadline on
# Windows, and a thinking partner that can hang a request forever is worse than
# one that is occasionally slow.
POLL_S = 0.12


class Unavailable(RuntimeError):
    """No thinking partner can start here, and the reason is a sentence."""


def _pkey(root) -> str:
    try:
        return str(Path(root).resolve())
    except OSError:
        return str(root)


# ---------------------------------------------------------------------------
# Which runner, which model, what it may spend
# ---------------------------------------------------------------------------

def _setting(root, key: str, fallback):
    """A stored setting, or the fallback. NEVER raises: a brainstorm must not
    become unusable because a settings doc will not read."""
    try:
        value = _settings.get(root, key)
    except Exception:
        return fallback
    return fallback if value is None or value == "" else value


def runner_for(root) -> "_runners.Runner":
    """Which CLI this project brainstorms on.

    Through runners.get rather than a name, because that is the whole answer to
    "expand later for codex, and local llms": a new thinking partner is one row
    in the table there and no change here. get() never raises on an unknown
    name, so a typo in a stored setting falls back to the default rather than
    taking the room down.
    """
    return _runners.get(str(_setting(root, "brainstorm.runner",
                                     _runners.DEFAULT_RUNNER)))


# The model a brainstorm falls back to when the SETTING CANNOT BE READ. It must
# equal the registry default, and it must not be blank.
#
# MEASURED, on a live session in another project: a turn ran on
# claude-opus-5[1m] and cost $0.0438 for one trivial exchange, while
# brainstorm.model read "sonnet". The cause was here — the fallback was "",
# which _model_for turned into None, which build_args turned into no --model at
# all, which the CLI turned into whatever it defaults to today. Any dashboard
# process older than the settings entry (or any registry read that raised) took
# that path. A blank fallback in a function whose entire purpose is "never let
# the CLI choose" was the bug, not the plumbing above it.
FALLBACK_MODEL = "sonnet"


def _model_for(root) -> Optional[str]:
    """The model a brainstorm turn runs on. NAMED, never inherited.

    dispatch._model_for exists because nothing passed --model for months and
    every seat silently ran on whatever the CLI defaulted to that day — 1.19
    billion input-side tokens in one night. The cheap room is the last place to
    repeat that, so the fallback here is a real model rather than an empty
    string; see FALLBACK_MODEL for the run that proved it matters.
    """
    chosen = str(_setting(root, "brainstorm.model", FALLBACK_MODEL) or "").strip()
    return chosen or FALLBACK_MODEL


def _ceiling(root) -> float:
    try:
        return max(0.0, float(_setting(root, "brainstorm.max_usd", 2.0)))
    except (TypeError, ValueError):
        return 2.0


def _scratch(root, session_id: int) -> Path:
    """The working directory a thinking session is given.

    NOT the game project. The tool set is empty, so this is belt rather than
    braces — but it is cheap belt: a cwd inside .bgate means CLAUDE.md
    auto-discovery finds the harness's instructions to a SEAT rather than
    handing them to a room that has no seat, and anything that ever did acquire
    a file tool would be pointed at a scratch directory instead of at somebody's
    game.
    """
    path = Path(root) / ".bgate" / "brainstorm" / f"session-{int(session_id)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sidecar(root, session_id: int) -> Path:
    """Where a session remembers WHICH CLI conversation it is.

    A file rather than a column, so this ships without a migration and so a
    stale one is deletable by hand. It holds no content — only the CLI's session
    id and a fingerprint of the last turn that session heard, which is what lets
    a resume know where to carry on from.
    """
    return (Path(root) / ".bgate" / "brainstorm"
            / f"session-{int(session_id)}.json")


def _read_sidecar(root, session_id: int) -> dict:
    try:
        data = json.loads(_sidecar(root, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_sidecar(root, session_id: int, data: dict) -> None:
    """Best effort, always. A brainstorm that cannot write its resume marker
    still works — it replays instead of continuing, which is the fallback this
    whole path is designed around."""
    try:
        path = _sidecar(root, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _sha(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:16]


def _mark(turn: dict) -> dict:
    """A turn, as something a later process can recognise without storing it.

    The transcript is already in the DB; keeping a second verbatim copy in the
    sidecar would be a second thing to keep in step and a second place somebody's
    private thinking sits. A role and a hash is enough to FIND the turn again in
    a window that may have slid.
    """
    return {"role": str(turn.get("role") or ""),
            "sha": _sha(turn.get("content"))}


def _resume_point(turns: list[dict], mark: dict) -> Optional[int]:
    """Index of the last turn a resumed session already heard, or None.

    Scanned from the END, because a conversation that circles back to the same
    sentence ("ok", "go on") would otherwise match the first one and replay half
    the session. None means "cannot place it" — the window slid past it, or the
    transcript was edited — and the caller must re-seed rather than guess.
    """
    if not mark or not mark.get("sha"):
        return None
    for i in range(len(turns) - 1, -1, -1):
        if (turns[i].get("role") == mark.get("role")
                and _sha(turns[i].get("content")) == mark.get("sha")):
            return i
    return None


def _pad_config(root, session_id: int) -> str:
    """The --mcp-config document registering the pad server, or "".

    Built by the pad server itself so the registration and the module cannot
    disagree, imported LAZILY and guarded because it pulls in the MCP SDK: a
    dashboard whose MCP extra is broken must still be able to hold a
    conversation. Returning "" degrades to a partner with no tools at all, which
    is the behaviour this room shipped with — worse, not broken.
    """
    try:
        from bgate_mcp import padserver

        return json.dumps(padserver.config(str(root), int(session_id)),
                          separators=(",", ":"))
    except Exception:
        return ""


def log_path(root, session_id: int, tag: str = "") -> Path:
    """Where this session's raw NDJSON goes.

    One file per brainstorm, appended across respawns — it is the backing for
    the terminal view this room is meant to grow. A one-shot (a synthesis) gets
    its own file rather than interleaving a different system prompt's turns into
    the conversation somebody is going to read back.
    """
    name = f"session-{int(session_id)}" + (f".{tag}" if tag else "") + ".log"
    return Path(root) / ".bgate" / "brainstorm" / name


# ---------------------------------------------------------------------------
# What a caller can be told before anything is spawned
# ---------------------------------------------------------------------------

def available(root) -> dict:
    """Can this project think at all, and on what.

    Answers without spawning anything, because it rides in the sessions index,
    which is polled. ``label`` is what the workspace header chip shows — it
    replaced a chip reading `gpt-4o-mini`, which named a model this room no
    longer talks to.
    """
    runner = runner_for(root)
    model = _model_for(root) or "the CLI default"
    label = f"{runner.name} · {model}"
    if runner.chat is None:
        return {"available": False, "runner": runner.name, "model": model,
                "label": label, "readonly": False, "cost_tracked": False,
                "reason": f"{runner.name} has no read-only conversational mode "
                          "here — a brainstorm may not run on a runner that "
                          "could write to the project. Set brainstorm.runner "
                          f"to one that has one ({', '.join(can_think())})."}
    exe = runner.find()
    if not exe:
        return {"available": False, "runner": runner.name, "model": model,
                "label": label, "readonly": True,
                "cost_tracked": runner.chat.cost_tracked,
                "reason": f"{runner.name} CLI not found on PATH — the "
                          "brainstorm room spawns a real session, so it needs "
                          "the CLI itself rather than an API key"}
    return {"available": True, "runner": runner.name, "model": model,
            "label": label, "readonly": True,
            "cost_tracked": runner.chat.cost_tracked,
            "readonly_by": runner.chat.readonly_by}


def can_think() -> list[str]:
    """Every runner that has a read-only conversational mode at all."""
    return sorted(name for name, r in _runners.RUNNERS.items() if r.chat)


def thinker(root, session_id: int) -> dict:
    """What this session's thinking partner IS, for the UI.

    Everything the header chip and the (coming) terminal view need in one place:
    which runner, which model, whether a process is live right now, where its
    transcript is on disk, and what this conversation has cost so far.
    """
    ready = available(root)
    key = (_pkey(root), int(session_id))
    note = _read_sidecar(root, session_id)
    with _lock:
        entry = _live.get(key)
        live = bool(entry) and entry["proc"].poll() is None
        detail = {
            "live": live,
            "turns": int((entry or {}).get("turns") or note.get("turns") or 0),
            "spent_usd": round(float((entry or {}).get("spent_usd") or 0.0), 4),
            "cli_session_id": str((entry or {}).get("cli_session_id")
                                  or note.get("cli_session_id") or ""),
            "resumed": bool((entry or {}).get("resumed")),
            "pads": bool((entry or {}).get("pads")),
            # THE READBACK, surfaced. What the CLI itself said it built, so a
            # human can check the room's promise instead of believing it. Empty
            # until a turn has been taken — this is observation, not a claim.
            "tools": list((entry or {}).get("tools") or []),
            "mcp_servers": list((entry or {}).get("mcp_servers") or []),
        }
    # "resumable" is what the CLOSE button can honestly promise. A closed
    # session with a marker reopens where it left off; one without replays.
    detail["resumable"] = bool(note.get("cli_session_id"))
    return {**ready, **detail, "max_usd": _ceiling(root),
            "log": str(log_path(root, session_id)),
            "session_id": int(session_id)}


def feed(root, session_id: int, cursor: int = 0, limit: int = 400) -> dict:
    """THE TERMINAL CHANNEL: what the session actually emitted, from a byte cursor.

    The other half of the two-channel split in the module docstring. This is the
    raw stream — run boundaries, the CLI's init, tool calls to the pad server,
    their results, assistant prose — rendered for a transcript view. The SPOKEN
    channel (ask()'s ``text``) is the final assistant prose alone and is never
    taken from here; reading this out loud would recite tool JSON.

    A cursor rather than a tail, because the view polls: re-parsing a session's
    whole log every two seconds is how a long conversation becomes slow to WATCH
    as well as to hold. ``cursor`` 0 means from the top, and the caller keeps
    the one it is given.

    NOT A PTY, and that is a deliberate ceiling rather than an unfinished job.
    The CLI here runs `-p` with stream-json on both ends — a structured event
    channel with no terminal attached and no cursor addressing to emulate. A
    real PTY would buy a spinner and colour codes at the cost of a second I/O
    path with different failure modes, for a session that has no interactive
    prompt to drive.
    """
    path = log_path(root, session_id)
    events: list[dict] = []
    try:
        size = path.stat().st_size
    except OSError:
        return {"events": [], "cursor": 0, "size": 0}
    start = max(0, int(cursor or 0))
    if start > size:
        start = 0          # the log was truncated or replaced; re-read it
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read()
            end = fh.tell()
    except OSError:
        return {"events": [], "cursor": start, "size": size}
    lines = chunk.split(b"\n")
    # A trailing partial line is not consumed: the cursor stops before it so the
    # next poll reads it whole. Half a JSON object rendered as a step is the
    # flicker that makes a live view look broken.
    tail = lines.pop()
    end -= len(tail)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            events.append({"kind": "raw", "text": line.decode(
                "utf-8", "replace")[:600]})
            continue
        step = _step(ev)
        if step:
            events.append(step)
    return {"events": events[-max(1, int(limit)):], "cursor": end, "size": size}


def _step(ev: Any) -> Optional[dict]:
    """One log event as a line a person can read. None for the noise.

    Deliberately lossy in one direction: the raw payloads of tool calls are
    summarised rather than dumped. A transcript view is for following what
    happened, and a 4KB Excalidraw scene pasted into it buries the sentence next
    to it.
    """
    if not isinstance(ev, dict):
        return None
    kind = str(ev.get("type") or "")
    if kind == "bgate_brainstorm_start":
        return {"kind": "boundary", "text": (
            "resumed the session" if ev.get("resumed") else "started a session"),
            "ts": ev.get("ts")}
    if kind == "system" and ev.get("subtype") == "init":
        tools = ev.get("tools") or []
        return {"kind": "init", "session": str(ev.get("session_id") or "")[:8],
                "model": str(ev.get("model") or ""),
                "tools": [str(t) for t in tools] if isinstance(tools, list) else [],
                "text": (f"{len(tools)} tool(s): "
                         + ", ".join(str(t) for t in tools) if tools
                         else "no tools")}
    if kind == "assistant":
        out = []
        for block in (ev.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                out.append({"kind": "say", "text": str(block["text"])[:4000]})
            elif block.get("type") == "tool_use":
                out.append({"kind": "tool", "name": str(block.get("name") or ""),
                            "text": _brief(block.get("input"))})
        return out[0] if len(out) == 1 else ({"kind": "group", "steps": out}
                                             if out else None)
    if kind == "user":
        for block in (ev.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return {"kind": "result", "text": _brief(block.get("content"))}
        return None          # replayed user turns: the chat pane already has them
    if kind == "result":
        return {"kind": "turn_end", "subtype": str(ev.get("subtype") or ""),
                "cost": ev.get("total_cost_usd"),
                "text": str(ev.get("result") or "")[:4000]}
    return None


def _brief(value: Any, cap: int = 300) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value)[:cap * 2]
    except (TypeError, ValueError):
        text = str(value)
    text = " ".join(str(text).split())
    return text[:cap] + ("…" if len(text) > cap else "")


# ---------------------------------------------------------------------------
# The process
# ---------------------------------------------------------------------------

def _reap(key: tuple[str, int], entry: dict) -> None:
    """Stop one thinking session and let go of its handles. Safe twice."""
    proc = entry.get("proc")
    for name in ("stdin",):
        try:
            entry[name].close()
        except Exception:
            pass
    try:
        if proc is not None and proc.poll() is None:
            _kill_tree(proc.pid)
    except Exception:
        pass
    try:
        entry["handle"].close()
    except Exception:
        pass
    with _lock:
        if _live.get(key) is entry:
            del _live[key]


def _kill_tree(pid: int) -> None:
    """The CLI spawns children even with no MCP servers; kill the tree."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, creationflags=_NO_WINDOW,
                           timeout=15)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def stop(root, session_id: int) -> dict:
    """End this session's thinking process. The conversation is not touched —
    it is rows in the DB, and the next message spawns a fresh partner seeded
    with it."""
    key = (_pkey(root), int(session_id))
    with _lock:
        entry = _live.get(key)
    if entry is None:
        return {"ok": True, "stopped": False}
    _reap(key, entry)
    return {"ok": True, "stopped": True}


def stop_all(root=None) -> dict:
    """End every thinking session, or every one belonging to one project.

    THE KILL SWITCH HAS TO REACH THIS ROOM. dispatch.kill_all promises to stop
    every agent on a project, and until the brainstorm partner was a spawned
    process there was nothing here for it to miss. There is now: a CLI session
    holding a pipe, invisible to the agents table, which would survive the one
    button whose whole job is "stop everything, I do not yet know what is
    wrong". It cannot write anything — but "the kill switch left a process
    running" is not a sentence this product should be able to produce.
    """
    want = _pkey(root) if root is not None else None
    with _lock:
        targets = [(k, e) for k, e in _live.items()
                   if want is None or k[0] == want]
    for key, entry in targets:
        _reap(key, entry)
    return {"stopped": [k[1] for k, _ in targets]}


def _evict_if_crowded() -> None:
    """Make room under MAX_LIVE by reaping the least recently used session."""
    while True:
        with _lock:
            if len(_live) < MAX_LIVE:
                return
            key, entry = min(_live.items(), key=lambda kv: kv[1]["last_at"])
        _reap(key, entry)


def _spawn(root, session_id: int, runner: "_runners.Runner", system: str, *,
           register: bool = True, tag: str = "", resume: str = "",
           pads: bool = True) -> dict:
    """Start one read-only thinking session.

    ``register`` is what separates the room from a one-shot: a persistent
    session is held in ``_live`` and answers the next message too, where a
    synthesis is spawned, asked once and reaped. An unregistered entry is never
    reachable by a second caller, which is the point — two synthesis presses
    must not queue behind each other on one pipe.

    ``resume`` continues a real CLI conversation instead of starting one. The
    entry records that it is provisional (``resumed``), because a resume can
    fail for reasons nothing here controls and the caller has to be able to fall
    back to a re-seed rather than report a broken room.

    ``pads`` registers the two-tool pad server. A synthesis says no: it is one
    question over a snapshot the caller already assembled, and a tool that could
    change the drawing mid-synthesis would make the plan a human reads describe
    a board that no longer exists.
    """
    exe = runner.find()
    if not exe:
        raise Unavailable(f"{runner.name} CLI not found on PATH")
    cwd = _scratch(root, session_id)
    path = log_path(root, session_id, tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "ab")
    # A run boundary, for the same reason dispatch writes one: the log appends
    # across respawns, and a reader that cannot tell this process's output from
    # the last one's shows a stale reply as the current answer. The terminal
    # view renders these as the seam between "you closed it" and "you came
    # back", which is a thing a person actually wants to see in a transcript.
    handle.write((json.dumps({"type": "bgate_brainstorm_start",
                              "session_id": int(session_id),
                              "runner": runner.name,
                              "resumed": bool(resume),
                              "ts": time.time()}) + "\n").encode("utf-8"))
    handle.flush()
    start_pos = handle.tell()
    mcp_config = _pad_config(root, session_id) if pads else ""
    args = runner.chat.build_args(exe, system=system, model=_model_for(root),
                                  max_usd=_ceiling(root),
                                  mcp_config=mcp_config, resume=resume)
    # The environment is the dashboard's MINUS the seat stamps. A thinking
    # session is nobody's seat and holds no work item, and leaving BGATE_SEAT
    # set would let anything that reads it (the hook, an env-sniffing tool a
    # future runner does have) treat this room as a dispatched agent.
    env = {k: v for k, v in os.environ.items()
           if k not in ("BGATE_SEAT", "BGATE_WORK_ITEM", "BGATE_LOCK_OWNER")}
    env["BGATE_ACTOR"] = f"brainstorm:{int(session_id)}"
    try:
        proc = subprocess.Popen(args, cwd=str(cwd), env=env,
                                stdin=subprocess.PIPE, stdout=handle,
                                stderr=handle, creationflags=_NO_WINDOW)
    except OSError as exc:
        handle.close()
        raise Unavailable(f"could not start {runner.name}: {exc}") from exc
    entry = {"proc": proc, "handle": handle, "stdin": proc.stdin,
             "log": str(path), "scan_pos": start_pos, "rem": b"",
             "sent": [], "turns": 0, "spent_usd": 0.0,
             "cli_session_id": str(resume or ""),
             "runner": runner.name, "system": system, "pads": bool(mcp_config),
             "resumed": bool(resume), "tools": [],
             "started_at": time.monotonic(), "last_at": time.monotonic(),
             "turn_lock": threading.Lock()}
    if register:
        with _lock:
            _live[(_pkey(root), int(session_id))] = entry
    return entry


def _usable(entry: dict, system: str) -> bool:
    """Is this entry still the right partner for this question?

    Three ways it is not: the process died, nobody has spoken to it in IDLE_S,
    or the system prompt changed (the seat was different, or the caller is
    asking a synthesis question of a chat session). A stale entry answers with
    silence or with the wrong persona, and both read to the human as the model
    being broken.
    """
    if entry["proc"].poll() is not None:
        return False
    if time.monotonic() - entry["last_at"] > IDLE_S:
        return False
    return entry.get("system") == system


# ---------------------------------------------------------------------------
# One turn
# ---------------------------------------------------------------------------

def _user_msg(text: str) -> str:
    """A stream-json user turn — the wire format the CLI reads from stdin."""
    return json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": text}]}}) + "\n"


def _delta(entry: dict, turns: list[dict]) -> list[dict]:
    """The turns this process has not been told yet.

    The caller hands the whole transcript window every time, because that is
    what a stateless chat endpoint needed and both doors still build it that
    way. A live session has already heard most of it, so re-sending would put
    the conversation in twice and bill for it.

    A window that no longer starts where this process's history starts — the
    40-message window slid, or the session was edited — falls back to the whole
    thing. Repeating context is a wasted turn; DROPPING it silently answers a
    question the human did not ask.
    """
    sent = entry.get("sent") or []
    if turns[:len(sent)] == sent:
        return turns[len(sent):]
    return turns


def _as_prompt(delta: list[dict]) -> str:
    """The delta as one user message.

    A single trailing turn goes across verbatim — anything else and the human's
    own sentence arrives wrapped in scaffolding it did not have. A longer delta
    is a RESEED (fresh process, existing conversation) and has to be labelled,
    or the model reads its own earlier replies as things the human said.
    """
    if len(delta) == 1 and delta[0].get("role") == "user":
        return str(delta[0].get("content") or "")
    lines = []
    for turn in delta[:-1]:
        who = "THEM" if turn.get("role") == "user" else "YOU (earlier)"
        lines.append(f"{who}: {turn.get('content')}")
    last = delta[-1] if delta else {"content": ""}
    head = ("EARLIER IN THIS CONVERSATION — you said the parts marked YOU:\n"
            + "\n\n".join(lines) + "\n\n----\n\n") if lines else ""
    return head + str(last.get("content") or "")


def _tokens(ev: dict) -> dict:
    """The turn's token usage, in the ledger's four names.

    Copied from dispatch rather than imported — see the module docstring on why
    this file does not hold the dispatcher.
    """
    usage = ev.get("usage")
    if not isinstance(usage, dict):
        return {}

    def n(key: str) -> int:
        try:
            return max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    return {"input": n("input_tokens"), "output": n("output_tokens"),
            "cache_read": n("cache_read_input_tokens"),
            "cache_write": n("cache_creation_input_tokens")}


def _model_of(ev: dict) -> str:
    usage = ev.get("modelUsage")
    if isinstance(usage, dict) and usage:
        return max(usage, key=lambda k: (usage[k] or {}).get("outputTokens", 0)
                   if isinstance(usage[k], dict) else 0)
    return str(ev.get("model") or "")


def _read_events(entry: dict) -> list[dict]:
    """Whatever the process has written since the last look, parsed.

    Forward from a byte cursor, with the possibly-partial last line carried
    over. A turn that re-read the whole file every 120ms would make a long
    conversation quadratically slower to hold.
    """
    try:
        with open(entry["log"], "rb") as fh:
            fh.seek(entry["scan_pos"])
            chunk = entry["rem"] + fh.read()
            entry["scan_pos"] = fh.tell()
    except OSError:
        return []
    lines = chunk.split(b"\n")
    entry["rem"] = lines.pop()
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue      # the CLI also writes plain stderr into this file
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _collect(entry: dict, deadline: float) -> dict:
    """Drain the log until this turn's `result` event, or fail saying why.

    A `result` is the CLI's end-of-turn, not end-of-session: the process goes
    straight back to waiting on stdin, which is what makes a conversation
    possible at all. Anything else here — an exit, a timeout — is terminal for
    this process, and the caller reaps it.
    """
    said: list[str] = []
    while True:
        for ev in _read_events(entry):
            kind = str(ev.get("type") or "")
            if kind == "system" and ev.get("subtype") == "init":
                entry["cli_session_id"] = str(ev.get("session_id") or "")
                # THE READBACK. init is the CLI's own statement of the tool list
                # and the MCP servers it constructed, and it is the only
                # trustworthy one — the model's account of its own tools was
                # observed to be a hallucination (it recited the standard
                # fifteen at a session whose init said "tools":[]). Recorded on
                # the entry so `thinker` can show it and so a human can check
                # the promise rather than believe it.
                tools = ev.get("tools")
                entry["tools"] = sorted(str(t) for t in tools) \
                    if isinstance(tools, list) else []
                servers = ev.get("mcp_servers")
                entry["mcp_servers"] = [
                    str((s or {}).get("name") if isinstance(s, dict) else s)
                    for s in servers] if isinstance(servers, list) else []
            elif kind == "rate_limit_event":
                # A REFUSED WINDOW IS NOT A HANG, AND MUST NOT LOOK LIKE ONE.
                # Observed on a real turn: a five_hour window with
                # overageStatus "rejected". Without this the room would sit on
                # a spinner until the 180-second turn timeout and then report
                # something generic about not answering — sending the human to
                # look at the dashboard when the answer is "your allowance is
                # out until it resets". Recorded rather than raised: the CLI may
                # still complete the turn, and the note is only used if it does
                # not.
                info = ev.get("rate_limit_info")
                info = info if isinstance(info, dict) else {}
                if str(info.get("status") or "") not in ("", "allowed"):
                    entry["rate_limited"] = (
                        f"the {info.get('rateLimitType') or 'usage'} limit is "
                        f"{info.get('status')}"
                        + (" and overage is off for this org"
                           if str(info.get("overageStatus")) == "rejected" else ""))
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        said.append(str(block.get("text") or ""))
            elif kind == "result":
                text = str(ev.get("result") or "").strip() or "\n".join(
                    s for s in said if s.strip()).strip()
                subtype = str(ev.get("subtype") or "")
                out = {"cost": ev.get("total_cost_usd"), "text": text,
                       "subtype": subtype, "tokens": _tokens(ev),
                       "model": _model_of(ev)}
                if ev.get("is_error") or subtype != "success" or not text:
                    # The CLI's OWN words, not a generic sentence. The two
                    # failures a human actually meets here are an expired login
                    # and the --max-budget-usd ceiling, and both say so in this
                    # field; "the model returned nothing" sends them looking in
                    # the wrong place. A budget stop is also terminal for the
                    # process, so the caller must reap rather than ask again.
                    out["dead"] = subtype not in ("", "success")
                    out["ok"] = False
                    out["error"] = (f"the thinking session ended as "
                                    f"{subtype or 'no result'}"
                                    + (f": {text[:400]}" if text else ""))
                    return out
                out["ok"] = True
                return out
        code = entry["proc"].poll()
        if code is not None:
            # Give the last flush a chance to land before calling it dead: the
            # process can write its result and exit inside one poll interval.
            for ev in _read_events(entry):
                if str(ev.get("type") or "") == "result":
                    return {"ok": False, "dead": True,
                            "error": "the thinking session ended: "
                                     + str(ev.get("result") or "")[:400]}
            return {"ok": False, "dead": True,
                    "error": f"the thinking session exited ({code}) without "
                             "answering — nothing was written either way"}
        if time.monotonic() >= deadline:
            limited = entry.get("rate_limited")
            return {"ok": False, "dead": True,
                    "error": (f"no answer — {limited}. Nothing was lost; try "
                              "again once the window resets." if limited else
                              f"the thinking session did not answer within "
                              f"{int(TURN_TIMEOUT_S)}s and was stopped")}
        time.sleep(POLL_S)


def _resume_failed(entry: dict, got: dict) -> bool:
    """Did this turn fail because we tried to continue a session that is gone?

    THE RULE IS DELIBERATELY CRUDE: a process spawned with --resume that dies
    before landing a single successful turn is treated as a failed resume,
    whatever it said on the way out.

    A first attempt matched on wording — "no conversation", "session not found"
    — and it did not fire against the real thing. A deliberately corrupted
    session id produced `error_during_execution` and nothing else: the CLI has
    no machine-readable "that session is gone", and the sentence it chooses is
    not ours to depend on. The same attempt also gated on ``turns``, which
    counts BILLED turns and had already been incremented by the failed attempt's
    own token usage, so the guard disarmed itself.

    So: ``ok_turns`` (successful turns only), and no word matching. Once one
    turn has landed the session demonstrably exists and a later failure is a
    real failure that must never be laundered into a silent restart. Before
    that, the cost of a false positive is one replayed conversation and the cost
    of a false negative is a room that is permanently broken for the user whose
    session store was pruned.
    """
    return bool(entry.get("resumed")) and not entry.get("ok_turns") \
        and bool(got.get("dead"))


def ask(root, session_id: int, system: str, turns: list[dict], *,
        persist: bool = True, timeout: float = TURN_TIMEOUT_S,
        detail: str = "", tag: str = "") -> dict:
    """One turn with the thinking partner. Text in, text out, nothing written.

    ``persist`` is the difference between the ROOM and a ONE-SHOT. The room is
    one process held open across messages, so a brainstorm is a conversation
    rather than forty independent questions. A synthesis is the one-shot: a
    single question over the whole session, asked under a different system
    prompt, whose answer must not land in the middle of what the human is
    reading — and which must be safe to press twice at once.

    Returns the adapters' shared shape — ``{ok, text, model, seconds,
    estimated_usd}`` or ``{ok: False, error}`` — because a failure here is
    RETURNED rather than raised: the caller has already stored the human's
    sentence and must not lose it to a CLI that would not start.
    """
    started = time.monotonic()
    ready = available(root)
    if not ready["available"]:
        return {"ok": False, "error": ready["reason"], "seconds": 0.0,
                "estimated_usd": 0.0, "runner": ready["runner"]}
    runner = runner_for(root)
    ceiling = _ceiling(root)
    key = (_pkey(root), int(session_id))

    with _lock:
        entry = _live.get(key) if persist else None
    if entry is not None and not _usable(entry, system):
        _reap(key, entry)
        entry = None
    if entry is not None and ceiling and entry["spent_usd"] >= ceiling:
        # The CLI's own --max-budget-usd already bounds one process; this bounds
        # the CONVERSATION, which outlives any one of them. Refused rather than
        # silently respawned, because a respawn is exactly how a per-process
        # ceiling gets laundered into no ceiling at all.
        return {"ok": False, "seconds": 0.0, "estimated_usd": 0.0,
                "runner": runner.name,
                "error": f"this brainstorm has spent ${entry['spent_usd']:.2f} "
                         f"of its ${ceiling:.2f} ceiling — raise "
                         "brainstorm.max_usd, or carry on in a new session"}
    fresh = entry is None
    if fresh:
        if persist:
            _evict_if_crowded()
        entry = _start(root, session_id, runner, system, turns,
                       persist=persist, tag=tag)
        if isinstance(entry, dict) and entry.get("failed"):
            return {"ok": False, "error": entry["failed"], "seconds": 0.0,
                    "estimated_usd": 0.0, "runner": runner.name}

    got, cost = _turn(root, key, entry, turns, session_id, timeout, detail,
                      persist)

    # THE RESUME FALLBACK. A --resume can fail for reasons nothing in this
    # process controls: the CLI pruned its session store, the machine changed,
    # a version bump moved the format. Retried ONCE, from scratch, with the
    # whole transcript re-seeded — which is the behaviour that shipped before
    # resume existed, so the worst case is the old case. A resume that silently
    # started a BLANK session would be far worse than an honest replay: the
    # human would be talking to a partner that has forgotten the last hour and
    # would not be told.
    if _resume_failed(entry, got) and persist:
        _forget_resume(root, session_id)
        entry = _start(root, session_id, runner, system, turns,
                       persist=persist, tag=tag, allow_resume=False)
        if isinstance(entry, dict) and entry.get("failed"):
            return {"ok": False, "error": entry["failed"], "seconds": 0.0,
                    "estimated_usd": round(cost, 4), "runner": runner.name}
        again, more = _turn(root, key, entry, turns, session_id, timeout,
                            detail, persist)
        got, cost = again, cost + more
        got["replayed"] = True

    seconds = round(time.monotonic() - started, 2)
    if not got.get("ok"):
        return {"ok": False,
                "error": str(got.get("error")
                             or f"the thinking session answered nothing "
                                f"({got.get('subtype') or 'no result'})")[:400],
                "seconds": seconds, "estimated_usd": round(cost, 4),
                "runner": runner.name}
    return {"ok": True, "text": got["text"],
            "model": got.get("model") or ready["model"],
            "runner": runner.name, "seconds": seconds,
            "estimated_usd": round(cost, 4),
            "resumed": bool(entry.get("resumed")),
            "replayed": bool(got.get("replayed")),
            "cli_session_id": entry.get("cli_session_id", "")}


def _start(root, session_id: int, runner, system: str, turns: list[dict], *,
           persist: bool, tag: str, allow_resume: bool = True):
    """Spawn, resuming the real CLI conversation where that is possible.

    Returns the entry, or ``{"failed": reason}`` — a sentinel rather than an
    exception because every caller of this turns it straight into the shared
    result shape, and the human's message is already stored by then.

    RESUME NEEDS TWO THINGS TO AGREE, not one: the CLI still has the session,
    and WE still know which turn it last heard. The second is the part people
    forget — resuming a session and then re-sending the whole transcript would
    duplicate the conversation inside the model's context, which reads as the
    partner going strange rather than as a bug. So the sidecar's turn mark has
    to be findable in the current window; if it is not (the 40-turn window slid
    past it), this replays instead, which is correct and merely more expensive.
    """
    note = _read_sidecar(root, session_id) if (persist and allow_resume) else {}
    resume, seeded = "", []
    if note.get("cli_session_id") and note.get("runner") == runner.name \
            and note.get("system_sha") == _sha(system):
        at = _resume_point(turns, note.get("last_turn") or {})
        if at is not None:
            resume = str(note["cli_session_id"])
            seeded = list(turns[:at + 1])
    try:
        entry = _spawn(root, session_id, runner, system, register=persist,
                       tag=tag, resume=resume, pads=persist)
    except Unavailable as exc:
        return {"failed": str(exc)}
    entry["sent"] = seeded
    return entry


def _forget_resume(root, session_id: int) -> None:
    """Drop the resume marker. Called when the CLI could not honour it, so the
    next message does not pay for the same failed resume again."""
    note = _read_sidecar(root, session_id)
    note.pop("cli_session_id", None)
    note.pop("last_turn", None)
    _write_sidecar(root, session_id, note)


def _turn(root, key, entry: dict, turns: list[dict], session_id: int,
          timeout: float, detail: str, persist: bool) -> tuple[dict, float]:
    """Deliver one message and read back one reply. Returns (result, cost)."""
    # Two browser tabs on one session would otherwise interleave two questions
    # on one pipe and each read the other's answer.
    with entry["turn_lock"]:
        delta = _delta(entry, list(turns))
        if not delta:
            delta = list(turns[-1:]) or [{"role": "user", "content": ""}]
        try:
            entry["stdin"].write(_user_msg(_as_prompt(delta)).encode("utf-8"))
            entry["stdin"].flush()
        except (OSError, AttributeError, ValueError) as exc:
            _reap(key, entry)
            return ({"ok": False, "dead": True,
                     "error": f"the thinking session's channel closed ({exc}) — "
                              "say it again and a fresh one starts"}, 0.0)
        got = _collect(entry, time.monotonic() + max(5.0, float(timeout)))
        entry["last_at"] = time.monotonic()
        cost = float(got.get("cost") or 0.0)
        if cost or got.get("tokens"):
            entry["spent_usd"] = float(entry["spent_usd"]) + cost
            entry["turns"] = int(entry["turns"]) + 1
            # THE LEDGER, because a thinking session that spends silently is
            # exactly what spend.py exists to prevent. kind="agent" bills to the
            # subscription side, which is what this is: the CLI's
            # total_cost_usd is the API-equivalent price of a run a plan already
            # covers, and summing it into real money is the mistake
            # spend.totals was fixed to stop making.
            _spend.record(root, cost, kind="agent",
                          model=str(got.get("model") or ""),
                          tokens=got.get("tokens") or {},
                          detail=(detail
                                  or f"brainstorm session {session_id} turn"))
        if got.get("ok"):
            # SUCCESSFUL turns, counted apart from billed ones. `turns` goes up
            # whenever tokens were spent, which includes a resume that failed on
            # arrival — gating the resume fallback on that number disarmed it.
            entry["ok_turns"] = int(entry.get("ok_turns") or 0) + 1
            entry["sent"] = list(turns)
            if persist and turns:
                # The resume marker, written AFTER the turn landed rather than
                # before it. A marker for a turn the CLI never actually heard
                # would make the next resume skip a message the human sent.
                _write_sidecar(root, session_id, {
                    "cli_session_id": entry.get("cli_session_id") or "",
                    "runner": entry.get("runner") or "",
                    "system_sha": _sha(entry.get("system") or ""),
                    "last_turn": _mark(turns[-1]),
                    "turns": int(entry["turns"]), "ts": time.time()})
        if not persist or got.get("dead"):
            _reap(key, entry)
    return got, cost
