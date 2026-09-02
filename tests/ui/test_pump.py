"""The shared daemon-pump primitive.

Auto-deploy, the follow-up router and the steer pump all run on this now. Their
own suites cover it end to end, but the invariants belong somewhere they can be
read without a project fixture and a live board — and a primitive that three
loops depend on with no test of its own is exactly how the three copies it
replaced drifted apart in the first place.
"""
from __future__ import annotations

import threading
import time

import pytest

from bgate_ui.pumps.pump import Pump


def _wait(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestStart:
    def test_it_is_idempotent_per_root(self):
        pump = Pump("bgate-test-idempotent", 30.0, lambda _root: None)
        before = {t for t in threading.enumerate() if t.name == pump.name}
        assert pump.start("/a") is True
        assert pump.start("/a") is True
        started = {t for t in threading.enumerate()
                   if t.name == pump.name} - before
        assert len(started) == 1, "the second start() spawned another loop"
        assert next(iter(started)).daemon

    def test_a_second_root_gets_its_own_loop(self):
        """Per project, not per process: the active project can change under a
        long-lived server, and one latched flag pumps the one the user left."""
        pump = Pump("bgate-test-two-roots", 30.0, lambda _root: None)
        before = {t for t in threading.enumerate() if t.name == pump.name}
        pump.start("/a")
        pump.start("/b")
        started = {t for t in threading.enumerate()
                   if t.name == pump.name} - before
        assert len(started) == 2

    def test_running_reports_per_root(self):
        pump = Pump("bgate-test-running", 30.0, lambda _root: None)
        assert pump.running("/a") is False
        pump.start("/a")
        assert pump.running("/a") is True
        assert pump.running("/b") is False


class TestTheKillSwitch:
    @pytest.mark.parametrize("value", ["0", "false", "off", "OFF", " 0 "])
    def test_it_refuses_to_start(self, monkeypatch, value):
        pump = Pump("bgate-test-off", 30.0, lambda _root: None,
                    env_var="BGATE_TEST_PUMP")
        monkeypatch.setenv("BGATE_TEST_PUMP", value)
        assert pump.disabled() is True
        assert pump.start("/a") is False
        assert pump.running("/a") is False

    def test_unset_means_on(self, monkeypatch):
        pump = Pump("bgate-test-unset", 30.0, lambda _root: None,
                    env_var="BGATE_TEST_PUMP")
        monkeypatch.delenv("BGATE_TEST_PUMP", raising=False)
        assert pump.disabled() is False
        assert pump.start("/a") is True

    def test_no_switch_at_all_is_never_disabled(self, monkeypatch):
        pump = Pump("bgate-test-noswitch", 30.0, lambda _root: None)
        monkeypatch.setenv("BGATE_TEST_PUMP", "0")
        assert pump.disabled() is False


class TestTheLoop:
    def test_it_passes_the_root_it_was_started_with(self):
        seen: list[str] = []
        pump = Pump("bgate-test-root-arg", 0.01, seen.append)
        pump.start("/some/project")
        assert _wait(lambda: seen), "the loop never ticked"
        assert seen[0] == "/some/project"

    def test_a_raising_tick_does_not_kill_the_loop(self):
        """Fail-safe: these loops sit beside the dashboard, not under it."""
        ticks: list[int] = []

        def _boom(_root):
            ticks.append(1)
            raise RuntimeError("seeded failure")

        pump = Pump("bgate-test-boom", 0.01, _boom)
        pump.start("/a")
        assert _wait(lambda: len(ticks) >= 3), "the loop stopped after a raise"
        assert any(t.name == pump.name and t.is_alive()
                   for t in threading.enumerate())

    def test_the_interval_is_asked_for_again_rather_than_frozen(self):
        """Each module passes ``lambda: POLL_S`` so a test can shorten it.

        Read once at construction, the value froze at the production interval and
        any test that shortened its module's POLL_S waited the real one instead.
        """
        interval = [30.0]
        pump = Pump("bgate-test-interval", lambda: interval[0],
                    lambda _root: None)
        assert pump.poll_s == 30.0
        interval[0] = 0.01
        assert pump.poll_s == 0.01

    def test_a_plain_number_still_works(self):
        assert Pump("bgate-test-number", 2, lambda _root: None).poll_s == 2.0


class TestReset:
    def test_it_unlatches_one_root(self):
        pump = Pump("bgate-test-reset-one", 30.0, lambda _root: None)
        pump.start("/a")
        pump.start("/b")
        pump.reset("/a")
        assert pump.running("/a") is False
        assert pump.running("/b") is True

    def test_no_argument_unlatches_everything(self):
        pump = Pump("bgate-test-reset-all", 30.0, lambda _root: None)
        pump.start("/a")
        pump.start("/b")
        pump.reset()
        assert pump.running("/a") is False
        assert pump.running("/b") is False
