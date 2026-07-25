"""Causal chains over telemetry that already exists — DESIGN.md §8, no engine.

An event that carries the gates it passed beats any log line. `punch_whiffed`
with `reason: "facing"` tells you the attack failed the facing gate; it does not
tell you the range gate *passed*, which is the fact that turns "it whiffed" into
"it was in range and pointed the wrong way." That second fact is the whole
diagnostic value, and today it is thrown away.

It does not have to be. **The gates run in a fixed order, so the losing gate
implies every gate before it passed.** `fight.gd` resolves an attack as:

    range -> facing -> elevation -> guard -> damage

so `reason: "elevation"` means range and facing both passed, necessarily, or
control would never have reached the elevation check. One string reconstructs the
entire ladder. That is why this module needs no change to the game, no second
runtime, and no new store — it reads the JSONL the shipped `BGateTelemetry`
autoload already writes and rebuilds §8-shaped chains from event ORDER plus the
ladder declared below.

What this deliberately is NOT: a simulator. It never re-derives what happened. It
reads what the game reported and makes the implications explicit. When a chain
says `range_ok:PASS`, that is an inference from the gate order — sound only while
the ladder here matches the game's resolution order, which is why `GateLadder`
is declared as data with the source line it mirrors, not scattered through code.
If someone reorders the gates in `fight.gd`, this file is the one place to fix,
and `tests/test_causal.py` fails loudly rather than silently producing plausible
nonsense.

Usage:
    from bgate_core import causal
    chains = causal.chains_from_file("session.jsonl")
    print(causal.summarize(chains)["by_failed_gate"])
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# --- event stream ----------------------------------------------------------


def read_events(path: str | os.PathLike[str]) -> list[dict]:
    """Parse a BGateTelemetry JSONL file into events, skipping unreadable lines.

    A telemetry file is appended to live and flushed on a timer, so the LAST
    line of a session that crashed (or was killed by BGATE_AUTOQUIT mid-write)
    is routinely a partial JSON object. That is normal, not corruption — drop it
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


# --- gate ladders ----------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    """One gate in a resolution ladder.

    name:    the gate's identity in the emitted chain.
    reason:  the `data.reason` value the game reports when THIS gate is the one
             that failed. None for gates that never name themselves (the final
             success step).
    detail:  telemetry fields to fold into the chain entry when present, so a
             chain reads `range_ok:dist=104.0,reach=115.0` instead of bare
             `range_ok`. Missing fields are skipped, never rendered as null.
    """
    name: str
    reason: Optional[str] = None
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChainSpec:
    """How to fold a flat event stream into causal chains.

    opens_on:   kinds that START an attempt.
    refusals:   kinds that END an attempt before it ever launched (the economy
                said no). These are terminal on their own — an attack refused by
                cooldown never reaches a gate.
    terminals:  kinds that END a launched attempt.
    actor_key:  the field in `data` identifying who acted. Chains are correlated
                per actor, because two fighters resolve attacks independently and
                interleave freely in one stream.
    ladder:     gates in RESOLUTION ORDER. Order is the load-bearing part.
    landed:     the terminal kind meaning full success (all gates passed).
    consequences: kinds that, when they follow a terminal for the same actor
                within `consequence_window`, are appended as downstream effects.
    """
    name: str
    opens_on: tuple[str, ...]
    refusals: tuple[str, ...]
    terminals: tuple[str, ...]
    actor_key: str
    ladder: tuple[Gate, ...]
    landed: str
    consequences: tuple[str, ...] = ()
    consequence_window: float = 0.5
    source: str = ""


# The fighter's attack pipeline, mirroring the real resolution order in
# haymaker/game/scripts/fight.gd `_on_player_punch` (~line 749) and its opponent
# twin `_on_opponent_strike` (~line 879). Both gate identically; the opponent
# path has no elevation-vs-block reordering.
#
# Ladder order is copied from the source, NOT guessed:
#   fight.gd:760  range     -> reason "range"
#   fight.gd:779  facing    -> reason "facing"      (_facing_ok)
#   fight.gd:793  elevation -> reason "elevation"   (_elevation_ok)
#   fight.gd:798  guard     -> punch_blocked, or reason "ducked" via take_hit
#   fight.gd:809  damage    -> punch_landed
FIGHTER_ATTACK = ChainSpec(
    name="fighter_attack",
    opens_on=("punch_thrown",),
    refusals=("punch_cooldown", "punch_gassed"),
    terminals=("punch_landed", "punch_blocked", "punch_whiffed"),
    actor_key="by",
    ladder=(
        Gate("range_ok", "range", ("distance", "reach")),
        Gate("facing_ok", "facing", ("distance",)),
        Gate("elevation_ok", "elevation", ("dy",)),
        Gate("guard_ok", "ducked", ()),
        Gate("damage_applied", None, ("damage", "counter", "target_hp")),
    ),
    landed="punch_landed",
    consequences=("gassed", "stagger", "combo", "combo_break", "stage_lost", "ko"),
    source="haymaker/game/scripts/fight.gd:749,879",
)

# The projectile pipeline. Same shape, shorter ladder — a fireball has no facing
# gate (it travels), so reading it through FIGHTER_ATTACK would assert a
# facing_ok that the game never checked.
FIREBALL = ChainSpec(
    name="fireball",
    opens_on=("fireball_thrown", "fireball_cast"),
    refusals=(),
    terminals=("fireball_landed", "fireball_blocked", "fireball_whiffed",
               "fireball_despawned"),
    actor_key="by",
    ladder=(
        Gate("guard_ok", "ducked", ()),
        Gate("damage_applied", None, ("damage", "target_hp")),
    ),
    landed="fireball_landed",
    consequences=("stage_lost", "ko"),
    source="haymaker/game/scripts/fight.gd:976,1029",
)

SPECS: dict[str, ChainSpec] = {s.name: s for s in (FIGHTER_ATTACK, FIREBALL)}


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
    causal_chain: list[str] = field(default_factory=list)
    consequences: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
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
        }


def _t(event: dict) -> float:
    value = event.get("t")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _fmt(entry: str, event: dict, fields: Iterable[str], t: float) -> str:
    """Render one chain entry with whichever detail fields the game reported."""
    data = event.get("data") or {}
    parts = []
    for key in fields:
        if key in data and data[key] is not None:
            value = data[key]
            if isinstance(value, float):
                value = round(value, 2)
            parts.append(f"{key}={value}")
        # A field the game did not report is simply absent. Rendering it as
        # `reach=None` would look like the engine measured a null reach.
    detail = ",".join(parts)
    return f"{entry}:{detail}@{t:.2f}" if detail else f"{entry}@{t:.2f}"


def _resolve(chain: Chain, spec: ChainSpec, terminal: dict) -> None:
    """Walk the ladder against a terminal event and record PASS/FAIL per gate.

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
            chain.causal_chain.append(_fmt(gate.name, terminal, gate.detail, t))
        chain.outcome = "landed"
        chain.failed_gate = None
        return

    # A block is a guard-gate failure that the game reports as its own kind
    # rather than as a `reason`, so it is matched by kind, not by string.
    if kind.endswith("_blocked"):
        for gate in spec.ladder:
            if gate.name == "guard_ok":
                chain.causal_chain.append(_fmt("guard_ok:BLOCKED", terminal, (), t))
                break
            chain.causal_chain.append(_fmt(gate.name, terminal, gate.detail, t))
        chain.outcome = "blocked"
        chain.failed_gate = "guard_ok"
        return

    # Whiffs that never reached a gate at all — the target was already down, or
    # the fight had ended. Recording these as a range failure would be a lie.
    if reason in ("target_ko", "fight_over"):
        chain.causal_chain.append(_fmt(f"aborted:{reason}", terminal, (), t))
        chain.outcome = "aborted"
        chain.failed_gate = None
        return

    for gate in spec.ladder:
        if gate.reason is not None and gate.reason == reason:
            chain.causal_chain.append(
                _fmt(f"{gate.name}:FAIL", terminal, gate.detail, t))
            chain.outcome = "ducked" if reason == "ducked" else "whiffed"
            chain.failed_gate = gate.name
            return
        chain.causal_chain.append(_fmt(gate.name, terminal, gate.detail, t))

    # An unrecognised reason. Report it rather than guessing a gate — a new
    # whiff reason added to the game should surface here as unknown, not be
    # silently attributed to whichever gate happens to sort last.
    chain.causal_chain.append(_fmt(f"unresolved:{reason}", terminal, (), t))
    chain.outcome = "unknown"
    chain.failed_gate = None


def build_chains(events: list[dict], spec: ChainSpec = FIGHTER_ATTACK) -> list[Chain]:
    """Fold an event stream into per-actor causal chains.

    Open attempts are tracked per actor. A second `opens_on` for an actor that
    already has one open closes the first as `dropped` — that is a real signal
    (an attack that produced no resolution at all), not a parse error, and it is
    exactly what a combo cancel looks like from the telemetry side.
    """
    open_chains: dict[str, Chain] = {}
    done: list[Chain] = []

    for event in events:
        kind = event.get("kind", "")
        data = event.get("data") or {}
        t = _t(event)
        actor = str(data.get(spec.actor_key, "")) or "unknown"

        if kind in spec.opens_on:
            if actor in open_chains:
                stale = open_chains.pop(actor)
                stale.outcome = "dropped"
                stale.t_end = t
                stale.causal_chain.append(f"superseded@{t:.2f}")
                done.append(stale)
            chain = Chain(
                spec=spec.name, actor=actor,
                move=str(data.get("type", "")) or "unknown",
                outcome="open", failed_gate=None, t_start=t, t_end=t,
                events=[event],
            )
            chain.causal_chain.append(_fmt(f"attempt:{actor}.{chain.move}",
                                           event, ("stamina_cost",), t))
            open_chains[actor] = chain
            continue

        if kind in spec.refusals:
            # Refusals stand alone: the attack never launched, so there is no
            # open chain to attach to and the economy IS the whole story.
            chain = Chain(
                spec=spec.name, actor=actor,
                move=str(data.get("type", "")) or "unknown",
                outcome="refused", failed_gate=kind, t_start=t, t_end=t,
                events=[event],
            )
            label = kind.replace("punch_", "")
            why = data.get("reason")
            chain.causal_chain.append(
                f"attempt:{actor}.{chain.move}@{t:.2f}")
            chain.causal_chain.append(
                f"{label}:FAIL{':' + str(why) if why else ''}@{t:.2f}")
            done.append(chain)
            continue

        if kind in spec.terminals:
            chain = open_chains.pop(actor, None)
            if chain is None:
                # A resolution with no recorded attempt. Happens when telemetry
                # started mid-attack, or when a test drives the resolver
                # directly (fight.gd's handlers are public entry points and its
                # comments note tests call them). Keep it as a partial rather
                # than dropping evidence on the floor.
                chain = Chain(
                    spec=spec.name, actor=actor,
                    move=str(data.get("type", "")) or "unknown",
                    outcome="open", failed_gate=None, t_start=t, t_end=t,
                    events=[],
                )
                chain.causal_chain.append(f"attempt:UNRECORDED@{t:.2f}")
            chain.events.append(event)
            chain.t_end = t
            _resolve(chain, spec, event)
            done.append(chain)
            continue

        if kind in spec.consequences:
            # Attach to the most recent resolved chain for this actor, within
            # the window. A `ko` half a second after a landed hook is caused by
            # it; a `ko` thirty seconds later is not.
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


def chains_from_file(path: str | os.PathLike[str],
                     spec: str | ChainSpec = FIGHTER_ATTACK) -> list[dict]:
    """Read a telemetry file and return §8-shaped chains as plain dicts."""
    resolved = SPECS[spec] if isinstance(spec, str) else spec
    return [c.to_dict() for c in build_chains(read_events(path), resolved)]


# --- aggregate -------------------------------------------------------------


def summarize(chains: list[dict] | list[Chain]) -> dict:
    """Roll chains up into the counts a tuning question actually asks.

    "Why do so many attacks whiff?" is answered by `by_failed_gate`, not by a
    filmstrip. A facing-gate spike is a different bug from a range-gate spike,
    and before this module the telemetry could not tell them apart without
    someone reading raw JSONL by eye.
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

    total = len(rows)
    landed = by_outcome.get("landed", 0)
    resolved = sum(by_outcome.get(k, 0) for k in
                   ("landed", "blocked", "whiffed", "ducked"))
    return {
        "total": total,
        "landed": landed,
        # Hit rate is over RESOLVED attacks. Counting refusals in the
        # denominator would make an economy problem look like an aim problem.
        "hit_rate": round(landed / resolved, 3) if resolved else None,
        "by_outcome": dict(sorted(by_outcome.items(), key=lambda kv: -kv[1])),
        "by_failed_gate": dict(sorted(by_gate.items(), key=lambda kv: -kv[1])),
        "by_actor": by_actor,
        "by_move": by_move,
    }


def find(chains: list[dict], *, actor: Optional[str] = None,
         outcome: Optional[str] = None, failed_gate: Optional[str] = None,
         move: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Filter chains — the 'show me the ones that failed on facing' query."""
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


def describe_spec(spec: str | ChainSpec = FIGHTER_ATTACK) -> dict:
    """What ladder is being asserted, and which source lines it mirrors.

    Exposed because the PASS inferences are only as sound as this ladder. An
    agent reading a chain should be able to see the assumption it rests on.
    """
    resolved = SPECS[spec] if isinstance(spec, str) else spec
    return {
        "name": resolved.name,
        "source": resolved.source,
        "opens_on": list(resolved.opens_on),
        "refusals": list(resolved.refusals),
        "terminals": list(resolved.terminals),
        "ladder": [
            {"gate": g.name, "fails_with_reason": g.reason,
             "detail_fields": list(g.detail)}
            for g in resolved.ladder
        ],
        "inference": "A terminal naming gate N as failed implies gates 1..N-1 "
                     "passed, because the game could not reach N otherwise. "
                     "Sound only while this ladder matches the source order.",
    }
