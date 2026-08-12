# Researching "taste" for art, rigging, and animation agents

2026-08-07, status refreshed 2026-08-11. Research notes. Agents can execute an
art, rig or animation task but cannot judge whether the result is any good.
This is what the literature says is computable, what is judge territory, and
what is unsolved.

## What has been built since

Five of the pieces this pass proposed now exist. Read the sections below as
background for them, not as a to-do list.

| Proposal | Shipped as |
| --- | --- |
| Curve metrics: arcs, easing, jitter, LoG anticipation | `animation_curves` (`arc_deviation`, `velocity_profile`, `sparc`, `anticipation`) |
| Weight-island / bleed detection | `blender_weights` |
| Silhouette scoring across a pose sweep | `blender_silhouette` (still marked experimental) |
| Self-intersection under pose | `blender_flex` (`new_self_pairs`) |
| Pairwise judging with randomised order and Elo | `art_tournament_verdict`, `art_tournament_standings` |

Still unbuilt: reference-based rig metrics against a template skeleton
(CD-J2J, weight-L1), a learned preference scorer, and any judge for
rigging or animation output.

## The reframe

What this repo had before was a **drift detector**: does the output match a
reference a human already approved. `artdirection.py` says so about itself.

The gap is not a missing rubric, it is a missing evaluation mechanism for two of
the three disciplines. `consistency_check` diffs one frame against one
reference, a single-image problem. A rig is judged by how a mesh deforms across
a **range of motion**; one pose tells you nothing. An animation is judged by the
**relationship between frames**: velocity, easing, arcs, lag between parent and
child joints. Diffing frame N against a reference is structurally blind to all
of that.

Each discipline has decades of formalised craft that partly decomposes into
computable things: the 12 principles of animation, rigging deformation theory,
art fundamentals.

## Art: pick the right family of scorer

| Family | Examples | Verdict |
| --- | --- | --- |
| Prompt-independent aesthetic | LAION-Aesthetics V2, NIMA, CLIP-IQA | Wrong default here. An audit ([arXiv:2601.09896](https://arxiv.org/abs/2601.09896)) found LAION conflates incompatible rating scales, skews to Western/English photographer taste, and over-rewards realistic landscapes and portraits, so it fights a stylised art direction. |
| Preference-trained | PickScore, ImageReward, HPSv2 | Consistently better ([arXiv:2509.21227](https://arxiv.org/abs/2509.21227)). PickScore reaches ~70.5% agreement with held-out human raters against a ~68% human-vs-human ceiling. |

If a learned scorer is ever bolted onto art review, use a preference/pairwise
model, re-anchored on this project's own pinned references.

## Animation: the strongest results of the three

| Signal | Technique | Source |
| --- | --- | --- |
| Perceptual thresholds for physics violation | Measured velocity/acceleration error a viewer notices | Reitsma & Pollard, SIGGRAPH 2003 ([CMU](http://graphics.cs.cmu.edu/nsp/projects/perception/perception.html)) |
| Anticipation, follow-through | Inverted Laplacian-of-Gaussian on a motion curve *creates* them; correlating against the operator to *detect* them is this project's own bet | Wang et al., *The Cartoon Animation Filter*, SIGGRAPH 2006 ([MSR](https://www.microsoft.com/en-us/research/publication/the-cartoon-animation-filter/)) |
| Slow-in / slow-out | Near-zero second derivative at a segment boundary is the signature of unwanted linear interpolation | White, Loken, van de Panne ([UBC](https://www.cs.ubc.ca/~van/papers/2006-siggraph-slowin.pdf)) |
| Foot skate | Joint-height/velocity thresholds; learned contact models superseded them | Kovar et al.; UnderPressure ([arXiv:2208.04598](https://arxiv.org/abs/2208.04598)) |
| Jitter | SPARC (spectral arc length), the most duration-unbiased and noise-robust smoothness metric in comparative studies | [JNER 2021](https://link.springer.com/article/10.1186/s12984-021-00949-6) |
| IK pop | No research literature; known engineering pattern of thresholding velocity/acceleration spikes at IK-blend boundaries | [Epic foot-sliding docs](https://dev.epicgames.com/documentation/en-us/unreal-engine/fix-foot-sliding-with-ik-retargeter-in-unreal-engine) |

**Learned motion critics do not transfer.** MotionCritic
([arXiv:2407.02272](https://arxiv.org/abs/2407.02272)) trains a perceptual
critic on 52k human preference pairs precisely because heuristics and
distribution distances do not align with human perception, but it is trained on
realistic human mocap, not stylised game animation. Evidence of feasibility, not
a reusable model.

Decomposition:

- **Directly computable**: arcs (path curvature vs straight-line residual),
  timing and spacing (velocity histograms), easing (2nd-derivative sign at
  segment boundaries), follow-through and overlap (cross-correlation lag between
  parent and child joint velocity peaks), squash and stretch (bone-length /
  volume deviation), anticipation (LoG correlation).
- **Proxy only**: secondary action (non-primary-chain motion energy), staging
  (rendered silhouette readability).
- **Irreducibly subjective**: appeal, exaggeration. Both are relative to a style
  target, and only a trained preference model can approach them.

## Rigging: automated today means "well-formed", not "good"

- **Structural/schema validation is standard and cheap.** Pyblish validators,
  AYON/OpenPype Maya publishers, Unreal Data Validation, Autodesk Flow Studio
  Character Rig Validation. All check naming, hierarchy, required bone sets,
  root presence. None look at deformation quality. This answers "is this rig
  exportable", not "does it look right".
- **Pose sweeps are standard practice, but the verdict is manual.** Rig-wrecking
  and range-of-motion stress tests are routine and the output is a video a human
  watches. The one place sweeps run in CI is regression testing, comparing a
  rig's GPU output against its own CPU output (DreamWorks' Toothless, SIGGRAPH
  2025), which proves determinism, not quality. No production tool scores a pose
  sweep for deformation quality.
- **Weight-paint analysis is where off-the-shelf leverage exists.** Blender's
  Easy Weight addon's "Weight Islands" panel is a geodesic-connectivity test: a
  vertex weighted to a bone whose influence region is a disconnected island is
  almost certainly bleed. Cheap to reimplement against Godot's 4-influence glTF
  import constraint. Lowest risk, highest leverage in this whole pass.
- **Reference-based metrics need a reference skeleton.** RigNet/UniRig's CD-J2J,
  CD-J2B, CD-B2B Chamfer distances and per-vertex skinning-weight L1 give a real
  quality number, conditional on a known-good reference rig. A template-driven
  humanoid pipeline (`blender_humanoid_template`) can plausibly supply one.
- **Self-intersection is tractable** as a no-reference check: discrete collision
  detection between non-adjacent triangles, no learned model.
- **No public studio QA checklist exists.** The defect taxonomy (candy-wrapper
  collapse under twist, volume loss at bends, joint bulging from dual quaternion
  skinning, self-intersection inside a bend) is characterised in the skinning
  literature (Kavan et al.; Jacobson/Sorkine Stretchable-and-Twistable-Bones),
  not in any public production document.

Bottom line: automate structural checks, weight-island and influence-count
analysis, self-intersection, and reference-based joint/weight metrics.
Silhouette and "does this look right" remain human in real studios.

## The judging layer: pairwise, never absolute

**Pairwise judging beats absolute scoring.** Zheng et al.
([arXiv:2306.05685](https://arxiv.org/abs/2306.05685)) is canonical and names
the failure modes to design around: position bias, verbosity bias,
self-enhancement bias. MLLM-as-a-Judge
([arXiv:2402.04788](https://arxiv.org/abs/2402.04788)) found GPT-4V hits 79.3%
human agreement on pairwise comparison but diverges sharply on absolute scoring
and batch ranking. A later paper formalises this as ranking-scoring decoupling
([arXiv:2604.25235](https://arxiv.org/abs/2604.25235)): judges order candidates
correctly while their absolute numbers carry roughly 40% of the score range in
uncertainty on aesthetic tasks.

**So: emit pairwise verdicts, accumulate Elo/Bradley-Terry, never a 1-10 score.
Randomise A/B order per comparison to cancel position bias.** GPTEval3D
([arXiv:2401.04092](https://arxiv.org/abs/2401.04092)) is the direct precedent
for 3D assets: GPT-4V pairwise-compares rendered turntables, converted to Elo.

**Maturity drops fast past still images.** Ranked by how far the methodology can
currently be trusted: still-image preference models, then text LLM-as-judge,
then video (VBench, VideoScore), then 3D assets (GPTEval3D, 3DGen-Bench), then
human motion (MotionCritic, mocap only), and last **rigging, where no learned
perceptual judge exists at all**.

Two numbers that set the ceiling:

- VideoGameQA-Bench (NeurIPS 2025, Sony Interactive Entertainment and U.
  Alberta, [site](https://asgaardlab.github.io/videogameqa-bench/)): frontier
  VLMs hit 78-83% on general glitch detection but specifically struggle with
  glitches related to body configuration, the exact category a rig or animation
  critic needs.
- GenAI-Arena ([arXiv:2406.04485](https://arxiv.org/abs/2406.04485)): GPT-4o
  reaches only ~49% agreement with human pairwise votes on generated image
  preference, against PickScore's ~70%.

A VLM judge over rendered art has cited precedent. A VLM judge over rigging or
animation is a coarse triage layer at best, with the deterministic metrics doing
the real work.

## The architecture

One pattern per discipline, not three bespoke systems:

1. **Rubric grounded in the discipline's real craft principles**, not invented
   per project.
2. **Deterministic proxies wherever the rubric allows**: LoG correlation for
   anticipation, 2nd-derivative sign for easing, SPARC for jitter,
   cross-correlation lag for overlap, weight-island connectivity for bleed,
   CD-J2J and weight-L1 against a reference skeleton, self-intersection.
3. **Pairwise tournament judging for what proxies cannot catch.** Never absolute
   scoring, order-randomised, Elo aggregation. Unproven for rigging and
   animation specifically.
4. **Human approval stays the gate.** A pass is machine-checked, not approved;
   only fail is autonomous. Judging cuts raw generations down to a finalist set
   so human attention goes on judgment, not triage.
5. **Winners close the loop as exemplars.** Done for art (`ref_pin` to LoRA
   training in `bgate_core/styles.py`). No analogue exists for rigs or animation
   clips. Options worth exploring: few-shot exemplar conditioning at generation
   time, or calibrating the deterministic proxies against this project's own
   approved output instead of fixed thresholds.

## The bets with no prior art

Three pieces are R&D, not adopted technique. Two are now built and should be
read as experiments that might not pan out.

- LoG correlation used as a *detector* of anticipation rather than a generator
  of it. Built, in `animation_curves`.
- Silhouette preservation across an automated pose sweep. Built, in
  `blender_silhouette`.
- A VLM pairwise judge specialised for stylised game rigging and animation. Not
  built. The closest analogues are realistic-human-motion-only or show VLMs weak
  at body-configuration faults.

## Failure modes to design against

- **Proxy gaming.** An agent optimising against fixed curve metrics will satisfy
  them (technically eased, technically non-linear) without producing motion
  anyone would call good. Proxies triage; they are not the final signal.
- **Absolute scoring anywhere.** Judges rank correctly and score unreliably.
- **A generator judging its own output.** An art agent that judged its own frame
  approved off-style drift three times. The fix is an independent judging pass.
- **Treating "pass" as approval.** A passing verdict is evidence, not
  authorisation. Only a human promotes an asset.
- **Borrowing LAION-Aesthetics-style scorers uncritically.** Documented bias
  toward Western photographic realism will penalise stylised game art.

## Open questions

- What is the reference-skeleton story for CD-J2J and weight-L1? Does the
  humanoid template pipeline reliably produce something usable as ground truth?
- What does "winners become exemplars" look like for animation and rigging,
  given there is no LoRA equivalent for curve data or mesh topology? Still the
  biggest open design question.
- Should tournament judging be attempted for rigging at all, given no prior art
  and VideoGameQA-Bench's body-configuration result? A deterministic-proxies-only
  first pass is the higher-confidence scope.
- Do `blender_silhouette`'s and `animation_curves`' thresholds hold on this
  project's own stylised clips? Both ship with borrowed constants that were
  never validated here.
