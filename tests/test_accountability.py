"""The gates that have to actually gate.

Four audit findings converge here: an agent could approve its own art, two
agents in one seat both passed the lock check, a lease was written but never
compared to the clock, and a re-pinned reference silently rewrote the history of
every artifact drawn against it. Each test below is one of those failures.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bgate_core import activity, artifacts, assets, db, refs, seats


@pytest.fixture()
def agent(monkeypatch):
    """Run the block as a dispatched agent rather than the human at the machine."""
    monkeypatch.setenv("BGATE_ACTOR", "agent:item-7")
    return "agent:item-7"


@pytest.fixture()
def candidate(root):
    path = root / "game" / "assets" / "hero.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"hero" * 16)
    return artifacts.register(root, "hero", path, producer="art")


class TestHumanMandatoryApproval:
    def test_agent_cannot_approve(self, root, candidate, agent):
        with pytest.raises(PermissionError, match="agent:item-7"):
            artifacts.review(root, candidate["id"], "approved")
        assert artifacts.get(root, candidate["id"])["status"] == "candidate"

    def test_agent_cannot_integrate_either(self, root, candidate, agent):
        """The bypass one hop over: 'integrated' also puts art in the build."""
        with pytest.raises(PermissionError):
            artifacts.review(root, candidate["id"], "integrated")

    def test_refusal_names_the_way_out(self, root, candidate, agent):
        with pytest.raises(PermissionError, match="qa_verdict"):
            artifacts.review(root, candidate["id"], "approved")

    def test_agent_may_reject(self, root, candidate, agent):
        got = artifacts.review(root, candidate["id"], "rejected", "drifts from ref")
        assert got["status"] == "rejected"
        assert got["reviewed_by"] == "agent:item-7"

    def test_qa_pass_records_judgement_without_promoting(self, root, candidate, agent):
        got = artifacts.qa_verdict(root, candidate["id"], passed=True, score=88,
                                   note="matches the anchor")
        assert got["metadata"]["qa_review"]["verdict"] == "pass"
        assert got["metadata"]["qa_review"]["actor"] == "agent:item-7"
        assert got["status"] == "candidate"  # still awaiting a human

    def test_qa_fail_is_a_real_rejection(self, root, candidate, agent):
        got = artifacts.qa_verdict(root, candidate["id"], passed=False,
                                   note="white halo")
        assert got["status"] == "rejected"

    def test_human_approval_is_allowed_and_stamped(self, root, candidate):
        got = artifacts.review(root, candidate["id"], "approved", "ship it")
        assert got["status"] == "approved"
        assert got["reviewed_by"] and not got["reviewed_by"].startswith("agent:")

    def test_explicit_actor_overrides_the_environment(self, root, candidate, agent):
        got = artifacts.review(root, candidate["id"], "approved",
                               actor="director@studio")
        assert got["reviewed_by"] == "director@studio"

    def test_the_ledger_names_who_acted(self, root, candidate, agent):
        artifacts.review(root, candidate["id"], "rejected")
        entry = next(e for e in activity.recent(root)
                     if e["kind"] == "artifact_review")
        assert entry["actor"] == "agent:item-7"

    def test_legacy_activity_rows_still_list(self, root):
        """Rows written before 0011 have no actor; the ticker must not care."""
        with db.tx(root) as conn:
            conn.execute("INSERT INTO activity (seat, kind, summary, ref) "
                         "VALUES ('art', 'legacy', 'from before', '')")
        assert [e for e in activity.recent(root) if e["kind"] == "legacy"][0][
            "actor"] == ""


class TestOwnerScopedLocks:
    def test_same_seat_second_execution_is_blocked(self, root):
        """The hole: can_write compared seats, so both art agents passed."""
        assets.lock(root, "game/assets/shard.blend", "art", owner="item-1")
        first = seats.can_write(root, "art", "game/assets/shard.blend",
                                owner="item-1")
        second = seats.can_write(root, "art", "game/assets/shard.blend",
                                 owner="item-2")
        assert first["allowed"] is True
        assert second["allowed"] is False
        assert "item-1" in second["reason"]

    def test_an_anonymous_caller_does_not_inherit_the_lock(self, root):
        assets.lock(root, "game/assets/shard.blend", "art", owner="item-1")
        assert seats.can_write(root, "art", "game/assets/shard.blend")["allowed"] is False

    def test_unowned_lock_keeps_the_old_seat_behaviour(self, root):
        """Existing projects hold locks with no owner recorded — still usable."""
        assets.lock(root, "game/assets/shard.blend", "art")
        assert seats.can_write(root, "art", "game/assets/shard.blend")["allowed"] is True


class TestLeaseExpiry:
    def _expire(self, root, path):
        past = (datetime.now(timezone.utc) - timedelta(seconds=60)
                ).strftime("%Y-%m-%d %H:%M:%S")
        with db.tx(root) as conn:
            conn.execute("UPDATE asset SET lease_expires_at = ? WHERE path = ?",
                         (past, path))

    def test_expired_lock_is_released_and_reclaimable(self, root):
        assets.lock(root, "game/assets/shard.blend", "art", owner="item-1")
        self._expire(root, "game/assets/shard.blend")

        assert "game/assets/shard.blend" in assets.reap_expired(root)
        assert assets.get(root, "game/assets/shard.blend")["lock_seat"] is None
        # And the next execution gets it without a human breaking the lock.
        got = assets.lock(root, "game/assets/shard.blend", "art", owner="item-2")
        assert got["lock_owner"] == "item-2"

    def test_a_lock_with_no_lease_is_left_alone(self, root):
        assets.lock(root, "game/assets/shard.blend", "art")
        with db.tx(root) as conn:
            conn.execute("UPDATE asset SET lease_expires_at = NULL "
                         "WHERE path = 'game/assets/shard.blend'")
        assert assets.reap_expired(root) == []
        assert assets.get(root, "game/assets/shard.blend")["lock_seat"] == "art"

    def test_lease_length_follows_the_operation(self):
        assert assets.lease_seconds("bake") > assets.lease_seconds("import")
        assert assets.lease_seconds("anything-else") == assets.DEFAULT_LEASE_S
        assert assets.lease_seconds("bake", 45) == 45  # explicit still wins

    def test_expired_path_lease_stops_blocking(self, root):
        assets.acquire_path_lease(root, "game/scripts/player.gd", "gameplay",
                                  "item-1", lease_s=30)
        with db.tx(root) as conn:
            conn.execute("UPDATE path_lease SET expires_at = ?",
                         ((datetime.now(timezone.utc) - timedelta(seconds=1)
                           ).strftime("%Y-%m-%d %H:%M:%S"),))
        assert assets.path_lease_for(root, "game/scripts/player.gd") is None
        assets.acquire_path_lease(root, "game/scripts/player.gd", "gameplay",
                                  "item-2")


class TestPathLeases:
    def test_second_execution_is_blocked_by_name(self, root):
        assets.acquire_path_lease(root, "game/scripts/player.gd", "gameplay",
                                  "item-1")
        verdict = seats.can_write(root, "gameplay", "game/scripts/player.gd",
                                  owner="item-2")
        assert verdict["allowed"] is False
        assert "item-1" in verdict["reason"]

    def test_the_holder_keeps_writing(self, root):
        assets.acquire_path_lease(root, "game/scripts/player.gd", "gameplay",
                                  "item-1")
        assert seats.can_write(root, "gameplay", "game/scripts/player.gd",
                               owner="item-1")["allowed"] is True

    def test_release_frees_everything_the_run_held(self, root):
        assets.acquire_path_lease(root, "game/scripts/a.gd", "gameplay", "item-1")
        assets.acquire_path_lease(root, "game/scripts/b.gd", "gameplay", "item-1")
        assert assets.release_path_leases(root, "item-1") == 2
        assert assets.list_path_leases(root) == []

    def test_the_hook_takes_and_enforces_the_lease(self, root, monkeypatch):
        from bgate_cli import hook

        payload = {"tool_name": "Write",
                   "tool_input": {"file_path": str(root / "game/scripts/player.gd")},
                   "cwd": str(root)}
        code, _ = hook.decide(payload, "gameplay", "item-1")
        assert code == hook.ALLOW
        assert assets.path_lease_for(root, "game/scripts/player.gd")["owner"] == "item-1"

        code, msg = hook.decide(payload, "gameplay", "item-2")
        assert code == hook.BLOCK
        assert "item-1" in msg  # the block names who is in the file

    def test_the_hook_without_an_owner_stays_out_of_the_way(self, root):
        from bgate_cli import hook

        payload = {"tool_name": "Write",
                   "tool_input": {"file_path": str(root / "game/scripts/player.gd")},
                   "cwd": str(root)}
        assert hook.decide(payload, "gameplay")[0] == hook.ALLOW
        assert assets.list_path_leases(root) == []


class TestWaiters:
    def test_a_blocked_acquire_registers_and_reports_the_waiter(self, root):
        import threading

        assets.lock(root, "game/assets/shard.blend", "art", owner="item-1")
        seen: list[list[dict]] = []

        def contend():
            try:
                assets.lock(root, "game/assets/shard.blend", "art",
                            owner="item-2", wait_s=2.0)
            except RuntimeError:
                pass

        thread = threading.Thread(target=contend)
        thread.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            found = assets.waiters(root, "game/assets/shard.blend")
            if found:
                seen.append(found)
                break
            time.sleep(0.05)
        assets.release(root, "game/assets/shard.blend", "art", owner="item-1")
        thread.join(timeout=5)

        assert seen and seen[0][0]["owner"] == "item-2"
        assert assets.get(root, "game/assets/shard.blend")["lock_owner"] == "item-2"
        assert assets.waiters(root) == []  # the waiter cleans up after itself

    def test_a_timed_out_wait_names_the_holder(self, root):
        assets.lock(root, "game/assets/shard.blend", "art", owner="item-1")
        with pytest.raises(RuntimeError, match="item-1"):
            assets.lock(root, "game/assets/shard.blend", "art", owner="item-2",
                        wait_s=0.3)
        assert assets.waiters(root) == []

    def test_immediate_failure_is_still_the_default(self, root):
        assets.lock(root, "game/assets/shard.blend", "art", owner="item-1")
        started = time.monotonic()
        with pytest.raises(RuntimeError):
            assets.lock(root, "game/assets/shard.blend", "art", owner="item-2")
        assert time.monotonic() - started < 1.0


class TestVersionedRefs:
    @pytest.fixture()
    def anchor(self, tmp_path):
        src = tmp_path / "tommy.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"v1" * 20)
        return src

    def test_repin_creates_r2_and_keeps_r1_resolvable(self, root, anchor, tmp_path):
        first = refs.pin(root, "tommy", str(anchor), kind="character")
        assert first["revision"] == 1

        better = tmp_path / "tommy2.png"
        better.write_bytes(b"\x89PNG\r\n\x1a\n" + b"v2" * 40)
        second = refs.pin(root, "tommy", str(better), kind="character")

        assert second["revision"] == 2
        assert second["path"] != first["path"]
        assert Path(first["path"]).is_file()  # r1 was NOT overwritten
        assert Path(first["path"]).read_bytes().endswith(b"v1" * 20)
        assert refs.resolve(root, "tommy") == second["path"]
        assert refs.resolve(root, "tommy@r1") == first["path"]
        assert refs.resolve(root, "tommy@1") == first["path"]
        assert [h["revision"] for h in refs.history(root, "tommy")] == [2, 1]
        assert len(refs.list_refs(root)) == 1

    def test_hash_is_recorded_per_revision(self, root, anchor, tmp_path):
        refs.pin(root, "tommy", str(anchor))
        first = refs.get(root, "tommy")["hash"]
        better = tmp_path / "t2.png"
        better.write_bytes(b"\x89PNG\r\n\x1a\n" + b"v2" * 40)
        refs.pin(root, "tommy", str(better))
        assert len(first) == 64
        assert refs.get(root, "tommy")["hash"] != first

    def test_unversioned_pins_keep_resolving(self, root, anchor):
        """A project pinned before 0011: a bare path, revision 1, no hash."""
        legacy = Path(root) / ".bgate" / "refs" / "legacy.png"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"\x89PNG\r\n\x1a\n" + b"old" * 10)
        with db.tx(root) as conn:
            conn.execute("INSERT INTO ref_pin (name, path, kind, note) "
                         "VALUES ('legacy', ?, 'style', '')", (str(legacy),))

        assert refs.resolve(root, "legacy") == str(legacy)
        assert [h["revision"] for h in refs.history(root, "legacy")] == [1]
        # Upgrading it starts a real history that still includes the old file.
        refs.pin(root, "legacy", str(anchor))
        assert [h["revision"] for h in refs.history(root, "legacy")] == [2, 1]
        assert refs.resolve(root, "legacy@1") == str(legacy)

    def test_artifacts_record_the_revision_they_were_drawn_against(
            self, root, anchor, tmp_path):
        refs.pin(root, "tommy", str(anchor), kind="character")
        out = root / "game" / "assets" / "tommy_idle.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\n" + b"idle" * 8)
        art = artifacts.register(root, "tommy-idle", out, producer="art",
                                 refs=["tommy"])

        pins = art["metadata"]["ref_pins"]
        assert pins[0]["name"] == "tommy" and pins[0]["revision"] == 1
        assert artifacts.ref_drift(root, art) == []

        better = tmp_path / "t2.png"
        better.write_bytes(b"\x89PNG\r\n\x1a\n" + b"v2" * 40)
        refs.pin(root, "tommy", str(better), kind="character")

        drift = artifacts.ref_drift(root, artifacts.get(root, art["id"]))
        assert drift and drift[0]["current_revision"] == 2
        assert "r2" in drift[0]["detail"]

    def test_a_raw_path_ref_is_not_a_pin(self, root, anchor):
        out = root / "game" / "assets" / "thing.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 8)
        art = artifacts.register(root, "thing", out, refs=[str(anchor)])
        assert "ref_pins" not in art["metadata"]
