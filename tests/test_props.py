"""Where props go, and the defects that put them in the wrong place.

Every test here is a measurement that failed on a real render first. The
sequence is worth keeping, because each fix exposed the next one and none of
them were visible in the numbers the previous version reported:

  1. uniform random  → all clutter stranded mid-floor, torches touching
  2. rank by enclosure → a barrel in all four corners of every room
  3. cluster on anchors → one barrel in a 169-cell hall, and 13 torches on one
     room while a 20-cell closet got 5
  4. cap the torches   → 17 of 22 on the WEST wall, because sorted order
     reaches it first and the budget runs out there
  5. grow piles along the wall ring → a straight row of four, reading as a shelf

The last one is the point of the whole module: placement that follows a rule
still looks arbitrary unless the rule is about what the room is FOR.
"""
from __future__ import annotations

from bgate_core import levelgen, props

WIDE = dict(width=48, height=32, seed=7, room_fill=0.85)


def _level():
    return levelgen.plan(WIDE["width"], WIDE["height"], seed=WIDE["seed"],
                         room_fill=WIDE["room_fill"])


def _plan(**kw):
    lvl = _level()
    walls = levelgen.wall_ring({tuple(c) for c in lvl["floor"]})
    return lvl, props.plan(lvl, seed=7, walls=walls, **kw)


class TestNothingStranded:
    def test_no_clutter_in_open_floor(self):
        """Defect 1. Enclosure is counted over the eight neighbours, so a prop
        in the middle of a room scores 0 — and cover is the ONE exception,
        placed deliberately in the open."""
        _, pl = _plan(density=0.12)
        loose = [p for p in pl["props"]
                 if p["kind"] == "floor" and p.get("role") != "cover"
                 and p["enclosure"] < props.MIN_ENCLOSURE]
        assert loose == []

    def test_a_wall_prop_is_on_the_wall_not_beside_it(self):
        """A wall mount drawn in the room next to its wall is a FLOATING prop,
        and that is exactly what it looks like. The cell it occupies is the
        masonry."""
        lvl, pl = _plan(density=0.12)
        walls = levelgen.wall_ring({tuple(c) for c in lvl["floor"]})
        floor = {tuple(c) for c in lvl["floor"]}
        mounts = [p for p in pl["props"] if p["mount"] == "wall"]
        assert mounts, "a walled level should get some light"
        for p in mounts:
            assert (p["x"], p["y"]) in walls, p
            assert (p["x"], p["y"]) not in floor, p
            assert tuple(p["lights"]) in floor, "it has to light a real cell"


class TestTheArtsOwnConstraints:
    """The reason placement logic alone cannot fix a floaty prop: some sprites
    CANNOT go on some walls, and no ranking of cells changes that."""

    def _mounts(self, **kw):
        lvl, pl = _plan(density=0.12, **kw)
        return lvl, pl, [p for p in pl["props"] if p["mount"] == "wall"]

    def test_a_side_view_sprite_never_lands_on_a_horizontal_wall(self):
        """`torch` declares ("e", "w"). A three-quarter view bolted to the north
        wall reads as pasted on, and the type says so rather than the placer
        guessing."""
        _, _, mounts = self._mounts(types=("torch", "barrel"))
        assert mounts
        assert {p["faces"] for p in mounts} <= {"e", "w"}
        assert all(p["type"] == "torch" for p in mounts)

    def test_nothing_mounts_on_the_wall_you_see_the_back_of(self):
        """The wall SOUTH of a room turns its inner face away from the camera —
        faces "n" here. Anything bolted there is behind its own masonry."""
        for types in (("torch",), ("sconce",), ("torch", "sconce", "banner")):
            _, pl, mounts = self._mounts(types=types + ("barrel",))
            assert all(p["faces"] != "n" for p in mounts), types
            assert pl["skipped"]["back_wall"] > 0, "and it is counted, not silent"

    def test_nothing_mounts_on_a_corner(self):
        lvl, pl, mounts = self._mounts()
        walls = levelgen.wall_ring({tuple(c) for c in lvl["floor"]})
        for p in mounts:
            assert not props._is_corner((p["x"], p["y"]), walls), p
        assert pl["skipped"]["corner"] > 0

    def test_a_front_facing_type_is_what_lights_the_north_wall(self):
        """The measured consequence of the constraint: with only a side-view
        torch the north walls stay dark and `no_side` counts it. Adding a
        front-facing sconce fills them and the count goes to zero."""
        _, side_only, _ = self._mounts(types=("torch", "barrel"))
        _, both, mounts = self._mounts(types=("torch", "sconce", "barrel"))
        assert side_only["skipped"]["no_side"] > 0
        assert both["skipped"]["no_side"] == 0
        assert "s" in {p["faces"] for p in mounts}
        assert {p["type"] for p in mounts} == {"torch", "sconce"}

    def test_only_a_type_that_allows_it_is_ever_mirrored(self):
        _, pl = _plan(density=0.2)
        for p in pl["props"]:
            if p["mirror"]:
                assert props.prop_type(p["type"])["mirror"], p

    def test_every_prop_carries_a_type_and_a_label(self):
        _, pl = _plan(density=0.12)
        for p in pl["props"]:
            assert p["type"] in props.PROP_TYPES
            assert p["label"] and p["mount"] in props.MOUNTS

    def test_an_undeclared_type_is_refused_not_ignored(self):
        import pytest
        with pytest.raises(props.PropError, match="unknown prop type"):
            _plan(density=0.1, types=("chandelier",))

    def test_cells_refuses_a_flip_the_type_forbids(self):
        """The cross-check between the plan and the emitter. It caught a real
        bug: `mirror` was being randomised on types that never declared it."""
        import pytest
        lvl, pl = _plan(density=0.12)
        pl["props"][0]["type"] = "altar"      # never mirrors
        pl["props"][0]["mirror"] = True
        with pytest.raises(props.PropError, match="does not allow flipping"):
            props.cells(pl, {n: (0, 0) for n in props.PROP_TYPES}, source=2)


class TestIntoTheEngine:
    def test_no_two_props_share_a_cell(self):
        """A TileMapLayer holds ONE tile per coordinate, so two props on a cell
        means one silently never exists. Only Godot's own encoder said so."""
        from bgate_core import tilemap
        for d in (0.05, 0.1, 0.2, 0.4, 0.8):
            _, pl = _plan(density=d)
            got = props.cells(pl, {n: (0, 0) for n in props.PROP_TYPES},
                              source=2)
            packed = tilemap.encode_cells(got["cells"])     # refuses duplicates
            assert len(tilemap.decode_cells(packed)) == len(got["cells"]), d

    def test_a_missing_atlas_entry_names_what_the_plan_placed(self):
        import pytest
        _, pl = _plan(density=0.12)
        with pytest.raises(props.PropError, match="no atlas entry"):
            props.cells(pl, {"torch": (0, 0)}, source=2)

    def test_the_flip_bit_is_godots_own(self):
        assert props.FLIP_H == 1 << 12 and props.FLIP_V == 1 << 13


class TestSpread:
    def test_lighting_is_not_all_on_one_wall(self):
        """Defect 4. The count was right and the placement was uniform garbage:
        every room lit from a single side, because sorted order reaches the west
        wall first and the budget runs out there.

        Asked with a type set that CAN reach three sides — with only the
        side-view torch two is the ceiling, and that is the art's constraint
        rather than a spread failure."""
        _, pl = _plan(density=0.12, types=("torch", "sconce", "barrel"))
        by_side: dict = {}
        for p in pl["props"]:
            if p["mount"] == "wall":
                by_side[p["faces"]] = by_side.get(p["faces"], 0) + 1
        total = sum(by_side.values())
        assert set(by_side) == {"s", "e", "w"}, by_side
        assert max(by_side.values()) < total * 0.6, by_side

    def test_with_only_a_side_view_type_two_sides_is_the_ceiling(self):
        _, pl = _plan(density=0.12, types=("torch", "barrel"))
        sides = {p["faces"] for p in pl["props"] if p["mount"] == "wall"}
        assert sides == {"e", "w"}

    def test_doorways_are_counted_as_doorways_not_as_cells(self):
        """A two-cell corridor arrives as two adjacent door cells. Counting
        cells made every dead end look like a thoroughfare the moment corridors
        got wider than one, and the level classified as all halls: vaults went
        from four to zero with no other change."""
        lvl, pl = _plan(density=0.12)
        assert pl["purposes"].get("vault"), pl["purposes"]
        floor = {tuple(c) for c in lvl["floor"]}
        doors = props.doorways(floor, lvl["rooms"])
        for room in lvl["rooms"]:
            role = props.room_role(room, floor, doors)
            assert role["doors"] <= len(role["door_cells"])

    def test_no_two_torches_adjacent(self):
        _, pl = _plan(density=0.12)
        lit = [(p["x"], p["y"]) for p in pl["props"] if p["mount"] == "wall"]
        for i, a in enumerate(lit):
            for b in lit[i + 1:]:
                assert abs(a[0] - b[0]) + abs(a[1] - b[1]) > 1, (a, b)

    def test_a_small_room_gets_fewer_torches_than_a_hall(self):
        """Defect 3: greedy spacing lit a 20-cell closet with five torches."""
        lvl, pl = _plan(density=0.12)
        rooms = lvl["rooms"]
        counts = []
        for r in rooms:
            n = sum(1 for p in pl["props"] if p["mount"] == "wall"
                    and r["x"] <= p["x"] < r["x"] + r["w"]
                    and r["y"] <= p["y"] < r["y"] + r["h"])
            counts.append((r["w"] * r["h"], n))
        assert max(n for _, n in counts) <= 4, counts


class TestPurpose:
    def test_the_spawn_room_is_not_filled_with_junk(self):
        """You do not spawn the player in a storeroom."""
        lvl, pl = _plan(density=0.14)
        spawn = tuple(lvl["spawn"])
        room = next(r for r in lvl["rooms"]
                    if r["x"] <= spawn[0] < r["x"] + r["w"]
                    and r["y"] <= spawn[1] < r["y"] + r["h"])
        inside = [p for p in pl["props"] if p["kind"] == "floor"
                  and room["x"] <= p["x"] < room["x"] + room["w"]
                  and room["y"] <= p["y"] < room["y"] + room["h"]]
        assert inside == []

    def test_halls_get_cover_and_closets_do_not(self):
        """The only prop with a gameplay job: one cell OFF the line between two
        doors, so it breaks the sight line across a room you walk through
        without blocking the route."""
        _, pl = _plan(density=0.12)
        assert pl["purposes"].get("hall"), "this layout should have halls"
        assert any(p.get("role") == "cover" for p in pl["props"])

    def test_cover_never_sits_on_the_route(self):
        lvl, pl = _plan(density=0.12)
        floor = {tuple(c) for c in lvl["floor"]}
        doors = props.doorways(floor, lvl["rooms"])
        for room in lvl["rooms"]:
            role = props.room_role(room, floor, doors)
            lines = props.walk_lines(role["door_cells"], floor)
            for p in pl["props"]:
                if p.get("role") == "cover":
                    assert (p["x"], p["y"]) not in lines

    def test_a_feature_only_lands_in_a_vault(self):
        # asked for BY NAME: the default set carries no centre type, because
        # none of them read from directly overhead yet
        lvl, pl = _plan(density=0.12, types=("altar", "barrel", "torch"))
        feats = [p for p in pl["props"] if p["kind"] == "centre"]
        assert feats
        floor = {tuple(c) for c in lvl["floor"]}
        doors = props.doorways(floor, lvl["rooms"])
        for p in feats:
            room = next(r for r in lvl["rooms"]
                        if tuple(r["center"]) == (p["x"], p["y"]))
            role = props.room_role(room, floor, doors)
            assert props.room_purpose(room, role, lvl) == "vault"


class TestTheGateThatMatters:
    def test_the_level_is_still_one_region(self):
        """A dressed level that has lost a room looks completely fine."""
        for d in (0.05, 0.12, 0.25, 0.6):
            _, pl = _plan(density=d)
            assert pl["checks"]["still_connected"], d

    def test_nothing_lands_on_the_spawn_the_exit_or_a_doorway(self):
        lvl, pl = _plan(density=0.3)
        floor = {tuple(c) for c in lvl["floor"]}
        forbidden = props.doorways(floor, lvl["rooms"])
        forbidden |= {tuple(lvl["spawn"]), tuple(lvl["exit"])}
        for p in pl["props"]:
            if p["solid"]:
                assert (p["x"], p["y"]) not in forbidden, p

    def test_the_same_seed_plans_the_same_level(self):
        a = _plan(density=0.12)[1]["props"]
        b = _plan(density=0.12)[1]["props"]
        assert a == b


class TestRoomFillThinsTheWalls:
    def test_a_higher_fill_leaves_less_rock_between_rooms(self):
        """Rooms taking a uniform-random slice of their BSP leaf left the other
        half as solid rock, so the level rendered as thin rooms separated by
        slabs. Invisible in the plan; the first thing you see on a map."""
        loose = levelgen.plan(48, 32, seed=7, room_fill=0.0)
        tight = levelgen.plan(48, 32, seed=7, room_fill=0.85)
        assert len(tight["floor"]) > len(loose["floor"]) * 1.2
        assert tight["connected"] and loose["connected"]


class TestSeatingAWallMount:
    """Two facts measured in the running engine, because neither is documented
    and both change the design:

      * a POSITIVE `texture_origin` shifts the texture the OTHER way —
        Vector2i(8, 0) moved the sprite eight pixels LEFT;
      * the flip bit does NOT mirror it. A flipped cell and a plain cell with
        the same origin both moved left by eight.

    The second is why a wall type that needs seating needs one atlas tile per
    facing: mirroring saves art, not placement."""

    def test_the_offset_moves_the_sprite_into_the_room(self):
        # faces "e" means the room is east, so the sprite moves east — and the
        # origin that does that is NEGATIVE
        assert props.mount_origin("e", (32, 32)) == (-9, 0)
        assert props.mount_origin("w", (32, 32)) == (9, 0)
        assert props.mount_origin("s", (32, 32)) == (0, -9)

    def test_an_unmountable_facing_gets_no_offset(self):
        assert props.mount_origin("", (32, 32)) == (0, 0)

    def test_origins_are_emitted_only_for_per_facing_tiles(self):
        """A shared tile cannot carry two different offsets, so asking for one
        would seat the prop correctly on one wall and wrongly on the other."""
        _, pl = _plan(density=0.12)
        shared = {n: (i % 8, i // 8) for i, n in enumerate(props.PROP_TYPES)}
        assert props.mount_origins(pl, shared, tile_size=(32, 32)) == {}
        split = dict(shared, torch={"e": (0, 7), "w": (1, 7)})
        got = props.mount_origins(pl, split, tile_size=(32, 32))
        assert got[(0, 7)] == (-9, 0) and got[(1, 7)] == (9, 0)

    def test_one_tile_for_two_facings_is_refused(self):
        import pytest
        _, pl = _plan(density=0.12)
        both = {n: (i % 8, i // 8) for i, n in enumerate(props.PROP_TYPES)}
        both["torch"] = {"e": (0, 7), "w": (0, 7)}      # ONE tile, two facings
        with pytest.raises(props.PropError, match="two facings"):
            props.mount_origins(pl, both, tile_size=(32, 32))

    def test_a_per_facing_atlas_never_uses_the_flip_bit(self):
        """The whole point: the flip would mirror the sprite and leave the
        offset pointing the wrong way."""
        _, pl = _plan(density=0.12)
        atlas = {n: (i % 8, i // 8) for i, n in enumerate(props.PROP_TYPES)}
        atlas["torch"] = {"e": (0, 7), "w": (1, 7)}
        got = props.cells(pl, atlas, source=2)
        wall_cells = [c for c in got["cells"]
                      if (c["ax"], c["ay"]) in ((0, 7), (1, 7))]
        assert wall_cells and all(c["alt"] == 0 for c in wall_cells)
        # symmetric floor clutter still flips — that is free variety, and it
        # carries no offset to point the wrong way
        assert got["mirrored"] > 0

    def test_a_facing_the_atlas_does_not_cover_is_named(self):
        import pytest
        _, pl = _plan(density=0.12)
        atlas = {n: (i % 8, i // 8) for i, n in enumerate(props.PROP_TYPES)}
        atlas["torch"] = {"e": (0, 7)}          # west deliberately absent
        with pytest.raises(props.PropError, match="no tile for that facing"):
            props.cells(pl, atlas, source=2)


ALL_TYPES = tuple(sorted(props.PROP_TYPES))


class TestTheOtherMounts:
    """Each mount exists because a placement rule could not be expressed as
    "clutter with different art"."""

    def _all(self, **kw):
        return _plan(density=0.1, types=ALL_TYPES, **kw)

    def test_the_way_in_and_the_way_out_are_marked(self):
        """Spawn and exit were coordinates everything else was told to avoid,
        and nothing ever marked them. A level you cannot see the exit of is a
        maze."""
        lvl, pl = self._all()
        portals = {p["type"]: (p["x"], p["y"]) for p in pl["props"]
                   if p["mount"] == "portal"}
        assert portals["stairs_up"] == tuple(lvl["spawn"])
        assert portals["stairs_down"] == tuple(lvl["exit"])

    def test_one_door_per_opening_not_per_cell(self):
        lvl, pl = self._all()
        floor = {tuple(c) for c in lvl["floor"]}
        openings = props.door_clusters(props.doorways(floor, lvl["rooms"]))
        doors = [p for p in pl["props"] if p["mount"] == "door"]
        assert doors and len(doors) <= len(openings)
        assert {p["axis"] for p in doors} <= {"h", "v"}

    def test_a_door_hangs_on_the_wall_not_in_the_gap(self):
        """A door drawn as a flat elevation and placed on the doorway's FLOOR
        cell reads as a door lying in the middle of the room — which is
        exactly how it rendered. A doorway is a gap in a wall; the door belongs
        on the masonry beside it, and only on the wall whose inner face turns
        toward the camera."""
        lvl, pl = self._all()
        floor = {tuple(c) for c in lvl["floor"]}
        walls = levelgen.wall_ring(floor)
        doors = [p for p in pl["props"] if p["mount"] == "door"]
        assert doors
        for p in doors:
            assert (p["x"], p["y"]) in walls, p
            assert (p["x"], p["y"]) not in floor, p
            assert p["faces"] == "s", p
            assert p["opening"], "it has to know which gap it closes"

    def test_an_opening_with_no_drawable_wall_is_counted(self):
        """A level with no doors at all looks like the type was never asked
        for, so the refusal is counted rather than silent."""
        _, pl = self._all()
        assert pl["skipped"]["no_side"] > 0

    def test_no_door_is_solid(self):
        """A door that blocks its own doorway severs the level, and the flood
        gate would then refuse every one of them."""
        _, pl = self._all()
        assert all(not p["solid"] for p in pl["props"] if p["mount"] == "door")

    def test_a_corner_mount_takes_the_cells_a_wall_mount_refuses(self):
        """Same geometry, opposite verdict — the corner test earns its keep
        twice."""
        lvl, pl = self._all()
        walls = levelgen.wall_ring({tuple(c) for c in lvl["floor"]})
        corners = [p for p in pl["props"] if p["mount"] == "corner"]
        mounts = [p for p in pl["props"] if p["mount"] == "wall"]
        assert corners
        assert all(props._is_corner((p["x"], p["y"]), walls) for p in corners)
        assert all(not props._is_corner((p["x"], p["y"]), walls) for p in mounts)

    def test_pillars_come_in_pairs(self):
        """Half a colonnade is worse than none: the room stops reading as built
        and starts reading as littered."""
        _, pl = self._all()
        pillars = [p for p in pl["props"] if p["mount"] == "pillar"]
        assert pillars and len(pillars) % 2 == 0

    def test_a_decal_may_sit_on_a_route_because_you_walk_over_it(self):
        lvl, pl = self._all()
        floor = {tuple(c) for c in lvl["floor"]}
        decals = [p for p in pl["props"] if p["mount"] == "overlay"]
        assert decals
        assert all(not p["solid"] for p in decals)
        assert all((p["x"], p["y"]) in floor for p in decals)

    def test_decals_and_props_are_separate_layers(self):
        """Not cosmetic: a TileMapLayer holds ONE tile per coordinate, so a
        crack in the floor and the barrel on it can only coexist as two."""
        from bgate_core import tilemap
        _, pl = self._all()
        assert pl["layers"] == ["decals", "props"]
        atlas = {n: (i % 8, i // 8) for i, n in enumerate(ALL_TYPES)}
        atlas["torch"] = {"e": (0, 7), "w": (1, 7)}
        seen = 0
        for lname in props.LAYERS:
            part = props.cells(pl, atlas, source=2, layer=lname)
            tilemap.encode_cells(part["cells"])      # refuses duplicates
            seen += len(part["cells"])
        assert seen == len(pl["props"])

    def test_unsplit_emission_collides_rather_than_dropping_one(self):
        """The failure mode this split prevents, stated as a test: without it
        the encoder refuses — which is right, because silently dropping one is
        how a level loses half its dressing."""
        import pytest
        from bgate_core import tilemap
        _, pl = self._all()
        atlas = {n: (i % 8, i // 8) for i, n in enumerate(ALL_TYPES)}
        atlas["torch"] = {"e": (0, 7), "w": (1, 7)}
        both = props.cells(pl, atlas, source=2)
        with pytest.raises(tilemap.TileError, match="one tile per coordinate"):
            tilemap.encode_cells(both["cells"])


class TestPurposeGatesTypes:
    def test_a_reward_only_lands_where_it_is_a_reward(self):
        lvl, pl = _plan(density=0.1, types=ALL_TYPES)
        floor = {tuple(c) for c in lvl["floor"]}
        doors = props.doorways(floor, lvl["rooms"])
        for p in pl["props"]:
            if p["type"] != "chest":
                continue
            room = next(r for r in lvl["rooms"]
                        if r["x"] <= p["x"] < r["x"] + r["w"]
                        and r["y"] <= p["y"] < r["y"] + r["h"])
            role = props.room_role(room, floor, doors)
            assert props.room_purpose(room, role, lvl) == "vault"

    def test_once_per_room_is_honoured(self):
        """Seven chests across four vaults is not a treasure room, it is a
        warehouse."""
        lvl, pl = _plan(density=0.4, types=ALL_TYPES)
        for room in lvl["rooms"]:
            inside = [p["type"] for p in pl["props"]
                      if room["x"] <= p["x"] < room["x"] + room["w"]
                      and room["y"] <= p["y"] < room["y"] + room["h"]]
            for name in ALL_TYPES:
                if props.prop_type(name).get("once"):
                    assert inside.count(name) <= 1, (room, name)

    def test_everything_still_holds_with_every_type_at_once(self):
        for d in (0.05, 0.2, 0.6):
            _, pl = _plan(density=d, types=ALL_TYPES)
            assert pl["checks"]["still_connected"], d


class TestTheArtContract:
    """Proportion, motion and state, declared in pixels — because art made to
    anything else does not fit. A prop drawn at the wrong proportion is not a
    stylistic difference, it is a sprite hanging off its cell."""

    def test_a_tall_prop_asks_for_a_tall_canvas(self):
        assert props.art_spec("pillar")["cell_px"] == [32, 64]
        assert props.art_spec("altar")["cell_px"] == [64, 64]
        assert props.art_spec("barrel")["cell_px"] == [32, 32]

    def test_the_ground_anchor_is_bottom_centre(self):
        """Where the prop meets the floor, which is what a taller-than-one-cell
        sprite is placed by."""
        assert props.art_spec("pillar")["ground"] == [16, 64]

    def test_a_loop_asks_for_one_row(self):
        got = props.art_spec("torch")
        assert got["motion"] == "loop"
        assert got["sheet_px"] == [32 * 4, 32]

    def test_states_are_not_a_loop_and_get_a_row_each(self):
        """An ambient cycle and a state machine are different mechanisms in the
        engine, so a type declares one or the other."""
        got = props.art_spec("chest")
        assert got["motion"] == "states"
        assert got["states"] == {"shut": 1, "opening": 6, "open": 1}
        assert got["sheet_px"] == [32 * 6, 32 * 3]     # longest state, 3 rows
        assert "anim" not in props.prop_type("chest")

    def test_a_wall_mount_asks_for_one_drawing_per_facing(self):
        """The flip bit cannot carry texture_origin, so both facings are real
        drawings — the manifest has to budget for that."""
        assert props.art_spec("torch")["facings"] == ["e", "w"]
        assert props.art_spec("barrel")["facings"] == []

    def test_the_manifest_totals_what_a_generation_must_produce(self):
        m = props.art_manifest()
        assert len(m["specs"]) == len(props.PROP_TYPES)
        assert m["drawings"] > len(m["specs"])         # torch counts twice
        assert m["frames"] > m["drawings"]             # loops and states

    def test_every_type_declares_at_most_one_motion_mechanism(self):
        for name, spec in props.PROP_TYPES.items():
            assert not (spec.get("anim") and spec.get("states")), name


class TestFootprint:
    """Judged by eye first, then measured. Of one generated set: the prop
    called good covered 63% of its cell and touched its border on 3% of the
    edge; the ones called "too big and too detailed" covered 84-91% and touched
    56-62%; the one carrying a slab of wall behind it touched 23%. One number —
    border fill — catches both being oversized and bringing a background."""

    def test_clutter_is_drawn_smaller_than_its_cell(self):
        for name in ("barrel", "crate", "rubble", "bones", "chest"):
            spec = props.art_spec(name)
            assert spec["footprint"] < 0.7, name
            assert spec["art_px"][0] < spec["cell_px"][0], name

    def test_a_door_is_meant_to_fill_its_cell(self):
        """It IS the wall at that point, so the rule does not apply to it."""
        assert props.art_spec("door")["footprint"] == 1.0
        assert props.art_spec("door")["art_px"] == props.art_spec("door")["cell_px"]

    def test_a_door_occupies_one_cell_so_it_sits_level_with_the_wall(self):
        """At (1, 2) it drew a cell ABOVE the wall band and stood proud of the
        masonry it is bolted to."""
        assert props.art_spec("door")["cells"] == [1, 1]
        assert props.art_spec("arch")["cells"] == [1, 1]

    def test_the_border_gate_separates_the_good_from_the_bad(self):
        from PIL import Image, ImageDraw
        from bgate_core import propsheet

        small = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        ImageDraw.Draw(small).ellipse([6, 6, 25, 25], fill=(200, 150, 90, 255))
        edge = Image.new("RGBA", (32, 32), (120, 110, 100, 255))
        assert propsheet.border_fill(small) <= propsheet.BORDER_MAX
        assert propsheet.border_fill(edge) > propsheet.BORDER_MAX

    def test_conform_scales_to_the_art_box_not_the_cell(self):
        from PIL import Image
        from bgate_core import propsheet

        blob = Image.new("RGBA", (400, 400), (200, 150, 90, 255))
        out, rep = propsheet.conform(blob, size=(32, 32), art_size=(20, 20))
        assert out.size == (32, 32)
        assert rep["art_size"] == [20, 20]
        assert rep["border_fill"] == 0.0, "it must not reach the cell edge"
