/* Studio — a direct generator graph built on NodeCanvas. It owns no seats,
 * queue items, or agent sessions; orchestration remains on its own screen. */
(function () {
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const get = async p => { try { const r = await fetch(p); return r.ok ? r.json() : {}; } catch (e) { return {}; } };
  /* A refusal from this helper used to be a bare {ok:false} with no sentence in
     it, and the callers below toasted success unconditionally anyway — so a
     dispatch the server declined (concurrency cap, dirty tree, a chain link
     whose predecessor has not landed) reported "dispatched" and the item simply
     never moved. The body already carries the reason in both of this repo's
     conventions; carry it out under one key so a caller has something to say. */
  const why = (body, status) => {
    const e = body && body.error;
    if (e && typeof e === "object") return e.message || e.code || "";
    if (typeof e === "string" && e) return e;
    if (body && typeof body.detail === "string" && body.detail) return body.detail;
    return `request failed - ${status}`;
  };
  const post = async (p, b) => {
    let r, body = null;
    try {
      r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
    } catch (e) {
      return { ok: false, error: "the dashboard is not reachable" };
    }
    try { body = await r.json(); } catch (e) { body = null; }
    if (!r.ok || (body && body.ok === false)) {
      return { ok: false, error: why(body, r.status) };
    }
    return body || { ok: r.ok };
  };
  const toast = (m, bad) => (window.BGWS ? BGWS.toast(m, bad) : console.log(m));

  /* Every flow this dispatcher can open. The tab strip and the whitelist are
     BOTH derived from here plus whatever a plugin registered on
     window.StudioFlows — a hardcoded ["workflows","game"] made two finished
     flows unreachable, and their script tags were never added either, so
     nothing registered them.

     THERE ARE NO LAZY MODULES AND NO BUILT-IN FLOWS LEFT. The agent flow drew a
     second orchestration canvas over the data the Agents console already owns;
     the sprite editor and audio mixer moved into the art and audio seats, which
     is where that work lives. The empty MODULES/BUILTIN maps they left behind,
     and the lazy loadScript() that could only ever iterate an empty map, are
     gone with them — a registered plugin supplies its own build(). */
  const CORE = { workflows: { label: "Generator graph", icon: "studio" } };

  const Studio = {
    _flow: null, _nc: null,

    // The registry, in tab order: the core builder, then every registered flow.
    flows() {
      const reg = window.StudioFlows || {};
      const ids = Object.keys(CORE)
        .concat(Object.keys(reg).filter(id => !(id in CORE)));
      return ids.map(id => {
        const meta = reg[id] || CORE[id] || {};
        return { id, label: meta.label || id, icon: meta.icon || "note" };
      });
    },
    async activate() {
      if (!this._flow) { try { this._flow = localStorage.getItem("studio-flow"); } catch (e) {} }
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
        body.innerHTML = `<div class="empty">no module registered for the “${esc(flow)}” flow</div>`;
      } catch (e) { body.innerHTML = `<div class="empty">studio error: ${esc(e.message)}</div>`; console.error(e); }
    },
    // Shared services handed to full flow modules (window.StudioFlows.<flow>).
    _api() {
      return { NodeCanvas: window.NodeCanvas, get, post, toast, esc,
               setCanvas: nc => { this._nc = nc; } };
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
