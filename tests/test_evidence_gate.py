"""The eyes gate: scene work may not report 'done' with no render to show.

The failure this pins, observed on a real board: an agent measured a level's
geometry (walkable cells, connectivity, flood fill - all green), declared the
doorways fine, and the render had black holes where the doors should be. The
prompt already ordered a look; this gate makes the claim mechanical - a run
whose writelog contains a .tscn write must also contain a shot (or hand one
over as `evidence`) before queue_complete accepts 'done'.

Dispatched through FastMCP like test_mcp_adjust, because the gate lives in
the tool body, not in queue.complete - the reaper banks dead runs through
queue.complete and a corpse cannot be asked for a screenshot.
"""
from __future__ import annotations

import json

import pytest
from PIL import Image

from bgate_core import queue, writelog
from bgate_mcp import server


@pytest.fixture()
def run(root, monkeypatch):
    """A dispatched item mid-run: the identity queue_complete sees."""
    monkeypatch.setenv("BGATE_ROOT", str(root))
    item = queue.add(root, "gameplay", "build the level")
    queue.set_status(root, item["id"], "dispatched")
    monkeypatch.setenv("BGATE_WORK_ITEM", str(item["id"]))
    monkeypatch.setenv("BGATE_SEAT", "gameplay")
    monkeypatch.setenv("BGATE_ACTOR", f"agent:item-{item['id']}")
    return item


async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


def _wrote(root, item, rel):
    assert writelog.record(root, rel, "gameplay", f"item-{item['id']}")


def _shot(root, name="look.png"):
    path = root / ".bgate_out" / "shots" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (4, 4), (9, 9, 9, 255)).save(path)
    return path


@pytest.mark.anyio
class TestTheEyesGate:
    async def test_scene_writes_with_no_render_are_refused_with_the_route(
            self, root, run):
        _wrote(root, run, "game/scenes/level.tscn")
        got = await call("queue_complete", item_id=run["id"], result="done it")
        assert got["ok"] is False and got["stage"] == "evidence_gate"
        assert "godot_screenshot" in got["error"]
        assert "game/scenes/level.tscn" in got["error"]
        # the refusal is a redirect, not a wall: failed reports stay open
        assert "failed=True" in got["error"]
        assert queue.get(root, run["id"])["status"] == "dispatched"

    async def test_a_shot_taken_this_run_satisfies_the_gate(self, root, run):
        _wrote(root, run, "game/scenes/level.tscn")
        _wrote(root, run, ".bgate_out/shots/level-look.png")
        got = await call("queue_complete", item_id=run["id"], result="looked")
        assert got.get("status") in ("done", "review"), got

    async def test_an_evidence_path_satisfies_the_gate(self, root, run):
        _wrote(root, run, "game/scenes/level.tscn")
        shot = _shot(root)
        got = await call("queue_complete", item_id=run["id"],
                         result="looked", evidence=str(shot))
        assert got.get("status") in ("done", "review"), got

    async def test_junk_evidence_is_refused_by_name(self, root, run):
        _wrote(root, run, "game/scenes/level.tscn")
        got = await call("queue_complete", item_id=run["id"],
                         result="looked", evidence="no/such/render.png")
        assert got["ok"] is False and got["stage"] == "evidence_gate"
        assert "no/such/render.png" in got["error"]

    async def test_failed_never_needs_evidence(self, root, run):
        """An honest 'failed' must always be accepted - a gate that taxes the
        honest report teaches hopeful 'done's."""
        _wrote(root, run, "game/scenes/level.tscn")
        got = await call("queue_complete", item_id=run["id"],
                         result="could not finish", failed=True)
        assert got.get("status") == "failed", got

    async def test_a_run_that_wrote_no_scene_is_untouched(self, root, run):
        _wrote(root, run, "game/scripts/player.gd")
        got = await call("queue_complete", item_id=run["id"], result="code")
        assert got.get("status") in ("done", "review"), got

    async def test_the_setting_turns_it_off(self, root, run, monkeypatch):
        from bgate_core import settings
        settings.set(root, "qa.require_evidence", False)
        _wrote(root, run, "game/scenes/level.tscn")
        got = await call("queue_complete", item_id=run["id"], result="done")
        assert got.get("status") in ("done", "review"), got


class TestServerWritesReachTheWritelog:
    """The gate is only as honest as the writelog, and the hook records only
    the agent's OWN tools - a scene written through an MCP tool happens in the
    server process. scenewire.apply and the fresh-scene/screenshot paths now
    record theirs."""

    def test_scenewire_apply_records_for_the_dispatched_item(
            self, root, monkeypatch, tmp_path):
        from bgate_core import scenewire
        monkeypatch.setenv("BGATE_WORK_ITEM", "41")
        scene = root / "game" / "scenes" / "hub.tscn"
        scene.parent.mkdir(parents=True, exist_ok=True)
        scene.write_text("[node]", encoding="utf-8")
        scenewire.apply(scene, "[node name=\"X\"]", root=root)
        assert "game/scenes/hub.tscn" in writelog.paths_for(root, "item-41")

    def test_a_human_session_records_nothing(self, root, monkeypatch):
        from bgate_core import scenewire
        monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
        scene = root / "game" / "scenes" / "solo.tscn"
        scene.parent.mkdir(parents=True, exist_ok=True)
        scene.write_text("[node]", encoding="utf-8")
        scenewire.apply(scene, "[node name=\"Y\"]", root=root)
        assert writelog.paths_for(root, "item-") == []
