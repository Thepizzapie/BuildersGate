# `bgate_engine` — agent-native engine (design phase)

The typed, deterministic spine Builders Gate renders instead of a path pile.
See **[DESIGN.md](DESIGN.md)** for the full architecture, decisions, and build order.

**Status:** schemas + design only — no runtime code yet (by decision, 2026-07-19).

## Locked decisions
- **Python engine is source of truth**; Godot renders engine-produced replays (it does
  not re-simulate for proofs → cross-runtime float parity is off the critical path).
- Design + schemas ship first for review, then build in the order in DESIGN.md §14.1.

## Schemas (`schemas/`)
| File | What it types |
|---|---|
| `component_defs.json` | Canonical components (Transform2D, Movement, Fighter, Health, Hitbox, StateMachine, SpriteRig) — derived field-by-field from the real `boxer.gd`/`fight.gd`, with units + semantics. |
| `authored_intent.schema.json` | Design requirements (ranges, priority, owner) — the top of the three representations. |
| `transaction.schema.json` | Isolated change sets; component-level conflict detection replacing file locks. |
| `run_identifier.schema.json` | The tuple that makes any run bit-replayable + named RNG streams + input schedule. |
| `query_response.schema.json` | Semantic `world.query` results with the entity dependency map. |
| `evidence_manifest.schema.json` | Per-tick structured render evidence (entity-id / collision / ui-layout buffers). |
| `protocol.commands.json` | The discoverable MCP command surface + capabilities + the critical-demo trace. |

## The unit of work
```
Observe → Hypothesize → Patch → Simulate → Assert → Commit
```

## First proof (critical demo)
Blind-side hit: query the causal chain → isolate a CombatSystem transaction → engine
names affected tests → replay the exact failing run → assert the blind-side collision
is gone → render before/after evidence → commit without touching unrelated components.
