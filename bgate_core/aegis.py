"""Aegis — may this seated agent touch this path, yes or no.

NAMED AFTER THE SEPARATE AEGIS PROJECT ON PURPOSE (github.com/Thepizzapie/AEGIS),
which is a general policy layer for AI coding agents. This module is that same
idea narrowed to exactly one property, the only one Builders Gate needs from it:
a seated agent touches its own project and nothing else. If you came here from
bgate_core wondering what "aegis" means, that is all it means. It is not a
sandbox, it does not know about lanes or locks, and it decides nothing about
WHAT may be written - only WHERE.

WHY IT IS PURE. Two separate processes ask this question about the same write:
the PreToolUse hook (bgate_cli/hook.py) and the MCP server. They share no
memory, so the only way both can answer identically is for the answer to be a
function of its arguments. Hence: no database, no logging, no environment reads,
no writes of any kind. It reads the filesystem to ask "is there a project here",
and that is the whole of its contact with the outside world.

THE BUG IT EXISTS TO CLOSE. hook._judge_path derives the project FROM THE WRITE
TARGET - db.resolve_root(target.parent) - so an agent dispatched for project A
that writes into project B is judged against B's seats and lanes, and one that
writes outside every project is waved through entirely. dispatch.py has always
known the right answer (it pins BGATE_ROOT at spawn) and nothing consumed it.
This is the consumer.

THE THREE VERDICTS, because "not allowed" comes in two flavours that deserve
different sentences and possibly different handling:

  allow    the caller proceeds to its own gates (lanes, locks, leases).
  deny     the target is inside a DIFFERENT Builders Gate project. Another
           game's files are on the other side of this call. Always refuse.
  outside  the target is outside the pinned project and outside every project,
           and not on the allowlist. Nobody else's work is at risk, but it is
           still not this agent's to write. Refuse by default - `is_allowed`
           treats it as a refusal - and keep it distinct so a caller can say
           "that is outside your project" rather than accusing the agent of
           reaching into somebody else's game.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

ALLOW, DENY, OUTSIDE = "allow", "deny", "outside"

# HOW HARD TO ENFORCE, and it lives here rather than in either caller because
# there are now two of them. The PreToolUse hook had this ladder to itself while
# it was the only gate; the MCP server is a second process asking the same
# question about the same agent, and a session where one of them blocks and the
# other warns is not a policy, it is a coin flip. One name, one default, both
# callers.
#
#   off    the boundary is not checked at all.
#   warn   DEFAULT FOR THIS RELEASE. The call lands and the crossing is logged.
#          The gate's job right now is to produce the evidence that proves
#          `block` would deny nothing legitimate; flipping straight to block
#          would have every false positive land as a dead agent on somebody's
#          board, discovered hours later.
#   block  a seated agent touching another tree is refused.
MODES = ("off", "warn", "block")
DEFAULT_MODE = "warn"

# What marks a directory as a Builders Gate project. Deliberately NOT imported
# from bgate_core.db: this module is loaded on the hook's hot path and by a
# second process that has no reason to drag sqlite and the whole migration
# engine in behind a two-string constant. If db.DB_DIRNAME/DB_FILENAME ever
# change, this changes with them - test_aegis asserts they still agree.
PROJECT_DIRNAME = ".bgate"
PROJECT_DBNAME = "game.db"

# The \\?\ extended-length prefix. Windows APIs hand these back for long paths,
# and \\?\C:\Games\X and C:\Games\X are the same directory - compared raw they
# are not, so a pinned root in one form and a target in the other would read as
# two different projects and deny every write the agent makes.
_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


def _strip_extended(text: str) -> str:
    if text.startswith(_EXTENDED_UNC):
        return "\\\\" + text[len(_EXTENDED_UNC):]
    if text.startswith(_EXTENDED):
        return text[len(_EXTENDED):]
    return text


def _key(path: Path) -> str:
    """A path reduced to the form two spellings of one location share.

    normcase because Windows is the supported platform and C:\\Games\\X,
    c:/games/x and C:/GAMES/x are one directory there - including the DRIVE
    LETTER, which people type both ways and which os.path.normcase lowercases
    along with everything else. normpath collapses the separators and the
    ``..``/``.`` that a resolve() on a nonexistent path leaves behind.
    """
    return os.path.normcase(os.path.normpath(_strip_extended(str(path))))


def _resolve(path: Path) -> Path:
    """Absolute, symlink- and junction-free, WITHOUT REQUIRING EXISTENCE.

    A write creates the file, so the interesting path almost never exists yet.
    ``strict=False`` resolves as much of the chain as does exist and appends the
    rest verbatim, which is exactly the intended location: if the containing
    directory is a junction into another project, the junction is followed and
    the containment check sees the real location rather than the pretty one.
    Resolution failing (a permission error mid-chain, a cyclic link) falls back
    to the unresolved absolute path rather than raising - a decision function
    that can throw is a gate that can dam the session.
    """
    try:
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return Path(os.path.abspath(str(path)))


def within(child: Path, parent: Path) -> bool:
    """Is ``child`` at or below ``parent``? Both already resolved.

    Separator-aware on purpose. A prefix comparison on the bare strings says
    C:\\Games\\Ember-old is inside C:\\Games\\Ember, which would hand an agent
    the wrong project by way of a sibling directory whose name starts the same.
    """
    child_key, parent_key = _key(child), _key(parent)
    if child_key == parent_key:
        return True
    stem = parent_key.rstrip("\\/")
    return child_key.startswith(stem + os.sep)


def project_at(path: Path) -> Optional[Path]:
    """The Builders Gate project containing ``path``, or None. Filesystem only.

    Starts AT ``path`` rather than at its parent, so a target that is the
    project root itself (or the root's own ``.bgate``) is recognised as that
    project instead of being attributed to whatever encloses it.
    """
    try:
        candidates = (path, *path.parents)
    except (OSError, ValueError):
        return None
    for candidate in candidates:
        try:
            if (candidate / PROJECT_DIRNAME / PROJECT_DBNAME).exists():
                return candidate
        except OSError:
            continue
    return None


def mode() -> str:
    """How hard to enforce the project boundary. Never raises.

    An unrecognised value is the default rather than an error: BGATE_AEGIS is
    set by hand and by dispatch, and a typo that silently disabled the gate
    would be the worst of the available failures.
    """
    chosen = os.environ.get("BGATE_AEGIS", "").strip().lower()
    return chosen if chosen in MODES else DEFAULT_MODE


def allowlist_dirs() -> list[Path]:
    """The directories a seated agent may touch outside its own project.

    IMPURE, like ``mode`` above and unlike everything else here, and it is
    separate from ``decide`` for that reason: it reads the environment, so a
    caller that wants a fully pure decision passes its own list. ``decide``
    calls this only when given none.

    Three entries, each earning its place by being somewhere the agent's TOOLS
    write rather than somewhere its work goes:

      * the system temp dir - every subprocess, every atomic-write dance, every
        Blender/ffmpeg scratch file lands here. Denying it breaks the toolchain
        rather than containing anything.
      * ``~/.bgate`` - the user-scoped toolchain dir (registry, active pointer,
        global .env). Note that the SCRATCH PROJECT lives inside it and is still
        denied: it is a project, and the different-project rule outranks this
        one. That is deliberate. A seated agent's output belongs to its game.
      * the runtime's own config dir - ``~/.claude``, where the agent's session
        state and settings live. It is already writing there by existing.

    BGATE_HOME and CLAUDE_CONFIG_DIR are honoured because both move the real
    directory; a hardcoded ~/.bgate would allowlist a path the user is not using
    and deny the one they are.
    """
    import tempfile

    home = Path.home()
    bgate_home = os.environ.get("BGATE_HOME") or ""
    claude_home = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    return [
        Path(tempfile.gettempdir()),
        Path(bgate_home).expanduser() if bgate_home else home / ".bgate",
        Path(claude_home).expanduser() if claude_home else home / ".claude",
    ]


def decide(pinned_root, target, *, cwd=None, seat: str = "",
           allowlist: Optional[Iterable] = None) -> dict:
    """May the agent pinned to ``pinned_root`` touch ``target``?

    Returns ``{"verdict": "allow"|"deny"|"outside", "reason": str,
    "scope": str}``. ``reason`` is written to be shown to the agent verbatim -
    it names both projects when they differ, because "denied" without the two
    names sends the agent looking for a lane problem it does not have.

    ``pinned_root`` falsy means nobody pinned this session: a hand-started
    director, not a dispatched worker. That is not a containment failure, it is
    the absence of a claim to enforce, so everything is allowed and ``scope`` is
    ``"unscoped"`` to say why - a caller wanting to log "this write was
    unscoped" can, and one wanting to require a scope can check for it.

    ``cwd`` resolves a relative ``target``. A relative target with no ``cwd`` is
    REFUSED rather than resolved against this process's working directory: the
    hook runs in a process whose cwd has nothing to do with the agent's, and
    guessing there is how a relative write silently lands in the wrong project.
    The session's cwd is in the hook payload; pass it.
    """
    if not pinned_root:
        return {"verdict": ALLOW, "scope": "unscoped",
                "reason": "no project pinned for this session (seatless "
                          "director) - nothing to contain"}

    root = _resolve(Path(pinned_root))
    scope = str(root)
    target_path = Path(target)
    if not target_path.is_absolute():
        if cwd is None:
            return {"verdict": OUTSIDE, "scope": scope,
                    "reason": f"{target!r} is a relative path and no cwd was "
                              "given to resolve it against; the location it "
                              "means cannot be determined here"}
        target_path = Path(cwd) / target_path
    target_path = _resolve(target_path)

    if within(target_path, root):
        return {"verdict": ALLOW, "scope": scope,
                "reason": f"inside the pinned project {root}"}

    # A DIFFERENT GAME IS ON THE OTHER SIDE OF THIS CALL. Checked before the
    # allowlist so that a project living inside an allowlisted directory - the
    # scratch project under ~/.bgate is the real case - stays protected.
    other = project_at(target_path)
    if other is not None and not within(other, root):
        who = f"seat {seat!r} " if seat else ""
        return {"verdict": DENY, "scope": scope,
                "reason": f"{who}is pinned to {root} but {target_path} is "
                          f"inside a different Builders Gate project ({other})"}

    for allowed in (allowlist if allowlist is not None else allowlist_dirs()):
        if within(target_path, _resolve(Path(allowed))):
            return {"verdict": ALLOW, "scope": scope,
                    "reason": f"outside {root} but under the allowed toolchain "
                              f"directory {Path(allowed)}"}

    return {"verdict": OUTSIDE, "scope": scope,
            "reason": f"{target_path} is outside the pinned project {root} and "
                      "outside every Builders Gate project"}


def is_allowed(result: dict) -> bool:
    """Verdict as the one bit most callers want. ``outside`` counts as refused -
    see the module docstring for why it is nonetheless not ``deny``."""
    return result.get("verdict") == ALLOW
