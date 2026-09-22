"""The rig's eyes: a proof sheet after every write, in the response.

USER DIRECTIVE (2026-09-22): "give the art agent eyes to look at this
instead of guessing every time."
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import shutil

from bgate_core.three_d import cutout, cutoutproof, cutoutwire


SIZES = {
    "head": (44, 46), "torso": (46, 62), "hip": (40, 24),
    "arm_near": (18, 34), "forearm_near": (16, 32), "hand_near": (14, 14),
    "arm_far": (18, 34), "forearm_far": (16, 32), "hand_far": (14, 14),
    "thigh_near": (20, 46), "shin_near": (18, 44), "foot_near": (28, 14),
    "thigh_far": (20, 46), "shin_far": (18, 44), "foot_far": (28, 14),
}


def _parts(root: Path) -> dict:
    from PIL import Image

    root.mkdir(parents=True, exist_ok=True)
    made = {}
    for slot, size in SIZES.items():
        png = root / f"{slot}.png"
        Image.new("RGBA", size, (200, 120, 90, 255)).save(png)
        made[slot] = {"texture": str(png), "part_hash": cutout.part_hash(png)}
    for far, near in cutout.BIPED_V1["reuse"].items():
        made[far]["texture"] = made[near]["texture"]
        made[far]["reuse_of"] = near
        made[far]["far_tint"] = cutout.BIPED_V1["far_tint"]
    return made


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "addons" / "bgate").mkdir(parents=True)
    (root / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="cutout"\n',
        encoding="utf-8")
    shutil.copy(Path(__file__).resolve().parents[2] / "src" / "templates" /
                "cutout" / "cutout_rig.gd",
                root / "addons" / "bgate" / "cutout_rig.gd")
    return root


@pytest.fixture
def doc(project):
    d = cutout.empty("hero")
    d["skin"] = _parts(project / "game" / "assets" / "characters" / "hero" / "parts")
    return cutout.normalise(d)


def _fake_shooter(calls):
    def shoot(project_dir, out_path, *, at, scene, timeout):
        from PIL import Image
        calls.append({"scene": scene, "at": at})
        Image.new("RGBA", (64, 36), (200, 196, 188, 255)).save(out_path)
        return {"ok": True, "path": str(out_path), "errors": []}
    return shoot


@pytest.fixture
def shooter(monkeypatch):
    calls: list = []
    monkeypatch.setattr(cutoutproof, "shooter", _fake_shooter(calls))
    return calls


def test_the_gym_places_one_rig_per_pose_with_a_label(project, doc):
    scene = project / "game" / "hero.tscn"
    cutoutwire.emit(doc, project_dir=project, scene_path=scene)
    gym = cutoutproof.write_gym(project, "hero", scene)
    text = Path(gym["gym"]).read_text(encoding="utf-8")
    assert text.count('instance=ExtResource("2_rig")') == len(cutoutproof.PROOF_POSES)
    assert 'path="res://game/hero.tscn"' in text
    script = Path(gym["script"]).read_text(encoding="utf-8")
    table = json.loads(script.split("const POSES := ")[1].split("\n")[0])
    assert table["P0"] == ["idle", 0.5, True]
    assert all(v[2] for v in table.values())          # every pose is a shipped clip


def test_proof_renders_the_gym_into_the_shots_folder(project, doc, shooter):
    scene = project / "game" / "hero.tscn"
    cutoutwire.emit(doc, project_dir=project, scene_path=scene)
    got = cutoutproof.proof(project, "hero", scene)
    assert got["ok"] and Path(got["image"]).is_file()
    assert Path(got["image"]).name.startswith("rigproof-hero-")
    assert "shots" in Path(got["image"]).parts
    assert shooter[0]["scene"].endswith("hero_proof_gym.tscn")
    assert "LOOK AT IT" in got["look"]
    assert cutoutproof.latest_proof(project, "hero") == Path(got["image"])


def test_a_proof_that_cannot_render_is_reported_not_raised(project, doc, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("no display")
    monkeypatch.setattr(cutoutproof, "shooter", broken)
    scene = project / "game" / "hero.tscn"
    cutoutwire.emit(doc, project_dir=project, scene_path=scene)
    got = cutoutproof.proof(project, "hero", scene)
    assert got["ok"] is False and got["image"] == "" and "no display" in got["error"]


# ---------------------------------------------------------------------------
# Game scale: the emitter sizes the rig, the proof shows it beside the player
# ---------------------------------------------------------------------------

def test_the_extent_is_measured_from_the_parts_not_the_template(doc):
    got = cutout.rest_extent(doc, SIZES)
    assert got is not None and got["height"] > 0
    tall = {k: (w, h * 2) for k, (w, h) in SIZES.items()}
    assert cutout.rest_extent(doc, tall)["height"] > got["height"]


def test_the_emitter_scales_and_lifts_visual_to_the_player_height(project, doc):
    scene = project / "game" / "hero.tscn"
    got = cutoutwire.emit(doc, project_dir=project, scene_path=scene,
                          sizes=SIZES, player_height_px=128)
    fit = got["game_fit"]
    extent = cutout.rest_extent(doc, SIZES)
    assert fit["game_height_px"] == 128
    assert abs(fit["scale"] * extent["height"] - 128) < 0.5
    assert abs(fit["lift"] + extent["bottom"] * fit["scale"]) < 0.01
    text = scene.read_text(encoding="utf-8")
    visual = text.split('[node name="Visual"')[1].split("[node")[0]
    assert f"scale = Vector2({fit['scale']:.6g}" in visual
    stamp = json.loads(cutoutwire.stamp_path(scene).read_text(encoding="utf-8"))
    assert stamp["game_fit"]["game_height_px"] == 128


def test_height_ratio_and_an_explicit_height_win(doc):
    d = dict(doc, height_ratio=2.0)
    assert cutoutwire.game_fit(d, SIZES, 128)["game_height_px"] == 256
    d = dict(doc, game_height_px=300)
    assert cutoutwire.game_fit(d, SIZES, 128)["game_height_px"] == 300


def test_no_player_height_leaves_the_rig_alone_and_says_why(doc):
    fit = cutoutwire.game_fit(doc, SIZES, 0)
    assert fit["scale"] == 1.0 and "scale_contract_set" in fit["source"]


def test_a_game_scene_that_rescales_the_rig_is_reported(project, doc):
    scene = project / "game" / "hero.tscn"
    cutoutwire.emit(doc, project_dir=project, scene_path=scene, sizes=SIZES,
                    player_height_px=128)
    player = project / "scenes" / "player.tscn"
    player.parent.mkdir(parents=True)
    player.write_text(
        '[gd_scene load_steps=2 format=3]\n\n'
        '[ext_resource type="PackedScene" path="res://game/hero.tscn" id="6_rig"]\n\n'
        '[node name="Player" type="Node2D"]\n\n'
        '[node name="Rig" parent="." instance=ExtResource("6_rig")]\n'
        'scale = Vector2(0.64, 0.64)\n', encoding="utf-8")
    gym = project / "scenes" / "hero_gym.tscn"
    gym.write_text(player.read_text(encoding="utf-8"), encoding="utf-8")
    found = cutoutwire.instance_overrides(project, scene)
    assert [(f["scene"], f["node"]) for f in found] == [
        ("res://scenes/player.tscn", "Rig")]


def test_the_proof_is_at_game_scale_beside_a_player_height_bar(project, doc):
    scene = project / "game" / "hero.tscn"
    cutoutwire.emit(doc, project_dir=project, scene_path=scene, sizes=SIZES,
                    player_height_px=128)
    gym = cutoutproof.write_gym(project, "hero", scene, reference_px=128,
                                game_height_px=128)
    text = Path(gym["gym"]).read_text(encoding="utf-8")
    assert gym["view_scale"] == 1.0
    assert "scale = Vector2(1.0, 1.0)" in text
    assert text.count('[node name="Ref') == len(cutoutproof.PROOF_POSES)
    assert "player height 128 px" in text


def test_a_boss_shrinks_rig_and_bar_together(project, doc):
    scene = project / "game" / "hero.tscn"
    cutoutwire.emit(doc, project_dir=project, scene_path=scene)
    gym = cutoutproof.write_gym(project, "hero", scene, reference_px=128,
                                game_height_px=600)
    assert gym["view_scale"] < 1.0


def test_defringe_strips_the_key_rim_and_keeps_the_inside():
    from PIL import Image
    from bgate_core.three_d.cutoutkit import defringe
    part = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    for x in range(8, 32):
        for y in range(8, 32):
            edge = x in (8, 31) or y in (8, 31) or x in (9, 30) or y in (9, 30)
            part.putpixel((x, y), (60, 200, 210, 255) if edge else (180, 90, 60, 255))
    out = defringe(part, (0, 255, 255))
    rim = [out.getpixel((9, y)) for y in range(10, 30)]
    assert all(p[3] for p in rim)
    assert all(p[1] - p[0] < 20 and p[2] - p[0] < 20 for p in rim)
    assert out.getpixel((20, 20)) == (180, 90, 60, 255)
    assert out.getpixel((8, 20))[3] == 0                 # eroded one px
