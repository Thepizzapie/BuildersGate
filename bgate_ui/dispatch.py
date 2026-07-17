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
    return (
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

    env = {**os.environ, "BGATE_SEAT": item["seat"], "BGATE_ROOT": str(root)}
    args = [claude, "-p", _prompt_for(item), "--permission-mode", permission_mode,
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
                out.append({"item_id": item_id, "state": "running",
                            "pid": entry["proc"].pid, "log": entry["log"]})
    return out


def stop(item_id: int) -> dict:
    with _lock:
        entry = _live.get(item_id)
        if not entry or entry["proc"].poll() is not None:
            return {"ok": False, "error": "no live agent for this item"}
        entry["proc"].terminate()
    return {"ok": True, "item_id": item_id}
