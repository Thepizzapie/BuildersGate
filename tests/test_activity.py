"""The activity ledger's prune — the one durable store that had no deleter.

events.prune shipped on day one and followup's tick calls it; the activity
table grew one row per lock, queue move and status line for the life of the
project. activity.prune mirrors events.prune's shape (window in days, returns
rows deleted, never raises) and rides the same PRUNE_EVERY tick in followup.
"""
from __future__ import annotations

import ast
from pathlib import Path

from bgate_core import activity, db


def _backdate(root, summary: str, days: int) -> None:
    with db.tx(root) as conn:
        conn.execute(
            "INSERT INTO activity (seat, kind, summary, ref, created_at) "
            "VALUES ('', 'test', ?, '', datetime('now', ?))",
            (summary, f"-{int(days)} days"))


def test_prune_deletes_only_rows_older_than_the_window(root):
    activity.log(root, "test", "fresh line")
    _backdate(root, "ancient line", days=120)
    _backdate(root, "recent-ish line", days=10)

    assert activity.prune(root, keep_days=90) == 1

    kept = [r["summary"] for r in activity.recent(root, limit=50)]
    assert "fresh line" in kept
    assert "recent-ish line" in kept
    assert "ancient line" not in kept


def test_prune_returns_zero_when_nothing_is_old(root):
    activity.log(root, "test", "today")
    assert activity.prune(root, keep_days=90) == 0
    assert [r["summary"] for r in activity.recent(root, limit=5)] == ["today"]


def test_prune_never_raises_on_a_missing_project(tmp_path):
    # The ledger never breaks the work: no project here, no exception.
    assert activity.prune(tmp_path / "nowhere") == 0


def test_followup_tick_is_wired_to_call_it():
    """The prune has a caller — without one it is dead code, which is exactly
    how the activity table went unbounded the first time. Asserted against the
    AST rather than by running the tick, which needs a live board."""
    source = (Path(__file__).resolve().parent.parent
              / "bgate_ui" / "followup.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "prune"
             and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "activity"]
    assert calls, "followup.py no longer calls activity.prune anywhere"
