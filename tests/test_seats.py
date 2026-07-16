"""Seats — the lane oracle, the lock integration, and the brief.

The check with teeth is can_write: it must fail closed for unknown seats, keep
seats in their lanes, and refuse a locked binary even when it's in-lane. That
last case is the whole reason locks and lanes are two separate gates.
"""
from __future__ import annotations

import pytest

from bgate_core import assets, bible, lore, playtest, seats
from bgate_core import db


class TestRoles:
    def test_seven_default_seats(self, root):
        got = seats.roles_for(root)
        assert set(got) == set(seats.ROLES)
        assert len(got) == 7

    def test_disable_a_seat(self, root):
        seats.configure(root, "audio", enabled=False)
        assert "audio" not in seats.roles_for(root)

    def test_override_lanes_per_project(self, root):
        seats.configure(root, "art", write_globs=["sprites/**"])
        assert seats.roles_for(root)["art"]["write_globs"] == ["sprites/**"]
        # Other seats keep code defaults.
        assert seats.roles_for(root)["qa"]["write_globs"] == seats.DEFAULT_SEATS["qa"]["write_globs"]

    def test_reenabling_preserves_overrides(self, root):
        seats.configure(root, "art", write_globs=["sprites/**"])
        seats.configure(root, "art", enabled=False)
        seats.configure(root, "art", enabled=True)
        assert seats.roles_for(root)["art"]["write_globs"] == ["sprites/**"]

    def test_unknown_role_rejected(self, root):
        with pytest.raises(ValueError, match="unknown role"):
            seats.configure(root, "wizard", enabled=True)


class TestCanWrite:
    @pytest.mark.parametrize("role,path,allowed", [
        ("gameplay", "game/scripts/player.gd", True),
        ("gameplay", "game/scenes/main.tscn", True),
        ("gameplay", "game/assets/shard.glb", False),      # art's lane
        ("art", "game/assets/textures/rock.png", True),
        ("art", "game/scripts/player.gd", False),
        ("qa", "tests/test_player.py", True),
        ("qa", "design/pillars.md", False),
        ("narrative", "design/lore/factions.md", True),
        ("tech", "game/project.godot", True),
        ("director", "design/cutline.md", True),
        ("director", "game/scripts/player.gd", False),
    ])
    def test_lanes(self, root, role, path, allowed):
        got = seats.can_write(root, role, path)
        assert got["allowed"] is allowed, got.get("reason")

    def test_unknown_seat_fails_closed(self, root):
        got = seats.can_write(root, "intern", "game/scripts/player.gd")
        assert got["allowed"] is False
        assert "fails closed" in got["reason"]

    def test_disabled_seat_fails_closed(self, root):
        seats.configure(root, "gameplay", enabled=False)
        got = seats.can_write(root, "gameplay", "game/scripts/player.gd")
        assert got["allowed"] is False

    def test_backslash_paths_normalize(self, root):
        got = seats.can_write(root, "gameplay", r"game\scripts\player.gd")
        assert got["allowed"] is True

    def test_in_lane_but_locked_by_another_seat_is_denied(self, root):
        """The case the two-gate design exists for: tech's lane covers game/**,
        but art holds the lock — tech must NOT get through."""
        assets.lock(root, "game/assets/shard.blend", "art")
        got = seats.can_write(root, "tech", "game/assets/shard.blend")
        assert got["allowed"] is False
        assert "locked by seat 'art'" in got["reason"]

    def test_the_lock_holder_writes_freely(self, root):
        assets.lock(root, "game/assets/shard.blend", "art")
        assert seats.can_write(root, "art", "game/assets/shard.blend")["allowed"] is True

    def test_released_lock_reopens_the_lane(self, root):
        path = root / "game" / "assets" / "shard.blend"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"blend")
        assets.lock(root, "game/assets/shard.blend", "art")
        assets.release(root, "game/assets/shard.blend", "art")
        assert seats.can_write(root, "tech", "game/assets/shard.blend")["allowed"] is True


class TestBrief:
    def test_brief_assembles_the_seats_world(self, root):
        bible.add(root, "pillar", "Tension over spectacle")
        lore.add_entity(root, "faction", "The Ashen Order", summary="Zealots.",
                        status="canon")
        lore.add_entity(root, "place", "Cinder Vault", status="draft")  # not canon
        assets.lock(root, "game/assets/shard.blend", "art")
        assets.lock(root, "game/assets/theme.ogg", "audio")
        seats.post_note(root, "director", "Cut line moved above multiplayer",
                        topic="scope")

        got = seats.brief(root, "art")
        assert got["mission"]
        assert got["write_lanes"] == seats.DEFAULT_SEATS["art"]["write_globs"]
        assert got["bible"]["pillars"][0]["title"] == "Tension over spectacle"
        assert [c["name"] for c in got["canon"]] == ["The Ashen Order"]  # draft excluded
        assert got["held_locks"] == ["game/assets/shard.blend"]
        assert got["others_locks"] == [{"path": "game/assets/theme.ogg", "seat": "audio"}]
        assert "Cut line moved" in got["notes"][0]["body"]

    def test_brief_carries_only_promoted_feedback_for_this_seat(self, root):
        with db.tx(root) as conn:
            conn.execute("INSERT INTO playtest_session (id, name, slug, status) "
                         "VALUES (1, 'R', 'r', 'ready')")
            conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat, status) "
                "VALUES (1, 5.0, 'fix', 'jump is floaty', 'gameplay', 'promoted')")
            conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat, status) "
                "VALUES (1, 9.0, 'fix', 'not yet promoted', 'gameplay', 'new')")
            conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat, status) "
                "VALUES (1, 12.0, 'like', 'music rocks', 'audio', 'promoted')")

        got = seats.brief(root, "gameplay")
        texts = [f["text"] for f in got["promoted_feedback"]]
        assert texts == ["jump is floaty"]  # not the unpromoted one, not audio's

    def test_brief_for_unknown_seat_raises(self, root):
        with pytest.raises(ValueError, match="unknown or disabled"):
            seats.brief(root, "wizard")


class TestBlackboard:
    def test_post_and_read(self, root):
        seats.post_note(root, "art", "shard.glb re-exported, 106 tris", topic="shard")
        seats.post_note(root, "qa", "regression suite green", topic="build")

        by_topic = seats.read_notes(root, topic="shard")
        assert len(by_topic) == 1
        assert by_topic[0]["role"] == "art"

        by_role = seats.read_notes(root, role="qa")
        assert by_role[0]["topic"] == "build"

    def test_empty_note_rejected(self, root):
        with pytest.raises(ValueError, match="empty"):
            seats.post_note(root, "art", "   ")

    def test_unknown_role_cannot_post(self, root):
        with pytest.raises(ValueError, match="unknown role"):
            seats.post_note(root, "ghost", "boo")
