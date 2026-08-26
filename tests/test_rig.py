"""Binding a generated mesh, and the count that is the only proof it happened.

THE FAILURE THIS MODULE EXISTS FOR: Blender's parent_set returns cleanly,
creates every vertex group, attaches the modifier — and can leave all of those
groups empty. Godot then loads a character with a Skeleton3D that animates
nothing. Measured on a real generation: 64,878 of 64,878 vertices unweighted
with every other check green. So the operator's success is not evidence, the
group count is not evidence, and the modifier is not evidence. The number of
vertices carrying no weight is.
"""
from __future__ import annotations

import json


from bgate_adapters import blender


class TestRigVerdict:
    """The report shape, without spawning Blender."""

    def _report(self, monkeypatch, marked: dict):
        def fake_run(script, **kw):
            return {"ok": True, "seconds": 1.0,
                    "print": blender._RIG_MARK + json.dumps(marked)}
        monkeypatch.setattr(blender, "run_script", fake_run)
        monkeypatch.setattr(blender.Path, "is_file", lambda self: True)
        return blender.rig("in.glb", "out.glb")

    def test_a_bind_that_weighted_nothing_is_not_rigged(self, monkeypatch):
        """The exact shape of the real failure: groups made, weights absent."""
        got = self._report(monkeypatch, {
            "ok": True, "rigged": False, "unweighted": 64878,
            "unweighted_pct": 100.0, "bound_with": "ARMATURE_ENVELOPE",
            "attempts": [{"bind": "ARMATURE_AUTO", "verts": 64878,
                          "unweighted": 64878, "groups": 22, "error": ""}]})
        assert got["ok"] is True          # the RUN worked
        assert got["rigged"] is False     # the ASSET did not
        assert got["unweighted"] == 64878
        # 22 groups existed the whole time — the count is what knew better.
        assert got["attempts"][0]["groups"] == 22

    def test_a_real_bind_reports_rigged(self, monkeypatch):
        got = self._report(monkeypatch, {
            "ok": True, "rigged": True, "unweighted": 3,
            "unweighted_pct": 0.015, "bound_with": "ARMATURE_AUTO"})
        assert got["rigged"] is True
        assert got["bound_with"] == "ARMATURE_AUTO"

    def test_a_missing_model_is_a_result_not_an_exception(self):
        got = blender.rig("no/such/file.glb", "out.glb")
        assert got["ok"] is False
        assert "no model" in got["error"]

    def test_a_silent_blender_run_is_reported(self, monkeypatch):
        """No marked line means no report — that must not read as success."""
        monkeypatch.setattr(blender, "run_script",
                            lambda script, **kw: {"ok": True, "print": ""})
        monkeypatch.setattr(blender.Path, "is_file", lambda self: True)
        got = blender.rig("in.glb", "out.glb")
        assert got["ok"] is False
        assert "no report" in got["error"]


class TestScriptContract:
    """Properties of the injected script that the failure depended on."""

    def test_adopt_and_bind_run_in_one_session(self):
        """Splitting them across a file is what unbound the mesh.

        glTF re-import carries a root transform, so a skeleton built after a
        round trip sits in a different space and heat finds nothing to weight.
        Both calls must appear in the SAME script.
        """
        src = blender._rig_script()
        assert "bg_adopt(" in src and "bg_human(" in src
        assert src.index("bg_adopt(") < src.index("bg_human(")
        assert "import_scene.gltf" in src
        assert src.count("import_scene.gltf") == 1

    def test_heat_is_tried_before_envelope(self):
        src = blender._rig_script()
        assert src.index("ARMATURE_AUTO") < src.index("ARMATURE_ENVELOPE")

    def test_the_verdict_counts_vertices_without_groups(self):
        assert "if not v.groups" in blender._rig_script()


class TestHumanoidTemplate:
    """The template has to SHIP, or a first-run user gets none of this.

    It is the fixed end of the pipeline: art conforms to the skeleton rather
    than the skeleton being bent to fit each generation. Measured on one
    character, bones further than 6 cm from any mesh vertex — 16 of 24 with the
    template scaled by height, 8 of 23 with the plate conditioned on it, 5 of 23
    with landmark fitting, 0 of 23 with both.
    """

    def test_the_assets_are_present(self):
        got = blender.humanoid_template()
        assert got["ok"] is True, got["reason"]
        for key in ("skeleton", "pose_front"):
            assert blender.Path(got[key]).is_file(), key

    def test_the_bone_set_is_godots_humanoid_profile(self):
        """Retargeting depends on these exact names, so they are pinned."""
        bones = blender.humanoid_template()["bones"]
        assert len(bones) == 23
        for required in ("Hips", "Spine", "Chest", "Head",
                         "LeftUpperArm", "LeftLowerArm", "LeftHand",
                         "RightUpperLeg", "RightLowerLeg", "RightFoot"):
            assert required in bones, required

    def test_it_ships_in_the_wheel(self):
        """package-data covers templates/**/*, and this lives under it.

        A wheel without these is a first run with no humanoid template, which
        is the entire point of shipping them rather than building per project.
        """
        root = blender.Path(blender.__file__).resolve().parent.parent
        rel = blender.HUMANOID_SKELETON.relative_to(root)
        assert rel.parts[0] == "templates", rel

    def test_the_pose_clause_names_the_stance(self):
        clause = blender.humanoid_template()["pose_clause"]
        for word in ("T-pose", "reference", "symmetrical"):
            assert word in clause, word

    def test_it_is_not_inside_a_project_scaffold(self):
        """templates/3d IS the Godot scaffold `bgate init --kind 3d` copies.

        Anything left there lands in the root of every new 3D game. The
        humanoid template ships beside it, not inside it — caught after the
        first version put a skeleton and two 1.5 MB plates into the scaffold.
        """
        parent = blender.HUMANOID_SKELETON.parent
        assert parent.name == "humanoid", parent
        scaffold = parent.parent / "3d"
        if scaffold.is_dir():
            stray = [p.name for p in scaffold.iterdir() if "humanoid" in p.name]
            assert not stray, stray
