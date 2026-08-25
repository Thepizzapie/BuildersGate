"""Regressions for what the Catnip Fiend 3D benchmark measured going wrong.

Every test here reproduces a MEASURED failure from one run of the harness
against a real 3D game, not a hypothetical. The generation itself was cheap and
mostly worked. What cost money was:

    verification proving the wrong thing
    process ceilings killing already-finished work
    runtime truth never checked at the player-facing layer
    board state that was technically correct and read as broken

Two failure directions, and the benchmark produced both. A FALSE GREEN is a
check that passes while measuring the wrong thing. A DISMISSED RED is a true
signal waved away because it looked like noise — and the canonical one here is
``N resources still in use at exit``, which two readers called harness noise for
a full session while it was a genuine leak in the project's own tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bgate_core import (assets, db, enginetests, findings, gitwork, greenlight,
                        project, queue, salvage, scalecontract, sceneproof,
                        spend, steerbox, traversal)


# ---------------------------------------------------------------------------
# §3 Traversal — the false green INSIDE the gate written to catch false greens
# ---------------------------------------------------------------------------
class TestSettledArrival:
    """Four of six climbing routes passed and were not traversable, because the
    tests measured vertical rise. Then the DRIVER written to fix that produced
    its own false green: it accepted arrival on any frame the body was near the
    target, including mid-arc during a jump that MISSED."""

    def _spec(self, **kw):
        return traversal.route(
            name="ledge", scene="res://l.tscn", launch="/L/Ledge",
            destination="/L/TopArea", inputs=[{"action": "jump", "frames": 1}],
            **kw)

    def test_a_ballistic_arc_through_the_volume_is_not_an_arrival(self):
        # The MISSED jump. The body passes through the destination volume on
        # the way up and keeps going. Airborne on every frame.
        spec = self._spec()
        samples = [{"frame": i, "inside": 20 <= i <= 24,
                    "grounded": False, "busy": False} for i in range(40)]
        got = traversal.grade(spec, samples)
        assert got["ok"] is False
        assert got["ever_inside"] is True
        assert got["inside_but_unsettled_frames"] == 5
        assert "FALSE GREEN" in got["why"]

    def test_a_scripted_mantle_carries_is_on_floor_and_still_fails(self):
        """THE ONE A GROUNDED CHECK DOES NOT CATCH. A mantle animation lerps
        the body through the air while is_on_floor() still reports the state
        from before the move began — so grounded is True, the body is inside
        the volume, and it is not there."""
        spec = self._spec()
        samples = [{"frame": i, "inside": True, "grounded": True,
                    "busy": True} for i in range(30)]
        got = traversal.grade(spec, samples)
        assert got["ok"] is False
        assert got["longest_settled_run"] == 0
        assert got["ever_grounded"] is True     # the check that was not enough

    def test_one_settled_frame_is_not_enough(self):
        spec = self._spec(settle_frames=3)
        samples = ([{"frame": 1, "inside": True, "grounded": True, "busy": False}]
                   + [{"frame": 2, "inside": True, "grounded": False,
                       "busy": False}])
        got = traversal.grade(spec, samples)
        assert got["ok"] is False
        assert got["longest_settled_run"] == 1
        assert got["required_frames"] == 3

    def test_three_consecutive_settled_frames_pass(self):
        spec = self._spec(settle_frames=3)
        samples = [{"frame": i, "inside": i >= 10, "grounded": i >= 10,
                    "busy": False} for i in range(1, 20)]
        got = traversal.grade(spec, samples)
        assert got["ok"] is True
        assert got["settled_at_frame"] == 12

    def test_a_controller_with_no_busy_query_is_REFUSED_not_sampled(self):
        """Sampling it naively is the bug, so the harness refuses the run."""
        naive = ("extends CharacterBody2D\n"
                 "func _physics_process(d):\n\tmove_and_slide()\n")
        assert traversal.controller_contract(naive)["ok"] is False
        with pytest.raises(traversal.ControllerRefused, match="scripted-move"):
            traversal.require_controller(naive)

    def test_any_of_the_accepted_query_names_satisfies_the_contract(self):
        for name in traversal.BUSY_ALIASES:
            source = f"extends Node\nfunc {name}() -> bool:\n\treturn false\n"
            assert traversal.require_controller(source) == name

    def test_a_route_must_name_a_real_arrival_volume_not_a_marker(self):
        # "within 2 m of a marker" is the claim that passed mid-jump.
        with pytest.raises(ValueError, match="arrival volume"):
            traversal.route(name="x", scene="s", launch="/L", destination="",
                            inputs=[{"action": "jump", "frames": 1}])
        with pytest.raises(ValueError, match="launch surface"):
            traversal.route(name="x", scene="s", launch="", destination="/A",
                            inputs=[{"action": "jump", "frames": 1}])

    def test_the_driver_is_bounded_and_says_where_it_is(self):
        """An unbounded wait-until-condition loop is indistinguishable from a
        hang; three agents were killed after 25 minutes of silence."""
        spec = self._spec()
        source = traversal.driver_source(spec, "out.json")
        assert spec["max_frames"] <= traversal.MAX_FRAMES
        assert "BGATE_TRAVERSAL_TICK" in source
        assert '_frame >= SPEC["max_frames"]' in source

    def test_a_step_can_hold_several_actions_at_once(self):
        # You do not stop running to jump. A one-action-per-step driver cannot
        # express the input the player actually uses.
        spec = traversal.route(
            name="x", scene="s", launch="/L", destination="/A",
            inputs=[{"actions": ["move_right", "jump"], "frames": 3}])
        assert spec["inputs"][0]["actions"] == ["move_right", "jump"]


# ---------------------------------------------------------------------------
# §1 The default scene — the one nobody's tests named
# ---------------------------------------------------------------------------
def _godot_project(root, scene: str = "res://game.tscn", *,
                   scaffold: bool = False, write_scene: bool = True):
    base = Path(root) / "game"
    base.mkdir(parents=True, exist_ok=True)
    (base / "project.godot").write_text(
        f'config_version=5\n\n[application]\nrun/main_scene="{scene}"\n',
        encoding="utf-8")
    if write_scene and scene.startswith("res://"):
        target = base / scene[len("res://"):]
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "[gd_scene format=3]\n\n[node name=\"Root\" type=\"Node3D\"]\n"
        if scaffold:
            body = ("[gd_scene format=3]\n\n"
                    "[node name=\"BGateDemo\" type=\"Node3D\"]\n")
        target.write_text(body, encoding="utf-8")
    return base


class TestDefaultScene:
    """The benchmark shipped with run/main_scene still pointing at the scaffold
    demo while every named-scene test passed. Each of those tests named the
    scene it tested; none named the one the game boots into."""

    def test_a_valid_named_scene_does_not_excuse_a_wrong_default(self, root):
        # The gameplay scene is fine. The DEFAULT points at the scaffold demo.
        base = _godot_project(root, "res://demo.tscn", scaffold=True)
        (base / "arena.tscn").write_text(
            "[gd_scene format=3]\n\n[node name=\"Arena\" type=\"Node3D\"]\n",
            encoding="utf-8")
        state = sceneproof.default_scene_state(root)
        assert state["ok"] is False
        assert state["scaffold"] is True
        rows = sceneproof.unproven(root)
        assert rows and "scaffold marker" in rows[0]["claim"]

    def test_release_fails_when_the_default_scene_is_the_scaffold(self, root):
        _godot_project(root, "res://demo.tscn", scaffold=True)
        got = greenlight.presentation_check(root)
        assert got["ok"] is False
        assert any("scaffold" in row for row in got["unmet"])

    def test_a_missing_default_scene_is_a_release_blocker(self, root):
        _godot_project(root, "res://gone.tscn", write_scene=False)
        state = sceneproof.default_scene_state(root)
        assert state["ok"] is False and state["exists"] is False
        assert "cannot boot" in state["why"]

    def test_no_declared_main_scene_is_named_as_such(self, root):
        base = Path(root) / "game"
        base.mkdir(parents=True, exist_ok=True)
        (base / "project.godot").write_text("config_version=5\n",
                                            encoding="utf-8")
        state = sceneproof.default_scene_state(root)
        assert state["ok"] is False
        assert "project manager" in state["why"]

    def test_a_captured_frame_nobody_described_still_blocks(self, root):
        """CAPTURING EVIDENCE IS NOT EXAMINING IT. The two-tailed cat shipped
        for a day with its turnaround renders already on disk."""
        _godot_project(root, "res://arena.tscn")
        frame = Path(root) / "beauty.png"
        frame.write_bytes(b"pretend png")
        sceneproof.record_capture(root, "res://arena.tscn", {
            "ok": True, "beauty": str(frame), "counts": {"entities": 4}})
        rows = sceneproof.unproven(root)
        assert len(rows) == 1
        assert rows[0]["kind"] == findings.JUDGEMENT
        assert "nobody has said what is IN the frame" in rows[0]["claim"]

        sceneproof.assert_content(
            root, "res://arena.tscn", frame=str(frame),
            says="the cat is on the counter, one tail, facing the door")
        assert sceneproof.unproven(root) == []

    def test_an_assertion_dies_with_the_frame_it_was_about(self, root):
        _godot_project(root, "res://arena.tscn")
        frame = Path(root) / "beauty.png"
        frame.write_bytes(b"first render")
        sceneproof.assert_content(root, "res://arena.tscn", frame=str(frame),
                                  says="one tail, and it is not forked")
        assert sceneproof.assertions_for(root, "res://arena.tscn")[0]["live"]
        frame.write_bytes(b"a DIFFERENT render entirely")
        stale = sceneproof.assertions_for(root, "res://arena.tscn")[0]
        assert stale["live"] is False
        assert "no longer applies" in stale["why_stale"]

    def test_looks_fine_is_refused_as_an_assertion(self, root):
        frame = Path(root) / "f.png"
        frame.write_bytes(b"x")
        with pytest.raises(ValueError, match="two-tailed"):
            sceneproof.assert_content(root, "res://a.tscn", frame=str(frame),
                                      says="looks fine")

    def test_all_four_cardinal_views_are_required_for_a_character(self, root):
        """THE FORKED TAIL WAS INVISIBLE FROM EVERY ANGLE ANYBODY RENDERED."""
        frame = Path(root) / "f.png"
        frame.write_bytes(b"x")
        for view in ("front", "left", "right"):
            sceneproof.assert_content(
                root, "res://cat.tscn", frame=str(frame), subject="cat",
                views=[view], says=f"the {view} view shows one head, four legs")
        assert sceneproof.character_gaps(root, "cat") == ["back"]
        sceneproof.assert_content(
            root, "res://cat.tscn", frame=str(frame), subject="cat",
            views=["back"], says="the rear view shows ONE tail, not two")
        assert sceneproof.character_gaps(root, "cat") == []


# ---------------------------------------------------------------------------
# §2 Delivered is not integrated
# ---------------------------------------------------------------------------
class TestAssetVerifyIsReleaseTruth:
    """asset_verify already answered this — dangling, delivered_but_unwired,
    stale imports — and nobody ran it. It was in no gate."""

    def test_an_unwired_delivered_asset_blocks_release(self, root, monkeypatch):
        monkeypatch.setattr(assets, "verify", lambda _r: {
            "ok": True, "missing": [], "integration": {
                "ok": False, "dangling": [], "dynamic_load_sites": 0,
                "delivered_but_unwired": [
                    {"path": "game/assets/cat.glb", "work_item_id": 12}],
                "freshness": {"ok": True, "stale": []}}})
        rows = greenlight._assets_unmet(root)
        assert len(rows) == 1
        assert "DELIVERED and nothing in the game consumes it" in rows[0]["claim"]
        assert "#12" in rows[0]["claim"]
        assert rows[0]["clears_by"]

    def test_a_stale_import_blocks_release(self, root, monkeypatch):
        """The engine serving older bytes than the disk. Every structural check
        passes and the running game draws the previous version."""
        monkeypatch.setattr(assets, "verify", lambda _r: {
            "ok": True, "missing": [], "integration": {
                "ok": False, "dangling": [], "delivered_but_unwired": [],
                "dynamic_load_sites": 0,
                "freshness": {"ok": False, "stale": [
                    {"path": "game/assets/cat.png", "on_disk_md5": "aaa",
                     "imported_md5": "bbb"}]}}})
        rows = greenlight._assets_unmet(root)
        assert any("OLDER build" in r["claim"] for r in rows)

    def test_dynamic_loading_is_preserved_as_a_caveat_not_erased(
            self, root, monkeypatch):
        monkeypatch.setattr(assets, "verify", lambda _r: {
            "ok": True, "missing": [], "integration": {
                "ok": False, "dangling": [], "dynamic_load_sites": 3,
                "delivered_but_unwired": [{"path": "a.png"}],
                "freshness": {"ok": True, "stale": []}}})
        row = greenlight._assets_unmet(root)[0]
        assert "CANDIDATE, not a verdict" in row["claim"]
        assert "3 place(s)" in row["claim"]

    def test_a_clean_project_produces_no_asset_rows(self, root, monkeypatch):
        monkeypatch.setattr(assets, "verify", lambda _r: {
            "ok": True, "missing": [], "integration": {
                "ok": True, "dangling": [], "delivered_but_unwired": [],
                "dynamic_load_sites": 0, "freshness": {"ok": True, "stale": []}}})
        assert greenlight._assets_unmet(root) == []


# ---------------------------------------------------------------------------
# §7 2D tools answering confidently about 3D, and rows nobody can clear
# ---------------------------------------------------------------------------
class TestDimensionBoundary:
    """scale_check measured render-canvas pixels for a 3D cat and reported
    '30.00 player-heights tall' for a 0.24 m animal. That number then became a
    blocking gate row."""

    def test_a_pixel_measurement_is_refused_on_a_3d_project(self, root):
        project.set_dimension(root, "3d")
        scalecontract.set_contract(root, player_height_px=64)
        with pytest.raises(scalecontract.WrongDimension, match="30.00"):
            scalecontract.check(root, "game/assets/cat_turnaround.png", "prop")

    def test_a_mesh_is_never_measured_in_pixels_even_on_a_2d_project(self, root):
        project.set_dimension(root, "2d")
        guard = scalecontract.dimension_guard(root, "game/assets/cat.glb", "prop")
        assert guard["ok"] is False
        assert guard["measure_with"] == "godot_inspect_resource"
        assert "AABB in metres" in guard["why"]

    def test_screen_space_classes_still_measure_on_a_3d_project(self, root):
        project.set_dimension(root, "3d")
        assert scalecontract.dimension_guard(root, "hud/heart.png", "ui")["ok"]

    def test_the_contract_has_a_class_for_the_thing_it_measures_against(self):
        """THE ROW NOBODY COULD CLEAR. The gate's own suggested call was
        `scale_check(path, klass)` and there was no klass for a PLAYER, while
        the contract's unit is literally player_height_px."""
        assert "player" in scalecontract.CLASSES

    def test_every_gate_row_names_an_action_that_can_clear_it(self, root):
        project.set_dimension(root, "3d")
        scalecontract.set_contract(root, player_height_px=64)
        rows = scalecontract.unmeasured_findings(root)
        audit = findings.audit(rows)
        assert audit["ok"], audit["why"]

    def test_a_row_with_no_action_is_a_harness_bug_not_backlog(self):
        bad = findings.make(gate="scale", key="x", claim="something is wrong",
                            tool="scale_check", clears_by="")
        verdict = findings.actionable(bad)
        assert verdict["ok"] is False
        assert verdict["kind"] == findings.IMPOSSIBLE
        audit = findings.audit([bad])
        assert audit["ok"] is False
        assert "HARNESS BUG" in audit["why"]

    def test_a_judgement_row_is_labelled_as_needing_a_person(self, root,
                                                            monkeypatch):
        monkeypatch.setattr(greenlight, "_audio_unmet", lambda _r: [
            findings.make(gate="audio", key="cue:1", kind=findings.JUDGEMENT,
                          claim="the door cue has never been heard in context",
                          tool="audiohooks", clears_by="audio_listen_record")])
        got = greenlight.presentation_check(root)
        assert any("[needs a person]" in row for row in got["unmet"])
        # A judgement row is CORRECT, not a defect in the gate.
        assert got["satisfiable"] is True


# ---------------------------------------------------------------------------
# §20 A false finding must be retractable, with an audit trail
# ---------------------------------------------------------------------------
class TestFindingProvenance:
    def test_a_finding_carries_the_tool_and_the_measurement(self, root):
        row = findings.record(root, findings.make(
            gate="scale", key="cat.png", claim="30.00 player-heights tall",
            tool="scale_check", inputs={"path": "cat.png", "klass": "prop"},
            measured={"height_px": 1920, "player_height_px": 64},
            clears_by="scale_check('cat.png', klass)"))
        held = findings.ledger(root)[0]
        assert held["tool"] == "scale_check"
        assert held["measured"]["height_px"] == 1920
        assert held["id"] == row["id"]

    def test_a_better_measurement_supersedes_it_and_it_stops_blocking(self, root):
        row = findings.record(root, findings.make(
            gate="scale", key="cat.glb", claim="30.00 player-heights tall",
            tool="scale_check", clears_by="scale_check('cat.glb', klass)"))
        findings.supersede(
            root, row["id"], tool="godot_inspect_resource",
            why="the engine AABB is 0.24 m on the longest axis; the earlier "
                "number measured a render canvas, not the asset",
            measured={"longest_axis_m": 0.24})
        assert findings.standing(root) == []
        # AND IT IS STILL VISIBLE. A retraction is not a delete.
        whole = findings.ledger(root)
        assert len(whole) == 1
        assert whole[0]["superseded_by"]
        assert "render canvas" in whole[0]["superseded_why"]
        assert whole[0]["superseded_by_tool"] == "godot_inspect_resource"
        assert findings.supersessions(root)[0]["_retracts"] == row["id"]

    def test_a_retraction_costs_a_sentence(self, root):
        row = findings.record(root, findings.make(
            gate="scale", key="k", claim="c", clears_by="x"))
        with pytest.raises(ValueError, match="costs a sentence"):
            findings.supersede(root, row["id"], why="wrong")

    def test_retracting_a_finding_that_does_not_exist_is_refused(self, root):
        with pytest.raises(LookupError):
            findings.supersede(root, "deadbeef1234",
                               why="this id was never in the ledger at all")

    def test_a_superseded_row_no_longer_refuses_the_release(self, root,
                                                            monkeypatch):
        claim = "cat.glb is off-scale as prop: 30.00 player-heights tall"
        monkeypatch.setattr(greenlight, "_scale_unmet", lambda _r: [
            findings.make(gate="scale", key="cat.glb", claim=claim,
                          tool="scale_check", clears_by="rescale it")])
        first = greenlight.presentation_check(root)
        assert any(claim in row for row in first["unmet"])
        held = [f for f in findings.standing(root, gate="scale")
                if f["key"] == "cat.glb"][0]
        findings.supersede(
            root, held["id"], tool="godot_inspect_resource",
            why="engine AABB says 0.24 m; the pixel measurement was of a "
                "turnaround render and is not about this project")
        after = greenlight.presentation_check(root)
        assert not any(claim in row for row in after["unmet"])
        assert after["superseded"] and after["superseded"][0]["key"] == "cat.glb"


class TestLabelsNameTheMeasuredTarget:
    """A test emitted `owner CAN guard bedroom (dresser) within 2m` while
    measuring a task marker 2.75 m from the dresser. That sentence was relayed
    into a director report, a filed work item and a dispatched agent's brief
    before anybody checked the coordinate."""

    def test_a_label_naming_a_different_artefact_is_reported(self):
        row = findings.make(
            gate="rooms", key="/Level/BedroomTaskMarker",
            claim="owner CAN guard bedroom (game/props/dresser.tscn) within 2m",
            tool="roomqa", measured={"distance_m": 2.75}, clears_by="move it")
        got = findings.label_check(row)
        assert got["ok"] is False
        assert "game/props/dresser.tscn" in got["why"]
        assert "BedroomTaskMarker" in got["why"]

    def test_a_label_that_names_what_it_measured_is_fine(self):
        row = findings.make(
            gate="scale", key="game/assets/cat.png",
            claim="game/assets/cat.png is off-scale as prop",
            tool="scale_check", clears_by="rescale it")
        assert findings.label_check(row)["ok"] is True

    def test_a_general_statement_naming_nothing_is_not_a_mislabel(self):
        row = findings.make(gate="scale", key="contract",
                            claim="no player_height_px is declared",
                            clears_by="scale_contract_set(...)")
        assert findings.label_check(row)["ok"] is True

    def test_the_audit_surfaces_mislabelled_rows(self):
        bad = findings.make(
            gate="rooms", key="/L/Marker",
            claim="the dresser at game/props/dresser.tscn blocks the door",
            measured={"distance_m": 2.75}, clears_by="move it")
        assert len(findings.audit([bad])["mislabelled"]) == 1


# ---------------------------------------------------------------------------
# §8 Verification must not dirty the tree
# ---------------------------------------------------------------------------
class TestEvidenceDoesNotDirtyTheTree:
    """A dirty tree refuses the WHOLE board, not one item, so taking a
    screenshot stalled every seat. Hit repeatedly."""

    def _repo(self, root):
        import subprocess
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"], ["add", "-A"],
                     ["commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=root, capture_output=True)

    def test_the_harness_own_override_cfg_does_not_dirty_the_tree(self, root):
        (root / "keep.txt").write_text("x", encoding="utf-8")
        self._repo(root)
        assert gitwork.dirty(root)["dirty"] is False
        # What a KILLED capture leaves behind.
        (root / "override.cfg").write_text(
            '[autoload]\nBGateShot="*res://.godot/bgate_run/bgate_shot.gd"\n',
            encoding="utf-8")
        got = gitwork.dirty(root)
        assert got["dirty"] is False, got["paths"]
        # Reported, not hidden: a leftover means a capture was killed.
        assert got["harness_scratch"] == ["override.cfg"]

    def test_a_users_own_override_cfg_STILL_dirties_the_tree(self, root):
        self._repo(root)
        (root / "override.cfg").write_text(
            "[display]\nwindow/size/viewport_width=1920\n", encoding="utf-8")
        got = gitwork.dirty(root)
        assert got["dirty"] is True
        assert "override.cfg" in got["paths"]

    def test_the_engine_cache_is_never_the_agents_work(self, root):
        self._repo(root)
        cache = root / ".godot" / "imported"
        cache.mkdir(parents=True)
        (cache / "x.png-abc.ctex").write_bytes(b"cached")
        assert gitwork.dirty(root)["dirty"] is False

    def test_the_injection_scripts_live_under_the_engine_cache_dir(self):
        from bgate_adapters import godot as _godot
        assert _godot.SCRATCH_DIR.startswith(".godot/")
        assert not _godot._SHOT_SCRIPT.startswith(".")
        assert not _godot._EVIDENCE_SCRIPT.startswith(".")


# ---------------------------------------------------------------------------
# §9 godot_run must be autoload-safe, and must not blame the caller's file
# ---------------------------------------------------------------------------
class TestAutoloadSafety:
    """Scripts naming project autoloads failed under godot_run, including
    scaffold code, with an error naming the CALLING FILE's line number. It read
    as a syntax error the author did not write. ~$9 before it was identified."""

    def _project(self, tmp_path):
        base = tmp_path / "auto"
        base.mkdir()
        (base / "project.godot").write_text(
            'config_version=5\n\n[application]\nrun/main_scene="res://m.tscn"\n'
            '\n[autoload]\nGameState="*res://game_state.gd"\n'
            'Telemetry="*res://telemetry.gd"\n', encoding="utf-8")
        return base

    def test_the_autoloads_are_read_off_project_godot(self, tmp_path):
        from bgate_adapters import godot as _godot
        base = self._project(tmp_path)
        assert _godot.project_autoloads(str(base)) == ["GameState", "Telemetry"]

    def test_a_scenetree_script_touching_an_autoload_is_refused_UP_FRONT(
            self, tmp_path):
        """Before the engine spawns, and naming the real cause. `--script`
        replaces the main loop, so Godot never instantiates autoloads at all —
        verified on 4.4.1: root.get_children() is []."""
        from bgate_adapters import godot as _godot
        base = self._project(tmp_path)
        got = _godot.run_script(
            "extends SceneTree\n\nfunc _init():\n\tprint(GameState.score)\n"
            "\tquit()\n", project_dir=str(base))
        assert got["ok"] is False
        assert got["autoloads"] == ["GameState"]
        assert "reads as a syntax error you did not write" in got["error"]
        assert "extends Node" in got["hint"]
        assert got["seconds"] == 0.0            # nothing was spawned

    def test_a_node_script_is_accepted_rather_than_refused(self, tmp_path):
        """It used to be refused outright with 'must extends SceneTree'. The
        node shape is the one that CAN see this project's autoloads."""
        from bgate_adapters import godot as _godot
        assert _godot._script_gate(
            "extends Node\nfunc _ready():\n\tget_tree().quit()\n") is None

    def test_a_scenetree_script_that_does_not_touch_them_still_runs(
            self, tmp_path):
        from bgate_adapters import godot as _godot
        base = self._project(tmp_path)
        assert _godot._script_gate(
            "extends SceneTree\nfunc _init():\n\tprint(1)\n\tquit()\n",
            str(base)) is None


# ---------------------------------------------------------------------------
# §11 A Blender operator that silently did nothing
# ---------------------------------------------------------------------------
class TestSilentOperatorNoOp:
    """bpy.ops.object.transform_apply returned {'CANCELLED'} on a bad context,
    changed nothing, and the run reported ok. Three identical 'rotated' exports
    were paid for.

    THE ORIGINAL FINDING SAID THE OPERATOR NO-OPS IN HEADLESS BLENDER. It does
    not — verified: called with a valid selection inside blender_run it applies
    correctly. What it does is return CANCELLED instead of raising when nothing
    is selected, and nobody looked at the return value."""

    def test_the_kit_applies_transforms_without_an_operator(self):
        from bgate_adapters import _blender_kit
        source = _blender_kit.KIT
        body = source.split("def bg_apply(")[1].split("\ndef ")[0]
        # The CODE, not the docstring explaining why the operator is gone.
        code = body.split('"""')[2] if body.count('"""') >= 2 else body
        assert "transform_apply" not in code
        assert "data.transform(bake)" in code
        assert "did not bake" in code          # it asserts, it does not hope

    def test_the_kit_ships_a_checker_for_operators_that_stay(self):
        from bgate_adapters import _blender_kit
        assert "def bg_op(" in _blender_kit.KIT
        assert "CANCELLED" in _blender_kit.KIT

    def test_a_discarded_effectful_operator_is_reported_as_an_issue(self):
        from bgate_adapters import blender
        assert blender._discarded_ops(
            "bpy.ops.object.transform_apply(rotation=True)\n") == \
            ["transform_apply"]
        # A wrapped one is not flagged: the lint is about DISCARDED results.
        assert blender._discarded_ops(
            'bg_op(bpy.ops.object.transform_apply(), "apply")\n') == []


# ---------------------------------------------------------------------------
# §12 godot_deliver_asset and the quadruped it failed
# ---------------------------------------------------------------------------
class TestNonHumanoidDelivery:
    def test_an_unsupported_shape_type_is_refused_not_dropped(self, tmp_path):
        from bgate_adapters import godot as _godot
        with pytest.raises(ValueError, match="shape_type"):
            _godot.deliver_asset(str(tmp_path), "x.glb", shape_type="convexo")

    def test_an_unsupported_body_type_is_refused_not_dropped(self, tmp_path):
        from bgate_adapters import godot as _godot
        with pytest.raises(ValueError, match="body_type"):
            _godot.deliver_asset(str(tmp_path), "x.glb", body_type="kinematic")

    def test_an_unknown_asset_class_is_refused(self, tmp_path):
        from bgate_adapters import godot as _godot
        with pytest.raises(ValueError, match="asset_class"):
            _godot.deliver_asset(str(tmp_path), "x.glb", asset_class="cat")

    def test_a_quadruped_collider_passes_the_gate_a_humanoid_would_fail(self):
        """A cat is roughly three times as long as it is tall. The absurdity
        test was written against a PERSON and applied to anything skinned."""
        from bgate_adapters import godot as _godot
        capsule = (0.30, 0.24)                 # 0.6 m across, 0.24 m tall
        as_human = _godot._delivery_checks(
            {"collider_count": 1, "ok": True}, {"ok": True, "total_tris": 10},
            character_capsule=capsule, asset_class="humanoid")
        as_cat = _godot._delivery_checks(
            {"collider_count": 1, "ok": True}, {"ok": True, "total_tris": 10},
            character_capsule=capsule, asset_class="quadruped")
        human_row = [c for c in as_human if c["check"] == "has_collider"][0]
        cat_row = [c for c in as_cat if c["check"] == "has_collider"][0]
        assert human_row["ok"] is False
        assert cat_row["ok"] is True
        assert "quadruped" in cat_row["measured"]

    def test_the_humanoid_band_is_unchanged(self):
        """A 1.75 m character with a 1.63 m capsule — her own arm span —
        shipped green. It must still fail."""
        from bgate_adapters import godot as _godot
        rows = _godot._delivery_checks(
            {"collider_count": 1, "ok": True}, {"ok": True, "total_tris": 10},
            character_capsule=(0.8158, 1.75), asset_class="humanoid")
        row = [c for c in rows if c["check"] == "has_collider"][0]
        assert row["ok"] is False
        assert "1.63 m across" in row["detail"]

    def test_the_authored_origin_is_preserved_unless_asked_otherwise(self):
        from bgate_adapters import godot as _godot
        kept = _godot.character_scene_text(
            "res://a.glb", node_name="A", bounds_size=[1, 2, 1],
            bounds_position=[0, 0, 0], recentre=False)
        moved = _godot.character_scene_text(
            "res://a.glb", node_name="A", bounds_size=[1, 2, 1],
            bounds_position=[0, 0, 0], recentre=True)
        assert "0, 0, 0)" in kept.split("Model")[1]
        assert kept != moved


# ---------------------------------------------------------------------------
# §14 / §16 Live channels, exhausted work, and a reason cut mid-sentence
# ---------------------------------------------------------------------------
def _item(root, seat="gameplay", title="do a thing"):
    return queue.add(root, seat, title, brief="a brief that is long enough")


class TestLiveChannelsAndExhaustion:
    def test_a_reopen_reason_is_not_cut_mid_sentence(self):
        long = ("The collider is the wrong size. " * 90).strip()
        clipped = queue.clip_reason(long)
        assert len(clipped) < len(long)
        assert "reason clipped" in clipped
        # It ends on a boundary, not mid-word.
        assert clipped.split("[")[0].rstrip().endswith(".")

    def test_a_short_reason_is_untouched(self):
        assert queue.clip_reason("fix the door") == "fix the door"

    def test_the_whole_reason_still_reaches_the_brief(self, root):
        item = _item(root)
        queue.set_status(root, item["id"], "failed", result="died")
        long = "Rebuild the route. " * 200
        queue.reopen(root, item["id"], long)
        after = queue.get(root, item["id"])
        assert long[:2000] in after["brief"]
        assert "reason clipped" in after["result"]

    def test_a_steer_is_recorded_in_the_items_own_history(self, root):
        """A mid-run correction used to exist only in the steer inbox, which is
        consumed and deleted. A later reader could not tell which corrections
        shaped a result."""
        item = _item(root)
        steerbox.post(root, int(item["id"]), "the cat faces the door, not away",
                      by="director")
        history = steerbox.history(root, int(item["id"]))
        assert len(history) == 1
        assert "faces the door" in history[0]["text"]
        assert history[0]["by"] == "director"

    def test_exhausted_work_is_not_offered_as_claimable(self, root):
        item = _item(root)
        item_id = int(item["id"])
        assert queue.next_for(root, "gameplay")["id"] == item_id
        queue.mark_exhausted(root, item_id,
                             "the automatic retry budget is spent")
        assert queue.next_for(root, "gameplay") is None
        assert queue.ready(root, seat="gameplay") == []

    def test_exhausted_work_says_WHY_and_what_would_release_it(self, root):
        item = _item(root)
        queue.mark_exhausted(root, int(item["id"]),
                             "it failed on a kie credit block twice")
        row = queue.get(root, int(item["id"]))
        assert queue.is_exhausted(row)
        assert "credit block" in row["exhausted_why"]

    def test_a_reopen_is_the_action_that_clears_exhaustion(self, root):
        item = _item(root)
        item_id = int(item["id"])
        queue.set_status(root, item_id, "failed", result="died")
        queue.mark_exhausted(root, item_id, "budget spent")
        queue.reopen(root, item_id, "the blocker is fixed, try again")
        assert not queue.is_exhausted(queue.get(root, item_id))
        assert queue.next_for(root, "gameplay")["id"] == item_id

    def test_queue_next_and_ready_no_longer_disagree(self, root):
        """THE TWO COPIES OF THE READINESS RULE. next_for carried its own SQL
        and did not filter human-held sources, so queue_next offered a
        qa-gate escalation — a row no auto-dispatcher will ever take."""
        queue.add(root, "director", "QA loop: #1 failed three rounds",
                  brief="a brief that is long enough",
                  source="qa-gate-escalation", source_ref="1")
        assert queue.ready(root, seat="director") == []
        assert queue.next_for(root, "director") is None

    def test_stalled_names_what_would_release_each_row(self, root):
        queue.add(root, "director", "escalation",
                  brief="a brief that is long enough",
                  source="qa-gate-escalation", source_ref="1")
        rows = queue.stalled(root)
        assert len(rows) == 1
        assert "never auto-dispatched" in rows[0]["stalled_because"]
        assert rows[0]["needs"]


# ---------------------------------------------------------------------------
# §15 A question addressed to the director must not become a question to a human
# ---------------------------------------------------------------------------
class TestQuestionRouting:
    def test_no_director_session_fails_immediately_rather_than_orphaning(
            self, root, monkeypatch):
        monkeypatch.setattr(steerbox, "_director_session_live",
                            lambda _r: {"available": False, "detail": {}})
        with pytest.raises(steerbox.NoRecipient, match="reach nobody"):
            steerbox.ask_director(root, "top-down or isometric for the hub?")

    def test_the_refusal_names_every_channel_that_would_have_worked(
            self, root, monkeypatch):
        monkeypatch.setattr(steerbox, "_director_session_live",
                            lambda _r: {"available": False, "detail": {}})
        try:
            steerbox.ask_director(root, "which?")
        except steerbox.NoRecipient as exc:
            said = str(exc)
        assert "queue_add" in said and "decision_add" in said
        assert "seat_post_note" in said
        assert "NOT silently sent to the human" in said

    def test_a_question_to_an_unknown_seat_is_refused(self, root):
        with pytest.raises(steerbox.NoRecipient, match="no 'wizard' seat"):
            steerbox.ask_seat(root, "wizard", "how does the spell work?")

    def test_a_seat_question_lands_on_the_blackboard(self, root):
        got = steerbox.ask_seat(root, "art", "is the cat's tail forked?",
                               by="qa")
        assert got["ok"] and got["delivered_to"] == "seat:art"
        from bgate_core import seats as _seats
        notes = _seats.read_notes(root, role="art")
        assert any("forked" in n["body"] for n in notes)

    def test_a_seat_question_steers_that_seats_running_agent(self, root):
        item = queue.add(root, "art", "paint the cat",
                         brief="a brief that is long enough")
        queue.reserve(root, int(item["id"]))
        got = steerbox.ask_seat(root, "art", "is the tail forked?", by="qa")
        assert got["steered_items"] == [int(item["id"])]
        assert got["live"] is True


# ---------------------------------------------------------------------------
# §17 Do not pay twice for work that already landed
# ---------------------------------------------------------------------------
class TestSalvageBeforeRetry:
    """One item ran three agents and $33 to deliver work that was ~95% complete
    after the first. The retry brief described the job and never mentioned that
    most of it was already on disk."""

    def test_files_the_harness_observed_make_an_item_resumable(self, root):
        from bgate_core import writelog
        item = _item(root, "art")
        item_id = int(item["id"])
        (root / "game").mkdir(exist_ok=True)
        (root / "game" / "cat.glb").write_bytes(b"a real mesh")
        writelog.record(root, "game/cat.glb", "art", salvage.owner_for(item_id))
        got = salvage.inspect(root, item_id)
        assert got["verdict"] == "resumable"
        assert got["files"]["on_disk"] == ["game/cat.glb"]
        assert "STILL ON DISK" in got["why"]

    def test_the_retry_brief_says_do_not_regenerate_it(self, root):
        from bgate_core import writelog
        item = _item(root, "art")
        item_id = int(item["id"])
        (root / "game").mkdir(exist_ok=True)
        (root / "game" / "cat.glb").write_bytes(b"mesh")
        writelog.record(root, "game/cat.glb", "art", salvage.owner_for(item_id))
        note = salvage.brief_note(root, item_id)
        assert "DO NOT REGENERATE" in note
        assert "game/cat.glb" in note
        assert "not a claim the dead agent made about itself" in note
        assert "paying twice" in note

    def test_a_run_that_wrote_nothing_is_honestly_a_regenerate(self, root):
        item = _item(root, "art")
        got = salvage.inspect(root, int(item["id"]))
        assert got["verdict"] == "regenerate"
        assert salvage.brief_note(root, int(item["id"])) == ""

    def test_observed_writes_that_vanished_are_unknown_not_regenerate(self, root):
        from bgate_core import writelog
        item = _item(root, "art")
        item_id = int(item["id"])
        writelog.record(root, "game/gone.glb", "art", salvage.owner_for(item_id))
        got = salvage.inspect(root, item_id)
        assert got["verdict"] == "unknown"
        assert "Look before regenerating" in got["why"]

    def test_the_two_ceilings_are_reported_as_independent(self):
        said = salvage.ceilings({"max_runtime_s": 900, "num_turns": 200})
        assert "independent of one another" in said["note"]
        assert "Raising the clock does nothing for a run that died on turns" \
            in said["note"]


# ---------------------------------------------------------------------------
# §18 Test output, and the engine error that was NOT noise
# ---------------------------------------------------------------------------
class TestTestRunnerSignals:
    def test_assertions_and_the_process_are_two_separate_verdicts(self):
        """`ok: false` with `0` failed assertions read as nonsense and was
        dismissed as harness noise by two readers for a full session. The
        engine error behind it — `N resources still in use at exit` — was a
        REAL leak in the project's own tests."""
        said = enginetests._why(
            assertions_ok=True, process_ok=False, fails=0,
            errors=["ERROR: 3 resources still in use at exit"], reported="")
        assert "ASSERTIONS PASSED" in said
        assert "separate fault" in said
        assert "not automatically noise" in said

    def test_a_failed_assertion_reads_as_a_failed_assertion(self):
        said = enginetests._why(False, True, 2, [], "")
        assert "2 FAIL marker(s)" in said

    def test_both_failing_is_named_as_two_faults(self):
        said = enginetests._why(False, False, 1, ["boom"], "")
        assert "two independent faults" in said

    def test_scratch_files_in_the_test_dir_are_not_suite_members(self):
        """Agents leave `tests/.orig_*.gd` and temp probes behind, and
        Path.glob('*.gd') matches leading dots unlike a shell glob — so a
        backup of a broken file was run and scored as a test."""
        assert enginetests._is_test_script(Path("tests/door_test.gd"))
        assert not enginetests._is_test_script(Path("tests/.orig_door.gd"))
        assert not enginetests._is_test_script(Path("tests/_scratch.gd"))

    def test_the_default_mode_is_concise_and_cites_the_full_log(self):
        result = {
            "ok": False, "scripts_run": 2, "full_log": "/tmp/run.log",
            "scripts": [
                {"script": "a.gd", "ok": True, "_output": "PASS\n" * 500},
                {"script": "b.gd", "ok": False, "_output": "x" * 5000},
            ]}
        concise = enginetests.shape(result, "failures_only")
        assert [s["script"] for s in concise["scripts"]] == ["b.gd"]
        assert concise["scripts_omitted"] == 1
        assert len(concise["scripts"][0]["output"]) <= enginetests.EXCERPT_CHARS
        assert "/tmp/run.log" in concise["note"]

    def test_full_mode_returns_every_script_whole(self):
        result = {"ok": False, "scripts_run": 1, "full_log": "",
                  "scripts": [{"script": "a.gd", "ok": True,
                               "_output": "PASS\n" * 500}]}
        whole = enginetests.shape(result, "full")
        assert whole["scripts"][0]["output"].count("PASS") == 500
        assert whole["scripts_omitted"] == 0

    def test_changed_mode_shows_only_scripts_whose_verdict_MOVED(self):
        result = {"ok": False, "scripts_run": 2, "full_log": "", "scripts": [
            {"script": "a.gd", "ok": False, "changed": True, "_output": "x"},
            {"script": "b.gd", "ok": False, "changed": False, "_output": "y"}]}
        moved = enginetests.shape(result, "changed")
        assert [s["script"] for s in moved["scripts"]] == ["a.gd"]

    def test_an_unknown_mode_is_refused(self, root):
        with pytest.raises(ValueError, match="mode must be one of"):
            enginetests.run(root, mode="everything")


# ---------------------------------------------------------------------------
# §19 Two engine processes on one .godot cache
# ---------------------------------------------------------------------------
class TestEngineSerialisation:
    """The deadlock's symptom is identical to a hang, and the rule lived in a
    hand-written seat note that every operator and agent had to remember."""

    def test_a_second_engine_call_waits_rather_than_racing(self, tmp_path):
        from bgate_core import enginelock
        with enginelock.hold(tmp_path, "first"):
            assert enginelock.holder(tmp_path)["what"] == "first"
            with pytest.raises(enginelock.EngineBusy, match="DEADLOCK"):
                with enginelock.hold(tmp_path, "second", wait_s=0):
                    pass

    def test_the_lock_is_released_even_when_the_call_raises(self, tmp_path):
        from bgate_core import enginelock
        with pytest.raises(RuntimeError):
            with enginelock.hold(tmp_path, "boom"):
                raise RuntimeError("the engine died")
        assert enginelock.holder(tmp_path) == {}

    def test_a_dead_holder_does_not_lock_the_project_forever(self, tmp_path):
        from bgate_core import enginelock
        path = enginelock.lock_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 999999, "what": "a killed run",
                                    "expires_at": 1.0}), encoding="utf-8")
        with enginelock.hold(tmp_path, "next", wait_s=1) as waited:
            assert waited["broke_stale"] is True

    def test_contention_is_reported_not_hidden(self, tmp_path):
        from bgate_core import enginelock
        with enginelock.hold(tmp_path, "only") as waited:
            assert waited["contended"] is False


# ---------------------------------------------------------------------------
# §21 "The brief's premise is false" as a first-class outcome
# ---------------------------------------------------------------------------
class TestPremiseRefuted:
    """Three times an agent was handed a brief with a false MEASURED premise,
    twice authored by the director. Each refusal stopped a wrong fix shipping,
    and each survived only as prose in a result note."""

    def test_it_carries_the_claim_the_measurement_and_what_happened_instead(
            self, root):
        item = _item(root, "gameplay", "move the dresser out of the guard cone")
        got = queue.premise_refuted(
            root, int(item["id"]),
            claim="the owner has only 0.14 m of clearance guarding the bedroom",
            measured="the assertion measures BedroomTaskMarker at (3.1, 0, 1.4), "
                     "2.75 m from the dresser it claims to be about",
            did_instead="fixed the mislabelled assertion and added the missing "
                        "half of the gate, with a deliberately-wrong control",
            by="gameplay")
        assert got["recorded"] is True
        assert "0.14 m" in got["claim"]
        rows = queue.refutations(root)
        assert len(rows) == 1
        assert rows[0]["item"] == str(item["id"])
        assert "2.75 m" in rows[0]["measured"]

    def test_a_refutation_without_a_measurement_is_refused(self, root):
        item = _item(root)
        with pytest.raises(ValueError, match="measurement that contradicts"):
            queue.premise_refuted(
                root, int(item["id"]),
                claim="the guard clearance figure in this brief is wrong",
                measured="", did_instead="fixed the assertion instead")

    def test_it_does_not_close_the_item(self, root):
        item = _item(root)
        queue.premise_refuted(
            root, int(item["id"]),
            claim="the vision cone has a blind spot below the guard",
            measured="the cone uses the flattened horizontal bearing, so the "
                     "claimed blind spot cannot exist",
            did_instead="traced the implementation and built the correct fix")
        assert queue.get(root, int(item["id"]))["status"] == "queued"

    def test_it_lands_on_the_items_own_record(self, root):
        item = _item(root)
        queue.premise_refuted(
            root, int(item["id"]),
            claim="the previous attempt reported PASS on this route",
            measured="the driver accepted arrival on a single mid-air frame",
            did_instead="fixed the driver, then re-ran the route")
        assert "PREMISE REFUTED" in queue.get(root, int(item["id"]))["result"]

    def test_it_reaches_the_morning_report(self, root):
        from bgate_core import gameplan
        item = _item(root)
        queue.premise_refuted(
            root, int(item["id"]),
            claim="the guard clearance is 0.14 m",
            measured="the marker is 2.75 m from the real target",
            did_instead="fixed the assertion")
        digest = gameplan.digest(root, hours=24)
        assert digest["premise_refuted"]
        assert "0.14 m" in digest["premise_refuted"][0]["claim"]


# ---------------------------------------------------------------------------
# §22 A total that silently omits a channel
# ---------------------------------------------------------------------------
class TestSpendIsWhole:
    """board_digest reported 84 credits across 11 kie calls that were in no
    spend figure and in no budget ceiling."""

    def test_the_total_carries_the_unpriced_channel_with_it(self, root):
        for _ in range(11):
            spend.record_unpriced(root, 7.64, kind="image", logical_name="cat")
        totals = spend.totals(root)
        assert totals["complete"] is False
        assert "unpriced credits" in totals["spend_line"]
        assert "11 calls" in totals["spend_line"]
        assert "BGATE_KIE_USD_PER_CREDIT" in totals["spend_line"]

    def test_a_project_with_no_unpriced_rows_prints_a_plain_total(self, root):
        spend.record(root, 4.12, kind="image")
        totals = spend.totals(root)
        assert totals["complete"] is True
        assert totals["spend_line"] == "$4.12"

    def test_the_budget_verdict_declares_its_own_blind_spot(self, root):
        spend.set_budget(root, per_project_usd=100, enforced=1)
        spend.record_unpriced(root, 84, kind="image")
        verdict = spend.check(root, projected_usd=1.0)
        assert verdict["allowed"] is True
        assert verdict["unpriced_rows"] == 1
        assert "cannot see them" in verdict["blind_spot"]

    def test_the_digest_prints_the_combined_line(self, root):
        from bgate_core import gameplan
        spend.record_unpriced(root, 84, kind="image")
        digest = gameplan.digest(root, hours=24)
        assert digest["spend"]["complete"] is False
        assert "unpriced credits" in digest["spend"]["line"]


# ---------------------------------------------------------------------------
# §25 Dependency order must be visually readable
# ---------------------------------------------------------------------------
class TestExecutionOrderIsReadable:
    """#42 done, #45 running, #43 queued read as a scheduler that skipped #43.
    It did not: #43 was filed after #42, then #45 was inserted between them
    because the route measurements needed real furniture dimensions."""

    def _chain(self, root):
        # Exactly the observed sequence, including the LATER-created insert.
        enlarge = queue.add(root, "gameplay", "Enlarge rooms",
                            brief="a brief that is long enough")
        routes = queue.add(root, "gameplay", "Rebuild climbing routes",
                           brief="a brief that is long enough",
                           depends_on=int(enlarge["id"]))
        furniture = queue.add(root, "art", "Swap in furniture",
                              brief="a brief that is long enough",
                              depends_on=int(enlarge["id"]))
        # #45 is inserted BETWEEN them, after both already existed.
        queue.add_dependency(root, int(routes["id"]), int(furniture["id"]))
        return (int(enlarge["id"]), int(routes["id"]), int(furniture["id"]))

    def test_ids_are_never_renumbered(self, root):
        enlarge, routes, furniture = self._chain(root)
        assert enlarge < routes < furniture      # creation order, untouched
        graph = queue.graph(root)
        assert {n["id"] for n in graph["nodes"]} == {enlarge, routes, furniture}

    def test_execution_order_is_enlarge_then_furniture_then_routes(self, root):
        enlarge, routes, furniture = self._chain(root)
        assert queue.graph(root)["order"] == [enlarge, furniture, routes]

    def test_the_two_dependency_stores_are_presented_as_ONE_graph(self, root):
        """work_item.depends_on holds #42 and work_item_dep holds #45. A user
        should not have to understand the database representation."""
        enlarge, routes, furniture = self._chain(root)
        node = [n for n in queue.graph(root)["nodes"] if n["id"] == routes][0]
        assert sorted(node["depends_on"]) == sorted([enlarge, furniture])

    def test_the_blocked_item_says_WHY_rather_than_looking_skipped(self, root):
        enlarge, routes, furniture = self._chain(root)
        queue.set_status(root, enlarge, "done")
        queue.reserve(root, furniture)           # 'running'
        line = queue.waiting_line(root, routes)
        assert line.startswith(f"WAITING ON #{furniture}")
        assert "Swap in furniture" in line       # the TITLE, not just the id

    def test_execution_state_separates_waiting_from_blocked(self, root):
        enlarge, routes, furniture = self._chain(root)
        queue.set_status(root, enlarge, "done")
        by_id = {n["id"]: n for n in queue.graph(root)["nodes"]}
        assert by_id[furniture]["execution_state"] == "ready"
        assert by_id[routes]["execution_state"] == "waiting"
        # A predecessor that will never land on its own is BLOCKED, not waiting.
        queue.set_status(root, furniture, "cancelled")
        by_id = {n["id"]: n for n in queue.graph(root)["nodes"]}
        assert by_id[routes]["execution_state"] == "blocked"

    def test_multiple_parents_name_all_of_them_and_which_one_blocks(self, root):
        enlarge, routes, furniture = self._chain(root)
        node = [n for n in queue.graph(root)["nodes"] if n["id"] == routes][0]
        assert sorted(node["unresolved"]) == sorted([enlarge, furniture])
        assert node["blocking_now"] in (enlarge, furniture)

    def test_successors_reads_BOTH_stores(self, root):
        """It read the depends_on column alone, so the graph could be walked in
        one direction only."""
        enlarge, routes, furniture = self._chain(root)
        assert routes in [int(r["id"]) for r in queue.successors(root, furniture)]

    def test_the_execution_path_of_one_item_is_its_whole_chain_in_order(
            self, root):
        enlarge, routes, furniture = self._chain(root)
        path = [row["id"] for row in queue.execution_path(root, routes)]
        assert path.index(enlarge) < path.index(furniture) < path.index(routes)

    def test_the_dashboard_api_says_the_same_thing(self, root):
        """§25 names UI/API/graph surfaces. The console payload is the one the
        dashboard draws, and it carried four booleans a reader had to combine."""
        from bgate_ui.routes import console as _console

        enlarge, routes, furniture = self._chain(root)
        queue.set_status(root, enlarge, "done")
        queue.reserve(root, furniture)
        conn = db.connect(root)
        rows = conn.execute("SELECT * FROM work_item ORDER BY id").fetchall()
        cards = [_console._card(r) for r in rows]
        _console._chain_state(conn, cards)
        by_id = {int(c["id"]): c for c in cards}
        assert by_id[furniture]["execution_state"] == "running"
        assert by_id[routes]["execution_state"] == "waiting"
        assert by_id[routes]["waiting_line"].startswith(
            f"WAITING ON #{furniture} Swap in furniture")
        # BOTH stores, merged, on the wire.
        assert sorted(by_id[routes]["depends_on_all"]) == sorted(
            [enlarge, furniture])

    def test_the_api_separates_blocked_from_waiting(self, root):
        from bgate_ui.routes import console as _console

        enlarge, routes, furniture = self._chain(root)
        queue.set_status(root, enlarge, "done")
        queue.set_status(root, furniture, "cancelled")
        conn = db.connect(root)
        cards = [_console._card(r) for r in
                 conn.execute("SELECT * FROM work_item").fetchall()]
        _console._chain_state(conn, cards)
        routes_card = [c for c in cards if int(c["id"]) == routes][0]
        assert routes_card["execution_state"] == "blocked"
        assert routes_card["stuck"] is True

    def test_the_queue_endpoint_no_longer_offers_a_doomed_deploy_button(
            self, root):
        """A THIRD COPY OF THE BLOCKING RULE. /api/queue's _with_chain_state
        read the depends_on COLUMN only, so an item held by an extra parent in
        work_item_dep came back ready=true — with a deploy button whose one
        possible outcome was a refusal — as long as its single-column parent
        happened to be done. This is the endpoint the board fetches."""
        from bgate_ui import app as _app

        enlarge, routes, furniture = self._chain(root)
        queue.set_status(root, enlarge, "done")     # the COLUMN parent lands
        queue.reserve(root, furniture)              # the TABLE parent runs on
        row = dict(db.connect(root).execute(
            "SELECT * FROM work_item WHERE id = ?", (routes,)).fetchone())
        got = _app._with_chain_state(root, row)
        assert got["ready"] is False
        assert got["execution_state"] == "waiting"
        assert got["waiting_on"]["id"] == furniture
        assert sorted(got["depends_on_all"]) == sorted([enlarge, furniture])
        assert got["waiting_line"].startswith(
            f"WAITING ON #{furniture} Swap in furniture")

    def test_the_queue_endpoint_separates_blocked_from_waiting(self, root):
        from bgate_ui import app as _app

        enlarge, routes, furniture = self._chain(root)
        queue.set_status(root, enlarge, "done")
        queue.set_status(root, furniture, "cancelled")
        row = dict(db.connect(root).execute(
            "SELECT * FROM work_item WHERE id = ?", (routes,)).fetchone())
        got = _app._with_chain_state(root, row)
        assert got["execution_state"] == "blocked"
        assert "will not reach 'done' on its own" in got["waiting_line"]

    def test_the_queue_endpoint_reports_exhausted_work(self, root):
        from bgate_ui import app as _app

        item = _item(root)
        queue.mark_exhausted(root, int(item["id"]), "retry budget spent")
        row = dict(db.connect(root).execute(
            "SELECT * FROM work_item WHERE id = ?", (int(item["id"]),)).fetchone())
        got = _app._with_chain_state(root, row)
        assert got["ready"] is False
        assert got["execution_state"] == "exhausted"

    def test_the_api_shows_exhausted_work_as_its_own_state(self, root):
        from bgate_ui.routes import console as _console

        item = _item(root)
        queue.mark_exhausted(root, int(item["id"]), "retry budget spent")
        conn = db.connect(root)
        cards = [_console._card(r) for r in
                 conn.execute("SELECT * FROM work_item").fetchall()]
        _console._chain_state(conn, cards)
        assert cards[0]["execution_state"] == "exhausted"
        assert "budget spent" in cards[0]["exhausted_why"]

# ===========================================================================
# SECOND ROUND (§26-§35) — what the same benchmark found after the first pass
# ===========================================================================
# Several of these are defects the FIRST pass introduced. §26 in particular is
# the inverse of §9: the autoload gate added to stop a misleading error started
# producing a misleading silence.


class TestARefusalIsNotAResult:
    """§26. The autoload gate refused seven healthy scripts and reported each
    as 0 passed / 0 failed. Zero-and-zero reads as "nothing to see", not "I
    declined to look" — and behind those empty scores sat 441 passing
    assertions and 2 that genuinely failed."""

    def _project(self, root):
        base = Path(root) / "game"
        (base / "tests").mkdir(parents=True, exist_ok=True)
        (base / "project.godot").write_text(
            'config_version=5\n\n[application]\nrun/main_scene="res://m.tscn"\n'
            '\n[autoload]\nScale="*res://scale.gd"\n', encoding="utf-8")
        return base

    def test_the_gate_marks_its_refusals_as_refusals(self):
        from bgate_adapters import godot as _godot
        got = _godot._script_gate("extends RefCounted\nfunc x():\n\tpass\n")
        assert got is not None
        assert got["refused"] == "wrong_base_class"

    def test_a_refused_script_is_not_scored_zero_and_zero(self, root):
        base = self._project(root)
        (base / "tests" / "foundation_test.gd").write_text(
            "extends SceneTree\n\nfunc _init():\n\tprint(Scale.COUNTER)\n"
            "\tquit()\n", encoding="utf-8")
        got = enginetests.run(root, mode="full")
        row = got["scripts"][0]
        # None, not 0. A number invites arithmetic and there is no honest
        # number here.
        assert row["passed"] is None and row["failed"] is None
        assert row["ran"] is False
        assert row["refused"] == "autoload_unreachable"
        assert "NOT been checked" in row["why_not_run"]

    def test_the_totals_separate_attempted_from_actually_run(self, root):
        base = self._project(root)
        (base / "tests" / "a_test.gd").write_text(
            "extends SceneTree\n\nfunc _init():\n\tprint(Scale.X)\n\tquit()\n",
            encoding="utf-8")
        got = enginetests.run(root, mode="summary")
        assert got["scripts_attempted"] == 1
        assert got["scripts_run"] == 0          # it counted 1 before
        assert got["scripts_refused"] == 1
        assert "NOT RUN" in got["refused_note"]

    def test_a_suite_with_a_refused_script_is_not_green(self, root):
        base = self._project(root)
        (base / "tests" / "a_test.gd").write_text(
            "extends SceneTree\n\nfunc _init():\n\tprint(Scale.X)\n\tquit()\n",
            encoding="utf-8")
        got = enginetests.run(root)
        assert got["ok"] is False


class TestUnknownArgumentsAreRefused:
    """§32. `godot_test_run(only=[...])` — the real parameter is `paths`.
    FastMCP validates against the schema and pydantic IGNORES extra keys, so
    the argument was dropped, all fifteen scripts ran, and the result said
    nothing about the parameter it had discarded."""

    @pytest.mark.anyio
    async def test_a_typo_is_refused_rather_than_dropped(self):
        from bgate_mcp import server as _server
        got = await _call(_server, "godot_test_run",
                          {"only": ["tests/door_test.gd"]})
        assert got["ok"] is False
        assert got["refused"] == "unknown_argument"
        assert got["unknown_arguments"] == ["only"]

    @pytest.mark.anyio
    async def test_it_names_the_parameter_you_probably_meant(self):
        from bgate_mcp import server as _server
        got = await _call(_server, "godot_test_run", {"path": "x.gd"})
        assert "did you mean 'paths'" in got["error"]

    @pytest.mark.anyio
    async def test_a_correct_call_is_untouched(self):
        from bgate_mcp import server as _server
        got = await _call(_server, "queue_get", {"item_id": 987654})
        assert got.get("refused") != "unknown_argument"


class TestHungAndExitedAreDifferentDeaths:
    """§31. A session that exited cleanly after 20 SECONDS was banked as "no
    output of any kind for 25 minutes - the session was hung", because silence
    is measured against the log and a finished process writes nothing either."""

    def test_a_hang_is_only_claimed_for_a_process_that_is_alive(self):
        import inspect
        from bgate_ui import dispatch as _dispatch
        source = inspect.getsource(_dispatch._watch_completion)
        stall = source.split("silent >= STALL_S")[1]
        # The poll must come BEFORE the trip, inside the stall branch.
        assert stall.index('entry["proc"].poll()') < stall.index("_trip(")
        assert "still running and silent" in stall

    def test_the_kill_note_keeps_the_runs_own_last_words(self):
        import inspect
        from bgate_ui import dispatch as _dispatch
        source = inspect.getsource(_dispatch._trip)
        assert "_last_words(" in source
        assert "THE RUN'S OWN LAST WORDS" in source

    def test_last_words_is_quiet_when_there_is_nothing_to_quote(self, root):
        from bgate_ui import dispatch as _dispatch
        assert _dispatch._last_words(str(root), 999999) == ""


class TestTheClaudeRunnerRegistersTheServer:
    """§28. `_claude_args` allow-listed `mcp__builders-gate` and never
    registered it — so whether a dispatched agent had the pipeline's tools at
    all depended on the human's ambient config. `_codex_args` had called
    mcp_overrides() since the day codex was added."""

    def test_the_server_is_registered_per_invocation(self):
        from bgate_ui import runners
        args = runners._claude_args("claude", permission_mode="acceptEdits",
                                    model=None, cwd=".", native_images=False)
        assert "--mcp-config" in args
        cfg = json.loads(args[args.index("--mcp-config") + 1])
        assert runners.MCP_SERVER_NAME in cfg["mcpServers"]

    def test_it_does_not_evict_the_humans_own_servers(self):
        from bgate_ui import runners
        args = runners._claude_args("claude", permission_mode="acceptEdits",
                                    model=None, cwd=".", native_images=False)
        # --strict-mcp-config would drop every server the user configured.
        assert "--strict-mcp-config" not in args

    def test_the_allow_list_still_names_the_prefix(self):
        from bgate_ui import runners
        args = runners._claude_args("claude", permission_mode="acceptEdits",
                                    model=None, cwd=".", native_images=False)
        assert f"mcp__{runners.MCP_SERVER_NAME}" in args


class TestHarnessEditedUnderRunningAgents:
    """§27. Two failure modes pointing opposite ways: a fresh spawn imports a
    half-written package, and a running agent keeps its old import for life."""

    def test_a_live_edit_is_visible_as_a_live_edit(self, tmp_path):
        from bgate_core import harness
        repo = tmp_path / "repo"
        (repo / "bgate_core").mkdir(parents=True)
        (repo / "bgate_core" / "x.py").write_text("x = 1\n", encoding="utf-8")
        assert harness.recently_edited(repo=repo) == ["bgate_core/x.py"]
        # Nothing written for a while reads as settled.
        assert harness.recently_edited(within_s=0.0, repo=repo) == []

    def test_block_mode_refuses_a_spawn_mid_edit(self, tmp_path, monkeypatch):
        from bgate_core import harness
        repo = tmp_path / "repo"
        (repo / "bgate_core").mkdir(parents=True)
        (repo / "bgate_core" / "x.py").write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(harness, "repo_root", lambda: repo)
        monkeypatch.setenv("BGATE_HARNESS_GUARD", "block")
        got = harness.spawn_guard(wait=False)
        assert got["ok"] is False
        assert "half-written" in got["why"]

    def test_warn_is_the_default_because_a_dammed_board_is_worse(self):
        from bgate_core import harness
        assert harness.DEFAULT_MODE == "warn"

    def test_drift_names_the_module_cache_rather_than_blaming_the_fix(self):
        from bgate_core import harness
        got = harness.drift("0000000000000000")
        assert got["drifted"] is True
        assert "STILL EXECUTING THE OLD COPY" in got["why"]
        assert "do not read its behaviour as a verdict on the fix" in got["why"]

    def test_the_spawn_stamps_which_harness_it_started_against(self):
        import inspect
        from bgate_ui import dispatch as _dispatch
        source = inspect.getsource(_dispatch._spawn)
        assert '"harness": _harness_at' in source
        assert "spawn_guard()" in source


class TestTheLedgerCanUndoADestructiveEdit:
    """§29. The write ledger stored paths and not content, so an agent
    replacing a file with a stub was recorded accurately and irrecoverably."""

    def test_the_previous_content_is_captured_on_first_touch(self, root):
        from bgate_core import writelog
        (root / "game").mkdir(exist_ok=True)
        target = root / "game" / "player.gd"
        original = "# careful work\n" * 40
        target.write_text(original, encoding="utf-8")

        writelog.preimage(root, "game/player.gd", "item-7")
        target.write_text("# oops\n", encoding="utf-8")
        kept = writelog.preimage_dir(root, "item-7") / "game/player.gd"
        assert kept.read_text(encoding="utf-8") == original

    def test_a_second_touch_does_not_overwrite_the_first(self, root):
        from bgate_core import writelog
        (root / "game").mkdir(exist_ok=True)
        target = root / "game" / "a.gd"
        target.write_text("first\n", encoding="utf-8")
        writelog.preimage(root, "game/a.gd", "item-7")
        target.write_text("second\n", encoding="utf-8")
        writelog.preimage(root, "game/a.gd", "item-7")
        kept = writelog.preimage_dir(root, "item-7") / "game/a.gd"
        # The state before the RUN, not before the last keystroke.
        assert kept.read_text(encoding="utf-8") == "first\n"

    def test_a_new_file_has_no_preimage_and_says_why(self, root):
        from bgate_core import writelog
        got = writelog.preimage(root, "game/brand_new.gd", "item-7")
        assert got["kept"] is False
        assert "did not exist before" in got["why"]

    def test_something_too_big_is_declined_not_silently_skipped(self, root):
        from bgate_core import writelog
        (root / "game").mkdir(exist_ok=True)
        big = root / "game" / "mesh.glb"
        big.write_bytes(b"x" * (writelog.MAX_PREIMAGE_BYTES + 1))
        got = writelog.preimage(root, "game/mesh.glb", "item-7")
        assert got["kept"] is False
        assert "recover this one from git" in got["why"]

    def test_recoverable_declares_what_it_does_not_cover(self, root):
        from bgate_core import writelog
        (root / "game").mkdir(exist_ok=True)
        (root / "game" / "a.gd").write_text("a\n", encoding="utf-8")
        writelog.preimage(root, "game/a.gd", "item-7")
        writelog.record(root, "game/a.gd", "gameplay", "item-7")
        writelog.record(root, "game/never_existed.gd", "gameplay", "item-7")
        got = writelog.recoverable(root, "item-7")
        assert got["preimages"] == ["game/a.gd"]
        assert got["no_preimage"] == ["game/never_existed.gd"]
        assert "AUTO-COMMIT IS THE REAL SAFETY NET" in got["recovery"]


class TestUncommittedIsNotUnsaved:
    """§30. Auto-commit is the real safety net and nothing said so."""

    def test_dirty_explains_what_dirty_means(self, root):
        import subprocess
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"], ["add", "-A"],
                     ["commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=root, capture_output=True)
        (root / "new.txt").write_text("x", encoding="utf-8")
        got = gitwork.dirty(root)
        assert got["dirty"] is True
        assert "UNCOMMITTED IS NOT UNSAVED" in got["means"]
        assert "dispatch.auto_commit" in got["means"]


class TestEveryGateDeclaresWhatItDoesNotMeasure:
    """§34. A green check is read as a statement about the thing; it is only
    ever a statement about what was measured."""

    def test_every_presentation_section_declares_its_limits(self, root):
        got = greenlight.presentation_check(root)
        spots = got["blind_spots"]
        assert spots["undeclared"] == []
        assert set(spots["declared"]) == set(greenlight._SECTIONS)

    def test_an_undeclared_gate_is_reported_as_a_gap_in_the_gate(self):
        got = findings.blind_spots(["scale", "something_new"])
        assert got["undeclared"] == ["something_new"]
        assert "not a guarantee" in got["why"]

    def test_the_scale_gate_names_the_dimension_trap(self):
        said = findings.BLIND_SPOTS["scale"]
        assert "opaque pixels" in said and "engine geometry" in said

    def test_the_traversal_gate_admits_it_proves_one_route(self):
        said = findings.BLIND_SPOTS["traversal"]
        assert "ONE route" in said


class TestBoardRowsCarryLiveness:
    """§33. `num_turns` and `total_cost_usd` are written at COMPLETION, so the
    two numbers that look like progress are the two that cannot report it while
    you need them. The only way to tell working from wedged was `stat`."""

    def test_a_running_item_carries_its_own_silence(self):
        from bgate_ui.routes import console as _console
        items = [{"id": 1, "status": "dispatched"}]
        _console._liveness(items, [{"item_id": 1, "last_output_s": 12,
                                    "seconds": 300, "cost_usd": 0.4,
                                    "runner": "claude"}])
        assert items[0]["progress"] == "working"
        assert items[0]["last_output_s"] == 12
        # The LIVE cost, not the column that stays 0 until completion.
        assert items[0]["live_cost_usd"] == 0.4

    def test_a_wedged_item_says_stalled_and_says_why(self):
        from bgate_ui.routes import console as _console
        items = [{"id": 1, "status": "dispatched"}]
        _console._liveness(items, [{"item_id": 1,
                                    "last_output_s": _console._STALL_S + 60}])
        assert items[0]["progress"] == "stalled"
        assert "watchdog kills" in items[0]["progress_why"]

    def test_quiet_is_not_stalled_because_an_atomic_call_writes_nothing(self):
        from bgate_ui.routes import console as _console
        items = [{"id": 1, "status": "dispatched"}]
        _console._liveness(items, [{"item_id": 1,
                                    "last_output_s": _console._QUIET_S + 1}])
        assert items[0]["progress"] == "quiet"
        assert "Often legitimate" in items[0]["progress_why"]

    def test_an_item_with_no_agent_is_left_alone(self):
        from bgate_ui.routes import console as _console
        items = [{"id": 1, "status": "queued"}]
        _console._liveness(items, [])
        assert "progress" not in items[0]


class TestGodotRunTakesAPath:
    """§35. Passing a path ran the PATH as GDScript: `res://tests/x.gd` is not
    a statement, so the engine reported a parse error on line 1 of a file the
    caller never wrote."""

    def test_a_single_line_ending_in_gd_is_read_as_a_path(self, tmp_path):
        from bgate_mcp import server as _server
        script = tmp_path / "probe.gd"
        script.write_text("extends SceneTree\n", encoding="utf-8")
        source, came_from = _server._script_source(str(script), None)
        assert source == "extends SceneTree\n"
        assert came_from == str(script)

    def test_res_paths_resolve_against_the_godot_project(self, tmp_path):
        from bgate_mcp import server as _server
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "a.gd").write_text("extends SceneTree\n",
                                                 encoding="utf-8")
        source, _ = _server._script_source("res://tests/a.gd", str(tmp_path))
        assert source == "extends SceneTree\n"

    def test_actual_source_is_never_mistaken_for_a_path(self):
        from bgate_mcp import server as _server
        program = "extends SceneTree\n\nfunc _init():\n\tquit()\n"
        assert _server._script_source(program, None) == (None, "")

    def test_a_one_line_program_is_not_a_path(self):
        from bgate_mcp import server as _server
        # No .gd suffix, so it is source however short it is.
        assert _server._script_source("extends SceneTree", None) == (None, "")


async def _call(server_module, tool: str, arguments: dict) -> dict:
    result = await server_module.mcp.call_tool(tool, arguments)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.fixture()
def anyio_backend():
    return "asyncio"
