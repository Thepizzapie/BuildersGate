/* Seat-workspace core: shared helpers + the shell dispatcher + a reusable
 * reference manager. Loaded before every seat module.
 *
 * MODULE CONTRACT — each static/seats/<seat>.js does:
 *   window.SeatWS = window.SeatWS || {};
 *   window.SeatWS.art = {
 *     label: "Art", glyph: "▲",
 *     render(container, bg) { ... },   // build the workspace into `container`
 *     refresh() { ... },               // optional; called ~every 3s while active
 *   };
 * `bg` is window.BGWS (helpers below). Never touch another seat's DOM.
 */
(function () {
  const BGWS = {
    async get(path) {
      const r = await fetch(path);
      if (!r.ok) throw new Error(`${r.status} ${await r.text().catch(() => "")}`);
      return r.json();
    },
    async post(path, body) {
      const r = await fetch(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      return r.json().catch(() => ({ ok: r.ok }));
    },
    async del(path) {
      const r = await fetch(path, { method: "DELETE" });
      return r.json().catch(() => ({ ok: r.ok }));
    },
    esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    },
    preview(rel) { return "/api/preview?rel=" + encodeURIComponent(rel); },
    el(html) {
      const t = document.createElement("template");
      t.innerHTML = String(html).trim();
      return t.content.firstChild;
    },
    fmtTime(t) {
      const s = Math.max(0, Number(t) || 0);
      return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
    },
    seats: ["director", "narrative", "gameplay", "tech", "art", "audio", "qa"],
    glyphs: { director: "◆", narrative: "¶", gameplay: "⌖", tech: "⚙", art: "▲", audio: "♪", qa: "✓" },
    toast(msg, bad) {
      let t = document.getElementById("bgws-toast");
      if (!t) {
        t = document.createElement("div"); t.id = "bgws-toast";
        t.style.cssText = "position:fixed;bottom:18px;left:50%;transform:translateX(-50%);z-index:9999;padding:9px 16px;border-radius:10px;font-size:13px;box-shadow:0 6px 24px rgba(0,0,0,.4);transition:opacity .3s";
        document.body.appendChild(t);
      }
      t.style.background = "var(--plate2)";
      t.style.color = bad ? "var(--bad)" : "var(--good)";
      t.style.border = "1px solid " + (bad ? "var(--bad)" : "var(--good)");
      t.textContent = msg; t.style.opacity = "1";
      clearTimeout(t._to); t._to = setTimeout(() => { t.style.opacity = "0"; }, 2600);
    },
    // The currently-selected work item, shared across seats (director sets it,
    // art/gameplay read it). Persisted so a reload keeps context.
    _item: (() => { try { return Number(localStorage.getItem("bgws-item")) || null; } catch (e) { return null; } })(),
    get activeItem() { return this._item; },
    setActiveItem(id) {
      this._item = id ? Number(id) : null;
      try { localStorage.setItem("bgws-item", this._item || ""); } catch (e) {}
      window.dispatchEvent(new CustomEvent("bgws-item", { detail: this._item }));
    },
  };
  window.BGWS = BGWS;

  /* ---- the per-seat status strip ------------------------------------------
   * Answers the three role-workspace questions on EVERY seat, above the
   * module's own workspace: what is this seat's agent doing NOW, what did it
   * recently produce, what is it blocked on. Data is the queue (per-seat work
   * items) + /api/agents (live dispatched sessions). Rendered by the shell so
   * a seat module can't opt out or drift. */
  const SeatStrip = {
    _seat: null,
    async paint(host, seat) {
      this._seat = seat;
      if (!host) return;
      let items = [], agents = [];
      try {
        const [q, a] = await Promise.all([
          BGWS.get("/api/queue").catch(() => ({ items: [] })),
          BGWS.get("/api/agents").catch(() => ({ agents: [] })),
        ]);
        items = (q.items || []).filter(it => it.seat === seat);
        agents = a.agents || [];
      } catch (e) { /* strip paints its offline state below */ }
      // A repaint can resolve after the user switched seats — never stomp it.
      if (this._seat !== seat || !host.isConnected) return;
      const live = {};
      agents.forEach(ag => { if (ag.state === "running") live[ag.item_id] = ag; });
      const current = items.filter(it => it.status === "dispatched");
      const byUpdated = (x, y) => String(y.updated_at || "").localeCompare(String(x.updated_at || ""));
      const outputs = items.filter(it => it.status === "done").sort(byUpdated).slice(0, 3);
      const blockers = items.filter(it => it.status === "failed").sort(byUpdated).slice(0, 3);
      const queued = items.filter(it => it.status === "queued").length;

      let state, dot;
      if (current.length) { state = current.some(it => live[it.id]) ? "working" : "dispatched"; dot = "#8fd6a8"; }
      else if (blockers.length) { state = "blocked"; dot = "#e0524a"; }
      else { state = "idle"; dot = "#3a4350"; }

      const itemLine = it => {
        const ag = live[it.id];
        const tail = ag && ag.last_output_s != null ? ` · output ${Math.round(ag.last_output_s)}s ago` : "";
        return `<div class="sst-item" title="${BGWS.esc((it.result || it.brief || "").slice(0, 300))}">` +
          `<span class="sst-id">#${it.id}</span>${BGWS.esc((it.title || "").slice(0, 64))}${BGWS.esc(tail)}</div>`;
      };
      const cell = (label, inner) =>
        `<div class="sst-cell"><div class="sst-h">${label}</div>${inner}</div>`;
      host.innerHTML = `
        <style>
          .sst{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start;background:#101319;border:1px solid #1e232c;border-radius:12px;padding:10px 14px;margin-bottom:12px;font-size:12px;color:#c4cbd6}
          .sst-cell{min-width:150px;flex:1}
          .sst-h{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#7a8494;margin-bottom:4px}
          .sst-state{display:flex;align-items:center;gap:7px;font-weight:600}
          .sst-dot{width:9px;height:9px;border-radius:50%;display:inline-block}
          .sst-item{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:340px;line-height:1.5}
          .sst-id{color:#7a8494;margin-right:6px;font-variant-numeric:tabular-nums}
          .sst-none{color:#5f6b7a}
          .sst-bad .sst-item{color:#f0b3b3}
          .sst-q{color:#7a8494;font-size:11px;margin-top:3px}
        </style>
        <div class="sst">
          ${cell("agent", `<div class="sst-state"><span class="sst-dot" style="background:${dot}"></span>${state}</div>` +
            (queued ? `<div class="sst-q">${queued} queued</div>` : ""))}
          ${cell("current task", current.length ? current.map(itemLine).join("")
            : `<div class="sst-none">none — queue work to this seat to dispatch an agent</div>`)}
          ${cell("recent output", outputs.length ? outputs.map(itemLine).join("")
            : `<div class="sst-none">nothing completed yet</div>`)}
          ${cell("blocked on", blockers.length
            ? `<div class="sst-bad">${blockers.map(itemLine).join("")}</div>`
            : `<div class="sst-none">nothing</div>`)}
        </div>`;
    },
    refresh() {
      const host = document.getElementById("seat-strip");
      if (host && this._seat) this.paint(host, this._seat);
    },
  };
  window.SeatStrip = SeatStrip;

  /* ---- the seat shell: sub-nav + render dispatch ------------------------- */
  const SeatShell = {
    current: null,
    activate() {
      const reg = window.SeatWS || {};
      const nav = document.getElementById("seat-subnav");
      if (!nav) return;
      const order = BGWS.seats.filter(s => reg[s]);
      Object.keys(reg).forEach(s => { if (!order.includes(s)) order.push(s); });
      if (!this.current || !reg[this.current]) {
        try { this.current = localStorage.getItem("bgws-seat"); } catch (e) {}
        if (!reg[this.current]) this.current = order[0] || null;
      }
      nav.innerHTML = order.map(s =>
        `<button class="seat-tab ${s === this.current ? "active" : ""}" data-seat="${s}"
           onclick="SeatShell.select('${s}')">${BGWS.glyphs[s] || "•"} ${BGWS.esc((reg[s].label) || s)}</button>`
      ).join("") || '<span class="empty">no seat modules loaded</span>';
      this.render();
    },
    select(seat) {
      this.current = seat;
      try { localStorage.setItem("bgws-seat", seat); } catch (e) {}
      document.querySelectorAll(".seat-tab").forEach(t =>
        t.classList.toggle("active", t.dataset.seat === seat));
      this.render();
    },
    render() {
      const body = document.getElementById("seat-body");
      const mod = (window.SeatWS || {})[this.current];
      if (!body || !mod) { if (body) body.innerHTML = '<div class="empty">pick a seat</div>'; return; }
      body.innerHTML = "";
      // The shared status strip renders above every seat's workspace; the
      // module gets its own child container so neither can wipe the other.
      const strip = document.createElement("div");
      strip.id = "seat-strip";
      const ws = document.createElement("div");
      ws.id = "seat-ws";
      body.appendChild(strip);
      body.appendChild(ws);
      SeatStrip.paint(strip, this.current);
      try { mod.render(ws, BGWS); }
      catch (e) { ws.innerHTML = `<div class="empty">workspace error: ${BGWS.esc(e.message)}</div>`; console.error(e); }
    },
    refresh() {
      const mod = (window.SeatWS || {})[this.current];
      if (document.getElementById("view-seats") && !document.getElementById("view-seats").hidden) {
        try { SeatStrip.refresh(); } catch (e) {}
        if (mod && typeof mod.refresh === "function") {
          try { mod.refresh(); } catch (e) {}
        }
      }
    },
  };
  window.SeatShell = SeatShell;
  setInterval(() => SeatShell.refresh(), 3000);

  /* ---- RefManager: reusable global + per-task reference panel ------------ */
  /* Usage: RefManager.mount(containerEl, { itemId }) — itemId optional (null =
     global refs only). Renders global pins + task anchors with add/upload/remove. */
  window.RefManager = {
    async mount(container, opts) {
      opts = opts || {};
      const itemId = opts.itemId || null;
      const [g, t] = await Promise.all([
        BGWS.get("/api/refs").catch(() => ({ refs: [] })),
        itemId ? BGWS.get(`/api/tasks/${itemId}/refs`).catch(() => ({ anchored: [], resolved: [] })) : Promise.resolve({ anchored: [] }),
      ]);
      const card = (r, scope) => {
        const path = r.resolved_path || r.path;
        const img = path ? `<img src="${BGWS.preview(scope === "task" ? path.replace(/^.*[\\/]\.bgate/, ".bgate") : path)}" onerror="this.style.opacity=.2">` : "";
        const del = scope === "task"
          ? `<button class="rm-x" title="remove anchor" onclick="RefManager._rmTask(${itemId},'${BGWS.esc(r.ref)}',this)">✕</button>`
          : `<button class="rm-x" title="unpin globally" onclick="RefManager._rmGlobal('${BGWS.esc(r.name)}',this)">✕</button>`;
        return `<div class="rm-card s-${scope}">${img}<div class="rm-meta"><b>${BGWS.esc(r.ref || r.name)}</b><span>${BGWS.esc(r.kind || "")}</span></div>${del}</div>`;
      };
      container.innerHTML = `
        <style>
          .rm-wrap{display:flex;flex-direction:column;gap:12px}
          .rm-h{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--ash);margin-bottom:6px}
          .rm-grid{display:flex;flex-wrap:wrap;gap:8px}
          .rm-card{position:relative;width:96px;background:var(--plate);border:1px solid var(--seam);border-radius:8px;overflow:hidden}
          .rm-card.s-task{border-color:var(--ember)}
          .rm-card img{width:100%;height:74px;object-fit:contain;background:var(--void);display:block}
          .rm-meta{padding:4px 6px;font-size:11px;line-height:1.3}.rm-meta b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rm-meta span{color:var(--ash);font-size:10px}
          .rm-x{position:absolute;top:3px;right:3px;width:18px;height:18px;border:0;border-radius:4px;background:rgba(0,0,0,.6);color:var(--bad);cursor:pointer;font-size:11px;line-height:1}
          .rm-add{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:4px}
          .rm-add input,.rm-add select{padding:6px 8px;background:var(--plate2);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:12px}
          .rm-drop{border:1px dashed var(--seam);border-radius:8px;padding:8px 12px;color:var(--ash);font-size:12px;cursor:pointer}
        </style>
        <div class="rm-wrap">
          ${itemId ? `<div><div class="rm-h">task anchors — this work item (priority over global)</div>
            <div class="rm-grid" id="rm-task">${(t.anchored || []).map(r => card(r, "task")).join("") || '<span class="empty">no task anchors yet</span>'}</div>
            <div class="rm-add">
              <input id="rm-task-ref" placeholder="pin name or path (e.g. tommy-bright16)" style="flex:1;min-width:180px">
              <select id="rm-task-kind"><option>style</option><option>character</option><option>ui</option><option>concept</option></select>
              <button class="qbtn small" onclick="RefManager._addTask(${itemId})">anchor to task</button>
            </div></div>` : ""}
          <div><div class="rm-h">global project references</div>
            <div class="rm-grid" id="rm-global">${(g.refs || []).map(r => card(r, "global")).join("") || '<span class="empty">no global refs</span>'}</div>
            <div class="rm-add">
              <input id="rm-g-name" placeholder="ref name">
              <select id="rm-g-kind"><option>style</option><option>character</option><option>ui</option><option>concept</option></select>
              <label class="rm-drop" id="rm-drop">drop / choose image to upload<input type="file" accept="image/*" style="display:none" id="rm-file" onchange="RefManager._upload(event,${itemId || "null"})"></label>
            </div></div>
        </div>`;
      const drop = container.querySelector("#rm-drop");
      if (drop) {
        drop.addEventListener("click", () => container.querySelector("#rm-file").click());
        drop.addEventListener("dragover", e => { e.preventDefault(); drop.style.borderColor = "var(--ember)"; });
        drop.addEventListener("drop", e => { e.preventDefault(); drop.style.borderColor = "var(--seam)"; this._uploadFiles(e.dataTransfer.files, itemId, container); });
      }
      this._container = container; this._opts = opts;
    },
    _reload() { if (this._container) this.mount(this._container, this._opts); },
    async _addTask(itemId) {
      const ref = document.getElementById("rm-task-ref").value.trim();
      const kind = document.getElementById("rm-task-kind").value;
      if (!ref) return;
      const r = await BGWS.post(`/api/tasks/${itemId}/refs`, { ref, kind });
      if (r.ok) { BGWS.toast("anchored " + ref); this._reload(); } else BGWS.toast(r.error || "failed", true);
    },
    async _rmTask(itemId, ref, btn) { await BGWS.del(`/api/tasks/${itemId}/refs?ref=${encodeURIComponent(ref)}`); this._reload(); },
    async _rmGlobal(name, btn) { await BGWS.del(`/api/refs/${encodeURIComponent(name)}`); this._reload(); },
    _upload(ev, itemId) { this._uploadFiles(ev.target.files, itemId, this._container); },
    async _uploadFiles(files, itemId, container) {
      const f = files && files[0]; if (!f) return;
      const name = (container.querySelector("#rm-g-name").value.trim()) || f.name.replace(/\.[^.]+$/, "");
      const kind = container.querySelector("#rm-g-kind").value;
      const data = await new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(f); });
      const resp = await BGWS.post("/api/refs/upload", { name, kind, data });
      if (resp.ok || resp.name) { BGWS.toast("pinned " + name); this._reload(); } else BGWS.toast(resp.error || "upload failed", true);
    },
  };
})();
