"""ITEM 21: listing tools must never blow the tool-result limit.

MEASURED FAILURE: asset_status returned 595 KB and asset_verify 96 KB on a
project with ~200 tracked assets - an agent's first call on either answered
"result exceeds maximum allowed tokens" and the seat started blind, billed
anyway. This builds a project with 300 tracked assets and asserts every
list-shaped tool's serialised result stays under the 40 KB cap.
"""
from __future__ import annotations

import json

import pytest

from bgate_core.art import refs as _refs
from bgate_core.design import lore as _lore
from bgate_core.store import assets as _assets
from bgate_mcp import server

CAP = 40_000


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return root


async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


def _seed_300_assets(root):
    out_dir = root / "sprites"
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(300):
        f = out_dir / f"sprite_{i:04d}.png"
        f.write_bytes(f"fake-png-bytes-{i}".encode() * 4)
        _assets.track(root, f)


def _seed_lore_and_refs(root):
    for i in range(150):
        _lore.add_entity(root, "item", f"widget_{i}",
                          summary="a widget " * 20, body="lore body " * 40)
    for i in range(150):
        (root / f"ref_{i}.png").write_bytes(b"x" * 200)
        _refs.pin(root, f"ref_{i}", str(root / f"ref_{i}.png"), kind="style")


class TestBoundedListings:
    def _payload_bytes(self, result: dict) -> int:
        return len(json.dumps(result).encode("utf-8"))

    @pytest.mark.anyio
    async def test_asset_status_is_bounded(self, wired):
        _seed_300_assets(wired)
        result = await call("asset_status")
        assert self._payload_bytes(result) <= CAP
        assert result["total"] == 300

    @pytest.mark.anyio
    async def test_asset_status_pages(self, wired):
        _seed_300_assets(wired)
        page1 = await call("asset_status", limit=50, offset=0)
        page2 = await call("asset_status", limit=50, offset=50)
        assert page1["assets"][0]["path"] != page2["assets"][0]["path"]

    @pytest.mark.anyio
    async def test_asset_verify_is_bounded(self, wired):
        _seed_300_assets(wired)
        result = await call("asset_verify")
        assert self._payload_bytes(result) <= CAP
        assert result["counts"]["clean"] == 300

    @pytest.mark.anyio
    async def test_queue_list_is_bounded(self, wired):
        for i in range(300):
            await call("queue_add", seat="art", title=f"item {i}",
                        brief="brief text " * 50)
        result = await call("queue_list", limit=200)
        assert self._payload_bytes(result) <= CAP

    @pytest.mark.anyio
    async def test_lore_list_is_bounded(self, wired):
        _seed_lore_and_refs(wired)
        result = await call("lore_list")
        assert self._payload_bytes(result) <= CAP

    @pytest.mark.anyio
    async def test_ref_list_is_bounded(self, wired):
        _seed_lore_and_refs(wired)
        result = await call("ref_list")
        assert self._payload_bytes(result) <= CAP

    @pytest.mark.anyio
    async def test_provider_status_is_bounded(self, wired):
        result = await call("provider_status")
        assert self._payload_bytes(result) <= CAP

    @pytest.mark.anyio
    async def test_board_digest_is_bounded(self, wired):
        for i in range(300):
            await call("queue_add", seat="art", title=f"item {i}",
                        brief="brief text " * 50)
        result = await call("board_digest")
        assert self._payload_bytes(result) <= CAP
