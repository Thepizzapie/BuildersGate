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
    # stream-json + verbose makes claude emit one NDJSON event per step AS IT
    # WORKS (tool calls, messages), instead of buffering everything to the end
    # -- that's what feeds the live "what is the agent doing" view. read_activity
    # parses this log back into readable steps.
    args = [claude, "-p", _prompt_for(item), "--permission-mode", permission_mode,
            "--output-format", "stream-json", "--verbose",
            "--allowedTools", "mcp__builders-gate", "Read", "Edit", "Write",
            "Glob", "Grep", "Bash"]
    if model:
        args += ["--model", model]

    log_handle = open(log_path, "ab")
    proc = subprocess.Popen(args, cwd=str(root), env=env,
                            stdin=subprocess.DEVNULL, stdout=log_handle,
                            stderr=log_handle, creationflags=_NO_WINDOW)
    with _lock:
        _live[item_id] = {"proc": proc, "log": str(log_path), "handle": log_handle}
    _queue.set_status(root, item_id, "dispatched")
    return {"ok": True, "item_id": item_id, "pid": proc.pid, "log": str(log_path)}


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
                _assets.heartbeat(root, f"item-{item_id}")
                out.append({"item_id": item_id, "state": "running",
                            "pid": entry["proc"].pid, "log": entry["log"]})
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
