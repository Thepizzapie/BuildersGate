"""blender_animate against a real rig — the picture and the file, not the call.

The pure library is proven in test_humanpose.py. What only Blender can answer
is whether the keyed actions survive the export as clips an engine can play
with the feet where the library put them, whether the facing gate fires on a
skin that faces the other way, and whether an unbound mesh riding inside the
rig gets dropped. Slow, and skipped without Blender, like every sibling.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bgate_adapters import animcurves, blender, bonepaths

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not blender.available()["available"],
                       reason="Blender not installed"),
]

BUILD = '''
import json
base = bg_human(height=1.8, heads=7.5, rig=True, pose="a", finish=True)
body, arm = base["obj"], base["rig"]
bpy.ops.object.select_all(action="DESELECT")
body.select_set(True); arm.select_set(True)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.parent_set(type="ARMATURE_AUTO")
__EXTRA__
'''

# The skin turned to face -Y under a skeleton whose feet still point +Y: the
# exact shape of the rig that walked a whole character backwards.
TURN_SKIN = '''
bpy.ops.object.select_all(action="DESELECT")
body.select_set(True)
bpy.context.view_layer.objects.active = body
import math
body.rotation_euler = (0.0, 0.0, math.pi)
bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
'''


def _rigged(tmp_path: Path, extra: str = "") -> Path:
    out = tmp_path / "rigged.glb"
    got = blender.run_script(BUILD.replace("__EXTRA__", extra),
                             export_glb=str(out), out_dir=str(tmp_path),
                             timeout=300)
    assert got["ok"], got.get("error")
    assert out.is_file()
    return out


def test_the_default_clips_export_and_every_support_gate_passes(tmp_path):
    rigged = _rigged(tmp_path)
    out = tmp_path / "anim.glb"
    rep = blender.animate(str(rigged), str(out), out_dir=str(tmp_path / "proof"),
                          proof_frames=3, textured=False, timeout=900)
    assert rep["ok"], rep.get("error")
    names = {c["action"] for c in rep["clips"] if c["ok"]}
    assert {"idle", "walk", "run", "crouch_idle", "pickup", "look_around"} <= names
    # The .glb carries them as animations an engine can play...
    clips = animcurves.extract_animations(out)
    assert clips["ok"] and {a["name"] for a in clips["animations"]} >= names
    # ...and the feet do what each clip was meant to do, measured off the file.
    assert rep["support"]["measured"]
    assert rep["support"]["passed"], rep["support"]["failed"]
    run = rep["support"]["clips"]["run"]
    walk = rep["support"]["clips"]["walk"]
    assert run["flight_fraction"] > 0.1
    assert walk["flight_fraction"] == 0.0
    # The proof exists and is a picture per clip.
    assert {s["clip"] for s in rep["sheets"]} == names
    for s in rep["sheets"]:
        assert Path(s["path"]).stat().st_size > 1000
    # The bg_human build carries a stray primitive; it did not ship.
    assert any(s["name"].startswith("Icosphere") for s in rep["strays"])
    paths = bonepaths.joint_paths(out, clip="walk")
    assert paths["ok"] and "Icosphere" not in json.dumps(paths.get("clips"))


def test_a_skin_facing_the_other_way_is_refused_then_repaired(tmp_path):
    rigged = _rigged(tmp_path, TURN_SKIN)
    out = tmp_path / "anim.glb"
    rep = blender.animate(str(rigged), str(out), clips=[{"kind": "walk"}],
                          proof_frames=0, timeout=600)
    assert rep["ok"] is False and rep["refused"] is True
    assert "DISAGREE" in rep["error"] and "facing='repair'" in rep["error"]
    assert not out.is_file()
    rep = blender.animate(str(rigged), str(out), clips=[{"kind": "walk"}],
                          proof_frames=0, facing="repair", timeout=600)
    assert rep["ok"], rep.get("error")
    assert rep["facing"]["repaired"]
    assert rep["facing"]["after"]["agrees"] is True
    # Repaired, the rig reads forward as -Y - and is then TURNED to the
    # pipeline's +Y (Godot's -Z) so the game does not play it backwards. The
    # skin turns with it and still agrees; the walk still lands.
    assert rep["oriented"]["turned_deg"] == 180
    assert rep["oriented"]["skin"]["agrees"] is True
    assert rep["rig"]["forward"][1] > 0.9
    assert rep["support"]["clips"]["walk"]["passed"]


# Resolved at IMPORT, before conftest redirects the user directory: the pack
# lives in the real ~/.bgate, and a test that downloads is a test that fails on
# a plane. The fixture below hands that real path back through the redirect.
from bgate_adapters import animlib as _animlib  # noqa: E402

REAL_PACK = _animlib.pack_file("quaternius-ual")


@pytest.fixture
def real_pack(monkeypatch):
    if REAL_PACK is None:
        pytest.skip("quaternius-ual not fetched")
    monkeypatch.setattr(_animlib, "pack_file", lambda name: REAL_PACK)
    return REAL_PACK


def test_a_library_clip_retargets_and_its_feet_land(tmp_path, real_pack):
    """Animator-keyed motion onto a rig it was never made for. The rest poses
    differ (T-pose pack, A-pose rig), the skeletons face opposite ways in
    Blender, and the hips sit at different heights - all three are handled
    by the retarget and all three show in the numbers here."""
    rigged = _rigged(tmp_path)
    out = tmp_path / "anim.glb"
    rep = blender.animate(str(rigged), str(out), proof_frames=0, timeout=900,
                          clips=[{"clip": "Walk_Loop", "name": "walk"},
                                 {"clip": "Jog_Fwd_Loop", "name": "jog"},
                                 {"kind": "idle"}])
    assert rep["ok"], rep.get("error")
    by = {c["name"]: c for c in rep["clips"]}
    assert by["walk"]["ok"] and by["jog"]["ok"] and by["idle"]["ok"]
    notes = by["walk"]["notes"]
    assert notes["kind"] == "library" and notes["mapped"] >= 20
    assert notes["unmapped_target"] == []
    assert abs(notes["yaw_deg"]) in (0.0, 180.0)
    # Walk keeps a foot down; the jog flies; both read off the exported file.
    assert rep["support"]["clips"]["walk"]["passed"]
    assert rep["support"]["clips"]["jog"]["passed"]
    assert rep["support"]["clips"]["jog"]["flight_fraction"] > 0.1
    # And the source skeleton did not ship inside the character.
    paths = bonepaths.joint_paths(out, clip="walk")
    assert "DEF-hips" not in json.dumps(paths.get("clips"))


def test_a_missing_pack_clip_is_named_not_guessed(tmp_path, real_pack):
    rigged = _rigged(tmp_path)
    rep = blender.animate(str(rigged), str(tmp_path / "anim.glb"), proof_frames=0,
                          clips=[{"clip": "Moonwalk_Loop", "pack": "quaternius-ual"}])
    assert rep["ok"] is False
    assert "Moonwalk_Loop" in rep["error"]


def test_a_keyed_clip_in_character_terms_lands(tmp_path):
    rigged = _rigged(tmp_path)
    out = tmp_path / "anim.glb"
    bow = {"name": "bow", "kind": "keyed", "loop": False,
           "keys": [{"t": 0.0}, {"t": 0.8, "lean": 45.0, "head_pitch": 15.0},
                    {"t": 1.6}]}
    rep = blender.animate(str(rigged), str(out), clips=[bow], proof_frames=0,
                          timeout=600)
    assert rep["ok"], rep.get("error")
    rec = rep["clips"][0]
    assert rec["action"] == "bow" and rec["notes"]["ignored_fields"] == []
    paths = bonepaths.joint_paths(out, clip="bow", joints=["Head", "Hips"])
    entry = paths["clips"][0]
    head = entry["positions"]["Head"]
    # The Head JOINT is the neck base, a short chain above the hips: a 45
    # degree bend carries it ~14 cm FORWARD and only ~4 cm down. glTF is
    # Y-up with Blender's +Y forward landing on -Z.
    mid = head[len(head) // 2]
    assert mid[2] < head[0][2] - 0.10          # forward
    assert mid[1] < head[0][1] - 0.02          # and lower
    assert abs(head[-1][1] - head[0][1]) < 0.02
    assert abs(head[-1][2] - head[0][2]) < 0.02
