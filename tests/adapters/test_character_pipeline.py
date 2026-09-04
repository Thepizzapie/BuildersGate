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

import inspect

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
            return {"ok": True, "path": out, "usd": 0.06}

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
        ok=True, usd=0.30, warnings=[]))
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
        assert got["usd"] == 0.30
        assert stub["plate"] is None and stub["mesh"] is None

    def test_an_unpriced_stage_makes_the_total_unknown_not_partial(self):
        """A number reads as the bill. One that silently omits an unpriced
        stage is worse than admitting the total is unknown."""
        assert blender._sum_usd([{"usd": 0.06}, {"usd": None}]) is None
        assert blender._sum_usd([{"usd": 0.06}, {"usd": 0.30}]) == 0.36


class TestTheSubjectReference:
    """The pose template says which STANCE, never which CHARACTER.

    Measured on catnip-fiend: the owner was rebuilt twice off plates
    conditioned on the template alone, and no two plates were the same figure.
    `character_generate` had no way to pass the pinned concept at all, so
    "generate him again, facing away" was not expressible.
    """

    def test_the_subject_ref_leads_and_the_template_follows(self, stub, tmp_path):
        ref = tmp_path / "owner_concept.png"
        ref.write_bytes(b"")
        blender.character("the owner", tmp_path, backend="krea",
                          ref_images=[str(ref)])
        refs = list(stub["plate"]["ref_paths"])
        assert refs[0] == str(ref), "the subject must lead — edit models read " \
                                    "the first image as the thing to edit"
        assert len(refs) == 2, "the stance template must still be carried"

    def test_the_subject_ref_is_held_harder_than_the_stance(self, stub, tmp_path):
        ref = tmp_path / "owner_concept.png"
        ref.write_bytes(b"")
        blender.character("the owner", tmp_path, backend="krea",
                          ref_images=[str(ref)], ref_strength=0.72)
        assert stub["plate"]["ref_strength"] == 0.72

    def test_without_one_the_call_is_exactly_what_it_always_was(self, stub,
                                                               tmp_path):
        """No subject ref: the template alone, at the measured 0.45 that made
        the skeleton fit at limbs=1.0 with no compensation."""
        blender.character("a pirate woman", tmp_path, backend="krea")
        assert len(stub["plate"]["ref_paths"]) == 1
        assert stub["plate"]["ref_strength"] == 0.45

    def test_blank_entries_are_dropped_rather_than_sent(self, stub, tmp_path):
        blender.character("the owner", tmp_path, backend="krea",
                          ref_images=["", "   "])
        assert len(stub["plate"]["ref_paths"]) == 1
        assert stub["plate"]["ref_strength"] == 0.45


class TestThePlateCallFitsTheProviderItActuallyGets:
    """The stub above takes **kw, and that is exactly how this shipped broken.

    character() called the plate provider with the UNION of two signatures -
    krea.generate takes ref_strength and root, imagegen.generate (gpt-image)
    takes neither - so every real run raised TypeError at stage one, before a
    plate or a mesh was made, on both the hosted and the local mesh backend.
    Fifteen tests were green throughout, because a stub that accepts anything
    answers a different question than "will the call go through".

    These fakes carry the REAL functions' signatures by binding against them,
    so they keep failing for the right reason after either adapter changes its
    parameters. Nothing here calls a provider.
    """

    @staticmethod
    def _faithful(real, record):
        def _fake(prompt, out, **kw):
            inspect.signature(real).bind(prompt, out, **kw)
            record.update({"prompt": prompt, **kw})
            return {"ok": True, "path": out, "usd": 0.06}
        # The fake WEARS the real signature, so a caller that introspects
        # before calling sees what it would see in production. Without this
        # the fake reads as **kw-takes-anything and the test proves nothing
        # about the call the shipped code makes.
        _fake.__signature__ = inspect.signature(real)
        return _fake

    @pytest.mark.parametrize("module_name", ["krea", "imagegen"])
    def test_every_plate_provider_accepts_the_call_character_makes(
            self, stub, monkeypatch, tmp_path, module_name):
        import importlib

        module = importlib.import_module(f"bgate_adapters.{module_name}")
        seen: dict = {}
        monkeypatch.setattr(
            blender, "_plate_provider",
            lambda name, root=None: type(
                "P", (), {"generate": staticmethod(
                    self._faithful(module.generate, seen))}))
        ref = tmp_path / "owner_concept.png"
        ref.write_bytes(b"")
        got = blender.character("the owner", tmp_path, backend="krea",
                                ref_images=[str(ref)], ref_strength=0.6)
        assert got["ok"] is True, got.get("error")
        assert seen["ref_paths"][0] == str(ref), (
            "the subject reference must reach every provider - it is the one "
            "parameter both of them take")

    def test_a_knob_the_provider_cannot_honour_is_named_not_assumed(
            self, stub, monkeypatch, tmp_path):
        # gpt-image holds a reference by delegating to edit(), which has no
        # strength. Dropping it is right; dropping it silently is how a run
        # reports "held at 0.6" having held it at whatever edit() does.
        from bgate_adapters import imagegen

        seen: dict = {}
        monkeypatch.setattr(
            blender, "_plate_provider",
            lambda name, root=None: type(
                "P", (), {"generate": staticmethod(
                    self._faithful(imagegen.generate, seen))}))
        ref = tmp_path / "owner_concept.png"
        ref.write_bytes(b"")
        got = blender.character("the owner", tmp_path, backend="krea",
                                ref_images=[str(ref)], ref_strength=0.6)
        plate = [s for s in got["steps"] if s["step"] == "plate"][0]
        assert "ref_strength" in plate["unsupported"]
        assert "ref_strength" not in seen


class TestThePlateGoesToTheProviderThatWasNamed:
    """provider="kie" painting at openai is how a funded project went dark.

    _plate_provider knew two names and sent everything else to gpt-image, so
    on a project whose character provider IS kie — with 3,446 credits and a
    drained openai account — the plate stage hit openai on provider="kie",
    provider="" and no provider at all, and reported a 429 for an account
    nobody had chosen. Three dead runs read as "the pipeline is closed".
    """

    def test_a_named_provider_is_not_silently_replaced_by_gpt_image(self):
        from bgate_adapters import imagegen, krea

        assert blender._plate_provider("krea") is krea
        assert blender._plate_provider("openai") is imagegen
        for name in ("kie", "local"):
            assert blender._plate_provider(name) is not imagegen, (
                f"provider={name!r} still falls through to gpt-image, so the "
                "call is billed to an account the caller did not name")

    def test_the_gateway_carries_the_refs_and_keys_nothing(self, monkeypatch,
                                                           tmp_path):
        # keyed=False on purpose: character() keys its own plate with despill
        # off, and the sprite contract was tried on this path and failed twice.
        from bgate_core.art import chroma

        seen: dict = {}

        def _fake(prompt, out_path, **kw):
            seen.update({"prompt": prompt, "out": str(out_path), **kw})
            return {"ok": True, "path": str(out_path), "usd": 0.04}

        monkeypatch.setattr(chroma, "generate", _fake)
        ref = tmp_path / "owner_concept.png"
        ref.write_bytes(b"")
        got = blender._plate_provider("kie").generate(
            "the owner", str(tmp_path / "plate.png"), size="1024x1536",
            task_kind="character", ref_paths=[str(ref)], ref_strength=0.6)
        assert got["ok"] is True
        assert seen["provider"] == "kie"
        assert seen["keyed"] is False
        assert seen["ref_paths"] == [str(ref)]
        assert seen["ref_strength"] == 0.6


class TestTheBudgetFloorIsNamedBeforeTheSpend:
    """8,000 triangles is a number this path can meet and must not be given.

    catnip-fiend's owner, at budget=8000: 13,842 triangles (the collapse could
    not reach the number), 42% non-manifold, face and feet folded into planes -
    off a plate that was a good likeness of the pinned ref. The budget came
    from an acceptance criterion written for a PROCEDURAL mesh and copied onto
    a generated one. rig()'s own note already said 8k shatters a character;
    nothing said it where the caller was choosing.
    """

    def test_the_quote_warns_before_a_shredding_budget_is_paid_for(self,
                                                                   tmp_path):
        got = blender.character("the owner", tmp_path, backend="krea",
                                budget=8000, dry_run=True)
        assert got["ok"] is True and got["stage"] == "quote"
        assert got.get("warnings"), (
            "the warning has to ride the DRY RUN - after the spend it is a "
            "post-mortem, not a decision")
        assert str(blender.HUMANOID_TRI_FLOOR) in got["warnings"][0]

    def test_a_sane_budget_is_not_nagged(self, tmp_path):
        got = blender.character("the owner", tmp_path, backend="krea",
                                budget=45000, dry_run=True)
        assert not got.get("warnings")
