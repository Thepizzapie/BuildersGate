"""The scene surface an agent can reach, and the lock that stops two of them.

Three things landed together and this file is the seam between them:

  * bgate_core.level.scenewire is on the MCP surface, so an agent places a node with
    the same parser the dashboard uses instead of hand-writing .tscn text;
  * every scene mutation — from a browser or from an agent — now checks the
    lock, which none of them did while a human clicking buttons was the only
    caller;
  * a build's staleness is measured against the WHOLE game project, not three
    named directories, so a level that lives in data/*.json stops being
    invisible to it.

The interesting tests are the refusals and the near-misses. A wire that works is
worth one assertion; a wire that silently writes through an agent's lock is the
bug the whole lock table exists to prevent, and it wants three.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import assets
from bgate_core.level import scenewire
from bgate_ui import webbuild
from bgate_ui.app import app

MAIN_TSCN = """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/main.gd" id="1_m"]

[node name="Main" type="Node2D"]
script = ExtResource("1_m")

[node name="Props" type="Node2D" parent="."]

[node name="Desk" type="Sprite2D" parent="Props"]
position = Vector2(64, 32)
"""


def _write(path: Path, text: str) -> None:
    """Godot writes \\n everywhere; write_text() would give CRLF on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def game(root):
    """A Godot project at <root>/game, with a level in data/ — the layout that
    exposed the staleness bug, because `data` was not one of the three scanned
    directories."""
    g = root / "game"
    _write(g / "project.godot",
           'config_version=5\n\n[application]\n\nconfig/name="Test"\n'
           'run/main_scene="res://scenes/main.tscn"\n')
    _write(g / "scripts" / "main.gd", "extends Node2D\n")
    _write(g / "scenes" / "main.tscn", MAIN_TSCN)
    _write(g / "data" / "floor_0.json", '{"grid": {"w": 8, "h": 8}}\n')
    (g / "assets").mkdir(parents=True, exist_ok=True)
    _write(g / "assets" / "chair.png", "")   # content irrelevant; the path is not
    return g


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# The lock, on the dashboard's own scene endpoints
# ---------------------------------------------------------------------------
def test_scene_write_through_a_held_lock_is_refused(client, root, game):
    """The collision the lock table exists to make visible.

    These endpoints took a backup and wrote anyway, which is recovery, not
    prevention: the holder may be an agent mid-edit that is about to write its
    own copy over this one, and the loser of that race never finds out.
    """
    assets.lock(root, "game/scenes/main.tscn", "gameplay", owner="item-7")
    r = client.post("/api/scene/node/property", json={
        "scene": "res://scenes/main.tscn", "node": "Props/Desk",
        "key": "position", "value": "Vector2(128, 32)"})
    assert r.status_code == 423
    body = r.json()
    assert "gameplay" in str(body)
    # And the file is untouched — a refusal that half-wrote would be worse than
    # no refusal at all.
    assert "Vector2(64, 32)" in (game / "scenes" / "main.tscn").read_text()


def test_a_dry_run_is_not_blocked_by_a_lock(client, root, game):
    """Refusing to LOOK at a locked file helps nobody. A dry run returns text."""
    assets.lock(root, "game/scenes/main.tscn", "gameplay", owner="item-7")
    r = client.post("/api/scene/node/property", json={
        "scene": "res://scenes/main.tscn", "node": "Props/Desk",
        "key": "position", "value": "Vector2(128, 32)", "dry_run": True})
    assert r.status_code == 200
    assert "Vector2(128, 32)" in r.json()["data"]["text"]


def test_force_is_the_deliberate_override(client, root, game):
    assets.lock(root, "game/scenes/main.tscn", "art", owner="item-9")
    r = client.post("/api/scene/node/property", json={
        "scene": "res://scenes/main.tscn", "node": "Props/Desk",
        "key": "position", "value": "Vector2(128, 32)", "force": True})
    assert r.status_code == 200
    assert "Vector2(128, 32)" in (game / "scenes" / "main.tscn").read_text()


def test_reads_report_the_lock_so_the_builder_can_say_so_first(client, root, game):
    """Learning the file is claimed at `apply`, after twenty staged drags, is
    learning it too late for the information to be worth anything."""
    for url in ("/api/scene/tree?scene=res://scenes/main.tscn",
                "/api/scene/outline?scene=res://scenes/main.tscn",
                "/api/scene/render?scene=res://scenes/main.tscn"):
        assert client.get(url).json()["lock"] is None

    assets.lock(root, "game/scenes/main.tscn", "gameplay", owner="item-7")
    for url in ("/api/scene/tree?scene=res://scenes/main.tscn",
                "/api/scene/outline?scene=res://scenes/main.tscn",
                "/api/scene/render?scene=res://scenes/main.tscn"):
        assert client.get(url).json()["lock"]["seat"] == "gameplay"


def test_lock_holder_never_raises_on_a_path_outside_the_root(root):
    """The contract that lets every writer call it inline. A lookup that fails
    is a reason not to CLAIM the file is free, never a reason to refuse."""
    assert assets.lock_holder(root, Path(root).parent / "elsewhere.tscn") is None
    assert assets.lock_holder(root, "game/scenes/nothing-here.tscn") is None


# ---------------------------------------------------------------------------
# The MCP scene tools
# ---------------------------------------------------------------------------
@pytest.fixture()
def mcp(root, monkeypatch):
    """The server module with its project root pointed at the fixture."""
    monkeypatch.setenv("BGATE_ROOT", str(root))
    monkeypatch.delenv("BGATE_SEAT", raising=False)
    import bgate_mcp.server as server
    return server


def _call(fn, **kwargs):
    """Invoke a @_tool through its own wrapper, which is async by construction.

    Calling the undecorated body would skip the wrapper — and the wrapper is
    where the project root contextvar is set, so the test would pass while the
    real call path was broken.
    """
    import anyio
    return anyio.run(lambda: fn(**kwargs))


def test_the_scene_tools_are_registered(mcp):
    import anyio
    names = {t.name for t in anyio.run(mcp.mcp.list_tools)}
    assert {"scene_outline", "scene_wire", "scene_unwire", "scene_node_add",
            "scene_set_property", "scene_swap_resource", "scene_attach_script",
            "scene_rename_node", "scene_reparent_node"} <= names


def test_set_property_moves_a_node_and_backs_the_file_up(mcp, root, game):
    out = _call(mcp.scene_set_property, godot_project=str(game),
                scene="res://scenes/main.tscn", node="Props/Desk",
                key="position", value="Vector2(320, 96)")
    assert out["ok"] and out["written"]
    assert "Vector2(320, 96)" in (game / "scenes" / "main.tscn").read_text()
    # The previous bytes are still on disk. "Undo" is not a thing an agent has.
    backup = root / out["backup"]
    assert backup.is_file() and "Vector2(64, 32)" in backup.read_text()


def test_a_dry_run_writes_nothing_and_returns_the_text(mcp, game):
    out = _call(mcp.scene_set_property, godot_project=str(game),
                scene="res://scenes/main.tscn", node="Props/Desk",
                key="z_index", value=5, dry_run=True)
    assert out["ok"] and out["written"] is False
    assert "z_index = 5" in out["text"]
    assert "z_index" not in (game / "scenes" / "main.tscn").read_text()


def test_wire_allocates_the_id_and_the_load_steps(mcp, game):
    """The four things a hand-written block gets wrong, three of which the
    engine reports as something else entirely."""
    out = _call(mcp.scene_wire, godot_project=str(game),
                scene="res://scenes/main.tscn",
                asset="res://assets/chair.png", parent="Props")
    assert out["ok"] and out["written"]
    text = (game / "scenes" / "main.tscn").read_text()
    parsed = scenewire.parse(text)
    import re
    assert int(re.search(r"load_steps=(\d+)", text).group(1)) \
        == len(parsed["ext"]) + parsed["sub_count"] + 1
    ids = [e["id"] for e in parsed["ext"]]
    assert len(ids) == len(set(ids))
    assert out["node_type"] == "Sprite2D"


def test_an_agent_is_refused_a_scene_another_seat_holds(mcp, root, game):
    assets.lock(root, "game/scenes/main.tscn", "art", owner="item-4")
    out = _call(mcp.scene_set_property, godot_project=str(game),
                scene="res://scenes/main.tscn", node="Props/Desk",
                key="position", value="Vector2(1, 1)")
    assert out["ok"] is False
    assert "art" in out["error"]
    assert "Vector2(64, 32)" in (game / "scenes" / "main.tscn").read_text()


def test_a_seat_is_not_blocked_by_its_own_lock(mcp, root, game, monkeypatch):
    """Taking the lock is what earns the write. A seat refused by the claim it
    made itself would have to break its own lock to work, which teaches every
    agent that force=True is routine."""
    monkeypatch.setenv("BGATE_SEAT", "gameplay")
    assets.lock(root, "game/scenes/main.tscn", "gameplay", owner="item-4")
    out = _call(mcp.scene_set_property, godot_project=str(game),
                scene="res://scenes/main.tscn", node="Props/Desk",
                key="position", value="Vector2(1, 1)")
    assert out["ok"] and out["written"]


def test_outline_filters_and_says_when_it_truncated(mcp, game):
    """A baked plate has fifteen hundred nodes. An unfiltered dump of that would
    bury the task in furniture, and a silent cut would be worse."""
    full = _call(mcp.scene_outline, godot_project=str(game),
                 scene="res://scenes/main.tscn")
    assert full["total"] == 3 and full["truncated"] is False
    assert "properties" not in full["nodes"][0]      # off unless asked for

    one = _call(mcp.scene_outline, godot_project=str(game),
                scene="res://scenes/main.tscn", match="desk")
    assert one["matched"] == 1 and one["nodes"][0]["path"] == "Props/Desk"
    # The role census is of the WHOLE scene, not of what was matched.
    assert sum(one["roles"].values()) == 3

    cut = _call(mcp.scene_outline, godot_project=str(game),
                scene="res://scenes/main.tscn", limit=1)
    assert cut["returned"] == 1 and cut["truncated"] is True and cut["total"] == 3


def test_a_missing_scene_fails_with_a_message_not_a_traceback(mcp, game):
    out = _call(mcp.scene_outline, godot_project=str(game),
                scene="res://scenes/nope.tscn")
    assert out["ok"] is False and "nope.tscn" in out["error"]


# ---------------------------------------------------------------------------
# Build staleness — what counts as source
# ---------------------------------------------------------------------------
def _stamp(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def test_a_level_in_data_makes_the_build_stale(root, game):
    """THE BUG THIS REPLACED. The scan named scripts/, scenes/ and assets/, so a
    project whose levels live in data/*.json had every level edit invisible: the
    build reported CURRENT, you played the old one, and concluded the tool had
    ignored you. That is verbatim the morning webbuild.py was written to
    prevent, reintroduced by the scan instead of the rebuild."""
    pck = root / "export" / "web" / "index.pck"
    pck.parent.mkdir(parents=True, exist_ok=True)
    pck.write_bytes(b"build")

    now = time.time()
    for p in game.rglob("*"):
        if p.is_file():
            _stamp(p, now - 600)
    _stamp(pck, now - 300)
    assert webbuild.status(root)["stale"] is False

    _stamp(game / "data" / "floor_0.json", now)
    st = webbuild.status(root)
    assert st["stale"] is True
    # And it says WHICH file, so "stale" is answerable rather than asserted.
    assert st["newest_source"] == "data/floor_0.json"
    assert "data/floor_0.json" in st["reason"]


def test_the_godot_cache_and_the_export_are_not_source(root, game):
    """Otherwise every build is instantly stale by its own doing: the export
    writes into the project, and a scan that counts its own output never
    converges."""
    pck = root / "export" / "web" / "index.pck"
    pck.parent.mkdir(parents=True, exist_ok=True)
    pck.write_bytes(b"build")

    now = time.time()
    for p in game.rglob("*"):
        if p.is_file():
            _stamp(p, now - 600)
    _stamp(pck, now - 300)

    _write(game / ".godot" / "imported" / "chair.png-abc.ctex", "x")
    _stamp(game / ".godot" / "imported" / "chair.png-abc.ctex", now)
    _write(game / "export" / "web" / "index.pck", "x")
    _stamp(game / "export" / "web" / "index.pck", now)

    assert webbuild.status(root)["stale"] is False
