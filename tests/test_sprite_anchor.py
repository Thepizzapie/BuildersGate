"""The model sheet: what conditions a pose, and what it costs.

The change under test is a reference-conditioning policy, not a pixel operation,
so it is tested by watching WHAT GETS SENT rather than what comes back. Every
generation is stubbed — no key, no provider, no spend — and the assertions are
about the reference lists the pose calls were built with, because that is the
whole of the mechanism.
"""
from __future__ import annotations

import json

import pytest

from bgate_core import db, project
from bgate_mcp import server


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def root(tmp_path, monkeypatch):
    home = tmp_path / "game"
    home.mkdir()
    project.init(str(home), "AnchorProbe", dimension="2d")
    monkeypatch.setenv("BGATE_ROOT", str(home))
    for var in ("BGATE_SEAT", "BGATE_WORK_ITEM", "BGATE_ACTOR"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture()
def calls(monkeypatch):
    """Stub every generation. Records (task_kind, prompt, refs) per call and
    writes a real keyed PNG so the structural gates downstream are exercised
    against a plausible file rather than skipped."""
    from PIL import Image, ImageDraw

    seen: list[dict] = []

    def fake_generate(prompt, out_path, **kw):
        img = Image.new("RGBA", (240, 360), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle((80, 40, 160, 330),
                                      fill=(200, 60, 40, 255))
        from pathlib import Path

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)
        seen.append({"task_kind": kw.get("task_kind"), "prompt": prompt,
                     "refs": [str(r) for r in (kw.get("ref_paths") or [])],
                     "path": str(out_path)})
        return {"ok": True, "path": str(out_path), "estimated_usd": 0.04,
                "seconds": 1.0, "chroma": {"name": "magenta"}, "alpha": {}}

    monkeypatch.setattr(server._chroma, "generate", fake_generate)
    # The vision judge is an API call and is not what this file is about.
    monkeypatch.setattr(server, "_vision_consistency",
                        lambda *a, **kw: {"ok": True, "frames": [], "min": 100,
                                          "flagged": [], "outliers": []})
    return seen


async def call(tool, /, **kwargs):
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


POSES = [{"name": "walk/0", "description": "contact, left leg leading"},
         {"name": "walk/1", "description": "passing on the right"}]


@pytest.mark.anyio
async def test_the_anchor_is_a_model_sheet_by_default(root, calls):
    """Three views of the character, generated once, off the approved anchor."""
    got = await call("image_sprites", character_prompt="a boxer",
                     poses=POSES, name="boxer", project_dir=str(root))
    assert got.get("ok") is True, got

    sheet = got["model_sheet"]
    assert len(sheet) == 3, got.get("model_sheet_dropped")
    assert sheet[0].endswith("reference.png")
    assert sheet[1].endswith("reference_three_quarter.png")
    assert sheet[2].endswith("reference_profile.png")

    # The extra views derive from the APPROVED anchor, not from the prompt —
    # a second independent generation of "the same character" is a second
    # character, which is the whole reason the anchor exists.
    anchors = [c for c in calls if c["task_kind"] == "anchor"]
    assert len(anchors) == 3
    assert anchors[0]["refs"] == []
    for view in anchors[1:]:
        assert view["refs"] == [sheet[0]]
    assert "three-quarter" in anchors[1]["prompt"]
    assert "side profile" in anchors[2]["prompt"]


@pytest.mark.anyio
async def test_every_pose_is_conditioned_on_all_three_views(root, calls):
    """The point of the change. One front view plus near-copies of it is the
    weak configuration; distinct angles are what carry identity."""
    got = await call("image_sprites", character_prompt="a boxer",
                     poses=POSES, name="boxer", project_dir=str(root))
    assert got.get("ok") is True, got
    sheet = got["model_sheet"]

    pose_calls = [c for c in calls if c["task_kind"] == "animation"]
    assert len(pose_calls) == len(POSES)
    for c in pose_calls:
        assert c["refs"][:3] == sheet, c["refs"]
    # The rolling reference still rides on top of the sheet — the previous
    # frame is continuity, the sheet is identity, and they are not alternatives.
    assert len(pose_calls[1]["refs"]) == 4
    assert pose_calls[1]["refs"][3] == pose_calls[0]["path"]


@pytest.mark.anyio
async def test_anchor_views_1_restores_the_single_view_behaviour(root, calls):
    got = await call("image_sprites", character_prompt="a boxer", poses=POSES,
                     name="boxer", anchor_views=1, project_dir=str(root))
    assert got.get("ok") is True, got
    assert len(got["model_sheet"]) == 1
    assert len([c for c in calls if c["task_kind"] == "anchor"]) == 1
    assert [c for c in calls if c["task_kind"] == "animation"][0]["refs"] == \
        got["model_sheet"]


@pytest.mark.anyio
async def test_a_supplied_anchor_still_gets_its_extra_angles(root, calls,
                                                             tmp_path):
    """An approved ref_image is ONE approved drawing. The other angles are
    derived from it, so they are bought even though the anchor was not."""
    from PIL import Image, ImageDraw

    approved = tmp_path / "approved.png"
    img = Image.new("RGBA", (240, 360), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((80, 40, 160, 330), fill=(200, 60, 40, 255))
    img.save(approved)

    got = await call("image_sprites", character_prompt="a boxer", poses=POSES,
                     name="boxer", ref_image=str(approved),
                     project_dir=str(root))
    assert got.get("ok") is True, got
    assert len(got["model_sheet"]) == 3
    assert got["model_sheet"][0] == str(approved)
    # No anchor was generated; two views were.
    assert len([c for c in calls if c["task_kind"] == "anchor"]) == 2


@pytest.mark.anyio
async def test_the_extra_views_are_priced_before_anything_is_bought(root, calls):
    """The spend gate prices the plan. A model sheet that was not in the
    estimate is an overrun discovered on the invoice."""
    # A ceiling that admits the anchor and the poses but NOT the two extra
    # views: medium poses at 0.042 x2, one high anchor at 0.167 = 0.251; the
    # two extra high views add 0.334.
    got = await call("image_sprites", character_prompt="a boxer", poses=POSES,
                     name="boxer", max_cost_usd=0.30, project_dir=str(root))
    assert got.get("ok") is False
    assert got["stage"] == "spend_gate"
    assert got["estimated_usd"] == pytest.approx(0.585, abs=0.01)
    assert calls == [], "the gate must refuse BEFORE the first call"


@pytest.mark.anyio
async def test_a_view_that_fails_is_dropped_not_fatal(root, calls, monkeypatch):
    """An auxiliary view improves the anchor; it is not part of it. Failing the
    whole set because the profile came back badly would be strictly worse than
    the single-view behaviour this replaces."""
    real = server._chroma.generate
    state = {"n": 0}

    def flaky(prompt, out_path, **kw):
        if kw.get("task_kind") == "anchor":
            state["n"] += 1
            if state["n"] == 3:            # the profile view
                return {"ok": False, "error": "content policy",
                        "estimated_usd": 0.0, "seconds": 0.5}
        return real(prompt, out_path, **kw)

    monkeypatch.setattr(server._chroma, "generate", flaky)
    got = await call("image_sprites", character_prompt="a boxer", poses=POSES,
                     name="boxer", project_dir=str(root))
    assert got.get("ok") is True, got
    assert len(got["model_sheet"]) == 2
    assert got["model_sheet_dropped"][0]["view"] == "profile"
    # And the poses still ran, on the views that survived.
    assert [c for c in calls if c["task_kind"] == "animation"]
