"""The console's director: one persistent, full-capability Claude Code session.

WHAT THIS REPLACES AND WHY. A console message used to become a work item
dispatched to a fresh seat-worker process: a switchboard prompt ("answer and
route, ten tool calls is a bug"), the director seat's lanes, and — because
every message was a new process — no memory of the message before it. The human's verdict on that, verbatim enough: it deflects,
it cannot investigate, and they end up opening a terminal and running `claude`
in the project to get a director that works. This module is that terminal
session, wired behind the console.

So the design is stated by what it is equal to: what you get by running
`claude` in the game project yourself.

  * FULL CAPABILITY. runners._claude_director_args — the dispatch tool set
    (Read/Edit/Write/Glob/Grep/Bash) plus the whole builders-gate MCP server,
    with the director framing APPENDED to the stock system prompt rather than
    replacing it.
  * SEATLESS. No BGATE_SEAT in its environment, which is precisely what a
    human-started top-level session looks like to the hook: lane enforcement
    softens to the machine's BGATE_DIRECTOR_MODE (default `collide`) instead
    of hard director-lanes-only. It still carries BGATE_ROOT, so containment
    decisions know which project it belongs to.
  * ONE CONVERSATION. One process per project, stdin held open between turns,
    resumed by the CLI's own session id across dashboard restarts (sidecar
    marker, same crude-and-honest fallback rules as brainsession: a resume
    that dies before its first successful turn is treated as failed and the
    session restarts fresh, reseeded with the recent transcript).
  * A CHAT IS A CHAT. A message is a line in .bgate/console/chat.jsonl and
    nothing else — no work item, no lineage stamp, no per-turn dispatch row.
    The transcript records what a terminal shows: what you said, what it said,
    and which tools it called on the way. Work appears on the board only when
    the director actually files it with queue_add.
  * ONE WORK-ITEM PATH SURVIVES, and only because it is not a chat: followup.py
    escalates a stuck item to the director as a real row it must settle.
    ``submit`` is that path.

WHAT BOUNDS IT: a turn timeout generous enough for real investigation, and the
kill switch (dispatch.kill_all reaches this module the same way it reaches
brainsession). No money ceiling — this product does not meter spend, and the
one budget that exists is the balance on the user's own provider account.

brainsession deliberately never imports dispatch so the read-only room cannot
drift into holding the dispatcher. This module has no such rule — it IS the
dispatcher's capability class — so it reuses dispatch's environment scrub
rather than growing a third copy.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from bgate_core.board import activity as _activity
from bgate_core.board import queue as _queue
from bgate_core.store import settings as _settings
from bgate_ui.agents import runners as _runners

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# project root (resolved) -> the live session entry.
_live: dict[str, dict] = {}
_lock = threading.Lock()

# One turn may run this long before the session is treated as wedged. Generous
# on purpose: "investigate why the last three gameplay items failed" is a real
# turn here, and the whole point over the switchboard is that it MAY take ten
# minutes of tool calls. A wedged process still dies — silently absorbing every
# later message is the one thing worse than a slow answer.
TURN_TIMEOUT_S = float(os.environ.get("BGATE_CONSOLE_TURN_S") or 15 * 60)

# A session nobody has spoken to for this long is reaped. Costs the next
# message a --resume, loses nothing: the conversation is the CLI's own session
# plus the work items on the board.
IDLE_S = int(os.environ.get("BGATE_CONSOLE_IDLE_S") or 60 * 60)

POLL_S = 0.15

FALLBACK_MODEL = "opus"

# Appended to the stock system prompt — the framing, not the capability. The
# capability is the argv. This is a session-start prompt and nothing more: who
# you are, which game this is, who the seats are and what each one can actually
# call. Everything else a normal `claude` session already knows.
DIRECTOR_SYSTEM = (
    "You are the DIRECTOR of this game project, in a persistent session behind "
    "the project dashboard's chat. You are a full session in the project "
    "directory — read files, search, run commands — and you also hold the "
    "builders-gate MCP toolset.\n"
    "\n"
    "Substantial game work goes to a seat: queue_add(seat, title, brief), or "
    "queue_add_chain when the pieces depend on each other. Nothing dispatches "
    "unless the dashboard is running, so say so if it is not. Corrections to a "
    "run already in flight are agent_steer(item_id, text), not a new item. A "
    "failed item is yours to read and queue_reopen with a reason.\n"
    "\n"
    "Answer the human in plain prose, and lead with the answer."
)


def _game_facts(root) -> str:
    """The one paragraph that says WHICH GAME this is. Best effort: a project
    whose row or bible cannot be read still gets a working director."""
    lines = []
    try:
        from bgate_core.store import project as _project

        row = _project.get(root)
        name = str(row.get("name") or "").strip() or Path(root).name
        pitch = str(row.get("pitch") or "").strip()
        kind = str(row.get("dimension") or "").strip()
        lines.append(f"GAME: {name}" + (f" — {pitch}" if pitch else "")
                     + (f" ({kind})" if kind else ""))
    except Exception:
        lines.append(f"GAME: {Path(root).name}")
    try:
        from bgate_core.design import bible as _bible

        seen = _bible.overview(root)
        for key in ("pillars", "loop"):
            titles = [str(s.get("title") or "") for s in (seen.get(key) or [])]
            titles = [t for t in titles if t][:6]
            if titles:
                lines.append(f"{key.upper()}: " + "; ".join(titles))
    except Exception:
        pass
    lines.append("bible_read gives you the full text of any of that.")
    return "\n".join(lines)


def _seat_table(root) -> str:
    """The seats, their missions, their write lanes and their tool surfaces.

    The toolset half is not decoration: a seat's MCP registry is trimmed to the
    crafts it practises (bgate_core.store.modules.SEAT_CRAFTS), so filing image work
    with the audio seat hands it a brief it has no tool to do.
    """
    from bgate_core.store import modules as _modules
    from bgate_core.board import seats as _seats

    try:
        table = _seats.roles_for(root)
    except Exception:
        table = dict(_seats.DEFAULT_SEATS)
    out = []
    for role, cfg in table.items():
        crafts = _modules.SEAT_CRAFTS.get(role)
        if crafts is None:
            tools = "every builders-gate tool"
        else:
            prefixes = [p for craft in crafts
                        for p in _modules.CRAFTS.get(craft, ())]
            tools = ", ".join(sorted(set(prefixes))) + "* plus the shared spine"
        mission = " ".join(str(cfg.get("mission") or "").split())
        lanes = ", ".join(cfg.get("write_globs") or []) or "(no write lane)"
        out.append(f"  {role} — {mission}\n"
                   f"    writes: {lanes}\n"
                   f"    tools: {tools}")
    return ("SEATS you can file work for (queue_add), and what each one can "
            "actually call:\n" + "\n".join(out))


def system_prompt(root) -> str:
    """The session-start prompt: the framing, the game, the seats."""
    return (f"{DIRECTOR_SYSTEM}\n\n{_game_facts(root)}\n\n{_seat_table(root)}")


class Unavailable(RuntimeError):
    """No director session can start here, and the reason is a sentence."""


def _pkey(root) -> str:
    try:
        return str(Path(root).resolve())
    except OSError:
        return str(root)


def _home(root) -> Path:
    path = Path(root) / ".bgate" / "console"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path(root) -> Path:
    """The session's raw NDJSON, appended across respawns."""
    return _home(root) / "director.log"


def _sidecar(root) -> Path:
    return _home(root) / "director-session.json"


def _read_sidecar(root) -> dict:
    try:
        data = json.loads(_sidecar(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_sidecar(root, data: dict) -> None:
    """Best effort: a session that cannot write its resume marker still works —
    it restarts fresh next time instead of continuing."""
    try:
        _sidecar(root).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def forget(root) -> None:
    """Drop the resume marker, so the next message starts a fresh conversation.
    What "clear the console" means for this module."""
    note = _read_sidecar(root)
    note.pop("cli_session_id", None)
    _write_sidecar(root, note)


# ---------------------------------------------------------------------------
# The transcript — what the chat pane renders
# ---------------------------------------------------------------------------
#
# One line per message in .bgate/console/chat.jsonl: {n, ts, role, text, tool}.
# `role` is user | assistant | tool | error. That is the whole store — a chat
# message is not a work item, has no status, and settles nothing.

_seq: dict[str, int] = {}
_chat_lock = threading.Lock()

# What one poll may carry back. A conversation longer than this is read from
# the file on disk, which is the durable copy.
CHAT_LIMIT = 400


def chat_path(root) -> Path:
    return _home(root) / "chat.jsonl"


def _read_chat(root) -> list[dict]:
    out = []
    try:
        with open(chat_path(root), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    out.append(ev)
    except OSError:
        return []
    return out


def _post(root, role: str, text: str, tool: str = "") -> dict:
    """Append one message. Returns it, numbered."""
    key = _pkey(root)
    with _chat_lock:
        if key not in _seq:
            got = _read_chat(root)
            _seq[key] = max((int(m.get("n") or 0) for m in got), default=0)
        _seq[key] += 1
        entry = {"n": _seq[key], "ts": time.time(), "role": role,
                 "text": str(text or "")[:20000]}
        if tool:
            entry["tool"] = tool
        try:
            with open(chat_path(root), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # an unwritable transcript must not eat the turn
    return entry


def history(root, after: int = 0) -> dict:
    """The conversation since message ``after``, plus whether one is running."""
    msgs = [m for m in _read_chat(root) if int(m.get("n") or 0) > int(after)]
    live = status(root)
    return {"messages": msgs[-CHAT_LIMIT:],
            "running": bool(live.get("running")),
            # Why a running turn is producing nothing, when it is not the model
            # thinking: an overloaded API being retried, or a refused usage
            # window. Empty when the turn is simply working.
            "waiting": str(live.get("waiting") or ""),
            "live": bool(live.get("live")),
            "session_id": live.get("cli_session_id") or "",
            "model": _model_for(root)}


def send(root, text: str) -> dict:
    """Say one thing to the director. Posts the message and takes the turn in
    a thread — the poll shows the reply as it arrives."""
    said = _post(root, "user", text)
    threading.Thread(target=_run_chat_turn, args=(str(root), str(text)),
                     daemon=True, name="director-chat").start()
    return {"ok": True, "n": said["n"]}


def reset(root) -> dict:
    """Start a fresh conversation: stop the process, drop the resume marker,
    and archive the transcript beside itself rather than deleting it."""
    stop(root)
    forget(root)
    with _chat_lock:
        _seq.pop(_pkey(root), None)
        path = chat_path(root)
        try:
            if path.exists():
                path.replace(path.with_name(
                    f"chat-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
        except OSError:
            pass
    return {"ok": True}


def _chat_reseed(root, limit: int = 12, cap: int = 4000) -> str:
    """The transcript tail, as the context a NON-resumable restart is seeded
    with. The old work-item path rebuilt this from the console rows; the chat
    path passed "" — so a stale CLI session id meant a fresh session answering
    'do the second option' mid-conversation with zero memory of the options
    and no disclosure. chat.jsonl holds exactly the words that fix that."""
    try:
        msgs = [m for m in _read_chat(root)
                if m.get("role") in ("user", "assistant")][-limit:]
        text = "\n".join(f"{m['role']}: {str(m.get('text') or '')[:400]}"
                         for m in msgs)
        return text[-cap:]
    except Exception:
        return ""      # a reseed we cannot build must not block the turn


def _run_chat_turn(root, text: str) -> None:
    def settle(reply: str, failed: bool) -> None:
        # A successful turn has already streamed its prose into the transcript
        # block by block; only a failure has something left to say.
        if failed:
            _post(root, "error", reply)

    try:
        _turn(root, text, _chat_reseed(root), settle, record=True)
    except Unavailable as exc:
        _post(root, "error", str(exc))
    except Exception as exc:
        _post(root, "error", f"the director session crashed: "
                             f"{type(exc).__name__}: {exc}")


def _setting(root, key: str, fallback):
    try:
        value = _settings.get(root, key)
    except Exception:
        return fallback
    return fallback if value is None or value == "" else value


def _model_for(root) -> str:
    """Named, never inherited — same rule as dispatch and brainsession, same
    measured reason (see brainsession.FALLBACK_MODEL)."""
    return str(_setting(root, "console.model", FALLBACK_MODEL)
               or "").strip() or FALLBACK_MODEL


def _kill_tree(pid: int) -> None:
    """Kill the CLI and its children (the MCP server it spawned).

    POSIX kills the process GROUP, which is why _spawn sets start_new_session:
    kill(pid) alone leaves the MCP child holding the pipe — the exact orphan
    the Windows /T flag exists to prevent.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, creationflags=_NO_WINDOW,
                           timeout=15)
        else:
            try:
                os.killpg(os.getpgid(pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                os.kill(pid, 9)
    except Exception:
        pass


def _environ(root) -> dict:
    """The dashboard's environment, scrubbed the way a dispatch is, then marked
    as the seatless top-level session it is.

    The scrub is dispatch's own (imported, not copied — this module is the same
    capability class, so brainsession's isolation rule does not apply). On top:
    no BGATE_SEAT — the hook treats a seatless session as the human-started
    director, with lane enforcement at the machine's BGATE_DIRECTOR_MODE — and
    BGATE_ROOT so containment knows whose session this is.
    """
    from bgate_ui.agents import dispatch as _dispatch

    env = _dispatch._scrubbed_environ()
    for name in ("BGATE_SEAT", "BGATE_WORK_ITEM", "BGATE_LOCK_OWNER"):
        env.pop(name, None)
    env["BGATE_ROOT"] = str(root)
    env["BGATE_ACTOR"] = "console:director"
    return env


def _spawn(root, resume: str = "") -> dict:
    exe = _runners.find_claude()
    if not exe:
        raise Unavailable(
            "claude CLI not found on PATH — the console director is a real "
            "session, so it needs the CLI itself rather than an API key")
    path = log_path(root)
    handle = open(path, "ab")
    handle.write((json.dumps({"type": "bgate_console_start",
                              "resumed": bool(resume),
                              "ts": time.time()}) + "\n").encode("utf-8"))
    handle.flush()
    args = _runners._claude_director_args(
        exe, system=system_prompt(root), model=_model_for(root),
        resume=resume)
    try:
        proc = subprocess.Popen(
            args, cwd=str(root), env=_environ(root),
            stdin=subprocess.PIPE, stdout=handle, stderr=handle,
            creationflags=_NO_WINDOW,
            start_new_session=(sys.platform != "win32"))
    except OSError as exc:
        handle.close()
        raise Unavailable(f"could not start claude: {exc}") from exc
    entry = {"proc": proc, "handle": handle, "stdin": proc.stdin,
             "log": str(path), "scan_pos": handle.tell(), "rem": b"",
             "turns": 0, "ok_turns": 0,
             "cli_session_id": str(resume or ""), "resumed": bool(resume),
             "current_item": 0, "busy": False, "record": "", "says": [],
             "waiting": "",
             "started_at": time.monotonic(), "last_at": time.monotonic(),
             "turn_lock": threading.Lock()}
    with _lock:
        _live[_pkey(root)] = entry
    return entry


def _reap(key: str, entry: dict) -> None:
    """Stop the session process and let go of its handles. Safe twice."""
    try:
        entry["stdin"].close()
    except Exception:
        pass
    try:
        proc = entry.get("proc")
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


def stop(root) -> dict:
    """End this project's console session. The conversation is not lost: the
    turns are work items and the CLI session resumes on the next message."""
    key = _pkey(root)
    with _lock:
        entry = _live.get(key)
    if entry is None:
        return {"ok": True, "stopped": False}
    _reap(key, entry)
    return {"ok": True, "stopped": True}


def stop_all(root=None) -> dict:
    """The kill switch's reach into this module — same contract as
    brainsession.stop_all, for the same reason: a session holding a pipe that
    survives the one button meaning 'stop everything' is not acceptable, and
    this one, unlike the brainstorm partner, CAN act."""
    want = _pkey(root) if root is not None else None
    with _lock:
        targets = [(k, e) for k, e in _live.items()
                   if want is None or k == want]
    for key, entry in targets:
        _reap(key, entry)
    return {"stopped": len(targets)}


def status(root) -> dict:
    """What the poll needs: whether a session is up, whether a turn is in
    flight, which item (if it is an escalation) and its last words so far."""
    with _lock:
        entry = _live.get(_pkey(root))
        if entry is None:
            note = _read_sidecar(root)
            return {"live": False, "running": False, "current_item": 0,
                    "thinking": "",
                    "cli_session_id": str(note.get("cli_session_id") or ""),
                    "turns": 0}
        says = list(entry.get("says") or [])
        waiting = str(entry.get("rate_limited") or entry.get("waiting") or "")
        return {"live": entry["proc"].poll() is None,
                "running": bool(entry.get("busy")),
                "current_item": int(entry.get("current_item") or 0),
                "thinking": (says[-1][:400] if says else waiting[:400]),
                "waiting": waiting[:400],
                "cli_session_id": str(entry.get("cli_session_id") or ""),
                "turns": int(entry.get("turns") or 0)}


# ---------------------------------------------------------------------------
# One turn
# ---------------------------------------------------------------------------

def _user_msg(text: str) -> str:
    return json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": text}]}}) + "\n"


def _read_events(entry: dict) -> list[dict]:
    """Forward from a byte cursor, partial last line carried over — the same
    tail-the-log read every stream consumer in this repo uses."""
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
            continue  # the CLI also writes plain stderr into this file
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _tokens(ev: dict) -> dict:
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


def _tool_note(block: dict) -> str:
    """One line saying what a tool call is ABOUT — the path, the command, the
    pattern — the way a terminal session shows it. Falls back to compact JSON
    rather than to nothing, because an unnamed tool call reads as a stall."""
    args = block.get("input")
    args = args if isinstance(args, dict) else {}
    for key in ("file_path", "path", "command", "pattern", "title", "query",
                "text", "seat", "prompt"):
        if args.get(key):
            return str(args[key])[:400]
    if not args:
        return ""
    try:
        return json.dumps(args)[:400]
    except (TypeError, ValueError):
        return ""


def _collect(entry: dict, deadline: float) -> dict:
    """Drain the log until this turn's `result` event, or fail saying why.

    Appends assistant prose to entry["says"] AS IT ARRIVES — that list is what
    the console's poll shows as the live reply while the turn runs.
    """
    while True:
        for ev in _read_events(entry):
            kind = str(ev.get("type") or "")
            if kind == "system" and ev.get("subtype") == "init":
                entry["cli_session_id"] = str(ev.get("session_id") or "")
            elif kind == "system" and ev.get("subtype") == "api_retry":
                # AN OVERLOADED API IS NOT A HANG EITHER. Observed live: ten
                # retries against 529 `overloaded`, backing off to 33s each,
                # while the pane showed "working…" and nothing else. The CLI
                # may still land the turn, so this is recorded (and shown as
                # the live line) rather than raised.
                entry["waiting"] = (
                    f"the API is {ev.get('error') or 'unavailable'} "
                    f"({ev.get('error_status') or '?'}) — retrying, attempt "
                    f"{ev.get('attempt') or '?'} of "
                    f"{ev.get('max_retries') or '?'}")
            elif kind == "rate_limit_event":
                # A refused usage window must not read as a hang — see
                # brainsession._collect, where this was observed live.
                info = ev.get("rate_limit_info")
                info = info if isinstance(info, dict) else {}
                if str(info.get("status") or "") not in ("", "allowed"):
                    entry["rate_limited"] = (
                        f"the {info.get('rateLimitType') or 'usage'} limit is "
                        f"{info.get('status')}")
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = str(block.get("text") or "")
                        if text.strip():
                            entry["waiting"] = ""
                            entry["says"].append(text)
                            del entry["says"][:-12]
                            if entry.get("record"):
                                _post(entry["record"], "assistant", text)
                    elif block.get("type") == "tool_use" and entry.get("record"):
                        _post(entry["record"], "tool", _tool_note(block),
                              tool=str(block.get("name") or "tool"))
            elif kind == "result":
                text = str(ev.get("result") or "").strip() or "\n".join(
                    s for s in entry["says"] if s.strip()).strip()
                subtype = str(ev.get("subtype") or "")
                out = {"cost": ev.get("total_cost_usd"), "text": text,
                       "subtype": subtype, "tokens": _tokens(ev),
                       "model": _model_of(ev)}
                if ev.get("is_error") or subtype != "success" or not text:
                    out["dead"] = subtype not in ("", "success")
                    out["ok"] = False
                    out["error"] = (f"the director session ended as "
                                    f"{subtype or 'no result'}"
                                    + (f": {text[:400]}" if text else ""))
                    return out
                out["ok"] = True
                return out
        code = entry["proc"].poll()
        if code is not None:
            for ev in _read_events(entry):
                if str(ev.get("type") or "") == "result":
                    return {"ok": False, "dead": True,
                            "error": "the director session ended: "
                                     + str(ev.get("result") or "")[:400]}
            return {"ok": False, "dead": True,
                    "error": f"the director session exited ({code}) without "
                             "answering"}
        if time.monotonic() >= deadline:
            limited = entry.get("rate_limited") or entry.get("waiting")
            return {"ok": False, "dead": True,
                    "error": (f"no answer — {limited}. Try again once the "
                              "window resets." if limited else
                              f"the director session did not answer within "
                              f"{int(TURN_TIMEOUT_S)}s and was stopped")}
        time.sleep(POLL_S)


def _resume_failed(entry: dict, got: dict) -> bool:
    """Same crude-and-honest rule as brainsession._resume_failed, for the same
    measured reason: the CLI has no machine-readable 'that session is gone',
    so a --resume process that dies before one successful turn is a failed
    resume, whatever it said on the way out."""
    return bool(entry.get("resumed")) and not entry.get("ok_turns") \
        and bool(got.get("dead"))


def _ensure(root) -> dict:
    """The live session, or a fresh spawn resuming the sidecar's conversation."""
    key = _pkey(root)
    with _lock:
        entry = _live.get(key)
    if entry is not None:
        stale = (entry["proc"].poll() is not None
                 or time.monotonic() - entry["last_at"] > IDLE_S)
        if not stale:
            return entry
        _reap(key, entry)
    resume = str(_read_sidecar(root).get("cli_session_id") or "")
    return _spawn(root, resume=resume)


def submit(root, item_id: int, prompt: str, reseed_context: str = "") -> dict:
    """Take one turn that must settle a WORK ITEM — followup's escalation of a
    stuck run. The chat path is `send`; this one exists because an escalation
    is a row on the board, not a message in a conversation.
    """
    thread = threading.Thread(
        target=_run_item_turn, args=(str(root), int(item_id), prompt,
                                     str(reseed_context or "")),
        daemon=True, name=f"director-turn-{int(item_id)}")
    thread.start()
    return {"ok": True, "item_id": int(item_id)}


def _run_item_turn(root, item_id: int, prompt: str, reseed_context: str) -> None:
    def settle(text: str, failed: bool) -> None:
        _settle(root, item_id, text, failed=failed)

    try:
        _turn(root, prompt, reseed_context, settle, item_id=item_id)
    except Unavailable as exc:
        settle(str(exc), True)
    except Exception as exc:  # a crashed collector must never strand the row
        settle(f"the director session crashed: {type(exc).__name__}: {exc}",
               True)


def _turn(root, prompt: str, reseed_context: str, settle,
          *, item_id: int = 0, record: bool = False) -> None:
    """One turn through the session. ``settle(text, failed)`` is what to do
    with the answer — post it to the chat, or close a work item with it."""
    # A loop, not a single re-check: this thread may wait on the turn lock
    # while the previous turn kills the process (a timeout). The
    # lock and the process belong to ONE entry — carrying a fresh entry under
    # a dead entry's lock would let two turns interleave on the fresh pipe.
    for _attempt in range(3):
        entry = _ensure(root)
        with entry["turn_lock"]:
            if entry["proc"].poll() is not None:
                _reap(_pkey(root), entry)
                continue  # died while this turn waited; take a fresh one
            entry["current_item"] = int(item_id)
            entry["busy"] = True
            entry["record"] = str(root) if record else ""
            entry["says"] = []
            entry["waiting"] = ""
            got, cost = _deliver(root, entry, prompt)
            if _resume_failed(entry, got):
                # The conversation the sidecar pointed at is gone. Restart
                # fresh, reseeded, and say so in the reply rather than
                # silently forgetting the last hour.
                forget(root)
                _reap(_pkey(root), entry)
                fresh = _spawn(root, resume="")
                head = ("(The console session could not be resumed, so this "
                        "is a fresh one. Recent conversation, for context:)"
                        "\n\n" + reseed_context + "\n\n----\n\n"
                        ) if reseed_context else ""
                with fresh["turn_lock"]:
                    fresh["current_item"] = int(item_id)
                    fresh["busy"] = True
                    fresh["record"] = str(root) if record else ""
                    fresh["says"] = list(entry.get("says") or [])
                    got, _more = _deliver(root, fresh, head + prompt)
                    fresh["current_item"] = 0
                    fresh["busy"] = False
                entry = fresh
            else:
                entry["current_item"] = 0
                entry["busy"] = False
            if got.get("ok"):
                settle(str(got.get("text") or ""), False)
            else:
                settle(str(got.get("error") or "the director session "
                                               "answered nothing"), True)
            if got.get("dead"):
                _reap(_pkey(root), entry)
            return
    settle("the director session kept dying before this turn could start", True)


def _deliver(root, entry: dict, prompt: str) -> tuple[dict, float]:
    """Write one user turn, read back one result. Returns (result, cost)."""
    try:
        entry["stdin"].write(_user_msg(prompt).encode("utf-8"))
        entry["stdin"].flush()
    except (OSError, AttributeError, ValueError) as exc:
        _reap(_pkey(root), entry)
        return ({"ok": False, "dead": True,
                 "error": f"the director session's channel closed ({exc}) — "
                          "say it again and a fresh one starts"}, 0.0)
    got = _collect(entry, time.monotonic() + max(30.0, TURN_TIMEOUT_S))
    entry["last_at"] = time.monotonic()
    cost = float(got.get("cost") or 0.0)
    if cost or got.get("tokens"):
        entry["turns"] = int(entry["turns"]) + 1
    if got.get("ok"):
        entry["ok_turns"] = int(entry.get("ok_turns") or 0) + 1
        _write_sidecar(root, {
            "cli_session_id": entry.get("cli_session_id") or "",
            "turns": int(entry["turns"]), "ts": time.time()})
    if got.get("dead") and entry["proc"].poll() is None:
        # A turn declared dead (timeout) over a process still running: kill it,
        # or the next turn interleaves with this one's late output.
        _kill_tree(entry["proc"].pid)
    return got, cost


def _settle(root, item_id: int, text: str, failed: bool = False) -> None:
    """Close the turn's work item with the reply. Guarded: a row that cannot be
    settled is logged loudly rather than silently stranded in 'dispatched'."""
    try:
        _queue.complete(root, item_id, result=text, failed=failed)
    except Exception as exc:
        try:
            _activity.log(root, "console",
                          f"could not settle console turn #{item_id}: "
                          f"{type(exc).__name__}: {exc}", seat="director",
                          ref=str(item_id))
        except Exception:
            pass
