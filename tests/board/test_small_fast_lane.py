"""EXIT 67 gripe 40c/item D — the small fast lane skips the QA agent.

Under 'agent' gate mode every maker-seat completion buys a reviewer, at the
same price for a one-line copy fix and a boss fight. An item marked
``size='small'`` whose OWN result names a passing check (a ``Verify:`` line,
or a clean pytest summary) already did its own verifying — spending a second
agent to re-confirm it is the waste this lane exists to cut. Everything else
still gets the ordinary gate: this narrows ``wants_qa_agent``, never widens
it.
"""
from __future__ import annotations

from bgate_core.board import gates, queue


def _agent_mode(root):
    gates.set_mode(root, "agent")


def test_small_and_verified_skips_the_reviewer(root):
    _agent_mode(root)
    item = queue.add(root, "art", "fix typo in sign", "brief", size="small")
    item = queue.complete(
        root, item["id"],
        result="Verify: reopened the scene in Godot, the sign now reads "
               "'SALOON' — checked by eye against the concept ref.")
    assert gates.wants_qa_agent_for(root, item) is False


def test_small_but_unverified_still_gets_reviewed(root):
    _agent_mode(root)
    item = queue.add(root, "art", "fix typo in sign", "brief", size="small")
    item = queue.complete(root, item["id"], result="done, looks good")
    assert gates.wants_qa_agent_for(root, item) is True


def test_medium_item_is_never_fast_laned_even_when_verified(root):
    _agent_mode(root)
    item = queue.add(root, "art", "rebuild the boss encounter", "brief",
                     size="medium")
    item = queue.complete(
        root, item["id"],
        result="Verify: 14 passed, 0 failed against the encounter suite.")
    assert gates.wants_qa_agent_for(root, item) is True


def test_pytest_style_summary_counts_as_a_named_check(root):
    assert gates.names_passing_check("ran the suite: 9 passed") is True
    assert gates.names_passing_check("ran the suite: 9 passed, 1 failed") is False
    assert gates.names_passing_check("looks good, PASS") is False
