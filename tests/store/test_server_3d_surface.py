"""The 3D pipeline's MCP surface: does a verdict survive, and does anything land?

Three defects, one theme — the 3D tools produced facts nobody downstream could
read. A turnaround stated WHY it failed per frame and the normalizer replaced
that with "the call failed without stating a reason", which reads as a broken
tool rather than as hot lights. The frames themselves came back as paths, so
"LOOK AT THE ASSET" was an instruction with no mechanism behind it. And combine,
texture and turnaround registered no artifact at all, which is why art_qa_verdict
and the dashboard could not see a 3D asset.

Everything that can be is dispatched through FastMCP rather than by calling the
plain function: the image content, the failure shape and the project binding all
live in the `_tool` decorator, so a direct call tests code no client reaches.

Nothing here touches Blender. The adapter is monkeypatched with the shapes it
really returns.
"""
from __future__ import annotations

import json

import pytest

from bgate_core.store import artifacts
from bgate_mcp import server


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    for var in ("BGATE_ACTOR", "BGATE_SEAT", "BGATE_WORK_ITEM"):
        monkeypatch.delenv(var, raising=False)
    return root


async def call(tool: str, /, **kwargs) -> dict:
    """The JSON payload a client would decode.

    The payload is the LAST block now: a tool may put image content in front of
    it, and a helper that reads content[0] would decode a picture.
    """
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[-1]
    return json.loads(block.text) if hasattr(block, "text") else block


async def call_blocks(tool: str, /, **kwargs) -> list:
    """Every content block, in order — what the model is actually handed."""
    result = await server.mcp.call_tool(tool, kwargs)
    return list(result[0] if isinstance(result, tuple) else result)


def _png(path, colour=(40, 90, 120), size=(48, 72)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return str(path)


def _frames(root, *, blown_front=True):
    """A turnaround's `renders`, in the adapter's own shape."""
    out = root / ".bgate_out" / "turn"
    made = []
    for label, degrees in (("front", 0), ("side", 90)):
        failed = blown_front and label == "front"
        made.append({
            "label": label, "degrees": degrees,
            "path": _png(out / f"hero_{label}.png",
                         (255, 255, 255) if failed else (40, 90, 120)),
            "exists": True, "checked": True,
            "blown": 0.91 if failed else 0.01,
            "mean": 251.0 if failed else 96.0,
            "ok": not failed,
            "verdict": ("blown out — 91% of the frame is pure white; the lights "
                        "are too hot to judge what you are looking at")
                       if failed else "",
        })
    return made


def _turnaround(frames, **extra):
    unreadable = [f for f in frames if not f["ok"]]
    return {"ok": not unreadable, "renders": frames, "unreadable": unreadable,
            **extra}


# ---------------------------------------------------------------------------
# A stated reason is never laundered into a generic failure
# ---------------------------------------------------------------------------
def test_a_per_frame_verdict_survives_normalisation():
    """The whole defect: ok=False, no top-level reason, and the one mechanical
    signal built to stop the agent sitting in renders[i]['verdict']."""
    got = server._normalize({
        "ok": False,
        "renders": [{"label": "front", "ok": False, "verdict": "blown out — 91%"},
                    {"label": "side", "ok": True, "verdict": ""}],
    })

    assert got["ok"] is False
    assert "blown out" in got["error"]
    assert "without stating a reason" not in got["error"]


def test_the_verdict_names_the_frame_that_failed():
    """A reason that does not say WHICH frame sends the agent to re-render all
    four — the label is the difference between a fix and a rerun."""
    got = server._normalize({"ok": False, "renders": [
        {"label": "back", "ok": False, "verdict": "too dark to read"}]})

    assert got["error"].startswith("back: ")


def test_the_frames_that_passed_stay_quiet():
    """Only entries claiming failure contribute; three good frames must not pad
    the reason with their silence or their success."""
    got = server._normalize({"ok": False, "renders": [
        {"label": "front", "ok": True, "verdict": ""},
        {"label": "side", "ok": True, "verdict": ""},
        {"label": "back", "ok": False, "verdict": "too dark to read"}]})

    assert got["error"] == "back: too dark to read"


def test_a_populated_top_level_error_is_not_overwritten():
    """The adapter joins the frame verdicts into a top-level `error`. The
    normalizer's job there is to leave it exactly alone — including when nested
    reasons exist that it could have preferred."""
    stated = "front: blown out — 91%; back: too dark to read"
    got = server._normalize({"ok": False, "error": stated, "renders": [
        {"label": "front", "ok": False, "verdict": "blown out — 91%"}]})

    assert got["error"] == stated


def test_a_reason_stated_as_a_list_is_joined_not_dropped():
    """The other way a populated reason still got replaced: `_REASON_KEYS` was
    read as `str` only, so an error stated as the list of frame verdicts was
    treated as no reason at all."""
    got = server._normalize({"ok": False,
                             "error": ["front: blown out", "back: too dark"]})

    assert got["error"] == "front: blown out; back: too dark"


def test_a_nested_reason_is_deduplicated_across_keys():
    """turnaround reports the same failing frame twice — in `renders` and again
    in `unreadable`. Saying it twice reads as two broken frames."""
    frame = {"label": "front", "ok": False, "verdict": "blown out — 91%"}
    got = server._normalize({"ok": False, "renders": [frame], "unreadable": [frame]})

    assert got["error"].count("blown out") == 1


def test_a_failure_with_nothing_stated_anywhere_still_gets_a_reason():
    """The generic string is the last resort and must stay reachable — a result
    that really says nothing must not come back with an empty error."""
    got = server._normalize({"ok": False, "stage": "poses"})

    assert got["error"] and "without stating a reason" in got["error"]


def test_success_payloads_with_failing_children_are_left_alone():
    """bgate_doctor answers 'what is installed' successfully while listing rows
    that are unavailable. Reaching into those would invent a failure."""
    report = {"blender": {"available": False, "reason": "not found"}}

    assert server._normalize(report) == report


@pytest.mark.anyio
async def test_turnaround_reports_the_verdict_through_the_tool(wired, monkeypatch):
    """End to end, the way the model receives it: the failure the tool exists to
    produce arrives as the failure, not as a broken-tool message."""
    monkeypatch.setattr(server._blender, "turnaround",
                        lambda *a, **k: _turnaround(_frames(wired)))

    got = await call("blender_turnaround", model="hero.glb",
                     out_dir=str(wired / ".bgate_out" / "turn"), stem="hero")

    assert got["ok"] is False
    assert "blown out" in got["error"] and "front" in got["error"]


# ---------------------------------------------------------------------------
# The frames reach the model as pixels
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_turnaround_hands_back_the_frames_as_image_content(wired, monkeypatch):
    """'LOOK AT THE ASSET' was prose on a tool that returned four paths and two
    floats — nothing in the transport ever carried a pixel, so the instruction
    could only be obeyed by an agent that believed it had."""
    monkeypatch.setattr(server._blender, "turnaround",
                        lambda *a, **k: _turnaround(_frames(wired)))

    blocks = await call_blocks("blender_turnaround", model="hero.glb",
                               out_dir=str(wired / ".bgate_out" / "turn"),
                               stem="hero")

    images = [b for b in blocks if getattr(b, "type", "") == "image"]
    assert len(images) == 2
    assert all(b.mimeType.startswith("image/") and b.data for b in images)
    # And the JSON is still there, still last, unchanged in shape.
    assert json.loads(blocks[-1].text)["renders"]


@pytest.mark.anyio
async def test_a_frame_that_was_never_written_is_not_offered_as_an_image(
        wired, monkeypatch):
    frames = _frames(wired)
    frames[1] = {**frames[1], "exists": False, "path": str(wired / "gone.png")}
    monkeypatch.setattr(server._blender, "turnaround",
                        lambda *a, **k: _turnaround(frames))

    blocks = await call_blocks("blender_turnaround", model="hero.glb",
                               out_dir=str(wired / ".bgate_out" / "turn"),
                               stem="hero")

    assert len([b for b in blocks if getattr(b, "type", "") == "image"]) == 1


@pytest.mark.anyio
async def test_an_undecodable_frame_never_costs_the_result(wired, monkeypatch):
    """A picture is a bonus. A frame Pillow chokes on must not turn a finished
    render into a tool error."""
    broken = wired / ".bgate_out" / "turn" / "hero_front.png"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not a png")
    frames = [{"label": "front", "degrees": 0, "path": str(broken), "exists": True,
               "checked": True, "blown": 0.0, "mean": 90.0, "ok": True,
               "verdict": ""}]
    monkeypatch.setattr(server._blender, "turnaround",
                        lambda *a, **k: _turnaround(frames))

    got = await call("blender_turnaround", model="hero.glb",
                     out_dir=str(wired / ".bgate_out" / "turn"), stem="hero")

    assert got["ok"] is True and "error" not in got


# ---------------------------------------------------------------------------
# 3D assets reach the artifact ledger, which is what the QA gate reads
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_every_turnaround_frame_becomes_an_artifact(wired, monkeypatch):
    monkeypatch.setattr(server._blender, "turnaround",
                        lambda *a, **k: _turnaround(_frames(wired)))

    got = await call("blender_turnaround", model="hero.glb",
                     out_dir=str(wired / ".bgate_out" / "turn"), stem="hero")

    assert len(got["artifact_ids"]) == 2
    names = {artifacts.get(wired, i)["logical_name"] for i in got["artifact_ids"]}
    assert names == {"hero-front", "hero-side"}
    # The verdict travels with the artifact, so a reviewer opening it later is
    # looking at the same measurement the tool made.
    front = next(a for a in (artifacts.get(wired, i) for i in got["artifact_ids"])
                 if a["logical_name"] == "hero-front")
    assert front["metadata"]["readable"] is False
    assert "blown out" in front["metadata"]["verdict"]
    assert front["metadata"]["preview"]          # archived to the gallery too


@pytest.mark.anyio
async def test_a_re_render_is_a_revision_of_the_same_angle(wired, monkeypatch):
    """One logical name per angle: fixing the lights and rendering again must
    supersede the white frame, not sit beside it as an unrelated asset."""
    monkeypatch.setattr(server._blender, "turnaround",
                        lambda *a, **k: _turnaround(_frames(wired)))
    await call("blender_turnaround", model="hero.glb",
               out_dir=str(wired / ".bgate_out" / "turn"), stem="hero")

    monkeypatch.setattr(server._blender, "turnaround",
                        lambda *a, **k: _turnaround(_frames(wired, blown_front=False)))
    again = await call("blender_turnaround", model="hero.glb",
                       out_dir=str(wired / ".bgate_out" / "turn"), stem="hero")

    revisions = [artifacts.get(wired, i)["revision"] for i in again["artifact_ids"]]
    assert revisions == [2, 2]


@pytest.mark.anyio
async def test_art_qa_can_be_pointed_at_a_turnaround_frame(wired, monkeypatch):
    """The point of registering at all: the existing gate now applies to 3D.
    Before this, art_qa_verdict(artifact_id=...) had nothing to name."""
    monkeypatch.setattr(server._blender, "turnaround",
                        lambda *a, **k: _turnaround(_frames(wired)))
    got = await call("blender_turnaround", model="hero.glb",
                     out_dir=str(wired / ".bgate_out" / "turn"), stem="hero")

    verdict = await call("art_qa_verdict", artifact_id=got["artifact_ids"][0],
                         verdict="fail", score=10, reasons="blown out")

    assert verdict["ok"] is True and verdict["status"] == "rejected"


@pytest.mark.anyio
async def test_frames_outside_the_project_say_so_instead_of_going_quiet(
        wired, tmp_path_factory, monkeypatch):
    """An artifact cannot be recorded for a file outside the root. Silently
    registering nothing is how a whole turnaround misses the gate unnoticed."""
    outside = tmp_path_factory.mktemp("elsewhere")
    frames = [{"label": "front", "degrees": 0,
               "path": _png(outside / "hero_front.png"), "exists": True,
               "checked": True, "blown": 0.0, "mean": 90.0, "ok": True,
               "verdict": ""}]
    monkeypatch.setattr(server._blender, "turnaround",
                        lambda *a, **k: _turnaround(frames))

    got = await call("blender_turnaround", model="hero.glb",
                     out_dir=str(outside), stem="hero")

    assert "artifact_ids" not in got
    assert "outside the project root" in got["artifact_note"]


@pytest.mark.anyio
async def test_blender_combine_registers_the_assembled_asset(wired, monkeypatch):
    """The assembled .glb was the one art output with no artifact, which is why
    consistency_check and art_qa_verdict could not see 3D at all."""
    out = wired / "out" / "hero.glb"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"glTF-ish")

    def fake_combine(parts, out_path, **kw):
        return {"ok": True, "armature": "Rig", "checks": [], "warnings": [],
                "manifest": str(out) + ".manifest.json", "layers": 2,
                "parts": [{"name": "body", "source": "out/body.glb", "tris": 900,
                           "objects": ["Body"], "bound": "deform",
                           "decal_on": "", "imported": True},
                          {"name": "cap", "source": "out/cap.glb", "tris": 120,
                           "objects": ["Cap"], "bound": "bone:Head",
                           "decal_on": "", "imported": True}]}

    monkeypatch.setattr(server._blender, "combine", fake_combine)

    got = await call("blender_combine", parts=["out/body.glb", "out/cap.glb"],
                     out_path=str(out), rig="body", root_name="hero")

    artifact = artifacts.get(wired, got["artifact_id"])
    assert artifact["logical_name"] == "hero"
    assert artifact["producer"] == "blender_combine"
    assert artifact["metadata"]["layers"] == ["body", "cap"]
    assert artifact["metadata"]["tris"] == 1020
    assert artifact["status"] == "candidate"     # a candidate, not an approval


@pytest.mark.anyio
async def test_blender_texture_registers_the_textured_layer(wired, monkeypatch):
    out = wired / "out" / "cap_textured.glb"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"glTF-ish")
    texture = _png(wired / ".bgate_out" / "art" / "cap.png")

    monkeypatch.setattr(server._blender, "apply_texture",
                        lambda model, image, out_path, **kw: {
                            "ok": True, "textured": ["CapMat"], "unwrapped": [],
                            "out_path": str(out_path)})

    got = await call("blender_texture", model="out/cap.glb", image=texture,
                     out_path=str(out))

    artifact = artifacts.get(wired, got["artifact_id"])
    assert artifact["logical_name"] == "cap_textured"
    assert artifact["metadata"]["texture"] == texture
    assert artifact["metadata"]["textured"] == ["CapMat"]


@pytest.mark.anyio
async def test_a_failed_assembly_registers_nothing(wired, monkeypatch):
    """Registration follows a successful run. Putting a failed export on the
    ledger gives a reviewer a file that may not exist."""
    monkeypatch.setattr(server._blender, "combine",
                        lambda *a, **k: {"ok": False, "error": "layer imported nothing",
                                         "parts": []})

    got = await call("blender_combine", parts=["out/body.glb"],
                     out_path=str(wired / "out" / "hero.glb"))

    assert got["ok"] is False and "artifact_id" not in got
    assert artifacts.list_revisions(wired) == []
