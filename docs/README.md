# docs/

Two kinds of document live here. **Onboarding** is written for someone arriving
without context. **Findings** are write-ups of things that went wrong on real
production runs, plus the audits — kept because the reasoning is the useful
part, not as reference documentation. The reference is the
[README](../README.md) and the tool docstrings.

Every document is dated at the top and reflects the day it was written.

## Onboarding

New here, or never run an MCP server before? Read these three, in order.

| Document | What it is |
|---|---|
| [start-here.md](start-here.md) | The front door. What problem this solves, what an MCP server is, this project's vocabulary defined once, what actually happens when you dispatch an agent, and a first-session walkthrough. Assumes nothing. |
| [faq.md](faq.md) | The questions people actually asked, answered against the code — how the character art was really made, whether it works for 3D (honestly), what it costs, why it felt slow, and pointing it at a game you already have. |
| [glossary.md](glossary.md) | Every term this project uses in a narrow sense — seat, lane, lock, dispatch, cut line, pin, canon, evidence — a sentence or two each. |

## Findings and audits

| Document | What it is |
|---|---|
| [gap-analysis.md](gap-analysis.md) | Where the pipeline can improve tenfold, written after the first real production run (an arcade fighter built by ~30 seat agents over two days). Every gap is backed by something that actually happened and what it cost. Ranked by leverage. |
| [character-consistency.md](character-consistency.md) | Why sprite frames of the same character drift between generations, and why the orchestrator's confident *correction* was wrong while the art agent's refusal was right. The origin of the pinned-reference discipline. |
| [gear-pipeline.md](gear-pipeline.md) | Item-as-object versus gear-as-worn: two kinds of gear art with opposite economics, and why the pipeline keeps them separate. |
| [magic.md](magic.md) | Design philosophy: "magic is when the distance between intent and result approaches zero", and what that implies about which features are worth building. |
| [qa-nitpick-audit.md](qa-nitpick-audit.md) | **Historical (2026-07-25).** An eight-persona code-review audit of the whole product. Harsh and largely fair. **Much of it is now fixed** — read the dated status header at the top before treating any finding as current. |
| [ui-ux-audit.md](ui-ux-audit.md) | A blind UX audit: evaluators saw only rendered screenshots of all 15 views, never the source, and judged as first-time users. Partly worked; the error-surfacing themes are still open. |
| [history/](history) | Archived agent-to-owner handoff notes. Historical only — see each file's header. |
