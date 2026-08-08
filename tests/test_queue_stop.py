"""A stop is not a crash, and the difference has to survive without a reader.

THE INCIDENT. Three items — #3 art, #13 tech, #14 director — flipped to
``status: failed`` in the same second. Read as three separate bugs. They were one
event: a human had pressed STOP ALL. The system said so, honestly, in the result
note ("stopped by Adrian — this run was ended by hand, it did not die on its
own") and nowhere else, so telling a kill from a crash meant reading English out
of a prose field. Same-second multi-seat failure is the signature of a systemic
event; nothing could see it.

WHAT THIS DOES NOT DO, and the tests below pin it deliberately: it does not add a
sixth status. 'failed' is keyed on in ~85 places — reopen()'s guard, the QA gate
query, the chain interlock, the console lanes — and a new status changes
behaviour in every one that filters by name, by omission and silently. The status
was never the missing thing. The CAUSE was, and a cause is its own column.

The other half is the heartbeat: a burst of identical 'failed' lines during a
STOP ALL is exactly as unreadable on the stream as it was in the database.
"""
from __future__ import annotations

import json

import pytest

from bgate_core import events, queue as _queue


def _notify_lines(root):
    path = root / ".bgate" / "notify.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture()
def running(root):
    item = _queue.add(root, "tech", title="scatter pass", brief="scatter it")
    _queue.set_status(root, item["id"], "dispatched")
    return _queue.get(root, item["id"])


class TestTheStopIsMachineReadable:
    def test_stopped_by_names_the_person(self, root, running):
        after = _queue.stop(root, running["id"], by="Adrian")
        assert after["stopped_by"] == "Adrian"

    def test_was_stopped_answers_without_reading_prose(self, root, running):
        assert _queue.was_stopped(_queue.get(root, running["id"])) is False
        _queue.stop(root, running["id"], by="Adrian")
        assert _queue.was_stopped(_queue.get(root, running["id"])) is True

    def test_a_real_failure_is_not_mistaken_for_a_stop(self, root, running):
        """The control. Without it, a field that is always truthy would pass
        every assertion above and distinguish nothing."""
        _queue.complete(root, running["id"], result="the build never linked",
                        failed=True)
        item = _queue.get(root, running["id"])
        assert item["status"] == "failed"
        assert _queue.was_stopped(item) is False

    def test_the_time_is_recorded(self, root, running):
        assert _queue.stop(root, running["id"], by="Adrian")["stopped_at"]


class TestTheStatusIsDeliberatelyStillFailed:
    def test_it_banks_as_failed(self, root, running):
        assert _queue.stop(root, running["id"], by="Adrian")["status"] == "failed"

    def test_a_stopped_item_can_still_be_reopened(self, root, running):
        """The reason for not inventing a status. A stopped run is usually 90%
        landed and IS worth another round — reopen() guards on
        done/failed/cancelled, and a 'stopped' status would have failed that
        guard with no error anyone wrote."""
        _queue.stop(root, running["id"], by="Adrian")
        back = _queue.reopen(root, running["id"], "finish the scatter pass")
        assert back["status"] == "queued"

    def test_the_stop_record_survives_the_reopen(self, root, running):
        """stopped_at is separate from updated_at so a re-run item still says it
        was stopped once, three rounds ago, rather than claiming it just now."""
        _queue.stop(root, running["id"], by="Adrian")
        _queue.reopen(root, running["id"], "finish it")
        assert _queue.get(root, running["id"])["stopped_by"] == "Adrian"


class TestTheHeartbeatSaysStop:
    def test_the_stream_carries_a_stopped_line(self, root, running):
        _queue.stop(root, running["id"], by="Adrian")
        assert [ln for ln in _notify_lines(root)
                if ln.get("item_id") == running["id"]
                and ln.get("status") == "stopped"]

    def test_the_bus_carries_an_item_stopped_event(self, root, running):
        _queue.stop(root, running["id"], by="Adrian")
        kinds = [e["kind"] for e in events.since(root, 0)["events"]]
        assert "item.stopped" in kinds

    def test_the_event_names_who(self, root, running):
        _queue.stop(root, running["id"], by="Adrian")
        stopped = [e for e in events.since(root, 0)["events"]
                   if e["kind"] == "item.stopped"][-1]
        assert stopped["payload"]["by"] == "Adrian"

    def test_the_kind_is_selectable_in_the_settings_panel(self):
        """A kind emitted but absent from settings.EVENT_KINDS has no checkbox,
        so it can never be added to notify.kinds and coerce() REFUSES it — which
        is how chain.filed ended up emitted and unselectable at the same time.
        (test_events pins the two lists equal in general; this pins the one kind
        added here, so the failure names itself.)"""
        from bgate_core import settings

        assert "item.stopped" in events.KINDS
        assert "item.stopped" in settings.EVENT_KINDS


class TestFallback:
    def test_an_unnamed_stopper_still_records_something(self, root, running):
        """An anonymous stop is still a stop; recording nothing would put it back
        in the crash bucket, which is the whole bug."""
        assert _queue.stop(root, running["id"])["stopped_by"]
