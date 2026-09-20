"""Designed rooms for 2D top-down games: build one from a plan, audit any one
for the faults a screenshot cannot show.

WHY THIS EXISTS. level_generate lays out a BSP dungeon. A JRPG town, a
scalehouse yard, a datacenter floor with three keyed cages is not a random
partition - it is a DESIGNED room: this wall here, the vendor there, the
entrance on the south edge. On the benchmark game every one of those was
written by a python baker the gameplay agent wrote for itself, and the
harness could not see inside it. Two of the results: an NPC walled in by a
barrier and a party spawned under a weighbridge - both invisible in the
screenshot the seat judged by eye, both found by QA a day later.

So: `build` takes an ASCII plan, the tileset's own manifest (the sidecar
tileset_generate / tileset_describe writes) and a list of props and markers
in CELL coordinates, and writes the scene - refusing, BEFORE any file is
written, a prop that leaves the map or a marker inside a wall. `audit` reads
any scene back - including ones no bgate tool wrote - and answers the space
questions: is every marker on the map, is any inside a solid, can the
player reach every one of them from the spawn, do two props overlap. Pure
Python over the .tscn text; no engine.
"""
from __future__ import annotations

import json
import os
import random
import re
from collections import deque
from pathlib import Path
from typing import Optional

from . import scenewire as _sw
from . import tilemap as _tm

#: What a plan character means when the caller does not say.
DEFAULT_LEGEND = {"#": "wall", ".": "floor", " ": "void"}
#: Share of floor / wall cells drawn with a variant tile, seeded.
VARIANT_RATE = 0.15
#: Marker kinds, inferred from a node name's prefix when not stated.
KIND_PREFIXES = (
    ("spawn", "spawn"), ("player", "spawn"), ("start", "spawn"),
    ("npc", "npc"), ("entrance", "entrance"), ("door", "entrance"),
    ("exit", "exit"), ("sign", "sign"), ("chest", "chest"),
    ("trigger", "trigger"), ("boss", "trigger"), ("stairs", "exit"),
)
#: Kinds the player must be able to walk up to.
REACHABLE_KINDS = ("npc", "entrance", "exit", "sign", "chest", "trigger")
#: Nodes that are the running game's own furniture, never a room fault.
IGNORE_NAMES = ("hud", "dialoguebox", "dialogue_box", "camera", "music",
                "party", "fieldhud", "canvaslayer")
#: Cells beyond which a scene is refused as "not a room".
MAX_CELLS = 250_000


class RoomError(ValueError):
    """A plan or a scene that cannot be trusted; refused, never repaired."""


# ---------------------------------------------------------------------------
# The tileset's own description
# ---------------------------------------------------------------------------

def manifest_for(root: Path, tileset_res: str) -> Optional[dict]:
    """The `<name>.tiles.json` beside a TileSet, or None."""
    rel = tileset_res.replace("res://", "")
    disk = Path(root) / rel
    side = disk.with_name(disk.stem + ".tiles.json")
    if not side.is_file():
        return None
    try:
        meta = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("kind") != "bgate-tileset":
        return None
    return meta


def _cells_of(meta: dict, section: str) -> list[tuple[int, int]]:
    part = meta.get(section) or {}
    out: list[tuple[int, int]] = []
    if section == "floor":
        table = part.get("table") or {}
        out += [tuple(int(v) for v in c) for c in table.values()]
        if part.get("solid"):
            out.append(tuple(int(v) for v in part["solid"]))
    else:
        if part.get("atlas"):
            out.append(tuple(int(v) for v in part["atlas"]))
        table = part.get("table") or {}
        out += [tuple(int(v) for v in c) for c in table.values()]
    out += [tuple(int(v) for v in c) for c in (part.get("variants") or [])]
    seen: list[tuple[int, int]] = []
    for c in out:
        if c not in seen:
            seen.append(c)
    return seen


def _props_of(meta: dict) -> dict[str, dict]:
    out = {}
    for name, spec in (meta.get("props") or {}).items():
        if not isinstance(spec, dict) or not spec.get("atlas"):
            continue
        fp = spec.get("footprint") or [1, 1]
        out[str(name)] = {"source": int(spec.get("source", 0)),
                          "atlas": (int(spec["atlas"][0]), int(spec["atlas"][1])),
                          "footprint": (max(1, int(fp[0])), max(1, int(fp[1]))),
                          "blocking": bool(spec.get("blocking", True))}
    return out


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _kind_of(name: str, scene: str = "", stated: str = "") -> str:
    if stated:
        return str(stated).lower()
    low = (name or "").lower()
    for prefix, kind in KIND_PREFIXES:
        if low.startswith(prefix):
            return kind
    stem = Path(scene.replace("res://", "")).stem.lower() if scene else ""
    for prefix, kind in KIND_PREFIXES:
        if stem.startswith(prefix) or stem.endswith(prefix):
            return kind
    if "entrance" in stem:
        return "entrance"
    return "other"


def parse_plan(plan: list[str] | str, legend: Optional[dict] = None) -> dict:
    """Rows of characters into cell sets. Ragged rows are padded with void.

    Returns {width, height, floor: {(x,y): variant_or_None}, wall: set,
    void: set, props_by_char: {(x,y): type}}.
    """
    rows = plan.splitlines() if isinstance(plan, str) else [str(r) for r in plan]
    rows = [r.rstrip("\n") for r in rows]
    while rows and not rows[-1].strip():
        rows.pop()
    if not rows:
        raise RoomError("the plan is empty")
    width = max(len(r) for r in rows)
    height = len(rows)
    if width * height > MAX_CELLS:
        raise RoomError(f"{width}x{height} cells is past the {MAX_CELLS} cap")
    key = dict(DEFAULT_LEGEND)
    for ch, what in (legend or {}).items():
        key[str(ch)] = str(what)
    floor: dict[tuple[int, int], Optional[int]] = {}
    wall: set[tuple[int, int]] = set()
    void: set[tuple[int, int]] = set()
    props_by_char: dict[tuple[int, int], str] = {}
    unknown: set[str] = set()
    for y, row in enumerate(rows):
        for x in range(width):
            ch = row[x] if x < len(row) else " "
            what = key.get(ch)
            if what is None:
                unknown.add(ch)
                continue
            if what == "wall":
                wall.add((x, y))
            elif what == "void":
                void.add((x, y))
            elif what == "floor":
                floor[(x, y)] = None
            elif what.startswith("floor:"):
                floor[(x, y)] = int(what.split(":", 1)[1])
            elif what.startswith("prop:"):
                floor[(x, y)] = None
                props_by_char[(x, y)] = what.split(":", 1)[1]
            else:
                unknown.add(ch)
    if unknown:
        raise RoomError(
            f"the plan uses {sorted(unknown)} and the legend does not say what "
            "they are - legend takes wall | floor | void | floor:<variant> | "
            "prop:<type>")
    return {"width": width, "height": height, "floor": floor, "wall": wall,
            "void": void, "props_by_char": props_by_char}


def _footprint_cells(at: tuple[int, int], fp: tuple[int, int]) -> list[tuple[int, int]]:
    return [(at[0] + dx, at[1] + dy) for dy in range(fp[1]) for dx in range(fp[0])]


def plan_room(meta: dict, plan: list[str] | str, *, legend: Optional[dict] = None,
              props: Optional[list[dict]] = None, markers: Optional[list[dict]] = None,
              seed: int = 0, tile_px: int = 0) -> dict:
    """Everything build() will write, checked, and nothing written.

    Refuses: a prop type the manifest does not have, a prop leaving the map
    or standing on a wall or another prop, a marker off the map or inside a
    solid. These are the faults that shipped; they are cheaper here than in a
    screenshot review a day later.
    """
    grid = parse_plan(plan, legend)
    tile_px = int(tile_px or meta.get("tile_px") or 32)
    rng = random.Random(int(seed))
    floor_cells = _cells_of(meta, "floor")
    wall_cells = _cells_of(meta, "wall")
    if not floor_cells:
        raise RoomError("the tileset manifest names no floor tile")
    floor_source = int((meta.get("floor") or {}).get("source", 0))
    wall_source = int((meta.get("wall") or {}).get("source", floor_source))
    floor_solid = floor_cells[0]
    floor_variants = floor_cells[1:]
    wall_solid = wall_cells[0] if wall_cells else None
    wall_variants = wall_cells[1:]
    catalogue = _props_of(meta)

    problems: list[str] = []
    layers: dict[str, list[dict]] = {"Floor": [], "Walls": [], "Props": []}
    for (x, y), variant in sorted(grid["floor"].items(), key=lambda kv: (kv[0][1], kv[0][0])):
        ax, ay = floor_solid
        if variant is not None and floor_variants:
            ax, ay = floor_variants[(variant - 1) % len(floor_variants)] if variant > 0 else floor_solid
        elif floor_variants and rng.random() < VARIANT_RATE:
            ax, ay = rng.choice(floor_variants)
        layers["Floor"].append({"x": x, "y": y, "source": floor_source, "ax": ax, "ay": ay})
    if grid["wall"] and wall_solid is None:
        problems.append("the plan has walls and the tileset manifest names no wall tile")
    for (x, y) in sorted(grid["wall"], key=lambda c: (c[1], c[0])):
        if wall_solid is None:
            break
        ax, ay = wall_solid
        if wall_variants and rng.random() < VARIANT_RATE:
            ax, ay = rng.choice(wall_variants)
        layers["Walls"].append({"x": x, "y": y, "source": wall_source, "ax": ax, "ay": ay})

    solid: set[tuple[int, int]] = set(grid["wall"])
    occupied: dict[tuple[int, int], str] = {}
    placed: list[dict] = []
    wanted = [{"type": t, "at": list(at)} for at, t in grid["props_by_char"].items()]
    wanted += [dict(p) for p in (props or [])]
    for p in wanted:
        ptype = str(p.get("type") or "")
        spec = catalogue.get(ptype)
        if spec is None:
            problems.append(f"prop {ptype!r} is not in the tileset manifest "
                            f"(it has {sorted(catalogue) or 'no props'})")
            continue
        try:
            at = (int(p["at"][0]), int(p["at"][1]))
        except (KeyError, TypeError, ValueError, IndexError):
            problems.append(f"prop {ptype!r} needs at=[x, y] in cells")
            continue
        cells = _footprint_cells(at, spec["footprint"])
        bad = [c for c in cells if c not in grid["floor"]]
        if bad:
            problems.append(f"prop {ptype!r} at {list(at)} ({spec['footprint'][0]}x"
                            f"{spec['footprint'][1]}) is not entirely on floor: {bad[:4]}")
            continue
        clash = [c for c in cells if c in occupied]
        if clash:
            problems.append(f"prop {ptype!r} at {list(at)} overlaps "
                            f"{occupied[clash[0]]!r} at {list(clash[0])}")
            continue
        for c in cells:
            occupied[c] = ptype
            if spec["blocking"]:
                solid.add(c)
        layers["Props"].append({"x": at[0], "y": at[1], "source": spec["source"],
                                "ax": spec["atlas"][0], "ay": spec["atlas"][1]})
        placed.append({"type": ptype, "at": list(at), "footprint": list(spec["footprint"]),
                       "blocking": spec["blocking"]})

    out_markers: list[dict] = []
    names: set[str] = set()
    for m in markers or []:
        name = str(m.get("name") or "").strip()
        if not name:
            problems.append(f"a marker needs a name: {m}")
            continue
        if name in names:
            problems.append(f"two markers are named {name!r}")
            continue
        names.add(name)
        scene = str(m.get("scene") or "")
        kind = _kind_of(name, scene, str(m.get("kind") or ""))
        if "pos" in m:
            px, py = float(m["pos"][0]), float(m["pos"][1])
            cell = (int(px // tile_px), int(py // tile_px))
        elif "at" in m:
            cell = (int(m["at"][0]), int(m["at"][1]))
            px = cell[0] * tile_px + tile_px / 2
            py = cell[1] * tile_px + tile_px / 2
        else:
            problems.append(f"marker {name!r} needs at=[x, y] in cells or pos=[px, py]")
            continue
        if not (0 <= cell[0] < grid["width"] and 0 <= cell[1] < grid["height"]) \
                or cell in grid["void"]:
            problems.append(f"marker {name!r} at cell {list(cell)} is off the map")
            continue
        if cell in solid:
            what = occupied.get(cell, "a wall")
            problems.append(f"marker {name!r} at cell {list(cell)} is inside "
                            f"{what if what == 'a wall' else 'prop ' + repr(what)}")
            continue
        out_markers.append({"name": name, "kind": kind, "scene": scene,
                            "cell": list(cell), "pos": [px, py],
                            "props": dict(m.get("props") or {})})

    if not any(mk["kind"] == "spawn" for mk in out_markers):
        problems.append("no spawn marker (a marker whose name starts with Spawn, "
                        "or kind='spawn') - the audit cannot prove reachability "
                        "without one, and neither can the game")

    return {"ok": not problems, "problems": problems, "tile_px": tile_px,
            "width": grid["width"], "height": grid["height"],
            "layers": layers, "props": placed, "markers": out_markers,
            "solid": sorted(solid), "floor": sorted(grid["floor"]),
            "void": sorted(grid["void"])}


_HEADER = '[gd_scene format=3]\n\n[node name="{root}" type="Node2D"]\n'


def build(root: str | os.PathLike[str], godot_project: str | os.PathLike[str],
          scene_res: str, tileset_res: str, plan: list[str] | str, *,
          legend: Optional[dict] = None, props: Optional[list[dict]] = None,
          markers: Optional[list[dict]] = None, seed: int = 0,
          root_name: str = "", root_script: str = "",
          extra: Optional[list[dict]] = None, overwrite: bool = False) -> dict:
    """Write the room. Refuses on any plan problem before touching disk."""
    proj = Path(godot_project)
    meta = manifest_for(proj, tileset_res)
    if meta is None:
        raise RoomError(
            f"{tileset_res} has no .tiles.json sidecar; tileset_generate writes "
            "one, tileset_describe writes one for a hand-built set")
    planned = plan_room(meta, plan, legend=legend, props=props, markers=markers,
                        seed=seed)
    if not planned["ok"]:
        return {"ok": False, "written": False, "problems": planned["problems"],
                "ascii": render(planned)}
    rel = scene_res.replace("res://", "")
    disk = proj / rel
    if disk.exists() and not overwrite:
        return {"ok": False, "written": False,
                "problems": [f"{scene_res} exists; pass overwrite=True to replace "
                             "it (a backup is kept)"]}
    name = root_name or _sw.sanitize_node_name(Path(rel).stem.title().replace("_", "")) or "Room"
    text = _HEADER.format(root=name)
    if root_script:
        text = _sw.attach_script(text, root_script, node=".")["text"]
    layers = [{"name": lname, "cells": cells}
              for lname, cells in planned["layers"].items() if cells]
    text = _sw.wire_tilemap(text, tileset_res, layers, parent=".",
                            owns=list(planned["layers"]))["text"]
    for mk in planned["markers"]:
        if mk["scene"]:
            got = _sw.wire(text, mk["scene"], node_name=mk["name"], parent=".",
                           dimension="2d")
            text, node = got["text"], got["node"]
        else:
            got = _sw.add_node(text, name=mk["name"], node_type="Marker2D", parent=".")
            text, node = got["text"], got["node"]
        text = _sw.set_property(text, node, "position",
                                f"Vector2({mk['pos'][0]:g}, {mk['pos'][1]:g})")["text"]
        for key, value in mk["props"].items():
            text = _sw.set_property(text, node, key, value)["text"]
    for ex in extra or []:
        scene = str(ex.get("scene") or "")
        if not scene:
            continue
        got = _sw.wire(text, scene, node_name=str(ex.get("name") or ""), parent=".",
                       dimension="2d")
        text = got["text"]
        for key, value in (ex.get("props") or {}).items():
            text = _sw.set_property(text, got["node"], key, value)["text"]
    backup = None
    disk.parent.mkdir(parents=True, exist_ok=True)
    if disk.exists():
        backup = disk.with_suffix(disk.suffix + ".bak")
        backup.write_text(disk.read_text(encoding="utf-8"), encoding="utf-8")
    disk.write_text(text, encoding="utf-8")
    result = audit(root, godot_project, scene_res, tileset_res=tileset_res)
    return {"ok": True, "written": True, "scene": scene_res,
            "backup": str(backup) if backup else None,
            "cells": {k: len(v) for k, v in planned["layers"].items()},
            "props": planned["props"], "markers": planned["markers"],
            "audit": result}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

_VEC2_RE = re.compile(r"Vector2i?\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)")
_PACKED_B64_RE = re.compile(r'PackedByteArray\("([^"]*)"\)')
_PACKED_INTS_RE = re.compile(r"PackedByteArray\(([\d,\s]*)\)")
_SUB_RECT_RE = re.compile(
    r'\[sub_resource type="RectangleShape2D" id="?([^"\]\s]+)"?\]\s*\n(?:[^\[]*?)size\s*=\s*Vector2\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)',
    re.S)


def _packed_cells(raw: str) -> list[dict]:
    m = _PACKED_B64_RE.search(raw)
    if m:
        return _tm.decode_cells(m.group(1))
    m = _PACKED_INTS_RE.search(raw)
    if not m:
        return []
    ints = [int(v) for v in m.group(1).replace("\n", " ").split(",") if v.strip()]
    import base64
    return _tm.decode_cells(base64.b64encode(bytes(ints)).decode("ascii"))


def _vec(raw: Optional[str]) -> Optional[tuple[float, float]]:
    if not raw:
        return None
    m = _VEC2_RE.search(raw)
    return (float(m.group(1)), float(m.group(2))) if m else None


def audit(root: str | os.PathLike[str], godot_project: str | os.PathLike[str],
          scene_res: str, *, tileset_res: str = "", spawn: str = "") -> dict:
    """The space questions, answered from the scene text.

    Solids come from three places, because the benchmark's rooms used all
    three: a TileMapLayer whose cells the tileset manifest calls wall or a
    blocking prop (or any layer NAMED like a wall layer); a node carrying a
    `footprint = Vector2i(w, h)` (the hand-baked prop convention); and a
    RectangleShape2D under a StaticBody2D. Markers are every instanced node
    and Marker2D with a position. Reachability is a flood fill from the
    spawn over floor cells that are not solid; a marker is reached when its
    own cell or a 4-neighbour is in the spawn's region.
    """
    proj = Path(godot_project)
    rel = scene_res.replace("res://", "")
    disk = proj / rel
    if not disk.is_file():
        raise RoomError(f"no scene at {scene_res}")
    text = disk.read_text(encoding="utf-8")
    parsed = _sw.parse(text)
    ext = {e["id"]: e["path"] for e in parsed["ext"]}

    tile_px = 0
    floor: set[tuple[int, int]] = set()
    solid: dict[tuple[int, int], str] = {}
    layers_seen: list[dict] = []
    manifests: dict[str, Optional[dict]] = {}

    def _manifest(ts_res: str) -> Optional[dict]:
        if ts_res not in manifests:
            manifests[ts_res] = manifest_for(proj, ts_res)
        return manifests[ts_res]

    for node in parsed["nodes"]:
        if node["type"] != "TileMapLayer":
            continue
        props = _sw.properties(text, parsed, node)
        cells = _packed_cells(props.get("tile_map_data", "") or "")
        ts_res = ""
        m = re.search(r'ExtResource\("([^"]+)"\)', props.get("tile_set", "") or "")
        if m:
            ts_res = ext.get(m.group(1), "")
        meta = _manifest(ts_res or tileset_res) if (ts_res or tileset_res) else None
        if meta and not tile_px:
            tile_px = int(meta.get("tile_px") or 0)
        lname = node["name"].lower()
        is_wall_layer = "wall" in lname or "solid" in lname or "collision" in lname
        wall_cells = set(_cells_of(meta, "wall")) if meta else set()
        floor_cells = set(_cells_of(meta, "floor")) if meta else set()
        catalogue = _props_of(meta) if meta else {}
        by_atlas = {v["atlas"]: (k, v) for k, v in catalogue.items()}
        counts = {"floor": 0, "wall": 0, "prop": 0, "unknown": 0}
        for c in cells:
            xy = (c["x"], c["y"])
            atlas = (c["ax"], c["ay"])
            if is_wall_layer or atlas in wall_cells:
                solid[xy] = "wall"
                counts["wall"] += 1
            elif atlas in by_atlas:
                pname, spec = by_atlas[atlas]
                counts["prop"] += 1
                for fc in _footprint_cells(xy, spec["footprint"]):
                    floor.add(fc)
                    if spec["blocking"]:
                        solid[fc] = f"prop {pname}"
            elif atlas in floor_cells or not meta:
                floor.add(xy)
                counts["floor"] += 1
            else:
                floor.add(xy)
                counts["unknown"] += 1
        layers_seen.append({"name": node["name"], "tileset": ts_res, "cells": len(cells),
                            "manifest": bool(meta), **counts})

    if not tile_px:
        m = re.search(r"tile_size\s*=\s*Vector2i\(\s*(\d+)", "")
        tile_px = 32
        # the scene's own TileSet, when it is a file we can read
        for ts_res in [l["tileset"] for l in layers_seen if l["tileset"]]:
            ts_disk = proj / ts_res.replace("res://", "")
            if ts_disk.is_file():
                m = re.search(r"tile_size\s*=\s*Vector2i\(\s*(\d+)", ts_disk.read_text(encoding="utf-8", errors="replace"))
                if m:
                    tile_px = int(m.group(1))
                    break

    def cell_of(pos: tuple[float, float]) -> tuple[int, int]:
        return (int(pos[0] // tile_px), int(pos[1] // tile_px))

    # hand-baked props: a node with a footprint and a position
    rect_shapes = {sid: (float(w), float(h)) for sid, w, h in _SUB_RECT_RE.findall(text)}
    markers: list[dict] = []
    for node in parsed["nodes"]:
        if node is parsed["nodes"][0]:
            continue
        props = _sw.properties(text, parsed, node)
        pos = _vec(props.get("position"))
        fp = _vec(props.get("footprint"))
        if fp and pos:
            at = cell_of(pos)
            for fc in _footprint_cells(at, (int(fp[0]), int(fp[1]))):
                solid[fc] = f"prop {node['name']}"
            continue
        if node["type"] == "CollisionShape2D":
            m = re.search(r'SubResource\("?([^")]+)"?\)', props.get("shape", "") or "")
            parent = next((n for n in parsed["nodes"] if _sw.node_path(n) == node["parent"]), None)
            if m and m.group(1) in rect_shapes and parent and parent["type"] in ("StaticBody2D", "AnimatableBody2D"):
                ppos = _vec(_sw.properties(text, parsed, parent).get("position")) or (0.0, 0.0)
                lpos = pos or (0.0, 0.0)
                w, h = rect_shapes[m.group(1)]
                cx, cy = ppos[0] + lpos[0], ppos[1] + lpos[1]
                x0, y0 = int((cx - w / 2) // tile_px), int((cy - h / 2) // tile_px)
                x1, y1 = int((cx + w / 2 - 1) // tile_px), int((cy + h / 2 - 1) // tile_px)
                for yy in range(y0, y1 + 1):
                    for xx in range(x0, x1 + 1):
                        solid[(xx, yy)] = f"collider {parent['name']}"
            continue
        if not (node["instance"] or node["type"] == "Marker2D"):
            continue
        if node["parent"] not in (None, "."):
            continue
        lname = node["name"].lower().replace("_", "")
        if any(lname.startswith(ig) for ig in IGNORE_NAMES):
            continue
        scene = ""
        m = re.search(r'ExtResource\("([^"]+)"\)', text[node["start"]:node["start"] + 200])
        if m:
            scene = ext.get(m.group(1), "")
        if pos is None:
            markers.append({"name": node["name"], "kind": _kind_of(node["name"], scene),
                            "scene": scene, "cell": None, "pos": None})
            continue
        markers.append({"name": node["name"], "kind": _kind_of(node["name"], scene),
                        "scene": scene, "cell": list(cell_of(pos)), "pos": list(pos)})

    walkable = {c for c in floor if c not in solid}
    findings: list[dict] = []
    if not floor:
        findings.append({"code": "no_floor", "severity": "warn", "node": "",
                         "detail": "no TileMapLayer with floor cells was found; the "
                                   "audit can only check markers against solids"})

    spawns = [m for m in markers if m["kind"] == "spawn" and m["cell"]]
    if spawn:
        spawns = [m for m in markers if m["name"] == spawn and m["cell"]] or spawns
    region: set[tuple[int, int]] = set()
    if not spawns:
        findings.append({"code": "no_spawn", "severity": "warn", "node": "",
                         "detail": "no spawn marker (Spawn*/Player*/Start*, or spawn=) - "
                                   "reachability was not proven"})
    else:
        start = tuple(spawns[0]["cell"])
        seeds = [start] if start in walkable else [
            n for n in ((start[0] + 1, start[1]), (start[0] - 1, start[1]),
                        (start[0], start[1] + 1), (start[0], start[1] - 1)) if n in walkable]
        if start in solid:
            findings.append({"code": "marker_in_solid", "severity": "fail",
                             "node": spawns[0]["name"], "cell": list(start),
                             "detail": f"{spawns[0]['name']} spawns inside {solid[start]}"})
        q = deque(seeds)
        region = set(seeds)
        while q:
            x, y = q.popleft()
            for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if n in walkable and n not in region:
                    region.add(n)
                    q.append(n)

    for m in markers:
        if m["kind"] == "spawn" or m["cell"] is None:
            continue
        cell = tuple(m["cell"])
        if floor and cell not in floor and cell not in solid:
            findings.append({"code": "marker_off_map", "severity": "fail",
                             "node": m["name"], "cell": list(cell),
                             "detail": f"{m['name']} at cell {list(cell)} is not on any floor tile"})
            continue
        if cell in solid and m["kind"] in ("npc", "chest", "trigger"):
            findings.append({"code": "marker_in_solid", "severity": "fail",
                             "node": m["name"], "cell": list(cell),
                             "detail": f"{m['name']} at cell {list(cell)} is inside {solid[cell]}"})
            continue
        if spawns and m["kind"] in REACHABLE_KINDS:
            near = [cell] + [(cell[0] + 1, cell[1]), (cell[0] - 1, cell[1]),
                             (cell[0], cell[1] + 1), (cell[0], cell[1] - 1)]
            if not any(n in region for n in near):
                findings.append({"code": "unreachable", "severity": "fail",
                                 "node": m["name"], "cell": list(cell),
                                 "detail": f"{m['name']} at cell {list(cell)} cannot be "
                                           f"reached from {spawns[0]['name']}"})

    if spawns and walkable:
        stranded = len(walkable - region)
        if stranded:
            findings.append({"code": "floor_unreachable", "severity": "warn", "node": "",
                             "detail": f"{stranded} floor cell(s) cannot be reached from "
                                       f"{spawns[0]['name']} - a sealed pocket, or a wall "
                                       "that should have a gap"})

    fails = [f for f in findings if f["severity"] == "fail"]
    picture = render({"floor": sorted(floor), "solid": solid,
                      "markers": markers, "void": []}, region=region)
    return {"ok": not fails, "scene": scene_res, "tile_px": tile_px,
            "layers": layers_seen, "markers": markers,
            "counts": {"floor": len(floor), "solid": len(solid),
                       "walkable": len(walkable), "reached": len(region),
                       "fail": len(fails), "warn": len(findings) - len(fails)},
            "findings": findings, "ascii": picture}


def render(planned: dict, region: Optional[set] = None) -> str:
    """The room as text: # wall, . floor, : reached floor, letters for
    markers (S spawn, N npc, E entrance, X exit, T sign, C chest, ! trigger,
    ? other), P a prop. Cheaper than a screenshot and shows the one thing a
    screenshot does not - which cells the player can actually stand on."""
    floor = {tuple(c) for c in planned.get("floor") or []}
    solid = planned.get("solid") or {}
    solid_cells = {tuple(c) for c in (solid.keys() if isinstance(solid, dict) else solid)}
    solid_kind = solid if isinstance(solid, dict) else {}
    cells = floor | solid_cells
    for m in planned.get("markers") or []:
        if m.get("cell"):
            cells.add(tuple(m["cell"]))
    if not cells:
        return ""
    xs = [c[0] for c in cells]; ys = [c[1] for c in cells]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if (x1 - x0 + 1) * (y1 - y0 + 1) > 20_000:
        return "(too large to draw)"
    letters = {"spawn": "S", "npc": "N", "entrance": "E", "exit": "X", "sign": "T",
               "chest": "C", "trigger": "!", "other": "?"}
    grid = [[" "] * (x1 - x0 + 1) for _ in range(y1 - y0 + 1)]
    for (x, y) in floor:
        grid[y - y0][x - x0] = ":" if region and (x, y) in region else "."
    for c in solid_cells:
        kind = str(solid_kind.get(c, "wall"))
        grid[c[1] - y0][c[0] - x0] = "#" if kind == "wall" else "P"
    for m in planned.get("markers") or []:
        if m.get("cell"):
            x, y = m["cell"]
            grid[y - y0][x - x0] = letters.get(m.get("kind", "other"), "?")
    return "\n".join("".join(row) for row in grid)
