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

from bgate_ui.agents import agentcli as A


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
    def test_every_runner_gets_a_row_and_runners_come_first(self, home):
        """A ROW IS NO LONGER THE SAME THING AS A RUNNER. Dispatching work to a
        CLI and letting the human's own sessions call the tools are different
        capabilities; the second is why anybody installs this, and Cursor can
        do it while never appearing in RUNNERS. What must still hold is that
        every runner is present and that runners lead the table."""
        rows = A.status()
        ids = [r["id"] for r in rows]
        assert set(A._runners.RUNNERS).issubset(set(ids))
        lead = ids[:len(A._runners.RUNNERS)]
        assert set(lead) == set(A._runners.RUNNERS)

    def test_only_a_runner_claims_it_can_be_dispatched_to(self, home):
        for row in A.status():
            assert row["dispatches"] is (row["id"] in A._runners.RUNNERS)
            if not row["dispatches"]:
                assert row["steerable"] is False
                assert row["cost_tracked"] is False
                assert "cannot dispatch" in row["used_for"]

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


class TestTheOtherClients:
    """Cursor, Windsurf, opencode, VS Code and Gemini.

    THE WHOLE POINT OF WIDENING THIS is that the bad-registration verdict is not
    a Claude Code fact. A bare `python` in ~/.cursor/mcp.json fails exactly the
    same way and nothing could see it, so the reader is shared and _judge is
    untouched — these tests pin that the shape differences (a different key, a
    different entry spelling) do not become different verdicts.
    """

    def test_a_bare_interpreter_is_the_same_bug_in_cursor(self, home):
        folder = home / ".cursor"
        folder.mkdir()
        (folder / "mcp.json").write_text(json.dumps({"mcpServers": {
            "builders-gate": {"command": "python",
                              "args": ["-m", "bgate_mcp.server"]}}}),
            encoding="utf-8")
        assert A._judge(A._cursor_read())["state"] == "bare"

    def test_opencode_s_command_array_reads_as_command_plus_args(self, home,
                                                                 monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        folder = home / ".config" / "opencode"
        folder.mkdir(parents=True)
        (folder / "opencode.json").write_text(json.dumps({"mcp": {
            "builders-gate": {"type": "local",
                              "command": [sys.executable, "-m", "bgate_mcp.server"]}}}),
            encoding="utf-8")
        entry = A._opencode_read()
        assert entry["command"] == sys.executable
        assert entry["args"] == ["-m", "bgate_mcp.server"]
        assert A._judge(entry)["state"] == "pinned"

    def test_vs_code_keeps_its_servers_under_servers_not_mcpservers(
            self, home, monkeypatch):
        monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        path = A._vscode_config()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"servers": {
            "builders-gate": {"command": sys.executable,
                              "args": ["-m", "bgate_mcp.server"]}}}),
            encoding="utf-8")
        assert A._judge(A._vscode_read())["state"] == "pinned"

    def test_a_config_with_comments_is_unreadable_not_absent(self, home):
        """ABSENT IS THE ONE VERDICT THAT WOULD DO HARM HERE: it sends a user to
        register something they already have. Several of these files legally
        carry // comments, so a parse failure says so."""
        folder = home / ".cursor"
        folder.mkdir()
        (folder / "mcp.json").write_text(
            '{\n  // mine\n  "mcpServers": {}\n}', encoding="utf-8")
        got = A._cursor_read()
        assert got["found"] is False
        assert "not plain JSON" in got["error"]

    def test_a_file_kind_client_is_never_written_to(self, home, monkeypatch):
        """THE PROMISE, AS A TEST. These configs are hand-edited by the user in
        formats their owners are free to change; this module reads them and
        renders the block, and register() must refuse rather than merge."""
        ran = []
        monkeypatch.setattr(A, "_run", lambda argv, timeout: ran.append(argv))
        got = A.register("cursor")
        assert got["ok"] is False
        assert not ran
        assert "does not hand-edit" in got["error"]
        assert "mcpServers" in got["block"]

    def test_the_block_carries_this_interpreter_already_filled_in(self, home):
        entry = json.loads(A.block("cursor"))["mcpServers"]["builders-gate"]
        assert entry["command"] == sys.executable
        assert entry["args"] == A.MODULE_ARGS

    def test_opencode_s_block_is_in_opencode_s_shape(self, home):
        entry = json.loads(A.block("opencode"))["mcp"]["builders-gate"]
        assert entry["command"] == [sys.executable, *A.MODULE_ARGS]

    def test_a_cli_kind_client_gets_a_command_and_no_block(self, home):
        assert A.command_line("claude")
        assert A.block("claude") == ""
        assert A.block("cursor")
        assert A.command_line("cursor") == ""

    def test_vs_code_s_copyable_line_survives_a_shell(self, home):
        """`code --add-mcp` takes a JSON OBJECT as one argument, so the copied
        line carries quotes inside quotes. Wrapping that in bare double quotes
        ends the argument at the first inner one and the user pastes a line that
        fails with a parse error nowhere near the cause."""
        line = A.command_line("vscode")
        assert line.startswith("code --add-mcp")
        assert '\\"name\\"' in line

    def test_gemini_is_registered_at_user_scope(self, home):
        """The default scope is the CURRENT PROJECT, and a registration that
        only works in the directory you were standing in is the same class of
        surprise this module exists for."""
        argv = A._gemini_argv("gemini", sys.executable)
        assert argv[:5] == ["gemini", "mcp", "add", "-s", "user"]

    def test_a_client_with_no_remover_does_not_raise_on_re_register(
            self, home, monkeypatch):
        """`code --add-mcp` upserts and has no remove subcommand. A missing
        remover must not be a KeyError on the one button that repairs a bad
        registration."""
        monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        path = A._vscode_config()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"servers": {
            "builders-gate": {"command": "python",
                              "args": ["-m", "bgate_mcp.server"]}}}),
            encoding="utf-8")
        seen = []
        monkeypatch.setattr(A, "_run", lambda argv, timeout: seen.append(argv)
                            or {"ok": True, "output": ""})
        monkeypatch.setattr(A, "_exe", lambda rid: r"C:\bin\code.cmd")
        A.register("vscode")
        assert len(seen) == 1                     # add only, no remove
        assert seen[0][1] == "--add-mcp"

    def test_removing_from_a_file_kind_client_names_the_file(self, home,
                                                             monkeypatch):
        monkeypatch.setattr(A, "_exe", lambda rid: str(home / ".cursor"))
        got = A.unregister("cursor")
        assert got["ok"] is False
        assert "mcp.json" in got["error"]


class TestGenericBlock:
    def test_there_is_an_answer_for_a_client_nobody_here_has_heard_of(self, home):
        """"Whatever agent the user uses" includes ones that do not exist yet.
        Every MCP client reads some spelling of this object, and the half a user
        gets wrong copying it off a docs page is the interpreter."""
        entry = json.loads(A.generic_block())["mcpServers"]["builders-gate"]
        assert entry["command"] == sys.executable
        assert A.payload()["generic_block"] == A.generic_block()


class TestDoctorRow:
    def test_wired_but_undispatchable_says_both_things(self, home, monkeypatch):
        """A machine with only Cursor wired can call the tools and cannot run
        the board. One lamp, two sentences — merging them loses the half that
        explains why nothing starts."""
        monkeypatch.setattr(A, "status", lambda: [
            {"id": "cursor", "label": "Cursor", "installed": True,
             "dispatches": False, "mcp": {"ok": True, "state": "pinned"}},
            {"id": "claude", "label": "Claude Code", "installed": False,
             "dispatches": True, "mcp": {"ok": False, "state": "absent"}},
        ])
        row = A.doctor_row()
        assert row["available"] is True
        assert "no dispatch runner" in row["detail"]
