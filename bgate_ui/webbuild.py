"""Keep the in-app playable build honest.

The dashboard's /play tab serves export/web/. If that export is older than the
game source, the human plays a stale build and — reasonably — concludes their
changes were ignored. That happened, and it wasted a morning. So the build is
checked for staleness and rebuilt on demand: what you play is always what the
source says.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _godot() -> str | None:
    from bgate_adapters import godot
    try:
        return godot.find_godot()
    except Exception:
        return None


def _game(root: str | os.PathLike[str]) -> Path | None:
    from bgate_core import project
    return project.game_dir(root)


# Trees inside the game dir that a build does NOT depend on. Everything else
# does, including directories nobody thought of when this was written.
SKIP_DIRS = {".godot", ".bgate", ".bgate_out", ".git", ".import", "__pycache__",
             "export", "build", ".asset_work", "node_modules"}


def _newest_source(game_dir: Path) -> tuple[float, str]:
    """Newest mtime under the game dir, and WHICH file it was.

    THIS USED TO NAME THE THREE DIRECTORIES IT SCANNED — scripts, scenes,
    assets — and that allowlist quietly decided what a build was allowed to
    depend on. A project that keeps its levels in `data/*.json` (the ones this
    tool's own layout editor writes) had every one of those edits invisible
    here: change the level, the build reports CURRENT, you play the old one and
    conclude the tool ignored you. That is verbatim the morning this module was
    written to prevent, reintroduced by the scan instead of the rebuild.

    So it is a denylist now. The export ships the whole project; what is NOT a
    source is the short, knowable list above, and a directory nobody has
    imagined yet defaults to counting rather than to being ignored.

    Returning the path costs one variable and makes "stale" answerable: the UI
    can say which file is newer than the build instead of asserting it.
    """
    latest, newest = 0.0, ""
    for p in game_dir.rglob("*"):
        if SKIP_DIRS & set(p.relative_to(game_dir).parts):
            continue
        try:
            if not p.is_file():
                continue
            m = p.stat().st_mtime
        except OSError:          # vanished mid-walk; it cannot be the newest
            continue
        if m > latest:
            latest, newest = m, p.relative_to(game_dir).as_posix()
    return latest, newest


def status(root: str | os.PathLike[str]) -> dict:
    """Is there a build, and is it current with the source?"""
    game = _game(root)
    pck = Path(root) / "export" / "web" / "index.pck"
    if game is None:
        return {"built": False, "stale": True, "reason": "no game project"}
    if not pck.exists():
        return {"built": False, "stale": True, "reason": "never exported"}
    src, newest = _newest_source(game)
    built = pck.stat().st_mtime
    stale = built < src
    return {"built": True, "stale": stale,
            "build_mtime": built, "source_mtime": src,
            # What makes it stale. Without this the UI can only assert.
            "newest_source": newest if stale else "",
            "reason": f"{newest} is newer than the build" if stale else ""}


def _export_error(stderr: str) -> str:
    """Godot's export failure, as one line a human can act on.

    Its stderr is several lines of C++ source locations around one sentence
    that matters, and the sentence is usually a missing export template - which
    names its own fix. Pulling it out beats printing the whole block or
    replacing it with "export failed".
    """
    if not stderr:
        return ""
    if "No export template found" in stderr:
        return ("Godot has no Web export templates installed - open Godot and "
                "use Editor > Manage Export Templates, or download them for "
                "this exact Godot version")
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("ERROR:") and "at:" not in line:
            return line[len("ERROR:"):].strip()
    return ""


def rebuild(root: str | os.PathLike[str], timeout: int = 240) -> dict:
    """Export the Web build from current source. What /play serves next."""
    game = _game(root)
    if game is None:
        return {"ok": False, "error": "no game project at this root"}
    if not (game / "export_presets.cfg").exists():
        return {"ok": False, "error": "no export_presets.cfg — copy the Web "
                                      "preset the scaffold ships "
                                      "(templates/shared/export_presets.cfg) "
                                      "into the game dir, or add one in the "
                                      "editor under Project > Export"}
    godot = _godot()
    if not godot:
        return {"ok": False, "error": "Godot not found (set BGATE_GODOT)"}

    out = Path(root) / "export" / "web"
    out.mkdir(parents=True, exist_ok=True)
    # What the build was before, so "did this write anything" is answerable.
    pck_before = out / "index.pck"
    before = pck_before.stat().st_mtime if pck_before.exists() else 0.0
    try:
        proc = subprocess.run(
            [godot, "--headless", "--path", str(game),
             "--export-release", "Web", str(out / "index.html")],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"export timed out after {timeout}s"}

    pck = out / "index.pck"

    # WHETHER GODOT SUCCEEDED, ASKED RATHER THAN INFERRED FROM A FILE EXISTING.
    #
    # This checked only that a pck was THERE, and a failed export leaves the
    # PREVIOUS one exactly where it was - so a build that could not run
    # reported ok, handed back the old file's size, and the panel went on
    # saying the build was behind. Observed with a real cause: Godot 4.4.1 with
    # no web export templates installed exits 1 and writes nothing, and this
    # returned {"ok": true, "bytes": 45463700} for a 43 MB pck from five days
    # earlier. "The button does nothing" is exactly what that looks like.
    #
    # THE MTIME IS CHECKED TOO, because a non-zero exit is not the only way to
    # write nothing, and "did this produce a NEW build" is the actual question -
    # the caller is about to serve the result to a playtester.
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    detail = (stderr or stdout)[-600:]

    if proc.returncode != 0:
        # Godot's own words. Its export errors name the fix (a missing export
        # template names the template), and swallowing them for a tidy sentence
        # is how a fixable problem becomes a mystery.
        return {"ok": False, "returncode": proc.returncode,
                "error": _export_error(stderr) or
                         f"godot export failed (exit {proc.returncode})",
                "detail": detail}

    if not pck.exists():
        return {"ok": False, "error": "export produced no build",
                "detail": detail}

    if pck.stat().st_mtime <= before:
        return {"ok": False,
                "error": "godot reported success but wrote no new build - the "
                         "export at export/web is the one that was already "
                         "there",
                "detail": detail}

    return {"ok": True, "bytes": pck.stat().st_size,
            "wasm": (out / "index.wasm").stat().st_size
                    if (out / "index.wasm").exists() else 0}
