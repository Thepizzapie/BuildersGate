/* WF — the workflow builder core.
 *
 * A workflow is a reusable, composable process the agents follow for a task-type:
 * a graph of typed STEPS (inputs, asset-generation, agents, control/QA) on the
 * NodeCanvas engine. Step types + starter templates are contributed by plugin
 * files (wf_steps_*.js) via WF.registerStep / WF.registerTemplate. This core owns
 * the library, the builder UI, persistence, and Run — which compiles the graph
 * (the step registry lives here, in the browser) and hands it to the server as a
 * PERSISTED run: one queue item per agent step, gates that block on a human, and
 * node statuses polled back onto the canvas. Reload the page and the run is still
 * there, because it never lived in this file.
 *
 * STEP CONTRACT (register from a plugin file):
 *   WF.registerStep({
 *     type:"art.animation",           // unique id
 *     category:"asset",               // input | asset | 3d | world | agent | control
 *     label:"Animation frames", glyph:"◈", accent:"var(--c-art)",
 *     ports(node){ return {in:[{id,label}], out:[{id,label}]}; },  // or omit for defaults
 *     defaults:{ frames:6, variants:2, ... },   // initial node.config
 *     body(node){ return "<html>"; },           // node-card body (small)
 *     config(node, ctx){ return "<html>"; },    // inspector config UI (ctx.commit(node) to persist)
 *     agentSeat:"art",                          // seat that runs this step (for Run); optional
 *     toBrief(node, wf){ return "brief text"; },// this step's agent brief for Run; optional
 *     kind:"agent"|"gate"|"consistency"|"passive", // what the RUN does with it; optional
 *   });
 *
 * `kind` is how a step behaves once the workflow is actually running: an agent
 * step becomes a queue item, a gate BLOCKS the run until a human approves it, a
 * consistency step has its threshold enforced against recorded scores, a passive
 * step just carries data. Omit it and the server derives it (gate/consistency by
 * type, agent from agentSeat) — it re-derives either way, so a step type cannot
 * lie its way past a gate.
 *
 * TEMPLATE CONTRACT:
 *   WF.registerTemplate({ id, name, category, hint, build(){ return {nodes,edges}; } });
 *   build() returns nodes:[{id,type,x,y,config?}] edges:[{from:[n,p],to:[n,p]}].
 */
(function () {
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const get = async p => { try { const r = await fetch(p); return r.ok ? r.json() : {}; } catch (e) { return {}; } };
  const post = async (p, b) => { try { const r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) }); return r.json().catch(() => ({ ok: r.ok })); } catch (e) { return { ok: false }; } };
  const toast = (m, bad) => (window.BGWS ? BGWS.toast(m, bad) : console.log(m));
  const uid = (p) => p + "_" + Math.random().toString(36).slice(2, 8);

  const CATS = [
    { id: "input", label: "Inputs" },
    { id: "asset", label: "2D asset gen" },
    { id: "world", label: "World / background" },
    { id: "3d", label: "3D · Blender" },
    { id: "agent", label: "Agents" },
    { id: "control", label: "Control / QA" },
    { id: "saved", label: "Saved workflows" },
  ];

  // API envelope: {ok:true,data} | {ok:false,error:{code,message}}. Unwrap once
  // here so every call site deals in payloads, not envelopes.
  const data = r => (r && r.ok && r.data !== undefined) ? r.data : null;
  const errMsg = r => (r && r.error && r.error.message) || "request failed";

  const STATUS_LABEL = { pending: "waiting", queued: "queued", running: "running",
    passed: "passed", failed: "failed", skipped: "skipped" };

  const WF = {
    steps: {}, templates: [], _nc: null, _wf: null, _saved: [], _api: null, _saveT: null,
    _run: null, _runNodes: null, _pollT: null,

    registerStep(def) { if (def && def.type) this.steps[def.type] = def; },
    registerTemplate(t) { if (t && t.id) this.templates.push(t); },

    /* config helper for step inspectors: WF.set('<nodeId>','field',value)
       updates the node's config, persists, and re-renders its card body. */
    set(id, field, value) {
      const w = (this._wf && this._wf.nodes || []).find(n => n.id === id);
      const cn = this._nc && this._nc.nodes.get(id);
      if (cn) { cn.config = cn.config || {}; cn.config[field] = value; }
      if (w) { w.config = w.config || {}; w.config[field] = value; }
      this.persist();
      // Re-render the whole graph (not just this node): downstream cards can
      // preview an upstream value — e.g. animation/edit nodes show the character
      // from their upstream anchor — so they must refresh when it changes. Node
      // bodies are display-only (no inputs), so this never steals inspector focus.
      if (this._nc) { try { this._nc.nodes.forEach(n => this._nc._renderNode(n)); } catch (e) { if (cn) this._nc._renderNode(cn); } }
    },

    _stepDef(type) { return this.steps[type] || { type, category: "control", label: type, glyph: "◇", body: () => "", ports: () => ({ in: [{ id: "i" }], out: [{ id: "o" }] }) }; },

    /* ---- pinned references ---------------------------------------------
       Pins are VERSIONED files named <slug>.rN<suffix> and the suffix is
       whatever was pinned, so ".bgate/refs/<name>.png" rendered every jpg and
       webp anchor blank — and pointed at a revision that may not exist. The
       registry is read once and every thumbnail resolves through it. A step
       may name an older revision as "<name>@r2". */
    _refs: { list: [], at: 0, loading: null },
    refsLoad(force) {
      const now = Date.now();
      if (!force && this._refs.list.length && now - this._refs.at < 20000) return Promise.resolve(this._refs.list);
      if (this._refs.loading) return this._refs.loading;
      this._refs.loading = get("/api/refs").then(d => {
        const list = (d && d.refs) || (d && d.data) || [];
        const first = !this._refs.at;
        if (Array.isArray(list)) { this._refs.list = list; this._refs.at = Date.now(); }
        this._refs.loading = null;
        // Node bodies asked for a thumbnail before the registry existed; repaint
        // them once it does, or every anchor stays blank until the next click.
        if (first && this._nc) {
          try { this._nc.nodes.forEach(n => this._nc._renderNode(n)); } catch (e) {}
        }
        return this._refs.list;
      }).catch(() => { this._refs.loading = null; return this._refs.list; });
      return this._refs.loading;
    },
    // "tommy@r2" -> {name:"tommy", revision:2}
    refParse(value) {
      const s = String(value == null ? "" : value).trim();
      const m = /^(.+?)@r?(\d+)$/.exec(s);
      return m ? { name: m[1], revision: parseInt(m[2], 10) } : { name: s, revision: null };
    },
    refPin(name) {
      const want = String(name || "").trim().toLowerCase();
      return this._refs.list.find(r => r && String(r.name || "").toLowerCase() === want) || null;
    },
    // The project-relative path /api/preview wants, for the exact revision asked for.
    refRel(value) {
      const q = this.refParse(value);
      if (!q.name) return "";
      const pin = this.refPin(q.name);
      if (!pin) { this.refsLoad(); return ""; }
      let p = String(pin.path || "").replace(/\\/g, "/");
      const cut = p.indexOf(".bgate/");
      if (cut !== -1) p = p.slice(cut);
      if (q.revision && q.revision !== Number(pin.revision || 1)) {
        // Older revisions sit beside the current one with the same suffix.
        p = p.replace(/\.r\d+(\.[^.]+)$/, ".r" + q.revision + "$1");
      }
      return p;
    },
    refImg(value, cls) {
      const rel = this.refRel(value);
      if (!rel) return "";
      return `<img class="${cls || "wf-b-img"}" src="/api/preview?rel=${encodeURIComponent(rel)}" onerror="this.style.display='none'">`;
    },
    /* The reference step's picker: real pins, real thumbnails, real revisions —
       it used to be one sentence and a free-text box, so a typo produced a
       workflow that ran against nothing. */
    refPicker(nodeId, field, current) {
      const host = `wf-refpick-${nodeId}`;
      this.refsLoad().then(() => this._paintRefPicker(host, nodeId, field));
      setTimeout(() => this._paintRefPicker(host, nodeId, field), 0);
      return `<div class="wf-refpick" id="${host}"><div class="wf-b-note">loading pinned references…</div></div>
        <div class="wf-row" style="flex-direction:column;align-items:stretch">
          <label style="margin-bottom:5px">or name it (supports <code>name@r2</code>)</label>
          <input type="text" style="width:100%" placeholder="a pinned ref, e.g. scoville"
            value="${esc(current || "")}" oninput="WF.set('${nodeId}','${field}',this.value)"></div>`;
    },
    _paintRefPicker(hostId, nodeId, field) {
      const host = document.getElementById(hostId);
      if (!host) return;
      const list = this._refs.list || [];
      if (!list.length) {
        host.innerHTML = `<div class="wf-b-note">no pinned references yet — pin one from the art seat's anchoring panel, then it appears here.</div>`;
        return;
      }
      const node = (this._wf && this._wf.nodes || []).find(n => n.id === nodeId);
      const cur = (node && node.config && node.config[field]) || "";
      const curName = this.refParse(cur).name.toLowerCase();
      host.innerHTML = list.map(r => {
        const rel = this.refRel(r.name);
        const sel = String(r.name || "").toLowerCase() === curName;
        return `<button class="wf-refcard ${sel ? "sel" : ""}" title="${esc(r.note || r.name)}"
          onclick="WF.set('${nodeId}','${field}','${esc(r.name)}');WF._paintRefPicker('${hostId}','${nodeId}','${field}')">
          ${rel ? `<img src="/api/preview?rel=${encodeURIComponent(rel)}" onerror="this.style.opacity=.12">` : `<span class="wf-refnone">?</span>`}
          <span class="wf-refname">${esc(r.name)}</span>
          <span class="wf-refmeta">${esc(r.kind || "")} · r${esc(r.revision || 1)}</span>
        </button>`;
      }).join("") + (curName && !this.refPin(curName)
        ? `<div class="wf-b-note" style="color:var(--bad)">“${esc(cur)}” is not a pinned reference — this step would run against nothing.</div>` : "");
    },

    /* ---- library landing ------------------------------------------------ */
    async open(host, api) {
      this._api = api || {};
      host.innerHTML = `<div class="wf-lib">
        <div class="wf-lib-head">
          <div><div class="wf-eyebrow">Workflow library</div><h3 class="wf-h">Templates &amp; saved workflows</h3></div>
          <button class="qbtn small" onclick="WF.newBlank()">＋ New workflow</button>
        </div>
        <div id="wf-lib-body"><div class="empty">loading…</div></div>
      </div>`;
      await this._loadSaved();
      this._renderLibrary();
    },
    async _loadSaved() {
      const d = await get("/api/workspace/studio/wf-index");
      this._saved = ((d.data && d.data.list) || []);
    },
    _renderLibrary() {
      const body = document.getElementById("wf-lib-body"); if (!body) return;
      const byCat = {};
      this.templates.forEach(t => (byCat[t.category] = byCat[t.category] || []).push(t));
      const tplCard = (t, saved) => `<button class="wf-card" onclick="WF.${saved ? "openSaved" : "openTemplate"}('${esc(saved ? t.id : t.id)}')">
        <span class="wf-card-g">${esc(t.glyph || "⬡")}</span>
        <span class="wf-card-t">${esc(t.name)}</span>
        <span class="wf-card-h">${esc(t.hint || (saved ? "saved workflow" : "template"))}</span></button>`;
      let html = "";
      CATS.filter(c => c.id !== "saved").forEach(c => {
        const ts = byCat[c.id] || [];
        if (!ts.length) return;
        html += `<div class="wf-lib-sec"><div class="wf-lib-cat">${esc(c.label)}</div><div class="wf-card-grid">${ts.map(t => tplCard(t)).join("")}</div></div>`;
      });
      if (this._saved.length) {
        html += `<div class="wf-lib-sec"><div class="wf-lib-cat">Your saved workflows</div><div class="wf-card-grid">${this._saved.map(s => `<button class="wf-card" onclick="WF.openSaved('${esc(s.id)}')"><span class="wf-card-g">◆</span><span class="wf-card-t">${esc(s.name)}</span><span class="wf-card-h">${esc(s.category || "workflow")} · ${(s.stepCount || 0)} steps</span><span class="wf-card-x" onclick="event.stopPropagation();WF.deleteSaved('${esc(s.id)}')">✕</span></button>`).join("")}</div></div>`;
      }
      body.innerHTML = html || `<div class="empty">no templates registered</div>`;
    },
    newBlank() { this.openWorkflow({ id: uid("wf"), name: "Untitled workflow", category: "custom", nodes: [], edges: [] }); },
    openTemplate(id) {
      const t = this.templates.find(x => x.id === id); if (!t) return;
      let built = { nodes: [], edges: [] };
      try { built = t.build() || built; } catch (e) { console.error(e); }
      this.openWorkflow({ id: uid("wf"), name: t.name, category: t.category, fromTemplate: id, nodes: built.nodes, edges: built.edges });
    },
    async openSaved(id) {
      const d = await get("/api/workspace/studio/wf:" + id);
      if (d.data && d.data.id) this.openWorkflow(d.data);
      else toast("could not load workflow", true);
    },
    /* Delete meant "drop it from the index": no confirmation, and the stored
       document (and any run history keyed to it) stayed behind forever. Now it
       asks first and empties the doc it is dropping. */
    async deleteSaved(id) {
      const entry = this._saved.find(s => s.id === id);
      const name = (entry && entry.name) || id;
      if (!window.confirm(`Delete the saved workflow “${name}”?\n\nThe stored document is removed too. Runs already started from it keep their own record.`)) return;
      this._saved = this._saved.filter(s => s.id !== id);
      await post("/api/workspace/studio/wf-index", { data: { list: this._saved } });
      // The workspace store has no DELETE; an empty document is the tombstone,
      // and openSaved() already treats a doc with no id as unloadable.
      await post("/api/workspace/studio/wf:" + id, { data: {} });
      if (this._wf && this._wf.id === id) { this._wf = null; clearTimeout(this._saveT); }
      toast(`deleted ${name}`);
      this._renderPalette();
      this._renderLibrary();
    },

    /* ---- builder -------------------------------------------------------- */
    openWorkflow(wf) {
      this._wf = wf;
      const host = document.getElementById("studio-body");
      host.innerHTML = `<div class="wf-build">
        <div class="wf-top">
          <button class="qbtn small ghost" onclick="Studio.select('workflows')">← library</button>
          <input class="wf-name" id="wf-name" value="${esc(wf.name)}" onchange="WF._wf.name=this.value;WF.persist()">
          <span class="wf-cat">${esc(wf.category || "custom")}</span>
          <div style="flex:1"></div>
          <button class="qbtn small ghost" onclick="WF.saveAsNode()">save as reusable node</button>
          <button class="qbtn small ghost" onclick="WF.save()">save</button>
          <button class="qbtn small" onclick="WF.run()">▶ Run workflow</button>
        </div>
        <div class="wf-runbar" id="wf-runbar" hidden></div>
        <div class="wf-main">
          <div class="wf-palette" id="wf-palette"></div>
          <div class="wf-canvas" id="wf-canvas"></div>
          <div class="wf-insp" id="wf-insp"><div class="wf-insp-empty">Select a step to configure it.</div></div>
        </div>
      </div>`;
      this._renderPalette();
      this._mountCanvas();
      this._attachRun();
    },
    /* A run lives on the server, so reopening the builder (or reloading the
       page entirely) re-attaches to whatever is still in flight. */
    async _attachRun() {
      clearTimeout(this._pollT);          // never let the last workflow's poll paint this one
      this._run = null; this._runNodes = null;
      const wfId = this._wf && this._wf.id; if (!wfId) return;
      const run = data(await get("/api/workflows/runs/latest?workflow_id=" + encodeURIComponent(wfId)));
      if (!run || !run.id) { this._renderRun(); return; }
      this._track(run.id);
    },
    _renderPalette() {
      const pal = document.getElementById("wf-palette"); if (!pal) return;
      const byCat = {};
      Object.values(this.steps).forEach(s => (byCat[s.category] = byCat[s.category] || []).push(s));
      // saved workflows are droppable as sub-workflow nodes
      let html = "";
      CATS.forEach(c => {
        let list = byCat[c.id] || [];
        if (c.id === "saved") list = this._saved.map(s => ({ type: "sub:" + s.id, label: s.name, glyph: "◆", accent: "var(--ember)" }));
        if (!list.length) return;
        html += `<div class="wf-pal-cat">${esc(c.label)}</div>` + list.map(s =>
          `<button class="wf-pi" style="--a:${s.accent || "var(--ember)"}" onclick="WF.addStep('${esc(s.type)}')"><span class="g">${esc(s.glyph || "◇")}</span> ${esc(s.label)}</button>`).join("");
      });
      pal.innerHTML = html || `<div class="empty">no steps</div>`;
    },
    _mountCanvas() {
      const NodeCanvas = (this._api && this._api.NodeCanvas) || window.NodeCanvas;
      const host = document.getElementById("wf-canvas");
      const nodes = (this._wf.nodes || []).map(n => this._toCanvasNode(n));
      const nc = new NodeCanvas(host, {
        nodes, edges: (this._wf.edges || []).slice(), accent: "var(--ember)",
        // The card body is the step's own preview with the LIVE run status
        // chipped on top — the canvas is where a run reports, not a console.
        renderBody: n => {
          let body = ""; try { body = this._stepDef(n.type).body ? this._stepDef(n.type).body(n) : ""; } catch (e) {}
          return this._statusChip(n.id) + body;
        },
        onSelect: n => this._inspect(n),
        onConnect: (from, to) => { this._wf.edges = nc.edges.slice(); this.persist(); },
        onNodeMove: n => { if (n) { const w = (this._wf.nodes || []).find(x => x.id === n.id); if (w) { w.x = n.x; w.y = n.y; } } this._wf.edges = nc.edges.slice(); this.persist(); },
        onNodeRemove: id => { this._wf.nodes = (this._wf.nodes || []).filter(n => n.id !== id); this._wf.edges = nc.edges.slice(); this.persist(); },
      });
      nc.mount(); nc.fit(); this._nc = nc;
      this.refsLoad();      // thumbnails resolve through the pin registry
      if (this._api && this._api.setCanvas) this._api.setCanvas(nc);
    },
    // a stored workflow node -> a NodeCanvas node (pull ports/glyph from the step def)
    _toCanvasNode(n) {
      const def = this._stepDef(n.type);
      const ports = def.ports ? def.ports(n) : { in: [{ id: "i" }], out: [{ id: "o" }] };
      return { id: n.id, type: n.type, config: n.config || Object.assign({}, def.defaults || {}),
        glyph: def.glyph || "◇", title: n.title || def.label || n.type, accent: def.accent || "var(--ember)",
        x: n.x != null ? n.x : 80, y: n.y != null ? n.y : 80, w: n.w || 220, ports, data: n.data };
    },
    addStep(type) {
      let def = this.steps[type];
      if (!def && type.startsWith("sub:")) def = { type, category: "saved", label: "sub-workflow", glyph: "◆", defaults: { ref: type.slice(4) }, body: () => "embedded workflow", ports: () => ({ in: [{ id: "i" }], out: [{ id: "o" }] }) };
      if (!def) return;
      const n = { id: uid("s"), type, x: 140, y: 120, config: Object.assign({}, def.defaults || {}) };
      (this._wf.nodes = this._wf.nodes || []).push(n);
      this._nc.addNode(this._toCanvasNode(n));
      this.persist();
    },
    _inspect(node) {
      const insp = document.getElementById("wf-insp"); if (!insp) return;
      if (!node) { insp.innerHTML = `<div class="wf-insp-empty">Select a step to configure it.</div>`; return; }
      const def = this._stepDef(node.type);
      const ctx = { esc, get, post, toast,
        commit: (n) => { const w = (this._wf.nodes || []).find(x => x.id === (n || node).id); if (w) { w.config = (n || node).config; w.title = (n || node).title; } this.persist(); if (this._nc) this._nc._renderNode(this._nc.nodes.get((n || node).id)); },
        activeItem: (window.BGWS ? BGWS.activeItem : null) };
      let html = `<div class="wf-insp-h"><span style="color:${def.accent || "var(--ember)"}">${esc(def.glyph || "◇")}</span> ${esc(def.label || node.type)}</div>`;
      try { html += (def.config ? def.config(node, ctx) : `<div class="wf-insp-p">No options.</div>`); }
      catch (e) { html += `<div class="wf-insp-p">config error: ${esc(e.message)}</div>`; }
      insp.innerHTML = html;
    },

    /* ---- persistence ---------------------------------------------------- */
    _serialize() {
      const nc = this._nc;
      const nodes = (this._wf.nodes || []).map(n => {
        const cn = nc && nc.nodes.get(n.id);
        return { id: n.id, type: n.type, title: n.title, x: cn ? cn.x : n.x, y: cn ? cn.y : n.y, config: (cn && cn.config) || n.config || {} };
      });
      return { id: this._wf.id, name: this._wf.name, category: this._wf.category, nodes, edges: (nc ? nc.edges : this._wf.edges || []) };
    },
    persist() { clearTimeout(this._saveT); this._saveT = setTimeout(() => this.save(true), 800); },
    async save(silent) {
      const wf = this._serialize(); this._wf.nodes = wf.nodes; this._wf.edges = wf.edges;
      await post("/api/workspace/studio/wf:" + wf.id, { data: wf });
      const entry = { id: wf.id, name: wf.name, category: wf.category, stepCount: wf.nodes.length };
      const i = this._saved.findIndex(s => s.id === wf.id);
      if (i >= 0) this._saved[i] = entry; else this._saved.push(entry);
      await post("/api/workspace/studio/wf-index", { data: { list: this._saved } });
      if (!silent) toast("workflow saved");
    },
    async saveAsNode() {
      await this.save(true);
      this._renderPalette();
      toast("saved — now a reusable node in the palette");
    },

    /* ---- run ------------------------------------------------------------ */
    /* The workflow is COMPILED here (the step registry lives in the browser)
       and EXECUTED on the server: one persisted run, one queue item per agent
       step, gates that actually block. The canvas then paints itself from the
       run's node statuses. */
    _compile(wf) {
      const order = this._topoOrder(wf);
      const nodes = order.map(id => wf.nodes.find(n => n.id === id)).filter(Boolean).map(n => {
        const def = this._stepDef(n.type);
        let brief = ""; try { brief = def.toBrief ? def.toBrief(n, wf) : ""; } catch (e) {}
        return { id: n.id, type: n.type, label: n.title || def.label || n.type,
          seat: def.agentSeat || "", kind: def.kind || "", brief, config: n.config || {} };
      });
      return { workflow: { id: wf.id, name: wf.name, category: wf.category },
        name: wf.name, nodes, edges: (wf.edges || []).slice() };
    },
    async run() {
      await this.save(true);                     // the run snapshots what is saved
      const wf = this._serialize();
      const plan = this._compile(wf);
      if (!plan.nodes.some(n => n.seat || n.kind === "consistency")) { toast("no agent/generation steps to run", true); return; }
      if (this._run && this._run.status === "running") { toast("this workflow is already running", true); return; }
      const res = await post("/api/workflows/runs", Object.assign({ dispatch: true }, plan));
      const run = data(res);
      if (!run) { toast(errMsg(res), true); return; }
      toast(`run #${run.id} started`);
      this._paint(run);
      this._track(run.id);
    },
    /* poll the run cheaply: the tick returns node statuses only, never the graph */
    _track(runId) {
      clearTimeout(this._pollT);
      const tick = async () => {
        const bar = document.getElementById("wf-runbar");
        if (!bar || !bar.isConnected) return;              // left the builder
        const run = data(await post(`/api/workflows/runs/${runId}/advance`, {}));
        if (!run) return;
        this._paint(run);
        if (run.status === "running") this._pollT = setTimeout(tick, 2500);
      };
      tick();
    },
    _paint(run) {
      const prev = this._run;
      this._run = run;
      const byId = {}; (run.nodes || []).forEach(n => byId[n.node_id] = n);
      this._runNodes = byId;
      const before = {}; ((prev && prev.nodes) || []).forEach(n => before[n.node_id] = n.status);
      if (this._nc) {
        (run.nodes || []).forEach(n => {
          if (before[n.node_id] === n.status) return;      // repaint only what moved
          const cn = this._nc.nodes.get(n.node_id);
          if (cn) { cn.badge = STATUS_LABEL[n.status] || n.status; this._nc._renderNode(cn); }
        });
      }
      this._renderRun();
    },
    _statusChip(nodeId) {
      const n = this._runNodes && this._runNodes[nodeId];
      if (!n) return "";
      const label = STATUS_LABEL[n.status] || n.status;
      return `<div class="wf-st wf-st-${esc(n.status)}" title="${esc(n.detail || "")}">${esc(label)}</div>`;
    },
    _renderRun() {
      const bar = document.getElementById("wf-runbar"); if (!bar) return;
      const run = this._run;
      if (!run) { bar.hidden = true; bar.innerHTML = ""; return; }
      bar.hidden = false;
      const c = run.counts || {};
      const done = (c.passed || 0) + (c.skipped || 0) + (c.failed || 0);
      const total = (run.nodes || []).length;
      const gates = (run.nodes || []).filter(n => n.kind === "gate" && n.status === "running");
      const failed = (run.nodes || []).filter(n => n.status === "failed");
      const live = (run.nodes || []).find(n => n.status === "queued" || n.status === "running");
      let html = `<div class="wf-run-head">
        <span class="wf-run-dot wf-st-${esc(run.status === "running" ? "running" : run.status === "passed" ? "passed" : run.status)}"></span>
        <b>Run #${run.id}</b> <span class="wf-run-s">${esc(run.status)}</span>
        <span class="wf-run-s">${done}/${total} steps</span>
        ${live && live.kind !== "gate" ? `<span class="wf-run-s">now: ${esc(live.label)}${live.work_item_id ? ` · item #${live.work_item_id}` : ""}</span>` : ""}
        <div style="flex:1"></div>
        ${run.status === "running" ? `<button class="qbtn small ghost" onclick="WF.cancelRun()">cancel run</button>` : ""}
      </div>`;
      gates.forEach(g => {
        html += `<div class="wf-gate">
          <span class="wf-gate-g">⏛</span>
          <span><b>${esc(g.label)}</b> is holding this run — a human has to decide.${
            g.detail ? ` <span class="wf-run-s">${esc(g.detail)}</span>` : ""}</span>
          <div style="flex:1"></div>
          <button class="qbtn small" onclick="WF.resolveGate('${esc(g.node_id)}','approve')">approve</button>
          <button class="qbtn small ghost" onclick="WF.resolveGate('${esc(g.node_id)}','reject')">reject</button>
        </div>`;
      });
      // A failure has to say WHY it failed — a consistency step that fails for
      // want of evidence reads differently from one that scored under its floor.
      failed.forEach(f => {
        html += `<div class="wf-fail"><b>${esc(f.label)}</b>${
          f.kind ? ` <span class="wf-run-s">${esc(f.kind)}</span>` : ""} — ${esc(f.detail || "failed with no reason recorded")}${
          f.work_item_id ? ` <span class="wf-run-s">item #${esc(f.work_item_id)}</span>` : ""}</div>`;
      });
      bar.innerHTML = html;
    },
    async resolveGate(nodeId, decision) {
      if (!this._run) return;
      const note = decision === "reject" ? (prompt("Why is this rejected?") || "") : "";
      const res = await post(`/api/workflows/runs/${this._run.id}/nodes/${encodeURIComponent(nodeId)}/approve`, { decision, note });
      const run = data(res);
      if (!run) { toast(errMsg(res), true); return; }
      toast(decision === "approve" ? "gate approved" : "gate rejected");
      this._paint(run);
      if (run.status === "running") this._track(run.id);
    },
    async cancelRun() {
      if (!this._run) return;
      const run = data(await post(`/api/workflows/runs/${this._run.id}/cancel`, {}));
      if (run) { clearTimeout(this._pollT); this._paint(run); toast("run cancelled"); }
    },
    _topoOrder(wf) {
      const ids = wf.nodes.map(n => n.id);
      const indeg = Object.fromEntries(ids.map(i => [i, 0]));
      const adj = Object.fromEntries(ids.map(i => [i, []]));
      (wf.edges || []).forEach(e => { if (adj[e.from[0]] && indeg[e.to[0]] != null) { adj[e.from[0]].push(e.to[0]); indeg[e.to[0]]++; } });
      const q = ids.filter(i => !indeg[i]); const out = [];
      while (q.length) { const i = q.shift(); out.push(i); (adj[i] || []).forEach(j => { if (--indeg[j] === 0) q.push(j); }); }
      ids.forEach(i => { if (!out.includes(i)) out.push(i); });   // cycles/leftovers
      return out;
    },
  };
  window.WF = WF;

  /* base universal steps so the builder is usable before plugins load */
  /* The task text is the run's north star: every step's brief quotes it. It used
     to be bound to a function declared in an inline <script> inside innerHTML —
     which the browser never executes — so the handler was undefined, nothing was
     ever stored, and EVERY workflow ran against "(no task text)". It binds to
     WF.set now, like every other field in the builder. */
  WF.registerStep({ type: "input.task", category: "input", label: "Task / complaint", glyph: "◎", accent: "var(--spark)",
    kind: "passive", defaults: { text: "" }, ports: () => ({ out: [{ id: "o", label: "task" }] }),
    body: n => `<div class="wf-b-note">${esc((n.config && n.config.text) || "the user's request…")}</div>`,
    config: (n) => `<div class="wf-insp-p">The task or complaint this workflow addresses. Every step's brief quotes it.</div>`
      + `<textarea class="wf-ta" placeholder="e.g. Scoville's hit-detection fires from behind" oninput="WF.set('${n.id}','text',this.value)">${esc((n.config && n.config.text) || "")}</textarea>` });
  WF.registerStep({ type: "input.reference", category: "input", label: "Reference", glyph: "▦", accent: "var(--c-art)",
    kind: "passive", defaults: { ref: "" }, ports: () => ({ out: [{ id: "o", label: "ref" }] }),
    body: n => (n.config && n.config.ref)
      ? (WF.refImg(n.config.ref) || `<div class="wf-b-note">${esc(n.config.ref)} — not a pinned reference</div>`)
      : `<div class="wf-b-note">pick a reference</div>`,
    config: (n, ctx) => `<div class="wf-insp-p">The anchor every downstream step conditions on. Pick a pinned reference — the pin is versioned, so <code>name@r2</code> holds this workflow to the revision it was designed against even after the anchor is re-pinned.</div>`
      + WF.refPicker(n.id, "ref", (n.config && n.config.ref) || "") });
  WF.registerStep({ type: "control.gate", category: "control", label: "Review gate", glyph: "⏛", accent: "var(--warn)",
    kind: "gate", defaults: {}, ports: () => ({ in: [{ id: "i", label: "" }], out: [{ id: "o", label: "ok" }] }),
    body: () => `<div class="wf-b-note">blocks until a human approves</div>`,
    config: () => `<div class="wf-insp-p">A real stop. When the run reaches this step it halts — no downstream step is queued — until a person approves or rejects it from the run bar above the canvas (or the pending-gates list). <b>Only a human can open it</b>; an agent calling the approval endpoint is refused. Rejecting fails the run.</div>` });

  if (!document.getElementById("wf-style")) {
    const s = document.createElement("style"); s.id = "wf-style";
    s.textContent = `
      .wf-lib{padding:6px 4px}
      .wf-lib-head{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:18px}
      .wf-eyebrow{font-family:var(--mono);font-size:9px;letter-spacing:.24em;text-transform:uppercase;color:var(--ash2)}
      .wf-h{font-size:18px;color:var(--bone);margin:4px 0 0}
      .wf-lib-sec{margin-bottom:22px}
      .wf-lib-cat{font-family:var(--mono);font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--ash);margin-bottom:10px}
      .wf-card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
      .wf-card{position:relative;display:flex;flex-direction:column;gap:4px;text-align:left;padding:16px;background:var(--plate);border:1px solid var(--seam);border-radius:12px;cursor:pointer;color:var(--bone);font:inherit}
      .wf-card:hover{border-color:var(--ember);background:var(--plate2)}
      .wf-card-g{font-size:18px;color:var(--ember)}
      .wf-card-t{font-size:13.5px;font-weight:600}
      .wf-card-h{font-size:11px;color:var(--ash)}
      .wf-card-x{position:absolute;top:8px;right:8px;color:var(--ash2);font-size:11px}
      .wf-card-x:hover{color:var(--bad)}
      /* builder */
      .wf-build{display:flex;flex-direction:column;height:100%}
      .wf-top{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--seam);border-bottom:0;border-radius:12px 12px 0 0;background:var(--iron)}
      .wf-name{background:transparent;border:1px solid transparent;border-radius:7px;color:var(--bone);font:inherit;font-size:14px;font-weight:600;padding:5px 8px;min-width:160px}
      .wf-name:hover,.wf-name:focus{border-color:var(--seam);background:var(--void);outline:none}
      .wf-cat{font-family:var(--mono);font-size:10px;color:var(--ash2);text-transform:uppercase;letter-spacing:.08em}
      /* run state */
      .wf-runbar{border:1px solid var(--seam);border-bottom:0;background:var(--void);padding:8px 10px;display:flex;flex-direction:column;gap:7px}
      .wf-run-head{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--bone)}
      .wf-run-dot{width:8px;height:8px;border-radius:50%;background:currentColor;box-shadow:0 0 0 3px color-mix(in srgb,currentColor 22%,transparent)}
      .wf-run-s{font-family:var(--mono);font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ash)}
      .wf-gate{display:flex;align-items:center;gap:9px;font-size:12px;color:var(--bone);background:var(--plate);border:1px solid var(--warn);border-radius:9px;padding:7px 10px}
      .wf-gate-g{color:var(--warn);font-size:14px}
      .wf-fail{font-size:12px;color:var(--bone);background:var(--plate);border:1px solid var(--bad);border-radius:9px;padding:7px 10px}
      .wf-st{display:inline-block;font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;padding:1px 7px;border:1px solid currentColor;border-radius:20px;margin-bottom:7px}
      .wf-st-pending,.wf-st-skipped{color:var(--ash2)}
      .wf-st-queued{color:var(--spark)}
      .wf-st-running{color:var(--ember)}
      .wf-st-passed{color:var(--good)}
      .wf-st-failed,.wf-st-cancelled{color:var(--bad)}
      .wf-main{flex:1;display:flex;border:1px solid var(--seam);border-radius:0 0 12px 12px;overflow:hidden;min-height:0}
      .wf-palette{width:186px;flex:none;background:var(--iron);border-right:1px solid var(--seam);padding:12px 10px;overflow-y:auto}
      .wf-pal-cat{font-family:var(--mono);font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--ash2);margin:12px 0 6px}
      .wf-pal-cat:first-child{margin-top:0}
      .wf-pi{display:flex;align-items:center;gap:8px;width:100%;text-align:left;padding:7px 9px;margin-bottom:5px;background:var(--plate);border:1px solid var(--seam);border-left:2px solid var(--a);border-radius:8px;color:var(--bone);font:inherit;font-size:12px;cursor:pointer}
      .wf-pi:hover{background:var(--plate2);border-color:var(--a)}
      .wf-pi .g{color:var(--a)}
      .wf-canvas{flex:1;position:relative;min-width:0}
      .wf-insp{width:270px;flex:none;background:var(--iron);border-left:1px solid var(--seam);padding:15px;overflow-y:auto}
      .wf-insp-empty{color:var(--ash2);font-size:12px}
      .wf-insp-h{font-size:13.5px;font-weight:600;color:var(--bone);margin-bottom:12px;display:flex;gap:8px;align-items:center}
      .wf-insp-p{font-size:12px;color:var(--ash);line-height:1.5;margin:6px 0}
      .wf-row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:8px 0;font-size:12px;color:var(--bone)}
      .wf-row label{color:var(--ash);font-size:12px}
      .wf-row input,.wf-row select,.wf-ta{background:var(--void);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:12px;padding:6px 8px}
      .wf-ta{width:100%;min-height:60px;resize:vertical;margin-top:6px}
      .wf-refpick{display:grid;grid-template-columns:repeat(auto-fill,minmax(76px,1fr));gap:7px;margin:8px 0}
      .wf-refcard{display:flex;flex-direction:column;gap:3px;padding:5px;background:var(--plate);border:1px solid var(--seam);border-radius:8px;cursor:pointer;color:var(--bone);font:inherit;text-align:left}
      .wf-refcard:hover{border-color:var(--ember)}
      .wf-refcard.sel{border-color:var(--ember);background:var(--plate2)}
      .wf-refcard img{width:100%;height:56px;object-fit:contain;background:#000;border-radius:5px}
      .wf-refnone{display:block;height:56px;line-height:56px;text-align:center;color:var(--ash2);background:#000;border-radius:5px}
      .wf-refname{font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .wf-refmeta{font-family:var(--mono);font-size:9px;color:var(--ash2)}
      .wf-b-note{font-size:11.5px;color:var(--ash);line-height:1.4}
      .wf-b-img{width:100%;height:78px;object-fit:contain;background:#000;border-radius:6px}
      .wf-b-tag{font-family:var(--mono);font-size:10px;color:var(--ash2)}
    `;
    document.head.appendChild(s);
  }
})();
