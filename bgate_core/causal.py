"""Causal chains over telemetry — DESIGN.md §8, for any game, no engine.

An event that carries the gates it passed beats any log line. A whiff event with
`reason: "facing"` tells you the attack failed the facing check; it does not tell
you the range check *passed*, which is the fact that turns "it whiffed" into "it
was in range and pointed the wrong way." That second fact is the diagnostic
value, and every game throws it away.

It does not have to be. **Resolution gates run in a fixed order, so the losing
gate implies every gate before it passed.** A game that resolves

    range -> facing -> elevation -> guard -> damage

and reports `reason: "elevation"` has told you range and facing both passed,
necessarily, or control would never have reached the elevation check. One string
reconstructs the whole ladder — from telemetry the game already writes, with no
second runtime and no new store.

WHAT IS IN THIS MODULE AND WHAT IS NOT
--------------------------------------
This module is the MECHANISM only. It knows nothing about any game: not one
event kind, not one field name, not one gate. That is deliberate, and it is the
correction to a first version of this file that hard-coded an arcade fighter's
vocabulary into `bgate_core` — the exact mistake DESIGN.md §16.2 diagnoses in
`component_defs.json`, repeated one layer up. Builders Gate is the harness; the
harness must not know what a "jab" is.

The only contract assumed is the one `playtest.telemetry_contract()` publishes
and the shipped `BGateTelemetry` autoload writes: JSONL of
`{ts, kind, data?, t?, schema?}`. Event kinds and payload fields are the GAME's
choice, so which kinds form a chain is per-project data, stored in
`.bgate/causal_specs.json` and loaded by `load_specs()`.

THE ORDER PROBLEM
-----------------
Every PASS in a chain is an inference from gate ORDER. Order is a property of
the game's source, not of its telemetry — no amount of event data reveals it.
`infer_spec()` can propose which kinds pair up and which reasons exist, because
those are observable; it CANNOT order the ladder. So an inferred spec is marked
`order_verified: false`, and chains built from it render passed gates with a `~`
prefix and carry a warning. An unverified ladder must never produce chains that
look as authoritative as a verified one — that is precisely how you get
plausible-and-wrong.

Usage:
    from bgate_core import causal
    specs = causal.load_specs(project_dir)
    chains = causal.chains_from_file("session.jsonl", specs["attack"])
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

SPEC_FILE = "causal_specs.json"


# --- event stream ----------------------------------------------------------


def read_events(path: str | os.PathLike[str]) -> list[dict]:
    """Parse a telemetry JSONL file into events, skipping unreadable lines.

    A telemetry file is appended live and flushed on a timer, so the LAST line
    of a session that crashed (or was killed by an autoquit mid-write) is
    routinely a partial JSON object. That is normal, not corruption — drop it
    and keep the rest rather than failing the whole read for one torn tail.
    """
    events: list[dict] = []
    p = Path(path)
    if not p.exists():
        return events
    with p.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "kind" in obj:
                events.append(obj)
    return events


# --- spec model ------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    """One gate in a resolution ladder.

    name:   the gate's identity in the emitted chain.
    reason: the `data.reason` value the game reports when THIS gate is the one
            that failed. None for the final success step, which names no reason.
    detail: telemetry fields to fold into the chain entry when present, so an
            entry reads `range_ok:distance=104,reach=115` instead of bare
            `range_ok`. Missing fields are skipped, never rendered as null.
    """
    name: str
    reason: Optional[str] = None
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChainSpec:
    """How to fold one game's flat event stream into causal chains.

    opens_on:   kinds that START an attempt.
    refusals:   kinds that END an attempt before it launched. Terminal alone —
                an action refused by an economy check never reaches a gate.
    terminals:  kinds that END a launched attempt.
    actor_key:  the `data` field identifying who acted, or "" for a
                single-actor game. Chains correlate per actor because
                independent actors interleave freely in one stream.
    ladder:     gates in RESOLUTION ORDER. The order is load-bearing.
    landed:     the terminal kind meaning full success (all gates passed).
    blocked_suffix: terminal kinds ending in this are treated as a failure of
                `blocked_gate` rather than as a `reason` string, for games that
                report a guard result as its own kind.
    aborted_reasons: reasons meaning the attempt never reached gate 1 at all.
    order_verified: whether a human confirmed `ladder` against the game's
                source. False for anything `infer_spec()` produced.
    """
    name: str
    opens_on: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    terminals: tuple[str, ...] = ()
    actor_key: str = ""
    ladder: tuple[Gate, ...] = ()
    landed: str = ""
    blocked_suffix: str = ""
    blocked_gate: str = ""
    aborted_reasons: tuple[str, ...] = ()
    consequences: tuple[str, ...] = ()
    consequence_window: float = 0.5
    order_verified: bool = False
    source: str = ""


def spec_from_dict(name: str, raw: dict) -> ChainSpec:
    """Build a ChainSpec from stored JSON. Unknown keys are ignored on purpose:
    a project file written against a newer version must not crash an older one."""
    return ChainSpec(
        name=name,
        opens_on=tuple(raw.get("opens_on", ())),
        refusals=tuple(raw.get("refusals", ())),
        terminals=tuple(raw.get("terminals", ())),
        actor_key=raw.get("actor_key", ""),
        ladder=tuple(
            Gate(g["gate"], g.get("fails_with_reason"),
                 tuple(g.get("detail_fields", ())))
            for g in raw.get("ladder", ())
        ),
        landed=raw.get("landed", ""),
        blocked_suffix=raw.get("blocked_suffix", ""),
        blocked_gate=raw.get("blocked_gate", ""),
        aborted_reasons=tuple(raw.get("aborted_reasons", ())),
        consequences=tuple(raw.get("consequences", ())),
        consequence_window=float(raw.get("consequence_window", 0.5)),
        order_verified=bool(raw.get("order_verified", False)),
        source=raw.get("source", ""),
    )


def spec_to_dict(spec: ChainSpec) -> dict:
    return {
        "opens_on": list(spec.opens_on),
        "refusals": list(spec.refusals),
        "terminals": list(spec.terminals),
        "actor_key": spec.actor_key,
        "ladder": [
            {"gate": g.name, "fails_with_reason": g.reason,
             "detail_fields": list(g.detail)}
            for g in spec.ladder
        ],
        "landed": spec.landed,
        "blocked_suffix": spec.blocked_suffix,
        "blocked_gate": spec.blocked_gate,
        "aborted_reasons": list(spec.aborted_reasons),
        "consequences": list(spec.consequences),
        "consequence_window": spec.consequence_window,
        "order_verified": spec.order_verified,
        "source": spec.source,
    }


def _spec_path(project_dir: str | os.PathLike[str]) -> Path:
    return Path(project_dir) / ".bgate" / SPEC_FILE


def load_specs(project_dir: str | os.PathLike[str]) -> dict[str, ChainSpec]:
    """Every chain spec this project defines. Empty when the file is absent —
    a project with no specs is the normal starting state, not an error."""
    path = _spec_path(project_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    specs = raw.get("specs", raw) if isinstance(raw, dict) else {}
    return {name: spec_from_dict(name, body)
            for name, body in specs.items() if isinstance(body, dict)}


def save_spec(project_dir: str | os.PathLike[str], spec: ChainSpec) -> dict:
    """Add or replace one spec, preserving the others."""
    path = _spec_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            existing = blob.get("specs", {}) if isinstance(blob, dict) else {}
        except json.JSONDecodeError:
            existing = {}
    existing[spec.name] = spec_to_dict(spec)
    path.write_text(json.dumps({"specs": existing}, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(path), "spec": spec.name,
            "specs": sorted(existing)}


def resolve_spec(spec: str | ChainSpec,
                 project_dir: Optional[str | os.PathLike[str]] = None) -> ChainSpec:
    if isinstance(spec, ChainSpec):
        return spec
    specs = load_specs(project_dir) if project_dir else {}
    if spec in specs:
        return specs[spec]
    known = sorted(specs) or ["<none defined>"]
    raise KeyError(
        f"no causal spec named {spec!r} in this project (have: {', '.join(known)}). "
        f"Run causal_infer_spec against a telemetry file to draft one.")


# --- chain construction ----------------------------------------------------


@dataclass
class Chain:
    spec: str
    actor: str
    move: str
    outcome: str
    failed_gate: Optional[str]
    t_start: float
    t_end: float
    order_verified: bool = True
    causal_chain: list[str] = field(default_factory=list)
    consequences: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {
            "spec": self.spec,
            "actor": self.actor,
            "move": self.move,
            "outcome": self.outcome,
            "failed_gate": self.failed_gate,
            "t_start": round(self.t_start, 3),
            "t_end": round(self.t_end, 3),
            "duration": round(self.t_end - self.t_start, 3),
            "causal_chain": self.causal_chain,
            "consequences": self.consequences,
            "order_verified": self.order_verified,
        }
        if not self.order_verified:
            out["warning"] = (
                "Gate order was inferred, not confirmed against the game's "
                "source. Entries marked '~' are ASSUMED to have passed; that "
                "assumption is only sound if the ladder order is right.")
        return out


def _t(event: dict) -> float:
    value = event.get("t")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _fmt(entry: str, event: dict, fields: Iterable[str], t: float) -> str:
    data = event.get("data") or {}
    parts = []
    for key in fields:
        if key in data and data[key] is not None:
            value = data[key]
            if isinstance(value, float):
                value = round(value, 2)
            parts.append(f"{key}={value}")
        # A field the game did not report is simply absent. Rendering it as
        # `reach=None` would look like the game measured a null reach.
    detail = ",".join(parts)
    return f"{entry}:{detail}@{t:.2f}" if detail else f"{entry}@{t:.2f}"


def _passed(spec: ChainSpec, gate: Gate, event: dict, t: float) -> str:
    """Render a gate the ladder says must have passed.

    Unverified ladders get a '~' so a reader can never mistake an assumption
    for an observation.
    """
    prefix = "" if spec.order_verified else "~"
    return _fmt(prefix + gate.name, event, gate.detail, t)


def _resolve(chain: Chain, spec: ChainSpec, terminal: dict) -> None:
    """Walk the ladder against a terminal and record PASS/FAIL per gate.

    This is where the inference lives: `reason` names the gate that failed, and
    every gate BEFORE it in resolution order is recorded as passed, because the
    game could not have reached the failing check otherwise.
    """
    kind = terminal.get("kind", "")
    data = terminal.get("data") or {}
    reason = data.get("reason")
    t = _t(terminal)

    if kind == spec.landed:
        for gate in spec.ladder:
            chain.causal_chain.append(_passed(spec, gate, terminal, t))
        chain.outcome = "landed"
        chain.failed_gate = None
        return

    # Some games report a guard result as its own KIND rather than a `reason`.
    if spec.blocked_suffix and kind.endswith(spec.blocked_suffix):
        target = spec.blocked_gate or (spec.ladder[-2].name
                                       if len(spec.ladder) > 1 else "")
        for gate in spec.ladder:
            if gate.name == target:
                chain.causal_chain.append(_fmt(f"{gate.name}:BLOCKED",
                                               terminal, (), t))
                break
            chain.causal_chain.append(_passed(spec, gate, terminal, t))
        chain.outcome = "blocked"
        chain.failed_gate = target
        return

    # Attempts that never reached a gate at all — the target was already gone,
    # or the round had ended. Attributing these to gate 1 would be a lie.
    if reason in spec.aborted_reasons:
        chain.causal_chain.append(_fmt(f"aborted:{reason}", terminal, (), t))
        chain.outcome = "aborted"
        chain.failed_gate = None
        return

    for gate in spec.ladder:
        if gate.reason is not None and gate.reason == reason:
            chain.causal_chain.append(
                _fmt(f"{gate.name}:FAIL", terminal, gate.detail, t))
            chain.outcome = "failed"
            chain.failed_gate = gate.name
            return
        chain.causal_chain.append(_passed(spec, gate, terminal, t))

    # An unrecognised reason. Report it rather than guessing — a new reason
    # added to the game should surface here as unknown, not be silently
    # attributed to whichever gate happens to sort last.
    chain.causal_chain.append(_fmt(f"unresolved:{reason}", terminal, (), t))
    chain.outcome = "unknown"
    chain.failed_gate = None


def build_chains(events: list[dict], spec: ChainSpec) -> list[Chain]:
    """Fold an event stream into per-actor causal chains.

    Open attempts are tracked per actor. A second `opens_on` for an actor that
    already has one open closes the first as `dropped` — real signal (an action
    that produced no resolution), not a parse error.
    """
    open_chains: dict[str, Chain] = {}
    done: list[Chain] = []

    def new_chain(actor: str, data: dict, t: float) -> Chain:
        return Chain(spec=spec.name, actor=actor,
                     move=str(data.get("type", "")) or "unknown",
                     outcome="open", failed_gate=None, t_start=t, t_end=t,
                     order_verified=spec.order_verified)

    for event in events:
        kind = event.get("kind", "")
        data = event.get("data") or {}
        t = _t(event)
        actor = (str(data.get(spec.actor_key, "")) or "unknown"
                 if spec.actor_key else "*")

        if kind in spec.opens_on:
            if actor in open_chains:
                stale = open_chains.pop(actor)
                stale.outcome = "dropped"
                stale.t_end = t
                stale.causal_chain.append(f"superseded@{t:.2f}")
                done.append(stale)
            chain = new_chain(actor, data, t)
            chain.events.append(event)
            chain.causal_chain.append(
                _fmt(f"attempt:{actor}.{chain.move}", event, ("cost",), t))
            open_chains[actor] = chain
            continue

        if kind in spec.refusals:
            # Refusals stand alone: the action never launched, so there is no
            # open chain to attach to and the refusal IS the whole story.
            chain = new_chain(actor, data, t)
            chain.outcome = "refused"
            chain.failed_gate = kind
            chain.events.append(event)
            why = data.get("reason")
            chain.causal_chain.append(f"attempt:{actor}.{chain.move}@{t:.2f}")
            chain.causal_chain.append(
                f"{kind}:FAIL{':' + str(why) if why else ''}@{t:.2f}")
            done.append(chain)
            continue

        if kind in spec.terminals:
            chain = open_chains.pop(actor, None)
            if chain is None:
                # A resolution with no recorded attempt: telemetry started
                # mid-action, or a test drove the resolver directly. Keep it as
                # a partial rather than dropping evidence on the floor.
                chain = new_chain(actor, data, t)
                chain.causal_chain.append(f"attempt:UNRECORDED@{t:.2f}")
            chain.events.append(event)
            chain.t_end = t
            _resolve(chain, spec, event)
            done.append(chain)
            continue

        if kind in spec.consequences:
            # Attach to the most recent resolved chain for this actor, within
            # the window. A knockout right after a landed hit was caused by it;
            # one thirty seconds later was not.
            for chain in reversed(done):
                if chain.actor != actor or chain.outcome == "refused":
                    continue
                if t - chain.t_end <= spec.consequence_window:
                    chain.consequences.append(f"{kind}@{t:.2f}")
                break

    for chain in open_chains.values():
        chain.outcome = "unresolved"
        chain.causal_chain.append("no_resolution_recorded")
        done.append(chain)

    done.sort(key=lambda c: c.t_start)
    return done


def chains_from_file(path: str | os.PathLike[str], spec: str | ChainSpec,
                     project_dir: Optional[str | os.PathLike[str]] = None
                     ) -> list[dict]:
    """Read a telemetry file and return §8-shaped chains as plain dicts."""
    resolved = resolve_spec(spec, project_dir)
    return [c.to_dict() for c in build_chains(read_events(path), resolved)]


# --- inference -------------------------------------------------------------


def _families(events: list[dict]) -> dict[str, list[str]]:
    """Group event kinds by leading underscore-separated token.

    `telemetry_contract()` asks for short names like 'jump' / 'level_load', and
    games overwhelmingly name a pipeline's stages with a shared prefix. That
    convention is the only structural signal available without knowing the game,
    so it is what clustering keys on — and why inference is a DRAFT, not truth.
    """
    families: dict[str, list[str]] = {}
    for kind in sorted({e.get("kind", "") for e in events}):
        if not kind or "_" not in kind:
            continue
        families.setdefault(kind.split("_", 1)[0], []).append(kind)
    return {k: v for k, v in families.items() if len(v) > 1}


def _actor_key(events: list[dict], kinds: set[str]) -> str:
    """Find a low-cardinality field present across a family — the actor.

    Low cardinality is the tell: an actor field takes a handful of values
    ("player"/"opponent") across many events, where a measurement field takes
    nearly as many distinct values as there are events.
    """
    counts: dict[str, set] = {}
    total = 0
    for event in events:
        if event.get("kind") not in kinds:
            continue
        total += 1
        for key, value in (event.get("data") or {}).items():
            if isinstance(value, (str, int, bool)):
                counts.setdefault(key, set()).add(value)
    best, best_n = "", 0
    for key, values in counts.items():
        if not (1 <= len(values) <= 8):
            continue
        seen = sum(1 for e in events
                   if e.get("kind") in kinds and key in (e.get("data") or {}))
        if seen >= total * 0.8 and seen > best_n:
            best, best_n = key, seen
    return best


def infer_spec(events: list[dict], name: str = "",
               family: str = "") -> dict:
    """Draft a ChainSpec from an unseen telemetry stream.

    Returns {spec, notes, candidates} — a DRAFT, never authoritative. What is
    genuinely inferable: which kinds form a pipeline (shared prefix), which is
    the opener (precedes others), which are terminals, which field is the
    actor, and which `reason` values occur. What is NOT inferable at any sample
    size: the ORDER of the gates. Order lives in the game's source, and the
    whole PASS inference rests on it — so the draft comes back
    `order_verified: false` and must be reviewed against the code before its
    chains are trusted.
    """
    families = _families(events)
    if not families:
        return {"ok": False, "error": "no multi-kind event families found — "
                                      "nothing here looks like a pipeline",
                "kinds": sorted({e.get("kind", "") for e in events})}

    if not family:
        family = max(families, key=lambda f: sum(
            1 for e in events if e.get("kind") in families[f]))
    if family not in families:
        return {"ok": False, "error": f"no family {family!r}",
                "candidates": sorted(families)}

    kinds = set(families[family])
    ordered = [e for e in events if e.get("kind") in kinds]

    # The opener is the kind that most often comes FIRST in a burst.
    firsts: dict[str, int] = {}
    prev_t = -999.0
    for event in ordered:
        if _t(event) - prev_t > 0.5:
            firsts[event.get("kind", "")] = firsts.get(event.get("kind", ""), 0) + 1
        prev_t = _t(event)
    opener = max(firsts, key=firsts.get) if firsts else sorted(kinds)[0]

    actor_key = _actor_key(events, kinds)
    reasons = sorted({(e.get("data") or {}).get("reason")
                      for e in ordered
                      if (e.get("data") or {}).get("reason")})
    terminals = sorted(kinds - {opener})

    # A landed/success terminal is the one that never carries a reason.
    with_reason = {e.get("kind") for e in ordered
                   if (e.get("data") or {}).get("reason")}
    success = [k for k in terminals if k not in with_reason]
    landed = success[0] if success else ""

    ladder = tuple(Gate(f"{r}_ok", r, ()) for r in reasons)
    if landed:
        ladder = ladder + (Gate("succeeded", None, ()),)

    spec = ChainSpec(
        name=name or family,
        opens_on=(opener,),
        terminals=tuple(terminals),
        actor_key=actor_key,
        ladder=ladder,
        landed=landed,
        aborted_reasons=(),
        order_verified=False,
        source="inferred from telemetry — ORDER UNCONFIRMED",
    )
    return {
        "ok": True,
        "spec": {spec.name: spec_to_dict(spec)},
        "candidates": sorted(families),
        "observed": {"kinds": sorted(kinds), "reasons": reasons,
                     "events": len(ordered)},
        "notes": [
            f"Opener guessed as {opener!r} (started {firsts.get(opener, 0)} bursts).",
            (f"Actor field guessed as {actor_key!r}."
             if actor_key else "No actor field found — treated as single-actor."),
            # Coverage, not just count. A sample in which nothing ever failed
            # yields an EMPTY ladder, and a draft that looks well-formed while
            # describing no gates at all is worse than an obvious error.
            (f"NO FAILURES IN THIS SAMPLE: every {family!r} event succeeded, so "
             f"no gates could be drafted at all. Gates are derived from the "
             f"`reason` values on failed attempts — capture a session that "
             f"contains failures and infer again, or write the ladder by hand."
             if not reasons else
             f"Ladder drafted from {len(reasons)} observed reason(s), IN "
             f"ALPHABETICAL ORDER, which is almost certainly not the game's "
             f"resolution order."),
            "REVIEW REQUIRED: open the game's resolution function, put the "
            "gates in the order it checks them, add each gate's detail fields, "
            "then set order_verified=true. Until then every passed gate renders "
            "with a '~' and chains carry a warning.",
        ],
        "coverage": {
            "reasons_observed": len(reasons),
            "sufficient": bool(reasons),
        },
    }


# --- aggregate -------------------------------------------------------------


def summarize(chains: list[dict] | list[Chain]) -> dict:
    """Roll chains up into the counts a tuning question actually asks.

    "Why do so many attempts fail?" is answered by `by_failed_gate`, not by a
    filmstrip: a spike on one gate is a different bug from a spike on another,
    and raw telemetry cannot tell them apart without reading JSONL by eye.
    """
    rows = [c.to_dict() if isinstance(c, Chain) else c for c in chains]
    by_outcome: dict[str, int] = {}
    by_gate: dict[str, int] = {}
    by_actor: dict[str, dict[str, int]] = {}
    by_move: dict[str, dict[str, int]] = {}

    for row in rows:
        outcome = row.get("outcome", "unknown")
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
        gate = row.get("failed_gate")
        if gate:
            by_gate[gate] = by_gate.get(gate, 0) + 1
        actor = row.get("actor", "unknown")
        by_actor.setdefault(actor, {})
        by_actor[actor][outcome] = by_actor[actor].get(outcome, 0) + 1
        move = row.get("move", "unknown")
        by_move.setdefault(move, {})
        by_move[move][outcome] = by_move[move].get(outcome, 0) + 1

    landed = by_outcome.get("landed", 0)
    # Success rate is over RESOLVED attempts. Counting refusals in the
    # denominator would make an economy problem look like an accuracy problem.
    resolved = sum(by_outcome.get(k, 0)
                   for k in ("landed", "blocked", "failed"))
    out = {
        "total": len(rows),
        "landed": landed,
        "success_rate": round(landed / resolved, 3) if resolved else None,
        "by_outcome": dict(sorted(by_outcome.items(), key=lambda kv: -kv[1])),
        "by_failed_gate": dict(sorted(by_gate.items(), key=lambda kv: -kv[1])),
        "by_actor": by_actor,
        "by_move": by_move,
    }
    if rows and not all(r.get("order_verified", True) for r in rows):
        out["warning"] = ("Built from an unverified gate order — "
                          "by_failed_gate is reliable, passed gates are not.")
    return out


def find(chains: list[dict], *, actor: Optional[str] = None,
         outcome: Optional[str] = None, failed_gate: Optional[str] = None,
         move: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Filter chains — the 'show me the ones that failed on gate X' query."""
    out = []
    for row in chains:
        if actor and row.get("actor") != actor:
            continue
        if outcome and row.get("outcome") != outcome:
            continue
        if failed_gate and row.get("failed_gate") != failed_gate:
            continue
        if move and row.get("move") != move:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def describe_spec(spec: ChainSpec) -> dict:
    """What ladder is being asserted, and whether anyone confirmed it.

    Exposed because the PASS inferences are only as sound as this ladder. An
    agent reading a chain should be able to see the assumption it rests on.
    """
    return {
        "name": spec.name,
        "source": spec.source,
        "order_verified": spec.order_verified,
        "actor_key": spec.actor_key or "(single-actor)",
        "opens_on": list(spec.opens_on),
        "refusals": list(spec.refusals),
        "terminals": list(spec.terminals),
        "landed": spec.landed,
        "ladder": [
            {"gate": g.name, "fails_with_reason": g.reason,
             "detail_fields": list(g.detail)}
            for g in spec.ladder
        ],
        "inference": "A terminal naming gate N as failed implies gates 1..N-1 "
                     "passed, because the game could not reach N otherwise. "
                     "Sound only while this ladder matches the game's real "
                     "resolution order — which is why order_verified exists.",
    }
