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

from bgate_core import queue, settings
from bgate_ui import directorsession, runners
from bgate_ui.routes import console as console_route
from bgate_ui.routes.orchestrator import _DELEGATED_RE

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


def _turn_item(root, text="hello"):
    item = queue.add(root, "director", title=text[:80],
                     brief=console_route._turn_brief(text), source="chat")
    assert queue.reserve(root, int(item["id"]))
    return int(item["id"])


# --- pure logic -------------------------------------------------------------

def test_turn_prompt_is_verbatim_and_stamps_lineage():
    prompt = directorsession.turn_prompt("fix the jump feel", 41)
    assert prompt.startswith("fix the jump feel\n")
    # The stamp the prompt teaches must be the one the console graph parses.
    assert _DELEGATED_RE.search(prompt.replace("`", "")).group(1) == "41"


def test_turn_brief_roundtrips_the_message():
    text = "a message\nwith two lines and " + "x" * 200
    assert console_route.said(console_route._turn_brief(text)) == text


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
        "claude", system="SYS", model="opus", max_usd=15.0, resume="sess-9")
    # Full capability: the dispatch tool set plus the MCP server.
    for tool in ("Read", "Edit", "Write", "Glob", "Grep", "Bash",
                 "mcp__builders-gate"):
        assert tool in args
    # Appended framing, not a replaced prompt; a resumed conversation; a budget.
    assert "--append-system-prompt" in args and "--system-prompt" not in args
    assert args[args.index("--resume") + 1] == "sess-9"
    assert "--max-budget-usd" in args
    assert "--max-turns" not in args
    bare = runners._claude_director_args(
        "claude", system="SYS", model="", max_usd=0.0, resume="")
    assert "--resume" not in bare and "--max-budget-usd" not in bare


def test_console_settings_defaults(root):
    assert settings.get(root, "console.model") == "opus"
    assert settings.get(root, "console.max_usd") == 15.0


def test_registry_write_does_not_wipe_on_bad_read(root, monkeypatch):
    """The read-error path must FAIL a save, never blank the registry."""
    settings.set(root, "qa.max_rounds", 5)
    real_get = settings._ws.get

    def bad_get(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(settings._ws, "get", bad_get)
    with pytest.raises(Exception):
        settings.set(root, "console.max_usd", 9.0)
    monkeypatch.setattr(settings._ws, "get", real_get)
    assert settings.get(root, "qa.max_rounds") == 5


# --- the process, end to end ------------------------------------------------

def test_turn_settles_item_with_reply(root, fake_claude):
    item_id = _turn_item(root, "what is the state of the board?")
    directorsession._run_turn(
        str(root), item_id,
        directorsession.turn_prompt("what is the state of the board?", item_id),
        "")
    item = queue.get(root, item_id)
    assert item["status"] == "done"
    assert item["result"].startswith("reply to: what is the state")
    # The sidecar remembers the CLI's own conversation for the next respawn.
    assert directorsession._read_sidecar(root)["cli_session_id"] == \
        "fake-session-1"
    probe = json.loads(fake_claude["probe"].read_text(encoding="utf-8"))
    assert probe["seat"] is None            # SEATLESS: the whole point
    assert probe["actor"] == "console:director"
    assert probe["root"] == str(root)


def test_second_turn_reuses_the_process(root, fake_claude):
    first = _turn_item(root, "one")
    directorsession._run_turn(str(root), first,
                              directorsession.turn_prompt("one", first), "")
    with directorsession._lock:
        pid = directorsession._live[directorsession._pkey(root)]["proc"].pid
    second = _turn_item(root, "two")
    directorsession._run_turn(str(root), second,
                              directorsession.turn_prompt("two", second), "")
    with directorsession._lock:
        entry = directorsession._live[directorsession._pkey(root)]
    assert entry["proc"].pid == pid          # one conversation, one process
    assert entry["turns"] == 2
    assert queue.get(root, second)["result"].startswith("reply to: two")


def test_failed_resume_falls_back_to_fresh_reseeded_session(
        root, fake_claude, monkeypatch):
    directorsession._write_sidecar(root, {"cli_session_id": "gone-session"})
    monkeypatch.setenv("BGATE_FAKE_RESUME_DIES", "1")
    item_id = _turn_item(root, "carry on")
    directorsession._run_turn(
        str(root), item_id, directorsession.turn_prompt("carry on", item_id),
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


def test_missing_cli_fails_the_turn_with_a_sentence(root, monkeypatch):
    monkeypatch.setattr(runners, "find_claude", lambda: None)
    item_id = _turn_item(root, "hello?")
    directorsession._run_turn(str(root), item_id, "hello?", "")
    item = queue.get(root, item_id)
    assert item["status"] == "failed"
    assert "claude CLI not found" in item["result"]


def test_budget_refusal_is_a_reply_not_a_failure(root, fake_claude):
    first = _turn_item(root, "one")
    directorsession._run_turn(str(root), first,
                              directorsession.turn_prompt("one", first), "")
    with directorsession._lock:
        directorsession._live[directorsession._pkey(root)]["spent_usd"] = 99.0
    second = _turn_item(root, "two")
    directorsession._run_turn(str(root), second,
                              directorsession.turn_prompt("two", second), "")
    item = queue.get(root, second)
    assert item["status"] in ("done", "review")
    assert "ceiling" in item["result"]


def test_status_reports_idle_session(root, fake_claude):
    item_id = _turn_item(root, "hello")
    directorsession._run_turn(str(root), item_id,
                              directorsession.turn_prompt("hello", item_id), "")
    live = directorsession.status(str(root))
    assert live["live"] is True
    assert live["current_item"] == 0
    assert live["cli_session_id"] == "fake-session-1"
    assert live["spent_usd"] > 0


def test_stop_and_clear_semantics(root, fake_claude):
    item_id = _turn_item(root, "hello")
    directorsession._run_turn(str(root), item_id,
                              directorsession.turn_prompt("hello", item_id), "")
    assert directorsession.stop(str(root))["stopped"] is True
    # Idempotent, and the transcript survives: only the process ended.
    assert directorsession.stop(str(root))["stopped"] is False
    time.sleep(0.1)
    assert queue.get(root, item_id)["status"] == "done"
