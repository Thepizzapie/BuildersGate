/* Art seat workspace — the flagship seat.
 *
 * Sections:
 *   1. Active-item picker      — focus an art work item (or browse all)
 *   2. Anchoring panel         — RefManager.mount (global + per-task refs)
 *   3. Flow map                — hand-rolled SVG: which assets are rigged into Godot
 *   4. Iteration lab           — filmstrip of every candidate revision, each shown
 *                                side-by-side with the reference it was drawn against,
 *                                with approve / reject / regenerate per card
 *   5. Art-QA reviewer control — dispatch an INDEPENDENT qa reviewer + live activity
 *
 * Contract: never throw uncaught (would blank the seat). Everything is guarded.
 */
(function () {
  window.SeatWS = window.SeatWS || {};

  const A = {
    label: "Art",
    glyph: "▲",

    // --- module state (survives refresh ticks) ---------------------------
    _bg: null,
    _els: null,            // { picker, refs, flow, lab } container nodes
    _arts: [],             // all artifact revisions
    _groups: [],           // assets/workspace groups (for the flow map)
    _queueArt: [],         // art work items (for the picker)
    _logical: null,        // selected logical asset name
    _itemOnly: true,       // filter lab to the active item's assets
    _reviewers: {},        // logical_name -> review work-item id (for activity poll)
    _refItem: undefined,   // itemId the RefManager is currently mounted for
    _detailSig: "",        // signature of the rendered detail (skip needless re-render)
    _onItem: null,         // bound window-event handler

    // --- entry -----------------------------------------------------------
    render(container, bg) {
      try {
        this._bg = bg;
        this._detailSig = "";
        try { this._logical = localStorage.getItem("art-logical") || null; } catch (e) {}
        container.innerHTML = STYLE + `
          <div class="art-root">
            <div class="art-pick" id="art-picker"></div>
            <div class="art-cols">
              <div class="art-side">
                <div class="art-card"><div class="art-h">▲ References &amp; anchoring</div>
                  <div id="art-refs"></div></div>
                <div class="art-card"><div class="art-h">⧉ Flow map — assets rigged into Godot</div>
                  <div id="art-flow" class="art-flowwrap"><div class="art-empty">loading…</div></div></div>
              </div>
              <div class="art-main">
                <div class="art-card" id="art-lab">
                  <div class="art-h">Iteration lab</div>
                  <div class="art-empty">loading candidates…</div>
                </div>
              </div>
            </div>
            <div class="art-lightbox" id="art-lightbox" hidden></div>
          </div>`;
        this._els = {
          picker: container.querySelector("#art-picker"),
          refs: container.querySelector("#art-refs"),
          flow: container.querySelector("#art-flow"),
          lab: container.querySelector("#art-lab"),
          lightbox: container.querySelector("#art-lightbox"),
        };
        // fix the stray glyph typo in a way that can't break: overwrite header text
        const labH = this._els.lab.querySelector(".art-h");
        if (labH) labH.textContent = "◎ Iteration lab";

        // react to shared active-item changes from other seats
        if (this._onItem) window.removeEventListener("bgws-item", this._onItem);
        this._onItem = () => { try { this._syncItem(); } catch (e) {} };
        window.addEventListener("bgws-item", this._onItem);

        this._els.lightbox.addEventListener("click", () => this._closeLightbox());

        this._loadAll(true);
      } catch (e) {
        try { container.innerHTML = `<div class="art-empty">workspace error: ${bg.esc(e.message)}</div>`; } catch (_) {}
        console.error("[art] render", e);
      }
    },

    // called ~every 3s while the seat is visible
    refresh() {
      try {
        if (!this._bg || !this._els) return;
        this._loadAll(false);
        this._pollReviewer();
      } catch (e) { console.error("[art] refresh", e); }
    },

    // --- data ------------------------------------------------------------
    async _loadAll(full) {
      const bg = this._bg;
      const [arts, ws, queue] = await Promise.all([
        bg.get("/api/artifacts").catch(() => ({ artifacts: [] })),
        bg.get("/api/assets/workspace").catch(() => ({ groups: [] })),
        bg.get("/api/queue").catch(() => ({ items: [] })),
      ]);
      this._arts = (arts && arts.artifacts) || [];
      this._groups = (ws && ws.groups) || [];
      this._queueArt = ((queue && queue.items) || []).filter(i => i && i.seat === "art");

      this._renderPicker();
      this._renderFlow();
      // ensure a valid selection
      const names = this._logicalNames();
      if (!this._logical || names.indexOf(this._logical) === -1) {
        this._logical = names[0] || null;
      }
      this._renderLab(full);
      if (full || this._refItem !== this._bg.activeItem) this._mountRefs();
    },

    _groupMap() {
      const m = {};
      for (const a of this._arts) {
        if (!a || !a.logical_name) continue;
        (m[a.logical_name] = m[a.logical_name] || []).push(a);
      }
      for (const k in m) m[k].sort((x, y) => (y.revision || 0) - (x.revision || 0));
      return m;
    },

    _logicalNames() {
      const m = this._groupMap();
      let names = Object.keys(m).sort((a, b) => a.localeCompare(b));
      const item = this._bg.activeItem;
      if (item && this._itemOnly) {
        const filtered = names.filter(n => m[n].some(a => a.work_item_id === item));
        if (filtered.length) return filtered;   // only narrow when it isn't empty
      }
      return names;
    },

    // --- 1. active-item picker ------------------------------------------
    _renderPicker() {
      const bg = this._bg, el = this._els.picker;
      const active = bg.activeItem;
      const opts = ['<option value="">— browse all assets —</option>'].concat(
        this._queueArt.map(i =>
          `<option value="${i.id}" ${i.id === active ? "selected" : ""}>#${i.id} · ${bg.esc((i.title || "").slice(0, 60))} · ${bg.esc(i.status || "")}</option>`)
      ).join("");
      const item = this._queueArt.find(i => i.id === active);
      el.innerHTML = `
        <label class="art-pl">Focus item</label>
        <select class="art-sel" id="art-item-sel">${opts}</select>
        ${item ? `<label class="art-chk"><input type="checkbox" id="art-itemonly" ${this._itemOnly ? "checked" : ""}> only this item's assets</label>` : ""}
        <button class="art-btn art-primary" id="art-review-all" title="Dispatch an independent QA reviewer over every current candidate">Run QA review · all candidates</button>`;
      const sel = el.querySelector("#art-item-sel");
      if (sel) sel.onchange = () => { bg.setActiveItem(sel.value || null); };
      const chk = el.querySelector("#art-itemonly");
      if (chk) chk.onchange = () => { this._itemOnly = chk.checked; this._detailSig = ""; this._loadAll(true); };
      const rall = el.querySelector("#art-review-all");
      if (rall) rall.onclick = () => this._runReview({}, "__all__", rall);
    },

    _syncItem() {
      // active item changed elsewhere: refresh picker selection + lab filter
      this._detailSig = "";
      this._renderPicker();
      const names = this._logicalNames();
      if (!this._logical || names.indexOf(this._logical) === -1) this._logical = names[0] || null;
      this._renderLab(true);
      this._mountRefs();
    },

    // --- 2. anchoring panel ---------------------------------------------
    _mountRefs() {
      try {
        const itemId = this._bg.activeItem || null;
        this._refItem = this._bg.activeItem;
        if (window.RefManager && typeof window.RefManager.mount === "function") {
          window.RefManager.mount(this._els.refs, { itemId });
        } else {
          this._els.refs.innerHTML = '<div class="art-empty">reference manager unavailable</div>';
        }
      } catch (e) {
        this._els.refs.innerHTML = '<div class="art-empty">reference panel error</div>';
        console.error("[art] refs", e);
      }
    },

    // --- 3. flow map (hand-rolled SVG) ----------------------------------
    _renderFlow() {
      const bg = this._bg, host = this._els.flow;
      try {
        const groups = (this._groups || []).filter(g => g && g.logical_name);
        if (!groups.length) { host.innerHTML = '<div class="art-empty">no assets yet</div>'; return; }
        const statusOf = (g) => {
          if (g.approved) return "approved";
          const revs = g.revisions || [];
          if (revs.some(r => r.status === "candidate")) return "candidate";
          if (revs.length && revs.every(r => r.status === "rejected")) return "rejected";
          return "other";
        };
        const rigged = (g) => (g.revisions || []).some(r =>
          (r.engine_import && Object.keys(r.engine_import).length) || r.used_in_current_build);
        const color = { approved: "var(--good)", candidate: "var(--warn)", rejected: "var(--bad)", other: "var(--ash2)" };

        const cols = 3, nodeW = 150, nodeH = 30, gapX = 26, gapY = 12, padL = 12, padT = 12;
        const rows = Math.ceil(groups.length / cols);
        const gx = padL + cols * (nodeW + gapX) + 70;   // godot column x
        const width = gx + 96;
        const height = Math.max(rows * (nodeH + gapY) + padT * 2, 120);
        const gy = height / 2 - nodeH / 2;
        const anyRig = groups.some(rigged);

        let edges = "", nodes = "";
        groups.forEach((g, i) => {
          const c = Math.floor(i / rows), r = i % rows;   // fill column-major so columns are short
          const x = padL + c * (nodeW + gapX);
          const y = padT + r * (nodeH + gapY);
          const st = statusOf(g);
          const sel = g.logical_name === this._logical;
          const label = g.logical_name.length > 20 ? g.logical_name.slice(0, 19) + "…" : g.logical_name;
          if (rigged(g)) {
            edges += `<path d="M${x + nodeW} ${y + nodeH / 2} C ${x + nodeW + 40} ${y + nodeH / 2}, ${gx - 40} ${gy + nodeH / 2}, ${gx} ${gy + nodeH / 2}" fill="none" stroke="var(--ember)" stroke-width="1.5" opacity="0.7"/>`;
          }
          nodes += `<g class="art-fnode" data-logical="${bg.esc(g.logical_name)}" style="cursor:pointer">
            <rect x="${x}" y="${y}" width="${nodeW}" height="${nodeH}" rx="7"
              fill="var(--plate)" stroke="${sel ? "var(--bone)" : color[st]}" stroke-width="${sel ? 2 : 1.3}"/>
            <circle cx="${x + 12}" cy="${y + nodeH / 2}" r="4" fill="${color[st]}"/>
            <text x="${x + 22}" y="${y + nodeH / 2 + 4}" fill="var(--bone)" font-size="11">${bg.esc(label)}</text>
          </g>`;
        });
        const godot = `<g>
          <rect x="${gx}" y="${gy}" width="84" height="${nodeH + 8}" rx="8" fill="var(--plate2)" stroke="var(--ember)" stroke-width="1.6"/>
          <text x="${gx + 42}" y="${gy + nodeH / 2 + 5}" fill="var(--ember)" font-size="12" font-weight="600" text-anchor="middle">GODOT</text>
        </g>`;
        host.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" class="art-svg">
          ${edges}${nodes}${anyRig ? godot : ""}
        </svg>
        <div class="art-legend">
          <span><i style="background:var(--good)"></i>approved</span>
          <span><i style="background:var(--warn)"></i>candidate</span>
          <span><i style="background:var(--bad)"></i>rejected</span>
          <span class="art-lg-edge">— rigged into Godot</span>
          ${anyRig ? "" : '<span class="art-muted">no asset imported into the engine yet</span>'}
        </div>`;
        host.querySelectorAll(".art-fnode").forEach(n =>
          n.addEventListener("click", () => this._selectLogical(n.dataset.logical)));
      } catch (e) {
        host.innerHTML = '<div class="art-empty">flow map error</div>';
        console.error("[art] flow", e);
      }
    },

    _selectLogical(name) {
      if (!name) return;
      this._logical = name;
      try { localStorage.setItem("art-logical", name); } catch (e) {}
      this._detailSig = "";
      this._renderFlow();
      this._renderLab(true);
    },

    // --- 4 + 5. iteration lab + QA control ------------------------------
    _renderLab(full) {
      const bg = this._bg, host = this._els.lab;
      try {
        const m = this._groupMap();
        const names = this._logicalNames();
        if (!names.length) {
          host.innerHTML = `<div class="art-h">◎ Iteration lab</div>
            <div class="art-empty">No artifacts yet. When the art seat produces candidate images they appear here — every revision as a thumbnail, each beside the reference it was drawn against.</div>`;
          return;
        }
        // signature: re-render detail only when the selected group's state changed
        const grp = m[this._logical] || [];
        const sig = this._logical + "|" + grp.map(a =>
          a.id + ":" + a.status + ":" + this._verdictSig(a)).join(",") +
          "|rv:" + Object.keys(this._reviewers).join(",");
        if (!full && sig === this._detailSig) { this._renderLabList(m, names); return; }
        this._detailSig = sig;

        host.innerHTML = `<div class="art-h">◎ Iteration lab</div>
          <div class="art-lab">
            <div class="art-list" id="art-list"></div>
            <div class="art-detail" id="art-detail"></div>
          </div>`;
        this._renderLabList(m, names);
        this._renderDetail(m);
      } catch (e) {
        host.innerHTML = '<div class="art-empty">iteration lab error</div>';
        console.error("[art] lab", e);
      }
    },

    _renderLabList(m, names) {
      const bg = this._bg;
      const list = this._els.lab.querySelector("#art-list");
      if (!list) return;
      list.innerHTML = names.map(n => {
        const g = m[n] || [];
        const approved = g.some(a => a.status === "approved" || a.status === "integrated");
        const cand = g.filter(a => a.status === "candidate").length;
        const dot = approved ? "var(--good)" : (cand ? "var(--warn)" : "var(--ash2)");
        return `<button class="art-lrow ${n === this._logical ? "sel" : ""}" data-logical="${bg.esc(n)}">
          <span class="art-dot" style="background:${dot}"></span>
          <span class="art-lname">${bg.esc(n)}</span>
          <span class="art-lcount">${g.length}${cand ? " · " + cand + "c" : ""}</span>
        </button>`;
      }).join("");
      list.querySelectorAll(".art-lrow").forEach(b =>
        b.addEventListener("click", () => this._selectLogical(b.dataset.logical)));
    },

    _verdictSig(a) {
      const qa = (a.metadata && a.metadata.qa_review) || null;
      return qa ? (qa.verdict || "?") + (qa.score != null ? qa.score : "") : "-";
    },

    _projRel(p) {
      const s = String(p || "").replace(/\\/g, "/");
      const i = s.indexOf(".bgate");
      return i !== -1 ? s.slice(i) : null;   // null => not previewable, skip
    },
    _revRel(a) {
      // Every generation overwrites <name>_sheet.png, so a.path resolves to the
      // LATEST render for EVERY revision (why the history looked identical).
      // The per-revision archived preview is unique and preserved — use it so
      // each iteration shows its own actual image.
      const arch = a && a.metadata && a.metadata.preview;
      return this._projRel(arch) || this._projRel(a.path) || a.path;
    },

    _refsFor(a) {
      const meta = a.metadata || {};
      let refs = Array.isArray(meta.resolved_refs) ? meta.resolved_refs.slice() : [];
      if (!refs.length && Array.isArray(a.refs)) refs = a.refs.slice();
      return refs.map(r => this._projRel(r)).filter(Boolean);
    },

    _renderDetail(m) {
      const bg = this._bg;
      const host = this._els.lab.querySelector("#art-detail");
      if (!host) return;
      const name = this._logical;
      const g = (m[name] || []).slice();   // newest first (already sorted)
      if (!g.length) { host.innerHTML = '<div class="art-empty">pick an asset on the left</div>'; return; }

      const badge = (s) => {
        const col = { candidate: "var(--warn)", approved: "var(--good)", integrated: "var(--good)",
          rejected: "var(--bad)", superseded: "var(--ash2)" }[s] || "var(--ash2)";
        return `<span class="art-badge" style="color:${col};border-color:${col}">${bg.esc(s)}</span>`;
      };
      const qaBadge = (a) => {
        const qa = (a.metadata && a.metadata.qa_review) || null;
        if (!qa) return "";
        const pass = qa.verdict === "pass";
        const col = pass ? "var(--good)" : "var(--bad)";
        const sc = qa.score != null ? " " + qa.score : "";
        return `<span class="art-badge art-qab" style="color:${col};border-color:${col}"
          title="${bg.esc(qa.reasons || "")}">QA ${bg.esc(qa.verdict || "?")}${sc}</span>`;
      };

      // filmstrip = every revision thumbnail
      const strip = g.map(a => {
        const rel = this._revRel(a);
        return `<div class="art-frame" data-id="${a.id}" title="r${a.revision} · ${bg.esc(a.status)}">
          <img src="${bg.preview(rel)}" onerror="this.style.opacity=.15" alt="">
          <span class="art-fr-r">r${a.revision}</span></div>`;
      }).join("");

      // candidate/revision cards (candidates first, then the rest)
      const order = g.slice().sort((a, b) => {
        const rank = s => s === "candidate" ? 0 : (s === "approved" || s === "integrated") ? 1 : 2;
        return rank(a.status) - rank(b.status) || (b.revision || 0) - (a.revision || 0);
      });
      const cards = order.map(a => {
        const rel = this._revRel(a);
        const refs = this._refsFor(a);
        const refThumbs = refs.length
          ? refs.map(r => `<img class="art-refimg" src="${bg.preview(r)}" onerror="this.style.opacity=.15" data-prev="${bg.esc(r)}" alt="ref">`).join("")
          : '<div class="art-noref">no reference on record</div>';
        const isCand = a.status === "candidate";
        return `<div class="art-cand">
          <div class="art-cand-top">
            <span class="art-rev">r${a.revision}</span>
            ${badge(a.status)}${qaBadge(a)}
            <span class="art-model">${bg.esc(a.model || a.producer || "")}</span>
          </div>
          <div class="art-compare">
            <div class="art-side-img">
              <div class="art-caplabel">candidate</div>
              <img class="art-prodimg" src="${bg.preview(rel)}" onerror="this.style.opacity=.15" data-prev="${bg.esc(rel)}" alt="candidate">
            </div>
            <div class="art-vs">vs</div>
            <div class="art-side-img art-refs">
              <div class="art-caplabel">reference${refs.length > 1 ? "s" : ""}</div>
              <div class="art-refrow">${refThumbs}</div>
            </div>
          </div>
          <div class="art-actions">
            <button class="art-btn art-ok" data-act="approve" data-id="${a.id}" ${a.status === "approved" ? "disabled" : ""}>Approve</button>
            <button class="art-btn art-no" data-act="reject" data-id="${a.id}">Reject</button>
            <button class="art-btn" data-act="regen" data-id="${a.id}">Regenerate</button>
            <button class="art-btn" data-act="restore" data-id="${a.id}" title="Make this revision the live sheet the game uses">Restore</button>
          </div>
        </div>`;
      }).join("");

      const reviewerId = this._reviewers[name];
      host.innerHTML = `
        <div class="art-detail-head">
          <div class="art-dtitle">${bg.esc(name)}</div>
          <button class="art-btn art-primary" id="art-review-one">Run independent QA review</button>
        </div>
        <div class="art-strip">${strip}</div>
        <div id="art-qa-activity">${reviewerId ? '<div class="art-muted">QA reviewer dispatched — activity loading…</div>' : ""}</div>
        <div class="art-cards">${cards}</div>`;

      // wire actions
      host.querySelectorAll(".art-frame img, .art-prodimg, .art-refimg").forEach(img =>
        img.addEventListener("click", (e) => {
          e.stopPropagation();
          const src = img.dataset.prev ? bg.preview(img.dataset.prev) : img.src;
          this._openLightbox(src);
        }));
      const one = host.querySelector("#art-review-one");
      if (one) one.onclick = () => this._runReview({ logical_name: name }, name, one);
      host.querySelectorAll(".art-actions button").forEach(b =>
        b.addEventListener("click", () => this._cardAction(b.dataset.act, Number(b.dataset.id), b)));
      if (reviewerId) this._pollReviewer();
    },

    async _cardAction(act, id, btn) {
      const bg = this._bg;
      if (!id) return;
      try {
        if (act === "approve") {
          btn.disabled = true;
          const r = await bg.post(`/api/artifacts/${id}/review`, { status: "approved" });
          if (r && r.error) { bg.toast(r.error, true); btn.disabled = false; }
          else { bg.toast("approved"); this._detailSig = ""; this._loadAll(false); }
        } else if (act === "reject") {
          const note = prompt("Reject reason (what's off-model?):");
          if (note == null) return;
          const r = await bg.post(`/api/artifacts/${id}/review`, { status: "rejected", note });
          if (r && r.error) bg.toast(r.error, true);
          else { bg.toast("rejected"); this._detailSig = ""; this._loadAll(false); }
        } else if (act === "regen") {
          const reason = prompt("Regenerate — what should improve?", "produce a stronger candidate");
          if (reason == null) return;
          const r = await bg.post(`/api/artifacts/${id}/regenerate`, { reason });
          if (r && r.error) bg.toast(r.error, true);
          else bg.toast("regeneration queued (item #" + (r.id || "?") + ")");
        } else if (act === "restore") {
          if (!confirm("Restore this revision as the live sheet the game uses?")) return;
          btn.disabled = true;
          const r = await bg.post(`/api/artifacts/${id}/restore`, {});
          if (r && r.error) { bg.toast(r.error, true); btn.disabled = false; }
          else { bg.toast("restored r" + (r.restored_revision ?? "") + " as the live sheet"); this._detailSig = ""; this._loadAll(false); }
        }
      } catch (e) { bg.toast("action failed", true); console.error("[art] action", e); }
    },

    async _runReview(body, key, btn) {
      const bg = this._bg;
      try {
        if (btn) { btn.disabled = true; btn.textContent = "dispatching…"; }
        const r = await bg.post("/api/art-qa/review", body);
        if (!r || r.ok === false || r.error) {
          bg.toast((r && r.error) || "QA review failed", true);
        } else {
          bg.toast(`QA reviewer dispatched (${r.candidate_count || 0} candidates)`);
          if (r.review_item_id) {
            if (key === "__all__") this._reviewers["__all__"] = r.review_item_id;
            else this._reviewers[key] = r.review_item_id;
            this._detailSig = "";
            this._renderLab(true);
            this._pollReviewer();
          }
        }
      } catch (e) { bg.toast("QA review failed", true); console.error("[art] review", e); }
      finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = key === "__all__" ? "Run QA review · all candidates" : "Run independent QA review";
        }
      }
    },

    async _pollReviewer() {
      const bg = this._bg;
      const host = this._els && this._els.lab.querySelector("#art-qa-activity");
      const id = this._reviewers[this._logical] || this._reviewers["__all__"];
      if (!host || !id) return;
      try {
        const act = await bg.get(`/api/agent-activity/${id}`).catch(() => null);
        if (!act) { host.innerHTML = ""; return; }
        const steps = (act.steps || []).slice(-8);
        const running = act.running;
        const rows = steps.map(s => {
          if (s.kind === "tool") return `<div class="art-step"><span class="art-sk art-tool">${bg.esc(s.name)}</span> <span class="art-shint">${bg.esc(s.hint || "")}</span></div>`;
          if (s.kind === "result") return `<div class="art-step"><span class="art-sk art-res">result</span> ${bg.esc((s.text || "").slice(0, 120))}</div>`;
          if (s.kind === "steer") return `<div class="art-step"><span class="art-sk">steer</span> ${bg.esc(s.text || "")}</div>`;
          return `<div class="art-step art-say">${bg.esc((s.text || "").slice(0, 160))}</div>`;
        }).join("");
        const fin = act.final ? `<div class="art-final">✓ ${bg.esc((act.final.text || "").slice(0, 200))}</div>` : "";
        host.innerHTML = `<div class="art-qapanel">
          <div class="art-qah">${running ? '<span class="art-live">● live</span>' : '<span class="art-muted">done</span>'} independent QA reviewer · item #${id}</div>
          ${rows || '<div class="art-muted">waiting for the reviewer to start…</div>'}${fin}</div>`;
      } catch (e) { /* leave prior content */ }
    },

    // --- lightbox --------------------------------------------------------
    _openLightbox(src) {
      const lb = this._els.lightbox;
      if (!lb) return;
      lb.innerHTML = `<img src="${src}" alt=""><div class="art-lbx">✕ click anywhere to close</div>`;
      lb.hidden = false;
    },
    _closeLightbox() {
      const lb = this._els.lightbox;
      if (lb) { lb.hidden = true; lb.innerHTML = ""; }
    },
  };

  const STYLE = `<style>
    .art-root{color:var(--bone);font-size:13px}
    .art-card{background:var(--plate);border:1px solid var(--seam);border-radius:12px;padding:14px;margin-bottom:14px}
    .art-h{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--ash);margin-bottom:12px}
    .art-empty{color:var(--ash);font-size:12px;padding:10px 4px;line-height:1.5}
    .art-muted{color:var(--ash);font-size:11px}
    .art-btn{padding:6px 11px;background:var(--plate2);border:1px solid var(--seam);border-radius:8px;color:var(--bone);font:inherit;font-size:12px;cursor:pointer}
    .art-btn:hover{border-color:var(--ember)}
    .art-btn:disabled{opacity:.45;cursor:default}
    .art-primary{background:var(--plate2);border-color:var(--ember);color:var(--ember)}
    .art-ok{color:var(--good);border-color:var(--good)}
    .art-no{color:var(--bad);border-color:var(--bad)}
    /* picker */
    .art-pick{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:var(--plate);border:1px solid var(--seam);border-radius:12px;padding:12px 14px;margin-bottom:14px}
    .art-pl{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--ash)}
    .art-sel{padding:6px 9px;background:var(--plate2);border:1px solid var(--seam);border-radius:8px;color:var(--bone);font:inherit;font-size:12px;max-width:420px}
    .art-chk{font-size:12px;color:var(--ash);display:flex;align-items:center;gap:6px;cursor:pointer}
    #art-review-all{margin-left:auto}
    /* columns */
    .art-cols{display:flex;gap:14px;align-items:flex-start}
    .art-side{width:340px;flex:0 0 340px}
    .art-main{flex:1;min-width:0}
    @media(max-width:1080px){.art-cols{flex-direction:column}.art-side{width:100%;flex:none}}
    /* flow map */
    .art-flowwrap{overflow:auto;max-height:320px}
    .art-svg{display:block}
    .art-legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;font-size:11px;color:var(--ash);align-items:center}
    .art-legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:middle}
    .art-lg-edge{color:var(--ember)}
    /* lab */
    .art-lab{display:flex;gap:12px;align-items:flex-start}
    .art-list{width:210px;flex:0 0 210px;max-height:560px;overflow:auto;display:flex;flex-direction:column;gap:3px}
    .art-lrow{display:flex;align-items:center;gap:7px;padding:6px 8px;background:transparent;border:1px solid transparent;border-radius:7px;color:var(--bone);cursor:pointer;text-align:left;font:inherit;font-size:12px;width:100%}
    .art-lrow:hover{background:var(--plate2)}
    .art-lrow.sel{background:var(--plate2);border-color:var(--seam2)}
    .art-dot{width:8px;height:8px;border-radius:50%;flex:0 0 8px}
    .art-lname{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .art-lcount{color:var(--ash);font-size:10px}
    .art-detail{flex:1;min-width:0}
    @media(max-width:720px){.art-lab{flex-direction:column}.art-list{width:100%;flex:none;flex-direction:row;flex-wrap:wrap}}
    .art-detail-head{display:flex;align-items:center;gap:12px;margin-bottom:10px}
    .art-dtitle{font-size:15px;font-weight:600;color:var(--bone);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    /* filmstrip */
    .art-strip{display:flex;gap:8px;overflow-x:auto;padding-bottom:8px;margin-bottom:12px;border-bottom:1px solid var(--seam)}
    .art-frame{position:relative;flex:0 0 auto;width:78px;height:78px;border:1px solid var(--seam);border-radius:8px;overflow:hidden;background:var(--void);cursor:pointer}
    .art-frame img{width:100%;height:100%;object-fit:contain}
    .art-fr-r{position:absolute;bottom:2px;right:3px;font-size:9px;background:rgba(0,0,0,.65);color:var(--bone);padding:1px 4px;border-radius:4px}
    /* candidate cards */
    .art-cards{display:flex;flex-direction:column;gap:12px}
    .art-cand{background:var(--plate);border:1px solid var(--seam);border-radius:10px;padding:12px}
    .art-cand-top{display:flex;align-items:center;gap:8px;margin-bottom:10px}
    .art-rev{font-weight:600;color:var(--bone)}
    .art-model{color:var(--ash);font-size:11px;margin-left:auto}
    .art-badge{font-size:10px;text-transform:uppercase;letter-spacing:.04em;border:1px solid;border-radius:5px;padding:1px 6px}
    .art-qab{cursor:help}
    .art-compare{display:flex;align-items:stretch;gap:10px}
    .art-side-img{flex:1;min-width:0}
    .art-side-img.art-refs{flex:1}
    .art-caplabel{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--ash);margin-bottom:4px}
    .art-prodimg{width:100%;max-height:260px;object-fit:contain;background:var(--void);border:1px solid var(--seam);border-radius:8px;cursor:zoom-in;display:block}
    .art-vs{align-self:center;color:var(--ash2);font-size:11px;font-style:italic}
    .art-refrow{display:flex;gap:6px;flex-wrap:wrap}
    .art-refimg{width:96px;height:120px;object-fit:contain;background:var(--void);border:1px solid var(--ember);border-radius:8px;cursor:zoom-in}
    .art-noref{color:var(--ash);font-size:11px;border:1px dashed var(--seam);border-radius:8px;padding:12px;text-align:center}
    .art-actions{display:flex;gap:8px;margin-top:10px}
    /* qa activity */
    .art-qapanel{background:var(--plate);border:1px solid var(--seam);border-radius:9px;padding:10px;margin-bottom:12px}
    .art-qah{font-size:11px;color:var(--ember);margin-bottom:6px}
    .art-live{color:var(--good)}
    .art-step{font-size:11px;color:var(--ash);padding:2px 0;line-height:1.4}
    .art-sk{display:inline-block;font-size:9px;text-transform:uppercase;letter-spacing:.04em;padding:1px 5px;border-radius:4px;background:var(--plate2);color:var(--ember);margin-right:5px}
    .art-tool{background:var(--plate2);color:var(--ember)}
    .art-res{background:var(--plate2);color:var(--good)}
    .art-say{color:var(--bone);font-style:italic}
    .art-shint{color:var(--ash)}
    .art-final{margin-top:6px;font-size:12px;color:var(--good)}
    /* lightbox */
    .art-lightbox{position:fixed;inset:0;background:rgba(4,6,9,.9);z-index:9998;display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:zoom-out;padding:24px}
    .art-lightbox[hidden]{display:none}
    .art-lightbox img{max-width:92vw;max-height:86vh;object-fit:contain;border-radius:8px;box-shadow:0 10px 60px rgba(0,0,0,.6)}
    .art-lbx{margin-top:14px;color:var(--ash);font-size:12px}
  </style>`;

  window.SeatWS.art = A;
})();
