/* modeledit.js — the 3D counterpart of spriteedit.js: open a mesh, look at it
 * from every angle, and pin down where things attach.
 *
 * The 3D pipeline (bgate_adapters/imageto3d.py, blender.py) generates .glb
 * files constantly and the dashboard could only ever list them — no preview,
 * no way to tell a clean export from a mangled one without opening Blender or
 * Godot. That gap is exactly the one spriteedit.js closed for pixels: land
 * 90% right, look at the actual thing, fix or label what's wrong, save it
 * back — except a mesh cannot be repainted from a browser the way a sheet
 * can, so this surface draws a line spriteedit.js never had to:
 *
 *   VIEWING is real editing here too. Shading modes, node visibility, camera
 *   framing — none of it round-trips into the .glb, but all of it is exactly
 *   what someone opens the file to check, and saving that state (the sidecar)
 *   means the SECOND time you open a model you are not starting over.
 *
 *   LABELLING is the part that writes something durable: named attachment
 *   SOCKETS — a 3D position and rotation, optionally hung off a named node.
 *   This is rigmap's slot anchor system carried into three dimensions, and
 *   deliberately draws from the SAME taxonomy (main_hand, off_hand, head, ...)
 *   bgate_core.modelmap imports from bgate_core.rigmap — a project that ships
 *   both a 2D rig and a 3D character must not maintain two vocabularies for
 *   "where the sword goes."
 *
 *   The mesh's bytes are NEVER rewritten. There is no save-pixels path here,
 *   only save-sidecar — read the file, never touch it.
 *
 * Same module shape as spriteedit.js / audiolab.js on purpose: embed(host,
 * rel) / unembed() / activate() for the Studio + rail integration, pick() /
 * open() / close() for the picker and the standalone workflow, and its own
 * injected <style> block so this file, like the other two, ships with zero
 * edits to app.css.
 */
import * as THREE from "three";
import { GLTFLoader } from "/static/vendor/three/examples/jsm/loaders/GLTFLoader.js";
import { OBJLoader } from "/static/vendor/three/examples/jsm/loaders/OBJLoader.js";
import { MTLLoader } from "/static/vendor/three/examples/jsm/loaders/MTLLoader.js";
import { DRACOLoader } from "/static/vendor/three/examples/jsm/loaders/DRACOLoader.js";
import { KTX2Loader } from "/static/vendor/three/examples/jsm/loaders/KTX2Loader.js";
import { OrbitControls } from "/static/vendor/three/examples/jsm/controls/OrbitControls.js";
import { TransformControls } from "/static/vendor/three/examples/jsm/controls/TransformControls.js";
import { RoomEnvironment } from "/static/vendor/three/examples/jsm/environments/RoomEnvironment.js";

// Shared across every load — a DRACOLoader/KTX2Loader owns a worker pool and
// a transcoder fetch, and creating a fresh one per model would mean paying
// that setup cost (and spinning up new workers) every time someone opens a
// different file. Blender's glTF exporter offers Draco compression as a
// one-click option and plenty of people click it; without these, GLTFLoader
// throws "No DRACOLoader instance provided" on exactly the models that
// enabled it, and KTX2 (KHR_texture_basisu) is the same story for textures.
const dracoLoader = new DRACOLoader();
dracoLoader.setDecoderPath("/static/vendor/three/examples/jsm/libs/draco/gltf/");
const ktx2Loader = new KTX2Loader();
ktx2Loader.setTranscoderPath("/static/vendor/three/examples/jsm/libs/basis/");
let ktx2Ready = false;

window.ModelEdit = (() => {
  // Same story as the sprite editor: built as a fullscreen overlay, needed as
  // a Studio page. _host set => mount inside it, flat and without a close.
  let _host = null;

  const visible = el => !!el && el.isConnected && el.getClientRects().length > 0;

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const I = (name, size) => (window.BGIcon ? BGIcon(name, { size: size || 16 }) : "");
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const clamp = (v, lo, hi) => v < lo ? lo : v > hi ? hi : v;
  const confirmAsk = (opts) => (window.askConfirm ? askConfirm(opts) : Promise.resolve(confirm(opts.body || opts.title || "")));

  const DISPLAY_MODES = [
    {id: "shaded", label: "shaded"},
    {id: "wireframe", label: "wireframe"},
    {id: "unlit", label: "unlit"},
    {id: "normals", label: "normals"},
  ];
  // Fallback until a model is opened and the server hands back its own
  // known_slots (kept in one place server-side: bgate_core.rigmap.KNOWN_SLOTS).
  let KNOWN_SLOTS = ["main_hand", "off_hand", "left_hand", "right_hand",
                     "head", "body", "feet", "throwable", "pivot", "muzzle", "fx"];

  const SLOT_COLOR = {
    main_hand: 0xe0524a, off_hand: 0xe7e7ea, left_hand: 0xe0a83c, right_hand: 0x5aa9e6,
    head: 0xe0a83c, body: 0x7fd08a, feet: 0x6adfc0, throwable: 0x7fd08a,
    pivot: 0x8a8f9c, muzzle: 0xe0a83c, fx: 0xe7e7ea,
  };
  const socketColor = name => SLOT_COLOR[name] || 0x9fd0ff;

  let S = null, $ = {};

  // ── style ──────────────────────────────────────────────────────────────
  function injectStyle(){
    if (document.getElementById("modeledit-style")) return;
    const s = document.createElement("style");
    s.id = "modeledit-style";
    s.textContent = [
      ".me-back{position:fixed;inset:0;z-index:1400;background:rgba(4,5,7,.86);backdrop-filter:blur(3px);display:flex;flex-direction:column}",
      ".me-back.me-embed{position:relative;inset:auto;z-index:auto;background:var(--surface-2);backdrop-filter:none;height:100%;border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden}",
      ".me-back.me-embed .me-closebtn{display:none}",
      ".me-land{display:grid;place-items:center;height:100%;min-height:420px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-lg)}",
      ".me-land-in{text-align:center;max-width:400px;padding:var(--s-8)}",
      ".me-land-in h3{font-size:var(--fs-xl);font-weight:var(--fw-regular);color:var(--text);margin-bottom:var(--s-4)}",
      ".me-land-in p{color:var(--text-3);font-size:var(--fs-md);line-height:var(--lh);margin-bottom:var(--s-7)}",
      ".me-bar{display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--seam);background:var(--iron);flex:none}",
      ".me-title{font-family:var(--mono);font-size:11.5px;letter-spacing:.1em;color:var(--bone);text-transform:uppercase}",
      ".me-sub{font-family:var(--mono);font-size:10px;color:var(--ash2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:280px}",
      ".me-dirty{color:var(--warn)}",
      ".me-spacer{flex:1}",
      ".me-btn{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;background:var(--plate);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;cursor:pointer}",
      ".me-btn .bgi{flex:none}",
      ".me-btn:hover:not(:disabled){border-color:var(--ember)}",
      ".me-btn:disabled{opacity:.4;cursor:default}",
      ".me-btn.go{background:var(--ember);color:var(--bg);border-color:var(--ember);font-weight:var(--fw-semi)}",
      ".me-btn.on{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}",
      ".me-closebtn{width:28px;height:28px;display:grid;place-items:center;background:none;border:1px solid var(--seam);border-radius:7px;color:var(--ash);cursor:pointer;flex:none}",
      ".me-closebtn:hover{border-color:var(--bad-line);color:var(--bad)}",
      ".me-body{flex:1;display:flex;min-height:0}",
      ".me-tools{width:52px;flex:none;background:var(--iron);border-right:1px solid var(--seam);padding:8px 0;display:flex;flex-direction:column;align-items:center;gap:5px;overflow-y:auto}",
      ".me-tool{width:36px;height:36px;display:grid;place-items:center;background:var(--plate);border:1px solid var(--seam);border-radius:8px;color:var(--ash);font:inherit;font-size:14px;padding:0;cursor:pointer;flex:none}",
      ".me-tool:hover{border-color:var(--ember);color:var(--bone)}",
      ".me-tool.on{background:var(--ember);border-color:var(--ember);color:var(--bg)}",
      ".me-toolsep{width:28px;height:1px;background:var(--seam);margin:4px 0;flex:none}",
      ".me-stage{flex:1;position:relative;min-width:0;overflow:hidden;background:var(--bg)}",
      ".me-canvas-host{position:absolute;inset:0}",
      ".me-canvas-host canvas{display:block;width:100%;height:100%}",
      ".me-hud{position:absolute;left:10px;bottom:10px;font-family:var(--mono);font-size:10px;color:var(--ash2);background:rgba(10,11,14,.8);border:1px solid var(--seam);border-radius:6px;padding:4px 8px;pointer-events:none;white-space:pre;line-height:1.5}",
      ".me-empty{position:absolute;inset:0;display:grid;place-items:center;color:var(--ash2);font-size:12px;text-align:center;padding:20px}",
      ".me-loading{position:absolute;inset:0;display:grid;place-items:center;color:var(--ash2);font-family:var(--mono);font-size:11px}",
      ".me-side{width:300px;flex:none;background:var(--iron);border-left:1px solid var(--seam);overflow-y:auto;padding:var(--s-4);display:flex;flex-direction:column;gap:var(--s-5)}",
      /* The sidebar's sections are .spanel + .sec-h out of app.css now (see
         sec() below), so nothing here restyles a header. What IS needed is the
         column they sit in: .me-side is a flex column, and without flex:none a
         panel with a 240px-tall outliner in it stretches or squashes its
         neighbours instead of scrolling the sidebar. The last-child rule kills
         the trailing gap every .me-row and .me-sock leaves behind, which
         otherwise reads as an uneven panel bottom on the three sections whose
         last element is a row rather than a tree. */
      ".me-side > .spanel{flex:none;min-width:0}",
      ".me-side > .spanel > :last-child{margin-bottom:0}",
      ".me-row{display:flex;align-items:center;gap:7px;margin-bottom:7px}",
      ".me-row label{font-family:var(--mono);font-size:10px;color:var(--ash2);flex:none}",
      ".me-in{flex:1;min-width:0;background:var(--void);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font:inherit;font-size:11.5px;padding:5px 7px}",
      ".me-in:focus{outline:none;border-color:var(--ember)}",
      ".me-in.num{flex:none;width:0;min-width:0}",
      ".me-vec3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;flex:1}",
      ".me-sel{flex:1;min-width:0;background:var(--void);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font:inherit;font-size:11.5px;padding:5px 7px}",
      ".me-tree{display:flex;flex-direction:column;gap:1px;max-height:240px;overflow-y:auto}",
      ".me-trow{display:flex;align-items:center;gap:6px;width:100%;text-align:left;padding:4px 6px;background:none;border:0;border-radius:var(--r-xs);color:var(--text-2);font:inherit;font-size:11px;cursor:pointer}",
      "button.me-trow:hover{background:var(--surface-3);color:var(--text)}",
      ".me-trow.on{background:var(--accent-soft);color:var(--text)}",
      ".me-trow .lb{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono)}",
      ".me-trow .tris{flex:none;font-family:var(--mono);font-size:9px;color:var(--text-3);font-variant-numeric:tabular-nums}",
      ".me-eye{flex:none;width:20px;height:20px;display:grid;place-items:center;background:none;border:0;color:var(--ash2);cursor:pointer;padding:0}",
      ".me-eye:hover{color:var(--bone)}",
      ".me-eye.off{opacity:.35}",
      ".me-sock{display:flex;align-items:center;gap:7px;padding:5px 7px;border:1px solid var(--seam);border-radius:7px;margin-bottom:5px;cursor:pointer}",
      ".me-sock:hover{border-color:var(--ember)}",
      ".me-sock.on{border-color:var(--accent);background:var(--accent-soft)}",
      ".me-sock i{width:9px;height:9px;border-radius:50%;flex:none}",
      ".me-sock .lb{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);font-size:11px;color:var(--bone)}",
      ".me-sock .node{font-size:9px;color:var(--ash2)}",
      ".me-sock .x{flex:none;color:var(--ash2);padding:0 3px;cursor:pointer}",
      ".me-sock .x:hover{color:var(--bad)}",
      // overflow-wrap: the res:// path in the engine readout is one unbroken
      // token, and inside a .spanel an unbreakable string hangs out over the
      // panel's own right edge rather than just widening a column nobody
      // measured.
      ".me-empty-note{font-size:11px;color:var(--ash2);padding:6px 2px;overflow-wrap:anywhere}",
      ".me-note{width:100%;min-height:64px;background:var(--void);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font:inherit;font-size:11.5px;padding:6px 8px;resize:vertical}",
      ".me-note:focus{outline:none;border-color:var(--ember)}",
      ".me-modes{display:flex;flex-wrap:wrap;gap:4px}",
      ".me-mode{flex:1 1 40%;padding:6px 4px;background:var(--plate);border:1px solid var(--seam);border-radius:7px;color:var(--ash);font:inherit;font-size:10.5px;cursor:pointer;text-align:center}",
      ".me-mode:hover{border-color:var(--ember)}",
      ".me-mode.on{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}",
      ".me-anim-row{display:flex;align-items:center;gap:6px;margin-bottom:6px}",
      ".me-anim-row input[type=range]{flex:1;accent-color:var(--accent)}",
      ".me-clipname{font-family:var(--mono);font-size:10px;color:var(--ash2)}",
      ".me-navcube{position:absolute;right:10px;top:10px;width:84px;height:84px;pointer-events:none;opacity:.9}",
      ".me-pick{position:fixed;inset:0;z-index:1401;background:rgba(4,5,7,.9);display:flex;align-items:center;justify-content:center;padding:40px}",
      ".me-pick-box{background:var(--iron);border:1px solid var(--seam);border-radius:12px;width:min(760px,100%);height:min(600px,90vh);display:flex;flex-direction:column;overflow:hidden}",
      ".me-pick-bar{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--seam)}",
      ".me-pick-bar input{flex:1;background:var(--void);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:12px;padding:7px 10px}",
      ".me-pick-list{overflow-y:auto;padding:8px;flex:1;min-width:0}",
      ".me-pick-i{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:7px;cursor:pointer;font-family:var(--mono);font-size:11px;color:var(--bone)}",
      ".me-pick-i:hover{background:var(--plate)}",
      ".me-pick-i .icon{flex:none;width:30px;height:30px;display:grid;place-items:center;background:var(--bg);border:1px solid var(--seam);border-radius:6px;color:var(--ash2)}",
      ".me-pick-i .name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".me-pick-i .m{color:var(--ash2);font-size:10px}",
      ".me-tag{font-size:9px;padding:1px 5px;border-radius:999px;border:1px solid var(--seam);color:var(--ash2)}",
      ".me-tag.warn{border-color:var(--warn);color:var(--warn)}",
    ].join("\n");
    document.head.appendChild(s);
  }

  // ── open / close ──────────────────────────────────────────────────────
  async function open(rel){
    if (!rel) return pick();
    if (S && S.dirty && !await confirmAsk({
        title: "Discard unsaved labels?",
        body: "Sockets, node overrides or notes you changed on " + S.name +
              " have not been saved.",
        ok: "discard", danger: true}))
      return;
    // The previous model's renderer, released before the next one is built.
    // `S` is replaced wholesale below, so opening a second model left the first
    // one's WebGLRenderer alive with no reference anything could reach: the rAF
    // loop stops (it compares S.three against the block it captured) but the GL
    // context, its textures and its framebuffers do not go anywhere. Browsers
    // cap live contexts — Chromium at ~16 — and force-lose the OLDEST when the
    // cap is hit, so a session spent browsing the picker eventually starts
    // killing contexts to make room for the one being looked at.
    if (S && S.three) teardownThree();
    mount();
    $.stage.querySelector(".me-empty")?.remove();
    const loading = document.createElement("div");
    loading.className = "me-loading";
    loading.textContent = "loading " + rel + " …";
    $.stage.appendChild(loading);
    try {
      const r = await fetch("/api/model3d/open?rel=" + encodeURIComponent(rel));
      const body = await r.json();
      if (!r.ok) throw new Error((body.error && body.error.message) || r.statusText);
      const d = body.data || body;
      KNOWN_SLOTS = d.known_slots && d.known_slots.length ? d.known_slots : KNOWN_SLOTS;
      S = {
        rel: d.rel, name: d.name, ext: d.ext, viewable: d.viewable,
        rawUrl: d.raw_url, resPath: d.res_path, bytes: d.bytes,
        model: d.model, dirty: false,
        tool: "select", selectedNode: null, selectedSocket: null,
        placeSlot: KNOWN_SLOTS[0] || "socket",
        clips: [], mixer: null, action: null, playing: false,
        nodeIndex: new Map(), matCache: new WeakMap(), socketObjects: new Map(),
        stats: {tris: 0, nodes: 0, materials: 0},
      };
      loading.remove();
      renderChrome();
      if (!S.viewable) {
        $.stage.innerHTML = '<div class="me-empty">' +
          `<div>${E(S.ext)} is not a format three.js can load in the browser.<br>` +
          'Open it in Blender, or convert it to .glb, to view it here.</div></div>';
        return;
      }
      ensureThree();
      loadIntoScene();
    } catch (e) {
      loading.remove();
      $.stage.innerHTML = `<div class="me-empty">could not open ${E(rel)}<br>${E(e.message || e)}</div>`;
      say("could not open " + rel + ": " + (e.message || e), "err");
    }
  }

  function close(){
    if (_host) { unembed(); return; }
    teardownThree();
    S = null;
    document.getElementById("me-back")?.remove();
    $ = {};
  }

  // ── picker ────────────────────────────────────────────────────────────
  let pickTimer = null;
  async function pick(q){
    injectStyle();
    let box = document.getElementById("me-pick");
    if (!box) {
      box = document.createElement("div");
      box.id = "me-pick";
      box.className = "me-pick";
      box.onclick = e => { if (e.target === box) closePick(); };
      document.body.appendChild(box);
    }
    box.innerHTML =
      '<div class="me-pick-box">' +
        '<div class="me-pick-bar">' +
          `<input id="me-pick-q" placeholder="search models…" value="${E(q || "")}" ` +
          'oninput="ModelEdit.pickSearch(this.value)" autofocus>' +
          `<button class="me-btn" onclick="ModelEdit.closePick()">${I("close")} close</button>` +
        '</div>' +
        '<div class="me-pick-list" id="me-pick-list"><div class="me-empty-note">searching…</div></div>' +
      '</div>';
    box.querySelector("#me-pick-q").focus();
    // A REFUSAL AND AN EMPTY PROJECT MUST NOT LOOK THE SAME.
    // This read the body without ever checking the status, so /api/model3d/list
    // answering 4xx/5xx — the envelope carries {error}, not {models} — landed on
    // `d.models || []` and painted "no 3D models found in this project." A
    // scanner that fell over therefore reported itself as a finished scan of a
    // project with nothing in it, and there was no unhandled rejection to see
    // either: a dropped connection threw out of this function and left the list
    // saying "searching…" for the rest of the session.
    let d = null;
    try {
      const r = await fetch("/api/model3d/list?limit=200" +
        (q ? "&q=" + encodeURIComponent(q) : ""));
      d = await r.json();
      if (!r.ok) throw new Error((d && d.error && d.error.message) || r.statusText);
    } catch (e) {
      const el = document.getElementById("me-pick-list");
      if (el) el.innerHTML = '<div class="me-empty-note">could not list this ' +
        'project\'s models - ' + E(e.message || e) + '</div>';
      return;
    }
    const list = document.getElementById("me-pick-list");
    if (!list) return;
    const models = d.models || [];
    if (!models.length) {
      list.innerHTML = '<div class="me-empty-note">no 3D models found in this project.</div>';
      return;
    }
    list.innerHTML = models.map(m => `
      <div class="me-pick-i" onclick="ModelEdit.closePick();ModelEdit.open('${E(m.rel)}')">
        <span class="icon">${I(m.viewable ? "model" : "gltf", 16)}</span>
        <span class="name">${E(m.rel)}</span>
        ${m.annotated ? '<span class="me-tag">labelled</span>' : ""}
        ${!m.viewable ? '<span class="me-tag warn">' + E(m.ext) + '</span>' : ""}
        <span class="m">${(m.bytes / 1024).toFixed(0)} KB</span>
      </div>`).join("");
  }
  function pickSearch(v){
    clearTimeout(pickTimer);
    pickTimer = setTimeout(() => pick(String(v || "").trim()), 200);
  }
  function closePick(){ document.getElementById("me-pick")?.remove(); }

  // ── chrome (built once, reused across opens) ─────────────────────────
  function mount(){
    let back = document.getElementById("me-back");
    if (back) { $.back = back; return; }
    injectStyle();
    back = document.createElement("div");
    back.id = "me-back";
    back.className = "me-back";
    document.body.appendChild(back);
    $.back = back;
    if (_host) {
      back.classList.add("me-embed");
      _host.innerHTML = "";
      _host.appendChild(back);
    }
    renderChrome();
  }

  function renderChrome(){
    if (!$.back) return;
    const dirty = S && S.dirty;
    $.back.innerHTML =
      '<div class="me-bar">' +
        '<span class="me-title">Model</span>' +
        `<span class="me-sub" title="${S ? E(S.rel) : ""}">${S ? E(S.rel) : "nothing open"}</span>` +
        // A " *" on the end of a path is not a save state. See SaveState in
        // seats/_core.js for what replaced it and why the toast was not it.
        (S ? '<span id="me-save-state"></span>' : "") +
        '<span class="me-spacer"></span>' +
        `<button class="me-btn" onclick="ModelEdit.pick()">${I("model")} open…</button>` +
        (S && S.viewable ? `<button class="me-btn" onclick="ModelEdit.fit()">${I("fit")} frame</button>` : "") +
        (S && S.viewable ? `<button class="me-btn" onclick="ModelEdit.snapshot()">${I("export_image")} snapshot</button>` : "") +
        (S ? `<button class="me-btn" onclick="ModelEdit.resetLabels()">${I("undo")} reset labels</button>` : "") +
        (S ? `<button class="me-btn go" onclick="ModelEdit.save()" ${dirty ? "" : "disabled"}>${I("export")} save</button>` : "") +
        (S ? '<button class="me-btn" onclick="ModelEdit.handoff()" title="Import it into the engine, ' +
             'instance it in a scene, or hand it to an agent">' + I("gate") + " put in game</button>" : "") +
        '<button class="me-closebtn" onclick="ModelEdit.close()" title="Close">✕</button>' +
      '</div>' +
      '<div class="me-body">' +
        '<div class="me-tools" id="me-tools"></div>' +
        '<div class="me-stage" id="me-stage">' +
          (S ? "" : '<div class="me-empty">Choose a model to open — the file picker searches every .glb, .gltf and .obj in the project.<br><br>' +
            `<button class="me-btn" onclick="ModelEdit.pick()">${I("model")} open a model…</button></div>`) +
        '</div>' +
        '<div class="me-side" id="me-side"></div>' +
      '</div>';
    $.stage = document.getElementById("me-stage");
    $.tools = document.getElementById("me-tools");
    $.side = document.getElementById("me-side");
    if (S) { renderTools(); renderSide(); }
    if (S && S.three && S.viewable) {
      // The canvas host was just rebuilt; the renderer's DOM element survives
      // in memory (ensureThree only creates it once) and just needs reparenting.
      $.stage.appendChild(S.three.hostEl);
      resizeThree();
    }
  }

  function renderTools(){
    if (!$.tools || !S) return;
    $.tools.innerHTML = [
      tool("select", "select", S.tool === "select"),
      tool("anchor", "socket", S.tool === "socket"),
      '<div class="me-toolsep"></div>',
      toggle("snap_grid", "grid", S.model.display.grid),
      toggle("stage", "ground", S.model.display.ground),
      toggle("loop", "autorotate", S.model.display.autorotate),
    ].join("");
    function tool(icon, id, on){
      return `<button class="me-tool ${on ? "on" : ""}" title="${id}" ` +
        `onclick="ModelEdit.setTool('${id}')">${I(icon, 18)}</button>`;
    }
    function toggle(icon, field, on){
      return `<button class="me-tool ${on ? "on" : ""}" title="${field}" ` +
        `onclick="ModelEdit.toggleDisplay('${field}')">${I(icon, 18)}</button>`;
    }
  }

  function setTool(id){
    if (!S) return;
    S.tool = id;
    if (S.three && S.three.transform) S.three.transform.detach();
    renderTools();
  }

  function toggleDisplay(field){
    if (!S) return;
    S.model.display[field] = !S.model.display[field];
    markDirty();
    applyDisplayToggles();
    renderTools();
  }

  function setBackground(hex){
    if (!S) return;
    S.model.display.background = hex || null;
    markDirty();
    applyDisplayToggles();
    renderSide();
  }

  // ── side panel ────────────────────────────────────────────────────────
  /* ONE SHAPE FOR EVERY LID IN THIS SIDEBAR, and it is the app's, not a local
     invention: .spanel + .sec-h out of app.css, the same construction
     settingsview.js and every seat workspace uses.
     What it replaced was `.me-h` — nine px of letter-spaced grey with no rule
     and no surface under it. Six of those in one 300px column meant "outliner",
     "sockets" and "notes" were three labels floating in one undifferentiated
     stack: nothing said where a section started, so the socket list read as a
     continuation of the node tree above it.
     NO `s-<seat>` CLASS. That variant tints the header glyph with a seat's hue
     and belongs to the seat workspaces, where a panel really is one seat's. The
     3D viewer is a tool, not a seat, and modeledit_tools.js mounts ITS panels
     into the same .me-body with a plain .spanel — tinting one column's icons
     art-pink and not the other's reads as a rendering fault, not a signal. The
     left edge carries KIND instead, which is the thing that actually varies
     here: a tree you edit, a readout you cannot. */
  function sec(icon, title, body, o){
    o = o || {};
    const n = (o.n === undefined || o.n === null || o.n === "")
      ? "" : `<span class="sec-n${o.tone ? " " + o.tone : ""}">${E(o.n)}</span>`;
    return `<section class="spanel ${o.kind || ""}">` +
      `<div class="sec-h">${I(icon, 15)}<h3 class="sec-t">${E(title)}</h3>${n}` +
      (o.note ? `<span class="sec-sub">${E(o.note)}</span>` : "") +
      (o.actions ? `<span class="sec-a">${o.actions}</span>` : "") +
      `</div>${body}</section>`;
  }

  function renderSide(){
    if (!$.side || !S) return;
    const nodes = [...S.nodeIndex.entries()];
    $.side.innerHTML =
      sec("lighting", "Shading",
        '<div class="me-modes">' + DISPLAY_MODES.map(m =>
          `<button class="me-mode ${S.model.display.mode === m.id ? "on" : ""}" ` +
          `onclick="ModelEdit.setDisplayMode('${m.id}')">${E(m.label)}</button>`).join("") +
        '</div>' +
        '<div class="me-row"><label>bg</label>' +
        `<input type="color" value="${E(S.model.display.background || "#14161b")}" ` +
        'onchange="ModelEdit.setBackground(this.value)">' +
        `<button class="me-btn" onclick="ModelEdit.setBackground(null)">reset</button></div>`,
        { note: S.viewable ? S.model.display.mode : "" }) +

      (S.clips.length ? animSection() : "") +

      sec("outline", "Outliner",
        (nodes.length
          ? '<div class="me-tree">' + nodes.map(([name, obj]) => nodeRow(name, obj)).join("") + '</div>'
          : '<div class="me-empty-note">no named nodes.</div>'),
        { kind: "k-list", n: nodes.length }) +

      selInspector() +

      sec("anchor", "Sockets",
        '<div class="me-row">' +
          `<select class="me-sel" id="me-slot">` +
          KNOWN_SLOTS.map(s => `<option value="${E(s)}" ${s === S.placeSlot ? "selected" : ""}>${E(s)}</option>`).join("") +
          `<option value="__custom" ${!KNOWN_SLOTS.includes(S.placeSlot) ? "selected" : ""}>custom…</option>` +
          '</select>' +
        '</div>' +
        (S.tool === "socket"
          ? '<div class="me-empty-note">click the model to place / move "' + E(S.placeSlot) + '".</div>'
          : "") +
        (S.model.sockets.length
          ? S.model.sockets.map(sk => socketRow(sk)).join("")
          : '<div class="me-empty-note">no attachment sockets yet — pick a slot above, choose the socket tool, then click the mesh.</div>'),
        { kind: "k-list", n: S.model.sockets.length }) +

      sec("note", "Notes",
        `<textarea class="me-note" id="me-notes" placeholder="anything a teammate should know about this model…" ` +
        `oninput="ModelEdit.notesField(this.value)">${E(S.model.notes || "")}</textarea>`,
        { kind: "k-doc" }) +

      (S.resPath
        ? sec("gate", "In the engine",
            `<div class="me-empty-note">${E(S.resPath)}</div>`, { kind: "k-read" })
        : "");

    const slotSel = document.getElementById("me-slot");
    if (slotSel) slotSel.onchange = () => {
      const v = slotSel.value;
      if (v === "__custom") {
        const name = prompt("custom slot name") || "";
        S.placeSlot = name.trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_") || S.placeSlot;
        renderSide();
      } else S.placeSlot = v;
    };
  }

  /* The count chip is FILLED (.sec-n.live) while a clip is running, which is
     the one piece of live state in this sidebar: the viewport can be showing a
     model mid-walk-cycle with the panel scrolled off, and a hollow pill said
     nothing about that. */
  function animSection(){
    return sec("animation", "Animation",
      '<div class="me-anim-row">' +
        `<select class="me-sel" onchange="ModelEdit.setClip(this.value)">` +
        S.clips.map(c => `<option value="${E(c)}" ${c === S.activeClipName ? "selected" : ""}>${E(c)}</option>`).join("") +
        '</select>' +
        `<button class="me-btn" onclick="ModelEdit.togglePlay()">${I(S.playing ? "pause" : "run", 14)}</button>` +
      '</div>' +
      '<div class="me-anim-row"><input type="range" min="0" max="1" step="0.001" value="0" ' +
        'id="me-scrub" oninput="ModelEdit.scrub(this.value)"></div>',
      { kind: "k-list", n: S.clips.length, tone: S.playing ? "live" : "" });
  }

  function nodeRow(name, obj){
    const ov = S.model.nodes[name];
    const on = ov ? ov.visible !== false : true;
    const tris = obj.userData._tris || 0;
    return `<button class="me-trow ${S.selectedNode === name ? "on" : ""}" ` +
      `onclick="ModelEdit.selectNode('${E(name)}')">` +
      `<span class="me-eye ${on ? "" : "off"}" onclick="event.stopPropagation();ModelEdit.toggleNode('${E(name)}')">${I(on ? "visible" : "hidden", 13)}</span>` +
      `<span class="lb">${E(name)}</span>` +
      (tris ? `<span class="tris">${tris}△</span>` : "") +
      '</button>';
  }

  function socketRow(sk){
    return `<div class="me-sock ${S.selectedSocket === sk.name ? "on" : ""}" ` +
      `onclick="ModelEdit.selectSocket('${E(sk.name)}')">` +
      `<i style="background:#${socketColor(sk.name).toString(16).padStart(6, "0")}"></i>` +
      `<span class="lb">${E(sk.name)}${sk.node ? `<span class="node"> · ${E(sk.node)}</span>` : ""}</span>` +
      `<span class="x" onclick="event.stopPropagation();ModelEdit.deleteSocket('${E(sk.name)}')">${I("delete", 12)}</span>` +
      '</div>';
  }

  function selInspector(){
    if (S.selectedSocket) {
      const sk = S.model.sockets.find(s => s.name === S.selectedSocket);
      if (!sk) return "";
      return sec("pin", "Selected socket",
        row("name", `<input class="me-in" value="${E(sk.name)}" onchange="ModelEdit.renameSocket('${E(sk.name)}', this.value)">`) +
        row("pos", vec3(sk.position, sk.name, "position")) +
        row("rot°", vec3(sk.rotation, sk.name, "rotation")) +
        row("note", `<input class="me-in" value="${E(sk.note || "")}" onchange="ModelEdit.socketNote('${E(sk.name)}', this.value)">`));
    }
    if (S.selectedNode) {
      const name = S.selectedNode;
      const ov = S.model.nodes[name] || {visible: true, color: null};
      return sec("select", "Selected node",
        row("name", `<span class="me-in" style="border:0;background:none;padding:5px 0">${E(name)}</span>`) +
        row("tint", `<input type="color" value="${ov.color || "#9fd0ff"}" onchange="ModelEdit.nodeColor('${E(name)}', this.value)">` +
          `<button class="me-btn" onclick="ModelEdit.nodeColor('${E(name)}', null)">clear</button>`) +
        row("opacity", `<input type="range" min="0" max="1" step="0.05" value="${ov.opacity != null ? ov.opacity : 1}" ` +
          `oninput="ModelEdit.nodeOpacity('${E(name)}', this.value)">`),
        { n: (ov.visible === false) ? "hidden" : "", tone: "warn" });
    }
    return "";
    function row(label, html){
      return `<div class="me-row"><label>${E(label)}</label>${html}</div>`;
    }
    /* ONE AXIS PER FIELD, and deliberately NOT the whole vector.
     *
     * Each of the three boxes used to bake the OTHER two components into its
     * own onchange as literals, so the handler was only correct for as long as
     * nothing moved the socket after it was rendered. That was survivable only
     * because every write path called renderSide() immediately afterwards and
     * re-baked all three — which is exactly the full-panel rebuild the gizmo
     * drag can no longer afford (see the objectChange listener). Take the
     * rebuild away and the stale literals become a socket that silently jumps
     * back to where it was two edits ago on the two axes you did not touch.
     * Sending the index instead means no field ever carries a value it does
     * not own. */
    function vec3(v, name, key){
      return '<div class="me-vec3">' + [0, 1, 2].map(i =>
        `<input class="me-in num" type="number" step="0.01" value="${v[i].toFixed(3)}" ` +
        `onchange="ModelEdit.socketAxis('${E(name)}','${key}',${i},this.value)">`
      ).join("") + '</div>';
    }
  }

  function markDirty(){ if (S) { S.dirty = true; renderBar(); } }
  function renderBar(){
    // Cheap partial repaint of just the bar's dirty state / save button,
    // rather than rebuilding the whole chrome (which would blow away canvas
    // focus and the outliner's scroll position) on every field edit.
    const save = $.back && $.back.querySelector(".me-btn.go");
    if (save) save.disabled = !(S && S.dirty);
    const sub = $.back && $.back.querySelector(".me-sub");
    if (sub && S) sub.textContent = S.rel;
    paintSaveState();
  }

  /* The one place that answers "is my work on disk". Every state a save has
     is here, including the two nobody rendered before: in flight, and failed.
     `at: 0` on a freshly-opened clean model is deliberate — it IS on disk, but
     it was not saved by this session, and stamping it "saved just now" would
     be a made-up fact. */
  function paintSaveState(){
    if (!window.SaveState || !S || !$.back) return;
    const el = $.back.querySelector("#me-save-state");
    if (!el) return;
    if (S.saving)         SaveState.set(el, {state:"saving"});
    else if (S.saveError) SaveState.set(el, {state:"error", detail:S.saveError});
    else if (S.dirty)     SaveState.set(el, {state:"dirty"});
    else                  SaveState.set(el, {state:"saved", at:S.savedAt || 0});
  }

  // ── field handlers ────────────────────────────────────────────────────
  function notesField(v){ if (S) { S.model.notes = v; markDirty(); } }

  function selectNode(name){
    if (!S) return;
    S.selectedSocket = null;
    S.selectedNode = S.selectedNode === name ? null : name;
    if (S.three) S.three.transform.detach();
    highlightSelection();
    renderTools_(); renderSide();
  }
  function renderTools_(){ /* placeholder kept for symmetry; tools panel does not depend on selection */ }

  function toggleNode(name){
    if (!S) return;
    const cur = S.model.nodes[name] || {visible: true, color: null};
    S.model.nodes[name] = {...cur, visible: cur.visible === false};
    markDirty(); applyNodeOverrides(); renderSide();
  }
  function nodeColor(name, hex){
    if (!S) return;
    const cur = S.model.nodes[name] || {visible: true, color: null};
    S.model.nodes[name] = {...cur, color: hex};
    markDirty(); applyNodeOverrides(); renderSide();
  }
  function nodeOpacity(name, v){
    if (!S) return;
    const cur = S.model.nodes[name] || {visible: true, color: null};
    S.model.nodes[name] = {...cur, opacity: parseFloat(v)};
    markDirty(); applyNodeOverrides();
  }

  function selectSocket(name){
    if (!S) return;
    S.selectedNode = null;
    S.selectedSocket = S.selectedSocket === name ? null : name;
    attachGizmoToSelectedSocket();
    renderSide();
  }
  function deleteSocket(name){
    if (!S) return;
    S.model.sockets = S.model.sockets.filter(s => s.name !== name);
    if (S.selectedSocket === name) S.selectedSocket = null;
    markDirty(); rebuildSocketObjects(); renderSide();
  }
  function renameSocket(oldName, raw){
    if (!S) return;
    const name = String(raw || "").trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_");
    if (!name || S.model.sockets.some(s => s.name === name && s.name !== oldName)) {
      say("that socket name is empty or already used", "err"); renderSide(); return;
    }
    const sk = S.model.sockets.find(s => s.name === oldName);
    if (sk) sk.name = name;
    S.selectedSocket = name;
    markDirty(); rebuildSocketObjects(); renderSide();
  }
  function socketAxis(name, key, i, value){
    if (!S) return;
    const sk = S.model.sockets.find(s => s.name === name);
    if (!sk || !Array.isArray(sk[key])) return;
    const n = parseFloat(value);
    sk[key][i] = isFinite(n) ? n : 0;
    markDirty(); syncSocketObject(sk); syncSocketInputs(sk);
  }

  /* Write a socket's numbers into the inspector's own six boxes, in place.
   *
   * The alternative — and what the gizmo's objectChange used to do — is
   * renderSide(), which replaces $.side.innerHTML wholesale. On a drag that
   * fires per pointermove, so the outliner's scroll position was thrown away
   * on every frame, the slot <select> and any open colour picker under the
   * cursor were torn off their elements, and a row per named node was rebuilt
   * dozens of times a second on a model that has hundreds of them. Same
   * reasoning as renderBar(): touch the fields that moved, leave the panel
   * that did not. A field the operator is typing in is skipped, because
   * overwriting the caret's own box mid-edit is its own bug. */
  function syncSocketInputs(sk){
    if (!$.side || !sk) return;
    const groups = $.side.querySelectorAll(".me-vec3");
    [sk.position, sk.rotation].forEach((vec, g) => {
      if (!groups[g] || !Array.isArray(vec)) return;
      const inputs = groups[g].querySelectorAll("input");
      for (let i = 0; i < inputs.length && i < 3; i++){
        if (document.activeElement !== inputs[i])
          inputs[i].value = Number(vec[i]).toFixed(3);
      }
    });
  }
  function socketNote(name, v){
    const sk = S && S.model.sockets.find(s => s.name === name);
    if (sk) { sk.note = v; markDirty(); }
  }

  function setDisplayMode(mode){
    if (!S) return;
    S.model.display.mode = mode;
    markDirty();
    applyDisplayMode();
    renderSide();
  }

  function setClip(name){
    if (!S || !S.three) return;
    S.activeClipName = name;
    const clip = S.three.gltfAnimations.find(c => c.name === name);
    if (S.three.mixer) S.three.mixer.stopAllAction();
    if (clip) {
      S.three.action = S.three.mixer.clipAction(clip);
      S.three.action.play();
      S.playing = true;
    }
    renderSide();
  }
  function togglePlay(){
    if (!S || !S.three || !S.three.action) return;
    S.playing = !S.playing;
    S.three.action.paused = !S.playing;
    renderSide();
  }
  function scrub(v){
    if (!S || !S.three || !S.three.action) return;
    S.playing = false;
    S.three.action.paused = true;
    const clip = S.three.action.getClip();
    S.three.action.time = clamp(parseFloat(v), 0, clip.duration);
    S.three.mixer.update(0);
    requestRender();
  }

  // ── three.js ──────────────────────────────────────────────────────────
  function ensureThree(){
    if (S.three) { $.stage.appendChild(S.three.hostEl); resizeThree(); return; }
    const hostEl = document.createElement("div");
    hostEl.className = "me-canvas-host";
    $.stage.appendChild(hostEl);

    const renderer = new THREE.WebGLRenderer({antialias: true, alpha: false, preserveDrawingBuffer: true});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    hostEl.appendChild(renderer.domElement);

    // KTX2Loader needs one real WebGL context to ask what compressed texture
    // formats the GPU actually supports before it can transcode anything;
    // detectSupport() is idempotent-ish but there is no reason to call it
    // more than once per page, so it is gated on the shared loader instead
    // of the per-model S.three block above it.
    if (!ktx2Ready) { ktx2Loader.detectSupport(renderer); ktx2Ready = true; }

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x14161b);

    const camera = new THREE.PerspectiveCamera(50, 1, 0.01, 5000);
    camera.position.set(3, 2.4, 3.6);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.target.set(0, 0.8, 0);
    controls.update();

    const hemi = new THREE.HemisphereLight(0xdfe8ff, 0x1a1a1a, 1.1);
    const key = new THREE.DirectionalLight(0xffffff, 2.6);
    key.position.set(4, 6, 3);
    key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024);
    key.shadow.camera.near = 0.5; key.shadow.camera.far = 40;
    key.shadow.camera.left = -6; key.shadow.camera.right = 6;
    key.shadow.camera.top = 6; key.shadow.camera.bottom = -6;
    const rim = new THREE.DirectionalLight(0x88aaff, 0.7);
    rim.position.set(-4, 2, -4);
    scene.add(hemi, key, rim);

    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.045).texture;

    const grid = new THREE.GridHelper(10, 20, 0x555b66, 0x2a2e36);
    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(50, 50),
      new THREE.MeshStandardMaterial({color: 0x1b1d22, roughness: 0.95, metalness: 0}));
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    ground.visible = false;
    scene.add(grid, ground);

    const transform = new TransformControls(camera, renderer.domElement);
    transform.addEventListener("dragging-changed", ev => { controls.enabled = !ev.value; });
    transform.addEventListener("objectChange", () => {
      const sk = S && S.selectedSocket && S.model.sockets.find(s => s.name === S.selectedSocket);
      if (!sk) return;
      const obj = S.socketObjects.get(sk.name);
      sk.position = [obj.position.x, obj.position.y, obj.position.z];
      const e = obj.rotation;
      sk.rotation = [THREE.MathUtils.radToDeg(e.x), THREE.MathUtils.radToDeg(e.y), THREE.MathUtils.radToDeg(e.z)];
      markDirty();
      syncSocketInputs(sk);
    });
    scene.add(transform.getHelper());

    const selectionBox = new THREE.BoxHelper(new THREE.Object3D(), 0x59e0c0);
    selectionBox.visible = false;
    scene.add(selectionBox);

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();

    // corner orientation gizmo — a second scene/camera, scissor-rendered into
    // the top-right corner after the main pass. See renderNavCube().
    const navScene = new THREE.Scene();
    const navCamera = new THREE.OrthographicCamera(-1.6, 1.6, 1.6, -1.6, 0.1, 10);
    buildNavCube(navScene);

    S.three = {
      hostEl, renderer, scene, camera, controls, grid, ground, transform,
      selectionBox, raycaster, pointer, navScene, navCamera, pmrem,
      root: null, mixer: null, action: null, gltfAnimations: [],
      clock: new THREE.Clock(), dirty: true, rafId: 0,
    };

    renderer.domElement.addEventListener("click", onCanvasClick);
    controls.addEventListener("change", requestRender);
    const ro = new ResizeObserver(() => resizeThree());
    ro.observe(hostEl);
    S.three.resizeObserver = ro;

    resizeThree();
    startLoop();
  }

  function buildNavCube(navScene){
    const axes = [
      {dir: [1, 0, 0], color: 0xe0524a}, {dir: [0, 1, 0], color: 0x7fd08a},
      {dir: [0, 0, 1], color: 0x5aa9e6},
    ];
    for (const a of axes) {
      const dir = new THREE.Vector3(...a.dir);
      const geo = new THREE.CylinderGeometry(0.03, 0.03, 1, 8);
      geo.translate(0, 0.5, 0);
      const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({color: a.color}));
      mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir);
      navScene.add(mesh);
      const cap = new THREE.Mesh(new THREE.SphereGeometry(0.09, 12, 12),
        new THREE.MeshBasicMaterial({color: a.color}));
      cap.position.copy(dir);
      navScene.add(cap);
    }
    navScene.add(new THREE.Mesh(new THREE.SphereGeometry(0.06, 10, 10),
      new THREE.MeshBasicMaterial({color: 0x8a8f9c})));
  }

  function renderNavCube(){
    const t = S.three, el = t.hostEl;
    const w = el.clientWidth, h = el.clientHeight;
    if (!w || !h) return;
    const size = Math.round(84 * Math.min(window.devicePixelRatio || 1, 2));
    const margin = Math.round(10 * Math.min(window.devicePixelRatio || 1, 2));
    t.navCamera.position.set(0, 0, 3).applyQuaternion(t.camera.quaternion);
    t.navCamera.up.copy(t.camera.up);
    t.navCamera.lookAt(0, 0, 0);
    t.renderer.setScissorTest(true);
    const rw = t.renderer.domElement.width, rh = t.renderer.domElement.height;
    t.renderer.setViewport(rw - size - margin, rh - size - margin, size, size);
    t.renderer.setScissor(rw - size - margin, rh - size - margin, size, size);
    t.renderer.clearDepth();
    t.renderer.render(t.navScene, t.navCamera);
    t.renderer.setScissorTest(false);
    t.renderer.setViewport(0, 0, rw, rh);
  }

  function resizeThree(){
    if (!S || !S.three) return;
    const el = S.three.hostEl;
    const w = Math.max(1, el.clientWidth), h = Math.max(1, el.clientHeight);
    S.three.renderer.setSize(w, h, false);
    S.three.camera.aspect = w / h;
    S.three.camera.updateProjectionMatrix();
    requestRender();
  }

  function requestRender(){ if (S && S.three) S.three.dirty = true; }

  function startLoop(){
    const t = S.three;
    const tick = () => {
      if (!S || S.three !== t) return; // torn down / replaced
      t.rafId = requestAnimationFrame(tick);
      if (!visible($.stage)) return; // workspace tab is hidden — do nothing
      const dt = t.clock.getDelta();
      let animating = false;
      if (t.controls.update()) animating = true; // damping in flight
      if (t.mixer && S.playing) { t.mixer.update(dt); animating = true; }
      if (S.model.display.autorotate && t.root) { t.root.rotation.y += dt * 0.4; animating = true; }
      if (animating || t.dirty) {
        t.dirty = false;
        t.renderer.setScissorTest(false);
        t.renderer.render(t.scene, t.camera);
        renderNavCube();
        updateHud();
      }
    };
    t.rafId = requestAnimationFrame(tick);
  }

  function updateHud(){
    if (!$.stage || !S) return;
    let hud = $.stage.querySelector(".me-hud");
    if (!hud) {
      hud = document.createElement("div");
      hud.className = "me-hud";
      $.stage.appendChild(hud);
    }
    hud.textContent = `${S.stats.tris.toLocaleString()} triangles · ` +
      `${S.stats.nodes} nodes · ${S.stats.materials} materials\n${S.bytes ? (S.bytes / 1024).toFixed(0) + " KB" : ""}`;
  }

  function onCanvasClick(ev){
    if (!S || !S.three || !S.three.root) return;
    const t = S.three;
    const rect = t.hostEl.getBoundingClientRect();
    t.pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
    t.pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
    t.raycaster.setFromCamera(t.pointer, t.camera);
    const hits = t.raycaster.intersectObject(t.root, true)
      .filter(h => h.object.visible && h.object.isMesh && !h.object.userData._isSocketMarker);
    if (!hits.length) return;
    const hit = hits[0];
    if (S.tool === "socket") {
      placeSocketAt(hit);
    } else {
      const nodeName = nearestNamedAncestor(hit.object);
      if (nodeName) selectNode(nodeName);
    }
  }

  function nearestNamedAncestor(obj){
    let cur = obj;
    while (cur) {
      if (cur.name && S.nodeIndex.has(cur.name)) return cur.name;
      cur = cur.parent;
    }
    return obj.name || null;
  }

  function placeSocketAt(hit){
    const name = S.placeSlot;
    const nodeName = nearestNamedAncestor(hit.object);
    const local = S.three.root.worldToLocal(hit.point.clone());
    let sk = S.model.sockets.find(s => s.name === name);
    if (!sk) {
      sk = {name, node: nodeName, position: [local.x, local.y, local.z], rotation: [0, 0, 0], note: ""};
      S.model.sockets.push(sk);
    } else {
      sk.node = nodeName;
      sk.position = [local.x, local.y, local.z];
    }
    S.selectedSocket = name;
    markDirty();
    rebuildSocketObjects();
    attachGizmoToSelectedSocket();
    renderSide();
  }

  function rebuildSocketObjects(){
    if (!S || !S.three) return;
    for (const obj of S.socketObjects.values()) {
      S.three.root && S.three.root.remove(obj);
      obj.traverse(c => { c.geometry && c.geometry.dispose(); c.material && c.material.dispose(); });
    }
    S.socketObjects.clear();
    if (!S.three.root) return;
    for (const sk of S.model.sockets) buildSocketObject(sk);
    requestRender();
  }

  function buildSocketObject(sk){
    const group = new THREE.Group();
    group.name = "__socket_" + sk.name;
    const color = socketColor(sk.name);
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.035, 12, 12),
      new THREE.MeshBasicMaterial({color, depthTest: false}));
    head.renderOrder = 999;
    // A socket marker is not part of the mesh being shaded — it must stay
    // this bright dot in every display mode. Without this flag
    // applyDisplayMode()'s traversal (which walks the WHOLE root, markers
    // included) would swap in a wireframe/normals material here too, and
    // switching back to "shaded" would set .material to undefined — there is
    // no _origMaterial to restore because this mesh never had one.
    head.userData._isSocketMarker = true;
    const axes = new THREE.AxesHelper(0.14);
    axes.renderOrder = 999;
    axes.material.depthTest = false;
    group.add(head, axes);
    syncGroupFromSocket(group, sk);
    S.three.root.add(group);
    S.socketObjects.set(sk.name, group);
  }

  function syncGroupFromSocket(group, sk){
    group.position.set(sk.position[0], sk.position[1], sk.position[2]);
    group.rotation.set(
      THREE.MathUtils.degToRad(sk.rotation[0]),
      THREE.MathUtils.degToRad(sk.rotation[1]),
      THREE.MathUtils.degToRad(sk.rotation[2]));
  }
  function syncSocketObject(sk){
    const obj = S.socketObjects.get(sk.name);
    if (obj) syncGroupFromSocket(obj, sk);
    requestRender();
  }

  function attachGizmoToSelectedSocket(){
    if (!S || !S.three) return;
    const obj = S.selectedSocket && S.socketObjects.get(S.selectedSocket);
    if (obj) S.three.transform.attach(obj);
    else S.three.transform.detach();
    requestRender();
  }

  function highlightSelection(){
    if (!S || !S.three) return;
    const box = S.three.selectionBox;
    if (S.selectedNode) {
      const obj = S.nodeIndex.get(S.selectedNode);
      if (obj) { box.setFromObject(obj); box.visible = true; }
    } else box.visible = false;
    requestRender();
  }

  // ── loading a model into the scene ───────────────────────────────────
  function loadIntoScene(){
    disposeCurrentModel();
    const url = S.rawUrl;
    if (S.ext === ".obj") {
      const mtlUrl = url.replace(/\.obj$/i, ".mtl");
      new MTLLoader().load(mtlUrl, mats => {
        mats.preload();
        new OBJLoader().setMaterials(mats).load(url, onLoaded, undefined, () => loadObjBare(url));
      }, undefined, () => loadObjBare(url));
    } else {
      new GLTFLoader()
        .setDRACOLoader(dracoLoader)
        .setKTX2Loader(ktx2Loader)
        .load(url, gltf => onLoaded(gltf.scene, gltf.animations), undefined, onLoadError);
    }
  }
  function loadObjBare(url){
    new OBJLoader().load(url, onLoaded, undefined, onLoadError);
  }
  function onLoadError(err){
    say("could not load the model geometry: " + (err && err.message || err), "err");
    $.stage.querySelector(".me-loading")?.remove();
  }

  function onLoaded(root, animations){
    if (!S || !S.three) return;
    S.three.root = root;
    S.three.scene.add(root);
    root.traverse(o => {
      if (o.isMesh) {
        o.castShadow = true; o.receiveShadow = true;
        o.userData._origMaterial = o.material;
        const geo = o.geometry;
        const tris = geo.index ? geo.index.count / 3 : (geo.attributes.position ? geo.attributes.position.count / 3 : 0);
        o.userData._tris = Math.round(tris);
      }
    });
    indexNodes(root);
    computeStats(root);
    S.three.gltfAnimations = animations || [];
    S.clips = S.three.gltfAnimations.map(c => c.name || "clip");
    if (S.clips.length) {
      S.three.mixer = new THREE.AnimationMixer(root);
      S.activeClipName = S.clips[0];
      S.three.action = S.three.mixer.clipAction(S.three.gltfAnimations[0]);
      S.three.action.play();
      S.playing = true;
    }
    sizeGroundAndGrid(root);
    applyNodeOverrides();
    applyDisplayMode();
    applyDisplayToggles();
    rebuildSocketObjects();
    if (S.model.camera) restoreCamera(); else fit();
    renderSide();
  }

  function indexNodes(root){
    S.nodeIndex.clear();
    root.traverse(o => {
      if (o.name && (o.isMesh || o.isGroup || o.isBone)) S.nodeIndex.set(o.name, o);
    });
  }

  function computeStats(root){
    let tris = 0, nodes = 0, mats = new Set();
    root.traverse(o => {
      nodes++;
      if (o.isMesh) {
        tris += o.userData._tris || 0;
        const m = o.material;
        (Array.isArray(m) ? m : [m]).forEach(mm => mm && mats.add(mm.uuid));
      }
    });
    S.stats = {tris, nodes, materials: mats.size};
  }

  function sizeGroundAndGrid(root){
    const box = new THREE.Box3().setFromObject(root);
    const size = box.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z, 0.5);
    const span = Math.max(2, Math.ceil(maxDim * 4));
    const t = S.three;
    t.scene.remove(t.grid);
    t.grid.geometry.dispose();
    t.grid = new THREE.GridHelper(span, Math.min(40, span * 2), 0x555b66, 0x2a2e36);
    t.grid.visible = S.model.display.grid;
    t.scene.add(t.grid);
    t.ground.scale.setScalar(span / 50 * 5);
    t.ground.position.y = box.min.y;
    t.camera.near = Math.max(0.001, maxDim / 1000);
    t.camera.far = Math.max(1000, maxDim * 200);
    t.camera.updateProjectionMatrix();
  }

  function disposeCurrentModel(){
    if (!S || !S.three || !S.three.root) return;
    S.three.transform.detach();
    S.three.selectionBox.visible = false;
    for (const obj of S.socketObjects.values()) {
      obj.traverse(c => { c.geometry && c.geometry.dispose(); c.material && c.material.dispose(); });
    }
    S.socketObjects.clear();
    S.three.root.traverse(o => {
      if (o.isMesh) {
        o.geometry && o.geometry.dispose();
        const dispose = m => { if (!m) return; Object.values(m).forEach(v => v && v.isTexture && v.dispose()); m.dispose(); };
        Array.isArray(o.material) ? o.material.forEach(dispose) : dispose(o.material);
      }
    });
    S.three.scene.remove(S.three.root);
    if (S.three.mixer) S.three.mixer.stopAllAction();
    S.three.root = null; S.three.mixer = null; S.three.action = null;
    S.nodeIndex.clear();
  }

  // ── display application ───────────────────────────────────────────────
  function altMaterial(mesh, mode){
    let cache = S.matCache.get(mesh);
    if (!cache) { cache = {}; S.matCache.set(mesh, cache); }
    if (cache[mode]) return cache[mode];
    let mat;
    if (mode === "wireframe") mat = new THREE.MeshBasicMaterial({color: 0x9fd0ff, wireframe: true});
    else if (mode === "normals") mat = new THREE.MeshNormalMaterial();
    else if (mode === "unlit") {
      const orig = mesh.userData._origMaterial;
      const src = Array.isArray(orig) ? orig[0] : orig;
      mat = new THREE.MeshBasicMaterial({
        color: (src && src.color) ? src.color.clone() : 0xffffff,
        map: (src && src.map) || null,
      });
    }
    cache[mode] = mat;
    return mat;
  }

  function applyDisplayMode(){
    if (!S || !S.three || !S.three.root) return;
    const mode = S.model.display.mode;
    S.three.root.traverse(o => {
      if (!o.isMesh || o.userData._isSocketMarker) return;
      o.material = mode === "shaded" ? o.userData._origMaterial : altMaterial(o, mode);
    });
    applyNodeOverrides(); // tint/opacity ride on top of whichever material is active
    requestRender();
  }

  function applyDisplayToggles(){
    if (!S || !S.three) return;
    S.three.grid.visible = S.model.display.grid;
    S.three.ground.visible = S.model.display.ground;
    S.three.scene.background = new THREE.Color(S.model.display.background || 0x14161b);
    requestRender();
  }

  function applyNodeOverrides(){
    if (!S || !S.three || !S.three.root) return;
    for (const [name, obj] of S.nodeIndex) {
      const ov = S.model.nodes[name];
      obj.visible = ov ? ov.visible !== false : true;
      if (!obj.isMesh) continue;
      const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
      for (const m of mats) {
        if (!m) continue;
        if (ov && ov.color && m.color) m.color.set(ov.color);
        if (ov && ov.opacity != null) { m.transparent = ov.opacity < 1; m.opacity = ov.opacity; }
        else { m.opacity = 1; }
      }
    }
    requestRender();
  }

  // ── camera ────────────────────────────────────────────────────────────
  function fit(){
    if (!S || !S.three || !S.three.root) return;
    const t = S.three;
    const box = new THREE.Box3().setFromObject(t.root);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z, 0.1);
    const dist = maxDim * 1.8;
    const dir = new THREE.Vector3(1, 0.75, 1).normalize();
    t.camera.position.copy(center).add(dir.multiplyScalar(dist));
    t.controls.target.copy(center);
    t.controls.update();
    requestRender();
  }

  function restoreCamera(){
    const t = S.three, cam = S.model.camera;
    if (!t || !cam) return;
    t.camera.position.set(cam.position[0], cam.position[1], cam.position[2]);
    t.camera.fov = cam.fov;
    t.camera.updateProjectionMatrix();
    t.controls.target.set(cam.target[0], cam.target[1], cam.target[2]);
    t.controls.update();
    requestRender();
  }

  function saveCameraBookmark(){
    if (!S || !S.three) return;
    const t = S.three;
    S.model.camera = {
      position: [t.camera.position.x, t.camera.position.y, t.camera.position.z],
      target: [t.controls.target.x, t.controls.target.y, t.controls.target.z],
      fov: t.camera.fov,
    };
    markDirty();
  }

  function teardownThree(){
    if (!S || !S.three) return;
    const t = S.three;
    cancelAnimationFrame(t.rafId);
    t.resizeObserver && t.resizeObserver.disconnect();
    disposeCurrentModel();
    t.transform.dispose();
    t.controls.dispose();
    t.pmrem.dispose();
    t.renderer.dispose();
    t.hostEl.remove();
    S.three = null;
  }

  // ── persistence ───────────────────────────────────────────────────────
  async function save(){
    if (!S) return;
    saveCameraBookmark();
    S.saving = true; S.saveError = null; renderBar();
    try {
      const r = await fetch("/api/model3d/save", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({rel: S.rel, model: S.model}),
      });
      const body = await r.json();
      if (!r.ok) throw new Error((body.error && body.error.message) || r.statusText);
      S.model = body.data.model;
      S.dirty = false;
      S.savedAt = Date.now();
      S.saving = false; S.saveError = null;
      renderBar();
      say("saved " + S.rel, "ok");
    } catch (e) {
      // The corner toast is 2.6 seconds long and the eye is on the canvas.
      // A failed save has to STAY on screen, next to the button that failed.
      S.saving = false;
      S.saveError = String((e && e.message) || e).slice(0, 120);
      renderBar();
      say("could not save: " + (e.message || e), "err");
    }
  }

  async function resetLabels(){
    if (!S) return;
    if (!await confirmAsk({
        title: "Reset all labels?",
        body: "Every socket, node override and note on " + S.name + " will be deleted. The model file itself is never touched.",
        ok: "reset", danger: true}))
      return;
    try {
      const r = await fetch("/api/model3d/reset", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({rel: S.rel}),
      });
      const body = await r.json();
      if (!r.ok) throw new Error((body.error && body.error.message) || r.statusText);
      S.model = body.data.model;
      S.selectedNode = null; S.selectedSocket = null; S.dirty = false;
      S.savedAt = Date.now(); S.saving = false; S.saveError = null;
      applyNodeOverrides(); applyDisplayMode(); applyDisplayToggles(); rebuildSocketObjects();
      renderChrome();
      say("labels reset", "ok");
    } catch (e) {
      say("could not reset: " + (e.message || e), "err");
    }
  }

  async function snapshot(){
    if (!S || !S.three || !S.three.root) { say("open a model first", "err"); return; }
    const t = S.three;
    t.renderer.setScissorTest(false);
    t.renderer.render(t.scene, t.camera);
    const png = t.renderer.domElement.toDataURL("image/png");
    try {
      const r = await fetch("/api/model3d/snapshot", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({rel: S.rel, png}),
      });
      const body = await r.json();
      if (!r.ok) throw new Error((body.error && body.error.message) || r.statusText);
      say("preview saved to " + body.data.preview, "ok");
    } catch (e) {
      say("could not save the snapshot: " + (e.message || e), "err");
    }
  }

  // ── Studio / rail integration ─────────────────────────────────────────
  function embed(host, rel){
    _host = host || null;
    injectStyle();
    if (rel) return open(rel);
    if (host) host.innerHTML =
      '<div class="me-land">' +
        '<div class="me-land-in">' +
          '<h3>3D model viewer</h3>' +
          '<p>Choose a .glb, .gltf or .obj to look at it from every angle, check its shading and topology, ' +
            'and mark where gear attaches. Everything you label here saves back to the project.</p>' +
          `<button class="qbtn" onclick="ModelEdit.pick()">${I("model")} open a model…</button>` +
        '</div></div>';
    return null;
  }
  function unembed(){ if (S) { teardownThree(); S = null; } _host = null; }

  function activate(){
    const host = document.getElementById("me-page");
    if (!host) return false;
    _host = host;
    injectStyle();
    if (!S) { embed(host); return true; }
    if ($.back && $.back.parentNode !== host) {
      $.back.classList.add("me-embed");
      host.innerHTML = "";
      host.appendChild($.back);
      if (S.three) { $.stage.appendChild(S.three.hostEl); resizeThree(); }
    }
    return true;
  }

  /* THE STEP AFTER "SAVED", shared with the sprite editor and the audio lab so
     that learning it in one teaches it in all three. A .glb cannot be wired
     into a scene directly — Godot imports it as a PackedScene and the thing a
     level instances is that scene — so for a model the panel's first exit is
     two steps: deliver into the engine (local, free, godot_deliver_asset), then
     instance the scene it wrote. The second exit files a work item that already
     names the model, the target scene and the Atlas references. */
  function handoff(){
    if (!window.Handoff){ say("the handoff panel did not load", true); return; }
    Handoff.fromEditor(S, {
      editor: "model",
      meta: {
        dirty: !!(S && S.dirty),
        sockets: (S && S.model && S.model.sockets || []).map(s => s.name).filter(Boolean),
      },
    });
  }

  return {
    open, close, pick, pickSearch, closePick, fit, save, resetLabels, snapshot,
    handoff,
    embed, unembed, activate,
    setTool, toggleDisplay, setDisplayMode, setBackground,
    selectNode, toggleNode, nodeColor, nodeOpacity,
    selectSocket, deleteSocket, renameSocket, socketAxis, socketNote,
    setClip, togglePlay, scrub, notesField,
    get state(){ return S; },
  };
})();
