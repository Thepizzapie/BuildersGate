# Lessons from a shipped game

2026-07-28. A knowledge-extraction pass over one Builders Gate project, back
into Builders Gate itself. The point is that the next game should not have to
learn any of this again.

Everything below is sorted into four buckets and nothing is proposed on a hunch:
each row cites the line in the game repo that earned it.

## The source project, factually

`C:\Users\marta\Desktop\dungeon` — *Corporate Quest: Dungeon of
Deliverables*, an isometric turn-based tactics RPG on Godot 4.7, built over
roughly three weeks almost entirely by agent seats driving this pipeline.

| | |
| --- | --- |
| GDScript | 38 files, 31,403 lines |
| Python tooling | 30 files, 9,839 lines |
| Test assertions | ~1,450 static `check()` sites across 11 suites (867 in `game/tests/`, 590 in the two engine-agnostic assertion libraries) |
| Design docs | 17 (`docs/design_*.md` + `docs/SCALE.md`) |
| Bible | 37 sections |
| Lore | 28 entities, 15 locked canon facts |
| Pipeline record | 41 work items, 46 seat notes, 154 artifact revisions, 293 tracked assets, 1,059 activity rows |

Human authorship is concentrated in direction — the bible's `DIRECTOR CALL`
sections, the pinned refs, and the cut decisions. Code, tools, docs and tests
are agent output reviewed by a human. The repo has no root `CLAUDE.md`: its
operating knowledge went into the bible database and one hand-written
`.agents/scene-composition.md`, which is exactly the trapping this document
exists to undo.

## Already landed — verified, no action

Two of the suspected lessons are **already in Builders Gate** and need nothing:

- **"One editable thing is one named node."** `.agents/scene-composition.md`
  (whole file) and bible §37 have already been ported into
  `bgate_core/seats.py` (gameplay `workflow`, tech `workflow`, qa
  `workflow` item 2) and `templates/shared/CLAUDE.md` §"Building scenes".
  The port is faithful, including the y-sort and layer-order caveats.
- **"LOOK at the render before declaring a visual thing done."** Covered by qa
  `workflow` item 1 (render the ACTUAL in-game result, side-by-side with the
  pinned ref), `godot_evidence`, `evidence_check_ui`, and CLAUDE.md
  "Do not claim something works because the code looks right."
  *But see L9 below — the game found a sharper corollary that is NOT covered.*
- **Completeness matrices** (bible §14 "a unit that cannot walk north is a
  bug") already appear verbatim-ish in qa `workflow` item 2.

## The table

Bucket key: **RULE** = seat mission / stamped CLAUDE.md · **CHECK** = MCP tool,
loader validation or scaffold stamp · **TEMPLATE** = `templates/` ·
**GAME** = stays in the game.

| # | Lesson | Bucket | Destination in Builders Gate | Evidence |
| --- | --- | --- | --- | --- |
| L1 | Determinism is enforced at the **loader**. A chance-shaped key anywhere in content data is a hard load failure, not a warning. | RULE + CHECK | `seats.py` gameplay mission (done); new `content_determinism_check` tool | `game/scripts/sim/loot.gd:104-111` (`FORBIDDEN_KEYS`), `:193-196`; `shop.gd:136-144`; `quests.gd:414-423`; `gear_table.gd:259,294`; `skills.gd:93,330`; `design_side_quests.md:234-236` |
| L2 | An assertion that would still pass with the feature deleted is not a test. Pair every claim with a **control that fails**. | RULE | `seats.py` qa mission (done) | `scripts/sim/combat_tests.gd:673-677` ("the anti-vacuum device"), `:2233-2236`, `:2804-2812`, `:1783-1785` ("`0 == 0` is exactly how this check would pass with the whole feature removed"), `:3705`, `:3717-3719`; `tests/dialogue_test.gd:204-205` |
| L3 | Pin a fixture to what is **structurally guaranteed**, never to whatever is least finished today. | RULE | `templates/shared/CLAUDE.md` (done) | `game/tests/dialogue_test.gd:190-202` (fixture moved twice, both times because art got made), `:377-380`; `tests/testbed_tests.gd:10-17` |
| L4 | Assets that exist but are never referenced look identical to missing features, and **nothing errors**. | CHECK | new `asset_orphans` tool; extend `asset_verify` | `tools/fix_cover.py:3-7` (11 filing cabinets granted zero cover, "nothing errors, so it survived to ship"); `gen_portraits.py:234-237` (a playable class with no portrait, invisible for weeks); `plan_preview.py:186-188`; `build_tileset.py:232-241` |
| L5 | **Generate the minimum, derive the rest.** Generation is spent only on genuinely new silhouettes. | RULE | `templates/shared/CLAUDE.md` art pipeline (done) | `tools/spritekit.py:22-34`, `:186-194` ("IDLE IS DERIVED TOO, and that is the most important line in this file"); `build_character.py:14-17`; `gen_weapons.py:15-17`; `derive_wall_tiles.py:14-21`; bible §14/§15 (rotation classes: 1, 2 or 4 gens, declared at ticket time) |
| L6 | **A generation chain decays.** Never condition frame N on frame N-1. | RULE | `templates/shared/CLAUDE.md` art pipeline (done) | `tools/gen_actions.py:3-19` (by frame 3 the character turned front-facing and shrank 932px→821px); `gen_idles.py:5-12`; `spritekit.py:186-194` ("a three-model bake-off looked flawless precisely because it only generated frames 0 and 1"), `:236-241` |
| L7 | A **style** reference and an **identity** reference cannot share a weight. | CHECK + RULE | `bgate_core/generate.py` / `promptwriter.py`: split ref weight by `ref_pin` kind; art mission note | `tools/gen_portraits.py:288-294` ("did not transfer style, it transferred the SUBJECT"), `:308-317` (at 0.62 shared strength, four subjects came back as the anchor), `:319-326`, `:377-396` |
| L8 | Integration lines you do **not** own get written down verbatim at a named call site, not half-landed. | RULE + TEMPLATE | `templates/shared/CLAUDE.md` new "Leftovers" section (done) | `scripts/sim/exploration.gd:48-72`, `loot.gd:63-85`, `inventory.gd:53-77`, `shop.gd:86`, `combat.gd:462`, `skills.gd:1027` — 6 of 9 sim files; `design_exploration.md:423-436`; enforced downstream at `tests/exploration_test.gd:578` |
| L9 | A preview whose **legend can go stale lies more confidently** than raw data does. | RULE | `templates/shared/CLAUDE.md` (done) | `tools/render_floor.py:38-43` (renderer drew every desk as an enemy — "the exact opposite of the bug it had just been used to find"), `:84-90`, `:102-105`; `design_set_dressing.md:288-291` |
| L10 | A spec states **what it did not do and what is still dark**. "Flagged, not fixed" beats a silent gap. | TEMPLATE | new `templates/shared/design/_spec.md` | Present in all 17 docs: `design_story_arc.md:706` ("Flagged, not fixed"); `design_equipment.md:661` "Where this document is guessing"; `design_skill_trees.md:562`; `design_exploration.md:502` "Honest gaps"; `design_loot.md:489-491` ("a leftover that vanishes silently teaches nobody anything") |
| L11 | A settled decision carries its **acceptance test** in the decision itself. | CHECK + TEMPLATE | `bible_add` accepts/prompts for `acceptance`; spec template | bible §37 ("open the scene and count what a designer can select. If the answer is 'the floor and the walls', the work is not done"); §17 ACCEPTANCE (wall_proof → preview_floor → godot_screenshot, in that order); §16 ("Non-negotiable invariants, each one learned from a shipped defect") |
| L12 | Defects live at **junctions**, not straight runs. Verify the worst combination, not the happy path. | RULE | `seats.py` qa mission or workflow | `tools/wall_proof.py:5-13`, `:44-83`, `:110-113` (all 16 connection combos + doors, in context, against a different material); `build_walls.py:2127-2128` ("blending is judged on a RUN, never on one tile"); bible §17 ACCEPTANCE |
| L13 | Consistency is **enforced after the fact in one place**, never asked of a model. | RULE | art workflow (partially present via `consistency_check`) | `tools/spritekit.py:1-8` ("four different-sized people and a character who changed height between walking and punching"), `:89-95`; `derive_props.py:167-171` ("an object does not change height when it turns") |
| L14 | Stop building the detector. **Hand-mark the eight numbers** and ship a visualiser for them. | RULE | `seats.py` art or tech workflow | `tools/grips.py:3-11` (four detector rules, "each fixing one character and breaking another"; "approximately right after an hour" vs "exactly right" in a minute), `:11` ("If the art is regenerated, re-read them — do not rebuild the detector"), `:81-110`; `design_equipment.md:530-532` |
| L15 | Let the model do what it is good at; let **code do exact placement**. | RULE | art workflow | `tools/gen_held.py:3-18` (generate a magenta placeholder, read centroid/principal-axis/extent off the mask); `stamp_gear.py:3-8`, `:101-103`; `gen_weapons.py:3-7` ("asking an image model to hit a 2px target… which it cannot do") |
| L16 | Any tool that rewrites project data ships `--check` and **defaults to dry**. | RULE + TEMPLATE | `seats.py` tech mission (done) | `--check` in `bake_floor.py:48`, `derive_props.py:30`, `sync_tileset.py:21`, `build_walls.py:2155`, `plan_preview.py:17`, `regrade_walls.py:31`, `grade_wall_tiles.py:37`; dry-by-default in `rooms.py:104`, `fix_cover.py:55`; `build_walls.py:2152-2176` (returns 1 without writing) |
| L17 | A second pass over the first pass's output is how one asset set drifts into **six incompatible families**. | RULE | tech/art workflow; also a bible-writing habit | `tools/build_walls.py:21-43` (six measured defects, "fixing them separately is what produced the drift"; `cubicle == panel` to the digit); `grade_wall_tiles.py:4-10`; `make_wall_tiles.py:4-13` (superseded tools annotated in place, not deleted); bible §16 |
| L18 | A deferral must be labelled **by decision, not by oversight**, or someone "fixes" it as a bug. | RULE | `seats.py` director mission (done) | bible §25 ("the AI is omniscient BY DECISION, not by oversight — nobody should 'fix' that as a bug"); §25 STAGE 2 "deliberately deferred"; `design_equipment.md:292` ("Reject. Do not invent a type system for four items") |
| L19 | One data file is the source of truth for **both** the art path and the sim path; hand-editing either output breaks the guarantee. | RULE | already implied by tech workflow; strengthen | bible §17 (`data/floor_*.json` seeds both `bake_floor` and `floor_layout.gd`); `design_set_dressing.md:5` ("Do not hand-edit `floor_0.json`"); `tools/rooms.py:5-8` ("would put the same information in two places and let them drift"); `bake_floor.py:51-56` |
| L20 | **Naming a thing to forbid it puts it in the picture**; the framing clause beats the description clause. | RULE | art workflow / `promptwriter.py` | `tools/gen_portraits.py:108-113` ("'NO face, NO hair, NO skin' still produced a face with hair and skin"), `:264-269` ("THE WORD 'PORTRAIT' WAS THE BUG… it was not ignoring the description; it was obeying the framing"), `:74-77`, `:47-51`; `gen_held.py:36-38` |
| L21 | The house test contract: `PASS <name>` / `FAIL <name>: <why>` / `QA_RESULT pass=N fail=N`, non-zero exit. | TEMPLATE | `templates/shared/tests/` harness stub | `game/tests/loot_test.gd:19-25`, `:56` — identical in all 10 harnesses; `tests/exploration_test.gd:5-7` states it as "House contract"; fold-in idiom at `tests/combat_test.gd:609-630`, spelled out as copyable boilerplate at `campaign_test.gd:11-15` |
| L22 | Price the option you are actually **testing**, and report quality-per-dollar. | CHECK | `bgate_core/spend.py` (mostly covered) | `tools/bakeoff.py:31-37` ("ideogram charges 2.5x for exactly the field we are testing, so the cheap headline price would be a lie here"), `:70-94` (scored A/B: separation metric + USD per contender) |
| L23 | Registration is not one number. Derive the placement number from the **same function that draws the art**. | GAME (with a general shadow) | stays | `tools/build_tileset.py:41-46`; `preview_floor.py:10-14`; `sync_tileset.py:5-18` (`texture_origin.x` never set → every wall floated half a cell) |
| L24 | Isometric 2:1 projection contract, wall cutaway shader, plinth reconciliation, rotation classes, the 32px riser ladder. | GAME | stays | bible §13, §16, §17; `docs/SCALE.md` throughout |
| L25 | The tutorial is protagonist-agnostic; casting is data. | GAME | stays | bible §24 |
| L26 | Every world fiction entity, quest, class, floor. | GAME | stays | 28 lore entities, 15 canon facts |

## GENERAL RULE — the exact sentences

Written to match the existing missions: terse, imperative, second person implied.
Each is followed by what was cut to pay for it.

### `seats.py` — gameplay mission

> Randomness lives in ONE declared seeded stream or nowhere — a chance-shaped
> field in content data (chance, weight, one_of, roll) is a load failure, not a
> design choice.

*Length:* +1 sentence. Nothing cut. The gameplay mission was the shortest of the
seven and this is the failure the game hard-coded into five separate loaders,
which is as strong a signal as the repo produces.

### `seats.py` — qa mission

> An assertion that would still pass with the feature deleted is not a test:
> every claim needs a control that fails.

*Cut to pay for it:* nothing removed, but note that `Run asset_verify after any
multi-seat session; godot_check_project before builds` duplicates
`workflow` item 3 and CLAUDE.md's loop step 6. It is the obvious cut if the
mission ever needs to shrink.

### `seats.py` — director mission

> Every settled decision names its acceptance test and what it deliberately
> leaves dark — a deferral nobody labelled gets "fixed" as a bug.

*Length:* +1 sentence on the shortest mission in the file. Justified: the
director is the only seat that writes the bible, and bible §16/§17/§25/§37 —
the four sections that did the most work in the game — are exactly the ones
that carry an acceptance test and a scope boundary. The other 33 do not.

### `seats.py` — tech mission

> A tool that rewrites project data ships `--check` and defaults to dry.

*Length:* +1 short sentence. Justified: nine of the game's thirty tools have a
`--check` mode and two default to dry, and the two that mutate JSON without one
(`bake_floor` before it was parameterised) produced the one recorded
silently-overwrote-the-campaign incident (`bake_floor.py:51-56`).

### `templates/shared/CLAUDE.md`

Three additions, all in the register of the existing file:

1. A **"Leftovers"** subsection under the loop (L8).
2. **Determinism** and **stale-legend** items in "What NOT to do" (L1, L9).
3. **Fixture** and **anti-vacuous assertion** items in "What NOT to do" (L2, L3).
4. Two lines in "Art pipeline" for chain decay and derivation (L5, L6).

## TOOL / CHECK — what it would check, and what half-does it already

### `asset_orphans` (L4) — the highest-value new tool

**What it checks.** Walk `game/**` for asset files (`.png`, `.ogg`, `.wav`,
`.glb`, `.tres`) and grep every `.tscn` / `.tres` / `.gd` for each file's
`res://` path and its `uid://`. Report three lists: *orphan* (on disk, no
referrer), *dangling* (referenced, not on disk), *declared-but-empty* (tracked
in the `asset` table, zero referrers). Cheap — it is a path index plus a
substring scan, no engine launch.

**What already half-does it.** `asset_verify` (`bgate_core/assets.py:473-511`)
compares tracked assets to disk by content hash and reports
clean/locked/modified/missing/untracked_hash. It answers "did someone stomp
this file", never "does anything point at this file". `godot_check_project`
catches dangling references at import time but is silent on orphans, which is
the direction that bit the game three times (cover data, portraits, audio).
`plan_preview.py:186-188` is the game's ad-hoc version of the dangling half.

### `content_determinism_check` (L1)

**What it checks.** Given a content directory, recursively scan every JSON/TRES
key against a forbidden-key list (`chance`, `weight`, `weights`, `one_of`,
`pick`, `random`, `roll`, `rolls`, `rarity_roll`, `shuffle`, `variance`,
`seed`), substring-matched so `drop_chance` and `crit_chance` are both caught.
Plus a source lint: assert `randi`/`randf`/`randomize`/`RandomNumberGenerator`
appear in no sim file outside a declared allowlist — with **comments stripped
first**, because a file that argues at length about RNG would otherwise trip
its own check (`tests/exploration_test.gd:109-123`).

**What already half-does it.** Nothing. `canon_check` is the closest shape — a
deterministic lexical filter that runs on every write — and is the right model
to copy. The scaffold's better move may be to stamp the loader guard into
`templates/shared/` so a new project's first content loader already rejects,
rather than to add a 79th tool an agent has to remember to call.

### `bible_add(acceptance=...)` (L11)

**What it checks.** An optional `acceptance` field on `bible_section`,
surfaced in `bible_read` and in `seat_brief`'s bible view. Not enforced —
a constraint with no mechanical acceptance test is a legitimate thing to
record. But `scope_check` and the qa workflow could both cite it, and a
constraint with an acceptance line is the one an agent can actually close.

**What already half-does it.** `bible_section` has `kind`, `title`, `body`,
`rank`. The game got this by convention alone: writing `ACCEPTANCE:` at the end
of the body. That convention worked, which is an argument for a template rather
than a schema change.

### `ref` weight split by kind (L7)

**What it checks.** Not a check — a fix. `ref_pin` already distinguishes
`kind="style"` from `kind="character"`. The generation path should weight them
differently by default, because at equal strength the identity signal wins and
the style ref transfers its subject. The game's measured numbers: 0.62 shared
produced four subjects rendered as the anchor; 0.28 for the style anchor
worked, and the closer a subject sits to the anchor the *less* anchor it can
take (`gen_portraits.py:308-326`).

**What already half-does it.** `ref_pin`/`ref_list` carry the kind;
`consistency_check` catches the drift *after* it happens. Nothing uses the kind
to weight the call.

## TEMPLATE

| What | Where | Why |
| --- | --- | --- |
| `templates/shared/tests/` — one `SceneTree` harness with the `check()` / `QA_RESULT` contract and the sub-suite fold-in idiom (L21) | new dir, overlaid by `scaffold.new_project` | The game had zero test-framework code; ten suites hand-rolled the same 8 lines. A new project should start with them. `bgate_core/scaffold.py:66-83` already overlays `shared/` wholesale, so this is a file drop with no code change. |
| `templates/shared/design/_spec.md` — spec skeleton: Status / Grounded in / Scope / the design question / what we lose honestly / explicitly rejected / the seams / LEFTOVERS / where I am guessing (L10) | new file | All 17 design docs converged on this shape independently. Stamping it saves every future project the convergence. |
| A `Determinism` stub section in the initial bible written by `project_init` (L1) | `bgate_core/project.py` seed sections | The game's bible §9 ("same state + same inputs = same outcome… tests drive the sim headless") was written on day one and is cited by five loaders. |

## If the owner does only three

1. **`asset_orphans`** (L4). It is the only lesson here where the failure is
   *completely silent in every existing tool* — the file is on disk, the hash
   matches, `godot_check_project` is green, and the feature does not exist. It
   bit the game in art, in audio and in cover data independently, which is the
   definition of a general failure rather than a domain one. It is also the
   cheapest to build: a path index and a substring scan, no engine, no model.

2. **The two mission sentences already landed — gameplay's determinism clause
   and qa's anti-vacuous-assertion clause** (L1, L2). Zero build cost, they ride
   into every future project's every `seat_brief`, and between them they are the
   two things the game's ~1,450 assertions were most often protecting. A test
   suite that is green because it is vacuous is strictly worse than no suite,
   and it is the default output of an agent asked to "add tests".

3. **`templates/shared/tests/` plus the spec skeleton** (L21, L10). Both are
   file drops into a tree `scaffold.py` already copies wholesale. The test
   harness is the difference between a project that can say "QA_RESULT pass=94
   fail=0" on day two and one that hand-rolls a runner in week three; the spec
   skeleton is what made the game's 17 docs auditable, and its most valuable
   sections are the ones nobody writes unprompted — "what we lose, honestly",
   "explicitly rejected", "where I am guessing".

Everything else in the table is real but second-order: L7 and L20 are art-path
tuning, L12–L17 are workflow prose that can wait for the next time a seat
mission is edited, and L23–L26 are the game's own.

## What was expected and not found

- **No root `CLAUDE.md` in the game project.** `bgate adopt` stamps one; this
  repo has none, so either it predates that or it was removed. Its operating
  knowledge went into the bible instead — which worked, but means none of it
  was visible to a session that did not call `bible_read`.
- **No orphan detection anywhere**, in either repo. Both have the *dangling*
  direction (reference → missing file) and neither has the *orphan* direction.
- **No audio evidence at all.** The suspicion that the exists-but-unreferenced
  failure recurred in audio is plausible but unproven here: the game has no
  audio pipeline, no audio tools, and the `audio` seat left no notes. L4 rests
  on art (`gen_portraits.py:234-237`) and cover data (`fix_cover.py:3-7`).
- **`playtest_item` is empty** — 0 rows. The playtest/telemetry loop, which is
  the most distinctive thing Builders Gate offers, went entirely unused on the
  project that most needed it. That is a finding about adoption, not about the
  game, and it is worth asking why before building anything else.
