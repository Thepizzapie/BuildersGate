"""Git, as the audit trail for what an agent actually did.

Dispatched agents run with ``--permission-mode acceptEdits`` and Bash straight
into the live working tree. Until this module there was no branch, no diff and
no undo: ``iterations`` already ran ``git diff --binary HEAD`` and threw the
diff away to keep a sha256, so the product could prove *that* something changed
and never show *what*. Two personas named exactly that as their sole adoption
blocker.

The contract here is a boundary commit per run (``work_item.base_commit``) plus
three reads against it — what changed, what it looks like, and put it back.
Worktree isolation is a bonus on top, not the mechanism: most projects dispatch
straight into the tree and must still get the diff.

Nothing in here raises. A project with no git, no commits, or no git binary is
the common first-run state, and a missing audit trail must never take down the
dispatcher — every entry point answers ``{"available": False, "reason": ...}``.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# A single file's diff is for a human to read, not a payload. Past this we send
# the head and say so rather than shipping a 40MB generated .tres to the browser.
MAX_DIFF_CHARS = 200_000

# Never surface (or revert) the dashboard's own state as if the agent wrote it.
#
# `.godot/` joined the list because that is where the harness's engine scratch
# now lives (see godot.SCRATCH_DIR) AND because it is the engine's own cache:
# running the game at all rewrites it, so counting it as the agent's work made
# every capture look like an edit.
_ALWAYS_IGNORE = (".bgate/", ".git/", ".godot/")

# A DIRTY TREE REFUSES THE WHOLE BOARD, NOT ONE ITEM — which is why a file the
# HARNESS wrote for its own purposes must never make the tree dirty. Taking a
# screenshot stalled every seat, repeatedly, because the capture injection
# leaves an `override.cfg` beside project.godot and Godot has no CLI flag that
# would let it live anywhere else.
#
# It is recognised by CONTENT, not by name: our override.cfg registers a
# BGate autoload and nothing else, and a project's own override.cfg is a
# legitimate thing to have that SHOULD still dirty the tree. Guessing by
# filename would silence both.
_HARNESS_MARKERS = ("BGateShot", "BGateEvidence", "BGATE-INJECTED")
_MAYBE_OURS = ("override.cfg", ".bgate_shot.gd", ".bgate_evidence.gd")


def _harness_scratch(root: str | os.PathLike[str], path: str) -> bool:
    """Is this an artefact the harness wrote for itself? Content-checked."""
    name = path.rsplit("/", 1)[-1]
    if name not in _MAYBE_OURS:
        return False
    try:
        head = (Path(root) / path).read_text(
            encoding="utf-8", errors="replace")[:600]
    except OSError:
        return False
    return any(mark in head for mark in _HARNESS_MARKERS)


def _run(cwd: str | os.PathLike[str], args: Sequence[str], *,
         timeout: int = 20, binary: bool = False):
    """Run a git command. Returns (ok, out, err); never raises."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True,
            stdin=subprocess.DEVNULL, timeout=timeout,
            creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return False, (b"" if binary else ""), "git is not installed, or not on PATH"
    except subprocess.TimeoutExpired:
        return False, (b"" if binary else ""), f"git did not finish within {timeout}s"
    except (OSError, subprocess.SubprocessError):
        # A LITERAL PER BRANCH, NOT THE EXCEPTION'S OWN WORDS. This string is
        # returned to the dashboard, and `FileNotFoundError: [WinError 2] The
        # system cannot find the file specified` told the reader less than the
        # two sentences above do — while being the kind of text that names
        # paths we did not choose to publish.
        return False, (b"" if binary else ""), "git could not be run"
    out = proc.stdout if binary else proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return proc.returncode == 0, out, err.strip()


def _has_git_dir(root: str | os.PathLike[str]) -> bool:
    """Is the project root ITSELF a repo? (.git is a file inside a worktree,
    hence exists() rather than is_dir().)

    Deliberately not a walk up the tree: git would happily answer for an
    enclosing repo — on a laptop where the home directory is a dotfiles repo,
    that makes ``/diff`` the user's whole home and ``/revert`` a weapon. The
    project owns its own history or the feature stays off.
    """
    return (Path(root) / ".git").exists()


def probe(root: str | os.PathLike[str]) -> dict:
    """Is this project a usable git repo? The gate every other call runs first."""
    if not _has_git_dir(root):
        return {"available": False,
                "reason": "the project root is not a git repository"}
    ok, out, err = _run(root, ["rev-parse", "--show-toplevel"], timeout=10)
    if not ok:
        reason = err or "git is not installed or this is not a repository"
        return {"available": False, "reason": reason}
    ok, head, err = _run(root, ["rev-parse", "HEAD"], timeout=10)
    if not ok:
        # A repo with zero commits has no boundary to diff against.
        return {"available": False,
                "reason": "repository has no commits yet — nothing to diff against"}
    return {"available": True, "reason": "", "toplevel": out.strip(),
            "head": head.strip()}


def initialize(root: str | os.PathLike[str], *,
               message: str = "Initial Builders Gate project") -> dict:
    """Create the standalone repository a newly scaffolded project needs.

    A project nested below some unrelated repository must not inherit that
    repository: every diff, worktree and revert operation is scoped to the
    project root itself. The explicit identity is command-local, so this works
    on a new machine without changing the operator's global Git configuration.
    """
    root = Path(root)
    if _has_git_dir(root):
        state = probe(root)
        return {**state, "created": False}

    ok, _out, err = _run(root, ["init", "-q"])
    if not ok:
        return {"available": False, "created": False,
                "reason": f"git init failed: {err}"}
    ok, _out, err = _run(root, ["add", "-A"])
    if not ok:
        return {"available": False, "created": True,
                "reason": f"git add failed: {err}"}
    ok, _out, err = _run(root, [
        "-c", "user.name=Builders Gate",
        "-c", "user.email=builders-gate@localhost",
        "commit", "-qm", message, "--no-gpg-sign",
    ])
    if not ok:
        return {"available": False, "created": True,
                "reason": f"git commit failed: {err}"}
    return {**probe(root), "created": True}


def head(root: str | os.PathLike[str]) -> str:
    ok, out, _ = _run(root, ["rev-parse", "HEAD"], timeout=10)
    return out.strip() if ok else ""


#: THE SAFETY NET, NAMED WHEREVER DIRTINESS IS REPORTED. Nothing in the system
#: said that auto-commit exists, so "uncommitted" was read as "unsaved" — and
#: an operator who believes an agent's work has not landed makes exactly the
#: wrong decision about it (re-run it, or discard the tree).
MEANING_OF_DIRTY = (
    "UNCOMMITTED IS NOT UNSAVED. These files are on disk. Every dispatched run "
    "commits the paths it touched when it finishes (the dispatch.auto_commit "
    "setting, on by default), against the base_commit stamped on its work item "
    "— so `git diff <base_commit>` is what a run changed and `git log` is the "
    "undo history. A dirty tree means a run is still going, or a commit was "
    "declined because the tree already held edits that were not that run's. "
    "It never means the work was lost.")


def _ignored(path: str) -> bool:
    return any(path.startswith(p) for p in _ALWAYS_IGNORE)


def dirty(root: str | os.PathLike[str]) -> dict:
    """Uncommitted work in the tree, as ``{available, dirty, paths}``.

    Dispatching on top of a dirty tree is not forbidden, but it makes the diff
    lie — the agent's changes and the human's are indistinguishable after the
    fact — so the dispatcher refuses unless told the mixing is intended.
    """
    state = probe(root)
    if not state["available"]:
        return {"available": False, "reason": state["reason"], "dirty": False,
                "paths": []}
    ok, out, err = _run(root, ["status", "--porcelain=v1", "-uall"])
    if not ok:
        return {"available": False, "reason": err, "dirty": False, "paths": []}
    paths: list[str] = []
    scratch: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:  # a rename reports "old -> new"
            path = path.split(" -> ", 1)[1]
        if _ignored(path):
            continue
        if _harness_scratch(root, path):
            scratch.append(path)
            continue
        paths.append(path)
    return {"available": True, "reason": "", "dirty": bool(paths),
            "paths": paths,
            # Reported, not hidden: a leftover means a capture was KILLED, which
            # is worth knowing even though it no longer stops the board.
            "harness_scratch": scratch,
            # WHAT DIRTY DOES AND DOES NOT MEAN. Read as "the work has not
            # landed", which is close to the opposite of the truth: the files
            # are on disk, and every dispatched run commits its OWN paths when
            # it finishes (dispatch.auto_commit, on by default). Uncommitted
            # means a run is still going, or the commit was declined because
            # the tree already held somebody else's edits — not that anything
            # was lost.
            "means": MEANING_OF_DIRTY}


def _identity_args(root: str | os.PathLike[str]) -> list[str]:
    """A command-local author when the repository has none configured.

    A machine with no user.name/user.email refuses every commit with "Please
    tell me who you are" - the CI runner does, and so does a fresh box a
    project was cloned onto. initialize() already carries this identity for
    the first commit; the agent commits it makes afterwards died on the same
    rule (Linux CI, chaos worktree tests). A configured identity is left
    alone: the human's name stays on the human's machine's commits.
    """
    ok_name, name, _ = _run(root, ["config", "user.name"], timeout=10)
    ok_mail, mail, _ = _run(root, ["config", "user.email"], timeout=10)
    if ok_name and name.strip() and ok_mail and mail.strip():
        return []
    # BEFORE the subcommand: `git -c k=v commit` sets config; `git commit -c`
    # means "reuse that commit's message" and refuses -m beside it.
    return ["-c", "user.name=Builders Gate", "-c", "user.email=builders-gate@localhost"]


def commit_paths(root: str | os.PathLike[str], paths: Sequence[str],
                 message: str) -> dict:
    """Commit EXACTLY these paths, leaving everything else uncommitted.

    THE DEADLOCK THIS ENDS. Nothing in this system ever committed, and a
    dispatch refuses a dirty tree by default — so the first agent to write a
    file made the tree dirty and every later dispatch was refused, forever,
    with a 20-second retry. Autopilot could not finish a second item without a
    human running git by hand. An overnight board of thirty items woke up with
    three done and the rest untouched.

    PATHS, NOT `commit -a`, and that is the whole safety argument: the caller
    passes the paths ITS OWN RUN touched (gitwork.touched since the run's base
    commit), so a human's unrelated uncommitted work is never swept into an
    agent's commit. If the human's edits are what make the tree dirty, it
    STAYS dirty and the next dispatch is still refused — which is the original
    rule working as intended rather than being bypassed.

    Never raises: a failed commit is reported and the run stands. The commit
    is bookkeeping on top of work that already landed on disk, and losing it
    must not turn a successful run into a failed one.
    """
    state = probe(root)
    if not state["available"]:
        return {"ok": False, "reason": state["reason"], "committed": []}
    wanted = [str(p).replace("\\", "/") for p in (paths or [])
              if str(p).strip() and not _ignored(str(p).replace("\\", "/"))]
    if not wanted:
        return {"ok": False, "reason": "nothing to commit", "committed": []}
    # -- ends the option list: a path that begins with a dash is a filename
    # here, not a flag, and a generated asset legitimately can.
    ok, _out, err = _run(root, ["add", "--", *wanted])
    if not ok:
        return {"ok": False, "reason": f"git add failed: {err}", "committed": []}
    # Staged-only check: `add` on an unchanged path stages nothing, and
    # `commit` with nothing staged is an error whose message reads like a
    # fault. Asking first turns that into an honest "nothing changed".
    ok, staged, _err = _run(root, ["diff", "--cached", "--name-only"])
    if not ok or not staged.strip():
        return {"ok": False, "reason": "nothing staged — no tracked change",
                "committed": []}
    ok, _out, err = _run(root, [*_identity_args(root), "commit", "--no-verify",
                                "--no-gpg-sign", "-m", message])
    if not ok:
        return {"ok": False, "reason": f"git commit failed: {err}",
                "committed": []}
    return {"ok": True, "reason": "", "commit": head(root),
            "committed": [p for p in staged.splitlines() if p.strip()]}


def touched(root: str | os.PathLike[str], base: str) -> dict:
    """Every path that changed since ``base``, tracked edits AND new files.

    ``git diff --name-only`` alone is the trap: an agent's most common output is
    a file that did not exist before, which is untracked and therefore invisible
    to diff. The revert scope would silently skip exactly the things it most
    needs to clean up.
    """
    state = probe(root)
    if not state["available"]:
        return {"available": False, "reason": state["reason"], "paths": [],
                "untracked": []}
    ok, out, err = _run(root, ["diff", "--name-only", base, "--"])
    if not ok:
        return {"available": False, "reason": err or f"unknown commit {base}",
                "paths": [], "untracked": []}
    tracked = [p.strip().strip('"') for p in out.splitlines() if p.strip()]
    ok, out, _ = _run(root, ["ls-files", "--others", "--exclude-standard"])
    untracked = [p.strip().strip('"') for p in out.splitlines() if p.strip()] if ok else []
    tracked = [p for p in tracked if not _ignored(p)]
    untracked = [p for p in untracked if not _ignored(p)]
    return {"available": True, "reason": "", "paths": sorted(set(tracked + untracked)),
            "untracked": untracked}


def _blob_sizes(root: Path, base: str) -> dict[str, int]:
    """Every blob size at ``base``, in one call.

    Same lesson as _split_by_file: `cat-file -s` per binary was another process
    per file, and an art item's scope is mostly binaries. `ls-tree -r -l` gives
    the whole tree's sizes in one spawn. Returns {} on failure, which leaves the
    per-file path below as the fallback.
    """
    ok, out, _ = _run(root, ["ls-tree", "-r", "-l", base], timeout=60)
    if not ok:
        return {}
    sizes: dict[str, int] = {}
    for line in out.splitlines():
        # "<mode> <type> <sha> <size>\t<path>"
        meta, tab, path = line.partition("\t")
        if not tab:
            continue
        parts = meta.split()
        if len(parts) >= 4 and parts[3].isdigit():
            sizes[_unquote(path)] = int(parts[3])
    return sizes


def _binary_size_delta(root: Path, base: str, path: str,
                       sizes: Optional[dict[str, int]] = None) -> dict:
    """Name + byte delta for a binary, because dumping the bytes helps nobody."""
    if sizes is not None and path in sizes:
        before = sizes[path]
    else:
        ok, out, _ = _run(root, ["cat-file", "-s", f"{base}:{path}"], timeout=10)
        before = int(out.strip()) if ok and out.strip().isdigit() else 0
    try:
        after = (root / path).stat().st_size
    except OSError:
        after = 0
    return {"binary": True, "size_before": before, "size_after": after,
            "size_delta": after - before, "diff": ""}


def _new_file_diff(root: Path, path: str) -> dict:
    """A synthetic unified diff for an untracked file — git will not produce one
    for a path it has never seen, and 'added' with no content is not a review."""
    target = root / path
    try:
        size = target.stat().st_size
        raw = target.read_bytes()
    except OSError as exc:
        return {"binary": False, "diff": "", "error": str(exc), "added": 0,
                "removed": 0}
    if b"\0" in raw[:8000]:
        return {"binary": True, "size_before": 0, "size_after": size,
                "size_delta": size, "diff": ""}
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    body = "\n".join("+" + ln for ln in lines)
    diff = (f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n")
    return {"binary": False, "diff": diff[:MAX_DIFF_CHARS],
            "truncated": len(diff) > MAX_DIFF_CHARS,
            "added": len(lines), "removed": 0}


def _split_by_file(text: str) -> dict[str, str]:
    """One `git diff` output carved into its per-file sections.

    THE ALTERNATIVE WAS A SUBPROCESS PER FILE, AND IT DID NOT SCALE. `diff()`
    used to run `git diff <base> -- <path>` once per changed path. On an item
    whose base commit is shared with forty other runs the scope is the whole
    working tree — 1835 files on the project this was found in — and at roughly
    50ms per process spawn on Windows that is a minute and a half of subprocess
    overhead for work git does in ONE second. The endpoint simply never
    answered: /api/queue/334/diff returned nothing after 90 seconds.

    Git already writes the per-file sections in one stream, each opening with
    `diff --git a/<path> b/<path>`, so the split is free. Paths with spaces or
    non-ASCII come back quoted, which is what `_unquote` handles.
    """
    out: dict[str, str] = {}
    if not text:
        return out
    for chunk in text.split("\ndiff --git ")[0:]:
        chunk = chunk.lstrip()
        if chunk.startswith("diff --git "):
            chunk = chunk[len("diff --git "):]
        if not chunk:
            continue
        header, _, _rest = chunk.partition("\n")
        # `a/<path> b/<path>`; take the b-side, which is the name after any
        # rename, and is what the caller keyed the scope list by.
        marker = header.rfind(" b/")
        if marker == -1:
            continue
        path = _unquote(header[marker + 3:].strip())
        out[path] = "diff --git " + chunk
    return out


def _unquote(path: str) -> str:
    """Git quotes paths with spaces or non-ASCII; the scope list does not."""
    path = path.strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        try:
            return path[1:-1].encode("latin-1", "backslashreplace") \
                             .decode("unicode_escape")
        except Exception:
            return path[1:-1]
    return path


def diff(root: str | os.PathLike[str], base: str,
         paths: Optional[Iterable[str]] = None) -> dict:
    """Per-file unified diffs since ``base`` — the surface the reviewer reads."""
    root = Path(root)
    scope = touched(root, base)
    if not scope["available"]:
        return {"available": False, "reason": scope["reason"], "base": base,
                "files": []}
    wanted = set(paths) if paths else None
    untracked = set(scope["untracked"])

    # numstat is the cheap way to learn added/removed AND binary-ness in one
    # call: git writes "-\t-\tpath" for anything it will not diff as text.
    stats: dict[str, tuple[str, str]] = {}
    ok, out, _ = _run(root, ["diff", "--numstat", base, "--"], timeout=30)
    if ok:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                stats[parts[2].strip().strip('"')] = (parts[0], parts[1])

    # ONE call for every tracked file's text, split locally. See _split_by_file:
    # the per-path loop this replaced spawned a process per file and timed the
    # endpoint out entirely on a large shared base. Scoped to `wanted` when the
    # caller asked for specific paths, so a single-file request stays cheap.
    bulk_args = ["diff", base, "--"] + (sorted(wanted) if wanted else [])
    ok_bulk, bulk_text, bulk_err = _run(root, bulk_args, timeout=60)
    sections = _split_by_file(bulk_text) if ok_bulk else {}
    # Binary sizes in one call too — an art item's scope is mostly binaries, and
    # a cat-file per file is the same mistake in a different place.
    blob_sizes = _blob_sizes(root, base)

    files = []
    for path in scope["paths"]:
        if wanted is not None and path not in wanted:
            continue
        if path in untracked:
            entry = {"path": path, "status": "added"}
            entry.update(_new_file_diff(root, path))
            files.append(entry)
            continue
        added, removed = stats.get(path, ("0", "0"))
        entry = {"path": path,
                 "status": "deleted" if not (root / path).exists() else "modified"}
        if added == "-" or removed == "-":
            entry.update(_binary_size_delta(root, base, path, blob_sizes))
            files.append(entry)
            continue
        text = sections.get(path, "")
        entry.update({
            "binary": False,
            "added": int(added) if added.isdigit() else 0,
            "removed": int(removed) if removed.isdigit() else 0,
            "diff": text[:MAX_DIFF_CHARS],
            "truncated": len(text) > MAX_DIFF_CHARS,
        })
        # A path git listed as changed but whose section we could not find is a
        # parsing miss, not an empty diff — say so rather than showing "no
        # changes" for a file that has them.
        if not ok_bulk:
            entry["error"] = bulk_err
        elif not text:
            entry["error"] = "no diff section returned for this path"
        files.append(entry)
    return {"available": True, "reason": "", "base": base, "files": files,
            "count": len(files)}


def fingerprint(root: str | os.PathLike[str],
                paths: Iterable[str]) -> dict[str, str]:
    """sha256 per path (empty string = the file is gone).

    Taken when a run ends and checked again at revert: it is what lets revert
    say "someone edited this after the agent did" instead of silently throwing
    away a human's work.
    """
    out: dict[str, str] = {}
    for path in paths:
        target = Path(root) / path
        try:
            out[path] = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            out[path] = ""
    return out


def revert(root: str | os.PathLike[str], base: str, *,
           expect: Optional[dict[str, str]] = None,
           paths: Optional[Iterable[str]] = None) -> dict:
    """Undo a run: restore every path it touched to its ``base`` content.

    Scoped by construction — only paths the run itself changed are considered,
    so an unrelated edit elsewhere in the tree survives. If ``expect`` (a
    :func:`fingerprint` from when the run ended) disagrees with what is on disk
    now, nothing is touched at all: a partial revert is worse than a refusal.
    """
    root = Path(root)
    scope = touched(root, base)
    if not scope["available"]:
        return {"available": False, "reason": scope["reason"], "reverted": [],
                "conflicts": []}
    targets = [p for p in scope["paths"]
               if paths is None or p in set(paths)]
    if not targets:
        return {"available": True, "reason": "nothing changed since the run started",
                "reverted": [], "conflicts": []}

    if expect is not None:
        # A path that is dirty now but was not in the run's fingerprint appeared
        # after the fact — not ours, so it drops out of scope rather than being
        # reverted. Anything that IS ours and no longer matches is a refusal.
        targets = [p for p in targets if p in expect]
        now = fingerprint(root, targets)
        conflicts = [p for p in targets if now.get(p, "") != expect[p]]
        if conflicts:
            return {"available": True, "reverted": [],
                    "conflicts": sorted(conflicts),
                    "reason": "these paths changed after the run finished — "
                              "revert would discard someone else's work"}
        if not targets:
            return {"available": True, "reverted": [], "conflicts": [],
                    "reason": "nothing this run touched is still changed"}

    reverted, failed = [], []
    for path in targets:
        existed, _, _ = _run(root, ["cat-file", "-e", f"{base}:{path}"], timeout=10)
        if existed:
            ok, _, err = _run(root, ["checkout", base, "--", path], timeout=30)
            if ok:
                reverted.append(path)
            else:
                failed.append({"path": path, "error": err})
            continue
        # Did not exist at the boundary: the run created it, so undo == delete.
        try:
            (root / path).unlink()
            reverted.append(path)
        except FileNotFoundError:
            reverted.append(path)
        except OSError as exc:
            failed.append({"path": path, "error": str(exc)})
    return {"available": True, "reason": "", "reverted": sorted(reverted),
            "failed": failed, "conflicts": []}


# ---------------------------------------------------------------------------
# Optional worktree isolation
# ---------------------------------------------------------------------------

# Isolation is OFF by default: a worktree moves the agent's cwd, which is a
# bigger change to the run than most projects want, and base_commit + diff +
# revert work identically without it. The switch is read through the settings
# registry (`dispatch.isolation`, which declares BGATE_GIT_ISOLATION as its
# supplying variable) at dispatch.py's call site — this module used to carry a
# second `isolation_enabled()` that read the bare variable and so ignored the
# stored value. Nothing called it; the registry is the one read path.

def worktree_paths(root: str | os.PathLike[str], item_id: int) -> tuple[Path, str]:
    return Path(root) / ".bgate" / "work" / f"item-{item_id}", f"bgate/item-{item_id}"


def _integration_path(root: str | os.PathLike[str], item_id: int) -> Path:
    return Path(root) / ".bgate" / "integrations" / f"item-{int(item_id)}.json"


#: How many times one Chaos branch may fail to merge, or be handed to the
#: Director without landing, before the item is failed instead of re-asked.
#: Without a ceiling a conflicting branch was re-prompted every five minutes
#: for as long as the server stayed up.
MAX_INTEGRATION_ATTEMPTS = 3


def _save_integration(root: str | os.PathLike[str], item_id: int,
                      result: dict) -> dict:
    prior = integration(root, item_id)
    row = {"attempts": int(prior.get("attempts") or 0),
           "prompts": int(prior.get("prompts") or 0),
           **result, "item_id": int(item_id)}
    try:
        path = _integration_path(root, item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row), encoding="utf-8")
    except OSError:
        pass
    return row


def integration(root: str | os.PathLike[str], item_id: int) -> dict:
    try:
        row = json.loads(_integration_path(root, item_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return row if isinstance(row, dict) else {}


def note_integration_prompt(root: str | os.PathLike[str], item_id: int) -> dict:
    """Count one hand-off to the Director. Past the ceiling the row stops being
    pending, so the scheduler stops asking."""
    row = integration(root, item_id)
    if not row.get("pending"):
        return row
    prompts = int(row.get("prompts") or 0) + 1
    if prompts > MAX_INTEGRATION_ATTEMPTS:
        return abandon_integration(
            root, item_id,
            f"handed to the Director {prompts - 1} times without landing")
    return _save_integration(root, item_id, {**row, "prompts": prompts})


def abandon_integration(root: str | os.PathLike[str], item_id: int,
                        reason: str) -> dict:
    row = integration(root, item_id)
    return _save_integration(root, item_id, {
        **row, "pending": False, "integrated": False, "failed": True,
        "reason": reason})


def _merge_failed(root, item_id: int, result: dict) -> dict:
    """A failure the BRANCH caused (conflict, missing branch, bad resolution
    commit). Counted; the ceiling fails the item. A dirty working branch is the
    human's state, not the branch's, and is not counted."""
    prior = integration(root, item_id)
    attempts = int(prior.get("attempts") or 0) + 1
    row = {**result, "attempts": attempts}
    if attempts >= MAX_INTEGRATION_ATTEMPTS:
        row.update(pending=False, failed=True,
                   reason=f"{result.get('reason') or 'merge failed'} "
                          f"({attempts} attempts; integration abandoned)")
    return _save_integration(root, item_id, row)


def integrations(root: str | os.PathLike[str], *, pending: bool = False) -> list[dict]:
    folder = Path(root) / ".bgate" / "integrations"
    rows = []
    try:
        paths = list(folder.glob("item-*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict) and (not pending or row.get("pending")):
            rows.append(row)
    return sorted(rows, key=lambda row: int(row.get("item_id") or 0))


def make_worktree(root: str | os.PathLike[str], item_id: int, *,
                  base: str = "HEAD") -> dict:
    """A private checkout at .bgate/work/item-<id> on branch bgate/item-<id>.

    The agent still runs with BGATE_ROOT pointing at the real project, so its
    MCP tools, DB and locks are the shared ones — only file edits are isolated.
    """
    state = probe(root)
    if not state["available"]:
        return {"available": False, "reason": state["reason"]}
    path, branch = worktree_paths(root, item_id)
    if path.exists():
        return {"available": True, "worktree": str(path), "branch": branch,
                "reused": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, _, err = _run(root, ["worktree", "add", "-B", branch, str(path), base],
                      timeout=120)
    if not ok:
        return {"available": False, "reason": err or "git worktree add failed"}
    return {"available": True, "worktree": str(path), "branch": branch,
            "reused": False}


def prepare_worktree(root: str | os.PathLike[str], item_id: int,
                     worktree: str | os.PathLike[str], base: str,
                     *, seat: str = "") -> dict:
    """Commit one isolated run so its branch is ready for Director review."""
    expected, branch = worktree_paths(root, int(item_id))
    try:
        if Path(worktree).resolve() != expected.resolve():
            return _save_integration(root, item_id, {
                "available": False, "pending": False,
                "reason": "worktree path does not belong to this item"})
    except OSError:
        return _save_integration(root, item_id, {
            "available": False, "pending": False,
            "reason": "worktree path could not be resolved"})
    scope = touched(expected, base)
    if not scope.get("available"):
        return _save_integration(root, item_id, {
            "available": False, "pending": False,
            "reason": scope.get("reason") or "worktree diff unavailable"})
    paths = list(scope.get("paths") or [])
    if not paths:
        return _save_integration(root, item_id, {
                "available": True, "pending": False, "integrated": True,
                "reason": "no file changes", "branch": branch,
                "worktree": str(expected), "paths": []})
    made = commit_paths(
        expected, paths,
        f"bgate: chaos item #{int(item_id)}"
        + (f" [{seat}]" if seat else "") + " - ready for Director integration")
    if not made.get("ok"):
        return _save_integration(root, item_id, {
                "available": False, "pending": False,
                "reason": made.get("reason") or "worktree commit failed",
                "branch": branch, "worktree": str(expected), "paths": paths})
    return _save_integration(root, item_id, {
            "available": True, "pending": True, "integrated": False,
            "reason": "", "branch": branch, "worktree": str(expected),
            "paths": paths, "commit": made.get("commit") or ""})


def merge_worktree(root: str | os.PathLike[str], item_id: int) -> dict:
    """Merge one prepared item branch into the project's current branch.

    A failed merge is aborted before returning, so conflict handling starts
    from a clean working branch rather than a half-merged repository.
    """
    root = Path(root)
    worktree, branch = worktree_paths(root, int(item_id))
    state = dirty(root)
    if not state.get("available"):
        return _save_integration(root, item_id, {
                "available": False, "integrated": False, "pending": True,
                "reason": state.get("reason") or "git unavailable"})
    if state.get("dirty"):
        return _save_integration(root, item_id, {
                "available": True, "integrated": False, "pending": True,
                "reason": "working branch is dirty",
                "paths": state.get("paths") or []})
    work_state = dirty(worktree)
    if not work_state.get("available"):
        return _save_integration(root, item_id, {
                "available": False, "integrated": False, "pending": True,
                "reason": work_state.get("reason") or "worktree unavailable",
                "branch": branch, "worktree": str(worktree)})
    if work_state.get("dirty"):
        fixed = commit_paths(
            worktree, work_state.get("paths") or [],
            f"bgate: director resolution for chaos item #{int(item_id)}")
        if not fixed.get("ok"):
            return _merge_failed(root, item_id, {
                    "available": True, "integrated": False, "pending": True,
                    "reason": fixed.get("reason") or
                              "Director resolution commit failed",
                    "branch": branch, "worktree": str(worktree),
                    "paths": work_state.get("paths") or []})
    ok, commit, err = _run(root, ["rev-parse", "--verify", branch])
    if not ok:
        return _merge_failed(root, item_id, {
                "available": True, "integrated": False, "pending": True,
                "reason": err or f"branch {branch} does not exist"})
    ok, _out, err = _run(root, [*_identity_args(root), "merge", "--no-ff", "--no-edit",
                                "--no-gpg-sign", branch], timeout=120)
    if not ok:
        _ok, conflicts, _why = _run(
            root, ["diff", "--name-only", "--diff-filter=U"], timeout=30)
        _run(root, ["merge", "--abort"], timeout=30)
        return _merge_failed(root, item_id, {
                "available": True, "integrated": False, "pending": True,
                "reason": err or "merge conflicted",
                "conflicts": [line for line in conflicts.splitlines() if line]})
    merged = head(root)
    return _save_integration(root, item_id, {
            "available": True, "integrated": True, "pending": False,
            "reason": "", "item_id": int(item_id), "branch": branch,
            "worktree": str(worktree), "source_commit": commit.strip(),
            "merge_commit": merged})


def remove_worktree(root: str | os.PathLike[str], item_id: int) -> dict:
    """Drop an isolated run's private checkout. Called from ``/api/queue/{id}
    /revert`` once a FULL revert has landed, which is the only point where the
    worktree is provably worth nothing — see that route for why run-finish is
    the wrong home (the agent's work is uncommitted in there, and diff/peek/
    preview resolve a finished item's files through it)."""
    path, branch = worktree_paths(root, item_id)
    ok, _, err = _run(root, ["worktree", "remove", "--force", str(path)],
                      timeout=60)
    return {"removed": ok, "reason": "" if ok else err, "worktree": str(path),
            "branch": branch}
