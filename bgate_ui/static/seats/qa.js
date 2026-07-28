/* QA seat workspace — a bot playtest environment.
 *
 * The reliable QA method here is driving the REAL game headless: a bot is a
 * scripted sequence of Input actions (jab @ tick 20, move_right held 30 ticks,
 * ...) that the backend replays against the fight scene via a runtime probe,
 * then reports the two fighters' positions / hp / stamina each few ticks. This
 * workspace lets you author bots, run a match, and read what actually happened
 * — plus record a live human/agent playtest and watch the live qa agent.
 *
 * Contract: window.SeatWS.qa = { label, glyph, render(container,bg), refresh() }
 * bg = window.BGWS. Never throws uncaught; every fetch is guarded.
 */
window.SeatWS = window.SeatWS || {};
window.SeatWS.qa = {
  label: "QA",
  glyph: (window.BGIcon ? BGIcon("qa", { size: 15 }) : ""),

  // --- module state -------------------------------------------------------
  _bg: null,
  _container: null,
  _bots: null,          // roster (array); null until loaded
  _editing: null,       // { index, bot } while the editor is open, else null
  _lastRun: null,       // last bot match result
  _running: false,
  _actions: ["move_left", "move_right", "jump", "jab", "hook",
             "block", "duck", "kick_light", "kick_heavy"],
  _items: [],           // active qa work items
  _gates: [],           // qa-gate verify runs (source='qa-gate'), newest first
  _selItem: null,       // selected qa work item id
  _playtest: null,      // last playtest status payload
  _preflight: null,     // /api/playtest/preflight — same gate the overview uses
  _runs: [],            // /api/qa-bots/runs — recorded verdicts, newest first
  _comparators: ["eq", "ne", "lt", "lte", "gt", "gte", "between", "contains"],

  _DEFAULT_BOT: {
    name: "aggressive rushdown",
    ticks: 240,
    actions: [
      { action: "move_right", at_tick: 0, hold_ticks: 30 },
      { action: "jab", at_tick: 20, hold_ticks: 1 },
      { action: "jab", at_tick: 40, hold_ticks: 1 },
      { action: "hook", at_tick: 60, hold_ticks: 1 },
      { action: "move_right", at_tick: 72, hold_ticks: 20 },
      { action: "jab", at_tick: 96, hold_ticks: 1 },
      { action: "kick_heavy", at_tick: 120, hold_ticks: 1 },
      { action: "jab", at_tick: 150, hold_ticks: 1 },
      { action: "hook", at_tick: 180, hold_ticks: 1 },
      { action: "jab", at_tick: 210, hold_ticks: 1 },
    ],
    // A bot that asserts nothing cannot pass — the server answers "unknown"
    // for an empty expect list, so the built-in ships with a real assertion.
    expect: [
      { property: "opponent_hp", comparator: "lt", value: 100,
        label: "the rushdown actually lands damage" },
    ],
  },

  // --- entry point --------------------------------------------------------
  render(container, bg) {
    this._bg = bg;
    this._container = container;
    container.innerHTML = this._shellHTML();
    // Paint sections async so a slow/failed fetch never blanks the workspace.
    this._loadActions();
    this._loadBots();
    this._loadPlaytest();
    this._loadPreflight();
    this._loadRuns();
    this._loadItems();
    this._paintResult();
  },

  refresh() {
    // Lightweight periodic update: don't disturb the editor or a running match.
    if (this._editing || this._running) return;
    this._loadPlaytest();
    this._loadGates();  // verdicts only — never repaints the agent panel (would clobber a steer being typed)
    if (this._selItem != null) this._loadActivity();
  },

  async _loadGates() {
    try {
      const r = await this._bg.get("/api/queue");
      if (r && Array.isArray(r.items)) {
        this._gates = r.items.filter(it => it.seat === "qa" && it.source === "qa-gate")
          .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
        this._paintVerdicts();
      }
    } catch (e) { /* keep last verdicts */ }
  },

  _shellHTML() {
    return `
      <style>
        .qa-wrap{display:flex;flex-direction:column;gap:14px;color:var(--text);font-size:13px}
        .qa-card{background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-lg);padding:var(--s-6)}
        .qa-card h3{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3);font-weight:var(--fw-semi)}
        .qa-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
        .qa-sp{justify-content:space-between}
        .qa-btn{padding:var(--s-4) var(--s-5);background:var(--surface-3);border:1px solid var(--line);border-radius:var(--r-sm);color:var(--text);font:inherit;font-size:var(--fs-sm);cursor:pointer;transition:background var(--dur-fast) var(--ease),border-color var(--dur-fast) var(--ease)}
        .qa-btn:hover{border-color:var(--accent)}
        .qa-btn.pri{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}
        .qa-btn.danger{color:var(--bad)}
        .qa-btn:disabled{opacity:.5;cursor:default}
        .qa-btn.small{padding:3px 8px;font-size:11px}
        .qa-in,.qa-sel{padding:5px 8px;background:var(--bg);border:1px solid var(--line);border-radius:7px;color:var(--text);font:inherit;font-size:12px}
        .qa-in.num{width:64px}
        .qa-bot{display:flex;align-items:center;gap:10px;justify-content:space-between;padding:9px 11px;border:1px solid var(--line);border-radius:9px;margin-bottom:8px;background:var(--bg)}
        .qa-bot b{font-size:13px}
        .qa-bot .meta{color:var(--text-3);font-size:11px}
        .qa-empty{color:var(--text-3);font-size:var(--fs-sm);padding:var(--s-5) var(--s-1);line-height:var(--lh)}
        .qa-actrow{display:flex;gap:6px;align-items:center;margin-bottom:6px}
        .qa-tag{display:inline-block;padding:2px 7px;border-radius:6px;background:var(--surface-1);border:1px solid var(--line);color:var(--text-2);font-size:11px;margin:2px 4px 2px 0}
        table.qa-t{border-collapse:collapse;width:100%;font-size:12px}
        table.qa-t th,table.qa-t td{text-align:right;padding:4px 8px;border-bottom:1px solid var(--line-soft)}
        table.qa-t th{color:var(--text-3);font-weight:var(--fw-semi);text-transform:uppercase;font-size:10px;letter-spacing:.04em}
        table.qa-t td:first-child,table.qa-t th:first-child{text-align:left}
        .qa-pre{background:var(--bg);border:1px solid var(--line-soft);border-radius:8px;padding:9px 11px;font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--text-2);max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-word}
        .qa-kv{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px}
        .qa-kv div{display:flex;flex-direction:column}
        .qa-kv span{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--text-3)}
        .qa-kv b{font-size:16px;font-variant-numeric:tabular-nums}
        .qa-bad{color:var(--bad)}
        .qa-good{color:var(--good)}
        .qa-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
        .qa-feed{display:flex;flex-direction:column;gap:5px;max-height:260px;overflow:auto}
        .qa-step{padding:5px 9px;border-radius:7px;background:var(--bg);border:1px solid var(--line-soft);font-size:12px;line-height:1.35}
        .qa-step .k{font-size:10px;text-transform:uppercase;color:var(--text-3);margin-right:6px}
        .qa-verdict{padding:9px 11px;border:1px solid var(--line);border-radius:9px;margin-bottom:8px;background:var(--bg)}
        .qa-vbadge{display:inline-block;padding:2px 9px;border-radius:6px;border:1px solid;font-size:11px;font-weight:var(--fw-semi);letter-spacing:.05em}
        .qa-nits{margin:7px 0 0;padding-left:18px;color:var(--bad);font-size:12px;line-height:1.5}
        .qa-nits li{margin-bottom:2px}
        .qa-unknown{color:var(--c-narrative)}
        .qa-fail{padding:7px 9px;border:1px solid var(--bad-line);border-radius:8px;background:var(--bad-soft);margin-bottom:6px;font-size:12px;line-height:1.45}
        .qa-fail .lbl{color:var(--bad);font-weight:var(--fw-semi)}
        .qa-sample{color:var(--text-3);font-size:11px;font-family:ui-monospace,Consolas,monospace;margin-top:4px;word-break:break-word}
        .qa-exp{display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
        .qa-exp .qa-in{min-width:0}
        .qa-hist{display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid var(--line-soft);font-size:12px}
        .qa-hist .when{color:var(--text-3);font-size:11px;margin-left:auto}
      </style>
      <div class="qa-wrap">
        <div class="qa-card">
          <div class="qa-row qa-sp"><h3 style="margin:0">Bot roster</h3>
            <div class="qa-row">
              <button class="qa-btn small pri" onclick="SeatWS.qa.runAll()"
                title="Run every bot in sequence and take one verdict for the set — one failure fails it, anything unproven is unknown">▶ run all</button>
              <button class="qa-btn small" onclick="SeatWS.qa.newBot()">+ new bot</button>
            </div></div>
          <div id="qa-roster"><div class="qa-empty">loading bots…</div></div>
          <div id="qa-editor"></div>
          <div id="qa-runall"></div>
        </div>
        <div class="qa-card">
          <h3>Verdicts — automatic QA-gate runs</h3>
          <div id="qa-verdicts"><div class="qa-empty">loading verdicts…</div></div>
        </div>
        <div class="qa-card">
          <h3>Match result</h3>
          <div id="qa-result"><div class="qa-empty">run a bot to see how the fight played out.</div></div>
        </div>
        <div class="qa-card">
          <h3>Bot run history — recorded verdicts</h3>
          <div id="qa-runs"><div class="qa-empty">loading run history…</div></div>
        </div>
        <div class="qa-card">
          <h3>Live playtest recording</h3>
          <div id="qa-playtest"><div class="qa-empty">loading…</div></div>
        </div>
        <div class="qa-card">
          <h3>Live QA agent</h3>
          <div id="qa-agent"><div class="qa-empty">loading…</div></div>
        </div>
      </div>`;
  },

  _$(id) { return this._container ? this._container.querySelector("#" + id) : null; },

  // --- bot roster ---------------------------------------------------------
  async _loadActions() {
    try {
      const r = await this._bg.get("/api/qa-bots/actions");
      if (r && Array.isArray(r.actions) && r.actions.length) this._actions = r.actions;
    } catch (e) { /* keep the default action list */ }
  },

  async _loadBots() {
    let bots = null;
    try {
      const r = await this._bg.get("/api/workspace/qa/bots");
      if (r && Array.isArray(r.data)) bots = r.data;
    } catch (e) { /* fall through to seed */ }
    if (!bots || !bots.length) {
      // Seed with the built-in so a match works out of the box (not yet saved).
      bots = [JSON.parse(JSON.stringify(this._DEFAULT_BOT))];
    }
    this._bots = bots.map(b => this._normBot(b));
    this._paintRoster();
  },

  _normBot(b) {
    b = b || {};
    const actions = Array.isArray(b.actions) ? b.actions.map(a => ({
      action: String((a && a.action) || "jab"),
      at_tick: Math.max(0, parseInt((a && a.at_tick) || 0, 10) || 0),
      hold_ticks: Math.max(1, parseInt((a && a.hold_ticks) || 1, 10) || 1),
    })) : [];
    // Expectations are what turn a run into a verdict. The server refuses a
    // malformed one with a 400 rather than skipping it, so keep the shape exact.
    const expect = Array.isArray(b.expect) ? b.expect.map(e => ({
      property: String((e && e.property) || "").trim(),
      comparator: this._comparators.indexOf((e && e.comparator) || "") !== -1
        ? e.comparator : "eq",
      value: this._parseVal(e && e.value),
      at_tick: (e && e.at_tick != null && e.at_tick !== "")
        ? Math.max(0, parseInt(e.at_tick, 10) || 0) : null,
      label: String((e && e.label) || "").trim(),
    })).filter(e => e.property) : [];
    return {
      name: String(b.name || "unnamed bot"),
      ticks: Math.max(1, parseInt(b.ticks || 240, 10) || 240),
      actions,
      expect,
    };
  },

  // Expectation values are typed: "120" is a number, "[10,20]" a between-pair,
  // "true"/"false" booleans. Everything else stays a string.
  _parseVal(raw) {
    if (typeof raw === "number" || typeof raw === "boolean" || Array.isArray(raw)) return raw;
    const s = String(raw == null ? "" : raw).trim();
    if (!s) return "";
    if (s === "true") return true;
    if (s === "false") return false;
    if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
    if (/^\[.*\]$/.test(s)) { try { return JSON.parse(s); } catch (e) { /* literal */ } }
    return s;
  },
  _showVal(v) { return Array.isArray(v) ? JSON.stringify(v) : String(v == null ? "" : v); },

  _paintRoster() {
    const host = this._$("qa-roster");
    if (!host) return;
    const bg = this._bg;
    if (!this._bots || !this._bots.length) {
      host.innerHTML = '<div class="qa-empty">no bots yet — click “+ new bot”.</div>';
      return;
    }
    host.innerHTML = this._bots.map((b, i) => `
      <div class="qa-bot">
        <div>
          <b>${bg.esc(b.name)}</b>
          <div class="meta">${b.actions.length} action${b.actions.length === 1 ? "" : "s"} · ${b.ticks} ticks · ${
            b.expect.length
              ? `${b.expect.length} expectation${b.expect.length === 1 ? "" : "s"}`
              : '<span class="qa-unknown" title="a bot that asserts nothing can only ever report UNKNOWN">no expectations</span>'}</div>
        </div>
        <div class="qa-row">
          <button class="qa-btn small pri" onclick="SeatWS.qa.runBot(${i})">▶ run</button>
          <button class="qa-btn small" onclick="SeatWS.qa.editBot(${i})">edit</button>
          <button class="qa-btn small danger" onclick="SeatWS.qa.deleteBot(${i})">✕</button>
        </div>
      </div>`).join("");
  },

  async _saveBots() {
    try {
      await this._bg.post("/api/workspace/qa/bots", { data: this._bots });
    } catch (e) { this._bg.toast("save failed", true); }
  },

  newBot() {
    this._editing = { index: -1, bot: { name: "new bot", ticks: 240, actions: [
      { action: this._actions[0] || "jab", at_tick: 0, hold_ticks: 1 }],
      expect: [{ property: "opponent_hp", comparator: "lt", value: 100, at_tick: null, label: "" }] } };
    this._paintEditor();
  },

  editBot(i) {
    const b = this._bots[i];
    if (!b) return;
    this._editing = { index: i, bot: JSON.parse(JSON.stringify(b)) };
    this._paintEditor();
  },

  deleteBot(i) {
    if (!this._bots || !this._bots[i]) return;
    this._bots.splice(i, 1);
    this._saveBots();
    this._paintRoster();
  },

  cancelEdit() { this._editing = null; this._paintEditor(); },

  _paintEditor() {
    const host = this._$("qa-editor");
    if (!host) return;
    if (!this._editing) { host.innerHTML = ""; return; }
    const bg = this._bg;
    const bot = this._editing.bot;
    const opts = a => this._actions.map(n =>
      `<option value="${bg.esc(n)}"${n === a ? " selected" : ""}>${bg.esc(n)}</option>`).join("");
    const props = ["player_x", "opponent_x", "distance", "player_hp", "opponent_hp",
                   "player_stamina", "tick", "ticks", "sample_count", "has_fight"];
    const expRows = (bot.expect || []).map((e, j) => `
      <div class="qa-exp" data-e="${j}">
        <input class="qa-in qa-x-prop" list="qa-props" style="width:130px" placeholder="property" value="${bg.esc(e.property || "")}">
        <select class="qa-sel qa-x-cmp">${this._comparators.map(c =>
          `<option value="${c}"${c === e.comparator ? " selected" : ""}>${c}</option>`).join("")}</select>
        <input class="qa-in qa-x-val" style="width:96px" placeholder="value" value="${bg.esc(this._showVal(e.value))}">
        <span class="meta" style="color:var(--text-3)">@tick</span>
        <input class="qa-in num qa-x-at" type="number" min="0" placeholder="end" value="${e.at_tick == null ? "" : e.at_tick}">
        <input class="qa-in qa-x-lbl" style="flex:1;min-width:110px" placeholder="what this proves (optional)" value="${bg.esc(e.label || "")}">
        <button class="qa-btn small danger" onclick="SeatWS.qa.rmExpect(${j})">✕</button>
      </div>`).join("");
    const rows = bot.actions.map((a, j) => `
      <div class="qa-actrow" data-j="${j}">
        <select class="qa-sel qa-a-action">${opts(a.action)}</select>
        <span class="meta" style="color:var(--text-3)">@tick</span>
        <input class="qa-in num qa-a-at" type="number" min="0" value="${a.at_tick}">
        <span class="meta" style="color:var(--text-3)">hold</span>
        <input class="qa-in num qa-a-hold" type="number" min="1" value="${a.hold_ticks}">
        <button class="qa-btn small danger" onclick="SeatWS.qa.rmAction(${j})">✕</button>
      </div>`).join("");
    host.innerHTML = `
      <div class="qa-card" style="margin-top:10px;background:var(--bg)">
        <div class="qa-row" style="margin-bottom:10px">
          <span class="meta" style="color:var(--text-3)">name</span>
          <input class="qa-in qa-e-name" style="flex:1;min-width:160px" value="${bg.esc(bot.name)}">
          <span class="meta" style="color:var(--text-3)">match ticks</span>
          <input class="qa-in num qa-e-ticks" type="number" min="1" value="${bot.ticks}">
        </div>
        <div class="meta" style="color:var(--text-3);margin-bottom:6px">action schedule (60 ticks ≈ 1 second)</div>
        <div id="qa-actions">${rows || '<div class="qa-empty">no actions — add one below.</div>'}</div>
        <div class="qa-row" style="margin-top:8px">
          <button class="qa-btn small" onclick="SeatWS.qa.addAction()">+ action</button>
        </div>
        <datalist id="qa-props">${props.map(p => `<option value="${p}">`).join("")}</datalist>
        <div class="meta" style="color:var(--text-3);margin:14px 0 6px">expectations — what this run has to prove.
          A bot with none reports <b class="qa-unknown">UNKNOWN</b>, never PASS.</div>
        <div id="qa-expects">${expRows || '<div class="qa-empty">no expectations — this bot cannot pass.</div>'}</div>
        <div class="qa-row" style="margin-top:8px">
          <button class="qa-btn small" onclick="SeatWS.qa.addExpect()">+ expectation</button>
          <div style="flex:1"></div>
          <button class="qa-btn small" onclick="SeatWS.qa.cancelEdit()">cancel</button>
          <button class="qa-btn small pri" onclick="SeatWS.qa.saveEdit()">save bot</button>
        </div>
      </div>`;
  },

  // Pull the live editor DOM back into the editing model (so add/remove keep edits).
  _syncEditor() {
    if (!this._editing) return;
    const host = this._$("qa-editor");
    if (!host) return;
    const bot = this._editing.bot;
    const nameEl = host.querySelector(".qa-e-name");
    const ticksEl = host.querySelector(".qa-e-ticks");
    if (nameEl) bot.name = nameEl.value.trim() || "unnamed bot";
    if (ticksEl) bot.ticks = Math.max(1, parseInt(ticksEl.value, 10) || 240);
    const rows = host.querySelectorAll(".qa-actrow");
    bot.actions = Array.from(rows).map(r => ({
      action: r.querySelector(".qa-a-action").value,
      at_tick: Math.max(0, parseInt(r.querySelector(".qa-a-at").value, 10) || 0),
      hold_ticks: Math.max(1, parseInt(r.querySelector(".qa-a-hold").value, 10) || 1),
    }));
    bot.expect = Array.from(host.querySelectorAll(".qa-exp")).map(r => {
      const at = r.querySelector(".qa-x-at").value;
      return {
        property: r.querySelector(".qa-x-prop").value.trim(),
        comparator: r.querySelector(".qa-x-cmp").value,
        value: this._parseVal(r.querySelector(".qa-x-val").value),
        at_tick: at === "" ? null : Math.max(0, parseInt(at, 10) || 0),
        label: r.querySelector(".qa-x-lbl").value.trim(),
      };
    });
  },

  addAction() {
    this._syncEditor();
    this._editing.bot.actions.push({ action: this._actions[0] || "jab", at_tick: 0, hold_ticks: 1 });
    this._paintEditor();
  },

  rmAction(j) {
    this._syncEditor();
    this._editing.bot.actions.splice(j, 1);
    this._paintEditor();
  },

  addExpect() {
    this._syncEditor();
    (this._editing.bot.expect = this._editing.bot.expect || [])
      .push({ property: "", comparator: "eq", value: "", at_tick: null, label: "" });
    this._paintEditor();
  },

  rmExpect(j) {
    this._syncEditor();
    (this._editing.bot.expect || []).splice(j, 1);
    this._paintEditor();
  },

  saveEdit() {
    this._syncEditor();
    const bot = this._normBot(this._editing.bot);
    if (this._editing.index >= 0) this._bots[this._editing.index] = bot;
    else this._bots.push(bot);
    this._editing = null;
    this._saveBots();
    this._paintRoster();
    this._paintEditor();
    this._bg.toast("saved " + bot.name);
  },

  // --- run a bot match ----------------------------------------------------
  async runBot(i) {
    const bot = this._bots && this._bots[i];
    if (!bot) return;
    this._running = true;
    this._lastRun = { pending: true, name: bot.name };
    this._paintResult();
    let res;
    try {
      res = await this._bg.post("/api/qa-bots/run",
        { bot: bot.name, actions: bot.actions, ticks: bot.ticks, expect: bot.expect || [] });
    } catch (e) {
      res = { ok: false, error: String(e && e.message || e) };
    }
    this._running = false;
    this._lastRun = Object.assign({ name: bot.name }, res || {});
    this._paintResult();
    this._loadRuns();
    // `ok` only means the probe drove the game. The VERDICT is the server's.
    const v = (res && res.verdict) || (res && res.ok ? "unknown" : "error");
    if (v === "pass") this._bg.toast(`${bot.name}: PASS`);
    else this._bg.toast(`${bot.name}: ${v.toUpperCase()}` +
      ((res && res.error) ? " — " + res.error : ""), v !== "pass");
  },

  /* One verdict for the whole roster — the shape a gate can consume. The
   * server is deliberately pessimistic: one fail fails the set, and anything
   * unproven is unknown rather than green. */
  async runAll() {
    if (!this._bots || !this._bots.length) { this._bg.toast("no bots to run", true); return; }
    const host = this._$("qa-runall");
    if (host) host.innerHTML = `<div class="qa-empty">running ${this._bots.length} bot(s) against the live game…</div>`;
    this._running = true;
    let res;
    try {
      res = await this._bg.post("/api/qa-bots/run-all", {
        bots: this._bots.map(b => ({ bot: b.name, actions: b.actions, ticks: b.ticks, expect: b.expect || [] })),
      });
    } catch (e) { res = { ok: false, error: { message: String(e && e.message || e) } }; }
    this._running = false;
    this._loadRuns();
    if (!host) return;
    const err = this._err(res);
    if (err) { host.innerHTML = `<div class="qa-fail"><span class="lbl">run-all failed</span> — ${this._bg.esc(err)}</div>`; return; }
    const d = this._data(res) || {};
    const counts = d.counts || {};
    host.innerHTML = `
      <div class="qa-verdict" style="margin-top:10px">
        <div class="qa-row" style="gap:10px">
          <span class="qa-vbadge" style="${this._vstyle(d.verdict)}">${this._bg.esc(String(d.verdict || "unknown").toUpperCase())}</span>
          <b>roster verdict</b>
          <span class="meta" style="color:var(--text-3)">${["pass", "fail", "error", "unknown"]
            .map(k => `${counts[k] || 0} ${k}`).join(" · ")}</span>
        </div>
        ${(d.regressions || []).length
          ? `<div class="qa-fail" style="margin-top:7px"><span class="lbl">regressed since baseline</span> — ${
              this._bg.esc(d.regressions.join(", "))}</div>` : ""}
        ${(d.runs || []).map(r => `<div class="qa-hist">
          <span class="qa-vbadge" style="${this._vstyle(r.verdict)}">${this._bg.esc(String(r.verdict || "?").toUpperCase())}</span>
          <b>${this._bg.esc(r.bot || "bot")}</b>
          <span class="meta" style="color:var(--text-3)">${(r.failures || []).length} failed check(s)</span>
        </div>`).join("")}
      </div>`;
  },

  // Envelope helpers: the new routes answer {ok,data}; the older ones are bare.
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
  _vstyle(v) {
    return {
      pass: "background:var(--good-soft);border-color:var(--good-line);color:var(--good)",
      fail: "background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)",
      error: "background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn)",
      unknown: "background:var(--info-soft);border-color:var(--info-line);color:var(--c-narrative)",
    }[String(v || "unknown").toLowerCase()] || "background:var(--surface-1);border-color:var(--line);color:var(--text-2)";
  },

  async _loadRuns() {
    let r = null;
    try { r = await this._bg.get("/api/qa-bots/runs?limit=12"); } catch (e) { /* keep last */ }
    const d = this._data(r);
    if (Array.isArray(d)) this._runs = d;
    this._paintRuns();
  },

  _paintRuns() {
    const host = this._$("qa-runs");
    if (!host) return;
    const bg = this._bg;
    if (!this._runs.length) {
      host.innerHTML = '<div class="qa-empty">no bot runs recorded yet — run a bot and its verdict is kept here, so "when did this start failing" has an answer.</div>';
      return;
    }
    host.innerHTML = this._runs.map(r => `
      <div class="qa-hist">
        <span class="qa-vbadge" style="${this._vstyle(r.verdict)}">${bg.esc(String(r.verdict || "?").toUpperCase())}</span>
        <b>${bg.esc(r.bot || "bot")}</b>
        ${r.is_baseline ? '<span class="qa-tag">baseline</span>' : ""}
        <span class="meta" style="color:var(--text-3)">${r.expectations || 0} check(s)${
          (r.failures || []).length ? ` · ${bg.esc((r.failures[0].reason || r.failures[0].label || "").slice(0, 70))}` : ""}</span>
        <span class="when">${bg.esc(r.created_at || "")}</span>
      </div>`).join("");
  },

  _paintResult() {
    const host = this._$("qa-result");
    if (!host) return;
    const bg = this._bg;
    const r = this._lastRun;
    if (!r) {
      host.innerHTML = '<div class="qa-empty">run a bot to see how the fight played out.</div>';
      return;
    }
    if (r.pending) {
      host.innerHTML = `<div class="qa-empty">running <b>${bg.esc(r.name)}</b> against the live game…</div>`;
      return;
    }
    const s = r.summary;
    // `ok` says the probe drove the game; it is NOT a verdict. The server
    // judges the expectations and answers pass/fail/error/unknown — painting
    // green off `ok` is exactly the gate-that-does-not-gate this seat is for.
    const verdict = String(r.verdict || (r.ok ? "unknown" : "error")).toLowerCase();
    let html = `<div class="qa-row qa-sp" style="margin-bottom:10px">
      <div class="qa-row" style="gap:10px">
        <span class="qa-vbadge" style="${this._vstyle(verdict)}">${bg.esc(verdict.toUpperCase())}</span>
        <b>${bg.esc(r.name || "match")}</b>
        <span class="meta" style="color:var(--text-3)">${r.ok ? "drove the game" : bg.esc(r.error || "the probe failed")}</span>
      </div>
      <span class="meta" style="color:var(--text-3)">${r.seconds != null ? r.seconds + "s" : ""}</span></div>`;
    if (verdict === "unknown") {
      html += `<div class="qa-empty qa-unknown">this run asserted nothing — add expectations to the bot so it can pass or fail. Unknown is not a pass.</div>`;
    }

    const failures = Array.isArray(r.failures) ? r.failures : [];
    if (failures.length) {
      html += failures.map(f => `<div class="qa-fail">
        <span class="lbl">${bg.esc(f.label || `${f.property} ${f.comparator} ${this._showVal(f.value)}`)}</span>
        <div>${bg.esc(f.reason || "failed")}</div>
        ${f.sample ? `<div class="qa-sample">sample @tick ${bg.esc(f.sample.tick)} — ${
          bg.esc(Object.keys(f.sample).filter(k => k !== "tick")
            .map(k => `${k}=${f.sample[k]}`).join("  "))}</div>` : ""}
      </div>`).join("");
    } else if (Array.isArray(r.results) && r.results.length) {
      html += `<div class="qa-row" style="margin-bottom:8px">${r.results.map(x =>
        `<span class="qa-tag qa-good">✓ ${bg.esc(x.label || x.property)}</span>`).join("")}</div>`;
    }

    const diff = r.baseline_diff;
    if (diff) {
      const flips = (diff.flipped || []).map(f =>
        `${f.label}: ${f.was_ok ? "passing → failing" : "failing → passing"}`).join(" · ");
      const moved = (diff.changed || []).slice(0, 6).map(c =>
        `${c.property} ${c.was} → ${c.now}`).join(" · ");
      html += `<div class="qa-verdict" style="margin-bottom:8px">
        <div class="meta" style="color:var(--text-3)">vs baseline #${bg.esc(diff.baseline_id)} (${bg.esc(diff.verdict_was || "?")} → ${bg.esc(diff.verdict_now || "?")})${
          diff.regressed ? ' — <b class="qa-bad">REGRESSION</b>' : ""}</div>
        ${flips ? `<div class="qa-bad" style="font-size:12px;margin-top:4px">${bg.esc(flips)}</div>` : ""}
        ${moved ? `<div class="meta" style="color:var(--text-3);margin-top:4px">${bg.esc(moved)}</div>` : ""}
      </div>`;
    }

    if (s && s.final) {
      const f = s.final;
      const kv = (label, val, cls) => `<div><span>${label}</span><b class="${cls || ""}">${val == null ? "—" : val}</b></div>`;
      html += `<div class="qa-kv">
        ${kv("player x", f.player_x)}
        ${kv("opponent x", f.opponent_x)}
        ${kv("distance", f.distance)}
        ${kv("player hp", f.player_hp, "qa-good")}
        ${kv("opponent hp", f.opponent_hp, f.opponent_hp != null && f.opponent_hp < 100 ? "qa-good" : "")}
        ${kv("player stamina", f.player_stamina)}
      </div>`;
    }

    if (s && Array.isArray(s.samples) && s.samples.length) {
      html += `<table class="qa-t"><thead><tr>
        <th>tick</th><th>player x</th><th>opp x</th><th>dist</th><th>player hp</th><th>opp hp</th><th>stamina</th>
        </tr></thead><tbody>` +
        s.samples.map(sm => `<tr>
          <td>${sm.tick}</td><td>${sm.player_x}</td><td>${sm.opponent_x}</td><td>${sm.distance}</td>
          <td>${sm.player_hp == null ? "—" : sm.player_hp}</td>
          <td>${sm.opponent_hp == null ? "—" : sm.opponent_hp}</td>
          <td>${sm.player_stamina == null ? "—" : sm.player_stamina}</td></tr>`).join("") +
        `</tbody></table>`;
    } else if (r.ok) {
      html += '<div class="qa-empty">the probe ran but sampled no fighter state.</div>';
    }

    if (s && Array.isArray(s.notes) && s.notes.length) {
      html += `<div style="margin-top:8px">${s.notes.map(n => `<span class="qa-tag">${bg.esc(n)}</span>`).join("")}</div>`;
    }
    if (Array.isArray(r.errors) && r.errors.length) {
      html += `<div style="margin-top:8px" class="qa-bad meta">${r.errors.map(e => bg.esc(e)).join("<br>")}</div>`;
    }
    const raw = (r.stdout || "").trim();
    html += `<div style="margin-top:10px"><div class="meta" style="color:var(--text-3);margin-bottom:5px">raw stdout</div>
      <div class="qa-pre">${raw ? bg.esc(raw) : "(no stdout)"}</div></div>`;
    host.innerHTML = html;
  },

  // --- live playtest recording -------------------------------------------
  async _loadPlaytest() {
    let st = null;
    try { st = await this._bg.get("/api/playtest/status"); } catch (e) { /* keep last */ }
    this._playtest = st;
    this._paintPlaytest();
  },

  async _loadPreflight() {
    // Same gate the overview's record button uses: a dead mic / missing
    // recorder has to be visible BEFORE the take, not at transcription time.
    try { this._preflight = await this._bg.get("/api/playtest/preflight?native=false"); }
    catch (e) { this._preflight = null; }
    this._paintPlaytest();
  },

  _paintPlaytest() {
    const host = this._$("qa-playtest");
    if (!host) return;
    const bg = this._bg;
    const st = this._playtest;
    // /api/playtest/status answers {recording:{id,name,telemetry_events,native,
    // level}, processing:[...]} — there is no `active`, and no `event_count`.
    const rec = st && st.recording;
    let statusHtml;
    if (!st) {
      statusHtml = '<span class="meta" style="color:var(--text-3)">status unavailable</span>';
    } else if (rec) {
      const evs = rec.telemetry_events != null ? rec.telemetry_events : 0;
      const lvl = rec.level;
      const mic = (lvl && typeof lvl === "object")
        ? (lvl.rms != null ? ` · mic ${Math.round(Number(lvl.rms) * 100)}%` : "")
        : (typeof lvl === "number" ? ` · mic ${Math.round(lvl * 100)}%` : "");
      const deaf = (lvl && typeof lvl === "object" && lvl.silent === true)
        ? ' · <span class="qa-bad">no mic signal</span>' : "";
      statusHtml = `<span><span class="qa-dot" style="background:var(--bad)"></span>recording
        <b>${bg.esc(rec.name || "session")}</b> · session ${bg.esc(rec.id)} · ${evs} telemetry events${
        rec.native ? " · native" : ""}${mic}${deaf}</span>`;
    } else {
      const proc = st.processing;
      statusHtml = proc && proc.length
        ? `<span><span class="qa-dot" style="background:var(--warn)"></span>processing ${proc.length} session(s)</span>`
        : '<span><span class="qa-dot" style="background:var(--line)"></span>idle</span>';
    }
    const p = this._preflight;
    const notReady = p && p.ready === false;
    const blockers = notReady
      ? Object.keys(p.checks || {}).filter(k => {
          const c = p.checks[k] || {};
          return !(c.ok != null ? c.ok : c.available);
        }).map(k => `${k} (${String((p.checks[k] || {}).reason || "unavailable").slice(0, 60)})`)
      : [];
    host.innerHTML = `
      <div class="qa-row qa-sp">
        <div>${statusHtml}</div>
        <div class="qa-row">
          <input class="qa-in qa-pt-name" placeholder="session name" style="width:170px" ${rec ? "disabled" : ""}>
          <button class="qa-btn small ${rec || notReady ? "" : "pri"}" onclick="SeatWS.qa.startPlaytest()"
            ${rec || notReady ? "disabled" : ""} title="${notReady ? bg.esc("not ready: " + blockers.join(" · ")) : "preflight, rebuild if stale, then record"}">● record</button>
          <button class="qa-btn small danger" onclick="SeatWS.qa.stopPlaytest()" ${rec ? "" : "disabled"}>■ stop</button>
        </div>
      </div>
      <div class="meta" style="color:var(--text-3);margin-top:8px">${
        notReady
          ? `<span class="qa-bad">not ready: ${bg.esc(blockers.join(" · ") || "preflight failed")}</span>`
          : "records a live human/agent play session — the same path as the overview's record button: preflight, rebuild a stale build, then boot the telemetry frame."
      }</div>
      <div class="meta" id="qa-pt-msg" style="color:var(--text-3);margin-top:4px"></div>`;
  },

  /* One record path, not two. The overview rebuilds a stale build, starts the
   * session and boots the frame with ?bgate_session= so telemetry is actually
   * attributed; posting {name} on its own recorded a session no build was
   * wired to. */
  async startPlaytest() {
    const bg = this._bg;
    const el = this._$("qa-playtest") && this._$("qa-playtest").querySelector(".qa-pt-name");
    const name = (el && el.value.trim()) || "qa session";
    const msg = (t, bad) => { const m = this._$("qa-pt-msg"); if (m) { m.textContent = t; m.className = "meta " + (bad ? "qa-bad" : ""); } };
    try {
      const p = await bg.get("/api/playtest/preflight?native=false").catch(() => null);
      this._preflight = p;
      if (p && p.ready === false) {
        msg("record blocked — preflight is not ready; see the blockers above", true);
        this._paintPlaytest();
        return;
      }
      const build = await bg.get("/api/play/status").catch(() => ({ stale: false }));
      if (build && build.stale) {
        msg("rebuilding the current build before recording…");
        const rebuilt = await bg.post("/api/play/rebuild", {});
        if (!rebuilt || !rebuilt.ok) {
          msg("record blocked: current build failed — " + ((rebuilt && rebuilt.error) || "?"), true);
          return;
        }
      }
      const r = await bg.post("/api/playtest/start", { name });
      if (!r || r.error || r.ok === false) {
        msg("start failed: " + ((r && (r.error || (r.error && r.error.message))) || "?"), true);
        bg.toast("could not start recording", true);
        return;
      }
      // Boot the game frame bound to this session so its telemetry lands on it.
      const sid = r.session_id || (r.recording && r.recording.id);
      if (sid && typeof window.bootFrame === "function") {
        try { window.bootFrame(sid); } catch (e) {}
      }
      msg("recording — play the build in the Play & record view and talk through it");
      bg.toast("recording started");
    } catch (e) {
      msg("could not start recording: " + (e && e.message), true);
      bg.toast("could not start recording", true);
    }
    this._loadPlaytest();
  },

  async stopPlaytest() {
    const msg = (t, bad) => { const m = this._$("qa-pt-msg"); if (m) { m.textContent = t; m.className = "meta " + (bad ? "qa-bad" : ""); } };
    try {
      const r = await this._bg.post("/api/playtest/stop", {});
      if (r && r.ok === false) { msg("stop failed: " + (r.error || "?"), true); this._bg.toast("stop failed", true); }
      else {
        msg(`session ${(r && r.session_id) || ""} transcribing — a director triage item lands in the queue when it finishes`);
        if (typeof window.bootFrame === "function") { try { window.bootFrame(); } catch (e) {} }
        this._bg.toast("recording stopped");
      }
    } catch (e) { this._bg.toast("could not stop recording", true); }
    this._loadPlaytest();
  },

  // --- live qa agent + verdicts --------------------------------------------
  async _loadItems() {
    let items = [];
    try {
      const r = await this._bg.get("/api/queue");
      if (r && Array.isArray(r.items)) items = r.items.filter(it => it.seat === "qa");
    } catch (e) { /* none */ }
    this._items = items;
    this._gates = items.filter(it => it.source === "qa-gate")
      .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    if (this._selItem == null && items.length) {
      // Prefer a dispatched/running item.
      const live = items.find(it => it.status === "dispatched") || items[0];
      this._selItem = live ? live.id : null;
    }
    this._paintVerdicts();
    this._paintAgent();
    if (this._selItem != null) this._loadActivity();
  },

  // The gate protocol (qa_gate.py): a verdict is written as "VERDICT: PASS" or
  // "VERDICT: FAIL" into the run's result. Only that marker is a verdict.
  // A done item with no marker is UNKNOWN — the gate finished without saying
  // what it decided, and calling that a PASS is a gate that does not gate.
  _verdictOf(it) {
    const m = /VERDICT:\s*(PASS|FAIL)/i.exec(it.result || "");
    if (m) return m[1].toUpperCase();
    if (it.status === "failed") return "ERROR";
    if (it.status === "done") return "UNKNOWN";
    return "REVIEWING";
  },

  _paintVerdicts() {
    const host = this._$("qa-verdicts");
    if (!host) return;
    const bg = this._bg;
    if (!this._gates.length) {
      host.innerHTML = '<div class="qa-empty">no QA-gate runs yet — when a maker seat completes a work item, an automatic verify run lands here with a PASS/FAIL verdict.</div>';
      return;
    }
    const style = {
      PASS: "background:var(--good-soft);border-color:var(--good-line);color:var(--good)",
      FAIL: "background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)",
      ERROR: "background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn)",
      UNKNOWN: "background:var(--info-soft);border-color:var(--info-line);color:var(--c-narrative)",
      REVIEWING: "background:var(--surface-1);border-color:var(--line);color:var(--text-2)",
    };
    host.innerHTML = this._gates.slice(0, 8).map(g => {
      const v = this._verdictOf(g);
      // "QA gate: verify #12 — title" -> keep the target readable, drop boilerplate
      const label = (g.title || "").replace(/^QA gate:\s*/i, "");
      const ref = parseInt(g.source_ref, 10) || null;
      let detail = "";
      if (v === "FAIL") {
        const after = (g.result || "").split(/VERDICT:\s*FAIL/i)[1] || "";
        const nits = after.split(/\n+/).map(s => s.replace(/^[-*\d.)\s]+/, "").trim())
          .filter(Boolean).slice(0, 5);
        detail = nits.length
          ? `<ul class="qa-nits">${nits.map(n => `<li>${bg.esc(n.slice(0, 160))}</li>`).join("")}</ul>`
          : `<div class="meta" style="color:var(--text-3)">${bg.esc((g.result || "").slice(0, 200))}</div>`;
      } else if (v === "PASS" && g.result) {
        detail = `<div class="meta" style="color:var(--text-3)">checks: ${bg.esc(g.result.slice(0, 180))}</div>`;
      } else if (v === "UNKNOWN") {
        detail = `<div class="meta qa-unknown">the gate run finished without writing a VERDICT line — nothing was decided here. Read the log, then reopen the item or re-run the gate.${
          g.result ? " Last output: " + bg.esc(g.result.slice(0, 160)) : ""}</div>`;
      } else if (v === "ERROR") {
        detail = `<div class="meta" style="color:var(--warn)">gate run died: ${bg.esc((g.result || "no result").slice(0, 160))}</div>`;
      }
      return `<div class="qa-verdict">
        <div class="qa-row qa-sp">
          <div class="qa-row" style="gap:10px">
            <span class="qa-vbadge" style="${style[v]}">${v}</span>
            <b>${bg.esc(label.slice(0, 80))}</b>
          </div>
          <div class="qa-row">
            ${ref ? `<button class="qa-btn small" title="the reviewed work item — a FAIL reopens it with the nitpick list"
                       onclick="SeatWS.qa.focusRef(${ref})">item #${ref}</button>` : ""}
            <button class="qa-btn small" title="open this run's live activity feed below"
              onclick="SeatWS.qa.selectItem(${g.id});document.querySelectorAll('.qa-item-sel').forEach(s=>s.value=${g.id})">log</button>
          </div>
        </div>
        ${detail}
      </div>`;
    }).join("");
  },

  focusRef(id) {
    // Make the offending item the shared active work item (director/art/
    // gameplay panels read it) so "fix it" starts one click from the verdict.
    this._bg.setActiveItem(id);
    this._bg.toast("work item #" + id + " set active — its seat's workspace now targets it");
  },

  _paintAgent() {
    const host = this._$("qa-agent");
    if (!host) return;
    const bg = this._bg;
    if (!this._items.length) {
      host.innerHTML = '<div class="qa-empty">no qa work items in the queue. Art-QA reviews and other qa tasks show up here once dispatched.</div>';
      return;
    }
    const opts = this._items.map(it =>
      `<option value="${it.id}"${it.id === this._selItem ? " selected" : ""}>#${it.id} · ${bg.esc((it.title || "").slice(0, 60))} [${bg.esc(it.status)}]</option>`).join("");
    host.innerHTML = `
      <div class="qa-row qa-sp" style="margin-bottom:10px">
        <select class="qa-sel qa-item-sel" style="flex:1;min-width:220px" onchange="SeatWS.qa.selectItem(this.value)">${opts}</select>
        <div class="qa-row">
          <input class="qa-in qa-steer" placeholder="steer the agent…" style="width:180px">
          <button class="qa-btn small" onclick="SeatWS.qa.steer()">steer</button>
          <button class="qa-btn small danger" onclick="SeatWS.qa.stopAgent()">stop</button>
        </div>
      </div>
      <div id="qa-feed" class="qa-feed"><div class="qa-empty">loading activity…</div></div>`;
  },

  selectItem(v) {
    this._selItem = parseInt(v, 10) || null;
    this._loadActivity();
  },

  async _loadActivity() {
    if (this._selItem == null) return;
    let act = null;
    try { act = await this._bg.get("/api/agent-activity/" + this._selItem); } catch (e) { /* */ }
    const feed = this._$("qa-feed");
    if (!feed) return;
    const bg = this._bg;
    if (!act || !Array.isArray(act.steps) || !act.steps.length) {
      feed.innerHTML = `<div class="qa-empty">${act && act.running ? "agent running — no steps yet." : "no activity recorded for this item."}</div>`;
      if (act && act.final) feed.innerHTML += `<div class="qa-step"><span class="k">result</span>${bg.esc(act.final.text || "")}</div>`;
      return;
    }
    const kindColor = { say: "var(--text-2)", tool: "var(--good)", result: "var(--text-3)", steer: "var(--warn)" };
    let html = act.steps.map(s => {
      const c = kindColor[s.kind] || "var(--text-3)";
      const label = s.kind === "tool" ? bg.esc(s.name || "tool") : s.kind;
      const body = s.kind === "tool"
        ? bg.esc(s.hint || "")
        : bg.esc(s.text || "");
      return `<div class="qa-step"><span class="k" style="color:${c}">${bg.esc(label)}</span>${body}</div>`;
    }).join("");
    if (act.final) {
      html += `<div class="qa-step"><span class="k" style="color:var(--good)">final</span>${bg.esc(act.final.text || "")}</div>`;
    }
    feed.innerHTML = html;
  },

  async steer() {
    if (this._selItem == null) return;
    const el = this._$("qa-agent") && this._$("qa-agent").querySelector(".qa-steer");
    const text = el && el.value.trim();
    if (!text) return;
    try {
      const r = await this._bg.post(`/api/queue/${this._selItem}/steer`, { text });
      if (r && r.ok === false) this._bg.toast(r.error || "steer failed", true);
      else { this._bg.toast("steered"); if (el) el.value = ""; }
    } catch (e) { this._bg.toast("steer failed", true); }
    this._loadActivity();
  },

  async stopAgent() {
    if (this._selItem == null) return;
    try {
      const r = await this._bg.post(`/api/queue/${this._selItem}/stop`, {});
      if (r && r.ok === false) this._bg.toast(r.error || "no live agent", true);
      else this._bg.toast("stopped");
    } catch (e) { this._bg.toast("stop failed", true); }
    this._loadItems();
  },
};
