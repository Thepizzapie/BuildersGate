/* WF — the workflow builder core.
 *
 * A workflow is a reusable, composable process the agents follow for a task-type:
 * a graph of typed STEPS (inputs, asset-generation, agents, control/QA) on the
 * NodeCanvas engine. Step types + starter templates are contributed by plugin
 * files (wf_steps_*.js) via WF.registerStep / WF.registerTemplate. This core owns
 * the library, the builder UI, persistence, and Run (translate → dispatch agents).
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
 *   });
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

  const WF = {
    steps: {}, templates: [], _nc: null, _wf: null, _saved: [], _api: null, _saveT: null,

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
    async deleteSaved(id) {
      this._saved = this._saved.filter(s => s.id !== id);
      await post("/api/workspace/studio/wf-index", { data: { list: this._saved } });
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
        <div class="wf-main">
          <div class="wf-palette" id="wf-palette"></div>
          <div class="wf-canvas" id="wf-canvas"></div>
          <div class="wf-insp" id="wf-insp"><div class="wf-insp-empty">Select a step to configure it.</div></div>
        </div>
      </div>`;
      this._renderPalette();
      this._mountCanvas();
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
        renderBody: n => { try { return this._stepDef(n.type).body ? this._stepDef(n.type).body(n) : ""; } catch (e) { return ""; } },
        onSelect: n => this._inspect(n),
        onConnect: (from, to) => { this._wf.edges = nc.edges.slice(); this.persist(); },
        onNodeMove: n => { if (n) { const w = (this._wf.nodes || []).find(x => x.id === n.id); if (w) { w.x = n.x; w.y = n.y; } } this._wf.edges = nc.edges.slice(); this.persist(); },
        onNodeRemove: id => { this._wf.nodes = (this._wf.nodes || []).filter(n => n.id !== id); this._wf.edges = nc.edges.slice(); this.persist(); },
      });
      nc.mount(); nc.fit(); this._nc = nc;
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

    /* ---- run: hand the ordered workflow to the director to orchestrate --- */
    async run() {
      const wf = this._serialize();
      const order = this._topoOrder(wf);
      const steps = order.map(id => wf.nodes.find(n => n.id === id)).filter(Boolean);
      const agentSteps = steps.filter(n => this._stepDef(n.type).agentSeat);
      if (!agentSteps.length) { toast("no agent/generation steps to run", true); return; }
      const taskNode = wf.nodes.find(n => n.type === "input.task");
      const taskText = (taskNode && taskNode.config && taskNode.config.text) || "(no task text — see the workflow name)";
      const plan = steps.map((n, i) => {
        const def = this._stepDef(n.type);
        const seat = def.agentSeat ? `[${def.agentSeat}]` : "[gate/data]";
        let brief = ""; try { brief = def.toBrief ? def.toBrief(n, wf) : ""; } catch (e) {}
        return `${i + 1}. ${def.label} ${seat}${brief ? " — " + brief : ""}`;
      }).join("\n");
      const brief =
        `Execute the workflow "${wf.name}" for this task/complaint:\n"${taskText}"\n\n` +
        `Run the steps IN ORDER below. For each AGENT step, carry out (or queue_add + dispatch) that seat's work with the given brief. ` +
        `For a consistency/test/review gate, do not proceed until it passes; a FAIL means loop back and redo the prior art/gameplay step, then re-check. ` +
        `Keep the task/complaint as the north star and report what each step produced.\n\nSteps:\n${plan}\n\n` +
        `When the whole workflow is satisfied, queue_complete with a summary of the run.`;
      const item = await post("/api/queue", { seat: "director", title: `Run: ${wf.name}`.slice(0, 80), brief, priority: 4, source: "workflow" });
      if (item && item.id) { await post(`/api/queue/${item.id}/dispatch`, {}); toast(`workflow handed to the director (item #${item.id})`); }
      else toast("could not start workflow", true);
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
  WF.registerStep({ type: "input.task", category: "input", label: "Task / complaint", glyph: "◎", accent: "var(--spark)",
    defaults: { text: "" }, ports: () => ({ out: [{ id: "o", label: "task" }] }),
    body: n => `<div class="wf-b-note">${esc((n.config && n.config.text) || "the user's request…")}</div>`,
    config: (n, ctx) => `<div class="wf-insp-p">The task or complaint this workflow addresses.</div><textarea class="wf-ta" oninput="n_taskset(this.value)" placeholder="e.g. Scoville's hit-detection fires from behind">${esc((n.config && n.config.text) || "")}</textarea><script>window.n_taskset=function(v){n=WF._nc.nodes.get('${n.id}');if(n){n.config=n.config||{};n.config.text=v;WF.persist();}}<\/script>` });
  WF.registerStep({ type: "input.reference", category: "input", label: "Reference", glyph: "▦", accent: "var(--c-art)",
    defaults: { ref: "" }, ports: () => ({ out: [{ id: "o", label: "ref" }] }),
    body: n => (n.config && n.config.ref) ? `<img class="wf-b-img" src="/api/preview?rel=${encodeURIComponent(".bgate/refs/" + n.config.ref + ".png")}" onerror="this.style.opacity=.12">` : `<div class="wf-b-note">pick a reference</div>`,
    config: (n, ctx) => `<div class="wf-insp-p">A reference image / anchor for downstream steps.</div>` });
  WF.registerStep({ type: "control.gate", category: "control", label: "Review gate", glyph: "⏛", accent: "var(--warn)",
    defaults: { mode: "human" }, ports: () => ({ in: [{ id: "i", label: "" }], out: [{ id: "o", label: "ok" }] }),
    body: n => `<div class="wf-b-note">pause for ${esc((n.config && n.config.mode) || "human")} approval</div>`,
    config: (n) => `<div class="wf-insp-p">Holds until approved — human review or an automatic check.</div>` });

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
      .wf-b-note{font-size:11.5px;color:var(--ash);line-height:1.4}
      .wf-b-img{width:100%;height:78px;object-fit:contain;background:#000;border-radius:6px}
      .wf-b-tag{font-family:var(--mono);font-size:10px;color:var(--ash2)}
    `;
    document.head.appendChild(s);
  }
})();
