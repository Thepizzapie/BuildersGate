"""Surface tools: continuous forms and real materials for generated 3D assets.

Each function loads a model (.glb/.gltf/.blend), runs one operation from the
surface kit (_blender_surface_kit, the same source an agent has inside
blender_run), and re-exports. The bake and decal passes are their own scripts
because they are not something an agent should improvise: a Cycles bake has a
dozen settings that each silently produce a black or a blank map when wrong.

Why (measured on Meridian, 2026-09-05): a tree of octagonal prisms shoved
through one another, a 116k-triangle car with eleven flat colours and no
textures, panel lines as geometry hovering over the paint. The kit had
bg_box, bg_cyl and join, so that is what the agents built with.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import blender as _blender
from ._blender_surface_kit import SURFACE_EXAMPLE  # noqa: F401  (re-exported for docs)

MARK = "BGATE_SURFACE:"

MATERIAL_PRESETS = {
    "car_paint": "glossy clearcoat paint with a fine metallic flake",
    "rubber": "matte black rubber with a fine grain",
    "brushed_metal": "anisotropic brushed steel",
    "chrome": "mirror chrome",
    "painted_metal_worn": "painted metal, paint worn to bare metal on edges (wear=0..1)",
    "plastic": "moulded plastic, slight sheen",
    "glass": "clear glass (transmission)",
    "bark": "tree bark, ridged",
    "wood": "planked wood grain",
    "concrete": "poured concrete, pitted",
    "emissive": "a light or screen (colour glows)",
}

BAKE_MAPS = ("albedo", "roughness", "normal", "ao")

_LOAD = r'''
import json, os
P = json.loads(r"""__PAYLOAD__""")
bg_wipe()
_before = set(bpy.data.objects.keys())
_path = P["model"]
if _path.lower().endswith(".blend"):
    with bpy.data.libraries.load(_path, link=False) as (_src, _dst):
        _dst.objects = list(_src.objects)
    for _o in _dst.objects:
        if _o is not None:
            bpy.context.scene.collection.objects.link(_o)
else:
    bpy.ops.import_scene.gltf(filepath=_path)
IMPORTED = [bpy.data.objects[n] for n in sorted(set(bpy.data.objects.keys()) - _before)]
MESHES = [o for o in IMPORTED if o.type == "MESH"]
# The importer's bone-shape widget is not part of any asset (see icosphere note).
for _o in list(MESHES):
    if _o.name.lower().startswith("icosphere") and not _o.data.materials:
        bpy.data.objects.remove(_o, do_unlink=True); MESHES.remove(_o)


def _pick(names):
    if not names:
        return list(MESHES)
    wanted = set(names)
    return [o for o in MESHES if o.name in wanted or o.data.name in wanted]


def _report(row):
    for o in bpy.context.scene.objects:
        if o.type == "MESH":
            o.data.calc_loop_triangles()
    row["objects"] = [{"name": o.name, "tris": len(o.data.loop_triangles),
                       "materials": [m.name for m in o.data.materials if m],
                       "uv": len(o.data.uv_layers)}
                      for o in bpy.context.scene.objects if o.type == "MESH"]
    _dump(row)


def _dump(row):
    # To a file, not stdout: run_script keeps 4000 characters of stdout and a
    # report with absolute paths in it loses its mark (see surface._run).
    with open(P["report_path"], "w", encoding="utf-8") as handle:
        json.dump(row, handle)
    print("__MARK__" + json.dumps({"ok": bool(row.get("ok")), "dumped": True}))
'''.replace("__MARK__", MARK)

_FUSE = _LOAD + r'''
parts = _pick(P.get("objects") or [])
if len(parts) < 1:
    _report({"ok": False, "error": "no mesh objects to fuse (names: %s)" % [o.name for o in MESHES]})
else:
    before = sum(len(o.data.polygons) for o in parts)
    fused = bg_fuse(parts, P.get("name") or "Fused", voxel=P.get("voxel") or 0.0,
                    smooth=int(P.get("smooth") or 0), target_tris=int(P.get("target_tris") or 0),
                    angle=float(P.get("angle") or 30.0))
    fused.data.calc_loop_triangles()
    _report({"ok": True, "fused": fused.name, "parts": len(parts), "faces_before": before,
             "tris_after": len(fused.data.loop_triangles),
             "materials": [m.name for m in fused.data.materials if m]})
'''

_SHADE = _LOAD + r'''
parts = _pick(P.get("objects") or [])
done = []
for o in parts:
    dims = bg_bounds(o)["dims"]
    bevel = P.get("bevel")
    if bevel is None:
        bevel = max(dims) * 0.005 if P.get("auto_bevel", True) else 0.0
    bg_shade(o, bevel=float(bevel), angle=float(P.get("angle") or 30.0),
             segments=int(P.get("segments") or 2), weighted=bool(P.get("weighted", True)))
    done.append({"name": o.name, "bevel": float(bevel)})
_report({"ok": bool(done), "shaded": done, "error": "" if done else "no mesh objects matched"})
'''

_DUMP = r'''
def _dump(row):
    with open(P["report_path"], "w", encoding="utf-8") as handle:
        json.dump(row, handle)
    print("__MARK__" + json.dumps({"ok": bool(row.get("ok")), "dumped": True}))
'''.replace("__MARK__", MARK)

_LATHE = r'''
import json
P = json.loads(r"""__PAYLOAD__""")
''' + _DUMP + r'''
bg_wipe()
mat = None
if P.get("preset"):
    mat = bg_material(P.get("material_name") or (P["name"] + "Mat"), P["preset"], P.get("colour"),
                      scale=float(P.get("scale") or 1.0))
obj = bg_lathe(P["name"], P["profile"], segments=int(P.get("segments") or 32), material=mat)
if P.get("bevel"):
    bg_shade(obj, bevel=float(P["bevel"]), angle=float(P.get("angle") or 40.0))
obj.data.calc_loop_triangles()
_dump({"ok": True, "name": obj.name, "tris": len(obj.data.loop_triangles),
       "dims": list(bg_bounds(obj)["dims"]),
       "materials": [m.name for m in obj.data.materials if m]})
'''

_LOFT = r'''
import json
P = json.loads(r"""__PAYLOAD__""")
''' + _DUMP + r'''
bg_wipe()
mat = None
if P.get("preset"):
    mat = bg_material(P.get("material_name") or (P["name"] + "Mat"), P["preset"], P.get("colour"),
                      scale=float(P.get("scale") or 1.0))
obj = bg_loft(P["name"], P["sections"], closed=bool(P.get("closed", True)), caps=bool(P.get("caps", True)),
              smooth=int(P.get("smooth") or 0), mirror_x=bool(P.get("mirror_x", False)), material=mat)
if P.get("bevel"):
    bg_shade(obj, bevel=float(P["bevel"]), angle=float(P.get("angle") or 45.0))
obj.data.calc_loop_triangles()
_dump({"ok": True, "name": obj.name, "tris": len(obj.data.loop_triangles),
       "dims": list(bg_bounds(obj)["dims"]),
       "materials": [m.name for m in obj.data.materials if m]})
'''

_ASSIGN = r'''
def _assign_presets(rows):
    applied, missing = [], []
    for row in rows:
        target = row.get("target") or ""
        preset = row["preset"]
        name = row.get("name") or ("%s_%s" % (target or "all", preset))
        mat = bg_material(name, preset, row.get("colour"), scale=float(row.get("scale") or 1.0),
                          roughness=row.get("roughness"), wear=float(row.get("wear") or 0.0),
                          strength=float(row.get("strength") or 1.0))
        hit = 0
        for o in MESHES:
            slots = o.data.materials
            if target in ("", "*") or o.name == target or o.data.name == target:
                if len(slots) == 0:
                    slots.append(mat); hit += 1
                for i in range(len(slots)):
                    slots[i] = mat; hit += 1
                continue
            for i, m in enumerate(slots):
                if m is not None and m.name == target:
                    slots[i] = mat; hit += 1
        applied.append({"target": target, "preset": preset, "material": mat.name, "slots": hit})
        if hit == 0:
            missing.append(target)
    return applied, missing
'''

_MATERIAL = _LOAD + _ASSIGN + r'''
applied, missing = _assign_presets(P["assign"])
# A .blend beside the glb keeps the node graphs a glTF cannot: it is what a
# later blender_bake should load, and what blender_run can open to edit.
sidecar = P.get("sidecar") or ""
if sidecar:
    bpy.ops.wm.save_as_mainfile(filepath=sidecar, copy=True)
_report({"ok": not missing, "applied": applied, "missing": missing, "sidecar": sidecar,
         "error": ("no object or material slot named %s" % missing) if missing else ""})
'''

_BAKE = _LOAD + _ASSIGN + r'''
import math
assigned, assign_missing = _assign_presets(P.get("assign") or [])
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = int(P.get("samples") or 16)
scene.cycles.use_denoising = False
try:
    scene.render.bake.margin = int(P.get("margin") or 8)
except Exception:
    pass
res = int(P.get("resolution") or 1024)
out_dir = P["textures_dir"]
os.makedirs(out_dir, exist_ok=True)
parts = _pick(P.get("objects") or [])
wanted = list(P.get("maps") or ["albedo", "roughness", "normal", "ao"])
baked = []
errors = []

def _ensure_uv(o):
    if not o.data.uv_layers:
        bg_unwrap(o)
        return True
    return False

def _metallic_of(mats):
    vals = []
    for m in mats:
        if m and m.use_nodes:
            b = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if b is not None and not b.inputs["Metallic"].is_linked:
                vals.append(float(b.inputs["Metallic"].default_value))
    return sum(vals) / len(vals) if vals else 0.0

for o in parts:
    unwrapped = _ensure_uv(o)
    mats = [m for m in o.data.materials if m is not None]
    if not mats:
        mats = [bg_material(o.name + "_plastic", "plastic", (0.6, 0.6, 0.6))]
        o.data.materials.append(mats[0])
    metallic = _metallic_of(mats)
    images = {}
    nodes_added = []
    for kind in wanted:
        img = bpy.data.images.new("%s_%s" % (o.name, kind), res, res, alpha=False,
                                  float_buffer=False)
        img.colorspace_settings.name = "sRGB" if kind == "albedo" else "Non-Color"
        images[kind] = img
    # One active image node per material, swapped per pass.
    for m in mats:
        m.use_nodes = True
        node = m.node_tree.nodes.new("ShaderNodeTexImage")
        node.name = "BGateBakeTarget"
        m.node_tree.nodes.active = node
        nodes_added.append((m, node))
    _bg_active(o)
    for kind in wanted:
        for m, node in nodes_added:
            node.image = images[kind]
            m.node_tree.nodes.active = node
        try:
            if kind == "albedo":
                bpy.ops.object.bake(type="DIFFUSE", pass_filter={"COLOR"}, use_clear=True,
                                    margin=int(P.get("margin") or 8))
            elif kind == "roughness":
                bpy.ops.object.bake(type="ROUGHNESS", use_clear=True, margin=int(P.get("margin") or 8))
            elif kind == "normal":
                bpy.ops.object.bake(type="NORMAL", normal_space="TANGENT", use_clear=True,
                                    margin=int(P.get("margin") or 8))
            elif kind == "ao":
                bpy.ops.object.bake(type="AO", use_clear=True, margin=int(P.get("margin") or 8))
        except Exception as exc:
            errors.append("%s/%s: %s" % (o.name, kind, exc))
    for m, node in nodes_added:
        m.node_tree.nodes.remove(node)
    # AO into the albedo, so the file carries the shading a glTF viewer will
    # not compute (no separate occlusion channel to rely on).
    if "ao" in images and "albedo" in images and float(P.get("ao_strength", 0.6)) > 0:
        try:
            import numpy as np
            a = np.array(images["albedo"].pixels[:], dtype=np.float32).reshape(-1, 4)
            ao = np.array(images["ao"].pixels[:], dtype=np.float32).reshape(-1, 4)
            k = float(P.get("ao_strength", 0.6))
            occl = 1.0 - k * (1.0 - ao[:, :3])
            a[:, :3] = np.clip(a[:, :3] * occl, 0.0, 1.0)
            images["albedo"].pixels.foreach_set(a.reshape(-1))
        except Exception as exc:
            errors.append("%s/ao-multiply: %s" % (o.name, exc))
    files = {}
    for kind, img in images.items():
        path = os.path.join(out_dir, "%s_%s.png" % (o.name, kind))
        img.filepath_raw = path
        img.file_format = "PNG"
        img.save()
        files[kind] = path
    # The baked material replaces every slot: image maps into the Principled
    # inputs, which is exactly what the glTF exporter carries.
    baked_mat = bpy.data.materials.new(o.name + "_baked")
    baked_mat.use_nodes = True
    tree = baked_mat.node_tree
    tree.nodes.clear()
    out = tree.nodes.new("ShaderNodeOutputMaterial"); out.location = (600, 0)
    bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (300, 0)
    tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    bsdf.inputs["Metallic"].default_value = metallic
    if "albedo" in images:
        t = tree.nodes.new("ShaderNodeTexImage"); t.image = images["albedo"]; t.location = (-300, 300)
        tree.links.new(t.outputs["Color"], bsdf.inputs["Base Color"])
    if "roughness" in images:
        t = tree.nodes.new("ShaderNodeTexImage"); t.image = images["roughness"]; t.location = (-300, 0)
        t.image.colorspace_settings.name = "Non-Color"
        tree.links.new(t.outputs["Color"], bsdf.inputs["Roughness"])
    if "normal" in images:
        t = tree.nodes.new("ShaderNodeTexImage"); t.image = images["normal"]; t.location = (-300, -300)
        t.image.colorspace_settings.name = "Non-Color"
        nm = tree.nodes.new("ShaderNodeNormalMap"); nm.location = (0, -300)
        tree.links.new(t.outputs["Color"], nm.inputs["Color"])
        tree.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    o.data.materials.clear()
    o.data.materials.append(baked_mat)
    baked.append({"name": o.name, "maps": files, "unwrapped": unwrapped, "metallic": metallic,
                  "resolution": res})
if assign_missing:
    errors.append("assign: no object or material slot named %s" % assign_missing)
_report({"ok": bool(baked) and not errors, "baked": baked, "errors": errors, "assigned": assigned})
'''

_DECAL = _LOAD + r'''
import math
from mathutils import Vector, Quaternion
placed = []
errors = []
by_name = {o.name: o for o in MESHES}
by_name.update({o.data.name: o for o in MESHES})
for i, d in enumerate(P["decals"]):
    target = by_name.get(d.get("target") or "")
    if target is None and MESHES:
        target = MESHES[0]
    if target is None:
        errors.append("decal %d: no target mesh" % i); continue
    pos = Vector(d["position"])
    normal = Vector(d.get("normal") or (0, 0, 1)).normalized()
    size = d.get("size") or [0.2, 0.2]
    name = d.get("name") or ("Decal_%02d" % (i + 1))
    bpy.ops.mesh.primitive_plane_add(size=1.0, location=pos + normal * float(d.get("offset", 0.05)))
    plane = bpy.context.active_object
    plane.name = name
    plane.scale = (float(size[0]), float(size[1]), 1.0)
    plane.rotation_mode = "QUATERNION"
    plane.rotation_quaternion = normal.to_track_quat("Z", "Y")
    if d.get("roll"):
        plane.rotation_quaternion = plane.rotation_quaternion @ Quaternion((0, 0, 1), math.radians(float(d["roll"])))
    bg_apply(plane, location=False, rotation=True, scale=True)
    # Subdivide so the sheet can follow curvature, then wrap it onto the target.
    _bg_active(plane)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.subdivide(number_cuts=int(d.get("cuts", 6)))
    bpy.ops.object.mode_set(mode="OBJECT")
    mod = plane.modifiers.new("BGateWrap", "SHRINKWRAP")
    mod.target = target
    mod.wrap_method = "PROJECT"
    mod.use_project_z = True
    mod.use_negative_direction = True
    mod.use_positive_direction = True
    mod.offset = float(d.get("lift", 0.003))
    _bg_apply_mod(plane, mod)
    bg_unwrap(plane) if not plane.data.uv_layers else None
    # UVs: the plane's own 0..1 square, so the image fills the sheet.
    uv = plane.data.uv_layers.active or plane.data.uv_layers.new()
    lo = Vector((min(v.co.x for v in plane.data.vertices), min(v.co.y for v in plane.data.vertices)))
    hi = Vector((max(v.co.x for v in plane.data.vertices), max(v.co.y for v in plane.data.vertices)))
    span = Vector((max(hi.x - lo.x, 1e-6), max(hi.y - lo.y, 1e-6)))
    # Vertex coordinates before the wrap were planar in XY; the wrap moved Z
    # mostly, so XY still spans the sheet.
    for loop in plane.data.loops:
        v = plane.data.vertices[loop.vertex_index].co
        uv.data[loop.index].uv = ((v.x - lo.x) / span.x, (v.y - lo.y) / span.y)
    mat = bpy.data.materials.new(name + "Mat")
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    out = tree.nodes.new("ShaderNodeOutputMaterial"); out.location = (500, 0)
    bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (200, 0)
    tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    tex = tree.nodes.new("ShaderNodeTexImage"); tex.location = (-200, 0)
    tex.image = bpy.data.images.load(d["image"])
    tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    # alphaMode MASK is read off the alpha socket's graph by the exporter, not
    # off blend_method (see _TEXTURE_SCRIPT): Alpha -> GREATER_THAN(cutoff).
    clip = tree.nodes.new("ShaderNodeMath"); clip.location = (0, -200)
    clip.operation = "GREATER_THAN"
    clip.inputs[1].default_value = float(d.get("alpha_cutoff", 0.5))
    tree.links.new(tex.outputs["Alpha"], clip.inputs[0])
    tree.links.new(clip.outputs["Value"], bsdf.inputs["Alpha"])
    bsdf.inputs["Roughness"].default_value = float(d.get("roughness", 0.5))
    bsdf.inputs["Metallic"].default_value = float(d.get("metallic", 0.0))
    if d.get("emissive"):
        tree.links.new(tex.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = float(d.get("emissive"))
    try:
        mat.blend_method = "CLIP"
    except Exception:
        pass
    try:
        mat.surface_render_method = "DITHERED"
    except Exception:
        pass
    mat.use_backface_culling = True
    mat.alpha_threshold = float(d.get("alpha_cutoff", 0.5)) if hasattr(mat, "alpha_threshold") else 0.5
    plane.data.materials.append(mat)
    plane.parent = target
    plane.matrix_parent_inverse = target.matrix_world.inverted()
    placed.append({"name": name, "target": target.name, "image": os.path.basename(d["image"])})
_report({"ok": bool(placed) and not errors, "placed": placed, "errors": errors})
'''


# ---------------------------------------------------------------------------

def _run(script: str, payload: dict, out_path, timeout: int) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="bgate_surface_") as tmp:
        report_path = Path(tmp) / "report.json"
        payload = {**payload, "report_path": str(report_path).replace("\\", "/")}
        src = script.replace("__PAYLOAD__", json.dumps(payload))
        result = _blender.run_script(src, export_glb=str(out_path), timeout=timeout, record=False)
        ack = _blender._marked(result, MARK)
        report: dict = {}
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except ValueError:
                report = {}
        elif ack and not ack.get("dumped"):
            report = ack
    ok = bool(result.get("ok")) and bool(report.get("ok"))
    out = {"ok": ok, "out_path": str(out_path), **report, "seconds": result.get("seconds")}
    if not ok:
        listed = report.get("errors") or []
        out["error"] = (report.get("error") or ("; ".join(str(e) for e in listed) if listed else "")
                        or result.get("error") or "the surface script wrote no report")
        if result.get("traceback"):
            out["traceback"] = str(result["traceback"])[-800:]
    return out


def _model(model) -> Path:
    src = Path(model)
    if not src.is_file():
        raise FileNotFoundError(f"no such model: {src}")
    return src


def fuse(model, out_path, *, objects: Optional[list] = None, name: str = "Fused",
         voxel: float = 0.0, smooth: int = 2, target_tris: int = 0, angle: float = 30.0,
         timeout: int = 600) -> dict:
    """Fuse touching shells into one continuous surface. See bg_fuse."""
    src = _model(model)
    return _run(_FUSE, {"model": str(src.resolve()), "objects": list(objects or []), "name": name,
                        "voxel": float(voxel), "smooth": int(smooth),
                        "target_tris": int(target_tris), "angle": float(angle)}, out_path, timeout)


def shade(model, out_path, *, objects: Optional[list] = None, bevel: Optional[float] = None,
          angle: float = 30.0, segments: int = 2, weighted: bool = True, timeout: int = 300) -> dict:
    """Bevel-by-angle, smooth-by-angle, weighted normals. See bg_shade."""
    src = _model(model)
    return _run(_SHADE, {"model": str(src.resolve()), "objects": list(objects or []),
                         "bevel": bevel, "angle": float(angle), "segments": int(segments),
                         "weighted": bool(weighted)}, out_path, timeout)


def lathe(profile: list, out_path, *, name: str = "Lathe", segments: int = 32,
          preset: str = "", colour=None, scale: float = 1.0, bevel: float = 0.0,
          timeout: int = 300) -> dict:
    if len(profile) < 2 or any(len(p) != 2 for p in profile):
        raise ValueError("profile is a list of [radius, height] pairs, at least two")
    if preset and preset not in MATERIAL_PRESETS:
        raise ValueError(f"unknown preset {preset!r}; one of {sorted(MATERIAL_PRESETS)}")
    payload = {"name": name, "profile": [[float(r), float(z)] for r, z in profile],
               "segments": int(segments), "preset": preset, "colour": colour,
               "scale": float(scale), "bevel": float(bevel)}
    return _run(_LATHE, payload, out_path, timeout)


def loft(sections: list, out_path, *, name: str = "Loft", closed: bool = True, caps: bool = True,
         smooth: int = 0, mirror_x: bool = False, preset: str = "", colour=None,
         scale: float = 1.0, bevel: float = 0.0, timeout: int = 300) -> dict:
    if len(sections) < 2:
        raise ValueError("sections needs at least two cross-sections")
    count = len(sections[0])
    if count < 2 or any(len(s) != count for s in sections) or any(len(p) != 3 for s in sections for p in s):
        raise ValueError("every section must be a list of [x, y, z] points with the same count (>= 2)")
    if preset and preset not in MATERIAL_PRESETS:
        raise ValueError(f"unknown preset {preset!r}; one of {sorted(MATERIAL_PRESETS)}")
    payload = {"name": name, "sections": [[[float(c) for c in p] for p in s] for s in sections],
               "closed": bool(closed), "caps": bool(caps), "smooth": int(smooth),
               "mirror_x": bool(mirror_x), "preset": preset, "colour": colour,
               "scale": float(scale), "bevel": float(bevel)}
    return _run(_LOFT, payload, out_path, timeout)


def _check_assign(assign: list) -> None:
    for row in assign or []:
        if row.get("preset") not in MATERIAL_PRESETS:
            raise ValueError(f"unknown preset {row.get('preset')!r}; one of {sorted(MATERIAL_PRESETS)}")


def apply_materials(model, out_path, assign: list, *, sidecar: bool = True,
                    timeout: int = 300) -> dict:
    """assign: [{target: <object|material slot|''=all>, preset, colour, scale, roughness, wear, name}].

    The glTF carries the Principled constants (colour, roughness, metallic,
    coat). The procedural detail lives only in the node graph, which a glTF
    cannot hold, so a `.blend` sidecar is saved beside `out_path` - bake from
    THAT (or pass the same `assign` to bake) to turn the detail into maps.
    """
    src = _model(model)
    if not assign:
        raise ValueError("assign needs at least one {target, preset} row")
    _check_assign(assign)
    side = str(Path(out_path).with_suffix(".blend").resolve()) if sidecar else ""
    return _run(_MATERIAL, {"model": str(src.resolve()), "assign": assign, "sidecar": side},
                out_path, timeout)


def bake(model, out_path, *, textures_dir, objects: Optional[list] = None, resolution: int = 1024,
         samples: int = 16, maps: Optional[list] = None, ao_strength: float = 0.6, margin: int = 8,
         assign: Optional[list] = None, timeout: int = 1800) -> dict:
    """Bake every object's material to albedo/roughness/normal/AO PNGs and rewire.

    `model` should be the `.blend` sidecar apply_materials wrote, or pass
    `assign` to apply presets in this same session first: a glb has already
    lost every procedural node, and baking it gives back the flat colour it
    carries (measured: a bark bake from a re-imported glb came out white).
    """
    src = _model(model)
    _check_assign(assign or [])
    wanted = [m for m in (maps or list(BAKE_MAPS))]
    bad = [m for m in wanted if m not in BAKE_MAPS]
    if bad:
        raise ValueError(f"unknown maps {bad}; choose from {list(BAKE_MAPS)}")
    Path(textures_dir).mkdir(parents=True, exist_ok=True)
    return _run(_BAKE, {"model": str(src.resolve()), "objects": list(objects or []),
                        "assign": list(assign or []),
                        "resolution": int(resolution), "samples": int(samples), "maps": wanted,
                        "ao_strength": float(ao_strength), "margin": int(margin),
                        "textures_dir": str(Path(textures_dir).resolve())}, out_path, timeout)


def panel_line_image(path, *, length_px: int = 512, width_px: int = 24, softness: int = 3,
                     colour=(12, 12, 14), alpha: int = 230) -> str:
    """A transparent PNG with one dark line down the middle: the decal for a
    panel gap. Long axis is X; the decal's `size` stretches it."""
    from PIL import Image, ImageFilter
    img = Image.new("RGBA", (length_px, width_px * 4), (0, 0, 0, 0))
    line = Image.new("RGBA", (length_px, width_px), (*colour, alpha))
    img.paste(line, (0, (width_px * 4 - width_px) // 2))
    if softness:
        img = img.filter(ImageFilter.GaussianBlur(softness))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return str(path)


def decals(model, out_path, decals: list, *, timeout: int = 300) -> dict:
    """decals: [{image, target, position[x,y,z], normal[x,y,z], size[w,h], roll, cuts, lift,
    roughness, metallic, emissive, name}]. Each becomes a conformed alpha-clipped sheet."""
    src = _model(model)
    if not decals:
        raise ValueError("decals needs at least one row")
    for i, d in enumerate(decals):
        if not d.get("image") or not Path(d["image"]).is_file():
            raise FileNotFoundError(f"decal {i}: no image at {d.get('image')!r}")
        if "position" not in d or len(d["position"]) != 3:
            raise ValueError(f"decal {i}: position must be [x, y, z]")
    rows = [{**d, "image": str(Path(d["image"]).resolve())} for d in decals]
    return _run(_DECAL, {"model": str(src.resolve()), "decals": rows}, out_path, timeout)
