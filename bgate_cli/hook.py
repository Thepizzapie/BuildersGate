"""PreToolUse hook — the teeth on seat lanes and asset locks.

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

  CAUGHT — redirections (``>`` ``>>`` ``2>`` ``&>``), cp/mv/install/ln/rsync,
    rm/rmdir/unlink/shred, tee, truncate/touch/mkdir, ``dd of=``, sed/perl
    in-place, curl -o / wget -O, git subcommands that rewrite the working tree,
    and an interpreter ``-c`` snippet that clearly writes (open(...,'w'),
    write_text, shutil.move, os.remove, writeFileSync, ...).
  FAIL-CLOSED — a command that clearly writes but whose target cannot be
    determined (an unparseable quote soup, an eval'd snippet, ``git apply``)
    is BLOCKED while the session's cwd is inside a project.
  NOT CAUGHT — anything a program does that the command line does not say:
    ``python build.py``, ``npm run x``, ``make``, a shell script, an editor.
    A determined process can still write; this stops the casual bypass, which
    is what actually happened. Do not read it as a sandbox.

FAIL-SAFE RULE: this must NEVER raise or exit nonzero by accident — a crashing
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

# Claude Code's contract: 0 allows, 2 blocks and shows stderr to the MODEL, any
# other nonzero is a non-blocking error whose stderr is shown to the HUMAN. That
# third channel is what WARN uses — the write lands, the person sees why it was
# questionable, and nothing is dammed.
ALLOW, WARN, BLOCK = 0, 1, 2

# Long enough to cover a working stretch between writes, short enough that a
# killed agent's claim clears on its own. Refreshed on every write it makes.
DEFAULT_LEASE_S = 900

LOG_NAME = "hook.log"

# ---------------------------------------------------------------------------
# THE SEATLESS SESSION — the participant this hook used to ignore completely.
#
# `if not seat: return ALLOW` was correct about identity and wrong about what
# follows from it. A session a human started has no BGATE_SEAT, so the hook went
# inert — which meant the one agent with the widest reach and no supervisor was
# the only one nothing checked. Two such sessions in one working tree edited the
# same file on the same afternoon and neither was told, because leases are taken
# per EXECUTION and a seatless session had no execution identity to take one for.
#
# It is not seatless, though. It holds the DIRECTOR seat — the seat qa_gate
# escalates to and routes/orchestrator.py is built around. So it gets that seat's
# identity here rather than a new concept.
#
# MODES, because the strict answer is not the safe default. The director's lane
# is design/**, so full enforcement refuses every game/** write a top-level
# session makes — occasionally right, frequently a dammed session, and a gate
# people turn off is worth less than a quieter one they leave on.
#
#   off      exactly the old behaviour: no identity, no lease, no checks.
#   collide  DEFAULT. Adds only what was missing: the session takes path leases
#            like any other execution, and a write into a file another live run
#            is holding is BLOCKED and names the holder. Lane violations pass —
#            the director writing game/** is normal and this mode says nothing
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


def session_owner(payload: dict) -> str:
    """An execution identity for a session nobody dispatched.

    Dispatched agents get BGATE_LOCK_OWNER=item-<id>. A hand-started session has
    none, which is why it could never hold or collide with a lease. Claude Code
    puts a stable session_id in every hook payload, so there IS an identity to
    use — it was simply never read. Truncated because it is a label in a message,
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
_SEPARATORS = {";", "|", "||", "&&", "&", "(", ")", "{", "}", "\n"}
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

# git subcommands that rewrite the working tree — i.e. every other seat's files.
_GIT_WRITES = {"checkout", "restore", "apply", "clean", "reset", "rm", "mv",
               "stash", "revert", "merge", "rebase", "cherry-pick", "pull",
               "am", "switch"}

# A snippet passed to an interpreter that is clearly writing. Read-only snippets
# must NOT match — blocking `python -c "print(1)"` would be its own outage.
_SNIPPET_WRITES = re.compile(
    r"""open\s*\([^)]*['"][rbt+]*[wax]|write_text|write_bytes|writelines|"""
    r"""\.write\s*\(|shutil\.(?:copy|move|rmtree)|"""
    r"""os\.(?:remove|unlink|rename|replace|mkdir|makedirs|rmdir|truncate)|"""
    r"""writeFileSync|appendFileSync|fs\.(?:write|unlink|rename|mkdir)|"""
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

    It writes no file — but read literally, `2>&1` says "redirect to the file
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
    out: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SEPARATORS:
            if current:
                out.append(current)
                current = []
        else:
            current.append(token)
    if current:
        out.append(current)
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

    Case-insensitive because PowerShell writes ``-Command`` — but only here:
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


def _scan_segment(tokens: list[str]) -> tuple[list[str], list[str]]:
    """One simple command -> (write targets, reasons it is unanalysable)."""
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
        return writes, unclear

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
            unclear.append(f"{program} with one operand — no visible destination")
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
                f"`git {sub}` rewrites the working tree — it can overwrite any "
                "seat's files and the paths it touches are not knowable here")
    elif program in _SHELLS:
        # `bash -c "echo x > game/foo.gd"` is just another shell command — read
        # it with the same rules rather than guessing at it with a regex.
        for snippet in _snippets(rest):
            inner = analyse_bash(snippet)
            writes.extend(inner["writes"])
            unclear.extend(inner["unclear"])
    elif program in _INTERPRETERS:
        for snippet in _snippets(rest):
            if _SNIPPET_WRITES.search(snippet):
                unclear.append(
                    f"a {program} -c snippet that writes files; this hook cannot "
                    "tell which ones")
    return writes, unclear


def analyse_bash(command: str) -> dict:
    """What a Bash command would write, as far as static reading can tell.

    Returns ``{"writes": [raw path strings], "unclear": [reasons]}``. ``unclear``
    is the fail-closed channel: something writes and we cannot name the target.
    """
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        # Unbalanced quotes. If it clearly writes, refuse; otherwise stay out.
        if _RAW_WRITES.search(command):
            return {"writes": [],
                    "unclear": ["the command could not be parsed (unbalanced "
                                "quotes?) and it writes"]}
        return {"writes": [], "unclear": []}

    writes: list[str] = []
    unclear: list[str] = []
    for segment in _split_segments(_collapse_fd_dups(tokens)):
        seg_writes, seg_unclear = _scan_segment(segment)
        writes.extend(seg_writes)
        unclear.extend(seg_unclear)
    writes = [w for w in writes if w.strip()
              and w.strip().lower() not in _NOT_A_FILE]
    return {"writes": writes, "unclear": unclear}


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
def decide(payload: dict, seat: str, owner: str = "",
           mode: str = "block") -> tuple[int, str]:
    """Pure decision, separated from stdio so tests can hit it directly.

    `mode` is "block" for a dispatched seat worker — its lane is the whole point
    of dispatching it — and one of DIRECTOR_MODES for a session that adopted no
    seat. It only ever softens the LANE gate; a lock or lease collision is a
    second live writer in the same file and is refused in every mode but "off".
    """
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool == "Bash":
        return _decide_bash(str(tool_input.get("command") or ""), payload,
                            seat, owner, mode)

    key = _PATH_KEYS.get(tool)
    if key is None:
        return ALLOW, ""  # not a file write — not this hook's business

    target = tool_input.get(key)
    if not target:
        return ALLOW, ""
    return _judge_path(str(target), payload, seat, owner, mode)


def _session_cwd(payload: dict):
    """A relative path is relative to the SESSION's cwd (in the payload), never
    to this hook process's — resolving against the wrong one lets relative
    writes silently bypass enforcement."""
    from pathlib import Path
    return Path(payload.get("cwd") or os.getcwd())


def _judge_path(target: str, payload: dict, seat: str,
                owner: str, mode: str = "block") -> tuple[int, str]:
    """Lane + lock + lease for one concrete path. ALLOW takes the lease."""
    # Lazy imports keep the hook fast on the (common) inert path.
    from pathlib import Path

    from bgate_core import assets, db, seats

    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = _session_cwd(payload) / target_path
    try:
        target_path = target_path.resolve()
    except OSError:
        return ALLOW, ""

    root = db.resolve_root(target_path.parent)
    if root is None:
        return ALLOW, ""  # not a Builders Gate project — stay out of the way

    try:
        rel = target_path.relative_to(Path(root).resolve())
    except ValueError:
        return ALLOW, ""  # writing outside the project — not ours to police

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
        # reached the lease check — which made "collide" mode allow precisely
        # the collision it exists to catch. Ask the collision gates directly.
        blocker, why = _collision(assets, root, str(rel), seat, owner)
        if blocker:
            verdict = {"path": str(rel).replace("\\", "/"), "reason": why}
        else:
            # Nobody else is in the file, so the write proceeds — the director
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
        return BLOCK, (
            f"[builders-gate] {verdict['path']}: {verdict['reason']}. "
            f"{blocker} is in that file right now — coordinate with it "
            "(seat_post_note) or work on something else; do not edit around it. "
            "If that run is dead, the claim expires on its own; asset_status "
            "shows what is held."
        )

    level = BLOCK if mode == "block" else WARN
    lead = ("[builders-gate] you hold the DIRECTOR seat (no BGATE_SEAT set)"
            if seat == DIRECTOR_SEAT and mode != "block"
            else f"[builders-gate] seat {seat!r}")
    tail = (" Put it on the board with queue_add(seat, ...) so a seat agent with "
            "that lane does it and the QA gate sees it — or set "
            "BGATE_DIRECTOR_MODE=collide if you mean to edit it here."
            if seat == DIRECTOR_SEAT else
            " Use seat_can_write to find your lanes, or asset_lock if you need to "
            "claim a binary.")
    return level, (f"{lead} may not write {verdict['path']}: "
                   f"{verdict['reason']}.{tail}")


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
        return ALLOW, ""  # obviously read-only — never in the way

    warning = ""
    for target in analysis["writes"]:
        code, message = _judge_path(target, payload, seat, owner, mode)
        if code == BLOCK:
            return BLOCK, (message + "\n[builders-gate] that write was in a Bash "
                                     "command; the lane rules are the same there.")
        if code == WARN and not warning:
            warning = message

    if analysis["unclear"]:
        from bgate_core import db
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
        from bgate_core import writelog
        writelog.record(root, rel, seat, owner,
                        tool=str((payload or {}).get("tool_name", "")))
    except Exception:
        pass


def _collision(assets, root, rel: str, seat: str, owner: str) -> tuple[str, str]:
    """Is another EXECUTION in this file right now? Returns (owner, why).

    The lock and lease gates of seats.can_write, asked on their own, because that
    function checks the lane first and returns on the first failure — so waiving
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
                    f"{entry['lock_at']} — binary assets don't merge")
    try:
        lease = assets.path_lease_for(root, rel)
    except Exception:
        lease = None
    if lease and (lease["owner"] or "") != owner:
        return (lease["owner"],
                f"leased by {lease['owner']} (seat {lease['seat'] or '?'}) since "
                f"{lease['acquired_at']} until {lease['expires_at'] or 'forever'} "
                "— that run is editing this file right now")
    return "", ""


def _hold(assets, root, rel: str, seat: str, owner: str) -> None:
    """Claim the path for this run so the next agent's write is a block, not a
    silent overwrite. Best effort by design — a lease we could not take must
    never stop a write the oracle already allowed."""
    if not owner:
        return  # no execution identity to attribute the lease to
    # HARNESS TRAILS ARE APPEND-ONLY AND SHARED ON PURPOSE. handoff/thread.jsonl
    # is one file per project that concurrent agents are meant to write; leasing
    # it would make the second agent's note a blocked write, which is the exact
    # opposite of what an append-only log is for. A lease exists to stop a silent
    # overwrite, and appending a line overwrites nothing.
    from bgate_core import seats as _seats
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

    from bgate_core import db
    root = os.environ.get("BGATE_ROOT") or ""
    resolved = Path(root) if root else db.resolve_root(start or os.getcwd())
    return (Path(resolved) / ".bgate" / LOG_NAME) if resolved else None


def _log_fail_open(detail: str, payload: dict | None = None) -> None:
    """A hook that fails open in silence is indistinguishable from a hook that
    is not installed. Say so — on stderr, and durably in the project."""
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


def recent_failures(start=None, limit: int = 10) -> list[dict]:
    path = log_path(start)
    if path is None or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


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

    from bgate_core import db

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
        out["reason"] = ("BGATE_SEAT is unset and BGATE_DIRECTOR_MODE=off — the "
                         "hook is fully inert; nothing is being enforced")
        return out
    if root is None:
        out["reason"] = "not inside a .bgate project — the hook stays out of the way"
        return out

    cwd = str(root)
    # The out-of-lane probes are BLOCK for a seated worker and for an explicitly
    # strict director. In "collide"/"warn" the lane is not the gate — the LEASE
    # is — so asserting BLOCK there would report a working hook as broken. The
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
        out["reason"] = ("a probe did not return the expected verdict — "
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
        out["reason"] = (f"no seat adopted; director mode {mode!r} — lanes and "
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
        mode = "block"
        if not seat:
            # THIS LINE USED TO BE `return ALLOW`. It read as "no adopted
            # identity, nothing to enforce", and the first half was true — but a
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
            # Only the director path invents an owner, and only when it has to.
            # A SEATED worker with no BGATE_LOCK_OWNER keeps the old semantics —
            # can_write treats an ownerless caller as unable to write over an
            # owned lock, which is stricter than anything derived here, and
            # loosening that to "no owner, no checks" would have quietly turned
            # the gate off for exactly the agents it was written for.
            owner = owner or session_owner(payload)
            if not owner:
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
        # fail-safe: a broken hook must never dam the session — but it must not
        # be able to masquerade as a working one either.
        _log_fail_open(f"{type(exc).__name__}: {exc}", payload)
        return ALLOW


if __name__ == "__main__":
    sys.exit(main())
