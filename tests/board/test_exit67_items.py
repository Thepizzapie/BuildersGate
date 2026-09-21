"""ITEMS 22, 24, 25, 26, 32 — EXIT 67 board-defect fixes, each with the
failure it replays and the behaviour that now holds.
"""
from __future__ import annotations

import pytest

from bgate_core.board import queue
from bgate_core.store import assets, db
from bgate_ui.agents import autodeploy, dispatch


# --------------------------------------------------------------------------
# ITEM 22 — leases die with the process.
# --------------------------------------------------------------------------
class TestItem22LeaseRelease:
    def test_completing_an_item_releases_its_path_leases(self, root):
        item = queue.add(root, "tech", "write a file", brief="edit design/brief.md")
        queue.set_status(root, item["id"], "dispatched")
        owner = f"item-{item['id']}"
        assets.acquire_path_lease(root, "design/brief.md", "director", owner)
        assert assets.path_lease_for(root, "design/brief.md") is not None

        queue.complete(root, item["id"], result="done", failed=False)

        assert assets.path_lease_for(root, "design/brief.md") is None

    def test_failing_an_item_releases_its_path_leases_too(self, root):
        item = queue.add(root, "tech", "write a file", brief="edit x.gd")
        queue.set_status(root, item["id"], "dispatched")
        owner = f"item-{item['id']}"
        assets.acquire_path_lease(root, "x.gd", "tech", owner)

        queue.set_status(root, item["id"], "failed", result="crashed")

        assert assets.path_lease_for(root, "x.gd") is None

    def test_stale_lease_is_swept_rather_than_blocking_dispatch(self, root, monkeypatch):
        """If a lease outlives its item anyway (e.g. a pre-fix row), the
        advisory check treats it as stale instead of reporting a collision."""
        holder = queue.add(root, "tech", "old holder", brief="x")
        queue.set_status(root, holder["id"], "dispatched")
        assets.acquire_path_lease(root, "scenes/graybox_house.tscn", "gameplay",
                                  f"item-{holder['id']}")
        # The holder finished through a path that did NOT release the lease
        # (simulating a pre-fix row / a lease this fix's own release missed).
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET status = 'done' WHERE id = ?",
                        (holder["id"],))

        asker = {"id": 999, "title": "", "brief": "touch scenes/graybox_house.tscn"}
        note = autodeploy._leased_path_in_brief(root, asker)

        assert note == ""
        assert assets.path_lease_for(root, "scenes/graybox_house.tscn") is None


# --------------------------------------------------------------------------
# ITEM 23 — a lease mentioned in brief TEXT no longer gates dispatch.
# --------------------------------------------------------------------------
class TestItem23AdvisoryOnly:
    def test_a_brief_naming_a_leased_path_is_not_a_floor_or_item_refusal(
        self, root, monkeypatch
    ):
        holder = queue.add(root, "director", "writing the brief", brief="x")
        queue.set_status(root, holder["id"], "dispatched")
        assets.acquire_path_lease(root, "design/brief.md", "director",
                                  f"item-{holder['id']}")
        item = queue.add(root, "tech", "do a thing",
                         brief="see design/brief.md for context", priority=50)

        monkeypatch.setattr(dispatch, "find_claude", lambda: "fake-claude")
        monkeypatch.setattr(dispatch, "dispatch",
                            lambda root_, item_id, **kw: {"ok": True})
        autodeploy.reset(root)
        result = autodeploy.tick(root, force=True)

        assert item["id"] in result["dispatched"], (
            "a brief merely NAMING a leased path must not stop dispatch"
        )


# --------------------------------------------------------------------------
# ITEM 24 — per-seat concurrency cap.
# --------------------------------------------------------------------------
class TestItem24SeatCap:
    def test_a_second_art_item_is_refused_once_the_seat_cap_is_hit(self, root):
        assert dispatch._seat_cap(root, "art") == 1  # the shipped default

        class _FakeProc:
            def poll(self):
                return None

        with dispatch._lock:
            dispatch._live[12345] = {"proc": _FakeProc(), "seat": "art"}
        try:
            item = queue.add(root, "art", "second art item", brief="x")
            result = dispatch._spawn(root, item["id"])
            assert result["ok"] is False
            assert result.get("code") == "seat_cap"
        finally:
            with dispatch._lock:
                dispatch._live.pop(12345, None)

    def test_seat_cap_is_not_a_floor_code(self):
        assert "seat_cap" not in autodeploy.FLOOR_CODES


# --------------------------------------------------------------------------
# ITEM 25 — dispatch order follows priority among READY items.
# --------------------------------------------------------------------------
class TestItem25PriorityOrder:
    def test_the_highest_priority_ready_item_dispatches_first(self, root, monkeypatch):
        low = queue.add(root, "tech", "low", brief="x", priority=84)
        mid = queue.add(root, "tech", "mid", brief="x", priority=87)
        high = queue.add(root, "art", "high", brief="x", priority=94)

        sent_order = []
        monkeypatch.setattr(dispatch, "find_claude", lambda: "fake-claude")

        def fake_dispatch(root_, item_id, **kw):
            sent_order.append(item_id)
            return {"ok": False, "code": "concurrency_limit", "error": "cap"}

        monkeypatch.setattr(dispatch, "dispatch", fake_dispatch)
        autodeploy.reset(root)
        autodeploy.tick(root, force=True)

        assert sent_order[0] == high["id"], (
            f"the 94-priority item must be attempted first, got order {sent_order}"
        )


# --------------------------------------------------------------------------
# ITEM 26 — a 'parked' status distinct from cancelled.
# --------------------------------------------------------------------------
class TestItem26Parked:
    def test_park_removes_the_item_from_ready_and_stalled(self, root):
        item = queue.add(root, "tech", "parkable", brief="x", priority=50)
        assert item["id"] in {r["id"] for r in queue.ready(root)}

        queue.park(root, item["id"], "waiting on art direction")

        assert queue.get(root, item["id"])["status"] == "parked"
        assert item["id"] not in {r["id"] for r in queue.ready(root)}
        assert item["id"] not in {r["id"] for r in queue.stalled(root)}

    def test_unpark_returns_it_to_queued_and_ready(self, root):
        item = queue.add(root, "tech", "parkable", brief="x")
        queue.park(root, item["id"], "on hold")

        result = queue.unpark(root, item["id"])

        assert result["status"] == "queued"
        assert item["id"] in {r["id"] for r in queue.ready(root)}

    def test_unpark_refuses_a_non_parked_item(self, root):
        item = queue.add(root, "tech", "not parked", brief="x")
        with pytest.raises(ValueError):
            queue.unpark(root, item["id"])

    def test_park_refuses_an_already_cancelled_item(self, root):
        item = queue.add(root, "tech", "gone", brief="x")
        queue.cancel(root, item["id"], "not needed")
        with pytest.raises(ValueError):
            queue.park(root, item["id"], "reason")

    def test_park_releases_the_items_leases(self, root):
        item = queue.add(root, "tech", "leased and parked", brief="x")
        queue.set_status(root, item["id"], "dispatched")
        assets.acquire_path_lease(root, "held.gd", "tech", f"item-{item['id']}")

        queue.park(root, item["id"], "paused")

        assert assets.path_lease_for(root, "held.gd") is None


# --------------------------------------------------------------------------
# ITEM 32 — auto-commit names every item the run carried.
# --------------------------------------------------------------------------
class TestItem32CommitAttribution:
    def test_commit_message_names_every_claimed_item_not_just_the_first(
        self, root, monkeypatch
    ):
        original = queue.add(root, "tech", "first", brief="x")
        queue.set_status(root, original["id"], "dispatched")
        second = queue.add(root, "tech", "second", brief="x")
        queue.set_run_fields(root, second["id"], actor=f"agent:item-{original['id']}")
        queue.set_status(root, second["id"], "dispatched")

        captured = {}

        class _FakeGit:
            def touched(self, root_, base):
                return {"available": True, "paths": ["scripts/a.gd"]}

            def commit_paths(self, root_, paths, message):
                captured["message"] = message
                return {"ok": True, "committed": paths, "commit": "abc12345"}

        class _FakeProv:
            def attribute(self, root_, item_id, seat, paths, since=""):
                return {"mine": paths, "left": []}

        monkeypatch.setattr(dispatch, "_git", _FakeGit())
        monkeypatch.setattr(dispatch, "_flag", lambda root_, key, env: True)
        monkeypatch.setitem(
            __import__("sys").modules, "bgate_core.store.provenance", _FakeProv())

        entry = {"base_commit": "deadbeef", "seat": "tech"}
        dispatch._auto_commit(root, original["id"], entry)

        msg = captured.get("message", "")
        assert f"#{original['id']}" in msg
        assert f"#{second['id']}" in msg


# --------------------------------------------------------------------------
# ITEM 28 — usage-limit detection, floor, requeue (not fail), auto-resume.
# --------------------------------------------------------------------------
class TestItem28UsageLimit:
    @pytest.mark.parametrize("text,expect", [
        ("You've hit your usage limit for this session.", True),
        ("Claude usage limit reached, resets at 3:00 PM.", True),
        ("429 rate limit exceeded, please retry", True),
        ("everything compiled and the tests are green", False),
    ])
    def test_claude_text_patterns(self, text, expect):
        hit = dispatch.detect_usage_limit(text, runner="claude")
        assert bool(hit) == expect

    def test_codex_percentage_pattern_needs_the_floor(self):
        assert dispatch.detect_usage_limit("usage: 40%", runner="codex") is None
        hit = dispatch.detect_usage_limit("usage: 93%", runner="codex")
        assert hit and hit["pct"] == 93

    def test_resume_time_is_carried_when_parseable(self):
        hit = dispatch.detect_usage_limit(
            "hit the usage limit, resets at 3:00 PM", runner="claude")
        assert hit["resumes_at"] == "3:00 PM"

    def test_note_usage_limit_floors_the_tick_and_emits_once(self, root, monkeypatch):
        autodeploy.reset(root)
        emitted = []
        monkeypatch.setattr(
            "bgate_core.store.events.emit",
            lambda root_, kind, ref="", payload=None: emitted.append((kind, payload)))

        autodeploy.note_usage_limit(root, "claude", "", "hit the usage limit")
        mem = autodeploy._mem(root)
        assert mem["floor_until"] > 0

        autodeploy.note_usage_limit(root, "claude", "", "hit the usage limit again")
        assert len(emitted) == 1, "a second hit before the floor clears must not re-emit"
        assert emitted[0][0] == "dispatch.blocked"
        assert emitted[0][1]["code"] == "usage_limit"

    def test_a_floored_tick_dispatches_nothing(self, root, monkeypatch):
        monkeypatch.setattr(dispatch, "find_claude", lambda: "fake-claude")
        autodeploy.reset(root)
        autodeploy.note_usage_limit(root, "claude", "", "hit the usage limit")

        result = autodeploy.tick(root, force=True)

        assert result["dispatched"] == []
        assert result.get("held") == "floor cooldown"

    def test_a_run_that_hit_the_limit_is_requeued_not_failed(self, root, monkeypatch):
        item = queue.add(root, "tech", "victim of the limit", brief="x")
        queue.set_status(root, item["id"], "dispatched")

        class _FakeProc:
            pid = 42

            def poll(self):
                return 1

        monkeypatch.setattr(dispatch, "_final_event",
                            lambda root_, item_id: {"text": "You've hit your usage limit"})
        monkeypatch.setattr(dispatch, "_finalize", lambda *a, **k: None)
        noted = {}
        monkeypatch.setattr(
            "bgate_ui.agents.autodeploy.note_usage_limit",
            lambda root_, runner, resumes, raw: noted.update(
                runner=runner, resumes=resumes, raw=raw))
        entry = {"proc": _FakeProc(), "log": "", "runner": "claude"}

        dispatch._reap(root, item["id"], entry, 1)

        assert queue.get(root, item["id"])["status"] == "queued"
        assert noted.get("runner") == "claude"
