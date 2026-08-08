# Researching "taste" for art, rigging, and animation agents

2026-08-07. Research notes, not a build plan. No code changes accompany this
doc. Written after a week of hands-on testing kept surfacing the same
complaint: agents can execute an art/rig/animation task but cannot judge
whether the result is any good.

## The reframe

What this repo already has (`consistency_check`, `art_qa_verdict`,
`artdirection.py`'s palette/pixel-grid checks) is a **drift detector**: does
the output match a reference the human already approved. `artdirection.py`
says this about itself, correctly: "What is deliberately NOT here: taste.
This cannot tell you the art is good. It tells you the art disagrees with
what the project wrote down."

The instinct is to read that gap as "add a quality checker next to the
consistency checker." That doesn't survive contact with rigging and
animation, for a structural reason: `consistency_check` diffs **one frame
against one reference**. That's a single-image problem. A rig is judged by
how a mesh deforms **across a range of motion** — one pose tells you
nothing. An animation is judged by the **relationship between frames** —
velocity, easing, arcs, lag between parent and child joints. Diffing frame N
against a reference is structurally blind to all of that. So this isn't
"missing rubric," it's "missing evaluation mechanism" for two of the three
disciplines.

The good news: aesthetic judgment in all three disciplines is not
undefined. Each has decades of formalized craft principles (the 12
principles of animation, rigging deformation theory, art fundamentals) that
partially decompose into things you can compute, not just vibe-check. The
rest of this doc is what the actual research literature says is computable,
what's judge-territory, and what's currently unsolved — discipline by
discipline, sourced, not speculated.

## Art: closest to solved elsewhere, but the wrong scorer is easy to pick

Learned aesthetic scorers are a real, mature category, but they split into
two families with very different suitability:

- **Prompt-independent aesthetic scorers** (LAION-Aesthetics V2, NIMA,
  CLIP-IQA) predict "prettiness" from the image alone. LAION-Aesthetics is
  the default most projects reach for, and it's specifically the wrong
  default here: an audit ([arXiv:2601.09896](https://arxiv.org/abs/2601.09896))
  found it conflates incompatible rating scales in its training data,
  skews toward Western/English photographer taste, and over-rewards
  realistic landscapes/portraits — i.e. it actively fights a stylized game
  art direction rather than serving it.
- **Preference-trained scorers** (PickScore, ImageReward, HPSv2), trained on
  hundreds of thousands of real pairwise human votes, consistently
  outperform the aesthetic-only family ([arXiv:2509.21227](https://arxiv.org/abs/2509.21227)
  survey). PickScore reaches ~70.5% agreement with held-out human raters
  against a ~68% human-vs-human ceiling — i.e. near the ceiling of what
  pairwise agreement can achieve at all.

Implication: if this project ever wants a learned scorer bolted onto art
review, it should be a preference/pairwise model, never an absolute
aesthetic-only one, and ideally fine-tuned or re-anchored on this project's
own pinned references rather than used off the shelf.

## Animation: the strongest results of the three, several with direct precedent

No one publishes "detect linear vs. eased interpolation from F-curves" as a
named technique, but real, load-bearing prior art exists one layer down:

- **Physics-violation thresholds are perceptually calibrated.** Reitsma &
  Pollard (SIGGRAPH 2003, [CMU](http://graphics.cs.cmu.edu/nsp/projects/perception/perception.html))
  measured exactly how much velocity/acceleration error in motion a viewer
  notices before it reads as wrong. This is the right shape of result to
  build detectors against — a computed number with a human-perception
  threshold attached, not an arbitrary cutoff.
- **Anticipation and follow-through are literally signal-processing
  operators.** Wang et al., *The Cartoon Animation Filter*
  (SIGGRAPH 2006, [MSR](https://www.microsoft.com/en-us/research/publication/the-cartoon-animation-filter/)):
  applying an inverted Laplacian-of-Gaussian to a motion curve *creates*
  anticipation/overshoot/follow-through. The converse — correlating a
  curve against that operator to detect whether those principles are
  *present or absent* — is the single most promising concrete building
  block this research turned up, and as far as the research agent could
  find, nobody has published it as a detector. That's a real novel bet,
  not an adopted technique.
- **Slow-in/slow-out has its own filter** (White, Loken, van de Panne,
  [UBC](https://www.cs.ubc.ca/~van/papers/2006-siggraph-slowin.pdf)):
  near-zero second derivative at a segment boundary is the
  computational signature of unwanted linear interpolation.
- **Mocap-cleanup QA is mature and directly reusable**: foot-skate
  detection via joint-height/velocity thresholds (Kovar et al.'s original
  technique, superseded by learned contact models like UnderPressure,
  [arXiv:2208.04598](https://arxiv.org/abs/2208.04598)); jitter via SPARC
  (spectral arc length), reported as the most duration-unbiased,
  noise-robust smoothness metric across comparative studies
  ([JNER 2021](https://link.springer.com/article/10.1186/s12984-021-00949-6));
  IK-pop detection has no research literature but a known engineering
  pattern (threshold velocity/acceleration spikes at IK-blend boundaries,
  per [Epic's own foot-sliding docs](https://dev.epicgames.com/documentation/en-us/unreal-engine/fix-foot-sliding-with-ik-retargeter-in-unreal-engine)).
- **Learned motion critics exist but don't transfer.** MotionCritic
  ([arXiv:2407.02272](https://arxiv.org/abs/2407.02272)) trains a
  perceptual critic on 52k human preference pairs specifically *because*
  heuristics and distribution distances don't align with human perception
  on their own — but it's trained on realistic human motion capture, not
  stylized game character animation, so it's evidence of feasibility, not
  a reusable model.

Decomposition, stated plainly: **directly computable** — arcs (path
curvature vs. straight-line residual), timing/spacing (velocity/spacing
histograms), easing (2nd-derivative sign at segment boundaries),
follow-through/overlap (cross-correlation lag between parent/child joint
velocity peaks), squash & stretch (bone-length/volume deviation),
anticipation (LoG-operator correlation, the novel piece above).
**Proxy-only** — secondary action (non-primary-chain motion energy),
staging (rendered silhouette readability). **Irreducibly subjective** —
appeal, exaggeration; both are relative to a style target, not absolute,
and only a trained preference model (not a hand-written metric) can
approach them, bounded by its training distribution.

## Rigging: automated today means "well-formed," not "good"

This is the discipline with the least existing machinery for the exact
thing being asked. What's real in production:

- **Structural/schema validation is standard and cheap.** Pyblish
  validator plugins, AYON/OpenPype Maya publishers, Unreal's Data
  Validation framework, Autodesk Flow Studio's Character Rig Validation —
  all check naming conventions, hierarchy, required bone sets, root
  presence, referenced-scene hygiene. None of them look at deformation
  quality at all. This is worth building (it's cheap and well-precedented)
  but it answers "is this rig exportable," not "does this rig look right."
- **Pose sweeps are standard practice, but the verdict is manual.** "Rig
  wrecking" / range-of-motion stress tests are routine; the output is a
  video a human watches. The one place sweeps run in CI is regression
  testing — comparing a rig's GPU output against its own CPU output
  (DreamWorks' Toothless SIGGRAPH 2025 talk) — which proves determinism,
  not quality. No production tool that scores a pose sweep for
  deformation quality was found. **A silhouette-preservation-through-a-pose-sweep
  scorer, if built here, would be genuinely novel** — same status as the
  anticipation detector above: a reasonable, well-motivated bet, not an
  adopted technique.
- **Weight-paint analysis is the one place off-the-shelf leverage exists
  today.** Blender's own Easy Weight addon's "Weight Islands" panel
  detects disconnected/rogue weight regions, which is a
  geodesic-connectivity test: a vertex weighted to a bone whose influence
  region is a disconnected island on the mesh surface is almost certainly
  bleed. That heuristic is cheap to reimplement directly against Godot's
  4-influence glTF import constraints, and it's the highest-leverage,
  lowest-risk piece of this entire research pass — it's a solved,
  publicly documented technique waiting to be wired in.
- **Reference-based metrics exist but need a reference skeleton.** RigNet/
  UniRig's CD-J2J / CD-J2B / CD-B2B (joint-to-joint / joint-to-bone /
  bone-to-bone Chamfer distances) and per-vertex skinning-weight L1 give a
  real quantitative quality number — conditional on having a known-good
  reference rig to diff against, which a template-driven humanoid pipeline
  (this repo already has `blender_humanoid_template`) can plausibly supply.
- **Self-intersection is solidly tractable** as a no-reference check: pure
  discrete collision detection between non-adjacent triangles, no learned
  model needed.
- **No public studio QA checklist exists.** The defect taxonomy (candy-wrapper
  collapse under twist, volume loss at bends, joint bulging from dual
  quaternion skinning, self-intersection on the inside of a bend) is
  well-characterized in the *skinning literature* (Kavan et al.; the
  Jacobson/Sorkine Stretchable-and-Twistable-Bones paper), not in any
  public production document. Treat the academic papers as the source of
  truth, not a studio blog post — none exist publicly.

Honest bottom line from the research agent: "automate structural checks,
weight-island/normalization/influence-count analysis, self-intersection,
and reference-based joint/weight metrics. Everything about silhouette and
'does this look right' remains human in real studios." This repo would be
pushing past what any known production pipeline currently automates if it
attempted the silhouette/deformation-quality piece.

## The judging layer: pairwise over absolute, and a sharp maturity cliff

Where a hand-written metric can't reach, the fallback is a model judge. The
literature here is unusually clear on methodology and unusually clear that
the methodology's reliability collapses as you move away from still images.

**Pairwise/tournament judging is well-established and clearly superior to
absolute scoring.** Zheng et al.'s LLM-as-judge paper
([arXiv:2306.05685](https://arxiv.org/abs/2306.05685)) is the canonical
reference and names the failure modes to design around: position bias,
verbosity bias, self-enhancement bias. The multimodal follow-on,
MLLM-as-a-Judge ([arXiv:2402.04788](https://arxiv.org/abs/2402.04788)),
found GPT-4V hits 79.3% human agreement on pairwise comparison but
diverges sharply on absolute scoring and batch ranking — the same
conclusion a more recent theoretical paper formalizes as
"ranking-scoring decoupling"
([arXiv:2604.25235](https://arxiv.org/abs/2604.25235)): judges order
candidates correctly while their absolute numbers carry roughly 40% of the
score range in uncertainty on aesthetic tasks. **Any critic built here
should emit pairwise verdicts and accumulate Elo/Bradley-Terry rankings —
Diffusion-DPO, PickScore, and Chatbot-Arena-style Elo are all built this
way — and should never be asked to emit a 1-10 quality score.** Always
randomize A/B order per comparison to cancel position bias; that's a
one-line implementation detail with outsized effect.

**GPTEval3D is the direct precedent for 3D assets**
([arXiv:2401.04092](https://arxiv.org/abs/2401.04092)): GPT-4V does
pairwise comparisons of rendered 3D-asset turntables, converted to Elo.
That's the recipe to adapt for rig/character review here — render a
turntable or pose-sweep loop, pairwise-judge it, accumulate Elo, never ask
for an absolute score.

**But maturity drops fast past still images, and animation/rigging sit at
the bottom.** Ranked by how much this methodology can currently be trusted:
still-image preference models ≫ text LLM-as-judge ≫ video (VBench,
VideoScore) > 3D assets (GPTEval3D, 3DGen-Bench) > human motion
(MotionCritic, research-grade, human-mocap-only) ≫ **rigging (no learned
perceptual judge exists at all)**. The most on-point negative result:
VideoGameQA-Bench (NeurIPS 2025, Sony Interactive Entertainment + U. Alberta,
[site](https://asgaardlab.github.io/videogameqa-bench/)) found frontier
VLMs hit 78-83% on general glitch detection but specifically **"struggle
with glitches related to body configuration"** — i.e. the exact category
of fault an animation/rig critic would need to catch. A separate check
(GenAI-Arena, [arXiv:2406.04485](https://arxiv.org/abs/2406.04485)) found
GPT-4o reaches only ~49% agreement with human pairwise votes on generated
*image* preference, well short of PickScore's ~70%, meaning even the
best-case modality is not close to human-replacement quality yet.

Design consequence: a VLM judge over rendered art has real, cited
precedent for being useful. A VLM judge over rigging or animation output
should be treated as a coarse triage layer at best — good for catching
egregious failures, not trustworthy as the primary quality signal — with
the deterministic curve/mesh metrics above doing the real work.

## The shape of an architecture, if this gets built

One pattern, instantiated per discipline, not three bespoke systems:

1. **Rubric grounded in the discipline's real craft principles** — not
   invented per-project. 12 principles for animation, the skinning-defect
   taxonomy for rigging, composition/silhouette/color-harmony fundamentals
   for art.
2. **Deterministic proxies wherever the rubric allows them** — cheap, no
   model call, computed from curve/mesh data directly. This is the bulk of
   what's genuinely reusable from this research: LoG-correlation for
   anticipation, 2nd-derivative sign for easing, SPARC for jitter,
   cross-correlation lag for overlap, weight-island connectivity for
   bleed, CD-J2J/weight-L1 against a reference skeleton, self-intersection
   detection.
3. **Pairwise tournament judging for what proxies can't catch** — never
   absolute scoring, order-randomized, Elo/Bradley-Terry aggregation.
   Well-precedented for art and (via GPTEval3D) for rendered 3D turntables.
   Explicitly unproven for rigging/animation specifically — expect it to
   need its own validation before being trusted as a gate.
4. **Human approval stays the actual gate**, same as this repo's existing
   art QA rule (a "pass" is machine-checked, not approved; only fail is
   autonomous). Tournament judging's job is to cut the volume of raw
   generations down to a small finalist set so human attention is spent on
   judgment, not triage.
5. **Winners close the loop as exemplars.** This repo already does step 5
   for art (`ref_pin` → LoRA training in `bgate_core/styles.py`). No
   equivalent exists for rigs or animation clips — there's no obvious
   analogue to "train a LoRA on a good animation," so this step needs its
   own design, not a copy of the art version. Possibilities worth exploring
   later: few-shot exemplar conditioning at generation time, or using
   approved clips as curve-shape references for the deterministic proxies
   themselves (i.e. the proxies get calibrated against this project's own
   approved output, not fixed thresholds).

## What's genuinely novel here — be honest about the bet being made

Three pieces of this have no prior art at all, per the research above, and
building them is an R&D bet, not adopting an established technique:

- Anticipation/follow-through detection via Laplacian-of-Gaussian
  correlation, used as a *detector* rather than a *generator* of those
  effects (the Cartoon Animation Filter paper runs it the other direction).
- Silhouette-preservation scoring across an automated rig pose sweep — no
  production tool does this; sweeps exist, but the verdict is always human.
- A VLM pairwise judge specialized for stylized game rigging/animation —
  the closest analogues (MotionCritic, VideoGameQA-Bench) are either
  realistic-human-motion-only or show VLMs specifically weak at body-
  configuration faults.

None of that means don't build them — it means budget for them as
experiments with a real chance of not panning out, not as engineering
tasks with a known-good outcome. The weight-island/bleed detector and the
structural rig validators are the opposite: solved elsewhere, low risk,
should be the first things built if this moves to implementation.

## Failure modes to design against

- **Proxy gaming.** An agent optimizing against a fixed set of curve
  metrics will learn to satisfy them (technically-eased curves, technically
  non-linear arcs) without producing motion anyone would call good. Proxies
  should triage/filter candidates down, not be the sole or final signal.
- **Absolute scoring anywhere in the pipeline.** The literature is
  consistent: judges rank correctly and score unreliably. Every judge
  interface here should be pairwise.
- **A generator judging its own output.** Already a named failure in this
  repo's own design notes ("an art agent that judged its own frame approved
  off-style drift three times") — the same risk applies identically to a
  rig or animation generator grading its own work, and the fix is the same
  one already in place for art: an independent judging pass.
- **Treating "pass" as approval.** Consistent with this repo's existing
  rule: a passing verdict from any of this machinery is evidence, not
  authorization. Only a human promotes an asset.
- **Borrowing LAION-Aesthetics-style scorers uncritically.** Documented
  bias toward Western/photographic realism will actively penalize
  stylized game art; if a learned image scorer gets used at all, it should
  be the preference-trained family (PickScore/ImageReward-style), and
  ideally re-anchored on this project's own approved references rather
  than used as shipped.

## Open questions, not yet answered

- Is there budget/appetite for the two genuinely novel R&D pieces
  (anticipation detector, rig silhouette-sweep scorer), or should a first
  version scope to the solved pieces only (structural rig validation,
  weight-bleed detection, easing/arc/jitter metrics, self-intersection) and
  treat judge-based quality scoring as a later phase?
- What's the reference-skeleton story for the CD-J2J/weight-L1 rigging
  metrics — does the humanoid template pipeline reliably produce something
  usable as ground truth, or does that need its own work first?
- What does "closing the loop" (winners become exemplars) actually look
  like for animation and rigging, given there's no LoRA-equivalent for
  curve data or mesh topology? This is unsolved in the doc above and
  probably the single biggest open design question if this moves forward.
- Should judge-based tournament scoring even be attempted for rigging in a
  first version, given no prior art exists and the closest analogue
  (VideoGameQA-Bench) shows VLMs specifically weak at body-configuration
  faults? A deterministic-proxies-only first pass may be the higher-
  confidence scope for rigging specifically.
