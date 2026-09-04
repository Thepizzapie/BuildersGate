"""A failed item goes back to the director — ONCE — and never in a loop.

This pins the cap, and the cap is the whole safety property. Before it, a failed
item sat on the board with a red marker until a human noticed; the obvious fix
(re-dispatch until it lands) is a token bonfire, because an item that failed for
a structural reason — a missing key, a credit block, an asset that does not
exist, a lane the seat cannot write to — fails identically every round. MEASURED:
item #405 failed on a kie credit block that was already filed as its own separate
item, and no number of retries could ever have fixed it.

So the four properties tested hardest here are the four ways money escapes:
  * a failure is retried at most `followup.max_auto_retries` times, ever;
  * the count is on the ROW, so a dashboard restart does not refill it;
  * an item a HUMAN stopped is never bought back;
  * an escalation cannot escalate itself.

The decision half is pure (see bgate_ui.agents.followup), so most of this is literal
dicts in and action dicts out — no thread, no clock, and no chance of the logic
being dead in production with a green suite.
"""
from __future__ import annotations

import time

from bgate_core.store import db
from bgate_core.board import queue
from bgate_ui.agents import followup


def _ev(event_id: int, item: int, kind: str = "item.failed", **payload) -> dict:
    payload.setdefault("item", item)
    return {"id": event_id, "kind": kind, "ref": str(item), "actor": "test",
            "payload": payload, "created_at": "2026-08-14 01:00:00"}


def _item(item_id: int = 41, **over) -> dict:
    base = {"id": item_id, "seat": "art", "title": "a sprite",
            "status": "failed", "source": "manual", "source_ref": "",
            "chain_id": "", "chain_pos": 0, "attempts": 0, "auto_retries": 0,
            "stopped_by": "", "result": "kie returned 402: no credit",
            "brief": "", "updated_at": "2026-08-14 01:00:00"}
    base.update(over)
    return base


def _settings(**over) -> dict:
    base = {"gate_mode": "agent", "director_debrief": False, "max_per_hour": 4,
            "max_age_min": 30, "auto_reopen_failures": True, "max_rounds": 3,
            "max_auto_retries": 1, "escalate_failures": True,
            "notify_kinds": [], "in_app": True, "webhook": "",
            "quiet_hours": ""}
    base.update(over)
    return base


def _board(items, **over) -> dict:
    rows = {int(i["id"]): i for i in items}
    base = {
        "now_s": time.time(), "main_seq": 0, "notify_seq": 0,
        "pending": followup.summarize_pending({}), "quiet": False,
        "dispatcher": True, "items": rows,
        "qa": {i: {"open": False, "last": "", "escalated": False} for i in rows},
        "successors": {i: [] for i in rows},
        "debrief_open": {followup.debrief_ref(r): False for r in rows.values()},
        "fail_escalated": {i: False for i in rows},
        "debriefs_last_hour": 0, "advanced": {}, "age_min": {},
    }
    base.update(over)
    return base


def _kinds(actions) -> list:
    return [a["kind"] for a in actions]


def _decide(item, **settings_over):
    item = dict(item)
    return followup.decide([_ev(1, int(item["id"]))],
                           _settings(**settings_over), _board([item]))


class TestTheCap:
    def test_the_first_failure_is_retried_once(self):
        got = _decide(_item(auto_retries=0))
        assert "reopen" in _kinds(got)
        assert "fail_escalate" not in _kinds(got)

    def test_the_second_failure_escalates_instead_of_retrying(self):
        """THE WHOLE POINT. The retry that was already bought did not fix it, so
        another one buys the same failure at the same price."""
        got = _decide(_item(auto_retries=1, attempts=1))
        assert "fail_escalate" in _kinds(got)
        assert "reopen" not in _kinds(got)

    def test_a_zero_budget_escalates_on_the_very_first_failure(self):
        """0 must mean zero. `int(x or default)` would read it as unset and buy
        an agent the operator explicitly refused."""
        got = _decide(_item(auto_retries=0), max_auto_retries=0)
        assert _kinds(got) == ["fail_escalate"]

    def test_the_budget_is_read_from_the_row_not_from_this_process(self, root):
        """A cap held in memory is not a cap: the dashboard restarts and the
        item is retried from zero. So the counter is a column."""
        item = queue.add(root, "art", "a sprite")
        item_id = int(item["id"])
        assert int(queue.get(root, item_id).get("auto_retries") or 0) == 0
        queue.note_auto_retry(root, item_id)
        db.close_all()                      # the restart, as far as SQLite cares
        assert int(queue.get(root, item_id)["auto_retries"]) == 1
        # And the decision made from that row refuses the second retry.
        fresh = dict(queue.get(root, item_id), status="failed")
        assert "reopen" not in _kinds(_decide(fresh))

    def test_the_retry_setting_off_still_reaches_the_director(self):
        """Off means 'do not spend', not 'say nothing'. The failure sitting on
        the board unmentioned is the bug this branch exists to remove."""
        got = _decide(_item(), auto_reopen_failures=False)
        assert _kinds(got) == ["fail_escalate"]

    def test_escalating_is_switchable_off_and_says_so(self):
        got = _decide(_item(), auto_reopen_failures=False,
                      escalate_failures=False)
        assert _kinds(got) == ["skip"]
        assert "escalate_failures is off" in got[0]["why"]


class TestTheThingsThatMustNeverBeRetried:
    def test_a_human_stopped_item_is_never_bought_back(self):
        """queue.stop banks a stop as 'failed' on purpose and the reaper
        announces it with item.failed like any other, so without this check
        pressing STOP would immediately re-spawn the agent."""
        got = _decide(_item(stopped_by="Sam"))
        assert _kinds(got) == ["skip"]
        assert "stopped" in got[0]["why"]

    def test_a_human_stopped_item_is_not_escalated_either(self):
        """The person who stopped it already knows. A director item about their
        own decision is how a channel earns its mute."""
        got = _decide(_item(stopped_by="Sam"), auto_reopen_failures=False)
        assert "fail_escalate" not in _kinds(got)

    def test_an_escalation_that_fails_does_not_escalate_itself(self):
        """The loop guard. One director item per failure per failure is the
        runaway this whole branch exists to stop."""
        got = _decide(_item(seat="director",
                            source=queue.FAILURE_ESCALATION_SOURCE,
                            source_ref="41"))
        assert _kinds(got) == ["skip"]
        assert "loop" in got[0]["why"]

    def test_the_debrief_and_the_qa_escalation_are_on_the_same_list(self):
        for source in ("completion", "qa-gate-escalation", "chat"):
            got = _decide(_item(seat="director", source=source))
            assert _kinds(got) == ["skip"], source

    def test_an_item_already_escalated_is_not_escalated_twice(self):
        got = followup.decide(
            [_ev(1, 41)], _settings(auto_reopen_failures=False),
            _board([_item(41)], fail_escalated={41: True}))
        assert _kinds(got) == ["skip"]
        assert "already been escalated" in got[0]["why"]

    def test_no_live_dispatcher_escalates_rather_than_queueing_a_retry(self):
        """A reopened item on a board that cannot run agents looks exactly like
        delegated work and is not — the lie this project refuses to tell."""
        got = followup.decide([_ev(1, 41)], _settings(),
                              _board([_item(41)], dispatcher=False))
        assert "reopen" not in _kinds(got)
        assert "fail_escalate" in _kinds(got)


class TestApplying:
    def _failed(self, root, **over):
        item = queue.add(root, over.pop("seat", "art"),
                         over.pop("title", "a sprite"), **over)
        return queue.set_status(root, int(item["id"]), "failed",
                                result="kie returned 402: no credit")

    def test_the_escalation_is_filed_for_the_director_and_names_the_failure(
            self, root):
        item = self._failed(root)
        item_id = int(item["id"])
        out = followup.apply_action(root, {
            "kind": "fail_escalate", "item": item_id, "event": 1,
            "guard": f"fail-escalation:{item_id}", "why": "capped",
            "reason": "its automatic retry budget is spent",
            "auto_retries": 1, "auto_cap": 1, "rounds": 2})
        assert out["ok"]
        filed = queue.get(root, int(out["escalation"]))
        assert filed["seat"] == "director"
        assert filed["source"] == queue.FAILURE_ESCALATION_SOURCE
        assert filed["source_ref"] == str(item_id)
        assert filed["status"] == "queued"
        # It must be unmistakably an escalation rather than a request to do the
        # work — a director that reads it as new work has re-dispatched the
        # failing item by hand, around the cap.
        assert filed["title"].startswith("FAILED:")
        assert "ESCALATION" in filed["brief"]
        assert "NOT a request to do the work" in filed["brief"]
        # And it carries the facts a decision needs: the seat, the failure text,
        # and the standing suspicion of an upstream blocker.
        assert "402" in filed["brief"] and "art" in filed["brief"]
        assert "STRUCTURAL" in filed["brief"]

    def test_the_escalation_is_dispatchable_to_a_director_agent(self, root):
        """Inverted on 2026-08-19. Held-for-a-human was the board's deepest
        dead end: with no console session open, every escalation sat queued
        forever and the failed item stayed failed until a person cleared and
        re-dispatched by hand. It is now an ordinary director-seat row - an
        auto-dispatcher may spawn an agent to diagnose and ACT on it - and
        the spend bound lives where it always did: one escalation per item,
        ever. Only qa-gate-escalation and chat stay human-held."""
        item = self._failed(root)
        followup.apply_action(root, {
            "kind": "fail_escalate", "item": int(item["id"]), "event": 1,
            "guard": "", "why": "", "reason": "capped", "auto_cap": 1})
        assert queue.FAILURE_ESCALATION_SOURCE not in queue.HELD_SOURCES
        claimed = queue.claim_next(root, "director", actor="agent:test")
        assert claimed is not None
        assert claimed["source"] == queue.FAILURE_ESCALATION_SOURCE

    def test_filing_twice_files_one(self, root):
        """Delivery is at-least-once: the batch that decided this is replayed
        after a crash, and the same item can fail twice inside one batch."""
        item = self._failed(root)
        action = {"kind": "fail_escalate", "item": int(item["id"]), "event": 1,
                  "guard": "", "why": "", "reason": "capped", "auto_cap": 1}
        first = followup.apply_action(root, action)
        second = followup.apply_action(root, action)
        assert first["ok"] and not second["ok"]
        assert "already escalated" in second["why"]
        filed = [r for r in queue.list_items(root)
                 if r["source"] == queue.FAILURE_ESCALATION_SOURCE]
        assert len(filed) == 1

    def test_a_retry_counts_itself_on_the_row(self, root):
        item = self._failed(root)
        item_id = int(item["id"])
        out = followup.apply_action(root, {
            "kind": "reopen", "item": item_id, "event": 1, "guard": "",
            "why": "retrying", "attempts": 0, "auto_retries": 0,
            "reason": "AUTO-REOPENED — it said: 402 no credit"})
        assert out["ok"]
        after = queue.get(root, item_id)
        assert after["status"] == "queued"
        assert int(after["auto_retries"]) == 1
        assert "AUTO-REOPENED" in after["brief"]

    def test_a_replayed_retry_does_not_buy_a_second_round(self, root):
        """The cap is re-checked at the moment of spending, not trusted from the
        snapshot — a replay that retried again is the cap not holding."""
        item = self._failed(root)
        item_id = int(item["id"])
        action = {"kind": "reopen", "item": item_id, "event": 1, "guard": "",
                  "why": "retrying", "attempts": 0, "auto_retries": 0,
                  "reason": "AUTO-REOPENED"}
        assert followup.apply_action(root, action)["ok"]
        queue.set_status(root, item_id, "failed", result="402 again")
        replay = followup.apply_action(root, action)
        assert not replay["ok"]
        assert int(queue.get(root, item_id)["auto_retries"]) == 1

    def test_applying_a_retry_to_a_stopped_item_refuses(self, root):
        """Between deciding and acting, somebody may have pressed STOP."""
        item = queue.add(root, "art", "a sprite")
        item_id = int(item["id"])
        queue.stop(root, item_id, by="Sam")
        out = followup.apply_action(root, {
            "kind": "reopen", "item": item_id, "event": 1, "guard": "",
            "why": "retrying", "attempts": 0, "auto_retries": 0,
            "reason": "AUTO-REOPENED"})
        assert not out["ok"]
        assert "stopped" in out["why"]
        assert queue.get(root, item_id)["status"] == "failed"


class TestTheWholeLoopTerminates:
    def test_fail_retry_fail_escalate_and_then_nothing(self, root):
        """End to end on a real board: one retry, one escalation, and the
        escalation's own failure adds nothing. This is the property that stops
        the money pump."""
        item = queue.add(root, "art", "a sprite that needs a paid provider")
        item_id = int(item["id"])
        queue.set_status(root, item_id, "failed", result="402: no credit")

        # Round one: the harness buys exactly one retry.
        board = _board([dict(queue.get(root, item_id))])
        first = followup.decide([_ev(1, item_id)], _settings(), board)
        assert _kinds(first) == ["reopen"]
        assert followup.apply_action(root, first[0])["ok"]

        # It fails the same way, because the blocker is upstream of the seat.
        queue.set_status(root, item_id, "failed", result="402: no credit")
        board = _board([dict(queue.get(root, item_id))])
        second = followup.decide([_ev(2, item_id)], _settings(), board)
        assert _kinds(second) == ["fail_escalate"]
        applied = followup.apply_action(root, second[0])
        escalation_id = int(applied["escalation"])

        # The escalation itself fails (the director agent crashed, say).
        queue.set_status(root, escalation_id, "failed", result="crashed")
        board = _board([dict(queue.get(root, escalation_id))])
        third = followup.decide([_ev(3, escalation_id)], _settings(), board)
        assert _kinds(third) == ["skip"]

        # One retry and one escalation, and the board has stopped growing.
        assert int(queue.get(root, item_id)["auto_retries"]) == 1
        filed = [r for r in queue.list_items(root)
                 if r["source"] == queue.FAILURE_ESCALATION_SOURCE]
        assert len(filed) == 1


class TestTheEscalationReachesTheSession:
    """The filed card becomes a decision that gets MADE.

    A project whose human uses the console's director session hands a fresh
    escalation to THAT session first (it reserves the row, so no other
    dispatcher can race it). A project without one no longer parks the card:
    the row is an ordinary director-seat item and autodeploy may spawn an
    agent for it.
    """

    def _fail_and_escalate(self, root):
        item = queue.add(root, "art", "doomed sprite")
        queue.set_status(root, int(item["id"]), "dispatched")
        queue.set_status(root, int(item["id"]), "failed", "kie 402: no credit")
        action = followup._action(
            "fail_escalate", 1, _ev(1, int(item["id"])),
            f"fail-escalation:{item['id']}", "budget spent",
            item=int(item["id"]), reason="budget spent",
            auto_retries=1, auto_cap=1, rounds=2)
        return int(item["id"]), followup.apply_action(root, action)

    def test_no_known_session_leaves_the_escalation_held(self, root):
        item_id, applied = self._fail_and_escalate(root)
        assert applied.get("session") is False
        esc = queue.get(root, int(applied["escalation"]))
        assert esc["status"] == "queued"        # the shipped behaviour

    def test_a_known_session_takes_the_escalation(self, root, monkeypatch):
        from bgate_ui.agents import directorsession, runners

        directorsession._write_sidecar(root, {"cli_session_id": "s1"})
        taken = {}

        def fake_submit(r, item_id, prompt, reseed_context=""):
            taken.update({"item_id": int(item_id), "prompt": prompt})
            return {"ok": True}

        monkeypatch.setattr(directorsession, "submit", fake_submit)
        monkeypatch.setattr(runners, "find_claude", lambda: "claude")
        item_id, applied = self._fail_and_escalate(root)
        assert applied.get("session") is True
        esc = queue.get(root, int(applied["escalation"]))
        assert esc["status"] == "dispatched"    # the session holds it now
        assert taken["item_id"] == int(applied["escalation"])
        # the prompt names the FAILED item so the session can act on it
        assert f"#{item_id}" in taken["prompt"]
        assert "queue_reopen" in taken["prompt"]

    def test_a_crashing_handoff_releases_the_escalation(self, root, monkeypatch):
        from bgate_ui.agents import directorsession, runners

        directorsession._write_sidecar(root, {"cli_session_id": "s1"})

        def boom(*a, **k):
            raise RuntimeError("no pipe")

        monkeypatch.setattr(directorsession, "submit", boom)
        monkeypatch.setattr(runners, "find_claude", lambda: "claude")
        item_id, applied = self._fail_and_escalate(root)
        assert applied.get("session") is False
        esc = queue.get(root, int(applied["escalation"]))
        assert esc["status"] == "queued"        # released, not stranded

    def test_the_switch_turns_it_off(self, root, monkeypatch):
        from bgate_core.store import settings as core_settings
        from bgate_ui.agents import directorsession

        directorsession._write_sidecar(root, {"cli_session_id": "s1"})
        core_settings.set(root, "followup.escalation_to_session", False)
        item_id, applied = self._fail_and_escalate(root)
        assert applied.get("session") is False
        assert queue.get(root, int(applied["escalation"]))["status"] == "queued"


class TestTheFailedSweep:
    """A failure whose item.failed event was lost is found anyway.

    events.emit is best-effort, so a locked database can eat the one event the
    router keys on — and a purely cursor-driven router then never retries or
    escalates that failure. The sweep routes it through the SAME branch, so
    every guard is the code the event path runs.
    """

    def _lost_failure(self, root, minutes_old: int = 30) -> int:
        item = queue.add(root, "art", "quietly dead")
        queue.set_status(root, int(item["id"]), "dispatched")
        queue.set_status(root, int(item["id"]), "failed", "kie 402")
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE work_item SET updated_at = "
                "datetime('now', ?) WHERE id = ?",
                (f"-{int(minutes_old)} minutes", int(item["id"])))
        return int(item["id"])

    def test_a_lost_failure_is_escalated_by_the_sweep(self, root):
        item_id = self._lost_failure(root)
        applied = followup.sweep_failed(
            root, _settings(auto_reopen_failures=False))
        assert applied, "the sweep found nothing"
        filed = [r for r in queue.list_items(root)
                 if r["source"] == queue.FAILURE_ESCALATION_SOURCE]
        assert len(filed) == 1
        assert filed[0]["source_ref"] == str(item_id)

    def test_the_sweep_is_idempotent(self, root):
        self._lost_failure(root)
        followup.sweep_failed(root, _settings(auto_reopen_failures=False))
        again = followup.sweep_failed(
            root, _settings(auto_reopen_failures=False))
        assert again == []
        filed = [r for r in queue.list_items(root)
                 if r["source"] == queue.FAILURE_ESCALATION_SOURCE]
        assert len(filed) == 1

    def test_a_fresh_failure_is_left_to_the_event_path(self, root):
        # Inside the grace window the event is presumed still in the batch.
        self._lost_failure(root, minutes_old=0)
        assert followup.sweep_failed(
            root, _settings(auto_reopen_failures=False)) == []

    def test_a_human_stop_is_not_swept(self, root):
        item = queue.add(root, "art", "stopped on purpose")
        queue.set_status(root, int(item["id"]), "dispatched")
        queue.stop(root, int(item["id"]), by="human@box")
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET updated_at = "
                         "datetime('now', '-30 minutes') WHERE id = ?",
                         (int(item["id"]),))
        assert followup.sweep_failed(
            root, _settings(auto_reopen_failures=False)) == []


class TestTheProgressBonus:
    """A failure that NAMED ITS NEXT MOVE buys one more round, and only one.

    The flat cap of one is right about a STRUCTURAL failure and wrong about
    iterative craft work, which fails by narrowing: the run that motivated this
    went knee weight-bleed -> ankle non-manifold damage -> that patch cleaned,
    seam remains, and was escalated to a human mid-narrowing holding an untried
    approach it had just written down. The class is declared by the agent, in
    queue_complete(next_approach=...), so what is pinned here is that declaring
    it is worth exactly ONE extra round and cannot be turned into a loop.
    """

    def test_a_named_next_approach_buys_a_second_automatic_round(self):
        got = _decide(_item(auto_retries=1, attempts=1,
                            result="seam still shatters\n\n"
                                   + queue.NEXT_APPROACH_MARKER
                                   + " stitch the boundary loop by hand"))
        assert "reopen" in _kinds(got)
        assert "fail_escalate" not in _kinds(got)

    def test_without_one_the_same_item_escalates(self):
        """The control for the test above — same counters, no declaration."""
        got = _decide(_item(auto_retries=1, attempts=1, result="seam still shatters"))
        assert "fail_escalate" in _kinds(got)

    def test_the_bonus_is_worth_exactly_one_round(self):
        """It is a bonus, not a bypass: an item that keeps naming a next move
        would otherwise retry forever, which is the bonfire the cap exists to
        stop."""
        got = _decide(_item(auto_retries=2, attempts=2,
                            result=queue.NEXT_APPROACH_MARKER + " one more idea"))
        assert "fail_escalate" in _kinds(got)
        assert "reopen" not in _kinds(got)

    def test_a_zero_cap_still_escalates_on_the_first_failure(self):
        """0 is an operator instruction ('escalate immediately'), not a budget
        for the bonus to stack on."""
        got = _decide(_item(auto_retries=0,
                            result=queue.NEXT_APPROACH_MARKER + " an idea"),
                      max_auto_retries=0)
        assert _kinds(got) == ["fail_escalate"]

    def test_the_round_cap_still_ends_it(self):
        """qa.max_rounds is the absolute ceiling and the bonus does not lift
        it — otherwise the two caps disagree and the looser one wins."""
        got = _decide(_item(auto_retries=1, attempts=3,
                            result=queue.NEXT_APPROACH_MARKER + " an idea"),
                      max_rounds=3)
        assert "fail_escalate" in _kinds(got)

    def test_the_next_move_leads_the_reopen_brief(self):
        """Buried under 1200 characters of post-mortem it would be re-derived,
        which is the cost this whole path exists to avoid."""
        got = _decide(_item(auto_retries=1, attempts=1,
                            result=queue.NEXT_APPROACH_MARKER
                                   + " stitch the boundary loop by hand"))
        reopen = [a for a in got if a["kind"] == "reopen"][0]
        assert reopen["reason"].startswith("START HERE")
        assert "stitch the boundary loop by hand" in reopen["reason"].split(
            "AUTO-REOPENED")[0]

    def test_a_human_stop_is_still_never_bought_back(self):
        """The stop guard runs before the budget, so a next_approach in the
        note of a run somebody killed must not resurrect it."""
        got = _decide(_item(auto_retries=0, stopped_by="adrian",
                            result=queue.NEXT_APPROACH_MARKER + " an idea"))
        assert _kinds(got) == ["skip"]


class TestTheMarker:
    """The declaration round-trips through the result note, not a column — so a
    project whose database predates this gets the behaviour, and the human
    reading the board sees the sentence the router acted on."""

    def test_round_trip(self):
        note = queue.with_next_approach("it failed", "  try   the other solver ")
        assert queue.NEXT_APPROACH_MARKER in note
        assert note.startswith("it failed")
        assert queue.next_approach_of({"result": note}) == "try the other solver"

    def test_an_empty_next_approach_changes_nothing(self):
        assert queue.with_next_approach("it failed", "   ") == "it failed"
        assert queue.next_approach_of({"result": "it failed"}) == ""

    def test_the_last_marker_wins(self):
        """A reopen appends the previous note to the brief and agents echo it,
        so a result can carry an older attempt's marker above its own."""
        note = queue.with_next_approach(
            "quoting round 1: " + queue.NEXT_APPROACH_MARKER + " the old idea",
            "the new idea")
        assert queue.next_approach_of({"result": note}) == "the new idea"
