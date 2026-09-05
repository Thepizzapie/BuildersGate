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


def test_the_director_and_the_ownership_rule_route_geometry_to_art():
    assert "goes to ART" in seats.DEFAULT_SEATS["director"]["mission"]
    assert "3D GEOMETRY IS ASSET GENERATION AND ASSET GENERATION IS ART" in seats.OWNERSHIP_RULE
    assert "Tech gets the CollisionShape3D" in seats.OWNERSHIP_RULE
