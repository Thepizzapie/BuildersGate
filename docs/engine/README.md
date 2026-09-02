# `bgate_engine` — an agent-native engine, proposed

A design note with schemas attached. **Not shipped, not accepted, not imported.**

This directory is `DESIGN.md`, this file, and seven JSON files. There is no
runtime code. Nothing in the repository imports `bgate_engine` — the only
references are packaging (`pyproject.toml` ships the schemas as data),
`tests/packaging/test_packaging.py` asserting they reach the wheel, and CI checking the
same.

**Status:** proposal, derived from one game. Every schema was reverse-engineered
field-by-field from a single title — *Commodity Brawler*, an arcade fighter — and
its `boxer.gd` / `fight.gd`. That is one data point behind a large architectural
commitment: a **second authoritative simulation model**, in Python, with Godot
demoted to a playback surface. Read **[DESIGN.md](DESIGN.md)** for the full
architecture, and **§16** before doing anything with it.

> **2026-07-25 — that commitment is now withdrawn.** The §16.4 experiment ran
> against two other titles and both failed. The canonical-world + authoritative-
> simulation claim is retired; the framing layer survives. See the section at the
> bottom of this file, and **DESIGN.md §16.5** for the evidence. A same-day
> amendment moving the sim runtime to Rust (`bgate-sim`) was **withdrawn with it
> and never implemented** — no crate exists.

## What is actually general, and what is one fighter

| File | What it types | Holds for a second game? |
|---|---|---|
| `authored_intent.schema.json` | Design requirements — acceptance ranges, priority, owner. | Yes. Its `owner` enum is tied to Builders Gate's eight seats, not to the fighter. |
| `transaction.schema.json` | Isolated change sets; field-level conflict detection instead of file locks. | Yes. Cosmetic leaks only (`id` pattern, `seat` enum). |
| `query_response.schema.json` | Semantic `world.query` results — the entity dependency map. | Yes. |
| `protocol.commands.json` | The discoverable command surface + capabilities. | Yes. `critical_demo_trace` is a boxer demo script by design. |
| `evidence_manifest.schema.json` | Per-tick structured render evidence (entity-id / collision / ui-layout buffers). | Mostly. Screen-space only, closed buffer enum — thin for 3D. |
| `run_identifier.schema.json` | The tuple that makes a run bit-replayable, + named RNG streams. | Almost. `tick_rate` is `"const": 60`, which is the fighter's `TICK = 1/60` written into the schema as a universal law. A 30Hz game is invalid against it. |
| `component_defs.json` | "Canonical" components: Transform2D, Movement, Fighter, Health, Hitbox, StateMachine, SpriteRig. | **No.** `Health` is generic; the rest is one fighting game — a 640×360 stage, boxing-ring bounds, jab/hook/kick reach tables, a stamina-and-gassed economy, and runtime counters mirroring `boxer.gd`'s `save_state()` exactly. |

The framing layer is plausibly general. The **world model** — the part that
decides what a game *is*, and the part the whole "canonical world" claim rests on
— is a single fighter.

## Locked decisions (proposed 2026-07-19, never ratified)
- **Python engine as source of truth**; Godot renders engine-produced replays and
  does not re-simulate for proofs, which takes cross-runtime float parity off the
  critical path. This is tractable *because* the source title was already
  rollback-disciplined; it is a rewrite-per-game for one that is not.
- Design + schemas ship first for review, then build in the order in DESIGN.md
  §14.1. The first half happened; the review did not.

## The unit of work
```
Observe → Hypothesize → Patch → Simulate → Assert → Commit
```

## The experiment ran. It came back negative. — 2026-07-25

§16.4 asked for a second game expressed against `component_defs.json` and
`run_identifier.schema.json` unchanged. Two were used: **tommy-tomato** (2D melee
soulslike, custom canvas engine, and the *fair* test — it already calls its module
`sim/`) and **tomato-strike** (3D tactical FPS). Both predate this document.

Both fail, and not on components — **neither game has a tick.** tommy-tomato runs
`requestAnimationFrame` with a clamped wall-clock delta (`Game.ts:537`);
tomato-strike's authority is `hostTick(state, inputs, dt)` advancing `state.now +=
dt * 1000` (`sim.ts:203`). Neither seeds its randomness: tommy-tomato draws its own
run seed from unseeded `Math.random()` (`Game.ts:2208`). So `run_identifier`'s
premise — a tuple that identifies a bit-replayable run — has nothing to attach to,
and `tick_rate: "const": 60` is the least of its problems.

The decisive part is *why*. Both titles independently chose **host-authoritative
netcode with snapshot interpolation** — the architecture you pick precisely so you
do not need determinism. That is not an oversight; it is a sound decision, made
twice. Which inverts §1: the engine was derived from the one title that already
satisfied its hardest precondition. Of three titles the deterministic one is 1 of 3
and the oldest. **The engine does not supply determinism — it requires it, per
game, and that is where the cost actually lives.**

**Outcome: §16.4's second branch.** Retire the canonical-world + authoritative-
simulation claim. Keep the framing layer (intent, transactions, queries, protocol,
evidence) — it survived both titles unchanged. Demote `component_defs.json` to one
worked example for one game. Salvage **causal chains** (§8) and **structured visual
evidence** (§9) onto the shipped `godot_run` / `godot_screenshot` path: no store, no
second runtime, and they improve the existing pipeline now.

Full evidence, per file and per game, in **DESIGN.md §16.5**.
