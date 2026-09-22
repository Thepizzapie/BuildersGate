"""A human-only gate is asked, not worked; a reopened item is dispatchable.

MEASURED (exit-67-r2, 2026-09-22): the graybox gate passed; a director item
was filed to call greenlight_graybox_verdict, a tool no agent can hold; it
ran three times into the refusal, was reopened for a third run and sat
queued but invisible (runs_of >= cap), and the whole board idled behind a
decision the human was never asked.
"""
from __future__ import annotations

from bgate_core.board import queue, steerbox
from bgate_core.design import greenlight
from bgate_ui.agents import autodeploy


class TestTheCapCountsRuns:
    def test_a_reopen_for_the_last_allowed_run_is_dispatchable(self, root):
        item = queue.add(root, "director", "unblock", brief="decide")
        queue.set_status(root, item["id"], "failed", result="no")
        queue.reopen(root, item["id"], "round 2")
        queue.set_status(root, item["id"], "failed", result="no")
        queue.reopen(root, item["id"], "round 3")          # third of three
        assert item["id"] in {r["id"] for r in queue.ready(root)}
        queue.set_status(root, item["id"], "failed", result="no")
        assert queue.at_attempt_cap(root, queue.get(root, item["id"]))
        try:
            queue.reopen(root, item["id"], "round 4")
        except ValueError:
            pass
        else:
            raise AssertionError("a fourth run must be refused")


def _thesis():
    return {"sentence": "At every exit the player chooses the road that fits the "
                        "build or risks the one that would change it.",
            "options": ["safe exit", "risky exit"],
            "stakes": "a build without the answer to the next biome loses the run",
            "tension": "keep moving and the build stays thin; stop and the "
                       "vehicle is gone",
            "dominant_strategy": "always take the safe exit; threat floors rise "
                                 "so no late exit is safe",
            "cadence": "one route choice every 4-6 minutes"}


def _submit(fresh_root):
    scene = fresh_root / "game" / "scenes"
    scene.mkdir(parents=True, exist_ok=True)
    (scene / "graybox.tscn").write_text("[gd_scene format=3]", encoding="utf-8")
    (fresh_root / "shot.png").write_bytes(b"not really a png")
    greenlight.set_thesis(fresh_root, _thesis())
    greenlight.advance(fresh_root, greenlight.GRAYBOX)
    greenlight.graybox_submit(fresh_root, scene="game/scenes/graybox.tscn",
                              evidence=["shot.png"])


class TestTheVerdictIsAsked:
    def test_submitting_a_graybox_asks_the_human_once(self, fresh_root):
        root = fresh_root
        _submit(root)
        open_ = [q for q in steerbox.open_questions(root)
                 if "GRAYBOX VERDICT NEEDED" in str(q.get("question"))]
        assert len(open_) == 1
        assert greenlight.ask_for_verdict(root) is False
        assert len([q for q in steerbox.open_questions(root)
                    if "GRAYBOX VERDICT NEEDED" in str(q.get("question"))]) == 1

    def test_a_verdict_clears_the_pending_flag(self, fresh_root):
        root = fresh_root
        _submit(root)
        assert greenlight.verdict_pending(root)
        greenlight.graybox_verdict(root, verdict="pass", interesting=True,
                                   why="three seeds win and the exit choice reads",
                                   by="human")
        assert not greenlight.verdict_pending(root)

    def test_an_idle_board_names_the_human_gate(self, fresh_root):
        root = fresh_root
        _submit(root)
        mem = {"cool": {}}
        autodeploy._note_human_gate(str(root), mem)
        from bgate_core.store import events
        got = [e for e in events.since(root, 0)["events"]
               if e.get("kind") == "dispatch.blocked"
               and (e.get("payload") or {}).get("code") == "human_verdict"]
        assert len(got) == 1
        autodeploy._note_human_gate(str(root), mem)          # once per idle stretch
        got = [e for e in events.since(root, 0)["events"]
               if e.get("kind") == "dispatch.blocked"
               and (e.get("payload") or {}).get("code") == "human_verdict"]
        assert len(got) == 1


class TestASubmittedGrayboxLiftsTheHold:
    def test_held_seats_open_once_the_graybox_is_submitted(self, fresh_root):
        greenlight.set_thesis(fresh_root, _thesis())
        greenlight.advance(fresh_root, greenlight.GRAYBOX)
        assert "art" in greenlight.held_seats(fresh_root)
        _submit(fresh_root)
        assert greenlight.held_seats(fresh_root) == ()
        assert greenlight.generation_allows(fresh_root, "image")[0]

    def test_a_failed_verdict_puts_the_hold_back(self, fresh_root):
        _submit(fresh_root)
        greenlight.graybox_verdict(fresh_root, verdict="fail", interesting=False,
                                   why="attack, dodge, hold interact - nothing to choose",
                                   by="human")
        assert "art" in greenlight.held_seats(fresh_root)
