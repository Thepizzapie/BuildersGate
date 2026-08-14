"""What the tech seat can be told about a project WITHOUT running the engine.

`godot_check_project` answers one question — does it still import — and it costs
a full headless import to answer. The two things the seat is actually judged on
between checks are cheaper than that and were reported by nothing:

* the SCENE CONVENTION. "One editable thing = one named node" is a rule agents
  are held to and no tool measured, so a generator that pasted twelve copies of
  a subtree instead of instancing one looked identical to a hand-built floor
  until somebody opened the editor.
* the GENERATOR CONTRACT. "A tool that rewrites project data ships --check and
  defaults to dry" is the rule that keeps a bake from silently clobbering hand
  placement. Which scripts actually honour it was a thing you found out by
  reading them one at a time.

Both are STATIC READS. Nothing here imports, runs, or guesses: every count comes
off the text of a .tscn or the source of a .py, and a script whose argparse this
cannot see is reported as unknown rather than as compliant. A number that cannot
be derived is not returned at all — the panel above this would rather show
nothing than show a plausible figure.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from bgate_core import scenewire

# Trees that are not the game: the engine's import cache, this tool's backups
# and its scene undo history, build output, dependencies. Scanning
# .bgate_out/scene_backups would report every historical copy of floor.tscn as
# a live convention breach.
_SKIP = {".godot", ".bgate", ".bgate_out", ".git", ".asset_work", "__pycache__",
         "export", "build", "dist", "node_modules", ".venv", "tmp"}

# Godot names a node it created after its type, with a counter for siblings:
# Sprite2D, Sprite2D2, Node3D17. A node still wearing that name was never named
# by a person, which is the whole of "one editable thing = one NAMED node".
_DEFAULT_NAME = re.compile(r"^(?P<stem>[A-Za-z][A-Za-z0-9]*?)(?P<n>\d*)$")

# A container whose only job is to hold children. Empty, it is either a marker
# or something a script fills at run time — and the second kind cannot be
# edited, reviewed or diffed, which is why the rule exists.
_CONTAINERS = {"Node", "Node2D", "Node3D", "Spatial", "CanvasLayer", "Control"}

_TILE_TYPES = {"TileMap", "TileMapLayer"}


def _walk(root: Path, suffix: str):
    """Every file with `suffix` under `root`, skipping the trees above."""
    for path in root.rglob(f"*{suffix}"):
        if any(part in _SKIP for part in path.parts):
            continue
        yield path


def _stem(name: str) -> str:
    m = _DEFAULT_NAME.match(name)
    return m.group("stem") if m else name


# ---------------------------------------------------------------------------
# Scene convention
# ---------------------------------------------------------------------------
def scene_convention(root: Path | str) -> dict:
    """Audit every scene in the project against the four structural rules.

    Returns the rules with their counts and, for each, the first few real
    offenders — a bare "12" is a number nobody can act on, and the point of the
    panel is to name the scene you have to open.
    """
    root = Path(root)
    scenes = 0
    nodes = 0
    unnamed: list[str] = []
    tile_objects: list[str] = []
    pasted: list[str] = []
    empty_containers: list[str] = []
    unreadable: list[str] = []

    for path in _walk(root, ".tscn"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            parsed = scenewire.parse(text)
        except (OSError, scenewire.WireError):
            # A .tscn this cannot parse is reported, not skipped: a scene the
            # audit could not read is exactly the scene most likely to be broken.
            unreadable.append(_rel(root, path))
            continue
        scenes += 1
        rel = _rel(root, path)
        by_parent: dict[str, list[dict]] = {}
        for node in parsed["nodes"]:
            nodes += 1
            parent = node.get("parent")
            if parent is not None:
                by_parent.setdefault(parent, []).append(node)

        for node in parsed["nodes"]:
            name, ntype = node["name"], node.get("type", "")
            path_in_scene = scenewire.node_path(node)
            if ntype and _stem(name) == ntype:
                unnamed.append(f"{rel} · {path_in_scene}")
            children = by_parent.get(path_in_scene, [])
            if ntype in _TILE_TYPES and children:
                tile_objects.append(f"{rel} · {path_in_scene} holds "
                                    f"{len(children)} child node"
                                    f"{'' if len(children) == 1 else 's'}")
            if ntype in _CONTAINERS and not children and node.get("parent") is not None:
                empty_containers.append(f"{rel} · {path_in_scene}")

        # A duplicated subtree looks like Cabinet, Cabinet2, Cabinet3 … under one
        # parent with no instance= on any of them. Two is a pair; three is a
        # pattern somebody should have made a scene out of.
        for parent, siblings in by_parent.items():
            groups: dict[str, list[dict]] = {}
            for node in siblings:
                if not node.get("instance"):
                    groups.setdefault(_stem(node["name"]), []).append(node)
            for stem, group in groups.items():
                if len(group) >= 3:
                    pasted.append(f"{rel} · {stem} ×{len(group)} under "
                                  f"{parent if parent != '.' else 'the root'}")

    rules = [
        _rule("One editable thing = one named node",
              f"{nodes} nodes across {scenes} scenes — "
              f"{len(unnamed)} still carry the name the engine gave them",
              unnamed, bad=True),
        _rule("TileMapLayer is terrain only",
              "a terrain layer holding objects is a layer nothing can select",
              tile_objects, bad=True),
        _rule("Instance, don't duplicate",
              "three or more siblings off one name stem, none of them instanced",
              pasted, bad=True),
        _rule("No empty container a script fills at run time",
              "a container with no children in the file is populated somewhere "
              "you cannot diff",
              empty_containers, bad=False),
    ]
    return {"scenes": scenes, "nodes": nodes, "rules": rules,
            "unreadable": unreadable}


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _rule(rule: str, detail: str, hits: list[str], *, bad: bool) -> dict:
    return {
        "rule": rule,
        "detail": detail,
        "count": len(hits),
        # "ok" and "0" are the same number, and the reference draws the passing
        # row differently on purpose: a clean rule is a result, not an absence.
        "tone": "good" if not hits else ("bad" if bad else "warn"),
        "examples": hits[:6],
        "more": max(0, len(hits) - 6),
    }


# ---------------------------------------------------------------------------
# Generator contract
# ---------------------------------------------------------------------------
# The two shapes of gate, and they mean opposite things. An --apply/--write flag
# means the script does nothing until told to, so its DEFAULT is dry. A
# --dry-run flag means the opposite: writing is what happens if you say nothing.
_APPLY_FLAGS = {"--apply", "--write", "--commit", "--execute", "--force"}
_DRY_FLAGS = {"--dry", "--dry-run", "--dryrun", "--no-write", "--plan"}
_CHECK_FLAGS = {"--check", "--verify"}

# Calls that put bytes on disk. `open(...)` is deliberately absent — the mode is
# what matters and it is checked separately.
_WRITE_CALLS = ("write_text", "write_bytes", "os.replace", "shutil.copy",
                "shutil.move", "shutil.copy2", "savefig", ".save(")

# Only the trees the tech seat's write globs name. A repo-wide .py scan would
# report every analysis notebook and every one-off measurement script as a
# generator, and the panel's whole claim is that these are the tools that
# rewrite PROJECT DATA.
_GENERATOR_DIRS = ("scripts", "tools")

# Scenes, resources and scripts only. project.godot and the .import sidecars are
# left out on purpose: every script that locates the project mentions
# "project.godot", and counting that as authored project data listed half the
# measurement scripts in the repo as generators.
_PROJECT_DATA = (".tscn", ".tres", ".gd")


def _argparse_flags(tree: ast.AST) -> set[str]:
    """Every long flag handed to an add_argument call in the file.

    Read out of the AST rather than by grepping the source, so a flag named in a
    comment or a docstring cannot make a script look compliant.
    """
    flags: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "add_argument"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.startswith("--"):
                    flags.add(arg.value)
    return flags


def generator_inventory(root: Path | str) -> dict:
    """Which scripts rewrite project data, and whether they ask first.

    A script is listed only if it BOTH writes to disk and names a project-data
    suffix — a measurement script that writes a .png report is not a generator
    and does not owe anyone a --check.
    """
    root = Path(root)
    rows: list[dict] = []
    scanned = 0
    for name in _GENERATOR_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(_walk(base, ".py")):
            try:
                src = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            writes = any(call in src for call in _WRITE_CALLS) or \
                re.search(r"open\([^)]*['\"][wa]b?['\"]", src) is not None
            touches = [s for s in _PROJECT_DATA if s in src]
            if not (writes and touches):
                continue
            try:
                flags = _argparse_flags(ast.parse(src))
                parsed = True
            except SyntaxError:
                flags, parsed = set(), False
            rows.append(_generator_row(_rel(root, path), flags, touches, parsed))
    return {"scanned": scanned, "dirs": list(_GENERATOR_DIRS), "rows": rows}


def _generator_row(rel: str, flags: set[str], touches: list[str],
                   parsed: bool) -> dict:
    if not parsed:
        # An unparseable file is UNKNOWN, never "compliant". The failure this
        # panel exists to catch is a tool that writes without asking, and
        # scoring a file nobody could read as a pass is that failure with a
        # green tag on it.
        return {"path": rel, "check": "unreadable", "check_ok": False,
                "dry": "unreadable", "dry_ok": False, "writes": touches,
                "note": "the file does not parse as Python, so its flags "
                        "cannot be read — treat as ungated"}
    has_check = bool(flags & _CHECK_FLAGS)
    apply_flag = sorted(flags & _APPLY_FLAGS)
    dry_flag = sorted(flags & _DRY_FLAGS)
    if apply_flag:
        dry, dry_ok = "dry", True
        why = f"writes only under {apply_flag[0]}"
    elif dry_flag:
        dry, dry_ok = "writes", False
        why = f"{dry_flag[0]} exists but writing is what happens by default"
    else:
        dry, dry_ok = "writes", False
        why = "no flag gates the write"
    return {
        "path": rel,
        "check": "--check" if has_check else "no --check",
        "check_ok": has_check,
        "dry": dry,
        "dry_ok": dry_ok,
        "writes": touches,
        "note": f"{why} · touches {', '.join(touches)}",
    }
