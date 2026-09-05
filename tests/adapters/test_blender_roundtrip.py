"""What the glTF round trip costs a rigged layer, and what puts it back.

THE DEFECT THIS FILE WAS WRITTEN AGAINST. A layer is modelled once, exported to
.glb, and re-imported by every later step — apply_texture, combine, turnaround.
Two things survive that trip badly, and both were measured on this machine
against Blender 4.5 and the 940-vertex ``bg_human`` base with its 22 deform
bones:

  * THE IMPORTER'S OWN JUNK. io_scene_gltf2 builds a 42-vertex "Icosphere" bone
    custom shape at the world origin for any .glb carrying an armature, and
    links it in PARENTLESS. Measured, weighting the layer WITH it: heat left
    242 vertices loose, envelope left 42 — the Icosphere's own, every one of
    them. So the layer stepped all the way down to ``deform:nearest``, which is
    rigid. Without it: heat 200, envelope 0, i.e. ``deform:envelope``.

  * THE BONE TAILS. glTF stores joint POSITIONS and not bone DIRECTIONS, so the
    importer guesses. Measured on the same base: 0 of 23 bone heads moved and 7
    of 23 tails did (9 by exact compare, two of them float32 export noise), and
    bone heat then left 200 of 940 vertices unweighted
    where the authoring scene left 0. All 200 sat between z=1.546 and z=1.800 —
    the skull cap, above the truncated tail of the Head bone, which is a LEAF
    and therefore the one kind of bone the importer has nothing to guess from.

Both numbers are asserted here rather than remembered, in both directions: the
control tests make the damage REPRODUCE, and the repair tests demand exactly
zero. Real Blender throughout — every claim in this file is a claim about what
io_scene_gltf2 does, and a mock would agree with any of it being wrong.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from bgate_adapters import blender

# slow as well as skipif: every test here drives a real Blender, which is what
# pyproject's `slow` marker is defined to mean. See test_blender.py's docstring.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not blender.available()["available"],
                       reason="Blender not installed"),
]

needs_pil = pytest.mark.skipif(
    importlib.util.find_spec("PIL") is None, reason="Pillow not installed")


# The base mesh library's own humanoid: a closed, unwrapped, weight-ready body
# and a 23-bone skeleton, 22 of them deforming. Built through the library and
# not by hand so that the numbers in this file mean something about the asset
# the pipeline actually produces.
BODY = """
bg_wipe()
base = bg_human(detail=1)
"""

# No armature, one material, no image — the layer that should be NAMED by the
# per-layer untextured check while a textured sibling is not.
CAP = """
bg_wipe()
obj = bg_box("Cap", size=(0.3, 0.3, 0.12), at=(0, 0, 1.7))
bg_finish(obj, colour=(0.2, 0.2, 0.6), material="cap_mat")
"""


@pytest.fixture(scope="module")
def rigged(tmp_path_factory) -> Path:
    """One real rigged layer, exported the way the modelling step exports it.

    Module-scoped: this is a Blender launch, and every test below re-imports
    the same file rather than paying for it again.
    """
    out = tmp_path_factory.mktemp("rigged")
    glb = out / "body.glb"
    got = blender.run_script(BODY, export_glb=str(glb), out_dir=str(out),
                             timeout=900)
    assert got["ok"] is True, got.get("error")
    assert glb.is_file()
    return glb


@pytest.fixture(scope="module")
def bare(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("bare")
    glb = out / "cap.glb"
    got = blender.run_script(CAP, export_glb=str(glb), out_dir=str(out),
                             timeout=900)
    assert got["ok"] is True, got.get("error")
    return glb


def _one_layer(glb: Path, out: Path) -> dict:
    """combine() over a single deforming layer that is also the rig."""
    return blender.combine(
        [{"path": str(glb), "name": "body", "bind": "deform"}],
        out, rig="body", timeout=900)


def _layer(result: dict, name: str) -> dict:
    for part in result.get("parts") or []:
        if part["name"] == name:
            return part
    raise AssertionError(f"no layer {name!r} in {[p['name'] for p in result['parts']]}")


def _checks(result: dict, kind: str) -> list[dict]:
    return [c for c in result.get("checks") or [] if c.get("check") == kind]


def _no_root(monkeypatch) -> None:
    """A developer with BGATE_ROOT exported must not silently pass the
    containment tests — the note is about THIS output, not their shell."""
    monkeypatch.delenv("BGATE_ROOT", raising=False)


# ---------------------------------------------------------------------------
# The importer's bone custom shape
# ---------------------------------------------------------------------------
class TestBoneCustomShape:
    """``turnaround`` has filtered this since the day it was measured. The
    import inside ``combine`` had not, and combine is where it does damage:
    turnaround only renders a grey ball, combine WEIGHTS one."""

    def test_the_icosphere_is_not_one_of_the_layers_objects(self, rigged, tmp_path):
        got = _one_layer(rigged, tmp_path / "hero.glb")

        objects = _layer(got, "body")["objects"]
        assert "Icosphere" not in objects, objects
        # Not by name alone: any parentless 42-vertex stray is the same bug
        # wearing whatever name the importer felt like using.
        assert objects == ["Body", "BodySkeleton"], objects

    def test_dropping_it_is_reported_rather_than_done_silently(self, rigged, tmp_path):
        """A layer quietly losing an object it did not author is still a layer
        losing an object. The note is what makes the deletion auditable."""
        got = _one_layer(rigged, tmp_path / "hero.glb")

        dropped = [n for n in got["notes"] if "custom shape" in n["note"]]
        assert dropped, got["notes"]
        assert dropped[0]["layer"] == "body"
        assert "Icosphere" in dropped[0]["note"]

    def test_the_icosphere_is_what_forced_the_layer_down_to_nearest(
            self, rigged, tmp_path):
        """THE CONTROL. Measured on this machine: weighting Body+Icosphere left
        242 loose under heat and 42 under envelope — so the ladder ran out and
        the whole body settled on rigid nearest-bone weights. The 42 are the
        Icosphere's own vertices, which is the entire point: one object nobody
        authored costs every other object in the layer its deformation.

        Asserted as a live measurement, not a remembered one, because the
        moment this stops reproducing the filter above is dead code.
        """
        got = blender.run_script(_MECHANISM.replace("__GLB__", str(rigged)),
                                 out_dir=str(tmp_path), timeout=900)
        assert got["ok"] is True, got.get("error")
        report = _marked(got)

        assert report["with_icosphere"]["envelope"] > 0, report
        assert report["with_icosphere_settles"] == "deform:nearest", report
        assert report["without_icosphere"]["envelope"] == 0, report
        assert report["without_icosphere_settles"] == "deform:envelope", report


# Run inside Blender by the control tests: import the layer raw, with NO repair
# of any kind, and walk the same heat -> envelope ladder combine() walks.
_MECHANISM = r'''
import bpy, json
bg_wipe()
bpy.ops.import_scene.gltf(filepath=r"__GLB__")
rig = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"][0]
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
bones = {b.name for b in rig.data.bones if b.use_deform}
report = {"meshes": {o.name: len(o.data.vertices) for o in meshes}}


def unweighted(group):
    total = 0
    for mesh in group:
        groups = mesh.vertex_groups
        for vert in mesh.data.vertices:
            if not any(g.weight > 0 and groups[g.group].name in bones
                       for g in vert.groups):
                total += 1
    return total


def unparent(group):
    for mesh in group:
        mesh.parent = None
        for mod in [m for m in mesh.modifiers if m.type == "ARMATURE"]:
            mesh.modifiers.remove(mod)
        mesh.vertex_groups.clear()


def ladder(group, label):
    out = {}
    for kind, name in (("ARMATURE_AUTO", "heat"), ("ARMATURE_ENVELOPE", "envelope")):
        unparent(group)
        bg_deselect()
        for mesh in group:
            mesh.select_set(True)
        rig.select_set(True)
        bpy.context.view_layer.objects.active = rig
        bpy.ops.object.parent_set(type=kind)
        out[name] = unweighted(group)
    unparent(group)
    report[label] = out
    report[label + "_settles"] = ("deform:heat" if not out["heat"] else
                                  "deform:envelope" if not out["envelope"] else
                                  "deform:nearest")


body = max(meshes, key=lambda o: len(o.data.vertices))
for mesh in meshes:
    bg_clean(mesh)
report["body_verts"] = len(body.data.vertices)
ladder(meshes, "with_icosphere")
ladder([body], "without_icosphere")
print("RESULT " + json.dumps(report))
'''


def _marked(got: dict) -> dict:
    lines = [l for l in (got.get("print") or "").splitlines()
             if l.startswith("RESULT ")]
    assert lines, (got.get("print") or "")[-2000:]
    return json.loads(lines[-1][len("RESULT "):])


# ---------------------------------------------------------------------------
# The bone tails glTF cannot carry
# ---------------------------------------------------------------------------
class TestRoundTripWeighting:

    def test_the_export_writes_the_rest_pose_down_beside_the_file(self, rigged):
        """The record is the whole fix. Without it the tails are gone the
        instant the exporter runs, and no later step can tell."""
        record = blender.read_layer_record(rigged)

        armatures = record.get("armatures") or {}
        assert list(armatures) == ["BodySkeleton"], armatures
        bones = armatures["BodySkeleton"]["bones"]
        assert len(bones) == 23, sorted(bones)
        assert set(bones["Head"]) >= {"head", "tail", "roll", "deform"}
        # The flag the importer also loses: 22 of the 23 deform, Root does not.
        assert sum(1 for spec in bones.values() if spec["deform"]) == 22

    def test_the_importer_truncates_the_leaf_bones_that_carried_the_skull(
            self, rigged, tmp_path):
        """THE CONTROL for the tails. Measured: 0 of 23 heads moved and 7 of 23
        tails did, and the ones that matter are the LEAVES — the importer has
        no child bone to read a direction off, so it invents a stub."""
        got = blender.run_script(_TAILS.replace("__GLB__", str(rigged)),
                                 out_dir=str(tmp_path), timeout=900)
        assert got["ok"] is True, got.get("error")
        report = _marked(got)

        assert report["heads_moved"] == 0, report["moved_names"]
        assert report["tails_moved"] > 0, report
        assert "Head" in report["moved_names"], report["moved_names"]

    def test_an_unrepaired_import_leaves_vertices_at_the_rest_pose(
            self, rigged, tmp_path):
        """MEASURED: 200 of 940, all of them above z=1.546 — the skull cap.
        Bounded rather than pinned to 200 because the exact figure belongs to
        one Blender version, but it is asserted to be a large minority of the
        mesh: a handful of loose vertices would be a different bug."""
        got = blender.run_script(_MECHANISM.replace("__GLB__", str(rigged)),
                                 out_dir=str(tmp_path), timeout=900)
        assert got["ok"] is True, got.get("error")
        report = _marked(got)

        loose = report["without_icosphere"]["heat"]
        assert loose >= 100, report
        assert loose < report["body_verts"], report

    def test_the_record_puts_the_tails_back_and_nothing_is_left_loose(
            self, rigged, tmp_path):
        """THE FIX, measured end to end: 200 of 940 unweighted becomes 0, and
        the layer binds with bone heat instead of settling for rigid."""
        got = _one_layer(rigged, tmp_path / "hero.glb")

        assert _layer(got, "body")["bound"] == "deform:heat", got["notes"]
        assert _checks(got, "unweighted_verts") == [], got["checks"]
        restored = [n for n in got["notes"] if "restored" in n["note"]]
        assert restored, got["notes"]

    def test_without_a_record_the_leaf_repair_still_binds_every_vertex(
            self, rigged, tmp_path):
        """A .glb from somewhere else has no sidecar, and that is most of them.

        Measured with the record hidden: growing each LEAF bone until it spans
        the geometry standing around its axis took the same layer from 200 of
        940 loose to 0, and did it on the chibi and dense A-pose bases too.
        """
        copied = tmp_path / "orphan.glb"
        shutil.copyfile(rigged, copied)
        assert not blender.layer_record_path(copied).exists()

        got = _one_layer(copied, tmp_path / "hero.glb")

        assert _layer(got, "body")["bound"] == "deform:heat", got["notes"]
        assert _checks(got, "unweighted_verts") == [], got["checks"]
        grown = [n for n in got["notes"] if "grew leaf bone" in n["note"]]
        assert grown, got["notes"]
        assert "Head" in grown[0]["note"], grown

    @needs_pil
    def test_a_texture_pass_hands_the_rig_on_instead_of_baking_the_guess(
            self, rigged, tmp_path):
        """apply_texture re-EXPORTS. Left alone it would write the importer's
        guessed tails out as if they were authored, and its own record would
        then agree with the guess — so combine would restore the damage."""
        from PIL import Image
        png = tmp_path / "skin.png"
        Image.new("RGB", (16, 16), (180, 120, 90)).save(png)
        out = tmp_path / "body_tex.glb"

        tex = blender.apply_texture(rigged, png, out, material="skin",
                                    timeout=900)
        assert tex["ok"] is True, tex.get("error")
        assert tex["dropped_shapes"] == ["Icosphere"], tex["dropped_shapes"]
        assert tex["rig_restored"].get("BodySkeleton", 0) > 0, tex["rig_restored"]

        before = blender.read_layer_record(rigged)["armatures"]["BodySkeleton"]
        after = blender.read_layer_record(out)["armatures"]["BodySkeleton"]
        assert sorted(after["bones"]) == sorted(before["bones"])
        assert sum(1 for s in after["bones"].values() if s["deform"]) == 22

        # And the textured layer still weights, which is what all of it is for.
        got = blender.combine(
            [{"path": str(out), "name": "body", "bind": "deform"}],
            tmp_path / "hero.glb", rig="body", timeout=900)
        assert _layer(got, "body")["bound"] == "deform:heat", got["notes"]
        assert _checks(got, "unweighted_verts") == [], got["checks"]


# Run inside Blender: what the round trip did to the rig, head by head and
# tail by tail, against the record the exporting run wrote.
_TAILS = r'''
import bpy, json
from mathutils import Vector

bg_wipe()
bpy.ops.import_scene.gltf(filepath=r"__GLB__")
rig = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"][0]
record = json.load(open(r"__GLB__" + ".bgate.json", encoding="utf-8"))
authored = record["armatures"][rig.name]["bones"]

heads = tails = 0
moved = []
for bone in rig.data.bones:
    spec = authored.get(bone.name)
    if spec is None:
        continue
    if (Vector(spec["head"]) - bone.head_local).length > 1e-5:
        heads += 1
    if (Vector(spec["tail"]) - bone.tail_local).length > 1e-5:
        tails += 1
        moved.append(bone.name)
print("RESULT " + json.dumps(
    {"heads_moved": heads, "tails_moved": tails, "moved_names": sorted(moved),
     "bones": len(rig.data.bones)}))
'''


# ---------------------------------------------------------------------------
# Which layer is the untextured one
# ---------------------------------------------------------------------------
class TestPerLayerUntextured:
    """``no_textures`` says the asset carries no image. On a six-layer
    character that names none of the six, and an agent told the asset is
    untextured re-runs all of them or guesses. This names one."""

    @needs_pil
    def test_the_check_names_the_bare_layer_and_spares_the_textured_one(
            self, rigged, bare, tmp_path):
        from PIL import Image
        png = tmp_path / "skin.png"
        Image.new("RGB", (16, 16), (180, 120, 90)).save(png)
        textured = tmp_path / "body_tex.glb"
        assert blender.apply_texture(rigged, png, textured, material="skin",
                                     timeout=900)["ok"] is True

        got = blender.combine(
            [{"path": str(textured), "name": "body", "bind": "deform"},
             {"path": str(bare), "name": "cap", "bind": "bone:Head"}],
            tmp_path / "hero.glb", rig="body", timeout=900)

        named = _checks(got, "untextured")
        assert [c["layer"] for c in named] == ["cap"], got["checks"]
        assert named[0]["materials"] == ["cap_mat"], named
        assert "cap" in named[0]["fix"]

    def test_it_names_a_rigged_layer_too_and_says_which_material(
            self, rigged, tmp_path):
        """The base mesh carries one authored material ("skin") and no image,
        which is exactly the flat-colour surface the check exists to catch —
        and naming the MATERIAL is what tells an agent which slot to texture."""
        got = _one_layer(rigged, tmp_path / "hero.glb")

        named = _checks(got, "untextured")
        assert [c["layer"] for c in named] == ["body"], got["checks"]
        assert named[0]["materials"] == ["skin"], named


# ---------------------------------------------------------------------------
# Outputs the ledger will never see
# ---------------------------------------------------------------------------
class TestArtifactNote:
    """An asset written outside the project cannot be registered, so no
    reviewer is ever shown it. turnaround's caller has said so since it was
    measured; combine and apply_texture returned a cheerful ok=True."""

    def test_combine_inside_the_project_says_nothing(self, rigged, root,
                                                     monkeypatch):
        """The note must be about containment and not about being noisy."""
        _no_root(monkeypatch)
        out = root / "out" / "hero.glb"
        out.parent.mkdir(parents=True, exist_ok=True)

        got = _one_layer(rigged, out)

        assert "artifact_note" not in got, got.get("artifact_note")

    @needs_pil
    def test_apply_texture_carries_the_same_sentence(self, rigged, tmp_path,
                                                     monkeypatch):
        from PIL import Image
        _no_root(monkeypatch)
        png = tmp_path / "skin.png"
        Image.new("RGB", (16, 16), (180, 120, 90)).save(png)

        got = blender.apply_texture(rigged, png, tmp_path / "body_tex.glb",
                                    material="skin", timeout=900)

        assert got["artifact_note"] == blender.ARTIFACT_NOTE
        assert "outside the project root" in got["artifact_note"]

    def test_the_note_is_decided_by_containment_not_by_the_shell(
            self, tmp_path, root, monkeypatch):
        """BGATE_ROOT is how the server is pointed at a project, so it decides
        this too — and a file under it is on the ledger wherever it sits."""
        monkeypatch.setenv("BGATE_ROOT", str(root))
        assert blender._artifact_note(root / "out" / "hero.glb") == ""

        monkeypatch.setenv("BGATE_ROOT", str(tmp_path / "elsewhere"))
        assert blender._artifact_note(root / "out" / "hero.glb") == \
            blender.ARTIFACT_NOTE
