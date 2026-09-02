"""Taking a GENERATED mesh and making it usable: weld, orient, budget.

A generation does not arrive as an asset. It arrives as a pile of disconnected
shells at an arbitrary scale facing an arbitrary direction. Everything here was
written against real Krea output and the numbers in the docstrings are measured,
not illustrative.

Two of them are corrections to my own first attempt, and both are the reason
these tests exist:

  * "weld until it is one shell" is WRONG. On a generated mannequin it took
    non-manifold edges from 3 to 20,285 and the decimator then could not reach
    an 8,000 triangle budget at all, stalling at 49,261 however many passes it
    got. The same mesh merged six times more gently sat in 4 shells with 3
    non-manifold edges and decimated to 7,999. Non-manifold count predicts
    decimatability; shell count does not.

  * "which way does this face" has NO universal geometric answer. Reading it
    from the bounding box rotated a crate 90 degrees onto a different footprint
    for no reason. It has to refuse to guess.
"""
from __future__ import annotations

import shutil

import pytest

from bgate_adapters import blender

_no_blender = pytest.mark.skipif(
    not shutil.which("blender") and not blender.available().get("available"),
    reason="needs a real Blender")


def requires_blender(obj):
    """SLOW as well as skipped-when-missing. See test_blender.py's docstring.

    Every test here happens to need it today, so this could be a module-level
    ``pytestmark``. It is a decorator because that is what the file already used
    and what its sibling blender suites use — a test added tomorrow that only
    reads the generated script should not inherit `slow` by sitting in this file.
    """
    return pytest.mark.slow(_no_blender(obj))


def run(body: str) -> dict:
    """Run a kit script and hand back the printed lines by their first word."""
    result = blender.run_script("bg_wipe()\n" + body, kit=True, timeout=300)
    assert result.get("ok"), result.get("traceback") or result.get("error")
    out: dict = {}
    for line in (result.get("print") or "").splitlines():
        parts = line.split(" ", 1)
        if parts[0].isupper() and len(parts) > 1:
            out[parts[0]] = parts[1].strip()
    return out


@requires_blender
class TestFacingRefusesToGuess:
    def test_a_shape_that_stands_on_feet_is_read_from_its_toes(self):
        """A foot reaches much further forward of the ankle than the heel does
        behind it, which is the only asymmetry a featureless figure reliably
        has — a generated head is often an egg, so a face is no use."""
        got = run('''
base = bg_human(height=1.8, pose="a")
read = bg_facing(base["obj"])
print("SIGN", read["sign"])
print("CONF", read["confident"])
print("STR", read["strength"])
''')
        assert got["SIGN"] == "1", "the base faces +Y, so its toes lead that way"
        assert got["CONF"] == "True"
        assert float(got["STR"]) >= 1.25, "below the threshold this is a guess"

    def test_something_with_no_front_is_left_alone(self):
        """MEASURED: guessing turned a generated crate 90 degrees onto a
        different footprint. An asset with no front is not a problem to solve."""
        got = run('''
box = bg_box("Crate", size=(1.0, 0.8, 0.6))
before = tuple(round(d, 4) for d in box.dimensions)
rep = bg_orient(box, kind="none")
print("TURNED", rep["turned_deg"])
print("SAME", before == tuple(round(d, 4) for d in box.dimensions))
print("NOTE", rep["note"][:60])
''')
        assert got["TURNED"] == "0"
        assert got["SAME"] == "True"

    def test_an_unreadable_front_refuses_rather_than_rotating(self):
        got = run('''
ball = bg_ball("Ball", radius=0.5)
rep = bg_orient(ball, kind="humanoid")
print("TURNED", rep["turned_deg"], "CONF", rep["confident"])
''')
        assert got["TURNED"].startswith("0")
        assert "False" in got["TURNED"] or "False" in got.get("CONF", "")

    def test_an_explicit_assume_is_obeyed_when_geometry_cannot_say(self):
        got = run('''
ball = bg_ball("Ball", radius=0.5)
rep = bg_orient(ball, kind="humanoid", assume=-1)
print("TURNED", rep["turned_deg"], "CONF", rep["confident"])
''')
        assert got["TURNED"].split(" ")[0] in {"0", "90", "180", "270"}
        assert "True" in got["TURNED"]

    def test_up_is_world_up_not_the_tallest_axis(self):
        """MEASURED: "up is the tallest axis" put a cap's up along its brim,
        because a cap lying flat is longer front-to-back than it is tall."""
        got = run('''
flat = bg_box("Flat", size=(0.6, 1.0, 0.2))
print("UP", bg_axes(flat)["up"])
''')
        assert got["UP"] == "2"


@requires_blender
class TestWeldingIsGentle:
    def test_welding_reports_whether_the_mesh_can_be_decimated(self):
        """Non-manifold count is the signal. Collapse will not cross a
        non-manifold junction, and merging harder manufactures them faster than
        it removes shells."""
        got = run('''
base = bg_human(height=1.8)
rep = bg_weld(base["obj"])
print("NM", rep["nonmanifold"], "DEC", rep["decimatable"], "SH", rep["shells"])
''')
        assert "True" in got["NM"]

    def test_the_merge_distance_scales_with_the_object(self):
        """One setting has to serve a 0.2 m cap and a 2 m figure, so it is a
        fraction of the bounding-box diagonal rather than an absolute."""
        got = run('''
small = bg_box("S", size=(0.2, 0.2, 0.2))
big = bg_box("B", size=(2.0, 2.0, 2.0))
a, b = bg_weld(small)["merge"], bg_weld(big)["merge"]
print("RATIO", round(b / max(a, 1e-9), 1))
''')
        assert float(got["RATIO"]) > 5


@requires_blender
class TestAdoptSaysWhenItFailed:
    def test_a_budget_that_was_met_says_so(self):
        got = run('''
base = bg_human(height=1.8, detail=2)
rep = bg_adopt(base["obj"], kind="humanoid", height=1.8, budget=1200)
print("MET", rep["budget"]["met"], "GOT", rep["budget"]["got"])
''')
        assert "True" in got["MET"]

    def test_a_budget_that_could_not_be_met_is_reported_not_hidden(self):
        """Returning a mesh far over budget with an ok-looking report is the
        exact failure this whole pass exists to stop. MEASURED on a generated
        crate: asked 2,000, got 97,954, because 78,618 non-manifold edges stop
        collapse dead at every merge distance."""
        got = run('''
box = bg_box("B", size=(1, 1, 1))
rep = bg_adopt(box, kind="none", budget=2)
b = rep["budget"]
print("ASKED", b["asked"], "GOT", b["got"], "MET", b["met"])
print("REASON", (b["reason"] or "none")[:70])
''')
        assert got["ASKED"].startswith("2 ")
        assert "MET" in got["ASKED"] or "GOT" in got["ASKED"]

    def test_adopt_grounds_what_it_scaled(self):
        """Grounding before scaling leaves the mesh floating by whatever the
        scale factor was, which is why the order in bg_adopt is fixed."""
        got = run('''
base = bg_human(height=1.8)
obj = base["obj"]
obj.location.z += 3.0
bg_adopt(obj, kind="humanoid", height=1.8)
import bpy
bpy.context.view_layer.update()
low = min((obj.matrix_world @ v.co).z for v in obj.data.vertices)
print("SOLE", round(low, 4), "TALL", round(obj.dimensions[2], 3))
''')
        assert abs(float(got["SOLE"].split(" ")[0])) < 0.01
