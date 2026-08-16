"""Point Builders Gate at a game that already exists.

`bgate init` scaffolds — it unpacks a template into an EMPTY directory and
refuses (without force) to touch anything else. That is correct for a new game
and useless for the person this tool keeps meeting: someone who already has a
Godot project, months of work in it, and a question ("what's missing?") that
needs the tool to READ their game rather than replace it.

So adoption is defined by what it will not do. It never copies a template file,
never passes force to the scaffolder, and never rewrites a byte a user wrote.
The only things it puts on disk are additive:

  .bgate/game.db   a new directory, or an existing one it updates in place
  .gitignore       APPENDED to, inside a marked block (the block exists because
                   the README tells you to keep your API key in .env next to
                   the game; without the ignore rule, following the README
                   commits the key)
  CLAUDE.md        appended to, same marked-block trick

Both marked blocks make the whole operation idempotent: run adopt twice and the
second run finds its own marker and rewrites the block in place instead of
stacking a second copy.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from . import db, project
from .scaffold import TEMPLATES_DIR

# Where the appended blocks start and end. Anything between the two lines is
# ours to rewrite; anything outside them is the user's and is never touched.
# Two flavours because the marker has to be a COMMENT in the host file: a `#`
# line in a CLAUDE.md is an H1 heading, and an HTML comment in a .gitignore is
# a pattern that matches nothing but reads as garbage.
MARK_START = "# --- Builders Gate (managed block — edits here may be rewritten) ---"
MARK_END = "# --- end Builders Gate ---"
MD_MARK_START = "<!-- BEGIN builders-gate (managed block — edits here may be rewritten) -->"
MD_MARK_END = "<!-- END builders-gate -->"

# Directories that are never someone's source: skipping them keeps the size and
# file counts honest (a .godot import cache can outweigh the whole game).
SKIP_DIRS = {".git", ".godot", ".import", ".bgate", ".bgate_out", "__pycache__",
             "node_modules", ".venv", "venv", "build", "dist", ".vs", ".idea"}

# Nodes that only exist in one dimension. Presence in a .tscn is strong
# evidence; the counts decide, because a 3D game usually still has 2D UI.
_3D_MARKERS = re.compile(
    r'type="(Node3D|Spatial|Camera3D|MeshInstance3D|CharacterBody3D|'
    r'RigidBody3D|StaticBody3D|CSG\w+|DirectionalLight3D|WorldEnvironment)"')
_2D_MARKERS = re.compile(
    r'type="(Node2D|Camera2D|Sprite2D|Sprite|AnimatedSprite2D|CharacterBody2D|'
    r'RigidBody2D|StaticBody2D|TileMap|TileMapLayer|Polygon2D)"')


def _iter_files(base: Path):
    """Every file under base that is plausibly the user's own work."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def detect(directory: str | os.PathLike[str]) -> dict:
    """Read the directory and report what kind of project it looks like.

    Read-only, and tolerant: an unreadable or weirdly-encoded file is skipped
    rather than aborting the scan, because "we could not classify one scene" is
    not a reason to refuse to adopt someone's game.
    """
    base = Path(directory).expanduser().resolve()
    out: dict = {
        "path": str(base),
        "exists": base.is_dir(),
        "godot": False,
        "godot_dir": None,
        "godot_name": "",
        "godot_version": "",
        "main_scene": "",
        "dimension": "2d",
        "dimension_evidence": {"3d_nodes": 0, "2d_nodes": 0, "features": []},
        "scenes": 0,
        "scripts": 0,
        "shaders": 0,
        "images": 0,
        "audio": 0,
        "models": 0,
        "files": 0,
        "bytes": 0,
        "top_dirs": [],
        "biggest_scenes": [],
        "has_git": (base / ".git").exists(),
        "already_adopted": (base / db.DB_DIRNAME / db.DB_FILENAME).exists(),
    }
    if not out["exists"]:
        return out

    game_dir = project.game_dir(base)
    if game_dir is not None:
        out["godot"] = True
        out["godot_dir"] = str(game_dir)
        try:
            cfg = (game_dir / "project.godot").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            cfg = ""
        name = re.search(r'config/name\s*=\s*"([^"]*)"', cfg)
        if name:
            out["godot_name"] = name.group(1)
        main = re.search(r'run/main_scene\s*=\s*"([^"]*)"', cfg)
        if main:
            out["main_scene"] = main.group(1)
        feats = re.search(r'config/features\s*=\s*PackedStringArray\(([^)]*)\)', cfg)
        if feats:
            values = re.findall(r'"([^"]*)"', feats.group(1))
            out["dimension_evidence"]["features"] = values
            for value in values:
                if re.fullmatch(r"\d+\.\d+", value):
                    out["godot_version"] = value

    scene_sizes: list[tuple[int, str]] = []
    for path in _iter_files(base):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        out["files"] += 1
        out["bytes"] += size
        suffix = path.suffix.lower()
        if suffix == ".tscn":
            out["scenes"] += 1
            scene_sizes.append((size, str(path.relative_to(base)).replace("\\", "/")))
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            out["dimension_evidence"]["3d_nodes"] += len(_3D_MARKERS.findall(text))
            out["dimension_evidence"]["2d_nodes"] += len(_2D_MARKERS.findall(text))
        elif suffix in (".gd", ".cs"):
            out["scripts"] += 1
        elif suffix in (".gdshader", ".shader", ".tres") and "shader" in path.name:
            out["shaders"] += 1
        elif suffix in (".png", ".jpg", ".jpeg", ".webp", ".svg", ".bmp"):
            out["images"] += 1
        elif suffix in (".wav", ".ogg", ".mp3"):
            out["audio"] += 1
        elif suffix in (".glb", ".gltf", ".blend", ".fbx", ".obj"):
            out["models"] += 1

    out["biggest_scenes"] = [name for _, name in
                             sorted(scene_sizes, reverse=True)[:5]]
    out["top_dirs"] = sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and p.name not in SKIP_DIRS)[:20]

    three = out["dimension_evidence"]["3d_nodes"]
    two = out["dimension_evidence"]["2d_nodes"]
    # A 3D game with a 2D HUD is the norm, so "any 3D at all" is the signal, and
    # 2d+3d is reserved for a project where both are load-bearing. Getting this
    # wrong is cheap — it seeds a bible field the user can correct — but getting
    # it silently wrong is not, hence dimension_evidence travels with the answer.
    # 2d+3d needs both sides to be substantial in ABSOLUTE terms as well as in
    # ratio. Ratio alone called a 3D game with a two-node menu "2d+3d", which is
    # the wrong answer stated confidently — the shape of mistake this report
    # exists to avoid.
    if min(three, two) >= 10 and min(three, two) >= max(three, two) * 0.4:
        out["dimension"] = "2d+3d"
    elif three > two:
        out["dimension"] = "3d"
    elif "mobile" in out["dimension_evidence"]["features"] and not two:
        out["dimension"] = "2d"
    else:
        out["dimension"] = "2d"
    return out


def _merge_block(target: Path, body: str, start: str = MARK_START,
                 end: str = MARK_END) -> dict:
    """Put `body` in target inside the managed block. Never destroys content.

    Three cases, all additive: no file (write the block), file without our
    marker (append the block), file with our marker (replace between markers so
    a second run is a no-op rather than a second copy).
    """
    block = f"{start}\n{body.rstrip()}\n{end}\n"
    if not target.exists():
        target.write_text(block, encoding="utf-8")
        return {"path": str(target), "action": "created"}

    existing = target.read_text(encoding="utf-8", errors="replace")
    if start in existing and end in existing:
        head, _, rest = existing.partition(start)
        _, _, tail = rest.partition(end)
        updated = head + block + tail.lstrip("\n")
        if updated == existing:
            return {"path": str(target), "action": "unchanged"}
        target.write_text(updated, encoding="utf-8")
        return {"path": str(target), "action": "refreshed"}

    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    target.write_text(existing + sep + block, encoding="utf-8")
    return {"path": str(target), "action": "appended"}


def stamp_gitignore(directory: str | os.PathLike[str]) -> dict:
    """Merge the template's ignore rules into <dir>/.gitignore.

    MERGE, not copy. An adopted project very likely already has a .gitignore
    that someone tuned, and replacing it to protect their API key while dropping
    their own rules is not a trade anyone asked for.
    """
    source = TEMPLATES_DIR / "shared" / ".gitignore"
    body = source.read_text(encoding="utf-8") if source.is_file() else (
        ".env\n.env.*\n!.env.example\n.bgate/\n.bgate_out/\n.godot/\n")
    return _merge_block(Path(directory) / ".gitignore", body)


def stamp_claude_md(directory: str | os.PathLike[str], name: str = "") -> dict:
    """Merge the Builders Gate briefing into <dir>/CLAUDE.md.

    Same marked-block discipline as .gitignore, for the same reason: a project
    that already has a CLAUDE.md has one because someone wrote it.
    """
    source = TEMPLATES_DIR / "shared" / "CLAUDE.md"
    if not source.is_file():
        return {"path": "", "action": "skipped",
                "error": f"template missing: {source}"}
    body = source.read_text(encoding="utf-8")
    body = body.replace("__PROJECT_NAME__", name or Path(directory).name)
    return _merge_block(Path(directory) / "CLAUDE.md", body,
                        MD_MARK_START, MD_MARK_END)


def would_clobber(directory: str | os.PathLike[str]) -> list[str]:
    """Files adoption would have to destroy to proceed. Empty means safe.

    It is always empty today, and that is the point: this function is the
    assertion that the write set really is additive-only. If someone later adds
    a plain copy to adopt(), this is where it gets caught.
    """
    base = Path(directory)
    doomed = []
    db_file = base / db.DB_DIRNAME / db.DB_FILENAME
    if db_file.exists() and not db_file.is_file():
        doomed.append(str(db_file))
    for name in (".gitignore", "CLAUDE.md"):
        candidate = base / name
        if candidate.exists() and candidate.is_dir():
            doomed.append(str(candidate))
    return doomed


# ---------------------------------------------------------------------------
# The telemetry autoload — the difference between a recording and evidence
# ---------------------------------------------------------------------------
# WHY THIS IS NOT PART OF adopt(). scaffold overlays templates/shared/ onto
# every project it CREATES, so a scaffolded game gets addons/bgate and its
# autoloads for free. An ADOPTED game never did, and nothing else installed them
# - so a project that came in through `bgate adopt` could record playtests
# forever and never emit a single event. Observed exactly that way: a real
# project with 28 sessions, 59 pieces of feedback, zero rows in playtest_event,
# and a review screen that said "NO TELEMETRY - THIS SESSION WAS AUDIO ONLY"
# every time without ever naming the missing addon.
#
# The obvious fix - have adopt install it - was tried and REVERTED, because
# adopt's promise is that it writes .gitignore, CLAUDE.md and the database and
# leaves every existing file byte-identical. project.godot is the user's file.
# Two tests hold that line and they are right to: a tool that edits your engine
# config as a side effect of being pointed at your repo is exactly what makes
# people afraid to run it. So this is an EXPLICIT action instead, offered where
# the absence actually hurts.
#
# ADDITIVE AND RE-RUNNABLE. The script is
# copied only when absent or stale, and the autoload lines are merged into the
# existing [autoload] section rather than replacing it - an adopted game has its
# own autoloads and losing them would be the one unrecoverable thing adopt is
# built never to do.
_AUTOLOADS = (
    ("BGateTelemetry", "res://addons/bgate/bgate_telemetry.gd"),
    ("BGateTuner", "res://addons/bgate/bgate_tuner.gd"),
)


def _merge_autoloads(config: Path, entries=_AUTOLOADS) -> dict:
    """Add the BGate autoloads to project.godot, keeping every existing one.

    Godot's config is INI-shaped but not INI: values are GDScript literals and
    configparser mangles them. So this is a line-level edit, which is also what
    keeps somebody's hand-written comments and ordering intact.
    """
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"action": "skipped", "why": f"cannot read project.godot: {exc}"}

    missing = [(name, path) for name, path in entries
               if f"\n{name}=" not in "\n" + text]
    if not missing:
        return {"action": "unchanged", "path": str(config)}

    lines = text.splitlines()
    added = [f'{name}="*{path}"' for name, path in missing]

    if "[autoload]" in text:
        at = lines.index("[autoload]")
        # After the section header and any blank line under it, so the file
        # keeps the shape Godot itself writes.
        insert = at + 1
        while insert < len(lines) and not lines[insert].strip():
            insert += 1
        lines[insert:insert] = added
    else:
        # A project with no autoloads at all: append a section rather than
        # guessing where one belongs among the others.
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[autoload]", ""] + added

    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"action": "merged", "path": str(config),
            "added": [name for name, _ in missing]}


def telemetry_status(directory: str | os.PathLike[str]) -> dict:
    """Is the BGate telemetry autoload registered in this game?

    Asked of project.godot rather than of the addon folder: a script sitting in
    addons/ that nothing autoloads emits exactly as much as no script at all,
    and that is the failure this reports.
    """
    base = Path(directory).expanduser().resolve()
    game = project.game_dir(base)
    config = (game / "project.godot") if game else None
    if config is None or not config.is_file():
        return {"ok": True, "reason": "", "installable": False,
                "why": "no Godot project here, so there is nothing to instrument"}
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": True, "reason": "", "installable": False,
                "why": f"cannot read project.godot ({exc})"}
    if "BGateTelemetry" in text:
        return {"ok": True, "reason": "", "installable": False,
                "path": str(config)}
    return {
        "ok": False,
        "installable": True,
        "path": str(config),
        "reason": ("the game has no BGate telemetry autoload, so a recording "
                   "captures picture and sound but no game events"),
        "fix": "install the addon (one file and one autoload line)",
    }


def install_telemetry(directory: str | os.PathLike[str]) -> dict:
    """Put the BGate addon in the game and register its autoloads.

    Returns {action, ...}. Never raises: a project that cannot take the addon
    (no game dir, a read-only tree) must still adopt.
    """
    base = Path(directory).expanduser().resolve()
    game = project.game_dir(base)
    if game is None or not (game / "project.godot").is_file():
        return {"action": "skipped", "why": "no Godot project to install into"}

    source = TEMPLATES_DIR / "shared" / "addons" / "bgate"
    if not source.is_dir():
        return {"action": "skipped", "why": "the addon is missing from this build"}

    dest = game / "addons" / "bgate"
    copied: list[str] = []
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for script in sorted(source.glob("*.gd")):
            target = dest / script.name
            # OVERWRITE ONLY WHAT WE SHIP, and only when it differs. The addon
            # is ours; a project that edited it gets the edit replaced on a
            # re-adopt, which is the same deal as every other stamped file.
            new = script.read_bytes()
            if not target.exists() or target.read_bytes() != new:
                target.write_bytes(new)
                copied.append(script.name)
    except OSError as exc:
        return {"action": "failed", "why": f"could not write the addon: {exc}"}

    merged = _merge_autoloads(game / "project.godot")
    return {"action": "installed" if (copied or merged.get("action") == "merged")
                      else "unchanged",
            "path": str(dest), "scripts": copied, "autoload": merged}


def adopt(directory: str | os.PathLike[str], name: str = "", pitch: str = "",
          dimension: Optional[str] = None, engine: str = "godot") -> dict:
    """Adopt an existing project. Additive only, and safe to re-run.

    Returns the detection report alongside what was written, because the first
    thing an adopting user needs is proof the tool understood their game.
    """
    base = Path(directory).expanduser().resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"{base} is not a directory — nothing to adopt")

    blocked = would_clobber(base)
    if blocked:
        raise FileExistsError(
            "refusing to adopt: these paths are in the way and adoption will "
            f"not destroy them: {', '.join(blocked)}")

    found = detect(base)
    was_adopted = found["already_adopted"]

    if not name:
        name = found["godot_name"] or base.name
    if dimension is None:
        dimension = found["dimension"]
    if engine == "godot" and not found["godot"]:
        # No project.godot is not a refusal — plenty of people adopt the repo
        # root before the engine files land, or use another engine entirely —
        # but recording engine=godot for a directory with no Godot in it makes
        # every later godot_* tool fail confusingly.
        engine = "none"

    # Preserve what a previous adopt/init already recorded: re-running adopt to
    # refresh detection must not silently wipe a pitch the user wrote later.
    if was_adopted and not pitch:
        try:
            pitch = project.get(base).get("pitch", "")
        except Exception:
            pitch = ""

    record = project.init(base, name, pitch=pitch, engine=engine,
                          dimension=dimension)
    written = {
        "gitignore": stamp_gitignore(base),
        "claude_md": stamp_claude_md(base, name),
    }
    # LANES, POINTED AT THE REPO THAT IS ACTUALLY HERE. The default seat table
    # is written against <root>/game and <root>/design; an adopted repo has
    # whatever layout its author chose, and against an ordinary one
    # (src/, assets/, scenes/) NO seat owns anything. With the hook installed
    # that means every dispatched agent is refused on contact with the source
    # tree, and the refusal reads as "wrong seat" rather than "wrong layout".
    # adopt already knew the layout — it computed top_dirs and threw it away.
    try:
        from . import seats as _seats
        lanes = _seats.apply_layout(base)
    except Exception as exc:                 # never fail an adopt over lanes
        lanes = {"changed": False, "why": f"could not set lanes: {exc}"}
    # ITS OWN KEY, not a `written` entry: that map is one row per stamped FILE
    # and every consumer reads action/path off it. A differently-shaped row in
    # there is a KeyError in the CLI printer, which is where this landed first.
    project.set_active(base)

    return {
        "ok": True,
        "path": str(base),
        "adopted": True,
        "already_adopted": was_adopted,
        "project": record,
        "detected": found,
        "written": written,
        "lanes": lanes,
        "next": [
            "bgate doctor — check the toolchain (godot, blender, ...)",
            "bgate hook-install . — make the lane rules bite (they are "
            "advisory until you do)",
            "bgate serve — the dashboard, on this project",
            "read CLAUDE.md, then fill in the bible with bible_add",
        ],
    }
