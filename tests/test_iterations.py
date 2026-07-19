from __future__ import annotations

import json

from bgate_core import artifacts, db, iterations


def test_iteration_captures_reproducible_build_inputs(root):
    game = root / "game"
    game.mkdir()
    (game / "project.godot").write_text("[application]\n", encoding="utf-8")
    scripts = game / "scripts"
    scripts.mkdir()
    (scripts / "player.gd").write_text(
        "@export var gravity: float = 1200.0\n"
        "@export_range(0, 1) var coyote_time = 0.12\n",
        encoding="utf-8")
    export = root / "export" / "web"
    export.mkdir(parents=True)
    (export / "index.pck").write_bytes(b"build-one")
    (root / ".bgate" / "test-results.json").write_text(
        json.dumps({"status": "passed", "checks": 14}), encoding="utf-8")

    image = root / "assets" / "hero.png"
    image.parent.mkdir()
    image.write_bytes(b"hero")
    revision = artifacts.register(root, "hero", image)
    artifacts.review(root, revision["id"], "approved")

    got = iterations.create(root, "Make the jump readable")

    assert got["goal"] == "Make the jump readable"
    assert got["source_fingerprint"]
    assert got["export_hash"]
    assert got["active_artifact_ids"] == [revision["id"]]
    assert got["tunables"]["game/scripts/player.gd"]["gravity"] == "1200.0"
    assert got["tests"]["status"] == "passed"
    assert got["telemetry_schema_version"] == 1
    assert got["events"][0]["stage"] == "snapshot"


def test_iteration_tracks_evidence_decisions_work_and_outcome(root):
    iteration = iterations.create(root, "Retune jump")
    with db.tx(root) as conn:
        session = conn.execute(
            "INSERT INTO playtest_session "
            "(name, slug, status, duration_s, iteration_id) "
            "VALUES ('jump', 'jump', 'ready', 20, ?)",
            (iteration["id"],)).lastrowid
        feedback = conn.execute(
            "INSERT INTO playtest_item "
            "(session_id, t, kind, text, seat, status) "
            "VALUES (?, 2, 'fix', 'jump is floaty', 'gameplay', 'promoted')",
            (session,)).lastrowid
        conn.execute(
            "INSERT INTO playtest_event (session_id, t, kind, data) "
            "VALUES (?, 2, 'jump', '{}')", (session,))

    iterations.add_event(
        root, iteration["id"], "decision", "feedback", str(feedback),
        "Promoted jump feedback")
    complete = iterations.complete_from_playtest(
        root, iteration["id"], session)

    assert complete["status"] == "complete"
    assert complete["outcome"]["feedback"] == 1
    assert complete["outcome"]["telemetry_events"] == 1
    assert [event["stage"] for event in complete["events"]] == [
        "snapshot", "decision", "outcome"]
