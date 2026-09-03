"""Texture and decal kinds — the prompt-scoping layer, and what it forbade.

The layered 3D path asks an agent to generate one texture per mesh layer and
apply it. Three things made that impossible, and each has a class here:

  * every texture prompt carried the full 2D-sprite clause, including the hard
    directive "No text, letters, words, numbers, labels or signage anywhere in
    the image" — so the seat brief instructed the agent to make a team logo its
    own textured layer using a tool that forbids text BY CONSTRUCTION;
  * the map came back as a lit illustration, whose baked highlights and shadows
    then multiplied a second time against the Blender lights and read as muddy;
  * a non-square generation stretched across a unit UV island, and nothing
    could make a repeating material at all.

The single most important test in this file is the regression guard: a real
user produced this product's best 2D character through the Krea path, and every
change here has to be additive and default-off for that path. Anything that
moves the ordinary character clause by one byte is a bug in this work, not a
test to update.
"""
from __future__ import annotations

import pytest

from bgate_adapters import imagegen, krea
from bgate_core.art import artdirection as ad
from bgate_core.design import bible

# The clause an ordinary character generation got BEFORE texture and decal
# existed, frozen verbatim. Not a paraphrase and not recomputed from the
# vocabulary — a guard that derives its expectation from the code it guards
# catches nothing.
CHARACTER_CLAUSE_BEFORE = (
    "\n\nStyle: true chunky pixel art with a visible pixel grid, hard-edged, no "
    "anti-aliased or painterly rendering; angled 3/4 isometric view, 2:1 tile "
    "geometry, never flat top-down and never straight-on front view; chibi "
    "proportions, large head, short body; 16-bit SNES-era sprite work; dark "
    "palette with neon accents. No text, letters, words, numbers, labels or "
    "signage anywhere in the image."
)

NO_TEXT = "no text, letters"


@pytest.fixture()
def brief(root):
    """The same bible the original art-direction suite uses, so the two files
    are talking about one project rather than two conveniences."""
    bible.add(root, "constraint", "ART DIRECTION LOCKED: the pixel set",
              "true chunky pixel art, visible pixel grid, no painterly "
              "rendering, dark neon office palette", rank=1)
    bible.add(root, "constraint", "PROJECTION LOCKED: isometric 2:1",
              "floor tiles are 2:1 diamonds, NOT flat top-down", rank=2)
    bible.add(root, "constraint", "Style anchor",
              "16-bit SNES-era tile art, chibi proportions", rank=3)
    bible.add(root, "constraint", "Determinism",
              "same state and same seed produce the same outcome", rank=4)
    return root


@pytest.fixture()
def png(tmp_path):
    from PIL import Image

    p = tmp_path / "anchor.png"
    Image.new("RGBA", (16, 16), (10, 200, 90, 255)).save(p)
    return str(p)


# ---------------------------------------------------------------------------
# The guard. Read this one first.
# ---------------------------------------------------------------------------

class TestOrdinary2DIsUntouched:
    def test_a_character_clause_is_byte_identical(self, brief):
        """The strongest statement this file can make: the Krea character path
        that already works gets exactly the string it got before."""
        assert ad.clause(brief, task_kind="animation") == CHARACTER_CLAUSE_BEFORE

    def test_an_unspecified_kind_is_byte_identical_too(self, brief):
        """Most callers never named a kind. They must be the last ones to
        notice a new kind exists."""
        assert ad.clause(brief) == CHARACTER_CLAUSE_BEFORE

    @pytest.mark.parametrize("kind", ["anchor", "sprite", "sheet", "portrait",
                                      "prop", "vfx", "ui", "icon",
                                      "item", "gear", "background"])
    def test_no_existing_kind_grew_a_form_clause(self, kind):
        assert ad.form_clause(kind) == ""

    def test_tile_is_a_texture_kind_now_and_that_was_the_point(self):
        """"tile" was missing from TEXTURE_KINDS, so `tileable=True` did nothing
        for the one kind actually named "tile" — this test used to pin that gap
        as the contract. A tile IS a flat albedo map and wants the clause."""
        assert ad.form_clause("tile") != ""
        assert "TEXTURE MAP" in ad.form_clause("tile")

    @pytest.mark.parametrize("kind", ["anchor", "sprite", "sheet", "portrait",
                                      "prop", "tile", "vfx", "ui", "background"])
    def test_every_existing_kind_still_forbids_text(self, brief, kind):
        assert NO_TEXT in ad.clause(brief, task_kind=kind).lower()

    @pytest.mark.parametrize("kind", ["anchor", "sprite", "sheet", "portrait",
                                      "prop", "tile", "vfx", "ui", "icon",
                                      "item", "gear", "background", ""])
    def test_the_silhouette_directive_still_reaches_everything_it_did(self, kind):
        """It gained a scope so a tiling floor would stop being told to have a
        readable outline. Every kind that had it keeps it."""
        assert any("silhouette" in d for d in ad.directives_for(kind))

    @pytest.mark.parametrize("kind", ["anchor", "sprite", "prop", ""])
    def test_no_existing_kind_is_forced_square(self, kind):
        assert imagegen.size_for("1536x1024", task_kind=kind) == "1536x1024"

    def test_a_tile_sheet_is_squared_now_and_that_is_correct(self):
        """"tile" became a texture kind — an atlas of NxN square cells wants a
        square canvas, and a wide one wastes the request. This test used to pin
        the opposite as contract."""
        assert imagegen.size_for("1536x1024", task_kind="tile") == "1024x1024"

    def test_a_tile_still_forbids_text_even_though_a_texture_does_not(self, brief):
        """A material may carry a stencilled logo. A terrain tile may not: the
        letters repeat across the whole level and autotiling scatters them at
        every rotation."""
        assert NO_TEXT in ad.clause(brief, task_kind="tile").lower()
        assert NO_TEXT not in ad.clause(brief, task_kind="texture").lower()


# ---------------------------------------------------------------------------
# DEFECT 1 — texture prompts carried the sprite clause
# ---------------------------------------------------------------------------

class TestTextureClause:
    def test_it_does_not_forbid_text(self, brief):
        """The defect that made the brief self-contradictory: the seat is told
        to put the team logo on its own textured layer with a tool whose every
        prompt ended in an absolute ban on letters."""
        assert NO_TEXT not in ad.clause(brief, task_kind="texture").lower()

    def test_it_carries_no_character_anatomy(self, brief):
        """"chibi proportions, large head, short body" on a wood plank."""
        assert "chibi" not in ad.clause(brief, task_kind="texture")

    def test_it_carries_no_projection(self, brief):
        """A UV-sampled map has no projection to be wrong about; asking for an
        isometric one gets a diamond of ground baked into the albedo."""
        assert "isometric" not in ad.clause(brief, task_kind="texture")

    def test_it_carries_no_silhouette(self, brief):
        """"bold outlines readable at small size" bakes a black border into
        every repeat of the tile."""
        assert "silhouette" not in ad.clause(brief, task_kind="texture")

    def test_it_demands_flat_unlit_albedo(self, brief):
        """The muddy-mesh defect: baked shading multiplies a second time
        against the scene lights, and no downstream step can remove it."""
        text = ad.clause(brief, task_kind="texture").lower()
        for demand in ("albedo", "no cast shadow", "no highlights",
                       "no baked shading", "no ambient occlusion"):
            assert demand in text, f"{demand!r} missing from the texture clause"

    def test_it_forbids_a_background_and_a_camera(self, brief):
        text = ad.clause(brief, task_kind="texture").lower()
        assert "no background" in text
        assert "no perspective" in text
        assert "orthographic" in text

    def test_the_form_clause_comes_first(self, brief):
        """Style is a nudge; form is the shape of the file. A model that runs
        out of attention must lose the nudge."""
        text = ad.clause(brief, task_kind="texture")
        assert text.index("TEXTURE MAP") < text.index("Style:")

    def test_the_project_palette_still_applies(self, brief):
        """Scoping subtracts anatomy and projection, not the project's look —
        a texture on a dark-neon game is still a dark-neon texture."""
        text = ad.clause(brief, task_kind="texture").lower()
        assert "neon" in text and "pixel art" in text

    def test_it_survives_a_project_with_no_bible(self, root):
        """The form clause is NOT read out of a bible. "A texture map carries
        no baked lighting" is true of the asset kind, so it must be emitted
        where the style clause correctly is not."""
        assert ad.clause(root, task_kind="texture").startswith("\n\nTEXTURE MAP")

    def test_material_and_albedo_are_the_same_kind(self, brief):
        """An agent naming this itself reaches for "material" as readily as
        "texture", and a near-miss kind silently means "unknown"."""
        for alias in ("material", "albedo", "TEXTURE", " texture "):
            assert ad.is_texture_kind(alias), alias
            assert NO_TEXT not in ad.clause(brief, task_kind=alias).lower()

    def test_tileable_is_asked_for_only_when_requested(self, brief):
        assert "tileable" not in ad.clause(brief, task_kind="texture").lower()
        asked = ad.clause(brief, task_kind="texture", tileable=True).lower()
        assert "seamlessly tileable" in asked
        assert "left edge matches the right" in asked

    def test_tileable_is_ignored_by_every_other_kind(self, brief):
        for kind in ("animation", "sprite", "prop", ""):
            assert (ad.clause(brief, task_kind=kind, tileable=True)
                    == ad.clause(brief, task_kind=kind))


class TestDecalClause:
    """The sibling need: here the text IS the asset."""

    def test_it_does_not_forbid_text(self, brief):
        assert NO_TEXT not in ad.clause(brief, task_kind="decal").lower()

    def test_it_demands_the_text_be_crisp(self, brief):
        text = ad.clause(brief, task_kind="decal").lower()
        for demand in ("legible", "exact spelling", "high contrast"):
            assert demand in text, f"{demand!r} missing from the decal clause"

    def test_it_is_isolated_with_nothing_behind_it(self, brief):
        text = ad.clause(brief, task_kind="decal").lower()
        assert "nothing else is in the frame" in text
        assert "no drop shadow" in text

    def test_it_carries_no_character_anatomy(self, brief):
        assert "chibi" not in ad.clause(brief, task_kind="decal")

    def test_it_keeps_the_readable_silhouette(self, brief):
        """Unlike a texture, a logo genuinely wants a silhouette that reads
        small — that is most of what a logo is."""
        assert any("silhouette" in d for d in ad.directives_for("decal"))

    def test_logo_is_the_same_kind(self, brief):
        for alias in ("logo", "emblem", "insignia", "sticker"):
            assert ad.is_decal_kind(alias), alias

    def test_a_decal_is_not_a_texture(self):
        assert not ad.is_texture_kind("decal")
        assert not ad.is_decal_kind("texture")

    def test_it_survives_a_project_with_no_bible(self, root):
        assert ad.clause(root, task_kind="decal").startswith("\n\nDECAL")


# ---------------------------------------------------------------------------
# DEFECT 2 — non-square maps, and nothing that repeats
# ---------------------------------------------------------------------------

class TestSquareConstraint:
    @pytest.mark.parametrize("kind", ["texture", "material", "albedo"])
    @pytest.mark.parametrize("asked", ["1536x1024", "1024x1536", "auto"])
    def test_a_texture_is_forced_square(self, kind, asked):
        """A 1536x1024 map sampled across a unit UV island is stretched 1.5x in
        one axis, and nothing downstream can undo it."""
        assert imagegen.size_for(asked, task_kind=kind) == "1024x1024"

    def test_the_square_it_picks_is_one_the_api_accepts(self):
        assert imagegen.SQUARE_SIZE in imagegen.SIZES

    def test_an_unnamed_kind_keeps_what_it_asked_for(self):
        assert imagegen.size_for("1024x1536") == "1024x1536"

    def test_krea_coerces_at_the_adapter(self, monkeypatch, tmp_path):
        """Krea takes an aspect ratio rather than pixels, so the coercion has
        to happen before aspect_for() reads the size."""
        seen = {}

        def fake_submit(prompt, **kw):
            seen.update(kw)
            raise krea.KreaError("stopped before spending")

        monkeypatch.setattr(krea, "submit", fake_submit)
        krea.generate("mossy stone", str(tmp_path / "t.png"),
                      size="1536x1024", task_kind="texture")
        assert seen["size"] == "1024x1024"
        assert krea.aspect_for(seen["size"]) == "1:1"

    def test_krea_leaves_ordinary_work_alone(self, monkeypatch, tmp_path):
        seen = {}

        def fake_submit(prompt, **kw):
            seen.update(kw)
            raise krea.KreaError("stopped before spending")

        monkeypatch.setattr(krea, "submit", fake_submit)
        krea.generate("a fighter", str(tmp_path / "c.png"),
                      size="1024x1536", task_kind="anchor")
        assert seen["size"] == "1024x1536"


class TestTileable:
    def test_the_edges_actually_join(self, tmp_path):
        """The guarantee, and the reason the pass exists: the prompt-side ask
        is only an ask, and a material that ALMOST tiles is not a material."""
        from PIL import Image

        p = tmp_path / "grain.png"
        im = Image.new("RGB", (32, 32))
        px = im.load()
        for y in range(32):
            for x in range(32):
                px[x, y] = (x * 8 % 256, y * 8 % 256, 40)
        im.save(p)

        assert imagegen.make_tileable(str(p))["ok"] is True

        out = Image.open(p).convert("RGB")
        w, h = out.size
        assert (w, h) == (32, 32)
        assert ([out.getpixel((0, y)) for y in range(h)]
                == [out.getpixel((w - 1, y)) for y in range(h)])
        assert ([out.getpixel((x, 0)) for x in range(w)]
                == [out.getpixel((x, h - 1)) for x in range(w)])

    def test_it_says_what_it_actually_did(self, png):
        """It mirrors. It does not synthesise a seamless texture, and the
        result has to say so or someone will use it on brickwork."""
        got = imagegen.make_tileable(png)
        assert got["method"] == "mirror-2x2"
        assert "symmetric" in got["note"]

    def test_it_can_write_beside_the_original(self, png, tmp_path):
        out = tmp_path / "tiled.png"
        got = imagegen.make_tileable(png, str(out))
        assert got["path"] == str(out) and out.is_file()

    def test_a_bad_file_is_a_note_not_a_crash(self, tmp_path):
        """The generation is already paid for. A post-pass that cannot run must
        never turn a delivered image into an error."""
        bad = tmp_path / "not-an-image.png"
        bad.write_text("nope")
        got = imagegen.make_tileable(str(bad))
        assert got["ok"] is False and got["note"]


# ---------------------------------------------------------------------------
# DEFECT 3 — "conditioned on the pinned refs", with no ref parameter
# ---------------------------------------------------------------------------

class TestReferencePlumbing:
    def test_paths_become_the_reference_array(self, png):
        refs = krea.refs_from_paths([png])
        assert len(refs) == 1
        assert refs[0]["url"].startswith("data:image/png;base64,")
        assert refs[0]["strength"] == 0.5

    def test_strength_is_carried_through(self, png):
        assert krea.refs_from_paths([png], 0.8)[0]["strength"] == 0.8

    def test_a_missing_anchor_fails_before_any_money_moves(self, tmp_path):
        with pytest.raises(krea.KreaError):
            krea.refs_from_paths([str(tmp_path / "gone.png")])

    def test_generate_takes_paths(self, monkeypatch, tmp_path, png):
        seen = {}

        def fake_submit(prompt, **kw):
            seen.update(kw)
            raise krea.KreaError("stopped before spending")

        monkeypatch.setattr(krea, "submit", fake_submit)
        krea.generate("a fighter", str(tmp_path / "o.png"), ref_paths=[png])
        assert len(seen["style_refs"]) == 1

    def test_built_refs_and_paths_add_rather_than_replace(self, monkeypatch,
                                                          tmp_path, png):
        """Backward compatible in the strict sense: a caller already passing
        style_refs keeps every one of them."""
        seen = {}

        def fake_submit(prompt, **kw):
            seen.update(kw)
            raise krea.KreaError("stopped before spending")

        monkeypatch.setattr(krea, "submit", fake_submit)
        krea.generate("a fighter", str(tmp_path / "o.png"),
                      style_refs=[krea.style_ref(png, 0.3)], ref_paths=[png])
        assert len(seen["style_refs"]) == 2

    def test_no_refs_still_sends_none(self, monkeypatch, tmp_path):
        seen = {}

        def fake_submit(prompt, **kw):
            seen.update(kw)
            raise krea.KreaError("stopped before spending")

        monkeypatch.setattr(krea, "submit", fake_submit)
        krea.generate("a fighter", str(tmp_path / "o.png"))
        assert seen["style_refs"] is None

    def test_a_bad_anchor_is_an_error_not_an_exception(self, monkeypatch,
                                                       tmp_path):
        """The adapter contract is a result dict; a caller must not have to
        wrap it in a try to survive a deleted pin."""
        monkeypatch.setattr(krea, "submit",
                            lambda *a, **k: pytest.fail("must not reach Krea"))
        got = krea.generate("a fighter", str(tmp_path / "o.png"),
                            ref_paths=[str(tmp_path / "gone.png")])
        assert got["ok"] is False and got["usd"] == 0.0

    def test_which_models_can_be_anchored_at_all(self):
        """imagen and flux-1.1-pro are prompt-only, so "generate conditioned on
        the pinned refs" is not a dearer version of the request there — it is
        not the request. Worth asking before quoting."""
        assert krea.supports_style_refs("krea-2-large") is True
        assert krea.supports_style_refs("krea-2-medium") is True
        assert krea.supports_style_refs("imagen-4") is False
        assert krea.supports_style_refs("flux-1.1-pro") is False


class TestReferencePricing:
    def test_a_reference_costs_more(self):
        """Krea's price moves with the payload: krea-2-large is $0.06 plain and
        $0.065 once references are attached."""
        assert krea.price_for("krea-2-large") == 0.06
        assert krea.price_for("krea-2-large", style_refs=1) == 0.065

    def test_a_character_reference_model_is_priced_at_its_real_rate(self):
        assert krea.price_for("ideogram-3", style_refs=1) == 0.1575

    def test_the_quote_counts_paths_not_just_built_refs(self, monkeypatch,
                                                        tmp_path, png):
        """The under-quote this closes: an anchored generation submitted via
        ref_paths used to be billed as if it were plain."""
        monkeypatch.setattr(krea, "submit",
                            lambda p, **k: {"job_id": "j1"})
        monkeypatch.setattr(krea, "poll",
                            lambda j, **k: {"status": "completed",
                                            "result": {"urls": ["http://x/i.png"]}})
        monkeypatch.setattr(krea, "download", lambda url, out, **k: 4)
        got = krea.generate("a fighter", str(tmp_path / "o.png"),
                            ref_paths=[png])
        assert got["ok"] is True
        assert got["usd"] == 0.065

    def test_a_plain_generation_is_still_quoted_plain(self, monkeypatch,
                                                      tmp_path):
        monkeypatch.setattr(krea, "submit", lambda p, **k: {"job_id": "j1"})
        monkeypatch.setattr(krea, "poll",
                            lambda j, **k: {"status": "completed",
                                            "result": {"urls": ["http://x/i.png"]}})
        monkeypatch.setattr(krea, "download", lambda url, out, **k: 4)
        got = krea.generate("a fighter", str(tmp_path / "o.png"))
        assert got["usd"] == 0.06


class TestGptImageReferenceSurface:
    """gpt-image has no reference input — its only way to hold an anchor is to
    EDIT it. That branch used to live in every caller; now the adapter owns it,
    so "generate, conditioned on the refs" is one call for both providers."""

    def test_generate_with_refs_delegates_to_edit(self, monkeypatch, tmp_path, png):
        seen = {}

        def fake_edit(prompt, refs, out, **kw):
            seen.update(prompt=prompt, refs=refs, out=out, **kw)
            return {"ok": True, "path": out}

        monkeypatch.setattr(imagegen, "edit", fake_edit)
        imagegen.generate("a fighter", str(tmp_path / "o.png"), ref_paths=[png])
        assert seen["refs"] == [png]

    def test_the_texture_square_survives_the_delegation(self, monkeypatch,
                                                        tmp_path, png):
        seen = {}

        def fake_edit(prompt, refs, out, **kw):
            seen.update(kw)
            return {"ok": True, "path": out}

        monkeypatch.setattr(imagegen, "edit", fake_edit)
        imagegen.generate("mossy stone", str(tmp_path / "o.png"),
                          ref_paths=[png], size="1536x1024", task_kind="texture")
        assert seen["size"] == "1024x1024"

    def test_no_refs_does_not_go_anywhere_near_edit(self, monkeypatch, tmp_path):
        monkeypatch.setattr(imagegen, "edit",
                            lambda *a, **k: pytest.fail("must not edit"))
        monkeypatch.setattr(imagegen, "available",
                            lambda: {"available": False, "reason": "no key"})
        got = imagegen.generate("a fighter", str(tmp_path / "o.png"))
        assert got["ok"] is False


# ---------------------------------------------------------------------------
# The door every generation walks through
# ---------------------------------------------------------------------------

class TestWiring:
    def test_chroma_hands_the_texture_clause_to_the_provider(self, brief,
                                                             monkeypatch,
                                                             tmp_path):
        from bgate_core.art import chroma

        seen = {}

        def fake(prompt, out_path, **kw):
            seen.update(prompt=prompt, **kw)
            return {"ok": False, "error": "stopped before spending"}

        monkeypatch.setattr("bgate_adapters.krea.generate", fake)
        chroma.generate("mossy stone", tmp_path / "t.png", provider="krea",
                        model="krea-2-medium", task_kind="texture",
                        size="1536x1024", tileable=True, root=brief)
        assert "TEXTURE MAP" in seen["prompt"]
        assert "Seamlessly tileable" in seen["prompt"]
        assert NO_TEXT not in seen["prompt"].lower()
        assert seen["task_kind"] == "texture" and seen["tileable"] is True

    def test_a_texture_is_not_keyed(self):
        """A texture map is full-bleed — the surface IS the asset. Keying one
        would hand back an empty file."""
        from bgate_core.art import chroma

        assert chroma.needs_key("texture") is False

    def test_the_form_clause_reaches_a_rootless_call(self, monkeypatch, tmp_path):
        """No project means no bible, but "a texture map carries no baked
        lighting" was never a bible statement."""
        from bgate_core.art import chroma

        seen = {}
        monkeypatch.setattr("bgate_adapters.krea.generate",
                            lambda p, o, **k: (seen.update(prompt=p),
                                               {"ok": False, "error": "x"})[1])
        chroma.generate("mossy stone", tmp_path / "t.png", provider="krea",
                        model="krea-2-medium", task_kind="texture")
        assert "TEXTURE MAP" in seen["prompt"]

    def test_an_ordinary_krea_character_call_is_unchanged(self, brief,
                                                          monkeypatch, tmp_path):
        """The path that produced this product's best asset. Nothing new may
        appear in its prompt."""
        from bgate_core.art import chroma

        seen = {}
        monkeypatch.setattr("bgate_adapters.krea.generate",
                            lambda p, o, **k: (seen.update(prompt=p),
                                               {"ok": False, "error": "x"})[1])
        chroma.generate("a fighter", tmp_path / "c.png", provider="krea",
                        model="krea-2-medium", task_kind="anchor", root=brief)
        assert "TEXTURE MAP" not in seen["prompt"]
        assert "DECAL" not in seen["prompt"]
        # The whole character clause is still there. Compared with its runs of
        # whitespace collapsed because the KEYED path has always squeezed them
        # (strip_transparency_asks) before appending the background contract.
        squeezed = " ".join(CHARACTER_CLAUSE_BEFORE.split())
        assert squeezed in " ".join(seen["prompt"].split())
        assert "BACKGROUND (mandatory)" in seen["prompt"]
