"""ITEM 9b — a human's 'no' is a signal, not just a status flip.

Three human rejections of the same tool's output on one project inside 24h
must refuse a dispatched seat's fourth call, and clearing the block must
restore it. This exercises the counter directly, the artifacts.review() hook
that feeds it, and the MCP-level refusal + rejections_clear tool.
"""
from __future__ import annotations

import json

import pytest

from bgate_core.board import rejections
from bgate_core.store import artifacts
# MODULE-LEVEL, like tests/board/test_mcp_server.py — registration decisions
# (module/seat/engine gates) run once, at import, off whatever BGATE_SEAT
# happens to be in the environment THEN. Importing here, during collection,
# pins that moment to before any test's monkeypatch.setenv runs; a lazy
# import inside a test body risks freezing the whole session's tool registry
# to whichever seat a stray earlier test happened to set first.
from bgate_mcp import server


class TestCounter:
    def test_three_human_rejections_block_the_tool(self, root):
        for _ in range(3):
            rejections.record(root, "image_sprites", seat="art",
                              reason="mixed sheet", by="a-human")
        assert rejections.blocked(root, "image_sprites")

    def test_two_rejections_do_not_block(self, root):
        for _ in range(2):
            rejections.record(root, "image_sprites", seat="art", by="a-human")
        assert not rejections.blocked(root, "image_sprites")

    def test_a_different_tool_is_unaffected(self, root):
        for _ in range(3):
            rejections.record(root, "image_sprites", seat="art", by="a-human")
        assert not rejections.blocked(root, "sfx_generate")

    def test_clear_unblocks_it(self, root):
        for _ in range(3):
            rejections.record(root, "image_sprites", seat="art", by="a-human")
        assert rejections.blocked(root, "image_sprites")
        cleared = rejections.clear(root, "image_sprites", by="a-human")
        assert cleared["ok"] and cleared["cleared"] == 3
        assert not rejections.blocked(root, "image_sprites")

    def test_blocked_tools_lists_only_tools_at_threshold(self, root):
        for _ in range(3):
            rejections.record(root, "image_sprites", seat="art", by="a-human")
        rejections.record(root, "sfx_generate", seat="audio", by="a-human")
        names = {b["tool"] for b in rejections.blocked_tools(root)}
        assert names == {"image_sprites"}

    def test_blocked_tools_can_scope_to_a_seat(self, root):
        for _ in range(3):
            rejections.record(root, "image_sprites", seat="art", by="a-human")
        assert rejections.blocked_tools(root, seat="art")
        assert not rejections.blocked_tools(root, seat="audio")


class TestArtifactsReviewHook:
    def test_a_human_rejection_is_counted(self, root):
        (root / "gnome.png").write_bytes(b"\x89PNG\r\n")
        art = artifacts.register(root, "gnome", root / "gnome.png",
                                 producer="image_sprites")
        artifacts.review(root, art["id"], "rejected", "mixed sheet",
                         actor="a-human")
        assert rejections.count(root, "image_sprites")["count"] == 1

    def test_an_agents_own_self_qa_rejection_is_not_counted(self, root):
        (root / "gnome2.png").write_bytes(b"\x89PNG\r\n")
        art = artifacts.register(root, "gnome2", root / "gnome2.png",
                                 producer="image_sprites")
        artifacts.review(root, art["id"], "rejected", "self-qa fail",
                         actor="agent:item-1")
        assert rejections.count(root, "image_sprites")["count"] == 0

    def test_three_human_rejections_of_the_same_tool_block_it(self, root):
        for i in range(3):
            (root / f"gnome{i}.png").write_bytes(b"\x89PNG\r\n")
            art = artifacts.register(root, f"gnome{i}",
                                     root / f"gnome{i}.png",
                                     producer="image_sprites")
            artifacts.review(root, art["id"], "rejected", "still mixed",
                             actor="a-human")
        assert rejections.blocked(root, "image_sprites")


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return root


async def _call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


class TestMCPRefusal:
    @pytest.mark.anyio
    async def test_a_dispatched_seat_is_refused_after_three_rejections(
            self, wired, monkeypatch):
        for _ in range(3):
            rejections.record(wired, "sprite_fit", seat="art", by="a-human")
        monkeypatch.setenv("BGATE_SEAT", "art")
        got = await _call("sprite_fit", sheet="nope.png", cell=[64, 96])
        assert got.get("refused") == "human_rejections"
        assert got.get("tool") == "sprite_fit"

    @pytest.mark.anyio
    async def test_the_director_with_no_seat_set_is_never_refused(
            self, wired, monkeypatch):
        for _ in range(3):
            rejections.record(wired, "sprite_fit", seat="art", by="a-human")
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        got = await _call("sprite_fit", sheet="nope.png", cell=[64, 96])
        assert got.get("refused") != "human_rejections"

    @pytest.mark.anyio
    async def test_rejections_clear_unblocks_a_dispatched_seat(
            self, wired, monkeypatch):
        for _ in range(3):
            rejections.record(wired, "sprite_fit", seat="art", by="a-human")
        monkeypatch.setenv("BGATE_SEAT", "art")
        blocked = await _call("sprite_fit", sheet="nope.png", cell=[64, 96])
        assert blocked.get("refused") == "human_rejections"
        cleared = await _call("rejections_clear", tool="sprite_fit")
        assert cleared.get("ok")
        after = await _call("sprite_fit", sheet="nope.png", cell=[64, 96])
        assert after.get("refused") != "human_rejections"
