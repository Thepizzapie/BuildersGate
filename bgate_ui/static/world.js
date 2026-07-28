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
 *   Lore tab    the canon graph, re-laid out here by how it is wired, rendered
 *               with NodeCanvas.
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
  // /api/bible answers the four editable kinds under their own plural keys.
  const GROUP_KEY = {
    pillar: "pillars", loop: "loop", constraint: "constraints", reference: "references",
  };
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
  let loreFilter = { kind: "", status: "", q: "" };
  /* Which bible sections are open. renderBibleTab() refetches and rebuilds the
   * whole tab after every write, so this cannot live in the DOM — a blur-save
   * would collapse everything the reader had opened. */
  const expanded = new Set();

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

  const sectionsOf = kind => (bible && bible[GROUP_KEY[kind]]) || [];

  /* How heavy a section is, said out loud. A 6,509-char act and a 148-char note
   * used to render identically; the collapsed row has to announce the weight or
   * collapsing just hides it. */
  function measure(body) {
    const chars = body.length;
    if (!chars) return { short: "empty", full: "nothing written yet" };
    const lines = body.split("\n").length;
    const n = chars >= 1000 ? (chars / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(chars);
    return { short: `${n} · ${lines} ln`, full: `${chars} characters · ${lines} lines` };
  }
  const peek = body => body.split("\n").map(l => l.trim()).find(Boolean)
    || "empty — the seats read this, so say something";

  /* The bible is a design document, not a settings list: ~52k characters across
   * 33 sections used to paint as 33 always-open bodies in one column. Sections
   * collapse to title + one-line peek + weight, the spine gives a 14-part arc a
   * shape you can take in at once, and the big kinds get the full column. */
  function sectionCards() {
    const groups = {};
    EDITABLE_KINDS.forEach(kind => { groups[kind] = sectionsOf(kind); });
    const cards = EDITABLE_KINDS.map(kind => {
      const list = groups[kind];
      const rows = list.map(section => sectionRow(section, kind)).join("");
      const allOpen = list.length > 0 && list.every(s => expanded.has(String(s.id)));
      return `
        <section class="wl-card wl-kind" data-wide="${list.length > 5 ? "1" : "0"}">
          <div class="wl-head">
            <h3>${E(KIND_LABEL[kind])} <span class="wl-n">${list.length}</span></h3>
            <div class="wl-actions">
              ${list.length ? `<button class="qbtn small ghost" id="wl-all-${kind}"
                onclick="World.toggleKind('${kind}')">${allOpen ? "collapse all" : "expand all"}</button>` : ""}
              <button class="qbtn small ghost" onclick="World.addSection('${kind}')">＋ add</button>
            </div>
          </div>
          <div class="wl-list wl-secs" id="wl-list-${kind}">${rows ||
            `<div class="empty">nothing written yet</div>`}</div>
        </section>`;
    }).join("");
    return `<div class="wl-doc">${spine(groups)}<div class="wl-kinds">${cards}</div></div>`;
  }

  function sectionRow(section, kind) {
    const open = expanded.has(String(section.id));
    const body = String(section.body || "");
    const m = measure(body);
    return `
      <article class="wl-sec ${open ? "open" : ""}" data-id="${section.id}" data-kind="${kind}">
        <div class="wl-sec-h">
          <span class="wl-grip" draggable="true" title="Drag to re-order">⠿</span>
          <button class="wl-disc" aria-expanded="${open}" aria-label="Expand section"
                  onclick="World.toggleSection(${section.id})">▸</button>
          <span class="wl-title" contenteditable="true" spellcheck="false"
                onblur="World.retitle(${section.id}, this.textContent)">${E(section.title)}</span>
          <span class="wl-meas" title="${E(m.full)}">${E(m.short)}</span>
          <button class="wl-x" title="Delete" onclick="World.removeSection(${section.id})">✕</button>
        </div>
        <button class="wl-peek" tabindex="-1" onclick="World.toggleSection(${section.id})">${E(peek(body))}</button>
        <div class="wl-body" contenteditable="true" spellcheck="false" data-empty="say more — the seats read this"
             onblur="World.rebody(${section.id}, this.innerText)">${E(body)}</div>
      </article>`;
  }

  function spine(groups) {
    const total = EDITABLE_KINDS.reduce((n, kind) => n + groups[kind].length, 0);
    const blocks = EDITABLE_KINDS.map(kind => !groups[kind].length ? "" : `
      <div class="wl-toc-k"><span>${E(KIND_LABEL[kind])}</span>
        <span class="wl-n">${groups[kind].length}</span></div>
      <ol class="wl-toc-l">${groups[kind].map((s, i) => `
        <li><button class="wl-toc-i" title="${E(s.title)}" onclick="World.jumpSection(${s.id})">
          <span class="wl-toc-r">${i + 1}</span><span class="wl-toc-t">${E(s.title)}</span>
        </button></li>`).join("")}</ol>`).join("");
    return `
      <nav class="wl-toc" aria-label="Bible contents">
        <div class="wl-head"><h3>Contents <span class="wl-n">${total}</span></h3></div>
        ${blocks || `<div class="empty">nothing written yet</div>`}
      </nav>`;
  }

  /* ---- reading the document -------------------------------------------- */

  function applyOpen(el, open) {
    if (!el) return;
    el.classList.toggle("open", open);
    const disc = el.querySelector(".wl-disc");
    if (disc) disc.setAttribute("aria-expanded", String(open));
  }
  function toggleSection(id, force) {
    const key = String(id);
    const open = force === undefined ? !expanded.has(key) : !!force;
    if (open) expanded.add(key); else expanded.delete(key);
    applyOpen(document.querySelector(`.wl-sec[data-id="${key}"]`), open);
  }
  function toggleKind(kind) {
    const rows = Array.from(document.querySelectorAll(`.wl-sec[data-kind="${kind}"]`));
    const open = !rows.every(r => r.classList.contains("open"));
    rows.forEach(r => {
      applyOpen(r, open);
      if (open) expanded.add(r.dataset.id); else expanded.delete(r.dataset.id);
    });
    const btn = document.getElementById(`wl-all-${kind}`);
    if (btn) btn.textContent = open ? "collapse all" : "expand all";
  }
  function jumpSection(id) {
    toggleSection(id, true);
    const el = document.querySelector(`.wl-sec[data-id="${id}"]`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.classList.add("lit");
    setTimeout(() => el.classList.remove("lit"), 900);
  }
  /* rebody() deliberately does not re-render (that would eat the caret), so the
   * collapsed peek and weight next to it are refreshed by hand or they lie. */
  function refreshMeasure(id, body) {
    const el = document.querySelector(`.wl-sec[data-id="${id}"]`);
    if (!el) return;
    const m = measure(body);
    const meas = el.querySelector(".wl-meas"), pk = el.querySelector(".wl-peek");
    if (meas) { meas.textContent = m.short; meas.title = m.full; }
    if (pk) pk.textContent = peek(body);
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
    const title = await askText({
      title: `New ${KIND_LABEL[kind] || kind}`,
      label: "Title", multiline: false, required: true, ok: "add",
    });
    if (!title || !title.trim()) return;
    // Append, never rank 0: passing 0 for every non-tier kind filed each new
    // section ahead of everything already written, so order was arbitrary.
    const rank = kind === "scope_tier"
      ? slots.filter(s => s !== "CUT").length + 1
      : sectionsOf(kind).length + 1;
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
    const body = String(text || "");
    refreshMeasure(id, body);
    const res = await PATCH(`/api/bible/${id}`, { body });
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

  /* ---- dragging the line, and everything else ---------------------------- *
   * One binder for every ordered list on the tab. It was hardcoded to #wl-tiers,
   * which is why only scope tiers could be re-ranked even though
   * POST /api/bible/reorder has always taken any kind. The caller says how a row
   * names itself and what committing an order means — for the tiers that is two
   * writes (order, then the line), for a kind it is the one reorder call. */
  function bindSortable(list, rowSel, keyOf, commit) {
    if (!list) return;
    let from = null;
    const rows = () => Array.from(list.querySelectorAll(rowSel));
    list.addEventListener("dragstart", e => {
      const row = e.target.closest(rowSel); if (!row) return;
      from = rows().indexOf(row);
      row.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", String(from)); } catch (err) { }
      // Sections hang draggable on the grip, not the row, so a text selection in
      // the contenteditable title or body cannot start a reorder. Drag the row
      // anyway, or the ghost is a lone ⠿.
      if (row !== e.target && e.dataTransfer.setDragImage) {
        try { e.dataTransfer.setDragImage(row, 16, 12); } catch (err) { }
      }
    });
    list.addEventListener("dragend", () => {
      rows().forEach(r => r.classList.remove("dragging", "over"));
    });
    list.addEventListener("dragover", e => {
      e.preventDefault();
      const row = e.target.closest(rowSel);
      rows().forEach(r => r.classList.toggle("over", r === row));
    });
    list.addEventListener("drop", e => {
      e.preventDefault();
      const row = e.target.closest(rowSel);
      if (!row || from === null) return;
      const to = rows().indexOf(row);
      if (to < 0 || to === from) return;
      const next = rows().map(keyOf);
      next.splice(to, 0, next.splice(from, 1)[0]);
      from = null;
      commit(next);
    });
  }

  function bindDrag() {
    bindSortable(document.getElementById("wl-tiers"), ".wl-row",
      row => slots[Number(row.dataset.i)], next => commitOrder(next));
    EDITABLE_KINDS.forEach(kind => bindSortable(
      document.getElementById(`wl-list-${kind}`), ".wl-sec",
      row => Number(row.dataset.id), next => reorderKind(kind, next)));
  }

  async function reorderKind(kind, order) {
    const res = await POST("/api/bible/reorder", { kind, order });
    if (!res.ok) return fail(res);
    renderBibleTab();
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
    // kind=/status= still narrow the FIRST load; every later fetch takes the
    // whole set. Filtering is client-side now, and the facet counts are derived
    // from what is in memory — a server-filtered refetch after a write would
    // leave them counting a subset and calling it the project.
    const first = !lore;
    const query = ["graph=true"];
    if (first && loreFilter.kind) query.push(`kind=${encodeURIComponent(loreFilter.kind)}`);
    if (first && loreFilter.status) query.push(`status=${encodeURIComponent(loreFilter.status)}`);
    const res = await GET(`/api/lore?${query.join("&")}`);
    if (!res.ok) { box.innerHTML = `<div class="empty">${E(res.error.message)}</div>`; return; }
    // graph rides alongside `data` on the envelope, not inside it.
    lore = { entities: res.data || [], graph: (res.body && res.body.graph) || { nodes: [], edges: [], kinds: [], statuses: [] } };
    drawLore();
  }

  /* ---- searching and faceting the canon --------------------------------- *
   * All 28 entities arrive on the first request WITH their bodies (the page
   * limit is 100), so narrowing them is a filter over memory. It used to be two
   * <select>s that refetched and rebuilt the canvas, which threw away pan, zoom
   * and the open entity every time the reader changed their mind. */
  const loreEntities = () => (lore && lore.entities) || [];
  const needleOf = () => loreFilter.q.trim().toLowerCase();
  const loreFiltering = () => !!(loreFilter.kind || loreFilter.status || needleOf());

  function loreHit(entity, needle) {
    if (!needle) return true;
    return `${entity.name || ""}\n${entity.summary || ""}\n${entity.body || ""}`
      .toLowerCase().includes(needle);
  }

  /** Slugs surviving the search and both facets. Node ids ARE entity slugs. */
  function loreVisible() {
    const needle = needleOf();
    const out = new Set();
    loreEntities().forEach(e => {
      if (loreFilter.kind && e.kind !== loreFilter.kind) return;
      if (loreFilter.status && e.status !== loreFilter.status) return;
      if (loreHit(e, needle)) out.add(e.slug);
    });
    return out;
  }

  /* A chip counts what picking it would leave, so the search and the OTHER
   * facet apply and its own does not — count a facet against itself and every
   * chip but the active one reads zero. */
  function facetCounts(field) {
    const needle = needleOf();
    const other = field === "kind" ? "status" : "kind";
    const n = {};
    loreEntities().forEach(e => {
      if (loreFilter[other] && e[other] !== loreFilter[other]) return;
      if (loreHit(e, needle)) n[e[field]] = (n[e[field]] || 0) + 1;
    });
    return n;
  }

  /* The server answers all seven kinds and all three statuses whatever the
   * project holds; offering "faction" to a project with no factions is noise,
   * so the values actually present are the ones that get a chip. */
  function facetRow(field, label) {
    const all = loreEntities();
    const order = ((field === "kind" ? lore.graph.kinds : lore.graph.statuses) || []);
    const present = order.filter(v => all.some(e => e[field] === v));
    if (present.length < 2) return "";
    const counts = facetCounts(field);
    return `
      <div class="wl-facet" data-facet="${field}">
        <span class="wl-facet-l">${E(label)}</span>
        ${present.map(v => `
          <button class="afilter ${loreFilter[field] === v ? "active" : ""}" data-v="${E(v)}"
                  onclick="World.setLoreFilter('${field}', '${E(v)}')">
            ${field === "kind" ? E(LORE_GLYPH[v] || "◆") + " " : ""}${E(v)}
            <span class="n">${counts[v] || 0}</span>
          </button>`).join("")}
      </div>`;
  }

  /* ---- placing the graph ------------------------------------------------- *
   * The payload arrives laid out server-side: one column per KIND at 300px
   * pitch, indexed into the seven-kind tuple. A project holding only characters,
   * concepts and species therefore drew columns at x=340/1540/1840 with a 960px
   * hole where faction/place/event/item would have been, stacked 19 characters
   * into a single 2,900px column, and left fit() clamped on its 0.55 floor
   * showing the top third of it. Kind is not structure — how the entities are
   * wired is. So the client re-places the nodes it was handed: every hub takes
   * its satellites in a fan beside it, on the side its links point away from,
   * and the resulting blocks are shelf-packed into a viewport-shaped rectangle.
   * lore.graph() is left alone; the MCP tool surface reads it too.
   *
   * Deterministic throughout — every tie breaks on the slug, so the same canon
   * draws the same picture on every load. */
  const LO = { w: 240, colGap: 32, rowH: 156, gap: 56, margin: 40, aspect: 2.4, hub: 3 };
  const CW = () => LO.w + LO.colGap;
  /* Nodes the reader has dragged this session. renderLoreTab() refetches and
   * rebuilds the canvas after every write, so without this, saving a summary
   * would throw away the arrangement they had just made. */
  const loreMoved = new Map();

  /** Undirected neighbours plus the in/out counts, which decide which side of
   *  its fan a hub stands on. */
  function loreAdj(nodes, edges) {
    const nb = new Map(), out = new Map(), inn = new Map();
    nodes.forEach(n => { nb.set(n.id, new Set()); out.set(n.id, 0); inn.set(n.id, 0); });
    (edges || []).forEach(e => {
      const a = e.from && e.from[0], b = e.to && e.to[0];
      if (a === b || !nb.has(a) || !nb.has(b)) return;
      out.set(a, out.get(a) + 1); inn.set(b, inn.get(b) + 1);
      nb.get(a).add(b); nb.get(b).add(a);
    });
    return { nb, out, inn, deg: id => nb.get(id).size };
  }

  /** Cols x rows for n cards at roughly the page's aspect, penalising the slots
   *  a ragged last row would leave empty. */
  function gridShape(n) {
    if (n <= 1) return { cols: 1, rows: 1 };
    let best = null;
    for (let cols = 1; cols <= n; cols++) {
      const rows = Math.ceil(n / cols);
      const score = Math.abs(Math.log((cols * CW()) / (rows * LO.rowH) / LO.aspect))
        + (cols * rows - n) * 0.04;
      if (!best || score < best.score) best = { cols, rows, score };
    }
    return best;
  }

  function gridBlock(ids) {
    const g = gridShape(ids.length);
    return {
      at: ids.map((id, i) => ({ id, x: (i % g.cols) * CW(), y: Math.floor(i / g.cols) * LO.rowH })),
      w: g.cols * CW(), h: g.rows * LO.rowH, key: ids[0],
    };
  }

  function hubBlock(hub, sats, adj) {
    if (!sats.length) return gridBlock([hub]);
    // The 14 tone links point INTO the tone guide, and an edge leaves a card on
    // its right and enters the next on its left — so a hub that is mostly a
    // destination has to stand to the RIGHT of its fan or every link doubles back.
    const right = adj.inn.get(hub) >= adj.out.get(hub);
    const g = gridShape(sats.length);
    const gx = right ? 0 : CW();
    const at = sats.map((id, i) => ({
      id, x: gx + (i % g.cols) * CW(), y: Math.floor(i / g.cols) * LO.rowH,
    }));
    const h = g.rows * LO.rowH;
    at.push({ id: hub, x: right ? g.cols * CW() : 0, y: Math.max(0, (h - LO.rowH) / 2) });
    return { at, w: (g.cols + 1) * CW(), h, key: hub };
  }

  function layoutLore(nodes, edges) {
    if (!nodes || !nodes.length) return;
    const adj = loreAdj(nodes, edges);
    const ids = nodes.map(n => n.id).slice().sort();
    const byDeg = (a, b) => adj.deg(b) - adj.deg(a) || (a < b ? -1 : 1);

    const hubs = ids.filter(id => adj.deg(id) >= LO.hub).sort(byDeg);
    const isHub = new Set(hubs);
    // A satellite goes to its biggest neighbouring hub, so the one dominant hub
    // keeps its crowd instead of it being split across whoever asked first.
    const owner = new Map();
    ids.forEach(id => {
      if (isHub.has(id)) return;
      const pick = [...adj.nb.get(id)].filter(h => isHub.has(h)).sort(byDeg)[0];
      if (pick) owner.set(id, pick);
    });

    const blocks = hubs.map(h =>
      hubBlock(h, ids.filter(i => owner.get(i) === h).sort(byDeg), adj));

    // What is left over: small linked runs kept whole, then the unlinked.
    const loose = ids.filter(i => !isHub.has(i) && !owner.has(i));
    const free = new Set(loose);
    const seen = new Set();
    loose.forEach(start => {
      if (seen.has(start) || !adj.deg(start)) return;
      const comp = [], stack = [start];
      seen.add(start);
      while (stack.length) {
        const id = stack.pop();
        comp.push(id);
        [...adj.nb.get(id)].sort().forEach(next => {
          if (free.has(next) && !seen.has(next)) { seen.add(next); stack.push(next); }
        });
      }
      blocks.push(gridBlock(comp.sort(byDeg)));
    });
    const alone = loose.filter(i => !adj.deg(i));
    if (alone.length) blocks.push(gridBlock(alone));
    if (!blocks.length) return;

    // Shelf-pack tallest first. A pile shaped like the viewport is the whole
    // difference between fit() clamping at its floor and showing the graph.
    blocks.sort((a, b) => b.h - a.h || b.w - a.w || (a.key < b.key ? -1 : 1));
    const area = blocks.reduce((sum, b) => sum + b.w * b.h, 0);
    const width = Math.max(Math.max(...blocks.map(b => b.w)), Math.sqrt(area * LO.aspect));
    let x = 0, y = 0, shelf = 0;
    blocks.forEach(b => {
      if (x > 0 && x + b.w > width) { x = 0; y += shelf + LO.gap; shelf = 0; }
      b.ox = x; b.oy = y;
      x += b.w + LO.gap;
      shelf = Math.max(shelf, b.h);
    });

    const pos = new Map();
    blocks.forEach(b => b.at.forEach(s =>
      pos.set(s.id, { x: LO.margin + b.ox + s.x, y: LO.margin + b.oy + s.y })));
    nodes.forEach(n => {
      const p = loreMoved.get(n.id) || pos.get(n.id);
      if (p) { n.x = p.x; n.y = p.y; }
    });
  }

  function drawLore() {
    const box = host(); if (!box) return;
    const g = lore.graph;
    box.innerHTML = `
      <div class="wl-toolbar">
        <input id="wl-search" class="asset-search" type="search" spellcheck="false"
               placeholder="search names, summaries and prose…" value="${E(loreFilter.q)}"
               aria-label="Search the canon" oninput="World.setLoreQuery(this.value)"
               onkeydown="if(event.key==='Escape'){event.preventDefault();World.clearLoreFilter()}">
        <span class="wl-note" id="wl-count"></span>
        <button class="qbtn small ghost" id="wl-clear" hidden
                onclick="World.clearLoreFilter()">clear</button>
        <button class="qbtn small ghost" onclick="World.addEntity()">＋ entity</button>
      </div>
      <div class="wl-facets">${facetRow("kind", "kind")}${facetRow("status", "canon")}</div>
      <div class="wl-graph">
        <div class="wl-canvas" id="wl-canvas"></div>
        <aside class="wl-entity" id="wl-entity"><div class="empty">pick a node</div></aside>
      </div>
      <div class="wl-modal" id="wl-modal" hidden></div>`;

    const hostEl = document.getElementById("wl-canvas");
    if (!(g.nodes || []).length) {
      hostEl.innerHTML = `<div class="empty">no lore yet — the first entity is usually the place the game happens in</div>`;
      paintLoreFilter();
      return;
    }
    // The payload arrives in NodeCanvas's own shape; the glyph and the placement
    // are ours to pick.
    (g.nodes || []).forEach(n => { n.glyph = LORE_GLYPH[n.kind] || "◆"; });
    layoutLore(g.nodes, g.edges);
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
      // deleting an edge reports a move of nobody, so the null is real
      onNodeMove: node => { if (node) loreMoved.set(node.id, { x: node.x, y: node.y }); },
      onConnect: (from, to) => linkEntities(from[0], to[0]),
      accent: "var(--ember)",
    }).mount();
    canvas.fit();
    paintLoreFilter();   // a write refetches; the filter has to survive it
    if (selected) drawEntity();
  }

  /* Filtering repaints, it does not refetch: one class per node and the chip
   * counts, so pan, zoom and the entity you were reading all stay put. The
   * match reaches the GRAPH rather than a list — where a thing sits in the
   * relationships is most of what you were searching for. */
  function paintLoreFilter() {
    if (!lore) return;
    const on = loreFiltering();
    const vis = loreVisible();
    const hostEl = document.getElementById("wl-canvas");
    if (hostEl) {
      hostEl.classList.toggle("wl-filtering", on);
      hostEl.querySelectorAll(".nc-node").forEach(el =>
        el.classList.toggle("wl-dim", on && !vis.has(el.dataset.node)));
    }
    ["kind", "status"].forEach(field => {
      const counts = facetCounts(field);
      document.querySelectorAll(`.wl-facet[data-facet="${field}"] .afilter`).forEach(btn => {
        const v = btn.dataset.v, n = counts[v] || 0;
        btn.classList.toggle("active", loreFilter[field] === v);
        btn.classList.toggle("wl-none", !n && loreFilter[field] !== v);
        const out = btn.querySelector(".n");
        if (out) out.textContent = n;
      });
    });
    const count = document.getElementById("wl-count");
    if (count) count.textContent = on
      ? `${vis.size} of ${loreEntities().length} entities match`
      : `${loreEntities().length} entities · ${(lore.graph.edges || []).length} links — drag a port to link two of them`;
    const clear = document.getElementById("wl-clear");
    if (clear) clear.hidden = !on;
  }

  function setLoreQuery(q) {
    loreFilter.q = String(q || "");
    paintLoreFilter();
  }
  function setLoreFilter(field, value) {
    // clicking the live chip again clears that facet — the pills replace two
    // selects, so "all kinds" has to stay reachable without one
    loreFilter[field] = loreFilter[field] === value ? "" : value;
    paintLoreFilter();
  }
  function clearLoreFilter() {
    loreFilter = { kind: "", status: "", q: "" };
    const box = document.getElementById("wl-search");
    if (box) box.value = "";
    paintLoreFilter();
  }

  /* ---- reading an entity ------------------------------------------------ *
   * A lore body is a design document, not a form field: ALL-CAPS-colon
   * headings, hard-wrapped paragraphs and "- " bullets. The longest one here is
   * 3,780 characters and it used to paint into a rows="6" textarea in a 340px
   * aside, which is the least readable surface in the tool holding its best
   * writing. So the panel reads first and renders the structure the prose
   * already has; editing is a mode you ask for.
   *
   * Every fragment goes through E() before it is HTML. This is prose a human
   * typed — it is never markup, and a scanner that trusted it would be an
   * injection. */
  const isLabel = s => s.length > 0 && s.length <= 48
    && /[A-Z]/.test(s) && s === s.toUpperCase();

  function proseBlocks(body) {
    const out = [];              // {t:"h"|"f"|"p"|"li"}
    let para = null;
    const flush = () => { if (para !== null) { out.push({ t: "p", text: para }); para = null; } };
    const last = () => out[out.length - 1];
    String(body || "").replace(/\r/g, "").split("\n").forEach(raw => {
      const line = raw.trim();
      if (!line) { flush(); return; }
      const bullet = line.match(/^[-*•·]\s+(.+)$/);
      if (bullet) { flush(); out.push({ t: "li", text: bullet[1] }); return; }
      // an indented line under a bullet is that bullet's second line, not a paragraph
      if (/^\s/.test(raw) && para === null && last() && last().t === "li") {
        last().text += " " + line; return;
      }
      const colon = line.indexOf(":");
      if (colon > 0) {
        const key = line.slice(0, colon).trim(), rest = line.slice(colon + 1).trim();
        // "JOB / CLASS: Paladin" is a field row; "ABILITIES:" alone is a
        // heading; "DESCRIPTION: <300 characters>" is a heading with a
        // paragraph under it — a field row would squeeze prose into a column.
        if (isLabel(key)) {
          flush();
          if (!rest) out.push({ t: "h", text: key });
          else if (rest.length > 120) out.push({ t: "h", text: key }, { t: "p", text: rest });
          else out.push({ t: "f", key, text: rest });
          return;
        }
      }
      if (isLabel(line)) { flush(); out.push({ t: "h", text: line }); return; }
      // The source is hard-wrapped at ~78 columns, so consecutive lines are one
      // paragraph and get re-flowed to whatever measure the panel actually has.
      para = para === null ? line : `${para} ${line}`;
    });
    flush();
    return out;
  }

  function renderProse(body) {
    const blocks = proseBlocks(body);
    if (!blocks.length) return `<div class="empty">no prose yet — the narrative seats read this</div>`;
    let html = "", open = false;
    const closeList = () => { if (open) { html += "</ul>"; open = false; } };
    blocks.forEach(b => {
      if (b.t === "li") {
        if (!open) { html += `<ul class="wl-rl">`; open = true; }
        html += `<li>${E(b.text)}</li>`;
        return;
      }
      closeList();
      if (b.t === "h") html += `<h4 class="wl-rh">${E(b.text)}</h4>`;
      else if (b.t === "f") html += `<div class="wl-rf"><span class="wl-rf-k">${E(b.key)}</span><span class="wl-rf-v">${E(b.text)}</span></div>`;
      else html += `<p class="wl-rp">${E(b.text)}</p>`;
    });
    closeList();
    return html;
  }

  /* drawEntity() runs on every node click and after every lore write, so it runs
   * far more often than the reader changes entity. Mode and scroll live out here
   * or an unrelated re-render drops them back at the top of a 3.8k body — and
   * the payload is cached so toggling read/edit is not a refetch. */
  let entityView = { slug: null, mode: "read", scroll: 0 };
  let entityData = null;
  const LONG_BODY = 700;   // past this, 340px is not a reading measure

  function widenPanel(on) {
    const g = document.querySelector(".wl-graph");
    if (g) g.classList.toggle("reading", !!on);
  }

  function setEntityMode(mode) {
    entityView.mode = mode === "edit" ? "edit" : "read";
    entityView.scroll = 0;    // the two modes do not share a scroll position
    drawEntity(true);
  }

  async function drawEntity(cached) {
    const panel = document.getElementById("wl-entity");
    if (!panel) return;
    if (!selected) {
      entityView = { slug: null, mode: "read", scroll: 0 };
      entityData = null;
      widenPanel(false);
      panel.innerHTML = `<div class="empty">pick a node</div>`;
      return;
    }
    if (entityView.slug !== selected) { entityView = { slug: selected, mode: "read", scroll: 0 }; entityData = null; }
    // dataset.slug is only set on a painted panel, so a rebuilt tab keeps the
    // scroll it had rather than recording the new element's 0.
    else if (panel.dataset.slug === selected) entityView.scroll = panel.scrollTop;
    if (cached && entityData) return paintEntity(panel, entityData);
    if (panel.dataset.slug !== selected) panel.innerHTML = `<div class="empty">loading…</div>`;
    const res = await GET(`/api/lore/${encodeURIComponent(selected)}`);
    if (!res.ok) { panel.innerHTML = `<div class="empty">${E(res.error.message)}</div>`; return; }
    entityData = res.data;
    paintEntity(panel, entityData);
  }

  function paintEntity(panel, payload) {
    const { entity, facts, links } = payload;
    const body = String(entity.body || "");
    const editing = entityView.mode === "edit";
    const m = measure(body);   // same weight readout the bible rows use
    widenPanel(body.length > LONG_BODY);
    panel.dataset.slug = entity.slug || selected;
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
      <div class="wl-ebar">
        <span class="wl-emeas" title="${E(m.full)}">${E(m.short)}</span>
        <button class="qbtn small ghost" onclick="World.setEntityMode('${editing ? "read" : "edit"}')">${
          editing ? "done editing" : "edit prose"}</button>
      </div>
      ${editing ? `
        <label class="wl-l">Summary</label>
        <textarea id="wl-esummary" rows="2" placeholder="one line">${E(entity.summary || "")}</textarea>
        <label class="wl-l">Body</label>
        <textarea id="wl-ebody" rows="18" placeholder="the prose a narrative agent reads">${E(body)}</textarea>
        <button class="qbtn small" onclick="World.saveEntity()">save prose</button>`
      : `
        ${entity.summary ? `<p class="wl-elead">${E(entity.summary)}</p>` : ""}
        <div class="wl-read">${renderProse(body)}</div>`}

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
    // innerHTML empties the scroll box; put the reader back where they were.
    panel.scrollTop = entityView.scroll || 0;
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
    // One form, not three questions: the three fields make ONE entity, and the
    // kind is a closed set — typed free-hand it reached the server misspelled
    // and opened a category of one.
    const kinds = ((lore && lore.graph.kinds) || []).length
      ? lore.graph.kinds : Object.keys(LORE_GLYPH);
    const out = await askText({
      title: "New entity", ok: "create",
      fields: [
        { name: "name", label: "Name", type: "text", required: true },
        {
          name: "kind", label: "Kind", type: "select", required: true,
          value: kinds.includes("concept") ? "concept" : kinds[0],
          options: kinds.map(k => ({ value: k, label: `${LORE_GLYPH[k] || "◆"} ${k}` })),
        },
        { name: "summary", label: "One-line summary", type: "text" },
      ],
    });
    if (!out || !out.name.trim()) return;
    const res = await gatedWrite(override => POST("/api/lore",
      { kind: out.kind, name: out.name.trim(), summary: out.summary.trim(), override }));
    if (res.ok) { selected = res.data.slug; renderLoreTab(); }
  }

  async function saveEntity() {
    const summary = document.getElementById("wl-esummary").value;
    const body = document.getElementById("wl-ebody").value;
    const res = await gatedWrite(override =>
      PATCH(`/api/lore/${encodeURIComponent(selected)}`, { summary, body, override }));
    if (res.ok) { toast("saved"); entityView.mode = "read"; renderLoreTab(); }
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
    const rel = await askText({
      title: "Link entities",
      body: `How does "${src}" relate to "${dst}"?`,
      label: "Relationship", placeholder: "rules, betrayed, lives-in",
      multiline: false, required: true, ok: "link",
    });
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
      .wl-cols{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:var(--s-6);align-items:start}
      @media(max-width:1080px){.wl-cols{grid-template-columns:1fr}}
      .wl-card{background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-md);padding:var(--s-5);margin-bottom:var(--s-5)}
      .wl-card.alarm{border-color:var(--bad)}
      .wl-head{display:flex;align-items:center;justify-content:space-between;gap:var(--s-5);margin-bottom:var(--s-4)}
      .wl-head h3{margin:0;font-size:var(--fs-md);color:var(--text)}
      .wl-actions{display:flex;gap:var(--s-3)}
      .wl-n{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-3);margin-left:var(--s-2)}
      .wl-n.bad{color:var(--bad)}
      .wl-note{margin:0 0 var(--s-5);font-size:var(--fs-xs);color:var(--text-3);line-height:var(--lh-snug)}
      .wl-list{display:flex;flex-direction:column;gap:var(--s-3)}
      .wl-row{display:flex;align-items:center;gap:var(--s-4);padding:var(--s-4) var(--s-5);background:var(--bg);border:1px solid var(--line);border-radius:var(--r-sm)}
      .wl-row.below{opacity:.55}
      .wl-row.dragging{opacity:.35}
      .wl-row.over{border-color:var(--accent)}
      .wl-grip{color:var(--text-3);cursor:grab;font-size:var(--fs-sm);flex:none}
      .wl-rank{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-3);width:var(--s-6);flex:none}
      .wl-title{flex:1;font-size:var(--fs-md);color:var(--text);outline:none;min-width:0}
      .wl-title:focus{border-bottom:1px solid var(--accent)}
      .wl-body{padding:var(--s-1) var(--s-5) var(--s-5) var(--s-9);font-size:var(--fs-sm);color:var(--text-2);outline:none;white-space:pre-wrap;min-height:var(--s-6)}
      .wl-body:focus{color:var(--text)}
      .wl-body:empty:before{content:attr(data-empty);color:var(--text-3)}
      .wl-badge{font-family:var(--mono);font-size:var(--fs-3xs);text-transform:uppercase;letter-spacing:var(--track-label);color:var(--text-3);border:1px solid var(--line-strong);border-radius:var(--r-full);padding:var(--s-1) var(--s-3);flex:none}
      .wl-badge.bad{color:var(--bad);border-color:var(--bad)}
      .wl-x{border:0;background:transparent;color:var(--text-3);cursor:pointer;font-size:var(--fs-sm);padding:var(--s-1) var(--s-2);flex:none}
      .wl-x:hover{color:var(--bad)}

      /* The design document. The spine is sticky because the point of it is to
       * stay legible while a 6.5k-character section is open next to it. */
      .wl-doc{display:grid;grid-template-columns:minmax(0,210px) minmax(0,1fr);gap:var(--s-6);align-items:start}
      .wl-toc{position:sticky;top:var(--s-4);background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-md);padding:var(--s-5);max-height:calc(100vh - 220px);overflow:auto}
      .wl-toc-k{display:flex;align-items:center;justify-content:space-between;gap:var(--s-3);margin:var(--s-5) 0 var(--s-2);font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text-3)}
      .wl-toc-l{list-style:none;margin:0;padding:0}
      .wl-toc-i{display:flex;align-items:baseline;gap:var(--s-3);width:100%;text-align:left;border:0;background:transparent;color:var(--text-2);font:inherit;font-size:var(--fs-xs);line-height:var(--lh-snug);padding:var(--s-2) var(--s-3);border-radius:var(--r-xs);cursor:pointer}
      .wl-toc-i:hover{background:var(--surface-3);color:var(--text)}
      .wl-toc-r{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);flex:none}
      .wl-toc-t{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .wl-kinds{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--s-5);align-items:start}
      /* 14 loop entries and 2 references do not want the same box. */
      .wl-kind{margin-bottom:0}
      .wl-kind[data-wide="1"]{grid-column:1/-1}
      @media(max-width:1400px){.wl-kinds{grid-template-columns:1fr}}
      @media(max-width:1080px){.wl-doc{grid-template-columns:1fr}.wl-toc{position:static;max-height:none}}

      .wl-secs{gap:var(--s-2)}
      .wl-sec{background:var(--bg);border:1px solid var(--line);border-radius:var(--r-sm);transition:border-color var(--dur) var(--ease)}
      .wl-sec.open{background:var(--surface-2);border-color:var(--line-strong)}
      .wl-sec.dragging{opacity:.35}
      .wl-sec.over,.wl-sec.lit{border-color:var(--accent)}
      .wl-sec.lit{background:var(--accent-wash)}
      .wl-sec-h{display:flex;align-items:center;gap:var(--s-3);padding:var(--s-4) var(--s-4) var(--s-4) var(--s-5)}
      .wl-disc{border:0;background:transparent;color:var(--text-3);cursor:pointer;font-size:var(--fs-2xs);line-height:1;padding:var(--s-1);flex:none;transition:transform var(--dur) var(--ease)}
      .wl-sec.open .wl-disc{transform:rotate(90deg);color:var(--accent)}
      .wl-meas{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);flex:none;white-space:nowrap}
      .wl-peek{display:block;width:100%;min-width:0;text-align:left;border:0;background:transparent;cursor:pointer;color:var(--text-3);font:inherit;font-size:var(--fs-xs);line-height:var(--lh-snug);padding:0 var(--s-5) var(--s-4) var(--s-9);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .wl-peek:hover{color:var(--text-2)}
      .wl-sec .wl-body{display:none}
      .wl-sec.open .wl-peek{display:none}
      .wl-sec.open .wl-body{display:block;max-width:76ch;margin:0 var(--s-5) var(--s-5) var(--s-9);padding:var(--s-4) 0 0;border-top:1px solid var(--line-soft);line-height:var(--lh-loose)}
      .wl-cut{background:var(--ember-soft);border-color:var(--ember);border-style:dashed}
      .wl-cut-label{font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--ember);flex:none}
      .wl-cut-note{font-size:11px;color:var(--ash2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .wl-strand{padding:9px 10px;background:var(--void);border:1px solid var(--seam);border-radius:9px}
      .wl-strand-t{font-size:12.5px;color:var(--bone)}
      .wl-strand-m{font-family:var(--mono);font-size:10px;color:var(--ash2);margin-top:3px}
      .wl-strand-a{display:flex;gap:6px;margin-top:8px}
      .wl-strand-a select{flex:1;min-width:0}
      .wl-stat{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid var(--seam);font-size:12px;color:var(--ash)}
      .wl-stat b{color:var(--bone);font-weight:var(--fw-semi);text-align:right}
      .wl-stat b.warn{color:var(--warn)}
      .wl-modal{position:fixed;inset:0;z-index:8500;background:var(--scrim);display:flex;align-items:center;justify-content:center;padding:24px;overflow:auto}
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
      .wl-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s-4);margin-bottom:var(--s-4)}
      .wl-toolbar .wl-note{margin:0;flex:1;min-width:160px}
      .wl-toolbar .asset-search{flex:1 1 240px;max-width:380px}
      /* .qbtn sets its own display, so [hidden] needs saying out loud */
      .wl-toolbar .qbtn[hidden]{display:none}
      /* facets: the .afilter pill, so the counts read like every other filter
       * row in the app. A value with nothing behind it is not offered at all. */
      .wl-facets{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s-5);margin-bottom:var(--s-5)}
      .wl-facets:empty{display:none}
      .wl-facet{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s-3)}
      .wl-facet-l{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text-3)}
      .wl-facet .afilter{cursor:pointer}
      .wl-facet .afilter.wl-none{opacity:.4}
      /* The search has to land on the GRAPH: misses recede, hits keep their
       * head lit, so a match is read in place in the relationship structure
       * instead of in a list beside it. */
      .nc-node.wl-dim{opacity:.22;transition:opacity var(--dur) var(--ease)}
      .wl-filtering .nc-node:not(.wl-dim) .nc-head{background:var(--accent-wash)}
      .wl-graph{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:14px;height:calc(100vh - 300px);min-height:440px}
      /* A 3.8k-character body has no measure at 340px, so the panel takes the
       * width when there is prose to take it for. */
      .wl-graph.reading{grid-template-columns:minmax(0,1fr) min(600px,44vw)}
      @media(max-width:1080px){.wl-graph,.wl-graph.reading{grid-template-columns:1fr;height:auto}.wl-canvas{height:460px}}
      .wl-canvas{position:relative;min-height:0}
      .wl-entity{background:var(--plate);border:1px solid var(--seam);border-radius:12px;padding:14px;overflow-y:auto}
      .wl-ehead{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:10px}
      .wl-ehead h3{margin:2px 0 0;font-size:15px;color:var(--bone)}
      .wl-ekind{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--ash2)}
      .wl-l{display:block;font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--ash2);margin:14px 0 6px}

      /* The read view. Prose, so: a capped measure, loose leading, and the
       * ALL-CAPS-colon headings the writing already uses given real weight. */
      .wl-ebar{display:flex;align-items:center;justify-content:space-between;gap:var(--s-4);padding:var(--s-3) 0 var(--s-4);border-bottom:1px solid var(--line-soft);margin-bottom:var(--s-5)}
      .wl-emeas{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);white-space:nowrap}
      .wl-elead{max-width:70ch;margin:0 0 var(--s-5);font-size:var(--fs-md);line-height:var(--lh);color:var(--text)}
      .wl-read{max-width:70ch;font-size:var(--fs-sm);line-height:var(--lh-loose);color:var(--text-2)}
      .wl-rh{margin:var(--s-7) 0 var(--s-4);font-family:var(--mono);font-size:var(--fs-2xs);letter-spacing:var(--track-label);text-transform:uppercase;color:var(--accent);font-weight:var(--fw-semi)}
      .wl-read>*:first-child{margin-top:0}
      .wl-rp{margin:0 0 var(--s-5)}
      .wl-rf{display:flex;gap:var(--s-4);padding:var(--s-3) 0;border-bottom:1px solid var(--line-soft)}
      .wl-rf-k{flex:none;min-width:var(--s-10);max-width:38%;font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text-3);line-height:var(--lh-loose)}
      .wl-rf-v{min-width:0;color:var(--text)}
      .wl-rl{margin:0 0 var(--s-5);padding:0 0 0 var(--s-6);list-style:none}
      .wl-rl li{position:relative;margin-bottom:var(--s-3)}
      .wl-rl li:before{content:"—";position:absolute;left:calc(-1 * var(--s-6));color:var(--text-3)}
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
    toggleSection, toggleKind, jumpSection,
    deleteReassigning, deleteForcing, closeModal,
    setLoreFilter, setLoreQuery, clearLoreFilter,
    addEntity, saveEntity, setStatus, addFact, overrideCanon,
    setEntityMode,
    _retry: null,
  };
})();
