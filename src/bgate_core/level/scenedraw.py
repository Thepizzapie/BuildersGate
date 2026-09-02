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

import copy
import math
import re
from typing import Callable, Optional

from . import scenewire

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
# What a light with no readable cookie falls back to: opaque at the centre,
# gone at the edge. Not a guess at the artist's ramp — a stand-in that is
# obviously a light, so a fixture is never invisible just because its texture
# is a format nothing here can read.
_DEFAULT_FALLOFF = [[0.0, 1.0], [0.55, 0.45], [1.0, 0.0]]

_GRADIENT_OFFSETS_RE = re.compile(r"offsets\s*=\s*PackedFloat32Array\(([^)]*)\)")
_GRADIENT_COLORS_RE = re.compile(r"colors\s*=\s*PackedColorArray\(([^)]*)\)")


def gradient_texture(tres_text: str) -> dict:
    """A GradientTexture2D .tres as stops the canvas can rebuild.

    Only the ALPHA ramp is carried, not the stop colours: a light cookie is a
    shape, and its colour comes from the light's own `color` property. Keeping
    the ramp's own white would tint every light white and throw away the one
    thing the level design is saying with them.
    """
    if "GradientTexture2D" not in tres_text:
        return {}
    offs = _GRADIENT_OFFSETS_RE.search(tres_text)
    cols = _GRADIENT_COLORS_RE.search(tres_text)
    if not offs or not cols:
        return {}
    try:
        points = [float(v) for v in offs.group(1).split(",") if v.strip()]
        channels = [float(v) for v in cols.group(1).split(",") if v.strip()]
    except ValueError:
        return {}
    # PackedColorArray is flat rgba, so a stop's alpha is every fourth value.
    alphas = channels[3::4]
    if not points or len(alphas) != len(points):
        return {}
    w = re.search(r"^width\s*=\s*(\d+)", tres_text, re.M)
    h = re.search(r"^height\s*=\s*(\d+)", tres_text, re.M)
    return {"gradient": [[p, a] for p, a in zip(points, alphas)],
            "size": [int(w.group(1)) if w else 256,
                     int(h.group(1)) if h else 256]}


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
              viewport: tuple[int, int] = DEFAULT_VIEWPORT,
              scale: float = 1.0) -> dict:
    """Every node as a positioned, ordered, drawable entry.

    `read` returns the text of a res:// resource (for SpriteFrames), `size_of`
    the pixel dimensions of a res:// image, and `rel_of` the project-relative
    path the browser can fetch it by. All three are injected so the geometry can
    be tested without a project on disk.
    """
    # One parse of each instanced scene per request, not one per instance. A
    # dressed floor is 233 props over three distinct .tscn files; re-reading and
    # re-parsing prop.tscn 233 times turned panning — which this endpoint exists
    # to keep off the network — into a second of server CPU.
    items = _walk(scene_text, read=read, size_of=size_of, rel_of=rel_of,
                  stack=(), cache={})
    # Paint order: z_index first, then the order the tree paints in — which on
    # a y-sorted scene is NOT declaration order. See paint_order().
    paint_order(items)
    items.sort(key=lambda i: (i["z"], i["paint"]))
    return {"viewport": list(viewport), "items": items,
            "tint": _canvas_tint(items),
            # What the PLAYER sees one world pixel as. See content_scale().
            "scale": float(scale or 1.0)}


# ---------------------------------------------------------------------------
# Y-sort
# ---------------------------------------------------------------------------
# WHY A DRESSED ISOMETRIC ROOM LOOKED SCRAMBLED EVEN WHEN EVERY NODE WAS IN THE
# RIGHT PLACE.
#
# This module used to paint by (z_index, declaration order). Godot paints a
# y-sorted parent's children by their GLOBAL Y, and floor_tut sets
# `y_sort_enabled` on five nodes. Declaration order there is prop_id order — map
# reading order — which has nothing to do with screen depth, so any two props
# whose file order disagreed with their depth drew the wrong way round: a desk
# over the chair tucked under it, a plant through the partition in front of it.
# On an isometric plate that is not an edge case, it is most pairs.
#
# THE KEY IS THE NODE'S OWN ORIGIN, NEVER ITS PICTURE. prop.tscn splits the two
# deliberately: `position` on the root Node2D is where the prop STANDS (the sort
# key) and `Art.offset` on the child sprite is how the art sits on the cell (the
# registration). Sorting by the drawn rectangle would fold the registration back
# into the depth and reintroduce exactly the bug that split exists to prevent —
# the same reason the cutaway shader reads MODEL_MATRIX's origin.
#
# WHAT IS MODELLED, and what is approximated:
#   * y_sort_enabled is read from the scene file per node, not assumed.
#   * The engine FLATTENS: a non-y-sorted child of a y-sorted node does not form
#     a group — it and its whole subtree join the ancestor's one sorted list,
#     each at its own origin. That is why a prop instance (Node2D root, not
#     y-sorted) and its Art sprite both land in Characters' list.
#   * A y-sorted child participates in its parent's list as a BLOCK and sorts
#     its own contents. So Ground/Props/Walls/Characters, all at y = 0 under a
#     y-sorted root, keep their file order and cannot interleave — which is the
#     engine's answer too, and is why this project fades walls with a shader
#     rather than expecting a prop to sort behind one.
#   * Ties break on declaration order, as the engine's `ysort_index` does, so
#     the picture is stable between repaints.
#   * NOT modelled: `y_sort_origin` on TileData (nothing in these projects sets
#     it), and z_index's interaction with y-sort beyond "z still wins", which is
#     the rule the panel already used.
def paint_order(items: list[dict]) -> None:
    """Stamp every item with `paint`, the index the engine would draw it at."""
    by_path = {i["path"]: i for i in items}
    kids: dict[str, list[dict]] = {}
    for i in items:
        path = i["path"]
        if path == ".":
            continue
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        # An instance's insides are named under the host, so the host is their
        # parent even though they come from another file. That is also what the
        # engine sees once the scene is instantiated.
        while parent and parent not in by_path and "/" in parent:
            parent = parent.rsplit("/", 1)[0]
        kids.setdefault(parent if parent in by_path else ".", []).append(i)

    def sorts(item: Optional[dict]) -> bool:
        return bool(item and item.get("ysort"))

    def members(host: dict) -> list[dict]:
        """Everything in ``host``'s one sorted list — its subtree, flattened
        through descendants that do not sort for themselves."""
        out: list[dict] = []
        stack = list(kids.get(host["path"], []))
        while stack:
            child = stack.pop(0)
            out.append(child)
            if not sorts(child):
                stack = kids.get(child["path"], []) + stack
        return out

    seq = 0

    def stamp(item: dict) -> None:
        nonlocal seq
        item["paint"] = seq
        seq += 1

    def block(item: dict) -> None:
        """An entry of some enclosing sorted list: itself, then its own list if
        it sorts. If it does not sort, its subtree is already in that list."""
        stamp(item)
        if sorts(item):
            for m in sorted(members(item),
                            key=lambda i: (round(i["y"], 4), i["order"])):
                block(m)

    def walk(item: dict) -> None:
        stamp(item)
        if sorts(item):
            for m in sorted(members(item),
                            key=lambda i: (round(i["y"], 4), i["order"])):
                block(m)
        else:
            for c in kids.get(item["path"], []):
                walk(c)

    root = by_path.get(".")
    if root is not None:
        walk(root)
    # Anything the walk could not reach (a malformed path) still needs a key,
    # and its declaration order is the honest one.
    for i in items:
        i.setdefault("paint", seq + i["order"])


def _canvas_tint(items: list[dict]) -> Optional[list[float]]:
    """The scene's CanvasModulate, if it has one.

    A whole floor lit warm in the engine and flat grey in the viewport is
    mostly this one node: CanvasModulate multiplies everything drawn on the
    canvas, and a preview that ignores it is a preview of a different scene.
    It is a property of the CANVAS, not of any node's own rectangle, so it
    rides on the payload rather than in the draw list — the client applies it
    over the frame once, after the items and before the lights, which is the
    order the engine composites in.
    """
    for item in items:
        if item["type"] == "CanvasModulate" and item["visible"]:
            return item["draw"].get("color")
    return None


def _walk(scene_text: str, *, read, size_of, rel_of,
          stack: tuple[str, ...] = (),
          patch: Optional[dict] = None,
          cache: Optional[dict] = None,
          nodes: Optional[list] = None) -> list[dict]:
    if nodes is None:
        nodes = scenewire.outline(scene_text)
    if patch:
        _apply_overrides(nodes, patch)
    overrides, skip = _collect_overrides(nodes)
    by_path = {n["path"]: n for n in nodes}
    world: dict[str, dict] = {}
    items = []

    # How many children each node has IN THE FILE. An empty container is the
    # single most common "why can I not edit this" — the answer is that a script
    # fills it at run time — and that is only knowable with the whole tree in
    # hand, so it is counted here and handed to the per-node resolver.
    # An override is not a child, so it is not counted as one: doing so made
    # every dressed prop look like a container with contents.
    kids: dict[str, int] = {}
    for node in nodes:
        parent = node["parent"]
        if parent is not None and node["path"] not in skip:
            kids[parent] = kids.get(parent, 0) + 1

    for order, node in enumerate(nodes):
        # An override block is a PATCH, not a node — it has already been folded
        # into the instanced scene it belongs to. Emitting it as well produced a
        # second, typeless item on the same path that drew nothing.
        if node["path"] in skip:
            continue
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
            # Whether this node sorts ITS CHILDREN by y. Read, never assumed:
            # the flag is what decides whether declaration order or depth wins,
            # and guessing it wrong reorders a whole room. See paint_order().
            "ysort": _bool(props.get("y_sort_enabled", "false"), False),
            "visible": visible,
            "modulate": list(_color(props.get("modulate", ""))),
            "draw": draw,
        })
        if node["instance"]:
            items.extend(_open_instance(
                items[-1], node, tf, order,
                read=read, size_of=size_of, rel_of=rel_of, stack=stack,
                patch=overrides.get(node["path"]), cache=cache))

    return items


# ---------------------------------------------------------------------------
# Overrides on nodes that live inside an instance
# ---------------------------------------------------------------------------
# THIS IS WHY A DRESSED FLOOR RENDERED AS FIVE HUNDRED CROSSES.
#
# Opening the instanced scene was never the missing piece — `_open_instance`
# has always done that. The missing piece is that in these projects the
# instanced scene is a BLANK: prop.tscn is a Node2D with an empty Sprite2D
# called Art, and which picture that Art wears is decided by the HOST file, as
# a Godot override block:
#
#   [node name="FilingCabinetSE_000" parent="Characters" instance=ExtResource("bp_scene")]
#   [node name="Art" parent="Characters/FilingCabinetSE_000" index="0"]
#   texture = ExtResource("bp_prop_filing_cabinet_se")
#
# The second block has no `type=` because it is not declaring a node — it is
# re-setting a property on one that prop.tscn already declares. Reading the two
# files separately, both are correct and neither draws: the instance says "ask
# prop.tscn", prop.tscn says "no texture assigned", and the endpoint reported
# `instance of prop.tscn — nothing in it draws in the file`, which was true and
# useless. Godot composes them. So do we.
def _override_host(path: str, instances: set[str]) -> Optional[str]:
    """The nearest ancestor of ``path`` that is an instanced scene, if any."""
    parts = path.split("/")
    for i in range(len(parts) - 1, 0, -1):
        candidate = "/".join(parts[:i])
        if candidate in instances:
            return candidate
    return None


def _collect_overrides(nodes: list[dict]) -> tuple[dict, set[str]]:
    """Split the outline into real nodes and patches onto instanced scenes.

    Returns ``({instance path: {relative path: node}}, {paths to skip})``.
    A block under an instance that DOES carry a type is a genuinely new node
    added beside the instance's own contents, and stays a node.
    """
    instances = {n["path"] for n in nodes if n["instance"]}
    if not instances:
        return {}, set()
    out: dict[str, dict[str, dict]] = {}
    skip: set[str] = set()
    for node in nodes:
        if node["type"]:
            continue
        host = _override_host(node["path"], instances)
        if host is None:
            continue
        out.setdefault(host, {})[node["path"][len(host) + 1:]] = node
        skip.add(node["path"])
    return out, skip


def _apply_overrides(nodes: list[dict], patch: dict) -> None:
    """Fold the host's overrides into the instanced scene's own nodes."""
    for node in nodes:
        over = patch.get(node["path"])
        if not over:
            continue
        node["properties"] = {**node["properties"], **over["properties"]}
        # A resource named by the override REPLACES the scene's own — that is
        # the entire point of `texture = ExtResource(...)` on an Art node whose
        # scene deliberately ships without one. Merge by property so an
        # override of `texture` does not drop an untouched `material`.
        merged = {r["property"]: r for r in node["resources"]}
        for res in over["resources"]:
            merged[res["property"]] = res
        node["resources"] = list(merged.values())


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

# Everything that puts something on the canvas, as opposed to a cross with a
# reason next to it.
_PICTURE_KINDS = ("image", "rect", "tiles", "light", "tint")


def _open_instance(host: dict, node: dict, tf: dict, order: int, *,
                   read, size_of, rel_of, stack: tuple[str, ...],
                   patch: Optional[dict] = None,
                   cache: Optional[dict] = None) -> list[dict]:
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
        # The outline is the parse; the patch is per-instance, so the cached
        # copy has to be deep — _apply_overrides writes into these dicts and a
        # shared one would leak the first prop's texture onto all 233.
        if cache is None:
            parsed = scenewire.outline(read(res_path) or "")
        else:
            if res_path not in cache:
                cache[res_path] = scenewire.outline(read(res_path) or "")
            parsed = copy.deepcopy(cache[res_path])
        sub = _walk("", read=read, size_of=size_of, rel_of=rel_of,
                    stack=stack + (res_path,), patch=patch, cache=cache,
                    nodes=parsed)
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
    # THE INSTANCED ROOT'S OWN TRANSFORM, carried on the PICTURE rather than
    # folded into the host node.
    #
    # light_fluoro_panel.tscn sets scale = Vector2(1, 0.5) on its root, and its
    # own comment says why: the cookie is a circle, the floor is a 64x32
    # isometric diamond, and an unsquashed pool reads as a sphere hovering in
    # the air. Dropping that scale drew 44 circles over an isometric room.
    #
    # It must NOT be composed into host x/y/sx/sy: those are what a drag reads
    # and what `apply` writes back as this node's `position`, and a host
    # carrying its instance's internal scale would write that scale into the
    # parent file the first time anyone nudged it.
    if root is not None and host["draw"].get("kind") != "marker":
        local = {"x": root["x"], "y": root["y"], "rot": root["rot"],
                 "sx": root["sx"], "sy": root["sy"]}
        if local != {"x": 0.0, "y": 0.0, "rot": 0.0, "sx": 1.0, "sy": 1.0}:
            host["draw"] = {**host["draw"], "local": local}
    host["children"] = sum(1 for i in sub
                           if i["path"] != "." and "/" not in i["path"])
    # The instanced ROOT's y-sort flag belongs to the host, which is the node
    # that node now IS — a y-sorted container shipped as its own scene sorts
    # its contents wherever it is placed. A host that sets the flag itself as
    # an override keeps it.
    if root is not None and root.get("ysort"):
        host["ysort"] = True

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
    # "light" and "tint" count as drawing. Every ceiling fitting in these
    # projects is an instance of a one-node light scene, so leaving them out
    # of this tally overwrote 44 correctly-resolved lights with
    # "nothing in it draws in the file" a line after resolving them.
    host["drawn"] = sum(1 for i in [host, *out]
                        if i["draw"].get("kind") in _PICTURE_KINDS)
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
        from . import tilemap
        tres = textures.get("tile_set")
        packed = props.get("tile_map_data", "")
        m = re.search(r'PackedByteArray\("([^"]*)"\)', packed)
        if not tres or not m:
            return {"kind": "marker", "reason":
                    "no TileSet assigned" if not tres else "no tiles placed"}
        try:
            layer = tilemap.layer_draw(
                m.group(1), tilemap.parse_tileset(read(tres) or ""),
                y_sort=_bool(props.get("y_sort_enabled", "false"), False))
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
                # Bounded by what the layer DRAWS, not by the cells it fills.
                # A 100px wall tile reaches 68px above its own 64x32 cell, and
                # a bound that stopped at the cell framed the opening view on a
                # rectangle with the tops of every wall outside it.
                "bounds": tilemap.bounds(
                    cells, shape=layer["shape"], layout=layer["layout"],
                    tile_size=layer["tile_size"],
                    sources={int(sid): src for sid, src in sources.items()})}

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

    # LIGHTS. The rooms in these projects are lit per area — warm over the
    # bullpen, cold over the server aisle — and a viewport that draws only the
    # albedo shows one evenly grey floor and nothing that says which room you
    # are in. A PointLight2D is its texture, multiplied by colour and energy
    # and added to what is under it, which is cheap enough to be worth doing
    # and close enough to be recognisably the same scene.
    #
    # Deliberately NOT modelled: LightOccluder2D and shadow casting. That is
    # a per-light visibility solve against 66 occluder polygons, and it buys
    # edges on shadows in a panel whose job is "is the hat on his head".
    if ntype == "CanvasModulate":
        return {"kind": "tint",
                "color": list(_color(props.get("color", "")))}

    if ntype in ("PointLight2D", "Light2D", "DirectionalLight2D"):
        path = textures.get("texture")
        rel = rel_of(path) if path else None
        size = size_of(path) if path else None
        cookie: dict = {}
        if rel and size:
            cookie = {"rel": rel, "size": [size[0], size[1]]}
        else:
            # A light's cookie is usually NOT a png. Every fixture in these
            # projects points at a GradientTexture2D .tres — a radial ramp
            # authored in the editor, with no file for the browser to fetch —
            # so `rel_of` came back empty and 44 lit rooms reported "no light
            # texture assigned". The ramp is three lines of text; send it and
            # let the canvas build the same falloff natively.
            cookie = gradient_texture(read(path) or "") if path else {}
            if not cookie:
                cookie = {"gradient": _DEFAULT_FALLOFF, "size": [256, 256]}
        return {"kind": "light", **cookie,
                "color": list(_color(props.get("color", ""))),
                "energy": _num(props.get("energy", 1.0), 1.0),
                "scale": _num(props.get("texture_scale", 1.0), 1.0),
                "offset": list(_vec2(props.get("offset", ""))),
                # 0 = ADD, 1 = SUB, 2 = MIX. Only the default is drawn as light;
                # the other two are rare and guessing at them would be worse
                # than saying the light is there and leaving it out of the mix.
                "blend": int(_num(props.get("blend_mode", 0))),
                "source": path}

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


def content_scale(project_godot_text: str) -> float:
    """How many screen pixels the game puts on one world pixel.

    THE ANSWER TO "props are not scaled right in Atlas", and it is not a prop
    bug. Measured against the engine's own frame of floor_tut by template
    matching the source art: `floor_carpet.png` (64x32) matches at scale 2.00
    with a score of 0.979, and `prop_office_chair_se.png` (30x41) matches at
    scale 2.00 as well. Tile and prop move together, so their RATIO — the thing
    that would show a per-node scale bug — is exactly what this module already
    produced. What differs is a single global factor.

    It comes from `window/stretch`: this project authors at 640x360 and
    presents at 1280x720, so every canvas item is drawn at 2x, uniformly.
    The viewport draws in WORLD units, where 100% means one world pixel per
    css pixel — which is the Godot editor's 100% and half of what the player
    sees. Reporting the factor lets the panel say which of the two a given zoom
    is, and offer the game's own scale in one click, instead of leaving the
    operator to conclude the art is wrong.

    Only `canvas_items` (and its old alias `2d`) scales the drawing; `viewport`
    mode renders small and blits, and `disabled` does nothing — neither changes
    the size of a canvas item relative to the window.
    """
    text = project_godot_text or ""
    mode = re.search(r'window/stretch/mode\s*=\s*"([^"]*)"', text)
    if not mode or mode.group(1) not in ("canvas_items", "2d"):
        return 1.0
    base = viewport_of(text)

    def override(key: str, fallback: int) -> int:
        m = re.search(rf"window/size/{key}\s*=\s*(\d+)", text)
        return int(m.group(1)) if m and int(m.group(1)) > 0 else fallback

    win_w = override("window_width_override", base[0])
    win_h = override("window_height_override", base[1])
    if not base[0] or not base[1]:
        return 1.0
    scale = min(win_w / base[0], win_h / base[1])
    integer = re.search(r'window/stretch/scale_mode\s*=\s*"([^"]*)"', text)
    if integer and integer.group(1) == "integer":
        scale = float(max(1, int(scale)))
    return round(scale, 4) if scale > 0 else 1.0


def viewport_of(project_godot_text: str) -> tuple[int, int]:
    """The game's own resolution, so the viewport frame is the real frame."""
    w = re.search(r"window/size/viewport_width\s*=\s*(\d+)",
                  project_godot_text or "")
    h = re.search(r"window/size/viewport_height\s*=\s*(\d+)",
                  project_godot_text or "")
    return (int(w.group(1)) if w else DEFAULT_VIEWPORT[0],
            int(h.group(1)) if h else DEFAULT_VIEWPORT[1])
