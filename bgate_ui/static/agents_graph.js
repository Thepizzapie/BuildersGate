/* agents_graph.js — the live delegation graph on the Agents console.
 *
 * The board this replaces could show you five lanes of cards and never once
 * show you that item #38 exists BECAUSE you asked for a desk-clutter set, or
 * that #41 is sitting behind a QA gate nobody opened. Those relations were all
 * in the database (work_item.source/source_ref, the DELEGATED-FROM line, the
 * qa-gate rows) and nowhere on screen.
 *
 * So: one tree, one incoming edge per node, columns left to right.
 *
 *     you said …   →   seat   →   task   →   handoff   →   gate
 *
 * A task whose parent is another task hangs off the PARENT rather than its
 * seat, which is what makes a handoff visible as a handoff.
 *
 * A gate hangs off work that is STILL IN FLIGHT. A candidate from a run that
 * finished last week is a review backlog (Assets owns that), not something the
 * floor is blocked on, and hanging it here made the graph look permanently
 * jammed. The server filters that; this file just draws what it is sent.
 *
 * Everything else here is upkeep: patch nodes in place so a drag survives a
 * poll, keep positions in the project's workspace doc, and never throw — the
 * graph is a reading of the floor, and a reading that crashes takes the console
 * with it.
 *
 * Data comes from /api/console/state, polled by agents_console.js and pushed in
 * through apply(). This module makes no requests of its own except the ones a
 * button press causes.
 */
(function () {
  "use strict";

  const SEATS = ["director", "narrative", "gameplay", "tech", "art", "audio", "qa"];
  const WS_PATH = "/api/workspace/director/console-graph";

  // x is the left edge of a node; the canvas pans, so this is only the FIRST
  // layout — a node the user drags keeps whatever it was given. Depth 1 tasks
  // sit at COL.task, each handoff a STEP further right.
  const COL = { turn: 30, seat: 350, task: 620, step: 300 };
  const ROW = { turn: 122, seat: 96, task: 104, phase: 86 };
  // PHASES ARE COLLAPSED UNLESS YOU ARE LOOKING AT THAT RUN.
  //
  // Three agents, eight phases each, is twenty-seven nodes in three columns that
  // reserved room for three — every stack grew down through the task below it and
  // the canvas auto-fitted to 49%, which is a picture of a mess rather than a
  // mess you can read. A run's phases are detail about ONE run; the graph's job
  // with three live is to show three runs. So the stack opens for the selected
  // task (and for a lone runner, where there is nothing to crowd), and every
  // other task carries its phase count on the node instead.
  //
  // From the settings registry (graph.phase_cap) via the page bootstrap; the
  // literal is the fallback for a page with no bootstrap, and it is clamped
  // because a stored 0 would draw no phase rows at all and read as a broken
  // graph rather than as a setting.
  const PHASE_CAP = (() => {
    const raw = Number((window.BGATE_SETTINGS || {}).phase_cap);
    return Number.isFinite(raw) && raw > 0 ? Math.max(1, Math.min(raw, 50)) : 6;
  })();
  // Every node lands to the RIGHT of what spawned it (that is the column) and a
  // little BELOW it (this). Levelling a child with its parent made a wide run
  // read as one flat row where the causal direction was carried only by the
  // edges; a staircase carries it in the layout, so the newest work is always
  // down-and-right of the thing that caused it.
  const DROP = 46;

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const AUDIO = /\.(wav|mp3|ogg|flac|m4a)$/i;
  const IMAGE = /\.(png|jpe?g|webp|gif|svg)$/i;

  /* What an agent produced, playable/viewable in place. A file path is not
     evidence — the thing the phase MADE is, and until this the only way to see
     it was to guess which asset it was and go find it in the library. */
  /* `item` scopes the image to a run's worktree. A file an isolated agent is
     editing does not exist at the project root yet, so a thumbnail built
     without it 404s and paints an empty bordered box — the console says the run
     is looking at something and then shows nothing. The peek link right next to
     these already passed item_id; the <img> did not. */
  function thumb(art, cls, item) {
    const rel = String((art && art.path) || "");
    if (!rel) return "";
    const scope = item ? `&item_id=${Number(item)}` : "";
    if (IMAGE.test(rel)) {
      const frames = ((art.metadata || {}).frames) || {};
      // Only the rail plays sheets: SpriteAnim has to be mounted after render
      // and NodeCanvas patches node bodies on its own schedule, so a player
      // inside a node would be re-created out from under its own timer.
      const count = cls.indexOf("cg-thumb") >= 0 ? Object.keys(frames).length : 0;
      return count > 1
        ? `<div class="${cls} spr-mount" data-rel="${esc(rel)}" data-count="${count}"
             data-item="${Number(item) || 0}"
             data-fps="8" title="${esc(art.logical_name || rel)} · ${count}f"></div>`
        : `<img class="${cls}" src="/api/preview?rel=${encodeURIComponent(rel)}${scope}"
             title="${esc(art.logical_name || rel)}" alt="">`;
    }
    if (AUDIO.test(rel)) {
      return `<audio class="${cls} cg-audio" controls preload="none"
                src="/api/audio/file?rel=${encodeURIComponent(rel)}"
                title="${esc(art.logical_name || rel)}"></audio>`;
    }
    return `<span class="${cls} cg-file" title="${esc(rel)}">${esc(
      rel.split(/[\\/]/).pop())}</span>`;
  }
  /* A grid of files the run touched, every one of them openable.
   *
   * The three sections it serves — looked at, read, made — were three different
   * shapes rendering the same thing, and none of them was clickable: a thumbnail
   * you cannot enlarge and a filename you cannot open are decoration. One tile,
   * one data-peek attribute, and peek.js does the rest through a single
   * delegated listener (these tiles are re-rendered on every poll, so per-tile
   * handlers would need re-binding every three seconds).
   */
  function fileGrid(entries, itemId, artifacts) {
    const run = itemId ? ` data-peek-item="${Number(itemId)}"` : "";
    return `<div class="cg-made">${(entries || []).map(a => {
      const rel = String((a && a.path) || "").replace(/\\/g, "/");
      if (!rel) return "";
      const label = artifacts ? (a.logical_name || rel.split("/").pop())
                              : rel.split("/").pop();
      const sub = artifacts
        ? `${esc(a.status || "")}${a.revision ? " · r" + a.revision : ""}`
        : esc(rel);
      return `<div class="cg-madeone open" role="button" tabindex="0"
                   data-peek="${esc(rel)}"${run} title="${esc(rel)}">
                ${thumb(a, "cg-thumb")}
                <div class="cg-madelabel">${esc(label)}<span>${sub}</span></div>
              </div>`;
    }).join("")}</div>`;
  }

  /* One row of the activity feed. A step that looked at a picture SHOWS the
     picture, and the narration straight after it is marked as what the agent
     concluded from looking — being told an agent "read anchor_vfx.r2.png" and
     then that it is "fixing the cut" is two facts with the evidence missing
     between them. */
  function stepRow(s, itemId) {
    const k = s.kind === "tool" ? "tool" : s.kind === "steer" ? "steer"
      : s.kind === "result" ? "res" : "say";
    const txt = s.kind === "tool"
      ? `<b>${esc(s.name || "tool")}</b> ${esc(trunc(s.hint || "", 110))}`
      : esc(trunc(s.text || "", 400));
    const run = itemId ? ` data-peek-item="${Number(itemId)}"` : "";
    // A picture opens IN the page now. It used to open a raw image in a new tab,
    // which loses the diff, the size and the run it belongs to — and costs you
    // the console you were watching.
    // target=_blank stays as the fallback: peek.js calls preventDefault, so the
    // viewer wins whenever it is loaded, and the link still opens the image
    // rather than navigating the whole console away when it is not.
    const shots = (s.images || []).map(rel =>
      `<a class="cg-eye" href="/api/preview?rel=${encodeURIComponent(rel)}"
          target="_blank" rel="noopener" data-peek="${esc(rel)}"${run}
          title="${esc(rel)}">${thumb({ path: rel }, "cg-shot-in")}</a>`).join("");
    // The files it NAMED. The log always had these paths; it had them in prose,
    // 90 characters wide, in the middle of a sentence you could read and not open.
    const files = (s.files || []).map(rel =>
      `<button class="cg-fchip" type="button" data-peek="${esc(rel)}"${run}
               title="${esc(rel)}">${esc(rel.split("/").pop())}</button>`).join("");
    return `<div class="cg-step k-${k}${s.analysis ? " analysis" : ""}">`
      + (s.analysis ? `<span class="cg-tag">what it sees</span>` : "")
      + txt
      + (files ? `<div class="cg-fchips">${files}</div>` : "")
      + (shots ? `<div class="cg-shots">${shots}</div>` : "")
      + `</div>`;
  }

  const seatColor = s => `var(--c-${SEATS.includes(s) ? s : "tech"})`;
  const trunc = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };

  /* The one line that says what an agent is doing right now. A tool call is
     the interesting event — "reading the log" is not a decision, "generating
     hero_idle against ref hero_v3" is. */
  function lastStep(steps) {
    const list = steps || [];
    for (let i = list.length - 1; i >= 0; i--) {
      const s = list[i];
      if (s.kind === "tool") return { k: "tool", t: s.name || "tool", h: s.hint || "" };
      if (s.kind === "steer") return { k: "steer", t: "steered", h: s.text || "" };
      if (s.kind === "result") return { k: "res", t: "", h: s.text || "" };
      if (s.kind === "say" && s.text) return { k: "say", t: "", h: s.text };
    }
    return null;
  }

  const AgentsGraph = {
    nc: null,
    host: null,
    state: null,
    nodes: new Map(),
    edges: [],
    sel: null,
    positions: {},
    filter: "active",
    _sig: "",
    _saveT: null,
    _fitted: false,
    _detail: null,
    _down: null,

    mount(host, detail) {
      this.host = host;
      this._detail = detail || null;
      try { this.filter = localStorage.getItem("bgate-graph-filter") || "active"; }
      catch (e) { this.filter = "active"; }
      // CLICK, NOT DRAG. NodeCanvas reports a selection on pointer DOWN, which
      // is correct for the canvas (a node has to highlight the instant you grab
      // it) and wrong for a panel that covers a third of the graph: reaching
      // for a node to move it slammed the rail open every time. So the rail
      // opens on a clean click and a drag only changes the highlight.
      host.addEventListener("pointerdown", e => {
        this._down = { x: e.clientX, y: e.clientY, open: this.railOpen() };
      }, true);
      host.addEventListener("pointerup", e => {
        const d = this._down; this._down = null;
        if (!d) return;
        const moved = Math.abs(e.clientX - d.x) > 4 || Math.abs(e.clientY - d.y) > 4;
        if (moved) return;                       // a drag: leave the rail alone
        this.renderDetail();
      }, true);
      this.loadPositions();
      return this;
    },

    railOpen() {
      return !!(this._detail && this._detail.classList.contains("open"));
    },

    setFilter(mode) {
      this.filter = mode === "all" ? "all" : "active";
      try { localStorage.setItem("bgate-graph-filter", this.filter); } catch (e) {}
      this._sig = "";
      this.nodes = new Map();
      this.rebuild();
      this.fit();
      return this.filter;
    },

    /* ---- data → nodes -------------------------------------------------- */
    apply(state) {
      if (!state || !this.host) return;
      this.state = state;
      try { this.rebuild(); } catch (e) { try { console.warn("[agents-graph]", e); } catch (_) {} }
      // After the rebuild: the node has to exist in the DOM before it can be lit.
      try { this.spotlight(this.liveSet()); } catch (e) {}
    },

    liveSet() {
      const s = this.state || {};
      return new Set((s.agents || [])
        .filter(a => a && a.state === "running" && a.item_id != null)
        .map(a => Number(a.item_id)));
    },

    /* ---- the spotlight ──────────────────────────────────────────────────
     * A NEW AGENT STARTING LOOKED EXACTLY LIKE ONE THAT HAD BEEN RUNNING FOR
     * TEN MINUTES. The graph is a static diagram between polls: nodes gain a
     * border treatment and a glyph, both of which you have to already be
     * looking at the right node to notice. On a stream nobody is — the viewer
     * is reading the conversation, and the moment work gets handed to a seat is
     * the moment they should look right.
     *
     * So a node that has JUST started blooms, and the rest of the canvas dims
     * behind it for a beat. Deliberately short and deliberately once: a
     * permanent highlight is just another border, and dimming that outstays the
     * event makes the board unreadable exactly when several things are running.
     *
     * Seeded on the first apply() so opening the view does not spotlight every
     * agent already at work — "already there" is not an event.
     */
    _lit: null, _spotTimer: 0,

    spotlight(live) {
      const before = this._lit;
      this._lit = new Set(live);
      if (!before) return;                       // first payload seeds only
      const fresh = [...live].filter(id => !before.has(id));
      if (!fresh.length || !this.host) return;
      // One at a time. Two nodes blooming while the canvas dims reads as a
      // flicker rather than as a pointer, and a fan-out starts four at once.
      const id = "task_" + fresh[0];
      const el = this.host.querySelector(`[data-node="${CSS.escape(id)}"]`);
      if (!el) return;
      this.host.classList.add("nc-spotting");
      el.classList.add("nc-spot");
      clearTimeout(this._spotTimer);
      this._spotTimer = setTimeout(() => {
        this.host.classList.remove("nc-spotting");
        this.host.querySelectorAll(".nc-spot").forEach(n => n.classList.remove("nc-spot"));
      }, 1800);
    },

    place(id, x, y) {
      const prev = this.nodes.get(id);
      if (prev && Number.isFinite(prev.x)) return { x: prev.x, y: prev.y };
      const saved = this.positions[id];
      if (saved && Number.isFinite(saved.x)) return { x: saved.x, y: saved.y };
      return { x, y };
    },

    /* Which work belongs on the graph.
     *
     * DEPLOYED WORK ONLY. A queued item is a plan, and a plan on the canvas is
     * indistinguishable from work in progress — that is what made this thing
     * read as a backlog. The queue lives in the console's own panel, where
     * deploying it is one button; crossing that line is what puts a task here.
     *
     * The exception is a task holding a gate. Its run is over, but the gate has
     * to hang off something: "approve this" with no idea what "this" was is not
     * a decision anyone can make. It leaves the graph the moment you act on it.
     *
     * Ancestors come back whatever the filter says, because a chain with its
     * middle removed is a lie about who caused what. */
    keep(items, live) {
      if (this.filter === "all") return items.slice(0, 60);
      const byId = new Map(items.map(i => [Number(i.id), i]));
      const parents = ((this.state || {}).lineage || {}).parents || {};
      const gated = new Set(((this.state || {}).gates || [])
        .map(g => Number(g.over_item_id || 0)).filter(Boolean));
      const keep = new Set();
      items.forEach(i => {
        const id = Number(i.id);
        if (live.has(id) || i.status === "dispatched" || gated.has(id)) keep.add(id);
      });
      // Walk up: a kept task's ancestors stay so the tree keeps its trunk.
      [...keep].forEach(id => {
        let up = Number(parents[id] || parents[String(id)] || 0), guard = 0;
        while (up && byId.has(up) && !keep.has(up) && guard++ < 12) {
          keep.add(up);
          up = Number(parents[up] || parents[String(up)] || 0);
        }
      });
      return items.filter(i => keep.has(Number(i.id)));
    },

    compute() {
      const s = this.state || {};
      const live = this.liveSet();
      const steps = s.steps || {};
      const parents = (s.lineage && s.lineage.parents) || {};
      // A turn that never dispatched is a message, not work: it belongs in the
      // transcript and in the queue panel, where deploying it is one button.
      // Drawing it here put an undeployed thing on a canvas whose whole rule is
      // that everything on it is live — which is exactly how a cancelled
      // dispatch ended up looking like it had started.
      const turns = (s.turns || []).filter(t => t.status !== "queued").slice(-8);
      const turnIds = new Set(turns.map(t => Number(t.id)));
      const items = this.keep((s.items || []).filter(i => i.source !== "chat"), live);
      const byId = new Map(items.map(i => [Number(i.id), i]));

      const nodes = new Map();
      const edges = [];
      const add = n => nodes.set(n.id, n);
      const IN = [{ id: "i", label: "" }], OUT = [{ id: "o", label: "" }];

      const parentOf = id => Number(parents[id] || parents[String(id)] || 0);

      // Depth: a turn's child is depth 1, its child is 2, and so on. A task
      // whose parent fell outside the window starts a trunk of its own.
      const depthOf = (id, guard) => {
        const p = parentOf(id);
        if (!p || (guard || 0) > 12) return 1;
        if (turnIds.has(p)) return 1;
        if (!byId.has(p)) return 1;
        return 1 + depthOf(p, (guard || 0) + 1);
      };

      // Children first, so a run reads top-to-bottom as it was delegated.
      const kids = new Map();
      items.forEach(i => {
        const p = parentOf(i.id);
        const key = byId.has(p) ? p : (turnIds.has(p) ? "turn_" + p : "_root");
        (kids.get(key) || kids.set(key, []).get(key)).push(i);
      });
      const cursor = {};
      const nextY = (col, want) => {
        const y = Math.max(cursor[col] || 20, want || 0);
        cursor[col] = y + ROW.task;
        return y;
      };

      // WHOSE PHASES ARE OPEN, decided before anything is placed — the stack has
      // to be reserved for at the moment its task takes a row, or the next task
      // in that column lands inside it.
      const phaseMap = s.phases || {};
      const withPhases = Object.keys(phaseMap)
        .filter(k => (phaseMap[k] || []).length
          && (live.has(Number(k)) || byId.has(Number(k))))
        .map(Number);
      // A selected PHASE counts as selecting its task — otherwise opening a
      // pocket collapses the stack it lives in and the rail shuts on itself.
      const sel = String(this.sel || "");
      const selectedItem = sel.startsWith("task_") ? Number(sel.slice(5))
        : sel.startsWith("phase_") ? Number(sel.split("_")[1]) : 0;
      const openFor = withPhases.length <= 1 ? new Set(withPhases)
        : new Set(withPhases.filter(id => id === selectedItem));
      const phaseRows = id => (openFor.has(Number(id))
        ? Math.min((phaseMap[String(id)] || []).length, PHASE_CAP) : 0);

      // ── what you said
      turns.forEach((t, i) => {
        const id = "turn_" + t.id;
        const p = this.place(id, COL.turn, 20 + i * ROW.turn);
        add({
          id, type: "turn", turn: t, title: trunc(t.said || t.title, 42),
          glyph: "»", w: 258, x: p.x, y: p.y,
          accent: "var(--accent)",
          badge: t.reply && t.reply.running ? "thinking"
            : (t.status === "done" ? "answered" : t.status),
          running: !!(t.reply && t.reply.running),
          ports: { out: OUT },
        });
      });

      // ── the floor. Only seats that are ON something: seven permanent boxes,
      // five of them idle, is a floor plan — this is a picture of what is
      // happening, and an idle seat is not happening.
      const counts = {};
      items.forEach(i => {
        const c = counts[i.seat] || (counts[i.seat] = { queued: 0, running: 0, done: 0 });
        if (live.has(Number(i.id))) c.running++;
        else if (i.status === "queued") c.queued++;
        if (i.status === "done") c.done++;
      });
      const working = SEATS.filter(s => counts[s]);
      working.forEach((seat, i) => {
        const id = "seat_" + seat;
        const p = this.place(id, COL.seat, 20 + i * ROW.seat);
        const c = counts[seat] || { queued: 0, running: 0, done: 0 };
        add({
          id, type: "seat", seat, title: seat.toUpperCase(), glyph: "▪",
          w: 206, x: p.x, y: p.y, accent: seatColor(seat), counts: c,
          badge: c.running ? "live" : "", running: !!c.running,
          // The seat boxes double as the canvas's colour key — this hue is that
          // seat, everywhere, for the rest of the graph.
          status: c.running ? "running" : "",
          ports: { in: IN, out: OUT },
        });
      });

      // ── the work, laid out depth-first from each root
      const laid = new Set();
      const layTask = (it, wantY) => {
        const id = "task_" + it.id;
        if (laid.has(id)) return;
        laid.add(id);
        const running = live.has(Number(it.id));
        const depth = depthOf(it.id);
        const col = COL.task + (depth - 1) * COL.step;
        const p = this.place(id, col, nextY(col, wantY));
        // Reserve the band its phase stack will occupy — in this column, so the
        // next sibling clears it, and in the phase column, so a CHILD task laid
        // there later does not land on top of the stack. This is the whole bug
        // behind the pile-up: the stack was drawn after the fact and never
        // claimed the space it took.
        const rows = phaseRows(it.id);
        if (rows) {
          const band = p.y + rows * ROW.phase + DROP;
          cursor[col] = Math.max(cursor[col] || 0, band);
          cursor[col + COL.step] = Math.max(cursor[col + COL.step] || 20, band);
        }
        const stack = Math.min((phaseMap[String(it.id)] || []).length, 99);
        add({
          id, type: "task", item: it, running, seat: it.seat,
          title: trunc(it.title, 40),
          glyph: running ? "▶" : it.status === "done" ? "✓"
            : it.status === "failed" ? "×" : "▷",
          w: 268, x: p.x, y: p.y, phases: stack, phasesOpen: !!rows,
          // HUE IS WHOSE, NOT WHAT STATE. A task wears its seat's colour for its
          // whole life; running/done/failed is carried by the border treatment
          // and the badge (see data-status). Painting a running node accent-
          // orange and a finished one green meant the canvas told you the status
          // of everything and the owner of nothing — which is backwards, because
          // the status is already written on the node in words.
          accent: it.status === "failed" ? "var(--bad)" : seatColor(it.seat),
          status: running ? "running" : it.status === "failed" ? "failed"
            : it.status === "done" ? "passed" : "",
          // A collapsed stack says so on the node, so "where did its steps go"
          // has an answer you can see instead of a feature that looks broken.
          badge: (!rows && stack) ? `${stack} phase${stack === 1 ? "" : "s"}`
            : running ? "running" : it.status,
          step: running ? lastStep(steps[String(it.id)]) : null,
          cost: it.total_cost_usd ? "$" + Number(it.total_cost_usd).toFixed(2) : "",
          ports: { in: IN, out: OUT },
        });
        // ONE incoming edge: the thing that caused this task. A handoff comes
        // from the parent task; everything else comes from its seat.
        const parent = parentOf(it.id);
        if (byId.has(parent)) {
          edges.push({ from: ["task_" + parent, "o"], to: [id, "i"] });
        } else {
          edges.push({ from: ["seat_" + it.seat, "o"], to: [id, "i"] });
          if (turnIds.has(parent)) {
            edges.push({ from: ["turn_" + parent, "o"], to: ["seat_" + it.seat, "i"] });
          }
        }
        (kids.get(Number(it.id)) || []).forEach(kid => layTask(kid, p.y + DROP));
      };
      turns.forEach(t => (kids.get("turn_" + t.id) || []).forEach(
        kid => layTask(kid, ((nodes.get("turn_" + t.id) || {}).y || 20) + DROP)));
      (kids.get("_root") || []).forEach(it => layTask(it));
      items.forEach(it => layTask(it));   // anything the walk missed

      // A turn that has not delegated anything yet still shows where it went.
      turns.forEach(t => {
        if (t.status !== "dispatched" && t.status !== "queued") return;
        if (edges.some(e => e.from[0] === "turn_" + t.id)) return;
        edges.push({ from: ["turn_" + t.id, "o"], to: ["seat_director", "i"] });
      });

      // ── the pockets of work inside each running agent.
      // A run is not one action: the agent says what it is about to do, does it,
      // says the next thing. Those are the units you actually want to open —
      // this phase produced these three renders, that one is where it went
      // wrong — and they exist only while the agent is working.
      const phases = s.phases || {};
      Object.keys(phases).forEach(itemId => {
        const anchor = nodes.get("task_" + itemId);
        if (!anchor) return;
        // Collapsed: the count is on the task node and the rail still lists every
        // phase. Select the task to open the stack.
        if (!openFor.has(Number(itemId))) return;
        const list = (phases[itemId] || []).slice(-PHASE_CAP);
        let prev = null;
        list.forEach((ph, i) => {
          const id = `phase_${itemId}_${ph.n}`;
          const col = anchor.x + COL.step;
          // Aligned with its task rather than dropped below it: the band that was
          // reserved starts at the anchor's row, and a stack that starts lower
          // than the space claimed for it is a stack that runs out the bottom.
          const p = this.place(id, col, (anchor.y || 20) + i * ROW.phase);
          const arts = ph.artifacts || [];
          add({
            id, type: "phase", phase: ph, itemId: Number(itemId),
            title: trunc(`${ph.n} · ${ph.title}`, 40),
            glyph: ph.state === "running" ? "▶" : ph.state === "trouble" ? "!" : "✓",
            // Narrower than its task and in its task's colour: a phase is part
            // OF a run, and a stack of full-width cards in a fourth colour read
            // as five more agents rather than one agent's five pockets.
            w: 226, x: p.x, y: p.y, seat: anchor.seat,
            accent: ph.state === "trouble" ? "var(--bad)" : seatColor(anchor.seat),
            badge: arts.length ? `${arts.length} made` : "",
            running: ph.state === "running",
            status: ph.state === "running" ? "running"
              : ph.state === "trouble" ? "failed" : "passed",
            ports: { in: IN, out: OUT },
          });
          edges.push(prev
            ? { from: [prev, "o"], to: [id, "i"] }
            : { from: ["task_" + itemId, "o"], to: [id, "i"] });
          prev = id;
        });
      });

      // ── sideways relations: two agents on one thing, one blocked on another,
      // one steering another. Dashed, because these are not delegations.
      (s.collab || []).forEach(c => {
        const a = "task_" + c.a, b = "task_" + c.b;
        if (!nodes.has(a) || !nodes.has(b)) return;
        edges.push({
          from: [a, "o"], to: [b, "i"], cls: "nc-soft",
          color: c.kind === "blocked" ? "var(--bad)"
            : c.kind === "steer" ? "var(--accent)" : "var(--info)",
          title: c.label || c.kind,
        });
      });

      // ── what the in-flight work cannot get past on its own
      (s.gates || []).slice(0, 12).forEach(g => {
        const id = "gate_" + g.id;
        const over = g.over_item_id;
        const anchor = over ? nodes.get("task_" + over) : null;
        // Past the phase column only when that stack is actually OPEN — a gate
        // shoved two columns right of a collapsed task is a gate nobody scrolls to.
        const col = anchor ? anchor.x + COL.step * (openFor.has(Number(over)) ? 2 : 1)
          : COL.task + COL.step * 2;
        const p = this.place(id, col, nextY(col, anchor ? anchor.y + DROP : 0));
        add({
          id, type: "gate", gate: g, title: trunc(g.title, 34),
          glyph: g.kind === "art" ? "◇" : "!", w: 236, x: p.x, y: p.y,
          accent: g.kind === "escalation" ? "var(--bad)" : "var(--spark)",
          // A gate is the one node that is NOT a seat's work — it is the board
          // waiting on a person, so it keeps its own colour and gets the dashed
          // outline that means "stopped here".
          status: "",
          badge: g.kind === "art" ? "approval"
            : g.kind === "escalation" ? "escalated" : "qa gate",
          ports: { in: IN },
        });
        if (anchor) edges.push({ from: ["task_" + over, "o"], to: [id, "i"] });
      });

      // Drop edges whose endpoints fell outside the window — a dangling edge
      // draws from nowhere and reads as a bug in the data.
      return { nodes, edges: edges.filter(e => nodes.has(e.from[0]) && nodes.has(e.to[0])) };
    },

    /* ---- render -------------------------------------------------------- */
    rebuild() {
      const next = this.compute();
      const sig = [...next.nodes.keys()].sort().join("|")
        + "#" + next.edges.map(e => e.from[0] + ">" + e.to[0]).join(",");
      if (!this.nc) {
        this.nodes = next.nodes;
        this.edges = next.edges;
        this._sig = sig;
        this.nc = new window.NodeCanvas(this.host, {
          nodes: [...next.nodes.values()],
          edges: next.edges,
          accent: "var(--accent)",
          renderBody: n => this.body(n),
          onSelect: n => this.onSelect(n),
          onNodeMove: n => this.onMove(n),
        });
        this.nc.mount();
        this.renderDetail();
        return;
      }
      if (sig !== this._sig) {
        this.nodes = next.nodes;
        this.edges = next.edges;
        this._sig = sig;
        this.nc.setNodes([...next.nodes.values()], next.edges);
        if (this.sel && next.nodes.has(this.sel)) this.nc.select(this.sel);
        else if (this.sel) {
          // Clear it on the CANVAS too. select() early-returns on an unchanged
          // id, so a node that dropped out and came back could never be picked
          // again — the rail simply would not open for it.
          this.sel = null;
          try { this.nc.select(null); } catch (e) {}
        }
        // Only repaint an open rail. A poll that reopens it undoes the whole
        // click-not-drag rule: NodeCanvas selects on pointer DOWN, so grabbing
        // a node to move it sets `sel` with the rail shut, and three seconds
        // later the panel slammed open over the graph.
        if (this.railOpen()) this.renderDetail(true);
        return;
      }
      // Same shape — patch the nodes that changed so a drag, a scroll and a
      // half-typed steer all survive the poll.
      const changed = [];
      for (const [id, fresh] of next.nodes) {
        const cur = this.nodes.get(id);
        if (!cur) continue;
        // `status` is in both lists deliberately. It drives the pulse and the
        // finished-work fade, and a field that is signed but never copied (or
        // copied but never signed) is a node that keeps painting its old state
        // until the graph's SHAPE happens to change — a finished agent that
        // pulses for another ten minutes.
        const sigOf = n => JSON.stringify([n.badge, n.accent, n.glyph, n.cost,
                                           n.counts, n.step, n.title, n.status,
                                           n.seat, n.phasesOpen,
                                           n.phase && n.phase.state,
                                           n.phase && (n.phase.artifacts || []).length]);
        const before = sigOf(cur);
        Object.assign(cur, {
          item: fresh.item, turn: fresh.turn, gate: fresh.gate, counts: fresh.counts,
          phase: fresh.phase, badge: fresh.badge, accent: fresh.accent,
          glyph: fresh.glyph, running: fresh.running, step: fresh.step,
          cost: fresh.cost, title: fresh.title, status: fresh.status,
          seat: fresh.seat, phases: fresh.phases, phasesOpen: fresh.phasesOpen,
        });
        const after = sigOf(cur);
        if (before !== after) changed.push(cur);
      }
      // ONE edge pass for the whole batch. addNode() re-renders every edge, and
      // every edge measures both its ports with getBoundingClientRect — with a
      // dozen nodes changing per poll that was thousands of forced layouts
      // every three seconds, on a canvas the user is trying to drag.
      if (changed.length) { try { this.nc.patchNodes(changed); } catch (e) {} }
      if (this.railOpen()) this.renderDetail(true);
    },

    body(n) {
      if (n.type === "turn") {
        const r = (n.turn && n.turn.reply) || {};
        const line = r.running ? (r.thinking || "thinking…") : (r.text || "no answer yet");
        return `<div class="cg-said">${esc(trunc(n.turn.said || "", 150))}</div>
          <div class="cg-line ${r.running ? "live" : ""}">${esc(trunc(line, 120))}</div>`;
      }
      if (n.type === "seat") {
        const c = n.counts || {};
        return `<div class="cg-meta">
          <span class="${c.running ? "on" : "off"}">${c.running ? "● " + c.running + " working" : "idle"}</span>
          <span>${c.queued || 0} queued</span></div>`;
      }
      if (n.type === "phase") {
        const ph = n.phase || {};
        const arts = ph.artifacts || [];
        // Made first, then what it is looking at — the node is small, and the
        // strip is a glance, not an inventory.
        const strip = arts.slice(0, 4).map(a => thumb(a, "cg-mini"))
          .concat((ph.seen || []).slice(0, 4 - Math.min(4, arts.length))
            .map(rel => thumb({ path: rel }, "cg-mini seen"))).join("");
        // WHOSE POCKET THIS IS, in words as well as in hue. Colour alone fails
        // the two people who need it most — anyone who cannot separate pink from
        // red, and anyone at 40% zoom on a canvas with three runs on it.
        return `<div class="cg-meta">
            ${n.seat ? `<span class="cg-owner" style="color:${seatColor(n.seat)}">${
              esc(n.seat)} · #${Number(n.itemId)}</span>` : ""}
            <span>${(ph.tools || []).length} tools</span>
            <span>${ph.results || 0} results</span>
            ${(ph.seen || []).length ? `<span>${ph.seen.length} seen</span>` : ""}
            ${(ph.read || []).length ? `<span>${ph.read.length} files</span>` : ""}
            ${ph.steers ? `<span class="cg-warn">${ph.steers} steer</span>` : ""}
          </div>
          ${strip ? `<div class="cg-strip">${strip}</div>` : ""}
          <div class="cg-line ${ph.state === "running" ? "live" : ""}">${
            ph.error ? esc(trunc(ph.error, 90))
              : esc((ph.tools || []).slice(0, 4).join(" · ") || "…")}</div>`;
      }
      if (n.type === "gate") {
        const g = n.gate || {};
        // A parked item is not a claim worth a glance, it is a stopped chain.
        // Both used to read "your call", which made the one that is actually
        // holding work up indistinguishable from the ten that are not.
        const what = g.kind === "art" ? "a human decides — approve or reject"
          : g.kind === "signoff" ? (g.parked
              ? "held in review — the chain behind it waits on you"
              : "the agent says this is done — your call")
          : g.kind === "escalation" ? "QA loop broken — you arbitrate"
          : "verifying the claim before it counts";
        return `<div class="cg-meta"><span>${esc(g.seat || "")}</span>
          <span>${esc(g.status || "")}</span></div>
          <div class="cg-line">${esc(what)}</div>`;
      }
      const it = n.item || {};
      const step = n.step;
      const line = step
        ? (step.k === "tool" ? `<b>${esc(step.t)}</b> ${esc(trunc(step.h, 60))}`
          : step.k === "steer" ? `steered · ${esc(trunc(step.h, 60))}`
          : esc(trunc(step.h, 80)))
        : esc(trunc(it.result || it.brief_preview || "", 80));
      // The seat rides on the task now that it has no node of its own.
      return `<div class="cg-meta">
          <span class="cg-chip" style="--sc:${seatColor(it.seat)}">${esc(it.seat || "")}</span>
          <span>#${esc(it.id)}</span>
          <span>${esc(it.source || "")}</span></div>
        <div class="cg-line ${n.running ? "live" : ""}">${line || "—"}</div>`;
    },

    /* ---- selection + the detail rail ----------------------------------- */
    onSelect(n) {
      this.sel = n ? n.id : null;
      // Only repaint an ALREADY-open rail here; opening it is the pointerup
      // handler's job, because this fires on pointer down (see mount).
      if (this.railOpen() || !n) this.renderDetail();
    },

    select(id) {
      if (!this.nc || !this.nodes.has(id)) return false;
      this.sel = id;
      this.nc.select(id);
      this.renderDetail();
      return true;
    },

    renderDetail(quiet) {
      const box = this._detail;
      if (!box) return;
      const n = this.sel ? this.nodes.get(this.sel) : null;
      if (!n) {
        box.classList.remove("open");
        box.innerHTML = "";
        this._detailHTML = "";
        return;
      }
      box.classList.add("open");
      // A HALF-TYPED STEER OUTLIVES EVERY REPAINT, unconditionally.
      //
      // This used to preserve the text only when the caller passed `quiet`, and
      // the caller that fires during a live run (a new phase pocket changes the
      // node set every few seconds) did not — so the sentence you were typing
      // into a working agent vanished mid-word. Preserving the VALUE was also
      // not enough on its own: the element is replaced, so focus and the caret
      // went with it and the next keystrokes landed nowhere.
      const old = box.querySelector(".cg-steer-in");
      const held = old ? {
        value: old.value,
        focused: document.activeElement === old,
        start: old.selectionStart,
        end: old.selectionEnd,
      } : null;
      const scroll = box.scrollTop;
      const html = this.detailHTML(n);
      // Nothing changed: leave the DOM (and the caret) completely alone.
      if (html === this._detailHTML && box.firstChild) return;
      this._detailHTML = html;
      box.innerHTML = html;
      box.scrollTop = scroll;
      box.querySelectorAll("[data-act]").forEach(b =>
        b.onclick = () => this.act(b.dataset.act, b.dataset.id, b));
      const steer = box.querySelector(".cg-steer-in");
      if (steer) {
        if (held && held.value) steer.value = held.value;
        steer.onkeydown = e => { if (e.key === "Enter") this.act("steer", steer.dataset.id, null); };
        if (held && held.focused) {
          try {
            steer.focus();
            steer.setSelectionRange(held.start ?? steer.value.length,
                                    held.end ?? steer.value.length);
          } catch (e) {}
        }
      }
      const close = box.querySelector(".cg-x");
      if (close) close.onclick = () => { this.sel = null; if (this.nc) this.nc.select(null); this.renderDetail(); };
      if (window.SpriteAnim) SpriteAnim.mountAll(box);
    },

    detailHTML(n) {
      const head = (eyebrow, title) => `<div class="cg-dhead">
        <div><div class="cg-de">${esc(eyebrow)}</div><div class="cg-dt">${esc(title)}</div></div>
        <button class="cg-x" aria-label="Close">×</button></div>`;

      if (n.type === "seat") {
        const live = this.liveSet();
        const items = ((this.state || {}).items || [])
          .filter(i => i.seat === n.seat && i.source !== "chat");
        const rows = items.slice(0, 14).map(i =>
          `<button class="cg-row" data-act="goto" data-id="task_${i.id}">
             <span class="cg-row-s">${live.has(Number(i.id)) ? "▶" : i.status === "done" ? "✓" : "▷"}</span>
             <span class="cg-row-t">${esc(i.title)}</span>
             <span class="cg-row-m">#${i.id}</span></button>`).join("")
          || `<div class="cg-empty">nothing routed here</div>`;
        const c = n.counts || {};
        return head("Seat", n.seat)
          + `<div class="cg-kv"><span>working</span><span>${c.running || 0}</span></div>`
          + `<div class="cg-kv"><span>queued</span><span>${c.queued || 0}</span></div>`
          + `<div class="cg-kv"><span>done</span><span>${c.done || 0}</span></div>`
          + `<div class="cg-sec">routed work</div><div class="cg-rows">${rows}</div>`
          + `<div class="cg-acts"><button class="qbtn small ghost" data-act="workspace"
               data-id="${esc(n.seat)}">open ${esc(n.seat)} workspace</button></div>`;
      }

      if (n.type === "turn") {
        const t = n.turn || {}; const r = t.reply || {};
        const kids = ((this.state || {}).items || []).filter(i => {
          const p = ((this.state.lineage || {}).parents || {});
          return Number(p[i.id] || p[String(i.id)] || 0) === Number(t.id);
        });
        const rows = kids.map(i => `<button class="cg-row" data-act="goto" data-id="task_${i.id}">
            <span class="cg-row-s" style="color:${seatColor(i.seat)}">${esc(i.seat)}</span>
            <span class="cg-row-t">${esc(i.title)}</span>
            <span class="cg-row-m">#${i.id}</span></button>`).join("")
          || `<div class="cg-empty">nothing delegated from this message yet</div>`;
        return head("You said", "#" + t.id)
          + `<div class="cg-quote">${esc(t.said || t.title)}</div>`
          + `<div class="cg-sec">director</div>`
          + `<div class="cg-answer ${r.running ? "live" : ""}">${esc(r.text || r.thinking || (r.running ? "working…" : "no answer yet"))}</div>`
          + `<div class="cg-sec">delegated</div><div class="cg-rows">${rows}</div>`
          + `<div class="cg-acts"><button class="qbtn small ghost" data-act="log" data-id="${t.id}">full log</button>`
          + (r.running ? `<button class="qbtn small ghost" data-act="stop" data-id="${t.id}">stop</button>` : "")
          + `</div>`;
      }

      if (n.type === "phase") {
        const ph = n.phase || {};
        const arts = ph.artifacts || [];
        // The state payload keeps step text only for the newest few phases —
        // repeating every step inside every phase was two thirds of the poll.
        // An older pocket says how many it had and points at the log, which is
        // the honest version of the empty state it would otherwise render.
        const dropped = Number(ph.steps_dropped || 0);
        const feed = (ph.steps || []).map(s => stepRow(s, n.itemId)).join("")
          + (dropped ? `<div class="cg-empty">${dropped} earlier step${
              dropped === 1 ? "" : "s"} in this pocket — open the full log</div>` : "")
          || `<div class="cg-empty">nothing recorded in this pocket</div>`;
        // What it had in front of it. First, because when an agent is working
        // the question is not "what did it file" — it is "what is it looking
        // at", and that was the one thing this panel could not answer.
        const seen = (ph.seen || []);
        const looking = seen.length
          ? `<div class="cg-sec">looking at · ${seen.length}</div>`
            + fileGrid(seen.map(rel => ({ path: rel })), n.itemId)
          : "";
        // The source, scenes and data it read. Same grid, different question:
        // "looking at" is pictures, this is the work itself — and both are now
        // openable rather than quoted.
        const read = (ph.read || []);
        const reading = read.length
          ? `<div class="cg-sec">files it read · ${read.length}</div>`
            + fileGrid(read.map(rel => ({ path: rel })), n.itemId)
          : "";
        const made = arts.length
          ? `<div class="cg-sec">made here · ${arts.length}</div>`
            + fileGrid(arts, n.itemId, true)
          : "";
        return head(`Phase ${ph.n} · item #${n.itemId}`, ph.title || "working")
          + `<div class="cg-kv"><span>state</span><span>${esc(ph.state || "")}</span></div>`
          + `<div class="cg-kv"><span>tools</span><span>${esc((ph.tools || []).join(", ")) || "—"}</span></div>`
          + (ph.error ? `<div class="cg-note bad">${esc(ph.error)}</div>` : "")
          + looking
          + reading
          + made
          + `<div class="cg-sec">what happened</div><div class="cg-feed">${feed}</div>`
          + `<div class="cg-acts">
               <button class="qbtn small ghost" data-act="goto" data-id="task_${n.itemId}">the task</button>
               <button class="qbtn small ghost" data-act="log" data-id="${n.itemId}">full log</button>
             </div>`;
      }

      if (n.type === "gate") {
        const g = n.gate || {};
        if (g.kind === "signoff") {
          return head(g.parked ? "Sign-off · held" : "Sign-off", g.title)
            + `<div class="cg-kv"><span>seat</span><span style="color:${seatColor(g.seat)}">${esc(g.seat)}</span></div>`
            + `<div class="cg-kv"><span>item</span><span>#${g.item_id}</span></div>`
            + `<div class="cg-sec">what it says it did</div>`
            + `<div class="cg-answer">${esc(g.result || "(no result note)")}</div>`
            + (g.parked
              ? `<div class="cg-note">This item is PARKED IN REVIEW under the
                   builder's gate — it is not closed, and anything chained behind
                   it will not start until you accept. Send it back and the reason
                   is appended to the brief for the next round.</div>`
              : `<div class="cg-note">'Done' is the agent's claim. Accept it and the
                   gate clears; send it back and the reason is appended to the brief
                   for whoever picks it up next.</div>`)
            + `<div class="cg-acts">
                 <button class="qbtn small" data-act="accept" data-id="${g.item_id}">accept</button>
                 <button class="qbtn small ghost" data-act="sendback" data-id="${g.item_id}">send back</button>
                 <button class="qbtn small ghost" data-act="log" data-id="${g.item_id}">log</button>
               </div>`;
        }
        if (g.kind === "art") {
          const img = g.path
            ? `<img class="cg-shot" src="/api/preview?rel=${encodeURIComponent(g.path)}" alt="">` : "";
          return head("Approval gate", g.title)
            + img
            + `<div class="cg-note">Only a human can approve a candidate. The agent that made it cannot.</div>`
            + `<div class="cg-acts">
                 <button class="qbtn small" data-act="approve" data-id="${g.artifact_id}">approve</button>
                 <button class="qbtn small ghost" data-act="reject" data-id="${g.artifact_id}">reject</button>
                 <button class="qbtn small ghost" data-act="assets" data-id="">open in assets</button></div>`;
        }
        return head(g.kind === "escalation" ? "Escalation" : "QA gate", g.title)
          + `<div class="cg-kv"><span>seat</span><span>${esc(g.seat)}</span></div>`
          + `<div class="cg-kv"><span>status</span><span>${esc(g.status)}</span></div>`
          + (g.over_item_id ? `<div class="cg-kv"><span>over</span><span>#${g.over_item_id}</span></div>` : "")
          + `<div class="cg-note">${g.kind === "escalation"
              ? "Deliberately not dispatched — three rounds failed and another agent will not settle it."
              : "An agent is checking another agent's claim before it counts as done."}</div>`
          + `<div class="cg-acts">
               ${g.status === "queued" ? `<button class="qbtn small" data-act="dispatch" data-id="${g.item_id}">dispatch</button>` : ""}
               <button class="qbtn small ghost" data-act="log" data-id="${g.item_id}">log</button>
               ${g.over_item_id ? `<button class="qbtn small ghost" data-act="goto" data-id="task_${g.over_item_id}">the item it gates</button>` : ""}
             </div>`;
      }

      const it = n.item || {};
      const steps = ((this.state || {}).steps || {})[String(it.id)] || [];
      const runPhases = ((this.state || {}).phases || {})[String(it.id)] || [];
      // The live tail comes from the newest phase, because that is where the
      // steps carry their pictures — the flat `steps` map is only the summary.
      const tail = (runPhases.slice(-1)[0] || {}).steps || steps;
      const feed = tail.slice(-8).map(s => stepRow(s, it.id)).join("")
        || `<div class="cg-empty">${n.running ? "warming up…" : "no live steps"}</div>`;

      // Everything this run has had in front of it, newest phase first. The
      // whole complaint this answers: watching an agent "work" without knowing
      // what it is looking at.
      const newestFirst = (key, cap) => {
        const out = [];
        for (let i = runPhases.length - 1; i >= 0 && out.length < cap; i--) {
          (runPhases[i][key] || []).forEach(rel => {
            if (out.length < cap && !out.includes(rel)) out.push(rel);
          });
        }
        return out;
      };
      const eyes = newestFirst("seen", 6);
      const eyesHTML = eyes.length
        ? `<div class="cg-sec">looking at</div>
           <div class="cg-strip big">${eyes.map(rel =>
             `<a class="cg-eye" href="/api/preview?rel=${encodeURIComponent(rel)}"
                 target="_blank" rel="noopener"
                 data-peek="${esc(rel)}" data-peek-item="${Number(it.id)}"
                 title="${esc(rel)} — click to expand">${thumb({ path: rel }, "cg-thumb", it.id)}</a>`
           ).join("")}</div>`
        : "";
      // The files the run is working IN, and — because this is the task rail and
      // the question here is "what is this run doing to my repo" — each one
      // opens straight onto its diff.
      const touched = newestFirst("read", 8);
      const readHTML = touched.length
        ? `<div class="cg-sec">files it read</div>
           <div class="cg-fchips">${touched.map(rel =>
             `<button class="cg-fchip" type="button" data-peek="${esc(rel)}"
                      data-peek-item="${Number(it.id)}" data-peek-view="diff"
                      title="${esc(rel)} — opens the diff">${esc(rel.split("/").pop())}</button>`
           ).join("")}</div>`
        : "";

      return head("Work item", "#" + it.id)
        + `<div class="cg-dt2">${esc(it.title)}</div>`
        + `<div class="cg-kv"><span>seat</span><span style="color:${seatColor(it.seat)}">${esc(it.seat)}</span></div>`
        + `<div class="cg-kv"><span>status</span><span>${n.running ? "running" : esc(it.status)}</span></div>`
        + `<div class="cg-kv"><span>source</span><span>${esc(it.source || "manual")}</span></div>`
        + (it.attempts ? `<div class="cg-kv"><span>rounds</span><span>${it.attempts}</span></div>` : "")
        + (it.total_cost_usd ? `<div class="cg-kv"><span>cost</span><span>$${Number(it.total_cost_usd).toFixed(3)}</span></div>` : "")
        + (it.brief_preview ? `<div class="cg-note">${esc(it.brief_preview)}${it.brief_len > 240 ? "…" : ""}</div>` : "")
        + (it.result ? `<div class="cg-sec">result</div><div class="cg-answer">${esc(it.result)}</div>` : "")
        + eyesHTML
        + `<div class="cg-sec">live steps</div><div class="cg-feed">${feed}</div>`
        + `<div class="cg-acts">
             ${it.status === "queued" ? `<button class="qbtn small" data-act="dispatch" data-id="${it.id}">dispatch</button>` : ""}
             ${n.running ? `<button class="qbtn small ghost" data-act="stop" data-id="${it.id}">stop</button>` : ""}
             <button class="qbtn small ghost" data-act="log" data-id="${it.id}">full log</button>
             <button class="qbtn small ghost" data-act="delegate" data-id="${it.id}">delegate</button>
           </div>`
        + (n.running ? `<div class="cg-steer">
             <input class="cg-steer-in steerin" data-id="${it.id}" placeholder="steer this agent…">
             <button class="qbtn small" data-act="steer" data-id="${it.id}">steer</button></div>
           <button class="cg-link" data-act="target" data-id="${it.id}">…or aim the chat box at this agent</button>` : "");
    },

    async act(what, id, btn) {
      const M = window.mutate;
      const box = this._detail;
      try {
        if (what === "goto") { this.select(id); return; }
        if (what === "log") { if (window.watchAgent) watchAgent(Number(id)); return; }
        if (what === "assets") { if (window.setWorkspace) setWorkspace("assets"); return; }
        if (what === "workspace") {
          if (window.setWorkspace) setWorkspace("seats");
          if (window.SeatShell && SeatShell.open) SeatShell.open(id);
          return;
        }
        if (what === "target") {
          const node = this.nodes.get("task_" + id);
          if (window.AgentsConsole) AgentsConsole.aim(Number(id), node && node.item);
          return;
        }
        if (what === "steer") {
          const el = box && box.querySelector(".cg-steer-in");
          const text = (el && el.value || "").trim();
          if (!text) { if (window.toast) toast("type a steer first"); return; }
          if (el) el.value = "";
          if (window._sendSteer) { await window._sendSteer(Number(id), text, el); }
          else await M(`/api/queue/${id}/steer`, { body: { text } });
          return;
        }
        if (what === "dispatch") {
          const r = await M(`/api/queue/${id}/dispatch`, { ok: `dispatched #${id}`, button: btn });
          if (!r.ok) return;
        } else if (what === "stop") {
          const r = await M(`/api/queue/${id}/stop`, { ok: `stopped #${id}`, button: btn });
          if (!r.ok) return;
        } else if (what === "delegate") {
          const r = await M("/api/orchestrator/delegate", { body: { item_id: Number(id) }, ok: "director is splitting it" });
          if (!r.ok) return;
        } else if (what === "accept") {
          const r = await M("/api/console/signoff",
                            { body: { item_id: Number(id), verdict: "accept" },
                              ok: `#${id} accepted`, button: btn });
          if (!r.ok) return;
        } else if (what === "sendback") {
          const reason = await window.askText({
            title: `Send #${id} back`,
            body: "What is wrong with it? This is appended to the brief, so the "
                + "next agent on this item reads exactly what you wrote.",
            placeholder: "the idle is off-model against the pinned ref — redo it…",
            ok: "send it back", required: true,
          });
          if (reason == null) return;
          const r = await M("/api/console/signoff",
                            { body: { item_id: Number(id), verdict: "reopen", reason },
                              ok: `#${id} sent back` });
          if (!r.ok) return;
        } else if (what === "approve" || what === "reject") {
          const r = await M(`/api/artifacts/${id}/review`,
                            { body: { status: what === "approve" ? "approved" : "rejected" },
                              ok: what === "approve" ? "approved" : "rejected", button: btn });
          if (!r.ok) return;
        }
        if (window.AgentsConsole && AgentsConsole.poll) AgentsConsole.poll();
      } catch (e) {
        if (window.toast) toast("action failed");
      }
    },

    /* ---- positions ----------------------------------------------------- */
    async loadPositions() {
      try {
        const d = await window.readJSON(WS_PATH, { data: {} });
        const data = (d && d.data) || {};
        if (data.positions && typeof data.positions === "object") this.positions = data.positions;
      } catch (e) { this.positions = {}; }
    },

    onMove(n) {
      if (!n) return;
      const cur = this.nodes.get(n.id);
      if (cur) { cur.x = n.x; cur.y = n.y; }
      this.positions[n.id] = { x: n.x, y: n.y };
      clearTimeout(this._saveT);
      this._saveT = setTimeout(() => {
        window.mutate(WS_PATH, { body: { data: { positions: this.positions } }, quiet: true });
      }, 800);
    },

    relayout() {
      // Forget every saved position and rebuild from the column layout. The
      // escape hatch for a graph somebody dragged into a knot.
      this.positions = {};
      this.nodes = new Map();
      this._sig = "";
      window.mutate(WS_PATH, { body: { data: { positions: {} } }, quiet: true });
      this.rebuild();
      if (this.nc) this.nc.fit();
    },

    fit() { if (this.nc) { try { this.nc.fit(); } catch (e) {} } },

    activate() {
      if (!this.nc) return;
      if (!this._fitted) { this._fitted = true; setTimeout(() => this.fit(), 30); }
    },
  };

  window.AgentsGraph = AgentsGraph;
})();
