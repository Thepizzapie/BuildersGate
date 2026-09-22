"""Generating the parts of a cutout character: the plan, the money, the ruler,
and the provenance that says which reference every part came from.

Written from EXIT 67, where frame sheets shipped with another character's
frames in them and nothing on a frame could say so. Every test here either
makes a refusal fire before money moves, or measures something a fake
generator wrote to disk and the pipeline then read back.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bgate_core.three_d import cutout, cutoutkit
from bgate_core.three_d.cutoutkit import KitError
from bgate_mcp import server


def _png(path: Path, size, colour=(200, 120, 90, 255), pad=0) -> Path:
    """A solid part on an optional transparent margin."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGBA", (size[0] + 2 * pad, size[1] + 2 * pad), (0, 0, 0, 0))
    canvas.paste(Image.new("RGBA", size, colour), (pad, pad))
    canvas.save(path)
    return path


class FakeGenerator:
    """Writes a PNG sized from a table, records every call, can fail on cue."""

    def __init__(self, ref_height: int, heights: dict | None = None,
                 fail: set | None = None):
        self.ref_height = ref_height
        self.heights = heights or {}
        self.fail = fail or set()
        self.calls: list[dict] = []

    def __call__(self, prompt, out_path, **kw):
        slot = Path(out_path).stem
        self.calls.append({"slot": slot, "prompt": prompt, **kw})
        if slot == "_sheet":                      # default mode: one sheet
            return FakeSheetGenerator(fail=bool(self.fail), heights=self.heights)(
                prompt, out_path, **kw)
        if slot in self.fail:
            return {"ok": False, "error": f"provider said no to {slot}"}
        spec = cutout.BIPED_V1["parts"][slot]
        frac = self.heights.get(slot, spec["height"])
        h = max(4, int(self.ref_height * frac))
        _png(Path(out_path), (max(4, h // 2), h), pad=40)   # a big empty canvas
        return {"ok": True, "path": str(out_path), "cost_usd": 0.05}


@pytest.fixture
def reference(tmp_path):
    return _png(tmp_path / "ref.png", (90, 200), pad=20)   # figure 200 px tall


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def test_the_plan_is_the_near_side_body_and_no_equipment():
    slots = [p["slot"] for p in cutoutkit.plan()]
    assert slots == ["head", "torso", "hip", "arm_near", "forearm_near",
                     "hand_near", "thigh_near", "shin_near", "foot_near"]
    assert "hat" not in slots and "weapon" not in slots
    assert "arm_far" not in slots


def test_a_subset_is_honoured_and_an_unknown_slot_refused():
    assert [p["slot"] for p in cutoutkit.plan(parts=["head"])] == ["head"]
    with pytest.raises(KitError):
        cutoutkit.plan(parts=["tentacle"])


def test_a_reused_slot_is_refused_and_names_its_source():
    with pytest.raises(KitError) as exc:
        cutoutkit.plan(parts=["arm_far"])
    assert "arm_near" in str(exc.value)


def test_estimate_prices_before_buying():
    got = cutoutkit.estimate()
    assert got["calls"] == 9 and got["cost_usd"] > 0


def test_the_prompt_isolates_the_part_and_carries_the_profile():
    part = cutoutkit.plan(parts=["forearm_near"])[0]
    text = cutoutkit.prompt_for(part, profile={"traits": "a green gnome",
                                               "style": "thick ink",
                                               "negative": "no gradients"})
    assert "forearm" in text and "nothing else in frame" in text
    assert "side view" in text.lower()
    assert "a green gnome" in text and "thick ink" in text and "no gradients" in text


# ---------------------------------------------------------------------------
# The ruler and the trim
# ---------------------------------------------------------------------------

def test_trim_cuts_the_canvas_to_the_part(tmp_path):
    p = _png(tmp_path / "torso.png", (40, 60), pad=100)
    assert cutoutkit.trim_alpha(p) == (42, 62)


def test_reference_height_is_the_figure_not_the_canvas(reference):
    assert cutoutkit.reference_height(reference) == 200


def test_scale_flag_fires_outside_the_band():
    part = cutoutkit.plan(parts=["head"])[0]            # 0.22 of the figure
    assert cutoutkit.scale_flag(part, (30, 44), 200) is None
    flag = cutoutkit.scale_flag(part, (30, 100), 200)   # a head the size of a torso
    assert flag and flag["slot"] == "head" and flag["ratio"] > 2


# ---------------------------------------------------------------------------
# The batch
# ---------------------------------------------------------------------------

def test_a_kit_over_the_ceiling_is_refused_before_any_call(tmp_path, reference):
    gen = FakeGenerator(200)
    with pytest.raises(KitError):
        cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                               out_dir=tmp_path / "parts", provider="fake",
                               max_paid_calls=3, generate=gen, mode="parts")
    assert gen.calls == []


def test_every_part_carries_the_reference_hash(tmp_path, reference):
    gen = FakeGenerator(200)
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen, mode="parts")
    assert got["ok"] is True, got
    assert got["calls"] == 9 and got["cost_usd"] == pytest.approx(0.45)
    ref_hash = cutoutkit.file_hash(reference)
    assert got["reference_hash"] == ref_hash
    assert {e["anchor_hash"] for e in got["parts"].values()} == {ref_hash}
    # Every call conditioned on the reference and went through the keyed kind.
    assert all(c["ref_paths"] == [str(reference)] for c in gen.calls)
    assert all(c["task_kind"] == "part" for c in gen.calls)
    # Trimmed: the 40 px canvas margin is gone.
    from PIL import Image
    with Image.open(got["parts"]["head"]["texture"]) as img:
        assert img.height == pytest.approx(44, abs=3)


def test_a_wrong_sized_part_is_flagged_not_rescaled(tmp_path, reference):
    gen = FakeGenerator(200, heights={"head": 0.6})
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen, mode="parts")
    assert got["ok"] is False
    assert [f["slot"] for f in got["flags"]] == ["head"]
    from PIL import Image
    with Image.open(got["parts"]["head"]["texture"]) as img:
        assert img.height > 100          # still the size it came back at


def test_two_consecutive_failures_stop_the_batch(tmp_path, reference):
    gen = FakeGenerator(200, fail={"torso", "hip"})
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen, mode="parts")
    assert got["stopped"]
    assert [f["slot"] for f in got["failed"]] == ["torso", "hip"]
    assert got["calls"] == 3              # head, torso, hip - then stop
    assert "head" in got["parts"] and "arm_near" not in got["parts"]


def test_one_failure_between_successes_does_not_stop(tmp_path, reference):
    gen = FakeGenerator(200, fail={"torso"})
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen, mode="parts")
    assert not got["stopped"] and got["calls"] == 9
    assert [f["slot"] for f in got["failed"]] == ["torso"]


def test_fill_reuse_inherits_provenance(tmp_path, reference):
    gen = FakeGenerator(200)
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen, mode="parts")
    skin = cutoutkit.fill_reuse(got["parts"])
    assert skin["hand_far"]["reuse_of"] == "hand_near"
    assert skin["hand_far"]["anchor_hash"] == got["reference_hash"]
    doc = cutout.empty("hero")
    doc["skin"] = skin
    doc["reference_hash"] = got["reference_hash"]
    assert cutout.status(cutout.normalise(doc))["complete"] is False   # hat, weapon
    assert cutout.status(cutout.normalise(doc))["problems"] == []


# ---------------------------------------------------------------------------
# Provenance the document enforces
# ---------------------------------------------------------------------------

def test_status_flags_a_part_from_another_reference(tmp_path, reference):
    gen = FakeGenerator(200)
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen, mode="parts")
    doc = cutout.empty("hero")
    doc["skin"] = cutoutkit.fill_reuse(got["parts"])
    doc["reference_hash"] = got["reference_hash"]
    doc["skin"]["head"]["anchor_hash"] = "deadbeefdeadbeef"   # the gator's head
    problems = cutout.status(cutout.normalise(doc))["problems"]
    assert [p for p in problems if p["kind"] == "stale_reference"
            and p["slot"] == "head"]


def test_status_flags_a_reference_that_moved_under_the_kit(tmp_path, reference):
    from bgate_core.art import refs
    from bgate_core.store import project
    project.init(tmp_path, "Probe", pitch="x")
    refs.pin(tmp_path, "hero_ref", str(reference), kind="character")
    gen = FakeGenerator(200)
    got = cutoutkit.generate_kit(tmp_path, "hero", refs.resolve(tmp_path, "hero_ref"),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen, mode="parts")
    doc = cutout.empty("hero")
    doc["skin"] = cutoutkit.fill_reuse(got["parts"])
    doc["reference"] = "hero_ref"
    doc["reference_hash"] = got["reference_hash"]
    assert cutout.status(cutout.normalise(doc), root=tmp_path)["problems"] == []
    # Re-pin a different image under the same name.
    other = _png(tmp_path / "other.png", (90, 200), colour=(20, 200, 20, 255))
    refs.pin(tmp_path, "hero_ref", str(other), kind="character")
    kinds = {p["kind"] for p in
             cutout.status(cutout.normalise(doc), root=tmp_path)["problems"]}
    assert "reference_moved" in kinds


# ---------------------------------------------------------------------------
# The MCP surface, end to end with a fake provider
# ---------------------------------------------------------------------------

async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.fixture()
def wired(root, monkeypatch, reference):
    from bgate_core.art import refs, chroma
    monkeypatch.setenv("BGATE_ROOT", str(root))
    monkeypatch.delenv("BGATE_SEAT", raising=False)
    refs.pin(root, "hero_ref", str(reference), kind="character")
    refs.profile_set(root, "hero_ref", traits="a green gnome", style="ink",
                     negative="gradients")
    gen = FakeGenerator(200)
    monkeypatch.setattr(chroma, "generate", gen)
    monkeypatch.setattr(server, "_provider_gate", lambda *a, **k: None)
    monkeypatch.setattr(server._providers, "provider_for",
                        lambda *a, **k: "fake")
    return root, gen


@pytest.mark.anyio
async def test_kit_generate_assembles_and_emits_with_provenance(wired):
    root, gen = wired
    got = await call("cutout_kit_generate", name="hero", reference="hero_ref")
    assert got["ok"] is True, got
    assert got["generation"]["calls"] == 1              # one sheet, nine parts
    assert sorted(got["generation"]["generated"]) == sorted(
        p["slot"] for p in cutoutkit.plan())
    # The profile rode into every prompt.
    assert all("a green gnome" in c["prompt"] for c in gen.calls)
    scene = Path(got["scene"])
    assert scene.is_file() and (root / "game" / "assets" / "characters" /
                                "hero" / "hero.cutout.json").is_file()
    doc = cutout.load(root / "game" / "assets" / "characters" / "hero" /
                      "hero.cutout.json")
    assert doc["reference"] == "hero_ref"
    assert doc["skin"]["hand_far"]["reuse_of"] == "hand_near"
    assert got["sprites"] == 15
    assert "aim" in got["clips"]
    status = await call("cutout_status", name="hero")
    assert status["problems"] == [] and set(status["missing"]) == {"hat", "weapon"}


@pytest.mark.anyio
async def test_part_rerun_redraws_one_part_and_keeps_the_rest(wired):
    root, gen = wired
    await call("cutout_kit_generate", name="hero", reference="hero_ref")
    before = cutout.load(root / "game" / "assets" / "characters" / "hero" /
                         "hero.cutout.json")
    gen.calls.clear()
    got = await call("cutout_part_rerun", name="hero", slot="forearm_near",
                     note="bare skin, not sleeved")
    assert got["ok"] is True, got
    # One sheet with one cell, and the note rode into it.
    assert [c["slot"] for c in gen.calls] == ["_sheet"]
    assert "bare skin" in gen.calls[0]["prompt"]
    assert "FOREARM_NEAR" in gen.calls[0]["prompt"]
    after = cutout.load(root / "game" / "assets" / "characters" / "hero" /
                        "hero.cutout.json")
    assert after["skin"]["head"] == before["skin"]["head"]
    assert after["skin"]["forearm_far"]["reuse_of"] == "forearm_near"


@pytest.mark.anyio
async def test_part_rerun_refuses_a_kit_with_no_reference(wired):
    root, gen = wired
    part = _png(root / "loose.png", (20, 40))
    assembled = await call("cutout_assemble", name="loose", parts={"head": str(part)})
    assert assembled["ok"] is True, assembled
    got = await call("cutout_part_rerun", name="loose", slot="head")
    assert got["ok"] is False and "names no reference" in got["error"]
    assert gen.calls == []


@pytest.mark.anyio
async def test_kit_generate_refuses_over_the_ceiling_before_spending(wired):
    root, gen = wired
    got = await call("cutout_kit_generate", name="hero", reference="hero_ref",
                     max_paid_calls=2, mode="parts")
    assert "error" in got and "max_paid_calls" in got["error"]
    assert gen.calls == []


@pytest.mark.anyio
async def test_templates_advertise_the_generator(wired):
    got = await call("cutout_templates")
    assert "not_built_yet" not in got
    biped = got["templates"][0]
    assert biped["equipment"] == ["hat", "weapon"]
    assert "hand_near" in biped["parts_to_generate"]
    assert "aim" in biped["clips"]



# ---------------------------------------------------------------------------
# The sheet: one call, every part, one style, one scale
# ---------------------------------------------------------------------------

class FakeSheetGenerator:
    """Redraws the layout the way a well-behaved model would: reads the
    layout json from ref_paths, paints one blob per cell at the template's
    height against the figure, on the key colour. Records the call."""

    def __init__(self, blank: set | None = None, fail: bool = False,
                 heights: dict | None = None):
        self.blank = blank or set()
        self.fail = fail
        self.heights = heights or {}
        self.calls: list[dict] = []

    def __call__(self, prompt, out_path, **kw):
        from PIL import Image
        self.calls.append({"prompt": prompt, "out": out_path, **kw})
        if self.fail:
            return {"ok": False, "error": "provider said no"}
        layout = json.loads(Path(kw["ref_paths"][1]).with_suffix(".json")
                            .read_text(encoding="utf-8"))
        rgb = tuple(layout["chroma"])
        w, h = layout["size"]
        img = Image.new("RGBA", (w, h), (*rgb, 255))
        fig_h = layout["figure_height_px"]
        for slot, (x0, y0, x1, y1) in layout["cells"].items():
            if slot in self.blank:
                continue
            frac = self.heights.get(slot, cutout.BIPED_V1["parts"][slot]["height"])
            ph = max(6, int(fig_h * frac))
            pw = max(6, ph // 2)
            cx = (x0 + x1) // 2
            img.paste(Image.new("RGBA", (pw, ph), (120, 80, 60, 255)),
                      (cx - pw // 2, y1 - 8 - ph))
        img.save(out_path)
        return {"ok": True, "path": str(out_path), "cost_usd": 0.08}


def test_the_sheet_is_one_call_and_every_part_lands(tmp_path, reference):
    gen = FakeSheetGenerator()
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen)
    assert got["mode"] == "sheet" and got["calls"] == 1
    assert len(gen.calls) == 1
    assert sorted(got["parts"]) == sorted(p["slot"] for p in cutoutkit.plan())
    assert got["flags"] == [] and got["failed"] == [] and got["ok"]
    assert Path(got["sheet"]).is_file()
    # the layout the model was shown, and the figure inside it
    assert gen.calls[0]["keyed"] is False
    assert Path(gen.calls[0]["ref_paths"][1]).name == "_sheet_layout.png"
    assert "PARTS SHEET" in gen.calls[0]["prompt"]
    for entry in got["parts"].values():
        assert Path(entry["texture"]).is_file()
        assert entry["anchor_hash"] == got["reference_hash"]


def test_an_empty_cell_is_a_failed_slot_not_a_blank_texture(tmp_path, reference):
    gen = FakeSheetGenerator(blank={"hand_near"})
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen)
    assert [f["slot"] for f in got["failed"]] == ["hand_near"]
    assert "hand_near" not in got["parts"]
    assert not (tmp_path / "parts" / "hand_near.png").exists()
    assert got["ok"] is False


def test_a_part_off_scale_on_the_sheet_is_flagged(tmp_path, reference):
    gen = FakeSheetGenerator(heights={"head": 0.4})
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen)
    assert [f["slot"] for f in got["flags"]] == ["head"]


def test_a_failed_sheet_fails_every_slot_and_stops(tmp_path, reference):
    gen = FakeSheetGenerator(fail=True)
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen)
    assert got["stopped"] and len(got["failed"]) == len(cutoutkit.plan())
    assert got["parts"] == {} and got["calls"] == 1


def test_the_old_per_part_loop_is_still_there_by_name(tmp_path, reference):
    gen = FakeGenerator(200)
    got = cutoutkit.generate_kit(tmp_path, "hero", str(reference),
                                 out_dir=tmp_path / "parts", provider="fake",
                                 generate=gen, mode="parts")
    assert got["mode"] == "parts" and got["calls"] == len(cutoutkit.plan())
