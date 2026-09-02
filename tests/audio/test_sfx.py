"""Procedural SFX — determinism, and the recipe that has to rebuild the render.

The audio house rule (dispatch.SEAT_RULES["audio"]) requires every synthesized
asset to ship a ``<name>.synth.json`` another process can re-render identically
from. That is a claim about BYTES, not about a file existing, so these tests
compare bytes: same recipe twice, and a recipe carried away from its wav and
rendered somewhere else entirely.

The seed is the only non-arithmetic input, and it is the thing that used to be
implicit. A module-level ``random`` would have made two identical recipes render
two different explosions — which nothing would have caught, because both are
plausible explosions.
"""
from __future__ import annotations

import json
import shutil
import wave
from pathlib import Path

import pytest

from bgate_core.audio import sfx
from bgate_mcp import server


async def call(tool: str, /, **kwargs) -> dict:
    """Dispatch through FastMCP, the way a client hits it."""
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return root


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------
class TestKinds:
    def test_every_kind_the_seat_was_asked_for_exists(self):
        names = {k["kind"] for k in sfx.kinds()}
        # The gap this module closed: there was no way to make a game sound at
        # all. These are the ones a 2D game asks for on day one.
        assert {"blip", "pickup", "jump", "laser", "explosion", "hit",
                "powerup"} <= names

    def test_the_words_a_designer_writes_resolve(self):
        assert sfx.resolve_kind("coin") == "pickup"
        assert sfx.resolve_kind("shoot") == "laser"
        assert sfx.resolve_kind("Thud") == "hit"
        assert sfx.resolve_kind("level up") == "powerup"

    def test_an_unknown_kind_names_the_options_instead_of_guessing(self):
        with pytest.raises(sfx.SfxError) as exc:
            sfx.resolve_kind("pew")
        assert "laser" in str(exc.value)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
class TestDeterminism:
    def test_the_same_recipe_renders_the_same_bytes(self):
        # explosion is the one that would expose a shared RNG: it is nothing but
        # noise, so two renders of it differ everywhere or nowhere.
        rec = sfx.recipe("explosion", name="boom")
        assert sfx.render(rec) == sfx.render(rec)

    def test_the_default_seed_comes_from_the_name_not_the_clock(self):
        first = sfx.recipe("explosion", name="boom")
        again = sfx.recipe("explosion", name="boom")
        assert first == again
        assert first["seed"] == sfx.default_seed("explosion", "boom")
        # Regenerating tomorrow must be a no-op, not a diff nobody asked for.
        assert sfx.render(first) == sfx.render(again)

    def test_a_different_seed_is_a_different_roll(self):
        one = sfx.render(sfx.recipe("explosion", name="boom", seed=1))
        two = sfx.render(sfx.recipe("explosion", name="boom", seed=2))
        assert one != two
        assert len(one) == len(two)          # same shape, different noise

    def test_two_names_do_not_collide_on_one_sound(self):
        assert (sfx.render(sfx.recipe("hit", name="hit_wood"))
                != sfx.render(sfx.recipe("hit", name="hit_stone")))

    @pytest.mark.parametrize("kind", sfx.KINDS)
    def test_every_kind_renders_a_16_bit_mono_wav_godot_can_import(self, kind,
                                                                  tmp_path):
        # AudioStreamWAV takes 8/16-bit PCM. A float WAV imports as silence with
        # no error at all, which is the worst possible way to be wrong.
        path = tmp_path / f"{kind}.wav"
        path.write_bytes(sfx.render(sfx.recipe(kind, name=kind)))
        with wave.open(str(path), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == sfx.DEFAULT_RATE
            assert 0.0 < handle.getnframes() / handle.getframerate() <= sfx.MAX_SECONDS

    def test_the_render_ends_on_silence(self):
        # A hard cut at a non-zero sample clicks, and a click on a sound that
        # fires ten times a second is what makes a game sound cheap.
        rec = sfx.recipe("laser", name="pew")
        with wave.open(__import__("io").BytesIO(sfx.render(rec)), "rb") as handle:
            frames = handle.readframes(handle.getnframes())
        assert frames[-2:] == b"\x00\x00"


# ---------------------------------------------------------------------------
# The knobs
# ---------------------------------------------------------------------------
class TestScaling:
    def test_base_hz_scales_every_pitch_in_the_preset(self):
        low = sfx.recipe("laser", name="a", base_hz=400)
        high = sfx.recipe("laser", name="a", base_hz=800)
        assert (high["layers"][0]["freq_start"]
                == pytest.approx(low["layers"][0]["freq_start"] * 2))
        assert (high["layers"][0]["freq_end"]
                == pytest.approx(low["layers"][0]["freq_end"] * 2))

    def test_duration_scales_every_time_including_the_arpeggio_steps(self):
        rec = sfx.recipe("powerup", name="up", duration_s=1.14)
        assert rec["seconds"] == pytest.approx(1.14, abs=0.01)
        starts = [l["start_s"] for l in rec["layers"]]
        assert starts == sorted(starts) and starts[-1] > starts[0]

    def test_a_recipe_past_the_cap_is_refused_as_music(self):
        with pytest.raises(sfx.SfxError) as exc:
            sfx.recipe("explosion", name="long", duration_s=60)
        assert str(sfx.MAX_SECONDS) in str(exc.value)

    def test_a_hand_edited_recipe_is_validated_not_trusted(self):
        rec = sfx.recipe("blip", name="ui")
        rec["layers"][0]["wave"] = "sawtooth"        # not one of WAVES
        with pytest.raises(sfx.SfxError) as exc:
            sfx.render(rec)
        assert "layers[0].wave" in str(exc.value)


# ---------------------------------------------------------------------------
# The sidecar — the whole point of the house rule
# ---------------------------------------------------------------------------
class TestRecipeRoundTrip:
    def test_generate_writes_the_wav_and_its_recipe_side_by_side(self, root):
        got = sfx.generate(root, "coin", "pickup_coin")
        wav, recipe_file = Path(got["path"]), Path(got["recipe_path"])
        assert wav.is_file() and recipe_file.is_file()
        assert recipe_file.name == "pickup_coin.synth.json"
        # Inside the audio seat's own lane, resolved through the real layout.
        assert got["rel_path"].endswith("assets/audio/sfx/pickup_coin.wav")

    def test_the_recipe_carries_everything_needed_to_re_render(self, root):
        got = sfx.generate(root, "explosion", "boom")
        rec = json.loads(Path(got["recipe_path"]).read_text(encoding="utf-8"))
        assert rec["version"] == sfx.RECIPE_VERSION
        assert rec["seed"] and rec["sample_rate"] and rec["layers"]
        layer = rec["layers"][0]
        for knob in ("wave", "freq_start", "freq_end", "sweep", "attack_s",
                     "decay_s", "sustain", "release_s", "lowpass_start_hz",
                     "lowpass_end_hz", "noise_mix", "gain", "bits"):
            assert knob in layer, knob

    def test_rerender_reproduces_the_identical_file(self, root):
        got = sfx.generate(root, "explosion", "boom")
        before = Path(got["path"]).read_bytes()
        again = sfx.rerender(got["recipe_path"])
        assert again["identical"] is True
        assert Path(got["path"]).read_bytes() == before

    def test_the_recipe_alone_is_enough_far_from_its_wav(self, root, tmp_path):
        """"Another process must be able to re-render the identical asset from
        the recipe alone" — so carry the sidecar somewhere with no wav in it."""
        got = sfx.generate(root, "laser", "shoot")
        original = Path(got["path"]).read_bytes()

        elsewhere = tmp_path / "someone_elses_machine"
        elsewhere.mkdir()
        shutil.copy2(got["recipe_path"], elsewhere / "shoot.synth.json")

        rebuilt = sfx.rerender(elsewhere / "shoot.synth.json")
        assert rebuilt["had_previous"] is False       # nothing to compare against
        assert Path(rebuilt["path"]).read_bytes() == original

    def test_a_recipe_that_renders_something_else_is_caught(self, root):
        # The failure mode a sidecar has that no sidecar does not: it looks like
        # provenance. `identical` is the field that keeps it honest.
        got = sfx.generate(root, "blip", "select")
        recipe_file = Path(got["recipe_path"])
        rec = json.loads(recipe_file.read_text(encoding="utf-8"))
        rec["layers"][0]["freq_start"] = 220.0
        recipe_file.write_text(json.dumps(rec), encoding="utf-8")
        assert sfx.rerender(recipe_file)["identical"] is False

    def test_a_recipe_from_a_newer_build_refuses_rather_than_guesses(self, root,
                                                                    tmp_path):
        rec = sfx.recipe("blip", name="x")
        rec["version"] = sfx.RECIPE_VERSION + 1
        path = tmp_path / "x.synth.json"
        path.write_text(json.dumps(rec), encoding="utf-8")
        with pytest.raises(sfx.SfxError) as exc:
            sfx.rerender(path)
        assert "newer" in str(exc.value)

    def test_a_name_cannot_escape_the_sfx_directory(self, root):
        got = sfx.generate(root, "blip", "../../evil")
        assert Path(got["path"]).parent == sfx.sfx_dir(root)


class TestListing:
    def test_a_wav_with_no_recipe_is_reported_not_hidden(self, root):
        sfx.generate(root, "blip", "good")
        orphan = sfx.sfx_dir(root) / "orphan.wav"
        orphan.write_bytes(sfx.render(sfx.recipe("hit", name="orphan")))

        found = {entry["name"]: entry for entry in sfx.list_sfx(root)}
        assert found["good"]["has_recipe"] is True
        assert found["orphan"]["has_recipe"] is False


# ---------------------------------------------------------------------------
# The MCP surface
# ---------------------------------------------------------------------------
@pytest.mark.anyio
class TestMcpSurface:
    async def test_the_audio_seat_can_now_make_a_sound(self, wired):
        got = await call("sfx_generate", kind="coin", name="coin")
        assert got.get("ok") is True and "error" not in got
        assert Path(got["path"]).is_file()
        assert Path(got["recipe_path"]).name == "coin.synth.json"

    async def test_rerender_through_the_surface_reports_identical(self, wired):
        made = await call("sfx_generate", kind="jump", name="hop")
        again = await call("sfx_rerender", recipe_path=made["recipe_rel_path"])
        assert again["identical"] is True

    async def test_kinds_is_answerable_without_a_project(self):
        got = await call("sfx_kinds")
        assert {k["kind"] for k in got["kinds"]} >= {"laser", "explosion"}

    async def test_a_bad_kind_comes_back_as_a_payload_not_an_exception(self, wired):
        got = await call("sfx_generate", kind="pew", name="x")
        assert got.get("ok") is False and got.get("error")

    async def test_the_listing_names_what_lost_its_recipe(self, wired):
        await call("sfx_generate", kind="hit", name="thump")
        (sfx.sfx_dir(wired) / "thump.synth.json").unlink()
        got = await call("sfx_list")
        assert got["without_recipe"] == ["thump"]
