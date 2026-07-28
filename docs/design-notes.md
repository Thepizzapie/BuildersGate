# Design notes

2026-07-27. The concepts the whole product rests on, and the technology choices
worth defending. [reference.md](reference.md) describes what each surface does.
[gotchas.md](gotchas.md) records what went wrong.

## The cut line

Scope tiers are ranked. The `cut_line` section marks where shipping stops.
Anything ranked at or below it is explicitly not being built. This is the only
mechanism that reliably stops an agent fleet from gold-plating. `scope_check(rank)`
answers "should I build this?" without a judgment call.

It is a refusal, not advice. `queue.add` will not FILE work under a cut tier, and
the dispatcher re-checks at the last possible moment before spawning a process.
The line moves, so an item queued legitimately can be retroactively out of scope
by the time anyone runs it, and spending an agent on that is the exact
gold-plating the tiers exist to stop.

Untiered work is deliberately allowed through, loudly flagged. Refusing it would
make the first cut line anyone draws reject the entire existing queue. The
predictable fix would be to turn the gate off, which is how a gate stops gating.

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
