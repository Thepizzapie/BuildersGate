# Design notes

2026-07-27, revised 2026-08-11. The concepts the product rests on.
[reference.md](reference.md) describes what each surface does.
[gotchas.md](gotchas.md) records what went wrong.

## Money and wall clock

The two things that run away unattended.

| Control | Where it acts |
| --- | --- |
| Spend ledger | Every paying call appends to it. |
| Per-item, per-day, per-project ceilings plus a concurrency cap | Checked before a process exists. |
| Watchdog | Kills the tree when it passes its runtime or cost ceiling, and says so on the item. |
| Dirty-tree refusal | On by default. Turn it off with `dispatch.allow_dirty` or `BGATE_ALLOW_DIRTY`. |

The dashboard's "dispatch all" has no cap of its own. Twenty queued items is
twenty claude trees on one laptop.

A run started on top of uncommitted work produces a diff that cannot tell the
agent's edits from yours, which is what the dirty-tree refusal is for.

## An agent may propose; only a human approves

A spawned session carries `BGATE_ACTOR=agent:item-<id>`, which is what makes
"approved" mean anything.

`art_qa_verdict` lets a reviewer FAIL a candidate outright. Refusing to ship is
a call a machine can make alone. A pass only records evidence and leaves the
revision a candidate waiting for a person. An art agent that judged its own
frame approved off-style drift three times; a second agent doing it instead is
the same failure with an extra hop.

## Facts versus prose

Entity `body` is prose for humans. `canon_fact` rows are one atomic, checkable
claim each ("The siege lasted seven years"). You cannot diff a paragraph for
contradictions. You can diff a sentence. `canon_check` reads facts.

`canon_check` is a filter, not a judge: deterministic lexical checks for retired
entities on stage, invented proper nouns, polarity flips and number
disagreements. No model call, so it runs on every write. `ok` means nothing
*mechanical* is wrong, and it will not catch thematic drift. An LLM adjudication
layer can consume this output; it cannot replace it.

## Assets lock, they do not merge

Two agents editing one `.blend` is the failure mode the `asset` table exists
for. Content-hashed, seat-locked, never merged.

## Blender gives facts back, not logs

`blender_run` returns per-object tri and vert counts off the *evaluated* mesh,
so modifiers count, plus UV warnings, materials, and optionally a render. A
script that throws is a normal result with `ok=False` plus the traceback and the
partial scene. An agent that cannot see what it built will confidently produce
nothing.

## Technology choices

| Choice | Reason |
| --- | --- |
| SQLite over Postgres | Projects are per-game and often throwaway. `.bgate/game.db` travels with the repo; a daemon per game is a tax with no return. |
| GDScript over .NET | The agent loop is edit, headless run, result. .NET puts a compile step between every iteration. GDScript is also what the models absorbed from Godot's docs. |
| FTS5 over embeddings | No daemon, no model download, no cold start. Semantic recall can layer in behind the same `find()` signature later. |

## Removed: the cut line

Scope tiers were ranked bands of ambition in the bible, with a `cut_line`
section marking where shipping stopped and `queue.add` refusing work under a cut
tier. Measured on a real project after months of use: two tiers, nothing cut,
seven untiered open items, zero refusals in the gate's entire life. Untiered
work had to be allowed through, filing under a tier was optional, so nothing was
ever filed under one.

Removed 2026-08-10. It cost three panels of the World view, a rule in every seat
brief, an MCP tool and a column on the busiest table in the schema. What
replaces it: a director who writes down what the project is not building, in the
bible, in words.
