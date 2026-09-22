"""Fun, then functional, then pretty: no provider paints, models or scores
anything until the human has played the graybox and said so.

Holding the ART SEAT kept art items off the board. It did not stop the
director, or a gameplay agent holding a prompt, from calling image_sprites
at the graybox stage - the seat hold only ever reached dispatch. Every paid
generation passes the server's provider preflight, so the stage is read
there too, for every caller. These tests are the gate firing, the two
honest ways through, and the rule that only the human opens the next stage.
"""
from __future__ import annotations

import json

import pytest

from bgate_core.design import greenlight
from bgate_mcp import server


class TestGenerationIsHeld:
    def test_image_generation_is_held_at_thesis(self, fresh_root):
        ok, why = greenlight.generation_allows(fresh_root, "image", "a portrait")
        assert ok is False
        assert "fun, then functional, then pretty" in why
        assert "greenlight_graybox_verdict" in why and "greenlight_waive('art'" in why

    @pytest.mark.parametrize("capability,seat", [
        ("image", "art"), ("3d", "art"), ("animate", "art"),
        ("music", "audio"), ("video", "cinematic")])
    def test_every_paid_capability_names_its_seat(self, fresh_root, capability, seat):
        ok, why = greenlight.generation_allows(fresh_root, capability)
        assert ok is False and f"a {seat} deliverable" in why

    def test_an_unknown_capability_is_not_this_gates_question(self, fresh_root):
        assert greenlight.generation_allows(fresh_root, "telemetry") == (True, "")

    def test_a_waiver_for_the_seat_opens_its_capabilities(self, fresh_root):
        greenlight.waive(fresh_root, "art", "the placeholder blocks need one keyed sprite")
        assert greenlight.generation_allows(fresh_root, "image")[0] is True
        assert greenlight.generation_allows(fresh_root, "music")[0] is False

    def test_the_hold_is_a_project_switch(self, fresh_root):
        # A game whose art IS the loop turns it off; the seat holds stay.
        from bgate_core.store import settings
        settings.set(fresh_root, "greenlight.generation_hold", False)
        assert greenlight.generation_allows(fresh_root, "image")[0] is True
        assert greenlight.allows(fresh_root, "art")[0] is False

    def test_production_opens_everything(self, root):
        # `root` is stamped to production by the suite fixture.
        for capability in greenlight.GENERATION_SEAT:
            assert greenlight.generation_allows(root, capability)[0] is True, capability


class TestTheProviderGateReadsTheStage:
    def test_the_provider_preflight_refuses_before_pricing(self, fresh_root, monkeypatch):
        from bgate_core.runtime import gateway

        def never(*a, **k):
            raise AssertionError("the stage refused; pricing must not run")
        monkeypatch.setattr(gateway, "pick", never)
        got = server._provider_gate(str(fresh_root), "image", "a hero portrait")
        assert got and got["ok"] is False and got["stage"] == "greenlight"
        assert "a hero portrait" in got["error"]


# ---------------------------------------------------------------------------
# Only the human opens the next stage
# ---------------------------------------------------------------------------

async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.fixture()
def wired(fresh_root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(fresh_root))
    for var in ("BGATE_SEAT", "BGATE_WORK_ITEM", "BGATE_ACTOR"):
        monkeypatch.delenv(var, raising=False)
    return fresh_root


@pytest.mark.anyio
async def test_an_agent_may_not_rule_on_the_graybox(wired, monkeypatch):
    monkeypatch.setenv("BGATE_SEAT", "gameplay")
    got = await call("greenlight_graybox_verdict", verdict="pass", interesting=True,
                     why="the loop is interesting because the choice is real")
    assert "error" in got and "only the human" in got["error"]


@pytest.mark.anyio
async def test_an_agent_may_not_move_a_project_forward(wired, monkeypatch):
    monkeypatch.setenv("BGATE_SEAT", "gameplay")
    got = await call("greenlight_advance", stage="graybox")
    assert "error" in got and "only the human" in got["error"]
    # Backward stays open to any seat: the refusal is about opening, not closing.
    got = await call("greenlight_advance", stage="thesis")
    assert "only the human" not in str(got.get("error", ""))
