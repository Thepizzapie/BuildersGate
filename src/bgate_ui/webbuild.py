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
    from bgate_core.store import project
    return project.game_dir(root)


# Trees inside the game dir that a build does NOT depend on. Everything else
# does, including directories nobody thought of when this was written.
SKIP_DIRS = {".godot", ".bgate", ".bgate_out", ".git", ".import", "__pycache__",
             "export", "build", ".asset_work", "node_modules", "dist"}


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
        parts = p.relative_to(game_dir).parts
        if SKIP_DIRS & set(parts) or parts[-1].startswith(".bgate"):
            continue             # the harness's own pid/log/lock files
        try:
            if not p.is_file():
                continue
            m = p.stat().st_mtime
        except OSError:          # vanished mid-walk; it cannot be the newest
            continue
        if m > latest:
            latest, newest = m, p.relative_to(game_dir).as_posix()
    return latest, newest


def _web_game(root: str | os.PathLike[str]) -> Path | None:
    """The vite + TypeScript game, when this project is a web one."""
    try:
        from bgate_core.runtime import enginetests as _et
        return _et._web_dir(root)
    except Exception:                                             # noqa: BLE001
        return None


def play_dir(root: str | os.PathLike[str]) -> Path:
    """Where /play/ serves from: Godot's export/web, or for a web project the
    vite build under .bgate_out/ (already gitignored, so building to play
    never dirties the tree and stops the board)."""
    if _game(root) is None and _web_game(root) is not None:
        return Path(root) / ".bgate_out" / "play"
    return Path(root) / "export" / "web"


def _web_status(root, game: Path) -> dict:
    page = play_dir(root) / "index.html"
    if not page.exists():
        return {"built": False, "stale": True, "engine": "web", "reason": "never built"}
    src, newest = _newest_source(game)
    built = page.stat().st_mtime
    stale = built < src
    return {"built": True, "stale": stale, "engine": "web",
            "build_mtime": built, "source_mtime": src,
            "newest_source": newest if stale else "",
            "reason": f"{newest} is newer than the build" if stale else "",
            "blocked": ""}


def _web_rebuild(root, game: Path, timeout: int) -> dict:
    """`npm run build` into export/web/, the directory /play/ serves.

    THE PLAY TAB WAS GODOT-ONLY: a web project's playtest showed "no web
    build - export it first" and Rebuild answered "no game project at this
    root" (dungeon-weaver, 2026-10-03). The project's own build script runs
    (tsc then vite), with vite pointed at export/web and a RELATIVE base, so
    the page's assets resolve under /play/ instead of the dashboard root.
    It builds into play_dir(), not export/web, which a web project's
    .gitignore does not cover."""
    from bgate_adapters import web as _web

    if not _web.installed(game):
        return {"ok": False, "engine": "web",
                "error": "node_modules is not there: run `npm install` in the game first"}
    out = play_dir(root).resolve()
    got = _web._npm_run(str(game), "build", timeout,
                        extra=["--", "--base=./", f"--outDir={out}", "--emptyOutDir"])
    ok = bool(got.get("ok")) and (out / "index.html").exists()
    return {"ok": ok, "engine": "web", "output": str(out),
            "seconds": got.get("seconds"),
            "error": "" if ok else str(got.get("error") or "the build wrote no index.html"),
            "stdout": str(got.get("stdout") or "")[-4000:],
            "stderr": str(got.get("stderr") or "")[-4000:]}


def status(root: str | os.PathLike[str]) -> dict:
    """Is there a build, is it current with the source, and CAN one be made?

    The third question is new and it is the one the panel could not answer. A
    screen that says "build is behind" beside a rebuild button, on a machine
    where no export can succeed, sends somebody to press a button that was
    never going to work - which is exactly what happened here. `blocked` says so
    before the press, in the words that name the fix.
    """
    game = _game(root)
    pck = Path(root) / "export" / "web" / "index.pck"
    if game is None:
        web = _web_game(root)
        if web is not None:
            return _web_status(root, web)
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
            "reason": f"{newest} is newer than the build" if stale else "",
            # Cheap: the version probe behind this is cached on the binary's
            # mtime, so a panel asking on every open pays one stat().
            "blocked": _templates_blocked() if stale else ""}


def _templates_blocked() -> str:
    """Why this Godot cannot export to Web, or "" if it can.

    Guarded: a probe that will not run must not stop a build that might. The
    export's own error handling is the backstop for everything this cannot
    foresee.
    """
    try:
        from bgate_adapters import godot as _g
        probe = _g.export_templates("web")
    except Exception:
        return ""
    if probe.get("available"):
        return ""
    return str(probe.get("reason") or "web export templates are not installed")


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
    """Export the Web build from current source. What /play serves next.

    THE PRESENTATION GATE BINDS HERE, and it has to bind HERE rather than in a
    panel or a checklist. Night Shift's presentation gate was blocked and the
    build shipped anyway, which is only possible when the gate is something a
    caller consults instead of something the export runs. `--export-release` is
    the one line in this product that turns a project into a build, so that is
    where the refusal lives.

    It bites ONLY at the release stage (greenlight). Every playtest build
    during development exports exactly as before — the gate is about a release
    candidate, not about iteration, and a gate that slowed iteration would be
    switched off inside a week.
    """
    game = _game(root)
    web = _web_game(root) if game is None else None
    if game is None and web is None:
        return {"ok": False, "error": "no game project at this root"}
    try:
        from bgate_core.design import greenlight as _greenlight

        _greenlight.release_guard(root)
    except ImportError:
        pass
    except Exception as exc:                                      # noqa: BLE001
        # StageRefused, or a check that would not run - which greenlight
        # already counts as a failure rather than a skip. Either way this
        # build does not happen, and the reason is the whole message.
        return {"ok": False, "error": str(exc), "refused": "presentation"}
    if web is not None:
        return _web_rebuild(root, web, timeout)
    if not (game / "export_presets.cfg").exists():
        return {"ok": False, "error": "no export_presets.cfg — copy the Web "
                                      "preset the scaffold ships "
                                      "(templates/shared/export_presets.cfg) "
                                      "into the game dir, or add one in the "
                                      "editor under Project > Export"}
    godot = _godot()
    if not godot:
        return {"ok": False, "error": "Godot not found (set BGATE_GODOT)"}

    # ASK BEFORE SPENDING FIVE SECONDS FAILING, and ask the thing that already
    # knows. godot.export_templates() is the same probe `bgate doctor` reports
    # under godot_web_templates, and it answers better than an export's stderr
    # can: it names the version Godot IS and the version the installed templates
    # are FOR, which is a thirty-second fix, where the engine's own message is a
    # path to a zip that does not exist.
    #
    # This was the whole bug from the outside. The export path never asked, so a
    # machine with a perfectly good Godot and mismatched templates ran the
    # export, failed, and (before the exit code was checked) reported success.
    blocked = _templates_blocked()
    if blocked:
        return {"ok": False, "error": blocked, "templates": True}

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
