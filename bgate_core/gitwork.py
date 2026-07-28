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
_ALWAYS_IGNORE = (".bgate/", ".git/")


def _run(cwd: str | os.PathLike[str], args: Sequence[str], *,
         timeout: int = 20, binary: bool = False):
    """Run a git command. Returns (ok, out, err); never raises."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True,
            stdin=subprocess.DEVNULL, timeout=timeout,
            creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, (b"" if binary else ""), f"{type(exc).__name__}: {exc}"
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


def head(root: str | os.PathLike[str]) -> str:
    ok, out, _ = _run(root, ["rev-parse", "HEAD"], timeout=10)
    return out.strip() if ok else ""


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
    paths = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:  # a rename reports "old -> new"
            path = path.split(" -> ", 1)[1]
        if not _ignored(path):
            paths.append(path)
    return {"available": True, "reason": "", "dirty": bool(paths), "paths": paths}


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


def _binary_size_delta(root: Path, base: str, path: str) -> dict:
    """Name + byte delta for a binary, because dumping the bytes helps nobody."""
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
            entry.update(_binary_size_delta(root, base, path))
            files.append(entry)
            continue
        ok, text, err = _run(root, ["diff", base, "--", path], timeout=30)
        entry.update({
            "binary": False,
            "added": int(added) if added.isdigit() else 0,
            "removed": int(removed) if removed.isdigit() else 0,
            "diff": text[:MAX_DIFF_CHARS] if ok else "",
            "truncated": ok and len(text) > MAX_DIFF_CHARS,
        })
        if not ok:
            entry["error"] = err
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

def isolation_enabled() -> bool:
    """Off by default: a worktree moves the agent's cwd, which is a bigger
    change to the run than most projects want. base_commit + diff + revert work
    identically without it."""
    return os.environ.get("BGATE_GIT_ISOLATION", "").strip().lower() in {
        "1", "true", "yes", "on"}


def worktree_paths(root: str | os.PathLike[str], item_id: int) -> tuple[Path, str]:
    return Path(root) / ".bgate" / "work" / f"item-{item_id}", f"bgate/item-{item_id}"


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


def remove_worktree(root: str | os.PathLike[str], item_id: int) -> dict:
    path, branch = worktree_paths(root, item_id)
    ok, _, err = _run(root, ["worktree", "remove", "--force", str(path)],
                      timeout=60)
    return {"removed": ok, "reason": "" if ok else err, "worktree": str(path),
            "branch": branch}
