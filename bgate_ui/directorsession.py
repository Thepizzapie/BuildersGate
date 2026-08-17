"""The console's director: one persistent, full-capability Claude Code session.

WHAT THIS REPLACES AND WHY. A console message used to become a work item
dispatched to a fresh seat-worker process: a switchboard prompt ("answer and
route, ten tool calls is a bug"), the director seat's lanes, a per-item cost
ceiling, and — because every message was a new process — no memory of the
message before it. The human's verdict on that, verbatim enough: it deflects,
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
  * TURNS ARE STILL WORK ITEMS. console_say files the row exactly as before —
    the transcript, the DELEGATED-FROM lineage and the archive all read work
    items, and none of that should know or care what answered. What changed is
    who completes the row: the collector thread here settles it with the
    session's reply, instead of the agent calling queue_complete on itself.

WHAT BOUNDS IT, since the per-item ceilings deliberately do not apply: the
CLI's own --max-budget-usd per process, a session ceiling across respawns
(console.max_usd — a refusal is delivered as the REPLY, not as a failure), a
turn timeout generous enough for real investigation, and the kill switch
(dispatch.kill_all reaches this module the same way it reaches brainsession).

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

from bgate_core import activity as _activity
from bgate_core import queue as _queue
from bgate_core import settings as _settings
from bgate_core import spend as _spend
from bgate_ui import runners as _runners

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
# capability is the argv; this only has to stop the two observed failure modes:
# the deflection ("I can only see the board") and the switchboard reflex of
# routing without understanding.
DIRECTOR_SYSTEM = (
    "You are the DIRECTOR of this game project, in a persistent session behind "
    "the project dashboard's console. The human who owns the project talks to "
    "you there; your final message each turn is rendered as your reply in that "
    "chat. You also hold the full builders-gate MCP toolset (the board, the "
    "bible, seats, steering) alongside your normal tools.\n"
    "\n"
    "You are a full session in the project directory — read files, search, run "
    "commands, check logs and the board. Never claim you can only see the "
    "board or that something is not your problem: if the answer is in the "
    "project, go get it. When something is genuinely out of reach, say exactly "
    "what and why.\n"
    "\n"
    "Division of labour: investigate, decide, arbitrate and answer yourself; "
    "small direct fixes are fine. Substantial game work goes on the board — "
    "queue_add(seat, title, brief) with a self-contained brief, or "
    "queue_add_chain when pieces depend on each other. Every brief you file "
    "for a console ask MUST start with the DELEGATED-FROM line the turn gives "
    "you — it is the only durable record of where the work came from.\n"
    "\n"
    "Corrections to work already running are steered, not re-queued: "
    "queue_list(status='dispatched') to see who is live, then "
    "agent_steer(item_id, text). Failed items are yours to move: read the "
    "item's result and log, work out what actually went wrong, then "
    "queue_reopen with a reason the next agent can act on — do not report a "
    "failure back to the human as a dead end when you can redispatch it.\n"
    "\n"
    "Answer the human in plain prose, and lead with the answer."
)


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


def _ceiling(root) -> float:
    try:
        return max(0.0, float(_setting(root, "console.max_usd", 15.0)))
    except (TypeError, ValueError):
        return 15.0


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
    from bgate_ui import dispatch as _dispatch

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
        exe, system=DIRECTOR_SYSTEM, model=_model_for(root),
        max_usd=_ceiling(root), resume=resume)
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
             "spent_usd": 0.0, "turns": 0, "ok_turns": 0,
             "cli_session_id": str(resume or ""), "resumed": bool(resume),
             "current_item": 0, "says": [],
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
    """What the console's poll needs to paint a live turn: whether a session is
    up, which item it is answering, and its last words so far."""
    with _lock:
        entry = _live.get(_pkey(root))
        if entry is None:
            note = _read_sidecar(root)
            return {"live": False, "current_item": 0, "thinking": "",
                    "cli_session_id": str(note.get("cli_session_id") or ""),
                    "spent_usd": 0.0, "turns": 0}
        says = list(entry.get("says") or [])
        return {"live": entry["proc"].poll() is None,
                "current_item": int(entry.get("current_item") or 0),
                "thinking": (says[-1][:400] if says else ""),
                "cli_session_id": str(entry.get("cli_session_id") or ""),
                "spent_usd": round(float(entry.get("spent_usd") or 0.0), 4),
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
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = str(block.get("text") or "")
                        if text.strip():
                            entry["says"].append(text)
                            del entry["says"][:-12]
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
            limited = entry.get("rate_limited")
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


def turn_prompt(text: str, turn_id: int) -> str:
    """The human's words, verbatim and FIRST, plus the one line of plumbing a
    turn needs: which turn this is, for the DELEGATED-FROM stamp. The stamp
    format is orchestrator.DELEGATED_FROM's, spelled out rather than imported —
    a routes import here would be upside down, and the format is load-bearing
    for the console graph either way (test-pinned in test_directorsession)."""
    return (f"{text}\n\n"
            f"(console turn #{turn_id} — any brief you file for this starts "
            f"with the line `DELEGATED-FROM: #{turn_id}`, then a blank line, "
            "then the brief.)")


def submit(root, item_id: int, prompt: str, reseed_context: str = "") -> dict:
    """Take one console turn, asynchronously. The item is already reserved;
    a collector thread settles it with the session's reply (or its failure).

    ``reseed_context`` is used ONLY when a resume fails and the conversation
    restarts fresh: it is the recent transcript, prepended so the new process
    is not answering with amnesia and pretending otherwise.
    """
    thread = threading.Thread(
        target=_run_turn, args=(str(root), int(item_id), prompt,
                                str(reseed_context or "")),
        daemon=True, name=f"director-turn-{int(item_id)}")
    thread.start()
    return {"ok": True, "item_id": int(item_id)}


def _run_turn(root, item_id: int, prompt: str, reseed_context: str) -> None:
    try:
        _turn(root, item_id, prompt, reseed_context)
    except Unavailable as exc:
        _settle(root, item_id, str(exc), failed=True)
    except Exception as exc:  # a crashed collector must never strand the row
        _settle(root, item_id,
                f"the director session crashed: {type(exc).__name__}: {exc}",
                failed=True)


def _turn(root, item_id: int, prompt: str, reseed_context: str) -> None:
    ceiling = _ceiling(root)
    # A loop, not a single re-check: this thread may wait on the turn lock
    # while the previous turn kills the process (timeout, budget stop). The
    # lock and the process belong to ONE entry — carrying a fresh entry under
    # a dead entry's lock would let two turns interleave on the fresh pipe.
    for _attempt in range(3):
        entry = _ensure(root)
        with entry["turn_lock"]:
            if entry["proc"].poll() is not None:
                _reap(_pkey(root), entry)
                continue  # died while this turn waited; take a fresh one
            spent = float(entry.get("spent_usd") or 0.0)
            if ceiling and spent >= ceiling:
                # Delivered as the REPLY, not as a failure: "I am out of
                # budget" is an answer the human can act on, and a failed chat
                # turn reads as the product breaking.
                _settle(root, item_id,
                        f"This console session has spent ${spent:.2f} of its "
                        f"${ceiling:.2f} ceiling (console.max_usd). Raise it "
                        "in Settings, or clear the console to start a fresh "
                        "session.")
                return
            entry["current_item"] = int(item_id)
            entry["says"] = []
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
                    fresh["says"] = list(entry.get("says") or [])
                    got, _more = _deliver(root, fresh, head + prompt)
                    fresh["current_item"] = 0
                entry = fresh
            else:
                entry["current_item"] = 0
            if got.get("ok"):
                _settle(root, item_id, str(got.get("text") or ""))
            else:
                _settle(root, item_id,
                        str(got.get("error") or "the director session "
                                                "answered nothing"),
                        failed=True)
            if got.get("dead"):
                _reap(_pkey(root), entry)
            return
    _settle(root, item_id,
            "the director session kept dying before this turn could start",
            failed=True)


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
        entry["spent_usd"] = float(entry["spent_usd"]) + cost
        entry["turns"] = int(entry["turns"]) + 1
        try:
            _spend.record(root, cost, kind="agent",
                          model=str(got.get("model") or ""),
                          tokens=got.get("tokens") or {}, seat="director",
                          detail="console director turn")
        except Exception:
            pass  # the ledger must not break the reply
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
