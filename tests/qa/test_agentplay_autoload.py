"""A bot run whose BGateAgent autoload failed to parse ends with the parse error.

BGateAgent is what quits the game at BGATE_AUTOQUIT. Without it the game runs
to the wall cap (ten times the game seconds plus a minute) and the run reads as
a bot that never decided.
"""
from __future__ import annotations

import sys
import time

from bgate_core.qa import agentplay
from bgate_core.runtime import proc as _proc

FAILURE = (
    "SCRIPT ERROR: Parse Error: The variable type is being inferred from a "
    "Variant value, so it will be typed as Variant. (Warning treated as error.)\n"
    "   at: GDScript::reload (res://addons/bgate/bgate_agent.gd:102)\n"
    'ERROR: Failed to load script "res://addons/bgate/bgate_agent.gd" with '
    'error "Parse error".\n')


def test_a_broken_agent_autoload_ends_the_run_with_its_parse_error(tmp_path,
                                                                   monkeypatch):
    (tmp_path / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    child = tmp_path / "game.py"
    child.write_text(f"import sys, time\nsys.stdout.write({FAILURE!r})\n"
                     "sys.stdout.flush()\ntime.sleep(120)\n", encoding="utf-8")
    monkeypatch.setattr(agentplay, "install", lambda root: {"placed": False})
    monkeypatch.setattr("bgate_adapters.godot.find_godot", lambda *a, **k: "godot")

    # Through runtime.proc.popen, so the child gets its own session: kill_tree
    # signals the child's process GROUP on POSIX, and a bare Popen shares
    # pytest's, which on the Linux CI runner killed the runner itself.
    def fake_popen(args, **kwargs):
        return _proc.popen([sys.executable, str(child)], **kwargs)

    monkeypatch.setattr(agentplay, "_popen", fake_popen)
    t0 = time.monotonic()
    got = agentplay.run(str(tmp_path), goal="play", seconds=30, policy="random")
    assert time.monotonic() - t0 < 30
    assert got["ok"] is False and got["autoload_failed"] is True, got
    assert got["parse_error"] == {
        "message": got["parse_error"]["message"],
        "file": "res://addons/bgate/bgate_agent.gd", "line": 102}
    assert got["timed_out"] is False
