"""Asset registry — the lock discipline and the drift detector.

The scenarios that matter are the collisions: two seats after one .blend, a
stomp with no lock held, a dead agent's stale lock. Happy paths are cheap;
these are the cases the module exists for.
"""
from __future__ import annotations

import pytest

from bgate_core.store import assets


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

    def test_relative_parent_escape_is_rejected(self, root):
        with pytest.raises(ValueError, match="outside the project root"):
            assets.lock(root, "../outside.blend", "art")

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

    def test_same_seat_different_execution_conflicts(self, root, blend):
        assets.track(root, blend)
        assets.lock(root, blend, "art", owner="item-1")
        with pytest.raises(RuntimeError, match="locked by seat 'art'"):
            assets.lock(root, blend, "art", owner="item-2")

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

    def test_unhashed_unlocked_asset_is_not_healthy(self, root):
        path = root / "assets" / "candidate.png"
        assets.lock(root, path, "art")  # lock-before-create: DB-only, no dirs
        path.parent.mkdir()  # the writer owns directory creation, not the lock
        path.write_bytes(b"candidate")
        assets.force_release(root, path)

        got = assets.verify(root)
        assert got["ok"] is False
        assert got["untracked_hash"] == ["assets/candidate.png"]
        assert got["counts"]["pending"] == 1


class TestNormalizePathContainment:
    """normalize_path is the containment check the whole product relies on —
    every registry key, every artifact path, and every caller-supplied frame in
    the cutscene pipeline goes through it. These are the shapes an attacker
    actually tries, and one of them used to get through.
    """

    @pytest.mark.parametrize("attack", [
        "../outside/secret.png",
        "../../../../../../../etc/passwd",
        "/etc/passwd",
        "art/../../outside/secret.png",
        "art/../../outside/./secret.png",
        "..//outside//secret.png",
        # WINDOWS SEPARATORS ON POSIX, and this is the one that got through. A
        # backslash is an ordinary filename character here, so this resolved to
        # one harmless non-existent child, passed containment, and was then
        # rewritten to "../../outside/secret.png" on the way out — which every
        # caller joins to the root and follows out of the project. In the
        # cutscene pipeline the escaped path is UPLOADED to a third party, so it
        # leaves the machine rather than merely being read.
        "..\\..\\outside\\secret.png",
        "art\\..\\..\\outside\\secret.png",
    ])
    def test_a_traversal_is_refused(self, root, attack):
        with pytest.raises(ValueError, match="outside the project"):
            assets.normalize_path(root, attack)

    def test_a_symlink_out_of_the_project_is_refused(self, root,
                                                     tmp_path_factory):
        """The classic bypass of a naive `..` check: no traversal in the string
        at all. resolve() has to run BEFORE the comparison, and it does.

        tmp_path_factory, NOT tmp_path: the `root` fixture IS tmp_path, so a
        file made there is INSIDE the project and the symlink would correctly
        be allowed — a test that passes while proving nothing."""
        elsewhere = tmp_path_factory.mktemp("elsewhere")
        outside = elsewhere / "outside.png"
        outside.write_bytes(b"\x89PNG")
        (root / "art").mkdir(parents=True, exist_ok=True)
        link = root / "art" / "innocent.png"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("this platform/user cannot create symlinks")
        with pytest.raises(ValueError, match="outside the project"):
            assets.normalize_path(root, "art/innocent.png")

    def test_ordinary_paths_are_unchanged(self, root):
        """Containment must not cost the normal case."""
        assert assets.normalize_path(root, "art/hero.png") == "art/hero.png"
        assert assets.normalize_path(root, "game/../art/hero.png") == "art/hero.png"
        assert assets.normalize_path(root, root / "art" / "hero.png") == "art/hero.png"

    def test_windows_separators_still_normalise_when_they_stay_inside(self, root):
        """The fix normalises separators rather than banning them — a Windows
        client posting `art\\hero.png` still gets the right registry key."""
        assert assets.normalize_path(root, "art\\hero.png") == "art/hero.png"
