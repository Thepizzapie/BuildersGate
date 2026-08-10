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
    if (s == null || !isFinite(s)) return "-";
    const m = Math.floor(s / 60), r = s - m * 60;
    return m ? `${m}:${r.toFixed(2).padStart(5, "0")}` : `${r.toFixed(3)}s`;
  };

  // An AudioBuffer is ~170 KB per second per channel at 44.1k float32, so the
  // history is capped by BYTES like the sprite editor's. Eight edits on a
  // 45-second stereo track is already 120 MB.
  const UNDO_BYTES = 320 * 1024 * 1024;

  const WAVES = ["sine", "square", "saw", "triangle", "noise"];

  let S = null, $ = {};

  /* ── geometry ─────────────────────────────────────────────────────────────
   * The arrangement's row height is a CONSTANT, not a fraction of the pane.
   * The track headers are DOM and the lanes are a canvas, and the only way two
   * different rendering systems agree on where row 4 starts is if neither of
   * them gets to decide. Everything below — the header column, the canvas, the
   * hit tests, the shared vertical scroll — reads these three numbers. */
  const RULER_H = 26;                    // the time strip along the top, CSS px
  const LANE_H  = 56;                    // one arrangement row
  const HEADS_W = 254;                   // the fixed left column

  /* ── track colour ─────────────────────────────────────────────────────────
   * Soundtrap tells its tracks apart by colour before it tells them apart by
   * name, and that is the one part of its palette that cannot come from the
   * theme tokens: a clip has to stay ITSELF across the parchment ground, the
   * charcoal ground and orbit's #000000.
   *
   * So the hue is derived from the source path (stable across reorder, and a
   * split's two halves keep their parent's colour because they are the same
   * sound), and the LIGHTNESS is then solved — not chosen — so that every hue
   * lands on the same WCAG relative luminance, 0.26. That single number is the
   * whole guarantee:
   *
   *     vs white (1.0):  1.05 / 0.31 = 3.4:1
   *     vs black (0.0):  0.31 / 0.05 = 6.2:1
   *
   * Both clear the 3:1 floor for graphical objects, so the same swatch reads on
   * paper and on pure black without a per-ground palette. Solving for luminance
   * rather than picking an HSL lightness is what stops yellow from blowing out
   * and blue from disappearing — at a fixed L=55% those two differ by 4x.
   *
   * The ink drawn INSIDE a clip is white at 85%: at luminance 0.26 the block is
   * always dark enough to take it, on every ground. That is part of the clip
   * palette too, not a stray hardcoded colour. */
  const CLIP_INK = "rgba(255,255,255,.86)";
  const CLIP_LUM = 0.26;
  const _clipColor = new Map();

  /* Golden-ratio hashing rather than `hash % 360`. Two paths that differ only
     in their last few characters produce adjacent 32-bit hashes, and modulo 360
     then hands them adjacent hues — observed: sfx_ability_cast and sfx_back
     came back as two oranges 11° apart, which is not an identity, it is a
     smudge. Multiplying into the fractional part scatters neighbours. */
  function _hue(key){
    let h = 2166136261;
    const s = String(key || "");
    for (let i = 0; i < s.length; i++){
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return ((h >>> 0) * 0.6180339887498949 % 1) * 360;
  }
  function _hsl(h, s, l){                       // → [r,g,b] in 0..1
    const c = (1 - Math.abs(2 * l - 1)) * s, hh = h / 60;
    const x = c * (1 - Math.abs((hh % 2) - 1)), m = l - c / 2;
    const t = hh < 1 ? [c,x,0] : hh < 2 ? [x,c,0] : hh < 3 ? [0,c,x]
            : hh < 4 ? [0,x,c] : hh < 5 ? [x,0,c] : [c,0,x];
    return [t[0] + m, t[1] + m, t[2] + m];
  }
  function _lum(rgb){
    const f = v => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]);
  }
  function _hex(rgb){
    return "#" + rgb.map(v =>
      Math.round(clamp(v, 0, 1) * 255).toString(16).padStart(2, "0")).join("");
  }
  /* The accent's own hue is RESERVED. Lane 0 — the open clip — is drawn in
     --accent, so a layer that lands within 26° of it is a layer you cannot tell
     from the clip. Observed on the orbit ground, whose accent is a 203° blue:
     the first layer came out #2393d5, one degree away. Seeded lazily because
     the ground can change under us, and re-seeded on a ground change so new
     tracks avoid the new accent; already-coloured tracks keep what they have. */
  function _rgbHue(css){
    let r, g, b;
    const m = /^#?([0-9a-f]{6})$/i.exec(String(css || "").trim());
    if (m){
      const n = parseInt(m[1], 16);
      r = (n >> 16 & 255) / 255; g = (n >> 8 & 255) / 255; b = (n & 255) / 255;
    } else {
      const p = /rgba?\(([^)]+)\)/i.exec(String(css || ""));
      if (!p) return null;
      const v = p[1].split(",").map(Number);
      r = v[0] / 255; g = v[1] / 255; b = v[2] / 255;
    }
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    if (!d) return null;
    const h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return ((h * 60) % 360 + 360) % 360;
  }
  const _usedHues = [];
  function reserveAccentHue(){
    const h = _rgbHue(BGTheme.color("--accent"));
    if (h != null && _usedHues.indexOf(h) < 0) _usedHues.push(h);
  }
  try{ window.addEventListener("bgate:theme", () => { try{ reserveAccentHue(); }catch(e){} }); }catch(e){}

  function clipColor(key){
    const k = String(key || "clip");
    if (_clipColor.has(k)) return _clipColor.get(k);
    if (!_usedHues.length) reserveAccentHue();
    // Then push a new hue off any it would collide with. Order-dependent, but
    // the result is CACHED per source path, so a colour never moves once a
    // track has one — reordering or splitting lanes cannot recolour them.
    let h = _hue(k);
    // ((a-b+540) % 360) - 180 is the SIGNED shortest way round; its magnitude
    // is the circular distance. (Getting this inverted put two clips 11° apart
    // and the separation pass silently did nothing.)
    const near = x => _usedHues.some(u =>
      Math.abs(((x - u + 540) % 360) - 180) < 26);
    for (let i = 0; i < 8 && near(h); i++) h = (h + 47) % 360;
    _usedHues.push(h);
    // Bisect the lightness until the hue sits on the target luminance. 18 steps
    // is far past the precision 8-bit channels can express.
    let lo = 0.05, hi = 0.95, sat = 0.72;
    for (let i = 0; i < 18; i++){
      const mid = (lo + hi) / 2;
      if (_lum(_hsl(h, sat, mid)) < CLIP_LUM) lo = mid; else hi = mid;
    }
    const out = _hex(_hsl(h, sat, (lo + hi) / 2));
    _clipColor.set(k, out);
    return out;
  }
  /* Lane 0 is the open clip and is not one of the coloured layers: it is the
     thing everything else is arranged AGAINST, so it takes the theme accent and
     follows the ground like the rest of the chrome. */
  function laneColor(i){
    if (!i) return BGTheme.color("--accent");
    const t = S.tracks[i - 1];
    return clipColor(t ? (t.source || t.name) : "lane" + i);
  }

  /* ── styles ───────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("audiolab-style")) return;
    const s = document.createElement("style");
    s.id = "audiolab-style";
    s.textContent = [
      // --overlay, not a baked near-black: on the light ground a hardcoded dark
      // scrim buries the panel it is supposed to sit behind.
      ".ab-back{position:fixed;inset:0;z-index:1400;background:var(--overlay);backdrop-filter:blur(3px);display:flex;flex-direction:column;font-size:var(--fs-sm)}",
      // Embedded in a Studio tab: a panel in the page, not a sheet over it.
      ".ab-back.ab-embed{position:relative;inset:auto;z-index:auto;background:var(--surface-1);backdrop-filter:none;height:100%;border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden}",
      ".ab-back.ab-embed .ab-closebtn{display:none}",
      // A file is hovering over the pane. Outline rather than a border so the
      // layout does not shift by 2px the moment a drag enters.
      ".ab-back.ab-drop,.ab-land.ab-drop{outline:2px dashed var(--accent);outline-offset:-6px}",
      ".ab-land{display:grid;place-items:center;height:100%;min-height:420px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-lg)}",
      ".ab-land-in{text-align:center;max-width:420px;padding:var(--s-8)}",
      ".ab-land-btns{display:flex;gap:var(--s-3);justify-content:center;flex-wrap:wrap}",
      ".ab-land-in h3{font-size:var(--fs-xl);font-weight:var(--fw-regular);color:var(--text);margin-bottom:var(--s-4)}",
      ".ab-land-in p{color:var(--text-3);font-size:var(--fs-md);line-height:var(--lh);margin-bottom:var(--s-7)}",

      /* ── top bar ────────────────────────────────────────────────────────
         Soundtrap's: mark, inline-editable title, a saved state, then the
         actions. The title here is the SAVE PATH, because that is what this
         document's name actually is. */
      ".ab-bar{display:flex;align-items:center;gap:var(--s-3);padding:var(--s-3) var(--s-5);border-bottom:1px solid var(--line);background:var(--surface-1);flex:none;min-height:46px}",
      ".ab-brand{display:flex;align-items:center;gap:var(--s-3);color:var(--text-2);flex:none}",
      ".ab-title{font-family:var(--mono);font-size:var(--fs-2xs);letter-spacing:var(--track-label);color:var(--text-3);text-transform:uppercase;white-space:nowrap}",
      ".ab-name{flex:1;min-width:90px;max-width:520px;background:transparent;border:1px solid transparent;border-radius:var(--r-xs);color:var(--text);font:inherit;font-family:var(--mono);font-size:var(--fs-sm);padding:var(--s-2) var(--s-3)}",
      ".ab-name:hover{border-color:var(--line)}",
      ".ab-name:focus{outline:none;border-color:var(--accent);background:var(--surface-2)}",
      ".ab-saved{display:flex;align-items:center;gap:var(--s-2);font-family:var(--mono);font-size:var(--fs-2xs);color:var(--good);flex:none;white-space:nowrap}",
      ".ab-saved.dirty{color:var(--warn)}",
      ".ab-saved i{width:7px;height:7px;border-radius:var(--r-full);background:currentColor;display:block}",
      ".ab-sub{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-3)}",
      ".ab-dirty{color:var(--warn)}",
      ".ab-spacer{flex:1}",
      ".ab-sep{width:1px;align-self:stretch;background:var(--line);margin:var(--s-2) var(--s-1);flex:none}",

      /* buttons ─ one family: a pill for a toggle, a plate for an action,
         a circle for an icon-only control. */
      ".ab-btn{display:inline-flex;align-items:center;gap:var(--s-2);padding:var(--s-3) var(--s-4);background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-xs);color:var(--text);font:inherit;font-size:var(--fs-xs);cursor:pointer;white-space:nowrap}",
      ".ab-btn:hover:not(:disabled){border-color:var(--accent);background:var(--surface-3)}",
      ".ab-btn:disabled{opacity:.4;cursor:default}",
      ".ab-btn.go{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}",
      ".ab-btn.go:hover:not(:disabled){background:var(--accent-hover);border-color:var(--accent-hover)}",
      ".ab-btn.wide{width:100%;justify-content:center;margin-bottom:var(--s-3)}",
      ".ab-btn.sm{padding:var(--s-2) var(--s-3);font-size:var(--fs-2xs)}",
      ".ab-ico{display:inline-grid;place-items:center;width:30px;height:30px;padding:0;flex:none;border-radius:var(--r-xs);background:var(--surface-2);border:1px solid var(--line);color:var(--text-2);cursor:pointer}",
      ".ab-ico:hover:not(:disabled){color:var(--text);border-color:var(--accent)}",
      ".ab-ico:disabled{opacity:.35;cursor:default}",
      ".ab-ico.on{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}",
      ".ab-ico.rnd{border-radius:var(--r-full)}",
      ".ab-tg{display:inline-flex;align-items:center;gap:var(--s-2);font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:.06em;padding:var(--s-2) var(--s-4);border:1px solid var(--line);border-radius:var(--r-full);background:var(--surface-2);color:var(--text-3);cursor:pointer;white-space:nowrap}",
      ".ab-tg:hover{color:var(--text);border-color:var(--line-strong)}",
      ".ab-tg.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}",
      ".ab-tg.ok.on{border-color:var(--good);color:var(--good);background:var(--good-soft)}",
      ".ab-tg.bad.on{border-color:var(--bad);color:var(--bad);background:var(--bad-soft)}",

      /* ── the stack: arrangement over sheet over transport, rail at the edge */
      ".ab-body{flex:1;display:flex;min-height:0;background:var(--bg)}",
      ".ab-main{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0}",

      /* ── arrangement ────────────────────────────────────────────────────
         A fixed left column of track headers and a canvas of lanes, sharing
         one vertical scroll offset and one row height. */
      ".ab-arr{flex:1;min-height:0;display:flex;overflow:hidden;background:var(--bg)}",
      ".ab-arr[hidden]{display:none}",
      ".ab-heads{width:" + HEADS_W + "px;flex:none;display:flex;flex-direction:column;background:var(--surface-1);border-right:1px solid var(--line);overflow:hidden}",
      ".ab-heads-top{height:" + RULER_H + "px;flex:none;display:flex;align-items:center;gap:var(--s-2);padding:0 var(--s-3);border-bottom:1px solid var(--line);background:var(--surface-2)}",
      ".ab-heads-top .l{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text-3);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".ab-heads-top .mini{width:20px;height:18px;border-radius:var(--r-xs);border:1px solid var(--line);background:var(--surface-1);color:var(--text-3);display:grid;place-items:center;cursor:pointer;padding:0;flex:none}",
      ".ab-heads-top .mini:hover{color:var(--text);border-color:var(--accent)}",
      ".ab-heads-clip{flex:1;min-height:0;overflow:hidden;position:relative}",
      ".ab-heads-in{position:absolute;left:0;right:0;top:0;will-change:transform}",
      ".ab-head{height:" + LANE_H + "px;box-sizing:border-box;display:flex;align-items:center;gap:var(--s-2);padding:0 var(--s-3) 0 var(--s-4);border-bottom:1px solid var(--line-soft);cursor:pointer;position:relative}",
      ".ab-head:hover{background:var(--surface-2)}",
      ".ab-head.sel{background:var(--surface-2)}",
      // The colour bar is the track's identity, held at the edge where the eye
      // learns one position for it.
      ".ab-head .bar{position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--line)}",
      ".ab-head.sel .bar{width:4px}",
      ".ab-head .badge{width:24px;height:24px;flex:none;border-radius:var(--r-full);display:grid;place-items:center;border:1px solid currentColor;background:var(--surface-2)}",
      ".ab-head .col{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}",
      ".ab-head .nm{width:100%;background:transparent;border:1px solid transparent;border-radius:var(--r-xs);color:var(--text);font:inherit;font-family:var(--mono);font-size:var(--fs-xs);padding:1px var(--s-2);text-overflow:ellipsis}",
      ".ab-head .nm:hover{border-color:var(--line)}",
      ".ab-head .nm:focus{outline:none;border-color:var(--accent);background:var(--surface-3)}",
      ".ab-head .nm[readonly]{cursor:default}",
      ".ab-head .meta{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);padding-left:var(--s-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".ab-head .btns{display:flex;align-items:center;gap:2px;flex:none}",
      ".ab-head .hb{width:21px;height:21px;padding:0;display:grid;place-items:center;border:1px solid transparent;border-radius:var(--r-xs);background:none;color:var(--text-3);cursor:pointer}",
      ".ab-head .hb:hover{color:var(--text);border-color:var(--line)}",
      ".ab-head .hb.on{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}",
      ".ab-head .hb.solo.on{color:var(--good);border-color:var(--good);background:var(--good-soft)}",
      ".ab-head .hb.rec.on{color:var(--bad);border-color:var(--bad);background:var(--bad-soft)}",
      ".ab-lanes{flex:1;position:relative;min-width:0;min-height:0}",
      ".ab-lanes canvas{position:absolute;inset:0;width:100%;height:100%;touch-action:none}",
      // The overlay owns the pointer: it is the topmost layer, and it is the
      // only one that repaints while the playhead moves.
      ".ab-lanes canvas.over{cursor:grab;background:transparent}",
      ".ab-wave{flex:1;position:relative;min-height:120px}",
      ".ab-wave canvas{position:absolute;inset:0;width:100%;height:100%;touch-action:none}",
      ".ab-wave canvas.over{cursor:text;background:transparent}",
      ".ab-hud{position:absolute;left:var(--s-4);bottom:var(--s-3);font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);background:var(--surface-1);border:1px solid var(--line);border-radius:var(--r-xs);padding:2px var(--s-3);pointer-events:none;white-space:pre;opacity:.92}",

      /* ── the context sheet ──────────────────────────────────────────────
         Soundtrap's bottom sheet: whatever is selected gets its editor here,
         and the arrangement above it collapses rather than being navigated
         away from. */
      ".ab-sheet{flex:none;display:flex;flex-direction:column;background:var(--surface-1);border-top:1px solid var(--line);min-height:0}",
      ".ab-sheet.collapsed{height:auto!important}",
      ".ab-grip{height:7px;flex:none;cursor:ns-resize;background:var(--surface-2);border-bottom:1px solid var(--line);display:grid;place-items:center}",
      ".ab-grip::after{content:'';width:44px;height:2px;border-radius:2px;background:var(--line-strong)}",
      ".ab-grip:hover::after{background:var(--accent)}",
      ".ab-sheet.collapsed .ab-grip{cursor:default}",
      ".ab-tabs{display:flex;align-items:center;gap:var(--s-2);padding:var(--s-3) var(--s-4);border-bottom:1px solid var(--line);flex:none;overflow-x:auto;scrollbar-width:thin}",
      ".ab-tab{display:inline-flex;align-items:center;gap:var(--s-2);font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);text-transform:uppercase;padding:var(--s-2) var(--s-4);border:1px solid transparent;border-radius:var(--r-full);background:none;color:var(--text-3);cursor:pointer;white-space:nowrap}",
      ".ab-tab:hover{color:var(--text)}",
      ".ab-tab.on{color:var(--accent);border-color:var(--accent-line);background:var(--accent-soft)}",
      ".ab-pane{flex:1;min-height:0;overflow:auto;padding:var(--s-5)}",
      ".ab-pane.flush{padding:0;display:flex;min-height:0}",
      ".ab-sheet.collapsed .ab-pane,.ab-sheet.collapsed .ab-tabhint{display:none}",
      ".ab-clipwrap{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0}",
      ".ab-clipbar{display:flex;align-items:center;gap:var(--s-2);padding:var(--s-3) var(--s-4);border-bottom:1px solid var(--line-soft);flex:none;flex-wrap:wrap;background:var(--surface-1)}",
      ".ab-rack{width:288px;flex:none;border-left:1px solid var(--line);overflow-y:auto;padding:var(--s-4);background:var(--surface-1)}",

      /* ── the panel rail ─────────────────────────────────────────────────
         Soundtrap's right edge: a vertical stack of circular buttons, each
         opening a panel into the sheet. */
      ".ab-rail{width:66px;flex:none;display:flex;flex-direction:column;align-items:center;gap:var(--s-2);padding:var(--s-4) 0;background:var(--surface-1);border-left:1px solid var(--line);overflow-y:auto}",
      ".ab-rb{width:52px;padding:var(--s-2) 0;border:0;background:none;color:var(--text-3);cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:3px}",
      ".ab-rb i{display:grid;place-items:center;width:34px;height:34px;border-radius:var(--r-full);border:1px solid var(--line);background:var(--surface-2)}",
      ".ab-rb s{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:.04em;text-decoration:none;text-transform:uppercase}",
      ".ab-rb:hover{color:var(--text)}",
      ".ab-rb:hover i{border-color:var(--line-strong)}",
      ".ab-rb.on{color:var(--accent)}",
      ".ab-rb.on i{border-color:var(--accent);background:var(--accent-soft)}",

      /* ── the transport, which is always in the same place ───────────────── */
      ".ab-tr{flex:none;display:flex;align-items:center;gap:var(--s-3);padding:var(--s-3) var(--s-5);border-top:1px solid var(--line);background:var(--surface-2);flex-wrap:wrap}",
      ".ab-tr .grp{display:flex;align-items:center;gap:var(--s-2)}",
      ".ab-clock{font-family:var(--mono);font-size:var(--fs-xl);letter-spacing:-.01em;color:var(--text);min-width:104px;text-align:center;font-variant-numeric:tabular-nums}",
      ".ab-tr .big{width:38px;height:38px;border-radius:var(--r-full)}",
      ".ab-tr .rec.armed{color:var(--bad);border-color:var(--bad)}",
      ".ab-vol{width:88px;accent-color:var(--accent)}",
      ".ab-read{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-3);white-space:nowrap}",
      ".ab-pop{position:absolute;bottom:46px;right:var(--s-5);z-index:5;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);box-shadow:var(--shadow-3);padding:var(--s-4);width:238px}",
      ".ab-pop[hidden]{display:none}",
      ".ab-pop .ab-h:first-child{margin-top:0}",

      // setMode()/openSheet() hide these with the `hidden` attribute, and the UA
      // rule that implements it ([hidden]{display:none}) loses to any author
      // display. Without this line hiding a flex box is a no-op.
      ".ab-studio[hidden],.ab-wave[hidden],.ab-stage[hidden],.ab-arr[hidden],.ab-rangebar[hidden]{display:none}",
      ".ab-studio{flex:1;min-height:0;display:flex}",
      ".ab-studio>*{flex:1;min-width:0}",
      // The range ops, which only exist while a range does.
      ".ab-rangebar{display:flex;align-items:center;gap:var(--s-2);padding:var(--s-2) var(--s-5);border-top:1px solid var(--accent-line);background:var(--accent-wash);flex:none;flex-wrap:wrap}",
      // The audition strip. Sits on the accent ground so a pending edit reads as
      // a state the pane is IN, not as one more row of buttons. Directly above
      // the transport, so it is always found in the same place.
      ".ab-stage{display:flex;align-items:center;gap:var(--s-2);padding:var(--s-2) var(--s-5);border-top:1px solid var(--accent-line);background:var(--accent-soft);flex:none;flex-wrap:wrap}",
      ".ab-stage .lbl{font-family:var(--mono);font-size:var(--fs-xs);color:var(--text)}",

      /* ── arc knobs ──────────────────────────────────────────────────────
         A knob for a continuous value, a pill for a toggle. Drag vertically;
         double-click returns it to its default. The arc and the readout are
         patched in place, never re-rendered — re-rendering a control mid-drag
         is how you kill the drag. */
      ".ab-knobs{display:flex;flex-wrap:wrap;gap:var(--s-4);margin-bottom:var(--s-4)}",
      ".ab-knob{display:flex;flex-direction:column;align-items:center;gap:1px;width:56px;flex:none;cursor:ns-resize;touch-action:none;user-select:none}",
      ".ab-knob.sm{width:34px;gap:0}",
      ".ab-knob svg{display:block;overflow:visible}",
      ".ab-knob .trk{stroke:var(--line-strong);fill:none}",
      ".ab-knob .arc{stroke:var(--accent);fill:none}",
      ".ab-knob .ptr{stroke:var(--text);fill:none}",
      ".ab-knob .kl{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:.08em;text-transform:uppercase;color:var(--text-3)}",
      ".ab-knob .kv{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text);font-variant-numeric:tabular-nums}",
      ".ab-knob:hover .kv{color:var(--accent)}",

      ".ab-h{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-wide);text-transform:uppercase;color:var(--text-3);margin:var(--s-6) 0 var(--s-3);display:flex;align-items:center;gap:var(--s-3)}",
      ".ab-h:first-child{margin-top:0}",
      ".ab-h span{flex:1;height:1px;background:var(--line)}",
      ".ab-row{display:flex;align-items:center;gap:var(--s-3);margin-bottom:var(--s-3);flex-wrap:wrap}",
      ".ab-row label{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-3);flex:none;min-width:46px}",
      ".ab-in{flex:1;min-width:0;background:var(--bg);border:1px solid var(--line);border-radius:var(--r-xs);color:var(--text);font:inherit;font-size:var(--fs-xs);padding:var(--s-3)}",
      ".ab-in:focus{outline:none;border-color:var(--accent)}",
      ".ab-in.num{flex:none;width:76px}",
      ".ab-grid2{display:grid;grid-template-columns:1fr 1fr;gap:var(--s-3);margin-bottom:var(--s-3)}",
      // A toggle sharing a row with a field must not be squeezed by it.
      ".ab-row .ab-tg{flex:none}",
      ".ab-row .ab-btn{flex:none}",
      ".ab-grid2 .ab-btn{width:100%;justify-content:center;margin-bottom:0}",
      ".ab-note{font-size:var(--fs-xs);color:var(--text-2);line-height:var(--lh);margin-bottom:var(--s-4)}",
      ".ab-note b{color:var(--text)}",
      ".ab-warn{color:var(--warn)}",
      ".ab-cols{display:flex;gap:var(--s-6);align-items:flex-start;flex-wrap:wrap}",
      ".ab-col{flex:1;min-width:240px}",
      ".ab-pick{position:fixed;inset:0;z-index:1401;background:var(--overlay);display:flex;align-items:center;justify-content:center;padding:40px}",
      ".ab-pbox{background:var(--surface-1);border:1px solid var(--line);border-radius:var(--r-md);width:min(720px,100%);max-height:100%;display:flex;flex-direction:column;overflow:hidden}",
      ".ab-plist{overflow-y:auto;padding:var(--s-4)}",
      ".ab-pi{display:flex;align-items:center;gap:var(--s-4);padding:var(--s-3) var(--s-4);border-radius:var(--r-xs);cursor:pointer;font-family:var(--mono);font-size:var(--fs-xs);color:var(--text)}",
      ".ab-pi:hover{background:var(--surface-3)}",
      ".ab-pi .m{margin-left:auto;color:var(--text-3);font-size:var(--fs-2xs)}",
      ".ab-tag{font-size:var(--fs-3xs);padding:1px var(--s-3);border-radius:var(--r-full);border:1px solid var(--line);color:var(--text-3)}",
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
          + "- saving will resample");
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
      // Which context panel the bottom sheet is showing, and how tall it is.
      sheet: "clip", sheetOpen: true, sheetH: 330,
      master: null, masterVol: 0.8, monMuted: false,
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
    makeMaster();
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
      // The overlay loop is the one thing that keeps calling back into a torn
      // down session. Clearing the flag is what stops it: both of its scheduled
      // callbacks check it, and they check S too.
      S._tick = 0;
      try { S.ctx.close(); } catch (e) {}
      if (S.ro) try { S.ro.disconnect(); } catch (e) {}
      if (S.lro) try { S.lro.disconnect(); } catch (e) {}
      if (S.onResize) window.removeEventListener("resize", S.onResize);
    }
    // Peaks are keyed on a buffer identity that dies with the context.
    PEAKS.clear();
    const back = document.getElementById("ab-back");
    if (back) back.remove();
    document.removeEventListener("keydown", onKey, true);
    S = null; $ = {};
  }

  async function labStatus(){
    const d = await readJSON("/api/audio/lab/status", null);
    if (!S) return;
    S.status = d && !d.__error ? d : null;
    renderSheet();
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
      sheet: "clip", sheetOpen: true, sheetH: 330,
      master: null, masterVol: 0.8, monMuted: false,
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
    makeMaster();
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
      say(`could not start that sound${e && e.message ? " - " + e.message : ""}`);
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
    say(selOnly ? `${fmt((b - a) / rate)} carved out - save it as ${saveAs}`
                : `duplicated - save it as ${saveAs}`, "ok");
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
            : (can ? "no selection - drag on the waveform" : why)}</span></div>
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

  /* ── DOM ──────────────────────────────────────────────────────────────────
   * ONE SCREEN. The lab used to be three exclusive modes — clip, layers,
   * studio — with a 300px column of every control the module owns stacked
   * beside whichever one was showing. Switching modes was navigation: the
   * arrangement disappeared to edit a clip, the clip disappeared to place it.
   *
   * This is Soundtrap's shape instead, because Soundtrap solved exactly this:
   *
   *   · the ARRANGEMENT is always on screen — a fixed left column of track
   *     headers and a lane canvas sharing one row height and one scroll;
   *   · whatever is selected gets a CONTEXT SHEET at the bottom, which grows
   *     and shrinks rather than replacing the arrangement;
   *   · the TRANSPORT is pinned under both and never moves, so play is in one
   *     place whatever you are editing;
   *   · a RAIL of circular buttons down the right edge opens the panels.
   *
   * The panels are the old side column, cut along the lines the work actually
   * has: clip / effects / instrument / patterns / mix / export.
   */
  const PANELS = [
    { id: "clip",    icon: "waveform", label: "clip",    title: "Clip - the waveform and what it is made of" },
    { id: "fx",      icon: "edit",     label: "fx",      title: "Effects - gain, fades, normalise, speed, repeat" },
    { id: "synth",   icon: "audio",    label: "synth",   title: "Instrument - a sound effect out of nothing" },
    { id: "pattern", icon: "atlas",    label: "pattern", title: "Patterns - the step sequencer" },
    { id: "mix",     icon: "agents",   label: "mix",     title: "Mix - layers, mixdown and bounce" },
    { id: "out",     icon: "assets",   label: "out",     title: "Export - save, and the Godot loop points" },
  ];
  const SHEET_MIN = 132, SHEET_MAX_FRAC = 0.78;
  function ic(name, size){ return BGIcon(name, { size: size || 16 }); }

  function mount(){
    const back = document.createElement("div");
    back.className = "ab-back"; back.id = "ab-back";
    back.innerHTML = `
      <div class="ab-bar">
        <span class="ab-brand" data-icon="audio" data-icon-size="18"></span>
        <span class="ab-title">audio lab</span>
        <input class="ab-name" id="ab-name" spellcheck="false"
               title="Where this saves. Editing it here is the same as editing it in the export panel."
               oninput="AudioLab.saveAsField(this.value)"
               onchange="AudioLab.renderSheet()">
        <span class="ab-saved" id="ab-saved"><i></i><b id="ab-saved-t">saved</b></span>
        <span class="ab-spacer"></span>
        <button class="ab-ico" id="ab-undo" onclick="AudioLab.undo()" title="Undo (Ctrl+Z)">${ic("undo")}</button>
        <button class="ab-ico" id="ab-redo" onclick="AudioLab.redo()" title="Redo (Ctrl+Shift+Z)">${ic("redo")}</button>
        <span class="ab-sep"></span>
        <button class="ab-btn sm" onclick="AudioLab.pick()">open</button>
        <button class="ab-btn sm" title="Start another sound - empty, a duplicate of this clip, or the selection"
                onclick="AudioLab.newDialog()">new</button>
        <button class="ab-btn sm" title="Bring a sound in from disk as the clip"
                onclick="document.getElementById('ab-file').click()">import</button>
        <input type="file" id="ab-file" accept="audio/*" multiple style="display:none"
               onchange="AudioLab.importPicked(event,false)">
        <button class="ab-btn sm go" id="ab-save" onclick="AudioLab.save()">save</button>
        <button class="ab-btn sm ab-closebtn" onclick="AudioLab.close()">exit</button>
      </div>
      <div class="ab-why" id="ab-why" hidden></div>
      <div class="ab-body">
        <div class="ab-main">

          <div class="ab-arr" id="ab-arr">
            <div class="ab-heads" id="ab-heads">
              <div class="ab-heads-top">
                <span class="l" id="ab-heads-l">tracks</span>
                <button class="mini" title="Layer another project sound"
                        onclick="AudioLab.addTrack()">${ic("select", 12)}</button>
                <button class="mini" title="Clear every mute and solo"
                        onclick="AudioLab.clearMutes()">${ic("mute", 12)}</button>
              </div>
              <div class="ab-heads-clip" id="ab-heads-clip">
                <div class="ab-heads-in" id="ab-heads-in"></div>
              </div>
            </div>
            <div class="ab-lanes" id="ab-lanes">
              <canvas id="ab-lcanvas"></canvas>
              <canvas id="ab-lover" class="over"></canvas>
              <div class="ab-hud" id="ab-lhud"></div>
            </div>
          </div>

          <div class="ab-sheet" id="ab-sheet">
            <div class="ab-grip" id="ab-grip" title="Drag to resize · double-click to collapse"></div>
            <div class="ab-tabs" id="ab-tabs"></div>
            <div class="ab-pane" id="ab-pane"></div>
          </div>

          <div class="ab-rangebar" id="ab-rangebar" hidden>
            <span class="ab-sub" id="ab-rangeinfo"></span>
            <span class="ab-spacer"></span>
            <button class="ab-btn sm" onclick="AudioLab.rangeTrim()"
                    title="Keep only the selected span of this layer">${ic("trim", 13)} trim to range</button>
            <button class="ab-btn sm" onclick="AudioLab.rangeRemove()"
                    title="Drop the selected span; a cut in the middle splits the lane in two">${ic("delete", 13)} remove range</button>
            <button class="ab-btn sm" onclick="AudioLab.rangePlay()"
                    title="Hear just this layer over just this span">${ic("run", 13)} play range</button>
            <button class="ab-tg" onclick="AudioLab.clearRange()"
                    title="Escape does this too">clear</button>
          </div>

          <div class="ab-stage" id="ab-stage" hidden>
            <span class="lbl" id="ab-stage-label"></span>
            <span class="ab-spacer"></span>
            <button class="ab-tg" id="ab-abtg" onclick="AudioLab.toggleAB()"
                    title="Hear the original instead, without losing the edit">A/B</button>
            <button class="ab-btn sm go" onclick="AudioLab.applyStaged()" title="Apply (Enter)">apply</button>
            <button class="ab-btn sm" onclick="AudioLab.cancelStaged()" title="Cancel (Esc)">cancel</button>
          </div>

          <div class="ab-rec" id="ab-rec" hidden>
            <span class="dot"></span>
            <span class="t" id="ab-rec-t">0.000s</span>
            <div class="ab-meter" id="ab-rec-meter"><i id="ab-rec-fill"></i></div>
            <span class="note" id="ab-rec-note"></span>
            <span class="ab-spacer"></span>
            <span class="ab-sub" id="ab-rec-dest"></span>
            <button class="ab-btn sm go" onclick="AudioLab.recStop()">${ic("stop", 13)} stop &amp; keep</button>
            <button class="ab-btn sm" onclick="AudioLab.recCancel()"
                    title="Throw the take away and close the microphone">discard</button>
          </div>

          <div class="ab-tr" id="ab-tr" style="position:relative">
            <span class="grp">
              <button class="ab-ico" onclick="AudioLab.toggleMute()" id="ab-mon"
                      title="Monitor level - this is what you hear, never what gets written">${ic("mute")}</button>
              <input class="ab-vol" id="ab-vol" type="range" min="0" max="100" step="1" value="80"
                     title="Monitor level"
                     oninput="AudioLab.setMaster(this.value)">
            </span>
            <span class="ab-sep"></span>
            <span class="ab-clock" id="ab-clock">00:00.0</span>
            <span class="grp">
              <button class="ab-ico rnd rec ab-recbtn" id="ab-recbtn" onclick="AudioLab.recStart(false)"
                      title="Record from a microphone">${ic("record")}</button>
              <button class="ab-ico rnd" onclick="AudioLab.toStart()"
                      title="Back to the start">${ic("skip_start")}</button>
              <button class="ab-ico rnd big go" id="ab-play" onclick="AudioLab.togglePlay()"
                      title="Play (Space)">${ic("run", 18)}</button>
              <button class="ab-ico rnd" onclick="AudioLab.stop()" title="Stop">${ic("stop")}</button>
              <button class="ab-ico rnd" id="ab-loopsel" onclick="AudioLab.toggleLoopSel()"
                      title="Loop the selection round and round">${ic("loop")}</button>
            </span>
            <span class="ab-sep"></span>
            <span class="ab-read" id="ab-read"></span>
            <span class="ab-spacer"></span>
            <span class="ab-read" id="ab-selinfo"></span>
            <button class="ab-ico" id="ab-gear" onclick="AudioLab.togglePrefs()"
                    title="Editing preferences">${ic("settings")}</button>
            <div class="ab-pop" id="ab-prefs" hidden></div>
          </div>
        </div>
        <div class="ab-rail" id="ab-rail"></div>
      </div>`;
    /* _host is module state and OUTLIVES the pane it points at. A seat that has
       been switched away from leaves a host that is still in the document and
       still non-null but has no boxes — and mounting into it renders the whole
       mixer somewhere nobody can see, with no error and nothing in the console.
       Observed: AudioLab.open() from the asset library painted into the hidden
       #aud-editor (overlayOnBody 0, inSeat true) instead of opening the overlay.
       spriteedit.js carries the same guard for the same reason. */
    const visible = el => !!el && el.isConnected && el.getClientRects().length > 0;
    if (visible(_host)) { back.classList.add("ab-embed"); _host.innerHTML = ""; _host.appendChild(back); }
    else { _host = null; document.body.appendChild(back); }

    /* The clip waveform and the beat maker are PERSISTENT nodes that the sheet
       borrows. Rebuilding either from innerHTML on a tab change would drop the
       canvas's listeners and remount the sequencer, so they live in a detached
       stash and get moved into the pane instead. */
    $ = { back,
          name: back.querySelector("#ab-name"),
          heads: back.querySelector("#ab-heads-in"),
          headsClip: back.querySelector("#ab-heads-clip"),
          lanes: back.querySelector("#ab-lanes"),
          lcanvas: back.querySelector("#ab-lcanvas"),
          lover: back.querySelector("#ab-lover"),
          lhud: back.querySelector("#ab-lhud"),
          sheet: back.querySelector("#ab-sheet"),
          tabs: back.querySelector("#ab-tabs"),
          pane: back.querySelector("#ab-pane"),
          rail: back.querySelector("#ab-rail"),
          clock: back.querySelector("#ab-clock"),
          read: back.querySelector("#ab-read"),
          selinfo: back.querySelector("#ab-selinfo"),
          play: back.querySelector("#ab-play"),
          stash: document.createElement("div") };
    $.stash.style.display = "none";

    // The clip surface: a static waveform canvas with an overlay for the parts
    // that move. See _paint() for why that split is not decoration.
    $.wave = document.createElement("div");
    $.wave.className = "ab-wave"; $.wave.id = "ab-wave";
    $.wave.innerHTML = `<canvas id="ab-canvas"></canvas>
      <canvas id="ab-over" class="over"></canvas>
      <div class="ab-hud" id="ab-hud"></div>`;
    $.canvas = $.wave.querySelector("#ab-canvas");
    $.over = $.wave.querySelector("#ab-over");
    $.hud = $.wave.querySelector("#ab-hud");
    $.studio = document.createElement("div");
    $.studio.className = "ab-studio"; $.studio.id = "ab-studio";
    $.stash.appendChild($.wave);
    $.stash.appendChild($.studio);
    back.appendChild($.stash);

    $.ctx2d = $.canvas.getContext("2d");
    $.octx2d = $.over.getContext("2d");
    $.lctx2d = $.lcanvas.getContext("2d");
    $.loctx2d = $.lover.getContext("2d");

    if (window.BGIcon) BGIcon.upgrade(back);
    bindWave();
    bindLayers();
    bindDrop(back);
    bindKnobs(back);
    bindGrip();
    bindHeadScroll();
    syncLoopBtn();
    document.addEventListener("keydown", onKey, true);
    if (window.ResizeObserver){
      S.ro = new ResizeObserver(() => paint());
      S.ro.observe($.wave);
      S.lro = new ResizeObserver(() => { paintLayers(); paintHead(); });
      S.lro.observe($.lanes);
    }
    S.onResize = () => { paint(); paintLayers(); paintHead(); };
    window.addEventListener("resize", S.onResize);
    sizeSheet();
    sizeCanvas();
    renderRail();
    renderSheet();
    renderHeads();
    applyMaster();
    // The arrangement is on screen from the first frame now, so its layers have
    // to be fetched from the first frame too. This used to be deferred until
    // somebody switched to "layers" mode, which no longer exists.
    S.tracks.forEach(t => ensureLayerBuf(t.source));
    layersFit();
    requestAnimationFrame(() => { paint(); paintLayers(); paintHead(); });
  }

  /* ── the panel rail and the sheet ─────────────────────────────────────── */
  function renderRail(){
    if (!$.rail) return;
    $.rail.innerHTML = PANELS.map(p => `
      <button class="ab-rb${S.sheet === p.id && S.sheetOpen ? " on" : ""}"
              title="${E(p.title)}" onclick="AudioLab.setSheet('${p.id}')">
        <i>${ic(p.icon, 17)}</i><s>${E(p.label)}</s></button>`).join("");
  }
  function sheetTabs(){
    if (!$.tabs) return;
    const p = PANELS.find(x => x.id === S.sheet) || PANELS[0];
    $.tabs.innerHTML = PANELS.map(x => `
      <button class="ab-tab${x.id === S.sheet ? " on" : ""}"
              title="${E(x.title)}" onclick="AudioLab.setSheet('${x.id}',true)">
        ${ic(x.icon, 13)}${E(x.label)}</button>`).join("")
      + `<span class="ab-spacer"></span>
         <button class="ab-tg" onclick="AudioLab.toggleSheet()">${
           S.sheetOpen ? "collapse" : "open"} ${E(p.label)}</button>`;
  }
  function sizeSheet(){
    if (!$.sheet) return;
    const open = !!S.sheetOpen;
    $.sheet.classList.toggle("collapsed", !open);
    const max = Math.max(SHEET_MIN, ($.back.clientHeight || 700) * SHEET_MAX_FRAC);
    S.sheetH = clamp(S.sheetH || 320, SHEET_MIN, max);
    $.sheet.style.height = open ? S.sheetH + "px" : "";
  }
  function toggleSheet(){
    if (!S) return;
    S.sheetOpen = !S.sheetOpen;
    sizeSheet(); sheetTabs(); renderRail();
    if (S.sheetOpen) renderSheet();
    requestAnimationFrame(() => { paint(); paintLayers(); paintHead(); });
  }
  /* Clicking the panel you are already in collapses it — the same button that
     opened the sheet is the one that puts the arrangement back. */
  function setSheet(id, force){
    if (!S) return;
    if (S.sheet === id && S.sheetOpen && !force){ toggleSheet(); return; }
    S.sheet = PANELS.some(p => p.id === id) ? id : "clip";
    S.sheetOpen = true;
    S.mode = S.sheet === "pattern" ? "studio" : S.sheet === "clip" ? "clip" : S.mode;
    sizeSheet(); renderRail(); renderSheet();
    requestAnimationFrame(() => { paint(); paintLayers(); paintHead(); });
  }
  function bindGrip(){
    const g = document.getElementById("ab-grip");
    if (!g) return;
    g.addEventListener("dblclick", toggleSheet);
    g.addEventListener("pointerdown", ev => {
      if (!S || !S.sheetOpen) return;
      g.setPointerCapture(ev.pointerId);
      const y0 = ev.clientY, h0 = S.sheetH;
      const max = Math.max(SHEET_MIN, $.back.clientHeight * SHEET_MAX_FRAC);
      const move = e => {
        S.sheetH = clamp(h0 + (y0 - e.clientY), SHEET_MIN, max);
        $.sheet.style.height = S.sheetH + "px";
      };
      const up = () => {
        g.removeEventListener("pointermove", move);
        g.removeEventListener("pointerup", up);
        paint(); paintLayers(); paintHead();
      };
      g.addEventListener("pointermove", move);
      g.addEventListener("pointerup", up);
    });
  }
  /* The header column and the lane canvas are two rendering systems showing one
     list, so exactly one number decides where row i is: S.lscroll. */
  function bindHeadScroll(){
    if (!$.headsClip) return;
    $.headsClip.addEventListener("wheel", ev => {
      if (!S) return;
      ev.preventDefault();
      scrollLanes(ev.deltaY);
    }, { passive: false });
  }
  function maxLaneScroll(){
    const H = ($.lanes ? $.lanes.clientHeight : 0) - RULER_H;
    return Math.max(0, (S.tracks.length + 1) * LANE_H - H);
  }
  function scrollLanes(dy){
    S.lscroll = clamp(S.lscroll + dy, 0, maxLaneScroll());
    syncHeadScroll();
    paintLayers(); paintHead();
  }
  function syncHeadScroll(){
    if ($.heads) $.heads.style.transform = `translateY(${-Math.round(S.lscroll)}px)`;
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
    if ($.over.width !== w || $.over.height !== h){
      $.over.width = w; $.over.height = h;
    }
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

  /* ── peaks, and why they are cached ───────────────────────────────────────
   * Reducing a 45-second stereo buffer to 1200 columns is ~500k reads. That was
   * happening on EVERY repaint: every pointermove of a selection drag, and
   * every animation frame while the playhead moved. The picture it produced was
   * identical each time — the samples had not changed and neither had the view.
   *
   * Two fixes, and they are the whole latency story here:
   *   1. the moving parts (selection, playhead, range) are drawn on a SECOND
   *      canvas stacked over the waveform, so a drag or a playing transport
   *      repaints ~20 lines instead of the surface;
   *   2. what is left is memoised on (buffer, channel, window, width), so the
   *      first frame after a zoom pays for the scan and no frame after it does.
   *
   * There is no worker: an AudioBuffer's channel data cannot cross a postMessage
   * boundary without a copy that costs more than the scan, so the honest answer
   * is to do the scan far less often rather than somewhere else.
   */
  let _bufSeq = 0;
  function bufId(buf){
    if (!buf.__abid) buf.__abid = ++_bufSeq;
    return buf.__abid;
  }
  const PEAKS = new Map();
  const PEAKS_CAP = 160;
  function peaksFor(buf, channel, from, to, width){
    const key = bufId(buf) + "|" + channel + "|" + Math.round(from) + "|"
              + Math.round(to) + "|" + width;
    const hit = PEAKS.get(key);
    if (hit) return hit;
    const out = peaks(buf, channel, from, to, width);
    if (PEAKS.size >= PEAKS_CAP) PEAKS.delete(PEAKS.keys().next().value);
    PEAKS.set(key, out);
    return out;
  }

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
    try{ paintLayers(); paintHead(); }catch(e){}
    try{ renderHeads(); }catch(e){}
  }); }catch(e){}

  function paint(){
    if (!S || !$.ctx2d) return;
    if (S._pending) return;
    S._pending = true;
    const run = () => { if (!S || !S._pending) return; S._pending = false; _paint(); };
    requestAnimationFrame(run);
    setTimeout(run, 120);
  }

  /* The static half: ground, waveform, channel rules, Godot loop markers.
     Repainted when the audio, the view or the ground changes — and at no other
     time. Everything that moves is in _paintSel below. */
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

    for (let k = 0; k < ch; k++){
      const top = k * laneH, mid = top + laneH / 2;
      // The zero line and the channel divider were baked near-white — invisible
      // on the light ground, which is the whole reason BGTheme.color exists.
      c.strokeStyle = BGTheme.color("--line");
      c.beginPath(); c.moveTo(0, mid); c.lineTo(W, mid); c.stroke();
      const p = peaksFor(vb, k, from, to, width);
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

    // Godot loop markers — the setting you cannot hear, drawn where you can see it.
    if (S.loop && S.loop.enabled){
      const rate = S.buf.sampleRate;
      const b = sampleToX(S.loop.begin_s * rate, W);
      c.strokeStyle = BGTheme.color("--info"); c.lineWidth = 1.5;
      c.beginPath(); c.moveTo(b, 0); c.lineTo(b, H); c.stroke();
      c.fillStyle = BGTheme.color("--info"); c.font = "10px " + MONO;
      c.fillText("loop", b + 4, 12);
      if (S.loop.end_s != null){
        const e = sampleToX(S.loop.end_s * rate, W);
        c.beginPath(); c.moveTo(e, 0); c.lineTo(e, H); c.stroke();
        c.fillText("end", e + 4, 12);
      }
      c.lineWidth = 1;
    }

    const rate = vb.sampleRate;
    $.hud.textContent = [
      `${fmt(vb.length / rate)}  ·  ${rate}Hz  ·  ${ch === 2 ? "stereo" : "mono"}`,
      `view ${fmt(from / rate)}–${fmt(to / rate)}`,
    ].join("   ");
    refreshHistory();
    syncStage();
    _paintSel();
  }

  /* The moving half. Selection band, its two grips, and the clip playhead —
     everything a drag or a running transport changes. Nothing here reads the
     samples, so this is cheap enough to run every frame. */
  function _paintSel(){
    if (!S || !$.octx2d || !$.over) return;
    const c = $.octx2d, dpr = S.dpr || 1;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = $.over.width / dpr, H = $.over.height / dpr;
    c.clearRect(0, 0, W, H);

    if (S.sel){
      const x0 = sampleToX(S.sel.a, W), x1 = sampleToX(S.sel.b, W);
      const a = Math.min(x0, x1), b = Math.max(x0, x1);
      c.fillStyle = BGTheme.color("--accent-soft");
      c.fillRect(a, 0, b - a, H);
      // Grips. An edge you cannot see is an edge nobody drags.
      c.fillStyle = BGTheme.color("--accent");
      c.fillRect(Math.round(a) - 1, 0, 2, H);
      c.fillRect(Math.round(b) - 1, 0, 2, H);
    }

    if (S.play){
      let t = S.playFrom + (S.ctx.currentTime - S.playStart) * S.buf.sampleRate;
      // A looping source wraps at loopEnd; the playhead has to wrap with it or
      // it runs off the end while the sound is still going round.
      if (S.play.loop && S.sel){
        const w = S.sel.b - S.sel.a;
        if (w > 0 && t > S.sel.b) t = S.sel.a + ((t - S.sel.a) % w);
      }
      drawHead(c, sampleToX(t, W), H, true);
    }

    const rate = viewBuf().sampleRate;
    if ($.selinfo){
      $.selinfo.textContent = S.sel
        ? `sel ${fmt(S.sel.a / rate)} → ${fmt(S.sel.b / rate)} (${fmt((S.sel.b - S.sel.a) / rate)})`
        : "no selection - edits apply to the whole clip";
    }
  }

  /* Soundtrap draws the playhead as a line with a triangular grab handle at the
     top, which is what tells you the line is a control and not a decoration.
     Same shape on both surfaces, so it reads as one object. */
  function drawHead(c, x, H, live, top){
    const y = top || 0;
    c.strokeStyle = BGTheme.color(live ? "--text" : "--text-dim");
    c.lineWidth = 1;
    c.beginPath(); c.moveTo(Math.round(x) + .5, y); c.lineTo(Math.round(x) + .5, H); c.stroke();
    c.fillStyle = BGTheme.color(live ? "--text" : "--text-dim");
    c.beginPath();
    c.moveTo(x - 5, y); c.lineTo(x + 5, y); c.lineTo(x, y + 7);
    c.closePath(); c.fill();
  }
  const MONO = "ui-monospace,Consolas,monospace";

  /* ONE animation loop for everything that moves: both overlays and the clock.
     It starts when something starts playing and stops itself when nothing is,
     so an idle pane costs no frames. The old code recursed paint() from inside
     paint(), which meant a moving playhead re-derived the whole waveform. */
  function startTick(){
    if (!S || S._tick) return;
    S._tick = 1;
    // rAF AND a timeout, whichever lands first — the same non-latching idiom
    // paint() uses, and for the same reason: rAF does not fire while the page
    // is not compositing (a background tab, a pane that is not on screen), and
    // a loop that only chains on rAF freezes the playhead and the clock for the
    // rest of the session. Observed while driving this pane headlessly.
    const run = () => {
      if (!S || !S._tick) return;
      S._tick = 0;
      _paintSel();
      paintHead();
      syncClock();
      if (S.play || S.lplay) startTick();
    };
    requestAnimationFrame(run);
    setTimeout(run, 40);
  }
  /* The big readout. Whichever transport is live is what it counts, because
     there is only one of them on screen and it has to mean something. */
  function syncClock(){
    if (!S || !$.clock) return;
    let t = 0;
    if (S.lplay) t = S.lplay.from + (S.ctx.currentTime - S.lplay.startCtxTime);
    else if (S.play) t = (S.playFrom + (S.ctx.currentTime - S.playStart) * S.buf.sampleRate)
                         / S.buf.sampleRate;
    else t = S.lhead || 0;
    const m = Math.floor(Math.max(0, t) / 60), s = Math.max(0, t) - m * 60;
    $.clock.textContent = `${String(m).padStart(2, "0")}:${s < 10 ? "0" : ""}${s.toFixed(1)}`;
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
    // The OVERLAY takes the pointer, because it is the topmost layer. Every
    // move below repaints only it — the waveform underneath does not move while
    // a selection is being dragged, so it must not be redrawn while one is.
    const el = $.over;
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
      _paintSel();
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
      _paintSel();
    });
    // Snap on release only: snapping mid-drag makes the edge jump under the
    // pointer and you can never land where you meant to.
    const end = () => { if (S){ if (S.drag) snapSel(); S.drag = null; renderSheet(); paint(); } };
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
  /* Writes the boxes back without re-rendering the panel: renderSheet() replaces
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
    renderSheet(); paint();
  }
  function toggleSnap(){ if (S){ S.ui.snap = !S.ui.snap; renderSheet(); paint(); } }

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
    renderSheet(); paint();
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
    if (ev.key === "Escape" && S.lrange){
      ev.preventDefault(); clearRange(); return;
    }
    // Overlay only. Embedded, close() strips #ab-back without restoring the
    // landing markup embed() wrote, leaving the Studio tab blank for good.
    if (ev.key === "Escape" && !_host){ ev.preventDefault(); closeAsk(); return; }
    // Space is "play what I am looking at", and togglePlay() is now the one
    // place that decides what that means — see its comment.
    if (ev.key === " "){ ev.preventDefault(); togglePlay(); return; }
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
      renderSheet(); paint();
      return;
    }
    if (ev.key === "Delete" || ev.key === "Backspace"){ ev.preventDefault(); cut(); return; }
  }

  /* ── playback ─────────────────────────────────────────────────────────────
   * Everything live goes through ONE master gain, which is the transport's
   * volume slider. It is a monitor level and nothing else: no render path
   * (mixdown, bounce, the beat maker's offline render, encodeWav) can see it,
   * because a level control that silently scaled what lands on disk is the
   * single worst bug a mixer can have. */
  function makeMaster(){
    if (!S || S.master) return;
    try {
      S.master = S.ctx.createGain();
      S.master.gain.value = S.monMuted ? 0 : S.masterVol;
      S.master.connect(S.ctx.destination);
    } catch (e){ S.master = null; }
  }
  /* The live destination. BeatMaker schedules into this too, so its preview
     obeys the same slider as everything else in the pane. */
  function dest(){ return (S && S.master) || (S && S.ctx.destination) || null; }
  function setMaster(v){
    if (!S) return;
    S.masterVol = clamp(Number(v) / 100, 0, 1);
    if (S.monMuted && S.masterVol > 0) S.monMuted = false;
    applyMaster();
  }
  function toggleMute(){ if (S){ S.monMuted = !S.monMuted; applyMaster(); } }
  function applyMaster(){
    if (S && S.master) S.master.gain.value = S.monMuted ? 0 : S.masterVol;
    const b = document.getElementById("ab-mon");
    if (b) b.classList.toggle("on", !!(S && S.monMuted));
    const sl = document.getElementById("ab-vol");
    if (sl) sl.value = Math.round((S ? S.masterVol : 0.8) * 100);
  }

  /* One button, two transports. With the arrangement permanently on screen the
     question "what does play mean" has a real answer: the clip on its own when
     you are looking at its editor, the whole stack otherwise. */
  function togglePlay(){
    if (!S) return;
    if (S.play || S.lplay){ stop(); return; }
    if (S.sheet === "clip" && S.sheetOpen) play();
    else playStack(S.lhead || 0);
  }
  function play(){
    if (!S) return;
    stop();
    try { S.ctx.resume(); } catch (e) {}
    makeMaster();
    const vb = viewBuf();
    const src = S.ctx.createBufferSource();
    src.buffer = vb;
    src.connect(dest());
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
  /* Back to zero. Whichever transport is live decides what "the start" is —
     the clip's own head when the clip is playing, the arrangement's otherwise. */
  function toStart(){
    if (!S) return;
    const wasStack = !!S.lplay, wasClip = !!S.play;
    stop();
    S.lhead = 0;
    if (S.sel) S.view = { from: 0, to: S.view.to - S.view.from };
    paintHead(); syncClock(); _paintSel();
    if (wasStack) playStack(0); else if (wasClip) play();
  }
  function stop(){
    if (!S) return;
    stopPreview();
    stopStack();                 // every path that silences the lab silences the stack
    if (!S.play){ syncPlay(); _paintSel(); return; }
    try { S.play.onended = null; S.play.stop(); } catch (e) {}
    S.play = null; syncPlay(); _paintSel();
  }
  /* One play button for two transports. Which one it drives is decided by
     togglePlay(); what it SHOWS is simply whether anything is running, because
     there is one of them on screen and two states would be a lie. */
  function syncPlay(){
    const on = !!(S && (S.play || S.lplay));
    if ($.play){
      $.play.innerHTML = ic(on ? "pause" : "run", 18);
      $.play.title = on ? "Pause (Space)" : "Play (Space)";
      $.play.classList.toggle("go", !on);
      if (window.BGIcon) BGIcon.upgrade($.play);
    }
    if (on) startTick(); else syncClock();
  }

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
    renderSheet(); paint();
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
    renderSheet(); paint();
  }
  /* Some ops want the selection to land on what they just made (the silence you
     inserted, the block you repeated). Held on the staged entry and applied
     after the commit, because until then the clip is still the old length. */
  function selAfter(sel){
    if (!S || !sel) return;
    const len = S.buf.length;
    const a = clamp(sel.a, 0, len), b = clamp(sel.b, 0, len);
    S.sel = b - a < 1 ? null : { a, b };
    renderSheet(); paint();
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
    renderSheet(); paint();
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
    renderSheet(); paint();
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
    renderSheet(); paint();
  }
  /* Soundtrap's "Saved! ✓" beside the title, and its inline-editable title. The
     title here is the save PATH, because that is what this document's name
     actually is — and it is a real input, so renaming and saving is one gesture
     rather than a trip to a panel. The input is never rewritten while it has
     focus; doing that ate the character you had just typed. */
  function refreshHistory(){
    const u = document.getElementById("ab-undo"), r = document.getElementById("ab-redo");
    if (u) u.disabled = !S.undo.length;
    if (r) r.disabled = !S.redo.length;
    if ($.name && document.activeElement !== $.name)
      $.name.value = S.rel || S.saveAs || "";
    const chip = document.getElementById("ab-saved");
    const txt = document.getElementById("ab-saved-t");
    if (chip) chip.classList.toggle("dirty", !!S.dirty);
    if (txt) txt.textContent = S.dirty ? "unsaved" : (S.rel ? "saved" : "new");
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
    makeMaster();
    src.buffer = buf; src.connect(dest()); src.start();
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
    if (key === "wave"){ renderSheet(); return; }
    // Everything else is behind a knob, which patches its own arc and readout.
    // renderSheet() here would replace the control mid-drag and kill the drag.
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
    renderSheet();
    paintLayers();
  }
  /* The numeric fields are bound to oninput, and renderSheet() replaces the whole
     panel — so re-rendering here destroyed the very box being typed into. Only
     the toggles, which change what the panel draws, may re-render. */
  function trackField(i, key, v){
    const t = S.tracks[i];
    if (!t) return;
    // Typing in the in/out/offset boxes moves the block a range was measured
    // against, and reverse flips which end of the source it points at.
    if (key !== "gain_db" && key !== "pan") dropRange(i + 1);
    if (key === "muted" || key === "solo" || key === "reverse"){
      t[key] = !!v; renderHeads(); paintLayers(); return;
    }
    // The track name is editable in its header now, so this setter has to take
    // a string. Falling through to parseFloat made it a silent no-op — the box
    // kept what you typed and S.tracks kept the old name until the next render
    // put it back. 80 chars because normalise_session slices there.
    if (key === "name"){
      t.name = String(v || "").slice(0, 80) || t.source.split("/").pop();
      renderHeads(); paintLayers(); return;
    }
    // Gain and pan change what a lane SOUNDS like, not what it looks like, so
    // the static canvas is left alone — this is the path a knob drag is on.
    if (key === "gain_db" || key === "pan"){
      const g = parseFloat(v);
      if (isFinite(g)) t[key] = key === "pan" ? clamp(g, -1, 1) : clamp(g, -60, 12);
      return;
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
    renderSheet(); paintLayers();
  }
  function dropTrack(i){
    S.tracks.splice(i, 1);
    // Unconditional: every lane after this one just shifted down an index, so
    // a range that still matched S.lrange.i would now be on someone else.
    dropRange(null);
    S.lsel = clamp(S.lsel, 0, S.tracks.length);
    renderSheet(); paintLayers();
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
    if (!loaded.length && !includeCurrent){ say("every layer is muted - nothing to mix"); return null; }
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
    if (r0.buf.duration > 900){ say("that mix is longer than 15 minutes - shorten it first"); return; }
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
      renderSheet();
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
      say(`importing ${list[0].name} - the other ${list.length - 1} need layers mode`);
      list = list.slice(0, 1);
    }
    for (const f of list){
      let buf;
      try {
        buf = await S.ctx.decodeAudioData(await f.arrayBuffer());
      } catch (e){
        // The name AND the reason: "could not decode" alone leaves you guessing
        // between a codec this browser lacks and a file that is not audio.
        say(`could not decode ${f.name}${e && e.message ? " - " + e.message : ""}`);
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

  /* Drop anywhere on the pane. WHERE you drop still decides what it becomes,
     but it is now a place rather than a mode: onto the arrangement (the lanes
     or the track headers) files the sound into the project and layers it;
     anywhere else — the clip editor, a panel, the chrome — it opens as the
     clip. That is a better rule than the old "whatever mode you are in",
     because with one screen there is no mode to be in.
     Bound to `back`, which close() removes — so these go with it and there is
     nothing to unbind. */
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
      const onArrangement = !!(S && $.lanes && ev.target &&
        (($.lanes.contains(ev.target)) ||
         ($.headsClip && $.headsClip.contains(ev.target))));
      importFiles(ev.dataTransfer.files, onArrangement);
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
        fix: "open the dashboard on http://127.0.0.1:7788 - browsers offer the "
           + "microphone to localhost or https only, never to a plain LAN address" });
      return bad;                     // nothing below can run without it
    }
    if (typeof window.MediaRecorder === "undefined")
      bad.push({ name: "mediarecorder",
        reason: "this browser has no MediaRecorder",
        fix: "use Chrome, Edge or Firefox - or import a file instead" });
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
        fix: "close whatever has it open - a call, OBS, a DAW - and try again" };
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
      say(c ? `${c.reason} - ${c.fix}` : "cannot record");
      return;
    }
    box.innerHTML =
      '<div class="hd"><b>cannot record</b>'
      + '<button class="x" onclick="AudioLab.recDismiss()" title="Dismiss">✕</button></div>'
      + '<ul class="ab-checks">' + checks.map(c =>
          `<li><b>${E(c.name)}</b><span class="r">${E(c.reason)}</span>`
          + `<span class="fix">→ ${E(c.fix)}</span></li>`).join("") + "</ul>"
      + '<div class="foot">Everything else in the lab works without a microphone - '
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
    if (bad.length){ recWhy(bad); say("no microphone - see the panel"); return; }
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
      say("the microphone did not open - see the panel");
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
        fix: "this browser offers no capture format the lab can read - import a file instead" }]);
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
      say(`the recorder failed${err && err.message ? " - " + err.message : ""}`);
      R.cancelled = true;
      recStop();
    };
    mr.onstop = () => {
      const chunks = R.chunks.slice(), cancelled = R.cancelled, asL = R.asLayer;
      const type = (R.mr && R.mr.mimeType) || recMime() || "audio/webm";
      recTeardown();                   // device released before anything slow happens
      if (cancelled){ say("take discarded"); return; }
      if (!chunks.length){ say("nothing was captured - the input produced no audio"); return; }
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
      const msg = R.hot ? "clipping - turn the input down"
        : (R.an && !R.heard && secs > REC_SILENT_AFTER) ? "no signal - the input is silent"
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
          + (e && e.message ? " - " + e.message : ""));
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
      S.ui[key] = !!v; renderSheet(); return;   // changes what the panel draws
    }
    const n = parseFloat(v);
    if (!isFinite(n)) return;          // a half-typed "-" or "." must not be 0
    S.ui[key] = n;
    // Same trap as synthField: renderSheet() here would replace the knob
    // mid-drag and kill it. The knob patches its own arc and readout; there is
    // nothing left for this to write.
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
    renderSheet(); paint();
    say(r.data.created
        ? `created ${r.data.rel}${r.data.needs_godot_import
            ? " - open the project in Godot once so it imports" : ""}`
        : `saved · previous copy at ${r.data.backup}`, "ok");
  }

  async function writeLoop(enabled){
    if (!S || !S.rel){ say("save the file first - loop points live in its .import"); return; }
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
    renderSheet(); paint();
    say(enabled ? `loop set from ${fmt(begin)}` : "looping turned off", "ok");
    if ((r.data.ignored || []).length) say(r.data.ignored[0]);
  }

  /* ── track headers ────────────────────────────────────────────────────────
   * Soundtrap's left column, and the single most transferable thing about it:
   * every track's controls are in the SAME PLACE on every row, so the eye
   * learns one position for mute and never looks for it again. The lab used to
   * put these in a scrolling form at the far right of the window, several
   * hundred pixels from the lane they controlled.
   *
   * Lane 0 is the open clip. It carries the same controls as a layer because it
   * is a lane in the same mix — but its mute/solo live on S.clipLane rather
   * than in S.tracks, because normalise_session refuses a track with no source.
   */
  function headRow(i){
    const isClip = !i;
    const t = isClip ? null : S.tracks[i - 1];
    const st = laneState(i);
    const col = laneColor(i);
    const buf = isClip ? S.buf : S.layerBufs[t.source];
    const sp = isClip ? null : (buf ? trackSpan(t, buf) : null);
    const cut = sp && buf && (sp.in_s > 0.0005 || sp.out_s < buf.duration - 0.0005);
    const meta = isClip
      ? `${fmt(S.buf.duration)} · ${(S.buf.sampleRate / 1000).toFixed(1)}k`
      : buf === undefined ? "decoding…"
      : buf === null ? "could not decode"
      : `at ${fmt(t.offset_s || 0)}${cut ? ` · cut ${fmt(sp.len)}` : ""}`;
    const name = isClip ? ((S.rel || S.saveAs || "clip").split("/").pop())
                        : (t.name || t.source);
    return `<div class="ab-head${S.lsel === i ? " sel" : ""}" data-lane="${i}"
                 onclick="AudioLab.focusLaneUI(${i})">
      <span class="bar" style="background:${col}"></span>
      <span class="badge" style="color:${col}">${ic(isClip ? "waveform" : "waveform", 13)}</span>
      <span class="col">
        <input class="nm" value="${E(name)}" spellcheck="false"${isClip ? " readonly" : ""}
               title="${E(isClip ? (S.rel || S.saveAs || "the open clip") : t.source)}"
               onclick="event.stopPropagation()"
               onchange="AudioLab.trackField(${i - 1},'name',this.value)">
        <span class="meta">${E(meta)}</span>
      </span>
      ${knob("k" + i + "g", "vol", st.gain_db || 0, -60, 12, .5, "db",
             (isClip ? "clip:gain_db" : "track:" + (i - 1) + ":gain_db"), true)}
      <span class="btns">
        <button class="hb${st.solo ? " on" : ""} solo" title="Solo - hear only the soloed lanes"
                onclick="event.stopPropagation();AudioLab.laneToggle(${i},'solo')">${ic("solo", 13)}</button>
        <button class="hb${st.muted ? " on" : ""}" title="Mute"
                onclick="event.stopPropagation();AudioLab.laneToggle(${i},'muted')">${ic("mute", 13)}</button>
        <button class="hb" title="More - trim, split, reverse, remove"
                onclick="event.stopPropagation();AudioLab.laneMenu(${i})">${ic("seats", 13)}</button>
      </span>
    </div>`;
  }

  function renderHeads(){
    if (!S || !$.heads) return;
    // Never rebuild the column out from under a name being typed into it.
    if ($.heads.contains(document.activeElement)) return;
    const rows = [headRow(0)];
    for (let i = 1; i <= S.tracks.length; i++) rows.push(headRow(i));
    $.heads.innerHTML = rows.join("");
    if (window.BGIcon) BGIcon.upgrade($.heads);
    const l = document.getElementById("ab-heads-l");
    if (l) l.textContent = `${S.tracks.length + 1} track${S.tracks.length ? "s" : ""}`;
    syncHeadScroll();
  }

  function focusLaneUI(i){
    if (!S) return;
    focusLane(i);
    renderHeads();
    paintLayers(); paintHead();
  }
  function laneToggle(i, key){
    if (!S) return;
    if (i) trackField(i - 1, key, !S.tracks[i - 1][key]);
    else clipLaneField(key);
    renderHeads();
  }
  /* The `…` overflow. Everything here is real and every entry that cannot run
     says why rather than vanishing — a menu whose contents change shape is a
     menu you have to read every time. */
  async function laneMenu(i){
    if (!S) return;
    focusLane(i);
    renderHeads();
    if (!i){
      const pick = await askPick({
        title: "the clip lane", placeholder: "", empty: "",
        fetch: async () => ({ items: [
          { value: "clip", label: "open its editor",  meta: "the clip panel below" },
          { value: "sel",  label: "carve out the selection", meta: "a new sound from the selected span" },
          { value: "dup",  label: "duplicate the clip", meta: "same audio, new name" },
        ], total: 3, truncated: false }) });
      if (pick === "clip") setSheet("clip");
      else if (pick === "sel") newFromSel();
      else if (pick === "dup") newDup();
      return;
    }
    const t = S.tracks[i - 1];
    if (!t) return;
    const pick = await askPick({
      title: t.name || t.source, placeholder: "", empty: "",
      fetch: async () => ({ items: [
        { value: "reverse", label: t.reverse ? "play it forwards again" : "play it backwards",
          meta: "the kept region, reversed" },
        { value: "split", label: "split at the playhead",
          meta: "two lanes, one source, complementary trims" },
        { value: "reset", label: "undo the trim", meta: "back to the whole file" },
        { value: "solo",  label: "audition this lane alone", meta: "play just this one" },
        { value: "drop",  label: "remove the layer", meta: "the file on disk is untouched" },
      ], total: 5, truncated: false }) });
    if (!S) return;
    if (pick === "reverse") trackField(i - 1, "reverse", !t.reverse);
    else if (pick === "split"){ focusLane(i); splitLayer(); }
    else if (pick === "reset") resetTrim(i - 1);
    else if (pick === "solo"){
      const bl = laneBlock(i);
      if (bl) playStack(bl.t0, { lane: i, until: bl.t1 });
      else say("that layer has not decoded yet");
    }
    else if (pick === "drop") dropTrack(i - 1);
    renderHeads();
  }
  function clearMutes(){
    if (!S) return;
    S.clipLane.muted = S.clipLane.solo = false;
    S.tracks.forEach(t => { t.muted = false; t.solo = false; });
    renderHeads(); paintLayers();
    say("every mute and solo cleared", "ok");
  }

  /* ── arc knobs ────────────────────────────────────────────────────────────
   * A knob for a continuous value, a pill for a toggle. That is Soundtrap's
   * rule and it is a good one: a row of sliders all look like the same control,
   * whereas a knob reads as one setting you turn and takes a quarter of the
   * width, which is what makes a per-track volume fit in a track header at all.
   *
   * `set` is a routing string — "track:2:gain_db", "ui:norm_db", "synth:freq",
   * "clip:pan" — because the drag is bound once by delegation rather than as an
   * inline handler per knob. On drag the arc and the readout are PATCHED, never
   * re-rendered: re-rendering the element you are dragging kills the drag,
   * which is the same trap synthField() and uiField() already document.
   */
  const KNOB_R = 13, KNOB_SWEEP = 270;
  function knobArc(frac){
    const a0 = -KNOB_SWEEP / 2, a1 = a0 + KNOB_SWEEP * clamp(frac, 0, 1);
    const pt = a => {
      const r = (a - 90) * Math.PI / 180;
      return [(20 + KNOB_R * Math.cos(r)).toFixed(2), (20 + KNOB_R * Math.sin(r)).toFixed(2)];
    };
    const p0 = pt(a0), p1 = pt(a1);
    return `M${p0[0]} ${p0[1]} A${KNOB_R} ${KNOB_R} 0 ${
      KNOB_SWEEP * frac > 180 ? 1 : 0} 1 ${p1[0]} ${p1[1]}`;
  }
  function knobPtr(frac){
    const a = (-KNOB_SWEEP / 2 + KNOB_SWEEP * clamp(frac, 0, 1) - 90) * Math.PI / 180;
    return `M${(20 + KNOB_R * 0.45 * Math.cos(a)).toFixed(2)} ${(20 + KNOB_R * 0.45 * Math.sin(a)).toFixed(2)}`
         + `L${(20 + KNOB_R * 0.95 * Math.cos(a)).toFixed(2)} ${(20 + KNOB_R * 0.95 * Math.sin(a)).toFixed(2)}`;
  }
  function knobText(v, unit){
    if (unit === "db") return (v > 0 ? "+" : "") + Number(v).toFixed(1);
    if (unit === "pan") return Math.abs(v) < .02 ? "C"
      : (v < 0 ? "L" : "R") + Math.round(Math.abs(v) * 100);
    if (unit === "x") return "×" + Number(v).toFixed(2);
    if (unit === "ms") return Math.round(v * 1000) + "m";
    if (unit === "hz") return Math.round(v) + "";
    return Number(v).toFixed(Math.abs(v) < 10 ? 2 : 0);
  }
  function knob(id, label, value, min, max, step, unit, set, small){
    const f = (Number(value) - min) / Math.max(1e-9, max - min);
    return `<span class="ab-knob${small ? " sm" : ""}" id="${id}" data-set="${E(set)}"
        data-min="${min}" data-max="${max}" data-step="${step}" data-unit="${E(unit)}"
        data-v="${value}" title="${E(label)} - drag up and down, double-click to reset"
        onclick="event.stopPropagation()">
      <svg width="${small ? 30 : 40}" height="${small ? 30 : 40}" viewBox="0 0 40 40">
        <path class="trk" stroke-width="3" stroke-linecap="round" d="${knobArc(1)}"/>
        <path class="arc" stroke-width="3" stroke-linecap="round" d="${knobArc(f)}"/>
        <path class="ptr" stroke-width="2" stroke-linecap="round" d="${knobPtr(f)}"/>
      </svg>
      ${small ? "" : `<span class="kl">${E(label)}</span>`}
      <span class="kv">${E(knobText(value, unit))}</span></span>`;
  }
  function knobSet(el, v){
    const min = +el.dataset.min, max = +el.dataset.max;
    const step = +el.dataset.step || 0.01;
    v = clamp(Math.round(v / step) * step, min, max);
    el.dataset.v = v;
    const f = (v - min) / Math.max(1e-9, max - min);
    const arc = el.querySelector(".arc"), ptr = el.querySelector(".ptr");
    const kv = el.querySelector(".kv");
    if (arc) arc.setAttribute("d", knobArc(f));
    if (ptr) ptr.setAttribute("d", knobPtr(f));
    if (kv) kv.textContent = knobText(v, el.dataset.unit);
    knobApply(el.dataset.set, v);
  }
  /* One place that knows what a knob is wired to. Everything it can reach is a
     setter that already existed — the knob is a new way to hold a value, not a
     new value. */
  function knobApply(set, v){
    const parts = String(set || "").split(":");
    if (parts[0] === "track") trackField(+parts[1], parts[2], v);
    else if (parts[0] === "clip"){ S.clipLane[parts[1]] = v; paintLayers(); }
    else if (parts[0] === "ui") uiField(parts[1], v);
    else if (parts[0] === "synth") synthField(parts[1], v);
    else if (parts[0] === "master") setMaster(v * 100);
  }
  function bindKnobs(root){
    root.addEventListener("pointerdown", ev => {
      const el = ev.target.closest && ev.target.closest(".ab-knob");
      if (!el || !S) return;
      ev.preventDefault(); ev.stopPropagation();
      el.setPointerCapture(ev.pointerId);
      const min = +el.dataset.min, max = +el.dataset.max;
      const y0 = ev.clientY, v0 = +el.dataset.v;
      // 180 px of travel covers the full range; Shift makes it 720 for a value
      // like gain where the last dB matters.
      const move = e => knobSet(el,
        v0 + ((y0 - e.clientY) / (e.shiftKey ? 720 : 180)) * (max - min));
      const up = () => {
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
        el.removeEventListener("pointercancel", up);
        renderHeads();
      };
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
      el.addEventListener("pointercancel", up);
    });
    root.addEventListener("dblclick", ev => {
      const el = ev.target.closest && ev.target.closest(".ab-knob");
      if (!el || !S) return;
      // Zero is the neutral value for every knob here except length and pitch,
      // and for those the minimum is the sane rest position.
      const min = +el.dataset.min, max = +el.dataset.max;
      knobSet(el, min <= 0 && max >= 0 ? 0 : min);
      renderHeads();
    });
  }

  /* ── the context sheet ────────────────────────────────────────────────────
   * Six panels, cut along the lines the work actually has. What used to be one
   * 300px column containing selection + edit + extend + loop + synth + mixer +
   * save, in that order, with no way to see the arrangement at the same time.
   */
  function renderSheet(){
    if (!S || !$.pane) return;
    const u = S.ui || (S.ui = { speed: 1, silence: 0.25, reps: 3, xf: 120 });
    if (u.units == null) u.units = "s";
    if (u.snap == null) u.snap = true;
    if (u.gain_db == null) u.gain_db = -3;
    if (u.fade_curve == null) u.fade_curve = "linear";
    if (u.norm_db == null) u.norm_db = -1;
    if (u.semitones == null) u.semitones = 0;
    if (u.preview == null) u.preview = true;

    sheetTabs();
    renderHeads();
    refreshHistory();
    syncTransportRead();
    if (!S.sheetOpen){ paintLayers(); return; }

    // The clip canvas and the sequencer are borrowed, not rebuilt. Park them
    // before innerHTML wipes the pane or the canvas loses its listeners and
    // BeatMaker loses the DOM it mounted into.
    if ($.wave.parentNode !== $.stash) $.stash.appendChild($.wave);
    if ($.studio.parentNode !== $.stash) $.stash.appendChild($.studio);

    const id = S.sheet;
    $.pane.classList.toggle("flush", id === "clip" || id === "pattern");
    $.pane.innerHTML =
        id === "clip"    ? paneClip()
      : id === "fx"      ? paneFx(u)
      : id === "synth"   ? paneSynth()
      : id === "pattern" ? `<div id="ab-studio-slot" style="flex:1;min-width:0;display:flex"></div>`
      : id === "mix"     ? paneMix()
      :                    paneOut();

    if (id === "clip"){
      const slot = $.pane.querySelector("#ab-clipslot");
      if (slot) slot.appendChild($.wave);
      sizeCanvas(); paint();
    } else if (id === "pattern"){
      const slot = $.pane.querySelector("#ab-studio-slot");
      if (slot) slot.appendChild($.studio);
      mountStudio();
    }
    if (window.BGIcon) BGIcon.upgrade($.pane);
    paintLayers();
  }

  /* CLIP — the waveform, what is selected in it, and the structural cuts. The
     ops here are not judged by ear (a trim either kept the right span or it did
     not), so they commit rather than staging. */
  function paneClip(){
    const rate = S.buf.sampleRate, sel = S.sel, u = S.ui;
    const step = unitStep();
    return `<div class="ab-clipwrap">
      <div class="ab-clipbar">
        <label style="min-width:0">in</label>
        <input class="ab-in num" id="ab-sel-a" type="number" step="${step}" min="0"
               value="${toUnits(sel ? sel.a : 0)}" onchange="AudioLab.selField('a',this.value)">
        <label style="min-width:0">out</label>
        <input class="ab-in num" id="ab-sel-b" type="number" step="${step}" min="0"
               value="${toUnits(sel ? sel.b : S.buf.length)}" onchange="AudioLab.selField('b',this.value)">
        <label style="min-width:0">len</label>
        <input class="ab-in num" id="ab-sel-len" type="number" step="${step}" min="0"
               value="${toUnits(sel ? sel.b - sel.a : 0)}" onchange="AudioLab.selField('len',this.value)">
        <select class="ab-in" style="flex:none;width:78px" onchange="AudioLab.selUnits(this.value)">
          ${["s","ms","samples"].map(x =>
            `<option value="${x}"${u.units === x ? " selected" : ""}>${x}</option>`).join("")}
        </select>
        <span class="ab-sep"></span>
        <button class="ab-btn sm" onclick="AudioLab.selectAll()">all</button>
        <button class="ab-btn sm" onclick="AudioLab.clearSel()">none</button>
        <button class="ab-ico" title="Zoom to the selection" onclick="AudioLab.zoomSel()">${ic("zoom_in", 14)}</button>
        <button class="ab-ico" title="Fit the whole clip" onclick="AudioLab.zoomFit()">${ic("zoom_out", 14)}</button>
        <span class="ab-spacer"></span>
        <span class="ab-read">${sel ? fmt((sel.b - sel.a) / rate) + " selected"
                                     : "whole clip"}</span>
      </div>
      <div id="ab-clipslot" style="flex:1;min-height:0;position:relative;display:flex"></div>
    </div>
    <div class="ab-rack">
      <div class="ab-h">cut<span></span></div>
      <div class="ab-grid2">
        <button class="ab-btn" onclick="AudioLab.trim()">${ic("trim", 13)} trim to sel</button>
        <button class="ab-btn" onclick="AudioLab.cut()">${ic("delete", 13)} delete</button>
        <button class="ab-btn" onclick="AudioLab.silence()">silence</button>
        <button class="ab-btn" onclick="AudioLab.reverse()">reverse</button>
      </div>
      <button class="ab-btn wide" onclick="AudioLab.toMono()">mix down to mono</button>
      <div class="ab-note">Drag an edge to move it, ←/→ nudges by 1 ms (Shift 10 ms,
        Alt the in point, Ctrl the whole selection). <b>Snap to zero</b> — in the
        transport's gear — lands the boundary on a zero crossing, which is what
        stops a trim from clicking.</div>

      <div class="ab-h">insert<span></span></div>
      <div class="ab-row">
        <label>silence</label>
        <input class="ab-in num" id="ab-sil" type="number" step="0.05" min="0" max="60"
               value="${S.ui.silence}" oninput="AudioLab.uiField('silence',this.value)">
        <button class="ab-btn" onclick="AudioLab.insertSilence(+document.getElementById('ab-sil').value)">insert</button>
      </div>
      <div class="ab-note">Lands at the selection's in point, or at the end when
        nothing is selected.</div>

      <div class="ab-h">this clip<span></span></div>
      <div class="ab-note">
        <b>${fmt(S.buf.duration)}</b> · ${rate} Hz ·
        ${S.buf.numberOfChannels === 2 ? "stereo" : "mono"}<br>
        ${E(S.rel || (S.saveAs + " - not written yet"))}</div>
      <div class="ab-grid2">
        <button class="ab-btn" onclick="AudioLab.newDup()">duplicate</button>
        <button class="ab-btn" onclick="AudioLab.newFromSel()">carve out sel</button>
      </div>
    </div>`;
  }

  /* FX — everything with an amount on it. Every op here goes through deliver(),
     so with "audition first" on it renders into the bar above the transport and
     waits for an apply. */
  function paneFx(u){
    return `<div class="ab-cols">
      <div class="ab-col">
        <div class="ab-h">level<span></span></div>
        <div class="ab-knobs">
          ${knob("kf-gain", "gain", u.gain_db, -36, 24, .5, "db", "ui:gain_db")}
          ${knob("kf-norm", "target", u.norm_db, -60, 0, .5, "db", "ui:norm_db")}
        </div>
        <div class="ab-grid2">
          <button class="ab-btn" onclick="AudioLab.gain(AudioLab.state.ui.gain_db)">apply gain</button>
          <button class="ab-btn" onclick="AudioLab.normalize(AudioLab.state.ui.norm_db)">normalise</button>
        </div>
        <div class="ab-row">
          <button class="ab-tg${u.norm_per_channel ? " on" : ""}"
                  onclick="AudioLab.uiField('norm_per_channel',${!u.norm_per_channel})">per channel</button>
          <span class="ab-read">${u.norm_per_channel ? "each side reaches the target"
                                                     : "one factor keeps the image"}</span>
        </div>
        <div class="ab-note">Leave headroom: a stinger normalised to <b>0 dBFS</b>
          clips the moment anything else plays under it.</div>

        <div class="ab-h">fade<span></span></div>
        <div class="ab-row">
          <label>curve</label>
          <select class="ab-in" onchange="AudioLab.uiField('fade_curve',this.value)">
            ${FADES.map(c => `<option value="${c}"${u.fade_curve === c ? " selected" : ""}>${c}</option>`).join("")}
          </select>
        </div>
        <div class="ab-grid2">
          <button class="ab-btn" onclick="AudioLab.fade('in')">fade in</button>
          <button class="ab-btn" onclick="AudioLab.fade('out')">fade out</button>
        </div>
        <div class="ab-note">A <b>linear</b> fade-out on a tail sounds like it stops
          early — logarithmic or equal-power is what a tail actually wants.</div>
      </div>

      <div class="ab-col">
        <div class="ab-h">pitch &amp; time<span></span></div>
        <div class="ab-knobs">
          ${knob("kf-speed", "speed", u.speed, 0.25, 4, .01, "x", "ui:speed")}
          ${knob("kf-semi", "semitones", u.semitones, -24, 24, 1, "n", "ui:semitones")}
        </div>
        <div class="ab-grid2">
          <button class="ab-btn" onclick="AudioLab.speed(AudioLab.state.ui.speed)">resample</button>
          <button class="ab-btn" onclick="AudioLab.speed(Math.pow(2,AudioLab.state.ui.semitones/12))">by semitones</button>
        </div>
        <div class="ab-note">Speed moves pitch with it, like pitching tape — which
          is how you make a heavy version of a light hit. Semitones is the same
          resample said in musical units, so the clip gets shorter as it goes up.</div>

        <div class="ab-h">extend<span></span></div>
        <div class="ab-knobs">
          ${knob("kf-reps", "repeat", u.reps, 2, 64, 1, "n", "ui:reps")}
          ${knob("kf-xf", "crossfade", u.xf, 0, 2000, 10, "n", "ui:xf")}
        </div>
        <button class="ab-btn wide"
                onclick="AudioLab.repeat(AudioLab.state.ui.reps,AudioLab.state.ui.xf)">
          repeat with crossfade</button>
        <div class="ab-note">An equal-power crossfade is what stops a butt-joined
          loop from clicking at the seam. Crossfade is in milliseconds.</div>

        <div class="ab-row">
          <button class="ab-tg${u.preview ? " on" : ""}"
                  onclick="AudioLab.uiField('preview',${!u.preview})">audition first</button>
          <span class="ab-read">${u.preview ? "apply / cancel each one"
                                            : "straight to the clip"}</span>
        </div>
      </div>
    </div>`;
  }

  /* INSTRUMENT — Soundtrap's knob row, and the closest thing this project has
     to one: the synth that makes an SFX out of nothing, no file and no API. */
  function paneSynth(){
    const p = S.synth;
    const i = WAVES.indexOf(p.wave);
    return `<div class="ab-cols">
      <div class="ab-col" style="max-width:520px">
        <div class="ab-h">instrument<span></span></div>
        <div class="ab-row">
          <button class="ab-ico" title="Previous waveform"
                  onclick="AudioLab.stepWave(-1)">${ic("undo", 13)}</button>
          <span class="ab-in" style="text-align:center;font-family:var(--mono)">${E(p.wave)}</span>
          <button class="ab-ico" title="Next waveform"
                  onclick="AudioLab.stepWave(1)">${ic("redo", 13)}</button>
          <span class="ab-read">${i + 1} / ${WAVES.length}</span>
        </div>
        <div class="ab-knobs">
          ${knob("ks-freq", "pitch", p.freq, 20, 8000, 1, "hz", "synth:freq")}
          ${knob("ks-sweep", "sweep", p.sweep, -100, 100, 1, "n", "synth:sweep")}
          ${knob("ks-seconds", "length", p.seconds, 0.02, 6, .01, "ms", "synth:seconds")}
          ${knob("ks-noise", "noise", p.noise, 0, 1, .01, "n", "synth:noise")}
          ${knob("ks-crush", "crush", p.crush, 0, 7, 1, "n", "synth:crush")}
        </div>
        <div class="ab-h">envelope<span></span></div>
        <div class="ab-knobs">
          ${knob("ks-attack", "attack", p.attack, 0, 1, .005, "ms", "synth:attack")}
          ${knob("ks-decay", "decay", p.decay, 0, 2, .005, "ms", "synth:decay")}
          ${knob("ks-sustain", "sustain", p.sustain, 0, 1, .01, "n", "synth:sustain")}
          ${knob("ks-release", "release", p.release, 0, 2, .005, "ms", "synth:release")}
          ${knob("ks-gain", "gain", p.gain, 0, 1, .01, "n", "synth:gain")}
        </div>
        <div class="ab-grid2">
          <button class="ab-btn" onclick="AudioLab.synthPreview()">${ic("run", 13)} audition</button>
          <button class="ab-btn" onclick="AudioLab.synthAppend()">append to clip</button>
        </div>
        <button class="ab-btn wide go" onclick="AudioLab.synthReplace()">generate — replace the clip</button>
        <div class="ab-note">Deterministic: the same settings are the same sound
          every time, so "that one was good, do it again" works. Replacing the
          clip is undoable like any other edit.</div>
      </div>
    </div>`;
  }

  /* MIX — the arrangement's own panel. The lanes above are the picture; this is
     what you can do to the whole stack. */
  function paneMix(){
    const solo = S.tracks.some(t => t.solo) || S.clipLane.solo;
    return `<div class="ab-cols">
      <div class="ab-col">
        <div class="ab-h">layers<span></span></div>
        <div class="ab-note">${S.tracks.length
          ? `<b>${S.tracks.length}</b> layer${S.tracks.length === 1 ? "" : "s"} under the clip${
              solo ? " · <b>solo is active</b>, so only soloed lanes are heard" : ""}.
             Drag a lane to move it, drag either end to trim, ${
             S.ui.ltool === "select" ? "drag across it to select a span (Alt-drag moves)"
                                     : "Alt-drag across it to select a span"}.`
          : `Layer other project sounds under this one — a hit plus a noise tail,
             a stinger over a pad. Nothing on disk is touched: a layer is a path,
             an offset and a trim.`}</div>
        <div class="ab-row">
          <button class="ab-tg${S.ui.ltool === "move" ? " on" : ""}"
                  onclick="AudioLab.setLayerTool('move')"
                  title="Drag a lane to slide it in time - Alt for the other tool">move</button>
          <button class="ab-tg${S.ui.ltool === "select" ? " on" : ""}"
                  onclick="AudioLab.setLayerTool('select')"
                  title="Drag across a lane to select a span - Alt for the other tool">select span</button>
          <button class="ab-tg" onclick="AudioLab.layersFit()">fit the view</button>
        </div>
        <div class="ab-grid2">
          <button class="ab-btn" onclick="AudioLab.addTrack()">layer a sound</button>
          <button class="ab-btn" title="Save a file from disk into the project and layer it"
                  onclick="document.getElementById('ab-lfile').click()">import a file</button>
          <button class="ab-btn ab-recbtn" onclick="AudioLab.recStart(true)">${ic("record", 13)} record a take</button>
          <button class="ab-btn" onclick="AudioLab.splitLayer()">split at playhead</button>
        </div>
        <input type="file" id="ab-lfile" accept="audio/*" multiple style="display:none"
               onchange="AudioLab.importPicked(event,true)">
        <div class="ab-h">clip lane<span></span></div>
        <div class="ab-knobs">
          ${knob("km-cg", "clip vol", S.clipLane.gain_db || 0, -60, 12, .5, "db", "clip:gain_db")}
          ${knob("km-cp", "clip pan", S.clipLane.pan || 0, -1, 1, .02, "pan", "clip:pan")}
        </div>
      </div>

      <div class="ab-col">
        <div class="ab-h">render<span></span></div>
        <div class="ab-grid2">
          <button class="ab-btn go" onclick="AudioLab.mixdown(true)">mix into this clip</button>
          <button class="ab-btn" onclick="AudioLab.mixdown(false)">layers only</button>
        </div>
        <div class="ab-row">
          <input class="ab-in" id="ab-bounce" value="${E(S.bounceAs || defaultBounce())}"
                 oninput="AudioLab.bounceAsField(this.value)">
        </div>
        <button class="ab-btn wide" id="ab-bounce-go" onclick="AudioLab.bounce()">bounce to that file</button>
        <div class="ab-note"><b>Mix</b> replaces this clip${(S.ui && S.ui.preview !== false)
          ? " - with an apply/cancel step, since <b>audition first</b> is on" : ""}.
          <b>Bounce</b> writes the stack to the path above and leaves this clip
          exactly as it is.</div>
        <button class="ab-btn wide" onclick="AudioLab.saveSession()">save the mix session</button>
        <div class="ab-note">The session — every layer's path, offset, trim, gain
          and pan — lands in a sidecar next to the clip, so a mixdown stays a
          document you can re-open rather than a one-shot.</div>
      </div>
    </div>`;
  }

  /* EXPORT — how this leaves the lab. Where it saves, in what format, and the
     Godot loop points, which are the one setting no audio editor can hear. */
  function paneOut(){
    const st = S.status, loop = S.loop || {}, sel = S.sel;
    return `<div class="ab-cols">
      <div class="ab-col">
        <div class="ab-h">save<span></span></div>
        <div class="ab-row">
          <input class="ab-in" id="ab-saveas" value="${E(S.saveAs || S.rel || "")}"
                 oninput="AudioLab.saveAsField(this.value)" onchange="AudioLab.renderSheet()">
        </div>
        <div class="ab-grid2">
          <button class="ab-btn go" onclick="AudioLab.save(false)">save</button>
          <button class="ab-btn" onclick="AudioLab.save(true)">save as</button>
        </div>
        <div class="ab-note">${st
          ? (st.ogg ? "Both <b>.wav</b> and <b>.ogg</b> can be written here."
                    : `<span class="ab-warn">${E(st.ogg_reason)}</span>`)
          : "checking what this install can write…"}</div>
        <div class="ab-note">A save keeps the previous bytes: the old copy lands
          in <b>.bgate_out/audio_backups</b> and the toast names it.</div>
      </div>

      <div class="ab-col">
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
                `<option value="${m}"${loop.mode === m ? " selected" : ""}>${m}</option>`).join("")}
            </select></div>` : `<div class="ab-note">An .ogg loops from its offset
              to the end of the stream — Godot's ogg importer has no loop end.</div>`}
          <div class="ab-grid2">
            <button class="ab-btn go" onclick="AudioLab.writeLoop(true)">${ic("loop", 13)} ${
              sel ? "loop from selection" : "enable looping"}</button>
            <button class="ab-btn" onclick="AudioLab.writeLoop(false)">turn off</button>
          </div>
          ${!loop.has_import ? `<div class="ab-note ab-warn">No .import yet — open
            the project in Godot once so the engine writes one.</div>` : ""}`
          : `<div class="ab-note">${E(S.rel || "this clip")} has no Godot loop
              settings (${E(loop.importer || "unknown importer")}).</div>`}
      </div>
    </div>`;
  }

  /* The transport's gear. Soundtrap hangs the metronome's options off one here;
     what this lab has behind the same button is the three preferences that
     change how an edit lands rather than what it sounds like. */
  function togglePrefs(){
    const p = document.getElementById("ab-prefs");
    if (!p || !S) return;
    if (!p.hidden){ p.hidden = true; return; }
    const u = S.ui;
    p.innerHTML = `
      <div class="ab-h">editing<span></span></div>
      <div class="ab-row">
        <button class="ab-tg${u.preview ? " on" : ""}"
                onclick="AudioLab.uiField('preview',${!u.preview});AudioLab.togglePrefs();AudioLab.togglePrefs()">audition first</button>
      </div>
      <div class="ab-row">
        <button class="ab-tg${u.snap ? " on" : ""}"
                onclick="AudioLab.toggleSnap();AudioLab.togglePrefs();AudioLab.togglePrefs()">snap to zero</button>
      </div>
      <div class="ab-row">
        <label>units</label>
        <select class="ab-in" onchange="AudioLab.selUnits(this.value)">
          ${["s","ms","samples"].map(x =>
            `<option value="${x}"${u.units === x ? " selected" : ""}>${x}</option>`).join("")}
        </select>
      </div>
      <div class="ab-note">Audition renders an amount-op into the bar above the
        transport instead of into the clip. Snapping lands a boundary on a zero
        crossing, which is what stops a trim clicking.</div>`;
    p.hidden = false;
  }

  function syncTransportRead(){
    if (!S || !$.read) return;
    const b = S.buf;
    $.read.textContent = `${b.sampleRate} Hz · ${b.numberOfChannels === 2 ? "stereo" : "mono"}`
      + `  ·  ${fmt(b.duration)}`;
  }

  function stepWave(d){
    if (!S) return;
    const i = (WAVES.indexOf(S.synth.wave) + d + WAVES.length) % WAVES.length;
    synthField("wave", WAVES[i]);
  }

  function mountStudio(){
    if (!window.BeatMaker){
      $.studio.innerHTML =
        `<div class="ab-note" style="padding:var(--s-8)">the beat maker did not load</div>`;
      return;
    }
    if (!S.studioMounted){
      BeatMaker.mount($.studio, (S.meta && S.meta.beat) || null);
      S.studioMounted = true;
    }
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

  const MIN_LEN = 0.02;                  // the shortest a trimmed lane may get, s

  /* Row height is a constant now — see RULER_H / LANE_H at the top of the
     module. It has to be: the headers are DOM and the lanes are a canvas, and a
     height that depended on how tall the pane happened to be is a height the
     two would compute differently the moment one of them was laid out and the
     other was not. laneHeight() is gone; read LANE_H. */
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
    // The header column prints the lane's length and trim, and until this
    // moment it could only print "decoding…" — so it has to be told too, or
    // every layer stays labelled as still decoding once it has finished.
    renderHeads();
    // The fit that ran before this decode landed could not know how long the
    // layer was, so a still-fitted view re-fits rather than clipping it off.
    if (S.lfit) layersFit(); else { paintLayers(); paintHead(); }
    return S.layerBufs[rel];
  }

  /* Both lane canvases are the same size. The static one carries the ruler, the
     lane grounds and the coloured clips; the overlay carries the playhead, the
     range and the drag feedback. */
  function sizeLayerCanvas(){
    if (!S || !$.lcanvas) return false;
    const r = $.lanes.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(r.width * dpr));
    const h = Math.max(1, Math.round(r.height * dpr));
    S.ldpr = dpr;
    if ($.lover.width !== w || $.lover.height !== h){
      $.lover.width = w; $.lover.height = h;
    }
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
    const i = Math.floor((y - RULER_H + S.lscroll) / LANE_H);
    return (i < 0 || i > S.tracks.length) ? null : i;
  }
  function laneTop(i){ return RULER_H + i * LANE_H - S.lscroll; }
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

  /* Which end of lane i's block the pointer is on — "in", "out" or null — using
     the same EDGE_PX grab radius the clip canvas's selection edges use, because
     it is the same gesture: grab an end, drag it, the audio behind it is kept
     or hidden. The interior of the block is still the move, so this is hit
     tested before the drag branch.
     Lane 0 is the clip: it is what t=0 MEANS here, so it never trims. */
  function laneEdgeAt(i, x, y){
    if (!S || !i) return null;
    const tr = S.tracks[i - 1];
    if (!tr) return null;
    const buf = S.layerBufs[tr.source];
    if (!buf) return null;               // undecoded: no length to grab an end of
    const top = laneTop(i);
    if (y < top || y > top + LANE_H) return null;
    const W = $.lcanvas.width / (S.ldpr || 1);
    const off = tr.offset_s || 0;
    const da = Math.abs(secToX(off, W) - x);
    const db = Math.abs(secToX(off + trackSpan(tr, buf).len, W) - x);
    if (da <= EDGE_PX && da <= db) return "in";
    if (db <= EDGE_PX) return "out";
    return null;
  }

  // 60 px is about the narrowest a m:ss label reads at. The list runs past the
  // 1–60 s band at both ends so a hard zoom never leaves the ruler blank.
  const TICK_STEPS = [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600];

  /* The STATIC arrangement: ruler, lane grounds, the coloured clip blocks and
   * their waveforms, trim caps, and the Godot loop bracket. Nothing here moves
   * while the transport runs, so none of it is repainted while it does.
   *
   * The labels and the mute/solo chips that used to be drawn INTO this canvas
   * are gone: they are DOM now, in the fixed header column, which is what makes
   * them reachable at any zoom, selectable, and renameable in place. A canvas
   * label was also unreadable the moment a clip started under it. */
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
    const n = S.tracks.length + 1;
    S.lscroll = clamp(S.lscroll, 0, Math.max(0, n * LANE_H - (H - RULER_H)));
    syncHeadScroll();

    // ── ruler
    let step = TICK_STEPS[TICK_STEPS.length - 1];
    for (const s of TICK_STEPS){ if (s * pps >= 60){ step = s; break; } }
    c.fillStyle = BGTheme.color("--surface-2");
    c.fillRect(0, 0, W, RULER_H);
    c.strokeStyle = BGTheme.color("--line-strong");
    c.fillStyle = BGTheme.color("--text-3");
    c.font = "10px " + MONO;
    c.beginPath();
    for (let t = Math.ceil(from / step) * step; t <= to; t += step){
      const x = Math.round(secToX(t, W)) + .5;
      c.moveTo(x, RULER_H - 6); c.lineTo(x, RULER_H);
    }
    c.stroke();
    for (let t = Math.ceil(from / step) * step; t <= to; t += step){
      const m = Math.floor(t / 60), s = t - m * 60;
      const lbl = `${m}:${s < 10 ? "0" : ""}${step < 1 ? s.toFixed(2) : Math.round(s)}`;
      c.fillText(lbl, Math.round(secToX(t, W)) + 3, 12);
    }
    // Faint gridlines down the lanes, so a clip's position reads against the
    // ruler rather than against nothing.
    c.strokeStyle = BGTheme.color("--line-soft");
    c.beginPath();
    for (let t = Math.ceil(from / step) * step; t <= to; t += step){
      const x = Math.round(secToX(t, W)) + .5;
      c.moveTo(x, RULER_H); c.lineTo(x, H);
    }
    c.stroke();

    // ── the Godot loop bracket, in the ruler where Soundtrap puts its loop
    // region. This is the one loop this project actually persists: it lives in
    // the clip's .import and is the reason a music track plays once and stops.
    if (S.loop && S.loop.enabled){
      const a = secToX(S.loop.begin_s || 0, W);
      const b = secToX(S.loop.end_s != null ? S.loop.end_s : S.buf.duration, W);
      c.fillStyle = BGTheme.color("--info-soft");
      c.fillRect(a, 0, Math.max(2, b - a), RULER_H);
      c.strokeStyle = BGTheme.color("--info"); c.lineWidth = 1.5;
      c.beginPath();
      c.moveTo(a + .5, RULER_H - 1); c.lineTo(a + .5, 3); c.lineTo(b - .5, 3);
      c.lineTo(b - .5, RULER_H - 1);
      c.stroke(); c.lineWidth = 1;
      c.fillStyle = BGTheme.color("--info");
      c.font = "9px " + MONO;
      c.fillText("loop", a + 4, 12);
    }

    // ── lanes. 0 is the clip and defines t=0; 1..n are S.tracks.
    c.save();
    c.beginPath(); c.rect(0, RULER_H, W, H - RULER_H); c.clip();
    for (let i = 0; i < n; i++){
      const top = laneTop(i);
      if (top > H || top + LANE_H < RULER_H) continue;
      const tr = i ? S.tracks[i - 1] : null;
      const buf = i ? S.layerBufs[tr.source] : S.buf;
      const off = i ? (tr.offset_s || 0) : 0;
      const focused = S.lsel === i;

      c.fillStyle = BGTheme.color(focused ? "--surface-2" : "--surface-1");
      c.fillRect(0, top, W, LANE_H);

      if (i && buf === undefined){
        c.fillStyle = BGTheme.color("--text-3");
        c.font = "10px " + MONO;
        c.fillText("decoding…", 10, top + LANE_H / 2 + 3);
      } else if (i && buf === null){
        c.fillStyle = BGTheme.color("--bad");
        c.font = "10px " + MONO;
        c.fillText("could not decode this source", 10, top + LANE_H / 2 + 3);
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
          const st = laneState(i);
          const col = laneColor(i);
          const y = top + 4, h = LANE_H - 9;

          // Soundtrap's clip: a saturated rounded block in the track's own
          // colour, waveform drawn inside it. Muted drops the fill to a ghost —
          // but KEEPS the outline at full strength, because a fill alone at 28%
          // vanished into the parchment ground and the lane read as empty
          // rather than as silenced.
          c.save();
          c.globalAlpha = st.muted ? 0.22 : 1;
          roundRect(c, x0, y, Math.max(2, x1 - x0), h, 4);
          c.fillStyle = col; c.fill();
          if (st.muted){
            c.globalAlpha = 1;
            c.strokeStyle = col; c.lineWidth = 1.5;
            roundRect(c, x0 + .75, y + .75, Math.max(2, x1 - x0) - 1.5, h - 1.5, 4);
            c.stroke(); c.lineWidth = 1;
            c.globalAlpha = 0.22;
          }

          const p = peaksFor(buf, 0, (sp.in_s + (t0 - off)) * buf.sampleRate,
                             (sp.in_s + (t1 - off)) * buf.sampleRate, px);
          c.save();
          roundRect(c, x0, y, Math.max(2, x1 - x0), h, 4); c.clip();
          c.strokeStyle = CLIP_INK;
          c.beginPath();
          const mid = y + h / 2, amp = h / 2 - 4;
          for (let x = 0; x < px; x++){
            const lo = p[x * 2], hi = p[x * 2 + 1];
            const cx = Math.floor(x0) + x + .5;
            const ya = mid - hi * amp, yb = mid - lo * amp;
            c.moveTo(cx, ya); c.lineTo(cx, Math.max(yb, ya + .6));
          }
          c.stroke();
          c.restore();

          // A cut end gets a hard rule, or a trimmed layer is indistinguishable
          // from a short file and nothing tells you audio is held back there.
          c.fillStyle = BGTheme.color("--text");
          if (sp.in_s > 0.0005) c.fillRect(secToX(off, W), y, 2, h);
          if (sp.out_s < buf.duration - 0.0005)
            c.fillRect(secToX(off + sp.len, W) - 2, y, 2, h);
          c.restore();

          // Focus ring last, in the theme accent — "the lane you are editing"
          // is chrome, not identity, so it follows the ground.
          if (focused){
            c.strokeStyle = BGTheme.color("--accent"); c.lineWidth = 2;
            roundRect(c, x0 + 1, y + 1, Math.max(2, x1 - x0) - 2, h - 2, 4);
            c.stroke(); c.lineWidth = 1;
          }
          if (st.solo){
            c.strokeStyle = BGTheme.color("--good"); c.lineWidth = 2;
            roundRect(c, x0 + 1, y + 1, Math.max(2, x1 - x0) - 2, h - 2, 4);
            c.stroke(); c.lineWidth = 1;
          }
        }
      }

      c.strokeStyle = BGTheme.color("--line-soft");
      c.beginPath();
      c.moveTo(0, top + LANE_H - .5); c.lineTo(W, top + LANE_H - .5); c.stroke();
    }
    c.restore();

    c.strokeStyle = BGTheme.color("--line");
    c.beginPath(); c.moveTo(0, RULER_H + .5); c.lineTo(W, RULER_H + .5); c.stroke();

    if ($.lhud) $.lhud.textContent =
      `${n} lane${n === 1 ? "" : "s"}  ·  view ${fmt(from)}–${fmt(to)}`;
    syncStack();
    paintHead();
  }

  function roundRect(c, x, y, w, h, r){
    const rr = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
    c.beginPath();
    if (c.roundRect){ c.roundRect(x, y, w, h, rr); return; }
    c.moveTo(x + rr, y);
    c.arcTo(x + w, y, x + w, y + h, rr);
    c.arcTo(x + w, y + h, x, y + h, rr);
    c.arcTo(x, y + h, x, y, rr);
    c.arcTo(x, y, x + w, y, rr);
    c.closePath();
  }

  /* The MOVING arrangement: the playhead, the range, and the hint line. Called
     every frame while the stack plays and on every drag — and it never touches
     a sample, which is the entire point of the split. */
  function paintHead(){
    if (!S || !$.loctx2d || !S.lview) return;
    const c = $.loctx2d, dpr = S.ldpr || 1;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = $.lover.width / dpr, H = $.lover.height / dpr;
    c.clearRect(0, 0, W, H);

    // The live range, on the lane holding it.
    if (S.lrange){
      const top = laneTop(S.lrange.i);
      if (top + LANE_H > RULER_H && top < H){
        const rx0 = secToX(S.lrange.a, W), rx1 = secToX(S.lrange.b, W);
        c.save();
        c.beginPath(); c.rect(0, RULER_H, W, H - RULER_H); c.clip();
        c.fillStyle = BGTheme.color("--accent-soft");
        c.fillRect(rx0, top + 2, Math.max(1, rx1 - rx0), LANE_H - 5);
        c.fillStyle = BGTheme.color("--accent");
        c.fillRect(rx0, top + 2, Math.max(1, rx1 - rx0), 3);
        c.fillRect(Math.round(rx0), top + 2, 1, LANE_H - 5);
        c.fillRect(Math.round(rx1) - 1, top + 2, 1, LANE_H - 5);
        c.restore();
      }
    }

    // Live while the stack plays, parked and dimmer when it does not — the
    // parked one is where the next play starts from, so it has to show.
    const head = S.lplay
      ? S.lplay.from + (S.ctx.currentTime - S.lplay.startCtxTime)
      : (S.lhead || 0);
    const hx = secToX(head, W);
    if (hx >= -6 && hx <= W + 6) drawHead(c, hx, H, !!S.lplay);
    syncRangeBar();
  }

  /* The one line of prose the arrangement needs, and it earns its place: trim
     is invisible until you are within 6 px of an end, and which meaning a plain
     drag carries depends on a toggle. */
  function syncRangeBar(){
    const bar = document.getElementById("ab-rangebar");
    const info = document.getElementById("ab-rangeinfo");
    const live = !!(S && S.lrange && !S.lrdrag);
    if (bar) bar.hidden = !live;
    if (info && live){
      const rg = S.lrange;
      info.textContent = `range ${fmt(rg.b - rg.a)} on ${laneLabel(rg.i)}`
        + `  ·  ${fmt(rg.a)}–${fmt(rg.b)} - a fade, a silence or a gain over a`
        + ` range is a clip edit, not something a layer can hold`;
    }
  }

  function bindLayers(){
    // The overlay is on top, so it is what the pointer reaches. Everything the
    // drags below change lives on the overlay too, so a lane being moved or
    // trimmed repaints ~30 lines rather than every waveform on screen; the
    // static canvas is asked for a repaint only when the audio underneath
    // actually changed shape.
    const el = $.lover;
    if (!el) return;
    el.addEventListener("pointerdown", ev => {
      if (!S || !S.lview) return;
      const r = el.getBoundingClientRect();
      const px = ev.clientX - r.left, py = ev.clientY - r.top;
      // The ruler band is the scrub strip: park the playhead where you clicked,
      // and re-arm from there if the stack is already running.
      if (py < RULER_H){
        S.lhead = clamp(xToSec(px), 0, layersTotal());
        if (S.lplay) playStack(S.lhead); else { paintHead(); syncClock(); }
        return;
      }
      el.setPointerCapture(ev.pointerId);
      const i = laneAt(py);
      if (i == null) return;
      focusLane(i);                 // moving the focus is what drops a range
      renderHeads();                // …and the header column follows the focus
      // The mute and solo chips that used to be hit-tested here are DOM in the
      // header column now, so the precedence is simply ruler → edge → drag: the
      // interior of a block still moves the lane exactly as it always did.
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
                : "the clip lane holds no range - select on the waveform in clip mode");
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
        paintHead();                 // a range is overlay-only; the audio has not moved
        return;
      }
      if (!S.ldrag){
        const y = ev.clientY - r.top;
        const i = laneAt(y);
        // Same test the pointerdown makes, so the cursor promises the gesture
        // you are actually about to get.
        const selecting = (S.ui.ltool === "select") !== ev.altKey;
        el.style.cursor = (i != null && laneEdgeAt(i, x, y)) ? "col-resize"
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
        paintHead();
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
        renderHeads();             // the header's "trimmed to" must agree with the canvas
        renderSheet();
        paintLayers();
        return;
      }
      if (!S.ldrag) return;
      const t = S.tracks[S.ldrag.i - 1];
      if (t) t.offset_s = Math.round((t.offset_s || 0) * 1000) / 1000;
      S.ldrag = null;
      el.style.cursor = "grab";
      renderHeads();               // the header's "at 0:03" must agree with the canvas
      paintLayers();
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    el.addEventListener("wheel", ev => {
      if (!S || !S.lview) return;
      ev.preventDefault();
      const r = el.getBoundingClientRect();
      if (ev.shiftKey){
        scrollLanes(ev.deltaY);      // shared with the header column
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
    paintLayers(); paintHead();
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
    if (!i){ say("focus a layer lane first - the clip lane cannot be split"); return; }
    const t = S.tracks[i - 1];
    if (!t) return;
    // Checked at the gesture rather than at save: normalise_session rejects a
    // session past 32 tracks outright, and failing there loses the whole save.
    if (S.tracks.length >= 32){ say("32 layers is the cap - nothing left to split into"); return; }
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
      say("the playhead is not inside that lane - click the ruler to move it");
      return;
    }
    if (d < MIN_LEN || sp.len - d < MIN_LEN){
      say(`too close to an end - each half must be at least ${Math.round(MIN_LEN * 1000)} ms`);
      return;
    }
    // The right half starts at the playhead, and that becomes an offset_s that
    // normalise_session validates against the same 900 s the drag clamps to.
    if (head > 900){ say("past the 900s cap - nothing can start there"); return; }

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
    renderSheet();
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
    renderSheet();
  }
  function clearRange(){ if (S && S.lrange){ S.lrange = null; paintHead(); } }

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
    if (a > 900){ say("past the 900s cap - nothing can start there"); return; }
    const s = rangeToSource(bl.tr, bl.sp, a, b);
    writeSpan(bl.tr, bl.buf, s.in_s, s.out_s, a);
    S.lrange = null;                     // the range IS the lane now
    renderSheet(); paintLayers();
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
      say("that is the whole layer - drop it from the mixer instead"); return;
    }
    if (head < MIN_LEN){                        // touches the start: a head trim
      if (b > 900){ say("past the 900s cap - nothing can start there"); return; }
      const s = rangeToSource(tr, sp, b, bl.t1);
      writeSpan(tr, buf, s.in_s, s.out_s, b);
    } else if (tail < MIN_LEN){                 // touches the end: a tail trim
      const s = rangeToSource(tr, sp, bl.t0, a);
      writeSpan(tr, buf, s.in_s, s.out_s, bl.t0);
    } else {
      // Checked at the gesture rather than at save: normalise_session rejects a
      // session past 32 tracks outright and failing there loses the whole save.
      if (S.tracks.length >= 32){
        say("32 layers is the cap - nothing left to split into"); return; }
      if (b > 900){ say("past the 900s cap - nothing can start there"); return; }
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
    renderSheet(); paintLayers();
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
    makeMaster();
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
      tail.connect(dest());
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
    // gain and pan are heard, not seen; only mute and solo change the picture.
    if (key === "muted" || key === "solo"){ renderHeads(); paintLayers(); }
  }

  function syncStack(){
    syncPlay();
    syncRangeBar();
  }

  /* ── modes, as they now are ───────────────────────────────────────────────
   * There are no modes any more — the arrangement is always up and the sheet
   * shows whichever panel you asked for. setMode() survives because it is a
   * PUBLIC entry point and because mixdown() and adopt() call it to bring the
   * audition bar into view, so it maps the three old names onto panels rather
   * than disappearing and breaking every caller.
   *
   *   "clip"   → the clip panel (where apply/cancel lives)
   *   "layers" → collapse the sheet; the arrangement is the whole pane
   *   "studio" → the patterns panel
   */
  function setMode(mode){
    if (!S) return;
    if (mode === "studio" || mode === "layers") cancelStaged();
    S.mode = (mode === "studio" || mode === "layers") ? mode : "clip";
    if (mode === "studio"){
      stop();
      setSheet("pattern", true);
    } else if (mode === "layers"){
      stop();
      if (window.BeatMaker) BeatMaker.stop();
      S.tracks.forEach(t => ensureLayerBuf(t.source));
      if (S.sheetOpen){ S.sheetOpen = false; sizeSheet(); renderRail(); sheetTabs(); }
      if (!S.lview) layersFit(); else { paintLayers(); paintHead(); }
    } else {
      if (window.BeatMaker) BeatMaker.stop();
      setSheet("clip", true);
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

  function loopMode(m){ S.loop.mode = m; renderSheet(); }
  function selectAll(){ S.sel = { a: 0, b: S.buf.length }; renderSheet(); paint(); }
  function clearSel(){ S.sel = null; renderSheet(); paint(); }
  function zoomSel(){ if (S.sel){ S.view = { from: S.sel.a, to: S.sel.b }; paint(); } }
  function zoomFit(){ S.view = { from: 0, to: S.buf.length }; paint(); }

  // Seat entry point: render into `host` instead of over the whole page.
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
          '<h3>Audio lab</h3>' +
          '<p>One screen: an arrangement of lanes, a context panel under it for ' +
            'whatever is selected, and a transport that never moves. Choose a ' +
            'sound to work on, start an empty one, or bring one in from disk - ' +
            'you can drop a file straight onto this pane. Everything you open ' +
            'here saves back to the project.</p>' +
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

  /* The rail's Audio lab page. Deliberately NOT unembed()'s counterpart: this
     one reparents a live session instead of tearing it down. unembed() exists
     because Studio destroys its host's markup on a tab change and would have
     orphaned a running ctx; a rail page keeps its host, so leaving and coming
     back must leave the arrangement, the undo stack and a playing transport
     alone. Rebuilding here would also remount the beat maker and drop the
     waveform canvas's listeners, which the stash exists to prevent. */
  function activate(){
    const host = document.getElementById("ab-page");
    if (!host) return false;
    _host = host;
    injectStyle();
    if (!S) { embed(host); return true; }
    if ($ && $.back && $.back.parentNode !== host){
      $.back.classList.add("ab-embed");
      host.innerHTML = "";
      host.appendChild($.back);
    }
    return true;
  }

  return {
    activate,
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
    // The redesign's own surface. Everything above kept its name and signature
    // — seats/audio.js mounts through embed()/unembed() and the asset library
    // calls open(rel), and both still mean exactly what they meant.
    renderSheet, setSheet, toggleSheet,
    focusLaneUI, laneToggle, laneMenu, clearMutes,
    setMaster, toggleMute, toStart, togglePrefs, stepWave,
    dest,
    get state(){ return S; },
  };
})();
