# Gap analysis — where the pipeline can improve tenfold

Written after the first real production run (a complete arcade fighter built by
~30 seat agents over two days). Every gap below is backed by something that
actually happened, with the cost it actually incurred. Ranked by leverage.

---

## 1. The feel gap — agents validate correctness, nobody validates feel
**Evidence:** a balance wave passed 98 deterministic assertions and a scripted
acceptance sim ("masher must lose"), shipped, and the user judged it *"an hour
of nothing work."* The pipeline's strongest gates — tests, sims, screenshots —
measure correctness. Feel has exactly one oracle (the player) and the loop put
them 60 minutes downstream of every decision.

**10x lever:** split changes into MECHANISMS (agent work: states, systems,
verified by tests) and NUMBERS (never agent work again). Numbers get:
- a live in-game tuning panel (F1 → sliders over every export, applied
  mid-fight, saved to an overrides file the game boots with)
- input-script replay: record the player's real inputs once; replay them
  against any change (the deterministic sim makes this exact) — regression
  testing with actual human play, not bot proxies
- scripted acceptance bots demoted to tripwires, never ship-gates

Feel-loop latency: ~60 min → ~1 min. This is the single biggest multiplier.

## 2. The seat_tool tax — every tool call pays a process spawn and a file dance
**Evidence:** sub-agents cannot call the MCP server directly (it anchors to the
launch cwd, not the game project), so every call is: write kwargs JSON → spawn
PowerShell → spawn a fresh server process → redirect output to a file → read
the file. Production passes ran 125–205 tool uses each; a majority were this
ceremony, not work. It also caused a real failure (an agent's blackboard note
silently lost to a cwd mismatch).

**10x lever:** accept the project root per-call — every tool gets an optional
`project_dir` argument (falling back to BGATE_ROOT/cwd walk-up). Agents then
call MCP tools natively from any session. Estimated 3–5x fewer tool invocations
per pass, a large latency cut, and a whole error class deleted.

## 3. The kill tax — agents die mid-flight and successors do archaeology
**Evidence:** six agents were killed by conversation interrupts across the run
(the harness kills background agents on interrupt — the user interrupts often;
that is normal usage, design for it). Each death cost a recovery agent doing
ground-truth forensics: git diffs, partial-frame inventories, "what did my
predecessor half-do." Roughly a quarter of total agent effort was recovery.
One death left a parse error live in the repo; one lost a generated portrait.

**10x lever:** a work manifest protocol — seats append to
`.bgate/progress/<task>.json` (steps done, artifacts produced, next step)
after every unit of work. A successor resumes from one file read instead of an
investigation. Cheap to add to the seat brief contract; near-eliminates the tax.

## 4. Art verification is expensive eyeballing; drift is caught late
**Evidence:** an entire six-frame batch drifted off-model and was caught by the
USER, not the pipeline. Separately, three actors judged character identity
against three different anchors (stale prose, the pinned reference, the user's
concepts) and disagreed — churn either way. Measured: no global similarity
metric gates identity (Unicom separates characters 0.66–0.83 vs 0.40–0.51
cross, but poses confound; CLIP useless).

**10x lever (already designed, docs/character-consistency.md):**
- character profiles with vision-derived trait text, auto-injected into prompts
  — nobody describes a character from memory again
- `consistency_check`: composed side-by-side + trait checklist + advisory
  tripwires, so every judgment starts from the same view
- the first-frame gate: an off-model batch dies at 1x spend, not Nx

## 5. Feedback intake is a human relay
**Evidence:** every piece of playtest feedback traveled: user types in chat →
orchestrator paraphrases → SendMessage → agent. Paraphrase introduced a real
error (the orchestrator's wrong character description). The purpose-built
playtest mode (record voice → transcribe → classify → route with telemetry
joins) exists and has never been used — blocked only by a missing microphone.

**10x lever:** make intake first-class and lossless: the mic playtest loop, plus
an in-game feedback key (jot a note mid-fight → lands as a classified playtest
item with a telemetry snapshot attached). Feedback reaches seats verbatim,
with data, without a relay.

## 6. Craft knowledge doesn't compound
**Evidence:** the run generated dozens of hard-won lessons (EEVEE cold-start,
stdin=DEVNULL, smart_project needs EDIT mode, identity-safe prompt patterns,
alpha-histogram verification, tunable ranges that feel right). They live in a
README section and dead agent transcripts. The next game's agents re-learn them
at full price.

**10x lever:** a craft-lessons registry (same recall machinery as lore, scoped
to the pipeline, not the game): seats end passes by writing lessons; briefs
surface the relevant ones. Second game onward starts at the first game's
ceiling, not its floor.

## 7. Waves are too big
**Evidence:** mechanisms + numbers + art landed as bundles; feedback arrived
after everything. A wrong direction cost a full wave.

**10x lever:** every increment ends PLAYABLE (determinism makes builds cheap;
web export makes them shareable) and the user touches the game between
increments, not between waves. Combined with #1 this converts "an hour of
nothing" into "five minutes, adjust, continue."

---

## Sequenced plan
| Order | Item | Effort | Multiplier |
|---|---|---|---|
| 1 | Tuning panel + softened defaults (#1) | hours | feel loop 30x |
| 2 | `project_dir` on every tool (#2) | hours | every pass 3–5x |
| 3 | Work manifests in the seat contract (#3) | small | recovery tax → ~0 |
| 4 | Character profiles + consistency_check (#4) | 1–2 passes | art spend halved, drift caught at 1x |
| 5 | Input-script replay (#1b) | 1 pass | human-grade regression, free forever |
| 6 | In-game feedback key; mic loop when hardware exists (#5) | 1 pass | lossless intake |
| 7 | Craft-lessons registry (#6) | 1 pass | compounds every future game |
