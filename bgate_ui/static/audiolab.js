/* audiolab.js — the audio editor, the mixer, and a synth for making SFX from nothing.
 *
 * Sound was the one asset class this tool could only LIST. You could hear a
 * clip and you could queue a task about it, and that was the entire vocabulary
 * — every actual change meant leaving for another program and coming back with
 * a file. Three things follow from closing that gap, and they are the three
 * halves of this module:
 *
 *   EDIT. Trim the silence off a hit, fade a tail, normalise a clip that came
 *   back too quiet, reverse it, slow it down. All small, all constant, all
 *   currently a round trip through a DAW.
 *
 *   EXTEND + MIX. A 30-second loop that needs to be 90, a hit that needs a
 *   layer of noise under it. The mixer is N tracks with offset/gain/pan
 *   rendered offline into one buffer, and the session is saved next to the
 *   file so a mixdown stays a document rather than a one-shot.
 *
 *   CREATE. The synth makes a sound effect out of nothing — waveform, pitch
 *   sweep, envelope, noise, bitcrush. That is how retro SFX are actually made,
 *   it costs no money and no API, and it means "we need a confirm blip" is
 *   thirty seconds of work instead of a generation request.
 *
 * THE DSP LIVES HERE, deliberately. WebAudio already decodes ogg/wav/mp3,
 * resamples and renders offline; shipping PCM to a server to do the same work
 * would be slower and worse. The server's job is the parts a browser cannot
 * reach: what a file is before it is decoded, bytes on disk with a backup, and
 * the Godot loop settings that live in a .import sidecar the engine owns.
 *
 * GODOT LOOP POINTS get their own section because they are invisible. A music
 * track whose .import says loop=false plays once and stops, and nothing about
 * the audio reveals it — you find out in the game. Both music tracks in the
 * project this was built against shipped that way.
 */
window.AudioLab = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const clamp = (v, lo, hi) => v < lo ? lo : v > hi ? hi : v;
  const fmt = s => {
    if (s == null || !isFinite(s)) return "—";
    const m = Math.floor(s / 60), r = s - m * 60;
    return m ? `${m}:${r.toFixed(2).padStart(5, "0")}` : `${r.toFixed(3)}s`;
  };

  // An AudioBuffer is ~170 KB per second per channel at 44.1k float32, so the
  // history is capped by BYTES like the sprite editor's. Eight edits on a
  // 45-second stereo track is already 120 MB.
  const UNDO_BYTES = 320 * 1024 * 1024;

  const WAVES = ["sine", "square", "saw", "triangle", "noise"];

  let S = null, $ = {};

  /* ── styles ───────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("audiolab-style")) return;
    const s = document.createElement("style");
    s.id = "audiolab-style";
    s.textContent = [
      ".ab-back{position:fixed;inset:0;z-index:1400;background:rgba(4,5,7,.9);backdrop-filter:blur(3px);display:flex;flex-direction:column}",
      ".ab-bar{display:flex;align-items:center;gap:9px;padding:9px 14px;border-bottom:1px solid var(--seam);background:var(--iron);flex:none}",
      ".ab-title{font-family:var(--mono);font-size:11.5px;letter-spacing:.1em;color:var(--bone);text-transform:uppercase}",
      ".ab-sub{font-family:var(--mono);font-size:10px;color:var(--ash2)}",
      ".ab-dirty{color:var(--warn)}",
      ".ab-spacer{flex:1}",
      ".ab-btn{padding:6px 11px;background:var(--plate);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;cursor:pointer}",
      ".ab-btn:hover:not(:disabled){border-color:var(--ember)}",
      ".ab-btn:disabled{opacity:.4;cursor:default}",
      ".ab-btn.go{background:var(--ember);color:#111;border-color:var(--ember);font-weight:600}",
      ".ab-btn.wide{width:100%;margin-bottom:6px}",
      ".ab-body{flex:1;display:flex;min-height:0}",
      ".ab-main{flex:1;min-width:0;display:flex;flex-direction:column;background:#0b0c0f}",
      ".ab-modes{display:flex;gap:6px;padding:8px 12px;border-bottom:1px solid var(--seam);background:var(--iron);flex:none}",
      ".ab-mode{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;padding:5px 12px;border:1px solid var(--seam);border-radius:999px;background:none;color:var(--ash);cursor:pointer}",
      ".ab-mode:hover{border-color:var(--ember);color:var(--bone)}",
      ".ab-mode.active{background:var(--plate);border-color:var(--ember);color:var(--bone)}",
      ".ab-studio{flex:1;min-height:0;display:flex}",
      ".ab-studio>*{flex:1;min-width:0}",
      ".ab-wave{flex:1;position:relative;min-height:0}",
      ".ab-wave canvas{position:absolute;inset:0;width:100%;height:100%;cursor:text;touch-action:none}",
      ".ab-hud{position:absolute;left:10px;bottom:8px;font-family:var(--mono);font-size:10px;color:var(--ash2);background:rgba(10,11,14,.82);border:1px solid var(--seam);border-radius:6px;padding:3px 8px;pointer-events:none;white-space:pre}",
      ".ab-transport{display:flex;align-items:center;gap:8px;padding:8px 12px;border-top:1px solid var(--seam);background:var(--iron);flex:none;flex-wrap:wrap}",
      ".ab-side{width:302px;flex:none;background:var(--iron);border-left:1px solid var(--seam);overflow-y:auto;padding:12px}",
      ".ab-h{font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--ash2);margin:16px 0 7px;display:flex;align-items:center;gap:7px}",
      ".ab-h:first-child{margin-top:0}",
      ".ab-h span{flex:1;height:1px;background:var(--seam)}",
      ".ab-row{display:flex;align-items:center;gap:7px;margin-bottom:6px}",
      ".ab-row label{font-family:var(--mono);font-size:10px;color:var(--ash2);flex:none;min-width:46px}",
      ".ab-in{flex:1;min-width:0;background:var(--void);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font:inherit;font-size:11.5px;padding:5px 7px}",
      ".ab-in:focus{outline:none;border-color:var(--ember)}",
      ".ab-in.num{flex:none;width:70px}",
      ".ab-grid2{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:6px}",
      ".ab-grid2 .ab-btn{width:100%}",
      ".ab-note{font-size:11px;color:var(--ash);line-height:1.5;margin-bottom:8px}",
      ".ab-note b{color:var(--bone)}",
      ".ab-warn{color:var(--warn)}",
      ".ab-track{border:1px solid var(--seam);border-radius:8px;padding:7px;margin-bottom:6px;background:var(--void)}",
      ".ab-track .hd{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;color:var(--bone);margin-bottom:5px}",
      ".ab-track .hd .x{margin-left:auto;cursor:pointer;color:var(--ash2)}",
      ".ab-track .hd .x:hover{color:var(--bad)}",
      ".ab-track .mini{display:flex;gap:4px;margin-top:5px}",
      ".ab-tg{font-family:var(--mono);font-size:9px;padding:2px 7px;border:1px solid var(--seam);border-radius:999px;background:none;color:var(--ash2);cursor:pointer}",
      ".ab-tg.on{border-color:var(--ember);color:var(--bone);background:var(--plate)}",
      ".ab-pick{position:fixed;inset:0;z-index:1401;background:rgba(4,5,7,.92);display:flex;align-items:center;justify-content:center;padding:40px}",
      ".ab-pbox{background:var(--iron);border:1px solid var(--seam);border-radius:12px;width:min(720px,100%);max-height:100%;display:flex;flex-direction:column;overflow:hidden}",
      ".ab-plist{overflow-y:auto;padding:8px}",
      ".ab-pi{display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:7px;cursor:pointer;font-family:var(--mono);font-size:11px;color:var(--bone)}",
      ".ab-pi:hover{background:var(--plate)}",
      ".ab-pi .m{margin-left:auto;color:var(--ash2);font-size:10px}",
      ".ab-tag{font-size:9px;padding:1px 6px;border-radius:999px;border:1px solid var(--seam);color:var(--ash2)}",
      ".ab-tag.on{border-color:var(--good);color:var(--good)}",
      ".ab-tag.loop{border-color:#3f5a7d;color:#7fb3ff}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── open / close ─────────────────────────────────────────────────────── */
  async function open(rel){
    injectStyle();
    if (S && S.dirty && !confirm("Discard unsaved audio edits?")) return;
    if (S) close(true);
    if (!rel) return pick();

    const meta = await readJSON(`/api/audio/lab/open?rel=${encodeURIComponent(rel)}`, null);
    if (!meta || meta.__error){ say((meta && meta.__error) || "could not open that sound"); return; }

    // Pin the context to the FILE's sample rate, not the sound card's. A
    // default AudioContext runs at the device rate (48k on most machines), and
    // decodeAudioData resamples into it — so opening a 44.1k asset and saving
    // it straight back would silently rewrite it at 48k, larger and resampled,
    // for no reason anyone asked for. The rate comes from the server probe,
    // which read the real header.
    const wanted = meta.info && meta.info.sample_rate;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    let ctx;
    try { ctx = wanted ? new Ctx({ sampleRate: wanted }) : new Ctx(); }
    catch (e){ ctx = new Ctx(); }
    let buf;
    try {
      const bytes = await fetch(`/api/audio/file?rel=${encodeURIComponent(rel)}`)
        .then(r => r.arrayBuffer());
      buf = await ctx.decodeAudioData(bytes);
    } catch (e){
      say("the browser could not decode that audio");
      try { ctx.close(); } catch (x) {}
      return;
    }
    if (wanted && buf.sampleRate !== wanted){
      say(`this browser decoded at ${buf.sampleRate}Hz, not the file's ${wanted}Hz `
          + "— saving will resample");
    }

    S = {
      rel, meta, ctx, buf,
      mtime: meta.mtime,
      sel: null,                 // {a, b} in samples, a < b
      view: { from: 0, to: buf.length },
      undo: [], redo: [], undoBytes: 0,
      dirty: false,
      play: null, playFrom: 0, playStart: 0,
      loop: Object.assign({}, meta.loop),
      tracks: (meta.session && meta.session.tracks || []).slice(),
      status: null, mode: "clip", studioMounted: false,
      synth: { wave:"square", freq:440, sweep:-40, seconds:0.22,
               attack:0.005, decay:0.08, sustain:0.25, release:0.09,
               noise:0, crush:0, gain:0.8 },
      saveAs: rel,
    };
    mount();
    labStatus();
    paint();
  }

  function close(silent){
    if (S && S.dirty && !silent &&
        !confirm("You have unsaved audio edits. Close anyway?")) return;
    stop();
    if (window.BeatMaker) try { BeatMaker.unmount(); } catch (e) {}
    if (S){
      try { S.ctx.close(); } catch (e) {}
      if (S.ro) try { S.ro.disconnect(); } catch (e) {}
      if (S.onResize) window.removeEventListener("resize", S.onResize);
    }
    const back = document.getElementById("ab-back");
    if (back) back.remove();
    document.removeEventListener("keydown", onKey, true);
    S = null; $ = {};
  }

  async function labStatus(){
    const d = await readJSON("/api/audio/lab/status", null);
    if (!S) return;
    S.status = d && !d.__error ? d : null;
    renderSide();
  }

  /* ── picker ───────────────────────────────────────────────────────────── */
  async function pick(q){
    injectStyle();
    let host = document.getElementById("ab-pick");
    if (!host){
      host = document.createElement("div");
      host.className = "ab-pick"; host.id = "ab-pick";
      host.innerHTML = `<div class="ab-pbox">
        <div class="ab-bar"><span class="ab-title">open a sound</span>
          <input class="ab-in" id="ab-pq" placeholder="filter by path…"
                 style="flex:1;max-width:260px" oninput="AudioLab.pickSearch(this.value)">
          <span class="ab-sub" id="ab-pn"></span>
          <span class="ab-spacer"></span>
          <button class="ab-btn" onclick="AudioLab.newSound()">new from synth</button>
          <button class="ab-btn" onclick="AudioLab.closePick()">close</button></div>
        <div class="ab-plist" id="ab-plist">
          <div class="ab-note" style="padding:22px;text-align:center">scanning…</div></div>
      </div>`;
      document.body.appendChild(host);
      host.addEventListener("click", ev => { if (ev.target === host) closePick(); });
      const i = host.querySelector("#ab-pq"); if (i) i.focus();
    }
    const d = await readJSON(
      `/api/audio/lab/list${q ? `?q=${encodeURIComponent(q)}` : ""}`, {sounds:[]});
    const sounds = d.sounds || [];
    const n = document.getElementById("ab-pn");
    if (n) n.textContent = d.truncated ? `${sounds.length} of ${d.total}`
                                       : `${sounds.length} sound${sounds.length===1?"":"s"}`;
    const list = document.getElementById("ab-plist");
    if (list) list.innerHTML = sounds.length ? sounds.map(s => `
      <div class="ab-pi" onclick="AudioLab.closePick();AudioLab.open('${E(s.rel)}')">
        <span>${E(s.name)}</span>
        ${s.loops ? `<span class="ab-tag loop">loops</span>` : ""}
        ${s.has_session ? `<span class="ab-tag on">mix</span>` : ""}
        <span class="m">${s.seconds != null ? fmt(s.seconds) : "?"} ·
          ${s.sample_rate || "?"}Hz ${s.channels === 2 ? "stereo" : "mono"} · ${E(s.rel)}</span>
      </div>`).join("")
      : `<div class="ab-note" style="padding:22px;text-align:center">no audio matches</div>`;
  }
  let pickTimer = null;
  function pickSearch(v){
    clearTimeout(pickTimer);
    pickTimer = setTimeout(() => pick(String(v || "").trim()), 200);
  }
  function closePick(){ const p = document.getElementById("ab-pick"); if (p) p.remove(); }

  /* A sound that does not exist yet. Opens the editor on a second of silence
     with the synth panel ready — "we need a confirm blip" starts here. */
  function newSound(){
    closePick();
    injectStyle();
    if (S) close(true);
    // 44.1k for a new sound: it is what every other asset in these projects
    // is, and a sheet of SFX at mismatched rates is a mixdown that resamples.
    const Ctx = window.AudioContext || window.webkitAudioContext;
    let ctx;
    try { ctx = new Ctx({ sampleRate: 44100 }); } catch (e){ ctx = new Ctx(); }
    const buf = ctx.createBuffer(1, Math.round(ctx.sampleRate * 0.5), ctx.sampleRate);
    S = {
      rel: null, meta: null, ctx, buf, mtime: null, sel: null,
      view: { from: 0, to: buf.length },
      undo: [], redo: [], undoBytes: 0, dirty: true,
      play: null, playFrom: 0, playStart: 0,
      loop: { supported:false, enabled:false, mode:"forward", begin_s:0, end_s:null },
      tracks: [], status: null, mode: "clip", studioMounted: false,
      synth: { wave:"square", freq:440, sweep:-40, seconds:0.22,
               attack:0.005, decay:0.08, sustain:0.25, release:0.09,
               noise:0, crush:0, gain:0.8 },
      saveAs: "game/assets/audio/sfx_new.wav",
    };
    mount();
    labStatus();
    paint();
    say("synthesise something, then save it under a name", "ok");
  }

  /* ── DOM ──────────────────────────────────────────────────────────────── */
  function mount(){
    const back = document.createElement("div");
    back.className = "ab-back"; back.id = "ab-back";
    back.innerHTML = `
      <div class="ab-bar">
        <span class="ab-title">audio lab</span>
        <span class="ab-sub" id="ab-name"></span>
        <span class="ab-spacer"></span>
        <button class="ab-btn" id="ab-undo" onclick="AudioLab.undo()" title="Undo (Ctrl+Z)">↶</button>
        <button class="ab-btn" id="ab-redo" onclick="AudioLab.redo()" title="Redo">↷</button>
        <button class="ab-btn" onclick="AudioLab.pick()">open…</button>
        <button class="ab-btn go" id="ab-save" onclick="AudioLab.save()">save</button>
        <button class="ab-btn" onclick="AudioLab.close()">close</button>
      </div>
      <div class="ab-body">
        <div class="ab-main">
          <div class="ab-modes" id="ab-modes">
            <button class="ab-mode active" data-m="clip" onclick="AudioLab.setMode('clip')">clip</button>
            <button class="ab-mode" data-m="studio" onclick="AudioLab.setMode('studio')">studio · beat maker</button>
          </div>
          <div class="ab-studio" id="ab-studio" hidden></div>
          <div class="ab-wave" id="ab-wave"><canvas id="ab-canvas"></canvas>
            <div class="ab-hud" id="ab-hud"></div></div>
          <div class="ab-transport">
            <button class="ab-btn go" id="ab-play" onclick="AudioLab.togglePlay()">▶ play</button>
            <button class="ab-btn" onclick="AudioLab.stop()">■</button>
            <button class="ab-btn" onclick="AudioLab.selectAll()">select all</button>
            <button class="ab-btn" onclick="AudioLab.clearSel()">clear selection</button>
            <button class="ab-btn" onclick="AudioLab.zoomSel()">zoom to selection</button>
            <button class="ab-btn" onclick="AudioLab.zoomFit()">fit</button>
            <span class="ab-sub" id="ab-selinfo"></span>
          </div>
        </div>
        <div class="ab-side" id="ab-side"></div>
      </div>`;
    document.body.appendChild(back);
    $ = { back, name: back.querySelector("#ab-name"),
          wave: back.querySelector("#ab-wave"), canvas: back.querySelector("#ab-canvas"),
          hud: back.querySelector("#ab-hud"), side: back.querySelector("#ab-side"),
          selinfo: back.querySelector("#ab-selinfo"), play: back.querySelector("#ab-play") };
    $.ctx2d = $.canvas.getContext("2d");
    bindWave();
    document.addEventListener("keydown", onKey, true);
    if (window.ResizeObserver){
      S.ro = new ResizeObserver(() => paint());
      S.ro.observe($.wave);
    }
    S.onResize = () => paint();
    window.addEventListener("resize", S.onResize);
    sizeCanvas();
    renderSide();
    requestAnimationFrame(() => paint());
  }

  /* Returns true when the backing store actually changed.
   *
   * Called from the paint path as well as the ResizeObserver, because the
   * observer is not a guarantee: a pane that is laid out while hidden never
   * fires one, and the panel then opens on a 1x1 canvas and looks broken. A
   * two-integer comparison per frame is a cheap way to never depend on it. */
  function sizeCanvas(){
    if (!S || !$.canvas) return false;
    const r = $.wave.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(r.width * dpr));
    const h = Math.max(1, Math.round(r.height * dpr));
    S.dpr = dpr;
    if ($.canvas.width === w && $.canvas.height === h) return false;
    $.canvas.width = w; $.canvas.height = h;
    return true;
  }

  /* ── waveform ─────────────────────────────────────────────────────────── */
  function peaks(channel, from, to, width){
    const data = S.buf.getChannelData(channel);
    const step = (to - from) / width;
    const out = new Float32Array(width * 2);
    for (let x = 0; x < width; x++){
      const s = Math.floor(from + x * step);
      const e = Math.min(S.buf.length, Math.max(s + 1, Math.floor(from + (x + 1) * step)));
      let lo = 1, hi = -1;
      // A 45-second track is 2M samples over ~1200 columns; scanning every
      // sample per column is 2M reads per repaint, which is fine, but a stride
      // keeps a zoomed-out 10-minute file interactive too.
      const stride = Math.max(1, Math.floor((e - s) / 512));
      for (let i = s; i < e; i += stride){
        const v = data[i];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
      if (lo > hi){ lo = 0; hi = 0; }
      out[x * 2] = lo; out[x * 2 + 1] = hi;
    }
    return out;
  }

  /* Coalesce repaints to one per frame, WITHOUT latching. requestAnimationFrame
   * does not fire while the page is not compositing (hidden pane, background
   * tab); a plain `if (pending) return` guard then stays true forever and the
   * canvas is frozen for the rest of the session. The timeout is the escape
   * hatch — whichever fires first does the work and clears the flag. */
  function paint(){
    if (!S || !$.ctx2d) return;
    if (S._pending) return;
    S._pending = true;
    const run = () => { if (!S || !S._pending) return; S._pending = false; _paint(); };
    requestAnimationFrame(run);
    setTimeout(run, 120);
  }

  function _paint(){
    if (!S) return;
    sizeCanvas();
    const c = $.ctx2d, dpr = S.dpr || 1;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = $.canvas.width / dpr, H = $.canvas.height / dpr;
    c.clearRect(0, 0, W, H);
    c.fillStyle = "#0b0c0f"; c.fillRect(0, 0, W, H);

    const ch = S.buf.numberOfChannels;
    const laneH = H / ch;
    const { from, to } = S.view;
    const width = Math.max(1, Math.floor(W));

    // selection band, under the waveform
    if (S.sel){
      const x0 = sampleToX(S.sel.a, W), x1 = sampleToX(S.sel.b, W);
      c.fillStyle = "rgba(255,106,61,.16)";
      c.fillRect(Math.min(x0, x1), 0, Math.abs(x1 - x0), H);
    }

    for (let k = 0; k < ch; k++){
      const top = k * laneH, mid = top + laneH / 2;
      c.strokeStyle = "rgba(232,226,216,.14)";
      c.beginPath(); c.moveTo(0, mid); c.lineTo(W, mid); c.stroke();
      const p = peaks(k, from, to, width);
      c.strokeStyle = "#ff8a5c";
      c.beginPath();
      for (let x = 0; x < width; x++){
        const lo = p[x * 2], hi = p[x * 2 + 1];
        const y0 = mid - hi * (laneH / 2 - 3);
        const y1 = mid - lo * (laneH / 2 - 3);
        c.moveTo(x + .5, y0); c.lineTo(x + .5, Math.max(y1, y0 + .6));
      }
      c.stroke();
      if (k){
        c.strokeStyle = "rgba(232,226,216,.18)";
        c.beginPath(); c.moveTo(0, top + .5); c.lineTo(W, top + .5); c.stroke();
      }
    }

    // Godot loop markers — the setting you cannot hear, drawn where you can see it.
    if (S.loop && S.loop.enabled){
      const rate = S.buf.sampleRate;
      const b = sampleToX(S.loop.begin_s * rate, W);
      c.strokeStyle = "#7fb3ff"; c.lineWidth = 1.5;
      c.beginPath(); c.moveTo(b, 0); c.lineTo(b, H); c.stroke();
      c.fillStyle = "#7fb3ff"; c.font = "10px ui-monospace,monospace";
      c.fillText("loop", b + 4, 12);
      if (S.loop.end_s != null){
        const e = sampleToX(S.loop.end_s * rate, W);
        c.beginPath(); c.moveTo(e, 0); c.lineTo(e, H); c.stroke();
        c.fillText("end", e + 4, 12);
      }
      c.lineWidth = 1;
    }

    if (S.play){
      const t = S.playFrom + (S.ctx.currentTime - S.playStart) * S.buf.sampleRate;
      const x = sampleToX(t, W);
      c.strokeStyle = "#e8e2d8";
      c.beginPath(); c.moveTo(x, 0); c.lineTo(x, H); c.stroke();
      paint();                                  // keep the playhead moving
    }

    const rate = S.buf.sampleRate;
    $.hud.textContent = [
      `${fmt(S.buf.length / rate)}  ·  ${rate}Hz  ·  ${ch === 2 ? "stereo" : "mono"}`,
      `view ${fmt(from / rate)}–${fmt(to / rate)}`,
    ].join("   ");
    if ($.selinfo){
      $.selinfo.textContent = S.sel
        ? `selection ${fmt(S.sel.a / rate)} → ${fmt(S.sel.b / rate)}  (${fmt((S.sel.b - S.sel.a) / rate)})`
        : "no selection — edits apply to the whole clip";
    }
    refreshHistory();
  }

  function sampleToX(sample, W){
    const { from, to } = S.view;
    return ((sample - from) / Math.max(1, to - from)) * W;
  }
  function xToSample(x){
    const W = $.canvas.width / (S.dpr || 1);
    const { from, to } = S.view;
    return clamp(Math.round(from + (x / W) * (to - from)), 0, S.buf.length);
  }

  function bindWave(){
    const el = $.canvas;
    el.addEventListener("pointerdown", ev => {
      if (!S) return;
      el.setPointerCapture(ev.pointerId);
      const r = el.getBoundingClientRect();
      const a = xToSample(ev.clientX - r.left);
      S.drag = { a };
      S.sel = null;
      paint();
    });
    el.addEventListener("pointermove", ev => {
      if (!S || !S.drag) return;
      const r = el.getBoundingClientRect();
      const b = xToSample(ev.clientX - r.left);
      S.sel = b === S.drag.a ? null
            : { a: Math.min(S.drag.a, b), b: Math.max(S.drag.a, b) };
      paint();
    });
    const end = () => { if (S){ S.drag = null; renderSide(); paint(); } };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    el.addEventListener("wheel", ev => {
      if (!S) return;
      ev.preventDefault();
      const r = el.getBoundingClientRect();
      const at = xToSample(ev.clientX - r.left);
      const span = S.view.to - S.view.from;
      const next = clamp(span * (ev.deltaY < 0 ? 1/1.25 : 1.25), 64, S.buf.length);
      const frac = (at - S.view.from) / span;
      let from = Math.round(at - frac * next);
      from = clamp(from, 0, Math.max(0, S.buf.length - next));
      S.view = { from, to: Math.min(S.buf.length, from + next) };
      paint();
    }, { passive: false });
  }

  function onKey(ev){
    if (!S) return;
    const t = ev.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)){
      if (ev.key === "Escape") t.blur();
      return;
    }
    const k = ev.key.toLowerCase();
    if (ev.key === "Escape"){ ev.preventDefault(); close(); return; }
    if (ev.key === " "){ ev.preventDefault(); togglePlay(); return; }
    if ((ev.ctrlKey || ev.metaKey) && k === "z"){
      ev.preventDefault(); ev.shiftKey ? redo() : undo(); return; }
    if ((ev.ctrlKey || ev.metaKey) && k === "s"){ ev.preventDefault(); save(); return; }
    if ((ev.ctrlKey || ev.metaKey) && k === "a"){ ev.preventDefault(); selectAll(); return; }
    if (ev.key === "Delete" || ev.key === "Backspace"){ ev.preventDefault(); cut(); return; }
  }

  /* ── playback ─────────────────────────────────────────────────────────── */
  function togglePlay(){ S && (S.play ? stop() : play()); }
  function play(){
    if (!S) return;
    stop();
    try { S.ctx.resume(); } catch (e) {}
    const src = S.ctx.createBufferSource();
    src.buffer = S.buf;
    src.connect(S.ctx.destination);
    const from = S.sel ? S.sel.a : 0;
    const dur = S.sel ? (S.sel.b - S.sel.a) / S.buf.sampleRate : undefined;
    src.onended = () => { if (S && S.play === src){ S.play = null; syncPlay(); paint(); } };
    src.start(0, from / S.buf.sampleRate, dur);
    S.play = src; S.playFrom = from; S.playStart = S.ctx.currentTime;
    syncPlay(); paint();
  }
  function stop(){
    if (!S || !S.play) return;
    try { S.play.onended = null; S.play.stop(); } catch (e) {}
    S.play = null; syncPlay(); paint();
  }
  function syncPlay(){ if ($.play) $.play.textContent = S && S.play ? "❚❚ pause" : "▶ play"; }

  /* ── history ──────────────────────────────────────────────────────────── */
  function bufBytes(b){ return b.length * b.numberOfChannels * 4; }
  function snapshot(){
    if (!S) return;
    S.undo.push(S.buf);
    S.undoBytes += bufBytes(S.buf);
    while (S.undoBytes > UNDO_BYTES && S.undo.length > 1)
      S.undoBytes -= bufBytes(S.undo.shift());
    S.redo.length = 0;
  }
  function commit(buf, label){
    stop();
    S.buf = buf;
    S.dirty = true;
    S.sel = null;
    S.view = { from: 0, to: buf.length };
    renderSide(); paint();
    if (label) say(label, "ok");
  }
  function undo(){
    if (!S || !S.undo.length) return;
    stop();
    S.redo.push(S.buf);
    S.buf = S.undo.pop();
    S.undoBytes -= bufBytes(S.buf);
    S.dirty = true; S.sel = null;
    S.view = { from: 0, to: S.buf.length };
    renderSide(); paint();
  }
  function redo(){
    if (!S || !S.redo.length) return;
    stop();
    S.undo.push(S.buf);
    S.undoBytes += bufBytes(S.buf);
    S.buf = S.redo.pop();
    S.dirty = true; S.sel = null;
    S.view = { from: 0, to: S.buf.length };
    renderSide(); paint();
  }
  function refreshHistory(){
    const u = document.getElementById("ab-undo"), r = document.getElementById("ab-redo");
    if (u) u.disabled = !S.undo.length;
    if (r) r.disabled = !S.redo.length;
    if ($.name) $.name.innerHTML =
      `${E(S.rel || S.saveAs + " (new)")}${S.dirty ? ' <span class="ab-dirty">● unsaved</span>' : ""}`;
  }

  /* ── edits ────────────────────────────────────────────────────────────── */
  function range(){
    return S.sel ? [S.sel.a, S.sel.b] : [0, S.buf.length];
  }
  function make(length, channels, rate){
    return S.ctx.createBuffer(channels || S.buf.numberOfChannels,
                              Math.max(1, Math.round(length)),
                              rate || S.buf.sampleRate);
  }
  function eachChannel(target, fn){
    for (let c = 0; c < target.numberOfChannels; c++)
      fn(target.getChannelData(c), c);
  }

  function trim(){
    const [a, b] = range();
    if (!S.sel){ say("select the part to keep first"); return; }
    snapshot();
    const out = make(b - a);
    eachChannel(out, (d, c) => d.set(S.buf.getChannelData(c).subarray(a, b)));
    commit(out, `trimmed to ${fmt((b - a) / S.buf.sampleRate)}`);
  }

  function cut(){
    if (!S.sel){ say("select the part to delete first"); return; }
    const [a, b] = range();
    if (b - a >= S.buf.length){ say("that would delete everything"); return; }
    snapshot();
    const out = make(S.buf.length - (b - a));
    eachChannel(out, (d, c) => {
      const src = S.buf.getChannelData(c);
      d.set(src.subarray(0, a), 0);
      d.set(src.subarray(b), a);
    });
    commit(out, `removed ${fmt((b - a) / S.buf.sampleRate)}`);
  }

  function silence(){
    const [a, b] = range();
    snapshot();
    const out = copyBuf();
    eachChannel(out, d => d.fill(0, a, b));
    commit(out, "silenced");
  }

  function copyBuf(){
    const out = make(S.buf.length);
    eachChannel(out, (d, c) => d.set(S.buf.getChannelData(c)));
    return out;
  }

  function fade(dir){
    const [a, b] = range();
    const n = b - a;
    if (n < 2){ say("select something to fade"); return; }
    snapshot();
    const out = copyBuf();
    eachChannel(out, d => {
      for (let i = 0; i < n; i++){
        const t = i / (n - 1);
        d[a + i] *= dir === "in" ? t : 1 - t;
      }
    });
    commit(out, `faded ${dir}`);
  }

  function gain(db){
    const [a, b] = range();
    const f = Math.pow(10, db / 20);
    snapshot();
    const out = copyBuf();
    eachChannel(out, d => {
      for (let i = a; i < b; i++) d[i] = clamp(d[i] * f, -1, 1);
    });
    commit(out, `${db > 0 ? "+" : ""}${db} dB`);
  }

  function normalize(){
    const [a, b] = range();
    let peak = 0;
    for (let c = 0; c < S.buf.numberOfChannels; c++){
      const d = S.buf.getChannelData(c);
      for (let i = a; i < b; i++){ const v = Math.abs(d[i]); if (v > peak) peak = v; }
    }
    if (peak < 1e-6){ say("that selection is silent"); return; }
    const f = 0.99 / peak;
    snapshot();
    const out = copyBuf();
    eachChannel(out, d => { for (let i = a; i < b; i++) d[i] = clamp(d[i] * f, -1, 1); });
    commit(out, `normalised (${(20 * Math.log10(f)).toFixed(1)} dB)`);
  }

  function reverse(){
    const [a, b] = range();
    snapshot();
    const out = copyBuf();
    eachChannel(out, d => {
      for (let i = a, j = b - 1; i < j; i++, j--){ const t = d[i]; d[i] = d[j]; d[j] = t; }
    });
    commit(out, "reversed");
  }

  function insertSilence(seconds){
    const n = Math.round(seconds * S.buf.sampleRate);
    if (n <= 0){ say("give it a positive length"); return; }
    const at = S.sel ? S.sel.a : S.buf.length;
    snapshot();
    const out = make(S.buf.length + n);
    eachChannel(out, (d, c) => {
      const src = S.buf.getChannelData(c);
      d.set(src.subarray(0, at), 0);
      d.set(src.subarray(at), at + n);
    });
    commit(out, `inserted ${fmt(seconds)} of silence`);
  }

  /* Repeat the selection (or the whole clip) N times. With a crossfade this is
   * how a 30-second loop becomes 90 seconds without a seam — butt-joining two
   * copies of a musical phrase clicks, and the click is what makes looped
   * music sound cheap. */
  function repeat(times, crossfadeMs){
    const [a, b] = range();
    const n = b - a;
    const reps = Math.max(2, Math.round(times));
    const xf = clamp(Math.round((crossfadeMs / 1000) * S.buf.sampleRate), 0,
                     Math.floor(n / 2));
    if (n < 2){ say("select something to repeat"); return; }
    snapshot();
    const total = n * reps - xf * (reps - 1);
    const out = make(total);
    eachChannel(out, (d, c) => {
      const src = S.buf.getChannelData(c);
      for (let r = 0; r < reps; r++){
        const at = r * (n - xf);
        for (let i = 0; i < n; i++){
          const v = src[a + i];
          const pos = at + i;
          if (pos >= total) break;
          if (r > 0 && i < xf){
            const t = i / xf;                  // equal-power keeps the level flat
            d[pos] = d[pos] * Math.cos(t * Math.PI / 2) + v * Math.sin(t * Math.PI / 2);
          } else {
            d[pos] = v;
          }
        }
      }
    });
    commit(out, `repeated ×${reps}${xf ? ` with a ${crossfadeMs}ms crossfade` : ""}`);
  }

  /* Resample. Speed and pitch move together, exactly like pitching a tape —
   * which is the effect you want for making a big version of a small hit. */
  function speed(factor){
    const f = clamp(factor, 0.25, 4);
    if (Math.abs(f - 1) < 1e-3) return;
    snapshot();
    const out = make(Math.max(2, Math.floor(S.buf.length / f)));
    eachChannel(out, (d, c) => {
      const src = S.buf.getChannelData(c);
      for (let i = 0; i < d.length; i++){
        const x = i * f;
        const i0 = Math.floor(x), frac = x - i0;
        const s0 = src[i0] || 0, s1 = src[i0 + 1] !== undefined ? src[i0 + 1] : s0;
        d[i] = s0 + (s1 - s0) * frac;          // linear is enough at these ratios
      }
    });
    commit(out, `${f < 1 ? "slowed" : "sped up"} ×${f.toFixed(2)}`);
  }

  function toMono(){
    if (S.buf.numberOfChannels === 1){ say("already mono"); return; }
    snapshot();
    const out = make(S.buf.length, 1);
    const d = out.getChannelData(0);
    const l = S.buf.getChannelData(0), r = S.buf.getChannelData(1);
    for (let i = 0; i < d.length; i++) d[i] = (l[i] + r[i]) / 2;
    commit(out, "mixed down to mono");
  }

  /* ── synth: a sound effect out of nothing ─────────────────────────────── */
  function synthRender(){
    const p = S.synth;
    const rate = S.ctx.sampleRate;
    const n = Math.max(1, Math.round(p.seconds * rate));
    const out = S.ctx.createBuffer(1, n, rate);
    const d = out.getChannelData(0);
    const a = Math.round(p.attack * rate), dec = Math.round(p.decay * rate);
    const rel = Math.round(p.release * rate);
    let phase = 0;
    // Deterministic noise: the same settings must produce the same sound, or
    // "that one was good, do it again" is impossible.
    let seed = 0x2545f491;
    const rnd = () => {
      seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5;
      return ((seed >>> 0) / 0xffffffff) * 2 - 1;
    };
    for (let i = 0; i < n; i++){
      const t = i / rate;
      const freq = Math.max(20, p.freq + p.sweep * (t / Math.max(p.seconds, 1e-6)) * p.freq / 100 * 10);
      phase += (2 * Math.PI * freq) / rate;
      let v;
      switch (p.wave){
        case "sine":     v = Math.sin(phase); break;
        case "square":   v = Math.sin(phase) >= 0 ? 1 : -1; break;
        case "saw":      v = ((phase / (2 * Math.PI)) % 1) * 2 - 1; break;
        case "triangle": v = 2 * Math.abs(((phase / (2 * Math.PI)) % 1) * 2 - 1) - 1; break;
        default:         v = rnd(); break;
      }
      if (p.noise > 0) v = v * (1 - p.noise) + rnd() * p.noise;
      let env;
      if (i < a) env = a ? i / a : 1;
      else if (i < a + dec) env = 1 - (1 - p.sustain) * ((i - a) / Math.max(dec, 1));
      else if (i > n - rel) env = p.sustain * ((n - i) / Math.max(rel, 1));
      else env = p.sustain;
      v *= Math.max(0, env) * p.gain;
      if (p.crush > 0){
        const levels = Math.max(2, Math.round(256 / Math.pow(2, p.crush)));
        v = Math.round(v * levels) / levels;
      }
      d[i] = clamp(v, -1, 1);
    }
    return out;
  }

  function synthPreview(){
    const buf = synthRender();
    try { S.ctx.resume(); } catch (e) {}
    const src = S.ctx.createBufferSource();
    src.buffer = buf; src.connect(S.ctx.destination); src.start();
  }
  function synthReplace(){ snapshot(); commit(synthRender(), "synthesised"); }
  function synthAppend(){
    const add = synthRender();
    snapshot();
    const out = make(S.buf.length + add.length);
    eachChannel(out, (d, c) => {
      d.set(S.buf.getChannelData(c), 0);
      d.set(add.getChannelData(0), S.buf.length);
    });
    commit(out, "appended");
  }
  function synthField(key, v){
    S.synth[key] = key === "wave" ? v : parseFloat(v);
    if (key === "wave") renderSide();
  }

  /* ── mixer ────────────────────────────────────────────────────────────── */
  async function addTrack(rel){
    if (!rel){
      const d = await readJSON("/api/audio/lab/list", {sounds:[]});
      const names = (d.sounds || []).map(s => s.rel);
      const choice = prompt("Layer which sound?\n\n" + names.slice(0, 40).join("\n"),
                            names[0] || "");
      if (!choice) return;
      rel = choice.trim();
    }
    S.tracks.push({ source: rel, name: rel.split("/").pop(), offset_s: 0,
                    gain_db: 0, pan: 0, muted: false, solo: false, reverse: false });
    renderSide();
  }
  function trackField(i, key, v){
    const t = S.tracks[i];
    if (!t) return;
    if (key === "muted" || key === "solo" || key === "reverse") t[key] = !!v;
    else t[key] = parseFloat(v) || 0;
    renderSide();
  }
  function dropTrack(i){ S.tracks.splice(i, 1); renderSide(); }

  /* Render every track plus the current clip into one buffer, offline. The
   * current clip is track zero and is never implicit — a mixdown that silently
   * included or excluded what you were looking at would be a coin flip. */
  async function mixdown(includeCurrent){
    if (!S.tracks.length && !includeCurrent){ say("nothing to mix"); return; }
    const rate = S.buf.sampleRate;
    const solo = S.tracks.some(t => t.solo);
    const loaded = [];
    for (const t of S.tracks){
      if (t.muted || (solo && !t.solo)) continue;
      try {
        const bytes = await fetch(`/api/audio/file?rel=${encodeURIComponent(t.source)}`)
          .then(r => r.arrayBuffer());
        loaded.push({ t, buf: await S.ctx.decodeAudioData(bytes) });
      } catch (e){
        say(`could not decode ${t.source}`);
        return;
      }
    }
    const ends = loaded.map(({t, buf}) => t.offset_s + buf.duration);
    if (includeCurrent) ends.push(S.buf.duration);
    const seconds = Math.max(0.01, ...ends);
    const channels = Math.max(1, ...loaded.map(x => x.buf.numberOfChannels),
                              includeCurrent ? S.buf.numberOfChannels : 1);
    const off = new OfflineAudioContext(channels, Math.ceil(seconds * rate), rate);

    const place = (buf, offset, db, pan, rev) => {
      let use = buf;
      if (rev){
        use = off.createBuffer(buf.numberOfChannels, buf.length, buf.sampleRate);
        for (let c = 0; c < buf.numberOfChannels; c++){
          const src = buf.getChannelData(c), d = use.getChannelData(c);
          for (let i = 0; i < src.length; i++) d[i] = src[src.length - 1 - i];
        }
      }
      const node = off.createBufferSource();
      node.buffer = use;
      let tail = node;
      const g = off.createGain();
      g.gain.value = Math.pow(10, db / 20);
      tail.connect(g); tail = g;
      if (channels > 1 && off.createStereoPanner){
        const p = off.createStereoPanner();
        p.pan.value = clamp(pan, -1, 1);
        tail.connect(p); tail = p;
      }
      tail.connect(off.destination);
      node.start(Math.max(0, offset));
    };
    if (includeCurrent) place(S.buf, 0, 0, 0, false);
    loaded.forEach(({t, buf}) => place(buf, t.offset_s, t.gain_db, t.pan, t.reverse));

    const rendered = await off.startRendering();
    // Bring it back into the live context so every later edit sees one kind of
    // buffer, not "the mixed one" and "the others".
    const out = make(rendered.length, rendered.numberOfChannels, rate);
    for (let c = 0; c < out.numberOfChannels; c++)
      out.getChannelData(c).set(rendered.getChannelData(c));
    snapshot();
    commit(out, `mixed ${loaded.length + (includeCurrent ? 1 : 0)} source(s)`);
  }

  async function saveSession(){
    const rel = S.rel || S.saveAs;
    const r = await mutate("/api/audio/lab/session", {
      body: { rel, session: { tracks: S.tracks, sample_rate: S.buf.sampleRate } }});
    if (r.ok) say(`mix session saved to ${r.data.path}`, "ok");
  }

  /* ── WAV encoding ─────────────────────────────────────────────────────── */
  function encodeWav(buf){
    const ch = buf.numberOfChannels, n = buf.length, rate = buf.sampleRate;
    const bytes = 44 + n * ch * 2;
    const ab = new ArrayBuffer(bytes);
    const view = new DataView(ab);
    const str = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    str(0, "RIFF"); view.setUint32(4, bytes - 8, true); str(8, "WAVE");
    str(12, "fmt "); view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); view.setUint16(22, ch, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * ch * 2, true);
    view.setUint16(32, ch * 2, true); view.setUint16(34, 16, true);
    str(36, "data"); view.setUint32(40, n * ch * 2, true);
    const data = [];
    for (let c = 0; c < ch; c++) data.push(buf.getChannelData(c));
    let off = 44;
    for (let i = 0; i < n; i++){
      for (let c = 0; c < ch; c++){
        const v = clamp(data[c][i], -1, 1);
        view.setInt16(off, v < 0 ? v * 0x8000 : v * 0x7fff, true);
        off += 2;
      }
    }
    let bin = "";
    const u8 = new Uint8Array(ab);
    for (let i = 0; i < u8.length; i += 0x8000)
      bin += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  /* ── persistence ──────────────────────────────────────────────────────── */
  async function save(asNew){
    if (!S) return;
    let rel = S.rel;
    if (asNew || !rel){
      rel = (document.getElementById("ab-saveas") || {}).value || S.saveAs;
      rel = String(rel || "").trim();
      if (!rel){ say("give it a path first"); return; }
    }
    if (/\.ogg$/i.test(rel) && S.status && !S.status.ogg){
      say(S.status.ogg_reason || "ffmpeg is needed to write .ogg"); return;
    }
    const wav = encodeWav(S.buf);
    const r = await mutate("/api/audio/lab/save", {
      body: { rel, wav, mtime: (rel === S.rel) ? S.mtime : undefined,
              ogg_quality: 6 },
      button: "ab-save" });
    if (!r.ok) return;
    S.rel = r.data.rel; S.mtime = r.data.mtime; S.saveAs = r.data.rel;
    S.meta = r.data; S.loop = r.data.loop || S.loop;
    S.dirty = false;
    renderSide(); paint();
    say(r.data.created
        ? `created ${r.data.rel}${r.data.needs_godot_import
            ? " — open the project in Godot once so it imports" : ""}`
        : `saved · previous copy at ${r.data.backup}`, "ok");
  }

  async function writeLoop(enabled){
    if (!S || !S.rel){ say("save the file first — loop points live in its .import"); return; }
    const rate = S.buf.sampleRate;
    const begin = S.sel ? S.sel.a / rate : (S.loop.begin_s || 0);
    const end = S.sel ? S.sel.b / rate : S.loop.end_s;
    const r = await mutate("/api/audio/lab/loop", {
      body: { rel: S.rel, enabled, begin_s: begin,
              end_s: (S.loop.importer === "ogg") ? null : end,
              mode: S.loop.mode || "forward" }});
    if (!r.ok) return;
    S.loop = r.data.loop;
    renderSide(); paint();
    say(enabled ? `loop set from ${fmt(begin)}` : "looping turned off", "ok");
    if ((r.data.ignored || []).length) say(r.data.ignored[0]);
  }

  /* ── side panel ───────────────────────────────────────────────────────── */
  function renderSide(){
    if (!S || !$.side) return;
    const rate = S.buf.sampleRate;
    const sel = S.sel;
    const p = S.synth;
    const st = S.status;
    const loop = S.loop || {};

    $.side.innerHTML = `
      <div class="ab-h">edit<span></span></div>
      <div class="ab-note">${sel
        ? `Selection <b>${fmt(sel.a / rate)} → ${fmt(sel.b / rate)}</b>`
        : `No selection — these apply to the <b>whole clip</b>. Drag on the
           waveform to select.`}</div>
      <div class="ab-grid2">
        <button class="ab-btn" onclick="AudioLab.trim()">trim to selection</button>
        <button class="ab-btn" onclick="AudioLab.cut()">delete</button>
        <button class="ab-btn" onclick="AudioLab.silence()">silence</button>
        <button class="ab-btn" onclick="AudioLab.reverse()">reverse</button>
        <button class="ab-btn" onclick="AudioLab.fade('in')">fade in</button>
        <button class="ab-btn" onclick="AudioLab.fade('out')">fade out</button>
        <button class="ab-btn" onclick="AudioLab.gain(-3)">−3 dB</button>
        <button class="ab-btn" onclick="AudioLab.gain(3)">+3 dB</button>
        <button class="ab-btn" onclick="AudioLab.normalize()">normalise</button>
        <button class="ab-btn" onclick="AudioLab.toMono()">to mono</button>
      </div>
      <div class="ab-row">
        <label>silence</label>
        <input class="ab-in num" id="ab-sil" type="number" step="0.05" min="0" value="0.25">
        <button class="ab-btn" onclick="AudioLab.insertSilence(+document.getElementById('ab-sil').value)">insert</button>
      </div>
      <div class="ab-row">
        <label>speed</label>
        <input class="ab-in num" id="ab-speed" type="number" step="0.05" min="0.25" max="4" value="1">
        <button class="ab-btn" onclick="AudioLab.speed(+document.getElementById('ab-speed').value)">apply</button>
      </div>
      <div class="ab-note">Speed moves pitch with it, like pitching tape — which
        is how you make a heavy version of a light hit.</div>

      <div class="ab-h">extend<span></span></div>
      <div class="ab-row">
        <label>repeat</label>
        <input class="ab-in num" id="ab-reps" type="number" min="2" max="64" value="3">×
        <input class="ab-in num" id="ab-xf" type="number" min="0" max="2000" value="120">ms
      </div>
      <button class="ab-btn wide" onclick="AudioLab.repeat(+document.getElementById('ab-reps').value,+document.getElementById('ab-xf').value)">
        repeat with crossfade</button>
      <div class="ab-note">An equal-power crossfade is what stops a butt-joined
        loop from clicking at the seam.</div>

      <div class="ab-h">godot loop<span></span></div>
      ${loop.supported ? `
        <div class="ab-note">${loop.enabled
          ? `This clip <b>loops</b> from <b>${fmt(loop.begin_s)}</b>${
              loop.end_s != null ? ` to <b>${fmt(loop.end_s)}</b>` : ""}.`
          : `<span class="ab-warn">This clip does not loop.</span> The setting
             lives in its <b>.import</b>, not in the audio — a music track that
             plays once and stops looks and sounds perfect right here.`}</div>
        ${loop.importer === "wav" ? `<div class="ab-row">
          <label>mode</label>
          <select class="ab-in" onchange="AudioLab.loopMode(this.value)">
            ${["forward","pingpong","backward"].map(m =>
              `<option value="${m}"${loop.mode===m?" selected":""}>${m}</option>`).join("")}
          </select></div>` : `<div class="ab-note">An .ogg loops from its offset
            to the end of the stream — Godot's ogg importer has no loop end.</div>`}
        <div class="ab-grid2">
          <button class="ab-btn go" onclick="AudioLab.writeLoop(true)">${
            sel ? "loop from selection" : "enable looping"}</button>
          <button class="ab-btn" onclick="AudioLab.writeLoop(false)">turn off</button>
        </div>
        ${!loop.has_import ? `<div class="ab-note ab-warn">No .import yet — open
          the project in Godot once so the engine writes one.</div>` : ""}`
        : `<div class="ab-note">${E(S.rel || "this clip")} has no Godot loop
            settings (${E(loop.importer || "unknown importer")}).</div>`}

      <div class="ab-h">synth<span></span></div>
      <div class="ab-row">
        <label>wave</label>
        <select class="ab-in" onchange="AudioLab.synthField('wave',this.value)">
          ${WAVES.map(w => `<option value="${w}"${p.wave===w?" selected":""}>${w}</option>`).join("")}
        </select>
      </div>
      ${synthRow("freq", "pitch Hz", p.freq, 20, 8000, 1)}
      ${synthRow("sweep", "sweep", p.sweep, -100, 100, 1)}
      ${synthRow("seconds", "length s", p.seconds, 0.02, 6, 0.01)}
      ${synthRow("attack", "attack", p.attack, 0, 1, 0.005)}
      ${synthRow("decay", "decay", p.decay, 0, 2, 0.005)}
      ${synthRow("sustain", "sustain", p.sustain, 0, 1, 0.01)}
      ${synthRow("release", "release", p.release, 0, 2, 0.005)}
      ${synthRow("noise", "noise", p.noise, 0, 1, 0.01)}
      ${synthRow("crush", "bitcrush", p.crush, 0, 7, 1)}
      <div class="ab-grid2">
        <button class="ab-btn" onclick="AudioLab.synthPreview()">▶ preview</button>
        <button class="ab-btn" onclick="AudioLab.synthAppend()">append</button>
      </div>
      <button class="ab-btn wide" onclick="AudioLab.synthReplace()">replace the clip</button>

      <div class="ab-h">mixer<span></span></div>
      ${S.tracks.length ? S.tracks.map((t, i) => `
        <div class="ab-track">
          <div class="hd">${E(t.name)}
            <span class="x" onclick="AudioLab.dropTrack(${i})">✕</span></div>
          <div class="ab-row"><label>at s</label>
            <input class="ab-in num" type="number" step="0.01" value="${t.offset_s}"
                   oninput="AudioLab.trackField(${i},'offset_s',this.value)">
            <label>dB</label>
            <input class="ab-in num" type="number" step="1" value="${t.gain_db}"
                   oninput="AudioLab.trackField(${i},'gain_db',this.value)"></div>
          <div class="ab-row"><label>pan</label>
            <input class="ab-in" type="range" min="-1" max="1" step="0.05" value="${t.pan}"
                   oninput="AudioLab.trackField(${i},'pan',this.value)"></div>
          <div class="mini">
            <button class="ab-tg${t.muted?" on":""}" onclick="AudioLab.trackField(${i},'muted',${!t.muted})">mute</button>
            <button class="ab-tg${t.solo?" on":""}" onclick="AudioLab.trackField(${i},'solo',${!t.solo})">solo</button>
            <button class="ab-tg${t.reverse?" on":""}" onclick="AudioLab.trackField(${i},'reverse',${!t.reverse})">reverse</button>
          </div>
        </div>`).join("")
        : `<div class="ab-note">Layer other project sounds under this one — a hit
            plus a noise tail, a stinger over a pad.</div>`}
      <button class="ab-btn wide" onclick="AudioLab.addTrack()">+ layer a sound</button>
      <div class="ab-grid2">
        <button class="ab-btn go" onclick="AudioLab.mixdown(true)">mix with this clip</button>
        <button class="ab-btn" onclick="AudioLab.mixdown(false)">layers only</button>
      </div>
      <button class="ab-btn wide" onclick="AudioLab.saveSession()">save mix session</button>

      <div class="ab-h">save<span></span></div>
      <div class="ab-row">
        <input class="ab-in" id="ab-saveas" value="${E(S.saveAs || S.rel || "")}">
      </div>
      <div class="ab-grid2">
        <button class="ab-btn go" onclick="AudioLab.save(false)">save</button>
        <button class="ab-btn" onclick="AudioLab.save(true)">save as</button>
      </div>
      <div class="ab-note">${st
        ? (st.ogg ? "Both <b>.wav</b> and <b>.ogg</b> can be written here."
                  : `<span class="ab-warn">${E(st.ogg_reason)}</span>`)
        : "checking what this install can write…"}</div>`;
    refreshHistory();
  }

  function synthRow(key, label, value, min, max, step){
    return `<div class="ab-row"><label>${E(label)}</label>
      <input class="ab-in" type="range" min="${min}" max="${max}" step="${step}"
             value="${value}" oninput="AudioLab.synthField('${key}',this.value)">
      <span class="ab-sub" style="width:44px;text-align:right">${value}</span></div>`;
  }

  /* ── studio mode ──────────────────────────────────────────────────────── */
  /* The beat maker is a second view onto the SAME clip, not a second document.
   * It renders through `adopt`, which lands the result in the normal undo stack
   * — so a rendered beat can then be trimmed, faded, looped and saved by
   * everything that already exists here. */
  function setMode(mode){
    if (!S) return;
    S.mode = mode === "studio" ? "studio" : "clip";
    const studio = document.getElementById("ab-studio");
    const wave = document.getElementById("ab-wave");
    const transport = document.querySelector(".ab-transport");
    document.querySelectorAll("#ab-modes .ab-mode").forEach(b =>
      b.classList.toggle("active", b.dataset.m === S.mode));
    if (studio) studio.hidden = S.mode !== "studio";
    if (wave) wave.hidden = S.mode === "studio";
    if (transport) transport.hidden = S.mode === "studio";
    if (S.mode === "studio"){
      stop();
      if (!window.BeatMaker){ studio.innerHTML =
        `<div style="padding:24px;color:var(--ash2)">the beat maker did not load</div>`; return; }
      if (!S.studioMounted){
        BeatMaker.mount(studio, (S.meta && S.meta.beat) || null);
        S.studioMounted = true;
      }
    } else {
      if (window.BeatMaker) BeatMaker.stop();
      paint();
    }
  }

  /* Take a buffer the studio rendered and make it the clip. Snapshotting first
     is what makes "render, hate it, undo" work. */
  function adopt(rendered, label){
    if (!S) return;
    stop();
    snapshot();
    const out = make(rendered.length, rendered.numberOfChannels, S.buf.sampleRate);
    for (let c = 0; c < out.numberOfChannels; c++)
      out.getChannelData(c).set(rendered.getChannelData(c));
    commit(out, label || "rendered");
    setMode("clip");
  }

  function loopMode(m){ S.loop.mode = m; renderSide(); }
  function selectAll(){ S.sel = { a: 0, b: S.buf.length }; renderSide(); paint(); }
  function clearSel(){ S.sel = null; renderSide(); paint(); }
  function zoomSel(){ if (S.sel){ S.view = { from: S.sel.a, to: S.sel.b }; paint(); } }
  function zoomFit(){ S.view = { from: 0, to: S.buf.length }; paint(); }

  return {
    open, close, pick, pickSearch, closePick, newSound, setMode, adopt,
    togglePlay, play, stop, undo, redo, save, saveSession, writeLoop, loopMode,
    selectAll, clearSel, zoomSel, zoomFit,
    trim, cut, silence, fade, gain, normalize, reverse, insertSilence, repeat,
    speed, toMono,
    synthField, synthPreview, synthReplace, synthAppend,
    addTrack, trackField, dropTrack, mixdown,
    get state(){ return S; },
  };
})();
