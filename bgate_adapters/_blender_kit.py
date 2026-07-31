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
that raises takes the whole run down with it.
"""

KIT = r'''
# --- Builders Gate modelling kit (injected) ---------------------------------
import bpy, bmesh, math
from mathutils import Vector


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


def bg_stats(obj):
    """verts / faces / loose / non-manifold — read this before you weight."""
    if obj is None or obj.type != "MESH":
        return {}
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        return {"verts": len(bm.verts), "faces": len(bm.faces),
                "loose": sum(1 for v in bm.verts if not v.link_edges),
                "nonmanifold": sum(1 for e in bm.edges if not e.is_manifold),
                "ngons": sum(1 for f in bm.faces if len(f.verts) > 4)}
    finally:
        bm.free()


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


def bg_apply(obj, location=False, rotation=True, scale=True):
    """Bake transforms into the mesh. An unapplied scale shears children and
    normals, and the readiness check will say so after export."""
    bg_only(obj)
    bpy.ops.object.transform_apply(location=location, rotation=rotation,
                                   scale=scale)
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


def bg_bone_chain(name, bones):
    """An armature from [(bone_name, head_xyz, tail_xyz, parent_or_None), ...].

    NAME THE BONES. `combine(bind='bone:Head')` matches on the name, and an
    armature of Bone/Bone.001/Bone.002 makes every rigid layer a guess.
    """
    bpy.ops.object.armature_add(location=(0, 0, 0))
    arm = bpy.context.active_object
    arm.name = name
    arm.data.name = name + "_data"
    bpy.ops.object.mode_set(mode="EDIT")
    edit = arm.data.edit_bones
    for bone in list(edit):
        edit.remove(bone)
    made = {}
    for bone_name, head, tail, parent in bones:
        bone = edit.new(bone_name)
        bone.head, bone.tail = Vector(head), Vector(tail)
        if parent and parent in made:
            bone.parent = made[parent]
            bone.use_connect = False
        made[bone_name] = bone
    bpy.ops.object.mode_set(mode="OBJECT")
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
# --- end kit ----------------------------------------------------------------
'''
