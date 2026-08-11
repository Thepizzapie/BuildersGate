"""Dialogue trees — the graph checks that run BEFORE the file lands.

Every refusal here is a bug that is invisible in the JSON and expensive in the
game: a choice pointing at a node that does not exist dead-ends on a branch most
players never take, a node nothing reaches is content that was written and is
not in the game, and a node from which no ending is reachable reads to a player
as a hang. A schema catches none of the three; a graph walk catches all of them
in microseconds.

The refusal has to NAME the node. "invalid dialogue" hands the writer a hunt
through 200 nodes for the one typo.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bgate_core import dialogue, lore
from bgate_mcp import server


async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return root


def tree() -> list[dict]:
    """A small, valid conversation: a fork, a loop back, and two endings."""
    return [
        {"id": "greet", "speaker": "Keeper", "text": "The gate is shut.",
         "choices": [{"text": "Open it.", "goto": "refuse"},
                     {"text": "Who shut it?", "goto": "who"}]},
        {"id": "who", "speaker": "Keeper", "text": "The ones who left.",
         "choices": [{"text": "Then open it.", "goto": "refuse"},
                     {"text": "I will wait.", "goto": "wait"}]},
        {"id": "refuse", "speaker": "Keeper", "text": "Not for you.",
         "choices": [{"text": "Ask again.", "goto": "who"},
                     {"text": "Leave.", "goto": "leave"}]},
        {"id": "wait", "speaker": "Keeper", "text": "Then wait.", "end": True},
        {"id": "leave", "speaker": "Keeper", "text": "Go well.", "end": True},
    ]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class TestValidation:
    def test_a_sound_tree_passes_and_reports_its_shape(self):
        got = dialogue.validate(tree())
        assert got["start"] == "greet"
        assert got["ends"] == ["leave", "wait"]
        assert len(got["nodes"]) == 5

    def test_a_dangling_choice_target_is_refused_and_named(self):
        nodes = tree()
        nodes[0]["choices"][1]["goto"] = "whoo"          # one letter, weeks later
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.validate(nodes)
        message = str(exc.value)
        assert "'greet'" in message and "'whoo'" in message
        assert "not a node" in message

    def test_an_orphan_node_is_refused_and_named(self):
        nodes = tree()
        nodes.append({"id": "secret", "text": "Nobody hears this.", "end": True})
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.validate(nodes)
        assert "'secret'" in str(exc.value)

    def test_a_conversation_with_no_way_out_is_refused(self):
        """A cycle with no reachable ending is not a crash and not a syntax
        error — it is a player stuck in a loop, which reads as a hang."""
        nodes = [
            {"id": "a", "text": "...", "choices": [{"text": "on", "goto": "b"}]},
            {"id": "b", "text": "...", "choices": [{"text": "back", "goto": "a"}]},
            {"id": "out", "text": "bye", "end": True},
        ]
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.validate(nodes)
        # The orphan check fires first here and names the unreachable ending;
        # remove it and the trap itself is what refuses.
        assert "'out'" in str(exc.value)

        nodes = nodes[:2]
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.validate(nodes)
        assert "never finishes" in str(exc.value)

    def test_a_trap_with_an_ending_elsewhere_is_still_refused(self):
        nodes = tree()
        # 'wait' stops being an ending and loops onto itself: reachable, linked,
        # and a hole the player cannot climb out of.
        nodes[3] = {"id": "wait", "text": "Then wait.",
                    "choices": [{"text": "Keep waiting.", "goto": "wait"}]}
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.validate(nodes)
        assert "'wait'" in str(exc.value)
        assert "cannot leave" in str(exc.value)

    def test_a_node_that_neither_ends_nor_continues_is_refused(self):
        nodes = tree()
        nodes[3] = {"id": "wait", "text": "Then wait."}      # end flag forgotten
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.validate(nodes)
        assert "'wait'" in str(exc.value) and "end: true" in str(exc.value)

    def test_an_ending_that_still_offers_choices_is_refused(self):
        nodes = tree()
        nodes[4]["choices"] = [{"text": "wait, no", "goto": "greet"}]
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.validate(nodes)
        assert "'leave'" in str(exc.value)

    def test_duplicate_ids_are_refused_before_anything_points_at_them(self):
        nodes = tree()
        nodes[1]["id"] = "greet"
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.validate(nodes)
        assert "ambiguous" in str(exc.value)

    def test_an_unlabelled_or_untargeted_choice_is_refused(self):
        nodes = tree()
        nodes[0]["choices"][0] = {"text": "", "goto": "refuse"}
        with pytest.raises(dialogue.DialogueError):
            dialogue.validate(nodes)
        nodes = tree()
        nodes[0]["choices"][0] = {"text": "Open it."}
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.validate(nodes)
        assert "goto" in str(exc.value)

    def test_a_start_that_is_not_a_node_is_refused(self):
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.validate(tree(), start="prologue")
        assert "prologue" in str(exc.value)

    def test_the_start_may_be_any_node_not_just_the_first(self):
        nodes = tree()
        nodes[0]["choices"].append({"text": "Wait here.", "goto": "wait"})
        got = dialogue.validate(nodes, start="greet")
        assert got["start"] == "greet"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
class TestWrite:
    def test_it_lands_in_the_narrative_seats_own_lane(self, root):
        got = dialogue.write(root, "Gate Keeper", tree())
        assert got["name"] == "gate-keeper"
        assert got["rel_path"] == "game/dialogue/gate-keeper.dialogue.json"
        assert Path(got["path"]).is_file()

    def test_the_file_is_json_godot_can_parse(self, root):
        dialogue.write(root, "keeper", tree())
        doc = json.loads(dialogue.path_for(root, "keeper").read_text("utf-8"))
        assert doc["format"] == "bgate.dialogue"
        assert doc["start"] == "greet"
        assert [n["id"] for n in doc["nodes"]][0] == "greet"
        assert doc["nodes"][0]["choices"][0]["goto"] == "refuse"

    def test_a_broken_tree_writes_nothing_at_all(self, root):
        nodes = tree()
        nodes[0]["choices"][0]["goto"] = "nowhere"
        with pytest.raises(dialogue.DialogueError):
            dialogue.write(root, "keeper", nodes)
        # The state this refuses to create: a file on disk that somebody imports.
        assert not dialogue.path_for(root, "keeper").exists()

    def test_read_and_list_round_trip(self, root):
        dialogue.write(root, "keeper", tree())
        got = dialogue.read(root, "keeper")
        assert len(got["nodes"]) == 5
        listing = dialogue.list_dialogues(root)
        assert [d["name"] for d in listing] == ["keeper"]
        assert listing[0]["ok"] is True

    def test_a_hand_broken_file_is_flagged_by_the_listing(self, root):
        dialogue.write(root, "keeper", tree())
        path = dialogue.path_for(root, "keeper")
        doc = json.loads(path.read_text("utf-8"))
        doc["nodes"][0]["choices"][0]["goto"] = "gone"
        path.write_text(json.dumps(doc), encoding="utf-8")
        entry = dialogue.list_dialogues(root)[0]
        assert entry["ok"] is False and "gone" in entry["error"]

    def test_reading_something_that_is_not_there_says_so(self, root):
        with pytest.raises(LookupError):
            dialogue.read(root, "nobody")


class TestCanonOnTheWayIn:
    """The seat's mission says canon_check runs on every narrative write, and a
    check the author has to remember is a check that happens on the good days."""

    def _canon(self, root) -> None:
        lore.add_entity(root, "faction", "The Ashen Order", status="canon")
        lore.add_fact(root, "The Ashen Order",
                      "The Ashen Order worships the flame.", locked=True)

    def test_a_hard_conflict_refuses_the_write(self, root):
        self._canon(root)
        nodes = [{"id": "line", "end": True,
                  "text": "The Ashen Order does not worship the flame."}]
        with pytest.raises(dialogue.DialogueError) as exc:
            dialogue.write(root, "heresy", nodes)
        assert "canon_check" in str(exc.value)
        assert not dialogue.path_for(root, "heresy").exists()

    def test_the_conflict_can_be_overridden_when_the_canon_is_what_changed(self, root):
        self._canon(root)
        nodes = [{"id": "line", "end": True,
                  "text": "The Ashen Order does not worship the flame."}]
        got = dialogue.write(root, "heresy", nodes, allow_canon_conflict=True)
        assert got["canon"]["verdict"] == "conflict"
        assert dialogue.path_for(root, "heresy").is_file()

    def test_choice_labels_are_checked_too_not_just_the_lines(self, root):
        self._canon(root)
        nodes = [
            {"id": "ask", "text": "Well?",
             "choices": [{"text": "The Ashen Order does not worship the flame.",
                          "goto": "done"}]},
            {"id": "done", "text": "...", "end": True},
        ]
        with pytest.raises(dialogue.DialogueError):
            dialogue.write(root, "sly", nodes)

    def test_a_review_flag_rides_along_instead_of_blocking(self, root):
        got = dialogue.write(root, "keeper", tree())
        # An unknown proper noun is the normal state of a first draft.
        assert got["canon"]["verdict"] in ("ok", "review")
        assert dialogue.path_for(root, "keeper").is_file()


# ---------------------------------------------------------------------------
# The MCP surface
# ---------------------------------------------------------------------------
@pytest.mark.anyio
class TestMcpSurface:
    async def test_the_narrative_seat_can_now_write_its_own_lane(self, wired):
        got = await call("dialogue_write", name="keeper", nodes=tree())
        assert got.get("ok") is True and got["nodes"] == 5
        assert Path(got["path"]).is_file()

    async def test_a_dangling_target_comes_back_as_an_error_payload(self, wired):
        nodes = tree()
        nodes[0]["choices"][1]["goto"] = "whoo"
        got = await call("dialogue_write", name="keeper", nodes=nodes)
        assert got.get("ok") is False
        assert "whoo" in got["error"]

    async def test_read_and_list_through_the_surface(self, wired):
        await call("dialogue_write", name="keeper", nodes=tree())
        got = await call("dialogue_read", name="keeper")
        assert got["start"] == "greet"
        listing = await call("dialogue_list")
        assert listing["count"] == 1 and listing["broken"] == []
