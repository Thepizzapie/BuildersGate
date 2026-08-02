"""The last mile of the 3D path, and the library nobody could find.

Two defects, one theme — capability that EXISTS and that no agent can reach,
because reaching it is a function of the MCP surface and nothing else.

  * `godot.deliver_asset` takes a .glb all the way into the engine: import,
    collider generation, a generated .tscn with the model under a
    CharacterBody3D, a lit preview scene, and a screenshot taken by Godot's own
    renderer. It was not a tool, so the 3D pipeline still declared victory at
    the .glb and the only "look at it" was a Blender render of a Blender scene.
  * `_blender_base` is spliced into the kit, so `bg_human()` and the landmark
    table ARE in scope inside blender_run — and the kit documentation block said
    nothing about them, which for an agent reading a tool schema is the same as
    them not existing.

Nothing here launches Godot. The adapter is monkeypatched with the shape it
really returns, and every tool is dispatched through FastMCP rather than called
directly: the image content, the project binding and the failure shape all live
in the `_tool` decorator, so a direct call tests code no client reaches.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bgate_adapters import _blender_base as blender_base
from bgate_adapters import _blender_kit as blender_kit
from bgate_core import artifacts
from bgate_mcp import server


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    for var in ("BGATE_ACTOR", "BGATE_SEAT", "BGATE_WORK_ITEM"):
        monkeypatch.delenv(var, raising=False)
    return root


async def call(tool: str, /, **kwargs) -> dict:
    """The JSON payload a client would decode — always the LAST content block,
    because this tool puts a picture in front of it."""
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[-1]
    return json.loads(block.text) if hasattr(block, "text") else block


async def call_blocks(tool: str, /, **kwargs) -> list:
    """Every content block, in order — what the model is actually handed."""
    result = await server.mcp.call_tool(tool, kwargs)
    return list(result[0] if isinstance(result, tuple) else result)


def _png(path, colour=(60, 70, 90), size=(64, 40)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return str(path)


# The gate, in the adapter's own shape. The numbers are the ones measured on the
# verified 4.7.1 run: one skeleton, an Idle clip, two blend shapes, a generated
# StaticBody3D/CollisionShape3D.
def _checks(*, sized=True, collided=True):
    return [
        {"check": "loads_in_engine", "required": True, "ok": True,
         "measured": "Node3D"},
        {"check": "has_geometry", "required": True, "ok": True,
         "measured": "4210 tris"},
        {"check": "materials_carry_a_texture", "required": True, "ok": True,
         "measured": "3/3 surfaces textured", "detail": []},
        {"check": "real_world_size", "required": True, "ok": sized,
         "measured": ("1.79 m longest axis" if sized
                      else "2880.0 m longest axis"),
         "detail": ("" if sized else
                    "2880.000 m across — glTF units are METRES, so this is a "
                    "building, not a prop.")},
        {"check": "has_collider", "required": True, "ok": collided,
         "measured": ("1 collision shapes" if collided
                      else "0 collision shapes")},
        {"check": "has_skeleton", "required": False, "ok": True,
         "measured": "1 Skeleton3D"},
        {"check": "has_animations", "required": False, "ok": True,
         "measured": "Idle"},
        {"check": "has_blend_shapes", "required": False, "ok": True,
         "measured": "Smile, Puff"},
    ]


def _engine_view():
    return {"ok": True, "root": "hero", "root_type": "Node3D",
            "total_tris": 4210, "skeleton_count": 1,
            "animations": ["Idle"], "animation_count": 1,
            "blend_shapes": ["Smile", "Puff"], "collider_count": 1,
            "size_check": {"ok": True, "longest_axis_m": 1.79,
                           "metres": [0.6, 1.79, 0.34]}}


@pytest.fixture()
def delivering(monkeypatch):
    """Install a fake deliver_asset and capture what crossed the boundary."""
    seen: dict = {}

    def install(*, sized=True, collided=True, shot=True, stem="hero",
                failure=None):
        def fake(project_dir, glb_path, **kwargs):
            seen.update(project_dir=str(project_dir), glb_path=str(glb_path),
                        **kwargs)
            if failure is not None:
                return dict(failure)
            path = ""
            if shot:
                path = _png(Path(kwargs["screenshot_dir"]) / f"{stem}.png")
            checks = _checks(sized=sized, collided=collided)
            return {
                "ok": all(c["ok"] for c in checks if c["required"]) and shot,
                "res_path": f"res://assets/{stem}.glb",
                "asset_rel": f"assets/{stem}.glb",
                "scene": f"res://scenes/{stem}.tscn",
                "scene_file": f"scenes/{stem}.tscn",
                "preview": f"res://scenes/{stem}_preview.tscn",
                "screenshot": path or None,
                "import_settings": {"ok": True},
                "engine_view": _engine_view(),
                "scene_view": {"ok": True, "collider_count": 1},
                "checks": checks,
                "steps": [{"step": "import", "ok": True, "errors": []},
                          {"step": "screenshot", "ok": bool(shot),
                           "path": path or None,
                           "error": "" if shot else "no screenshot produced"}],
            }

        monkeypatch.setattr(server._godot, "deliver_asset", fake)
        return seen

    return install


# ---------------------------------------------------------------------------
# The tool exists at all — the entire defect, as one assertion
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_delivery_is_on_the_mcp_surface():
    """deliver_asset was built, verified end to end on Godot 4.7.1, and unwired.
    A capability no client can name is one that did not ship."""
    names = {tool.name for tool in await server.mcp.list_tools()}

    assert "godot_deliver_asset" in names


@pytest.mark.anyio
async def test_a_delivery_reports_the_scene_and_the_frame(wired, delivering):
    seen = delivering()

    got = await call("godot_deliver_asset", godot_project=str(wired / "game"),
                     glb=str(wired / "out" / "hero.glb"))

    assert got["ok"] is True and "error" not in got
    assert got["res_path"] == "res://assets/hero.glb"
    assert got["scene"] == "res://scenes/hero.tscn"
    assert got["preview"] == "res://scenes/hero_preview.tscn"
    assert got["engine_view"]["skeleton_count"] == 1
    assert got["engine_view"]["animations"] == ["Idle"]
    assert seen["glb_path"].endswith("hero.glb")
    assert seen["project_dir"] == str(wired / "game")


@pytest.mark.anyio
async def test_the_delivery_options_are_forwarded(wired, delivering):
    """Every one of these changes what lands in the project — a tool that
    accepts them and drops them is worse than one that never offered them."""
    seen = delivering()

    await call("godot_deliver_asset", godot_project=str(wired / "game"),
               glb=str(wired / "out" / "crate.glb"), name="crate",
               dest_rel="assets/props", scene_rel="scenes/props",
               physics="all", shape_type="box", body_type="static",
               character_body="StaticBody3D", at=2.0, max_size_m=12.0,
               min_size_m=0.02, nominal_size_m=0.6, with_camera=True,
               overwrite_scene=True, timeout=420)

    assert seen["with_camera"] is True and seen["overwrite_scene"] is True
    assert seen["name"] == "crate"
    assert seen["dest_rel"] == "assets/props"
    assert seen["scene_rel"] == "scenes/props"
    assert seen["physics"] == "all" and seen["shape_type"] == "box"
    assert seen["character_body"] == "StaticBody3D"
    assert seen["at"] == 2.0
    assert seen["max_size_m"] == 12.0 and seen["nominal_size_m"] == 0.6
    assert seen["timeout"] == 420


@pytest.mark.anyio
async def test_the_size_bound_defaults_to_the_adapters_own_choice(wired,
                                                                  delivering):
    """None is not "no limit" — it is "decide from what the asset IS" (4 m
    skinned, 50 m otherwise). Passing a number here would take that away."""
    seen = delivering()

    await call("godot_deliver_asset", godot_project=str(wired / "game"),
               glb=str(wired / "out" / "hero.glb"))

    assert seen["max_size_m"] is None


@pytest.mark.anyio
async def test_the_tool_defaults_protect_the_players_view_and_the_humans_scene(
        wired, delivering):
    """Both defaults were found by booting a real game. A camera on an
    instanced character decided the level's view (the boot frame was the inside
    of the character's head), and a plain redelivery rewrote the .tscn, which
    destroyed the same hand edit five times in one session. Defaulting either of
    these the other way in the wrapper puts both back."""
    seen = delivering()

    await call("godot_deliver_asset", godot_project=str(wired / "game"),
               glb=str(wired / "out" / "hero.glb"))

    assert seen["with_camera"] is False
    assert seen["overwrite_scene"] is False


@pytest.mark.anyio
async def test_the_frame_is_written_inside_the_project(wired, delivering):
    """An artifact cannot be recorded for a file outside the root, and a frame
    off the ledger is one art QA and the dashboard never see."""
    seen = delivering()

    await call("godot_deliver_asset", godot_project=str(wired / "game"),
               glb=str(wired / "out" / "hero.glb"))

    shot_dir = Path(seen["screenshot_dir"])
    assert shot_dir.is_relative_to(wired / ".bgate_out" / "3d")


# ---------------------------------------------------------------------------
# The frame reaches the model as pixels
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_the_screenshot_comes_back_as_image_content(wired, delivering):
    """The point of the whole step: this is the first time anyone sees the
    asset under the engine's renderer. Handing back a path is not seeing it."""
    delivering()

    blocks = await call_blocks("godot_deliver_asset",
                               godot_project=str(wired / "game"),
                               glb=str(wired / "out" / "hero.glb"))

    images = [b for b in blocks if getattr(b, "type", "") == "image"]
    assert len(images) == 1
    assert images[0].mimeType.startswith("image/") and images[0].data
    # And the JSON is still there, still last, unchanged in shape.
    assert json.loads(blocks[-1].text)["scene"] == "res://scenes/hero.tscn"


@pytest.mark.anyio
async def test_a_delivery_that_took_no_frame_offers_no_image(wired, delivering):
    delivering(shot=False)

    blocks = await call_blocks("godot_deliver_asset",
                               godot_project=str(wired / "game"),
                               glb=str(wired / "out" / "hero.glb"))

    assert [b for b in blocks if getattr(b, "type", "") == "image"] == []
    assert json.loads(blocks[-1].text)["ok"] is False


# ---------------------------------------------------------------------------
# The frame lands on the artifact ledger, which is what the QA gate reads
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_the_in_engine_frame_becomes_an_artifact(wired, delivering):
    delivering()

    got = await call("godot_deliver_asset", godot_project=str(wired / "game"),
                     glb=str(wired / "out" / "hero.glb"))

    artifact = artifacts.get(wired, got["artifact_id"])
    assert artifact["logical_name"] == "hero-in-engine"
    assert artifact["producer"] == "godot_deliver_asset"
    assert artifact["status"] == "candidate"      # a candidate, not an approval
    assert artifact["metadata"]["delivered"] is True
    assert artifact["metadata"]["scene"] == "res://scenes/hero.tscn"
    assert artifact["metadata"]["failed_checks"] == []
    assert artifact["metadata"]["preview"]        # archived to the gallery too
    assert got["screenshot_preview"]


@pytest.mark.anyio
async def test_art_qa_can_be_pointed_at_the_in_engine_frame(wired, delivering):
    """Before this the only 3D thing a reviewer could open was a Blender render
    of a Blender scene, which is the one place the engine's opinion is absent."""
    delivering()
    got = await call("godot_deliver_asset", godot_project=str(wired / "game"),
                     glb=str(wired / "out" / "hero.glb"))

    verdict = await call("art_qa_verdict", artifact_id=got["artifact_id"],
                         verdict="pass", score=80, reasons="stands on the floor")

    assert verdict["ok"] is True
    assert verdict["logical_name"] == "hero-in-engine"
    # A pass is evidence, not an approval — the revision stays a candidate.
    assert verdict["status"] == "candidate" and verdict["awaiting_human"] is True


@pytest.mark.anyio
async def test_a_redelivery_is_a_revision_of_the_same_asset(wired, delivering):
    """One logical name: re-delivering after a fix supersedes the bad frame
    rather than sitting beside it as an unrelated asset."""
    delivering(sized=False)
    await call("godot_deliver_asset", godot_project=str(wired / "game"),
               glb=str(wired / "out" / "hero.glb"))

    delivering()
    again = await call("godot_deliver_asset", godot_project=str(wired / "game"),
                       glb=str(wired / "out" / "hero.glb"))

    artifact = artifacts.get(wired, again["artifact_id"])
    assert artifact["logical_name"] == "hero-in-engine"
    assert artifact["revision"] == 2


# ---------------------------------------------------------------------------
# The failure shape — a gate that hides the asset is one you cannot debug
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_a_failed_gate_still_produces_the_scene_and_the_frame(wired,
                                                                    delivering):
    """MEASURED: a `giant_hero` at 2880 m fails real_world_size and is still
    delivered on purpose. Withholding the screenshot from the one delivery a
    human most needs to look at is the opposite of a gate."""
    delivering(sized=False, stem="giant_hero")

    blocks = await call_blocks("godot_deliver_asset",
                               godot_project=str(wired / "game"),
                               glb=str(wired / "out" / "giant_hero.glb"))
    got = json.loads(blocks[-1].text)

    assert got["ok"] is False
    assert got["scene"] == "res://scenes/giant_hero.tscn"
    assert got["screenshot"]
    assert len([b for b in blocks if getattr(b, "type", "") == "image"]) == 1


@pytest.mark.anyio
async def test_the_failure_names_the_check_that_failed(wired, delivering):
    """Left to the normalizer the reason is whichever nested `detail` it finds
    first — a sentence about metres, not the name of the row to go and fix."""
    delivering(sized=False, collided=False, stem="giant_hero")

    got = await call("godot_deliver_asset", godot_project=str(wired / "game"),
                     glb=str(wired / "out" / "giant_hero.glb"))

    assert "real_world_size" in got["error"]
    assert "has_collider" in got["error"]
    assert "without stating a reason" not in got["error"]


@pytest.mark.anyio
async def test_a_failed_delivery_is_still_registered(wired, delivering):
    """The rejected frame has to be nameable, or the QA gate can only ever be
    shown assets that already passed."""
    delivering(sized=False, stem="giant_hero")

    got = await call("godot_deliver_asset", godot_project=str(wired / "game"),
                     glb=str(wired / "out" / "giant_hero.glb"))

    artifact = artifacts.get(wired, got["artifact_id"])
    assert artifact["logical_name"] == "giant_hero-in-engine"
    assert artifact["metadata"]["delivered"] is False
    assert artifact["metadata"]["failed_checks"] == ["real_world_size"]


@pytest.mark.anyio
async def test_an_asset_the_engine_cannot_load_reports_the_engines_reason(
        wired, delivering):
    """The import failure path returns before there is anything to photograph.
    Nothing is registered, because there is no frame to register."""
    delivering(failure={"ok": False,
                        "error": "the engine could not load the asset",
                        "steps": [{"step": "import", "ok": False,
                                   "errors": ["ERROR: Cannot open file"]}]})

    got = await call("godot_deliver_asset", godot_project=str(wired / "game"),
                     glb=str(wired / "out" / "broken.glb"))

    assert got["ok"] is False
    assert got["error"] == "the engine could not load the asset"
    assert "artifact_id" not in got
    assert artifacts.list_revisions(wired) == []


@pytest.mark.anyio
async def test_a_raising_adapter_is_a_result_not_a_broken_server(wired,
                                                                 monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("Godot not found")

    monkeypatch.setattr(server._godot, "deliver_asset", boom)

    got = await call("godot_deliver_asset", godot_project=str(wired / "game"),
                     glb=str(wired / "out" / "hero.glb"))

    assert got["ok"] is False and "Godot not found" in got["error"]


# ---------------------------------------------------------------------------
# The kit documentation block and the base mesh library cannot drift apart
# ---------------------------------------------------------------------------
#
# `_blender_base` is spliced into KIT, so its functions are genuinely in scope
# inside blender_run. The only thing standing between an agent and them is the
# docstring — so these two tests pull in opposite directions on purpose:
# renaming a function in the library fails the second, and dropping one from the
# docs fails it too, while inventing a helper in prose fails the first.

KIT_DOC = server.blender_run.__doc__ or ""

# The base-mesh entry points the docs are REQUIRED to name. Stated explicitly
# rather than derived: not every def in the library is a thing an agent should
# be reaching for (bg_human_marks and bg_quadruped_proportions are called FOR
# it), and a test that demanded all of them would push the block back to the
# size it was cut down from.
REQUIRED_BASE_NAMES = (
    # start here, or you are inventing a body out of primitives again
    "bg_human", "bg_quadruped", "bg_prop_frame",
    # the shared frame: where things are, and how a layer gets put there
    "bg_proportions", "bg_mark", "bg_fit",
    # the rig, by NAME — combine binds on a string
    "bg_human_chain", "bg_human_skeleton", "bg_roll", "bg_bone", "BG_BONE_NAMES",
    # the unit convention nothing in this pipeline declared before
    "BG_UNIT", "BG_HUMAN_HEIGHT", "BG_GROUND", "BG_FORWARD",
    "bg_unit_check", "bg_unit_assert", "bg_rescale",
    # geometry, binding, and the self-check
    "bg_shell", "bg_weight", "bg_base_report", "bg_base_assert",
    "bg_base_help", "BG_BASE_EXAMPLE",
)


def _named_in_kit_docs() -> set[str]:
    """Every kit identifier the blender_run docstring mentions."""
    return set(re.findall(r"\b(bg_[a-z0-9_]+|BG_[A-Z0-9_]+)\b", KIT_DOC))


def _defined_in(source: str, name: str) -> bool:
    if name.startswith("BG_"):
        return bool(re.search(r"(?m)^%s\s*=" % re.escape(name), source))
    return f"def {name}(" in source


class TestTheKitBlockAndTheLibraryStayInStep:

    def test_the_docs_name_nothing_that_does_not_exist(self):
        """A helper that only exists in prose costs an agent a whole run: it
        writes the call, Blender raises NameError, and the traceback is the
        first anyone hears of it."""
        missing = sorted(name for name in _named_in_kit_docs()
                         if not _defined_in(blender_kit.KIT, name))

        assert missing == [], (
            f"blender_run's docstring names {missing}, which nothing in the "
            "injected kit defines")

    @pytest.mark.parametrize("name", REQUIRED_BASE_NAMES)
    def test_every_required_entry_point_is_documented(self, name):
        """The defect this file exists for: the library was spliced into the
        kit and the docs said nothing, so no agent would ever call it."""
        assert name in _named_in_kit_docs(), (
            f"{name} is in the kit and unmentioned in blender_run's docstring — "
            "an agent reading the tool schema cannot know it exists")

    @pytest.mark.parametrize("name", REQUIRED_BASE_NAMES)
    def test_every_documented_entry_point_is_still_in_the_library(self, name):
        """The other direction. `_blender_base` is the source of truth for the
        spelling; renaming there must break the docs rather than silently
        leaving them describing a function that is gone."""
        source = (blender_kit.KIT if name == "BG_BASE_EXAMPLE"
                  else blender_base.BASE)
        assert _defined_in(source, name)

    def test_the_docs_say_to_start_from_the_base_rather_than_from_primitives(self):
        """The single most useful thing in the block. MEASURED: a cap fitted via
        bg_fit(head_top, "on") rests on the crown at 10% overlap; the same cap at
        a guessed 1.7 m is 89% INSIDE the skull and passed every check the old
        pipeline had."""
        assert "head_top" in KIT_DOC
        assert re.search(r"(?i)\bnot from primitives\b", KIT_DOC)

    def test_the_docs_state_the_limit_of_the_base(self):
        """A blockout described as a character gets shipped as one."""
        lowered = KIT_DOC.lower()
        assert "no face" in lowered and "no fingers" in lowered

    def test_the_block_is_still_a_reference_card_and_not_an_essay(self):
        """It competes for attention with the rest of the brief; the art seat's
        own brief was cut from 1,442 words to 824 for exactly this reason."""
        assert len(KIT_DOC.split()) <= 800, (
            f"the blender_run kit block is {len(KIT_DOC.split())} words — at "
            "that size an agent skims it and writes its own helpers instead")
