"""Structured visual evidence — DESIGN.md §9 on the shipped screenshot path.

Split deliberately in two. The manifest ASSERTIONS (`check_ui_matches`) are pure
functions over a dict and always run. The capture itself needs a real display and
a real engine, so those tests skip without Godot rather than mocking the boundary
— mocking would only prove the mock works, and every bug this code can have lives
in the boundary.
"""
from __future__ import annotations

import json

import pytest

from bgate_adapters import godot


# --- assertions over a manifest (no engine needed) -------------------------


MANIFEST = {
    "frame": "beauty.png",
    "buffers": ["beauty", "collision", "ui_layout"],
    "viewport": [1280, 720],
    "entities": {
        "Tommy": {"screen_bounds": [110, 232, 154, 270], "visible": True,
                  "class": "AnimatedSprite2D", "z": 0},
        "Scoville": {"screen_bounds": [400, 232, 444, 270], "visible": True,
                     "class": "AnimatedSprite2D", "z": 0},
        "OffScreen": {"screen_bounds": [-90, 10, -40, 60], "visible": True,
                      "class": "Sprite2D", "z": 0},
    },
    "ui": {
        "PlayerHealth": {"screen_bounds": [36, 24, 310, 32],
                         "value": {"value": 92.0, "max": 150.0}, "visible": True},
        "RoundLabel": {"screen_bounds": [600, 20, 680, 40],
                       "value": {"text": "ROUND 1"}, "visible": True},
    },
}


class TestUiAssertions:
    def test_matching_value_passes(self):
        """The §9 payoff: prove the HUD agrees with the sim, not eyeball a PNG."""
        got = godot.check_ui_matches(MANIFEST, {"PlayerHealth": 92.0})
        assert got["ok"] is True
        assert got["checks"][0]["actual"] == 92.0

    def test_mismatched_value_fails_with_both_numbers(self):
        got = godot.check_ui_matches(MANIFEST, {"PlayerHealth": 60.0})
        assert got["ok"] is False
        check = got["checks"][0]
        assert check["expected"] == 60.0 and check["actual"] == 92.0
        # The bounds come back too, so a failure can be pointed at on the frame.
        assert check["screen_bounds"] == [36, 24, 310, 32]

    def test_tolerance_absorbs_a_tweening_bar(self):
        """A health bar animating toward its target is legitimately a hair off
        for a few frames. Exact equality would fail on animation, not on a bug."""
        assert godot.check_ui_matches(MANIFEST, {"PlayerHealth": 92.4})["ok"] is True
        assert godot.check_ui_matches(
            MANIFEST, {"PlayerHealth": 92.4}, tolerance=0.01)["ok"] is False

    def test_text_values_compare_as_strings(self):
        assert godot.check_ui_matches(MANIFEST, {"RoundLabel": "ROUND 1"})["ok"] is True
        assert godot.check_ui_matches(MANIFEST, {"RoundLabel": "ROUND 2"})["ok"] is False

    def test_missing_node_is_a_failure_not_a_crash(self):
        """A renamed HUD node must fail the check, loudly. Silently passing
        because the node vanished is the worst possible outcome for a QA gate."""
        got = godot.check_ui_matches(MANIFEST, {"Nonexistent": 1})
        assert got["ok"] is False
        assert "not found" in got["checks"][0]["error"]

    def test_accepts_a_bare_ui_dict_too(self):
        """Callers holding just the `ui` sub-dict should not have to re-wrap it."""
        got = godot.check_ui_matches(MANIFEST["ui"], {"PlayerHealth": 92.0})
        assert got["ok"] is True

    def test_several_checks_report_individually(self):
        got = godot.check_ui_matches(
            MANIFEST, {"PlayerHealth": 92.0, "RoundLabel": "WRONG"})
        assert got["ok"] is False
        assert [c["ok"] for c in got["checks"]] == [True, False]


class TestManifestShape:
    def test_offscreen_entity_is_detectable_from_bounds(self):
        """"Is it on screen at all" is a question a beauty frame cannot answer
        and this manifest can — negative bounds against the viewport width."""
        width, height = MANIFEST["viewport"]
        offscreen = [
            name for name, e in MANIFEST["entities"].items()
            if e["screen_bounds"][2] < 0 or e["screen_bounds"][0] > width
            or e["screen_bounds"][3] < 0 or e["screen_bounds"][1] > height
        ]
        assert offscreen == ["OffScreen"]

    def test_buffers_are_from_the_schema_enum(self):
        """Matches schemas/evidence_manifest.schema.json's closed buffer enum."""
        allowed = {"beauty", "entity_id", "collision", "navigation", "depth",
                   "ui_layout", "bone_overlay"}
        assert set(MANIFEST["buffers"]) <= allowed


# --- guard rails (no engine needed) ----------------------------------------


class TestGuards:
    def test_missing_project_is_reported(self, tmp_path):
        got = godot.evidence(str(tmp_path), str(tmp_path / "out"))
        assert got["ok"] is False
        assert "project.godot" in got["error"]

    def test_refuses_to_clobber_an_existing_override(self, tmp_path):
        """The capture injects an autoload via override.cfg. A project that
        already has one is someone's real config — never overwrite it."""
        (tmp_path / "project.godot").write_text("config_version=5\n",
                                                encoding="utf-8")
        (tmp_path / "override.cfg").write_text("[autoload]\n", encoding="utf-8")
        got = godot.evidence(str(tmp_path), str(tmp_path / "out"))
        assert got["ok"] is False
        assert "refusing to clobber" in got["error"]

    def test_injection_is_removed_even_when_the_run_fails(self, tmp_path,
                                                          monkeypatch):
        """A leftover override.cfg silently changes how the user's project runs
        forever after. The cleanup must survive the failure path."""
        project = tmp_path / "proj"
        project.mkdir()
        (project / "project.godot").write_text("config_version=5\n",
                                               encoding="utf-8")

        def boom(cmd, **kwargs):
            raise godot.subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(godot.subprocess, "run", boom)
        monkeypatch.setattr(godot, "find_godot", lambda *a, **k: "godot.exe")

        got = godot.evidence(str(project), str(tmp_path / "out"), timeout=1)
        assert got["ok"] is False
        assert not (project / "override.cfg").exists()
        assert not (project / ".bgate_evidence.gd").exists()

    def test_gdscript_uses_tabs_not_spaces(self):
        """GDScript rejects mixed indentation. A space-indented line in the
        injected autoload is a parse error at capture time, in a file no editor
        ever opens — cheap to assert here, baffling to debug there."""
        for line in godot._EVIDENCE_GD.splitlines():
            if line.strip() and line[:1] == " ":
                pytest.fail(f"space-indented GDScript line: {line!r}")

    def test_capture_waits_for_frame_post_draw(self):
        """A bare get_image() races the renderer and intermittently returns the
        PREVIOUS frame. Pin the await so nobody 'simplifies' it away."""
        assert "await RenderingServer.frame_post_draw" in godot._EVIDENCE_GD

    def test_overlay_is_excluded_from_its_own_manifest(self):
        """The injected overlay must never be reported as game content."""
        assert "if node == _overlay or node == self:" in godot._EVIDENCE_GD

    def test_animated_sprites_are_measured(self):
        """AnimatedSprite2D has NO get_rect() in Godot 4 though Sprite2D does.

        Measured on haymaker: without an explicit branch the manifest came back
        with 27 entities and not one of them a fighter — a complete HUD map and
        no game. Regression guard for the asymmetry, not for the syntax.
        """
        assert "node is AnimatedSprite2D" in godot._EVIDENCE_GD
        assert "get_frame_texture" in godot._EVIDENCE_GD

    def test_hidden_subtrees_are_skipped_by_default(self):
        """A hidden Control was never laid out, so its bounds are pre-layout
        garbage. haymaker's closed F1 panel contributed ~90 labels all claiming
        identical bounds, outnumbering real content 30:1."""
        assert "not vis and not _include_hidden" in godot._EVIDENCE_GD


# --- live capture (needs the real engine + a display) ----------------------


HAYMAKER = r"C:\Users\adria\Desktop\haymaker\game"


@pytest.mark.skipif(not godot.available()["available"],
                    reason="Godot not installed")
class TestLiveCapture:
    def test_captures_manifest_from_a_real_project(self, tmp_path):
        import os
        if not os.path.exists(os.path.join(HAYMAKER, "project.godot")):
            pytest.skip("haymaker project not present")

        out = tmp_path / "ev"
        got = godot.evidence(HAYMAKER, str(out), at=1.5, timeout=180)
        assert got["ok"] is True, got
        assert got["counts"]["entities"] > 0, "walked the tree and found nothing"

        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["entities"].values():
            assert len(entry["screen_bounds"]) == 4
            assert isinstance(entry["visible"], bool)

    def test_both_fighters_survive_their_shared_node_name(self, tmp_path):
        """Both combatants' sprites are named `Sprite`. Keying the manifest by
        node.name alone would silently drop one — the collision path must give
        the second one its full path instead. Real case, not hypothetical."""
        import os
        if not os.path.exists(os.path.join(HAYMAKER, "project.godot")):
            pytest.skip("haymaker project not present")

        got = godot.evidence(HAYMAKER, str(tmp_path / "ev"), at=3.0,
                             scene="res://scenes/main.tscn", timeout=200)
        assert got["ok"] is True, got

        sprites = {k: v for k, v in got["entities"].items()
                   if v["class"] == "AnimatedSprite2D"}
        assert len(sprites) == 2, f"expected both fighters, got {list(sprites)}"
        # Distinct nodes, distinct bounds — not one entry written twice.
        bounds = [tuple(v["screen_bounds"]) for v in sprites.values()]
        assert bounds[0] != bounds[1]
        assert any("/" in k for k in sprites), "collision key should be a path"

    def test_reports_the_viewport_to_window_scale(self, tmp_path):
        """Bounds are in viewport space; the PNG is at window resolution. For
        Commodity Brawler that is a 640x360 stage rendered at 1280x720 — a 2x
        factor that silently breaks any overlay drawn without it."""
        import os
        if not os.path.exists(os.path.join(HAYMAKER, "project.godot")):
            pytest.skip("haymaker project not present")

        got = godot.evidence(HAYMAKER, str(tmp_path / "ev"), at=1.5, timeout=180)
        assert got["ok"] is True, got
        assert got["bounds_space"] == "viewport"
        assert got["window"] == [1280, 720]

        vp_w, vp_h = got["viewport"]
        sx, sy = got["scale"]
        assert sx == pytest.approx(1280 / vp_w)
        assert sy == pytest.approx(720 / vp_h)
