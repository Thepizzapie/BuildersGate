"""Is each coding-agent CLI installed, and is Builders Gate actually wired into it.

THE PAPERCUT THIS EXISTS FOR IS NAMED IN CLAUDE.md, and it is the worst one in
the product's setup story:

    claude mcp add builders-gate --scope user -- <ABSOLUTE-python-path> -m bgate_mcp.server

    **Use the absolute path to the interpreter.** The claude CLI resolves a bare
    `python` differently than the shell does and reports "failed to connect" for
    a server that runs fine. On Windows this is the single most common failure,
    and the error message points nowhere near the cause.

A registration pointing at the wrong interpreter looks EXACTLY like a working
one until a tool call fails, and the failure names the connection rather than
the interpreter. Nothing in the product could see that state, so nothing could
say it out loud. This module can: it reads the registration, compares the
interpreter against the one this dashboard is running on, and says which of the
three states it is in.

WHAT IT DOES NOT DO.

  * IT DOES NOT LAUNCH ANYBODY'S CLI. Detection, wiring status, and a config
    write. Spawning an interactive session is the brainstorm room's machinery
    and there must not be a second one.
  * IT DOES NOT HAND-EDIT THEIR CONFIG. ``~/.claude.json`` and
    ``~/.codex/config.toml`` are files the user also edits, in formats their
    owners are free to change. Registration goes through the CLI's OWN
    ``mcp add`` subcommand — argv list, no shell — so the tool that owns the
    format writes the format. Reading is done directly, because reading cannot
    corrupt anything and shelling out per repaint would not be free.
  * IT DOES NOT DUPLICATE :mod:`bgate_ui.runners`. That module is the registry —
    which CLIs exist, how to find them, what each one can do — and
    ``runners.available()`` is the installed/path detection. This adds only the
    half runners has no opinion about: how each CLI persistently registers an
    MCP server for the user's OWN interactive sessions, which is a different
    thing from ``runners.mcp_overrides()`` (that is per-invocation, in memory,
    for a dispatched agent, and deliberately leaves nothing behind).

A THIRD CLI IS ONE ENTRY in :data:`WIRINGS` — provided ``runners.RUNNERS`` has
it, since that is where "does this CLI exist" is answered.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from bgate_ui import runners as _runners

# Windows: never flash a console window out of a dashboard request.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

SERVER = _runners.MCP_SERVER_NAME
MODULE_ARGS = ["-m", "bgate_mcp.server"]

# A registration whose command is one of these AND carries no directory part is
# the documented failure. The "no directory part" half is load-bearing: an
# absolute path ending in python.exe is the CORRECT answer, and a set membership
# test on the basename alone would flag every good registration as broken. That
# mistake was made once here and caught by running it against this machine.
_BARE = {"python", "python3", "py", "python.exe", "python3.exe", "py.exe",
         "pythonw.exe", "python.bat"}


def _is_bare(command: str) -> bool:
    head, tail = os.path.split(command or "")
    return not head and tail.lower() in _BARE

# How long a `mcp add` is allowed to take. Generous: these CLIs sometimes touch
# the network on first run.
REGISTER_TIMEOUT = 90
VERIFY_TIMEOUT = 45


@dataclass(frozen=True)
class Wiring:
    """How one CLI persistently registers an MCP server, and where that lands."""

    id: str
    label: str
    # The config file a human would open. Shown, never written by this module.
    config: Callable[[], Path] = field(repr=False, default=lambda: Path())
    read: Callable[[], dict] = field(repr=False, default=lambda: {})
    # exe, interpreter -> argv. The CLI's own subcommand does the write.
    argv: Callable[[str, str], list[str]] = field(
        repr=False, default=lambda exe, py: [])
    how: str = ""
    scope_note: str = ""


# ---------------------------------------------------------------------------
# Claude Code — ~/.claude.json, "mcpServers" at user scope
# ---------------------------------------------------------------------------

def _claude_config() -> Path:
    return Path.home() / ".claude.json"


def _claude_read() -> dict:
    """The builders-gate entry, at whichever scope it is registered.

    USER SCOPE IS CHECKED FIRST AND IS THE ONE THIS PANEL OFFERS, because it
    covers every game project on the machine including ones that do not exist
    yet — the same argument ``bgate hook-install --scope user`` makes. A
    project-scoped entry is reported when found so a user who set one up by hand
    is not told they have nothing.
    """
    path = _claude_config()
    out: dict[str, Any] = {"found": False, "path": str(path), "scope": "",
                           "command": "", "args": [], "error": ""}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        out["error"] = "no ~/.claude.json yet — the CLI writes it on first run"
        return out
    try:
        doc = json.loads(raw)
    except ValueError:
        out["error"] = f"{path} is not valid JSON; refusing to guess at it"
        return out
    if not isinstance(doc, dict):
        out["error"] = f"{path} is not an object"
        return out

    entry = ((doc.get("mcpServers") or {}) if isinstance(
        doc.get("mcpServers"), dict) else {}).get(SERVER)
    scope = "user"
    if not isinstance(entry, dict):
        entry, scope = None, ""
        projects = doc.get("projects")
        if isinstance(projects, dict):
            for name, blob in projects.items():
                if not isinstance(blob, dict):
                    continue
                candidate = (blob.get("mcpServers") or {})
                if isinstance(candidate, dict) and isinstance(
                        candidate.get(SERVER), dict):
                    entry, scope = candidate[SERVER], f"local ({name})"
                    break
    if not isinstance(entry, dict):
        return out
    args = entry.get("args")
    out.update(found=True, scope=scope,
               command=str(entry.get("command") or ""),
               args=[str(a) for a in args] if isinstance(args, list) else [])
    return out


def _claude_argv(exe: str, interpreter: str) -> list[str]:
    # `mcp add` refuses a name it already holds, so the existing one is removed
    # first — this is a re-register as much as a register, and the broken state
    # it fixes is one where an entry is already there.
    return [exe, "mcp", "add", SERVER, "--scope", "user", "--",
            interpreter, *MODULE_ARGS]


def _claude_unregister(exe: str) -> list[str]:
    return [exe, "mcp", "remove", SERVER, "--scope", "user"]


# ---------------------------------------------------------------------------
# Codex — ~/.codex/config.toml, [mcp_servers.<name>]
# ---------------------------------------------------------------------------

def _codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "config.toml"


_TOML_TABLE = re.compile(r"^\s*\[mcp_servers\.(?:\"([^\"]+)\"|([^\].]+))\]\s*$")
_TOML_STR = re.compile(r"""^\s*command\s*=\s*(['"])(.*)\1\s*$""")
_TOML_ARGS = re.compile(r"^\s*args\s*=\s*(\[.*\])\s*$")


def _codex_read() -> dict:
    """The ``[mcp_servers.builders-gate]`` table.

    Parsed with ``tomllib`` when it is there (3.11+) and with a line scan when
    it is not — this project supports 3.10, and a setup panel that goes blank on
    the oldest supported interpreter is a setup panel that fails exactly where
    setup is hardest. The scan reads only ``command`` and ``args`` and is
    explicitly not a TOML parser; anything it cannot read is reported as
    unreadable rather than as absent.
    """
    path = _codex_config()
    out: dict[str, Any] = {"found": False, "path": str(path), "scope": "user",
                           "command": "", "args": [], "error": ""}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        out["error"] = "no ~/.codex/config.toml yet — Codex writes it on first run"
        return out

    try:
        import tomllib
    except ImportError:
        tomllib = None                                           # noqa: N806
    if tomllib is not None:
        try:
            doc = tomllib.loads(raw)
        except Exception as exc:                                 # noqa: BLE001
            out["error"] = f"{path} would not parse as TOML: {exc}"
            return out
        entry = (doc.get("mcp_servers") or {}).get(SERVER)
        if isinstance(entry, dict):
            args = entry.get("args")
            out.update(found=True, command=str(entry.get("command") or ""),
                       args=[str(a) for a in args] if isinstance(args, list) else [])
        return out

    inside = False
    for line in raw.splitlines():
        header = _TOML_TABLE.match(line)
        if header:
            inside = (header.group(1) or header.group(2) or "").strip() == SERVER
            if inside:
                out["found"] = True
            continue
        if not inside:
            continue
        got = _TOML_STR.match(line)
        if got:
            out["command"] = got.group(2)
            continue
        got = _TOML_ARGS.match(line)
        if got:
            try:
                out["args"] = [str(a) for a in json.loads(got.group(1))]
            except ValueError:
                pass
    return out


def _codex_argv(exe: str, interpreter: str) -> list[str]:
    return [exe, "mcp", "add", SERVER, "--", interpreter, *MODULE_ARGS]


def _codex_unregister(exe: str) -> list[str]:
    return [exe, "mcp", "remove", SERVER]


WIRINGS: dict[str, Wiring] = {
    "claude": Wiring(
        id="claude", label="Claude Code",
        config=_claude_config, read=_claude_read, argv=_claude_argv,
        how="Registers the Builders Gate MCP server at USER scope, so every "
            "game project on this machine gets the tools — including projects "
            "that do not exist yet.",
        scope_note="user scope · ~/.claude.json"),
    "codex": Wiring(
        id="codex", label="Codex CLI",
        config=_codex_config, read=_codex_read, argv=_codex_argv,
        how="Writes a [mcp_servers.builders-gate] table into Codex's own "
            "config. Separate from the per-run wiring a dispatched Codex agent "
            "gets — that one is injected in memory and leaves nothing behind, "
            "so it does not help your own `codex` sessions.",
        scope_note="user scope · ~/.codex/config.toml"),
}

_UNREGISTER = {"claude": _claude_unregister, "codex": _codex_unregister}


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

def interpreter() -> str:
    """The interpreter a registration SHOULD name: the one running this process.

    Same value ``runners.mcp_overrides`` uses and the same value
    ``bgate_cli._pin`` writes into the hook, for the same reason all three give:
    it is the environment ``pip install -e .`` ran in, and a bare name resolves
    against whatever is first on PATH when the CLI fires.
    """
    return sys.executable


def _same_file(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
            os.path.realpath(b))
    except OSError:
        return os.path.normcase(a) == os.path.normcase(b)


def _judge(entry: dict) -> dict:
    """Which of the four wiring states this registration is in.

    ``pinned`` is the only good one. The other three all render as "registered"
    in the CLI's own listing, which is exactly why they are worth separating
    here — the whole point of this row is to distinguish a registration that
    works from one that looks identical and does not.
    """
    if not entry.get("found"):
        return {"state": "absent",
                "verdict": "not registered — your own sessions of this CLI have "
                           "no Builders Gate tools",
                "ok": False}
    command = str(entry.get("command") or "")
    args = list(entry.get("args") or [])
    if _is_bare(command):
        return {"state": "bare",
                "verdict": f"registered, but its command is a bare '{command}'. "
                           "This is the documented Windows failure: the CLI "
                           "resolves that against whatever is first on PATH when "
                           "it launches the server, which is routinely not the "
                           "environment Builders Gate was installed into. It "
                           "reports 'failed to connect' and points nowhere near "
                           "the interpreter.",
                "ok": False}
    if args and args != MODULE_ARGS:
        return {"state": "odd-args",
                "verdict": f"registered, but it runs {' '.join(args)} rather "
                           f"than {' '.join(MODULE_ARGS)}. That may be "
                           "deliberate; nothing here will change it without "
                           "being asked.",
                "ok": False}
    if not _same_file(command, interpreter()):
        return {"state": "other-interpreter",
                "verdict": f"registered against a different interpreter than "
                           f"this dashboard is running on. It will work if that "
                           f"one also has Builders Gate installed, and fail with "
                           f"'failed to connect' if it does not. Registered: "
                           f"{command}",
                "ok": False}
    return {"state": "pinned",
            "verdict": "registered at user scope, pinned to the same interpreter "
                       "this dashboard runs on",
            "ok": True}


def command_line(runner_id: str) -> str:
    """The command a human would type, for the copy button and for the docs.

    Shown even when the button is available: this is the line in CLAUDE.md and
    in every support answer, and a user who wants to know what a button did is
    owed the ability to read it.
    """
    one = WIRINGS.get(runner_id)
    if not one:
        return ""
    return " ".join(_quote(part) for part in one.argv(runner_id, interpreter()))


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part else part


def status() -> list[dict]:
    """Every coding-agent CLI: installed, wired, and what is wrong if anything.

    Never raises and never blocks on a subprocess — every fact here comes from a
    ``shutil.which`` and a file read.
    """
    found = _runners.available()
    rows = []
    for runner_id, runner in _runners.RUNNERS.items():
        one = WIRINGS.get(runner_id)
        detected = found.get(runner_id) or {}
        try:
            entry = one.read() if one else {}
        except Exception as exc:                                 # noqa: BLE001
            entry = {"found": False, "error": f"{type(exc).__name__}: {exc}"}
        judged = _judge(entry) if one else {
            "state": "unknown", "ok": False,
            "verdict": "this CLI has no MCP wiring described here yet"}
        rows.append({
            "id": runner_id,
            "label": one.label if one else runner_id,
            "installed": bool(detected.get("installed")),
            "path": str(detected.get("path") or ""),
            "note": runner.note,
            "steerable": bool(runner.steerable),
            "cost_tracked": bool(runner.cost_tracked),
            "requires_git_repo": bool(runner.requires_git_repo),
            "used_for": ("Dispatched board agents run on this, and it is what "
                         "the brainstorm room talks to."
                         if runner_id == _runners.DEFAULT_RUNNER
                         else "An alternative runner for dispatched agents."),
            "default_runner": runner_id == _runners.DEFAULT_RUNNER,
            "mcp": {
                **entry,
                **judged,
                "server": SERVER,
                "expected_command": interpreter(),
                "expected_args": list(MODULE_ARGS),
                "how": one.how if one else "",
                "scope_note": one.scope_note if one else "",
                "command_line": command_line(runner_id),
                "can_register": bool(detected.get("installed")) and bool(one),
            },
        })
    return rows


def payload() -> dict:
    return {
        "runners": status(),
        "interpreter": interpreter(),
        "server": SERVER,
        # The one fact that makes the whole section legible: WHY an absolute
        # path. Stated once, here, rather than in the three places it is shown.
        "why_absolute": (
            "A bare `python` resolves against whatever is first on PATH when "
            "the CLI launches the server — routinely not the environment "
            "Builders Gate was installed into. The CLI then reports 'failed to "
            "connect', which points nowhere near the interpreter. Every "
            "registration written from here names this exact interpreter."),
    }


# ---------------------------------------------------------------------------
# The two writes
# ---------------------------------------------------------------------------

def _run(argv: list[str], timeout: int) -> dict:
    """One bounded subprocess, argv list, no shell, stdin closed.

    NO SHELL, EVER: the interpreter path contains spaces on the supported
    platform and a shell string is one quoting mistake from executing something
    else. stdin=DEVNULL for the reason the adapters give — under a stdio MCP
    server an inherited stdin is the client's protocol channel.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return {"ok": False, "output": f"{argv[0]} is not on PATH any more"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"timed out after {timeout}s"}
    except Exception as exc:                                     # noqa: BLE001
        return {"ok": False, "output": f"{type(exc).__name__}: {exc}"}
    text = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    return {"ok": proc.returncode == 0, "code": proc.returncode,
            "output": text[-1200:]}


def register(runner_id: str) -> dict:
    """Register (or re-register) the Builders Gate MCP server for one CLI.

    A CONFIG WRITE, PERFORMED BY THE CLI THAT OWNS THE CONFIG. The existing
    entry is removed first so this is idempotent and so it can repair the broken
    states :func:`_judge` names — ``mcp add`` refuses a name it already holds,
    which would make the one button that fixes a bad registration the one button
    that cannot.

    The caller gates this on a human. Registering an MCP server changes what
    every future session of that CLI can do, on this whole machine.
    """
    one = WIRINGS.get(runner_id)
    if not one:
        return {"ok": False, "error": f"no MCP wiring described for '{runner_id}'"}
    runner = _runners.RUNNERS.get(runner_id)
    exe = runner.find() if runner else None
    if not exe:
        return {"ok": False,
                "error": f"the {runner_id} CLI is not on PATH, so there is "
                         "nothing to register into"}
    before = one.read()
    removed = None
    if before.get("found"):
        removed = _run(_UNREGISTER[runner_id](exe), REGISTER_TIMEOUT)
    added = _run(one.argv(exe, interpreter()), REGISTER_TIMEOUT)
    after = one.read()
    judged = _judge(after)
    if not added["ok"] and not judged["ok"]:
        return {"ok": False,
                "error": (f"`{one.label} mcp add` failed: "
                          + (added.get("output") or "no output")),
                "command_line": command_line(runner_id),
                "removed": removed}
    return {"ok": True, "state": judged["state"], "verdict": judged["verdict"],
            "output": added.get("output", ""), "entry": after}


def unregister(runner_id: str) -> dict:
    one = WIRINGS.get(runner_id)
    runner = _runners.RUNNERS.get(runner_id)
    exe = runner.find() if runner else None
    if not one or not exe:
        return {"ok": False, "error": f"the {runner_id} CLI is not available"}
    got = _run(_UNREGISTER[runner_id](exe), REGISTER_TIMEOUT)
    after = one.read()
    return {"ok": not after.get("found"), "output": got.get("output", ""),
            "entry": after}


def verify(runner_id: str) -> dict:
    """Ask the REGISTERED interpreter whether it can actually import the server.

    This is the check that separates a registration that works from one that
    only looks right, and it is the reason the interpreter comparison is not the
    last word: a different interpreter is fine if Builders Gate is installed in
    it, and this is the only way to find that out short of starting a session
    and watching a tool call fail.

    Human-gated by the caller, because it executes the command the config names.
    It runs a one-line import and prints nothing but a path — it does not start
    the MCP server, which would sit on stdin forever.
    """
    one = WIRINGS.get(runner_id)
    if not one:
        return {"ok": False, "error": f"no MCP wiring described for '{runner_id}'"}
    entry = one.read()
    command = str(entry.get("command") or "")
    if not entry.get("found") or not command:
        return {"ok": False,
                "error": "nothing is registered for this CLI, so there is no "
                         "interpreter to ask"}
    if _is_bare(command):
        return {"ok": False, "command": command,
                "error": "the registration names a bare interpreter, and this "
                         "process cannot reproduce how the CLI would resolve "
                         "it — that unpredictability IS the bug. Re-register "
                         "to pin it."}
    got = _run([command, "-c",
                "import bgate_mcp.server, sys; print(sys.executable)"],
               VERIFY_TIMEOUT)
    if got["ok"]:
        return {"ok": True, "command": command,
                "detail": "that interpreter imports the Builders Gate MCP "
                          "server cleanly — the registration is live",
                "output": got.get("output", "")}
    return {"ok": False, "command": command,
            "error": "that interpreter cannot import bgate_mcp — this is the "
                     "'failed to connect' state, seen from the inside",
            "output": got.get("output", "")}


# What each bad state means, in one clause, for the doctor line. The panel gets
# the full paragraph from _judge; a report row gets the short form of the same
# fact so the two never say different things about one registration.
_SHORT = {
    "absent": "not registered — its own sessions have no Builders Gate tools",
    "bare": "registered against a bare `python`, which resolves against PATH "
            "at launch — the documented 'failed to connect'",
    "other-interpreter": "registered against a different interpreter than this "
                         "one; it works only if Builders Gate is installed "
                         "there too",
    "odd-args": "registered, but running something other than "
                "`-m bgate_mcp.server`",
    "unknown": "no MCP wiring is described for this CLI",
}


def doctor_row() -> dict:
    """One optional row: is at least one coding-agent CLI correctly wired.

    Green needs BOTH halves. "Installed" alone was the only half anything ever
    checked, and it is the half that is almost never the problem.
    """
    try:
        rows = status()
    except Exception as exc:                                     # noqa: BLE001
        return {"name": "agent_cli", "available": False, "optional": True,
                "detail": f"{type(exc).__name__}: {exc}"}
    good = [r["label"] for r in rows if r["installed"] and r["mcp"].get("ok")]
    if good:
        return {"name": "agent_cli", "available": True, "optional": True,
                "detail": ", ".join(good) + " wired to this interpreter"}
    installed = [r for r in rows if r["installed"]]
    if not installed:
        return {"name": "agent_cli", "available": False, "optional": True,
                "detail": "no coding-agent CLI found on PATH — the board can "
                          "file work but nothing can be dispatched"}
    return {
        "name": "agent_cli", "available": False, "optional": True,
        "detail": "; ".join(
            f"{r['label']} {_SHORT.get(r['mcp'].get('state'), r['mcp'].get('state'))}"
            for r in installed)
        + " — fix it in Settings → Agent CLIs",
    }


def find_optional(runner_id: str) -> Optional[str]:
    """The resolved CLI path, or None. Thin wrapper so callers do not reach into
    ``runners.RUNNERS`` for a lookup this module already does."""
    runner = _runners.RUNNERS.get(runner_id)
    return runner.find() if runner else None
