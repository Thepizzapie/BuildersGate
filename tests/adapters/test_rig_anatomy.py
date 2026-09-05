"""The rig report's ANATOMY verdict, on figures whose anatomy is known.

Two bg_human builds of the same 6-head, 1.75 m figure: a slim one whose crotch
can be measured, and a fat one whose thighs touch so it cannot. The slim rig
must report a MEASURED trunk and say how far the height-only template would
have placed it; the fat rig must FAIL - `ok` false, "TRUNK ASSUMED" - because
an assumed trunk is exactly how a character shipped with thigh bones 37 cm
above its crotch while every gate said green. Real Blender, two short runs.
"""

from __future__ import annotations

import pytest

from bgate_adapters import blender

needs_blender = pytest.mark.skipif(not blender.available().get("available"),
                                   reason="Blender not installed")


def _known(tmp_path, build: float) -> str:
    glb = tmp_path / f"known_{build}.glb"
    script = blender._rig_script().split("PAY = json.loads(")[0] + f'''
for o in list(bpy.context.scene.objects):
    bpy.data.objects.remove(o, do_unlink=True)
bg_human(height=1.75, heads=6.0, build={build}, rig=False, name="Known", pose="a", detail=3, finish=True)
'''
    got = blender.run_script(script, export_glb=str(glb), timeout=600, record=False)
    assert got.get("ok"), got.get("error")
    return str(glb)


@needs_blender
@pytest.mark.slow
def test_a_measured_trunk_is_reported_with_the_templates_disagreement(tmp_path):
    report = blender.rig(_known(tmp_path, 0.6), str(tmp_path / "slim.glb"), height=1.75, timeout=900)
    assert report.get("ok") is True and report.get("rigged") is True, report.get("error")
    anatomy = report["anatomy"]
    assert anatomy["trunk_measured"] is True and anatomy["ok"] is True
    # A 6-head figure hung by a 7.5-head template: the shift is real but small.
    assert anatomy["template_worst_bone"] in ("Hips", "Spine", "Chest", "UpperChest", "Neck", "Head")
    assert 0.0 < anatomy["template_worst_shift"] < anatomy["bound_body_heights"]
    assert anatomy["template_disagreed"] is False
    trunk = report["placed"]["trunk"]
    assert set(trunk["template_heights"]) == set(trunk["template_shift"])
    # Measured landmarks sit in anatomical order.
    assert anatomy["crotch"] < anatomy["shoulder_line"] < anatomy["neck"]


@needs_blender
@pytest.mark.slow
def test_an_unmeasurable_trunk_fails_the_rig_loudly(tmp_path):
    report = blender.rig(_known(tmp_path, 1.0), str(tmp_path / "fat.glb"), height=1.75, timeout=900)
    assert report.get("rigged") is True          # the bind itself worked...
    assert report.get("ok") is False             # ...and the rig is still refused
    assert "TRUNK ASSUMED" in report["error"]
    assert report["anatomy"]["trunk_measured"] is False and report["anatomy"]["ok"] is False
    assert "crotch" in report["anatomy"]["why"]
