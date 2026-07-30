"""The follow-up router — the decision half, tested as a pure function.

`qa_gate` was dead in production for weeks with a green suite, because the only
way to reach its logic was through a daemon thread that swallowed everything it
raised. This router is split precisely so that cannot happen again: `decide()`
takes a literal list of events, a literal settings dict and a literal board, and
returns actions. No database, no clock, no thread.

What is pinned here is the branch table (spec section 2.2), the guards that make
at-least-once delivery safe, and the two leashes that stop the debrief being a
money pump.
"""
from __future__ import annotations

import time

import pytest

from bgate_core import events as _events
from bgate_core import queue
from bgate_ui import followup


def _ev(event_id: int, kind: str, item: int, **payload) -> dict:
    payload.setdefault("item", item)
    return {"id": event_id, "kind": kind, "ref": str(item), "actor": "test",
            "payload": payload, "created_at": "2026-07-30 01:00:00"}


def _item(item_id: int, **over) -> dict:
    base = {"id": item_id, "seat": "art", "title": "a sprite", "status": "done",
            "source": "manual", "source_ref": "", "chain_id": "", "chain_pos": 0,
            "attempts": 0, "result": "drew it", "brief": "",
            "updated_at": "2026-07-30 01:00:00"}
    base.update(over)
    return base


def _settings(**over) -> dict:
    base = {"gate_mode": "agent", "director_debrief": False, "max_per_hour": 4,
            "max_age_min": 30, "auto_reopen_failures": False, "max_rounds": 3,
            "notify_kinds": [], "in_app": True, "webhook": "", "quiet_hours": ""}
    base.update(over)
    return base


def _board(items, **over) -> dict:
    """The same shape `snapshot()` builds, by hand. Keys are its keys — a fixture
    that invents its own would test a board that never exists."""
    rows = {int(i["id"]): i for i in items}
    base = {
        "now_s": time.time(), "main_seq": 0, "notify_seq": 0,
        "pending": followup.summarize_pending({}), "quiet": False,
        "dispatcher": True,
        "items": rows,
        "qa": {i: {"open": False, "last": "", "escalated": False} for i in rows},
        "successors": {i: [] for i in rows},
        "debrief_open": {followup.debrief_ref(r): False for r in rows.values()},
        "debriefs_last_hour": 0,
        "advanced": {},
        "age_min": {},
    }
    base.update(over)
    return base


def _kinds(actions) -> list:
    return [a["kind"] for a in actions]


class TestBranchTable:
    def test_the_agent_gate_opens_a_qa_round(self, root):
        got = followup.decide([_ev(1, "item.done", 41)], _settings(gate_mode="agent"),
                              _board([_item(41)]))
        assert "qa_spawn" in _kinds(got)

    def test_no_gate_spawns_nothing(self, root):
        got = followup.decide([_ev(1, "item.done", 41)], _settings(gate_mode="none"),
                              _board([_item(41)]))
        assert "qa_spawn" not in _kinds(got)

    def test_a_held_item_is_skipped_with_the_reason_said_out_loud(self):
        """Under the builder's gate the router must do NOTHING and be able to say
        why — silence and 'held for you' look identical from the outside."""
        got = followup.decide([_ev(1, "item.review", 41)],
                              _settings(gate_mode="builders"),
                              _board([_item(41, status="review")]))
        held = [a for a in got if a["kind"] == "skip" and a["branch"] == 3]
        assert held and "approval" in held[0]["why"]

    def test_a_failure_is_reopened_only_when_the_setting_says_so(self):
        ev, item = _ev(1, "item.failed", 41), _item(41, status="failed")
        off = followup.decide([ev], _settings(auto_reopen_failures=False),
                              _board([item]))
        assert "reopen" not in _kinds(off)
        on = followup.decide([ev], _settings(auto_reopen_failures=True),
                             _board([item]))
        assert "reopen" in _kinds(on)

    def test_a_failure_past_the_round_cap_is_not_reopened_forever(self):
        got = followup.decide([_ev(1, "item.failed", 41)],
                              _settings(auto_reopen_failures=True, max_rounds=3),
                              _board([_item(41, status="failed", attempts=5)]))
        assert "reopen" not in _kinds(got)

    def test_a_chain_with_a_successor_does_not_debrief(self):
        """Branch 5 is for the END of a piece of work. Debriefing every link
        would file a director run per chain step."""
        got = followup.decide(
            [_ev(1, "item.done", 41)],
            _settings(gate_mode="none", director_debrief=True),
            _board([_item(41, chain_id="c1", chain_pos=1)],
                   successors={41: [{"id": 42, "seat": "gameplay",
                                     "title": "wire", "status": "queued",
                                     "chain_pos": 2}]}))
        assert "debrief" not in _kinds(got)
        assert "emit" in _kinds(got)          # chain.advanced instead

    def test_the_last_link_debriefs_when_it_is_switched_on(self):
        got = followup.decide(
            [_ev(1, "item.done", 41)],
            _settings(gate_mode="none", director_debrief=True),
            _board([_item(41, chain_id="c1", chain_pos=2)]))
        assert "debrief" in _kinds(got)

    def test_an_event_for_an_item_that_no_longer_exists_is_skipped_not_crashed(self):
        got = followup.decide([_ev(1, "item.done", 999)], _settings(), _board([]))
        assert _kinds(got) == ["skip"]


class TestTheLeash:
    def test_the_debrief_is_off_by_default(self):
        """A feature that spends money per completion must never arrive switched
        on. The registry default is what this is really asserting."""
        from bgate_core import settings as _settings_mod
        assert _settings_mod.setting("followup.director_debrief").default is False
        got = followup.decide([_ev(1, "item.done", 41)],
                              _settings(gate_mode="none"), _board([_item(41)]))
        assert "debrief" not in _kinds(got)

    def test_one_debrief_per_chain(self):
        held = _item(41, chain_id="c1", chain_pos=2)
        board = _board([held])
        board["debrief_open"] = {followup.debrief_ref(held): True}
        got = followup.decide([_ev(1, "item.done", 41)],
                              _settings(gate_mode="none", director_debrief=True), board)
        assert "debrief" not in _kinds(got)

    def test_the_rate_cap_holds(self):
        got = followup.decide(
            [_ev(1, "item.done", 41)],
            _settings(gate_mode="none", director_debrief=True, max_per_hour=2),
            _board([_item(41)], debriefs_last_hour=2))
        assert "debrief" not in _kinds(got)

    def test_a_stale_completion_is_not_debriefed_hours_later(self):
        """Catching up after a restart must not fire eight hours of debriefs."""
        got = followup.decide(
            [_ev(1, "item.done", 41)],
            _settings(gate_mode="none", director_debrief=True, max_age_min=30),
            # age_min is keyed by EVENT id, not item id — the age that
            # matters is the event's, which is what a resuming cursor replays.
            _board([_item(41)], age_min={1: 600.0}))
        assert "debrief" not in _kinds(got)

    def test_no_debrief_without_a_live_dispatcher(self):
        """A queued row on a dead board looks exactly like delegated work and is
        not — the trap DIRECTOR_PROTOCOL already warns about."""
        got = followup.decide(
            [_ev(1, "item.done", 41)],
            _settings(gate_mode="none", director_debrief=True),
            _board([_item(41)], dispatcher=False))
        assert "debrief" not in _kinds(got)

    @pytest.mark.parametrize("source", ["qa-gate", "qa-gate-escalation",
                                        "completion", "chat"])
    def test_the_router_never_debriefs_its_own_kind_of_work(self, source):
        """A completion loop that debriefs its own debriefs is the money pump
        qa_gate.MAX_ROUNDS exists to stop."""
        got = followup.decide(
            [_ev(1, "item.done", 41)],
            _settings(gate_mode="none", director_debrief=True),
            _board([_item(41, source=source)]))
        assert "debrief" not in _kinds(got)


class TestIdempotency:
    def test_an_already_routed_event_is_not_routed_twice(self):
        """Delivery is at-least-once: a subscriber that acts and then dies before
        writing its cursor sees the same event again."""
        got = followup.decide([_ev(7, "item.done", 41)], _settings(gate_mode="agent"),
                              _board([_item(41)], main_seq=7))
        assert "qa_spawn" not in _kinds(got)

    def test_an_open_qa_round_is_not_opened_again(self):
        got = followup.decide([_ev(1, "item.done", 41)], _settings(gate_mode="agent"),
                              _board([_item(41)],
                                     qa={41: {"open": True, "last": "",
                                              "escalated": False}}))
        assert "qa_spawn" not in _kinds(got)

    def test_every_action_carries_a_guard_key(self):
        """The guard is what makes re-delivery safe; an action without one cannot
        be de-duplicated by the runner."""
        got = followup.decide([_ev(1, "item.done", 41)], _settings(gate_mode="agent"),
                              _board([_item(41)]))
        assert got and all(a.get("guard") for a in got)


class TestQuietHoursAndWebhook:
    @pytest.mark.parametrize("window, hour, quiet", [
        ("22:00-07:00", 23, True),      # inside, before midnight
        ("22:00-07:00", 3, True),       # inside, after midnight — the wraparound
        ("22:00-07:00", 12, False),
        ("", 3, False),                 # unset means never quiet
    ])
    def test_quiet_hours_wrap_around_midnight(self, window, hour, quiet):
        when = time.struct_time((2026, 7, 30, hour, 0, 0, 3, 211, 0))
        assert followup.in_quiet_hours(window, when) is quiet

    @pytest.mark.parametrize("url", [
        "http://example.com/hook",       # not https
        "https://127.0.0.1/hook",        # loopback
        "https://10.0.0.5/hook",         # private range
        "https://169.254.169.254/latest",  # the cloud metadata endpoint
        "ftp://example.com",
        "not a url",
    ])
    def test_a_webhook_that_would_be_an_ssrf_is_refused(self, url):
        """This posts project text from a loopback service to a URL a user typed.
        The address rules are the whole reason it is safe to ship."""
        target, why = followup.webhook_target(url)
        assert target == "" and why

    def test_a_public_https_webhook_is_accepted(self, monkeypatch):
        # Resolution is stubbed so the check is deterministic and offline: what
        # is under test is the address RULE, not this machine's DNS.
        import socket
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
        target, why = followup.webhook_target("https://hooks.example.com/abc")
        assert target and not why


class TestAgainstARealBoard:
    """The I/O half. `snapshot` is where every guard actually reads the database,
    and none of those queries had ever been executed."""

    def test_snapshot_gathers_the_board_decide_needs(self, root):
        first, second = queue.add_chain(root, [
            {"seat": "tech", "title": "bake"},
            {"seat": "gameplay", "title": "wire"}])
        queue.complete(root, first["id"], result="baked")
        batch = _events.since(root, 0)["events"]
        board = followup.snapshot(root, batch, settings=followup.load_settings(root))
        assert int(first["id"]) in board["items"]
        assert board["successors"].get(int(first["id"]))
        assert "dispatcher" in board and "now_s" in board
        assert board["qa"] and board["debrief_open"]

    def test_load_settings_survives_an_unreadable_registry(self, root, monkeypatch):
        from bgate_core import settings as _settings_mod
        monkeypatch.setattr(_settings_mod, "get",
                            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
        got = followup.load_settings(root)
        assert got["max_rounds"] >= 1 and got["director_debrief"] is False

    def test_a_full_tick_on_a_real_completion_does_not_raise(self, root, monkeypatch):
        """The one end-to-end pass: real DB, real cursor, real guards — with
        dispatch stubbed so nothing spawns."""
        from bgate_ui import dispatch as _dispatch
        monkeypatch.setattr(_dispatch, "dispatch",
                            lambda *a, **k: {"ok": True, "item_id": 0})
        monkeypatch.setattr(_dispatch, "find_claude", lambda: "claude")
        item = queue.add(root, "art", "a sprite")
        queue.complete(root, item["id"], result="drew it")
        got = followup.tick(root)
        assert isinstance(got, dict)
        # The cursor moved, so a second tick is a no-op rather than a repeat.
        assert _events.cursor_get(root, followup.CONSUMER) > 0
        again = followup.tick(root)
        assert not [a for a in (again.get("actions") or [])
                    if a.get("kind") == "qa_spawn"]
