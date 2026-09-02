"""What an execution actually wrote, recorded by the harness rather than claimed.

THE FAILURE THIS CLOSES. A QA agent finished a gate and reported "no files were
touched". It had written one: its own `.bgate/progress/item-<id>.jsonl`, because
the WORK MANIFEST rule every seat carries tells it to. The report was not
dishonest -- it answered about the PROJECT's files, correctly, and its own
checkpoint file was invisible to the protocol, to its own model of the job, and
to any check.

The last of those is the real defect. Nothing in the system could contradict it:
the hook logged only failures, the activity ledger has twenty-odd kinds and not
one of them is a file write, and `path_lease` -- the sole trace of a write --
is reaped on expiry by design, because a lock outliving its holder is
indistinguishable from a permanently blocked path. So a completion note's file
list was unfalsifiable, in the one seat whose entire job is refusing to take a
claim at face value.

ASKING THE AGENT HARDER DOES NOT FIX IT. The obvious patch is a required
disclosure field on queue_complete, and it fixes omission rather than accuracy:
an agent that did not realise it wrote a file will not list it either, and
refusing completion without the field breaks every caller and anything already
in flight. The harness already SEES every write -- the PreToolUse hook is on the
path -- and was throwing that away. Recording it turns the file list from
something an agent asserts into something the harness observed, which is the
same discipline this repo applies to every other claim about the world.

ONE FILE PER EXECUTION OWNER, append-only, alongside the agent's own trail and
deliberately not inside it: `.bgate/progress/item-5.jsonl` belongs to the agent
and is prose it chose to write, while `.bgate/writes/item-5.jsonl` belongs to
the harness and is the record the agent does not get a vote on. Mixing them
would let an edit to one look like an edit to the other.

BEST-EFFORT, ALWAYS. This is called from inside the PreToolUse hook, whose one
hard rule is that it must never dam a session: a write the oracle already
allowed cannot be blocked because its bookkeeping failed. Every function here
swallows its own errors.

IT NOW KEEPS A PRE-IMAGE, AND IT IS NOT A BACKUP SYSTEM. The ledger recorded
that a path was written and nothing about what was there before, so a
destructive edit -- an agent replacing a 500-line file with a 3-line stub it
believed was the whole thing -- was recorded accurately and irrecoverably. The
harness watched it happen and kept the receipt, not the goods.

:func:`preimage` copies the file's EXISTING bytes the FIRST time a given
execution touches it, before the write lands. First-touch only, so a file
edited twenty times keeps what it looked like before that agent started rather
than before its last keystroke -- which is the version somebody restoring
actually wants. Bounded by :data:`MAX_PREIMAGE_BYTES` and by
:data:`MAX_PREIMAGE_FILES`, because this rides on the hook's hot path and an
unbounded copy of every generated .glb would make writing slower than working.

WHAT IT IS NOT. Not version control, not a substitute for the auto-commit that
is the real safety net (see :func:`recovery_note`), and not a promise: a write
this harness never saw -- one made by a program the hook cannot parse -- has no
pre-image, and :func:`recoverable` says so rather than implying coverage it
does not have.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import db

DIRNAME = "writes"

# A cap so a runaway loop cannot fill a disk with its own audit trail. Well past
# any real item: the largest single work item in this repo's history touched
# well under a hundred paths.
MAX_LINES = 2000

#: Where first-touch copies live. Beside the ledger, under the harness's own
#: directory, gitignored with everything else in `.bgate/`.
PREIMAGE_DIRNAME = "preimages"

#: Per-file ceiling. Big enough for any source file anybody edits by hand,
#: small enough that a generated mesh or a texture is skipped rather than
#: copied on the hook's hot path.
MAX_PREIMAGE_BYTES = 512 * 1024

#: Per-execution ceiling. An agent touching more files than this is doing a
#: sweep, and a sweep's recovery path is the commit, not a pile of copies.
MAX_PREIMAGE_FILES = 200


# Characters that mean "this is not one file". The hook reads write targets off
# a shell command line, which is a fence and not a parser: a glob it cannot
# expand, an unbalanced bracket, a fragment of a printf argument all arrive here
# looking like paths. From the benchmark projects' own write logs:
#
#     Bash art   0]
#     Bash qa    0
#     Bash qa    thresh:
#     Bash qa    assets/sprites/_qa_*
#     Bash art   art/preview/*.import
#
# None of those is a file, and every one of them was in a record whose whole
# purpose is to be believed about what a run touched. Judging them was right -
# an unreadable write target must still be graded - but RECORDING them makes the
# record noise, and a noisy record gets ignored, which is how the file list went
# back to being self-reported in practice.
#
# Deliberately narrow, and it errs toward keeping: `Makefile` and `.gdignore`
# have no extension and are real, so extensionlessness is not a test. What is
# tested is characters no filename this harness will ever write can contain.
_IMPLAUSIBLE = set('*?[]<>|"') | {":"}


def plausible(rel: str) -> bool:
    """Could this string be a path to one file in this project?

    Not "does it exist" - the hook runs BEFORE the write, so the answer is
    almost always no. This is the weaker and answerable question, and it is the
    one that separates a real target from a shell fragment.
    """
    rel = str(rel or "").strip()
    if not rel:
        return False
    if any(ch in _IMPLAUSIBLE for ch in rel):
        return False
    if any(ch.isspace() for ch in rel):
        return False
    # A bare number is a positional argument somebody's parser lost hold of.
    return not rel.replace(".", "").isdigit()


def _safe(owner: str) -> str:
    """Owner ids become FILENAMES, and they arrive from the environment
    (BGATE_LOCK_OWNER) or a harness payload, so anything that could climb out of
    the directory is stripped rather than escaped."""
    keep = [c for c in str(owner).strip() if c.isalnum() or c in "-_:."]
    return ("".join(keep).replace(":", "-") or "unknown")[:64]


def path_for(root: str | os.PathLike[str], owner: str) -> Path:
    return Path(root) / db.DB_DIRNAME / DIRNAME / f"{_safe(owner)}.jsonl"


def preimage_dir(root: str | os.PathLike[str], owner: str) -> Path:
    return Path(root) / db.DB_DIRNAME / PREIMAGE_DIRNAME / _safe(owner)


def preimage(root: str | os.PathLike[str], rel: str, owner: str) -> dict:
    """Copy a file's CURRENT bytes before an execution first overwrites it.

    Returns ``{kept, why, path}``; never raises, and never blocks the write it
    is about to let happen. Called by the hook immediately before it allows an
    edit, which is the only moment the previous content still exists.

    FIRST TOUCH ONLY. A file edited twenty times in one run keeps what it
    looked like before that run started -- the version somebody restoring
    actually wants -- rather than the state one keystroke ago.
    """
    if not owner:
        return {"kept": False, "why": "no execution owner", "path": ""}
    rel = str(rel).replace("\\", "/").lstrip("/")
    source = Path(root) / rel
    target = preimage_dir(root, owner) / rel
    try:
        if target.exists():
            return {"kept": True, "why": "already captured this run",
                    "path": str(target)}
        if not source.is_file():
            # A NEW FILE HAS NO PRE-IMAGE, and that is worth recording as a
            # fact rather than as an absence: "there was nothing here before"
            # is exactly what a restorer needs to know.
            return {"kept": False, "why": "the file did not exist before this "
                                          "write", "path": ""}
        size = source.stat().st_size
        if size > MAX_PREIMAGE_BYTES:
            return {"kept": False, "path": "",
                    "why": (f"{size} bytes is over the {MAX_PREIMAGE_BYTES}-byte "
                            "pre-image ceiling — recover this one from git")}
        base = preimage_dir(root, owner)
        if base.is_dir() and sum(1 for _ in base.rglob("*")) >= MAX_PREIMAGE_FILES:
            return {"kept": False, "path": "",
                    "why": "this run has already captured its file ceiling — "
                           "recover from git"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return {"kept": True, "why": "", "path": str(target)}
    except Exception:                                             # noqa: BLE001
        return {"kept": False, "why": "the copy failed", "path": ""}


def recoverable(root: str | os.PathLike[str], owner: str) -> dict:
    """What this execution could be rolled back from, and what it could not.

    DECLARES ITS OWN GAPS. A path in the ledger with no pre-image is either a
    file that did not exist before (nothing to restore) or one too big to copy
    (git has it). Reporting only the successes would imply a coverage this does
    not have — see the module docstring.
    """
    kept, missing = [], []
    base = preimage_dir(root, owner)
    for rel in paths_for(root, owner):
        (kept if (base / rel).is_file() else missing).append(rel)
    return {
        "owner": owner, "preimages": kept, "no_preimage": missing,
        "dir": str(base),
        "note": ("a pre-image is the file's content BEFORE this execution "
                 "first touched it. Paths under `no_preimage` were created by "
                 "this run (nothing to restore) or were too large to copy — "
                 "for those, and for anything this harness never observed, "
                 "the commit is the recovery path."),
        "recovery": recovery_note(),
    }


def recovery_note() -> str:
    """THE SAFETY NET, NAMED. See #30: auto-commit is what actually makes an
    agent's work recoverable, and nothing anywhere said so — 'uncommitted' was
    read as 'the work has not landed' when it means the opposite."""
    return ("AUTO-COMMIT IS THE REAL SAFETY NET. Every dispatched run commits "
            "the paths IT touched at the end (gitwork.commit_paths against the "
            "run's base_commit), so `git log` is the undo history and "
            "`git diff <base_commit>` is what a run changed. The pre-images "
            "here are a shorter-range convenience for a single destructive "
            "edit inside a live run. Note that UNCOMMITTED DOES NOT MEAN THE "
            "WORK DID NOT LAND: it means the run has not finished, or the tree "
            "was dirty with somebody else's edits and the commit was declined.")


def record(root: str | os.PathLike[str], rel: str, seat: str, owner: str,
           tool: str = "") -> bool:
    """Note one permitted write. Returns whether it landed; never raises."""
    if not owner:
        return False
    # NORMALISE BEFORE COMPARING, not just before writing. The caller is the hook,
    # whose `rel` is a Path relative to the project root, so on Windows it
    # arrives as `game\scripts\x.gd` while everything already stored reads
    # `game/scripts/x.gd`. An earlier draft normalised only on the way out, so
    # the de-dup below never matched and one file edited twenty times produced
    # twenty identical lines — a record that inflates with effort rather than
    # describing what changed.
    rel = str(rel).replace("\\", "/").lstrip("/")
    if not plausible(rel):
        return False
    try:
        target = path_for(root, owner)
        target.parent.mkdir(parents=True, exist_ok=True)
        # An agent editing one file twenty times should read as one path. Re-
        # reading per write is affordable because these are tens of lines, and it
        # keeps the record a SET of paths, which is what a reader wants.
        if rel in paths_for(root, owner):
            return True
        if _count(target) >= MAX_LINES:
            return False
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "t": time.strftime("%Y-%m-%d %H:%M:%S"),
                "path": rel,
                "seat": str(seat or "")[:40],
                "tool": str(tool or "")[:24],
            }, ensure_ascii=False) + "\n")
            fh.flush()
        return True
    except Exception:
        return False


def _count(target: Path) -> int:
    try:
        with target.open("r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def entries(root: str | os.PathLike[str], owner: str) -> list[dict]:
    """Every recorded write for one execution, in order. Never raises."""
    out: list[dict] = []
    try:
        raw = path_for(root, owner).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue          # a line torn by a kill costs itself, not the rest
        if isinstance(rec, dict) and rec.get("path"):
            out.append(rec)
    return out


def paths_for(root: str | os.PathLike[str], owner: str) -> list[str]:
    """Just the paths, de-duplicated, in first-written order."""
    seen: dict[str, bool] = {}
    for rec in entries(root, owner):
        seen[str(rec["path"])] = True
    return list(seen)


def split(root: str | os.PathLike[str], owner: str) -> dict:
    """The two categories the false report conflated, told apart mechanically.

    `project` is the user's own files -- the ones they mean when they ask what
    an agent changed. `harness` is Builders Gate's own bookkeeping under
    `.bgate/`, which lives inside the project directory but is gitignored on
    setup and never enters their history. Reporting one number for both is what
    produced "no files were touched" from an agent that had written one.
    """
    project, harness = [], []
    for rel in paths_for(root, owner):
        (harness if rel.startswith(".bgate/") else project).append(rel)
    return {"project": project, "harness": harness,
            "total": len(project) + len(harness)}


def summary(root: str | os.PathLike[str], owner: str) -> str:
    """One human-readable block for a completion note. "" when nothing was written.

    Deliberately terse: it is appended to a result field that is capped at 2000
    characters and read by the next agent, so it states the counts, names the
    project files in full, and does not enumerate harness bookkeeping beyond its
    count -- nobody reviewing a deliverable needs eleven checkpoint paths, they
    need to know the number was not zero.
    """
    got = split(root, owner)
    if not got["total"]:
        return ""
    lines = [f"FILES WRITTEN (observed by the harness, not self-reported): "
             f"{len(got['project'])} project, {len(got['harness'])} harness."]
    for rel in got["project"][:40]:
        lines.append(f"  {rel}")
    if len(got["project"]) > 40:
        lines.append(f"  ...and {len(got['project']) - 40} more")
    if got["harness"]:
        # No em dash: this string is read by a human in the dashboard and in the
        # next agent's brief, not just by a reviewer of this file.
        lines.append(f"  (+{len(got['harness'])} under .bgate/, Builders Gate's "
                     "own bookkeeping: gitignored, not your project's files)")
    return "\n".join(lines)


def clear(root: str | os.PathLike[str], owner: str) -> bool:
    """Drop one execution's record. For a re-run that should start clean."""
    try:
        path_for(root, owner).unlink()
        return True
    except OSError:
        return False
