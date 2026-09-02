"""`bgate connect` — the command that used to be a paragraph in the README.

WHAT IT REPLACED, and why the replacement is a command rather than a better
paragraph:

    claude mcp add builders-gate --scope user -- <ABSOLUTE-python-path> -m bgate_mcp.server

Setup asked a new user to hand-assemble the one argument the docs themselves
admit is the most common failure on the supported platform. The dashboard has
been able to fill it in for a while — Settings → Agent CLIs — but reaching that
meant starting a server to finish an install, and a user whose registration is
broken is exactly the user who cannot get there.

THE INVARIANTS THESE PIN.

  * A bare `bgate connect` WRITES NOTHING. It is the report. Registering
    changes what every future session of a client can do on the whole machine,
    so it takes a named target or --all.
  * --all means every client that can be WRITTEN to, not every client that can
    be named. A file-kind row has no write, and including it would put a
    guaranteed failure in the exit code of the command users are told to run.
  * Every write is VERIFIED. "Registered" is precisely the claim that has been
    wrong before; the interpreter the config now names gets asked whether it can
    import the server.
  * An unknown name is an error that lists the known ones, not a silent no-op.

Nothing here spawns a CLI: agentcli's _run is patched, and the assertions are on
the argv and the exit code.
"""
from __future__ import annotations

import json
import sys

import pytest

from bgate_cli import main as M
from bgate_ui.agents import agentcli as A


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(A.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    # No client is installed unless a test says so, or this suite reports on
    # whatever the developer happens to have on PATH.
    monkeypatch.setattr(A, "shutil", type("S", (), {"which": staticmethod(
        lambda name: None)})())
    monkeypatch.setattr(A._runners, "find_claude", lambda: None)
    monkeypatch.setattr(A._runners, "find_codex", lambda: None)
    return tmp_path


def _wired(home):
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "builders-gate": {"command": sys.executable,
                          "args": ["-m", "bgate_mcp.server"]}}}),
        encoding="utf-8")


class TestTheReport:
    def test_a_bare_connect_writes_nothing(self, home, monkeypatch, capsys):
        ran = []
        monkeypatch.setattr(A, "_run", lambda argv, timeout: ran.append(argv))
        M.connect([])
        assert not ran
        assert "MCP server" in capsys.readouterr().out

    def test_it_exits_1_when_nothing_on_the_machine_can_call_the_tools(
            self, home, capsys):
        assert M.connect([]) == 1
        capsys.readouterr()

    def test_it_exits_0_once_one_client_is_correctly_wired(self, home,
                                                           monkeypatch, capsys):
        _wired(home)
        monkeypatch.setattr(A._runners, "find_claude", lambda: r"C:\bin\claude.exe")
        assert M.connect([]) == 0
        assert "wired, pinned to this interpreter" in capsys.readouterr().out

    def test_the_report_names_the_restart_nobody_remembers(self, home, capsys):
        M.connect([])
        assert "restart the client" in capsys.readouterr().out

    def test_show_prints_a_block_for_a_client_with_no_mcp_add(self, home,
                                                              capsys):
        (home / ".cursor").mkdir()
        M.connect([], show=True)
        out = capsys.readouterr().out
        assert "mcpServers" in out
        assert sys.executable in out

    def test_show_answers_for_a_client_nobody_here_has_heard_of(self, home,
                                                               capsys):
        M.connect([], show=True)
        assert "any other MCP client" in capsys.readouterr().out

    def test_json_is_the_whole_payload(self, home, capsys):
        M.connect([], as_json=True)
        doc = json.loads(capsys.readouterr().out)
        assert doc["interpreter"] == sys.executable
        assert {r["id"] for r in doc["runners"]} == set(A.ids())


class TestWriting:
    def test_a_named_target_registers_and_then_verifies_it(self, home,
                                                           monkeypatch, capsys):
        """VERIFICATION IS NOT OPTIONAL HERE. "Registered" is the claim that has
        been wrong before, and the only way to separate a registration that
        works from one that looks identical is to ask the interpreter it names
        whether it can import the server."""
        monkeypatch.setattr(A._runners, "find_claude", lambda: r"C:\bin\claude.exe")
        seen = []

        def fake_run(argv, timeout):
            seen.append(argv)
            if argv[1:2] == ["-c"]:
                return {"ok": True, "output": sys.executable}
            _wired(home)                       # the CLI wrote its config
            return {"ok": True, "output": "Added builders-gate"}

        monkeypatch.setattr(A, "_run", fake_run)
        assert M.connect(["claude"]) == 0
        assert seen[0][1:3] == ["mcp", "add"]
        assert seen[-1][1] == "-c"             # the import probe
        assert "import bgate_mcp.server" in seen[-1][2]
        assert "-> " in capsys.readouterr().out

    def test_a_failed_registration_is_a_nonzero_exit(self, home, monkeypatch,
                                                     capsys):
        monkeypatch.setattr(A._runners, "find_claude", lambda: r"C:\bin\claude.exe")
        monkeypatch.setattr(A, "_run", lambda argv, timeout: {
            "ok": False, "output": "no such command"})
        assert M.connect(["claude"]) == 1
        assert "no such command" in capsys.readouterr().out

    def test_an_unknown_client_lists_the_known_ones(self, home, capsys):
        assert M.connect(["cursur"]) == 1
        out = capsys.readouterr().out
        assert "unknown client" in out
        for known in A.ids():
            assert known in out

    def test_a_file_kind_target_refuses_rather_than_merging(self, home,
                                                            monkeypatch, capsys):
        ran = []
        monkeypatch.setattr(A, "_run", lambda argv, timeout: ran.append(argv))
        assert M.connect(["cursor"]) == 1
        assert not ran
        assert "does not hand-edit" in capsys.readouterr().out


class TestAll:
    def test_all_is_every_writable_client_not_every_named_one(self, home,
                                                              monkeypatch):
        """--all on a machine with Cursor installed must not queue a write that
        cannot happen. The user was told to run this command; its exit code has
        to mean something."""
        (home / ".cursor").mkdir()
        monkeypatch.setattr(A._runners, "find_claude", lambda: r"C:\bin\claude.exe")
        picked = [r["id"] for r in A.status()
                  if r["installed"] and r["mcp"].get("can_register")]
        assert picked == ["claude"]

    def test_the_verb_reaches_connect_through_main(self, home, monkeypatch,
                                                   capsys):
        monkeypatch.setattr(sys, "argv", ["bgate", "connect"])
        assert M.main() == 1                   # nothing wired in this fake home
        assert "MCP server" in capsys.readouterr().out
