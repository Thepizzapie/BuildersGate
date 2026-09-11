"""What a 3D scene LOOKS like: every Node3D resolved to something drawable.

The 2D draw list (scenedraw.py) turns a .tscn into a flat, paint-ordered list a
canvas can composite. A 3D scene has no paint order and no canvas; it has a
tree of Node3Ds each carrying a Transform3D, a handful of ways to put a mesh on
one, and a camera. So this module walks the tree the way the engine does at
load, accumulates each node's world matrix through its ancestors, and resolves
what it draws into a shape a WebGL viewport can build directly: a box of this
size, a sphere of this radius, this .glb, this collision capsule, this light.

THREE THINGS THAT ARE EASY TO GET WRONG, pinned here:

  * THE TWELVE NUMBERS OF A Transform3D ARE ROW-MAJOR. `Transform3D(xx, xy, xz,
    yx, yy, yz, zx, zy, zz, ox, oy, oz)` is basis.rows[0], rows[1], rows[2],
    origin. Reading the first three as the X axis vector gives the transpose,
    which is the INVERSE rotation, and a scene whose every rotated prop faces
    the wrong way is the symptom. Measured on a hand-authored track kit.
  * `rotation` IS EULER YXZ IN RADIANS. Node3D's default rotation order is
    YXZ: the basis is Y(yaw) * X(pitch) * Z(roll). A scene the editor saved
    carries `transform =`; a scene an agent wrote carries `position`,
    `rotation` and `scale`, and both have to land in the same place.
  * A PRIMITIVE MESH IS A SUB-RESOURCE WITH DEFAULTS. `BoxMesh` with no `size`
    line is 1 x 1 x 1; CylinderMesh is radius 0.5, height 2; PlaneMesh is
    2 x 2 facing +Y. A viewport that only drew what the file spelt out would
    draw the graybox kit as nothing.

I/O-free by construction, like its 2D sibling: the caller supplies `read` for
res:// paths and `model_url_of` for the URL a browser can fetch a model from.
Matrices are 4 x 4 row-major lists; the viewport hands them to
three.js's Matrix4.set(), which takes row-major arguments.
"""
from __future__ import annotations

import copy
import math
import re
from typing import Callable, Optional

from . import scenewire

_VEC3_RE = re.compile(
    r"Vector3\(\s*(-?[\d.e+-]+)\s*,\s*(-?[\d.e+-]+)\s*,\s*(-?[\d.e+-]+)\s*\)")
_VEC2_RE = re.compile(r"Vector2\(\s*(-?[\d.e+-]+)\s*,\s*(-?[\d.e+-]+)\s*\)")
_XFORM_RE = re.compile(r"Transform3D\(([^)]*)\)")
_COLOR_RE = re.compile(
    r"Color\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*(?:,\s*(-?[\d.]+)\s*)?\)")
_EXT_ID_RE = re.compile(r'ExtResource\("([^"]+)"\)')
_SUB_ID_RE = re.compile(r'SubResource\("([^"]+)"\)')
_SUB_BLOCK_RE = re.compile(
    r'^\[sub_resource\s+type="(?P<type>[^"]+)"\s+id="(?P<id>[^"]+)"\s*\]',
    re.MULTILINE)
_BLOCK_START_RE = re.compile(r"^\[", re.MULTILINE)
_AABB_RE = re.compile(r'"aabb":\s*AABB\(([^)]*)\)')

IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]

#: Node classes whose transform is a Transform3D. Everything else in a 3D scene
#: (a Node, a CanvasLayer with the HUD, a Timer) sits at its parent's place.
SPATIAL_SUFFIXES = ("3D",)
SPATIAL_EXACT = {"Node3D", "WorldEnvironment", "Path3D", "PathFollow3D",
                 "RemoteTransform3D", "Skeleton3D", "BoneAttachment3D"}

#: Model formats an instance can point at directly. Godot imports each as a
#: PackedScene, so `instance=ExtResource("x")` on a .glb is an ordinary line.
MODEL_SUFFIXES = (".glb", ".gltf", ".obj", ".fbx", ".dae", ".blend")

_MAX_INSTANCE_DEPTH = 3


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------
def _num(value, default=0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bool(value, default=True) -> bool:
    text = str(value or "").strip().lower()
    if text in ("true", "false"):
        return text == "true"
    return default


def _vec3(value, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    got = _VEC3_RE.search(str(value or ""))
    if not got:
        return default
    return tuple(float(g) for g in got.groups())  # type: ignore[return-value]


def _vec2(value, default=(0.0, 0.0)) -> tuple[float, float]:
    got = _VEC2_RE.search(str(value or ""))
    if not got:
        return default
    return float(got.group(1)), float(got.group(2))


def _color(value, default=(1.0, 1.0, 1.0, 1.0)) -> tuple:
    got = _COLOR_RE.search(str(value or ""))
    if not got:
        return default
    r, g, b, a = got.groups()
    return (float(r), float(g), float(b), float(a) if a is not None else 1.0)


# ---------------------------------------------------------------------------
# Matrices: 4 x 4, row-major, column vectors (p' = M p)
# ---------------------------------------------------------------------------
def mat_mul(a: list, b: list) -> list:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]


def mat_from_trs(position, rotation_yxz, scale) -> list:
    """Godot's Node3D composition: T * R(YXZ) * S."""
    px, py, pz = position
    rx, ry, rz = rotation_yxz
    sx, sy, sz = scale
    cx, sxn = math.cos(rx), math.sin(rx)
    cy, syn = math.cos(ry), math.sin(ry)
    cz, szn = math.cos(rz), math.sin(rz)
    # Basis.from_euler(EULER_ORDER_YXZ) = Y * X * Z.
    ymat = [[cy, 0.0, syn], [0.0, 1.0, 0.0], [-syn, 0.0, cy]]
    xmat = [[1.0, 0.0, 0.0], [0.0, cx, -sxn], [0.0, sxn, cx]]
    zmat = [[cz, -szn, 0.0], [szn, cz, 0.0], [0.0, 0.0, 1.0]]

    def m3(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
                for i in range(3)]

    basis = m3(m3(ymat, xmat), zmat)
    scaled = [[basis[i][0] * sx, basis[i][1] * sy, basis[i][2] * sz]
              for i in range(3)]
    return [[*scaled[0], px], [*scaled[1], py], [*scaled[2], pz],
            [0.0, 0.0, 0.0, 1.0]]


def mat_from_transform3d(literal: str) -> Optional[list]:
    """The 12 numbers of a Transform3D literal as a 4 x 4. ROW-MAJOR basis."""
    got = _XFORM_RE.search(str(literal or ""))
    if not got:
        return None
    try:
        n = [float(x) for x in got.group(1).split(",")]
    except ValueError:
        return None
    if len(n) != 12:
        return None
    return [[n[0], n[1], n[2], n[9]],
            [n[3], n[4], n[5], n[10]],
            [n[6], n[7], n[8], n[11]],
            [0.0, 0.0, 0.0, 1.0]]


def local_matrix(props: dict) -> list:
    """This node's own transform, from whichever spelling the file uses.

    `transform =` wins when present: the editor writes it and it carries
    everything. Otherwise position / rotation / scale compose the way Node3D
    composes them, with Godot's defaults for whichever is missing.
    """
    if "transform" in props:
        got = mat_from_transform3d(props["transform"])
        if got is not None:
            return got
    return mat_from_trs(_vec3(props.get("position")),
                        _vec3(props.get("rotation")),
                        _vec3(props.get("scale"), (1.0, 1.0, 1.0)))


def decompose(m: list) -> dict:
    """position, scale and YXZ euler out of a matrix, for the inspector."""
    px, py, pz = m[0][3], m[1][3], m[2][3]
    cols = [[m[r][c] for r in range(3)] for c in range(3)]
    sx, sy, sz = (math.sqrt(sum(v * v for v in col)) or 1.0 for col in cols)
    r = [[m[0][0] / sx, m[0][1] / sy, m[0][2] / sz],
         [m[1][0] / sx, m[1][1] / sy, m[1][2] / sz],
         [m[2][0] / sx, m[2][1] / sy, m[2][2] / sz]]
    # Inverse of Y * X * Z: sin(x) = -r[1][2].
    sin_x = max(-1.0, min(1.0, -r[1][2]))
    x = math.asin(sin_x)
    if abs(sin_x) < 0.9999:
        y = math.atan2(r[0][2], r[2][2])
        z = math.atan2(r[1][0], r[1][1])
    else:                                   # gimbal lock: fold yaw into roll
        y = math.atan2(-r[2][0], r[0][0])
        z = 0.0
    return {"position": [round(px, 4), round(py, 4), round(pz, 4)],
            "rotation": [round(x, 5), round(y, 5), round(z, 5)],
            "scale": [round(sx, 4), round(sy, 4), round(sz, 4)]}


def is_spatial(node_type: str) -> bool:
    return node_type in SPATIAL_EXACT or node_type.endswith(SPATIAL_SUFFIXES) \
        or node_type.startswith("CSG")


def is_3d_scene(text: str) -> bool:
    """Does the root, or anything near it, live in 3D space?

    A 3D game's main scene is a Node3D; a 3D game's HUD scene is a Control,
    and is rightly drawn by the 2D viewport. Ask the root first and fall
    back to "any spatial node at all", which catches a Node root holding a
    world.
    """
    try:
        parsed = scenewire.parse(text)
    except scenewire.WireError:
        return False
    if not parsed["nodes"]:
        return False
    root = parsed["nodes"][0]["type"]
    if is_spatial(root):
        return True
    if root in ("Control", "CanvasLayer") or root.endswith("2D"):
        return False
    return any(is_spatial(n["type"]) for n in parsed["nodes"][1:])


# ---------------------------------------------------------------------------
# Sub-resources: the meshes, shapes and materials a scene defines inline
# ---------------------------------------------------------------------------
_SUB_CACHE: dict[tuple[int, int], dict] = {}

#: A property line longer than this is baked data (a PackedVector3Array of
#: mesh vertices runs to megabytes) and nothing here reads it. Skipping it
#: keeps a 75 MB scene from being copied into a dict of strings.
_MAX_PROP_LINE = 2048


def sub_resources(text: str) -> dict[str, dict]:
    """{id: {type, props}} for every [sub_resource] block in the file.

    Memoised on the text the same way scenewire.parse is, and for the same
    scene: one pass over the file per process, not one per render.
    """
    key = (len(text), hash(text))
    hit = _SUB_CACHE.get(key)
    if hit is not None:
        return hit
    out: dict[str, dict] = {}
    heads = list(_SUB_BLOCK_RE.finditer(text))
    for i, head in enumerate(heads):
        end = len(text)
        nxt = _BLOCK_START_RE.search(text, head.end())
        if nxt:
            end = nxt.start()
        props: dict[str, str] = {}
        start = head.end()
        while start < end:
            nl = text.find("\n", start, end)
            if nl < 0:
                nl = end
            if text.startswith("_surfaces", start):
                # AN ARRAYMESH'S GEOMETRY, left in the file and indexed. The
                # line is megabytes of base64; what a draw list needs from
                # it is the bounding box (cheap, read now) and where the
                # surfaces begin (so mesh_data can decode them on demand).
                props["_surfaces_span"] = (start, end)
                boxes = [[float(v) for v in m.split(",")]
                         for m in _AABB_RE.findall(text, start, end)]
                if boxes:
                    lo = [min(b[i] for b in boxes) for i in range(3)]
                    hi = [max(b[i] + b[i + 3] for b in boxes) for i in range(3)]
                    props["_aabb"] = lo + [hi[i] - lo[i] for i in range(3)]
                props["_surface_count"] = len(boxes)
                # The surfaces run to the end of the block, over many lines
                # (the editor writes one key per line); nothing after them
                # is a plain property, so stop here.
                break
            if nl - start <= _MAX_PROP_LINE:
                stripped = text[start:nl].strip()
                if stripped and not stripped.startswith(";") and "=" in stripped:
                    k, _, value = stripped.partition("=")
                    props[k.strip()] = value.strip()
            start = nl + 1
        out[head.group("id")] = {"type": head.group("type"), "props": props,
                                 "id": head.group("id")}
    if len(_SUB_CACHE) >= 64:
        _SUB_CACHE.pop(next(iter(_SUB_CACHE)))
    _SUB_CACHE[key] = out
    return out


def _material_color(material: Optional[dict]) -> Optional[list]:
    if not material:
        return None
    props = material.get("props", {})
    for key in ("albedo_color", "color"):
        if key in props:
            return list(_color(props[key]))
    return None


def _mesh_draw(mesh: Optional[dict], subs: dict) -> dict:
    """A primitive mesh sub-resource as a shape the viewport can build."""
    if not mesh:
        return {"kind": "marker", "reason": "no mesh assigned"}
    kind = mesh["type"]
    p = mesh.get("props", {})
    color = None
    mat_id = _SUB_ID_RE.search(p.get("material", ""))
    if mat_id:
        color = _material_color(subs.get(mat_id.group(1)))
    out: dict = {"mesh": kind}
    if kind == "BoxMesh":
        out.update(kind="box", size=list(_vec3(p.get("size"), (1.0, 1.0, 1.0))))
    elif kind == "SphereMesh":
        out.update(kind="sphere", radius=_num(p.get("radius"), 0.5),
                   height=_num(p.get("height"), 1.0))
    elif kind == "CylinderMesh":
        out.update(kind="cylinder", top_radius=_num(p.get("top_radius"), 0.5),
                   bottom_radius=_num(p.get("bottom_radius"), 0.5),
                   height=_num(p.get("height"), 2.0))
    elif kind == "CapsuleMesh":
        out.update(kind="capsule", radius=_num(p.get("radius"), 0.5),
                   height=_num(p.get("height"), 2.0))
    elif kind == "PlaneMesh":
        size = _vec2(p.get("size"), (2.0, 2.0))
        out.update(kind="plane", size=[size[0], size[1]],
                   orientation=int(_num(p.get("orientation"), 1)))
    elif kind == "QuadMesh":
        size = _vec2(p.get("size"), (1.0, 1.0))
        out.update(kind="plane", size=[size[0], size[1]], orientation=2)
    elif kind == "PrismMesh":
        out.update(kind="prism", size=list(_vec3(p.get("size"), (1.0, 1.0, 1.0))))
    elif kind == "TorusMesh":
        out.update(kind="torus", inner_radius=_num(p.get("inner_radius"), 0.5),
                   outer_radius=_num(p.get("outer_radius"), 1.0))
    elif kind == "ArrayMesh" and p.get("_aabb"):
        # Real geometry, decoded on demand by mesh_data() and fetched by the
        # viewport per mesh id; the box is what draws until it arrives.
        out.update(kind="arraymesh", id=mesh.get("id", ""), aabb=p["_aabb"],
                   surfaces=int(p.get("_surface_count") or 0))
    else:
        out.update(kind="mesh_unknown",
                   reason=f"{kind} has no inline geometry to draw; shown as "
                          "a unit box")
    if color:
        out["color"] = color
    return out


def _shape_draw(shape: Optional[dict]) -> dict:
    if not shape:
        return {"kind": "marker", "reason": "no shape assigned"}
    kind = shape["type"]
    p = shape.get("props", {})
    if kind == "BoxShape3D":
        return {"kind": "box", "shape": kind, "wire": True,
                "size": list(_vec3(p.get("size"), (1.0, 1.0, 1.0)))}
    if kind == "SphereShape3D":
        return {"kind": "sphere", "shape": kind, "wire": True,
                "radius": _num(p.get("radius"), 0.5)}
    if kind == "CapsuleShape3D":
        return {"kind": "capsule", "shape": kind, "wire": True,
                "radius": _num(p.get("radius"), 0.5),
                "height": _num(p.get("height"), 2.0)}
    if kind == "CylinderShape3D":
        return {"kind": "cylinder", "shape": kind, "wire": True,
                "top_radius": _num(p.get("radius"), 0.5),
                "bottom_radius": _num(p.get("radius"), 0.5),
                "height": _num(p.get("height"), 2.0)}
    return {"kind": "mesh_unknown", "shape": kind, "wire": True,
            "reason": f"{kind} carries its geometry as data; shown as a unit box"}


# ---------------------------------------------------------------------------
# What one node draws
# ---------------------------------------------------------------------------
def _draw_for(node: dict, props: dict, subs: dict, *, read, model_url_of,
              children: int) -> dict:
    ntype = node["type"]
    sub = lambda key: (subs.get(_SUB_ID_RE.search(props[key]).group(1))  # noqa: E731
                       if key in props and _SUB_ID_RE.search(props[key]) else None)

    if ntype == "MeshInstance3D":
        draw = _mesh_draw(sub("mesh"), subs)
        override = sub("surface_material_override/0")
        color = _material_color(override)
        if color:
            draw["color"] = color
        if "mesh" in props and _EXT_ID_RE.search(props["mesh"]):
            ext = next((r for r in node["resources"] if r["property"] == "mesh"),
                       None)
            path = ext["path"] if ext else ""
            draw = {"kind": "mesh_unknown", "mesh": "external", "path": path,
                    "reason": f"{path.rsplit('/', 1)[-1]} is a mesh resource "
                              "file; shown as a unit box"}
        return draw
    if ntype in ("CSGBox3D", "CSGSphere3D", "CSGCylinder3D", "CSGTorus3D",
                 "CSGMesh3D", "CSGPolygon3D", "CSGCombiner3D"):
        color = _material_color(sub("material"))
        if ntype == "CSGBox3D":
            out = {"kind": "box", "size": list(_vec3(props.get("size"), (1.0, 1.0, 1.0)))}
        elif ntype == "CSGSphere3D":
            out = {"kind": "sphere", "radius": _num(props.get("radius"), 0.5)}
        elif ntype == "CSGCylinder3D":
            r = _num(props.get("radius"), 0.5)
            out = {"kind": "cylinder", "top_radius": r, "bottom_radius": r,
                   "height": _num(props.get("height"), 2.0)}
        elif ntype == "CSGMesh3D":
            out = _mesh_draw(sub("mesh"), subs)
        else:
            out = {"kind": "mesh_unknown", "reason": f"{ntype} shown as a unit box"}
        out["csg"] = True
        if color:
            out["color"] = color
        return out
    if ntype in ("CollisionShape3D",):
        return _shape_draw(sub("shape"))
    if ntype == "CollisionPolygon3D":
        return {"kind": "mesh_unknown", "wire": True,
                "reason": "a polygon collider; shown as a unit box"}
    if ntype == "Camera3D":
        return {"kind": "camera", "fov": _num(props.get("fov"), 75.0),
                "current": _bool(props.get("current", "false"), False),
                "near": _num(props.get("near"), 0.05),
                "far": _num(props.get("far"), 4000.0)}
    if ntype == "DirectionalLight3D":
        return {"kind": "light", "light": "directional",
                "color": list(_color(props.get("light_color"))),
                "energy": _num(props.get("light_energy"), 1.0),
                "shadow": _bool(props.get("shadow_enabled", "false"), False)}
    if ntype == "OmniLight3D":
        return {"kind": "light", "light": "omni",
                "color": list(_color(props.get("light_color"))),
                "energy": _num(props.get("light_energy"), 1.0),
                "range": _num(props.get("omni_range"), 5.0)}
    if ntype == "SpotLight3D":
        return {"kind": "light", "light": "spot",
                "color": list(_color(props.get("light_color"))),
                "energy": _num(props.get("light_energy"), 1.0),
                "range": _num(props.get("spot_range"), 5.0),
                "angle": _num(props.get("spot_angle"), 45.0)}
    if ntype == "WorldEnvironment":
        env = sub("environment")
        bg = None
        if env:
            bg = _material_color({"props": {"color": env["props"].get(
                "background_color", "")}}) if "background_color" in env["props"] else None
        return {"kind": "environment", "background": bg,
                "sky": bool(env and "sky" in env["props"])}
    if ntype == "Sprite3D":
        ext = next((r for r in node["resources"] if r["property"] == "texture"), None)
        rel = model_url_of(ext["path"], kind="image") if ext else None
        return {"kind": "sprite", "rel": rel,
                "pixel_size": _num(props.get("pixel_size"), 0.01),
                "billboard": int(_num(props.get("billboard"), 0))}
    if ntype == "Label3D":
        return {"kind": "label", "text": props.get("text", '""').strip('"')}
    if ntype in ("Marker3D", "Path3D", "PathFollow3D", "RemoteTransform3D",
                 "BoneAttachment3D", "Skeleton3D"):
        return {"kind": "marker", "reason": ntype}
    if ntype.startswith("GPUParticles3D") or ntype.startswith("CPUParticles3D"):
        return {"kind": "marker", "reason": "particles"}
    if ntype in ("Area3D", "StaticBody3D", "RigidBody3D", "CharacterBody3D",
                 "AnimatableBody3D", "VehicleBody3D", "NavigationRegion3D"):
        return {"kind": "group", "reason": ntype, "children": children}
    if ntype == "Node3D":
        return {"kind": "group", "reason": "container", "children": children}
    if node.get("instance"):
        return {"kind": "marker", "reason": "instance"}
    return {"kind": "none", "reason": ntype or "no type"}


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------
def _overrides(nodes: list[dict]) -> tuple[dict, set[str]]:
    """Same split as the 2D walk: patches onto instanced scenes vs nodes."""
    instances = {n["path"] for n in nodes if n["instance"]}
    if not instances:
        return {}, set()
    out: dict[str, dict[str, dict]] = {}
    skip: set[str] = set()
    for node in nodes:
        if node["type"]:
            continue
        parts = node["path"].split("/")
        host = None
        for i in range(len(parts) - 1, 0, -1):
            cand = "/".join(parts[:i])
            if cand in instances:
                host = cand
                break
        if host is None:
            continue
        out.setdefault(host, {})[node["path"][len(host) + 1:]] = node
        skip.add(node["path"])
    return out, skip


def _apply_patch(nodes: list[dict], patch: dict) -> None:
    for node in nodes:
        over = patch.get(node["path"])
        if not over:
            continue
        node["properties"] = {**node["properties"], **over["properties"]}
        merged = {r["property"]: r for r in node["resources"]}
        for res in over["resources"]:
            merged[res["property"]] = res
        node["resources"] = list(merged.values())


def _walk(scene_text: str, *, read, model_url_of, stack: tuple[str, ...] = (),
          patch: Optional[dict] = None, base_matrix: Optional[list] = None,
          prefix: str = "", visible_base: bool = True,
          scene_path: str = "") -> list[dict]:
    nodes = scenewire.outline(scene_text)
    if patch:
        _apply_patch(nodes, patch)
    overrides, skip = _overrides(nodes)
    subs = sub_resources(scene_text)
    world: dict[str, list] = {}
    visible_of: dict[str, bool] = {}
    kids: dict[str, int] = {}
    for node in nodes:
        parent = node["parent"]
        if parent is not None and node["path"] not in skip:
            kids[parent] = kids.get(parent, 0) + 1
    items: list[dict] = []
    for order, node in enumerate(nodes):
        if node["path"] in skip:
            continue
        props = node["properties"]
        parent = node["parent"]
        parent_key = "." if parent is None else (parent if parent != "." else ".")
        base = base_matrix or IDENTITY
        if parent is not None:
            base = world.get(parent_key, base_matrix or IDENTITY)
        # An instance carries no type in the host file but it is placed like
        # a Node3D: its `transform` or `position` line is the whole reason it
        # is in the scene.
        spatial = is_spatial(node["type"]) or bool(node["instance"])
        local = local_matrix(props) if spatial else IDENTITY
        m = mat_mul(base, local)
        world[node["path"]] = m

        visible = _bool(props.get("visible", "true"))
        if parent is not None and not visible_of.get(parent_key, True):
            visible = False
        if not visible_base:
            visible = False
        visible_of[node["path"]] = visible

        draw = _draw_for(node, props, subs, read=read, model_url_of=model_url_of,
                         children=kids.get(node["path"], 0))
        if draw.get("kind") == "arraymesh":
            # The id is only meaningful inside the file that defines it, and
            # an instanced scene's meshes live in THAT file, not the host's.
            draw["scene"] = scene_path
        item = {
            "path": (prefix + "/" + node["path"]) if prefix and node["path"] != "."
                    else (prefix or node["path"]),
            "own_path": node["path"],
            "name": node["name"], "type": node["type"], "role": node["role"],
            "order": order, "script": node.get("script", ""),
            "children": kids.get(node["path"], 0),
            "spatial": spatial,
            "world": [[round(v, 5) for v in row] for row in m],
            "local": decompose(local) if spatial else None,
            # Which spelling the file uses, so a staged move writes back in
            # the same one: `transform =` as one line, or position / rotation
            # / scale as the lines that changed. Godot reads both, in order,
            # and a file carrying both is a file whose second line silently
            # wins.
            "has_transform": "transform" in props,
            "visible": visible,
            "draw": draw,
            "of": prefix or None,
        }
        items.append(item)
        if node["instance"]:
            items.extend(_open_instance(item, node, m, read=read,
                                        model_url_of=model_url_of, stack=stack,
                                        patch=overrides.get(node["path"]),
                                        visible=visible))
    return items


def _open_instance(host: dict, node: dict, m: list, *, read, model_url_of,
                   stack: tuple[str, ...], patch: Optional[dict],
                   visible: bool) -> list[dict]:
    res_path = next((r["path"] for r in node["resources"]
                     if r["property"] == "instance"), "")
    name = res_path.rsplit("/", 1)[-1] or "a scene"
    host["instance"] = res_path
    if not res_path:
        host["draw"] = {"kind": "marker", "reason": "instance of nothing"}
        return []
    low = res_path.lower()
    if low.endswith(MODEL_SUFFIXES):
        # A model file IS the instance: Godot imports it as a PackedScene whose
        # root is the model's own node tree. The browser loads the same bytes.
        url = model_url_of(res_path, kind="model")
        host["draw"] = ({"kind": "model", "url": url, "path": res_path}
                        if url else
                        {"kind": "mesh_unknown", "path": res_path,
                         "reason": f"{name} could not be located for the viewport"})
        return []
    if not low.endswith(".tscn"):
        host["draw"] = {"kind": "marker", "reason": f"instance of {name}"}
        return []
    if res_path in stack:
        host["draw"] = {"kind": "marker", "reason": f"{name} instances itself"}
        return []
    if len(stack) >= _MAX_INSTANCE_DEPTH:
        host["draw"] = {"kind": "marker",
                        "reason": f"instance of {name}, nested too deep to draw"}
        return []
    text = read(res_path)
    if not text:
        host["draw"] = {"kind": "marker", "reason": f"{name} could not be read"}
        return []
    try:
        inner = _walk(text, read=read, model_url_of=model_url_of,
                      stack=stack + (res_path,), patch=copy.deepcopy(patch),
                      base_matrix=m, prefix=host["path"], visible_base=visible,
                      scene_path=res_path)
    except scenewire.WireError as exc:
        host["draw"] = {"kind": "marker", "reason": f"{name}: {exc}"}
        return []
    # The instanced root sits exactly where the host does; its own draw is the
    # host's picture, and the rest ride along as `of` entries the viewport
    # shows but does not edit. Godot selects the instance, not its insides.
    if inner:
        root = inner[0]
        if root["draw"].get("kind") not in ("group", "none", "marker"):
            host["draw"] = root["draw"]
        elif host["draw"].get("kind") in (None, "marker", "none"):
            host["draw"] = {"kind": "group", "reason": f"instance of {name}",
                            "children": len(inner) - 1}
        for entry in inner:
            entry["of"] = host["path"]
            entry["editable"] = False
        return inner[1:]
    return []


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
def _bounds(items: list[dict]) -> Optional[dict]:
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    seen = False
    for item in items:
        if not item.get("spatial"):
            continue
        d = item["draw"]
        half = None
        if d["kind"] == "box" or d["kind"] == "prism":
            half = [s / 2 for s in d["size"]]
        elif d["kind"] == "sphere":
            half = [d["radius"]] * 3
        elif d["kind"] in ("cylinder", "capsule"):
            r = max(d.get("top_radius", 0), d.get("bottom_radius", 0), d.get("radius", 0))
            half = [r, d["height"] / 2, r]
        elif d["kind"] == "plane":
            half = [d["size"][0] / 2, 0.01, d["size"][1] / 2]
        elif d["kind"] == "arraymesh":
            a = d["aabb"]
            # Not centred on the node: the box is offset by its own origin.
            centre = [a[i] + a[i + 3] / 2 for i in range(3)]
            half = [a[i + 3] / 2 for i in range(3)]
            m = item["world"]
            for sx in (-1, 1):
                for sy in (-1, 1):
                    for sz in (-1, 1):
                        p = (centre[0] + sx * half[0], centre[1] + sy * half[1],
                             centre[2] + sz * half[2])
                        for axis in range(3):
                            v = (m[axis][0] * p[0] + m[axis][1] * p[1]
                                 + m[axis][2] * p[2] + m[axis][3])
                            lo[axis] = min(lo[axis], v)
                            hi[axis] = max(hi[axis], v)
                            seen = True
            continue
        elif d["kind"] in ("mesh_unknown", "model", "camera", "light", "marker",
                           "sprite", "label", "group"):
            half = [0.5, 0.5, 0.5]
        if half is None:
            continue
        m = item["world"]
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    p = (sx * half[0], sy * half[1], sz * half[2])
                    for axis in range(3):
                        v = (m[axis][0] * p[0] + m[axis][1] * p[1]
                             + m[axis][2] * p[2] + m[axis][3])
                        lo[axis] = min(lo[axis], v)
                        hi[axis] = max(hi[axis], v)
                        seen = True
    if not seen:
        return None
    return {"min": [round(v, 3) for v in lo], "max": [round(v, 3) for v in hi]}


def _size_unknown_meshes(items: list[dict]) -> None:
    """Give a mesh nothing here can read the footprint of its sibling collider.

    A MeshInstance3D whose mesh is an external .res or .mesh draws as a unit
    box, which on a street of buildings is a field of one-metre cubes where
    the blocks should be. The body it sits under almost always carries a
    CollisionShape3D of the same footprint, so the placeholder takes that
    size and the scene reads at the right scale. Marked `sized_by`, so the
    viewport can say it is a guess.
    """
    by_parent: dict[Optional[str], list[dict]] = {}
    for item in items:
        parent = item["path"].rsplit("/", 1)[0] if "/" in item["path"] else "."
        by_parent.setdefault(parent, []).append(item)
    for item in items:
        d = item["draw"]
        if d.get("kind") != "mesh_unknown" or d.get("wire"):
            continue
        parent = item["path"].rsplit("/", 1)[0] if "/" in item["path"] else "."
        for sib in by_parent.get(parent, []):
            sd = sib["draw"]
            if sib is item or not sd.get("wire") or sd.get("kind") not in ("box", "cylinder", "capsule", "sphere"):
                continue
            hint = {k: v for k, v in sd.items() if k not in ("wire", "shape")}
            hint["reason"] = d.get("reason", "")
            hint["sized_by"] = sib["path"]
            hint["placeholder"] = True
            item["draw"] = hint
            break


def draw_list(scene_text: str, *, read: Callable[[str], Optional[str]],
              model_url_of: Callable[..., Optional[str]],
              scene_path: str = "") -> dict:
    """Every node with its world matrix and what it draws, plus the camera.

    `model_url_of(res_path, kind=...)` returns the URL a browser can fetch a
    model or image from, or None when the file is not there. `scene_path` is
    the res:// path of this scene, stamped on every inline mesh so the
    viewport asks the right file for its bytes.
    """
    items = _walk(scene_text, read=read, model_url_of=model_url_of,
                  scene_path=scene_path)
    _size_unknown_meshes(items)
    cameras = [i for i in items if i["draw"].get("kind") == "camera"]
    current = next((c for c in cameras if c["draw"].get("current")), None) \
        or (cameras[0] if cameras else None)
    lights = [i for i in items if i["draw"].get("kind") == "light"]
    roles: dict[str, int] = {}
    for item in items:
        roles[item["role"]] = roles.get(item["role"], 0) + 1
    return {
        "dimension": "3d",
        "items": items,
        "bounds": _bounds(items),
        "camera": ({"path": current["path"], "world": current["world"],
                    "fov": current["draw"]["fov"]} if current else None),
        "lights": len(lights),
        "roles": roles,
        "root": items[0]["path"] if items else None,
        "root_type": items[0]["type"] if items else "",
    }


# ---------------------------------------------------------------------------
# ArrayMesh geometry, decoded from the text the file already carries
# ---------------------------------------------------------------------------
# Godot 4 serialises an ArrayMesh's surfaces as base64 PackedByteArrays with a
# `format` bitfield. The two layouts a saved scene can hold:
#   * COMPRESS_ATTRIBUTES (1 << 29) set: a position is three uint16, each a
#     0..65535 fraction of the surface's AABB, padded to 8 bytes;
#   * unset: three float32.
# Either way the positions are a PLANAR block at the start of vertex_data,
# count entries long; normals and tangents follow in their own blocks.
# Indices are uint16 while the surface has at most 65536 vertices, uint32
# otherwise. Only triangle lists (primitive 3) are decoded; a strip or a line
# surface is skipped rather than drawn wrong. Verified against a real 75 MB
# scene: the decoded extents match the AABB the file states to the metre.
_SURFACE_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)
_KEY_RE = re.compile(r'"(\w+)":\s*(-?\d+)')
_BYTES_RE = re.compile(r'"(\w+)":\s*PackedByteArray\("([^"]*)"\)')
FLAG_COMPRESS_ATTRIBUTES = 1 << 29
_MESH_CACHE: dict[tuple[int, int, str], dict] = {}


def mesh_data(text: str, mesh_id: str) -> Optional[dict]:
    """{positions: bytes(float32 xyz), indices: bytes(uint32), vertex_count,
    triangle_count, aabb} for one ArrayMesh sub-resource, or None."""
    import base64
    import struct

    key = (len(text), hash(text), mesh_id)
    hit = _MESH_CACHE.get(key)
    if hit is not None:
        return hit
    sub = sub_resources(text).get(mesh_id)
    if not sub or sub["type"] != "ArrayMesh" or "_surfaces_span" not in sub["props"]:
        return None
    start, end = sub["props"]["_surfaces_span"]
    positions = bytearray()
    indices: list[int] = []
    base = 0
    for surface in _SURFACE_RE.finditer(text, start, end):
        body = surface.group(0)
        nums = {k: int(v) for k, v in _KEY_RE.findall(body)}
        blobs = {k: v for k, v in _BYTES_RE.findall(body)}
        if nums.get("primitive", 3) != 3 or "vertex_data" not in blobs:
            continue
        aabb_m = _AABB_RE.search(body)
        aabb = ([float(v) for v in aabb_m.group(1).split(",")] if aabb_m
                else [0.0, 0.0, 0.0, 1.0, 1.0, 1.0])
        fmt = nums.get("format", 0)
        count = nums.get("vertex_count", 0)
        try:
            raw = base64.b64decode(blobs["vertex_data"])
        except ValueError:
            continue
        if not count:
            continue
        # PLANAR, NOT INTERLEAVED. The position block comes first and is
        # count x 8 bytes (compressed: 3 x uint16 + 1 padding uint16) or
        # count x 12 (float32 x 3); normals and tangents follow as their own
        # blocks. Measured: reading an interleaved stride of 20 on a mesh
        # with normals and tangents put 526 NaNs and a 3e38 in a bench.
        if fmt & FLAG_COMPRESS_ATTRIBUTES:
            if len(raw) < count * 8:
                continue
            for n in range(count):
                a, b, c = struct.unpack_from("<3H", raw, n * 8)
                positions += struct.pack("<3f", aabb[0] + a / 65535 * aabb[3],
                                         aabb[1] + b / 65535 * aabb[4],
                                         aabb[2] + c / 65535 * aabb[5])
        else:
            if len(raw) < count * 12:
                continue
            positions += raw[:count * 12]
        if "index_data" in blobs:
            try:
                idx = base64.b64decode(blobs["index_data"])
            except ValueError:
                idx = b""
            width = "H" if count <= 65536 else "I"
            size = 2 if width == "H" else 4
            n_idx = len(idx) // size
            indices.extend(base + v for v in struct.unpack_from(f"<{n_idx}{width}", idx))
        else:
            indices.extend(range(base, base + count))
        base += count
    if not positions:
        return None
    out = {"positions": bytes(positions),
           "indices": struct.pack(f"<{len(indices)}I", *indices),
           "vertex_count": base, "triangle_count": len(indices) // 3,
           "aabb": sub["props"].get("_aabb")}
    if len(_MESH_CACHE) >= 256:
        _MESH_CACHE.pop(next(iter(_MESH_CACHE)))
    _MESH_CACHE[key] = out
    return out
