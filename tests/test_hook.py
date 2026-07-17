"""The PreToolUse hook — enforcement with the fail-safe rule.

Two properties matter more than the happy path: it must fail OPEN on anything
unexpected (a crashing hook dams every write in a session), and it must stay
inert outside its jurisdiction (no seat adopted, not a bgate project, not a
file-writing tool).
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from bgate_cli import hook
from bgate_cli.main import install_hook
from bgate_core import assets


def payload(tool: str, path: str, cwd: str = "") -> dict:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"tool_name": tool, "tool_input": {key: path}, "cwd": cwd}


class TestDecide:
    def test_in_lane_write_allowed(self, root):
        code, _ = hook.decide(payload("Write", str(root / "game/scripts/player.gd")),
                              "gameplay")
        assert code == hook.ALLOW

    def test_out_of_lane_write_blocked_with_guidance(self, root):
        code, msg = hook.decide(payload("Write", str(root / "game/assets/rock.png")),
                                "gameplay")
        assert code == hook.BLOCK
        assert "gameplay" in msg and "lanes" in msg

    def test_locked_binary_blocks_even_in_lane(self, root):
        assets.lock(root, "game/assets/shard.blend", "art")
        code, msg = hook.decide(payload("Edit", str(root / "game/assets/shard.blend")),
                                "tech")
        assert code == hook.BLOCK
        assert "locked by seat 'art'" in msg

    def test_lock_holder_writes_through(self, root):
        assets.lock(root, "game/assets/shard.blend", "art")
        code, _ = hook.decide(payload("Edit", str(root / "game/assets/shard.blend")),
                              "art")
        assert code == hook.ALLOW

    def test_outside_a_bgate_project_is_not_our_business(self, tmp_path):
        code, _ = hook.decide(payload("Write", str(tmp_path / "anything.py")),
                              "gameplay")
        assert code == hook.ALLOW

    def test_non_write_tools_pass(self, root):
        code, _ = hook.decide({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                              "gameplay")
        assert code == hook.ALLOW

    def test_relative_path_resolves_against_cwd(self, root):
        code, msg = hook.decide(
            payload("Write", "game/assets/rock.png", cwd=str(root)), "gameplay")
        assert code == hook.BLOCK


class TestProcessBoundary:
    """The hook as Claude Code actually runs it: a subprocess fed JSON."""

    def run_hook(self, data: dict, seat: str, cwd: str) -> subprocess.CompletedProcess:
        import os
        env = {**os.environ, "BGATE_SEAT": seat}
        return subprocess.run([sys.executable, "-m", "bgate_cli.hook"],
                              input=json.dumps(data), capture_output=True,
                              text=True, timeout=60, cwd=cwd, env=env)

    def test_block_is_exit_2_with_stderr(self, root):
        got = self.run_hook(payload("Write", str(root / "game/assets/x.png")),
                            "gameplay", str(root))
        assert got.returncode == 2
        assert "builders-gate" in got.stderr

    def test_allow_is_exit_0(self, root):
        got = self.run_hook(payload("Write", str(root / "game/scripts/x.gd")),
                            "gameplay", str(root))
        assert got.returncode == 0

    def test_no_seat_means_inert(self, root):
        import os
        env = {k: v for k, v in os.environ.items() if k != "BGATE_SEAT"}
        got = subprocess.run([sys.executable, "-m", "bgate_cli.hook"],
                             input=json.dumps(payload("Write", str(root / "game/assets/x.png"))),
                             capture_output=True, text=True, timeout=60,
                             cwd=str(root), env=env)
        assert got.returncode == 0

    def test_garbage_stdin_fails_open(self, root):
        import os
        got = subprocess.run([sys.executable, "-m", "bgate_cli.hook"],
                             input="this is not json {{{",
                             capture_output=True, text=True, timeout=60,
                             cwd=str(root),
                             env={**os.environ, "BGATE_SEAT": "gameplay"})
        assert got.returncode == 0  # fail-safe: never dam the session


class TestInstall:
    def test_installs_into_fresh_project(self, tmp_path):
        got = install_hook(str(tmp_path))
        assert got["ok"] and got["installed"]
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        entry = settings["hooks"]["PreToolUse"][0]
        assert "bgate_cli.hook" in entry["hooks"][0]["command"]

    def test_merges_without_clobbering_existing_hooks(self, tmp_path):
        existing = {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-precious-hook"}]}
        ]}, "otherSetting": True}
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text(json.dumps(existing))

        install_hook(str(tmp_path))
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        commands = [h["command"] for e in settings["hooks"]["PreToolUse"]
                    for h in e["hooks"]]
        assert "my-precious-hook" in commands
        assert any("bgate_cli.hook" in c for c in commands)
        assert settings["otherSetting"] is True

    def test_idempotent(self, tmp_path):
        install_hook(str(tmp_path))
        got = install_hook(str(tmp_path))
        assert got["installed"] is False
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert len(settings["hooks"]["PreToolUse"]) == 1

    def test_refuses_to_overwrite_corrupt_settings(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text("{corrupt")
        got = install_hook(str(tmp_path))
        assert got["ok"] is False
        assert (tmp_path / ".claude" / "settings.json").read_text() == "{corrupt"
