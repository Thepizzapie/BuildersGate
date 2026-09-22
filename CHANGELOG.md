# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released to a package index yet; `0.1.0` is the state of the
repository at first publication. There is no earlier release history to record.

## [Unreleased]

### Added
- **A re-run behind a fix does not count against the run cap.** The graybox
  gate found a wall per run and was told "no more runs" on the fourth, and
  the QA agent that found the wall could not file the fix because two runs
  hours earlier had spent its filing cap. `queue_reopen(after=<fix id>)`
  hangs the reopened item behind the fix, resets its run count and refuses
  when the fix is already done or cancelled. The per-agent filing cap of two
  now counts items filed during the CURRENT run, not the item's whole life.
- **A run cap per work item, for everyone.** One item ran nine times
  (~$33) through its auto-retry, a director reopen and two dashboard
  send-backs, and no run could land it because the brief was five
  deliverables wide. `dispatch.max_attempts` (3 by default, human-only)
  counts every run in an item's life: past it autopilot never lists the
  item, the dispatch button refuses (`attempt_cap`), `queue_reopen` refuses
  with the split it wants instead, the QA gate and the follow-up router
  stop reopening, and the failure escalation cancels the item and asks the
  director for a chain of single-deliverable items. A parked or cancelled
  item stays that way when a killed run is banked as failed; that overwrite
  is how a parked item grew a reopen button. And the board never stays
  stuck: when autopilot idles behind a dead link it files one UNBLOCK item
  for the director (close it as superseded, reopen with a changed brief,
  split it and re-hang the waiters, or cancel the waiters; ask the human
  only when the board cannot decide), dispatched like any other and never
  filed twice; the director protocol makes that job the director's, not
  the human's.
  Above all, a dead item holds nothing: cancelling, parking or exhausting
  an item cuts every queued successor loose at once, with what the dead
  item left on disk written into the successor's brief, so a chain never
  waits on something that will not land.
  Every failure reaches the director: a closed escalation no longer
  silences the next failure of the same item (one OPEN escalation dedups, a
  closed one does not), the escalation brief says done means something is
  ready and leads with file-the-fix-and-hang-this-behind-it, and the
  QA-loop escalation is dispatched to the director seat instead of held for
  a person.
- **A chain behind a dead link says so.** A six-link chain's head failed,
  the director's escalation filed a replacement item that did the head's
  job, and the five links behind the head sat queued and silent for an hour
  while the board read as stuck, because it was. `queue.blocked_chains`
  names every queued item waiting on a FAILED, PARKED or CANCELLED link;
  autopilot, when it has nothing to dispatch, announces that once as a
  `dispatch.blocked` event and an activity line with the three ways out
  (reopen the link, close it as superseded, cut the dependency);
  `board_digest` reports the dead link instead of blaming the dashboard or
  autopilot; and a failure escalation names what the failure is holding and
  tells the director that a replacement item does not release it.
- **Fun, then functional, then pretty.** Holding the art seat kept art items
  off the board; it never stopped the director, or a gameplay agent with a
  prompt in hand, from calling `image_sprites` at the graybox stage. While a
  project is at thesis or graybox, every paid generation (image, 3D,
  animation, music, video, speech) now refuses for every caller at the
  provider preflight, with the way through in the message; primitives,
  blockouts and placeholder sprites never touch a provider and stay open.
  Only the human rules on the graybox or moves a project forward a stage
  (backward stays open to any seat). It is one route, not the only one:
  `greenlight.generation_hold` is a per-project switch, on for new projects,
  off for a game whose art is the mechanic. A scratch project is never held.
- **A chain is for order, not for lists.** Ten independent cutout rigs
  filed as a ten-deep ladder ran one at a time on a two-slot board. A chain
  link now waits on the one before it only when it is a seat handoff, its
  brief names the predecessor, or it says `after: true`; same-seat links
  that do not mention each other run beside each other, hanging off the
  handoff above them. `mode="linear"` keeps the old ladder. The art seat's
  default concurrency is two (the shared-upload collision that set it to one
  is fixed and the frame gate is in front of it now).
- **Every worker seat picks its own CLI and model.** Routing used to stop
  at the art seat on purpose. `dispatch.runner` (claude | codex) is the
  board default and each seat has `dispatch.runner_<seat>` and
  `dispatch.model_<seat>` (blank inherits); art keeps `art.runner` and
  `dispatch.model_art`. `dispatch.codex_model` names what a Codex-run seat
  gets when its model setting is a Claude alias, before Codex's own catalog
  default. The trade a seat makes on codex (no live steering, no cost
  ceiling) is stated in the settings' help rather than refused by the code.
- **A constraint the human stated is a ruling, and the harness enforces it.**
  On the EXIT 67 build the human said on night one that a run-and-gun needs
  a 2D rig and that generated frame sheets would not carry it; the director
  answered "trust the process", dispatched the cast on `image_sprites`, and
  every art failure of the next twelve hours followed. The bible had a
  `constraint` kind with no idea who stated it. Bible sections now carry
  `stated_by`, `binds` (seats) and `forbids` (MCP tools); `bible_add` and the
  dashboard's constraint rows take them, and only a human session may write
  `stated_by='human'`. A human ruling reaches a bound seat three ways: it is
  printed under the work item in the dispatch prompt and untrimmed in
  `seat_brief` (`rulings`); a forbidden tool refuses the seat's call at the
  tool wrapper; and dispatch refuses a brief that names a forbidden tool
  (`forbidden_by_ruling`, per item, not a floor). The director protocol says
  to record the ruling before filing the work. (`bgate_core.design.bible`,
  migration 0047)
- **`cutout_kit_generate` and `cutout_part_rerun`: the cutout rig generates
  its own parts.** The rig, emitter and animation library shipped in July;
  the parts were still made by hand and no project used it. One call now
  draws every part of a template from ONE pinned reference through the
  keyed-background contract, trims each to its alpha box, checks its height
  against the reference figure (flagged, never rescaled), records the
  reference hash on every part, assembles and emits. It refuses over
  `max_paid_calls` before buying anything and stops after two consecutive
  provider failures. `cutout_part_rerun` redraws one part. `cutout_status`
  gains `stale_reference` (a part from another run or character) and
  `reference_moved`. The biped template gains wrist bones and hand slots;
  the weapon hangs from the near hand so the grip is inside the hand in
  every frame, and an `aim` clip braces both hands on it - the pose the
  frame pipeline could not hold. (`bgate_core.three_d.cutoutkit`)
- **The human's own gripes after EXIT 67: what is current, who to ask,
  how long an item gets, how big an item may be.** `project_current` and a
  block under every dispatched item name the files changed OUTSIDE the board
  in the last hours and who changed them, so an agent never edits a file the
  human just touched by hand without knowing. The dispatch prompt says to ask
  the moment a question arises and that `ask_director` is the default; an
  open director question shows in `pending_decisions` and `board_digest` and
  escalates to the human after ten minutes of director silence; a landed
  item's commit steers every running agent whose lane it touched. Items
  carry `size` and `acceptance`; size derives the runtime ceiling and turn
  cap, the harness steers "land what you have" at 60 and 85 percent of the
  budget, running rows and the card show elapsed against ceiling, and a
  small item whose result names a passing check skips the QA agent.
  `queue_add` refuses a broad or acceptance-less brief from an agent (split
  it with `queue_add_chain`) and warns the director. Per-attempt cost sits
  on the item card; events carry the attempt number. Leftovers landed too:
  `artifacts.sweep_stale` moves a superseded name's loose files to
  `.bgate_out/.stale/`, `godot_export_verify` boots the exported pck
  headless and fails on a SCRIPT ERROR, a builder's-gate rejection counts
  toward the tool's human-rejection stop, `item_to_spriteframes` conforms a
  generated icon to the pinned palette, and the sprite contract carries
  `subject_class` for the prop gate.
- **The EXIT 67 refinement pass: thirty defects from one night, fixed
  across the harness.** A Metal Slug style 2D run-and-gun was built over
  twelve hours by ~33 agents and failed on sight. Every item below names
  what was measured.
  - *Art gates* (`bgate_core.art.framegate`): a per-frame gate against the
    character's OWN anchor fails a frame - silhouette and palette drift,
    translucent ghost limbs, a second head above the shoulders, a detached
    object, near-duplicate adjacent frames, a prop that grew limbs. Every
    pose and reference view written by `image_sprites` carries an
    anchor-hash sidecar; stale frames are dropped before stitching and the
    sheet fails with per-frame verdicts, which `queue_reopen` now carries
    into the reopened brief. `gear.stamp_generated` conforms a stamped
    weapon to the pinned palette and flags ink weight.
  - *Export, not editor* (`bgate_core.qa.exportlint`, `exportgate`):
    `godot_check_project` lints `get_files()` filtered on `.tres`/`.tscn`
    without a `.remap` strip, zero-width and BOM literals, and unfiltered
    `res://` FileAccess reads - the three patterns that shipped an empty
    export; the 2D template gains `ResDir.files()`; the release
    presentation check refuses a project whose export is unverified or
    stale. `godot_run`/`godot_test_run` take `time_scale` (8 for drives);
    identical runs in one item are refused on the third; the engine lock
    reports a `self_collision`; inflight rows show "waiting on Godot pid
    N". `scale_contract_set` refuses ratio classes that look like pixels.
  - *Board plumbing*: leases die with the process and never gate on a path
    merely named in a brief; `dispatch.max_per_seat` (art=1); `parked`
    status with `queue_park`/`queue_unpark`/`queue_cancel`; a chained
    agent's death is attributed to the item it holds; auto-commit names
    every item the run claimed; a usage-limit message floors dispatch for
    that runner, requeues instead of failing, and resumes on its own;
    runtime ceiling 9800 s / 800 turns; temp-dir registrations lose to
    real projects; `board_digest` rows carry the attempt number.
  - *Money and payload*: every list-shaped tool result is capped at 40 KB
    with a "more" pointer and `asset_status`/`asset_verify` page; each work
    item has `max_paid_calls` (default 30) enforced where every paid
    generation passes, and the same name generated three times in one item
    needs a `replace_reason`; dispatch refuses a seat whose keyed provider
    is drained and the brief carries the provider table; artifact revisions
    keep a content-hash copy so the gallery never shows a stale file under
    a shared path.
  - *Director and review*: `sprite_sheet_check` renders review pages at
    >= 300 px per frame; three human rejections of one tool's output block
    that tool for dispatched seats with a STOP AND ASK line until
    `rejections_clear`; the QA gate reopens an art item whose result has no
    per-frame verdict; `board_focus_set` names the one slice in progress
    and `queue_add` warns on a brief that names another; `concept_compare`
    composes a candidate beside the pinned concept with measured deltas.
- **Project kickoff from the brief and the screenshots.** Every project so
  far started the same way: a long brief pasted into the director chat with
  a row of reference images and "pin these in the bible", one screen after a
  create card that took a 200-character pitch. The create card now takes the
  whole paste - a brief box (screenshots pasted into it become references)
  and an image drop - and the server does what the human did by hand: the
  brief goes to `design/brief.md` with a `reference` bible section pointing
  at it, each image is pinned as a concept ref and anchored to a second
  section, a `next` note lands on the thread, and the director is started on
  the same first turn the human used to type (read the brief, settle the
  thesis, write the bible, record the not-building list, lay the board out
  as chains, ask only what the brief leaves open). `bgate init` and
  `bgate adopt` take `--brief FILE` and repeatable `--ref IMAGE` and seed the
  same way, leaving the kickoff on the thread for whoever opens the chat.
  Adopt over HTTP takes the same fields. (`bgate_core.design.kickoff`)

### Fixed
- `room_build` wrote its scene and then returned a failure: the writelog
  helper was called with one argument against a two-argument signature, so
  every successful build raised inside its own `try`, and the scene write
  never reached the writelog the evidence gate and auto-commit read.
- **`traversal_prove` drove the player wrong, and three agents paid for it.**
  The bot driver released every action and pressed the block's actions
  again on every physics frame; Godot 4.4 buffers those a frame, so a
  controller polling `is_action_just_pressed` saw a jump only when its block
  ENDED, two frames late and from the wrong ground, and a six-block program
  read as "only the first two applied". EXIT 67 r2's stage item failed three
  attempts ($14) on it: one agent blamed the level, one the controller's
  `is_in_scripted_move`, and the director's escalation blamed the driver,
  correctly. A block now presses its actions once at its start and releases
  them once at its end, mirrored through an `InputEventAction` so
  event-driven controllers see it too; every trace sample carries the block
  being held and the body's position; an in-engine test drives a
  run-jump-run route over a gap and asserts the jump fires inside its block.

### Changed
- **2D space and sheets, from one night's failures.** The benchmark game
  shipped complete with its towns graybox, its party four sizes of person
  under three-times-taller enemies, an NPC walled in and a spawn under a
  weighbridge, and 211 asset revisions for 181 names. Four detailed fixes:
  - `room_build` / `room_audit`: a designed top-down room from an ASCII
    plan (walls, floor variants, manifest props with footprints, instanced
    markers at cell centres), refusing a marker in a wall or a prop off the
    floor BEFORE the scene is written; and the space audit over any scene,
    hand-baked included - markers on the map, none inside a solid, every
    NPC/sign/chest/entrance/exit reachable from the spawn, with an ASCII
    picture of what the player can actually stand on.
  - `sprite_fit` / `sprite_family_check`: one standing height per character
    (idle-anchored, same factor for every frame, feet on one row, palette
    snapped, overflow reported never cropped), and the family read together
    - heights within 6%, feet within 2px, on the pinned palette, no colour
    a sibling lacks. image_sprites and animation_generate fit on landing
    when the sprite contract declares `standing_px`.
  - The sprite contract scopes `cell`, `view`, `layout`, `standing_px` and
    `feet_row` per character and per action (a 128x128 side-view battle
    sheet beside a 32x32 overworld one, for one person), `sprite_contract_set`
    merges a patch instead of replacing it, and the scale contract takes
    `stages` - a player height per screen - with a `boss` class of its own;
    `image_generate(klass=, stage=)` shrinks an oversized enemy to the band
    on landing.
  - The regeneration gate: image_generate, image_edit and animation_generate
    refuse a logical name that already has an APPROVED revision unless
    `replace_reason` says what is wrong with it. Nothing spent, the live
    path returned.
  Briefs: art (the contract, the fit, the family, the gate), gameplay/level
  (room_build not a baker, room_audit on every field scene), QA (room_audit
  and the party family in the presentation gate).
- **The director's usage meters are the director's.** The Claude usage
  bridge's context figure - whatever Claude Code session last drew a status
  line - overwrote the director's own count (measured: 893k of 1000k shown
  for a session that had used 180k); the session's context now always wins
  and the bridge only supplies the 5h/weekly percentages, with the stream's
  newer status and reset riding on top. Every figure carries WHEN it was
  true: a bridge reading older than 15 minutes is dimmed with its age, a
  window whose reset passed says "reset" instead of the dash an unlinked
  bridge draws, and the bridge line says how old its last read is and that
  the status line only runs in a terminal `claude` session. Codex's context
  read the thread's running total (730k of a 272k window); it is the last
  turn's prompt now, against the window the server names.
- **`screen_audit`: the composition faults a screenshot shows.** Over one
  `godot_evidence` manifest - which now records the texture size behind every
  sprite and the string width behind every label - it reports `party_scale`,
  `scale_clash`, `label_overflow`, `font_scale` (a bitmap font drawn off its
  native multiple) and `pixel_density` (sprites at a different density than
  the backdrop). `godot_check_project` gains `presentation`: every
  `font_size` checked against every `.fnt`'s native size, and the default
  texture filter. MEASURED: a battle frame with every asset passing
  `scale_check` drew the party at 40px against 260px enemies and clipped the
  enemy name; nothing measured the frame.
- **A rejected asset cannot come back.** Reviewing a revision `rejected`, or
  approving a newer one, retires the old file in the canon with the live
  revision as its successor: the hook refuses a seated agent's read or write of
  it, and `scene_wire`, `scene_swap_resource`, `godot_import_asset` and
  `godot_deliver_asset` refuse to put it in the game, naming what to use
  instead. `asset_verify` reports `stale_wired`: every scene, resource or script
  still naming a rejected or superseded file. MEASURED: agents kept wiring
  rejected candidates because the reject was a row nothing on the write path
  read.
- **Codex workers: no reviewer, a real sandbox, and the hook.** `codex exec`
  seats no longer run with `--approve-for-me`. They run `workspace-write`
  with `approval_policy = never`, network off, and — on Windows —
  `windows.sandbox = "elevated"` passed explicitly, because
  `--ignore-user-config` had been dropping the user's sandbox backend and
  Codex was treating every command and patch as read-only-refused; the
  reviewer was approving ordinary commands out of a sandbox that was not
  there. The Builders Gate PreToolUse hook is now injected into every Codex
  seat with `-c hooks.PreToolUse` and `--dangerously-bypass-hook-trust`
  (the flag for automation that vets its own hooks), so containment, lanes,
  locks and the two gates below apply to Codex exactly as to Claude. The
  hook answers Codex with stdout JSON (`permissionDecision: deny`): measured
  on 0.154, a hook that exits 2 is logged and ignored. `apply_patch` is
  judged per file from the patch text (it arrives as `command`). Codex's
  MCP tools run under `default_tools_approval_mode = "approve"`, the one
  mode that answers under `never`. `dispatch.codex_auto_approve` now covers
  only the Codex Director console, and that console reads a command through
  the egress gate before accepting it: `pip install` from the project root
  goes back to the human.
- **Egress gate.** A seated agent may not run installers (pip, npm install,
  winget, cargo install, …), network clients (curl, wget, ssh,
  Invoke-WebRequest, …), `git push/clone/fetch/pull`, or another agent CLI.
  Refused by program name with a message that says what to do instead
  (`queue_complete(next_approach=…)`); logged to `.bgate/hook.log`. Read
  through `sudo`/`env`/`bash -c`/`eval`/`powershell -Command` and `-c`
  snippets. `dispatch.allow_egress` (machine, human-only, guarded) lifts it.
- **File text does not travel on a command line.** A seated agent that
  writes a file with `cat > f <<EOF`, `echo … > f`, `tee`, `Set-Content`,
  `Out-File` or `[IO.File]::Write*` is refused and sent to Write/Edit
  (Codex: apply_patch). Program output may still redirect (`godot … >
  run.log`). Observed 2026-09-16: a heredoc carrying download-and-run
  strings on a `bash -c` line was killed by Windows Defender as ClickFix;
  the shape is what any agent on any machine can reproduce, so the shape
  is what a seat is refused.
- **`godot_test_run` marker scan.** `failures=0` in a probe's summary and
  `PASS … (the fail state)` lines no longer count as FAIL markers; markers
  are counted per line, zero-count summaries are not failures.
- **Delivery preview frames the model where it is.** The photo stage aims
  at the model's measured box centre and puts the floor at its lowest
  point. With `normalize_origin` off (the default), a feet-at-zero rig was
  photographed floating with its head off the frame — every Meridian cast
  delivery — and then approved.
- **Artifacts with a failed required check are never auto-approved.**
  `art.auto_approve` / `gate.mode = none` waive the human gate, not the
  machine verdict; such a revision stays a candidate and the activity log
  says which check failed. The auto-approve note now names the setting that
  actually waived the gate.
- Frozen build: `BuildersGate.exe hook` hosts the PreToolUse hook, as `mcp`
  hosts the server.
### Added
- **Canon: which world is current, which files are retired.** A project
  now records the scene the game IS (`world`, defaulting to project.godot's
  main scene), named current files (`reporter`, `world_layout`, …) and a
  RETIRED list of paths/globs, each with its successor and reason
  (`bgate_core/board/canon.py`). It reaches agents three ways: printed at
  the top of every dispatched brief and in the SessionStart block; the
  `canon_status` / `canon_audit` tools (every seat) with `canon_set` /
  `canon_retire` / `canon_unretire` (refused to a seated worker — what is
  current is the director's and the human's call); and the PreToolUse hook,
  which refuses a seat's READ or write of a retired path and names the
  successor — reads too, because the way agents ended up extending a
  retired map was by reading it as the example. `canon_audit` reads the
  tree against the list: scenes/scripts still referencing retired paths,
  scene basenames living under two directories, and scenes/scripts nothing
  reaches from the world, the autoloads or a tool (through ext_resource,
  `res://` strings, directory prefixes a script builds paths from, and
  `class_name`). Observed on Meridian: forty items dispatched into a tree
  carrying the first street beside the island; 58 references to
  `main.tscn` in one agent's log, 88 to a retired module in another.
- **`/api/play/pad`: a touch layout from the input map.** A d-pad and up
  to six buttons derived from the project's own actions (builtins included,
  with their real physical keycodes), each carrying the DOM `code`/`key`/
  `keyCode` the web build's key listener reads - so a phone can play a
  keyboard game with no per-project mapping.
- **The phone can play the build.** `/play/*` and the game's telemetry POST
  accept the phone token as a cookie (`bgate_phone`) from the tailnet side,
  because the engine's own `.wasm`/`.pck` fetches cannot carry a header. The
  cookie opens the build and nothing else.
- **Settings > Phone: the companion app's door, from the desk.** The phone
  now has its own token (`~/.bgate/remote-token`, machine-wide so switching
  projects from the desk or the phone keeps it paired), not the dashboard's, so it
  can be rotated - new token, new QR, every phone cut off - without logging
  the desktop out of itself. The tailnet side is admitted per request, so it
  can be closed and reopened from the panel without a restart, and every
  method from that side needs the phone token (a GET of `/api/state` is the
  whole project). Every device that connects is seen - address, user agent,
  requests, last path, online or idle - and can be revoked on its own, or
  forgotten. `/api/remote*` is loopback-only: from the phone it does not
  exist. Existing phones re-pair once, since their token was the old one.
- **Codex workers: no reviewer, a real sandbox, and the hook.** `codex exec`
  seats no longer run with `--approve-for-me`. They run `workspace-write`
  with `approval_policy = never`, network off, and — on Windows —
  `windows.sandbox = "elevated"` passed explicitly, because
  `--ignore-user-config` had been dropping the user's sandbox backend and
  Codex was treating every command and patch as read-only-refused; the
  reviewer was approving ordinary commands out of a sandbox that was not
  there. The Builders Gate PreToolUse hook is now injected into every Codex
  seat with `-c hooks.PreToolUse` and `--dangerously-bypass-hook-trust`
  (the flag for automation that vets its own hooks), so containment, lanes,
  locks and the two gates below apply to Codex exactly as to Claude. The
  hook answers Codex with stdout JSON (`permissionDecision: deny`): measured
  on 0.154, a hook that exits 2 is logged and ignored. `apply_patch` is
  judged per file from the patch text (it arrives as `command`). Codex's
  MCP tools run under `default_tools_approval_mode = "approve"`, the one
  mode that answers under `never`. `dispatch.codex_auto_approve` now covers
  only the Codex Director console, and that console reads a command through
  the egress gate before accepting it: `pip install` from the project root
  goes back to the human.
- **Egress gate.** A seated agent may not run installers (pip, npm install,
  winget, cargo install, …), network clients (curl, wget, ssh,
  Invoke-WebRequest, …), `git push/clone/fetch/pull`, or another agent CLI.
  Refused by program name with a message that says what to do instead
  (`queue_complete(next_approach=…)`); logged to `.bgate/hook.log`. Read
  through `sudo`/`env`/`bash -c`/`eval`/`powershell -Command` and `-c`
  snippets. `dispatch.allow_egress` (machine, human-only, guarded) lifts it.
- **File text does not travel on a command line.** A seated agent that
  writes a file with `cat > f <<EOF`, `echo … > f`, `tee`, `Set-Content`,
  `Out-File` or `[IO.File]::Write*` is refused and sent to Write/Edit
  (Codex: apply_patch). Program output may still redirect (`godot … >
  run.log`). Observed 2026-09-16: a heredoc carrying download-and-run
  strings on a `bash -c` line was killed by Windows Defender as ClickFix;
  the shape is what any agent on any machine can reproduce, so the shape
  is what a seat is refused.
- **`godot_test_run` marker scan.** `failures=0` in a probe's summary and
  `PASS … (the fail state)` lines no longer count as FAIL markers; markers
  are counted per line, zero-count summaries are not failures.
- **Delivery preview frames the model where it is.** The photo stage aims
  at the model's measured box centre and puts the floor at its lowest
  point. With `normalize_origin` off (the default), a feet-at-zero rig was
  photographed floating with its head off the frame — every Meridian cast
  delivery — and then approved.
- **Artifacts with a failed required check are never auto-approved.**
  `art.auto_approve` / `gate.mode = none` waive the human gate, not the
  machine verdict; such a revision stays a candidate and the activity log
  says which check failed. The auto-approve note now names the setting that
  actually waived the gate.
- Frozen build: `BuildersGate.exe hook` hosts the PreToolUse hook, as `mcp`
  hosts the server.
### Added
- **Canon: which world is current, which files are retired.** A project
  now records the scene the game IS (`world`, defaulting to project.godot's
  main scene), named current files (`reporter`, `world_layout`, …) and a
  RETIRED list of paths/globs, each with its successor and reason
  (`bgate_core/board/canon.py`). It reaches agents three ways: printed at
  the top of every dispatched brief and in the SessionStart block; the
  `canon_status` / `canon_audit` tools (every seat) with `canon_set` /
  `canon_retire` / `canon_unretire` (refused to a seated worker — what is
  current is the director's and the human's call); and the PreToolUse hook,
  which refuses a seat's READ or write of a retired path and names the
  successor — reads too, because the way agents ended up extending a
  retired map was by reading it as the example. `canon_audit` reads the
  tree against the list: scenes/scripts still referencing retired paths,
  scene basenames living under two directories, and scenes/scripts nothing
  reaches from the world, the autoloads or a tool (through ext_resource,
  `res://` strings, directory prefixes a script builds paths from, and
  `class_name`). Observed on Meridian: forty items dispatched into a tree
  carrying the first street beside the island; 58 references to
  `main.tscn` in one agent's log, 88 to a retired module in another.
### Changed
- **Codex workers: no reviewer, a real sandbox, and the hook.** `codex exec`
  seats no longer run with `--approve-for-me`. They run `workspace-write`
  with `approval_policy = never`, network off, and — on Windows —
  `windows.sandbox = "elevated"` passed explicitly, because
  `--ignore-user-config` had been dropping the user's sandbox backend and
  Codex was treating every command and patch as read-only-refused; the
  reviewer was approving ordinary commands out of a sandbox that was not
  there. The Builders Gate PreToolUse hook is now injected into every Codex
  seat with `-c hooks.PreToolUse` and `--dangerously-bypass-hook-trust`
  (the flag for automation that vets its own hooks), so containment, lanes,
  locks and the two gates below apply to Codex exactly as to Claude. The
  hook answers Codex with stdout JSON (`permissionDecision: deny`): measured
  on 0.154, a hook that exits 2 is logged and ignored. `apply_patch` is
  judged per file from the patch text (it arrives as `command`). Codex's
  MCP tools run under `default_tools_approval_mode = "approve"`, the one
  mode that answers under `never`. `dispatch.codex_auto_approve` now covers
  only the Codex Director console, and that console reads a command through
  the egress gate before accepting it: `pip install` from the project root
  goes back to the human.
- **Egress gate.** A seated agent may not run installers (pip, npm install,
  winget, cargo install, …), network clients (curl, wget, ssh,
  Invoke-WebRequest, …), `git push/clone/fetch/pull`, or another agent CLI.
  Refused by program name with a message that says what to do instead
  (`queue_complete(next_approach=…)`); logged to `.bgate/hook.log`. Read
  through `sudo`/`env`/`bash -c`/`eval`/`powershell -Command` and `-c`
  snippets. `dispatch.allow_egress` (machine, human-only, guarded) lifts it.
- **File text does not travel on a command line.** A seated agent that
  writes a file with `cat > f <<EOF`, `echo … > f`, `tee`, `Set-Content`,
  `Out-File` or `[IO.File]::Write*` is refused and sent to Write/Edit
  (Codex: apply_patch). Program output may still redirect (`godot … >
  run.log`). Observed 2026-09-16: a heredoc carrying download-and-run
  strings on a `bash -c` line was killed by Windows Defender as ClickFix;
  the shape is what any agent on any machine can reproduce, so the shape
  is what a seat is refused.
- **`godot_test_run` marker scan.** `failures=0` in a probe's summary and
  `PASS … (the fail state)` lines no longer count as FAIL markers; markers
  are counted per line, zero-count summaries are not failures.
- **Delivery preview frames the model where it is.** The photo stage aims
  at the model's measured box centre and puts the floor at its lowest
  point. With `normalize_origin` off (the default), a feet-at-zero rig was
  photographed floating with its head off the frame — every Meridian cast
  delivery — and then approved.
- **Artifacts with a failed required check are never auto-approved.**
  `art.auto_approve` / `gate.mode = none` waive the human gate, not the
  machine verdict; such a revision stays a candidate and the activity log
  says which check failed. The auto-approve note now names the setting that
  actually waived the gate.
- Frozen build: `BuildersGate.exe hook` hosts the PreToolUse hook, as `mcp`
  hosts the server.

### Added
- **Kits: reusable systems a project takes one at a time.** `kit_list`,
  `kit_install`, `kit_remove` and `bgate kit`. Eight ship under
  `src/templates/kits/godot/`: a four-way top-down controller, the
  platformer controller, the third-person controller with its camera rig,
  the arcade vehicle with its chase camera, 2D and 3D interactables with
  pickups, a stackable slot inventory, and a health component. Each is a
  manifest (what it needs: input actions with default keys, autoloads,
  other kits; what it provides: signals, exports, groups) plus scripts, or
  a pointer at a script the scaffold already ships so the two cannot
  drift. Install never overwrites (`force` replaces with a `.bak`), appends
  only the MISSING input actions to `project.godot`, and then loads every
  installed script inside the running project to say whether the engine
  compiled it, because `--import` compiles nothing that no scene
  references. `.bgate/kits.json` is the ledger that lets `kit_list` tell
  installed from modified from stale, and lets `kit_remove` refuse a file
  someone edited. The gap it closes was measured: three shipped games,
  three hand-written inventories, three health components, none the same
  shape and none tested.
- **The machine-wide asset library.** `library_publish`, `library_search`,
  `library_import` and `bgate library`. `~/.bgate/library` is the store one
  game publishes to and the next imports from, the same shape as the key
  store and `animlib`: machine-wide, in no repository, following the person.
  Content-addressed blobs, an index carrying name, kind, tags, collection,
  dimensions and provenance; `.rig.json` sidecars travel with their sheet,
  `.import` files never do. Import refuses to overwrite a file that differs
  and tracks what landed so `asset_verify` covers it from birth. Deletion
  is `bgate library forget`, a CLI command and not a tool, for the reason
  the animlib fetch is.
- **Canon: which world is current, which files are retired.** A project
  now records the scene the game IS (`world`, defaulting to project.godot's
  main scene), named current files (`reporter`, `world_layout`, …) and a
  RETIRED list of paths/globs, each with its successor and reason
  (`bgate_core/board/canon.py`). It reaches agents three ways: printed at
  the top of every dispatched brief and in the SessionStart block; the
  `canon_status` / `canon_audit` tools (every seat) with `canon_set` /
  `canon_retire` / `canon_unretire` (refused to a seated worker — what is
  current is the director's and the human's call); and the PreToolUse hook,
  which refuses a seat's READ or write of a retired path and names the
  successor — reads too, because the way agents ended up extending a
  retired map was by reading it as the example. `canon_audit` reads the
  tree against the list: scenes/scripts still referencing retired paths,
  scene basenames living under two directories, and scenes/scripts nothing
  reaches from the world, the autoloads or a tool (through ext_resource,
  `res://` strings, directory prefixes a script builds paths from, and
  `class_name`). Observed on Meridian: forty items dispatched into a tree
  carrying the first street beside the island; 58 references to
  `main.tscn` in one agent's log, 88 to a retired module in another.

## [0.1.46] - 2026-09-12

### Added
- **Three engines. `project.engine` decides which tools register, which doctor rows are graded, where the scaffolder looks, and what every seat is told to verify with**
- **The web engine: vite + TypeScript templates, a dev server, a build with a payload budget, vitest, headless screenshots**
- **The Unity engine: adopted through the Hub's project, compiled in batchmode, tested through the Test Framework, photographed by the editor, instrumented by a self-booting MonoBehaviour**
- **`engine_status`, `engine_check`, `engine_screenshot`, `engine_scaffold`, `engine_templates`, `project_set_engine`: the engine-neutral spine**
- **Atlas builds 3D scenes: a three.js viewport with orbit, selection, a move/rotate/scale gizmo and staged writes**
- **The file's own meshes: ArrayMesh geometry decoded from the .tscn's base64 surfaces and served per mesh**
- **The first-run card offers Godot, Web and Unity; Unity as an adopt path; `bgate init --engine web`; `POST /api/project/adopt`**
- **The 3D character pipeline: quadruped poses, `godot_character_wire`, `godot_clip_capture`, `godot_clip_retarget`**
- **The 3D viewport renders on demand and can be stopped; the exe build checks and smokes its assets**

### Changed
- **Seat lanes, playtest, the Tests tab, the asset scan and the dashboard's engine card follow the project's engine**
- **The scene draw list is memoised on the text, the render on the files it reads: a 5,483-node scene opens in 0.1 s after the first 5**
- **Every seat carries the engine-neutral tools; web and Unity families go to the seats that were measured using their Godot equivalents**
- **Dependency floors raised above their advisories, and audited at the floor in CI**
- **Templates live under `templates/<engine>/<kind>`**

### Fixed
- **A running game could not report telemetry: the guard refused the proxied origin and the tokenless POST**
- **`engine_screenshot` spread an async tool wrapper's coroutine into a dict**
- **Node subprocesses decoded as cp1252 on Windows and handed back an empty stdout for a green vitest run**
- **The dev server's stdout was a pipe nobody drained; a recycled pid could get its tree killed**
- **The Unity Test Framework's `-runTests` was combined with `-quit`, so a green suite wrote no results**
- **The character probe's IK weight read 0.00 for every NPC; Blender's self-intersection baseline kept the last keyed pose**
- **Managed blocks stamped by an earlier release were doubled by re-adopting; Unity's Library tree was scanned as the game**
- **An instance's placement stacked on the packed root's transform; caches raced on the thread pool; `engine_status` lost the Godot project path**

### Security
- **Every optional path argument on the web and Unity tools is contained; the headless browser visits loopback URLs only; `dist` must resolve inside the project**

## [0.1.45] - 2026-09-06

### Added
- **The modelling kit builds the smooth shape FIRST: hull, skin, blob, tube, round, rock**
- **Surface tools: fuse, shade, lathe, loft, eleven material presets, bake, decals**
- **`blender_tree`, `blender_scatter` and `blender_look_audit`**
- **Live Blender: the kit inside a Blender that stays open**
- **`mesh_faceting`: the gap between the silhouette and the triangle count**
- **`blockout_generate`: a measured 3D graybox from a spec or a level_plan**
- **`godot_scene_audit` and `godot_export_verify`: the 3D gates every game hand-wrote and none shipped**
- **The 3D scaffold is third-person, with a prop kit and an arcade vehicle demo**
- **Chaos dispatch mode: every run in its own worktree, integration bounded**
- **Codex seats: forwarded MCP identity, isolated config, bounded auto-approval**

### Changed
- **A dispatched agent carries the tools it was measured using, not 203**
- **Autodeploy refuses to spawn an item whose brief names a leased file**
- **The tool schema bills the contract, not the essay**
- **In a 3D project the cast is GENERATED; every visible mesh routes to art**
- **The Builders Gate checkout itself is refused as a project root**
- **The test suite lost 144 prose-pinning tests and can no longer launch real agents**

### Fixed
- **`blender_animate` runs on Blender 5.x**
- **The director console died at spawn on Windows with a >8 KB system prompt**
- **Playtest told every adopted project it had no game**
- **Dashboard and MCP project creation re-root the seat lanes to the scaffold's layout**
- **SessionStart read the raw autopilot doc; project_create left the active pointer behind**
- **Audio Lab: mp3 probing, an edited mp3 saves as a sibling ogg, `encode_ogg` no longer raises**
- **The rig anatomy verdict fails a trunk it could not measure**
- **Agent commits and chaos merges carry a fallback author when the repo has none**
- **Two tools were registered as `blender_sweep`; the tube is `blender_tube`**
- **`bgate_track_gen.gd` preloaded a path that only exists after `track_generate`**

### Security
- **Audio Lab paths are contained in the shape CodeQL recognises; `project_create` keeps exception text out of the reply**

Full narrative: [docs/decisions/0.1.45.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.45.md)

## [0.1.44] - 2026-09-04

### Removed
- **The spend ledger and every budget ceiling**

### Added
- **The director console runs on Codex as well as Claude Code**
- **An opt-in, credential-free Claude usage bridge**
- **Audio Lab separates a clip into stems locally**
- **`art.mesh_route` settles how the art seat makes NEW geometry**
- **`track_generate` builds a measured, drivable circuit from a JSON spec**
- **`ui_concept` paints the game's screens and derives a palette and a Godot Theme**
- **`sfx_prompt` generates real SFX through kie sounds**
- **`godot_export_probe` runs a script against the EXPORTED pck**
- **`bgate connect`, wiring your coding agent is a command, not a paragraph to retype**
- …and 1 more, in the decisions file.

### Changed
- **A spawned agent files at most two non-duplicate items**
- **The root config files carry rules, not essays**
- **The README is 232 lines instead of 308**
- **The repository root is a table of contents again**
- **`bgate_engine/` is `docs/engine/`, and stops shipping**
- **The dashboard package and the suite got the same treatment**
- **`bgate_core` is ten subpackages, not 117 files in a heap**
- **`tools/` is gone; `panel_api.py` is a script and lives in `scripts/`**
- **One sandbox path for the floor-art scripts**

### Security
- **Every path derived from an Audio Lab stem request is re-checked**
- **`bgate publish` put the author's own directory on the public web**
- **`0.0.0.0` is no longer an accepted `Host`**
- **`release-exe.yml` narrows `contents: write` to the one job that needs it**
- **`.claude/launch.json.example` had a literal tab in its `BGATE_ROOT` placeholder**

### Fixed
- **Three MCP tools were joining the shared spine by accident**
- **The model card on the workflow canvas gets its per-node run button back**
- **The copyable registration line escapes its JSON, so VS Code's pastes whole**
- **`bgate doctor` no longer conflates "nothing is wired" with "no runner installed"**

Full narrative: [docs/decisions/0.1.44.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.44.md)

## [0.1.43] - 2026-09-01

### Added
- **The rig and animation gates now measure the thing they are named after**
- **Every important gate now terminates at the actual player-facing runtime**

### Changed
- **`godot_run` is autoload-safe**
- **Evidence tools no longer dirty the tree**
- **`godot_test_run` returns concise output by default and separates two signals**
- **`godot_deliver_asset` handles non-humanoids**
- **`ask_human` names its recipient**
- **`queue_update` on a running item says whether it reached anybody**
- **Steers are recorded in the item's own history**
- **Retry exhaustion is a state, not two counters to add up by hand**
- …and 23 more, in the decisions file.

Full narrative: [docs/decisions/0.1.43.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.43.md)

## [0.1.42]

The director you talk to is a real session, a failed item no longer stops everything behind it, and the installer asks what you actually want.

### Added
- **The console's director is a persistent Claude Code session**
- **A preferred provider and model, honoured everywhere**
- **Optional features are modules, chosen when you install**
- **A dispatched seat gets its own craft's tools, not all of them**

### Changed
- **A failure is retried and then acted on**
- **Agents stay on the item they were given**
- **A 2D project starts without the 3D pipeline**

### Fixed
- **Five ways past the write gate**
- **Tools that took a raw path could reach another game**
- **Dispatch races that cost real runs**
- …and 3 more, in the decisions file.

Full narrative: [docs/decisions/0.1.42.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.42.md)

## [0.1.412]

The app opened a black window if you had ever renamed a project.

### Fixed
- **A folder registered under two names blanked the whole interface**

Full narrative: [docs/decisions/0.1.412.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.412.md)

## [0.1.411]

The installer, which 0.1.41 built and then forgot to publish.

### Fixed
- **`BuildersGate-setup.exe` is on the release page**

Full narrative: [docs/decisions/0.1.411.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.411.md)

## [0.1.41]

Everything in this release is one thing: the app you download now does what the app you ran from a source checkout always did.

### Fixed
- **The app could not start from a shortcut**
- **The app's own window code had never run**
- **Playtest recording could not work**
- **Speech to text was left out for a reason that was not true**
- **A missing transcriber disabled the Record button**
- **Menus and drawers rendered behind the page**
- **Clicking a project in the switcher did nothing**
- **"Open Orchestration" went nowhere**
- **The test suite wrote into your real project list**

### Added
- **An installer**
- …and 7 more, in the decisions file.

Full narrative: [docs/decisions/0.1.41.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.41.md)

## [0.1.40] - 2026-08-12

Forty-nine commits.

### Added
- **Machine-wide API keys (`~/.bgate/.env`), and `bgate key` to manage them**
- **A scratch project at `~/.bgate/scratch`, for generations that belong to no game**
- **The anchor is a model sheet now (`anchor_views`, default 3)**
- **Character work on Krea is pinned to `nano-banana-2`**
- **`sprite_plan`, and archetypes for `image_sprites`**
- **Per-frame timing in the emitted resource**
- **Ping-pong cycles**
- **Palette locking (`palette_lock`, default `"auto"`)**
- **A motion report on every assembled sheet**
- **A 3D model viewer and editor**
- …and 68 more, in the decisions file.

Full narrative: [docs/decisions/0.1.40.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.40.md)

## [0.1.35] - 2026-08-09

Twenty-two commits.

### Added
- **Music generation through Suno (kie.ai)**
- **Brainstorm**
- **Streamer chat, and feedback sessions**
- **Deepgram speech, both directions**
- **A provider registry**
- **Local runtime and coding-agent setup panels**
- **A playtest notepad**
- **Work history on the overview**
- **The sprite editor and audio lab are pages again**
- **An `orbit` theme**
- …and 12 more, in the decisions file.

Full narrative: [docs/decisions/0.1.35.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.35.md)

## [0.1.34] - 2026-08-08

Twenty-two commits, and one sentence covers most of them: **a check that grades its own homework is not a check.** The rig gates measured whether…

### Added
- **`blender_flex`, the deformation gate**
- **`blender_rig` audits before it binds**
- **`godot_retarget_check`, the engine's own verdict**
- **Rig and animation quality metrics**
- **Pairwise art tournament judging**
- **Nine scene tools on the MCP surface**
- **The scene builder plays the build it just wrote**
- **`assets.lock_holder()`**
- **`pending_decisions`**
- **The heartbeat carries pending decisions**
- …and 22 more, in the decisions file.

Full narrative: [docs/decisions/0.1.34.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.34.md)

## [0.1.33] - 2026-08-02

### Added
- **Atlas grew a fourth mode: `code · edit it`**
- **`POST /api/godot/file`**
- **`GET /api/scene/files`**
- **A `real` backdrop in the scene viewport**

### Changed
- **Streamer mode costs roughly nothing now**
- **`/api/screenmap` is cached and no longer walks the engine's import cache**
- **`/api/preview` takes `item_id`**

### Fixed
- **Three panels ignored `hidden`**
- **Long dropdowns closed the instant they opened**
- **`app.css` is cache-busted like the JS**
- …and 8 more, in the decisions file.

Full narrative: [docs/decisions/0.1.33.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.33.md)

## [0.1.32] - 2026-08-01

One call turns "a model that looks like X" into a rigged character in the engine, and `force` stops meaning "overwrite your project".

### Added
- **`character_generate`, the whole pipeline as one tool**

### Fixed
- **`force` meant "overwrite your project", and said nothing about it**

Full narrative: [docs/decisions/0.1.32.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.32.md)

## [0.1.31] - 2026-08-01

A generated mesh becomes a rigged character an engine can move, and the skeleton it binds to is the same one every time.

### Added
- **`blender_rig`, the missing step between geometry and a character**
- **A shipped humanoid template**

### Fixed
- **The shoulder joint was 20 cm outside the body**
- **`bg_flipped` answered `0` both when it found nothing and when it broke**
- **A collapse could meet its budget and destroy the asset**
- **A 404 meant "server missing" when it means "server answering"**
- **EEVEE answers to two names**

### Added, the paths a session could not reach
- **Krea 3D shipped unreachable**
- **Which knobs a backend takes is now discoverable**
- **ComfyUI claimed to be available with no workflow**
- …and 9 more, in the decisions file.

Full narrative: [docs/decisions/0.1.31.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.31.md)

## [0.1.30] - 2026-07-31

The 3D path stops being a blockout generator.

### Added, 3D
- **Image-to-3D on the user's own GPU**
- **Krea 3D**
- **`bg_adopt`, a generation is not an asset**
- **Orientation is refused rather than guessed**
- **`doctor` gains an `imageto3d` row**
- **`spend` gains a `mesh` kind**

### Added, CI
- **A CI pipeline that gates on more than pytest**
- **`packaging/smoke_wheel.py`**
- **A release guard**
- **`.github/workflows/security.yml`**
- …and 2 more, in the decisions file.

Full narrative: [docs/decisions/0.1.30.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.30.md)

## [0.1.29] - 2026-07-30

### Fixed
- **Every quality gate in the 3D path was a false negative**
- **The importer's `Icosphere` was the whole story on weighting**
- **The turnaround could not detect the failure it was written for**
- **`blender_sweep` could delete outside the project root**
- **The tool told to make the logo was forbidden from drawing text**
- **Decals shipped opaque**

### Added
- **A proportioned rigged base, so the agent stops inventing a body**
- **`godot_deliver_asset`**
- **`blender_layer_rerun`**
- **3D is visible to QA**
- …and 7 more, in the decisions file.

Full narrative: [docs/decisions/0.1.29.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.29.md)

## [0.1.28] - 2026-07-30

Nothing.

Full narrative: [docs/decisions/0.1.28.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.28.md)

## [0.1.27] - 2026-07-30

### Added
- **An agent finishing now reaches somebody**
- **A transition log something finally reads**
- **Work chains: dependent items filed as one ordered group**
- **Three approval gates, because the question is not "strict or loose", it is WHO IS AWAY**
- **A heartbeat, for the failures that are an ABSENCE of transitions**
- **`ask_human`**
- **Notifications**
- **One settings surface**
- **The agent rails open what they name**

### Changed
- **The QA gate's loop moved into the follow-up router**
- …and 10 more, in the decisions file.

Full narrative: [docs/decisions/0.1.27.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.27.md)

## [0.1.26] - 2026-07-29

### Added
- **The MCP server now ships `instructions`, so a session cannot be lobotomized by changing directory**
- **A `SessionStart` hook that preloads the board**
- **A project thread for in-flight state**
- **`bgate hook-install --scope user`**
- **The VFX pipeline**

### Fixed
- **An agent's reported file list is now the one the harness observed**
- **Every seat was instructed to write a file its own lane forbade**
- **The PreToolUse hook ignored the widest-reach agent in the system**
- `bgate hook-status` no longer reports a seatless session as inert when it is not, and the scaffolded `CLAUDE.md` no longer claims `queue_next` "marks…

### Known issues
- `vfx_animate` joins model-supplied `name` / `out_dir` onto the output directory with no containment, so a traversal writes outside the project root.
- …and 4 more, in the decisions file.

Full narrative: [docs/decisions/0.1.26.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.26.md)

## [0.1.25] - 2026-07-28

### Fixed
- **Every wheel and exe shipped without the Godot Web export preset**

Full narrative: [docs/decisions/0.1.25.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.25.md)

## [0.1.24] - 2026-07-28

### Fixed
- **A clean install pulled MCP SDK 2.0 and every MCP tool stopped importing**

Full narrative: [docs/decisions/0.1.24.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.24.md)

## [0.1.23] - 2026-07-28

### Added
- **The Agents view is a console, not a board**
- **A message to the director is a work item with its own log**
- **A staging queue between the conversation and the graph**
- **Auto-deploy**
- **Phases, the pockets of work inside a running agent**
- **You can see what the agent sees**
- **Sign-off gates**
- **Cross-agent work is drawn**
- **The director can steer its own workers**
- **Console sessions**
- …and 19 more, in the decisions file.

Full narrative: [docs/decisions/0.1.23.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.23.md)

## [0.1.22] - 2026-07-28

### Fixed
- **The first-run screen offered to create a project in `C:\Windows\system32`**
- A `PermissionError` from the create endpoint now answers 400 with the directory and a suggestion instead of leaking `[WinError 5] Access is denied`.

Full narrative: [docs/decisions/0.1.22.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.22.md)

## [0.1.21] - 2026-07-28

### Fixed
- **The desktop app had no icon**
- **The logo was a redrawing of itself in four places**
- **The rail brand painted the whole mark one colour**

### Added
- **"Run anyway" on the uncommitted-changes refusal**

Full narrative: [docs/decisions/0.1.21.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.21.md)

## [0.1.2] - 2026-07-28

### Fixed
- The standalone Windows build ships as a folder rather than a self-extracting executable.
- It is still **not code signed**, so Smart App Control will refuse to launch it ("we can't confirm who published BuildersGate.exe").

Full narrative: [docs/decisions/0.1.2.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.2.md)

## [0.1.1] - 2026-07-28

A UI/UX pass over the whole dashboard, and the first downloadable build.

### Added
- **Light and dark grounds**
- **Desktop app**
- **A standalone Windows build**
- **Sprite editor**
- **Audio lab**
- **World bible**
- **Scene composition convention**

### Changed
- The dashboard's CSS is one stylesheet (`bgate_ui/static/app.css`) with a declared cascade order, replacing six `<style>` blocks that had accumulated…
- Every `<select>` is a searchable in-app combobox.
- Every `window.prompt` / `window.confirm` is an in-app dialog.
- …and 11 more, in the decisions file.

Full narrative: [docs/decisions/0.1.1.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.1.md)

## [0.1.0] - 2026-07-27

First public release.

### Added
- **MCP server**
- **Seven agent seats**
- **Godot adapter**
- **Blender adapter**
- **Two image providers**
- **Dashboard**
- **Playtest mode**
- **`bgate publish`**
- **`bgate doctor`**
- MIT licence, `.env.example`, and this changelog.
- …and 6 more, in the decisions file.

Full narrative: [docs/decisions/0.1.0.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.0.md)

[Unreleased]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.43...HEAD
[0.1.43]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.42...v0.1.43
[0.1.42]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.412...v0.1.42
[0.1.412]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.411...v0.1.412
[0.1.411]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.41...v0.1.411
[0.1.41]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.40...v0.1.41
[0.1.40]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.35...v0.1.40
[0.1.35]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.34...v0.1.35
[0.1.34]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.33...v0.1.34
[0.1.33]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.32...v0.1.33
[0.1.32]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.31...v0.1.32
[0.1.31]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.30...v0.1.31
[0.1.30]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.29...v0.1.30
[0.1.29]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.28...v0.1.29
[0.1.28]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.27...v0.1.28
[0.1.27]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.26...v0.1.27
[0.1.26]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.25...v0.1.26
[0.1.25]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.24...v0.1.25
[0.1.24]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.23...v0.1.24
[0.1.23]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.22...v0.1.23
[0.1.22]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.21...v0.1.22
[0.1.21]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.2...v0.1.21
[0.1.2]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Thepizzapie/BuildersGate/releases/tag/v0.1.0
