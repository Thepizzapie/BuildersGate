"""Canon - which world, scene, script and asset is CURRENT, and which is retired.

WHY THIS EXISTS. Over one day on Meridian, forty board items were dispatched
into a repository that still carried its first street (`scenes/main.tscn`, the
`tools/build_*` generators, `east_expansion.gd` and its districts) beside the
island that replaced it (`scenes/fresh_island.tscn`). Nothing said which was
which. Agents read the old street's probes as the pattern to copy, extended
the old map's district modules, and wired cast into `main.tscn` - measured
from their logs: 58 references to `main.tscn` in one item, 88 to the retired
`coastal_front.gd` in another. The artifact registry versions every delivered
file, but an agent does not read the registry; it reads the tree, and the
tree had two of everything.

THE ANSWER IS A LIST, NOT A GUESS. The canon is a small workspace document
owned by the director seat:

    world     the scene the game IS (defaults to project.godot's main scene)
    entries   logical name -> the current path (`reporter`, `hollis_farm`)
    retired   path patterns that are no longer the game, each with the path
              that replaced it and why

It reaches agents three ways, and the third one has teeth:

  1. `block()` is printed into every dispatched brief and the SessionStart
     context, so the first thing a seat reads is which world it is in.
  2. `canon_status` / `canon_audit` are MCP tools; the audit names every
     scene or script that still references a retired path, every duplicate
     scene set (the same basename under two directories), and every scene
     the world does not reach.
  3. The PreToolUse hook refuses a SEATED agent's read or write of a retired
     path and names the successor. A human's own session is not gated - a
     retired file is still yours to open.

Retiring is a DECISION, so `retire()` is refused to seated workers at the MCP
layer (see server.canon_retire); the director and the human do it.
"""
from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path
from typing import Optional

from ..store import workspace as _ws

SEAT = "director"
KEY = "canon"
#: Retired patterns named per reason in a brief before the rest is counted.
BLOCK_ROWS = 8

_MAIN_SCENE_RE = re.compile(r'^run/main_scene\s*=\s*"([^"]*)"', re.MULTILINE)
# ext_resource lines in a .tscn, and the res:// strings a script loads.
_EXT_RES_RE = re.compile(r'\[ext_resource[^\]]*\bpath="(res://[^"]+)"')
_RES_STR_RE = re.compile(r'"(res://[^"]+\.(?:tscn|scn|gd|glb|gltf|png|tres|res))"')
# `"res://scenes/vehicles/" + id + ".tscn"`: a directory prefix a script
# builds paths from. Everything under it counts as reached - the audit
# reads text, not control flow, and a dynamic load is still a load.
_RES_DIR_RE = re.compile(r'"(res://[^"]+/)"')
_CLASS_NAME_RE = re.compile(r"^\s*class_name\s+([A-Za-z_]\w*)", re.MULTILINE)
_SCENE_EXT = (".tscn", ".scn")


def get(root: str | os.PathLike[str]) -> dict:
    doc = _ws.get(root, SEAT, KEY, {})
    doc.setdefault("world", "")
    doc.setdefault("entries", {})
    doc.setdefault("retired", [])
    return doc


def _save(root, doc: dict) -> dict:
    return _ws.set(root, SEAT, KEY, doc)


def normalise(path: str) -> str:
    """One spelling for a project-relative path: forward slashes, no `res://`,
    no leading `./`. A retired pattern and a judged target must compare in
    the same spelling or the gate answers about a different file."""
    text = str(path or "").strip().replace("\\", "/")
    if text.startswith("res://"):
        text = text[len("res://"):]
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def main_scene(project_dir: str | os.PathLike[str]) -> str:
    """`application/run/main_scene` from project.godot, normalised, or ''."""
    try:
        text = (Path(project_dir) / "project.godot").read_text(encoding="utf-8")
    except OSError:
        return ""
    found = _MAIN_SCENE_RE.search(text)
    return normalise(found.group(1)) if found else ""


_AUTOLOAD_RE = re.compile(r'^\w+\s*=\s*"\*?(res://[^"]+)"', re.MULTILINE)


def autoloads(project_dir: str | os.PathLike[str]) -> list[str]:
    """The [autoload] scripts and scenes - roots of the game as much as the
    main scene is: Meridian's whole story layer hangs off one."""
    try:
        text = (Path(project_dir) / "project.godot").read_text(encoding="utf-8")
    except OSError:
        return []
    section = text.split("[autoload]", 1)
    if len(section) < 2:
        return []
    body = section[1].split("\n[", 1)[0]
    return [normalise(m) for m in _AUTOLOAD_RE.findall(body)]


def world(root, project_dir: str | os.PathLike[str] = "") -> str:
    """The current world: the explicit canon entry, else the main scene."""
    explicit = normalise(get(root).get("world") or "")
    if explicit:
        return explicit
    return main_scene(project_dir) if project_dir else ""


def set_world(root, path: str) -> dict:
    doc = get(root)
    doc["world"] = normalise(path)
    return _save(root, doc)


def set_entry(root, name: str, path: str, kind: str = "", note: str = "") -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("an entry needs a name")
    doc = get(root)
    doc["entries"][name] = {"path": normalise(path), "kind": str(kind or ""),
                            "note": str(note or ""), "at": _now()}
    return _save(root, doc)


def drop_entry(root, name: str) -> dict:
    doc = get(root)
    doc["entries"].pop(str(name or "").strip(), None)
    return _save(root, doc)


def retire(root, pattern: str, successor: str = "", reason: str = "") -> dict:
    """Mark a path (or glob) as no longer the game. `successor` is what to
    use instead and is what the hook's refusal names."""
    pattern = normalise(pattern)
    if not pattern:
        raise ValueError("a retired pattern must name a path")
    doc = get(root)
    doc["retired"] = [r for r in doc["retired"] if r.get("pattern") != pattern]
    doc["retired"].append({"pattern": pattern, "successor": normalise(successor),
                           "reason": str(reason or ""), "at": _now()})
    return _save(root, doc)


def unretire(root, pattern: str) -> dict:
    pattern = normalise(pattern)
    doc = get(root)
    doc["retired"] = [r for r in doc["retired"] if r.get("pattern") != pattern]
    return _save(root, doc)


def retired_match(root, rel_path: str) -> Optional[dict]:
    """The retired entry covering this project-relative path, if any.

    A pattern matches the path itself, any file under it when it names a
    directory, and glob syntax (`scripts/districts/**`, `tools/build_*`).
    """
    rel = normalise(rel_path)
    if not rel:
        return None
    for row in get(root).get("retired", []):
        pat = normalise(row.get("pattern", ""))
        if not pat:
            continue
        if rel == pat or rel.startswith(pat + "/"):
            return row
        if fnmatch.fnmatchcase(rel, pat):
            return row
        if pat.endswith("/**") and rel.startswith(pat[:-3] + "/"):
            return row
    return None


def block(root, project_dir: str | os.PathLike[str] = "") -> str:
    """The lines a seat reads first: which world, which files, which not."""
    doc = get(root)
    current = world(root, project_dir)
    if not current and not doc["entries"] and not doc["retired"]:
        return ""
    lines = ["CANON - what is current in this project:"]
    if current:
        lines.append(f"  WORLD    {current}  (the scene the game is; probes, "
                     "shots and integration target THIS scene)")
    for name, row in sorted(doc["entries"].items()):
        note = f"  - {row['note']}" if row.get("note") else ""
        kind = f" [{row['kind']}]" if row.get("kind") else ""
        lines.append(f"  {name:<10} {row.get('path', '')}{kind}{note}")
    if doc["retired"]:
        lines.append("  RETIRED  - not the game any more. Do not read them as "
                     "the pattern to copy, do not extend them, do not wire "
                     "into them; the hook refuses a seat's reads and writes "
                     "there:")
        # One line per REASON, not per file: a hundred probes of the old
        # street are one fact. The brief states the rule and the successor;
        # canon_status holds the roster and the hook knows every pattern.
        groups: dict[tuple[str, str], list[str]] = {}
        for row in doc["retired"]:
            key = (row.get("successor", ""), row.get("reason", ""))
            groups.setdefault(key, []).append(row["pattern"])
        for (succ, why), patterns in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            patterns = sorted(patterns)
            head = ", ".join(patterns[:BLOCK_ROWS])
            more = (f", +{len(patterns) - BLOCK_ROWS} more" if len(patterns) > BLOCK_ROWS else "")
            lines.append(f"    {head}{more}")
            tail = (f"      ->  use {succ}" if succ else "") + (f"  ({why})" if why else "")
            if tail:
                lines.append(tail)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AUDIT - the tree read against the list.
# ---------------------------------------------------------------------------
def _project_files(project_dir: Path, exts: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for path in project_dir.rglob("*"):
        if path.suffix.lower() in exts and ".godot" not in path.parts \
                and not any(part.startswith(".") for part in path.parts):
            out.append(path)
    return out


def _references(path: Path) -> tuple[list[str], list[str]]:
    """(files, directory prefixes) a scene or script names, normalised."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    found = _EXT_RES_RE.findall(text) if path.suffix in _SCENE_EXT else []
    found += _RES_STR_RE.findall(text)
    prefixes = [normalise(p) for p in _RES_DIR_RE.findall(text)]
    return sorted({normalise(f) for f in found}), sorted({p for p in prefixes if p})


def audit(root, project_dir: str | os.PathLike[str]) -> dict:
    """What the tree still says that the canon does not.

    references_to_retired  scene/script -> the retired paths it names
    duplicate_scenes       one basename living under several directories
    unreachable_scenes     scenes the world does not reach (through
                           ext_resource/preload chains) and no tool loads
    """
    project = Path(project_dir)
    scenes = [p for p in _project_files(project, _SCENE_EXT)
              if not p.name.endswith("_preview.tscn")]
    scripts = _project_files(project, (".gd",))
    rel = lambda p: normalise(str(p.relative_to(project)))  # noqa: E731

    graph: dict[str, list[str]] = {}
    prefixes: dict[str, list[str]] = {}
    texts: dict[str, str] = {}
    for path in scenes + scripts:
        graph[rel(path)], prefixes[rel(path)] = _references(path)
        try:
            texts[rel(path)] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            texts[rel(path)] = ""
    # `class_name Foo` makes a script reachable by NAME, not by path: a
    # `TwoBoneIK3D.new()` in a live script loads two_bone_ik_3d.gd with no
    # res:// string anywhere. Every mention of a declared class in a reached
    # file reaches the script that declares it.
    classes: dict[str, str] = {}
    for src, text in texts.items():
        if src.endswith(".gd"):
            found = _CLASS_NAME_RE.search(text)
            if found:
                classes[found.group(1)] = src
    class_re = (re.compile(r"\b(" + "|".join(re.escape(c) for c in sorted(classes)) + r")\b")
                if classes else None)
    for src, text in texts.items():
        if class_re is None:
            break
        for name in set(class_re.findall(text)):
            if classes[name] != src:
                graph[src].append(classes[name])

    references_to_retired: dict[str, list[str]] = {}
    for src, refs in graph.items():
        if retired_match(root, src):
            continue          # a retired file naming retired files is fine
        hits = [r for r in refs if retired_match(root, r)]
        if hits:
            references_to_retired[src] = hits

    by_name: dict[str, list[str]] = {}
    for path in scenes:
        by_name.setdefault(path.name, []).append(rel(path))
    duplicate_scenes = {name: sorted(paths) for name, paths in by_name.items()
                        if len(paths) > 1}

    start = world(root, project)
    reached: set[str] = set()
    reached_dirs: set[str] = set()
    frontier = ([start] if start else []) + autoloads(project)
    while frontier:
        node = frontier.pop()
        if node in reached:
            continue
        reached.add(node)
        frontier.extend(graph.get(node, []))
        reached_dirs.update(prefixes.get(node, []))
    # Anything a tool loads counts as reachable-on-purpose: probes and views
    # are how the team looks at parts of the game in isolation.
    tool_loaded: set[str] = set()
    for src, refs in graph.items():
        if src.startswith("tools/"):
            tool_loaded.update(refs)
            reached_dirs.update(prefixes.get(src, []))

    def _under_reached_dir(path: str) -> bool:
        return any(path.startswith(d + "/") for d in reached_dirs)

    unreachable = sorted(
        rel(p) for p in scenes
        if rel(p) not in reached and rel(p) not in tool_loaded
        and not _under_reached_dir(rel(p))
        and not rel(p).startswith("tools/") and not retired_match(root, rel(p)))

    # Scripts the game never loads either - the old generators live here.
    unreachable_scripts = sorted(
        rel(p) for p in scripts
        if rel(p) not in reached and rel(p) not in tool_loaded
        and not _under_reached_dir(rel(p))
        and not rel(p).startswith("tools/") and not retired_match(root, rel(p)))

    return {
        "world": start,
        "roots": ([start] if start else []) + autoloads(project),
        "references_to_retired": references_to_retired,
        "duplicate_scenes": duplicate_scenes,
        "unreachable_scenes": unreachable,
        "unreachable_scripts": unreachable_scripts,
        "scenes": len(scenes), "scripts": len(scripts),
        "retired_patterns": [r["pattern"] for r in get(root)["retired"]],
    }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
