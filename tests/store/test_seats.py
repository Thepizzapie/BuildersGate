"""Seats — the lane oracle, the lock integration, and the brief.

The check with teeth is can_write: it must fail closed for unknown seats, keep
seats in their lanes, and refuse a locked binary even when it's in-lane. That
last case is the whole reason locks and lanes are two separate gates.
"""
from __future__ import annotations

import pytest

from bgate_core.store import assets
from bgate_core.design import bible, lore
from bgate_core.board import seats
from bgate_core.store import db


class TestRoles:
    def test_every_default_seat_is_active(self, root):
        got = seats.roles_for(root)
        assert set(got) == set(seats.ROLES)
        # Counted against the table rather than a literal. The literal was 7 and
        # went stale the day an eighth seat landed, which is the failure mode a
        # test named for a number always has.
        assert len(got) == len(seats.DEFAULT_SEATS)

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

    def test_brief_carries_the_identity_frame(self, root):
        """Spawned workers were re-litigating their own identity; the brief must
        resolve it up front."""
        frame = seats.brief(root, "art")["your_role"]
        assert "SPAWNED SEAT WORKER" in frame
        assert "not an injection" in frame.lower()
        # The real boundary is preserved, not blanket-waived.
        assert "through TOOLS" in frame and ".env" in frame


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


def test_every_seat_has_a_persona_and_an_override_survives_other_edits(tmp_path):
    """The floor reads a seat's LOOK from the seat table, not from its name.

    Three properties, and the third is the one that would break quietly:
      - every default seat ships a persona, so no reader handles its absence
      - an override merges key by key, so changing the floor keeps the cast
      - a configure() call that says nothing about persona leaves it alone
    """
    root = tmp_path / "proj"
    root.mkdir()
    db.connect(root)

    table = seats.roles_for(root)
    assert all("persona" in cfg for cfg in table.values()), (
        "a seat with no persona would make the floor fall back to defaults")
    assert table["tech"]["persona"]["surface"] == "concrete"

    seats.configure(root, "tech", persona={"surface": "wood"})
    persona = seats.roles_for(root)["tech"]["persona"]
    assert persona["surface"] == "wood"
    assert persona["cast"] == "tech", "an override blanked the keys it did not set"

    # The trap: an unrelated edit must not wipe the project's floor.
    seats.configure(root, "tech", mission="keep the lights on")
    after = seats.roles_for(root)["tech"]
    assert after["persona"]["surface"] == "wood", (
        "configure() wiped the persona when it was not asked to touch it")
    assert after["mission"] == "keep the lights on"


def test_a_persona_override_does_not_leak_between_projects(tmp_path):
    """It is a column on this project's seat, not a global setting."""
    one, two = tmp_path / "one", tmp_path / "two"
    for p in (one, two):
        p.mkdir()
        db.connect(p)
    seats.configure(one, "art", persona={"vibe": "murals"})
    assert seats.roles_for(one)["art"]["persona"]["vibe"] == "murals"
    assert seats.roles_for(two)["art"]["persona"]["vibe"] == "paint"


def test_a_seat_personality_reaches_the_dispatch_prompt(tmp_path):
    """`style` is the one persona field that changes what an agent does.

    Everything else on a persona is how the studio view looks. This is appended
    to the dispatch prompt, so it has to actually arrive - and it has to arrive
    with the sentence that stops it being read as a new mission, because a text
    box that quietly outranks a seat's brief is a way to talk an agent out of
    its lanes with something that looks like a bit of fun.
    """
    from bgate_ui.agents import dispatch

    root = tmp_path / "proj"
    root.mkdir()
    db.connect(root)
    item = {"id": 1, "seat": "art", "title": "t", "brief": "b", "source": "x"}

    plain = dispatch._prompt_for(str(root), item)
    assert "CARRIES ITSELF" not in plain, (
        "a project that set no personality got one anyway")

    seats.configure(root, "art", persona={"style": "Blunt. Hates meetings."})
    withit = dispatch._prompt_for(str(root), item)
    assert "Blunt. Hates meetings." in withit
    assert "changes your tone, not your job" in withit, (
        "the guardrail that keeps a personality from reading as a mission is gone")
    assert withit.rstrip().endswith("carry on as normal."), (
        "the personality must come LAST, after the job, the lanes and the gates")

    # And it is surfaced on the brief too, which is the other channel a seat
    # reads its identity from.
    assert seats.brief(root, "art")["personality"] == "Blunt. Hates meetings."

    seats.configure(root, "art", persona={"style": None})
    assert "CARRIES ITSELF" not in dispatch._prompt_for(str(root), item)
