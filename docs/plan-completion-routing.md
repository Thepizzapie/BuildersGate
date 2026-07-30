# Plan: completion routing, notifications, and one settings surface

2026-07-29. A design to build against, not a shipped description. Everything
below is expressed in the machinery this repo already has — the point of writing
it down first is that three of tonight's features each added a switch in a
different place, and a fourth one done the same way makes the pile permanent.

**Revision 2**, after a gap analysis against the code. Two choices in the first
draft were wrong and are corrected here with the reasoning kept in place, because
both are mistakes that are easy to make again: the event log cannot be a JSONL
file (§2.1), and the director debrief would have been refused on arrival (§2.4).

Companion reading: [design-notes.md](design-notes.md) for the concepts,
[glossary.md](glossary.md) for the vocabulary (work chain, approval gate).

---

## 0. The complaint, stated exactly

> "Agents complete tasks but do not tell me, in app or otherwise. Better: report
> back to the director, and have the director either dispatch another agent to
> finish the implementation or ping me to ask if anything else is needed."

Three separate holes sit behind that one sentence, and they need different fixes.

**Hole 1 — the event log is write-only.** `queue._notify()` appends every status
transition to `.bgate/notify.jsonl`, and its own docstring says why: "so an
orchestrator (or the UI) can tail/long-poll one file instead of sleep-polling the
queue." Nothing reads it. Grepped: the writer, and a lane note in `seats.py`.

**Hole 2 — the director is not a subscriber.** A finished item spawns a QA agent
(under the agent gate) or parks in `review` (under the builder's gate), and in
both cases the *director* — the seat that owns what happens next — is told
nothing. Chains, added tonight, advance mechanically and silently: link 2 starts
because link 1 reached `done`, and no one narrates that.

**Hole 3 — nothing reaches a human who is not looking.** `_gates()` surfaces a
finished item as a sign-off gate, but only inside the console, only while that
view is open, and only for `SIGNOFF_HOURS`. The desktop app's `_notify()` is a
blocking `MessageBoxW` used for startup errors — it steals focus and holds a
thread, so it is not a per-event channel.

**And a fourth, which is the honest limit of this design:** a browser tab that is
closed cannot be notified. §2.3 says which channel survives that and which does
not, rather than letting a bell imply it fixed the whole complaint.

---

## 1. What already exists (build on these, do not duplicate)

| machinery | where | what it gives us |
|---|---|---|
| status transitions | `queue.set_status` → `_notify` | the event, already written, already at the right choke point |
| completion choke point | `queue.complete` | one place every finish funnels through |
| cross-process coordination | SQLite in WAL mode via `db.tx` | the ONLY thing here that is safe against many writers — see §2.1 |
| forward-only migrations | `db._MIGRATIONS` + `PRAGMA user_version` | how the event table lands |
| daemon-thread reactors | `qa_gate`, `autodeploy`, `steerpump` | the established pattern, and its three copied bugs |
| idempotency guard | `qa_gate._open_gate_exists()` | the pattern that makes re-delivery safe |
| durable small state | `workspace.get/set` (versioned, `StaleWrite`) | where `autopilot.on` and `gate.mode` already live |
| budget enforcement | `spend.check`, `spend_budget` row | the gate any new spending path must pass |
| observed writes | `queue._with_observed_writes` | the harness's file list, not the agent's claim |
| recursion guards | `qa_gate.GATED_SEATS` / `HELD_SOURCES` | the pattern for "never gate the gate" |
| in-page toasts + `askText` | `static/index.html`, `ask.js` | the UI primitives a drawer needs |

**The anti-pattern to avoid:** a fourth daemon thread. `qa_gate`, `autodeploy`
and `steerpump` are three copies of one loop, each with its own cutoff, cooldown
and fail-safe bookkeeping. `qa_gate`'s cutoff is the cost of that copying — it
reviews "only transitions after the server started", so every completion that
happens while the dashboard is down is never reviewed and nothing says so.

---

## 2. Part A — the event log and the router

### 2.1 `bgate_core/events.py` — a TABLE, not a file

> **Corrected from revision 1, which said JSONL with a byte cursor.** That cannot
> be implemented correctly here. `_notify` is a bare `open(..., "a")` run by
> whichever process flips the status — and `queue_complete` executes in the **MCP
> server process** (one per Claude session; four were live while this was
> written), while `_reap` and `settle_stranded` execute in the **dashboard
> process**. Multi-writer, no lock. A monotonic `seq` in that file needs a
> cross-process lock I would have to invent, and interleaved partial lines are a
> real Windows failure mode. Revision 1 reused the byte-cursor trick from
> `dispatch.read_activity`, which is the wrong precedent: that exists for agent
> **stdout** — huge, single-writer, streaming. This is low-volume and
> many-writer, and the repo already has exactly one safe answer for that.

Migration 0016 adds:

```sql
CREATE TABLE event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,   -- the cursor, for free
    kind       TEXT NOT NULL,
    ref        TEXT NOT NULL DEFAULT '',            -- item id, chain id, path
    actor      TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_event_kind ON event(kind, id);
```

API:

```python
emit(root, kind, ref="", payload=None) -> int          # returns id
since(root, seq=0, kinds=(), limit=200) -> dict         # {events, seq, gap}
cursor_get(root, consumer) -> int                       # workspace: consumer/<name>
cursor_set(root, consumer, seq) -> None
prune(root, keep_days=14) -> int
```

* `kind` is a dotted string and the vocabulary stays small on purpose:
  `item.done`, `item.review`, `item.failed`, `item.approved`, `item.rejected`,
  `chain.advanced`, `chain.stalled`, `gate.mode`, `budget.refused`,
  `director.question`, `agent.spawned`, `agent.exited`.
* **`gap: true` when a cursor points below the retained window.** A pruned range
  must be reported, not silently skipped — `read_activity` already has the
  vocabulary for this (`dropped`, `truncated`) and the lesson behind it.
* **`notify.jsonl` keeps being written, unchanged.** Its docstring advertises a
  tail/long-poll surface; repurposing or dropping it is a breaking change to a
  documented interface for no gain. The table is additive.

Why this is the scalable shape: every feature below — notify me, debrief the
director, fire a webhook, badge the window — is a **subscriber**, not another
thread with its own idea of "recent".

### 2.2 `bgate_ui/followup.py` — one router, and it must be pure

Runs beside the existing reactors and **absorbs `qa_gate`'s loop** rather than
sitting next to it. On each terminal transition, in order:

1. **failed** → emit `item.failed`; if `followup.auto_reopen_failures` is on and
   `attempts` is under the cap, `queue.reopen` with the failure text; else leave
   it and notify.
2. **gate mode `agent`** → today's QA spawn, behaviour unchanged, now cursor-driven
   (which is what fixes the startup-cutoff hole).
3. **gate mode `builders`** → the item is already in `review`; notify, do nothing
   else. The chain stays blocked, which is that mode's whole point.
4. **done with a chain successor** → nothing to dispatch (autodeploy picks the
   successor up now that it is ready); emit `chain.advanced` so the handoff is
   legible while it happens instead of inferred afterwards.
5. **done and nothing follows** → the **director debrief** (§2.4), if enabled.

Two constraints on the shape, both learned from the reactors this replaces:

* **Pure core, threaded edge.** The decision is a function
  `decide(events, settings, board) -> [actions]` with no I/O, and a thin runner
  applies the actions. `qa_gate`'s tests call `_scan_once` directly *because* it
  is not separable; this one is testable from a fixture list of events.
* **Every action carries an idempotency guard.** Delivery is at-least-once: a
  subscriber that acts and then dies before writing its cursor will act again on
  restart — double QA spawn, double debrief, double webhook. `qa_gate` already
  guards with `_open_gate_exists()`; each branch here declares the equivalent
  ("a debrief for this chain already exists", "this event id already notified").
* **Staleness policy, because catching up is not always desirable.** A subscriber
  that was down for eight hours must not fire eight hours of pings and debriefs.
  Rules: notifications COLLAPSE on resume ("11 items finished while you were
  away"), and a debrief is skipped when the completion is older than
  `followup.max_age_min` (default 30) with one `item.done` notice instead.

### 2.3 Reaching the human — and what survives a closed tab

| channel | survives a closed browser | how |
|---|---|---|
| header bell + drawer | **no** | `GET /api/events?since=<seq>` on the console's existing poll — not a second timer |
| in-page toast | no | existing `toast()` |
| desktop window title/badge | yes, while `bgate app` runs | WebView2 title; **not** `MessageBoxW` — blocking and focus-stealing is worse than silence |
| webhook | **yes** | one optional `notify.webhook` URL, POSTed by the same subscriber |

Said plainly rather than implied: **the bell only tells you things while you are
already looking.** It is worth having because the drawer is where "what happened
while I was in another view" gets answered, but the only channels that solve the
original complaint are the desktop window and the webhook.

Webhook rules, because a loopback service POSTing user-supplied URLs is an SSRF
and an exfiltration path: **https only, no private or link-local address ranges,
one attempt with a short timeout, failures logged to the activity ledger.** And
it deliberately breaks the "nothing leaves this machine" promise in
[start-here.md](start-here.md) — so it ships off, and the settings row says so.

### 2.4 The director debrief — and its leash

A completed item files a `source='completion'` work item for the director whose
brief carries: what finished and its result note; the **harness-observed file
list** (`_with_observed_writes`, not the agent's self-report); the chain, the
position in it, and what is queued or blocked behind it; the active gate mode; the
budget left today. Then exactly three legal moves — **dispatch the follow-up**
(`queue_add` / `queue_add_chain`), **ask the human one question** (`ask_human`,
§2.5), or **close it out** saying nothing further is needed. It may not do seat
work itself; the brief restates that standing rule because a debrief holding a
fresh diff is the most tempting place to break it.

> **Corrected from revision 1, which would have been refused on arrival.**
> `dispatch.py:421` refuses any dispatch on a dirty tree, with no exemption by
> source — and an agent that just finished writing files *leaves the tree dirty by
> definition*. That is the exact state the debrief reacts to, so nearly every
> debrief would have been filed, refused, cooled down by autodeploy, and looked
> like it silently never happened. So: the debrief dispatches with
> `allow_dirty=True` deliberately (it reads and decides; it does not edit), and
> the brief SAYS the diff it is looking at is uncommitted, because a director
> that assumes a clean tree will draw the wrong conclusion from `git status`.

And the second thing revision 1 left out: **a debrief needs `bgate serve` up.**
Filed on a dead board it is the exact trap `DIRECTOR_PROTOCOL` already warns
about — a queued row that looks like delegated work and is not. The router only
files one when it can see a live dispatcher, and says so in the notification when
it cannot.

The leash, because this spends money on every completion:

| guard | value |
|---|---|
| default | **off** (`followup.director_debrief`) |
| one debrief per **chain**, not per link | last link only, guarded by an existence query |
| never debriefs | `source in ('qa-gate', 'qa-gate-escalation', 'completion', 'chat')` |
| staleness | skipped past `followup.max_age_min` (default 30) |
| rate cap | `followup.max_per_hour`, default 4 |
| budget | the ordinary `spend.check` path — a debrief is a dispatch |

A completion loop that debriefs its own debriefs is the money pump
`qa_gate.MAX_ROUNDS` exists to stop; the source guard is the same trick.

### 2.5 `ask_human(question, refs)` — the missing ping

A new MCP tool for the director seat. Emits `director.question`, which lights the
bell and renders in the console as a card with a reply box.

Deliberately **not** a work item: a question that becomes a queued row is a row
somebody has to dispatch in order to read, which is how "ask the human" turns
into "spawn an agent to ask the human".

The answer has to land somewhere that will actually be read, and that depends on
whether the asker is still alive — revision 1 said "the item's brief", which for
a `done` item is writing to nobody:

* **asker still running** → the steer inbox (`steerbox` → `steerpump`), which is
  the path a mid-run correction already takes;
* **asker finished** → `handoff_note(kind='decision')` plus the answer attached to
  the question event, so the next session and the next debrief both see it;
* **question unanswered past `notify.question_stale_h`** → one `chain.stalled`
  reminder, not a repeat of the question.

### 2.6 Time-based events (the gap revision 1 had no answer for)

The bus is transition-driven, and the failure being fixed is partly an ABSENCE of
transitions: an item parked in `review` that nobody approves, a chain whose next
link never became ready, a question nobody answered. None of those emit anything,
so the quiet failure mode gets reintroduced one layer up.

So there is one heartbeat producer, on the dashboard's existing tick, that emits
from elapsed time rather than from change:

* `chain.stalled` — a chain with a `review`/blocked head untouched for
  `notify.stall_hours` (default 2), once per chain per stall, reset on movement.
* `item.aging` — a `review` item older than the same window.

One emitter, two rules, and every consumer already handles both because they are
ordinary events.

---

## 3. Part B — one settings surface

Tonight's switches landed in four different mechanisms:

| where | examples |
|---|---|
| SQL row | `spend_budget`: per-item/day/project USD, `max_runtime_s`, `max_concurrent`, `enforced` |
| workspace doc | `director/autopilot`, `director/gate` |
| env var only | `BGATE_QA_GATE`, `BGATE_DIRECTOR_MODE`, `BGATE_ALLOW_DIRTY`, `BGATE_ISOLATION` |
| module constant | `qa_gate.MAX_ROUNDS`, `SIGNOFF_HOURS`, `PHASE_CAP`, the console poll intervals |

Nothing lists them, nothing says which the environment is overriding, and adding
one means editing a route, a payload shape and a template.

### 3.1 `bgate_core/settings.py` — a registry, not a new store

One declarative table describing settings that **already live** where they live:

```python
Setting(key="gate.mode", group="Gates", kind="enum",
        choices=("none", "agent", "builders"), default="agent",
        scope="project", store=("workspace", "director", "gate", "mode"),
        # NOT env="BGATE_QA_GATE". That var is a boolean kill switch, so it does
        # not SUPPLY this value, it COERCES it — see 3.2.
        env_coerce=("BGATE_QA_GATE", lambda raw: "none" if _falsey(raw) else None),
        help="Who signs off before an agent's work counts as done.")
```

* `GET /api/settings` → groups → fields → `{value, default, source, help}` where
  `source` is `default | stored | env`.
* `PATCH /api/settings` validates against the declared kind/range in ONE place.
* The UI renders **from the description**, so a new toggle is one registry entry
  and no UI change. That is the scalability requirement, concretely.
* `bgate doctor` prints effective values and their source — which is what stops
  an env override from being invisible.

Storage is not moved; the registry points at the existing store per setting. Two
consequences worth stating:

* **`/api/spend/budget` becomes a thin alias** over the registry rather than a
  second writer with its own validation. Two write paths for one SQL row is
  exactly the duplication this part claims to delete.
* **Client-side settings need delivery.** `console.poll_*` and `graph.phase_cap`
  are JS constants; they ride in the index-page bootstrap that already
  cache-busts the module `src` attributes — not a second fetch on load — and every
  one keeps its hardcoded fallback for the case where the bootstrap is missing.

### 3.2 Precedence, stated once

**env > project stored > default**, and the API always reports which won, so the
UI can grey out what an env var owns — the gate control already does this for
`BGATE_QA_GATE` and this generalises it.

Two kinds of env override, because the existing vars are not one kind:

* **supplying** — the var holds the value (`BGATE_DIRECTOR_MODE=collide`);
* **coercing** — the var is a switch that forces a value (`BGATE_QA_GATE=0` forces
  `gate.mode` to `none`). Revision 1 modelled this as `env=NAME` on an enum,
  which is a type mismatch: a boolean cannot be read as one of three modes.

### 3.3 The initial registry

| group | keys |
|---|---|
| Dispatch | `autopilot.on`, `dispatch.allow_dirty`, `dispatch.isolation`, `dispatch.max_concurrent`* |
| Gates | `gate.mode`, `qa.max_rounds`, `signoff.hours` |
| Follow-up | `followup.director_debrief`, `followup.max_per_hour`, `followup.max_age_min`, `followup.auto_reopen_failures` |
| Notifications | `notify.in_app`, `notify.kinds`, `notify.webhook`, `notify.stall_hours`, `notify.question_stale_h`, `notify.quiet_hours` |
| Budget | the five `spend_budget` fields* |
| Console | `console.poll_live_ms`, `console.poll_idle_ms`, `graph.phase_cap` |

`*` already stored in `spend_budget`; the registry describes them rather than
copying them.

### 3.4 Where it appears

A **Settings** view in the left nav under Command, grouped as the registry is. The
console keeps its two inline controls (auto-deploy, the gate segmented control) —
a switch that matters mid-run must be reachable without leaving the canvas — but
both read through the registry, so there is one truth and one validation path.

---

## 4. Order of work

Each step ships alone with its own tests.

1. **`events.py` + migration 0016 + cursors + `/api/events`.** No behaviour
   change: the table, the API and the tests only. Replayable from a fixture,
   which is what makes every later step testable without threads.
2. **`followup.py` with branches 1–4**, absorbing `qa_gate`'s loop. Same
   behaviour, minus the startup-cutoff hole. Pure `decide()` + thin runner, with
   the idempotency guard per branch.
3. **`settings.py` + `/api/settings` + doctor output.** Migrate `gate.mode` and
   `autopilot.on` to read through it (same storage, no data move), and make
   `/api/spend/budget` an alias.
4. **Notifications**: bell, drawer, `notify.*`, webhook (off by default, with the
   address rules).
5. **Heartbeat producer** (`chain.stalled`, `item.aging`) — small, and everything
   downstream already handles it.
6. **`ask_human`** + the console card + the three answer paths.
7. **Director debrief** — default off, capped, `allow_dirty`, live-dispatcher
   check, one per chain.

Step 5 lands before 6 and 7 on purpose: the stall reminder is what stops the new
routing from having its own quiet failure mode.

## 5. Non-goals, said out loud

* **No cloud push and no accounts.** The webhook is the only way off the machine,
  and it ships off.
* **No fourth daemon thread.** Subscribers run in the dashboard's existing loop.
* **No per-user read state.** Single-operator tool; the unread cursor is per
  project, not per person.
* **No Slack/Discord/email in-tree.** They are webhook consumers, and each one
  here is a dependency and a credential.
* **The debrief is opt-in forever.** A feature that spends money per completion
  must never arrive switched on in an upgrade.
* **A closed browser is not notified.** Stated in §2.3 rather than papered over.
