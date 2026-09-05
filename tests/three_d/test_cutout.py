"""The cutout rig: the document, the emitter, and the engine's verdict.

THE RULE THIS FILE FOLLOWS is the one test_blender_gates.py set: every test
either makes a refusal FIRE or measures something that came out of a real tool.
Two of them exist because of bugs found while building this:

  * ``test_zero_keys_are_floats_not_ints`` — "%g" renders 0.0 as `0`, Godot's
    .tres parser reads that as an INT, and a value track with mixed int/float
    keys plays, advances, fires its method keys and moves nothing at all.

  * ``test_emitted_rig_actually_animates_in_godot`` — the same failure, caught
    the only way it can be caught: by making the engine play the clip and
    reading the bone afterwards. Importing the scene proves nothing; a rig with
    every part at the origin imports perfectly.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bgate_adapters import godot
from bgate_core.three_d import cutout, cutoutwire
from bgate_core.three_d.cutout import CutoutError

needs_godot = pytest.mark.skipif(
    not godot.available()["available"], reason="Godot not installed")

SIZES = {
    "head": (44, 46), "torso": (46, 62), "hip": (40, 24),
    "arm_near": (18, 34), "forearm_near": (16, 32),
    "arm_far": (18, 34), "forearm_far": (16, 32),
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


# ---------------------------------------------------------------------------
# The document refuses what would break silently
# ---------------------------------------------------------------------------

def test_a_fresh_document_is_valid_and_complete_in_shape():
    d = cutout.empty("hero")
    assert cutout.normalise(d) == d
    assert len(d["bones"]) == 13
    assert d["skin"] == {}


def test_a_cycle_is_refused():
    d = cutout.empty("hero")
    by = {b["name"]: b for b in d["bones"]}
    by["hips"]["parent"] = "chest"
    with pytest.raises(CutoutError) as exc:
        cutout.normalise(d)
    assert "cycle" in str(exc.value)


def test_a_slot_on_a_missing_bone_is_refused():
    d = cutout.empty("hero")
    d["slots"][0]["bone"] = "tentacle"
    with pytest.raises(CutoutError):
        cutout.normalise(d)


def test_duplicate_bone_names_are_refused():
    d = cutout.empty("hero")
    d["bones"][3]["name"] = "chest"
    with pytest.raises(CutoutError) as exc:
        cutout.normalise(d)
    assert "duplicate" in str(exc.value)


def test_bone_paths_are_the_hierarchy():
    d = cutout.empty("hero")
    assert cutout.bone_node_path(d, "head") == "Visual/hips/chest/head"
    assert (cutout.bone_node_path(d, "foot_near")
            == "Visual/hips/thigh_near/shin_near/foot_near")


def test_adjustments_move_the_rest_pose_and_survive_baking():
    d = cutout.empty("hero")
    d["adjustments"] = {"arm_near": {"rot": -12.0, "pos": [0, -3]}}
    d = cutout.normalise(d)
    rest = cutout.rest_pose(d)
    assert rest["arm_near"]["rot"] == pytest.approx(8.0 - 12.0)
    assert rest["arm_near"]["pos"][1] == 22 - 3
    # THE CLAIM THAT MATTERS: frame one of a clip does not erase it.
    baked = cutoutwire.bake_clip(d, "walk")
    track = next(t for t in baked["tracks"]
                 if t["path"].endswith("arm_near:rotation"))
    first = track["keys"][0][1]
    # walk's first arm_near delta is -18 degrees on top of the adjusted -4.
    assert first == pytest.approx(cutoutwire.to_godot_rot(-4.0 - 18.0))


def test_status_lists_what_is_missing_rather_than_refusing(project, doc):
    got = cutout.status(doc, root=project)
    assert got["complete"] is False           # hat and weapon are unfilled
    assert set(got["missing"]) == {"hat", "weapon"}
    assert got["problems"] == []


def test_status_flags_a_pivot_authored_against_a_different_drawing(project, doc):
    from PIL import Image

    doc["skin"]["head"]["pivot_source"] = "authored"
    part = Path(doc["skin"]["head"]["texture"])
    Image.new("RGBA", (44, 46), (10, 10, 10, 255)).save(part)   # regenerate it
    got = cutout.status(doc, root=project)
    kinds = {p["kind"] for p in got["problems"]}
    assert "stale_pivot" in kinds


def test_status_flags_a_rig_that_hovers():
    d = cutout.empty("hero")
    d["adjustments"] = {"hips": {"pos": [0, 60]}}
    got = cutout.status(cutout.normalise(d))
    assert any(p["kind"] == "origin" for p in got["problems"])


# ---------------------------------------------------------------------------
# The emitter
# ---------------------------------------------------------------------------

def test_zero_keys_are_floats_not_ints(project, doc):
    """THE BUG. A value track with an int key does not apply, and says nothing."""
    text = cutoutwire.library_text(doc, ["walk"])
    values = [line for line in text.splitlines() if line.startswith('"values"')]
    assert values
    for line in values:
        for number in line.split("[", 1)[1].rstrip("]").split(","):
            number = number.strip()
            if not number or number.startswith("Vector2"):
                continue
            assert "." in number or "e" in number, f"integer key in {line}"


def test_every_sprite_pins_absolute_z(project, doc):
    text = cutoutwire.scene_text(doc, project_dir=project,
                                 library_res="res://x.tres",
                                 script_res="res://y.gd", sizes=SIZES)
    sprites = text.count('type="Sprite2D"')
    assert sprites == 13
    assert text.count("z_as_relative = false") == sprites


def test_looping_clips_have_no_key_on_the_length(project, doc):
    for name in ("idle", "walk", "run"):
        baked = cutoutwire.bake_clip(doc, name)
        assert baked["loop_mode"] == 1
        for track in baked["tracks"]:
            assert track["keys"][-1][0] < baked["length"] - 1e-9


def test_the_no_loop_clips_do_not_loop(project, doc):
    for name in cutout.NO_LOOP:
        assert cutoutwire.bake_clip(doc, name)["loop_mode"] == 0


def test_emit_is_byte_identical_for_an_unchanged_document(project, doc):
    scene = project / "game" / "hero.tscn"
    first = cutoutwire.emit(doc, project_dir=project, scene_path=scene,
                            sizes=SIZES)
    assert first["ok"] is True
    body = scene.read_text(encoding="utf-8")
    again = cutoutwire.emit(doc, project_dir=project, scene_path=scene,
                            sizes=SIZES)
    assert again["ok"] is True
    assert scene.read_text(encoding="utf-8") == body


def test_emit_refuses_to_clobber_a_hand_edited_scene(project, doc):
    scene = project / "game" / "hero.tscn"
    cutoutwire.emit(doc, project_dir=project, scene_path=scene, sizes=SIZES)
    scene.write_text(scene.read_text(encoding="utf-8") + "\n; a human was here\n",
                     encoding="utf-8")
    refused = cutoutwire.emit(doc, project_dir=project, scene_path=scene,
                              sizes=SIZES)
    assert refused["ok"] is False
    assert refused["refused"][0]["kind"] == "scene"
    forced = cutoutwire.emit(doc, project_dir=project, scene_path=scene,
                             sizes=SIZES, force=True)
    assert forced["ok"] is True
    assert "a human was here" not in scene.read_text(encoding="utf-8")


def test_unfilled_slots_emit_no_node(project, doc):
    got = cutoutwire.emit(doc, project_dir=project,
                          scene_path=project / "game" / "hero.tscn", sizes=SIZES)
    assert set(got["unfilled"]) == {"hat", "weapon"}
    body = (project / "game" / "hero.tscn").read_text(encoding="utf-8")
    assert 'name="hat"' not in body


# ---------------------------------------------------------------------------
# The engine's verdict — the only test that can catch an inert clip
# ---------------------------------------------------------------------------

PROBE = r'''extends SceneTree
var rig
var player: AnimationPlayer
var thigh: Node2D
var rest := 0.0
var events := []
var stage := 0
var out := {}

func _initialize() -> void:
    var packed = ResourceLoader.load("__SCENE__")
    if packed == null:
        out["error"] = "scene did not load"
        _say()
        return
    rig = packed.instantiate()
    get_root().add_child(rig)
    player = rig.get_node_or_null("AnimationPlayer")
    thigh = rig.get_node_or_null("Visual/hips/thigh_near")
    out["has_player"] = player != null
    out["has_bone"] = thigh != null
    if player == null or thigh == null:
        _say()
        return
    var leaked := []
    for n in rig.find_children("*", "Sprite2D", true, false):
        if n.z_as_relative:
            leaked.append(String(n.name))
    out["z_leaks"] = leaked
    out["sprites"] = rig.find_children("*", "Sprite2D", true, false).size()
    out["clips"] = player.get_animation_list()
    rig.anim_event.connect(func(n): events.append(String(n)))
    rest = thigh.rotation

func _process(_d) -> bool:
    if out.has("error") or player == null:
        return true
    if stage == 0:
        player.play("walk")
        player.advance(0.05)
        out["walk_moves_bone"] = absf(thigh.rotation - rest) > 0.01
        out["walk_rot"] = snappedf(thigh.rotation, 0.0001)
        player.stop()
        thigh.rotation = rest
        player.play("attack_melee")
        player.advance(0.3)
        stage += 1
        return false
    if stage == 1:
        out["events"] = events
        rig.seek_quiet(0.0)
        rig.seek_quiet(0.3)
        stage += 1
        return false
    out["events_after_quiet_seek"] = events.size()
    out["ok"] = true
    _say()
    return true

func _say() -> void:
    print("BGATE_CUTOUT:" + JSON.stringify(out))
    quit()
'''


@needs_godot
def test_emitted_rig_actually_animates_in_godot(project, doc):
    """Import proves nothing: a rig with every part at the origin imports fine."""
    scene = project / "game" / "assets" / "characters" / "hero" / "hero.tscn"
    got = cutoutwire.emit(doc, project_dir=project, scene_path=scene,
                          script_res="res://addons/bgate/cutout_rig.gd",
                          sizes=SIZES)
    assert got["ok"] is True, got

    # IMPORT FIRST, AND THE FAILURE WITHOUT IT IS INSTRUCTIVE: a project with no
    # .godot cache has no .import sidecar for the part PNGs, so every
    # Texture2D ext_resource in the scene fails to resolve and Godot drops the
    # nodes that referenced them. The scene still loads. The AnimationPlayer is
    # still there. Every bone is gone, which reads exactly like an emitter that
    # wrote no bones.
    imported = godot.check_project(str(project), timeout=240)
    assert imported.get("ok") is True, imported

    run = godot.run_script(PROBE.replace("__SCENE__", got["scene_res"]),
                           project_dir=str(project), timeout=240)
    report = {}
    for line in (run.get("stdout") or "").splitlines():
        if line.startswith("BGATE_CUTOUT:"):
            report = json.loads(line[len("BGATE_CUTOUT:"):])
            break
    assert report, run.get("stdout", "")[-2000:]
    assert report.get("ok") is True, report
    assert report["sprites"] == 13
    assert report["z_leaks"] == []
    assert sorted(report["clips"]) == sorted(cutout.clip_names())
    # THE ONE THAT MATTERS: the clip drives the skeleton.
    assert report["walk_moves_bone"] is True, report
    # Events fire, and a rewind does not fire them again.
    assert report["events"] == ["hit"], report
    assert report["events_after_quiet_seek"] == 1, report
