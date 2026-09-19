"""The egress gate: a seat cannot install, fetch, push or spawn an agent.

The Codex runner used to hand every worker `--approve-for-me` - a second
model reviewing the first one's request to leave the sandbox. This gate is the
deterministic replacement: the program is on the list or it is not, and the
refusal is a sentence the agent can act on. It applies to a SEATED session
only; a human's own session installs what it likes.

Also pinned here: the payload shapes Codex actually sends (MEASURED, 0.154),
and the answer shape Codex actually honours - stdout JSON, not exit 2.
"""
from __future__ import annotations

import json
import os

import pytest

from bgate_cli import hook


class TestBashEgress:
    @pytest.mark.parametrize("command", [
        "pip install requests",
        "python -m pip install x",
        "python3 -m pip install --user x",
        "sudo apt-get install -y ffmpeg",
        "npm install left-pad",
        "npm i",
        "npx cowsay hi",
        "yarn add x",
        "cargo install ripgrep",
        "go get github.com/x/y",
        "uv pip install x",
        "winget install Git.Git",
        "choco install ffmpeg",
        "git push origin main",
        "git clone https://x/y",
        "git fetch --all",
        # The network clients alone. This data deliberately does NOT spell
        # the paste-and-run idioms (a fetch piped into a shell): Defender
        # matched a command line carrying them as ClickFix, and a test
        # file's job is to prove the gate, not to look like the attack.
        "curl -sL https://x -o f.txt",
        "wget http://x/f.tar.gz",
        "ssh box 'ls'",
        "gh pr create",
        "claude -p 'do stuff'",
        "codex exec 'do stuff'",
        "bash -c 'pip install x'",
        "eval 'wget http://x'",
        "env FOO=1 pip install x",
        "ls && pip install x",
        "ls\npip install x",
        "python -c \"import urllib.request; urllib.request.urlopen('http://x')\"",
        "powershell -Command \"Invoke-WebRequest http://x -OutFile a.zip\"",
        "pwsh -Command 'Install-Module Pester'",
        # Obfuscation is not a way past: an encoded payload exists only to be
        # unreadable here, and unreadable is refused.
        "powershell -EncodedCommand aQB3AHIAIABoAHQAdABwADoALwAvAHgA",
        "powershell -enc aQB3AHIA",
        "pwsh -e aQB3AHIA",
        # Versioned interpreters were a hole: only `python`/`python3` were
        # in the interpreter table.
        "python3.12 -m pip install x",
        "python3.11 -m ensurepip",
    ])
    def test_refused(self, command):
        assert hook.egress_hits(command), command

    @pytest.mark.parametrize("command", [
        "npm run build",
        "npm test",
        "npx --no-install vite build",
        "git status && git commit -am x",
        "git diff",
        "cargo build --release",
        "go build ./...",
        "python tools/build.py",
        "godot --headless --path game --script res://tools/x.gd",
        "powershell -Command \"Get-Content a.txt\"",
        "echo pip install",
        "cat README.md | grep pip",
        "python -c \"print(1)\"",
        "uv run pytest",
    ])
    def test_local_work_passes(self, command):
        assert hook.egress_hits(command) == [], command


class TestPowerShellEgress:
    @pytest.mark.parametrize("command", [
        "Invoke-WebRequest http://x -OutFile a",
        "iwr http://x",
        "Invoke-Expression $script",
        "& pip install x",
        "Start-Process pip -ArgumentList install,x",
        "python -m pip install x",
        "npm run build; npm install foo",
        "Install-Module Pester",
        "New-Object System.Net.WebClient",
    ])
    def test_refused(self, command):
        assert hook.egress_hits_powershell(command), command

    @pytest.mark.parametrize("command", [
        "Get-Content a.txt",
        "New-Item -ItemType Directory x",
        "npm run build",
        "git status",
    ])
    def test_local_work_passes(self, command):
        assert hook.egress_hits_powershell(command) == [], command


@pytest.fixture(autouse=True)
def _outside_any_project(tmp_path, monkeypatch):
    """Every decision here is judged from a bare temp dir. The checkout's
    own .bgate is a real project: a director-session test run from it took
    a lease on `a.txt` that then blocked the next test with a lock message
    instead of the gate under test."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BGATE_ROOT", raising=False)


def _payload(command, **extra):
    return {"tool_name": "Bash", "tool_input": {"command": command},
            "cwd": os.getcwd(), "session_id": "s1", **extra}


class TestDecision:
    def test_a_seat_is_refused(self, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "art")
        monkeypatch.delenv("BGATE_ALLOW_EGRESS", raising=False)
        code, message = hook.decide(_payload("pip install requests"), "art",
                                    "item-1", "warn")
        assert code == hook.BLOCK
        assert "pip install requests" in message
        assert "next_approach" in message

    def test_a_seat_sees_the_list_form_codex_sends(self, monkeypatch):
        """Codex hands the shell tool's command as an argv list."""
        monkeypatch.setenv("BGATE_SEAT", "art")
        monkeypatch.delenv("BGATE_ALLOW_EGRESS", raising=False)
        code, _ = hook.decide(_payload(["pip", "install", "requests"]), "art",
                              "item-1", "warn")
        assert code == hook.BLOCK

    def test_the_human_session_is_not_gated(self, monkeypatch):
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        code, _ = hook.decide(_payload("pip install requests"),
                              hook.DIRECTOR_SEAT, "session:abc", "collide")
        assert code == hook.ALLOW

    @pytest.mark.skipif(os.name != "nt", reason="Codex runs Bash-named tools under PowerShell on Windows")
    @pytest.mark.parametrize("command", [
        "iwr http://x",                            # egress, PowerShell spelling
        "Set-Content .bgate_out/p.txt 'hi'",       # inline text, PowerShell spelling
    ])
    def test_a_bash_named_call_on_windows_gets_the_powershell_reading(
            self, monkeypatch, command):
        """MEASURED: a Codex worker's shell tool reports as `Bash` and runs
        PowerShell; judged by the Bash reading alone, both of these ran."""
        monkeypatch.setenv("BGATE_SEAT", "art")
        monkeypatch.delenv("BGATE_ALLOW_EGRESS", raising=False)
        code, _ = hook.decide(_payload(command), "art", "item-1", "warn")
        assert code == hook.BLOCK

    def test_the_machine_override_opens_it(self, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "art")
        monkeypatch.setenv("BGATE_ALLOW_EGRESS", "1")
        code, _ = hook.decide(_payload("pip install requests"), "art",
                              "item-1", "warn")
        assert code == hook.ALLOW


class TestShellContent:
    """A seat's file text goes through Write/Edit, never the shell.

    OBSERVED 2026-09-16: a heredoc writing a test file through the client's
    Bash tool put the file's text on a `bash -c` command line, and Windows
    Defender killed the process as Trojan:Win32/ClickFix. Any agent on any
    user's machine can produce that shape by writing a file through the
    shell; the shape is refused for a seat, and only for a seat.
    """

    @pytest.mark.parametrize("command", [
        "cat > game/a.gd <<'EOF'\nextends Node\nEOF",
        "cat <<EOF > notes.txt\nhello\nEOF",
        "echo 'extends Node' > game/a.gd",
        "printf '%s\\n' line >> log.txt",
        "echo x | tee game/a.gd",
        "bash -c 'echo hi > a.txt'",
        "eval 'echo hi > a.txt'",
        "powershell -Command \"Set-Content a.txt hi\"",
    ])
    def test_inline_text_is_refused(self, command, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "art")
        assert hook.inline_content_writes(command), command
        code, message = hook.decide(_payload(command), "art", "item-1", "warn")
        assert code == hook.BLOCK
        assert "Write/Edit" in message

    @pytest.mark.parametrize("command", [
        "godot --headless --path game > run.log 2>&1",
        "python tools/build.py > build.log",
        "cp a.gd b.gd",
        "python - <<'EOF'\nprint(1)\nEOF",       # a script on stdin, no file
        "python - <<'EOF' 2>&1\nprint(1)\nEOF",  # `2>&1` is a stream, not a file
        "cat game/a.gd",
        "ls > /dev/null",
        "echo done > /dev/null",                 # not a file anyone owns
        "some_cmd || echo skipped > nul",
        "git diff > changes.patch",
    ])
    def test_program_output_still_redirects(self, command):
        assert hook.inline_content_writes(command) == [], command

    @pytest.mark.parametrize("command", [
        "Set-Content -Path a.txt -Value 'hi'",
        "'hi' | Out-File a.txt",
        "Add-Content a.txt 'more'",
        "[IO.File]::WriteAllText('a.txt', 'hi')",
        "echo hi > a.txt",
    ])
    def test_powershell_inline_text_is_refused(self, command, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "art")
        assert hook.inline_content_writes_powershell(command), command
        payload = {"tool_name": "PowerShell", "tool_input": {"command": command},
                   "cwd": os.getcwd(), "session_id": "s1"}
        code, _ = hook.decide(payload, "art", "item-1", "warn")
        assert code == hook.BLOCK

    @pytest.mark.parametrize("command", [
        "Get-Content a.txt",
        "Copy-Item a.gd b.gd",
        "godot --headless --path game *> run.log",
        # Program output piped into a writer is what the refusal text
        # promises stays open.
        "godot --headless --path game | Out-File run.log",
        "& 'C:\\godot.exe' --version | Set-Content ver.txt",
        "Get-Content a.log | Add-Content all.log",
        "echo hi 2>&1",
    ])
    def test_powershell_program_output_passes(self, command):
        assert hook.inline_content_writes_powershell(command) == [], command

    @pytest.mark.parametrize("command", [
        "'hi' | Out-File a.txt",
        "\"hi\" | Set-Content a.txt",
        "@'\nhi\n'@ | Out-File a.txt",
        "$text | Out-File a.txt",
        "echo hi | Out-File a.txt",
        "godot --version | Out-File a.txt -InputObject 'x'",
    ])
    def test_powershell_text_sources_are_refused(self, command):
        assert hook.inline_content_writes_powershell(command), command

    def test_the_human_session_may_still_use_the_shell(self, monkeypatch):
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        code, _ = hook.decide(_payload("echo hi > a.txt"),
                              hook.DIRECTOR_SEAT, "session:abc", "collide")
        assert code != hook.BLOCK


class TestCodexPayloads:
    def test_apply_patch_targets_are_read_from_the_patch_text(self):
        patch = ("*** Begin Patch\n*** Update File: game/scripts/a.gd\n@@\n-x\n+y\n"
                 "*** Add File: game/b.gd\n+z\n*** Delete File: c.txt\n"
                 "*** Move to: d.txt\n*** End Patch")
        assert hook.patch_targets(patch) == [
            "game/scripts/a.gd", "game/b.gd", "c.txt", "d.txt"]

    def test_apply_patch_arrives_as_command(self, monkeypatch):
        """MEASURED (codex 0.154): tool_input = {"command": "<patch>"}. Every
        file the patch names must reach the write gate, and a refusal on any
        one of them is the verdict."""
        judged: list[str] = []

        def fake_judge(target, payload, seat, owner, mode):
            judged.append(target)
            return (hook.BLOCK, "no") if target.endswith("b.gd") else (hook.ALLOW, "")

        monkeypatch.setattr(hook, "_judge_path", fake_judge)
        payload = {"tool_name": "apply_patch",
                   "tool_input": {"command": "*** Begin Patch\n*** Update File: "
                                             "game/a.gd\n@@\n-x\n+y\n*** Add File: "
                                             "game/b.gd\n+z\n*** End Patch"},
                   "cwd": os.getcwd(), "session_id": "s1", "turn_id": "t1"}
        code, message = hook.decide(payload, "art", "item-1", "warn")
        assert judged == ["game/a.gd", "game/b.gd"]
        assert (code, message) == (hook.BLOCK, "no")

    def test_codex_is_answered_on_stdout_not_by_exit_code(self, capsys):
        """MEASURED: Codex 0.154 ran `pip install` straight through a hook
        that exited 2 with the refusal on stderr, and stopped on the JSON
        permissionDecision form. Claude Code honours exit 2 and ignores
        stdout on it, so the two clients get different answers."""
        assert hook._is_codex({"turn_id": "t"}) is True
        assert hook._is_codex({"session_id": "s"}) is False
        assert hook._answer_codex(hook.BLOCK, "no") == 0
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == "no"
        assert hook._answer_codex(hook.WARN, "careful") == 0
        assert json.loads(capsys.readouterr().out) == {"systemMessage": "careful"}
        assert hook._answer_codex(hook.ALLOW, "") == 0
        assert capsys.readouterr().out == ""
