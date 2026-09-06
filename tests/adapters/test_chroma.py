"""The keyable-background contract — the thing that manufactures alpha.

These tests exist because BOTH providers were measured failing to return
transparency on the same 4-frame character sheet: gpt-image-1 answered
background="transparent" with a brown gradient, and no Krea model has a
transparency parameter at all. Everything below pins the replacement: pick a key
colour the art does not use, demand a flat backdrop of it, key it out, and PROVE
the cut is clean before calling it a sprite.

No network. The providers are stubbed at the adapter boundary — what is under
test is the contract, not the vendors.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw

from bgate_core.art import chroma


# ---------------------------------------------------------------------------
# helpers — synthetic generations, the good and the broken
# ---------------------------------------------------------------------------

def _flat_backdrop(path, bg=(255, 0, 255), fg=(40, 90, 200), size=(240, 320)):
    """What an obedient model returns: figure on a perfectly uniform backdrop."""
    img = Image.new("RGBA", size, bg + (255,))
    ImageDraw.Draw(img).ellipse((60, 60, size[0] - 60, size[1] - 60),
                                fill=fg + (255,))
    img.save(path)
    return str(path)


def _gradient_backdrop(path, size=(240, 320)):
    """What a disobedient model returns: the exact failure measured on gpt-image
    — a gradient where a flat key colour was demanded. Keying this leaves the
    frame border opaque, which is what the audit must catch."""
    img = Image.new("RGBA", size, (0, 0, 0, 255))
    px = img.load()
    for y in range(size[1]):
        t = y / (size[1] - 1)
        for x in range(size[0]):
            px[x, y] = (int(255 * (1 - t)) or 1, int(30 * t), int(255 * (1 - t)) or 1, 255)
    ImageDraw.Draw(img).ellipse((60, 60, size[0] - 60, size[1] - 60),
                                fill=(40, 90, 200, 255))
    img.save(path)
    return str(path)


def _green_shirt_anchor(path, size=(120, 160)):
    """A character painted mostly in green — the collision `pick` must avoid."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((20, 40, 100, 140), fill=(0, 190, 60, 255))    # the shirt
    d.ellipse((45, 10, 80, 45), fill=(230, 190, 160, 255))     # a face
    img.save(path)
    return str(path)


# ---------------------------------------------------------------------------
# 1. the key colour is chosen AGAINST the art
# ---------------------------------------------------------------------------

class TestKeyColorAvoidsTheArt:
    def test_a_green_character_does_not_get_a_green_screen(self, tmp_path):
        anchor = _green_shirt_anchor(tmp_path / "anchor.png")
        name, rgb = chroma.pick(anchor)
        assert name != "green" and rgb != (0, 255, 0), (
            "keyed green out of a green shirt — the key must be picked against "
            "the art's own palette")

    def test_the_pinned_anchor_constrains_the_pick_too(self, tmp_path):
        """The working reference may be neutral while the ANCHOR is the thing
        identity has to hold — its palette has to count."""
        neutral = tmp_path / "ref.png"
        Image.new("RGBA", (60, 60), (120, 120, 120, 255)).save(neutral)
        anchor = _green_shirt_anchor(tmp_path / "anchor.png")
        assert chroma.pick(neutral, anchors=[anchor])[0] != "green"

    def test_the_pick_reports_the_distance_it_won_by(self, tmp_path):
        anchor = _green_shirt_anchor(tmp_path / "anchor.png")
        report = chroma.pick_report(anchor)
        assert report["name"] == chroma.pick(anchor)[0]
        assert report["distance"] > chroma.distance_to((0, 255, 0),
                                                       chroma.palette_of(anchor))
        assert report["safe"] is True

    def test_no_reference_falls_back_to_magenta(self):
        assert chroma.pick() == ("magenta", (255, 0, 255))

    def test_distance_is_to_the_nearest_color_not_the_average(self):
        # One colliding colour in an otherwise distant palette must still be
        # disqualifying; averaging would hide it.
        assert chroma.distance_to((0, 255, 0), [(0, 250, 0), (10, 10, 10)]) < 10


# ---------------------------------------------------------------------------
# 2. the clause: injected for sprite work, withheld from plates
# ---------------------------------------------------------------------------

class TestWhoGetsTheClause:
    def test_sprite_shaped_kinds_are_keyed(self):
        for kind in ("anchor", "animation", "item", "sprite", "sheet", "gear"):
            assert chroma.needs_key(kind), kind

    def test_full_bleed_kinds_are_not(self):
        for kind in ("background", "tile", "ui", "concept", "plate"):
            assert not chroma.needs_key(kind), kind

    def test_unknown_kinds_do_not_get_keyed(self):
        assert not chroma.needs_key("") and not chroma.needs_key("whatever")

    def test_the_clause_is_explicit_about_flatness(self):
        text = chroma.clause(("magenta", (255, 0, 255))).lower()
        for demand in ("flat", "uniform", "single solid", "255,0,255",
                       "no gradient", "no vignette", "inside the frame"):
            assert demand in text, demand

    def test_a_backdrop_does_not(self, tmp_path, stub_openai):
        chroma.generate("a rainy alley", tmp_path / "b.png", provider="openai",
                        task_kind="background")
        assert stub_openai["prompt"] == "a rainy alley"
        assert "background" not in stub_openai["kwargs"]

    def test_the_keyed_path_never_also_asks_the_api_for_alpha(
            self, tmp_path, stub_openai):
        # Asking for both is how you get a half-transparent image with a
        # gradient through it — two half-cuts instead of one clean one.
        chroma.generate("a paladin", tmp_path / "d.png", provider="openai",
                        task_kind="anchor", transparent=True)
        assert stub_openai["kwargs"].get("transparent") is False


# ---------------------------------------------------------------------------
# 3. the audit is a gate, not a decoration
# ---------------------------------------------------------------------------

class TestTheAuditGate:
    def test_a_clean_flat_backdrop_passes(self, tmp_path):
        path = _flat_backdrop(tmp_path / "clean.png")
        got = chroma.finish(path, (255, 0, 255), name="magenta")
        assert got["ok"], got.get("error")
        assert got["alpha"]["clean"] is True
        # and it really is a cut-out, not a rectangle
        assert Image.open(path).convert("RGBA").getchannel("A").getextrema()[0] == 0

    def test_a_gradient_backdrop_FAILS_with_the_specific_flag(self, tmp_path):
        path = _gradient_backdrop(tmp_path / "gradient.png")
        got = chroma.finish(path, (255, 0, 255), name="magenta")
        assert not got["ok"], "a gradient backdrop was accepted as a clean cut"
        assert any("background bleed" in f for f in got["alpha"]["flags"]), \
            got["alpha"]["flags"]
        assert "did not key cleanly" in got["error"]

    def test_the_rejected_file_is_left_on_disk_to_look_at(self, tmp_path):
        path = _gradient_backdrop(tmp_path / "gradient.png")
        chroma.finish(path, (255, 0, 255), name="magenta")
        assert (tmp_path / "gradient.png").is_file()

    def test_dirty_alpha_is_flagged(self, tmp_path):
        """Colour left sitting under alpha 0 — it fringes the moment anything
        rescales. The keyer zeroes RGB; anything that does not, fails."""
        img = Image.new("RGBA", (120, 120), (255, 0, 255, 0))   # alpha 0, hot RGB
        ImageDraw.Draw(img).rectangle((30, 30, 90, 90), fill=(40, 90, 200, 255))
        path = tmp_path / "dirty.png"
        img.save(path)
        flags = chroma.audit(path)
        assert any("dirty alpha" in f for f in flags["flags"]), flags

    def test_a_hollow_interior_fails_rather_than_merely_advises(self, tmp_path):
        """A big enclosed hole means the key colour appeared INSIDE the art and
        was cut out of it — the collision `pick` exists to prevent."""
        img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle((40, 40, 160, 160), fill=(40, 90, 200, 255))
        d.rectangle((70, 70, 130, 130), fill=(0, 0, 0, 0))       # the hole
        path = tmp_path / "hollow.png"
        img.save(path)
        flags = chroma.audit(path)
        assert any("hollow interior" in f for f in flags["flags"]), flags

    def test_generation_reports_failure_instead_of_returning_dirty_alpha(
            self, tmp_path, stub_openai):
        stub_openai["painter"] = _gradient_backdrop
        got = chroma.generate("a paladin", tmp_path / "e.png",
                              provider="openai", task_kind="animation")
        assert got["ok"] is False
        assert any("background bleed" in f for f in got["alpha"]["flags"])
        assert got["rejected_path"]

    def test_an_unkeyed_file_is_recognised(self, tmp_path):
        opaque = tmp_path / "opaque.png"
        Image.new("RGBA", (80, 80), (120, 60, 30, 255)).save(opaque)
        assert chroma.looks_unkeyed(opaque)
        cut = _flat_backdrop(tmp_path / "cut.png")
        assert chroma.looks_unkeyed(cut), "a backdrop that was never keyed"
        chroma.finish(cut, (255, 0, 255), name="magenta")
        assert not chroma.looks_unkeyed(cut)


# ---------------------------------------------------------------------------
# 4. both providers walk through the same door
# ---------------------------------------------------------------------------

class TestProviderParity:
    def test_krea_sprite_work_is_keyed_and_audited(self, tmp_path, stub_krea):
        got = chroma.generate("a paladin", tmp_path / "k.png", provider="krea",
                              model="krea-2-medium", task_kind="animation")
        assert got["ok"], got.get("error")
        assert got["keyed"] is True
        assert "COMPLETELY FLAT" in stub_krea["prompt"]
        assert got["alpha"]["clean"] is True
        # Krea has NO transparency parameter — the key is the only alpha there is
        assert Image.open(tmp_path / "k.png").convert("RGBA") \
            .getchannel("A").getextrema()[0] == 0

    def test_krea_gets_the_same_key_colour_openai_would(self, tmp_path,
                                                        stub_krea, stub_openai):
        anchor = _green_shirt_anchor(tmp_path / "anchor.png")
        a = chroma.generate("x", tmp_path / "a.png", provider="openai",
                            task_kind="anchor", ref_paths=[anchor])
        b = chroma.generate("x", tmp_path / "b.png", provider="krea",
                            model="krea-2-medium", task_kind="anchor",
                            ref_paths=[anchor])
        assert a["chroma"]["name"] == b["chroma"]["name"] != "green"

    def test_krea_backdrops_are_left_alone(self, tmp_path, stub_krea):
        got = chroma.generate("a rainy alley", tmp_path / "bg.png",
                              provider="krea", model="flux-1-dev",
                              task_kind="background")
        assert got["keyed"] is False
        assert stub_krea["prompt"] == "a rainy alley"
        assert "alpha" not in got

    def test_a_provider_failure_is_a_reason_not_a_traceback(self, tmp_path,
                                                            monkeypatch):
        from bgate_adapters import krea as _krea
        monkeypatch.setattr(_krea, "generate",
                            lambda *a, **k: {"ok": False, "error": "no credit"})
        got = chroma.generate("x", tmp_path / "z.png", provider="krea",
                              task_kind="animation")
        assert got["ok"] is False and "no credit" in got["error"]

    def test_a_transparent_anchor_is_plated_before_it_is_sent(self, tmp_path,
                                                              stub_krea):
        """MEASURED: a keyed anchor handed to Krea as a style reference gets its
        alpha flattened to BLACK, and the model then paints the new frame on
        black with a magenta rim light. 100% opaque border, audit rejected,
        $0.03 gone. The reference has to SHOW the backdrop we want."""
        anchor = tmp_path / "anchor.png"
        img = Image.new("RGBA", (80, 100), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle((20, 20, 60, 80), fill=(0, 190, 60, 255))
        img.save(anchor)

        chroma.generate("a pose", tmp_path / "p.png", provider="krea",
                        model="krea-2-medium", task_kind="animation",
                        ref_paths=[str(anchor)])
        sent = stub_krea["kwargs"]["style_refs"]
        assert len(sent) == 1 and sent[0]["url"] != str(anchor)

        plated = tmp_path / ".chroma_refs" / "anchor_on_chroma.png"
        assert plated.is_file(), "the anchor was sent with its alpha intact"
        corner = Image.open(plated).convert("RGB").getpixel((0, 0))
        assert corner == (255, 0, 255), corner   # the key colour, not black

    def test_an_opaque_anchor_is_sent_as_is(self, tmp_path, stub_krea):
        anchor = _flat_backdrop(tmp_path / "already_plated.png")
        chroma.generate("a pose", tmp_path / "q.png", provider="krea",
                        model="krea-2-medium", task_kind="animation",
                        ref_paths=[anchor])
        assert not (tmp_path / ".chroma_refs").exists()

    def test_an_unknown_provider_is_refused_by_name(self, tmp_path):
        got = chroma.generate("x", tmp_path / "z.png", provider="midjourney",
                              task_kind="animation")
        assert got["ok"] is False and "midjourney" in got["error"]


# ---------------------------------------------------------------------------
# stubs — the adapter boundary, never the network
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_openai(monkeypatch):
    """Capture what imagegen was asked for, and paint whatever the test wants."""
    seen: dict = {"painter": _flat_backdrop}

    def _paint(prompt, out_path, **kwargs):
        seen["prompt"] = prompt
        seen["kwargs"] = kwargs
        bg = (255, 0, 255)
        for _, rgb in chroma.CHROMA:
            if f"{rgb[0]},{rgb[1]},{rgb[2]}" in prompt:
                bg = rgb
                break
        painter = seen["painter"]
        if painter is _flat_backdrop:
            painter(out_path, bg=bg)
        else:
            painter(out_path)
        return {"ok": True, "path": str(out_path), "bytes": 1, "seconds": 0.1,
                "usd": 0.042, "model": "gpt-image-1"}

    from bgate_adapters import imagegen

    monkeypatch.setattr(imagegen, "generate",
                        lambda prompt, out, **kw: _paint(prompt, out, **kw))
    monkeypatch.setattr(imagegen, "edit",
                        lambda prompt, refs, out, **kw: _paint(prompt, out, **kw))
    return seen


@pytest.fixture
def stub_krea(monkeypatch):
    seen: dict = {"painter": _flat_backdrop}

    def _generate(prompt, out_path, **kwargs):
        seen["prompt"] = prompt
        seen["kwargs"] = kwargs
        bg = (255, 0, 255)
        for _, rgb in chroma.CHROMA:
            if f"{rgb[0]},{rgb[1]},{rgb[2]}" in prompt:
                bg = rgb
                break
        seen["painter"](out_path, bg=bg) if seen["painter"] is _flat_backdrop \
            else seen["painter"](out_path)
        return {"ok": True, "path": str(out_path), "bytes": 1, "seconds": 0.1,
                "usd": 0.03, "provider": "krea",
                "model": kwargs.get("model")}

    from bgate_adapters import krea

    monkeypatch.setattr(krea, "generate", _generate)
    return seen
