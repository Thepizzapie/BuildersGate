"""The last mile: a .glb, in the engine, with a collider, in a scene, on screen.

Everything before this file stopped at the .glb and called it delivery. The only
"look at it" was an EEVEE render in Blender, of a Blender scene, under Blender
lights — so the whole class of defects that happens BETWEEN the exporter and the
player was invisible by construction. These tests are the engine's opinion.

Two layers, deliberately:

  * The pure-text tests run everywhere in milliseconds. They pin the shapes that
    are easy to get subtly wrong on a refactor — the Transform3D component
    order, the `PATH:` key in `_subresources`, the capsule/height relationship
    Godot rejects at load — without needing an engine.

  * The `slow` tests build a REAL rigged, shape-keyed, textured, animated
    character in Blender and push it through a REAL Godot import. Those are the
    ones that actually caught the bugs this file exists for, and none of them
    can be faked: the failures were all in what the tools do, not in what we
    believed they do.
"""
from __future__ import annotations

import json
import math
import re

import pytest

from bgate_adapters import blender, godot

HAS_GODOT = godot.available()["available"]
HAS_BLENDER = blender.available()["available"]

needs_godot = pytest.mark.skipif(not HAS_GODOT, reason="Godot not installed")
needs_blender = pytest.mark.skipif(not HAS_BLENDER, reason="Blender not installed")


# ---------------------------------------------------------------------------
# A character worth testing: rigged, skinned, shape-keyed, textured, animated,
# and carrying a modifier. Every one of those is a thing that used to be lost
# somewhere between Blender and Godot.
# ---------------------------------------------------------------------------

CHARACTER = """
import bpy, math

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)

scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 24

arm_data = bpy.data.armatures.new("HeroArmature")
arm = bpy.data.objects.new("HeroArmature", arm_data)
scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
root = arm_data.edit_bones.new("Root")
root.head, root.tail = (0, 0, 0), (0, 0, 0.9)
spine = arm_data.edit_bones.new("Spine")
spine.head, spine.tail = (0, 0, 0.9), (0, 0, 1.8)
spine.parent = root
bpy.ops.object.mode_set(mode="OBJECT")

bpy.ops.mesh.primitive_cylinder_add(radius=0.3, depth=1.8, vertices=12,
                                    location=(0, 0, 0.9))
body = bpy.context.active_object
body.name = "HeroBody"
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.uv.smart_project(angle_limit=math.radians(66))
bpy.ops.object.mode_set(mode="OBJECT")

sub = body.modifiers.new("Subsurf", "SUBSURF")
sub.levels = sub.render_levels = 1

lower = body.vertex_groups.new(name="Root")
upper = body.vertex_groups.new(name="Spine")
for v in body.data.vertices:
    (lower if v.co.z < 0 else upper).add([v.index], 1.0, "REPLACE")
body.parent = arm
body.modifiers.new("Armature", "ARMATURE").object = arm

body.shape_key_add(name="Basis", from_mix=False)
smile = body.shape_key_add(name="Smile", from_mix=False)
for i, v in enumerate(smile.data):
    v.co.x += 0.12 if i % 2 == 0 else -0.05
puff = body.shape_key_add(name="Puff", from_mix=False)
for v in puff.data:
    v.co.y += 0.08

img = bpy.data.images.new("HeroAlbedo", 64, 64)
img.generated_type = "COLOR_GRID"
mat = bpy.data.materials.new("HeroSkin")
mat.use_nodes = True
tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
tex.image = img
mat.node_tree.links.new(tex.outputs["Color"],
                        mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
body.data.materials.append(mat)

bpy.context.view_layer.objects.active = arm
arm.animation_data_create()
arm.animation_data.action = bpy.data.actions.new("Idle")
bpy.ops.object.mode_set(mode="POSE")
pb = arm.pose.bones["Spine"]
pb.rotation_mode = "XYZ"
for frame, angle in ((1, 0.0), (12, 0.25), (24, 0.0)):
    scene.frame_set(frame)
    pb.rotation_euler = (angle, 0, 0)
    pb.keyframe_insert(data_path="rotation_euler", frame=frame)
bpy.ops.object.mode_set(mode="OBJECT")
scene.frame_set(1)
__SCALE__
"""

# The same character, forty times too big — and NOT by scaling the mesh. The
# factor lives in the node transform, exactly where a real unit mistake lives
# and exactly where the old local-aabb check could not see it.
GIANT = CHARACTER.replace("__SCALE__", "arm.scale = (40.0, 40.0, 40.0)")
CHARACTER = CHARACTER.replace("__SCALE__", "")

# An unskinned prop with a textured material. Colliders for this one have to
# come from the .import file — a .glb cannot carry one.
PROP = """
import bpy
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0.5))
crate = bpy.context.active_object
crate.name = "Crate"
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode="OBJECT")
img = bpy.data.images.new("CrateAlbedo", 32, 32)
img.generated_type = "COLOR_GRID"
mat = bpy.data.materials.new("CrateWood")
mat.use_nodes = True
tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
tex.image = img
mat.node_tree.links.new(tex.outputs["Color"],
                        mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
crate.data.materials.append(mat)
"""

# A material with a NAME and no image. This is the "21 materials and ZERO
# images" disaster in miniature — it passed the old check with flying colours,
# because the old check only ever read `mat.resource_name`.
UNTEXTURED = """
import bpy
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=1.0)
ob = bpy.context.active_object
ob.data.materials.append(bpy.data.materials.new("Emberglass"))
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode="OBJECT")
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Export the fixtures once; a Blender launch costs more than every
    assertion in this file put together."""
    if not HAS_BLENDER:
        pytest.skip("Blender not installed")
    out = tmp_path_factory.mktemp("glb")
    made = {}
    for name, script in (("hero", CHARACTER), ("giant", GIANT),
                         ("crate", PROP), ("bare", UNTEXTURED)):
        path = out / f"{name}.glb"
        got = blender.run_script(script, export_glb=str(path), kit=False,
                                 record=False, timeout=300)
        assert got["ok"], (name, got.get("error"), got.get("traceback"))
        assert got["glb"]["exported"], (name, got["glb"])
        made[name] = (str(path), got["glb"])
    return made


@pytest.fixture(scope="module")
def project3d(tmp_path_factory):
    from bgate_core import scaffold

    target = tmp_path_factory.mktemp("game3d") / "proj"
    scaffold.new_project(str(target), "Delivery", kind="3d")
    return str(target)


# ---------------------------------------------------------------------------
# 6. Shape keys and an action surviving the export
# ---------------------------------------------------------------------------

def _glb_json(path: str) -> dict:
    """The .glb's own JSON chunk. No Blender, no Godot, no opinion."""
    import struct

    with open(path, "rb") as fh:
        magic, _ver, _total = struct.unpack("<4sII", fh.read(12))
        assert magic == b"glTF"
        length, _ctype = struct.unpack("<II", fh.read(8))
        return json.loads(fh.read(length).decode("utf-8"))


class TestBlenderExportKeepsWhatTheEngineNeeds:
    """`export_apply=True` was forced on every export on this path, and
    Blender's own wording for that flag is "prevents exporting shape keys". So
    blend shapes were structurally impossible: no blink, no facial expression,
    no corrective shape, ever — and silently, with a cheerful ok=True."""

    @pytest.mark.slow
    @needs_blender
    def test_shape_keys_reach_the_glb(self, built):
        path, _ = built["hero"]
        gltf = _glb_json(path)
        targets = max(len(prim.get("targets") or [])
                      for mesh in gltf["meshes"]
                      for prim in mesh["primitives"])
        assert targets == 2, "the two shape keys did not survive the exporter"
        names = [mesh.get("extras", {}).get("targetNames")
                 for mesh in gltf["meshes"]]
        assert ["Smile", "Puff"] in names, names

    @pytest.mark.slow
    @needs_blender
    def test_the_action_reaches_the_glb(self, built):
        path, _ = built["hero"]
        gltf = _glb_json(path)
        assert [a["name"] for a in gltf.get("animations", [])] == ["Idle"]
        assert gltf["animations"][0]["channels"], "an animation with no channels"

    @pytest.mark.slow
    @needs_blender
    def test_the_skin_reaches_the_glb(self, built):
        path, _ = built["hero"]
        gltf = _glb_json(path)
        assert gltf.get("skins"), "no skin — the rig did not export"
        assert gltf["skins"][0]["joints"], "a skin with no joints"

    @pytest.mark.slow
    @needs_blender
    def test_export_apply_is_off_only_when_shape_keys_force_it(self, built):
        """The safe half of the fix. A scene with NO shape keys must behave
        exactly as it always did — modifiers applied by the exporter."""
        _, hero = built["hero"]
        _, crate = built["crate"]
        assert hero["export_apply"] is False
        assert hero["shape_key_meshes"] == ["HeroBody"]
        assert crate["export_apply"] is True
        assert crate["applied_modifiers"] is True

    @pytest.mark.slow
    @needs_blender
    def test_a_modifier_it_cannot_apply_is_reported_not_swallowed(self, built):
        """Blender itself refuses to apply a modifier to a shape-keyed mesh.
        That is a real trade — geometry detail for blend shapes — and the human
        has to be told which one we kept, not left to find out in the engine."""
        _, hero = built["hero"]
        skipped = hero["modifiers"]["skipped"]
        assert any("Subsurf" in s["modifier"] for s in skipped), skipped
        assert all("shape keys" in s["reason"] for s in skipped
                   if "Subsurf" in s["modifier"])

    @pytest.mark.slow
    @needs_blender
    def test_animation_and_morph_flags_are_explicit(self, built):
        """They happen to default True on 4.5. "Happen to" is how a character
        ships as a T-pose statue after a version bump."""
        _, hero = built["hero"]
        assert hero["exported_animations"] is True
        assert hero["exported_morph_targets"] is True
        assert hero["actions"] == ["Idle"]
        assert hero["armatures"] == ["HeroArmature"]


# ---------------------------------------------------------------------------
# 1. The extended walk: skeletons, animations, albedo textures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def hero_view(built, project3d):
    if not HAS_GODOT:
        pytest.skip("Godot not installed")
    got = godot.import_asset(project3d, built["hero"][0])
    assert got["ok"], got
    return got["engine_view"]


@pytest.mark.slow
@needs_godot
@needs_blender
class TestTheEngineIsAsked:
    def test_the_rig_arrives_as_a_skeleton(self, hero_view):
        """The old walk visited only MeshInstance3D, so a rig that failed to
        import produced a report identical to a rig that imported perfectly."""
        assert hero_view["skeleton_count"] == 1, hero_view["skeletons"]
        bones = hero_view["skeletons"][0]
        assert bones["bones"] == 2
        assert "Spine" in bones["bone_names"]

    def test_the_action_arrives_as_an_animation_player(self, hero_view):
        assert hero_view["animation_count"] == 1
        assert hero_view["animations"] == ["Idle"]
        clip = hero_view["animation_players"][0]["animations"][0]
        assert clip["length"] > 0.0 and clip["tracks"] > 0

    def test_the_mesh_is_skinned_to_it(self, hero_view):
        assert hero_view["skinned_meshes"] == 1
        assert all(m["skinned"] for m in hero_view["meshes"])

    def test_blend_shapes_survive_all_the_way_into_godot(self, hero_view):
        assert hero_view["blend_shapes"] == ["Smile", "Puff"]

    def test_a_material_must_carry_a_texture_not_just_a_name(self, hero_view):
        materials = hero_view["materials"]
        assert materials["surfaces"] == materials["with_albedo_texture"] > 0
        assert materials["without_albedo_texture"] == []
        surface = hero_view["meshes"][0]["surfaces"][0]
        assert surface["has_albedo_texture"] is True
        assert surface["albedo_size"] == [64, 64]

    def test_a_named_material_with_no_image_is_caught(self, built, project3d):
        """The exact shape of the "21 materials and ZERO images" failure: every
        material present and named, not one of them sampling anything."""
        got = godot.import_asset(project3d, built["bare"][0])
        view = got["engine_view"]
        missing = view["materials"]["without_albedo_texture"]
        assert missing, "an untextured material passed the texture check"
        assert view["materials"]["with_albedo_texture"] == 0
        assert any(m["material"] or m["surface"] == 0 for m in missing)

    def test_every_mesh_reports_the_path_the_import_file_needs(self, hero_view):
        """`_subresources` is keyed "PATH:<node path>". Without the path from
        the walk there is no way to address a node's importer settings."""
        for mesh in hero_view["meshes"]:
            assert mesh["path"], mesh
            assert mesh["path"].endswith(mesh["name"])


# ---------------------------------------------------------------------------
# 2 + 3. The global-transform aabb, and the size gate it feeds
# ---------------------------------------------------------------------------

@pytest.mark.slow
@needs_godot
@needs_blender
class TestSizeIsMeasuredAfterTheTransform:
    def test_local_aabb_hides_a_scaled_up_character(self, built, project3d):
        """The whole bug in one assertion. `mesh.get_aabb()` is the Mesh
        RESOURCE's box; it knows nothing of the node transforms above it. A
        character scaled up in its node hierarchy reported a normal size and
        sailed through — the one number that would have caught it was measured
        before the transform that broke it."""
        got = godot.import_asset(project3d, built["giant"][0])
        mesh = got["engine_view"]["meshes"][0]

        local = max(mesh["aabb_size"])
        world = max(mesh["aabb_global_size"])
        assert world > local * 30, (local, world)
        assert mesh["scale"] == [40.0, 40.0, 40.0]

    def test_the_size_gate_fails_a_giant_character(self, built, project3d):
        view = godot.inspect_resource(project3d, "res://assets/giant.glb",
                                      max_size_m=4.0)
        check = view["size_check"]
        assert check["ok"] is False
        assert check["longest_axis_m"] > 100
        assert "METRES" in check["note"]
        # A usable fix, not just a complaint.
        assert 0 < check["suggested_scale"] < 0.01

    def test_the_size_gate_passes_a_normal_character(self, hero_view):
        check = hero_view["size_check"]
        assert check["ok"] is True
        assert 1.5 < check["longest_axis_m"] < 2.1, check

    def test_a_too_small_asset_is_caught_too(self, built, project3d):
        """glTF is metres in both directions: 0.018 m imports as silently as
        180 m does."""
        view = godot.inspect_resource(project3d, "res://assets/hero.glb",
                                      min_size_m=5.0, max_size_m=500.0)
        assert view["size_check"]["ok"] is False
        assert "coin" in view["size_check"]["note"]

    def test_bounds_are_utf8_clean(self, built, project3d):
        """Godot writes UTF-8; Windows Python decodes with the ANSI codepage
        unless told otherwise. The note used to come back with a mojibaked em
        dash embedded in a finding handed to a caller."""
        view = godot.inspect_resource(project3d, "res://assets/giant.glb",
                                      max_size_m=4.0)
        assert "—" in view["size_check"]["note"]
        assert "â" not in view["size_check"]["note"]


# ---------------------------------------------------------------------------
# 4. The .import file, and the colliders that never existed without it
# ---------------------------------------------------------------------------

class TestImportSettingsText:
    """Pure text. The `_subresources` shape is fiddly and version-sensitive;
    pinning it here means a regression shows up as a diff, not as an asset you
    can walk through."""

    def test_subresources_uses_the_path_prefix(self):
        rendered = godot._render_subresources(
            {"Crate": {"generate/physics": True, "physics/shape_type": 3}})
        assert '"PATH:Crate": {' in rendered
        assert '"generate/physics": true' in rendered
        assert '"physics/shape_type": 3' in rendered
        assert rendered.startswith("_subresources={")

    def test_no_nodes_renders_the_empty_form(self):
        assert godot._render_subresources({}) == "_subresources={}"

    def test_shape_and_body_names_map_to_the_importer_enums(self):
        assert godot.SHAPE_TYPES["trimesh"] == 2
        assert godot.SHAPE_TYPES["box"] == 3
        assert godot.BODY_TYPES["static"] == 0

    def test_writing_settings_needs_an_existing_import_file(self, tmp_path):
        (tmp_path / "project.godot").write_text("", encoding="utf-8")
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "x.glb").write_bytes(b"glTF")
        got = godot.write_import_settings(str(tmp_path), "assets/x.glb",
                                          physics_nodes={"X": {}})
        assert got["ok"] is False
        assert "imported once" in got["error"]

    def test_params_are_replaced_not_duplicated(self, tmp_path):
        """The empty form Godot writes for a freshly imported asset is a
        SINGLE LINE, `_subresources={}`. A pattern that only knows the
        multi-line form appends a second `_subresources` key instead of
        replacing this one, and which of the two the engine honours is then
        down to ConfigFile's duplicate-key behaviour. Found by this test."""
        asset = tmp_path / "a.glb"
        asset.write_bytes(b"glTF")
        asset.with_suffix(".glb.import").write_text(
            '[remap]\n\nimporter="scene"\n\n[params]\n\n'
            "animation/import=true\n_subresources={}\ngltf/naming_version=2\n",
            encoding="utf-8")
        godot.write_import_settings(str(tmp_path), "a.glb",
                                    physics_nodes={"Mesh": {}},
                                    params={"animation/import": False},
                                    purge=False)
        text = asset.with_suffix(".glb.import").read_text(encoding="utf-8")
        assert text.count("animation/import=") == 1
        assert "animation/import=false" in text
        assert text.count("_subresources={") == 1
        assert '"PATH:Mesh"' in text
        # The head is not ours to rewrite, and neither is anything after the
        # key we replaced.
        assert 'importer="scene"' in text
        assert "gltf/naming_version=2" in text

    def test_an_existing_multiline_block_is_replaced_whole(self, tmp_path):
        asset = tmp_path / "a.glb"
        asset.write_bytes(b"glTF")
        asset.with_suffix(".glb.import").write_text(
            "[remap]\n\n[params]\n\n_subresources={\n"
            '"nodes": {\n"PATH:Old": {\n"generate/physics": true\n}\n}\n}\n'
            "gltf/naming_version=2\n", encoding="utf-8")
        godot.write_import_settings(str(tmp_path), "a.glb",
                                    physics_nodes={"Mesh": {}}, purge=False)
        text = asset.with_suffix(".glb.import").read_text(encoding="utf-8")
        assert text.count("_subresources={") == 1
        assert '"PATH:Old"' not in text
        assert '"PATH:Mesh"' in text
        assert "gltf/naming_version=2" in text

    def test_the_cached_import_is_purged_so_a_reimport_actually_runs(
            self, tmp_path):
        """Godot decides "already imported" from the SOURCE file's md5. Change
        only the params and the reimport is skipped — the new settings never
        take, and it looks exactly like the settings being wrong."""
        asset = tmp_path / "a.glb"
        asset.write_bytes(b"glTF")
        asset.with_suffix(".glb.import").write_text(
            "[remap]\n\n[params]\n\n_subresources={}\n", encoding="utf-8")
        cache = tmp_path / ".godot" / "imported"
        cache.mkdir(parents=True)
        (cache / "a.glb-deadbeef.scn").write_bytes(b"x")
        (cache / "a.glb-deadbeef.md5").write_text("x", encoding="utf-8")
        (cache / "unrelated.png-1234.ctex").write_bytes(b"x")

        got = godot.write_import_settings(str(tmp_path), "a.glb",
                                          physics_nodes={"Mesh": {}})
        assert sorted(got["purged"]) == ["a.glb-deadbeef.md5",
                                         "a.glb-deadbeef.scn"]
        assert (cache / "unrelated.png-1234.ctex").exists()


@pytest.mark.slow
@needs_godot
@needs_blender
class TestCollidersExistInTheEngine:
    def test_a_bare_glb_import_has_no_collider_at_all(self, built, project3d):
        """Not a bug in Godot — glTF has no collider concept and
        `generate/physics` defaults OFF. It IS a bug to ship that and call the
        asset delivered: nothing could stand on it or be shot."""
        got = godot.import_asset(project3d, built["crate"][0])
        assert got["engine_view"]["collider_count"] == 0

    def test_writing_the_import_file_produces_a_real_collision_shape(
            self, built, project3d, tmp_path):
        got = godot.deliver_asset(project3d, built["crate"][0],
                                  shape_type="box",
                                  screenshot_dir=str(tmp_path))
        colliders = got["engine_view"]["colliders"]
        assert colliders, got["steps"]
        assert any(c["body"] == "StaticBody3D" and c["shape"] == "BoxShape3D"
                   for c in colliders), colliders
        assert got["import_settings"]["physics_nodes"]["Crate"][
            "generate/physics"] is True

    def test_a_skinned_mesh_is_left_to_its_capsule(self, built, project3d,
                                                   tmp_path):
        """physics="auto" deliberately skips skinned meshes. A trimesh
        StaticBody3D welded to a character turns it into a wall you cannot
        move; the CharacterBody3D capsule in the generated scene is its
        collider."""
        got = godot.deliver_asset(project3d, built["hero"][0],
                                  screenshot_dir=str(tmp_path))
        assert got["import_settings"].get("physics_nodes") in (None, {})
        assert got["scene_view"]["collider_count"] >= 1
        assert any(c["shape"] == "CapsuleShape3D"
                   for c in got["scene_view"]["colliders"])


# ---------------------------------------------------------------------------
# 5. The generated .tscn
# ---------------------------------------------------------------------------

class TestGeneratedSceneText:
    """templates/3d/scenes/main.tscn ships a CharacterBody3D and a capsule with
    NO MESH; an imported .glb is a Node3D with no body. Neither half is a
    character, and nothing in the pipeline ever married them."""

    def _scene(self, **kw):
        kw.setdefault("node_name", "Hero")
        kw.setdefault("bounds_size", [0.8, 1.8, 0.7])
        kw.setdefault("bounds_position", [-0.4, 0.0, -0.35])
        return godot.character_scene_text("res://assets/hero.glb", **kw)

    def test_the_model_is_a_child_never_the_root(self):
        """A .glb is re-imported on every change and anything written into it
        is discarded. The script, the collider and the hurtbox have to hang off
        a node the importer does not own."""
        text = self._scene(script_res="res://scripts/player.gd")
        assert '[node name="Hero" type="CharacterBody3D"]' in text
        assert ('[node name="Model" parent="." instance=ExtResource("1_model")]'
                in text)
        assert 'script = ExtResource("2_script")' in text

    def test_it_carries_a_collision_shape(self):
        text = self._scene()
        assert '[node name="CollisionShape3D" type="CollisionShape3D" parent="."]' in text
        assert 'shape = SubResource("CapsuleShape3D_body")' in text

    def test_the_capsule_is_fitted_to_the_measured_bounds(self):
        """The template's capsule was a guess (0.4 / 1.8) that no asset was
        ever checked against. This one comes from the engine's own numbers."""
        text = self._scene(bounds_size=[1.0, 2.4, 0.9])
        assert "height = 2.4000" in text
        assert re.search(r"radius = 0\.50", text), text

    def test_a_squat_asset_does_not_produce_a_capsule_godot_rejects(self):
        """Godot refuses a capsule whose radius exceeds half its height. A
        crate or a turret hits that immediately."""
        text = self._scene(bounds_size=[3.0, 0.6, 3.0])
        radius = float(re.search(r"radius = ([\d.]+)", text).group(1))
        height = float(re.search(r"height = ([\d.]+)", text).group(1))
        assert radius <= height / 2, (radius, height)

    def test_the_model_is_recentred_on_the_body_origin(self):
        """The template's capsule sits ON the body origin and player.gd's
        camera offset assumes the eyes are near the top of it. An asset whose
        own origin is at its feet ends up buried in the floor."""
        text = self._scene(bounds_size=[0.8, 1.8, 0.7],
                           bounds_position=[-0.4, 0.0, -0.35])
        line = [ln for ln in text.splitlines()
                if ln.startswith("transform = Transform3D")][0]
        y = float(line.rstrip(")").split(",")[-2])
        assert y == pytest.approx(-0.9, abs=0.01), line

    def test_load_steps_counts_what_is_actually_there(self):
        for kwargs in ({}, {"script_res": "res://scripts/player.gd"}):
            text = self._scene(**kwargs)
            declared = int(re.search(r"load_steps=(\d+)", text).group(1))
            present = (text.count("[ext_resource ")
                       + text.count("[sub_resource "))
            assert declared == present + 1, (kwargs, declared, present)

    def test_the_uid_is_carried_through_when_known(self):
        text = self._scene(model_uid="uid://abc123")
        assert 'uid="uid://abc123"' in text
        assert 'uid=' not in self._scene()


class TestPreviewSceneText:
    def test_the_preview_camera_wins_over_the_characters_own(self):
        """MEASURED, and the reason the first frame off this path was a
        full-screen blur: the character scene carries a first-person Camera3D
        (player.gd needs $Camera3D), Godot makes the FIRST camera into the tree
        current, and the subject is instanced before the preview camera. The
        screenshot was taken from inside the character's head."""
        text = godot.preview_scene_text("res://scenes/hero.tscn",
                                        longest_axis=1.8)
        assert '[node name="PreviewCamera" type="Camera3D" parent="."]' in text
        camera = text.split('[node name="PreviewCamera"', 1)[1]
        assert "current = true" in camera

    def test_the_camera_stands_on_the_side_the_face_is_on(self):
        """The screenshot is the artifact a human is handed to judge the asset,
        and for a while it showed the back of the head.

        Bases face +Y in Blender (BG_FORWARD), the glTF exporter maps Blender +Y
        to glTF -Z, and Godot calls -Z forward. MEASURED end to end: the
        imported Body AABB spans z -0.1853..+0.1123 with LeftToes at z=-0.1149
        ahead of LeftFoot at z=0. An eye on +Z therefore looks the SAME way the
        character does. The control that proves it is the camera and not the
        mesh: the identical base rotated 180 degrees back to the old facing
        photographed correctly from the old +Z eye.

        Nothing else would report this. The turnaround labels its angles and can
        be checked; a single preview frame of a faceless base looks plausible
        from either side."""
        text = godot.preview_scene_text("res://scenes/hero.tscn",
                                        longest_axis=1.8)
        camera = text.split('[node name="PreviewCamera"', 1)[1]
        line = [ln for ln in camera.splitlines()
                if ln.startswith("transform = Transform3D")][0]
        eye_z = float(line.rstrip(")").split(",")[-1])
        assert eye_z < 0, f"preview eye is on the +Z side, behind the face: {line}"

    def test_the_lens_is_a_portrait_lens_and_the_whole_figure_fits(self):
        """Godot's default 75 degree fov shows ~5 m of vertical at the framing
        distance, so a 1.8 m character covers at most a third of the picture.
        MEASURED on the first real delivery: the figure was ~8% of the frame
        and too small to tell which way it faced, which is the one question
        the shot exists to answer.

        The aim stays at the ORIGIN. character_scene_text recentres the model
        onto the body origin (MEASURED: transform origin (0, -0.9, 0.0365) for
        a 1.8 m figure), so the origin already IS the middle — aiming at
        reach*0.5 aims at the top of the head and drops the legs out of frame.
        That was done, photographed and reverted; this test is the receipt."""
        text = godot.preview_scene_text("res://scenes/hero.tscn",
                                        longest_axis=1.8)
        camera = text.split('[node name="PreviewCamera"', 1)[1]
        assert "fov = 40.0" in camera, camera

        line = [ln for ln in camera.splitlines()
                if ln.startswith("transform = Transform3D")][0]
        eye = [float(v) for v in line.rstrip(")").split("(", 1)[1].split(",")][-3:]
        dist = sum(v * v for v in eye) ** 0.5          # aim is the origin
        visible_h = 2.0 * dist * math.tan(math.radians(40.0) / 2.0)
        assert 1.8 < visible_h < 1.8 * 2.0, (
            f"1.8 m subject in {visible_h:.2f} m of frame — clipped or a speck")

    def test_there_is_a_floor_to_stand_on(self):
        """The subject runs the template player script, which applies gravity
        from its first physics frame. With no floor it had fallen ~10 m out of
        frame by the 1.2 s capture, and the screenshot came back as an empty
        background that looked exactly like a failed import."""
        text = godot.preview_scene_text("res://scenes/hero.tscn",
                                        longest_axis=1.8, floor_y=-0.9)
        assert '[node name="Floor" type="StaticBody3D" parent="."]' in text
        assert 'shape = SubResource("BoxShape3D_floor")' in text

    def test_the_floor_sits_under_the_feet_not_through_them(self):
        text = godot.preview_scene_text("res://scenes/hero.tscn",
                                        longest_axis=1.8, floor_y=-0.9)
        block = text.split('[node name="Floor"', 1)[1]
        line = [ln for ln in block.splitlines()
                if ln.startswith("transform = Transform3D")][0]
        top_y = float(line.rstrip(")").split(",")[-2])
        assert top_y < -0.9, line

    def test_ambient_light_is_on(self):
        """A single directional light leaves every surface facing away from it
        pure black, and the frame reads as a broken import."""
        text = godot.preview_scene_text("res://scenes/hero.tscn",
                                        longest_axis=1.8)
        assert "ambient_light_source = 2" in text
        assert "ambient_light_energy" in text


class TestTransformLiteral:
    def test_twelve_floats_are_basis_rows_then_origin(self):
        """Verified against templates/3d/scenes/main.tscn's Sun: reading those
        twelve floats as ROWS is the only reading under which the three COLUMNS
        come out orthonormal and the light points somewhere sensible."""
        text = godot._transform3d(((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                                  (0, 2, -5))
        assert text == "Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 2, -5)"

    def test_a_camera_looks_down_its_local_negative_z(self):
        x, y, z = godot._look_at_basis((0.0, 0.0, 5.0), (0.0, 0.0, 0.0))
        assert z == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)
        assert x == pytest.approx((1.0, 0.0, 0.0), abs=1e-6)
        assert y == pytest.approx((0.0, 1.0, 0.0), abs=1e-6)

    def test_the_basis_stays_orthonormal_off_axis(self):
        import math

        x, y, z = godot._look_at_basis((2.0, 1.5, 4.0), (0.0, 0.0, 0.0))
        for vec in (x, y, z):
            assert math.sqrt(sum(c * c for c in vec)) == pytest.approx(1.0,
                                                                       abs=1e-6)
        assert sum(a * b for a, b in zip(x, z)) == pytest.approx(0.0, abs=1e-6)
        assert sum(a * b for a, b in zip(y, z)) == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# The gate, and the whole loop
# ---------------------------------------------------------------------------

class TestDeliveryChecks:
    def _view(self, **over):
        view = {
            "ok": True, "root_type": "Node3D", "total_tris": 44,
            "materials": {"surfaces": 1, "with_albedo_texture": 1,
                          "without_albedo_texture": []},
            "size_check": {"ok": True, "longest_axis_m": 1.8},
            "collider_count": 1, "skeleton_count": 1, "animation_count": 1,
            "animations": ["Idle"], "blend_shapes": ["Smile"],
        }
        view.update(over)
        return view

    def test_a_complete_character_passes_everything(self):
        checks = godot._delivery_checks(self._view(), self._view())
        assert all(c["ok"] for c in checks), checks

    def test_named_materials_with_no_texture_fail_the_required_gate(self):
        view = self._view(materials={
            "surfaces": 21, "with_albedo_texture": 0,
            "without_albedo_texture": [{"mesh": "Body", "surface": 0,
                                        "material": "Skin"}]})
        row = next(c for c in godot._delivery_checks(view, view)
                   if c["check"] == "materials_carry_a_texture")
        assert row["ok"] is False and row["required"] is True
        assert "0/21" in row["measured"]

    def test_a_missing_rig_reports_without_failing_the_gate(self):
        """A crate has no skeleton and that is not a defect. The row must
        report, not block — a gate that cries wolf gets switched off."""
        view = self._view(skeleton_count=0, animation_count=0, animations=[],
                          blend_shapes=[])
        checks = godot._delivery_checks(view, view)
        assert all(c["ok"] for c in checks if c["required"])
        assert not next(c for c in checks if c["check"] == "has_skeleton")["ok"]

    def test_no_collider_fails(self):
        checks = godot._delivery_checks(self._view(collider_count=0),
                                        self._view())
        assert not next(c for c in checks
                        if c["check"] == "has_collider")["ok"]

    def test_a_bad_size_fails(self):
        view = self._view(size_check={"ok": False, "longest_axis_m": 180.0,
                                      "note": "metres"})
        assert not next(c for c in godot._delivery_checks(view, view)
                        if c["check"] == "real_world_size")["ok"]


@pytest.mark.slow
@needs_godot
@needs_blender
class TestTheLoopCloses:
    """The step the pipeline never had: import the assembled asset, instance it
    into a scene, run the ACTUAL engine, and photograph the result. Everything
    before this asserted on a Blender render of a Blender scene under Blender
    lights, which cannot see a single last-mile defect."""

    def test_glb_to_screenshot(self, built, project3d, tmp_path):
        got = godot.deliver_asset(project3d, built["hero"][0],
                                  script_res="res://scripts/player.gd",
                                  screenshot_dir=str(tmp_path))
        assert got["ok"], got["steps"]

        names = [s["step"] for s in got["steps"]]
        assert names[0] == "import" and names[-1] == "screenshot"
        assert all(s["ok"] for s in got["steps"]), got["steps"]

        shot = tmp_path / "hero.png"
        assert shot.exists() and shot.stat().st_size > 5000, "a blank frame"

    def test_the_generated_scene_loads_in_the_engine(self, built, project3d,
                                                     tmp_path):
        """A .tscn that looks right in a diff and fails to instantiate is
        exactly the failure a text-only test cannot see."""
        got = godot.deliver_asset(project3d, built["hero"][0],
                                  script_res="res://scripts/player.gd",
                                  screenshot_dir=str(tmp_path))
        scene = got["scene_view"]
        assert scene["ok"] is True
        assert scene["root_type"] == "CharacterBody3D"
        assert scene["skeleton_count"] == 1
        assert scene["animation_count"] == 1
        assert scene["collider_count"] >= 1

    def test_a_giant_character_is_refused_by_the_gate(self, built, project3d,
                                                     tmp_path):
        got = godot.deliver_asset(project3d, built["giant"][0],
                                  screenshot_dir=str(tmp_path))
        assert got["ok"] is False
        row = next(c for c in got["checks"]
                   if c["check"] == "real_world_size")
        assert row["ok"] is False
        # It still produced its artefacts — a failing gate that also refuses to
        # show you the asset is a gate you cannot debug.
        assert got["scene"] and got["screenshot"]

    def test_the_bound_tightens_for_anything_skinned(self, built, project3d,
                                                     tmp_path):
        """4 m for a character, 50 m for a prop. A vehicle is legitimately
        large; a humanoid over 4 m across is a unit error."""
        hero = godot.deliver_asset(project3d, built["hero"][0],
                                   screenshot_dir=str(tmp_path))
        crate = godot.deliver_asset(project3d, built["crate"][0],
                                    shape_type="box",
                                    screenshot_dir=str(tmp_path))
        assert hero["engine_view"]["size_check"]["max_m"] == 4.0
        assert crate["engine_view"]["size_check"]["max_m"] == 50.0
