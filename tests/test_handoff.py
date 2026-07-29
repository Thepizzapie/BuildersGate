"""The project thread — in-flight state that survives a session's death.

Two properties matter more than the happy path. It must be APPEND-ONLY and
crash-tolerant, because the sessions worth resuming are the ones that were killed
mid-flight and never ran a summary step. And it must stay in its lane: the bible
owns settled decisions and the queue owns dispatched work, so a thread that grows
its own copies of those is a third store that will drift from both.
"""
from __future__ import annotations

import io
import json
import sys

import pytest

from bgate_core import handoff
from bgate_cli import session


class TestNote:
    def test_appends_and_reads_back_in_order(self, root):
        handoff.note(root, "state", "first")
        handoff.note(root, "next", "second")
        trail = handoff.read(root)
        assert [r["kind"] for r in trail] == ["state", "next"]
        assert [r["text"] for r in trail] == ["first", "second"]

    def test_nothing_is_ever_rewritten(self, root):
        """Append-only is the whole design: a summary written at the end is a
        summary a killed session never writes."""
        handoff.note(root, "state", "one")
        first = handoff.path_for(root).read_text(encoding="utf-8")
        handoff.note(root, "state", "two")
        after = handoff.path_for(root).read_text(encoding="utf-8")
        assert after.startswith(first)
        assert after.count("\n") == 2

    def test_flushed_per_line_so_a_kill_keeps_what_landed(self, root):
        """No buffering: the last note before a crash is the important one."""
        handoff.note(root, "next", "survive me")
        assert "survive me" in handoff.path_for(root).read_text(encoding="utf-8")

    def test_rejects_an_unknown_kind(self, root):
        with pytest.raises(ValueError, match="unknown kind"):
            handoff.note(root, "vibes", "x")

    def test_rejects_empty_text(self, root):
        with pytest.raises(ValueError):
            handoff.note(root, "state", "   ")

    def test_refs_and_actor_are_recorded(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-7")
        rec = handoff.note(root, "decision", "chose X", refs=["bible#3", "item 41"])
        assert rec["refs"] == ["bible#3", "item 41"]
        # dispatch.py stamps BGATE_ACTOR, so a seat worker's notes are
        # attributable without the caller passing anything.
        assert rec["actor"] == "agent:item-7"

    def test_actor_defaults_to_director(self, root, monkeypatch):
        monkeypatch.delenv("BGATE_ACTOR", raising=False)
        assert handoff.note(root, "state", "x")["actor"] == "director"

    def test_text_and_refs_are_bounded(self, root):
        rec = handoff.note(root, "state", "x" * 5000, refs=[str(i) for i in range(50)])
        assert len(rec["text"]) == handoff.MAX_TEXT
        assert len(rec["refs"]) == handoff.MAX_REFS


class TestRead:
    def test_missing_thread_is_empty_not_an_error(self, root):
        assert handoff.read(root) == []
        assert handoff.digest(root) == []

    def test_a_half_written_final_line_is_skipped_not_fatal(self, root):
        """The process died mid-write. Losing one note must not cost the rest."""
        handoff.note(root, "state", "good one")
        with handoff.path_for(root).open("a", encoding="utf-8") as fh:
            fh.write('{"kind": "state", "text": "trunca')
        trail = handoff.read(root)
        assert len(trail) == 1 and trail[0]["text"] == "good one"

    def test_limit_takes_the_most_recent(self, root):
        for i in range(5):
            handoff.note(root, "state", f"n{i}")
        assert [r["text"] for r in handoff.read(root, limit=2)] == ["n3", "n4"]

    def test_kind_filter(self, root):
        handoff.note(root, "state", "s")
        handoff.note(root, "deferred", "d")
        got = handoff.read(root, kind="deferred")
        assert len(got) == 1 and got[0]["text"] == "d"

    def test_concurrent_writers_interleave_without_loss(self, root):
        """One thread per project, not per session, precisely so this is safe.

        The per-session design needed the MCP server and the hooks to agree on
        what "this session" is; they are separate processes and the harness only
        hands a session_id to hooks. Append-only lines need no such agreement.
        """
        for i in range(20):
            handoff.note(root, "state", f"a{i}", actor="director")
            handoff.note(root, "state", f"b{i}", actor="agent:item-1")
        trail = handoff.read(root)
        assert len(trail) == 40
        assert sum(1 for r in trail if r["actor"] == "agent:item-1") == 20


class TestDigest:
    def test_is_bounded_because_it_lands_in_every_session(self, root):
        for i in range(40):
            handoff.note(root, "state", f"n{i}")
        lines = handoff.digest(root)
        # header + at most DIGEST_NOTES entries
        assert len(lines) == handoff.DIGEST_NOTES + 1
        assert "of 40 note(s)" in lines[0]
        assert "handoff_read" in lines[0]      # says how to get the rest

    def test_keeps_chronological_order(self, root):
        """The thread is a narrative. Bucketing by kind loses which decision came
        before which deferral, which is the only thing a narrative adds."""
        handoff.note(root, "decision", "first")
        handoff.note(root, "deferred", "second")
        handoff.note(root, "next", "third")
        body = "\n".join(handoff.digest(root))
        assert body.index("first") < body.index("second") < body.index("third")

    def test_non_director_notes_are_attributed(self, root):
        handoff.note(root, "state", "mine", actor="director")
        handoff.note(root, "state", "theirs", actor="agent:item-9")
        body = "\n".join(handoff.digest(root))
        assert "<agent:item-9>" in body
        assert "<director>" not in body        # the default is noise, not signal


class TestSessionStartIntegration:
    def test_the_thread_reaches_a_new_session(self, root, monkeypatch):
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: False)
        handoff.note(root, "deferred",
                     "archive drawer pays nothing on purpose; do not 'fix' it",
                     refs=["docs/design_tutorial_floor.md"])
        text = session.build_context(str(root))
        assert "THREAD" in text
        assert "do not 'fix' it" in text
        assert "docs/design_tutorial_floor.md" in text

    def test_a_broken_thread_does_not_break_the_hook(self, root, monkeypatch):
        """SessionStart runs once, before the first turn. A crash here is a
        session that will not start."""
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: False)
        monkeypatch.setattr(handoff, "digest",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        text = session.build_context(str(root))
        assert text and "BOARD" in text      # the rest of the block survives
        assert "THREAD" not in text

    def test_no_thread_means_no_block(self, root, monkeypatch):
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: False)
        assert "THREAD" not in session.build_context(str(root))
