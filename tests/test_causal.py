"""Causal chains over shipped telemetry — DESIGN.md §8, no engine.

The load-bearing claim in `bgate_core.causal` is an INFERENCE, not a
measurement: a terminal naming gate N as the failure implies gates 1..N-1
passed, because the game could not have reached N otherwise. That is sound only
while the ladder matches the game's real resolution order — which is why
`order_verified` exists and why an unverified ladder renders differently.

These tests define their own spec inline. That is the point: the module must
know nothing about any particular game, so a test that imported a game constant
would be testing the wrong thing. One integration test at the end loads a real
project's spec from disk.
"""
from __future__ import annotations

import json

import pytest

from bgate_core import causal


def _ev(t: float, kind: str, **data) -> dict:
    return {"schema": 1, "ts": 1_700_000_000.0 + t, "t": t,
            "kind": kind, "data": data}


# A deliberately non-fighter vocabulary: a heist game's lockpick pipeline. If
# anything game-specific leaks back into bgate_core, these stop working.
HEIST = causal.ChainSpec(
    name="lockpick",
    opens_on=("pick_started",),
    refusals=("pick_refused",),
    terminals=("pick_opened", "pick_jammed", "pick_failed"),
    actor_key="thief",
    ladder=(
        causal.Gate("in_reach", "distance", ("gap",)),
        causal.Gate("tool_ok", "wrong_tool", ("tool",)),
        causal.Gate("unseen", "spotted", ("guard",)),
        causal.Gate("opened", None, ("loot",)),
    ),
    landed="pick_opened",
    blocked_suffix="_jammed",
    blocked_gate="tool_ok",
    aborted_reasons=("alarm_already_tripped",),
    consequences=("alarm", "escape"),
    order_verified=True,
    source="heist.gd — fictional, for tests",
)


# --- the headline case -----------------------------------------------------


def test_late_gate_failure_records_earlier_gates_as_passed():
    """The whole point: naming gate 3 proves gates 1 and 2 passed.

    A raw `reason=spotted` line cannot distinguish "too far away" from "close
    enough, right tool, and a guard saw you". Those are different bugs.
    """
    chain = causal.build_chains([
        _ev(1.00, "pick_started", thief="mara", type="tension_wrench"),
        _ev(1.40, "pick_failed", thief="mara", type="tension_wrench",
            reason="spotted", gap=0.5, tool="wrench", guard="hallway"),
    ], HEIST)[0].to_dict()

    assert chain["outcome"] == "failed"
    assert chain["failed_gate"] == "unseen"
    joined = " ".join(chain["causal_chain"])
    assert "in_reach:gap=0.5@1.40" in joined
    assert "tool_ok:tool=wrench@1.40" in joined
    assert "unseen:FAIL" in joined
    # Gates after the failure must not be claimed either way.
    assert "opened" not in joined


def test_first_gate_failure_claims_nothing_passed():
    chain = causal.build_chains([
        _ev(2.0, "pick_started", thief="mara", type="rake"),
        _ev(2.2, "pick_failed", thief="mara", reason="distance", gap=9.0),
    ], HEIST)[0].to_dict()
    assert chain["failed_gate"] == "in_reach"
    assert chain["causal_chain"][1].startswith("in_reach:FAIL")
    assert not any("tool_ok" in e for e in chain["causal_chain"])


def test_success_marks_every_gate_passed():
    chain = causal.build_chains([
        _ev(4.0, "pick_started", thief="dov", type="bump_key"),
        _ev(4.4, "pick_opened", thief="dov", loot="ledger"),
    ], HEIST)[0].to_dict()
    assert chain["outcome"] == "landed"
    assert chain["failed_gate"] is None
    joined = " ".join(chain["causal_chain"])
    for gate in ("in_reach", "tool_ok", "unseen", "opened"):
        assert gate in joined and f"{gate}:FAIL" not in joined


def test_blocked_kind_maps_to_its_gate_not_to_a_reason():
    """Some games report a guard result as its own KIND rather than a reason."""
    chain = causal.build_chains([
        _ev(5.0, "pick_started", thief="mara", type="rake"),
        _ev(5.2, "pick_jammed", thief="mara"),
    ], HEIST)[0].to_dict()
    assert chain["outcome"] == "blocked"
    assert chain["failed_gate"] == "tool_ok"
    joined = " ".join(chain["causal_chain"])
    assert "in_reach" in joined and "tool_ok:BLOCKED" in joined
    assert "unseen" not in joined


def test_refusal_never_reaches_a_gate():
    """An action refused before it launched took no measurements at all."""
    chain = causal.build_chains([
        _ev(6.0, "pick_refused", thief="dov", type="rake", reason="no_tool"),
    ], HEIST)[0].to_dict()
    assert chain["outcome"] == "refused"
    assert chain["failed_gate"] == "pick_refused"
    assert "in_reach" not in " ".join(chain["causal_chain"])


def test_aborted_reason_is_not_attributed_to_a_gate():
    chain = causal.build_chains([
        _ev(7.0, "pick_started", thief="mara"),
        _ev(7.1, "pick_failed", thief="mara", reason="alarm_already_tripped"),
    ], HEIST)[0].to_dict()
    assert chain["outcome"] == "aborted"
    assert chain["failed_gate"] is None
    assert "in_reach" not in " ".join(chain["causal_chain"])


# --- stream shape ----------------------------------------------------------


def test_actors_interleave_without_crossing_chains():
    chains = [c.to_dict() for c in causal.build_chains([
        _ev(1.0, "pick_started", thief="mara", type="rake"),
        _ev(1.1, "pick_started", thief="dov", type="bump_key"),
        _ev(1.2, "pick_failed", thief="dov", reason="distance", gap=9.0),
        _ev(1.3, "pick_opened", thief="mara", loot="ledger"),
    ], HEIST)]
    by_actor = {c["actor"]: c for c in chains}
    assert by_actor["mara"]["outcome"] == "landed"
    assert by_actor["dov"]["failed_gate"] == "in_reach"


def test_single_actor_game_needs_no_actor_field():
    """actor_key="" is a supported shape, not a degenerate one."""
    solo = causal.ChainSpec(
        name="solo", opens_on=("try",), terminals=("done",),
        ladder=(causal.Gate("ok", None, ()),), landed="done",
        order_verified=True)
    chain = causal.build_chains(
        [_ev(1.0, "try"), _ev(1.1, "done")], solo)[0].to_dict()
    assert chain["actor"] == "*"
    assert chain["outcome"] == "landed"


def test_second_attempt_supersedes_an_unresolved_one():
    chains = [c.to_dict() for c in causal.build_chains([
        _ev(1.0, "pick_started", thief="mara", type="rake"),
        _ev(1.2, "pick_started", thief="mara", type="bump_key"),
        _ev(1.6, "pick_opened", thief="mara"),
    ], HEIST)]
    assert [c["outcome"] for c in chains] == ["dropped", "landed"]
    assert "superseded@1.20" in chains[0]["causal_chain"]


def test_consequences_attach_only_within_the_window():
    near = causal.build_chains([
        _ev(1.0, "pick_started", thief="mara"),
        _ev(1.4, "pick_opened", thief="mara"),
        _ev(1.5, "alarm", thief="mara"),
    ], HEIST)[0].to_dict()
    assert near["consequences"] == ["alarm@1.50"]

    far = causal.build_chains([
        _ev(1.0, "pick_started", thief="mara"),
        _ev(1.4, "pick_opened", thief="mara"),
        _ev(30.0, "alarm", thief="mara"),
    ], HEIST)[0].to_dict()
    assert far["consequences"] == []


def test_resolution_without_an_attempt_is_kept_as_partial():
    chain = causal.build_chains(
        [_ev(1.0, "pick_opened", thief="mara")], HEIST)[0].to_dict()
    assert chain["outcome"] == "landed"
    assert chain["causal_chain"][0] == "attempt:UNRECORDED@1.00"


def test_unknown_reason_is_reported_not_guessed():
    """A reason added to the game later must surface as unresolved rather than
    being attributed to whichever gate happens to sort last."""
    chain = causal.build_chains([
        _ev(1.0, "pick_started", thief="mara"),
        _ev(1.1, "pick_failed", thief="mara", reason="brand_new"),
    ], HEIST)[0].to_dict()
    assert chain["outcome"] == "unknown"
    assert chain["failed_gate"] is None
    assert any("unresolved:brand_new" in e for e in chain["causal_chain"])


def test_missing_detail_fields_are_omitted_not_nulled():
    """Rendering `gap=None` would read as "the game measured a null gap"."""
    chain = causal.build_chains([
        _ev(1.0, "pick_started", thief="mara"),
        _ev(1.1, "pick_failed", thief="mara", reason="wrong_tool", tool="rake"),
    ], HEIST)[0].to_dict()
    entry = [e for e in chain["causal_chain"] if e.startswith("in_reach")][0]
    assert "gap" not in entry and "None" not in entry


# --- unverified ladders must look different --------------------------------


def test_unverified_order_marks_assumed_gates_and_warns():
    """An inferred ladder must never produce chains as authoritative-looking as
    a verified one. That is exactly how plausible-and-wrong happens."""
    draft = causal.ChainSpec(
        name="draft", opens_on=("pick_started",),
        terminals=("pick_failed", "pick_opened"), actor_key="thief",
        ladder=(causal.Gate("in_reach", "distance", ()),
                causal.Gate("unseen", "spotted", ()),
                causal.Gate("opened", None, ())),
        landed="pick_opened", order_verified=False)

    chain = causal.build_chains([
        _ev(1.0, "pick_started", thief="mara"),
        _ev(1.1, "pick_failed", thief="mara", reason="spotted"),
    ], draft)[0].to_dict()

    assert chain["order_verified"] is False
    assert "warning" in chain
    # The gate the ladder ASSUMES passed is marked; the observed failure is not.
    assert any(e.startswith("~in_reach") for e in chain["causal_chain"])
    assert any(e.startswith("unseen:FAIL") for e in chain["causal_chain"])

    summary = causal.summarize([chain])
    assert "warning" in summary


def test_verified_order_has_no_tildes_or_warning():
    chain = causal.build_chains([
        _ev(1.0, "pick_started", thief="mara"),
        _ev(1.1, "pick_failed", thief="mara", reason="spotted"),
    ], HEIST)[0].to_dict()
    assert chain["order_verified"] is True
    assert "warning" not in chain
    assert not any(e.startswith("~") for e in chain["causal_chain"])


# --- specs are project data, not harness constants -------------------------


def test_module_defines_no_specs_of_its_own():
    """Regression guard on the actual correction.

    A first version of this module hard-coded an arcade fighter's event kinds
    into bgate_core — the mistake DESIGN.md §16.2 diagnoses in
    component_defs.json, one layer up. Specs are per-project data; the harness
    ships none. Checked structurally rather than by grepping text, because the
    invariant is "no spec constants", not "no game words in the prose".
    """
    leaked = [name for name, value in vars(causal).items()
              if isinstance(value, (causal.ChainSpec, causal.Gate))]
    assert leaked == [], f"harness defines game specs: {leaked}"

    containers = [name for name, value in vars(causal).items()
                  if isinstance(value, (dict, list, tuple)) and value
                  and not name.startswith("__")
                  and any(isinstance(v, (causal.ChainSpec, causal.Gate))
                          for v in (value.values() if isinstance(value, dict)
                                    else value))]
    assert containers == [], f"harness ships a spec registry: {containers}"


def test_no_game_vocabulary_in_executable_code():
    """The same invariant from the other direction: game words may appear in
    explanatory prose (they make the docs concrete) but never in code."""
    import io
    import inspect
    import tokenize

    source = inspect.getsource(causal)
    code = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        # Drop comments and every string literal — docstrings, error messages,
        # and doc text are prose, not vocabulary the module acts on.
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        code.append(tok.string)
    joined = " ".join(code).lower()

    for leak in ("punch", "jab", "hook", "fireball", "fighter", "stamina",
                 "gassed", "boxer"):
        assert leak not in joined, f"game vocabulary in code: {leak!r}"


def test_specs_round_trip_through_disk(tmp_path):
    saved = causal.save_spec(tmp_path, HEIST)
    assert saved["ok"] and "lockpick" in saved["specs"]

    loaded = causal.load_specs(tmp_path)
    assert set(loaded) == {"lockpick"}
    spec = loaded["lockpick"]
    assert spec.actor_key == "thief"
    assert [g.name for g in spec.ladder] == [
        "in_reach", "tool_ok", "unseen", "opened"]
    assert spec.order_verified is True


def test_saving_preserves_sibling_specs(tmp_path):
    other = causal.ChainSpec(name="heist_alarm", opens_on=("a",),
                             terminals=("b",), landed="b")
    causal.save_spec(tmp_path, HEIST)
    causal.save_spec(tmp_path, other)
    assert set(causal.load_specs(tmp_path)) == {"lockpick", "heist_alarm"}


def test_project_with_no_specs_is_not_an_error(tmp_path):
    assert causal.load_specs(tmp_path) == {}


def test_unknown_spec_name_says_what_is_available(tmp_path):
    causal.save_spec(tmp_path, HEIST)
    with pytest.raises(KeyError, match="lockpick"):
        causal.resolve_spec("nope", tmp_path)


def test_corrupt_spec_file_degrades_to_empty(tmp_path):
    path = tmp_path / ".bgate"
    path.mkdir()
    (path / causal.SPEC_FILE).write_text("{not json", encoding="utf-8")
    assert causal.load_specs(tmp_path) == {}


def test_unknown_spec_keys_are_ignored_not_fatal():
    """A project file written by a newer version must not crash an older one."""
    spec = causal.spec_from_dict("x", {"opens_on": ["a"], "terminals": ["b"],
                                       "from_the_future": {"nested": 1}})
    assert spec.opens_on == ("a",)


# --- inference -------------------------------------------------------------


def test_infer_drafts_a_spec_from_unseen_telemetry():
    """Bootstrap for a game the harness has never seen."""
    events = []
    t = 0.0
    for reason in ("distance", "spotted", None, "wrong_tool", None):
        events.append(_ev(t, "pick_started", thief="mara", type="rake"))
        if reason:
            events.append(_ev(t + 0.2, "pick_failed", thief="mara",
                              reason=reason))
        else:
            events.append(_ev(t + 0.2, "pick_opened", thief="mara"))
        t += 5.0

    got = causal.infer_spec(events)
    assert got["ok"] is True
    spec = got["spec"]["pick"]
    assert spec["opens_on"] == ["pick_started"]
    assert set(spec["terminals"]) == {"pick_failed", "pick_opened"}
    assert spec["actor_key"] == "thief"
    assert spec["landed"] == "pick_opened"
    assert {g["fails_with_reason"] for g in spec["ladder"]} == {
        "distance", "spotted", "wrong_tool", None}


def test_inferred_spec_is_never_marked_verified():
    """Order is a property of the source, not of telemetry. Inference must not
    claim to have solved the one thing it cannot solve."""
    events = []
    for i, reason in enumerate(("distance", "spotted")):
        events.append(_ev(i * 5.0, "pick_started", thief="mara"))
        events.append(_ev(i * 5.0 + 0.2, "pick_failed", thief="mara",
                          reason=reason))
    got = causal.infer_spec(events)
    assert got["spec"]["pick"]["order_verified"] is False
    assert any("REVIEW REQUIRED" in n for n in got["notes"])
    assert any("order" in n.lower() for n in got["notes"])


def test_infer_says_so_when_the_sample_contains_no_failures():
    """Observed live: a session where the CPU landed 14 of 14 attacks yields an
    EMPTY ladder. A draft that looks well-formed while describing no gates at
    all is worse than an obvious error, so the coverage gap is named."""
    events = []
    for i in range(4):
        events.append(_ev(i * 5.0, "pick_started", thief="mara"))
        events.append(_ev(i * 5.0 + 0.2, "pick_opened", thief="mara"))

    got = causal.infer_spec(events)
    assert got["ok"] is True
    assert got["coverage"] == {"reasons_observed": 0, "sufficient": False}
    assert any("NO FAILURES IN THIS SAMPLE" in n for n in got["notes"])


def test_infer_reports_when_nothing_looks_like_a_pipeline():
    got = causal.infer_spec([_ev(1.0, "fps", fps=60), _ev(2.0, "fps", fps=59)])
    assert got["ok"] is False
    assert "fps" in got["kinds"]


def test_infer_lists_other_candidate_families():
    events = [
        _ev(1.0, "pick_started", thief="m"), _ev(1.2, "pick_opened", thief="m"),
        _ev(2.0, "door_opened"), _ev(2.5, "door_closed"),
    ]
    got = causal.infer_spec(events)
    assert got["ok"] is True
    assert "door" in got["candidates"] and "pick" in got["candidates"]


def test_infer_can_target_a_named_family():
    events = [
        _ev(1.0, "pick_started", thief="m"), _ev(1.2, "pick_opened", thief="m"),
        _ev(2.0, "door_opened"), _ev(2.5, "door_closed"),
    ]
    got = causal.infer_spec(events, family="door")
    assert got["ok"] is True and "door" in got["spec"]


# --- file reading ----------------------------------------------------------


def test_torn_final_line_does_not_fail_the_read(tmp_path):
    """Telemetry is flushed on a timer, so a killed session routinely leaves a
    half-written last line. That must cost one event, not the whole file."""
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(_ev(1.0, "pick_started", thief="m"))
                    + "\n" + '{"kind": "pick_op', encoding="utf-8")
    events = causal.read_events(path)
    assert len(events) == 1 and events[0]["kind"] == "pick_started"


def test_missing_file_reads_as_empty(tmp_path):
    assert causal.read_events(tmp_path / "nope.jsonl") == []


def test_chains_from_file_resolves_a_project_spec(tmp_path):
    causal.save_spec(tmp_path, HEIST)
    tel = tmp_path / "telemetry.jsonl"
    tel.write_text("\n".join(json.dumps(e) for e in [
        _ev(1.0, "pick_started", thief="mara"),
        _ev(1.2, "pick_failed", thief="mara", reason="spotted"),
    ]), encoding="utf-8")

    chains = causal.chains_from_file(tel, "lockpick", tmp_path)
    assert chains[0]["failed_gate"] == "unseen"
    assert chains[0]["duration"] == pytest.approx(0.2, abs=1e-6)


# --- aggregate -------------------------------------------------------------


def test_summary_separates_one_failing_gate_from_another():
    events = []
    t = 0.0
    for reason in ("spotted", "spotted", "spotted", "distance"):
        events.append(_ev(t, "pick_started", thief="mara"))
        events.append(_ev(t + 0.1, "pick_failed", thief="mara", reason=reason))
        t += 5.0
    events.append(_ev(t, "pick_started", thief="mara"))
    events.append(_ev(t + 0.1, "pick_opened", thief="mara"))

    summary = causal.summarize(causal.build_chains(events, HEIST))
    assert summary["total"] == 5
    assert summary["by_failed_gate"] == {"unseen": 3, "in_reach": 1}
    assert summary["success_rate"] == pytest.approx(0.2)


def test_success_rate_excludes_refusals():
    """Counting refusals in the denominator makes an economy problem look like
    an accuracy problem."""
    summary = causal.summarize(causal.build_chains([
        _ev(1.0, "pick_started", thief="m"),
        _ev(1.1, "pick_opened", thief="m"),
        _ev(2.0, "pick_refused", thief="m"),
        _ev(3.0, "pick_refused", thief="m"),
    ], HEIST))
    assert summary["by_outcome"]["refused"] == 2
    assert summary["success_rate"] == pytest.approx(1.0)


def test_find_filters_by_gate():
    chains = [c.to_dict() for c in causal.build_chains([
        _ev(1.0, "pick_started", thief="mara"),
        _ev(1.1, "pick_failed", thief="mara", reason="spotted"),
        _ev(5.0, "pick_started", thief="dov"),
        _ev(5.1, "pick_failed", thief="dov", reason="distance"),
    ], HEIST)]
    hits = causal.find(chains, failed_gate="unseen")
    assert len(hits) == 1 and hits[0]["actor"] == "mara"


# --- integration with a real project ---------------------------------------


HAYMAKER = r"C:\Users\adria\Desktop\haymaker"


def test_a_real_project_spec_loads_and_declares_its_source():
    """The fighter's vocabulary lives in ITS project, not in the harness."""
    import os
    if not os.path.exists(os.path.join(HAYMAKER, ".bgate",
                                       causal.SPEC_FILE)):
        pytest.skip("haymaker project spec not present")

    specs = causal.load_specs(HAYMAKER)
    assert "attack" in specs
    attack = specs["attack"]
    assert attack.order_verified is True
    assert "fight.gd" in attack.source
    assert [g.name for g in attack.ladder] == [
        "range_ok", "facing_ok", "elevation_ok", "guard_ok", "damage_applied"]
    # A projectile travels, so its ladder deliberately has no facing gate.
    assert "facing_ok" not in [g.name for g in specs["fireball"].ladder]
