"""The approval-gate selector reaches the cards it claims to control.

THE FIELD REPORT THIS FILE EXISTS FOR. A human set the gate to ``none``, which
the label spells out as "an agent's own word closes its item", and the dashboard
went on drawing APPROVAL cards over every generated candidate and SIGN-OFF cards
over every finished item — each one saying only a human could decide. Observed
across seats, art and director both, which is what ruled out "the art path forgot
to check" and named it one defect at the gate layer.

Why it is worth this much test: a gate that asks anyway has two failure modes and
both are silent. Either work stalls behind a card nobody knew to click — nothing
lands in notify.jsonl, so the board looks exactly like an agent still working —
or the human learns to rubber-stamp, which spends the gate's credibility on the
runs where it IS switched on.

The second half is the inverse bug, found while fixing the first. Under
``builders`` a finished item parks in 'review' (queue.complete), and the sign-off
query asked for 'done' — so the one mode that genuinely mandates a human decision
was the one whose blocked items had no card at all.

These assert through ``_gates`` with a real database rather than through the JS,
because the payload is the contract; the card is a rendering of it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import artifacts, db, gates, queue as _queue


@pytest.fixture()
def client(root, monkeypatch):
    from bgate_ui.app import app

    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


def _gates_for(root, active=None):
    """Call the console's gate list the way /api/console/state does."""
    from bgate_ui.routes import console as _console

    conn = db.connect(root)
    live = {int(r["id"]) for r in conn.execute(
        "SELECT id FROM work_item WHERE status IN ('queued', 'dispatched')")}
    return _console._gates(root, conn, active if active is not None else live)


def _kinds(rows):
    return sorted({r["kind"] for r in rows})


@pytest.fixture()
def finished(root):
    """One maker-seat item an agent has reported done."""
    item = _queue.add(root, "art", title="paint the heron",
                      brief="one heron, full body")
    _queue.complete(root, item["id"], result="painted it")
    return _queue.get(root, item["id"])


@pytest.fixture()
def candidate(root):
    """One generated candidate hanging off a still-running item."""
    item = _queue.add(root, "art", title="fox plate", brief="a fox")
    _queue.set_status(root, item["id"], "dispatched")
    image = root / "fox.png"
    image.write_bytes(b"fox-bytes")
    art = artifacts.register(root, "species_red_fox", image,
                             producer="image_generate",
                             work_item_id=item["id"])
    return item, art


class TestGateNone:
    """'none' is a sentence: do not stop to ask me."""

    def test_no_signoff_card_for_a_finished_item(self, root, finished):
        gates.set_mode(root, gates.NONE)
        assert "signoff" not in _kinds(_gates_for(root))

    def test_no_approval_card_for_a_candidate(self, root):
        gates.set_mode(root, gates.NONE)
        item = _queue.add(root, "art", title="fox plate", brief="a fox")
        _queue.set_status(root, item["id"], "dispatched")
        image = root / "fox.png"
        image.write_bytes(b"fox-bytes")
        artifacts.register(root, "species_red_fox", image,
                           producer="image_generate", work_item_id=item["id"])
        assert "art" not in _kinds(_gates_for(root))

    def test_the_candidate_is_approved_not_merely_hidden(self, root):
        """THE HALF THAT MATTERS MOST. Suppressing the card while leaving the
        revision a candidate would be the worse bug: the live path still holds
        the previous image and the only surface that said so is the one just
        hidden. A gate that is off is off at the decision."""
        gates.set_mode(root, gates.NONE)
        image = root / "fox.png"
        image.write_bytes(b"fox-bytes")
        art = artifacts.register(root, "species_red_fox", image,
                                 producer="image_generate")
        assert art["status"] in ("approved", "integrated")

    def test_a_queued_qa_item_still_shows(self, root):
        """The control. 'none' suppresses ASKING, not the board — a qa-gate row
        is a real work item somebody filed, and hiding a queued agent would be a
        different lie told by the same fix."""
        gates.set_mode(root, gates.NONE)
        item = _queue.add(root, "qa", title="verify the heron",
                          brief="check it", source="qa-gate")
        assert [r for r in _gates_for(root, active={item["id"]})
                if r["kind"] == "qa"]


class TestGateAgent:
    """The default. A machine verifies the claim; the human is not the reviewer."""

    def test_no_signoff_card(self, root, finished):
        """The QA seat already reviewed it — a second human ask buys nothing."""
        gates.set_mode(root, gates.AGENT)
        assert "signoff" not in _kinds(_gates_for(root))

    def test_the_candidate_still_needs_a_human(self, root, candidate):
        """Deliberately NOT extended to art: an agent's verdict leaves the
        revision a candidate, because an agent approving art is the drift the
        art-QA router exists to stop."""
        gates.set_mode(root, gates.AGENT)
        item, art = candidate
        assert art["status"] == "candidate"
        assert "art" in _kinds(_gates_for(root, active={item["id"]}))


class TestGateBuilders:
    """The human approves, and the card has to exist for them to do it."""

    def test_signoff_card_for_a_finished_item(self, root, finished):
        gates.set_mode(root, gates.BUILDERS)
        assert "signoff" in _kinds(_gates_for(root))

    def test_an_item_parked_in_review_gets_a_card(self, root):
        """The inverse bug. holds_for_human parks a completion in 'review' and
        the query asked for 'done', so the one gate this mode mandates drew
        nothing — the chain behind it stopped with no card saying why."""
        gates.set_mode(root, gates.BUILDERS)
        item = _queue.add(root, "tech", title="scatter pass", brief="scatter")
        done = _queue.complete(root, item["id"], result="scattered 850")
        assert done["status"] == "review"          # the precondition, asserted
        rows = [r for r in _gates_for(root) if r["kind"] == "signoff"]
        assert [r for r in rows if r["item_id"] == item["id"] and r["parked"]]

    def test_a_parked_item_is_never_aged_out(self, root, monkeypatch):
        """'review' is a stopped chain, not a claim worth a glance. Ageing it out
        of the window would hide the block and leave the work behind it queued
        forever with nothing on screen explaining it."""
        gates.set_mode(root, gates.BUILDERS)
        item = _queue.add(root, "tech", title="scatter pass", brief="scatter")
        _queue.complete(root, item["id"], result="scattered 850")
        from bgate_ui.routes import console as _console
        monkeypatch.setattr(_console, "_signoff_hours", lambda r: 0.25)
        db.connect(root).execute(
            "UPDATE work_item SET updated_at = '2001-01-01 00:00:00' "
            "WHERE id = ?", (item["id"],))
        db.connect(root).commit()
        rows = [r for r in _gates_for(root) if r["kind"] == "signoff"]
        assert [r for r in rows if r["item_id"] == item["id"]]

    def test_an_acked_item_drops_off(self, root, finished):
        """Unchanged behaviour, pinned so the rewritten query keeps it: the
        acknowledgement is what makes this a gate rather than a backlog."""
        gates.set_mode(root, gates.BUILDERS)
        from bgate_ui.routes import console as _console
        from bgate_core import workspace as _ws

        _ws.set(root, _console.SEAT, _console.SIGNOFF_KEY,
                {"acked": {str(finished["id"]): {"verdict": "accept"}}})
        rows = [r for r in _gates_for(root) if r["kind"] == "signoff"]
        assert not [r for r in rows if r["item_id"] == finished["id"]]


class TestActingOnAParkedItem:
    """A card that appears has to be a card that works.

    Putting 'review' items in the sign-off list is only half a fix: the accept
    button acked into a workspace doc, which would have cleared the CARD and left
    the CHAIN stopped — the gate switched off at the drawing rather than at the
    decision, which is the exact mistake being fixed one layer up.
    """

    @pytest.fixture()
    def parked(self, root):
        gates.set_mode(root, gates.BUILDERS)
        item = _queue.add(root, "tech", title="scatter pass", brief="scatter")
        _queue.complete(root, item["id"], result="scattered 850")
        return _queue.get(root, item["id"])

    def test_accept_releases_the_chain(self, client, root, parked):
        got = client.post("/api/console/signoff",
                          json={"item_id": parked["id"], "verdict": "accept"})
        assert got.status_code == 200
        assert got.json()["released"] is True
        assert _queue.get(root, parked["id"])["status"] == "done"

    def test_accept_records_who_signed(self, client, root, parked):
        """An approval nobody signed is not an approval."""
        client.post("/api/console/signoff",
                    json={"item_id": parked["id"], "verdict": "accept"})
        assert _queue.get(root, parked["id"])["approved_by"]

    def test_send_back_requeues_with_the_reason_in_the_brief(self, client, root,
                                                             parked):
        got = client.post("/api/console/signoff",
                          json={"item_id": parked["id"], "verdict": "reopen",
                                "reason": "the legs are cropped"})
        assert got.status_code == 200
        after = _queue.get(root, parked["id"])
        assert after["status"] == "queued"
        assert "the legs are cropped" in after["brief"]

    def test_a_finished_item_is_still_only_acked(self, client, root):
        """The control: an item that is genuinely 'done' must NOT be run through
        approve() — it has no chain to release and approve() refuses anything
        that is not parked. The two paths have to stay distinguishable."""
        gates.set_mode(root, gates.BUILDERS)
        item = _queue.add(root, "art", title="paint", brief="paint")
        _queue.complete(root, item["id"], result="painted", skip_gate=True)
        _queue.set_status(root, item["id"], "done")
        got = client.post("/api/console/signoff",
                          json={"item_id": item["id"], "verdict": "accept"})
        assert got.status_code == 200
        assert got.json()["released"] is False
        assert not [r for r in _gates_for(root) if r["kind"] == "signoff"
                    and r["item_id"] == item["id"]]


class TestDegradation:
    """A gate the board cannot READ must not blank the board.

    The mode is now consulted on every poll, which means an unreadable registry
    is on the critical path of drawing the graph at all. It must degrade to
    DEFAULT — 'agent', the behaviour that shipped before the setting existed —
    rather than to silence, because silence here means a candidate awaiting a
    human with no card, which is the bug this whole file is about arriving from
    the opposite direction.
    """

    @pytest.fixture()
    def unreadable(self, monkeypatch):
        monkeypatch.setattr(gates, "mode",
                            lambda r: (_ for _ in ()).throw(RuntimeError("db")))

    def test_it_does_not_raise(self, root, candidate, unreadable):
        _gates_for(root)

    def test_it_falls_back_to_the_agent_gate(self, root, candidate, unreadable):
        """Not to 'none'. Failing open would hide a pending human decision on
        exactly the poll where something is already wrong."""
        item, art = candidate
        assert "art" in _kinds(_gates_for(root, active={item["id"]}))
