"""What blocks a recording, and what merely makes it worse.

THE BUG THIS PINS. `ready` was `all(checks)`, so every check gated the Record
button — including the transcriber, which is not needed to record and only
decides whether a transcript comes out the other end. On a packaged install
(where speech-to-text is not bundled) the panel therefore said

    record unavailable · 1 check failing

and offered `pip install -e ".[stt,record]"` as the remedy, to somebody running
a .exe who has no checkout to run it in. The feature they came for worked
perfectly and the button was dead.

So the distinction is now data — bgate_core.playtest.REQUIRED_CHECKS — and this
is the test that keeps it honest. A new check added to the preflight lands in
one group or the other, and if somebody adds a blocking one by accident the
`optional` case here starts failing.
"""
from __future__ import annotations

import pytest

from bgate_core import playtest


@pytest.fixture()
def checks(monkeypatch):
    """A preflight where every probe passes, so tests can fail exactly one."""
    from bgate_adapters import recorder, transcribe

    monkeypatch.setattr(recorder, "find_ffmpeg", lambda: "C:/fake/ffmpeg.exe")
    monkeypatch.setattr(recorder, "probe_mic", lambda d=None: {"ok": True})
    monkeypatch.setattr(recorder, "list_windows", lambda: [])
    monkeypatch.setattr(recorder, "resolve_window",
                        lambda t=None, hints=None: {
                            "title": "Game", "whole_desktop": False,
                            "matches": ["Game"], "note": ""})
    monkeypatch.setattr(transcribe, "available", lambda: {"available": True})
    return transcribe, recorder


class TestRequiredVsOptional:
    def test_all_good_is_ready(self, checks):
        p = playtest.preflight()
        assert p["ready"] is True
        assert p["blocking"] == []
        assert p["degraded"] == []

    def test_missing_transcriber_does_not_block_recording(self, checks, monkeypatch):
        """THE REGRESSION. Recording must stay available without speech-to-text."""
        transcribe, _ = checks
        # monkeypatch, NOT a bare assignment: setting the attribute directly
        # leaks into every test that runs afterwards in the same session, and
        # the symptom is an unrelated module's test failing depending on
        # collection order.
        monkeypatch.setattr(transcribe, "available",
                            lambda: {"available": False, "reason": "not bundled"})

        p = playtest.preflight()
        assert p["ready"] is True, "a missing transcriber must not disable Record"
        assert p["blocking"] == []
        assert p["degraded"] == ["transcriber"]
        # And it must say what it COSTS, not what to install.
        costs = p["checks"]["transcriber"]["costs"]
        assert "transcript" in costs
        assert "pip" not in costs.lower()

    def test_missing_ffmpeg_does_block(self, checks, monkeypatch):
        """The other half: a genuinely required tool still stops the button."""
        _, recorder = checks

        def boom():
            raise RuntimeError("ffmpeg not found")
        monkeypatch.setattr(recorder, "find_ffmpeg", boom)

        p = playtest.preflight()
        assert p["ready"] is False
        assert p["blocking"] == ["ffmpeg"]

    def test_every_check_declares_which_group_it_is_in(self, checks):
        """No check may be silently optional by omission."""
        p = playtest.preflight()
        for name, check in p["checks"].items():
            assert "required" in check, f"{name} does not say if it blocks"


class TestNoTerminalInstructions:
    """The panel must not tell a packaged user to run developer commands."""

    def test_optional_costs_never_mention_pip_or_checkouts(self):
        for name, text in playtest.OPTIONAL_COSTS.items():
            low = text.lower()
            assert "pip " not in low, f"{name} tells the user to run pip"
            assert "checkout" not in low, f"{name} tells the user to get a checkout"
            assert "bgate doctor" not in low, f"{name} sends the user to a terminal"
