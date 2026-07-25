# Builders Gate — QA nitpick audit (FE + BE)

_8 game-developer personas audited assigned surfaces of the app as a static code review, then a synthesis pass deduped and ranked. 145 raw findings -> 30 to add, 77 to adjust, 10 themes._

Date: 2026-07-25 · Branch: `main` @ `1344575`

---

## Executive summary

Builders Gate is a genuinely good domain model wearing a prototype's UI and a single-user's operational assumptions. The thinking that is hard to buy is present and correct: the cut line as an anti-gold-plating device, canon facts vs prose, "binary assets lock, they don't merge," verifying a glTF tri count inside a real headless Godot rather than on disk, the playtest voice-to-telemetry join on one clock, immutable artifact revisions, and adapters full of scar tissue (stdin=DEVNULL because MCP eats stdio, 0-byte-exe rejection, chroma-key for gpt-image punching holes in eyes). All eight personas independently said some version of "I would use this on a jam prototype tomorrow." None of them would put it near a shipping repo, and they converged on the same three reasons. First, there is no version control in the trust model: agents run `--permission-mode acceptEdits` with Bash straight into the live working tree, there is no branch, no commit, no diff surface anywhere (iterations.py runs `git diff --binary HEAD` and throws the diff away to keep a sha256), and no revert. Second, there is no ceiling on anything — no spend cap on agent sessions, no wall-clock timeout, no concurrency limit on batch dispatch, and the QA gate can loop fail→reopen→re-dispatch forever. Third, the enforcement the README sells as "teeth" is theater: the PreToolUse hook only inspects Write/Edit/MultiEdit/NotebookEdit and returns ALLOW for Bash, `seat_configure` lets a seat widen its own lanes to `**`, `can_write` ignores `lock_owner` so two same-seat agents both pass the lock gate, and asset leases are written and heartbeat but never once compared against the clock. Where it genuinely helps: painted-art iteration with pinned refs, the Blender/Godot adapter legs, the playtest capture-to-classified-feedback pipeline, and the agent cockpit (stream-json parsing, live steering with consumption latency, orphan reaping) — that last one is further along than anything comparable. Where it is theater: the workflow builder's gate/select/consistency nodes (they emit English prose into a director brief and gate nothing), the cut line (no rank on work_item, nothing consults `scope_check`), the art QA gate (`art_qa_verdict(verdict='pass')` calls `artifacts.review(..., 'approved')` directly, so an LLM promotes art to approved with zero humans), and the QA verdict parser (a done item with no VERDICT marker renders green PASS). The cross-cutting tell is that this has only ever run on one game on one machine: `assetCategory()` hardcodes "scoville"/"tommy", the play-panel hint advertises fighting-game controls the shipped 2D template does not implement, SEAT_RULES injects another game's pinned ref names into every dispatch, and `pip install` from a wheel yields a dashboard with no JavaScript and a scaffolder with no templates because `package-data` is `static/*.html`. There is no CI. Verdict: an excellent single-player tool and an excellent set of ideas; six to eight weeks from being a defensible studio tool, and the six to eight weeks are almost entirely about isolation, ceilings, one error contract, and a human write path.

---

## Top 10 — do these first

1. Unify the HTTP error envelope AND make every frontend mutation read the response — one shared mutate() with a toast. Every other finding in this report is harder to diagnose while every failure in the product renders as a blank panel or a button that does nothing; this is a day of work that makes the tool honest.
2. Add `PRAGMA busy_timeout` (+ retry-with-jitter, + a guarded _migrate). One line stands between a normal 8-agent fan-out and 'database is locked' silently losing an agent's completed work. Cheapest blocker in the report.
3. Stop art_qa_verdict('pass') from calling artifacts.review(...,'approved') — write qa_review metadata and a 'qa_passed' status instead. An LLM currently promotes art to canon with zero humans, which is the exact failure the art-QA router was built to prevent.
4. Fix packaging (static/**/*, templates as package data, bgate_engine in packages) and add CI with a clean-venv wheel smoke test. Today `pip install builders-gate` yields a dashboard with no JavaScript and a scaffolder that raises FileNotFoundError, and nothing has ever verified otherwise.
5. Add POST /api/queue/{id}/reopen + PATCH + cancel, and fix POST /api/queue's 500s. A failed item is currently a dead end and a typo'd brief is unfixable from the UI — retrying failed work is the single most common motion in an agent runner and it has no button.
6. Ship git isolation: dispatch into `git worktree`/branch per item, plus GET /api/agent-diff/{item_id} off the existing bgate_run_start marker. Two personas named 'I can never see what an agent changed, and there is no way back' as their sole adoption blocker, and iterations.py already computes the diff before discarding it.
7. Add a spend ceiling and persist total_cost_usd/num_turns onto work_item, plus a wall-clock timeout and a concurrent-agent cap. Right now dispatchAll on a 20-item column is an unbounded overnight invoice and the QA gate can loop fail→reopen forever with no attempt counter.
8. Make the hook Bash-aware (or drop Bash from --allowedTools by default) and bind seat_configure to a human. The lane/lock model the README sells as 'teeth' is defeated by one `python -c` and by a seat widening its own write_globs to '**' — until this lands, every safety claim in the docs is false.
9. Fix the two most fixable lies in the frontend: wf.js's n_taskset (every workflow ships with a blank task) and the 'unassigned' seat option missing from promote (unrouted bugs are silently filed under director). Both are single-line-class bugs that quietly corrupt the tool's core output.
10. Cut the poll cost: drop assets.verify out of /api/state, replace the 500-query N+1 in artifacts.workspace() with one IN clause, and give read_activity/agent_log the byte cursor _scan_steer_echoes already uses. An idle tab currently hashes gigabytes and re-parses 10MB logs every few seconds on the same box building the game.

---

## Cross-cutting themes

_Patterns more than one persona hit independently — the real signal._

### The frontend systematically discards the backend's error messages

**Layer:** both · **Raised by:** solo-indie, gameplay-eng, studio-cto, qa-lead, tools-eng, producer

Two mutually exclusive error conventions coexist (FastAPI 4xx {detail} vs HTTP 200 {ok:false,error}), so the client gave up: index.html wraps every fetch in .catch(()=>({})) — which does not fire on a 500 because the error body is valid JSON — and dispatchItem/stopItem/addItem/reviewArtifact/promoteFeedback/dismissFeedback/mergeFeedback all `await fetch()` and never read the response. The single most common first-run failure ('claude CLI not found on PATH') is a perfect actionable string the backend produces and the UI throws in the bin. Every failure in this product renders as either nothing happening or a blank panel.

### Machinery gets built, computed, persisted — and then nothing reads it

**Layer:** both · **Raised by:** tech-art-lead, gameplay-eng, producer, qa-lead, pipeline-eng

A recurring shape: the hard half is done and the last 20 lines are missing. consistency_check writes palette-drift numbers into metadata.consistency and artifacts.workspace() surfaces them — art.js never renders them. sprites._sequence_flags computes per-animation jitter — no consumer. iterations._git_snapshot captures a full binary diff and reduces it to a hash. feedback.extract returns t_end and the INSERT drops it, so the ±4s telemetry join is anchored to the start of a 15-second complaint. assets.lock writes lease_expires_at and heartbeat refreshes it; grep finds zero comparisons against the current time. queue.add accepts source/source_ref, director.js renders the source badge, and POST /api/queue drops both fields in between.

### Gates that do not gate

**Layer:** both · **Raised by:** tech-art-lead, producer, qa-lead, studio-cto, gameplay-eng, pipeline-eng

Six independent controls are drawn as hard gates and enforce nothing. The workflow builder's control.consistency interpolates its threshold into a prose paragraph; control.gate says 'pause for human approval' with no endpoint, no approval queue, no pause. art_qa_verdict('pass') calls artifacts.review(...,'approved') directly — the router built to stop the art seat self-approving lets a different agent do exactly that. qa.js _verdictOf defaults a done item with no parseable marker to PASS. The PreToolUse hook — 'the teeth' — returns ALLOW for every tool that is not Write/Edit/MultiEdit/NotebookEdit while dispatch grants Bash. The cut line has no rank column on work_item and nothing calls scope_check. Fake gates are worse than absent ones because people build process on top of them.

### No ceiling on anything an agent can spend

**Layer:** backend · **Raised by:** tech-art-lead, gameplay-eng, studio-cto, pipeline-eng, producer

total_cost_usd is parsed off the final result event and returned in an ephemeral JSON response — never persisted, no column, no aggregate. _watch_completion's kill clock only starts after the item already reached done/failed, so a wedged agent runs forever. dispatchAll loops every queued item in a seat with no cap, no stagger, no confirm. image_sprites has no limit and no timeout while item_variants has both. The QA gate has no attempt counter, so an uncompromising reviewer ('almost is a FAIL') plus queue_reopen is an unbounded money pump on a subjective visual deliverable. Nothing in the product can answer 'worst case, what does this cost tonight'.

### Only agents can reach half the capability — the human has fewer powers than the fleet

**Layer:** both · **Raised by:** producer, tools-eng, gameplay-eng, qa-lead

queue.update and queue_reopen exist in core and are exposed as MCP tools with no HTTP route, so from the dashboard a failed item is a dead end and a typo'd brief is unfixable. bible.add/update/remove are MCP-only, so the producer authors the scope document by asking the thing it constrains to write it. lore.link/add_fact/canon.check are MCP-only. There is no place in the entire product to type a repro step. The dashboard is a viewer over a store that only agents can write.

### Poll-everything: the idle dashboard is the heaviest process on the box

**Layer:** backend · **Raised by:** solo-indie, tools-eng, gameplay-eng, producer, qa-lead

/api/state re-hashes every tracked binary (full sha256) behind a 10s cache while two independent 3s and 4s pollers hit it; artifacts.workspace() does up to 500 N+1 work_item lookups per call; read_activity read_text()s the entire (documented-as-10MB) log and json.loads every line per live agent per 3s poll, while the steer-echo scanner 100 lines away does it correctly with a byte cursor; /api/screenmap rglobs the whole project fresh per request with no cache; the playtest preflight timer opens the microphone for 1.5s and spawns a whisper probe subprocess every 15 seconds forever.

### Concurrency is unsafe at every layer

**Layer:** backend · **Raised by:** tools-eng, studio-cto, gameplay-eng, pipeline-eng, producer

SQLite is opened WAL with no busy_timeout while the HTTP threadpool, the qa_gate daemon, N _watch_completion threads, the playtest worker, and every spawned agent's own MCP server process all write the same file — and _migrate() runs unguarded in every process on first open. can_write compares lock_seat only, so two concurrently dispatched art items both pass the lock gate on the same .blend. Two agents in overlapping lanes edit the same .gd with last-write-wins. Workspace docs (storyboard, workflows) are unconditional upserts with no version/etag, so two tabs silently eat each other's afternoon.

### It has never been run on a second project, or off the author's machine

**Layer:** both · **Raised by:** solo-indie, studio-cto, pipeline-eng

assetCategory() returns 'scoville'/'tommy' from substring matches and CAT_ORDER puts them first in every project's asset view. The play panel hardcodes 'J/K punch · U/I kick' while templates/2d/scripts/player.gd reads only jump/move_left/move_right. SEAT_RULES — injected unconditionally into every dispatch — names 'tommy/scoville-bright16' and tells agents to run game/tests/fight_test.gd. bgate_engine/DESIGN.md states its schemas are derived field-by-field from one specific title's boxer.gd. And packaging ships only static/*.html with templates/ and bgate_engine/ unpackaged, with no .github/ and no CI, so a wheel install produces a JS-less dashboard and a scaffolder that raises FileNotFoundError.

### Evidence is captured beautifully and cannot leave the tool

**Layer:** both · **Raised by:** qa-lead, tech-art-lead, producer, gameplay-eng

The playtest leg assembles ~90% of a real bug report — build_ref, frame_path, exact quote, nearby non-fps telemetry — and offers no export, no copy, no markdown, no zip; there is no notes/repro column on playtest_item to hold what the mic did not catch; and the video endpoint serves a plain starlette 0.38.6 FileResponse with no Range support, so every timeline marker, moment dot and transcript line in the review overlay is a seek promise the transport cannot keep. Similarly there is no diff anywhere for what an agent changed, no cost readout, and no way to hand any of it to a person who is not sitting at the dashboard.

### Two ways to do everything, and the flagship surface picked the dumb one

**Layer:** both · **Raised by:** tech-art-lead, qa-lead, tools-eng, pipeline-eng

POST /api/artifacts/{id}/react fans a verdict three ways (disposition + durable seat note the next agent reads + live steer); the art seat's Approve/Reject buttons call /review instead, which writes a column nobody reads — 'react' appears in art.js only inside a comment. Two record buttons record different things: the overview path checks build staleness, rebuilds, and boots the telemetry frame; qa.js posts {name} and does none of it. read_activity correctly seeks past the bgate_run_start marker; agent_log 200 lines away does not, so the Raw log tab interleaves runs. MCP failures arrive in three incompatible shapes ({error}, {ok:false,error}, {available:false,reason}) with no shared predicate.

---

## To ADD — missing capability (30)

_Net-new features that do not exist today, ranked by impact-per-effort._

| # | Item | Layer | Sev | Effort | File |
|---|---|---|---|---|---|
| 1 | Git isolation per dispatch + a real diff surface + revert | backend | blocker | L | `bgate_ui/dispatch.py:249` |
| 2 | Spend ceiling and persisted per-item cost | backend | blocker | M | `bgate_ui/dispatch.py:606` |
| 3 | A first-run path: `bgate init` CLI + POST /api/project + a no-project dashboard state | both | blocker | M | `bgate_ui/static/index.html:1199` |
| 4 | HTTP routes to reopen, edit, and cancel work items | backend | blocker | S | `bgate_ui/app.py:218` |
| 5 | Bug report export from a playtest session | both | blocker | M | `bgate_ui/app.py:650` |
| 6 | A notes / repro-steps field on feedback items | both | blocker | S | `bgate_core/db.py:155` |
| 7 | Bible/scope write API and an enforceable cut line | both | blocker | L | `bgate_ui/app.py:180` |
| 8 | Real workflow run state — persist runs, drive steps, paint node status | both | blocker | L | `bgate_ui/static/wf.js:230` |
| 9 | Range-aware video serving so the review overlay can actually seek | both | blocker | S | `bgate_ui/app.py:682` |
| 10 | Human-mandatory approval, with an actor identity on every review | backend | blocker | M | `bgate_core/db.py:223` |
| 11 | Visual diff / compare mode in the art review surface | frontend | major | M | `bgate_ui/static/seats/art.js:385` |
| 12 | Batch review: multi-select, one shared reason, keyboard triage | frontend | major | M | `bgate_ui/static/seats/art.js:396` |
| 13 | Wall-clock timeout and a concurrency cap on dispatch | backend | major | M | `bgate_ui/dispatch.py:273` |
| 14 | An auth token and an Origin/CSRF guard on the mutating surface | backend | major | S | `bgate_ui/app.py:765` |
| 15 | CI plus a wheel smoke test, and tests for the process-lifecycle code | backend | major | M | `pyproject.toml:28` |
| 16 | limit/offset (or cursors) on every list endpoint | backend | major | M | `bgate_ui/app.py:319` |
| 17 | A job model for long-running engine operations | backend | major | M | `bgate_ui/routes/godot_ws.py:110` |
| 18 | `bgate doctor` — one aggregate dependency check that does not open the mic | backend | major | S | `bgate_mcp/server.py:1897` |
| 19 | Cost and latency surfaced in the art UI | both | major | S | `bgate_adapters/imagegen.py:32` |
| 20 | Surface consistency scores and sequence-jitter flags in the art review UI | frontend | major | M | `bgate_ui/static/seats/art.js:349` |
| 21 | Lore/canon HTTP endpoints and a real World view | both | major | M | `bgate_ui/static/index.html:1787` |
| 22 | Expectations and baselines on QA bots so a run can fail | both | major | M | `bgate_ui/routes/qa_bots.py:233` |
| 23 | Live mic level in playtest status | both | major | S | `bgate_ui/app.py:589` |
| 24 | Window picker for capture — stop recording the whole desktop | both | major | S | `bgate_ui/app.py:545` |
| 25 | Advisory locks on text paths, not just tracked binaries | backend | major | M | `bgate_core/seats.py:227` |
| 26 | Version pinned references instead of overwriting them | backend | major | M | `bgate_core/refs.py:47` |
| 27 | project_dir on every MCP tool; delete the mutable _ACTIVE_ROOT global | backend | major | M | `bgate_mcp/server.py:44` |
| 28 | Ship the F1 live-tuning overlay the UI advertises in four places | both | major | M | `bgate_ui/static/seats/gameplay.js:174` |
| 29 | Blocking lock acquire with a waiter list, and leases sized to the operation | both | minor | M | `bgate_core/assets.py:121` |
| 30 | Give the QA seat the evidence stream and add keyboard affordances everywhere | both | minor | M | `bgate_ui/app.py:469` |

### 1. Git isolation per dispatch + a real diff surface + revert

`bgate_ui/dispatch.py:249` · **backend** · **blocker** · effort **L** · raised by gameplay-eng, studio-cto

**Why:** Agents spawn with cwd=root, acceptEdits and Bash, straight into the live working tree. Grepping the repo for git usage finds exactly one call site (iterations.py:34). There is no branch, no worktree, no commit, no diff endpoint and no revert. read_activity's entire audit trail for an Edit is {kind:'tool', name:'Edit', hint:'<80 chars of file_path>'}. On a real project this is 20 unreviewable agent changes a day mixed with human work, git bisect dead, and no way back at 2am. Both the gameplay engineer and the CTO named this as their single adoption blocker.

**Fix:** Dispatch each item into `git worktree` at .bgate/work/item-<id>/ on branch bgate/item-<id>; at minimum capture `git rev-parse HEAD` + `git stash create` at the existing bgate_run_start marker and add GET /api/agent-diff/{item_id} returning per-file unified diffs since that boundary, rendered as a tab beside Activity/Raw log, plus POST /api/queue/{id}/revert scoped to the paths the run touched. Refuse to dispatch into a dirty tree unless explicitly opted in.

### 2. Spend ceiling and persisted per-item cost

`bgate_ui/dispatch.py:606` · **backend** · **blocker** · effort **M** · raised by studio-cto, gameplay-eng, tech-art-lead, pipeline-eng

**Why:** read_activity extracts `cost: ev.total_cost_usd` from the final result event and returns it in an ephemeral response. There is no cost column on work_item, no spend table in any of the 10 migrations, and no aggregate. Nothing caps concurrent dispatches, per-item turns, wall-clock, or total run spend. The image leg gets a per-image price table and a purchase cap; the thing that actually spends — long agent sessions at 125-205 tool uses — gets nothing. You also can never answer the only question that decides renewal: did this cost less than the engineer-hours it replaced.

**Fix:** Persist total_cost_usd and num_turns onto work_item at completion; add a spend table with per-item / per-day / per-project budgets that refuse dispatch past the ceiling; surface a running total in the dashboard header and a per-logical-asset $ total in the art iteration lab.

### 3. A first-run path: `bgate init` CLI + POST /api/project + a no-project dashboard state

`bgate_ui/static/index.html:1199` · **both** · **blocker** · effort **M** · raised by solo-indie

**Why:** Grepping all of bgate_ui for project_init/scaffold returns one hit — an error string. The rail's nine views cannot create a project or invoke godot_scaffold, and bgate_cli exposes only serve/hook-install/hook. So documented steps 1 and 2 of the loop are reachable only by registering an MCP server, starting a Claude session, and asking an agent to call a tool you cannot see — and if that session's cwd is wrong the project lands somewhere else. Everything in the shell assumes a project built by someone who already knew the tool.

**Fix:** Add `bgate init <name> [--kind 2d|3d]` doing project.init + scaffold.new_project and printing the absolute root; add POST /api/project; make /api/state return 200 {project:null, hint} so the UI can render a real first-run screen with a Create project form instead of an error state.

### 4. HTTP routes to reopen, edit, and cancel work items

`bgate_ui/app.py:218` · **backend** · **blocker** · effort **S** · raised by gameplay-eng, tools-eng, producer

**Why:** queue.update() exists in core ('how a reviewer enriches a ticket') and queue_reopen exists as an MCP tool; the HTTP surface has GET /api/queue, POST /api/queue, /wait and the three per-item actions — no GET by id, no PATCH, no DELETE, no reopen. dispatch() refuses anything not status=='queued'. So a failed item can only be stared at, a typo'd brief can only be superseded by a second item, and the human at the cockpit has strictly fewer queue powers than the agents it dispatches. Retrying failed work is the single most common thing anyone does with an agent runner.

**Fix:** Add GET /api/queue/{id}, PATCH /api/queue/{id} (queue.update), POST /api/queue/{id}/reopen (payload {reason}), and DELETE or a `cancelled` status. Map ValueError->400 and LookupError->404. Wire 'retry with note', edit and cancel affordances onto the Failed and Queued lane chips and into director/gameplay/tech seat rows.

### 5. Bug report export from a playtest session

`bgate_ui/app.py:650` · **both** · **blocker** · effort **M** · raised by qa-lead

**Why:** The playtest route surface has no export of any kind; grep for export/bug report/markdown over bgate_ui and playtest.py returns nothing, and the review overlay has no copy, share, print or download control. The tool assembles build_ref, frame_path, the exact quote and the nearby telemetry — 90% of a real ticket — and then the only egress is JSON into an overlay. Every piece of evidence dies inside a SQLite file, so QA still writes the bug by hand in a second tool and the evidence and the ticket drift apart immediately.

**Fix:** GET /api/playtest/{id}/report?format=md returning one markdown bug report per promoted item (build_ref, iteration commit, t, quote, nearby non-fps events, embedded frame, notes) plus a per-item 'copy bug report' button; a .zip with frames covers attach-to-ticket.

### 6. A notes / repro-steps field on feedback items

`bgate_core/db.py:155` · **both** · **blocker** · effort **S** · raised by qa-lead

**Why:** playtest_item is (session_id, segment_id, t, kind, text, seat, frame_path, status, promoted_ref, director_recommendation, merged_into_id) — no free-text column, and grep for 'repro' across the whole product returns a routing regex and a mission string. The transcript captures what was said, not the preconditions the player knows and the mic did not hear (which round, that they had blocked twice, that it only happens after a KO reset). That knowledge exists for ninety seconds and the tool structurally cannot hold it. A bug without repro steps is a rumour.

**Fix:** Migrate `notes TEXT NOT NULL DEFAULT ''` (and ideally repro_steps) onto playtest_item, add a textarea per feedback card that PATCHes it, and include it in playtest.brief() so the director's triage brief and any export carry it.

### 7. Bible/scope write API and an enforceable cut line

`bgate_ui/app.py:180` · **both** · **blocker** · effort **L** · raised by producer

**Why:** Every bible reference in app.py and routes/ is a read; bible.add/update/remove are MCP-only, so the producer authors the scope document by asking the fleet it is meant to constrain. Worse, the cut line is structurally unenforceable: bible.in_scope(root, rank) compares an integer that work_item does not have — queue.add stores seat/title/brief/priority/source/source_ref, no rank and no bible_section link — and the only caller of scope_check is an MCP tool an agent must voluntarily invent a rank for and voluntarily ask. The module docstring calls the cut line 'the only thing that reliably stops an agent fleet from gold-plating'; as built it stops nothing.

**Fix:** Add POST/PATCH/DELETE /api/bible/sections plus reorder-by-rank; make the World bible view an editable list with a draggable cut-line divider. Add scope_tier_id (nullable FK to bible_section) to work_item, require or infer it in queue.add, and hard-flag or refuse dispatch of items at-or-below the cut line with a red badge in director.js.

### 8. Real workflow run state — persist runs, drive steps, paint node status

`bgate_ui/static/wf.js:230` · **both** · **blocker** · effort **L** · raised by producer

**Why:** run() topo-sorts the graph, flattens every step into a numbered prose list, POSTs one director work item, dispatches it, toasts 'workflow handed to the director', and returns. No run record, no per-node status, no polling, no workflow-run endpoint anywhere in routes/. The canvas after clicking Run looks identical to before. The reason a non-engineer draws a pipeline is to watch it run; what executes is a to-do list you hope an LLM follows.

**Fix:** Persist a workflow run (run_id, wf snapshot, per-node status queued/running/passed/failed), drive steps server-side as one queue item per agent step carrying run_id in source_ref, and paint node status back onto the canvas from a /api/workflows/runs/{id} poll.

### 9. Range-aware video serving so the review overlay can actually seek

`bgate_ui/app.py:682` · **both** · **blocker** · effort **S** · raised by qa-lead

**Why:** pt_video returns starlette FileResponse(path, media_type='video/mp4'); the installed starlette is 0.38.6, whose FileResponse contains zero range handling and emits no Accept-Ranges (verified against the installed source). Meanwhile seekReview sets video.currentTime from timeline markers, tuning rows, moment dots and every transcript line. The single most important QA move — 'the KO at 7:42, watch it' — requires pulling the whole mp4 before an arbitrary seek resolves, and seeking past the buffer does nothing. Every marker in this UI is a promise the transport cannot keep.

**Fix:** Implement a range-aware handler (parse Range, return 206 with Content-Range/Accept-Ranges from a seeked slice), or pin a starlette whose FileResponse honours Range.

### 10. Human-mandatory approval, with an actor identity on every review

`bgate_core/db.py:223` · **backend** · **blocker** · effort **M** · raised by studio-cto, tech-art-lead

**Why:** There is no human identity anywhere in the data model: activity is (id, seat, kind, summary, ref, created_at) with seat '' when anonymous; work_item has no assignee or requester; asset locks are keyed to lock_seat plus lock_owner='item-<id>'. Combined with art_qa_verdict auto-approving, an AI reviewer can promote a drifted character to approved overnight and the audit trail says 'art' — which at a 30-person studio is five people plus every agent any of them launched. That is not an audit trail; it also kills the legal conversation about who authorized a generated asset.

**Fix:** Add an `actor` column (OS user / configured studio identity) to activity, work_item, artifact_revision reviews and asset locks; stamp it from the dashboard session and from the agent env at dispatch; require a non-agent actor for status='approved'; show it in the ledger.

### 11. Visual diff / compare mode in the art review surface

`bgate_ui/static/seats/art.js:385` · **frontend** · **major** · effort **M** · raised by tech-art-lead

**Why:** The compare block is one <img max-height:260px> beside 96x120px ref thumbs, and the lightbox shows exactly one image at a time with no pairing. Grep for 'diff' across art.js, flow_asset.js and artifacts.py: zero hits. No onion-skin, no swipe, no r(N) vs r(N-1) overlay, no palette delta rendering. Style drift is a 3-5% change in palette and line weight — you cannot see that in a 96px thumbnail or by alt-tabbing between lightboxes. This is the one feature that would let a tech art lead reject bad art instead of rubber-stamping it.

**Fix:** Add a lightbox compare mode: pick any two frames from the filmstrip, stack them with an opacity slider plus a mix-blend-mode:difference toggle, and show the computed palette-delta number. Even the plain CSS difference overlay is a ~40-line win.

### 12. Batch review: multi-select, one shared reason, keyboard triage

`bgate_ui/static/seats/art.js:396` · **frontend** · **major** · effort **M** · raised by tech-art-lead

**Why:** Every candidate card carries its own Approve/Reject/Regenerate/Restore row firing one POST each, and Reject/Regenerate use blocking window.prompt(). No multi-select, no shift-click range, no 'reject all failed QA', and grep of art.js for keydown returns zero — not even Escape on the lightbox. One animation run at 8 frames x 4 variants is 32 cards; triaging that through native prompt() dialogs is worse than the importer it replaces. Batch triage is the entire reason to have a review dashboard.

**Fix:** Checkbox multi-select plus a sticky action bar (Approve N / Reject N with one shared reason / Regenerate N); replace prompt() with an inline textarea; bind j/k to move the filmstrip, a/r to approve/reject, Escape to close the lightbox.

### 13. Wall-clock timeout and a concurrency cap on dispatch

`bgate_ui/dispatch.py:273` · **backend** · **major** · effort **M** · raised by gameplay-eng, producer, studio-cto

**Why:** _watch_completion's exit_grace_s=90 kill clock only starts AFTER the item reaches done/failed and stdin closes; if an agent never calls queue_complete the loop polls forever. There is no max runtime, no turn budget, and no server-side cap on concurrent agents — dispatch() only refuses a second agent for the same item_id. dispatchAll(seat) loops every queued item with no stagger and no confirmation, so one click on a 20-item column launches 20 claude processes each spawning its own MCP children, on the same box running the Godot editor. The module's own docstring records 14 orphaned agents as an observed failure.

**Fix:** Take max_runtime_s and max_cost_usd per dispatch with a server default, enforce both in _watch_completion (_kill_tree + set_status('failed','exceeded N-minute budget') on trip); add a server-side max-concurrent-agents that queues the overflow and returns {ok:false,error:'concurrency limit N reached'}; make dispatchAll confirm above a threshold and add a 'stop all'.

### 14. An auth token and an Origin/CSRF guard on the mutating surface

`bgate_ui/app.py:765` · **backend** · **major** · effort **S** · raised by tools-eng, studio-cto

**Why:** There is no CORSMiddleware, no auth, no Origin check and no CSRF token anywhere in bgate_ui — the only middleware is _coi_headers. Meanwhile POST /api/godot/run executes a caller-supplied GDScript string, POST /api/playtest/start accepts game_cmd and launch_native, and POST /api/queue/{id}/dispatch spawns a Claude session with acceptEdits and Bash against the repo. Browsers permit cross-origin POSTs without preflight for simple content types, so any page open in another tab reaches port 7788. 'It's bound to 127.0.0.1' is a defensible posture for a store viewer, not for an arbitrary-code-execution endpoint.

**Fix:** Middleware rejecting state-changing requests whose Origin/Sec-Fetch-Site is not same-origin, plus a per-run bearer token printed by `bgate serve` and stored in .bgate/ that the page embeds and every mutating request must send; gate dispatch behind an explicit config flag so a viewer-only deployment cannot spawn agents.

### 15. CI plus a wheel smoke test, and tests for the process-lifecycle code

`pyproject.toml:28` · **backend** · **major** · effort **M** · raised by studio-cto, pipeline-eng

**Why:** There is no .github/ directory, so nothing runs the 22 test files. And the tested half is the pure deterministic half: grepping tests/ for dispatch/qa_gate/routes matches only incidental strings — there is no test for dispatch.py (620 lines of process lifecycle, kill trees, pid ledgers, stdin steering), none for qa_gate.py, none for any of the 8 routers, and test_ui.py is 103 lines. The untested half spawns processes, kills trees by pid, and auto-creates billable work — and the MEMORY note records the QA gate being silently dead for weeks on a SQL binding bug, which is exactly what an untested daemon thread with a fail-safe `except` produces.

**Fix:** GitHub Actions running `pytest -m 'not slow'` plus a job that builds the wheel, installs into a clean venv, and asserts godot_templates reports available and the dashboard's JS files exist. Add tests for dispatch lifecycle against a fake CLI binary (dispatch -> steer -> complete -> reap -> orphan sweep), qa_gate scan against a seeded DB, and a smoke test hitting every registered route for a non-500.

### 16. limit/offset (or cursors) on every list endpoint

`bgate_ui/app.py:319` · **backend** · **major** · effort **M** · raised by tools-eng, tech-art-lead, producer

**Why:** /api/artifacts accepts status and logical_name but not limit, silently taking list_revisions' default of 100 while artifacts.workspace() uses 500 — so the iteration lab's filmstrip and the flow map disagree once a project passes 100 revisions, with no total and no 'showing N of M'. /api/queue has no limit and queue.list_items has no LIMIT clause, returning every work item ever created including full briefs on a 3s poll. /api/state returns the full asset list plus 100 artifacts plus 12 iterations in one blob; /api/refs and /api/audio/list are unbounded walks. Only /api/activity and /api/iterations expose limit, neither an offset.

**Fix:** Add capped limit + offset/cursor to every list endpoint and return {items, total, next_cursor} so the UI can render a count and a 'load more' instead of silently truncating.

### 17. A job model for long-running engine operations

`bgate_ui/routes/godot_ws.py:110` · **backend** · **major** · effort **M** · raised by tools-eng

**Why:** godot_run passes timeout=int(payload.get('timeout',120)) straight through with no upper bound, so a client can request 100000; godot_inspect, godot_check, godot_screenshot and /api/play/rebuild are the same shape. All are synchronous, holding a threadpool thread until the engine returns, with no job id, no progress and no cancel. A full import check on a real project is minutes of dead fetch and a spinner; close the tab and the work keeps running with nothing watching it. Meanwhile any client can wedge a worker by passing a huge timeout.

**Fix:** Clamp with max(5, min(int(...), 600)); then convert to POST -> 202 {job_id} with GET /api/jobs/{id} returning {state, progress, result}. The playtest processing worker is already the pattern to reuse.

### 18. `bgate doctor` — one aggregate dependency check that does not open the mic

`bgate_mcp/server.py:1897` · **backend** · **major** · effort **S** · raised by pipeline-eng, solo-indie

**Why:** External-binary health is spread across blender_status, godot_status, image_status and playtest_check with four differently-shaped failures; grep for doctor/health returns nothing but playtest.preflight. playtest_check is the only path to ffmpeg/ffprobe/whisper status and it runs recorder.probe_mic, which records 1.5s of live audio and walks every input device. So on a CI box with no audio device the ffmpeg answer is buried behind an unrelated mic failure, and the first question on any new machine ('what's missing?') costs four calls.

**Fix:** Add a bgate_doctor tool and `bgate doctor` CLI returning one dict: {blender, godot, ffmpeg, ffprobe, whisper, openai_key, python} each with {available, path, version, min_required, reason}. Split preflight so the ffmpeg/whisper checks do not require the mic probe.

### 19. Cost and latency surfaced in the art UI

`bgate_adapters/imagegen.py:32` · **both** · **major** · effort **S** · raised by tech-art-lead

**Why:** IMAGE_PRICE_USD and price_per_image() exist and items.py returns estimated_cost_usd in item_generate/item_variants responses, but grepping bgate_ui/static for cost/usd returns zero hits in art.js, flow_asset.js and wf_steps_asset.js. wf_steps_asset shows 'Total candidates: N images' as a count when frames x variants is exactly a spend estimate — 8 frames x 4 variants at 'high' is $5.34 per button click warned only as '32 images'. imagegen._save() also returns no elapsed time, unlike sprites.render_sprites which returns 'seconds', so there is no way to tell a slow run from a wedged one.

**Fix:** Return elapsed seconds from imagegen.generate/edit, persist seconds and estimated_usd into artifact metadata at register time, show a running $ total per logical asset in the iteration lab header, and put a live '~$X.XX' next to the candidate count in the art.animation config.

### 20. Surface consistency scores and sequence-jitter flags in the art review UI

`bgate_ui/static/seats/art.js:349` · **frontend** · **major** · effort **M** · raised by tech-art-lead

**Why:** artifacts.workspace() explicitly attaches revision['consistency'] = metadata.get('consistency', {}) and the reviewer brief instructs the agent to run consistency_check for 'a palette-drift number plus the profile checklist' — grep art.js for 'consistency' finds nothing outside CSS. Separately sprites._sequence_flags computes per-animation max adjacent height jump and returns it in from_pose_images, with a docstring saying a human/vision pass should look; no frontend consumer exists. So the one drift class a still-frame review is blind to (height popping between adjacent frames) stays invisible, and the reviewer is left with a single agent-authored 0-100 score they cannot calibrate.

**Fix:** Render metadata.consistency on each candidate card beside the QA badge (palette distance, Unicom-vs-ref, the composite image the tool already builds), coloured against the documented <0.55 threshold; persist the sequence block into artifact metadata and badge affected assets ('jitter: jab ±22%'); add a looping SpriteFrames preview at the .tres fps.

### 21. Lore/canon HTTP endpoints and a real World view

`bgate_ui/static/index.html:1787` · **both** · **major** · effort **M** · raised by producer

**Why:** renderWorld() emits one <span class='entity'> per canon entity with the summary as a title tooltip — that is the entire lore surface. lore.links_of/facts_of/brief/link/add_fact and canon.check exist in core and are reachable only as MCP tools; grepping app.py and routes/ finds none of them. Draft entities are fetched into /api/state and never rendered. canon.py is arguably the smartest module in the repo (it catches a retired character walking back on stage, and 'the siege lasted three years' against a locked seven) and a human cannot run it, see the graph, lock a fact, or see what is still draft.

**Fix:** Add /api/lore (list, entity brief, link, add_fact, status change) and /api/canon/check; give the World bible view an entity list with facts and a link graph — nodecanvas.js already renders exactly this shape.

### 22. Expectations and baselines on QA bots so a run can fail

`bgate_ui/routes/qa_bots.py:233` · **both** · **major** · effort **M** · raised by qa-lead

**Why:** qa_bots_run returns {ok, summary:{samples, final, notes}, stdout, stderr, seconds} where `ok` means only 'the probe ran and found two fighters'. The stored bot shape is {name, ticks, actions} — no expected values, no tolerance, no saved prior run — and the UI prints a table of positions/hp/stamina plus raw stdout and stops. Deterministic input replay against the real engine is a proper regression harness; without assertions and a baseline every run is a human squinting at numbers, which nobody does twice. It cannot gate a merge, cannot run in a loop, and cannot say that yesterday's damage tuning broke the rushdown bot.

**Fix:** Add optional `expect` entries per bot (property, comparator, value, at_tick) evaluated server-side into pass/fail with the offending sample; persist the last run per bot as a baseline with a diff view; expose a 'run all bots' endpoint returning an aggregate verdict the QA gate can consume.

### 23. Live mic level in playtest status

`bgate_ui/app.py:589` · **both** · **major** · effort **S** · raised by qa-lead

**Why:** pt_status returns {id, name, telemetry_events, native} only. The Recording dataclass accumulates audio callback status strings in rec._err and frames in rec._frames, but neither is queried while recording — they surface only in recorder.stop()'s warnings. The overview lamp and the QA panel are driven purely by event count. probe_mic deliberately passes a silent device on the theory that gaming headsets are noise-gated, which means the only thing between the operator and a 25-minute silent recording is the transcript, discovered afterwards. A full playtest lost to a muted headset is a real and unrecoverable cost.

**Fix:** Expose rolling audio peak/rms and captured seconds from the live Recording in /api/playtest/status, drive a level meter next to the record button, and warn in-UI after N seconds of digital silence.

### 24. Window picker for capture — stop recording the whole desktop

`bgate_ui/app.py:545` · **both** · **major** · effort **S** · raised by qa-lead

**Why:** playtest.preflight() supports a window_title check that calls recorder.list_windows and fails with 'no visible window matching ... — start the game first', but pt_preflight only forwards `native` and never window_title; neither toggleRecord nor qa.js sends one, so recorder.start falls to target='desktop'. Every recording therefore captures the dashboard, Slack and the second monitor instead of the game — unusable as evidence to hand to an external partner, triple the file size the Range-less video endpoint must ship, and the one preflight check that would catch 'you forgot to launch the game' is written and never called.

**Fix:** Add a window picker backed by recorder.list_windows (the playtest_devices MCP tool already does this), pass window_title to both preflight and start, and default the native path to the launched Godot window title.

### 25. Advisory locks on text paths, not just tracked binaries

`bgate_core/seats.py:227` · **backend** · **major** · effort **M** · raised by gameplay-eng

**Why:** gameplay's write_globs are ['game/scripts/**','game/scenes/**']; tech's are ['game/**','scripts/**','*.cfg','*.godot'] — a strict superset. can_write's lock gate only fires when assets.get returns a registered entry with lock_seat set, i.e. tracked binaries; a plain .gd is never locked. dispatch() only rejects a second agent on the same item_id, so a gameplay item and a tech item run concurrently by design and both agents can open game/scripts/player.gd. Last write wins silently mid-run; in a normal multi-dev setup git would at least report a conflict.

**Fix:** Have the hook take an advisory lock on any path a seat writes for the duration of the run (owner is already BGATE_LOCK_OWNER=item-<id>) and block a second agent's write with the owning item id in the message; failing that, detect overlapping-path runs after the fact and flag them in the UI.

### 26. Version pinned references instead of overwriting them

`bgate_core/refs.py:47` · **backend** · **major** · effort **M** · raised by tech-art-lead

**Why:** pin() does shutil.copy2 to .bgate/refs/<slug><suffix> and UPSERTs — re-pinning replaces the file. Artifacts store resolved_refs as absolute paths to that same file and art.js renders whatever is there today. There is no pin history and no hash recorded, so nothing detects the swap. docs/character-consistency.md calls the pinned reference canon and re-pinning a director-level act; after one re-pin every archived candidate displays beside a reference it was never generated against, and the audit trail for 'why did we approve this' becomes fiction.

**Fix:** Version pins like artifacts (<slug>.rN<suffix>, row points at newest), record the pin's sha256 in each artifact's resolved_refs entry, and warn on cards whose ref hash no longer matches.

### 27. project_dir on every MCP tool; delete the mutable _ACTIVE_ROOT global

`bgate_mcp/server.py:44` · **backend** · **major** · effort **M** · raised by studio-cto

**Why:** _ACTIVE_ROOT is a module-level global that project_select mutates; _root() reads it or BGATE_ROOT or walks up from cwd. Core tools take no project argument — bible_add, seat_brief, queue_add all resolve implicitly — while only the Godot adapters accept project_dir. With process-global active-root, two sessions sharing an MCP server silently write each other's bible, and docs/gap-analysis.md already documents an instance of this class ('an agent's blackboard note silently lost to a cwd mismatch') and ranks project_dir as lever #2 at hours of effort. It also forces one MCP registration per game.

**Fix:** Add optional project_dir to every tool (falling back to BGATE_ROOT / cwd walk-up) and remove the mutable global so concurrency does not depend on call ordering.

### 28. Ship the F1 live-tuning overlay the UI advertises in four places

`bgate_ui/static/seats/gameplay.js:174` · **both** · **major** · effort **M** · raised by studio-cto

**Why:** Four UI strings promise it — gameplay.js:174 'Live build — F1 opens the in-game live tuning panel', flow_game.js:105 and :232, flows.js:195 'boot current build · F1 tuning'. templates/ contains 11 files and grepping them for tuning/F1 returns zero; the only overlaid autoload is bgate_telemetry.gd. iterations.py:94 reads .bgate/tunables.json if it happens to exist but nothing in the scaffold writes one. The gap analysis calls the tuning panel the single biggest multiplier (feel-loop 60 min -> 1 min). Promising it in the chrome before it exists is how the tool loses the room in the first hour.

**Fix:** Ship the overlay in templates/shared/addons/bgate/ (F1 toggles sliders over every exported tunable, writes .bgate/tunables.json, game boots with it), or delete the F1 copy from all four strings until it lands.

### 29. Blocking lock acquire with a waiter list, and leases sized to the operation

`bgate_core/assets.py:121` · **both** · **minor** · effort **M** · raised by studio-cto, tech-art-lead

**Why:** lock() takes lease_s=300 and a held lock raises ('binary assets don't merge; wait for release or re-plan') with no wait, no waiter queue, and no API showing who is blocked on what. With one person driving that is fine; with five art people plus their agents contending for the same character .blend, every contention costs a full agent restart and a lost context window and nobody can see the line. A 5-minute default lease against a 30-minute image_sprites batch also means leases expire mid-work.

**Fix:** Add an optional blocking acquire with timeout plus a waiters list surfaced in the seat view, and derive the default lease from the operation's expected duration rather than a flat 300s.

### 30. Give the QA seat the evidence stream and add keyboard affordances everywhere

`bgate_ui/app.py:469` · **both** · **minor** · effort **M** · raised by qa-lead, solo-indie

**Why:** _queue_playtest_triage always adds the triage item to 'director'. The QA workspace shows a bot roster, gate verdicts, a record button and an agent log — no session list, no feedback triage, no view of promoted items — while feedback.route() can route to 'qa' and nothing acts on it. In every studio the person who watches the recording back and writes the repro is QA, not the creative director. Separately there is a dead arrow-key handler bound to .workspace-tabs (zero matching markup, so the ?. no-ops forever) and grep of static/ for 'Escape' returns nothing: the lightbox, Inspector drawer, asset drawer and full-screen review overlay all close only by mouse.

**Fix:** Surface the session list and untriaged-feedback queue inside the QA workspace and route the triage item to qa (director reviews the filed items), or add a parallel qa 'confirm and write repro' item per promoted bug. Delete the dead listener; add a global keydown closing whichever overlay is open on Escape and bind 1-9 to the rail views.

---

## To ADJUST — existing things to change (77)

_Things that exist but are wrong, incomplete, or unusable, ranked by impact-per-effort._

| # | Item | Layer | Sev | Effort | File |
|---|---|---|---|---|---|
| 1 | Two error conventions coexist, so the UI gave up and shows blank panels | backend | blocker | M | `bgate_ui/app.py:551` |
| 2 | Every mutation in the shell is fire-and-forget — the perfect error string is discarded | frontend | blocker | S | `bgate_ui/static/index.html:2122` |
| 3 | art_qa_verdict('pass') calls artifacts.review(...,'approved') — an LLM approves art with no human | backend | blocker | S | `bgate_mcp/server.py:1767` |
| 4 | Approve does not make the revision live — the game keeps loading the rejected sheet | both | blocker | S | `bgate_ui/static/seats/art.js:397` |
| 5 | The PreToolUse hook only guards 4 tools while dispatch grants Bash | backend | blocker | M | `bgate_cli/hook.py:21` |
| 6 | seat_configure lets a seat rewrite its own write lanes | backend | blocker | S | `bgate_mcp/server.py:2055` |
| 7 | Every one of ~90 MCP tools is a blocking sync def on FastMCP's event loop | backend | blocker | M | `bgate_mcp/server.py:38` |
| 8 | SQLite has no busy_timeout while 8+ processes write it, and _migrate races | backend | blocker | S | `bgate_core/db.py:428` |
| 9 | The workflow task box calls a function that never exists | frontend | blocker | S | `bgate_ui/static/wf.js:271` |
| 10 | Workflow gate / select / consistency nodes are drawn as gates and enforce nothing | both | blocker | M | `bgate_ui/static/wf_steps_asset.js:200` |
| 11 | A packaged wheel has no JavaScript and no scaffold templates | backend | blocker | S | `pyproject.toml:28` |
| 12 | `python -m bgate_ui` prints nothing — no URL, no port, no confirmation | backend | major | S | `bgate_ui/app.py:761` |
| 13 | A fresh machine sees 'Offline · server 503' because the helpful branch is dead code | both | major | S | `bgate_ui/static/index.html:2055` |
| 14 | POST /api/queue 500s on a missing field or unknown seat, and drops source/source_ref | backend | major | S | `bgate_ui/app.py:229` |
| 15 | status() marks an agent that exited 0 without reporting as DONE | backend | major | S | `bgate_ui/dispatch.py:507` |
| 16 | Items stick in 'dispatched' forever after a dashboard restart | backend | major | S | `bgate_ui/dispatch.py:359` |
| 17 | GET /api/agents mutates the database — reconciliation lives in a read handler | backend | major | M | `bgate_ui/dispatch.py:494` |
| 18 | stop() terminates only the parent and records the stop as a mystery crash | backend | major | S | `bgate_ui/dispatch.py:613` |
| 19 | Activity feed is hard-capped at 40 steps and re-parses the whole log every 3 seconds | both | major | M | `bgate_ui/dispatch.py:557` |
| 20 | /api/state re-hashes every tracked binary on a poll loop, with an N+1 alongside it | backend | major | S | `bgate_ui/app.py:181` |
| 21 | queue_wait pins a threadpool worker for up to 30 minutes and polls the DB | backend | major | S | `bgate_ui/app.py:243` |
| 22 | refs_upload writes a temp file using the unsanitized user-supplied name | backend | major | S | `bgate_ui/routes/refs.py:77` |
| 23 | Asset leases are written and heartbeat but never once compared to the clock | backend | major | S | `bgate_core/assets.py:136` |
| 24 | can_write compares lock_seat only, so two same-seat agents both pass the lock gate | backend | major | S | `bgate_core/seats.py:252` |
| 25 | The hook fails open silently and cannot be proven live | backend | major | S | `bgate_cli/hook.py:85` |
| 26 | hook-install bakes an absolute interpreter path into a committed settings.json | backend | major | S | `bgate_cli/main.py:16` |
| 27 | The QA gate can loop fail -> reopen -> re-dispatch forever | backend | major | S | `bgate_ui/qa_gate.py:34` |
| 28 | QA gate treats a done item with no VERDICT marker as PASS | frontend | major | S | `bgate_ui/static/seats/qa.js:479` |
| 29 | Feedback items drop t_end, so the telemetry join misses the event being complained about | backend | major | S | `bgate_core/playtest.py:356` |
| 30 | Opening a session review shells out to ffmpeg synchronously inside the GET | backend | major | M | `bgate_core/playtest.py:581` |
| 31 | pt_video 500s on a bad id and reports a security error for a session with no video | backend | major | S | `bgate_ui/app.py:685` |
| 32 | A server restart mid-session orphans ffmpeg and leaves an unplayable mp4 | backend | major | M | `bgate_ui/app.py:33` |
| 33 | Promote's seat dropdown omits 'unassigned', silently filing unrouted bugs under director | frontend | major | S | `bgate_ui/static/index.html:1962` |
| 34 | Merged feedback items look identical to dismissed ones, and merging is irreversible | both | major | S | `bgate_ui/static/index.html:1961` |
| 35 | The art seat's Approve/Reject call the dumb endpoint, so rejection teaches nothing | both | major | S | `bgate_ui/static/seats/art.js:438` |
| 36 | artifact_react always returns ok:true with a grab-bag of optional error keys | backend | major | S | `bgate_ui/app.py:351` |
| 37 | Finished agents appear for exactly one poll, then vanish with their result | backend | major | S | `bgate_ui/dispatch.py:494` |
| 38 | Director board renders every item ever created, briefs and all, every 3 seconds | both | major | S | `bgate_ui/routes/orchestrator.py:100` |
| 39 | Delegation loses the parent-child link the moment the page reloads | both | major | S | `bgate_ui/routes/orchestrator.py:74` |
| 40 | Two fully-built Studio flows are unreachable — the flow whitelist is hardcoded | frontend | major | S | `bgate_ui/static/flows.js:19` |
| 41 | Asset locks and drift never appear in the art seat | frontend | major | M | `bgate_ui/static/seats/art.js:186` |
| 42 | Narrative storyboard is an island: no canon check, no lore link, no path to work | frontend | major | M | `bgate_ui/static/seats/narrative.js:130` |
| 43 | Workspace docs are last-write-wins with no version check, and narrative refresh is a deliberate no-op | both | major | S | `bgate_core/workspace.py:30` |
| 44 | MCP failures arrive in three incompatible shapes with no shared predicate | backend | major | M | `bgate_mcp/server.py:60` |
| 45 | image_sprites has no spend cap or timeout while item_variants has both | backend | major | S | `bgate_mcp/server.py:1035` |
| 46 | An agent-facing error names a parameter no MCP tool exposes | backend | major | S | `bgate_adapters/imagegen.py:85` |
| 47 | godot._errors substring-greps 'invalid' and 'error:', failing healthy builds | backend | major | S | `bgate_adapters/godot.py:415` |
| 48 | Blender discovery sorts install dirs lexicographically and enforces no version floor | backend | major | S | `bgate_adapters/blender.py:94` |
| 49 | .env is not in .gitignore, and the tooling claims it is | backend | major | S | `.gitignore` |
| 50 | The .env cache never invalidates, so fixing a missing key needs a server restart | backend | major | S | `bgate_core/envfile.py:13` |
| 51 | SEAT_RULES hardcodes another game's assets into every dispatch, and seats can't override them | backend | major | M | `bgate_ui/dispatch.py:50` |
| 52 | Asset library categories and the play-panel controls hint are hardcoded to one game | frontend | major | S | `bgate_ui/static/index.html:1576` |
| 53 | Record button is permanently dead on a stock install with a truncated, unactionable reason | both | major | S | `bgate_ui/static/index.html:2593` |
| 54 | A route module that fails to import disappears silently | backend | major | S | `bgate_ui/routes/__init__.py:25` |
| 55 | README describes a dashboard that no longer exists | frontend | major | S | `README.md:89` |
| 56 | Responsive CSS targets deleted markup, so the game renders in a 390px letterbox | frontend | major | S | `bgate_ui/static/index.html:995` |
| 57 | Iteration timeline reports hashes and raw counts, never whether the game got better | both | major | M | `bgate_core/iterations.py:231` |
| 58 | bgate_engine proposes a second authoritative simulation derived from one title | backend | major | L | `bgate_engine/DESIGN.md:1` |
| 59 | Agent activity leaks the seat-identity system prompt and truncates results to 160 chars | frontend | minor | S | `bgate_ui/dispatch.py:594` |
| 60 | Raw log tab interleaves runs with no separator, while the parser 200 lines away handles it | both | minor | S | `bgate_ui/app.py:303` |
| 61 | Dispatching a nonexistent item id returns a 500 stack instead of a clean error | backend | minor | S | `bgate_ui/dispatch.py:202` |
| 62 | reap_orphans kills by name prefix from a best-effort pid file | backend | minor | S | `bgate_ui/dispatch.py:392` |
| 63 | Gameplay seat leaves Stop and Steer enabled with no live agent | frontend | minor | S | `bgate_ui/static/seats/gameplay.js:184` |
| 64 | Reference thumbnails hardcode .png, so jpg/webp pins render blank | frontend | minor | S | `bgate_ui/static/wf_steps_asset.js:19` |
| 65 | The workflow reference node has no picker — its config panel is one sentence | frontend | minor | S | `bgate_ui/static/wf.js:272` |
| 66 | Saved-workflow delete has no confirmation and orphans the stored document | both | minor | S | `bgate_ui/static/wf.js:115` |
| 67 | QA panel's playtest widget reads a field the API does not send | frontend | minor | S | `bgate_ui/static/seats/qa.js:412` |
| 68 | The QA panel's record button bypasses preflight, staleness check and frame boot | frontend | minor | S | `bgate_ui/static/seats/qa.js:433` |
| 69 | Seeking ignores video_offset_s, so the human sees uncorrected time and the agent sees corrected | frontend | minor | S | `bgate_ui/static/index.html:1999` |
| 70 | Preflight polls every 15s, opening the mic and spawning a whisper probe each time | both | minor | S | `bgate_ui/static/index.html:2702` |
| 71 | Atlas's dead/missing-asset badge only appears after you have already opened Atlas, and rescans everything per request | both | minor | S | `bgate_ui/static/atlas.js:79` |
| 72 | Temp directories leak on every Godot/sprite call and on every Blender failure path | backend | minor | S | `bgate_adapters/godot.py:135` |
| 73 | Adapters write to fixed output paths that concurrent seats clobber | backend | minor | S | `bgate_mcp/server.py:1461` |
| 74 | seat_brief returns an uncapped blob and every seat is told to call it first | backend | minor | S | `bgate_core/seats.py:282` |
| 75 | _model_for's docstring tells the reader to use a model the file bans | backend | minor | S | `bgate_adapters/imagegen.py:44` |
| 76 | Per-pixel Python loops run inline on the same event loop everything else is blocked on | backend | nice-to-have | S | `bgate_mcp/server.py:1016` |
| 77 | Four stacked theme layers in one 2762-line file, and the declared UI font never loads | frontend | nice-to-have | M | `bgate_ui/static/index.html:972` |

### 1. Two error conventions coexist, so the UI gave up and shows blank panels

`bgate_ui/app.py:551` · **backend** · **blocker** · effort **M** · raised by tools-eng, studio-cto, solo-indie, producer

**Why:** pt_start catches everything and returns HTTP 200 {ok:false,error:'TypeError: ...'}; pt_stop and queue_wait do the same; pt_retry raises 409, pt_merge and artifact_review raise 400 producing {detail}; orchestrator.delegate returns 200 {ok:false}. Nothing declares which shape an endpoint uses, so you cannot write one handleResponse() — which is exactly why index.html wraps every call in .catch(()=>({})), a catch that never fires on a 500 because FastAPI's error body is valid JSON. Every backend failure in this product renders as an empty dashboard indistinguishable from an empty project.

**Fix:** One envelope — {ok, data, error:{code, message, detail?}} — enforced by paired @app.exception_handler(Exception) and @app.exception_handler(HTTPException) so unhandled exceptions are wrapped too, with a stable machine-readable code (no_active_session, already_processing, claude_cli_missing) the UI can branch on.

### 2. Every mutation in the shell is fire-and-forget — the perfect error string is discarded

`bgate_ui/static/index.html:2122` · **frontend** · **blocker** · effort **S** · raised by solo-indie, gameplay-eng, studio-cto, qa-lead

**Why:** dispatchItem, stopItem, addItem, reviewArtifact, regenerateArtifact, retrySession, promoteFeedback, dismissFeedback, mergeFeedback and linkFeedbackAsset all `await fetch()` then re-poll, never checking r.ok or the body — while a toast helper (BGWS.toast in seats/_core.js:47) already exists and director/gameplay/tech/audio use it. The backend returns {ok:false,error:'claude CLI not found on PATH'}, 'item N is dispatched, not queued', 'item N already has a live agent'. So the literal first click of an evaluation on a machine without the CLI does nothing forever with no toast, no row change, no console hint. On the QA side a triage pass is 30 clicks; three 400s in the middle go unnoticed and a softlock is never filed.

**Fix:** A shared mutate(path, body) that checks r.ok, reads detail/error, calls BGWS.toast, disables the control while in flight, and skips the re-render on failure so the operator's selection survives. Replace the blanket .catch(()=>({})) with a distinct 'backend error' state so empty != broken.

### 3. art_qa_verdict('pass') calls artifacts.review(...,'approved') — an LLM approves art with no human

`bgate_mcp/server.py:1767` · **backend** · **blocker** · effort **S** · raised by tech-art-lead

**Why:** Verified: the tool maps pass->'approved' and calls _artifacts.review directly, which flips prior approved revisions to 'superseded' and stamps the new one. There is no gate and no config flag (no BGATE_ART_GATE / require_human anywhere). The entire premise of the art_qa router — 'agents judging a frame in isolation have approved off-style drift three times' — is undermined by letting a different agent do exactly that. A QA agent can promote a drifted fighter to canon overnight and the only trace is an activity line. Compounded by art_qa_review having no idempotency check, so a double-click races two reviewers and an impatient second 'pass' can approve art the first had failed.

**Fix:** Split verdict from disposition: write metadata.qa_review and set status to 'qa_passed' (or leave 'candidate' with a pass badge), never 'approved'. Reserve 'approved' for POST /api/artifacts/{id}/review originating from a UI session with an actor. Add BGATE_ART_AUTOAPPROVE=1 defaulted off. Before dispatching a review, return an in-flight qa item with {already_running:true} instead of spawning a second.

### 4. Approve does not make the revision live — the game keeps loading the rejected sheet

`bgate_ui/static/seats/art.js:397` · **both** · **blocker** · effort **S** · raised by tech-art-lead

**Why:** Approve posts {status:'approved'} to /api/artifacts/{id}/review, which only mutates the DB row. The file the game loads is the stable sheet path, and every generation overwrites <name>_sheet.png; copying an older revision back is a separate action (/api/artifacts/{id}/restore). Only Restore is captioned 'Make this revision the live sheet the game uses' — Approve's tooltip says nothing. So you approve r3 because r5 went painterly, the toast says approved, and r5 ships. Approval that does not change what ships is worse than no approval: it manufactures false confidence, and it is the exact silent-wrong-asset bug that costs a day of bisecting in-engine.

**Fix:** Make review(status='approved') on an image artifact also restore its archived preview over the live path, or refuse with an actionable error when the live file's hash != the revision's. At minimum badge each card LIVE when hashes match and have Approve on a non-live revision prompt 'also make this the live sheet?'.

### 5. The PreToolUse hook only guards 4 tools while dispatch grants Bash

`bgate_cli/hook.py:21` · **backend** · **blocker** · effort **M** · raised by gameplay-eng, pipeline-eng, studio-cto

**Why:** Verified: _PATH_KEYS is exactly Write/Edit/MultiEdit/NotebookEdit and decide() returns ALLOW immediately for anything else, while dispatch.py:233 passes --allowedTools mcp__builders-gate Read Edit Write Glob Grep Bash with acceptEdits. So `python -c "open('project.godot','w')..."`, `cp`, `mv`, `git checkout .` or `rm -rf` from any seat sails past every lane and lock check — and the dispatch prompt still tells the agent 'the PreToolUse hook enforces them', so it will not self-police. The README sells lanes and locks as teeth; they are a speed bump on four tool names. Asset pipelines are full of copy/move/rename, which is precisely where agents reach for Bash.

**Fix:** Add Bash to the matcher and parse the command for write-ish targets (redirects, cp/mv/rm/git checkout), at minimum blocking any command line containing a path locked by another seat; or drop Bash from --allowedTools by default behind a per-dispatch opt-in. Stop claiming full enforcement in _prompt_for when Bash is granted.

### 6. seat_configure lets a seat rewrite its own write lanes

`bgate_mcp/server.py:2055` · **backend** · **blocker** · effort **S** · raised by pipeline-eng

**Why:** Verified: seat_configure(role, enabled, write_globs, mission) is an unguarded MCP tool and seats.configure validates only that the role exists before persisting arbitrary globs. Compare _lock_identity (server.py:70), which does bind asset_lock/asset_release to the session's BGATE_SEAT — nothing equivalent guards this. `seat_configure(role='art', write_globs=['**'])` permanently grants art write access to the whole repo from inside a sandboxed session, and it is a call an agent could plausibly make in good faith after being blocked ('my lane must be misconfigured'). The only trace is a row nobody reads.

**Fix:** Apply the same _lock_identity binding: refuse seat_configure when BGATE_SEAT is set (it is a human/coordinator operation), or at minimum refuse when the caller's adopted seat equals the role being widened; log every write_globs change to the activity ledger.

### 7. Every one of ~90 MCP tools is a blocking sync def on FastMCP's event loop

`bgate_mcp/server.py:38` · **backend** · **blocker** · effort **M** · raised by pipeline-eng

**Why:** Verified by count: 90 module-level tool defs, zero async. The MCP SDK does `await fn(...) if fn_is_async else fn(...)`, so a sync tool executes inline on the asyncio loop. blender_run (420s), blender_sprites (420s), godot_import_asset (240s), playtest_stop (1800s whisper ceiling) and image_sprites (unbounded: 1 ref + N poses x 300s HTTP each + retries) each hold the whole server hostage. transcribe.py's docstring states the exact principle being violated — 'never import faster_whisper into the server process... inline in FastMCP's async loop that stalls every other tool call'. The whisper model was correctly subprocessed and then everything else was left to block anyway.

**Fix:** Make every tool `async def` with the blocking body in `await anyio.to_thread.run_sync(...)`, at minimum the adapter-calling ones (blender_*, godot_*, image_*, item_*, playtest_*), plus a per-external-binary concurrency limiter so two seats do not fight over one GPU.

### 8. SQLite has no busy_timeout while 8+ processes write it, and _migrate races

`bgate_core/db.py:428` · **backend** · **blocker** · effort **S** · raised by tools-eng, studio-cto

**Why:** Verified: connect() sets journal_mode=WAL, foreign_keys=ON, synchronous=NORMAL and never busy_timeout — the default is 0, so any write contention raises 'database is locked' immediately rather than waiting. Writers are genuinely concurrent: the HTTP threadpool, the _finish_playtest worker, N _watch_completion threads, the qa_gate daemon, and every spawned agent's own MCP server process. WAL buys concurrent readers, not writers. The failure mode is an agent finishing its work and then failing to record it — silent lost work that gets blamed on the AI. _migrate() also runs unguarded in every process on first open, so two fresh processes can race the same script.

**Fix:** conn.execute('PRAGMA busy_timeout = 5000') in connect(); wrap writes in retry-with-jitter; map OperationalError('database is locked') in db.tx to a retryable 503 with Retry-After; guard _migrate() with an exclusive transaction.

### 9. The workflow task box calls a function that never exists

`bgate_ui/static/wf.js:271` · **frontend** · **blocker** · effort **S** · raised by producer

**Why:** Verified: input.task's config() returns a textarea with oninput="n_taskset(this.value)" plus a <script> block defining window.n_taskset, assigned via insp.innerHTML in _inspect(). Script tags injected through innerHTML never execute, so n_taskset is undefined — it appears nowhere else in the codebase. Every keystroke throws ReferenceError and config.text stays empty, so run() falls back to '(no task text — see the workflow name)'. The whole point of a workflow is 'here is the complaint, run the process against it'; every workflow the marquee non-engineer feature builds ships to the director with the complaint blank.

**Fix:** Drop the inline script and use the working pattern from the same file: oninput="WF.set('<node id>','text',this.value)" — WF.set already updates config, persists and re-renders.

### 10. Workflow gate / select / consistency nodes are drawn as gates and enforce nothing

`bgate_ui/static/wf_steps_asset.js:200` · **both** · **blocker** · effort **M** · raised by tech-art-lead, producer

**Why:** control.consistency declares {threshold:80}, an out port labelled 'passed', and inspector copy promising it 'rejects anything off-model' — its entire runtime contribution is toBrief(), a paragraph of English handed to a qa agent, with the threshold interpolated into prose and nothing comparing it to a score. control.select ('pauses the run so a person chooses') has no config, no port wiring and a static body(). control.gate says 'pause for human approval', has no agentSeat, no endpoint, no approval queue and no pause. A human review gate is the one control that stops a fleet shipping garbage overnight; a node that says it pauses and does not is worse than not offering it, because people build process on it.

**Fix:** Wire them for real (consistency reads metadata.qa_review.score against the threshold and refuses to advance; gate halts the run at status awaiting_approval and surfaces an approve/reject card in the director seat) or relabel them 'QA brief' / 'Review reminder', drop the port labelled 'passed', and remove gate from the palette until it holds.

### 11. A packaged wheel has no JavaScript and no scaffold templates

`pyproject.toml:28` · **backend** · **blocker** · effort **S** · raised by solo-indie, pipeline-eng, studio-cto

**Why:** Verified against the file: package-data is bgate_ui = ['static/*.html'] while index.html loads ~15 scripts (nodecanvas.js, flows.js, flow_*.js, wf*.js, atlas.js) plus static/seats/*.js, none matched and static/seats not declared. packages lists five and omits bgate_engine. scaffold.py:20 resolves TEMPLATES_DIR = Path(__file__).parent.parent/'templates', i.e. site-packages/templates, and templates/ is neither a package nor package-data — so godot_templates reports unavailable and godot_scaffold raises FileNotFoundError. `pip install -e .` hides all of it. The moment anyone installs normally — a wheel, a CI box, a teammate — the flagship first-run action fails and Studio/Seat workspaces/Atlas are dead 404s.

**Fix:** package-data bgate_ui = ['static/**/*']; move templates under bgate_core/templates (or add via package-data + MANIFEST.in) and resolve with importlib.resources; add bgate_engine to packages; add the clean-venv wheel smoke test to CI.

### 12. `python -m bgate_ui` prints nothing — no URL, no port, no confirmation

`bgate_ui/app.py:761` · **backend** · **major** · effort **S** · raised by solo-indie

**Why:** Verified: serve() calls uvicorn.run(..., log_level='warning'), which suppresses uvicorn's INFO-level 'running on http://...' banner, and nothing in serve(), bgate_ui/__main__.py or bgate_cli/main.py prints a line. No browser auto-open. You run the documented command and the terminal sits with a blinking cursor — started? crashed? which port? That is the first ten seconds of the product and it reads as broken. It also means the routes/__init__ 'skipped loudly' print goes into a console nobody is watching.

**Fix:** Print `dashboard → http://127.0.0.1:{port}` plus the resolved project root (or a loud 'no project found here') before uvicorn.run, and offer webbrowser.open behind --no-open.

### 13. A fresh machine sees 'Offline · server 503' because the helpful branch is dead code

`bgate_ui/static/index.html:2055` · **both** · **major** · effort **S** · raised by solo-indie

**Why:** Verified: pollState does showOffline(r.status === 404 ? 'no project — run project_init' : `server ${r.status}`), but _root() and state() both raise HTTPException(503). Nothing on this route ever returns 404, so the only branch a new user can hit is the literal string 'server 503' — while the actual helpful detail ('run the dashboard from inside a game project') sits unread in the response body. The very first paint says 'Offline · server 503' next to a retry link that will fail forever, which is the difference between trying again tomorrow and deleting the repo.

**Fix:** Match 503 as well as 404 and surface (await r.json()).detail; better, have /api/state return 200 {project:null, hint} so the UI renders a first-run screen rather than an error state.

### 14. POST /api/queue 500s on a missing field or unknown seat, and drops source/source_ref

`bgate_ui/app.py:229` · **backend** · **major** · effort **S** · raised by solo-indie, tools-eng, producer

**Why:** Verified: queue_add does payload['seat'] and payload['title'] with bare subscripts and int(payload.get('priority',0)) with no try/except, so KeyError/ValueError become a bare 500 with body 'Internal Server Error' — while queue.add already computed the perfect message (the valid seat list) that the transport throws away, and the artifact endpoints two screens down do the right thing. It also forwards only seat/title/brief/priority: wf.js posts {source:'workflow'} and flow_agent.js posts task items, both landing as source='manual', so director.js's source badge (which renders when source != manual) can never appear. Callers like flow_agent.js test `res.id || res.ok !== false`, so a 500 body reads as success — the client says 'queued to art' and nothing was queued.

**Fix:** Declare a Pydantic model (seat, title, brief='', priority=0, source='manual', source_ref='') so FastAPI returns a 422 with per-field detail for free; catch ValueError -> HTTPException(400, str(exc)) to surface the seat list; pass source/source_ref through with a whitelist of source values.

### 15. status() marks an agent that exited 0 without reporting as DONE

`bgate_ui/dispatch.py:507` · **backend** · **major** · effort **S** · raised by studio-cto

**Why:** Verified: when a reaped process is found with the item still 'dispatched', the code writes 'done' if code == 0 else 'failed'. Exit 0 is the normal exit for a session that hit a context limit, was killed by the harness, or simply stopped early — none of which mean the work was done. The gap analysis documents six agents killed mid-flight in one production run. A silently-abandoned task landing in DONE is the worst possible failure for a producer because it removes the work from the board; you ship a feature you believe exists.

**Fix:** Add an `abandoned`/`unknown` status for exit-without-self-report and surface it as needing triage. Only queue_complete — the agent's own explicit claim — may write 'done'.

### 16. Items stick in 'dispatched' forever after a dashboard restart

`bgate_ui/dispatch.py:359` · **backend** · **major** · effort **S** · raised by gameplay-eng

**Why:** reap_orphans kills surviving claude trees from the pids ledger and clears it but never touches work_item status; the only place a dispatched item flips to done/failed is status(), which walks the in-memory _live dict that died with the previous process. So an item mid-flight at restart stays 'dispatched' forever, dispatch() then refuses it ('is dispatched, not queued'), and with no reopen route it cannot be retried, deleted, or removed from the Running-lane math. On Windows with port conflicts and code edits, restarts are constant; after a week the board is half ghosts and none of the counts are trustworthy.

**Fix:** In reap_orphans (and at startup regardless), flip any status=='dispatched' item whose pid is gone to failed/abandoned with result 'dashboard restarted while this agent was running — reopen to retry'.

### 17. GET /api/agents mutates the database — reconciliation lives in a read handler

`bgate_ui/dispatch.py:494` · **backend** · **major** · effort **M** · raised by tools-eng

**Why:** Verified: status(), reached only via GET /api/agents and GET /api/orchestrator/overview, closes file handles, calls _queue.set_status to mark items done/failed, closes agent stdin, calls _assets.heartbeat and deletes from _live. So the correctness of the queue depends on someone having a browser tab open: close the dashboard overnight and items that died still show as running with stale locks held. A GET must be safe — you should be able to curl it in a monitoring loop without changing state.

**Fix:** Move reconciliation into a background reaper thread started alongside the QA gate (app.py:34 is already the pattern) and make status() a pure read of _live; emit asset heartbeats from the dispatch watcher, not the poll.

### 18. stop() terminates only the parent and records the stop as a mystery crash

`bgate_ui/dispatch.py:613` · **backend** · **major** · effort **S** · raised by gameplay-eng

**Why:** Verified: stop() does entry['proc'].terminate() while every other kill path in the module deliberately uses _kill_tree (taskkill /T /F) — _watch_completion:310 and reap_orphans:393 — with a docstring explaining that terminate alone left 14 orphaned claude.exe at peak because 'the agent's own MCP-server children orphan too'. The Stop button is what you press when the machine is already thrashing, and it leaves children holding the SQLite file and the Godot binary. It also records no intent, so status() writes 'session exited 1 without self-reporting' — three days later the Failed lane cannot distinguish a real breakage from something you killed on purpose.

**Fix:** Call _kill_tree(entry['proc'].pid) after a short terminate grace and _unrecord_pid; mark entry['stopped_by_user'] before terminating and write a distinct 'cancelled' status (or result 'stopped by the user at <ts>') so the Failed lane stays honest.

### 19. Activity feed is hard-capped at 40 steps and re-parses the whole log every 3 seconds

`bgate_ui/dispatch.py:557` · **both** · **major** · effort **M** · raised by gameplay-eng, tools-eng

**Why:** read_activity does log_path.read_text() on the whole file and json.loads every line on every call, then returns steps[-40:] — and agent_activity takes no limit param, so 40 is the ceiling for every consumer while the Inspector is documented as 'full detail + full-scroll history' for a run whose step_count says 300. index.html polls it for every live agent on a 3s loop with the Inspector polling again on top; /api/agent-log?tail=2000 also read_text()s the whole file to slice the tail. _scan_steer_echoes 100 lines away does it correctly with a byte cursor and a docstring saying 'so a 10MB log costs nothing per poll'. Four agents in and the dashboard re-parses tens of megabytes every three seconds on the box compiling the Godot build — and the decision you opened the drawer to find happened 200 steps ago and is unreachable.

**Fix:** Cache parsed steps per item keyed on (file size, run_start_pos) and tail-read only new bytes as _scan_steer_echoes does; add ?limit= and ?before= cursors to /api/agent-activity with a 'load earlier' control; seek from the end in agent_log and return {lines, total_bytes, offset}, 404 for a genuinely missing log so 'not dispatched yet' differs from 'started, silent'.

### 20. /api/state re-hashes every tracked binary on a poll loop, with an N+1 alongside it

`bgate_ui/app.py:181` · **backend** · **major** · effort **S** · raised by solo-indie, tools-eng

**Why:** state() calls _asset_verification, cached only 10 seconds, and assets.verify re-hashes every tracked path via a full sha256 read; the dashboard polls /api/state every 3s and refreshOverview fires an additional one every 4s, so a full SHA sweep of the whole art directory runs roughly every 10 seconds forever. In the same request artifacts.workspace() fetches up to 500 revisions and then issues one SELECT per revision with a work_item_id — up to 500 round-trips — and /api/assets/workspace calls it again independently. An idle tab on a second monitor means the laptop is continuously hashing gigabytes and fighting Godot's importer for disk.

**Fix:** Skip re-hashing when (mtime, size) matches the registry; split verify out of /api/state onto the explicit POST /api/assets/verify and return verified_at so the UI can show staleness. Replace the per-revision lookup with one SELECT ... WHERE id IN (...) hydrated from a dict — the `tracked` lookup in the same function already does it correctly.

### 21. queue_wait pins a threadpool worker for up to 30 minutes and polls the DB

`bgate_ui/app.py:243` · **backend** · **major** · effort **S** · raised by gameplay-eng, tools-eng

**Why:** Verified: queue_wait is a sync def, so it occupies one of Starlette's ~40 threadpool threads for the clamped ceiling of 1800s, doing time.sleep(2.0) and one _queue.get per requested id every pass. The docstring acknowledges it does not stall the event loop but not that it holds a finite worker shared with /api/state and /api/agents — so an orchestrator firing one wait per dispatch batch (exactly what the docstring recommends) can starve the dashboard while agents are running, which is when you need it most. Meanwhile queue._notify already appends every status transition to .bgate/notify.jsonl specifically so consumers can tail one file. Bad input returns HTTP 200 with {error}.

**Fix:** async def with await asyncio.sleep, or back it with an in-process asyncio.Event fired from queue.set_status, or expose notify.jsonl as SSE. Lower the ceiling, return a resumable cursor, and 400 on malformed ids.

### 22. refs_upload writes a temp file using the unsanitized user-supplied name

`bgate_ui/routes/refs.py:77` · **backend** · **major** · effort **S** · raised by tech-art-lead, tools-eng

**Why:** Verified: tmp = Path(tempfile.gettempdir()) / f'bgate_upload_{name}.{ext}' where name comes straight from the JSON body with only .strip() — the ext is allow-listed, the name is not, and slugify happens later inside pin() for the destination only. A name containing ../ escapes the temp dir before pin() sees it and tmp.write_bytes clobbers whatever is there. Every other file-touching endpoint got this right — refs_pin does src.relative_to(r.resolve()) and 403s, deps.safe_under, _safe_audio, pt_video — and the one that didn't is the one that writes arbitrary bytes. Combined with the missing CSRF guard that is an arbitrary-file-write primitive.

**Fix:** Slugify name before building the path (util.slugify already exists), or use tempfile.NamedTemporaryFile(suffix=f'.{ext}') and ignore name for the filesystem entirely; validate name for the DB key too.

### 23. Asset leases are written and heartbeat but never once compared to the clock

`bgate_core/assets.py:136` · **backend** · **major** · effort **S** · raised by tech-art-lead, pipeline-eng

**Why:** Verified by grep: every reference to lease_expires_at across bgate_core and bgate_ui is a write or a display echo — there is no comparison against the current time anywhere. lock() rejects any held lock regardless of how long ago the lease died, and can_write checks only that lock_seat is set. seats.py:318 openly states 'agents die mid-flight constantly (interrupts are normal usage)', so the normal case is a lock held by a process that no longer exists, blocking the seat forever, with the only remedy force_release — which the docstring calls 'a human's call' but which is exposed to any agent with no seat check at all. You get the full bookkeeping cost of leases with none of the recovery.

**Fix:** Treat a lock whose lease_expires_at is past as free in lock() and can_write() (log 'lease expired, reclaimed from <seat>' to activity); split verify()'s 'locked' into locked vs stale so the UI can show it; gate force_release on the absence of BGATE_SEAT so it really is a human's call.

### 24. can_write compares lock_seat only, so two same-seat agents both pass the lock gate

`bgate_core/seats.py:252` · **backend** · **major** · effort **S** · raised by studio-cto

**Why:** Verified: assets.lock records both lock_seat and lock_owner (dispatch sets BGATE_LOCK_OWNER=item-<id>) and assets.release enforces owner-level ownership, but can_write — the oracle the hook asks — only checks `entry['lock_seat'] != role`. Two concurrently dispatched art items therefore both pass on the same .blend. The README's headline justification for the whole asset table is 'two agents editing one .blend loses someone's work'; the lock knows about execution ownership and the gate that actually blocks writes throws that information away, so the exact failure the feature exists to prevent is live the moment you fan out two art agents.

**Fix:** Pass the execution owner (BGATE_LOCK_OWNER, already in the hook's env) into can_write and reject when lock_owner differs, not just when lock_seat differs.

### 25. The hook fails open silently and cannot be proven live

`bgate_cli/hook.py:85` · **backend** · **major** · effort **S** · raised by pipeline-eng

**Why:** Verified: main() wraps everything in `except Exception: return ALLOW` with no logging on that path, no signal when BGATE_SEAT is set but the import failed, and no self-test — bgate_cli offers only hook, hook-install and serve. If bgate_core fails to import (wrong venv, half-installed package, a db.py that raises at import), enforcement vanishes and every write succeeds exactly as if the hook were working. Fail-open is the right default; fail-open silently and unobservably is how a gate stays 'installed' while files land outside lanes for a week.

**Fix:** On the exception path append to <root>/.bgate/hook-errors.log and print one line to stderr (still exit 0). Add `bgate hook --check` that feeds a synthetic payload through decide() and prints the verdict plus resolved root and seat.

### 26. hook-install bakes an absolute interpreter path into a committed settings.json

`bgate_cli/main.py:16` · **backend** · **major** · effort **S** · raised by pipeline-eng

**Why:** HOOK_CONFIG's command is sys.executable + ' -m bgate_cli.hook', evaluated on whichever machine ran hook-install, written into <project>/.claude/settings.json — on this box that resolves under AppData/Local/Packages. install_hook's dedupe matches on .endswith('bgate_cli.hook'), so a stale entry with a dead interpreter counts as 'already installed' and is never repaired. .claude/settings.json is a file people commit, so every teammate who clones the game repo inherits a hook pointing at someone else's AppData that either spams the session or silently allows — and rerunning hook-install will not fix it.

**Fix:** Emit `python -m bgate_cli.hook` (or the bgate console script) and let PATH resolve it; if an absolute path is genuinely needed write it to a gitignored settings.local.json. Make the dedupe compare the full command and rewrite when it differs.

### 27. The QA gate can loop fail -> reopen -> re-dispatch forever

`bgate_ui/qa_gate.py:34` · **backend** · **major** · effort **S** · raised by studio-cto

**Why:** Verified: a daemon polls every 10s over GATED_SEATS=(art, gameplay, audio, narrative) and on done creates and dispatches a QA item whose brief instructs `queue_reopen item_id=<n>` on FAIL, and 'a re-done original gets a fresh QA round once the prior one closed'. Grep of qa_gate.py finds no round/attempt/max counter, no backoff, and no escalation-to-human. The QA persona is explicitly tuned so 'almost is a FAIL'; pair an uncompromising reviewer with an unbounded retry loop and a subjective visual deliverable and it runs all night on a HUD nobody will approve. Nobody can tell a producer 'worst case this costs X'.

**Fix:** Track a round counter per source_ref; after 2-3 failures stop dispatching, mark the item `blocked`, and surface it in the dashboard as needing a human decision. Combine with the global spend ceiling.

### 28. QA gate treats a done item with no VERDICT marker as PASS

`bgate_ui/static/seats/qa.js:479` · **frontend** · **major** · effort **S** · raised by qa-lead

**Why:** _verdictOf regexes /VERDICT:\s*(PASS|FAIL)/ on it.result and, failing that, returns 'PASS' whenever status=='done'. The gate brief only asks for 'VERDICT: FAIL' in the FAIL branch — a PASS is told to complete 'with the evidence' with no required marker. So an agent that completes with a rambling result, or one that fails and forgets the literal string, renders as a green PASS badge. A gate that defaults to green when it cannot parse its own output manufactures false confidence, and this repo already learned that lesson once (the memory note about the gate being silently dead for weeks on a SQL binding bug).

**Fix:** Require an explicit 'VERDICT: PASS' in the pass branch of _brief_for and make _verdictOf return 'UNPARSED' (amber, distinct from PASS) for a done item with no marker. Better: store the verdict in a column instead of regexing prose.

### 29. Feedback items drop t_end, so the telemetry join misses the event being complained about

`bgate_core/playtest.py:356` · **backend** · **major** · effort **S** · raised by qa-lead

**Why:** Verified: feedback.extract() groups segments into thoughts and returns both t and t_end, the INSERT into playtest_item writes only t, and the schema has no column for it — while brief() pulls events with `t BETWEEN item.t - 4 AND item.t + 4`. group_thoughts deliberately merges up to a 1.0s-gap ramble into one item and the module's own comment says a real complaint runs 5-15 seconds. So a 12-second item gets an 8-second evidence window and the jump event at second 11 — the whole reason the item exists — falls outside the join. That is the headline feature ('air_time 0.92s instead of a vibe') silently missing on the longest, most substantive items.

**Fix:** Add t_end to playtest_item, persist it, widen brief()'s window to [t - window_s, t_end + window_s], and return the span so the UI can draw the marker as a range.

### 30. Opening a session review shells out to ffmpeg synchronously inside the GET

`bgate_core/playtest.py:581` · **backend** · **major** · effort **M** · raised by qa-lead

**Why:** brief() calls _ensure_filmstrip on every call, which on first open runs recorder.extract_filmstrip — one ffmpeg pass over the whole mp4 with timeout=300 — and pt_review calls brief(include_transcript=True) on every /api/playtest/{id} GET while openSession shows only 'loading session {id}…' with no progress and no timeout. The first thing anyone does after a session is click it open, and that click can hang for minutes on a static string with no way to tell working from wedged. The director triage item tells the agent to read every frame, so the agent's first playtest_brief eats the same stall.

**Fix:** Extract the filmstrip in the worker thread that already runs transcription in _finish_playtest, record progress in processing_stage, and have _ensure_filmstrip return [] with a 'generating' flag instead of blocking the request.

### 31. pt_video 500s on a bad id and reports a security error for a session with no video

`bgate_ui/app.py:685` · **backend** · **major** · effort **S** · raised by tools-eng

**Why:** Verified: pt_video calls playtest.get with no try/except so a bad id raises LookupError -> 500 (pt_retry has the same hole, while pt_review and iteration_detail correctly map to 404). And session['video_path'] may be None: Path('').resolve() resolves to the CWD, which then fails relative_to(root/.bgate/playtests) and returns 403 'video path escapes playtest storage' — so a session that recorded audio but no video reports a path-traversal error and you chase a phantom security bug for an hour.

**Fix:** except LookupError -> HTTPException(404) in both; check `if not session['video_path']: raise HTTPException(404, 'session has no video')` before resolving.

### 32. A server restart mid-session orphans ffmpeg and leaves an unplayable mp4

`bgate_ui/app.py:33` · **backend** · **major** · effort **M** · raised by qa-lead

**Why:** _LIVE is an in-memory dict and stop() marks the session failed when the recorder is gone. The startup hook reaps orphaned agent processes but does nothing about a row still in status='recording' or an ffmpeg child that outlived the server, and recorder.stop() is the only path that writes 'q' to ffmpeg's stdin — which the code itself notes is required or 'the moov atom never lands'. Restart the dashboard once during a long session (which happens, because agents wedge) and you lose the video entirely, not just the tail: no moov atom means the file will not open, while an orphaned ffmpeg keeps writing and eating disk. The transcript survives and the one thing a human needs dies.

**Fix:** Persist the ffmpeg pid alongside the session; add a startup sweep that terminates orphaned recorders gracefully and marks stale 'recording' rows failed with a reason; attempt `ffmpeg -i broken.mp4 -c copy` remux salvage, or record fragmented mp4 so a killed capture stays playable.

### 33. Promote's seat dropdown omits 'unassigned', silently filing unrouted bugs under director

`bgate_ui/static/index.html:1962` · **frontend** · **major** · effort **S** · raised by qa-lead

**Why:** options(Object.keys(SEATS), item.seat) builds the select from SEATS = {director, narrative, gameplay, tech, art, audio, qa} — no 'unassigned' — but feedback.route() returns 'unassigned' whenever no SEAT_RULES regex matches and it is a legal value. With no matching option the browser selects the first, director, and promoteFeedback posts it verbatim with no visual cue that anything changed. Unrouted items are exactly the ones a human is supposed to route, and the UI pre-answers the question wrong: promote a batch of eight and the director quietly owns four gameplay bugs. Nobody goes looking for a bug that appears to have an owner.

**Fix:** Include 'unassigned' in the options (or a disabled 'choose a seat…' placeholder that blocks promote until picked) and reject an unchanged 'unassigned' inline rather than defaulting.

### 34. Merged feedback items look identical to dismissed ones, and merging is irreversible

`bgate_ui/static/index.html:1961` · **both** · **major** · effort **S** · raised by qa-lead

**Why:** playtest.merge sets status='dismissed' and merged_into_id=target; brief() selects i.* so merged_into_id is in the payload, but grep of every .js/.html finds zero references — the card renders only a 'dismissed' chip and, because status != 'new', loses its buttons entirely, so the merge cannot be undone from the UI. Merging duplicates is the core triage motion and this one is a trapdoor: merge #14 into #9, realise #14 was a different bug that used the same words, and it now reads identically to something you deliberately threw away. 'Never lose a bug report' means a merged item stays visibly attached to its target.

**Fix:** Give merged items their own status/badge ('merged into #9', linking to the target), keep them out of the dismissed bucket, and add an unmerge that clears merged_into_id and restores status='new'.

### 35. The art seat's Approve/Reject call the dumb endpoint, so rejection teaches nothing

`bgate_ui/static/seats/art.js:438` · **both** · **major** · effort **S** · raised by tech-art-lead

**Why:** Verified: grep of art.js for 'react' finds one hit, inside a comment. POST /api/artifacts/{id}/react fans a verdict three ways — sets disposition, posts a durable 'ART PREFERENCE' seat note the next agent reads in seat_brief, and live-steers the running agent — and the flagship art surface wires itself to /review instead, which only writes review_note into a column nobody reads. So 'lost the headband, palette went muddy' dies in the DB and the next generation makes the identical mistake. The good feedback loop was built and the seat that most needs it picked the other one.

**Fix:** Point the art seat's Approve/Reject at /api/artifacts/{id}/react with verdict like/dislike and the active item_id, or make /review itself post the seat note and steer. Keep one path.

### 36. artifact_react always returns ok:true with a grab-bag of optional error keys

`bgate_ui/app.py:351` · **backend** · **major** · effort **S** · raised by tools-eng

**Why:** out = {'ok': True, ...} is set up front and never flipped; each of the three sub-operations stuffs a different optional key on failure (review_error, note_error, steer_error) and an unrecognized verdict silently does nothing and still returns ok:true. The client ternary-chains on presence — r.steered ? 'steered + saved' : r.saved_preference ? 'saved' : 'done' — so a call where all three legs failed renders the word 'done'. You dislike an off-model sprite, the UI says done, the preference note never got written, and the next art agent repeats the mistake.

**Fix:** Return {ok, verdict, steps:[{name:'review'|'preference'|'steer', ok, error?}]} with top-level ok as the AND; reject an unknown verdict with a 400 listing valid values.

### 37. Finished agents appear for exactly one poll, then vanish with their result

`bgate_ui/dispatch.py:494` · **backend** · **major** · effort **S** · raised by producer

**Why:** status() reaps in the same pass it reports: on a nonzero poll it appends {item_id, state:'exited', code} and immediately deletes from _live. director.js builds a 'finished' card with the agent's final result text from exactly that payload, so the next 3s poll no longer contains it and the card disappears — and two open tabs race, with only one ever seeing it. You walk back to your desk and the board is empty: did it finish, did it fail, what did it produce. The one moment you most need ('agent done, here is what it did') is shown for under three seconds to whoever polled first.

**Fix:** Keep exited entries in a bounded recently-finished list (last 20, or 10 minutes) and return them with state:'exited' until acknowledged so the board can show a stable completion card.

### 38. Director board renders every item ever created, briefs and all, every 3 seconds

`bgate_ui/routes/orchestrator.py:100` · **both** · **major** · effort **S** · raised by producer

**Why:** overview() calls _queue.list_items(r) with no status filter and no limit; queue.list_items has no LIMIT clause and returns full rows including the entire brief; director.js renders every returned item into its seat column with the only status-conditional bit being hidden dispatch buttons; SeatShell polls refresh() every 3s. Month two with ~400 completed items, the producer cockpit is a wall of grey cards you scroll past to find the six things actually queued, and every open tab pulls every brief every three seconds.

**Fix:** Default the overview to open items (queued/dispatched) with a 'show completed' toggle and limit/since params; strip brief from the list payload and fetch it on card expand.

### 39. Delegation loses the parent-child link the moment the page reloads

`bgate_ui/routes/orchestrator.py:74` · **both** · **major** · effort **S** · raised by producer

**Why:** delegate() creates a director item with source='delegate' and source_ref=<source item id> and returns delegate_item_id, but the client stores it in an in-memory S.delegateWatch purely for a border highlight, lost on reload. The child items the director then creates via queue_add get whatever source the agent passes, and the delegate brief never tells it to set source_ref — so tasks produced by a split have no recorded link back to the original ask. You hand a chunky feature to the director, come back after lunch, and five tasks across four seats have nothing tying them to what you asked, which makes the only question about delegation ('did the split cover the whole ask') unanswerable.

**Fix:** Instruct the delegate brief to pass source='delegate', source_ref=<original item id> on every queue_add, and render children nested under (or linked from) their parent in the director board.

### 40. Two fully-built Studio flows are unreachable — the flow whitelist is hardcoded

`bgate_ui/static/flows.js:19` · **frontend** · **major** · effort **S** · raised by producer

**Why:** Verified: Studio.activate() forces this._flow into ['workflows','game'] and index.html renders exactly two subnav buttons, while flow_agent.js:24 registers window.StudioFlows.agent (a ~500-line live orchestration canvas with polling, steer, delegate, position persistence) and flow_asset.js:732 registers StudioFlows.asset. Nothing in static/ ever calls Studio.select('agent') or Studio.select('asset'), and flows.js still carries older built-in asset()/agent() methods that are equally unreachable. Both files load and sit dead. Someone shipped the nicest orchestration view in the product and nobody can find it; half the Studio code a bug report targets is not running.

**Fix:** Add Agent flow / Asset flow subnav tabs and widen the activate() whitelist, or delete flow_agent.js, flow_asset.js and the legacy methods.

### 41. Asset locks and drift never appear in the art seat

`bgate_ui/static/seats/art.js:186` · **frontend** · **major** · effort **M** · raised by tech-art-lead

**Why:** artifacts.workspace() attaches a full lock block per revision (seat, owner, work_item_id, heartbeat_at, lease_expires_at) and /api/assets/verify returns modified/missing/untracked_hash, but art.js consumes /api/assets/workspace only for the flow map's rigged/approved colouring and grepping it for 'lock' returns CSS. Drift is shown solely in the generic Vault table. So the pipeline owner hits Regenerate on a sheet another agent holds a lock on, the work queues happily, and the conflict surfaces as a stomped binary later — and 'modified with no lock held', the one signal that says somebody stomped your file, is invisible from the flagship art surface.

**Fix:** Badge each logical asset row with its lock holder and lease, disable Regenerate/Restore under a foreign lock with a tooltip naming the holder, and surface verify()'s 'modified' set as a red flag on the affected asset.

### 42. Narrative storyboard is an island: no canon check, no lore link, no path to work

`bgate_ui/static/seats/narrative.js:130` · **frontend** · **major** · effort **M** · raised by producer

**Why:** The module talks to exactly three endpoints (workspace storyboard, /api/artifacts, /api/refs). There is a 'lore' panel preset but it is only a text template — no entity binding, no canon_check, no queue_add. So three acts of beats sit in a JSON blob nobody downstream reads: gameplay never sees them, canon never validates them, and every beat gets retyped into the queue by hand. It is a whiteboard bolted onto a pipeline it does not touch.

**Fix:** Add a per-panel action bar: 'check canon' (POST the beat text to a new /api/canon/check), 'link entity' (bind panel -> lore slug), and 'queue this beat' (queue_add to narrative/gameplay with the panel text as brief and the panel id as source_ref).

### 43. Workspace docs are last-write-wins with no version check, and narrative refresh is a deliberate no-op

`bgate_core/workspace.py:30` · **both** · **major** · effort **S** · raised by producer, tools-eng

**Why:** workspace.set() is an unconditional upsert on (seat,key) with no version/etag returning only {ok, seat, key} — no updated_at echo, though the column exists — and ws_set validates neither seat (against DEFAULT_SEATS, which queue.add does validate) nor key length nor blob size. narrative.js autosaves the whole storyboard a second after any keystroke and its refresh() is an explicit no-op 'so it can never clobber in-progress typing'; WF.persist() does the same for the whole workflow. Two tabs, or you and the narrative lead, and the last person to type wins the entire board — panels the other added evaporate with no warning and no history. On a storyboard that is a morning of work.

**Fix:** Validate seat (404 otherwise), cap key length and body size, return updated_at from set() and ws_get, and accept If-Match/expected_updated_at that 409s on mismatch so the client can offer 'this board changed elsewhere — reload?'.

### 44. MCP failures arrive in three incompatible shapes with no shared predicate

`bgate_mcp/server.py:60` · **backend** · **major** · effort **M** · raised by pipeline-eng

**Why:** _fail returns {'error': 'TypeName: msg'} with no ok key; adapters return {'ok': False, 'error': ...}; probes return {'available': False, 'reason': ...}; queue_next returns {'empty': True}; project_status, seat_brief, bible_read and lore_brief return bare payloads with neither. So godot_status with Godot missing gives {available:false, reason} while godot_run with Godot missing gives {error:'GodotNotFound: ...'} — same cause, different shape, no shared key. Worse, item_variants returns {'ok': False} for a *policy refusal* (over the spend limit), indistinguishable from a real error, so an agent reads it as 'the tool is broken' and retries with force. That one costs money.

**Fix:** One envelope enforced by a decorator over every @mcp.tool: {ok:true, ...payload} or {ok:false, error_code, error, hint} with error_code from a closed set (BINARY_MISSING, TIMEOUT, LOCKED, OUT_OF_SCOPE, SPEND_LIMIT, NOT_FOUND). Document it once in the module docstring.

### 45. image_sprites has no spend cap or timeout while item_variants has both

`bgate_mcp/server.py:1035` · **backend** · **major** · effort **S** · raised by pipeline-eng

**Why:** item_variants refuses to run when planned images exceed limit (default 12) and returns the dollar estimate so the agent can confirm. image_sprites takes no limit, no timeout and no cost cap: it generates a reference with up to max_retries re-rolls, then one edit per pose, then re-rolls every frame the consistency gate flags up to max_retries, and each gate pass additionally calls gpt-4o-mini vision over all frames. The docstring says 'Cost: 1 ref + 1 edit per pose (~$0.04-0.25 each)', omitting the retry multiplier and the vision calls entirely — so an agent budgets 12 x $0.25 = $3 and the real bill is double plus vision, with no point at which the tool stops and asks.

**Fix:** Give image_sprites the same limit/estimate/refuse contract as item_variants, add a timeout parameter, and correct the docstring to state the worst case (ref re-rolls + N poses + up to N retries + one vision call per gate pass).

### 46. An agent-facing error names a parameter no MCP tool exposes

`bgate_adapters/imagegen.py:85` · **backend** · **major** · effort **S** · raised by pipeline-eng

**Why:** Verified: _reject_multi_pose tells the caller to 'pass allow_multi=true only for genuinely multi-subject art (crowds, rosters, backdrops)', and imagegen.generate/edit both accept it — but grep shows allow_multi appears nowhere in bgate_mcp/server.py, so image_generate and image_edit neither declare nor forward it. An agent asking for a tavern crowd or a character-select roster trips the regex on 'four' or 'roster', reads an error naming the exact fix, tries to pass it, gets a schema validation failure, and then starts mangling the prompt to dodge a regex. That is the worst kind of dead end: one that lies about having an exit.

**Fix:** Add allow_multi: bool = False to image_generate and image_edit and forward it; if it is meant to be unreachable, delete the instruction from the error text.

### 47. godot._errors substring-greps 'invalid' and 'error:', failing healthy builds

`bgate_adapters/godot.py:415` · **backend** · **major** · effort **S** · raised by pipeline-eng

**Why:** Verified: _errors() flags any line containing script error / parse error / error: / failed to load / can't open / cannot open / invalid, and check_project sets ok = returncode==0 and not errors — so one line mentioning 'invalid' fails the build check. Godot 4.4+ routinely prints UID and cache warnings containing that word during --import, and any user script printing a string with 'error:' trips it. Meanwhile run_script uses a completely different rule (ok = returncode==0 and 'SCRIPT ERROR' not in output), so the same engine output is judged two ways in one module. godot_check_project is the 'does it still build' gate the tech seat is told to run; a false FAIL sends an agent chasing a nonexistent regression, and after twice the agents learn to ignore the gate.

**Fix:** Match Godot's real line prefixes (SCRIPT ERROR, ERROR:, USER ERROR:, Failed to load) anchored at line start, split warnings into a separate list that does not affect ok, and use one helper in both run_script and check_project.

### 48. Blender discovery sorts install dirs lexicographically and enforces no version floor

`bgate_adapters/blender.py:94` · **backend** · **major** · effort **S** · raised by pipeline-eng

**Why:** Verified: find_blender returns sorted(found)[-1] over glob hits, a plain string sort — 'Blender 4.9' beats 'Blender 4.10' and 'Blender 10.0' loses to 'Blender 4.5'. godot.py:98 does this correctly with a parsed version tuple, so the inconsistency is inside one codebase. Neither adapter enforces a minimum: ENGINES advertises BLENDER_EEVEE_NEXT (4.2+) and on an older build _blender_runner catches the TypeError and silently substitutes BLENDER_WORKBENCH, so a team on distro Blender gets lit renders quietly downgraded to flat workbench previews and the art seat 'fixes' lighting for two days against a render mode it never asked for. The 4.10 sort bug lands the day Blender ships that version.

**Fix:** Reuse godot.py's parsed-version ranking; add MIN_BLENDER/MIN_GODOT checked in available()/version() with an actionable reason; make the engine downgrade a loud result field (engine_requested vs engine_used) rather than a silent except.

### 49. .env is not in .gitignore, and the tooling claims it is

`.gitignore` · **backend** · **major** · effort **S** · raised by pipeline-eng

**Why:** Verified: the repo .gitignore lists .bgate/, .bgate_out/, .qa_deps/, .asset_work/, .godot/ and more, and has no .env entry. envfile.load_project_env reads <root>/.env for the active project root, and project_init creates .bgate/ without touching .gitignore — grep for 'gitignore' across bgate_core/bgate_mcp/bgate_cli returns nothing. Meanwhile image_status's docstring and imagegen.available() both tell the user to put the key 'in the project's .env (gitignored)', asserting a property the tooling never establishes. `git add -A` after a scaffolding session is how API keys reach GitHub, and the product's own onboarding text is what convinced the user it could not happen.

**Fix:** Add .env to the repo .gitignore; have project_init append .env to <root>/.gitignore (creating it if absent) and report that it did; have image_status verify with `git check-ignore -q .env` instead of claiming it.

### 50. The .env cache never invalidates, so fixing a missing key needs a server restart

`bgate_core/envfile.py:13` · **backend** · **major** · effort **S** · raised by pipeline-eng

**Why:** Verified: _loaded is a module-level set keyed on the resolved path with no TTL and no mtime check; reset_cache() exists but is documented 'Tests only' and is exposed by no tool or route. The shell-wins rule is `if name not in os.environ`, so a var set to the empty string counts as present and permanently shadows the file. The loop this creates: image_status says the key is not set, the user writes it into .env, the agent calls image_status again, still not set, and now both are debugging the wrong thing. Same trap for BGATE_IMAGE_MODEL or BGATE_PALETTE_COHESION mid-tuning-session.

**Fix:** Key the cache on (path, st_mtime_ns) so an edited .env reloads, treat an empty-string existing var as absent, and expose an env_reload tool (or make image_status force a reload) returning loaded KEY NAMES only.

### 51. SEAT_RULES hardcodes another game's assets into every dispatch, and seats can't override them

`bgate_ui/dispatch.py:50` · **backend** · **major** · effort **M** · raised by studio-cto

**Why:** SEAT_RULES is injected unconditionally into every dispatch prompt and contains project-specific direction ('Director directive: gpt-image-2 is banned'), while the protocol block hardcodes 'run game/tests/fight_test.gd via godot_run when combat code moved'. seats.py:96 bakes named pinned refs into the QA persona — concept-fight-hud, concept-select, tommy/scoville-bright16 — and seats.configure() can only override enabled, write_globs and mission; the workflow text and SEAT_RULES are not overridable at all. Every agent on someone else's game is told to compare its work against an asset that project does not own and run a test file that does not exist.

**Fix:** Move SEAT_RULES and per-seat workflow text into per-project config (extend seat_config with workflow/rules columns, expose via seat_configure), ship the current strings as an example project profile, and keep the shared library game-agnostic.

### 52. Asset library categories and the play-panel controls hint are hardcoded to one game

`bgate_ui/static/index.html:1576` · **frontend** · **major** · effort **S** · raised by solo-indie

**Why:** Verified: assetCategory(name) returns 'scoville' if the logical name contains scoville and 'tommy' if it contains tommy, and CAT_ORDER lists them as the first two section headings in every project's Assets view — so every asset in a different game lands in 'misc' below two near-empty buckets. Separately the play panel hint is the literal string 'A/D move · Space jump · J/K punch · U/I kick · S block · L duck · F1 tuning' while templates/2d/scripts/player.gd reads only jump/move_left/move_right and scaffold.py describes the 2d template as a side-on platformer slice. You scaffold the platformer, boot it, and the dashboard tells you to press J to punch.

**Fix:** Derive groupings from canon entity names (/api/state already ships lore.canon) or the logical-name prefix before the first underscore, keeping the generic buckets as fallback; read controls from project.godot's [input] section (the Atlas scanner already parses project sources) or drop the hint to a generic line.

### 53. Record button is permanently dead on a stock install with a truncated, unactionable reason

`bgate_ui/static/index.html:2593` · **both** · **major** · effort **S** · raised by solo-indie

**Why:** ptPreflight disables #pt-btn unless p.ready, and playtest.preflight requires ffmpeg + mic + transcriber all ok — while faster-whisper and sounddevice are optional extras, so `pip install -e .` always fails. The UI renders 'not ready: transcriber (' + reason.slice(0,60) + ')' inside a one-line flex bar with text-overflow:ellipsis, so at narrow widths you get a chopped-off error and no remediation. Playtest mode is the reason to use this tool at all and its button is greyed out on install day; the fix is in the README twenty lines above where the reader stopped.

**Fix:** Render failing checks as a small list with the literal fix per check ('transcriber missing -> pip install -e \".[stt,record]\"', 'ffmpeg not on PATH -> ...') and do not truncate the reason.

### 54. A route module that fails to import disappears silently

`bgate_ui/routes/__init__.py:25` · **backend** · **major** · effort **S** · raised by solo-indie

**Why:** Verified: register() claims a broken module is 'skipped loudly', but loudly is print(f'[routes] skipped {info.name}: {exc}') into a terminal serve() already silenced to log_level='warning' and that (per the no-startup-banner finding) nobody is watching. app.py assigns the result to _registered_routes, which is never read, never logged and never exposed. If art_qa.py fails to import because Pillow is a version off, the Art workspace just 404s with a blank panel — nothing in the UI, console or API says a feature was skipped, so the conclusion is 'the tool is broken' when the fix is one pip install.

**Fix:** Print registered/skipped on startup regardless of log level and expose {routes:{registered:[...], skipped:[{name,error}]}} in /api/state so the UI can badge the affected view.

### 55. README describes a dashboard that no longer exists

`README.md:89` · **frontend** · **major** · effort **S** · raised by solo-indie, studio-cto

**Why:** The README names 'the Floor' (seven seat bays with glyph, accent, lamp, held binaries), 'The Ledger', 'Asset Lab', 'Playtest Review', 'Iteration Timeline', 'The World'. The actual rail is Overview / Agents / Studio / Seat workspaces / Playtests / Assets / Atlas / World bible / Timeline. The seat bays are gone — renderFloor now emits .seatpill chips into #floor inside the Agents view, and the .bay CSS is dead (grep confirms zero `class="bay` in any file). Nothing in the README mentions Studio, Seat workspaces or Atlas. Docs describing a previous version are worse than none: after the Floor turns out not to exist, nothing else the README says is trusted. Requirements also omits Claude Code, the Anthropic account, token cost and the acceptEdits permission mode that one dashboard click activates.

**Fix:** Rewrite the dashboard section against the current rail, one line per view, naming prerequisites (Atlas needs scenes, Playtests needs ffmpeg+whisper); add Claude Code to Requirements with a note on cost and permission mode; add a first-dispatch confirmation naming the permission mode and the tools granted.

### 56. Responsive CSS targets deleted markup, so the game renders in a 390px letterbox

`bgate_ui/static/index.html:995` · **frontend** · **major** · effort **S** · raised by solo-indie

**Why:** Verified: no markup uses .bay, .app-shell or .build-stage. The live shell is .deck{grid-template-columns:var(--rail-w) 1fr; height:100vh; overflow:hidden} with no media query touching .deck or .rail anywhere, while every existing breakpoint targets classes that no longer exist. Consequence: the intended .build-stage .playpanel{height:620px} never applies and #gameframe falls back to min-height:390px. On a 1366x768 laptop the 236px rail is permanent and the game renders below the fold under five stat tiles; on a 1440p monitor it still renders at 390px. The one thing you actually want to look at is the smallest thing on screen.

**Fix:** Delete the dead breakpoints, add a real one on .deck collapsing the rail to icons (~64px) below ~1100px, and give #gameframe an aspect-ratio-based size that grows with the stage.

### 57. Iteration timeline reports hashes and raw counts, never whether the game got better

`bgate_core/iterations.py:231` · **both** · **major** · effort **M** · raised by producer

**Why:** complete_from_playtest builds outcome = {session_id, duration_s, feedback, telemetry_events, promoted} plus versus_previous as a raw delta of the same counts and snapshot_delta booleans; the UI renders commit/dirty/source/export fingerprint stubs and 'feedback +7 · events +240'. The iteration's own goal string is printed as a heading and never evaluated against the outcome — there is no verdict field anywhere in iterations.py. 'Feedback +7' is unreadable: did the build get better (players engaged, more notes) or worse (more complaints)? This is the view you would put on the wall at milestone review and it cannot be used for that.

**Fix:** Capture a goal verdict on iteration close (met / partial / missed plus one line of why, written by the director from the transcript), store it in outcome_json, lead each timeline card with it, and move fingerprints behind a details toggle.

### 58. bgate_engine proposes a second authoritative simulation derived from one title

`bgate_engine/DESIGN.md:1` · **backend** · **major** · effort **L** · raised by studio-cto

**Why:** The locked decision, stated twice, is that the Python engine is the source of truth and Godot becomes a renderer/playback surface that does not re-simulate for proofs. §1 is titled 'This is not a generic toy — it describes Commodity Brawler' and states the schemas are derived field-by-field from one test game's boxer.gd/fight.gd; status is schemas-only with no runtime, and it replaces file locks with component-level conflict detection. The appeal is real — typed addressable replayable state is the honest answer to 'agents cannot reason about a path pile' — but the ask is that a team maintain game logic twice, in shipping GDScript and in Python, in lockstep forever. That divergence is a bug class that will eat more time than agents save, and every new genre needs the whole thing re-derived.

**Fix:** Reframe the engine as an extractive index over the real GDScript (parse/instrument the shipping code into typed queryable state) rather than an authoritative re-implementation, and prove the component schema generalizes across two dissimilar genres before locking the decision.

### 59. Agent activity leaks the seat-identity system prompt and truncates results to 160 chars

`bgate_ui/dispatch.py:594` · **frontend** · **minor** · effort **S** · raised by studio-cto

**Why:** read_activity emits {'kind':'result','text': txt.strip()[:160]} for every tool result and [:280] for assistant text, and the initial user turn carries SEAT_IDENTITY (seats.py:126, beginning 'YOU ARE A SPAWNED SEAT WORKER...') which docs/ui-ux-audit.md confirms is rendered verbatim into the QA log view. There is no expand or raw toggle in the parsed feed. Two problems in one panel: a producer sees the internal prompt scaffolding, which reads as the tool leaking its guts, and an engineer cannot see the one tool result they need because it was cut at 160 characters. The audit already flagged this as MAJOR and it is still the shape of the code.

**Fix:** Filter the SEAT_IDENTITY preamble out of user-turn rendering, and store full result text with a per-step expand toggle instead of a hard truncation.

### 60. Raw log tab interleaves runs with no separator, while the parser 200 lines away handles it

`bgate_ui/app.py:303` · **both** · **minor** · effort **S** · raised by gameplay-eng

**Why:** The log is opened 'ab' and appends across re-dispatches. read_activity handles this correctly by seeking past the last bgate_run_start marker, with a comment noting that stale results shown as current was a real observed bug. agent_log has no such filtering — it splitlines the whole file and returns lines[-tail:], so the Raw log tab (tail=2000) shows the tail of run 1 and all of run 2 with nothing but a bare JSON marker between them. The raw log is the fallback you use precisely when debugging a re-run, which is exactly the case where two runs are interleaved and you misread the previous attempt's error as the current one.

**Fix:** Give agent_log a run=current|all parameter defaulting to current, reusing the marker split, and render a visible '── run 2 started <ts> ──' divider when showing all.

### 61. Dispatching a nonexistent item id returns a 500 stack instead of a clean error

`bgate_ui/dispatch.py:202` · **backend** · **minor** · effort **S** · raised by gameplay-eng

**Why:** dispatch() calls _queue.get(root, item_id), which raises LookupError for a missing id; neither dispatch() nor queue_dispatch catches it and app.py registers no global exception handler, so it surfaces as an unhandled 500 — while every other error in the same function returns a clean {ok:false,error}, and orchestrator.py:70 already shows the intended LookupError -> 404 pattern. It is the stale-board case: the tab was open, an item was completed and cleaned up elsewhere, and the click returns an opaque 500 that the Overview UI does not even display. A structured 404 is something a UI can act on.

**Fix:** try/except LookupError in dispatch()/steer()/stop() returning {ok:false,error:'no work item N'}, or raise HTTPException(404) in the routes as orchestrator.py does.

### 62. reap_orphans kills by name prefix from a best-effort pid file

`bgate_ui/dispatch.py:392` · **backend** · **minor** · effort **S** · raised by studio-cto

**Why:** On startup reap_orphans reads .bgate/agents/pids.json and for any recorded pid not in _live runs tasklist and kills the tree if name.startswith('claude'). That name-prefix check is the entire pid-reuse guard, ledger writes are best-effort (except: pass) so entries can be stale indefinitely, and _kill_tree is taskkill /T /F. On a long-uptime Windows box where the developer keeps an interactive Claude Code session open all day, a recycled pid means restarting the dashboard force-kills their work with no prompt and no log line beyond 'reaped N orphaned agent(s)'. Small blast radius, terrible trust impact the one time it fires.

**Fix:** Record process creation time alongside the pid and require it to match before killing; skip any pid whose command line lacks the BGATE_SEAT/BGATE_WORK_ITEM marker; log each kill with pid, item and command line.

### 63. Gameplay seat leaves Stop and Steer enabled with no live agent

`bgate_ui/static/seats/gameplay.js:184` · **frontend** · **minor** · effort **S** · raised by gameplay-eng

**Why:** The gameplay panel renders Dispatch/Stop/Steer as always-enabled and relies on the backend's 'no live agent for this item' surfacing as a toast, while tech.js does it properly — re-rendering the button set from `running` and setting disabled on both the steer input and its button. You type a course-correction into a box that looks live, hit enter, and learn from a toast that the agent finished 30 seconds ago; your text is gone. Two seat panels behaving differently in the same app also means you stop trusting either one's affordances.

**Fix:** Mirror tech.js: disable Stop/Steer unless liveAgentFor(S.selItem), disable Dispatch unless the item is queued, and do not clear the steer input until the POST returns ok.

### 64. Reference thumbnails hardcode .png, so jpg/webp pins render blank

`bgate_ui/static/wf_steps_asset.js:19` · **frontend** · **minor** · effort **S** · raised by tech-art-lead

**Why:** refThumb builds '/api/preview?rel=' + '.bgate/refs/' + name + '.png' and hides the img on error; flow_asset.js:87 does the same. But refs.pin writes dest = <slug> + src.suffix.lower() and the upload endpoint accepts jpg, jpeg, webp, gif and svg. Photo-bashed jpg references pin fine and resolve fine for generation, and then the workflow node shows a blank card — so the artist thinks the character anchor is not set and re-pins it, which (per the ref-versioning finding) is a destructive act that rewrites the history of every past comparison.

**Fix:** Have /api/refs return the project-relative path (it already has ref.path) and use that for the preview URL instead of reconstructing a filename with a guessed extension.

### 65. The workflow reference node has no picker — its config panel is one sentence

`bgate_ui/static/wf.js:272` · **frontend** · **minor** · effort **S** · raised by producer

**Why:** input.reference's body() renders an <img> from '.bgate/refs/' + n.config.ref + '.png' when config.ref is set and otherwise says 'pick a reference'. Its config() returns only a paragraph — no list, no input, no fetch of /api/refs — and nothing else in the codebase sets config.ref. The card says 'pick a reference' and clicking it gives prose. Art consistency across a fleet lives or dies on pinned refs, so a reference node that cannot point at a reference gets dragged in once and quietly abandoned.

**Fix:** Give it a real config(): fetch /api/refs, render a thumbnail grid, set config.ref via WF.set — narrative.js's _openPicker (seats/narrative.js:470) is already exactly this widget.

### 66. Saved-workflow delete has no confirmation and orphans the stored document

`bgate_ui/static/wf.js:115` · **both** · **minor** · effort **S** · raised by producer

**Why:** The ✕ on a saved-workflow card calls WF.deleteSaved(id) immediately; deleteSaved filters the id from the local list, POSTs the new index and re-renders, never deleting the workspace doc at key 'wf:<id>' — and routes/workspace_doc.py has no DELETE endpoint to do so. No confirm, no undo. An ✕ in the corner of a clickable card is a misclick waiting to happen, and the workflow you spent an afternoon wiring is gone from the library with no way back while its data sits in the project DB forever.

**Fix:** Confirm before delete (or offer an undo toast), add DELETE /api/workspace/{seat}/{key}, and call it so the document goes with the index entry.

### 67. QA panel's playtest widget reads a field the API does not send

`bgate_ui/static/seats/qa.js:412` · **frontend** · **minor** · effort **S** · raised by qa-lead

**Why:** _paintPlaytest reads rec.event_count then st.event_count; /api/playtest/status names the field telemetry_events and puts nothing at the top level, so both branches miss and the panel renders a literal em dash on every poll. The event count is the one signal that telemetry is actually flowing — the main dashboard even colours its lamp on it — so from the QA seat a live session and a session where the game emits nothing look identical.

**Fix:** Read rec.telemetry_events; grep for other field-name drift between the seat panels and the status payload while you are there.

### 68. The QA panel's record button bypasses preflight, staleness check and frame boot

`bgate_ui/static/seats/qa.js:433` · **frontend** · **minor** · effort **S** · raised by qa-lead

**Why:** startPlaytest posts {name} only. The overview path first checks /api/play/status, rebuilds a stale web build and aborts if the rebuild fails, then boots the game iframe with bgate_session so telemetry has somewhere to post. The QA panel does none of it and never calls preflight, so a missing ffmpeg or dead mic surfaces as a generic 'could not start recording' toast. Two buttons labelled record that record different things is how a session gets recorded against a stale build and an afternoon goes to chasing a bug fixed yesterday — and the panel's own copy claims 'same flow as the app playtest', which is exactly what it is not.

**Fix:** Extract toggleRecord's logic into a shared module both entry points call so preflight, rebuild-if-stale and frame boot happen identically, and show preflight failure reasons inline.

### 69. Seeking ignores video_offset_s, so the human sees uncorrected time and the agent sees corrected

`bgate_ui/static/index.html:1999` · **frontend** · **minor** · effort **S** · raised by qa-lead

**Why:** seekReview sets video.currentTime = t where t is a session-clock timestamp, and syncTranscript compares video.currentTime directly against segment t_start/t_end. But video time 0 corresponds to session time video_offset_s, and the backend does correct for it when pulling item frames (video_t = max(0, item.t - session.video_offset_s)). The offset is in the session row but is not included in brief()'s session projection. Small today because ffmpeg starts first, but it is an inconsistency between the evidence the agent sees and the evidence the human sees, and inconsistent evidence is how a bug gets argued about instead of fixed. It drifts silently the moment the capture path gains a startup delay.

**Fix:** Include video_offset_s in brief()'s session payload and subtract it in seekReview and syncTranscript, matching the frame-extraction path.

### 70. Preflight polls every 15s, opening the mic and spawning a whisper probe each time

`bgate_ui/static/index.html:2702` · **both** · **minor** · effort **S** · raised by qa-lead

**Why:** setInterval(ptPreflight, 15000) hits /api/playtest/preflight, which runs recorder.probe_mic (sd.rec of 1.5s + sd.wait) and transcribe.available(), a subprocess launch with a 60s timeout — guarded only by `if (ptRecording) return`. Grabbing the input device every 15 seconds all day fights Discord and OBS and keeps waking a wireless headset, and the dashboard spawns four python subprocesses a minute forever to re-answer a question whose answer changes about once a week.

**Fix:** Cache the transcriber/ffmpeg checks for minutes, make the mic probe on-demand behind a 'test mic' button, and back the interval off once a preflight has come back ready.

### 71. Atlas's dead/missing-asset badge only appears after you have already opened Atlas, and rescans everything per request

`bgate_ui/static/atlas.js:79` · **both** · **minor** · effort **S** · raised by solo-indie, producer

**Why:** #rc-atlas starts display:none and is only written inside render(), run from Atlas.activate() — nothing scans /api/screenmap on boot, while the equivalent counters for Agents and Playtests are updated by the global polls. The whole point of a red count in the nav is to tell you about a problem you have not looked at yet; this one only reports problems you have already seen, so 12 orphaned sprites bloating an export stay invisible. Separately GET /api/screenmap calls screenmap.scan fresh per request ('derived fresh per call — no manifest'): it rglobs every .tscn and reads each, reads every referenced script, rglobs every .gd again, reads every .tres, then rglobs the whole assets/ tree, with no mtime cache, no ETag and no limit.

**Fix:** Fetch /api/screenmap once on boot and on a slow interval purely to populate the badge; cache the scan on a cheap directory-mtime fingerprint, invalidate on change, and send an ETag so repeat views are 304s.

### 72. Temp directories leak on every Godot/sprite call and on every Blender failure path

`bgate_adapters/godot.py:135` · **backend** · **minor** · effort **S** · raised by pipeline-eng

**Why:** godot.run_script and godot.inspect_resource each mkdtemp and never clean up; sprites.render_sprites mkdtemps a directory holding every rendered pose PNG and never removes it. blender.run_script does rmtree but only on the fully successful path — the timeout return, the no-result return and the JSON-decode return all leak, i.e. exactly the failure cases you retry most. A day of sprite iteration at 128px x 20 poses x dozens of runs quietly fills %TEMP% with hundreds of MB, and the runs that leak most are the ones that already failed, so a retry loop compounds it. On a build agent with a small system disk that is a disk-full at 3am with no clue in the logs.

**Fix:** try/finally with shutil.rmtree(ignore_errors=True) or tempfile.TemporaryDirectory; keep the directory only when the run failed AND stash its path in the result so it is debuggable on purpose.

### 73. Adapters write to fixed output paths that concurrent seats clobber

`bgate_mcp/server.py:1461` · **backend** · **minor** · effort **S** · raised by pipeline-eng

**Why:** godot_screenshot writes every capture to <root>/.bgate_out/shot.png; blender.run_script renders to <out>/render.png — the _archive_preview docstring even calls this out ('renders land on a fixed path and each run overwrites the last'); consistency_check writes .bgate_out/art/consistency_check.png. Archiving copies these to timestamped previews afterwards, but the returned `path` always points at the shared mutable file. The whole premise is seven seats working at once: art renders while QA screenshots, both get a path that by the time anyone reads it holds the other's image, and the archive copy can race the overwrite too.

**Fix:** Include a per-call unique suffix (pid + monotonic ns, or BGATE_WORK_ITEM) in every adapter output filename and return that path; the fixed name buys nothing now that previews are archived.

### 74. seat_brief returns an uncapped blob and every seat is told to call it first

`bgate_core/seats.py:282` · **backend** · **minor** · effort **S** · raised by pipeline-eng

**Why:** brief() returns pinned_refs (unbounded), approved_artifacts (50 'approved' + 50 'integrated' = up to 100 rows with paths and review notes), bible.overview (unbounded), every canon entity with its summary (unbounded), 25 promoted feedback rows and notes — with only note_limit parameterised — while the tool docstring instructs every seat to read it BEFORE doing seat work. Six months in, the first call every agent makes is the biggest one and it is mandatory: context budget spent on 100 artifact rows the seat will never open and every canon entity when art needs three. Agents that hit a bloated brief start skipping it, which is the exact failure the brief exists to prevent.

**Fix:** Add sections: list[str] | None with per-section limits so a seat can request {mission, lanes, refs} without the canon dump; cap approved_artifacts to ~10 most recent per logical_name; return counts plus a pointer tool for the rest.

### 75. _model_for's docstring tells the reader to use a model the file bans

`bgate_adapters/imagegen.py:44` · **backend** · **minor** · effort **S** · raised by tech-art-lead

**Why:** The docstring reads 'the mode routes: transparent -> gpt-image-1, opaque -> gpt-image-2' while the constants directly above are DEFAULT_OPAQUE_MODEL = 'gpt-image-1' and DEFAULT_TRANSPARENT_MODEL = 'gpt-image-1', and the banner comment at line 19 says gpt-image-2 is BANNED by director directive. Both branches return gpt-image-1. A stale docstring on the money-spending adapter is how somebody 'fixes' the routing back to a banned model six months from now — the comment above explains exactly why that model broke sprite work and the docstring below tells them to use it.

**Fix:** Update the docstring to describe the actual single-model routing and point at the env overrides.

### 76. Per-pixel Python loops run inline on the same event loop everything else is blocked on

`bgate_mcp/server.py:1016` · **backend** · **nice-to-have** · effort **S** · raised by pipeline-eng

**Why:** _chroma_key iterates every pixel in Python (for y: for x: with a per-pixel sqrt) over the 1024x1536 output of each pose — ~1.6M iterations per frame, once per pose in image_sprites. _palette_hist iterates getdata() per image once per frame plus once for the ref on every gate pass; _alpha_flags does another full-image Python loop plus a flood fill. Seconds of pure CPU per frame for work Pillow/numpy do in milliseconds — not the reason a 20-pose run is slow, but the part that is slow for no reason, and it makes the loop-blocking problem measurably worse per frame.

**Fix:** Replace _chroma_key with a numpy distance mask (numpy is already a dependency of the record extra) or Image.point/ImageChops, and build histograms with Image.quantize/getcolors instead of a Python loop over getdata().

### 77. Four stacked theme layers in one 2762-line file, and the declared UI font never loads

`bgate_ui/static/index.html:972` · **frontend** · **nice-to-have** · effort **M** · raised by solo-indie

**Why:** index.html carries four full :root token blocks each overriding the last — the original foundry palette, a <style id='ux-rework'> block, a DALA block and a PLATFORM THEME block — roughly 1,180 lines of CSS before <body>, much of it styling markup that no longer exists (.bay, .masthead, .band, .lower, .workspace-tab). Three of the four declare --sans with 'Inter' first, but there is no @font-face and no external stylesheet is possible under COEP require-corp, so Inter silently falls back to Segoe UI. Not a blocker, but it is why the small stuff is broken everywhere else: nobody can tell which of four rules wins, so dead responsive CSS and orphaned classes accumulate, and the app was designed against Inter's metrics while rendering in Segoe UI.

**Fix:** Flatten to one token block, delete rules whose selectors have no markup, and either self-host Inter as a base64 @font-face or set --sans to what actually renders.

---

## Persona verdicts — would I actually use this?

### Maya — solo indie dev, ships small 2D Godot games to itch.io

There is a genuinely impressive machine in here — the playtest-to-telemetry join and the asset lock/drift model are things I'd actually pay for. But I cannot get to any of it without reading source. `python -m bgate_ui` prints literally nothing (no URL), the dashboard on a fresh machine paints nine empty nav items and the words "Offline · server 503", and the only way to create a project or scaffold a game is to have already registered an MCP server and gotten a Claude session to call `project_init` — which then doesn't even tell you which directory it wrote to. The single blocker: there is no first-run path. Everything in the shell assumes a project that already exists, built by someone who already knew the tool. Give me a "New project" button (or a `bgate init`/`bgate new` CLI) plus a startup line printing the URL and I'd give the rest a real weekend.

### Devon — technical art lead, 12-person studio, owns the Blender→Godot pipeline

The bones are better than I expected — pinned refs as durable artifacts, immutable revisions, an explicitly independent QA seat, and a real per-image price table are all things I've had to hand-build before. The iteration lab showing candidate-beside-its-reference is genuinely the right primitive. But I would not point this at a shipping build today, because the one thing I care about most is broken: `art_qa_verdict` with verdict='pass' calls `artifacts.review(..., 'approved')` directly, so an LLM reviewer promotes AI art to approved with zero human in the loop, and the workflow builder's "Consistency check" step is sold as a gate while nothing downstream reads its result. The single blocker is that there is no human-mandatory approval step and no visual diff — I get one image next to one reference at 260px and a prompt() box, which is not enough evidence to sign off on art that ships.

### Ravi — gameplay engineer (lives in the Godot editor + a terminal; cares about iteration speed, determinism, and seeing exactly what an agent changed)

The dispatch loop is genuinely further along than I expected — stream-json parsing into a readable feed, live steering with real consumption-latency tracking, orphan reaping, a run-boundary marker in the log. That's someone who has actually watched these agents wedge. But I cannot adopt it, and the blocker is singular: I can never see what an agent changed. There is no diff, anywhere. read_activity gives me a tool name and an 80-char file path hint and that is the entire audit trail; iterations.py hashes the git diff and throws the diff away. Second blocker right behind it: a failed item is a dead end in the UI — queue_reopen exists as an MCP tool but has no HTTP route, so from the dashboard a failure can only be stared at, never retried. Give me `git diff` per run boundary and a retry button and I'd run this on a real project; without them I'm letting an agent with unrestricted Bash edit my scripts and finding out later by reading the log.

### Sam — producer / creative director, small studio. Non-engineer. Thinks in milestones, scope cuts, and "is the game actually getting better?"

There's a real product buried in here — the agent cockpit and the delegate flow are the closest thing I've seen to a producer's seat over an agent fleet, and the iteration snapshot idea (goal → build fingerprint → playtest evidence) is the right causal model. But I can't drive it. The two things my job actually is — writing the design bible and holding the cut line — have no write path in the UI at all; the bible is a read-only chip list and the only way to author it is to ask an agent to call an MCP tool for me. And the workflow builder, the marquee "non-engineer builds the process" feature, has a dead field: the task-text box calls a function that never gets defined, so every workflow I build runs against "(no task text)". Single blocker: nothing lets a non-engineer author or enforce scope, so the cut line is decoration and the fleet will gold-plate exactly the way the docstring says it shouldn't.

### Priya — QA lead (4 shipped titles; repro steps, evidence, and never losing a bug report)

The bones are better than most in-house tools I've used: one clock joining transcript, frames, and telemetry is exactly the right idea, and the tuning-delta panel ("damage_scale 1.0 -> 0.75 @ 3:41") is genuinely something I'd screenshot into a bug. But it is a *feedback capture* tool, not a QA tool. I cannot scrub the video (the endpoint serves an mp4 with no Range support), I cannot type a repro step anywhere — playtest_item has no notes column and nothing in the UI collects one — and I cannot export a single bug report to hand to a programmer or attach to a ticket. The single blocker is that last one: every piece of evidence this thing captures dies inside a SQLite file and an overlay I can't share, so I'd still be writing the actual bug in a second tool by hand, and then what was the point.

### Chen — tools/backend engineer (internal studio tooling)

The domain modeling is genuinely good — the SQLite schema, the artifact revision table, and the activity ledger are what I'd have built. But the HTTP layer in front of it is not a contract, it's a pile of handlers, and that's the blocker: two mutually exclusive error conventions coexist (FastAPI `{detail}` at 4xx vs `200 {ok:false,error}`), so the frontend has given up entirely and wraps every fetch in `.catch(()=>({}))` — which doesn't even fire on a 500, because a 500 body is still valid JSON. Every failure in this product renders as a blank panel. I would not point my team at this until every endpoint returns one envelope with a machine-readable code and every list endpoint takes limit/offset. The rest — the N+1 in /api/state, no `busy_timeout` on a WAL DB with concurrent writers, blocking Godot calls with a caller-controlled timeout — I could fix in a week. The error contract is the thing that decides whether a UI can be honest.

### Jules — build/pipeline engineer (CI + engine integration)

The adapter layer is the most honest external-binary code I've read in a hobby-scale tool — stdin=DEVNULL because the MCP stdio channel gets eaten, the 0-byte-exe rejection, the CUDA probe that actually consumes the lazy generator, the chroma-key workaround for gpt-image punching holes in eyes. Somebody got burned and wrote it down. I'd use the Blender/Godot legs tomorrow. But I would not put this in a pipeline my team depends on yet, and the single blocker is that every one of ~70 tools is a blocking `def` on FastMCP's asyncio loop: `image_sprites` can hold that loop for 30 minutes of paid API calls while the dashboard, the queue, and every other seat's tool call sit dead behind it — which is exactly the failure `transcribe.py`'s docstring says the design exists to avoid. Second-order but close behind: it can't be distributed. `templates/` isn't package data and `static/*.js` isn't either, so `pip install builders-gate` yields a scaffolder with no templates and a dashboard with no JavaScript, and there's no CI to have caught it.

### Alex — CTO, 30-person studio, evaluating agentic game dev

The domain thinking here is the best I have seen in this space — the cut line, canon facts vs prose, "assets lock, they don't merge," verifying the glTF tri count inside a real headless Godot rather than on disk, and the playtest voice-to-telemetry join are all things a person who has actually shipped a game would invent. I would happily run this on a 2-person game jam prototype tomorrow. I cannot put it near our shipping repo, and the blocker is one thing: there is no version control in the trust model. Agents are spawned with `--permission-mode acceptEdits` and Bash allowed, straight into the live working tree, with no branch, no commit, no diff review, and no revert path — and the lane/lock hook that is supposed to be "the teeth" only inspects Write/Edit/MultiEdit, so a single `Bash` call walks around every gate the README sells. Add that agent spend is read from the result event and thrown away, the QA gate auto-dispatches unbounded fix rounds, and SQLite is opened with no `busy_timeout` while 8 agent processes write it, and week one of a studio trial ends with a locked DB, a surprise invoice, and a working tree nobody can bisect. Fix git isolation + a spend ceiling + Bash-aware enforcement and I would run a real pilot; without them this is a brilliant single-player tool wearing a studio's clothes.

