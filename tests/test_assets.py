"""Asset registry — the lock discipline and the drift detector.

The scenarios that matter are the collisions: two seats after one .blend, a
stomp with no lock held, a dead agent's stale lock. Happy paths are cheap;
these are the cases the module exists for.
"""
from __future__ import annotations

import pytest

from bgate_core import assets


@pytest.fixture()
def blend(root):
    path = root / "assets" / "shard.blend"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"BLENDER-v450" + b"\x00" * 64)
    return path


class TestTracking:
    def test_track_records_hash_kind_size(self, root, blend):
        got = assets.track(root, blend)
        assert got["path"] == "assets/shard.blend"
        assert got["kind"] == "blender"
        assert len(got["hash"]) == 64
        assert got["bytes"] == blend.stat().st_size

    def test_paths_normalize_absolute_and_relative(self, root, blend):
        assets.track(root, blend)  # absolute
        same = assets.get(root, "assets/shard.blend")  # relative, forward slashes
        assert same["path"] == "assets/shard.blend"
        assert assets.get(root, r"assets\shard.blend")["path"] == "assets/shard.blend"

    def test_outside_project_root_is_rejected(self, root, tmp_path_factory):
        stranger = tmp_path_factory.mktemp("elsewhere") / "x.blend"
        stranger.write_bytes(b"x")
        with pytest.raises(ValueError, match="outside the project root"):
            assets.track(root, stranger)

    def test_missing_file_is_an_error(self, root):
        with pytest.raises(FileNotFoundError):
            assets.track(root, "assets/ghost.blend")

    def test_kind_inference(self):
        assert assets.kind_of("a.glb") == "model"
        assert assets.kind_of("a.PNG") == "texture"
        assert assets.kind_of("a.tscn") == "scene"
        assert assets.kind_of("a.xyz") == "unknown"


class TestLocking:
    def test_lock_then_conflict(self, root, blend):
        """The core collision: art holds it, tech must NOT get it."""
        assets.track(root, blend)
        assets.lock(root, blend, "art")
        with pytest.raises(RuntimeError, match="locked by seat 'art'"):
            assets.lock(root, blend, "tech")

    def test_same_seat_relock_is_idempotent(self, root, blend):
        assets.track(root, blend)
        assets.lock(root, blend, "art")
        got = assets.lock(root, blend, "art")  # refresh, not error
        assert got["lock_seat"] == "art"

    def test_lock_before_create_claims_the_path(self, root):
        """Normal flow: claim the path, THEN write the file."""
        got = assets.lock(root, "assets/new_boss.blend", "art")
        assert got["lock_seat"] == "art"
        assert got["hash"] == ""  # nothing on disk yet

    def test_release_rehashes_and_frees(self, root, blend):
        assets.track(root, blend)
        before = assets.get(root, blend)["hash"]
        assets.lock(root, blend, "art")
        blend.write_bytes(b"BLENDER-v450-EDITED" + b"\x00" * 64)

        got = assets.release(root, blend, "art")
        assert got["lock_seat"] is None
        assert got["hash"] != before  # the edit is now the recorded content

    def test_only_the_holder_releases(self, root, blend):
        assets.track(root, blend)
        assets.lock(root, blend, "art")
        with pytest.raises(RuntimeError, match="cannot release"):
            assets.release(root, blend, "tech")

    def test_release_unlocked_is_a_noop(self, root, blend):
        assets.track(root, blend)
        assert assets.release(root, blend, "art")["lock_seat"] is None

    def test_force_release_breaks_a_dead_agents_lock(self, root, blend):
        assets.track(root, blend)
        assets.lock(root, blend, "art")
        got = assets.force_release(root, blend)
        assert got["lock_seat"] is None
        # And the path is claimable again.
        assert assets.lock(root, blend, "tech")["lock_seat"] == "tech"

    def test_blank_seat_rejected(self, root, blend):
        with pytest.raises(ValueError, match="seat"):
            assets.lock(root, blend, "  ")


class TestDriftDetection:
    def test_clean_registry_verifies_ok(self, root, blend):
        assets.track(root, blend)
        got = assets.verify(root)
        assert got["ok"] is True
        assert got["clean"] == ["assets/shard.blend"]

    def test_unlocked_edit_is_named_as_drift(self, root, blend):
        """The silent clobber — the exact failure this module exists to expose."""
        assets.track(root, blend)
        blend.write_bytes(b"STOMPED BY SOMEONE WITHOUT A LOCK")

        got = assets.verify(root)
        assert got["ok"] is False
        assert got["modified"][0]["path"] == "assets/shard.blend"
        assert "no lock held" in got["modified"][0]["detail"]

    def test_locked_edit_is_expected_not_drift(self, root, blend):
        assets.track(root, blend)
        assets.lock(root, blend, "art")
        blend.write_bytes(b"legitimate in-progress edit")

        got = assets.verify(root)
        assert got["ok"] is True
        assert got["locked"][0]["seat"] == "art"
        assert got["modified"] == []

    def test_deleted_asset_is_missing(self, root, blend):
        assets.track(root, blend)
        blend.unlink()
        got = assets.verify(root)
        assert got["ok"] is False
        assert got["missing"] == ["assets/shard.blend"]

    def test_full_lifecycle_ends_clean(self, root, blend):
        """track -> lock -> edit -> release -> verify: the intended rhythm."""
        assets.track(root, blend)
        assets.lock(root, blend, "art")
        blend.write_bytes(b"the new shard, properly locked")
        assets.release(root, blend, "art")

        got = assets.verify(root)
        assert got["ok"] is True
        assert got["clean"] == ["assets/shard.blend"]
        assert got["locked"] == []
