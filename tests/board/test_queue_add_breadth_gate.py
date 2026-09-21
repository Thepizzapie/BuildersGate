"""GRIPE 41 wiring at the MCP surface: queue_add refuses a broad brief or a
missing acceptance for a non-director caller, and warns (rather than refuses)
the director.
"""
from __future__ import annotations

import json

import pytest

from bgate_mcp import server


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return root


async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.mark.anyio
class TestQueueAddBreadthGate:
    async def test_non_director_without_acceptance_is_refused(
            self, wired, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "tech")
        got = await call("queue_add", seat="tech", title="fix the jump",
                         brief="player.gd checks is_on_floor too late")
        assert got.get("ok") is False
        assert got["refused"] == "no_acceptance"

    async def test_non_director_broad_brief_is_refused(self, wired, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "tech")
        brief = (
            "Ship the shop screen:\n"
            "- add the buy button\n"
            "- wire the currency counter\n"
            "- add a sell flow and then polish the animation once that's done\n"
        )
        got = await call("queue_add", seat="tech", title="shop screen",
                         brief=brief, acceptance="shop screen opens and sells")
        assert got.get("ok") is False
        assert got["refused"] == "too_broad"
        assert "queue_add_chain" in got["error"]

    async def test_non_director_narrow_brief_with_acceptance_succeeds(
            self, wired, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "tech")
        got = await call(
            "queue_add", seat="tech", title="fix the jump",
            brief="player.gd checks is_on_floor after the velocity update",
            acceptance="godot_test_run shows 0 failures", size="small")
        assert "error" not in got
        assert got["size"] == "small"

    async def test_director_bypasses_the_hard_refusal_but_still_gets_a_warning(
            self, wired, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "director")
        brief = (
            "Ship the shop screen:\n"
            "- add the buy button\n"
            "- wire the currency counter\n"
            "- add a sell flow and then polish once that's done\n"
        )
        got = await call("queue_add", seat="tech", title="shop screen",
                         brief=brief)
        assert "error" not in got
        assert got.get("warnings")
