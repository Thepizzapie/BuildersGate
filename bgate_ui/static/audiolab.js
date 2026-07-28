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
  // Same story as the sprite editor: built as a fullscreen overlay, needed as
  // a Studio page. _host set => mount inside it, flat and without a close.
  let _host = null;
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
      // --overlay, not a baked near-black: on the light ground a hardcoded dark
      // scrim buries the panel it is supposed to sit behind.
      ".ab-back{position:fixed;inset:0;z-index:1400;background:var(--overlay);backdrop-filter:blur(3px);display:flex;flex-direction:column}",
      // Embedded in a Studio tab: a panel in the page, not a sheet over it.
      ".ab-back.ab-embed{position:relative;inset:auto;z-index:auto;background:var(--surface-2);backdrop-filter:none;height:100%;border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden}",
      ".ab-back.ab-embed .ab-closebtn{display:none}",
      // A file is hovering over the pane. Outline rather than a border so the
      // layout does not shift by 2px the moment a drag enters.
      ".ab-back.ab-drop,.ab-land.ab-drop{outline:2px dashed var(--accent);outline-offset:-6px}",
      ".ab-land{display:grid;place-items:center;height:100%;min-height:420px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-lg)}",
      ".ab-land-in{text-align:center;max-width:380px;padding:var(--s-8)}",
      ".ab-land-btns{display:flex;gap:var(--s-3);justify-content:center;flex-wrap:wrap}",
      ".ab-land-in h3{font-size:var(--fs-xl);font-weight:var(--fw-regular);color:var(--text);margin-bottom:var(--s-4)}",
      ".ab-land-in p{color:var(--text-3);font-size:var(--fs-md);line-height:var(--lh);margin-bottom:var(--s-7)}",
      ".ab-bar{display:flex;align-items:center;gap:9px;padding:9px 14px;border-bottom:1px solid var(--seam);background:var(--iron);flex:none}",
      ".ab-title{font-family:var(--mono);font-size:11.5px;letter-spacing:.1em;color:var(--bone);text-transform:uppercase}",
      ".ab-sub{font-family:var(--mono);font-size:10px;color:var(--ash2)}",
      ".ab-dirty{color:var(--warn)}",
      ".ab-spacer{flex:1}",
      ".ab-btn{padding:6px 11px;background:var(--plate);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;cursor:pointer}",
      ".ab-btn:hover:not(:disabled){border-color:var(--ember)}",
      ".ab-btn:disabled{opacity:.4;cursor:default}",
      ".ab-btn.go{background:var(--ember);color:var(--bg);border-color:var(--ember);font-weight:var(--fw-semi)}",
      ".ab-btn.wide{width:100%;margin-bottom:6px}",
      ".ab-body{flex:1;display:flex;min-height:0}",
      ".ab-main{flex:1;min-width:0;display:flex;flex-direction:column;background:var(--bg)}",
      ".ab-modes{display:flex;gap:6px;padding:8px 12px;border-bottom:1px solid var(--seam);background:var(--iron);flex:none}",
      ".ab-mode{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;padding:5px 12px;border:1px solid var(--seam);border-radius:999px;background:none;color:var(--ash);cursor:pointer}",
      ".ab-mode:hover{border-color:var(--ember);color:var(--bone)}",
      ".ab-mode.active{background:var(--plate);border-color:var(--ember);color:var(--bone)}",
      ".ab-studio{flex:1;min-height:0;display:flex}",
      ".ab-studio>*{flex:1;min-width:0}",
      ".ab-wave{flex:1;position:relative;min-height:0}",
      ".ab-wave canvas{position:absolute;inset:0;width:100%;height:100%;cursor:text;touch-action:none}",
      ".ab-layers{flex:1;position:relative;min-height:0}",
      ".ab-layers canvas{position:absolute;inset:0;width:100%;height:100%;cursor:grab;touch-action:none}",
      ".ab-hud{position:absolute;left:10px;bottom:8px;font-family:var(--mono);font-size:10px;color:var(--ash2);background:var(--surface-1);border:1px solid var(--seam);border-radius:6px;padding:3px 8px;pointer-events:none;white-space:pre}",
      ".ab-transport{display:flex;align-items:center;gap:8px;padding:8px 12px;border-top:1px solid var(--seam);background:var(--iron);flex:none;flex-wrap:wrap}",
      // setMode() hides these with the `hidden` attribute, and the UA rule that
      // implements it ([hidden]{display:none}) loses to any author display.
      // Without this line the studio and the clip transport were both permanently
      // on screen and mode switching looked like a no-op.
      ".ab-studio[hidden],.ab-transport[hidden],.ab-wave[hidden],.ab-stage[hidden],.ab-layers[hidden]{display:none}",
      // The range ops, which only exist while a range does. A flex row of its
      // own so the four buttons wrap as a unit, and its own [hidden] rule for
      // the same reason the line above needs one.
      "#ab-lrange{display:flex;align-items:center;gap:8px}",
      "#ab-lrange[hidden]{display:none}",
      // The audition strip. Sits on the accent ground so a pending edit reads as
      // a state the pane is IN, not as one more row of buttons.
      ".ab-stage{display:flex;align-items:center;gap:var(--s-2);padding:var(--s-2) var(--s-4);border-top:1px solid var(--accent-line);background:var(--accent-soft);flex:none;flex-wrap:wrap}",
      ".ab-stage .lbl{font-family:var(--mono);font-size:var(--fs-xs);color:var(--text)}",
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
      // A toggle sharing a row with a field must not be squeezed by it.
      ".ab-row .ab-tg{flex:none}",
      ".ab-row .ab-btn{flex:none}",
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
      ".ab-pick{position:fixed;inset:0;z-index:1401;background:var(--overlay);display:flex;align-items:center;justify-content:center;padding:40px}",
      ".ab-pbox{background:var(--iron);border:1px solid var(--seam);border-radius:12px;width:min(720px,100%);max-height:100%;display:flex;flex-direction:column;overflow:hidden}",
      ".ab-plist{overflow-y:auto;padding:8px}",
      ".ab-pi{display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:7px;cursor:pointer;font-family:var(--mono);font-size:11px;color:var(--bone)}",
      ".ab-pi:hover{background:var(--plate)}",
      ".ab-pi .m{margin-left:auto;color:var(--ash2);font-size:10px}",
      ".ab-tag{font-size:9px;padding:1px 6px;border-radius:999px;border:1px solid var(--seam);color:var(--ash2)}",
      ".ab-tag.on{border-color:var(--good);color:var(--good)}",
      ".ab-tag.loop{border-color:var(--line-strong);color:var(--text)}",
      // The "new sound" chooser reuses the picker's sheet, but its rows are a
      // form rather than a list — each starting point is a card that says what
      // it makes and carries its own button.
      ".ab-opts{overflow-y:auto;padding:var(--s-4);display:flex;flex-direction:column;gap:var(--s-4)}",
      // --surface-2, not --surface-1: the sheet behind it is already --surface-1
      // (via --iron), and a card the colour of its ground is not a card.
      ".ab-opt{border:1px solid var(--line);border-radius:var(--r-sm);padding:var(--s-5);background:var(--surface-2)}",
      // Unavailable, not hidden: "duplicate" that vanishes when nothing is open
      // reads as a missing feature. Dimmed with its reason in the header instead.
      ".ab-opt.off{opacity:.55}",
      ".ab-opt .hd{display:flex;align-items:baseline;gap:var(--s-3);margin-bottom:var(--s-3);flex-wrap:wrap}",
      ".ab-opt .hd b{font-family:var(--mono);font-size:var(--fs-2xs);letter-spacing:.14em;text-transform:uppercase;color:var(--text)}",
      ".ab-opt .hd span{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-3)}",
      ".ab-opt .ab-btn.wide{margin-bottom:0}",
      ".ab-opt .ab-row:last-child{margin-bottom:0}",
      // Preflight failure panel. Deliberately the playtest bar's shape — same
      // problem (a device or a binary is missing), so the same reading.
      ".ab-why{padding:var(--s-5) var(--s-6);border-bottom:1px solid var(--line);background:var(--surface-2);font-family:var(--mono);font-size:var(--fs-xs);line-height:var(--lh);color:var(--text-2);flex:none}",
      ".ab-why[hidden]{display:none}",
      ".ab-why .hd{display:flex;align-items:center;gap:var(--s-3);margin-bottom:var(--s-4)}",
      ".ab-why .hd b{color:var(--text);text-transform:uppercase;letter-spacing:.1em;font-size:var(--fs-3xs)}",
      ".ab-why .hd .x{margin-left:auto;border:0;background:none;color:var(--text-3);font:inherit;cursor:pointer;padding:0 var(--s-2)}",
      ".ab-why .hd .x:hover{color:var(--text)}",
      ".ab-why .foot{margin-top:var(--s-4);color:var(--text-3)}",
      ".ab-checks{list-style:none;display:flex;flex-direction:column;gap:var(--s-4)}",
      ".ab-checks li{display:flex;flex-direction:column;gap:var(--s-1);padding-left:var(--s-5);border-left:2px solid var(--bad-line)}",
      ".ab-checks b{color:var(--text);text-transform:uppercase;letter-spacing:.1em;font-size:var(--fs-3xs)}",
      ".ab-checks .r{color:var(--bad);word-break:break-word}",
      ".ab-checks .fix{color:var(--text);word-break:break-word}",
      // On the cold landing card there is no bar to hang it under, so it drops
      // into the card itself and needs an edge of its own.
      ".ab-land-in .ab-why{margin-top:var(--s-6);text-align:left;border:1px solid var(--line);border-radius:var(--r-sm)}",
      // Recording strip. Sits on the bad-line edge because it is a state the
      // pane is IN — the mic is open until this bar goes away.
      ".ab-rec{display:flex;align-items:center;gap:var(--s-3);padding:var(--s-2) var(--s-4);border-top:1px solid var(--bad-line);background:var(--surface-2);flex:none;flex-wrap:wrap}",
      ".ab-rec[hidden]{display:none}",
      ".ab-rec .dot{width:9px;height:9px;border-radius:var(--r-full);background:var(--bad);flex:none;animation:ab-blink 1s steps(2,end) infinite}",
      "@keyframes ab-blink{50%{opacity:.2}}",
      ".ab-rec .t{font-family:var(--mono);font-size:var(--fs-sm);color:var(--text);min-width:72px}",
      ".ab-rec .note{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-3)}",
      ".ab-rec .note.warn{color:var(--bad)}",
      ".ab-meter{flex:1;min-width:80px;max-width:220px;height:8px;border-radius:var(--r-xs);background:var(--surface-3);border:1px solid var(--line);overflow:hidden}",
      ".ab-meter i{display:block;height:100%;width:0;background:var(--accent)}",
      ".ab-meter.hot i{background:var(--bad)}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── open / close ─────────────────────────────────────────────────────── */
  /* Every path that replaces the open clip has to ask first. newSound() used to
     skip this and threw away the edits and the undo stack without a word.
     Awaited by all five callers — the question is a real element now, so a
     guard nobody waits for is a guard that lets the edits go. */
  async function discardGuard(){
    if (!(S && S.dirty)) return true;
    return await askConfirm({
      title: "Discard unsaved audio edits?",
      body: "The clip goes back to what is on disk, and the undo history goes with it.",
      ok: "discard", danger: true });
  }

  async function open(rel){
    injectStyle();
    if (!await discardGuard()) return;
    if (S) close();
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
      // &v=mtime: a saved file is a new cache key, so re-opening can never
      // decode the revision that was on disk before the save.
      const bytes = await fetch(
        `/api/audio/file?rel=${encodeURIComponent(rel)}&v=${meta.mtime || 0}`)
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
      staged: null,              // a rendered-but-uncommitted edit; see stage()
      dirty: false,
      play: null, preview: null, playFrom: 0, playStart: 0,
      loop: Object.assign({}, meta.loop),
      tracks: (meta.session && meta.session.tracks || []).slice(),
      // Layers mode. Its view is SECONDS, not samples of S.buf: a layer has its
      // own rate and can end long past the clip, which S.view cannot express.
      lview: null, layerBufs: {}, layerBusy: {}, lscroll: 0, lsel: 0,
      // A time range hangs off ONE lane, so it carries that lane's index with
      // it — see focusLane() for why it cannot be allowed to outlive it.
      lrange: null,
      lplay: null, lhead: 0,
      // The clip is a lane in the stack and needs the same three buttons the
      // layers have — but it is NOT pushed into S.tracks, because the server's
      // normalise_session rejects a track with no `source`.
      clipLane: { muted: false, solo: false, gain_db: 0, pan: 0 },
      status: null, mode: "clip", studioMounted: false,
      synth: { wave:"square", freq:440, sweep:-40, seconds:0.22,
               attack:0.005, decay:0.08, sustain:0.25, release:0.09,
               noise:0, crush:0, gain:0.8 },
      ui: { speed: 1, silence: 0.25, reps: 3, xf: 120,
            gain_db: -3, fade_curve: "linear", norm_db: -1,
            norm_per_channel: false, semitones: 0,
            units: "s", snap: true, loopSel: false, preview: true,
            // Which meaning the layer canvas's drag carries. In `ui` and not
            // beside lsel so it survives "new from selection" with the rest of
            // the toolbar state.
            ltool: "move" },
      saveAs: rel,
    };
    mount();
    labStatus();
    paint();
  }

  /* The question is the only asynchronous thing about closing, so it lives out
     here: open(), newSound() and unembed() tear the session down and rebuild it
     in the same breath, and they need close() to be finished when it returns.
     Everything a person clicks goes through closeAsk() instead. */
  async function closeAsk(){
    if (S && S.dirty && !await askConfirm({
      title: "You have unsaved audio edits. Close anyway?",
      body: "The edits and the undo history are dropped. Save or bounce first to keep them.",
      ok: "close anyway", danger: true })) return;
    close();
  }

  function close(){
    stop();
    // Before the DOM goes: a take in flight has an OS device open, and closing
    // the pane out from under it would leave the microphone live with nothing
    // on screen saying so.
    recTeardown();
    if (window.BeatMaker) try { BeatMaker.unmount(); } catch (e) {}
    if (S){
      try { S.ctx.close(); } catch (e) {}
      if (S.ro) try { S.ro.disconnect(); } catch (e) {}
      if (S.lro) try { S.lro.disconnect(); } catch (e) {}
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
          <button class="ab-btn" onclick="AudioLab.newDialog()">new sound…</button>
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

  /* The same list as a VALUE. pick() above is an entry point: it opens what was
     clicked and returns nothing, which is no use to a caller that needs a path
     back — layering a sound and adding a sample to the beat both want one.
     Same endpoint, same rows, same server-side filter; askPick adds the arrow
     keys. BeatMaker calls this rather than growing a second copy. */
  function pickSound(title){
    return askPick({
      title: title || "pick a sound",
      placeholder: "filter by path…",
      empty: "no audio matches",
      fetch: async q => {
        const d = await readJSON(
          `/api/audio/lab/list${q ? `?q=${encodeURIComponent(q)}` : ""}`, {sounds:[]});
        const sounds = d.sounds || [];
        return {
          items: sounds.map(s => ({
            value: s.rel,
            label: s.name,
            tags: [].concat(s.loops ? [{ text: "loops", tone: "accent" }] : [],
                            s.has_session ? [{ text: "mix", tone: "ok" }] : []),
            meta: `${s.seconds != null ? fmt(s.seconds) : "?"} · `
                + `${s.sample_rate || "?"}Hz ${s.channels === 2 ? "stereo" : "mono"} · ${s.rel}`,
          })),
          total: d.total == null ? sounds.length : d.total,
          truncated: !!d.truncated,
        };
      },
    });
  }

  /* ── making a new sound ───────────────────────────────────────────────────
   * "New" used to mean exactly one thing: half a second of silence with the
   * synth panel open. That is one starting point out of four, and the other
   * three were reachable only by saving the current file under another name and
   * editing it back down — which is a destructive round trip through the clip
   * you were trying to keep.
   *
   * All four end at newSession(): a session whose clip has no path on disk yet.
   * Nothing here writes anything; the first save() decides where it lands. */

  /* Rates a game asset is plausibly written at. 44.1k is the default because it
     is what every other sound in these projects is, and a sheet of SFX at
     mismatched rates is a mixdown that resamples. */
  const NEW_RATES = [8000, 11025, 16000, 22050, 32000, 44100, 48000];
  // Module level, not in S: the chooser is open when there is no session.
  let newDefs = { seconds: 1, rate: 44100, channels: 1 };

  /* A context pinned to the rate the sound is meant to BE, and an empty buffer
     at that rate. The buffer's rate is passed explicitly rather than read back
     off the context: a browser that refuses the constructor hint falls back to
     the device rate, and inheriting that would silently make a "44.1k" new
     sound 48k. Nothing is decoded into this context, so the mismatch costs only
     a resample at playback. */
  function newCtxBuf(rate, length, channels){
    const r = clamp(Math.round(rate) || 44100, 8000, 96000);
    const Ctx = window.AudioContext || window.webkitAudioContext;
    let ctx;
    try { ctx = new Ctx({ sampleRate: r }); } catch (e){ ctx = new Ctx(); }
    // Channels are not clamped to stereo: the blank path already offers only
    // mono or stereo, and a duplicate has to keep whatever the source decoded
    // to — narrowing it here would drop channels without saying so.
    return { ctx,
             buf: ctx.createBuffer(clamp(channels || 1, 1, 32),
                                   Math.max(1, Math.round(length)), r) };
  }

  /* `keepUi` carries the editing preferences — units, snap, audition-first —
     across the boundary, because carving three hits out of one take is three
     new sessions and re-setting them each time is the whole cost of the
     feature. It is copied, never aliased: the old S is about to be dropped. */
  function newSession(ctx, buf, saveAs, keepUi){
    S = {
      rel: null, meta: null, ctx, buf, mtime: null, sel: null,
      view: { from: 0, to: buf.length },
      undo: [], redo: [], undoBytes: 0, staged: null, dirty: true,
      play: null, preview: null, playFrom: 0, playStart: 0,
      loop: { supported:false, enabled:false, mode:"forward", begin_s:0, end_s:null },
      tracks: [],
      lview: null, layerBufs: {}, layerBusy: {}, lscroll: 0, lsel: 0,
      lrange: null,
      lplay: null, lhead: 0,
      clipLane: { muted: false, solo: false, gain_db: 0, pan: 0 },
      status: null, mode: "clip", studioMounted: false,
      synth: { wave:"square", freq:440, sweep:-40, seconds:0.22,
               attack:0.005, decay:0.08, sustain:0.25, release:0.09,
               noise:0, crush:0, gain:0.8 },
      ui: Object.assign({ speed: 1, silence: 0.25, reps: 3, xf: 120,
            gain_db: -3, fade_curve: "linear", norm_db: -1,
            norm_per_channel: false, semitones: 0,
            units: "s", snap: true, loopSel: false, preview: true,
            ltool: "move" }, keepUi || {}),
      saveAs: saveAs || "game/assets/audio/sfx_new.wav",
    };
    mount();
    labStatus();
    paint();
  }

  /* sfx/hit.wav → sfx/hit_copy.wav. Same shape as defaultBounce()'s suffix, and
     a name for a clip that was never saved either. */
  function relSuffix(sfx){
    const rel = (S && (S.rel || S.saveAs)) || "";
    if (!rel) return "game/assets/audio/sound" + sfx + ".wav";
    const dot = rel.lastIndexOf(".");
    return dot > rel.lastIndexOf("/")
      ? rel.slice(0, dot) + sfx + rel.slice(dot)
      : rel + sfx + ".wav";
  }

  /* A sound that does not exist yet. Opens the editor on half a second of
     silence with the synth panel ready — "we need a confirm blip" starts here.
     `quiet` is for the import and record paths, which bootstrap a session this
     way and are about to replace the silence: "synthesise something" would be a
     lie. Kept as its own entry point because those two call it directly. */
  async function newSound(quiet){
    closePick(); closeNew();
    injectStyle();
    if (!await discardGuard()) return;
    const made = newBuild(44100, 44100 * 0.5, 1);
    if (!made) return;
    const keep = S && S.ui;              // read before close() drops S
    if (S) close();
    newSession(made.ctx, made.buf, "game/assets/audio/sfx_new.wav", keep);
    if (!quiet) say("synthesise something, then save it under a name", "ok");
  }

  /* Allocate BEFORE the old session is torn down. A rate the browser will not
     take or a length it cannot allocate has to leave you where you were —
     failing after close() means an empty pane and the clip gone with it. */
  function newBuild(rate, length, channels){
    try { return newCtxBuf(rate, length, channels); }
    catch (e){
      say(`could not start that sound${e && e.message ? " — " + e.message : ""}`);
      return null;
    }
  }

  function fieldNum(id, dflt){
    const el = document.getElementById(id);
    const n = el ? parseFloat(el.value) : NaN;
    return isFinite(n) ? n : dflt;
  }

  /* Silence of a chosen shape. The point is the shape: a take you are about to
     record over, or a bed the right length to build a loop in. */
  async function newBlank(){
    const secs = clamp(fieldNum("ab-new-secs", newDefs.seconds), 0.01, MAX_SECONDS);
    const rate = clamp(Math.round(fieldNum("ab-new-rate", newDefs.rate)), 8000, 96000);
    const ch = fieldNum("ab-new-ch", newDefs.channels) === 2 ? 2 : 1;
    newDefs = { seconds: secs, rate, channels: ch };
    closeNew();
    if (!await discardGuard()) return;
    const made = newBuild(rate, secs * rate, ch);
    if (!made) return;
    const keep = S && S.ui;
    if (S) close();
    newSession(made.ctx, made.buf, "game/assets/audio/sfx_new.wav", keep);
    say(`${fmt(secs)} of silence · ${rate} Hz ${ch === 2 ? "stereo" : "mono"}`, "ok");
  }

  /* The two starting points that come FROM the open clip. Both copy the samples
   * across while the old session is still standing: close() closes the context
   * its buffer belongs to, and reading one across that boundary is not
   * something to rely on. */
  async function newFromClip(selOnly){
    if (!S){ say("open or create a sound first"); return; }
    // Same rule as save(): an audition is not the clip yet, and duplicating
    // "what I can hear" while S.buf is still the old audio is a silent wrong
    // answer. Apply or cancel first, then duplicate.
    if (S.staged){ say("apply or cancel the pending edit first"); return; }
    if (selOnly && !S.sel){ say("drag a selection on the waveform first"); return; }
    const a = selOnly ? S.sel.a : 0, b = selOnly ? S.sel.b : S.buf.length;
    const rate = S.buf.sampleRate, nch = S.buf.numberOfChannels;
    // A duplicate is of the CLIP, not of the stack: layers are an arrangement
    // that belongs to the session they were built in, and "bounce to a new
    // file" is the button that flattens them.
    const saveAs = relSuffix(selOnly ? "_part" : "_copy");
    const keep = S.ui;
    closeNew();
    if (!await discardGuard()) return;
    if (!S) return;                      // the pane can be closed under the question
    const made = newBuild(rate, b - a, nch);
    if (!made) return;
    for (let c = 0; c < nch; c++)
      made.buf.getChannelData(c).set(S.buf.getChannelData(c).subarray(a, b));
    close();
    newSession(made.ctx, made.buf, saveAs, keep);
    say(selOnly ? `${fmt((b - a) / rate)} carved out — save it as ${saveAs}`
                : `duplicated — save it as ${saveAs}`, "ok");
  }
  function newDup(){ return newFromClip(false); }
  function newFromSel(){ return newFromClip(true); }

  function closeNew(){ const n = document.getElementById("ab-new"); if (n) n.remove(); }

  /* The chooser. Every option is drawn whether or not it can run right now, with
     the reason it cannot in its header — a card that disappears when nothing is
     open reads as a feature that does not exist. */
  function newDialog(){
    injectStyle();
    closePick(); closeNew();
    const rate = S ? S.buf.sampleRate : 0;
    const shape = S ? `${fmt(S.buf.duration)} · ${rate} Hz `
                    + (S.buf.numberOfChannels === 2 ? "stereo" : "mono") : "";
    // The same two conditions newFromClip() enforces, said up front rather than
    // as a toast after the click.
    const can = !!S && !S.staged;
    const why = !S ? "nothing is open" : "an edit is waiting to be applied";
    const canSel = can && !!S.sel;
    const host = document.createElement("div");
    host.className = "ab-pick"; host.id = "ab-new";
    host.innerHTML = `<div class="ab-pbox">
      <div class="ab-bar"><span class="ab-title">new sound</span>
        <span class="ab-sub">nothing is written until you save it</span>
        <span class="ab-spacer"></span>
        <button class="ab-btn" onclick="AudioLab.closeNew()">close</button></div>
      <div class="ab-opts">
        <div class="ab-opt">
          <div class="hd"><b>empty</b><span>silence to record, import or synthesise into</span></div>
          <div class="ab-row">
            <label>length s</label>
            <input class="ab-in num" id="ab-new-secs" type="number" step="0.1"
                   min="0.01" max="${MAX_SECONDS}" value="${newDefs.seconds}">
            <label style="min-width:30px">rate</label>
            <select class="ab-in" id="ab-new-rate">${NEW_RATES.map(r =>
              `<option value="${r}"${newDefs.rate === r ? " selected" : ""}>${r} Hz</option>`).join("")}</select>
            <select class="ab-in" id="ab-new-ch">
              <option value="1"${newDefs.channels === 1 ? " selected" : ""}>mono</option>
              <option value="2"${newDefs.channels === 2 ? " selected" : ""}>stereo</option>
            </select>
            <button class="ab-btn go" onclick="AudioLab.newBlank()">create</button>
          </div>
        </div>

        <div class="ab-opt${can ? "" : " off"}">
          <div class="hd"><b>duplicate</b><span>${can ? E(shape) : why}</span></div>
          <div class="ab-note">The clip exactly as it is now, under a new name. The
            file you opened is left alone — this is the safe way to try a second
            version. Layers stay with this session; <b>bounce</b> flattens those.</div>
          <button class="ab-btn wide"${can ? "" : " disabled"}
                  onclick="AudioLab.newDup()">duplicate the clip</button>
        </div>

        <div class="ab-opt${canSel ? "" : " off"}">
          <div class="hd"><b>from the selection</b><span>${canSel
            ? E(`${fmt((S.sel.b - S.sel.a) / rate)} selected`)
            : (can ? "no selection — drag on the waveform" : why)}</span></div>
          <div class="ab-note">Carve one hit out of a take: the selected span becomes
            a sound of its own and the take keeps every sample it had.</div>
          <button class="ab-btn wide"${canSel ? "" : " disabled"}
                  onclick="AudioLab.newFromSel()">use the selection</button>
        </div>

        <div class="ab-opt">
          <div class="hd"><b>synth</b><span>half a second, panel open</span></div>
          <div class="ab-note">Waveform, pitch sweep, envelope, noise and bitcrush —
            how a confirm blip actually gets made, with no file and no API.</div>
          <button class="ab-btn wide" onclick="AudioLab.newSound()">new from synth</button>
        </div>
      </div></div>`;
    document.body.appendChild(host);
    host.addEventListener("click", ev => { if (ev.target === host) closeNew(); });
    const f = host.querySelector("#ab-new-secs"); if (f) f.focus();
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
        <button class="ab-btn" title="Start another sound — empty, a duplicate of this clip, or the selection"
                onclick="AudioLab.newDialog()">new…</button>
        <button class="ab-btn" title="Bring a sound in from disk as the clip"
                onclick="document.getElementById('ab-file').click()">import…</button>
        <input type="file" id="ab-file" accept="audio/*" multiple style="display:none"
               onchange="AudioLab.importPicked(event,false)">
        <button class="ab-btn ab-recbtn" title="Capture from a microphone as the clip"
                onclick="AudioLab.recStart(false)">● record…</button>
        <button class="ab-btn go" id="ab-save" onclick="AudioLab.save()">save</button>
        <button class="ab-btn ab-closebtn" onclick="AudioLab.close()">close</button>
      </div>
      <div class="ab-why" id="ab-why" hidden></div>
      <div class="ab-body">
        <div class="ab-main">
          <div class="ab-modes" id="ab-modes">
            <button class="ab-mode active" data-m="clip" onclick="AudioLab.setMode('clip')">clip</button>
            <button class="ab-mode" data-m="layers" onclick="AudioLab.setMode('layers')">layers</button>
            <button class="ab-mode" data-m="studio" onclick="AudioLab.setMode('studio')">studio · beat maker</button>
          </div>
          <div class="ab-studio" id="ab-studio" hidden></div>
          <div class="ab-wave" id="ab-wave"><canvas id="ab-canvas"></canvas>
            <div class="ab-hud" id="ab-hud"></div></div>
          <div class="ab-layers" id="ab-layers" hidden><canvas id="ab-lcanvas"></canvas>
            <div class="ab-hud" id="ab-lhud"></div></div>
          <div class="ab-stage" id="ab-stage" hidden>
            <span class="lbl" id="ab-stage-label"></span>
            <span class="ab-spacer"></span>
            <button class="ab-tg" id="ab-abtg" onclick="AudioLab.toggleAB()"
                    title="Hear the original instead, without losing the edit">A/B</button>
            <button class="ab-btn go" onclick="AudioLab.applyStaged()" title="Apply (Enter)">apply</button>
            <button class="ab-btn" onclick="AudioLab.cancelStaged()" title="Cancel (Esc)">cancel</button>
          </div>
          <div class="ab-rec" id="ab-rec" hidden>
            <span class="dot"></span>
            <span class="t" id="ab-rec-t">0.000s</span>
            <div class="ab-meter" id="ab-rec-meter"><i id="ab-rec-fill"></i></div>
            <span class="note" id="ab-rec-note"></span>
            <span class="ab-spacer"></span>
            <span class="ab-sub" id="ab-rec-dest"></span>
            <button class="ab-btn go" onclick="AudioLab.recStop()">■ stop &amp; keep</button>
            <button class="ab-btn" onclick="AudioLab.recCancel()"
                    title="Throw the take away and close the microphone">discard</button>
          </div>
          <div class="ab-transport">
            <button class="ab-btn go" id="ab-play" onclick="AudioLab.togglePlay()">▶ play</button>
            <button class="ab-tg" id="ab-loopsel" onclick="AudioLab.toggleLoopSel()"
                    title="Play the selection round and round">↻ loop sel</button>
            <button class="ab-btn" onclick="AudioLab.stop()">■</button>
            <button class="ab-btn" onclick="AudioLab.selectAll()">select all</button>
            <button class="ab-btn" onclick="AudioLab.clearSel()">clear selection</button>
            <button class="ab-btn" onclick="AudioLab.zoomSel()">zoom to selection</button>
            <button class="ab-btn" onclick="AudioLab.zoomFit()">fit</button>
            <span class="ab-sub" id="ab-selinfo"></span>
          </div>
          <div class="ab-transport" id="ab-ltransport" hidden>
            <button class="ab-btn go" id="ab-lplay" onclick="AudioLab.toggleStack()"
                    title="Hear the clip and every layer together (Space)">▶ play stack</button>
            <button class="ab-btn" onclick="AudioLab.stop()">■</button>
            <button class="ab-tg" id="ab-ltool-move" onclick="AudioLab.setLayerTool('move')"
                    title="Drag a lane to slide it in time — hold Alt for the other tool">⇔ move</button>
            <button class="ab-tg" id="ab-ltool-sel" onclick="AudioLab.setLayerTool('select')"
                    title="Drag across a lane to select a span of it — hold Alt for the other tool">▭ select</button>
            <button class="ab-tg" id="ab-lclipm" onclick="AudioLab.clipLaneField('muted')"
                    title="Mute the clip lane">clip M</button>
            <button class="ab-tg" id="ab-lclips" onclick="AudioLab.clipLaneField('solo')"
                    title="Solo the clip lane">clip S</button>
            <button class="ab-btn" onclick="AudioLab.addTrack()">+ layer a sound</button>
            <button class="ab-btn" title="Save a file from disk into the project and layer it"
                    onclick="document.getElementById('ab-lfile').click()">+ import a file</button>
            <input type="file" id="ab-lfile" accept="audio/*" multiple style="display:none"
                   onchange="AudioLab.importPicked(event,true)">
            <button class="ab-btn ab-recbtn" title="Record a take into the project and layer it"
                    onclick="AudioLab.recStart(true)">● record a take</button>
            <button class="ab-btn" onclick="AudioLab.splitLayer()"
                    title="Cut the focused layer in two where the playhead is">✂ split at playhead</button>
            <button class="ab-btn" onclick="AudioLab.layersFit()">fit</button>
            <span id="ab-lrange" hidden>
              <button class="ab-btn" onclick="AudioLab.rangeTrim()"
                      title="Keep only the selected span of this layer">⇥ trim to range</button>
              <button class="ab-btn" onclick="AudioLab.rangeRemove()"
                      title="Drop the selected span; a cut in the middle splits the lane in two">✂ remove range</button>
              <button class="ab-btn" onclick="AudioLab.rangePlay()"
                      title="Hear just this layer over just this span">▶ play range</button>
              <button class="ab-tg" onclick="AudioLab.clearRange()"
                      title="Escape does this too">clear range</button>
            </span>
            <span class="ab-sub" id="ab-linfo"></span>
          </div>
        </div>
        <div class="ab-side" id="ab-side"></div>
      </div>`;
    if (_host) { back.classList.add("ab-embed"); _host.innerHTML = ""; _host.appendChild(back); }
    else document.body.appendChild(back);
    $ = { back, name: back.querySelector("#ab-name"),
          wave: back.querySelector("#ab-wave"), canvas: back.querySelector("#ab-canvas"),
          hud: back.querySelector("#ab-hud"), side: back.querySelector("#ab-side"),
          selinfo: back.querySelector("#ab-selinfo"), play: back.querySelector("#ab-play"),
          layers: back.querySelector("#ab-layers"), lcanvas: back.querySelector("#ab-lcanvas"),
          lhud: back.querySelector("#ab-lhud"), linfo: back.querySelector("#ab-linfo") };
    $.ctx2d = $.canvas.getContext("2d");
    $.lctx2d = $.lcanvas.getContext("2d");
    bindWave();
    bindLayers();
    bindDrop(back);
    syncLoopBtn();
    document.addEventListener("keydown", onKey, true);
    if (window.ResizeObserver){
      S.ro = new ResizeObserver(() => paint());
      S.ro.observe($.wave);
      S.lro = new ResizeObserver(() => paintLayers());
      S.lro.observe($.layers);
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
  /* The buffer you are looking at and listening to, which is not always the
     buffer the ops read. A staged edit takes over the canvas, the HUD and the
     transport; A/B flips back to the original without dropping it. The DSP
     deliberately keeps reading S.buf, so restaging at a different amount
     recomputes from the original instead of compounding on the last preview. */
  function viewBuf(){ return S.staged && !S.staged.ab ? S.staged.buf : S.buf; }

  // Takes the buffer explicitly: layers mode draws a different buffer per lane,
  // and reading viewBuf() internally made this only ever able to draw the clip.
  function peaks(buf, channel, from, to, width){
    const data = buf.getChannelData(channel);
    const step = (to - from) / width;
    const out = new Float32Array(width * 2);
    for (let x = 0; x < width; x++){
      const s = Math.floor(from + x * step);
      const e = Math.min(buf.length, Math.max(s + 1, Math.floor(from + (x + 1) * step)));
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

  // A ground change invalidates every colour already painted into the canvas.
  // BGTheme.flush() has already run by the time this fires (same event, earlier
  // listener), so paint() picks up the new values.
  try{ window.addEventListener("bgate:theme", () => {
    try{ paint(); }catch(e){}
    try{ paintLayers(); }catch(e){}
  }); }catch(e){}

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
    c.fillStyle = BGTheme.color("--bg"); c.fillRect(0, 0, W, H);

    const vb = viewBuf();
    const ch = vb.numberOfChannels;
    const laneH = H / ch;
    const { from, to } = S.view;
    const width = Math.max(1, Math.floor(W));

    // selection band, under the waveform
    let selX = null;
    if (S.sel){
      const x0 = sampleToX(S.sel.a, W), x1 = sampleToX(S.sel.b, W);
      selX = [Math.min(x0, x1), Math.max(x0, x1)];
      c.fillStyle = BGTheme.color("--accent-soft");
      c.fillRect(selX[0], 0, selX[1] - selX[0], H);
    }

    for (let k = 0; k < ch; k++){
      const top = k * laneH, mid = top + laneH / 2;
      // The zero line and the channel divider were baked near-white — invisible
      // on the light ground, which is the whole reason BGTheme.color exists.
      c.strokeStyle = BGTheme.color("--line");
      c.beginPath(); c.moveTo(0, mid); c.lineTo(W, mid); c.stroke();
      const p = peaks(vb, k, from, to, width);
      c.strokeStyle = BGTheme.color("--accent-hover");
      c.beginPath();
      for (let x = 0; x < width; x++){
        const lo = p[x * 2], hi = p[x * 2 + 1];
        const y0 = mid - hi * (laneH / 2 - 3);
        const y1 = mid - lo * (laneH / 2 - 3);
        c.moveTo(x + .5, y0); c.lineTo(x + .5, Math.max(y1, y0 + .6));
      }
      c.stroke();
      if (k){
        c.strokeStyle = BGTheme.color("--line-strong");
        c.beginPath(); c.moveTo(0, top + .5); c.lineTo(W, top + .5); c.stroke();
      }
    }

    // Grips, over the waveform. An edge you cannot see is an edge nobody drags.
    if (selX){
      c.fillStyle = BGTheme.color("--accent");
      c.fillRect(Math.round(selX[0]) - 1, 0, 2, H);
      c.fillRect(Math.round(selX[1]) - 1, 0, 2, H);
    }

    // Godot loop markers — the setting you cannot hear, drawn where you can see it.
    if (S.loop && S.loop.enabled){
      const rate = S.buf.sampleRate;
      const b = sampleToX(S.loop.begin_s * rate, W);
      c.strokeStyle = BGTheme.color("--info"); c.lineWidth = 1.5;
      c.beginPath(); c.moveTo(b, 0); c.lineTo(b, H); c.stroke();
      c.fillStyle = BGTheme.color("--info"); c.font = "10px ui-monospace,monospace";
      c.fillText("loop", b + 4, 12);
      if (S.loop.end_s != null){
        const e = sampleToX(S.loop.end_s * rate, W);
        c.beginPath(); c.moveTo(e, 0); c.lineTo(e, H); c.stroke();
        c.fillText("end", e + 4, 12);
      }
      c.lineWidth = 1;
    }

    if (S.play){
      let t = S.playFrom + (S.ctx.currentTime - S.playStart) * S.buf.sampleRate;
      // A looping source wraps at loopEnd; the playhead has to wrap with it or
      // it runs off the end while the sound is still going round.
      if (S.play.loop && S.sel){
        const w = S.sel.b - S.sel.a;
        if (w > 0 && t > S.sel.b) t = S.sel.a + ((t - S.sel.a) % w);
      }
      const x = sampleToX(t, W);
      c.strokeStyle = BGTheme.color("--text");
      c.beginPath(); c.moveTo(x, 0); c.lineTo(x, H); c.stroke();
      paint();                                  // keep the playhead moving
    }

    const rate = vb.sampleRate;
    $.hud.textContent = [
      `${fmt(vb.length / rate)}  ·  ${rate}Hz  ·  ${ch === 2 ? "stereo" : "mono"}`,
      `view ${fmt(from / rate)}–${fmt(to / rate)}`,
    ].join("   ");
    if ($.selinfo){
      $.selinfo.textContent = S.sel
        ? `selection ${fmt(S.sel.a / rate)} → ${fmt(S.sel.b / rate)}  (${fmt((S.sel.b - S.sel.a) / rate)})`
        : "no selection — edits apply to the whole clip";
    }
    refreshHistory();
    syncStage();
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

  /* ── selection ────────────────────────────────────────────────────────── */
  /* The selection used to exist only as a drag: you could not type a boundary,
     could not adjust one without redrawing it, and every cut landed wherever
     the pointer happened to be. It is an object now — typed, draggable by
     either edge, nudgeable, and snapped to a zero crossing. */

  const EDGE_PX = 6;                    // grab radius for an edge, in CSS px

  function edgeAt(x, W){
    if (!S || !S.sel) return null;
    const w = W == null ? $.canvas.width / (S.dpr || 1) : W;
    const da = Math.abs(sampleToX(S.sel.a, w) - x);
    const db = Math.abs(sampleToX(S.sel.b, w) - x);
    if (da <= EDGE_PX && da <= db) return "a";
    if (db <= EDGE_PX) return "b";
    return null;
  }

  /* Butt-joining at a non-zero sample is what makes a trimmed hit click: the
     waveform steps to silence in one sample and that step IS a transient.
     Searches outward up to 3 ms for a sign change in the summed channels. */
  function snapZero(i){
    if (!S) return i;
    const len = S.buf.length;
    i = clamp(Math.round(i), 0, len);
    const win = Math.max(1, Math.round(S.buf.sampleRate * 0.003));
    const ch = S.buf.numberOfChannels;
    const data = [];
    for (let c = 0; c < ch; c++) data.push(S.buf.getChannelData(c));
    const sum = k => { let v = 0; for (let c = 0; c < ch; c++) v += data[c][k]; return v; };
    const cross = k => {
      if (k <= 0 || k >= len) return false;
      const a = sum(k - 1), b = sum(k);
      return (a <= 0 && b > 0) || (a >= 0 && b < 0);
    };
    for (let d = 0; d <= win; d++){
      if (cross(i - d)) return i - d;
      if (cross(i + d)) return i + d;
    }
    return i;
  }

  function snapSel(){
    if (!S || !S.sel || !S.ui || !S.ui.snap) return;
    const a = snapZero(S.sel.a), b = snapZero(S.sel.b);
    if (b - a >= 1) S.sel = { a, b };
  }

  function bindWave(){
    const el = $.canvas;
    el.addEventListener("pointerdown", ev => {
      if (!S) return;
      el.setPointerCapture(ev.pointerId);
      const r = el.getBoundingClientRect();
      const x = ev.clientX - r.left;
      const W = $.canvas.width / (S.dpr || 1);
      const e = edgeAt(x, W);
      if (e){
        // Grab the boundary you already made instead of throwing it away.
        S.drag = { mode: "edge", edge: e, anchor: e === "a" ? S.sel.b : S.sel.a };
        return;
      }
      S.drag = { mode: "new", a: xToSample(x) };
      S.sel = null;
      paint();
    });
    el.addEventListener("pointermove", ev => {
      if (!S) return;
      const r = el.getBoundingClientRect();
      const x = ev.clientX - r.left;
      if (!S.drag){
        const W = $.canvas.width / (S.dpr || 1);
        el.style.cursor = edgeAt(x, W) ? "col-resize" : "text";
        return;
      }
      const p = xToSample(x);
      if (S.drag.mode === "edge"){
        const an = S.drag.anchor;
        if (p !== an){
          S.sel = { a: Math.min(an, p), b: Math.max(an, p) };
          S.drag.edge = p < an ? "a" : "b";   // crossing the anchor flips the edge you hold
        }
      } else {
        S.sel = p === S.drag.a ? null
              : { a: Math.min(S.drag.a, p), b: Math.max(S.drag.a, p) };
      }
      paint();
    });
    // Snap on release only: snapping mid-drag makes the edge jump under the
    // pointer and you can never land where you meant to.
    const end = () => { if (S){ if (S.drag) snapSel(); S.drag = null; renderSide(); paint(); } };
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

  /* Typed boundaries. `S.ui.units` decides what the three boxes mean; samples
     are the only thing S.sel ever holds. */
  function unitScale(){
    const u = (S.ui && S.ui.units) || "s";
    return u === "samples" ? 1 : u === "ms" ? S.buf.sampleRate / 1000 : S.buf.sampleRate;
  }
  function unitStep(){
    const u = (S.ui && S.ui.units) || "s";
    return u === "samples" ? 1 : u === "ms" ? 0.1 : 0.001;
  }
  function toUnits(samples){
    const u = (S.ui && S.ui.units) || "s";
    if (u === "samples") return String(Math.round(samples));
    const v = samples / unitScale();
    return u === "ms" ? v.toFixed(1) : v.toFixed(4);
  }
  /* Writes the boxes back without re-rendering the panel: renderSide() replaces
     the very input the value was typed into, which blurs it mid-edit. */
  function syncSelFields(){
    const s = S.sel;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    set("ab-sel-a", toUnits(s ? s.a : 0));
    set("ab-sel-b", toUnits(s ? s.b : S.buf.length));
    set("ab-sel-len", toUnits(s ? s.b - s.a : 0));
  }
  /* No selection reads as the whole clip everywhere else here, so an in point
     typed against nothing has to mean "from there to the end", not "to zero". */
  function selField(which, v){
    if (!S) return;
    const n = parseFloat(v);
    const len = S.buf.length;
    if (!isFinite(n)){ syncSelFields(); paint(); return; }
    const cur = S.sel || { a: 0, b: len };
    const at = clamp(Math.round(n * unitScale()), 0, len);
    let a = cur.a, b = cur.b;
    if (which === "a") a = at;
    else if (which === "b") b = at;
    else b = clamp(a + at, 0, len);          // length moves the out point, keeps the in
    if (b < a){ const t = a; a = b; b = t; }
    S.sel = b - a < 1 ? null : { a, b };     // a zero-length selection is no selection
    snapSel();                                // snap after entry, never during a drag
    syncSelFields(); paint();
  }
  function selUnits(u){
    if (!S) return;
    S.ui.units = (u === "ms" || u === "samples") ? u : "s";
    renderSide(); paint();
  }
  function toggleSnap(){ if (S){ S.ui.snap = !S.ui.snap; renderSide(); paint(); } }

  /* Nudge. 1 ms a press, 10 ms with Shift; Alt moves the in point instead of
     the out, Ctrl/Cmd slides the whole selection. */
  function nudgeSel(dir, ms, moveStart, whole){
    if (!S) return;
    const len = S.buf.length;
    const d = dir * Math.max(1, Math.round(S.buf.sampleRate * ms / 1000));
    const cur = S.sel || { a: 0, b: len };
    let a = cur.a, b = cur.b;
    if (whole){
      const w = b - a;
      a = clamp(a + d, 0, Math.max(0, len - w));
      b = Math.min(len, a + w);
    } else if (moveStart){
      a = clamp(a + d, 0, len);
    } else {
      b = clamp(b + d, 0, len);
    }
    if (b < a){ const t = a; a = b; b = t; }
    S.sel = b - a < 1 ? null : { a, b };
    renderSide(); paint();
  }

  function onKey(ev){
    if (!S) return;
    // A question drawn over the pane owns the keyboard. This listener is on
    // document in the CAPTURE phase, so it runs before anything inside the
    // dialog — without this, Space on its buttons plays the clip instead of
    // pressing them and Escape closes the editor out from under the question.
    if (document.querySelector(".ask-scrim, .ask-inline")) return;
    const t = ev.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)){
      if (ev.key === "Escape") t.blur();
      return;
    }
    const k = ev.key.toLowerCase();
    // A sheet over the pane owns Escape before the pane does — otherwise the
    // chooser's own Escape closed the whole editor behind it.
    if (ev.key === "Escape" && document.getElementById("ab-new")){
      ev.preventDefault(); closeNew(); return;
    }
    // A pending audition owns Enter and Escape — before Escape can close the
    // overlay out from under an edit that has not been applied yet.
    if (S.staged && (ev.key === "Enter" || ev.key === "Escape")){
      ev.preventDefault();
      if (ev.key === "Enter") applyStaged(); else cancelStaged();
      return;
    }
    // A live range owns Escape before the pane does, the same way the staged
    // edit above does: dismissing the selection is what Escape means for a
    // selection everywhere else here, and closing the editor instead would take
    // the arrangement with it.
    if (ev.key === "Escape" && S.mode === "layers" && S.lrange){
      ev.preventDefault(); clearRange(); return;
    }
    // Overlay only. Embedded, close() strips #ab-back without restoring the
    // landing markup embed() wrote, leaving the Studio tab blank for good.
    if (ev.key === "Escape" && !_host){ ev.preventDefault(); closeAsk(); return; }
    // Space is "play what I am looking at": in layers that is the whole stack,
    // not the clip on its own.
    if (ev.key === " "){
      ev.preventDefault();
      if (S.mode === "layers") toggleStack(); else togglePlay();
      return;
    }
    if ((ev.ctrlKey || ev.metaKey) && k === "z"){
      ev.preventDefault(); ev.shiftKey ? redo() : undo(); return; }
    if ((ev.ctrlKey || ev.metaKey) && k === "s"){ ev.preventDefault(); save(); return; }
    if ((ev.ctrlKey || ev.metaKey) && k === "a"){ ev.preventDefault(); selectAll(); return; }
    if (ev.key === "ArrowLeft" || ev.key === "ArrowRight"){
      ev.preventDefault();
      nudgeSel(ev.key === "ArrowLeft" ? -1 : 1, ev.shiftKey ? 10 : 1,
               ev.altKey, ev.ctrlKey || ev.metaKey);
      return;
    }
    // Collapse to a clip edge. A zero-length selection is no selection, so this
    // is also how you get back to "the edits apply to the whole clip" — the
    // zoom is kept and moved to that end rather than thrown away.
    if (ev.key === "Home" || ev.key === "End"){
      ev.preventDefault();
      S.sel = null;
      const span = Math.min(S.buf.length, S.view.to - S.view.from);
      S.view = ev.key === "Home" ? { from: 0, to: span }
                                 : { from: S.buf.length - span, to: S.buf.length };
      renderSide(); paint();
      return;
    }
    if (ev.key === "Delete" || ev.key === "Backspace"){ ev.preventDefault(); cut(); return; }
  }

  /* ── playback ─────────────────────────────────────────────────────────── */
  function togglePlay(){ S && (S.play ? stop() : play()); }
  function play(){
    if (!S) return;
    stop();
    try { S.ctx.resume(); } catch (e) {}
    const vb = viewBuf();
    const src = S.ctx.createBufferSource();
    src.buffer = vb;
    src.connect(S.ctx.destination);
    const rate = vb.sampleRate;
    // A staged length-changing edit (speed, repeat, insert) is shorter or longer
    // than the selection was drawn against, so clamp before it reaches start():
    // an offset past the end of a buffer plays nothing at all.
    // A selection that lands entirely past the end of a shortened staged buffer
    // would start(0, end, 0) and play silence, which reads as a broken button.
    let a = S.sel ? Math.min(S.sel.a, vb.length) : 0;
    let b = S.sel ? Math.min(S.sel.b, vb.length) : vb.length;
    if (b - a < 1){ a = 0; b = vb.length; }
    const ranged = b - a < vb.length;
    const loopSel = !!(ranged && S.ui && S.ui.loopSel);
    const from = a;
    src.onended = () => { if (S && S.play === src){ S.play = null; syncPlay(); paint(); } };
    if (loopSel){
      src.loop = true;
      src.loopStart = a / rate;
      src.loopEnd = b / rate;
      // A looping source ignores the duration argument and stops at it anyway
      // on some engines — start with no duration and let loopEnd do the work.
      src.start(0, src.loopStart);
    } else {
      src.start(0, from / rate, ranged ? (b - a) / rate : undefined);
    }
    S.play = src; S.playFrom = from; S.playStart = S.ctx.currentTime;
    syncPlay(); paint();
  }
  function toggleLoopSel(){
    if (!S) return;
    S.ui.loopSel = !S.ui.loopSel;
    syncLoopBtn();
    if (S.play) play();          // a running source cannot be told to loop; re-arm it
  }
  function syncLoopBtn(){
    const b = document.getElementById("ab-loopsel");
    if (b) b.classList.toggle("on", !!(S && S.ui && S.ui.loopSel));
  }
  function stop(){
    if (!S) return;
    stopPreview();
    stopStack();                 // every path that silences the lab silences the stack
    if (!S.play) return;
    try { S.play.onended = null; S.play.stop(); } catch (e) {}
    S.play = null; syncPlay(); paint();
  }
  function syncPlay(){ if ($.play) $.play.textContent = S && S.play ? "❚❚ pause" : "▶ play"; }

  /* ── history ──────────────────────────────────────────────────────────── */
  function bufBytes(b){ return b.length * b.numberOfChannels * 4; }
  /* A history entry is the buffer plus where you were looking at it. Pushing a
     bare buffer meant undo landed you at the whole clip with nothing selected. */
  function frame(){
    return { buf: S.buf, sel: S.sel && { a: S.sel.a, b: S.sel.b },
             view: { from: S.view.from, to: S.view.to } };
  }
  function restoreFrame(e){
    const len = S.buf.length;
    S.sel = null;
    if (e.sel){
      const a = clamp(e.sel.a, 0, len), b = clamp(e.sel.b, 0, len);
      if (b - a >= 1) S.sel = { a, b };
    }
    const from = clamp(e.view ? e.view.from : 0, 0, len);
    const to = clamp(e.view ? e.view.to : len, 0, len);
    S.view = to - from < 64 ? { from: 0, to: len } : { from, to };
  }
  function snapshot(){
    if (!S) return;
    S.undo.push(frame());
    S.undoBytes += bufBytes(S.buf);
    while (S.undoBytes > UNDO_BYTES && S.undo.length > 1)
      S.undoBytes -= bufBytes(S.undo.shift().buf);
    S.redo.length = 0;
  }

  /* Every edit used to end with `S.sel = null` and the view reset to the whole
     clip, so zooming to a 40 ms transient and fading it threw both away — an edit
     could only be redone from scratch, never refined. An op now describes the
     splice it performed and the selection and zoom are carried across it. */
  function mapIndex(i, remap){
    const at = remap.at, removed = remap.removed, inserted = remap.inserted;
    if (i <= at) return i;
    if (i >= at + removed) return i + inserted - removed;
    return at + inserted;              // inside the replaced span: collapse to its end
  }
  function mapRange(r, remap, len){
    if (!r) return null;
    const n = len == null ? S.buf.length : len;
    const a = clamp(mapIndex(r.a, remap), 0, n);
    const b = clamp(mapIndex(r.b, remap), 0, n);
    return b - a < 1 ? null : { a, b };
  }

  /* `remap` is {at, removed, inserted} describing the splice, the string "reset"
     when the whole clip was replaced, or nothing at all for the equal-length ops
     (silence, fade, gain, normalise, reverse) where every index still points at
     the same sample. */
  function commit(buf, label, remap){
    stop();
    const resized = buf.length !== S.buf.length;
    S.buf = buf;
    S.dirty = true;
    if (remap === "reset" || (resized && !remap)){
      S.sel = null;
      S.view = { from: 0, to: buf.length };
    } else {
      if (remap){
        S.sel = mapRange(S.sel, remap, buf.length);
        const v = mapRange({ a: S.view.from, b: S.view.to }, remap, buf.length);
        S.view = v ? { from: v.a, to: v.b } : { from: 0, to: buf.length };
      }
      const from = clamp(S.view.from, 0, buf.length), to = clamp(S.view.to, 0, buf.length);
      // A view that collapsed to a handful of samples is unreadable; fit instead.
      S.view = to - from < 64 ? { from: 0, to: buf.length } : { from, to };
      if (S.sel){
        const a = clamp(S.sel.a, 0, buf.length), b = clamp(S.sel.b, 0, buf.length);
        S.sel = b - a < 1 ? null : { a, b };
      }
    }
    renderSide(); paint();
    if (label) say(label, "ok");
  }

  /* ── staging ──────────────────────────────────────────────────────────── */
  /* Every op used to be snapshot → mutate → commit, so the only way to hear a
     fade was to perform it and undo it. With an amount on every effect that
     loop is commit-listen-undo-adjust-repeat, which is worse than no controls
     at all. A parameterised op now renders into S.staged and waits: the pane
     draws and plays the staged buffer, A/B flips back to the original, apply
     commits it and cancel drops it. Structural ops (trim, delete, silence,
     reverse, to mono, mixdown, adopt, the synth) commit as before — they are
     not judged by ear.

     A staged edit holds a SECOND full buffer alive (~170 KB per second per
     channel), on top of the undo history. Cancel drops it, apply hands it over,
     and a second stage() replaces the first — no snapshot is taken until apply,
     so a replaced staging leaks nothing into the history. */
  function stage(buf, label, remap, sel){
    if (!S) return;
    stop();
    S.staged = { buf, label, remap, sel: sel || null, ab: false };
    renderSide(); paint();
  }
  /* Some ops want the selection to land on what they just made (the silence you
     inserted, the block you repeated). Held on the staged entry and applied
     after the commit, because until then the clip is still the old length. */
  function selAfter(sel){
    if (!S || !sel) return;
    const len = S.buf.length;
    const a = clamp(sel.a, 0, len), b = clamp(sel.b, 0, len);
    S.sel = b - a < 1 ? null : { a, b };
    renderSide(); paint();
  }
  function applyStaged(){
    if (!S || !S.staged) return;
    const e = S.staged;
    S.staged = null;
    snapshot();
    commit(e.buf, e.label, e.remap);
    selAfter(e.sel);
  }
  function cancelStaged(){
    if (!S || !S.staged) return;
    stop();
    S.staged = null;
    renderSide(); paint();
  }
  function toggleAB(){
    if (!S || !S.staged) return;
    S.staged.ab = !S.staged.ab;
    paint();
    play();          // a running source cannot be swapped; re-arm it so the
  }                  // comparison is immediate rather than "press play again"
  function syncStage(){
    const bar = document.getElementById("ab-stage");
    if (!bar) return;
    const e = S && S.staged;
    bar.hidden = !e;
    if (!e) return;
    const l = document.getElementById("ab-stage-label");
    if (l) l.textContent = (e.ab ? "comparing against the original · " : "auditioning ") + e.label;
    const t = document.getElementById("ab-abtg");
    if (t) t.classList.toggle("on", !!e.ab);
  }

  /* The shared tail of every op that takes an amount. Preview on (the default)
     stages the result; off commits it the way it always did. */
  function deliver(buf, label, remap, sel){
    if (!S) return;
    if (!S.ui || S.ui.preview !== false){ stage(buf, label, remap, sel); return; }
    snapshot();
    commit(buf, label, remap);
    selAfter(sel);
  }

  function undo(){
    if (!S || !S.undo.length) return;
    cancelStaged();          // an uncommitted edit has no place in the history
    stop();
    S.redo.push(frame());
    const e = S.undo.pop();
    S.undoBytes -= bufBytes(e.buf);
    S.buf = e.buf;
    S.dirty = true;
    restoreFrame(e);
    renderSide(); paint();
  }
  function redo(){
    if (!S || !S.redo.length) return;
    cancelStaged();
    stop();
    S.undo.push(frame());
    S.undoBytes += bufBytes(S.buf);
    const e = S.redo.pop();
    S.buf = e.buf;
    S.dirty = true;
    restoreFrame(e);
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
    commit(out, `trimmed to ${fmt((b - a) / S.buf.sampleRate)}`, "reset");
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
    commit(out, `removed ${fmt((b - a) / S.buf.sampleRate)}`,
           { at: a, removed: b - a, inserted: 0 });
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

  /* The fade-in multiplier at t∈[0,1]; a fade-out is the same shape read
     backwards. A linear fade-out on a tail sounds like it stops early — the ear
     reads level in dB, so log or equal-power is what a tail actually wants. */
  function curveAt(t, curve){
    switch (curve){
      case "equal-power": return Math.sin(t * Math.PI / 2);
      case "exponential": return t * t;
      case "logarithmic": return Math.sqrt(t);
      case "s-curve":     return t * t * (3 - 2 * t);
      default:            return t;             // linear
    }
  }
  const FADES = ["linear", "equal-power", "exponential", "logarithmic", "s-curve"];

  function fade(dir, curve){
    const shape = curve || (S.ui && S.ui.fade_curve) || "linear";
    const [a, b] = range();
    const n = b - a;
    if (n < 2){ say("select something to fade"); return; }
    const out = copyBuf();
    eachChannel(out, d => {
      for (let i = 0; i < n; i++){
        const t = i / (n - 1);
        d[a + i] *= dir === "in" ? curveAt(t, shape) : curveAt(1 - t, shape);
      }
    });
    deliver(out, `faded ${dir} · ${shape}`);
  }

  function gain(db){
    const v = Number(db != null ? db : (S.ui && S.ui.gain_db));
    if (!isFinite(v) || Math.abs(v) < 1e-6) return;   // 0 dB is a no-op, not an edit
    const [a, b] = range();
    const f = Math.pow(10, v / 20);
    const out = copyBuf();
    eachChannel(out, d => {
      for (let i = a; i < b; i++) d[i] = clamp(d[i] * f, -1, 1);
    });
    deliver(out, `${v > 0 ? "+" : ""}${v} dB`);
  }

  function normalize(targetDb){
    const [a, b] = range();
    const t = Number(targetDb != null ? targetDb : (S.ui && S.ui.norm_db));
    const target = isFinite(t) ? clamp(t, -60, 0) : -1;
    const amp = Math.pow(10, target / 20);
    const per = !!(S.ui && S.ui.norm_per_channel);
    const peaks = [];
    let all = 0;
    for (let c = 0; c < S.buf.numberOfChannels; c++){
      const d = S.buf.getChannelData(c);
      let p = 0;
      for (let i = a; i < b; i++){ const v = Math.abs(d[i]); if (v > p) p = v; }
      peaks.push(p);
      if (p > all) all = p;
    }
    if (all < 1e-6){ say("that selection is silent"); return; }
    const out = copyBuf();
    // Per channel each side reaches the target on its own, which rescues a
    // lopsided stereo take but moves the image; one factor keeps the image.
    eachChannel(out, (d, c) => {
      const peak = per ? peaks[c] : all;
      if (peak < 1e-6) return;
      const f = amp / peak;
      for (let i = a; i < b; i++) d[i] = clamp(d[i] * f, -1, 1);
    });
    const applied = 20 * Math.log10(amp / all);
    deliver(out, per
      ? `normalised each channel to ${target.toFixed(1)} dBFS`
      : `normalised to ${target.toFixed(1)} dBFS (${applied > 0 ? "+" : ""}${applied.toFixed(1)} dB)`);
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

  /* Note the order in this and the two ops below: snapshot() goes AFTER the
     destination buffer exists. createBuffer throws on a length the field lets
     you type, and snapshotting first left an undo entry with no matching edit
     — "undo" then marked a clean file unsaved and changed nothing. */
  function insertSilence(seconds){
    const secs = clamp(Number(seconds) || 0, 0, 60);   // matches the input's range
    const n = Math.round(secs * S.buf.sampleRate);
    if (n <= 0){ say("give it a positive length"); return; }
    const at = S.sel ? S.sel.a : S.buf.length;
    const out = make(S.buf.length + n);
    eachChannel(out, (d, c) => {
      const src = S.buf.getChannelData(c);
      d.set(src.subarray(0, at), 0);
      d.set(src.subarray(at), at + n);
    });
    // the silence you just made is what you want selected next
    deliver(out, `inserted ${fmt(secs)} of silence`,
            { at, removed: 0, inserted: n }, { a: at, b: at + n });
  }

  /* Repeat the selection (or the whole clip) N times. With a crossfade this is
   * how a 30-second loop becomes 90 seconds without a seam — butt-joining two
   * copies of a musical phrase clicks, and the click is what makes looped
   * music sound cheap. */
  function repeat(times, crossfadeMs){
    const [a, b] = range();
    const n = b - a;
    const reps = clamp(Math.round(Number(times) || 2), 2, 64);  // the input's
    // max= is not enforced on a typed value, and the public API calls repeat(3)
    // with no crossfade at all — an undefined there used to reach createBuffer
    // as a NaN length.
    const ms = Number(crossfadeMs) || 0;
    const xf = clamp(Math.round((ms / 1000) * S.buf.sampleRate), 0,
                     Math.floor(n / 2));
    if (n < 2){ say("select something to repeat"); return; }
    const total = n * reps - xf * (reps - 1);
    // Splice the repeated block back between the untouched head and tail. Sizing
    // the output from the selection alone silently deleted everything outside it.
    const out = make(S.buf.length - n + total);
    eachChannel(out, (d, c) => {
      const src = S.buf.getChannelData(c);
      d.set(src.subarray(0, a), 0);
      for (let r = 0; r < reps; r++){
        const at = r * (n - xf);
        for (let i = 0; i < n; i++){
          const v = src[a + i];
          const pos = at + i;
          if (pos >= total) break;
          if (r > 0 && i < xf){
            const t = i / xf;                  // equal-power keeps the level flat
            d[a + pos] = d[a + pos] * Math.cos(t * Math.PI / 2) + v * Math.sin(t * Math.PI / 2);
          } else {
            d[a + pos] = v;
          }
        }
      }
      d.set(src.subarray(b), a + total);
    });
    deliver(out, `repeated ×${reps}${xf ? ` with a ${ms}ms crossfade` : ""}`,
            { at: a, removed: n, inserted: total }, { a, b: a + total });
  }

  /* Resample. Speed and pitch move together, exactly like pitching a tape —
   * which is the effect you want for making a big version of a small hit. */
  function speed(factor){
    const f = clamp(Number(factor) || 1, 0.25, 4);
    if (Math.abs(f - 1) < 1e-3) return;
    // Selection-scoped like every other op: this used to resample the whole clip
    // while the panel beside it printed the selection range it was ignoring.
    const [a, b] = range();
    const n = b - a;
    if (n < 2){ say("select something to resample"); return; }
    const m = Math.max(2, Math.floor(n / f));
    const out = make(S.buf.length - n + m);
    eachChannel(out, (d, c) => {
      const src = S.buf.getChannelData(c);
      d.set(src.subarray(0, a), 0);
      for (let i = 0; i < m; i++){
        const x = i * f;
        const k = Math.floor(x), frac = x - k;
        const i0 = a + k;
        const s0 = i0 < b ? src[i0] : 0;
        const s1 = i0 + 1 < b ? src[i0 + 1] : s0;
        d[a + i] = s0 + (s1 - s0) * frac;      // linear is enough at these ratios
      }
      d.set(src.subarray(b), a + m);
    });
    deliver(out, `${f < 1 ? "slowed" : "sped up"} ×${f.toFixed(2)}`,
            { at: a, removed: n, inserted: m }, { a, b: a + m });
  }

  function toMono(){
    const nc = S.buf.numberOfChannels;
    if (nc === 1){ say("already mono"); return; }
    snapshot();
    const out = make(S.buf.length, 1);
    const d = out.getChannelData(0);
    // Sum every channel. Hardcoding L/R threw away channels 3+ of a quad or 5.1
    // file while still toasting "mixed down to mono".
    for (let c = 0; c < nc; c++){
      const s = S.buf.getChannelData(c);
      for (let i = 0; i < d.length; i++) d[i] += s[i] / nc;
    }
    commit(out, "mixed down to mono", "reset");
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

  /* The preview node has to be reachable from the stop paths. Untracked, every
     click stacked another copy that nothing — transport, spacebar, a mode
     change — could silence short of reloading the page. */
  function synthPreview(){
    const buf = synthRender();
    try { S.ctx.resume(); } catch (e) {}
    stopPreview();
    const src = S.ctx.createBufferSource();
    src.buffer = buf; src.connect(S.ctx.destination); src.start();
    src.onended = () => { if (S && S.preview === src) S.preview = null; };
    S.preview = src;
  }
  function stopPreview(){
    if (!S || !S.preview) return;
    try { S.preview.onended = null; S.preview.stop(); } catch (e) {}
    S.preview = null;
  }
  function synthReplace(){ snapshot(); commit(synthRender(), "synthesised", "reset"); }
  function synthAppend(){
    const add = synthRender();
    snapshot();
    const out = make(S.buf.length + add.length);
    eachChannel(out, (d, c) => {
      d.set(S.buf.getChannelData(c), 0);
      d.set(add.getChannelData(0), S.buf.length);
    });
    commit(out, "appended", "reset");
  }
  function synthField(key, v){
    S.synth[key] = key === "wave" ? v : parseFloat(v);
    if (key === "wave"){ renderSide(); return; }
    // Patch the readout in place. renderSide() here would replace the slider
    // mid-drag and kill it, so the printed number used to just sit frozen.
    const el = document.getElementById("ab-sv-" + key);
    if (el) el.textContent = S.synth[key];
  }

  /* ── mixer ────────────────────────────────────────────────────────────── */
  async function addTrack(rel){
    if (!rel){
      rel = await pickSound("layer which sound?");
      if (!rel) return;
      if (!S) return;                    // the pane can be closed under the picker
    }
    // in_s/out_s are seconds into the SOURCE file — the kept region, which
    // lands at offset_s. null out_s is "to the end", so a fresh layer plays
    // whole and nothing on disk is ever touched by a trim.
    S.tracks.push({ source: rel, name: rel.split("/").pop(), offset_s: 0,
                    in_s: 0, out_s: null,
                    gain_db: 0, pan: 0, muted: false, solo: false, reverse: false });
    focusLane(S.tracks.length);           // focus what you just added
    ensureLayerBuf(rel);
    renderSide();
    paintLayers();
  }
  /* The numeric fields are bound to oninput, and renderSide() replaces the whole
     panel — so re-rendering here destroyed the very box being typed into. Only
     the toggles, which change what the panel draws, may re-render. */
  function trackField(i, key, v){
    const t = S.tracks[i];
    if (!t) return;
    // Typing in the in/out/offset boxes moves the block a range was measured
    // against, and reverse flips which end of the source it points at.
    if (key !== "gain_db" && key !== "pan") dropRange(i + 1);
    if (key === "muted" || key === "solo" || key === "reverse"){
      t[key] = !!v; renderSide(); paintLayers(); return;
    }
    // An emptied out_s box means "to the end of the source" — the same null the
    // session stores. Falling through to parseFloat would leave the old number
    // sitting there while the box says otherwise.
    if (key === "out_s" && String(v).trim() === ""){
      t.out_s = null; paintLayers(); return;
    }
    const n = parseFloat(v);
    // A negative in/out is the one bad number the canvas HIDES — trackSpan
    // clamps it to 0 to draw, so the lane looks right while normalise_session is
    // going to refuse the whole session on save. Floored here instead.
    if (isFinite(n))                    // a half-typed "-" or "." must not be 0
      t[key] = (key === "in_s" || key === "out_s") ? Math.max(0, n) : n;
    paintLayers();                      // the lane follows the box it was typed in
  }
  /* A trim is non-destructive — the source file was never written to — so
     undoing one is just clearing the two numbers. */
  function resetTrim(i){
    const t = S.tracks[i];
    if (!t) return;
    t.in_s = 0; t.out_s = null;
    dropRange(i + 1);                   // the block just grew back under it
    renderSide(); paintLayers();
  }
  function dropTrack(i){
    S.tracks.splice(i, 1);
    // Unconditional: every lane after this one just shifted down an index, so
    // a range that still matched S.lrange.i would now be on someone else.
    dropRange(null);
    S.lsel = clamp(S.lsel, 0, S.tracks.length);
    renderSide(); paintLayers();
  }

  /* Render every track plus the current clip into one buffer, offline. The
   * current clip is track zero and is never implicit — a mixdown that silently
   * included or excluded what you were looking at would be a coin flip.
   * Returns the buffer rather than committing it, so the same render can go to
   * the clip (mixdown) or to a separate file (bounce). null means it already
   * said why it could not. */
  async function renderMix(includeCurrent){
    if (!S.tracks.length && !includeCurrent){ say("nothing to mix"); return null; }
    const rate = S.buf.sampleRate;
    const solo = S.tracks.some(t => t.solo);
    const loaded = [];
    // Keyed on the source path, because a split makes two tracks out of one file
    // and every half would otherwise be fetched and decoded over again.
    const decoded = new Map();
    for (const t of S.tracks){
      if (t.muted || (solo && !t.solo)) continue;
      let buf = decoded.get(t.source);
      if (!buf){
        try {
          // no-store: a layer has no mtime to key on, and mixing a stale copy of a
          // just-saved source bakes the old audio into the clip.
          const bytes = await fetch(
            `/api/audio/file?rel=${encodeURIComponent(t.source)}`, { cache: "no-store" })
            .then(r => r.arrayBuffer());
          buf = await S.ctx.decodeAudioData(bytes);
        } catch (e){
          say(`could not decode ${t.source}`);
          return null;
        }
        decoded.set(t.source, buf);
      }
      loaded.push({ t, buf });
    }
    // The count guard above is on the raw track list; mute/solo can empty it
    // here, and the 0.01 s floor below then committed 10 ms of silence over the
    // clip and marked the file dirty.
    if (!loaded.length && !includeCurrent){ say("every layer is muted — nothing to mix"); return null; }
    const ends = loaded.map(({t, buf}) => t.offset_s + trackSpan(t, buf).len);
    if (includeCurrent) ends.push(S.buf.duration);
    const seconds = Math.max(0.01, ...ends);
    // A pan on an all-mono mix needs somewhere to go, or the slider is inert.
    const panned = loaded.some(x => Math.abs(x.t.pan || 0) > 0.001);
    const channels = Math.max(panned ? 2 : 1, ...loaded.map(x => x.buf.numberOfChannels),
                              includeCurrent ? S.buf.numberOfChannels : 1);
    const off = new OfflineAudioContext(channels, Math.ceil(seconds * rate), rate);

    // The reverse used to live here; sliceBuf does both now, so this places a
    // buffer that is already exactly what the lane contributes.
    const place = (buf, offset, db, pan) => {
      const node = off.createBufferSource();
      node.buffer = buf;
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
    if (includeCurrent) place(S.buf, 0, 0, 0);
    loaded.forEach(({t, buf}) => {
      // Trim first, reverse second: reverse means "the piece I kept, played
      // backwards". The canvas, the stack preview and this all agree on that
      // order, and they have to — the picture is a promise about the sound.
      const sp = trackSpan(t, buf);
      place(sliceBuf(off, buf, sp.in_s, sp.out_s, t.reverse),
            t.offset_s, t.gain_db, t.pan);
    });

    const rendered = await off.startRendering();
    // Bring it back into the live context so every later edit sees one kind of
    // buffer, not "the mixed one" and "the others".
    const out = make(rendered.length, rendered.numberOfChannels, rate);
    for (let c = 0; c < out.numberOfChannels; c++)
      out.getChannelData(c).set(rendered.getChannelData(c));
    return { buf: out, count: loaded.length + (includeCurrent ? 1 : 0) };
  }

  async function mixdown(includeCurrent){
    const r = await renderMix(includeCurrent);
    if (!r) return;
    // deliver() honours "audition first": on, the mix is staged and A/B flips
    // back to the pre-mix clip; off, it snapshots and commits as it always did.
    deliver(r.buf, `mixed ${r.count} source(s)`, "reset");
    // The audition bar and its A/B live in the clip pane, so a mix staged from
    // layers mode would otherwise have no apply/cancel anywhere on screen.
    setMode("clip");
  }

  /* Where the clip's copy of the mix lands by default: sfx/hit.wav ->
   * sfx/hit_mix.wav, and a name for a clip that has never been saved. */
  function defaultBounce(){
    const rel = S && S.rel;
    if (!rel) return "game/assets/audio/mix.wav";
    const dot = rel.lastIndexOf(".");
    return dot > rel.lastIndexOf("/")
      ? rel.slice(0, dot) + "_mix" + rel.slice(dot)
      : rel + "_mix.wav";
  }
  function bounceAsField(v){ if (S) S.bounceAs = v; }

  /* The way out of "mixing spends the clip": render the same stack and write it
   * to its own file. S.buf, S.dirty and the undo stack are deliberately left
   * alone — nothing about the open clip changes. */
  async function bounce(){
    if (!S) return;
    const rel = String(S.bounceAs || defaultBounce()).trim();
    if (!rel){ say("give the bounce a path first"); return; }
    if (/\.ogg$/i.test(rel) && S.status && !S.status.ogg){
      say(S.status.ogg_reason || "ffmpeg is needed to write .ogg"); return;
    }
    const r0 = await renderMix(true);
    if (!r0) return;
    // The server's validate_wav refuses anything past 900 s. Catching it here
    // saves encoding tens of megabytes of base64 for a guaranteed 400.
    if (r0.buf.duration > 900){ say("that mix is longer than 15 minutes — shorten it first"); return; }
    const wav = encodeWav(r0.buf);
    // No mtime: the target is a file this session has never read, so there is
    // nothing to be stale against — only the "already exists" 409 can fire.
    const post = force => mutate("/api/audio/lab/save", {
      body: { rel, wav, overwrite: force || undefined, ogg_quality: 6 },
      button: "ab-bounce-go", quiet: !force });
    let r = await post(false);
    if (!r.ok && (r.code === "exists" || r.code === "conflict")){
      const exists = r.code === "exists";
      if (!await askConfirm({
        title: exists ? `${rel} already exists. Overwrite it?`
                      : `${rel} changed on disk. Overwrite it with this mix?`,
        body: exists ? "" : "Whatever wrote it since this session opened is what gets replaced.",
        ok: "overwrite", danger: true })){
        say(r.error);
        const box = document.getElementById("ab-bounce");
        if (box){ box.focus(); box.select(); }
        return;
      }
      r = await post(true);
    } else if (!r.ok){
      say(r.error);
    }
    if (!r.ok) return;
    say(`bounced to ${r.data.rel}`, "ok");
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

  /* ── import ───────────────────────────────────────────────────────────── */
  /* Until now the lab could only open audio that was already inside the project
   * tree, so anything downloaded, exported from another tool or sitting on the
   * desktop had to be filed by hand before it could be touched — and layering it
   * needs it on disk anyway.
   *
   * No new route: /api/audio/lab/save takes a `rel` that does not exist yet and
   * a base64 WAV, which is exactly what encodeWav() produces. Decode client
   * side, encode, POST. */

  const IMPORT_DIR = "game/assets/audio/imported/";

  /* Mirrors bgate_core/audiolab.MAX_SECONDS and the 180 MB base64 body cap in
     routes/audiolab.py. Both are checked here so a long file fails in a
     sentence rather than as a 400 after megabytes have gone up the wire. */
  const MAX_SECONDS = 900;
  const MAX_B64 = 180 * 1024 * 1024;

  function importStem(name){
    const s = String(name || "").toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "_").replace(/_+/g, "_")
      .replace(/^[_-]+|[_-]+$/g, "").slice(0, 48);
    return s || "import";
  }

  /* Where a decoded buffer goes once it exists, whatever produced it — the file
     picker, a drop, or the recorder. Two destinations and no dialog asking
     which: the door you came in through already said. */
  async function landAudio(buf, label, asLayer, suggestedName){
    if (!S || !buf) return;
    if (buf.duration > MAX_SECONDS){ say("that is longer than the 900s cap"); return; }
    const stem = importStem(suggestedName);

    if (!asLayer){
      // Adopted at the SESSION's rate. decodeAudioData already resampled into
      // S.ctx, so a 48k file lands in a 44.1k session as 44.1k — which is what
      // every other buffer here is, and what mixing and saving assume.
      const copy = make(buf.length, buf.numberOfChannels, S.buf.sampleRate);
      for (let c = 0; c < copy.numberOfChannels; c++)
        copy.getChannelData(c).set(buf.getChannelData(c));
      // deliver(), not commit(): an import is a replacement of the whole clip
      // and deserves the audition bar and the undo stack like any other op.
      deliver(copy, label, "reset");
      S.saveAs = "game/assets/audio/" + stem + ".wav";
      renderSide();
      return;
    }

    // A layer is a project-relative path in S.tracks, so it has to be on disk
    // before it can be one.
    const wav = encodeWav(buf);
    if (wav.length > MAX_B64){ say("that file is too large to save"); return; }
    let r = null;
    for (let n = 1; n <= 9; n++){
      const rel = IMPORT_DIR + stem + (n > 1 ? `-${n}` : "") + ".wav";
      // quiet: the "exists" 409 is the loop's own signal, not something to
      // toast. Never overwrite — the name collided with someone else's file.
      r = await mutate("/api/audio/lab/save", { body: { rel, wav }, quiet: true });
      if (r.ok || r.code !== "exists") break;
    }
    if (!S) return;
    if (!r.ok){ say(r.error || `could not save ${stem}.wav`); return; }
    addTrack(r.data.rel);
    say(`layered ${r.data.rel}`, "ok");
  }

  async function importFiles(files, asLayer){
    let list = Array.from(files || []);
    if (!list.length) return;
    // Dragging a file onto an empty pane is the first thing anyone tries, and
    // "open or create a sound first" is a wall in front of the obvious move.
    // Bootstrap the same blank session "new from synth" makes; the file lands
    // in it a moment later.
    if (!S){ await newSound(true); if (!S) return; }
    // Only one buffer can BE the clip, and stage() replaces a pending audition
    // without a word — so a three-file drop in clip mode kept the third and
    // lost two in silence. Take the first and say where the others went.
    if (!asLayer && list.length > 1){
      say(`importing ${list[0].name} — the other ${list.length - 1} need layers mode`);
      list = list.slice(0, 1);
    }
    for (const f of list){
      let buf;
      try {
        buf = await S.ctx.decodeAudioData(await f.arrayBuffer());
      } catch (e){
        // The name AND the reason: "could not decode" alone leaves you guessing
        // between a codec this browser lacks and a file that is not audio.
        say(`could not decode ${f.name}${e && e.message ? " — " + e.message : ""}`);
        continue;                    // one unreadable file must not end the drop
      }
      if (!S) return;                // the pane can be closed mid-decode
      await landAudio(buf, `imported ${f.name}`, asLayer,
                      f.name.replace(/\.[^.]+$/, ""));
      if (!S) return;                // …or mid-save, before the next file
    }
  }

  function importPicked(ev, asLayer){
    const el = ev && ev.target;
    if (!el) return;
    importFiles(el.files, asLayer);
    el.value = "";      // or picking the same file twice fires no `change` at all
  }

  /* Drop anywhere on the pane. Where you drop decides what it becomes: in
     layers mode the file is filed into the project and layered, anywhere else
     it opens as the clip. Bound to `back`, which close() removes — so these go
     with it and there is nothing to unbind. */
  function bindDrop(back){
    const isFiles = ev => !!ev.dataTransfer &&
      Array.from(ev.dataTransfer.types || []).indexOf("Files") >= 0;
    back.addEventListener("dragover", ev => {
      if (!isFiles(ev)) return;
      ev.preventDefault();                       // without this the drop never fires
      back.classList.add("ab-drop");
    });
    back.addEventListener("dragleave", ev => {
      // relatedTarget is where the pointer went. Dragging between two children
      // fires dragleave on the one being left, which is not leaving the pane.
      if (!ev.relatedTarget || !back.contains(ev.relatedTarget))
        back.classList.remove("ab-drop");
    });
    back.addEventListener("drop", ev => {
      if (!isFiles(ev)) return;
      ev.preventDefault();
      back.classList.remove("ab-drop");
      importFiles(ev.dataTransfer.files, S && S.mode === "layers");
    });
  }

  /* ── live recording ───────────────────────────────────────────────────── */
  /* The last source of audio that still meant leaving: a voice line, a real
   * door, a chair scraped across a floor. getUserMedia + MediaRecorder gives a
   * Blob, decodeAudioData gives an AudioBuffer, and from there a take is
   * indistinguishable from an import — same landAudio(), same two destinations,
   * same undo. Nothing new goes to the server.
   *
   * UNTESTED CAPTURE PATH. The machine this was written on has no working
   * input — `bgate doctor` reports "no input device would open" — so everything
   * from getUserMedia to the decoded take is written and not run. The FAILURE
   * path is the part that got the attention, because on a machine like this one
   * it is the only path anyone will see, and a disabled button with no reason
   * is worse than no button. */

  /* Cheap probes only: the ones that can answer without opening the device.
     getUserMedia is the real test and it happens in recStart(); its rejection
     comes back through this same panel. */
  async function recPreflight(){
    const bad = [];
    if (!window.isSecureContext || !navigator.mediaDevices ||
        !navigator.mediaDevices.getUserMedia){
      bad.push({ name: "capture api",
        reason: "this page is not allowed to reach a microphone",
        fix: "open the dashboard on http://127.0.0.1:7788 — browsers offer the "
           + "microphone to localhost or https only, never to a plain LAN address" });
      return bad;                     // nothing below can run without it
    }
    if (typeof window.MediaRecorder === "undefined")
      bad.push({ name: "mediarecorder",
        reason: "this browser has no MediaRecorder",
        fix: "use Chrome, Edge or Firefox — or import a file instead" });
    try {
      const devs = await navigator.mediaDevices.enumerateDevices();
      const ins = devs.filter(d => d.kind === "audioinput");
      // An EMPTY enumeration is not proof of a missing microphone: some browsers
      // withhold the whole list until a permission has been granted once. Only
      // call it missing when other kinds came back and no input did.
      if (!ins.length && devs.length)
        bad.push({ name: "input device",
          reason: "the system reports no audio input",
          fix: "plug in or enable a microphone (Windows: Settings › System › Sound › Input), then try again" });
    } catch (e){ /* enumeration is a bonus; getUserMedia is the real gate */ }
    return bad;
  }

  /* A DOMException name is the only reliable thing getUserMedia rejects with —
     the message is browser prose and sometimes empty. Turn it into the same
     reason/fix pair the preflight produces. */
  function recDenied(e){
    const n = (e && e.name) || "";
    const msg = (e && e.message) || "";
    if (n === "NotFoundError" || n === "DevicesNotFoundError" || n === "OverconstrainedError")
      return { name: "input device", reason: msg || "no microphone would open",
        fix: "plug one in or enable it (Windows: Settings › System › Sound › Input), then try again" };
    if (n === "NotAllowedError" || n === "PermissionDeniedError" || n === "SecurityError")
      return { name: "permission", reason: msg || "the browser blocked microphone access",
        fix: "click the padlock in the address bar, allow the microphone for this page, then try again" };
    if (n === "NotReadableError" || n === "TrackStartError" || n === "AbortError")
      return { name: "device busy", reason: msg || "the microphone is held by something else",
        fix: "close whatever has it open — a call, OBS, a DAW — and try again" };
    return { name: "microphone", reason: msg || "the microphone could not be opened",
      fix: "check the device in the system sound settings, then try again" };
  }

  /* Mounted, the panel lives under the toolbar. Cold, there is no toolbar yet
     and the preflight can still fail, so it drops into the landing card — which
     mount() replaces wholesale the moment a session exists. */
  function recWhyBox(){
    let el = document.getElementById("ab-why");
    if (el) return el;
    const land = document.querySelector(".ab-land-in");
    if (!land) return null;
    el = document.createElement("div");
    el.className = "ab-why"; el.id = "ab-why";
    land.appendChild(el);
    return el;
  }

  function recWhy(checks){
    const box = recWhyBox();
    if (!box){                        // nowhere to draw: say the first one and stop
      const c = checks[0];
      say(c ? `${c.reason} — ${c.fix}` : "cannot record");
      return;
    }
    box.innerHTML =
      '<div class="hd"><b>cannot record</b>'
      + '<button class="x" onclick="AudioLab.recDismiss()" title="Dismiss">✕</button></div>'
      + '<ul class="ab-checks">' + checks.map(c =>
          `<li><b>${E(c.name)}</b><span class="r">${E(c.reason)}</span>`
          + `<span class="fix">→ ${E(c.fix)}</span></li>`).join("") + "</ul>"
      + '<div class="foot">Everything else in the lab works without a microphone — '
      + 'import a file, or synthesise one.</div>';
    box.hidden = false;
  }

  function recDismiss(){
    const b = document.getElementById("ab-why");
    if (b){ b.hidden = true; b.innerHTML = ""; }
  }

  // Opus in webm is what every browser that can record at all agrees on;
  // audio/mp4 is Safari's. An empty string means "let the browser choose".
  const REC_TYPES = ["audio/webm;codecs=opus", "audio/webm",
                     "audio/ogg;codecs=opus", "audio/mp4"];
  function recMime(){
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
    for (const t of REC_TYPES) if (MediaRecorder.isTypeSupported(t)) return t;
    return "";
  }

  /* The buttons only go disabled once S.rec exists, and everything before that
     is awaited — a second click during the preflight or the permission prompt
     would open a second stream and orphan the first, which is the live-mic
     indicator that never goes away. */
  let recBusy = false;
  async function recStart(asLayer){
    if (recBusy || (S && S.rec)) return;
    recBusy = true;
    try { await recBegin(asLayer); }
    finally { recBusy = false; }
  }

  async function recBegin(asLayer){
    injectStyle();
    const bad = await recPreflight();
    if (bad.length){ recWhy(bad); say("no microphone — see the panel"); return; }
    // A take that replaces the clip is a bigger commitment than picking a file,
    // and it arrives minutes after the decision. Ask BEFORE the take, so a "no"
    // costs nothing; deliver() still makes the result itself undoable.
    if (!asLayer && S && !await discardGuard()) return;
    // Same bootstrap the drop path uses: recording is a perfectly good reason
    // for a session to exist, and "open something first" is a wall.
    if (!S){ await newSound(true); if (!S) return; }
    recDismiss();
    stop();                            // speakers into an open microphone is a loop

    let stream;
    try {
      // The call-centre processing is off on purpose. echoCancellation and
      // autoGainControl are tuned for speech and will duck, pump and notch a
      // sound effect; this is a recorder, not a headset.
      stream = await navigator.mediaDevices.getUserMedia({ audio: {
        echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
    } catch (e){
      if (S) recWhy([recDenied(e)]);
      say("the microphone did not open — see the panel");
      return;
    }
    // The permission prompt is modal and slow; the pane can be gone by the time
    // it is answered, and an orphaned stream keeps the live-mic indicator on.
    if (!S){ try { stream.getTracks().forEach(t => t.stop()); } catch (x) {} return; }

    let mr;
    try {
      const mime = recMime();
      mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    } catch (e){
      try { stream.getTracks().forEach(t => t.stop()); } catch (x) {}
      recWhy([{ name: "mediarecorder",
        reason: (e && e.message) || "the recorder would not start",
        fix: "this browser offers no capture format the lab can read — import a file instead" }]);
      return;
    }

    let src = null, an = null;
    try {
      src = S.ctx.createMediaStreamSource(stream);
      an = S.ctx.createAnalyser();
      an.fftSize = 1024;
      src.connect(an);                 // analyser only; reaching destination is feedback
    } catch (e){ an = null; }          // the meter is a nicety, a take without one still works
    try { S.ctx.resume(); } catch (e) {}

    const R = S.rec = {
      stream, mr, src, an, chunks: [], asLayer: !!asLayer,
      data: an ? new Float32Array(an.fftSize) : null,
      t0: performance.now(), raf: 0,
      cancelled: false, stopping: false, hot: false, heard: false,
    };
    mr.ondataavailable = ev => { if (ev.data && ev.data.size) R.chunks.push(ev.data); };
    mr.onerror = ev => {
      const err = ev && ev.error;
      say(`the recorder failed${err && err.message ? " — " + err.message : ""}`);
      R.cancelled = true;
      recStop();
    };
    mr.onstop = () => {
      const chunks = R.chunks.slice(), cancelled = R.cancelled, asL = R.asLayer;
      const type = (R.mr && R.mr.mimeType) || recMime() || "audio/webm";
      recTeardown();                   // device released before anything slow happens
      if (cancelled){ say("take discarded"); return; }
      if (!chunks.length){ say("nothing was captured — the input produced no audio"); return; }
      recLand(new Blob(chunks, { type }), asL);
    };
    try {
      mr.start(250);                   // timeslice: chunks arrive as we go, not in one lump
    } catch (e){
      recTeardown();
      recWhy([{ name: "recorder", reason: (e && e.message) || "the recorder would not start",
        fix: "try again, or import a file instead" }]);
      return;
    }
    syncRec();
    recTick();
  }

  function recStop(){
    const R = S && S.rec;
    if (!R || R.stopping) return;
    R.stopping = true;
    // The take lands in onstop, once the last chunk has arrived. Stopping the
    // tracks here instead would truncate it.
    try { R.mr.stop(); } catch (e){ recTeardown(); }
  }

  function recCancel(){
    const R = S && S.rec;
    if (!R) return;
    R.cancelled = true;                // read by onstop, which then drops the chunks
    recStop();
  }

  /* Releases the OS device. A stream whose tracks are never stopped leaves the
     browser's live-microphone indicator burning long after the pane is gone,
     which reads as the app listening in the background. */
  function recTeardown(){
    const R = S && S.rec;
    if (!R) return;
    S.rec = null;
    if (R.raf) cancelAnimationFrame(R.raf);
    if (R.mr){
      R.mr.ondataavailable = null; R.mr.onerror = null;
      if (R.mr.state !== "inactive"){
        R.mr.onstop = null;            // teardown is never a landing
        try { R.mr.stop(); } catch (e) {}
      }
    }
    try { R.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
    try { if (R.src) R.src.disconnect(); } catch (e) {}
    syncRec();
  }

  function syncRec(){
    const on = !!(S && S.rec);
    const bar = document.getElementById("ab-rec");
    if (bar) bar.hidden = !on;
    document.querySelectorAll(".ab-recbtn").forEach(b => { b.disabled = on; });
    const dest = document.getElementById("ab-rec-dest");
    if (dest) dest.textContent = on
      ? (S.rec.asLayer ? "→ a new layer" : "→ replaces the clip") : "";
    if (on) return;
    const fill = document.getElementById("ab-rec-fill");
    if (fill) fill.style.width = "0%";
    const meter = document.getElementById("ab-rec-meter");
    if (meter) meter.classList.remove("hot");
    const note = document.getElementById("ab-rec-note");
    if (note){ note.textContent = ""; note.className = "note"; }
  }

  const REC_SILENT_AFTER = 2.5;        // seconds of nothing before the meter says so

  function recTick(){
    const R = S && S.rec;
    if (!R) return;
    R.raf = requestAnimationFrame(recTick);
    const secs = (performance.now() - R.t0) / 1000;
    const t = document.getElementById("ab-rec-t");
    if (t) t.textContent = fmt(secs);
    if (R.an && R.data){
      R.an.getFloatTimeDomainData(R.data);
      let sum = 0, peak = 0;
      for (let i = 0; i < R.data.length; i++){
        const v = R.data[i], a = v < 0 ? -v : v;
        sum += v * v;
        if (a > peak) peak = a;
      }
      // dBFS over the bottom 60 dB. A linear meter spends most of its travel on
      // signal nobody can hear and then pins for the whole useful range.
      const db = 20 * Math.log10(Math.max(Math.sqrt(sum / R.data.length), 1e-6));
      const fill = document.getElementById("ab-rec-fill");
      if (fill) fill.style.width = (clamp((db + 60) / 60, 0, 1) * 100).toFixed(1) + "%";
      if (peak > 0.0008) R.heard = true;
      if (peak >= 0.99) R.hot = true;  // sticky: a clip you glanced away from still counts
      const meter = document.getElementById("ab-rec-meter");
      if (meter) meter.classList.toggle("hot", R.hot);
    }
    const note = document.getElementById("ab-rec-note");
    if (note){
      // A device that opens and then delivers silence looks exactly like a
      // working recorder, and that is the failure this machine actually has.
      const msg = R.hot ? "clipping — turn the input down"
        : (R.an && !R.heard && secs > REC_SILENT_AFTER) ? "no signal — the input is silent"
        : "";
      if (note.textContent !== msg){
        note.textContent = msg;
        note.className = "note" + (msg ? " warn" : "");
      }
    }
    // landAudio() refuses anything past the cap, so a forgotten recorder has to
    // be stopped here or the whole take is thrown away at the end of it.
    if (secs >= MAX_SECONDS){ say(`stopped at the ${MAX_SECONDS}s cap`); recStop(); }
  }

  async function recLand(blob, asLayer){
    if (!S) return;
    let buf;
    try { buf = await S.ctx.decodeAudioData(await blob.arrayBuffer()); }
    catch (e){
      say("the take was recorded but this browser could not decode it"
          + (e && e.message ? " — " + e.message : ""));
      return;
    }
    if (!S) return;                    // the pane can be closed mid-decode
    const d = new Date(), p = n => String(n).padStart(2, "0");
    const stem = `take_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
    await landAudio(buf, `recorded take · ${fmt(buf.duration)}`, asLayer, stem);
  }

  /* ── persistence ──────────────────────────────────────────────────────── */
  /* Anything the panel owns has to live in S, not only in the markup: every
     commit, undo and waveform drag re-renders the side, and a value that was
     only in the DOM silently reverts to the template default. For the save
     path that meant a typed name reverting to the ORIGINAL file. */
  function saveAsField(v){ if (S) S.saveAs = v; }
  function uiField(key, v){
    if (!S || !S.ui) return;
    if (key === "fade_curve"){
      S.ui.fade_curve = FADES.indexOf(v) >= 0 ? v : "linear";
      return;
    }
    if (key === "norm_per_channel" || key === "preview"){
      S.ui[key] = !!v; renderSide(); return;   // changes what the panel draws
    }
    const n = parseFloat(v);
    if (!isFinite(n)) return;          // a half-typed "-" or "." must not be 0
    S.ui[key] = n;
    // Same trap as synthField: renderSide() here would replace the slider
    // mid-drag and kill it. Patch the readout in place instead.
    const el = document.getElementById("ab-uv-" + key);
    if (el) el.textContent = n + (el.dataset.sfx || "");
  }

  async function save(asNew){
    if (!S) return;
    // Writing while an audition is up would save whatever S.buf still is, which
    // is not what the pane is showing or playing.
    if (S.staged){ say("apply or cancel the pending edit first"); return; }
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
    const post = force => mutate("/api/audio/lab/save", {
      body: { rel, wav, mtime: (rel === S.rel) ? S.mtime : undefined,
              overwrite: force || undefined, ogg_quality: 6 },
      // quiet on the first try so a 409 asks the question instead of toasting a
      // dead end; failures we do not handle are said by hand below.
      button: "ab-save", quiet: !force });
    let r = await post(false);
    // Both 409s are recoverable and the server will not act until we say so.
    // Without this the disk-changed case was a dead end: every retry sent the
    // same stale mtime, and the only way out discarded the edits.
    if (!r.ok && (r.code === "exists" || r.code === "conflict")){
      const exists = r.code === "exists";
      if (!await askConfirm({
        title: exists ? `${rel} already exists. Overwrite it?`
                      : `${rel} changed on disk since you opened it. `
                        + "Overwrite it with your edits?",
        body: exists ? "" : "Whatever wrote it since this session opened is what gets replaced.",
        ok: "overwrite", danger: true })){
        say(r.error);
        const box = document.getElementById("ab-saveas");
        if (box){ box.focus(); box.select(); }
        return;
      }
      r = await post(true);
    } else if (!r.ok){
      say(r.error);
    }
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
              // a clip that does not loop reports mode "disabled"; sending that
              // back would ask the server to enable looping in mode 0.
              mode: (S.loop.mode && S.loop.mode !== "disabled") ? S.loop.mode : "forward" }});
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
    const u = S.ui || (S.ui = { speed: 1, silence: 0.25, reps: 3, xf: 120 });
    if (u.units == null) u.units = "s";
    if (u.snap == null) u.snap = true;
    if (u.gain_db == null) u.gain_db = -3;
    if (u.fade_curve == null) u.fade_curve = "linear";
    if (u.norm_db == null) u.norm_db = -1;
    if (u.semitones == null) u.semitones = 0;
    if (u.preview == null) u.preview = true;
    const step = unitStep();

    $.side.innerHTML = `
      <div class="ab-h">selection<span></span></div>
      <div class="ab-row">
        <label>in</label>
        <input class="ab-in num" id="ab-sel-a" type="number" step="${step}" min="0"
               value="${toUnits(sel ? sel.a : 0)}" onchange="AudioLab.selField('a',this.value)">
        <label style="min-width:22px">out</label>
        <input class="ab-in num" id="ab-sel-b" type="number" step="${step}" min="0"
               value="${toUnits(sel ? sel.b : S.buf.length)}" onchange="AudioLab.selField('b',this.value)">
      </div>
      <div class="ab-row">
        <label>length</label>
        <input class="ab-in num" id="ab-sel-len" type="number" step="${step}" min="0"
               value="${toUnits(sel ? sel.b - sel.a : 0)}" onchange="AudioLab.selField('len',this.value)">
        <select class="ab-in" onchange="AudioLab.selUnits(this.value)">
          ${["s","ms","samples"].map(x =>
            `<option value="${x}"${u.units===x?" selected":""}>${x}</option>`).join("")}
        </select>
      </div>
      <div class="ab-row">
        <button class="ab-tg${u.snap?" on":""}" onclick="AudioLab.toggleSnap()">snap to zero</button>
        <span class="ab-sub">${sel ? fmt((sel.b - sel.a) / rate) : "whole clip"}</span>
      </div>
      <div class="ab-note">Drag an edge to move it, ←/→ to nudge by 1 ms (Shift
        10 ms, Alt the in point, Ctrl the whole selection). Snapping lands the
        boundary on a zero crossing, which is what stops a trim from clicking.</div>

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
      </div>
      <button class="ab-btn wide" onclick="AudioLab.toMono()">to mono</button>

      <div class="ab-row">
        <button class="ab-tg${u.preview?" on":""}"
                onclick="AudioLab.uiField('preview',${!u.preview})">audition first</button>
        <span class="ab-sub">${u.preview ? "apply / cancel each one" : "straight to the clip"}</span>
      </div>
      <div class="ab-note">With this on, the ops below render into a bar over the
        transport instead of into the clip: play it, <b>A/B</b> it against the
        original, then apply or cancel. Trim, delete and the rest commit either
        way — there is nothing to dial in on those.</div>

      ${uiRow("gain_db", "gain", u.gain_db, -36, 24, 0.5, " dB")}
      <button class="ab-btn wide" onclick="AudioLab.gain(AudioLab.state.ui.gain_db)">apply gain</button>

      <div class="ab-row">
        <label>fade</label>
        <select class="ab-in" onchange="AudioLab.uiField('fade_curve',this.value)">
          ${FADES.map(c =>
            `<option value="${c}"${u.fade_curve===c?" selected":""}>${c}</option>`).join("")}
        </select>
      </div>
      <div class="ab-grid2">
        <button class="ab-btn" onclick="AudioLab.fade('in')">fade in</button>
        <button class="ab-btn" onclick="AudioLab.fade('out')">fade out</button>
      </div>
      <div class="ab-note">A <b>linear</b> fade-out on a tail sounds like it stops
        early — logarithmic or equal-power is what a tail actually wants.</div>

      <div class="ab-row">
        <label>norm dB</label>
        <input class="ab-in num" id="ab-norm" type="number" step="0.5" min="-60" max="0"
               value="${u.norm_db}" oninput="AudioLab.uiField('norm_db',this.value)">
        <button class="ab-tg${u.norm_per_channel?" on":""}"
                onclick="AudioLab.uiField('norm_per_channel',${!u.norm_per_channel})">per channel</button>
      </div>
      <button class="ab-btn wide" onclick="AudioLab.normalize(AudioLab.state.ui.norm_db)">normalise</button>
      <div class="ab-note">Leave headroom: a stinger normalised to <b>0 dBFS</b>
        clips the moment anything else plays under it.</div>

      <div class="ab-row">
        <label>silence</label>
        <input class="ab-in num" id="ab-sil" type="number" step="0.05" min="0" max="60"
               value="${u.silence}" oninput="AudioLab.uiField('silence',this.value)">
        <button class="ab-btn" onclick="AudioLab.insertSilence(+document.getElementById('ab-sil').value)">insert</button>
      </div>
      <div class="ab-row">
        <label>speed</label>
        <input class="ab-in num" id="ab-speed" type="number" step="0.05" min="0.25" max="4"
               value="${u.speed}" oninput="AudioLab.uiField('speed',this.value)">
        <button class="ab-btn" onclick="AudioLab.speed(+document.getElementById('ab-speed').value)">apply</button>
      </div>
      <div class="ab-row">
        <label>pitch st</label>
        <input class="ab-in num" id="ab-semi" type="number" step="1" min="-24" max="24"
               value="${u.semitones}" oninput="AudioLab.uiField('semitones',this.value)">
        <button class="ab-btn" onclick="AudioLab.speed(Math.pow(2,AudioLab.state.ui.semitones/12))">apply</button>
      </div>
      <div class="ab-note">Speed moves pitch with it, like pitching tape — which
        is how you make a heavy version of a light hit. Semitones is the same
        resample said in musical units, so the clip gets shorter as it goes up.</div>

      <div class="ab-h">extend<span></span></div>
      <div class="ab-row">
        <label>repeat</label>
        <input class="ab-in num" id="ab-reps" type="number" min="2" max="64"
               value="${u.reps}" oninput="AudioLab.uiField('reps',this.value)">×
        <input class="ab-in num" id="ab-xf" type="number" min="0" max="2000"
               value="${u.xf}" oninput="AudioLab.uiField('xf',this.value)">ms
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
          <div class="ab-row"><label>in s</label>
            <input class="ab-in num" type="number" step="0.01" min="0" value="${t.in_s || 0}"
                   oninput="AudioLab.trackField(${i},'in_s',this.value)">
            <label>out s</label>
            <input class="ab-in num" type="number" step="0.01" placeholder="end"
                   value="${t.out_s == null ? "" : t.out_s}"
                   oninput="AudioLab.trackField(${i},'out_s',this.value)"></div>
          <div class="ab-row"><label>pan</label>
            <input class="ab-in" type="range" min="-1" max="1" step="0.05" value="${t.pan}"
                   oninput="AudioLab.trackField(${i},'pan',this.value)"></div>
          <div class="mini">
            <button class="ab-tg${t.muted?" on":""}" onclick="AudioLab.trackField(${i},'muted',${!t.muted})">mute</button>
            <button class="ab-tg${t.solo?" on":""}" onclick="AudioLab.trackField(${i},'solo',${!t.solo})">solo</button>
            <button class="ab-tg${t.reverse?" on":""}" onclick="AudioLab.trackField(${i},'reverse',${!t.reverse})">reverse</button>
            <button class="ab-tg" onclick="AudioLab.resetTrim(${i})">reset trim</button>
          </div>
        </div>`).join("")
        : `<div class="ab-note">Layer other project sounds under this one — a hit
            plus a noise tail, a stinger over a pad.</div>`}
      <button class="ab-btn wide" onclick="AudioLab.addTrack()">+ layer a sound</button>
      <div class="ab-grid2">
        <button class="ab-btn go" onclick="AudioLab.mixdown(true)">mix with this clip</button>
        <button class="ab-btn" onclick="AudioLab.mixdown(false)">layers only</button>
      </div>
      <div class="ab-row">
        <input class="ab-in" id="ab-bounce" value="${E(S.bounceAs || defaultBounce())}"
               oninput="AudioLab.bounceAsField(this.value)">
      </div>
      <button class="ab-btn wide" id="ab-bounce-go"
              onclick="AudioLab.bounce()">bounce to a new file</button>
      <div class="ab-note"><b>Mix</b> replaces this clip${(S.ui && S.ui.preview !== false)
        ? " — with an apply/cancel step, since <b>audition first</b> is on" : ""}.
        <b>Bounce</b> writes the stack to the file above and leaves this clip
        exactly as it is.</div>
      <button class="ab-btn wide" onclick="AudioLab.saveSession()">save mix session</button>

      <div class="ab-h">save<span></span></div>
      <div class="ab-row">
        <input class="ab-in" id="ab-saveas" value="${E(S.saveAs || S.rel || "")}"
               oninput="AudioLab.saveAsField(this.value)">
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
    if (S.mode === "layers") paintLayers();
  }

  function synthRow(key, label, value, min, max, step){
    return `<div class="ab-row"><label>${E(label)}</label>
      <input class="ab-in" type="range" min="${min}" max="${max}" step="${step}"
             value="${value}" oninput="AudioLab.synthField('${key}',this.value)">
      <span class="ab-sub" id="ab-sv-${key}" style="width:44px;text-align:right">${value}</span></div>`;
  }

  /* The edit rack's parallel of synthRow. The suffix rides on the readout as a
     data attribute so uiField can rebuild the text without knowing the units. */
  function uiRow(key, label, value, min, max, step, suffix){
    const sfx = suffix || "";
    return `<div class="ab-row"><label>${E(label)}</label>
      <input class="ab-in" type="range" min="${min}" max="${max}" step="${step}"
             value="${value}" oninput="AudioLab.uiField('${key}',this.value)">
      <span class="ab-sub" id="ab-uv-${key}" data-sfx="${E(sfx)}"
            style="width:52px;text-align:right">${value}${E(sfx)}</span></div>`;
  }

  /* ── layers mode ──────────────────────────────────────────────────────── */
  /* The mixer's side panel is a form: every layer's position is a number you
   * type. This is the same model — S.tracks, unchanged — drawn as lanes on a
   * shared time axis, so you can see one sound sitting under another and move
   * it by dragging.
   *
   * It is a third mode rather than extra lanes on the clip canvas because the
   * two surfaces disagree about both axes. The clip canvas's whole pointer
   * contract is selection and its S.view is SAMPLES of S.buf; a layer has its
   * own sample rate and can end long past the clip, so this view has to be in
   * seconds spanning the whole stack — a quantity S.view cannot hold. */

  const RULER_H = 18;                    // the time strip along the top, CSS px
  const MIN_LEN = 0.02;                  // the shortest a trimmed lane may get, s

  function laneHeight(H){
    return clamp((H - RULER_H) / (S.tracks.length + 1), 38, 88);
  }
  /* The kept region of a track, clamped against the source it was decoded from.
     A trim is stored as two seconds-into-the-file numbers and nothing else, so
     every read of "how long is this layer" has to come through here or the four
     paths that draw and play a lane will disagree with each other.
     With nothing decoded the length is 0, which is the same "contributes only
     its offset" the callers had before trimming existed. */
  function trackSpan(tr, buf){
    const dur = buf ? buf.duration : 0;
    const in_s = clamp(tr.in_s || 0, 0, dur);
    const out_s = clamp(tr.out_s == null ? dur : tr.out_s, in_s, dur);
    return { in_s, out_s, len: out_s - in_s };
  }

  /* One buffer holding exactly what a lane contributes — the kept region, and
     reversed if the track asks. Both playback paths already allocated a
     reversed copy, so this is the same cost with the trim folded in, and their
     duration/start() maths then works unchanged against a shorter buffer. */
  function sliceBuf(ctx, buf, in_s, out_s, reverse){
    const rate = buf.sampleRate;
    const a = clamp(Math.round(in_s * rate), 0, buf.length);
    const b = clamp(Math.round(out_s * rate), a, buf.length);
    if (!reverse && a === 0 && b === buf.length) return buf;   // nothing to do
    const n = Math.max(1, b - a);
    const out = ctx.createBuffer(buf.numberOfChannels, n, rate);
    for (let c = 0; c < buf.numberOfChannels; c++){
      const src = buf.getChannelData(c), d = out.getChannelData(c);
      // Reverse applies to the TRIMMED region, not to the whole source: what
      // you kept, played backwards.
      if (reverse) for (let i = 0; i < n; i++) d[i] = src[b - 1 - i] || 0;
      else d.set(src.subarray(a, a + n));
    }
    return out;
  }

  /* The full span the stack occupies, in seconds. Layers that have not decoded
     yet contribute only their offset — the fit widens once they land. */
  function layersTotal(){
    let t = S.buf.duration;
    for (const tr of S.tracks){
      t = Math.max(t, tr.offset_s + trackSpan(tr, S.layerBufs[tr.source]).len);
    }
    return Math.max(0.5, t);
  }

  /* Decoded layers, cached by source path. no-store for the same reason
     mixdown() gives: a layer has no mtime to key on, so a cached copy of a
     just-saved source would draw the audio that is no longer there. */
  async function ensureLayerBuf(rel){
    if (!S || !rel) return null;
    if (Object.prototype.hasOwnProperty.call(S.layerBufs, rel)) return S.layerBufs[rel];
    if (S.layerBusy[rel]) return null;
    S.layerBusy[rel] = true;
    try {
      const bytes = await fetch(`/api/audio/file?rel=${encodeURIComponent(rel)}`,
                                { cache: "no-store" }).then(r => r.arrayBuffer());
      const buf = await S.ctx.decodeAudioData(bytes);
      if (!S) return null;
      S.layerBufs[rel] = buf;
    } catch (e){
      if (S) S.layerBufs[rel] = null;    // null is "tried and failed", not "not tried"
    }
    if (!S) return null;
    delete S.layerBusy[rel];
    // The fit that ran before this decode landed could not know how long the
    // layer was, so a still-fitted view re-fits rather than clipping it off.
    if (S.lfit) layersFit(); else paintLayers();
    return S.layerBufs[rel];
  }

  function sizeLayerCanvas(){
    if (!S || !$.lcanvas) return false;
    const r = $.layers.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(r.width * dpr));
    const h = Math.max(1, Math.round(r.height * dpr));
    S.ldpr = dpr;
    if ($.lcanvas.width === w && $.lcanvas.height === h) return false;
    $.lcanvas.width = w; $.lcanvas.height = h;
    return true;
  }

  // Same non-latching coalescer as paint(), and for the same reason: rAF never
  // fires while this pane is hidden, so a plain latch would freeze it for good.
  function paintLayers(){
    if (!S || !$.lctx2d) return;
    if (S._lpending) return;
    S._lpending = true;
    const run = () => { if (!S || !S._lpending) return; S._lpending = false; _paintLayers(); };
    requestAnimationFrame(run);
    setTimeout(run, 120);
  }

  function secToX(t, W){
    const { from, to } = S.lview;
    return ((t - from) / Math.max(1e-6, to - from)) * W;
  }
  function xToSec(x){
    const W = $.lcanvas.width / (S.ldpr || 1);
    const { from, to } = S.lview;
    return from + (x / Math.max(1, W)) * (to - from);
  }
  /* Which lane a y lands on — null over the ruler or past the bottom lane. */
  function laneAt(y){
    if (!S || y < RULER_H) return null;
    const H = $.lcanvas.height / (S.ldpr || 1);
    const lh = laneHeight(H);
    const i = Math.floor((y - RULER_H + S.lscroll) / lh);
    return (i < 0 || i > S.tracks.length) ? null : i;
  }
  /* Lane 0 is the clip; 1..n are S.tracks. Both carry muted/solo, but the clip's
     live outside S.tracks (see S.clipLane), so reads go through here. */
  function laneLabel(i){
    if (!i) return S.rel || S.saveAs || "clip";
    const tr = S.tracks[i - 1];
    return tr ? (tr.name || tr.source) : "";
  }
  function laneState(i){ return (i ? S.tracks[i - 1] : S.clipLane) || {}; }

  /* S.lsel is a lane index and nothing more, but a time range hangs off one
     particular lane — so moving the focus is what invalidates it. A range that
     outlives its lane and is then acted on edits audio the user never pointed
     at, which is the failure this whole helper exists to make impossible. */
  function focusLane(i){
    if (!S) return;
    const n = clamp(i, 0, S.tracks.length);
    if (S.lsel !== n) S.lrange = null;
    S.lsel = n;
  }
  /* The same clear, for the other direction: the lane stayed put but its
     geometry moved under the range. Called from every path that rewrites a
     track's in/out/offset without going through the range ops themselves. */
  function dropRange(lane){
    if (S && S.lrange && (lane == null || S.lrange.i === lane)) S.lrange = null;
  }

  /* A lane's block in TIMELINE seconds — where the kept region starts and ends
     on the ruler, which is the space the pointer is in. Lane 0 is the clip: it
     has no in/out, so it has no range either, the same way it has no trim. */
  function laneBlock(i){
    if (!S || !i) return null;
    const tr = S.tracks[i - 1];
    const buf = tr && S.layerBufs[tr.source];
    if (!buf) return null;
    const sp = trackSpan(tr, buf);
    const t0 = tr.offset_s || 0;
    return { tr, buf, sp, t0, t1: t0 + sp.len };
  }

  /* Where lane i's M and S chips sit, in CSS px. The painter and the hit test
     both call this, so a chip you can see is a chip you can press. */
  function laneChips(i, top){
    const c = $.lctx2d;
    if (!c) return null;
    c.font = "10px ui-monospace,monospace";
    const x0 = 4 + c.measureText(laneLabel(i)).width + 10 + 5;
    return { m: { x: x0, y: top + 4, w: 15, h: 14 },
             s: { x: x0 + 18, y: top + 4, w: 15, h: 14 } };
  }
  function inChip(b, x, y){
    return !!b && x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h;
  }

  /* Which end of lane i's block the pointer is on — "in", "out" or null — using
     the same EDGE_PX grab radius the clip canvas's selection edges use, because
     it is the same gesture: grab an end, drag it, the audio behind it is kept
     or hidden. The interior of the block is still the move, so this is hit
     tested before the drag branch exactly like the M/S chips are.
     Lane 0 is the clip: it is what t=0 MEANS here, so it never trims. */
  function laneEdgeAt(i, x, y){
    if (!S || !i) return null;
    const tr = S.tracks[i - 1];
    if (!tr) return null;
    const buf = S.layerBufs[tr.source];
    if (!buf) return null;               // undecoded: no length to grab an end of
    const H = $.lcanvas.height / (S.ldpr || 1);
    const lh = laneHeight(H);
    const top = RULER_H + i * lh - S.lscroll;
    if (y < top || y > top + lh) return null;
    const W = $.lcanvas.width / (S.ldpr || 1);
    const off = tr.offset_s || 0;
    const da = Math.abs(secToX(off, W) - x);
    const db = Math.abs(secToX(off + trackSpan(tr, buf).len, W) - x);
    if (da <= EDGE_PX && da <= db) return "in";
    if (db <= EDGE_PX) return "out";
    return null;
  }
  function drawChip(c, b, glyph, on, onToken){
    c.fillStyle = BGTheme.color(on ? onToken : "--surface-3");
    c.fillRect(b.x, b.y, b.w, b.h);
    c.fillStyle = BGTheme.color(on ? "--accent-fg" : "--text-3");
    c.font = "10px ui-monospace,monospace";
    c.fillText(glyph, b.x + 4, b.y + 11);
  }

  // 60 px is about the narrowest a m:ss label reads at. The list runs past the
  // 1–60 s band at both ends so a hard zoom never leaves the ruler blank.
  const TICK_STEPS = [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600];

  function _paintLayers(){
    if (!S || !$.lctx2d) return;
    if (!S.lview) S.lview = { from: 0, to: layersTotal() * 1.02 };
    sizeLayerCanvas();
    const c = $.lctx2d, dpr = S.ldpr || 1;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = $.lcanvas.width / dpr, H = $.lcanvas.height / dpr;
    c.clearRect(0, 0, W, H);
    c.fillStyle = BGTheme.color("--bg"); c.fillRect(0, 0, W, H);

    const { from, to } = S.lview;
    const pps = W / Math.max(1e-6, to - from);
    const lh = laneHeight(H);
    const n = S.tracks.length + 1;
    // Keep the stack from scrolling off its own bottom after a lane is dropped.
    const maxScroll = Math.max(0, n * lh - (H - RULER_H));
    S.lscroll = clamp(S.lscroll, 0, maxScroll);

    // ── ruler
    let step = TICK_STEPS[TICK_STEPS.length - 1];
    for (const s of TICK_STEPS){ if (s * pps >= 60){ step = s; break; } }
    c.strokeStyle = BGTheme.color("--line");
    c.fillStyle = BGTheme.color("--text-3");
    c.font = "10px ui-monospace,monospace";
    // Ticks stay inside the band: the lane fills below are opaque, so a
    // full-height gridline would be paint nobody ever sees.
    c.beginPath();
    for (let t = Math.ceil(from / step) * step; t <= to; t += step){
      const x = Math.round(secToX(t, W)) + .5;
      c.moveTo(x, RULER_H - 5); c.lineTo(x, RULER_H);
    }
    c.stroke();
    for (let t = Math.ceil(from / step) * step; t <= to; t += step){
      const m = Math.floor(t / 60), s = t - m * 60;
      const lbl = `${m}:${s < 10 ? "0" : ""}${step < 1 ? s.toFixed(2) : Math.round(s)}`;
      c.fillText(lbl, Math.round(secToX(t, W)) + 3, 10);
    }

    // ── lanes. 0 is the clip and defines t=0; 1..n are S.tracks.
    for (let i = 0; i < n; i++){
      const top = RULER_H + i * lh - S.lscroll;
      if (top > H || top + lh < RULER_H) continue;
      const tr = i ? S.tracks[i - 1] : null;
      const buf = i ? S.layerBufs[tr.source] : S.buf;
      const off = i ? (tr.offset_s || 0) : 0;
      const focused = S.lsel === i;

      c.save();
      c.beginPath(); c.rect(0, RULER_H, W, H - RULER_H); c.clip();

      c.fillStyle = BGTheme.color(i % 2 ? "--surface-2" : "--surface-1");
      c.fillRect(0, top, W, lh);

      const label = i ? (tr.name || tr.source) : (S.rel || S.saveAs || "clip");
      if (i && buf === undefined){
        c.fillStyle = BGTheme.color("--text-3");
        c.font = "10px ui-monospace,monospace";
        c.fillText(`${label}   decoding…`, 8, top + lh / 2 + 3);
      } else if (i && buf === null){
        c.fillStyle = BGTheme.color("--bad");
        c.font = "10px ui-monospace,monospace";
        c.fillText(`${label}   could not decode`, 8, top + lh / 2 + 3);
      } else if (buf){
        // The block is the KEPT region: it starts at offset_s and runs for
        // span.len, and its waveform is read from in_s into the source rather
        // than from the file's head.
        const sp = i ? trackSpan(tr, buf)
                     : { in_s: 0, out_s: buf.duration, len: buf.duration };
        const t0 = Math.max(off, from), t1 = Math.min(off + sp.len, to);
        if (t1 > t0){
          const x0 = secToX(t0, W), x1 = secToX(t1, W);
          const px = Math.max(1, Math.floor(x1 - x0));
          if (focused){
            c.fillStyle = BGTheme.color("--accent-soft");
            c.fillRect(x0, top + 1, x1 - x0, lh - 2);
          }
          const mid = top + lh / 2;
          c.strokeStyle = BGTheme.color("--line");
          c.beginPath(); c.moveTo(x0, mid); c.lineTo(x1, mid); c.stroke();
          // Channel 0 only: a lane this short cannot show two, and which one is
          // louder is not what you are reading a layer stack for.
          const p = peaks(buf, 0, (sp.in_s + (t0 - off)) * buf.sampleRate,
                          (sp.in_s + (t1 - off)) * buf.sampleRate, px);
          const st = laneState(i);
          c.strokeStyle = BGTheme.color(
            st.muted ? "--text-3" : st.solo ? "--good"
                     : i ? "--text-2" : "--accent-hover");
          c.beginPath();
          const amp = lh / 2 - 6;
          for (let x = 0; x < px; x++){
            const lo = p[x * 2], hi = p[x * 2 + 1];
            const px0 = Math.floor(x0) + x + .5;
            const y0 = mid - hi * amp, y1 = mid - lo * amp;
            c.moveTo(px0, y0); c.lineTo(px0, Math.max(y1, y0 + .6));
          }
          c.stroke();
          // A cut end gets a rule, or a trimmed layer is indistinguishable from
          // a short file and there is nothing telling you audio is being held
          // back behind that edge.
          c.fillStyle = BGTheme.color("--accent-hover");
          if (sp.in_s > 0.0005) c.fillRect(secToX(off, W), top + 1, 2, lh - 2);
          if (sp.out_s < buf.duration - 0.0005)
            c.fillRect(secToX(off + sp.len, W) - 2, top + 1, 2, lh - 2);
        }
      }

      // The range, if this is the lane holding it. Deliberately NOT a fill: the
      // focused lane is already washed in --accent-soft above, so a soft fill
      // over it would be invisible. A hard bar along the top edge plus a rule
      // at each end reads at every lane height from 38 px up.
      if (S.lrange && S.lrange.i === i){
        const rx0 = secToX(S.lrange.a, W), rx1 = secToX(S.lrange.b, W);
        c.fillStyle = BGTheme.color("--accent");
        c.fillRect(rx0, top + 1, Math.max(1, rx1 - rx0), 3);
        c.fillRect(Math.round(rx0), top + 1, 1, lh - 2);
        c.fillRect(Math.round(rx1) - 1, top + 1, 1, lh - 2);
      }

      // Label chip last, over the waveform — a name you cannot read is no label.
      c.font = "10px ui-monospace,monospace";
      const tw = c.measureText(label).width;
      c.fillStyle = BGTheme.color("--surface-3");
      c.fillRect(4, top + 4, tw + 10, 14);
      c.fillStyle = BGTheme.color("--text");
      c.fillText(label, 9, top + 14);

      // M/S beside the name, so the stack can be muted and soloed without
      // leaving the canvas for the side panel.
      const chips = laneChips(i, top);
      if (chips){
        const lst = laneState(i);
        drawChip(c, chips.m, "M", !!lst.muted, "--accent");
        drawChip(c, chips.s, "S", !!lst.solo, "--good");
      }

      c.strokeStyle = BGTheme.color("--line");
      c.beginPath();
      c.moveTo(0, top + lh - .5); c.lineTo(W, top + lh - .5); c.stroke();
      c.restore();
    }

    // After the lanes: lane 0's fill starts exactly at RULER_H and would
    // otherwise swallow the seam between the ruler and the stack.
    c.strokeStyle = BGTheme.color("--line");
    c.beginPath(); c.moveTo(0, RULER_H + .5); c.lineTo(W, RULER_H + .5); c.stroke();

    // ── playhead. Live while the stack plays, parked and dimmer when it does
    // not — the parked one is where the next ▶ starts from, so it has to show.
    const head = S.lplay
      ? S.lplay.from + (S.ctx.currentTime - S.lplay.startCtxTime)
      : (S.lhead || 0);
    const hx = Math.round(secToX(head, W)) + .5;
    if (hx >= 0 && hx <= W){
      c.strokeStyle = BGTheme.color(S.lplay ? "--text" : "--text-3");
      c.beginPath(); c.moveTo(hx, 0); c.lineTo(hx, H); c.stroke();
    }

    if ($.lhud) $.lhud.textContent =
      `${n} lanes  ·  view ${fmt(from)}–${fmt(to)}`;
    if ($.linfo){
      const tr = S.lsel ? S.tracks[S.lsel - 1] : null;
      // The hint is the whole discoverability story for this canvas: trim is
      // invisible until you are within 6 px of an end, and which meaning a
      // plain drag carries depends on a toggle. Both get written down, plus the
      // Alt inverse, plus the live range if there is one.
      const tb = tr ? S.layerBufs[tr.source] : null;
      const sp = tb ? trackSpan(tr, tb) : null;
      const cut = sp && (sp.in_s > 0.0005 || sp.out_s < tb.duration - 0.0005);
      const rg = S.lrange;
      const tool = S.ui.ltool === "select"
        ? "drag across a lane to select a span (Alt-drag moves it)"
        : "drag the lane to move it (Alt-drag selects a span)";
      $.linfo.textContent = S.lplay
        ? `playing from ${fmt(S.lplay.from)}  ·  ${fmt(head)}`
        : rg
          ? `range ${fmt(rg.b - rg.a)} on ${laneLabel(rg.i)}`
            + `  ·  ${fmt(rg.a)}–${fmt(rg.b)}`
            + " — trim to it, remove it or play it; a fade, a silence or a gain"
            + " over a range is a clip-mode edit, not something a layer can hold"
          : tr
            ? `${tr.name || tr.source} at ${fmt(tr.offset_s || 0)}`
              + (cut ? `  ·  trimmed to ${fmt(sp.len)} (${fmt(sp.in_s)}–${fmt(sp.out_s)} of the file)` : "")
              + ` — ${tool}, drag either end to trim (Shift for fine)`
            : `click the ruler to set the playhead — ${tool}, drag either end of a lane to trim`;
    }
    syncStack();
    if (S.lplay) paintLayers();          // keep the playhead moving
  }

  function bindLayers(){
    const el = $.lcanvas;
    if (!el) return;
    el.addEventListener("pointerdown", ev => {
      if (!S || !S.lview) return;
      const r = el.getBoundingClientRect();
      const px = ev.clientX - r.left, py = ev.clientY - r.top;
      // The ruler band is the scrub strip: park the playhead where you clicked,
      // and re-arm from there if the stack is already running.
      if (py < RULER_H){
        S.lhead = clamp(xToSec(px), 0, layersTotal());
        if (S.lplay) playStack(S.lhead); else paintLayers();
        return;
      }
      el.setPointerCapture(ev.pointerId);
      const i = laneAt(py);
      if (i == null) return;
      focusLane(i);                 // moving the focus is what drops a range
      // Chips are hit BEFORE the drag branch: a press that lands on one is a
      // toggle, not the start of a time drag.
      const lh0 = laneHeight($.lcanvas.height / (S.ldpr || 1));
      const chips = laneChips(i, RULER_H + i * lh0 - S.lscroll);
      const hit = chips && (inChip(chips.m, px, py) ? "muted"
                          : inChip(chips.s, px, py) ? "solo" : null);
      if (hit){
        if (i) trackField(i - 1, hit, !S.tracks[i - 1][hit]);
        else clipLaneField(hit);
        return;
      }
      // …and an end is hit before the drag, for the same reason: the precedence
      // is ruler → chips → edge → move, so the interior of a block still moves
      // the lane exactly as it did before trimming existed.
      const edge = laneEdgeAt(i, px, py);
      if (edge){
        const t = S.tracks[i - 1];
        const sp = trackSpan(t, S.layerBufs[t.source]);
        S.ltrim = { i, edge, x0: px, in0: sp.in_s, out0: sp.out_s,
                    off0: t.offset_s || 0 };
        dropRange(i);               // the block is about to move under it
        el.style.cursor = "col-resize";
        paintLayers();
        return;
      }
      // Then, and only then, which meaning the plain drag carries. Alt inverts
      // whichever tool is armed, so a move-mode user can pull one range without
      // leaving move and a select-mode user can nudge one lane without leaving
      // select. Edge still won above — that precedence is the only reason
      // drag-to-move survived this feature at all.
      if ((S.ui.ltool === "select") !== ev.altKey){
        const bl = laneBlock(i);
        if (!bl){
          say(i ? "that layer has not decoded yet"
                : "the clip lane holds no range — select on the waveform in clip mode");
          paintLayers();
          return;
        }
        // Clamped to the block: a range over empty timeline is not a range, and
        // every op below would have to re-clamp it anyway.
        const at = clamp(xToSec(px), bl.t0, bl.t1);
        S.lrdrag = { i, a: at, lo: bl.t0, hi: bl.t1 };
        S.lrange = { i, a: at, b: at };
        el.style.cursor = "crosshair";
        paintLayers();
        return;
      }
      // Lane 0 is the clip: it is what t=0 MEANS here, so it cannot move.
      if (i > 0){
        const t = S.tracks[i - 1];
        S.ldrag = { i, x0: ev.clientX - r.left, off0: t.offset_s || 0 };
        el.style.cursor = "grabbing";
      }
      paintLayers();
    });
    el.addEventListener("pointermove", ev => {
      if (!S || !S.lview) return;
      const r = el.getBoundingClientRect();
      const x = ev.clientX - r.left;
      if (S.ltrim){
        const tm = S.ltrim;
        const t = S.tracks[tm.i - 1];
        const buf = t && S.layerBufs[t.source];
        if (!buf){ S.ltrim = null; return; }
        const W = $.lcanvas.width / (S.ldpr || 1);
        const pps = W / Math.max(1e-6, S.lview.to - S.lview.from);
        const d = (x - tm.x0) / pps;
        if (tm.edge === "out"){
          t.out_s = clamp(tm.out0 + d, (t.in_s || 0) + MIN_LEN, buf.duration);
        } else {
          // The left end moves in_s and offset_s by the SAME delta, or the audio
          // you can still see slides along the timeline while you trim it.
          // Two floors: there is nothing before the head of the source to
          // reveal, and normalise_session rejects a negative offset_s outright.
          const lo = Math.max(-tm.in0, -tm.off0);
          let dd = clamp(d, lo, tm.out0 - tm.in0 - MIN_LEN);
          // The same zero snap the move branch has, and Shift turns it off here
          // for the same reason: it is the fine-positioning key.
          if (!ev.shiftKey && Math.abs(tm.off0 + dd) * pps < 4)
            dd = clamp(-tm.off0, lo, tm.out0 - tm.in0 - MIN_LEN);
          t.in_s = tm.in0 + dd;
          t.offset_s = tm.off0 + dd;
        }
        paintLayers();
        return;
      }
      if (S.lrdrag){
        const d = S.lrdrag;
        // a is where the press landed, not the lower end — dragging leftwards
        // has to make the same range as dragging rightwards.
        const at = clamp(xToSec(x), d.lo, d.hi);
        S.lrange = { i: d.i, a: Math.min(d.a, at), b: Math.max(d.a, at) };
        paintLayers();
        return;
      }
      if (!S.ldrag){
        const y = ev.clientY - r.top;
        const i = laneAt(y);
        const lh0 = i == null ? 0 : laneHeight($.lcanvas.height / (S.ldpr || 1));
        const ch = i == null ? null : laneChips(i, RULER_H + i * lh0 - S.lscroll);
        // Same test the pointerdown makes, so the cursor promises the gesture
        // you are actually about to get.
        const selecting = (S.ui.ltool === "select") !== ev.altKey;
        el.style.cursor = (ch && (inChip(ch.m, x, y) || inChip(ch.s, x, y))) ? "pointer"
                        : (i != null && laneEdgeAt(i, x, y)) ? "col-resize"
                        : (i != null && i > 0) ? (selecting ? "crosshair" : "grab")
                        : y < RULER_H ? "col-resize" : "default";
        return;
      }
      const t = S.tracks[S.ldrag.i - 1];
      if (!t){ S.ldrag = null; return; }
      const W = $.lcanvas.width / (S.ldpr || 1);
      const pps = W / Math.max(1e-6, S.lview.to - S.lview.from);
      let off = S.ldrag.off0 + (x - S.ldrag.x0) / pps;
      // Shift is fine positioning, so it also turns the snap off.
      if (!ev.shiftKey && Math.abs(off) * pps < 4) off = 0;
      // The floor at 0 is not taste: normalise_session validates offset_s in
      // [0, 900] and rejects the session outright if a layer starts negative.
      t.offset_s = clamp(off, 0, 900);
      // The block just slid out from under any range measured against it — the
      // same clear the trim drag does on the way in. Only once it has actually
      // moved, so a click that merely focuses the lane still keeps the range.
      if (t.offset_s !== S.ldrag.off0) dropRange(S.ldrag.i);
      paintLayers();
    });
    const end = () => {
      if (!S) return;
      if (S.lrdrag){
        S.lrdrag = null;
        // A click, not a drag. A range shorter than the shortest trim can act on
        // nothing, and leaving it armed would leave the range row live over it.
        if (S.lrange && S.lrange.b - S.lrange.a < MIN_LEN) S.lrange = null;
        el.style.cursor = S.ui.ltool === "select" ? "crosshair" : "grab";
        paintLayers();
        return;
      }
      if (S.ltrim){
        const t = S.tracks[S.ltrim.i - 1];
        if (t){
          const ms = v => Math.round(v * 1000) / 1000;
          const buf = S.layerBufs[t.source];
          t.in_s = ms(t.in_s || 0);
          t.offset_s = ms(t.offset_s || 0);
          // Dragged back out to the file's end, out_s goes back to null: an
          // untrimmed lane should not carry a number that a re-encoded source
          // would then contradict.
          t.out_s = t.out_s == null ? null
                  : (buf && t.out_s >= buf.duration - 0.0005) ? null
                  : ms(t.out_s);
        }
        S.ltrim = null;
        el.style.cursor = "grab";
        renderSide();             // the in/out boxes must agree with the canvas
        paintLayers();
        return;
      }
      if (!S.ldrag) return;
      const t = S.tracks[S.ldrag.i - 1];
      if (t) t.offset_s = Math.round((t.offset_s || 0) * 1000) / 1000;
      S.ldrag = null;
      el.style.cursor = "grab";
      renderSide();               // the mixer form must agree with the canvas
      paintLayers();
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    el.addEventListener("wheel", ev => {
      if (!S || !S.lview) return;
      ev.preventDefault();
      const r = el.getBoundingClientRect();
      if (ev.shiftKey){
        const H = $.lcanvas.height / (S.ldpr || 1);
        const lh = laneHeight(H);
        const maxScroll = Math.max(0, (S.tracks.length + 1) * lh - (H - RULER_H));
        S.lscroll = clamp(S.lscroll + ev.deltaY, 0, maxScroll);
        paintLayers();
        return;
      }
      const at = xToSec(ev.clientX - r.left);
      const span = S.lview.to - S.lview.from;
      const total = Math.max(layersTotal() * 1.02, S.lview.to);
      const next = clamp(span * (ev.deltaY < 0 ? 1/1.25 : 1.25), 0.05, total);
      const frac = (at - S.lview.from) / span;
      let f = clamp(at - frac * next, 0, Math.max(0, total - next));
      S.lview = { from: f, to: Math.min(total, f + next) };
      S.lfit = false;                    // you chose this view; stop re-fitting it
      paintLayers();
    }, { passive: false });
  }

  function layersFit(){
    if (!S) return;
    S.lview = { from: 0, to: layersTotal() * 1.02 };
    S.lscroll = 0;
    S.lfit = true;
    paintLayers();
  }

  /* The right half's name. An existing ·N is stripped before the next one is
     added, or a lane split three times reads "hit·2·2·2" and the number stops
     meaning anything. The stem is what gets shortened at the 80-char cap, since
     normalise_session slices there and trimming the suffix instead would
     collapse every piece past the cap onto one string. */
  function splitName(base){
    const stem0 = String(base || "").replace(/·\d+$/, "");
    const taken = new Set(S.tracks.map(t => t.name));
    for (let k = 2; k < 1000; k++){
      const sfx = "·" + k;
      const cand = stem0.slice(0, Math.max(0, 80 - sfx.length)) + sfx;
      if (!taken.has(cand)) return cand;
    }
    return stem0.slice(0, 80);
  }

  /* Cut the focused lane in two at the playhead. Nothing is written and nothing
   * is copied: both halves keep pointing at the same source file and just carry
   * complementary in/out, which is the whole reason the trim model is two
   * numbers rather than a new buffer. Split, then drag one half away. */
  function splitLayer(){
    if (!S) return;
    const i = S.lsel;
    // Lane 0 is the clip and is what t=0 means here; it has no in/out to split.
    if (!i){ say("focus a layer lane first — the clip lane cannot be split"); return; }
    const t = S.tracks[i - 1];
    if (!t) return;
    // Checked at the gesture rather than at save: normalise_session rejects a
    // session past 32 tracks outright, and failing there loses the whole save.
    if (S.tracks.length >= 32){ say("32 layers is the cap — nothing left to split into"); return; }
    const buf = S.layerBufs[t.source];
    if (!buf){ say("that layer has not decoded yet"); return; }
    const sp = trackSpan(t, buf);
    const off = t.offset_s || 0;
    // The live head while the stack runs, the parked one otherwise — the same
    // pair _paintLayers draws, so the cut lands on the line you can see.
    const head = S.lplay
      ? S.lplay.from + (S.ctx.currentTime - S.lplay.startCtxTime)
      : (S.lhead || 0);
    const d = head - off;                  // how far into the block the cut is
    if (d <= 0 || d >= sp.len){
      say("the playhead is not inside that lane — click the ruler to move it");
      return;
    }
    if (d < MIN_LEN || sp.len - d < MIN_LEN){
      say(`too close to an end — each half must be at least ${Math.round(MIN_LEN * 1000)} ms`);
      return;
    }
    // The right half starts at the playhead, and that becomes an offset_s that
    // normalise_session validates against the same 900 s the drag clamps to.
    if (head > 900){ say("past the 900s cap — nothing can start there"); return; }

    const ms = v => Math.round(v * 1000) / 1000;
    // out_s at the file's end goes back to null, the same "to the end of the
    // source" the fresh-layer and drag-trim paths both store.
    const cap = v => (v >= buf.duration - 0.0005 ? null : ms(v));
    // A reversed block plays [in_s, out_s] backwards, so a cut d into the block
    // is the source point out_s - d, and the halves swap which end they keep.
    const cut = t.reverse ? sp.out_s - d : sp.in_s + d;
    const right = Object.assign({}, t, {
      name: splitName(t.name || t.source.split("/").pop()),
      offset_s: ms(head),
      in_s: t.reverse ? ms(sp.in_s) : ms(cut),
      out_s: t.reverse ? cap(cut) : cap(sp.out_s),
    });
    // The left half keeps its offset_s and its name; only the end it lost moves.
    t.in_s = t.reverse ? ms(cut) : ms(sp.in_s);
    t.out_s = t.reverse ? cap(sp.out_s) : cap(cut);
    t.offset_s = ms(off);

    // Directly after the original, so lane order still reads left to right.
    S.tracks.splice(i, 0, right);
    focusLane(i + 1);
    renderSide();
    paintLayers();
  }

  /* ── the range ────────────────────────────────────────────────────────── */
  /* Only ops that the two-numbers trim model can actually express live here.
   * Silence, fade, normalize and gain over a range are NOT among them: they
   * need automation this schema does not have, and faking them would mean
   * writing to a source file three other sessions may be pointing at. The route
   * for those stays the clip — see the hint $.linfo prints while a range is up. */

  function setLayerTool(name){
    if (!S) return;
    // The range deliberately SURVIVES a tool change: the Alt inverse means a
    // move-mode user can pull one range and then reach for these buttons, and
    // clearing it here would make that path pointless.
    S.ui.ltool = name === "select" ? "select" : "move";
    paintLayers();
  }
  function clearRange(){ if (S && S.lrange){ S.lrange = null; paintLayers(); } }

  /* Timeline [a, b] on this lane, expressed as the two seconds-into-the-SOURCE
     numbers a track stores. A reversed block plays its kept region backwards, so
     the ends swap — the same mapping splitLayer does for its one cut point. */
  function rangeToSource(tr, sp, a, b){
    const off = tr.offset_s || 0;
    const da = a - off, db = b - off;
    return tr.reverse
      ? { in_s: sp.out_s - db, out_s: sp.out_s - da }
      : { in_s: sp.in_s + da, out_s: sp.in_s + db };
  }
  /* The one writer for a track's three position numbers, so every op lands the
     same shape normalise_session accepts: in_s inside the source, out_s null at
     the file's end and never <= in_s, offset_s inside [0, 900]. */
  function writeSpan(tr, buf, in_s, out_s, offset_s){
    const ms = v => Math.round(v * 1000) / 1000;
    tr.in_s = ms(clamp(in_s, 0, buf.duration));
    const o = clamp(out_s, tr.in_s + MIN_LEN, buf.duration);
    tr.out_s = o >= buf.duration - 0.0005 ? null : ms(o);
    tr.offset_s = ms(clamp(offset_s, 0, 900));
  }
  /* The range clamped against the lane it belongs to, or null if there is
     nothing left of it. Every op goes through this rather than trusting
     S.lrange: the block can have moved since the drag that made it. */
  function liveRange(){
    if (!S || !S.lrange) return null;
    const bl = laneBlock(S.lrange.i);
    if (!bl){ say("that layer has not decoded yet"); return null; }
    const a = clamp(S.lrange.a, bl.t0, bl.t1);
    const b = clamp(S.lrange.b, bl.t0, bl.t1);
    if (b - a < MIN_LEN){
      say(`the range is shorter than the ${Math.round(MIN_LEN * 1000)} ms floor`);
      return null;
    }
    return { bl, a, b };
  }

  /* Keep only what the range covers. offset_s follows the range's left end for
     the same reason the left trim handle moves it: the audio you can still see
     must not slide along the timeline while you cut. */
  function rangeTrim(){
    const rg = liveRange();
    if (!rg) return;
    const { bl, a, b } = rg;
    if (a > 900){ say("past the 900s cap — nothing can start there"); return; }
    const s = rangeToSource(bl.tr, bl.sp, a, b);
    writeSpan(bl.tr, bl.buf, s.in_s, s.out_s, a);
    S.lrange = null;                     // the range IS the lane now
    renderSide(); paintLayers();
  }

  /* Drop what the range covers. At either end of the block that is one trim; in
     the middle it is the two complementary halves splitLayer writes with the
     middle piece never created — so a mid-lane cut stays inside the
     non-destructive model instead of reaching for the file. */
  function rangeRemove(){
    const rg = liveRange();
    if (!rg) return;
    const { bl, a, b } = rg;
    const tr = bl.tr, buf = bl.buf, sp = bl.sp;
    const head = a - bl.t0, tail = bl.t1 - b;   // what survives on each side
    if (head < MIN_LEN && tail < MIN_LEN){
      say("that is the whole layer — drop it from the mixer instead"); return;
    }
    if (head < MIN_LEN){                        // touches the start: a head trim
      if (b > 900){ say("past the 900s cap — nothing can start there"); return; }
      const s = rangeToSource(tr, sp, b, bl.t1);
      writeSpan(tr, buf, s.in_s, s.out_s, b);
    } else if (tail < MIN_LEN){                 // touches the end: a tail trim
      const s = rangeToSource(tr, sp, bl.t0, a);
      writeSpan(tr, buf, s.in_s, s.out_s, bl.t0);
    } else {
      // Checked at the gesture rather than at save: normalise_session rejects a
      // session past 32 tracks outright and failing there loses the whole save.
      if (S.tracks.length >= 32){
        say("32 layers is the cap — nothing left to split into"); return; }
      if (b > 900){ say("past the 900s cap — nothing can start there"); return; }
      // The right piece is copied off the track BEFORE the track is rewritten,
      // and both spans are measured from the `sp` snapshot for the same reason.
      const right = Object.assign({}, tr, {
        name: splitName(tr.name || tr.source.split("/").pop()) });
      const rs = rangeToSource(tr, sp, b, bl.t1);
      writeSpan(right, buf, rs.in_s, rs.out_s, b);
      const ls = rangeToSource(tr, sp, bl.t0, a);
      writeSpan(tr, buf, ls.in_s, ls.out_s, bl.t0);
      S.tracks.splice(S.lrange.i, 0, right);
      focusLane(S.lrange.i + 1);
    }
    S.lrange = null;
    renderSide(); paintLayers();
  }

  /* Audition just this lane over just this span. Straight through playStack —
     the scope argument exists so this does not become a second scheduler that
     can disagree with the first about trim, reverse, gain and pan. */
  function rangePlay(){
    const rg = liveRange();
    if (!rg) return;
    playStack(rg.a, { lane: S.lrange.i, until: rg.b });
  }

  /* ── stack playback ───────────────────────────────────────────────────── */
  /* Scheduled in the LIVE S.ctx, not through an OfflineAudioContext. Offline
   * renders the same mix, but it renders it whole: it cannot be started from a
   * playhead or stopped part-way, which is the only thing auditioning a stack
   * is for. mixdown() keeps the offline path because it wants a buffer. */

  function toggleStack(){ if (S) S.lplay ? stopStack() : playStack(S.lhead || 0); }

  /* `scope` is { lane, until } and is how "play range" auditions one lane over
     one span without a second scheduler that could disagree with this one about
     trim, reverse, gain or pan. Omitted, this is the whole stack as before. */
  function playStack(fromSec, scope){
    if (!S) return;
    stop();                                  // clears the clip source and any prior stack
    try { S.ctx.resume(); } catch (e) {}
    const at = Math.max(0, fromSec || 0);

    const cl = S.clipLane || {};
    // `i` is the LANE index, which is not the index in this array: an undecoded
    // track is skipped below, so scope has to match on the tagged number.
    const lanes = [{ i: 0, buf: S.buf, off: 0, in_s: 0, out_s: S.buf.duration,
                     len: S.buf.duration, muted: !!cl.muted, solo: !!cl.solo,
                     gain_db: cl.gain_db || 0, pan: cl.pan || 0, reverse: false }];
    for (let k = 0; k < S.tracks.length; k++){
      const t = S.tracks[k];
      const b = S.layerBufs[t.source];
      if (!b) continue;                      // undefined = still decoding, null = failed
      const sp = trackSpan(t, b);
      lanes.push({ i: k + 1, buf: b, off: t.offset_s || 0, in_s: sp.in_s,
                   out_s: sp.out_s,
                   len: sp.len, muted: !!t.muted, solo: !!t.solo,
                   gain_db: t.gain_db || 0, pan: t.pan || 0, reverse: !!t.reverse });
    }
    let live;
    if (scope){
      // An explicit audition of one lane ignores mute and solo: you asked for
      // THIS lane, and refusing because something else is soloed is not answer.
      live = lanes.filter(l => l.i === scope.lane);
      if (!live.length){ say("that layer has not decoded yet"); return; }
    } else {
      // Same solo semantics as mixdown(): one solo anywhere makes solo the filter.
      const solo = lanes.some(l => l.solo);
      live = lanes.filter(l => !l.muted && (!solo || l.solo));
      if (!live.length){ say("every lane is muted"); return; }
    }

    // A small lead, or the first lane is already late by the time start() lands.
    const t0 = S.ctx.currentTime + 0.06;
    const srcs = [];
    let longest = null, longestEnd = -1;
    for (const l of live){
      const end = l.off + l.len;
      if (end <= at) continue;               // finished before the playhead
      // Trim and reverse in one buffer, in that order — the same order the
      // canvas draws and mixdown renders. The seek maths below then treats the
      // kept region as if it were the whole file, which is what it is here.
      const use = sliceBuf(S.ctx, l.buf, l.in_s, l.out_s, l.reverse);
      const node = S.ctx.createBufferSource();
      node.buffer = use;
      let tail = node;
      const g = S.ctx.createGain();
      g.gain.value = Math.pow(10, (l.gain_db || 0) / 20);
      tail.connect(g); tail = g;
      if (S.ctx.createStereoPanner){
        const p = S.ctx.createStereoPanner();
        p.pan.value = clamp(l.pan || 0, -1, 1);
        tail.connect(p); tail = p;
      }
      tail.connect(S.ctx.destination);
      if (l.off >= at) node.start(t0 + (l.off - at));
      else node.start(t0, at - l.off);       // already running: seek into it
      // A scoped audition stops at the range's end rather than at the buffer's.
      // stop() fires onended, so the transport still resets itself.
      if (scope && scope.until != null)
        node.stop(t0 + Math.max(0.01, Math.min(scope.until, end) - at));
      srcs.push(node);
      if (end > longestEnd){ longestEnd = end; longest = node; }
    }
    if (!srcs.length){ say("nothing plays from here"); return; }

    S.lplay = { srcs, startCtxTime: t0, from: at };
    // Only the LAST lane to end resets the transport; the others finish early
    // and would otherwise stop the whole thing mid-stack.
    if (longest) longest.onended = () => {
      if (!S || !S.lplay || S.lplay.srcs.indexOf(longest) < 0) return;
      S.lplay = null; S.lhead = at;
      paintLayers();
    };
    paintLayers();
  }

  function stopStack(){
    if (!S || !S.lplay) return;
    for (const src of S.lplay.srcs){
      try { src.onended = null; src.stop(); } catch (e) {}
      try { src.disconnect(); } catch (e) {}
    }
    S.lplay = null;
    paintLayers();
  }

  /* The clip lane lives outside S.tracks, so it needs its own setter. Called
     with no value it flips — that is what the M/S chips and pills both want. */
  function clipLaneField(key, v){
    if (!S || !S.clipLane) return;
    S.clipLane[key] = v === undefined ? !S.clipLane[key] : v;
    renderSide();
    paintLayers();
  }

  function syncStack(){
    const b = document.getElementById("ab-lplay");
    if (b) b.textContent = S && S.lplay ? "❚❚ stop" : "▶ play stack";
    const cl = (S && S.clipLane) || {};
    const m = document.getElementById("ab-lclipm");
    const s = document.getElementById("ab-lclips");
    if (m) m.classList.toggle("on", !!cl.muted);
    if (s) s.classList.toggle("on", !!cl.solo);
    const tool = (S && S.ui && S.ui.ltool) === "select" ? "select" : "move";
    const tm = document.getElementById("ab-ltool-move");
    const ts = document.getElementById("ab-ltool-sel");
    if (tm) tm.classList.toggle("on", tool === "move");
    if (ts) ts.classList.toggle("on", tool === "select");
    // Hidden while the drag that makes the range is still running: the row
    // wraps the transport, and having it appear under the pointer mid-gesture
    // moves every other button out from under it.
    const rr = document.getElementById("ab-lrange");
    if (rr) rr.hidden = !(S && S.lrange && !S.lrdrag);
  }

  /* ── studio mode ──────────────────────────────────────────────────────── */
  /* The beat maker is a second view onto the SAME clip, not a second document.
   * It renders through `adopt`, which lands the result in the normal undo stack
   * — so a rendered beat can then be trimmed, faded, looped and saved by
   * everything that already exists here. */
  function setMode(mode){
    if (!S) return;
    // Leaving the clip pane with an audition pending would hide the only bar
    // that can apply it, so it goes with you as a cancel.
    if (mode === "studio" || mode === "layers") cancelStaged();
    S.mode = (mode === "studio" || mode === "layers") ? mode : "clip";
    const studio = document.getElementById("ab-studio");
    const wave = document.getElementById("ab-wave");
    // :not() because the layers bar is a .ab-transport too now, and a bare
    // querySelector would be a document-order coin flip between the two.
    const transport = document.querySelector(".ab-transport:not(#ab-ltransport)");
    const layers = document.getElementById("ab-layers");
    const ltransport = document.getElementById("ab-ltransport");
    document.querySelectorAll("#ab-modes .ab-mode").forEach(b =>
      b.classList.toggle("active", b.dataset.m === S.mode));
    if (studio) studio.hidden = S.mode !== "studio";
    if (wave) wave.hidden = S.mode !== "clip";
    if (transport) transport.hidden = S.mode !== "clip";
    if (layers) layers.hidden = S.mode !== "layers";
    if (ltransport) ltransport.hidden = S.mode !== "layers";
    if (S.mode === "studio"){
      stop();
      if (!window.BeatMaker){ studio.innerHTML =
        `<div style="padding:24px;color:var(--ash2)">the beat maker did not load</div>`; return; }
      if (!S.studioMounted){
        BeatMaker.mount(studio, (S.meta && S.meta.beat) || null);
        S.studioMounted = true;
      }
    } else if (S.mode === "layers"){
      stop();
      if (window.BeatMaker) BeatMaker.stop();
      S.tracks.forEach(t => ensureLayerBuf(t.source));
      if (!S.lview) layersFit(); else paintLayers();
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
    commit(out, label || "rendered", "reset");
    setMode("clip");
  }

  function loopMode(m){ S.loop.mode = m; renderSide(); }
  function selectAll(){ S.sel = { a: 0, b: S.buf.length }; renderSide(); paint(); }
  function clearSel(){ S.sel = null; renderSide(); paint(); }
  function zoomSel(){ if (S.sel){ S.view = { from: S.sel.a, to: S.sel.b }; paint(); } }
  function zoomFit(){ S.view = { from: 0, to: S.buf.length }; paint(); }

  // Studio entry point: render into `host` instead of over the whole page.
  function embed(host, rel){
    _host = host || null;
    // The landing card below is styled by the injected sheet, and every other
    // entry point (open/pick/newSound) injected it. Opening the Studio tab
    // cold therefore painted .ab-land with no rules at all.
    injectStyle();
    if (rel) return open(rel);
    // Landing state. Opening the tab used to fire the picker modal straight
    // away, which reads as an error dialog rather than as a workspace.
    if (host) host.innerHTML =
      '<div class="ab-land">' +
        '<div class="ab-land-in">' +
          '<h3>Audio mixer</h3>' +
          '<p>Choose a sound to work on, start an empty one, or bring one in from ' +
            'disk — you can drop a file straight onto this pane. Everything you ' +
            'open here saves back to the project.</p>' +
          '<div class="ab-land-btns">' +
            '<button class="qbtn" onclick="AudioLab.pick()">open a sound…</button>' +
            '<button class="qbtn ghost" onclick="AudioLab.newDialog()">new sound…</button>' +
            '<button class="qbtn ghost" onclick="document.getElementById(\'ab-landfile\').click()">import a file…</button>' +
            '<button class="qbtn ghost ab-recbtn" onclick="AudioLab.recStart(false)">record…</button>' +
          '</div>' +
          '<input type="file" id="ab-landfile" accept="audio/*" style="display:none" ' +
                 'onchange="AudioLab.importPicked(event,false)">' +
        '</div></div>';
    // The cold pane is where a drop is most likely and was the one place it did
    // nothing: bindDrop() only ever ran from mount(). importFiles() opens a
    // session for it, and mount() then replaces this card and its listeners.
    const land = host && host.querySelector(".ab-land");
    if (land) bindDrop(land);
    return null;
  }
  // Studio blows away the host's markup on a tab change, which used to leave the
  // session alive behind it: capture-phase keydown still bound, ctx still open,
  // beat still playing. Tear the whole thing down; silent so a tab change never
  // throws the unsaved-edits confirm at someone who did not ask to close.
  function unembed(){ if (S) close(); _host = null; }

  return {
    // The exported close is the one that asks; the bare teardown stays private
    // so no caller can skip the question by reaching for it.
    open, close: closeAsk, pick, pickSound, pickSearch, closePick, newSound, setMode, adopt,
    newDialog, closeNew, newBlank, newDup, newFromSel,
    embed, unembed,
    togglePlay, play, stop, undo, redo, save, saveAsField, uiField,
    saveSession, writeLoop, loopMode,
    selectAll, clearSel, zoomSel, zoomFit,
    selField, selUnits, toggleSnap, toggleLoopSel,
    stage, applyStaged, cancelStaged, toggleAB,
    trim, cut, silence, fade, gain, normalize, reverse, insertSilence, repeat,
    speed, toMono,
    synthField, synthPreview, synthReplace, synthAppend,
    addTrack, trackField, resetTrim, dropTrack, mixdown, bounce, bounceAsField,
    layersFit, splitLayer,
    setLayerTool, clearRange, rangeTrim, rangeRemove, rangePlay,
    importFiles, importPicked,
    recStart, recStop, recCancel, recDismiss,
    toggleStack, playStack, stopStack, clipLaneField,
    get state(){ return S; },
  };
})();
