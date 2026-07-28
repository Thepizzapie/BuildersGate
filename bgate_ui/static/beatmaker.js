/* beatmaker.js — the studio half of the audio lab: a step sequencer that
 * renders into the clip editor.
 *
 * The clip editor can trim, fade and layer what already exists. It cannot make
 * a piece of music, and "make a loop" is most of what a game actually needs
 * from audio — a menu bed, a combat loop, a stinger, a UI blip pattern. So this
 * is the other half: tempo, a grid, voices, patterns, and a song.
 *
 * ONE DESIGN RULE ABOVE ALL: what you hear live and what gets rendered must be
 * the same signal. The way that is guaranteed here is that nothing is
 * synthesised with live oscillator nodes — every voice is rendered ONCE into an
 * AudioBuffer and then scheduled as a BufferSource, in both the live scheduler
 * and the offline render. "It sounded right in the tool" cannot become a lie
 * that way; the two paths play identical bytes.
 *
 * Voices are procedural and deterministic (a seeded noise generator, not
 * Math.random) so the same settings are the same sound every time — otherwise
 * "that one was good, do it again" is impossible. A kit that needs no sample
 * files also means a beat can be made in a project that has no audio at all.
 *
 * Timing uses the standard WebAudio lookahead scheduler: a setInterval that
 * runs ahead of the clock and schedules note starts at exact sample times.
 * Scheduling on rAF or on setInterval directly is what makes browser sequencers
 * swing when nobody asked them to.
 *
 * Registered as window.BeatMaker; AudioLab mounts it as its "studio" mode and
 * `renderInto` hands the result back as the clip, which means every existing
 * edit, loop-point and save path applies to a beat with no new plumbing.
 */
window.BeatMaker = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const clamp = (v, lo, hi) => v < lo ? lo : v > hi ? hi : v;

  const DRUMS = ["kick", "snare", "hat_closed", "hat_open", "clap", "tom",
                 "rim", "cowbell"];
  const SYNTHS = ["sine", "square", "saw", "triangle"];
  const NOTE_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
  const RESOLUTIONS = [
    { v: 4, label: "1/16" }, { v: 2, label: "1/8" }, { v: 1, label: "1/4" },
    { v: 3, label: "1/8T" }, { v: 6, label: "1/16T" }, { v: 8, label: "1/32" },
  ];
  // Lookahead scheduler constants — the standard pair. 25ms of polling with
  // 120ms of runway survives a garbage collection without dropping a step.
  const TICK_MS = 25, LOOKAHEAD_S = 0.12;

  let B = null;              // the session
  let ctx = null;            // borrowed from AudioLab so both share a clock
  let host = null;
  let ui = { pattern: 0, track: 0, playing: false, step: -1, song: false };
  let voiceCache = new Map();   // signature -> AudioBuffer
  let sampleCache = new Map();  // rel -> AudioBuffer
  let timer = null, nextTime = 0, nextStep = 0, nextSongIndex = 0;

  /* ── defaults ─────────────────────────────────────────────────────────── */
  function emptySteps(n){
    return Array.from({length:n}, () => ({ on:false, vel:1, note:0 }));
  }
  function track(name, kind, voice, over){
    return Object.assign({
      name, kind, voice, source:"", gain_db:0, pan:0, pitch:0, decay:1,
      muted:false, solo:false, steps: emptySteps(B ? B.steps : 16),
    }, over || {});
  }
  /* A kit you can hit play on immediately. An empty grid is a blank page, and a
   * blank page is where a tool like this loses people. */
  function starter(){
    const steps = 16;
    const on = (t, idxs, vel) => idxs.forEach(i => {
      t.steps[i].on = true; t.steps[i].vel = vel || 1; });
    const kick = track("kick", "drum", "kick");
    const snare = track("snare", "drum", "snare");
    const hat = track("hat", "drum", "hat_closed", { gain_db: -8 });
    const bass = track("bass", "synth", "square", { gain_db: -6, decay: 0.6, pitch: -24 });
    on(kick, [0, 6, 8, 14]);
    on(snare, [4, 12]);
    [0,2,4,6,8,10,12,14].forEach(i => { hat.steps[i].on = true; hat.steps[i].vel = i % 4 ? 0.55 : 1; });
    on(bass, [0, 3, 8, 11]);
    bass.steps[3].note = 3; bass.steps[11].note = 5;
    return {
      version: 1, bpm: 120, swing: 0, steps, resolution: 4, master_gain_db: 0,
      patterns: [{ name: "A", tracks: [kick, snare, hat, bass] },
                 { name: "B", tracks: [track("kick","drum","kick"),
                                       track("snare","drum","snare"),
                                       track("hat","drum","hat_open",{gain_db:-10}),
                                       track("bass","synth","saw",{gain_db:-6,pitch:-24})] }],
      song: ["A", "A", "A", "B"], notes: "",
    };
  }

  /* ── voice rendering ──────────────────────────────────────────────────── */
  // Deterministic noise. Math.random would make every render of the same
  // pattern a different file.
  function noiseGen(seed){
    let s = seed >>> 0 || 0x2545f491;
    return () => { s ^= s << 13; s ^= s >>> 17; s ^= s << 5;
                   return ((s >>> 0) / 0xffffffff) * 2 - 1; };
  }

  function drumBuffer(voice, decay, pitch){
    const rate = ctx.sampleRate;
    const spec = {
      kick:       { s: 0.42, f0: 150, f1: 42,  noise: 0.0,  curve: 26 },
      snare:      { s: 0.24, f0: 220, f1: 160, noise: 0.72, curve: 14 },
      hat_closed: { s: 0.06, f0: 8000, f1: 7000, noise: 1.0, curve: 60 },
      hat_open:   { s: 0.34, f0: 8000, f1: 7000, noise: 1.0, curve: 9 },
      clap:       { s: 0.22, f0: 1200, f1: 900, noise: 0.95, curve: 18 },
      tom:        { s: 0.36, f0: 260, f1: 90,  noise: 0.08, curve: 12 },
      rim:        { s: 0.08, f0: 1700, f1: 1500, noise: 0.35, curve: 55 },
      cowbell:    { s: 0.22, f0: 820, f1: 800, noise: 0.0,  curve: 18 },
    }[voice] || { s: 0.2, f0: 200, f1: 100, noise: 0.5, curve: 20 };

    const bend = Math.pow(2, pitch / 12);
    const seconds = clamp(spec.s * decay, 0.01, 4);
    const n = Math.max(1, Math.round(seconds * rate));
    const buf = ctx.createBuffer(1, n, rate);
    const d = buf.getChannelData(0);
    const rnd = noiseGen(0x9e3779b9 ^ voice.length * 2654435761);
    let phase = 0;
    // A clap is three quick bursts, not one — that is the whole character of
    // the sound, and a single burst reads as a snare with no body.
    const claps = voice === "clap" ? [0, 0.011, 0.023] : null;
    for (let i = 0; i < n; i++){
      const t = i / n;
      const env = Math.exp(-spec.curve * t);
      const f = (spec.f1 + (spec.f0 - spec.f1) * Math.exp(-9 * t)) * bend;
      phase += (2 * Math.PI * f) / rate;
      let v = Math.sin(phase) * (1 - spec.noise) + rnd() * spec.noise;
      if (voice === "cowbell") v = (Math.sin(phase) + Math.sin(phase * 1.5)) * 0.5;
      let e = env;
      if (claps){
        const ts = i / rate;
        e = 0;
        for (const off of claps){
          if (ts >= off) e = Math.max(e, Math.exp(-60 * (ts - off)));
        }
        e *= Math.exp(-6 * t);
      }
      d[i] = clamp(v * e, -1, 1);
    }
    return buf;
  }

  function synthBuffer(voice, decay, note, pitch){
    const rate = ctx.sampleRate;
    const seconds = clamp(0.5 * decay, 0.02, 4);
    const n = Math.max(1, Math.round(seconds * rate));
    const buf = ctx.createBuffer(1, n, rate);
    const d = buf.getChannelData(0);
    const freq = 440 * Math.pow(2, (note + pitch) / 12);
    let phase = 0;
    const a = Math.round(0.004 * rate), r = Math.round(0.05 * rate);
    for (let i = 0; i < n; i++){
      phase += (2 * Math.PI * freq) / rate;
      const p = (phase / (2 * Math.PI)) % 1;
      let v;
      switch (voice){
        case "sine":     v = Math.sin(phase); break;
        case "square":   v = p < 0.5 ? 1 : -1; break;
        case "saw":      v = p * 2 - 1; break;
        default:         v = 2 * Math.abs(p * 2 - 1) - 1; break;
      }
      let env = Math.exp(-4.5 * (i / n));
      if (i < a) env *= i / a;
      if (i > n - r) env *= (n - i) / r;
      d[i] = clamp(v * env * 0.7, -1, 1);
    }
    return buf;
  }

  /* Cached by everything that affects the samples. A cache keyed on less than
     that is a slider that silently stops working. */
  function voiceBuffer(t, note){
    if (t.kind === "sample") return sampleCache.get(t.source) || null;
    const key = `${t.kind}:${t.voice}:${t.decay}:${t.pitch}:${t.kind === "synth" ? note : 0}`;
    if (!voiceCache.has(key)){
      voiceCache.set(key, t.kind === "drum"
        ? drumBuffer(t.voice, t.decay, t.pitch)
        : synthBuffer(t.voice, t.decay, note, t.pitch));
    }
    return voiceCache.get(key);
  }
  function invalidate(){ voiceCache.clear(); }

  async function loadSamples(){
    const wanted = new Set();
    B.patterns.forEach(p => p.tracks.forEach(t => {
      if (t.kind === "sample" && t.source) wanted.add(t.source); }));
    for (const rel of wanted){
      if (sampleCache.has(rel)) continue;
      try {
        const bytes = await fetch(`/api/audio/file?rel=${encodeURIComponent(rel)}`)
          .then(r => r.arrayBuffer());
        sampleCache.set(rel, await ctx.decodeAudioData(bytes));
      } catch (e){ say(`could not load ${rel}`); sampleCache.set(rel, null); }
    }
  }

  /* ── timing ───────────────────────────────────────────────────────────── */
  function stepSeconds(){ return 60 / B.bpm / B.resolution; }
  /* Swing delays every OFF-beat step. Applying it to every step would just be
     a slower tempo, which is the classic way to implement swing wrongly. */
  function stepOffset(i){
    return (i % 2 === 1) ? stepSeconds() * B.swing * 0.5 : 0;
  }
  function patternSeconds(){ return stepSeconds() * B.steps; }
  function songSeconds(){ return patternSeconds() * Math.max(1, B.song.length); }

  function audibleTracks(pattern){
    const solo = pattern.tracks.some(t => t.solo);
    return pattern.tracks.filter(t => !t.muted && (!solo || t.solo));
  }

  /* One scheduling primitive, used by the live scheduler AND the offline
     render. Two code paths here would be two different-sounding results. */
  function fire(destCtx, dest, t, cell, when){
    const buf = voiceBuffer(t, cell.note);
    if (!buf) return;
    const src = destCtx.createBufferSource();
    src.buffer = buf;
    let tail = src;
    const g = destCtx.createGain();
    g.gain.value = Math.pow(10, t.gain_db / 20) * cell.vel;
    tail.connect(g); tail = g;
    if (destCtx.createStereoPanner && Math.abs(t.pan) > 0.001){
      const p = destCtx.createStereoPanner();
      p.pan.value = clamp(t.pan, -1, 1);
      tail.connect(p); tail = p;
    }
    tail.connect(dest);
    src.start(when);
  }

  function scheduleStep(destCtx, dest, patternIndex, step, when){
    const pattern = B.patterns[patternIndex];
    if (!pattern) return;
    audibleTracks(pattern).forEach(t => {
      const cell = t.steps[step];
      if (cell && cell.on) fire(destCtx, dest, t, cell, when + stepOffset(step));
    });
  }

  /* ── live playback ────────────────────────────────────────────────────── */
  function play(songMode){
    stop();
    if (!B) return;
    ui.playing = true; ui.song = !!songMode;
    try { ctx.resume(); } catch (e) {}
    nextStep = 0;
    nextSongIndex = 0;
    nextTime = ctx.currentTime + 0.06;
    timer = setInterval(tick, TICK_MS);
    tick();
    render();
  }

  function tick(){
    if (!B || !ui.playing) return;
    while (nextTime < ctx.currentTime + LOOKAHEAD_S){
      const patternIndex = ui.song
        ? B.patterns.findIndex(p => p.name === B.song[nextSongIndex % B.song.length])
        : patIndex();
      scheduleStep(ctx, ctx.destination, patternIndex < 0 ? 0 : patternIndex,
                   nextStep, nextTime);
      const at = nextTime, at_step = nextStep;
      // The playhead is a VIEW of the clock, never the thing driving it.
      setTimeout(() => { if (ui.playing){ ui.step = at_step; paintPlayhead(); } },
                 Math.max(0, (at - ctx.currentTime) * 1000));
      nextTime += stepSeconds();
      nextStep++;
      if (nextStep >= B.steps){
        nextStep = 0;
        nextSongIndex++;
      }
    }
  }

  function stop(){
    if (timer) clearInterval(timer);
    timer = null;
    ui.playing = false; ui.step = -1;
    paintPlayhead();
    render();
  }
  function toggle(songMode){ ui.playing ? stop() : play(songMode); }

  /* ── offline render ───────────────────────────────────────────────────── */
  async function renderBuffer(songMode){
    const rate = ctx.sampleRate;
    const seconds = (songMode ? songSeconds() : patternSeconds()) + 1.2;  // tail
    const off = new OfflineAudioContext(2, Math.ceil(seconds * rate), rate);
    const master = off.createGain();
    master.gain.value = Math.pow(10, B.master_gain_db / 20);
    master.connect(off.destination);

    const bars = songMode ? B.song.length : 1;
    for (let bar = 0; bar < bars; bar++){
      const pi = songMode
        ? Math.max(0, B.patterns.findIndex(p => p.name === B.song[bar]))
        : patIndex();
      for (let s = 0; s < B.steps; s++){
        scheduleStep(off, master, pi, s, bar * patternSeconds() + s * stepSeconds());
      }
    }
    return off.startRendering();
  }

  /* Four voices on one step sum past 1.0 easily — a kick and a bass on beat one
   * is already there. Left alone that clips twice: once in the float buffer and
   * again, harder, when the save encodes to 16-bit PCM. So the render is scaled
   * back to 0.99 when it overshoots, and the amount is REPORTED rather than
   * quietly applied — a mix that got 3 dB quieter without being told is a mix
   * you fight for ten minutes. */
  function headroom(buf){
    let peak = 0;
    for (let c = 0; c < buf.numberOfChannels; c++){
      const d = buf.getChannelData(c);
      for (let i = 0; i < d.length; i++){
        const v = d[i] < 0 ? -d[i] : d[i];
        if (v > peak) peak = v;
      }
    }
    if (peak <= 0.99) return { peak, trimmed_db: 0 };
    const f = 0.99 / peak;
    for (let c = 0; c < buf.numberOfChannels; c++){
      const d = buf.getChannelData(c);
      for (let i = 0; i < d.length; i++) d[i] *= f;
    }
    return { peak, trimmed_db: 20 * Math.log10(f) };
  }

  async function renderTo(songMode){
    if (!window.AudioLab || !AudioLab.state){ say("open the audio lab first"); return; }
    // Called straight from an inline onclick, so a throw in here used to become
    // an unhandled rejection: the button did nothing and said nothing.
    try {
      stop();
      await loadSamples();
      const rendered = await renderBuffer(songMode);
      const head = headroom(rendered);
      const what = songMode ? `the song · ${B.song.join(" ")}`
                            : `pattern ${pat().name}`;
      AudioLab.adopt(rendered, `rendered ${what}` + (head.trimmed_db
        ? ` · pulled back ${Math.abs(head.trimmed_db).toFixed(1)} dB to stop it clipping`
        : ""));
    } catch (e){
      say(`could not render: ${(e && e.message) || e}`);
    }
  }

  /* ── session ──────────────────────────────────────────────────────────── */
  async function saveBeat(){
    const rel = (AudioLab.state && (AudioLab.state.rel || AudioLab.state.saveAs));
    if (!rel){ say("give the clip a path first"); return; }
    const r = await mutate("/api/audio/lab/beat", { body: { rel, beat: B }});
    if (r.ok) say(`pattern saved to ${r.data.path}`, "ok");
  }

  /* ── UI ───────────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("beatmaker-style")) return;
    const s = document.createElement("style");
    s.id = "beatmaker-style";
    s.textContent = [
      ".bm{display:flex;flex-direction:column;height:100%;min-height:0;background:var(--bg)}",
      ".bm-top{display:flex;align-items:center;gap:9px;padding:9px 13px;border-bottom:1px solid var(--seam);background:var(--iron);flex-wrap:wrap;flex:none}",
      ".bm-l{font-family:var(--mono);font-size:9px;letter-spacing:.16em;text-transform:uppercase;color:var(--ash2)}",
      ".bm-in{background:var(--void);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font:inherit;font-size:11.5px;padding:4px 7px;width:66px}",
      ".bm-in:focus{outline:none;border-color:var(--ember)}",
      ".bm-in.wide{width:auto;flex:1;min-width:130px}",
      ".bm-b{padding:5px 10px;background:var(--plate);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;cursor:pointer}",
      ".bm-b:hover{border-color:var(--ember)}",
      ".bm-b.go{background:var(--ember);color:var(--bg);border-color:var(--ember);font-weight:var(--fw-semi)}",
      ".bm-b.on{border-color:var(--ember);background:var(--plate2)}",
      ".bm-pat{display:flex;gap:4px}",
      ".bm-pc{width:28px;height:26px;border-radius:6px;border:1px solid var(--seam);background:var(--plate);color:var(--ash);font:inherit;font-size:11px;cursor:pointer}",
      ".bm-pc.on{border-color:var(--ember);color:var(--bg);background:var(--ember);font-weight:var(--fw-semi)}",
      ".bm-grid{flex:1;overflow:auto;padding:10px 13px}",
      ".bm-ruler{display:grid;gap:3px;margin-bottom:5px;margin-left:190px}",
      ".bm-ruler div{font-family:var(--mono);font-size:8.5px;color:var(--ash2);text-align:center}",
      ".bm-ruler div.beat{color:var(--bone)}",
      ".bm-row{display:flex;align-items:center;gap:7px;margin-bottom:4px}",
      ".bm-head{width:183px;flex:none;display:flex;align-items:center;gap:5px}",
      ".bm-head .nm{flex:1;min-width:0;font-family:var(--mono);font-size:10.5px;color:var(--bone);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}",
      ".bm-head .nm.sel{color:var(--ember)}",
      ".bm-tg{font-family:var(--mono);font-size:8.5px;padding:1px 5px;border:1px solid var(--seam);border-radius:4px;background:none;color:var(--ash2);cursor:pointer}",
      ".bm-tg.on{border-color:var(--ember);color:var(--bone);background:var(--plate)}",
      ".bm-tg.solo.on{border-color:var(--good);color:var(--good)}",
      ".bm-steps{display:grid;gap:3px;flex:1}",
      ".bm-s{height:26px;border-radius:5px;border:1px solid var(--seam);background:var(--void);cursor:pointer;position:relative;padding:0}",
      ".bm-s.beat{border-color:var(--line)}",
      ".bm-s.on{background:var(--ember);border-color:var(--ember)}",
      ".bm-s.on.synth{background:var(--text);border-color:var(--text)}",
      ".bm-s .nt{position:absolute;inset:0;display:grid;place-items:center;font-family:var(--mono);font-size:8px;color:var(--bg);pointer-events:none}",
      ".bm-s.play{box-shadow:0 0 0 2px var(--bone)}",
      ".bm-foot{display:flex;align-items:center;gap:8px;padding:8px 13px;border-top:1px solid var(--seam);background:var(--iron);flex-wrap:wrap;flex:none}",
      ".bm-hint{font-family:var(--mono);font-size:9.5px;color:var(--ash2)}",
      ".bm-ins{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:8px 13px;border-top:1px solid var(--seam);background:var(--iron);flex:none}",
    ].join("\n");
    document.head.appendChild(s);
  }

  function mount(el, session){
    injectStyle();
    host = el;
    ctx = window.AudioLab && AudioLab.state ? AudioLab.state.ctx : null;
    if (!ctx){ host.innerHTML = `<div class="bm-hint" style="padding:24px">the audio lab is not open</div>`; return; }
    B = session || starter();
    // ui is module state and outlives a mount. Carrying pattern/track indices
    // over from the last clip pointed them off the end of the new one, which
    // render() papered over and every click handler then threw on.
    ui = { pattern: 0, track: 0, playing: false, step: -1, song: false };
    invalidate();
    loadSamples();
    render();
  }

  function unmount(){ stop(); host = null; }

  /* Single reader for the selected pattern; heals the index instead of leaving
     a stale one to throw in whichever handler is touched next. */
  function patIndex(){ if (!B.patterns[ui.pattern]) ui.pattern = 0; return ui.pattern; }
  function pat(){ return B.patterns[patIndex()]; }

  function render(){
    if (!host || !B) return;
    const pattern = pat();
    const cols = `repeat(${B.steps}, minmax(19px, 1fr))`;
    const per = B.resolution;

    host.innerHTML = `
      <div class="bm">
        <div class="bm-top">
          <span class="bm-l">bpm</span>
          <input class="bm-in" type="number" min="20" max="300" value="${B.bpm}"
                 onchange="BeatMaker.field('bpm',this.value)">
          <span class="bm-l">swing</span>
          <input class="bm-in" type="number" min="0" max="0.7" step="0.05" value="${B.swing}"
                 onchange="BeatMaker.field('swing',this.value)">
          <span class="bm-l">grid</span>
          <select class="bm-in" onchange="BeatMaker.field('resolution',this.value)">
            ${RESOLUTIONS.map(r => `<option value="${r.v}"${r.v===B.resolution?" selected":""}>${r.label}</option>`).join("")}
          </select>
          <span class="bm-l">steps</span>
          <input class="bm-in" type="number" min="1" max="64" value="${B.steps}"
                 onchange="BeatMaker.field('steps',this.value)">
          <span class="bm-l">pattern</span>
          <div class="bm-pat">
            ${B.patterns.map((p, i) => `<button class="bm-pc${i===ui.pattern?" on":""}"
              onclick="BeatMaker.selectPattern(${i})">${E(p.name)}</button>`).join("")}
            <button class="bm-pc" title="add a pattern" onclick="BeatMaker.addPattern()">+</button>
          </div>
          <span style="flex:1"></span>
          <button class="bm-b ${ui.playing && !ui.song ? "on" : "go"}"
                  onclick="BeatMaker.toggle(false)">${ui.playing && !ui.song ? "■ stop" : "▶ pattern"}</button>
          <button class="bm-b ${ui.playing && ui.song ? "on" : ""}"
                  onclick="BeatMaker.toggle(true)">${ui.playing && ui.song ? "■ stop" : "▶ song"}</button>
        </div>

        <div class="bm-grid">
          <div class="bm-ruler" style="grid-template-columns:${cols}">
            ${Array.from({length:B.steps}, (_, i) =>
              `<div class="${i % per === 0 ? "beat" : ""}">${i % per === 0 ? (i / per) + 1 : "·"}</div>`).join("")}
          </div>
          ${pattern.tracks.map((t, ti) => `
            <div class="bm-row">
              <div class="bm-head">
                <span class="nm${ti===ui.track?" sel":""}" title="${E(t.kind)} · ${E(t.voice || t.source)}"
                      onclick="BeatMaker.selectTrack(${ti})">${E(t.name)}</span>
                <button class="bm-tg${t.muted?" on":""}" onclick="BeatMaker.trackField(${ti},'muted',${!t.muted})">m</button>
                <button class="bm-tg solo${t.solo?" on":""}" onclick="BeatMaker.trackField(${ti},'solo',${!t.solo})">s</button>
                <button class="bm-tg" title="remove" onclick="BeatMaker.dropTrack(${ti})">✕</button>
              </div>
              <div class="bm-steps" style="grid-template-columns:${cols}">
                ${t.steps.map((c, si) => `<button
                  class="bm-s${c.on?" on":""}${t.kind==="synth"?" synth":""}${si%per===0?" beat":""}${ui.step===si?" play":""}"
                  data-t="${ti}" data-s="${si}"
                  style="${c.on ? `opacity:${(0.4 + c.vel * 0.6).toFixed(2)}` : ""}"
                  title="click toggle · shift-click accent${t.kind==="synth"?" · wheel = pitch":""}"
                  onclick="BeatMaker.stepClick(event,${ti},${si})"
                  ${t.kind==="synth" ? `onwheel="BeatMaker.stepWheel(event,${ti},${si})"` : ""}
                  >${c.on && t.kind==="synth" ? `<span class="nt">${noteName(c.note)}</span>` : ""}</button>`).join("")}
              </div>
            </div>`).join("")}
        </div>

        <div class="bm-ins">
          <span class="bm-l">add</span>
          ${DRUMS.map(v => `<button class="bm-b" onclick="BeatMaker.addTrack('drum','${v}')">${E(v.replace("_"," "))}</button>`).join("")}
          ${SYNTHS.map(v => `<button class="bm-b" onclick="BeatMaker.addTrack('synth','${v}')">${E(v)}</button>`).join("")}
          <button class="bm-b" onclick="BeatMaker.addSample()">project sample…</button>
        </div>

        ${trackPanel(pattern.tracks[ui.track], ui.track)}

        <div class="bm-foot">
          <span class="bm-l">song</span>
          <input class="bm-in wide" value="${E(B.song.join(" "))}"
                 title="Pattern names in order, e.g. A A B A"
                 onchange="BeatMaker.field('song',this.value)">
          <span class="bm-hint">${B.song.length} bar${B.song.length===1?"":"s"} ·
            ${songSeconds().toFixed(1)}s @ ${B.bpm}bpm</span>
          <span style="flex:1"></span>
          <button class="bm-b" onclick="BeatMaker.clearPattern()">clear</button>
          <button class="bm-b" onclick="BeatMaker.saveBeat()">save pattern</button>
          <button class="bm-b" onclick="BeatMaker.renderTo(false)">render pattern → clip</button>
          <button class="bm-b go" onclick="BeatMaker.renderTo(true)">render song → clip</button>
        </div>
      </div>`;
  }

  function trackPanel(t, ti){
    if (!t) return "";
    return `<div class="bm-ins">
      <span class="bm-l">${E(t.name)}</span>
      <input class="bm-in wide" value="${E(t.name)}" style="max-width:130px"
             onchange="BeatMaker.trackField(${ti},'name',this.value)">
      ${t.kind !== "sample" ? `<select class="bm-in" style="width:auto"
        onchange="BeatMaker.trackField(${ti},'voice',this.value)">
        ${(t.kind === "drum" ? DRUMS : SYNTHS).map(v =>
          `<option value="${v}"${t.voice===v?" selected":""}>${E(v)}</option>`).join("")}
      </select>` : `<span class="bm-hint">${E(t.source)}</span>`}
      <span class="bm-l">gain</span>
      <input class="bm-in" type="number" step="1" min="-60" max="12" value="${t.gain_db}"
             onchange="BeatMaker.trackField(${ti},'gain_db',this.value)">
      <span class="bm-l">pan</span>
      <input class="bm-in" type="number" step="0.1" min="-1" max="1" value="${t.pan}"
             onchange="BeatMaker.trackField(${ti},'pan',this.value)">
      <span class="bm-l">pitch</span>
      <input class="bm-in" type="number" step="1" min="-24" max="24" value="${t.pitch}"
             onchange="BeatMaker.trackField(${ti},'pitch',this.value)">
      <span class="bm-l">decay</span>
      <input class="bm-in" type="number" step="0.05" min="0.05" max="4" value="${t.decay}"
             onchange="BeatMaker.trackField(${ti},'decay',this.value)">
      <span class="bm-hint">shift-click a step for accent${t.kind==="synth" ? " · scroll a step to pitch it" : ""}</span>
    </div>`;
  }

  function noteName(n){
    const i = ((n % 12) + 12) % 12;
    return NOTE_NAMES[i] + (n >= 12 ? "+" : n <= -12 ? "−" : "");
  }

  function paintPlayhead(){
    if (!host) return;
    host.querySelectorAll(".bm-s").forEach(el =>
      el.classList.toggle("play", +el.dataset.s === ui.step));
  }

  /* ── actions ──────────────────────────────────────────────────────────── */
  function stepClick(ev, ti, si){
    const t = pat().tracks[ti];
    const c = t.steps[si];
    if (ev && ev.shiftKey && c.on){
      // Accent cycles down through three levels rather than toggling off, so
      // shift is always "make it quieter", never "did that delete it?".
      c.vel = c.vel > 0.8 ? 0.66 : c.vel > 0.5 ? 0.33 : 1;
    } else {
      c.on = !c.on;
      if (c.on) c.vel = c.vel || 1;
    }
    ui.track = ti;
    render();
    if (c.on) fire(ctx, ctx.destination, t, c, ctx.currentTime + 0.01);
  }
  function stepWheel(ev, ti, si){
    ev.preventDefault();
    const t = pat().tracks[ti];
    const c = t.steps[si];
    if (!c.on) return;
    c.note = clamp(c.note + (ev.deltaY < 0 ? 1 : -1), -36, 36);
    render();
    fire(ctx, ctx.destination, t, c, ctx.currentTime + 0.01);
  }
  function selectPattern(i){ ui.pattern = i; ui.track = 0; render(); }
  function selectTrack(i){ ui.track = i; render(); }
  function addPattern(){
    if (B.patterns.length >= 8){ say("eight patterns is the cap"); return; }
    const src = pat();
    const name = String.fromCharCode(65 + B.patterns.length);
    B.patterns.push({ name, tracks: src.tracks.map(t => Object.assign({}, t,
      { steps: t.steps.map(c => Object.assign({}, c)) })) });
    ui.pattern = B.patterns.length - 1;
    render();
    say(`pattern ${name} copied from ${src.name}`, "ok");
  }
  function addTrack(kind, voice){
    const p = pat();
    if (p.tracks.length >= 16){ say("sixteen tracks is the cap"); return; }
    p.tracks.push(track(voice.replace("_", " "), kind, voice));
    ui.track = p.tracks.length - 1;
    render();
  }
  /* The lab's picker, not a second one: same endpoint, same rows, and a path
     typed by hand was never a real way to choose a file. */
  async function addSample(){
    if (!window.AudioLab || !AudioLab.pickSound){ say("open the audio lab first"); return; }
    const rel = await AudioLab.pickSound("which project sound?");
    if (!rel) return;
    const p = pat();
    p.tracks.push(track(rel.split("/").pop(), "sample", "", { source: rel }));
    ui.track = p.tracks.length - 1;
    await loadSamples();
    render();
  }
  function dropTrack(ti){
    pat().tracks.splice(ti, 1);
    ui.track = 0;
    render();
  }
  function trackField(ti, key, v){
    const t = pat().tracks[ti];
    if (!t) return;
    if (key === "muted" || key === "solo") t[key] = !!v;
    else if (key === "name" || key === "voice") t[key] = String(v);
    else t[key] = parseFloat(v) || 0;
    invalidate();
    render();
  }
  function clearPattern(){
    pat().tracks.forEach(t =>
      t.steps.forEach(c => { c.on = false; }));
    render();
  }
  function field(key, v){
    if (key === "song"){
      const names = B.patterns.map(p => p.name);
      const wanted = String(v).toUpperCase().split(/[\s,]+/).filter(Boolean);
      const bad = wanted.filter(x => !names.includes(x));
      if (bad.length){ say(`no pattern named ${bad[0]} — have ${names.join(", ")}`); render(); return; }
      B.song = wanted.slice(0, 64);
    } else if (key === "steps"){
      const n = clamp(parseInt(v, 10) || 16, 1, 64);
      B.steps = n;
      // Grow with empty steps, shrink by truncating — never by rebuilding, or
      // resizing the grid would silently erase a pattern.
      B.patterns.forEach(p => p.tracks.forEach(t => {
        while (t.steps.length < n) t.steps.push({ on:false, vel:1, note:0 });
        t.steps.length = n;
      }));
    } else if (key === "resolution"){
      B.resolution = parseInt(v, 10) || 4;
      // An empty box gives NaN and `NaN || 0` is 0 — bpm 0 makes stepSeconds()
      // Infinity, which stalls the scheduler after one step and makes the
      // offline render throw. min= on the input does not stop an onchange.
    } else if (key === "bpm"){
      B.bpm = clamp(parseFloat(v) || 120, 20, 300);
    } else if (key === "swing"){
      B.swing = clamp(parseFloat(v) || 0, 0, 0.7);
    } else {
      B[key] = parseFloat(v) || 0;
    }
    if (ui.playing) play(ui.song);       // re-arm the scheduler on a tempo change
    render();
  }

  return {
    mount, unmount, render, play, stop, toggle, renderTo, saveBeat,
    stepClick, stepWheel, selectPattern, selectTrack, addPattern, addTrack,
    addSample, dropTrack, trackField, clearPattern, field,
    starter,
    get session(){ return B; },
  };
})();
