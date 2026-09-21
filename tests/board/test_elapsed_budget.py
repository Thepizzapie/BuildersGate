"""EXIT 67 gripe 40c — elapsed vs its own ceiling, on the running row.

Before this a running item's card said only "running"; the one question worth
asking mid-run ("is it near its ceiling, or nowhere close?") needed opening
the item and subtracting timestamps by hand. ``runlimits.elapsed_s`` reads
the OPEN ``agent_runs`` row (durable across a dashboard restart); the digest
and the queue-list decoration both thread it next to
``runlimits.runtime_ceiling``.
"""
from __future__ import annotations

import time

from bgate_core.board import agentreg, queue, runlimits
from bgate_core.design import gameplan


def _running_item(root, max_runtime_s=None):
    item = queue.add(root, "art", "paint the boss", "brief",
                     max_runtime_s=max_runtime_s)
    assert queue.reserve(root, item["id"])
    return item


def test_elapsed_s_is_none_with_no_open_run(root):
    item = _running_item(root)
    assert runlimits.elapsed_s(root, item["id"]) is None


def test_elapsed_s_reads_the_open_run_started_at(root):
    item = _running_item(root)
    started = time.time() - 90
    row_id = agentreg.record(4242, item_id=item["id"], seat="art", root=root)
    assert row_id
    # record() stamps started_at = time.time() at call time; backdate it so
    # the assertion does not depend on wall-clock slack in the test itself.
    from bgate_core.store import db
    with db.tx(root) as conn:
        conn.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?",
                     (started, row_id))
    elapsed = runlimits.elapsed_s(root, item["id"])
    assert elapsed is not None and 85 <= elapsed <= 100


def test_elapsed_s_is_none_once_the_run_is_closed(root):
    item = _running_item(root)
    agentreg.record(4243, item_id=item["id"], seat="art", root=root)
    agentreg.finish(root, item["id"], 4243, status="exited")
    assert runlimits.elapsed_s(root, item["id"]) is None


def test_digest_running_rows_carry_elapsed_and_ceiling(root):
    item = _running_item(root, max_runtime_s=1800)
    agentreg.record(4244, item_id=item["id"], seat="art", root=root)
    out = gameplan.digest(root, hours=12)
    rows = [r for r in out["running"] if r["id"] == item["id"]]
    assert rows, "the running item must appear in digest()['running']"
    row = rows[0]
    assert row["ceiling_s"] == 1800
    assert row["elapsed_s"] is not None and row["elapsed_s"] >= 0
