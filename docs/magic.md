# Making it feel like magic — design philosophy

The target, stated precisely: **magic is when the distance between intent and
result approaches zero.** Not "agents do more" — agents doing more of the wrong
thing is how you get an hour of nothing. Every idea here shortens the
intent→result path, grounded in what the first production run proved.

---

## I. The great inversions

### The game is the interface
Today: leave the game → describe feelings in chat → orchestrator paraphrases →
agents act → return to the game. Every hop loses signal (one hop invented a
wrong character description).

Inverted: **the pipeline lives inside the game.** You play; it watches your
inputs, telemetry, and (with a mic) your voice. Between rounds it surfaces what
it noticed: *"you whiffed 40% of hooks at max range — reach wrong? [try +10]"*.
The playtest IS the standup. Feedback never travels; it's born where the work
is. Everything needed already exists: telemetry, input capture, tuning
overrides, a deterministic sim.

### Determinism is a time machine, not just netcode prep
`save_state` + seeded replay was built for rollback. Its bigger use:
**counterfactual play.** "That exchange felt cheap" → the pipeline replays YOUR
last ten seconds of real inputs under twenty tuning variants, headless, in
seconds — and shows the diff: *"under variant C your counter lands."* Tuning
stops being sliders in the abstract and becomes re-living moments you actually
played. No other part of the stack is this close to genuine magic per unit of
effort.

### Taste is an artifact, not a vibe
Canon stores facts. Refs store approved images. But the user's TASTE — every
"CORN STARS is inappropriate," every "too much gravity," every "decent but…" —
lives in chat and evaporates. Store it: a **taste ledger**, case-law style.
Each entry: the artifact judged, the verdict, the reason. Agents consult it
like precedent and cite it ("halo-glow rejected in case 12"). Two properties of
case law make it the right model: it compounds, and it binds without requiring
the judge to be present.

### Brains are fungible; seats are permanent
Agents die constantly (interrupts are normal usage). The fix isn't fewer
deaths — it's making death free: **agents must be stateless functions over
durable seat state.** Everything a seat knows (progress manifests, refs, taste
precedents, craft lessons) lives in the pipeline, so any brain — Sonnet, Opus,
a human — picks up the seat mid-stride. The measure of success: killing an
agent mid-task costs one file read, not an archaeology dig.

### The character sheet compiles into the game
Canon says Scoville "telegraphs his hook long." The tunable says 0.85s. The
animation contract says `telegraph_hook`. Today these agree by convention and
drift silently. Make the links literal: one knowledge graph where the fact IS
the tunable IS the animation requirement, and changing any endpoint flags the
others. The bible stops describing the game and starts **compiling into it.**

## II. Quality as search, not generation

- **Best-of-N with cheap screening:** generate 4 low-quality candidates,
  auto-rank (tripwires + one fast visual check), then spend high-quality budget
  only regenerating the winner. Search beats hoping.
- **First-frame gates everywhere:** any batch's first output is reviewed before
  the rest spend. Bad direction dies at 1x cost.
- **Evolutionary balance (with a taste leash):** determinism means any two
  tuning variants can fight 100 headless matches in seconds. Let balance emerge
  from selection pressure — but the fitness function is constrained by the
  taste ledger, because the masher experiment proved metrics without taste
  optimize into un-fun. Human veto is a feature, not a failure.
- **Session length is the truth serum:** telemetry already records when play
  starts and stops. The fun corridor reveals itself in whether the player keeps
  playing. Use it as the slowest, most honest signal.

## III. The pipeline's immune system

Every production failure this run became a permanent guard (stdin=DEVNULL, the
cold-start warmup, sheet-generation refusal, the edit() regression tests). That
loop ran through a human orchestrator. Institutionalize it: every agent failure
writes an incident record; a periodic immune pass turns REPEATED incidents into
guards, tests, or docs. A pipeline that metabolizes its own pain gets harder to
hurt every week — that's the property that separates tools from organisms.

## IV. The north-star metric

**Jam time: idea → playable slice in the user's hands.** Currently hours.
Everything above should move this number, and the number should be watched:
periodically have the pipeline build a small random game unattended and measure.
When jam time regresses, the pipeline broke — even if every test is green.

---

*Sequencing note: the gap-analysis plan (docs/gap-analysis.md) is the floor —
items 1–3 there are prerequisites for most of this. The counterfactual replay
engine and the taste ledger are the two ideas here with the highest
magic-per-effort and should follow immediately after.*
