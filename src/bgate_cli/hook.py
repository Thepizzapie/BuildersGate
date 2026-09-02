"""PreToolUse hook - the teeth on seat lanes and asset locks.

Claude Code pipes the pending tool call as JSON on stdin. Exit 2 blocks the call
(stderr is shown to the model); exit 0 allows it. This hook asks the same oracle
the tools expose (seats.can_write): lane check AND lock check, both gates.

It also TAKES an advisory lease on every path it lets through, keyed to this
run's BGATE_LOCK_OWNER. Binaries lock because they cannot merge; two agents in
overlapping lanes editing one .gd is last-write-wins with no warning at all. The
lease turns that into a block that names the item already in the file.

Inert unless BOTH hold:
  * BGATE_SEAT is set in the session's environment (the identity to enforce)
  * the file being written lives under a .bgate project

BASH IS GUARDED TOO, AND ONLY PARTIALLY. Guarding Write/Edit alone was a gate
that did not gate: dispatch grants the agent Bash, so ``echo x > game/foo.gd``
walked straight past every lane and lock claim in the README. This hook now
parses Bash commands for the realistic write vectors and applies the same rules.
It cannot parse shell in general, so the contract is stated honestly:

  CAUGHT - redirections (``>`` ``>>`` ``2>`` ``&>``), cp/mv/install/ln/rsync,
    rm/rmdir/unlink/shred, tee, truncate/touch/mkdir, ``dd of=``, sed/perl
    in-place, curl -o / wget -O, git subcommands that rewrite the working tree,
    and an interpreter ``-c`` snippet that clearly writes (open(...,'w'),
    write_text, shutil.move, os.remove, writeFileSync, ...).
  FAIL-CLOSED - a command that clearly writes but whose target cannot be
    determined (an unparseable quote soup, an eval'd snippet, ``git apply``)
    is BLOCKED while the session's cwd is inside a project.
  NOT CAUGHT - anything a program does that the command line does not say:
    ``python build.py``, ``npm run x``, ``make``, a shell script, an editor.
    A determined process can still write; this stops the casual bypass, which
    is what actually happened. Do not read it as a sandbox.

CONTAINMENT IS A SEPARATE QUESTION AND IT IS ASKED FIRST. Lanes and locks answer
"what may this agent touch"; they never answered "where", because this hook used
to derive the project FROM THE WRITE TARGET. An agent dispatched for Ember that
wrote into Hollow was therefore judged against HOLLOW's seats, and one that wrote
outside every project was waved through with no check at all. bgate_core.board.aegis
answers the where-question against the root dispatch PINNED at spawn time, and
the answer is now consulted before the lane gate. See ``_contain``.

READS ARE CHECKED TOO, and only for containment. "This agent cannot touch
anything outside its project" is not a statement about writing: Read, Glob and
Grep are how another game's design docs, keys and unreleased plot end up in a
transcript. There is no lane or lock question to ask about a read - a seat may
read anything inside its own project - so those tools go through the containment
gate alone.

FAIL-SAFE RULE: this must NEVER raise or exit nonzero by accident - a crashing
hook blocks every write in the session. Any unexpected error means exit 0. That
silence used to be indistinguishable from "enforcement is off", so failing open
now LOGS (stderr + .bgate/hook.log) and ``bgate hook --selftest`` proves, live,
that the hook is installed and biting.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

# Tool → the input key that carries the file path.
_PATH_KEYS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

# Tool → the input key that carries a path being READ. These reach only the
# containment gate: a seat may read anything inside its own project, so there is
# no lane question here, and leases exist to stop a silent overwrite, which a
# read cannot cause.
#
# Glob and Grep name their directory `path` and DEFAULT IT TO THE SESSION'S OWN
# when it is absent, so a missing key is not "nothing to check" - it is the cwd,
# and that is what gets judged. What is NOT judged is the glob PATTERN: a
# pattern is not a path and expanding one here would mean walking the filesystem
# on the hook's hot path to answer a question the tool is about to answer
# anyway. A pattern that climbs out of the project reaches files that are
# themselves outside it, and each of those is a Read this hook does see.
_READ_PATH_KEYS = {
    "Read": "file_path",
    "NotebookRead": "notebook_path",
    "Glob": "path",
    "Grep": "path",
}

# Claude Code's contract: 0 allows, 2 blocks and shows stderr to the MODEL, any
# other nonzero is a non-blocking error whose stderr is shown to the HUMAN. That
# third channel is what WARN uses - the write lands, the person sees why it was
# questionable, and nothing is dammed.
ALLOW, WARN, BLOCK = 0, 1, 2

# Long enough to cover a working stretch between writes, short enough that a
# killed agent's claim clears on its own. Refreshed on every write it makes.
DEFAULT_LEASE_S = 900

LOG_NAME = "hook.log"

# ---------------------------------------------------------------------------
# THE SEATLESS SESSION - the participant this hook used to ignore completely.
#
# `if not seat: return ALLOW` was correct about identity and wrong about what
# follows from it. A session a human started has no BGATE_SEAT, so the hook went
# inert - which meant the one agent with the widest reach and no supervisor was
# the only one nothing checked. Two such sessions in one working tree edited the
# same file on the same afternoon and neither was told, because leases are taken
# per EXECUTION and a seatless session had no execution identity to take one for.
#
# It is not seatless, though. It holds the DIRECTOR seat - the seat qa_gate
# escalates to and routes/orchestrator.py is built around. So it gets that seat's
# identity here rather than a new concept.
#
# MODES, because the strict answer is not the safe default. The director's lane
# is design/**, so full enforcement refuses every game/** write a top-level
# session makes - occasionally right, frequently a dammed session, and a gate
# people turn off is worth less than a quieter one they leave on.
#
#   off      exactly the old behaviour: no identity, no lease, no checks.
#   collide  DEFAULT. Adds only what was missing: the session takes path leases
#            like any other execution, and a write into a file another live run
#            is holding is BLOCKED and names the holder. Lane violations pass - #            the director writing game/** is normal and this mode says nothing
#            about it. Nothing that was legal yesterday becomes illegal today
#            unless somebody else is genuinely in the file.
#   warn     as collide, plus lane violations reported to the human on exit 1.
#            The write still lands.
#   block    the director is a seat like any other: out of lane is refused.
#            Choose this when everything should go through the board.
DIRECTOR_SEAT = "director"
DIRECTOR_MODES = ("off", "collide", "warn", "block")
DEFAULT_DIRECTOR_MODE = "collide"


def director_mode() -> str:
    """How hard to check a session that adopted no seat. Never raises."""
    mode = os.environ.get("BGATE_DIRECTOR_MODE", "").strip().lower()
    return mode if mode in DIRECTOR_MODES else DEFAULT_DIRECTOR_MODE


# ---------------------------------------------------------------------------
# THE SEATED WORKER'S LANE - advisory by default since 2026-08-19.
#
# A seat is a TOOLSET plus a PROJECT BOUNDARY (aegis, which now defaults to
# block). The lane table inside that boundary turned out to do more harm than
# good as a hard gate: the default lanes assume the <root>/game + <root>/design
# scaffold, so on an adopted repo every seat was refused on contact with the
# real source tree, and the observed agent response to a refusal was not
# routing but dying politely - "failed" with nothing done, cleared and
# redispatched by a human. The refusal MESSAGE (which seat owns the path, the
# queue_add call that hands work over) earned its keep; the exit code 2 did
# not. So the message survives as a warning to the human and the write lands.
#
# Same ladder shape as DIRECTOR_MODES, different population. THE LADDER
# ITSELF LIVES IN bgate_core.board.seats (LANE_MODES / lane_mode), single-sourced
# for the same reason aegis's ladder is: two processes must answer alike.
#
#   collide  lanes waived silently; a collision with another live run still
#            blocks and the lease is still taken.
#   warn     DEFAULT. As collide, plus out-of-lane writes reported to the
#            human on exit 1. The write lands; the agent is not interrupted.
#   block    the old behaviour - out of lane is refused. For projects whose
#            lane table is curated and trusted.
def worker_lane_mode() -> str:
    """How hard to enforce a SEATED worker's lane. Never raises.

    Imported lazily like every bgate_core import here - the hook is a fresh
    process on every tool call.
    """
    from bgate_core.board import seats

    return seats.lane_mode()


# ---------------------------------------------------------------------------
# CONTAINMENT - the where-question, and its own ladder.
#
# Deliberately NOT folded into DIRECTOR_MODES even though it reads the same
# shape, because the two dials govern different populations and would fight if
# they were one. The director ladder softens the LANE for a seatless session;
# this one governs the PROJECT BOUNDARY for a seated one, and a seatless
# director is exempt from it entirely (see `_contain`).
#
# THE LADDER ITSELF NOW LIVES IN bgate_core.board.aegis and these three names are
# aliases onto it. It moved when the MCP server became a second enforcer: that
# process asks the same question about the same agent, and if each side kept its
# own copy of the dial then BGATE_AEGIS=block could mean "refused" at the hook
# and "warned" at the tools, which is not a policy anyone could reason about.
# The names stay because callers and tests here use them.
#
#   off    the old behaviour: the boundary is not checked at all.
#   warn   the call lands and the human is told on exit 1 - the
#          evidence-gathering mode this gate shipped at.
#   block  DEFAULT since 2026-08-19. A seated agent touching another tree is
#          refused. The boundary hardened the same day the lane gate went
#          advisory: a seat is a toolset plus THIS line.
def aegis_mode() -> str:
    """How hard to enforce the project boundary. Never raises.

    Imported lazily, like every other bgate_core import in this module: the
    hook is a fresh process on every tool call, so nothing it might not need
    belongs at the top of the file.
    """
    from bgate_core.board import aegis

    return aegis.mode()


def __getattr__(name: str):
    # AEGIS_MODES and DEFAULT_AEGIS_MODE resolved on first touch rather than at
    # import, so the alias costs nothing in the runs that never look at it.
    from bgate_core.board import aegis

    if name == "AEGIS_MODES":
        return aegis.MODES
    if name == "DEFAULT_AEGIS_MODE":
        return aegis.DEFAULT_MODE
    raise AttributeError(name)


def session_owner(payload: dict) -> str:
    """An execution identity for a session nobody dispatched.

    Dispatched agents get BGATE_LOCK_OWNER=item-<id>. A hand-started session has
    none, which is why it could never hold or collide with a lease. Claude Code
    puts a stable session_id in every hook payload, so there IS an identity to
    use - it was simply never read. Truncated because it is a label in a message,
    not a key.
    """
    sid = str(payload.get("session_id") or "").strip()
    return f"session:{sid[:12]}" if sid else ""

# ---------------------------------------------------------------------------
# Bash parsing. Deliberately small: every rule below exists because it is a way
# an agent has actually written a file, not because it completes a grammar.
# ---------------------------------------------------------------------------
_REDIRECT_WRITE = {">", ">>", ">|", "&>", ">&", "&>>"}
_REDIRECT_READ = {"<", "<<", "<<<", "<&"}
# NO "\n" HERE, deliberately: shlex with whitespace_split consumes a newline
# as ordinary whitespace, so a newline token never reaches this set — which is
# exactly how `ls\nrm -rf game/` was once judged as the program `ls` with
# three extra arguments. Lines are split BEFORE lexing (see _logical_lines).
_SEPARATORS = {";", "|", "||", "&&", "&", "(", ")", "{", "}"}
_FD_PREFIX = {"1", "2", "&"}

# Wrappers that carry the real command as their tail.
_PASSTHROUGH = {"sudo", "env", "command", "nohup", "time", "xargs", "nice",
                "stdbuf", "exec", "builtin"}

# program -> how to read its write targets.
_ALL_ARGS = {"rm", "rmdir", "unlink", "shred", "tee", "truncate", "touch",
             "mkdir", "chmod", "chown"}
_DEST_LAST = {"cp", "mv", "install", "ln", "rsync"}
_INPLACE = {"sed", "perl", "gawk", "awk"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_INTERPRETERS = {"python", "python3", "py", "node", "nodejs", "ruby", "perl",
                 "php", "pwsh", "powershell"} | _SHELLS
_EVAL_FLAGS = {"-c", "-e", "--eval", "-command", "--command", "-E"}

# Flags whose VALUE is not a path we should mistake for a target.
_VALUE_FLAGS = {"-s", "--size", "-m", "--mode", "-S", "--suffix", "--reference"}
# Flags whose value IS the write target.
_OUTPUT_FLAGS = {"-o", "--output", "-O", "--output-document",
                 "-t", "--target-directory"}

# git subcommands that rewrite the working tree - i.e. every other seat's files.
_GIT_WRITES = {"checkout", "restore", "apply", "clean", "reset", "rm", "mv",
               "stash", "revert", "merge", "rebase", "cherry-pick", "pull",
               "am", "switch"}

# A snippet passed to an interpreter that is clearly writing. Read-only snippets
# must NOT match - blocking `python -c "print(1)"` would be its own outage.
_SNIPPET_WRITES = re.compile(
    r"""open\s*\([^)]*['"][rbt+]*[wax]|write_text|write_bytes|writelines|"""
    r"""\.write\s*\(|shutil\.(?:copy|move|rmtree)|"""
    r"""os\.(?:remove|unlink|rename|replace|mkdir|makedirs|rmdir|truncate)|"""
    r"""writeFileSync|appendFileSync|fs\.(?:write|unlink|rename|mkdir)|"""
    # perl spells a write as a mode SIGIL in the filename argument -
    # open(F, ">x") / open(F, ">>x") - and python's [rbt+]*[wax] shape
    # cannot see it; `unlink` is perl's os.remove.
    r"""open\s*\([^)]*['"]\s*>{1,2}|\bunlink\b|"""
    r"""subprocess\.(?:run|call|check_call|check_output|Popen)|os\.system|"""
    r"""File\.write|IO\.write|Remove-Item|Set-Content|Out-File""",
    re.I | re.X)

# Last-ditch: the command could not be tokenised at all. If it carries any of
# these it clearly writes, so it fails closed instead of sliding through.
_RAW_WRITES = re.compile(
    r">|\btee\b|\bcp\b|\bmv\b|\brm\b|\bsed\s+-i|\bdd\b|\btruncate\b|\bmkdir\b",
    re.I)

# Sinks that are not files anyone owns.
_NOT_A_FILE = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "nul"}


def _collapse_fd_dups(tokens: list[str]) -> list[str]:
    """Drop `2>&1`-style descriptor duplication before anything else looks at it.

    It writes no file - but read literally, `2>&1` says "redirect to the file
    named 1", so every `cmd > out 2>&1` an agent runs would be judged as a write
    to ./1 and blocked. `&> file` and `>& file` DO write a file and stay.
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        dup = (token in {">&", "<&", "&"}
               and i + 1 < len(tokens)
               and (tokens[i + 1].isdigit() or tokens[i + 1] == "-"))
        if dup:
            i += 2
            if out and out[-1] in _FD_PREFIX:
                out.pop()  # the source fd, e.g. the 2 in `2>&1`
            continue
        out.append(token)
        i += 1
    return out


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """One token stream -> the simple commands inside it."""
    return [segment for segment, _open, _close in _scoped_segments(tokens)]


def _scoped_segments(tokens: list[str]) -> list[tuple[list[str], int, int]]:
    """The simple commands, each with the subshell brackets around it.

    ``(segment, opened_before, closed_after)``. The brackets are separators, so
    the plain splitter above threw them away - which made a ``cd`` inside
    ``( ... )`` look like it changed the shell's directory for everything after
    the closing paren. It does not, and modelling it that way judged later
    relative writes against a directory the shell had already left. That is not
    a cosmetic misread: it decides which seat's lane a path falls in, and it
    produced write-log entries like
    ``assets/audio/assets/audio/attack.synth.json`` in the benchmark projects -
    a path that exists nowhere.
    """
    out: list[tuple[list[str], int, int]] = []
    current: list[str] = []
    opened = 0
    for token in tokens:
        if token in _SEPARATORS:
            if current:
                out.append((current, opened, 0))
                current, opened = [], 0
            if token in ("(", "{"):
                opened += 1
            elif token in (")", "}") and out:
                segment, before, after = out[-1]
                out[-1] = (segment, before, after + 1)
        else:
            current.append(token)
    if current:
        out.append((current, opened, 0))
    return out


def _program(args: list[str]) -> tuple[str, list[str]]:
    """Strip env assignments and wrappers; return (program, its args)."""
    while args:
        head = args[0]
        if "=" in head and not head.startswith("-") and "/" not in head.split("=")[0]:
            args = args[1:]  # VAR=value prefix
            continue
        name = os.path.basename(head).lower()
        if name.endswith(".exe"):
            name = name[:-4]
        if name in _PASSTHROUGH and len(args) > 1:
            args = args[1:]
            continue
        return name, args[1:]
    return "", []


def _positional(args: list[str]) -> list[str]:
    """Non-flag arguments, skipping the values of flags that take one."""
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg.startswith("-"):
            if arg in _VALUE_FLAGS:
                skip = True
            continue
        out.append(arg)
    return out


def _flag_value(args: list[str], flags: set[str]) -> list[str]:
    """Values of the given flags, in either ``-o x`` or ``-o=x`` form."""
    out, want = [], False
    for arg in args:
        if want:
            out.append(arg)
            want = False
            continue
        if arg in flags:
            want = True
        elif "=" in arg and arg.split("=", 1)[0] in flags:
            out.append(arg.split("=", 1)[1])
    return out


def _snippets(args: list[str]) -> list[str]:
    """Code passed inline to an interpreter (``-c``, ``-e``, ``-Command``).

    Case-insensitive because PowerShell writes ``-Command`` - but only here:
    for curl/wget, ``-o`` and ``-O`` are different flags and folding them
    together would make the hook read a URL as a file path.
    """
    out, want = [], False
    for arg in args:
        if want:
            out.append(arg)
            want = False
            continue
        low = arg.lower()
        if low in _EVAL_FLAGS:
            want = True
        elif "=" in low and low.split("=", 1)[0] in _EVAL_FLAGS:
            out.append(arg.split("=", 1)[1])
    return out


def _scan_segment(tokens: list[str],
                  embedded: bool = False) -> tuple[list[str], list[str], str]:
    """One simple command -> (write targets, unanalysable reasons, program).

    ``embedded`` means this command arrived INSIDE an ``eval`` or a shell's
    ``-c`` string. There the program position must be readable, because the
    whole payload is one opaque token to the outer command line: ``eval
    "$CMD"`` used to come back clean - the inner pass saw a program named
    ``$CMD``, matched it against no table, and reported nothing, which made a
    two-line variable assignment a bypass of the entire gate. A top-level
    ``$CMD`` is left alone (it is not a detected write, and the fail-open rule
    holds); an embedded one fails closed because eval exists to run text as
    commands and text we cannot read is exactly the unclear channel's job.
    """
    writes: list[str] = []
    unclear: list[str] = []
    args: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _REDIRECT_WRITE:
            if args and args[-1] in _FD_PREFIX:
                args.pop()  # the fd number in `2> log`
            if i + 1 < len(tokens):
                target = tokens[i + 1]
                # `2>&1` duplicates a descriptor; it writes no file. The '&' and
                # the fd that follows it are not a path.
                if target.startswith("&"):
                    i += 2
                    if i < len(tokens) and tokens[i].isdigit():
                        i += 1
                    continue
                writes.append(target)
                i += 2
                continue
            unclear.append("a redirection with no visible target")
            i += 1
            continue
        if token in _REDIRECT_READ:
            i += 2  # the source (or heredoc delimiter) is read, not written
            continue
        args.append(token)
        i += 1

    program, rest = _program(args)
    if not program:
        return writes, unclear, program
    if embedded and (program.startswith("$") or "`" in program):
        unclear.append(
            f"an eval/-c payload whose command is an unexpanded expansion "
            f"({program}) - what it runs cannot be read here")
        return writes, unclear, program

    positional = _positional(rest)
    if program in _ALL_ARGS:
        writes.extend(positional)
        writes.extend(_flag_value(rest, _OUTPUT_FLAGS))
    elif program in _DEST_LAST:
        targeted = _flag_value(rest, {"-t", "--target-directory"})
        if targeted:
            writes.extend(targeted)
        elif len(positional) >= 2:
            writes.append(positional[-1])
        elif positional:
            unclear.append(f"{program} with one operand - no visible destination")
    elif program == "dd":
        of = [a.split("=", 1)[1] for a in rest if a.startswith("of=")]
        if of:
            writes.extend(of)
        else:
            unclear.append("dd without a visible of= target")
    elif program in _INPLACE:
        if any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in rest):
            # The first positional is the script unless -e/-f supplied one.
            scripted = any(a in ("-e", "-f", "--expression", "--file") for a in rest)
            writes.extend(positional if scripted else positional[1:])
    elif program in ("curl", "wget"):
        writes.extend(_flag_value(rest, _OUTPUT_FLAGS))
    elif program == "git":
        sub = positional[0] if positional else ""
        if sub in _GIT_WRITES:
            unclear.append(
                f"`git {sub}` rewrites the working tree - it can overwrite any "
                "seat's files and the paths it touches are not knowable here")
    elif program == "eval":
        # eval's arguments ARE a shell command. shlex has already unquoted
        # them, so `eval 'echo x > game/foo.gd'` arrives as one clean payload
        # token — re-analysed with the same rules. The module used to claim
        # eval'd snippets fail closed while eval was in no table at all: a
        # one-token bypass of the whole gate.
        inner = analyse_bash(" ".join(rest), _embedded=True)
        writes.extend(inner["writes"])
        unclear.extend(inner["unclear"])
    elif program in _SHELLS:
        # `bash -c "echo x > game/foo.gd"` is just another shell command - read
        # it with the same rules rather than guessing at it with a regex.
        for snippet in _snippets(rest):
            inner = analyse_bash(snippet, _embedded=True)
            writes.extend(inner["writes"])
            unclear.extend(inner["unclear"])
    # NOT an elif: perl sits in _INPLACE **and** _INTERPRETERS, and an elif
    # chain let the in-place branch shadow the snippet check — so
    # `perl -e '<writing code>'` without -i never reached the fail-closed
    # path the docstring promised. Shells are excluded because their branch
    # above already re-analysed the snippet properly.
    if program in _INTERPRETERS and program not in _SHELLS:
        for snippet in _snippets(rest):
            if _SNIPPET_WRITES.search(snippet):
                unclear.append(
                    f"a {program} -c snippet that writes files; this hook cannot "
                    "tell which ones")
    return writes, unclear, program


_HEREDOC_RE = re.compile(r"<<-?\s*(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z_][\w-]*))")


def _logical_lines(command: str) -> list[tuple[str, str]]:
    """Physical lines of a command -> (command line, its heredoc body).

    Lines are the shell's own statement separator and have to be split BEFORE
    lexing: shlex with whitespace_split eats a newline as ordinary whitespace,
    so a multi-line command collapsed into one token stream and every write on
    line two or later passed every gate.

    A heredoc body is DATA belonging to the line that opened it, not commands
    — it is peeled off and returned beside its line so the caller can grade it
    as a snippet instead of parsing prose as shell. ``<<<`` is a word, never a
    heredoc, and is masked before the delimiter scan.
    """
    out: list[tuple[str, list[str]]] = []
    pending: list[str] = []          # open heredoc delimiters, in order
    for raw in str(command or "").split("\n"):
        if pending:
            if raw.strip() == pending[0]:
                pending.pop(0)
            elif out:
                out[-1][1].append(raw)
            continue
        found = [a or b or c for a, b, c in
                 _HEREDOC_RE.findall(raw.replace("<<<", " "))]
        out.append((raw, []))
        pending.extend(found)
    return [(line, "\n".join(body)) for line, body in out]


def analyse_bash(command: str, *, _embedded: bool = False) -> dict:
    """What a Bash command would write, as far as static reading can tell.

    Returns ``{"writes": [raw path strings], "unclear": [reasons]}``. ``unclear``
    is the fail-closed channel: something writes and we cannot name the target.

    ``cd`` IS MODELLED, because it was the containment bypass: relative write
    targets used to resolve against the session's cwd unconditionally, so
    ``cd ../other-game && echo x > game/foo.gd`` was judged as a write into
    THIS project's game/ — in-lane, in-project, allowed — while the shell wrote
    into the other one. A resolvable cd shifts every later relative target; an
    unresolvable one (``cd $DIR``, ``cd -``, bare ``cd``) makes later relative
    writes unclear rather than guessed.
    """
    writes: list[str] = []
    unclear: list[str] = []
    cd_to = ""            # where the command has cd'd so far ("" = nowhere)
    cd_unknown = False
    subshells: list[tuple[str, bool]] = []   # directory state saved at each `(`
    for line, heredoc in _logical_lines(command):
        if not line.strip():
            continue
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            tokens = list(lex)
        except ValueError:
            # Unbalanced quotes. If it clearly writes, refuse; else stay out.
            if _RAW_WRITES.search(line):
                unclear.append("the command could not be parsed (unbalanced "
                               "quotes?) and it writes")
            continue
        for segment, opened, closed in _scoped_segments(_collapse_fd_dups(tokens)):
            # A SUBSHELL'S `cd` DOES NOT ESCAPE IT. Save the shell's directory
            # state on `(` and put it back on `)` - which is what the shell
            # itself does, and what this did not.
            for _ in range(opened):
                subshells.append((cd_to, cd_unknown))
            seg_writes, seg_unclear, program = _scan_segment(segment, _embedded)
            if program == "cd":
                target = next((t for t in segment[1:]
                               if not t.startswith("-")), "")
                if not target or any(ch in target for ch in "$`~"):
                    cd_unknown = True
                elif os.path.isabs(target) or target[1:2] == ":":
                    cd_to, cd_unknown = target, False
                else:
                    cd_to = _join(cd_to, target)
                for _ in range(closed):
                    if subshells:
                        cd_to, cd_unknown = subshells.pop()
                continue
            if heredoc and program in _INTERPRETERS \
                    and program not in _SHELLS \
                    and _SNIPPET_WRITES.search(heredoc):
                # `python <<EOF` is `-c` with extra steps: the body is the
                # script, and one that writes fails closed like a -c snippet.
                seg_unclear.append(
                    f"a {program} heredoc script that writes files; this hook "
                    "cannot tell which ones")
            for w in seg_writes:
                if not w.strip() or w.strip().lower() in _NOT_A_FILE:
                    continue
                if "$" in w or "`" in w or w.startswith("~"):
                    # An unexpanded target is not a path, it is a placeholder
                    # for one. `sh -c 'echo x > $F'` used to record a write to
                    # a literal in-project file named $F and pass containment
                    # while the real target could be any tree at all - same
                    # rule as an unresolvable cd: unclear, never guessed.
                    seg_unclear.append(
                        f"a write target ({w}) containing an expansion this "
                        "hook cannot resolve")
                    continue
                if os.path.isabs(w) or w[1:2] == ":":
                    writes.append(w)
                elif cd_unknown:
                    seg_unclear.append(
                        f"a relative write ({w}) after a cd whose target this "
                        "hook cannot resolve")
                elif cd_to:
                    writes.append(_join(cd_to, w))
                else:
                    writes.append(w)
            unclear.extend(seg_unclear)
            for _ in range(closed):
                if subshells:
                    cd_to, cd_unknown = subshells.pop()
    return {"writes": writes, "unclear": unclear}


def _join(base: str, rel: str) -> str:
    """`base`/`rel`, COLLAPSED. `a/b/../c` and `a/c` are the same file and only
    one of them matches a lane glob, so leaving the `..` in produced a verdict
    about a path the shell will never write to."""
    joined = os.path.join(base, rel) if base else rel
    return os.path.normpath(joined).replace("\\", "/")


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
def decide(payload: dict, seat: str, owner: str = "",
           mode: str = "block") -> tuple[int, str]:
    """Pure decision, separated from stdio so tests can hit it directly.

    `mode` is one of WORKER_LANE_MODES for a dispatched seat worker (default
    "warn" - the lane is advisory, the project boundary is what a seat
    enforces) and one of DIRECTOR_MODES for a session that adopted no seat.
    It only ever softens the LANE gate; a lock or lease collision is a second
    live writer in the same file and is refused in every mode but "off".
    """
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool == "Bash":
        return _decide_bash(str(tool_input.get("command") or ""), payload,
                            seat, owner, mode)
    if tool == "PowerShell":
        return _decide_powershell(str(tool_input.get("command") or ""),
                                  payload, seat, owner, mode)

    key = _PATH_KEYS.get(tool)
    if key is not None:
        target = tool_input.get(key)
        if not target:
            return ALLOW, ""
        return _judge_path(str(target), payload, seat, owner, mode)

    read_key = _READ_PATH_KEYS.get(tool)
    if read_key is not None:
        return _judge_read(tool_input.get(read_key), payload, tool)

    return ALLOW, ""  # neither a read nor a write - not this hook's business


def _session_cwd(payload: dict):
    """A relative path is relative to the SESSION's cwd (in the payload), never
    to this hook process's - resolving against the wrong one lets relative
    writes silently bypass enforcement."""
    from pathlib import Path
    return Path(payload.get("cwd") or os.getcwd())


def _contain(target: str, payload: dict, tool: str = "",
             verb: str = "write") -> tuple[int, str]:
    """Is this path inside the project this agent was dispatched for?

    ``(ALLOW, "")`` means carry on to whatever gate comes next; it does NOT mean
    the write is permitted, only that it is in the right tree.

    THE ROOT IS THE PINNED ONE, from BGATE_ROOT, and that is the entire point.
    ``db.resolve_root(target)`` reads the project off the file being written,
    which is how an agent writing into another game got judged by that game's
    rules. ``project.active_root()`` is no better: the human can switch the
    active project from the dashboard while an agent runs, and a running agent's
    containment must not move under it halfway through. Dispatch already pins
    the root at spawn and nothing consumed it. This consumes it.

    SEATED SESSIONS ONLY. A hand-started director legitimately works across
    projects - reading one game's design while planning another is ordinary
    top-level work - so a session with no BGATE_SEAT is exempt. The agents this
    contains are the ones nobody is watching.
    """
    mode = aegis_mode()
    if mode == "off":
        return ALLOW, ""
    seat = os.environ.get("BGATE_SEAT", "").strip()
    if not seat:
        return ALLOW, ""
    pinned = os.environ.get("BGATE_ROOT", "").strip()
    if not pinned:
        # Nothing claimed a scope for this session, so there is no claim to
        # enforce. aegis says the same thing ("unscoped"); short-circuiting here
        # keeps the common seatless-and-unpinned case off the import path.
        return ALLOW, ""

    try:
        from bgate_core.board import aegis
        cwd = _session_cwd(payload)
        result = aegis.decide(pinned, target, cwd=cwd, seat=seat)
        allowed = aegis.is_allowed(result)
        if allowed:
            # ORDINARY IN-PROJECT WORK IS NOT LOGGED - that is every file the
            # agent touches, and it would bury the handful of lines the audit is
            # for. An allow that only survived because of the TOOLCHAIN
            # ALLOWLIST is a different animal: it crossed the boundary and was
            # let through, which is exactly what has to be reviewable before the
            # default moves to block. Asking again with an empty allowlist is
            # how we tell the two apart without reading aegis's prose, and it is
            # cheap: an in-project target returns before aegis touches the disk.
            crossed = not aegis.is_allowed(
                aegis.decide(pinned, target, cwd=cwd, seat=seat, allowlist=[]))
            if crossed:
                _log_containment(result, target, tool, mode, payload)
            return ALLOW, ""
        _log_containment(result, target, tool, mode, payload)
    except Exception as exc:
        # The fail-safe rule applies here like everywhere else: a containment
        # check that cannot run must not become a session that cannot work.
        _log_fail_open(f"containment check failed: {type(exc).__name__}: {exc}",
                       payload)
        return ALLOW, ""

    if mode == "warn":
        # Exit 1 is non-blocking and its stderr goes to the HUMAN, not the
        # model, so this names the dial: the person reading it is the one who
        # decides whether the finding is a bug in the gate or in the agent.
        return WARN, (
            f"[builders-gate] CONTAINMENT WARNING - the {verb} was ALLOWED: "
            f"{result['reason']}. BGATE_AEGIS=block would have refused it; "
            "this line is the evidence that decides whether it should.")

    other_game = result["verdict"] == "deny"
    tail = (" Another game's files are on the other side of that path. Nothing "
            "makes that yours to touch, including being asked to."
            if other_game else
            " Your work belongs in the tree you were dispatched for and "
            "nowhere else.")
    return BLOCK, (
        f"[builders-gate] seat {seat!r} may not {verb} that path: "
        f"{result['reason']}.{tail} If your task genuinely needs something from "
        "outside, do not go and get it - say so in your result note and name "
        "the path, so a human decides.")


def _judge_read(target, payload: dict, tool: str) -> tuple[int, str]:
    """Containment, and nothing else, for the tools that only look.

    A missing path is not "nothing to judge": Glob and Grep default it to the
    session's own directory, so that is what gets judged. Treating absent as
    exempt would make `Grep(pattern)` with the cwd parked in another project the
    one read that walks straight past this gate.
    """
    return _contain(str(target or _session_cwd(payload)), payload, tool,
                    verb="read")


def _judge_path(target: str, payload: dict, seat: str,
                owner: str, mode: str = "block") -> tuple[int, str]:
    """Containment, then lane + lock + lease, for one concrete path.

    ORDER IS LOAD-BEARING. Containment runs first because the lane gate cannot
    answer this question: it resolves the project from the target, so a write
    into another game is graded against that game's lanes and a write outside
    every project is waved through. Asking "is this even your tree" first makes
    both of those a refusal instead of an accident.

    In `warn` the containment finding does NOT short-circuit the lane gate. A
    warning that skipped the remaining checks would be a loosening dressed as a
    softening - a cross-project write would stop being graded by anything at
    all - so the lane gate still runs and the warning is only what surfaces when
    it had nothing louder to say.
    """
    contained, note = _contain(target, payload,
                               str((payload or {}).get("tool_name", "")))
    if contained == BLOCK:
        return BLOCK, note
    code, message = _judge_lanes(target, payload, seat, owner, mode)
    if contained == WARN and code == ALLOW:
        return WARN, note
    return code, message


def _judge_lanes(target: str, payload: dict, seat: str,
                 owner: str, mode: str = "block") -> tuple[int, str]:
    """Lane + lock + lease for one concrete path. ALLOW takes the lease."""
    # Lazy imports keep the hook fast on the (common) inert path.
    from pathlib import Path

    from bgate_core.store import assets, db
    from bgate_core.board import seats

    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = _session_cwd(payload) / target_path
    try:
        target_path = target_path.resolve()
    except OSError:
        return ALLOW, ""

    root = db.resolve_root(target_path.parent)
    if root is None:
        return ALLOW, ""  # not a Builders Gate project - stay out of the way

    try:
        rel = target_path.relative_to(Path(root).resolve())
    except ValueError:
        return ALLOW, ""  # writing outside the project - not ours to police

    verdict = seats.can_write(root, seat, str(rel), owner=owner)
    if verdict["allowed"]:
        _hold(assets, root, str(rel), seat, owner)
        _note_write(root, str(rel), seat, owner, payload)
        return ALLOW, ""

    # WHICH GATE FAILED, read off the verdict rather than off its prose: only a
    # lock or a lease names an `owner`, because only those two have somebody on
    # the other side of them. A lane failure is a rule about one writer; a
    # collision is a fact about two.
    blocker = verdict.get("owner") or ""
    if not blocker and mode != "block":
        # THE LANE GATE SHORT-CIRCUITS, and waiving it must not waive the two
        # gates behind it. can_write runs lane -> lock -> lease and returns on
        # the FIRST failure, so a director write that is out of lane never
        # reached the lease check - which made "collide" mode allow precisely
        # the collision it exists to catch. Ask the collision gates directly.
        blocker, why = _collision(assets, root, str(rel), seat, owner)
        if blocker:
            verdict = {"path": str(rel).replace("\\", "/"), "reason": why}
        else:
            # Nobody else is in the file, so the write proceeds - the director
            # writing outside design/** is ordinary. TAKE THE LEASE FIRST, in
            # BOTH softened modes: "warn" is "collide plus a sentence", and an
            # earlier draft returned the warning without holding, which made the
            # warning the only thing warn mode did and left the next session to
            # collide with nothing.
            _hold(assets, root, str(rel), seat, owner)
            # Recorded in BOTH softened modes, because both let the write land:
            # warn returns exit 1, which is non-blocking, so the file changes
            # either way and an audit that skipped it would under-report exactly
            # the sessions running with the gate loosened.
            _note_write(root, str(rel), seat, owner, payload)
            if mode == "collide":
                return ALLOW, ""

    if blocker:
        # WAITING IS NOT A PLAN. Observed: one run polled the same leased path
        # 8 times over 24 minutes (each poll a full-context turn), another was
        # killed mid-wait. When the holder is a work item, the board can do the
        # waiting instead - that is exactly what depends_on is for.
        route = ""
        held_item = re.match(r"item-(\d+)$", str(blocker))
        if held_item:
            route = (" If your work cannot land without this file, do not poll "
                     "and do not wait: file the follow-up with queue_add(your "
                     f"seat, ..., depends_on={held_item.group(1)}) so it runs "
                     "after that item finishes, and continue what you CAN do.")
        return BLOCK, (
            f"[builders-gate] {verdict['path']}: {verdict['reason']}. "
            f"{blocker} is in that file right now - coordinate with it "
            "(seat_post_note) or work on something else; do not edit around it. "
            "If that run is dead, the claim expires on its own; asset_status "
            "shows what is held." + route
        )

    level = BLOCK if mode == "block" else WARN
    lead = ("[builders-gate] you hold the DIRECTOR seat (no BGATE_SEAT set)"
            if seat == DIRECTOR_SEAT and mode != "block"
            else f"[builders-gate] seat {seat!r}")
    tail = (" Put it on the board with queue_add(seat, ...) so a seat agent with "
            "that lane does it and the QA gate sees it - or set "
            "BGATE_DIRECTOR_MODE=collide if you mean to edit it here."
            if seat == DIRECTOR_SEAT else
            _worker_route(root, str(rel), seat))
    return level, (f"{lead} may not write {verdict['path']}: "
                   f"{verdict['reason']}.{tail}")


def _worker_route(root, rel: str, seat: str) -> str:
    """The sentence after a worker's lane refusal - a route, not a wall.

    This tail used to say "use seat_can_write to find your lanes", which
    answers a question nobody asked: the agent knows where its lanes are, it
    is standing at the edge of them holding work. The observed result was the
    dead-end pattern - the write refused, a LEFTOVERS block or a seat note
    filed, the item closed, and the work never queued for the seat that could
    do it. Fifteen files carried those blocks; the notes outlived the last
    board row by five hours. So the refusal now names the seat that owns the
    path and the exact call that hands the work over. Best-effort by the same
    rule as everything in this hook: an unreadable seat table degrades to the
    generic route, never to a crash.
    """
    try:
        from bgate_core.board import seats as _seats
        owners = [o for o in _seats.lane_owners(root, rel) if o != seat]
    except Exception:
        owners = []
    if owners:
        named = owners[0]
        also = f" (also in-lane: {', '.join(owners[1:])})" if len(owners) > 1 else ""
        return (f" That path is the {named!r} seat's lane{also}. Do NOT stop, "
                "and do not leave this in a note or a LEFTOVERS block - "
                f"queue_add({named!r}, title, brief) files it for an agent that "
                "CAN write it (pass depends_on=<your item id> if it needs your "
                "output), then continue your own work. Handing work on IS part "
                "of finishing yours.")
    # NO OWNER IS USUALLY A LAYOUT MISMATCH, NOT AN EXOTIC PATH. The default
    # lanes assume the scaffold layout (<root>/game, <root>/design); an ADOPTED
    # repo - or one made by `bgate init`, which scaffolds into <root> - has its
    # source somewhere no lane names, so EVERY seat is refused on contact with
    # the real tree. Routing that to the director as a ruling is a dead end:
    # the director's lane is design/** and it cannot write the path either.
    # Say what is actually wrong, and name the one call that fixes it for good.
    return (" NO SEAT'S LANE COVERS THAT PATH AT ALL, which usually means this "
            "project's layout does not match the default lanes (they assume "
            "<root>/game and <root>/design). This is a configuration problem, "
            "not a routing one - a queue item for another seat would be "
            "refused the same way. Report it in your result note, name the "
            "path and the directory it lives in, and say that a human should "
            "widen the owning seat with seat_configure(role, write_globs=[...]) "
            " - an agent may not widen its own lanes. Then continue with "
            "whatever part of your task IS inside your lanes.")


# Write-shaped PowerShell. Cmdlets, their default aliases, redirection, and
# the .NET escape hatch. Deliberately broad: this is a fence, not a parser.
_PS_WRITES = re.compile(
    r"set-content|add-content|out-file|new-item|remove-item|move-item|"
    r"copy-item|rename-item|clear-content|start-process|invoke-expression|"
    r"\biex\b|\bsc\b|\bni\b|\bri\b|\bmi\b|\bcpi\b|\brm\b|\bmv\b|\bcp\b|"
    r"\bdel\b|\bmkdir\b|\bmd\b|>|\[io\.file\]|::write",
    re.I)


def _decide_powershell(command: str, payload: dict, seat: str,
                       owner: str, mode: str = "block") -> tuple[int, str]:
    """PowerShell is FENCED, not parsed.

    The desktop harness offers a PowerShell tool that only ``Bash`` handling
    ever saw — ``Set-Content game/foo.gd`` walked past every lane, lock and
    lease. Building a second analyser for a second shell's grammar is how this
    hook stops being 'deliberately small', so instead: a command that LOOKS
    like it writes is refused for an enforced seat with directions to the
    checked tools, and everything read-shaped passes. Same fail-closed policy
    and the same softenings as an unparseable Bash command: outside a project
    nothing is protected, and a seatless session's collide/warn mode is
    advisory here exactly as it is there.
    """
    if not command.strip():
        return ALLOW, ""
    if not _PS_WRITES.search(command):
        return ALLOW, ""
    from bgate_core.store import db
    if db.resolve_root(_session_cwd(payload)) is None:
        return ALLOW, ""
    if mode in ("collide", "warn"):
        return ALLOW, ""
    return BLOCK, (
        "[builders-gate] refusing a PowerShell command that appears to write: "
        "seat lanes cannot be read off PowerShell syntax, so writes there are "
        "not checkable. Use Write/Edit on the specific file, or Bash with "
        "explicit paths - both are checked precisely.")


def _decide_bash(command: str, payload: dict, seat: str,
                 owner: str, mode: str = "block") -> tuple[int, str]:
    """Same lane/lock rules, read off a shell command line.

    Order matters: named targets are judged first so the agent gets the precise
    "you are out of lane" message rather than the generic refusal.
    """
    if not command.strip():
        return ALLOW, ""
    analysis = analyse_bash(command)
    if not analysis["writes"] and not analysis["unclear"]:
        return ALLOW, ""  # obviously read-only - never in the way

    warning = ""
    for target in analysis["writes"]:
        code, message = _judge_path(target, payload, seat, owner, mode)
        if code == BLOCK:
            return BLOCK, (message + "\n[builders-gate] that write was in a Bash "
                                     "command; the lane rules are the same there.")
        if code == WARN and not warning:
            warning = message

    if analysis["unclear"]:
        from bgate_core.store import db
        # Outside a project there is nothing to protect, so an unreadable
        # command is not our problem. Inside one, it fails closed.
        if db.resolve_root(_session_cwd(payload)) is None:
            return ALLOW, ""
        # ...but only for an identity whose lanes are being enforced. Refusing
        # every unparseable command a top-level session runs would dam the
        # session over a quoting style, which is the cure being worse.
        if mode in ("collide", "warn"):
            return ALLOW, ""
        return BLOCK, (
            "[builders-gate] refusing a Bash command this hook cannot verify: "
            + "; ".join(analysis["unclear"]) + ". "
            "Seat lanes are enforced on Bash too, and a write whose target "
            "cannot be read is not a write we can allow. Use Write/Edit on the "
            "specific file (they are checked precisely), or run the command "
            "with explicit paths."
        )
    return (WARN, warning) if warning else (ALLOW, "")


def _note_write(root, rel: str, seat: str, owner: str, payload: dict) -> None:
    """Record a PERMITTED write so the file list stops being self-reported.

    A QA agent closed a gate saying "no files were touched" while having written
    its own `.bgate/progress/item-<id>.jsonl`, which the WORK MANIFEST rule told
    it to write. Nothing could contradict that: this hook logged only failures,
    the activity ledger records no writes, and the path lease -- the one trace --
    is reaped on expiry. The harness was on the path for every write and threw
    the knowledge away.

    Recorded for METADATA PATHS TOO, and that is the point rather than an
    oversight: the file the false report missed was a harness file, and
    `_hold` deliberately does not lease those, so this is the only thing that
    sees them at all.

    Best effort, like every other side effect here. A write the oracle already
    allowed must never fail because its bookkeeping did.
    """
    if not owner:
        return
    try:
        from bgate_core.store import writelog
        # THE PRE-IMAGE, TAKEN HERE BECAUSE HERE IS THE ONLY MOMENT IT EXISTS.
        # This hook runs BEFORE the tool, so the file on disk is still the old
        # one. A ledger of paths told you a destructive edit had happened and
        # gave you no way to undo it; first-touch copies give a live run a
        # short-range undo. Before record(), so a write that is about to
        # clobber something is captured even if the ledger append then fails.
        writelog.preimage(root, rel, owner)
        writelog.record(root, rel, seat, owner,
                        tool=str((payload or {}).get("tool_name", "")))
    except Exception:
        pass


def _collision(assets, root, rel: str, seat: str, owner: str) -> tuple[str, str]:
    """Is another EXECUTION in this file right now? Returns (owner, why).

    The lock and lease gates of seats.can_write, asked on their own, because that
    function checks the lane first and returns on the first failure - so waiving
    the lane also skipped these, which is the opposite of what waiving the lane
    is supposed to mean. Same data, same rules, no ordering dependency.

    Best-effort like everything else here: an unreadable store answers "nobody",
    because a hook that blocks on its own inability to check is a hook that dams
    the session.
    """
    try:
        entry = assets.get(root, rel)
    except Exception:
        entry = None
    if entry and entry["lock_seat"]:
        held = (entry["lock_owner"] or "").strip()
        if entry["lock_seat"] != seat or (held and held != owner):
            return (held or f"seat {entry['lock_seat']}",
                    f"locked by {held or entry['lock_seat']} since "
                    f"{entry['lock_at']} - binary assets don't merge")
    try:
        lease = assets.path_lease_for(root, rel)
    except Exception:
        lease = None
    if lease and (lease["owner"] or "") != owner:
        return (lease["owner"],
                f"leased by {lease['owner']} (seat {lease['seat'] or '?'}) since "
                f"{lease['acquired_at']} until {lease['expires_at'] or 'forever'} "
                " - that run is editing this file right now")
    return "", ""


def _hold(assets, root, rel: str, seat: str, owner: str) -> None:
    """Claim the path for this run so the next agent's write is a block, not a
    silent overwrite. Best effort by design - a lease we could not take must
    never stop a write the oracle already allowed."""
    if not owner:
        return  # no execution identity to attribute the lease to
    # HARNESS TRAILS ARE APPEND-ONLY AND SHARED ON PURPOSE. handoff/thread.jsonl
    # is one file per project that concurrent agents are meant to write; leasing
    # it would make the second agent's note a blocked write, which is the exact
    # opposite of what an append-only log is for. A lease exists to stop a silent
    # overwrite, and appending a line overwrites nothing.
    from bgate_core.board import seats as _seats
    if _seats.is_metadata(rel):
        return
    try:
        lease_s = int(os.environ.get("BGATE_LEASE_S", "") or DEFAULT_LEASE_S)
    except ValueError:
        lease_s = DEFAULT_LEASE_S
    try:
        assets.acquire_path_lease(root, rel, seat, owner, lease_s=lease_s)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Proving it is alive
# ---------------------------------------------------------------------------
def log_path(start=None):
    """Where fail-open events are recorded. None when there is no project."""
    from pathlib import Path

    from bgate_core.store import db
    root = os.environ.get("BGATE_ROOT") or ""
    resolved = Path(root) if root else db.resolve_root(start or os.getcwd())
    return (Path(resolved) / ".bgate" / LOG_NAME) if resolved else None


def _log_fail_open(detail: str, payload: dict | None = None) -> None:
    """A hook that fails open in silence is indistinguishable from a hook that
    is not installed. Say so - on stderr, and durably in the project."""
    line = f"[builders-gate] hook FAILED OPEN (write allowed unchecked): {detail}"
    try:
        print(line, file=sys.stderr)
    except Exception:
        pass
    try:
        from datetime import datetime, timezone
        start = (payload or {}).get("cwd") or None
        path = log_path(start)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": stamp, "event": "fail_open", "detail": detail[:2000],
                "tool": (payload or {}).get("tool_name", ""),
                "seat": os.environ.get("BGATE_SEAT", ""),
            }) + "\n")
    except Exception:
        pass  # a logger that raises would defeat the fail-safe it documents


def _log_containment(result: dict, target, tool: str, mode: str,
                     payload: dict | None = None) -> None:
    """Put every boundary crossing on the record, allowed or not.

    THIS IS WHAT MAKES `warn` WORTH SHIPPING. The default is warn precisely
    because nobody yet knows whether a real board produces legitimate
    cross-project touches, and a warning that vanishes into a terminal answers
    that question for nobody. `enforced` records whether the mode of the day
    actually stopped anything, so a later reader can tell "block would have
    refused this" from "block did refuse this" without inferring it from dates.

    Same file as the fail-open trail, deliberately: one log to read when asking
    what the gate has been doing. Best effort, by the rule that governs every
    side effect in this module - a logger that raises would take the session
    down to save a line of audit.
    """
    try:
        from datetime import datetime, timezone
        path = log_path((payload or {}).get("cwd") or None)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": "containment",
                "verdict": result.get("verdict", ""),
                "mode": mode,
                "enforced": mode == "block" and result.get("verdict") != "allow",
                "tool": tool,
                "seat": os.environ.get("BGATE_SEAT", ""),
                "owner": os.environ.get("BGATE_LOCK_OWNER", ""),
                "scope": result.get("scope", ""),
                "target": str(target)[:1000],
                "reason": str(result.get("reason", ""))[:1000],
            }) + "\n")
    except Exception:
        pass


def _log_rows(start=None) -> list[dict]:
    path = log_path(start)
    if path is None or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def recent_failures(start=None, limit: int = 10) -> list[dict]:
    """Fail-open events only. FILTERED BY EVENT rather than just tailing the
    file, because containment now shares the log and a caller asking "has this
    hook been failing open" must not be handed boundary crossings instead - the
    two mean opposite things about whether enforcement is working."""
    return [row for row in _log_rows(start)
            if row.get("event", "fail_open") == "fail_open"][-limit:]


def recent_containment(start=None, limit: int = 20) -> list[dict]:
    """The containment lines out of the same log, for the human reviewing
    whether `block` is safe to make the default."""
    return [row for row in _log_rows(start)
            if row.get("event") == "containment"][-limit:]


# A path no seat owns, used to prove the gate bites without touching real work.
PROBE_PATH = ".bgate/hook-selftest.probe"


def selftest(start=None, seat: str = "") -> dict:
    """Is enforcement actually live RIGHT NOW? Run real decisions and say.

    The hook is deliberately silent and deliberately fails open, which means
    "installed and working" and "not installed at all" look identical from the
    outside. This is the difference: it runs the real ``decide`` against probes
    whose verdicts are known, so a green line here is evidence, not a claim.
    """
    from pathlib import Path

    from bgate_core.store import db

    seat = (seat or os.environ.get("BGATE_SEAT", "")).strip()
    owner = os.environ.get("BGATE_LOCK_OWNER", "").strip()
    # A seatless session is checked now, so reporting it as inert would be the
    # status command telling the same lie the hook used to.
    mode = "block" if seat else director_mode()
    seated = bool(seat)
    if not seated and mode != "off":
        seat = DIRECTOR_SEAT
        owner = owner or "session:selftest"
    root = db.resolve_root(start or os.getcwd())
    out: dict = {
        "seat": seat,
        "seated": seated,
        "mode": mode,
        "owner": owner,
        "project_root": str(root) if root else "",
        "installed": False,
        "enforcing": False,
        "probes": [],
        "recent_failures": recent_failures(start),
        # Reported whether or not it is biting, because "which project am I
        # pinned to" is the first thing to check when an agent is being refused
        # its own files - and an EMPTY pinned root is itself the finding.
        "aegis": aegis_mode(),
        "pinned_root": os.environ.get("BGATE_ROOT", ""),
        "containment": recent_containment(start),
    }
    if root is not None:
        settings = Path(root) / ".claude" / "settings.json"
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
            out["installed"] = any(
                "bgate_cli.hook" in h.get("command", "")
                for entry in data.get("hooks", {}).get("PreToolUse", [])
                for h in entry.get("hooks", []))
            out["matchers"] = [
                entry.get("matcher", "") for entry in
                data.get("hooks", {}).get("PreToolUse", [])
                if any("bgate_cli.hook" in h.get("command", "")
                       for h in entry.get("hooks", []))]
        except (OSError, ValueError):
            out["settings_error"] = f"cannot read {settings}"

    if not seat:
        out["reason"] = ("BGATE_SEAT is unset and BGATE_DIRECTOR_MODE=off - the "
                         "hook is fully inert; nothing is being enforced")
        return out
    if root is None:
        out["reason"] = "not inside a .bgate project - the hook stays out of the way"
        return out

    cwd = str(root)
    # The out-of-lane probes are BLOCK for a seated worker and for an explicitly
    # strict director. In "collide"/"warn" the lane is not the gate - the LEASE
    # is - so asserting BLOCK there would report a working hook as broken. The
    # expectation follows the configuration, which is the only way this stays
    # evidence rather than a slogan.
    lane_verdict = BLOCK if mode == "block" else (WARN if mode == "warn" else ALLOW)
    probes = [
        ("Write out of lane",
         {"tool_name": "Write", "cwd": cwd,
          "tool_input": {"file_path": PROBE_PATH}}, lane_verdict),
        ("Bash redirect out of lane",
         {"tool_name": "Bash", "cwd": cwd,
          "tool_input": {"command": f"echo probe > {PROBE_PATH}"}}, lane_verdict),
        ("Bash read-only",
         {"tool_name": "Bash", "cwd": cwd,
          "tool_input": {"command": "git status --porcelain"}}, ALLOW),
    ]
    passed = True
    for label, payload, expected in probes:
        try:
            code, message = decide(payload, seat, owner, mode)
        except Exception as exc:  # a raising oracle IS the finding
            out["probes"].append({"probe": label, "error": f"{type(exc).__name__}: {exc}"})
            passed = False
            continue
        good = code == expected
        passed = passed and good
        out["probes"].append({"probe": label, "expected": expected,
                              "got": code, "ok": good,
                              "message": message[:200]})
    out["enforcing"] = passed
    if not passed:
        out["reason"] = ("a probe did not return the expected verdict - "
                         "enforcement is NOT trustworthy in this session")
    elif seated:
        out["reason"] = "lane + lock enforcement is live"
    elif mode == "collide":
        out["reason"] = (
            "no seat adopted, so this session holds the DIRECTOR seat: path "
            "leases ARE taken and a file another live run is editing WILL be "
            "refused, but the director's lane is not enforced. "
            "BGATE_DIRECTOR_MODE=warn to hear about out-of-lane writes, =block "
            "to refuse them, =off for the old inert behaviour.")
    else:
        out["reason"] = (f"no seat adopted; director mode {mode!r} - lanes and "
                         "leases are both being checked on this session")
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv or "--status" in argv:
        print(json.dumps(selftest(), indent=2))
        return 0
    payload: dict = {}
    try:
        seat = os.environ.get("BGATE_SEAT", "").strip()
        owner = os.environ.get("BGATE_LOCK_OWNER", "").strip()
        # A dispatched worker's lane is advisory by default (BGATE_LANES) -
        # the project boundary (aegis) is what a seat enforces. See
        # WORKER_LANE_MODES.
        mode = worker_lane_mode()
        if not seat:
            # THIS LINE USED TO BE `return ALLOW`. It read as "no adopted
            # identity, nothing to enforce", and the first half was true - but a
            # seatless session is not identity-less, it is the DIRECTOR, and the
            # thing worth enforcing on it is not its lane but whether somebody
            # else is already in the file. See DIRECTOR_MODES.
            mode = director_mode()
            if mode == "off":
                return ALLOW
            seat = DIRECTOR_SEAT
        # stdin must be read before any decision now: the session identity for a
        # hand-started session lives in the payload, not the environment.
        payload = json.loads(sys.stdin.read() or "{}")
        if mode != "block":
            # A dispatched worker already has BGATE_LOCK_OWNER=item-<id>;
            # session_owner is the fallback for a hand-started session. A
            # seated worker somehow lacking both keeps the old strict
            # semantics via decide() - can_write treats an ownerless caller
            # as unable to write over an owned lock - so only the DIRECTOR
            # path may bail out on a missing identity.
            owner = owner or session_owner(payload)
            if not owner and seat == DIRECTOR_SEAT \
                    and not os.environ.get("BGATE_SEAT", "").strip():
                # Nothing distinguishes this session from any other, so a lease
                # would be meaningless and a collision unattributable. The
                # director's lane is advisory anyway; do nothing rather than
                # something wrong.
                return ALLOW
        code, message = decide(payload, seat, owner, mode)
        if message:
            print(message, file=sys.stderr)
        return code
    except Exception as exc:
        # fail-safe: a broken hook must never dam the session - but it must not
        # be able to masquerade as a working one either.
        _log_fail_open(f"{type(exc).__name__}: {exc}", payload)
        return ALLOW


if __name__ == "__main__":
    sys.exit(main())
