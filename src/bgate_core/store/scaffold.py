"""Stamp out a Godot project wired for playtesting.

Templates are real, runnable slices — not empty shells. Each ships a player whose
"feel" tunables (gravity, fall_multiplier, coyote_time) are exported AND emitted
as telemetry, so the very first playtest already produces the join that makes
"the jump feels floaty" actionable.

The shared/ tree (the BGate autoload) is overlaid onto every template, so there
is one copy of the telemetry code rather than one per dimension.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import project
from .util import slugify

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
KINDS = ("2d", "3d")

_NAME_TOKEN = "__PROJECT_NAME__"

# Rewritten rather than copied, because they carry the project name token.
_TEXT_SUFFIXES = (".godot", ".tscn", ".gd", ".cfg", ".svg", ".md")


def _rendered(item: Path, name: str) -> str:
    return item.read_text(encoding="utf-8").replace(_NAME_TOKEN, name)


def _already_ours(out: Path, item: Path, name: str) -> bool:
    """True when the file on disk already IS what this template would write.

    Text files are compared through read_text so the CRLF that write_text puts
    down on Windows does not read back as "the user changed this" and turn every
    re-run into a wall of skips.
    """
    try:
        if item.suffix in _TEXT_SUFFIXES:
            return out.read_text(encoding="utf-8") == _rendered(item, name)
        return out.read_bytes() == item.read_bytes()
    except (OSError, UnicodeDecodeError):
        return False  # unreadable, or not even the encoding we write: not ours


def _backup(out: Path) -> Path:
    """Copy out to <name>.bak, never onto an existing backup.

    A second replace run that reused the same .bak would destroy the rescue copy
    taken by the first one — the exact loss the backup exists to prevent.
    """
    bak = out.with_name(out.name + ".bak")
    n = 1
    while bak.exists():
        bak = out.with_name(f"{out.name}.bak.{n}")
        n += 1
    shutil.copy2(out, bak)
    return bak


def list_templates() -> list[dict]:
    out = []
    for kind in KINDS:
        path = TEMPLATES_DIR / kind
        out.append({
            "kind": kind,
            "available": path.is_dir(),
            "path": str(path),
            "description": {
                "2d": "Side-on platformer slice: player, ground, ledge, jump/land "
                      "telemetry, feel tunables exported.",
                "3d": "First-person slice: capsule player, ground, block, jump/land "
                      "telemetry, feel tunables exported.",
            }[kind],
        })
    return out


def new_project(dest: str | os.PathLike[str], name: str, kind: str = "2d",
                force: bool = False, replace: bool = False) -> dict:
    """Create a Godot project at dest from the given template.

    Refuses to write into a non-empty directory unless force — a scaffolder that
    quietly overwrites someone's work is a data-loss bug wearing a feature's hat.

    force used to be that bug. It meant "write every template file over whatever
    is there", so someone reaching for it to top up a missing addon lost their
    project.godot, their player.gd and their export_presets.cfg in place, with
    no backup and no mention of it in the result. export_presets.cfg is the
    unforgiving one: the .gitignore this same template stamps excludes it, so
    the customised export targets were not in git either.

    So force now means FILL IN WHAT IS MISSING. A file that already matches what
    we would write is left alone; a file that differs is the user's and is
    skipped, not overwritten. replace=True is the separate, explicit "yes, put
    the template back" — and even that copies each victim to <name>.bak first.
    Both the skips and the replacements come back in the result so the caller
    can say what happened instead of the user finding out in a diff.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")

    template = TEMPLATES_DIR / kind
    shared = TEMPLATES_DIR / "shared"
    if not template.is_dir():
        raise FileNotFoundError(f"template not found: {template}")

    target = Path(dest)
    project.refuse_harness(target, "scaffold a game")
    if target.exists() and any(target.iterdir()) and not (force or replace):
        raise FileExistsError(
            f"{target} is not empty — pass force=True to scaffold into it anyway"
        )
    target.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    created: list[str] = []
    unchanged: list[str] = []
    replaced: list[dict] = []
    skipped: list[dict] = []

    for source in (template, shared):
        if not source.is_dir():
            continue
        for item in source.rglob("*"):
            if item.is_dir():
                continue
            rel = item.relative_to(source)
            out = target / rel
            name_rel = str(rel).replace("\\", "/")

            backup: Path | None = None
            if out.exists():
                if _already_ours(out, item, name):
                    unchanged.append(name_rel)
                    continue
                if not replace:
                    skipped.append({
                        "file": name_rel,
                        # NAMES BOTH SPELLINGS, because this string is read in
                        # two places that cannot use each other's: a terminal
                        # user gets it from `bgate init` and needs the flag, an
                        # agent gets it from godot_scaffold and needs the
                        # argument. Naming only the Python kwarg told someone
                        # standing at a shell to pass something they cannot.
                        "reason": "differs from the template — kept your version; "
                                  "overwrite it with `bgate init --replace` (or "
                                  "replace=True from the API). A .bak is taken "
                                  "first",
                    })
                    continue
                backup = _backup(out)

            out.parent.mkdir(parents=True, exist_ok=True)
            # .md is in here for templates/shared/CLAUDE.md, which greets the
            # user's Claude session by the project's real name — a briefing that
            # says __PROJECT_NAME__ reads as a broken tool on first contact.
            if item.suffix in _TEXT_SUFFIXES:
                out.write_text(_rendered(item, name), encoding="utf-8")
            else:
                shutil.copy2(item, out)

            written.append(name_rel)
            if backup is None:
                created.append(name_rel)
            else:
                replaced.append({"file": name_rel, "backup": str(backup)})

    # THE ROOT ABOVE THE GODOT PROJECT GETS ONE TOO, AND IT IS THE ONE THAT
    # MATTERS. This template stamps a well-written .gitignore into the Godot
    # project directory, including the line about `.bgate/` holding the
    # dashboard's auth token — and `.bgate/` does not live in the Godot project.
    # It lives in the Builders Gate root ABOVE it, which was stamped with
    # nothing.
    #
    # REPRODUCED: `git init` at that root, `git add -A`, and .bgate/game.db and
    # its WAL landed in the first commit along with the token. `bgate init` and
    # `bgate adopt` both stamp the root correctly; the hole was specific to
    # scaffolding, which is the documented path for a NEW game.
    #
    # Merged into whatever is there, never replacing it, and best-effort: a
    # scaffold that succeeded must not be reported as failed because an ignore
    # file could not be merged.
    root_ignore = None
    try:
        from . import adopt as _adopt, db as _db

        bgate_root = _db.resolve_root(target) or (
            target.parent if (target.parent / ".bgate").is_dir() else None)
        if bgate_root and Path(bgate_root).resolve() != target.resolve():
            root_ignore = _adopt.stamp_gitignore(bgate_root)
    except Exception as exc:
        root_ignore = {"action": "failed", "error": f"{type(exc).__name__}: {exc}"}

    result = {
        "ok": True,
        "path": str(target),
        "kind": kind,
        "name": name,
        "root_gitignore": root_ignore,
        "slug": slugify(name),
        # files stays "what we wrote", the shape every caller already reads.
        "files": sorted(written),
        "created": sorted(created),
        "unchanged": sorted(unchanged),
        "replaced": sorted(replaced, key=lambda r: r["file"]),
        "skipped": sorted(skipped, key=lambda s: s["file"]),
        "next": [
            "godot_check_project to import and validate it",
            "playtest_start, then launch the game with BGATE_TELEMETRY set",
            "BGateTelemetry.emit_event(kind, data) from your own code",
        ],
    }
    # A caller that only prints result["files"] would otherwise report "0 files"
    # on a run that deliberately left the user's work alone, which reads as a
    # no-op rather than as a decision. Give it one line it can print verbatim.
    if skipped:
        result["note"] = (
            f"{len(skipped)} file(s) already in the project differ from the "
            f"template and were left alone: "
            + ", ".join(s["file"] for s in result["skipped"])
        )
    elif replaced:
        result["note"] = (
            f"{len(replaced)} file(s) were replaced; the previous contents are "
            f"beside them as .bak: "
            + ", ".join(r["file"] for r in result["replaced"])
        )
    return result
