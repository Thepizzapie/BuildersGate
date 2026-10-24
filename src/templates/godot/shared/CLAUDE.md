<!-- Stamped here by Builders Gate (`bgate init` / `bgate adopt`). It tells a
Claude Code session how to work in THIS game project. Tools named below exist
on the `builders-gate` MCP server, but each seat loads only its own crafts:
a tool missing from your list may belong to another seat (queue the work to
it) or be unlockable with `tool_unlock`. Only if `project_status` itself is
missing is the server not connected; say so and stop. -->

# __PROJECT_NAME__: how to work in this project

This game is wired to **Builders Gate**, an MCP server holding the settled
design (the bible), the world's facts (lore), the work queue, the art rules and
the proof that things run. It is the shared memory that stops a long project
contradicting itself.

## The loop

1. **Orient.** `project_status`, `bible_read`, `queue_list`. A hand-started
   session then INVENTORIES, one call each: `bgate_doctor`, `image_status`,
   `blender_status`, `pending_decisions`, `seat_notes`, `handoff_read`,
   `ref_list`. `available: true` is a capability to USE, not a gate you
   passed. A dispatched run already has its seat's short brief in its prompt;
   `seat_brief(role)` is for when you need more.
2. **Create work.** `queue_add(seat, title, brief)`. One item, one deliverable
   a person could check. With `bgate serve` up, a spawned agent holding that
   seat runs it and QA checks it; that is the path to use.
3. **Work.** `seat_can_write(role, path)` when unsure of a path: it answers the
   lane rule and the lock state. Before writing a system, `kit_list`: it may be
   one `kit_install(name)` away.
4. **Prove it.** `godot_check_project` (it still opens), `godot_screenshot` /
   `godot_evidence` (it looks right in the engine), `consistency_check` (art),
   your own tests (qa).
5. **Close it.** `queue_complete(item_id, result="...")`, where `result` says
   what you did and what proves it. `failed=True` if it did not work; an
   honest failure beats a green item that lied. `queue_reopen(item_id, reason)`
   if it comes back. Re-read `queue_list` after each `queue_complete`: closing
   a chain link dispatches the next one.

## Seats

`seat_list` for the live config. director (pillars, priorities, arbitration),
narrative (lore, quests, dialogue), gameplay (mechanics, feel), level (layout,
pacing, encounters), tech (engine, build, performance), art (models, sprites,
the look), audio, cinematic, qa (tests, repro, the gate before "done").

**Integration you do not own: QUEUE IT.** When your system needs a line in
another seat's file, `queue_add(that seat, title, brief)` naming the file, the
call site and the exact line. A code comment, a `seat_post_note` or a
"blocked" result dispatches nobody: measured on a real board, four generated
assets were never wired in because the request was a note, not a job.

## Bible and lore: read before designing

- `bible_read()` before designing anything. Settled decisions go in with
  `bible_add`; world facts with `lore_add`, then `lore_fact(..., locked=True)`
  for canon.
- **Run `canon_check(text)` before writing any narrative text.** If canon is
  wrong, change it deliberately with `lore_update`; do not argue with it.
- `recall(query)` searches everything the project knows. Use it before asking
  the user something they already answered.

## Building scenes: nodes, not layers

1. Anything a person might select, move, rename, re-skin, script or delete is
   its own named node (`Desk_03`, `DoorEast`), not a tile index or `Node2D7`.
2. `TileMapLayer` is terrain only. A layer named `Props` is the rule broken.
3. Repeated content is `instance=ExtResource(...)` of a source scene, never a
   pasted subtree.
4. `Node.add_child()` is for the genuinely dynamic (spawns, projectiles, VFX), not
   set dressing. Generated scenes stay node-shaped and never clobber hand
   placement.

## Art: generate against a reference

1. `ref_pin(name, path, kind)` the reference; concept art the user gives you
   is pinned, which makes it authoritative.
2. `image_sprites(...)` for anything animated (a real sheet plus the Godot
   `SpriteFrames`); never hand-assemble frames from `image_generate` calls.
3. `consistency_check(candidate_path, character)` before anything lands; fix
   what it flags. Then `godot_import_asset` and `godot_screenshot`: the
   engine's view is the truth.
4. A generation chain decays: every frame conditions on the pinned ref and one
   approved base, never on frame N-1. Generate only new silhouettes; mirrors,
   breaths and facings are transforms.
5. Binaries: `asset_lock(path, seat)` before editing, `asset_release` after.

Playtests: `playtest_start` / `playtest_stop`, then `playtest_promote` what was
said into the queue.

## What NOT to do

- **Do not bypass the queue.** Found something? `queue_add` it, then decide
  whether to do it now.
- **Do not write outside your lanes** or into a file another seat has locked.
- **Do not invent canon.** Not in the lore means not true yet.
- **Do not skip the consistency gate** because an image "looks fine"; drift is
  invisible one asset at a time.
- **Do not claim it works because the code looks right.** Run it, screenshot
  it, attach the evidence to `queue_complete`.
- **Do not write an assertion that passes with the feature deleted.** Pair
  every claim with a control that must fail.
- **Do not trust a preview whose legend can go stale.** Derive its labels from
  the source the real thing reads.
- **Do not put a chance-shaped field** ("chance", "weight", "roll") in content
  data. Randomness lives in one seeded stream that serializes with the save.
- **Do not scaffold over this game** (`godot_scaffold` is for new projects),
  and keep API keys in the gitignored `.env` only.

Environment trouble: `bgate_doctor` checks Godot, Blender and API keys in one
pass.
