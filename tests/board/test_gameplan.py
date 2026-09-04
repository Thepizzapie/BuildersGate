"""The game-plan compiler: manifest -> plan_row -> slice on the board."""
from __future__ import annotations

import pytest

from bgate_core.design import gameplan
from bgate_core.board import queue


MANIFEST = [
    {"kind": "scene", "name": "hub_room", "seat": "gameplay", "slice": True,
     "acceptance": "boots headless, player node spawns"},
    {"kind": "asset", "name": "hero_sheet", "seat": "art", "slice": True,
     "acceptance": "idle+walk4, passes consistency_check",
     "depends_on": ["hub_room"]},
    {"kind": "sound", "name": "ambience_bed", "seat": "audio", "slice": False},
]


class TestValidate:
    def test_rejects_unknown_kind_seat_and_dangling_dep(self):
        with pytest.raises(ValueError, match="kind"):
            gameplan.validate_manifest([{"kind": "vibe", "name": "x",
                                         "seat": "art"}])
        with pytest.raises(ValueError, match="seat"):
            gameplan.validate_manifest([{"kind": "asset", "name": "x",
                                         "seat": "wizard"}])
        with pytest.raises(ValueError, match="not a manifest row"):
            gameplan.validate_manifest([{"kind": "asset", "name": "x",
                                         "seat": "art",
                                         "depends_on": ["ghost"]}])

    def test_rejects_a_cycle_naming_its_members(self):
        two = [{"kind": "asset", "name": "a", "seat": "art",
                "depends_on": ["b"]},
               {"kind": "asset", "name": "b", "seat": "art",
                "depends_on": ["a"]}]
        with pytest.raises(ValueError, match="cycle"):
            gameplan._ordered(gameplan.validate_manifest(two))


class TestIngest:
    def test_slice_rows_reach_the_board_with_their_dependency(self, root):
        got = gameplan.ingest(root, MANIFEST, session_id=1)
        assert got["rows"] == 3
        assert len(got["slice_filed"]) == 2      # ambience_bed stays spec
        hub = next(f for f in got["slice_filed"] if f["name"] == "hub_room")
        hero = next(f for f in got["slice_filed"] if f["name"] == "hero_sheet")
        assert hero["depends_on"] == hub["id"]   # a real link, not a priority
        assert queue.get(root, hero["id"])["source"] == "game-plan"

    def test_ingest_is_idempotent_on_names(self, root):
        gameplan.ingest(root, MANIFEST, session_id=1)
        again = gameplan.ingest(root, MANIFEST, session_id=1)
        assert again["updated"] == 3
        assert again["slice_filed"] == []        # items kept, not duplicated


class TestStatus:
    def test_status_answers_what_remains(self, root):
        gameplan.ingest(root, MANIFEST, session_id=1)
        before = gameplan.status(root)
        assert before["rows"] == 3
        assert before["spec"] == 1 and before["on_board"] == 2
        assert before["slice"]["complete"] is False

        for item in queue.list_items(root):
            queue.set_status(root, item["id"], "done", result="landed")
        after = gameplan.status(root)
        assert after["built"] == 2
        # BUILT IS NOT IN THE GAME. Nothing references these yet, so the slice
        # is not complete and every row is still remaining — the whole point of
        # the wired/verified states (see TestWiredAndVerified).
        assert after["slice"]["complete"] is False
        assert {r["name"] for r in after["remaining"]} == {
            "hub_room", "hero_sheet", "ambience_bed"}

    def test_a_failed_item_reads_lost_not_built(self, root):
        gameplan.ingest(root, MANIFEST, session_id=1)
        first = queue.list_items(root)[0]
        queue.set_status(root, first["id"], "failed", result="died")
        got = gameplan.status(root)
        assert got["lost"] == 1


class TestMultiParent:
    """A game is a DAG: the scene needs the sprite AND the sound AND the script."""

    FAN_IN = [
        {"kind": "asset", "name": "hero_png", "seat": "art", "slice": True},
        {"kind": "sound", "name": "step_wav", "seat": "audio", "slice": True},
        {"kind": "scene", "name": "hero_scene", "seat": "gameplay", "slice": True,
         "depends_on": ["hero_png", "step_wav"]},
    ]

    def test_every_parent_holds_the_child_not_just_the_first(self, root):
        got = gameplan.ingest(root, self.FAN_IN, session_id=1)
        scene = next(f for f in got["slice_filed"] if f["name"] == "hero_scene")
        art = next(f for f in got["slice_filed"] if f["name"] == "hero_png")
        audio = next(f for f in got["slice_filed"] if f["name"] == "step_wav")
        assert sorted(queue.parents(root, scene["id"])) == sorted(
            [art["id"], audio["id"]])

        # The first parent landing is NOT enough — that was the old behaviour.
        queue.set_status(root, art["id"], "done", result="painted")
        held = queue.blocker(root, scene["id"])
        assert held is not None and held["id"] == audio["id"]
        assert queue.next_for(root, "gameplay") is None

        queue.set_status(root, audio["id"], "done", result="recorded")
        assert queue.blocker(root, scene["id"]) is None
        assert queue.next_for(root, "gameplay")["id"] == scene["id"]

    def test_blocker_names_the_others_it_is_also_waiting_on(self, root):
        got = gameplan.ingest(root, self.FAN_IN, session_id=1)
        scene = next(f for f in got["slice_filed"] if f["name"] == "hero_scene")
        held = queue.blocker(root, scene["id"])
        assert "also_waiting_on" in held


class TestCutDependency:
    def test_a_cancelled_predecessor_no_longer_strands_its_successor(self, root):
        first = queue.add(root, "art", "the anchor")
        second = queue.add(root, "gameplay", "needs it", depends_on=first["id"])
        queue.set_status(root, first["id"], "cancelled", result="dropped")
        assert queue.blocker(root, second["id"]) is not None   # the dead state

        got = queue.cut_dependency(root, second["id"], first["id"], by="adrian")
        assert got["ready"] is True
        assert queue.blocker(root, second["id"]) is None
        assert queue.next_for(root, "gameplay")["id"] == second["id"]

    def test_cutting_an_extra_parent_leaves_the_others(self, root):
        a = queue.add(root, "art", "a")
        b = queue.add(root, "audio", "b")
        child = queue.add(root, "tech", "c", depends_on=a["id"])
        queue.add_dependency(root, child["id"], b["id"])
        got = queue.cut_dependency(root, child["id"], b["id"], by="adrian")
        assert got["still_waiting_on"] == [a["id"]]
        assert got["ready"] is False

    def test_cutting_what_is_not_a_dependency_is_refused(self, root):
        a = queue.add(root, "art", "a")
        b = queue.add(root, "tech", "b")
        with pytest.raises(LookupError):
            queue.cut_dependency(root, b["id"], a["id"])


class TestWiredAndVerified:
    """'built' flatters: a sprite on disk no scene references is not in the game."""

    def _built(self, root):
        gameplan.ingest(root, MANIFEST, session_id=1)
        for item in queue.list_items(root):
            queue.set_status(root, item["id"], "done", result="landed")

    def test_a_built_row_no_scene_references_is_not_in_the_game(self, root):
        self._built(root)
        got = gameplan.status(root)
        assert got["built"] == 2 and got["wired"] == 0
        assert got["slice"]["complete"] is False        # built != playable
        assert {r["name"] for r in got["remaining"]} >= {"hero_sheet"}

    def test_a_scene_reference_moves_the_row_to_wired(self, root):
        self._built(root)
        scenes = root / "game" / "scenes"
        scenes.mkdir(parents=True, exist_ok=True)
        (scenes / "hub.tscn").write_text(
            '[ext_resource type="Texture2D" path="res://assets/hero_sheet.png" '
            'id="1"]\n', encoding="utf-8")
        got = gameplan.status(root)
        assert got["wired"] >= 1
        assert "hero_sheet" not in {r["name"] for r in got["remaining"]}

    def test_a_qa_pass_marks_the_row_verified(self, root):
        self._built(root)
        target = queue.list_items(root)[0]
        gate = queue.add(root, "qa", "QA gate: verify", source="qa-gate",
                         source_ref=str(target["id"]))
        queue.set_status(root, gate["id"], "done",
                         result="VERDICT: PASS — screenshots attached")
        assert gameplan.status(root)["verified"] == 1

    def test_a_gate_with_no_verdict_marker_does_not_verify(self, root):
        self._built(root)
        target = queue.list_items(root)[0]
        gate = queue.add(root, "qa", "QA gate: verify", source="qa-gate",
                         source_ref=str(target["id"]))
        queue.set_status(root, gate["id"], "done", result="looked at it, seems ok")
        assert gameplan.status(root)["verified"] == 0


class TestSliceCheck:
    def test_not_due_until_every_slice_row_is_built(self, root):
        gameplan.ingest(root, MANIFEST, session_id=1)
        assert gameplan.slice_check_due(root)["due"] is False
        for item in queue.list_items(root):
            queue.set_status(root, item["id"], "done", result="landed")
        assert gameplan.slice_check_due(root)["due"] is True

    def test_it_files_one_qa_item_that_reviews_the_game(self, root):
        gameplan.ingest(root, MANIFEST, session_id=1)
        for item in queue.list_items(root):
            queue.set_status(root, item["id"], "done", result="landed")
        got = gameplan.open_slice_check(root)
        assert got["ok"] is True
        filed = queue.get(root, got["item"])
        assert filed["seat"] == "qa"
        assert "godot_run" in filed["brief"] and "plan_status" in filed["brief"]
        # And it names what is built but not referenced — the real finding.
        assert "NOT REFERENCED" in filed["brief"]

    def test_it_does_not_file_twice_for_an_unchanged_slice(self, root):
        gameplan.ingest(root, MANIFEST, session_id=1)
        for item in queue.list_items(root):
            queue.set_status(root, item["id"], "done", result="landed")
        first = gameplan.open_slice_check(root)
        assert gameplan.slice_check_due(root)["due"] is False   # one is open
        queue.set_status(root, first["item"], "done", result="VERDICT: PASS")
        assert gameplan.slice_check_due(root)["due"] is False   # unchanged


class TestLayoutLanes:
    """The lanes must describe the repo that is actually here.

    The default table assumes <root>/game; `bgate init` scaffolds into <root>
    and an adopted repo has whatever its author chose. Against an ordinary
    layout NO seat owned anything, so every dispatched agent was refused on
    contact with the source tree.
    """

    def test_a_root_layout_project_is_detected_and_relaned(self, root):
        from bgate_core.board import seats
        (root / "project.godot").write_text(
            'run/main_scene="res://scenes/main.tscn"\n', encoding="utf-8")
        layout = seats.detect_layout(root)
        assert layout["prefix"] == ""
        assert layout["matches"] is False

        # Before: the real source tree has no owner. (scripts/** happens to be
        # in tech's lane already; scenes and assets are owned by nobody, which
        # is what refuses the gameplay and art seats on contact.)
        assert seats.lane_owners(root, "scenes/main.tscn") == []
        assert seats.lane_owners(root, "assets/hero.png") == []
        seats.apply_layout(root)
        # After: the seats that own these in the scaffold layout own them here.
        assert "gameplay" in seats.lane_owners(root, "scenes/main.tscn")
        assert "art" in seats.lane_owners(root, "assets/hero.png")
        assert seats.can_write(root, "gameplay", "scripts/player.gd")["allowed"]

    def test_the_scaffold_layout_is_left_alone(self, root):
        from bgate_core.board import seats
        game = root / "game"
        game.mkdir(exist_ok=True)
        (game / "project.godot").write_text("\n", encoding="utf-8")
        assert seats.detect_layout(root)["prefix"] == "game/"
        got = seats.apply_layout(root)
        assert got["changed"] is False
        assert seats.can_write(root, "gameplay", "game/scripts/x.gd")["allowed"]

    def test_relaning_never_hands_one_seat_the_whole_project(self, root):
        from bgate_core.board import seats
        lanes = seats.lanes_for_layout("")
        # tech's game/** would collapse to a bare ** and swallow every other
        # seat's lane; it must not survive the rewrite.
        assert "**" not in lanes["tech"]
        assert "*.godot" in lanes["tech"]

    def test_doctor_reports_a_layout_mismatch(self, root):
        from bgate_core.runtime import doctor
        (root / "project.godot").write_text("\n", encoding="utf-8")
        rows_ = {r["name"]: r for r in doctor.project_report(str(root))}
        assert rows_["seat_lanes"]["ok"] is False
        assert "bgate adopt" in rows_["seat_lanes"]["fix"]


class TestDigest:
    """The morning report — nothing else answered 'what happened overnight'."""

    def test_it_separates_finished_failed_and_awaiting_you(self, root):
        from bgate_core.design import gameplan as gp
        a = queue.add(root, "art", "painted")
        b = queue.add(root, "tech", "broke")
        queue.set_status(root, a["id"], "done", result="landed")
        queue.set_status(root, b["id"], "failed", result="killed: ceiling")
        got = gp.digest(root)
        assert [f["id"] for f in got["finished"]] == [a["id"]]
        assert [f["id"] for f in got["failed"]] == [b["id"]]
        assert "ceiling" in got["failed"][0]["why"]

    def test_it_names_a_dirty_tree_as_the_reason_the_board_stopped(self, root):
        import subprocess
        from bgate_core.design import gameplan as gp
        for args in (["init"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=root, capture_output=True)
        (root / "seed.txt").write_text("s\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=root,
                       capture_output=True)
        queue.add(root, "art", "waiting forever")
        (root / "someone_edited.gd").write_text("x\n", encoding="utf-8")
        got = gp.digest(root)
        # The signature of the deadlock, named rather than left to be guessed.
        assert "uncommitted" in got["blocked"]
        assert "whole board" in got["blocked"].lower()

    def test_a_quiet_board_with_nothing_queued_is_not_reported_as_blocked(self, root):
        from bgate_core.design import gameplan as gp
        assert gp.digest(root)["blocked"] == ""


class TestPaidPathsPreflightTheAccount:
    """There is no budget to ask. What every paid path must still do is ask
    whether ANY provider can serve it, because a drained or unkeyed account
    refuses regardless of price and an agent that learns it from a 402 has
    already paid for the lesson."""

    def test_every_paid_image_tool_preflights_the_provider(self):
        # The gate guarded ONE tool of twelve. This is the regression that
        # notices if a paid path is added (or reverted) without one.
        import ast
        from pathlib import Path
        src = Path("src/bgate_mcp/server.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        paid = {"image_generate", "image_edit", "item_generate", "item_variants",
                "image_talkhead", "vfx_animate", "image_sprites",
                "kie_video_generate"}
        ungated = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in paid:
                body = ast.get_source_segment(src, node) or ""
                if "_provider_gate" not in body:
                    ungated.append(node.name)
        assert ungated == [], f"paid tools with no provider preflight: {ungated}"

    def test_there_is_no_spend_gate_left_to_call(self):
        """The ledger and its reservation gate are gone (db migration 0045).
        A helper that came back would be a budget coming back with it — and so
        would the wrappers that existed only to consult one."""
        from bgate_mcp import server

        for gone in ("_spend_gate", "_run_ceiling", "_paid_gate",
                     "_gate_images"):
            assert not hasattr(server, gone), gone
