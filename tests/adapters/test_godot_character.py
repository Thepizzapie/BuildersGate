"""character_wire without an engine: the clip resolution, the scene text,
and the verdict on a probe. The engine half is proven by running it (see
the module docstring); here the writing is the thing under test."""
from __future__ import annotations

import pytest

from bgate_adapters import godot_character as gc


class TestResolveClips:
    def test_humanoid_defaults_land_in_speed_order(self):
        r = gc.resolve_clips(["idle", "walk", "run", "jump", "hit", "wave"])
        assert r["locomotion"] == ["idle", "walk", "run"]
        assert r["jump"] == "jump"
        assert r["actions"] == ["hit", "wave"]
        assert r["missing"] == ["fall", "land"]

    def test_a_pack_s_names_resolve(self):
        r = gc.resolve_clips(["Idle_Loop", "Walk_Loop", "Jog_Fwd_Loop", "Sprint_Loop",
                              "Jump_Start", "Jump_Loop", "Jump_Land", "PickUp_Table"])
        assert r["locomotion"] == ["Idle_Loop", "Walk_Loop", "Jog_Fwd_Loop", "Sprint_Loop"]
        assert r["jump"] == "Jump_Start"
        assert r["fall"] == "Jump_Loop"
        assert r["land"] == "Jump_Land"
        assert r["actions"] == ["PickUp_Table"]

    def test_quadruped_gaits_rank_walk_trot_gallop(self):
        r = gc.resolve_clips(["gallop", "alert", "trot", "walk", "idle"])
        assert r["locomotion"] == ["idle", "walk", "trot", "gallop"]
        assert r["actions"] == ["alert"]

    def test_overrides_pin_a_role(self):
        r = gc.resolve_clips(["A", "B", "C"], {"idle": "A", "walk": "B", "jump": "C"})
        assert r["locomotion"] == ["A", "B"]
        assert r["jump"] == "C"
        assert r["actions"] == []

    def test_loop_suffix_is_ignored_for_matching(self):
        r = gc.resolve_clips(["walk-loop", "idle-loop"])
        assert r["locomotion"] == ["idle-loop", "walk-loop"]

    def test_nothing_readable_reports_missing_locomotion(self):
        r = gc.resolve_clips(["A", "B"])
        assert r["locomotion"] == []
        assert "idle/walk" in r["missing"]


class TestSceneText:
    def _scene(self, **kw):
        kw.setdefault("node_name", "Hero")
        kw.setdefault("bounds_size", [0.6, 1.8, 0.4])
        kw.setdefault("bounds_position", [-0.3, 0.0, -0.2])
        kw.setdefault("resolved", gc.resolve_clips(["idle", "walk", "run", "jump", "hit"]))
        kw.setdefault("controller_res", "res://scripts/third_person_controller.gd")
        kw.setdefault("animator_res", "res://scripts/character_animator.gd")
        return gc.character_scene_text("res://assets/hero.glb", **kw)

    def test_the_model_keeps_its_feet_on_the_origin_and_the_capsule_is_lifted(self):
        text = self._scene()
        assert 'name="Model" parent="." instance=ExtResource("1_model")' in text
        assert "transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0.0000, 0)" in text
        assert "0.9000, 0)\nshape = SubResource(\"CapsuleShape3D_body\")" in text

    def test_a_centred_model_is_lifted_onto_the_floor(self):
        text = self._scene(bounds_position=[-0.3, -0.9, -0.2])
        assert "0, 0.9000, 0)\n\n[node name=\"CollisionShape3D\"" in text

    def test_the_animator_carries_every_role(self):
        text = self._scene()
        assert 'locomotion = PackedStringArray("idle", "walk", "run")' in text
        assert 'clip_jump = "jump"' in text
        assert 'actions = PackedStringArray("hit")' in text
        assert 'anim_player = NodePath("../Model/AnimationPlayer")' in text
        assert 'script = ExtResource("3_anim")' in text

    def test_no_controller_means_no_script_on_the_body(self):
        text = self._scene(controller_res="")
        assert 'script = ExtResource("2_ctrl")' not in text
        assert "load_steps=5" in text


class TestJudgeProbe:
    def _probe(self, states, steps, **extra):
        return {"measured": True, "ok": True,
                "report": {"built": True, "states": states, "missing": [], "looped": []},
                "steps": [{"step": i + 1, "state": s, "blend": b} for i, (s, b) in enumerate(steps)],
                **extra}

    def test_a_machine_that_reaches_every_state_passes(self):
        probe = self._probe(["Locomotion", "Jump", "hit"],
                            [("Locomotion", 0), ("Locomotion", 4), ("Locomotion", 7),
                             ("Jump", 7), ("Jump", 7), ("Locomotion", 0),
                             ("hit", 0), ("hit", 0)],
                            action_tried="hit", action_accepted=True)
        v = gc.judge_probe(probe, {"locomotion": ["idle", "walk", "run"]})
        assert v["passed"], v["issues"]

    def test_a_jump_that_never_lands_fails(self):
        probe = self._probe(["Locomotion", "Jump"],
                            [("Locomotion", 0), ("Locomotion", 4), ("Locomotion", 7),
                             ("Jump", 7), ("Jump", 7), ("Jump", 0), ("Jump", 0), ("Jump", 0)])
        v = gc.judge_probe(probe, {"locomotion": ["idle", "walk", "run"]})
        assert not v["passed"]
        assert any("touchdown" in i for i in v["issues"])

    def test_a_blend_that_ignores_speed_fails(self):
        probe = self._probe(["Locomotion"], [("Locomotion", 0)] * 8)
        v = gc.judge_probe(probe, {"locomotion": ["idle", "walk"]})
        assert any("blend" in i for i in v["issues"])

    def test_an_unmeasured_probe_fails_closed(self):
        v = gc.judge_probe({"measured": False, "error": "no engine"}, {})
        assert v["passed"] is False and v["issues"] == ["no engine"]


def test_wire_refuses_a_missing_project(tmp_path):
    r = gc.wire_character(str(tmp_path), "x.glb")
    assert r["ok"] is False and "project.godot" in r["error"]


def test_the_template_ships_the_animator():
    assert (gc.TEMPLATE_SCRIPTS / gc.ANIMATOR_SCRIPT).is_file()
    for script in gc.CONTROLLERS.values():
        if script:
            assert (gc.TEMPLATE_SCRIPTS / script).is_file()


@pytest.mark.parametrize("controller", ["banana"])
def test_wire_refuses_an_unknown_controller(tmp_path, controller):
    (tmp_path / "project.godot").write_text("", encoding="utf-8")
    r = gc.wire_character(str(tmp_path), "x.glb", controller=controller)
    assert r["ok"] is False and "controller" in r["error"]
