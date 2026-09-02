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
  * IT DOES NOT DUPLICATE :mod:`bgate_ui.agents.runners`. That module is the registry —
    which CLIs exist, how to find them, what each one can do — and
    ``runners.available()`` is the installed/path detection. This adds only the
    half runners has no opinion about: how each CLI persistently registers an
    MCP server for the user's OWN interactive sessions, which is a different
    thing from ``runners.mcp_overrides()`` (that is per-invocation, in memory,
    for a dispatched agent, and deliberately leaves nothing behind).

ANOTHER CLIENT IS ONE ENTRY in :data:`WIRINGS`. It does NOT have to be a
``runners.RUNNERS`` entry: dispatching work to a CLI and letting the human's own
sessions of it call the Builders Gate tools are different capabilities, and the
second one is the whole reason somebody installs this. A wiring that is not a
runner declares its own ``find`` and reports "wiring only — the board cannot
dispatch to it", which is a true sentence rather than an absence.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from bgate_ui.agents import runners as _runners

# Windows: never flash a console window out of a dashboard request.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

SERVER = _runners.MCP_SERVER_NAME

# HOW TO START THE SERVER, WHICH IS NOT THE SAME SENTENCE IN BOTH BUILDS.
#
# From source, `command` is a real interpreter and `-m bgate_mcp.server` is the
# module to run. Frozen, sys.executable IS BuildersGate.exe — there is no
# interpreter to hand a module to — so the registration named the app and the
# CLI ran `BuildersGate.exe -m bgate_mcp.server`. The launcher read a leading
# dash as "no command given" and opened the DESKTOP APP, which is why inviting
# a seat into a brainstorm room produced "Builders Gate is already running"
# instead of a participant.
#
# One constant, so registration, verification and repair cannot disagree about
# what a correct entry looks like: everything downstream compares against this.
FROZEN = bool(getattr(sys, "frozen", False))
MODULE_ARGS = ["mcp"] if FROZEN else ["-m", "bgate_mcp.server"]

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
    """How one client persistently registers an MCP server, and where that lands.

    TWO KINDS, AND THE DIFFERENCE IS WHO PERFORMS THE WRITE.

      ``kind="cli"``   the client ships a ``mcp add`` subcommand, so the tool
                       that owns the format writes the format and we only hand
                       it an argv. This is the only kind with a button.
      ``kind="file"``  the client has no such subcommand and its registration
                       lives in a JSON file the human also edits by hand. We
                       READ it — which is where the whole bare-interpreter
                       verdict comes from, and it is just as wrong in Cursor's
                       mcp.json as in Claude's — and we render the exact block
                       to paste. We do not write it. The module docstring's
                       "IT DOES NOT HAND-EDIT THEIR CONFIG" is not softened by
                       there being more clients; it is the reason this kind
                       exists instead of a merge nobody asked for.

    ``find`` is how the client is detected when it is NOT one of
    :data:`bgate_ui.agents.runners.RUNNERS` — a client we can wire but cannot dispatch
    to is a first-class row here, because "can my own Cursor session see the
    Builders Gate tools" is a real question and the dispatch table has no
    opinion on it. Runners keep using their own ``find``.
    """

    id: str
    label: str
    # The config file a human would open. Shown, written only by the client.
    config: Callable[[], Path] = field(repr=False, default=lambda: Path())
    read: Callable[[], dict] = field(repr=False, default=lambda: {})
    # exe, interpreter -> argv. The CLI's own subcommand does the write.
    argv: Callable[[str, str], list[str]] = field(
        repr=False, default=lambda exe, py: [])
    how: str = ""
    scope_note: str = ""
    kind: str = "cli"                       # "cli" | "file"
    # Detection for clients runners.RUNNERS has never heard of. None -> runner.
    find: Callable[[], Optional[str]] = field(repr=False, default=None)
    # interpreter -> the block a human pastes. "file" kind only.
    block: Callable[[str], str] = field(repr=False, default=None)
    install_hint: str = ""
    # What a human types, when that differs from the row id: `code`, not
    # `vscode`. Display only — register() execs the path `find` resolved.
    exe_name: str = ""


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


# ---------------------------------------------------------------------------
# Every other client: one JSON file, three spellings of the same object
# ---------------------------------------------------------------------------
#
# Cursor, Windsurf, VS Code and opencode all store MCP servers as JSON. They
# disagree about the KEY ("mcpServers" / "servers" / "mcp") and opencode
# disagrees about the ENTRY too (one `command` array rather than a command plus
# args). None of that changes the judgement — a bare interpreter is the same bug
# in all four files — so the reader is one function parameterised by those two
# facts, and _judge is untouched.

def _json_entry(path: Path, key: str, *, shape: str = "command_args",
                missing: str = "") -> dict:
    """The builders-gate entry inside ``path``, normalised to command + args."""
    out: dict[str, Any] = {"found": False, "path": str(path), "scope": "user",
                           "command": "", "args": [], "error": ""}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        out["error"] = missing or f"no {path} yet — the client writes it on first run"
        return out
    try:
        doc = json.loads(raw)
    except ValueError:
        # Several of these files legally carry // comments. Say that rather
        # than "absent", because absent is the one verdict that would send a
        # user to re-register a registration they already have.
        out["error"] = (f"{path} is not plain JSON (comments or a trailing "
                        "comma?), so this cannot read it — open it yourself")
        return out
    if not isinstance(doc, dict):
        out["error"] = f"{path} is not an object"
        return out
    entry = (doc.get(key) or {})
    entry = entry.get(SERVER) if isinstance(entry, dict) else None
    if not isinstance(entry, dict):
        return out
    if shape == "command_list":
        raw_cmd = entry.get("command")
        parts = ([str(p) for p in raw_cmd] if isinstance(raw_cmd, list)
                 else ([str(raw_cmd)] if raw_cmd else []))
        out.update(found=True, command=parts[0] if parts else "",
                   args=parts[1:])
        return out
    args = entry.get("args")
    out.update(found=True, command=str(entry.get("command") or ""),
               args=[str(a) for a in args] if isinstance(args, list) else [])
    return out


def _seen(exe: str = "", *paths) -> Callable[[], Optional[str]]:
    """Detection for a client that may not put anything on PATH.

    PATH FIRST, THEN WHAT IT LEFT ON DISK, and the second is a WEAKER CLAIM
    labelled as one: an editor's config directory says it has run here at least
    once, which is the only thing a GUI client reliably leaves behind. It is
    enough to decide whether to show a row and to print a paste-in block. It is
    never used to claim something is dispatchable — that answer comes from
    ``runners.RUNNERS`` and nowhere else.
    """
    def go() -> Optional[str]:
        hit = shutil.which(exe) if exe else None
        if hit:
            return hit
        for path in paths:
            candidate = path() if callable(path) else path
            if candidate and Path(candidate).exists():
                return str(candidate)
        return None
    return go


def _paste(interpreter: str, key: str, *, shape: str = "command_args",
           indent: int = 2) -> str:
    """The exact block to paste, with this interpreter already in it."""
    if shape == "command_list":
        entry: dict[str, Any] = {"type": "local",
                                 "command": [interpreter, *MODULE_ARGS],
                                 "enabled": True}
    else:
        entry = {"command": interpreter, "args": list(MODULE_ARGS)}
    return json.dumps({key: {SERVER: entry}}, indent=indent)


# Cursor — ~/.cursor/mcp.json
def _cursor_config() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def _cursor_read() -> dict:
    return _json_entry(_cursor_config(), "mcpServers",
                       missing="Cursor has no ~/.cursor/mcp.json yet — it "
                               "writes one the first time you add a server")


# Windsurf — ~/.codeium/windsurf/mcp_config.json
def _windsurf_config() -> Path:
    return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"


def _windsurf_read() -> dict:
    return _json_entry(_windsurf_config(), "mcpServers",
                       missing="Windsurf has no mcp_config.json yet")


# opencode — ~/.config/opencode/opencode.json, and a different entry shape
def _opencode_config() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(root) / "opencode" / "opencode.json"


def _opencode_read() -> dict:
    return _json_entry(_opencode_config(), "mcp", shape="command_list",
                       missing="opencode has no opencode.json yet")


# VS Code / GitHub Copilot — user mcp.json, written by `code --add-mcp`
def _vscode_config() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or (Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "Code" / "User" / "mcp.json"


def _vscode_read() -> dict:
    # "servers", not "mcpServers". VS Code is the one that renamed the key.
    return _json_entry(_vscode_config(), "servers",
                       missing="VS Code has no user mcp.json yet")


def _vscode_argv(exe: str, interpreter: str) -> list[str]:
    # `--add-mcp` takes ONE json argument carrying the name, and it upserts —
    # which is why there is no remove step below. Verified against the flag
    # this VS Code build advertises in `code --help`.
    return [exe, "--add-mcp", json.dumps(
        {"name": SERVER, "command": interpreter, "args": list(MODULE_ARGS)})]


# Gemini CLI — ~/.gemini/settings.json, written by `gemini mcp add`
def _gemini_config() -> Path:
    return Path.home() / ".gemini" / "settings.json"


def _gemini_read() -> dict:
    return _json_entry(_gemini_config(), "mcpServers",
                       missing="no ~/.gemini/settings.json yet — the CLI "
                               "writes it on first run")


def _gemini_argv(exe: str, interpreter: str) -> list[str]:
    # `-s user`, because the default scope is the CURRENT PROJECT and a
    # registration that only works in the directory the user happened to be
    # standing in is the same class of surprise this whole module exists for.
    return [exe, "mcp", "add", "-s", "user", SERVER, interpreter, *MODULE_ARGS]


def _gemini_unregister(exe: str) -> list[str]:
    return [exe, "mcp", "remove", "-s", "user", SERVER]


def _find(name: str) -> Callable[[], Optional[str]]:
    return lambda: shutil.which(name)


WIRINGS: dict[str, Wiring] = {
    "claude": Wiring(
        id="claude", label="Claude Code",
        config=_claude_config, read=_claude_read, argv=_claude_argv,
        how="Registers the Builders Gate MCP server at USER scope, so every "
            "game project on this machine gets the tools — including projects "
            "that do not exist yet.",
        scope_note="user scope · ~/.claude.json",
        install_hint="https://claude.com/claude-code"),
    "codex": Wiring(
        id="codex", label="Codex CLI",
        config=_codex_config, read=_codex_read, argv=_codex_argv,
        how="Writes a [mcp_servers.builders-gate] table into Codex's own "
            "config. Separate from the per-run wiring a dispatched Codex agent "
            "gets — that one is injected in memory and leaves nothing behind, "
            "so it does not help your own `codex` sessions.",
        scope_note="user scope · ~/.codex/config.toml",
        install_hint="npm i -g @openai/codex"),
    "gemini": Wiring(
        id="gemini", label="Gemini CLI",
        config=_gemini_config, read=_gemini_read, argv=_gemini_argv,
        find=_find("gemini"),
        how="Registers at user scope through Gemini's own `mcp add`, so every "
            "directory gets the tools rather than the one you were standing in.",
        scope_note="user scope · ~/.gemini/settings.json",
        install_hint="npm i -g @google/gemini-cli"),
    "vscode": Wiring(
        id="vscode", label="VS Code (GitHub Copilot)",
        config=_vscode_config, read=_vscode_read, argv=_vscode_argv,
        find=_find("code"), exe_name="code",
        how="Adds the server to your VS Code USER mcp.json through the "
            "editor's own `--add-mcp`, so Copilot's agent mode can call the "
            "Builders Gate tools in any workspace.",
        scope_note="user profile · Code/User/mcp.json",
        install_hint="https://code.visualstudio.com"),
    "cursor": Wiring(
        id="cursor", label="Cursor",
        kind="file", config=_cursor_config, read=_cursor_read,
        find=_seen("cursor", lambda: Path.home() / ".cursor"),
        block=lambda py: _paste(py, "mcpServers"),
        how="Cursor has no `mcp add` subcommand, so this reads the file and "
            "shows you the block — including whether an entry already there "
            "names a bare interpreter, which fails the same way it does "
            "everywhere else.",
        scope_note="all projects · ~/.cursor/mcp.json",
        install_hint="https://cursor.com"),
    "windsurf": Wiring(
        id="windsurf", label="Windsurf",
        kind="file", config=_windsurf_config, read=_windsurf_read,
        find=_seen("windsurf", lambda: Path.home() / ".codeium" / "windsurf"),
        block=lambda py: _paste(py, "mcpServers"),
        how="Windsurf writes its MCP config from the editor's own UI, so this "
            "reads it and shows you the block to paste.",
        scope_note="all projects · ~/.codeium/windsurf/mcp_config.json",
        install_hint="https://windsurf.com"),
    "opencode": Wiring(
        id="opencode", label="opencode",
        kind="file", config=_opencode_config, read=_opencode_read,
        find=_seen("opencode", _opencode_config),
        block=lambda py: _paste(py, "mcp", shape="command_list"),
        how="opencode spells an entry as one `command` ARRAY under `mcp` "
            "rather than a command plus args. The block below is already in "
            "that shape.",
        scope_note="global config · opencode.json",
        install_hint="npm i -g opencode-ai"),
}

# No entry here means `mcp add` upserts and there is nothing to undo first —
# VS Code's `--add-mcp` is that case. register() reads this with .get for
# exactly that reason; a missing remover must not be a KeyError on the one
# button that repairs a bad registration.
_UNREGISTER = {"claude": _claude_unregister, "codex": _codex_unregister,
               "gemini": _gemini_unregister}


# The paste-in block for a client nobody here has heard of. Every MCP client
# that is not one of the above still speaks this object, and a user with one is
# owed the interpreter-correct version of it rather than the docs' placeholder.
def generic_block(interpreter: str = "") -> str:
    return _paste(interpreter or globals()["interpreter"](), "mcpServers")


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
                "verdict": "not registered — your own sessions of this client "
                           "have no Builders Gate tools",
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
    if not one or one.kind != "cli":
        return ""
    # The BARE name, not the resolved path: this is the line a human types, and
    # `code --add-mcp` is what they would type even though the row's id is
    # "vscode". The resolved path is what register() actually execs.
    return " ".join(_quote(part)
                    for part in one.argv(one.exe_name or runner_id, interpreter()))


def block(runner_id: str) -> str:
    """The config block to paste, for a client that has no `mcp add`.

    Empty for a CLI-kind client, because there the answer is the command and
    two answers to one question is how a setup page stops being read.
    """
    one = WIRINGS.get(runner_id)
    if not one or one.kind != "file" or one.block is None:
        return ""
    return one.block(interpreter())


def _quote(part: str) -> str:
    """Shell-safe enough to paste, in cmd, PowerShell and bash alike.

    THE INNER QUOTES ARE THE POINT. `code --add-mcp` takes a JSON OBJECT as one
    argument, so the copied line carries `{"name": …}` inside it; wrapping that
    in bare double quotes ends the argument at the first inner quote and the
    user pastes a line that fails with a parse error nowhere near the cause.
    Escaping them as \\" is what all three shells read back as a literal quote.
    """
    if '"' in part:
        return '"' + part.replace('"', '\\"') + '"'
    return f'"{part}"' if " " in part else part


def ids() -> list[str]:
    """Every client this module knows, runners first.

    ONE ORDER, DEFINED ONCE. The panel, the CLI table and the doctor row all
    read it, so "which came first" cannot drift between them — and runners lead
    because a runner is the only kind of row that can also do work.
    """
    order = [k for k in _runners.RUNNERS if k in WIRINGS]
    return order + [k for k in WIRINGS if k not in order]


def _exe(runner_id: str) -> Optional[str]:
    """Where this client actually is, whoever knows: the runner table if it is
    a runner, the wiring's own ``find`` if it is not."""
    runner = _runners.RUNNERS.get(runner_id)
    if runner is not None:
        try:
            return runner.find()
        except Exception:                                        # noqa: BLE001
            return None
    one = WIRINGS.get(runner_id)
    if one is None or one.find is None:
        return None
    try:
        return one.find()
    except Exception:                                            # noqa: BLE001
        return None


def status() -> list[dict]:
    """Every coding-agent client: installed, wired, and what is wrong if anything.

    Never raises and never blocks on a subprocess — every fact here comes from a
    ``shutil.which``, a directory test and a file read.
    """
    found = _runners.available()
    rows = []
    for runner_id in ids():
        one = WIRINGS[runner_id]
        runner = _runners.RUNNERS.get(runner_id)
        detected = (found.get(runner_id) or {}) if runner else {}
        path = str(detected.get("path") or "") or (_exe(runner_id) or "")
        installed = bool(detected.get("installed")) if runner else bool(path)
        try:
            entry = one.read()
        except Exception as exc:                                 # noqa: BLE001
            entry = {"found": False, "error": f"{type(exc).__name__}: {exc}"}
        judged = _judge(entry)
        writable = one.kind == "cli"
        rows.append({
            "id": runner_id,
            "label": one.label,
            "installed": installed,
            "path": path,
            "note": runner.note if runner else "",
            "dispatches": bool(runner),
            "steerable": bool(runner and runner.steerable),
            "cost_tracked": bool(runner and runner.cost_tracked),
            "requires_git_repo": bool(runner and runner.requires_git_repo),
            "used_for": ("Dispatched board agents run on this, and it is what "
                         "the brainstorm room talks to."
                         if runner_id == _runners.DEFAULT_RUNNER
                         else "An alternative runner for dispatched agents."
                         if runner else
                         "Wiring only — your own sessions of it get the "
                         "Builders Gate tools; the board cannot dispatch work "
                         "to it."),
            "default_runner": runner_id == _runners.DEFAULT_RUNNER,
            "install_hint": one.install_hint,
            "mcp": {
                **entry,
                **judged,
                "server": SERVER,
                "kind": one.kind,
                "expected_command": interpreter(),
                "expected_args": list(MODULE_ARGS),
                "how": one.how,
                "scope_note": one.scope_note,
                "config_path": str(one.config()),
                "command_line": command_line(runner_id),
                "block": block(runner_id),
                # A file-kind client is never registrable from here BY DESIGN;
                # the button's absence is the promise not to merge someone's
                # hand-edited JSON, not a missing feature.
                "can_register": installed and writable,
            },
        })
    return rows


def payload() -> dict:
    return {
        "runners": status(),
        "interpreter": interpreter(),
        "server": SERVER,
        "generic_block": generic_block(),
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
    if one.kind != "cli":
        return {"ok": False, "block": block(runner_id),
                "config_path": str(one.config()),
                "error": f"{one.label} has no `mcp add` subcommand, and this "
                         "does not hand-edit a config you also edit. The block "
                         "to paste is in `block`."}
    exe = _exe(runner_id)
    if not exe:
        return {"ok": False,
                "error": f"the {runner_id} CLI is not on PATH, so there is "
                         "nothing to register into"}
    before = one.read()
    removed = None
    remover = _UNREGISTER.get(runner_id)
    if before.get("found") and remover:
        removed = _run(remover(exe), REGISTER_TIMEOUT)
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
    exe = _exe(runner_id)
    if not one or not exe:
        return {"ok": False, "error": f"the {runner_id} CLI is not available"}
    remover = _UNREGISTER.get(runner_id)
    if remover is None:
        return {"ok": False, "config_path": str(one.config()),
                "error": f"{one.label} has no remove subcommand — delete the "
                         f"'{SERVER}' entry from {one.config()} yourself"}
    got = _run(remover(exe), REGISTER_TIMEOUT)
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
    installed = [r for r in rows if r["installed"]]
    # TWO DIFFERENT SENTENCES, AND THEY MUST NOT BE MERGED. Wiring is about the
    # user's own sessions; dispatch is about the board. A machine with Cursor
    # wired and no runner installed is green here and still cannot dispatch, so
    # the dispatch half is said out loud rather than implied by the lamp.
    no_runner = not any(r["installed"] and r["dispatches"] for r in rows)
    dispatch = (" — no dispatch runner on PATH, so the board can file work but "
                "nothing runs it" if no_runner else "")
    if good:
        return {"name": "agent_cli", "available": True, "optional": True,
                "detail": ", ".join(good) + " wired to this interpreter"
                          + dispatch}
    if not installed:
        return {"name": "agent_cli", "available": False, "optional": True,
                "detail": "no coding-agent CLI or editor found — nothing on "
                          "this machine can call the Builders Gate tools. "
                          "`bgate connect` lists what it looks for."}
    return {
        "name": "agent_cli", "available": False, "optional": True,
        "detail": "; ".join(
            f"{r['label']} {_SHORT.get(r['mcp'].get('state'), r['mcp'].get('state'))}"
            for r in installed)
        + " — run `bgate connect` or fix it in Settings → Agent CLIs",
    }
