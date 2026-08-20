"""The tool surface for capability the adapters already had and nobody could reach.

Three defects, one theme — work was BUILT and then left unwired, so the briefs
that describe it were false in the only place an agent can check: the tool
signature.

  * every art seat brief says to generate "conditioned on the pinned refs", and
    image_generate took no reference of any kind. The instruction could only be
    obeyed by switching to image_edit, or by believing it had been.
  * blender_texture exposed one image and no PBR at all, so `roughness`,
    `metallic`, `normal`, `emission` and the alpha mode that makes a decal
    export as MASK existed in the adapter and had no caller.
  * "re-run that one layer, not the character" was the promise the layered 3D
    path is built on. manifest_recipe could answer it; nothing asked.

And one contradiction: chroma.clause hard-coded "NO text" into the keyable
background contract, which is why a decal — an asset that is ENTIRELY text —
could not be keyed and had to fall back to asking the API for transparency,
which no provider grants.

Nothing here spends money, launches Blender or opens a socket. The adapters are
monkeypatched with the shapes they really return, and every tool is dispatched
through FastMCP rather than called directly — the project binding and the
failure shape live in the `_tool` decorator, so a direct call tests code no
client reaches.
"""
from __future__ import annotations

import json
import re

import pytest

from bgate_core import artifacts, chroma
from bgate_mcp import server

# "NO text" as a WHOLE phrase. The contract also says "NO texture", and a naive
# substring test passes on that — which would make this file agree with itself
# while the ban it exists to catch was still in the prompt.
NO_TEXT = re.compile(r"\bNO text\b")


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    for var in ("BGATE_ACTOR", "BGATE_SEAT", "BGATE_WORK_ITEM"):
        monkeypatch.delenv(var, raising=False)
    # The paid tools now ask the provider gateway before touching an adapter,
    # and a keyless test environment is HONESTLY unroutable — but this file's
    # charter is the arguments that cross the chroma boundary, not the gate
    # in front of it (test_gateway owns that). Wave every call through.
    from bgate_core import gateway
    monkeypatch.setattr(gateway, "pick", lambda root_, cap: {
        "provider": "kie", "alternatives": [], "why": "stubbed for wiring"})
    return root


async def call(tool: str, /, **kwargs) -> dict:
    """The JSON payload a client would decode (always the LAST content block)."""
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[-1]
    return json.loads(block.text) if hasattr(block, "text") else block


def _png(path, colour=(120, 60, 40), size=(32, 32)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return str(path)


@pytest.fixture()
def spent(monkeypatch):
    """Capture what the server hands chroma.generate, and spend nothing.

    ok=False stops before keying, auditing and artifact registration — this file
    is about the arguments that cross the boundary, not about what comes back.
    """
    seen: dict = {}

    def fake(prompt, out_path, **kwargs):
        seen.update(prompt=prompt, out_path=str(out_path), **kwargs)
        return {"ok": False, "error": "stopped before spending"}

    monkeypatch.setattr(server._chroma, "generate", fake)
    return seen


# ---------------------------------------------------------------------------
# image_generate can be conditioned at all
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_the_old_call_is_untouched(wired, spent):
    """Every existing caller passes prompt/filename/size/quality and nothing
    else. The new parameters must default to exactly what happened before:
    no references, no kind, and keying decided by `transparent`."""
    await call("image_generate", prompt="a title card", filename="title.png")

    assert spent["ref_paths"] == []
    assert spent["anchors"] == []
    assert spent["task_kind"] == ""
    assert spent["tileable"] is False
    assert spent["keyed"] in (None, False)   # None -> needs_key("") -> False
    assert spent["transparent"] is False     # never the API's own alpha


@pytest.mark.anyio
async def test_references_reach_chroma(wired, spent, tmp_path):
    ref = _png(tmp_path / "anchor.png")

    await call("image_generate", prompt="the same fighter, side on",
               filename="side.png", ref_images=[ref])

    assert spent["ref_paths"] == [ref]


@pytest.mark.anyio
async def test_a_pinned_name_is_accepted_where_a_path_is(wired, spent, tmp_path):
    """The pin is the thing the brief names; a path is what the agent would have
    had to go and look up."""
    pinned = await call("ref_pin", name="hero", path=_png(tmp_path / "hero.png"),
                        kind="character")

    await call("image_generate", prompt="the hero, running", filename="run.png",
               ref_images=["hero"])

    assert spent["ref_paths"] == [pinned["path"]]


@pytest.mark.anyio
async def test_pinned_refs_reach_the_tool_with_no_path_passed_by_hand(
        wired, spent, tmp_path):
    """THE DEFECT THIS FILE IS NAMED FOR. The agent states which anchors it
    wants by KIND; the tool already knows where they live."""
    hero = await call("ref_pin", name="hero", path=_png(tmp_path / "h.png"),
                      kind="character")
    await call("ref_pin", name="palette", path=_png(tmp_path / "p.png"),
               kind="style")

    got = await call("image_generate", prompt="the hero, mid-swing",
                     filename="swing.png", use_pinned="character")

    assert spent["ref_paths"] == [hero["path"]]      # the style pin is not one
    assert got["refs_used"] == ["hero"]


@pytest.mark.anyio
async def test_use_pinned_all_takes_every_anchor(wired, spent, tmp_path):
    await call("ref_pin", name="hero", path=_png(tmp_path / "h.png"),
               kind="character")
    await call("ref_pin", name="palette", path=_png(tmp_path / "p.png"),
               kind="style")

    await call("image_generate", prompt="a matching prop", filename="prop.png",
               use_pinned="all")

    assert len(spent["ref_paths"]) == 2


@pytest.mark.anyio
async def test_an_explicit_ref_is_not_duplicated_by_the_pull(wired, spent,
                                                             tmp_path):
    hero = await call("ref_pin", name="hero", path=_png(tmp_path / "h.png"),
                      kind="character")

    await call("image_generate", prompt="the hero again", filename="again.png",
               ref_images=["hero"], use_pinned="character")

    assert spent["ref_paths"] == [hero["path"]]


@pytest.mark.anyio
async def test_a_kind_with_no_pins_says_so_rather_than_generating_blind(
        wired, spent, tmp_path):
    """Silently generating unconditioned art LOOKS like a result, and costs the
    same as one."""
    await call("ref_pin", name="palette", path=_png(tmp_path / "p.png"),
               kind="style")

    got = await call("image_generate", prompt="the hero", filename="h.png",
                     use_pinned="character")

    assert got["ok"] is False
    assert "character" in got["error"] and "style" in got["error"]
    assert not spent          # refused before the provider was ever called


@pytest.mark.anyio
async def test_anchors_steer_the_key_colour_without_being_sent_to_the_model(
        wired, spent, tmp_path):
    """An anchor here is palette evidence — the greens the chroma must avoid —
    not another image for the model to copy."""
    anchor = _png(tmp_path / "identity.png")

    await call("image_generate", prompt="a matching icon", filename="icon.png",
               anchors=[anchor], task_kind="icon")

    assert spent["anchors"] == [anchor]
    assert spent["ref_paths"] == []


# ---------------------------------------------------------------------------
# task_kind and tileable, which change decisions rather than wording
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_task_kind_and_tileable_reach_chroma(wired, spent):
    await call("image_generate", prompt="mossy stone", filename="moss.png",
               task_kind="texture", tileable=True)

    assert spent["task_kind"] == "texture"
    assert spent["tileable"] is True


@pytest.mark.anyio
async def test_a_texture_is_not_keyed_by_the_tool(wired, spent):
    """The surface IS the whole frame; keying one hands back an empty file. The
    tool must not decide this itself — it defers to the kind."""
    await call("image_generate", prompt="brushed steel", filename="steel.png",
               task_kind="texture")

    assert spent["keyed"] is None          # chroma.needs_key("texture") is False


@pytest.mark.anyio
async def test_a_decal_needs_no_transparent_flag(wired, spent):
    """`transparent=True` was the workaround for decal being locked off the
    keyed path. The kind alone is now enough, and the flag stays un-passed."""
    await call("image_generate", prompt="the team wordmark", filename="logo.png",
               task_kind="decal")

    assert spent["keyed"] is None          # chroma.needs_key("decal") is True
    assert spent["transparent"] is False
    assert chroma.needs_key("decal") is True


@pytest.mark.anyio
async def test_transparent_still_forces_the_keyed_path(wired, spent):
    """The old flag keeps its old meaning for the kinds that have no task_kind."""
    await call("image_generate", prompt="a floating rune", filename="rune.png",
               transparent=True)

    assert spent["keyed"] is True


# ---------------------------------------------------------------------------
# The decal contradiction, at the layer that held it
# ---------------------------------------------------------------------------
class TestDecalOnTheKeyedPath:
    """A logo is sprite-shaped — it composites onto a cap — so it needs real
    alpha. It could not have it while the contract that manufactures alpha
    forbade the only thing a logo is."""

    @pytest.fixture()
    def provider(self, monkeypatch):
        seen: dict = {}

        def fake(prompt, out_path, **kwargs):
            seen.update(prompt=prompt, **kwargs)
            return {"ok": False, "error": "stopped before spending"}

        monkeypatch.setattr("bgate_adapters.imagegen.generate", fake)
        return seen

    def test_the_keyed_contract_no_longer_bans_the_subject(self, provider,
                                                           tmp_path):
        chroma.generate("the Ironsides wordmark", tmp_path / "logo.png",
                        provider="openai", task_kind="decal")

        assert NO_TEXT.search(provider["prompt"]) is None

    def test_and_it_demands_the_lettering_instead(self, provider, tmp_path):
        chroma.generate("the Ironsides wordmark", tmp_path / "logo.png",
                        provider="openai", task_kind="decal")

        text = provider["prompt"].lower()
        assert "is the graphic" in text
        assert "background only" in text

    def test_it_still_gets_the_whole_background_mechanism(self, provider,
                                                          tmp_path):
        """Scoping the ban must not soften the contract — the audit still has
        to be able to cut this cleanly."""
        chroma.generate("the Ironsides wordmark", tmp_path / "logo.png",
                        provider="openai", task_kind="decal")

        text = provider["prompt"].lower()
        for demand in ("completely flat", "uniform", "single solid",
                       "no gradient", "no vignette", "inside the frame"):
            assert demand in text, demand

    def test_the_decal_never_also_asks_the_api_for_alpha(self, provider,
                                                         tmp_path):
        chroma.generate("the Ironsides wordmark", tmp_path / "logo.png",
                        provider="openai", task_kind="decal", transparent=True)

        assert provider["transparent"] is False

    def test_a_character_still_gets_the_ban(self, provider, tmp_path):
        """The variant is scoped to the kinds whose subject IS text. A paladin
        with a caption is still a defect."""
        chroma.generate("a paladin", tmp_path / "p.png", provider="openai",
                        task_kind="anchor")

        assert NO_TEXT.search(provider["prompt"]) is not None

    @pytest.mark.parametrize("alias", ["decal", "logo", "insignia", "emblem",
                                       "sticker"])
    def test_every_decal_alias_is_keyed(self, alias):
        assert chroma.needs_key(alias) is True
        assert chroma.text_is_subject(alias) is True

    def test_a_plate_kind_is_untouched(self):
        assert chroma.needs_key("background") is False
        assert chroma.text_is_subject("anchor") is False


# ---------------------------------------------------------------------------
# blender_texture: every map the adapter takes
# ---------------------------------------------------------------------------
@pytest.fixture()
def textured(monkeypatch):
    """Capture the apply_texture call. Returns the adapter's real success shape."""
    seen: dict = {}

    def fake(model, image, out_path, **kwargs):
        seen.update(model=model, image=image, out_path=str(out_path), **kwargs)
        return {"ok": True, "textured": ["CapMat"], "unwrapped": [],
                "alpha": kwargs.get("alpha"), "out_path": str(out_path)}

    monkeypatch.setattr(server._blender, "apply_texture", fake)
    return seen


def _layer(root, name="cap_textured.glb"):
    out = root / "out" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"glTF-ish")
    return out


@pytest.mark.anyio
async def test_every_pbr_map_is_forwarded(wired, textured, tmp_path):
    maps = {kind: _png(tmp_path / f"{kind}.png")
            for kind in ("albedo", "roughness", "metallic", "normal", "emission")}

    await call("blender_texture", model="out/cap.glb", image=maps["albedo"],
               out_path=str(_layer(wired)), roughness=maps["roughness"],
               metallic=maps["metallic"], normal=maps["normal"],
               emission=maps["emission"], normal_strength=0.4)

    assert textured["image"] == maps["albedo"]
    for kind in ("roughness", "metallic", "normal", "emission"):
        assert textured[kind] == maps[kind], kind
    assert textured["normal_strength"] == 0.4


@pytest.mark.anyio
async def test_an_omitted_map_is_absent_not_empty(wired, textured, tmp_path):
    """The adapter reads "" as a path and raises FileNotFoundError on it; None
    is how you say "no map"."""
    await call("blender_texture", model="out/cap.glb",
               image=_png(tmp_path / "albedo.png"), out_path=str(_layer(wired)))

    assert textured["roughness"] is None and textured["normal"] is None


@pytest.mark.anyio
async def test_maps_can_be_applied_without_a_base_colour(wired, textured,
                                                         tmp_path):
    """Adding roughness to an already-coloured layer must not force a re-paint
    of its albedo."""
    await call("blender_texture", model="out/cap.glb", image="",
               out_path=str(_layer(wired)),
               roughness=_png(tmp_path / "rough.png"))

    assert textured["image"] is None
    assert textured["roughness"]


@pytest.mark.anyio
async def test_the_alpha_mode_and_decal_flag_are_forwarded(wired, textured,
                                                           tmp_path):
    """MEASURED: without alpha="clip" the decal exports OPAQUE and ships as a
    solid rectangle of key colour glued over the cap."""
    await call("blender_texture", model="out/cap.glb",
               image=_png(tmp_path / "logo.png"), out_path=str(_layer(wired)),
               alpha="clip", alpha_cutoff=0.35, decal=True)

    assert textured["alpha"] == "clip"
    assert textured["alpha_cutoff"] == 0.35
    assert textured["decal"] is True


@pytest.mark.anyio
async def test_all_slots_is_off_unless_asked_for(wired, textured, tmp_path):
    """It used to be the DEFAULT, which painted skin, eyes and mouth with one
    image and called the layer textured."""
    await call("blender_texture", model="out/body.glb",
               image=_png(tmp_path / "skin.png"), out_path=str(_layer(wired)))

    assert textured["all_slots"] is False


@pytest.mark.anyio
async def test_all_slots_travels_when_it_is(wired, textured, tmp_path):
    await call("blender_texture", model="out/body.glb",
               image=_png(tmp_path / "skin.png"), out_path=str(_layer(wired)),
               all_slots=True, material="")

    assert textured["all_slots"] is True


@pytest.mark.anyio
async def test_the_artifact_records_which_maps_produced_the_surface(
        wired, textured, tmp_path):
    """A reviewer judging a surface has to be able to reach the images it was
    made from — one of them is now four."""
    albedo = _png(tmp_path / "albedo.png")
    rough = _png(tmp_path / "rough.png")

    got = await call("blender_texture", model="out/cap.glb", image=albedo,
                     out_path=str(_layer(wired)), roughness=rough, alpha="clip")

    artifact = artifacts.get(wired, got["artifact_id"])
    assert artifact["metadata"]["maps"] == {"base_color": albedo,
                                            "roughness": rough}
    assert artifact["metadata"]["alpha"] == "clip"


# ---------------------------------------------------------------------------
# blender_layer_rerun: the promise the manifest was already keeping
# ---------------------------------------------------------------------------
def _recipe_part(name, path, **extra):
    return {"name": name, "path": str(path), "at": [0.0, 0.0, 0.0],
            "rotate": [0.0, 0.0, 0.0], "scale": 1.0, "bind": "deform",
            "decal_on": "", **extra}


def _assembled(root, *, cap_script="bg_ball('Cap')", cap_on_disk=True,
               body_on_disk=True):
    """An assembled asset with its manifest, exactly as combine() writes it."""
    from bgate_adapters import blender

    out = root / "out" / "hero.glb"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"glTF-ish")
    body, cap = out.parent / "body.glb", out.parent / "cap.glb"
    if body_on_disk:
        body.write_bytes(b"glTF-ish")
    if cap_on_disk:
        cap.write_bytes(b"glTF-ish")

    recipe = [_recipe_part("body", body),
              _recipe_part("cap", cap, bind="bone:Head", at=[0.0, 0.0, 1.6])]
    result = {
        "ok": True, "armature": "Skeleton", "rig": "body", "root_name": "hero",
        "checks": [], "warnings": [],
        "parts": [
            {"name": "body", "objects": ["Body"], "tris": 900, "bound": "deform",
             "decal_on": "", "source": str(body), "at": [0.0, 0.0, 0.0],
             "rotate": [0.0, 0.0, 0.0], "scale": 1.0, "bind": "deform",
             "is_rig": True, "script": "bg_box('Body')"},
            {"name": "cap", "objects": ["Cap"], "tris": 120, "bound": "bone:Head",
             "decal_on": "", "source": str(cap), "at": [0.0, 0.0, 1.6],
             "rotate": [0.0, 0.0, 0.0], "scale": 1.0, "bind": "bone:Head",
             "is_rig": False, "script": cap_script},
        ],
    }
    blender.write_manifest(out, result, recipe=recipe)
    return out


@pytest.fixture()
def rebuilt(monkeypatch):
    """Blender, stubbed at both doors: the layer script and the assembly."""
    seen: dict = {"scripts": [], "combines": []}

    def fake_run_script(script, **kwargs):
        seen["scripts"].append({"script": script, **kwargs})
        glb = kwargs.get("export_glb")
        if glb:
            from pathlib import Path

            Path(glb).parent.mkdir(parents=True, exist_ok=True)
            Path(glb).write_bytes(b"rebuilt glTF")
        return {"ok": True, "seconds": 1.2, "print": ""}

    def fake_combine(parts, out_path, **kwargs):
        seen["combines"].append({"parts": parts, "out_path": str(out_path),
                                 **kwargs})
        return {"ok": True, "armature": "Skeleton", "checks": [], "warnings": [],
                "manifest": str(out_path) + ".manifest.json", "layers": len(parts),
                "parts": [{"name": part["name"], "source": part["path"],
                           "objects": [part["name"].title()],
                           "tris": 260 if part["name"] == "cap" else 900,
                           "bound": part["bind"], "decal_on": "", "imported": True}
                          for part in parts]}

    monkeypatch.setattr(server._blender, "run_script", fake_run_script)
    monkeypatch.setattr(server._blender, "combine", fake_combine)
    return seen


@pytest.mark.anyio
async def test_one_layer_is_rebuilt_and_the_asset_reassembled(wired, rebuilt):
    """The whole point: a new script for the cap, and the body is not touched."""
    asset = _assembled(wired)

    got = await call("blender_layer_rerun", asset=str(asset), layer="cap",
                     script="bg_cyl('Cap', radius=0.14)")

    assert got["ok"] is True and got["rebuilt"] == "script"
    assert len(rebuilt["scripts"]) == 1
    ran = rebuilt["scripts"][0]
    assert ran["script"] == "bg_cyl('Cap', radius=0.14)"
    assert ran["export_glb"].endswith("cap.glb")     # over the layer's own file
    assert ran["kit"] is True


@pytest.mark.anyio
async def test_the_recorded_placement_survives_the_rerun(wired, rebuilt):
    """A layer put back at the origin, unrotated and unbound, is a different
    asset — which is exactly what re-typing the arguments produces."""
    asset = _assembled(wired)

    await call("blender_layer_rerun", asset=str(asset), layer="cap",
               script="bg_cyl('Cap')")

    combined = rebuilt["combines"][0]
    cap = next(p for p in combined["parts"] if p["name"] == "cap")
    assert cap["at"] == [0.0, 0.0, 1.6]
    assert cap["bind"] == "bone:Head"
    assert combined["rig"] == "body" and combined["root_name"] == "hero"
    assert [p["name"] for p in combined["parts"]] == ["body", "cap"]


@pytest.mark.anyio
async def test_a_swept_layer_is_rebuilt_from_its_recorded_script(wired, rebuilt):
    """After blender_sweep the layer files are gone; the script that built each
    one is what the manifest keeps so this is recoverable at all."""
    asset = _assembled(wired, cap_script="bg_ball('Cap', radius=0.2)",
                       cap_on_disk=False)

    got = await call("blender_layer_rerun", asset=str(asset), layer="cap")

    assert got["ok"] is True and got["rebuilt"] == "script"
    assert rebuilt["scripts"][0]["script"] == "bg_ball('Cap', radius=0.2)"


@pytest.mark.anyio
async def test_a_replacement_file_is_used_in_place(wired, rebuilt):
    asset = _assembled(wired)
    replacement = wired / "out" / "cap_v2.glb"
    replacement.write_bytes(b"glTF-ish")

    got = await call("blender_layer_rerun", asset=str(asset), layer="cap",
                     source=str(replacement))

    assert got["rebuilt"] == "file"
    assert rebuilt["scripts"] == []          # nothing was run
    cap = next(p for p in rebuilt["combines"][0]["parts"] if p["name"] == "cap")
    assert cap["path"] == str(replacement.resolve())


@pytest.mark.anyio
async def test_it_reports_what_changed(wired, rebuilt):
    """"Did that fix it" has to be a number. The manifest holds the counts from
    before, and combine rewrites the manifest — so they are read first or not
    at all."""
    asset = _assembled(wired)

    got = await call("blender_layer_rerun", asset=str(asset), layer="cap",
                     script="bg_cyl('Cap')")

    assert got["changed"]["tris_before"] == 120
    assert got["changed"]["tris_after"] == 260
    assert got["layer"] == "cap"


@pytest.mark.anyio
async def test_an_unknown_layer_names_the_ones_that_exist(wired, rebuilt):
    asset = _assembled(wired)

    got = await call("blender_layer_rerun", asset=str(asset), layer="hat",
                     script="bg_ball('Hat')")

    assert got["ok"] is False
    assert "body" in got["error"] and "cap" in got["error"]
    assert rebuilt["combines"] == []


@pytest.mark.anyio
async def test_another_missing_layer_stops_the_run_instead_of_losing_it(
        wired, rebuilt):
    """combine would assemble happily around the hole and hand back a character
    with no body — ok=True, one layer short."""
    asset = _assembled(wired, body_on_disk=False)

    got = await call("blender_layer_rerun", asset=str(asset), layer="cap",
                     script="bg_cyl('Cap')")

    assert got["ok"] is False
    assert "body" in got["error"]
    assert rebuilt["combines"] == []


@pytest.mark.anyio
async def test_a_failing_layer_script_never_reaches_the_assembly(wired,
                                                                 monkeypatch):
    asset = _assembled(wired)
    combines = []
    monkeypatch.setattr(server._blender, "run_script",
                        lambda *a, **k: {"ok": False,
                                         "error": "NameError: bg_cylinder"})
    monkeypatch.setattr(server._blender, "combine",
                        lambda *a, **k: combines.append(a) or {"ok": True})

    got = await call("blender_layer_rerun", asset=str(asset), layer="cap",
                     script="bg_cylinder('Cap')")

    assert got["ok"] is False and "bg_cylinder" in got["error"]
    assert got["stage"] == "layer"
    assert combines == []


@pytest.mark.anyio
async def test_the_rerun_is_a_revision_of_the_same_asset(wired, rebuilt):
    """One logical name: the fixed character supersedes the broken one rather
    than sitting beside it as an unrelated asset the reviewer must compare."""
    asset = _assembled(wired)
    first = await call("blender_combine",
                       parts=[{"name": "body", "path": str(asset),
                               "bind": "deform"}],
                       out_path=str(asset), rig="body", root_name="hero")

    again = await call("blender_layer_rerun", asset=str(asset), layer="cap",
                       script="bg_cyl('Cap')")

    assert artifacts.get(wired, first["artifact_id"])["logical_name"] == "hero"
    artifact = artifacts.get(wired, again["artifact_id"])
    assert artifact["logical_name"] == "hero"
    assert artifact["producer"] == "blender_layer_rerun"
    assert artifact["revision"] == 2


@pytest.mark.anyio
async def test_an_asset_with_no_manifest_says_so(wired, rebuilt):
    stray = wired / "out" / "orphan.glb"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"glTF-ish")

    got = await call("blender_layer_rerun", asset=str(stray), layer="cap",
                     script="bg_cyl('Cap')")

    assert got["ok"] is False and "manifest" in got["error"]
