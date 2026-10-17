"""`bgate oneshot`: a brief becomes a running board, with every edge mocked.

The edges are the module-level functions in bgate_cli.oneshot (the director
turn, the detached server, the dispatch wait); everything else - init, git,
hook, settings, seats, verification, promote_slice - runs for real on a tmp
project, because those are the steps the hand-run kickoff got wrong.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bgate_cli import oneshot
from bgate_core.board import queue, seats
from bgate_core.design import domainplan, greenlight
from bgate_core.store import settings

BRIEF = ("A turn-based artillery game: two teams of garden gnomes lob "
         "pinecones across a destructible back yard. Wind, fall damage, water "
         "kills, sudden death after round ten. Chunky cartoon look.")

THESIS = {
    "sentence": "Every turn the player decides whether to take the safe small "
                "hit from cover or walk into the open for the big shot.",
    "options": ["safe hit from cover", "walk out for the big shot"],
    "stakes": "walking out exposes the gnome to every enemy next turn",
    "tension": "wind and terrain change what counts as cover every turn",
    "dominant_strategy": "always lobbing from cover, which the sudden-death "
                         "water rise is there to punish",
    "cadence": "once per turn, every 45 seconds",
}
RISKS = [{"hypothesis": "a bitmap terrain carved by every blast holds 60 fps "
                        "with four teams on screen",
          "retire_by": "godot_test_run on the backyard with 16 craters"}]


def _plans() -> dict:
    goal = "This domain's part of the finished game is complete and plays as "
    return {
        "tech": {"goal": goal + "the engine spine.", "done_when": ["godot_test_run green"],
                 "entries": [{"name": "match_state", "acceptance": "turn order test green",
                              "owns": "whose turn it is", "interfaces": ["turn_started signal"],
                              "slice": True}]},
        "gameplay": {"goal": goal + "the core artillery loop.",
                     "done_when": ["a shot carves terrain in a test"],
                     "entries": [{"name": "lob", "acceptance": "projectile lands within 2px of prediction",
                                  "verb": "lob a pinecone", "feedback": "thunk and crater",
                                  "tunables": ["gravity=400"], "slice": True,
                                  "depends_on": ["match_state"]}]},
        "level": {"goal": goal + "the back yard arena.", "done_when": ["room_audit clean on the yard"],
                  "entries": [{"name": "backyard", "acceptance": "both teams spawn on solid ground",
                               "purpose": "test", "introduces": ["lob"], "slice": True,
                               "depends_on": ["lob"]}]},
        "art": {"goal": goal + "the chunky cartoon look.", "done_when": ["every slice asset scale_check green"],
                "entries": [{"name": "gnome_sheet", "acceptance": "40px gnome reads at 1x",
                             "use": "the player's team", "view": "side-on at 40px", "slice": True}]},
        "animation": {"goal": goal + "the gnome's motion.", "done_when": ["walk and lob clips play in game"],
                      "entries": [{"name": "gnome_lob", "acceptance": "clip plays on lob input",
                                   "character": "gnome_sheet", "plays_on": "lob pressed",
                                   "depends_on": ["gnome_sheet"]}]},
        "ui": {"goal": goal + "the HUD and the turn timer.", "done_when": ["evidence_check_ui passes"],
               "entries": [{"name": "hud", "acceptance": "timer and wind visible in a screenshot",
                            "purpose": "show the turn clock and wind", "shows": ["timer", "wind"],
                            "slice": True}]},
        "audio": {"goal": goal + "the sound of the yard.", "done_when": ["every cue plays in a capture"],
                  "entries": [{"name": "lob_thunk", "acceptance": "plays within 100ms of impact",
                               "type": "sfx", "trigger": "lob"}]},
        "qa": {"goal": goal + "a proven golden path.", "done_when": ["the golden path scenario passes"],
               "entries": [{"name": "one_full_turn", "acceptance": "a turn completes with a hit",
                            "steps": ["boot", "lob", "see the crater"], "covers": ["gameplay"]}]},
    }


def _complete_director(root, prompt):
    """A director that does the whole kickoff in one turn."""
    greenlight.set_thesis(root, THESIS)
    greenlight.set_risks(root, RISKS)
    domainplan.set_plans(root, _plans())
    greenlight.advance(root, "slice")
    return {"ok": True, "text": "planned eight domains\nONESHOT: DONE"}


@pytest.fixture()
def edges(monkeypatch):
    """Stand in for the server and the dispatch wait; record the calls."""
    calls = {"serve": [], "wait": 0, "director": []}
    monkeypatch.setattr(oneshot, "start_serve",
                        lambda root, port: calls["serve"].append((str(root), port)) or
                        {"ok": True, "pid": 4242, "url": f"http://127.0.0.1:{port}",
                         "log": "serve.log"})

    def wait(root, seconds=0, poll_s=0):
        calls["wait"] += 1
        return {"ok": True, "dispatched": [1], "why": "#1 [tech] match_state"}
    monkeypatch.setattr(oneshot, "wait_for_dispatch", wait)
    return calls


def _git_ok(root: Path) -> bool:
    got = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    return got.returncode == 0 and bool(got.stdout.strip())


class TestTheWholePath:
    def test_a_brief_becomes_a_board(self, tmp_path, monkeypatch, edges, capsys):
        brief = tmp_path / "brief.md"
        brief.write_text(BRIEF, encoding="utf-8")
        turns = []

        def director(root, prompt):
            turns.append(prompt)
            return _complete_director(root, prompt)
        monkeypatch.setattr(oneshot, "run_director", director)
        dest = tmp_path / "gnomefall"

        code = oneshot.run("Gnomefall", str(brief), kind="2d", dest=str(dest),
                           port=7791, concurrency=3, auto_advance=True)
        out = capsys.readouterr().out
        assert code == 0, out

        # 1. init + git: a repo with a first commit, the brief inside it
        assert (dest / ".bgate" / "game.db").is_file()
        assert (dest / "design" / "brief.md").read_text(encoding="utf-8").strip() == BRIEF
        assert _git_ok(dest)
        tracked = subprocess.run(["git", "-C", str(dest), "ls-files"],
                                 capture_output=True, text=True).stdout
        assert "design/brief.md" in tracked
        # 2. hook + settings
        hook = json.loads((dest / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "hooks" in hook
        assert settings.get(dest, "dispatch.max_concurrent") == 3
        assert settings.get(dest, "dispatch.allow_dirty") is True
        assert settings.get(dest, "dispatch.max_attempts") == 4
        assert settings.get(dest, "board.brief_chars") == 3000
        assert settings.get(dest, "board.brief_lanes") == 4
        assert settings.get(dest, "limits.max_runtime_s") == 3600
        assert settings.get(dest, "autopilot.stage_advance") is True
        # no story in the brief: the two story seats are off
        roles = seats.roles_for(dest)
        assert "narrative" not in roles and "cinematic" not in roles
        assert "gameplay" in roles
        # 3. one director turn, on a prompt that carries the brief and the order
        assert len(turns) == 1
        assert BRIEF[:40] in turns[0]
        assert "ONE-SHOT" in turns[0] and "domain_plan_set(plans=" in turns[0]
        assert turns[0].index("ONE-SHOT") < turns[0].index("--- BRIEF ---")
        assert "auto-advances" in turns[0]
        assert greenlight.stage(dest) == greenlight.SLICE
        # 4. the slice is on the board and the server was started on the port
        filed = queue.list_items(dest, status="queued")
        assert {i["title"].split(": ", 1)[1] for i in filed} >= {
            "match_state", "lob", "backyard", "gnome_sheet", "hud"}
        assert edges["serve"] == [(str(dest), 7791)]
        assert edges["wait"] == 1
        assert "http://127.0.0.1:7791" in out
        assert "dispatch started" in out

    def test_without_auto_advance_the_switch_stays_off(self, tmp_path, monkeypatch, edges):
        brief = tmp_path / "brief.md"
        brief.write_text(BRIEF, encoding="utf-8")
        monkeypatch.setattr(oneshot, "run_director", _complete_director)
        dest = tmp_path / "plain"
        assert oneshot.run("Plain", str(brief), dest=str(dest)) == 0
        assert settings.get(dest, "autopilot.stage_advance") is False
        assert settings.get(dest, "dispatch.max_concurrent") == 4

    def test_a_story_brief_keeps_the_story_seats(self, tmp_path, monkeypatch, edges):
        brief = tmp_path / "brief.md"
        brief.write_text(BRIEF + " The story follows a gnome exile's return, "
                         "told in cutscenes between rounds.", encoding="utf-8")
        turns = []

        def director(root, prompt):
            turns.append(prompt)
            greenlight.set_thesis(root, THESIS)
            greenlight.set_risks(root, RISKS)
            plans = _plans()
            plans["narrative"] = {
                "goal": "The exile's return is told in four beats between rounds.",
                "done_when": ["every beat lands in a playtest"],
                "entries": [{"name": "beat_return", "acceptance": "the beat shows before round 2",
                             "reveals": "why the gnome left", "delivered_by": "cinematic"}]}
            plans["cinematic"] = {
                "goal": "Two short skippable sequences bookend the match.",
                "done_when": ["both sequences play and skip"],
                "entries": [{"name": "intro_pan", "acceptance": "pans the yard in 4 s, skippable",
                             "trigger": "match start; returns on skip or end",
                             "purpose": "shows the whole yard before the first shot"}]}
            domainplan.set_plans(root, plans)
            greenlight.advance(root, "slice")
            return {"ok": True, "text": "ONESHOT: DONE"}
        monkeypatch.setattr(oneshot, "run_director", director)
        dest = tmp_path / "story"
        assert oneshot.run("Story", str(brief), dest=str(dest)) == 0
        roles = seats.roles_for(dest)
        assert "narrative" in roles and "cinematic" in roles
        assert "narrative, cinematic" in turns[0]

    def test_gaps_are_reprompted_exactly_once(self, tmp_path, monkeypatch, edges, capsys):
        brief = tmp_path / "brief.md"
        brief.write_text(BRIEF, encoding="utf-8")
        turns = []

        def director(root, prompt):
            turns.append(prompt)
            if len(turns) == 1:
                # thesis and six plans, but no risks, no qa, no audio
                greenlight.set_thesis(root, THESIS)
                plans = _plans()
                plans.pop("qa"), plans.pop("audio")
                domainplan.set_plans(root, plans)
                return {"ok": True, "text": "most of it"}
            greenlight.set_risks(root, RISKS)
            domainplan.set_plans(root, {k: v for k, v in _plans().items()
                                        if k in ("qa", "audio")})
            return {"ok": True, "text": "ONESHOT: DONE"}
        monkeypatch.setattr(oneshot, "run_director", director)
        dest = tmp_path / "gappy"
        assert oneshot.run("Gappy", str(brief), dest=str(dest)) == 0
        assert len(turns) == 2
        assert "SECOND PASS" in turns[1]
        assert "no key risks" in turns[1] and "the qa domain has no plan" in turns[1]
        # the director never pressed the button; the command, run by the
        # human, advanced the planned project itself
        assert greenlight.stage(dest) == greenlight.SLICE
        assert "re-prompting once" in capsys.readouterr().out

    def test_a_board_still_broken_after_two_passes_fails_without_serving(
            self, tmp_path, monkeypatch, edges, capsys):
        brief = tmp_path / "brief.md"
        brief.write_text(BRIEF, encoding="utf-8")
        monkeypatch.setattr(oneshot, "run_director",
                            lambda root, prompt: {"ok": True, "text": "I planned nothing"})
        dest = tmp_path / "broken"
        assert oneshot.run("Broken", str(brief), dest=str(dest)) == 1
        out = capsys.readouterr().out
        assert "not ready after two director passes" in out
        assert "no mechanical thesis" in out
        assert edges["serve"] == []
        # the project itself exists for a human to finish
        assert (dest / ".bgate" / "game.db").is_file()

    def test_a_dead_director_is_an_error_not_a_board(self, tmp_path, monkeypatch, edges):
        brief = tmp_path / "brief.md"
        brief.write_text(BRIEF, encoding="utf-8")
        monkeypatch.setattr(oneshot, "run_director",
                            lambda root, prompt: {"ok": False, "text": "claude CLI not found"})
        assert oneshot.run("Dead", str(brief), dest=str(tmp_path / "dead")) == 1
        assert edges["serve"] == []


class TestArguments:
    def test_help_prints_usage(self, monkeypatch, capsys):
        from bgate_cli.main import main as cli_main

        monkeypatch.setattr(sys, "argv", ["bgate", "oneshot", "--help"])
        assert cli_main() == 0
        out = capsys.readouterr().out
        assert "bgate oneshot NAME --brief FILE" in out
        assert "--auto-advance" in out

    def test_no_arguments_is_a_usage_error(self, monkeypatch, capsys):
        from bgate_cli.main import main as cli_main

        monkeypatch.setattr(sys, "argv", ["bgate", "oneshot"])
        assert cli_main() == 2
        assert "bgate oneshot NAME" in capsys.readouterr().out

    def test_the_flags_parse(self):
        got = oneshot.parse(["Gnomefall", "--brief", "b.md", "--kind", "2d",
                             "--engine", "godot", "--dir", "x", "--port", "7791",
                             "--concurrency", "6", "--auto-advance",
                             "--ref", "a.png", "--ref", "b.png"])
        assert got == {"name": "Gnomefall", "brief": "b.md", "kind": "2d",
                       "engine": "godot", "dest": "x", "port": 7791,
                       "concurrency": 6, "auto_advance": True, "without": "",
                       "refs": ["a.png", "b.png"]}

    def test_a_missing_brief_is_refused_before_anything_is_written(self, tmp_path, capsys):
        assert oneshot.run("Nope", str(tmp_path / "missing.md"),
                           dest=str(tmp_path / "nope")) == 2
        assert not (tmp_path / "nope").exists()
        assert "cannot read --brief" in capsys.readouterr().out

    def test_no_brief_flag_is_refused(self, tmp_path, capsys):
        assert oneshot.run("Nope", "", dest=str(tmp_path / "nope")) == 2
        assert "--brief FILE is required" in capsys.readouterr().out

    @pytest.mark.parametrize("argv", [["--brief"], ["X", "--port", "abc", "--brief", "b"],
                                      ["X", "--brief", "b", "--bogus"]])
    def test_bad_flags_are_usage_errors(self, argv):
        with pytest.raises(ValueError):
            oneshot.parse(argv)


class TestTheDirectorEdge:
    def test_the_turn_goes_through_the_console_session(self, root, monkeypatch):
        """run_director uses directorsession._turn - the same runner, model and
        tools as the dashboard's chat - and lifts the turn timeout for it."""
        from bgate_ui.agents import directorsession

        seen = {}

        def fake_turn(r, prompt, reseed, settle, *, item_id=0, record=False):
            seen["timeout"] = directorsession.TURN_TIMEOUT_S
            seen["prompt"] = prompt
            seen["record"] = record
            settle("ONESHOT: DONE", False)
        monkeypatch.setattr(directorsession, "_turn", fake_turn)
        monkeypatch.setattr(directorsession, "TURN_TIMEOUT_S", 900.0)
        got = oneshot.run_director(root, "do the kickoff")
        assert got == {"ok": True, "text": "ONESHOT: DONE"}
        assert seen["prompt"] == "do the kickoff" and seen["record"] is True
        assert seen["timeout"] >= oneshot.DIRECTOR_TURN_S
        assert directorsession.TURN_TIMEOUT_S == 900.0   # restored

    def test_an_unavailable_cli_is_reported_not_raised(self, root):
        # conftest resolves find_claude to None for every test
        got = oneshot.run_director(root, "do the kickoff")
        assert got["ok"] is False
        assert "claude CLI" in got["text"]

    def test_wait_for_dispatch_sees_a_dispatched_item(self, root):
        item = queue.add(root, "tech", "a system", brief="x" * 40)
        assert oneshot.wait_for_dispatch(root, seconds=0.1, poll_s=0.01)["ok"] is False
        queue.set_status(root, item["id"], "dispatched")
        got = oneshot.wait_for_dispatch(root, seconds=0.1, poll_s=0.01)
        assert got["ok"] is True and got["dispatched"] == [item["id"]]
