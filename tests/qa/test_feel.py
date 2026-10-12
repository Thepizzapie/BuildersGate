"""The feel check: two bot runs in, verdicts out, the gate reads them.

Each verdict gets a run that should earn it AND a control that must not, so
none of these would pass with its rule deleted.
"""
from __future__ import annotations

import json
from pathlib import Path

from bgate_core.qa import feel


def _run(tmp: Path, name: str, decisions: list[dict], events=()) -> Path:
    d = tmp / name
    d.mkdir()
    (d / "decisions.jsonl").write_text(
        "\n".join(json.dumps(r) for r in decisions), encoding="utf-8")
    (d / "telemetry.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return d


def _decisions(choices, progress_to=1.0, danger_every=2, t0=1000.0):
    n = len(choices)
    return [{"choice": c, "ts": t0 + i, "progress": progress_to * (i + 1) / n,
             "danger": i % danger_every == 0} for i, c in enumerate(choices)]


def test_a_varied_pressured_run_against_a_weak_baseline_passes(tmp_path):
    smart = feel.measure(_run(tmp_path, "s", _decisions(
        ["shoot", "jump", "dash", "shoot", "jump", "move"] * 10),
        events=[{"kind": "player_death", "ts": 1010.5}]))
    base = feel.measure(_run(tmp_path, "b", _decisions(["shoot"] * 60,
                                                       progress_to=0.3)))
    got = feel.verdict(smart, base)
    assert got["verdict"] == "PASS", got


def test_one_button_and_flat(tmp_path):
    smart = feel.measure(_run(tmp_path, "s", _decisions(
        ["shoot"] * 50 + ["jump"] * 10),
        events=[{"kind": "fight_death", "ts": 1005}]))
    base = feel.measure(_run(tmp_path, "b", _decisions(["shoot"] * 60,
                                                       progress_to=0.95)))
    fails = feel.verdict(smart, base)["fails"]
    assert "ONE_BUTTON" in fails and "FLAT" in fails


def test_no_pressure_and_trivial(tmp_path):
    smart = feel.measure(_run(tmp_path, "s", _decisions(
        ["shoot", "jump", "dash"] * 20, danger_every=50)))
    base = feel.measure(_run(tmp_path, "b", _decisions(["shoot"] * 60,
                                                       progress_to=0.2,
                                                       danger_every=50)))
    fails = feel.verdict(smart, base)["fails"]
    assert "NO_PRESSURE" in fails and "TRIVIAL" in fails
    # control: the same smart run, but the passive bot is threatened half the
    # time - a good player avoiding danger is not a pressureless scene
    pressured = feel.measure(_run(tmp_path, "b2", _decisions(
        ["shoot"] * 60, progress_to=0.2, danger_every=2)))
    assert "NO_PRESSURE" not in feel.verdict(smart, pressured)["fails"]


def test_unfair_needs_deaths_on_one_spot(tmp_path):
    rows = _decisions(["shoot", "jump", "dash"] * 20)
    # three deaths right after decision 30 (progress 0.5) - one spot
    events = [{"kind": "fight_death", "ts": 1030.2 + k * 0.01} for k in range(3)]
    smart = feel.measure(_run(tmp_path, "s", rows, events))
    base = feel.measure(_run(tmp_path, "b", _decisions(["shoot"] * 60,
                                                       progress_to=0.2)))
    assert "UNFAIR" in feel.verdict(smart, base)["fails"]
    # control: the same three deaths spread over the scene are not unfair
    spread = [{"kind": "fight_death", "ts": 1005.2},
              {"kind": "fight_death", "ts": 1030.2},
              {"kind": "fight_death", "ts": 1055.2}]
    smart2 = feel.measure(_run(tmp_path, "s2", rows, spread))
    assert "UNFAIR" not in feel.verdict(smart2, base)["fails"]


def test_unmeasured_when_the_observer_reports_nothing(tmp_path):
    rows = [{"choice": c, "ts": 1000 + i} for i, c in
            enumerate(["shoot", "jump", "dash"] * 10)]
    smart = feel.measure(_run(tmp_path, "s", rows))
    base = feel.measure(_run(tmp_path, "b", rows))
    got = feel.verdict(smart, base)
    assert got["verdict"] == "FAIL" and "UNMEASURED" in got["fails"]


def _godot(root: Path) -> Path:
    (root / "project.godot").write_text("[application]\n", encoding="utf-8")
    return root


def test_gate_blocks_until_a_scene_passes(tmp_path):
    root = _godot(tmp_path)
    assert feel.blockers(root) and "no scene has passed" in feel.blockers(root)[0]
    d = feel.feel_dir(root)
    d.mkdir(parents=True)
    (d / "arena.json").write_text(json.dumps(
        {"scene": "arena", "verdict": "FAIL", "fails": {"FLAT": "x"}}))
    assert "FAILED on arena" in feel.blockers(root)[0]
    (d / "arena.json").write_text(json.dumps(
        {"scene": "arena", "verdict": "PASS", "fails": {}}))
    assert feel.blockers(root) == []


def test_gate_is_silent_where_no_bot_can_play(tmp_path):
    assert feel.blockers(tmp_path) == []


def test_progress_stuck_at_zero_is_unmeasured_not_trivial_flat(tmp_path):
    rows = [{"choice": c, "ts": 1000 + i, "progress": 0.0, "danger": i % 2 == 0}
            for i, c in enumerate(["shoot", "jump", "dash"] * 30)]
    smart = feel.measure(_run(tmp_path, "s", rows))
    base = feel.measure(_run(tmp_path, "b", rows))
    fails = feel.verdict(smart, base)["fails"]
    assert "UNMEASURED" in fails and "FLAT" not in fails
