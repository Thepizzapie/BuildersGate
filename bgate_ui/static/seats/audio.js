/* Audio seat workspace (v1).
 *
 * The seat spec is still open, so this is an honest starter: a sound LIBRARY
 * (list + in-browser playback of the project's audio files), a persisted CUE
 * SHEET mapping game events -> sounds, and a LIVE AGENT panel for the active
 * audio work item. See the note banner in render() for what's deliberately TODO.
 *
 * Contract (see _core.js): window.SeatWS.audio = { label, glyph, render, refresh }.
 * `bg` is window.BGWS. Never touch another seat's DOM; never throw uncaught.
 */
(function () {
  window.SeatWS = window.SeatWS || {};

  const AUD = {
    label: "Audio",
    glyph: "♪",
    _bg: null,
    _root: null,
    _sounds: [],   // [{rel,name,bytes}] from /api/audio/list
    _arts: [],     // audio artifacts pulled from /api/assets/workspace
    _cues: [],     // [{event,sound,note}] persisted at workspace/audio/cues
    _activeItem: null,
    _agentItems: [],

    render(container, bg) {
      this._bg = bg;
      this._root = container;
      container.innerHTML = this._style() + `
        <div class="aud-wrap">
          <div class="aud-note">
            <b>Audio workspace v1</b> — seat spec is open; this covers library +
            playback + cue mapping.
            <span class="aud-todo">TODO: waveform view, generation, in-engine hookup.</span>
          </div>
          <div class="aud-card" id="aud-lib">
            <div class="aud-h">♪ Sound library <span class="aud-sub" id="aud-lib-count"></span></div>
            <div id="aud-lib-body"><div class="aud-empty">loading…</div></div>
          </div>
          <div class="aud-card" id="aud-cues">
            <div class="aud-h">▤ Cue sheet <span class="aud-sub">which sound plays when</span>
              <span class="aud-actions">
                <button class="aud-btn" id="aud-cue-add">+ row</button>
                <button class="aud-btn aud-primary" id="aud-cue-save">save</button>
              </span>
            </div>
            <div id="aud-cue-body"><div class="aud-empty">loading…</div></div>
          </div>
          <div class="aud-card" id="aud-agent">
            <div class="aud-h">◈ Live audio agent</div>
            <div id="aud-agent-body"><div class="aud-empty">loading…</div></div>
          </div>
        </div>`;

      // Wire the two static buttons once.
      const addBtn = container.querySelector("#aud-cue-add");
      const saveBtn = container.querySelector("#aud-cue-save");
      if (addBtn) addBtn.onclick = () => this._addCueRow();
      if (saveBtn) saveBtn.onclick = () => this._saveCues();

      this._loadAll();
    },

    // Called ~every 3s by the shell. Only refresh the (cheap, non-destructive)
    // live-agent panel — never rebuild the library or clobber unsaved cue edits.
    refresh() {
      try {
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
        </tr>`);
      });

      body.innerHTML = `<table class="aud-tbl">
        <thead><tr><th>name</th><th>path</th><th>size</th><th>play</th></tr></thead>
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
      const text = window.prompt("Steer the audio agent — course correction:");
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
        .aud-wrap{display:flex;flex-direction:column;gap:14px;color:#e6e8ee;font-size:13px}
        .aud-note{background:#101319;border:1px solid #1e232c;border-left:3px solid #3b7f9e;border-radius:10px;padding:10px 14px;color:#c7cdd8;line-height:1.5}
        .aud-note b{color:#e6e8ee}
        .aud-todo{color:#7f8b9c;font-style:italic;margin-left:4px}
        .aud-card{background:#101319;border:1px solid #1e232c;border-radius:14px;padding:14px 16px}
        .aud-h{font-size:13px;font-weight:600;color:#e6e8ee;display:flex;align-items:center;gap:8px;margin-bottom:12px}
        .aud-sub{font-weight:400;color:#7f8b9c;font-size:11px}
        .aud-actions{margin-left:auto;display:flex;gap:6px}
        .aud-btn{background:#161b22;border:1px solid #2b323d;color:#c7cdd8;border-radius:8px;padding:5px 11px;font:inherit;font-size:12px;cursor:pointer}
        .aud-btn:hover{border-color:#3b7f9e;color:#e6e8ee}
        .aud-primary{background:#123039;border-color:#3b7f9e;color:#bfe0ee}
        .aud-danger{border-color:#5a2a2a;color:#f0b3b3}
        .aud-tbl{width:100%;border-collapse:collapse;font-size:12px}
        .aud-tbl th{text-align:left;color:#7f8b9c;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:4px 8px;border-bottom:1px solid #1e232c}
        .aud-tbl td{padding:6px 8px;border-bottom:1px solid #171b22;vertical-align:middle}
        .aud-name{color:#e6e8ee;font-weight:500;white-space:nowrap}
        .aud-tag{color:#7f8b9c;font-size:10px;font-weight:400}
        .aud-path{color:#8a93a2;font-family:ui-monospace,monospace;font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .aud-bytes{color:#7f8b9c;white-space:nowrap}
        .aud-play audio,.aud-prev audio{height:30px;max-width:220px;vertical-align:middle}
        .aud-muted{color:#5a6472}
        .aud-in{background:#0c0f14;border:1px solid #2b323d;color:#e6e8ee;border-radius:7px;padding:5px 8px;font:inherit;font-size:12px;width:100%;box-sizing:border-box}
        .aud-in:focus{outline:none;border-color:#3b7f9e}
        .aud-del{padding:4px 8px;color:#f0b3b3;border-color:#3a2626}
        .aud-empty{color:#7f8b9c;padding:10px 2px;line-height:1.5}
        .aud-empty code{background:#0c0f14;border:1px solid #1e232c;border-radius:5px;padding:1px 5px;font-size:11px}
        .aud-agent-top{display:flex;gap:8px;align-items:center;margin-bottom:10px}
        .aud-agent-top .aud-in{width:auto;flex:1;min-width:0}
        #aud-feed{display:flex;flex-direction:column;gap:5px;max-height:340px;overflow:auto}
        .aud-step{padding:6px 10px;border-radius:8px;font-size:12px;line-height:1.4}
        .s-tool{background:#0f171c;border:1px solid #1e2a30}
        .s-tool b{color:#7fc4dd}
        .s-tool span{color:#8a93a2;font-family:ui-monospace,monospace;font-size:11px}
        .s-say{background:#12141a;color:#c7cdd8}
        .s-res{background:#0d1016;color:#8a93a2;font-family:ui-monospace,monospace;font-size:11px}
        .s-steer{background:#1a1526;border:1px solid #3a2f52;color:#c9b8ea}
        .aud-final{background:#0f1a14;border:1px solid #274a34;border-radius:8px;padding:8px 10px;color:#bfe6cf;font-size:12px;margin-top:4px}
        .aud-final b{color:#8fd6a8}
        .aud-run{color:#7f8b9c;font-size:11px;padding:2px}
      </style>`;
    },
  };

  window.SeatWS.audio = AUD;
})();
