"""kit_* and library_* through FastMCP: the project binding, the failure
shape and the writelog all live in `_tool`, so a direct call tests code no
client reaches.

Nothing here needs Godot: `check=False` on install, and the compile proof is
tested for real in tests/store/test_kits.py.
"""
from __future__ import annotations

import json

import pytest
from PIL import Image

from bgate_core.store import assets, kits, scaffold
from bgate_mcp import server


@pytest.fixture()
def game(root, monkeypatch):
    """A pinned 2D project with the scaffold in its root."""
    scaffold.new_project(root, "Emberfall", kind="2d", force=True)
    monkeypatch.setenv("BGATE_ROOT", str(root))
    for var in ("BGATE_ACTOR", "BGATE_SEAT", "BGATE_WORK_ITEM"):
        monkeypatch.delenv(var, raising=False)
    return root


async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[-1]
    return json.loads(block.text) if hasattr(block, "text") else block


class TestKitsOverMcp:
    def test_registered(self):
        names = {t.name for t in server.mcp._tool_manager.list_tools()}
        assert {"kit_list", "kit_install", "kit_remove",
                "library_publish", "library_search", "library_import"} <= names

    @pytest.mark.anyio
    async def test_list_filters_to_the_project_dimension(self, game):
        got = await call("kit_list")
        names = {k["name"] for k in got["kits"]}
        assert "topdown_controller" in names and "vehicle_controller" not in names
        assert got["dimension"] == "2d"
        assert all(k["state"] == "absent" for k in got["kits"])

    @pytest.mark.anyio
    async def test_install_then_list_reads_installed(self, game):
        got = await call("kit_install", name="inventory", check=False)
        assert got["ok"] and got["written"] == ["scripts/inventory.gd"]
        assert (game / "scripts" / "inventory.gd").is_file()
        assert (game / ".bgate" / "kits.json").is_file()
        rows = {k["name"]: k["state"] for k in (await call("kit_list"))["kits"]}
        assert rows["inventory"] == "installed"

    @pytest.mark.anyio
    async def test_dimension_mismatch_is_a_normalized_failure(self, game):
        got = await call("kit_install", name="vehicle_controller", check=False)
        assert got["ok"] is False
        assert "3d kit" in got["error"]
        got = await call("kit_install", name="vehicle_controller", check=False,
                         any_dimension=True)
        assert got["ok"]

    @pytest.mark.anyio
    async def test_remove_refuses_an_edited_file(self, game):
        await call("kit_install", name="health", check=False)
        (game / "scripts" / "health.gd").write_text("edited", encoding="utf-8")
        got = await call("kit_remove", name="health")
        assert got["ok"] is False and got["refused"]
        assert (game / "scripts" / "health.gd").exists()

    @pytest.mark.anyio
    async def test_check_is_skipped_with_a_reason_without_godot(self, game, monkeypatch):
        monkeypatch.setattr(server._godot, "available", lambda: {"available": False})
        got = await call("kit_install", name="health", check=True)
        assert got["ok"] and "skipped" in got["check"]


class TestLibraryOverMcp:
    @pytest.mark.anyio
    async def test_publish_search_import_round_trip(self, game, tmp_path):
        sheet = game / "assets" / "hero_walk.png"
        sheet.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (64, 16), (5, 5, 5, 255)).save(sheet)
        got = await call("library_publish", paths=["assets/hero_walk.png"],
                         tags=["Hero"], collection="hero")
        assert got["ok"] and len(got["published"]) == 1
        entry = got["published"][0]
        assert entry["tags"] == ["hero"] and entry["sources"][0]["project"] == "Test Game"

        found = await call("library_search", query="hero")
        assert found["matched"] == 1 and found["entries"][0]["id"] == entry["id"]

        # Into the SAME project under another name: lands, is tracked.
        got = await call("library_import", ids=[entry["id"]], dest="assets/imported",
                         rename="knight_walk")
        assert got["ok"]
        row = got["imported"][0]
        assert row["path"] == "assets/imported/knight_walk.png"
        assert row["res"] == "res://assets/imported/knight_walk.png"
        assert got["tracked"] == ["assets/imported/knight_walk.png"]
        assert assets.get(game, "assets/imported/knight_walk.png")["hash"] == entry["hash"]

    @pytest.mark.anyio
    async def test_publish_outside_the_project_is_refused(self, game, tmp_path_factory):
        stranger = tmp_path_factory.mktemp("elsewhere") / "elsewhere.png"
        Image.new("RGB", (4, 4)).save(stranger)
        got = await call("library_publish", paths=[str(stranger)])
        assert got["ok"] is False and "outside the project root" in got["error"]

    @pytest.mark.anyio
    async def test_unknown_id_is_a_normalized_failure(self, game):
        got = await call("library_import", ids=["nope"])
        assert got["ok"] is False and "no library entry" in got["error"]

    def test_forget_is_not_a_tool(self):
        names = {t.name for t in server.mcp._tool_manager.list_tools()}
        assert not any(n.startswith("library_") and "forget" in n for n in names)
        assert "library_publish" in names


def test_every_kit_is_godot_owned_and_engine_spine():
    """The classification that keeps a web project from being offered
    GDScript, and audio from carrying a controller installer."""
    from bgate_core.runtime import engines
    from bgate_core.store import modules

    for name in ("kit_list", "kit_install", "kit_remove"):
        assert name in engines.ENGINE_TOOLS["godot"]
        assert modules.spine_group(name) == "engine"
        assert not modules.seat_tool_enabled(name, "audio")
        assert modules.seat_tool_enabled(name, "gameplay")
    for name in ("library_publish", "library_search", "library_import"):
        assert modules.spine_group(name) == "core"
        assert modules.seat_tool_enabled(name, "audio")
        assert engines.tool_enabled(name, "web")
    assert set(kits.KEYCODES) >= {"W", "A", "S", "D", "Space", "E", "Shift"}
