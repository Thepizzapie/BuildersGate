"""Blender adapter, exercised against REAL Blender.

Mocking the subprocess here would test nothing worth testing — the whole risk in
this adapter is the boundary: does bpy actually run, do stats come back, does a
broken script report its traceback instead of vanishing. Skipped when Blender
isn't installed rather than faked.

MARKED SLOW, which it always was and never said. pyproject defines that marker
as "hits real Blender/whisper", ci.yml's own comment assumes these carry it, and
CONTRIBUTING tells people to run `-m "not slow"` — but the mark was never on
them, so the documented fast run drove a real Blender for two minutes on every
machine that had one. It changes nothing on CI, where no runner has Blender and
the skipif already took these out.
"""
from __future__ import annotations

import pytest

from bgate_adapters import blender

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not blender.available()["available"],
                       reason="Blender not installed"),
]

CUBE = """
import bpy
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
cube = bpy.context.active_object
cube.name = "TestCube"
print("built", cube.name)
"""


class TestDiscovery:
    def test_finds_blender(self):
        assert blender.available()["available"] is True

    def test_reports_version(self):
        assert "Blender" in blender.version()["version"]

    def test_bad_override_is_explicit(self, monkeypatch):
        monkeypatch.setenv("BGATE_BLENDER", r"C:\nope\blender.exe")
        with pytest.raises(blender.BlenderNotFound, match="missing file"):
            blender.find_blender()


class TestRunScript:
    def test_empty_scene_has_no_default_cube(self, tmp_path):
        got = blender.run_script("pass", out_dir=str(tmp_path))
        assert got["ok"] is True
        # --factory-startup DOES load the default scene; the point is that it's
        # the known baseline (Cube/Camera/Light), not a user's customized one.
        assert {o["name"] for o in got["scene"]["objects"]} == {"Cube", "Camera", "Light"}

    def test_builds_geometry_and_reports_stats(self, tmp_path):
        got = blender.run_script(CUBE, out_dir=str(tmp_path))
        assert got["ok"] is True
        assert "built TestCube" in got["print"]

        cube = next(o for o in got["scene"]["objects"] if o["name"] == "TestCube")
        assert cube["type"] == "MESH"
        assert cube["tris"] == 12  # a cube is 6 quads = 12 tris
        assert cube["verts"] == 8

    def test_totals_aggregate_across_meshes(self, tmp_path):
        script = CUBE + """
bpy.ops.mesh.primitive_cube_add(size=1, location=(3, 0, 0))
bpy.context.active_object.name = "Second"
"""
        got = blender.run_script(script, out_dir=str(tmp_path))
        # Default Cube + TestCube + Second = 3 meshes, 12 tris each.
        assert got["scene"]["totals"]["meshes"] == 3
        assert got["scene"]["totals"]["tris"] == 36

    def test_modifiers_are_evaluated_not_ignored(self, tmp_path):
        """Stats come from the evaluated mesh — a subsurf must change the count."""
        script = CUBE + """
mod = cube.modifiers.new("Subsurf", "SUBSURF")
mod.levels = 2
"""
        got = blender.run_script(script, out_dir=str(tmp_path))
        cube = next(o for o in got["scene"]["objects"] if o["name"] == "TestCube")
        assert cube["tris"] > 12

    def test_missing_uv_is_warned(self, tmp_path):
        script = """
import bpy
mesh = bpy.data.meshes.new("Bare")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
bpy.context.collection.objects.link(bpy.data.objects.new("Bare", mesh))
"""
        got = blender.run_script(script, out_dir=str(tmp_path))
        bare = next(o for o in got["scene"]["objects"] if o["name"] == "Bare")
        assert bare["uv_layers"] == 0
        assert "UV" in bare["warning"]

    def test_materials_are_listed(self, tmp_path):
        script = CUBE + """
mat = bpy.data.materials.new("Emberglass")
cube.data.materials.append(mat)
"""
        got = blender.run_script(script, out_dir=str(tmp_path))
        assert "Emberglass" in got["scene"]["materials"]
        cube = next(o for o in got["scene"]["objects"] if o["name"] == "TestCube")
        assert cube["materials"] == ["Emberglass"]


class TestFailures:
    def test_syntax_error_returns_payload_not_exception(self, tmp_path):
        got = blender.run_script("def (((:", out_dir=str(tmp_path))
        assert got["ok"] is False
        assert "SyntaxError" in got["error"]

    def test_undefined_name_is_reported(self, tmp_path):
        got = blender.run_script("cube.location = (1, 2, 3)", out_dir=str(tmp_path))
        assert got["ok"] is False
        assert "NameError" in got["error"]

    def test_runtime_error_reports_scene_anyway(self, tmp_path):
        got = blender.run_script(CUBE + "\nraise ValueError('boom')",
                                 out_dir=str(tmp_path))
        assert got["ok"] is False
        assert "boom" in got["error"]
        assert "ValueError" in got["traceback"]
        # Partial state is diagnostic — the cube it built before dying is visible.
        assert any(o["name"] == "TestCube" for o in got["scene"]["objects"])

    def test_missing_blend_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            blender.run_script("pass", blend_file=str(tmp_path / "ghost.blend"))

    def test_bad_engine_rejected(self):
        with pytest.raises(ValueError, match="engine"):
            blender.run_script("pass", engine="UNREAL")


class TestRender:
    def test_render_writes_a_png(self, tmp_path):
        script = CUBE + """
bpy.context.scene.render.resolution_x = 64
bpy.context.scene.render.resolution_y = 64
"""
        got = blender.run_script(script, render=True, out_dir=str(tmp_path))
        assert got["ok"] is True
        assert got["render"]["rendered"] is True

        png = tmp_path / "render.png"
        assert png.exists() and png.stat().st_size > 0
        assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_render_without_camera_explains_itself(self, tmp_path):
        script = """
import bpy
for obj in list(bpy.context.scene.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
"""
        got = blender.run_script(script, render=True, out_dir=str(tmp_path))
        assert got["ok"] is True
        assert got["render"]["rendered"] is False
        assert "camera" in got["render"]["reason"]


class TestStdinIsolation:
    """Regression guard for a bug that cost an hour to find.

    Under a stdio MCP server, the server's stdin IS the client's protocol
    channel. A Blender that inherits it blocks forever at ~0% CPU — which looks
    exactly like a slow render and gets misdiagnosed as a GPU stall. Nothing in
    a normal test run catches this, because standalone stdin is a terminal.
    """

    def test_spawn_always_detaches_stdin(self, monkeypatch):
        captured = {}

        def spy(cmd, **kwargs):
            captured.update(kwargs)
            return blender.subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(blender.subprocess, "run", spy)
        blender._spawn(["x"], timeout=1)
        assert captured["stdin"] is blender.subprocess.DEVNULL

    def test_run_script_does_not_inherit_stdin(self, tmp_path, monkeypatch):
        captured = {}

        def spy(cmd, **kwargs):
            captured.update(kwargs)
            raise blender.subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(blender.subprocess, "run", spy)
        blender.run_script("pass", out_dir=str(tmp_path), timeout=1)
        assert captured["stdin"] is blender.subprocess.DEVNULL


class TestWarmup:
    def test_warmup_renders_and_reports(self, tmp_path):
        got = blender.warmup("BLENDER_EEVEE_NEXT", out_dir=str(tmp_path))
        assert got["ok"] is True
        assert got["warmed"] is True

    def test_workbench_needs_no_warmup(self, tmp_path):
        got = blender.warmup("BLENDER_WORKBENCH", out_dir=str(tmp_path))
        assert got["warmed"] is False
        assert "no warmup" in got["reason"]

    def test_cold_gpu_render_gets_a_generous_timeout(self, tmp_path, monkeypatch):
        """A caller's small timeout must not cause a bogus cold-start failure."""
        monkeypatch.setattr(blender, "_warmed", set())
        captured = {}

        def spy(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            raise blender.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(blender.subprocess, "run", spy)
        got = blender.run_script("pass", render=True, engine="BLENDER_EEVEE_NEXT",
                                 timeout=5, out_dir=str(tmp_path))
        assert captured["timeout"] == blender.COLD_START_TIMEOUT
        assert "shader warmup" in got["hint"]


class TestSceneStats:
    def test_reads_a_saved_blend(self, tmp_path):
        blend = tmp_path / "saved.blend"
        blender.run_script(CUBE + f"\nbpy.ops.wm.save_as_mainfile(filepath=r'{blend}')",
                           out_dir=str(tmp_path))
        assert blend.exists()

        got = blender.scene_stats(str(blend))
        assert got["ok"] is True
        assert any(o["name"] == "TestCube" for o in got["scene"]["objects"])


# ---------------------------------------------------------------------------
# Assembly — the layered path
# ---------------------------------------------------------------------------

def _part(tmp_path, name, script):
    """Build one layer as a real .glb, the way the modelling step would."""
    out = tmp_path / f"{name}.glb"
    got = blender.run_script(script, export_glb=str(out), out_dir=str(tmp_path))
    assert got["ok"] is True, got.get("error")
    assert out.exists()
    return out


BODY = """
import bpy
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=2)
bpy.context.active_object.name = "Body"
"""

CAP = """
import bpy
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.mesh.primitive_uv_sphere_add(radius=1.2, location=(0, 0, 2))
bpy.context.active_object.name = "Cap"
"""

LOGO = """
import bpy
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.mesh.primitive_plane_add(size=0.4, location=(0, -1.2, 2))
bpy.context.active_object.name = "Logo"
"""


def _textured_part(tmp_path, name, script, colour=(102, 28, 163)):
    """One layer with a real image texture on it — a SHIPPABLE layer.

    A bare primitive round-tripped through glTF carries no material and no
    image, and combine() refuses to call that pile an asset. That refusal is
    correct, so the way to earn ok=True is to texture the fixture rather than
    to lower the bar — which also walks the same road a real run walks: model
    the layer, generate the map, apply_texture it on.
    """
    from PIL import Image

    raw = _part(tmp_path, name, script)
    image = tmp_path / f"{name}_texture.png"
    Image.new("RGB", (64, 64), colour).save(image)

    out = tmp_path / f"{name}_textured.glb"
    got = blender.apply_texture(raw, image, out, timeout=300)
    assert got["ok"] is True, got.get("error")
    assert got["textured"], f"{name}: no material received the image"
    return out


class TestCombineValidation:
    """Rejected before Blender is launched — every one of these would otherwise
    surface minutes later as a traceback, or as a silently missing layer."""

    def test_no_parts_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="at least one part"):
            blender.combine([], tmp_path / "out.glb")

    def test_unsupported_suffix_names_what_it_reads(self, tmp_path):
        bad = tmp_path / "layer.fbx"
        bad.write_bytes(b"")
        with pytest.raises(ValueError, match="combine reads"):
            blender.combine([str(bad)], tmp_path / "out.glb")

    def test_missing_file_is_named(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="no such file"):
            blender.combine([str(tmp_path / "gone.glb")], tmp_path / "out.glb")

    def test_duplicate_layer_names_refused(self, tmp_path):
        one = _part(tmp_path, "body", BODY)
        with pytest.raises(ValueError, match="unique"):
            blender.combine([{"path": str(one), "name": "x"},
                             {"path": str(one), "name": "x"}], tmp_path / "out.glb")

    def test_decal_target_must_exist(self, tmp_path):
        one = _part(tmp_path, "body", BODY)
        with pytest.raises(ValueError, match="not one of the parts"):
            blender.combine([{"path": str(one), "name": "logo", "decal_on": "cap"}],
                            tmp_path / "out.glb")

    def test_decal_on_itself_refused(self, tmp_path):
        one = _part(tmp_path, "body", BODY)
        with pytest.raises(ValueError, match="decal on itself"):
            blender.combine([{"path": str(one), "name": "logo", "decal_on": "logo"}],
                            tmp_path / "out.glb")

    def test_bad_bind_explains_the_vocabulary(self, tmp_path):
        one = _part(tmp_path, "body", BODY)
        with pytest.raises(ValueError, match="bone:<BoneName>"):
            blender.combine([{"path": str(one), "bind": "glue"}], tmp_path / "out.glb")

    def test_rig_must_name_a_part(self, tmp_path):
        one = _part(tmp_path, "body", BODY)
        with pytest.raises(ValueError, match="rig="):
            blender.combine([{"path": str(one), "name": "body"}],
                            tmp_path / "out.glb", rig="skeleton")


class TestCombine:
    def test_layers_assemble_into_one_glb(self, tmp_path):
        # TEXTURED layers on purpose. This is the one test that claims the whole
        # round trip produces a finished asset, so it has to hand combine()
        # something that IS one — ok=True is earned here, not asserted over a
        # grey blob the way it used to be.
        parts = [{"path": str(_textured_part(tmp_path, "body", BODY)), "name": "body"},
                 {"path": str(_textured_part(tmp_path, "cap", CAP)), "name": "cap"}]
        out = tmp_path / "player.glb"

        got = blender.combine(parts, out, timeout=300)
        assert got["ok"] is True, got.get("error")
        assert got["glb"]["exported"] is True
        assert out.exists()
        assert got["layers"] == 2
        assert all(p["imported"] for p in got["parts"])
        # What "finished" means, and what the assembly used to report empty.
        assert got["materials"], got["checks"]
        assert got["images"], got["checks"]

    def test_an_untextured_pile_of_layers_is_not_called_an_asset(self, tmp_path):
        """The gate, asserted FIRING — which nothing in this suite ever did.

        MEASURED: combine() returned ok=True, checks=[] and warnings=[] over an
        assembly carrying zero materials, zero images and zero textures. It
        assembled fine. It was not an asset, and every check that would have
        said so was thrown away on the way out.
        """
        parts = [{"path": str(_part(tmp_path, "body", BODY)), "name": "body"},
                 {"path": str(_part(tmp_path, "cap", CAP)), "name": "cap"}]
        got = blender.combine(parts, tmp_path / "grey.glb", timeout=300)

        # The verdict is a VERDICT, not a refusal: the layers still assembled
        # and the file is still on disk to look at.
        assert got["glb"]["exported"] is True
        assert (tmp_path / "grey.glb").exists()
        assert all(p["imported"] for p in got["parts"])

        assert got["ok"] is False
        fired = {c["check"] for c in got["checks"]}
        assert "no_materials" in fired, got["checks"]
        assert "no_textures" in fired, got["checks"]
        # An ok=False with no reason reads downstream as a broken tool.
        assert "not an asset" in (got["error"] or "")
        assert any("no image texture" in w for w in got["warnings"]), got["warnings"]

    def test_the_runners_readiness_issues_reach_the_checks(self, tmp_path):
        """`issues` was present in the return and read by nobody.

        The runner measures game-readiness on every run; combine() merged the
        result wholesale and then reported checks=[] anyway, so a caller reading
        the two keys that matter saw a clean bill.
        """
        parts = [{"path": str(_part(tmp_path, "body", BODY)), "name": "body"}]
        got = blender.combine(parts, tmp_path / "bare.glb", timeout=300)

        promoted = [c for c in got["checks"] if c["check"] == "no_material"]
        assert promoted, got["checks"]
        # Named by LAYER, because a re-run costs one layer and that needs
        # knowing which one.
        assert promoted[0]["layer"] == "body"
        assert promoted[0]["object"]
        assert any("no_material" in w for w in got["warnings"]), got["warnings"]

    def test_each_layer_reports_its_own_tris(self, tmp_path):
        parts = [{"path": str(_part(tmp_path, "body", BODY)), "name": "body"},
                 {"path": str(_part(tmp_path, "cap", CAP)), "name": "cap"}]
        got = blender.combine(parts, tmp_path / "player.glb", timeout=300)

        by_name = {p["name"]: p for p in got["parts"]}
        # A cube is 12 tris; the sphere is far more. The point is that the
        # numbers are PER LAYER — a scene total cannot tell you which layer
        # blew the budget, which is the only actionable form.
        assert by_name["body"]["tris"] == 12
        assert by_name["cap"]["tris"] > 12
        assert all(p["imported"] for p in got["parts"])

    def test_a_decal_is_fitted_not_left_to_z_fight(self, tmp_path):
        parts = [{"path": str(_part(tmp_path, "cap", CAP)), "name": "cap"},
                 {"path": str(_part(tmp_path, "logo", LOGO)), "name": "logo",
                  "decal_on": "cap"}]
        got = blender.combine(parts, tmp_path / "player.glb", timeout=300)

        # This test is about the FIT. These primitives carry no texture, so the
        # asset gate correctly withholds ok — what proves the assembly ran is
        # the export and the layers arriving.
        assert got["glb"]["exported"] is True
        assert all(p["imported"] for p in got["parts"])
        # The whole reason the decal is its own layer: it gets conformed to the
        # surface instead of being modelled flush against it.
        assert not [c for c in got["checks"]
                    if c["check"] == "decal_not_fitted"], got["checks"]

    def test_binding_without_a_rig_says_so(self, tmp_path):
        parts = [{"path": str(_part(tmp_path, "body", BODY)), "name": "body",
                  "bind": "deform"}]
        got = blender.combine(parts, tmp_path / "player.glb", timeout=300)

        assert any("no rig was named" in w for w in got["warnings"])
        assert got["parts"][0]["bound"] == "none"

    def test_above_the_ceiling_warns_but_still_assembles(self, tmp_path):
        one = str(_part(tmp_path, "body", BODY))
        parts = [{"path": one, "name": f"layer{i}"}
                 for i in range(blender.MAX_LAYERS + 1)]
        got = blender.combine(parts, tmp_path / "many.glb", timeout=420)

        assert any("planning ceiling" in w for w in got["warnings"])
        # "still assembles" is the claim, and the export is what settles it —
        # the ceiling is a warning, not a refusal, and it must not turn into one
        # just because the fixture is untextured.
        assert got["glb"]["exported"] is True
        assert (tmp_path / "many.glb").exists()
        assert got["layers"] == blender.MAX_LAYERS + 1
        assert all(p["imported"] for p in got["parts"])


RIG = """
import bpy
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.object.armature_add()
bpy.context.active_object.name = "Skeleton"
"""


def _rig_part(tmp_path):
    """The armature as a .blend — glTF drops a lone armature with nothing
    skinned to it, and the rig layer arrives before anything is bound."""
    out = tmp_path / "skeleton.blend"
    got = blender.run_script(
        RIG + f"\nbpy.ops.wm.save_as_mainfile(filepath=r'{out}')",
        out_dir=str(tmp_path))
    assert got["ok"] is True, got.get("error")
    return out


class TestCombineRig:
    def test_soft_deforms_and_hard_rides_a_bone(self, tmp_path):
        parts = [{"path": str(_rig_part(tmp_path)), "name": "skeleton"},
                 {"path": str(_part(tmp_path, "body", BODY)), "name": "body",
                  "bind": "deform"},
                 {"path": str(_part(tmp_path, "cap", CAP)), "name": "cap",
                  "bind": "bone:Bone"}]
        got = blender.combine(parts, tmp_path / "player.glb", rig="skeleton",
                              timeout=420)

        # About the BIND, not about shippability: bare primitives are not an
        # asset and combine() says so, which is a different question from
        # whether the rig took.
        assert got["glb"]["exported"] is True
        assert got["armature"]
        by_name = {p["name"]: p for p in got["parts"]}
        # A jersey deforms with the body; a cap does not bend, it rides a bone.
        # The method is part of the answer: heat is what you want, and the
        # fallbacks are reported rather than hidden behind a plain "deform".
        assert by_name["body"]["bound"].startswith("deform:")
        assert by_name["body"]["bound"].split(":")[1] in ("heat", "envelope", "nearest")
        assert by_name["cap"]["bound"] == "bone:Bone"
        # The layer that would stand still while the body moves.
        assert not [c for c in got["checks"] if c["check"] == "unbound"], got["checks"]

    def test_a_bone_that_does_not_exist_is_named_with_the_ones_that_do(self, tmp_path):
        parts = [{"path": str(_rig_part(tmp_path)), "name": "skeleton"},
                 {"path": str(_part(tmp_path, "cap", CAP)), "name": "cap",
                  "bind": "bone:Head"}]
        got = blender.combine(parts, tmp_path / "player.glb", rig="skeleton",
                              timeout=300)

        note = " ".join(n["note"] for n in got["notes"])
        assert "Head" in note and "Bone" in note
        assert got["parts"][1]["bound"] == "none"

    def test_a_rig_layer_with_no_armature_says_nothing_was_bound(self, tmp_path):
        parts = [{"path": str(_part(tmp_path, "body", BODY)), "name": "body"},
                 {"path": str(_part(tmp_path, "cap", CAP)), "name": "cap",
                  "bind": "deform"}]
        got = blender.combine(parts, tmp_path / "player.glb", rig="body",
                              timeout=300)

        assert any("no armature" in n["note"] for n in got["notes"])
        assert got["parts"][1]["bound"] == "none"


# ---------------------------------------------------------------------------
# The case that failed on a real user
# ---------------------------------------------------------------------------
# Asked for a baseball player in one pass, the pipeline returned a figure whose
# pose read fine and whose hands and cap did not, with the team logo on the cap
# scrambled. This is that same request through the layered path: the parts that
# lost the attention budget are their own layers, and the logo is a decal
# conformed to the cap instead of a texture baked into a whole-body generation.

def _blob(name, primitive, at, size=1.0):
    # A sphere is sized by radius and everything else by size; bpy rejects the
    # wrong keyword outright rather than ignoring it.
    dial = "radius" if "sphere" in primitive else "size"
    return f"""
import bpy
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.mesh.{primitive}({dial}={size}, location={at})
bpy.context.active_object.name = "{name}"
"""


class TestBaseballPlayer:
    """Six layers and a rig — the shape a person describes, not a mesh editor."""

    def _layers(self, tmp_path):
        return [
            {"path": str(_rig_part(tmp_path)), "name": "skeleton"},
            {"path": str(_part(tmp_path, "body",
                               _blob("Body", "primitive_cube_add", (0, 0, 0), 2.0))),
             "name": "body", "bind": "deform"},
            {"path": str(_part(tmp_path, "uniform",
                               _blob("Uniform", "primitive_cube_add", (0, 0, 0), 2.1))),
             "name": "uniform", "bind": "deform"},
            {"path": str(_part(tmp_path, "cap",
                               _blob("Cap", "primitive_uv_sphere_add", (0, 0, 1.6)))),
             "name": "cap", "bind": "bone:Bone"},
            {"path": str(_part(tmp_path, "glove",
                               _blob("Glove", "primitive_uv_sphere_add", (1.2, 0, 0)))),
             "name": "glove", "bind": "bone:Bone"},
            {"path": str(_part(tmp_path, "cleats",
                               _blob("Cleats", "primitive_cube_add", (0, 0, -1.4), 0.8))),
             "name": "cleats", "bind": "deform"},
            {"path": str(_part(tmp_path, "logo",
                               _blob("Logo", "primitive_plane_add", (0, -0.9, 1.7), 0.4))),
             "name": "logo", "decal_on": "cap", "bind": "bone:Bone"},
        ]

    def test_a_baseball_player_assembles_rigged_and_whole(self, tmp_path):
        got = blender.combine(self._layers(tmp_path), tmp_path / "player.glb",
                              rig="skeleton", timeout=600)

        assert got["glb"]["exported"] is True
        assert (tmp_path / "player.glb").exists()
        assert got["armature"]
        # "whole" is about the layers arriving and the binds taking. These
        # fixtures are untextured primitives, so the asset gate withholds ok —
        # that is a different failure from a character coming apart.
        assert not [c for c in got["checks"] if c["check"] == "unbound"], got["checks"]

        # Seven layers is under the ceiling — a real character does not need
        # eight, which is the number the ceiling exists to keep it under.
        assert got["layers"] <= blender.MAX_LAYERS
        missing = [p["name"] for p in got["parts"] if not p["imported"]]
        assert not missing, f"layers that imported nothing: {missing}"

    def test_nothing_detaches_or_tears(self, tmp_path):
        got = blender.combine(self._layers(tmp_path), tmp_path / "player.glb",
                              rig="skeleton", timeout=600)

        # These two are the in-engine failures: a layer that stands still while
        # the body moves, and one that tears at the rest pose.
        assert not [c for c in got["checks"] if c["check"] == "unbound"], got["checks"]
        assert not [c for c in got["checks"]
                    if c["check"] == "unweighted_verts"], got["checks"]
        # The third way a character comes apart: a layer that never arrived.
        #
        # NOT an empty warnings list. warnings now also carries the honest
        # complaints about this fixture — untextured primitives with no
        # materials — and asserting emptiness meant asserting that combine()
        # noticed nothing, which is exactly the bug that survived here.
        assert not [w for w in got["warnings"]
                    if "imported nothing" in w], got["warnings"]

    def test_the_logo_is_fitted_to_the_cap_not_flush_against_it(self, tmp_path):
        got = blender.combine(self._layers(tmp_path), tmp_path / "player.glb",
                              rig="skeleton", timeout=600)

        assert not [c for c in got["checks"] if c["check"] == "decal_not_fitted"]
        # The cap is hard geometry on a bone; the jersey deforms with the body.
        by_name = {p["name"]: p for p in got["parts"]}
        assert by_name["cap"]["bound"] == "bone:Bone"
        assert by_name["uniform"]["bound"].startswith("deform:")
        assert by_name["logo"]["decal_on"] == "cap"

    def test_every_layer_is_priced_and_named_separately(self, tmp_path):
        got = blender.combine(self._layers(tmp_path), tmp_path / "player.glb",
                              rig="skeleton", timeout=600)

        # A re-run costs one layer, not the character — which is only true if
        # the report says WHICH layer, and how big it was.
        for part in got["parts"]:
            if part["name"] == "skeleton":
                continue
            assert part["tris"] > 0, part
            assert part["objects"], part



# ---------------------------------------------------------------------------
# The four gaps the first real character run exposed
# ---------------------------------------------------------------------------

class TestKit:
    """The modelling floor. An agent rewrote 33 KB of this per request."""

    def test_the_kit_is_available_without_importing_it(self, tmp_path):
        script = """
obj = bg_box("Slab", size=(2, 1, 0.5))
bg_finish(obj, colour=(0.4, 0.11, 0.64), material="kit_purple")
print("stats", bg_stats(obj))
"""
        got = blender.run_script(script, out_dir=str(tmp_path))
        assert got["ok"] is True, got.get("error")
        slab = next(o for o in got["scene"]["objects"] if o["name"] == "Slab")
        # bg_finish owes the pipeline all four: clean, applied, unwrapped, mat.
        assert slab["uv_layers"] > 0
        assert slab["materials"] == ["kit_purple"]

    def test_kit_can_be_switched_off(self, tmp_path):
        got = blender.run_script("bg_box('X')", kit=False, out_dir=str(tmp_path))
        assert got["ok"] is False
        assert "bg_box" in got["error"]

    def test_clean_removes_what_bone_heat_refuses(self, tmp_path):
        # Doubled verts and a loose vertex — the exact geometry that makes
        # automatic weighting fail silently.
        script = """
obj = bg_box("Dirty")
mesh = obj.data
before = len(mesh.vertices)
import bmesh
bm = bmesh.new(); bm.from_mesh(mesh)
for v in list(bm.verts):
    bm.verts.new(v.co)          # a duplicate of every vertex, unconnected
bm.to_mesh(mesh); bm.free()
print("dirty", bg_stats(obj))
bg_clean(obj)
print("clean", bg_stats(obj))
"""
        got = blender.run_script(script, out_dir=str(tmp_path))
        assert got["ok"] is True, got.get("error")
        assert "loose" in got["print"]
        clean = got["print"].split("clean ")[1]
        assert "'loose': 0" in clean


class TestTexture:
    def test_a_generated_image_reaches_the_material(self, tmp_path):
        from PIL import Image

        model = _part(tmp_path, "cap", CAP)
        image = tmp_path / "cap_texture.png"
        Image.new("RGB", (64, 64), (102, 28, 163)).save(image)

        out = tmp_path / "cap_textured.glb"
        got = blender.apply_texture(model, image, out, timeout=300)

        assert got["ok"] is True, got.get("error")
        assert got["textured"], "no material received the image"
        assert out.exists()

    def test_the_texture_actually_lands_in_the_glb(self, tmp_path):
        import json as _json
        import struct
        from PIL import Image

        model = _part(tmp_path, "cap", CAP)
        image = tmp_path / "cap_texture.png"
        Image.new("RGB", (64, 64), (102, 28, 163)).save(image)
        out = tmp_path / "cap_textured.glb"
        blender.apply_texture(model, image, out, timeout=300)

        raw = out.read_bytes()
        doc = _json.loads(raw[20:20 + struct.unpack_from("<I", raw, 12)[0]])
        # THE ASSERTION THE FIRST RUN WOULD HAVE FAILED: 21 materials, 0 images.
        assert len(doc.get("images", [])) >= 1
        assert len(doc.get("textures", [])) >= 1

    def test_missing_image_is_named(self, tmp_path):
        model = _part(tmp_path, "cap", CAP)
        with pytest.raises(FileNotFoundError, match="texture image"):
            blender.apply_texture(model, tmp_path / "gone.png", tmp_path / "o.glb")


class TestTurnaround:
    def test_renders_every_angle_and_reads_them_back(self, tmp_path):
        blender.warmup("BLENDER_EEVEE_NEXT", out_dir=str(tmp_path))
        model = _part(tmp_path, "body", BODY)

        got = blender.turnaround(model, tmp_path, stem="body", size=(256, 320),
                                 timeout=600)

        assert got["renders"], got.get("error")
        assert len(got["renders"]) == len(blender.TURNAROUND_ANGLES)
        for render in got["renders"]:
            assert render["exists"], render
            # Every frame comes back JUDGED — this is the look-back the first
            # run skipped, made mechanical.
            assert render["checked"] is True
            assert "blown" in render and "mean" in render

    def test_a_blown_out_frame_is_not_ok(self, tmp_path):
        from PIL import Image

        white = tmp_path / "white.png"
        Image.new("L", (64, 64), 255).save(white)
        verdict = blender._exposure_report(white)
        assert verdict["ok"] is False
        assert "blown out" in verdict["verdict"]

    def test_a_black_frame_is_not_ok(self, tmp_path):
        from PIL import Image

        black = tmp_path / "black.png"
        Image.new("L", (64, 64), 4).save(black)
        verdict = blender._exposure_report(black)
        assert verdict["ok"] is False
        assert "too dark" in verdict["verdict"]


class TestManifestAndSweep:
    def _assembled(self, tmp_path):
        parts = [{"path": str(_part(tmp_path, "body", BODY)), "name": "body"},
                 {"path": str(_part(tmp_path, "cap", CAP)), "name": "cap"}]
        out = tmp_path / "player.glb"
        got = blender.combine(parts, out, timeout=300)
        # These tests are about the RECORD, not about shippability. Untextured
        # primitives assemble and export; combine() declines to call the result
        # a finished asset, and the manifest is written either way — which is
        # the point, because the run that needs re-running is the failed one.
        assert got["glb"]["exported"] is True, got.get("error")
        assert out.exists()
        return out, got

    def test_a_run_records_itself_beside_its_output(self, tmp_path):
        import json as _json

        out, got = self._assembled(tmp_path)
        path = blender.manifest_path(out)
        assert path.is_file()
        assert got["manifest"] == str(path)

        doc = _json.loads(path.read_text(encoding="utf-8"))
        assert doc["asset"] == "player.glb"
        assert [layer["name"] for layer in doc["layers"]] == ["body", "cap"]
        # Sources are what makes a single layer re-runnable later.
        assert all(layer["source"] for layer in doc["layers"])

    def test_sweep_removes_the_layers_and_keeps_the_asset(self, tmp_path):
        out, _ = self._assembled(tmp_path)
        layers = [tmp_path / "body.glb", tmp_path / "cap.glb"]
        assert all(p.exists() for p in layers)

        got = blender.sweep(out)

        assert not any(p.exists() for p in layers)
        assert out.exists()
        assert blender.manifest_path(out).exists()
        assert got["bytes_freed"] > 0

    def test_sweep_keeps_the_history_of_what_it_deleted(self, tmp_path):
        import json as _json

        out, _ = self._assembled(tmp_path)
        blender.sweep(out)

        doc = _json.loads(blender.manifest_path(out).read_text(encoding="utf-8"))
        # The files are gone; the record of the run is not. That is the whole
        # difference between cleanup and amnesia.
        assert len(doc["removed"]) == 2
        assert doc["swept_at"]
        assert [layer["name"] for layer in doc["layers"]] == ["body", "cap"]

    def test_dry_run_deletes_nothing(self, tmp_path):
        out, _ = self._assembled(tmp_path)
        got = blender.sweep(out, dry_run=True)

        assert got["dry_run"] is True
        assert len(got["removed"]) == 2
        assert (tmp_path / "body.glb").exists()

    def test_sweep_refuses_without_a_manifest(self, tmp_path):
        stray = tmp_path / "unknown.glb"
        stray.write_bytes(b"")
        with pytest.raises(FileNotFoundError, match="no manifest"):
            blender.sweep(stray)
