"""The rig editor's API: list, load a rig with its clips baked, save edits.

The editor exists because a generated rig lands a few pixels off and a person
dragging the joint is the cheap, certain fix (2026-09-22). What matters here is
that a save lands in the .cutout.json the emitter reads, that a moved pivot is
recorded as authored, and that a far piece follows the near piece it reuses.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bgate_core.three_d import cutout
from bgate_ui.app import app

REL = "game/assets/characters/hero/hero.cutout.json"


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def rig(root):
    (root / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="Test"\n', encoding="utf-8")
    home = root / "game" / "assets" / "characters" / "hero"
    parts = home / "parts"
    parts.mkdir(parents=True)
    skin = {}
    for slot in ("head", "torso", "arm_near", "thigh_near"):
        png = parts / f"{slot}.png"
        Image.new("RGBA", (20, 40), (200, 120, 90, 255)).save(png)
        skin[slot] = {"texture": str(png)}
    skin["arm_far"] = {"texture": skin["arm_near"]["texture"], "reuse_of": "arm_near",
                       "far_tint": [0.7, 0.7, 0.7, 1.0]}
    doc = cutout.empty("hero")
    doc["skin"] = skin
    cutout.save(home / "hero.cutout.json", doc)
    return home


def test_rigs_are_listed_and_one_loads_with_every_clip_baked(client, rig):
    got = client.get("/api/cutout/rigs").json()
    assert [r["rel"] for r in got["rigs"]] == [REL]
    data = client.get("/api/cutout/rig", params={"rel": REL}).json()
    assert data["ok"] and data["name"] == "hero"
    assert set(data["clips"]) == set(cutout.clip_names())
    assert "arm_near" in data["clips"]["aim"]["tracks"]
    assert data["parts"]["arm_far"]["reuse_of"] == "arm_near"
    img = client.get(data["parts"]["head"]["url"])
    assert img.status_code == 200


def test_a_save_writes_the_rig_and_re_emits_it(client, rig):
    body = {"rel": REL,
            "adjustments": {"arm_near": {"pos": [0.0, -3.0], "rot": 4.0}},
            "pieces": {"arm_near": {"pivot": [0.4, 0.8], "rot_offset": 5.0, "scale": 1.1}}}
    got = client.post("/api/cutout/save", json=body).json()
    assert got["ok"] and got["emitted"]
    doc = cutout.load(rig / "hero.cutout.json")
    assert doc["adjustments"]["arm_near"] == {"pos": [0.0, -3.0], "rot": 4.0}
    near = doc["skin"]["arm_near"]
    assert near["pivot"] == [0.4, 0.8] and near["pivot_source"] == "authored"
    assert near["rot_offset"] == 5.0 and near["scale"] == 1.1 and near["fit"] is False
    # The far piece reuses the near drawing, so it follows the near edit.
    assert doc["skin"]["arm_far"]["pivot"] == [0.4, 0.8]
    assert (rig / "hero.tscn").is_file()
    stamp = json.loads((rig / "hero.tscn.emit.json").read_text(encoding="utf-8"))
    assert stamp["name"] == "hero"


def test_a_path_outside_the_project_is_refused(client, rig):
    r = client.get("/api/cutout/rig", params={"rel": "../../etc/x.cutout.json"})
    assert r.status_code in (403, 404)


def test_a_posed_key_round_trips_through_the_rig_into_the_bake(client, rig):
    before = client.get("/api/cutout/rig", params={"rel": REL}).json()
    # Key the head at t=0.5 in idle to 0.4 rad (Godot, clockwise).
    body = {"rel": REL, "clips": {"idle": {"head": {"rot": [[0.0, 0.0], [0.5, 0.4]]}}}}
    got = client.post("/api/cutout/save", json=body).json()
    assert got["ok"] and got["overrides"] == {"idle": ["head"]}
    keys = got["clips"]["idle"]["tracks"]["head"]["rot"]
    # Played exactly as keyed: no follow-through lag on a hand-keyed bone.
    assert [k[0] for k in keys] == [0.0, 0.5]
    assert keys[1][1] == pytest.approx(0.4, abs=1e-4)
    doc = cutout.load(rig / "hero.cutout.json")
    assert doc["clip_overrides"]["idle"]["head"]["rot"][1][0] == 0.5
    # Other clips and other bones still play the library.
    assert got["clips"]["walk"] == before["clips"]["walk"]
    # Reset puts the library back.
    got = client.post("/api/cutout/save", json={"rel": REL, "clip_reset": {"idle": ["head"]}}).json()
    assert got["overrides"] == {}
    assert got["clips"]["idle"] == before["clips"]["idle"]


def test_a_layer_edit_reorders_the_slot(client, rig):
    got = client.post("/api/cutout/save", json={"rel": REL, "pieces": {"head": {"z": 1}}}).json()
    assert got["parts"]["head"]["z"] == 1
    doc = cutout.load(rig / "hero.cutout.json")
    assert next(s for s in doc["slots"] if s["name"] == "head")["z"] == 1
