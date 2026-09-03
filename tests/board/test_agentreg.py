"""The machine-wide agent registry, and the pid-reuse case it exists for.

The interesting tests here are the ones that fake a STALE entry: a row left by
a run whose process is long dead, whose pid the OS has since handed to
something else. Everything the registry does about killing, listing and
reconciling hangs off telling that apart from a live agent, so it is tested
directly rather than inferred from a happy path.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time

import pytest

from bgate_core.board import agentreg, queue
from bgate_core.store import db, project

HAVE_PSUTIL = importlib.util.find_spec("psutil") is not None


@pytest.fixture()
def without_psutil(monkeypatch):
    """Run as if psutil were not installed - WHICH IT USUALLY IS NOT.

    psutil is not a declared dependency and must never become one, so the
    ctypes/`/proc` fallback is the path almost every real user takes. On a
    developer machine that happens to have psutil, every probe test would
    otherwise exercise the one code path users do not have.
    """
    monkeypatch.setattr(agentreg, "_psutil_probe", lambda pid: None)


def _dispatched_item(root, title: str = "make the thing") -> int:
    """A work item sitting in 'dispatched' - the state a dead agent strands."""
    item = queue.add(root, "art", title, "brief")
    assert queue.reserve(root, item["id"])
    return int(item["id"])


def _dead_pid() -> int:
    """A pid that certainly names nothing any more.

    A real process, run to completion, rather than an invented number: on
    Windows the Popen object holds the process handle open after exit, which is
    exactly the case where "the handle opens" must NOT be read as "it is alive".
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


# ---------------------------------------------------------------------------
# Probing a pid
# ---------------------------------------------------------------------------


def test_probe_reports_this_process_alive_with_a_plausible_start():
    exists, started = agentreg.probe(os.getpid())
    assert exists is True
    assert started is not None, "the test host must be able to time its own pid"
    # Somewhere between "started after the machine booted this year" and "not in
    # the future" - the point is that it is a real epoch stamp, not ticks or a
    # FILETIME that never got its 1601 offset subtracted.
    assert 0 < started <= time.time() + 5
    assert time.time() - started < 365 * 24 * 3600


def test_probe_is_stable_across_calls():
    """The start time is compared to a value recorded minutes or hours earlier,
    so two readings of the same process must not drift."""
    first = agentreg.process_start(os.getpid())
    second = agentreg.process_start(os.getpid())
    assert first is not None and second is not None
    assert abs(first - second) < agentreg.START_TOLERANCE_S


def test_probe_reports_an_exited_process_as_gone():
    exists, started = agentreg.probe(_dead_pid())
    assert exists is False
    assert started is None


def test_probe_without_psutil_answers_the_same_way(without_psutil):
    """The fallback is not a degraded mode - it has to give the same verdicts,
    because the pid-reuse check is only as good as the start time it gets."""
    exists, started = agentreg.probe(os.getpid())
    assert exists is True
    assert started is not None
    assert 0 < started <= time.time() + 5
    assert agentreg.probe(_dead_pid()) == (False, None)


@pytest.mark.skipif(not HAVE_PSUTIL, reason="psutil is not installed here")
def test_the_fallback_start_time_agrees_with_psutil(monkeypatch):
    """Cross-check against a known-good clock.

    The ctypes path converts a FILETIME (100ns ticks since 1601) into epoch
    seconds by hand, and an offset that is wrong by the 369 years between those
    two epochs still LOOKS like a plausible float. psutil is the second opinion
    that catches it.
    """
    known = agentreg.process_start(os.getpid())
    monkeypatch.setattr(agentreg, "_psutil_probe", lambda pid: None)
    ours = agentreg.process_start(os.getpid())
    assert ours is not None
    assert abs(ours - known) < agentreg.START_TOLERANCE_S


def test_a_recycled_pid_is_caught_without_psutil(root, without_psutil):
    item_id = _dispatched_item(root)
    agentreg.record(os.getpid(), item_id=item_id, root=str(root))
    _restamp(root, os.getpid(), agentreg.process_start(os.getpid()) - 10_000)

    assert agentreg.live() == []
    assert [f["item_id"] for f in agentreg.reconcile()["failed"]] == [item_id]


# ---------------------------------------------------------------------------
# record / forget / live
# ---------------------------------------------------------------------------


def test_record_writes_one_row_with_the_identity_fields(root):
    row_id = agentreg.record(os.getpid(), item_id=7, seat="art", root=str(root),
                             runner="claude", log=str(root / "a.log"))
    assert row_id is not None
    (data,) = agentreg.open_runs(str(root))
    assert data["pid"] == os.getpid()
    assert data["item_id"] == 7
    assert data["seat"] == "art"
    assert os.path.normcase(data["root"]) == os.path.normcase(str(root.resolve()))
    assert data["runner"] == "claude"
    assert data["started_at"] > 0
    assert data["ended_at"] is None and data["status"] == "running"
    # THE FIELD THE WHOLE MODULE TURNS ON.
    assert data["proc_started"] is not None


def test_live_sees_a_recorded_running_process(root):
    agentreg.record(os.getpid(), item_id=1, seat="art", root=str(root))
    assert [e["pid"] for e in agentreg.live()] == [os.getpid()]
    assert agentreg.stale() == []


def test_forget_closes_the_row(root):
    agentreg.record(os.getpid(), item_id=1, root=str(root))
    assert agentreg.forget(os.getpid()) is True
    assert agentreg.entries() == []
    assert agentreg.last_run(str(root), 1)["status"] == "gone"
    assert agentreg.forget(os.getpid()) is False


def test_an_exited_process_is_stale_not_live(root):
    pid = _dead_pid()
    agentreg.record(pid, item_id=1, root=str(root))
    assert agentreg.live() == []
    assert [e["pid"] for e in agentreg.stale()] == [pid]


# ---------------------------------------------------------------------------
# PID REUSE - the case this module is built around
# ---------------------------------------------------------------------------


def _restamp(root, pid: int, started) -> None:
    """Rewrite a row's recorded start time, to fake a stale row whose pid
    the OS has since handed to a different program."""
    with db.tx(root) as conn:
        conn.execute("UPDATE agent_runs SET proc_started = ? WHERE pid = ?",
                     (started, pid))


def test_a_recycled_pid_is_not_our_agent(root):
    """The entry names a LIVE pid, but not the process that was recorded.

    Without the start time this is indistinguishable from a running agent, and
    "stop it" would kill whatever now owns the number.
    """
    agentreg.record(os.getpid(), item_id=1, seat="art", root=str(root))
    _restamp(root, os.getpid(), agentreg.process_start(os.getpid()) - 10_000)
    assert agentreg.live() == []
    assert [e["pid"] for e in agentreg.stale()] == [os.getpid()]


def test_reconcile_fails_the_item_behind_a_recycled_pid(root):
    item_id = _dispatched_item(root)
    agentreg.record(os.getpid(), item_id=item_id, seat="art", root=str(root))
    _restamp(root, os.getpid(), agentreg.process_start(os.getpid()) - 10_000)

    out = agentreg.reconcile()

    assert [f["item_id"] for f in out["failed"]] == [item_id]
    assert queue.get(root, item_id)["status"] == "failed"
    assert agentreg.entries() == [], "a settled entry must not be settled twice"


def test_reconcile_leaves_a_genuinely_live_agent_alone(root):
    """The opposite mistake, and the more expensive one: failing an item out
    from under an agent that is still working."""
    item_id = _dispatched_item(root)
    agentreg.record(os.getpid(), item_id=item_id, seat="art", root=str(root))

    out = agentreg.reconcile()

    assert out["failed"] == []
    assert queue.get(root, item_id)["status"] == "dispatched"
    assert len(agentreg.entries()) == 1


def test_reconcile_never_overwrites_a_result_the_agent_reported(root):
    """A run that called queue_complete before dying owns its own record."""
    item_id = _dispatched_item(root)
    queue.complete(root, item_id, result="shipped the sprite sheet")
    agentreg.record(_dead_pid(), item_id=item_id, root=str(root))

    out = agentreg.reconcile()

    assert out["failed"] == []
    assert "shipped the sprite sheet" in queue.get(root, item_id)["result"]
    assert agentreg.entries() == [], "the dead entry is still cleared"


def test_reconcile_settles_a_dead_agents_item(root):
    item_id = _dispatched_item(root)
    agentreg.record(_dead_pid(), item_id=item_id, seat="art", root=str(root))

    out = agentreg.reconcile()

    assert [f["item_id"] for f in out["failed"]] == [item_id]
    item = queue.get(root, item_id)
    assert item["status"] == "failed"
    assert "never reported" in item["result"]


def test_reconcile_spans_projects(root, tmp_path):
    """The whole point of a machine-wide registry: one call settles agents
    spawned against projects this dashboard was never opened on."""
    import shutil

    other = tmp_path.parent / (tmp_path.name + "-other")
    shutil.copytree(root, other)
    here = _dispatched_item(root, "item in this project")
    there = _dispatched_item(other, "item in the other project")
    agentreg.record(_dead_pid(), item_id=here, root=str(root))
    agentreg.record(_dead_pid(), item_id=there, root=str(other))

    agentreg.reconcile()

    assert queue.get(root, here)["status"] == "failed"
    assert queue.get(other, there)["status"] == "failed"


def test_an_unknowable_process_is_neither_live_nor_settled(root, monkeypatch):
    """A host that will not describe a pid (access denied, most often) is the
    third answer, and it must not be rounded to either of the other two: listing
    it invites a kill, failing it kills a work item that may still be running."""
    item_id = _dispatched_item(root)
    agentreg.record(os.getpid(), item_id=item_id, root=str(root))
    monkeypatch.setattr(agentreg, "probe", lambda pid: (None, None))

    assert agentreg.live() == []
    assert agentreg.stale() == []
    assert agentreg.reconcile() == {"failed": [], "cleared": []}
    assert queue.get(root, item_id)["status"] == "dispatched"
    assert len(agentreg.entries()) == 1


def test_an_entry_with_no_recorded_start_time_is_not_claimed_as_ours(root):
    """Written by an older build, or on a host that would not say. The pid is
    alive, but nothing proves it is the same process - so it stays unknowable
    rather than being promoted on the strength of a recycled number."""
    item_id = _dispatched_item(root)
    agentreg.record(os.getpid(), item_id=item_id, root=str(root))
    _restamp(root, os.getpid(), None)

    assert agentreg.live() == []
    assert agentreg.stale() == []
    assert queue.get(root, item_id)["status"] == "dispatched"


def test_a_registered_project_with_no_database_is_skipped_not_raised(
        root, tmp_path):
    agentreg.record(os.getpid(), item_id=1, root=str(root))
    ghost = tmp_path / "ghost"
    ghost.mkdir()
    project.register(ghost, "ghost")
    assert [e["pid"] for e in agentreg.entries()] == [os.getpid()]


def test_a_finished_row_is_not_part_of_the_fleet(root):
    agentreg.record(os.getpid(), item_id=1, root=str(root))
    assert agentreg.finish(str(root), 1, os.getpid(), status="done",
                           result={"outcome": "done"}, cost_usd=0.25)
    assert agentreg.entries() == []
    run = agentreg.last_run(str(root), 1)
    assert run["status"] == "done" and run["cost_usd"] == 0.25
    assert run["result"] == {"outcome": "done"}
    # Finished once is finished: a second close does not rewrite the outcome.
    assert agentreg.finish(str(root), 1, os.getpid(), status="gone") is False
    assert agentreg.last_run(str(root), 1)["status"] == "done"
