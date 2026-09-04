"""The console's director session, exercised against a FAKE claude binary.

Same approach as test_dispatch_lifecycle and for the same reason: the CLI
contract is small (argv flags, a stream-json user turn on stdin, NDJSON events
back, a `result` per turn), so a ~30-line python program that honours it makes
spawn -> turn -> settle -> resume-fallback deterministic and offline.

The fake differs from the dispatch one in the one way that matters here: it
answers with a `result` event PER TURN and keeps reading stdin, because the
director session is a conversation holding its pipe open — not an errand that
reports once at EOF.
"""
from __future__ import annotations

import json
import sys
import time

import pytest

from bgate_core.board import queue
from bgate_core.store import settings
from bgate_ui.agents import codexmeta, directorsession, runners

FAKE_SESSION_CLI = r'''
import json, os, sys

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

argv = sys.argv[1:]
probe = os.environ.get("BGATE_FAKE_PROBE")
if probe:
    with open(probe, "w", encoding="utf-8") as fh:
        json.dump({"argv": argv, "cwd": os.getcwd(),
                   "seat": os.environ.get("BGATE_SEAT"),
                   "root": os.environ.get("BGATE_ROOT"),
                   "actor": os.environ.get("BGATE_ACTOR")}, fh)
if "--resume" in argv and os.environ.get("BGATE_FAKE_RESUME_DIES"):
    sys.exit(1)
emit({"type": "system", "subtype": "init", "session_id": "fake-session-1"})
while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    if ev.get("type") != "user":
        continue
    blocks = ev.get("message", {}).get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "thinking about: " + text[:60]}]}})
    emit({"type": "result", "subtype": "success",
          "result": "reply to: " + text[:200], "total_cost_usd": 0.01,
          "usage": {"input_tokens": 10, "output_tokens": 5}})
'''


@pytest.fixture()
def fake_claude(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("fakecli")
    script = d / "fake_claude.py"
    script.write_text(FAKE_SESSION_CLI, encoding="utf-8")
    probe = d / "probe.json"
    if sys.platform == "win32":
        exe = d / "claude.bat"
        exe.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                       encoding="utf-8")
    else:
        exe = d / "claude"
        exe.write_text(f"#!{sys.executable}\n{FAKE_SESSION_CLI}",
                       encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("BGATE_FAKE_PROBE", str(probe))
    monkeypatch.setattr(runners, "find_claude", lambda: str(exe))
    return {"exe": exe, "probe": probe}


@pytest.fixture(autouse=True)
def no_stray_sessions():
    yield
    directorsession.stop_all()


def _escalation(root, text="look at this"):
    """The one path that is still a work item: followup escalating a stuck run."""
    item = queue.add(root, "director", title=text[:80], brief=text,
                     source="qa-gate-escalation")
    assert queue.reserve(root, int(item["id"]))
    return int(item["id"])


def _chat(root, text="hello"):
    directorsession._run_chat_turn(str(root), text)
    return directorsession.history(root)["messages"]


# --- pure logic -------------------------------------------------------------

def test_the_system_prompt_names_the_game_and_the_seats(root):
    prompt = directorsession.system_prompt(root)
    assert "DIRECTOR" in prompt
    assert "GAME:" in prompt
    # every seat, with the lanes and the tool surface it actually holds
    assert "SEATS you can file work for" in prompt
    for seat in ("narrative", "art", "gameplay"):
        assert f"  {seat} \u2014 " in prompt
    assert "writes: game/scripts/**" in prompt      # gameplay's lane
    assert "blender_" in prompt                     # art's craft surface
    assert "queue_add" in prompt


def test_sidecar_roundtrip_and_forget(root):
    directorsession._write_sidecar(root, {"cli_session_id": "abc", "turns": 3})
    assert directorsession._read_sidecar(root)["cli_session_id"] == "abc"
    directorsession.forget(root)
    note = directorsession._read_sidecar(root)
    assert "cli_session_id" not in note
    assert note.get("turns") == 3      # forget drops the id, not the record


def test_resume_failed_rule():
    dead = {"ok": False, "dead": True}
    assert directorsession._resume_failed(
        {"resumed": True, "ok_turns": 0}, dead)
    # One landed turn proves the session exists; later deaths are real.
    assert not directorsession._resume_failed(
        {"resumed": True, "ok_turns": 1}, dead)
    assert not directorsession._resume_failed(
        {"resumed": False, "ok_turns": 0}, dead)


def test_director_argv_shape():
    args = runners._claude_director_args(
        "claude", system="SYS", model="opus", resume="sess-9")
    # Full capability: the dispatch tool set plus the MCP server.
    for tool in ("Read", "Edit", "Write", "Glob", "Grep", "Bash",
                 "mcp__builders-gate"):
        assert tool in args
    # Appended framing, not a replaced prompt, and a resumed conversation.
    assert "--append-system-prompt" in args and "--system-prompt" not in args
    assert args[args.index("--resume") + 1] == "sess-9"
    assert "--max-turns" not in args
    # No money ceiling anywhere: this product does not meter spend.
    assert "--max-budget-usd" not in args
    bare = runners._claude_director_args(
        "claude", system="SYS", model="", resume="")
    assert "--resume" not in bare


def test_console_settings_defaults(root):
    assert settings.get(root, "console.runner") == "claude"
    assert settings.get(root, "console.model") == "opus"


def test_codex_director_uses_native_exec_resume():
    fresh = runners._codex_director_args(
        "codex", model="gpt-5.6-sol", cwd="C:/game")
    assert fresh[:3] == ["codex", "exec", "--json"]
    assert "workspace-write" in fresh and "mcp_servers.builders-gate" in " ".join(fresh)
    resumed = runners._codex_director_args(
        "codex", model="gpt-5.6-terra", cwd="C:/game", resume="thread-4")
    assert resumed[:4] == ["codex", "exec", "resume", "--json"]
    assert resumed[-2:] == ["thread-4", "-"]


def test_runner_and_model_switch_keep_native_sessions(root, monkeypatch):
    monkeypatch.setattr(runners, "find_codex", lambda: "codex")
    monkeypatch.setattr(codexmeta, "snapshot", lambda force=False: {
        "models": [{"value": "gpt-test", "label": "GPT Test", "default": True}],
        "limits": {}})
    directorsession._write_sidecar(root, {
        "sessions": {"claude": "claude-1", "codex": "codex-1"}})
    got = directorsession.configure(root, "codex", "gpt-test")
    assert got["runner"] == "codex" and got["model"] == "gpt-test"
    assert directorsession._session_for(root, "claude") == "claude-1"
    assert directorsession._session_for(root, "codex") == "codex-1"


def test_usage_windows_are_named_by_duration(monkeypatch):
    monkeypatch.setattr(codexmeta, "snapshot", lambda force=False: {
        "models": [], "limits": {"codex": {
            "primary": {"usedPercent": 21, "windowDurationMins": 300,
                        "resetsAt": 100},
            "secondary": {"usedPercent": 34, "windowDurationMins": 10080,
                          "resetsAt": 200}}}})
    got = codexmeta.usage_for("gpt-any")
    assert got["five_hour"]["used_percent"] == 21
    assert got["weekly"]["used_percent"] == 34


def test_registry_write_does_not_wipe_on_bad_read(root, monkeypatch):
    """The read-error path must FAIL a save, never blank the registry."""
    settings.set(root, "qa.max_rounds", 5)
    real_get = settings._ws.get

    def bad_get(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(settings._ws, "get", bad_get)
    with pytest.raises(Exception):
        settings.set(root, "console.model", "sonnet")
    monkeypatch.setattr(settings._ws, "get", real_get)
    assert settings.get(root, "qa.max_rounds") == 5


# --- the process, end to end ------------------------------------------------

def test_a_turn_lands_in_the_transcript(root, fake_claude):
    msgs = _chat(root, "what is the state of the board?")
    assert [m["role"] for m in msgs] == ["assistant"]
    assert msgs[0]["text"].startswith("thinking about: what is the state")
    # The sidecar remembers the CLI's own conversation for the next respawn.
    assert directorsession._read_sidecar(root)["cli_session_id"] == \
        "fake-session-1"
    probe = json.loads(fake_claude["probe"].read_text(encoding="utf-8"))
    assert probe["seat"] is None            # SEATLESS: the whole point
    assert probe["actor"] == "console:director"
    assert probe["root"] == str(root)


def test_second_turn_reuses_the_process(root, fake_claude):
    _chat(root, "one")
    with directorsession._lock:
        pid = directorsession._live[directorsession._pkey(root)]["proc"].pid
    msgs = _chat(root, "two")
    with directorsession._lock:
        entry = directorsession._live[directorsession._pkey(root)]
    assert entry["proc"].pid == pid          # one conversation, one process
    assert entry["turns"] == 2
    assert msgs[-1]["text"].startswith("thinking about: two")


def test_failed_resume_falls_back_to_fresh_reseeded_session(
        root, fake_claude, monkeypatch):
    directorsession._write_sidecar(root, {"cli_session_id": "gone-session"})
    monkeypatch.setenv("BGATE_FAKE_RESUME_DIES", "1")
    item_id = _escalation(root, "carry on")
    directorsession._run_item_turn(str(root), item_id, "carry on",
                                   "THEM: earlier context")
    item = queue.get(root, item_id)
    assert item["status"] == "done"
    # The fresh session was reseeded and told it is fresh, honestly.
    assert "could not be resumed" in item["result"]
    # The second spawn carried no --resume, and the marker moved on.
    probe = json.loads(fake_claude["probe"].read_text(encoding="utf-8"))
    assert "--resume" not in probe["argv"]
    assert directorsession._read_sidecar(root)["cli_session_id"] == \
        "fake-session-1"


def test_missing_cli_says_so_in_the_chat(root, monkeypatch):
    monkeypatch.setattr(runners, "find_claude", lambda: None)
    msgs = _chat(root, "hello?")
    assert msgs[-1]["role"] == "error"
    assert "claude CLI not found" in msgs[-1]["text"]


def test_status_reports_idle_session(root, fake_claude):
    _chat(root, "hello")
    live = directorsession.status(str(root))
    assert live["live"] is True
    assert live["running"] is False
    assert live["current_item"] == 0
    assert live["cli_session_id"] == "fake-session-1"


def test_stop_and_clear_semantics(root, fake_claude):
    _chat(root, "hello")
    assert directorsession.stop(str(root))["stopped"] is True
    # Idempotent, and the transcript survives: only the process ended.
    assert directorsession.stop(str(root))["stopped"] is False
    time.sleep(0.1)
    assert directorsession.history(root)["messages"]


def test_a_new_session_archives_the_transcript_rather_than_deleting_it(
        root, fake_claude):
    _chat(root, "one")
    assert directorsession.history(root)["messages"]
    directorsession.reset(root)
    assert directorsession.history(root)["messages"] == []
    kept = list((root / ".bgate" / "console").glob("chat-*.jsonl"))
    assert kept, "the old conversation was deleted rather than archived"


def test_tool_calls_are_recorded_as_their_own_lines(root):
    directorsession._post(root, "user", "go")
    block = {"type": "tool_use", "name": "Read",
             "input": {"file_path": "game/scenes/hub.tscn"}}
    directorsession._post(root, "tool", directorsession._tool_note(block),
                          tool=block["name"])
    msgs = directorsession.history(root)["messages"]
    assert [m["role"] for m in msgs] == ["user", "tool"]
    assert msgs[-1]["tool"] == "Read"
    assert msgs[-1]["text"] == "game/scenes/hub.tscn"


def test_an_overloaded_api_is_named_rather_than_shown_as_working(monkeypatch):
    """A 529 being retried must not read as a silent hang.

    Measured live: ten retries against `overloaded`, backing off to 33s each,
    while the pane showed "working…" and nothing else for minutes.
    """
    events = [
        {"type": "system", "subtype": "api_retry", "attempt": 3,
         "max_retries": 10, "error_status": 529, "error": "overloaded"},
        {"type": "result", "subtype": "success", "result": "landed",
         "total_cost_usd": 0.0},
    ]

    class _Proc:
        def poll(self):
            return None

    entry = {"proc": _Proc(), "says": [], "waiting": "", "record": ""}
    monkeypatch.setattr(directorsession, "_read_events",
                        lambda _e: [events.pop(0)] if events else [])
    got = directorsession._collect(entry, time.monotonic() + 5)
    assert got["ok"] is True
    assert "overloaded" in entry["waiting"]
    assert "529" in entry["waiting"]
    assert "3 of 10" in entry["waiting"]


def test_a_turn_that_times_out_while_retrying_says_the_api_was_overloaded(
        monkeypatch):
    events = [{"type": "system", "subtype": "api_retry", "attempt": 1,
               "max_retries": 10, "error_status": 529, "error": "overloaded"}]

    class _Proc:
        def poll(self):
            return None

    entry = {"proc": _Proc(), "says": [], "waiting": "", "record": ""}
    monkeypatch.setattr(directorsession, "_read_events",
                        lambda _e: [events.pop(0)] if events else [])
    got = directorsession._collect(entry, time.monotonic() - 1)
    assert got["ok"] is False
    assert "overloaded" in got["error"]
