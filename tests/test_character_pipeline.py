"""The one call that runs plate -> mesh -> rig -> engine.

Every stage of this was already reachable and a caller still had to know:
condition the plate on the template's STANCE but not on its mannequin; do not
inherit the project's 2D art bible into a reconstruction plate; key the plate or
the backdrop arrives as geometry; that a bind reports success having weighted
nothing. Those are not judgement calls, they are the same five steps in the same
order, which is what a tool is for.

Nothing here spends money or spawns Blender. What is under test is the ORDER,
the gating, and the refusals — the parts that decide whether a caller is billed
twice for a mistake the first stage could have caught.
"""
from __future__ import annotations

import pytest

from bgate_adapters import blender


@pytest.fixture()
def stub(monkeypatch, tmp_path):
    """Stand in for every paid or slow stage, recording what was called."""
    calls: dict = {"plate": None, "mesh": None, "rig": None, "deliver": None}

    class _Provider:
        @staticmethod
        def generate(prompt, out, **kw):
            calls["plate"] = {"prompt": prompt, **kw}
            return {"ok": True, "path": out, "estimated_usd": 0.06}

    monkeypatch.setattr(blender, "_plate_provider",
                        lambda name, root=None: _Provider)
    monkeypatch.setattr(blender, "_key_plate", lambda src, dst: (0.18, ""))
    monkeypatch.setattr(blender, "rig", lambda *a, **k: dict(
        calls.__setitem__("rig", k) or {},
        rigged=True, bound_with="ARMATURE_AUTO", unweighted=0,
        adopt={"quality": {"ok": True}}))

    import bgate_adapters.imageto3d as i3d
    monkeypatch.setattr(i3d, "choose", lambda root=None: {"backend": "krea"})
    monkeypatch.setattr(i3d, "price_for", lambda b, **k: 0.30)
    monkeypatch.setattr(i3d, "generate", lambda *a, **k: dict(
        calls.__setitem__("mesh", k) or {},
        ok=True, estimated_usd=0.30, warnings=[]))
    return calls


class TestOrderAndGating:
    def test_it_runs_plate_then_mesh_then_rig(self, stub, tmp_path):
        got = blender.character("a pirate woman", tmp_path, backend="krea")
        assert got["ok"] is True
        assert [s["step"] for s in got["steps"]] == ["plate", "mesh", "rig"]

    def test_a_failed_plate_costs_only_the_plate(self, stub, monkeypatch, tmp_path):
        """The mesh is the expensive half. A plate that cannot be keyed must
        stop the run — an unkeyed plate measured 605 s and 21% non-manifold,
        refused by the quality gate downstream, against 216 s and 16% keyed."""
        monkeypatch.setattr(blender, "_key_plate",
                            lambda src, dst: (0.0, "background is not flat"))
        got = blender.character("a pirate woman", tmp_path, backend="krea")
        assert got["ok"] is False
        assert got["stage"] == "plate"
        assert stub["mesh"] is None, "it paid for a mesh after the plate failed"

    def test_a_bind_that_weighted_nothing_is_not_ok(self, stub, monkeypatch,
                                                    tmp_path):
        """rigged=False is a refusal, not a warning. Measured: 64,878 of 64,878
        vertices carrying no weight with every other check green."""
        monkeypatch.setattr(blender, "rig", lambda *a, **k: {
            "rigged": False, "unweighted": 64878, "bound_with": "ARMATURE_AUTO",
            "reason": "the bind weighted nothing", "adopt": {"quality": {"ok": True}}})
        got = blender.character("a pirate woman", tmp_path, backend="krea")
        assert got["ok"] is False
        assert got["stage"] == "rig"
        assert "weighted nothing" in got["error"]

    def test_nothing_is_written_into_a_game_unless_asked(self, stub, tmp_path):
        got = blender.character("a pirate woman", tmp_path, backend="krea")
        assert "scene" not in got
        assert stub["deliver"] is None


class TestItMatchesTheRunThatWorked:
    """The parameters are not defaults, they are the ones that produced a
    character: skeleton fitted at limbs=1.0 with no compensation, 0 bones
    outside the mesh, 0 unweighted vertices, delivered into Godot and animated.
    Pinned so a later tidy-up cannot quietly change them back."""

    def test_the_plate_is_conditioned_on_the_pose_reference(self, stub, tmp_path):
        """The reference carries the STANCE — arms out, feet flat, symmetrical,
        framed head to feet. That is what let the template skeleton fit with no
        compensation; without it the generator picks a stance and the skeleton
        has to be bent to match."""
        blender.character("a pirate woman", tmp_path, backend="krea")
        assert stub["plate"]["ref_paths"], stub["plate"]
        assert stub["plate"]["ref_strength"] == 0.45
        assert "T-pose" in stub["plate"]["prompt"]

    def test_the_plate_is_tall(self, stub, tmp_path):
        """1024x1536. A full body on a square canvas leaves the head at ~120 px
        and the face is then invented rather than reconstructed."""
        blender.character("a pirate woman", tmp_path, backend="krea")
        assert stub["plate"]["size"] == "1024x1536"

    def test_the_project_root_reaches_the_paid_stages(self, stub, tmp_path):
        """Keys and the spend ledger come from the project the call named."""
        blender.character("a pirate woman", tmp_path, backend="krea",
                          root="C:/some/project")
        assert stub["plate"]["root"] == "C:/some/project"
        assert stub["mesh"]["root"] == "C:/some/project"

    def test_the_mesh_is_generated_at_1024(self, stub, tmp_path):
        """What was run. 1536 was never tried on a 12 GB card, and TRELLIS is
        where VRAM gets tight."""
        blender.character("a pirate woman", tmp_path, backend="krea")
        assert stub["mesh"]["resolution"] == "1024"

    def test_the_rig_uses_the_template_stance(self, stub, tmp_path):
        """pose="t" — the plate is a T-pose, so the skeleton must be too. An
        A-pose skeleton inside a T-pose body is what put the hand bones 14 cm
        outside the mesh."""
        blender.character("a pirate woman", tmp_path, backend="krea")
        assert stub["rig"]["pose"] == "t"
        assert stub["rig"]["kind"] == "humanoid"

    def test_dry_run_quotes_and_spends_nothing(self, stub, tmp_path):
        """A tool that bills on the first call is one nobody trusts twice."""
        got = blender.character("a pirate woman", tmp_path, backend="krea",
                                dry_run=True)
        assert got["ok"] is True and got["stage"] == "quote"
        assert got["estimated_usd"] == 0.30
        assert stub["plate"] is None and stub["mesh"] is None

    def test_an_unpriced_stage_makes_the_total_unknown_not_partial(self):
        """A number reads as the bill. One that silently omits an unpriced
        stage is worse than admitting the total is unknown."""
        assert blender._sum_usd([{"usd": 0.06}, {"usd": None}]) is None
        assert blender._sum_usd([{"usd": 0.06}, {"usd": 0.30}]) == 0.36
