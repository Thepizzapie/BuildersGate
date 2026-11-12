"""The feel check: two bot runs in, verdicts out, the gate reads them.

Each verdict gets a run that should earn it AND a control that must not, so
none of these would pass with its rule deleted.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

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
        ["shoot", "jump", "dash"] * 20, danger_every=50),
        events=[{"kind": "run_end", "ts": 1061, "data": {"status": "won"}}]))
    base = feel.measure(_run(tmp_path, "b", _decisions(["shoot"] * 60,
                                                       progress_to=0.2,
                                                       danger_every=50),
        events=[{"kind": "run_end", "ts": 1061, "data": {"status": "won"}}]))
    fails = feel.verdict(smart, base)["fails"]
    assert "NO_PRESSURE" in fails and "TRIVIAL" in fails
    # control: the same smart run, but the passive bot is threatened half the
    # time - a good player avoiding danger is not a pressureless scene
    pressured = feel.measure(_run(tmp_path, "b2", _decisions(
        ["shoot"] * 60, progress_to=0.2, danger_every=2)))
    assert "NO_PRESSURE" not in feel.verdict(smart, pressured)["fails"]


def test_trivial_is_a_note_not_a_fail_when_no_run_reached_an_end(tmp_path):
    """Measured (Turf War): 90 s is 3-4 turns of a 6-10 minute match. Nobody
    can die in that window, and the FAIL it produced filed tuning work."""
    smart = feel.measure(_run(tmp_path, "s", _decisions(
        ["shoot", "jump", "dash"] * 20)))
    base = feel.measure(_run(tmp_path, "b", _decisions(["shoot"] * 60,
                                                       progress_to=0.2)))
    assert smart["ended"] is False and smart["deaths"] == 0
    got = feel.verdict(smart, base, seconds=90)
    assert got["verdict"] == "PASS", got
    assert "TRIVIAL" not in got["fails"]
    assert "not measurable" in got["notes"]["TRIVIAL"] and "90 s" in got["notes"]["TRIVIAL"]
    # control: the same runs, BOTH carrying a match_end the bot survived, ARE
    # trivial; one run ending while the other did not is still unjudged
    end = [{"kind": "match_end", "ts": 1061, "data": {"status": "won"}}]
    ended = feel.measure(_run(tmp_path, "s2", _decisions(
        ["shoot", "jump", "dash"] * 20), events=end))
    assert ended["ended"] is True and ended["end_status"] == "won"
    assert "TRIVIAL" not in feel.verdict(ended, base)["fails"]
    base_ended = feel.measure(_run(tmp_path, "b2", _decisions(
        ["shoot"] * 60, progress_to=0.2), events=end))
    assert "TRIVIAL" in feel.verdict(ended, base_ended)["fails"]


def test_an_end_event_without_a_status_leaves_trivial_unjudged(tmp_path):
    """Measured (Turf War, 480 s): `match_end {turn: 23, winner: 1}`, the CPU
    had knocked out every athlete, and the check read 0 deaths and TRIVIAL."""
    winner_only = [{"kind": "match_end", "ts": 1061,
                    "data": {"turn": 23, "winner": 1}}]
    smart = feel.measure(_run(tmp_path, "s", _decisions(
        ["shoot", "jump", "dash"] * 20), events=winner_only))
    base = feel.measure(_run(tmp_path, "b", _decisions(
        ["shoot"] * 60, progress_to=0.2), events=winner_only))
    assert smart["ended"] is True and smart["end_status"] == "unknown"
    got = feel.verdict(smart, base, seconds=480)
    assert "TRIVIAL" not in got["fails"]
    assert "no status" in got["notes"]["TRIVIAL"]
    # controls: a lost status is a death (no TRIVIAL question at all), and a
    # knocked-out event counts as one too, status or no status
    lost = feel.measure(_run(tmp_path, "l", _decisions(["shoot"] * 60), events=[
        {"kind": "match_end", "ts": 1061, "data": {"winner": 1, "status": "lost"}}]))
    assert lost["deaths"] == 1 and lost["end_status"] == "lost"
    ko = feel.measure(_run(tmp_path, "k", _decisions(["shoot"] * 60), events=[
        {"kind": "athlete_knocked_out", "ts": 1030, "data": {"reason": "pit"}},
        *winner_only]))
    assert ko["deaths"] == 1


def test_one_button_counts_only_frames_that_offered_a_choice(tmp_path):
    """Measured (Turf War): "done" was the only option in retreat/resolve
    frames, and counting those made a turn-based game read as one-button."""
    forced = [{"choice": "done", "ts": 1000 + i, "progress": i / 80,
               "danger": True, "options": ["done"]} for i in range(50)]
    real = [{"choice": c, "ts": 1050 + i, "progress": (50 + i) / 80,
             "danger": i % 2 == 0, "options": ["dash", "jump", "shoot"]}
            for i, c in enumerate(["shoot", "jump", "dash"] * 10)]
    smart = feel.measure(_run(tmp_path, "s", forced + real,
                              events=[{"kind": "fight_death", "ts": 1070}]))
    assert smart["decisions"] == 80 and smart["forced"] == 50
    assert smart["decisions_with_choice"] == 30
    assert smart["top_share"] == round(10 / 30, 3)
    base = feel.measure(_run(tmp_path, "b", _decisions(["shoot"] * 60,
                                                       progress_to=0.2)))
    assert "ONE_BUTTON" not in feel.verdict(smart, base)["fails"]
    # control: the same rows without the options field count every frame
    stripped = [{k: v for k, v in r.items() if k != "options"} for r in forced + real]
    old = feel.measure(_run(tmp_path, "s2", stripped,
                            events=[{"kind": "fight_death", "ts": 1070}]))
    assert old["top_share"] == round(50 / 80, 3)
    assert "ONE_BUTTON" in feel.verdict(old, base)["fails"]


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
    # THE DESK IS A PERSON'S (Turf War, 2026-10-17): held from every
    # auto-dispatcher, not a repair, and the brief proposes rather than files.
    assert item["source"] in queue.HELD_SOURCES
    assert item["kind"] != "fix"
    assert "A PERSON files them" in item["brief"]
    assert "Do not file gameplay or level items" in item["brief"]
    assert "file each as kind=feature or fix" not in item["brief"]
    assert got["filed"] not in {int(r["id"]) for r in queue.ready(root, limit=50)}
    assert queue.claim_next(root, "director", "agent:item-999") is None
    # the belt to the braces: work filed FROM the feel item is a feature
    # unless it names a plan row, whatever its filer called it
    child = queue.add(root, "gameplay", "triple the damage", brief="x",
                      source="seat:director", source_ref=str(got["filed"]),
                      kind="fix")
    assert child["kind"] == "feature"
    planned = queue.add(root, "level", "cut the arena", brief="x",
                        source="seat:director", source_ref=str(got["filed"]),
                        kind="fix", planned=True)
    assert planned["kind"] == "fix"
    other = queue.add(root, "qa", "re-pin the golden hash", brief="x",
                      source="seat:director", source_ref="12345", kind="fix")
    assert other["kind"] == "fix"                 # control: no feel parent
    # a second failing run of the same scene files nothing new
    assert feel.escalate(root, _failing()) == {"open": got["filed"]}
    passed = {**_failing(), "verdict": "PASS", "fails": {}}
    assert feel.escalate(root, passed) == {"closed": [got["filed"]]}
    assert queue.get(root, got["filed"])["status"] == "done"


def test_polish_and_content_wait_while_a_scene_fails(root):
    from bgate_core.board import queue

    polish = queue.add(root, "audio", "shinier boss theme", brief="x",
                       kind="polish")
    feature = queue.add(root, "tech", "save slots", brief="x", kind="feature")
    d = feel.feel_dir(root)
    d.mkdir(parents=True)
    (d / "a.json").write_text(json.dumps(_failing()))
    ready_ids = {int(r["id"]) for r in queue.ready(root, limit=50)}
    assert polish["id"] not in ready_ids and feature["id"] in ready_ids
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


def test_a_fail_halts_art_level_and_gameplay_whatever_their_kind(root):
    """B3 (cat-cafe, 2026-10-25): the hold read kind alone, so items filed
    kind '' or fix built two days of content on a FEEL FAIL."""
    from bgate_core.board import queue

    filed = feel.escalate(root, _failing())["filed"]
    feel.feel_dir(root).mkdir(parents=True)
    (feel.feel_dir(root) / "scenes_a_tscn.json").write_text(
        json.dumps(_failing()))
    held = [queue.add(root, "art", "keeper sprite", brief="x"),
            queue.add(root, "level", "route the alley", brief="x", kind="fix"),
            queue.add(root, "gameplay", "noise meter", brief="x",
                      kind="feature")]
    free = [queue.add(root, "qa", "smoke the build", brief="x"),
            queue.add(root, "director", "rule on the pillars", brief="x"),
            # the work a person files from the FEEL FAIL item is how it ends,
            # and so is that work's own follow-up
            queue.add(root, "gameplay", "hiding costs noise", brief="x",
                      source="human", source_ref=str(filed))]
    free.append(queue.add(root, "level", "re-route past the bins", brief="x",
                          source="seat:gameplay",
                          source_ref=str(free[-1]["id"])))
    ready_ids = {int(r["id"]) for r in queue.ready(root, limit=50)}
    assert not {h["id"] for h in held} & ready_ids
    assert {f["id"] for f in free} <= ready_ids
    assert queue.claim_next(root, "art", "agent:item-999") is None
    assert "art, level and gameplay wait" in queue.slice_hold_reason(root,
                                                                     held[0])
    # a person clears it: the scene is waived on record, its item closes,
    # and the held seats dispatch again
    with pytest.raises(ValueError):
        feel.clear(root, "scenes/a.tscn", by="human", reason=" ")
    # a scene that is not failing is refused by name: a typo used to write a
    # waiver-only verdict file, leave the hold on and answer ok
    for wrong in ("", "scenes/b.tscn"):
        with pytest.raises(ValueError, match="failing: scenes/a.tscn"):
            feel.clear(root, wrong, by="human", reason="looks fine")
    assert sorted(p.name for p in feel.feel_dir(root).iterdir()) == [
        "scenes_a_tscn.json"]
    assert held[0]["id"] not in {int(r["id"]) for r in queue.ready(root, limit=50)}
    # the board's detail panel gets the scene and the sentence to clear it by
    from bgate_core.board import lifecycle
    node = next(n for n in lifecycle.graph(root)["nodes"] if n["id"] == filed)
    assert node["feel"]["scene"] == "scenes_a_tscn"
    assert node["feel"]["summary"].startswith("The feel check failed on")
    assert all(n["feel"] is None for n in lifecycle.graph(root)["nodes"]
               if n["id"] == held[0]["id"])
    got = feel.clear(root, "scenes/a.tscn", by="human", reason="the bot "
                     "cannot see the bins; I played it and it holds up")
    assert got["closed"] == [filed]
    assert queue.get(root, filed)["status"] == "done"
    ready_ids = {int(r["id"]) for r in queue.ready(root, limit=50)}
    assert {h["id"] for h in held} <= ready_ids
    assert queue.slice_hold_reason(root, held[0]) == ""


def test_the_person_reads_one_sentence_and_the_changes_first(root):
    from bgate_core.board import queue

    got = feel.escalate(root, _failing(fails={
        "FLAT": "hold-shoot matched", "NO_PRESSURE": "threatened 2%"}))
    assert got["summary"] == (
        "The feel check failed on scenes/a.tscn: holding one input plays as "
        "well as deciding; nothing threatens a player who stands still. Art, "
        "level and gameplay work is paused until it passes or you clear it.")
    head = queue.get(root, got["filed"])["brief"].split("---")[0]
    assert head.startswith(got["summary"])
    assert feel.REMEDY["FLAT"] in head and feel.REMEDY["NO_PRESSURE"] in head
    assert "Numbers:" not in head                 # the wall sits below the line


def test_only_a_person_can_clear_a_failing_scene(root, monkeypatch):
    from fastapi.testclient import TestClient

    from bgate_core.board import queue
    from bgate_ui.app import app

    monkeypatch.setenv("BGATE_ROOT", str(root))
    filed = feel.escalate(root, _failing())["filed"]
    feel.feel_dir(root).mkdir(parents=True)
    (feel.feel_dir(root) / "scenes_a_tscn.json").write_text(
        json.dumps(_failing()))
    client = TestClient(app)
    body = {"scene": "scenes/a.tscn", "reason": "played it, it holds up"}
    monkeypatch.setenv("BGATE_WORK_ITEM", "41")
    got = client.post("/api/feel/clear", json=body)
    assert got.status_code == 403
    assert queue.get(root, filed)["status"] == "queued"
    monkeypatch.delenv("BGATE_WORK_ITEM")
    got = client.post("/api/feel/clear", json={**body, "scene": "nope"})
    assert got.status_code == 400 and "nope" in got.text
    got = client.post("/api/feel/clear", json=body)
    assert got.status_code == 200, got.text
    assert got.json()["data"]["closed"] == [filed]
