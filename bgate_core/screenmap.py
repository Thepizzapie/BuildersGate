"""Atlas — the auto-derived map of every screen and every asset it uses.

Why this exists: by the time a game has a handful of scenes, "what uses what"
lives in nobody's head — a select screen quietly shares portraits with the
fight HUD, backgrounds are loaded from script string-literals, and dead assets
pile up unnoticed. Every .tscn already DECLARES its edges (ext_resource), and
scripts declare the rest as res:// string literals — so the whole graph is
derivable in one scan, no hand-maintained manifest to rot.

Output contract (one dict, JSON-ready):
    screens  [{id, label, path, script_paths}]
    nodes    {id: {id, kind, label, path (root-relative), exists, preview}}
    edges    [{from, to, via}]          via: "scene" | "script" | "tres"
    orphans  [asset node ids]           on disk, referenced by nothing
    missing  [node ids]                 referenced, not on disk

Node kinds: screen | sprites (SpriteFrames .tres) | texture | audio | script
| font | shader | scene-res (non-screen packed scene) | other. The sprites
node chains to its sheet textures via 'tres' edges, so a sprite sheet swap
shows exactly which screens it reaches.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_EXT_RE = re.compile(
    r'\[ext_resource\s+type="(?P<type>[^"]+)"[^\]]*?path="(?P<path>res://[^"]+)"')
_RES_LIT_RE = re.compile(r'"(res://[^"]+?\.[A-Za-z0-9]{2,5})"')
# A path BUILT at runtime — "res://assets/props/derived/%s.png" % p["sprite"].
# These are not exotic: every data-driven loader in a real project reaches its
# art this way, and matching only exact literals reported the entire prop and
# gear library as dead. The template is resolved against what is actually on
# disk, so it can only ever mark files that exist.
_RES_TMPL_RE = re.compile(r'"(res://[^"]*%[sdfvx][^"]*\.[A-Za-z0-9]{2,5})"')

_KIND_BY_TYPE = {
    "Texture2D": "texture", "CompressedTexture2D": "texture",
    "SpriteFrames": "sprites", "AudioStream": "audio",
    "AudioStreamWAV": "audio", "AudioStreamOggVorbis": "audio",
    "Script": "script", "GDScript": "script", "PackedScene": "scene-res",
    "FontFile": "font", "Shader": "shader",
}
_KIND_BY_SUFFIX = {
    ".png": "texture", ".webp": "texture", ".jpg": "texture", ".svg": "texture",
    ".wav": "audio", ".ogg": "audio", ".mp3": "audio",
    ".gd": "script", ".tscn": "scene-res", ".tres": "sprites",
    ".ttf": "font", ".otf": "font", ".gdshader": "shader",
}
# Asset roots that count for orphan detection — engine plumbing (.godot,
# addons) and non-asset trees stay out of the picture.
_ORPHAN_SUFFIXES = (".png", ".webp", ".jpg", ".svg", ".wav", ".ogg", ".mp3",
                    ".tres")
# Data files whose res:// strings are real references. A manifest a script reads
# at runtime holds the same edges a preload would, and the game ships whatever
# it names.
_DATA_SUFFIXES = (".json", ".cfg", ".csv", ".txt")
# Trees whose contents are not part of the game. .bgate_out holds the tool's own
# scene/sheet backups — counting a backup .tscn as a screen would double every
# screen in the map the first time anything was wired.
_SKIP_DIRS = frozenset({".godot", ".bgate_out", ".bgate", ".git", ".asset_work",
                        "export", "build", "__pycache__"})


def _skip(p: Path) -> bool:
    return bool(_SKIP_DIRS & set(p.parts))


def _godot_dir(root: Path) -> Path | None:
    for cand in (root, root / "game"):
        if (cand / "project.godot").is_file():
            return cand
    hits = [p.parent for p in root.glob("*/project.godot")]
    return hits[0] if hits else None


def _kind_for(rtype: str, res_path: str) -> str:
    if rtype in _KIND_BY_TYPE:
        return _KIND_BY_TYPE[rtype]
    return _KIND_BY_SUFFIX.get(Path(res_path).suffix.lower(), "other")


def scan(root: str | os.PathLike[str]) -> dict:
    root = Path(root).resolve()
    gd = _godot_dir(root)
    if gd is None:
        return {"error": "no project.godot found at root or one level down"}

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    def res_to_disk(res_path: str) -> Path:
        return gd / res_path[len("res://"):]

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()
    templates: list[tuple[str, str]] = []      # (owner node id, template path)

    def add_node(res_path: str, kind: str) -> str:
        disk = res_to_disk(res_path)
        nid = res_path
        if nid not in nodes:
            is_img = disk.suffix.lower() in (".png", ".webp", ".jpg", ".svg")
            nodes[nid] = {
                "id": nid, "kind": kind, "label": disk.name,
                "path": rel(disk) if disk.exists() else res_path,
                "exists": disk.is_file(),
                "preview": rel(disk) if (is_img and disk.is_file()) else None,
            }
        return nid

    def add_edge(src: str, dst: str, via: str) -> None:
        key = (src, dst, via)
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append({"from": src, "to": dst, "via": via})

    # --- screens: every .tscn in the project (skip .godot cache) -------------
    screens = []
    tscns = [p for p in gd.rglob("*.tscn") if not _skip(p)]
    screen_ids = {f"res://{p.relative_to(gd).as_posix()}" for p in tscns}
    parsed_scripts: set = set()
    for tscn in sorted(tscns):
        sid = f"res://{tscn.relative_to(gd).as_posix()}"
        nodes[sid] = {"id": sid, "kind": "screen", "label": tscn.stem,
                      "path": rel(tscn), "exists": True, "preview": None}
        script_paths: list[str] = []
        text = tscn.read_text(encoding="utf-8", errors="replace")
        for m in _EXT_RE.finditer(text):
            res_path, rtype = m.group("path"), m.group("type")
            kind = _kind_for(rtype, res_path)
            if res_path in screen_ids:
                add_edge(sid, res_path, "scene")   # screen -> screen link
                continue
            nid = add_node(res_path, kind)
            add_edge(sid, nid, "scene")
            if kind == "script":
                script_paths.append(res_path)
        screens.append({"id": sid, "label": tscn.stem, "path": rel(tscn),
                        "script_paths": script_paths})

        # --- scripts: res:// string literals are dynamic loads ---------------
        for sp in script_paths:
            disk = res_to_disk(sp)
            if not disk.is_file():
                continue
            parsed_scripts.add(disk)
            source = disk.read_text(encoding="utf-8", errors="replace")
            templates += [(sid, t) for t in set(_RES_TMPL_RE.findall(source))]
            for lit in set(_RES_LIT_RE.findall(source)):
                if lit == sp or lit in screen_ids:
                    if lit in screen_ids and lit != sid:
                        add_edge(sid, lit, "script")
                    continue
                nid = add_node(lit, _kind_for("", lit))
                add_edge(sid, nid, "script")

    # --- every other script ---------------------------------------------------
    # Their res:// literals are real references (the arena list the fight swaps
    # backgrounds from, the sheets a marker preloads), and edges hang off the
    # script's own node.
    #
    # COVERED IS WHAT WAS PARSED, NOT WHAT IS A NODE. It used to be every script
    # NODE, and that dropped a whole class of file on the floor: a script
    # attached to a scene that is not one of the top-level SCREENS becomes a node
    # in the scene pass, but its literals are only read for screens — so it was
    # skipped here as "already covered" while nothing had actually read it.
    # Measured on a real project: 37 asset families were reported unused while a
    # script preloaded them, `unit_marker.gd` alone accounting for the whole
    # enemy sprite set.
    covered = {d for d in parsed_scripts}
    for gdfile in gd.rglob("*.gd"):
        if _skip(gdfile) or gdfile in covered:
            continue
        source = gdfile.read_text(encoding="utf-8", errors="replace")
        lits = set(_RES_LIT_RE.findall(source))
        tmpls = set(_RES_TMPL_RE.findall(source))
        if not lits and not tmpls:
            continue
        snid = add_node(f"res://{gdfile.relative_to(gd).as_posix()}", "script")
        templates += [(snid, t) for t in tmpls]
        for lit in lits:
            if lit == snid:
                continue
            if lit in screen_ids:
                add_edge(snid, lit, "script")
                continue
            add_edge(snid, add_node(lit, _kind_for("", lit)), "script")

    # --- project.godot: the engine holds assets too --------------------------
    # The icon, the audio bus layout, the autoloads and the main scene are
    # referenced by the PROJECT rather than by any scene, so a graph seeded only
    # from .tscn files reported them as dead art — and `icon.svg` showing up on
    # a delete-me list is how you learn the orphan list cannot be trusted.
    proj = gd / "project.godot"
    if proj.is_file():
        pid = "res://project.godot"
        nodes[pid] = {"id": pid, "kind": "screen", "label": "project.godot",
                      "path": rel(proj), "exists": True, "preview": None}
        source = proj.read_text(encoding="utf-8", errors="replace")
        # Autoloads are written "*res://scripts/audio.gd" — the star is Godot's
        # "enabled" marker, not part of the path.
        for lit in set(_RES_LIT_RE.findall(source.replace('"*res://', '"res://'))):
            if lit == pid:
                continue
            add_edge(pid, add_node(lit, _kind_for("", lit)), "project")

    # --- SpriteFrames chains: .tres -> its sheet textures --------------------
    for nid, node in list(nodes.items()):
        if node["kind"] != "sprites" or not node["exists"]:
            continue
        disk = res_to_disk(nid)
        for m in _EXT_RE.finditer(
                disk.read_text(encoding="utf-8", errors="replace")):
            tid = add_node(m.group("path"), _kind_for(m.group("type"),
                                                      m.group("path")))
            add_edge(nid, tid, "tres")

    # --- runtime-built paths: resolve the templates against disk -------------
    # A loader that reaches its art through "res://.../%s.png" references every
    # file the pattern can name. Resolving against what exists means this can
    # only mark real files, never invent one, and the edge is tagged 'template'
    # so a reader can tell a resolved pattern from a written literal.
    for owner, template in templates:
        tail = template[len("res://"):]
        if ".." in tail:
            continue
        pattern = re.compile(
            "^" + re.sub(r"%[sdfvx]", "[^/]+", re.escape(tail)
                         .replace(r"\%", "%")) + "$")
        parent = gd / Path(tail).parent
        if not parent.is_dir():
            continue
        for p in sorted(parent.iterdir()):
            if not p.is_file() or p.name.endswith(".import"):
                continue
            candidate = p.relative_to(gd).as_posix()
            if not pattern.match(candidate):
                continue
            nid = add_node(f"res://{candidate}", _kind_for("", candidate))
            add_edge(owner, nid, "template")

    # --- data files: a manifest the game loads is part of the graph ----------
    # THE OTHER HALF OF DATA-DRIVEN LOADING. Template paths were already
    # followed because "every data-driven loader in a real project reaches its
    # art this way" — but the commonest shape is not a %s in the source, it is a
    # JSON the script reads at runtime with the res:// paths INSIDE IT. Stopping
    # at the .json reported a whole VFX set as dead while the game shipped it:
    # vfx_manifest.gd -> vfx_manifest.json -> *_frames.tres -> *_sheet.png, and
    # the walk gave up at the arrow nothing parsed.
    #
    # Scoped to data files that are ALREADY NODES, i.e. something in the project
    # references them. Scanning every .json under the project would drag in tool
    # output and build manifests that the game never opens — a title manifest an
    # art run wrote but nothing has wired yet SHOULD read as unused.
    #
    # Runs after the template pass on purpose: a manifest is most often reached
    # BY a template ("res://data/items/%s/%s.json"), so scanning earlier would
    # find it is not a node yet and follow none of its references.
    for nid, node in list(nodes.items()):
        if not node.get("exists") or Path(nid).suffix.lower() not in _DATA_SUFFIXES:
            continue
        disk = res_to_disk(nid)
        try:
            source = disk.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        templates += [(nid, t) for t in set(_RES_TMPL_RE.findall(source))]
        for lit in set(_RES_LIT_RE.findall(source)):
            if lit == nid:
                continue
            add_edge(nid, add_node(lit, _kind_for("", lit)), "data")

    # --- orphans: assets on disk that nothing references ---------------------
    # "Derived variant" carve-out: paths built at runtime by string concat
    # (bg_market.png -> bg_market_f1.png animation frames) never appear as
    # literals, but a sibling named <referenced-stem>_<suffix> is clearly the
    # same asset family — link it to its base instead of calling it dead.
    referenced_disk = {str(res_to_disk(n)) for n in nodes}
    ref_stems = {(str(res_to_disk(n).parent), res_to_disk(n).stem): n
                 for n in list(nodes)}
    orphans = []
    assets_dir = gd / "assets"
    if assets_dir.is_dir():
        for p in assets_dir.rglob("*"):
            if not (p.suffix.lower() in _ORPHAN_SUFFIXES and p.is_file()
                    and not _skip(p)
                    and not p.name.endswith(".import")
                    and str(p) not in referenced_disk):
                continue
            rid = f"res://{p.relative_to(gd).as_posix()}"
            is_img = p.suffix.lower() in (".png", ".webp", ".jpg", ".svg")
            base = None
            stem = p.stem
            while "_" in stem and base is None:
                stem = stem.rsplit("_", 1)[0]
                base = ref_stems.get((str(p.parent), stem))
            node = {"id": rid, "kind": _KIND_BY_SUFFIX.get(
                        p.suffix.lower(), "other"),
                    "label": p.name, "path": rel(p), "exists": True,
                    "preview": rel(p) if is_img else None}
            if base is not None:
                nodes[rid] = {**node, "derived_from": base}
                add_edge(base, rid, "derived")
            else:
                nodes[rid] = {**node, "orphan": True}
                orphans.append(rid)

    missing = [nid for nid, n in nodes.items() if not n["exists"]]
    return {"godot_dir": rel(gd) or ".", "screens": screens, "nodes": nodes,
            "edges": edges, "orphans": sorted(orphans),
            "missing": sorted(missing)}
