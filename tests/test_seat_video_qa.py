"""The two reads the Video and QA workspaces were drawing empty states over.

What is pinned here is the DIFFERENCE BETWEEN MEASURED AND ASSUMED, in both
directions:

  * cinecheck must never report "0 untranscoded" on a machine that cannot
    measure anything. A filename ending in .ogv is the exact evidence that
    failed this project before — a libtheora build that writes files Godot
    opens and cannot decode — so an absent ffprobe has to produce null, not a
    zero, and the panel has to be able to tell them apart.
  * enginetests must never report a green suite out of nothing run. `no_tests`
    is a distinct outcome from `ok`, and the history has to survive a corrupt
    line rather than losing every earlier run to one bad write.
"""
from __future__ import annotations

import json
from pathlib import Path

from bgate_core import cinecheck, enginetests


# ---------------------------------------------------------------------------
# Theora, measured
# ---------------------------------------------------------------------------
def test_a_missing_file_is_not_measured_and_says_why(tmp_path):
    got = cinecheck.probe(tmp_path / "nothing.ogv")
    assert got["exists"] is False
    assert got["demuxed"] is False
    assert "not on disk" in got["why"]


def test_no_ffprobe_means_unmeasured_not_a_pass(tmp_path, monkeypatch):
    """The whole point. No probe, no claim — and the reason is on the row."""
    monkeypatch.setattr(cinecheck, "ffprobe", lambda: None)
    f = tmp_path / "cut.ogv"
    f.write_bytes(b"not really an ogg")
    got = cinecheck.probe(f)
    assert got["measured"] is False
    assert got["demuxed"] is False
    assert "ffprobe" in got["why"]


def test_an_ogv_suffix_over_garbage_bytes_is_not_theora(tmp_path, monkeypatch):
    """A file that exists, ends in .ogv, and does not demux is the bug."""
    if not cinecheck.ffprobe():
        import pytest
        pytest.skip("this assertion is about what ffprobe says; there is none")
    f = tmp_path / "broken.ogv"
    f.write_bytes(b"\x00" * 2048)
    got = cinecheck.probe(f)
    assert got["measured"] is True
    assert got["demuxed"] is False
    assert got["video_codec"] != "theora"


def test_survey_reports_null_untranscoded_when_it_cannot_measure(tmp_path,
                                                                 monkeypatch):
    monkeypatch.setattr(cinecheck, "ffprobe", lambda: None)
    monkeypatch.setattr(cinecheck._cine, "kept", lambda *a, **k: [])
    got = cinecheck.survey(tmp_path)
    assert got["probe"] is False
    assert got["untranscoded"] is None
    assert "ffprobe" in got["why"]


def test_survey_counts_only_what_it_actually_measured(tmp_path, monkeypatch):
    """One kept take, installed, real bytes — and the count follows the probe."""
    monkeypatch.setattr(cinecheck, "ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(cinecheck, "probe", lambda p, **k: {
        "path": str(p), "exists": True, "bytes": 10, "demuxed": True,
        "container": "ogg", "video_codec": "theora", "audio_codec": "vorbis",
        "duration_s": 3.0, "measured": True, "why": ""})
    monkeypatch.setattr(cinecheck._cine, "kept", lambda *a, **k: [
        {"artifact_id": 7, "logical_name": "intro", "sequence": "intro",
         "kind": "cutscene", "installed": True, "install_stale": False,
         "godot_res": "res://x.ogv", "installed_path": "game/x.ogv",
         "path": "out/x.ogv"}])
    got = cinecheck.survey(tmp_path)
    assert got["untranscoded"] == 0
    assert got["measured"] == 1
    assert got["rows"][0]["theora"] is True


# ---------------------------------------------------------------------------
# Watched — the seat's own gate, which had no record behind it
# ---------------------------------------------------------------------------
def test_nobody_has_watched_it_until_somebody_has(tmp_path):
    assert cinecheck.watched(tmp_path) == {}
    entry = cinecheck.mark_watched(tmp_path, 7, actor="human")
    assert entry["by"] == "human"
    assert cinecheck.watched(tmp_path)["7"]["at"] == entry["at"]


def test_the_watch_log_survives_being_garbage(tmp_path):
    d = tmp_path / ".bgate"
    d.mkdir()
    (d / cinecheck.WATCH_FILE).write_text("{not json", encoding="utf-8")
    assert cinecheck.watched(tmp_path) == {}


def test_survey_marks_a_watched_cut_and_counts_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(cinecheck, "ffprobe", lambda: None)
    monkeypatch.setattr(cinecheck._cine, "kept", lambda *a, **k: [
        {"artifact_id": 1, "logical_name": "a", "kind": "cutscene",
         "installed_path": "", "path": ""},
        {"artifact_id": 2, "logical_name": "b", "kind": "cutscene",
         "installed_path": "", "path": ""}])
    assert cinecheck.survey(tmp_path)["unwatched"] == 2
    cinecheck.mark_watched(tmp_path, 2)
    assert cinecheck.survey(tmp_path)["unwatched"] == 1


# ---------------------------------------------------------------------------
# Engine test history
# ---------------------------------------------------------------------------
def test_a_project_with_no_godot_project_says_so_rather_than_passing(tmp_path):
    got = enginetests.discover(tmp_path)
    assert got["scripts"] == []
    assert "project.godot" in got["why"]


def test_an_empty_tests_dir_is_named_as_the_problem(tmp_path):
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    got = enginetests.discover(tmp_path)
    assert got["scripts"] == []
    assert "green" in got["why"]


def test_discover_finds_the_scripts_that_exist(tmp_path):
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "b_test.gd").write_text("x", encoding="utf-8")
    (tmp_path / "tests" / "a_test.gd").write_text("x", encoding="utf-8")
    assert enginetests.discover(tmp_path)["scripts"] == ["a_test.gd", "b_test.gd"]


def test_no_tests_is_recorded_as_a_run_and_is_not_ok(tmp_path):
    """A gate that tested nothing has to leave a mark saying it tested nothing."""
    (tmp_path / "project.godot").write_text("[application]\n", encoding="utf-8")
    got = enginetests.run(tmp_path)
    assert got["ok"] is False and got["no_tests"] is True
    hist = enginetests.history(tmp_path)
    assert len(hist) == 1 and hist[0]["no_tests"] is True


def test_history_is_newest_first_and_skips_a_corrupt_line(tmp_path):
    log = tmp_path / ".bgate" / enginetests.LOG_FILE
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps({"at": "1", "ok": True}) + "\n"
        + "{ not json\n"
        + json.dumps({"at": "2", "ok": False}) + "\n", encoding="utf-8")
    rows = enginetests.history(tmp_path)
    assert [r["at"] for r in rows] == ["2", "1"]


def test_record_keeps_the_verdict_and_drops_the_engine_chatter(tmp_path):
    enginetests.record(tmp_path, {
        "ok": False, "scripts_run": 2, "scripts_failed": 1, "passed": 5,
        "failures": 1,
        "scripts": [{"script": "a.gd", "ok": True, "passed": 5, "failed": 0,
                     "output": "x" * 100000}]})
    row = enginetests.history(tmp_path)[0]
    assert row["scripts_failed"] == 1
    assert "output" not in row["scripts"][0]
    assert Path(tmp_path / ".bgate" / enginetests.LOG_FILE).is_file()
