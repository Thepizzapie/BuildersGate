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
from pathlib import Path

# Enough to see the shape of the board without pasting it into the context. A
# director that needs the sixth item can call queue_list; one that needs to know
# the board is busy needs only the top of it.
TOP_N = 5

# How long a 'dispatched' row may sit untouched before it is shown as STALE
# rather than RUNNING. Well past dispatch.HARD_RUNTIME_S (2h), so a genuinely
# long run is never libelled — anything past this has outlived every ceiling
# the dispatcher enforces and is almost certainly a row a dead dashboard left.
STALE_RUN_MIN = 180


def _age_min(stamp: str):
    """Minutes since a SQLite `datetime('now')` stamp (UTC), or None.

    Returns None rather than 0 for anything unparseable: "no age" and "just
    now" mean opposite things to a reader deciding whether to trust a row.
    """
    from datetime import datetime, timezone

    text = str(stamp or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            when = datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        delta = (datetime.now(timezone.utc) - when).total_seconds() / 60.0
        return max(0.0, delta)
    return None


def _ago(minutes: float) -> str:
    if minutes < 90:
        return f"{int(minutes)}m ago"
    if minutes < 60 * 48:
        return f"{int(minutes // 60)}h ago"
    return f"{int(minutes // 1440)}d ago"
BOARD_PORT = 7788
PROBE_TIMEOUT_S = 0.25
# The dashboard answers this without a token and it is on loopback, so the cost
# is a millisecond. Still budgeted, because a hook that hangs is a session that
# does not start.
ROOT_TIMEOUT_S = 0.4
# Other projects to name when this directory is not one. Four is enough to
# recognise the one you meant; more is a directory listing.
KNOWN_N = 4


def _board_root(port: int = BOARD_PORT) -> str:
    """Which project the LIVE dashboard is actually serving.

    A dashboard is per-root: it dispatches, runs autopilot and delivers steers
    for ONE project. So "something is listening on 7788" does not mean "your
    queued item will run" — it means that for whoever's root it booted with.
    Measured cost of not saying which: a session found a 200 on the port,
    assumed it was its own board, and queued work onto a dead one.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/project",
                timeout=ROOT_TIMEOUT_S) as resp:
            return str((json.loads(resp.read() or b"{}").get("data")
                        or {}).get("root") or "")
    except Exception:
        return ""


def _real(path) -> str:
    """A path in the one form two of them can be compared in.

    RESOLVED, not just normcased. On Windows the two spellings of the same
    directory are not string-equal: a GitHub Actions runner reports
    ``C:\\Users\\RUNNER~1\\AppData\\Local\\Temp`` for the temp directory while
    everything else in the process says ``C:\\Users\\runneradmin\\...``. normpath
    and normcase fix slashes and case and leave the 8.3 short name alone, so a
    prefix test between them silently never matches — which is exactly how the
    fixture filter below passed locally and failed in CI.
    """
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except Exception:
        return os.path.normcase(os.path.normpath(str(path)))


def _temp_dir() -> str:
    """The system temp directory, resolved — projects under it are fixtures.

    The test suite registers its fixture projects in the same machine-wide
    registry the human's games live in, and a pytest tmpdir outlives the run that
    made it. Measured: five registered projects, four of them dead fixtures, and
    the actual game pushed below an "...and 1 more". A project inside temp is
    never the one the human meant.
    """
    try:
        import tempfile
        return _real(tempfile.gettempdir())
    except Exception:
        return ""


def _known_lines(here) -> list[str]:
    """The other projects on this machine, for a session that landed outside one.

    THIS HOOK NEVER PICKS A PROJECT. It cannot: the cwd is the only thing that
    says what the human meant, and when the cwd is not a game the honest answer
    is a list plus an instruction to pass `project_dir`. What it must not do is
    stay quiet, because the alternative to four lines here is an agent grepping
    the desktop — which is the ten minutes this block exists to buy back.

    Ordered by when each project was last touched, so a live game outranks a
    pytest temp dir that still happens to have a game.db in it.
    """
    from bgate_core.store import db as _db
    from bgate_core.store import project as _project
    from bgate_core.board import queue as _queue

    tmp = _temp_dir()
    rows: list[tuple[str, str, str, int, int]] = []
    for name, root in (_project.known_projects() or {}).items():
        if str(root) == str(here):
            continue
        if tmp and _real(root).startswith(tmp):
            continue
        try:
            row = _db.connect(root).execute(
                "SELECT name, updated_at FROM project").fetchone()
        except Exception:
            continue
        if not row:
            continue
        try:
            queued = len(_queue.list_items(root, status="queued") or [])
            running = len(_queue.list_items(root, status="dispatched") or [])
        except Exception:
            queued, running = 0, 0
        rows.append((str(row["updated_at"] or ""), str(row["name"] or name),
                     str(root), queued, running))
    if not rows:
        return ["PROJECTS no other project is registered on this machine. "
                "`bgate adopt` in a game directory registers one."]
    rows.sort(reverse=True)
    out = [f"PROJECTS {len(rows)} registered elsewhere — pass one as "
           "`project_dir` on EVERY call; do not go looking for it:"]
    for _, name, root, queued, running in rows[:KNOWN_N]:
        busy = f"  ({queued} queued, {running} running)" if queued or running else ""
        out.append(f"  {name} — {root}{busy}")
    if len(rows) > KNOWN_N:
        out.append(f"  ...and {len(rows) - KNOWN_N} more (project_select)")
    return out


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
    from bgate_core.store import assets as _assets
    from bgate_core.store import db as _db
    from bgate_core.board import queue as _queue

    out: list[str] = []

    # --- what game is this ---------------------------------------------------
    row = None
    try:
        row = _db.connect(root).execute(
            "SELECT name, pitch, engine, dimension FROM project").fetchone()
    except Exception:
        row = None
    if row:
        head = f"PROJECT  {row['name']}"
        tag = " ".join(x for x in (row["engine"], row["dimension"]) if x)
        if tag:
            head += f"  [{tag}]"
        out.append(head)
        if row["pitch"]:
            out.append(f"         {row['pitch']}")
    out.append(f"ROOT     {root}")

    # A .bgate with no project row is a real and common shape: the Builders Gate
    # checkout itself, a directory somebody ran a tool in, a half-finished adopt.
    # Everything below this line would then describe an empty board belonging to
    # no game — which reads exactly like "the game has nothing on it" and sent a
    # session hunting the disk for the project it was asked about. So say what
    # this root is NOT, and name the ones it could have meant.
    if not row:
        out.append("NO GAME  this root has a .bgate but no project — it is not a "
                   "game. An empty board below is THIS directory's, not any "
                   "game's, and every bgate tool needs an explicit project_dir.")
        try:
            out.extend(_known_lines(root))
        except Exception:
            pass
    else:
        # Self-registering, so discovery works for the NEXT session that starts
        # somewhere else. The registry was only ever written by init/adopt/select,
        # which is why a game worked on for a week could still be missing from it.
        try:
            from bgate_core.store import project as _project
            _project.register(root)
        except Exception:
            pass

    # --- the board, and whether it can run anything --------------------------
    up = _serve_is_up()
    try:
        queued = _queue.list_items(root, status="queued") or []
        running = _queue.list_items(root, status="dispatched") or []
    except Exception:
        queued, running = [], []

    if not up:
        out.append(f"BOARD    bgate serve is DOWN — nothing will dispatch. "
                   f"{len(queued)} queued items are parked, not running. "
                   f"Start it with `bgate serve` before delegating, or say so.")
    else:
        served = _board_root()
        if served and os.path.normcase(os.path.normpath(served)) != \
                os.path.normcase(os.path.normpath(str(root))):
            out.append(f"BOARD    bgate serve is UP but SERVING ANOTHER ROOT "
                       f"({served}) — a queue_add here will NOT dispatch. "
                       f"{len(queued)} queued, {len(running)} running. Restart "
                       f"it on this root before delegating, or say so.")
        else:
            out.append(f"BOARD    bgate serve is UP on this root. "
                       f"{len(queued)} queued, {len(running)} running.")
            # Two things that make an UP board dispatch nothing anyway. Both are
            # local reads, and both have already cost a session the difference
            # between "delegated" and "filed": autopilot is a persisted switch
            # that survives a restart OFF, and dispatch() refuses outright on a
            # dirty tree (bgate_ui/agents/dispatch.py — "commit or stash first").
            # THE EFFECTIVE VALUE, not the raw doc. autopilot.on defaults to
            # ON and a fresh project has no doc at all, so reading the doc
            # told the director "autopilot is OFF" on a board that was
            # dispatching (Hot Cargo, 2026-09-04) - and it filed work with a
            # hand-deploy warning while the console banner said on.
            try:
                from bgate_core.store import settings as _settings
                if not bool(_settings.get(root, "autopilot.on")):
                    out.append("         autopilot is OFF — queued items wait "
                               "for a hand on the deploy button, not for a slot.")
            except Exception:
                pass
            try:
                from bgate_core.board import gitwork as _git
                state = _git.dirty(root)
                if state.get("dirty"):
                    out.append(f"         tree is DIRTY ({len(state['paths'])} "
                               "path(s)) — dispatch REFUSES on that. Commit, "
                               "stash, or dispatch with allow_dirty.")
            except Exception:
                pass

    # WHO SIGNS OFF, and what is already stuck behind that. Under the builder's
    # gate a board full of 'review' is not a stall, it is the board waiting on a
    # person — and a session that does not know which mode is on reads the same
    # queue as either "nothing is happening" or "nothing needs me".
    try:
        from bgate_core.board import gates as _gates
        held = _queue.list_items(root, status="review")
        line = f"SIGNOFF  approval gate = {_gates.mode(root)}"
        if held:
            line += (f" — {len(held)} item(s) FINISHED and waiting on the human: "
                     + ", ".join(f"#{h['id']}" for h in held[:6]))
            if len(held) > 6:
                line += ", ..."
        out.append(line)
    except Exception:
        pass

    # AGE, BECAUSE 'dispatched' IS NOT PROOF OF LIFE. _live dies with the
    # dashboard and only dispatch.reconcile (which runs inside `bgate serve`)
    # un-strands the rows it left behind — so a session opened days later was
    # told three-week-old corpses were RUNNING, with nothing to distinguish
    # them from an agent working right now.
    for item in running[:TOP_N]:
        age = _age_min(item.get("updated_at") or "")
        stale = age is not None and age >= STALE_RUN_MIN
        mark = "STALE  " if stale else "RUNNING"
        when = f" ({_ago(age)})" if age is not None else ""
        out.append(f"  {mark} #{item['id']} [{item['seat']}] "
                   f"{item['title'][:64]}{when}")
    if any((_age_min(i.get("updated_at") or "") or 0) >= STALE_RUN_MIN
           for i in running[:TOP_N]):
        out.append("  ^ STALE = marked dispatched but untouched for "
                   f"{STALE_RUN_MIN // 60}h+. If no agent is really running, "
                   "`bgate serve` settles these on startup; until then they "
                   "are neither running nor finished.")
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
        from bgate_core.board import handoff as _handoff
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
        from bgate_core.store import db as _db
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
