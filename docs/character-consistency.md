# Character consistency — findings and plan

Why this document exists: during production of a real game, sprite frames of the
same character drifted between generations (build, clothing details, flame
shapes varying frame-to-frame), and — more instructive — the orchestrator issued
a confidently WRONG correction, describing the character from stale prompt text
instead of the approved reference. The art agent re-checked the pinned reference,
refused, and escalated. It was right. Both failures point at the same root:
**character identity lived in prose that brains (human or model) re-imagine,
instead of in artifacts that get measured.**

## Measured findings (real production frames, 2026-07-17)

Setup: pinned character reference vs generated pose frames, two characters,
plus a cross-character floor.

| Check | Result | Verdict |
|---|---|---|
| Palette nearest-neighbor RGB distance | on-model 20–24, drift-suspect 26 | separates COLOR drift only; blind to identity |
| CLIP ViT-B/32 cosine vs ref | 0.91–0.92 for everything | useless at this granularity — no separation |
| **Unicom ViT-B/32 cosine vs ref** | same character 0.66–0.83; **cross-character 0.40–0.51** | separates characters cleanly; but extreme poses (duck 0.57) overlap the floor |

Conclusions:
1. **No global-similarity metric is a gate.** Pose variance overlaps identity
   variance. Unicom works as a *tripwire* (score < ~0.55 vs the neutral ref →
   flag for review), not as an accept/reject.
2. **The reliable detector is structured visual judgment** — an agent looking at
   reference and candidate side by side against an explicit trait checklist.
   The failures happened when agents judged frames in isolation, from memory.
3. **Prevention beats detection.** Identity drifts at generation time when the
   prompt under-specifies who the character is and the pose language implies
   anatomy the character doesn't have.

## The plan

### Phase 1 — character profiles (prevention)
A `character_profile` per fighter, stored in the project DB, created AT PIN TIME:

- **trait text**: written by a vision pass looking at the approved reference —
  never from anyone's memory of a prompt. ("Pepper-headed humanoid; human
  muscular torso, bare fists; flame-tipped green stem; black sash + pants…")
- **negative traits**: what generations must not introduce.
- **palette**: extracted hex swatches + tolerance.
- **pins**: the reference, and a growing set of *accepted frames* by pose.

`image_sprites` / `image_edit` assemble identity language FROM the profile —
agents describe only the pose (limb positions, never anatomy words). The
orchestrator incident becomes structurally impossible: nobody types identity
prose anymore.

### Phase 2 — conditioning upgrades (prevention)
- **Multi-reference edits**: condition each pose on the reference PLUS the
  nearest-pose accepted frame (edit() already accepts multiple images).
- **Model sheets**: a turnaround (front/side/action + palette bar) as THE pinned
  reference — conditions far better than a lone pose.
- **First-frame gate**: the first frame of each animation batch is reviewed
  before the rest generate; an off-model first frame stops the batch at 1 spend,
  not N.

### Phase 3 — structured checking (detection)
- `consistency_check(candidate, character)` tool: composes a side-by-side image
  (reference | accepted-nearest-pose | candidate) plus the profile's trait
  checklist, so every reviewing agent judges from the same view; attaches the
  deterministic tripwires (palette distance, Unicom-vs-ref, Unicom-vs-nearest-
  accepted) as advisory scores in the result.
- Pose-matched embedding comparison (candidate vs nearest ACCEPTED frame rather
  than the neutral ref) to reduce the pose confound; head-crop comparison as an
  experiment (identity concentrates in the head for mascot-style designs).

### Phase 4 — exploration
- Per-character few-shot classifier over accepted frames (grows with the game).
- Region embeddings / background-removal effects on the tripwire quality.
- Cross-asset style coherence (sprites vs portraits vs UI) via the same profile.

## Rules of thumb that already hold
- The pinned reference is canon. Corrections that contradict it require
  re-pinning (a director-level act), not a mid-flight prose description.
- An agent that pushes back on instructions contradicting the pinned artifact
  is behaving correctly. Reward the escalation path; it caught a real error.
