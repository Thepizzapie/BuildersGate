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
import uuid
from pathlib import Path

from bgate_core.board import activity as _activity
from bgate_core.board import gitwork as _gitwork
from bgate_core.board import queue as _queue
from bgate_core.store import settings as _settings
from bgate_ui.agents import runners as _runners

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# project root (resolved) -> the live session entry.
_live: dict[str, dict] = {}
_lock = threading.Lock()

# Dashboard-owned watches for dispatched work. These are deliberately outside
# either CLI session: switching the Director between Claude and Codex must not
# stop it following the agents it launched.
_board_watches: dict[tuple[str, int], str] = {}
_board_watch_lock = threading.Lock()

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
CODEX_APPROVAL_POLICY = "untrusted"
CODEX_SANDBOX = "read-only"

# Appended to the stock system prompt — the framing, not the capability. The
# capability is the argv. This is a session-start prompt and nothing more: who
# you are, which game this is, who the seats are and what each one can actually
# call. Everything else a normal `claude` session already knows.
DIRECTOR_SYSTEM = (
    "You are the DIRECTOR of this game project, in a persistent session behind "
    "the project dashboard's chat. Use the Builders Gate tools for board, "
    "project, and agent operations. Do not inspect dashboard internals, process "
    "lists, databases, local APIs, or CLI state as a fallback when a Builders "
    "Gate tool is denied or unavailable. Use shell commands only for project "
    "file inspection, builds, and tests requested by the human.\n"
    "\n"
    "Substantial game work goes to a seat: queue_add(seat, title, brief), or "
    "queue_add_chain when the pieces depend on each other. Nothing dispatches "
    "unless the dashboard is running, so say so if it is not. Corrections to a "
    "run already in flight are agent_steer(item_id, text), not a new item. A "
    "failed item is yours to read and queue_reopen with a reason. The dashboard "
    "board monitor follows every dispatched agent and posts its outcome here; "
    "do not make a one-time queue check and describe a newly queued item as "
    "idle or undispatched.\n"
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


def _dispatch_instruction(root) -> str:
    mode = str(_setting(root, "dispatch.mode", "structured"))
    return (
        "STRUCTURED: file independent work as separate queue_add calls so ready "
        "items batch across the available agent slots. Use queue_add_chain, or "
        "explicit dependencies, whenever one task consumes another task's output "
        "or must wait for the same locked file."
        if mode == "structured" else
        "CHAOS: file every available independent task immediately; the scheduler "
        "fills the agent cap and isolates every task in a git worktree. Dependencies "
        "still use queue_add_chain. Completed branches return to you for review and "
        "worktree_merge; resolve integration conflicts without dropping either side.")


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


def _session_for(root, runner: str) -> str:
    note = _read_sidecar(root)
    sessions = note.get("sessions")
    if isinstance(sessions, dict) and sessions.get(runner):
        return str(sessions[runner])
    # Compatibility with sidecars written before the console had two CLIs.
    return str(note.get("cli_session_id") or "") if runner == "claude" else ""


def _remember(root, **changes) -> dict:
    note = _read_sidecar(root)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(note.get(key), dict):
            note[key] = {**note[key], **value}
        else:
            note[key] = value
    note["ts"] = time.time()
    _write_sidecar(root, note)
    return note


def forget(root, runner: str = "") -> None:
    """Drop the resume marker, so the next message starts a fresh conversation.
    What "clear the console" means for this module."""
    note = _read_sidecar(root)
    note.pop("cli_session_id", None)
    sessions = note.get("sessions") if isinstance(note.get("sessions"), dict) else {}
    if runner:
        sessions.pop(runner, None)
    else:
        sessions = {}
    note["sessions"] = sessions
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
            "approvals": pending_approvals(root),
            **configuration(root),
            "usage": usage(root),
            "usage_bridge": usage_bridge(root)}


def pending_approvals(root) -> list[dict]:
    """Approval prompts emitted by the live Codex app-server connection."""
    with _lock:
        entry = _live.get(_pkey(root))
        rows = list((entry or {}).get("approvals", {}).values())
    return [{key: value for key, value in row.items()
             if not key.startswith("_")} for row in rows]


def decide_approval(root, approval_id: str, decision: str) -> dict:
    """Answer one app-server approval using its original JSON-RPC id."""
    with _lock:
        entry = _live.get(_pkey(root))
        row = ((entry or {}).get("approvals") or {}).get(str(approval_id))
    if entry is None or row is None:
        raise ValueError("that approval is no longer pending")
    allowed = [str(value) for value in row.get("available_decisions") or []]
    if decision not in allowed:
        raise ValueError("that decision is not available for this approval")
    result = _approval_result(row, decision)
    try:
        _codex_write(entry, {"id": row["_rpc_id"], "result": result})
    except (OSError, ValueError, KeyError) as exc:
        with _lock:
            entry.get("approvals", {}).pop(str(approval_id), None)
        raise ValueError("the Codex session is gone; send a message to "
                         "restart it") from exc
    with _lock:
        entry.get("approvals", {}).pop(str(approval_id), None)
        if not entry.get("approvals"):
            entry["waiting"] = ""
    return {"ok": True, "id": str(approval_id), "decision": decision}


def _approval_result(row: dict, decision: str) -> dict:
    kind = row["kind"]
    if kind in ("command", "file_change"):
        return {"decision": decision}
    elif kind == "permissions":
        requested = row.get("permissions") if isinstance(row.get("permissions"), dict) else {}
        granted = ({key: value for key, value in requested.items()
                    if value is not None} if decision.startswith("accept") else {})
        return {"permissions": granted,
                "scope": "session" if decision == "acceptForSession" else "turn"}
    return {"action": decision, "content": None, "_meta": None}


def _auto_approve(root, approval: dict) -> bool:
    """Auto-review only the surfaces already bounded by this orchestration."""
    if not _setting(root, "dispatch.codex_auto_approve", False):
        return False
    kind = approval.get("kind")
    if kind == "mcp":
        return str(approval.get("server") or "") == _runners.MCP_SERVER_NAME
    if kind != "command":
        return False
    cwd = str(approval.get("cwd") or "").strip()
    if not cwd:
        return True
    try:
        Path(cwd).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def send(root, text: str) -> dict:
    """Say one thing to the director. Posts the message and takes the turn in
    a thread — the poll shows the reply as it arrives."""
    said = _post(root, "user", text)
    threading.Thread(target=_run_chat_turn, args=(str(root), str(text)),
                     daemon=True, name="director-chat").start()
    return {"ok": True, "n": said["n"]}


_integration_inflight: set[tuple[str, int]] = set()


def _abandon_item(root, item_id: int, reason: str) -> None:
    """A Chaos branch past its integration ceiling fails the item, so the
    board shows a failure with a reason instead of a hold nobody labelled."""
    from bgate_core.board import queue as _queue
    try:
        if _queue.get(root, int(item_id)).get("status") == "integrating":
            _queue.complete(root, int(item_id), failed=True,
                            result="Chaos integration abandoned: " + reason)
    except LookupError:
        return
    _post(root, "error", f"item #{int(item_id)}: Chaos integration abandoned "
                         f"({reason})")
    report_monitored_item(root, item_id)


def request_integration(root, item_id: int) -> dict:
    """Hand a completed Chaos branch to the persistent Director session."""
    key = (_pkey(root), int(item_id))
    with _lock:
        if key in _integration_inflight:
            return {"ok": True, "already_running": True, "item_id": int(item_id)}
        _integration_inflight.add(key)
    dirty = _gitwork.dirty(root)
    if dirty.get("available") and dirty.get("dirty"):
        with _lock:
            _integration_inflight.discard(key)
        return {"ok": False, "item_id": int(item_id),
                "reason": "working branch is dirty; the scheduler re-offers "
                          "the branch once it is clean"}
    noted = _gitwork.note_integration_prompt(root, item_id)
    if noted.get("failed") or not noted.get("pending"):
        with _lock:
            _integration_inflight.discard(key)
        if noted.get("failed"):
            _abandon_item(root, item_id, str(noted.get("reason") or ""))
        return {"ok": False, "item_id": int(item_id), "abandoned": bool(
            noted.get("failed")), "reason": str(noted.get("reason") or "")}

    def run() -> None:
        prompt = (
            f"CHAOS integration is ready for work item #{int(item_id)}. "
            "Review the item result and branch diff. If it is sound, call "
            f"worktree_merge(item_id={int(item_id)}). If the merge conflicts, "
            "inspect both sides, correct the isolated branch without discarding "
            "unrelated work, and retry. Report the integration outcome.")

        def settle(reply: str, failed: bool) -> None:
            if failed:
                _post(root, "error", reply)

        try:
            _post(root, "tool", f"item #{int(item_id)} is ready for review",
                  tool="Chaos integration")
            _turn(str(root), prompt, _chat_reseed(root), settle, record=True)
        except Exception as exc:
            _post(root, "error", f"Chaos integration #{int(item_id)} failed: "
                                  f"{type(exc).__name__}: {exc}")
        finally:
            report_monitored_item(root, item_id)
            with _lock:
                _integration_inflight.discard(key)

    threading.Thread(target=run, daemon=True,
                     name=f"director-integrate-{int(item_id)}").start()
    return {"ok": True, "item_id": int(item_id)}


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
    runner = _runner_for(root)
    models = _read_sidecar(root).get("models")
    if isinstance(models, dict) and str(models.get(runner) or "").strip():
        return str(models[runner]).strip()
    if runner == "claude":
        return str(_setting(root, "console.model", FALLBACK_MODEL)
                   or "").strip() or FALLBACK_MODEL
    from bgate_ui.agents import codexmeta

    rows = codexmeta.snapshot().get("models") or []
    preferred = next((r for r in rows if r.get("default")), None)
    return str((preferred or (rows[0] if rows else {})).get("value") or "")


def _runner_for(root) -> str:
    name = str(_setting(root, "console.runner", "claude") or "claude").lower()
    return name if name in _runners.RUNNERS else "claude"


def _model_options(root) -> dict[str, list[dict]]:
    from bgate_core.runtime import modelcatalog
    from bgate_ui.agents import codexmeta

    claude = [{"value": value, "label": value.title()}
              for value in modelcatalog.AGENT_MODELS]
    codex = list(codexmeta.snapshot().get("models") or [])
    current = _read_sidecar(root).get("models")
    current = current if isinstance(current, dict) else {}
    for name, rows in (("claude", claude), ("codex", codex)):
        value = str(current.get(name) or "")
        if value and all(row.get("value") != value for row in rows):
            rows.insert(0, {"value": value, "label": value})
    return {"claude": claude, "codex": codex}


def configuration(root) -> dict:
    installed = _runners.available()
    return {"runner": _runner_for(root), "model": _model_for(root),
            "dispatch_mode": str(_setting(root, "dispatch.mode", "structured")),
            "runners": [
                {"value": key,
                 "label": "Claude Code" if key == "claude" else "Codex",
                 "installed": bool(row.get("installed"))}
                for key, row in installed.items()],
            "models": _model_options(root)}


def configure(root, runner: str, model: str) -> dict:
    runner = str(runner or "").strip().lower()
    model = str(model or "").strip()
    if runner not in _runners.RUNNERS:
        raise ValueError(f"unknown director runner '{runner}'")
    if not _runners.RUNNERS[runner].find():
        raise ValueError(f"{runner} CLI is not installed")
    if status(root).get("running"):
        raise ValueError("the director is working; switch after this turn")
    rows = _model_options(root).get(runner) or []
    if not model:
        preferred = next((r for r in rows if r.get("default")), None)
        model = str((preferred or (rows[0] if rows else {})).get("value") or "")
    if rows and model not in {str(r.get("value") or "") for r in rows}:
        raise ValueError(f"model '{model}' is not offered by {runner}")
    stop(root)
    _settings.set(root, "console.runner", runner)
    _settings.set(root, "console.model", model)
    _remember(root, models={runner: model})
    return {"ok": True, **configuration(root), "usage": usage(root)}


def _store_usage(root, runner: str, tokens: dict, context_limit=0) -> None:
    if not tokens:
        return
    if runner == "claude":
        used = sum(int(tokens.get(k) or 0)
                   for k in ("input", "cache_read", "cache_write"))
    else:
        used = int(tokens.get("input") or tokens.get("input_tokens") or 0)
    note = _read_sidecar(root)
    all_usage = note.get("usage") if isinstance(note.get("usage"), dict) else {}
    runner_usage = all_usage.get(runner) if isinstance(all_usage.get(runner), dict) else {}
    runner_usage["context"] = {"used": max(0, used),
                               "limit": max(0, int(context_limit or 0))}
    all_usage[runner] = runner_usage
    _remember(root, usage=all_usage)


def _rate_window(info: dict) -> tuple[str, dict]:
    kind = str(info.get("rateLimitType") or info.get("type") or "").lower()
    key = "five_hour" if "five" in kind or "5h" in kind else \
          "weekly" if "week" in kind or "seven" in kind else ""
    raw = info.get("usedPercent", info.get("utilization"))
    try:
        percent = float(raw)
        if percent <= 1:
            percent *= 100
        percent = max(0, min(100, round(percent)))
    except (TypeError, ValueError):
        percent = None
    row = {"status": str(info.get("status") or "")}
    if percent is not None:
        row["used_percent"] = percent
    reset = info.get("resetsAt", info.get("resetAt"))
    if reset is not None:
        row["resets_at"] = reset
    return key, row


def _store_rate_window(root, runner: str, info: dict) -> None:
    key, window = _rate_window(info)
    if not key:
        return
    note = _read_sidecar(root)
    all_usage = note.get("usage") if isinstance(note.get("usage"), dict) else {}
    runner_usage = all_usage.get(runner) if isinstance(all_usage.get(runner), dict) else {}
    runner_usage[key] = window
    all_usage[runner] = runner_usage
    _remember(root, usage=all_usage)


def usage(root) -> dict:
    runner = _runner_for(root)
    note = _read_sidecar(root)
    all_usage = note.get("usage") if isinstance(note.get("usage"), dict) else {}
    out = dict(all_usage.get(runner) or {})
    if runner == "codex":
        from bgate_ui.agents import codexmeta
        out.update(codexmeta.usage_for(_model_for(root)))
        context = dict(out.get("context") or {})
        if context and not context.get("limit"):
            context["limit"] = codexmeta.context_for(_model_for(root))
            out["context"] = context
    elif runner == "claude":
        from bgate_ui.agents import claudeusage
        out.update(claudeusage.usage())
    return {"context": out.get("context") or {},
            "five_hour": out.get("five_hour") or {},
            "weekly": out.get("weekly") or {}}


def usage_bridge(root) -> dict:
    if _runner_for(root) != "claude":
        return {"enabled": False, "has_snapshot": False,
                "needs_restart": False}
    from bgate_ui.agents import claudeusage
    return claudeusage.status()


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
    # To a FILE, beside the log: the framing plus the seat table is past
    # cmd.exe's argv limit on Windows (see _claude_director_args).
    system = system_prompt(root)
    system_file = _home(root) / "director-system.md"
    try:
        system_file.write_text(system, encoding="utf-8")
        args = _runners._claude_director_args(
            exe, system=system, model=_model_for(root),
            resume=resume, system_file=str(system_file))
    except OSError:
        args = _runners._claude_director_args(
            exe, system=system, model=_model_for(root), resume=resume)
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
             "root": str(root), "runner": "claude", "model": _model_for(root),
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
            runner = _runner_for(root)
            return {"live": False, "running": False, "current_item": 0,
                    "thinking": "",
                    "runner": runner,
                    "cli_session_id": _session_for(root, runner),
                    "turns": 0}
        says = list(entry.get("says") or [])
        waiting = str(entry.get("rate_limited") or entry.get("waiting") or "")
        return {"live": entry["proc"].poll() is None,
                "running": bool(entry.get("busy")),
                "current_item": int(entry.get("current_item") or 0),
                "thinking": (says[-1][:400] if says else waiting[:400]),
                "waiting": waiting[:400],
                "runner": str(entry.get("runner") or "claude"),
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


def _context_limit(ev: dict) -> int:
    """Read a provider-reported context window without assuming one by model."""
    wanted = {"context_window", "contextwindow", "model_context_window",
              "modelcontextwindow"}
    stack = [ev]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in wanted:
                    try:
                        return max(0, int(child or 0))
                    except (TypeError, ValueError):
                        pass
                if isinstance(child, (dict, list)):
                    stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
    return 0


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
                _store_rate_window(entry.get("root") or "", "claude", info)
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
                       "model": _model_of(ev),
                       "context_limit": _context_limit(ev)}
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


_codex_turn_locks: dict[str, threading.Lock] = {}


def _codex_lock(root) -> threading.Lock:
    key = _pkey(root)
    with _lock:
        return _codex_turn_locks.setdefault(key, threading.Lock())


def _codex_tool(item: dict) -> tuple[str, str]:
    kind = str(item.get("type") or "")
    if kind in ("commandExecution", "command_execution"):
        return "Bash", str(item.get("command") or "")[:400]
    if kind in ("mcpToolCall", "mcp_tool_call"):
        name = str(item.get("tool") or item.get("name") or "MCP")
        args = item.get("arguments") or item.get("input") or {}
        try:
            hint = json.dumps(args)[:400] if isinstance(args, dict) else str(args)[:400]
        except (TypeError, ValueError):
            hint = ""
        return name, hint
    if kind in ("fileChange", "file_change", "webSearch", "web_search"):
        return kind.replace("_", " ").title(), str(
            item.get("path") or item.get("query") or "")[:400]
    return "", ""


_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file_change",
    "item/permissions/requestApproval": "permissions",
    "mcpServer/elicitation/request": "mcp",
}


def _codex_write(entry: dict, message: dict) -> None:
    raw = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
    with entry["write_lock"]:
        entry["stdin"].write(raw)
        entry["stdin"].flush()


def _codex_reader(entry: dict) -> None:
    while True:
        try:
            line = entry["stdout"].readline()
        except (OSError, ValueError):
            return
        if not line:
            return
        try:
            entry["handle"].write(line)
            entry["handle"].flush()
        except (OSError, ValueError):
            pass
        try:
            ev = json.loads(line)
        except (TypeError, ValueError):
            continue
        method = str(ev.get("method") or "")
        if method in _APPROVAL_METHODS and "id" in ev:
            params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
            approval_id = uuid.uuid4().hex
            decisions = params.get("availableDecisions")
            if not isinstance(decisions, list):
                decisions = (["accept", "acceptForSession", "decline"]
                             if _APPROVAL_METHODS[method] != "mcp"
                             else ["accept", "decline"])
            approval = {
                "id": approval_id, "kind": _APPROVAL_METHODS[method],
                "method": method, "reason": str(params.get("reason") or
                                                   params.get("message") or ""),
                "command": str(params.get("command") or ""),
                "cwd": str(params.get("cwd") or ""),
                "server": str(params.get("serverName") or ""),
                "permissions": params.get("permissions"),
                "available_decisions": decisions,
                "_rpc_id": ev["id"], "_params": params,
            }
            if _auto_approve(entry["root"], approval):
                _codex_write(entry, {"id": ev["id"],
                                     "result": _approval_result(approval, "accept")})
                continue
            with _lock:
                entry["approvals"][approval_id] = approval
                entry["waiting"] = "waiting for your approval"
            continue
        if "id" in ev and ("result" in ev or "error" in ev):
            with entry["rpc_cv"]:
                entry["rpc_results"][str(ev["id"])] = ev
                entry["rpc_cv"].notify_all()
            continue
        entry["events"].append(ev)


def _codex_rpc(entry: dict, method: str, params: dict,
               timeout: float = 30.0) -> dict:
    with entry["rpc_cv"]:
        request_id = entry["next_rpc"]
        entry["next_rpc"] += 1
    _codex_write(entry, {"id": request_id, "method": method, "params": params})
    deadline = time.monotonic() + timeout
    with entry["rpc_cv"]:
        while str(request_id) not in entry["rpc_results"]:
            if entry["proc"].poll() is not None:
                raise Unavailable(f"Codex app server exited ({entry['proc'].returncode})")
            left = deadline - time.monotonic()
            if left <= 0:
                raise Unavailable(f"Codex app server did not answer {method}")
            entry["rpc_cv"].wait(min(left, 0.25))
        response = entry["rpc_results"].pop(str(request_id))
    if response.get("error"):
        error = response["error"]
        message = error.get("message") if isinstance(error, dict) else error
        raise Unavailable(f"Codex {method} failed: {message}")
    result = response.get("result")
    return result if isinstance(result, dict) else {}


def _spawn_codex(root, resume: str = "") -> dict:
    exe = _runners.find_codex()
    if not exe:
        raise Unavailable("codex CLI not found; install or select Claude Code")
    reason = _runners.preflight(_runners.RUNNERS["codex"], str(root), exe)
    if reason:
        raise Unavailable(reason)
    path = log_path(root)
    handle = open(path, "ab")
    handle.write((json.dumps({"type": "bgate_console_start", "runner": "codex-app-server",
                              "resumed": bool(resume), "ts": time.time()}) + "\n").encode())
    handle.flush()
    model = _model_for(root)
    args = _runners._codex_app_server_args(exe)
    try:
        proc = subprocess.Popen(
            args, cwd=str(root), env=_environ(root), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=handle, creationflags=_NO_WINDOW,
            start_new_session=(sys.platform != "win32"))
    except OSError as exc:
        handle.close()
        raise Unavailable(f"could not start Codex: {exc}") from exc
    entry = {"proc": proc, "handle": handle, "stdin": proc.stdin,
             "stdout": proc.stdout, "write_lock": threading.Lock(),
             "rpc_cv": threading.Condition(), "rpc_results": {},
             "next_rpc": 1, "events": [], "approvals": {},
             "root": str(root), "runner": "codex", "model": model,
             "cli_session_id": resume, "resumed": bool(resume),
             "current_item": 0, "busy": False,
             "record": "", "says": [], "waiting": "",
             "turns": 0, "started_at": time.monotonic(),
             "last_at": time.monotonic(), "turn_lock": threading.Lock()}
    with _lock:
        _live[_pkey(root)] = entry
    threading.Thread(target=_codex_reader, args=(entry,), daemon=True,
                     name="codex-app-reader").start()
    try:
        _codex_rpc(entry, "initialize", {"clientInfo": {
            "name": "builders-gate", "title": "Builders Gate", "version": "1"
        }, "capabilities": {"experimentalApi": True,
                             "requestAttestation": False}})
        _codex_write(entry, {"method": "initialized"})
        common = {"model": model or None, "cwd": str(root),
                  "approvalPolicy": CODEX_APPROVAL_POLICY,
                  "approvalsReviewer": "user", "sandbox": CODEX_SANDBOX}
        if resume:
            result = _codex_rpc(entry, "thread/resume", {
                "threadId": resume, **common, "excludeTurns": True})
        else:
            result = _codex_rpc(entry, "thread/start", {
                **common, "developerInstructions": system_prompt(root)})
    except Exception:
        _reap(_pkey(root), entry)
        raise
    thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
    entry["cli_session_id"] = str(thread.get("id") or resume)
    return entry


def _monitor_text(item: dict, status: str, runner: str = "",
                  ready: tuple[int, ...] = ()) -> str:
    item_id = int(item.get("id") or 0)
    title = str(item.get("title") or "work")[:120]
    if status == "dispatched":
        via = f" via {runner}" if runner else ""
        return f"Watching item #{item_id} ({title}){via}."
    result = " ".join(str(item.get("result") or "").split())[:300]
    line = f"Item #{item_id} is {status}"
    if result:
        line += f": {result}"
    if ready:
        line += ". Now ready: " + ", ".join(f"#{value}" for value in ready)
    return line + "."


def monitor_item(root, item_id: int, runner: str = "") -> None:
    """Attach the Director transcript to a dispatched board item."""
    key = (_pkey(root), int(item_id))
    with _board_watch_lock:
        if key in _board_watches:
            return
        _board_watches[key] = "dispatched"

    try:
        first = _queue.get(root, int(item_id))
    except Exception:
        with _board_watch_lock:
            _board_watches.pop(key, None)
        return

    _post(root, "tool", _monitor_text(first, "dispatched", runner),
          tool="Board monitor")

def report_monitored_item(root, item_id: int) -> None:
    """Publish the current state after the dispatch lifecycle advances it."""
    key = (_pkey(root), int(item_id))
    with _board_watch_lock:
        last = _board_watches.get(key)
    if last is None:
        return
    try:
        item = _queue.get(root, int(item_id))
    except Exception:
        return
    status = str(item.get("status") or "")
    if status == last:
        return
    ready = ()
    if status == "done":
        try:
            successors = {int(row["id"])
                          for row in _queue.successors(root, item_id)}
            now_ready = {int(row["id"]) for row in _queue.ready(root)}
            ready = tuple(sorted(successors & now_ready))
        except Exception:
            pass
    _post(root, "tool", _monitor_text(item, status, ready=ready),
          tool="Board monitor")
    with _board_watch_lock:
        if status in {"done", "failed", "cancelled"}:
            _board_watches.pop(key, None)
        else:
            _board_watches[key] = status


def _ensure_codex(root, resume: str = "") -> dict:
    key = _pkey(root)
    with _lock:
        entry = _live.get(key)
    if entry is not None:
        stale = (entry.get("runner") != "codex"
                 or entry.get("model") != _model_for(root)
                 or entry["proc"].poll() is not None)
        if not stale:
            return entry
        _reap(key, entry)
    return _spawn_codex(root, resume=resume)


def _collect_codex(entry: dict, deadline: float) -> dict:
    final = ""
    tokens = {}
    while True:
        while entry["events"]:
            ev = entry["events"].pop(0)
            method = str(ev.get("method") or "")
            params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
            if method == "item/completed":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if item.get("type") == "agentMessage":
                    text = str(item.get("text") or "").strip()
                    if text:
                        final = text
                        entry["says"].append(text)
                        del entry["says"][:-12]
                        if entry.get("record"):
                            _post(entry["record"], "assistant", text)
                elif entry.get("record"):
                    tool, hint = _codex_tool(item)
                    if tool:
                        _post(entry["record"], "tool", hint, tool=tool)
            elif method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage") or {}
                total = usage.get("total") if isinstance(usage, dict) else {}
                tokens = {"input": int((total or {}).get("inputTokens") or 0),
                          "output": int((total or {}).get("outputTokens") or 0),
                          "cache_read": int((total or {}).get("cachedInputTokens") or 0)}
            elif method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if turn.get("status") == "failed":
                    error = turn.get("error") or {}
                    return {"ok": False, "error": str(
                        error.get("message") if isinstance(error, dict) else error)[:400]}
                return {"ok": bool(final), "text": final, "tokens": tokens,
                        "error": "Codex completed without an answer" if not final else ""}
            elif method == "error":
                error = params.get("error") or params
                message = error.get("message") if isinstance(error, dict) else error
                return {"ok": False, "error": str(message or "Codex turn failed")[:400]}
        code = entry["proc"].poll()
        if code is not None:
            return {"ok": False, "dead": True,
                    "error": f"Codex app server exited ({code}) without answering"}
        if time.monotonic() >= deadline:
            return {"ok": False, "dead": True,
                    "error": f"Codex did not answer within {int(TURN_TIMEOUT_S)}s and was stopped"}
        time.sleep(POLL_S)


def _codex_deliver(root, entry: dict, prompt: str, *, record: bool,
                   item_id: int) -> dict:
    entry["current_item"] = int(item_id)
    entry["busy"] = True
    entry["record"] = str(root) if record else ""
    entry["says"] = []
    entry["waiting"] = ""
    _codex_rpc(entry, "turn/start", {
        "threadId": entry["cli_session_id"],
        "input": [{"type": "text", "text": prompt, "text_elements": []}],
        "approvalPolicy": CODEX_APPROVAL_POLICY, "approvalsReviewer": "user"})
    try:
        return _collect_codex(entry, time.monotonic() + max(30.0, TURN_TIMEOUT_S))
    finally:
        entry["current_item"] = 0
        entry["busy"] = False
        entry["record"] = ""
        entry["waiting"] = ""
        entry["last_at"] = time.monotonic()


def _turn_codex(root, prompt: str, reseed_context: str, settle, *,
                item_id: int = 0, record: bool = False) -> None:
    with _codex_lock(root):
        resume = _session_for(root, "codex")
        for attempt in range(2):
            text = prompt
            if not resume:
                context = ("\n\nRecent dashboard conversation:\n" + reseed_context
                           if reseed_context else "")
                text = system_prompt(root) + context + "\n\nHuman request:\n" + prompt
            try:
                entry = _ensure_codex(root, resume=resume)
            except Unavailable:
                if resume and attempt == 0:
                    forget(root, "codex")
                    resume = ""
                    continue
                raise
            got = _codex_deliver(root, entry, text, record=record,
                                 item_id=item_id)
            if got.get("ok"):
                session_id = str(entry.get("cli_session_id") or "")
                _remember(root, sessions={"codex": session_id})
                _store_usage(root, "codex", got.get("tokens") or {},
                             got.get("context_limit") or 0)
                settle(str(got.get("text") or ""), False)
                return
            if resume and attempt == 0:
                forget(root, "codex")
                _reap(_pkey(root), entry)
                resume = ""
                continue
            settle(str(got.get("error") or "Codex answered nothing"), True)
            return

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
        stale = (entry.get("runner") != "claude"
                 or entry.get("model") != _model_for(root)
                 or entry["proc"].poll() is not None
                 or time.monotonic() - entry["last_at"] > IDLE_S)
        if not stale:
            return entry
        _reap(key, entry)
    resume = _session_for(root, "claude")
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
    prompt = prompt + "\n\nDirector dispatch policy: " + _dispatch_instruction(root)
    if _runner_for(root) == "codex":
        _turn_codex(root, prompt, reseed_context, settle,
                    item_id=item_id, record=record)
        return
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
        session_id = entry.get("cli_session_id") or ""
        _remember(root, cli_session_id=session_id,
                  sessions={"claude": session_id}, turns=int(entry["turns"]))
    _store_usage(root, "claude", got.get("tokens") or {},
                 got.get("context_limit") or 0)
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
