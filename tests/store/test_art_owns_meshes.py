"""Every visible mesh is the art seat's, whichever route makes it.

Hot Cargo, 2026-09-04: the director filed the whole 3D cast (player, runner,
cruiser, drone - primitives authored inline in .tscn) to TECH because the
bible's look said "boxes and cylinders, no imported meshes" and art's lanes did
not reach a scene file. Nobody measured a single model. These pin the three
places that now say otherwise.
"""

from bgate_core.board import seats


def test_art_lanes_reach_mesh_bearing_scenes():
    can = seats.DEFAULT_SEATS["art"]["write_globs"]
    from fnmatch import fnmatch
    for path in ("game/scenes/props/crate.tscn", "game/scenes/characters/runner.tscn",
                 "game/scenes/vehicles/cruiser.tscn", "game/scenes/enemies/drone_model.tscn",
                 "game/scenes/world/models/tower.tscn"):
        assert any(fnmatch(path, g) or fnmatch(path, g.replace("**/", "")) for g in can), path


def test_the_art_mission_claims_primitives():
    mission = seats.DEFAULT_SEATS["art"]["mission"]
    assert "EVERY VISIBLE MESH IS ART'S" in mission
    assert "BoxMesh" in mission and "not a reassignment to tech" in mission


def test_the_cast_is_generated_and_a_primitives_only_bible_is_a_human_call():
    d = seats.DEFAULT_SEATS["director"]["mission"]
    assert "THE CAST IS GENERATED" in d and "ask_human before" in d
    assert "a keyed provider is not an install" in d
    w = seats.ART_3D_WORKFLOW
    assert "NOT the cast" in w and "PRIMITIVES ONLY" in w and "ask_human" in w
    assert "props, vehicles, terrain" not in w
