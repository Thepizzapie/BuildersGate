"""The engine-retarget and clip-capture helpers, without an engine: the
BoneMap text, the profile identity map, the name matching, the sheets, the
scene text with libraries and IK, the IK verdict."""
from __future__ import annotations

import pytest

from bgate_adapters import godot as _godot
from bgate_adapters import godot_capture as gcap
from bgate_adapters import godot_character as gc
from bgate_adapters import godot_retarget as gr


class TestBoneMap:
    def test_the_map_inverts_to_profile_keys_and_embeds_the_profile(self):
        text = gr.bone_map_tres({"DEF-hips": "Hips", "DEF-head": "Head", "root": "Root"})
        assert '[gd_resource type="BoneMap"' in text
        assert 'type="SkeletonProfileHumanoid"' in text
        assert 'bone_map/Hips = "DEF-hips"' in text
        assert 'bone_map/Root = "root"' in text

    def test_a_profile_bone_mapped_twice_keeps_the_first(self):
        text = gr.bone_map_tres({"a": "Hips", "b": "Hips"})
        assert text.count("bone_map/Hips") == 1 and '"a"' in text

    def test_profile_identity_keeps_profile_names_and_lifts_root(self):
        m = gr.profile_identity(["Root", "Hips", "LeftFoot", "Tail1", "root2"])
        assert m == {"Root": "Root", "Hips": "Hips", "LeftFoot": "LeftFoot"}
        assert gr.profile_identity(["root"]) == {"root": "Root"}

    def test_loop_suffix_is_stripped_case_insensitively(self):
        assert gr._loopless("Walk_Loop") == "walk"
        assert gr._loopless("walk-loop") == "walk"
        assert gr._loopless("Roll_RM") == "roll_rm"


class TestImportSettingsCarryResources:
    def test_resource_literal_survives_a_merge(self, tmp_path):
        """The retarget block holds Resource("res://x.tres"); parsing it as
        JSON failed and every rewrite REPLACED the node dict."""
        asset = tmp_path / "a.glb"
        asset.write_bytes(b"x")
        ini = tmp_path / "a.glb.import"
        ini.write_text('[remap]\nimporter="scene"\n\n[params]\nanimation/import=true\n'
                       '_subresources={\n"nodes": {\n"PATH:Rig/Skeleton3D": {\n'
                       '"retarget/bone_map": Resource("res://m.tres"),\n'
                       '"retarget/rest_fixer/overwrite_axis": true\n}\n}\n}\n',
                       encoding="utf-8")
        got = _godot.write_import_settings(str(tmp_path), "a.glb",
                                           node_params={"Rig/Skeleton3D": {"x/y": 1}},
                                           purge=False)
        assert got["ok"] and not got["merge_error"]
        text = ini.read_text(encoding="utf-8")
        assert 'Resource("res://m.tres")' in text
        assert '"retarget/rest_fixer/overwrite_axis": true' in text
        assert '"x/y": 1' in text

    def test_raw_literal_is_written_verbatim(self):
        assert _godot._gd_literal(_godot.RawLiteral('Resource("res://a")')) == 'Resource("res://a")'
        assert _godot._gd_literal("plain") == '"plain"'


class TestSceneTextWithLibrariesAndIK:
    def test_libraries_and_ik_land_in_the_scene(self):
        text = gc.character_scene_text(
            "res://assets/hero.glb", node_name="Hero", bounds_size=[0.6, 1.8, 0.4],
            bounds_position=[-0.3, 0.0, -0.2],
            resolved=gc.resolve_clips(["idle", "walk", "ual/Jog_Fwd"]),
            animator_res="res://scripts/character_animator.gd",
            ik_res="res://scripts/character_ik.gd",
            libraries=["res://assets/anim/ual.res"])
        assert 'libraries = PackedStringArray("res://assets/anim/ual.res")' in text
        assert '[node name="IK" type="Node" parent="."]' in text
        assert 'script = ExtResource("4_ik")' in text
        assert 'locomotion = PackedStringArray("idle", "walk", "ual/Jog_Fwd")' in text

    def test_a_pin_onto_an_already_placed_clip_does_not_duplicate_it(self):
        r = gc.resolve_clips(["idle", "walk", "run", "ual/Idle", "ual/Walk", "ual/Jog_Fwd", "ual/Sprint"],
                             {"idle": "ual/Idle", "walk": "ual/Walk", "run": "ual/Jog_Fwd"})
        assert r["locomotion"] == ["ual/Idle", "ual/Walk", "ual/Jog_Fwd", "ual/Sprint"]
        assert r["actions"] == []

    def test_own_clips_beat_library_clips_for_a_role(self):
        r = gc.resolve_clips(["idle", "walk", "jump", "ual/Idle", "ual/Walk", "ual/Jump_Start"])
        assert r["locomotion"] == ["idle", "walk"]
        assert r["jump"] == "jump"
        # a library walk that lost the role is superseded, not a one-shot
        assert r["actions"] == ["ual/Jump_Start"]


class TestIKVerdict:
    def _probe(self, ik):
        return {"measured": True, "ok": True,
                "report": {"built": True, "states": ["Locomotion"], "missing": [], "looped": []},
                "steps": [{"step": 1, "state": "Locomotion", "blend": 0.0}],
                "ik": ik}

    def test_feet_on_the_ramp_pass(self):
        v = gc.judge_probe(self._probe({"feet": ["LeftFoot", "RightFoot"], "weight": 1.0,
                                        "hits": {"LeftFoot": {"hit": True, "error": 0.01},
                                                 "RightFoot": {"hit": True, "error": 0.02}},
                                        "errors": []}), {"locomotion": ["idle"]})
        assert v["passed"], v["issues"]
        assert v["ik"]["foot_error_m"]["RightFoot"] == 0.02

    def test_a_foot_off_the_ramp_or_no_contact_fails(self):
        v = gc.judge_probe(self._probe({"feet": ["LeftFoot"], "weight": 0.1,
                                        "hits": {"LeftFoot": {"hit": True, "error": 0.2}},
                                        "errors": []}), {"locomotion": ["idle"]})
        assert not v["passed"]
        assert any("off the ramp" in i for i in v["issues"])
        assert any("weight" in i for i in v["issues"])


class TestSheets:
    def test_frames_compose_one_sheet_per_clip(self, tmp_path):
        PIL = pytest.importorskip("PIL.Image")
        frames = []
        for clip in ("walk", "ual/Walk"):
            for view in ("side", "front34"):
                for i in range(2):
                    path = tmp_path / f"{clip.replace('/', '-')}_{view}_{i}.png"
                    PIL.new("RGB", (8, 12), (i * 90, 20, 20)).save(path)
                    frames.append({"clip": clip, "view": view, "sample": i, "path": str(path)})
        sheets = gcap._sheets(frames, tmp_path)
        assert [s["clip"] for s in sheets] == ["walk", "ual/Walk"]
        for s in sheets:
            assert s["frames"] == 4
            img = PIL.open(s["path"])
            assert img.size == (16, 24 + 24)


def test_the_template_ships_the_ik_script():
    assert (gc.TEMPLATE_SCRIPTS / gc.IK_SCRIPT).is_file()
