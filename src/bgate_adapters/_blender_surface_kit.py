"""The SURFACE half of the modelling kit: continuous forms and real materials.

Spliced into the kit after the primitive helpers (see _blender_kit.KIT), so an
agent inside blender_run has these by name, and the surface tools in
surface.py run the same source - one set of bytes, two callers.

WHY THIS EXISTS. Measured on Meridian (2026-09-05): a tree whose branches
were octagonal prisms shoved through the trunk, leaves as flat triangles with
the void showing between them, a 116k-triangle car with eleven flat materials
and zero textures, headlights floating in front of the fender, panel lines as
dark strips hovering over the paint. None of that is a modelling-skill problem;
it is what falls out of a vocabulary that is bg_box, bg_cyl and join. These
helpers are the operations a modeller actually reaches for: fuse touching
shells into one surface, bevel and smooth by angle, revolve a profile, loft
cross-sections, and give a surface a material that is more than one colour.
"""

SURFACE = r'''
# --- surface kit ------------------------------------------------------------
import math as _math
import bmesh as _bmesh
from mathutils import Vector as _V
from mathutils.bvhtree import BVHTree as _BVH


def _bg_active(obj):
    bg_only(obj)
    bpy.context.view_layer.objects.active = obj
    return obj


def _bg_apply_mod(obj, mod):
    _bg_active(obj)
    bpy.ops.object.modifier_apply(modifier=mod.name)


def _bg_smooth_by_angle(obj, angle=30.0):
    """Smooth shading with a hard-edge angle, on whichever API this Blender has."""
    _bg_active(obj)
    bpy.ops.object.shade_smooth()
    rad = _math.radians(angle)
    try:
        bpy.ops.object.shade_smooth_by_angle(angle=rad)          # 4.2+
        return "shade_smooth_by_angle"
    except Exception:
        pass
    try:
        bpy.ops.object.shade_auto_smooth(use_auto_smooth=True, angle=rad)  # 4.1
        return "shade_auto_smooth"
    except Exception:
        pass
    try:
        obj.data.use_auto_smooth = True                           # <= 4.0
        obj.data.auto_smooth_angle = rad
        return "use_auto_smooth"
    except Exception:
        return "shade_smooth"


def bg_shade(obj, bevel=0.0, angle=30.0, segments=2, weighted=True):
    """Bevel-by-angle + smooth-by-angle + weighted normals. The faceted look
    goes away at almost no triangle cost. `bevel` is metres (0 skips it; a
    good default is 0.5% of the object's largest dimension)."""
    if obj is None or obj.type != "MESH":
        return obj
    # WELD FIRST. A glTF round trip splits every vertex along a hard edge (a
    # flat-shaded box comes back as 24 verts and 12 loose triangles), and a
    # bevel or a smooth-by-angle on unshared edges does nothing. Measured:
    # 12 tris in, 12 tris out, "bevel applied".
    bg_clean(obj)
    if bevel and bevel > 0:
        mod = obj.modifiers.new("BGateBevel", "BEVEL")
        mod.width = float(bevel)
        mod.segments = int(segments)
        mod.limit_method = "ANGLE"
        mod.angle_limit = _math.radians(angle)
        mod.harden_normals = False
        _bg_apply_mod(obj, mod)
    how = _bg_smooth_by_angle(obj, angle)
    if weighted:
        try:
            mod = obj.modifiers.new("BGateWeightedNormal", "WEIGHTED_NORMAL")
            mod.keep_sharp = True
            _bg_apply_mod(obj, mod)
        except Exception:
            pass
    return obj


def bg_fuse(objects, name, voxel=0.0, smooth=2, target_tris=0, angle=30.0):
    """Fuse touching/overlapping meshes into ONE continuous surface.

    Join -> voxel remesh (the union happens here: a branch grows out of the
    trunk instead of poking through it) -> smooth -> decimate back to
    `target_tris` (0 keeps the remesh count) -> material indices carried over
    from the nearest original face, so a fused car keeps its paint, glass and
    rubber. `voxel` is the remesh cell in metres; 0 picks 0.4% of the largest
    dimension. Returns the fused object.
    """
    meshes = [o for o in objects if o is not None and o.type == "MESH"]
    if not meshes:
        return None
    # A frozen copy of the originals is the material oracle.
    bg_deselect()
    oracle_parts = []
    for o in meshes:
        c = o.copy(); c.data = o.data.copy(); c.name = o.name + "__oracle"
        bpy.context.scene.collection.objects.link(c)
        oracle_parts.append(c)
    oracle = bg_join(oracle_parts, name + "__oracle") if len(oracle_parts) > 1 else oracle_parts[0]
    bg_apply(oracle)
    fused = bg_join(meshes, name)
    bg_apply(fused)
    size = max(bg_bounds(fused)["dims"]) or 1.0
    cell = float(voxel) if voxel and voxel > 0 else size * 0.004
    fused.data.remesh_voxel_size = cell
    fused.data.remesh_voxel_adaptivity = 0.0
    fused.data.use_remesh_fix_poles = True
    fused.data.use_remesh_preserve_volume = True
    _bg_active(fused)
    bpy.ops.object.voxel_remesh()
    if smooth and smooth > 0:
        mod = fused.modifiers.new("BGateSmooth", "SMOOTH")
        mod.factor = 0.5
        mod.iterations = int(smooth)
        _bg_apply_mod(fused, mod)
    if target_tris and target_tris > 0:
        fused.data.calc_loop_triangles()
        have = len(fused.data.loop_triangles)
        if have > target_tris:
            mod = fused.modifiers.new("BGateDecimate", "DECIMATE")
            mod.ratio = max(0.01, float(target_tris) / float(have))
            mod.use_collapse_triangulate = True
            _bg_apply_mod(fused, mod)
    # Materials back from the oracle: nearest original face wins.
    slots = [m for m in oracle.data.materials if m is not None]
    fused.data.materials.clear()
    for m in slots:
        fused.data.materials.append(m)
    if slots:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        tree = _BVH.FromObject(oracle, depsgraph)
        om = oracle.matrix_world.inverted()
        fm = fused.matrix_world
        polys = oracle.data.polygons
        for face in fused.data.polygons:
            co = om @ (fm @ face.center)
            hit = tree.find_nearest(co)
            if hit and hit[2] is not None and hit[2] < len(polys):
                face.material_index = min(polys[hit[2]].material_index, len(slots) - 1)
    bpy.data.objects.remove(oracle, do_unlink=True)
    _bg_smooth_by_angle(fused, angle)
    bg_unwrap(fused)
    return fused


def bg_lathe(name, profile, segments=32, at=(0, 0, 0), material=None):
    """Revolve a 2D profile [(radius, height), ...] around Z into a smooth
    solid: a tyre, a rim, a bottle, a column, a lamp. Radius 0 at either end
    makes a pole; otherwise the end stays open (add a cap point at r=0).
    Returns the object, unwrapped cylindrically."""
    pts = [(float(r), float(z)) for r, z in profile]
    if len(pts) < 2:
        raise ValueError("bg_lathe needs at least two profile points")
    segs = max(3, int(segments))
    bm = _bmesh.new()
    rings = []
    for r, z in pts:
        if abs(r) < 1e-6:
            rings.append([bm.verts.new((0.0, 0.0, z))])
        else:
            ring = []
            for s in range(segs):
                a = 2.0 * _math.pi * s / segs
                ring.append(bm.verts.new((r * _math.cos(a), r * _math.sin(a), z)))
            rings.append(ring)
    bm.verts.ensure_lookup_table()
    uv = bm.loops.layers.uv.new("UVMap")
    n = len(rings)
    for i in range(n - 1):
        a, b = rings[i], rings[i + 1]
        for s in range(segs):
            s2 = (s + 1) % segs
            va = a[s] if len(a) > 1 else a[0]
            va2 = a[s2] if len(a) > 1 else a[0]
            vb = b[s] if len(b) > 1 else b[0]
            vb2 = b[s2] if len(b) > 1 else b[0]
            verts = [v for v in (va, va2, vb2, vb)]
            uniq = []
            for v in verts:
                if v not in uniq:
                    uniq.append(v)
            if len(uniq) < 3:
                continue
            try:
                f = bm.faces.new(uniq)
            except ValueError:
                continue
            for loop in f.loops:
                idx = verts.index(loop.vert)
                u = (s + (1 if idx in (1, 2) else 0)) / segs
                v = (i + (1 if idx in (2, 3) else 0)) / (n - 1)
                loop[uv].uv = (u, v)
    _bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = at
    _bg_smooth_by_angle(obj, 40.0)
    if material is not None:
        obj.data.materials.append(material)
    return obj


def bg_loft(name, sections, closed=True, caps=True, smooth=0, mirror_x=False,
            at=(0, 0, 0), material=None):
    """Skin a surface across cross-sections: a car body from five outlines, a
    boat hull, a fuselage. `sections` is a list of rings, each a list of
    [x, y, z] with the SAME point count. closed=True joins each ring's last
    point to its first (a tube); False leaves a strip. caps fills the two end
    rings; smooth adds that many subdivision levels; mirror_x mirrors across
    X so you draw half the car. Returns the object."""
    rings = [[_V((float(x), float(y), float(z))) for x, y, z in ring] for ring in sections]
    if len(rings) < 2:
        raise ValueError("bg_loft needs at least two sections")
    count = len(rings[0])
    if count < 2 or any(len(r) != count for r in rings):
        raise ValueError("every section must have the same number of points (>= 2)")
    bm = _bmesh.new()
    bverts = [[bm.verts.new(p) for p in ring] for ring in rings]
    span = count if closed else count - 1
    for i in range(len(bverts) - 1):
        a, b = bverts[i], bverts[i + 1]
        for s in range(span):
            s2 = (s + 1) % count
            try:
                bm.faces.new((a[s], a[s2], b[s2], b[s]))
            except ValueError:
                continue
    if caps and closed and count >= 3:
        for ring in (bverts[0], list(reversed(bverts[-1]))):
            try:
                bm.faces.new(ring)
            except ValueError:
                pass
    _bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = at
    if mirror_x:
        mod = obj.modifiers.new("BGateMirror", "MIRROR")
        mod.use_axis[0] = True
        mod.use_clip = True
        mod.use_mirror_merge = True
        mod.merge_threshold = 1e-4
        _bg_apply_mod(obj, mod)
    if smooth and smooth > 0:
        mod = obj.modifiers.new("BGateSubsurf", "SUBSURF")
        mod.levels = mod.render_levels = int(smooth)
        _bg_apply_mod(obj, mod)
    _bg_smooth_by_angle(obj, 45.0)
    bg_unwrap(obj)
    if material is not None:
        obj.data.materials.append(material)
    return obj


# ---------------------------------------------------------------------------
# Material presets. A FLAT COLOUR IS A PLACEHOLDER; these are surfaces. Each is
# a Principled BSDF plus the procedural detail the material is recognised by,
# parameterised by colour and scale. glTF carries the Principled constants
# (colour, roughness, metallic, coat); the procedural detail reaches the file
# only through bg_bake / blender_bake, which turns it into image maps.
BG_MATERIAL_PRESETS = {
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


def _bg_rgb(colour):
    if colour is None:
        return (0.6, 0.6, 0.6, 1.0)
    if isinstance(colour, str):
        c = colour.lstrip("#")
        return (int(c[0:2], 16) / 255.0, int(c[2:4], 16) / 255.0, int(c[4:6], 16) / 255.0, 1.0)
    c = list(colour) + [1.0]
    return (float(c[0]), float(c[1]), float(c[2]), float(c[3]))


def _bg_node(tree, kind, **props):
    node = tree.nodes.new(kind)
    for k, v in props.items():
        try:
            setattr(node, k, v)
        except Exception:
            pass
    return node


def _bg_set(bsdf, name, value):
    if name in bsdf.inputs:
        try:
            bsdf.inputs[name].default_value = value
        except Exception:
            pass


def bg_material(name, preset, colour=None, scale=1.0, roughness=None, wear=0.0,
                strength=1.0, obj=None):
    """A named material from a preset (see BG_MATERIAL_PRESETS) in `colour`
    (hex or rgb), with procedural detail at `scale` (1.0 = metre-ish grain).
    Assigns it to `obj` when given. Re-calling with the same name rebuilds it."""
    if preset not in BG_MATERIAL_PRESETS:
        raise ValueError("unknown preset %r; one of %s" % (preset, sorted(BG_MATERIAL_PRESETS)))
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    out = _bg_node(tree, "ShaderNodeOutputMaterial", location=(600, 0))
    bsdf = _bg_node(tree, "ShaderNodeBsdfPrincipled", location=(300, 0))
    tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    rgb = _bg_rgb(colour)
    _bg_set(bsdf, "Base Color", rgb)
    coord = _bg_node(tree, "ShaderNodeTexCoord", location=(-900, 0))
    mapping = _bg_node(tree, "ShaderNodeMapping", location=(-700, 0))
    tree.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
    s = float(scale) if scale else 1.0

    def noise(nscale, detail=2.0, rough=0.5, loc=(-450, 0)):
        n = _bg_node(tree, "ShaderNodeTexNoise", location=loc)
        _bg_set(n, "Scale", nscale / s); _bg_set(n, "Detail", detail); _bg_set(n, "Roughness", rough)
        tree.links.new(mapping.outputs["Vector"], n.inputs["Vector"])
        return n

    def bump(source_socket, strength_value, distance=0.02):
        b = _bg_node(tree, "ShaderNodeBump", location=(50, -300))
        _bg_set(b, "Strength", strength_value); _bg_set(b, "Distance", distance)
        tree.links.new(source_socket, b.inputs["Height"])
        tree.links.new(b.outputs["Normal"], bsdf.inputs["Normal"])
        return b

    def ramp(source_socket, stops, loc=(-200, 200)):
        r = _bg_node(tree, "ShaderNodeValToRGB", location=loc)
        el = r.color_ramp.elements
        while len(el) > 1:
            el.remove(el[-1])
        el[0].position, el[0].color = stops[0]
        for pos, col in stops[1:]:
            e = el.new(pos); e.color = col
        tree.links.new(source_socket, r.inputs["Fac"])
        return r

    rough_default = {"car_paint": 0.2, "rubber": 0.85, "brushed_metal": 0.35, "chrome": 0.05,
                     "painted_metal_worn": 0.4, "plastic": 0.45, "glass": 0.05, "bark": 0.9,
                     "wood": 0.6, "concrete": 0.9, "emissive": 0.5}[preset]
    _bg_set(bsdf, "Roughness", float(roughness) if roughness is not None else rough_default)

    if preset == "car_paint":
        _bg_set(bsdf, "Metallic", 0.4)
        _bg_set(bsdf, "Coat Weight", 1.0); _bg_set(bsdf, "Coat Roughness", 0.03)
        flake = noise(900.0, detail=0.0, rough=1.0)
        bump(flake.outputs["Fac"], 0.015 * strength, 0.002)
    elif preset == "rubber":
        _bg_set(bsdf, "Metallic", 0.0)
        _bg_set(bsdf, "Specular IOR Level", 0.3)
        grain = noise(60.0, detail=4.0)
        bump(grain.outputs["Fac"], 0.25 * strength, 0.01)
    elif preset == "brushed_metal":
        _bg_set(bsdf, "Metallic", 1.0)
        _bg_set(bsdf, "Anisotropic", 0.7)
        stretched = _bg_node(tree, "ShaderNodeMapping", location=(-700, -300))
        stretched.inputs["Scale"].default_value = (1.0, 60.0, 1.0)
        tree.links.new(coord.outputs["Object"], stretched.inputs["Vector"])
        streak = _bg_node(tree, "ShaderNodeTexNoise", location=(-450, -300))
        _bg_set(streak, "Scale", 40.0 / s); _bg_set(streak, "Detail", 6.0)
        tree.links.new(stretched.outputs["Vector"], streak.inputs["Vector"])
        r = ramp(streak.outputs["Fac"], [(0.35, (0.25, 0.25, 0.25, 1)), (0.65, (0.5, 0.5, 0.5, 1))], loc=(-200, -300))
        tree.links.new(r.outputs["Color"], bsdf.inputs["Roughness"])
    elif preset == "chrome":
        _bg_set(bsdf, "Metallic", 1.0)
    elif preset == "painted_metal_worn":
        _bg_set(bsdf, "Metallic", 0.0)
        geo = _bg_node(tree, "ShaderNodeNewGeometry", location=(-700, 300))
        ao = _bg_node(tree, "ShaderNodeAmbientOcclusion", location=(-700, 500))
        _bg_set(ao, "Distance", 0.2 * s)
        grime = noise(25.0, detail=3.0, loc=(-700, 700))
        # Edge mask: pointiness (convex edges) sharpened, mixed with grime,
        # scaled by `wear`.
        edge = ramp(geo.outputs["Pointiness"], [(0.5, (0, 0, 0, 1)), (0.62, (1, 1, 1, 1))], loc=(-450, 300))
        mixmask = _bg_node(tree, "ShaderNodeMixRGB", location=(-200, 300), blend_type="MULTIPLY")
        _bg_set(mixmask, "Fac", 1.0)
        tree.links.new(edge.outputs["Color"], mixmask.inputs["Color1"])
        tree.links.new(grime.outputs["Fac"], mixmask.inputs["Color2"])
        amount = _bg_node(tree, "ShaderNodeMath", location=(0, 300), operation="MULTIPLY")
        tree.links.new(mixmask.outputs["Color"], amount.inputs[0])
        amount.inputs[1].default_value = max(0.0, min(1.0, float(wear))) * 2.0
        clampn = _bg_node(tree, "ShaderNodeClamp", location=(150, 300))
        tree.links.new(amount.outputs["Value"], clampn.inputs["Value"])
        paint_vs_metal = _bg_node(tree, "ShaderNodeMixRGB", location=(100, 100))
        paint_vs_metal.inputs["Color1"].default_value = rgb
        paint_vs_metal.inputs["Color2"].default_value = (0.55, 0.55, 0.58, 1.0)
        tree.links.new(clampn.outputs["Result"], paint_vs_metal.inputs["Fac"])
        tree.links.new(paint_vs_metal.outputs["Color"], bsdf.inputs["Base Color"])
        tree.links.new(clampn.outputs["Result"], bsdf.inputs["Metallic"])
        # AO darkens the paint in crevices, faintly.
        dirt = _bg_node(tree, "ShaderNodeMixRGB", location=(-200, 500), blend_type="MULTIPLY")
        _bg_set(dirt, "Fac", 0.35)
        tree.links.new(paint_vs_metal.outputs["Color"], dirt.inputs["Color1"])
        tree.links.new(ao.outputs["Color"], dirt.inputs["Color2"])
        tree.links.new(dirt.outputs["Color"], bsdf.inputs["Base Color"])
        bump(grime.outputs["Fac"], 0.08 * strength, 0.005)
    elif preset == "plastic":
        _bg_set(bsdf, "Metallic", 0.0)
        _bg_set(bsdf, "Specular IOR Level", 0.5)
        grain = noise(200.0, detail=1.0)
        bump(grain.outputs["Fac"], 0.03 * strength, 0.002)
    elif preset == "glass":
        _bg_set(bsdf, "Transmission Weight", 1.0)
        _bg_set(bsdf, "IOR", 1.45)
        _bg_set(bsdf, "Alpha", 0.35)
        mat.blend_method = "BLEND" if hasattr(mat, "blend_method") else mat.blend_method
    elif preset == "bark":
        ridges = _bg_node(tree, "ShaderNodeMapping", location=(-700, -300))
        ridges.inputs["Scale"].default_value = (1.0, 1.0, 0.15)
        tree.links.new(coord.outputs["Object"], ridges.inputs["Vector"])
        tex = _bg_node(tree, "ShaderNodeTexNoise", location=(-450, -300))
        _bg_set(tex, "Scale", 18.0 / s); _bg_set(tex, "Detail", 8.0); _bg_set(tex, "Roughness", 0.7)
        tree.links.new(ridges.outputs["Vector"], tex.inputs["Vector"])
        dark = tuple(c * 0.45 for c in rgb[:3]) + (1.0,)
        r = ramp(tex.outputs["Fac"], [(0.3, dark), (0.7, rgb)], loc=(-200, -300))
        tree.links.new(r.outputs["Color"], bsdf.inputs["Base Color"])
        bump(tex.outputs["Fac"], 0.6 * strength, 0.03)
    elif preset == "wood":
        stretch = _bg_node(tree, "ShaderNodeMapping", location=(-700, -300))
        stretch.inputs["Scale"].default_value = (1.0, 0.08, 1.0)
        tree.links.new(coord.outputs["Object"], stretch.inputs["Vector"])
        grain = _bg_node(tree, "ShaderNodeTexNoise", location=(-450, -300))
        _bg_set(grain, "Scale", 12.0 / s); _bg_set(grain, "Detail", 6.0)
        tree.links.new(stretch.outputs["Vector"], grain.inputs["Vector"])
        dark = tuple(c * 0.6 for c in rgb[:3]) + (1.0,)
        r = ramp(grain.outputs["Fac"], [(0.35, dark), (0.65, rgb)], loc=(-200, -300))
        tree.links.new(r.outputs["Color"], bsdf.inputs["Base Color"])
        bump(grain.outputs["Fac"], 0.15 * strength, 0.01)
    elif preset == "concrete":
        pits = noise(45.0, detail=6.0, rough=0.8)
        dark = tuple(c * 0.75 for c in rgb[:3]) + (1.0,)
        r = ramp(pits.outputs["Fac"], [(0.3, dark), (0.7, rgb)])
        tree.links.new(r.outputs["Color"], bsdf.inputs["Base Color"])
        bump(pits.outputs["Fac"], 0.3 * strength, 0.02)
    elif preset == "emissive":
        _bg_set(bsdf, "Emission Color", rgb)
        _bg_set(bsdf, "Emission Strength", 4.0 * strength)
    mat["bgate_preset"] = preset
    if obj is not None and obj.type == "MESH":
        if mat.name not in [m.name for m in obj.data.materials if m]:
            obj.data.materials.append(mat)
    return mat


# ---------------------------------------------------------------------------
# Trees and scatter. A tree is a recursive set of tapered curves swept into
# one mesh, never a stack of prisms; leaves are alpha-clipped cards placed at
# the tips. Deterministic per seed.

def bg_leaf_material(name, image_path, colour=None):
    """An alpha-clipped leaf card material from a PNG with alpha. MASK is read
    by the exporter off the alpha socket's graph (Alpha -> GREATER_THAN), so
    that is how it is wired; the card shows both sides."""
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    out = _bg_node(tree, "ShaderNodeOutputMaterial", location=(500, 0))
    bsdf = _bg_node(tree, "ShaderNodeBsdfPrincipled", location=(200, 0))
    tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    _bg_set(bsdf, "Roughness", 0.6)
    _bg_set(bsdf, "Specular IOR Level", 0.3)
    tex = _bg_node(tree, "ShaderNodeTexImage", location=(-300, 0))
    tex.image = bpy.data.images.load(image_path)
    if colour is not None:
        tint = _bg_node(tree, "ShaderNodeMixRGB", location=(-50, 100), blend_type="MULTIPLY")
        _bg_set(tint, "Fac", 1.0)
        tree.links.new(tex.outputs["Color"], tint.inputs["Color1"])
        tint.inputs["Color2"].default_value = _bg_rgb(colour)
        tree.links.new(tint.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    clip = _bg_node(tree, "ShaderNodeMath", location=(-50, -200), operation="GREATER_THAN")
    clip.inputs[1].default_value = 0.5
    tree.links.new(tex.outputs["Alpha"], clip.inputs[0])
    tree.links.new(clip.outputs["Value"], bsdf.inputs["Alpha"])
    mat.use_backface_culling = False
    try:
        mat.blend_method = "CLIP"
    except Exception:
        pass
    mat["bgate_preset"] = "leaf_card"
    return mat


def bg_tree(name="Tree", seed=1, height=6.0, trunk_radius=0.22, levels=3, branches=(4, 3, 2),
            length_ratio=0.62, radius_ratio=0.55, spread_deg=45.0, up_pull=0.35, lean_deg=6.0,
            segments=8, taper=0.35, bark=None, leaves=True, leaf_image=None, leaf_size=0.35,
            leaves_per_tip=6, leaf_colour=None, target_tris=0, fuse=True):
    """A tree as ONE swept surface plus one leaf-card mesh.

    Recursive tapered branches (curve splines with per-point radius, bevelled
    and converted), joined and welded into a single trunk mesh. Leaves are
    alpha-clipped quads at the tips of the last level, in one mesh named
    `<name>_Leaves`. Deterministic per `seed`. Returns (trunk_obj, leaves_obj).
    """
    import random as _random
    rng = _random.Random(int(seed))
    lengths = [float(height)]
    radii = [float(trunk_radius)]
    for _ in range(1, int(levels)):
        lengths.append(lengths[-1] * float(length_ratio))
        radii.append(radii[-1] * float(radius_ratio))
    tips = []          # (position, direction) of terminal segments
    splines = []       # each: list of (Vector, radius)

    def grow(origin, direction, level):
        L = lengths[level]
        R = radii[level]
        steps = 6
        pts = []
        pos = _V(origin)
        d = _V(direction).normalized()
        for i in range(steps + 1):
            t = i / steps
            r = R * (1.0 - (1.0 - float(taper)) * t)
            pts.append((pos.copy(), max(r, 0.004)))
            if i < steps:
                # Wander a little, pull toward up, keep length.
                jitter = _V((rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-0.3, 0.6))) * 0.18
                d = (d + jitter + _V((0, 0, float(up_pull))) * (0.15 if level else 0.05)).normalized()
                pos = pos + d * (L / steps)
        splines.append(pts)
        if level + 1 < int(levels):
            count = int(branches[min(level, len(branches) - 1)])
            for k in range(count):
                t = rng.uniform(0.45 if level == 0 else 0.3, 1.0)
                idx = min(int(t * steps), steps - 1)
                base = pts[idx][0].lerp(pts[idx + 1][0], t * steps - idx)
                parent_dir = (pts[idx + 1][0] - pts[idx][0]).normalized()
                # Spread around the parent by a random azimuth and the spread angle.
                az = rng.uniform(0, 2 * _math.pi) if count > 1 else rng.uniform(0, 2 * _math.pi)
                side = parent_dir.cross(_V((0, 0, 1)))
                if side.length < 1e-4:
                    side = _V((1, 0, 0))
                side = side.normalized().rotated(_math.radians(0.0), parent_dir) if False else side.normalized()
                perp = side.rotated(parent_dir, az) if hasattr(side, "rotated") else side
                from mathutils import Quaternion as _Q
                perp = _Q(parent_dir, az) @ side
                ang = _math.radians(float(spread_deg) * rng.uniform(0.7, 1.15))
                child = (parent_dir * _math.cos(ang) + perp * _math.sin(ang)).normalized()
                grow(base, child, level + 1)
        else:
            tips.append((pts[-1][0], d))

    lean = _math.radians(float(lean_deg))
    az0 = rng.uniform(0, 2 * _math.pi)
    grow(_V((0, 0, 0)), _V((_math.sin(lean) * _math.cos(az0), _math.sin(lean) * _math.sin(az0), _math.cos(lean))), 0)

    curve = bpy.data.curves.new(name + "Curve", "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = 1.0
    curve.bevel_resolution = max(1, int(segments) // 4)
    curve.use_fill_caps = True
    for pts in splines:
        sp = curve.splines.new("POLY")
        sp.points.add(len(pts) - 1)
        for p, (co, r) in zip(sp.points, pts):
            p.co = (co.x, co.y, co.z, 1.0)
            p.radius = r
    cobj = bpy.data.objects.new(name + "Curve", curve)
    bpy.context.scene.collection.objects.link(cobj)
    _bg_active(cobj)
    bpy.ops.object.convert(target="MESH")
    trunk = bpy.context.active_object
    trunk.name = name
    bg_clean(trunk, merge=0.002)
    if bark is not None:
        trunk.data.materials.append(bark)
    if fuse:
        # Each branch was its own swept tube meeting the parent's surface;
        # the voxel union makes them ONE shell - the whole point of a tree
        # that is not prisms pushed through a trunk.
        trunk = bg_fuse([trunk], name, smooth=2, target_tris=int(target_tris or 0), angle=50.0)
    elif target_tris and target_tris > 0:
        trunk.data.calc_loop_triangles()
        have = len(trunk.data.loop_triangles)
        if have > target_tris:
            mod = trunk.modifiers.new("BGateDecimate", "DECIMATE")
            mod.ratio = max(0.02, float(target_tris) / float(have))
            _bg_apply_mod(trunk, mod)
    if not fuse:
        _bg_smooth_by_angle(trunk, 50.0)
        bg_unwrap(trunk)
    leaves_obj = None
    if leaves and tips:
        bm = _bmesh.new()
        uv = bm.loops.layers.uv.new("UVMap")
        half = float(leaf_size) * 0.5
        for tip, d in tips:
            for _ in range(int(leaves_per_tip)):
                # A card per leaf, randomly rotated, hung a little below the tip.
                off = _V((rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-0.6, 0.3))) * float(leaf_size) * 0.8
                centre = tip + off
                yaw = rng.uniform(0, 2 * _math.pi)
                pitch = rng.uniform(-0.9, 0.3)
                from mathutils import Euler as _E
                rot = _E((pitch, 0.0, yaw), "XYZ").to_matrix()
                sz = half * rng.uniform(0.7, 1.3)
                corners = [rot @ _V((-sz, 0, 0)), rot @ _V((sz, 0, 0)), rot @ _V((sz, 0, 2 * sz)), rot @ _V((-sz, 0, 2 * sz))]
                verts = [bm.verts.new(centre + c) for c in corners]
                try:
                    f = bm.faces.new(verts)
                except ValueError:
                    continue
                for loop, (u, v) in zip(f.loops, ((0, 0), (1, 0), (1, 1), (0, 1))):
                    loop[uv].uv = (u, v)
        lmesh = bpy.data.meshes.new(name + "_Leaves")
        bm.to_mesh(lmesh); bm.free()
        leaves_obj = bpy.data.objects.new(name + "_Leaves", lmesh)
        bpy.context.scene.collection.objects.link(leaves_obj)
        if leaf_image:
            leaves_obj.data.materials.append(bg_leaf_material(name + "_LeafMat", leaf_image, leaf_colour))
        else:
            leaves_obj.data.materials.append(bg_material(name + "_LeafMat", "plastic", leaf_colour or "#5f8a3c"))
    return trunk, leaves_obj


def bg_scatter(target, item, count=200, seed=1, scale=(0.8, 1.25), align=True, up_only=True,
               name=None, sink=0.0):
    """Scatter copies of `item` (a mesh object) over `target`'s faces, area-
    weighted, as ONE mesh. `align` rotates each copy to the face normal;
    `up_only` skips faces pointing down; `sink` drops each copy into the
    surface by that many metres. Deterministic per `seed`."""
    import random as _random
    rng = _random.Random(int(seed))
    bpy.context.view_layer.update()
    mw = target.matrix_world
    faces = [(p, (mw.to_3x3() @ p.normal).normalized(), p.area * (mw.to_3x3().determinant() ** (2.0 / 3.0)))
             for p in target.data.polygons]
    if up_only:
        faces = [f for f in faces if f[1].z > 0.2]
    if not faces:
        return None
    total = sum(f[2] for f in faces)
    bm = _bmesh.new()
    src = _bmesh.new()
    src.from_mesh(item.data)
    uv_src = src.loops.layers.uv.active
    uv_dst = bm.loops.layers.uv.new("UVMap")
    import mathutils as _mu
    for _ in range(int(count)):
        pick = rng.uniform(0, total)
        acc = 0.0
        face = faces[-1]
        for f in faces:
            acc += f[2]
            if acc >= pick:
                face = f
                break
        poly, normal, _area = face
        vs = [mw @ target.data.vertices[i].co for i in poly.vertices]
        # Uniform point in a random triangle fan of the polygon.
        a, b, c = vs[0], vs[rng.randrange(1, len(vs) - 1) if len(vs) > 2 else 1], vs[-1] if len(vs) > 2 else vs[1]
        r1, r2 = rng.random(), rng.random()
        if r1 + r2 > 1:
            r1, r2 = 1 - r1, 1 - r2
        p = a + (b - a) * r1 + (c - a) * r2 - normal * float(sink)
        s = rng.uniform(float(scale[0]), float(scale[1]))
        rot = _mu.Matrix.Rotation(rng.uniform(0, 2 * _math.pi), 3, "Z")
        if align:
            rot = normal.to_track_quat("Z", "Y").to_matrix() @ rot
        xf = _mu.Matrix.Translation(p) @ (rot @ _mu.Matrix.Scale(s, 3)).to_4x4()
        vmap = {}
        for v in src.verts:
            vmap[v.index] = bm.verts.new(xf @ v.co)
        for f in src.faces:
            try:
                nf = bm.faces.new([vmap[v.index] for v in f.verts])
            except ValueError:
                continue
            nf.material_index = f.material_index
            if uv_src is not None:
                for lo, ld in zip(f.loops, nf.loops):
                    ld[uv_dst].uv = lo[uv_src].uv
    src.free()
    mesh = bpy.data.meshes.new((name or (item.name + "_Scatter")))
    bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name or (item.name + "_Scatter"), mesh)
    bpy.context.scene.collection.objects.link(obj)
    for m in item.data.materials:
        obj.data.materials.append(m)
    _bg_smooth_by_angle(obj, 45.0)
    return obj


def bg_surface_help():
    print(BG_SURFACE_EXAMPLE)
    return BG_SURFACE_EXAMPLE
'''

SURFACE_EXAMPLE = r'''
# A wheel and a fused tree, the surface way. Compare with BG_EXAMPLE.
tread = bg_lathe("Tyre", [(0.22, -0.11), (0.31, -0.10), (0.34, -0.06), (0.34, 0.06),
                          (0.31, 0.10), (0.22, 0.11), (0.22, -0.11)], segments=48)
bg_material("Rubber", "rubber", "#141414", obj=tread)
rim = bg_lathe("Rim", [(0.0, -0.08), (0.20, -0.08), (0.22, -0.02), (0.22, 0.02), (0.20, 0.08), (0.0, 0.08)],
               segments=48)
bg_material("Alloy", "brushed_metal", "#c8c8cc", obj=rim)
bg_shade(rim, bevel=0.004)

trunk = bg_cyl("Trunk", radius=0.25, depth=4.0, at=(0, 0, 2.0), verts=12)
bg_taper(trunk, top=0.6)
limb = bg_cyl("Limb", radius=0.12, depth=2.2, at=(0.86, 0, 4.08), verts=10)  # one end ON the trunk axis
limb.rotation_euler = (0, 0.9, 0)
tree = bg_fuse([trunk, limb], "Tree", smooth=3, target_tris=6000)   # ONE surface, no prism poking through
bg_material("Bark", "bark", "#6b5a45", scale=1.0, obj=tree)
'''
