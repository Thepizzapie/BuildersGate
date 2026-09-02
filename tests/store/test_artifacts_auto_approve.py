"""art.auto_approve — the switch that stops every render queueing a human card.

Written because the first attempt at this feature only gated ``review()``, and
that was not enough: an agent never CALLS review(), it REGISTERS, and a
registration lands as a candidate. The switch was on, the wall was gone, and the
approval cards kept coming — which is the whole complaint the setting exists to
answer. These tests pin the behaviour at the point that actually fires.
"""
from __future__ import annotations

import pytest

from bgate_core.store import artifacts, settings


def _make(root, name="kiosk", body=b"render-bytes"):
    """Register one render. Uses the shared ``root`` fixture from conftest."""
    f = root / "assets" / f"{name}.png"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(body)
    return artifacts.register(root, name, f, producer="agent:item-1")


class TestDefaultIsAHumanGate:
    def test_registration_lands_as_candidate(self, root):
        """The default must not change: a human still decides."""
        assert artifacts._auto_approve(root) is False
        assert _make(root)["status"] == "candidate"

    def test_agent_may_not_approve(self, root):
        art = _make(root)
        with pytest.raises(PermissionError):
            artifacts.review(root, art["id"], "approved", actor="agent:item-1")

    def test_agent_may_still_reject(self, root):
        """Rejection was never the human's exclusive call, and must stay open."""
        art = _make(root)
        out = artifacts.review(root, art["id"], "rejected", actor="agent:item-1")
        assert out["status"] == "rejected"


class TestSwitchedOn:
    def test_registration_is_approved_not_candidate(self, root):
        """The actual fix: no candidate, so no card."""
        settings.set(root, "art.auto_approve", True)
        assert _make(root)["status"] in ("approved", "integrated")

    def test_agent_may_approve(self, root):
        settings.set(root, "art.auto_approve", False)
        art = _make(root)
        settings.set(root, "art.auto_approve", True)
        out = artifacts.review(root, art["id"], "approved", actor="agent:item-1")
        assert out["status"] in ("approved", "integrated")

    def test_a_failed_auto_approve_still_keeps_the_revision(self, root, monkeypatch):
        """The registration is the thing that matters. Losing it because the
        convenience step raised would be a far worse bug than a stray card."""
        settings.set(root, "art.auto_approve", True)
        monkeypatch.setattr(artifacts, "review",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        art = _make(root)
        assert art["status"] == "candidate"
        assert artifacts.get(root, art["id"])["id"] == art["id"]


class TestFailsClosed:
    def test_unreadable_project_is_not_permission(self, tmp_path):
        """An unknown is not a yes. A registry that cannot be read must leave
        the human gate standing."""
        assert artifacts._auto_approve(tmp_path / "no-such-project") is False
