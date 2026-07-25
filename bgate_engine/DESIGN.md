# Builders Gate — Agent-Native Engine (`bgate_engine`)

**Status:** design proposal, pre-implementation. No runtime code yet.
**Decisions locked (2026-07-19):**
- **Python engine is the source of truth.** The canonical world + the deterministic
  simulation live in `bgate_engine`. Godot becomes a **renderer / playback surface**
  driven by engine-produced state and replays — it does not re-simulate for proofs.
- **Ship the design + schemas first** (this document) for review before any runtime code.

---

## 0. Why this exists

Everything Builders Gate does today renders an **unstructured pile**: artifacts are
paths, prompts, and free-text work notes. Nothing underneath is typed, queryable, or
provable. Every UI is therefore a best-effort view over data with no spine — which is
why the dashboard keeps feeling surface-level no matter how it's styled.

The engine is that spine. It makes the game a **database + simulator + debugger +
renderer** behind one protocol. Agents interact with explicit state and verified
operations; humans interact through visual tools built on the *same* API.

Five defining properties:

1. **Everything is addressable** — every entity, component, asset, test, transaction,
   simulation, tick, and event has a stable id.
2. **Every mutation is typed and transactional** — no untyped file edits; changes are
   validated operations against a schema, isolated in a change set.
3. **Every simulation is reproducible** — `{world_revision, runtime_version, scene,
   seed, tick_rate, input_schedule_hash, asset_manifest_hash}` replays bit-for-bit.
4. **Every result is inspectable** — state and events are retained per tick; you can
   time-travel and diff.
5. **Every claim produces evidence** — an assertion links to the exact replay, ticks,
   causal chain, and render buffers that prove it.

The unit of game development stops being "edit this file" and becomes the loop:

```
Observe → Hypothesize → Patch → Simulate → Assert → Commit
```

That loop is the engine's primary workflow.

---

## 1. This is not a generic toy — it describes *Commodity Brawler*

The test game (`../haymaker`, "Commodity Brawler") already made the hard commitment:
its combat is a **fixed-step deterministic simulation** built for rollback netcode.
`game/scripts/boxer.gd` is explicit about it:

- `sim_tick()` — the whole per-tick step, takes **no delta**, no tweens, no rendering.
  Calling it N times from a starting state always lands on the same result.
- `TICK = 1/60`; tunables authored in seconds are converted to **integer tick counts**
  at the moment an action starts (rollback-resim discipline).
- `save_state()` / `load_state()` — a complete runtime snapshot dictionary.
- Events already flow through `_emit(kind, data)` → `BGateTelemetry`.
- Hit resolution (`game/scripts/fight.gd`) resolves **by state**, gated on range and
  **facing** — `_facing_ok(attacker_x, facing, target_x)` is literally the blind-side
  gate the critical demo targets.

So the engine's job is not to invent an ECS for a toy. It is to **lift the semantics
already encoded in GDScript into typed, queryable canonical state**, reimplement
`sim_tick` as the authoritative Python runtime, and let Godot render what the engine
produces. The schemas below are derived field-by-field from `boxer.gd`/`fight.gd`.

---

## 2. Three representations

The engine keeps **authored intent**, **canonical world**, and **runtime state**
strictly separate. Keeping them apart is what lets the engine say *"the requirement
says 68–76 px; parameters produce 91 px; `tx_84` introduced the regression."*

```
Authored intent      design constraints, ranges, priority, owner
      ↓
Canonical world      what the game IS — entities, components, revisions
      ↓
Runtime state        what HAPPENED in one deterministic run — per tick
```

### 2.1 Authored intent (a *requirement*)
Grounded in the real jump the playtester dialed in on the F1 panel
(`jump_velocity -172.80`, `jump_gravity 105.75`, peak ≈ 141 px):

```json
{
  "id": "movement.jump_profile",
  "requirements": {
    "peak_height_px": [130, 152],
    "air_time_s":     [1.5, 1.75]
  },
  "priority": "ship",
  "owner": "gameplay",
  "verified_by": ["test.jump_profile"]
}
```

### 2.2 Canonical world (what the game *is*)
Derived from `boxer.gd`'s exported tunables. A fighter entity at some revision:

```json
{
  "entity": "fighter.tommy",
  "revision": 43,
  "components": {
    "Movement":  { "walk_speed": 62.0, "jump_velocity": -172.80,
                   "jump_gravity": 105.75, "ring_min_x": 90.0, "ring_max_x": 550.0 },
    "Fighter":   { "jab_damage": 4.0, "jab_reach": 115.0, "jab_cost": 10.0,
                   "hook_damage": 10.0, "hook_reach": 135.0,
                   "repeat_fatigue_scale": 2.4, "gassed_stun_ticks": 40 },
    "Health":    { "max_hp": 100.0 }
  }
}
```

### 2.3 Runtime state (what *happened*, one tick of one run)
This is `save_state()`, addressed by `(sim_id, tick, entity)`:

```json
{
  "sim": "sim_91", "tick": 212, "entity": "fighter.tommy",
  "state": {
    "hp": 92.0, "stamina": 71.3, "state_machine": "punch",
    "position": [144.0, 266.3], "facing": 1,
    "busy_elapsed_ticks": 6, "busy_contact_fired": true
  }
}
```

---

## 3. Engine layers

```
┌──────────────────────────────────────────────┐
│ Human editor · Agents · CI · Builders Gate    │
├──────────────────────────────────────────────┤
│ Agent Protocol & Capability Layer (MCP)       │
├──────────────────────────────────────────────┤
│ Transactions · Queries · Tests · Evidence     │
├──────────────────────────────────────────────┤
│ Canonical World & Asset Graph  (SQLite)       │
├──────────────────────────────────────────────┤
│ Deterministic Simulation Runtime  (Python)    │
├──────────────────────────────────────────────┤
│ Render / Audio / Platform Adapters  (Godot)   │
└──────────────────────────────────────────────┘
```

Where each layer lives in the repo:

| Layer | Home | Notes |
|---|---|---|
| Protocol & capability | `bgate_mcp/` (new engine tools) | same MCP server the seats already use |
| Transactions/queries/tests/evidence | `bgate_engine/txn.py`, `query.py`, `tests.py`, `evidence.py` | pure Python over SQLite |
| Canonical world & asset graph | `bgate_engine/world.py` + `.bgate/engine.db` | new SQLite store, sibling to the existing `.bgate` game store |
| Deterministic runtime | `bgate_engine/sim/` | reimplements `sim_tick` semantics; **authoritative** |
| Render/audio adapters | `../haymaker` Godot project | plays back engine replays; a thin `bgate_playback.gd` harness |

Builders Gate sits **above** the engine and is unchanged in spirit:

```
Builders Gate      design intent · seats · work queue · canon · artifact review ·
                   playtest decisions · orchestration       (decides WHAT & WHO)
Engine             world · schemas · transactions · simulation · rendering ·
                   debugging · tests · export               (the safe machinery)
```

---

## 4. Determinism — and why the runtime decision makes it tractable

The spec's property #3 is the load-bearing one; #4 and #5 are downstream of it. The
usual nightmare is **cross-runtime float parity** (Python sim vs Godot physics
diverging in the last bits). **Our decision removes that problem:**

- The **Python runtime is authoritative**. All proofs (causal chains, assertions,
  before/after evidence) are computed from the Python sim's own event log and
  snapshots. Determinism reduces to *"the Python runtime is self-consistent"* — given
  the same run identifier, it emits an identical event log and identical snapshots
  every time. That is achievable in plain Python: fixed operation order, integer tick
  counters (already how `boxer.gd` works), and **named seeded RNG streams**.
- **Godot never re-simulates for a proof.** It is handed an engine replay (input
  schedule + per-tick state) and *renders* it to produce evidence frames. Rendering
  drift cannot corrupt a claim, because the claim was already proven in Python.
- A separate, optional **conformance harness** can later replay the same input
  schedule in Godot and assert agreement within tolerance — but it is a *nice-to-have
  parity check*, never on the critical path for committing a change.

**Run identifier** (the thing that makes any run replayable by anyone):

```json
{
  "world_revision": 144,
  "runtime_version": "0.1.0",
  "scene": "fight",
  "seed": 88122,
  "tick_rate": 60,
  "input_schedule_hash": "sha256:…",
  "asset_manifest_hash": "sha256:…"
}
```

**Named RNG streams** — cosmetic randomness must never alter combat:

```
rng.cpu.decision      rng.combat.critical
rng.audio.pitch       rng.particles
```

Each stream is seeded as `hash(seed, stream_name)`; adding `rng.particles` cannot
perturb `rng.cpu.decision`. (Commodity Brawler is near-deterministic-by-input today;
these streams are the discipline for when CPU jump decisions / crits arrive.)

---

## 5. Protocol layer (small, composable, discoverable)

An agent must not need a 5,000-token manual. It bootstraps from:

```
engine.capabilities     world.summary     schema.list
scene.list              test.list
```

Every command returns its own schema, examples, side effects, and required capability.

```
→ { "command": "schema.describe", "component": "Movement" }
← { "fields": {
      "jump_velocity": { "type": "number", "units": "pixels_per_second",
                         "note": "Negative moves upward." },
      "jump_gravity":  { "type": "number", "units": "pixels_per_second_squared",
                         "minimum": 0 } } }
```

**Units and semantics are mandatory** on every field — without them an agent will
produce structurally valid nonsense (e.g. a positive `jump_velocity` that falls
through the floor). See `schemas/component_defs.json`.

Command surface (full list + capabilities in `schemas/protocol.commands.json`):

| Group | Commands |
|---|---|
| Discover | `engine.capabilities`, `schema.list`, `schema.describe`, `world.summary`, `scene.list`, `test.list` |
| Query | `world.query`, `entity.get`, `component.get`, `asset.get`, `transactions.of` |
| Mutate | `transaction.create`, `component.patch`, `entity.add`, `change.plan`, `transaction.commit`, `transaction.abort` |
| Simulate | `simulation.run`, `simulation.state`, `simulation.events`, `simulation.diff` |
| Prove | `test.list`, `test.affected_by`, `test.assert`, `evidence.render` |

---

## 6. Query system (semantic, not filesystem)

Agents get a dependency map **before** they mutate anything:

```
world.query: entities with Movement and Health
world.query: assets referenced by fighter.tommy
world.query: systems that write opponent.health
world.query: transactions that changed player.jump_velocity
world.query: tests affected by Combat.Hitbox changes
```

A useful response includes relationships (schema: `schemas/query_response.schema.json`):

```json
{
  "entity": "fighter.tommy",
  "components": ["Transform2D", "Movement", "Fighter", "Health", "StateMachine", "SpriteRig"],
  "used_by": ["scene.fight", "scene.training"],
  "assets": ["sprite.tommy_bright16", "audio.tommy_voice"],
  "systems": ["MovementSystem", "CombatSystem", "AnimationSystem"],
  "tests": ["jump_profile", "facing_hitbox", "stamina_regen"]
}
```

---

## 7. Transactions & concurrency (component-level, not file locks)

Each agent works in an isolated change set off a base revision:

```
main revision 142
 ├── tx_art_12       (sprite.scoville_bright16)
 ├── tx_gameplay_51  (system.combat, fighter.*.hitboxes)
 └── tx_audio_09     (audio cues)
```

A transaction (schema: `schemas/transaction.schema.json`) carries: base revision,
actor + seat, work item, proposed operations, validation results, simulation evidence,
dependencies changed, commit status. Non-overlapping component patches **merge**;
overlapping ones are rejected with precision the old file-lock model can't express:

```json
{ "ok": false, "error": "revision_conflict",
  "entity": "fighter.tommy", "component": "Movement", "field": "jump_gravity",
  "expected": 142, "current": 144, "changed_by": "tx_gameplay_48" }
```

This directly replaces the asset-path locking Builders Gate does today (recall the
`item-37` stale-lock flag in the art notes — component-scoped transactions make that
class of "who holds this file" bug structurally impossible).

### 7.1 Plans before mutations
For nontrivial goals the agent asks for a plan the engine **derives from dependency
metadata** — no internal LLM required:

```
→ { "command": "change.plan",
    "goal": "CPU uses jumps without breaking deterministic replays",
    "targets": ["fighter.cpu", "system.cpu_controller"] }
← { "operations": [
      "Add JumpDecision component to fighter.cpu",
      "Enable jump transitions in CpuController",
      "Add seeded stream rng.cpu.jump",
      "Extend replay schema with decision events" ],
    "affected_tests": ["cpu_determinism", "replay_roundtrip", "fighter_bounds"],
    "risks": ["Wall-clock randomness would break replay determinism."] }
```

---

## 8. Time-travel inspection & causal chains

The runtime retains per-tick snapshots + the event log, so:

```
simulation.state(sim_91, tick=220)
simulation.events(sim_91, ticks=180..240)
simulation.diff(sim_91, tick_a=190, tick_b=220)
```

A **causal chain** beats a log line. Built from the real `fight.gd` pipeline
(contact → range gate → facing gate → block gate → `take_hit` → `damage_applied`):

```json
{
  "event": "damage_applied", "tick": 212,
  "source": "fighter.scoville", "target": "fighter.tommy", "amount": 8,
  "causal_chain": [
    "input:scoville.jab@205",
    "transition:idle→punch@205",
    "contact_tick@211",
    "range_ok:dist=104<reach=115@212",
    "facing_ok:facing=1,needed=1@212",
    "collision@212",
    "damage_applied@212"
  ]
}
```

A **blind-side bug** is then legible as a chain where `facing_ok` reads
`facing=-1,needed=1` yet `damage_applied` still fires — exactly the class of defect
the critical demo fixes and proves gone.

---

## 9. Structured visual evidence

A plain screenshot says what the game *looks* like; extra buffers say what *is where*.
The engine (via the Godot adapter, or a headless Python rasterizer for hitboxes) can
emit: beauty frame · entity-ID frame · collision-shape frame · depth · UI-layout ·
animation-bone overlay. Manifest schema: `schemas/evidence_manifest.schema.json`.

```json
{
  "frame": "evidence/sim_91/tick_212.png",
  "buffers": ["beauty", "entity_id", "collision"],
  "entities": { "fighter.tommy": { "screen_bounds": [110,232,154,270],
                                    "visible": true, "occlusion": 0 } },
  "ui": { "player_health": { "screen_bounds": [36,24,310,32], "value": 92 } }
}
```

An art agent inspects composition; a QA agent checks the health bar matches runtime hp.
This is the engine-level upgrade of Builders Gate's current `godot_screenshot` +
`playtest` filmstrip — same idea, now addressable and tied to a tick.

---

## 10. Behavior as data (not only scripts)

The FSM already in `boxer.gd` (`enum State { IDLE, PUNCH, BLOCK, DUCK, KO, KICK, JUMP,
JUMP_KICK, GASSED, STAGGER }`) becomes a **structured behavior graph** the engine can
validate *without executing arbitrary code*:

```json
{
  "machine": "fighter",
  "states": ["idle","walk","jump","punch","kick","block","duck","gassed","stagger","ko"],
  "transitions": [
    { "from": "idle", "to": "punch", "when": {"input_pressed": "jab"},
      "unless": {"any": ["gassed", "stamina_below:10"]} }
  ]
}
```

```
behavior.find_unreachable_states
behavior.find_ambiguous_transitions
behavior.trace entity=fighter.tommy ticks=100..200
```

Script escape hatches stay allowed; the graph is the safe, queryable default.

---

## 11. Asset graph (semantic objects, not paths)

Assets become typed nodes with consumers and status — this is where the Assets page
you just rebuilt gets its real backing store:

```json
{
  "asset": "sprite.tommy_bright16", "type": "SpriteRig2D", "revision": 7,
  "source": "tommy_sheet_v7.png",
  "animations": { "idle": {"frames":2,"fps":6,"loop":true},
                  "jab":  {"frames":2,"fps":12,"loop":false} },
  "approved_reference": "ref.tommy_bright16",
  "consumers": ["fighter.tommy"], "status": "approved"
}
```

The engine **refuses export** when: a rejected revision is referenced · required
animation names are missing · sprite bounds vary outside tolerance · an audio event
references a missing cue · an asset changed without a transaction · art-QA evidence is
absent. (These are exactly the failure modes the current art pipeline catches only by
convention and long work notes.)

---

## 12. Agent-scoped context (the evolution of `seat_brief`)

Instead of dumping the whole project into every agent, the engine emits a scoped bundle:

```json
{
  "seat": "gameplay", "work_item": 51, "mission": "Fix blind-side hits",
  "targets": ["system.combat", "fighter.*.hitboxes"],
  "relevant_requirements": ["combat.facing_rule"],
  "relevant_tests": ["facing_hitbox", "crossup"],
  "locked_components": [], "recent_changes": ["tx_gameplay_48"],
  "allowed_commands": ["world.query","transaction.create","component.patch",
                       "simulation.run","test.assert"]
}
```

---

## 13. Human editor = same protocol

No editor-only hidden state. A human drag is the same typed op an agent would issue:

```
Human drags entity → entity.patch Transform2D → transaction preview → commit
```

Every agent mutation appears in the editor as a reviewable transaction; if a human can
do it, an authorized agent can issue the corresponding typed operation, and vice versa.

---

## 14. First prototype — the critical demo

The prototype needs only: SQLite world store · component schemas · transaction API ·
fixed-step 2D sim (Transform, Movement, Health, Hitbox, StateMachine) · input injection
· event recording · state queries · test assertions · Godot playback exporter · MCP
interface. **No custom editor or renderer required initially.**

The one demo that proves the architecture (not "AI bolted onto conventional dev"):

```
1. Agent asks why blind-side hits occur.
2. Engine returns the collision's causal chain (facing_ok reads wrong-way, damage still applied).
3. Agent opens an isolated CombatSystem transaction.
4. Engine reports affected tests (facing_hitbox, crossup).
5. Agent runs the exact failing replay by its run identifier.
6. Assertion confirms the blind-side collision is gone.
7. Engine renders before/after evidence frames.
8. Transaction commits without touching unrelated components.
```

### 14.1 Build order (each step is independently reviewable)

| # | Deliverable | Proves |
|---|---|---|
| 0 | **This doc + schemas** (current step) | agreed shape |
| 1 | `.bgate/engine.db` + `world.py`: entities/components/revisions, seeded from `boxer.gd` tunables | addressable + typed |
| 2 | `sim/` fixed-step runtime for Transform/Movement/Health/Hitbox/StateMachine; input injection; event log; snapshots | reproducible sim |
| 3 | `simulation.run` + `simulation.events` + causal-chain builder over the hit pipeline | inspectable, causal chains |
| 4 | `txn.py`: create/patch/validate/commit with component-level conflict detection | typed transactional |
| 5 | `tests.py`: `test.affected_by` + `test.assert` (facing_hitbox as the first test) | claims → evidence |
| 6 | `evidence.py`: replay export + before/after frames (Godot playback or headless hitbox raster) | visual evidence |
| 7 | `bgate_mcp` engine tools wrapping all of the above | agent-native surface |

### 14.2 Open questions to settle before step 1
- **Store split:** one `engine.db` per project alongside the existing game `.bgate`
  store (proposed), vs. new tables inside it. Proposed: separate file, clean lifecycle.
- **System model:** are `MovementSystem`/`CombatSystem` first-class rows (queryable
  "systems that write X") in the MVP, or derived later? Proposed: register them as
  metadata in step 2 so `world.query systems that write opponent.health` works early —
  it's the query the demo leans on.
- **Snapshot granularity:** full snapshot every tick (simple, larger) vs. keyframe +
  event replay (compact, more code). Proposed: full snapshot every tick for the MVP;
  the fight is seconds long and it makes time-travel trivial.

---

## 15. What this does *not* change

- The existing Godot game keeps running and shipping; the engine grows beside it.
- Builders Gate's seats, queue, canon, and artifact review stay — they gain a typed
  store to sit on instead of a path pile.
- No new runtime dependencies are assumed beyond the Python stdlib + SQLite already in
  use (schemas are plain JSON; validation can be hand-rolled or use an existing dep if
  already vendored).
