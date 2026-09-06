"""The FORM half of the modelling kit: build the smooth shape FIRST.

Spliced into the kit after the surface helpers (see _blender_kit.KIT), so an
agent inside blender_run has these by name, and the form tools in form.py run
the same source.

WHY THIS EXISTS. The surface kit repairs what boxes-and-cylinders produce:
fuse the shells, bevel the edges, bake the materials. Measured on Meridian's
coupe, the repair route is the wrong one for anything mechanical - a voxel
union of a wheel is a disc, a remeshed body is a lump, and a car built out of
boxes stays a car built out of boxes. The cheap, blocky look is decided in the
FIRST script, by the vocabulary the first script has. These are the ways a
modeller starts a form that was never blocky:

  bg_hull    a body from its SILHOUETTES: side outline x top outline (x front),
             the way a car, boat, plane, fish or spaceship is drawn - one shell
             from two polygons, rounded as much as asked.
  bg_skin    a body from a STICK FIGURE: joints with radii, links between them
             - a creature, a character block, a hand, a root system - one
             smooth organic shell with limbs that grow out of the torso.
  bg_blob    a body from BLOBS that merge: metaballs - a boulder, a canopy, a
             cloud, a slime, a belly - unioned by construction, no seams.
  bg_sweep   a tube along a path with a radius per point: pipes, cables,
             horns, tails, roots, railings, tentacles.
  bg_round   a subdivision pass that keeps the edges you name: a box becomes
             a cushion, a pebble, a soft-edged car block, with crease control.
  bg_rock    a boulder: displaced sphere, seeded, optional flat bottom.

Every one returns a single clean, unwrapped mesh with smooth shading, ready
for bg_material / bg_shade / a bake. bg_form_help() prints a worked coupe from
two outlines, a quadruped from eleven joints and a rock.
"""

FORM = r'''
# --- form kit ---------------------------------------------------------------
from mathutils import noise as _noise


def _bg_poly_prism(name, points, plane, lo, hi):
    """Extrude a closed 2D polygon into a prism. `plane` names the two axes the
    points live in ("xz" = a side view, "xy" = a top view, "yz" = a front
    view); the third axis runs from lo to hi."""
    axes = {"x": 0, "y": 1, "z": 2}
    a, b = axes[plane[0]], axes[plane[1]]
    c = ({0, 1, 2} - {a, b}).pop()
    bm = _bmesh.new()
    def mk(u, v, w):
        co = [0.0, 0.0, 0.0]; co[a] = float(u); co[b] = float(v); co[c] = float(w)
        return bm.verts.new(co)
    bottom = [mk(u, v, lo) for u, v in points]
    top = [mk(u, v, hi) for u, v in points]
    n = len(points)
    bm.faces.new(bottom)
    bm.faces.new(list(reversed(top)))
    for i in range(n):
        j = (i + 1) % n
        try:
            bm.faces.new((bottom[i], bottom[j], top[j], top[i]))
        except ValueError:
            pass
    _bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _bg_symmetric(points, axis_index):
    """If every point sits on one side of an axis (a half outline), mirror it
    into the full closed outline. A car's top view is drawn as one half."""
    pts = [(float(u), float(v)) for u, v in points]
    vals = [p[axis_index] for p in pts]
    if min(vals) < -1e-6 and max(vals) > 1e-6:
        return pts
    sign = 1.0 if max(vals) > 1e-6 else -1.0
    # Order along the OTHER axis, mirror back along it.
    other = 1 - axis_index
    pts = sorted(pts, key=lambda p: p[other])
    back = [((p[0], -p[1]) if axis_index == 1 else (-p[0], p[1])) for p in reversed(pts)]
    full = pts + back
    # Drop duplicated centreline points.
    out = []
    for p in full:
        if not out or (abs(out[-1][0] - p[0]) > 1e-6 or abs(out[-1][1] - p[1]) > 1e-6):
            out.append(p)
    if len(out) > 2 and abs(out[0][0] - out[-1][0]) < 1e-6 and abs(out[0][1] - out[-1][1]) < 1e-6:
        out.pop()
    return out


def _bg_boolean(target, cutter, operation="INTERSECT"):
    mod = target.modifiers.new("BGateBool", "BOOLEAN")
    mod.operation = operation
    mod.operand_type = "OBJECT"
    mod.object = cutter
    try:
        mod.solver = "EXACT"
    except Exception:
        pass
    _bg_apply_mod(target, mod)
    bpy.data.objects.remove(cutter, do_unlink=True)
    return target


def bg_hull(name, side, top, front=None, round=2, voxel=0.0, target_tris=0, material=None,
            bevel=0.0):
    """A body from its silhouettes - the way a car, boat, plane, fish or ship
    is drawn.

    `side` is the closed outline seen from the side, [(x, z), ...]; `top` the
    outline seen from above, [(x, y), ...] (give the +y half and it is
    mirrored); `front` optionally the outline seen from the front, [(y, z),
    ...] (half mirrored the same way). The three prisms are intersected into
    one shell, then `round` (0-4) softens it: 0 keeps the hard silhouette
    edges (bevelled by `bevel` metres if given), 1 is a light chamfer, 2 a
    production car, 4 a bar of soap. `target_tris` decimates the rounded
    result. Returns one object, unwrapped, smooth-shaded."""
    side = [(float(x), float(z)) for x, z in side]
    top = _bg_symmetric(top, 1)
    if len(side) < 3 or len(top) < 3:
        raise ValueError("bg_hull needs closed outlines of at least three points")
    xs = [p[0] for p in side] + [p[0] for p in top]
    zs = [p[1] for p in side]
    ys = [p[1] for p in top]
    pad = 0.05 * max(max(xs) - min(xs), max(zs) - min(zs), 1e-3)
    body = _bg_poly_prism(name, side, "xz", min(ys) - pad, max(ys) + pad)
    plan = _bg_poly_prism(name + "__plan", top, "xy", min(zs) - pad, max(zs) + pad)
    _bg_boolean(body, plan)
    if front:
        fr = _bg_symmetric(front, 0)
        if len(fr) >= 3:
            face = _bg_poly_prism(name + "__front", fr, "yz", min(xs) - pad, max(xs) + pad)
            _bg_boolean(body, face)
    bg_clean(body)
    if material is not None:
        body.data.materials.append(material)
    r = int(round or 0)
    if r > 0:
        body = bg_fuse([body], name, voxel=voxel, smooth=2 * r, target_tris=int(target_tris or 0),
                       angle=40.0)
        bg_unwrap(body)
    else:
        if bevel and bevel > 0:
            bg_shade(body, bevel=float(bevel), angle=35.0)
        else:
            _bg_smooth_by_angle(body, 35.0)
        bg_unwrap(body)
    return body


def bg_skin(name, joints, links, subsurf=2, material=None, mirror_x=False):
    """A body from a stick figure: `joints` are (x, y, z, radius), `links` are
    (i, j) index pairs. The Skin modifier wraps every link in a tube of the
    joints' radii and blends them where they meet, so a leg grows out of a hip
    instead of poking into it; `subsurf` levels round it off. A creature, a
    character block-in, a hand, a root ball, a coral. mirror_x builds the +x
    half and mirrors it. Returns one welded, unwrapped object."""
    if len(joints) < 2 or not links:
        raise ValueError("bg_skin needs at least two joints and one link")
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(float(j[0]), float(j[1]), float(j[2])) for j in joints], [(int(a), int(b)) for a, b in links], [])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    _bg_active(obj)
    skin = obj.modifiers.new("BGateSkin", "SKIN")
    skin.use_smooth_shade = True
    skin.branch_smoothing = 0.5
    layer = obj.data.skin_vertices[0]
    for i, j in enumerate(joints):
        r = float(j[3]) if len(j) > 3 else 0.1
        layer.data[i].radius = (r, r)
        layer.data[i].use_root = (i == 0)
    _bg_apply_mod(obj, skin)
    if mirror_x:
        mod = obj.modifiers.new("BGateMirror", "MIRROR")
        mod.use_axis[0] = True
        mod.use_clip = True
        mod.use_mirror_merge = True
        mod.merge_threshold = 1e-3
        _bg_apply_mod(obj, mod)
    if subsurf and int(subsurf) > 0:
        mod = obj.modifiers.new("BGateSubsurf", "SUBSURF")
        mod.levels = mod.render_levels = int(subsurf)
        _bg_apply_mod(obj, mod)
    bg_clean(obj)
    _bg_smooth_by_angle(obj, 60.0)
    bg_unwrap(obj)
    if material is not None:
        obj.data.materials.append(material)
    return obj


def bg_blob(name, balls, resolution=0.06, threshold=0.6, target_tris=0, material=None):
    """A body from blobs that merge: metaballs. `balls` are (x, y, z, radius)
    - a NEGATIVE radius carves - or (x, y, z, radius, sx, sy, sz) for an
    ellipsoid stretched by those factors. Neighbouring balls union smoothly by
    construction: a boulder from five, a tree canopy from twenty, a slime, a
    belly, a cloud. `resolution` is the mesh cell in metres (smaller = finer,
    slower). Returns one object, unwrapped, smooth."""
    if not balls:
        raise ValueError("bg_blob needs at least one ball")
    mb = bpy.data.metaballs.new(name)
    mb.resolution = float(resolution)
    mb.render_resolution = float(resolution)
    mb.threshold = float(threshold)
    for b in balls:
        el = mb.elements.new()
        r = float(b[3])
        el.co = (float(b[0]), float(b[1]), float(b[2]))
        el.radius = abs(r)
        el.use_negative = r < 0
        if len(b) >= 7:
            el.type = "ELLIPSOID"
            el.size_x, el.size_y, el.size_z = float(b[4]), float(b[5]), float(b[6])
    obj = bpy.data.objects.new(name, mb)
    bpy.context.scene.collection.objects.link(obj)
    _bg_active(obj)
    bpy.context.view_layer.update()
    bpy.ops.object.convert(target="MESH")
    obj = bpy.context.active_object
    obj.name = name
    obj.data.name = name
    bg_clean(obj)
    if target_tris and int(target_tris) > 0:
        _bg_decimate_to(obj, int(target_tris))
    _bg_smooth_by_angle(obj, 60.0)
    bg_unwrap(obj)
    if material is not None:
        obj.data.materials.append(material)
    return obj


def bg_sweep(name, points, radii, segments=12, caps=True, smooth_path=True, material=None):
    """A tube along a path with a radius per point: a pipe, a cable, a horn, a
    tail, a root, a railing, a tentacle. `points` are (x, y, z), `radii` one
    float per point (or one float for all). smooth_path=True runs a smooth
    curve through the points; False keeps straight runs between them. Returns
    one object, welded, unwrapped."""
    pts = [_V((float(x), float(y), float(z))) for x, y, z in points]
    if len(pts) < 2:
        raise ValueError("bg_sweep needs at least two points")
    if isinstance(radii, (int, float)):
        radii = [float(radii)] * len(pts)
    if len(radii) != len(pts):
        raise ValueError("bg_sweep needs one radius per point")
    curve = bpy.data.curves.new(name + "Curve", "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = 1.0
    curve.bevel_resolution = max(1, int(segments) // 4)
    curve.use_fill_caps = bool(caps)
    if smooth_path and len(pts) >= 3:
        sp = curve.splines.new("NURBS")
        sp.points.add(len(pts) - 1)
        sp.order_u = min(4, len(pts))
        sp.use_endpoint_u = True
        curve.resolution_u = 8
    else:
        sp = curve.splines.new("POLY")
        sp.points.add(len(pts) - 1)
    for p, co, r in zip(sp.points, pts, radii):
        p.co = (co.x, co.y, co.z, 1.0)
        p.radius = max(float(r), 0.001)
    cobj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(cobj)
    _bg_active(cobj)
    bpy.ops.object.convert(target="MESH")
    obj = bpy.context.active_object
    obj.name = name
    bg_clean(obj, merge=0.0005)
    _bg_smooth_by_angle(obj, 60.0)
    bg_unwrap(obj)
    if material is not None:
        obj.data.materials.append(material)
    return obj


def bg_round(obj, levels=2, crease_angle=None):
    """Subdivision-surface rounding that keeps the edges you name. A box
    becomes a cushion; with crease_angle=60 the edges sharper than 60 degrees
    stay as creases (a car block keeps its sill line, a crate keeps its
    corners while its faces soften). Returns obj."""
    if obj is None or obj.type != "MESH":
        return obj
    bg_clean(obj)
    if crease_angle is not None:
        bm = _bmesh.new(); bm.from_mesh(obj.data)
        crease = bm.edges.layers.float.get("crease_edge") or bm.edges.layers.float.new("crease_edge")
        rad = _math.radians(float(crease_angle))
        for e in bm.edges:
            if len(e.link_faces) == 2 and e.calc_face_angle(0.0) > rad:
                e[crease] = 1.0
        bm.to_mesh(obj.data); bm.free()
    mod = obj.modifiers.new("BGateSubsurf", "SUBSURF")
    mod.levels = mod.render_levels = max(1, int(levels))
    mod.use_creases = True
    _bg_apply_mod(obj, mod)
    _bg_smooth_by_angle(obj, 45.0)
    bg_unwrap(obj)
    return obj


def bg_rock(name, size=(1.0, 0.8, 0.6), seed=1, detail=4, roughness=0.3, facets=0.0,
            flat_bottom=True, material=None, at=(0, 0, 0)):
    """A boulder: a sphere displaced by seeded fractal noise, scaled to
    `size` (metres, x y z), sat on the ground. `detail` is icosphere
    subdivisions (3 pebble 320 tris, 4 rock 1280, 5 hero boulder 5120); `roughness` 0-1 how far
    the noise pushes; `facets` 0-1 planar-decimates toward a chiselled
    low-poly look; flat_bottom slices the underside so it sits. Same seed,
    same rock."""
    import random as _random
    rng = _random.Random(int(seed))
    bm = _bmesh.new()
    _bmesh.ops.create_icosphere(bm, subdivisions=max(1, int(detail)), radius=1.0)
    offset = _V((rng.uniform(-50, 50), rng.uniform(-50, 50), rng.uniform(-50, 50)))
    amp = 0.35 * float(roughness)
    for v in bm.verts:
        p = v.co * 1.6 + offset
        n1 = _noise.noise(p)
        n2 = _noise.noise(p * 2.3 + _V((7.1, 3.3, 9.9)))
        n3 = _noise.noise(p * 5.1 + _V((1.7, 8.2, 4.4)))
        d = 1.0 + amp * (n1 + 0.5 * n2 + 0.25 * n3)
        v.co = v.co * d
    if flat_bottom:
        for v in bm.verts:
            if v.co.z < -0.55:
                v.co.z = -0.55
    _bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    sx, sy, sz = (float(s) for s in size)
    obj.scale = (sx / 2.0, sy / 2.0, sz / (1.55 if flat_bottom else 2.0))
    bg_apply(obj)
    b = bg_bounds(obj)
    obj.location = (float(at[0]), float(at[1]), float(at[2]) - b["min"][2])
    bg_apply(obj, location=True)
    if facets and float(facets) > 0:
        mod = obj.modifiers.new("BGateFacets", "DECIMATE")
        mod.decimate_type = "DISSOLVE"
        mod.angle_limit = _math.radians(2.0 + 18.0 * float(facets))
        _bg_apply_mod(obj, mod)
        _bg_active(obj)
        bpy.ops.object.shade_flat()
    else:
        bg_clean(obj)
        _bg_smooth_by_angle(obj, 40.0)
    bg_unwrap(obj)
    if material is not None:
        obj.data.materials.append(material)
    return obj


def bg_form_help():
    print(BG_FORM_EXAMPLE)
    return BG_FORM_EXAMPLE
'''

FORM_EXAMPLE = r'''
# A coupe, a quadruped and a rock, the FORM way: the smooth shape is built
# first, from silhouettes, a stick figure and blobs. Compare with BG_EXAMPLE
# (a body out of boxes) and BG_SURFACE_EXAMPLE (repairing tacked shells).

# --- coupe: two outlines -> one shell ---------------------------------------
# Side view (x forward, z up), metres. Draw the profile a person would sketch:
# low nose, hood, windscreen, roof, rear glass, boot, bumper, floor.
SIDE = [(-2.10, 0.35), (-2.05, 0.65), (-1.60, 0.72), (-0.95, 0.75), (-0.55, 1.05),
        (0.30, 1.22), (1.05, 1.20), (1.55, 0.98), (1.95, 0.80), (2.15, 0.62),
        (2.15, 0.35), (1.70, 0.30), (-1.70, 0.30)]
# Top view (x forward, y across) - the +y HALF, mirrored for you.
TOP = [(-2.15, 0.00), (-2.10, 0.70), (-1.60, 0.86), (0.60, 0.92), (1.80, 0.86),
       (2.15, 0.62), (2.15, 0.00)]
paint = bg_material("Paint", "car_paint", "#7a1020")
body = bg_hull("Body", SIDE, TOP, round=2, target_tris=16000, material=paint)
for x, y in ((1.35, 0.85), (1.35, -0.85), (-1.35, 0.85), (-1.35, -0.85)):
    tyre = bg_lathe("Tyre", [(0.22, -0.11), (0.31, -0.10), (0.34, -0.06), (0.34, 0.06),
                             (0.31, 0.10), (0.22, 0.11), (0.22, -0.11)], segments=40)
    bg_material("Rubber", "rubber", "#141414", obj=tyre)
    tyre.rotation_euler = (1.5708, 0, 0); tyre.location = (x, y, 0.34)

# --- quadruped: eleven joints -> one organic body ----------------------------
J = [(0.0, 0, 0.62, 0.20),   # 0 chest (root)
     (-0.55, 0, 0.60, 0.18), # 1 hips
     (0.42, 0, 0.78, 0.11),  # 2 neck
     (0.70, 0, 0.92, 0.13),  # 3 head
     (0.18, 0.17, 0.35, 0.06), (0.18, 0.19, 0.04, 0.05),    # 4,5 front leg R
     (0.18, -0.17, 0.35, 0.06), (0.18, -0.19, 0.04, 0.05),  # 6,7 front leg L
     (-0.60, 0.16, 0.32, 0.07), (-0.62, 0.18, 0.04, 0.05),  # 8,9 back leg R
     (-0.60, -0.16, 0.32, 0.07), (-0.62, -0.18, 0.04, 0.05),  # 10,11 back leg L
     (-0.85, 0, 0.66, 0.05), (-1.10, 0, 0.55, 0.03)]        # 12,13 tail
L = [(0, 1), (0, 2), (2, 3), (0, 4), (4, 5), (0, 6), (6, 7), (1, 8), (8, 9), (1, 10), (10, 11),
     (1, 12), (12, 13)]
beast = bg_skin("Beast", J, L, subsurf=2)
bg_material("Hide", "plastic", "#6b4a3a", obj=beast)
beast.location.y = 4.0

# --- rock -------------------------------------------------------------------
rock = bg_rock("Boulder", size=(1.6, 1.2, 0.9), seed=3, detail=4, roughness=0.35, at=(0, -4, 0))
bg_material("Stone", "concrete", "#6e6a62", obj=rock)
'''
