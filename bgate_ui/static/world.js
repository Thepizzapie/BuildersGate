/* World — the producer's surface, and the only one that was missing a pen.
 *
 * The audit: "the two things my job actually is — writing the design bible and
 * holding the cut line — have no write path in the UI at all; the bible is a
 * read-only chip list." So this module is a write surface first:
 *
 *   Bible tab   every section editable in place, and the SCOPE TIERS rendered as
 *               one drag-ordered list with the cut line as a draggable row in it.
 *               Dropping the line is the scope decision: it commits the tier
 *               order (POST /api/bible/reorder) and then the line's own rank,
 *               because rank is the shared numeric space scope.check compares.
 *               What the line strands — open work it retroactively invalidates —
 *               is the panel next to it, with a one-click re-file.
 *   Lore tab    the canon graph, laid out server-side, rendered with NodeCanvas.
 *
 * Two refusals are surfaced rather than swallowed, because both are the feature:
 *   409 from DELETE /api/bible/{id}  — work is filed under that section; the user
 *                                      picks reassign / untier / cancel.
 *   409 from a lore write            — the prose breaks canon; the flags are shown
 *                                      and a human may override. An agent may not.
 */
window.World = (() => {
  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const TABS = [{ id: "bible", label: "Bible & cut line" },
                { id: "lore", label: "Lore graph" }];
  const KIND_LABEL = {
    pillar: "Pillars", loop: "Core loop", constraint: "Constraints",
    reference: "References", scope_tier: "Scope tiers", cut_line: "Cut line",
  };
  const EDITABLE_KINDS = ["pillar", "loop", "constraint", "reference"];
  const STATUS_COLOR = { canon: "var(--good)", draft: "var(--warn)", retired: "var(--bad)" };
  const LORE_GLYPH = {
    faction: "⚑", character: "☗", place: "⌂", event: "✦", item: "◈",
    concept: "◍", species: "❖",
  };

  let tab = "bible";
  let bible = null;      // {pillars, loop, constraints, references, cut_line, in_scope, cut, sections, kinds}
  let scope = null;      // {cut_line, tiers, in_scope, cut, untiered_open, stranded}
  let slots = [];        // scope-tier ids interleaved with the "CUT" marker
  let lore = null;       // {data:[entities], graph:{nodes,edges,kinds,statuses}}
  let canvas = null;
  let selected = null;   // slug of the entity in the side panel
  let loreFilter = { kind: "", status: "" };

  /* ---- transport ------------------------------------------------------- *
   * The new routes answer {ok,data}; the older app.py routes answer a bare
   * payload. One reader for both, and a failure is a value rather than a
   * throw so every call site can render the refusal it got. */
  async function req(path, opts) {
    let r;
    try { r = await fetch(path, opts); }
    catch (e) { return { ok: false, status: 0, error: { code: "offline", message: "backend unreachable", detail: {} } }; }
    let body = {};
    try { body = await r.json(); } catch (e) { }
    if (r.ok && body && body.ok !== false) {
      return { ok: true, data: (body && body.data !== undefined) ? body.data : body, body };
    }
    const err = (body && body.error) || { code: "error", message: `request failed · ${r.status}`, detail: {} };
    return { ok: false, status: r.status, error: { detail: {}, ...err } };
  }
  const GET = p => req(p);
  const send = (method, p, body) => req(p, {
    method, headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const POST = (p, b) => send("POST", p, b);
  const PATCH = (p, b) => send("PATCH", p, b);
  const DEL = p => req(p, { method: "DELETE" });

  function toast(msg, bad) {
    if (window.BGWS && BGWS.toast) BGWS.toast(msg, bad);
    else if (bad) console.warn(msg);
  }
  function fail(res) { toast(res.error.message, true); }

  /* ---- shell ----------------------------------------------------------- */
  function activate() {
    const nav = document.getElementById("world-subnav");
    if (nav) nav.innerHTML = TABS.map(t =>
      `<button class="seat-tab ${t.id === tab ? "active" : ""}" onclick="World.setTab('${t.id}')">${E(t.label)}</button>`).join("");
    if (tab === "bible") renderBibleTab(); else renderLoreTab();
  }
  function setTab(next) {
    tab = next;
    canvas = null;   // the host element is about to be replaced
    activate();
  }

  const host = () => document.getElementById("world-root");

  /* ====================================================================== *
   * Bible + cut line
   * ====================================================================== */

  async function renderBibleTab() {
    const box = host(); if (!box) return;
    box.innerHTML = `<div class="empty">reading the bible…</div>`;
    const [b, s] = await Promise.all([GET("/api/bible"), GET("/api/scope")]);
    if (!b.ok) { box.innerHTML = `<div class="empty">${E(b.error.message)}</div>`; return; }
    bible = b.data;
    scope = s.ok ? s.data : null;
    rebuildSlots();
    drawBible();
  }

  /* The list the user drags: tier ids with a "CUT" marker at the line. Its
   * INDEX is the decision — everything after it is not being built. */
  function rebuildSlots() {
    const tiers = (scope && scope.tiers) || [];
    const above = tiers.filter(t => !t.below_cut).map(t => String(t.id));
    const below = tiers.filter(t => t.below_cut).map(t => String(t.id));
    slots = scope && scope.cut_line ? [...above, "CUT", ...below] : [...above, ...below];
  }

  function tierById(id) {
    return ((scope && scope.tiers) || []).find(t => String(t.id) === String(id));
  }

  function drawBible() {
    const box = host(); if (!box) return;
    box.innerHTML = `
      <div class="wl-cols">
        <div class="wl-main">${scopeCard()}${sectionCards()}</div>
        <div class="wl-side">${strandedCard()}${scopeStatsCard()}</div>
      </div>
      <div class="wl-modal" id="wl-modal" hidden></div>`;
    bindDrag();
  }

  function scopeCard() {
    const line = scope && scope.cut_line;
    const rows = slots.map((slot, index) => slot === "CUT"
      ? `<div class="wl-row wl-cut" draggable="true" data-i="${index}" title="Drag to move the cut line">
           <span class="wl-grip">⠿</span>
           <span class="wl-cut-label">CUT LINE · ${E(line ? line.title : "")}</span>
           <span class="wl-cut-note">everything below is explicitly not being built</span>
         </div>`
      : tierRow(tierById(slot), index)).join("");
    const draw = line ? "" :
      `<button class="qbtn small" onclick="World.drawCutLine()">draw the cut line</button>`;
    return `
      <div class="wl-card">
        <div class="wl-head">
          <h3>Scope tiers <span class="wl-n">${slots.filter(s => s !== "CUT").length}</span></h3>
          <div class="wl-actions">${draw}
            <button class="qbtn small ghost" onclick="World.addSection('scope_tier')">＋ tier</button></div>
        </div>
        <p class="wl-note">Drag to rank, drag the line to cut. Rank is priority — highest first.</p>
        <div class="wl-list" id="wl-tiers">${rows || `<div class="empty">no scope tiers yet — add the first thing you are actually building</div>`}</div>
      </div>`;
  }

  function tierRow(tier, index) {
    if (!tier) return "";
    const cut = tier.below_cut;
    const items = tier.items || { total: 0, open: 0 };
    return `
      <div class="wl-row ${cut ? "below" : ""}" draggable="true" data-i="${index}" data-id="${tier.id}">
        <span class="wl-grip">⠿</span>
        <span class="wl-rank">${tier.rank}</span>
        <span class="wl-title" contenteditable="true" spellcheck="false"
              onblur="World.retitle(${tier.id}, this.textContent)">${E(tier.title)}</span>
        ${items.open ? `<span class="wl-badge ${cut ? "bad" : ""}">${items.open} open</span>` : ""}
        ${cut ? `<span class="wl-badge bad">cut</span>` : ""}
        <button class="wl-x" title="Delete this tier" onclick="World.removeSection(${tier.id})">✕</button>
      </div>`;
  }

  function sectionCards() {
    const groups = {
      pillar: bible.pillars || [], loop: bible.loop || [],
      constraint: bible.constraints || [], reference: bible.references || [],
    };
    return EDITABLE_KINDS.map(kind => {
      const rows = groups[kind].map(section => `
        <div class="wl-row" data-id="${section.id}">
          <span class="wl-title" contenteditable="true" spellcheck="false"
                onblur="World.retitle(${section.id}, this.textContent)">${E(section.title)}</span>
          <button class="wl-x" title="Delete" onclick="World.removeSection(${section.id})">✕</button>
        </div>
        <div class="wl-body" contenteditable="true" spellcheck="false" data-empty="say more — the seats read this"
             onblur="World.rebody(${section.id}, this.innerText)">${E(section.body || "")}</div>`).join("");
      return `
        <div class="wl-card">
          <div class="wl-head">
            <h3>${E(KIND_LABEL[kind])} <span class="wl-n">${groups[kind].length}</span></h3>
            <button class="qbtn small ghost" onclick="World.addSection('${kind}')">＋ add</button>
          </div>
          <div class="wl-list">${rows || `<div class="empty">nothing written yet</div>`}</div>
        </div>`;
    }).join("");
  }

  function strandedCard() {
    const list = (scope && scope.stranded) || [];
    const targets = ((scope && scope.in_scope) || []);
    const rows = list.map(item => `
      <div class="wl-strand">
        <div class="wl-strand-t">${E(item.title)}</div>
        <div class="wl-strand-m">#${item.id} · ${E(item.seat)} · ${E(item.status)} · under <b>${E(item.tier_title)}</b></div>
        <div class="wl-strand-a">
          <select id="wl-refile-${item.id}" aria-label="Move to tier">
            <option value="">untier it</option>
            ${targets.map(t => `<option value="${t.id}">${E(t.title)}</option>`).join("")}
          </select>
          <button class="qbtn small" onclick="World.refile(${item.id})">re-file</button>
        </div>
      </div>`).join("");
    return `
      <div class="wl-card ${list.length ? "alarm" : ""}">
        <div class="wl-head"><h3>Stranded by the line <span class="wl-n ${list.length ? "bad" : ""}">${list.length}</span></h3></div>
        <p class="wl-note">Open work sitting at or below the cut line. The line is retroactive or it is theatre — re-file it or cut it.</p>
        <div class="wl-list">${rows || `<div class="empty">nothing stranded — the line and the queue agree</div>`}</div>
      </div>`;
  }

  function scopeStatsCard() {
    if (!scope) return "";
    const line = scope.cut_line;
    return `
      <div class="wl-card">
        <div class="wl-head"><h3>The line</h3></div>
        <div class="wl-stat"><span>cut line</span><b>${line ? E(line.title) + " · rank " + line.rank : "not drawn"}</b></div>
        <div class="wl-stat"><span>in scope</span><b>${(scope.in_scope || []).length} tiers</b></div>
        <div class="wl-stat"><span>cut</span><b>${(scope.cut || []).length} tiers</b></div>
        <div class="wl-stat"><span>untiered open work</span><b class="${scope.untiered_open ? "warn" : ""}">${scope.untiered_open}</b></div>
        <p class="wl-note">Untiered work is flagged, not refused — refusing it would make the first line anyone draws reject the whole queue.</p>
      </div>`;
  }

  /* ---- bible mutations -------------------------------------------------- */

  async function addSection(kind) {
    const title = prompt(`New ${KIND_LABEL[kind] || kind} — title?`);
    if (!title || !title.trim()) return;
    const rank = kind === "scope_tier" ? slots.filter(s => s !== "CUT").length + 1 : 0;
    const res = await POST("/api/bible", { kind, title: title.trim(), rank });
    if (!res.ok) return fail(res);
    renderBibleTab();
  }

  async function drawCutLine() {
    // The line is its own section, so "no line" is genuinely "the scope call has
    // not been made" rather than a missing field. Draw it at the bottom: nothing
    // is cut until the producer drags it up.
    const res = await POST("/api/bible", {
      kind: "cut_line", title: "ship it",
      rank: slots.filter(s => s !== "CUT").length + 1,
    });
    if (!res.ok) return fail(res);
    renderBibleTab();
  }

  async function retitle(id, text) {
    const title = String(text || "").trim();
    if (!title) return renderBibleTab();   // refuse the empty title, restore it
    const res = await PATCH(`/api/bible/${id}`, { title });
    if (!res.ok) { fail(res); renderBibleTab(); }
  }

  async function rebody(id, text) {
    const res = await PATCH(`/api/bible/${id}`, { body: String(text || "") });
    if (!res.ok) fail(res);
  }

  async function removeSection(id, query) {
    const res = await DEL(`/api/bible/${id}${query || ""}`);
    if (res.ok) { closeModal(); return renderBibleTab(); }
    if (res.error.code === "conflict" && (res.error.detail.work_items || []).length) {
      return dependentsModal(id, res.error);
    }
    fail(res);
  }

  /* The 409 the delete route raises carries the work items filed under the
   * section. Rendering it as a CHOICE is the point — swallowing it would either
   * lose the delete or silently untier live work. */
  function dependentsModal(id, error) {
    const items = error.detail.work_items || [];
    const targets = ((scope && scope.tiers) || []).filter(t => String(t.id) !== String(id));
    openModal(`
      <h3>${items.length} work item${items.length === 1 ? " is" : "s are"} filed under this section</h3>
      <p class="wl-note">${E(error.message)}</p>
      <div class="wl-list">${items.map(i =>
        `<div class="wl-strand"><div class="wl-strand-t">${E(i.title)}</div>
         <div class="wl-strand-m">#${i.id} · ${E(i.seat)} · ${E(i.status)}</div></div>`).join("")}</div>
      <div class="wl-choice">
        <div class="wl-opt">
          <select id="wl-dep-target" aria-label="Move the work to">
            ${targets.map(t => `<option value="${t.id}">${E(t.title)}</option>`).join("")}
          </select>
          <button class="qbtn small" onclick="World.deleteReassigning(${id})"
                  ${targets.length ? "" : "disabled"}>move the work, then delete</button>
        </div>
        <button class="qbtn small ghost" onclick="World.deleteForcing(${id})">untier the work and delete</button>
        <button class="qbtn small ghost" onclick="World.closeModal()">cancel</button>
      </div>`);
  }
  function deleteReassigning(id) {
    const to = document.getElementById("wl-dep-target");
    if (!to || !to.value) return;
    removeSection(id, `?reassign_to=${encodeURIComponent(to.value)}`);
  }
  function deleteForcing(id) { removeSection(id, "?force=true"); }

  async function refile(itemId) {
    const select = document.getElementById(`wl-refile-${itemId}`);
    const tier = select && select.value ? Number(select.value) : null;
    const res = await POST("/api/scope/assign", { item_id: itemId, scope_tier_id: tier });
    if (!res.ok) return fail(res);
    toast(tier ? "re-filed" : "untiered");
    renderBibleTab();
  }

  /* ---- dragging the line ------------------------------------------------ */

  function bindDrag() {
    const list = document.getElementById("wl-tiers");
    if (!list) return;
    let from = null;
    list.addEventListener("dragstart", e => {
      const row = e.target.closest(".wl-row"); if (!row) return;
      from = Number(row.dataset.i);
      row.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", String(from)); } catch (err) { }
    });
    list.addEventListener("dragend", () => {
      list.querySelectorAll(".wl-row").forEach(r => r.classList.remove("dragging", "over"));
    });
    list.addEventListener("dragover", e => {
      e.preventDefault();
      const row = e.target.closest(".wl-row");
      list.querySelectorAll(".wl-row").forEach(r => r.classList.toggle("over", r === row));
    });
    list.addEventListener("drop", e => {
      e.preventDefault();
      const row = e.target.closest(".wl-row");
      if (!row || from === null) return;
      const to = Number(row.dataset.i);
      if (to === from) return;
      const next = slots.slice();
      next.splice(to, 0, next.splice(from, 1)[0]);
      from = null;
      commitOrder(next);
    });
  }

  /* Committing an order IS the scope decision, so it is two writes in a fixed
   * order: the tiers get contiguous ranks 1..N, then the line is parked at the
   * rank of the first tier it cuts. scope.check compares those ranks directly. */
  async function commitOrder(next) {
    const order = next.filter(s => s !== "CUT").map(Number);
    if (order.length) {
      const res = await POST("/api/bible/reorder", { kind: "scope_tier", order });
      if (!res.ok) { fail(res); return renderBibleTab(); }
    }
    const line = scope && scope.cut_line;
    if (line) {
      const above = next.indexOf("CUT");
      const res = await PATCH(`/api/bible/${line.id}`, { rank: above + 1 });
      if (!res.ok) { fail(res); return renderBibleTab(); }
    }
    slots = next;
    await renderBibleTab();
    const strandedNow = (scope && scope.stranded) || [];
    if (strandedNow.length) toast(`${strandedNow.length} open item(s) now below the line`, true);
  }

  /* ---- modal ------------------------------------------------------------ */
  function openModal(html) {
    const el = document.getElementById("wl-modal");
    if (!el) return;
    el.innerHTML = `<div class="wl-modal-card">${html}</div>`;
    el.hidden = false;
  }
  function closeModal() {
    const el = document.getElementById("wl-modal");
    if (el) { el.hidden = true; el.innerHTML = ""; }
  }

  /* ====================================================================== *
   * Lore graph
   * ====================================================================== */

  async function renderLoreTab() {
    const box = host(); if (!box) return;
    box.innerHTML = `<div class="empty">reading canon…</div>`;
    const query = ["graph=true"];
    if (loreFilter.kind) query.push(`kind=${encodeURIComponent(loreFilter.kind)}`);
    if (loreFilter.status) query.push(`status=${encodeURIComponent(loreFilter.status)}`);
    const res = await GET(`/api/lore?${query.join("&")}`);
    if (!res.ok) { box.innerHTML = `<div class="empty">${E(res.error.message)}</div>`; return; }
    // graph rides alongside `data` on the envelope, not inside it.
    lore = { entities: res.data || [], graph: (res.body && res.body.graph) || { nodes: [], edges: [], kinds: [], statuses: [] } };
    drawLore();
  }

  function drawLore() {
    const box = host(); if (!box) return;
    const g = lore.graph;
    box.innerHTML = `
      <div class="wl-toolbar">
        <select id="wl-fkind" onchange="World.setLoreFilter()" aria-label="Filter by kind">
          <option value="">all kinds</option>
          ${(g.kinds || []).map(k => `<option value="${k}" ${loreFilter.kind === k ? "selected" : ""}>${E(k)}</option>`).join("")}
        </select>
        <select id="wl-fstatus" onchange="World.setLoreFilter()" aria-label="Filter by status">
          <option value="">all statuses</option>
          ${(g.statuses || []).map(s => `<option value="${s}" ${loreFilter.status === s ? "selected" : ""}>${E(s)}</option>`).join("")}
        </select>
        <span class="wl-note">${(g.nodes || []).length} entities · ${(g.edges || []).length} links — drag a port to link two of them</span>
        <button class="qbtn small ghost" onclick="World.addEntity()">＋ entity</button>
      </div>
      <div class="wl-graph">
        <div class="wl-canvas" id="wl-canvas"></div>
        <aside class="wl-entity" id="wl-entity"><div class="empty">pick a node</div></aside>
      </div>
      <div class="wl-modal" id="wl-modal" hidden></div>`;

    const hostEl = document.getElementById("wl-canvas");
    if (!(g.nodes || []).length) {
      hostEl.innerHTML = `<div class="empty">no lore yet — the first entity is usually the place the game happens in</div>`;
      return;
    }
    // The payload is already laid out server-side in NodeCanvas's own shape;
    // only the glyph is ours to pick.
    (g.nodes || []).forEach(n => { n.glyph = LORE_GLYPH[n.kind] || "◆"; });
    canvas = new NodeCanvas(hostEl, {
      nodes: g.nodes, edges: g.edges,
      renderBody: node => `
        <div class="wl-node-meta">
          <span class="wl-pill" style="--p:${STATUS_COLOR[node.status] || "var(--ash)"}">${E(node.status)}</span>
          <span class="wl-node-kind">${E(node.kind)}</span>
          ${node.facts ? `<span class="wl-node-kind">${node.facts} fact${node.facts === 1 ? "" : "s"}</span>` : ""}
        </div>
        <div class="wl-node-sum">${E(node.summary || "no summary yet")}</div>`,
      onSelect: node => { selected = node ? node.id : null; drawEntity(); },
      onConnect: (from, to) => linkEntities(from[0], to[0]),
      accent: "var(--ember)",
    }).mount();
    canvas.fit();
    if (selected) drawEntity();
  }

  function setLoreFilter() {
    loreFilter = {
      kind: document.getElementById("wl-fkind").value,
      status: document.getElementById("wl-fstatus").value,
    };
    renderLoreTab();
  }

  async function drawEntity() {
    const panel = document.getElementById("wl-entity");
    if (!panel) return;
    if (!selected) { panel.innerHTML = `<div class="empty">pick a node</div>`; return; }
    panel.innerHTML = `<div class="empty">loading…</div>`;
    const res = await GET(`/api/lore/${encodeURIComponent(selected)}`);
    if (!res.ok) { panel.innerHTML = `<div class="empty">${E(res.error.message)}</div>`; return; }
    const { entity, facts, links } = res.data;
    panel.innerHTML = `
      <div class="wl-ehead">
        <div>
          <div class="wl-ekind">${E(entity.kind)}</div>
          <h3>${E(entity.name)}</h3>
        </div>
        <select id="wl-estatus" onchange="World.setStatus()" aria-label="Canon status">
          ${["draft", "canon", "retired"].map(s =>
            `<option value="${s}" ${entity.status === s ? "selected" : ""}>${s}</option>`).join("")}
        </select>
      </div>
      <label class="wl-l">Summary</label>
      <textarea id="wl-esummary" rows="2" placeholder="one line">${E(entity.summary || "")}</textarea>
      <label class="wl-l">Body</label>
      <textarea id="wl-ebody" rows="6" placeholder="the prose a narrative agent reads">${E(entity.body || "")}</textarea>
      <button class="qbtn small" onclick="World.saveEntity()">save prose</button>

      <label class="wl-l">Canon facts <span class="wl-n">${facts.length}</span></label>
      <p class="wl-note">One checkable statement each. This is what refuses a contradicting write.</p>
      <div class="wl-list">${facts.map(f => `
        <div class="wl-fact ${f.locked ? "locked" : ""}">
          <span>${E(f.statement)}</span>
          ${f.locked ? `<span class="wl-badge">locked</span>` : ""}
        </div>`).join("") || `<div class="empty">no facts — nothing here can be contradicted yet</div>`}</div>
      <div class="wl-factadd">
        <input id="wl-newfact" placeholder="assert one fact…" maxlength="300">
        <label class="wl-check"><input type="checkbox" id="wl-factlock"> lock</label>
        <button class="qbtn small" onclick="World.addFact()">assert</button>
      </div>

      <label class="wl-l">Links <span class="wl-n">${links.length}</span></label>
      <div class="wl-list">${links.map(l =>
        `<div class="wl-fact"><span>${l.dir === "out" ? "→" : "←"} <b>${E(l.rel)}</b> · ${E(l.name)}</span></div>`
      ).join("") || `<div class="empty">no links — drag between node ports</div>`}</div>`;
  }

  /* ---- the canon gate --------------------------------------------------- *
   * POST /api/lore and the fact/patch routes 409 with the conflict flags and do
   * NOT write. Showing the flags and offering the override to a human is the
   * whole contract — a UI that retried silently would put the old formality
   * straight back. */
  function canonModal(error, retry) {
    const flags = error.detail.flags || [];
    const conflicts = flags.filter(f => f.level === "conflict");
    const reviews = flags.filter(f => f.level !== "conflict");
    const row = f => `
      <div class="wl-flag ${f.level}">
        <div class="wl-flag-h"><b>${E(f.code)}</b>${f.entity ? ` · ${E(f.entity)}` : ""}</div>
        <div>${E(f.message)}</div>
        ${f.canon ? `<div class="wl-flag-q">canon: ${E(f.canon)}</div>` : ""}
        ${f.text ? `<div class="wl-flag-q">yours: ${E(f.text)}</div>` : ""}
      </div>`;
    openModal(`
      <h3>This breaks canon</h3>
      <p class="wl-note">${E(error.message)} Nothing was written.</p>
      ${conflicts.map(row).join("")}
      ${reviews.length ? `<div class="wl-l">also worth a look</div>${reviews.map(row).join("")}` : ""}
      <div class="wl-choice">
        <button class="qbtn small" onclick="World.overrideCanon()">override — I know, write it anyway</button>
        <button class="qbtn small ghost" onclick="World.closeModal()">cancel, I'll fix the text</button>
      </div>`);
    window.World._retry = retry;
  }
  function overrideCanon() {
    const retry = window.World._retry;
    window.World._retry = null;
    closeModal();
    if (retry) retry(true);
  }
  /* Every canon-gated write funnels through here so the override path is
   * identical to the first attempt, minus the flag. */
  async function gatedWrite(run) {
    const res = await run(false);
    if (res.ok) return res;
    if (res.status === 409 && (res.error.detail.flags || res.error.detail.verdict)) {
      canonModal(res.error, async override => {
        const again = await run(override);
        if (!again.ok) return fail(again);
        toast("written over the canon flag");
        renderLoreTab();
      });
      return res;
    }
    fail(res);
    return res;
  }

  async function addEntity() {
    const name = prompt("Entity name?");
    if (!name || !name.trim()) return;
    const kind = prompt(`Kind? one of: ${(lore.graph.kinds || []).join(", ")}`, "concept");
    if (!kind) return;
    const summary = prompt("One-line summary? (optional)") || "";
    const res = await gatedWrite(override => POST("/api/lore",
      { kind: kind.trim(), name: name.trim(), summary, override }));
    if (res.ok) { selected = res.data.slug; renderLoreTab(); }
  }

  async function saveEntity() {
    const summary = document.getElementById("wl-esummary").value;
    const body = document.getElementById("wl-ebody").value;
    const res = await gatedWrite(override =>
      PATCH(`/api/lore/${encodeURIComponent(selected)}`, { summary, body, override }));
    if (res.ok) { toast("saved"); renderLoreTab(); }
  }

  async function setStatus() {
    const status = document.getElementById("wl-estatus").value;
    const res = await PATCH(`/api/lore/${encodeURIComponent(selected)}`, { status });
    if (!res.ok) { fail(res); return drawEntity(); }
    renderLoreTab();
  }

  async function addFact() {
    const input = document.getElementById("wl-newfact");
    const statement = (input.value || "").trim();
    if (!statement) return;
    const locked = document.getElementById("wl-factlock").checked;
    const res = await gatedWrite(override => POST(
      `/api/lore/${encodeURIComponent(selected)}/facts`, { statement, locked, override }));
    if (res.ok) { input.value = ""; renderLoreTab(); }
  }

  async function linkEntities(src, dst) {
    const rel = prompt(`How does "${src}" relate to "${dst}"? (e.g. rules, betrayed, lives-in)`);
    if (!rel || !rel.trim()) return renderLoreTab();   // undo the optimistic edge
    const res = await POST("/api/lore/link", { src, dst, rel: rel.trim() });
    if (!res.ok) { fail(res); return renderLoreTab(); }
    toast("linked");
  }

  /* ---- styles (injected once, engine-style) ----------------------------- */
  if (!document.getElementById("world-style")) {
    const style = document.createElement("style");
    style.id = "world-style";
    style.textContent = `
      .wl-cols{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;align-items:start}
      @media(max-width:1080px){.wl-cols{grid-template-columns:1fr}}
      .wl-card{background:var(--plate);border:1px solid var(--seam);border-radius:12px;padding:14px;margin-bottom:14px}
      .wl-card.alarm{border-color:var(--bad)}
      .wl-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}
      .wl-head h3{margin:0;font-size:13px;color:var(--bone)}
      .wl-actions{display:flex;gap:6px}
      .wl-n{font-family:var(--mono);font-size:10px;color:var(--ash2);margin-left:5px}
      .wl-n.bad{color:var(--bad)}
      .wl-note{margin:0 0 10px;font-size:11.5px;color:var(--ash2);line-height:1.45}
      .wl-list{display:flex;flex-direction:column;gap:6px}
      .wl-row{display:flex;align-items:center;gap:9px;padding:8px 10px;background:var(--void);border:1px solid var(--seam);border-radius:9px}
      .wl-row.below{opacity:.55}
      .wl-row.dragging{opacity:.35}
      .wl-row.over{border-color:var(--ember)}
      .wl-grip{color:var(--ash2);cursor:grab;font-size:12px;flex:none}
      .wl-rank{font-family:var(--mono);font-size:10px;color:var(--ash2);width:16px;flex:none}
      .wl-title{flex:1;font-size:13px;color:var(--bone);outline:none;min-width:0}
      .wl-title:focus{border-bottom:1px solid var(--ember)}
      .wl-body{padding:2px 10px 10px 34px;font-size:12px;color:var(--ash);outline:none;white-space:pre-wrap;min-height:16px}
      .wl-body:focus{color:var(--bone)}
      .wl-body:empty:before{content:attr(data-empty);color:var(--seam2)}
      .wl-badge{font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--ash2);border:1px solid var(--seam2);border-radius:20px;padding:1px 7px;flex:none}
      .wl-badge.bad{color:var(--bad);border-color:var(--bad)}
      .wl-x{border:0;background:transparent;color:var(--ash2);cursor:pointer;font-size:12px;padding:2px 4px;flex:none}
      .wl-x:hover{color:var(--bad)}
      .wl-cut{background:var(--ember-soft);border-color:var(--ember);border-style:dashed}
      .wl-cut-label{font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--ember);flex:none}
      .wl-cut-note{font-size:11px;color:var(--ash2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .wl-strand{padding:9px 10px;background:var(--void);border:1px solid var(--seam);border-radius:9px}
      .wl-strand-t{font-size:12.5px;color:var(--bone)}
      .wl-strand-m{font-family:var(--mono);font-size:10px;color:var(--ash2);margin-top:3px}
      .wl-strand-a{display:flex;gap:6px;margin-top:8px}
      .wl-strand-a select{flex:1;min-width:0}
      .wl-stat{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid var(--seam);font-size:12px;color:var(--ash)}
      .wl-stat b{color:var(--bone);font-weight:500;text-align:right}
      .wl-stat b.warn{color:var(--warn)}
      .wl-modal{position:fixed;inset:0;z-index:8500;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:24px;overflow:auto}
      .wl-modal[hidden]{display:none}
      .wl-modal-card{width:min(560px,100%);background:var(--plate);border:1px solid var(--seam);border-radius:14px;padding:22px}
      .wl-modal-card h3{margin:0 0 8px;font-size:15px;color:var(--bone)}
      .wl-choice{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
      .wl-opt{display:flex;gap:6px;flex:1;min-width:260px}
      .wl-opt select{flex:1;min-width:0}
      .wl-flag{padding:10px 12px;border:1px solid var(--seam);border-left-width:3px;border-radius:8px;margin-bottom:8px;font-size:12.5px;color:var(--ash);background:var(--void)}
      .wl-flag.conflict{border-left-color:var(--bad)}
      .wl-flag.review{border-left-color:var(--warn)}
      .wl-flag-h{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--bone);margin-bottom:4px}
      .wl-flag-q{margin-top:5px;padding-left:9px;border-left:1px solid var(--seam2);color:var(--ash2);font-size:11.5px}
      .wl-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px}
      .wl-toolbar .wl-note{margin:0;flex:1;min-width:160px}
      .wl-graph{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:14px;height:calc(100vh - 300px);min-height:440px}
      @media(max-width:1080px){.wl-graph{grid-template-columns:1fr;height:auto}.wl-canvas{height:460px}}
      .wl-canvas{position:relative;min-height:0}
      .wl-entity{background:var(--plate);border:1px solid var(--seam);border-radius:12px;padding:14px;overflow-y:auto}
      .wl-ehead{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:10px}
      .wl-ehead h3{margin:2px 0 0;font-size:15px;color:var(--bone)}
      .wl-ekind{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--ash2)}
      .wl-l{display:block;font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--ash2);margin:14px 0 6px}
      .wl-entity textarea,.wl-entity input,.wl-toolbar select,.wl-strand-a select,.wl-opt select{width:100%;padding:8px 10px;background:var(--void);border:1px solid var(--seam);border-radius:8px;color:var(--bone);font:inherit;font-size:12.5px}
      .wl-toolbar select{width:auto}
      .wl-entity textarea{resize:vertical;margin-bottom:8px}
      .wl-fact{display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--void);border:1px solid var(--seam);border-radius:8px;font-size:12px;color:var(--ash)}
      .wl-fact.locked{border-color:var(--ember-dim)}
      .wl-fact span{flex:1;min-width:0}
      .wl-factadd{display:flex;align-items:center;gap:6px;margin-top:8px}
      .wl-factadd input#wl-newfact{flex:1}
      .wl-check{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--ash2);white-space:nowrap}
      .wl-check input{width:auto}
      .wl-node-meta{display:flex;align-items:center;gap:6px;margin-bottom:5px}
      .wl-pill{font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--p);border:1px solid var(--p);border-radius:20px;padding:1px 7px}
      .wl-node-kind{font-family:var(--mono);font-size:9.5px;color:var(--ash2)}
      .wl-node-sum{font-size:11.5px;line-height:1.4;color:var(--ash)}
    `;
    document.head.appendChild(style);
  }

  return {
    activate, setTab, refresh: activate,
    addSection, removeSection, retitle, rebody, drawCutLine, refile,
    deleteReassigning, deleteForcing, closeModal,
    setLoreFilter, addEntity, saveEntity, setStatus, addFact, overrideCanon,
    _retry: null,
  };
})();
