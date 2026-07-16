"""The MCP surface, exercised the way a client hits it.

Tools are called through the registered handler (not the plain function) so this
covers schema generation and dispatch, and asserts the contract the model sees:
errors come back as an "error" payload, never as a raised exception.
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
    """Dispatch through FastMCP and decode the payload a client would receive.

    ``tool`` is positional-only — tools have their own 'name' argument.
    """
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.mark.anyio
async def test_tools_are_registered():
    names = {t.name for t in await server.mcp.list_tools()}
    assert {
        "project_init", "project_status", "bible_add", "bible_update", "bible_read",
        "scope_check", "lore_add", "lore_update", "lore_brief", "lore_list",
        "lore_link", "lore_fact", "canon_check", "recall",
    } <= names


@pytest.mark.anyio
async def test_every_tool_has_a_description():
    for tool in await server.mcp.list_tools():
        assert tool.description and len(tool.description) > 30, tool.name


@pytest.mark.anyio
async def test_full_authoring_flow(wired):
    assert (await call("bible_add", kind="pillar", title="Tension over spectacle"))["id"] > 0
    await call("bible_add", kind="scope_tier", title="Core loop", rank=1)
    await call("bible_add", kind="cut_line", title="--- ship ---", rank=2)
    await call("bible_add", kind="scope_tier", title="Multiplayer", rank=5)

    view = await call("bible_read")
    assert [s["title"] for s in view["in_scope"]] == ["Core loop"]
    assert [s["title"] for s in view["cut"]] == ["Multiplayer"]
    assert (await call("scope_check", rank=5))["in_scope"] is False

    await call("lore_add", kind="faction", name="The Ashen Order", status="canon")
    await call("lore_fact", ref="The Ashen Order",
               statement="The Ashen Order worships the flame.", locked=True)

    status = await call("project_status")
    assert status["counts"] == {"bible_sections": 4, "entities": 1,
                                "canon_entities": 1, "facts": 1, "links": 0}

    assert (await call("recall", query="flame"))["results"]

    clean = await call("canon_check", text="The Ashen Order worships the flame.")
    assert clean["verdict"] == "ok"
    broken = await call("canon_check", text="The Ashen Order does not worship the flame.")
    assert broken["verdict"] == "conflict"


@pytest.mark.anyio
async def test_errors_return_payload_not_raise(wired):
    assert "error" in await call("lore_brief", ref="nobody-here")
    assert "error" in await call("bible_add", kind="vibes", title="x")


@pytest.mark.anyio
async def test_missing_project_explains_itself(tmp_path, monkeypatch):
    monkeypatch.delenv("BGATE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    got = await call("project_status")
    assert "error" in got and "project_init" in got["error"]
