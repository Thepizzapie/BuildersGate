"""Item 31c/34 — per-attempt cost history, and the attempt number on a
landing report.

31c: ``agent_runs`` already carries ``cost_usd`` per run; nothing surfaced it
per attempt, so a re-done item's card showed only the running total.
``agentreg.runs_for_item`` numbers every recorded run oldest-first —
deliberately NOT named ``attempts`` on the item dict, because that key is
already ``work_item.attempts``, the round COUNTER every existing reader
treats as an int.

34: a completion's activity line and event payload now say which attempt
this is (attempts is the count of PRIOR reopens, so the live one is
``attempts + 1``).
"""
from __future__ import annotations

from bgate_core.board import activity, agentreg, queue


def test_runs_for_item_numbers_oldest_first_with_cost(root):
    item = queue.add(root, "art", "paint the boss", "brief")
    r1 = agentreg.record(1001, item_id=item["id"], seat="art", root=root)
    agentreg.finish(root, item["id"], 1001, status="exited", cost_usd=0.42)
    r2 = agentreg.record(1002, item_id=item["id"], seat="art", root=root)
    agentreg.finish(root, item["id"], 1002, status="exited", cost_usd=1.10)
    assert r1 and r2

    runs = agentreg.runs_for_item(root, item["id"])
    assert [r["n"] for r in runs] == [1, 2]
    assert [round(r["cost_usd"], 2) for r in runs] == [0.42, 1.10]


def test_runs_for_item_is_empty_for_a_never_run_item(root):
    item = queue.add(root, "art", "paint the boss", "brief")
    assert agentreg.runs_for_item(root, item["id"]) == []


def test_completion_payload_carries_the_attempt_number(root):
    item = queue.add(root, "art", "paint the boss", "brief")
    assert queue.reserve(root, item["id"])
    payload = queue._item_event_payload(queue.get(root, item["id"]))
    assert payload["attempt"] == 1        # attempts is 0 until a reopen

    queue.complete(root, item["id"], failed=True, result="broke on the boss")
    queue.reopen(root, item["id"], "fix the boss collider")
    reopened = queue.get(root, item["id"])
    assert reopened["attempts"] == 1
    payload2 = queue._item_event_payload(reopened)
    assert payload2["attempt"] == 2


def test_landing_activity_line_names_the_attempt(root):
    item = queue.add(root, "art", "paint the boss", "brief")
    assert queue.reserve(root, item["id"])
    queue.complete(root, item["id"], failed=True, result="broke on the boss")
    lines = [str(a.get("message") or a.get("summary") or "")
             for a in activity.recent(root, limit=20)]
    assert any(f"#{item['id']}" in line or str(item["id"]) in line for line in lines)
    assert any("(attempt 1)" in line for line in lines)
