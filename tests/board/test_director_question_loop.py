"""EXIT 67 gripe 39b — a director question is now a QUESTION, not a whisper.

Before this, ``ask_director`` wrote straight into the director session's
stdin and recorded nothing: ``pending_decisions``, ``board_digest`` and the
stale-question reminder had no idea it had ever happened, so a director that
was mid-thought (or simply not there) left an asker waiting on an answer no
surface showed as pending. This records the question as an ordinary
QUESTION_KIND event first, exposes it (with age) as its own list, and
escalates it to the human if the director stays silent past the window —
same idempotency discipline as the general ``remind_stale``.
"""
from __future__ import annotations

import pytest

from bgate_core.board import queue, steerbox
from bgate_core.store import db


@pytest.fixture(autouse=True)
def _director_is_live(monkeypatch):
    """Fake a live director session without spawning anything real."""
    from bgate_ui.agents import directorsession

    monkeypatch.setattr(directorsession, "status",
                        lambda root: {"running": True, "cli_session_id": "x"})
    sent = []
    monkeypatch.setattr(directorsession, "send",
                        lambda root, text: sent.append(text) or {"ok": True})
    return sent


def _age_it(root, seq: int, minutes: float) -> None:
    """Backdate a question's event row so it reads as ``minutes`` old,
    without sleeping the test."""
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE event SET created_at = datetime('now', ?) WHERE id = ?",
            (f"-{minutes} minutes", int(seq)))


def test_ask_director_records_a_question_and_delivers_it_live(root, _director_is_live):
    filed = steerbox.ask_director(root, "which palette for the boss?", by="art")
    assert filed["ok"] and filed["live"]
    assert _director_is_live and "which palette" in _director_is_live[0]

    listed = steerbox.questions_for_director(root)
    assert any(q["event_seq"] == filed["seq"] for q in listed)


def test_a_question_older_than_ten_minutes_escalates_exactly_once(root):
    filed = steerbox.ask_director(root, "hub weather: yes or no?", by="art")
    _age_it(root, filed["seq"], 15)

    first = steerbox.remind_stale_directors(root)
    assert [q["event_seq"] for q in first] == [filed["seq"]]

    second = steerbox.remind_stale_directors(root)
    assert second == [], "a question already reminded must not fire twice"


def test_a_fresh_question_does_not_escalate(root):
    filed = steerbox.ask_director(root, "still fresh", by="art")
    assert steerbox.remind_stale_directors(root) == []
    # and it is still open, not silently dropped
    assert any(q["event_seq"] == filed["seq"]
               for q in steerbox.questions_for_director(root))


def test_an_answer_to_an_escalated_question_still_reaches_a_live_asker(root):
    item = queue.add(root, "art", "boss palette", "brief")
    assert queue.reserve(root, item["id"])   # -> dispatched, i.e. "still running"

    filed = steerbox.ask_director(root, "which palette?", item_id=item["id"], by="art")
    _age_it(root, filed["seq"], 15)
    steerbox.remind_stale_directors(root)    # escalated; must not change routing

    answered = steerbox.answer(root, filed["seq"], "use the pinned autumn ref",
                               by="human")
    assert answered["route"] == "steer"
    assert answered["delivered"] is True

    pending = steerbox.pending(root, item["id"])
    assert any("autumn ref" in p["text"] for p in pending)

    # and the question itself is closed — a second answer is refused, not
    # silently accepted as a contradiction of the first.
    with pytest.raises(steerbox.AlreadyAnswered):
        steerbox.answer(root, filed["seq"], "something else", by="human")
