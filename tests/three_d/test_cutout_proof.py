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
