"""ITEM #20: the sheet-level and in-engine foot-drift gates must report the
SAME metric names, so a verdict caught by one is a verdict the other can be
asked to confirm without a translation step.

Pure-Python: no Godot needed. ``godot._animation_gate_findings`` takes the
manifest shape ``godot_evidence`` produces (``foot_drift_raw`` per entity)
directly, and ``spritekit._band_report`` is exercised through synthetic
cells built the same way tests/art's own suite builds them.
"""
from __future__ import annotations

from bgate_adapters import godot
from bgate_core.art import spritekit


def _cell(height: int, bottom_offset: int = 0):
    """A single opaque square cell, ``height`` tall, shifted down by
    ``bottom_offset`` px — enough for ``cell_stats`` to read a bottom edge."""
    from PIL import Image

    size = height + bottom_offset + 4
    im = Image.new("RGBA", (height, size), (0, 0, 0, 0))
    block = Image.new("RGBA", (height, height), (200, 40, 40, 255))
    im.paste(block, (0, bottom_offset))
    return im


class TestSharedVocabulary:
    def test_both_gates_use_the_same_finding_keys(self):
        # Sheet-level: three frames, one foot noticeably higher than the
        # other two — the drifting-foot shape spritekit's own suite uses.
        cells = [_cell(100, 0), _cell(100, 0), _cell(100, 20)]
        sheet = spritekit._band_report(cells, ["f0", "f1", "f2"])
        sheet_finding = next(
            f for f in sheet["findings"] if f["kind"] == "foot_drift")

        # In-engine: the equivalent raw shape godot_evidence would produce
        # for an AnimatedSprite2D whose feet drift the same amount.
        entities = {"fighter": {"foot_drift_raw": {
            "animation": "walk",
            "bottoms": [100.0, 100.0, 120.0],
            "heights": [100.0, 100.0, 100.0],
        }}}
        engine_findings = godot._animation_gate_findings(entities)
        engine_finding = next(
            f for f in engine_findings if f["kind"] == "foot_drift")

        assert sheet_finding["kind"] == engine_finding["kind"] == "foot_drift"
        # Same metric, same unit: normalized spread as a fraction of height.
        assert sheet_finding["value"] == engine_finding["value"]
        # Every key spritekit's row report promises is present on the engine
        # finding too — a downstream reader must not need to know which gate
        # produced the dict it is holding.
        for key in ("kind", "value"):
            assert key in sheet_finding and key in engine_finding

    def test_an_in_engine_pass_reports_ok_true(self):
        entities = {"fighter": {"foot_drift_raw": {
            "animation": "idle",
            "bottoms": [100.0, 100.1, 99.9],
            "heights": [100.0, 100.0, 100.0],
        }}}
        findings = godot._animation_gate_findings(entities)
        assert findings and findings[0]["ok"]
        assert findings[0]["threshold"] == spritekit.FOOT_DRIFT_MAX

    def test_entities_with_no_animation_data_are_skipped(self):
        entities = {"hud_bar": {"texture": "res://hp.png"}}
        assert godot._animation_gate_findings(entities) == []

    def test_a_single_frame_animation_cannot_drift(self):
        entities = {"statue": {"foot_drift_raw": {
            "animation": "idle", "bottoms": [50.0], "heights": [50.0]}}}
        assert godot._animation_gate_findings(entities) == []
