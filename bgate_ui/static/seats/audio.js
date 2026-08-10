/* Audio seat workspace (v2).
 *
 * Three modes:
 *   library — the project's sound files, a persisted CUE SHEET mapping game
 *             events -> sounds, and the live agent panel for this seat.
 *   music   — GENERATE music with Suno through kie, then audition the batch and
 *             keep exactly one. See the block comment above _renderMusic.
 *   lab     — AudioLab, mounted in the seat that owns sound.
 *
 * Contract (see _core.js): window.SeatWS.audio = { label, glyph, render, refresh }.
 * `bg` is window.BGWS. Never touch another seat's DOM; never throw uncaught.
 */
(function () {
  const BGICON = (n) => (window.BGIcon ? BGIcon(n, { size: 15 }) : "");
  window.SeatWS = window.SeatWS || {};

  const AUD = {
    label: "Audio",
    glyph: BGICON("audio"),
    _bg: null,
    _root: null,
    _sounds: [],   // [{rel,name,bytes}] from /api/audio/list
    _arts: [],     // audio artifacts pulled from /api/assets/workspace
    _cues: [],     // [{event,sound,note}] persisted at workspace/audio/cues
    _activeItem: null,
    _agentItems: [],
    _mode: "library",   // "library" (sounds + cues) | "music" (Suno) | "lab"

    // --- music generation state (see the block comment above _loadMusic) ---
    _mOpts: null,       // /api/music/options — models, limits, availability
    _mCands: [],        // candidate takes awaiting keep-or-discard
    _mKept: [],         // approved takes, and whether each is really in the game
    _mJobs: [],         // every music job this project has run, newest first
    _mNew: {},          // artifact ids that landed this session — badged "new"
    _mTimer: null,      // 1s elapsed-clock heartbeat, live jobs only
    _mIdle: 0,          // refresh ticks since the last job poll when idle
    _mBusy: false,      // a keep/discard is in flight; don't repaint over it
    _mPainted: false,   // the FORM is built once — never repaint someone's typing

    render(container, bg) {
      this._bg = bg;
      this._root = container;
      this._mPainted = false;
      try {
        const saved = localStorage.getItem("aud-mode");
        this._mode = (saved === "lab" || saved === "music") ? saved : "library";
      } catch (e) {}
      container.innerHTML = this._style() + `
        <div class="aud-modes">
          <button class="aud-mode" data-m="library">${BGICON("audio")} Library &amp; cues</button>
          <button class="aud-mode" data-m="music">${BGICON("waveform")} Music</button>
          <button class="aud-mode" data-m="lab">${BGICON("edit")} Audio lab</button>
        </div>
        <div class="aud-editor" id="aud-editor" hidden></div>
        <div class="aud-wrap" id="aud-music" hidden></div>
        <div class="aud-wrap" id="aud-main">
          <div class="aud-card" id="aud-lib">
            <h3 class="aud-h">${BGICON("audio")} Sound library <span class="aud-sub" id="aud-lib-count"></span>
              <span class="aud-actions">
                <button class="aud-btn aud-primary" id="aud-open-studio"
                        title="Trim, layer, synthesise and mix — the audio lab, in this seat">
                  ${BGICON("edit")} open the audio lab
                </button>
              </span>
            </h3>
            <div id="aud-lib-body"><div class="aud-empty">loading…</div></div>
          </div>
          <div class="aud-card" id="aud-cues">
            <h3 class="aud-h">${BGICON("timeline")} Cue sheet <span class="aud-sub">which sound plays when</span>
              <span class="aud-actions">
                <button class="aud-btn" id="aud-cue-add">+ row</button>
                <button class="aud-btn aud-primary" id="aud-cue-save">save</button>
              </span>
            </h3>
            <div id="aud-cue-body"><div class="aud-empty">loading…</div></div>
          </div>
          <div class="aud-card" id="aud-agent">
            <h3 class="aud-h">${BGICON("agents")} Live audio agent</h3>
            <div id="aud-agent-body"><div class="aud-empty">loading…</div></div>
          </div>
        </div>`;

      // Wire the static buttons once.
      const addBtn = container.querySelector("#aud-cue-add");
      const saveBtn = container.querySelector("#aud-cue-save");
      if (addBtn) addBtn.onclick = () => this._addCueRow();
      if (saveBtn) saveBtn.onclick = () => this._saveCues();

      // The lab used to be a Studio tab and this button used to leave the seat
      // for it — the one surface that can actually change a sound was two
      // workspaces away from the seat that owns sound. It mounts here now.
      const labBtn = container.querySelector("#aud-open-studio");
      if (labBtn) labBtn.onclick = () => this._setMode("lab");
      container.querySelectorAll(".aud-mode").forEach(b =>
        b.addEventListener("click", () => this._setMode(b.dataset.m)));
      const body = container.querySelector("#aud-lib-body");
      if (body) body.addEventListener("click", (ev) => {
        const btn = ev.target.closest("[data-edit]");
        if (btn) this.openSound(btn.getAttribute("data-edit"));
      });

      this._applyMode();
      this._loadAll();
    },

    /* --- the audio lab lives here now -------------------------------------
     * AudioLab.embed() is unchanged; only where it mounts and how it is reached
     * moved. Everything else that hands off to it (the asset library's "open in
     * audio lab") still gets the fullscreen overlay, because AudioLab only
     * embeds into a host that is genuinely on screen.
     */
    _setMode(mode) {
      const next = (mode === "lab" || mode === "music") ? mode : "library";
      if (next === this._mode) return;
      this._mode = next;
      try { localStorage.setItem("aud-mode", next); } catch (e) {}
      this._applyMode();
    },

    _applyMode() {
      const root = this._root;
      if (!root) return;
      const mode = this._mode;
      const lab = mode === "lab";
      const main = root.querySelector("#aud-main");
      const music = root.querySelector("#aud-music");
      const host = root.querySelector("#aud-editor");
      if (main) main.hidden = mode !== "library";
      if (music) music.hidden = mode !== "music";
      if (host) host.hidden = !lab;
      root.querySelectorAll(".aud-mode").forEach(b =>
        b.classList.toggle("on", b.dataset.m === mode));
      if (mode === "music") this._loadMusic();
      if (!lab) { this._unembed(); return; }
      if (!window.AudioLab || !AudioLab.embed) {
        if (host) host.innerHTML = `<div class="aud-empty">the audio lab did not load</div>`;
        return;
      }
      if (host && !host.firstChild) AudioLab.embed(host);
    },

    // Public: open a specific sound in this seat's lab.
    openSound(rel) {
      this._setMode("lab");
      try { if (window.AudioLab) AudioLab.embed(this._root.querySelector("#aud-editor"), rel); }
      catch (e) {}
    },

    _unembed() {
      try { if (window.AudioLab && AudioLab.unembed) AudioLab.unembed(); } catch (e) {}
      const host = this._root && this._root.querySelector("#aud-editor");
      if (host) host.innerHTML = "";
    },

    // Called by SeatShell before this seat's container is discarded. The lab
    // holds an AudioContext, a capture-phase key binding and possibly a playing
    // transport; leaving the seat has to end all three. The music panel holds a
    // 1s interval for the elapsed clock — a seat that leaves a timer behind is
    // a seat that ticks forever against DOM that has been thrown away.
    unmount() { this._stopPulse(); this._unembed(); },

    // Called ~every 3s by the shell. Only refresh the (cheap, non-destructive)
    // live-agent panel — never rebuild the library or clobber unsaved cue edits.
    refresh() {
      try {
        if (this._mode === "music") { this._tickMusic(); return; }
        if (this._activeItem != null) this._renderAgent();
      } catch (e) { /* fail-safe */ }
    },

    async _loadAll() {
      const bg = this._bg;
      const [lib, ws, cues, groups] = await Promise.all([
        bg.get("/api/audio/list").catch(() => ({ sounds: [] })),
        bg.get("/api/queue").catch(() => ({ items: [] })),
        bg.get("/api/workspace/audio/cues").catch(() => ({ data: null })),
        bg.get("/api/assets/workspace").catch(() => ({ groups: [] })),
      ]);

      this._sounds = Array.isArray(lib && lib.sounds) ? lib.sounds : [];
      this._arts = this._audioArtifacts(groups && groups.groups);
      const data = cues && cues.data;
      this._cues = (data && Array.isArray(data.cues)) ? data.cues
        : (Array.isArray(data) ? data : []);
      this._agentItems = ((ws && ws.items) || []).filter(i => i && i.seat === "audio");
      if (this._activeItem == null) {
        const live = this._agentItems.find(i => i.status === "dispatched")
          || this._agentItems.find(i => i.status === "queued")
          || this._agentItems[0];
        this._activeItem = live ? live.id : null;
      }

      this._renderLibrary();
      this._renderCues();
      this._renderAgent();
    },

    // Audio artifacts from the asset workspace: any revision whose kind or path
    // looks like audio. Guarded end-to-end.
    _audioArtifacts(groups) {
      const out = [];
      const isAudio = (r) => {
        const k = String((r && r.kind) || "").toLowerCase();
        const p = String((r && r.path) || "").toLowerCase();
        return k.includes("audio") || k.includes("sound") || k.includes("sfx")
          || /\.(wav|ogg|mp3)$/.test(p);
      };
      try {
        (groups || []).forEach(g => {
          const revs = [].concat(g.candidates || [], g.revisions || []);
          revs.forEach(r => {
            if (isAudio(r)) {
              out.push({
                logical_name: g.logical_name || (r && r.logical_name) || "?",
                path: (r && r.path) || "",
                kind: (r && r.kind) || "",
                status: (r && r.status) || "",
              });
            }
          });
        });
      } catch (e) { /* fail-safe */ }
      return out;
    },

    _fmtBytes(n) {
      n = Number(n) || 0;
      if (n < 1024) return n + " B";
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
      return (n / 1024 / 1024).toFixed(1) + " MB";
    },

    _renderLibrary() {
      const bg = this._bg;
      const body = this._root.querySelector("#aud-lib-body");
      const count = this._root.querySelector("#aud-lib-count");
      if (!body) return;
      const total = this._sounds.length + this._arts.length;
      if (count) count.textContent = total ? `(${total})` : "";

      if (!total) {
        body.innerHTML = '<div class="aud-empty">no audio files found under '
          + '<code>game/assets/audio</code> (or <code>audio/</code>). '
          + 'Drop .wav/.ogg/.mp3 files there and refresh.</div>';
        return;
      }

      const fileSrc = (rel) => "/api/audio/file?rel=" + encodeURIComponent(rel);
      const rows = [];
      this._sounds.forEach(s => {
        const rel = bg.esc(s.rel);
        rows.push(`<tr>
          <td class="aud-name">${bg.esc(s.name)}</td>
          <td class="aud-path" title="${rel}">${rel}</td>
          <td class="aud-bytes">${this._fmtBytes(s.bytes)}</td>
          <td class="aud-play"><audio controls preload="none" src="${fileSrc(s.rel)}"></audio></td>
          <td><button class="aud-btn" data-edit="${rel}"
                title="open ${rel} in the audio lab">edit</button></td>
        </tr>`);
      });
      this._arts.forEach(a => {
        const rel = bg.esc(a.path);
        // Artifact paths (e.g. .bgate_out/...) are project-relative too; only
        // wire a player when the suffix is actually audio.
        const playable = /\.(wav|ogg|mp3)$/i.test(a.path || "");
        rows.push(`<tr>
          <td class="aud-name">${bg.esc(a.logical_name)} <span class="aud-tag">${bg.esc(a.status || "artifact")}</span></td>
          <td class="aud-path" title="${rel}">${rel}</td>
          <td class="aud-bytes">${bg.esc(a.kind || "")}</td>
          <td class="aud-play">${playable
            ? `<audio controls preload="none" src="${fileSrc(a.path)}"></audio>`
            : '<span class="aud-muted">no preview</span>'}</td>
          <td>${playable ? `<button class="aud-btn" data-edit="${rel}"
                title="open ${rel} in the audio lab">edit</button>` : ""}</td>
        </tr>`);
      });

      body.innerHTML = `<table class="aud-tbl">
        <thead><tr><th>name</th><th>path</th><th>size</th><th>play</th><th></th></tr></thead>
        <tbody>${rows.join("")}</tbody></table>`;
    },

    _soundOptions(selected) {
      const bg = this._bg;
      const opts = ['<option value="">— pick a sound —</option>'];
      const rels = new Set();
      this._sounds.forEach(s => rels.add(s.rel));
      this._arts.forEach(a => { if (/\.(wav|ogg|mp3)$/i.test(a.path || "")) rels.add(a.path); });
      // Include a selected value even if it's not in the current library.
      if (selected && !rels.has(selected)) rels.add(selected);
      Array.from(rels).forEach(rel => {
        const sel = rel === selected ? " selected" : "";
        opts.push(`<option value="${bg.esc(rel)}"${sel}>${bg.esc(rel)}</option>`);
      });
      return opts.join("");
    },

    _renderCues() {
      const bg = this._bg;
      const body = this._root.querySelector("#aud-cue-body");
      if (!body) return;
      if (!this._cues.length) {
        body.innerHTML = '<div class="aud-empty">no cues yet — hit '
          + '<b>+ row</b> to map a game event (e.g. <code>jab_hit</code>) to a sound, '
          + 'then <b>save</b>.</div>';
        return;
      }
      const rows = this._cues.map((c, i) => `<tr data-i="${i}">
        <td><input class="aud-in aud-ev" value="${bg.esc(c.event || "")}" placeholder="event (jab_hit)"></td>
        <td><select class="aud-in aud-snd">${this._soundOptions(c.sound || "")}</select></td>
        <td><input class="aud-in aud-nt" value="${bg.esc(c.note || "")}" placeholder="note"></td>
        <td class="aud-prev">${(c.sound && /\.(wav|ogg|mp3)$/i.test(c.sound))
          ? `<audio controls preload="none" src="/api/audio/file?rel=${encodeURIComponent(c.sound)}"></audio>`
          : '<span class="aud-muted">—</span>'}</td>
        <td><button class="aud-btn aud-del" data-i="${i}">✕</button></td>
      </tr>`).join("");
      body.innerHTML = `<table class="aud-tbl">
        <thead><tr><th>event</th><th>sound</th><th>note</th><th>preview</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`;

      body.querySelectorAll(".aud-del").forEach(btn => {
        btn.onclick = () => { this._syncCuesFromDom(); this._cues.splice(Number(btn.dataset.i), 1); this._renderCues(); };
      });
      // Live-update the row preview when the sound select changes.
      body.querySelectorAll("tr[data-i]").forEach(tr => {
        const sel = tr.querySelector(".aud-snd");
        const prev = tr.querySelector(".aud-prev");
        if (sel && prev) sel.onchange = () => {
          const rel = sel.value;
          prev.innerHTML = (rel && /\.(wav|ogg|mp3)$/i.test(rel))
            ? `<audio controls preload="none" src="/api/audio/file?rel=${encodeURIComponent(rel)}"></audio>`
            : '<span class="aud-muted">—</span>';
        };
      });
    },

    _syncCuesFromDom() {
      const body = this._root && this._root.querySelector("#aud-cue-body");
      if (!body) return;
      const rows = body.querySelectorAll("tr[data-i]");
      if (!rows.length) return;
      const next = [];
      rows.forEach(tr => {
        next.push({
          event: (tr.querySelector(".aud-ev") || {}).value || "",
          sound: (tr.querySelector(".aud-snd") || {}).value || "",
          note: (tr.querySelector(".aud-nt") || {}).value || "",
        });
      });
      this._cues = next;
    },

    _addCueRow() {
      this._syncCuesFromDom();
      this._cues.push({ event: "", sound: "", note: "" });
      this._renderCues();
    },

    async _saveCues() {
      this._syncCuesFromDom();
      const clean = this._cues.filter(c => (c.event || c.sound || c.note));
      try {
        const r = await this._bg.post("/api/workspace/audio/cues", { data: { cues: clean } });
        if (r && (r.ok || r.key || r.data !== undefined)) {
          this._cues = clean;
          this._bg.toast(`saved ${clean.length} cue${clean.length === 1 ? "" : "s"}`);
          this._renderCues();
        } else {
          this._bg.toast((r && r.error) || "save failed", true);
        }
      } catch (e) {
        this._bg.toast("save failed", true);
      }
    },

    /* ---- music: generate, watch, audition, keep one -------------------------
     * WHY A GALLERY AND NOT A FILE. One Suno request comes back as SEVERAL
     * takes — the reference shows an array and never commits to a count — so
     * the shape here is the art seat's: a batch of candidates, a human listens,
     * ONE is kept and the rest are rejected, and every take keeps its
     * provenance row either way. Keep and discard are artifact reviews
     * (approved / rejected), the same call art makes, so 'only a human may
     * approve' is inherited rather than reinvented.
     *
     * KEEPING IS WHAT MAKES A TRACK REAL. It copies the file out of the scratch
     * directory into game/assets/audio/music/ — inside the engine project and
     * inside the audio lab's file walk, which deliberately skips .bgate_out.
     * Until then a generated track is not an asset, it is a candidate.
     *
     * THE PROMPT IS THE PANEL. Everything else — name, model, the two mode
     * switches — is qualification and sits on one line under it; style, title,
     * negative tags, vocal gender and duration are behind a disclosure, because
     * a stack of equally-weighted fields makes the one that matters invisible.
     *
     * A RUNNING JOB IS NOT A SPINNER. Suno reports PENDING -> TEXT_SUCCESS ->
     * FIRST_SUCCESS -> SUCCESS and the server turns each into a sentence; this
     * shows that sentence, a bar, a live elapsed count, and what happens next.
     * A minute of unexplained spinner is indistinguishable from a hang, and
     * what people do about an apparent hang is start a second paid generation.
     *
     * EVERY JOB IS VISIBLE, not just the one this tab started. The seat used to
     * hold a single job id in a variable, so a reload, a second generation or a
     * dashboard restart left rows nobody could see or clear — which is what
     * "old prompt still queued" was. Each row names its own prompt, so a batch
     * finishing while the form holds different text cannot be mistaken for an
     * answer to what is on screen.
     *
     * THE FORM IS BUILT ONCE. This seat repaints every three seconds and a
     * prompt is a long thing to type; only the job strip and the gallery are
     * ever repainted. Same rule the art seat's style card follows.
     */
    async _loadMusic() {
      const host = this._root && this._root.querySelector("#aud-music");
      if (!host) return;
      if (!this._mOpts) {
        host.innerHTML = '<div class="aud-card"><div class="aud-empty">loading…</div></div>';
        const got = await this._bg.get("/api/music/options").catch(() => null);
        this._mOpts = (got && got.data) || { available: false,
          reason: "the music API did not answer — restart bgate serve so the "
                + "dashboard picks up the music routes" };
      }
      if (!this._mPainted) { this._paintMusic(); this._mPainted = true; }
      await Promise.all([this._loadJobs(), this._loadCandidates()]);
    },

    async _loadCandidates() {
      const got = await this._bg.get("/api/music/candidates").catch(() => null);
      const d = (got && got.data) || {};
      this._mCands = Array.isArray(d.candidates) ? d.candidates : [];
      this._mKept = Array.isArray(d.kept) ? d.kept : [];
      this._renderCandidates();
    },

    _paintMusic() {
      const bg = this._bg, o = this._mOpts || {};
      const host = this._root.querySelector("#aud-music");
      if (!host) return;
      const models = (o.models || []).map(m =>
        `<option value="${bg.esc(m)}"${m === o.default_model ? " selected" : ""}>${bg.esc(m)}</option>`
      ).join("");
      const blocked = !o.available;

      host.innerHTML = `
        <div class="aud-card mus-compose">
          <h3 class="aud-h">${BGICON("waveform")} Generate music
            <span class="aud-sub">Suno via kie · one request returns ${bg.esc(o.tracks_hint || 2)} takes,
              you keep one</span>
          </h3>
          ${blocked ? `<div class="mus-strip bad">${BGICON("stop")}
            <span><b>Music generation is unavailable.</b> ${bg.esc(o.reason || "kie is not configured")}</span>
          </div>` : ""}

          <textarea class="mus-prompt" id="aud-prompt" rows="3"
            placeholder="Describe the music. e.g. tense low synth loop for a night-time chase, sparse, no drums until the second bar"></textarea>
          <div class="mus-meter">
            <span class="mus-count" id="aud-count"></span>
            <span class="mus-hint" id="aud-hint"></span>
          </div>

          <div class="mus-row">
            <input class="aud-in mus-name" id="aud-name" placeholder="asset name (chase_theme)">
            <select class="aud-in mus-model" id="aud-model" title="Suno model">${models}</select>
            <label class="mus-chip"><input type="checkbox" id="aud-instr" checked>
              <span>instrumental</span></label>
            <label class="mus-chip"><input type="checkbox" id="aud-custom">
              <span>my lyrics</span></label>
            <button class="aud-btn mus-more" id="aud-more" aria-expanded="false">
              ${BGICON("more")} more</button>
            <button class="aud-btn aud-primary mus-go" id="aud-go" ${blocked ? "disabled" : ""}>
              ${BGICON("run")} generate</button>
          </div>

          <div class="mus-adv" id="aud-adv" hidden>
            <label class="aud-f custom-only">
              <span class="aud-fl">style <em id="aud-style-lim"></em></span>
              <input class="aud-in" id="aud-style" placeholder="dark synthwave, analogue tape">
            </label>
            <label class="aud-f custom-only">
              <span class="aud-fl">title <em id="aud-title-lim"></em></span>
              <input class="aud-in" id="aud-title" placeholder="Night Run">
            </label>
            <label class="aud-f">
              <span class="aud-fl">negative tags <em>what to keep out</em></span>
              <input class="aud-in" id="aud-neg" placeholder="brass, choir">
            </label>
            <label class="aud-f vocal-only">
              <span class="aud-fl">vocal gender</span>
              <select class="aud-in" id="aud-voc">
                <option value="">either</option><option value="f">f</option><option value="m">m</option>
              </select>
            </label>
            <label class="aud-f narrow">
              <span class="aud-fl">duration (s)</span>
              <input class="aud-in" id="aud-dur" type="number" placeholder="—">
            </label>
          </div>

          ${this._spendStrip()}
        </div>

        <div id="aud-jobs"></div>

        <div class="aud-card" id="aud-cands">
          <h3 class="aud-h">${BGICON("select")} Takes to audition
            <span class="aud-sub" id="aud-cand-count"></span>
            <span class="aud-actions">
              <button class="aud-btn" id="aud-cand-reload">${BGICON("verify")} refresh</button>
            </span>
          </h3>
          <div id="aud-cand-body"><div class="aud-empty">loading…</div></div>
        </div>`;

      const $ = (id) => host.querySelector(id);
      const sync = () => this._syncLimits();
      ["#aud-model", "#aud-custom", "#aud-instr"].forEach(id => {
        const el = $(id); if (el) el.addEventListener("change", sync);
      });
      const prompt = $("#aud-prompt");
      if (prompt) {
        prompt.addEventListener("input", sync);
        // Ctrl/Cmd+Enter generates — the prompt is where the hands are.
        prompt.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
            ev.preventDefault(); this._generate();
          }
        });
      }
      const more = $("#aud-more");
      if (more) more.onclick = () => {
        const adv = $("#aud-adv");
        adv.hidden = !adv.hidden;
        more.setAttribute("aria-expanded", String(!adv.hidden));
        more.classList.toggle("on", !adv.hidden);
      };
      const go = $("#aud-go");
      if (go) go.onclick = () => this._generate();
      const reload = $("#aud-cand-reload");
      if (reload) reload.onclick = () => { this._loadJobs(); this._loadCandidates(); };
      host.addEventListener("click", (ev) => {
        const take = ev.target.closest("[data-mact]");
        if (take) {
          this._candAction(take.getAttribute("data-mact"),
                           Number(take.getAttribute("data-id")), take);
          return;
        }
        const job = ev.target.closest("[data-jact]");
        if (job) this._jobAction(job.getAttribute("data-jact"),
                                 job.getAttribute("data-job"), job);
      });
      this._syncLimits();
    },

    /* UNPRICED IS A WARNING WITH AN INSTRUCTION, not an error in the corner.
     * kie publishes no per-model price for Suno and the Suno record carries no
     * creditsConsumed, so Builders Gate measures the CREDITS (an account-balance
     * delta) and can only turn them into dollars once a human supplies the rate.
     * Saying "unpriced" in red beside the button read as something being broken;
     * what is actually true is narrower and actionable. */
    _spendStrip() {
      const o = this._mOpts || {}, bg = this._bg;
      if (o.usd_per_credit) {
        return `<div class="mus-strip ok">${BGICON("spend")}
          <span>Spend is recorded at <b>$${bg.esc(Number(o.usd_per_credit).toFixed(4))}</b>
          per credit. Every run lands in the project ledger in dollars.</span></div>`;
      }
      return `<div class="mus-strip warn">${BGICON("spend")}
        <span><b>Spend is not being recorded.</b> kie publishes no price for Suno,
        so a run's credits are measured but no dollar figure is filed — the ledger
        under-counts by whatever music costs you. Put
        <code>BGATE_KIE_USD_PER_CREDIT=&lt;your rate&gt;</code> in the project's
        <code>.env</code> and restart the dashboard. Generation works either way.</span>
      </div>`;
    },

    // The ceilings come from the ADAPTER's own tables (/api/music/options), not
    // from numbers typed into this file — a limit copied into a form is a limit
    // that goes stale silently and then 422s after the user typed 900 words.
    _syncLimits() {
      const host = this._root && this._root.querySelector("#aud-music");
      const o = this._mOpts;
      if (!host || !o || !o.limits) return;
      const model = (host.querySelector("#aud-model") || {}).value || o.default_model;
      const custom = !!(host.querySelector("#aud-custom") || {}).checked;
      const instrumental = !!(host.querySelector("#aud-instr") || {}).checked;
      const lim = o.limits[model] || {};
      const mode = custom ? (lim.custom || {}) : (lim.simple || {});
      const cap = Number(mode.prompt || 0);
      const used = ((host.querySelector("#aud-prompt") || {}).value || "").length;
      const over = cap > 0 && used > cap;

      const count = host.querySelector("#aud-count");
      if (count) {
        count.textContent = `${used} / ${cap}`;
        count.classList.toggle("over", over);
      }
      const sl = host.querySelector("#aud-style-lim");
      if (sl) sl.textContent = `max ${mode.style || 0}`;
      const tl = host.querySelector("#aud-title-lim");
      if (tl) tl.textContent = `max ${mode.title || 0}`;
      host.querySelectorAll(".custom-only").forEach(el => { el.hidden = !custom; });
      host.querySelectorAll(".vocal-only").forEach(el => { el.hidden = instrumental; });

      // duration is ONE model's parameter. Hiding it beats offering a field
      // whose only effect on V5 is a refusal.
      const dur = host.querySelector("#aud-dur");
      const range = lim.duration;
      if (dur) {
        const box = dur.closest(".aud-f");
        if (box) box.hidden = !range;
        if (range) {
          dur.min = range[0]; dur.max = range[1];
          dur.placeholder = `${range[0]}–${range[1]}`;
        } else { dur.value = ""; }
      }
      const hint = host.querySelector("#aud-hint");
      if (hint) {
        hint.textContent = over
          ? `${used - cap} over the ${model} limit — kie would refuse this`
          : (custom ? `${model} · your lyrics` : `${model} · Suno writes it`)
            + (used ? " · ctrl+enter to generate" : "");
        hint.classList.toggle("bad", over);
      }
      const go = host.querySelector("#aud-go");
      if (go && o.available !== false) go.disabled = over || !used;
    },

    async _generate() {
      const host = this._root && this._root.querySelector("#aud-music");
      const bg = this._bg;
      if (!host) return;
      const val = (id) => ((host.querySelector(id) || {}).value || "").trim();
      const on = (id) => !!(host.querySelector(id) || {}).checked;
      const body = {
        prompt: val("#aud-prompt"), name: val("#aud-name"),
        model: val("#aud-model"), custom: on("#aud-custom"),
        instrumental: on("#aud-instr"),
      };
      if (on("#aud-custom")) {
        if (val("#aud-style")) body.style = val("#aud-style");
        if (val("#aud-title")) body.title = val("#aud-title");
      }
      if (val("#aud-neg")) body.negative_tags = val("#aud-neg");
      if (!on("#aud-instr") && val("#aud-voc")) body.vocal_gender = val("#aud-voc");
      if (val("#aud-dur")) body.duration = Number(val("#aud-dur"));
      if (!body.prompt) { bg.toast("a generation needs a prompt", true); return; }

      const r = await bg.post("/api/music/generate", body);
      const d = (r && r.data) || {};
      if (!d.job_id) { bg.toast(this._errText(r) || "generate failed", true); return; }
      bg.toast("generating — watch the job below");
      await this._loadJobs();
      const strip = host.querySelector("#aud-jobs");
      if (strip) strip.scrollIntoView({ block: "nearest", behavior: "smooth" });
    },

    /* ---- jobs ------------------------------------------------------------- */
    async _loadJobs() {
      const got = await this._bg.get("/api/music/jobs").catch(() => null);
      const jobs = ((got && got.data) || {}).jobs;
      const next = Array.isArray(jobs) ? jobs : [];
      // WHICH ONES JUST FINISHED. Compared against the previous poll rather
      // than tracked from the click, so a job started in another tab — or
      // before a reload — still announces itself.
      const was = {};
      (this._mJobs || []).forEach(j => { was[j.id] = j.terminal; });
      const landed = next.filter(j => j.terminal && was[j.id] === false);
      this._mJobs = next;
      this._renderJobs();
      if (landed.length) this._announce(landed);
      this._pulse();
      return next;
    },

    _announce(landed) {
      const bg = this._bg;
      landed.forEach(job => {
        const r = job.result || {};
        if (job.state === "done" && r.ok) {
          const n = (r.candidates || []).length;
          (r.candidates || []).forEach(c => {
            if (c.artifact_id) this._mNew[c.artifact_id] = true;
          });
          bg.toast(`${n} take${n === 1 ? "" : "s"} ready — “${
            String(r.prompt || "").slice(0, 40)}”`);
        } else if (r.cancelled) {
          bg.toast("generation cancelled" + (r.task_id ? " — recoverable" : ""), true);
        } else {
          bg.toast(r.error || job.error || "generation failed", true);
        }
      });
      this._loadCandidates().then(() => {
        const body = this._root && this._root.querySelector("#aud-cands");
        if (body) body.scrollIntoView({ block: "start", behavior: "smooth" });
      });
    },

    // A 1s heartbeat ONLY while something is live, so the elapsed count reads
    // like a stopwatch instead of jumping in threes. Cleared the moment nothing
    // is running, and on unmount — a seat that leaves a timer behind is a seat
    // that polls forever from a detached DOM.
    _pulse() {
      const live = (this._mJobs || []).some(j => !j.terminal && !j.orphaned);
      if (!live) { this._stopPulse(); return; }
      if (this._mTimer) return;
      this._mTimer = setInterval(() => {
        try {
          if (this._mode !== "music" || !this._root || !this._root.isConnected) {
            this._stopPulse(); return;
          }
          this._root.querySelectorAll("[data-since]").forEach(el => {
            el.textContent = this._elapsed(el.getAttribute("data-since"));
          });
        } catch (e) { this._stopPulse(); }
      }, 1000);
    },

    _stopPulse() {
      if (this._mTimer) { clearInterval(this._mTimer); this._mTimer = null; }
    },

    _elapsed(stamp) {
      const ms = this._bg.stampMs(stamp);
      if (!ms) return "";
      const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
      return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    },

    async _tickMusic() {
      if (this._mBusy) return;
      const live = (this._mJobs || []).some(j => !j.terminal && !j.orphaned);
      if (live) { await this._loadJobs(); return; }
      // Nothing running: still refresh occasionally so a job started elsewhere
      // (an MCP call, another tab) turns up rather than needing a manual reload.
      this._mIdle = (this._mIdle || 0) + 1;
      if (this._mIdle >= 5) { this._mIdle = 0; await this._loadJobs(); }
    },

    _renderJobs() {
      const bg = this._bg;
      const host = this._root && this._root.querySelector("#aud-jobs");
      if (!host) return;
      const jobs = this._mJobs || [];
      // Everything live, plus the last few that finished — a finished run is
      // where the cost and the task id are, and dropping it the instant it
      // lands is how a failure becomes invisible.
      const live = jobs.filter(j => !j.terminal);
      const recent = jobs.filter(j => j.terminal).slice(0, 3);
      const rows = live.concat(recent);
      if (!rows.length) { host.innerHTML = ""; return; }
      host.innerHTML = `<div class="aud-card mus-jobs">
        <h3 class="aud-h">${BGICON("timeline")} Generations
          <span class="aud-sub">${live.length
            ? `${live.length} in flight` : "nothing running"}</span></h3>
        ${rows.map(j => this._jobRow(j)).join("")}
      </div>`;
    },

    _jobRow(j) {
      const bg = this._bg;
      const r = j.result || {};
      const prompt = String(j.prompt || r.prompt || "").slice(0, 90);
      const pct = Math.round(100 * (Number(j.progress) || 0));
      let tone = "run", head = j.stage || j.state, foot = "", actions = "";

      if (j.orphaned) {
        tone = "stale";
        head = "orphaned by a dashboard restart";
        foot = "the thread running this died with the previous dashboard — it "
             + "will never finish. If it had reached Suno the batch was charged "
             + "for and can still be collected.";
        actions = `<button class="aud-btn" data-jact="dismiss" data-job="${j.id}">
            ${BGICON("close")} dismiss</button>`
          + (j.task_id ? `<button class="aud-btn aud-primary" data-jact="recover"
               data-job="${j.id}">${BGICON("export")} recover the takes</button>` : "");
      } else if (!j.terminal) {
        foot = "Suno renders in one to three minutes. The takes download and file "
             + "themselves the moment it finishes — you can leave this tab.";
        actions = `<button class="aud-btn aud-danger" data-jact="cancel" data-job="${j.id}">
            ${BGICON("stop")} cancel</button>`;
      } else if (j.state === "done" && r.ok) {
        tone = "ok";
        head = `${(r.candidates || []).length} take(s) ready to audition`;
        foot = this._cost(r);
        actions = `<button class="aud-btn" data-jact="listen" data-job="${j.id}">
            ${BGICON("audio")} listen</button>
          <button class="aud-btn" data-jact="dismiss" data-job="${j.id}">
            ${BGICON("close")} clear</button>`;
      } else {
        tone = "bad";
        head = r.cancelled ? "cancelled" : "failed";
        foot = r.error || j.error || "no reason recorded";
        actions = (r.task_id ? `<button class="aud-btn aud-primary" data-jact="recover"
              data-job="${j.id}">${BGICON("export")} recover the takes</button>` : "")
          + `<button class="aud-btn" data-jact="dismiss" data-job="${j.id}">
               ${BGICON("close")} clear</button>`;
      }

      return `<div class="mus-job t-${tone}">
        <div class="mus-jtop">
          ${!j.terminal && !j.orphaned ? '<span class="aud-spin"></span>' : ""}
          <b>${bg.esc(head)}</b>
          ${!j.terminal && !j.orphaned
            ? `<span class="mus-clock" data-since="${bg.esc(j.created_at || "")}">${
                bg.esc(this._elapsed(j.created_at))}</span>` : ""}
          <span class="mus-jsp"></span>
          <span class="aud-muted">#${bg.esc(j.id)}</span>
        </div>
        ${!j.terminal && !j.orphaned
          ? `<div class="mus-bar"><i style="width:${pct}%"></i></div>` : ""}
        ${prompt ? `<div class="mus-jprompt">“${bg.esc(prompt)}${
            String(j.prompt || "").length > 90 ? "…" : ""}”</div>` : ""}
        ${foot ? `<div class="mus-jfoot">${bg.esc(foot)}</div>` : ""}
        ${j.task_id ? `<div class="mus-jfoot mono">kie task ${bg.esc(j.task_id)}</div>` : ""}
        <div class="mus-jacts">${actions}</div>
      </div>`;
    },

    async _jobAction(act, id, btn) {
      const bg = this._bg;
      if (!id) return;
      if (act === "listen") {
        const body = this._root && this._root.querySelector("#aud-cands");
        if (body) body.scrollIntoView({ block: "start", behavior: "smooth" });
        return;
      }
      btn.disabled = true;
      try {
        if (act === "cancel") {
          const r = await bg.post(`/api/jobs/${id}/cancel`, {});
          const d = (r && r.data) || {};
          bg.toast(d.stopped ? "cancelled" :
            "cancel requested — it stops at the next poll; Suno was already "
            + "asked, so the batch is charged for and recoverable", !d.stopped);
        } else if (act === "dismiss") {
          const r = await bg.post(`/api/music/jobs/${id}/dismiss`, {});
          const err = this._errText(r);
          if (err) bg.toast(err, true);
        } else if (act === "recover") {
          const job = (this._mJobs || []).find(j => String(j.id) === String(id));
          const task = job && job.task_id;
          if (!task) { bg.toast("this job never reached Suno — nothing to recover", true); return; }
          bg.toast("collecting the takes kie is holding…");
          const r = await bg.post("/api/music/recover", { task_id: task });
          const err = this._errText(r);
          if (err) { bg.toast(err, true); }
          else {
            const d = r.data || {};
            bg.toast(d.count
              ? `recovered ${d.count} take(s) — no new charge`
              : (d.note || "nothing new to recover"));
            await this._loadCandidates();
          }
        }
      } catch (e) {
        bg.toast(act + " failed", true);
      }
      btn.disabled = false;
      await this._loadJobs();
    },

    // UNPRICED IS NOT $0.00. kie publishes no per-model price and the Suno
    // record carries no creditsConsumed, so a run whose cost could not be
    // measured says so — a zero here would read as "free" to anyone budgeting.
    _cost(r) {
      if (r && r.estimated_usd) {
        return `$${Number(r.estimated_usd).toFixed(4)}`
          + (r.accounted ? " — filed to the project ledger" : " — not filed");
      }
      if (r && r.credits_consumed) {
        return `${r.credits_consumed} credits used · no dollar rate set, so `
          + "nothing was filed to the ledger";
      }
      return "cost not recorded — kie publishes no price for Suno";
    },

    _renderCandidates() {
      const bg = this._bg;
      const body = this._root && this._root.querySelector("#aud-cand-body");
      const count = this._root && this._root.querySelector("#aud-cand-count");
      if (!body) return;
      if (count) {
        count.textContent = this._mCands.length
          ? `${this._mCands.length} awaiting a decision`
          : (this._mKept.length ? `${this._mKept.length} kept` : "");
      }
      const card = (c, keptRow) => {
        const cost = c.estimated_usd ? `$${Number(c.estimated_usd).toFixed(4)}`
          : (c.credits_consumed ? `${c.credits_consumed} cr` : "unpriced");
        const secs = c.duration_s ? bg.fmtTime(c.duration_s) : "—";
        const inst = c.install || null;
        return `<div class="aud-take${keptRow ? " kept" : ""}${
            c.installed ? " live" : ""}${this._mNew[c.artifact_id] ? " fresh" : ""}">
          <div class="aud-takehead">
            ${this._mNew[c.artifact_id] ? '<span class="aud-tag new">new</span>' : ""}
            <b>${bg.esc(c.title || c.logical_name)}</b>
            <span class="aud-tag">r${bg.esc(c.revision)}</span>
            <span class="aud-tag">${bg.esc(c.model || "?")}</span>
            <span class="aud-tag">${bg.esc(secs)}</span>
            <span class="aud-tag">${bg.esc(c.instrumental ? "instrumental" : "vocal")}</span>
            <span class="aud-tag" title="${bg.esc(c.credits_source || "")}">${bg.esc(cost)}</span>
            ${keptRow ? `<span class="aud-tag ${c.installed ? "good" : "warnt"}">${
                bg.esc(c.status)}${c.installed ? " · in the game" : ""}</span>` : ""}
          </div>
          ${c.exists
            ? `<audio controls preload="none" src="${bg.esc(c.url)}"></audio>`
            : `<div class="mus-strip bad">${BGICON("stop")}<span>the file is gone
                 from disk — kie only keeps its own copy 14 days; regenerate</span></div>`}
          ${keptRow ? this._stateStrip(c) : ""}
          <div class="aud-takefoot">
            <span class="aud-path" title="${bg.esc(c.path)}">${bg.esc(c.path)}</span>
            ${keptRow ? this._keptActions(c, inst) : `
                 <button class="aud-btn aud-primary" data-mact="keep" data-id="${c.artifact_id}"
                   title="install under the engine project and approve this revision">
                   ${BGICON("select")} keep</button>
                 <button class="aud-btn aud-danger" data-mact="discard" data-id="${c.artifact_id}">
                   ${BGICON("delete")} discard</button>`}
          </div>
          ${c.prompt ? `<div class="aud-takeprompt">${bg.esc(String(c.prompt).slice(0, 400))}</div>` : ""}
        </div>`;
      };
      const pending = this._mCands.map(c => card(c, false)).join("");
      const kept = this._mKept.map(c => card(c, true)).join("");
      body.innerHTML = (pending || kept)
        ? `${pending}${kept ? `<div class="aud-kepth">approved takes</div>${kept}` : ""}`
        : '<div class="aud-empty">no generated tracks yet. Write a prompt above — '
          + 'a request comes back as several takes, you keep one, and keeping is '
          + 'what copies it into the engine project.</div>';
    },

    /* THREE THINGS CAN BE WRONG WITH AN APPROVED TAKE, and each has one button.
     *
     * APPROVED BUT NOT INSTALLED is the state that started all this, and it used
     * to render as a grey sentence reading "approved, but no install was
     * recorded". Every take of every batch lands there on a project whose
     * approval gate is off: artifacts.register approves the revision as it files
     * it, so there is no candidate, no keep, and nothing copies the file into
     * the engine project. The row says approved and the game has nothing.
     *
     * NOT CHOSEN is the sibling take. Offering it "install" would be a trap —
     * the file would go into the game while the APPROVED revision stayed the
     * other one, so the badge and the bytes would disagree. Picking a different
     * take is keep(): install AND approve, in one act, which supersedes the one
     * that was auto-chosen.
     *
     * INSTALLED BUT SUPERSEDED is that trap after somebody has fallen into it
     * (or after a later batch superseded this take). The game loads this file;
     * the approved revision is a different one. Say so, and offer the same fix.
     */
    _stateStrip(c) {
      const bg = this._bg;
      const chosen = c.status === "approved" || c.status === "integrated";
      if (!c.installed) {
        if (!chosen) {
          return `<div class="mus-strip">${BGICON("select")}
            <span><b>Not the chosen take.</b> ${bg.esc(c.status)} — the game is
            loading a different one. Keeping this take installs it
            <em>and</em> makes it the approved revision.</span>
            <button class="aud-btn aud-primary" data-mact="keep" data-id="${c.artifact_id}">
              ${BGICON("select")} use this one instead</button>
          </div>`;
        }
        const where = bg.esc((c.install || {}).path || "");
        const why = c.install_missing
          ? `it was installed at <code>${where}</code>, but that file is no
             longer on disk.`
          : c.install_stale
            ? `<code>${where}</code> holds a DIFFERENT take now — every take of a
               batch installs to the same file, and another one overwrote this.`
            : `this take was approved automatically — this project's approval
               gate is off — so nothing copied it into the engine project.`;
        return `<div class="mus-strip warn">${BGICON("gate")}
          <span><b>Approved, but the game cannot load it.</b> ${why}
          Install it to <code>game/assets/audio/music/</code> and it becomes a
          real project asset the mixer and Godot can both reach.</span>
          <button class="aud-btn aud-primary" data-mact="install" data-id="${c.artifact_id}">
            ${BGICON("export")} install now</button>
        </div>`;
      }
      if (!chosen) {
        return `<div class="mus-strip warn">${BGICON("gate")}
          <span><b>The game loads this take, but it is ${bg.esc(c.status)}.</b>
          The approved revision is a different one, so the badge and the bytes
          disagree. Make this the approved take, or install the approved one from
          its own card.</span>
          <button class="aud-btn aud-primary" data-mact="keep" data-id="${c.artifact_id}">
            ${BGICON("select")} make this the approved take</button>
        </div>`;
      }
      return "";
    },

    // The lab points at the INSTALLED file, never at the .bgate_out take. The
    // lab is an editor: saving over a candidate would change bytes whose hash is
    // already recorded against an immutable revision, and the lab's own file
    // walk cannot see .bgate_out anyway. Edit the thing that ships.
    _keptActions(c, inst) {
      const bg = this._bg;
      if (!c.installed) return "";
      return `<span class="aud-note">in the game at
          <code>${bg.esc(inst.path)}</code> · <code>${bg.esc(inst.godot_res)}</code></span>
        <button class="aud-btn aud-primary" data-mact="lab" data-id="${c.artifact_id}"
          title="trim, loop, layer and mix this track in the audio lab">
          ${BGICON("waveform")} open in audio lab</button>
        <button class="aud-btn" data-mact="reinstall" data-id="${c.artifact_id}"
          title="copy this take over the installed file again">
          ${BGICON("export")} re-install</button>`;
    },

    async _candAction(act, id, btn) {
      const bg = this._bg;
      if (!id || this._mBusy) return;
      if (act === "lab") {
        const row = this._mKept.find(c => c.artifact_id === id);
        const rel = row && row.installed && row.install && row.install.path;
        if (rel) this.openSound(rel);
        else bg.toast("install it first — the lab edits the file in the game, "
                      + "not the scratch take", true);
        return;
      }
      if (act === "install" || act === "reinstall") {
        btn.disabled = true;
        try {
          const r = await bg.post("/api/music/install", { artifact_id: id });
          const err = this._errText(r);
          if (err) { bg.toast(err, true); btn.disabled = false; }
          else {
            const inst = ((r.data || {}).install) || {};
            bg.toast("installed at " + (inst.path || "the project"));
          }
        } catch (e) { bg.toast("install failed", true); btn.disabled = false; }
        await this._loadCandidates();
        this._loadAll();
        return;
      }
      let note = "";
      if (act === "discard") {
        note = await bg.askText({
          title: "Discard this take", label: "what was wrong with it?",
          ok: "discard", placeholder: "e.g. drums come in too early; wrong era",
        });
        if (note == null) return;
      }
      this._mBusy = true;
      btn.disabled = true;
      try {
        const r = await bg.post(`/api/music/${act}`, { artifact_id: id, note });
        const err = this._errText(r);
        if (err) { bg.toast(err, true); btn.disabled = false; }
        else if (act === "keep") {
          const inst = ((r.data || {}).install) || {};
          bg.toast("kept — installed at " + (inst.path || "the project"));
        } else bg.toast("discarded");
        delete this._mNew[id];
      } catch (e) {
        bg.toast(act + " failed", true);
        btn.disabled = false;
      } finally {
        this._mBusy = false;
      }
      this._loadCandidates();
      this._loadAll();
    },

    // The one error envelope (bgate_ui/api.py): {ok:false, error:{message}}.
    _errText(r) {
      if (!r) return "no answer from the dashboard";
      if (r.ok === false) return (r.error && r.error.message) || "request failed";
      return "";
    },

    _renderAgent() {
      const bg = this._bg;
      const body = this._root && this._root.querySelector("#aud-agent-body");
      if (!body) return;

      if (!this._agentItems.length) {
        body.innerHTML = '<div class="aud-empty">no audio work items in the queue.</div>';
        return;
      }

      const picker = `<select class="aud-in" id="aud-item-pick">${this._agentItems.map(i =>
        `<option value="${i.id}"${i.id === this._activeItem ? " selected" : ""}>#${i.id} · ${bg.esc(i.title || "")} [${bg.esc(i.status || "")}]</option>`
      ).join("")}</select>`;

      const item = this._agentItems.find(i => i.id === this._activeItem);
      const isLive = item && item.status === "dispatched";

      const controls = `<span class="aud-actions">
        ${item && item.status === "queued"
          ? `<button class="aud-btn aud-primary" id="aud-dispatch">dispatch</button>` : ""}
        ${isLive ? `<button class="aud-btn" id="aud-steer">steer</button>
          <button class="aud-btn aud-danger" id="aud-stop">stop</button>` : ""}
      </span>`;

      body.innerHTML = `<div class="aud-agent-top">${picker}${controls}</div>
        <div id="aud-feed"><div class="aud-empty">no activity yet</div></div>`;

      const pick = body.querySelector("#aud-item-pick");
      if (pick) pick.onchange = () => { this._activeItem = Number(pick.value); this._renderAgent(); };
      const disp = body.querySelector("#aud-dispatch");
      if (disp) disp.onclick = () => this._dispatch();
      const steer = body.querySelector("#aud-steer");
      if (steer) steer.onclick = () => this._steer();
      const stop = body.querySelector("#aud-stop");
      if (stop) stop.onclick = () => this._stop();

      if (this._activeItem != null) this._loadFeed();
    },

    async _loadFeed() {
      const bg = this._bg;
      const id = this._activeItem;
      if (id == null) return;
      let act;
      try { act = await bg.get(`/api/agent-activity/${id}`); }
      catch (e) { act = { steps: [], running: false, final: null }; }
      const feed = this._root && this._root.querySelector("#aud-feed");
      if (!feed || this._activeItem !== id) return;

      const steps = (act && act.steps) || [];
      if (!steps.length && !(act && act.final)) {
        feed.innerHTML = '<div class="aud-empty">'
          + ((act && act.running) ? "agent running — waiting for first step…" : "no activity recorded.")
          + '</div>';
        return;
      }
      const rowFor = (s) => {
        if (s.kind === "tool") return `<div class="aud-step s-tool"><b>${bg.esc(s.name)}</b> <span>${bg.esc(s.hint || "")}</span></div>`;
        if (s.kind === "result") return `<div class="aud-step s-res">${bg.esc(s.text || "")}</div>`;
        if (s.kind === "steer") return `<div class="aud-step s-steer">steer: ${bg.esc(s.text || "")}</div>`;
        return `<div class="aud-step s-say">${bg.esc(s.text || "")}</div>`;
      };
      let html = steps.map(rowFor).join("");
      if (act && act.final) {
        html += `<div class="aud-final"><b>result (${bg.esc(act.final.subtype || "")})</b> ${bg.esc(act.final.text || "")}</div>`;
      }
      feed.innerHTML = html + `<div class="aud-run">${(act && act.running) ? "● running" : "○ idle"}</div>`;
    },

    async _dispatch() {
      const id = this._activeItem;
      if (id == null) return;
      try {
        const r = await this._bg.post(`/api/queue/${id}/dispatch`, {});
        if (r && (r.ok || r.pid)) { this._bg.toast("dispatched #" + id); }
        else this._bg.toast((r && r.error) || "dispatch failed", true);
      } catch (e) { this._bg.toast("dispatch failed", true); }
      this._loadAll();
    },

    async _steer() {
      const id = this._activeItem;
      if (id == null) return;
      const text = await this._bg.askText({
        title: "Steer the audio agent",
        label: "course correction", ok: "send", required: true });
      if (!text) return;
      try {
        const r = await this._bg.post(`/api/queue/${id}/steer`, { text });
        this._bg.toast((r && r.ok) ? "steer sent" : ((r && r.error) || "steer failed"), !(r && r.ok));
      } catch (e) { this._bg.toast("steer failed", true); }
    },

    async _stop() {
      const id = this._activeItem;
      if (id == null) return;
      try {
        const r = await this._bg.post(`/api/queue/${id}/stop`, {});
        this._bg.toast((r && r.ok) ? "stopped #" + id : ((r && r.error) || "stop failed"), !(r && r.ok));
      } catch (e) { this._bg.toast("stop failed", true); }
      this._loadAll();
    },

    _style() {
      return `<style>
        /* The seat's two halves. Library is sounds + cues + the live agent;
           Lab is AudioLab, mounted here rather than in Studio — see _setMode. */
        .aud-modes{display:flex;gap:6px;margin-bottom:14px}
        .aud-mode{display:flex;align-items:center;gap:6px;padding:5px 12px;background:var(--surface-2);
          border:1px solid var(--line);border-radius:8px;color:var(--text-3);font:inherit;font-size:12px;cursor:pointer}
        .aud-mode:hover{border-color:var(--accent);color:var(--text-2)}
        .aud-mode.on{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}
        .aud-editor{min-height:620px;height:calc(100vh - 300px)}
        .aud-wrap{display:flex;flex-direction:column;gap:14px;color:var(--text);font-size:13px}
        .aud-card{background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-lg);padding:var(--s-6)}
        .aud-h{font-size:13px;font-weight:var(--fw-semi);color:var(--text);display:flex;align-items:center;gap:8px;margin-bottom:12px}
        .aud-sub{font-weight:400;color:var(--text-3);font-size:11px}
        .aud-actions{margin-left:auto;display:flex;gap:6px}
        .aud-btn{padding:var(--s-4) var(--s-5);background:var(--surface-3);border:1px solid var(--line);border-radius:var(--r-sm);color:var(--text);font:inherit;font-size:var(--fs-sm);cursor:pointer;transition:background var(--dur-fast) var(--ease),border-color var(--dur-fast) var(--ease)}
        .aud-btn:hover{border-color:var(--accent);color:var(--text)}
        .aud-primary{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}
        .aud-danger{border-color:var(--bad-line);color:var(--bad)}
        .aud-tbl{width:100%;border-collapse:collapse;font-size:12px}
        .aud-tbl th{text-align:left;color:var(--text-3);font-weight:var(--fw-semi);font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:4px 8px;border-bottom:1px solid var(--line)}
        .aud-tbl td{padding:6px 8px;border-bottom:1px solid var(--line-soft);vertical-align:middle}
        .aud-name{color:var(--text);font-weight:var(--fw-semi);white-space:nowrap}
        .aud-tag{color:var(--text-3);font-size:10px;font-weight:400}
        .aud-path{color:var(--text-3);font-family:ui-monospace,monospace;font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .aud-bytes{color:var(--text-3);white-space:nowrap}
        .aud-play audio,.aud-prev audio{height:30px;max-width:220px;vertical-align:middle}
        .aud-muted{color:var(--text-3)}
        .aud-in{background:var(--bg);border:1px solid var(--line);color:var(--text);border-radius:7px;padding:5px 8px;font:inherit;font-size:12px;width:100%;box-sizing:border-box}
        .aud-in:focus{outline:none;border-color:var(--accent)}
        .aud-del{padding:4px 8px;color:var(--bad);border-color:var(--bad-soft)}
        .aud-empty{color:var(--text-3);padding:10px 2px;line-height:1.5}
        .aud-empty code{background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:1px 5px;font-size:11px}
        .aud-agent-top{display:flex;gap:8px;align-items:center;margin-bottom:10px}
        .aud-agent-top .aud-in{width:auto;flex:1;min-width:0}
        #aud-feed{display:flex;flex-direction:column;gap:5px;max-height:340px;overflow:auto}
        .aud-step{padding:6px 10px;border-radius:8px;font-size:12px;line-height:1.4}
        .s-tool{background:var(--surface-1);border:1px solid var(--line)}
        .s-tool b{color:var(--text)}
        .s-tool span{color:var(--text-3);font-family:ui-monospace,monospace;font-size:11px}
        .s-say{background:var(--surface-1);color:var(--text)}
        .s-res{background:var(--bg);color:var(--text-3);font-family:ui-monospace,monospace;font-size:11px}
        .s-steer{background:var(--info-soft);border:1px solid var(--info-line);color:var(--c-narrative)}
        .aud-final{background:var(--good-soft);border:1px solid var(--good-line);border-radius:8px;padding:8px 10px;color:var(--good);font-size:12px;margin-top:4px}
        .aud-final b{color:var(--good)}
        .aud-run{color:var(--text-3);font-size:11px;padding:2px}

        /* ---- music: compose, jobs, take gallery ---------------------------
           These carry words, so the fills are the OPAQUE ramp (--solid-N),
           never the glass surfaces — see the block comment in app.css. On the
           dark and light themes --solid-N resolves to the same tone the
           surface ramp already was, so only the orbit theme sees a change,
           which is the theme the rule exists for.

           THE PROMPT OUTWEIGHS EVERYTHING. It gets the size, the whole width
           and the only large type on the card; name/model/toggles are one
           quiet line beneath it, and the five fields almost nobody touches are
           behind "more". The previous version gave a 500-character prompt the
           same visual weight as a "negative tags" box, which is how a panel
           ends up feeling like a form instead of a place to write. */
        #aud-music .aud-card{background:var(--solid-2)}
        .mus-prompt{width:100%;box-sizing:border-box;background:var(--solid-1);
          border:1px solid var(--line);border-radius:10px;color:var(--text);
          font:inherit;font-size:15px;line-height:1.55;padding:12px 14px;resize:vertical;
          min-height:78px}
        .mus-prompt::placeholder{color:var(--text-3)}
        .mus-prompt:focus{outline:none;border-color:var(--accent)}
        .mus-meter{display:flex;align-items:baseline;gap:10px;margin:5px 2px 12px;
          font-size:11px;color:var(--text-3)}
        .mus-count{font-variant-numeric:tabular-nums}
        .mus-count.over{color:var(--bad)}
        .mus-hint{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .mus-hint.bad{color:var(--bad)}
        .mus-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
        .mus-row .mus-name{flex:1 1 190px;min-width:0}
        .mus-row .mus-model{flex:0 0 118px}
        .mus-chip{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;
          background:var(--solid-3);border:1px solid var(--line);border-radius:20px;
          font-size:12px;color:var(--text-2);cursor:pointer;white-space:nowrap}
        .mus-chip:hover{border-color:var(--accent)}
        .mus-chip input{margin:0;accent-color:var(--accent)}
        .mus-more.on{background:var(--accent-soft);border-color:var(--accent)}
        .mus-go{padding:7px 16px;font-weight:var(--fw-semi)}
        .mus-adv{display:flex;flex-wrap:wrap;gap:10px 14px;margin-top:12px;padding-top:12px;
          border-top:1px dashed var(--line)}
        /* A class that sets display OUTRANKS the UA's [hidden] rule, so the
           disclosure has to say so itself — without this the advanced fields
           are permanently open and the "more" button does nothing visible. */
        .mus-adv[hidden]{display:none}
        .aud-f{display:flex;flex-direction:column;gap:4px;flex:1 1 200px;min-width:0}
        .aud-f.narrow{flex:0 0 130px}
        .aud-f[hidden]{display:none}
        .aud-fl{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);
          display:flex;gap:8px;align-items:baseline}
        .aud-fl em{font-style:normal;text-transform:none;letter-spacing:0;color:var(--text-3);
          font-variant-numeric:tabular-nums;opacity:.8}

        /* One strip component for every "you should know this" line — the
           unpriced advisory, an approved-but-not-installed track, kie being
           unconfigured. Tone by class, never by inventing a new box. */
        .mus-strip{display:flex;align-items:flex-start;gap:9px;margin-top:12px;
          padding:9px 12px;border-radius:9px;font-size:12px;line-height:1.55;
          background:var(--solid-3);border:1px solid var(--line);color:var(--text-2)}
        .mus-strip svg{flex:none;margin-top:2px}
        .mus-strip span{flex:1;min-width:0}
        .mus-strip b{color:var(--text)}
        .mus-strip code{background:var(--solid-1);border:1px solid var(--line);
          border-radius:4px;padding:1px 5px;font-size:11px;
          font-family:ui-monospace,monospace;word-break:break-all}
        .mus-strip .aud-btn{flex:none;align-self:center}
        .mus-strip.warn{border-color:var(--warn);color:var(--warn)}
        .mus-strip.warn b,.mus-strip.warn code{color:var(--warn)}
        .mus-strip.bad{border-color:var(--bad);color:var(--bad)}
        .mus-strip.bad b{color:var(--bad)}
        .mus-strip.ok{border-color:var(--good);color:var(--good)}
        .mus-strip.ok b{color:var(--good)}

        /* A running generation, said in words. */
        .mus-jobs .mus-job{background:var(--solid-3);border:1px solid var(--line);
          border-radius:10px;padding:9px 12px;margin-bottom:8px}
        .mus-job:last-child{margin-bottom:0}
        .mus-job.t-ok{border-color:var(--good)}
        .mus-job.t-bad{border-color:var(--bad)}
        .mus-job.t-stale{border-color:var(--warn);border-style:dashed}
        .mus-jtop{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--text)}
        .mus-job.t-ok .mus-jtop b{color:var(--good)}
        .mus-job.t-bad .mus-jtop b{color:var(--bad)}
        .mus-job.t-stale .mus-jtop b{color:var(--warn)}
        .mus-jsp{flex:1}
        .mus-clock{font-variant-numeric:tabular-nums;color:var(--text-3);font-size:11px}
        .mus-bar{height:4px;border-radius:3px;background:var(--solid-1);
          border:1px solid var(--line);margin:8px 0 2px;overflow:hidden}
        .mus-bar i{display:block;height:100%;background:var(--accent);
          transition:width .6s var(--ease,ease)}
        .mus-jprompt{margin-top:6px;color:var(--text-2);font-size:12px;line-height:1.5}
        .mus-jfoot{margin-top:4px;color:var(--text-3);font-size:11px;line-height:1.5}
        .mus-jfoot.mono{font-family:ui-monospace,monospace;font-size:10.5px;word-break:break-all}
        .mus-jacts{display:flex;gap:7px;flex-wrap:wrap;margin-top:8px}
        .mus-jacts:empty{display:none}
        .aud-spin{width:11px;height:11px;flex:none;border-radius:50%;
          border:2px solid var(--line);border-top-color:var(--accent);
          animation:aud-sp .8s linear infinite}
        @keyframes aud-sp{to{transform:rotate(360deg)}}
        @media (prefers-reduced-motion:reduce){.aud-spin{animation:none}
          .mus-bar i{transition:none}}

        .aud-note{color:var(--text-3);font-size:11px}
        .aud-note code{background:var(--solid-1);border:1px solid var(--line);border-radius:4px;
          padding:1px 4px;font-size:10px;font-family:ui-monospace,monospace}
        .aud-take{background:var(--solid-3);border:1px solid var(--line);border-radius:10px;
          padding:10px 12px;margin-bottom:9px}
        .aud-take.live{border-color:var(--good)}
        .aud-take.kept:not(.live){border-color:var(--warn)}
        .aud-take.fresh{box-shadow:0 0 0 1px var(--accent) inset}
        .aud-takehead{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-bottom:7px}
        .aud-takehead b{font-size:13px}
        .aud-take .aud-tag{border:1px solid var(--line);border-radius:5px;padding:0 5px;font-size:10px}
        .aud-take .aud-tag.good{color:var(--good);border-color:var(--good)}
        .aud-take .aud-tag.warnt{color:var(--warn);border-color:var(--warn)}
        .aud-take .aud-tag.new{color:var(--accent);border-color:var(--accent);
          text-transform:uppercase;letter-spacing:.05em}
        .aud-take audio{width:100%;max-width:520px;height:34px;display:block}
        .aud-takefoot{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px}
        .aud-takefoot .aud-path{flex:1;min-width:120px;max-width:none}
        .aud-takeprompt{margin-top:7px;color:var(--text-3);font-size:11px;line-height:1.5}
        .aud-kepth{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3);
          margin:14px 0 7px;padding-top:10px;border-top:1px solid var(--line)}
      </style>`;
    },
  };

  window.SeatWS.audio = AUD;
})();
