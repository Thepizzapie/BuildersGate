"""Causal chains over shipped telemetry — DESIGN.md §8 without the engine.

The load-bearing claim in `bgate_core.causal` is an INFERENCE, not a
measurement: a terminal event naming gate N as the failure implies gates 1..N-1
passed, because the game could not have reached N otherwise. That is sound only
while the ladder matches `fight.gd`'s real resolution order.

So these tests do two jobs. Most of them pin the chain output for known event
streams. The last one pins the ladder itself against the ORDER documented in the
spec — if someone reorders the gates in the game and updates the ladder without
thinking, that test is the thing that should make them think.
"""
from __future__ import annotations

import json

import pytest

from bgate_core import causal


def _ev(t: float, kind: str, **data) -> dict:
    return {"schema": 1, "ts": 1_700_000_000.0 + t, "t": t,
            "kind": kind, "data": data}


def _write(tmp_path, events) -> str:
    path = tmp_path / "telemetry.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                    encoding="utf-8")
    return str(path)


# --- the headline case -----------------------------------------------------


def test_facing_whiff_records_range_as_passed():
    """The whole point: `reason=facing` proves the RANGE gate passed.

    A raw `punch_whiffed reason=facing` line cannot distinguish "too far away"
    from "in range, pointed the wrong way". The chain must state the second
    explicitly, because they are different bugs with different fixes.
    """
    events = [
        _ev(1.00, "punch_thrown", by="player", type="jab", stamina_cost=10.0),
        _ev(1.15, "punch_whiffed", by="player", type="jab", reason="facing",
            distance=104.0),
    ]
    chains = causal.build_chains(events)
    assert len(chains) == 1
    chain = chains[0].to_dict()

    assert chain["outcome"] == "whiffed"
    assert chain["failed_gate"] == "facing_ok"

    joined = " ".join(chain["causal_chain"])
    # Range PASSED — stated, not implied by omission.
    assert "range_ok:distance=104.0@1.15" in joined
    assert "facing_ok:FAIL" in joined
    # Gates after the failure must NOT be claimed either way.
    assert "elevation_ok" not in joined
    assert "guard_ok" not in joined
    assert "damage_applied" not in joined


def test_range_whiff_claims_no_gate_passed():
    """A range failure is the FIRST gate, so nothing may be reported as passed."""
    events = [
        _ev(2.0, "punch_thrown", by="player", type="hook", stamina_cost=27.0),
        _ev(2.2, "punch_whiffed", by="player", type="hook", reason="range",
            distance=210.0, reach=135.0),
    ]
    chain = causal.build_chains(events)[0].to_dict()
    assert chain["failed_gate"] == "range_ok"
    assert chain["causal_chain"][1].startswith("range_ok:FAIL")
    assert not any("facing_ok" in e for e in chain["causal_chain"])


def test_elevation_whiff_passes_range_and_facing():
    events = [
        _ev(3.0, "punch_thrown", by="opponent", type="jab"),
        _ev(3.3, "punch_whiffed", by="opponent", type="jab", reason="elevation",
            distance=90.0, dy=120.0),
    ]
    chain = causal.build_chains(events)[0].to_dict()
    joined = " ".join(chain["causal_chain"])
    assert chain["failed_gate"] == "elevation_ok"
    assert "range_ok:distance=90.0" in joined
    assert "facing_ok:distance=90.0" in joined
    assert "elevation_ok:FAIL:dy=120.0" in joined


def test_landed_marks_every_gate_passed():
    events = [
        _ev(4.0, "punch_thrown", by="player", type="hook", stamina_cost=27.0),
        _ev(4.4, "punch_landed", by="player", type="hook", damage=10.0,
            counter=False, target_hp=90.0, distance=100.0, dy=0.0),
    ]
    chain = causal.build_chains(events)[0].to_dict()
    assert chain["outcome"] == "landed"
    assert chain["failed_gate"] is None
    joined = " ".join(chain["causal_chain"])
    for gate in ("range_ok", "facing_ok", "elevation_ok", "guard_ok"):
        assert gate in joined
        assert f"{gate}:FAIL" not in joined
    assert "damage_applied:damage=10.0" in joined


def test_blocked_is_a_guard_failure_not_a_whiff():
    """`punch_blocked` is its own KIND, not a `reason` — it must still land on
    the guard gate, with the three gates before it recorded as passed."""
    events = [
        _ev(5.0, "punch_thrown", by="player", type="jab"),
        _ev(5.2, "punch_blocked", by="player", type="jab", distance=95.0),
    ]
    chain = causal.build_chains(events)[0].to_dict()
    assert chain["outcome"] == "blocked"
    assert chain["failed_gate"] == "guard_ok"
    joined = " ".join(chain["causal_chain"])
    assert "range_ok" in joined and "facing_ok" in joined and "elevation_ok" in joined
    assert "guard_ok:BLOCKED" in joined
    assert "damage_applied" not in joined


# --- the economy path ------------------------------------------------------


def test_refusal_is_terminal_and_never_reaches_a_gate():
    """An attack refused by cooldown never launched — reporting a range result
    for it would invent a measurement the game never took."""
    events = [
        _ev(6.0, "punch_cooldown", by="player", type="jab"),
        _ev(6.5, "punch_gassed", by="player", type="hook", reason="overreached"),
    ]
    chains = [c.to_dict() for c in causal.build_chains(events)]
    assert [c["outcome"] for c in chains] == ["refused", "refused"]
    assert chains[0]["failed_gate"] == "punch_cooldown"
    assert chains[1]["failed_gate"] == "punch_gassed"
    for chain in chains:
        joined = " ".join(chain["causal_chain"])
        assert "range_ok" not in joined
    assert "gassed:FAIL:overreached" in " ".join(chains[1]["causal_chain"])


def test_aborted_target_ko_is_not_attributed_to_a_gate():
    events = [
        _ev(7.0, "punch_thrown", by="player", type="jab"),
        _ev(7.1, "punch_whiffed", by="player", type="jab", reason="target_ko"),
    ]
    chain = causal.build_chains(events)[0].to_dict()
    assert chain["outcome"] == "aborted"
    assert chain["failed_gate"] is None
    assert "range_ok" not in " ".join(chain["causal_chain"])


# --- stream shape ----------------------------------------------------------


def test_two_actors_interleave_without_crossing_chains():
    """Both fighters resolve independently and their events interleave freely;
    a chain must never pick up the other actor's terminal."""
    events = [
        _ev(1.0, "punch_thrown", by="player", type="jab"),
        _ev(1.1, "punch_thrown", by="opponent", type="hook"),
        _ev(1.2, "punch_whiffed", by="opponent", type="hook", reason="range",
            distance=200.0, reach=135.0),
        _ev(1.3, "punch_landed", by="player", type="jab", damage=4.0,
            target_hp=96.0),
    ]
    chains = [c.to_dict() for c in causal.build_chains(events)]
    by_actor = {c["actor"]: c for c in chains}
    assert by_actor["player"]["outcome"] == "landed"
    assert by_actor["player"]["move"] == "jab"
    assert by_actor["opponent"]["outcome"] == "whiffed"
    assert by_actor["opponent"]["move"] == "hook"


def test_second_attempt_supersedes_an_unresolved_one():
    """A combo cancel starts a fresh attack before the old one resolved. That
    is real signal, not a parse failure."""
    events = [
        _ev(1.0, "punch_thrown", by="player", type="jab"),
        _ev(1.2, "punch_thrown", by="player", type="hook"),
        _ev(1.6, "punch_landed", by="player", type="hook", damage=10.0),
    ]
    chains = [c.to_dict() for c in causal.build_chains(events)]
    assert [c["outcome"] for c in chains] == ["dropped", "landed"]
    assert "superseded@1.20" in chains[0]["causal_chain"]


def test_consequences_attach_only_within_the_window():
    """A ko right after a landed hook was caused by it. A ko much later was not."""
    near = causal.build_chains([
        _ev(1.0, "punch_thrown", by="player", type="hook"),
        _ev(1.4, "punch_landed", by="player", type="hook", damage=10.0),
        _ev(1.5, "ko", by="player", loser="opponent"),
    ])[0].to_dict()
    assert near["consequences"] == ["ko@1.50"]

    far = causal.build_chains([
        _ev(1.0, "punch_thrown", by="player", type="hook"),
        _ev(1.4, "punch_landed", by="player", type="hook", damage=10.0),
        _ev(30.0, "ko", by="player", loser="opponent"),
    ])[0].to_dict()
    assert far["consequences"] == []


def test_resolution_without_a_recorded_attempt_is_kept_as_partial():
    """fight.gd's handlers are public entry points its own comments note tests
    call directly. Dropping those resolutions would discard real evidence."""
    chain = causal.build_chains([
        _ev(1.0, "punch_landed", by="player", type="jab", damage=4.0),
    ])[0].to_dict()
    assert chain["outcome"] == "landed"
    assert chain["causal_chain"][0] == "attempt:UNRECORDED@1.00"


def test_unknown_reason_is_reported_not_guessed():
    """A whiff reason added to the game later must surface as unresolved rather
    than being silently attributed to whichever gate sorts last."""
    chain = causal.build_chains([
        _ev(1.0, "punch_thrown", by="player", type="jab"),
        _ev(1.1, "punch_whiffed", by="player", type="jab", reason="brand_new"),
    ])[0].to_dict()
    assert chain["outcome"] == "unknown"
    assert chain["failed_gate"] is None
    assert any("unresolved:brand_new" in e for e in chain["causal_chain"])


def test_missing_detail_fields_are_omitted_not_nulled():
    """Rendering `reach=None` would read as "the engine measured a null reach"."""
    chain = causal.build_chains([
        _ev(1.0, "punch_thrown", by="player", type="jab"),
        _ev(1.1, "punch_whiffed", by="player", type="jab", reason="range",
            distance=200.0),
    ])[0].to_dict()
    entry = [e for e in chain["causal_chain"] if e.startswith("range_ok")][0]
    assert "distance=200.0" in entry
    assert "reach" not in entry
    assert "None" not in entry


# --- file reading ----------------------------------------------------------


def test_torn_final_line_does_not_fail_the_read(tmp_path):
    """Telemetry is flushed on a timer, so a killed session routinely leaves a
    half-written last line. That must cost one event, not the whole file."""
    path = tmp_path / "t.jsonl"
    good = json.dumps(_ev(1.0, "punch_thrown", by="player", type="jab"))
    path.write_text(good + "\n" + '{"kind": "punch_lan', encoding="utf-8")
    events = causal.read_events(path)
    assert len(events) == 1
    assert events[0]["kind"] == "punch_thrown"


def test_missing_file_reads_as_empty(tmp_path):
    assert causal.read_events(tmp_path / "nope.jsonl") == []


def test_chains_from_file_roundtrip(tmp_path):
    path = _write(tmp_path, [
        _ev(1.0, "punch_thrown", by="player", type="jab"),
        _ev(1.2, "punch_whiffed", by="player", type="jab", reason="facing",
            distance=104.0),
    ])
    chains = causal.chains_from_file(path)
    assert chains[0]["failed_gate"] == "facing_ok"
    assert chains[0]["duration"] == pytest.approx(0.2, abs=1e-6)


# --- aggregate -------------------------------------------------------------


def test_summary_answers_why_attacks_whiff():
    """`by_failed_gate` is the number a tuning question actually wants: a facing
    spike and a range spike are different bugs and must not aggregate together."""
    events = []
    t = 0.0
    for reason in ("facing", "facing", "facing", "range"):
        events.append(_ev(t, "punch_thrown", by="player", type="jab"))
        events.append(_ev(t + 0.1, "punch_whiffed", by="player", type="jab",
                          reason=reason, distance=100.0))
        t += 1.0
    events.append(_ev(t, "punch_thrown", by="player", type="jab"))
    events.append(_ev(t + 0.1, "punch_landed", by="player", type="jab",
                      damage=4.0))

    summary = causal.summarize(causal.build_chains(events))
    assert summary["total"] == 5
    assert summary["by_failed_gate"] == {"facing_ok": 3, "range_ok": 1}
    assert summary["hit_rate"] == pytest.approx(0.2)


def test_hit_rate_excludes_refusals():
    """Counting refusals in the denominator makes an economy problem look like
    an aim problem."""
    events = [
        _ev(1.0, "punch_thrown", by="player", type="jab"),
        _ev(1.1, "punch_landed", by="player", type="jab", damage=4.0),
        _ev(2.0, "punch_cooldown", by="player", type="jab"),
        _ev(3.0, "punch_cooldown", by="player", type="jab"),
    ]
    summary = causal.summarize(causal.build_chains(events))
    assert summary["by_outcome"]["refused"] == 2
    assert summary["hit_rate"] == pytest.approx(1.0)


def test_find_filters_by_gate():
    events = [
        _ev(1.0, "punch_thrown", by="player", type="jab"),
        _ev(1.1, "punch_whiffed", by="player", type="jab", reason="facing"),
        _ev(2.0, "punch_thrown", by="opponent", type="hook"),
        _ev(2.1, "punch_whiffed", by="opponent", type="hook", reason="range"),
    ]
    chains = [c.to_dict() for c in causal.build_chains(events)]
    hits = causal.find(chains, failed_gate="facing_ok")
    assert len(hits) == 1 and hits[0]["actor"] == "player"
    assert causal.find(chains, actor="opponent")[0]["move"] == "hook"


# --- the inference itself --------------------------------------------------


def test_ladder_order_matches_the_documented_source_order():
    """This is the guard on the whole module.

    Every PASS in a chain is inferred from this order. If the game's gates are
    reordered, this test failing is the intended way to find out — the
    alternative is chains that stay plausible and become wrong.

    Source: haymaker/game/scripts/fight.gd — range (:760), facing (:779),
    elevation (:793), guard (:798), damage (:809).
    """
    spec = causal.describe_spec("fighter_attack")
    assert [g["gate"] for g in spec["ladder"]] == [
        "range_ok", "facing_ok", "elevation_ok", "guard_ok", "damage_applied"]
    assert [g["fails_with_reason"] for g in spec["ladder"]] == [
        "range", "facing", "elevation", "ducked", None]
    assert "fight.gd" in spec["source"]


def test_fireball_spec_has_no_facing_gate():
    """A projectile travels, so the game never checks facing for it. Reading a
    fireball through the punch ladder would assert a gate that never ran."""
    spec = causal.describe_spec("fireball")
    assert "facing_ok" not in [g["gate"] for g in spec["ladder"]]

    chain = causal.build_chains([
        _ev(1.0, "fireball_thrown", by="opponent", type="fireball"),
        _ev(1.8, "fireball_blocked", by="opponent", type="fireball"),
    ], causal.FIREBALL)[0].to_dict()
    assert chain["failed_gate"] == "guard_ok"
    assert "facing_ok" not in " ".join(chain["causal_chain"])


def test_every_spec_is_self_consistent():
    """A ladder whose reasons do not match its terminals silently produces
    `unknown` for real events — cheap to check, expensive to debug."""
    for name, spec in causal.SPECS.items():
        assert spec.landed in spec.terminals, name
        reasons = [g.reason for g in spec.ladder if g.reason]
        assert len(reasons) == len(set(reasons)), f"{name} has duplicate reasons"
        assert spec.ladder[-1].reason is None, f"{name} must end in a success gate"
