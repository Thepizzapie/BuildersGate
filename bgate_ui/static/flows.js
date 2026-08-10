/* Studio — visual editors built on the NodeCanvas engine:
 *   workflows : the workflow builder (WF)
 *   agent     : orchestration (queued tasks → seats → live agents)
 * Frontend only; wired to the existing endpoints. window.Studio is the dispatcher.
 *
 * Two flows were removed here. "Asset flow" duplicated the Assets library and
 * the art seat, which both do the same generate-and-review loop with the real
 * revision history behind it. "Game editor" could not edit anything — no save,
 * no write path — it read the Godot tree, screenshotted, and dispatched queue
 * items, all of which Playtests and Agents already do.
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
  // AGENT FLOW WAS REMOVED. It drew a second orchestration canvas over the same
  // data the Agents console already owns — two places to watch one floor, and
  // the one with the conversation, the queue and the live rails is the one that
  // survived. Its module is gone from the tree; nothing lazily loads here now.
  const MODULES = {};
  // Flows this file implements itself — the fallback when a module is absent.
  //
  // THE SPRITE EDITOR AND THE AUDIO MIXER LEFT. They were tabs here, which put
  // the art tools in a workspace that has nothing else to do with art and left
  // the art seat as a review-only surface that could point at a bad sprite and
  // not fix it. They are now mounted inside the seats that own that work —
  // SeatWS.art and SeatWS.audio call the same SpriteEdit.embed()/AudioLab.embed()
  // this used to. Deliberately NOT left behind as redirect tabs: a tab whose
  // only content is "it moved" is a third place to look for a tool with one home.
  const BUILTIN = {};
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
      // No tab here hosts an embedded editor any more — the art and audio seats
      // own those, and unembedding them from Studio would tear down a session
      // living in a workspace this dispatcher does not manage.
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

    /* ══════════════════ AGENT FLOW ══════════════════ */
    async agent(host) {
      host.innerHTML = `<div class="st-wrap"><div class="st-canvas" id="st-canvas" style="flex:1"></div>
        <div class="st-insp" id="st-insp"><div class="st-insp-empty">Select a task or agent.</div></div></div>`;
      const [q, ag] = await Promise.all([get("/api/queue"), get("/api/agents")]);
      const live = new Set((ag.agents || []).filter(a => a.state === "running").map(a => a.item_id));
      const seatColors = { director: "var(--c-director)", narrative: "var(--c-narrative)", gameplay: "var(--c-gameplay)", tech: "var(--c-tech)", art: "var(--c-art)", audio: "var(--c-audio)", cinematic: "var(--c-cinematic)", qa: "var(--c-qa)" };
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
      .st-insp-h{font-size:13px;font-weight:var(--fw-semi);color:var(--bone);margin-bottom:12px}
      .st-insp-img{width:100%;border-radius:8px;background:var(--bg);margin-bottom:10px}
      .st-insp-p{font-size:12px;color:var(--ash);line-height:1.5;margin-top:8px}
      .st-insp-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
      .st-kv{display:flex;justify-content:space-between;gap:10px;font-size:12px;padding:4px 0;border-bottom:1px solid var(--seam)}
      .st-kv span:first-child{color:var(--ash2);font-family:var(--mono);font-size:11px}
      .st-kv span:last-child{color:var(--bone)}
      .st-thumb{width:100%;height:96px;object-fit:contain;background:var(--bg);border-radius:6px;display:block}
      .st-ta{width:100%;min-height:64px;resize:vertical;background:var(--void);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:12px;padding:7px}
      .st-run{width:100%;padding:9px;background:var(--ember);color:var(--bg);border:0;border-radius:8px;font:inherit;font-weight:var(--fw-semi);font-size:12px;cursor:pointer}
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
      .st-gstage{flex:1;display:flex;align-items:center;justify-content:center;background:var(--bg);position:relative}
      .st-gframe{width:100%;height:100%;border:0}
      .st-gimg{max-width:100%;max-height:100%;object-fit:contain}
      .st-code{font-family:var(--mono);font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-word;color:var(--text);margin-top:6px}
    `;
    document.head.appendChild(s);
  }
})();
