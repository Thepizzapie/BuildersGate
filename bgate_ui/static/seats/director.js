/* DIRECTOR seat — the orchestrator / control-tower workspace.
 *
 * Directly attacks "managing multiple agents is awful": a multi-agent cockpit
 * (live board of every running agent, each steerable + stoppable), a queue
 * board grouped by seat with dispatch + batch-dispatch, and a "review for
 * delegation" action that spawns a director agent to split a task across seats.
 *
 * Contract: window.SeatWS.director = { label, glyph, render(container, bg), refresh() }.
 * Nothing here may throw uncaught — every fetch is guarded, every handler wrapped.
 */
(function () {
  const BGICON = (n) => (window.BGIcon ? BGIcon(n, { size: 15 }) : "");
  "use strict";

  // Shared mutable state for this seat (single instance while mounted).
  const S = {
    bg: null,
    container: null,
    overview: { queue: {}, agents: [] },
    activity: {},        // item_id -> agent-activity json
    steerDrafts: {},     // item_id -> in-progress steer text
    selected: {},        // item_id -> true (batch selection, queued items)
    delegateWatch: null, // delegate item id most recently spawned (highlight)
    busy: false,
  };

  const STYLE = `
    .dir-wrap{display:flex;flex-direction:column;gap:18px;color:#e6e8ee;font-size:13px}
    .dir-sec{background:#101319;border:1px solid #1e232c;border-radius:12px;padding:14px 16px}
    .dir-sec-h{display:flex;align-items:center;gap:10px;margin:0 0 12px;font-size:12px;
      text-transform:uppercase;letter-spacing:.06em;color:#8a93a2}
    .dir-sec-h .dir-count{color:#3b7f9e;font-weight:600}
    .dir-sec-h .dir-spacer{flex:1}
    .dir-empty{color:#6b7280;font-size:12px;padding:10px 2px}
    /* agent board */
    .dir-agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
    .dir-acard{background:#0d1016;border:1px solid #242a35;border-radius:10px;padding:11px 12px;
      display:flex;flex-direction:column;gap:8px}
    .dir-acard.dir-deleg{border-color:#3b7f9e;box-shadow:0 0 0 1px rgba(59,127,158,.25)}
    .dir-ahead{display:flex;align-items:center;gap:8px}
    .dir-glyph{font-size:15px;width:20px;text-align:center;color:#3b7f9e}
    .dir-atitle{flex:1;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .dir-badge{font-size:10px;padding:2px 7px;border-radius:20px;text-transform:uppercase;letter-spacing:.04em}
    .dir-b-run{background:#16221a;color:#8fe0b0;border:1px solid #274a34}
    .dir-b-done{background:#152029;color:#7fb8d4;border:1px solid #244658}
    .dir-b-fail{background:#2a1616;color:#e79b9b;border:1px solid #5a2a2a}
    .dir-ameta{font-size:11px;color:#8a93a2;display:flex;gap:12px;flex-wrap:wrap}
    .dir-steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:3px;
      background:#07090d;border:1px solid #1a1f27;border-radius:7px;padding:7px 9px;max-height:120px;overflow:auto}
    .dir-step{font-size:11px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .dir-step .k{color:#5b6675;margin-right:5px}
    .dir-step.k-tool .k{color:#3b7f9e}
    .dir-step.k-steer .k{color:#c9a24a}
    .dir-step.k-result .k{color:#5f8a6e}
    .dir-steer{display:flex;gap:6px}
    .dir-steer-in{flex:1;min-width:0;padding:6px 8px;background:#12141a;border:1px solid #2b3a44;
      border-radius:7px;color:#e6e8ee;font:inherit;font-size:12px}
    .dir-arow{display:flex;gap:6px;align-items:center}
    /* buttons */
    .dir-btn{padding:6px 11px;background:#182029;border:1px solid #2b3a44;border-radius:7px;
      color:#cfe3ee;font:inherit;font-size:12px;cursor:pointer;white-space:nowrap}
    .dir-btn:hover{border-color:#3b7f9e}
    .dir-btn:disabled{opacity:.45;cursor:default}
    .dir-btn.dir-primary{background:#173241;border-color:#2f6f8c;color:#daf0fb}
    .dir-btn.dir-danger{background:#241618;border-color:#5a2a2a;color:#eaa}
    .dir-btn.dir-sm{padding:4px 8px;font-size:11px}
    /* queue board */
    .dir-cols{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;align-items:start}
    .dir-col{background:#0d1016;border:1px solid #1e232c;border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:8px}
    .dir-colh{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:600}
    .dir-colh .dir-spacer{flex:1}
    .dir-qcard{background:#12151c;border:1px solid #232a34;border-radius:8px;padding:8px 9px;display:flex;flex-direction:column;gap:6px}
    .dir-qcard.sel{border-color:#3b7f9e;background:#131b22}
    .dir-qtop{display:flex;align-items:flex-start;gap:7px}
    .dir-qtitle{flex:1;font-size:12px;line-height:1.35;cursor:pointer}
    .dir-qtitle:hover{color:#9fd2e6}
    .dir-qmeta{display:flex;gap:8px;align-items:center;font-size:10px;color:#7a8496}
    .dir-st{font-size:10px;padding:1px 6px;border-radius:12px;border:1px solid #2b333f;color:#9aa4b2}
    .dir-st.queued{color:#c9a24a;border-color:#4a3f22}
    .dir-st.dispatched{color:#8fe0b0;border-color:#274a34}
    .dir-st.done{color:#7fb8d4;border-color:#244658}
    .dir-st.failed{color:#e79b9b;border-color:#5a2a2a}
    .dir-qacts{display:flex;gap:5px;flex-wrap:wrap}
    .dir-chk{width:14px;height:14px;accent-color:#3b7f9e;margin-top:2px;cursor:pointer}
    .dir-batch{display:flex;align-items:center;gap:10px;padding:8px 12px;background:#131b22;
      border:1px solid #2f6f8c;border-radius:9px}
    .dir-batch b{color:#daf0fb}
  `;

  function e(html) {
    // Guarded element builder (falls back to BGWS.el, then a text node).
    try {
      if (S.bg && S.bg.el) return S.bg.el(html);
      const t = document.createElement("template"); t.innerHTML = String(html).trim();
      return t.content.firstChild;
    } catch (err) { return document.createTextNode(""); }
  }
  function esc(s) { try { return S.bg.esc(s); } catch (err) { return ""; } }
  function toast(m, bad) { try { S.bg.toast(m, bad); } catch (err) {} }
  function mounted() { return S.container && document.body.contains(S.container); }

  // ---- render -------------------------------------------------------------

  function render(container, bg) {
    S.bg = bg; S.container = container;
    S.activity = {}; S.overview = { queue: {}, agents: [] };
    container.innerHTML =
      `<style>${STYLE}</style>
       <div class="dir-wrap">
         <section class="dir-sec">
           <h3 class="dir-sec-h">${BGICON("agents")} Live agent board
             <span class="dir-count" id="dir-agent-count"></span>
             <span class="dir-spacer"></span>
             <button class="dir-btn dir-sm" onclick="DirCtl.refreshNow()">refresh</button>
           </h3>
           <div id="dir-agents"><div class="dir-empty">loading agents…</div></div>
         </section>
         <section class="dir-sec">
           <h3 class="dir-sec-h">Queue board — by seat
             <span class="dir-count" id="dir-queue-count"></span>
             <span class="dir-spacer"></span>
             <span id="dir-batch-slot"></span>
           </h3>
           <div id="dir-queue"><div class="dir-empty">loading queue…</div></div>
         </section>
       </div>`;
    // First paint.
    refresh();
  }

  // ---- refresh (poll) -----------------------------------------------------

  function refresh() {
    if (!mounted() || !S.bg) return;
    S.bg.get("/api/orchestrator/overview")
      .then(ov => {
        if (!mounted()) return;
        S.overview = {
          queue: (ov && ov.queue) || {},
          agents: (ov && ov.agents) || [],
        };
        renderQueue();
        return pullActivity();
      })
      .then(() => { if (mounted()) renderAgents(); })
      .catch(() => {
        // Overview failed (no project / API blip). Show an empty state, not a throw.
        if (!mounted()) return;
        const a = document.getElementById("dir-agents");
        const q = document.getElementById("dir-queue");
        if (a) a.innerHTML = `<div class="dir-empty">agent board unavailable right now</div>`;
        if (q) q.innerHTML = `<div class="dir-empty">queue unavailable right now</div>`;
      });
  }

  function pullActivity() {
    const running = (S.overview.agents || []).filter(a => a && a.state === "running");
    return Promise.all(running.map(a =>
      S.bg.get(`/api/agent-activity/${a.item_id}`)
        .then(act => { S.activity[a.item_id] = act || {}; })
        .catch(() => { /* keep last-known activity */ })
    ));
  }

  // Map of item_id -> item, from every seat column in the overview.
  function itemIndex() {
    const idx = {};
    const q = S.overview.queue || {};
    Object.keys(q).forEach(seat => {
      (q[seat] || []).forEach(it => { if (it && it.id != null) idx[it.id] = it; });
    });
    return idx;
  }

  // ---- agent board --------------------------------------------------------

  function renderAgents() {
    const host = document.getElementById("dir-agents");
    if (!host) return;
    // Don't clobber a steer box the user is typing in — repaint on the next tick.
    const ae = document.activeElement;
    if (ae && ae.classList && ae.classList.contains("dir-steer-in") && host.contains(ae)) {
      const cnt = document.getElementById("dir-agent-count");
      if (cnt) cnt.textContent = countRunning() ? `(${countRunning()} running)` : "";
      return;
    }
    const agents = S.overview.agents || [];
    const idx = itemIndex();
    const cnt = document.getElementById("dir-agent-count");
    if (cnt) cnt.textContent = agents.length ? `(${countRunning()} running)` : "";

    if (!agents.length) {
      host.innerHTML = `<div class="dir-empty">No agents running. Dispatch a queued item below, or "Review for delegation" to spin up a director.</div>`;
      return;
    }
    const grid = document.createElement("div");
    grid.className = "dir-agrid";
    agents.forEach(a => { try { grid.appendChild(agentCard(a, idx)); } catch (err) {} });
    host.innerHTML = "";
    host.appendChild(grid);
  }

  function countRunning() {
    return (S.overview.agents || []).filter(a => a && a.state === "running").length;
  }

  function agentCard(a, idx) {
    const id = a.item_id;
    const item = idx[id] || {};
    const seat = item.seat || "director";
    const glyph = (S.bg.glyphs && S.bg.glyphs[seat]) || "•";
    const title = item.title || `work item #${id}`;
    const isDeleg = item.source === "delegate" || id === S.delegateWatch;
    const running = a.state === "running";
    const act = S.activity[id] || {};
    const steps = Array.isArray(act.steps) ? act.steps.slice(-4) : [];
    const stepCount = act.step_count != null ? act.step_count : steps.length;

    let badge, badgeText;
    if (running) { badge = "dir-b-run"; badgeText = "running"; }
    else if (a.code === 0 || (act.final && act.final.subtype === "success")) { badge = "dir-b-done"; badgeText = "finished"; }
    else { badge = "dir-b-fail"; badgeText = a.state === "exited" ? "exited" : "stopped"; }

    const card = document.createElement("div");
    card.className = "dir-acard" + (isDeleg ? " dir-deleg" : "");

    const stepsHtml = steps.length
      ? steps.map(st => {
          const k = (st && st.kind) || "say";
          let body;
          if (k === "tool") body = `<span class="k">${esc(st.name || "tool")}</span>${esc(st.hint || "")}`;
          else if (k === "result") body = `<span class="k">→</span>${esc(st.text || "")}`;
          else if (k === "steer") body = `<span class="k">steer</span>${esc(st.text || "")}`;
          else body = `<span class="k">say</span>${esc(st.text || "")}`;
          return `<li class="dir-step k-${esc(k)}">${body}</li>`;
        }).join("")
      : `<li class="dir-step"><span class="k">…</span>no steps yet</li>`;

    const finalHtml = (!running && act.final && act.final.text)
      ? `<div class="dir-ameta">result: ${esc(String(act.final.text).slice(0, 140))}</div>` : "";

    card.innerHTML =
      `<div class="dir-ahead">
         <span class="dir-glyph">${esc(glyph)}</span>
         <span class="dir-atitle" title="${esc(title)}">${esc(title)}</span>
         <span class="dir-badge ${badge}">${esc(badgeText)}</span>
       </div>
       <div class="dir-ameta">
         <span>#${esc(id)} · ${esc(seat)}</span>
         <span>${esc(stepCount)} steps</span>
         ${a.pid ? `<span>pid ${esc(a.pid)}</span>` : ""}
         ${a.steers ? `<span>${esc(a.steers)} steers</span>` : ""}
       </div>
       <ul class="dir-steps">${stepsHtml}</ul>
       ${finalHtml}`;

    if (running) {
      const steer = document.createElement("div");
      steer.className = "dir-steer";
      const input = e(`<input class="dir-steer-in" placeholder="steer this agent…">`);
      if (input && input.tagName === "INPUT") {
        input.value = S.steerDrafts[id] || "";
        input.addEventListener("input", () => { S.steerDrafts[id] = input.value; });
        input.addEventListener("keydown", ev => { if (ev.key === "Enter") DirCtl.steer(id, input); });
      }
      const send = e(`<button class="dir-btn dir-primary dir-sm">steer</button>`);
      if (send) send.addEventListener("click", () => DirCtl.steer(id, input));
      const stop = e(`<button class="dir-btn dir-danger dir-sm">stop</button>`);
      if (stop) stop.addEventListener("click", () => DirCtl.stop(id));
      steer.appendChild(input); steer.appendChild(send); steer.appendChild(stop);
      card.appendChild(steer);
    }
    return card;
  }

  // ---- queue board --------------------------------------------------------

  function renderQueue() {
    const host = document.getElementById("dir-queue");
    if (!host) return;
    const q = S.overview.queue || {};
    const order = (S.bg.seats || Object.keys(q));
    Object.keys(q).forEach(s => { if (order.indexOf(s) < 0) order.push(s); });

    let total = 0, queuedTotal = 0;
    order.forEach(s => {
      const items = q[s] || [];
      total += items.length;
      queuedTotal += items.filter(i => i && i.status === "queued").length;
    });
    const cnt = document.getElementById("dir-queue-count");
    if (cnt) cnt.textContent = total ? `(${total} items · ${queuedTotal} queued)` : "";

    // Prune stale selections (items no longer queued/present).
    const present = {};
    order.forEach(s => (q[s] || []).forEach(i => { if (i) present[i.id] = i.status; }));
    Object.keys(S.selected).forEach(idk => {
      if (present[idk] !== "queued") delete S.selected[idk];
    });
    renderBatchBar();

    if (!total) {
      host.innerHTML = `<div class="dir-empty">Queue is empty. Add work from the queue view, or promote playtest feedback.</div>`;
      return;
    }
    const cols = document.createElement("div");
    cols.className = "dir-cols";
    order.forEach(seat => {
      const items = q[seat] || [];
      if (!items.length) return; // only show seats that have work
      try { cols.appendChild(queueColumn(seat, items)); } catch (err) {}
    });
    host.innerHTML = "";
    host.appendChild(cols);
  }

  function queueColumn(seat, items) {
    const glyph = (S.bg.glyphs && S.bg.glyphs[seat]) || "•";
    const queued = items.filter(i => i && i.status === "queued");
    const col = document.createElement("div");
    col.className = "dir-col";
    const head = document.createElement("div");
    head.className = "dir-colh";
    head.innerHTML =
      `<span class="dir-glyph">${esc(glyph)}</span><span>${esc(seat)}</span>
       <span class="dir-spacer"></span><span class="dir-qmeta">${esc(items.length)}</span>`;
    if (queued.length) {
      const all = e(`<button class="dir-btn dir-sm" title="dispatch all queued for ${esc(seat)}">dispatch all (${queued.length})</button>`);
      if (all) all.addEventListener("click", () => DirCtl.dispatchAll(seat));
      head.appendChild(all);
    }
    col.appendChild(head);
    items.forEach(it => { try { col.appendChild(queueCard(it)); } catch (err) {} });
    return col;
  }

  function queueCard(it) {
    const id = it.id;
    const status = it.status || "queued";
    const isQueued = status === "queued";
    const card = document.createElement("div");
    card.className = "dir-qcard" + (S.selected[id] ? " sel" : "");

    const top = document.createElement("div");
    top.className = "dir-qtop";
    if (isQueued) {
      const chk = e(`<input type="checkbox" class="dir-chk">`);
      if (chk) {
        chk.checked = !!S.selected[id];
        chk.addEventListener("change", () => {
          if (chk.checked) S.selected[id] = true; else delete S.selected[id];
          card.classList.toggle("sel", chk.checked);
          renderBatchBar();
        });
      }
      top.appendChild(chk);
    }
    const title = e(`<div class="dir-qtitle" title="click to focus this item across seats">${esc(it.title || ("#" + id))}</div>`);
    if (title) title.addEventListener("click", () => { try { S.bg.setActiveItem(id); toast("focused #" + id); } catch (err) {} });
    top.appendChild(title);
    card.appendChild(top);

    const meta = document.createElement("div");
    meta.className = "dir-qmeta";
    meta.innerHTML =
      `<span class="dir-st ${esc(status)}">${esc(status)}</span>
       <span>#${esc(id)}</span>
       ${it.priority ? `<span>p${esc(it.priority)}</span>` : ""}
       ${it.source && it.source !== "manual" ? `<span>${esc(it.source)}</span>` : ""}`;
    card.appendChild(meta);

    if (isQueued) {
      const acts = document.createElement("div");
      acts.className = "dir-qacts";
      const disp = e(`<button class="dir-btn dir-primary dir-sm">dispatch</button>`);
      if (disp) disp.addEventListener("click", () => DirCtl.dispatch(id, disp));
      const deleg = e(`<button class="dir-btn dir-sm" title="spawn a director agent to review + split this across seats">review for delegation</button>`);
      if (deleg) deleg.addEventListener("click", () => DirCtl.delegate(id, deleg));
      acts.appendChild(disp); acts.appendChild(deleg);
      card.appendChild(acts);
    }
    return card;
  }

  function renderBatchBar() {
    const slot = document.getElementById("dir-batch-slot");
    if (!slot) return;
    const ids = Object.keys(S.selected);
    if (!ids.length) { slot.innerHTML = ""; return; }
    slot.innerHTML = "";
    const bar = document.createElement("span");
    bar.className = "dir-batch";
    bar.innerHTML = `<b>${ids.length}</b> selected`;
    const go = e(`<button class="dir-btn dir-primary dir-sm">dispatch selected</button>`);
    if (go) go.addEventListener("click", () => DirCtl.dispatchSelected());
    const clr = e(`<button class="dir-btn dir-sm">clear</button>`);
    if (clr) clr.addEventListener("click", () => { S.selected = {}; renderQueue(); });
    bar.appendChild(go); bar.appendChild(clr);
    slot.appendChild(bar);
  }

  // ---- actions (window.DirCtl) -------------------------------------------

  const DirCtl = {
    refreshNow() { try { refresh(); } catch (err) {} },

    async dispatch(id, btn) {
      if (btn) btn.disabled = true;
      try {
        const r = await S.bg.post(`/api/queue/${id}/dispatch`, {});
        if (r && r.ok) { toast(`dispatched #${id} (pid ${r.pid || "?"})`); }
        else { toast((r && r.error) || `dispatch #${id} failed`, true); if (btn) btn.disabled = false; }
      } catch (err) { toast(`dispatch #${id} failed`, true); if (btn) btn.disabled = false; }
      refresh();
    },

    async dispatchAll(seat) {
      const items = (S.overview.queue[seat] || []).filter(i => i && i.status === "queued");
      if (!items.length) return;
      let ok = 0, fail = 0;
      for (const it of items) {
        try {
          const r = await S.bg.post(`/api/queue/${it.id}/dispatch`, {});
          if (r && r.ok) ok++; else fail++;
        } catch (err) { fail++; }
      }
      toast(`${seat}: dispatched ${ok}${fail ? `, ${fail} failed` : ""}`, fail > 0 && ok === 0);
      refresh();
    },

    async dispatchSelected() {
      const ids = Object.keys(S.selected);
      if (!ids.length) return;
      let ok = 0, fail = 0;
      for (const id of ids) {
        try {
          const r = await S.bg.post(`/api/queue/${id}/dispatch`, {});
          if (r && r.ok) { ok++; delete S.selected[id]; } else fail++;
        } catch (err) { fail++; }
      }
      toast(`batch: dispatched ${ok}${fail ? `, ${fail} failed` : ""}`, fail > 0 && ok === 0);
      refresh();
    },

    async delegate(id, btn) {
      if (btn) btn.disabled = true;
      try {
        const r = await S.bg.post("/api/orchestrator/delegate", { item_id: Number(id) });
        if (r && r.ok) {
          S.delegateWatch = r.delegate_item_id || null;
          toast(`delegating #${id} → director agent #${r.delegate_item_id}`);
        } else {
          toast((r && r.error) || `delegate #${id} failed`, true);
          if (btn) btn.disabled = false;
        }
      } catch (err) { toast(`delegate #${id} failed`, true); if (btn) btn.disabled = false; }
      refresh();
    },

    async steer(id, input) {
      const text = input && input.value != null ? String(input.value).trim() : (S.steerDrafts[id] || "").trim();
      if (!text) { toast("steer text is empty", true); return; }
      try {
        const r = await S.bg.post(`/api/queue/${id}/steer`, { text });
        if (r && r.ok) {
          toast(`steered #${id}`);
          S.steerDrafts[id] = "";
          if (input) input.value = "";
        } else { toast((r && r.error) || "steer failed", true); }
      } catch (err) { toast("steer failed", true); }
    },

    async stop(id) {
      try {
        const r = await S.bg.post(`/api/queue/${id}/stop`, {});
        if (r && r.ok) toast(`stopped #${id}`); else toast((r && r.error) || "stop failed", true);
      } catch (err) { toast("stop failed", true); }
      refresh();
    },
  };
  window.DirCtl = DirCtl;

  window.SeatWS = window.SeatWS || {};
  window.SeatWS.director = { label: "Director", glyph: BGICON("director"), render, refresh };
})();
