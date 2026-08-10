"""Is Builders Gate actually wired into each coding-agent CLI.

THE STATE THIS EXISTS TO CATCH is a registration that looks right and is not.
CLAUDE.md names it: a bare `python` resolves against whatever is first on PATH
when the CLI launches the server, the CLI reports "failed to connect", and the
message points nowhere near the interpreter. On Windows it is the single most
common setup failure in this product. `claude mcp list` shows that registration
and a correct one identically, which is exactly why something had to be able to
tell them apart.

Four states, and three of them are the trap:

    absent              nothing registered
    bare                `python`, no directory part — the documented failure
    other-interpreter   absolute, but not the one this dashboard runs on
    pinned              the only good one

THE FIRST BUG IN THIS MODULE WAS THE THIRD STATE EATING THE FOURTH: `bare` was
tested by BASENAME, so every correct `C:\\...\\python.exe` registration was
flagged as broken. That is what test_an_absolute_python_is_not_bare pins.

NOTHING HERE SPAWNS A CLI. Registration goes through the CLI's own `mcp add`
because it owns the config format; the tests patch the runner so no subprocess
ever starts, and assert on the ARGV, which is the part that has to be right.
"""
from __future__ import annotations

import json
import sys

import pytest

from bgate_ui import agentcli as A


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A fake home so nothing here can read or write the developer's real
    ~/.claude.json — which on this machine holds their whole session history."""
    monkeypatch.setattr(A.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    return tmp_path


def _claude_json(home, entry, *, scope="user", project="C:/game"):
    doc = {"numStartups": 3}
    if entry is not None:
        if scope == "user":
            doc["mcpServers"] = {"builders-gate": entry}
        else:
            doc["projects"] = {project: {"mcpServers": {"builders-gate": entry}}}
    (home / ".claude.json").write_text(json.dumps(doc), encoding="utf-8")


def _codex_toml(home, body):
    folder = home / ".codex"
    folder.mkdir(exist_ok=True)
    (folder / "config.toml").write_text(body, encoding="utf-8")


GOOD = {"type": "stdio", "command": sys.executable,
        "args": ["-m", "bgate_mcp.server"], "env": {}}


class TestJudge:
    def test_nothing_registered_says_so_in_the_user_s_terms(self, home):
        _claude_json(home, None)
        got = A._judge(A._claude_read())
        assert got["state"] == "absent"
        assert "your own sessions" in got["verdict"]

    def test_a_bare_python_is_the_documented_failure(self, home):
        _claude_json(home, {"command": "python", "args": ["-m", "bgate_mcp.server"]})
        got = A._judge(A._claude_read())
        assert got["state"] == "bare"
        assert "failed to connect" in got["verdict"]

    def test_an_absolute_python_is_not_bare(self, home):
        """THE REGRESSION. A basename test flags every correct registration on
        Windows, where the right answer always ends in \\python.exe."""
        _claude_json(home, GOOD)
        assert A._judge(A._claude_read())["state"] == "pinned"

    def test_a_different_interpreter_is_a_separate_state(self, home):
        """Not automatically wrong — it works if Builders Gate is installed
        there too — so it gets its own state and its own sentence rather than
        being lumped in with the bare case."""
        _claude_json(home, {"command": r"C:\Python311\python.exe",
                            "args": ["-m", "bgate_mcp.server"]})
        got = A._judge(A._claude_read())
        assert got["state"] == "other-interpreter"
        assert r"C:\Python311\python.exe" in got["verdict"]

    def test_unexpected_args_are_reported_rather_than_corrected(self, home):
        _claude_json(home, {"command": sys.executable,
                            "args": ["-m", "something_else"]})
        got = A._judge(A._claude_read())
        assert got["state"] == "odd-args"
        assert "may be deliberate" in got["verdict"]


class TestClaudeConfig:
    def test_a_project_scoped_entry_is_found_and_labelled(self, home):
        _claude_json(home, GOOD, scope="project")
        entry = A._claude_read()
        assert entry["found"] is True
        assert entry["scope"].startswith("local")

    def test_a_missing_file_is_not_an_error_state(self, home):
        entry = A._claude_read()
        assert entry["found"] is False
        assert "first run" in entry["error"]

    def test_broken_json_refuses_to_guess(self, home):
        (home / ".claude.json").write_text("{{{", encoding="utf-8")
        assert "not valid JSON" in A._claude_read()["error"]


class TestCodexConfig:
    def test_the_table_is_read(self, home):
        _codex_toml(home, "model = 'gpt-5'\n\n[mcp_servers.builders-gate]\n"
                          f"command = '{sys.executable}'\n"
                          'args = ["-m", "bgate_mcp.server"]\n')
        entry = A._codex_read()
        assert entry["found"] is True
        assert entry["command"] == sys.executable
        assert entry["args"] == ["-m", "bgate_mcp.server"]

    def test_an_unrelated_server_is_not_mistaken_for_ours(self, home):
        _codex_toml(home, "[mcp_servers.other]\ncommand = 'node'\n")
        assert A._codex_read()["found"] is False

    def test_it_reads_the_same_answer_without_tomllib(self, home, monkeypatch):
        """3.10 is a supported interpreter and has no tomllib. A setup panel
        that goes blank on the oldest supported Python fails exactly where
        setup is hardest."""
        _codex_toml(home, "[mcp_servers.builders-gate]\n"
                          f"command = '{sys.executable}'\n"
                          'args = ["-m", "bgate_mcp.server"]\n')
        real = __import__

        def no_tomllib(name, *a, **k):
            if name == "tomllib":
                raise ImportError("no tomllib on 3.10")
            return real(name, *a, **k)

        monkeypatch.setattr("builtins.__import__", no_tomllib)
        entry = A._codex_read()
        assert entry["found"] is True
        assert entry["command"] == sys.executable


class TestRegistration:
    def test_the_argv_pins_the_interpreter_and_uses_no_shell(self, home,
                                                             monkeypatch):
        seen = []
        monkeypatch.setattr(A, "_run", lambda argv, timeout: seen.append(argv)
                            or {"ok": True, "output": ""})
        monkeypatch.setitem(A._runners.RUNNERS, "claude",
                            A._runners.RUNNERS["claude"])
        monkeypatch.setattr(A._runners, "find_claude", lambda: r"C:\bin\claude.exe")
        _claude_json(home, None)
        A.register("claude")
        argv = seen[-1]
        assert argv[:6] == [r"C:\bin\claude.exe", "mcp", "add", "builders-gate",
                            "--scope", "user"]
        assert argv[6] == "--"
        assert argv[7] == sys.executable       # never a bare name
        assert argv[8:] == ["-m", "bgate_mcp.server"]
        # A LIST, not a string. The interpreter path has spaces on the supported
        # platform and a shell string is one quoting mistake from running
        # something else.
        assert all(isinstance(part, str) for part in argv)

    def test_an_existing_entry_is_removed_first(self, home, monkeypatch):
        """`mcp add` refuses a name it already holds, so without this the one
        button that repairs a bad registration is the one that cannot."""
        seen = []
        monkeypatch.setattr(A, "_run", lambda argv, timeout: seen.append(argv)
                            or {"ok": True, "output": ""})
        monkeypatch.setattr(A._runners, "find_claude", lambda: r"C:\bin\claude.exe")
        _claude_json(home, {"command": "python", "args": ["-m", "bgate_mcp.server"]})
        A.register("claude")
        assert seen[0][1:3] == ["mcp", "remove"]
        assert seen[1][1:3] == ["mcp", "add"]

    def test_a_missing_cli_refuses_instead_of_execing_none(self, home,
                                                           monkeypatch):
        monkeypatch.setattr(A._runners, "find_claude", lambda: None)
        got = A.register("claude")
        assert got["ok"] is False
        assert "not on PATH" in got["error"]

    def test_the_copyable_command_matches_what_the_button_runs(self, home):
        line = A.command_line("claude")
        assert "mcp add builders-gate --scope user --" in line
        assert sys.executable in line
        assert "-m bgate_mcp.server" in line


class TestVerify:
    def test_a_bare_registration_refuses_to_guess_how_it_would_resolve(
            self, home, monkeypatch):
        _claude_json(home, {"command": "python", "args": ["-m", "bgate_mcp.server"]})
        called = []
        monkeypatch.setattr(A, "_run", lambda argv, timeout: called.append(argv))
        got = A.verify("claude")
        assert got["ok"] is False
        assert "unpredictability IS the bug" in got["error"]
        assert not called          # nothing was executed

    def test_it_asks_the_registered_interpreter_to_import_not_to_serve(
            self, home, monkeypatch):
        """Starting the actual server would sit on stdin forever. The probe is a
        one-line import that prints a path and exits."""
        _claude_json(home, GOOD)
        seen = []
        monkeypatch.setattr(A, "_run", lambda argv, timeout: seen.append(argv)
                            or {"ok": True, "output": sys.executable})
        got = A.verify("claude")
        assert got["ok"] is True
        assert seen[0][0] == sys.executable
        assert seen[0][1] == "-c"
        assert "import bgate_mcp.server" in seen[0][2]

    def test_a_failing_import_is_named_as_the_failed_to_connect_state(
            self, home, monkeypatch):
        _claude_json(home, GOOD)
        monkeypatch.setattr(A, "_run", lambda argv, timeout: {
            "ok": False, "output": "ModuleNotFoundError: bgate_mcp"})
        got = A.verify("claude")
        assert got["ok"] is False
        assert "failed to connect" in got["error"]


class TestPayload:
    def test_every_runner_in_the_registry_gets_a_row(self, home):
        rows = A.status()
        assert {r["id"] for r in rows} == set(A._runners.RUNNERS)

    def test_detection_is_borrowed_from_runners_not_re_derived(self, home,
                                                               monkeypatch):
        """runners.available() already answers installed/path. A second lookup
        here could disagree with the one dispatch uses, which is the worst
        possible place for two answers."""
        monkeypatch.setattr(A._runners, "find_claude", lambda: r"C:\bin\claude.exe")
        row = [r for r in A.status() if r["id"] == "claude"][0]
        assert row["installed"] is True
        assert row["path"] == r"C:\bin\claude.exe"

    def test_the_payload_explains_why_absolute_once(self, home):
        why = A.payload()["why_absolute"]
        assert "failed to connect" in why
        assert "PATH" in why
