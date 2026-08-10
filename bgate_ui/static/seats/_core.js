/* Seat-workspace core: shared helpers + the shell dispatcher + a reusable
 * reference manager. Loaded before every seat module.
 *
 * MODULE CONTRACT — each static/seats/<seat>.js does:
 *   window.SeatWS = window.SeatWS || {};
 *   window.SeatWS.art = {
 *     label: "Art",
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
    // A stored ref/artifact path is absolute; /api/preview refuses anything
    // that is not root-relative. Anchoring on .bgate is what the task scope
    // already did — this just makes it the one rule everywhere.
    relRef(path) {
      if (!path) return "";
      const cut = String(path).replace(/^.*[\\/](?=\.bgate[\\/])/, "");
      return cut.replace(/\\/g, "/");
    },
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
    // Real geometry, one grid, one stroke weight — see icons.js. These used to
    // be seven unrelated Unicode glyphs (◆ ¶ ⌖ ⚙ ▲ ♪ ✓) resolved by whatever
    // symbol font the OS had, so no two sat on the same baseline.
    glyphs: new Proxy({}, {
      get: (_t, seat) => (window.BGIcon && BGIcon.has(seat))
        ? BGIcon(seat, { size: 15 }) : "",
    }),
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
    // The in-page replacements for prompt()/confirm() — ask.js, loaded above
    // this file. Mirrored here so seat code keeps its bg.* idiom instead of
    // growing a second copy of the pattern per seat.
    askText(o) { return window.askText(o); },
    askConfirm(o) { return window.askConfirm(o); },
    askPick(o) { return window.askPick(o); },
    // Timestamps out of SQLite are `datetime('now')` — UTC, no zone marker.
    // Parsing them as local time is how "3 minutes ago" became "5 hours ago".
    stampMs(when) {
      const text = String(when || "");
      if (!text) return 0;
      const ms = Date.parse(text.replace(" ", "T")
        + (/[zZ]|[+-]\d\d:?\d\d$/.test(text) ? "" : "Z"));
      return Number.isFinite(ms) ? ms : 0;
    },
    ago(when) {
      const ms = BGWS.stampMs(when);
      if (!ms) return "";
      const s = Math.max(0, (Date.now() - ms) / 1000);
      if (s < 45) return "just now";
      if (s < 5400) return `${Math.round(s / 60)}m ago`;
      if (s < 172800) return `${Math.round(s / 3600)}h ago`;
      return `${Math.round(s / 86400)}d ago`;
    },
    bytes(n) {
      const v = Number(n) || 0;
      if (v < 1024) return `${v} B`;
      if (v < 1048576) return `${(v / 1024).toFixed(0)} KB`;
      return `${(v / 1048576).toFixed(1)} MB`;
    },
    async copy(text) {
      const s = String(text == null ? "" : text);
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(s);
          return true;
        }
      } catch (e) { /* fall through to the textarea route */ }
      try {
        const ta = document.createElement("textarea");
        ta.value = s;
        ta.style.cssText = "position:fixed;left:-9999px;top:0";
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand("copy");
        ta.remove();
        return ok;
      } catch (e) { return false; }
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

  /* ---- SeatWork: the shared work + logs panel -----------------------------
   * ONE component, rendered above every seat's workspace by the shell, so no
   * seat can opt out or grow its own copy. It answers the only question the
   * old four-cell strip could not: SHOW ME EVERYTHING THIS SEAT HAS DONE.
   *
   * Every work item this seat owns is here, grouped by what state it is in —
   * in progress / queued / completed / failed. Nothing is hidden: the finished
   * groups start collapsed behind their count because they are historical, not
   * because they are secret, and one click opens them.
   *
   * The "blocked on" cell this replaces listed items whose status was `failed`
   * under a heading that promised a reason and then printed a title. Failed is
   * not blocked, a title is not a reason, and the actual reason (the item's
   * result, and the transcript that produced it) was two views away. It is now
   * one click away, in SeatLog.
   */
  const WORK_GROUPS = [
    { key: "active", label: "in progress", tone: "good", open: true,
      match: st => st === "dispatched" },
    { key: "review", label: "waiting on you", tone: "warn", open: true,
      match: st => st === "review" },
    { key: "queued", label: "queued", tone: "warn", open: true,
      match: st => st === "queued" },
    { key: "done", label: "completed", tone: "", open: false,
      match: st => st === "done" },
    { key: "failed", label: "failed", tone: "bad", open: false,
      match: st => st === "failed" || st === "cancelled" },
  ];
  const WORK_PAGE = 40;   // rows drawn per group before "show all"

  const SeatWork = {
    _seat: null,
    _sig: "",
    _items: [],
    _live: {},
    _filter: "",
    _detail: {},          // item id -> brief/result expanded inline
    _all: {},             // group key -> ignore the WORK_PAGE cap
    _open: (() => {
      try { return JSON.parse(localStorage.getItem("bgws-work-open") || "{}") || {}; }
      catch (e) { return {}; }
    })(),

    _isOpen(key) {
      const g = WORK_GROUPS.find(x => x.key === key);
      const stored = this._open[key];
      return stored == null ? !!(g && g.open) : !!stored;
    },
    _setOpen(key, on) {
      this._open[key] = !!on;
      try { localStorage.setItem("bgws-work-open", JSON.stringify(this._open)); } catch (e) {}
    },

    async paint(host, seat) {
      if (!host) return;
      if (seat !== this._seat) {
        this._seat = seat; this._sig = ""; this._filter = ""; this._detail = {}; this._all = {};
      }
      let items = [], agents = [];
      try {
        const [q, a] = await Promise.all([
          BGWS.get("/api/queue").catch(() => ({ items: [] })),
          BGWS.get("/api/agents").catch(() => ({ agents: [] })),
        ]);
        items = (q.items || []).filter(it => it && it.seat === seat);
        agents = a.agents || [];
      } catch (e) { /* falls through to the offline state below */ }
      // A repaint can resolve after the user switched seats — never stomp it.
      if (this._seat !== seat || !host.isConnected) return;

      this._live = {};
      agents.forEach(ag => { if (ag && ag.state === "running") this._live[ag.item_id] = ag; });
      items.sort((x, y) => String(y.updated_at || "").localeCompare(String(x.updated_at || "")));
      this._items = items;

      // Only rebuild when the data actually moved. This repaints every 3s and
      // the panel holds open groups, an inline detail and a filter box.
      const sig = seat + "|" + items.map(it =>
        `${it.id}:${it.status}:${it.updated_at}`).join(",") + "|" + Object.keys(this._live).join(",");
      if (sig === this._sig && host.firstChild) { this._stamp(host); return; }
      this._sig = sig;
      this._draw(host);
    },

    // Cheap tick: only the relative timestamps move.
    _stamp(host) {
      host.querySelectorAll("[data-ago]").forEach(el => {
        const t = BGWS.ago(el.getAttribute("data-ago"));
        if (t && el.textContent !== t) el.textContent = t;
      });
    },

    _draw(host) {
      const focused = document.activeElement;
      const keepFocus = focused && focused.classList
        && focused.classList.contains("swk-find") && host.contains(focused);
      host.innerHTML = this._style() + this._body();
      if (!host._swkWired) {
        host._swkWired = true;
        host.addEventListener("click", e => { try { this._click(e); } catch (err) {} });
        host.addEventListener("input", e => {
          if (!e.target.classList.contains("swk-find")) return;
          this._filter = e.target.value;
          const rows = host.querySelector("#swk-groups");
          if (rows) rows.innerHTML = this._groupsHtml();
        });
      }
      if (keepFocus) {
        const box = host.querySelector(".swk-find");
        if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
      }
    },

    _click(e) {
      const head = e.target.closest(".swk-gh");
      if (head) {
        const key = head.getAttribute("data-g");
        this._setOpen(key, !this._isOpen(key));
        this._sig = ""; this.refresh();
        return;
      }
      const more = e.target.closest(".swk-more");
      if (more) {
        this._all[more.getAttribute("data-g")] = true;
        this._sig = ""; this.refresh();
        return;
      }
      const logs = e.target.closest(".swk-logs");
      if (logs) {
        e.stopPropagation();
        const it = this._items.find(x => String(x.id) === logs.getAttribute("data-id"));
        if (it) SeatLog.open(it);
        return;
      }
      const focus = e.target.closest(".swk-focus");
      if (focus) {
        e.stopPropagation();
        const id = Number(focus.getAttribute("data-id"));
        BGWS.setActiveItem(id);
        BGWS.toast("focused #" + id);
        return;
      }
      const row = e.target.closest(".swk-row");
      if (row) {
        const id = row.getAttribute("data-id");
        this._detail[id] = !this._detail[id];
        this._sig = ""; this.refresh();
      }
    },

    _body() {
      const items = this._items;
      const running = items.filter(it => this._live[it.id]).length;
      const dispatched = items.filter(it => it.status === "dispatched").length;
      let state = "idle", tone = "off";
      if (running) { state = `working · ${running} live`; tone = "good"; }
      else if (dispatched) { state = "dispatched"; tone = "good"; }
      const counts = WORK_GROUPS.map(g => {
        const n = items.filter(it => g.match(it.status)).length;
        return n ? `<span class="swk-chip t-${g.tone || "off"}">${n} ${BGWS.esc(g.label)}</span>` : "";
      }).join("");
      return `
        <div class="swk">
          <div class="swk-top">
            <span class="swk-state t-${tone}"><span class="swk-dot"></span>${BGWS.esc(state)}</span>
            ${counts || '<span class="swk-none">no work on this seat yet</span>'}
            <span class="swk-sp"></span>
            <input class="swk-find" placeholder="filter this seat's work…"
                   value="${BGWS.esc(this._filter)}">
          </div>
          <div class="swk-groups" id="swk-groups">${this._groupsHtml()}</div>
        </div>`;
    },

    _groupsHtml() {
      const q = this._filter.trim().toLowerCase();
      const hit = it => !q || String(it.id).includes(q)
        || String(it.title || "").toLowerCase().includes(q)
        || String(it.status || "").toLowerCase().includes(q);
      const out = WORK_GROUPS.map(g => {
        const all = this._items.filter(it => g.match(it.status));
        if (!all.length) return "";
        const rows = all.filter(hit);
        const open = this._isOpen(g.key) || (!!q && !!rows.length);
        const capped = this._all[g.key] ? rows : rows.slice(0, WORK_PAGE);
        return `<section class="swk-g${open ? " open" : ""}">
          <button class="swk-gh" data-g="${g.key}">
            <span class="swk-caret"></span>
            <span class="swk-glabel t-${g.tone || "off"}">${BGWS.esc(g.label)}</span>
            <span class="swk-gn">${rows.length}${rows.length !== all.length ? ` of ${all.length}` : ""}</span>
          </button>
          ${open ? `<div class="swk-rows">
            ${capped.map(it => this._row(it)).join("")
              || '<div class="swk-none swk-pad">nothing matches that filter</div>'}
            ${capped.length < rows.length
              ? `<button class="swk-more" data-g="${g.key}">show the other ${rows.length - capped.length}</button>` : ""}
          </div>` : ""}
        </section>`;
      }).join("");
      return out || '<div class="swk-none swk-pad">Nothing has been queued to this seat. Work filed here shows up with its full transcript.</div>';
    },

    _row(it) {
      const live = this._live[it.id];
      const open = !!this._detail[it.id];
      const st = String(it.status || "");
      const tail = live && live.last_output_s != null
        ? `output ${Math.round(live.last_output_s)}s ago` : "";
      const body = (it.result || it.brief || "").trim();
      return `<div class="swk-item${open ? " open" : ""}">
        <div class="swk-row" data-id="${it.id}">
          <span class="swk-caret sm"></span>
          <span class="swk-st s-${BGWS.esc(st)}${live ? " livedot" : ""}">${BGWS.esc(st)}</span>
          <button class="swk-focus" data-id="${it.id}" title="focus this item across seats">#${it.id}</button>
          <span class="swk-title">${BGWS.esc(it.title || "untitled")}</span>
          ${tail ? `<span class="swk-tail">${BGWS.esc(tail)}</span>` : ""}
          ${it.priority ? `<span class="swk-tag">p${BGWS.esc(it.priority)}</span>` : ""}
          ${it.source && it.source !== "manual" ? `<span class="swk-tag">${BGWS.esc(it.source)}</span>` : ""}
          <span class="swk-when" data-ago="${BGWS.esc(it.updated_at || "")}">${BGWS.esc(BGWS.ago(it.updated_at))}</span>
          <button class="swk-logs" data-id="${it.id}">open log</button>
        </div>
        ${open ? `<div class="swk-detail">
          ${it.brief ? `<div class="swk-dh">brief</div><div class="swk-dt">${BGWS.esc(String(it.brief).slice(0, 4000))}</div>` : ""}
          ${it.result ? `<div class="swk-dh">result</div><div class="swk-dt">${BGWS.esc(String(it.result).slice(0, 4000))}</div>` : ""}
          ${!body ? '<div class="swk-none">no brief or result recorded — the transcript is still there</div>' : ""}
        </div>` : ""}
      </div>`;
    },

    _style() {
      return `<style>
        .swk{background:var(--surface-1);border:1px solid var(--line);border-radius:12px;
          margin-bottom:12px;font-size:12px;color:var(--text);overflow:hidden}
        .swk-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:9px 12px;
          border-bottom:1px solid var(--line);background:var(--surface-2)}
        .swk-sp{flex:1}
        .swk-state{display:inline-flex;align-items:center;gap:6px;font-weight:var(--fw-semi)}
        .swk-dot{width:8px;height:8px;border-radius:50%;background:var(--text-3);display:inline-block}
        .swk-state.t-good .swk-dot{background:var(--good)}
        .swk-chip{padding:2px 8px;border-radius:20px;border:1px solid var(--line);
          color:var(--text-2);font-size:11px;font-variant-numeric:tabular-nums}
        .swk-chip.t-good{color:var(--good);border-color:var(--good)}
        .swk-chip.t-warn{color:var(--warn);border-color:var(--warn)}
        .swk-chip.t-bad{color:var(--bad);border-color:var(--bad)}
        .swk-find{padding:5px 9px;min-width:180px;background:var(--surface-1);border:1px solid var(--line);
          border-radius:7px;color:var(--text);font:inherit;font-size:12px}
        .swk-find:focus{outline:none;border-color:var(--accent)}
        .swk-none{color:var(--text-3)}
        .swk-pad{padding:10px 12px;line-height:1.5}
        .swk-g{border-bottom:1px solid var(--line)}
        .swk-g:last-child{border-bottom:0}
        .swk-gh{display:flex;align-items:center;gap:8px;width:100%;padding:7px 12px;background:none;
          border:0;color:var(--text-2);font:inherit;font-size:11px;text-transform:uppercase;
          letter-spacing:.06em;cursor:pointer;text-align:left}
        .swk-gh:hover{background:var(--surface-2)}
        .swk-glabel.t-good{color:var(--good)}
        .swk-glabel.t-warn{color:var(--warn)}
        .swk-glabel.t-bad{color:var(--bad)}
        .swk-gn{color:var(--text-3);font-variant-numeric:tabular-nums;letter-spacing:0}
        .swk-caret{width:0;height:0;flex:none;border-left:5px solid currentColor;
          border-top:4px solid transparent;border-bottom:4px solid transparent;
          opacity:.6;transition:transform .12s ease}
        .swk-caret.sm{border-left-width:4px;border-top-width:3px;border-bottom-width:3px;opacity:.4}
        .swk-g.open>.swk-gh .swk-caret{transform:rotate(90deg)}
        .swk-item.open .swk-caret.sm{transform:rotate(90deg)}
        .swk-rows{padding:0 0 6px}
        .swk-row{display:flex;align-items:center;gap:8px;padding:4px 12px 4px 20px;cursor:pointer;
          line-height:1.6}
        .swk-row:hover{background:var(--surface-2)}
        .swk-st{font-size:10px;padding:1px 7px;border-radius:12px;border:1px solid var(--line);
          color:var(--text-3);flex:none;text-transform:uppercase;letter-spacing:.04em}
        .swk-st.s-dispatched,.swk-st.livedot{color:var(--good);border-color:var(--good)}
        .swk-st.s-queued,.swk-st.s-review{color:var(--warn);border-color:var(--warn)}
        .swk-st.s-failed,.swk-st.s-cancelled{color:var(--bad);border-color:var(--bad)}
        .swk-focus{background:none;border:0;padding:0;color:var(--text-3);font:inherit;font-size:11px;
          font-variant-numeric:tabular-nums;cursor:pointer;flex:none}
        .swk-focus:hover{color:var(--accent)}
        .swk-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .swk-tail{color:var(--good);font-size:11px;flex:none}
        .swk-tag{color:var(--text-3);font-size:10px;border:1px solid var(--line);border-radius:5px;
          padding:0 5px;flex:none}
        .swk-when{color:var(--text-3);font-size:11px;flex:none;min-width:62px;text-align:right}
        .swk-logs{flex:none;padding:2px 9px;background:var(--surface-2);border:1px solid var(--line);
          border-radius:6px;color:var(--text-2);font:inherit;font-size:11px;cursor:pointer}
        .swk-logs:hover{border-color:var(--accent);color:var(--text)}
        .swk-more{margin:4px 12px 2px 20px;padding:3px 10px;background:none;border:1px dashed var(--line);
          border-radius:6px;color:var(--text-3);font:inherit;font-size:11px;cursor:pointer}
        .swk-more:hover{border-color:var(--accent);color:var(--text-2)}
        .swk-detail{margin:2px 12px 8px 20px;padding:8px 11px;background:var(--surface-2);
          border:1px solid var(--line);border-radius:8px}
        .swk-dh{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3);margin:2px 0 3px}
        .swk-dt{white-space:pre-wrap;line-height:1.55;color:var(--text-2);max-height:220px;overflow:auto;
          margin-bottom:6px}
      </style>`;
    },

    refresh() {
      const host = document.getElementById("seat-strip");
      if (host && this._seat) this.paint(host, this._seat);
    },
  };
  window.SeatWork = SeatWork;

  /* ---- SeatLog: the transcript reader -------------------------------------
   * The review surface. An agent log is a stream-json file that runs to tens of
   * thousands of lines across several re-dispatches, and the only way to read
   * one used to be a 60-line tail in a side panel.
   *
   * Three decisions make this usable:
   *
   *   PARSED, NOT DUMPED. Default view folds the stream into what a person
   *   actually reviews — what the agent said, which tool it called on what,
   *   what came back, what it finally reported. Raw json is one toggle away for
   *   when the parse is the thing in question.
   *
   *   WINDOWED. Every line is measured into a fixed-height row and only the
   *   rows on screen are in the DOM. Long text is wrapped at flatten time
   *   rather than by CSS, so a row's height is known without measuring it and
   *   a 40k-line transcript scrolls at the same speed as a 40-line one.
   *   innerHTML of the whole log is what locks the page, and this never does it.
   *
   *   RUNS ARE VISIBLE. The log APPENDS across re-dispatches. all_runs=true
   *   asks the server for the history with its boundary markers, and the reader
   *   can jump between them instead of scrolling through a dead run.
   */
  const SeatLog = {
    LH: 21,          // fixed row height, must match .slg-l in the stylesheet
    WRAP: 150,       // wrap width in characters, applied when flattening
    TAIL: 40000,     // lines requested; the server tails from the end
    _st: null,

    async open(item) {
      const it = item || {};
      this.close();
      const back = document.createElement("div");
      back.className = "slg-back";
      back.id = "bgws-log";
      back.innerHTML = this._style() + this._chrome(it);
      document.body.appendChild(back);
      this._st = {
        item: it, back, allRuns: false, mode: "readable",
        raw: [], rows: [], runs: [], q: "", matches: [], mi: 0, loading: true,
      };
      back.addEventListener("click", e => {
        if (e.target === back) this.close();
      });
      back.querySelector(".slg-close").addEventListener("click", () => this.close());
      back.querySelector(".slg-runs").addEventListener("click", () => {
        this._st.allRuns = !this._st.allRuns; this._load();
      });
      back.querySelector(".slg-mode").addEventListener("click", () => {
        this._st.mode = this._st.mode === "readable" ? "raw" : "readable";
        this._rebuild();
      });
      back.querySelector(".slg-copy").addEventListener("click", async () => {
        const ok = await BGWS.copy(this._st.raw.join("\n"));
        BGWS.toast(ok ? "log copied" : "could not copy", !ok);
      });
      back.querySelector(".slg-reload").addEventListener("click", () => this._load());
      const find = back.querySelector(".slg-find");
      find.addEventListener("input", () => { this._st.q = find.value; this._search(); });
      find.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); this._jump(e.shiftKey ? -1 : 1); }
      });
      back.querySelector(".slg-prev").addEventListener("click", () => this._jump(-1));
      back.querySelector(".slg-next").addEventListener("click", () => this._jump(1));
      back.querySelector(".slg-runprev").addEventListener("click", () => this._jumpRun(-1));
      back.querySelector(".slg-runnext").addEventListener("click", () => this._jumpRun(1));
      const vp = back.querySelector(".slg-vp");
      vp.addEventListener("scroll", () => {
        if (this._st._raf) return;
        this._st._raf = requestAnimationFrame(() => { this._st._raf = 0; this._rows(); });
      });
      // Re-wrap when the pane changes size — otherwise a widened window keeps
      // reading at the old column and a narrowed one clips.
      this._onResize = () => {
        clearTimeout(this._rz);
        this._rz = setTimeout(() => {
          if (!this._st) return;
          const before = this._wrapW;
          this._measure(this._st);
          if (Math.abs((this._wrapW || 0) - (before || 0)) > 4) this._rebuild();
        }, 220);
      };
      window.addEventListener("resize", this._onResize);
      this._onKey = e => {
        if (e.key === "Escape") { this.close(); return; }
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
          e.preventDefault(); find.focus(); find.select();
        }
      };
      document.addEventListener("keydown", this._onKey, true);
      await this._load();
      find.focus();
    },

    close() {
      if (this._onKey) { document.removeEventListener("keydown", this._onKey, true); this._onKey = null; }
      if (this._onResize) { window.removeEventListener("resize", this._onResize); this._onResize = null; }
      clearTimeout(this._rz);
      const old = document.getElementById("bgws-log");
      if (old) old.remove();
      this._st = null;
    },

    async _load() {
      const S = this._st;
      if (!S) return;
      S.loading = true;
      this._status("reading the transcript…");
      let data = { lines: [], runs: 0, run: 0 };
      try {
        data = await BGWS.get(`/api/agent-log/${S.item.id}?tail=${this.TAIL}&all_runs=${S.allRuns}`);
      } catch (e) { /* renders as "no transcript" below */ }
      if (this._st !== S || !S.back.isConnected) return;
      S.raw = data.lines || [];
      S.runCount = data.runs || 0;
      S.loading = false;
      const btn = S.back.querySelector(".slg-runs");
      btn.classList.toggle("on", S.allRuns);
      btn.textContent = S.allRuns ? `all ${S.runCount || 1} runs` : "latest run only";
      this._rebuild();
    },

    // Wrap to what the column can actually SHOW. Rows are a fixed height so the
    // window arithmetic stays exact, which means a line longer than the column
    // is clipped rather than reflowed — wrapping to a constant 150 characters
    // clipped every one of them on a narrow pane. Measured from the real
    // monospace metrics once, then reused.
    _measure(S) {
      const body = S.back.querySelector(".slg-body");
      if (!body) return;
      // A real row, hidden: the line-number and gutter columns, the gaps and
      // the padding all take their true widths, so no constant here can be
      // wrong when the layout changes under it.
      const probe = document.createElement("div");
      probe.className = "slg-l";
      probe.style.visibility = "hidden";
      probe.innerHTML = '<span class="slg-n">0</span><span class="slg-g"></span>' +
        '<span class="slg-t">' + "0".repeat(100) + "</span>";
      body.appendChild(probe);
      const cell = probe.querySelector(".slg-t");
      const colW = cell.getBoundingClientRect().width;
      const ch = (cell.scrollWidth / 100) || 6.6;
      probe.remove();
      this._wrapW = Math.max(24, Math.floor(colW / ch));
    },

    _rebuild() {
      const S = this._st;
      if (!S) return;
      this._measure(S);
      S.back.querySelector(".slg-mode").textContent =
        S.mode === "readable" ? "readable" : "raw json";
      S.rows = S.mode === "readable" ? this._parse(S.raw) : this._rawRows(S.raw);
      S.runs = [];
      S.rows.forEach((r, i) => { if (r.cls === "run") S.runs.push(i); });
      const vp = S.back.querySelector(".slg-vp");
      S.back.querySelector(".slg-spacer").style.height = (S.rows.length * this.LH) + "px";
      vp.scrollTop = 0;
      this._search();
      this._rows();
      this._status(S.rows.length
        ? `${S.rows.length.toLocaleString()} lines · ${S.runCount || 1} run${(S.runCount || 1) === 1 ? "" : "s"}`
        : "no transcript on disk for this item — it may never have been dispatched");
      const nav = S.back.querySelector(".slg-runnav");
      nav.hidden = S.runs.length < 2;
    },

    _status(text) {
      const el = this._st && this._st.back.querySelector(".slg-status");
      if (el) el.textContent = text;
    },

    // ---- flattening -------------------------------------------------------
    // Every display row is exactly LH tall, which is what makes the window
    // arithmetic exact. Wrapping happens HERE, not in CSS, for the same reason.
    _wrap(text, out, cls, gutter) {
      const width = this._wrapW || this.WRAP;
      String(text == null ? "" : text).split("\n").forEach(para => {
        const line = para.replace(/\t/g, "  ").replace(/\r/g, "");
        if (!line.length) { out.push({ cls, g: gutter, t: "" }); gutter = ""; return; }
        let i = 0;
        while (i < line.length) {
          let end = Math.min(line.length, i + width);
          if (end < line.length) {
            const sp = line.lastIndexOf(" ", end);
            if (sp > i + width * 0.6) end = sp + 1;
          }
          out.push({ cls, g: gutter, t: line.slice(i, end) });
          gutter = "";
          i = end;
        }
      });
      if (gutter) out.push({ cls, g: gutter, t: "" });
    },

    _rawRows(lines) {
      const out = [];
      lines.forEach(line => {
        if (/^\s*─+\s*run /.test(line)) out.push({ cls: "run", g: "", t: line.trim() });
        else this._wrap(line, out, "raw", "");
      });
      return out;
    },

    // Mirrors dispatch._absorb: the same events, the same names, so what is
    // read here is what the activity feed and the console show.
    _parse(lines) {
      const out = [];
      const blocks = ev => {
        const m = ev && ev.message;
        const c = m && m.content;
        return Array.isArray(c) ? c : [];
      };
      lines.forEach(line => {
        const text = String(line == null ? "" : line);
        if (!text.trim()) return;
        if (/^\s*─+\s*run /.test(text)) { out.push({ cls: "run", g: "", t: text.trim() }); return; }
        let ev = null;
        try { ev = JSON.parse(text); } catch (e) { ev = null; }
        if (!ev || typeof ev !== "object") { this._wrap(text, out, "raw", ""); return; }
        const type = ev.type;
        if (type === "bgate_run_start") {
          out.push({ cls: "run", g: "", t: "───── run start ─────" });
        } else if (type === "assistant") {
          blocks(ev).forEach(b => {
            if (b.type === "text" && String(b.text || "").trim()) {
              this._wrap(String(b.text).trim(), out, "say", "agent");
            } else if (b.type === "tool_use") {
              const name = String(b.name || "?").replace("mcp__builders-gate__", "");
              const inp = (b.input && typeof b.input === "object") ? b.input : {};
              const hint = inp.path || inp.file_path || inp.command || inp.prompt
                || inp.title || inp.query || inp.role || inp.pattern || "";
              this._wrap(name + (hint ? "  " + String(hint) : ""), out, "tool", "tool");
            } else if (b.type === "thinking" && String(b.thinking || "").trim()) {
              this._wrap(String(b.thinking).trim(), out, "think", "thinking");
            }
          });
        } else if (type === "user") {
          blocks(ev).forEach(b => {
            if (b.type === "tool_result") {
              const c = b.content;
              const txt = typeof c === "string" ? c
                : (Array.isArray(c) && c[0] && typeof c[0] === "object" ? (c[0].text || "") : "");
              if (String(txt).trim()) this._wrap(String(txt).trim(), out, "result", "→");
            } else if (b.type === "text" && String(b.text || "").includes("DIRECTOR STEER")) {
              this._wrap(String(b.text).split("DIRECTOR STEER").pop().trim(), out, "steer", "steer");
            }
          });
        } else if (type === "result") {
          const meta = [];
          if (ev.subtype) meta.push(String(ev.subtype));
          if (ev.num_turns != null) meta.push(ev.num_turns + " turns");
          if (ev.total_cost_usd != null) meta.push("$" + Number(ev.total_cost_usd).toFixed(4));
          out.push({ cls: "run", g: "", t: "───── result · " + meta.join(" · ") + " ─────" });
          this._wrap(String(ev.result || ""), out, "final", "result");
        } else if (type === "system") {
          // Session bookkeeping. Kept — it is where a killed background task or
          // a permission denial shows up — but visually demoted.
          this._wrap(String(ev.subtype || "system"), out, "sys", "sys");
        } else if (type && /^(thread|turn|item)\./.test(type)) {
          const item = (ev.item && typeof ev.item === "object") ? ev.item : {};
          const label = item.text || item.command || item.tool || item.name || type;
          this._wrap(String(label), out, item.type === "agent_message" ? "say" : "tool",
            item.type === "agent_message" ? "agent" : "tool");
        }
      });
      return out;
    },

    // ---- search -----------------------------------------------------------
    _search() {
      const S = this._st;
      if (!S) return;
      const q = S.q.trim().toLowerCase();
      S.matches = [];
      if (q.length >= 2) {
        S.rows.forEach((r, i) => { if (r.t.toLowerCase().includes(q)) S.matches.push(i); });
      }
      S.mi = 0;
      const c = S.back.querySelector(".slg-count");
      c.textContent = q.length < 2 ? "" : (S.matches.length ? `1 / ${S.matches.length}` : "no matches");
      this._rows();
      if (S.matches.length) this._scrollTo(S.matches[0]);
    },

    _jump(dir) {
      const S = this._st;
      if (!S || !S.matches.length) return;
      S.mi = (S.mi + dir + S.matches.length) % S.matches.length;
      S.back.querySelector(".slg-count").textContent = `${S.mi + 1} / ${S.matches.length}`;
      this._scrollTo(S.matches[S.mi]);
    },

    _jumpRun(dir) {
      const S = this._st;
      if (!S || !S.runs.length) return;
      const vp = S.back.querySelector(".slg-vp");
      const here = Math.round(vp.scrollTop / this.LH);
      let target = dir > 0
        ? S.runs.find(i => i > here + 1)
        : [...S.runs].reverse().find(i => i < here - 1);
      if (target == null) target = dir > 0 ? S.runs[S.runs.length - 1] : S.runs[0];
      this._scrollTo(target, true);
    },

    _scrollTo(index, top) {
      const S = this._st;
      if (!S) return;
      const vp = S.back.querySelector(".slg-vp");
      vp.scrollTop = Math.max(0, index * this.LH - (top ? 8 : vp.clientHeight / 2));
      this._rows();
    },

    // ---- windowed render --------------------------------------------------
    _rows() {
      const S = this._st;
      if (!S) return;
      const vp = S.back.querySelector(".slg-vp");
      const body = S.back.querySelector(".slg-body");
      const n = S.rows.length;
      const start = Math.max(0, Math.floor(vp.scrollTop / this.LH) - 12);
      const end = Math.min(n, Math.ceil((vp.scrollTop + vp.clientHeight) / this.LH) + 12);
      const q = S.q.trim();
      const cur = S.matches.length ? S.matches[S.mi] : -1;
      const out = [];
      for (let i = start; i < end; i++) {
        const r = S.rows[i];
        out.push(`<div class="slg-l k-${r.cls}${i === cur ? " cur" : ""}">` +
          `<span class="slg-n">${i + 1}</span>` +
          `<span class="slg-g">${BGWS.esc(r.g)}</span>` +
          `<span class="slg-t">${this._hl(r.t, q)}</span></div>`);
      }
      body.style.transform = `translateY(${start * this.LH}px)`;
      body.innerHTML = out.join("") ||
        (S.loading ? "" : `<div class="slg-l k-sys"><span class="slg-n"></span><span class="slg-g"></span><span class="slg-t">nothing to show</span></div>`);
    },

    _hl(text, q) {
      if (!q || q.length < 2) return BGWS.esc(text);
      const lower = text.toLowerCase(), needle = q.toLowerCase();
      let out = "", i = 0;
      for (;;) {
        const at = lower.indexOf(needle, i);
        if (at < 0) { out += BGWS.esc(text.slice(i)); break; }
        out += BGWS.esc(text.slice(i, at)) +
          `<mark class="slg-m">${BGWS.esc(text.slice(at, at + q.length))}</mark>`;
        i = at + q.length;
      }
      return out;
    },

    _chrome(it) {
      const st = String(it.status || "");
      return `<div class="slg-sheet">
        <div class="slg-bar">
          <span class="slg-st s-${BGWS.esc(st)}">${BGWS.esc(st)}</span>
          <span class="slg-id">#${BGWS.esc(it.id)}</span>
          <span class="slg-title">${BGWS.esc(it.title || "work item")}</span>
          <span class="slg-sp"></span>
          <button class="slg-btn slg-close">close</button>
        </div>
        <div class="slg-tools">
          <input class="slg-find" placeholder="search the transcript…">
          <span class="slg-count"></span>
          <button class="slg-btn slg-prev" title="previous match (Shift+Enter)">prev</button>
          <button class="slg-btn slg-next" title="next match (Enter)">next</button>
          <span class="slg-runnav" hidden>
            <span class="slg-sep"></span>
            <span class="slg-lbl">run</span>
            <button class="slg-btn slg-runprev">back</button>
            <button class="slg-btn slg-runnext">forward</button>
          </span>
          <span class="slg-sp"></span>
          <button class="slg-btn slg-runs">latest run only</button>
          <button class="slg-btn slg-mode">readable</button>
          <button class="slg-btn slg-reload">reload</button>
          <button class="slg-btn slg-copy">copy</button>
        </div>
        <div class="slg-main">
          <div class="slg-vp"><div class="slg-spacer"></div><div class="slg-body"></div></div>
          <aside class="slg-side">
            <div class="slg-sh">brief</div>
            <div class="slg-sb">${it.brief ? BGWS.esc(it.brief) : '<span class="slg-dim">no brief recorded</span>'}</div>
            <div class="slg-sh">result</div>
            <div class="slg-sb">${it.result ? BGWS.esc(it.result) : '<span class="slg-dim">nothing reported back</span>'}</div>
            <div class="slg-sh">item</div>
            <div class="slg-sb slg-dim">${BGWS.esc(it.seat || "")} · ${BGWS.esc(it.status || "")}
              ${it.priority ? " · p" + BGWS.esc(it.priority) : ""}
              ${it.source ? " · " + BGWS.esc(it.source) : ""}
              ${it.updated_at ? " · " + BGWS.esc(BGWS.ago(it.updated_at)) : ""}</div>
          </aside>
        </div>
        <div class="slg-foot"><span class="slg-status"></span></div>
      </div>`;
    },

    _style() {
      return `<style>
        .slg-back{position:fixed;inset:0;z-index:1500;background:rgba(0,0,0,.62);
          display:flex;align-items:center;justify-content:center;padding:28px}
        /* The surface tokens are translucent by design (the theme is glass over
           black). A full-screen reader cannot be: the page behind it would show
           through the transcript. Opaque base, theme tint painted on top of it,
           so it still belongs to the theme without being see-through. */
        .slg-sheet{display:flex;flex-direction:column;width:min(1480px,100%);height:100%;
          background-color:var(--bg);
          background-image:linear-gradient(var(--surface-1),var(--surface-1));
          border:1px solid var(--line);border-radius:14px;overflow:hidden;
          color:var(--text);font-size:12px}
        .slg-bar,.slg-tools{display:flex;align-items:center;gap:8px;padding:9px 13px;
          border-bottom:1px solid var(--line);background:var(--surface-2);flex:none;flex-wrap:wrap}
        .slg-sp{flex:1}
        .slg-title{font-weight:var(--fw-semi);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .slg-id{color:var(--text-3);font-variant-numeric:tabular-nums}
        .slg-st{font-size:10px;padding:1px 8px;border-radius:12px;border:1px solid var(--line);
          color:var(--text-3);text-transform:uppercase;letter-spacing:.04em}
        .slg-st.s-done{color:var(--good);border-color:var(--good)}
        .slg-st.s-dispatched{color:var(--good);border-color:var(--good)}
        .slg-st.s-queued,.slg-st.s-review{color:var(--warn);border-color:var(--warn)}
        .slg-st.s-failed,.slg-st.s-cancelled{color:var(--bad);border-color:var(--bad)}
        .slg-btn{padding:4px 10px;background:var(--surface-3);border:1px solid var(--line);border-radius:7px;
          color:var(--text-2);font:inherit;font-size:11px;cursor:pointer}
        .slg-btn:hover{border-color:var(--accent);color:var(--text)}
        .slg-btn.on{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}
        .slg-find{flex:0 1 320px;padding:5px 10px;background:var(--surface-3);border:1px solid var(--line);
          border-radius:7px;color:var(--text);font:inherit;font-size:12px}
        .slg-find:focus{outline:none;border-color:var(--accent)}
        .slg-count{color:var(--text-3);font-variant-numeric:tabular-nums;min-width:64px}
        .slg-lbl{color:var(--text-3);font-size:11px}
        .slg-sep{width:1px;height:16px;background:var(--line);display:inline-block}
        .slg-main{flex:1;display:flex;min-height:0}
        .slg-vp{flex:1;position:relative;overflow:auto;min-width:0}
        .slg-spacer{width:1px}
        .slg-body{position:absolute;top:0;left:0;right:0;will-change:transform}
        .slg-l{height:21px;line-height:21px;display:flex;gap:10px;padding:0 12px;
          font-family:var(--mono,ui-monospace,monospace);font-size:11.5px;white-space:pre}
        .slg-l.cur{background:var(--accent-soft)}
        .slg-n{width:52px;flex:none;text-align:right;color:var(--text-3);opacity:.45;
          font-variant-numeric:tabular-nums;user-select:none}
        .slg-g{width:62px;flex:none;color:var(--text-3);overflow:hidden;text-overflow:ellipsis}
        .slg-t{flex:1;min-width:0;color:var(--text-2);overflow:hidden;text-overflow:ellipsis}
        .slg-l.k-say .slg-t{color:var(--text)}
        .slg-l.k-say .slg-g{color:var(--accent)}
        .slg-l.k-tool .slg-t{color:var(--accent)}
        .slg-l.k-tool .slg-g{color:var(--accent)}
        .slg-l.k-result .slg-t{color:var(--text-3)}
        .slg-l.k-think .slg-t{color:var(--text-3);font-style:italic}
        .slg-l.k-steer .slg-t{color:var(--warn)}
        .slg-l.k-steer .slg-g{color:var(--warn)}
        .slg-l.k-final .slg-t{color:var(--good)}
        .slg-l.k-final .slg-g{color:var(--good)}
        .slg-l.k-sys .slg-t{color:var(--text-3);opacity:.6}
        .slg-l.k-sys .slg-n,.slg-l.k-sys .slg-g{opacity:.35}
        .slg-l.k-run{background:var(--surface-2)}
        .slg-l.k-run .slg-t{color:var(--accent);letter-spacing:.04em}
        .slg-m{background:var(--accent);color:var(--bg);border-radius:2px}
        .slg-side{width:320px;flex:none;border-left:1px solid var(--line);background:var(--surface-2);
          overflow:auto;padding:12px 14px}
        .slg-sh{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3);
          margin:10px 0 4px}
        .slg-sh:first-child{margin-top:0}
        .slg-sb{white-space:pre-wrap;line-height:1.55;color:var(--text-2);word-break:break-word}
        .slg-dim{color:var(--text-3)}
        .slg-foot{flex:none;padding:6px 13px;border-top:1px solid var(--line);background:var(--surface-2);
          color:var(--text-3);font-size:11px}
        /* On a narrow pane the transcript column is the thing worth having.
           The brief and the result are still on the item's row in the seat
           panel, so losing the aside here costs nothing that is not one
           keypress away — a 34-character-wide log costs everything. */
        @media (max-width:1080px){
          .slg-back{padding:10px}
          .slg-side{display:none}
          .slg-n{width:38px}
          .slg-g{width:48px}
          .slg-l{gap:8px;padding:0 8px}
        }
      </style>`;
    },
  };
  window.SeatLog = SeatLog;

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
      // A seat that mounted a full editor (art -> SpriteEdit, audio -> AudioLab)
      // has to hand it back before its container is discarded, or the editor
      // stays bound to a detached node and every later open() from the asset
      // library renders into nothing.
      const prev = (window.SeatWS || {})[this.current];
      if (prev && typeof prev.unmount === "function") {
        try { prev.unmount(); } catch (e) {}
      }
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
      // The shared work panel renders above every seat's workspace; the module
      // gets its own child container so neither can wipe the other.
      const strip = document.createElement("div");
      strip.id = "seat-strip";
      const ws = document.createElement("div");
      ws.id = "seat-ws";
      body.appendChild(strip);
      body.appendChild(ws);
      SeatWork.paint(strip, this.current);
      try { mod.render(ws, BGWS); }
      catch (e) { ws.innerHTML = `<div class="empty">workspace error: ${BGWS.esc(e.message)}</div>`; console.error(e); }
    },
    visible() {
      const view = document.getElementById("view-seats");
      // `.active` is what setWorkspace toggles; `hidden` is never set on a
      // deck view, so the old `!view.hidden` was true forever and every seat
      // kept polling the queue from behind whatever view you had switched to.
      return !!view && view.classList.contains("active");
    },
    refresh() {
      if (!this.visible()) return;
      const mod = (window.SeatWS || {})[this.current];
      try { SeatWork.refresh(); } catch (e) {}
      if (mod && typeof mod.refresh === "function") {
        try { mod.refresh(); } catch (e) {}
      }
    },
    // Leaving the seats view does NOT go through select(), and activate() will
    // rebuild the whole seat body on the way back in — so an embedded editor
    // left running here would be a live AudioContext / keybinding / render loop
    // bound to DOM that is about to be thrown away. Tear it down on the way out.
    watchView() {
      const view = document.getElementById("view-seats");
      if (!view || this._watching || !window.MutationObserver) return;
      this._watching = true;
      let was = view.classList.contains("active");
      new MutationObserver(() => {
        const now = view.classList.contains("active");
        if (was && !now) {
          const mod = (window.SeatWS || {})[this.current];
          if (mod && typeof mod.unmount === "function") {
            try { mod.unmount(); } catch (e) {}
          }
        }
        was = now;
      }).observe(view, { attributes: true, attributeFilter: ["class"] });
    },
  };
  window.SeatShell = SeatShell;
  setInterval(() => SeatShell.refresh(), 3000);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => SeatShell.watchView());
  } else SeatShell.watchView();

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
        // /api/preview only accepts root-relative paths, and a pin's stored
        // path is absolute — the task scope normalised it and the global scope
        // did not, which is why every global ref card rendered blank. One rule
        // for both: cut everything ahead of .bgate, leave relative paths alone.
        const img = path ? `<img src="${BGWS.preview(BGWS.relRef(path))}" onerror="this.style.opacity=.2">` : "";
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
