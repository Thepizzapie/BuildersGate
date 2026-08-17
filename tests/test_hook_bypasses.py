"""The five documented ways past the Bash gate, and the second shell.

Each of these was a working bypass: a command shape an agent could use (or
stumble into) that wrote files while the analyser reported nothing. The tests
pin the CLOSED state — every shape either names its write targets or lands in
``unclear``, the fail-closed channel.
"""
from __future__ import annotations

import os

from bgate_cli import hook


def _writes(command: str) -> list[str]:
    return hook.analyse_bash(command)["writes"]


def _unclear(command: str) -> list[str]:
    return hook.analyse_bash(command)["unclear"]


class TestMultilineCommands:
    def test_line_two_is_a_command_not_arguments(self):
        got = hook.analyse_bash("ls\nrm -rf game/level.tscn")
        assert got["writes"] == ["game/level.tscn"]

    def test_every_line_is_read(self):
        got = hook.analyse_bash("echo a > one.txt\necho b > two.txt\nls")
        assert got["writes"] == ["one.txt", "two.txt"]


class TestEval:
    def test_a_quoted_eval_payload_is_reanalysed(self):
        assert _writes("eval 'echo x > game/foo.gd'") == ["game/foo.gd"]

    def test_read_only_eval_stays_clean(self):
        got = hook.analyse_bash("eval 'ls -la'")
        assert got == {"writes": [], "unclear": []}


class TestCd:
    def test_a_resolvable_cd_shifts_relative_writes(self):
        got = _writes("cd ../other-game && echo x > game/foo.gd")
        assert got == [os.path.join("../other-game", "game/foo.gd")]

    def test_two_cds_compound(self):
        got = _writes("cd sub && cd deeper && touch a.txt")
        assert got == [os.path.join("sub", "deeper", "a.txt")]

    def test_an_absolute_write_ignores_the_cd(self):
        target = "C:/elsewhere/x.gd" if os.name == "nt" else "/elsewhere/x.gd"
        assert _writes(f"cd sub && echo x > {target}") == [target]

    def test_an_unresolvable_cd_fails_closed_on_relative_writes(self):
        assert _unclear("cd $SOMEWHERE && echo x > foo.gd")
        assert _writes("cd $SOMEWHERE && echo x > foo.gd") == []

    def test_a_bare_cd_fails_closed_too(self):
        assert _unclear("cd && rm foo.gd")


class TestInterpreterSnippets:
    def test_perl_e_that_writes_is_unclear_without_dash_i(self):
        # perl is in _INPLACE and _INTERPRETERS; the in-place branch used to
        # shadow the snippet check entirely.
        assert _unclear("perl -e 'open(F, \">x\"); print F 1'")

    def test_python_heredoc_that_writes_is_unclear(self):
        body = 'python <<EOF\nopen("x","w").write("1")\nEOF'
        assert _unclear(body)

    def test_a_prose_heredoc_is_not_shell(self):
        # The body belongs to cat, not to a shell — the only write is the
        # redirect target, and 'rm -rf' in prose is not a command.
        got = hook.analyse_bash(
            "cat > notes.md <<EOF\nprose about rm -rf things\nEOF")
        assert got["writes"] == ["notes.md"]
        assert got["unclear"] == []

    def test_python_c_spawning_a_subprocess_is_unclear(self):
        assert _unclear("python -c \"import subprocess; "
                        "subprocess.run(['rm', 'x'])\"")


class TestPowerShellIsFenced:
    def _payload(self, command: str, cwd: str) -> dict:
        return {"tool_name": "PowerShell", "cwd": cwd,
                "tool_input": {"command": command}}

    def test_a_write_shaped_command_is_refused_for_a_seat(self, root):
        code, msg = hook.decide(self._payload(
            "Set-Content game/foo.gd 'x'", str(root)), "tech", mode="block")
        assert code == hook.BLOCK
        assert "PowerShell" in msg

    def test_read_shaped_commands_pass(self, root):
        code, _ = hook.decide(self._payload(
            "Get-ChildItem game", str(root)), "tech", mode="block")
        assert code == hook.ALLOW

    def test_outside_a_project_nothing_is_protected(self, tmp_path_factory):
        outside = tmp_path_factory.mktemp("no-project")
        code, _ = hook.decide(self._payload(
            "Remove-Item x.gd", str(outside)), "tech", mode="block")
        assert code == hook.ALLOW

    def test_collide_mode_is_advisory_here_too(self, root):
        code, _ = hook.decide(self._payload(
            "Set-Content game/foo.gd 'x'", str(root)),
            hook.DIRECTOR_SEAT, mode="collide")
        assert code == hook.ALLOW
