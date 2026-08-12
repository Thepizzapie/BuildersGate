# Lessons from a shipped game

2026-07-28, status refreshed 2026-08-11. A knowledge-extraction pass over one
Builders Gate project, back into Builders Gate itself, so the next game does not
have to learn any of it again. Every row cites the line in the game repo that
earned it.

## Status at a glance

| Landed | Still unbuilt |
| --- | --- |
| Four seat-mission sentences (L1, L2, L16, L18), verified in `bgate_core/seats.py` | `asset_orphans` (L4) |
| `templates/shared/CLAUDE.md`: chain decay, derivation, fixtures, determinism, stale legends, leftovers (L3, L5, L6, L8, L9) | `content_determinism_check` (L1) |
| Scene composition and render-before-done, already ported before this pass | `bible_add(acceptance=...)` (L11) |
| | `templates/shared/tests/` and `design/_spec.md` (L10, L21) |
| | Reference weight split by `ref_pin` kind (L7) |

## The source project

*Corporate Quest: Dungeon of Deliverables*, an isometric turn-based tactics RPG
on Godot 4.7, built over roughly three weeks almost entirely by agent seats
driving this pipeline.

| | |
| --- | --- |
| GDScript | 38 files, 31,403 lines |
| Python tooling | 30 files, 9,839 lines |
| Test assertions | ~1,450 static `check()` sites across 11 suites: 867 in `game/tests/`, 590 in the two engine-agnostic assertion libraries |
| Design docs | 17 (`docs/design_*.md` plus `docs/SCALE.md`) |
| Bible | 37 sections |
| Lore | 28 entities, 15 locked canon facts |
| Pipeline record | 41 work items, 46 seat notes, 154 artifact revisions, 293 tracked assets, 1,059 activity rows |

Human authorship is concentrated in direction: the bible's `DIRECTOR CALL`
sections, the pinned refs, the cut decisions. Code, tools, docs and tests are
agent output reviewed by a human.

## The table

Bucket key: **RULE** = seat mission or stamped CLAUDE.md · **CHECK** = MCP tool,
loader validation or scaffold stamp · **TEMPLATE** = `templates/` ·
**GAME** = stays in the game.

| # | Lesson | Bucket | Destination in Builders Gate | Evidence |
| --- | --- | --- | --- | --- |
| L1 | Determinism is enforced at the **loader**. A chance-shaped key anywhere in content data is a hard load failure, not a warning. | RULE + CHECK | `seats.py` gameplay mission (done); `content_determinism_check` tool | `game/scripts/sim/loot.gd:104-111` (`FORBIDDEN_KEYS`), `:193-196`; `shop.gd:136-144`; `quests.gd:414-423`; `gear_table.gd:259,294`; `skills.gd:93,330`; `design_side_quests.md:234-236` |
| L2 | An assertion that would still pass with the feature deleted is not a test. Pair every claim with a **control that fails**. | RULE | `seats.py` qa mission (done) | `scripts/sim/combat_tests.gd:673-677`, `:2233-2236`, `:2804-2812`, `:1783-1785`, `:3705`, `:3717-3719`; `tests/dialogue_test.gd:204-205` |
| L3 | Pin a fixture to what is **structurally guaranteed**, never to whatever is least finished today. | RULE | `templates/shared/CLAUDE.md` (done) | `game/tests/dialogue_test.gd:190-202` (fixture moved twice, both times because art got made), `:377-380`; `tests/testbed_tests.gd:10-17` |
| L4 | Assets that exist but are never referenced look identical to missing features, and **nothing errors**. | CHECK | `asset_orphans` tool; extend `asset_verify` | `tools/fix_cover.py:3-7` (11 filing cabinets granted zero cover); `gen_portraits.py:234-237` (a playable class with no portrait, invisible for weeks); `plan_preview.py:186-188`; `build_tileset.py:232-241` |
| L5 | **Generate the minimum, derive the rest.** Generation is spent only on genuinely new silhouettes. | RULE | `templates/shared/CLAUDE.md` art pipeline (done) | `tools/spritekit.py:22-34`, `:186-194`; `build_character.py:14-17`; `gen_weapons.py:15-17`; `derive_wall_tiles.py:14-21`; bible §14/§15 (rotation classes: 1, 2 or 4 gens, declared at ticket time) |
| L6 | **A generation chain decays.** Never condition frame N on frame N-1. | RULE | `templates/shared/CLAUDE.md` art pipeline (done) | `tools/gen_actions.py:3-19` (by frame 3 the character turned front-facing and shrank 932px to 821px); `gen_idles.py:5-12`; `spritekit.py:186-194`, `:236-241` |
| L7 | A **style** reference and an **identity** reference cannot share a weight. | CHECK + RULE | `generate.py` / `promptwriter.py`: split ref weight by `ref_pin` kind | `tools/gen_portraits.py:288-294`, `:308-317` (at 0.62 shared strength, four subjects came back as the anchor), `:319-326`, `:377-396` |
| L8 | Integration lines you do **not** own get written down verbatim at a named call site, not half-landed. | RULE + TEMPLATE | `templates/shared/CLAUDE.md` "Leftovers" (done) | `scripts/sim/exploration.gd:48-72`, `loot.gd:63-85`, `inventory.gd:53-77`, `shop.gd:86`, `combat.gd:462`, `skills.gd:1027` (6 of 9 sim files); `design_exploration.md:423-436`; `tests/exploration_test.gd:578` |
| L9 | A preview whose **legend can go stale lies more confidently** than raw data does. | RULE | `templates/shared/CLAUDE.md` (done) | `tools/render_floor.py:38-43` (renderer drew every desk as an enemy), `:84-90`, `:102-105`; `design_set_dressing.md:288-291` |
| L10 | A spec states **what it did not do and what is still dark**. "Flagged, not fixed" beats a silent gap. | TEMPLATE | `templates/shared/design/_spec.md` | Present in all 17 docs: `design_story_arc.md:706`; `design_equipment.md:661`; `design_skill_trees.md:562`; `design_exploration.md:502`; `design_loot.md:489-491` |
| L11 | A settled decision carries its **acceptance test** in the decision itself. | CHECK + TEMPLATE | `bible_add` accepts `acceptance`; spec template | bible §37; §17 ACCEPTANCE (wall_proof, preview_floor, godot_screenshot, in that order); §16 |
| L12 | Defects live at **junctions**, not straight runs. Verify the worst combination, not the happy path. | RULE | `seats.py` qa mission or workflow | `tools/wall_proof.py:5-13`, `:44-83`, `:110-113` (all 16 connection combos plus doors); `build_walls.py:2127-2128`; bible §17 |
| L13 | Consistency is **enforced after the fact in one place**, never asked of a model. | RULE | art workflow (partly present via `consistency_check`) | `tools/spritekit.py:1-8`, `:89-95`; `derive_props.py:167-171` |
| L14 | Stop building the detector. **Hand-mark the eight numbers** and ship a visualiser for them. | RULE | `seats.py` art or tech workflow | `tools/grips.py:3-11` (four detector rules, each fixing one character and breaking another), `:11`, `:81-110`; `design_equipment.md:530-532` |
| L15 | Let the model do what it is good at; let **code do exact placement**. | RULE | art workflow | `tools/gen_held.py:3-18`; `stamp_gear.py:3-8`, `:101-103`; `gen_weapons.py:3-7` |
| L16 | Any tool that rewrites project data ships `--check` and **defaults to dry**. | RULE + TEMPLATE | `seats.py` tech mission (done) | `--check` in `bake_floor.py:48`, `derive_props.py:30`, `sync_tileset.py:21`, `build_walls.py:2155`, `plan_preview.py:17`, `regrade_walls.py:31`, `grade_wall_tiles.py:37`; dry by default in `rooms.py:104`, `fix_cover.py:55`; `build_walls.py:2152-2176` (returns 1 without writing) |
| L17 | A second pass over the first pass's output is how one asset set drifts into **six incompatible families**. | RULE | tech/art workflow | `tools/build_walls.py:21-43` (six measured defects); `grade_wall_tiles.py:4-10`; `make_wall_tiles.py:4-13`; bible §16 |
| L18 | A deferral must be labelled **by decision, not by oversight**, or someone "fixes" it as a bug. | RULE | `seats.py` director mission (done) | bible §25; `design_equipment.md:292` |
| L19 | One data file is the source of truth for **both** the art path and the sim path. | RULE | tech workflow; strengthen | bible §17 (`data/floor_*.json` seeds both `bake_floor` and `floor_layout.gd`); `design_set_dressing.md:5`; `tools/rooms.py:5-8`; `bake_floor.py:51-56` |
| L20 | **Naming a thing to forbid it puts it in the picture**; the framing clause beats the description clause. | RULE | art workflow / `promptwriter.py` | `tools/gen_portraits.py:108-113` ("NO face, NO hair, NO skin" still produced a face with hair and skin), `:264-269` (the word "portrait" was the bug), `:74-77`, `:47-51`; `gen_held.py:36-38` |
| L21 | The house test contract: `PASS <name>` / `FAIL <name>: <why>` / `QA_RESULT pass=N fail=N`, non-zero exit. | TEMPLATE | `templates/shared/tests/` harness stub | `game/tests/loot_test.gd:19-25`, `:56`, identical in all 10 harnesses; `tests/exploration_test.gd:5-7`; fold-in idiom at `tests/combat_test.gd:609-630`; copyable boilerplate at `campaign_test.gd:11-15` |
| L22 | Price the option you are actually **testing**, and report quality-per-dollar. | CHECK | `bgate_core/spend.py` (mostly covered) | `tools/bakeoff.py:31-37` (ideogram charges 2.5x for exactly the field being tested), `:70-94` |
| L23 | Registration is not one number. Derive the placement number from the **same function that draws the art**. | GAME | stays | `tools/build_tileset.py:41-46`; `preview_floor.py:10-14`; `sync_tileset.py:5-18` (`texture_origin.x` never set, so every wall floated half a cell) |
| L24 | Isometric 2:1 projection contract, wall cutaway shader, plinth reconciliation, rotation classes, the 32px riser ladder. | GAME | stays | bible §13, §16, §17; `docs/SCALE.md` |
| L25 | The tutorial is protagonist-agnostic; casting is data. | GAME | stays | bible §24 |
| L26 | Every world fiction entity, quest, class, floor. | GAME | stays | 28 lore entities, 15 canon facts |

## The two highest-value unbuilt checks

### `asset_orphans` (L4)

Walk `game/**` for asset files (`.png`, `.ogg`, `.wav`, `.glb`, `.tres`) and
grep every `.tscn` / `.tres` / `.gd` for each file's `res://` path and its
`uid://`. Report three lists:

- **orphan**: on disk, no referrer
- **dangling**: referenced, not on disk
- **declared-but-empty**: tracked in the `asset` table, zero referrers

A path index plus a substring scan. No engine launch, no model call.

`asset_verify` (`bgate_core/assets.py:473-511`) compares tracked assets to disk by
content hash. It answers "did someone stomp this file", never "does anything
point at this file". `godot_check_project` catches dangling references at import
time and is silent on orphans, which is the direction that bit the game three
times.

### `content_determinism_check` (L1)

Scan every JSON/TRES key in a content directory against a forbidden-key list
(`chance`, `weight`, `weights`, `one_of`, `pick`, `random`, `roll`, `rolls`,
`rarity_roll`, `shuffle`, `variance`, `seed`), substring-matched so
`drop_chance` and `crit_chance` are both caught. Plus a source lint: assert
`randi` / `randf` / `randomize` / `RandomNumberGenerator` appear in no sim file
outside a declared allowlist, **with comments stripped first**, or a file that
argues about RNG trips its own check (`tests/exploration_test.gd:109-123`).

Nothing half-does this today. `canon_check` is the right shape to copy. The
cheaper move may be to stamp the loader guard into `templates/shared/` so a new
project's first content loader already rejects, rather than adding a tool an
agent has to remember to call.

## The rest of the proposals

- **`bible_add(acceptance=...)`** (L11). An optional field on `bible_section`,
  surfaced in `bible_read` and `seat_brief`. Not enforced. The game got this by
  convention alone, writing `ACCEPTANCE:` at the end of the body, which is an
  argument for a template over a schema change.
- **Reference weight split by kind** (L7). `ref_pin` already distinguishes
  `kind="style"` from `kind="character"`; nothing uses the kind to weight the
  call. At equal strength the identity signal wins and the style ref transfers
  its subject. Measured: 0.62 shared produced four subjects rendered as the
  anchor; 0.28 for the style anchor worked, and the closer a subject sits to the
  anchor the *less* anchor it can take (`gen_portraits.py:308-326`).
- **`templates/shared/tests/`** (L21). One `SceneTree` harness with the
  `check()` / `QA_RESULT` contract and the sub-suite fold-in idiom. The game had
  zero test-framework code; ten suites hand-rolled the same 8 lines.
  `bgate_core/scaffold.py:66-83` already overlays `shared/` wholesale, so this is
  a file drop.
- **`templates/shared/design/_spec.md`** (L10). Status / Grounded in / Scope /
  the design question / what we lose honestly / explicitly rejected / the seams /
  LEFTOVERS / where I am guessing. All 17 design docs converged on this shape
  independently.
- **A `Determinism` stub in the bible written by `project_init`** (L1). The
  game's bible §9 was written on day one and cited by five loaders.

## If the owner does only three

1. **`asset_orphans`**. The only lesson here where the failure is completely
   silent in every existing tool: the file is on disk, the hash matches,
   `godot_check_project` is green, and the feature does not exist. It bit the
   game in art, in audio and in cover data independently.
2. **The two mission sentences** (L1, L2). Already landed. Zero build cost, and
   they ride into every future project's `seat_brief`.
3. **`templates/shared/tests/` plus the spec skeleton** (L21, L10). Both are
   file drops into a tree `scaffold.py` already copies.

L7 and L20 are art-path tuning, L12 to L17 are workflow prose that can wait for
the next seat-mission edit, and L23 to L26 are the game's own.

## What was expected and not found

- **No root `CLAUDE.md` in the game project.** `bgate adopt` stamps one. Its
  operating knowledge went into the bible instead, so none of it was visible to
  a session that did not call `bible_read`.
- **No orphan detection anywhere**, in either repo. Both have the dangling
  direction, neither has the orphan direction.
- **No audio evidence.** The game has no audio pipeline, no audio tools, and the
  `audio` seat left no notes. L4 rests on art (`gen_portraits.py:234-237`) and
  cover data (`fix_cover.py:3-7`).
- **`playtest_item` is empty**, 0 rows. The playtest/telemetry loop went
  entirely unused on the project that most needed it. That is a finding about
  adoption, and worth asking why before building anything else.
