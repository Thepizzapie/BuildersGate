"""The five documented ways past the Bash gate, and the second shell.

Each of these was a working bypass: a command shape an agent could use (or
stumble into) that wrote files while the analyser reported nothing. The tests
pin the CLOSED state — every shape either names its write targets or lands in
``unclear``, the fail-closed channel.
"""
from __future__ import annotations

import os

import pytest

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

    def test_cd_state_survives_a_newline(self):
        # A newline separates statements exactly like `&&` - it must not reset
        # the tracked directory, or line two's relative write is judged in the
        # wrong tree.
        got = hook.analyse_bash("cd ../other\necho x > game/foo.gd")
        assert got["writes"] == [os.path.join("../other", "game/foo.gd")]

    def test_commands_after_a_heredoc_are_still_read(self):
        got = hook.analyse_bash(
            "cat > a.txt <<EOF\nprose\nEOF\necho b > b.txt")
        assert got["writes"] == ["a.txt", "b.txt"]
        assert got["unclear"] == []

    def test_a_trailing_line_continuation_fails_closed(self):
        # `echo x > \` continued onto the next line: the split leaves line one
        # with a dangling escape shlex refuses, and it visibly writes - so it
        # lands in unclear rather than losing the target.
        assert _unclear("echo x > \\\nfile.gd")


class TestEval:
    def test_a_quoted_eval_payload_is_reanalysed(self):
        assert _writes("eval 'echo x > game/foo.gd'") == ["game/foo.gd"]

    def test_read_only_eval_stays_clean(self):
        got = hook.analyse_bash("eval 'ls -la'")
        assert got == {"writes": [], "unclear": []}

    def test_an_opaque_variable_payload_fails_closed(self):
        # CMD='rm -rf game'; eval "$CMD" - the inner pass sees a program named
        # $CMD, matches no table, and used to report nothing at all.
        assert _unclear("CMD='rm -rf game'\neval \"$CMD\"")
        assert _unclear('eval "$CMD"')

    def test_an_opaque_program_after_a_separator_fails_closed(self):
        assert _unclear("eval 'ls; $CMD'")

    def test_bash_dash_c_with_a_variable_payload_fails_closed(self):
        assert _unclear('bash -c "$CMD"')
        assert _unclear('sh -c "`cat cmds`"')

    def test_expansions_in_arguments_alone_stay_clean(self):
        # The program is readable and writes nothing; an expanded ARGUMENT to a
        # read-only command is not a write shape and must not dam the session.
        got = hook.analyse_bash("eval 'echo $HOME'")
        assert got == {"writes": [], "unclear": []}


class TestUnresolvableTargets:
    def test_a_dollar_target_is_unclear_not_a_literal_write(self):
        # `echo x > $F` was recorded as a write to an in-project file named $F
        # and passed containment while the real target could be any tree.
        got = hook.analyse_bash("echo x > $F")
        assert got["writes"] == []
        assert got["unclear"]

    def test_a_backtick_target_is_unclear(self):
        assert _writes("echo x > `mktemp`") == []
        assert _unclear("echo x > `mktemp`")

    def test_a_tilde_target_is_unclear(self):
        # The hook resolves relative targets against the session cwd; a ~ is
        # neither relative nor absolute to it, so guessing would misplace it.
        assert _writes("echo x > ~/notes.txt") == []
        assert _unclear("echo x > ~/notes.txt")

    def test_inside_an_embedded_shell_too(self):
        assert _writes("sh -c 'echo x > $F'") == []
        assert _unclear("sh -c 'echo x > $F'")


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


class TestCdEscapesAreContained:
    """The parser's cd model has to reach the containment gate, not just the
    analysis dict: ``cd ../other-game && echo x > game/foo.gd`` must be judged
    where the shell would actually write, against the PINNED root."""

    @pytest.fixture()
    def other(self, tmp_path_factory):
        from bgate_core import db, project
        path = tmp_path_factory.mktemp("hollow")
        project.init(path, "Hollow")
        db.close_all()
        return path

    def test_a_cd_then_relative_write_is_refused_by_containment(
            self, root, other, monkeypatch):
        # The allowlist is emptied because pytest's tmp dirs live under the
        # system temp directory, which the real allowlist permits.
        from bgate_core import aegis
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        monkeypatch.setenv("BGATE_ROOT", str(root))
        monkeypatch.setenv("BGATE_AEGIS", "block")
        monkeypatch.setattr(aegis, "allowlist_dirs", list)
        rel = os.path.relpath(other, root).replace("\\", "/")
        code, msg = hook.decide(
            {"tool_name": "Bash", "cwd": str(root),
             "tool_input": {"command":
                            f"cd {rel} && echo x > game/scripts/player.gd"}},
            "gameplay", "item-1", "block")
        assert code == hook.BLOCK
        assert "different Builders Gate project" in msg

    def test_the_same_write_without_the_cd_is_in_lane(self, root, monkeypatch):
        # The control: the refusal above is the cd resolution, not the path.
        from bgate_core import aegis
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        monkeypatch.setenv("BGATE_ROOT", str(root))
        monkeypatch.setenv("BGATE_AEGIS", "block")
        monkeypatch.setattr(aegis, "allowlist_dirs", list)
        code, _ = hook.decide(
            {"tool_name": "Bash", "cwd": str(root),
             "tool_input": {"command": "echo x > game/scripts/player.gd"}},
            "gameplay", "item-1", "block")
        assert code == hook.ALLOW


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
