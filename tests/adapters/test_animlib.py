"""The animation-library adapter: bone maps, the pack reader, the one download.

The pack itself is optional and lives outside the repo, so everything that
can be proven without it is proven on synthetic input, and the tests that
need the real bytes skip when it is not fetched rather than fetching it - a
test suite that downloads is a test suite that fails on a plane.
"""
from __future__ import annotations

import base64
import json
import struct

import pytest

from bgate_adapters import animlib


class TestBoneMaps:
    def test_rigify_def_names_map_to_the_profile(self):
        names = ["root", "DEF-hips", "DEF-spine.001", "DEF-spine.002",
                 "DEF-spine.003", "DEF-neck", "DEF-head", "DEF-shoulder.L",
                 "DEF-upper_arm.L", "DEF-forearm.L", "DEF-hand.L",
                 "DEF-thigh.R", "DEF-shin.R", "DEF-foot.R", "DEF-toe.R",
                 "DEF-f_index.01.L"]
        got = animlib.bone_map(names, "rigify")
        assert got["DEF-hips"] == "Hips"
        assert got["DEF-spine.002"] == "Chest"
        assert got["DEF-upper_arm.L"] == "LeftUpperArm"
        assert got["DEF-forearm.L"] == "LeftLowerArm"
        assert got["DEF-thigh.R"] == "RightUpperLeg"
        assert got["DEF-toe.R"] == "RightToes"
        # fingers and the root are not profile bones and are left out
        assert "DEF-f_index.01.L" not in got and "root" not in got

    def test_mixamo_names_map_with_or_without_the_prefix(self):
        got = animlib.bone_map(["mixamorig:Hips", "mixamorig:LeftUpLeg",
                                "RightForeArm", "Spine2", "LeftToeBase"], "mixamo")
        assert got == {"mixamorig:Hips": "Hips", "mixamorig:LeftUpLeg": "LeftUpperLeg",
                       "RightForeArm": "RightLowerArm", "Spine2": "UpperChest",
                       "LeftToeBase": "LeftToes"}

    def test_auto_picks_the_naming_that_maps_the_most(self):
        rigify = ["DEF-hips", "DEF-thigh.L", "DEF-shin.L", "DEF-head"]
        assert set(animlib.bone_map(rigify, "auto").values()) == {
            "Hips", "LeftUpperLeg", "LeftLowerLeg", "Head"}
        godot = ["Hips", "LeftUpperLeg", "Head", "Extra"]
        assert animlib.bone_map(godot, "auto") == {"Hips": "Hips",
                                                    "LeftUpperLeg": "LeftUpperLeg",
                                                    "Head": "Head"}

    def test_an_unknown_naming_is_refused(self):
        with pytest.raises(ValueError):
            animlib.bone_map(["Hips"], "unreal")


def _tiny_gltf(tmp_path, *, embed: bool):
    """A one-node, one-animation glTF: enough to read names and durations."""
    times = struct.pack("<3f", 0.0, 0.5, 1.25)
    values = struct.pack("<12f", 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1)
    blob = times + values
    doc = {
        "asset": {"version": "2.0"},
        "nodes": [{"name": "root", "children": [1]}, {"name": "DEF-hips"}],
        "skins": [{"joints": [0, 1]}],
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 12},
                        {"buffer": 0, "byteOffset": 12, "byteLength": 48}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3,
                       "type": "SCALAR", "min": [0.0], "max": [1.25]},
                      {"bufferView": 1, "componentType": 5126, "count": 3,
                       "type": "VEC4"}],
        "animations": [{"name": "Walk_Loop",
                        "samplers": [{"input": 0, "output": 1}],
                        "channels": [{"sampler": 0,
                                      "target": {"node": 1, "path": "rotation"}}]},
                       {"name": "Roll_RM",
                        "samplers": [{"input": 0, "output": 1}],
                        "channels": [{"sampler": 0,
                                      "target": {"node": 1, "path": "rotation"}}]}],
    }
    if embed:
        doc["buffers"][0]["uri"] = ("data:application/octet-stream;base64,"
                                    + base64.b64encode(blob).decode())
    else:
        doc["buffers"][0]["uri"] = "tiny.bin"
        (tmp_path / "tiny.bin").write_bytes(blob)
    path = tmp_path / "tiny.gltf"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class TestReadingAPack:
    def test_a_gltf_with_a_sibling_bin_reads(self, tmp_path):
        doc, blob = animlib.read_gltf(_tiny_gltf(tmp_path, embed=False))
        assert doc["animations"][0]["name"] == "Walk_Loop"
        assert len(blob) == 60

    def test_a_gltf_with_an_embedded_buffer_reads(self, tmp_path):
        _, blob = animlib.read_gltf(_tiny_gltf(tmp_path, embed=True))
        assert len(blob) == 60

    def test_clips_come_with_duration_loop_and_root_motion(self, tmp_path, monkeypatch):
        """Read through the pack machinery, with the pack redirected to the
        synthetic file - so this is the same path blender_animate resolves."""
        path = _tiny_gltf(tmp_path, embed=False)
        monkeypatch.setattr(animlib, "pack_file", lambda name: path)
        rows = animlib.clips("quaternius-ual")
        assert rows == [{"name": "Walk_Loop", "seconds": 1.25, "loop": True,
                         "root_motion": False},
                        {"name": "Roll_RM", "seconds": 1.25, "loop": False,
                         "root_motion": True}]
        resolved = animlib.resolve("quaternius-ual")
        assert resolved["ok"] and resolved["bone_map"] == {"DEF-hips": "Hips"}
        assert resolved["unmapped"] == ["root"]


class TestTheCacheAndTheRefusals:
    def test_home_follows_bgate_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BGATE_HOME", str(tmp_path / "elsewhere"))
        assert animlib.home() == tmp_path / "elsewhere" / "animlib"
        st = animlib.status()
        row = st["packs"]["quaternius-ual"]
        assert row["fetched"] is False and row["fetch"] == "bgate animlib fetch quaternius-ual"

    def test_an_unfetched_pack_resolves_to_the_command_not_a_download(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BGATE_HOME", str(tmp_path))
        calls = []
        monkeypatch.setattr(animlib.urllib.request, "urlopen",
                            lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("downloaded")))
        got = animlib.resolve("quaternius-ual")
        assert got["ok"] is False and "bgate animlib fetch" in got["error"]
        assert calls == []

    def test_a_hash_mismatch_unpacks_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BGATE_HOME", str(tmp_path))

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"not the pack"
        monkeypatch.setattr(animlib.urllib.request, "urlopen", lambda *a, **k: _Resp())
        got = animlib.fetch("quaternius-ual")
        assert got["ok"] is False and "SHA-256" in got["error"]
        assert not (tmp_path / "animlib" / "quaternius-ual").exists()

    def test_an_unknown_pack_is_named(self):
        assert "no such pack" in animlib.fetch("nope")["error"]
        assert animlib.resolve("nope")["ok"] is False

    def test_the_doctor_row_is_never_a_failure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BGATE_HOME", str(tmp_path))
        row = animlib.doctor_row()
        assert row["available"] is False
        assert "bgate animlib fetch quaternius-ual" in row["reason"]


# Resolved at IMPORT, before conftest redirects the user directory to a temp
# dir: the pack lives in the real ~/.bgate, and the fixture hands that path
# back through the redirect. A test that downloads fails on a plane.
REAL_PACK = animlib.pack_file("quaternius-ual")


@pytest.fixture
def real_pack(monkeypatch):
    if REAL_PACK is None:
        pytest.skip("quaternius-ual not fetched")
    monkeypatch.setattr(animlib, "pack_file", lambda name: REAL_PACK)
    return REAL_PACK


@pytest.mark.usefixtures("real_pack")
class TestTheRealPack:
    def test_every_profile_limb_bone_is_mapped(self):
        resolved = animlib.resolve("quaternius-ual")
        mapped = set(resolved["bone_map"].values())
        for bone in ("Hips", "Spine", "Chest", "UpperChest", "Neck", "Head",
                     "LeftUpperArm", "LeftLowerArm", "LeftHand",
                     "RightUpperLeg", "RightLowerLeg", "RightFoot", "RightToes"):
            assert bone in mapped, bone
        assert all("f_" in n or "thumb" in n or n == "root"
                   for n in resolved["unmapped"])

    def test_the_locomotion_clips_are_there_and_loop(self):
        clips = {c["name"]: c for c in animlib.clips("quaternius-ual")}
        for name in ("Walk_Loop", "Jog_Fwd_Loop", "Sprint_Loop", "Idle_Loop",
                     "Crouch_Idle_Loop", "PickUp_Table"):
            assert name in clips, name
        assert clips["Walk_Loop"]["loop"] and not clips["PickUp_Table"]["loop"]
        assert clips["Roll_RM"]["root_motion"]
