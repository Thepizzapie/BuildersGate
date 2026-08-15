

def test_the_claude_session_id_is_captured_and_resets_between_runs(tmp_path):
    """A run's session id is what makes it resumable in a terminal.

    Every line the CLI writes carries the same `session_id`, and Claude keeps
    that session's transcript on disk - so the id is the handle for
    `claude --resume`. It was in the log the whole time and nothing read it.

    THE RESET IS THE PART WORTH A TEST. The log APPENDS across re-dispatches, so
    a second run's feed must not carry the first run's session: resuming run 1
    while looking at run 2 opens a transcript that has nothing to do with what
    is on screen.
    """
    import json

    from bgate_ui import dispatch

    root = tmp_path / "proj"
    (root / ".bgate" / "agents").mkdir(parents=True)
    log = root / ".bgate" / "agents" / "item-1.log"

    def write(*lines):
        with log.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")

    write({"type": "bgate_run_start", "item_id": 1},
          {"type": "system", "session_id": "aaaaaaaa-1111"},
          {"type": "assistant",
           "message": {"content": [{"type": "text", "text": "first run"}]}})
    assert dispatch.read_activity(str(root), 1, limit=0)["session_id"] == "aaaaaaaa-1111"

    # A re-dispatch appends to the SAME file with a new session.
    write({"type": "bgate_run_start", "item_id": 1},
          {"type": "system", "session_id": "bbbbbbbb-2222"},
          {"type": "assistant",
           "message": {"content": [{"type": "text", "text": "second run"}]}})
    feed = dispatch.read_activity(str(root), 1, limit=0)
    assert feed["session_id"] == "bbbbbbbb-2222", (
        "the previous run's session survived a re-dispatch")
    assert feed["step_count"] == 1, "the previous run's steps survived too"
