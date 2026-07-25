/* Studio — three visual editors built on the NodeCanvas engine:
 *   asset : the generation pipeline (Reference + Prompt → Generate → Candidate)
 *   agent : orchestration (queued tasks → seats → live agents)
 *   game  : a Godot-style editor workspace (viewport + files + run)
 * Frontend only; wired to the existing endpoints. window.Studio is the dispatcher.
 */
(function () {
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const get = async p => { try { const r = await fetch(p); return r.ok ? r.json() : {}; } catch (e) { return {}; } };
  const post = async (p, b) => { try { const r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) }); return r.json().catch(() => ({ ok: r.ok })); } catch (e) { return { ok: false }; } };
  const toast = (m, bad) => (window.BGWS ? BGWS.toast(m, bad) : console.log(m));

  /* Every flow this dispatcher can open, and where its module lives. The tab
     strip and the whitelist are BOTH derived from here plus whatever a plugin
     registered on window.StudioFlows — a hardcoded ["workflows","game"] made
     two finished flows (asset, agent) unreachable, and their script tags were
     never added either, so nothing registered them. Loading is lazy and
     idempotent; a module that fails to load falls back to the built-in below. */
  const MODULES = {
    asset: "/static/flow_asset.js",
    agent: "/static/flow_agent.js",
    game: "/static/flow_game.js",
  };
  // Flows this file implements itself — the fallback when a module is absent.
  const BUILTIN = {
    asset: { label: "Asset flow", icon: "assets" },
    agent: { label: "Agent flow", icon: "agents" },
    game: { label: "Game editor", icon: "gameplay" },
  };
  const CORE = { workflows: { label: "Workflows", icon: "studio" } };

  function loadScript(src) {
    return new Promise(resolve => {
      if (document.querySelector(`script[src="${src}"]`)) { resolve(false); return; }
      const s = document.createElement("script");
      s.src = src;
      s.onload = () => resolve(true);
      s.onerror = () => { console.warn("[studio] could not load", src); resolve(false); };
      document.head.appendChild(s);
    });
  }

  const Studio = {
    _flow: null, _nc: null, _loaded: null,

    // The registry, in tab order: the core builder, then every registered or
    // built-in flow. Adding a flow_*.js is all it takes to appear here.
    flows() {
      const reg = window.StudioFlows || {};
      const ids = Object.keys(CORE)
        .concat(Object.keys(BUILTIN).filter(id => !(id in CORE)))
        .concat(Object.keys(reg).filter(id => !(id in CORE) && !(id in BUILTIN)));
      return ids.map(id => {
        const meta = reg[id] || BUILTIN[id] || CORE[id] || {};
        return { id, label: meta.label || id, icon: meta.icon || "note" };
      });
    },
    _ensureModules() {
      if (!this._loaded) {
        this._loaded = Promise.all(Object.keys(MODULES).map(id =>
          ((window.StudioFlows || {})[id] ? Promise.resolve(false) : loadScript(MODULES[id]))));
      }
      return this._loaded;
    },
    async activate() {
      if (!this._flow) { try { this._flow = localStorage.getItem("studio-flow"); } catch (e) {} }
      await this._ensureModules();
      this._renderNav();
      const ids = this.flows().map(f => f.id);
      if (ids.indexOf(this._flow) === -1) this._flow = ids[0] || "workflows";
      this.select(this._flow);
    },
    _renderNav() {
      const nav = document.getElementById("studio-subnav");
      if (!nav) return;
      nav.innerHTML = this.flows().map(f =>
        `<button class="seat-tab ${f.id === this._flow ? "active" : ""}" data-flow="${esc(f.id)}"
           onclick="Studio.select('${esc(f.id)}')">${window.BGIcon ? BGIcon(f.icon, { size: 14 }) : ""}${esc(f.label)}</button>`).join("");
    },
    select(flow) {
      const ids = this.flows().map(f => f.id);
      if (ids.indexOf(flow) === -1) flow = ids[0] || "workflows";
      this._flow = flow;
      try { localStorage.setItem("studio-flow", flow); } catch (e) {}
      document.querySelectorAll("#studio-subnav .seat-tab").forEach(t => t.classList.toggle("active", t.dataset.flow === flow));
      const body = document.getElementById("studio-body");
      if (!body) return;
      body.innerHTML = ""; this._nc = null;
      try {
        if (flow === "workflows") {
          if (window.WF && WF.open) { WF.open(body, this._api()); return; }
          body.innerHTML = `<div class="empty">workflow builder not loaded</div>`; return;
        }
        const plugin = (window.StudioFlows || {})[flow];
        if (plugin && plugin.build) { plugin.build(body, this._api()); return; }
        if (typeof this[flow] === "function") { this[flow](body); return; }  // built-in
        body.innerHTML = `<div class="empty">no module registered for the “${esc(flow)}” flow</div>`;
      } catch (e) { body.innerHTML = `<div class="empty">studio error: ${esc(e.message)}</div>`; console.error(e); }
    },
    // Shared services handed to full flow modules (window.StudioFlows.<flow>).
    _api() {
      return { NodeCanvas: window.NodeCanvas, get, post, toast, esc,
               reselect: () => this.select(this._flow),
               setCanvas: nc => { this._nc = nc; } };
    },

    /* ══════════════════ ASSET FLOW ══════════════════ */
    async asset(host) {
      host.innerHTML = `<div class="st-wrap">
        <div class="st-palette">
          <div class="st-ph">Add node</div>
          <button class="st-pi" data-add="reference">▦ Reference</button>
          <button class="st-pi" data-add="prompt">✎ Prompt</button>
          <button class="st-pi" data-add="model">✦ Generate</button>
          <div class="st-ph" style="margin-top:16px">How it flows</div>
          <div class="st-hint">Wire <b>Reference</b> + <b>Prompt</b> into <b>Generate</b>. Running Generate dispatches the art agent with your prompt and anchored refs; new <b>Candidate</b>s land on the right as it produces them.</div>
        </div>
        <div class="st-canvas" id="st-canvas"></div>
        <div class="st-insp" id="st-insp"><div class="st-insp-empty">Select a node to inspect it.</div></div>
      </div>`;
      const [refs, arts] = await Promise.all([get("/api/refs"), get("/api/artifacts")]);
      const refList = (refs.refs || []).slice(0, 3);
      const cands = (arts.artifacts || []).filter(a => a.status === "candidate" || a.status === "approved").slice(0, 5);
      const nodes = [], edges = [];
      refList.forEach((r, i) => nodes.push({ id: "ref" + i, type: "reference", glyph: "▦", title: r.name, badge: r.kind, x: 30, y: 30 + i * 138, w: 176, data: r, ports: { out: [{ id: "o", label: "ref" }] } }));
      const py = 30 + refList.length * 138 + 10;
      nodes.push({ id: "prompt", type: "prompt", glyph: "✎", title: "Prompt", x: 30, y: py, w: 250, data: { text: "" }, ports: { out: [{ id: "o", label: "prompt" }] } });
      nodes.push({ id: "model", type: "model", glyph: "✦", title: "Generate · gpt-image", badge: "run", x: 360, y: 150, w: 236, ports: { in: [{ id: "ref", label: "refs" }, { id: "prompt", label: "prompt" }], out: [{ id: "o", label: "image" }] } });
      cands.forEach((a, i) => nodes.push({ id: "cand" + a.id, type: "candidate", glyph: "▣", title: a.logical_name, badge: a.status, x: 680, y: 30 + i * 150, w: 200, data: a, ports: { in: [{ id: "i", label: "in" }] } }));
      refList.forEach((r, i) => edges.push({ from: ["ref" + i, "o"], to: ["model", "ref"] }));
      edges.push({ from: ["prompt", "o"], to: ["model", "prompt"] });
      if (cands[0]) edges.push({ from: ["model", "o"], to: ["cand" + cands[0].id, "i"] });

      const nc = new NodeCanvas(document.getElementById("st-canvas"), {
        nodes, edges, accent: "var(--ember)",
        renderBody: n => this._assetBody(n),
        onSelect: n => this._assetInspect(n),
      });
      nc.mount(); nc.fit(); this._nc = nc;
      host.querySelectorAll(".st-pi").forEach(b => b.onclick = () => this._assetAdd(b.dataset.add));
    },
    // Pins are versioned files (<slug>.rN<suffix>) with a stored path, and the
    // suffix is whatever was pinned — jpg and webp render blank if you assume
    // .png. Always ask the ref for its own path.
    refRel(ref) {
      const p = String((ref && (ref.resolved_path || ref.path)) || "").replace(/\\/g, "/");
      const cut = p.indexOf(".bgate/");
      return cut === -1 ? p : p.slice(cut);
    },
    _assetBody(n) {
      if (n.type === "reference") {
        const rel = this.refRel(n.data);
        return rel ? `<img class="st-thumb" src="/api/preview?rel=${encodeURIComponent(rel)}" onerror="this.style.opacity=.12">` : "";
      }
      if (n.type === "candidate") {
        return `<img class="st-thumb" src="/api/preview?rel=${encodeURIComponent(n.data.path)}" onerror="this.style.opacity=.12">`;
      }
      if (n.type === "prompt") return `<textarea class="st-ta" oninput="Studio._promptText=this.value" placeholder="Describe the asset to generate…">${esc(n.data.text || "")}</textarea>`;
      if (n.type === "model") return `<button class="st-run" onclick="Studio.runGenerate(event)">▶ Run generate</button><div class="st-sub">dispatches the art agent</div>`;
      return "";
    },
    _assetInspect(n) {
      const insp = document.getElementById("st-insp");
      if (!insp) return;
      if (!n) { insp.innerHTML = `<div class="st-insp-empty">Select a node to inspect it.</div>`; return; }
      const kv = (k, v) => `<div class="st-kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`;
      let body = `<div class="st-insp-h">${n.glyph} ${esc(n.title)}</div>`;
      if (n.type === "candidate") {
        const a = n.data;
        body += `<img class="st-insp-img" src="/api/preview?rel=${encodeURIComponent(a.path)}" onerror="this.style.opacity=.12">`;
        body += kv("status", a.status) + kv("revision", "r" + a.revision) + kv("model", a.model || "—") + kv("producer", a.producer || "—");
        const qr = (a.metadata || {}).qa_review;
        if (qr) body += kv("QA verdict", `${qr.verdict} · ${qr.score}/100`);
        body += `<div class="st-insp-actions">
          <button class="qbtn small" onclick="Studio.reviewCandidate(${a.id},'approved')">approve</button>
          <button class="qbtn small ghost" onclick="Studio.reviewCandidate(${a.id},'rejected')">reject</button>
          <button class="qbtn small ghost" onclick="Studio.regen(${a.id})">regenerate</button></div>`;
      } else if (n.type === "reference") {
        const rel = this.refRel(n.data);
        if (rel) body += `<img class="st-insp-img" src="/api/preview?rel=${encodeURIComponent(rel)}" onerror="this.style.opacity=.12">`;
        body += kv("name", n.data.name) + kv("revision", "r" + (n.data.revision || 1)) +
                kv("kind", n.data.kind) + (n.data.note ? kv("note", n.data.note) : "");
      } else if (n.type === "model") {
        body += `<div class="st-insp-p">Connect references and a prompt, then run to dispatch the art seat. Candidates appear as the agent produces them.</div>`;
      } else if (n.type === "prompt") {
        body += `<div class="st-insp-p">The generation prompt. It becomes the art work item's brief.</div>`;
      }
      insp.innerHTML = body;
    },
    _assetAdd(type) {
      if (!this._nc) return;
      const id = type + "_" + Date.now().toString(36);
      const base = { reference: { glyph: "▦", title: "Reference", w: 176, ports: { out: [{ id: "o", label: "ref" }] }, data: {} },
        prompt: { glyph: "✎", title: "Prompt", w: 250, ports: { out: [{ id: "o", label: "prompt" }] }, data: { text: "" } },
        model: { glyph: "✦", title: "Generate · gpt-image", badge: "run", w: 236, ports: { in: [{ id: "ref", label: "refs" }, { id: "prompt", label: "prompt" }], out: [{ id: "o", label: "image" }] } } }[type];
      this._nc.addNode(Object.assign({ id, type, x: 120, y: 120 }, base));
    },
    async runGenerate(ev) {
      if (ev) ev.stopPropagation();
      const prompt = (this._promptText || "").trim();
      if (!prompt) { toast("write a prompt first", true); return; }
      const item = await post("/api/queue", { seat: "art", title: prompt.slice(0, 60), brief: "Generate art: " + prompt, priority: 3 });
      if (item && item.id) { await post(`/api/queue/${item.id}/dispatch`, {}); toast("art agent dispatched"); }
      else toast("could not queue", true);
    },
    // /react, not /review: a verdict has to leave a durable seat note (and a
    // live steer) behind it, or rejecting teaches the next agent nothing.
    async reviewCandidate(id, status) {
      const verdict = status === "approved" ? "like" : "dislike";
      const note = verdict === "dislike"
        ? (window.prompt("Reject — what is off-model? (the art seat keeps this)", "") || "")
        : "";
      const r = await post(`/api/artifacts/${id}/react`, { verdict, note });
      const err = (r && r.error && (r.error.message || r.error)) || (r && r.review_error) || null;
      toast(err ? String(err) : status, !!err);
      this.select("asset");
    },
    async regen(id) { await post(`/api/artifacts/${id}/regenerate`, { reason: "from studio" }); toast("regenerate queued"); },

    /* ══════════════════ AGENT FLOW ══════════════════ */
    async agent(host) {
      host.innerHTML = `<div class="st-wrap"><div class="st-canvas" id="st-canvas" style="flex:1"></div>
        <div class="st-insp" id="st-insp"><div class="st-insp-empty">Select a task or agent.</div></div></div>`;
      const [q, ag] = await Promise.all([get("/api/queue"), get("/api/agents")]);
      const live = new Set((ag.agents || []).filter(a => a.state === "running").map(a => a.item_id));
      const seatColors = { director: "#e8c05a", narrative: "#b083e8", gameplay: "#ff5c33", tech: "#4fa3ff", art: "#ff7ab8", audio: "#43d6a5", qa: "#9adb4f" };
      const items = (q.items || []).filter(i => i.status !== "done").slice(0, 10);
      const seats = [...new Set(items.map(i => i.seat))];
      const nodes = [], edges = [];
      seats.forEach((s, i) => nodes.push({ id: "seat_" + s, type: "seat", glyph: "◈", title: s.toUpperCase(), badge: live.has(items.find(x => x.seat === s && live.has(x.id)) && items.find(x => x.seat === s && live.has(x.id)).id) ? "live" : "", accent: seatColors[s] || "var(--ember)", x: 420, y: 30 + i * 120, w: 190, data: { seat: s }, ports: { in: [{ id: "i", label: "" }], out: [{ id: "o", label: "" }] } }));
      items.forEach((it, i) => {
        const running = live.has(it.id);
        nodes.push({ id: "task_" + it.id, type: "task", glyph: running ? "▶" : "▷", title: it.title, badge: running ? "running" : it.status, accent: running ? "var(--ember)" : "var(--seam2)", x: 30, y: 30 + i * 96, w: 240, data: it, ports: { out: [{ id: "o", label: "" }] } });
        edges.push({ from: ["task_" + it.id, "o"], to: ["seat_" + it.seat, "i"] });
      });
      const nc = new NodeCanvas(document.getElementById("st-canvas"), {
        nodes, edges, accent: "var(--ember)",
        renderBody: n => n.type === "task" ? `<div class="st-tmeta">${esc(n.data.source || "")} · ${esc(n.data.status)}</div>` : `<div class="st-tmeta">${(n.badge === "live") ? "● working" : "idle"}</div>`,
        onSelect: n => this._agentInspect(n, live),
      });
      nc.mount(); nc.fit(); this._nc = nc;
    },
    _agentInspect(n, live) {
      const insp = document.getElementById("st-insp"); if (!insp) return;
      if (!n) { insp.innerHTML = `<div class="st-insp-empty">Select a task or agent.</div>`; return; }
      if (n.type === "task") {
        const it = n.data, running = live && live.has(it.id);
        insp.innerHTML = `<div class="st-insp-h">${n.glyph} ${esc(it.title)}</div>
          <div class="st-kv"><span>seat</span><span>${esc(it.seat)}</span></div>
          <div class="st-kv"><span>status</span><span>${esc(it.status)}</span></div>
          <div class="st-insp-p">${esc((it.brief || "").slice(0, 300))}</div>
          <div class="st-insp-actions">
            ${it.status === "queued" ? `<button class="qbtn small" onclick="Studio.dispatchTask(${it.id})">dispatch</button>` : ""}
            <button class="qbtn small ghost" onclick="watchAgent(${it.id})">watch</button>
            ${running ? `<button class="qbtn small ghost" onclick="Studio.stopTask(${it.id})">stop</button>` : ""}
          </div>`;
      } else {
        insp.innerHTML = `<div class="st-insp-h">${n.glyph} ${esc(n.title)}</div><div class="st-insp-p">Seat. Tasks routed here run as dispatched agents.</div>`;
      }
    },
    async dispatchTask(id) { await post(`/api/queue/${id}/dispatch`, {}); toast("dispatched"); this.select("agent"); },
    async stopTask(id) { await post(`/api/queue/${id}/stop`, {}); toast("stop sent"); this.select("agent"); },

    /* ══════════════════ GAME EDITOR ══════════════════ */
    async game(host) {
      host.innerHTML = `<div class="st-game">
        <div class="st-gpanel">
          <div class="st-ph">Scene · scripts</div>
          <div class="st-tree" id="st-gtree"><div class="empty">loading…</div></div>
        </div>
        <div class="st-gviewport">
          <div class="st-gbar">
            <button class="qbtn small" onclick="Studio.gameBoot()">▶ boot build</button>
            <button class="qbtn small ghost" onclick="Studio.gameShot()">screenshot</button>
            <button class="qbtn small ghost" onclick="Studio.gameCheck()">build-check</button>
            <span class="st-gstat" id="st-gstat"></span>
          </div>
          <div class="st-gstage" id="st-gstage"><button class="playbtn" onclick="Studio.gameBoot()">▶ boot current build · F1 tuning</button></div>
        </div>
        <div class="st-insp" id="st-insp"><div class="st-insp-empty">Pick a script to view it.</div></div>
      </div>`;
      const st = await get("/api/godot/status");
      document.getElementById("st-gstat").textContent = st.available ? `Godot ${st.version || "detected"}` : "godot unavailable";
      const tree = await get("/api/godot/files?kind=.gd");
      const render = (nodes, d = 0) => (nodes || []).map(n => n.dir
        ? `<div class="st-tdir" style="padding-left:${d * 12}px">▸ ${esc(n.name)}</div>` + render(n.children, d + 1)
        : `<div class="st-tfile" style="padding-left:${d * 12 + 12}px" onclick="Studio.openScript('${esc(n.rel)}')">${esc(n.name)}</div>`).join("");
      document.getElementById("st-gtree").innerHTML = render(tree.tree) || `<div class="empty">no scripts</div>`;
    },
    gameBoot() {
      const stage = document.getElementById("st-gstage");
      if (stage) stage.innerHTML = `<iframe class="st-gframe" src="/play/?t=${Date.now()}" allow="autoplay; fullscreen"></iframe>`;
    },
    async gameShot() {
      document.getElementById("st-gstat").textContent = "capturing…";
      const r = await post("/api/godot/screenshot", {});
      const stage = document.getElementById("st-gstage");
      if (r.ok && r.rel && stage) stage.innerHTML = `<img class="st-gimg" src="/api/preview?rel=${encodeURIComponent(r.rel)}&t=${Date.now()}">`;
      document.getElementById("st-gstat").textContent = r.ok ? "screenshot" : "capture failed";
    },
    async gameCheck() {
      document.getElementById("st-gstat").textContent = "checking…";
      const r = await post("/api/godot/check", {});
      document.getElementById("st-gstat").textContent = r.ok ? "✓ build ok" : `✕ ${(r.errors || []).length} errors`;
    },
    async openScript(rel) {
      const insp = document.getElementById("st-insp");
      insp.innerHTML = `<div class="st-insp-empty">loading…</div>`;
      const d = await get("/api/godot/file?rel=" + encodeURIComponent(rel));
      insp.innerHTML = `<div class="st-insp-h">⚙ ${esc(rel.split("/").pop())}</div><pre class="st-code">${esc(d.text || "")}</pre>`;
    },
  };
  window.Studio = Studio;

  if (!document.getElementById("studio-style")) {
    const s = document.createElement("style"); s.id = "studio-style";
    s.textContent = `
      .st-wrap{display:flex;height:100%;gap:0;border:1px solid var(--seam);border-radius:12px;overflow:hidden;background:var(--iron)}
      .st-palette{width:190px;flex:none;background:var(--iron);border-right:1px solid var(--seam);padding:14px 12px;overflow-y:auto}
      .st-ph{font-family:var(--mono);font-size:9px;letter-spacing:.22em;text-transform:uppercase;color:var(--ash2);margin-bottom:8px}
      .st-pi{display:block;width:100%;text-align:left;padding:9px 11px;margin-bottom:6px;background:var(--plate);border:1px solid var(--seam);border-radius:9px;color:var(--bone);font:inherit;font-size:12.5px;cursor:pointer}
      .st-pi:hover{border-color:var(--ember);background:var(--plate2)}
      .st-hint{font-size:11.5px;color:var(--ash);line-height:1.5}.st-hint b{color:var(--bone)}
      .st-canvas{flex:1;position:relative;min-width:0}
      .st-insp{width:250px;flex:none;background:var(--iron);border-left:1px solid var(--seam);padding:15px;overflow-y:auto}
      .st-insp-empty{color:var(--ash2);font-size:12px}
      .st-insp-h{font-size:13px;font-weight:600;color:var(--bone);margin-bottom:12px}
      .st-insp-img{width:100%;border-radius:8px;background:#000;margin-bottom:10px}
      .st-insp-p{font-size:12px;color:var(--ash);line-height:1.5;margin-top:8px}
      .st-insp-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
      .st-kv{display:flex;justify-content:space-between;gap:10px;font-size:12px;padding:4px 0;border-bottom:1px solid var(--seam)}
      .st-kv span:first-child{color:var(--ash2);font-family:var(--mono);font-size:11px}
      .st-kv span:last-child{color:var(--bone)}
      .st-thumb{width:100%;height:96px;object-fit:contain;background:#000;border-radius:6px;display:block}
      .st-ta{width:100%;min-height:64px;resize:vertical;background:var(--void);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:12px;padding:7px}
      .st-run{width:100%;padding:9px;background:var(--ember);color:#111;border:0;border-radius:8px;font:inherit;font-weight:600;font-size:12px;cursor:pointer}
      .st-sub{text-align:center;font-size:10px;color:var(--ash2);margin-top:6px}
      .st-tmeta{font-family:var(--mono);font-size:10.5px;color:var(--ash)}
      /* game editor */
      .st-game{display:flex;height:100%;border:1px solid var(--seam);border-radius:12px;overflow:hidden;background:var(--iron)}
      .st-gpanel{width:220px;flex:none;border-right:1px solid var(--seam);padding:12px 10px;overflow-y:auto}
      .st-tree{font-family:var(--mono);font-size:11.5px}
      .st-tdir{color:var(--ash2);padding:3px 0}
      .st-tfile{color:var(--ash);padding:3px 0;cursor:pointer;border-radius:5px}
      .st-tfile:hover{color:var(--bone);background:var(--plate)}
      .st-gviewport{flex:1;display:flex;flex-direction:column;min-width:0}
      .st-gbar{display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid var(--seam)}
      .st-gstat{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--ash)}
      .st-gstage{flex:1;display:flex;align-items:center;justify-content:center;background:#050607;position:relative}
      .st-gframe{width:100%;height:100%;border:0}
      .st-gimg{max-width:100%;max-height:100%;object-fit:contain}
      .st-code{font-family:var(--mono);font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-word;color:#cdd6e4;margin-top:6px}
    `;
    document.head.appendChild(s);
  }
})();
