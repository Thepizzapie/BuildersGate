"""Dispatch — the dashboard spawns real Claude seat sessions against work items.

Why this architecture wins: a session spawned with cwd = the game project gets
(1) the builders-gate MCP tools NATIVELY (the server resolves the project by
cwd — no runner scripts, no kwargs files), and (2) the PreToolUse lane/lock
hook with BGATE_SEAT set — actual enforcement, not honor-system. The dashboard
is user-run software, so a dispatch click is the USER launching the agent.

One live session per work item; state is in-memory plus a log file per item
(.bgate/agents/item-<id>.log) so a dashboard restart loses handles, not history.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from bgate_core import assets as _assets
from bgate_core import queue as _queue

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_live: dict[int, dict] = {}
_lock = threading.Lock()


def _user_msg(text: str) -> str:
    """A stream-json user turn — the wire format the CLI reads from stdin."""
    return json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": text}]}}) + "\n"


def find_claude() -> Optional[str]:
    exe = shutil.which("claude")
    if exe:
        return exe
    fallback = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    return str(fallback) if fallback.exists() else None


def _prompt_for(item: dict) -> str:
    from bgate_core.seats import SEAT_IDENTITY

    return (
        SEAT_IDENTITY + "\n\n"
        f"You are the {item['seat'].upper()} seat of the Builders Gate game project "
        "in the current directory. The builders-gate MCP tools are available to you "
        "NATIVELY — no runner scripts.\n\n"
        f"WORK ITEM #{item['id']} ({item['source']}): {item['title']}\n"
        f"{item['brief']}\n\n"
        "Protocol, in order:\n"
        "1. seat_brief for your role — mission, lanes, bible, pinned refs, notes.\n"
        f"2. Read .bgate/progress/item-{item['id']}.jsonl if it exists (a "
        "predecessor's trail); append one JSON line "
        '{"step":...,"artifacts":[...],"next":...} after EVERY unit of work.\n'
        "3. Do the work inside your lanes (the PreToolUse hook enforces them; "
        "seat_can_write is the oracle). Lock binaries before editing.\n"
        "4. Verify per house norms: godot_check_project after structural changes; "
        "run game/tests/fight_test.gd via godot_run when combat code moved "
        "(fail=0 or report exactly why); godot_screenshot when the change is "
        "visible; LOOK at what you produce.\n"
        "5. seat_post_note with what changed.\n"
        f"6. Mark the item: call queue_complete with item_id={item['id']} and a "
        "one-paragraph result (status 'done', or 'failed' with the honest reason).\n"
    )


def dispatch(root: str, item_id: int, *, permission_mode: str = "acceptEdits",
             model: Optional[str] = None) -> dict:
    """Spawn a Claude session against a queued item. One per item."""
    claude = find_claude()
    if not claude:
        return {"ok": False, "error": "claude CLI not found on PATH"}
    item = _queue.get(root, item_id)
    if item["status"] != "queued":
        return {"ok": False, "error": f"item {item_id} is {item['status']}, not queued"}
    with _lock:
        if item_id in _live and _live[item_id]["proc"].poll() is None:
            return {"ok": False, "error": f"item {item_id} already has a live agent"}

    log_dir = Path(root) / ".bgate" / "agents"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"item-{item_id}.log"

    env = {
        **os.environ,
        "BGATE_SEAT": item["seat"],
        "BGATE_ROOT": str(root),
        "BGATE_WORK_ITEM": str(item_id),
        "BGATE_LOCK_OWNER": f"item-{item_id}",
    }
    # stream-json OUTPUT makes claude emit one NDJSON event per step AS IT WORKS
    # (tool calls, messages) instead of buffering to the end -- that feeds the
    # live activity view. stream-json INPUT keeps stdin open as a channel: the
    # initial prompt is the first user message, and steer() can inject more
    # user turns WHILE the agent runs. --replay-user-messages echoes injected
    # steers back into the output log so they show in the activity feed. The
    # process waits on stdin, so it only exits when we close the pipe (done in
    # status() once the agent self-reports via queue_complete).
    args = [claude, "-p", "--permission-mode", permission_mode,
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--replay-user-messages",
            "--allowedTools", "mcp__builders-gate", "Read", "Edit", "Write",
            "Glob", "Grep", "Bash"]
    if model:
        args += ["--model", model]

    log_handle = open(log_path, "ab")
    proc = subprocess.Popen(args, cwd=str(root), env=env,
                            stdin=subprocess.PIPE, stdout=log_handle,
                            stderr=log_handle, creationflags=_NO_WINDOW)
    # Deliver the task as the first streamed user message, then leave stdin open.
    try:
        proc.stdin.write(_user_msg(_prompt_for(item)).encode("utf-8"))
        proc.stdin.flush()
    except OSError as exc:
        proc.kill()
        return {"ok": False, "error": f"could not send prompt to agent: {exc}"}
    with _lock:
        _live[item_id] = {"proc": proc, "log": str(log_path), "handle": log_handle,
                          "stdin": proc.stdin, "steers": [], "stdin_closed": False}
    _queue.set_status(root, item_id, "dispatched")
    # The streamed session waits on stdin forever; close it once the agent
    # self-reports so it exits even when no dashboard is polling /api/agents.
    threading.Thread(target=_watch_completion, args=(root, item_id),
                     daemon=True).start()
    return {"ok": True, "item_id": item_id, "pid": proc.pid, "log": str(log_path)}


def _watch_completion(root: str, item_id: int, poll_s: float = 4.0) -> None:
    """Close the agent's stdin once it has queue_complete'd, so the waiting
    process reaches EOF and exits — independent of any UI polling."""
    import time
    while True:
        time.sleep(poll_s)
        with _lock:
            entry = _live.get(item_id)
            if not entry:
                return
            if entry["proc"].poll() is not None:
                return  # already gone; status() will reap
            if entry.get("stdin_closed"):
                return
            try:
                if _queue.get(root, item_id)["status"] in ("done", "failed"):
                    try:
                        entry["stdin"].close()
                    except OSError:
                        pass
                    entry["stdin_closed"] = True
                    return
            except LookupError:
                return


def steer(root: str, item_id: int, text: str) -> dict:
    """Inject a live user message into a running agent — course-correction
    without killing and re-dispatching. Lands as a new user turn mid-work."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "steer text is empty"}
    with _lock:
        entry = _live.get(item_id)
        if not entry or entry["proc"].poll() is not None:
            return {"ok": False, "error": "no live agent for this item"}
        if entry.get("stdin_closed"):
            return {"ok": False, "error": "agent is finishing; steer channel closed"}
        try:
            entry["stdin"].write(_user_msg(f"STEER FROM THE DIRECTOR (act on this now): {text}").encode("utf-8"))
            entry["stdin"].flush()
        except OSError as exc:
            return {"ok": False, "error": f"agent not accepting input: {exc}"}
        entry["steers"].append(text)
    return {"ok": True, "item_id": item_id, "steers": len(entry["steers"])}


def status(root: str) -> list[dict]:
    """Live agent table for the dashboard; reaps finished processes."""
    out = []
    with _lock:
        for item_id, entry in list(_live.items()):
            code = entry["proc"].poll()
            if code is not None:
                entry["handle"].close()
                # The agent should have queue_complete'd itself; a nonzero exit
                # with the item still 'dispatched' means it died — mark failed.
                try:
                    item = _queue.get(root, item_id)
                    if item["status"] == "dispatched":
                        _queue.set_status(
                            root, item_id,
                            "done" if code == 0 else "failed",
                            result=f"session exited {code} without self-reporting")
                except LookupError:
                    pass
                del _live[item_id]
                out.append({"item_id": item_id, "state": "exited", "code": code})
            else:
                # The streamed session waits on stdin forever. Once the agent
                # has self-reported (queue_complete -> status no longer
                # 'dispatched'), close stdin so it hits EOF and exits.
                if not entry.get("stdin_closed"):
                    try:
                        item = _queue.get(root, item_id)
                        if item["status"] in ("done", "failed"):
                            entry["stdin"].close()
                            entry["stdin_closed"] = True
                    except LookupError:
                        pass
                _assets.heartbeat(root, f"item-{item_id}")
                out.append({"item_id": item_id, "state": "running",
                            "pid": entry["proc"].pid, "log": entry["log"],
                            "steers": len(entry.get("steers", []))})
    return out


def read_activity(root: str, item_id: int, limit: int = 40) -> dict:
    """Parse an agent's stream-json log into a readable live activity feed:
    what tools it's calling, what it's saying, and its final result."""
    import json

    log_path = Path(root) / ".bgate" / "agents" / f"item-{item_id}.log"
    if not log_path.is_file():
        return {"steps": [], "running": item_id in _live, "final": None}

    steps: list[dict] = []
    final = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    steps.append({"kind": "say", "text": block["text"].strip()[:280]})
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    hint = (inp.get("path") or inp.get("file_path") or inp.get("role")
                            or inp.get("title") or inp.get("query") or inp.get("prompt")
                            or inp.get("command") or "")
                    steps.append({"kind": "tool", "name": name.replace("mcp__builders-gate__", ""),
                                  "hint": str(hint)[:80]})
        elif etype == "user":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    c = block.get("content")
                    txt = c if isinstance(c, str) else (
                        c[0].get("text", "") if isinstance(c, list) and c else "")
                    if txt.strip():
                        steps.append({"kind": "result", "text": txt.strip()[:160]})
                elif block.get("type") == "text":
                    # Replayed user turns. The initial prompt is one too; only
                    # surface live steers (they carry the director marker).
                    txt = block.get("text", "")
                    marker = "STEER FROM THE DIRECTOR (act on this now): "
                    if marker in txt:
                        steps.append({"kind": "steer",
                                      "text": txt.split(marker, 1)[1].strip()[:200]})
        elif etype == "result":
            final = {"subtype": ev.get("subtype"),
                     "text": str(ev.get("result", ""))[:400],
                     "cost": ev.get("total_cost_usd"),
                     "turns": ev.get("num_turns")}
    live = item_id in _live and _live[item_id]["proc"].poll() is None
    return {"steps": steps[-limit:], "running": live, "final": final,
            "step_count": len(steps)}


def stop(item_id: int) -> dict:
    with _lock:
        entry = _live.get(item_id)
        if not entry or entry["proc"].poll() is not None:
            return {"ok": False, "error": "no live agent for this item"}
        entry["proc"].terminate()
    return {"ok": True, "item_id": item_id}
