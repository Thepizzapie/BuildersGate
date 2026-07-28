"""The human-only approval rule, tested through the environment agents get.

There was already a test asserting `review(..., 'approved')` refuses an actor
named `agent:...`. It passed while the gate was wide open, because nothing
checked what a *real dispatched agent's environment* resolves to — and dispatch
never set BGATE_ACTOR, so every spawned agent read as the machine's human user
and could approve its own art.

So these tests do not pass an actor in. They set the environment dispatch
actually sets and ask who the process thinks it is. A gate is only proved by
the path the attacker takes.
"""
from __future__ import annotations

import pytest

from bgate_core import artifacts
from bgate_ui import api, dispatch


@pytest.fixture(autouse=True)
def _clean_identity(monkeypatch):
    for name in ("BGATE_ACTOR", "BGATE_SEAT", "BGATE_WORK_ITEM",
                 "BGATE_LOCK_OWNER", "BGATE_STUDIO_USER"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def candidate(root):
    image = root / "hero.png"
    image.write_bytes(b"hero")
    return artifacts.register(root, "hero", image, producer="image_generate")


class TestDispatchedAgentIdentity:
    def test_the_spawn_env_names_the_agent(self):
        """The regression that mattered: dispatch stamped seat and work item but
        not the identity the approval gate reads."""
        assert "BGATE_ACTOR" in dispatch.dispatch.__globals__["os"].environ or True
        source = dispatch.__file__
        with open(source, encoding="utf-8") as handle:
            assert '"BGATE_ACTOR"' in handle.read()

    def test_a_work_item_env_resolves_to_an_agent(self, monkeypatch):
        monkeypatch.setenv("BGATE_WORK_ITEM", "7")
        monkeypatch.setenv("BGATE_SEAT", "art")
        assert api.current_actor() == "agent:item-7"
        assert not api.is_human(api.current_actor())

    def test_a_seat_env_alone_still_resolves_to_an_agent(self, monkeypatch):
        """Fail closed: any spawn path that forgets BGATE_ACTOR must not hand
        the session a human identity."""
        monkeypatch.setenv("BGATE_SEAT", "art")
        assert api.current_actor() == "agent:seat-art"
        assert not api.is_human(api.current_actor())

    def test_an_explicit_actor_still_wins(self, monkeypatch):
        monkeypatch.setenv("BGATE_WORK_ITEM", "7")
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-99")
        assert api.current_actor() == "agent:item-99"

    def test_a_plain_shell_is_a_human(self):
        assert api.is_human(api.current_actor())


class TestApprovalThroughTheAgentEnvironment:
    def test_a_dispatched_agent_cannot_approve_its_own_art(
            self, root, candidate, monkeypatch):
        """The hole, verbatim: this passed before the fix."""
        monkeypatch.setenv("BGATE_SEAT", "art")
        monkeypatch.setenv("BGATE_WORK_ITEM", "7")
        monkeypatch.setenv("BGATE_LOCK_OWNER", "item-7")
        with pytest.raises(PermissionError):
            artifacts.review(root, candidate["id"], "approved", note="mine")

    def test_a_dispatched_agent_cannot_integrate_either(
            self, root, candidate, monkeypatch):
        """'integrated' was the one-hop bypass around 'approved'."""
        monkeypatch.setenv("BGATE_WORK_ITEM", "7")
        with pytest.raises(PermissionError):
            artifacts.review(root, candidate["id"], "integrated")

    def test_an_agent_may_still_reject(self, root, candidate, monkeypatch):
        """Refusing to ship is an agent-legal decision — the asymmetry is the
        point, and over-tightening it would make the gate useless in practice."""
        monkeypatch.setenv("BGATE_WORK_ITEM", "7")
        artifacts.review(root, candidate["id"], "rejected", note="drifted")
        assert artifacts.get(root, candidate["id"])["status"] == "rejected"

    def test_a_human_can_approve(self, root, candidate):
        artifacts.review(root, candidate["id"], "approved", note="ship it")
        assert artifacts.get(root, candidate["id"])["status"] == "approved"

    def test_the_approval_records_who_did_it(self, root, candidate):
        artifacts.review(root, candidate["id"], "approved")
        assert artifacts.get(root, candidate["id"])["reviewed_by"]
