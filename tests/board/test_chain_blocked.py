"""A chain behind a dead link says so, instead of leaving the board silent.

MEASURED (EXIT 67 r2, 2026-09-22): a six-link chain's head FAILED. The
director's escalation filed a fresh item that did the head's job and passed
QA; the five links behind the head stayed queued, nothing was ready, and the
board read as stuck for an hour - because it was, quietly. autodeploy ticked
in silence, board_digest said "the dashboard is down or autopilot is off"
(both wrong), and the escalation brief never mentioned what the failure was
holding. These tests are the three places that now speak.
"""
from __future__ import annotations

from bgate_core.board import queue
from bgate_core.design import gameplan
from bgate_core.store import events
from bgate_ui.agents import autodeploy, dispatch, followup


def _chain(root):
    return queue.add_chain(root, [
        {"seat": "gameplay", "title": "prove the climb", "priority": 9},
        {"seat": "gameplay", "title": "prove the collapse", "priority": 9},
        {"seat": "tech", "title": "export it", "priority": 9},
    ])


class TestTheQueueNamesDeadLinks:
    def test_a_failed_head_holds_every_link_behind_it(self, root):
        head, second, third = _chain(root)
        queue.set_status(root, head["id"], "failed", result="no path")
        got = queue.blocked_chains(root)
        assert len(got) == 1
        assert got[0]["blocker"]["id"] == head["id"]
        assert got[0]["blocker"]["status"] == "failed"
        # Only the DIRECT dependant is queued behind it; the third waits on
        # the second, which is merely queued, not dead.
        assert [w["id"] for w in got[0]["waiting"]] == [second["id"]]

    def test_a_parked_link_releases_its_chain_at_once(self, root):
        # park() releases dependents itself (release_dependents), so a parked
        # head is never a dead link the board has to discover later.
        head, second, _ = _chain(root)
        queue.park(root, head["id"], "not now")
        assert queue.blocked_chains(root) == []
        assert second["id"] in {r["id"] for r in queue.ready(root)}

    def test_a_running_predecessor_is_not_dead(self, root):
        head, _, _ = _chain(root)
        queue.set_status(root, head["id"], "dispatched")
        assert queue.blocked_chains(root) == []

    def test_closing_the_head_releases_the_chain(self, root):
        head, second, _ = _chain(root)
        queue.set_status(root, head["id"], "failed", result="no path")
        assert second["id"] not in {r["id"] for r in queue.ready(root)}
        queue.set_status(root, head["id"], "done", result="superseded by #99")
        assert queue.blocked_chains(root) == []
        assert second["id"] in {r["id"] for r in queue.ready(root)}

    def test_the_description_names_the_three_ways_out(self, root):
        head, second, _ = _chain(root)
        queue.set_status(root, head["id"], "failed", result="no path")
        text = queue.describe_blocked_chains(queue.blocked_chains(root))
        assert f"#{second['id']}" in text and f"#{head['id']}" in text
        assert "FAILED" in text
        for way in ("queue_reopen", "superseded", "queue_cut_dependency"):
            assert way in text


class TestTheBoardSpeaks:
    def test_autopilot_announces_a_dead_link_once(self, root, monkeypatch):
        monkeypatch.setattr(dispatch, "find_claude", lambda: "claude")
        head, _, _ = _chain(root)
        queue.set_status(root, head["id"], "failed", result="no path")
        autodeploy.reset(root)
        autodeploy.tick(str(root), force=True)
        autodeploy.tick(str(root), force=True)
        got = [e for e in events.since(root, 0)["events"]
               if e.get("kind") == "dispatch.blocked"
               and (e.get("payload") or {}).get("code") == "chain_blocked"]
        assert len(got) == 1, got
        payload = got[0]["payload"]
        assert payload["blockers"][0]["id"] == head["id"]
        assert payload["waiting"] == 1
        assert "queue_reopen" in payload["reason"]

    def test_the_digest_says_why_instead_of_blaming_the_dashboard(self, root):
        head, _, _ = _chain(root)
        queue.set_status(root, head["id"], "failed", result="no path")
        blocked = gameplan.digest(root)["blocked"]
        assert f"#{head['id']}" in blocked and "FAILED" in blocked
        assert "dashboard is down" not in blocked

    def test_the_escalation_brief_names_what_the_failure_holds(self, root):
        head, second, _ = _chain(root)
        queue.set_status(root, head["id"], "failed", result="no path")
        out = followup._do_fail_escalate(
            str(root), {"item": head["id"], "reason": "no path through the climb"})
        assert "why" not in out or "escalated" not in str(out.get("why", ""))
        rows = [r for r in queue.list_items(root, status="queued")
                if r["seat"] == "director"]
        assert rows, "no escalation was filed"
        brief = rows[-1]["brief"]
        assert "THIS FAILURE BLOCKS THE BOARD" in brief
        assert f"#{second['id']}" in brief and "RELEASE THEM" in brief


class TestTheDirectorReleasesIt:
    def test_autopilot_files_one_unblock_item_per_dead_link(self, root, monkeypatch):
        monkeypatch.setattr(dispatch, "find_claude", lambda: "claude")
        monkeypatch.setattr(dispatch, "dispatch",
                            lambda r, i, **k: {"ok": False, "code": "concurrency_limit",
                                               "error": "cap"})
        head, second, _ = _chain(root)
        queue.set_status(root, head["id"], "failed", result="no path")
        autodeploy.reset(root)
        autodeploy.tick(str(root), force=True)
        autodeploy.tick(str(root), force=True)
        filed = [r for r in queue.list_items(root)
                 if r["source"] == queue.UNBLOCK_SOURCE]
        assert len(filed) == 1, [r["title"] for r in filed]
        item = filed[0]
        assert item["seat"] == "director"
        assert item["source_ref"] == str(head["id"])
        assert f"#{second['id']}" in item["brief"]
        for way in ("superseded", "queue_reopen", "queue_add_chain",
                    "queue_cut_dependency", "queue_cancel", "ask_human"):
            assert way in item["brief"], way

    def test_the_unblock_item_itself_is_ready_so_the_board_is_not_idle(self, root):
        head, _, _ = _chain(root)
        queue.set_status(root, head["id"], "failed", result="no path")
        queue.file_unblock(root, queue.blocked_chains(root)[0])
        assert any(r["source"] == queue.UNBLOCK_SOURCE for r in queue.ready(root))

    def test_a_released_link_files_no_second_item(self, root):
        head, _, _ = _chain(root)
        queue.set_status(root, head["id"], "failed", result="no path")
        first = queue.file_unblock(root, queue.blocked_chains(root)[0])
        assert first is not None
        assert queue.file_unblock(root, queue.blocked_chains(root)[0]) is None
        queue.set_status(root, first["id"], "done", result="closed as superseded")
        queue.set_status(root, head["id"], "done", result="superseded")
        assert queue.blocked_chains(root) == []

    def test_the_director_protocol_owns_the_stuck_board(self):
        from bgate_core.board import seats
        assert "THE BOARD NEVER STAYS STUCK" in seats.DIRECTOR_PROTOCOL


class TestADeadItemHoldsNothing:
    def test_cancelling_a_head_releases_the_next_link_with_a_note(self, root):
        head, second, third = _chain(root)
        queue.cancel(root, head["id"], "five deliverables wide; split elsewhere")
        assert second["id"] in {r["id"] for r in queue.ready(root)}
        brief = queue.get(root, second["id"])["brief"]
        assert "--- RELEASED ---" in brief and f"#{head['id']}" in brief
        assert "CANCELLED" in brief and "fail fast" in brief
        # The third still waits on the second, which is alive and queued.
        assert third["id"] not in {r["id"] for r in queue.ready(root)}
        assert queue.blocked_chains(root) == []

    def test_parking_releases_too(self, root):
        head, second, _ = _chain(root)
        queue.park(root, head["id"], "not tonight")
        assert second["id"] in {r["id"] for r in queue.ready(root)}

    def test_exhausting_releases_too(self, root):
        head, second, _ = _chain(root)
        queue.set_status(root, head["id"], "failed", result="no")
        queue.mark_exhausted(root, head["id"], "retry budget spent")
        assert second["id"] in {r["id"] for r in queue.ready(root)}

    def test_a_release_is_on_the_record(self, root):
        head, second, _ = _chain(root)
        queue.cancel(root, head["id"], "gone")
        got = [e for e in events.since(root, 0)["events"]
               if e.get("kind") == "queue.released"]
        assert got and got[0]["payload"]["released"] == [second["id"]]
