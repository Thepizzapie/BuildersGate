/* Art seat workspace — the flagship seat.
 *
 * Sections:
 *   1. Active-item picker      — focus an art work item (or browse all)
 *   2. Anchoring panel         — RefManager.mount (global + per-task refs)
 *   3. Flow map                — hand-rolled SVG: which assets are rigged into Godot
 *   4. Iteration lab           — filmstrip of every candidate revision, each shown
 *                                side-by-side with the reference it was drawn against,
 *                                with approve / reject / regenerate per card,
 *                                batch triage, and a compare lightbox
 *   5. Art-QA reviewer control — dispatch an INDEPENDENT qa reviewer + live activity
 *
 * The technical-art-lead blocker this answers: "one image next to one reference
 * at 260px and a prompt() box is not enough evidence to sign off on art that
 * ships." So: any two frames can be stacked with an opacity slider, a CSS
 * `difference` blend and a computed palette delta; animations loop at their
 * .tres fps; twelve frames are approved in one keystroke with one shared
 * reason; every candidate carries what it cost and what the consistency and
 * sequence machinery already measured about it.
 *
 * Contract: never throw uncaught (would blank the seat). Everything is guarded,
 * and a failed fetch renders its error message — never an empty panel.
 */
(function () {
  const BGICON = (n) => (window.BGIcon ? BGIcon(n, { size: 15 }) : "");
  window.SeatWS = window.SeatWS || {};

  // Documented thresholds. These live in one place so a chip's colour and the
  // tooltip that explains it can never disagree.
  const T = {
    SIMILARITY: 0.55,   // docs/character-consistency.md — <0.55 vs the ref = drift
    PALETTE_DRIFT: 30,  // consistency_check palette_note — >30 = color drift likely
    IDENTITY: 78,       // server _CONSISTENCY_FLOOR — vision identity score 0-100
  };

  const A = {
    label: "Art",
    glyph: BGICON("art"),

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
    _onKey: null,          // bound document keydown handler
    _sel: null,            // Set of artifact ids selected for batch triage
    _cmp: null,            // [artifactId, artifactId] picked for compare
    _focusId: null,        // filmstrip cursor (j/k)
    _strip: [],            // artifact ids in filmstrip order
    _cost: { by_logical: {}, prices: {} },
    _animTimer: null,      // looping SpriteFrames preview
    _locks: null,          // GET /api/locks — held claims, waiters, path leases

    // --- entry -----------------------------------------------------------
    render(container, bg) {
      try {
        this._bg = bg;
        this._detailSig = "";
        this._sel = new Set();
        this._cmp = [];
        this._focusId = null;
        try { this._logical = localStorage.getItem("art-logical") || null; } catch (e) {}
        container.innerHTML = STYLE + `
          <div class="art-root">
            <div class="art-pick" id="art-picker"></div>
            <div class="art-cols">
              <div class="art-side">
                <div class="art-card"><h3 class="art-h">${BGICON("reference")} References &amp; anchoring</h3>
                  <div id="art-refs"></div></div>
                <div class="art-card"><h3 class="art-h">⧉ Flow map — assets rigged into Godot</h3>
                  <div id="art-flow" class="art-flowwrap"><div class="art-empty">loading…</div></div></div>
                <div class="art-card"><h3 class="art-h">⛨ Locks &amp; contention</h3>
                  <div id="art-locks"><div class="art-empty">loading locks…</div></div></div>
              </div>
              <div class="art-main">
                <!-- Full width, above the lab, because this is not a reference
                     panel: it decides what EVERY image this project generates
                     looks like. The anchors are shown rather than counted —
                     "6 of 26" is the least useful way to describe a dataset of
                     pictures, and "which of my anchors is in this" is the
                     question actually being asked. -->
                <div class="art-card art-stcard"><h3 class="art-h">${BGICON("art")} Trained style
                  <span class="art-sth" id="art-stmode"></span></h3>
                  <div id="art-style"><div class="art-empty">loading…</div></div></div>
                <div class="art-card" id="art-lab">
                  <h3 class="art-h">Iteration lab</h3>
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
          locks: container.querySelector("#art-locks"),
          lab: container.querySelector("#art-lab"),
          lightbox: container.querySelector("#art-lightbox"),
          style: container.querySelector("#art-style"),
          stmode: container.querySelector("#art-stmode"),
        };
        // fix the stray glyph typo in a way that can't break: overwrite header text
        const labH = this._els.lab.querySelector(".art-h");
        if (labH) labH.innerHTML = BGICON("consistency") + " Iteration lab";

        // react to shared active-item changes from other seats
        if (this._onItem) window.removeEventListener("bgws-item", this._onItem);
        this._onItem = () => { try { this._syncItem(); } catch (e) {} };
        window.addEventListener("bgws-item", this._onItem);

        // keyboard triage — bound once, guarded so it never fires while the
        // user is typing a reason into a textarea.
        if (this._onKey) document.removeEventListener("keydown", this._onKey);
        this._onKey = (e) => { try { this._key(e); } catch (err) {} };
        document.addEventListener("keydown", this._onKey);

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

    // --- response shape helpers ------------------------------------------
    // New route modules answer {ok:true,data}; app.py's older endpoints answer a
    // bare payload. Both are normal here — neither may produce a blank panel.
    _data(r) {
      if (r && typeof r === "object" && r.ok === true && "data" in r) return r.data;
      return r;
    },
    _err(r) {
      if (!r) return "no response from the server";
      if (r.ok === false || r.error) {
        const e = r.error;
        if (!e) return "request failed";
        if (typeof e === "string") return e;
        return e.message || e.code || "request failed";
      }
      return null;
    },

    // --- data ------------------------------------------------------------
    async _loadAll(full) {
      const bg = this._bg;
      const [arts, ws, queue, cost, locks, style] = await Promise.all([
        bg.get("/api/artifacts").catch(() => ({ artifacts: [] })),
        bg.get("/api/assets/workspace").catch(() => ({ groups: [] })),
        bg.get("/api/queue").catch(() => ({ items: [] })),
        bg.get("/api/art/cost").catch(() => null),
        // bg.get throws on a non-2xx, and the status is the useful part: a 404
        // means this dashboard process predates /api/locks, which is a restart,
        // not a mystery.
        bg.get("/api/locks").catch(e => ({ ok: false, error: { message: String((e && e.message) || "unreachable").slice(0, 120) } })),
        // A dashboard that predates this route 404s; the panel says so rather
        // than rendering an empty card that looks like "no styles trained".
        bg.get("/api/art/style").catch(() => null),
      ]);
      this._style = this._data(style);
      this._arts = (arts && arts.artifacts) || [];
      this._groups = (ws && ws.groups) || [];
      this._queueArt = ((queue && queue.items) || []).filter(i => i && i.seat === "art");
      const c = this._data(cost);
      if (c && typeof c === "object" && !this._err(cost)) {
        this._cost = {
          by_logical: c.by_logical || {},
          prices: c.prices || {},
          totals: c.totals || null,
        };
      }

      this._locks = { payload: this._data(locks), error: this._err(locks) };

      this._renderPicker();
      this._renderFlow();
      this._renderLocks();
      this._renderStyle();
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

    _art(id) { return this._arts.find(a => a && a.id === id) || null; },
    _meta(a) { return (a && a.metadata) || {}; },
    _money(n) {
      const v = Number(n);
      if (!isFinite(v) || v <= 0) return "$0.00";
      return "$" + (v < 0.01 ? v.toFixed(3) : v.toFixed(2));
    },
    _price(quality) {
      const p = this._cost.prices || {};
      const v = p[quality];
      return typeof v === "number" ? v : (typeof p.medium === "number" ? p.medium : 0);
    },
    // The adapter's own quality keys — never a price list invented here. If the
    // endpoint has not answered yet, fall back to the names only (the estimate
    // then reads $0.00 rather than a made-up number).
    _qualities() {
      const keys = Object.keys(this._cost.prices || {});
      return keys.length ? keys : ["low", "medium", "high", "auto"];
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
      const tot = this._cost.totals || null;
      const spent = tot && tot.by_kind && typeof tot.by_kind.image === "number"
        ? `<span class="art-spend" title="every image this project has bought, from the spend ledger">image spend ${this._money(tot.by_kind.image)}</span>` : "";
      el.innerHTML = `
        <label class="art-pl">Focus item</label>
        <select class="art-sel" id="art-item-sel">${opts}</select>
        ${item ? `<label class="art-chk"><input type="checkbox" id="art-itemonly" ${this._itemOnly ? "checked" : ""}> only this item's assets</label>` : ""}
        ${spent}
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

    /* --- 3b. locks + contention -----------------------------------------
     * The art seat is where you act on a held lock: this is the panel that
     * turns "the agent silently did nothing" into "gameplay is holding
     * hero_sheet.png and two runs are queued behind it". */
    _renderLocks() {
      const bg = this._bg, host = this._els && this._els.locks;
      if (!host) return;
      try {
        const st = this._locks || {};
        if (st.error) { host.innerHTML = `<div class="art-empty">locks unavailable — ${bg.esc(st.error)}</div>`; return; }
        const d = st.payload || {};
        const held = Array.isArray(d.held) ? d.held : [];
        const leases = Array.isArray(d.path_leases) ? d.path_leases : [];
        if (!held.length && !leases.length) {
          host.innerHTML = '<div class="art-empty">nothing locked — every asset is free to edit.</div>';
          return;
        }
        const short = p => { const s = String(p || "").replace(/\\/g, "/"); const i = s.lastIndexOf("/"); return i === -1 ? s : s.slice(i + 1); };
        const rows = held.map(h => {
          const waiters = Array.isArray(h.waiters) ? h.waiters : [];
          const mine = h.seat === "art";
          const stale = this._leaseStale(h.lease_expires_at);
          return `<div class="art-lock ${mine ? "mine" : ""}">
            <div class="art-lockrow">
              <span class="art-lockseat" style="color:${mine ? "var(--ember)" : "var(--ash)"}">${bg.esc(h.seat || "?")}</span>
              <span class="art-lockpath" title="${bg.esc(h.path)}">${bg.esc(short(h.path))}</span>
              ${h.work_item_id ? `<button class="art-btn art-lockitem" data-lockitem="${h.work_item_id}" title="focus the work item holding this">#${h.work_item_id}</button>` : ""}
            </div>
            <div class="art-lockmeta">${bg.esc(h.owner || h.actor || "unnamed holder")}${h.since ? " · since " + bg.esc(h.since) : ""}${
              stale ? ' · <span style="color:var(--bad)">lease expired</span>' : (h.lease_expires_at ? " · lease " + bg.esc(h.lease_expires_at) : "")}</div>
            ${waiters.length ? `<div class="art-lockwait">⏳ ${waiters.length} waiting: ${
              bg.esc(waiters.map(w => `${w.seat}${w.owner ? " (" + w.owner + ")" : ""}`).join(", "))}</div>` : ""}
          </div>`;
        }).join("");
        const leaseRow = leases.length
          ? `<div class="art-lockmeta" style="margin-top:8px">${leases.length} text path lease${leases.length === 1 ? "" : "s"} held: ${
              bg.esc(leases.slice(0, 6).map(l => `${short(l.path)} → ${l.seat || l.owner || "?"}`).join(", "))}</div>`
          : "";
        host.innerHTML = rows + leaseRow;
        host.querySelectorAll("[data-lockitem]").forEach(b =>
          b.addEventListener("click", () => bg.setActiveItem(Number(b.dataset.lockitem))));
      } catch (e) {
        host.innerHTML = '<div class="art-empty">lock panel error</div>';
        console.error("[art] locks", e);
      }
    },
    // The server already drops expired claims; this only colours a lease whose
    // clock ran out between polls.
    _leaseStale(when) {
      if (!when) return false;
      const t = Date.parse(String(when).replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(String(when)) ? "" : "Z"));
      return isFinite(t) && t < Date.now();
    },

    // /api/artifacts carries the raw revision rows; /api/assets/workspace folds
    // in ref_drift, the live lock and the integration record. Marry them by id
    // so a card can show evidence neither endpoint has alone.
    _wsRev(id) {
      const groups = this._groups || [];
      for (const g of groups) {
        for (const r of (g.revisions || [])) if (r && r.id === id) return r;
      }
      return null;
    },
    _driftChips(a) {
      const w = this._wsRev(a.id);
      const drift = (w && w.ref_drift) || a.ref_drift || [];
      if (!Array.isArray(drift) || !drift.length) return "";
      return drift.map(d => this._chip(
        `ref drift: ${d.name || "?"}`, "var(--bad)",
        d.detail || `${d.name} has been re-pinned since this was generated — this card is no longer evidence of what it claims`,
        "art-cons")).join("");
    },
    _liveChips(a) {
      const w = this._wsRev(a.id);
      const out = [];
      const lock = w && w.lock;
      if (lock && lock.seat) {
        out.push(this._chip(`locked · ${lock.seat}`,
          lock.seat === "art" ? "var(--warn)" : "var(--bad)",
          `${lock.path || a.path} is held by the ${lock.seat} seat` +
          (lock.owner ? ` (${lock.owner})` : "") +
          (lock.work_item_id ? ` for item #${lock.work_item_id}` : "") +
          " — regenerating it now would collide", "art-cons"));
      }
      // Approval has to MOVE the file; the backend records whether it could.
      const integ = (w && w.integration) || this._meta(a).integration;
      if (integ && typeof integ === "object" && integ.ok === false) {
        out.push(this._chip("approved, NOT live", "var(--bad)",
          `${integ.detail || "the approved revision was not installed"} — the game is still loading a different image`,
          "art-cons"));
      } else if (integ && integ.promoted) {
        out.push(this._chip("live", "var(--good)",
          `installed at ${integ.path} — this is the image the build loads`, "art-cons"));
      }
      return out.join("");
    },

    _selectLogical(name) {
      if (!name) return;
      this._logical = name;
      try { localStorage.setItem("art-logical", name); } catch (e) {}
      this._detailSig = "";
      this._sel = new Set();
      this._cmp = [];
      this._focusId = null;
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
          host.innerHTML = `<h3 class="art-h">${BGICON("consistency")} Iteration lab</h3>
            <div class="art-empty">No artifacts yet. When the art seat produces candidate images they appear here — every revision as a thumbnail, each beside the reference it was drawn against.</div>`;
          return;
        }
        // signature: re-render detail only when the selected group's state
        // changed. Selection/compare/focus are deliberately NOT in it — they are
        // synced in place so a half-typed reason survives a refresh tick.
        const grp = m[this._logical] || [];
        const sig = this._logical + "|" + grp.map(a =>
          a.id + ":" + a.status + ":" + this._verdictSig(a) + ":" + this._stateSig(a)).join(",") +
          "|rv:" + Object.keys(this._reviewers).join(",") +
          "|$:" + (this._cost.by_logical || {})[this._logical];
        if (!full && sig === this._detailSig) { this._renderLabList(m, names); return; }
        this._detailSig = sig;

        // The picker lists every logical asset — 99 of them on this project —
        // in a 560px-tall scroller with no way to narrow it. A filter costs one
        // input and turns "scroll and hope" into "type three letters".
        host.innerHTML = `<h3 class="art-h">${BGICON("consistency")} Iteration lab</h3>
          <div class="art-lab">
            <div class="art-listwrap">
              <input class="art-lfilter" id="art-lfilter" type="search"
                     placeholder="Filter assets…" aria-label="Filter assets"
                     autocomplete="off" spellcheck="false">
              <div class="art-list" id="art-list"></div>
              <div class="art-lnone" id="art-lnone" hidden>nothing matches that</div>
            </div>
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
        const jitter = g.some(a => this._seqFlags(a).length);
        const drifted = g.some(a => { const w = this._wsRev(a.id); return w && (w.ref_drift || []).length; });
        const locked = g.map(a => this._wsRev(a.id)).find(w => w && w.lock && w.lock.seat);
        const usd = (this._cost.by_logical || {})[n];
        return `<button class="art-lrow ${n === this._logical ? "sel" : ""}" data-logical="${bg.esc(n)}">
          <span class="art-dot" style="background:${dot}"></span>
          <span class="art-lname">${bg.esc(n)}</span>
          ${drifted ? '<span class="art-ldrift" title="a reference this asset was generated against has been re-pinned since">⇅</span>' : ""}
          ${locked ? `<span class="art-llock" title="held by the ${bg.esc(locked.lock.seat)} seat">⛨</span>` : ""}
          ${jitter ? '<span class="art-ljit" title="a multi-frame animation on this asset has adjacent-frame height jitter">⚡</span>' : ""}
          ${usd ? `<span class="art-lusd" title="spent on this asset">${this._money(usd)}</span>` : ""}
          <span class="art-lcount">${g.length}${cand ? " · " + cand + "c" : ""}</span>
        </button>`;
      }).join("");
      // One delegated listener on the scroller instead of 99 on the rows —
      // the list is rebuilt on every poll, so per-row binding re-ran 99 times
      // every few seconds.
      if (!list._wired) {
        list._wired = true;
        list.addEventListener("click", (ev) => {
          const row = ev.target.closest(".art-lrow");
          if (row && list.contains(row)) this._selectLogical(row.dataset.logical);
        });
      }
      this._wireFilter();
    },

    // Filtering is pure show/hide on rows already in the DOM: no refetch, and
    // it survives the poll because _renderLabList re-applies the live term.
    _wireFilter() {
      const box = this._els.lab && this._els.lab.querySelector("#art-lfilter");
      if (!box) return;
      const apply = () => {
        const q = (box.value || "").trim().toLowerCase();
        const list = this._els.lab.querySelector("#art-list");
        const none = this._els.lab.querySelector("#art-lnone");
        if (!list) return;
        let shown = 0;
        list.querySelectorAll(".art-lrow").forEach((row) => {
          const hit = !q || (row.dataset.logical || "").toLowerCase().includes(q);
          row.hidden = !hit;
          if (hit) shown++;
        });
        if (none) none.hidden = shown > 0;
      };
      if (!box._wired) { box._wired = true; box.addEventListener("input", apply); }
      apply();
    },

    _verdictSig(a) {
      const qa = (a.metadata && a.metadata.qa_review) || null;
      return qa ? (qa.verdict || "?") + (qa.score != null ? qa.score : "") : "-";
    },
    // Lock / drift / integration move without the revision row changing, so
    // they have to be part of what tells the detail pane to repaint.
    _stateSig(a) {
      const w = this._wsRev(a.id) || {};
      const lock = w.lock || {};
      const integ = w.integration || this._meta(a).integration || {};
      return [(w.ref_drift || []).length, lock.seat || "", integ.promoted ? "live" : (integ.ok === false ? "stuck" : "")].join("/");
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

    // ---- evidence chips -------------------------------------------------
    _chip(txt, col, title, cls) {
      const bg = this._bg;
      return `<span class="art-badge ${cls || ""}" style="color:${col};border-color:${col}"
        title="${bg.esc(title || "")}">${bg.esc(txt)}</span>`;
    },

    /* metadata.consistency — written by consistency_check (palette drift +
     * alpha tripwires + composite) or by the vision identity pass inside
     * image_sprites (per-frame 0-100 scores). Both shapes render; unknown
     * shapes render whatever numbers they do carry rather than nothing. */
    _consChips(a) {
      const c = this._meta(a).consistency;
      if (!c || typeof c !== "object") return "";
      const out = [];
      if (c.palette_drift != null && isFinite(Number(c.palette_drift))) {
        const d = Number(c.palette_drift);
        out.push(this._chip(`palette Δ ${d}`, d > T.PALETTE_DRIFT ? "var(--bad)" : "var(--good)",
          c.palette_note || `advisory: >${T.PALETTE_DRIFT} = color drift likely; a low value does NOT prove identity`,
          "art-cons"));
      }
      ["palette_similarity", "similarity", "unicom", "score_ratio"].forEach(k => {
        const v = c[k];
        if (typeof v === "number" && v >= 0 && v <= 1) {
          out.push(this._chip(`${k.replace(/_/g, " ")} ${v.toFixed(2)}`,
            v < T.SIMILARITY ? "var(--bad)" : "var(--good)",
            `tripwire: <${T.SIMILARITY} against the neutral reference reads as drift`, "art-cons"));
        }
      });
      if (typeof c.min === "number") {
        out.push(this._chip(`identity min ${c.min}`, c.min < T.IDENTITY ? "var(--bad)" : "var(--good)",
          `vision identity score 0-100 across the set; <${T.IDENTITY} = noticeable drift`, "art-cons"));
      }
      if (Array.isArray(c.flagged) && c.flagged.length) {
        out.push(this._chip(`drift: ${c.flagged.join(", ")}`, "var(--bad)",
          "frames the identity pass flagged for a regenerate", "art-cons"));
      }
      const al = c.alpha || {};
      if (Array.isArray(al.flags) && al.flags.length) {
        out.push(this._chip(`alpha ✕ ${al.flags.length}`, "var(--bad)", al.flags.join("\n"), "art-cons"));
      } else if (al.clean === true) {
        out.push(this._chip("alpha clean", "var(--good)",
          "no white halo / bleed / dirty alpha / hollow measured", "art-cons"));
      }
      if (Array.isArray(al.review) && al.review.length) {
        out.push(this._chip("alpha · look", "var(--warn)", al.review.join("\n"), "art-cons"));
      }
      if (c.auto_fail === true) {
        out.push(this._chip("AUTO-FAIL", "var(--bad)",
          "an alpha tripwire fired — this frame must not land", "art-cons"));
      }
      if (c.ok === false && c.error) {
        out.push(this._chip("check errored", "var(--ash2)", String(c.error), "art-cons"));
      }
      return out.join("");
    },

    /* metadata.sequence — sprites._sequence_flags: per-animation adjacent-frame
     * height jitter. Written by image_sprites (painted path). */
    _seqFlags(a) {
      const s = this._meta(a).sequence;
      if (!s || typeof s !== "object") return [];
      return Array.isArray(s.flags) ? s.flags : [];
    },
    _seqChips(a) {
      const s = this._meta(a).sequence;
      if (!s || typeof s !== "object") return "";
      const flags = this._seqFlags(a);
      if (!flags.length) {
        return Array.isArray(s.flagged)
          ? this._chip("motion steady", "var(--good)",
              "no adjacent-frame height jitter above the 18% advisory", "art-cons")
          : "";
      }
      return flags.map(f => this._chip(
        `jitter: ${f.anim} ±${Math.round((Number(f.max_adjacent_height_jump) || 0) * 100)}%`,
        "var(--warn)",
        `the drawn character height pops between adjacent frames of ${f.anim}` +
        (Array.isArray(f.height_range) ? ` (${f.height_range.join("–")}px)` : "") +
        " — advisory: a human should look, not an auto-reject",
        "art-cons")).join("");
    },

    _costChip(a) {
      const m = this._meta(a);
      const bits = [];
      if (typeof m.estimated_usd === "number" && m.estimated_usd > 0) bits.push("~" + this._money(m.estimated_usd));
      if (typeof m.seconds === "number" && m.seconds > 0) bits.push(m.seconds.toFixed(1) + "s");
      if (!bits.length) return "";
      const calls = m.image_calls ? ` over ${m.image_calls} image calls` : "";
      return this._chip(bits.join(" · "), "var(--ash)",
        `estimated spend and wall-clock latency for this revision${calls}`, "art-cost");
    },

    // ---- animations available for the looping preview -------------------
    _anims(a) {
      const m = this._meta(a);
      const frames = m.frames && typeof m.frames === "object" ? m.frames : null;
      if (!frames) return {};
      const by = {};
      Object.keys(frames).forEach(pose => {
        const rel = this._projRel(frames[pose]);
        if (!rel) return;
        const cut = pose.indexOf("/");
        const anim = cut === -1 ? pose : pose.slice(0, cut);
        const idx = cut === -1 ? 0 : parseInt(pose.slice(cut + 1), 10) || 0;
        (by[anim] = by[anim] || []).push({ idx, rel });
      });
      Object.keys(by).forEach(k => by[k].sort((x, y) => x.idx - y.idx));
      return by;
    },
    _fps(a) {
      const f = Number(this._meta(a).fps);
      return isFinite(f) && f > 0 ? f : 8;   // sprites' own default
    },

    _renderDetail(m) {
      const bg = this._bg;
      const host = this._els.lab.querySelector("#art-detail");
      if (!host) return;
      const name = this._logical;
      const g = (m[name] || []).slice();   // newest first (already sorted)
      if (!g.length) { host.innerHTML = '<div class="art-empty">pick an asset on the left</div>'; return; }

      this._strip = g.map(a => a.id);
      this._cmp = (this._cmp || []).filter(id => this._strip.indexOf(id) !== -1);
      this._sel = new Set([...(this._sel || [])].filter(id => this._strip.indexOf(id) !== -1));
      if (this._strip.indexOf(this._focusId) === -1) this._focusId = this._strip[0] || null;

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

      // filmstrip = every revision thumbnail; clicking picks it for compare
      const strip = g.map(a => {
        const rel = this._revRel(a);
        return `<div class="art-frame" data-id="${a.id}" title="r${a.revision} · ${bg.esc(a.status)} — click to pick for compare">
          <img src="${bg.preview(rel)}" onerror="this.style.opacity=.15" alt="">
          <span class="art-fr-r">r${a.revision}</span>
          <span class="art-fr-c"></span>
          <button class="art-fr-z" data-zoom="${a.id}" title="open full size">⤢</button>
        </div>`;
      }).join("");

      // any revision with per-pose frames can be played back at its .tres fps
      const playable = g.filter(a => Object.keys(this._anims(a)).length);

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
        const cons = this._meta(a).consistency || {};
        const composite = this._projRel(cons.composite);
        const evid = this._consChips(a) + this._seqChips(a) +
                     this._driftChips(a) + this._liveChips(a);
        return `<div class="art-cand" data-card="${a.id}">
          <div class="art-cand-top">
            <label class="art-cbxw" title="select for batch triage">
              <input type="checkbox" class="art-cbx" data-id="${a.id}"></label>
            <span class="art-rev">r${a.revision}</span>
            ${badge(a.status)}${qaBadge(a)}${this._costChip(a)}
            <span class="art-model">${bg.esc(a.model || a.producer || "")}</span>
          </div>
          ${evid ? `<div class="art-evid">${evid}</div>` : ""}
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
            ${composite ? `<div class="art-side-img art-compside">
              <div class="art-caplabel">consistency composite</div>
              <img class="art-refimg art-compimg" src="${bg.preview(composite)}" onerror="this.style.opacity=.15" data-prev="${bg.esc(composite)}" alt="composite">
            </div>` : ""}
          </div>
          <div class="art-actions">
            <button class="art-btn art-ok" data-act="approve" data-id="${a.id}" ${a.status === "approved" || a.status === "integrated" ? "disabled" : ""}>Approve</button>
            <button class="art-btn art-no" data-act="reject" data-id="${a.id}">Reject</button>
            <button class="art-btn" data-act="regen" data-id="${a.id}">Regenerate</button>
            <button class="art-btn" data-act="restore" data-id="${a.id}" title="Make this revision the live sheet the game uses">Restore</button>
          </div>
          <div class="art-askhost"></div>
        </div>`;
      }).join("");

      const reviewerId = this._reviewers[name];
      const usd = (this._cost.by_logical || {})[name];
      host.innerHTML = `
        <div class="art-detail-head">
          <div class="art-dtitle">${bg.esc(name)}</div>
          <span class="art-hcost" title="every image call charged to this logical asset, from the spend ledger">${usd ? this._money(usd) : "$0.00"} spent</span>
          <button class="art-btn art-primary" id="art-review-one">Run independent QA review</button>
        </div>
        <div class="art-striptools">
          <span class="art-muted">click two frames to compare · <b>j</b>/<b>k</b> move · <b>a</b> approve · <b>r</b> reject · <b>Esc</b> close</span>
          ${playable.length ? `<select class="art-sel art-small" id="art-playpick">
            ${playable.map(a => `<option value="${a.id}">r${a.revision} · ${this._fps(a)} fps</option>`).join("")}
          </select><button class="art-btn" id="art-play">▶ Loop animation</button>` : ""}
          <button class="art-btn art-primary" id="art-cmp" disabled>Compare</button>
        </div>
        <div class="art-strip">${strip}</div>
        <div id="art-qa-activity">${reviewerId ? '<div class="art-muted">QA reviewer dispatched — activity loading…</div>' : ""}</div>
        <div class="art-bar" id="art-bar" hidden>
          <div class="art-barrow">
            <b id="art-bar-n">0 selected</b>
            <button class="art-btn art-ok" data-batch="approve" id="art-bar-ok">Approve</button>
            <button class="art-btn art-no" data-batch="reject" id="art-bar-no">Reject</button>
            <span class="art-barsep"></span>
            <label class="art-barlbl">candidates
              <input type="number" class="art-num" id="art-bar-count" min="1" max="12" value="1"></label>
            <label class="art-barlbl">quality
              <select class="art-sel art-small" id="art-bar-q">
                ${this._qualities().map(q =>
                  `<option value="${bg.esc(q)}" ${q === "medium" ? "selected" : ""}>${bg.esc(q)}${
                    this._price(q) ? " · " + this._money(this._price(q)) : ""}</option>`).join("")}
              </select></label>
            <span class="art-est" id="art-bar-est" title="candidate count × selected frames × the adapter's own per-image price">~$0.00</span>
            <button class="art-btn" data-batch="regen" id="art-bar-regen">Regenerate</button>
            <span class="art-barsep"></span>
            <button class="art-btn" id="art-bar-clear">clear</button>
          </div>
          <textarea class="art-ta" id="art-bar-reason" rows="2"
            placeholder="one shared reason — applied to every selected frame (what's off-model? what should improve?)"></textarea>
        </div>
        <div class="art-cards">${cards}</div>`;

      // wire actions
      host.querySelectorAll(".art-prodimg, .art-refimg").forEach(img =>
        img.addEventListener("click", (e) => {
          e.stopPropagation();
          const src = img.dataset.prev ? bg.preview(img.dataset.prev) : img.src;
          this._openLightbox(src);
        }));
      host.querySelectorAll(".art-frame").forEach(f => {
        f.addEventListener("click", (e) => {
          if (e.target && e.target.classList.contains("art-fr-z")) return;
          this._pickCompare(Number(f.dataset.id));
        });
      });
      host.querySelectorAll(".art-fr-z").forEach(b =>
        b.addEventListener("click", (e) => {
          e.stopPropagation();
          const a = this._art(Number(b.dataset.zoom));
          if (a) this._openLightbox(bg.preview(this._revRel(a)));
        }));
      host.querySelectorAll(".art-cbx").forEach(cb =>
        cb.addEventListener("change", () => this._toggleSel(Number(cb.dataset.id), cb.checked)));
      const one = host.querySelector("#art-review-one");
      if (one) one.onclick = () => this._runReview({ logical_name: name }, name, one);
      host.querySelectorAll(".art-actions button").forEach(b =>
        b.addEventListener("click", () => this._cardAction(b.dataset.act, Number(b.dataset.id), b)));
      const cmp = host.querySelector("#art-cmp");
      if (cmp) cmp.onclick = () => this._openCompare();
      const play = host.querySelector("#art-play");
      if (play) play.onclick = () => {
        const pick = host.querySelector("#art-playpick");
        this._openAnim(Number(pick && pick.value));
      };
      host.querySelectorAll("[data-batch]").forEach(b =>
        b.addEventListener("click", () => this._batch(b.dataset.batch, b)));
      const clear = host.querySelector("#art-bar-clear");
      if (clear) clear.onclick = () => { this._sel = new Set(); this._syncSel(); };
      ["#art-bar-count", "#art-bar-q"].forEach(sel => {
        const el = host.querySelector(sel);
        if (el) el.addEventListener("input", () => this._syncSel());
      });

      this._syncSel();
      this._syncStrip();
      if (reviewerId) this._pollReviewer();
    },

    // ---- selection + compare picking (synced in place, never re-rendered) --
    _toggleSel(id, on) {
      if (!id) return;
      if (on) this._sel.add(id); else this._sel.delete(id);
      this._syncSel();
    },
    _syncSel() {
      const host = this._els && this._els.lab.querySelector("#art-detail");
      if (!host) return;
      const n = this._sel.size;
      const bar = host.querySelector("#art-bar");
      if (bar) bar.hidden = n === 0;
      const label = host.querySelector("#art-bar-n");
      if (label) label.textContent = `${n} selected`;
      const ok = host.querySelector("#art-bar-ok");
      if (ok) ok.textContent = `Approve ${n}`;
      const no = host.querySelector("#art-bar-no");
      if (no) no.textContent = `Reject ${n}`;
      const rg = host.querySelector("#art-bar-regen");
      if (rg) rg.textContent = `Regenerate ${n}`;
      const cnt = Math.max(1, Number((host.querySelector("#art-bar-count") || {}).value) || 1);
      const q = (host.querySelector("#art-bar-q") || {}).value || "medium";
      const est = host.querySelector("#art-bar-est");
      if (est) {
        est.textContent = "~" + this._money(n * cnt * this._price(q));
        est.title = `${n} frame(s) × ${cnt} candidate(s) × ${this._money(this._price(q))} per image at ${q} quality`;
      }
      host.querySelectorAll(".art-cbx").forEach(cb => {
        const on = this._sel.has(Number(cb.dataset.id));
        cb.checked = on;
        const card = cb.closest(".art-cand");
        if (card) card.classList.toggle("sel", on);
      });
    },
    _pickCompare(id) {
      if (!id) return;
      this._focusId = id;
      const i = this._cmp.indexOf(id);
      if (i !== -1) this._cmp.splice(i, 1);
      else { this._cmp.push(id); if (this._cmp.length > 2) this._cmp.shift(); }
      this._syncStrip();
    },
    _syncStrip() {
      const host = this._els && this._els.lab.querySelector("#art-detail");
      if (!host) return;
      host.querySelectorAll(".art-frame").forEach(f => {
        const id = Number(f.dataset.id);
        const i = this._cmp.indexOf(id);
        f.classList.toggle("cmp", i !== -1);
        f.classList.toggle("focus", id === this._focusId);
        const tag = f.querySelector(".art-fr-c");
        if (tag) tag.textContent = i === -1 ? "" : (i === 0 ? "A" : "B");
      });
      const btn = host.querySelector("#art-cmp");
      if (btn) {
        btn.disabled = this._cmp.length !== 2;
        const revs = this._cmp.map(id => { const a = this._art(id); return a ? "r" + a.revision : "?"; });
        btn.textContent = this._cmp.length === 2 ? `Compare ${revs[0]} ⇄ ${revs[1]}` : "Compare";
      }
    },

    // ---- inline reason prompt (replaces every prompt()) -------------------
    _ask(host, opts) {
      const bg = this._bg;
      opts = opts || {};
      return new Promise(resolve => {
        try {
          const old = host.querySelector(".art-ask");
          if (old) old.remove();
          const row = document.createElement("div");
          row.className = "art-ask";
          row.innerHTML = `
            <div class="art-asklabel">${bg.esc(opts.label || "reason")}</div>
            ${opts.confirmOnly ? "" : `<textarea class="art-ta" rows="2"
              placeholder="${bg.esc(opts.placeholder || "")}">${bg.esc(opts.value || "")}</textarea>`}
            <div class="art-askbtns">
              <button class="art-btn art-primary" data-a="ok">${bg.esc(opts.ok || "confirm")}</button>
              <button class="art-btn" data-a="no">cancel</button>
              <span class="art-muted">Ctrl+Enter confirms · Esc cancels</span>
            </div>`;
          host.appendChild(row);
          const ta = row.querySelector("textarea");
          const done = (v) => { try { row.remove(); } catch (e) {} resolve(v); };
          row.querySelector('[data-a="ok"]').onclick = () => done(ta ? ta.value : "");
          row.querySelector('[data-a="no"]').onclick = () => done(null);
          if (ta) {
            ta.focus();
            ta.setSelectionRange(ta.value.length, ta.value.length);
            ta.addEventListener("keydown", e => {
              e.stopPropagation();
              if (e.key === "Escape") { e.preventDefault(); done(null); }
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); done(ta.value); }
            });
          }
          row.scrollIntoView({ block: "nearest" });
        } catch (e) { resolve(null); }
      });
    },
    _askHost(id) {
      const host = this._els && this._els.lab.querySelector(`.art-cand[data-card="${id}"] .art-askhost`);
      return host || (this._els && this._els.lab.querySelector("#art-detail"));
    },

    async _cardAction(act, id, btn) {
      const bg = this._bg;
      if (!id) return;
      try {
        if (act === "approve") {
          await this._approve([id], "");
        } else if (act === "reject") {
          const note = await this._ask(this._askHost(id), {
            label: "Reject — what's off-model?", ok: "reject",
            placeholder: "e.g. head is a size larger than the reference; line weight thickened",
          });
          if (note == null) return;
          await this._reject([id], note);
        } else if (act === "regen") {
          const reason = await this._ask(this._askHost(id), {
            label: "Regenerate — what should improve?", ok: "queue regenerate",
            value: "produce a stronger candidate",
          });
          if (reason == null) return;
          await this._regen([id], reason, 1);
        } else if (act === "restore") {
          const go = await this._ask(this._askHost(id), {
            label: "Restore this revision as the live sheet the game uses?",
            ok: "restore", confirmOnly: true,
          });
          if (go == null) return;
          btn.disabled = true;
          const r = await bg.post(`/api/artifacts/${id}/restore`, {});
          const err = this._err(r);
          if (err) { bg.toast(err, true); btn.disabled = false; }
          else { bg.toast("restored r" + (r.restored_revision ?? "") + " as the live sheet"); this._reload(); }
        }
      } catch (e) { bg.toast("action failed", true); console.error("[art] action", e); }
    },


    /* ---- the trained style ------------------------------------------------
     * The art seat's own rule is that a style reference and an identity
     * reference cannot share a weight — at equal strength the style ref
     * transfers the SUBJECT and the whole cast comes back as one person. This
     * panel is how a project stops paying that: train the look from the anchors
     * a human already approved, and the reference slot is free for identity.
     *
     * The toggle is the loud part on purpose. A LoRA's drift is baked into the
     * model rather than visible in a payload, so "which look is this project
     * generating with right now" has to be answerable at a glance.
     */
    _renderStyle() {
      const bg = this._bg, host = this._els && this._els.style;
      const badge = this._els && this._els.stmode;
      if (!host) return;
      try {
        const d = this._style;

        // DO NOT REBUILD OVER SOMEBODY'S TYPING. This seat refreshes every ~3
        // seconds and this method replaces the card's whole innerHTML, so a
        // name being typed into the field was destroyed mid-keystroke — the
        // field emptied itself every three seconds and the style could not be
        // named at all. Same rule the graph already follows for a half-typed
        // steer: a poll may repaint what the SERVER owns, never what the
        // person is in the middle of.
        //
        // Two guards, because either alone is not enough: identical payload =>
        // nothing to repaint at all, and a focused field => keep what is in it
        // (with the caret) across the repaint that a real change forces.
        const sig = JSON.stringify([d && d.mode, d && d.source,
                                    d && d.active && d.active.style_id,
                                    d && d.running,
                                    d && d.dataset && d.dataset.usable_names]);
        const held = host.querySelector("#art-stname");
        const typing = held && document.activeElement === held;
        // The signature alone is not enough: switching seats REBUILDS the
        // container, so the card comes back empty while the payload is
        // unchanged — and a skip there leaves "loading…" on screen forever.
        // Compare the host element too; a new one always paints.
        const sameHost = this._styleHost === host;
        this._styleHost = host;
        if (sameHost && sig === this._styleSig && !this._styleForce) return;
        const carry = held ? { value: held.value, start: held.selectionStart,
                               end: held.selectionEnd, focused: typing } : null;
        this._styleSig = sig;
        this._styleForce = false;
        if (!d) {
          host.innerHTML = '<div class="art-empty">style training needs a newer '
            + 'dashboard — restart <code>bgate serve</code>.</div>';
          if (badge) badge.textContent = "";
          return;
        }
        const ds = d.dataset || {};
        const active = d.active || null;
        const run = d.running || {};
        const lora = d.mode === "lora";
        const anchors = ds.anchors || [];
        const rejected = ds.rejected || [];

        // The header badge answers "what is this project generating with" from
        // across the room, which is the question a baked-in style makes hard.
        if (badge) {
          badge.textContent = lora && active
            ? `using ${active.name || active.style_id}`
            : "using references";
          badge.className = "art-sth" + (lora && active ? " on" : "");
        }

        // Which anchors will be enlarged to reach the floor, so a 1.09x resize
        // is visible on the tile rather than buried in a warning line.
        const up = {};
        (ds.upscaled || []).forEach(u => { up[String(u.path)] = u; });
        const thumbs = (list, cls) => list.map(a => `
          <figure class="art-stthumb ${cls}" title="${bg.esc(a.name || "")}${
            a.why ? " — " + bg.esc(a.why) : ""}">
            ${a.rel
              ? `<img src="/api/preview?rel=${encodeURIComponent(a.rel)}" alt="" loading="lazy">`
              : `<div class="art-stmissing"></div>`}
            <figcaption>${bg.esc(a.name || "")}</figcaption>
            ${up[String(a.path || "")]
              ? `<div class="art-stup">↑${up[String(a.path)].scale}x</div>` : ""}
          </figure>`).join("");

        const trainedRow = active
          ? `<div class="art-stactive">
               <div class="art-stactname">${bg.esc(active.name || active.style_id)}</div>
               <div class="art-stdim">${bg.esc(active.style_id)} · ${
                 Number(active.images || 0)} anchors · strength ${
                 Number(active.strength ?? 0.85)} · trained ${
                 bg.esc(String(active.trained_at || "").slice(0, 16))}</div>
               ${(active.sources || []).length
                 ? `<div class="art-stdim">from ${(active.sources || []).slice(0, 10)
                      .map(n => bg.esc(n)).join(", ")}${
                      (active.sources || []).length > 10 ? " …" : ""}</div>`
                 : ""}
             </div>`
          : "";

        const busy = run.status === "running";
        const cost = busy
          ? `<span class="art-stnote">training “${bg.esc(run.name || "")}” from ${
               Number(run.images || 0)} anchors — 5 to 15 minutes.</span>`
          : run.status === "failed"
            ? `<span class="art-stwarn">last run failed: ${bg.esc(run.error || "")}</span>`
            : !ds.ok
              ? `<span class="art-stwarn">${bg.esc(ds.reason || "not enough usable anchors")}</span>`
              : `<span class="art-stnote">5-15 minutes, and it costs money. Krea
                 publishes no training price, so this is NOT bounded by the spend
                 ceiling the way a generation is.</span>`;

        host.innerHTML = `
          <div class="art-stgrid">
            <div class="art-stleft">
              <div class="art-strow">
                <span class="art-stlabel">generate with</span>
                <span class="art-stseg">
                  <button class="art-stopt${lora ? "" : " on"}" data-stmode="refs"
                          title="Send the pinned anchors as style references — how it has always worked">references</button>
                  <button class="art-stopt${lora ? " on" : ""}" data-stmode="lora"
                          title="Use the style trained from those anchors, freeing the reference slot for identity"
                          ${active ? "" : "disabled"}>trained style</button>
                </span>
              </div>
              <p class="art-stwhy">A style reference and an identity reference
                cannot share a weight — at equal strength the style ref transfers
                the <b>subject</b>, and the cast comes back as one person.
                Training the look into a model frees that slot for identity.</p>
              ${trainedRow || `<div class="art-stnote">Nothing trained yet, so
                generations use the references. Training does not change that
                until you switch the toggle.</div>`}
              <div class="art-strow">
                <input class="art-stname" id="art-stname" placeholder="name this style"
                       maxlength="60"${busy ? " disabled" : ""}>
                <button class="qbtn small" id="art-sttrain"${
                  busy || !ds.ok ? " disabled" : ""}>train</button>
              </div>
              ${cost}
              ${(ds.warnings || []).map(w =>
                  `<div class="art-stwarn">${bg.esc(w)}</div>`).join("")}
            </div>
            <div class="art-stright">
              <div class="art-stsec">the dataset · ${anchors.length} of ${
                Number(ds.candidates || 0)} will train
                <span class="art-stseg art-stsrc">
                  ${(d.sources || ["pins", "assets", "both"]).map(src => `
                    <button class="art-stopt${d.source === src ? " on" : ""}"
                            data-stsrc="${src}"
                            title="${src === "pins"
                              ? "The anchors a human approved through ref_pin"
                              : src === "assets"
                                ? "The game's own shipped art under game/assets"
                                : "Both shelves, de-duplicated"}">${src}</button>`).join("")}
                </span>
              </div>
              <div class="art-stfilm">${
                thumbs(anchors, "ok") || '<div class="art-empty">no pinned anchor clears 1024px on its short side.</div>'}</div>
              ${rejected.length
                ? `<details class="art-stdrop">
                     <summary>${rejected.length} pinned anchor${
                       rejected.length === 1 ? "" : "s"} cannot — mostly too small</summary>
                     <div class="art-stfilm dim">${thumbs(rejected, "no")}</div>
                   </details>`
                : ""}
            </div>
          </div>`;

        // Put the half-typed name back, caret included.
        const fresh = host.querySelector("#art-stname");
        if (fresh && carry && carry.value) {
          fresh.value = carry.value;
          if (carry.focused) {
            fresh.focus();
            try { fresh.setSelectionRange(carry.start, carry.end); } catch (e) {}
          }
        }

        host.querySelectorAll("[data-stmode]").forEach(b => b.onclick = async () => {
          if (b.disabled) return;
          const r = await window.mutate("/api/settings",
            { method: "PATCH", body: { "art.style_source": b.dataset.stmode },
              button: b, ok: b.dataset.stmode === "lora"
                ? "generating with the trained style" : "generating with references" });
          if (r.ok) this._reload();
        });
        host.querySelectorAll("[data-stsrc]").forEach(b => b.onclick = async () => {
          const r = await window.mutate("/api/settings",
            { method: "PATCH", body: { "art.style_dataset": b.dataset.stsrc },
              button: b, ok: `dataset: ${b.dataset.stsrc}` });
          if (r.ok) this._reload();
        });
        const train = host.querySelector("#art-sttrain");
        if (train) train.onclick = () => this._trainStyle();
      } catch (e) {
        host.innerHTML = '<div class="art-empty">style panel error</div>';
        console.error("[art] style", e);
      }
    },

    async _trainStyle() {
      const host = this._els && this._els.style;
      const input = host && host.querySelector("#art-stname");
      const name = String((input && input.value) || "").trim();
      if (!name) { this._bg.toast("name the style first", true); return; }
      const ds = (this._style || {}).dataset || {};
      const yes = await window.askConfirm({
        title: `Train “${name}” from ${(ds.usable_names || []).length} anchors?`,
        body: "This uploads those anchors to Krea and trains a LoRA. It takes 5 "
            + "to 15 minutes and it costs money — Krea publishes no price for "
            + "training, so it is NOT bounded by the spend ceiling the way a "
            + "generation is.\n\nIt does not change how anything generates "
            + "until you switch the toggle above.",
        ok: "train it", cancel: "not now",
      });
      if (!yes) return;
      const r = await window.mutate("/api/art/style/train",
        { body: { name }, button: host.querySelector("#art-sttrain"),
          ok: "training started — this panel updates when it lands" });
      if (r.ok) this._reload();
    },

    _reload() { this._detailSig = ""; this._styleForce = true; this._loadAll(false); },

    // ---- the three review verbs, batch-shaped ---------------------------
    /* Verdicts go through /react, never /review. React fans one decision three
     * ways — the disposition, a durable art-seat preference note the NEXT agent
     * reads in its seat brief, and a live steer to the agent still working the
     * item. /review writes a column nobody reads, so a rejection through it
     * teaches nothing. The endpoint reports a refused disposition in
     * `review_error` (it keeps the note and the steer either way), so that has
     * to be surfaced as well as the envelope error. */
    async _react(ids, verdict, note) {
      const bg = this._bg;
      const item = bg.activeItem || null;
      let done = 0, firstErr = null;
      for (const id of ids) {
        const body = { verdict, note: note || "" };
        if (item) body.item_id = item;
        const r = await bg.post(`/api/artifacts/${id}/react`, body);
        // Approval is human-only server-side; a 403 comes back as a real
        // sentence and must be shown, not swallowed into a blank panel.
        const err = this._err(r) || (r && r.review_error) || null;
        if (err) { if (!firstErr) firstErr = err; } else done++;
      }
      return { done, firstErr };
    },
    async _approve(ids, note) {
      const bg = this._bg;
      const { done, firstErr } = await this._react(ids, "like", note);
      if (firstErr) bg.toast(done ? `${done} approved · ${firstErr}` : firstErr, true);
      else bg.toast(done === 1 ? "approved — installed as the live sheet" : `approved ${done}`);
      this._sel = new Set();
      this._reload();
    },
    async _reject(ids, note) {
      const bg = this._bg;
      const { done, firstErr } = await this._react(ids, "dislike", note);
      if (firstErr) bg.toast(done ? `${done} rejected · ${firstErr}` : firstErr, true);
      else bg.toast(done === 1 ? "rejected — the art seat keeps the reason" : `rejected ${done}`);
      this._sel = new Set();
      this._reload();
    },
    async _regen(ids, reason, count) {
      const bg = this._bg;
      const n = Math.max(1, Number(count) || 1);
      const brief = n > 1
        ? `${reason || "produce a stronger candidate"} — produce ${n} fresh candidates to choose between.`
        : (reason || "produce a stronger candidate");
      let done = 0, firstErr = null;
      for (const id of ids) {
        const r = await bg.post(`/api/artifacts/${id}/regenerate`, { reason: brief });
        const err = this._err(r);
        if (err) { if (!firstErr) firstErr = err; } else done++;
      }
      if (firstErr) bg.toast(done ? `${done} queued · ${firstErr}` : firstErr, true);
      else bg.toast(`regeneration queued for ${done} frame${done === 1 ? "" : "s"}`);
      this._sel = new Set();
      this._reload();
    },

    async _batch(act, btn) {
      const bg = this._bg;
      const ids = [...this._sel];
      if (!ids.length) return;
      const host = this._els.lab.querySelector("#art-detail");
      const ta = host && host.querySelector("#art-bar-reason");
      const reason = (ta && ta.value || "").trim();
      if ((act === "reject" || act === "regen") && !reason) {
        bg.toast("a shared reason is required — say what's wrong once, it applies to all " + ids.length, true);
        if (ta) ta.focus();
        return;
      }
      try {
        if (btn) btn.disabled = true;
        if (act === "approve") await this._approve(ids, reason);
        else if (act === "reject") await this._reject(ids, reason);
        else if (act === "regen") {
          const cnt = Number((host.querySelector("#art-bar-count") || {}).value) || 1;
          await this._regen(ids, reason, cnt);
        }
      } catch (e) { bg.toast("batch action failed", true); console.error("[art] batch", e); }
      finally { if (btn) btn.disabled = false; }
    },

    // ---- keyboard triage -------------------------------------------------
    _typing() {
      const el = document.activeElement;
      if (!el) return false;
      const tag = (el.tagName || "").toUpperCase();
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
    },
    _active() {
      const view = document.getElementById("view-seats");
      if (!view || view.hidden) return false;
      if (window.SeatShell && window.SeatShell.current !== "art") return false;
      return !!(this._els && this._els.lab && this._els.lab.isConnected);
    },
    _key(e) {
      if (!this._active()) return;
      if (e.key === "Escape") {
        // Escape works even from a field: it is the universal "get me out".
        if (this._els.lightbox && !this._els.lightbox.hidden) { this._closeLightbox(); e.preventDefault(); }
        else if (this._sel && this._sel.size) { this._sel = new Set(); this._syncSel(); e.preventDefault(); }
        else if (this._cmp && this._cmp.length) { this._cmp = []; this._syncStrip(); e.preventDefault(); }
        return;
      }
      if (this._typing() || e.ctrlKey || e.metaKey || e.altKey) return;
      const strip = this._strip || [];
      if (!strip.length) return;
      const at = Math.max(0, strip.indexOf(this._focusId));
      if (e.key === "j" || e.key === "J") {
        this._focusId = strip[Math.min(strip.length - 1, at + 1)];
        this._syncStrip(); this._scrollToFocus(); e.preventDefault();
      } else if (e.key === "k" || e.key === "K") {
        this._focusId = strip[Math.max(0, at - 1)];
        this._syncStrip(); this._scrollToFocus(); e.preventDefault();
      } else if (e.key === "a" || e.key === "A") {
        e.preventDefault();
        const ids = this._sel.size ? [...this._sel] : (this._focusId ? [this._focusId] : []);
        if (ids.length) this._approve(ids, "");
      } else if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        const ids = this._sel.size ? [...this._sel] : (this._focusId ? [this._focusId] : []);
        if (!ids.length) return;
        const host = this._sel.size
          ? this._els.lab.querySelector("#art-detail")
          : this._askHost(ids[0]);
        this._ask(host, {
          label: `Reject ${ids.length} frame${ids.length === 1 ? "" : "s"} — one shared reason`,
          ok: "reject", placeholder: "what's off-model?",
        }).then(note => { if (note != null) this._reject(ids, note); });
      } else if (e.key === " " && this._focusId) {
        e.preventDefault();
        this._toggleSel(this._focusId, !this._sel.has(this._focusId));
      }
    },
    _scrollToFocus() {
      try {
        const el = this._els.lab.querySelector(`.art-frame[data-id="${this._focusId}"]`);
        if (el) el.scrollIntoView({ block: "nearest", inline: "nearest" });
      } catch (e) {}
    },

    async _runReview(body, key, btn) {
      const bg = this._bg;
      try {
        if (btn) { btn.disabled = true; btn.textContent = "dispatching…"; }
        const r = await bg.post("/api/art-qa/review", body);
        const err = this._err(r);
        if (err) {
          bg.toast(err, true);
        } else {
          const d = this._data(r) || r;
          bg.toast(`QA reviewer dispatched (${d.candidate_count || 0} candidates)`);
          if (d.review_item_id) {
            if (key === "__all__") this._reviewers["__all__"] = d.review_item_id;
            else this._reviewers[key] = d.review_item_id;
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
        const raw = await bg.get(`/api/agent-activity/${id}`).catch(() => null);
        const act = this._data(raw);
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

    // --- lightbox: single / compare / animation ---------------------------
    _openLightbox(src) {
      const lb = this._els.lightbox;
      if (!lb) return;
      this._stopAnim();
      lb.innerHTML = `<img src="${src}" alt=""><div class="art-lbx">✕ click anywhere to close</div>`;
      lb.hidden = false;
    },
    _closeLightbox() {
      const lb = this._els.lightbox;
      this._stopAnim();
      if (lb) { lb.hidden = true; lb.innerHTML = ""; }
    },
    _stopAnim() {
      if (this._animTimer) { clearInterval(this._animTimer); this._animTimer = null; }
    },

    /* Visual diff. Two frames stacked in the same box: an opacity slider for a
     * dissolve and a `mix-blend-mode: difference` toggle that turns every pixel
     * that MOVED black-on-not-black. Plus the palette delta, computed the same
     * way the server's histogram tripwire computes it, so the number in the UI
     * and the number in the ledger mean the same thing. */
    _openCompare() {
      const bg = this._bg, lb = this._els.lightbox;
      if (!lb || this._cmp.length !== 2) return;
      const [a, b] = this._cmp.map(id => this._art(id));
      if (!a || !b) { bg.toast("those revisions are no longer loaded", true); return; }
      this._stopAnim();
      const srcA = bg.preview(this._revRel(a)), srcB = bg.preview(this._revRel(b));
      lb.innerHTML = `
        <div class="art-lbwrap" id="art-lbwrap">
          <div class="art-lbhead">
            <b>r${a.revision}</b> <span class="art-muted">${bg.esc(a.status)}</span>
            <span class="art-vs">⇄</span>
            <b>r${b.revision}</b> <span class="art-muted">${bg.esc(b.status)}</span>
            <span class="art-lbdelta" id="art-delta">palette …</span>
          </div>
          <div class="art-cmpstage" id="art-cmpstage">
            <img id="art-cmpa" src="${srcA}" alt="A">
            <img id="art-cmpb" src="${srcB}" alt="B" style="opacity:.5">
          </div>
          <div class="art-lbctl">
            <label class="art-barlbl">overlay r${b.revision}
              <input type="range" id="art-op" min="0" max="100" value="50"></label>
            <label class="art-barlbl"><input type="checkbox" id="art-diff"> difference blend</label>
            <button class="art-btn" id="art-swap">swap A/B</button>
            <button class="art-btn" id="art-lbclose">✕ close</button>
          </div>
        </div>`;
      lb.hidden = false;
      const wrap = lb.querySelector("#art-lbwrap");
      if (wrap) wrap.addEventListener("click", e => e.stopPropagation());
      const imgA = lb.querySelector("#art-cmpa"), imgB = lb.querySelector("#art-cmpb");
      const op = lb.querySelector("#art-op"), diff = lb.querySelector("#art-diff");
      const stage = lb.querySelector("#art-cmpstage");
      op.addEventListener("input", () => { imgB.style.opacity = String(Number(op.value) / 100); });
      diff.addEventListener("change", () => {
        stage.classList.toggle("diff", diff.checked);
        if (diff.checked) { imgB.style.opacity = "1"; op.value = "100"; }
      });
      lb.querySelector("#art-swap").onclick = () => {
        this._cmp.reverse(); this._syncStrip(); this._openCompare();
      };
      lb.querySelector("#art-lbclose").onclick = () => this._closeLightbox();
      this._paletteDelta(imgA, imgB, lb.querySelector("#art-delta"));
    },

    /* Histogram intersection over opaque pixels, 4 bits per channel — the same
     * shape as the server's _palette_hist/_hist_intersect. 1.0 = identical
     * palettes; the documented tripwire is <0.55. Same-origin images, so the
     * canvas is never tainted; any failure reports itself rather than lying. */
    _paletteDelta(imgA, imgB, out) {
      if (!out) return;
      const bg = this._bg;
      const hist = (img) => {
        const c = document.createElement("canvas");
        const n = 128;
        c.width = n; c.height = n;
        const ctx = c.getContext("2d", { willReadFrequently: true });
        ctx.clearRect(0, 0, n, n);
        ctx.drawImage(img, 0, 0, n, n);
        const px = ctx.getImageData(0, 0, n, n).data;
        const h = new Float64Array(4096);
        let total = 0;
        for (let i = 0; i < px.length; i += 4) {
          if (px[i + 3] <= 96) continue;
          h[((px[i] >> 4) << 8) | ((px[i + 1] >> 4) << 4) | (px[i + 2] >> 4)] += 1;
          total++;
        }
        if (!total) return null;
        for (let i = 0; i < h.length; i++) h[i] /= total;
        return h;
      };
      const run = () => {
        try {
          const ha = hist(imgA), hb = hist(imgB);
          if (!ha || !hb) { out.textContent = "palette n/a (no opaque pixels)"; return; }
          let inter = 0;
          for (let i = 0; i < ha.length; i++) inter += Math.min(ha[i], hb[i]);
          const v = Math.max(0, Math.min(1, inter));
          const bad = v < T.SIMILARITY;
          out.textContent = `palette match ${v.toFixed(3)} · Δ ${(1 - v).toFixed(3)}`;
          out.style.color = bad ? "var(--bad)" : "var(--good)";
          out.title = `histogram intersection of opaque pixels (1.00 = identical palettes). ` +
            `Documented tripwire: <${T.SIMILARITY} reads as colour drift.`;
        } catch (e) {
          out.textContent = "palette delta unavailable";
          out.style.color = "var(--ash)";
          out.title = String(e && e.message || e);
        }
      };
      let pending = 0;
      [imgA, imgB].forEach(img => {
        if (img.complete && img.naturalWidth) return;
        pending++;
        img.addEventListener("load", () => { if (--pending <= 0) run(); }, { once: true });
        img.addEventListener("error", () => {
          out.textContent = "palette delta unavailable (image failed to load)";
        }, { once: true });
      });
      if (!pending) run();
    },

    /* Looping SpriteFrames-style playback at the .tres fps, from the per-pose
     * frame files the sprite legs already record in metadata.frames. */
    _openAnim(artifactId) {
      const bg = this._bg, lb = this._els.lightbox;
      const a = this._art(artifactId);
      if (!lb || !a) return;
      const anims = this._anims(a);
      const names = Object.keys(anims).sort();
      if (!names.length) { bg.toast("this revision has no per-frame files to play", true); return; }
      const fps = this._fps(a);
      this._stopAnim();
      lb.innerHTML = `
        <div class="art-lbwrap" id="art-lbwrap">
          <div class="art-lbhead">
            <b>r${a.revision}</b> <span class="art-muted">looping at ${fps} fps (from the .tres)</span>
            <span class="art-lbdelta" id="art-animlbl"></span>
          </div>
          <div class="art-animstage"><img id="art-animimg" alt=""></div>
          <div class="art-lbctl">
            ${names.map(n => `<button class="art-btn art-animpick" data-anim="${bg.esc(n)}">${bg.esc(n)} · ${anims[n].length}f</button>`).join("")}
            <button class="art-btn" id="art-animpause">⏸ pause</button>
            <button class="art-btn" id="art-lbclose">✕ close</button>
          </div>
        </div>`;
      lb.hidden = false;
      const wrap = lb.querySelector("#art-lbwrap");
      if (wrap) wrap.addEventListener("click", e => e.stopPropagation());
      const img = lb.querySelector("#art-animimg");
      const lbl = lb.querySelector("#art-animlbl");
      const pause = lb.querySelector("#art-animpause");
      lb.querySelector("#art-lbclose").onclick = () => this._closeLightbox();

      let current = names[0], i = 0, playing = true;
      const paint = () => {
        const fr = anims[current];
        if (!fr || !fr.length) return;
        i = i % fr.length;
        img.src = bg.preview(fr[i].rel);
        if (lbl) lbl.textContent = `${current} — frame ${i + 1}/${fr.length}`;
      };
      const tick = () => { if (!playing) return; i++; paint(); };
      const start = () => {
        this._stopAnim();
        this._animTimer = setInterval(tick, Math.max(40, 1000 / fps));
      };
      lb.querySelectorAll(".art-animpick").forEach(b => b.addEventListener("click", e => {
        e.stopPropagation();
        current = b.dataset.anim; i = 0; playing = true;
        if (pause) pause.textContent = "⏸ pause";
        paint(); start();
      }));
      if (pause) pause.onclick = () => {
        playing = !playing;
        pause.textContent = playing ? "⏸ pause" : "▶ play";
      };
      // preload so the first loop isn't a slideshow of blanks
      names.forEach(n => anims[n].forEach(f => { const p = new Image(); p.src = bg.preview(f.rel); }));
      paint();
      start();
    },
  };

  const STYLE = `<style>
    .art-root{color:var(--bone);font-size:13px}
    .art-card{background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-lg);padding:var(--s-6);margin-bottom:var(--s-6)}
    .art-h{display:flex;align-items:center;gap:var(--s-4);margin:0 0 var(--s-5);font-size:var(--fs-2xs);font-weight:var(--fw-bold);text-transform:uppercase;letter-spacing:var(--track-label);color:var(--text-3)}
    .art-empty{color:var(--text-3);font-size:var(--fs-sm);padding:var(--s-5) var(--s-1);line-height:var(--lh)}

    /* Trained style. Full width above the lab, because it decides what EVERY
       image this project generates looks like — and a LoRA's drift is baked
       into the model rather than visible in a payload, so "which look is on"
       has to be answerable from across the room. */
    .art-stcard{border-color:var(--accent-line)}
    .art-sth{margin-left:auto;font-family:var(--mono);font-size:var(--fs-3xs);
      font-weight:400;color:var(--text-3);text-transform:none;letter-spacing:0}
    .art-sth.on{color:var(--accent)}
    .art-stgrid{display:grid;grid-template-columns:minmax(280px,1fr) minmax(0,1.15fr);
      gap:var(--s-6)}
    .art-strow{display:flex;align-items:center;gap:var(--s-4);margin:var(--s-4) 0}
    .art-stlabel{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);
      text-transform:uppercase;letter-spacing:var(--track-label);white-space:nowrap}
    .art-stseg{display:inline-flex;border:1px solid var(--seam);border-radius:var(--r-full);overflow:hidden}
    .art-stopt{padding:5px 13px;border:0;background:transparent;color:var(--text-3);
      font:inherit;font-family:var(--mono);font-size:var(--fs-3xs);cursor:pointer}
    .art-stopt + .art-stopt{border-left:1px solid var(--seam)}
    .art-stopt:hover:not(:disabled){color:var(--bone);background:var(--plate2)}
    .art-stopt.on{background:var(--accent-soft);color:var(--accent);font-weight:var(--fw-semi)}
    .art-stopt:disabled{opacity:.45;cursor:not-allowed}
    .art-stwhy{font-size:12px;color:var(--text-2);line-height:1.55;margin:var(--s-4) 0}
    .art-stwhy b{color:var(--bone)}
    .art-stactive{padding:var(--s-4);border-radius:var(--r-sm);
      background:var(--accent-soft);border:1px solid var(--accent-line)}
    .art-stactname{font-size:13px;font-weight:var(--fw-semi);color:var(--bone)}
    .art-stdim{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);margin-top:2px}
    .art-stnote{display:block;font-size:11.5px;color:var(--text-3);line-height:1.5;margin-top:var(--s-3)}
    .art-stwarn{display:block;font-size:11.5px;color:var(--warn);line-height:1.5;margin-top:var(--s-3)}
    .art-stsec{display:flex;align-items:center;gap:var(--s-4);flex-wrap:wrap;
      font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);
      text-transform:uppercase;letter-spacing:var(--track-label);margin-bottom:var(--s-3)}
    .art-stsrc{margin-left:auto}
    /* The anchors themselves. "6 of 26" is the least useful way to describe a
       dataset of pictures; the pictures are the description. */
    .art-stfilm{display:flex;flex-wrap:wrap;gap:var(--s-3)}
    .art-stfilm.dim{opacity:.55}
    .art-stthumb{margin:0;width:88px}
    .art-stthumb img,.art-stmissing{width:88px;height:66px;object-fit:cover;
      border-radius:var(--r-sm);border:1px solid var(--seam);background:var(--plate2);
      display:block;image-rendering:pixelated}
    .art-stthumb.ok img{border-color:var(--accent-line)}
    .art-stthumb.no img{filter:grayscale(1)}
    .art-stup{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--accent)}
    .art-stthumb figcaption{font-family:var(--mono);font-size:var(--fs-3xs);
      color:var(--text-3);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .art-stdrop{margin-top:var(--s-5)}
    .art-stdrop summary{cursor:pointer;font-family:var(--mono);font-size:var(--fs-3xs);
      color:var(--text-3)}
    .art-stdrop summary:hover{color:var(--bone)}
    .art-stdrop .art-stfilm{margin-top:var(--s-3)}
    .art-stname{flex:1;min-width:0;padding:6px 10px;background:var(--plate2);
      border:1px solid var(--seam);border-radius:var(--r-sm);color:var(--bone);
      font:inherit;font-size:12px}
    .art-stname:focus{outline:none;border-color:var(--accent-line)}
    @media(max-width:1100px){.art-stgrid{grid-template-columns:1fr}}
    .art-muted{color:var(--ash);font-size:11px}
    .art-btn{padding:6px 11px;background:var(--plate2);border:1px solid var(--seam);border-radius:8px;color:var(--bone);font:inherit;font-size:12px;cursor:pointer}
    .art-btn:hover{border-color:var(--ember)}
    .art-btn:disabled{opacity:.45;cursor:default}
    .art-primary{background:var(--plate2);border-color:var(--ember);color:var(--ember)}
    .art-ok{color:var(--good);border-color:var(--good)}
    .art-no{color:var(--bad);border-color:var(--bad)}
    .art-ta{width:100%;box-sizing:border-box;padding:7px 9px;background:var(--plate2);border:1px solid var(--seam);border-radius:8px;color:var(--bone);font:inherit;font-size:12px;resize:vertical}
    .art-ta:focus{outline:none;border-color:var(--ember)}
    .art-num{width:56px;padding:5px 7px;background:var(--plate2);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:12px}
    /* picker */
    .art-pick{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:var(--plate);border:1px solid var(--seam);border-radius:12px;padding:12px 14px;margin-bottom:14px}
    .art-pl{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--ash)}
    .art-sel{padding:6px 9px;background:var(--plate2);border:1px solid var(--seam);border-radius:8px;color:var(--bone);font:inherit;font-size:12px;max-width:420px}
    .art-sel.art-small{padding:4px 7px;font-size:11px}
    .art-chk{font-size:12px;color:var(--ash);display:flex;align-items:center;gap:6px;cursor:pointer}
    .art-spend{font-size:11px;color:var(--ember);border:1px solid var(--seam);border-radius:6px;padding:3px 8px;font-variant-numeric:tabular-nums}
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
    .art-listwrap{width:210px;flex:0 0 210px;display:flex;flex-direction:column;gap:7px}
    .art-lfilter{width:100%;padding:var(--s-3) var(--s-4);background:var(--bg);border:1px solid var(--line);
      border-radius:var(--r-sm);color:var(--text);font:inherit;font-size:var(--fs-sm);outline:none}
    .art-lfilter:focus{border-color:var(--accent)}
    .art-lnone{color:var(--text-3);font-size:var(--fs-sm);padding:var(--s-4) var(--s-1)}
    .art-lnone[hidden]{display:none}
    .art-lrow[hidden]{display:none}
    .art-list{width:100%;max-height:520px;overflow:auto;display:flex;flex-direction:column;gap:3px}
    .art-lrow{display:flex;align-items:center;gap:7px;padding:6px 8px;background:transparent;border:1px solid transparent;border-radius:7px;color:var(--bone);cursor:pointer;text-align:left;font:inherit;font-size:12px;width:100%}
    .art-lrow:hover{background:var(--plate2)}
    .art-lrow.sel{background:var(--plate2);border-color:var(--seam2)}
    .art-dot{width:8px;height:8px;border-radius:50%;flex:0 0 8px}
    .art-lname{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .art-lcount{color:var(--ash);font-size:10px}
    .art-ljit{color:var(--warn);font-size:10px}
    .art-ldrift{color:var(--bad);font-size:10px}
    .art-llock{color:var(--ash2);font-size:10px}
    /* locks panel */
    .art-lock{border:1px solid var(--seam);border-radius:8px;padding:7px 9px;margin-bottom:7px;background:var(--plate2)}
    .art-lock.mine{border-color:var(--ember)}
    .art-lockrow{display:flex;align-items:center;gap:7px}
    .art-lockseat{font-size:10px;text-transform:uppercase;letter-spacing:.05em}
    .art-lockpath{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px}
    .art-lockitem{padding:2px 7px;font-size:10px}
    .art-lockmeta{font-size:10px;color:var(--ash);margin-top:3px;line-height:1.4}
    .art-lockwait{font-size:11px;color:var(--warn);margin-top:4px}
    .art-lusd{color:var(--ash);font-size:10px;font-variant-numeric:tabular-nums}
    .art-detail{flex:1;min-width:0}
    @media(max-width:720px){.art-lab{flex-direction:column}.art-listwrap{width:100%;flex:none}.art-list{flex-direction:row;flex-wrap:wrap}}
    .art-detail-head{display:flex;align-items:center;gap:12px;margin-bottom:10px}
    .art-dtitle{font-size:15px;font-weight:var(--fw-semi);color:var(--bone);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .art-hcost{font-size:12px;color:var(--ember);border:1px solid var(--seam);border-radius:6px;padding:3px 9px;font-variant-numeric:tabular-nums;white-space:nowrap}
    /* filmstrip */
    .art-striptools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:7px}
    .art-striptools .art-muted{flex:1;min-width:180px}
    .art-strip{display:flex;gap:8px;overflow-x:auto;padding-bottom:8px;margin-bottom:12px;border-bottom:1px solid var(--seam)}
    .art-frame{position:relative;flex:0 0 auto;width:78px;height:78px;border:1px solid var(--seam);border-radius:8px;overflow:hidden;background:var(--void);cursor:pointer}
    .art-frame:hover{border-color:var(--ash2)}
    .art-frame.focus{border-color:var(--bone);box-shadow:0 0 0 1px var(--bone) inset}
    .art-frame.cmp{border-color:var(--ember);box-shadow:0 0 0 2px var(--ember) inset}
    .art-frame img{width:100%;height:100%;object-fit:contain;pointer-events:none}
    .art-fr-r{position:absolute;bottom:2px;right:3px;font-size:9px;background:rgba(0,0,0,.65);color:var(--bone);padding:1px 4px;border-radius:4px}
    .art-fr-c{position:absolute;bottom:2px;left:3px;font-size:9px;color:var(--ember);font-weight:var(--fw-semi)}
    .art-fr-z{position:absolute;top:2px;right:2px;width:17px;height:17px;padding:0;border:0;border-radius:4px;background:rgba(0,0,0,.6);color:var(--bone);font-size:10px;line-height:1;cursor:zoom-in}
    /* candidate cards */
    .art-cards{display:flex;flex-direction:column;gap:12px}
    .art-cand{background:var(--plate);border:1px solid var(--seam);border-radius:10px;padding:12px}
    .art-cand.sel{border-color:var(--ember)}
    .art-cand-top{display:flex;align-items:center;gap:8px;margin-bottom:10px}
    .art-cbxw{display:flex;align-items:center;cursor:pointer}
    .art-rev{font-weight:var(--fw-semi);color:var(--bone)}
    .art-model{color:var(--ash);font-size:11px;margin-left:auto}
    .art-badge{font-size:10px;text-transform:uppercase;letter-spacing:.04em;border:1px solid;border-radius:5px;padding:1px 6px}
    .art-qab{cursor:help}
    .art-cons,.art-cost{cursor:help;text-transform:none;letter-spacing:0}
    .art-evid{display:flex;gap:6px;flex-wrap:wrap;margin:-4px 0 10px}
    .art-compare{display:flex;align-items:stretch;gap:10px;flex-wrap:wrap}
    .art-side-img{flex:1;min-width:0}
    .art-side-img.art-refs{flex:1}
    .art-side-img.art-compside{flex:0 0 auto}
    .art-caplabel{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--ash);margin-bottom:4px}
    .art-prodimg{width:100%;max-height:260px;object-fit:contain;background:var(--void);border:1px solid var(--seam);border-radius:8px;cursor:zoom-in;display:block}
    .art-vs{align-self:center;color:var(--ash2);font-size:11px;font-style:italic}
    .art-refrow{display:flex;gap:6px;flex-wrap:wrap}
    .art-refimg{width:96px;height:120px;object-fit:contain;background:var(--void);border:1px solid var(--ember);border-radius:8px;cursor:zoom-in}
    .art-compimg{width:150px;border-color:var(--seam2)}
    .art-noref{color:var(--ash);font-size:11px;border:1px dashed var(--seam);border-radius:8px;padding:12px;text-align:center}
    .art-actions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
    /* inline ask (replaces prompt/confirm) */
    .art-ask{margin-top:10px;padding:10px;border:1px solid var(--ember);border-radius:9px;background:var(--plate2)}
    .art-asklabel{font-size:11px;color:var(--ember);margin-bottom:6px}
    .art-askbtns{display:flex;gap:8px;align-items:center;margin-top:7px;flex-wrap:wrap}
    /* batch bar */
    .art-bar{position:sticky;top:0;z-index:5;background:var(--plate2);border:1px solid var(--ember);border-radius:10px;padding:10px;margin-bottom:12px}
    .art-bar[hidden]{display:none}
    .art-barrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
    .art-barlbl{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--ash)}
    .art-barsep{width:1px;height:18px;background:var(--seam)}
    .art-est{font-size:12px;color:var(--ember);font-variant-numeric:tabular-nums;cursor:help}
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
    .art-lightbox>img{max-width:92vw;max-height:86vh;object-fit:contain;border-radius:8px;box-shadow:0 10px 60px rgba(0,0,0,.6)}
    .art-lbx{margin-top:14px;color:var(--ash);font-size:12px}
    .art-lbwrap{cursor:default;display:flex;flex-direction:column;gap:10px;align-items:stretch;max-width:94vw}
    .art-lbhead{display:flex;align-items:center;gap:10px;color:var(--bone);font-size:13px;flex-wrap:wrap}
    .art-lbdelta{margin-left:auto;font-size:12px;font-variant-numeric:tabular-nums;cursor:help}
    .art-lbctl{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:var(--plate);border:1px solid var(--seam);border-radius:10px;padding:10px 12px}
    .art-cmpstage,.art-animstage{position:relative;width:min(88vw,900px);height:min(72vh,700px);background:var(--void);border:1px solid var(--seam);border-radius:10px;overflow:hidden}
    .art-cmpstage img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
    .art-cmpstage.diff{background:var(--bg)}
    .art-cmpstage.diff img{mix-blend-mode:difference}
    .art-cmpstage.diff img:first-child{mix-blend-mode:normal}
    .art-animstage img{width:100%;height:100%;object-fit:contain;image-rendering:pixelated}
  </style>`;

  window.SeatWS.art = A;
})();
