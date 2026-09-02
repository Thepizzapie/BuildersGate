# Builders Gate — Agent-Native Engine (`bgate_engine`)

> ## Read this first: what this document is, and what it is not
>
> **It is** a proposal, written from one game. Every schema here was reverse-
> engineered field-by-field from a single title (*Commodity Brawler*, an arcade
> fighter) and its `boxer.gd` / `fight.gd`. It is an exploration of what an
> agent-native engine could look like if that title's shape generalised.
>
> **It is not** a validated general model, an accepted architecture, or shipped.
> `bgate_engine/` contains this document and seven JSON files. There is no
> runtime code, and **nothing in the repository imports it** — the only
> references anywhere are packaging (`pyproject.toml` ships the schemas as data),
> `tests/packaging/test_packaging.py` asserting they land in the wheel, and CI checking the
> same. Deleting the directory would break a packaging test and nothing else.
>
> **The load-bearing risk** is the derivation itself. Section 1 presents
> single-title derivation as a *strength* ("not a generic toy"). Read it as the
> known weakness it also is: this proposes a **second authoritative simulation
> model** — a large, long-lived commitment — generalised from one data point.
> Reverse-engineering one game's runtime always produces something that looks
> principled, because the thing it was traced from is real and coherent. That
> says nothing about the second game. See **§16** for which parts survive a
> second title, which are the fighter leaking through, and what would have to be
> true before any of this is built.
>
> **Decisions recorded 2026-07-19** (proposals, not ratified):
> - **Python engine as the source of truth.** The canonical world + the
>   deterministic simulation would live in `bgate_engine`; Godot becomes a
>   **renderer / playback surface** driven by engine-produced state and replays,
>   and does not re-simulate for proofs.
> - **Ship the design + schemas first** (this document) for review before any
>   runtime code. That step happened; the review it was waiting for did not.
>
> **Amended 2026-07-25 — the sim runtime is Rust** (`../bgate-sim`).
> **⚠️ WITHDRAWN the same day — see §16.5. Never implemented; no crate exists.**
> The §16.4 experiment was run against two other titles and both failed, so the
> layer this amendment chose a language for should not be built. Retained as a
> record of a decision made and withdrawn, not as guidance. Original text follows.
>
> Recorded in
> the same register as the decisions above: proposed, not ratified. It *narrows*
> the first decision rather than replacing it — the engine is still the source of
> truth, Godot still never re-simulates, and only the **language of the
> deterministic runtime** changed. Python keeps the world store, transactions,
> queries, tests, evidence, and the MCP surface; steps 1 and 3–7 of §14.1 are
> untouched. Three reasons, in the order they mattered:
>
> 1. **One authoritative sim, and the choice expires at step 2.** §4's argument
>    is that exactly one simulator exists. Writing step 2 in Python and porting
>    later yields two, reintroducing the cross-runtime float-parity problem §4 was
>    structured to dodge. A now-or-never fork, which is why it was worth settling
>    before step 1 seeds `engine.db`.
> 2. **WASM is the only clean answer to cloud-side agents.** One crate emits a
>    native extension for local Builders Gate and a `.wasm` for cloud agents —
>    browser, Worker, or sandbox, with no Python or Godot install.
> 3. **Throughput converts tuning into search.** §2.1's `peak_height_px [130,152]`
>    / `air_time_s [1.5,1.75]` requirement stops being a human on the F1 panel and
>    becomes an exhaustive sweep. See §4.1.
>
> **What this amendment does not do:** it does not answer the load-bearing risk
> above, and it makes that risk *worse* in one respect — a Rust runtime is a
> larger, longer-lived commitment than a Python one, so single-title derivation
> matters more, not less. It is therefore scoped deliberately to **Commodity
> Brawler's simulation only** — the one title the schemas were traced from — so it
> stays cheap to discard. **§16 is still missing:** the preamble promises it and
> this document ends at §15. Nothing past step 2 should be built until it exists.

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

## 1. It describes *Commodity Brawler* — which is both the evidence and the limit

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

**And that is the whole problem with them.** Commodity Brawler is not a
representative game; it is an unusually convenient one. Fixed-step, rollback-
disciplined, integer tick counters, an explicit `save_state()`, no physics body,
no delta anywhere. Nothing in the design below has been asked to hold for a game
that leans on Godot's physics, uses variable timestep, has more than two entities
alive at once, or streams a world larger than a 640×360 stage. A model traced
from the one project that already did the hard determinism work will always fit
that project. §16 is the honest accounting of what that buys and what it costs.

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
| Protocol & capability | `src/bgate_mcp/` (new engine tools) | same MCP server the seats already use |
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

---

## 16. Evidence, limitations, and how to settle this

### 16.1 Relationship to what shipped: none

`bgate_engine/` is this document, `README.md`, and seven JSON files. No Python,
no tests of its own, and no importer. Grepping the repository for `bgate_engine`
returns `pyproject.toml` (which ships the schemas as package data),
`tests/packaging/test_packaging.py` (which asserts they reach the wheel), and the CI wheel
inspection — no `import bgate_engine` anywhere, in this repo or the game.

Every capability the demo in §14 promises has a shipped, non-engine counterpart
today: `godot_run` drives the real engine headless and a QA bot evaluates
server-side `expect` entries against sampled runtime state; `bgate_core.board.gitwork`
gives per-file diffs and a scoped revert off a captured base commit;
`bgate_core.board.iterations` records the causal chain; `godot_screenshot` and the
playtest filmstrip are the evidence frames. Those are worse than what §9 and §14
describe — they are unaddressable, untyped, and prove less. They also exist. Any
case for building this has to be made against them, not against the path pile
§0 argues with.

### 16.2 What is game-agnostic and what is the fighter leaking through

Read per file, not in aggregate — the split is not even:

| Schema | Verdict |
|---|---|
| `authored_intent.schema.json` | **Agnostic.** Requirement id, named acceptance ranges, priority, owner, `verified_by`. Nothing genre-bound. Its `owner` enum hard-codes Builders Gate's eight seats — a coupling to *this product*, not to the fighter. Examples are boxer; the shape is not. |
| `transaction.schema.json` | **Agnostic.** Base revision, ops, validation, evidence links, field-level conflict. The one leak is cosmetic: `id` is pinned to `^tx_[a-z]+_[0-9]+$`, and `seat` again enumerates the eight seats. |
| `query_response.schema.json` | **Agnostic.** An entity/component/asset/system/test relationship map. Would apply unchanged to any ECS-shaped world. |
| `protocol.commands.json` | **Agnostic** command surface (discover / query / mutate / simulate / prove) with per-command capability and side effects. `critical_demo_trace` is boxer-specific on purpose — it is a demo script, not a contract. |
| `evidence_manifest.schema.json` | **Mostly agnostic.** Per-tick frame + buffers + screen bounds + UI values. The buffer list is a closed enum, and `entities`/`ui` are screen-space only — fine for 2D, thin for a 3D title that wants world-space or per-instance evidence. |
| `run_identifier.schema.json` | **Agnostic except one field.** `tick_rate` is `"const": 60` — Commodity Brawler's `TICK = 1/60` written into the schema as a universal law. That single `const` is the clearest example of the derivation problem in this directory: a second game at 30 or 120Hz is invalid against the schema that claims to identify any run. Otherwise sound. |
| `component_defs.json` | **This is one fighting game with the serial numbers filed off.** `Health` is generic. Everything else is not: `Transform2D` bakes a 640×360 stage and arcade auto-facing; `Movement` carries `ring_min_x`/`ring_max_x` (a boxing ring) and a hand-rolled jump arc that presumes no physics body; `Fighter` is jab/hook/kick reach-damage-cost plus the stamina, fatigue, gassed and stagger economy four playtests of *that game* tuned; `Hitbox` is a state rule whose whole content is the blind-side facing gate; `StateMachine`'s enum and `runtime_counters` mirror `boxer.gd`'s `save_state()` exactly, with a comment noting `fight_test.gd` hard-codes the integer values; `SpriteRig.required_animations` defaults to the fighter's move list. |

So the framing layer (intent, transactions, queries, protocol, evidence) is
plausibly general and would be worth keeping even if the rest were dropped. The
**world model** — the part that decides what a game *is* — is a single fighter,
and it is the part the whole "canonical world" claim rests on.

### 16.3 What would have to be true for this to generalise

Stated as falsifiable conditions, because "it seems principled" is not one:

1. **A second game's runtime lifts into `component_defs.json` without a rewrite.**
   Not "we add components" — additions are expected. The test is whether
   `Transform2D`, `Movement`, `StateMachine` survive contact, or whether the
   second title needs its own incompatible versions of them. If every game brings
   its own world model, the canonical store is a per-game schema with extra
   ceremony, and the value collapses to the framing layer alone.
2. **A game that does not already have `sim_tick` can be given one.** Commodity
   Brawler was rollback-disciplined before the engine was imagined. §4's
   determinism argument assumes that discipline is present. For a normal Godot
   game leaning on `_physics_process` and a `CharacterBody2D`, "reimplement the
   sim in Python and make Godot a renderer" is not a lift — it is a full rewrite
   of the game, per game, and the engine is then a reason to write games twice.
3. **The Python runtime is fast enough to be the authority.** No benchmark exists.
   §14.2 proposes a full snapshot every tick because "the fight is seconds long".
   A game that is not seconds long invalidates the storage plan and possibly the
   time-travel feature that plan exists to serve.
4. **Someone chooses to author through it.** §13 claims editor and agent issue the
   same typed ops. There is no editor, and the existing pipeline's mutations are
   MCP tools against `.bgate/game.db`. A second authoritative store that nothing
   writes to is drift waiting to happen — the exact failure `asset_verify` exists
   to catch, one layer up.

### 16.4 Recommended next step: one cheap experiment, then decide

Do **not** start at §14.1 step 1. The build order assumes the architecture and
front-loads a SQLite world store; it cannot fail in a way that teaches anything.
Instead:

**Take a second game — ideally one nobody wrote with this document open — and try
to express it in `component_defs.json` and `run_identifier.schema.json` as they
stand.** No code. Hours, not weeks. Record every field that had to change, every
component that had to be added, and every one that turned out to mean something
different. Two outcomes, both useful:

- *The framing holds and only the components grow* → the general parts are real.
  Split this document: keep intent / transactions / queries / protocol / evidence
  as the proposal, and demote `component_defs.json` to what it actually is — one
  worked example of a game-specific schema.
- *The second game does not fit* → retire the "canonical world + authoritative
  Python simulation" claim. Salvage the two ideas that need no engine at all and
  would improve the shipped pipeline tomorrow: **causal chains** (§8 — an event
  that carries the gates it passed beats any log line, and the QA bot could emit
  them today from GDScript) and **structured visual evidence** (§9 — an entity-id
  or collision buffer beside the beauty frame, which `godot_screenshot` could
  produce without a new store).

Until one of those happens, this directory is a design note with schemas
attached, and should be cited as one.

### 16.5 The experiment was run (2026-07-25). Result: the second game does not fit.

§16.4 asked for a second game, expressed against `component_defs.json` and
`run_identifier.schema.json` as they stand, no code. Two were used — a near case
and a far case, both written years before this document, neither with it open:

| | **tommy-tomato** — *Harvest Souls* | **tomato-strike** — *Garden Offensive* |
|---|---|---|
| Genre | 2D melee soulslike | 3D tactical FPS |
| Stack | custom HTML5 canvas engine | react-three-fiber + WebRTC |
| Core | `src/game/sim/Game.ts` (119 KB, explicit `sim/`) | `src/game/core/sim.ts` + `core/types.ts` |
| Why chosen | *closest* to the fighter — 2D, melee, real-time, and it already calls its module `sim` | *furthest* — 3D, ranged, round-based, team economy |

The near case is the fair test; the far case only confirms it.

**Neither game has a tick.** This is the finding, not the component mismatch.

- tommy-tomato drives everything from `requestAnimationFrame` with a clamped
  wall-clock delta — `let dt = (now - this.lastT) / 1000; if (dt > 0.05) dt = 0.05`
  (`Game.ts:537`). Every update is `dt`-parameterised: `updatePlayer(dt)`,
  `updateEnemy(e, dt)`, eight `ai*(e, dt, …)` behaviours, `updateProjectiles(dt)`,
  `updateCombatVsPlayer(dt)`.
- tomato-strike's authority is `hostTick(state, inputs, dt)` (`sim.ts:203`), which
  advances a wall-clock accumulator, `state.now += dt * 1000`. The only fixed step
  in the entire title is `const dt = 1/60` inside grenade integration
  (`sim.ts:523`) — a sub-system, not the simulation.

**Neither seeds its randomness**, and not just cosmetically. tommy-tomato draws
the *run seed itself* from unseeded global random — `newRun(Math.floor(Math.random()
* 1e9))` (`Game.ts:2208`) — then passes `() => Math.random()` as the roll function
for boon selection (`:2269`). tomato-strike picks the bomb carrier (`sim.ts:154`)
and spawn points (`:596`) the same way. §4's named-stream discipline has nothing
to attach to: there is no seed to derive streams from.

So `run_identifier.schema.json` fails both, and `tick_rate: "const": 60` is the
least of it. The schema's *premise* — that `{world_revision, seed, tick_rate,
input_schedule_hash, …}` identifies a bit-replayable run — requires fixed-step
plus seeded RNG. Neither title has either. Neither is one `const` away.

`component_defs.json` fails as §16.2 predicted, and worse in the near case than
expected. tommy-tomato's `Enemy` (`Game.ts:88–118`) is a single flat 31-field
struct that mixes canonical state (`x, y, vx, vy, hp, state, timer`), the
soulslike poise economy (`staggerVal`, `staggerT`) that `Fighter` has no vocabulary
for, presentation (`phase // anim phase`, `attackProg // 0..1 for art`), and
netcode interpolation (`tx?`, `ty? // client interpolation`) in one place — so it
violates §2's three-representation separation *within a single type*, which is the
separation the whole store depends on. Absent entirely from the schema: projectiles,
pickups, charms, the sap economy, husks, bonfire/area transitions, boss phases
(`bossMove`, `bossPhase2`). tomato-strike additionally needs `Vec3` and yaw/pitch
against a `Transform2D` whose `position` is length-2 with `facing` an enum of
`[-1, 1]`; hitscan `ShotMsg`/`ShotHit` applied from clients rather than resolved at
a contact tick; and a whole match layer — `RoundPhase`, `BombState` plant/defuse
progress, `InventoryItem`, the buy economy, `TeamId` — that sits *above* entities
and has no home in the model at all.

**Condition §16.3.1 fails. Condition §16.3.2 fails, and fails harder than it was
written.** §16.3.2 anticipated "a full rewrite of the game, per game." The evidence
is worse than cost: *both* titles independently adopted **host-authoritative
netcode with snapshot interpolation** — tommy-tomato via `netTick` /
`interpolateEnemies` / `RemotePlayer.snap`, tomato-strike via `hostTick` plus
client `ShotMsg`. That is the architecture you choose *specifically so that you do
not need determinism*. It is not an oversight to be corrected; it is a sound
decision, made twice, that is incompatible with the premise.

Which exposes the confound in §1. This document reads "the test game already made
the hard commitment" as evidence that the engine is grounded in something real.
Read the other way: **the engine was derived from the one title that already
satisfied its hardest precondition.** Across three titles by the same author the
deterministic one is 1 of 3, and it is the *oldest* — both later games moved away
from it. The engine does not supply determinism. It requires it, per game, as a
precondition, and that precondition is where the real cost lives.

**Verdict: §16.4's second outcome.** Retire the canonical-world + authoritative-
simulation claim. Keep the framing layer (intent, transactions, queries, protocol,
evidence), which survived both titles unchanged and is worth keeping on its own.
Demote `component_defs.json` to what it is: one worked example, for one game.
Salvage **causal chains** (§8) and **structured visual evidence** (§9) onto the
shipped `godot_run` / `godot_screenshot` path, per §16.4 — both need no store, no
second runtime, and would improve the existing pipeline without any of this.

**This also cancels the Rust amendment in the preamble.** `bgate-sim` was scoped to
be the authoritative runtime; that layer should not be built, so the language it
would have been written in is moot. The reasoning there was sound *conditional on
the engine being built* — the condition failed. Nothing was implemented; the
amendment is retained above as a record of a decision made and withdrawn.
