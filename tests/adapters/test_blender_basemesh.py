"""The base mesh library, exercised against REAL Blender.

WHY THESE RUN IN BLENDER. The library is a string of bpy source injected into
an agent's script, so there is nowhere else it can run. More to the point,
every property worth asserting here is a property of Blender's behaviour and
not of ours: whether bone-heat weighting accepts the mesh, whether a bone
survives leaving edit mode with the roll it was given, whether a lofted shell
comes back manifold. A mocked bpy would agree with any of those being wrong.

WHAT THIS FILE IS DEFENDING. The 3D path could previously reach "a pile of
correctly-named primitives" and no further, and the measured failure it kept
producing was not ugliness — it was a body that would not weight, a layer
authored at a height somebody guessed, and an asset whose scale nothing in the
pipeline had ever declared. So the assertions are: the proportions are the ones
asked for, the mesh is clean enough to weight, the weighting actually binds
every vertex, the landmarks are where they say they are, and one metre means
one metre.
"""
from __future__ import annotations

import json

import pytest

from bgate_adapters import _blender_base as base
from bgate_adapters import _blender_kit as kit
from bgate_adapters import blender

_no_blender = pytest.mark.skipif(
    not blender.available()["available"], reason="Blender not installed"
)


def requires_blender(obj):
    """SLOW as well as skipped-when-missing. See test_blender.py's docstring.

    Composed rather than set as a module-level ``pytestmark`` because a handful
    of tests here assert on the generated script without ever running it, and
    marking those slow would take real coverage out of the default run to no
    purpose.
    """
    return pytest.mark.slow(_no_blender(obj))


def _result(tmp_path, script):
    """Run a script in Blender and read back the one line it marked.

    bpy state does not survive the subprocess. Only what the script chose to
    print is evidence; anything else is a guess about what happened in a
    process that has already exited.
    """
    got = blender.run_script(script, out_dir=str(tmp_path), timeout=900)
    assert got["ok"] is True, got.get("error", "") + (got.get("traceback") or "")
    marked = [ln for ln in got["print"].splitlines() if ln.startswith("RESULT ")]
    assert marked, got["print"][-3000:]
    return json.loads(marked[-1][len("RESULT "):])


def _fails(tmp_path, script):
    got = blender.run_script(script, out_dir=str(tmp_path), timeout=900)
    assert got["ok"] is False, "expected the library to refuse this"
    return got["error"] + "\n" + (got.get("traceback") or "")


# ---------------------------------------------------------------------------
# The library as source — no Blender needed
# ---------------------------------------------------------------------------

class TestSource:
    def test_the_library_is_valid_python(self):
        compile(base.BASE, "<BASE>", "exec")

    def test_the_worked_base_script_is_valid_python(self):
        # It is a REFERENCE SCRIPT, not prose. If it stops compiling it has
        # stopped being worth reading, and it is the only end-to-end example of
        # authoring a layer ONTO a body rather than beside one.
        compile(base.BASE_EXAMPLE, "<BASE_EXAMPLE>", "exec")

    def test_the_library_ships_inside_the_kit(self):
        # The agent that needs this is already inside Blender with one script in
        # front of it. A library it has to import is a library it does not have.
        assert "def bg_human(" in kit.KIT
        assert "def bg_proportions(" in kit.KIT
        assert "def bg_unit_check(" in kit.KIT
        assert base.BASE_EXAMPLE in kit.KIT
        compile(kit.KIT, "<KIT>", "exec")

    def test_the_library_comes_after_the_helpers_it_calls(self):
        # bg_human calls bg_join, bg_finish and bg_bone_chain at RUN time, so
        # ordering is not strictly required — but a reader who finds the base
        # library above the primitives it is built on will assume otherwise.
        assert kit.KIT.index("def bg_clean(") < kit.KIT.index("def bg_human(")
        assert kit.KIT.index("def bg_bone_chain(") < kit.KIT.index("def bg_human(")


# ---------------------------------------------------------------------------
# Proportions
# ---------------------------------------------------------------------------

_PROPORTIONS = r"""
import json
out = []
for case in [{"height": 1.8, "heads": 7.5}, {"height": 1.8, "heads": 8.0},
             {"height": 1.6, "heads": 5.0}, {"height": 1.0, "heads": 3.0},
             {"height": 2.4, "heads": 7.5, "build": 1.6},
             {"height": 1.8, "heads": 7.5, "limbs": 1.3},
             {"height": 1.8, "heads": 7.5, "shoulders": 1.4}]:
    bg_wipe()
    made = bg_human(**case)
    box = bg_bounds(made["obj"])
    P = made["props"]
    out.append({"case": case, "height": box["dims"][2], "span": box["dims"][0],
                "sole": box["min"][2], "head": P["head"],
                "chin": P["chin"], "crotch": P["crotch_z"],
                "shoulder_hw": P["shoulder_hw"]})
print("RESULT " + json.dumps(out))
"""


@requires_blender
class TestProportions:
    """A CHARACTER IS ONE MEASUREMENT AND A HEAD COUNT. Everything else is
    derived, and these are the derivations a person can check by eye on a
    turnaround — which is why they are the ones that must be exact."""

    @pytest.fixture(scope="class")
    def rows(self, tmp_path_factory):
        return _result(tmp_path_factory.mktemp("props"), _PROPORTIONS)

    def test_the_figure_is_the_height_it_was_asked_for(self, rows):
        for row in rows:
            asked = row["case"]["height"]
            assert abs(row["height"] - asked) < 0.002, row["case"]

    def test_the_soles_stand_on_the_ground_plane(self, rows):
        # A character whose feet are not at z=0 floats or sinks in every engine
        # that drops it at the origin, and no render angle shows it.
        for row in rows:
            assert abs(row["sole"]) < 0.001, row["case"]

    def test_the_head_is_one_head_count_of_the_height(self, rows):
        for row in rows:
            case = row["case"]
            assert abs(row["head"] - case["height"] / case["heads"]) < 1e-6, case
            # and the body below the chin is what is left over
            assert abs(row["chin"] + row["head"] - case["height"]) < 1e-6, case

    def test_stylising_shortens_the_body_not_the_head(self, rows):
        # The whole reason head-count is a parameter: a 3-head chibi is not a
        # shrunken adult, it is an adult with two thirds of its body replaced by
        # head. If both scaled together the parameter would do nothing.
        by_heads = {row["case"]["heads"]: row for row in rows
                    if row["case"]["height"] in (1.8, 1.6, 1.0)}
        chibi, adult = by_heads[3.0], by_heads[7.5]
        assert chibi["head"] / chibi["case"]["height"] > 0.30
        assert adult["head"] / adult["case"]["height"] < 0.15

    def test_the_crotch_sits_at_half_the_height_on_an_adult(self, rows):
        adult = [r for r in rows if r["case"] == {"height": 1.8, "heads": 7.5}][0]
        assert abs(adult["crotch"] / adult["case"]["height"] - 0.5) < 0.005

    def test_the_arm_span_equals_the_height_on_a_realistic_figure(self, rows):
        # The one proportion check a human can do on a turnaround without a
        # ruler. It holds at realistic head-counts only, and that is correct
        # rather than a limitation: arm length is a fraction of the CHIN
        # HEIGHT, so a 5-head stylised figure has a short body and short arms
        # to match. `shoulders` and `limbs` deliberately break it too — that is
        # what they are for — so they are excluded rather than fudged.
        checked = 0
        for row in rows:
            case = row["case"]
            if "shoulders" in case or "limbs" in case or case["heads"] < 7.0:
                continue
            assert abs(row["span"] / row["height"] - 1.0) < 0.02, case
            checked += 1
        assert checked >= 3

    def test_a_stylised_figure_has_proportionally_shorter_arms(self, rows):
        stylised = [r for r in rows if r["case"]["heads"] == 5.0][0]
        chibi = [r for r in rows if r["case"]["heads"] == 3.0][0]
        assert chibi["span"] / chibi["height"] < stylised["span"] / stylised["height"] < 0.98

    def test_build_widens_without_changing_the_height(self, rows):
        heavy = [r for r in rows if r["case"].get("build") == 1.6][0]
        assert abs(heavy["height"] - 2.4) < 0.002

    def test_shoulders_widen_the_shoulder_line(self, rows):
        wide = [r for r in rows if r["case"].get("shoulders") == 1.4][0]
        plain = [r for r in rows if r["case"] == {"height": 1.8, "heads": 7.5}][0]
        assert wide["shoulder_hw"] > plain["shoulder_hw"] * 1.3

    def test_nonsense_proportions_raise(self, tmp_path):
        # DELIBERATELY LOUD, like bg_bone_chain. A silently clamped 0-head
        # figure is worse than a stopped run: it looks built.
        for bad in ("bg_proportions(height=0)", "bg_proportions(heads=1.0)",
                    "bg_proportions(heads=40)"):
            assert "bg_proportions" in _fails(tmp_path, bad)


# ---------------------------------------------------------------------------
# Cleanliness — the single measured failure mode of this pipeline
# ---------------------------------------------------------------------------

_CLEAN = r"""
import json
out = []
for case in [{"detail": 0}, {"detail": 1}, {"detail": 2}, {"pose": "a"},
             {"heads": 3.0, "height": 1.0}, {"build": 0.6}, {"limbs": 0.7}]:
    bg_wipe()
    made = bg_human(**case)
    report = bg_base_report(made)
    bind = bg_weight(made["obj"], made["rig"])
    out.append({"case": case, "verts": report["verts"], "faces": report["faces"],
                "loose": report["loose"], "nonmanifold": report["nonmanifold"],
                "flipped": report["flipped"], "ngons": report["ngons"],
                "uv": len(made["obj"].data.uv_layers),
                "materials": [m.name for m in made["obj"].data.materials],
                "unweighted": bind["unweighted"], "total": bind["total"],
                "deform_bones": bind["bones"]})
print("RESULT " + json.dumps(out))
"""


@requires_blender
class TestCleanliness:
    """bg_clean EXISTS BECAUSE BONE-HEAT WEIGHTING SILENTLY REFUSES bad
    geometry. A base mesh that arrives doubled, loose or inside-out hands the
    agent the exact problem the base was built to remove."""

    @pytest.fixture(scope="class")
    def rows(self, tmp_path_factory):
        return _result(tmp_path_factory.mktemp("clean"), _CLEAN)

    def test_no_loose_geometry(self, rows):
        for row in rows:
            assert row["loose"] == 0, row["case"]

    def test_every_shell_is_closed(self, rows):
        # Lofted shells are capped at both ends by construction. A non-manifold
        # edge here means a cap was skipped or a ring count did not line up.
        for row in rows:
            assert row["nonmanifold"] == 0, row["case"]

    def test_nothing_is_inside_out(self, rows):
        # An inverted face passes every other check, refuses weighting and
        # renders as a hole.
        for row in rows:
            assert row["flipped"] == 0, row["case"]

    def test_no_ngons(self, rows):
        # Caps are triangle fans, not n-gons: an n-gon triangulates
        # unpredictably on glTF export, which is where a flat sole creases.
        for row in rows:
            assert row["ngons"] == 0, row["case"]

    def test_the_base_is_texturable_and_coloured(self, rows):
        # No UV layer means no texture, and the readiness check only says so
        # after the layer is already built and exported.
        for row in rows:
            assert row["uv"] == 1, row["case"]
            assert row["materials"], row["case"]

    def test_the_vertex_count_stays_in_budget(self, rows):
        by_detail = {row["case"].get("detail"): row for row in rows}
        assert by_detail[0]["verts"] < by_detail[1]["verts"] < by_detail[2]["verts"]
        assert by_detail[2]["verts"] < 4000, "a base mesh, not a sculpt"

    def test_automatic_weighting_leaves_nothing_unweighted(self, rows):
        # THE ASSERTION THE WHOLE LIBRARY IS FOR. Automatic weighting does not
        # raise when it gives up; it leaves vertices with no group, which reads
        # in-engine as the mesh tearing at the rest pose and reads in Blender as
        # nothing at all.
        for row in rows:
            assert row["unweighted"] == 0, (row["case"], row["unweighted"],
                                            row["total"])
            assert row["total"] > 300, row["case"]
            assert row["deform_bones"] >= 20, row["case"]


# ---------------------------------------------------------------------------
# The skeleton — the bone names ARE the contract
# ---------------------------------------------------------------------------

_SKELETON = r"""
import json
bg_wipe()
made = bg_human(height=1.8, heads=7.5)
rig = made["rig"]
axes = {}
for bone in rig.data.bones:
    matrix = bone.matrix_local.to_3x3()
    # THREE NUMBERS, NOT TWO VECTORS. run_script keeps only the LAST 4000
    # characters of stdout, and a 23-bone dump is already most of that budget;
    # printing head and tail in full evicts the "RESULT " prefix off the front
    # of the line and the read comes back empty rather than wrong.
    axes[bone.name] = {"z": [round(v, 3) for v in matrix.col[2]],
                       "x": round(bone.head_local[0], 4),
                       "y0": round(bone.head_local[1], 4),
                       "y1": round(bone.tail_local[1], 4),
                       "deform": bone.use_deform,
                       "parent": bone.parent.name if bone.parent else None,
                       "length": round(bone.length, 5)}
bg_wipe()
other = bg_human(height=1.8, convention="blender")
print("RESULT " + json.dumps({
    "axes": axes,
    "roles": {key: bg_bone(made, key) for key in
              ("root", "hips", "spine", "chest", "upperchest", "neck", "head",
               "shoulder.L", "upperarm.L", "lowerarm.L", "hand.L",
               "upperleg.R", "lowerleg.R", "foot.R", "toes.R")},
    "blender_names": sorted(b.name for b in other["rig"].data.bones),
}, separators=(",", ":")))
"""


@requires_blender
class TestSkeleton:
    """combine(bind='bone:Head') MATCHES ON A STRING. An armature of Bone.001
    makes every rigid layer a guess and no humanoid retarget can read it."""

    @pytest.fixture(scope="class")
    def got(self, tmp_path_factory):
        return _result(tmp_path_factory.mktemp("skel"), _SKELETON)

    def test_the_bones_carry_godot_humanoid_names(self, got):
        # These are Godot's SkeletonProfileHumanoid spellings, so an import maps
        # them with no hand-written bone table.
        expected = {
            "Root", "Hips", "Spine", "Chest", "UpperChest", "Neck", "Head",
            "LeftShoulder", "LeftUpperArm", "LeftLowerArm", "LeftHand",
            "RightShoulder", "RightUpperArm", "RightLowerArm", "RightHand",
            "LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "LeftToes",
            "RightUpperLeg", "RightLowerLeg", "RightFoot", "RightToes",
        }
        assert set(got["axes"]) == expected

    def test_no_bone_was_renamed_by_blender(self, got):
        # bg_bone_chain raises on a rename, so this is really a guard on the
        # names themselves staying inside Blender's 63-character limit.
        assert not [name for name in got["axes"] if "." in name]

    def test_the_chain_is_one_tree_rooted_at_root(self, got):
        roots = [name for name, row in got["axes"].items() if row["parent"] is None]
        assert roots == ["Root"]
        assert got["axes"]["Hips"]["parent"] == "Root"
        assert got["axes"]["LeftUpperLeg"]["parent"] == "Hips"
        assert got["axes"]["LeftUpperArm"]["parent"] == "LeftShoulder"
        assert got["axes"]["LeftToes"]["parent"] == "LeftFoot"

    def test_the_root_does_not_deform(self, got):
        # A deforming root drags the whole mesh with the engine's transform
        # handle. Every other bone must deform, or a rigid layer bound to it
        # binds to nothing that combine's deform_bones() will count.
        assert got["axes"]["Root"]["deform"] is False
        assert all(row["deform"] for name, row in got["axes"].items()
                   if name != "Root")

    def test_no_bone_is_zero_length(self, got):
        # Blender DELETES zero-length bones on leaving edit mode and says
        # nothing about it.
        for name, row in got["axes"].items():
            assert row["length"] > 1e-3, name

    def test_every_limb_and_spine_bone_rolls_to_face_forward(self, got):
        # ROLL IS THE DIFFERENCE BETWEEN A RIG AND A PILE OF BONES. With every
        # roll at 0 each hinge bends about whatever axis fell out of its
        # head/tail direction, and a humanoid retarget produces the
        # twisted-forearm look that reads as a broken animation. The rule is
        # one rule: local Z faces the character's front, and the front is +Y.
        for name, row in got["axes"].items():
            if "Foot" in name or "Toes" in name:
                continue
            assert row["z"][1] > 0.98, (name, row["z"])

    def test_the_left_bones_are_on_the_figures_own_left(self, got):
        # NOT COSMETIC. These are SkeletonProfileHumanoid names an engine
        # retargets on, and a base whose "Left" bones sit on its anatomical
        # right mirrors every animation ever bound to it without one error.
        # A figure facing +Y with +Z up has its left at -X.
        for name in ("LeftHand", "LeftUpperArm", "LeftFoot", "LeftToes"):
            assert got["axes"][name]["x"] < 0.0, (name, got["axes"][name])
        for name in ("RightHand", "RightUpperArm", "RightFoot", "RightToes"):
            assert got["axes"][name]["x"] > 0.0, (name, got["axes"][name])

    def test_the_toe_bones_run_toward_the_front(self, got):
        # The one bone chain that encodes the facing rather than inheriting it.
        # Foot head -> Toes tail must travel +Y, or the character walks
        # backwards out of the exporter.
        for tag in ("Left", "Right"):
            foot, toes = got["axes"][tag + "Foot"], got["axes"][tag + "Toes"]
            assert toes["y1"] > toes["y0"] > foot["y0"], (tag, foot, toes)
            assert toes["y1"] > 0.0, (tag, toes)

    def test_the_feet_roll_to_face_up_instead(self, got):
        # A toe bone runs along the forward axis, so there is no roll that can
        # align its local Z to it. Feet reference world up, which is the
        # convention for feet anyway.
        for name in ("LeftFoot", "RightFoot", "LeftToes", "RightToes"):
            assert got["axes"][name]["z"][2] > 0.9, (name, got["axes"][name]["z"])

    def test_bone_roles_resolve_to_the_real_names(self, got):
        assert got["roles"]["head"] == "Head"
        assert got["roles"]["hand.L"] == "LeftHand"
        assert got["roles"]["toes.R"] == "RightToes"

    def test_the_blender_convention_is_also_available(self, got):
        assert "UpperArm.L" in got["blender_names"]
        assert "Head" in got["blender_names"]      # unchanged across conventions

    def test_an_unknown_bone_role_raises(self, tmp_path):
        # bind='bone:<a name you spelled wrong>' binds nothing and the layer
        # stands still while the body moves. Refuse at the lookup instead.
        message = _fails(tmp_path, "bg_bone({}, 'lefthand')")
        assert "bg_bone" in message and "lefthand" in message


# ---------------------------------------------------------------------------
# Landmarks — the shared frame layers never had
# ---------------------------------------------------------------------------

_MARKS = r"""
import json
bg_wipe()
made = bg_human(height=1.8, heads=7.5)
body, marks = made["obj"], made["marks"]
box = bg_bounds(body)


def half_width_at(z, band=0.012):
    xs = [abs((body.matrix_world @ v.co).x) for v in body.data.vertices
          if abs((body.matrix_world @ v.co).z - z) < band]
    return max(xs) if xs else None


cap = bg_ball("Cap", radius=0.5)
fitted = bg_fit(cap, bg_mark(made, "head"), mode="around", clearance=0.006)
bg_fit(cap, bg_mark(made, "head_top"), mode="on", clearance=-0.02)
sat = bg_overlap(cap, body)

naive = bg_ball("Naive", radius=0.12, at=(0, 0, 1.7))
guessed = bg_overlap(naive, body)

held = bg_box("Held", size=(0.05, 0.05, 0.4))
bg_fit(held, bg_mark(made, "hand.R"), mode="at", scale=False)

print("RESULT " + json.dumps({
    "names": sorted(marks),
    "crown_vs_mesh_top": marks["head_top"]["pos"][2] - box["max"][2],
    "sole_vs_mesh_bottom": marks["sole.L"]["pos"][2] - box["min"][2],
    "waist": [marks["waist"]["radius"], half_width_at(marks["waist"]["pos"][2])],
    "chest": [marks["chest"]["radius"], half_width_at(marks["chest"]["pos"][2])],
    "head_girth": marks["head"]["girth"],
    "wrist_below_shoulder": marks["shoulder.L"]["pos"][2] - marks["wrist.L"]["pos"][2],
    "shoulder_inside_wrist": marks["wrist.L"]["pos"][0] - marks["shoulder.L"]["pos"][0],
    "cap": {"scale": fitted["scale"], "dims": bg_bounds(cap)["dims"],
            "intersects": sat["intersects"], "fraction": sat["fraction"],
            "verdict": sat["verdict"]},
    "guessed": {"intersects": guessed["intersects"],
                "fraction": guessed["fraction"], "verdict": guessed["verdict"]},
    "held_centre": list(bg_bounds(held)["centre"]),
    "hand_mark": list(marks["hand.R"]["pos"]),
}))
"""


@requires_blender
class TestLandmarks:
    """LAYERS ARE AUTHORED IN ISOLATED EMPTY SCENES. Nothing puts two of them
    side by side until they are already combined, so "is the cap at head
    height" was never a question anyone could answer while it was cheap."""

    @pytest.fixture(scope="class")
    def got(self, tmp_path_factory):
        return _result(tmp_path_factory.mktemp("marks"), _MARKS)

    def test_the_frame_names_every_place_a_layer_attaches(self, got):
        for name in ("head_top", "head", "neck", "chin", "eye_line", "face",
                     "shoulder_line", "chest", "waist", "hips", "crotch",
                     "shoulder.L", "elbow.L", "wrist.L", "hand.L",
                     "knee.R", "ankle.R", "foot.R", "sole.R", "ground"):
            assert name in got["names"], name

    def test_the_crown_landmark_is_the_top_of_the_head(self, got):
        assert abs(got["crown_vs_mesh_top"]) < 0.002

    def test_the_sole_landmark_is_the_bottom_of_the_foot(self, got):
        assert abs(got["sole_vs_mesh_bottom"]) < 0.002

    def test_a_landmark_radius_is_the_mesh_it_describes(self, got):
        # A published radius that does not match the surface is worse than no
        # radius: it is a number a layer will be scaled to.
        for key in ("waist", "chest"):
            stated, measured = got[key]
            assert measured is not None, key
            assert abs(stated - measured) < 0.004, (key, stated, measured)

    def test_the_head_girth_is_a_hatband(self, got):
        # An adult head measures around 0.55 m. A layer scaled to this number
        # is a hat that fits.
        assert 0.48 < got["head_girth"] < 0.62

    def test_the_arm_landmarks_run_down_the_arm(self, got):
        # OUTWARD, not "positive". The left arm runs out along -X, so the test
        # that used to read `wrist.x - shoulder.x > 0.3` would now pass only by
        # accident on a base whose left and right had been swapped.
        assert got["shoulder_inside_wrist"] < -0.3
        assert abs(got["wrist_below_shoulder"]) < 0.01     # T-pose: level

    def test_a_cap_fitted_to_the_head_landmark_lands_on_the_head(self, got):
        # THE HEADLINE CLAIM. Fitted, the cap touches the skull and does not
        # swallow it.
        assert got["cap"]["intersects"] is True, got["cap"]["verdict"]
        assert got["cap"]["fraction"] < 0.5, got["cap"]["verdict"]
        assert 0.18 < got["cap"]["dims"][0] < 0.24

    def test_the_same_cap_at_a_guessed_height_is_sunk_in_the_skull(self, got):
        # The control. A sphere at "about head height" — which is what an agent
        # writes without a frame — is 89% inside the head and every check in the
        # pipeline passes it.
        assert got["guessed"]["fraction"] > 0.6, got["guessed"]["verdict"]

    def test_a_held_prop_lands_in_the_hand(self, got):
        for stated, measured in zip(got["hand_mark"], got["held_centre"]):
            assert abs(stated - measured) < 0.002

    def test_an_unknown_landmark_raises(self, tmp_path):
        # A mis-typed landmark returning an empty dict puts the layer at the
        # world origin, between the feet, which reads on a turnaround as "the
        # hat did not generate" and costs another run to find.
        message = _fails(tmp_path, "bg_mark(bg_human(), 'hed')")
        assert "bg_mark" in message and "hed" in message


# ---------------------------------------------------------------------------
# The facing convention — +Y, and the same +Y everywhere
# ---------------------------------------------------------------------------

_FACING = r"""
import json
bg_wipe()
base = bg_human(height=1.8, heads=7.5)
body, marks, P = base["obj"], base["marks"], base["props"]
co = [(body.matrix_world @ v.co) for v in body.data.vertices]

# THE MESH'S OWN ASYMMETRY. A nude base has no face, so the feet are the only
# geometry that says which way it is pointing — and they are geometry, not a
# constant a test could agree with while the model disagreed.
foot = [c for c in co if c.z < P["ankle_z"] * 1.2]
head = [c for c in co if c.z > P["chin"]]
jaw = [c for c in co if P["chin"] < c.z < P["chin"] + P["head"] * 0.35]
cranium = [c for c in co if c.z > P["chin"] + P["head"] * 0.65]

bg_wipe()
quad = bg_quadruped(length=1.2, height=0.75)
qco = [(quad["obj"].matrix_world @ v.co) for v in quad["obj"].data.vertices]
qmarks = quad["marks"]
qbones = {b.name: [round(v, 5) for v in b.head_local]
          for b in quad["rig"].data.bones}

bg_wipe()
prop = bg_prop_frame(size=(0.4, 0.4, 0.6))

print("RESULT " + json.dumps({
    "forward": list(BG_FORWARD), "left": list(BG_LEFT),
    "sides": {tag: sign for tag, sign in BG_SIDES},
    "foot_toe_y": max(c.y for c in foot), "foot_heel_y": min(c.y for c in foot),
    "jaw_y": max(c.y for c in jaw), "cranium_y": min(c.y for c in cranium),
    "head_y_max": max(c.y for c in head), "head_y_min": min(c.y for c in head),
    "toe_y": P["toe_y"], "heel_y": P["heel_y"],
    "marks": {name: {"pos": list(marks[name]["pos"]),
                     "dir": list(marks[name]["dir"])}
              for name in ("face", "eye_line", "head", "head_top", "foot.L",
                           "hand.L", "hand.R", "shoulder.L", "shoulder.R")},
    "quad": {"muzzle": list(qmarks["muzzle"]["pos"]),
             "tail": list(qmarks["tail"]["pos"]),
             "withers": list(qmarks["withers"]["pos"]),
             "rump": list(qmarks["rump"]["pos"]),
             "front_paw_L": list(qmarks["front_paw.L"]["pos"]),
             "back_paw_L": list(qmarks["back_paw.L"]["pos"]),
             "head_bone": qbones.get("Head"), "tail_bone": qbones.get("Tail"),
             "y_min": min(c.y for c in qco), "y_max": max(c.y for c in qco)},
    "prop": {name: {"pos": list(prop["marks"][name]["pos"]),
                    "dir": list(prop["marks"][name]["dir"])}
             for name in ("front", "back", "left", "right", "grip")},
}))
"""


@requires_blender
class TestTheBaseFacesForward:
    """+Y IN BLENDER, WHICH IS -Z IN THE ENGINE.

    Blender's own front view looks along +Y, so the Blender-native answer is
    -Y and every rig tutorial gives it. But the glTF exporter turns Blender +Y
    into glTF -Z, and -Z is what Godot calls forward — so a base authored the
    Blender way arrives in the engine facing the camera it should be walking
    away from, and the only fixes are a hidden compensating rotation in the
    export path or a 180 every consumer has to remember.

    THE FAILURE THIS CLASS IS REALLY GUARDING IS THE HALF-FLIP. The landmarks,
    the mesh, the bone chain, the quadruped and the prop anchors each encode a
    direction separately; a base whose feet point one way and whose "face"
    landmark points the other passes every check in the sibling classes, looks
    correct on a turnaround, and puts the hat on the back of the head.
    """

    @pytest.fixture(scope="class")
    def got(self, tmp_path_factory):
        return _result(tmp_path_factory.mktemp("facing"), _FACING)

    def test_the_declared_forward_is_plus_y(self, got):
        assert got["forward"] == [0.0, 1.0, 0.0]

    def test_the_declared_left_is_up_cross_forward(self, got):
        # BG_UP x BG_FORWARD = (0,0,1) x (0,1,0) = (-1,0,0). Not a preference:
        # get it backwards and every "Left" bone name is a lie.
        assert got["left"] == [-1.0, 0.0, 0.0]
        assert got["sides"] == {"L": -1.0, "R": 1.0}

    def test_the_feet_point_forward(self, got):
        """THE MEASUREMENT, not the constant. The toes reach further +Y than the
        heel reaches -Y — on the mesh, in world space, after the join."""
        assert got["foot_toe_y"] > 0.0 > got["foot_heel_y"], got
        assert got["foot_toe_y"] > abs(got["foot_heel_y"]) * 2.0, got
        assert got["toe_y"] > 0.0 > got["heel_y"], got

    def test_the_jaw_leads_and_the_cranium_trails(self, got):
        # The head is the second piece of asymmetric geometry, and it is built
        # by a different function from the feet — which is exactly how a half
        # flip gets in.
        assert got["jaw_y"] > 0.0, got
        assert got["cranium_y"] < 0.0, got
        assert got["head_y_max"] > 0.0 > got["head_y_min"], got

    def test_the_face_landmarks_are_on_the_face_side(self, got):
        for name in ("face", "eye_line"):
            mark = got["marks"][name]
            assert mark["pos"][1] > 0.0, (name, mark)
            assert mark["dir"] == [0.0, 1.0, 0.0], (name, mark)

    def test_the_head_landmark_sits_behind_the_axis_with_the_cranium(self, got):
        # A hat centred on the spine rather than on the skull rides forward.
        for name in ("head", "head_top"):
            assert got["marks"][name]["pos"][1] < 0.0, (name, got["marks"][name])

    def test_the_foot_landmark_is_ahead_of_the_ankle(self, got):
        assert got["marks"]["foot.L"]["pos"][1] > 0.0, got["marks"]["foot.L"]
        assert got["marks"]["foot.L"]["dir"] == [0.0, 1.0, 0.0]

    def test_the_left_landmarks_are_on_the_figures_own_left(self, got):
        assert got["marks"]["hand.L"]["pos"][0] < 0.0, got["marks"]["hand.L"]
        assert got["marks"]["hand.R"]["pos"][0] > 0.0, got["marks"]["hand.R"]
        assert got["marks"]["shoulder.L"]["dir"] == [-1.0, 0.0, 0.0]
        assert got["marks"]["shoulder.R"]["dir"] == [1.0, 0.0, 0.0]

    def test_the_quadruped_faces_the_same_way_as_the_human(self, got):
        """A wolf pointing the other way from the person who walks it is the
        half-flip one directory over."""
        quad = got["quad"]
        assert quad["muzzle"][1] > 0.0 > quad["tail"][1], quad
        # The nose is the front-most point of the animal and the tail the back-
        # most, which is also what bg_quadruped_proportions solved `length`
        # against — so getting this wrong changes the declared size too.
        assert quad["muzzle"][1] == pytest.approx(quad["y_max"], abs=0.02), quad
        assert quad["tail"][1] > quad["y_min"], quad
        assert quad["withers"][1] > quad["rump"][1], quad
        assert quad["front_paw_L"][1] > quad["back_paw_L"][1], quad
        assert quad["front_paw_L"][0] < 0.0, quad
        assert quad["head_bone"][1] > 0.0 > quad["tail_bone"][1], quad

    def test_a_props_front_is_the_same_front(self, got):
        """A crate whose "front" faced the opposite way from the figure carrying
        it would put every decal on its back the moment either one was right."""
        prop = got["prop"]
        assert prop["front"]["pos"][1] > 0.0 > prop["back"]["pos"][1], prop
        assert prop["front"]["dir"] == [0.0, 1.0, 0.0], prop
        assert prop["back"]["dir"] == [0.0, -1.0, 0.0], prop
        assert prop["left"]["pos"][0] < 0.0 < prop["right"]["pos"][0], prop
        assert prop["left"]["dir"] == [-1.0, 0.0, 0.0], prop
        # A handle is on the front face, not the back one.
        assert prop["grip"]["pos"][1] > 0.0, prop


# ---------------------------------------------------------------------------
# The unit convention
# ---------------------------------------------------------------------------

_UNITS = r"""
import json
out = {"checks": []}
for value in (1.8, 1.75, 2.4, 180.0, 0.018, 1800.0, 0.0018, 70.87):
    report = bg_unit_check(value, expect=1.8)
    out["checks"].append({"in": value, "ok": report["ok"],
                          "guess": report["unit_guess"],
                          "fix": report["scale_to_fix"],
                          "verdict": report["verdict"]})
out["unit"] = BG_UNIT
out["default_height"] = BG_HUMAN_HEIGHT

bg_wipe()
made = bg_human(height=1.8)
made["obj"].scale = (100.0, 100.0, 100.0)
bpy.context.view_layer.update()
out["scaled"] = bg_unit_check(made["obj"], expect=1.8)["unit_guess"]
bg_rescale(made["obj"], 1.8, others=(made["rig"],))
after = bg_unit_check(made["obj"], expect=1.8)
out["rescaled"] = {"ok": after["ok"], "height": after["height"],
                   "sole": bg_bounds(made["obj"])["min"][2]}
print("RESULT " + json.dumps(out))
"""


@requires_blender
class TestUnitConvention:
    """NOTHING IN THIS PIPELINE DECLARED A UNIT. blender_turnaround frames the
    camera to the subject's own bounding box, so a 180 m character and a 0.018 m
    one come back as identical, perfect-looking renders. Scale is the one defect
    here that is completely invisible downstream and completely fatal in a
    level."""

    @pytest.fixture(scope="class")
    def got(self, tmp_path_factory):
        return _result(tmp_path_factory.mktemp("units"), _UNITS)

    def test_the_declared_unit_is_the_metre(self, got):
        assert got["unit"] == "metre"
        assert got["default_height"] == 1.8

    def test_it_accepts_a_person_sized_person(self, got):
        for row in got["checks"]:
            if row["in"] in (1.8, 1.75, 2.4):
                assert row["ok"] is True, row["verdict"]

    def test_it_rejects_a_hundred_times_too_big(self, got):
        row = [r for r in got["checks"] if r["in"] == 180.0][0]
        assert row["ok"] is False
        assert row["guess"] == "centimetres"
        assert abs(row["fix"] - 0.01) < 1e-6

    def test_it_rejects_a_hundred_times_too_small(self, got):
        row = [r for r in got["checks"] if r["in"] == 0.018][0]
        assert row["ok"] is False
        assert abs(row["fix"] - 100.0) < 1e-3

    def test_it_names_the_unit_somebody_probably_meant(self, got):
        guesses = {row["in"]: row["guess"] for row in got["checks"]}
        assert guesses[1800.0] == "millimetres"
        assert guesses[70.87] == "inches"

    def test_the_verdict_says_what_to_scale_by(self, got):
        row = [r for r in got["checks"] if r["in"] == 180.0][0]
        assert "0.01" in row["verdict"]

    def test_a_failing_check_can_be_made_to_raise(self, tmp_path):
        message = _fails(tmp_path, "bg_unit_assert(180.0, expect=1.8)")
        assert "bg_unit_assert" in message and "centimetres" in message

    def test_rescale_fixes_a_wrong_scale_and_re_grounds_it(self, got):
        assert got["scaled"] == "centimetres"
        assert got["rescaled"]["ok"] is True
        assert abs(got["rescaled"]["height"] - 1.8) < 0.002
        assert abs(got["rescaled"]["sole"]) < 0.002


# ---------------------------------------------------------------------------
# The other two bases, and the base's own self-check
# ---------------------------------------------------------------------------

_OTHERS = r"""
import json
out = {"quadrupeds": [], "props": []}
for args in ({"length": 1.2, "height": 0.75},
             {"length": 0.5, "height": 0.30},
             {"length": 2.4, "height": 1.6, "build": 1.2}):
    bg_wipe()
    made = bg_quadruped(**args)
    report = bg_base_report(made, expect_height=args["height"])
    bind = bg_weight(made["obj"], made["rig"])
    out["quadrupeds"].append(
        {"args": args, "dims": list(report["dims"]), "sole": report["min"][2],
         "loose": report["loose"], "nonmanifold": report["nonmanifold"],
         "flipped": report["flipped"], "unweighted": bind["unweighted"],
         "bones": report["bones"], "marks": report["marks"]})

for size in ((0.4, 0.4, 0.6), (1.2, 0.8, 0.35)):
    bg_wipe()
    made = bg_prop_frame(size=size)
    report = bg_base_report(made)
    out["props"].append({"size": list(size), "dims": list(report["dims"]),
                         "base": report["min"][2], "loose": report["loose"],
                         "nonmanifold": report["nonmanifold"],
                         "flipped": report["flipped"],
                         "bones": report["bones"], "marks": report["marks"]})
print("RESULT " + json.dumps(out))
"""


@requires_blender
class TestQuadrupedAndProp:
    @pytest.fixture(scope="class")
    def got(self, tmp_path_factory):
        return _result(tmp_path_factory.mktemp("others"), _OTHERS)

    def test_a_quadruped_is_the_size_it_was_asked_for(self, got):
        # Both numbers are exact and both are declared. A `build` that quietly
        # changed the height would be the same defect as an undeclared unit, in
        # a smaller package.
        for row in got["quadrupeds"]:
            assert abs(row["dims"][2] - row["args"]["height"]) < 0.002, row["args"]
            assert abs(row["dims"][1] - row["args"]["length"]) < 0.002, row["args"]

    def test_a_quadruped_stands_on_its_paws(self, got):
        for row in got["quadrupeds"]:
            assert abs(row["sole"]) < 0.001, row["args"]

    def test_a_quadruped_is_clean_and_fully_weighted(self, got):
        for row in got["quadrupeds"]:
            assert (row["loose"], row["nonmanifold"], row["flipped"]) == (0, 0, 0)
            assert row["unweighted"] == 0, row["args"]

    def test_a_quadruped_has_named_bones_and_landmarks(self, got):
        bones = got["quadrupeds"][0]["bones"]
        for name in ("Root", "Hips", "Spine", "Chest", "Neck", "Head", "Tail",
                     "FrontUpperLeg.L", "BackLowerLeg.R", "FrontPaw.R"):
            assert name in bones, name
        marks = got["quadrupeds"][0]["marks"]
        for name in ("withers", "back", "rump", "head", "muzzle", "tail",
                     "front_paw.L", "back_knee.R"):
            assert name in marks, name

    def test_a_prop_frame_is_exactly_the_size_it_declares(self, got):
        # Insetting the end rings vertically as well as horizontally made a
        # 0.6 m crate measure 0.564 and stand 18 mm off the floor — a 6% error
        # no render shows and no unit check is tight enough to catch.
        for row in got["props"]:
            for asked, measured in zip(row["size"], row["dims"]):
                assert abs(asked - measured) < 0.002, row["size"]
            assert abs(row["base"]) < 0.001, row["size"]

    def test_a_prop_frame_is_clean_and_has_a_bone_to_bind_to(self, got):
        for row in got["props"]:
            assert (row["loose"], row["nonmanifold"], row["flipped"]) == (0, 0, 0)
            assert row["bones"] == ["Body", "Root"]
            for name in ("base", "top", "front", "centre", "grip"):
                assert name in row["marks"], name


_SELF_CHECK = r"""
import json
bg_wipe()
made = bg_human(height=1.8)
report = bg_base_assert(made)
out = {"clean": [report["loose"], report["nonmanifold"], report["flipped"]],
       "unit_ok": report["unit"]["ok"]}
# and the worked script runs, exactly as an agent would read it
bg_wipe()
exec(BG_BASE_EXAMPLE)
out["example"] = "ran"
print("RESULT " + json.dumps(out))
"""


@requires_blender
class TestSelfCheck:
    def test_the_base_checks_itself_and_the_worked_script_runs(self, tmp_path):
        got = _result(tmp_path, _SELF_CHECK)
        assert got["clean"] == [0, 0, 0]
        assert got["unit_ok"] is True
        assert got["example"] == "ran"

    def test_the_self_check_refuses_a_base_at_the_wrong_scale(self, tmp_path):
        # bg_base_assert is the one thing between a 180 m character and a
        # perfect-looking turnaround of it.
        message = _fails(tmp_path, (
            "made = bg_human(height=1.8)\n"
            "made['obj'].scale = (100.0, 100.0, 100.0)\n"
            "bpy.context.view_layer.update()\n"
            "bg_base_assert(made)\n"))
        assert "bg_base_assert" in message
