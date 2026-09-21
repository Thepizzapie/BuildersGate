"""ITEM #19b: the same scripted drive, run a third time in one work item,
is refused rather than burning another five minutes of wall clock.
"""
from __future__ import annotations

from bgate_core.runtime import rundedup


def test_the_first_two_identical_runs_are_allowed(tmp_path):
    fp = rundedup.fingerprint(script="print(1)", project_dir=str(tmp_path))
    for _ in range(2):
        got = rundedup.check(tmp_path, item_id="item_5", fp=fp)
        assert got["ok"] and not got["refuse"]
        rundedup.record(tmp_path, item_id="item_5", fp=fp)


def test_the_third_identical_run_is_refused(tmp_path):
    fp = rundedup.fingerprint(script="print(1)", project_dir=str(tmp_path))
    for _ in range(2):
        rundedup.record(tmp_path, item_id="item_5", fp=fp)
    got = rundedup.check(tmp_path, item_id="item_5", fp=fp)
    assert not got["ok"]
    assert got["refuse"]
    assert got["count"] == 2


def test_force_overrides_the_cap(tmp_path):
    fp = rundedup.fingerprint(script="print(1)", project_dir=str(tmp_path))
    for _ in range(2):
        rundedup.record(tmp_path, item_id="item_5", fp=fp)
    got = rundedup.check(tmp_path, item_id="item_5", fp=fp, force=True)
    assert got["ok"]
    assert not got["refuse"]


def test_a_different_script_is_a_different_fingerprint(tmp_path):
    a = rundedup.fingerprint(script="print(1)", project_dir=str(tmp_path))
    b = rundedup.fingerprint(script="print(2)", project_dir=str(tmp_path))
    assert a != b


def test_a_different_work_item_is_not_capped_by_anothers_runs(tmp_path):
    fp = rundedup.fingerprint(script="print(1)", project_dir=str(tmp_path))
    for _ in range(3):
        rundedup.record(tmp_path, item_id="item_5", fp=fp)
    got = rundedup.check(tmp_path, item_id="item_9", fp=fp)
    assert got["ok"]
    assert got["count"] == 0


def test_no_work_item_is_never_capped(tmp_path):
    fp = rundedup.fingerprint(script="print(1)", project_dir=str(tmp_path))
    for _ in range(5):
        got = rundedup.check(tmp_path, item_id="", fp=fp)
        assert got["ok"]
        rundedup.record(tmp_path, item_id="", fp=fp)


def test_refusal_message_names_the_log_when_there_is_one(tmp_path):
    fp = rundedup.fingerprint(script="print(1)", project_dir=str(tmp_path))
    for _ in range(2):
        rundedup.record(tmp_path, item_id="item_5", fp=fp,
                        log_path=str(tmp_path / "run.log"))
    got = rundedup.check(tmp_path, item_id="item_5", fp=fp)
    msg = rundedup.refusal_message(got)
    assert "run.log" in msg
    assert "already ran this" in msg
