/* nowplaying.js — one place that knows what is making noise right now.
 *
 * THE PROBLEM THIS EXISTS FOR. Sound is the only asset class in this dashboard
 * with more than one player, and until now none of them knew about the others:
 *
 *   · the audio seat's library table renders an <audio controls> PER ROW, and
 *     so do its cue rows and its music candidates;
 *   · the asset library, the peek sheet and the scene builder each render more;
 *   · the audio lab plays through WebAudio, and by design keeps playing when you
 *     navigate off its page (see AudioLab.activate) — so it can be sounding from
 *     a screen that is not on screen;
 *   · the beat maker schedules its own preview.
 *
 * Browsers do not make <audio> exclusive, so two rows both play, and a track
 * left running in the lab plays under whatever you start next. What made this
 * more than an annoyance is that nothing on the page NAMED the second sound:
 * you could hear two songs and have no way to find out what the other one was,
 * let alone stop it, short of hunting every table on every page for a control
 * that happened to be mid-play.
 *
 * WHY THIS IS A READOUT AND NOT A MUTE. The obvious fix is exclusivity — start
 * one, stop the rest. That was deliberately not chosen: hearing a cue over a
 * music bed is a real thing to want, and the lab's mixer exists precisely to
 * play lanes together. Taking that away to fix a labelling problem trades a
 * capability for a symptom. So concurrency stays, and what gets fixed is the
 * part that was actually broken — that it was INVISIBLE. Two sounds at once is
 * now a state the page shows you, names, and hands you a stop for.
 *
 * HOW IT KNOWS. Two mechanisms, because there are two kinds of player:
 *
 *   NATIVE <audio>/<video> are caught document-wide with capture-phase play /
 *   pause / ended listeners. Media events do not bubble, but they do capture,
 *   which is what makes one pair of listeners cover every element on every page
 *   including ones rendered long after this file ran. Nothing at the render
 *   sites had to change, and a table that grows a new player next month is
 *   covered the day it ships.
 *
 *   WEBAUDIO has no element and fires no events, so those players say so:
 *   claim(id, {label, stop}) when they start, release(id) when they stop. Two
 *   callers today, the audio lab and the beat maker.
 *
 * Registered as window.NowPlaying.
 */
window.NowPlaying = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (name, size) => (typeof BGIcon === "function"
    ? BGIcon(name, { size: size || 14 }) : "");

  // Native elements are keyed by the element itself; claimed sources by string
  // id. Two maps rather than one, because an element can be removed from the
  // document while this file is not looking and a WeakMap-ish set of DOM nodes
  // must not keep it alive on its own.
  const els = new Set();          // <audio>/<video> currently not paused
  const claims = new Map();       // id → {label, kind, stop}

  let host = null, styled = false;

  /* The name of a sound, from whatever the element has. Almost every player in
     this app points at /api/audio/file?rel=<project path>, so the rel is the
     honest label; anything else falls back to the last path segment. A blob:
     URL (a recording, an unsaved take) has no name in it at all. */
  function labelFor(el) {
    const raw = el.currentSrc || el.getAttribute("src") || "";
    if (!raw) return "a sound";
    if (/^blob:/.test(raw)) return "an unsaved take";
    let path = raw;
    try {
      const u = new URL(raw, location.href);
      path = u.searchParams.get("rel") || u.pathname;
    } catch (e) { /* not parseable — fall through to the raw string */ }
    const seg = String(path).split(/[\\/]/).filter(Boolean).pop() || path;
    try { return decodeURIComponent(seg); } catch (e) { return seg; }
  }

  /* Where the sound is coming from, so the row is findable. An element inside a
     known panel says which one; the id a claim carries says it itself. */
  function whereFor(el) {
    if (el.closest("#ab-back, .ab-back")) return "audio lab";
    if (el.closest("#aud-lib-body")) return "sound library";
    if (el.closest("#aud-cue-body")) return "cues";
    if (el.closest("#aud-music")) return "music";
    if (el.closest("#al-root, .al-sheet")) return "assets";
    if (el.closest(".pk-wrap")) return "preview";
    if (el.closest("#sb-root")) return "scene builder";
    return "";
  }

  /* One row per thing that is sounding. Elements first — they are the ones with
     a visible control somewhere — then claims. */
  function sounding() {
    const out = [];
    els.forEach(el => {
      if (el.paused || el.ended) return;       // a stale entry the events missed
      out.push({ key: el, label: labelFor(el), where: whereFor(el),
                 stop: () => { try { el.pause(); } catch (e) {} } });
    });
    claims.forEach((c, id) => {
      out.push({ key: id, label: c.label || id, where: c.kind || "",
                 stop: () => { try { c.stop && c.stop(); } catch (e) {} } });
    });
    return out;
  }

  function style() {
    if (styled) return;
    styled = true;
    const css = [
      // Above the audio lab's own overlay (z 1400) — the lab is exactly where a
      // second sound is most confusing — but under --z-ask (9000) so a confirm
      // still lands on top of it.
      ".np-wrap{position:fixed;left:var(--s-6,16px);bottom:var(--s-6,16px);z-index:1500;",
      "  display:none;flex-direction:column;gap:6px;max-width:min(420px,86vw);",
      "  padding:8px 10px;border-radius:var(--r-md,8px);font-size:var(--fs-sm,12px);",
      "  background:var(--surface-2);border:1px solid var(--line-strong);",
      "  box-shadow:var(--shadow-2);color:var(--text)}",
      ".np-wrap.on{display:flex}",
      // Two or more at once is the state that sent someone looking for this, so
      // it is the state that gets the colour.
      ".np-wrap.many{border-color:var(--warn-line);background:var(--warn-soft)}",
      ".np-hd{display:flex;align-items:center;gap:8px;color:var(--text-2);",
      "  font-family:var(--mono,monospace);font-size:11px;letter-spacing:.04em;",
      "  text-transform:uppercase}",
      ".np-wrap.many .np-hd{color:var(--warn)}",
      ".np-hd .np-sp{flex:1}",
      ".np-row{display:flex;align-items:center;gap:8px;min-width:0}",
      ".np-nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".np-wh{color:var(--text-3);font-size:11px;flex:none}",
      ".np-btn{flex:none;display:inline-flex;align-items:center;gap:5px;",
      "  padding:2px 8px;border-radius:var(--r-sm,5px);cursor:pointer;",
      "  background:var(--surface-3);border:1px solid var(--line-strong);",
      "  color:var(--text-2);font-size:11px;font-family:inherit;line-height:1.6}",
      ".np-btn:hover{background:var(--surface-4);color:var(--text);border-color:var(--accent-line)}",
      ".np-btn.all{color:var(--text)}",
      "@media (prefers-reduced-motion:no-preference){",
      "  .np-wrap.many{animation:np-in .18s ease-out}",
      "  @keyframes np-in{from{transform:translateY(4px);opacity:0}to{transform:none;opacity:1}}}",
    ].join("");
    const tag = document.createElement("style");
    tag.id = "np-style";
    tag.textContent = css;
    document.head.appendChild(tag);
  }

  function ensureHost() {
    if (host && host.isConnected) return host;
    style();
    host = document.createElement("div");
    host.className = "np-wrap";
    host.id = "np-wrap";
    document.body.appendChild(host);
    host.addEventListener("click", ev => {
      const b = ev.target.closest("[data-np]");
      if (!b) return;
      const which = b.getAttribute("data-np");
      if (which === "all") return stopAll();
      const list = sounding();
      const one = list[Number(which)];
      if (one) { one.stop(); render(); }
    });
    return host;
  }

  let pending = false;
  /* Coalesced: pause on one element and play on another arrive as two separate
     events in the same tick, and rendering twice makes the readout flicker
     through a state ("nothing playing") that was never true. */
  function render() {
    if (pending) return;
    pending = true;
    setTimeout(() => { pending = false; paint(); }, 0);
  }

  function paint() {
    const list = sounding();
    // Drop elements that stopped without telling us — a src swap, a detach.
    els.forEach(el => { if (el.paused || el.ended) els.delete(el); });
    if (!list.length) {
      if (host) { host.className = "np-wrap"; host.innerHTML = ""; }
      return;
    }
    const h = ensureHost();
    const many = list.length > 1;
    h.className = "np-wrap on" + (many ? " many" : "");
    const rows = list.map((s, i) => `<div class="np-row">
      <span class="np-nm" title="${E(s.label)}">${E(s.label)}</span>
      ${s.where ? `<span class="np-wh">${E(s.where)}</span>` : ""}
      <button class="np-btn" data-np="${i}" title="stop ${E(s.label)}">stop</button>
    </div>`).join("");
    h.innerHTML = `<div class="np-hd">
        ${icon("audio", 13)}
        <span>${many ? `${list.length} sounds playing at once` : "now playing"}</span>
        <span class="np-sp"></span>
        ${many ? '<button class="np-btn all" data-np="all">stop all</button>' : ""}
      </div>${rows}`;
    if (window.BGIcon && BGIcon.upgrade) try { BGIcon.upgrade(h); } catch (e) {}
  }

  function stopAll() {
    sounding().forEach(s => s.stop());
    render();
  }

  /* Capture phase: media events do not bubble, so a listener on document only
     ever sees them on the way DOWN. This is the whole reason no render site had
     to be touched. */
  function onPlay(ev) {
    const el = ev.target;
    if (!el || !("paused" in el)) return;
    els.add(el);
    render();
  }
  function onStop(ev) {
    const el = ev.target;
    if (!el) return;
    els.delete(el);
    render();
  }
  document.addEventListener("play", onPlay, true);
  document.addEventListener("playing", onPlay, true);
  document.addEventListener("pause", onStop, true);
  document.addEventListener("ended", onStop, true);
  document.addEventListener("emptied", onStop, true);

  /* A WebAudio player announcing itself. Idempotent: the audio lab calls this
     from syncPlay(), which runs on every transport repaint, so claiming an
     already-claimed id has to be free and must not restart the readout. */
  function claim(id, o) {
    if (!id) return;
    const was = claims.get(id);
    const next = { label: (o && o.label) || id, kind: (o && o.kind) || "",
                   stop: o && o.stop };
    if (was && was.label === next.label && was.kind === next.kind
        && was.stop === next.stop) return;
    claims.set(id, next);
    render();
  }
  function release(id) {
    if (claims.delete(id)) render();
  }

  return { claim, release, stopAll, render,
           // What is sounding, as data — the label and origin only, so a caller
           // cannot reach in and stop something it does not own.
           get list(){ return sounding().map(s => ({ label: s.label, where: s.where })); },
           get count(){ return sounding().length; } };
})();
