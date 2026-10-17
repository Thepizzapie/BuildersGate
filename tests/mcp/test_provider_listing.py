"""provider_status says what each capability serves, and the table is true.

MEASURED (Turf War, 2026-10-16): the audio seat read provider_status, saw
kie under image / music / video and no "sound" line anywhere, concluded its
seat had no sound-effects route, and gave up - while sfx_prompt, which gates
on "music", sat in its own tool list and had already produced two clips.
The listing sent it wrong. A listing that names the second job cannot; a
table that is pinned to the gate calls cannot drift back.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from bgate_core.runtime import gateway
from bgate_mcp import server

MCP_DIR = Path(server.__file__).resolve().parent


def _gated_tools() -> dict[str, set[str]]:
    """Every (capability -> tool) pair in the MCP modules, from the source.

    A ``_provider_gate(root, "<capability>", ...)`` call inside a top-level
    function is that function gating on that capability. Read with ast, not
    a regex, so a multi-line call or a renamed first argument still counts.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(MCP_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "_provider_gate"
                        and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)):
                    continue
                found.setdefault(node.args[1].value, set()).add(fn.name)
    return found


class TestTheTableIsTrue:
    def test_every_gated_tool_is_listed_under_its_capability(self):
        gated = _gated_tools()
        assert gated, "no _provider_gate calls found - the scan is broken"
        for capability, tools in gated.items():
            listed = set(gateway.CAPABILITY_TOOLS.get(capability, ()))
            missing = tools - listed
            assert not missing, (
                f"{sorted(missing)} gate on {capability!r} but are not listed "
                "under it in gateway.CAPABILITY_TOOLS - that is the listing "
                "that sent the audio seat wrong")

    def test_nothing_is_listed_that_does_not_gate(self):
        gated = _gated_tools()
        for capability, tools in gateway.CAPABILITY_TOOLS.items():
            extra = set(tools) - gated.get(capability, set())
            assert not extra, (
                f"{sorted(extra)} are listed under {capability!r} but do not "
                "gate on it")

    def test_every_capability_in_the_table_says_what_it_serves(self):
        for capability in gateway.CAPABILITIES:
            assert gateway.CAPABILITY_SERVES.get(capability), capability
            assert capability in gateway.CAPABILITY_TOOLS

    def test_music_is_named_as_the_sound_route(self):
        assert "sound" in gateway.CAPABILITY_SERVES["music"].lower()
        assert "sfx_prompt" in gateway.CAPABILITY_TOOLS["music"]


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
class TestTheListingSaysIt:
    async def test_provider_status_carries_serves_and_tools(self, wired):
        got = await call("provider_status")
        assert "sound" in got["capability_serves"]["music"].lower()
        assert "sfx_prompt" in got["capability_tools"]["music"]
        # The old shape is untouched: the order per capability is still there.
        assert got["capabilities"]["music"] == list(gateway.CAPABILITIES["music"])
        music = got["routing"]["families"]["music"]
        assert "sound" in music["serves"].lower()
        assert "sfx_prompt" in music["tools"]
