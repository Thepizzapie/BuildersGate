# Design notes

2026-07-27. The concepts the whole product rests on, and the technology choices
worth defending. [reference.md](reference.md) describes what each surface does.
[gotchas.md](gotchas.md) records what went wrong.

## The cut line, and why it is gone

Scope tiers were ranked bands of ambition in the bible, with a `cut_line`
section marking where shipping stopped, `queue.add` refusing to file work under
a cut tier, and the dispatcher re-checking before spawning. It read as the
sharpest gate in the product.

It never refused anything, and it could not have. Untiered work had to be
allowed through - refusing it would make the first cut line anyone drew reject
that project's entire existing queue, and the predictable fix would be to turn
the gate off, which is how a gate stops gating. So untiered work was flagged and
passed, and because filing under a tier was optional and inconvenient, no work
item was ever filed under one. Measured on a real project after months of use:
two tiers, nothing cut, seven untiered open items, zero refusals in the gate's
entire life. The cost was three panels of the World view, a rule in every seat
brief, an MCP tool, and a column on the busiest table in the schema.

Removed 2026-08-10. A gate that cannot fail is worse than no gate: it teaches
everyone reading the rules that the rules are decoration. What replaces it is
the thing that was doing the work anyway - a director who writes down what the
project is not building, in the bible, in words.

## Money and wall clock

These are the two things that run away unattended.

Every paying call appends to a spend ledger. The dispatcher consults the budget
*before* a process exists: per-item, per-day and per-project ceilings, plus a
concurrency cap. The dashboard's "dispatch all" has no cap of its own, and twenty
queued items is twenty claude trees on one laptop.

Once running, a watchdog kills the tree when it passes its runtime or cost
ceiling and says so on the item.

A dispatch also refuses a dirty tree by default. A run started on top of
uncommitted work produces a diff that cannot tell the agent's edits from yours.

## An agent may propose; only a human approves

A spawned session carries `BGATE_ACTOR=agent:item-<id>`, and that is what makes
"approved" mean anything.

An art agent that judged its own frame approved off-style drift three times. A
second agent doing it instead is the same failure with an extra hop. So
`art_qa_verdict` lets a reviewer FAIL a candidate outright, because refusing to
ship is a call a machine can make alone. A pass only records evidence and leaves
the revision a candidate waiting for a person.

## Facts versus prose

Entity `body` is prose for humans. `canon_fact` rows are one atomic, checkable
claim each ("The siege lasted seven years"). You cannot diff a paragraph for
contradictions. You can diff a sentence. `canon_check` reads facts.

## canon_check is a filter, not a judge

Deterministic lexical checks: retired entities on stage, invented proper nouns,
polarity flips, number disagreements. No model call, so it can run on every
write.

It will not catch subtle thematic drift, and `ok` only means nothing *mechanical*
is wrong. An LLM adjudication layer can consume this output. It cannot replace
it, since a model checking its own output for canon drift is the fox guarding the
henhouse.

## Assets lock, they do not merge

Two agents editing one `.blend` is the failure mode the `asset` table exists for.
Content-hashed, seat-locked, never merged.

## Blender gives facts back, not logs

`blender_run` returns per-object tri and vert counts off the *evaluated* mesh, so
modifiers count, plus UV warnings, materials, and optionally a render. A script
that throws is a normal result with `ok=False` plus the traceback and the partial
scene. An agent that cannot see what it built will confidently produce nothing.

## Technology choices

**SQLite over Postgres.** Builders Gate projects are per-game and often
throwaway. A daemon per game is a tax with no return. `.bgate/game.db` travels
with the repo.

**GDScript over .NET.** The agent loop is edit, headless run, result. .NET puts a
compile step between every iteration. GDScript is also what the models have
absorbed from Godot's docs and forums.

**FTS5 over embeddings, for now.** No daemon, no model download, no cold start.
Semantic recall can layer in behind the same `find()` signature later.
