"""Pre-production research from the MCP door, and the two lines it must not cross.

ADOPT IS A HUMAN'S. research_plan_adopt turns a drafted plan into the bible,
the not-building list, open decisions and the thesis. An agent that drafts a
plan and adopts it is the review step reviewing itself, so it is refused on
the same fail-closed signal brainstorm_deploy uses (BGATE_SEAT /
BGATE_WORK_ITEM / an agent actor). Every other research tool stays open to an
agent: researching is not the dangerous part.

THE RESEARCHER HAS THE WEB AND NOTHING ELSE. It is the one spawned session
allowed WebSearch and WebFetch, so its argv is pinned here: exactly those two
built-in tools, no MCP server (--strict-mcp-config, no --mcp-config), no
settings sources, and a scratch working directory rather than the project.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from bgate_core.design import bible, research
from bgate_core.store import db, modules, project
from bgate_mcp import server
from bgate_ui.agents import researcher

PLAN = {
    "pillars": [{"title": "The light is the clock", "body": "Every run is a night."}],
    "core_loop": "Tend the lamp, row out to rescue ships, trade salvage.",
    "not_building": [{"text": "Crafting", "reason": "Weakest system in Dredge."}],
}


async def call(tool: str, /, **kwargs) -> dict:
    """Dispatch through FastMCP and decode what a client would receive."""
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    project.init(tmp_path, "Emberfall", pitch="lighthouse roguelite")
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    for var in ("BGATE_SEAT", "BGATE_WORK_ITEM", "BGATE_ACTOR"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    db.close_all()


def _drafted(root):
    comp = research.add_comp(root, "Dredge")
    research.record(root, comp["id"], {"findings": [
        {"system": "economy", "verdict": "failed", "claim": "Inflated late."}]})
    research.save_plan(root, research.validate_plan(PLAN))


@pytest.mark.anyio
@pytest.mark.parametrize("var,value", [
    ("BGATE_SEAT", "director"),
    ("BGATE_WORK_ITEM", "7"),
    ("BGATE_ACTOR", "agent:director"),
])
async def test_agent_may_not_adopt(wired, monkeypatch, var, value):
    _drafted(wired)
    monkeypatch.setenv(var, value)
    out = await call("research_plan_adopt")
    assert out["ok"] is False and "may not adopt" in out["error"]
    assert bible.list_sections(wired, kind="pillar") == []


@pytest.mark.anyio
async def test_human_adopts_through_the_tool(wired):
    _drafted(wired)
    out = await call("research_plan_adopt")
    assert out["ok"] is True
    assert [s["title"] for s in bible.list_sections(wired, kind="pillar")] \
        == ["The light is the clock"]
    again = await call("research_plan_adopt")
    assert again["ok"] is False and "adopted" in again["error"]


@pytest.mark.anyio
async def test_agent_can_do_everything_but_adopt(wired, monkeypatch):
    monkeypatch.setenv("BGATE_SEAT", "director")
    added = await call("research_comp_add", title="Hades",
                       why="run structure", relation="loop")
    assert added["status"] == "confirmed"
    dropped = await call("research_comp_set", comp_id=added["id"],
                         status="dropped")
    assert dropped["status"] == "dropped"
    bad = await call("research_comp_set", comp_id=added["id"],
                     status="researched")
    assert bad["ok"] is False
    assert (await call("research_status"))["counts"]["dropped"] == 1
    assert (await call("research_findings", verdict="nope"))["ok"] is False


@pytest.mark.anyio
async def test_tools_register_and_are_gated_by_the_module(wired):
    names = {t.name for t in await server.mcp.list_tools()}
    assert {"research_status", "research_suggest", "research_teardown",
            "research_findings", "research_grid", "research_plan_draft",
            "research_plan", "research_plan_adopt"} <= names
    assert "research" in modules.names()
    assert not modules.tool_enabled("research_teardown", {"research"})
    assert modules.tool_enabled("research_teardown", set())


def test_researcher_argv_is_web_only():
    args = researcher.build_args("claude", system="S", model="sonnet")
    assert args[args.index("--tools") + 1] == "WebSearch,WebFetch"
    assert args[args.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert "--strict-mcp-config" in args and "--mcp-config" not in args
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--system-prompt") + 1] == "S"


def test_researcher_runs_outside_the_project(wired, monkeypatch):
    seen = {}

    def fake_run(args, **kw):
        seen.update(kw, args=args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(
            {"type": "result", "is_error": False, "result": "{\"comps\": []}",
             "total_cost_usd": 0.12}), stderr="")
    monkeypatch.setattr(researcher._runners, "find_claude", lambda: "claude")
    monkeypatch.setattr(researcher.subprocess, "run", fake_run)
    out = researcher.run(wired, system="S", prompt="P", kind="suggest")
    assert out == {**out, "ok": True, "text": "{\"comps\": []}", "usd": 0.12}
    assert seen["input"] == "P"
    assert str(wired) not in seen["cwd"]


def test_researcher_reports_failure_instead_of_raising(wired, monkeypatch):
    monkeypatch.setattr(researcher._runners, "find_claude", lambda: None)
    assert researcher.run(wired, system="S", prompt="P")["ok"] is False

    def timeout(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)
    monkeypatch.setattr(researcher._runners, "find_claude", lambda: "claude")
    monkeypatch.setattr(researcher.subprocess, "run", timeout)
    out = researcher.run(wired, system="S", prompt="P", kind="teardown")
    assert out["ok"] is False and "ceiling" in out["error"]
