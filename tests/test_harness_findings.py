"""The stress-test findings, each as the test that would have caught it.

Every case here is a thing that happened in one overnight run of a real game
project, not a thing that could happen. The comments name what it cost.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bgate_core import activity, db, project, queue, scaffold, seats, settings
from bgate_ui import qa_gate


@pytest.fixture()
def root(tmp_path):
    project.init(tmp_path, "Findings", pitch="a project for regression tests")
    yield tmp_path
    db.close_all()


@pytest.fixture()
def as_agent(monkeypatch):
    """The environment a dispatched seat actually runs in."""
    monkeypatch.setenv("BGATE_ACTOR", "agent:item-7")
    monkeypatch.setenv("BGATE_WORK_ITEM", "7")
    monkeypatch.setenv("BGATE_SEAT", "art")


@pytest.fixture()
def as_human(monkeypatch):
    for var in ("BGATE_ACTOR", "BGATE_WORK_ITEM", "BGATE_SEAT"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# #1 and #3 — an agent could switch off the gate that judges it
# ---------------------------------------------------------------------------

HUMAN_ONLY = ("gate.mode", "budget.enforced", "dispatch.max_concurrent",
              "budget.per_item_usd", "qa.max_rounds", "qa.gated_seats")


@pytest.mark.parametrize("key", HUMAN_ONLY)
def test_the_constraint_switches_are_declared_human_only(key):
    assert settings.setting(key).human_only is True, key


def test_an_agent_cannot_switch_off_its_own_reviewer(root, as_agent):
    """FOUR TIMES in one run, gate.mode was found back at 'none'."""
    with pytest.raises(settings.SettingError) as exc:
        settings.set(root, "gate.mode", "none")
    assert "HUMAN-ONLY" in str(exc.value)
    assert settings.get(root, "gate.mode") == "agent"


def test_an_agent_cannot_turn_the_budget_into_a_report(root, as_agent):
    """budget.enforced off makes every ceiling advisory — which is how the
    worst overruns in the run got past $16 against a $5 ceiling."""
    with pytest.raises(settings.SettingError):
        settings.set(root, "budget.enforced", False)
    assert settings.get(root, "budget.enforced") is True


def test_an_agent_cannot_widen_its_own_concurrency(root, as_agent):
    """Set to 4 by a human, observed at 9 and then 11."""
    with pytest.raises(settings.SettingError):
        settings.set(root, "dispatch.max_concurrent", 12)


def test_the_refusal_names_what_to_do_instead(root, as_agent):
    with pytest.raises(settings.SettingError) as exc:
        settings.set(root, "gate.mode", "none")
    assert "ask_human" in str(exc.value)


def test_a_human_may_still_change_all_of_them(root, as_human):
    settings.set(root, "gate.mode", "builders")
    assert settings.get(root, "gate.mode") == "builders"
    settings.set(root, "dispatch.max_concurrent", 6)
    assert settings.get(root, "dispatch.max_concurrent") == 6


def test_a_seat_env_alone_reads_as_a_machine(root, monkeypatch):
    """BGATE_ACTOR is one stamp in one spawn path; BGATE_SEAT is set by every
    spawn because the hook needs it. Forgetting the first must not disable the
    gate."""
    monkeypatch.delenv("BGATE_ACTOR", raising=False)
    monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
    monkeypatch.setenv("BGATE_SEAT", "gameplay")
    assert activity.is_machine("someone@host") is True
    with pytest.raises(settings.SettingError):
        settings.set(root, "gate.mode", "none")


def test_an_ordinary_setting_is_still_writable_by_an_agent(root, as_agent):
    """The policy is a short list, not a lockdown."""
    settings.set(root, "art.lora_strength", 0.5)
    assert settings.get(root, "art.lora_strength") == 0.5


# ---------------------------------------------------------------------------
# #5 — QA coverage was a hardcoded tuple with no override
# ---------------------------------------------------------------------------

def test_gated_seats_defaults_to_every_maker_seat(root):
    # The old four left tech and cinematic closing on the agent's word alone;
    # every maker seat is gated by default now. director and qa stay out:
    # that is recursion, not review.
    assert qa_gate.gated_seats(root) == (
        "art", "gameplay", "audio", "narrative", "tech", "cinematic")


def test_a_project_can_narrow_qa_to_one_seat(root, as_human):
    settings.set(root, "qa.gated_seats", ["art"])
    assert qa_gate.gated_seats(root) == ("art",)


def test_the_registry_refuses_qa_and_director_outright(root, as_human):
    """Gating the gate is recursion, not review. The choices list refuses it
    before a doc can hold it."""
    with pytest.raises(settings.SettingError):
        settings.set(root, "qa.gated_seats", ["art", "qa"])


def test_a_doc_holding_illegal_seats_cannot_gate_the_gate(root):
    """Belt and braces, and TWO mechanisms have to agree here.

    A settings doc edited by hand — or written by a version whose choices list
    differed — can hold anything. The registry refuses to READ a stored list
    that is not in `choices` and falls back to the default; the gate then
    filters qa and director out of whatever it is handed. Either alone would do;
    the invariant being asserted is that neither route can produce a reviewer
    reviewing reviewers.
    """
    from bgate_core import workspace as _ws

    data = dict(_ws.get(root, settings.REGISTRY_SEAT, settings.REGISTRY_KEY) or {})
    data["qa.gated_seats"] = ["art", "qa", "director"]
    _ws.set(root, settings.REGISTRY_SEAT, settings.REGISTRY_KEY, data,
            if_version=_ws.version(root, settings.REGISTRY_SEAT,
                                   settings.REGISTRY_KEY))
    seats_now = qa_gate.gated_seats(root)
    assert "qa" not in seats_now and "director" not in seats_now


def test_the_gates_own_filter_holds_without_the_registry(root, monkeypatch):
    """The second mechanism on its own: hand the gate a bad list directly."""
    monkeypatch.setattr("bgate_core.settings.get",
                        lambda *_a, **_k: ["art", "qa", "director", "gameplay"])
    assert qa_gate.gated_seats(root) == ("art", "gameplay")


# ---------------------------------------------------------------------------
# #6 — a hand-closed item spawned a stale reviewer
# ---------------------------------------------------------------------------

def test_a_human_hand_close_does_not_owe_a_review(root, as_human):
    item = queue.add(root, "art", "vehicles")
    queue.complete(root, item["id"], result="closed by hand after a kill")
    assert queue.get(root, item["id"])["gate_skip"] == 1


def test_an_agent_reporting_its_own_work_still_owes_a_review(root, as_agent):
    item = queue.add(root, "art", "hero character")
    queue.complete(root, item["id"], result="done, rigged and verified")
    assert queue.get(root, item["id"])["gate_skip"] == 0


def test_the_caller_can_say_either_way(root, as_agent):
    item = queue.add(root, "art", "a thing")
    queue.complete(root, item["id"], result="done", skip_gate=True)
    assert queue.get(root, item["id"])["gate_skip"] == 1


def test_who_closed_it_is_recorded(root, as_agent):
    item = queue.add(root, "art", "a thing")
    queue.complete(root, item["id"], result="done")
    assert queue.get(root, item["id"])["closed_by"].startswith("agent:")


# ---------------------------------------------------------------------------
# #2 and #4 — a stopped run threw its own work away
# ---------------------------------------------------------------------------

def test_reopen_hands_the_next_round_what_is_already_on_disk(root):
    """A ceiling stop and a dashboard restart both leave finished files behind.
    Re-running from scratch pays twice for work that is sitting there."""
    from bgate_core import writelog

    item = queue.add(root, "gameplay", "player controller")
    writelog.record(root, "game/scripts/player.gd", "gameplay",
                    f"item-{item['id']}")
    queue.set_status(root, item["id"], "failed", result="stopped at the ceiling")
    queue.reopen(root, item["id"], "continue where it stopped")
    brief = queue.get(root, item["id"])["brief"]
    assert "ALREADY ON DISK" in brief
    assert "player.gd" in brief


def test_the_wrap_up_fires_before_the_kill_not_after():
    """The ceiling asks the agent to bank its work before it becomes a kill."""
    from bgate_ui import dispatch

    assert 0.0 < dispatch.WRAP_AT < 1.0
    text = dispatch.WRAP_TEXT.format(spent=4.10, limit=5.0)
    assert "queue_complete" in text and "$4.10" in text


# ---------------------------------------------------------------------------
# #9 — the scaffold left the root ungitignored, and git ate the token
# ---------------------------------------------------------------------------

def test_scaffold_stamps_the_root_that_actually_holds_the_token(root):
    got = scaffold.new_project(Path(root) / "game", "Findings", kind="2d")
    assert got["ok"] is True
    stamped = (Path(root) / ".gitignore")
    assert stamped.is_file(), "the Builders Gate root got no .gitignore"
    body = stamped.read_text(encoding="utf-8")
    assert ".bgate/" in body and ".env" in body


# ---------------------------------------------------------------------------
# #10 — docs/** was owned by nobody while seats were told to write there
# ---------------------------------------------------------------------------

def test_docs_has_an_owner_in_the_default_lane_table():
    lanes = seats.DEFAULT_SEATS["director"]["write_globs"]
    assert "docs/**" in lanes


def test_the_director_can_write_the_report_every_seat_was_told_to_append_to(root):
    assert seats.can_write(root, "director",
                           "docs/3d-pipeline-report.md")["allowed"] is True


# ---------------------------------------------------------------------------
# #7 — two dashboards, one port, and writes landing on the wrong game
# ---------------------------------------------------------------------------

def test_health_says_which_project_is_being_served(root, monkeypatch):
    from fastapi.testclient import TestClient
    from bgate_ui import app as _app

    monkeypatch.setenv("BGATE_ROOT", str(root))
    got = TestClient(_app.app).get("/api/health")
    assert got.status_code == 200, "the probe must not need a token"
    assert Path(got.json()["root"]).resolve() == Path(root).resolve()


def test_a_dashboard_on_another_root_is_detected(monkeypatch, tmp_path):
    from bgate_ui import app as _app

    monkeypatch.setattr(
        _app, "_serving_elsewhere",
        lambda port, root: str(tmp_path / "someone-elses-game"))
    with pytest.raises(SystemExit) as exc:
        _app.serve(port=7788)
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# #8 — a Krea-only setup read as "no image generation"
# ---------------------------------------------------------------------------

def test_image_status_reports_every_leg(root, monkeypatch):
    import asyncio

    import bgate_mcp.server as server

    monkeypatch.setenv("BGATE_ROOT", str(root))
    got = asyncio.run(server.image_status())
    # Derived from the provider registry, not written out again here: this
    # named three legs and kie made it four. A hand-kept copy of a registry is
    # a second registry that goes stale on the next provider.
    from bgate_core import providers as _providers

    # The rented providers come from the registry; "local" is the leg that is
    # not a provider at all (a runtime on this machine), so it is named.
    assert set(got["legs"]) == {p.id for p in _providers.art_providers()} | {"local"}
    # `available` answers about the LEG, not about one adapter.
    assert got["available"] == bool(got["providers"])


# ---------------------------------------------------------------------------
# The migration mechanism my own column addition broke
# ---------------------------------------------------------------------------

def test_add_column_migrations_are_replay_safe(root):
    """ADD COLUMN has no IF NOT EXISTS, so replaying one used to raise
    'duplicate column name' and take the dashboard down — the exact failure the
    CREATE-based healing already covered."""
    conn = db.connect(root)
    sql = ("ALTER TABLE work_item ADD COLUMN closed_by TEXT NOT NULL DEFAULT '';"
           "ALTER TABLE work_item ADD COLUMN brand_new TEXT NOT NULL DEFAULT '';")
    kept = db._skip_existing_columns(conn, sql)
    assert "closed_by" not in kept
    assert "brand_new" in kept
