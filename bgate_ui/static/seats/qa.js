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
  glyph: "✓",

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
        .qa-wrap{display:flex;flex-direction:column;gap:14px;color:#e6e8ee;font-size:13px}
        .qa-card{background:#101319;border:1px solid #1e232c;border-radius:12px;padding:14px 16px}
        .qa-card h3{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#8a93a2;font-weight:600}
        .qa-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
        .qa-sp{justify-content:space-between}
        .qa-btn{padding:6px 12px;background:#161b22;border:1px solid #2b3440;border-radius:8px;color:#e6e8ee;font:inherit;font-size:12px;cursor:pointer}
        .qa-btn:hover{border-color:#3b7f9e}
        .qa-btn.pri{background:#12303a;border-color:#3b7f9e;color:#cfeaf4}
        .qa-btn.danger{color:#f0b3b3}
        .qa-btn:disabled{opacity:.5;cursor:default}
        .qa-btn.small{padding:3px 8px;font-size:11px}
        .qa-in,.qa-sel{padding:5px 8px;background:#0c0f14;border:1px solid #2b3440;border-radius:7px;color:#e6e8ee;font:inherit;font-size:12px}
        .qa-in.num{width:64px}
        .qa-bot{display:flex;align-items:center;gap:10px;justify-content:space-between;padding:9px 11px;border:1px solid #1e232c;border-radius:9px;margin-bottom:8px;background:#0c0f14}
        .qa-bot b{font-size:13px}
        .qa-bot .meta{color:#7a8494;font-size:11px}
        .qa-empty{color:#6b7280;font-size:12px;padding:6px 0}
        .qa-actrow{display:flex;gap:6px;align-items:center;margin-bottom:6px}
        .qa-tag{display:inline-block;padding:2px 7px;border-radius:6px;background:#131922;border:1px solid #24303c;color:#9fb4c2;font-size:11px;margin:2px 4px 2px 0}
        table.qa-t{border-collapse:collapse;width:100%;font-size:12px}
        table.qa-t th,table.qa-t td{text-align:right;padding:4px 8px;border-bottom:1px solid #1a1f28}
        table.qa-t th{color:#7a8494;font-weight:600;text-transform:uppercase;font-size:10px;letter-spacing:.04em}
        table.qa-t td:first-child,table.qa-t th:first-child{text-align:left}
        .qa-pre{background:#07090d;border:1px solid #1a1f28;border-radius:8px;padding:9px 11px;font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#9aa6b4;max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-word}
        .qa-kv{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px}
        .qa-kv div{display:flex;flex-direction:column}
        .qa-kv span{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:#7a8494}
        .qa-kv b{font-size:16px;font-variant-numeric:tabular-nums}
        .qa-bad{color:#f0b3b3}
        .qa-good{color:#8fd6a8}
        .qa-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
        .qa-feed{display:flex;flex-direction:column;gap:5px;max-height:260px;overflow:auto}
        .qa-step{padding:5px 9px;border-radius:7px;background:#0c0f14;border:1px solid #1a1f28;font-size:12px;line-height:1.35}
        .qa-step .k{font-size:10px;text-transform:uppercase;color:#7a8494;margin-right:6px}
        .qa-verdict{padding:9px 11px;border:1px solid #1e232c;border-radius:9px;margin-bottom:8px;background:#0c0f14}
        .qa-vbadge{display:inline-block;padding:2px 9px;border-radius:6px;border:1px solid;font-size:11px;font-weight:700;letter-spacing:.05em}
        .qa-nits{margin:7px 0 0;padding-left:18px;color:#d5b8b8;font-size:12px;line-height:1.5}
        .qa-nits li{margin-bottom:2px}
      </style>
      <div class="qa-wrap">
        <div class="qa-card">
          <div class="qa-row qa-sp"><h3 style="margin:0">Bot roster</h3>
            <button class="qa-btn small" onclick="SeatWS.qa.newBot()">+ new bot</button></div>
          <div id="qa-roster"><div class="qa-empty">loading bots…</div></div>
          <div id="qa-editor"></div>
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
    return {
      name: String(b.name || "unnamed bot"),
      ticks: Math.max(1, parseInt(b.ticks || 240, 10) || 240),
      actions,
    };
  },

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
          <div class="meta">${b.actions.length} action${b.actions.length === 1 ? "" : "s"} · ${b.ticks} ticks</div>
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
      { action: this._actions[0] || "jab", at_tick: 0, hold_ticks: 1 }] } };
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
    const rows = bot.actions.map((a, j) => `
      <div class="qa-actrow" data-j="${j}">
        <select class="qa-sel qa-a-action">${opts(a.action)}</select>
        <span class="meta" style="color:#7a8494">@tick</span>
        <input class="qa-in num qa-a-at" type="number" min="0" value="${a.at_tick}">
        <span class="meta" style="color:#7a8494">hold</span>
        <input class="qa-in num qa-a-hold" type="number" min="1" value="${a.hold_ticks}">
        <button class="qa-btn small danger" onclick="SeatWS.qa.rmAction(${j})">✕</button>
      </div>`).join("");
    host.innerHTML = `
      <div class="qa-card" style="margin-top:10px;background:#0c0f14">
        <div class="qa-row" style="margin-bottom:10px">
          <span class="meta" style="color:#7a8494">name</span>
          <input class="qa-in qa-e-name" style="flex:1;min-width:160px" value="${bg.esc(bot.name)}">
          <span class="meta" style="color:#7a8494">match ticks</span>
          <input class="qa-in num qa-e-ticks" type="number" min="1" value="${bot.ticks}">
        </div>
        <div class="meta" style="color:#7a8494;margin-bottom:6px">action schedule (60 ticks ≈ 1 second)</div>
        <div id="qa-actions">${rows || '<div class="qa-empty">no actions — add one below.</div>'}</div>
        <div class="qa-row" style="margin-top:8px">
          <button class="qa-btn small" onclick="SeatWS.qa.addAction()">+ action</button>
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
        { actions: bot.actions, ticks: bot.ticks });
    } catch (e) {
      res = { ok: false, error: String(e && e.message || e) };
    }
    this._running = false;
    this._lastRun = Object.assign({ name: bot.name }, res || {});
    this._paintResult();
    if (res && res.ok) this._bg.toast("match ran: " + bot.name);
    else this._bg.toast((res && res.error) || "match failed", true);
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
    let html = `<div class="qa-row qa-sp" style="margin-bottom:10px">
      <div><b>${bg.esc(r.name || "match")}</b>
        <span class="${r.ok ? "qa-good" : "qa-bad"}" style="margin-left:8px">${r.ok ? "● drove the game" : "● " + bg.esc(r.error || "failed")}</span></div>
      <span class="meta" style="color:#7a8494">${r.seconds != null ? r.seconds + "s" : ""}</span></div>`;

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
    html += `<div style="margin-top:10px"><div class="meta" style="color:#7a8494;margin-bottom:5px">raw stdout</div>
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

  _paintPlaytest() {
    const host = this._$("qa-playtest");
    if (!host) return;
    const bg = this._bg;
    const st = this._playtest;
    const rec = st && (st.recording || st.active);
    let statusHtml;
    if (!st) {
      statusHtml = '<span class="meta" style="color:#7a8494">status unavailable</span>';
    } else if (rec) {
      const evs = (rec.event_count != null ? rec.event_count : (st.event_count != null ? st.event_count : "—"));
      statusHtml = `<span><span class="qa-dot" style="background:#e0524a"></span>recording
        <b>${bg.esc(rec.name || "session")}</b> · ${evs} events</span>`;
    } else {
      const proc = st.processing;
      statusHtml = proc && proc.length
        ? `<span><span class="qa-dot" style="background:#d9a441"></span>processing ${proc.length} session(s)</span>`
        : '<span><span class="qa-dot" style="background:#3a4350"></span>idle</span>';
    }
    host.innerHTML = `
      <div class="qa-row qa-sp">
        <div>${statusHtml}</div>
        <div class="qa-row">
          <input class="qa-in qa-pt-name" placeholder="session name" style="width:170px" ${rec ? "disabled" : ""}>
          <button class="qa-btn small ${rec ? "" : "pri"}" onclick="SeatWS.qa.startPlaytest()" ${rec ? "disabled" : ""}>● record</button>
          <button class="qa-btn small danger" onclick="SeatWS.qa.stopPlaytest()" ${rec ? "" : "disabled"}>■ stop</button>
        </div>
      </div>
      <div class="meta" style="color:#7a8494;margin-top:8px">records a live human/agent play session (same flow as the app playtest) for QA to watch back.</div>`;
  },

  async startPlaytest() {
    const el = this._$("qa-playtest") && this._$("qa-playtest").querySelector(".qa-pt-name");
    const name = (el && el.value.trim()) || "qa session";
    try {
      const r = await this._bg.post("/api/playtest/start", { name });
      if (r && r.error) this._bg.toast(r.error, true);
      else this._bg.toast("recording started");
    } catch (e) { this._bg.toast("could not start recording", true); }
    this._loadPlaytest();
  },

  async stopPlaytest() {
    try {
      await this._bg.post("/api/playtest/stop", {});
      this._bg.toast("recording stopped");
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

  // The gate protocol (qa_gate.py): a FAIL always writes "VERDICT: FAIL" +
  // the nitpick list into the run's result; a PASS completes with evidence.
  // So: explicit verdict wins; done without a FAIL marker is a PASS; a run
  // whose own session died is an ERROR, not a verdict.
  _verdictOf(it) {
    const m = /VERDICT:\s*(PASS|FAIL)/i.exec(it.result || "");
    if (m) return m[1].toUpperCase();
    if (it.status === "done") return "PASS";
    if (it.status === "failed") return "ERROR";
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
      PASS: "background:#12301f;border-color:#2f6b48;color:#8fd6a8",
      FAIL: "background:#331416;border-color:#7a3535;color:#f0b3b3",
      ERROR: "background:#332a14;border-color:#7a6a35;color:#e0c15a",
      REVIEWING: "background:#131922;border-color:#24303c;color:#9fb4c2",
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
          : `<div class="meta" style="color:#7a8494">${bg.esc((g.result || "").slice(0, 200))}</div>`;
      } else if (v === "PASS" && g.result) {
        detail = `<div class="meta" style="color:#7a8494">checks: ${bg.esc(g.result.slice(0, 180))}</div>`;
      } else if (v === "ERROR") {
        detail = `<div class="meta" style="color:#e0c15a">gate run died: ${bg.esc((g.result || "no result").slice(0, 160))}</div>`;
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
    const kindColor = { say: "#9fb4c2", tool: "#7fd3b0", result: "#8a93a2", steer: "#e0c15a" };
    let html = act.steps.map(s => {
      const c = kindColor[s.kind] || "#8a93a2";
      const label = s.kind === "tool" ? bg.esc(s.name || "tool") : s.kind;
      const body = s.kind === "tool"
        ? bg.esc(s.hint || "")
        : bg.esc(s.text || "");
      return `<div class="qa-step"><span class="k" style="color:${c}">${bg.esc(label)}</span>${body}</div>`;
    }).join("");
    if (act.final) {
      html += `<div class="qa-step"><span class="k" style="color:#8fd6a8">final</span>${bg.esc(act.final.text || "")}</div>`;
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
