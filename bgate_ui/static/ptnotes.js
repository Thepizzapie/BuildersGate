/* ptnotes.js — the playtest notepad, and the frame grab that goes with it.
 *
 * A playtest has only ever captured what you SAY. That is the right default —
 * talking costs no hands while you are playing — but it is the only channel
 * there is, and it fails in ways that are not rare: a shared room, a call, a
 * dead mic, and above all anything with a number or a proper noun in it.
 * Whisper does not hear "armour 4 should be 40". It hears "armor for should be
 * forty", and files it as a note nobody can act on. Those observations used to
 * leave the session entirely.
 *
 * What lands here is NOT a parallel note store. Every note is POSTed to
 * /api/playtest/<id>/notes and written as a transcript segment plus a feedback
 * item — the same pair a spoken remark produces — so it appears in the
 * transcript at the right second and flows through triage, promote, merge,
 * dismiss and the bug report with nothing downstream adapted.
 *
 * ── TWO THINGS WERE MEASURED BEFORE THIS WAS DESIGNED ──────────────────────
 *
 * 1. A PARENT-DOCUMENT HOTKEY CANNOT WORK WHILE YOU ARE PLAYING. With the game
 *    iframe focused, a real F2 keypress fired the IFRAME's listener and not the
 *    parent's — 1 hit inside, 0 outside. So the hotkey is installed INTO the
 *    build's document (same origin: /play/ is served by the dashboard), in the
 *    capture phase, where it is also swallowed before Godot's own input handler
 *    can see it. A project that binds F2 to something therefore does not fire
 *    that action every time you reach for the notepad. The parent keeps its own
 *    listener for when focus is in the dashboard rather than the game; between
 *    them the key works from either side, and neither one can double-fire,
 *    because the event only ever exists in one of the two documents.
 *
 * 2. THE CANVAS READS BACK REAL PIXELS. Godot 4's web export creates its WebGL2
 *    context with preserveDrawingBuffer:true (verified on the live build), so
 *    canvas.toDataURL() from the parent returns the frame rather than the blank
 *    image an unpreserved drawing buffer hands back outside a rAF callback.
 *    That is why the frame is grabbed LIVE instead of extracted from the
 *    recording: it needs no recording to be finalised, no ffmpeg seek, and no
 *    waiting — and it is the only route that works at all before the mp4's moov
 *    atom is written at stop. Notes taken against a NATIVE Godot window have no
 *    canvas in this page and get their frame backfilled server-side from the
 *    video when the session stops (playtest._backfill_note_frames).
 *
 * ── WHY THE PAD ANCHORS ITSELF ON OPEN ─────────────────────────────────────
 * You see something at 0:42, hit the hotkey, and type for fifteen seconds. If
 * the note were stamped when you pressed save it would sit fifteen seconds
 * downstream of the thing it describes — past the frame, past the telemetry,
 * next to whatever happened after. So opening the pad freezes BOTH the
 * timestamp and the frame at that instant, and the note carries them however
 * long the typing takes.
 *
 * Registered as window.PtNotes. Injects its own <style>; touches no shared CSS.
 */
window.PtNotes = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const icon = (n, size) => {
    try { return BGIcon(n, { size: size || 14 }); } catch (e) { return ""; }
  };

  const HOTKEY = "F2";
  const POLL_MS = 4000;
  const KINDS = ["like", "fix", "add", "change", "question", "note"];
  const SEATS = ["director", "narrative", "gameplay", "tech", "art", "audio",
                 "qa", "unassigned"];
  /* Downscale only what is genuinely huge. A 4K canvas as lossless PNG is
     ~8 MB, which is a slow POST and a slow write for no extra evidence; at or
     under this width the frame is sent untouched, so a pixel-art bug is still
     inspectable pixel for pixel. */
  const MAX_FRAME_W = 1920;

  let host = null, pad = null, tab = null;
  let isOpen = false;
  let session = null;        // the live recording, or null
  let anchorTs = 0;          // epoch seconds this note began — 0 until armed
  let anchorClock = "";      // the same instant as mm:ss on the session clock
  let frameData = "";        // data URL of the grabbed frame
  let frameWhy = "";         // why there is no frame, in words
  let notes = [];
  let timer = null;
  /* Which session the compose form was last BUILT for; -1 means "showing the
     nothing-is-recording panel". The poll must not rebuild the form under
     someone's hands (see renderLive), so it only does so when this changes. */
  let builtFor = null;

  /* ── styles ───────────────────────────────────────────────────────────── */
  function injectStyle() {
    if (document.getElementById("ptnotes-style")) return;
    const s = document.createElement("style");
    s.id = "ptnotes-style";
    s.textContent = [
      /* The handle. Always reachable while the Playtests view is up, because
         the hotkey is undiscoverable on its own and a feature nobody finds is
         not shipped. */
      ".ptn-tab{position:fixed;right:0;top:50%;transform:translateY(-50%);z-index:calc(var(--z-drawer) - 1);",
      "  display:flex;flex-direction:column;align-items:center;gap:var(--s-3);",
      "  padding:var(--s-6) var(--s-3);border:1px solid var(--line);border-right:0;",
      "  border-radius:var(--r-md) 0 0 var(--r-md);background:var(--solid-2);color:var(--text-2);",
      "  cursor:pointer;font:inherit;box-shadow:var(--shadow-3)}",
      ".ptn-tab:hover{color:var(--text);border-color:var(--accent-line);background:var(--solid-3)}",
      ".ptn-tab .lb{writing-mode:vertical-rl;font-family:var(--mono);font-size:var(--fs-3xs,9.5px);",
      "  letter-spacing:var(--track-label);text-transform:uppercase}",
      ".ptn-tab .kb{font-family:var(--mono);font-size:9px;color:var(--text-3);border:1px solid var(--line);",
      "  border-radius:var(--r-xs);padding:1px 3px}",
      ".ptn-tab.rec{border-color:var(--bad-line);color:var(--text)}",
      ".ptn-tab[hidden]{display:none}",

      /* OPAQUE, on purpose. This panel sits over a running game; text on a
         translucent surface with a moving picture behind it is unreadable, and
         the whole point is to write things down accurately.
         --solid-N, NOT --surface-N. That was the bug: the intent above was
         right and the token was wrong. In the orbit theme --surface-2 is
         rgba(255,255,255,.045), so this panel and every control in it rendered
         see-through and the page behind bled into the notes. --solid-N exists
         for exactly this and aliases back to --surface-N in dark and light, so
         nothing changes on the other two grounds. */
      ".ptn-pad{position:fixed;top:0;right:0;bottom:0;width:min(400px,92vw);z-index:var(--z-drawer);",
      "  display:flex;flex-direction:column;background:var(--solid-2);border-left:1px solid var(--line);",
      "  box-shadow:var(--shadow-3);transform:translateX(101%);transition:transform var(--dur) var(--ease);",
      "  color:var(--text)}",
      ".ptn-pad.on{transform:none}",
      "@media (prefers-reduced-motion:reduce){.ptn-pad{transition:none}}",
      ".ptn-head{display:flex;align-items:center;gap:var(--s-4);padding:var(--s-5) var(--s-6);",
      "  border-bottom:1px solid var(--line);background:var(--solid-1)}",
      ".ptn-head h4{margin:0;font-size:var(--fs-md,13px);font-weight:var(--fw-semi);letter-spacing:var(--track-label);",
      "  text-transform:uppercase;font-family:var(--mono)}",
      /* A bordered hit target, not a bare glyph. It was invisible: a --text-3
         cross on a transparent header, over whatever the page had behind it. */
      ".ptn-x{margin-left:auto;background:var(--solid-3);border:1px solid var(--line);",
      "  color:var(--text-2);cursor:pointer;font-size:13px;line-height:1;",
      "  width:26px;height:26px;display:grid;place-items:center;flex:none;",
      "  padding:0;border-radius:var(--r-xs)}",
      ".ptn-x:hover{color:var(--text);border-color:var(--accent-line)}",
      ".ptn-body{flex:1;overflow-y:auto;padding:var(--s-6);display:flex;flex-direction:column;gap:var(--s-5)}",
      ".ptn-chip{font-family:var(--mono);font-size:var(--fs-2xs,10px);color:var(--text-3);border:1px solid var(--line);",
      "  border-radius:var(--r-full);padding:2px var(--s-4);white-space:nowrap}",
      ".ptn-chip.at{color:var(--accent);border-color:var(--accent-line)}",
      ".ptn-chip.bad{color:var(--bad);border-color:var(--bad-line)}",
      ".ptn-when{display:flex;align-items:center;gap:var(--s-3);flex-wrap:wrap}",
      ".ptn-shot{border:1px solid var(--line);border-radius:var(--r-sm);overflow:hidden;background:var(--solid-1);",
      "  position:relative}",
      ".ptn-shot img{display:block;width:100%;height:auto;max-height:210px;object-fit:contain}",
      ".ptn-shot .none{padding:var(--s-6);font-size:var(--fs-xs,11px);color:var(--text-3);line-height:1.5}",
      ".ptn-shotbar{display:flex;gap:var(--s-3);align-items:center;padding:var(--s-3) var(--s-4);",
      "  border-top:1px solid var(--line-soft);background:var(--solid-2)}",
      ".ptn-ta{width:100%;box-sizing:border-box;min-height:132px;resize:vertical;background:var(--solid-1);",
      "  border:1px solid var(--line);border-radius:var(--r-sm);color:var(--text);font:inherit;",
      "  font-size:var(--fs-sm,12px);line-height:1.55;padding:var(--s-5)}",
      ".ptn-ta:focus{outline:none;border-color:var(--accent)}",
      ".ptn-ta::placeholder{color:var(--text-3)}",
      ".ptn-row{display:flex;gap:var(--s-3);align-items:center;flex-wrap:wrap}",
      ".ptn-row select{background:var(--solid-1);border:1px solid var(--line);border-radius:var(--r-xs);",
      "  color:var(--text-2);font:inherit;font-size:var(--fs-2xs,10px);padding:var(--s-3) var(--s-4)}",
      ".ptn-btn{display:inline-flex;align-items:center;gap:var(--s-3);border:1px solid var(--line);",
      "  border-radius:var(--r-sm);background:var(--solid-3);color:var(--text);font:inherit;",
      "  font-size:var(--fs-2xs,10px);font-family:var(--mono);letter-spacing:.06em;text-transform:uppercase;",
      "  padding:var(--s-4) var(--s-5);cursor:pointer}",
      ".ptn-btn:hover{border-color:var(--accent-line)}",
      ".ptn-btn.go{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}",
      ".ptn-btn.go:hover{background:var(--accent-hover);border-color:var(--accent-hover)}",
      ".ptn-btn:disabled{opacity:.45;cursor:not-allowed}",
      ".ptn-hint{font-size:var(--fs-2xs,10px);color:var(--text-3);line-height:1.5}",
      ".ptn-hint kbd{font-family:var(--mono);border:1px solid var(--line);border-radius:var(--r-xs);",
      "  padding:0 3px;color:var(--text-2)}",
      ".ptn-warn{border:1px solid var(--warn-line);background:var(--warn-soft);color:var(--text-2);",
      "  border-radius:var(--r-sm);padding:var(--s-5);font-size:var(--fs-xs,11px);line-height:1.55}",
      ".ptn-warn b{color:var(--warn)}",
      ".ptn-sec{font-family:var(--mono);font-size:var(--fs-3xs,9.5px);letter-spacing:var(--track-label);",
      "  text-transform:uppercase;color:var(--text-3);border-top:1px solid var(--line-soft);",
      "  padding-top:var(--s-5);margin-top:var(--s-2)}",
      ".ptn-list{display:flex;flex-direction:column;gap:var(--s-4)}",
      ".ptn-item{display:flex;gap:var(--s-4);border:1px solid var(--line-soft);border-radius:var(--r-sm);",
      "  padding:var(--s-4);background:var(--solid-1)}",
      ".ptn-item img{width:64px;height:40px;object-fit:cover;border-radius:var(--r-xs);flex:none;",
      "  border:1px solid var(--line)}",
      ".ptn-item .tx{min-width:0;font-size:var(--fs-xs,11px);line-height:1.5;color:var(--text-2);",
      "  overflow-wrap:anywhere}",
      ".ptn-item .mt{display:flex;gap:var(--s-3);align-items:center;margin-top:var(--s-2)}",
      ".ptn-item .tm{font-family:var(--mono);font-size:var(--fs-2xs,10px);color:var(--accent)}",
      ".ptn-empty{font-size:var(--fs-xs,11px);color:var(--text-3)}",

      /* A NOTE FROM A VIEWER MUST NOT LOOK LIKE ONE OF YOURS. Same list, same
         clock — they are observations about the same seconds and separating
         them into two panels would hide that — but a different left edge and a
         handle on the front, because the dev has to weigh them differently and
         has to be able to do it while skim-reading. Somebody watching a
         compressed stream did not see what you saw. */
      ".ptn-item.guest{border-left:2px solid var(--accent-line)}",
      ".ptn-item .who{font-family:var(--mono);font-size:var(--fs-2xs,10px);",
      "  color:var(--accent);margin-right:var(--s-3)}",
      ".ptn-chip.guest{color:var(--accent);border-color:var(--accent-line)}",
    ].join("");
    document.head.appendChild(s);
  }

  /* ── the running build, and what can be read out of it ────────────────── */
  function gameFrame() { return document.getElementById("gameframe"); }

  function gameCanvas() {
    const f = gameFrame();
    if (!f) return null;
    // Same origin (/play/ is this dashboard), so this never throws in practice.
    // Guarded anyway: an iframe mid-navigation has no document at all.
    try { return (f.contentDocument || {}).querySelector ? f.contentDocument.querySelector("canvas") : null; }
    catch (e) { return null; }
  }

  function focusGame() {
    const c = gameCanvas();
    const f = gameFrame();
    try { (c || f || document.body).focus(); } catch (e) { /* nothing to focus */ }
  }

  /* Grab the frame. Returns {data, why} — never throws, and always explains
     itself, because a capture button that quietly produces nothing is worse
     than one that is honestly unavailable. */
  function grabFrame() {
    if (!gameFrame()) {
      return { data: "", why: "no build is running in the dashboard. Boot the "
                            + "current build to attach frames - or write the "
                            + "note without one." };
    }
    const canvas = gameCanvas();
    if (!canvas || !canvas.width || !canvas.height) {
      return { data: "", why: "the build has not put a canvas on screen yet." };
    }
    try {
      let source = canvas;
      if (canvas.width > MAX_FRAME_W) {
        const scaled = document.createElement("canvas");
        scaled.width = MAX_FRAME_W;
        scaled.height = Math.round(canvas.height * (MAX_FRAME_W / canvas.width));
        scaled.getContext("2d").drawImage(canvas, 0, 0, scaled.width, scaled.height);
        source = scaled;
      }
      const url = source.toDataURL("image/png");
      // A blank readback still encodes to a valid, tiny PNG. Length is the
      // cheap tell that we got a picture rather than an empty buffer.
      if (!url || url.length < 1024) {
        return { data: "", why: "the canvas read back empty - the build may be "
                              + "between frames." };
      }
      return { data: url, why: "" };
    } catch (err) {
      return { data: "", why: "the browser refused to read the canvas: "
                            + (err && err.message ? err.message : err) };
    }
  }

  /* ── the hotkey, on both sides of the iframe boundary ─────────────────── */
  /* One physical press must produce one toggle. Two listeners exist because
     the key can arrive in either document, and normally exactly one of them
     sees it — but "normally" is doing real work in that sentence: it depends on
     where focus is at the instant of the press, and a toggle that fires twice
     opens and shuts the pad so fast it looks like the hotkey is dead. Observed
     once while testing. 60ms is far under any human double-tap and far over the
     gap between two handlers running off the same event. */
  const SAME_PRESS_MS = 60;
  let lastHotkey = 0;

  function onHotkey(event) {
    if (event.key !== HOTKEY) return false;
    // Capture phase inside the build: swallow it so a project that binds F2
    // does not fire that action every time somebody reaches for the notepad.
    event.preventDefault();
    if (event.stopImmediatePropagation) event.stopImmediatePropagation();
    const now = Date.now();
    if (now - lastHotkey < SAME_PRESS_MS) return true;
    lastHotkey = now;
    toggle();
    return true;
  }

  /* The inner window is replaced on every navigation and its listeners go with
     it, so the flag lives ON that window: a fresh one has no flag and is
     re-armed on the next tick. */
  function armGame() {
    const f = gameFrame();
    if (!f) return;
    let win;
    try { win = f.contentWindow; } catch (e) { return; }
    if (!win || !win.document || win.__bgNotesArmed) return;
    win.__bgNotesArmed = true;
    win.addEventListener("keydown", onHotkey, true);
  }

  /* ── state ────────────────────────────────────────────────────────────── */
  function inPlaytests() {
    const view = document.getElementById("view-playtests");
    return Boolean(view && view.classList.contains("active"));
  }

  function clockOf(ts) {
    // Mirrors playtest._clock so the pad shows the same label the backend will
    // store. Without a session there is no origin and nothing to show.
    if (!session || !session.started_epoch) return "";
    const t = Math.max(0, ts - session.started_epoch);
    return `${String(Math.floor(t / 60)).padStart(2, "0")}:`
         + `${(t % 60).toFixed(2).padStart(5, "0")}`;
  }

  /* Freeze the moment this note is ABOUT: the timestamp and the frame, both
     taken now, both carried through however long the typing runs. */
  function armNote() {
    anchorTs = Date.now() / 1000;
    anchorClock = clockOf(anchorTs);
    const shot = grabFrame();
    frameData = shot.data;
    frameWhy = shot.why;
  }

  function disarm() {
    anchorTs = 0;
    anchorClock = "";
    frameData = "";
    frameWhy = "";
  }

  async function refresh() {
    const status = await fetch("/api/playtest/status")
      .then(r => r.json()).catch(() => null);
    const live = status && status.recording;
    const changed = (live ? live.id : 0) !== (session ? session.id : 0);
    session = live ? { id: live.id, name: live.name,
                       started_epoch: live.started_epoch } : null;
    if (session) {
      const got = await fetch(`/api/playtest/${session.id}/notes`)
        .then(r => r.json()).catch(() => null);
      notes = (got && got.data && got.data.notes) || [];
    } else if (changed) {
      notes = [];
    }
    return changed;
  }

  /* ── render ───────────────────────────────────────────────────────────── */
  function options(list, selected) {
    return list.map(v =>
      `<option value="${E(v)}"${v === selected ? " selected" : ""}>${E(v)}</option>`
    ).join("");
  }

  function renderTab() {
    if (!tab) return;
    tab.hidden = !inPlaytests();
    tab.classList.toggle("rec", Boolean(session));
    tab.title = session
      ? `Notepad - recording session ${session.id}. ${HOTKEY} from anywhere, `
        + `including while the game has focus.`
      : `Notepad - press ${HOTKEY}. Notes need a running recording to land on.`;
  }

  /* One note. `mine` is computed server-side (an empty author means the project
     owner), so this does not have to know that rule — it only has to render the
     difference. EVERYTHING here goes through E(): a viewer's handle and their
     words are strangers' text arriving in the dev's DOM, and this is the one
     place in the notepad where that is true. */
  function noteMarkup(note) {
    const guest = note.mine === false;
    return `
      <div class="ptn-item${guest ? " guest" : ""}">
        ${note.frame_rel
          ? `<img src="/api/preview?rel=${encodeURIComponent(note.frame_rel)}" alt="">`
          : ""}
        <div class="tx">${guest
            ? `<span class="who">${E(note.author || "viewer")}</span>` : ""}${E(note.text)}
          <div class="mt"><span class="tm">${E(note.clock || "")}</span>
            <span class="ptn-chip">${E(note.kind)}</span>
            <span class="ptn-chip">${E(note.seat)}</span>
            ${guest ? `<span class="ptn-chip guest">from chat</span>` : ""}</div>
        </div>
      </div>`;
  }

  function listMarkup() {
    const guests = notes.filter(n => n.mine === false).length;
    const mine = notes.length - guests;
    /* Counted separately in the header because "12 notes" over a list where
       nine of them are strangers' is a number that means something different
       from what it looks like. */
    const label = guests
      ? `${mine} yours · ${guests} from chat`
      : `${notes.length} typed`;
    return `<div class="ptn-sec">this session · ${E(label)}</div>
      <div class="ptn-list">${notes.length
        ? notes.slice().reverse().map(noteMarkup).join("")
        : `<div class="ptn-empty">Nothing written yet this session.</div>`}</div>`;
  }

  function renderPad() {
    if (!pad) return;
    const body = pad.querySelector(".ptn-body");
    if (!body) return;

    // No session, no note. This is not a missing feature — a note's timestamp
    // is SECONDS FROM SESSION START, and with nothing recording there is no
    // zero to measure from, so there is nowhere honest to put it. Say that,
    // rather than showing a compose box that throws the words away.
    if (!session) {
      builtFor = -1;
      body.innerHTML = `
        <div class="ptn-warn"><b>Nothing is recording.</b><br>
          A note is stamped in seconds from the start of a session — that is what
          lines it up with the video, the transcript and the telemetry. With no
          session running there is no clock to stamp it against, so the notepad
          stays shut. Hit <b>● record</b> above, then press
          <kbd>${E(HOTKEY)}</kbd> again.</div>
        <div class="ptn-hint">Frames come straight off the running build's
          canvas, so booting the build first means every note can carry one.</div>`;
      return;
    }

    const previous = body.querySelector(".ptn-ta");
    const draft = previous ? previous.value : "";
    const kind = (body.querySelector(".ptn-kind") || {}).value || "";
    const seat = (body.querySelector(".ptn-seat") || {}).value || "";
    builtFor = session.id;

    body.innerHTML = `
      <div class="ptn-when">
        <span class="ptn-chip at">${E(anchorClock || "--:--")}</span>
        <span class="ptn-chip">session ${E(String(session.id))}</span>
        <span class="ptn-hint">stamped when the pad opened, not when you save</span>
      </div>
      <div class="ptn-shot">
        ${frameData
          ? `<img src="${E(frameData)}" alt="frame captured with this note">`
          : `<div class="none">${E(frameWhy || "no frame attached.")}</div>`}
        <div class="ptn-shotbar">
          <button class="ptn-btn" data-act="regrab">${icon("export_image")} grab frame now</button>
          ${frameData ? `<button class="ptn-btn" data-act="drop">${icon("delete")} drop</button>` : ""}
        </div>
      </div>
      <textarea class="ptn-ta" placeholder="What just happened? Type it - this lands in the transcript at ${E(anchorClock || "this moment")}."></textarea>
      <div class="ptn-row">
        <select class="ptn-kind" aria-label="Kind">
          <option value="">kind · auto</option>${options(KINDS, kind)}
        </select>
        <select class="ptn-seat" aria-label="Seat">
          <option value="">seat · auto</option>${options(SEATS, seat)}
        </select>
      </div>
      <div class="ptn-row">
        <button class="ptn-btn go" data-act="save">${icon("note")} save note</button>
        <span class="ptn-hint"><kbd>Enter</kbd> saves · <kbd>Shift</kbd>+<kbd>Enter</kbd>
          newline · <kbd>Esc</kbd> back to the game</span>
      </div>
      ${listMarkup()}`;

    const area = body.querySelector(".ptn-ta");
    if (area) {
      area.value = draft;
      area.addEventListener("keydown", onComposeKey);
    }
  }

  function render() { renderTab(); renderPad(); }

  /* What the 4-second poll is allowed to touch.
   *
   * renderPad() replaces the whole body, which destroys and rebuilds the
   * textarea. Doing that on a timer means the caret jumps to the end and focus
   * is lost every four seconds, mid-sentence — the panel would eat the note it
   * exists to collect. So the poll refreshes only the list of already-saved
   * notes, and rebuilds the form ONLY when the thing it was built for changed:
   * a different session, or the recording starting or stopping under it. */
  function renderLive() {
    renderTab();
    if (!pad) return;
    const want = session ? session.id : -1;
    if (want !== builtFor) { renderPad(); return; }
    if (!session) return;
    const list = pad.querySelector(".ptn-list");
    const head = pad.querySelector(".ptn-sec");
    if (!list || !head) return;
    const fresh = document.createElement("div");
    fresh.innerHTML = listMarkup();
    head.replaceWith(fresh.firstElementChild);
    list.replaceWith(fresh.lastElementChild);
  }

  /* ── actions ──────────────────────────────────────────────────────────── */
  function onComposeKey(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      save();
    }
  }

  async function save() {
    const area = pad && pad.querySelector(".ptn-ta");
    const text = area ? area.value.trim() : "";
    if (!text) { say("nothing to save - type the note first"); return; }
    if (!session) { say("no recording is running - a note needs a session clock"); return; }

    const kind = (pad.querySelector(".ptn-kind") || {}).value || "";
    const seat = (pad.querySelector(".ptn-seat") || {}).value || "";
    const payload = { text, ts: anchorTs || Date.now() / 1000 };
    if (kind) payload.kind = kind;
    if (seat) payload.seat = seat;
    if (frameData) payload.frame = frameData;

    let body = null;
    try {
      const response = await fetch(`/api/playtest/${session.id}/notes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      body = await response.json();
      if (!response.ok || body.ok === false) {
        const message = (body && body.error && body.error.message)
          || `save failed · ${response.status}`;
        say("note not saved - " + message);
        return;
      }
    } catch (err) {
      // The words are still in the textarea and stay there. Losing a note to a
      // dropped request would be the one unrecoverable failure in this feature.
      say("note not saved - the dashboard is unreachable. Your text is still here.");
      return;
    }

    const saved = (body && body.data) || {};
    if (saved.frame_error) say("note saved, but the frame was rejected - " + saved.frame_error);
    notes.push(saved);
    if (area) area.value = "";
    // Re-arm: the NEXT note is about a new moment, and it should carry that
    // moment's timestamp and frame rather than this one's.
    armNote();
    render();
    const next = pad.querySelector(".ptn-ta");
    if (next) next.focus();
  }

  function onPadClick(event) {
    const button = event.target.closest("[data-act]");
    if (!button) return;
    const act = button.dataset.act;
    if (act === "save") { save(); return; }
    if (act === "drop") { frameData = ""; frameWhy = "frame dropped."; render(); return; }
    if (act === "regrab") {
      const shot = grabFrame();
      frameData = shot.data;
      frameWhy = shot.why;
      // Re-grabbing means "THIS is the moment", so the stamp moves with it.
      anchorTs = Date.now() / 1000;
      anchorClock = clockOf(anchorTs);
      render();
      const area = pad.querySelector(".ptn-ta");
      if (area) area.focus();
    }
  }

  /* ── open / close ─────────────────────────────────────────────────────── */
  async function open() {
    mount();
    isOpen = true;
    pad.classList.add("on");
    pad.setAttribute("aria-hidden", "false");

    // ANCHOR BEFORE ANY AWAIT. The frame and the timestamp have to be the
    // instant the key went down. Awaiting the status fetch first would hand
    // every note whatever the game had drawn by the time a round trip came
    // back — which, for the fast-moving moment that made you reach for the
    // notepad, is already the wrong frame.
    armNote();
    render();
    const area = pad.querySelector(".ptn-ta");
    if (area) area.focus();

    // Now find out which session it belongs to. anchorTs is already fixed; the
    // only thing this can change is the mm:ss LABEL, which needs the session's
    // start epoch to compute.
    const changed = await refresh();
    if (!isOpen) return;                 // closed again while the fetch was out
    anchorClock = clockOf(anchorTs);
    if (changed || !pad.querySelector(".ptn-ta")) {
      const draft = area ? area.value : "";
      render();
      const next = pad.querySelector(".ptn-ta");
      if (next) { next.value = draft; next.focus(); }
      return;
    }
    const stamp = pad.querySelector(".ptn-chip.at");
    if (stamp) stamp.textContent = anchorClock || "--:--";
    renderLive();
  }

  function close() {
    if (!pad) return;
    isOpen = false;
    pad.classList.remove("on");
    pad.setAttribute("aria-hidden", "true");
    disarm();
    // Hand the keyboard back, or the next WASD goes into a panel nobody can see.
    focusGame();
  }

  function toggle() { if (isOpen) close(); else open(); }

  /* ── mount ────────────────────────────────────────────────────────────── */
  function mount() {
    if (host) return;
    injectStyle();
    host = document.createElement("div");
    host.id = "ptnotes-host";

    tab = document.createElement("button");
    tab.className = "ptn-tab";
    tab.type = "button";
    tab.hidden = true;
    tab.innerHTML = `${icon("note", 15)}<span class="lb">notes</span>`
                  + `<span class="kb">${E(HOTKEY)}</span>`;
    tab.addEventListener("click", toggle);

    pad = document.createElement("aside");
    pad.className = "ptn-pad";
    pad.setAttribute("aria-hidden", "true");
    pad.setAttribute("aria-label", "Playtest notepad");
    pad.innerHTML = `
      <div class="ptn-head">${icon("note", 15)}<h4>notepad</h4>
        <button class="ptn-x" type="button" aria-label="Close notepad">&#10005;</button></div>
      <div class="ptn-body"></div>`;
    pad.querySelector(".ptn-x").addEventListener("click", close);
    pad.addEventListener("click", onPadClick);

    host.appendChild(tab);
    host.appendChild(pad);
    document.body.appendChild(host);
  }

  function tick() {
    armGame();
    if (!inPlaytests() && !isOpen) { renderTab(); return; }
    refresh().then(renderLive);
  }

  function init() {
    mount();
    // The parent half of the hotkey. It does not fire while the game has focus
    // (measured — see the header), which is exactly why armGame() exists; this
    // covers the other case, when focus is somewhere in the dashboard.
    document.addEventListener("keydown", event => {
      // Escape closes from anywhere while the pad is open. It used to close
      // only with focus inside the compose box, so anyone who clicked the
      // frame, a chip, or the page behind it had no keyboard way out — and the
      // × was a --text-3 glyph on a header that the orbit theme rendered
      // transparent, so there was no visible way out either. Two half-missing
      // affordances read as none.
      if (event.key === "Escape" && isOpen
          && document.activeElement !== gameFrame()) {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== HOTKEY) return;
      if (!inPlaytests() && !isOpen) return;
      // The build owns the keyboard right now, so the listener installed INSIDE
      // it owns this key. Handling it here as well is how one press became two
      // toggles.
      if (document.activeElement === gameFrame()) return;
      onHotkey(event);
    }, true);

    const view = document.getElementById("view-playtests");
    if (view) {
      new MutationObserver(renderTab)
        .observe(view, { attributes: true, attributeFilter: ["class"] });
    }
    if (timer) clearInterval(timer);
    timer = setInterval(tick, POLL_MS);
    tick();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  return { init, open, close, toggle, grabFrame, hotkey: HOTKEY };
})();
