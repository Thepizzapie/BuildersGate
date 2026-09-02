"""bpy source injected into an agent's script — the modelling floor.

NOT imported by Builders Gate. This module holds ONE string, and that string is
prepended to the agent's own script inside Blender.

WHY IT EXISTS. Measured on the first real character run: an agent wrote 33 KB of
its own modelling helpers — materials, UV unwrapping, mesh hygiene, mirroring,
joining — before it modelled anything, and then spent twenty more minutes
discovering that bone-heat weighting fails on the geometry those helpers
produced. Every request would have paid that same cost from zero. The helpers
are not the art; they are the floor the art stands on, and a floor belongs in
the repo.

THE HYGIENE FUNCTIONS ARE THE POINT, not the primitives. `clean()` is what makes
a mesh survive automatic weights, and it is the single thing an agent is least
likely to think of and most likely to be destroyed by.

Everything here is defensive: an agent's script runs after this, and a helper
that raises takes the whole run down with it. The ONE deliberate exception is
`bg_bone_chain`, which validates its bone list and raises — a rig that is wrong
is worth stopping for, because the alternative is an armature that looks built
and comes apart in the engine.

THE WORKED EXAMPLE IS PART OF THE KIT. `EXAMPLE` below is a complete, running
layer script — proportions, a named humanoid chain with roll, mirroring, taper,
checks, `bg_finish` last. It is spliced into KIT as `BG_EXAMPLE`, so an agent
inside Blender can `print(bg_help())` and read it without leaving the script.
"""

_HELPERS = r'''
# --- Builders Gate modelling kit (injected) ---------------------------------
import bpy, bmesh, math
from mathutils import Matrix, Vector


def bg_deselect():
    for o in bpy.context.scene.objects:
        o.select_set(False)


def bg_only(obj):
    """Make obj the one selected, active object — the state most bpy.ops want."""
    bg_deselect()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return obj


def bg_wipe():
    """Empty scene. --factory-startup ships Cube/Camera/Light, and a layer that
    quietly contains the default cube is a bug found later, in the engine."""
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    for block in list(bpy.data.meshes):
        if not block.users:
            bpy.data.meshes.remove(block)


def bg_clean(obj, merge=0.0002, recalc=True):
    """Make a mesh weightable. THIS IS THE ONE THAT MATTERS.

    Automatic (bone-heat) weighting refuses geometry with doubled vertices,
    loose verts/edges, zero-area faces or inverted normals — and it does not say
    so, it just leaves vertices unweighted, which reads in-engine as the mesh
    tearing at the rest pose. Every modelling step that booleans, mirrors or
    joins produces exactly that geometry.

    Call this on every mesh before it leaves your layer script.
    """
    if obj is None or obj.type != "MESH":
        return obj
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    try:
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=merge)
        loose = [v for v in bm.verts if not v.link_edges]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="VERTS")
        degenerate = [f for f in bm.faces if f.calc_area() < 1e-9]
        if degenerate:
            bmesh.ops.delete(bm, geom=degenerate, context="FACES")
        if recalc:
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    return obj


def bg_bounds(obj):
    """World-space axis-aligned bounds: min / max / dims / centre.

    WORLD SPACE, not local. `obj.data.vertices` are local coordinates and a
    layer that was scaled reports the wrong size from them; these numbers are
    what the object actually occupies in the scene.
    """
    empty = {"min": (0.0, 0.0, 0.0), "max": (0.0, 0.0, 0.0),
             "dims": (0.0, 0.0, 0.0), "centre": (0.0, 0.0, 0.0)}
    if obj is None:
        return empty
    try:
        # matrix_world is stale until the depsgraph catches up — a scale set two
        # lines ago reads as 1.0 without this, which silently halves every
        # measurement taken during a build.
        bpy.context.view_layer.update()
        matrix = obj.matrix_world
        corners = [matrix @ Vector(corner) for corner in obj.bound_box]
        if not corners:
            return empty
        low = tuple(min(c[i] for c in corners) for i in range(3))
        high = tuple(max(c[i] for c in corners) for i in range(3))
        return {"min": low, "max": high,
                "dims": tuple(high[i] - low[i] for i in range(3)),
                "centre": tuple((high[i] + low[i]) / 2.0 for i in range(3))}
    except Exception:
        return empty


def bg_flipped(obj):
    """How many faces point INWARD — count, not a guess.

    An inverted face survives every other check, refuses bone-heat weighting and
    renders as a hole in the model. Measured by recalculating normals on a throw-
    away copy and counting the ones that had to turn around; the mesh is not
    touched. `bg_clean(recalc=True)` is the fix.

    RETURNS -1, NOT 0, WHEN THE CHECK ITSELF FAILS. This used to answer 0 on an
    internal error, which is the same answer as "no inverted faces" — a check
    that reports clean when it broke. It went unnoticed while nothing depended
    on it; bg_adopt's quality verdict now does, and a gate that cannot tell "I
    found none" from "I could not look" is worse than no gate. Callers must
    treat a negative count as UNKNOWN and fail closed.
    """
    if obj is None or obj.type != "MESH" or not obj.data.polygons:
        return 0
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        before = [f.normal.copy() for f in bm.faces]
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.faces.ensure_lookup_table()
        return sum(1 for i, f in enumerate(bm.faces)
                   if f.normal.dot(before[i]) < 0)
    except Exception:
        return -1
    finally:
        bm.free()


def bg_stats(obj):
    """verts / faces / loose / non-manifold / ngons / flipped / SIZE.

    Read this before you weight, and read `dims` before you hand a layer over.
    A layer is authored in its own scene with no shared proportion frame, so
    "is the cap head-sized" is not a thing anyone can see — it is a number, and
    this is the number. dims/centre/min/max are WORLD SPACE, in metres.
    """
    if obj is None or obj.type != "MESH":
        return {}
    bounds = bg_bounds(obj)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        stats = {"verts": len(bm.verts), "faces": len(bm.faces),
                 "loose": sum(1 for v in bm.verts if not v.link_edges),
                 "nonmanifold": sum(1 for e in bm.edges if not e.is_manifold),
                 "ngons": sum(1 for f in bm.faces if len(f.verts) > 4)}
    finally:
        bm.free()
    stats["flipped"] = bg_flipped(obj)
    stats.update(bounds)
    return stats


def bg_overlap(a, b):
    """Do two objects' world bounds intersect, and by how much?

    LAYERS ARE BUILT IN ISOLATED SCENES. Nothing else in the pipeline compares
    two of them until they are already combined, so a cap sunk inside the head
    or floating a hand's width above it passes every check there is. This is
    that check.

    Returns intersects / overlap (per axis, 0 where they are apart) / gap (per
    axis, 0 where they meet) / volume / fraction (of the SMALLER object's box,
    so 1.0 means fully swallowed) / inside ("a"/"b"/None) / verdict.
    """
    box_a, box_b = bg_bounds(a), bg_bounds(b)
    if not any(box_a["dims"]) or not any(box_b["dims"]):
        # Almost always a joined-away object: bg_join leaves ONE object and the
        # parts are gone, so a comparison written after the join compares two
        # ghosts. Say that instead of reporting a confident zero.
        return {"intersects": False, "overlap": (0.0, 0.0, 0.0),
                "gap": (0.0, 0.0, 0.0), "volume": 0.0, "fraction": 0.0,
                "inside": None,
                "verdict": "no bounds — one of these has no size, or is no "
                           "longer in the scene (bg_join consumes its parts)"}
    over = [min(box_a["max"][i], box_b["max"][i])
            - max(box_a["min"][i], box_b["min"][i]) for i in range(3)]
    hit = all(o > 0.0 for o in over)
    overlap = tuple(max(o, 0.0) for o in over)
    gap = tuple(max(-o, 0.0) for o in over)

    def _volume(box):
        return box["dims"][0] * box["dims"][1] * box["dims"][2]

    shared = overlap[0] * overlap[1] * overlap[2]
    smaller = min(_volume(box_a), _volume(box_b))
    fraction = (shared / smaller) if smaller > 1e-12 else 0.0

    def _within(inner, outer):
        return all(outer["min"][i] <= inner["min"][i] + 1e-6
                   and inner["max"][i] <= outer["max"][i] + 1e-6
                   for i in range(3))

    inside = "a" if _within(box_a, box_b) else ("b" if _within(box_b, box_a)
                                                else None)
    if inside:
        verdict = "%s is entirely inside the other's bounds" % inside
    elif not hit:
        verdict = "apart by %.4f" % max(gap)
    elif fraction > 0.5:
        verdict = "sunk: %.0f%% of the smaller box is shared" % (fraction * 100)
    else:
        verdict = "touching: %.0f%% of the smaller box is shared" % (
            fraction * 100)
    return {"intersects": hit, "overlap": overlap, "gap": gap,
            "volume": shared, "fraction": fraction, "inside": inside,
            "verdict": verdict}


def bg_mat(obj, name, rgb, rough=0.6, metal=0.0):
    """Assign a named material. rgb is 0-1 float triple.

    A FLAT COLOUR IS A PLACEHOLDER, NOT A TEXTURE. Use this to block a layer in;
    the shipped surface comes from a generated image through blender_texture,
    because 21 flat materials and zero image maps is what "untextured" looks
    like in a glTF.
    """
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
        for key, value in (("Roughness", rough), ("Metallic", metal)):
            if key in bsdf.inputs:
                bsdf.inputs[key].default_value = value
    if obj is not None and obj.type == "MESH":
        if mat.name not in [s.name for s in obj.data.materials]:
            obj.data.materials.append(mat)
    return mat


def bg_unwrap(obj, angle=66.0, margin=0.02):
    """Smart-project UVs. A mesh with no UV layer cannot be textured at all —
    the readiness check flags it, and by then the layer is already built."""
    if obj is None or obj.type != "MESH":
        return obj
    bg_only(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    try:
        bpy.ops.uv.smart_project(angle_limit=math.radians(angle),
                                 island_margin=margin)
    except TypeError:                       # older builds take degrees
        bpy.ops.uv.smart_project(angle_limit=angle, island_margin=margin)
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def bg_box(name, size=(1, 1, 1), at=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(size=1, location=at)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = size
    bg_apply(obj)
    return obj


def bg_cyl(name, radius=0.5, depth=1.0, at=(0, 0, 0), verts=16):
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth,
                                        vertices=verts, location=at)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def bg_ball(name, radius=0.5, at=(0, 0, 0), segments=24, rings=12):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=at,
                                         segments=segments, ring_count=rings)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def bg_plane(name, size=1.0, at=(0, 0, 0)):
    bpy.ops.mesh.primitive_plane_add(size=size, location=at)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def bg_taper(obj, top=0.6, axis=2):
    """Scale one end of a mesh — a limb is a tapered cylinder, not a tube."""
    if obj is None or obj.type != "MESH":
        return obj
    values = [v.co[axis] for v in obj.data.vertices]
    if not values:
        return obj
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    for vert in obj.data.vertices:
        t = (vert.co[axis] - low) / span
        factor = 1.0 + (top - 1.0) * t
        for other in (0, 1, 2):
            if other != axis:
                vert.co[other] *= factor
    obj.data.update()
    return obj


def bg_op(result, what):
    """Fail LOUDLY on a bpy operator that quietly did nothing.

    THE SILENT NO-OP. `bpy.ops.*` does not raise when its poll fails — it
    returns `{'CANCELLED'}` and carries on. MEASURED: a script calling
    `bpy.ops.object.transform_apply(...)` with nothing selected got
    `{'CANCELLED'}`, changed not one vertex, and the run reported `ok: True`.
    Three identical "rotated" exports shipped before anybody checked the return
    value, because nothing in the pipeline ever looked at it.

    Wrap any operator whose effect you are relying on:

        bg_op(bpy.ops.object.shade_smooth(), "shade_smooth")
    """
    if not isinstance(result, set):
        return result
    if "FINISHED" in result:
        return result
    raise RuntimeError(
        "bpy.ops %s returned %s and changed NOTHING. Operators do not raise "
        "when their poll fails - they return CANCELLED and the script carries "
        "on reporting success. The usual cause is context: nothing selected, "
        "no active object, or the wrong mode. Select and activate the object "
        "first (bg_only) and try again." % (what, sorted(result)))


def bg_apply(obj, location=False, rotation=True, scale=True):
    """Bake transforms into the mesh. NO OPERATOR — it cannot silently no-op.

    An unapplied scale shears children and normals, and the readiness check
    will say so after export. What it will NOT say is that the apply never
    happened: `bpy.ops.object.transform_apply` returns `{'CANCELLED'}` on a bad
    context and changes nothing, which is indistinguishable from success from
    the calling script's point of view. Three re-exports were paid for before
    that was identified.

    So this transforms the mesh data by the object's own matrix directly and
    then resets the components it baked. There is no poll to fail, no context
    to be wrong, and no CANCELLED to ignore. It also VERIFIES: if the
    components it was told to bake are not identity afterwards, it raises.
    """
    matrix = obj.matrix_basis
    translation, rotation_q, scale_v = matrix.decompose()
    bake = Matrix.Identity(4)
    if location:
        bake = Matrix.Translation(translation) @ bake
    if rotation:
        bake = bake @ rotation_q.to_matrix().to_4x4()
    if scale:
        bake = bake @ Matrix.Diagonal(scale_v).to_4x4()

    data = getattr(obj, "data", None)
    if data is not None and hasattr(data, "transform"):
        data.transform(bake)
        if hasattr(data, "update"):
            data.update()
    else:
        raise RuntimeError(
            "bg_apply needs object data that can be transformed (a mesh, a "
            "curve); %r has none" % getattr(obj, "name", obj))

    # Reset only what was baked, so a partial apply behaves like the operator's.
    if location:
        obj.location = (0.0, 0.0, 0.0)
    if rotation:
        obj.rotation_euler = (0.0, 0.0, 0.0)
        if hasattr(obj, "rotation_quaternion"):
            obj.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    if scale:
        obj.scale = (1.0, 1.0, 1.0)

    # THE ASSERTION THE OPERATOR NEVER MADE.
    after = obj.matrix_basis.decompose()
    if scale and max(abs(c - 1.0) for c in after[2]) > 1e-5:
        raise RuntimeError("bg_apply: scale did not bake on %r" % obj.name)
    if rotation and abs(after[1].angle) > 1e-5:
        raise RuntimeError("bg_apply: rotation did not bake on %r" % obj.name)
    return obj


def bg_mirror(obj, axis=0, apply=True):
    """Mirror across an axis — the cheapest symmetry there is. Applied by
    default, because a live mirror modifier that reaches automatic weighting
    doubles vertices the weighting then refuses."""
    mod = obj.modifiers.new("BGateMirror", "MIRROR")
    mod.use_axis[axis] = True
    mod.use_clip = True
    if apply:
        bg_only(obj)
        bpy.ops.object.modifier_apply(modifier=mod.name)
    return obj


def bg_smooth(obj, levels=1, shade=True):
    if levels:
        mod = obj.modifiers.new("BGateSubsurf", "SUBSURF")
        mod.levels = mod.render_levels = levels
        bg_only(obj)
        bpy.ops.object.modifier_apply(modifier=mod.name)
    if shade:
        bg_only(obj)
        bpy.ops.object.shade_smooth()
    return obj


def bg_join(objects, name):
    """Join meshes into one object, keeping every material slot.

    A LAYER SHOULD LEAVE AS ONE MESH. Automatic weighting is per-object, and a
    layer that arrives as fourteen loose islands weights fourteen times or not
    at all — which is what an unweighted-vertex report is usually telling you.
    """
    meshes = [o for o in objects if o is not None and o.type == "MESH"]
    if not meshes:
        return None
    if len(meshes) == 1:
        meshes[0].name = name
        return bg_clean(meshes[0])
    bg_deselect()
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = name
    return bg_clean(joined)


BG_MIN_BONE = 1e-4


def bg_bone_chain(name, bones):
    """An armature from [(bone, head_xyz, tail_xyz, parent_or_None, roll_deg), ...].

    The last two entries are optional: parent defaults to None (a root) and roll
    to 0 degrees. ROLL IS IN DEGREES here; bpy stores radians and this converts.

    ORDER DOES NOT MATTER. Bones are created in one pass and parented in a
    second, so a natural top-down list — Head, Neck, Chest, Spine, Hips — wires
    up exactly like a bottom-up one. The old single-pass version resolved
    parents against the bones it had already made, so top-down authoring
    produced FIVE PARENTLESS ROOTS, silently, and the rig only looked wrong once
    something was skinned to it.

    THIS FUNCTION RAISES, and that is the point — everything else in the kit
    swallows its problems, a rig cannot afford to:
      * a parent name no bone in the list defines (the silent-root bug)
      * two bones with the same name
      * head == tail — Blender DELETES zero-length bones on mode exit and says
        nothing, so the bone is simply not in the armature you get back
      * a name Blender had to change (Head -> Head.001, or a >63-char name it
        truncates) — `combine(bind='bone:Head')` matches on the name, and a
        renamed bone means that layer binds to nothing
    Every message names the bone.

    NAME THE BONES, and name them the way a retarget expects (Hips/Spine/Chest/
    Neck/Head, UpperArm.L, LowerLeg.R). An armature of Bone/Bone.001/Bone.002
    makes every rigid layer a guess and no humanoid retarget can read it.

    SET THE ROLL ON LIMBS. With every roll at 0 there is no consistent twist
    axis: both elbows bend on whatever axis fell out of the head/tail direction,
    and a Godot or Mixamo humanoid retarget produces the twisted-forearm look
    that reads as "the animation is broken".
    """
    rows = []
    seen = {}
    for index, entry in enumerate(list(bones or [])):
        row = tuple(entry)
        if len(row) < 3:
            raise ValueError(
                "bg_bone_chain: bone %d needs at least "
                "(name, head, tail); got %r" % (index, row))
        bone_name = str(row[0])
        head, tail = Vector(row[1]), Vector(row[2])
        parent = row[3] if len(row) > 3 else None
        roll = row[4] if len(row) > 4 else 0.0
        if bone_name in seen:
            raise ValueError(
                "bg_bone_chain: bone %r is defined twice (entries %d and %d) — "
                "Blender would rename the second one and bind='bone:%s' would "
                "then match the wrong bone" % (bone_name, seen[bone_name],
                                               index, bone_name))
        if (tail - head).length < BG_MIN_BONE:
            raise ValueError(
                "bg_bone_chain: bone %r has head == tail (%r) — Blender deletes "
                "zero-length bones on leaving edit mode without reporting it, "
                "so this bone would just not exist. Give it a tail."
                % (bone_name, tuple(head)))
        seen[bone_name] = index
        rows.append((bone_name, head, tail,
                     str(parent) if parent else None, float(roll or 0.0)))

    for bone_name, _head, _tail, parent, _roll in rows:
        if parent is not None and parent not in seen:
            raise ValueError(
                "bg_bone_chain: bone %r names parent %r, which no bone in the "
                "list defines. Known bones: %s. (An unresolved parent used to "
                "make the bone a silent root.)"
                % (bone_name, parent, ", ".join(sorted(seen)) or "none"))

    bpy.ops.object.armature_add(location=(0, 0, 0))
    arm = bpy.context.active_object
    arm.name = name
    arm.data.name = name + "_data"
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit = arm.data.edit_bones
        for bone in list(edit):
            edit.remove(bone)                # the default single "Bone"
        made = {}
        for bone_name, head, tail, _parent, roll in rows:
            bone = edit.new(bone_name)
            if bone.name != bone_name:
                raise ValueError(
                    "bg_bone_chain: Blender named the bone %r, not %r — "
                    "bind='bone:%s' will match nothing. Pick another name "
                    "(63 characters max, and unique in this armature)."
                    % (bone.name, bone_name, bone_name))
            bone.head, bone.tail = head, tail
            bone.roll = math.radians(roll)
            made[bone_name] = bone
        for bone_name, _head, _tail, parent, _roll in rows:
            if parent is not None:
                made[bone_name].parent = made[parent]
                made[bone_name].use_connect = False
    finally:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass

    survived = {bone.name for bone in arm.data.bones}
    lost = [row[0] for row in rows if row[0] not in survived]
    if lost:
        raise ValueError(
            "bg_bone_chain: Blender dropped %s on leaving edit mode — the "
            "armature you would have got back is missing them."
            % ", ".join(repr(bone_name) for bone_name in lost))
    return arm


def bg_finish(obj, colour=None, material="layer", unwrap=True, clean=True):
    """The four things every layer owes the pipeline, in the right order:
    clean, apply transforms, unwrap, material. Call it last in a layer script."""
    if obj is None:
        return None
    if clean:
        bg_clean(obj)
    bg_apply(obj)
    if unwrap:
        bg_unwrap(obj)
    if colour is not None:
        bg_mat(obj, material, colour)
    return obj


def bg_help():
    """Print the worked reference layer script. Read it before you write one.

    AND IF YOU ARE MODELLING A CHARACTER, READ `bg_base_help()` INSTEAD. This
    example builds a body out of primitives, which is the long way round and
    the reason the base mesh library exists: `bg_human()` hands you a correctly
    proportioned, clean, weight-ready body plus its skeleton plus a table of
    landmarks to hang clothing on. Start there and spend the run on the
    character rather than on rediscovering where a shoulder goes.
    """
    print(BG_EXAMPLE)
    print("\n# For a character, start from a base mesh instead: bg_base_help()")
    return BG_EXAMPLE
'''


# ---------------------------------------------------------------------------
# The worked example. ONE complete layer script, not pseudocode — it runs, and
# it is the only end-to-end reference an agent has. Before this existed the
# nearest thing to a worked character in this repo was a test helper that built
# a person out of a cube and three spheres.
# ---------------------------------------------------------------------------

EXAMPLE = r'''
# --- Builders Gate reference layer script: a humanoid body + its rig ---------
# Copy the SHAPE of this, not the numbers. It runs as written.
#
# UNITS ARE METRES AND THE PROPORTIONS ARE THE WHOLE JOB. A character is built
# out of one measurement — head height — and every other number is derived from
# it. An adult is 7.5 heads tall. Numbers picked per-part instead of derived is
# what produces the figure whose pose reads fine and whose hands do not.

bg_wipe()

HEIGHT = 1.75                    # the one number a person actually gives you
HEAD = HEIGHT / 7.5              # 0.233 m — everything below is a multiple

CROWN = HEIGHT
CHIN = CROWN - HEAD
SHOULDER_Z = HEIGHT * 0.82       # 1.435
CHEST_Z = HEIGHT * 0.70
WAIST_Z = HEIGHT * 0.60
HIP_Z = HEIGHT * 0.53
KNEE_Z = HEIGHT * 0.28
ANKLE_Z = HEIGHT * 0.04
ELBOW_Z = HEIGHT * 0.63
WRIST_Z = HEIGHT * 0.47          # fingertips reach mid-thigh; wrists sit here

SHOULDER_X = HEIGHT * 0.115      # HALF-widths: the mirror makes the other side
HIP_X = HEIGHT * 0.055
TORSO_W, TORSO_D = HEIGHT * 0.19, HEIGHT * 0.11

WRIST_R, UPPER_ARM_R = 0.028, 0.052
ANKLE_R, THIGH_R = 0.038, 0.082

# ---- centreline geometry ---------------------------------------------------
torso = bg_box("Torso", size=(TORSO_W, TORSO_D, SHOULDER_Z - WAIST_Z),
               at=(0, 0, (SHOULDER_Z + WAIST_Z) / 2))
pelvis = bg_box("Pelvis", size=(TORSO_W * 0.92, TORSO_D * 0.95, WAIST_Z - HIP_Z),
                at=(0, 0, (WAIST_Z + HIP_Z) / 2))
neck = bg_cyl("Neck", radius=HEAD * 0.19, depth=CHIN - SHOULDER_Z,
              at=(0, 0, (CHIN + SHOULDER_Z) / 2))
head = bg_ball("Head", radius=HEAD * 0.5, at=(0, 0, CHIN + HEAD * 0.45))

# Check how the parts sit against each other WHILE THEY ARE STILL PARTS: a join
# consumes them, and after it nothing in the scene can be compared to anything.
# This is the check no eye can do on a layer built in its own empty scene.
seam = bg_overlap(head, neck)
assert seam["intersects"], "the head is floating off the neck: " + seam["verdict"]
sunk = bg_overlap(head, torso)
assert not sunk["intersects"], "the head is inside the chest: " + sunk["verdict"]

# ---- one side, then mirror it ----------------------------------------------
# bg_taper LEAVES THE LOW END OF THE AXIS ALONE and scales the high end, so
# model the limb at its THIN end's radius and open the other end out.
arm = bg_cyl("Arm", radius=WRIST_R, depth=SHOULDER_Z - WRIST_Z,
             at=(SHOULDER_X, 0, (SHOULDER_Z + WRIST_Z) / 2), verts=12)
bg_taper(arm, top=UPPER_ARM_R / WRIST_R, axis=2)

leg = bg_cyl("Leg", radius=ANKLE_R, depth=HIP_Z - ANKLE_Z,
             at=(HIP_X, 0, (HIP_Z + ANKLE_Z) / 2), verts=12)
bg_taper(leg, top=THIGH_R / ANKLE_R, axis=2)

# The feet stand ON z = 0. A character whose soles are not on the ground plane
# floats or sinks in every engine that drops it at the origin.
foot = bg_box("Foot", size=(0.09, 0.24, ANKLE_Z * 1.5),
              at=(HIP_X, -0.06, ANKLE_Z * 0.75))

side = bg_join([arm, leg, foot], "Side")
# MIRROR REFLECTS ACROSS THE OBJECT ORIGIN, NOT THE WORLD CENTRELINE, and a
# join keeps the FIRST object's origin — here the arm's, out at +X. Bake the
# location in first or the second arm lands on top of the first.
bg_apply(side, location=True)
bg_mirror(side, axis=0)
# And mirror ONLY the off-centre parts: mirroring a torso that straddles x=0
# gives you two torsos in the same place. That reads as a clean mesh, weights
# like garbage, and doubles the tri count of every layer it touches.

body = bg_join([torso, pelvis, neck, head, side], "Body")
bg_smooth(body, levels=0, shade=True)
# levels=1 subdivides — it also pulls the surface in toward its neighbours, so
# a boxy body gets visibly smaller. Re-measure with bg_stats if you use it.

# ---- the rig ---------------------------------------------------------------
# Top-down authoring is fine: parents are wired in a second pass. Roll is in
# DEGREES, and the arms carry 90 so both elbows bend on the same axis — with
# every roll at 0 a humanoid retarget gives you the twisted-forearm look.
chain = [
    ("Head",  (0, 0, CHIN),       (0, 0, CROWN),      "Neck",  0.0),
    ("Neck",  (0, 0, SHOULDER_Z), (0, 0, CHIN),       "Chest", 0.0),
    ("Chest", (0, 0, CHEST_Z),    (0, 0, SHOULDER_Z), "Spine", 0.0),
    ("Spine", (0, 0, WAIST_Z),    (0, 0, CHEST_Z),    "Hips",  0.0),
    ("Hips",  (0, 0, HIP_Z),      (0, 0, WAIST_Z),    None,    0.0),
]
for side_name, sign in (("L", 1.0), ("R", -1.0)):
    roll = 90.0 * sign
    chain += [
        ("Shoulder." + side_name,
         (sign * HEAD * 0.15, 0, SHOULDER_Z), (sign * SHOULDER_X, 0, SHOULDER_Z),
         "Chest", roll),
        ("UpperArm." + side_name,
         (sign * SHOULDER_X, 0, SHOULDER_Z), (sign * SHOULDER_X, 0, ELBOW_Z),
         "Shoulder." + side_name, roll),
        ("LowerArm." + side_name,
         (sign * SHOULDER_X, 0, ELBOW_Z), (sign * SHOULDER_X, 0, WRIST_Z),
         "UpperArm." + side_name, roll),
        ("Hand." + side_name,
         (sign * SHOULDER_X, 0, WRIST_Z), (sign * SHOULDER_X, 0, WRIST_Z - 0.09),
         "LowerArm." + side_name, roll),
        ("UpperLeg." + side_name,
         (sign * HIP_X, 0, HIP_Z), (sign * HIP_X, 0, KNEE_Z), "Hips", 0.0),
        ("LowerLeg." + side_name,
         (sign * HIP_X, 0, KNEE_Z), (sign * HIP_X, 0, ANKLE_Z),
         "UpperLeg." + side_name, 0.0),
        ("Foot." + side_name,
         (sign * HIP_X, 0, ANKLE_Z), (sign * HIP_X, -0.17, ANKLE_Z * 0.4),
         "LowerLeg." + side_name, 0.0),
    ]
rig = bg_bone_chain("Skeleton", chain)

# ---- check it before you hand it over --------------------------------------
# These are the questions nobody can answer by looking at a layer in isolation.
stats = bg_stats(body)
print("body", stats["verts"], "verts", stats["faces"], "faces")
print("size", tuple(round(v, 3) for v in stats["dims"]), "m")
assert stats["loose"] == 0, "loose geometry: bone-heat weighting will refuse it"
assert stats["flipped"] == 0, "inverted faces: bg_clean(recalc=True)"
assert abs(stats["dims"][2] - HEIGHT) < HEAD * 0.5, "not the height asked for"
assert abs(stats["min"][2]) < 0.01, "the feet are not on the ground plane"

# ---- last, always ----------------------------------------------------------
bg_apply(body, location=True)     # origin on the world centre, at the feet
bg_finish(body, colour=(0.62, 0.47, 0.38), material="skin")
print("layer ready:", body.name, "+", rig.name,
      len(rig.data.bones), "bones")
# --- end reference script ---------------------------------------------------
'''


# The base mesh library rides along in the same injected string. It is a second
# file only because it is a second subject: the helpers above are a floor under
# whatever an agent models, `_blender_base` hands it a body it did not have to
# invent. Everything there is written against the helpers above, so it must be
# spliced in AFTER them.
from ._blender_base import BASE as _BASE
from ._blender_base import BASE_EXAMPLE as _BASE_EXAMPLE

# The examples ship INSIDE the kit as `BG_EXAMPLE` / `BG_BASE_EXAMPLE`, because
# the agent that most needs them is already inside Blender with only this
# script in front of it.
KIT = (_HELPERS
       + _BASE
       + '\n\nBG_EXAMPLE = r"""' + EXAMPLE + '"""\n'
       + '\n\nBG_BASE_EXAMPLE = r"""' + _BASE_EXAMPLE + '"""\n'
       + "# --- end kit ---------------------------------------------------------------\n")
