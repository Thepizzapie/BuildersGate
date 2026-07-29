<!-- Stamped here by Builders Gate (`bgate init` / `bgate adopt`). It tells a
Claude Code session how to work in THIS game project. Everything below names
tools that really exist on the `builders-gate` MCP server — if a tool name here
does not appear in your tool list, the server is not connected; say so instead
of improvising. -->

# __PROJECT_NAME__ — how to work in this project

You are working on a game. This repo is wired to **Builders Gate**, an MCP
server that holds the design decisions, the work queue, the art rules and the
proof that things actually run. It is not a linter and not a framework — it is
the shared memory that stops a long-running game project from contradicting
itself.

**Never used this before? Read to "The loop", do that, ignore the rest until
you need it.**

Start every fresh session with `project_status`, then `queue_list`. If those
two tools are missing from your tool list, the MCP server is not connected —
tell the user to run `bgate serve` / check their `.mcp.json`, and stop.

## The vocabulary, in one line each

- **Bible** — the design decisions that are settled. Pillars, scope tiers, art
  direction. Read it before you design anything.
- **Lore** — the world's facts: characters, places, factions, items. Facts can
  be *locked*, meaning they are canon and you may not contradict them.
- **Seat** — a role you adopt while working (art, gameplay, ...). A seat has a
  mission and a set of files it is allowed to write.
- **Work item** — one task, in a queue, owned by a seat. Work is claimed and
  closed through the queue, not done ad hoc.
- **Reference (ref)** — a pinned image that later art is generated *against*, so
  the game keeps one look.
- **Lock** — a claim on a binary file (a .png, a .blend) so two seats don't
  overwrite each other. Binaries do not merge.

## The loop

This is the whole working cycle. Do it in this order.

1. **Orient.** `project_status`, `bible_read`, `queue_list`.
2. **Create work.** `queue_add(seat=..., title=..., brief=..., priority=...)`.
   One item = one deliverable a person could check.
3. **Get it run.** With `bgate serve` up, a queued item is picked up and executed
   by a spawned agent holding that seat — that is the path that gets a QA gate,
   and it is the one to use. `queue_next(seat)` only *reads* the highest-priority
   queued item for that seat: it is a peek, not a claim, and it marks nothing, so
   two agents calling it get the same row. Either way, do not just start editing
   files because you saw a TODO.
4. **Adopt the seat.** `seat_brief(role)` gives you that seat's mission, its
   write lanes, and the bible/canon context it needs. Read it before touching
   anything.
5. **Work.** Before writing a file, `seat_can_write(role, path)` if you are at
   all unsure — it answers with the lane rule AND the lock state.
6. **Prove it.** Whatever you changed, produce evidence:
   `godot_check_project` (does the project still import and open),
   `godot_screenshot` / `godot_evidence` (does it look right in the actual
   engine), `consistency_check` (art), your own tests (qa).
7. **Close it.** `queue_complete(item_id, result="...")` — `result` is what you
   did and what proves it. `failed=True` if it didn't work; a failed item
   closed honestly is worth more than a green one that lied.
   `queue_reopen(item_id, reason)` if it comes back.

## Seats

Seven, fixed. `seat_list` for the live config, `seat_brief(role)` before you
work as one.

| seat | owns |
| --- | --- |
| `director` | pillars, scope, the cut line, arbitrating conflicts |
| `narrative` | lore graph, quests, dialogue |
| `gameplay` | mechanics, systems, game feel |
| `tech` | engine, build, performance, project plumbing |
| `art` | models, textures, sprites, the look |
| `audio` | SFX, music hooks |
| `qa` | tests, repro, and the picky gate before anyone says "done" |

Seats leave each other notes with `seat_post_note` / `seat_notes` — use it for
"I changed the player collider, gameplay should re-tune the jump", not chat.

### Leftovers: the integration you do not own

You will constantly finish a system that needs one line in a file belonging to
another seat. Do not half-land it and do not silently skip it. Write a
**LEFTOVERS** block at the top of the file you *do* own, and put the same list
in your spec:

```
## LEFTOVERS: the integration this file deliberately does not make.
## combat.gd is owned by another seat. Each is one line at a named site,
## and NONE of them is written here.
##
##   combat.gd, the KO branch of _apply_damage():
##     _inv.drop_all(u.id, u.pos)
```

One line, one named call site, verbatim. When somebody lands it, rewrite the
entry in place with the date and what replaced it. Do not delete it. A
leftover that vanishes silently teaches nobody anything, and a half-landed
integration is indistinguishable from a bug.

## Bible and lore — read before designing

`bible_read()` first, every time you are about to design something. It is
cheap and it is the difference between building the game and building a
different game next to it.

- Add a settled decision: `bible_add(kind, title, body, rank)`. `rank` is the
  scope tier — `scope_check(rank)` tells you whether something at that tier is
  above the cut line (i.e. whether it gets built at all).
- Add world facts: `lore_add(kind, name, summary)`, then `lore_fact(ref,
  statement, locked=True)` for anything that is now canon, and `lore_link` for
  relationships. `lore_brief(ref)` / `lore_list` to read back.
- **Before you write any narrative text, run `canon_check(text)`.** It tells you
  whether the text contradicts a locked fact. Do not skip it and do not argue
  with it — if canon is wrong, change canon deliberately with `lore_update`.
- `recall(query)` searches everything the project knows. Use it before asking
  the user a question they have already answered.

## Building scenes

The rule that makes it work: **a scene is made of nodes, not layers.** Someone
has to open what you build and change it without you in the room.

1. **One thing, one node.** Anything a person might select, move, rename,
   re-skin, script or delete is its own node in the `.tscn` — props, characters,
   interactables, spawn points, lights, triggers, cameras. A tile index inside a
   packed array is not something you can click, name, or give a property to.
2. **`TileMapLayer` is for terrain.** Floor, walls, ceiling — surfaces where the
   unit of editing genuinely *is* the tile. It is not a container for objects. A
   layer named `Props` or `Decor` is this rule already broken.
3. **Instance, don't duplicate.** Repeated content goes in as
   `instance=ExtResource("...")` pointing at a source scene, so fixing the source
   fixes all forty placements. Never paste a subtree.
4. **Give nodes meaningful names.** `Desk_03`, `DoorEast`, `Spawn_Guard_A` are
   editable. `Node2D7` is not findable, not scriptable, and not reviewable.
5. **Node.add_child() is for the genuinely dynamic** — spawned enemies,
   projectiles, VFX, pooled effects. It is not how set dressing gets placed. If a
   script fills a container that a designer should be arranging by hand, that
   container is the bug.

This holds for generated scenes too. Generated is fine; monolithic is not. If a
baker or importer writes a scene, say so in a header comment and keep it
node-shaped anyway — and if a human is expected to arrange something the
generator also writes, the generator reads that arrangement back or hands
ownership over. Silently clobbering hand placement is the failure this section
exists to prevent.

Why any of this: a scene that is four big layers is a scene nobody can edit. The
authoring left the editor and moved into your code, where the designer cannot
reach it.

## Art pipeline

The rule that makes it work: **generate against a reference, never from scratch
twice.**

1. **Pin the reference.** `ref_pin(name, path, kind="style"|"character")`.
   `ref_list` to see what is already pinned. If the user gives you concept art,
   pin it — that is what makes it authoritative.
2. **Describe the recurring character once.** `profile_set(name, traits, style,
   negative)`, read back with `profile_get`. Now every generation of that
   character starts from the same description.
3. **Generate.** `image_generate(prompt, filename)` for a still,
   `image_edit(prompt, ref_images, filename)` to change an existing image, and
   `image_sprites(character_prompt, poses, name, ...)` for anything animated —
   it stitches a real sprite sheet plus the Godot `SpriteFrames` resource.
   Never hand-assemble an animation out of separate `image_generate` calls.
4. **Pass the gate.** `consistency_check(candidate_path, character)` compares
   the new art against the pinned reference and flags drift and alpha defects
   (white halos, background bleed, dirty alpha). Fix what it flags, re-run,
   and only then `art_qa_verdict(artifact_id, verdict)`.
5. **Land it in the engine.** `godot_import_asset`, then look at it:
   `godot_screenshot`. The engine's view is the truth, not the PNG in a folder.

Two rules that cost real money to learn:

- **A generation chain decays.** Never condition frame N on frame N-1. Every
  frame conditions on the pinned ref and one approved base, so there is no chain
  to drift along and no ordering effect. A three-frame test looks flawless
  because the drift starts at frame 2.
- **Generate the minimum, derive the rest.** Spend generation only on
  silhouettes that are genuinely new. Everything that is the same thing moved
  (an idle breath, a mirrored facing, a walk cycle off an idle) is a transform,
  and a transform is exactly consistent by construction where a regeneration is
  a new chance to become a different character.

Binaries: `asset_lock(path, seat)` before editing, `asset_release(path, seat)`
after, `asset_status` to see who holds what, `asset_verify` after any session
where several seats were working.

## Playtesting

`playtest_start(name)` → play the game → `playtest_stop()`. It records the
session and turns what was said into concrete items you can promote into the
queue with `playtest_promote`, or drop with `playtest_dismiss`. `playtest_brief`
reads one back. This is how "the jump feels floaty" becomes a work item sitting
next to the telemetry numbers that explain it.

## What NOT to do

- **Do not bypass the queue.** Work that was never a work item has no owner, no
  brief, and no record that it happened. If you find something to fix, `queue_add`
  it — then decide whether to do it now.
- **Do not write outside your seat's lanes,** and do not edit a file another
  seat has locked. `seat_can_write` answers both questions in one call. If the
  answer is no, the fix is a note or a queue item, not a workaround.
- **Do not invent canon.** If a name, a date, a relationship or a rule is not in
  the lore, it is not true yet. Ask the user, or add it deliberately with
  `lore_add` / `lore_fact`. `canon_check` before narrative text, always.
- **Do not skip the consistency gate** because the image "looks fine". Drift is
  invisible one asset at a time and obvious across twenty.
- **Do not build a scene out of a few big layers.** One editable thing is one
  named node; `TileMapLayer` is terrain, not a bucket for objects; and a script
  that add_child()s the set dressing has taken authoring away from the designer.
  See "Building scenes".
- **Do not claim something works because the code looks right.** Screenshot it,
  run it, `godot_check_project` it. Attach the evidence to `queue_complete`.
- **Do not trust a preview whose legend can go stale.** A plan render, a debug
  overlay or a contact sheet that classifies things from a hardcoded list will
  one day draw the wrong thing confidently, which is worse than drawing nothing.
  Derive what the preview labels from the same source the real thing reads.
- **Do not write an assertion that would still pass with the feature deleted.**
  `0 == 0` is green. A determinism check over a run that never used the new code
  is green. Pair every claim with a control that fails: a different seed that
  must disagree, a damaged save that must resume differently, a non-zero count.
- **Do not pin a fixture to whatever is least finished.** It breaks on the day
  that thing gets finished, which is the worst possible moment to be looking at
  a red test. Pin to something structurally guaranteed to hold.
- **Do not put a chance-shaped field in content data.** A key named "chance",
  "weight", "one_of", "roll" or "drop_chance" anywhere in a content record,
  top level, nested, or inside a stock line, should be rejected outright, not
  ignore them. Randomness lives in one declared seeded stream that serializes
  with the save, or nowhere. A silently-ignored roll is a table that lies.
- **Do not scaffold over an existing game.** `godot_scaffold` is for new
  projects. This one already exists.
- **Do not put API keys anywhere but `.env`** at this project's root. The
  `.gitignore` here already excludes it — keep it that way.

## When something is broken

`bgate_doctor` (or `bgate doctor` in a terminal) checks every external
dependency — Godot, Blender, image API keys — in one pass and says what is
missing and why. Run it before debugging anything that smells like environment.
`godot_status`, `blender_status`, `image_status` answer the same question one
tool at a time.
