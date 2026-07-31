"""The gates themselves — every check FIRING, and every rig number MEASURED.

``test_blender.py`` covers the adapter's happy path, and it asserts check lists
are EMPTY. That is half a test suite: a check that can never fire passes those
assertions forever, and the eleven defects this file was written against all
lived in exactly that blind spot — an ``unbound`` gated so it could not report a
failed bone bind, an ``unweighted_verts`` counting vertex groups no bone reads,
a ``decal_not_fitted`` that only asked whether a modifier existed, an exposure
verdict computed over the backdrop, and four "angles" that were byte-identical.

So the rule here is the opposite one: every test either makes a check FIRE, or
measures a number that came out of Blender rather than out of the docstring.

Real Blender throughout, for the reason the sibling file gives — the risk in
this adapter is entirely at the bpy boundary, and a mock cannot tell you that
``parent_type='BONE'`` moved your cap two metres.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bgate_adapters import blender

pytestmark = pytest.mark.skipif(
    not blender.available()["available"], reason="Blender not installed"
)

WIPE = """
import bpy
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
"""


def _glb(tmp_path, name, script):
    """Build one layer as a real .glb, the way the modelling step would."""
    out = tmp_path / f"{name}.glb"
    got = blender.run_script(script, export_glb=str(out), out_dir=str(tmp_path))
    assert got["ok"] is True, got.get("error")
    assert out.exists()
    return out


def _blend(tmp_path, name, script):
    out = tmp_path / f"{name}.blend"
    got = blender.run_script(script + f"\nbpy.ops.wm.save_as_mainfile(filepath=r'{out}')",
                             out_dir=str(tmp_path))
    assert got["ok"] is True, got.get("error")
    return out


def _placements(tmp_path, glb):
    """Re-import an assembled .glb and report where every mesh ACTUALLY landed.

    Reading combine()'s own report would only prove it is self-consistent. The
    question these tests ask is what came out the other side of the exporter,
    which is the only thing the engine ever sees.
    """
    script = WIPE + f"""
import json, math
bpy.ops.import_scene.gltf(filepath=r'{glb}')
bpy.context.view_layer.update()
placed = {{}}
for obj in bpy.context.scene.objects:
    if obj.type != "MESH":
        continue
    placed[obj.name] = {{
        "at": [round(v, 3) for v in obj.matrix_world.translation],
        "scale": [round(v, 3) for v in obj.matrix_world.to_scale()],
        "rotate": [round(math.degrees(v), 1) for v in obj.matrix_world.to_euler()],
    }}
print("PLACED:" + json.dumps(placed))
"""
    got = blender.run_script(script, out_dir=str(tmp_path))
    assert got["ok"] is True, got.get("error")
    for line in got["print"].splitlines():
        if line.startswith("PLACED:"):
            placed = json.loads(line[len("PLACED:"):])
            return {n: v for n, v in placed.items() if n not in BONE_SHAPES}
    raise AssertionError(f"the re-import reported nothing: {got['print'][-500:]}")


def _kinds(result):
    return sorted({check["check"] for check in result["checks"]})


def _named(result, kind):
    return [check for check in result["checks"] if check["check"] == kind]


# The importer's own bone custom shape is a mesh too, and it is not the asset.
# MEASURED (Blender 4.5): importing any .glb carrying an armature makes
# io_scene_gltf2 build a 2x2x2 "Icosphere" at the world ORIGIN, link it into
# the scene with hide_render False, and leave it PARENTLESS — which is why it
# was the only thing the old turnaround pivot ever rotated.
BONE_SHAPES = {"Icosphere"}


# ---------------------------------------------------------------------------
# Layer placement — what "at", "rotate" and "scale" are supposed to mean
# ---------------------------------------------------------------------------

SLAB = WIPE + """
bpy.ops.mesh.primitive_cube_add(size=1)
obj = bpy.context.active_object
obj.name = "Slab"
bg_finish(obj, colour=(0.5, 0.5, 0.5), material="slab_mat")
"""

# bg_finish APPLIES transforms, so the authored scale has to be set after it —
# otherwise the fixture bakes into the mesh and cannot demonstrate a clobber.
PRESCALED_SLAB = SLAB + """
obj.scale = (2, 2, 3)
"""


class TestLayerTransform:
    """Assigning over an imported node's transform is not placing a layer.

    Every one of these came back wrong before: `obj.scale = (s, s, s)` threw
    away the layer's authored scale, and `obj.rotation_euler = ...` did nothing
    at all because the glTF importer leaves rotation_mode == 'QUATERNION'.
    """

    def test_a_layer_lands_where_at_says(self, tmp_path):
        slab = _glb(tmp_path, "slab", SLAB)
        out = tmp_path / "placed.glb"
        blender.combine([{"path": str(slab), "name": "slab", "at": [1, 2, 3]}],
                        out, timeout=300)

        placed = _placements(tmp_path, out)["Slab"]
        assert placed["at"] == pytest.approx([1.0, 2.0, 3.0], abs=1e-3)

    def test_rotate_actually_rotates(self, tmp_path):
        slab = _glb(tmp_path, "slab", SLAB)
        out = tmp_path / "turned.glb"
        blender.combine([{"path": str(slab), "name": "slab", "rotate": [0, 0, 90]}],
                        out, timeout=300)

        placed = _placements(tmp_path, out)["Slab"]
        # MEASURED as exactly 0.0 before the fix: the object's rotation_mode is
        # QUATERNION on import, and writing rotation_euler in that mode is a
        # silent no-op that changes matrix_world by nothing whatsoever.
        assert placed["rotate"][2] == pytest.approx(90.0, abs=0.5)

    def test_scale_actually_scales(self, tmp_path):
        slab = _glb(tmp_path, "slab", SLAB)
        out = tmp_path / "big.glb"
        blender.combine([{"path": str(slab), "name": "slab", "scale": 2.0}],
                        out, timeout=300)

        placed = _placements(tmp_path, out)["Slab"]
        assert placed["scale"] == pytest.approx([2.0, 2.0, 2.0], abs=1e-3)

    def test_scale_1_does_not_flatten_a_layer_that_had_its_own(self, tmp_path):
        """scale=1.0 means "leave it alone", not "reset it to one"."""
        slab = _glb(tmp_path, "slab", PRESCALED_SLAB)
        out = tmp_path / "kept.glb"
        blender.combine([{"path": str(slab), "name": "slab", "scale": 1.0}],
                        out, timeout=300)

        placed = _placements(tmp_path, out)["Slab"]
        # MEASURED as (1, 1, 1) before the fix — the assignment clobbered a
        # (2, 2, 3) layer with the default nobody asked to apply.
        assert placed["scale"] == pytest.approx([2.0, 2.0, 3.0], abs=1e-3)

    def test_transforms_compose_with_the_layers_own(self, tmp_path):
        slab = _glb(tmp_path, "slab", PRESCALED_SLAB)
        out = tmp_path / "composed.glb"
        blender.combine([{"path": str(slab), "name": "slab", "scale": 2.0,
                          "at": [1, 0, 0], "rotate": [0, 0, 90]}],
                        out, timeout=300)

        placed = _placements(tmp_path, out)["Slab"]
        assert placed["scale"] == pytest.approx([4.0, 4.0, 6.0], abs=1e-3)
        assert placed["at"] == pytest.approx([1.0, 0.0, 0.0], abs=1e-3)
        assert placed["rotate"][2] == pytest.approx(90.0, abs=0.5)


# ---------------------------------------------------------------------------
# Binding — where a bone-parented layer ends up, and what fails to bind
# ---------------------------------------------------------------------------

RIG = WIPE + """
bpy.ops.object.armature_add()
bpy.context.active_object.name = "Skeleton"
"""

# The same rig with a bone that cannot deform anything. It is still a bone, it
# still takes automatic weights, and the vertices it holds still never move.
RIG_NO_DEFORM = RIG + """
bpy.context.active_object.data.bones[0].use_deform = False
"""

BODY = WIPE + """
bpy.ops.mesh.primitive_cube_add(size=2)
obj = bpy.context.active_object
obj.name = "Body"
bg_finish(obj, colour=(0.40, 0.11, 0.64), material="body_mat")
"""

CAP = WIPE + """
bpy.ops.mesh.primitive_uv_sphere_add(radius=1.2, location=(0, 0, 2))
obj = bpy.context.active_object
obj.name = "Cap"
bg_finish(obj, colour=(0.04, 0.72, 0.74), material="cap_mat")
"""


class TestRigidBind:
    def test_a_bone_bound_layer_keeps_its_authored_position(self, tmp_path):
        """THE MEASUREMENT THIS TEST EXISTS FOR.

        parent_type='BONE' places a child relative to the bone's TAIL, in
        bone-local space where the bone's +Y runs along its own length.
        MEASURED with the old matrix_parent_inverse: a cap authored at (0, 0, 2)
        on the default bone (head (0,0,0), tail (0,0,1)) exported at
        (0, -2, 1), rotated 90 degrees — a cap sitting beside the character's
        knee, in a file that reported a clean assembly.
        """
        parts = [{"path": str(_blend(tmp_path, "skeleton", RIG)), "name": "skeleton"},
                 {"path": str(_glb(tmp_path, "cap", CAP)), "name": "cap",
                  "bind": "bone:Bone"}]
        out = tmp_path / "player.glb"
        got = blender.combine(parts, out, rig="skeleton", timeout=420)

        assert got["glb"]["exported"] is True, got.get("error")
        placed = _placements(tmp_path, out)
        assert placed["Cap"]["at"] == pytest.approx([0.0, 0.0, 2.0], abs=1e-2)
        # ...and no spurious rotation, which is the other half of what the bone
        # tail convention was doing to it.
        assert placed["Cap"]["rotate"] == pytest.approx([0.0, 0.0, 0.0], abs=0.5)


class TestUnboundFires:
    def test_a_bone_bind_that_did_not_resolve_is_a_CHECK_not_a_note(self, tmp_path):
        """A bone: bind naming a bone the armature does not have used to leave
        nothing but a note, and the `unbound` check was gated on bind ==
        'deform' so it could not report it. Notes are prose; checks are the
        thing a caller can act on."""
        parts = [{"path": str(_blend(tmp_path, "skeleton", RIG)), "name": "skeleton"},
                 {"path": str(_glb(tmp_path, "cap", CAP)), "name": "cap",
                  "bind": "bone:Head"}]
        got = blender.combine(parts, tmp_path / "player.glb", rig="skeleton",
                              timeout=420)

        unbound = _named(got, "unbound")
        assert unbound, f"nothing reported the failed bone bind: {got['checks']}"
        assert unbound[0]["layer"] == "cap"
        assert unbound[0]["asked"] == "bone:Head"

    def test_a_layer_that_did_bind_reports_no_unbound(self, tmp_path):
        parts = [{"path": str(_blend(tmp_path, "skeleton", RIG)), "name": "skeleton"},
                 {"path": str(_glb(tmp_path, "cap", CAP)), "name": "cap",
                  "bind": "bone:Bone"}]
        got = blender.combine(parts, tmp_path / "player.glb", rig="skeleton",
                              timeout=420)

        # The rig layer itself binds nothing and must not be accused of it.
        assert not _named(got, "unbound"), got["checks"]


class TestUnweightedFires:
    def test_weight_on_a_bone_that_cannot_deform_is_not_weight(self, tmp_path):
        """MEASURED: a cube carrying one vertex group weighted 1.0 reported ZERO
        unweighted vertices, because the count asked `any(g.weight > 0 for g in
        v.groups)` — any vertex group at all. A group no deform bone reads moves
        nothing, and the mesh tears at the rest pose exactly as if it were bare.
        """
        parts = [{"path": str(_blend(tmp_path, "skeleton", RIG_NO_DEFORM)),
                  "name": "skeleton"},
                 {"path": str(_glb(tmp_path, "body", BODY)), "name": "body",
                  "bind": "deform"}]
        got = blender.combine(parts, tmp_path / "player.glb", rig="skeleton",
                              timeout=420)

        loose = _named(got, "unweighted_verts")
        assert loose, f"a non-deform bind reported nothing: {got['checks']}"
        # The cube is 8 vertices and every one of them is held by a bone that
        # cannot move it.
        assert loose[0]["count"] == 8
        assert "deform weight" in loose[0]["detail"]

    def test_a_real_deform_bind_reports_none(self, tmp_path):
        parts = [{"path": str(_blend(tmp_path, "skeleton", RIG)), "name": "skeleton"},
                 {"path": str(_glb(tmp_path, "body", BODY)), "name": "body",
                  "bind": "deform"}]
        got = blender.combine(parts, tmp_path / "player.glb", rig="skeleton",
                              timeout=420)

        assert not _named(got, "unweighted_verts"), got["checks"]


# ---------------------------------------------------------------------------
# Decals — the shrinkwrap has to have something to move
# ---------------------------------------------------------------------------

# Tangent to the cap and big enough that four corners cannot describe it: this
# is the shape whose middle sinks into the surface it is supposed to sit on.
TANGENT_LOGO = WIPE + """
import math
bpy.ops.mesh.primitive_plane_add(size=1.2, location=(0, -1.2, 2),
                                 rotation=(math.radians(90), 0, 0))
obj = bpy.context.active_object
obj.name = "Logo"
bg_finish(obj, colour=(0.90, 0.90, 0.10), material="logo_mat")
"""


class TestDecalFit:
    def _parts(self, tmp_path):
        return [{"path": str(_glb(tmp_path, "cap", CAP)), "name": "cap"},
                {"path": str(_glb(tmp_path, "logo", TANGENT_LOGO)), "name": "logo",
                 "decal_on": "cap"}]

    def test_a_subdivided_decal_hugs_the_surface(self, tmp_path):
        got = blender.combine(self._parts(tmp_path), tmp_path / "fitted.glb",
                              timeout=420)
        assert not _named(got, "decal_not_fitted"), got["checks"]

    def test_a_decal_too_coarse_to_follow_the_curve_is_CAUGHT(self, tmp_path, monkeypatch):
        """Shrinkwrap only moves the vertices a mesh already has.

        MEASURED (Blender 4.5) with subdivision disabled: this 4-vertex plane
        settled 0.175 away from the cap against a 0.0249 budget — its corners on
        the surface and its face buried inside it. The old check asked only
        whether a SHRINKWRAP modifier existed, so it passed.
        """
        monkeypatch.setattr(blender, "DECAL_EDGE_RATIO", 100.0)
        got = blender.combine(self._parts(tmp_path), tmp_path / "sagging.glb",
                              timeout=420)

        unfitted = _named(got, "decal_not_fitted")
        assert unfitted, f"a sagging decal passed: {got['checks']}"
        assert unfitted[0]["gap"] > unfitted[0]["tolerance"]
        assert unfitted[0]["layer"] == "logo"

    def test_a_decal_target_with_no_mesh_is_still_caught(self, tmp_path):
        parts = [{"path": str(_blend(tmp_path, "skeleton", RIG)), "name": "skeleton"},
                 {"path": str(_glb(tmp_path, "logo", TANGENT_LOGO)), "name": "logo",
                  "decal_on": "skeleton"}]
        got = blender.combine(parts, tmp_path / "nothing.glb", timeout=300)

        assert _named(got, "decal_not_fitted"), got["checks"]


# ---------------------------------------------------------------------------
# What combine() does with the runner's own findings
# ---------------------------------------------------------------------------

# Carried as a .blend on purpose: glTF has no n-gons, so a .glb round trip
# TRIANGULATES this on the way out and the ngons finding never reaches the
# assembly. MEASURED — the same fixture through .glb reports no_uv and
# no_material only; through .blend it reports all three.
BARE = WIPE + """
mesh = bpy.data.meshes.new("Bare")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0.5, 2, 0)],
                 [], [(0, 1, 2, 3, 4)])
mesh.update()
bpy.context.collection.objects.link(bpy.data.objects.new("Bare", mesh))
"""


class TestIssuesArePromoted:
    """MEASURED: an end-to-end run returned ok=True, checks=[], warnings=[] over
    a .glb whose materials were None and whose image and texture counts were
    both zero. `result["issues"]` held every one of those findings; combine()
    merged the dict and then reported the two keys a caller actually reads as
    empty."""

    def test_a_layer_with_no_material_and_no_uv_reaches_checks(self, tmp_path):
        got = blender.combine([{"path": str(_blend(tmp_path, "bare", BARE)),
                                "name": "bare"}],
                              tmp_path / "bare_out.glb", timeout=300)

        kinds = _kinds(got)
        assert "no_material" in kinds, got["checks"]
        assert "no_uv" in kinds, got["checks"]
        assert "ngons" in kinds, got["checks"]
        # Named by LAYER, not just by object — the point of layers is that one
        # of them can be re-run alone.
        assert _named(got, "no_material")[0]["layer"] == "bare"

    def test_promoted_issues_also_reach_warnings(self, tmp_path):
        got = blender.combine([{"path": str(_blend(tmp_path, "bare", BARE)),
                                "name": "bare"}],
                              tmp_path / "bare_out.glb", timeout=300)

        assert any("no_material" in w for w in got["warnings"]), got["warnings"]

    def test_a_flat_untextured_assembly_is_not_ok(self, tmp_path):
        """21 materials and ZERO images is a grey blob, and it used to pass."""
        got = blender.combine([{"path": str(_glb(tmp_path, "body", BODY)),
                                "name": "body"}],
                              tmp_path / "flat.glb", timeout=300)

        assert got["materials"], "the fixture was supposed to carry a material"
        assert got["images"] == []
        assert got["ok"] is False
        assert "no_textures" in _kinds(got)
        # And it says why, rather than leaving a caller to guess from ok=False.
        assert "image texture" in (got["error"] or "")

    def test_a_textured_assembly_passes(self, tmp_path):
        from PIL import Image

        model = _glb(tmp_path, "cap", CAP)
        image = tmp_path / "cap_texture.png"
        Image.new("RGB", (64, 64), (102, 28, 163)).save(image)
        textured = tmp_path / "cap_textured.glb"
        blender.apply_texture(model, image, textured, timeout=300)

        got = blender.combine([{"path": str(textured), "name": "cap"}],
                              tmp_path / "ok.glb", timeout=300)

        assert got["images"], got.get("error")
        assert got["ok"] is True, got.get("error")
        assert "no_textures" not in _kinds(got)
        assert "no_materials" not in _kinds(got)


# ---------------------------------------------------------------------------
# The turnaround rig — four ANGLES, one exposure, wherever the subject is
# ---------------------------------------------------------------------------

def _figure(size, at):
    """A deliberately ASYMMETRIC subject, built to the base's own facing.

    A cube-and-sphere on the axis renders the same picture from the front, the
    side and the back, which would make the four-distinct-frames test pass on a
    rig that never rotated anything. So this one has a NOSE on its +Y side —
    the way bg_human faces — and an arm out its -X, which is the same figure's
    own left. Four angles then owe four different images, AND the nose says
    which of them is which: a label is only right if "front" is the frame the
    nose is in.
    """
    x, y, z = at
    return WIPE + """
bpy.ops.mesh.primitive_cube_add(size=%(size)f, location=(%(x)f, %(y)f, %(z)f))
torso = bpy.context.active_object
torso.name = "Torso"
bg_finish(torso, colour=(0.40, 0.11, 0.64), material="torso_mat")
bpy.ops.mesh.primitive_uv_sphere_add(radius=%(nose)f, location=(%(x)f, %(ny)f, %(nz)f))
nose = bpy.context.active_object
nose.name = "Nose"
bg_finish(nose, colour=(0.90, 0.30, 0.10), material="nose_mat")
bpy.ops.mesh.primitive_cube_add(size=%(arm)f, location=(%(ax)f, %(y)f, %(z)f))
arm = bpy.context.active_object
arm.name = "Arm"
bg_finish(arm, colour=(0.04, 0.72, 0.74), material="arm_mat")
""" % {"size": size, "x": x, "y": y, "z": z,
       "nose": size * 0.28, "ny": y + size * 0.6, "nz": z + size * 0.3,
       "arm": size * 0.5, "ax": x - size * 0.8}


def _digest(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _parts_in_frame(path):
    """Count the nose's pixels and the arm's, over the SUBJECT only.

    Hue, not RGB distance: the rig renders through AgX, so the orange nose
    lands around (224, 192, 176) and the teal arm around (176, 208, 208) —
    both far from the colours they were authored in, and both still
    unambiguously warm and cool against the lavender torso.
    """
    from PIL import Image

    image = Image.open(path).convert("RGBA")
    width, height = image.size
    counts = {"nose": 0, "arm": 0, "subject": 0}
    weighted = {"nose": 0, "arm": 0}
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = image.getpixel((x, y))
            if alpha < 8:
                continue
            counts["subject"] += 1
            if red > green > blue and red - blue > 24:
                counts["nose"] += 1
                weighted["nose"] += x
            elif blue > red and green > red and green - red > 12:
                counts["arm"] += 1
                weighted["arm"] += x
    for part in ("nose", "arm"):
        counts[part + "_x"] = (weighted[part] / counts[part] / width
                               if counts[part] else None)
    return counts


# Turnarounds are the expensive tests in this file — eight renders each, plus a
# GPU warmup. These are shared across the class rather than rebuilt per test.
@pytest.fixture(scope="module")
def _shared(tmp_path_factory):
    root = tmp_path_factory.mktemp("turnaround")
    blender.warmup("BLENDER_EEVEE_NEXT", out_dir=str(root))
    built = {}
    for name, size, at in (("small", 0.3, (0.0, 0.0, 0.0)),
                           ("large", 10.0, (0.0, 0.0, 0.0)),
                           ("offcentre", 2.0, (3.0, -5.0, 0.0))):
        model = _glb(root, name, _figure(size, at))
        built[name] = blender.turnaround(model, root, stem=name, size=(256, 320),
                                         timeout=600)
    return root, built


class TestTurnaroundFraming:
    def test_the_four_frames_are_pairwise_distinct(self, _shared):
        """MEASURED: a combine() product re-imports under an "Assembled" root
        with its layers parented to an armature, so NO mesh was parentless,
        nothing joined the rotating pivot, and all four "angles" came back as
        byte-identical fronts that four separate renders had been paid for."""
        _root, built = _shared
        renders = built["small"]["renders"]
        assert len(renders) == len(blender.TURNAROUND_ANGLES)

        digests = [_digest(Path(r["path"])) for r in renders]
        assert len(set(digests)) == len(digests), \
            "the turnaround rendered the same frame more than once"

    def test_the_rotated_view_is_framed_further_back(self, _shared):
        """A bounding box turned 45 degrees is up to 1.41x as wide as it is
        square-on, against the 1.15x of visible width a fixed distance bought —
        which is why the threequarter view was the one that came back cropped.
        """
        _root, built = _shared
        by_label = {r["label"]: r for r in built["small"]["renders"]}
        assert by_label["threequarter"]["distance"] > by_label["front"]["distance"]

    def test_the_distance_depends_on_the_turn_and_not_on_the_label(self, _shared):
        """front and back are 180 apart and the bbox offsets are symmetric about
        the centre, so the fit MUST come out identical. It is the cheap proof
        that re-labelling the angles left the camera geometry alone."""
        _root, built = _shared
        by_label = {r["label"]: r for r in built["small"]["renders"]}
        assert by_label["front"]["distance"] == pytest.approx(
            by_label["back"]["distance"], abs=1e-6)


class TestTurnaroundLabelsTheFace:
    """A DISTINCT FRAME IS NOT A CORRECT ONE.

    The sibling test above only asks that the four frames differ, and they
    differ under any rotation at all — including the one that renders the back
    and files it under "front". The base faces +Y (BG_FORWARD), the rig's
    camera stands at -Y, so "front" is the 180 frame; this asks the renders
    whether that is what actually happened.
    """

    def test_the_front_frame_shows_the_face_and_the_back_frame_does_not(self, _shared):
        _root, built = _shared
        frames = {r["label"]: _parts_in_frame(Path(r["matte"]))
                  for r in built["small"]["renders"]}

        # The nose is a sphere on the +Y side, clear of the torso from in front
        # and completely behind it from the back. MEASURED at 256x320:
        # front 3552 nose pixels, back 0.
        assert frames["front"]["nose"] > 500, frames
        assert frames["back"]["nose"] == 0, frames
        assert frames["side"]["nose"] > 200, frames

    def test_the_labels_are_not_merely_180_degrees_out(self, _shared):
        """The failure this catches is the plausible one: swapping front and
        back but leaving threequarter and side where they were, which shows a
        face in the front frame and a back-three-quarter beside it."""
        _root, built = _shared
        frames = {r["label"]: _parts_in_frame(Path(r["matte"]))
                  for r in built["small"]["renders"]}

        # Three-quarter is between front and side, so the nose is visible but
        # partly turned away — fewer pixels than either neighbour.
        assert 0 < frames["threequarter"]["nose"] < frames["front"]["nose"], frames

    def test_the_figures_left_arm_reads_on_the_correct_side_of_the_frame(self, _shared):
        """The other half of the convention. The arm is on the figure's own
        left (-X); a figure facing the camera shows its left arm on the
        VIEWER'S RIGHT, and turning it round moves the arm to the other side of
        the frame. Get BG_SIDES backwards and this is what goes wrong."""
        _root, built = _shared
        frames = {r["label"]: _parts_in_frame(Path(r["matte"]))
                  for r in built["small"]["renders"]}

        assert frames["front"]["arm_x"] > 0.55, frames["front"]
        assert frames["back"]["arm_x"] < 0.45, frames["back"]
        # Profile: the arm is directly behind the torso and invisible.
        assert frames["side"]["arm"] == 0, frames["side"]

    def test_a_subject_nowhere_near_the_origin_is_still_in_frame(self, _shared):
        """The camera used to be placed at (0, -reach * 2.4, centre[2]): X
        hardcoded to zero and Y measured from the world origin, so only Z was
        ever centred. A subject centred at y=-5 with reach 1 sat BEHIND the
        camera and rendered empty grey — which then PASSED the exposure check,
        because an empty grey frame is neither blown out nor dark."""
        _root, built = _shared
        got = built["offcentre"]

        assert got["centre"][0] == pytest.approx(3.0, abs=1.5)
        assert got["centre"][1] == pytest.approx(-5.0, abs=1.5)
        assert got["ok"] is True, got.get("error")
        for render in got["renders"]:
            assert render["subject"] > 0.05, render


class TestTurnaroundExposure:
    def test_exposure_does_not_depend_on_how_big_the_subject_is(self, _shared):
        """energy = 220 * max(reach, 1.0) against a standoff proportional to
        reach makes irradiance go as 32/reach: a 0.3 m prop took ~360 W/m2 where
        a 2 m character took ~16, a 20x swing, and the clamp froze the power
        outright below a metre while the lights kept closing in. Squared holds
        it flat, and these two models differ in size by 33x."""
        _root, built = _shared
        small, large = built["small"], built["large"]

        assert small["ok"] is True, small.get("error")
        assert large["ok"] is True, large.get("error")
        for a, b in zip(small["renders"], large["renders"]):
            assert b["mean"] == pytest.approx(a["mean"], abs=8.0), (a, b)
            # The framing is the other half of it: the same subject must fill
            # the same share of the frame at any scale.
            assert b["subject"] == pytest.approx(a["subject"], abs=0.02), (a, b)

    def test_every_frame_writes_a_matte_to_be_judged_from(self, _shared):
        _root, built = _shared
        for render in built["small"]["renders"]:
            assert Path(render["path"]).is_file()
            assert Path(render["matte"]).is_file(), render


# ---------------------------------------------------------------------------
# The exposure verdict itself — over the SUBJECT, not over the backdrop
# ---------------------------------------------------------------------------

MID_GREY = (114, 114, 114)


def _subject_on_grey(path, size, subject_rgb, coverage=0.2):
    """A frame's worth of matte: an opaque-looking backdrop with alpha 0, and a
    subject block covering `coverage` of it with alpha 255."""
    from PIL import Image

    image = Image.new("RGBA", size, MID_GREY + (0,))
    band = max(1, round(size[1] * coverage))
    top = (size[1] - band) // 2
    image.paste(Image.new("RGBA", (size[0], band), subject_rgb + (255,)), (0, top))
    image.save(path)
    return path


class TestExposureIsMeasuredOnTheSubject:
    def test_a_blown_white_figure_on_a_grey_field_is_REJECTED(self, tmp_path):
        """THE MEASUREMENT THAT MADE THE OLD CHECK USELESS.

        The rig paints an opaque linear (0.28, 0.29, 0.32) x 0.6 backdrop, which
        encodes to about 114/255, and a humanoid covers 15-25% of a portrait
        frame. Run over EVERY pixel, a figure blown to solid white scored
        blown ~= 0.20 against a 0.35 threshold and sailed through.
        """
        frame = _subject_on_grey(tmp_path / "blown.png", (128, 160), (255, 255, 255))
        verdict = blender._exposure_report(frame)

        assert verdict["checked"] is True
        assert verdict["blown"] == pytest.approx(1.0)
        assert verdict["ok"] is False
        assert "blown out" in verdict["verdict"]

    def test_the_same_frame_measured_over_everything_would_have_passed(self):
        """Proof the fixture above is the real failing case and not a strawman:
        20% coverage is under BLOWN_FRACTION, so the whole-frame statistic this
        check used to compute could not have failed it."""
        assert 0.2 < blender.BLOWN_FRACTION

    def test_a_pure_black_subject_is_rejected_even_on_a_grey_field(self, tmp_path):
        """mean over the whole frame could never reach DARK_MEAN either: a
        completely black render still scored about 97 against a threshold of 24,
        because it was mostly measuring the backdrop."""
        frame = _subject_on_grey(tmp_path / "black.png", (128, 160), (2, 2, 2))
        verdict = blender._exposure_report(frame)

        assert verdict["ok"] is False
        assert "too dark" in verdict["verdict"]

    def test_a_railed_colour_channel_is_visible_to_the_check(self, tmp_path):
        """Saturated (255, 0, 0) has a luma of 76 — perfectly respectable, and
        every bit of red detail in it is gone. Luma alone cannot see this."""
        frame = _subject_on_grey(tmp_path / "red.png", (128, 160), (255, 0, 0))
        verdict = blender._exposure_report(frame)

        assert verdict["mean"] == pytest.approx(76.0, abs=1.0)
        assert verdict["clipped"] == pytest.approx(1.0)
        assert verdict["ok"] is False
        assert "clipped" in verdict["verdict"]

    def test_a_well_exposed_subject_passes(self, tmp_path):
        frame = _subject_on_grey(tmp_path / "good.png", (128, 160), (150, 120, 190))
        verdict = blender._exposure_report(frame)

        assert verdict["ok"] is True, verdict
        assert verdict["subject"] == pytest.approx(0.2, abs=0.02)

    def test_an_empty_matte_is_a_failure_not_a_pass(self, tmp_path):
        from PIL import Image

        frame = tmp_path / "empty.png"
        Image.new("RGBA", (64, 64), MID_GREY + (0,)).save(frame)
        verdict = blender._exposure_report(frame)

        assert verdict["ok"] is False
        assert "nothing" in verdict["verdict"]

    def test_an_image_with_no_alpha_is_all_subject(self, tmp_path):
        """The sibling suite judges plain L-mode images, and they still mean
        what they always meant: no transparency, no background to exclude."""
        from PIL import Image

        frame = tmp_path / "white.png"
        Image.new("L", (64, 64), 255).save(frame)
        verdict = blender._exposure_report(frame)

        assert verdict["subject"] == pytest.approx(1.0)
        assert verdict["ok"] is False


# ---------------------------------------------------------------------------
# The stop-signal has to say something
# ---------------------------------------------------------------------------

class TestTurnaroundStatesItsReason:
    """ok=False with error=None is rewritten downstream into "the call failed
    without stating a reason", which turns this function's one mechanical
    stop-signal into what looks to an agent like a broken tool."""

    def _canned(self, monkeypatch, tmp_path, frames):
        report = {"renders": frames, "reach": 1.0, "centre": [0, 0, 0]}
        monkeypatch.setattr(blender, "run_script", lambda *a, **k: {
            "ok": True, "error": None,
            "print": "BGATE_TURNAROUND:" + json.dumps(report)})
        model = tmp_path / "model.glb"
        model.write_bytes(b"glb")
        return blender.turnaround(model, tmp_path, stem="t")

    def test_a_failing_frame_names_itself_in_error(self, tmp_path, monkeypatch):
        good = _subject_on_grey(tmp_path / "g.png", (64, 80), (150, 120, 190))
        bad = _subject_on_grey(tmp_path / "b.png", (64, 80), (255, 255, 255))
        frames = [{"label": "front", "degrees": 0, "path": str(good), "matte": str(good)},
                  {"label": "side", "degrees": 90, "path": str(bad), "matte": str(bad)}]

        got = self._canned(monkeypatch, tmp_path, frames)

        assert got["ok"] is False
        assert got["error"], "ok=False with no reason is the bug this guards"
        assert "1 of 2 frames unreadable" in got["error"]
        assert "side" in got["error"]
        assert "blown out" in got["error"]

    def test_all_frames_readable_leaves_error_alone(self, tmp_path, monkeypatch):
        good = _subject_on_grey(tmp_path / "g.png", (64, 80), (150, 120, 190))
        frames = [{"label": "front", "degrees": 0, "path": str(good), "matte": str(good)}]

        got = self._canned(monkeypatch, tmp_path, frames)

        assert got["ok"] is True
        assert not got["error"]

    def test_no_frames_at_all_still_says_why(self, tmp_path, monkeypatch):
        got = self._canned(monkeypatch, tmp_path, [])

        assert got["ok"] is False
        assert "no frames" in got["error"]
