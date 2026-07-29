"""SessionStart hook — what the director needs to know before its first turn.

THE MCP `instructions` FIELD CARRIES THE ROLE; THIS CARRIES THE SITUATION.
Those two are different things and only one of them can travel in a static
string. `instructions` is fixed when the stdio server boots, so it can say "the
dashboard is what runs queued work" but never "the dashboard is down right now",
"four items are queued", or "another session has been in combat_view.gd for six
minutes". A director that has to ask three questions before it can act will
skip them, and today's evidence is that it does.

So this prints STATE, not doctrine: the project, the board, who else is holding
a file, and whether enforcement is live. Nothing here repeats the role text —
that arrives on the same turn through the server and saying it twice buys
nothing but tokens.

CONTRACT WITH THE HARNESS. Claude Code runs SessionStart before the first user
turn and adds `hookSpecificOutput.additionalContext` to the session. Exit 0
always. Print nothing when there is nothing to say — outside a Builders Gate
project this hook is silent, which matters because it installs at USER scope and
therefore runs for every session on the machine, most of which are not games.

FAIL-OPEN, HARDER THAN THE PreToolUse HOOK. That one fails open per tool call;
this one runs once and a crash here is a session that will not start. Every
lookup is individually guarded and a total failure prints nothing at all.
"""
from __future__ import annotations

import json
import os
import socket
import sys

# Enough to see the shape of the board without pasting it into the context. A
# director that needs the sixth item can call queue_list; one that needs to know
# the board is busy needs only the top of it.
TOP_N = 5
BOARD_PORT = 7788
PROBE_TIMEOUT_S = 0.25


def _serve_is_up(port: int = BOARD_PORT) -> bool:
    """Is the dashboard actually accepting connections?

    Not "is there a token file" — `.bgate/ui-token` is minted once and outlives
    every run, so its presence answers a question nobody asked. The only honest
    test of "will a queued item be dispatched" is whether something is listening.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _lines(root) -> list[str]:
    from bgate_core import assets as _assets
    from bgate_core import db as _db
    from bgate_core import queue as _queue

    out: list[str] = []

    # --- what game is this ---------------------------------------------------
    try:
        row = _db.connect(root).execute(
            "SELECT name, pitch, engine, dimension FROM project").fetchone()
        if row:
            head = f"PROJECT  {row['name']}"
            tag = " ".join(x for x in (row["engine"], row["dimension"]) if x)
            if tag:
                head += f"  [{tag}]"
            out.append(head)
            if row["pitch"]:
                out.append(f"         {row['pitch']}")
    except Exception:
        pass
    out.append(f"ROOT     {root}")

    # --- the board, and whether it can run anything --------------------------
    up = _serve_is_up()
    try:
        queued = _queue.list_items(root, status="queued") or []
        running = _queue.list_items(root, status="dispatched") or []
    except Exception:
        queued, running = [], []

    if up:
        out.append(f"BOARD    bgate serve is UP — queue_add dispatches. "
                   f"{len(queued)} queued, {len(running)} running.")
    else:
        out.append(f"BOARD    bgate serve is DOWN — nothing will dispatch. "
                   f"{len(queued)} queued items are parked, not running. "
                   f"Start it with `bgate serve` before delegating, or say so.")

    for item in running[:TOP_N]:
        out.append(f"  RUNNING #{item['id']} [{item['seat']}] {item['title'][:64]}")
    for item in queued[:TOP_N]:
        out.append(f"  queued  #{item['id']} [{item['seat']}] p{item['priority']} "
                   f"{item['title'][:64]}")
    if len(queued) > TOP_N:
        out.append(f"  ...and {len(queued) - TOP_N} more (queue_list)")

    # --- WHO ELSE IS IN THE FILES -------------------------------------------
    # The one this file exists for. Two sessions edited one module on one
    # afternoon and neither knew, because nothing ever told either of them who
    # else was working. A lease is only useful if you learn about it before you
    # have already written the file.
    try:
        leases = _assets.list_path_leases(root) or []
    except Exception:
        leases = []
    try:
        locked = _assets.list_assets(root, locked_only=True) or []
    except Exception:
        locked = []
    if leases or locked:
        out.append(f"LIVE     {len(leases)} file(s) leased, {len(locked)} binary "
                   "lock(s) held — someone else may be working right now:")
        for lease in leases[:TOP_N]:
            out.append(f"  {lease['path']} — {lease['owner']} "
                       f"(seat {lease['seat'] or '?'}) until {lease['expires_at']}")
        for asset in locked[:TOP_N]:
            out.append(f"  {asset['path']} — LOCKED by seat {asset['lock_seat']}")
        out.append("  Do not edit those; coordinate (seat_post_note) or pick "
                   "something else.")

    # --- WHERE THE LAST SESSION GOT TO --------------------------------------
    # The half a static string cannot carry and the board does not record: what
    # somebody was midway through, what they decided and why, what they
    # deliberately left alone. Last, because it is the longest block and a reader
    # who stops early has still seen the board.
    try:
        from bgate_core import handoff as _handoff
        out.extend(_handoff.digest(root))
    except Exception:
        pass

    # --- is anything actually enforced --------------------------------------
    try:
        from bgate_cli import hook as _hook
        mode = "block" if os.environ.get("BGATE_SEAT", "").strip() else _hook.director_mode()
        out.append(f"GATE     PreToolUse director mode = {mode}"
                   + ("  (collisions refused, lane advisory)" if mode == "collide"
                      else "  (fully inert)" if mode == "off" else ""))
    except Exception:
        pass

    return out


def build_context(cwd: str = "") -> str:
    """The block to inject, or "" when this is not a Builders Gate project."""
    try:
        from bgate_core import db as _db
        root = _db.resolve_root(cwd or os.getcwd())
    except Exception:
        return ""
    if root is None:
        return ""
    try:
        body = "\n".join(_lines(root))
    except Exception:
        return ""
    return (
        "=== BUILDERS GATE — board state at session start ===\n"
        + body
        + "\n(Your role and the dispatch protocol arrive separately, from the "
          "builders-gate MCP server. This block is only the live situation.)"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cwd = ""
    try:
        # Claude Code pipes the session payload on stdin; `cwd` is the session's,
        # which is not this process's when the hook is installed at user scope.
        if not sys.stdin.isatty():
            payload = json.loads(sys.stdin.read() or "{}")
            cwd = str(payload.get("cwd") or "")
    except Exception:
        cwd = ""
    if "--print" in argv:                      # `bgate session-start --print`
        text = build_context(cwd)
        print(text if text else "(not inside a Builders Gate project)")
        return 0
    try:
        text = build_context(cwd)
        if text:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }}))
    except Exception:
        pass                                   # a silent session beats no session
    return 0


if __name__ == "__main__":
    sys.exit(main())
