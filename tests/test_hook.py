"""The PreToolUse hook — enforcement with the fail-safe rule.

Two properties matter more than the happy path: it must fail OPEN on anything
unexpected (a crashing hook dams every write in a session), and it must stay
inert outside its jurisdiction (no seat adopted, not a bgate project, not a
file-writing tool).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


from bgate_cli import hook
from bgate_cli import session
from bgate_cli.main import install_hook
from bgate_cli.main import main as cli_main
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


class TestDirectorMode:
    """The seatless session — which used to be the one nothing checked.

    `if not seat: return ALLOW` was right that a hand-started session adopts no
    seat and wrong about what follows. It holds the DIRECTOR seat, and the thing
    worth checking on it is not its lane (writing game/** from the top level is
    ordinary) but whether another live run is already in the file. Two seatless
    sessions edited one file in one afternoon and neither was told, because
    leases key on an execution identity and a seatless session had none.
    """

    def _judge(self, root, path, sid, mode="collide", seat="", owner=""):
        payload = {"tool_name": "Write", "cwd": str(root), "session_id": sid,
                   "tool_input": {"file_path": str(path)}}
        return hook.decide(payload, seat or hook.DIRECTOR_SEAT,
                           owner or hook.session_owner(payload), mode)

    def test_two_seatless_sessions_cannot_share_a_file(self, root):
        """THE REGRESSION, named. First writer takes the lease, second is told."""
        target = root / "game" / "scripts" / "server.gd"
        assert self._judge(root, target, "aaaa-1111")[0] == hook.ALLOW
        code, message = self._judge(root, target, "bbbb-2222")
        assert code == hook.BLOCK
        # It must NAME the holder — "someone is in this file" that cannot say who
        # is a message you cannot act on.
        assert "session:aaaa-1111" in message
        assert "editing this file right now" in message

    def test_the_lease_holder_is_not_blocked_by_its_own_claim(self, root):
        target = root / "game" / "scripts" / "server.gd"
        assert self._judge(root, target, "aaaa-1111")[0] == hook.ALLOW
        assert self._judge(root, target, "aaaa-1111")[0] == hook.ALLOW

    def test_a_different_file_is_never_a_collision(self, root):
        self._judge(root, root / "game" / "scripts" / "a.gd", "aaaa-1111")
        assert self._judge(root, root / "game" / "scripts" / "b.gd",
                           "bbbb-2222")[0] == hook.ALLOW

    def test_collide_mode_does_not_enforce_the_directors_lane(self, root):
        """The default must not break a workflow that was legal yesterday.

        The director's lane is design/**, so enforcing it would refuse every
        game/** write a top-level session makes. A gate people switch off is
        worth less than a quieter one they leave on.
        """
        code, _ = self._judge(root, root / "game" / "scripts" / "x.gd", "cccc-3333")
        assert code == hook.ALLOW

    def test_warn_mode_reports_the_lane_without_blocking(self, root):
        code, message = self._judge(root, root / "game" / "scripts" / "x.gd",
                                    "cccc-3333", mode="warn")
        # Exit 1: non-blocking, stderr goes to the HUMAN. The write still lands.
        assert code == hook.WARN
        assert "DIRECTOR seat" in message and "queue_add" in message

    def test_block_mode_refuses_the_lane(self, root):
        code, message = self._judge(root, root / "game" / "scripts" / "x.gd",
                                    "cccc-3333", mode="block")
        assert code == hook.BLOCK
        assert "outside director's lanes" in message

    def test_a_collision_blocks_even_in_warn_mode(self, root):
        """warn softens the LANE. It does not soften a second live writer."""
        target = root / "game" / "scripts" / "shared.gd"
        self._judge(root, target, "aaaa-1111", mode="warn")
        assert self._judge(root, target, "bbbb-2222", mode="warn")[0] == hook.BLOCK

    def test_a_dispatched_agent_collides_with_the_director(self, root):
        """The lease is one namespace, so it works in both directions."""
        target = root / "game" / "scripts" / "shared.gd"
        self._judge(root, target, "eeee-5555")
        code, message = self._judge(root, target, "", mode="block",
                                    seat="gameplay", owner="item-42")
        assert code == hook.BLOCK and "session:eeee-5555" in message

    def test_seated_workers_are_unchanged(self, root):
        """No softening may leak into the agents the gate was written for."""
        code, _ = self._judge(root, root / "game" / "assets" / "x.png", "",
                              mode="block", seat="gameplay", owner="item-1")
        assert code == hook.BLOCK

    def test_mode_is_read_from_the_env_and_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("BGATE_DIRECTOR_MODE", "block")
        assert hook.director_mode() == "block"
        monkeypatch.setenv("BGATE_DIRECTOR_MODE", "OFF")
        assert hook.director_mode() == "off"          # case-insensitive
        monkeypatch.setenv("BGATE_DIRECTOR_MODE", "nonsense")
        assert hook.director_mode() == hook.DEFAULT_DIRECTOR_MODE
        monkeypatch.delenv("BGATE_DIRECTOR_MODE")
        assert hook.director_mode() == "collide"

    def test_off_restores_the_old_inert_behaviour(self, root, monkeypatch):
        """An escape hatch that is exactly the previous semantics, not nearly."""
        monkeypatch.setenv("BGATE_DIRECTOR_MODE", "off")
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {"tool_name": "Write", "cwd": str(root), "session_id": "zzzz",
             "tool_input": {"file_path": str(root / "game" / "scripts" / "x.gd")}})))
        assert hook.main([]) == hook.ALLOW

    def test_a_session_with_no_id_enforces_nothing(self, root, monkeypatch):
        """No identity means a lease would be meaningless and a collision
        unattributable — so do nothing rather than something wrong."""
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.delenv("BGATE_LOCK_OWNER", raising=False)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {"tool_name": "Write", "cwd": str(root),
             "tool_input": {"file_path": str(root / "game" / "scripts" / "x.gd")}})))
        assert hook.main([]) == hook.ALLOW

    def test_status_no_longer_calls_a_live_hook_inert(self, root, monkeypatch):
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        report = hook.selftest(str(root))
        assert report["seated"] is False
        assert report["mode"] == "collide"
        assert report["enforcing"] is True
        assert "DIRECTOR" in report["reason"]
        # It says what IS enforced and what is not, rather than "inert".
        assert "leases ARE taken" in report["reason"]
        assert "nothing is being enforced" not in report["reason"]

    def test_status_still_says_inert_when_it_really_is(self, root, monkeypatch):
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.setenv("BGATE_DIRECTOR_MODE", "off")
        report = hook.selftest(str(root))
        assert report["enforcing"] is False
        assert "inert" in report["reason"]


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


class TestUserScopeInstall:
    """ONE INSTALL, EVERY PROJECT — including projects that do not exist yet.

    Per-project install made enforcement a switch you had to remember to flip in
    each new repo, which meant it was off exactly when a fresh project needed it
    most. The handler never justified that: it resolves the project by walking up
    from the file being written, so it was always machine-wide code wearing a
    per-repo switch.
    """

    def test_writes_to_home_not_to_a_project(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Path.home() on Windows
        monkeypatch.setenv("HOME", str(tmp_path))          # ...and everywhere else
        got = install_hook(".", scope="user")
        assert got["ok"] and got["installed"] and got["scope"] == "user"
        assert Path(got["settings"]) == tmp_path / ".claude" / "settings.json"
        assert (tmp_path / ".claude" / "settings.json").exists()

    def test_user_scope_pins_the_interpreter(self, tmp_path, monkeypatch):
        """The opposite rule from project scope, and the reason is the file.

        The project copy is COMMITTED, so `python -m` is right there: an absolute
        path would bake one machine's venv into every checkout. ~/.claude is
        committed nowhere, and a bare `python` there resolves against whatever is
        first on PATH when the hook fires — routinely not the environment bgate
        lives in. The hook then dies on ModuleNotFoundError, FAILS OPEN, and
        enforcement stops with no symptom but a line in hook.log.
        """
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        got = install_hook(".", scope="user")
        assert sys.executable in got["command"]
        assert not got["command"].startswith("python ")
        # Quoted, because the interpreter path contains spaces on a default
        # Windows install ("Program Files", "Local\\Microsoft\\WindowsApps").
        assert got["command"].startswith('"')

    def test_project_scope_still_uses_python_m(self, tmp_path):
        got = install_hook(str(tmp_path), scope="project")
        assert got["command"] == "python -m bgate_cli.hook"
        assert sys.executable not in got["command"]

    def test_user_scope_merges_and_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "my-precious-hook"}]}]},
            "env": {"MY_VAR": "1"}}))
        install_hook(".", scope="user")
        again = install_hook(".", scope="user")
        assert not again["installed"] and not again["updated"]
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        commands = [h["command"] for e in settings["hooks"]["PreToolUse"]
                    for h in e["hooks"]]
        assert "my-precious-hook" in commands
        assert sum("bgate_cli.hook" in c for c in commands) == 1
        assert settings["env"] == {"MY_VAR": "1"}

    def test_unknown_scope_refuses_rather_than_guessing(self, tmp_path):
        got = install_hook(str(tmp_path), scope="global")
        assert got["ok"] is False and "scope" in got["error"]
        assert not (tmp_path / ".claude").exists()

    def test_argv_scope_flag_does_not_eat_its_own_value(self, tmp_path, monkeypatch,
                                                        capsys):
        """`--scope project ./game` must not install into ./project.

        The positional scan drops every `-` token, so without also dropping the
        flag's VALUE the word "project" is the first positional and becomes the
        target directory.
        """
        monkeypatch.setattr(sys, "argv",
                            ["bgate", "hook-install", "--scope", "project",
                             str(tmp_path)])
        assert cli_main() == 0
        got = json.loads(capsys.readouterr().out)
        assert Path(got["settings"]) == tmp_path / ".claude" / "settings.json"


class TestSessionStart:
    """The board state a static string structurally cannot carry.

    The MCP `instructions` field is fixed when the stdio server boots, so it can
    state the director's ROLE and never the SITUATION: what is queued, whether
    the dashboard is even up to run it, who else is holding a file right now. A
    session that has to ask three questions before it can act will skip them.
    """

    def test_silent_outside_a_builders_gate_project(self, tmp_path):
        # It installs at USER scope, so it runs for every session on the machine.
        # Most of those are not games and must not be told about one.
        assert session.build_context(str(tmp_path)) == ""

    def test_names_the_project_and_the_board(self, root):
        text = session.build_context(str(root))
        assert "BUILDERS GATE" in text and "ROOT" in text and "BOARD" in text

    def test_says_when_the_board_cannot_run_anything(self, root, monkeypatch):
        """The trap: a queued row on a dead dashboard looks like delegated work.

        `bgate serve` down is the difference between 'dispatched' and 'parked',
        and it is invisible from the queue itself.
        """
        from bgate_core import queue as _queue
        _queue.add(root, "art", "Do a thing")
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: False)
        text = session.build_context(str(root))
        assert "DOWN" in text and "parked" in text
        assert "Do a thing" in text

        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: True)
        assert "UP" in session.build_context(str(root))

    def test_surfaces_who_else_is_in_a_file(self, root, monkeypatch):
        """THE REGRESSION THIS EXISTS FOR. Two sessions, one module, one
        afternoon, no warning — because nothing ever told either of them that
        the other was working. A lease is worth little if you learn about it
        only after you have written the file."""
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: False)
        assets.acquire_path_lease(root, "game/scripts/combat.gd",
                                  "gameplay", "item-7")
        text = session.build_context(str(root))
        assert "LIVE" in text
        assert "game/scripts/combat.gd" in text and "item-7" in text
        assert "coordinate" in text

    def test_reports_the_enforcement_mode(self, root, monkeypatch):
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.setenv("BGATE_DIRECTOR_MODE", "block")
        assert "director mode = block" in session.build_context(str(root))

    def test_a_broken_lookup_prints_nothing_rather_than_crashing(self, root,
                                                                 monkeypatch):
        """This runs ONCE, before the first turn. A PreToolUse crash costs one
        tool call; a crash here costs the whole session."""
        monkeypatch.setattr(session, "_lines",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert session.build_context(str(root)) == ""

    def test_emits_the_documented_hook_envelope(self, root, capsys, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(
            json.dumps({"cwd": str(root), "hook_event_name": "SessionStart"})))
        assert session.main([]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "BUILDERS GATE" in payload["hookSpecificOutput"]["additionalContext"]

    def test_emits_nothing_at_all_outside_a_project(self, tmp_path, capsys,
                                                    monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(
            json.dumps({"cwd": str(tmp_path)})))
        assert session.main([]) == 0
        assert capsys.readouterr().out.strip() == ""

    def test_a_bgate_dir_with_no_project_says_so_and_names_the_games(
            self, tmp_path, monkeypatch):
        """THE TEN MINUTES THIS BLOCK EXISTS TO BUY BACK.

        A .bgate with no project row is the Builders Gate checkout itself, and a
        session starting there was handed an empty board for a root that is not a
        game — which reads exactly like "the game has nothing on it". The session
        then spent its first four turns grepping the desktop for the project it
        had been asked about by name.
        """
        from bgate_core import db, project
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / db.DB_DIRNAME).mkdir()
        db.connect(checkout)                       # a schema, deliberately no project
        game = tmp_path / "a-real-game"
        game.mkdir()
        project.init(game, "Corporate Quest")
        monkeypatch.setattr(session, "_temp_dir", lambda: "")   # fixtures live in temp
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: False)

        text = session.build_context(str(checkout))
        assert "NO GAME" in text
        assert "project_dir" in text
        assert "Corporate Quest" in text and str(game) in text
        db.close_all()

    def test_the_temp_filter_survives_two_spellings_of_one_directory(
            self, tmp_path, monkeypatch):
        """CAUGHT BY CI, NOT BY THIS SUITE, WHICH IS THE POINT OF THE TEST.

        A GitHub Actions runner reports the temp directory as
        `C:\\Users\\RUNNER~1\\AppData\\Local\\Temp` while every path the process
        builds says `C:\\Users\\runneradmin\\...`. normcase and normpath fix
        slashes and case and leave the 8.3 short name alone, so the prefix test
        never matched and a fixture project was listed as a real game. On a
        developer machine both spellings are already long, so it passed.

        The 8.3 form cannot be manufactured portably, so this asserts the
        property that fixes it: two spellings of ONE directory must compare
        equal after `_real`.
        """
        from bgate_core import db, project
        fixture = tmp_path / "fixture-game"
        fixture.mkdir()
        project.init(fixture, "Fixture Game")
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / db.DB_DIRNAME).mkdir()
        db.connect(checkout)

        # The same directory, spelled the way a different API would hand it
        # back: trailing separator, a redundant `.` segment, and case variation
        # ONLY WHERE CASE IS NOT PART OF THE NAME.
        #
        # Uppercasing is another spelling of one directory on Windows and macOS
        # and a different directory on Linux, where `/TMP/pytest-of-...` simply
        # does not exist — so applying it unconditionally asserted that _real
        # resolves two paths that are not the same path to the same string,
        # which is neither true nor wanted. That is what failed the advisory
        # Linux job. Probed rather than keyed on sys.platform, because the
        # filesystem decides this, not the OS.
        spelled = str(tmp_path)
        if os.path.exists(spelled.upper()):
            spelled = spelled.upper()
        spelled += os.sep + "." + os.sep
        monkeypatch.setattr(session, "_temp_dir", lambda: session._real(spelled))
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: False)

        text = session.build_context(str(checkout))
        assert "Fixture Game" not in text, (
            "two spellings of one directory did not compare equal — the fixture "
            "filter is back to a raw string prefix test")
        db.close_all()

    def test_fixture_projects_in_temp_do_not_crowd_out_the_real_one(
            self, tmp_path, monkeypatch):
        """The registry is machine-wide and the suite writes to it. Four dead
        pytest tmpdirs ahead of the one game is a list that answers nothing."""
        from bgate_core import db, project
        fixture = tmp_path / "fixture-game"
        fixture.mkdir()
        project.init(fixture, "Fixture Game")      # registered, and inside temp
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / db.DB_DIRNAME).mkdir()
        db.connect(checkout)
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: False)
        # The filter must drop it — an honest "nothing registered" beats a list
        # of four dead fixtures with the real game hidden under "...and 1 more".
        text = session.build_context(str(checkout))
        assert "Fixture Game" not in text
        assert "no other project is registered" in text
        db.close_all()

    def test_a_board_serving_another_root_is_not_this_project_s_board(
            self, root, monkeypatch):
        """A dashboard is per-root: it dispatches for ONE project. `something is
        listening on 7788` therefore does not mean `your queued item will run`,
        and a session that read the port as its own queued work onto a board that
        was never going to pick it up."""
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: True)
        monkeypatch.setattr(session, "_board_root", lambda *a, **k: r"D:\some\other\game")
        text = session.build_context(str(root))
        assert "SERVING ANOTHER ROOT" in text and "will NOT dispatch" in text

        monkeypatch.setattr(session, "_board_root", lambda *a, **k: str(root))
        assert "SERVING ANOTHER ROOT" not in session.build_context(str(root))

    def test_an_up_board_that_still_dispatches_nothing_says_why(
            self, root, monkeypatch):
        """Autopilot is a persisted switch that survives a restart OFF, and
        dispatch() refuses outright on a dirty tree. Either one makes 'the board
        is UP' a lie, and neither is visible from the queue."""
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: True)
        monkeypatch.setattr(session, "_board_root", lambda *a, **k: str(root))
        from bgate_core import gitwork
        monkeypatch.setattr(gitwork, "dirty",
                            lambda *a, **k: {"available": True, "dirty": True,
                                             "paths": ["game/scripts/a.gd"]})
        text = session.build_context(str(root))
        assert "autopilot is OFF" in text
        assert "DIRTY" in text and "allow_dirty" in text

        from bgate_core import workspace
        workspace.set(root, "director", "autopilot", {"on": True})
        monkeypatch.setattr(gitwork, "dirty",
                            lambda *a, **k: {"available": True, "dirty": False,
                                             "paths": []})
        clean = session.build_context(str(root))
        assert "autopilot is OFF" not in clean and "DIRTY" not in clean

    def test_a_project_root_registers_itself_for_the_next_session(
            self, root, monkeypatch):
        """Discovery only works if the registry knows the game. It was written by
        init/adopt/select alone, so a game worked on for a week could still be
        missing from it — and then the block above has nothing to offer."""
        from bgate_core import project
        reg = project.user_dir() / "projects.json"
        reg.unlink(missing_ok=True)
        monkeypatch.setattr(session, "_serve_is_up", lambda *a, **k: False)
        session.build_context(str(root))
        assert str(root.resolve()) in json.loads(reg.read_text()).values()

    def test_install_registers_both_events(self, tmp_path):
        got = install_hook(str(tmp_path), scope="project")
        assert set(got["events_installed"]) == {"PreToolUse", "SessionStart"}
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert "bgate_cli.session" in \
            settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        # clear/compact are on the matcher deliberately: those are exactly the
        # moments the context is discarded and the board has to arrive again.
        matcher = settings["hooks"]["SessionStart"][0]["matcher"]
        assert "clear" in matcher and "compact" in matcher

    def test_an_older_install_gains_sessionstart_without_duplicating(self, tmp_path):
        """The upgrade path: a project already carrying only the PreToolUse gate."""
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"PreToolUse": [{
                "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                "hooks": [{"type": "command",
                           "command": "python -m bgate_cli.hook"}]}]}}))
        got = install_hook(str(tmp_path), scope="project")
        assert got["events_installed"] == ["SessionStart"]
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert len(settings["hooks"]["PreToolUse"]) == 1
        assert len(settings["hooks"]["SessionStart"]) == 1


class TestHarnessMetadataLane:
    """The system's own instruction was unfollowable wherever the gate was on.

    Every seat's rules end with the WORK MANIFEST — append to
    .bgate/progress/<task>.jsonl after every completed unit of work — and no
    seat's write_globs contain .bgate/**. So the hook refused it for all seven,
    and the agent was left choosing between the rule it was handed and the gate
    in front of it. It stayed hidden because the gate only bites where it is
    installed; a project without .claude/settings.json let the write through, so
    the trail existed and the contradiction never surfaced.
    """

    def _judge(self, root, rel, role="qa", owner="item-5"):
        import os
        payload = {"tool_name": "Write", "cwd": str(root), "session_id": "s",
                   "tool_input": {"file_path": os.path.join(root, *rel.split("/"))}}
        return hook.decide(payload, role, owner, "block")[0]

    @pytest.mark.parametrize("role", ["qa", "art", "gameplay", "tech",
                                      "narrative", "audio", "director"])
    def test_every_seat_can_keep_its_instructed_trail(self, root, role):
        assert self._judge(root, ".bgate/progress/item-5.jsonl",
                           role=role) == hook.ALLOW

    def test_every_seat_can_append_to_the_project_thread(self, root):
        assert self._judge(root, ".bgate/handoff/thread.jsonl") == hook.ALLOW

    @pytest.mark.parametrize("rel", [
        ".bgate/game.db",              # the store — must go through the tools,
                                       # or the ledger and versioned writes are bypassed
        ".bgate/ui-token",             # the dashboard bearer token, written 0600
        ".bgate/agents/item-5.log",    # run logs
        ".bgate/notify.jsonl",
        ".bgate/../escape.txt",        # the carve-out must not be a way out of it
    ])
    def test_the_carve_out_is_not_a_hole(self, root, rel):
        """NARROW BY CONSTRUCTION. `.bgate/**` would have handed every seat the
        dashboard auth token and a way to corrupt the DB behind its own API."""
        assert self._judge(root, rel) == hook.BLOCK

    def test_an_unknown_seat_still_fails_closed_on_metadata(self, root):
        """The carve-out sits AFTER the unknown-seat check deliberately, so it
        never becomes a way for an unidentified caller to write anything."""
        assert self._judge(root, ".bgate/progress/item-5.jsonl",
                           role="nonexistent") == hook.BLOCK

    def test_the_shared_thread_is_not_leased(self, root):
        """handoff/thread.jsonl is ONE append-only file concurrent agents are
        meant to share. Leasing it would make the second agent's note a blocked
        write — the opposite of what an append-only log is for. A lease stops a
        silent overwrite, and appending a line overwrites nothing."""
        assert self._judge(root, ".bgate/handoff/thread.jsonl",
                           owner="item-1") == hook.ALLOW
        assert self._judge(root, ".bgate/handoff/thread.jsonl",
                           owner="item-2") == hook.ALLOW

    def test_ordinary_files_still_lease(self, root):
        """The control: if this passes too, the lease was switched off wholesale."""
        assert self._judge(root, "game/scripts/x.gd", role="gameplay",
                           owner="item-1") == hook.ALLOW
        assert self._judge(root, "game/scripts/x.gd", role="gameplay",
                           owner="item-2") == hook.BLOCK

    def test_the_rule_and_the_lane_now_agree(self, root):
        """Bind the two together so they cannot drift apart again: the path named
        in the WORK MANIFEST rule must be one the oracle permits."""
        from bgate_core import seats
        brief = seats.brief(root, "qa")
        manifest = next(r for r in brief["rules"] if r.startswith("WORK MANIFEST"))
        assert ".bgate/progress/" in manifest
        assert seats.can_write(root, "qa", ".bgate/progress/item-1.jsonl")["allowed"]


class TestRefusalsRoute:
    """A refusal that only names the wall teaches an agent to stop.

    The observed pattern on a real board: a worker hit its lane, correctly
    refused to trespass, wrote a LEFTOVERS block or a seat note, and closed —
    and nothing was ever queued for the seat that owned the path. The refusal
    is the one message guaranteed to arrive at that exact moment, so it now
    carries the route: which seat owns the path, and the queue_add call that
    hands the work over.
    """

    def test_a_lane_refusal_names_the_owning_seat_and_the_handoff(self, root):
        # gameplay writing an asset: art's game/assets/** is the most specific
        # owner and must be named ahead of tech's blanket game/**.
        code, msg = hook.decide(payload("Write", str(root / "game/assets/rock.png")),
                                "gameplay")
        assert code == hook.BLOCK
        assert "queue_add('art'" in msg
        assert "depends_on" in msg
        assert "Do NOT stop" in msg

    def test_the_most_specific_lane_wins_over_a_blanket_one(self, root):
        # audio's game/assets/audio/** beats art's game/assets/** and
        # tech's game/**.
        code, msg = hook.decide(
            payload("Write", str(root / "game/assets/audio/hit.wav")), "gameplay")
        assert code == hook.BLOCK
        assert "queue_add('audio'" in msg

    def test_an_unowned_path_names_the_layout_mismatch_not_a_dead_route(self, root):
        # Routing this to the director was wrong: its lane is design/**, so the
        # escalation target cannot write it either. An adopted repo (or one
        # `bgate init` scaffolded into <root>) hits this for EVERY path.
        code, msg = hook.decide(payload("Write", str(root / "orphan/nobody.txt")),
                                "gameplay")
        assert code == hook.BLOCK
        assert "seat_configure" in msg
        assert "layout" in msg
        assert "queue_add('director'" not in msg

    def test_a_lease_refusal_suggests_depending_on_the_holding_item(self, root):
        target = root / "game" / "scripts" / "shared.gd"
        data = {"tool_name": "Write", "cwd": str(root),
                "tool_input": {"file_path": str(target)}}
        assert hook.decide(data, "gameplay", "item-12")[0] == hook.ALLOW
        code, msg = hook.decide(data, "tech", "item-13")
        assert code == hook.BLOCK
        assert "depends_on=12" in msg
        assert "do not poll" in msg

    def test_the_directors_refusal_is_unchanged(self, root):
        # The director's tail predates this and is asserted elsewhere; this
        # guards that the worker route did not replace it.
        data = {"tool_name": "Write", "cwd": str(root), "session_id": "dddd-4444",
                "tool_input": {"file_path": str(root / "game/scripts/x.gd")}}
        code, msg = hook.decide(data, hook.DIRECTOR_SEAT,
                                hook.session_owner(data), "block")
        assert code == hook.BLOCK
        assert "queue_add(seat, ...)" in msg
