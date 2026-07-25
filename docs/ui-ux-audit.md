# Builders Gate dashboard — blind UX/UX audit

Method: two Cortana agents (BUIGAT-1, BUIGAT-2) each ran 3 **blind** evaluator
subagents against live screenshots of all 15 views (9 top-level + 6 seat
workspaces) on the running dashboard (`:7788`, serving *Corporate Quest*).
Blind = evaluators saw only rendered screenshots, never the source, and judged
as first-time users. **91 raw findings → ~70 unique after cross-merge.**
`(×N)` = flagged independently by N evaluators — confirmation weight.

Ranking is by leverage, not just severity: the cross-cutting chrome fixes are
cheap and win on every screen, so they lead.

---

## TOP CROSS-CUTTING (fix once, wins everywhere)

Every evaluator hit these because the chrome is on every screen.

1. **`vault drift` + footer `1 drift · 1 missing · 0 pending` (×6 — every evaluator).**
   Undefined red alarm. Not clickable, no tooltip, no fix path, two wordings for
   maybe one issue. Reads as "something's broken, and I can't act on it."
   → **CHANGE**: plain language + tooltip ("1 asset changed on disk since last
   sync"), make the pill open the drift-detail list with a fix action; if benign,
   drop the alarm-red.
2. **`0 live` pill (×3).** Green dot at zero reads "running" when nothing is; no
   noun — live *what*? → **CHANGE**: "0 agents live", neutral gray at 0, green ≥1.
3. **Raw filesystem path in header/sidebar, clipped mid-string (×4).**
   `C:\Users\adria\Desktop\downsizing` — noise/leak, cut with no ellipsis.
   → **CHANGE**: drop it (keep project name + engine), or start-truncate + tooltip.
4. **Right-aligned low-contrast monospace subtitle is each page's only purpose
   statement (×2)**, floating far from the left heading across a wide empty middle.
   → **CHANGE**: move it under the H1, left-aligned as body text.
5. **Two voices.** Polished sentence-case headers vs lowercase terminal-speak
   empty states that name raw tool functions (`playtest_check`, `Review for
   delegation`). → **CHANGE**: one human voice in the UI; tool names live only in
   a dev tooltip.
6. **Zero-count sidebar badges** (`Playtests 0`) render like real ones (`Atlas 67`).
   → **CHANGE**: hide badges at 0.

---

## BLOCKERS

- **Atlas `67 DEAD`** — big red stat, no explanation, no next action, and it
  contradicts the "every screen wired to every asset" tagline. → **ADD**: click
  it to filter a "Dead / unused" panel (tooltip: "not referenced by any scene,
  script, or SpriteFrame") + bulk "Archive dead".
- **Header/status never resolves** — logo shows `connecting…`, bottom pill shows
  a green dot with just `…`. With `0 live` the whole app reads as
  possibly-disconnected. → **CHANGE**: resolve to Connected / Offline+retry with a
  timestamp; never a permanent "connecting…".
- **World bible cut-line reads inverted** — struck-through `Tier 1 — first
  playable` / `Tier 2 — vertical slice` sit *above* the chip grid, so it reads as
  "everything below is cut" — almost certainly the opposite of the truth.
  → **CHANGE**: in-scope above the line, cut below, label each group; don't
  strikethrough active milestone names.
- **QA seat has no verdict** — a QA screen with no PASS/FAIL anywhere; the run
  dropdown says `[done]`, but "done" is not a verdict; no checks, no failure list.
  → **ADD**: a verdict panel per run — PASS/FAIL badge, checks run, failures
  linking to the offending item.
- **Seats answer none of the core role questions (×2)** — what is this role's
  agent doing *now*, what did it recently produce, what is it blocked on?
  → **ADD**: a persistent per-seat status strip (agent + idle/working/blocked,
  current task, last 3 outputs, open blockers).
- **Narrative seat is 100% empty** — bare dot grid, and the helper text
  ("drag a panel header…") references panels that don't exist, directly
  contradicting "each seat tuned to its craft". → **ADD**: starter panels, or an
  explicit "No panels yet — add your first" state with presets.
- **Empty states dead-end** — Overview (all zero, no first action), Playtests
  (names `playtest_check` with no button), Director board (references a
  "Dispatch"/"Review for delegation" control that isn't visible). → **ADD** a real
  CTA/button to each, or state plainly it's agent-driven.

---

## MAJOR

### Raw internals leaking to users
- **Gameplay "PLAY ALONGSIDE" shows a JSON error blob** `{"detail":"no web build
  — export it first (tech seat)"}`. → **CHANGE**: "No playable build yet — ask the
  Tech seat to export a web build" + a button that jumps there / triggers it.
- **QA log dumps agent internals** — system-prompt JSON ("YOU ARE A SPAWNED SEAT
  WORKER…") mid-sentence, plus `TOOLSEARCH … No matching deferred tools found`.
  → **CHANGE**: human-readable step summary; raw payloads behind an expand toggle;
  interpret tool errors into a status chip (retrying/failed/ok).
- **Failed cards show no cause/retry** — Director's `failed` Paladin card gives no
  error and no action. → **ADD**: expandable error + Retry / View log.

### Clipped content = actions users can't reach
- **Art Approve / Reject / Regenerate / Restore buttons cut off at the viewport
  bottom** — the primary decision controls are effectively invisible.
  → **CHANGE**: sticky/pinned action bar (or shrink the candidate image).
- **Agents board `FAILED` column clipped off the right edge**, no scrollbar — users
  may never see the most action-worthy items. → **CHANGE**: horizontal scroll
  affordance, or fit all columns; FAILED must be visible.

### Machine identifiers instead of human names
- **Filenames as identity** — `pm_paladin_idle_se`, `prop_copier_ne`, `_ref`/`_se`
  suffixes everywhere (Assets, Atlas, Art seat). Reads like a dev file tree.
  → **ADD**: human display name ("Paladin — idle, SE facing") + filename as mono
  subtext; decode suffixes into a legend/tags.
- **`MISC 66`** — one meaningless bucket though `class_`/`enemy_`/`prop_` structure
  exists. → **CHANGE**: group by Characters / Enemies / Props / Tilesets.
- **Priority `p7/p8/p9`, bare `#IDs`, run "ID soup"** (`#5 · QA gate: verify #1 —
  …[done]`). → **CHANGE**: "Priority 8"; run selector = title + status chip, IDs to
  a secondary line.

### Atlas / Assets
- **Atlas renders blank white squares instead of thumbnails** on a screen that is
  literally a visual map of art. → **CHANGE**: real thumbnails; distinct
  missing-vs-loading glyph.
- **Affordance invisible** — cards/nodes give no hover/cursor/button cue; "Click
  any node to deploy a task" is undiscoverable *and* vague. → **ADD**: hover state
  + cursor + explicit "+ Task" on hover; reword to the outcome ("Assign an agent
  to revise this asset").
- **`CHARACTER_TEST` / `test_player.gd` scaffolding surfaced as a top-level
  screen** — erodes trust it's the real game. → **CHANGE**: tag/hide test screens.

### Seat workspaces — no shared model
- **No shared seat skeleton (×3)** — Narrative = freeform canvas, Tech = fixed
  dashboard, Audio = flat file table, QA = stacked panels, Gameplay = code
  console. Switching tabs feels like switching apps. → **CHANGE**: one seat
  template (status strip + work area + activity log) with role tools slotted in.
- **Agent presence inconsistent (×2)** — QA has a "LIVE QA AGENT" steer/stop panel;
  other seats have nothing, and sidebar "Agents 0" never ties to a seat.
  → **ADD**: the same live-agent panel on every seat, or an explicit "No agent —
  assign/start" state.
- **Seat tabs carry no status** — nothing shows which seat is busy/blocked/has new
  output, so no reason to open any tab. → **ADD**: per-tab status dots/counters.
- **GDScript console intimidating for non-devs** (Gameplay + Tech) — "must extend
  SceneTree and quit()" handed to a user told AI builds the game *for* them.
  → **CHANGE**: purpose header + collapse by default / advanced toggle;
  pre-run validation with inline error + "reset to template".
- **"QUEUE BOARD — BY SEAT (26 ITEMS · 0 QUEUED)" shows only `done` cards** — a
  "queue" with 0 queued full of completed work is self-contradictory.
  → **CHANGE**: rename "Work Board" / split done vs open / toggle.

### Per-screen
- **World bible**: pillars have no section label (only decodable from the
  right-aligned subtitle); 11 chips look clickable but open nothing; no add/edit on
  the pipeline's "source of truth". → **ADD** a "Pillars" label + chip detail panel
  + edit (or state it's agent-written).
- **Audio**: every player reads `0:00 / 0:00` — durations never load, playback
  looks broken; no add/generate control; banner claims cue-mapping that's absent
  and ships a `TODO:` dev note. → **CHANGE** preload durations; **ADD** add/generate;
  drop dev TODOs from the product.
- **Tech**: "Engine & build" stuck on `checking Godot…` with no spinner/timeout
  (indistinguishable from a hang); "Project files" lists `.godot` cache artifacts
  with full hashes wrapping mid-token. → **CHANGE** animated loader + timeout;
  filter cache entries by default.
- **Vault/integrity jargon block** — `RUN INTEGRITY AUDIT`, `TRACKED BINARIES ·
  INTEGRITY` unexplained. → **CHANGE** plain labels + tooltips.

---

## MINOR / NIT (grouped)

- **Overview**: in-game control hints (`A/D move · Space jump…`) shown before any
  build runs → show only when running. "web build" dropdown unlabeled → label
  "Target". "Play & record" is a black void → placeholder + progress.
- **Studio**: `Workflows`/`Game editor` ambiguous (tabs vs launch) → segmented
  tabs. Pipeline arrow shorthand (`anchor→per-frame variants→QA→sheet→Godot`) →
  per-step gloss.
- **Assets**: overlapping signals per card (`3 new` badge + status dot + `4 revs`)
  → consolidate + spell out "revisions". Truncated names (two identical
  `prop_conference_table_…`) → middle-truncate/wrap. Sprite-sheet card shows a
  crushed micro-strip → show one representative frame. Blank checkerboard cards →
  always show name + status; skeleton vs "no preview".
- **Atlas**: grammar `1 assets` → conditional plural; `SpriteFrames` → gloss.
  Layout imbalance (tall CHARACTER_TEST vs short others, dead right space) →
  masonry/packed grid.
- **Gameplay**: `checking engine…` next to a RED dot conflicts → amber/animated for
  checking. Screenshot field `1.0` unlabeled → "Scale ×" + tooltip.
- **Art**: cryptic chips `4·3c` / `3·2c` → tooltip legend. `gpt-image-1` model
  badge → hide/behind details. Two red buttons "Run QA review · all candidates" vs
  "Run independent QA review" → differentiate scope. Sidebar ref thumbnails
  truncated + bottom row clipped → tooltips + padding.
- **Director**: per-seat count (narrative 2, gameplay 4…) unlabeled, doesn't
  reconcile with "0 QUEUED" → label + reconcile.
- **Playtests**: content in top ~240px, rest dead black → teach the flow while
  empty. Title duplicated (top bar + h1). Kicker "EVIDENCE" abstract → "Recorded
  sessions". Subtitle references a "director" that isn't in nav.
- **Timeline**: empty-state links nowhere → "Record a playtest to start your first
  iteration →". No ghost/skeleton of an iteration. Kicker "CAUSAL HISTORY" opaque.
  Different empty-state treatment than Playtests → one component.
- **Narrative**: `+ Panel` gives no hint what panel types exist → labeled picker.
  `→ Link mode` / `✕ Delete` / `↺ Save` enabled with nothing on canvas →
  disable-until-valid; auto-save + "Saved" indicator.
- **QA**: bot roster uses `ticks` (engine jargon), no last-run time/result →
  purpose + last-run + seconds. "steer the agent…" input gives no hint / no
  disabled state when none live.
- **Seats (all)**: H1 always "Seat workspaces" + static "PURPOSE-BUILT WORKSPACES"
  tagline — never says which role → per-seat H1 + role summary. Section-header
  drift (`♪ Sound library (18)` vs bare `BOT ROSTER`) → one pattern. Orientation
  banner only on Audio → same "what this covers" banner everywhere.

---

## What's already good (keep it)
- Left nav grouping (COMMAND / BUILD / LIBRARY) — well-organized, consistent.
- Seat tab bar — active tab clearly outlined orange, icons + labels readable.
- Studio template grid — the most self-explanatory screen (categorized cards +
  short descriptions).
- Overall visual coherence — shared chrome, typography, pills across screens.
