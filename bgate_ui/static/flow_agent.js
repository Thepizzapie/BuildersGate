/* Agent flow — a live multi-agent orchestration canvas.
 *
 * Registered as window.StudioFlows.agent and mounted by flows.js. Renders the
 * work queue as Task nodes wired into the 7 Seat nodes, polls /api/agents +
 * /api/queue live to reflect running agents (ember pulse) without clobbering
 * dragged positions, and drives dispatch/watch/steer/stop/delegate from a
 * right-hand inspector. Node positions persist to the director workspace.
 *
 * Frontend only. Never throws — every async path is guarded.
 */
(function () {
  const SEATS = ["director", "narrative", "gameplay", "tech", "art", "audio", "qa"];
  const SEAT_COLORS = {
    director: "var(--warn)", narrative: "var(--c-narrative)", gameplay: "var(--bad)",
    tech: "var(--text)", art: "var(--c-narrative)", audio: "var(--good)", qa: "var(--good)",
  };
  const MUTED = "var(--seam2)";
  const EMBER = "var(--ember)";
  const WS_PATH = "/api/workspace/director/agent-flow";

  injectStyle();

  window.StudioFlows = window.StudioFlows || {};
  window.StudioFlows.agent = {
    label: "Agent flow", glyph: "⌁",
    build(host, api) { try { new AgentFlow(host, api).start(); } catch (e) { fail(host, api, e); } },
  };

  function fail(host, api, e) {
    try { host.innerHTML = `<div class="empty" style="padding:24px">agent flow error: ${api.esc(e && e.message || e)}</div>`; } catch (_) {}
    try { console.error("[agent-flow]", e); } catch (_) {}
  }

  class AgentFlow {
    constructor(host, api) {
      this.host = host;
      this.api = api;
      this.nc = null;
      this.nodes = new Map();     // our own authoritative node objects (hold x/y)
      this.edges = [];
      this.items = [];            // last queue snapshot
      this.live = new Set();      // running item ids
      this.sel = null;            // selected node id
      this.positions = {};        // saved {nodeId:{x,y}}
      this._poll = null;
      this._feed = null;
      this._saveT = null;
      this._destroyed = false;
      this._idsig = "";
    }

    async start() {
      const esc = this.api.esc;
      this.host.innerHTML = `<div class="fg-wrap">
        <div class="fg-palette">
          <div class="fg-ph">New task</div>
          <select class="fg-in" id="fg-seat">
            ${SEATS.map(s => `<option value="${s}">${s}</option>`).join("")}
          </select>
          <input class="fg-in" id="fg-title" placeholder="task title…" maxlength="120">
          <select class="fg-in" id="fg-prio">
            <option value="0">normal</option>
            <option value="3">high</option>
            <option value="5">urgent</option>
          </select>
          <button class="fg-add" id="fg-addbtn">＋ queue task</button>
          <div class="fg-ph" style="margin-top:18px">Live</div>
          <div class="fg-hint" id="fg-livestat">connecting…</div>
        </div>
        <div class="fg-canvas" id="fg-canvas"></div>
        <div class="fg-insp" id="fg-insp"><div class="fg-insp-empty">Select a task or seat.</div></div>
      </div>`;

      this.$canvas = this.host.querySelector("#fg-canvas");
      this.$insp = this.host.querySelector("#fg-insp");
      this.$livestat = this.host.querySelector("#fg-livestat");

      const addBtn = this.host.querySelector("#fg-addbtn");
      if (addBtn) addBtn.onclick = () => this.addTask();
      const titleIn = this.host.querySelector("#fg-title");
      if (titleIn) titleIn.onkeydown = e => { if (e.key === "Enter") this.addTask(); };

      // teardown when the host is torn out of the DOM (flow switch / reselect)
      this._watchDetach();

      await this.loadPositions();
      const [q, ag] = await Promise.all([this.api.get("/api/queue"), this.api.get("/api/agents")]);
      this.ingest(q, ag);
      this.buildCanvas();
      this.startPolling();
    }

    /* ---------------- data → nodes ---------------- */
    ingest(q, ag) {
      this.items = ((q && q.items) || []).filter(i => i && i.status !== "done");
      this.live = new Set(((ag && ag.agents) || []).filter(a => a && a.state === "running").map(a => a.item_id));
    }

    seatCounts(seat) {
      let queued = 0, running = 0, total = 0;
      for (const it of this.items) {
        if (it.seat !== seat) continue;
        total++;
        if (this.live.has(it.id)) running++;
        else if (it.status === "queued") queued++;
      }
      return { queued, running, total };
    }

    // Build the full node/edge set from the current queue, honoring saved and
    // in-memory positions so a rebuild never yanks a node the user dragged.
    computeNodes() {
      const nodes = new Map();
      const edges = [];
      const pos = (id, dx, dy) => {
        const prev = this.nodes.get(id);
        if (prev && Number.isFinite(prev.x)) return { x: prev.x, y: prev.y };
        const saved = this.positions[id];
        if (saved && Number.isFinite(saved.x)) return { x: saved.x, y: saved.y };
        return { x: dx, y: dy };
      };

      SEATS.forEach((s, i) => {
        const id = "seat_" + s;
        const c = this.seatCounts(s);
        const p = pos(id, 520, 30 + i * 118);
        nodes.set(id, {
          id, type: "seat", seat: s, glyph: "◈", title: s.toUpperCase(),
          badge: c.running ? "live" : "", accent: SEAT_COLORS[s] || EMBER,
          x: p.x, y: p.y, w: 190, counts: c,
          ports: { in: [{ id: "i", label: "" }] },
        });
      });

      const tasks = this.items.slice(0, 40);
      tasks.forEach((it, i) => {
        const id = "task_" + it.id;
        const running = this.live.has(it.id);
        const p = pos(id, 40, 30 + i * 92);
        nodes.set(id, {
          id, type: "task", item: it, glyph: running ? "▶" : "▷",
          title: it.title || ("item " + it.id),
          badge: running ? "running" : (it.status || ""),
          accent: running ? EMBER : MUTED, running,
          x: p.x, y: p.y, w: 250,
          ports: { out: [{ id: "o", label: "" }] },
        });
        if (nodes.has("seat_" + it.seat)) edges.push({ from: [id, "o"], to: ["seat_" + it.seat, "i"] });
      });

      return { nodes, edges };
    }

    idSignature(nodesMap) { return [...nodesMap.keys()].sort().join("|"); }

    buildCanvas() {
      const { nodes, edges } = this.computeNodes();
      this.nodes = nodes;
      this.edges = edges;
      this._idsig = this.idSignature(nodes);

      const nc = new this.api.NodeCanvas(this.$canvas, {
        nodes: [...nodes.values()],
        edges,
        accent: EMBER,
        renderBody: n => this.renderBody(n),
        onSelect: n => this.onSelect(n),
        onNodeMove: n => this.onNodeMove(n),
      });
      nc.mount();
      nc.fit();
      this.nc = nc;
      if (this.api.setCanvas) this.api.setCanvas(nc);
      this.renderInspector();
    }

    renderBody(n) {
      const esc = this.api.esc;
      if (n.type === "task") {
        const it = n.item || {};
        const dot = n.running ? `<span class="fg-dot"></span>` : "";
        const label = n.running ? "running" : esc(it.status || "queued");
        return `<div class="fg-tmeta">${dot}<span>${esc(it.source || "manual")}</span> · <span>${label}</span></div>`;
      }
      const c = n.counts || { queued: 0, running: 0 };
      const state = c.running ? `<span class="fg-live">● ${c.running} working</span>` : `<span class="fg-idle">idle</span>`;
      return `<div class="fg-smeta">${state}<span class="fg-q">${c.queued} queued</span></div>`;
    }

    /* ---------------- live polling ---------------- */
    startPolling() {
      const tick = async () => {
        if (this._destroyed) return;
        if (!document.body.contains(this.host)) { this.destroy(); return; }
        try {
          const [q, ag] = await Promise.all([this.api.get("/api/queue"), this.api.get("/api/agents")]);
          this.ingest(q, ag);
          this.applyLive();
        } catch (e) { /* keep polling */ }
      };
      this._poll = setInterval(tick, 3000);
    }

    // Diff-update in place: mutate existing node objects (badge/accent/glyph)
    // and re-render only those; rebuild fully only when the node SET changes.
    applyLive() {
      if (!this.nc) return;
      const next = this.computeNodes();
      const sig = this.idSignature(next.nodes);

      if (sig !== this._idsig) {
        // set of nodes changed (task added/removed) — rebuild, positions kept
        this.nodes = next.nodes;
        this.edges = next.edges;
        this._idsig = sig;
        try {
          this.nc.setNodes([...next.nodes.values()], next.edges);
          if (this.sel && next.nodes.has(this.sel)) this.nc.select(this.sel);
          else this.sel = null;
        } catch (e) {}
        this.updateLiveStat();
        this.renderInspector();
        return;
      }

      // same set — patch each node in place
      for (const [id, fresh] of next.nodes) {
        const cur = this.nodes.get(id);
        if (!cur) continue;
        const changed = cur.badge !== fresh.badge || cur.accent !== fresh.accent ||
          cur.glyph !== fresh.glyph || cur.running !== fresh.running ||
          JSON.stringify(cur.counts) !== JSON.stringify(fresh.counts);
        cur.item = fresh.item;
        cur.counts = fresh.counts;
        cur.badge = fresh.badge;
        cur.accent = fresh.accent;
        cur.glyph = fresh.glyph;
        cur.running = fresh.running;
        if (changed) { try { this.nc.addNode(cur); } catch (e) {} } // re-renders in place (keeps x/y)
      }
      if (this.sel) this.nc.select(this.sel);
      this.updateLiveStat();
      // refresh inspector for the selected task's status if visible
      if (this.sel && this.nodes.get(this.sel) && this.nodes.get(this.sel).type === "task") this.renderInspector(true);
    }

    updateLiveStat() {
      if (!this.$livestat) return;
      const running = this.live.size;
      const open = this.items.length;
      this.$livestat.innerHTML = running
        ? `<span class="fg-live">● ${running} agent${running === 1 ? "" : "s"} running</span> · ${open} open`
        : `${open} open task${open === 1 ? "" : "s"} · idle`;
    }

    /* ---------------- selection + inspector ---------------- */
    onSelect(n) {
      this.sel = n ? n.id : null;
      this.stopFeed();
      this.renderInspector();
      const node = this.sel ? this.nodes.get(this.sel) : null;
      if (node && node.type === "task" && node.running) this.startFeed(node.item.id);
    }

    renderInspector(quiet) {
      if (!this.$insp) return;
      const node = this.sel ? this.nodes.get(this.sel) : null;
      if (!node) { if (!quiet) this.$insp.innerHTML = `<div class="fg-insp-empty">Select a task or seat.</div>`; return; }
      if (node.type === "seat") { this.renderSeatInspector(node); return; }
      this.renderTaskInspector(node, quiet);
    }

    renderSeatInspector(node) {
      const esc = this.api.esc;
      const c = node.counts || this.seatCounts(node.seat);
      const tasks = this.items.filter(i => i.seat === node.seat);
      const rows = tasks.length ? tasks.map(it =>
        `<div class="fg-srow" data-goto="task_${it.id}"><span>${this.live.has(it.id) ? "▶" : "▷"}</span>
           <span class="fg-srow-t">${esc(it.title || ("#" + it.id))}</span></div>`).join("")
        : `<div class="fg-insp-p">No open tasks routed to this seat.</div>`;
      this.$insp.innerHTML = `
        <div class="fg-insp-h" style="color:${SEAT_COLORS[node.seat] || "var(--bone)"}">${node.glyph} ${esc(node.title)}</div>
        <div class="fg-kv"><span>running</span><span>${c.running}</span></div>
        <div class="fg-kv"><span>queued</span><span>${c.queued}</span></div>
        <div class="fg-kv"><span>total open</span><span>${c.total}</span></div>
        <div class="fg-ph" style="margin:14px 0 6px">Routed tasks</div>
        <div class="fg-slist">${rows}</div>`;
      this.$insp.querySelectorAll("[data-goto]").forEach(el => el.onclick = () => {
        const id = el.dataset.goto;
        if (this.nodes.has(id) && this.nc) { this.nc.select(id); this.sel = id; this.onSelect(this.nodes.get(id)); try { this.nc.fit && null; } catch (e) {} }
      });
    }

    renderTaskInspector(node, quiet) {
      const esc = this.api.esc;
      const it = node.item || {};
      const running = this.live.has(it.id);
      const isQueued = it.status === "queued" && !running;
      const brief = (it.brief || "").slice(0, 320);
      const feed = running ? `<div class="fg-ph" style="margin:14px 0 6px">Live activity</div>
        <div class="fg-feed" id="fg-feed"><div class="fg-insp-p">listening…</div></div>` : "";
      this.$insp.innerHTML = `
        <div class="fg-insp-h">${node.glyph} ${esc(it.title || ("item " + it.id))}</div>
        <div class="fg-kv"><span>seat</span><span style="color:${SEAT_COLORS[it.seat] || "var(--bone)"}">${esc(it.seat || "—")}</span></div>
        <div class="fg-kv"><span>status</span><span>${running ? "running" : esc(it.status || "—")}</span></div>
        <div class="fg-kv"><span>source</span><span>${esc(it.source || "manual")}</span></div>
        <div class="fg-kv"><span>priority</span><span>${esc(String(it.priority == null ? 0 : it.priority))}</span></div>
        ${brief ? `<div class="fg-insp-p">${esc(brief)}${(it.brief || "").length > 320 ? "…" : ""}</div>` : ""}
        <div class="fg-actions">
          ${isQueued ? `<button class="fg-btn primary" data-act="dispatch">▷ dispatch</button>` : ""}
          <button class="fg-btn" data-act="watch">watch</button>
          ${running ? `<button class="fg-btn" data-act="stop">stop</button>` : ""}
          <button class="fg-btn" data-act="delegate">delegate</button>
        </div>
        <div class="fg-steer">
          <input class="fg-in" id="fg-steer-in" placeholder="steer the agent…">
          <button class="fg-btn" data-act="steer">send</button>
        </div>
        ${feed}`;

      this.$insp.querySelectorAll("[data-act]").forEach(b => b.onclick = () => this.taskAction(b.dataset.act, it.id));
      const steerIn = this.$insp.querySelector("#fg-steer-in");
      if (steerIn) steerIn.onkeydown = e => { if (e.key === "Enter") this.taskAction("steer", it.id); };

      if (running && !quiet) this.startFeed(it.id);
    }

    async taskAction(act, id) {
      const T = this.api.toast;
      try {
        if (act === "watch") {
          if (typeof window.watchAgent === "function") window.watchAgent(id);
          else T("watch unavailable", true);
          return;
        }
        if (act === "dispatch") { await this.api.post(`/api/queue/${id}/dispatch`, {}); T("dispatched"); }
        else if (act === "stop") { await this.api.post(`/api/queue/${id}/stop`, {}); T("stop sent"); }
        else if (act === "delegate") { await this.api.post("/api/orchestrator/delegate", { item_id: id }); T("director delegating…"); }
        else if (act === "steer") {
          const inp = this.$insp.querySelector("#fg-steer-in");
          const text = (inp && inp.value || "").trim();
          if (!text) { T("type a steer first", true); return; }
          await this.api.post(`/api/queue/${id}/steer`, { text });
          if (inp) inp.value = "";
          T("steer sent");
        }
        // refresh promptly after a mutating action
        const [q, ag] = await Promise.all([this.api.get("/api/queue"), this.api.get("/api/agents")]);
        this.ingest(q, ag);
        this.applyLive();
      } catch (e) { T("action failed", true); }
    }

    async addTask() {
      const T = this.api.toast;
      try {
        const seat = (this.host.querySelector("#fg-seat") || {}).value || "director";
        const titleEl = this.host.querySelector("#fg-title");
        const title = (titleEl && titleEl.value || "").trim();
        const prio = parseInt((this.host.querySelector("#fg-prio") || {}).value || "0", 10) || 0;
        if (!title) { T("enter a title", true); return; }
        const res = await this.api.post("/api/queue", { seat, title, brief: title, priority: prio });
        if (res && (res.id || res.ok !== false)) {
          if (titleEl) titleEl.value = "";
          T("queued to " + seat);
          const [q, ag] = await Promise.all([this.api.get("/api/queue"), this.api.get("/api/agents")]);
          this.ingest(q, ag);
          this.applyLive();
        } else T("could not queue", true);
      } catch (e) { T("could not queue", true); }
    }

    /* ---------------- live activity feed ---------------- */
    startFeed(itemId) {
      this.stopFeed();
      const draw = async () => {
        if (this._destroyed) return;
        const box = this.$insp && this.$insp.querySelector("#fg-feed");
        if (!box) { this.stopFeed(); return; }
        try {
          const a = await this.api.get(`/api/agent-activity/${itemId}`);
          const steps = ((a && a.steps) || []).slice(-8);
          if (!steps.length) { box.innerHTML = `<div class="fg-insp-p">${a && a.running ? "warming up…" : "no activity yet"}</div>`; }
          else box.innerHTML = steps.map(s => this.feedRow(s)).join("");
          if (a && a.final) box.insertAdjacentHTML("beforeend", `<div class="fg-frow final">✓ ${this.api.esc(String(a.final).slice(0, 160))}</div>`);
          if (a && !a.running) this.stopFeed();  // finished
        } catch (e) {}
      };
      draw();
      this._feed = setInterval(draw, 3000);
    }

    feedRow(s) {
      const esc = this.api.esc;
      const k = s.kind;
      if (k === "tool") return `<div class="fg-frow tool"><span class="fg-fk">⚙ ${esc(s.name || "tool")}</span>${s.hint ? `<span class="fg-fh">${esc(s.hint)}</span>` : ""}</div>`;
      if (k === "say") return `<div class="fg-frow say">${esc(s.text || "")}</div>`;
      if (k === "steer") return `<div class="fg-frow steer">➤ ${esc(s.text || "")}</div>`;
      if (k === "result") return `<div class="fg-frow res">${esc(s.text || "")}</div>`;
      return `<div class="fg-frow">${esc(s.text || s.name || "")}</div>`;
    }

    stopFeed() { if (this._feed) { clearInterval(this._feed); this._feed = null; } }

    /* ---------------- persistence ---------------- */
    async loadPositions() {
      try {
        const r = await this.api.get(WS_PATH);
        const data = (r && r.data) || {};
        if (data && data.positions && typeof data.positions === "object") this.positions = data.positions;
      } catch (e) { this.positions = {}; }
    }

    onNodeMove(n) {
      if (!n) return;
      const cur = this.nodes.get(n.id);
      if (cur) { cur.x = n.x; cur.y = n.y; }
      this.positions[n.id] = { x: n.x, y: n.y };
      if (this._saveT) clearTimeout(this._saveT);
      this._saveT = setTimeout(() => this.savePositions(), 700);
    }

    async savePositions() {
      try { await this.api.post(WS_PATH, { data: { positions: this.positions } }); }
      catch (e) {}
    }

    /* ---------------- lifecycle ---------------- */
    _watchDetach() {
      // Poll loop already self-terminates when host leaves the DOM; this is a
      // belt-and-suspenders observer for immediate teardown.
      try {
        const obs = new MutationObserver(() => {
          if (!document.body.contains(this.host)) { this.destroy(); obs.disconnect(); }
        });
        if (this.host.parentNode) obs.observe(this.host.parentNode, { childList: true });
        this._obs = obs;
      } catch (e) {}
    }

    destroy() {
      this._destroyed = true;
      if (this._poll) { clearInterval(this._poll); this._poll = null; }
      this.stopFeed();
      if (this._saveT) { clearTimeout(this._saveT); this._saveT = null; }
      if (this._obs) { try { this._obs.disconnect(); } catch (e) {} this._obs = null; }
    }
  }

  /* ---------------- styles (injected once) ---------------- */
  function injectStyle() {
    if (document.getElementById("flow-agent-style")) return;
    const s = document.createElement("style");
    s.id = "flow-agent-style";
    s.textContent = `
      .fg-wrap{display:flex;height:100%;border:1px solid var(--seam);border-radius:12px;overflow:hidden;background:var(--iron)}
      .fg-palette{width:190px;flex:none;background:var(--iron);border-right:1px solid var(--seam);padding:14px 12px;overflow-y:auto}
      .fg-ph{font-family:var(--mono);font-size:9px;letter-spacing:.22em;text-transform:uppercase;color:var(--ash2);margin-bottom:8px}
      .fg-in{display:block;width:100%;box-sizing:border-box;margin-bottom:8px;padding:8px 9px;background:var(--void);border:1px solid var(--seam);border-radius:8px;color:var(--bone);font:inherit;font-size:12px}
      .fg-in:focus{outline:none;border-color:var(--ember)}
      .fg-add{width:100%;padding:9px;background:var(--ember);color:var(--bg);border:0;border-radius:8px;font:inherit;font-weight:var(--fw-semi);font-size:12px;cursor:pointer}
      .fg-add:hover{filter:brightness(1.08)}
      .fg-hint{font-size:11.5px;color:var(--ash);line-height:1.5}
      .fg-canvas{flex:1;position:relative;min-width:0}
      .fg-insp{width:260px;flex:none;background:var(--iron);border-left:1px solid var(--seam);padding:15px;overflow-y:auto}
      .fg-insp-empty{color:var(--ash2);font-size:12px}
      .fg-insp-h{font-size:13px;font-weight:var(--fw-semi);color:var(--bone);margin-bottom:12px;line-height:1.3;word-break:break-word}
      .fg-insp-p{font-size:12px;color:var(--ash);line-height:1.5;margin-top:8px;word-break:break-word}
      .fg-kv{display:flex;justify-content:space-between;gap:10px;font-size:12px;padding:4px 0;border-bottom:1px solid var(--seam)}
      .fg-kv span:first-child{color:var(--ash2);font-family:var(--mono);font-size:11px}
      .fg-kv span:last-child{color:var(--bone)}
      .fg-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
      .fg-btn{padding:6px 11px;background:var(--plate);border:1px solid var(--seam);border-radius:8px;color:var(--bone);font:inherit;font-size:11.5px;cursor:pointer}
      .fg-btn:hover{border-color:var(--ember)}
      .fg-btn.primary{background:var(--ember);color:var(--bg);border-color:var(--ember);font-weight:var(--fw-semi)}
      .fg-steer{display:flex;gap:6px;margin-top:10px}
      .fg-steer .fg-in{margin-bottom:0;flex:1}
      .fg-tmeta{font-family:var(--mono);font-size:10.5px;color:var(--ash);display:flex;align-items:center;gap:5px;flex-wrap:wrap}
      .fg-smeta{font-family:var(--mono);font-size:10.5px;color:var(--ash);display:flex;justify-content:space-between;gap:8px}
      .fg-live{color:var(--good);font-weight:var(--fw-semi)}
      .fg-idle{color:var(--ash2)}
      .fg-q{color:var(--ash2)}
      .fg-dot{width:7px;height:7px;border-radius:50%;background:var(--ember);display:inline-block;box-shadow:0 0 0 0 var(--ember);animation:fg-pulse 1.4s infinite}
      @keyframes fg-pulse{0%{box-shadow:0 0 0 0 rgba(255,106,61,.55)}70%{box-shadow:0 0 0 7px rgba(255,106,61,0)}100%{box-shadow:0 0 0 0 rgba(255,106,61,0)}}
      .fg-slist{display:flex;flex-direction:column;gap:2px}
      .fg-srow{display:flex;align-items:center;gap:7px;padding:5px 6px;border-radius:6px;cursor:pointer;font-size:12px;color:var(--ash)}
      .fg-srow:hover{background:var(--plate);color:var(--bone)}
      .fg-srow-t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .fg-feed{display:flex;flex-direction:column;gap:5px;max-height:260px;overflow-y:auto}
      .fg-frow{font-size:11px;line-height:1.4;padding:5px 7px;border-radius:6px;background:var(--plate);color:var(--ash);word-break:break-word}
      .fg-frow.tool{border-left:2px solid var(--accent)}
      .fg-frow .fg-fk{font-family:var(--mono);color:var(--bone);display:block}
      .fg-frow .fg-fh{font-family:var(--mono);color:var(--ash2);font-size:10px}
      .fg-frow.say{color:var(--bone)}
      .fg-frow.steer{border-left:2px solid var(--ember);color:var(--bone)}
      .fg-frow.res{color:var(--ash)}
      .fg-frow.final{border-left:2px solid var(--good);color:var(--good)}
    `;
    document.head.appendChild(s);
  }
})();
