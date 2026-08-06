"""Which CLI a dispatched agent actually runs on.

Dispatch was one CLI hardcoded in one function: `claude`, with claude's flags,
emitting claude's stream-json, parsed by a reader that knew those event names.
That was correct while there was one runner and became the obstacle the moment
there were two.

WHY A SECOND RUNNER AT ALL, given the first one works: the Codex CLI generates
images natively. Every other seat wants the engine, the parser and the ledger
this tool already has; the art seat is the one place where the runner's OWN
capability is the point. So this is deliberately not "pluggable agents" — it is
one seat, one alternative, and a table that says exactly how the two differ.

WHAT A RUNNER MUST DECLARE, because dispatch's behaviour depends on all four
and guessing any of them produces a silent failure rather than an error:

    steerable      claude keeps stdin open as a live channel, so agent_steer can
                   inject a user turn mid-run. `codex exec` reads stdin ONCE, as
                   an appended block, and closes it. A steer sent to a Codex
                   agent goes nowhere, so the caller has to be told rather than
                   left watching a message that will never land.

    cost_tracked   claude reports total_cost_usd per turn, which is what the
                   per-item ceiling at _observed_cost kills a runaway on. Codex
                   reports TOKENS and no price. A price table would be a number
                   in this repo going stale against OpenAI's, silently
                   under-reporting until somebody notices the bill — so there
                   is none, the ceiling is declared not to apply, and the run
                   says so everywhere it is shown. An untracked cost that
                   ANNOUNCES itself is recoverable; one that reads as $0.00 is
                   the thing that empties an account overnight.

    prompt_via     "stream" keeps the pipe open for the life of the run;
                   "stdin_once" writes the prompt and closes, which is the only
                   thing `codex exec` will act on.

    events         the log parser needs to know which vocabulary it is reading.
                   The two do not collide (`assistant`/`user`/`result` versus
                   `thread.started`/`item.completed`/`turn.completed`), so one
                   reader handles a log of either kind without being told which.

THE SANDBOX IS NOT BYPASSED. Codex runs under `--sandbox workspace-write` with
`--cd` at the project, which was verified to write to the real directory rather
than the sandbox's shadow copy. That distinction is worth stating because the
shadow is the default OUTSIDE a git repo: a run started with
`--skip-git-repo-check` on a non-repo directory reports every write as
successful and leaves the real tree untouched. `requires_git_repo` is that
finding turned into a precondition.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

# The MCP server every seat needs whichever CLI it runs on. An art agent that
# cannot call ref_list, asset_lock, consistency_check or artifact register is
# not an art seat, it is an image generator with a prompt — the whole point of
# routing a seat here is that ONLY the generation step changes.
MCP_SERVER_NAME = "builders-gate"


def _npm_shim(name: str) -> Optional[str]:
    """Windows npm installs a `.cmd` shim that `shutil.which` finds only when
    the npm prefix is on PATH — and it is not, in a service started from an
    IDE or a scheduled task. Codex lives here on this platform, so a runner
    reported "not installed" while `npm ls -g` listed it."""
    if sys.platform != "win32":
        return None
    shim = Path(os.environ.get("APPDATA", "")) / "npm" / f"{name}.cmd"
    return str(shim) if shim.is_file() else None


def find_claude() -> Optional[str]:
    exe = shutil.which("claude")
    if exe:
        return exe
    fallback = Path.home() / ".local" / "bin" / (
        "claude.exe" if sys.platform == "win32" else "claude")
    return str(fallback) if fallback.exists() else None


def find_codex() -> Optional[str]:
    return shutil.which("codex") or _npm_shim("codex")


def mcp_overrides(server_name: str = MCP_SERVER_NAME) -> list[str]:
    """Register the Builders Gate MCP server for ONE invocation.

    `-c` overlays config.toml in memory, so this never edits the user's
    ~/.codex/config.toml. That is deliberate: the dashboard writing into a
    config the user also hand-edits is a merge nobody asked it to perform, and
    an entry left behind after an experiment is worse than one that has to be
    re-passed. Verified with `codex mcp list -c mcp_servers.…`, which shows the
    injected server alongside the persistent ones.

    The interpreter is THIS process's, not a bare `python`: the same absolute
    path the install docs insist on, for the same reason — a bare name resolves
    differently under a spawned CLI than in the shell and the failure reads as
    "server not connected" with nothing pointing at the interpreter.
    """
    return [
        "-c", f'mcp_servers.{server_name}.command={_toml_str(sys.executable)}',
        "-c", f'mcp_servers.{server_name}.args=["-m","bgate_mcp.server"]',
    ]


def _toml_str(value: str) -> str:
    """A TOML literal string. `-c` parses the value as TOML, and a Windows path
    in a basic string turns \\U into a bad unicode escape and fails the parse."""
    return "'" + str(value).replace("'", "") + "'"


@dataclass(frozen=True)
class Runner:
    name: str
    find: Callable[[], Optional[str]]
    steerable: bool
    cost_tracked: bool
    prompt_via: str                     # "stream" | "stdin_once"
    requires_git_repo: bool = False
    # Why this runner might be chosen over the default, shown in Settings.
    note: str = ""
    build_args: Callable[..., list[str]] = field(repr=False, default=None)


def _claude_args(exe: str, *, permission_mode: str, model: Optional[str],
                 cwd: str, native_images: bool) -> list[str]:
    """Unchanged from the single-runner era, on purpose.

    stream-json OUTPUT makes claude emit one NDJSON event per step AS IT WORKS
    instead of buffering to the end, which is what feeds the live activity view.
    stream-json INPUT keeps stdin open as a channel so steer() can inject user
    turns while the agent runs. --replay-user-messages echoes those back into
    the log so they appear in the feed.

    `native_images` is ignored here and that is not an oversight: Claude Code
    has no image generation of its own, so there is nothing to switch off. The
    parameter stays in the signature so the two runners share one call site —
    a caller that had to know which runner accepts which argument would be the
    hardcoding this module exists to remove.
    """
    return [exe, "-p", "--permission-mode", permission_mode,
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--replay-user-messages",
            "--allowedTools", f"mcp__{MCP_SERVER_NAME}", "Read", "Edit", "Write",
            "Glob", "Grep", "Bash"] + (["--model", model] if model else [])


def _codex_args(exe: str, *, permission_mode: str, model: Optional[str],
                cwd: str, native_images: bool) -> list[str]:
    """`codex exec`, JSONL, sandboxed to the project directory.

    --sandbox workspace-write --cd <project> writes to the REAL tree. Verified
    rather than assumed: an identical run outside a git repo (with
    --skip-git-repo-check) wrote into a shadow cwd under
    ~/.codex/.sandbox/cwd/<hash> and reported success, which is why
    requires_git_repo is a precondition and why --skip-git-repo-check is not
    passed here however convenient it looks.

    --disable image_generation IS THE SETTING. `art.image_backend=bgate` turns
    the CLI's own image tool off at the process boundary, so the agent cannot
    quietly use it instead of image_generate — which matters because the bgate
    path is the one carrying the pinned references, the style, the consistency
    check and the artifact ledger. Prompt text asking it not to would be a
    request; a missing tool is a fact.
    """
    args = [exe, "exec", "--json", "--sandbox", "workspace-write", "--cd", cwd]
    args += mcp_overrides()
    args += ["--enable" if native_images else "--disable", "image_generation"]
    if model:
        args += ["--model", model]
    return args


# `find` is late-bound through this module's own globals rather than holding the
# function object, so monkeypatching `runners.find_claude` (which the dispatch
# tests do, to stand a fake CLI up on disk) is actually seen by the table.
RUNNERS: dict[str, Runner] = {
    "claude": Runner(
        name="claude", find=lambda: find_claude(), steerable=True, cost_tracked=True,
        prompt_via="stream", build_args=_claude_args,
        note="The default. Live steering and per-run cost tracking both work."),
    "codex": Runner(
        name="codex", find=lambda: find_codex(), steerable=False, cost_tracked=False,
        prompt_via="stdin_once", requires_git_repo=True, build_args=_codex_args,
        note="Generates images natively. No live steering, and it reports "
             "tokens rather than dollars — the per-run cost ceiling cannot "
             "bite, so runs on it are marked cost-not-tracked."),
}

DEFAULT_RUNNER = "claude"


def get(name: str) -> Runner:
    """The named runner, or the default. Never raises on an unknown name: this
    is read from stored settings, and a typo there must not take the board down
    — it falls back to the runner that always works."""
    return RUNNERS.get((name or "").strip().lower() or DEFAULT_RUNNER,
                       RUNNERS[DEFAULT_RUNNER])


def available() -> dict[str, dict]:
    """Every runner and whether its CLI is actually on this machine — what the
    Settings panel needs to grey out an option instead of offering one that
    fails at dispatch."""
    out = {}
    for key, runner in RUNNERS.items():
        exe = runner.find()
        out[key] = {"name": key, "installed": bool(exe), "path": exe or "",
                    "steerable": runner.steerable,
                    "cost_tracked": runner.cost_tracked,
                    "note": runner.note}
    return out


_LOOK_IT_UP = object()   # "the caller did not resolve one", NOT "found nothing"


def preflight(runner: Runner, cwd: str, exe=_LOOK_IT_UP) -> Optional[str]:
    """The reason this runner cannot start here, or None.

    Checked BEFORE a process exists, because both failures it catches are
    silent afterwards: a missing CLI is an exec error nobody reads, and a
    non-repo working directory makes every write land in a sandbox shadow and
    report success.

    ``exe`` lets the caller pass a path it already resolved — dispatch keeps
    the claude lookup on its own module (see dispatch._executable), so asking
    the table again here could disagree with what is about to be launched. A
    caller that passes None is saying IT FOUND NOTHING, which is why the
    sentinel exists: falling back to the table on a falsy value would look the
    CLI up a second way, pass, and hand Popen a None argv[0].
    """
    resolved = runner.find() if exe is _LOOK_IT_UP else exe
    if not resolved:
        # Wording kept as "<name> CLI not found on PATH" because that sentence
        # is what the dispatch contract has always answered and what callers
        # match on; the only thing added is WHICH cli, now that there are two.
        return (f"{runner.name} CLI not found on PATH"
                + (" (npm installs it as codex.cmd under %APPDATA%\\npm, which "
                   "is not always on PATH)" if runner.name == "codex" else ""))
    try:
        safe_cwd = Path(cwd).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return f"{cwd} is not a valid working directory"
    if runner.requires_git_repo and not (safe_cwd / ".git").exists():
        return (f"{cwd} is not a git repository, and {runner.name} sandboxes a "
                "non-repo working directory to a shadow copy — every write "
                "would report success and change nothing here")
    return None
