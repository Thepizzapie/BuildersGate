"""Work chains and the three-way approval gate.

TWO GAPS, ONE STORY. A director splitting an ask across seats could only express
"this goes after that" as a priority, and priority is a preference among things
that are all READY — so auto-deploy started the item that needed a scene in the
same tick as the item that creates it, and the second agent wrote against a file
that did not exist. And "done" meant whatever the agent said it meant: the only
review available was all-or-nothing (the QA gate, or BGATE_QA_GATE=0), with no
way for the owner to be the one who signs off.

The tests below are written from the failure end: a link must not be dispatchable
before its predecessor lands, and a gate mode must actually change where a
completed item comes to rest.
"""
from __future__ import annotations

import pytest

from bgate_core import db, gates, queue
from bgate_ui import autodeploy, dispatch as _dispatch, qa_gate


@pytest.fixture(autouse=True)
def _default_gate(monkeypatch):
    """No stray env override, and no inherited mode from another test."""
    monkeypatch.delenv("BGATE_QA_GATE", raising=False)
    monkeypatch.delenv("BGATE_ACTOR", raising=False)


def _chain(root):
    return queue.add_chain(root, [
        {"seat": "tech", "title": "bake the scene", "priority": 9},
        {"seat": "gameplay", "title": "wire the view to it", "priority": 9},
    ])


class TestChainShape:
    def test_each_link_waits_on_the_one_before_it(self, root):
        first, second = _chain(root)
        assert first["depends_on"] is None
        assert second["depends_on"] == first["id"]
        assert first["chain_id"] == second["chain_id"]
        assert (first["chain_pos"], second["chain_pos"]) == (1, 2)

    def test_a_malformed_link_lands_nothing(self, root):
        """Half a chain is worse than no chain: the surviving links look like
        ordinary work and dispatch in any order."""
        with pytest.raises(ValueError):
            queue.add_chain(root, [
                {"seat": "tech", "title": "fine"},
                {"seat": "nosuchseat", "title": "bad seat"},
            ])
        assert queue.list_items(root) == []

    def test_a_one_link_chain_is_refused_as_a_misuse(self, root):
        with pytest.raises(ValueError):
            queue.add_chain(root, [{"seat": "tech", "title": "solo"}])

    def test_chain_ids_do_not_collide(self, root):
        a = _chain(root)[0]["chain_id"]
        b = _chain(root)[0]["chain_id"]
        assert a != b

    def test_a_dependency_on_a_fiction_is_refused(self, root):
        with pytest.raises(LookupError):
            queue.add(root, "tech", "waits on nothing real", depends_on=9999)


class TestReadiness:
    def test_the_second_link_is_blocked_until_the_first_is_done(self, root):
        first, second = _chain(root)
        held = queue.blocker(root, second["id"])
        assert held and held["id"] == first["id"] and held["status"] == "queued"
        queue.set_status(root, first["id"], "done", result="baked")
        assert queue.blocker(root, second["id"]) is None

    def test_a_failed_predecessor_leaves_the_chain_parked_not_cancelled(self, root):
        """Auto-cancelling the tail would destroy filed work over one bad run.
        The chain stalls, visibly, and a human decides."""
        first, second = _chain(root)
        queue.set_status(root, first["id"], "failed", result="could not")
        assert queue.blocker(root, second["id"])["status"] == "failed"
        assert queue.get(root, second["id"])["status"] == "queued"

    def test_next_for_never_hands_a_seat_blocked_work(self, root):
        _, second = _chain(root)
        assert queue.next_for(root, "gameplay") is None
        queue.add(root, "gameplay", "unrelated, ready now")
        assert queue.next_for(root, "gameplay")["title"] == "unrelated, ready now"

    def test_autodeploy_filters_blocked_links_out_of_its_candidates(self, root):
        first, second = _chain(root)
        ids = [c["id"] for c in autodeploy._candidates(str(root))]
        assert first["id"] in ids and second["id"] not in ids
        queue.set_status(root, first["id"], "done", result="baked")
        assert second["id"] in [c["id"] for c in autodeploy._candidates(str(root))]

    def test_dispatch_refuses_a_blocked_link_and_names_what_it_waits_for(
            self, root, monkeypatch):
        """The button and 'deploy all' do not go through autodeploy's filter, so
        the refusal has to exist at the last gate too — and it has to say which
        item is in the way, or it is not actionable."""
        monkeypatch.setattr(_dispatch, "find_claude", lambda: "claude")
        first, second = _chain(root)
        got = _dispatch.dispatch(str(root), second["id"])
        assert got["ok"] is False
        assert got.get("code") == "blocked_on_dependency"
        assert got["detail"]["waiting_on"] == first["id"]
        assert f"#{first['id']}" in got["error"]

    def test_a_deleted_predecessor_unblocks_rather_than_strands(self, root):
        first, second = _chain(root)
        with db.tx(root) as conn:
            conn.execute("DELETE FROM work_item WHERE id = ?", (first["id"],))
        assert queue.blocker(root, second["id"]) is None


class TestGateModes:
    def test_the_default_is_what_shipped_before_the_setting_existed(self, root):
        assert gates.mode(root) == gates.AGENT
        assert gates.wants_qa_agent(root) and not gates.holds_for_human(root)

    def test_only_the_three_modes_are_settable(self, root):
        gates.set_mode(root, gates.BUILDERS)
        assert gates.mode(root) == gates.BUILDERS
        with pytest.raises(ValueError):
            gates.set_mode(root, "sometimes")

    def test_the_legacy_kill_switch_still_wins(self, root, monkeypatch):
        """BGATE_QA_GATE=0 predates this module and is in somebody's shell
        profile. It keeps meaning what it meant: no automatic review at all."""
        gates.set_mode(root, gates.BUILDERS)
        monkeypatch.setenv("BGATE_QA_GATE", "0")
        assert gates.mode(root) == gates.NONE
        state = gates.state(root)
        assert state["stored"] == gates.BUILDERS and state["env_override"]

    def test_the_state_never_shows_a_mode_the_env_is_overriding(self, root,
                                                                monkeypatch):
        monkeypatch.setenv("BGATE_QA_GATE", "off")
        assert gates.state(root)["mode"] == gates.NONE


class TestCompletionThroughTheGate:
    def test_no_gate_and_agent_gate_both_close_the_item(self, root):
        for mode in (gates.NONE, gates.AGENT):
            gates.set_mode(root, mode)
            item = queue.add(root, "art", f"a thing under {mode}")
            got = queue.complete(root, item["id"], result="shipped")
            assert got["status"] == "done", mode

    def test_the_builders_gate_parks_finished_work_in_review(self, root):
        gates.set_mode(root, gates.BUILDERS)
        item = queue.add(root, "art", "a sprite")
        assert queue.complete(root, item["id"], result="drew it")["status"] == "review"

    def test_a_failure_is_never_held_for_approval(self, root):
        """The gate holds work for a yes; nobody is asked to bless a crash."""
        gates.set_mode(root, gates.BUILDERS)
        item = queue.add(root, "art", "a sprite")
        got = queue.complete(root, item["id"], result="could not", failed=True)
        assert got["status"] == "failed"

    def test_review_does_not_release_the_next_link(self, root):
        """THE POINT OF THE WHOLE FEATURE. Unapproved work must not become the
        foundation the next agent builds on."""
        gates.set_mode(root, gates.BUILDERS)
        first, second = _chain(root)
        queue.complete(root, first["id"], result="baked")
        assert queue.get(root, first["id"])["status"] == "review"
        assert queue.blocker(root, second["id"])["status"] == "review"
        queue.approve(root, first["id"], note="looks right")
        assert queue.blocker(root, second["id"]) is None


class TestApproveAndReject:
    def test_approval_closes_it_and_records_who(self, root, monkeypatch):
        gates.set_mode(root, gates.BUILDERS)
        item = queue.add(root, "art", "a sprite")
        queue.complete(root, item["id"], result="drew it")
        got = queue.approve(root, item["id"], note="ship it", by="adrian")
        assert got["status"] == "done"
        assert got["approved_by"] == "adrian"
        assert "APPROVED by adrian" in got["result"] and "ship it" in got["result"]

    def test_only_held_work_can_be_approved(self, root):
        item = queue.add(root, "art", "not finished")
        with pytest.raises(ValueError):
            queue.approve(root, item["id"])

    def test_rejection_requeues_with_the_reason_in_the_brief(self, root):
        gates.set_mode(root, gates.BUILDERS)
        item = queue.add(root, "art", "a sprite", brief="draw one")
        queue.complete(root, item["id"], result="drew it")
        got = queue.reject(root, item["id"], "off-model against the pinned ref",
                           by="adrian")
        assert got["status"] == "queued"
        assert "off-model against the pinned ref" in got["brief"]
        assert got["attempts"] == 1          # the round counter moved
        assert queue.successors  # sanity: module surface intact

    def test_a_rejection_without_a_reason_is_refused(self, root):
        gates.set_mode(root, gates.BUILDERS)
        item = queue.add(root, "art", "a sprite")
        queue.complete(root, item["id"], result="drew it")
        with pytest.raises(ValueError):
            queue.reject(root, item["id"], "   ")

    def test_the_drain_list_is_what_the_human_owes_an_answer_on(self, root):
        gates.set_mode(root, gates.BUILDERS)
        held = queue.add(root, "art", "a sprite")
        queue.complete(root, held["id"], result="drew it")
        queue.add(root, "tech", "still queued")
        waiting = queue.awaiting_review(root)
        assert [i["id"] for i in waiting] == [held["id"]]


class TestQaGateHonoursTheMode:
    def test_the_qa_scan_is_a_no_op_unless_the_agent_gate_is_on(self, root,
                                                                monkeypatch):
        """A studio that switched to the builder's gate must stop paying for QA
        agents it no longer wants — and the mode is read per scan, not at
        startup, so the switch takes effect on the next item."""
        calls: list[int] = []
        monkeypatch.setattr(_dispatch, "dispatch",
                            lambda r, i, **k: calls.append(i) or {"ok": True})
        item = queue.add(root, "art", "a sprite")
        queue.set_status(root, item["id"], "done", result="drew it")

        gates.set_mode(root, gates.NONE)
        qa_gate._scan_once(str(root), "1970-01-01 00:00:00")
        assert calls == []

        gates.set_mode(root, gates.AGENT)
        qa_gate._scan_once(str(root), "1970-01-01 00:00:00")
        assert len(calls) == 1


class TestOverTheWire:
    """The routes the dashboard drives — the gate control and the two buttons.

    Approve/reject are HTTP-only on purpose and there is no MCP equivalent: a
    tool an agent can call is a gate an agent can clear on its own behalf.
    """

    @pytest.fixture()
    def client(self, root, monkeypatch):
        from fastapi.testclient import TestClient
        from bgate_ui.app import app
        monkeypatch.setenv("BGATE_ROOT", str(root))
        return TestClient(app)

    def test_the_gate_round_trips(self, client):
        assert client.get("/api/gate").json()["mode"] == gates.AGENT
        assert client.post("/api/gate", json={"mode": "builders"}).status_code == 200
        assert client.get("/api/gate").json()["mode"] == gates.BUILDERS
        bad = client.post("/api/gate", json={"mode": "whenever"})
        assert bad.status_code == 400

    def test_a_chain_can_be_filed_and_read_back(self, client):
        made = client.post("/api/queue/chain", json={"links": [
            {"seat": "tech", "title": "bake"},
            {"seat": "gameplay", "title": "wire"},
        ]})
        assert made.status_code == 200
        chain_id = made.json()["chain_id"]
        back = client.get(f"/api/queue/chain/{chain_id}").json()
        assert [i["chain_pos"] for i in back["items"]] == [1, 2]
        assert client.post("/api/queue/chain", json={"links": []}).status_code == 400
        assert client.get("/api/queue/chain/nope").status_code == 404

    def test_the_listing_says_why_a_row_has_no_deploy_button(self, client, root):
        first, second = _chain(root)
        rows = {i["id"]: i for i in client.get("/api/queue").json()["items"]}
        assert rows[first["id"]]["ready"] is True
        assert rows[second["id"]]["ready"] is False
        assert rows[second["id"]]["waiting_on"]["id"] == first["id"]

    def test_approve_and_reject_over_http(self, client, root):
        gates.set_mode(root, gates.BUILDERS)
        held = queue.add(root, "art", "a sprite")
        queue.complete(root, held["id"], result="drew it")
        assert client.get("/api/queue/review").json()["count"] == 1

        no = client.post(f"/api/queue/{held['id']}/reject", json={"reason": ""})
        assert no.status_code == 400
        sent_back = client.post(f"/api/queue/{held['id']}/reject",
                                json={"reason": "off-model"})
        assert sent_back.status_code == 200
        assert sent_back.json()["status"] == "queued"

        queue.complete(root, held["id"], result="redrew it")
        ok = client.post(f"/api/queue/{held['id']}/approve", json={})
        assert ok.status_code == 200 and ok.json()["status"] == "done"
        assert client.get("/api/queue/review").json()["count"] == 0

    def test_the_console_payload_carries_the_mode_and_the_hold(self, client, root):
        gates.set_mode(root, gates.BUILDERS)
        first, second = _chain(root)
        queue.complete(root, first["id"], result="baked")
        state = client.get("/api/console/state?steps=false").json()
        assert state["gate"]["mode"] == gates.BUILDERS
        assert state["floor"]["review"] == 1
        cards = {c["id"]: c for c in state["items"]}
        assert cards[second["id"]]["ready"] is False
        assert cards[second["id"]]["waiting_on"]["status"] == "review"


class TestConsolePayloadWeight:
    """Why the cockpit was heavy: it shipped every step twice.

    MEASURED on a live board — one art agent's 20 phases were 106KB of a 162KB
    payload, and every step's text appeared once in `steps` and again inside the
    phase that contained it. The client renders the newest phase's tail plus one
    opened pocket; it never had a use for the other eighteen.
    """

    def test_older_phases_ship_no_step_text_but_report_the_count(self):
        from bgate_ui.routes.console import _trim_phases, PHASE_STEPS, STEP_PHASES
        phases = [{"n": i + 1, "steps": [{"text": "x" * 200} for _ in range(9)]}
                  for i in range(6)]
        out = _trim_phases(phases)
        assert [len(p["steps"]) for p in out] == \
            [0] * (6 - STEP_PHASES) + [PHASE_STEPS] * STEP_PHASES
        # The honest empty state: an old pocket says how many it had.
        assert out[0]["steps_dropped"] == 9
        assert out[-1]["steps_dropped"] == 9 - PHASE_STEPS

    def test_the_rebuild_is_skipped_when_no_new_steps_arrived(self, root,
                                                             monkeypatch):
        """split() walks the whole ring and look() stats every path it finds, per
        live agent, per poll, PER TAB. Two tabs on one running agent paid for it
        twice a tick."""
        from bgate_ui import phases as _phases
        from bgate_ui.routes import console as _console
        _console._PHASE_CACHE.clear()
        calls = []
        real = _phases.split
        monkeypatch.setattr(_phases, "split",
                            lambda steps, **kw: calls.append(1) or real(steps, **kw))
        feed = {"step_count": 3, "running": True}
        steps = [{"kind": "step", "ts": 1.0, "text": "did a thing"}]
        for _ in range(4):
            _console._phases_for(root, 7, feed, steps, [])
        assert len(calls) == 1

        # A new step invalidates it — a stale cockpit is not a fast one.
        _console._phases_for(root, 7, {"step_count": 4, "running": True},
                             steps + [{"kind": "step", "ts": 2.0, "text": "another"}], [])
        assert len(calls) == 2
