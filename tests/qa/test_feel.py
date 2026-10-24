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


# --- the board ---------------------------------------------------------------

def _failing(scene="scenes/a.tscn", fails=None):
    return {"scene": scene, "verdict": "FAIL", "at": "now",
            "fails": fails or {"FLAT": "hold-shoot matched the smart bot"},
            "smart": {"decisions": 900, "top_choice": "shoot",
                      "progress_max": 0.5},
            "baseline": {"progress_max": 0.48, "danger_share": 0.1},
            "runs": {}}


def test_a_failure_files_one_director_item_and_a_pass_closes_it(root):
    from bgate_core.board import queue

    got = feel.escalate(root, _failing())
    item = queue.get(root, got["filed"])
    assert item["seat"] == "director" and item["source"] == feel.FEEL_SOURCE
    assert "FLAT" in item["title"] and "make the hold-one-input" in item["brief"]
    # a second failing run of the same scene files nothing new
    assert feel.escalate(root, _failing()) == {"open": got["filed"]}
    passed = {**_failing(), "verdict": "PASS", "fails": {}}
    assert feel.escalate(root, passed) == {"closed": [got["filed"]]}
    assert queue.get(root, got["filed"])["status"] == "done"


def test_polish_and_content_wait_while_a_scene_fails(root):
    from bgate_core.board import queue

    polish = queue.add(root, "art", "shinier boss", brief="x", kind="polish")
    fix = queue.add(root, "gameplay", "give firing a heat cost", brief="x",
                    kind="feature")
    d = feel.feel_dir(root)
    d.mkdir(parents=True)
    (d / "a.json").write_text(json.dumps(_failing()))
    ready_ids = {int(r["id"]) for r in queue.ready(root, limit=50)}
    assert polish["id"] not in ready_ids and fix["id"] in ready_ids
    assert "feel check is failing" in queue.slice_hold_reason(root, polish)
    # control: once the scene passes, the polish is free again
    (d / "a.json").write_text(json.dumps({**_failing(), "verdict": "PASS"}))
    assert polish["id"] in {int(r["id"]) for r in queue.ready(root, limit=50)}


def test_a_run_end_with_a_losing_status_is_a_death(tmp_path):
    rows = _decisions(["accept", "reject", "scroll"] * 20)
    lost = feel.measure(_run(tmp_path, "s", rows, [
        {"kind": "run_end", "ts": 1030.5, "data": {"status": "fired"}}]))
    won = feel.measure(_run(tmp_path, "w", rows, [
        {"kind": "run_end", "ts": 1030.5, "data": {"status": "shipped"}}]))
    assert lost["deaths"] == 1 and won["deaths"] == 0


def test_every_spelling_of_the_boot_page_is_one_scene():
    assert {feel._slug(x) for x in ("", "/", "index.html", "/index.html")} == {"boot"}
    assert feel._slug("scenes/levels/arena.tscn") == "scenes_levels_arena_tscn"


def test_a_run_whose_code_changed_mid_run_files_nothing(tmp_path, monkeypatch):
    run_dir = _run(tmp_path, "s", _decisions(["shoot"] * 60))
    monkeypatch.setattr(feel.agentplay, "run",
                        lambda root, **k: {"dir": str(run_dir), "usd": 0.0})
    versions = iter(["abc", "def"])
    monkeypatch.setattr(feel, "_code_version", lambda root: next(versions))
    filed = []
    monkeypatch.setattr(feel, "escalate", lambda root, out: filed.append(out))
    got = feel.check(tmp_path, scene="", goal="x", seconds=1)
    assert got["stale"] is True and "skipped" in got["board"] and not filed


def test_being_caught_is_a_death(tmp_path):
    """A stealth game ends a run with a catch, not a death event."""
    rows = _decisions(["sneak", "hide", "go"] * 20)
    caught = feel.measure(_run(tmp_path, "c", rows, [
        {"kind": "sam_catch", "ts": 1030.5, "data": {"by": "sight"}}]))
    assert caught["deaths"] == 1
