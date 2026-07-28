"""What a scene LOOKS like — every node resolved to something drawable.

The node graph answers "what is wired to what". It cannot answer "is the hat on
his head", and that is the question you actually have open when you are placing
things. Answering it means doing what the engine does at load: walk the tree,
accumulate each node's transform through its ancestors, resolve what it draws,
and sort the result by z.

So this module turns a .tscn into a flat, ordered DRAW LIST — one entry per
visible node, in paint order, each carrying a world transform and a description
of its picture. A browser canvas can render that directly, and a click can be
hit-tested against it.

Three things it resolves that are not obvious from the scene text alone:

  * ANIMATED SPRITES. An AnimatedSprite2D draws a REGION of a sheet, and which
    region lives in a separate SpriteFrames .tres — an AtlasTexture sub-resource
    per frame. Drawing the whole sheet instead (the naive read) puts a twelve-
    frame strip on screen where one character belongs.
  * CENTERING. A Sprite2D is centred on its position by default and a Control is
    not. Getting this wrong offsets half the scene by half a sprite, which reads
    as "the maths is broken" rather than "one flag was missed".
  * CONTROL RECTS. A ColorRect's size is not a property, it is the difference
    between two anchor offsets. Placeholder art in these projects is mostly
    ColorRects, so a viewport that cannot place them shows an empty stage.

I/O-free by construction: the caller supplies a `read` callable for res:// paths
and a `size_of` callable for images. That keeps the geometry testable without a
project on disk, which is the only way the transform maths gets pinned properly.
"""
from __future__ import annotations

import math
import re
from typing import Callable, Optional

from bgate_core import scenewire

# Default Godot viewport, used when project.godot says nothing.
DEFAULT_VIEWPORT = (1152, 648)

_VEC2_RE = re.compile(r"Vector2\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)")
_VEC2I_RE = re.compile(r"Vector2i\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)")
_COLOR_RE = re.compile(
    r"Color\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)")
_RECT2_RE = re.compile(
    r"Rect2\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)")
_EXT_ID_RE = re.compile(r'ExtResource\("([^"]+)"\)')
_SUB_ID_RE = re.compile(r'SubResource\("([^"]+)"\)')

# Node classes that put something on screen. Anything else is structure — it
# still gets an entry (so it can be selected and moved) but draws as a marker.
_IMAGE_TYPES = {"Sprite2D", "AnimatedSprite2D", "TextureRect", "NinePatchRect"}
_RECT_TYPES = {"ColorRect", "Panel", "Button", "Label", "RichTextLabel",
               "ProgressBar"}
_CONTROL_TYPES = _RECT_TYPES | {"TextureRect", "NinePatchRect", "Control",
                                "VBoxContainer", "HBoxContainer"}
# Classes that exist to HOLD other nodes. One of these with no children in the
# file is not a node that failed to draw — it is a container something fills at
# run time, and reporting it as blank-for-no-reason is what makes the viewport
# read as broken.
_CONTAINER_TYPES = {"Node", "Node2D", "Node3D", "CanvasLayer", "YSort",
                    "Control", "ParallaxBackground", "ParallaxLayer"}


def _vec2(value: str, default=(0.0, 0.0)) -> tuple[float, float]:
    m = _VEC2_RE.search(value or "") or _VEC2I_RE.search(value or "")
    return (float(m.group(1)), float(m.group(2))) if m else default


def _num(value: str, default=0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bool(value: str, default=True) -> bool:
    text = str(value or "").strip().lower()
    if text in ("true", "1"):
        return True
    if text in ("false", "0"):
        return False
    return default


def _color(value: str, default=(1.0, 1.0, 1.0, 1.0)) -> tuple:
    m = _COLOR_RE.search(value or "")
    return tuple(float(m.group(i)) for i in range(1, 5)) if m else default


# ---------------------------------------------------------------------------
# SpriteFrames
# ---------------------------------------------------------------------------
def first_frame(tres_text: str) -> Optional[dict]:
    """The sheet and region an AnimatedSprite2D shows before anything plays.

    A SpriteFrames is a list of animations whose frames point at AtlasTexture
    sub-resources; each of those carries the Rect2 region on the sheet. Taking
    the first frame of the first animation is what the editor shows for an
    unplayed AnimatedSprite2D, so it is what a static preview should show.
    """
    if not tres_text:
        return None
    atlases: dict[str, tuple[str, tuple]] = {}
    current = None
    for block in re.split(r"\n(?=\[)", tres_text):
        head = block.split("\n", 1)[0]
        m = re.match(r'\[sub_resource type="AtlasTexture" id="([^"]+)"\]', head)
        if not m:
            continue
        current = m.group(1)
        ext = _EXT_ID_RE.search(block)
        rect = _RECT2_RE.search(block)
        if ext and rect:
            atlases[current] = (ext.group(1),
                                tuple(float(rect.group(i)) for i in range(1, 5)))
    if not atlases:
        return None

    ext_paths = {m.group(1): m.group(2) for m in re.finditer(
        r'\[ext_resource type="[^"]*"\s+(?:uid="[^"]*"\s+)?path="([^"]+)"\s+id="([^"]+)"\]',
        tres_text)}
    ext_paths = {v: k for k, v in ext_paths.items()}   # id -> path

    order = re.search(r'"texture":\s*SubResource\("([^"]+)"\)', tres_text)
    key = order.group(1) if order and order.group(1) in atlases else \
        next(iter(atlases))
    ext_id, region = atlases[key]
    sheet = ext_paths.get(ext_id)
    if not sheet:
        return None
    return {"sheet": sheet, "region": list(region)}


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------
def _compose(parent: dict, local: dict) -> dict:
    """Child transform in world space. Godot's Transform2D, spelled out.

    Rotation composes, scale multiplies, and the child's offset is rotated and
    scaled by the parent before it is added — skipping that last part is the
    classic bug where children drift as soon as a parent is rotated.
    """
    cos, sin = math.cos(parent["rot"]), math.sin(parent["rot"])
    lx, ly = local["x"] * parent["sx"], local["y"] * parent["sy"]
    return {
        "x": parent["x"] + lx * cos - ly * sin,
        "y": parent["y"] + lx * sin + ly * cos,
        "rot": parent["rot"] + local["rot"],
        "sx": parent["sx"] * local["sx"],
        "sy": parent["sy"] * local["sy"],
    }


IDENTITY = {"x": 0.0, "y": 0.0, "rot": 0.0, "sx": 1.0, "sy": 1.0}


def _local(props: dict, node_type: str) -> dict:
    """A node's own transform. Controls position by anchor offset, not position."""
    if node_type in _CONTROL_TYPES:
        x = _num(props.get("offset_left", 0))
        y = _num(props.get("offset_top", 0))
        pos = (x, y)
    else:
        pos = _vec2(props.get("position", ""))
    scale = _vec2(props.get("scale", ""), (1.0, 1.0))
    return {"x": pos[0], "y": pos[1], "rot": _num(props.get("rotation", 0)),
            "sx": scale[0] or 1.0, "sy": scale[1] or 1.0}


def _control_size(props: dict) -> tuple[float, float]:
    if "size" in props:
        return _vec2(props["size"], (0.0, 0.0))
    w = _num(props.get("offset_right", 0)) - _num(props.get("offset_left", 0))
    h = _num(props.get("offset_bottom", 0)) - _num(props.get("offset_top", 0))
    return (w, h)


# ---------------------------------------------------------------------------
# The draw list
# ---------------------------------------------------------------------------
def draw_list(scene_text: str, *,
              read: Callable[[str], Optional[str]],
              size_of: Callable[[str], Optional[tuple]],
              rel_of: Callable[[str], Optional[str]],
              viewport: tuple[int, int] = DEFAULT_VIEWPORT) -> dict:
    """Every node as a positioned, ordered, drawable entry.

    `read` returns the text of a res:// resource (for SpriteFrames), `size_of`
    the pixel dimensions of a res:// image, and `rel_of` the project-relative
    path the browser can fetch it by. All three are injected so the geometry can
    be tested without a project on disk.
    """
    items = _walk(scene_text, read=read, size_of=size_of, rel_of=rel_of,
                  stack=())
    # Paint order: z_index first, then declaration order, exactly as Godot does
    # for siblings at the same z. Sorting by z alone would shuffle everything
    # that shares the default 0 into whatever order the sort felt like.
    items.sort(key=lambda i: (i["z"], i["order"]))
    return {"viewport": list(viewport), "items": items}


def _walk(scene_text: str, *, read, size_of, rel_of,
          stack: tuple[str, ...] = ()) -> list[dict]:
    nodes = scenewire.outline(scene_text)
    by_path = {n["path"]: n for n in nodes}
    world: dict[str, dict] = {}
    items = []

    # How many children each node has IN THE FILE. An empty container is the
    # single most common "why can I not edit this" — the answer is that a script
    # fills it at run time — and that is only knowable with the whole tree in
    # hand, so it is counted here and handed to the per-node resolver.
    kids: dict[str, int] = {}
    for node in nodes:
        parent = node["parent"]
        if parent is not None:
            kids[parent] = kids.get(parent, 0) + 1

    for order, node in enumerate(nodes):
        props = node["properties"]
        parent = node["parent"]
        base = world.get(parent if parent != "." else ".", IDENTITY) \
            if parent is not None else IDENTITY
        tf = _compose(base, _local(props, node["type"]))
        world[node["path"]] = tf

        visible = _bool(props.get("visible", "true"))
        # Godot hides a whole subtree, so an invisible parent means invisible
        # children no matter what their own flag says.
        if parent is not None:
            host = by_path.get(parent if parent != "." else ".")
            if host is not None and not host.get("_visible", True):
                visible = False
        node["_visible"] = visible

        draw = _draw_for(node, props, read=read, size_of=size_of, rel_of=rel_of,
                         children=kids.get(node["path"], 0))
        items.append({
            "path": node["path"], "name": node["name"], "type": node["type"],
            "role": node["role"], "order": order,
            "script": node.get("script", ""),
            "children": kids.get(node["path"], 0),
            "x": round(tf["x"], 3), "y": round(tf["y"], 3),
            "rot": round(tf["rot"], 5),
            "sx": round(tf["sx"], 4), "sy": round(tf["sy"], 4),
            "z": int(_num(props.get("z_index", 0))),
            "visible": visible,
            "modulate": list(_color(props.get("modulate", ""))),
            "draw": draw,
        })
        if node["instance"]:
            items.extend(_open_instance(
                items[-1], node, tf, order,
                read=read, size_of=size_of, rel_of=rel_of, stack=stack))

    return items


# ---------------------------------------------------------------------------
# Instanced scenes
# ---------------------------------------------------------------------------
# An instanced child carries no `type=` and no properties of its own, so every
# branch above falls through and it draws as a bare unlabelled dot. That is the
# one shape a scene made of individual, editable components is entirely built
# out of — a floor of Desk_01, Desk_02, Plant_01 would have rendered as an empty
# frame. So the instance is OPENED: its scene is read, drawn at the instance's
# world transform, and its nodes are carried along as `of` entries the viewport
# shows but does not let you edit. Godot selects the instance, not its insides.
_MAX_INSTANCE_DEPTH = 3


def _open_instance(host: dict, node: dict, tf: dict, order: int, *,
                   read, size_of, rel_of, stack: tuple[str, ...]) -> list[dict]:
    """Give ``host`` the instanced scene's picture; return its nodes as items."""
    res_path = next((r["path"] for r in node["resources"]
                     if r["property"] == "instance"), "")
    name = res_path.rsplit("/", 1)[-1] or "a scene"
    if not res_path:
        host["draw"] = {"kind": "marker", "reason": "instance of nothing"}
        return []
    host["instance"] = res_path
    if res_path in stack:
        host["draw"] = {"kind": "marker",
                        "reason": f"{name} instances itself"}
        return []
    if len(stack) >= _MAX_INSTANCE_DEPTH:
        host["draw"] = {"kind": "marker",
                        "reason": f"instance of {name} — nested too deep to draw"}
        return []
    try:
        sub = _walk(read(res_path) or "", read=read, size_of=size_of,
                    rel_of=rel_of, stack=stack + (res_path,))
    except (scenewire.WireError, ValueError):
        sub = []
    if not sub:
        host["draw"] = {"kind": "marker",
                        "reason": f"instance of {name} — could not read it"}
        return []

    sub.sort(key=lambda i: (i["z"], i["order"]))
    root = next((i for i in sub if i["path"] == "."), None)
    picture = (root or {}).get("draw") or {}
    host["draw"] = picture if picture.get("kind") != "marker" else {
        "kind": "marker", "reason": f"instance of {name}"}
    host["children"] = sum(1 for i in sub
                           if i["path"] != "." and "/" not in i["path"])

    out = []
    for k, entry in enumerate(sub):
        if entry is root:
            continue
        world = _compose(tf, {"x": entry["x"], "y": entry["y"],
                              "rot": entry["rot"],
                              "sx": entry["sx"], "sy": entry["sy"]})
        out.append({**entry,
                    "path": f'{host["path"]}/{entry["path"]}',
                    # Which instance owns it. The viewport routes a click here
                    # to the instance itself rather than offering to move a node
                    # that lives in another file. A nested instance keeps its
                    # own owner — re-prefixed, so the path still resolves.
                    "of": f'{host["path"]}/{entry["of"]}' if entry.get("of")
                          else host["path"],
                    "order": order + (k + 1) / 1000.0,
                    "z": host["z"] + entry["z"],
                    "visible": host["visible"] and entry["visible"],
                    "x": round(world["x"], 3), "y": round(world["y"], 3),
                    "rot": round(world["rot"], 5),
                    "sx": round(world["sx"], 4), "sy": round(world["sy"], 4)})

    # Did opening it actually produce a picture? An instance whose insides all
    # get their art from a script at run time is the normal case in these
    # projects, and it is a very different thing from one that failed to load —
    # so it says which, and the viewport keeps reporting it as blank-with-a-
    # reason instead of quietly drawing nothing.
    host["drawn"] = sum(1 for i in [host, *out]
                        if i["draw"].get("kind") in ("image", "rect", "tiles"))
    if not host["drawn"]:
        host["draw"] = {"kind": "marker", "reason":
                        f"instance of {name} — nothing in it draws in the file"}
    return out


def _draw_for(node: dict, props: dict, *, read, size_of, rel_of,
              children: int = 0) -> dict:
    ntype = node["type"]
    textures = {r["property"]: r["path"] for r in node["resources"]}

    # A tile-based game IS its tilemaps. Skipping them reported "nothing in
    # this scene draws" for a level made of five hundred tiles.
    if ntype in ("TileMapLayer", "TileMap"):
        from bgate_core import tilemap
        tres = textures.get("tile_set")
        packed = props.get("tile_map_data", "")
        m = re.search(r'PackedByteArray\("([^"]*)"\)', packed)
        if not tres or not m:
            return {"kind": "marker", "reason":
                    "no TileSet assigned" if not tres else "no tiles placed"}
        try:
            layer = tilemap.layer_draw(m.group(1),
                                       tilemap.parse_tileset(read(tres) or ""))
        except tilemap.TileError as exc:
            return {"kind": "marker", "reason": str(exc)}
        if not layer["cells"]:
            return {"kind": "marker", "reason": "no tiles placed"}
        # Resolve each source's texture to something the browser can fetch,
        # dropping any that cannot be — a tile pointing at a missing file
        # should not take the other four hundred down with it.
        sources, dropped = {}, 0
        for sid, src in layer["sources"].items():
            rel = rel_of(src["texture"])
            size = size_of(src["texture"])
            if not rel or not size:
                dropped += 1
                continue
            sources[str(sid)] = {"rel": rel, "region": src["region"],
                                 "origin": src["origin"],
                                 "sheet": [size[0], size[1]]}
        cells = [c for c in layer["cells"] if str(c[2]) in sources]
        if not cells:
            return {"kind": "marker", "reason": "tile textures are missing"}
        return {"kind": "tiles", "tile_size": layer["tile_size"],
                "shape": layer["shape"], "layout": layer["layout"],
                "sources": sources, "cells": cells,
                "dropped_sources": dropped,
                "bounds": tilemap.bounds(cells, shape=layer["shape"],
                                         layout=layer["layout"],
                                         tile_size=layer["tile_size"])}

    if ntype == "AnimatedSprite2D":
        tres = textures.get("sprite_frames")
        frame = first_frame(read(tres) or "") if tres else None
        if frame:
            rel = rel_of(frame["sheet"])
            if rel:
                return {"kind": "image", "rel": rel, "region": frame["region"],
                        "size": frame["region"][2:],
                        "centered": _bool(props.get("centered", "true")),
                        "offset": list(_vec2(props.get("offset", ""))),
                        "source": frame["sheet"]}
        return {"kind": "marker", "reason":
                "no SpriteFrames assigned" if not tres else "unreadable frames"}

    if ntype in ("Sprite2D", "Sprite3D"):
        path = textures.get("texture")
        if path:
            rel, size = rel_of(path), size_of(path)
            if rel and size:
                region = list(_RECT2_RE.search(props.get("region_rect", "")).groups()) \
                    if _bool(props.get("region_enabled", "false")) \
                    and _RECT2_RE.search(props.get("region_rect", "")) else None
                return {"kind": "image", "rel": rel,
                        "region": [float(v) for v in region] if region
                        else [0, 0, size[0], size[1]],
                        "size": [size[0], size[1]] if not region
                        else [float(region[2]), float(region[3])],
                        "centered": _bool(props.get("centered", "true")),
                        "offset": list(_vec2(props.get("offset", ""))),
                        "source": path}
        return {"kind": "marker", "reason": "no texture assigned"}

    if ntype in ("TextureRect", "NinePatchRect"):
        path = textures.get("texture")
        w, h = _control_size(props)
        if path:
            rel, size = rel_of(path), size_of(path)
            if rel and size:
                return {"kind": "image", "rel": rel,
                        "region": [0, 0, size[0], size[1]],
                        "size": [w or size[0], h or size[1]],
                        "centered": False, "offset": [0, 0], "source": path}
        return {"kind": "rect", "size": [w, h], "color": [0.3, 0.3, 0.35, 0.6]}

    if ntype in _RECT_TYPES:
        w, h = _control_size(props)
        return {"kind": "rect", "size": [w, h],
                "color": list(_color(props.get("color", ""),
                                     (0.35, 0.38, 0.45, 0.85))),
                "text": node["name"] if ntype in ("Label", "Button") else ""}

    if ntype in ("Camera2D", "Camera3D"):
        return {"kind": "camera"}

    if ntype in ("CollisionShape2D", "CollisionPolygon2D", "Area2D",
                 "StaticBody2D", "CharacterBody2D", "RigidBody2D"):
        return {"kind": "body"}

    # An empty container is the honest answer to "why can I only edit the
    # floors and walls": there is nothing in the file to edit, because a script
    # puts the contents there with add_child at run time. The root is exempt —
    # a one-node scene is a script host, which the caller already says better.
    if ntype in _CONTAINER_TYPES and not children \
            and node["parent"] is not None:
        return {"kind": "marker", "reason": "no children in the file"}

    return {"kind": "marker", "reason": ""}


def viewport_of(project_godot_text: str) -> tuple[int, int]:
    """The game's own resolution, so the viewport frame is the real frame."""
    w = re.search(r"window/size/viewport_width\s*=\s*(\d+)",
                  project_godot_text or "")
    h = re.search(r"window/size/viewport_height\s*=\s*(\d+)",
                  project_godot_text or "")
    return (int(w.group(1)) if w else DEFAULT_VIEWPORT[0],
            int(h.group(1)) if h else DEFAULT_VIEWPORT[1])
