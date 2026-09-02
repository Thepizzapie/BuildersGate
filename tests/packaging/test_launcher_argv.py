"""What the frozen binary does with each argv, and what it must never do.

THE BUG. A frozen build has no interpreter: sys.executable IS BuildersGate.exe.
An agent's MCP registration therefore named the app as its command and passed
`-m bgate_mcp.server` as its args, exactly as a source install would. The
launcher decided the subcommand with

    cmd = argv[0] if argv and not argv[0].startswith("-") else ""

so a leading dash collapsed to "" -- the same value a bare double-click
produces -- and "" opens the desktop window. Inviting a seat into a brainstorm
room spawned an agent, the agent's CLI started its MCP server, and the user got
"Builders Gate is already running" instead of a participant.

Two things fix it and both are tested here: the binary hosts the server under
its own `mcp` subcommand, and no dashed argv may reach the GUI path.

The launcher is a packaging script rather than an importable package, so it is
loaded by path.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "packaging" / "launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("bgate_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def spy(monkeypatch, launcher):
    """Record which entry point main() reaches, without running any of them."""
    calls: list[str] = []

    import bgate_ui.app
    import bgate_ui.window.desktop
    monkeypatch.setattr(bgate_ui.app, "serve",
                        lambda *a, **k: calls.append("serve"))
    monkeypatch.setattr(bgate_ui.window.desktop, "run",
                        lambda *a, **k: (calls.append("WINDOW"), 0)[1])

    import bgate_mcp.server
    monkeypatch.setattr(bgate_mcp.server, "main",
                        lambda *a, **k: calls.append("mcp"))
    monkeypatch.setattr(launcher, "_selftest", lambda: calls.append("selftest"))
    return calls


def run(launcher, monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["BuildersGate.exe", *argv])
    return launcher.main()


class TestTheMcpEntryPoint:
    def test_mcp_starts_the_server(self, launcher, monkeypatch, spy):
        """The registration a frozen build writes must start the server."""
        assert run(launcher, monkeypatch, ["mcp"]) == 0
        assert spy == ["mcp"]

    def test_mcp_never_opens_a_window(self, launcher, monkeypatch, spy):
        assert "WINDOW" not in spy


class TestNoDashedArgvOpensAWindow:
    def test_the_exact_reported_command(self, launcher, monkeypatch, spy):
        """THE REGRESSION: `BuildersGate.exe -m bgate_mcp.server`."""
        code = run(launcher, monkeypatch, ["-m", "bgate_mcp.server"])
        assert "WINDOW" not in spy, "a dashed argv opened the desktop app"
        assert code != 0, "an unrecognised argument must fail, not succeed quietly"

    @pytest.mark.parametrize("argv", [
        ["-c", "import sys"],
        ["--version"],
        ["-X", "utf8"],
    ])
    def test_other_dashed_argv_are_refused_too(self, launcher, monkeypatch, spy, argv):
        """Anything shelling out to 'the interpreter' hits this path."""
        code = run(launcher, monkeypatch, argv)
        assert "WINDOW" not in spy
        assert code != 0


class TestTheWindowStillOpensWhenItShould:
    def test_bare_double_click(self, launcher, monkeypatch, spy):
        assert run(launcher, monkeypatch, []) == 0
        assert spy == ["WINDOW"]

    def test_explicit_app(self, launcher, monkeypatch, spy):
        assert run(launcher, monkeypatch, ["app"]) == 0
        assert spy == ["WINDOW"]

    def test_the_apps_own_flags_are_not_mistaken_for_a_command(
            self, launcher, monkeypatch, spy):
        """--port and --debug are the app's, and must still open it."""
        assert run(launcher, monkeypatch, ["--port", "7788"]) == 0
        assert spy == ["WINDOW"]

    def test_serve_still_serves(self, launcher, monkeypatch, spy):
        assert run(launcher, monkeypatch, ["serve"]) == 0
        assert spy == ["serve"]


class TestRegistrationMatchesTheLauncher:
    """The args written into a registration must be args the launcher accepts."""

    def test_frozen_registers_the_mcp_subcommand(self, monkeypatch):
        import bgate_ui.agents.agentcli as agentcli
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        importlib.reload(agentcli)
        try:
            assert agentcli.MODULE_ARGS == ["mcp"]
        finally:
            monkeypatch.undo()
            importlib.reload(agentcli)

    def test_from_source_registers_the_module(self):
        import bgate_ui.agents.agentcli as agentcli
        assert agentcli.MODULE_ARGS == ["-m", "bgate_mcp.server"]

    def test_runners_uses_the_same_answer(self):
        """Two places writing the registration must not drift apart."""
        import bgate_ui.agents.agentcli as agentcli
        from bgate_ui.agents import runners
        flags = runners.mcp_overrides("builders-gate")
        written = [f for f in flags if ".args=" in f][0]
        for arg in agentcli.MODULE_ARGS:
            assert f'"{arg}"' in written
