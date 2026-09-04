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
from typing import Callable, Optional

# Safe to import at module scope BECAUSE padconfig imports no MCP — see the
# note above PAD_TOOLS below, and padconfig's own docstring.
from bgate_mcp.padconfig import TOOL_NAMES as PAD_TOOLS

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

    FROZEN AND FROM SOURCE ARE DIFFERENT SENTENCES. Frozen, sys.executable is
    BuildersGate.exe rather than an interpreter, and `-m bgate_mcp.server`
    handed to it was read by the launcher as "no command given" — so
    registering an MCP server for a spawned agent opened a second copy of the
    desktop app. The frozen binary hosts the server under its own `mcp`
    subcommand instead. The answer is shared with agentcli.MODULE_ARGS so the
    two cannot drift.
    """
    from bgate_ui.agents.agentcli import MODULE_ARGS
    args = ",".join(f'"{a}"' for a in MODULE_ARGS)
    return [
        "-c", f'mcp_servers.{server_name}.command={_toml_str(sys.executable)}',
        "-c", f'mcp_servers.{server_name}.args=[{args}]',
    ]


def claude_mcp_config(server_name: str = MCP_SERVER_NAME) -> list[str]:
    """Register the Builders Gate MCP server for ONE `claude -p` invocation.

    THE HOLE THIS FILLS. `_claude_args` passed `--allowedTools mcp__<server>`
    and nothing else — AN ALLOW-LIST ENTRY FOR A SERVER IT NEVER REGISTERED.
    Whether a dispatched agent had any bgate tools at all therefore depended on
    the human's ambient config: if they had run `claude mcp add builders-gate
    --scope user` it worked, and if they had not, the allow-list permitted a
    prefix matching no tool. The agent then ran the whole item with none of the
    pipeline's tools and nothing anywhere said so.

    `_codex_args` had this right from the day codex was added — it calls
    `mcp_overrides()`. The Claude path was "unchanged from the single-runner
    era, on purpose", and that purpose stopped applying the moment a second
    runner demonstrated what the first was missing.

    `--mcp-config` takes inline JSON and applies to that invocation only, so
    this never edits ~/.claude.json — the same bargain `mcp_overrides` strikes
    with ~/.codex/config.toml, for the same reason.

    NOT `--strict-mcp-config`. That would drop every server the user configured
    themselves, and a dispatched agent losing the human's own tooling is a
    worse surprise than the one being fixed. Ours becomes guaranteed; theirs is
    left alone.
    """
    import json as _json

    from bgate_ui.agents.agentcli import MODULE_ARGS
    return ["--mcp-config", _json.dumps({"mcpServers": {
        server_name: {"command": sys.executable, "args": list(MODULE_ARGS)},
    }})]


def _toml_str(value: str) -> str:
    """A TOML literal string. `-c` parses the value as TOML, and a Windows path
    in a basic string turns \\U into a bad unicode escape and fails the parse."""
    return "'" + str(value).replace("'", "") + "'"


@dataclass(frozen=True)
class Chat:
    """How a runner holds a THINKING conversation that cannot touch the repo.

    A second shape for the same CLI, and it exists because the brainstorm room
    needed a real Claude Code session and the room's whole promise is that
    nothing is written until a human presses Deploy. A dispatched agent is the
    opposite of that by design, so the two cannot share ``build_args``.

    ``readonly_by`` is the flags that make "it cannot write" a FACT about the
    process rather than a sentence in a prompt. It is stored as text because it
    is the thing to SHOW a human who asks why they should believe the promise —
    see brainsession.thinker(), which puts it in the session payload.

    ``prompt_via`` repeats the field of the same name on Runner because the two
    can differ: a runner may be steerable while dispatched and still have no way
    to take a second conversational turn. "stream" means one process holds the
    whole conversation; "stdin_once" means every turn is a fresh process that
    has to be re-seeded with the transcript.
    """

    build_args: Callable[..., list[str]] = field(repr=False, default=None)
    prompt_via: str = "stream"
    cost_tracked: bool = True
    readonly_by: str = ""


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
    # How this runner talks WITHOUT being able to write. None means it has no
    # such mode here yet, and the brainstorm room refuses it rather than
    # guessing — see the codex entry below for exactly what an entry needs.
    chat: Optional[Chat] = None


def _claude_args(exe: str, *, permission_mode: str, model: Optional[str],
                 cwd: str, native_images: bool, max_turns: int = 0) -> list[str]:
    """The capability surface of a dispatched Claude run.

    IT REGISTERS THE MCP SERVER, which it did not do for as long as there were
    two runners: `--allowedTools mcp__builders-gate` allow-listed a prefix
    without registering the server behind it, so an agent had the pipeline's
    tools only if the human happened to have added them at user scope. See
    :func:`claude_mcp_config`.

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

    --max-turns is a SECOND ceiling, not a duplicate of the cost one. The cost
    ceiling reads total_cost_usd, which the CLI only prints at a result
    boundary; an agent grinding through a tool loop offers no such boundary and
    runs past its dollars unobserved. Turns are counted by the CLI and end the
    session on their own. 0 means the caller wants none.
    """
    return [exe, "-p", "--permission-mode", permission_mode,
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--replay-user-messages",
            "--allowedTools", f"mcp__{MCP_SERVER_NAME}", "Read", "Edit", "Write",
            "Glob", "Grep", "Bash"] \
        + claude_mcp_config() \
        + (["--model", model] if model else []) \
        + (["--max-turns", str(max_turns)] if max_turns else [])


def _codex_args(exe: str, *, permission_mode: str, model: Optional[str],
                cwd: str, native_images: bool, max_turns: int = 0) -> list[str]:
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

    `max_turns` is accepted and dropped: `codex exec` has no turn ceiling to
    pass it to. Taking the argument keeps ONE call site in dispatch — the
    alternative is the caller branching on runner name, which is the
    hardcoding this module exists to remove. Runs here are already marked
    cost-not-tracked wherever they are shown, and unbounded turns are the same
    class of fact about the same runner.
    """
    args = [exe, "exec", "--json", "--sandbox", "workspace-write", "--cd", cwd]
    args += mcp_overrides()
    args += ["--enable" if native_images else "--disable", "image_generation"]
    if model:
        args += ["--model", model]
    return args


def _codex_director_args(exe: str, *, model: Optional[str], cwd: str,
                         resume: str = "") -> list[str]:
    """A full Codex director turn, new or resumed.

    ``codex exec`` is intentionally one process per turn. Its native resume
    command preserves the conversation while avoiding a fake long-lived stdin
    channel (Codex closes stdin after one prompt). Both shapes keep the same
    workspace sandbox and Builders Gate MCP overlay as dispatched Codex work.
    """
    if not resume:
        return _codex_args(
            exe, permission_mode="acceptEdits", model=model, cwd=cwd,
            native_images=True) + ["-"]
    args = [exe, "exec", "resume", "--json"]
    args += mcp_overrides()
    args += ["--enable", "image_generation"]
    if model:
        args += ["--model", model]
    return args + [resume, "-"]


# THE FLAGS THAT MAKE A BRAINSTORM SESSION UNABLE TO WRITE.
#
# Named as a constant, and read by both the argv builder and the sentence shown
# to the human, so the two cannot drift into a promise the process is not
# keeping. Every one of these was checked against `claude --version 2.1.226` by
# reading the session's own `system/init` event, which reports the tool list and
# the MCP servers the CLI actually constructed — the model's own account of what
# tools it has is a hallucination and was observed to be one (it recited the
# standard list at a session whose init said `"tools":[]`).
#
#   --tools ""              THE guarantee. Not a permission preference: the
#                           built-in tool set is EMPTY, so there is no Write, no
#                           Edit, no Bash and no Read to grant. init reported
#                           "tools":[] and a session asked to write a file
#                           produced no file.
#   --strict-mcp-config     ONLY the servers named by --mcp-config, whatever is
#                           registered on the machine. This is the flag that
#                           matters most on a developer machine: builders-gate
#                           is registered at USER scope, so a plain `claude`
#                           spawned in a game project would inherit queue_add,
#                           bible_add and image_generate. With no --mcp-config
#                           init reported "mcp_servers":[]; with the pad
#                           document it reported exactly ["pads"] and a tool
#                           list of exactly the two pad tools.
#   --setting-sources ""    user/project/local settings cannot add tools,
#                           permissions, hooks or MCP servers back.
#   --disable-slash-commands
#                           a brainstorm message is arbitrary human text, and a
#                           message that happens to start with "/" must not be
#                           able to run a skill. init reported
#                           "slash_commands":[] and "skills":[] with this on,
#                           and both were POPULATED without it.
#   --allowedTools <the two pad tools, by full name>
#                           the belt, and it is NAMED rather than broad. With
#                           the built-in set empty and one two-tool server
#                           registered, this list and the actual tool list are
#                           the same two strings — so a server that ever got in
#                           by another route contributes nothing that is
#                           approved. Written as full names, not the
#                           `mcp__pads` prefix a dispatched agent uses, because
#                           a prefix approves whatever that server grows.
#
# WHAT IS DELIBERATELY *NOT* HERE ANY MORE, and both removals were forced by
# measurement rather than preference:
#
#   --permission-mode plan  it was the belt, and it turned out to refuse THE PAD
#                           TOOLS TOO. Observed: a session holding exactly
#                           mcp__pads__pad_read answered "I'm currently in plan
#                           mode, which blocks me from calling tools other than
#                           writing to the plan file — including the read-only
#                           pad_read call you asked for". Keeping it would have
#                           shipped a two-tool server that could never be
#                           called, which is worse than either alternative:
#                           a feature that silently does nothing reads as the
#                           model being unhelpful. The guarantee never rested on
#                           plan mode anyway — it rests on the tool set being
#                           empty and the MCP config being exhaustive, both of
#                           which are unchanged and both of which were read back
#                           off the CLI's own init event.
#   --no-session-persistence
#                           it was here on the reasoning that the transcript
#                           already lives in the project's DB and a second copy
#                           of somebody's private thinking under ~/.claude is a
#                           copy nobody asked for. That lost to a direct request
#                           — "proper cli emulation for seamless resumes" —
#                           because the flag and --resume are one switch read
#                           two ways: a session that was never persisted cannot
#                           be resumed, and re-seeding a transcript into a fresh
#                           process is a replay, not a continuation. The trade
#                           is stated rather than hidden.
#
# WHAT THE ROOM'S PROMISE NOW RESTS ON, in one sentence, because flags have come
# and gone and it should be possible to check without reading all of the above:
# the process holds exactly the tools named below, every one of which reads or
# writes a row in THIS PROJECT'S OWN DATABASE, and not one of which can file
# work, dispatch an agent, run a command or touch a file in the game.
#
# THE CANON TOOLS ARE NEW AND THE LINE THEY DO NOT CROSS IS THE SAME LINE. A
# seat can now read the bible and write to it, because a room asked to settle
# what this world is and then forbidden to write it down made the human the
# transport for their own decision. Writing canon is not filing work: the board
# is still reached only by a human pressing Deploy on a plan they have read.

# PAD_TOOLS is imported at the top of this module now. It USED TO BE A
# HAND-COPIED DUPLICATE of padserver.TOOL_NAMES, carrying a comment that the
# real one could not be imported because padserver pulls in the MCP SDK and
# this module is loaded by every dispatch — including on machines with no MCP
# extra installed — plus a test asserting the copy had not drifted. The list
# now lives in bgate_mcp.padconfig, which imports nothing but sys, so the
# reason for the duplicate is gone and so is the duplicate.

_CLAUDE_READONLY = ["--tools", "", "--strict-mcp-config",
                    "--setting-sources", "", "--disable-slash-commands",
                    "--permission-mode", "acceptEdits"]

CLAUDE_READONLY_BY = (
    'claude --tools "" (the built-in tool set is empty — no Write, no Bash, no '
    "Read), --strict-mcp-config with --mcp-config naming a two-tool pad server "
    "and nothing else (so no builders-gate and no queue_add), --allowedTools "
    'listing exactly those two tools by name, and --setting-sources "" so no '
    "settings file can add any of it back. Checked against the session's own "
    "init event, which is the only account of its tools that is not the model's")


def _claude_chat_args(exe: str, *, system: str, model: Optional[str],
                      mcp_config: str = "", resume: str = "") -> list[str]:
    """A Claude Code session that can THINK and cannot TOUCH THE PROJECT.

    Same CLI and the same stream-json channel as a dispatched agent — one
    process, stdin held open, one `result` event per turn — with the entire
    capability surface removed rather than merely unused, and then exactly two
    tools handed back. See _CLAUDE_READONLY for what each flag is doing and
    what was observed without it.

    --system-prompt REPLACES the default rather than appending to it. The
    default one describes a coding agent with a working directory and a task,
    which is exactly the wrong frame for the cheap room and is also the bulk of
    the per-turn cache write: the same trivial turn measured 9,860 cache-creation
    tokens with the stock prompt and 3,076 with this one.

    ``mcp_config`` IS THE ONLY WAY A TOOL GETS IN, and it is a JSON document
    rather than a server name — see bgate_mcp.padserver.config, which builds it.
    Paired with --strict-mcp-config it is an exhaustive statement: these servers
    and no others, whatever is registered on the machine. The pad server is two
    tools over one brainstorm session; passing "" here is a session with no
    tools at all, which is what a synthesis gets.

    ``resume`` continues a real CLI session by id instead of starting one. It is
    what makes reopening a brainstorm a CONTINUATION rather than a replay. It is
    also the flag most likely to fail for reasons outside this process — a
    pruned session store, a moved machine, a version bump — so the caller must
    treat a resumed spawn as provisional and fall back to a fresh one; see
    brainsession._collect, which detects it, and ask(), which retries once.
    """
    return [exe, "-p",
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--replay-user-messages",
            *_CLAUDE_READONLY] \
        + (["--mcp-config", mcp_config, "--allowedTools", *PAD_TOOLS]
           if mcp_config else []) \
        + (["--resume", resume] if resume else []) \
        + ["--system-prompt", system] \
        + (["--model", model] if model else [])


def _claude_director_args(exe: str, *, system: str, model: Optional[str],
                          resume: str = "") -> list[str]:
    """The director console's session: a FULL Claude Code session, held open.

    The third argv shape, and the one the console chat was missing. The other
    two are its parents: _claude_args is the capability surface (the dispatch
    tool set plus the whole builders-gate server), _claude_chat_args is the
    lifecycle (one process, stdin held open, one `result` per turn, --resume to
    continue the CLI's own conversation across dashboard restarts). This is the
    first with the second, because the human asked for exactly that: the thing
    they get by opening a terminal in the project and running `claude`, wired
    behind the console instead.

    --append-system-prompt, NOT --system-prompt. The chat shape replaces the
    default prompt because "a coding agent with a working directory" is the
    wrong frame for a read-only room. It is the RIGHT frame here — the whole
    complaint with the switchboard was a director that could not investigate —
    so the director framing is appended to the stock prompt rather than
    replacing it.

    No --max-turns. The dispatch shape carries one because a work item is a
    bounded errand; a conversation is not. Nothing bounds this one but the
    human closing it — there is no money ceiling anywhere in this product.
    """
    return [exe, "-p", "--permission-mode", "acceptEdits",
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--replay-user-messages",
            "--allowedTools", f"mcp__{MCP_SERVER_NAME}", "Read", "Edit", "Write",
            "Glob", "Grep", "Bash"] \
        + (["--resume", resume] if resume else []) \
        + ["--append-system-prompt", system] \
        + (["--model", model] if model else [])


# `find` is late-bound through this module's own globals rather than holding the
# function object, so monkeypatching `runners.find_claude` (which the dispatch
# tests do, to stand a fake CLI up on disk) is actually seen by the table.
RUNNERS: dict[str, Runner] = {
    "claude": Runner(
        name="claude", find=lambda: find_claude(), steerable=True, cost_tracked=True,
        prompt_via="stream", build_args=_claude_args,
        chat=Chat(build_args=_claude_chat_args, prompt_via="stream",
                  cost_tracked=True, readonly_by=CLAUDE_READONLY_BY),
        note="The default. Live steering and per-run cost tracking both work."),
    "codex": Runner(
        name="codex", find=lambda: find_codex(), steerable=False, cost_tracked=False,
        prompt_via="stdin_once", requires_git_repo=True, build_args=_codex_args,
        # NO CHAT ENTRY, AND THAT IS A STATEMENT RATHER THAN A GAP. Codex
        # documents `--sandbox read-only`, which is the right mechanism, but the
        # claude one above was believed only because its own init event was read
        # back and a write attempt was made and failed. Nothing here has been
        # verified that way, and a read-only claim that turns out to be wrong is
        # worse than a runner the room refuses to use.
        #
        # WHAT A CODEX (OR LOCAL-LLM) ENTRY NEEDS, so this is one row of work:
        #   build_args   argv that removes the capability rather than declining
        #                to use it — for codex `--sandbox read-only --cd <a
        #                scratch dir>` and NO mcp_overrides() call, since that
        #                helper injects the whole builders-gate server including
        #                its writers.
        #   prompt_via   "stdin_once" for `codex exec`, which reads stdin once
        #                and closes it. brainsession already handles that: it
        #                re-seeds a fresh process with the transcript per turn
        #                rather than pretending the conversation persisted.
        #   cost_tracked False for codex — it reports tokens and no price, so
        #                a run on it never says what it cost and the payload
        #                says so.
        #   readonly_by the sentence a human is shown when they ask why they
        #                should believe the room writes nothing.
        note="Generates images natively. No live steering, and it reports "
             "tokens rather than dollars, so runs on it are marked "
             "cost-not-tracked."),
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
    # NO CONTAINMENT CHECK AGAINST Path.cwd(). One was added here by a CodeQL
    # autofix and it refused every real dispatch: `cwd` is the GAME PROJECT
    # root (or its .bgate/work worktree), and the dashboard's own process
    # directory is wherever `bgate serve` was launched — the checkout, another
    # project, or C:\Windows\System32 if it started from a service. Those are
    # unrelated by design; the board serves projects it does not live inside.
    # The alert it silenced is about untrusted input reaching a path
    # expression, and the answer to that here is provenance, not containment:
    # this value comes from the registry and from make_worktree, never from a
    # request body.
    if runner.requires_git_repo and not (safe_cwd / ".git").exists():
        return (f"{cwd} is not a git repository, and {runner.name} sandboxes a "
                "non-repo working directory to a shadow copy — every write "
                "would report success and change nothing here")
    return None
