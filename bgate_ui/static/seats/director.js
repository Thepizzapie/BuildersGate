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
    mode: "board",       // "board" (control tower) | "brainstorm"
  };

  const STYLE = `
    .dir-wrap{display:flex;flex-direction:column;gap:var(--s-6);color:var(--text);font-size:13px}
    /* The two sections are .spanel + .sec-h out of app.css. They used to be a
       private .dir-sec box with a .dir-sec-h line of uppercase text inside it,
       which is a header that is only a FONT SIZE away from the cards under it -
       the control tower read as one continuous field of grey boxes. Nothing
       local is left: the band, the icon, the count pill and the actions slot
       are all the shared classes now. */
    .dir-empty{color:var(--text-3);font-size:var(--fs-sm);padding:var(--s-5) var(--s-1);line-height:var(--lh)}
    /* agent board */
    .dir-agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
    .dir-acard{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:11px 12px;
      display:flex;flex-direction:column;gap:8px}
    .dir-acard.dir-deleg{border-color:var(--accent);box-shadow:0 0 0 1px rgba(59,127,158,.25)}
    .dir-ahead{display:flex;align-items:center;gap:8px}
    .dir-glyph{font-size:15px;width:20px;text-align:center;color:var(--accent)}
    .dir-atitle{flex:1;font-weight:var(--fw-semi);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .dir-badge{font-size:10px;padding:2px 7px;border-radius:20px;text-transform:uppercase;letter-spacing:.04em}
    .dir-b-run{background:var(--good-soft);color:var(--good);border:1px solid var(--good-line)}
    .dir-b-done{background:var(--surface-2);color:var(--text);border:1px solid var(--accent-line)}
    .dir-b-fail{background:var(--bad-soft);color:var(--bad);border:1px solid var(--bad-line)}
    .dir-ameta{font-size:11px;color:var(--text-3);display:flex;gap:12px;flex-wrap:wrap}
    .dir-steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:3px;
      background:var(--bg);border:1px solid var(--line-soft);border-radius:7px;padding:7px 9px;max-height:120px;overflow:auto}
    .dir-step{font-size:11px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .dir-step .k{color:var(--text-3);margin-right:5px}
    .dir-step.k-tool .k{color:var(--accent)}
    .dir-step.k-steer .k{color:var(--warn)}
    .dir-step.k-result .k{color:var(--good)}
    .dir-steer{display:flex;gap:6px}
    .dir-steer-in{flex:1;min-width:0;padding:6px 8px;background:var(--surface-1);border:1px solid var(--line);
      border-radius:7px;color:var(--text);font:inherit;font-size:12px}
    .dir-arow{display:flex;gap:6px;align-items:center}
    /* buttons */
    .dir-btn{padding:var(--s-4) var(--s-5);background:var(--surface-3);border:1px solid var(--line);border-radius:var(--r-sm);color:var(--text);font:inherit;font-size:var(--fs-sm);cursor:pointer;transition:background var(--dur-fast) var(--ease),border-color var(--dur-fast) var(--ease);white-space:nowrap}
    .dir-btn:hover{border-color:var(--accent)}
    .dir-btn:disabled{opacity:.45;cursor:default}
    .dir-btn.dir-primary{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}
    .dir-btn.dir-danger{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}
    .dir-btn.dir-sm{padding:4px 8px;font-size:11px}
    /* queue board */
    .dir-cols{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;align-items:start}
    .dir-col{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:8px}
    .dir-colh{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:var(--fw-semi)}
    .dir-colh .dir-spacer{flex:1}
    .dir-qcard{background:var(--surface-1);border:1px solid var(--line);border-radius:8px;padding:8px 9px;display:flex;flex-direction:column;gap:6px}
    .dir-qcard.sel{border-color:var(--accent);background:var(--surface-1)}
    .dir-qtop{display:flex;align-items:flex-start;gap:7px}
    .dir-qtitle{flex:1;font-size:12px;line-height:1.35;cursor:pointer}
    .dir-qtitle:hover{color:var(--text)}
    .dir-qmeta{display:flex;gap:8px;align-items:center;font-size:10px;color:var(--text-3)}
    .dir-st{font-size:10px;padding:1px 6px;border-radius:12px;border:1px solid var(--line);color:var(--text-2)}
    .dir-st.queued{color:var(--warn);border-color:var(--warn-line)}
    .dir-st.dispatched{color:var(--good);border-color:var(--good-line)}
    .dir-st.done{color:var(--text);border-color:var(--accent-line)}
    .dir-st.failed{color:var(--bad);border-color:var(--bad-line)}
    .dir-qacts{display:flex;gap:5px;flex-wrap:wrap}
    .dir-chk{width:14px;height:14px;accent-color:var(--accent);margin-top:2px;cursor:pointer}
    /* It rides in the header band's .sec-a now, so it is sized to the band
       rather than to a row of its own - a 9px-radius pill with 8px of padding
       in there pushed the whole lid a line taller than the section above it. */
    .dir-batch{display:flex;align-items:center;gap:var(--s-4);padding:var(--s-2) var(--s-4);
      background:var(--surface-1);border:1px solid var(--accent);border-radius:var(--r-sm);
      font-size:var(--fs-xs)}
    .dir-batch b{color:var(--text)}
    /* mode bar — the control tower and the brainstorm surface are both
       full-width tools, so they take turns instead of stacking. */
    .dir-modes{display:flex;gap:6px;margin-bottom:var(--s-5)}
    .dir-modebtn{display:flex;align-items:center;gap:6px;padding:5px 12px;background:var(--surface-2);
      border:1px solid var(--line);border-radius:8px;color:var(--text-3);font:inherit;font-size:12px;cursor:pointer}
    .dir-modebtn:hover{border-color:var(--accent);color:var(--text-2)}
    .dir-modebtn.on{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}
    .dir-brain{min-height:640px}
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

  /* A COUNT PILL IS A NUMBER; THE WORD GOES BESIDE IT. .sec-n is a chip sized
     for digits, so "(3 running)" stretched the band and stopped lining up with
     every other section header in the app. The number goes in the pill, the
     noun in the .sec-sub next to it, and an empty pill hides itself. */
  function setCount(nId, subId, n, word) {
    const pill = document.getElementById(nId);
    if (pill) pill.textContent = n ? String(n) : "";
    const sub = document.getElementById(subId);
    if (sub) sub.textContent = n ? word : "";
  }

  // ---- render -------------------------------------------------------------

  function render(container, bg) {
    S.bg = bg; S.container = container;
    S.activity = {}; S.overview = { queue: {}, agents: [] };
    try { S.mode = localStorage.getItem("dir-mode") === "brainstorm" ? "brainstorm" : "board"; } catch (err) {}
    container.innerHTML =
      `<style>${STYLE}</style>
       <div class="dir-modes">
         <button class="dir-modebtn" data-m="board">${BGICON("agents")} Control tower</button>
         <button class="dir-modebtn" data-m="brainstorm">${BGICON("concept")} Brainstorm</button>
       </div>
       <div class="dir-brain" id="dir-brain" hidden></div>
       <div class="dir-wrap" id="dir-boardwrap">
         <section class="spanel s-director k-read">
           <div class="sec-h">${BGICON("agents")}<h3 class="sec-t">Live agent board</h3>
             <span class="sec-n" id="dir-agent-count"></span>
             <span class="sec-sub" id="dir-agent-sub"></span>
             <span class="sec-a">
               <button class="dir-btn dir-sm" onclick="DirCtl.refreshNow()">refresh</button>
             </span>
           </div>
           <div id="dir-agents"><div class="dir-empty">loading agents…</div></div>
         </section>
         <section class="spanel s-director k-list">
           <div class="sec-h">${BGICON("seats")}<h3 class="sec-t">Queue board - by seat</h3>
             <span class="sec-n" id="dir-queue-count"></span>
             <span class="sec-sub" id="dir-queue-sub"></span>
             <span class="sec-a" id="dir-batch-slot"></span>
           </div>
           <div id="dir-queue"><div class="dir-empty">loading queue…</div></div>
         </section>
       </div>`;
    container.querySelectorAll(".dir-modebtn").forEach(b =>
      b.addEventListener("click", () => setMode(b.dataset.m)));
    applyMode();
    // First paint.
    refresh();
  }

  /* The brainstorm workspace (chat + writing pad + drawing pad) is a
   * three-panel surface in its own right; squeezing it under the agent board
   * would give it a sliver. The two modes take turns. */
  function setMode(mode) {
    const next = mode === "brainstorm" ? "brainstorm" : "board";
    if (next === S.mode) return;
    S.mode = next;
    try { localStorage.setItem("dir-mode", next); } catch (err) {}
    applyMode();
    if (next === "board") refresh();
  }

  function applyMode() {
    if (!S.container) return;
    const brain = S.mode === "brainstorm";
    const board = S.container.querySelector("#dir-boardwrap");
    const host = S.container.querySelector("#dir-brain");
    if (board) board.hidden = brain;
    if (host) host.hidden = !brain;
    S.container.querySelectorAll(".dir-modebtn").forEach(b =>
      b.classList.toggle("on", (b.dataset.m === "brainstorm") === brain));
    if (!brain) { unmountBrain(); return; }
    if (!window.Brainstorm || !Brainstorm.mount) {
      if (host) host.innerHTML = `<div class="dir-empty">the brainstorm workspace did not load</div>`;
      return;
    }
    try { Brainstorm.mount(host, { seat: "director" }); }
    catch (err) { if (host) host.innerHTML = `<div class="dir-empty">brainstorm failed to start</div>`; }
  }

  function unmountBrain() {
    try { if (window.Brainstorm && Brainstorm.unmount) Brainstorm.unmount(); } catch (err) {}
  }

  // ---- refresh (poll) -----------------------------------------------------

  function refresh() {
    if (!mounted() || !S.bg) return;
    // Nothing on the board is on screen in brainstorm mode; polling the
    // overview and every running agent's activity to repaint it is pure cost.
    if (S.mode === "brainstorm") return;
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
      setCount("dir-agent-count", "dir-agent-sub", countRunning(), "running");
      return;
    }
    // LIVE means live. This board used to keep every exited and stopped session
    // on screen, so a floor with nothing running looked identical to a busy one
    // and the two cards that mattered were buried under twenty finished ones.
    // Finished runs are not lost — they are the completed/failed groups in the
    // shared work panel above, with their whole transcript.
    const agents = (S.overview.agents || []).filter(a => a && a.state === "running");
    const idx = itemIndex();
    setCount("dir-agent-count", "dir-agent-sub", agents.length, "running");

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
    // BGWS.glyphs returns rendered <svg> markup from icons.js, not text. Running
    // it through esc() printed the SVG source into the card as visible source
    // code; it comes from a fixed icon table, never from user input.
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
         <span class="dir-glyph">${glyph}</span>
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

  // The board is a WORKLOAD, not an archive. A queue column that listed every
  // done and failed item this project ever produced ran to fifty cards a seat,
  // and the one queued item you came to dispatch was somewhere in the middle of
  // them. Only work that is still moving is a queue.
  const ACTIVE = { queued: 1, dispatched: 1, review: 1 };
  const isActive = it => !!(it && ACTIVE[it.status]);

  function renderQueue() {
    const host = document.getElementById("dir-queue");
    if (!host) return;
    const raw = S.overview.queue || {};
    const q = {};
    Object.keys(raw).forEach(s => { q[s] = (raw[s] || []).filter(isActive); });
    const order = (S.bg.seats || Object.keys(q));
    Object.keys(q).forEach(s => { if (order.indexOf(s) < 0) order.push(s); });

    let total = 0, queuedTotal = 0;
    order.forEach(s => {
      const items = q[s] || [];
      total += items.length;
      queuedTotal += items.filter(i => i && i.status === "queued").length;
    });
    setCount("dir-queue-count", "dir-queue-sub", total,
             `active · ${queuedTotal} queued`);

    // Prune stale selections (items no longer queued/present).
    const present = {};
    order.forEach(s => (q[s] || []).forEach(i => { if (i) present[i.id] = i.status; }));
    Object.keys(S.selected).forEach(idk => {
      if (present[idk] !== "queued") delete S.selected[idk];
    });
    renderBatchBar();

    if (!total) {
      host.innerHTML = `<div class="dir-empty">Nothing active. Completed and failed work is in the panel above, grouped by state, with its full transcript.</div>`;
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
    // BGWS.glyphs returns rendered <svg> markup from icons.js, not text. Running
    // it through esc() printed the SVG source into the card as visible source
    // code; it comes from a fixed icon table, never from user input.
    const glyph = (S.bg.glyphs && S.bg.glyphs[seat]) || "•";
    const queued = items.filter(i => i && i.status === "queued");
    const col = document.createElement("div");
    col.className = "dir-col";
    const head = document.createElement("div");
    head.className = "dir-colh";
    head.innerHTML =
      `<span class="dir-glyph">${glyph}</span><span>${esc(seat)}</span>
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
  window.SeatWS.director = {
    label: "Director", glyph: BGICON("director"), render, refresh,
    // SeatShell calls this before the container is discarded — Brainstorm owns
    // timers and observers it has to be told to drop.
    unmount() { unmountBrain(); },
  };
})();
