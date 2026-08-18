"""Level generation — the properties, not the pixels.

A generated level is wrong in ways a screenshot does not show: a room nobody
can walk to, a seed that does not reproduce, two rooms fused into one cavity, a
corner tile facing out of the map. Every one of those is a property that can be
stated and checked, so it is.
"""
from __future__ import annotations

import pytest

from bgate_core import autotile, levelgen, scenewire, tilemap
from bgate_core.autotile import E, N, NE, W


# ---------------------------------------------------------------------------
# Masks
# ---------------------------------------------------------------------------
def test_a_corner_only_counts_when_both_its_sides_are_filled():
    """Without this reduction the table needs 256 entries, 209 unreachable."""
    assert autotile.canonical8(NE) == 0
    assert autotile.canonical8(N | NE) == N
    assert autotile.canonical8(N | E | NE) == N | E | NE


def test_the_blob_has_exactly_forty_seven_shapes():
    masks = autotile.blob47_masks()
    assert len(masks) == 47
    assert len(set(masks)) == 47


def test_the_side_mask_reads_the_four_neighbours():
    filled = {(0, 0), (0, -1), (1, 0)}
    assert autotile.bitmask(filled, 0, 0, bits=4) == N | E


def test_outside_the_region_is_solid_when_asked():
    """A dungeon carved from rock: without this every boundary cell believes it
    has open space behind it and the map grows a decorative border."""
    filled = {(0, 0)}
    region = (0, 0, 4, 4)
    assert autotile.bitmask(filled, 0, 0, bits=4, region=region) == 0
    assert autotile.bitmask(filled, 0, 0, bits=4, region=region,
                            outside=True) == N | W


# ---------------------------------------------------------------------------
# Terrains
# ---------------------------------------------------------------------------
def test_a_solid_terrain_puts_the_same_tile_everywhere():
    terrain = autotile.Terrain.solid(3, (2, 5))
    cells = autotile.resolve({(0, 0), (9, 9)}, terrain)
    assert [(c["source"], c["ax"], c["ay"]) for c in cells] == [(3, 2, 5)] * 2


def test_blob47_lays_the_masks_out_row_major():
    terrain = autotile.Terrain.blob47(1, columns=8)
    masks = autotile.blob47_masks()
    assert terrain.atlas_for(masks[0]) == (0, 0)
    assert terrain.atlas_for(masks[7]) == (7, 0)
    assert terrain.atlas_for(masks[8]) == (0, 1)
    assert len(terrain.table) == 47


def test_a_table_that_maps_two_shapes_onto_one_sprite_is_refused():
    """NE alone reduces to 0, which is already the empty shape — the sheet is
    saying two different things about one tile."""
    with pytest.raises(autotile.TerrainError):
        autotile.Terrain.from_table(0, {0: (0, 0), NE: (1, 0)})


def test_an_unmapped_shape_is_dropped_and_counted_not_guessed():
    terrain = autotile.Terrain.from_table(0, {0: (0, 0)}, bits=4)
    filled = {(0, 0), (1, 0)}                    # both have one neighbour
    assert autotile.resolve(filled, terrain) == []
    assert autotile.unmapped(filled, terrain) == {E: 1, W: 1}


def test_the_fallback_fills_holes_the_table_leaves():
    terrain = autotile.Terrain.from_table(0, {0: (0, 0)}, bits=4,
                                          fallback=(9, 9))
    cells = autotile.resolve({(0, 0), (1, 0)}, terrain)
    assert [(c["ax"], c["ay"]) for c in cells] == [(9, 9), (9, 9)]
    assert autotile.unmapped({(0, 0), (1, 0)}, terrain) == {}


def test_every_blob_shape_a_real_map_produces_is_in_the_table():
    """The end of the 'seams line up' claim: no wall cell in a generated level
    asks for a sprite the 47-blob does not have."""
    level = levelgen.plan(48, 32, seed=11)
    wall = autotile.Terrain.blob47(1)
    walls = [tuple(c) for c in level["walls"]]
    assert autotile.unmapped(walls, wall, region=tuple(level["region"]),
                             outside=True) == {}


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
def test_the_same_seed_is_the_same_level():
    assert levelgen.plan(40, 40, seed=7) == levelgen.plan(40, 40, seed=7)


def test_different_seeds_are_different_levels():
    assert levelgen.plan(40, 40, seed=7) != levelgen.plan(40, 40, seed=8)


@pytest.mark.parametrize("seed", range(12))
def test_every_room_is_reachable_from_every_other(seed):
    """The guarantee BSP is chosen FOR. Joining the two halves of every cut
    builds a spanning tree over the rooms; if this ever fails, the join is
    wrong, not the luck."""
    level = levelgen.plan(56, 40, seed=seed)
    assert level["connected"] is True
    assert levelgen.connected([tuple(c) for c in level["floor"]])


@pytest.mark.parametrize("seed", range(8))
def test_rooms_never_overlap_and_never_touch(seed):
    """Two rooms sharing a wall read as one L-shaped cavity. The leaf margin is
    what prevents it, and this is the test that says so."""
    level = levelgen.plan(56, 40, seed=seed, margin=1)
    grown = []
    for r in level["rooms"]:
        grown.append({(x, y)
                      for y in range(r["y"] - 1, r["y"] + r["h"] + 1)
                      for x in range(r["x"] - 1, r["x"] + r["w"] + 1)})
    cores = [levelgen.Rect(r["x"], r["y"], r["w"], r["h"]).cells()
             for r in level["rooms"]]
    for i, core in enumerate(cores):
        for j, halo in enumerate(grown):
            if i != j:
                assert not (core & halo), f"rooms {i} and {j} touch"


@pytest.mark.parametrize("seed", range(8))
def test_nothing_lands_outside_the_map(seed):
    level = levelgen.plan(48, 32, seed=seed, corridor_width=2)
    for x, y in level["floor"]:
        assert 0 <= x < 48 and 0 <= y < 32
    for r in level["rooms"]:
        assert 0 <= r["x"] and r["x"] + r["w"] <= 48
        assert 0 <= r["y"] and r["y"] + r["h"] <= 32


def test_walls_wrap_the_floor_including_the_diagonals():
    """A four-neighbour ring leaves a hole at every inside corner, and the
    player can see through it."""
    ring = levelgen.wall_ring({(0, 0)})
    assert len(ring) == 8
    assert (1, 1) in ring and (-1, -1) in ring


def test_a_wall_is_never_also_a_floor():
    level = levelgen.plan(40, 40, seed=3)
    assert not ({tuple(c) for c in level["floor"]}
                & {tuple(c) for c in level["walls"]})


def test_a_level_too_small_for_its_own_rooms_says_so_before_generating():
    with pytest.raises(levelgen.LevelError) as exc:
        levelgen.plan(40, 40, min_leaf=5, min_room=6)
    assert "min_leaf" in str(exc.value)
    with pytest.raises(levelgen.LevelError):
        levelgen.plan(8, 8, min_leaf=10)


def test_a_disconnected_floor_is_reported_as_such():
    assert levelgen.connected({(0, 0), (1, 0)})
    assert not levelgen.connected({(0, 0), (5, 5)})
    assert levelgen.connected(set()), "nothing is trivially connected"


def test_the_ascii_map_shows_floor_walls_and_void():
    art = levelgen.ascii_map(levelgen.plan(40, 40, seed=1))
    assert "." in art and "#" in art
    assert art.count("\n") == 41          # height + the two border rows - 1


# ---------------------------------------------------------------------------
# Plan -> layers
# ---------------------------------------------------------------------------
def test_layers_split_floor_and_wall_onto_their_own_tilemaps():
    """One layer per terrain: a wall drawn over a floor cell would evict the
    floor, and the hole shows the moment anything is destructible."""
    level = levelgen.plan(40, 32, seed=5)
    out = levelgen.layers(level, floor=autotile.Terrain.solid(0, (0, 0)),
                          wall=autotile.Terrain.blob47(1))
    assert [ly["name"] for ly in out] == ["Floor", "Walls"]
    assert len(out[0]["cells"]) == len(level["floor"])
    # walls FILL the rock by default; this test used to pin the one-cell ring,
    # which is what left the rest of the map empty behind every wall
    assert len(out[1]["cells"]) == len(level["solid"])
    assert out[1]["unmapped"] == {}


def test_a_wall_ring_is_still_available_for_open_terrain():
    """A ring is right when the level is not carved out of rock — an outdoor
    map wants a wall edge, not a solid fill over everything that is not path."""
    level = levelgen.plan(40, 32, seed=5)
    out = levelgen.layers(level, floor=autotile.Terrain.solid(0, (0, 0)),
                          wall=autotile.Terrain.blob47(1), wall_fill=False)
    assert len(out[1]["cells"]) == len(level["walls"])
    assert len(level["solid"]) > len(level["walls"])


def test_every_layer_encodes_without_a_coordinate_collision():
    """encode_cells refuses duplicates, so this is the check that resolve()
    never emits two tiles for one cell."""
    level = levelgen.plan(48, 32, seed=2)
    for layer in levelgen.layers(level, floor=autotile.Terrain.solid(0),
                                 wall=autotile.Terrain.blob47(1)):
        assert tilemap.decode_cells(tilemap.encode_cells(layer["cells"]))


# ---------------------------------------------------------------------------
# Into the scene
# ---------------------------------------------------------------------------
SCENE = ('[gd_scene load_steps=1 format=3]\n\n'
         '[node name="Level" type="Node2D"]\n')


def _wire(text, level=None, **kw):
    level = level or levelgen.plan(40, 32, seed=4)
    layers = levelgen.layers(level, floor=autotile.Terrain.solid(0),
                             wall=autotile.Terrain.blob47(1))
    return scenewire.wire_tilemap(text, "res://tiles/dungeon.tres", layers, **kw)


def test_the_layers_land_as_tilemaplayer_nodes_with_the_tileset():
    out = _wire(SCENE)
    assert '[ext_resource type="TileSet" path="res://tiles/dungeon.tres"' in out["text"]
    assert out["text"].count('type="TileMapLayer"') == 2
    assert f'tile_set = ExtResource("{out["id"]}")' in out["text"]
    assert 'tile_map_data = PackedByteArray("' in out["text"]
    assert [ly["action"] for ly in out["layers"]] == ["add", "add"]


def test_regenerating_replaces_the_layers_instead_of_stacking_them():
    """Append-only turns a re-run into Ground, Ground2, Ground3 — all still
    drawing, the old one on top, and nothing in the scene says so."""
    once = _wire(SCENE)
    twice = _wire(once["text"], levelgen.plan(40, 32, seed=99))
    assert twice["text"].count('type="TileMapLayer"') == 2
    assert twice["text"].count('name="Walls"') == 1
    assert [ly["action"] for ly in twice["layers"]] == ["replace", "replace"]
    assert twice["text"].count("res://tiles/dungeon.tres") == 1, "one ext_resource"


def test_a_name_that_belongs_to_something_else_is_refused_not_clobbered():
    scene = SCENE + '\n[node name="Walls" type="StaticBody2D" parent="."]\n'
    with pytest.raises(scenewire.WireError) as exc:
        _wire(scene)
    assert "StaticBody2D" in str(exc.value)


def test_the_written_scene_still_parses_and_load_steps_is_right():
    out = _wire(SCENE)
    parsed = scenewire.parse(out["text"])
    assert parsed["load_steps"] == len(parsed["ext"]) + parsed["sub_count"] + 1
    assert [n["name"] for n in parsed["nodes"]] == ["Level", "Floor", "Walls"]


def _tileset(columns: int = 8, rows: int = 6) -> str:
    """A TileSet whose atlas actually DEFINES its tiles.

    The "N:M/0 = 0" lines are not decoration — a cell pointing at a coordinate
    the atlas never defined draws nothing and reports nothing, so a fixture
    without them cannot catch the failure that matters.
    """
    defined = "".join(f"{x}:{y}/0 = 0\n"
                      for y in range(rows) for x in range(columns))
    return ('[gd_resource type="TileSet" format=3]\n\n'
            '[ext_resource type="Texture2D" path="res://t.png" id="1_t"]\n\n'
            '[sub_resource type="TileSetAtlasSource" id="A"]\n'
            'texture = ExtResource("1_t")\n'
            'texture_region_size = Vector2i(16, 16)\n'
            + defined +
            '\n[resource]\ntile_size = Vector2i(16, 16)\n'
            'sources/0 = SubResource("A")\n')


TILESET = _tileset()


def test_the_engine_side_reader_gets_the_tiles_back_out():
    """End to end: plan -> autotile -> encode -> .tscn -> the draw list the
    viewport renders. If any link inverts the format the count breaks here."""
    from bgate_core import scenedraw

    tileset = TILESET
    level = levelgen.plan(40, 32, seed=4)
    out = _wire(SCENE, level)
    drawn = scenedraw.draw_list(
        out["text"],
        read=lambda p: tileset if p == "res://tiles/dungeon.tres" else None,
        size_of=lambda p: (16, 16),
        rel_of=lambda p: p.replace("res://", "game/"))
    floor = next(i for i in drawn["items"] if i["name"] == "Floor")["draw"]
    assert floor["kind"] == "tiles"
    assert len(floor["cells"]) == len(level["floor"])


# ---------------------------------------------------------------------------
# The MCP tools
# ---------------------------------------------------------------------------
import json                                                    # noqa: E402

from bgate_mcp import server                                   # noqa: E402


async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.fixture()
def game(root, monkeypatch):
    """A Godot project with one tileset in it, inside a bgate root."""
    monkeypatch.setenv("BGATE_ROOT", str(root))
    gd = root / "game"
    (gd / "tiles").mkdir(parents=True)
    (gd / "project.godot").write_text('config_version=5\n', encoding="utf-8")
    (gd / "tiles" / "dungeon.tres").write_text(TILESET, encoding="utf-8")
    return gd


@pytest.mark.anyio
class TestLevelTools:
    async def test_plan_shows_the_level_without_touching_anything(self):
        out = await call("level_plan", width=40, height=32, seed=2)
        assert out["ok"] and out["connected"] is True
        assert out["room_count"] >= 2
        assert "#" in out["ascii"] and "." in out["ascii"]

    async def test_plan_refuses_parameters_that_cannot_produce_a_level(self):
        out = await call("level_plan", width=40, height=40, min_leaf=5,
                         min_room=6)
        assert out["ok"] is False and "min_leaf" in out["error"]

    async def test_generate_writes_the_layers_into_a_new_scene(self, game):
        out = await call("level_generate", godot_project=str(game),
                         scene="scenes/level.tscn", tileset="tiles/dungeon.tres",
                         width=40, height=32, seed=5, create=True)
        assert out["ok"] and out["written"] and out["connected"]
        text = (game / "scenes" / "level.tscn").read_text(encoding="utf-8")
        assert text.count('type="TileMapLayer"') == 2
        assert out["created"] is True

    async def test_regenerating_leaves_one_floor_and_one_walls(self, game):
        kw = dict(godot_project=str(game), scene="scenes/level.tscn",
                  tileset="tiles/dungeon.tres", create=True)
        await call("level_generate", seed=1, **kw)
        out = await call("level_generate", seed=2, **kw)
        text = (game / "scenes" / "level.tscn").read_text(encoding="utf-8")
        assert text.count('type="TileMapLayer"') == 2
        assert [ly["action"] for ly in out["layers"]] == ["replace", "replace"]
        assert out["backup"], "the previous scene is still on disk"

    async def test_a_source_the_tileset_does_not_have_is_refused(self, game):
        out = await call("level_generate", godot_project=str(game),
                         scene="scenes/level.tscn", tileset="tiles/dungeon.tres",
                         wall_source=9, create=True)
        assert out["ok"] is False and "no source" in out["error"]
        assert not (game / "scenes" / "level.tscn").exists()

    async def test_a_missing_scene_is_refused_unless_create_is_asked_for(self, game):
        out = await call("level_generate", godot_project=str(game),
                         scene="scenes/level.tscn", tileset="tiles/dungeon.tres")
        assert out["ok"] is False and "create=true" in out["error"]

    async def test_dry_run_reports_the_edit_and_writes_nothing(self, game):
        out = await call("level_generate", godot_project=str(game),
                         scene="scenes/level.tscn", tileset="tiles/dungeon.tres",
                         create=True, dry_run=True)
        assert out["ok"] and out["written"] is False
        assert not (game / "scenes" / "level.tscn").exists()

    async def test_a_path_outside_the_godot_project_is_refused(self, game):
        out = await call("level_generate", godot_project=str(game),
                         scene="../../escape.tscn",
                         tileset="tiles/dungeon.tres", create=True)
        assert out["ok"] is False and "outside" in out["error"]

    async def test_a_sheet_too_small_for_the_layout_is_refused(self, game):
        """A 47-blob laid out on a sheet that only defines 16 tiles points at
        coordinates the atlas never declared. Godot places nothing there and
        says nothing — the level is invisible exactly where the shape is
        hardest, which is the last place anyone looks."""
        (game / "tiles" / "small.tres").write_text(_tileset(columns=4, rows=4),
                                                   encoding="utf-8")
        out = await call("level_generate", godot_project=str(game),
                         scene="scenes/level.tscn", tileset="tiles/small.tres",
                         wall_layout="blob47", create=True)
        assert out["ok"] is False
        assert "does not define these atlas tiles" in out["error"]
        assert not (game / "scenes" / "level.tscn").exists()

    async def test_a_layout_that_fits_the_sheet_is_accepted(self, game):
        """The same 16-tile sheet with grid16, which is what it is for."""
        (game / "tiles" / "small.tres").write_text(_tileset(columns=4, rows=4),
                                                   encoding="utf-8")
        out = await call("level_generate", godot_project=str(game),
                         scene="scenes/level.tscn", tileset="tiles/small.tres",
                         wall_layout="grid16", wall_columns=4, create=True)
        assert out["ok"] and out["written"]

    async def test_a_side_scroller_project_is_refused_the_top_down_geometry(
            self, root, game):
        """Under gravity a connected floor guarantees nothing, so the guard
        points at the generator that builds for the jump instead."""
        from bgate_core import gameview
        gameview.save(root, "side_scroller")
        out = await call("level_generate", godot_project=str(game),
                         scene="scenes/level.tscn", tileset="tiles/dungeon.tres",
                         create=True)
        assert out["ok"] is False and "sidescroll_generate" in out["error"]
        assert not (game / "scenes" / "level.tscn").exists()

    async def test_a_top_down_project_is_refused_the_platformer_geometry(
            self, game):
        """The mirror of the guard above: the default view is top_down, and
        sidescroll_generate refuses it before touching the scene."""
        out = await call("sidescroll_generate", godot_project=str(game),
                         scene="scenes/level.tscn", tileset="tiles/dungeon.tres",
                         create=True)
        assert out["ok"] is False and "game_view_set" in out["error"]
        assert not (game / "scenes" / "level.tscn").exists()


ISO_TILESET = TILESET.replace("\n[resource]\ntile_size",
                              "\n[resource]\ntile_shape = 1\n"
                              "tile_layout = 5\ntile_size")

PLAYER_GD = (
    "extends CharacterBody2D\n"
    "@export var speed := 220.0\n"
    "@export var jump_velocity := -380.0\n"
    "@export var gravity := 980.0\n"
    "@export var fall_multiplier := 1.6\n")

PLAYER_TSCN = (
    '[gd_scene load_steps=2 format=3]\n\n'
    '[ext_resource type="Script" path="res://scripts/player.gd" id="1_p"]\n\n'
    '[node name="Player" type="CharacterBody2D"]\n'
    'script = ExtResource("1_p")\n')


@pytest.mark.anyio
class TestTheViewRoutesTheGeometry:
    """The tileset's shape and the project's view must tell one story."""

    async def test_an_isometric_project_refuses_a_square_tileset(
            self, root, game):
        from bgate_core import gameview
        gameview.save(root, "isometric")
        out = await call("level_generate", godot_project=str(game),
                         scene="scenes/level.tscn",
                         tileset="tiles/dungeon.tres", create=True)
        assert out["ok"] is False and "shape" in out["error"]
        assert not (game / "scenes" / "level.tscn").exists()

    async def test_a_top_down_project_refuses_a_diamond_tileset(
            self, game):
        (game / "tiles" / "iso.tres").write_text(ISO_TILESET,
                                                 encoding="utf-8")
        out = await call("level_generate", godot_project=str(game),
                         scene="scenes/level.tscn", tileset="tiles/iso.tres",
                         create=True)
        assert out["ok"] is False and "shape" in out["error"]

    async def test_an_isometric_level_y_sorts_everything_above_the_floor(
            self, root, game):
        """A wall drawn after the player standing south of it reads as the
        player inside the wall — depth sort or the projection is a lie."""
        from bgate_core import gameview
        gameview.save(root, "isometric")
        (game / "tiles" / "iso.tres").write_text(ISO_TILESET,
                                                 encoding="utf-8")
        out = await call("level_generate", godot_project=str(game),
                         scene="scenes/level.tscn", tileset="tiles/iso.tres",
                         seed=5, create=True)
        assert out["ok"] and out["written"]
        text = (game / "scenes" / "level.tscn").read_text(encoding="utf-8")
        walls = text[text.index('[node name="Walls"'):]
        walls = walls[:walls.index("[node") if "[node" in walls[1:] else None]
        assert "y_sort_enabled = true" in walls
        floor = text[text.index('[node name="Floor"'):text.index('[node name="Walls"')]
        assert "y_sort_enabled" not in floor

    async def test_a_tileset_is_composed_from_two_kie_textures(
            self, game, monkeypatch):
        """The provider rule: kie draws the materials, geometry does the
        rest. Coverage is total by construction — no roll to fail."""
        from PIL import Image

        from bgate_adapters import kie
        from bgate_core import tilemap

        calls = []

        def fake_generate(prompt, out_path, **kw):
            calls.append(kw)
            colour = (200, 180, 140, 255) if len(calls) == 1 else (20, 8, 30, 255)
            Image.new("RGBA", (1024, 1024), colour).save(out_path)
            return {"ok": True, "path": str(out_path), "estimated_usd": 0.02}

        monkeypatch.setattr(kie, "generate_image", fake_generate)
        out = await call("tileset_generate", name="kietiles",
                         prompt="warm sandstone floor", bits=4)
        assert out["ok"], out.get("error")
        assert out["spend"]["provider"] == "kie"
        assert out["coverage"] == {"have": 16, "want": 16,
                                   "constructed": True}
        assert len(calls) == 2 and all(k.get("tileable") for k in calls)
        parsed = tilemap.parse_tileset(
            open(out["tileset"], encoding="utf-8").read())
        assert sorted(len(s["tiles"]) for s in parsed["sources"].values()) == [16]
        assert out["collision"]["tiles"] > 0

    async def test_the_iso_tileset_generator_refuses_rather_than_misreads(
            self, root, game):
        """The mask detector reads square boundaries; a diamond's corners are
        transparent by construction. Refusing with the reason beats a
        confident wrong tileset."""
        from bgate_core import gameview
        gameview.save(root, "isometric")
        out = await call("tileset_generate", name="isoset",
                         prompt="stone floor meeting void")
        assert out["ok"] is False and "isometric" in out["error"]


@pytest.mark.anyio
class TestThePlayerCarriesItsOwnJump:
    """sidescroll_generate(player_scene=...) — the handoff, closed."""

    @pytest.fixture()
    def platformer(self, root, game):
        from bgate_core import gameview
        gameview.save(root, "side_scroller")
        (game / "scripts").mkdir()
        (game / "scripts" / "player.gd").write_text(PLAYER_GD,
                                                    encoding="utf-8")
        (game / "player.tscn").write_text(PLAYER_TSCN, encoding="utf-8")
        return game

    async def test_the_jump_is_read_from_the_player_and_the_player_is_placed(
            self, platformer):
        out = await call("sidescroll_generate", godot_project=str(platformer),
                         scene="scenes/level.tscn",
                         tileset="tiles/dungeon.tres",
                         player_scene="player.tscn",
                         length=80, height=14, seed=3, create=True)
        assert out["ok"], out.get("error")
        assert out["jump"]["source"] == "player_scene"
        assert out["player"]["placed"] == "added"
        assert out["player"]["position"] == out["spawn_px"]
        assert out["player"]["pixels"]["speed"] == 220.0
        text = (platformer / "scenes" / "level.tscn").read_text(
            encoding="utf-8")
        assert 'instance=ExtResource' in text
        assert f"Vector2({out['spawn_px'][0]}, {out['spawn_px'][1]})" in text

    async def test_rerunning_moves_the_player_instead_of_stacking_a_second(
            self, platformer):
        kw = dict(godot_project=str(platformer), scene="scenes/level.tscn",
                  tileset="tiles/dungeon.tres", player_scene="player.tscn",
                  length=80, height=14, create=True)
        await call("sidescroll_generate", seed=3, **kw)
        out = await call("sidescroll_generate", seed=9, **kw)
        assert out["ok"] and out["player"]["placed"] == "moved"
        text = (platformer / "scenes" / "level.tscn").read_text(
            encoding="utf-8")
        assert text.count('[node name="Player"') == 1

    async def test_a_player_scene_that_does_not_exist_is_refused(
            self, platformer):
        out = await call("sidescroll_generate", godot_project=str(platformer),
                         scene="scenes/level.tscn",
                         tileset="tiles/dungeon.tres",
                         player_scene="ghost.tscn", create=True)
        assert out["ok"] is False and "player" in out["error"].lower()

    async def test_a_player_with_no_tunables_is_refused_naming_them(
            self, platformer):
        (platformer / "bare.tscn").write_text(
            '[gd_scene format=3]\n\n'
            '[node name="Bare" type="CharacterBody2D"]\n', encoding="utf-8")
        out = await call("sidescroll_generate", godot_project=str(platformer),
                         scene="scenes/level.tscn",
                         tileset="tiles/dungeon.tres",
                         player_scene="bare.tscn", create=True)
        assert out["ok"] is False and "speed" in out["error"]

    async def test_a_prop_manifest_teaches_the_tileset_its_atlas(
            self, root, platformer):
        """The seam the acceptance run hit live: prop cells referenced a
        source id the tileset never defined — KeyError in the gap check,
        and had it slipped past, Godot would draw nothing and say nothing.
        The manifest now becomes a source on first use."""
        from bgate_core import tilemap

        (root / "assets" / "tiles").mkdir(parents=True, exist_ok=True)
        (root / "assets" / "tiles" / "props.json").write_text(
            json.dumps({
                "name": "p", "view": "side_scroller", "tile_px": 16,
                "texture": "res://assets/tiles/p_props.png",
                "types": ["barrel"], "atlas": {"barrel": [0, 0]},
                "tiles": [[0, 0]], "sizes": {}, "origins": {},
                "animation": {}}), encoding="utf-8")
        out = await call("sidescroll_generate", godot_project=str(platformer),
                         scene="scenes/level.tscn",
                         tileset="tiles/dungeon.tres",
                         player_scene="player.tscn",
                         prop_manifest="assets/tiles/props.json",
                         length=80, height=14, seed=3, create=True)
        assert out["ok"], out.get("error")
        assert out["props"]["placed"] > 0
        back = tilemap.parse_tileset(
            (platformer / "tiles" / "dungeon.tres").read_text(
                encoding="utf-8"))
        assert any(s["texture"] == "res://assets/tiles/p_props.png"
                   for s in back["sources"].values())

    async def test_the_scene_override_beats_the_script_default(
            self, platformer):
        """Same precedence the engine uses. Half a gravity means twice the
        modelled arc, so the spec must see what the game will run."""
        (platformer / "floaty.tscn").write_text(
            PLAYER_TSCN.replace('script = ExtResource("1_p")\n',
                                'script = ExtResource("1_p")\n'
                                'gravity = 490.0\n'),
            encoding="utf-8")
        out = await call("sidescroll_generate", godot_project=str(platformer),
                         scene="scenes/level.tscn",
                         tileset="tiles/dungeon.tres",
                         player_scene="floaty.tscn",
                         length=80, height=14, seed=3, create=True)
        assert out["ok"], out.get("error")
        assert out["player"]["pixels"]["gravity"] == 490.0
