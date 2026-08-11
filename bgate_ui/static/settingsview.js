/* settingsview.js — every switch in one place, rendered FROM the server's
 * description of itself.
 *
 * Four features each put their switch in a different mechanism: a column on
 * spend_budget, a workspace doc, an env var read inline, a module constant.
 * bgate_core.settings now describes all of them in one registry and
 * GET /api/settings hands that description over verbatim. THIS FILE RENDERS THE
 * DESCRIPTION AND NOTHING ELSE — no key is named here, no range is repeated, no
 * group is invented. Adding a switch is one registry entry in Python and no
 * change in this file; adding a GROUP is one entry too, because the navigation
 * below is built from the groups that came back, not from a list kept here.
 *
 * WHY IT IS NOT ONE COLUMN ANY MORE. It was, and it outgrew it: thirty-odd
 * switches in one scroll, every one of them the same size, so a concurrency
 * ceiling that decides how much of your machine agents may eat looked exactly
 * like a polling interval. Three things changed and nothing else did:
 *
 *   ONE PANE AT A TIME, chosen from the registry's own groups, plus three
 *   lenses that cut across them — everything, what has been CHANGED from the
 *   default, and what the ENVIRONMENT is currently overriding. "What have I
 *   touched on this machine" was previously unanswerable without reading every
 *   row; it is now a number in the sidebar.
 *
 *   A FILTER over key, help text, group and env var name. With this many
 *   switches, scanning is the wrong interaction, and the help text is where the
 *   word you remember usually is ("dirty", "worktree", "webhook").
 *
 *   WEIGHT BY CONSEQUENCE, from flags the registry already declares. `guard`
 *   means turning it on gives up a protection; `human_only` means an agent is
 *   refused the write because the switch is a constraint ON agents. Those rows
 *   carry a marked edge, their badges, and their full help; the cosmetic ones
 *   are clamped to two lines. No key is special-cased to get there — a new
 *   guard flag in Python promotes its row here for free.
 *
 * A FIELD THE ENVIRONMENT OWNS IS DISABLED AND SAYS WHOSE IT IS. `source: "env"`
 * (and its `locked` flag) means a variable is supplying or coercing the value, so
 * the control is dead and the row names the variable — AND, when a value is also
 * stored for the project, prints both: what is in force now and what takes over
 * the moment the variable goes away. A panel that offers to edit a value
 * BGATE_QA_GATE has already forced is the most expensive lie a settings surface
 * can tell; a panel that hides the saved value underneath it is the second.
 *
 * CREDENTIALS ARE A PEER SECTION, NOT A ROW. providerkeys.js owns #pv-host and
 * its own endpoint, because /api/settings returns every field's value verbatim
 * and a secret in that registry would be one missed exception from being
 * printed. This module gives that panel a nav entry of its own and MOVES the
 * existing #pv-host element into the pane — the same element node, so its
 * listeners, its wiring flag and its module's reference to it all survive. It
 * never renders a key, never reads /api/providers, and nothing about a
 * credential passes through the settings payload.
 *
 * WRITE, THEN RE-RENDER FROM THE RESPONSE. PATCH returns the whole description
 * again, and the value that comes back is the EFFECTIVE one, which is not always
 * what was sent — an env var can win, a number is clamped by its declared range,
 * a list is deduped. Painting the value we asked for would show a save that is
 * not in force. So the response is the only thing that repaints the row, and a
 * refusal repaints from a fresh GET rather than leaving the control showing
 * something no store agreed to.
 *
 * PREFIX: cfg-. app.css owns st-* (and pk-, pv-, and thirty-odd others); the
 * controls below deliberately keep their st- classes so the toggles, segments
 * and chips stay the ones the stylesheet already themes, and every piece of NEW
 * chrome is cfg-, which nothing else in the tree uses. Checked before claiming:
 * this panel's neighbour shipped as pk- once and inherited .pk-wrap's
 * position:fixed from the peek overlay.
 */
(function () {
  "use strict";

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const trunc = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  const icon = (name, size) => (window.BGIcon ? BGIcon(name, { size: size || 14 }) : "");
  const cssq = s => (window.CSS && CSS.escape ? CSS.escape(String(s))
    : String(s).replace(/[^\w-]/g, m => "\\" + m));

  // A settings page is not a live view: it is read when you open it and after a
  // write. This is the floor under a driver that calls render() on the console's
  // 3s tick — a switch nobody is touching does not need re-fetching that often.
  const STALE_MS = 20000;

  const SOURCE_NOTE = {
    default: "the built-in default",
    stored: "saved for this project",
    env: "forced by the environment",
  };

  /* The three cross-cutting views, and the three panes that are not settings at
     all. Prefixed so they can never collide with a group name the registry adds
     later — GROUPS is Python's list and this file does not get a vote on it. */
  const ALL = "~all", CHANGED = "~changed", ENVL = "~env", CREDS = "~creds",
        LOCAL = "~local", AGENTS = "~agents";
  const SLUG = { [ALL]: "all", [CHANGED]: "changed", [ENVL]: "env",
                 [CREDS]: "credentials", [LOCAL]: "local", [AGENTS]: "agent-clis" };
  const UNSLUG = { all: ALL, changed: CHANGED, env: ENVL, credentials: CREDS,
                   local: LOCAL, "agent-clis": AGENTS };

  /* WIRED UP — the panes that answer "what is connected", as opposed to the
     registry groups above them, which answer "how the floor behaves". Three
     destinations, three questions, and they are peers rather than one stacked
     pane because they are read at different moments for different reasons:

       Credentials      is there a key for a hosted provider
       Local generators can this machine make art right now
       Agent CLIs       is Claude Code installed, and can it see our tools

     The last two shipped joined by an "and" — which was the tell, because they
     had arrived in one task rather than because they belong together. A ComfyUI
     endpoint and an MCP registration have nothing in common past both being
     local. Splitting them cost one row here.

     Each is an element declared in index.html and owned by another module,
     MOVED into its slot and parked back in the view (hidden) when a different
     pane is showing — never rebuilt, because a fresh div with the same id looks
     identical and is dead: the owning module holds a reference to the original
     and has wired its listeners onto it. */
  const FOREIGN = [
    { pane: CREDS, host: "pv-host", slot: "cfg-creds-slot" },
    { pane: LOCAL, host: "lc-host", slot: "cfg-local-slot" },
    { pane: AGENTS, host: "ag-host", slot: "cfg-agents-slot" },
  ];
  const WIRED = FOREIGN.map(one => one.pane);

  /* A group's icon, with a fallback that is not a blank. A group this map has
     never heard of still gets a nav row — that is the point of building the nav
     from the response — it just wears the generic sliders. */
  const GROUP_ICON = {
    Dispatch: "agents", Gates: "gate", Art: "art", "Follow-up": "timeline",
    Notifications: "note", Budget: "spend", Console: "overview", Privacy: "hidden",
  };
  const groupIcon = name => GROUP_ICON[name] || "settings";

  const PANE_STORE = "bgate-settings-pane";

  /* Non-default, and said the same way everywhere: the sidebar count, the row
     badge and the reset link all ask this one question. `source === "stored"`
     rather than comparing the raw stored blob, because the raw is whatever the
     store keeps (a budget bool is a 1) and the coerced value is what the human
     set. A stored value hidden under an env override is not counted here — it
     is counted, and printed, under the environment lens instead. */
  const isChanged = f => f.source === "stored"
    && JSON.stringify(f.value) !== JSON.stringify(f.default);

  /* Consequence, from flags the registry declares. Nothing here knows a key. */
  const tierOf = f => f.guard ? "guard" : (f.human_only ? "human" : "");

  function injectStyle() {
    if (document.getElementById("cfg-style")) return;
    const s = document.createElement("style");
    s.id = "cfg-style";
    // Every colour is a theme variable. The orbit ground repaints the whole UI
    // through these and one hardcoded hex is a panel that stops matching.
    s.textContent = [
      /* LEFT, not centred. .st-shell centres itself in 920px because it was
         once a single column of rows; this panel is a 212px nav against a pane
         and centring THAT put a wide gutter on the left of the sidebar while
         every other view in the deck starts at the same x. "centered and
         awkward" was the complaint, and the margin was the whole of it. */
      ".st-shell.cfg-shell{max-width:1180px;margin:0}",

      /* ── header ── */
      ".cfg-top{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:var(--s-4) var(--s-5);",
      "align-items:end;padding-bottom:var(--s-5);margin-bottom:var(--s-6);border-bottom:1px solid var(--line)}",
      ".cfg-h1{font-size:var(--fs-xl);font-weight:var(--fw-medium);color:var(--text);letter-spacing:var(--track-tight)}",
      ".cfg-sub{font-size:var(--fs-xs);color:var(--text-3);line-height:var(--lh);max-width:72ch;margin-top:var(--s-2)}",
      ".cfg-sub code{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-2)}",
      ".cfg-tools{display:flex;gap:var(--s-4);align-items:center;flex-wrap:wrap;justify-content:flex-end}",
      ".cfg-find{position:relative;display:inline-flex;align-items:center}",
      ".cfg-find .bgi{position:absolute;left:9px;color:var(--text-dim);pointer-events:none}",
      ".cfg-search{width:250px;max-width:44vw;padding:var(--s-4) var(--s-4) var(--s-4) 30px;",
      "background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);",
      "color:var(--text);font:inherit;font-size:var(--fs-xs)}",
      ".cfg-search::placeholder{color:var(--text-dim)}",
      ".cfg-search:focus{outline:none;border-color:var(--accent)}",

      /* ── two columns ── */
      ".cfg-body{display:grid;grid-template-columns:212px minmax(0,1fr);gap:var(--s-7);align-items:start}",
      ".cfg-nav{display:flex;flex-direction:column;gap:1px;min-width:0;position:sticky;top:var(--s-4)}",
      ".cfg-navh{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-wide);",
      "text-transform:uppercase;color:var(--text-dim);padding:var(--s-5) var(--s-4) var(--s-3)}",
      ".cfg-item{display:flex;align-items:center;gap:var(--s-3);width:100%;padding:var(--s-3) var(--s-4);",
      "background:transparent;border:1px solid transparent;border-radius:var(--r-sm);color:var(--text-3);",
      "font:inherit;font-size:var(--fs-xs);text-align:left;cursor:pointer;min-width:0}",
      ".cfg-item .bgi{color:var(--text-dim);flex:none}",
      ".cfg-item:hover{background:var(--surface-2);color:var(--text)}",
      ".cfg-item:hover .bgi{color:var(--text-3)}",
      ".cfg-item.on{background:var(--surface-3);border-color:var(--line);color:var(--text)}",
      ".cfg-item.on .bgi{color:var(--text-2)}",
      ".cfg-item.dim{opacity:.45}",
      ".cfg-lab{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".cfg-n{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-dim);",
      "font-variant-numeric:tabular-nums;flex:none}",
      ".cfg-item.on .cfg-n{color:var(--text-2)}",
      ".cfg-n.hot{color:var(--accent)}",
      ".cfg-n.warn{color:var(--warn)}",
      ".cfg-sep{height:1px;background:var(--line-soft);margin:var(--s-4) var(--s-4)}",

      /* ── the pane ── */
      ".cfg-main{min-width:0}",
      /* EVERY BLOCK IN THE PANE IS A .spanel WITH A .sec-h BAND (app.css).
         This page had three competing header treatments - a big .cfg-panehead
         <h3> when a group was the whole pane, a small uppercase .cfg-sech rule
         when it was one of several, and nothing at all around the rows - so
         the same group looked like two different objects depending on how you
         arrived at it, and thirty settings rows ran together underneath either
         one. One shape now, the same one the bible and the seat workspaces
         wear, and no local rule redefines it. */
      ".cfg-sec{margin-bottom:var(--s-6)}",
      ".cfg-note{font-size:var(--fs-xs);color:var(--text-3);line-height:var(--lh);max-width:76ch;margin:0 0 var(--s-5)}",
      ".cfg-note:last-child{margin-bottom:0}",
      ".cfg-note code{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-2)}",
      ".cfg-rows{display:flex;flex-direction:column;gap:var(--s-4)}",
      ".cfg-blank{font-size:var(--fs-xs);color:var(--text-3);line-height:var(--lh);max-width:70ch;",
      "padding:var(--s-6);background:var(--surface-1);border:1px solid var(--line);border-radius:var(--r-md)}",
      ".cfg-err{font-size:var(--fs-xs);color:var(--bad);line-height:var(--lh);padding:var(--s-5) var(--s-6);",
      "background:var(--bad-soft);border:1px solid var(--bad-line);border-radius:var(--r-md);margin-bottom:var(--s-6)}",

      /* ── rows: weight by consequence ── */
      ".st-row.cfg-guard{box-shadow:inset 3px 0 0 var(--bad)}",
      ".st-row.cfg-human{box-shadow:inset 3px 0 0 var(--warn)}",
      ".st-row.cfg-hit{border-color:var(--accent)}",
      ".st-row.cfg-clamp .st-help{display:-webkit-box;-webkit-line-clamp:2;line-clamp:2;",
      "-webkit-box-orient:vertical;overflow:hidden}",
      ".cfg-badge{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-wide);",
      "text-transform:uppercase;border-radius:var(--r-xs);padding:var(--s-1) var(--s-3);",
      "border:1px solid var(--line);color:var(--text-3);background:var(--surface-3)}",
      ".cfg-badge.guard{color:var(--bad);border-color:var(--bad-line);background:var(--bad-soft)}",
      ".cfg-badge.human{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}",
      ".cfg-badge.changed{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}",
      ".cfg-prec{grid-column:1 / span 2;display:flex;flex-wrap:wrap;gap:var(--s-2) var(--s-4);",
      "font-size:var(--fs-xs);color:var(--warn);line-height:var(--lh-snug)}",
      ".cfg-prec b{font-family:var(--mono);font-weight:var(--fw-regular);color:var(--text-2)}",
      ".cfg-link{background:none;border:0;padding:0;color:var(--text-dim);font:inherit;",
      "font-family:var(--mono);font-size:var(--fs-2xs);text-decoration:underline;cursor:pointer}",
      ".cfg-link:hover{color:var(--accent)}",

      /* ── narrow ── */
      "@media (max-width:940px){.cfg-body{grid-template-columns:1fr}",
      ".cfg-nav{position:static;flex-direction:row;flex-wrap:wrap;gap:var(--s-2)}",
      ".cfg-navh{display:none}.cfg-sep{display:none}",
      ".cfg-item{width:auto}.cfg-top{grid-template-columns:1fr}.cfg-tools{justify-content:flex-start}}",

      /* The orbit ground marks the pane you are in with the one sheen the theme
         uses everywhere else, at the same 1.5px and the same token. No new hue,
         and inert in the other three grounds — which is why it is scoped rather
         than left to --iris-rim being 0. */
      ':root[data-theme="orbit"] .cfg-item.on{position:relative}',
      ':root[data-theme="orbit"] .cfg-item.on::after{content:"";position:absolute;inset:0;',
      "border-radius:inherit;padding:1.5px;background:var(--iris);opacity:var(--iris-rim);",
      "-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);",
      "-webkit-mask-composite:xor;mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);",
      "mask-composite:exclude;pointer-events:none}",
    ].join("");
    document.head.appendChild(s);
  }

  const SettingsView = {
    root: null, mounted: false,
    payload: null,            // the last /api/settings body, verbatim
    fields: {},               // key -> field, an index over payload.groups
    query: "",                // the filter box
    pane: "",                 // a group name, or one of the four constants
    _open: {},                // key -> true, rows whose help is expanded
    _pv: null,                // {hostId: element} for the FOREIGN hosts, while
                              // they are detached mid-render. null between.
    _focus: "",               // a deep-linked key waiting to be scrolled to
    _busy: "", _err: null, _lastRead: 0, _reading: false, _sig: "", _find: 0,

    /* ---- mount ----------------------------------------------------------
     * Returns false when the host is not in the DOM yet so a caller can retry,
     * the same contract AgentsConsole.mount() has. The id fallbacks are there
     * because index.html's activateWorkspace() calls activate() with no
     * arguments — the view element is the container in that path. */
    mount(container) {
      if (this.mounted) return true;
      const host = typeof container === "string"
        ? document.getElementById(container)
        : (container || document.getElementById("st-host")
          || document.getElementById("view-settings"));
      if (!host) return false;
      this.root = host;
      this.mounted = true;
      injectStyle();
      host.classList.add("st-wrap");
      // The st-* rules live in app.css; only the cfg-* chrome above is injected,
      // and it names no st- selector on its own. A second copy of a rule the
      // stylesheet owns parses later and wins, so an app.css fix would have been
      // invisible with nothing pointing at the reason.
      host.innerHTML = `<div class="st-shell cfg-shell" id="st-shell">
             <div class="cfg-blank">loading settings…</div>
           </div>`;

      /* FOUR delegated listeners for the whole panel, on the container. Every
         control is rebuilt from the response after every write, so per-element
         handlers would need rebinding on each one and a missed rebind is a
         control that silently stops saving. */
      host.addEventListener("click", e => {
        try { this._onClick(e); } catch (err) { this._warn(err); }
      });
      host.addEventListener("change", e => {
        try { this._onChange(e); } catch (err) { this._warn(err); }
      });
      host.addEventListener("input", e => {
        try { this._onInput(e); } catch (err) { this._warn(err); }
      });
      host.addEventListener("keydown", e => {
        // Enter commits a number/text field. Without it the value only lands on
        // blur, and "I typed it and pressed enter" is the most common way to
        // believe a setting was saved when it was not.
        if (e.key === "Escape" && e.target && e.target.id === "cfg-search") {
          this._setQuery("");
          const box = document.getElementById("cfg-search");
          if (box) box.value = "";
          return;
        }
        if (e.key !== "Enter") return;
        const input = e.target && e.target.closest && e.target.closest("[data-st-key]");
        if (!input || input.tagName !== "INPUT") return;
        e.preventDefault();
        input.blur();
      });

      // A pasted #settings/<key> link has to work on a cold load AND on a hash
      // typed into an already-open tab; replaceState (used when a pane changes)
      // deliberately does not fire this, so the two cannot loop.
      window.addEventListener("hashchange", () => {
        try { if (this._readHash()) this.paint(true); } catch (err) { this._warn(err); }
      });

      this.refresh(true);
      return true;
    },

    /* index.html hands a view to its module with activate(); the shell calls it
       on every click of the rail item, so it must be cheap and idempotent. */
    activate(container) {
      if (!this.mount(container)) return false;
      this._readHash();
      this.refresh(false);
      return true;
    },

    /* The driver may hand us either a settings description (straight from a
       PATCH or GET elsewhere) or the console state it happens to be holding.
       Only the first is data; the second is a heartbeat. */
    render(state) {
      if (!this.mounted) return false;
      if (state && !state.__error && Array.isArray(state.groups)) {
        this._absorb(state);
        this.paint();
        return true;
      }
      this.refresh(false);
      return true;
    },

    /* ---- reading -------------------------------------------------------- */
    async refresh(force) {
      if (!this.mounted || this._reading) return;
      if (!force && Date.now() - this._lastRead < STALE_MS) return;
      // A refresh in the middle of typing would take the half-entered number
      // away. Forced reads (mount, an explicit reload, a failed write) still go
      // through: after a refusal the field on screen is the wrong value.
      if (!force && this._typing()) return;
      this._reading = true;
      try {
        const d = await window.readJSON("/api/settings", { groups: [] });
        if (d && d.__error) {
          this._err = { message: String(d.__error) };
          this.paint();
          return;
        }
        // A read that worked clears a read failure but NOT a field refusal: the
        // sentence explaining why a value was rejected is the only record of it,
        // and the re-read that follows a refusal would erase it.
        if (this._err && !this._err.key) this._err = null;
        this._absorb(d);
        this.paint();
      } catch (e) {
        this._warn(e);
      } finally {
        this._reading = false;
        this._lastRead = Date.now();
      }
    },

    _absorb(d) {
      this.payload = d && Array.isArray(d.groups) ? d : { groups: [] };
      const index = {};
      (this.payload.groups || []).forEach(g =>
        (g.fields || []).forEach(f => { if (f && f.key) index[f.key] = f; }));
      this.fields = index;
      this._settlePane();
    },

    field(key) { return this.fields[String(key)] || null; },

    groups() { return (this.payload && this.payload.groups) || []; },

    all() { return Object.keys(this.fields).map(k => this.fields[k]); },

    /* A pane that no longer exists — a group renamed in Python, a stored
       preference from an older build — must not leave the panel showing
       nothing. Fall back to the first group the server sent. */
    _settlePane() {
      const names = this.groups().map(g => g.name);
      const legal = names.concat([ALL, CHANGED, ENVL, CREDS]);
      if (this.pane && legal.indexOf(this.pane) >= 0) return;
      let saved = "";
      try { saved = localStorage.getItem(PANE_STORE) || ""; } catch (e) { saved = ""; }
      this.pane = legal.indexOf(saved) >= 0 ? saved : (names[0] || ALL);
    },

    _typing() {
      const a = document.activeElement;
      return !!(a && this.root && this.root.contains(a)
        && (a.tagName === "INPUT" || a.tagName === "TEXTAREA"));
    },

    /* ---- deep links -----------------------------------------------------
     * #settings/<key> opens the group that holds it and scrolls to the row;
     * #settings/<group|lens> opens that pane. Nothing else in the dashboard
     * reads location.hash, so this namespace is ours and costs no router. */
    _readHash() {
      const m = String(location.hash || "").match(/^#settings\/(.+)$/);
      if (!m) return false;
      const token = decodeURIComponent(m[1]);
      if (this.fields[token]) {
        this.pane = this.fields[token].group;
        this.query = "";
        this._focus = token;
        return true;
      }
      const lens = UNSLUG[token.toLowerCase()];
      if (lens) { this.pane = lens; this.query = ""; return true; }
      const group = this.groups().filter(g =>
        String(g.name).toLowerCase() === token.toLowerCase())[0];
      if (group) { this.pane = group.name; this.query = ""; return true; }
      // An unknown token is most likely a key from a build that has it and this
      // one does not — remembered, so the row is jumped to as soon as the
      // description arrives, rather than silently dropped.
      this._focus = token;
      return false;
    },

    _writeHash(token) {
      try {
        history.replaceState(null, "", "#settings/" + encodeURIComponent(token));
      } catch (e) { /* a browser that refuses is not a reason to fail a click */ }
    },

    setPane(pane) {
      this.pane = pane;
      this.query = "";
      const box = document.getElementById("cfg-search");
      if (box) box.value = "";
      try { localStorage.setItem(PANE_STORE, pane); } catch (e) { }
      this._writeHash(SLUG[pane] || pane);
      this.paint();
    },

    _setQuery(text) {
      this.query = String(text || "");
      this.paint();
    },

    /* ---- writing --------------------------------------------------------
     * One key per PATCH. The endpoint accepts a whole payload and validates all
     * of it before the first write lands, but one key at a time is what makes a
     * refusal legible: the error sentence names its own key and range, so it can
     * be shown against the control that caused it without unpicking
     * detail.errors — which window.mutate does not carry through anyway. */
    async patch(key, value, el) {
      const f = this.field(key);
      if (!f) return;
      if (f.locked) {
        // Defence in depth: the control is already disabled. This catches the
        // enum/chip/toggle path where a click can still reach a styled div.
        window.toast(`${key} is forced by ${this._vars(f)} - the environment wins`);
        return;
      }
      // A switch that WIDENS a guard asks first. `dispatch.allow_dirty` used to
      // need an environment variable; describing it in the registry made it one
      // click, and one click that lets agents write on top of your uncommitted
      // work is one to make on purpose. Only on the way ON — turning a guard
      // back on is never the dangerous direction.
      if (f.guard && value && !f.value) {
        const yes = await window.askConfirm({
          title: `Turn off a guard?`,
          body: `${key}\n\n${f.help}\n\nThis is recorded in the timeline and in `
              + `the notification drawer either way.`,
          ok: "turn it off", cancel: "leave it on", danger: true,
        });
        if (!yes) return;
      }
      if (this._busy) return;   // the response is the new truth; one at a time
      this._busy = key;
      const button = (el && el.tagName === "BUTTON") ? el : null;
      const r = await window.mutate("/api/settings",
        { method: "PATCH", body: { [key]: value }, button, quiet: true });
      this._busy = "";
      if (!r.ok) {
        // The message already names the key and the legal range (the registry
        // writes it). Shown in the row AND as a toast: the row may be scrolled
        // out of view by the time the click lands.
        this._err = { key, message: r.error, status: r.status };
        window.toast(r.error);
        // 409 means another tab wrote first, 503 that a store refused. Either
        // way the value on screen is not what is stored, so re-read.
        await this.refresh(true);
        return;
      }
      this._err = null;
      this._absorb(r.data);
      this.paint();
      // THE LOST CALLER. streamer.js exports StreamerChip.refresh() with a
      // comment saying Settings calls it after a save so the chip does not lag
      // the switch by a poll interval — and nothing here ever did, so flipping
      // privacy.streamer left the chip reading the old state for up to a poll.
      // Guarded and swallowed: this is a cosmetic follow-up to a write that has
      // already succeeded, and it must not turn a saved setting into an error.
      try {
        if (window.StreamerChip) window.StreamerChip.refresh();
      } catch (e) { /* the chip is not mounted on this page */ }
      const now = this.field(key);
      const shown = now ? this.show(now, now.value) : String(value);
      // An env var can make the stored value not the effective one. Saying
      // "saved" and showing something else is the lie this panel exists to stop.
      if (now && now.source === "env") {
        window.toast(`${key} saved, but ${this._vars(now)} is overriding it - `
          + `in force: ${shown}`);
      } else {
        window.toast(`${key} → ${shown}`, "ok");
      }
    },

    _vars(f) {
      const vars = Array.isArray(f.env_vars) ? f.env_vars : [];
      return f.env || vars.join(" / ") || "the environment";
    },

    /* ---- events --------------------------------------------------------- */
    _onClick(e) {
      const t = e.target;
      if (!t || !t.closest) return;
      // An explicit re-read is a clean slate — including the last refusal.
      if (t.closest("[data-st-reload]")) { this._err = null; this.refresh(true); return; }
      const nav = t.closest("[data-cfg-nav]");
      if (nav) { this.setPane(nav.dataset.cfgNav); return; }
      const cfg = t.closest("[data-cfg-act]");
      if (cfg) {
        const act = cfg.dataset.cfgAct;
        if (act === "more") {
          const key = cfg.dataset.cfgKey;
          if (this._open[key]) delete this._open[key]; else this._open[key] = true;
          this.paint();
          return;
        }
        if (act === "link") { this._copyLink(cfg.dataset.cfgKey); return; }
        if (act === "clearq") {
          this._setQuery("");
          const box = document.getElementById("cfg-search");
          if (box) { box.value = ""; box.focus(); }
          return;
        }
        if (act === "goto") { this.setPane(cfg.dataset.cfgNav || ALL); return; }
      }
      const hit = t.closest("[data-st-act]");
      if (!hit || hit.disabled) return;
      const key = hit.dataset.stKey;
      const f = this.field(key);
      if (!f) return;
      const act = hit.dataset.stAct;
      if (act === "bool") { this.patch(key, !f.value, hit); return; }
      if (act === "enum") {
        if (String(f.value) === hit.dataset.stVal) return;   // no-op write
        this.patch(key, hit.dataset.stVal, hit);
        return;
      }
      if (act === "chip") {
        const have = Array.isArray(f.value) ? f.value.map(String) : [];
        const one = hit.dataset.stVal;
        const next = have.indexOf(one) >= 0
          ? have.filter(v => v !== one)
          : have.concat([one]);
        this.patch(key, next, hit);
        return;
      }
      if (act === "reset") { this.patch(key, f.default, hit); return; }
    },

    _onInput(e) {
      const t = e.target;
      if (!t || t.id !== "cfg-search") return;
      // Debounced, and it repaints the nav and the pane only — never the frame,
      // because rewriting the frame would take the input out from under the
      // caret mid-word.
      const text = t.value;
      clearTimeout(this._find);
      this._find = setTimeout(() => this._setQuery(text), 90);
    },

    _onChange(e) {
      const input = e.target && e.target.closest && e.target.closest("[data-st-key]");
      if (!input || !input.dataset.stAct) return;
      const key = input.dataset.stKey;
      const f = this.field(key);
      if (!f) return;
      const act = input.dataset.stAct;
      if (act === "num") {
        const raw = String(input.value || "").trim();
        if (raw === "") { this.paint(true); return; }   // blank is not a number
        if (Number(raw) === Number(f.value)) return;
        this.patch(key, raw, input);
        return;
      }
      if (act === "text") {
        const raw = String(input.value || "").trim();
        if (raw === String(f.value == null ? "" : f.value)) return;
        this.patch(key, raw, input);
        return;
      }
      if (act === "csv") {
        const parts = String(input.value || "").split(",")
          .map(s => s.trim()).filter(Boolean);
        this.patch(key, parts, input);
      }
    },

    async _copyLink(key) {
      const url = location.origin + location.pathname
        + "#settings/" + encodeURIComponent(key);
      this._writeHash(key);
      try {
        await navigator.clipboard.writeText(url);
        window.toast(`link to ${key} copied`, "ok");
      } catch (e) {
        // No clipboard permission (or no secure context). The hash is already
        // in the address bar, which is the thing being linked to.
        window.toast(`${key} is in the address bar - copy it from there`);
      }
    },

    /* ---- painting -------------------------------------------------------
     * The FRAME (header, filter box, the two columns) is written once. The nav
     * and the pane are written on every change. Splitting them is what lets the
     * filter box keep focus and caret while its own results repaint under it. */
    paint(force) {
      if (!this.mounted) return;
      const shell = document.getElementById("st-shell");
      if (!shell) return;
      try {
        const built = !!force || !shell.querySelector("#cfg-frame");
        if (built) this._frame(shell);
        const sig = this._signature();
        if (!built && sig === this._sig) return;
        this._sig = sig;
        this._body();
      } catch (e) {
        this._warn(e);
      }
    },

    /* Every value, source and lock in one string, plus what the panel itself is
       showing. The source belongs in it as much as the value does: setting an
       env var and reloading changes nothing else on the page, and a signature
       that watched only values would leave the row claiming to be editable. */
    _signature() {
      const bits = [
        this.pane, this.query, Object.keys(this._open).sort().join(","),
        this._err ? `${this._err.key || ""}:${this._err.message}` : "",
      ];
      this.groups().forEach(g =>
        (g.fields || []).forEach(f => bits.push(
          `${f.key}=${JSON.stringify(f.value)}/${f.source}/${f.locked ? 1 : 0}`)));
      return bits.join("|");
    },

    _frame(shell) {
      this._detachPv();
      const p = this.payload || {};
      shell.innerHTML = `<div id="cfg-frame">
        <div class="cfg-top">
          <div>
            <div class="cfg-h1">Settings</div>
            <div class="cfg-sub">Precedence is
              <code>${esc(p.precedence || "env > project stored > default")}</code>
              — every row says which layer won, and a row the environment owns
              names the variable and cannot be edited here.</div>
          </div>
          <div class="cfg-tools">
            <label class="cfg-find">${icon("qa", 14)}<input class="cfg-search"
              id="cfg-search" type="search" autocomplete="off" spellcheck="false"
              aria-label="filter settings"
              placeholder="filter key or help text"
              value="${esc(this.query)}"></label>
            <button class="st-btn" type="button" data-st-reload="1">re-read</button>
          </div>
        </div>
        <div class="cfg-body">
          <nav class="cfg-nav" id="cfg-nav" aria-label="Settings sections"></nav>
          <div class="cfg-main" id="cfg-main"></div>
        </div>
      </div>`;
    },

    _body() {
      const nav = document.getElementById("cfg-nav");
      const main = document.getElementById("cfg-main");
      if (!nav || !main) return;
      this._detachPv();
      try {
        nav.innerHTML = this._nav();
        main.innerHTML = this._pane();
      } finally {
        // ALWAYS, even if a render above threw: #pv-host and #lc-host belong to
        // other modules and dropping either on the floor takes that panel out
        // of the page until a reload.
        this._attachPv();
      }
      this._jump();
    },

    /* ---- the foreign hosts ----------------------------------------------
     * MOVED, never rebuilt. Each owning module holds a reference to its exact
     * element, wired its listeners onto it and marks it with a data- flag; a
     * fresh div with the same id would look identical and be dead. Names kept
     * as _detachPv/_attachPv because tests/test_settings_ui.py asserts on this
     * behaviour by name and the pair now handles the whole FOREIGN table. */
    _detachPv() {
      this._pv = this._pv || {};
      FOREIGN.forEach(one => {
        const el = this._pv[one.host] || document.getElementById(one.host);
        if (!el) return;
        this._pv[one.host] = el;
        if (el.parentNode) el.parentNode.removeChild(el);
      });
    },

    _attachPv() {
      const held = this._pv;
      if (!held) return;
      const view = document.getElementById("view-settings");
      FOREIGN.forEach(one => {
        const el = held[one.host];
        if (!el) return;
        const slot = document.getElementById(one.slot);
        if (slot) { el.hidden = false; slot.appendChild(el); held[one.host] = null; return; }
        // Not on that pane: park it back in the view, hidden, so it stays in the
        // document. The owning module paints into it either way and finds it by
        // id the next time it activates.
        if (view) { el.hidden = true; view.appendChild(el); held[one.host] = null; }
      });
      if (FOREIGN.every(one => !held[one.host])) this._pv = null;
    },

    _jump() {
      const key = this._focus;
      if (!key) return;
      const row = document.querySelector(`[data-st-row="${cssq(key)}"]`);
      if (!row) {
        // Held until the description arrives: a link opened cold paints the
        // frame before /api/settings answers, and dropping the target there
        // means a pasted deep link works on the second visit only.
        if (this.payload && this.groups().length) this._focus = "";
        return;
      }
      this._focus = "";
      try { row.scrollIntoView({ block: "center", behavior: "smooth" }); }
      catch (e) { row.scrollIntoView(); }
      row.classList.add("cfg-hit");
      setTimeout(() => { try { row.classList.remove("cfg-hit"); } catch (e) { } }, 2600);
    },

    /* ---- navigation ----------------------------------------------------- */
    _terms() {
      return this.query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    },

    /* Key, group, help, choices AND env var names. The var name matters: the
       reason to open this page at all is often "something is overriding me",
       and the string in hand is BGATE_QA_GATE, not gate.mode. */
    _match(f, terms) {
      if (!terms.length) return true;
      const hay = [f.key, f.group, f.help, (f.choices || []).join(" "),
        (f.env_vars || []).join(" ")].join(" ").toLowerCase();
      return terms.every(t => hay.indexOf(t) >= 0);
    },

    _hits() {
      const terms = this._terms();
      if (!terms.length) return null;
      return this.groups().map(g => ({
        name: g.name,
        fields: (g.fields || []).filter(f => this._match(f, terms)),
      })).filter(g => g.fields.length);
    },

    _nav() {
      const hits = this._hits();
      const searching = !!hits;
      const counted = {};
      (hits || []).forEach(g => { counted[g.name] = g.fields.length; });
      const all = this.all();
      const changed = all.filter(isChanged).length;
      const locked = all.filter(f => f.locked).length;
      const found = (hits || []).reduce((n, g) => n + g.fields.length, 0);

      const item = (id, label, ic, n, tone, title) => {
        const on = !searching && this.pane === id;
        // The WIRED destinations never dim under a search: they hold no
        // registry fields, so a zero there means "nothing matched in the
        // settings" rather than "nothing here".
        const dim = searching && !n && WIRED.indexOf(id) < 0;
        return `<button class="cfg-item${on ? " on" : ""}${dim ? " dim" : ""}"
          type="button" data-cfg-nav="${esc(id)}"
          ${title ? `title="${esc(title)}"` : ""}
          aria-current="${on ? "true" : "false"}">
          ${icon(ic, 14)}<span class="cfg-lab">${esc(label)}</span>
          ${n === "" ? "" : `<span class="cfg-n${tone ? " " + tone : ""}">${esc(n)}</span>`}
        </button>`;
      };

      const groups = this.groups().map(g => item(
        g.name, g.name, groupIcon(g.name),
        searching ? (counted[g.name] || 0) : (g.fields || []).length,
        searching && counted[g.name] ? "hot" : ""));

      return item(ALL, "All settings", "seats", searching ? found : all.length,
                  searching ? "hot" : "")
        + item(CHANGED, "Changed", "edit", changed, changed ? "hot" : "",
               "Saved for this project and not the built-in default")
        + item(ENVL, "Env-forced", "doctor", locked, locked ? "warn" : "",
               "A variable in the environment is supplying or forcing the value")
        + `<div class="cfg-navh">Groups</div>`
        + groups.join("")
        /* A HEADING, NOT A SEPARATOR. These three are not "the rest of
           settings" — they are a different question (what is connected, vs how
           the floor behaves) and naming that is worth more than any styling
           inside the panes. */
        + `<div class="cfg-navh">Wired up</div>`
        + item(CREDS, "Credentials", "lock", "", "",
               "API keys for the hosted providers - on their own endpoint, and "
               + "it never sends a key back")
        + item(LOCAL, "Local generators", "art", "", "",
               "ComfyUI and the local image-to-3D servers: what can generate on "
               + "this machine, with no key and no bill")
        + item(AGENTS, "Agent CLIs", "agents", "", "",
               "Claude Code and Codex: installed, and whether Builders Gate is "
               + "registered with the right interpreter");
    },

    /* ---- panes ---------------------------------------------------------- */
    _pane() {
      const readErr = (this._err && !this._err.key)
        ? `<div class="cfg-err">settings could not be read —
           ${esc(this._err.message)}</div>` : "";
      if (!this.payload) return readErr + `<div class="cfg-blank">loading settings…</div>`;
      if (!this.groups().length) {
        return readErr + `<div class="cfg-blank">the settings registry answered with
          nothing — this build's dashboard is older than its core, or the project
          could not be read.</div>`;
      }

      const hits = this._hits();
      if (hits) return readErr + this._found(hits);
      if (this.pane === CREDS) return readErr + this._credentials();
      if (this.pane === LOCAL) return readErr + this._local();
      if (this.pane === AGENTS) return readErr + this._agents();
      if (this.pane === CHANGED) return readErr + this._lens(
        "Changed", isChanged,
        `Everything saved for this project that is not the built-in default.
         Empty is the honest answer on a fresh checkout — the defaults are the
         considered position, and a switch you never moved is one this page has
         no news about.`,
        `Nothing has been changed from its default. Every value on this board is
         the one the registry ships.`);
      if (this.pane === ENVL) return readErr + this._lens(
        "Env-forced", f => !!f.locked,
        `A variable in the environment is supplying or forcing these values, so
         the stored setting is not what the board is using. The control is dead
         on purpose; unset the variable and restart the dashboard to hand the
         switch back to this page.`,
        `Nothing in the environment is overriding this project. Every value on
         this board comes from the project or from the built-in default.`);
      if (this.pane === ALL) {
        return readErr + this.groups().map(g => this._section(g.name, g.fields)).join("");
      }
      const group = this.groups().filter(g => g.name === this.pane)[0];
      if (!group) return readErr + `<div class="cfg-blank">that group is gone.</div>`;
      return readErr + this._section(group.name, group.fields);
    },

    /* One shape for every lid on this page: icon, label, count, actions - the
       .sec-h contract out of app.css, built here so the six callers below
       cannot drift apart the way .cfg-panehead and .cfg-sech did. */
    _sec(ic, title, body, o) {
      o = o || {};
      const n = (o.n === undefined || o.n === null || o.n === "")
        ? "" : `<span class="sec-n${o.tone ? " " + o.tone : ""}">${esc(o.n)}</span>`;
      return `<section class="spanel ${o.kind || ""} cfg-sec">
        <div class="sec-h">${icon(ic, 15)}<h3 class="sec-t">${esc(title)}</h3>${n}
          ${o.note ? `<span class="sec-sub">${esc(o.note)}</span>` : ""}
          ${o.actions ? `<span class="sec-a">${o.actions}</span>` : ""}
        </div>${body}</section>`;
    },

    _lens(title, keep, note, blank) {
      const secs = this.groups().map(g => ({
        name: g.name, fields: (g.fields || []).filter(keep),
      })).filter(g => g.fields.length);
      const n = secs.reduce((t, g) => t + g.fields.length, 0);
      const head = this._sec(title === "Env-forced" ? "doctor" : "edit", title,
        `<p class="cfg-note">${esc(note).replace(/\s+/g, " ")}</p>` +
        (n ? "" : `<div class="cfg-blank">${esc(blank).replace(/\s+/g, " ")}</div>`),
        { kind: "k-doc", n: n || "", note: n ? `field${n === 1 ? "" : "s"}` : "" });
      return head + secs.map(g => this._section(g.name, g.fields)).join("");
    },

    _found(hits) {
      const n = hits.reduce((t, g) => t + g.fields.length, 0);
      return this._sec("qa", `${n} match${n === 1 ? "" : "es"}`,
        `<p class="cfg-note">Matched on key, group, help text and env var name,
          across every group.</p>`,
        { kind: "k-doc", n, tone: "good",
          note: `for “${this.query.trim()}”`,
          actions: `<button class="cfg-link" type="button" data-cfg-act="clearq">clear</button>` })
        + hits.map(g => this._section(g.name, g.fields)).join("");
    },

    _credentials() {
      return this._sec("lock", "Credentials",
        `<p class="cfg-note">Kept out of the registry above on purpose.
          <code>GET /api/settings</code> returns every field's value verbatim —
          which is exactly what lets a new switch render with no code change — so
          a secret described there would be one missed exception from being
          printed on this page. These have their own endpoint, and it never sends
          a key back: what you see below is a state, a reason and the last four
          characters.</p>
        <p class="cfg-note">The rest of this page is <b>this project's</b>
          settings. Keys are the exception: a credential belongs to the person,
          not the game, so each one below can be saved for every project on this
          machine instead. The panel says which layer is actually supplying each
          value, which is the question worth asking when a key is set and
          nothing works.</p>
        <div id="cfg-creds-slot"></div>`,
        { kind: "k-doc", note: "provider keys" });
    },

    /* The other answer to the question Credentials asks. A human wondering "why
       can I not make a 2D image" has two possible answers — no key, or nothing
       running here — and they are one nav row apart on purpose. */
    _local() {
      return this._sec("art", "Local generators",
        `<p class="cfg-note">Generators you run on your own machine: ComfyUI for
          2D art, and the local image-to-3D servers. Configure them here, start
          them yourself, and this page notices - Builders Gate talks to this
          software, it does not launch it, so nothing here can leave a model
          loaded in your GPU after you close the page.</p>
        <div id="cfg-local-slot"></div>`,
        { kind: "k-doc", note: "no key, no bill" });
    },

    /* PLUMBING, NOT CAPABILITY, and that is why it is not the pane above. This
       is touched once at setup and then only when something breaks; it has no
       stored value at all, only "is that CLI installed and is our MCP server
       registered with it, against which interpreter". */
    _agents() {
      return this._sec("agents", "Agent CLIs",
        `<p class="cfg-note">The coding-agent CLIs on this machine, and whether
          the Builders Gate tools are reachable from <em>your own</em> sessions
          of them - which is a different question from whether the board can
          dispatch work, and one that looks fine right up until a tool call
          fails. Set once; come back when something breaks.</p>
        <div id="cfg-agents-slot"></div>`,
        { kind: "k-doc", note: "installation & wiring" });
    },

    /* ONE HEADER, however you arrived. There used to be a `sole` branch that
       drew a big <h3> when a group WAS the pane and a small uppercase rule
       when it was one of several, so the same group was two different objects
       depending on whether you clicked its nav row or the "All settings" lens.
       It is a .spanel with a .sec-h either way now, and the pane's own title
       is the nav row you are standing on. */
    _section(name, fields) {
      const list = fields || [];
      if (!list.length) return "";
      return this._sec(groupIcon(name), name,
        `<div class="cfg-rows">${list.map(f => this._row(f)).join("")}</div>`,
        { kind: "k-list", n: list.length });
    },

    /* ---- one row -------------------------------------------------------- */
    _row(f) {
      const bad = this._err && this._err.key === f.key;
      const range = (f.min != null || f.max != null)
        ? `${f.min != null ? f.min : "−∞"}…${f.max != null ? f.max : "∞"}` : "";
      const changed = isChanged(f);
      const tier = tierOf(f);
      // The consequential rows keep their whole paragraph; the rest are clamped
      // to two lines and open on demand. That difference IS the hierarchy — a
      // page where a polling interval argues at the same length as the switch
      // that lets agents write over your uncommitted work has no hierarchy.
      const weighty = !!tier || f.locked || bad;
      const clamp = !weighty && !this._open[f.key];
      return `<div class="st-row${f.locked ? " locked" : ""}${bad ? " bad" : ""}${
                 tier ? " cfg-" + tier : ""}${clamp ? " cfg-clamp" : ""}"
                   data-st-row="${esc(f.key)}">
        <div class="st-label">
          <code class="st-key">${esc(f.key)}</code>
          ${f.guard
            ? `<span class="cfg-badge guard" title="Turning this on gives up a protection the studio had. The panel confirms it and the ledger records it either way.">guard</span>`
            : ""}
          ${f.human_only
            ? `<span class="cfg-badge human" title="An agent is refused this write. It is a constraint ON agents, and one an agent could switch off is not a constraint.">human only</span>`
            : ""}
          ${changed
            ? `<span class="cfg-badge changed" title="Saved for this project - not the built-in default">changed</span>`
            : ""}
          ${f.scope === "machine"
            ? `<span class="st-tag" title="Describes this machine or checkout, not the game - it does not travel with the project">machine</span>`
            : ""}
          ${f.locked
            ? `<span class="st-tag env" title="${esc(f.env_override || "")}">${esc(this._vars(f))}</span>`
            : ""}
          <span class="st-src ${esc(f.source)}"
                title="${esc(SOURCE_NOTE[f.source] || f.source)}">${esc(f.source)}</span>
        </div>
        <div class="st-ctl">${this._control(f)}</div>
        <div class="st-help">${esc(f.help || "")}</div>
        ${this._precedence(f)}
        <div class="st-foot">
          <span class="st-def">default ${esc(trunc(this.show(f, f.default), 60))}${
            range ? ` · ${esc(range)}` : ""}</span>
          ${changed
            ? `<button class="st-link" type="button" data-st-act="reset"
                       data-st-key="${esc(f.key)}">reset</button>` : ""}
          ${clamp || this._open[f.key]
            ? `<button class="cfg-link" type="button" data-cfg-act="more"
                       data-cfg-key="${esc(f.key)}">${this._open[f.key] ? "less" : "why"}</button>`
            : ""}
          <button class="cfg-link" type="button" data-cfg-act="link"
                  data-cfg-key="${esc(f.key)}"
                  title="Copy a link straight to this setting">link</button>
          ${bad ? `<span class="st-badnote">${esc(this._err.message)}</span>` : ""}
        </div>
      </div>`;
    },

    /* WHICH LAYER WON, on the row, in full. The env note said only that a
       variable was overriding; it never said what the project had SAVED, so a
       value typed here, accepted, and then invisible under a shell profile
       looked like a write that had not landed. `stored` is in the description
       already — this prints it. */
    _precedence(f) {
      if (!f.locked) return "";
      const saved = f.stored == null ? "" : this.show(f, f.stored);
      return `<div class="cfg-prec">
        <span>${esc(f.env_override || `${this._vars(f)} is overriding this`)}</span>
        <span>in force: <b>${esc(trunc(this.show(f, f.value), 48))}</b></span>
        ${saved
          ? `<span>saved here: <b>${esc(trunc(saved, 48))}</b> — it takes effect
             when the variable goes away</span>`
          : `<span>nothing is saved for this project underneath it</span>`}
      </div>`;
    },

    /* One control per declared kind. The kind comes from the registry, so a new
       field of an existing kind renders with no edit here; an UNKNOWN kind falls
       back to a read-only value rather than an input that would POST the wrong
       shape — a control that cannot be right should not pretend. */
    _control(f) {
      const key = esc(f.key);
      const off = f.locked ? " disabled" : "";
      const kind = String(f.kind || "");
      if (kind === "bool") {
        const on = !!f.value;
        return `<button class="st-toggle${on ? " on" : ""}" type="button"
                  data-st-act="bool" data-st-key="${key}"
                  aria-pressed="${on ? "true" : "false"}"${off}>
                  <span class="st-knob"></span></button>
                <span class="st-val">${on ? "on" : "off"}</span>`;
      }
      if (kind === "enum") {
        const choices = Array.isArray(f.choices) ? f.choices : [];
        return `<div class="st-seg" role="group">${choices.map(c => {
          const on = String(f.value) === String(c);
          return `<button class="st-segb${on ? " on" : ""}" type="button"
                    data-st-act="enum" data-st-key="${key}" data-st-val="${esc(c)}"
                    aria-pressed="${on ? "true" : "false"}"${off}>${esc(c)}</button>`;
        }).join("")}</div>`;
      }
      if (kind === "int" || kind === "float") {
        const step = kind === "int" ? "1" : "any";
        return `<input class="st-in st-num" type="number" inputmode="decimal"
                  data-st-act="num" data-st-key="${key}" step="${step}"
                  ${f.min != null ? `min="${esc(f.min)}"` : ""}
                  ${f.max != null ? `max="${esc(f.max)}"` : ""}
                  value="${esc(f.value)}"${off}>`;
      }
      if (kind === "list") {
        const have = Array.isArray(f.value) ? f.value.map(String) : [];
        const choices = Array.isArray(f.choices) ? f.choices : [];
        // A declared choice list is a chip per choice; an open list is text,
        // because there is nothing to enumerate and a chip you cannot add is a
        // dead end.
        if (choices.length) {
          return `<div class="st-chips">${choices.map(c => {
            const on = have.indexOf(String(c)) >= 0;
            return `<button class="st-chip${on ? " on" : ""}" type="button"
                      data-st-act="chip" data-st-key="${key}" data-st-val="${esc(c)}"
                      aria-pressed="${on ? "true" : "false"}"${off}>${esc(c)}</button>`;
          }).join("")}</div>
          <span class="st-val">${have.length} of ${choices.length}</span>`;
        }
        return `<input class="st-in" type="text" data-st-act="csv"
                  data-st-key="${key}" value="${esc(have.join(", "))}"
                  placeholder="comma separated"${off}>`;
      }
      if (kind === "string") {
        return `<input class="st-in" type="text" data-st-act="text"
                  data-st-key="${key}" value="${esc(f.value == null ? "" : f.value)}"
                  placeholder="${esc(f.default ? String(f.default) : "empty")}"${off}>`;
      }
      return `<span class="st-val ro">${esc(trunc(this.show(f, f.value), 80))}
                <i>(kind "${esc(kind)}" — this dashboard has no control for it)</i></span>`;
    },

    /* A value as a sentence fragment. Used for the default line, the precedence
       line and the toast, so it has to be defined for every kind including the
       empty string — an empty default that renders as nothing reads as a bug in
       the panel. */
    show(f, value) {
      const kind = String((f && f.kind) || "");
      if (kind === "bool") {
        // The budget store keeps its booleans as 1/0, and `stored` is raw.
        if (value === 1 || value === "1") return "on";
        if (value === 0 || value === "0") return "off";
        return value ? "on" : "off";
      }
      if (kind === "list") {
        const items = Array.isArray(value) ? value : String(value || "").split(",");
        const clean = items.map(s => String(s).trim()).filter(Boolean);
        return clean.length ? clean.join(", ") : "nothing";
      }
      if (value === "" || value == null) return "empty";
      return String(value);
    },

    _warn(e) { try { console.warn("[settings]", e); } catch (_) { } },

  };

  window.SettingsView = SettingsView;
})();
