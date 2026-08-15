"""The Claude session behind a run, and handing it back to a terminal.

Every dispatched agent IS a Claude Code session, and Claude keeps that session's
transcript on disk - so a run this dashboard draws as cards can be picked up in
a terminal exactly where it left off. The id was in the agent's log the whole
time and nothing read it out.
"""
from __future__ import annotations

import json

from bgate_ui import dispatch
from bgate_ui.routes.agent_session import _project_slug, _transcript


def _write(log, *events):
    with log.open("a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def test_the_session_id_is_captured_from_the_log(tmp_path):
    root = tmp_path / "proj"
    (root / ".bgate" / "agents").mkdir(parents=True)
    log = root / ".bgate" / "agents" / "item-1.log"

    _write(log,
           {"type": "bgate_run_start", "item_id": 1},
           {"type": "system", "session_id": "aaaaaaaa-1111"},
           {"type": "assistant",
            "message": {"content": [{"type": "text", "text": "first run"}]}})

    feed = dispatch.read_activity(str(root), 1, limit=0)
    assert feed["session_id"] == "aaaaaaaa-1111"


def test_a_redispatch_does_not_inherit_the_previous_session(tmp_path):
    """THE RESET IS THE PART WORTH A TEST.

    The log APPENDS across re-dispatches, so a second run's feed must not carry
    the first run's session: resuming run 1 while looking at run 2 opens a
    transcript that has nothing to do with what is on screen.
    """
    root = tmp_path / "proj"
    (root / ".bgate" / "agents").mkdir(parents=True)
    log = root / ".bgate" / "agents" / "item-2.log"

    _write(log,
           {"type": "bgate_run_start", "item_id": 2},
           {"type": "system", "session_id": "aaaaaaaa-1111"},
           {"type": "assistant",
            "message": {"content": [{"type": "text", "text": "first"}]}})
    assert dispatch.read_activity(str(root), 2, limit=0)["session_id"] == "aaaaaaaa-1111"

    _write(log,
           {"type": "bgate_run_start", "item_id": 2},
           {"type": "system", "session_id": "bbbbbbbb-2222"},
           {"type": "assistant",
            "message": {"content": [{"type": "text", "text": "second"}]}})

    feed = dispatch.read_activity(str(root), 2, limit=0)
    assert feed["session_id"] == "bbbbbbbb-2222", (
        "the previous run's session survived a re-dispatch")
    assert feed["step_count"] == 1, "the previous run's steps survived too"


def test_a_run_with_no_session_reports_none_rather_than_guessing(tmp_path):
    root = tmp_path / "proj"
    (root / ".bgate" / "agents").mkdir(parents=True)
    log = root / ".bgate" / "agents" / "item-3.log"
    _write(log, {"type": "bgate_run_start", "item_id": 3})

    assert dispatch.read_activity(str(root), 3, limit=0)["session_id"] == ""


def test_the_project_slug_matches_claude_codes_own_scheme():
    """Claude stores transcripts under a slugged absolute path.

    Every character that is not a letter, digit or dash becomes a dash - drive
    letter, colon and both separators all collapse. Asserted because it is a
    guess about somebody else's scheme, and a wrong guess silently reports every
    session as unresumable.
    """
    assert _project_slug(r"C:\Users\adria\Desktop\bg-testbed") == \
        "C--Users-adria-Desktop-bg-testbed"
    assert _project_slug("/home/x/games/thing") == "-home-x-games-thing"


def test_a_missing_transcript_is_not_offered_as_resumable(tmp_path):
    """Offering a resume for a transcript Claude has cleaned up is a command
    that fails in the user's terminal quoting an id they have never seen."""
    assert _transcript(tmp_path, "") is None
    assert _transcript(tmp_path, "no-such-session-0000") is None
