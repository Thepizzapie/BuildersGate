"""The event bus — against a real database, because that is the point.

This module was written and shipped without a single statement of its SQL ever
being executed: the migration was applied once to a scratch DB (which is what
caught an index-name collision with 0002) and nothing else ran. Every query
below is therefore a first execution, and the tests are ordered by what would
hurt most if it were wrong — the cursor, the gap flag, and the promise that a
failed emit can never take down the transition that caused it.

The first draft of this bus was a JSONL file with a byte cursor. It could not
work: `queue_complete` executes in the MCP server process while `_reap` executes
in the dashboard's, so the log is multi-writer across processes and a monotonic
sequence in an appended file needs a lock that does not exist here. The table is
the fix, and `test_two_writers_never_collide` is what pins it.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from bgate_core.store import db, events


def _ids(batch) -> list:
    return [e["id"] for e in batch["events"]]


class TestEmit:
    def test_an_event_round_trips_with_its_payload_parsed(self, root):
        seq = events.emit(root, "item.done", ref="41", payload={"item": 41, "seat": "art"})
        assert seq > 0
        got = events.since(root)["events"][0]
        assert got["kind"] == "item.done" and got["ref"] == "41"
        assert got["payload"] == {"item": 41, "seat": "art"}   # dict, not a string
        assert got["created_at"]

    def test_ids_are_monotonic(self, root):
        made = [events.emit(root, "item.done", ref=str(i)) for i in range(5)]
        assert made == sorted(made) and len(set(made)) == 5

    def test_a_broken_emit_returns_zero_instead_of_raising(self, root, monkeypatch):
        """THE RULE THIS MODULE LIVES BY. emit() is called from inside
        queue.complete(); an exception here would turn a bookkeeping failure into
        a work item that never got its status."""
        def boom(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(db, "tx", boom)
        assert events.emit(root, "item.done", ref="1") == 0

    def test_a_non_dict_payload_is_wrapped_rather_than_lost(self, root):
        events.emit(root, "item.done", ref="1", payload=["a", "b"])
        assert events.since(root)["events"][0]["payload"] == {"value": ["a", "b"]}

    def test_an_oversized_payload_is_truncated_not_corrupted(self, root):
        events.emit(root, "item.done", ref="1",
                    payload={"result": "x" * (events.MAX_PAYLOAD * 2)})
        got = events.since(root)["events"][0]["payload"]
        assert got.get("_truncated") is True and got.get("chars")
        # Still a dict a consumer can read, not a half-written JSON string.
        assert isinstance(got, dict)

    def test_unparseable_stored_json_reads_as_empty_not_as_a_crash(self, root):
        events.emit(root, "item.done", ref="1", payload={"ok": 1})
        with db.tx(root) as conn:
            conn.execute("UPDATE event SET payload = ?", ("{not json",))
        assert events.since(root)["events"][0]["payload"] == {}


class TestSince:
    def test_the_cursor_advances_only_over_what_was_returned(self, root):
        for i in range(3):
            events.emit(root, "item.done", ref=str(i))
        first = events.since(root, 0, limit=2)
        assert len(first["events"]) == 2 and first["more"] is True
        assert first["seq"] == first["events"][-1]["id"]
        second = events.since(root, first["seq"])
        assert len(second["events"]) == 1 and second["more"] is False

    def test_an_empty_batch_leaves_the_cursor_where_it_was(self, root):
        seq = events.emit(root, "item.done", ref="1")
        assert events.since(root, seq)["seq"] == seq

    def test_a_filtered_read_never_skips_past_what_it_filtered_out(self, root):
        """The trap: advancing to the table head on a filtered read means a
        consumer that later widens its filter silently loses everything that
        happened while it was narrow."""
        wanted = events.emit(root, "item.done", ref="1")
        events.emit(root, "agent.spawned", ref="2")
        batch = events.since(root, 0, kinds=("item.done",))
        assert _ids(batch) == [wanted]
        assert batch["seq"] == wanted          # NOT the agent.spawned id
        assert events.since(root, batch["seq"])["events"][0]["kind"] == "agent.spawned"

    def test_head_is_the_newest_id_regardless_of_the_filter(self, root):
        events.emit(root, "item.done", ref="1")
        newest = events.emit(root, "agent.spawned", ref="2")
        assert events.since(root, 0, kinds=("item.done",))["head"] == newest

    def test_a_pruned_range_is_reported_as_a_gap(self, root):
        """"You missed 40 events" and "nothing happened" must never look the
        same to a subscriber."""
        seen = events.emit(root, "item.done", ref="1")     # the consumer's cursor
        missed = events.emit(root, "item.done", ref="2")    # pruned out from under it
        events.emit(root, "item.done", ref="3")
        # Modelled the way prune() actually works — oldest first — because that
        # is the only shape that can strand a cursor. `gap` is computed from the
        # log's MIN(id), so a hand-carved hole in the middle would not be seen;
        # nothing in the tree makes one.
        with db.tx(root) as conn:
            conn.execute("DELETE FROM event WHERE id <= ?", (missed,))
        assert events.since(root, seen)["gap"] is True

    def test_a_fresh_subscriber_is_not_a_gap(self, root):
        """seq<=0 has not LOST anything — flagging it puts a permanent 'events
        dropped' warning on every new project."""
        events.emit(root, "item.done", ref="1")
        assert events.since(root, 0)["gap"] is False

    def test_sitting_on_the_oldest_surviving_id_is_not_a_gap(self, root):
        first = events.emit(root, "item.done", ref="1")
        events.emit(root, "item.done", ref="2")
        assert events.since(root, first)["gap"] is False

    def test_limit_is_clamped_rather_than_trusted(self, root):
        events.emit(root, "item.done", ref="1")
        assert events.since(root, 0, limit=10_000_000)["events"]
        assert events.since(root, 0, limit=0)["events"]

    def test_an_unreadable_log_reads_as_nothing_new(self, root, monkeypatch):
        monkeypatch.setattr(db, "connect",
                            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
        got = events.since(root, 0)
        assert got["events"] == [] and got["seq"] == 0


class TestCursors:
    def test_a_cursor_survives_and_is_per_consumer(self, root):
        assert events.cursor_get(root, "followup") == 0     # never run
        events.cursor_set(root, "followup", 12)
        events.cursor_set(root, "ui", 4)
        assert events.cursor_get(root, "followup") == 12
        assert events.cursor_get(root, "ui") == 4

    def test_cursors_lists_every_subscriber(self, root):
        events.cursor_set(root, "followup", 3)
        events.cursor_set(root, "ui", 9)
        got = {c["consumer"]: c["seq"] for c in events.cursors(root)}
        assert got["followup"] == 3 and got["ui"] == 9

    def test_a_cursor_write_that_fails_does_not_raise(self, root, monkeypatch):
        from bgate_core.store import workspace as _ws
        monkeypatch.setattr(_ws, "set",
                            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
        events.cursor_set(root, "followup", 5)     # must not raise


class TestPrune:
    def test_recent_events_survive_and_old_ones_do_not(self, root):
        keep = events.emit(root, "item.done", ref="new")
        old = events.emit(root, "item.done", ref="old")
        with db.tx(root) as conn:
            conn.execute("UPDATE event SET created_at = datetime('now', '-40 days') "
                         "WHERE id = ?", (old,))
        assert events.prune(root, keep_days=14) == 1
        assert _ids(events.since(root, 0)) == [keep]

    def test_keep_days_is_floored_so_a_zero_cannot_empty_the_log(self, root):
        events.emit(root, "item.done", ref="1")
        events.prune(root, keep_days=0)
        assert events.since(root, 0)["events"], "prune(0) wiped today's events"


class TestMultiWriter:
    def test_two_writers_never_collide(self, root):
        """WHY THIS IS A TABLE AND NOT A FILE. The dashboard and every MCP server
        write these; an appended JSONL cannot hand out a monotonic sequence
        across processes without a lock. Two connections here stand in for two
        processes — the ids must be distinct and ordered."""
        first = sqlite3.connect(str(db.db_path(root)))
        second = sqlite3.connect(str(db.db_path(root)))
        try:
            made = []
            for i in range(6):
                conn = first if i % 2 == 0 else second
                cur = conn.execute(
                    "INSERT INTO event (kind, ref, actor, payload) VALUES (?,?,?,?)",
                    ("item.done", str(i), "test", json.dumps({"i": i})))
                conn.commit()
                made.append(int(cur.lastrowid))
        finally:
            first.close(); second.close()
        assert len(set(made)) == 6 and made == sorted(made)
        assert _ids(events.since(root, 0)) == made


class TestVocabulary:
    def test_every_kind_the_tree_emits_is_declared(self):
        """A kind missing from KINDS is a kind the settings validator refuses,
        which is how `chain.filed` became unselectable in the notification list
        while the queue was emitting it."""
        from bgate_core.store import settings
        assert set(settings.EVENT_KINDS) == set(events.KINDS)


@pytest.fixture()
def _quiet_actor(monkeypatch):
    monkeypatch.delenv("BGATE_ACTOR", raising=False)
    return None
