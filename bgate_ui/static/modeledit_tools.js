/* modeledit_tools.js — the draft-to-asset column in the 3D viewer.
 *
 * WHY THESE FIVE TOOLS AND NOT A MODELLING PACKAGE. bgate_adapters/krea.py
 * says what a generated mesh actually is: "geometry and texture with NO RIG,
 * so it is a draft that still owes the pipeline a CLEAN, a SCALE, an
 * ORIENTATION and a SKELETON before it is an asset", and generate_3d's own
 * success payload hands the caller the same list as next_steps. Every one of
 * those was already a Blender tool. None of them was in the one surface that
 * had the model open, so the operator's loop was: look at the mesh here,
 * decide it is wrong, go somewhere else to say so, come back and look again.
 * This column is those steps, next to the thing they are about.
 *
 * IT MEASURES SERVER-SIDE AND PREVIEWS CLIENT-SIDE, and the split is not
 * arbitrary. three.js can count the triangles it built, but a shell count, a
 * non-manifold edge, an inverted face and an unapplied object scale are
 * properties of the AUTHORED mesh, and GLTFLoader has already flattened or
 * dropped all four by the time anything here can see them. So the numbers come
 * from Blender (/api/model3d/inspect) and the live ghost — a scale, a quarter
 * turn, an origin shift on the loaded root — is drawn in the browser, where it
 * costs nothing and can be taken back. Nothing is written until "bake", which
 * is one headless Blender round trip for every pending step at once.
 *
 * IT NEVER OVERWRITES BY ACCIDENT. A bake writes <stem>.baked.glb beside the
 * draft. Replacing the original is a tick box, and the server copies the
 * original into .bgate_out/model_backups before it does.
 *
 * IT DOES NOT OWN modeledit.js AND MUST NOT NEED TO. It attaches through that
 * module's public surface (window.ModelEdit.state, .open(), .toggleDisplay())
 * and mounts a column of its own into .me-body — the same shape bible_refs.js
 * uses against the World bible, and for the same reason: renderChrome()
 * rewrites that subtree whenever the model changes, so this remounts itself
 * rather than asking anyone to call it.
 *
 * Registered as window.ModelTools.
 */
import * as THREE from "three";
import { TransformControls } from "/static/vendor/three/examples/jsm/controls/TransformControls.js";

/* ── A BUG IN A FILE I DO NOT OWN, SHIMMED HERE ───────────────────────────
 *
 * modeledit.js's teardownThree() calls S.three.transform.dispose(), and the
 * vendored TransformControls.dispose() ends with `this.traverse(...)`. In this
 * build TransformControls extends Controls, not Object3D — there is no
 * traverse — so dispose() throws TypeError every time. teardownThree is called
 * from open(), BEFORE the next model loads, so the exception meant THE VIEWER
 * COULD ONLY EVER OPEN ONE MODEL PER PAGE LOAD: the second pick threw, the
 * loader never ran, and the first mesh stayed on screen looking like the
 * picker had simply been ignored.
 *
 * Reproduced at /static/vendor/three/examples/jsm/controls/TransformControls.js
 * line 539 against modeledit.js line 1220.
 *
 * THIS IS A WORKAROUND, NOT THE FIX. The fix belongs in one of those two
 * files and both are owned elsewhere right now; disposing through the helper
 * (getHelper()/_root, which IS an Object3D) is what upstream does. Patching
 * the prototype from here fixes it for modeledit.js's own picker too, which
 * is the point — a shim that only rescued this panel's buttons would leave
 * the bug in place everywhere a human actually meets it.
 */
(() => {
  const proto = TransformControls && TransformControls.prototype;
  if (!proto || typeof proto.traverse === "function") return;
  const broken = proto.dispose;
  proto.dispose = function(){
    try { this.disconnect(); } catch (e) { /* already disconnected */ }
    const root = (typeof this.getHelper === "function" && this.getHelper()) ||
                 this._root || null;
    if (root && typeof root.traverse === "function") {
      root.traverse(child => {
        if (child.geometry) child.geometry.dispose();
        if (child.material) child.material.dispose();
      });
    }
  };
  proto.dispose._bgateShimFor = broken;
})();

window.ModelTools = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const I = (n, s) => (window.BGIcon ? BGIcon(n, {size: s || 14}) : "");
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const confirmAsk = o => (window.askConfirm ? askConfirm(o)
    : Promise.resolve(confirm(o.body || o.title || "")));

  // The project's unit convention, stated once. _blender_base.py:
  // BG_HUMAN_HEIGHT = 1.8, "glTF is metres; Godot agrees." The server echoes
  // it back on every inspect so this list is a convenience, not a second
  // source of truth.
  const REFS = [
    {label: "adult",   m: 1.80},
    {label: "door",    m: 2.10},
    {label: "waist",   m: 1.00},
    {label: "crate",   m: 0.60},
    {label: "mug",     m: 0.12},
  ];
  const BUDGETS = [2000, 8000, 20000, 60000];
  const FACE = ["front", "right", "back", "left"];

  // A model big enough that a Blender round trip is a wait rather than a
  // blink does not get measured until asked. 1.08s for a 3.5 MB draft, and
  // it climbs with the file.
  const AUTO_INSPECT_MAX_BYTES = 24 * 1024 * 1024;
  const TICK_MS = 400;

  const LS_OPEN = "bgate.modeltools.open";

  let host = null;                     // my column, inside .me-body
  let shownRel = "";                   // the model the panel is describing
  let insp = null, inspBusy = false, inspErr = "";
  let bakeBusy = false, bakeErr = "", baked = null;
  let viewerBox = null;                // what three.js measured, instantly
  let overlay = null;                  // everything I added to their scene
  let base = null;                     // root TRS before any preview
  let open = localStorage.getItem(LS_OPEN) !== "0";
  let showRef = true;
  let plan = blankPlan();
  let timer = 0;

  // The skeleton chain. Four separate Blender/Godot calls that only make
  // sense in this order, each holding its own last answer so the panel can
  // show all four states at once — which is the point: "it bound" and "the
  // weights are clean" and "it survives being bent" are three different
  // questions and a rig can pass any one of them and fail the next.
  let rigKind = "humanoid", rigBudget = 0;
  let tpl = null;
  let rigged = null, rigBusy = false, rigErr = "";
  let weights = null, wBusy = false, wErr = "";
  let flex = null, fBusy = false, fErr = "";
  let retarget = null, rtBusy = false, rtErr = "";
  let blOpened = null, blBusy = false, blErr = "";
  let engine = null, enBusy = false, enErr = "";

  function blankPlan(){
    return {weld: false, join: false, decimate: 0, height: 0,
            turns: 0, origin: "keep", replace: false};
  }
  const planEmpty = () => !plan.weld && !plan.join && !plan.decimate &&
    !plan.height && !plan.turns && plan.origin === "keep";

  const ME = () => (window.ModelEdit && window.ModelEdit.state) || null;
  const num = (v, d) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : (d || 0);
  };
  const m3 = v => (num(v, 0)).toFixed(3);
  const K = n => Number(n || 0).toLocaleString();

  /* ── style ─────────────────────────────────────────────────────────────
     Colour and radius are tokens only. The panels themselves are .spanel +
     .sec-h out of app.css rather than a sixth private panel treatment — the
     complaint that block answers ("each section has the same looking
     everything", and every surface inventing its own) is exactly what a new
     column in an existing editor is in a position to make worse. */
  function injectStyle(){
    if (document.getElementById("mt-style")) return;
    const s = document.createElement("style");
    s.id = "mt-style";
    s.textContent = [
      ".mt-col{width:328px;flex:none;min-width:0;display:flex;flex-direction:column;",
        "background:var(--surface-1);border-left:1px solid var(--line);overflow:hidden}",
      ".mt-col.mt-shut{width:40px}",

      /* the column's own header band, on the same ramp .sec-h uses */
      ".mt-bar{display:flex;align-items:center;gap:var(--s-3);flex:none;",
        "padding:var(--s-4) var(--s-4);background:var(--surface-4);",
        "border-bottom:1px solid var(--line)}",
      ".mt-bar h4{margin:0;flex:1;min-width:0;font-family:var(--mono);",
        "font-size:var(--fs-2xs);font-weight:var(--fw-semi);letter-spacing:var(--track-label);",
        "text-transform:uppercase;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      ".mt-col.mt-shut .mt-bar{padding:var(--s-4) 0;justify-content:center}",
      ".mt-col.mt-shut .mt-bar h4,.mt-col.mt-shut .mt-body{display:none}",
      ".mt-tab{writing-mode:vertical-rl;font-family:var(--mono);font-size:var(--fs-3xs);",
        "letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text-3);",
        "padding:var(--s-5) 0;text-align:center;cursor:pointer;display:none}",
      ".mt-col.mt-shut .mt-tab{display:block}",

      ".mt-body{flex:1;min-height:0;overflow-y:auto;padding:var(--s-4);",
        "display:flex;flex-direction:column;gap:var(--s-4)}",
      ".mt-col .spanel{padding:var(--s-4);border-radius:var(--r-sm)}",
      ".mt-col .spanel > .sec-h:first-child{margin:calc(-1 * var(--s-4)) calc(-1 * var(--s-4)) var(--s-4);",
        "padding:var(--s-3) var(--s-4);border-radius:var(--r-sm) var(--r-sm) 0 0}",
      ".mt-col .sec-h{gap:var(--s-3);min-height:0;padding-bottom:var(--s-3);margin-bottom:var(--s-4)}",

      /* a readout is a two-column list of label -> number, and nothing else */
      ".mt-grid{display:grid;grid-template-columns:1fr auto;gap:var(--s-2) var(--s-4);align-items:baseline}",
      ".mt-k{font-size:var(--fs-2xs);color:var(--text-3);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".mt-v{font-family:var(--mono);font-size:var(--fs-xs);color:var(--text);",
        "text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}",
      ".mt-v.bad{color:var(--bad)}",
      ".mt-v.warn{color:var(--warn)}",
      ".mt-v.good{color:var(--good)}",
      ".mt-v.dim{color:var(--text-3)}",
      ".mt-note{font-size:var(--fs-2xs);color:var(--text-3);line-height:var(--lh);margin-top:var(--s-3)}",
      ".mt-note.bad{color:var(--bad)}",
      ".mt-note.warn{color:var(--warn)}",
      ".mt-rule{height:1px;background:var(--line);margin:var(--s-4) calc(-1 * var(--s-4))}",

      ".mt-row{display:flex;align-items:center;gap:var(--s-3);flex-wrap:wrap}",
      ".mt-row + .mt-row{margin-top:var(--s-3)}",
      ".mt-col input[type=number],.mt-col input[type=text]{flex:1;min-width:0;width:100%;",
        "background:var(--surface-3);border:1px solid var(--line);border-radius:var(--r-xs);",
        "color:var(--text);font-family:var(--mono);font-size:var(--fs-xs);padding:var(--s-2) var(--s-3)}",
      ".mt-col input:focus{outline:none;border-color:var(--accent)}",
      ".mt-col label{display:flex;align-items:center;gap:var(--s-3);font-size:var(--fs-2xs);color:var(--text-2);cursor:pointer}",
      ".mt-col .qbtn{flex:none}",
      ".mt-seg{display:flex;gap:var(--s-2);flex-wrap:wrap}",
      ".mt-unit{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);flex:none}",

      /* the bake result: two columns of the same measurements, before / after */
      ".mt-ba{display:grid;grid-template-columns:1fr auto auto;gap:var(--s-2) var(--s-4);align-items:baseline}",
      ".mt-ba .h{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);",
        "text-transform:uppercase;color:var(--text-3);text-align:right}",
      ".mt-ba .b{font-family:var(--mono);font-size:var(--fs-xs);color:var(--text-3);text-align:right;font-variant-numeric:tabular-nums}",
      ".mt-ba .a{font-family:var(--mono);font-size:var(--fs-xs);color:var(--text);text-align:right;font-variant-numeric:tabular-nums}",
      ".mt-ba .a.good{color:var(--good)}",
      ".mt-steps{margin-top:var(--s-3);display:flex;flex-direction:column;gap:var(--s-2)}",
      ".mt-step{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-2);line-height:var(--lh-tight)}",
      ".mt-busy{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);letter-spacing:var(--track-label);text-transform:uppercase}",

      /* the deform test's contact sheet: six poses, small, in the panel that
         asked for them - a render you have to go and find is a render nobody
         looks at, which is how a torn shoulder ships */
      ".mt-shots{display:grid;grid-template-columns:1fr 1fr;gap:var(--s-3);margin-top:var(--s-3)}",
      ".mt-shot{margin:0;border:1px solid var(--line);border-radius:var(--r-sm);overflow:hidden;background:var(--surface-3)}",
      ".mt-shot img{display:block;width:100%;height:82px;object-fit:contain;background:var(--surface-2)}",
      ".mt-shot figcaption{display:flex;flex-direction:column;gap:1px;padding:var(--s-2) var(--s-3);",
        "font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-2);line-height:var(--lh-tight)}",
      ".mt-shot figcaption span{color:var(--text-3)}",
      ".mt-shot figcaption span.bad{color:var(--bad)}",
    ].join("");
    document.head.appendChild(s);
  }

  /* ── mount / remount ───────────────────────────────────────────────────
     renderChrome() replaces .me-body wholesale every time a model opens, so
     "mount once" is not a thing that is available here. The tick re-inserts
     the column if it has gone, which is cheap because the common case is one
     DOM lookup that finds it already in place. */
  function ensureMounted(body){
    if (host && host.parentNode === body) return;
    injectStyle();
    if (!host) {
      host = document.createElement("div");
      host.className = "mt-col";
      host.id = "mt-col";
      host.addEventListener("click", onClick);
      host.addEventListener("change", onChange);
      host.addEventListener("input", onInput);
    }
    const side = body.querySelector(".me-side");
    body.insertBefore(host, side || null);
    render();
  }

  function detach(){
    dropOverlay();
    revertPreview();
    if (host && host.parentNode) host.parentNode.removeChild(host);
    shownRel = ""; insp = null; inspErr = ""; baked = null; bakeErr = "";
    viewerBox = null; base = null; plan = blankPlan();
  }

  /* ── the tick ──────────────────────────────────────────────────────────
     modeledit.js fires no events. Four checks at 2.5 Hz is less machinery
     than a MutationObserver on a subtree that is rebuilt wholesale, and it
     also covers the case the observer would miss: the model's geometry
     arriving asynchronously some time after S.rel changed. */
  function tick(){
    const S = ME();
    const body = document.querySelector("#me-back .me-body");
    if (!S || !body || !S.viewable) { if (host) detach(); return; }
    ensureMounted(body);
    if (S.rel !== shownRel) {
      dropOverlay(); revertPreview();
      // Everything resets on a new model EXCEPT a bake report whose output
      // IS the new model. bake() reopens the file it just wrote, so without
      // this the before/after table — the only place the numbers are shown —
      // vanished a tick after it appeared, on every single bake.
      const keepBake = baked && baked.out === S.rel ? baked : null;
      shownRel = S.rel; insp = null; inspErr = ""; baked = keepBake;
      bakeErr = ""; viewerBox = null; base = null; plan = blankPlan();
      rigged = null; rigErr = ""; weights = null; wErr = "";
      flex = null; fErr = ""; retarget = null; rtErr = "";
      blOpened = null; blErr = ""; engine = null; enErr = "";
      render();
      if (!tpl) loadTemplate();
    }
    if (S.three && S.three.root && !viewerBox) {
      viewerBox = readViewerBox(S);
      captureBase(S);
      drawOverlay();
      render();
      if (!insp && !inspBusy && !inspErr &&
          (S.bytes || 0) <= AUTO_INSPECT_MAX_BYTES) measure();
    }
  }

  /* ── what the browser can measure on its own, in one frame ─────────────
     Not a substitute for the Blender pass; it is what fills the panel in the
     half second before that comes back, and it is the frame the ghost is
     drawn in. */
  function readViewerBox(S){
    const box = new THREE.Box3().setFromObject(S.three.root);
    if (box.isEmpty()) return null;
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3());
    return {min: box.min.clone(), max: box.max.clone(), size, centre};
  }

  function captureBase(S){
    const r = S.three.root;
    if (!r) return;
    base = {pos: r.position.clone(), quat: r.quaternion.clone(),
            scale: r.scale.clone()};
  }

  /* ── the ghost ─────────────────────────────────────────────────────────
     Every preview below writes to the loaded root's own transform and
     nothing else. Reparenting it under a wrapper of mine would have been
     tidier to reason about and would also have broken disposeCurrentModel(),
     which removes the root from the SCENE — from a wrapper that is a no-op,
     and the model leaks. */
  function applyPreview(){
    const S = ME();
    if (!S || !S.three || !S.three.root || !base) return;
    const r = S.three.root;
    let k = 1;
    if (plan.height > 0 && curHeight() > 1e-9) k = plan.height / curHeight();
    r.scale.copy(base.scale).multiplyScalar(k);
    r.quaternion.copy(base.quat);
    if (plan.turns) r.rotateY(-Math.PI / 2 * plan.turns);

    r.position.copy(base.pos);
    r.updateMatrixWorld(true);
    if (plan.origin !== "keep") {
      const box = new THREE.Box3().setFromObject(r);
      if (!box.isEmpty()) {
        const c = box.getCenter(new THREE.Vector3());
        r.position.sub(new THREE.Vector3(
          c.x, plan.origin === "centre" ? c.y : box.min.y, c.z));
      }
    }
    r.updateMatrixWorld(true);
    drawOverlay();
    if (S.three) S.three.dirty = true;
  }

  function revertPreview(){
    const S = ME();
    if (!S || !S.three || !S.three.root || !base) return;
    const r = S.three.root;
    r.position.copy(base.pos);
    r.quaternion.copy(base.quat);
    r.scale.copy(base.scale);
    r.updateMatrixWorld(true);
    S.three.dirty = true;
  }

  /* The height the tools reason about: Blender's if it has answered, the
     viewer's box until then. They agree to five decimals on every model
     tested; the viewer's is simply available first. */
  function curHeight(){
    if (insp && insp.measure) return num(insp.measure.height, 0);
    return viewerBox ? viewerBox.size.y : 0;
  }

  /* ── overlay: the reference figure, the front arrow, the origin cross ───
     Created lazily, and every geometry and material it makes is disposed in
     dropOverlay. modeledit.js is already leaking one WebGLRenderer per model
     opened; a second leak from the panel bolted onto it would be worse, not
     equal. */
  function ensureOverlay(scene){
    if (overlay) return overlay;
    overlay = new THREE.Group();
    overlay.name = "bgate_modeltools_overlay";
    overlay.renderOrder = 3;
    scene.add(overlay);
    return overlay;
  }

  function dropOverlay(){
    if (!overlay) return;
    overlay.traverse(o => {
      if (o.geometry) o.geometry.dispose();
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      mats.forEach(m => m && m.dispose && m.dispose());
    });
    if (overlay.parent) overlay.parent.remove(overlay);
    overlay = null;
  }

  function boxLines(w, h, d, colour){
    const g = new THREE.BoxGeometry(w, h, d);
    const edges = new THREE.EdgesGeometry(g);
    g.dispose();
    const mat = new THREE.LineBasicMaterial({
      color: colour, transparent: true, opacity: 0.75, depthTest: false});
    return new THREE.LineSegments(edges, mat);
  }

  function drawOverlay(){
    const S = ME();
    if (!S || !S.three || !S.three.root) return;
    dropOverlay();
    if (!showRef) { S.three.dirty = true; return; }
    const g = ensureOverlay(S.three.scene);

    S.three.root.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(S.three.root);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const span = Math.max(size.x, size.y, size.z, 0.2);

    // The reference figure: a box the target height, stood beside the model
    // so "is this thing person-sized" is a look rather than a calculation.
    const refH = plan.height > 0 ? plan.height : 1.8;
    const ref = boxLines(0.42, refH, 0.26, 0x6adfc0);
    ref.position.set(box.max.x + Math.max(0.35, span * 0.22),
                     box.min.y + refH / 2, 0);
    g.add(ref);

    // The model's own extent, as a wire box. This is the number the panel is
    // printing, drawn in the place it was measured.
    const own = boxLines(Math.max(size.x, 1e-4), Math.max(size.y, 1e-4),
                         Math.max(size.z, 1e-4), 0x5aa9e6);
    own.position.copy(box.getCenter(new THREE.Vector3()));
    g.add(own);

    // FRONT. glTF and Godot both face -Z, and a generated mesh arrives
    // facing anywhere; this is the arrow the orient buttons are turning.
    const arrow = new THREE.ArrowHelper(
      new THREE.Vector3(0, 0, -1), new THREE.Vector3(0, 0.02, 0),
      Math.max(span * 0.75, 0.4), 0xe0a83c,
      Math.max(span * 0.18, 0.1), Math.max(span * 0.09, 0.05));
    arrow.line.material.depthTest = false;
    arrow.cone.material.depthTest = false;
    g.add(arrow);

    // ORIGIN. Where the file's own zero is, relative to the mesh around it.
    const dot = new THREE.Mesh(
      new THREE.SphereGeometry(Math.max(span * 0.018, 0.008), 12, 8),
      new THREE.MeshBasicMaterial({color: 0xe0524a, depthTest: false}));
    g.add(dot);
    S.three.dirty = true;
  }

  /* ── server calls ──────────────────────────────────────────────────────
     Plain fetch on purpose. bgate_ui/app.py injects a shim that wraps
     window.fetch and stamps X-Bgate-Token onto every same-origin request, so
     a second token path here would be a copy of a mechanism that already
     works — and one that goes stale the day the header name changes. */
  async function getJSON(url){
    const r = await fetch(url);
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(((b.error || {}).message) || r.statusText);
    return b.data || b;
  }

  async function postJSON(url, body){
    const r = await fetch(url, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)});
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(((b.error || {}).message) || r.statusText);
    return b.data || b;
  }

  async function measure(refresh){
    const S = ME();
    if (!S || inspBusy) return;
    inspBusy = true; inspErr = ""; render();
    try {
      insp = await getJSON("/api/model3d/inspect?rel=" +
        encodeURIComponent(S.rel) + (refresh ? "&refresh=1" : ""));
      if (!plan.height && insp.measure) plan.height = 0;
    } catch (e) {
      inspErr = String((e && e.message) || e).slice(0, 200);
    }
    inspBusy = false;
    render();
  }

  async function bake(){
    const S = ME();
    if (!S || bakeBusy || planEmpty()) return;
    if (plan.replace && !await confirmAsk({
        title: "Replace " + S.name + "?",
        body: "The original is copied into .bgate_out/model_backups first, " +
              "but every reference to this file will see the new geometry.",
        ok: "replace", danger: true})) return;
    bakeBusy = true; bakeErr = ""; baked = null; render();
    try {
      const ops = {weld: plan.weld, join: plan.join,
                   decimate: plan.decimate, height: plan.height,
                   turns: plan.turns, origin: plan.origin};
      baked = await postJSON("/api/model3d/bake",
        {rel: S.rel, ops, replace: !!plan.replace});
      revertPreview();
      plan = blankPlan();
      insp = null; viewerBox = null;
      say("baked to " + baked.out, "ok");
      // Reopening is the honest way to show the result: it is a different
      // file and the viewer's own loader has to read it, exactly as Godot
      // will. shownRel changes, so the tick re-measures from scratch.
      if (window.ModelEdit) ModelEdit.open(baked.out);
    } catch (e) {
      bakeErr = String((e && e.message) || e).slice(0, 300);
      say("the bake failed: " + bakeErr, "err");
    }
    bakeBusy = false;
    render();
  }

  /* ── the skeleton chain ────────────────────────────────────────────────
     Four calls in a fixed order, each one a local Blender or a local Godot
     and none of them costing anything. They are separate buttons rather than
     one "rig it" because they answer separate questions and the middle two
     are the ones people skip: a bind that reported success and was never
     bent is the failure this pipeline actually ships. */
  async function loadTemplate(){
    try { tpl = await getJSON("/api/model3d/rig_template"); render(); }
    catch (e) { tpl = null; }
  }

  async function fitRig(){
    const S = ME();
    if (!S || rigBusy) return;
    if (!await confirmAsk({
        title: "Fit a skeleton to " + S.name + "?",
        body: "Blender adopts the mesh (weld, scale to " + m3(rigHeight()) +
              " m, orient), fits the " + ((tpl && tpl.bone_count) || 23) +
              "-bone humanoid template and binds it. Writes a new " +
              "<name>.rigged.glb. This can take a few minutes on a heavy draft.",
        ok: "fit"})) return;
    rigBusy = true; rigErr = ""; rigged = null; render();
    try {
      rigged = await postJSON("/api/model3d/rig",
        {rel: S.rel, kind: rigKind, height: rigHeight(), budget: rigBudget});
      say(rigged.rigged ? ("bound " + rigged.bones + " bones") : "the bind did not take", rigged.rigged ? "ok" : "err");
    } catch (e) {
      rigErr = String((e && e.message) || e).slice(0, 300);
      say("the rig failed: " + rigErr, "err");
    }
    rigBusy = false; render();
  }

  async function runWeights(){
    const S = ME();
    if (!S || wBusy) return;
    wBusy = true; wErr = ""; weights = null; render();
    try { weights = await postJSON("/api/model3d/weights", {rel: S.rel}); }
    catch (e) { wErr = String((e && e.message) || e).slice(0, 300); }
    wBusy = false; render();
  }

  async function runFlex(){
    const S = ME();
    if (!S || fBusy) return;
    fBusy = true; fErr = ""; flex = null; render();
    try { flex = await postJSON("/api/model3d/flex", {rel: S.rel, render: true}); }
    catch (e) { fErr = String((e && e.message) || e).slice(0, 300); }
    fBusy = false; render();
  }

  /* GODOT'S OWN NUMBERS. Worth having next to Blender's because the importer
     applies its own scene scale and its own material handling, so the AABB and
     triangle count the GAME runs with are not always the ones the mesh was
     authored with — and when they differ, the engine's are the true ones. */
  async function runEngine(){
    const S = ME();
    if (!S || enBusy) return;
    enBusy = true; enErr = ""; engine = null; render();
    try { engine = await getJSON("/api/model3d/engine_view?rel=" + encodeURIComponent(S.rel)); }
    catch (e) { enErr = String((e && e.message) || e).slice(0, 300); }
    enBusy = false; render();
  }

  async function runRetarget(){
    const S = ME();
    if (!S || rtBusy) return;
    rtBusy = true; rtErr = ""; retarget = null; render();
    try { retarget = await getJSON("/api/model3d/retarget?rel=" + encodeURIComponent(S.rel)); }
    catch (e) { rtErr = String((e && e.message) || e).slice(0, 300); }
    rtBusy = false; render();
  }

  /* ── the escape hatch ──────────────────────────────────────────────────
     Every check above can tell you a mesh is broken and none of them can fix
     a torn shoulder. Blender is already a hard dependency — doctor gates on
     it, every blender_* tool shells out to it — so this is a local process
     launch, not a new anything, and it is the difference between a diagnosis
     and a cure. */
  async function openInBlender(){
    const S = ME();
    if (!S || blBusy) return;
    if (!await confirmAsk({
        title: "Open " + S.name + " in Blender?",
        body: "Blender starts with this file imported. It opens the file the " +
              "viewer is showing - if you baked or rigged, that is the new " +
              "one. Anything you save there lands on disk, NOT in this " +
              "session: reopen the model here afterwards or you will be " +
              "looking at the old copy.",
        ok: "open Blender"})) return;
    blBusy = true; blErr = ""; blOpened = null; render();
    try {
      blOpened = await postJSON("/api/model3d/open_in_blender", {rel: S.rel});
      say("Blender is starting with " + S.name, "ok");
    } catch (e) {
      blErr = String((e && e.message) || e).slice(0, 300);
      say(blErr, "err");
    }
    blBusy = false; render();
  }

  // The height the skeleton is fitted at: whatever the SCALE tool settled on,
  // falling back to what the mesh actually measures and then to the project
  // default. Typing it twice is how a rig ends up at a different scale from
  // the mesh it was fitted to.
  function rigHeight(){
    if (plan.height > 0) return plan.height;
    const h = curHeight();
    return h > 0.05 ? h : ((tpl && tpl.default_height) || 1.8);
  }

  /* ── events ────────────────────────────────────────────────────────────*/
  function onClick(ev){
    const b = ev.target.closest("[data-mt]");
    if (!b) return;
    const act = b.getAttribute("data-mt");
    const val = b.getAttribute("data-v");
    if (act === "shut") { open = !open; localStorage.setItem(LS_OPEN, open ? "1" : "0"); render(); return; }
    if (act === "measure") { measure(true); return; }
    if (act === "ref") { showRef = !showRef; drawOverlay(); render(); return; }
    if (act === "height") { plan.height = Number(val); applyPreview(); render(); return; }
    if (act === "turns") {
      plan.turns = Number(val) % 4;
      const S = ME();
      // A preview angle that is being spun by the autorotate toggle is not a
      // preview of anything. Turn it off through their own API rather than
      // reaching into the state object.
      if (S && S.model && S.model.display.autorotate && window.ModelEdit)
        ModelEdit.toggleDisplay("autorotate");
      applyPreview(); render(); return;
    }
    if (act === "origin") { plan.origin = val; applyPreview(); render(); return; }
    if (act === "decimate") { plan.decimate = Number(val); render(); return; }
    if (act === "weld") { plan.weld = !plan.weld; render(); return; }
    if (act === "join") { plan.join = !plan.join; render(); return; }
    if (act === "reset") { plan = blankPlan(); revertPreview(); drawOverlay(); render(); return; }
    if (act === "bake") { bake(); return; }
    if (act === "open") { if (window.ModelEdit) ModelEdit.open(val); return; }
    if (act === "kind") { rigKind = val; render(); return; }
    if (act === "rigbudget") { rigBudget = Number(val); render(); return; }
    if (act === "fit") { fitRig(); return; }
    if (act === "weights") { runWeights(); return; }
    if (act === "flex") { runFlex(); return; }
    if (act === "retarget") { runRetarget(); return; }
    if (act === "blender") { openInBlender(); return; }
    if (act === "engine") { runEngine(); return; }
  }

  function onChange(ev){
    const el = ev.target.closest("[data-mtf]");
    if (!el) return;
    const f = el.getAttribute("data-mtf");
    if (f === "replace") { plan.replace = !!el.checked; render(); }
  }

  function onInput(ev){
    const el = ev.target.closest("[data-mtf]");
    if (!el) return;
    const f = el.getAttribute("data-mtf");
    if (f === "height") { plan.height = Math.max(0, num(el.value, 0)); applyPreview(); paintScale(); }
    if (f === "decimate") { plan.decimate = Math.max(0, Math.round(num(el.value, 0))); paintClean(); }
  }

  /* ── render ────────────────────────────────────────────────────────────*/
  function render(){
    if (!host) return;
    // EVERY BUTTON IN HERE REBUILDS THE WHOLE COLUMN, and the column is
    // taller than the viewport. Without this, pressing "check" on the weights
    // panel throws the reader back to the top of the inspect readout and they
    // have to find their place again to read the answer they just asked for.
    const body0 = host.querySelector(".mt-body");
    const keepScroll = body0 ? body0.scrollTop : 0;
    host.classList.toggle("mt-shut", !open);
    host.innerHTML =
      '<div class="mt-bar">' +
        '<h4>draft to asset</h4>' +
        `<button class="qbtn ghost small" data-mt="shut" title="${open ? "collapse" : "expand"}">${open ? "›" : "‹"}</button>` +
      '</div>' +
      '<div class="mt-tab" data-mt="shut">draft to asset</div>' +
      '<div class="mt-body">' +
        secInspect() + secScale() + secOrient() + secOrigin() +
        secClean() + secBake() +
        secRig() + secWeights() + secFlex() + secRetarget() + secBlender() +
      '</div>';
    const body1 = host.querySelector(".mt-body");
    if (body1 && keepScroll) body1.scrollTop = keepScroll;
  }

  function panel(kind, icon, title, badge, badgeKind, body, actions){
    return `<section class="spanel ${kind}">` +
      '<div class="sec-h">' + I(icon, 13) +
        `<h4 class="sec-t">${E(title)}</h4>` +
        `<span class="sec-n ${badgeKind || ""}">${E(badge || "")}</span>` +
        (actions ? `<div class="sec-a">${actions}</div>` : "") +
      '</div>' + body + '</section>';
  }

  const row = (k, v, cls) =>
    `<div class="mt-k">${E(k)}</div><div class="mt-v ${cls || ""}">${E(v)}</div>`;

  /* 1. INSPECT ───────────────────────────────────────────────────────────*/
  function secInspect(){
    const S = ME();
    const act = `<button class="qbtn ghost small" data-mt="measure" ${inspBusy ? "disabled" : ""}>` +
      (inspBusy ? "measuring…" : "measure") + "</button>";
    if (inspErr)
      return panel("k-read", "qa", "inspect", "", "bad",
        `<div class="mt-note bad">${E(inspErr)}</div>`, act);
    if (!insp) {
      const b = viewerBox;
      const body = b
        ? '<div class="mt-grid">' +
            row("triangles", K(S && S.stats ? S.stats.tris : 0)) +
            row("height", m3(b.size.y) + " m") +
            row("width × depth", m3(b.size.x) + " × " + m3(b.size.z)) +
          '</div><div class="mt-note">' +
          (inspBusy ? "Blender is counting shells, non-manifold edges and inverted faces…"
                    : "the viewer's own numbers - press measure for shells, non-manifold edges, UVs and the skeleton") +
          '</div>'
        : '<div class="mt-busy">waiting for the geometry…</div>';
      return panel("k-read", "verify", "inspect", "", "", body, act);
    }

    const m = insp.measure, t = insp;
    const watertight = m.shells === 1 && m.nonmanifold === 0;
    const badge = watertight ? "clean" : "draft";
    const sk = (t.skeletons || [])[0];
    const img = (t.images || [])[0];
    const body = '<div class="mt-grid">' +
      row("triangles", K(m.tris)) +
      row("vertices", K(m.verts)) +
      row("meshes", K(m.meshes) + (t.objects > m.meshes ? " / " + K(t.objects) + " objects" : "")) +
      row("shells", K(m.shells), m.shells > 1 ? "warn" : "good") +
      row("non-manifold", K(m.nonmanifold), m.nonmanifold ? "warn" : "good") +
      row("inverted faces", K(t.flipped_faces), t.flipped_faces ? "bad" : "good") +
      row("n-gons", K(m.ngons), m.ngons ? "warn" : "dim") +
      '</div><div class="mt-rule"></div><div class="mt-grid">' +
      row("bounding box", m.dims.map(v => v.toFixed(2)).join(" × ") + " m") +
      row("height", m3(m.height) + " m") +
      row("origin sits", originWord(m)) +
      '</div><div class="mt-rule"></div><div class="mt-grid">' +
      row("materials", K(t.materials)) +
      row("UV layers", t.no_uv_meshes ? (t.no_uv_meshes + " mesh(es) have none") : "on every mesh",
          t.no_uv_meshes ? "bad" : "good") +
      row("textures", t.image_count ? (t.image_count + " · " + (img ? img.w + "×" + img.h : "")) : "none",
          t.image_count ? "" : "warn") +
      row("skeleton", sk ? (sk.bones + " bones") : "none", sk ? "good" : "warn") +
      (sk ? row("deform bones", K(sk.deform_bones)) : "") +
      row("vertex groups", K(t.vertex_groups), t.vertex_groups ? "" : "dim") +
      row("animations", K(t.animations), t.animations ? "" : "dim") +
      '</div>' +
      (watertight ? "" :
        `<div class="mt-note warn">${K(m.shells)} disconnected shell(s) and ${K(m.nonmanifold)} non-manifold edge(s) - not watertight. Weld below before decimating.</div>`) +
      (t.facing && t.facing.verdict
        ? `<div class="mt-note">front: ${E(t.facing.verdict)}</div>` : "") +
      `<div class="mt-note">${insp.cached ? "cached" : "measured"} by Blender in ${num(insp.seconds, 0).toFixed(2)}s</div>`;
    return panel("k-read", "verify", "inspect", badge,
                 watertight ? "good" : "warn", body, act);
  }

  function originWord(m){
    const o = m.origin_in_box || [0, 0, 0];
    const h = num(m.height, 0);
    const dy = num(o[1], 0);             // origin height above the mesh floor
    const off = Math.hypot(num(o[0], 0), num(o[2], 0));
    if (h < 1e-6) return "-";
    if (Math.abs(dy) < h * 0.02 && off < h * 0.02) return "at the feet";
    // A negative reading is the mesh FLOATING above its own zero, which is a
    // different defect from an origin buried in the chest and reads terribly
    // as "-65% up the model".
    if (dy < 0) return m3(-dy) + " m below the mesh";
    return Math.round(dy / h * 100) + "% up the model";
  }

  /* 2. SCALE TO UNIT ─────────────────────────────────────────────────────*/
  function secScale(){
    const h = curHeight();
    const k = plan.height > 0 && h > 1e-9 ? plan.height / h : 1;
    const body =
      '<div class="mt-grid" id="mt-scale-read">' + scaleRead(h, k) + '</div>' +
      '<div class="mt-row" style="margin-top:var(--s-3)">' +
        `<input type="number" step="0.01" min="0" data-mtf="height" value="${plan.height || ""}" placeholder="${h ? h.toFixed(3) : "target height"}">` +
        '<span class="mt-unit">m</span>' +
      '</div>' +
      '<div class="mt-row mt-seg">' +
        REFS.map(r => `<button class="qbtn ghost small ${plan.height === r.m ? "on" : ""}" data-mt="height" data-v="${r.m}">${E(r.label)} ${r.m}</button>`).join("") +
        `<button class="qbtn ghost small ${!plan.height ? "on" : ""}" data-mt="height" data-v="0">leave</button>` +
      '</div>' +
      '<div class="mt-note">The project is metres and an adult is 1.8 (BG_HUMAN_HEIGHT). Godot agrees.</div>';
    return panel("k-list", "fit", "scale to unit",
                 plan.height ? "×" + k.toFixed(3) : "", plan.height ? "warn" : "",
                 body);
  }
  function scaleRead(h, k){
    return row("measured height", h ? m3(h) + " m" : "-") +
      row("target", plan.height ? m3(plan.height) + " m" : "unchanged",
          plan.height ? "good" : "dim") +
      row("factor", plan.height ? "× " + k.toFixed(4) : "-",
          plan.height ? "good" : "dim");
  }
  function paintScale(){
    const el = document.getElementById("mt-scale-read");
    if (!el) return;
    const h = curHeight();
    el.innerHTML = scaleRead(h, plan.height > 0 && h > 1e-9 ? plan.height / h : 1);
  }

  /* 3. ORIENT ────────────────────────────────────────────────────────────*/
  function secOrient(){
    const f = (insp && insp.facing) || null;
    const a = (insp && insp.axes) || null;
    const body =
      '<div class="mt-seg">' +
        FACE.map((lbl, i) =>
          `<button class="qbtn ghost small ${plan.turns === i ? "on" : ""}" data-mt="turns" data-v="${i}" ` +
          `title="the side now facing -Z becomes the front">${E(lbl)}</button>`).join("") +
      '</div>' +
      '<div class="mt-grid" style="margin-top:var(--s-3)">' +
        row("quarter turns", plan.turns ? plan.turns + " (" + (plan.turns * 90) + "°)" : "none",
            plan.turns ? "good" : "dim") +
        (f ? row("readable front", f.confident ? "yes" : "no", f.confident ? "good" : "warn") : "") +
        (a && a.certainty != null ? row("axis certainty", (num(a.certainty, 0) * 100).toFixed(1) + "%",
            num(a.certainty, 0) < 0.15 ? "warn" : "") : "") +
      '</div>' +
      (f && f.verdict ? `<div class="mt-note ${f.confident ? "" : "warn"}">${E(f.verdict)}</div>` : "") +
      '<div class="mt-note">The amber arrow is -Z, which is forward in glTF and in Godot. Pick the label of the side that should end up pointing along it.</div>';
    return panel("k-list", "redo", "orient",
                 plan.turns ? plan.turns * 90 + "°" : "", plan.turns ? "warn" : "",
                 body);
  }

  /* 4. ORIGIN / PIVOT ────────────────────────────────────────────────────*/
  function secOrigin(){
    const m = insp && insp.measure;
    const body =
      '<div class="mt-seg">' +
        [["feet", "feet / base"], ["centre", "centre"], ["keep", "leave"]].map(([v, lbl]) =>
          `<button class="qbtn ghost small ${plan.origin === v ? "on" : ""}" data-mt="origin" data-v="${v}">${E(lbl)}</button>`).join("") +
      '</div>' +
      '<div class="mt-grid" style="margin-top:var(--s-3)">' +
        (m ? row("origin sits", originWord(m), originWord(m) === "at the feet" ? "good" : "warn") : "") +
        (m ? row("offset x y z", (m.origin_in_box || []).map(v => num(v, 0).toFixed(3)).join("  ")) : "") +
        row("will move to", plan.origin === "keep" ? "unchanged" :
            (plan.origin === "feet" ? "footprint centre, on y=0" : "bounding box centre"),
            plan.origin === "keep" ? "dim" : "good") +
      '</div>' +
      '<div class="mt-note">The red dot on the model is the file\'s own zero. A mesh whose origin is in its chest cannot be dropped on a floor tile.</div>';
    return panel("k-list", "place", "origin",
                 plan.origin === "keep" ? "" : plan.origin,
                 plan.origin === "keep" ? "" : "warn", body);
  }

  /* 5. CLEAN ─────────────────────────────────────────────────────────────*/
  function secClean(){
    const m = insp && insp.measure;
    const body =
      '<div class="mt-row">' +
        `<button class="qbtn ghost small ${plan.weld ? "on" : ""}" data-mt="weld">weld shells</button>` +
        `<button class="qbtn ghost small ${plan.join ? "on" : ""}" data-mt="join">join meshes</button>` +
      '</div>' +
      '<div class="mt-row" style="margin-top:var(--s-4)">' +
        `<input type="number" step="500" min="0" data-mtf="decimate" value="${plan.decimate || ""}" placeholder="triangle budget">` +
        '<span class="mt-unit">tris</span>' +
      '</div>' +
      '<div class="mt-row mt-seg">' +
        BUDGETS.map(b => `<button class="qbtn ghost small ${plan.decimate === b ? "on" : ""}" data-mt="decimate" data-v="${b}">${(b / 1000)}k</button>`).join("") +
        `<button class="qbtn ghost small ${!plan.decimate ? "on" : ""}" data-mt="decimate" data-v="0">leave</button>` +
      '</div>' +
      '<div class="mt-grid" id="mt-clean-read" style="margin-top:var(--s-3)">' + cleanRead(m) + '</div>' +
      '<div class="mt-note">Welding merges by a fraction of the mesh\'s own diagonal, so one setting serves a mug and a building. It is what makes a mesh decimatable - non-manifold count, not shell count, is what stalls the collapse.</div>';
    const on = plan.weld || plan.join || plan.decimate;
    return panel("k-list", "eraser", "clean", on ? "on" : "", on ? "warn" : "", body);
  }
  function cleanRead(m){
    return (m ? row("shells now", K(m.shells), m.shells > 1 ? "warn" : "good") : "") +
      (m ? row("non-manifold now", K(m.nonmanifold), m.nonmanifold ? "warn" : "good") : "") +
      (m ? row("triangles now", K(m.tris)) : "") +
      row("budget", plan.decimate ? K(plan.decimate) : "unchanged",
          plan.decimate ? "good" : "dim");
  }
  function paintClean(){
    const el = document.getElementById("mt-clean-read");
    if (el) el.innerHTML = cleanRead(insp && insp.measure);
  }

  /* 6. BAKE ──────────────────────────────────────────────────────────────*/
  function secBake(){
    const S = ME();
    const steps = [];
    if (plan.weld) steps.push("weld");
    if (plan.join) steps.push("join");
    if (plan.decimate) steps.push("decimate to " + K(plan.decimate));
    if (plan.height) steps.push("scale to " + m3(plan.height) + " m");
    if (plan.turns) steps.push("turn " + plan.turns * 90 + "°");
    if (plan.origin !== "keep") steps.push("origin to " + plan.origin);

    let body =
      '<div class="mt-note">' +
        (steps.length
          ? "one Blender pass: " + E(steps.join(", ")) + "."
          : "nothing pending - pick something above.") +
      '</div>' +
      '<div class="mt-row" style="margin-top:var(--s-4)">' +
        `<label><input type="checkbox" data-mtf="replace" ${plan.replace ? "checked" : ""}> replace the original</label>` +
      '</div>' +
      '<div class="mt-note">' +
        (plan.replace
          ? "the original is copied to .bgate_out/model_backups first"
          : "writes " + E(S ? S.name.replace(/\.[^.]+$/, "") + ".baked.glb" : "&lt;name&gt;.baked.glb")) +
      '</div>' +
      '<div class="mt-row" style="margin-top:var(--s-4)">' +
        `<button class="qbtn" data-mt="bake" ${(bakeBusy || planEmpty()) ? "disabled" : ""}>` +
          (bakeBusy ? "baking…" : "bake") + "</button>" +
        `<button class="qbtn ghost small" data-mt="reset" ${planEmpty() ? "disabled" : ""}>clear</button>` +
        `<button class="qbtn ghost small ${showRef ? "on" : ""}" data-mt="ref">guides</button>` +
      '</div>';

    if (bakeErr) body += `<div class="mt-note bad">${E(bakeErr)}</div>`;
    if (baked) body += bakeResult(baked);
    return panel("k-read", "export", "bake", baked ? "done" : "",
                 baked ? "good" : "", body);
  }

  function bakeResult(b){
    const A = b.after || {}, B = b.before || {};
    const line = (k, f) => {
      const bv = f(B), av = f(A);
      return `<div class="mt-k">${E(k)}</div><div class="b">${E(bv)}</div>` +
        `<div class="a ${bv !== av ? "good" : ""}">${E(av)}</div>`;
    };
    return '<div class="mt-rule"></div>' +
      '<div class="mt-ba">' +
        '<div class="mt-k"></div><div class="h">before</div><div class="h">after</div>' +
        line("triangles", m => K(m.tris)) +
        line("vertices", m => K(m.verts)) +
        line("shells", m => K(m.shells)) +
        line("non-manifold", m => K(m.nonmanifold)) +
        line("height", m => m3(m.height)) +
        line("origin", m => (m.origin_in_box || []).map(v => num(v, 0).toFixed(2)).join(" ")) +
      '</div>' +
      '<div class="mt-steps">' +
        (b.steps || []).map(s => `<div class="mt-step">${E(stepLine(s))}</div>`).join("") +
      '</div>' +
      `<div class="mt-note">wrote ${E(b.out)} · ${Math.round((b.bytes || 0) / 1024)} KB · ${num(b.seconds, 0).toFixed(2)}s` +
      (b.backup ? ` · backup ${E(b.backup)}` : "") + '</div>' +
      `<div class="mt-row"><button class="qbtn ghost small" data-mt="open" data-v="${E(b.out)}">open the result</button></div>`;
  }

  /* 7. RIG ───────────────────────────────────────────────────────────────*/
  function hasSkeleton(){
    return !!(insp && (insp.skeletons || []).length);
  }

  function secRig(){
    const S = ME();
    const sk = (insp && (insp.skeletons || [])[0]) || null;
    const kinds = (tpl && tpl.kinds) || ["humanoid", "long", "none"];
    const bones = (tpl && tpl.bone_count) || 23;
    let body = "";

    if (sk) {
      body += '<div class="mt-grid">' +
        row("skeleton", sk.name, "good") +
        row("bones", K(sk.bones), "good") +
        row("deform bones", K(sk.deform_bones)) +
        row("vertex groups", K(insp.vertex_groups),
            insp.vertex_groups ? "good" : "bad") +
        '</div>' +
        '<div class="mt-note">This mesh is already skinned. Check it below - a bind that reported success and was never bent is the one that tears in the engine.</div>';
    } else if (insp) {
      body += `<div class="mt-note warn">No skeleton. This is geometry, not a character - it cannot be animated, retargeted or driven by an AnimationPlayer until one is fitted.</div>`;
    }

    body += '<div class="mt-row mt-seg" style="margin-top:var(--s-4)">' +
      kinds.map(k => `<button class="qbtn ghost small ${rigKind === k ? "on" : ""}" data-mt="kind" data-v="${E(k)}" ` +
        `title="how the adopt step reads this mesh's forward before it turns it">${E(k)}</button>`).join("") +
      '</div>' +
      '<div class="mt-grid" style="margin-top:var(--s-3)">' +
        row("fit at height", m3(rigHeight()) + " m",
            plan.height > 0 ? "good" : "") +
        row("template", bones + " bones") +
        row("decimate first", rigBudget ? K(rigBudget) + " tris" : "no", rigBudget ? "" : "dim") +
      '</div>' +
      '<div class="mt-row mt-seg">' +
        BUDGETS.map(b => `<button class="qbtn ghost small ${rigBudget === b ? "on" : ""}" data-mt="rigbudget" data-v="${b}">${b / 1000}k</button>`).join("") +
        `<button class="qbtn ghost small ${!rigBudget ? "on" : ""}" data-mt="rigbudget" data-v="0">as is</button>` +
      '</div>' +
      '<div class="mt-row" style="margin-top:var(--s-4)">' +
        `<button class="qbtn" data-mt="fit" ${rigBusy ? "disabled" : ""}>` +
          (rigBusy ? "fitting…" : (sk ? "re-fit a skeleton" : "fit a skeleton")) + "</button>" +
      '</div>' +
      '<div class="mt-note">The height comes from the scale tool above, so the skeleton and the mesh cannot end up at two different scales.</div>';

    if (rigErr) body += `<div class="mt-note bad">${E(rigErr)}</div>`;
    if (rigged) {
      const cov = rigged.coverage || {};
      const ad = (rigged.adopt || {});
      body += '<div class="mt-rule"></div><div class="mt-grid">' +
        row("bound", rigged.rigged ? "yes" : "no", rigged.rigged ? "good" : "bad") +
        row("bones", K(rigged.bones), "good") +
        row("method", rigged.bound_with || "-") +
        row("unweighted", K(rigged.unweighted) + " (" + num(rigged.unweighted_pct, 0).toFixed(3) + "%)",
            num(rigged.unweighted_pct, 0) > 1 ? "bad" : "good") +
        (cov.checked != null ? row("bone coverage", cov.found + " / " + cov.checked,
            cov.passed ? "good" : "bad") : "") +
        (ad.weld ? row("welded to", K(ad.weld.shells) + " shells") : "") +
        row("took", num(rigged.seconds, 0).toFixed(1) + "s") +
        '</div>' +
        (rigged.reason ? `<div class="mt-note bad">${E(rigged.reason)}</div>` : "") +
        `<div class="mt-note">wrote ${E(rigged.out || "-")}</div>` +
        (rigged.out ? `<div class="mt-row"><button class="qbtn ghost small" data-mt="open" data-v="${E(rigged.out)}">open the rigged mesh</button></div>` : "");
    }
    return panel("k-list", "rig", "skeleton",
                 sk ? sk.bones + " bones" : (insp ? "none" : ""),
                 sk ? "good" : (insp ? "warn" : ""), body);
  }

  /* 8. WEIGHTS ───────────────────────────────────────────────────────────*/
  function secWeights(){
    const v = weights && weights.verdict;
    const act = `<button class="qbtn ghost small" data-mt="weights" ${(wBusy || !hasSkeleton()) ? "disabled" : ""}>` +
      (wBusy ? "checking…" : "check") + "</button>";
    let body = '<div class="mt-note">One bone should own one connected patch of mesh. Several means the bind heat jumped a gap - a thigh that also owns part of the other thigh - and that only shows when something bends.</div>';
    if (!hasSkeleton())
      return panel("k-read", "verify", "weights", "", "",
        body + '<div class="mt-note warn">Needs a skeleton first.</div>', act);
    if (wErr) body += `<div class="mt-note bad">${E(wErr)}</div>`;
    if (weights) {
      const issues = (v && v.issues) || [];
      body = '<div class="mt-grid">' +
        row("verdict", v && v.passed ? "clean" : "bleeding", v && v.passed ? "good" : "bad") +
        row("deform bones", K(weights.deform_bones)) +
        row("mesh shells", K(weights.mesh_shells), weights.mesh_shells > 1 ? "warn" : "good") +
        row("bones flagged", K(issues.length), issues.length ? "bad" : "good") +
        '</div>' +
        (weights.worst || []).slice(0, 6).map(b =>
          `<div class="mt-grid" style="margin-top:var(--s-2)">` +
          row(b.bone, b.islands + " islands · " + K(b.bleed_vertices) + " bled",
              b.islands > 1 ? "warn" : "good") + '</div>').join("") +
        (issues.length ? `<div class="mt-note warn">${E(issues[0].note || "")}</div>` : "") +
        `<div class="mt-note">${num(weights.seconds, 0).toFixed(1)}s</div>` + body;
    }
    return panel("k-read", "verify", "weights",
                 weights ? (v && v.passed ? "clean" : "bleed") : "",
                 weights ? (v && v.passed ? "good" : "bad") : "", body, act);
  }

  /* 9. DEFORM TEST ───────────────────────────────────────────────────────*/
  function secFlex(){
    const act = `<button class="qbtn ghost small" data-mt="flex" ${(fBusy || !hasSkeleton()) ? "disabled" : ""}>` +
      (fBusy ? "bending…" : "bend it") + "</button>";
    let body = '<div class="mt-note">Six joints driven to a real angle, measured for volume loss, pinching and new self-intersections, and rendered. The only check that turns a bad bind from a statistic into something you can see.</div>';
    if (!hasSkeleton())
      return panel("k-read", "animation", "deform test", "", "",
        body + '<div class="mt-note warn">Needs a skeleton first.</div>', act);
    if (fErr) body += `<div class="mt-note bad">${E(fErr)}</div>`;
    if (flex) {
      const v = flex.verdict || {};
      body = '<div class="mt-grid">' +
        row("verdict", v.passed ? "holds up" : "fails", v.passed ? "good" : "bad") +
        row("poses", K((flex.poses || []).length)) +
        row("issues", K((v.issues || []).length), (v.issues || []).length ? "bad" : "good") +
        '</div>' +
        '<div class="mt-shots">' + (flex.poses || []).map(p =>
          (p.render_url
            ? `<figure class="mt-shot"><img src="${E(p.render_url)}" alt="${E(p.label)}">` +
              `<figcaption>${E(p.label)}<span class="${num(p.volume_ratio, 1) < 0.9 || num(p.new_self_pairs, 0) > 500 ? "bad" : ""}">` +
              `vol ${(num(p.volume_ratio, 1) * 100).toFixed(1)}% · ${K(p.new_self_pairs)} new hits</span></figcaption></figure>`
            : "")).join("") + '</div>' +
        ((v.issues || []).length ? `<div class="mt-note bad">${E(v.issues[0].note || "")}</div>` : "") +
        `<div class="mt-note">${num(flex.seconds, 0).toFixed(1)}s</div>` + body;
    }
    return panel("k-read", "animation", "deform test",
                 flex ? ((flex.verdict || {}).passed ? "holds" : "fails") : "",
                 flex ? ((flex.verdict || {}).passed ? "good" : "bad") : "",
                 body, act);
  }

  /* 10. ENGINE ───────────────────────────────────────────────────────────*/
  function secRetarget(){
    const act =
      `<button class="qbtn ghost small" data-mt="engine" ${enBusy ? "disabled" : ""}>` +
        (enBusy ? "reading…" : "measure") + "</button>" +
      `<button class="qbtn ghost small" data-mt="retarget" ${rtBusy ? "disabled" : ""}>` +
        (rtBusy ? "asking Godot…" : "retarget") + "</button>";
    let body = '<div class="mt-note">The last gate: will Godot\'s own retargeter map this skeleton onto its humanoid profile. Only answerable once the file is inside the game project.</div>';
    if (enErr) body += `<div class="mt-note bad">${E(enErr)}</div>`;
    if (engine && engine.available) {
      const e = engine.engine || {}, sz = e.size_check || {};
      body = '<div class="mt-grid">' +
        row("godot triangles", K(e.total_tris)) +
        row("godot size", (sz.metres || []).map(v => num(v, 0).toFixed(2)).join(" × ") + " m") +
        row("longest axis", m3(sz.longest_axis_m) + " m", sz.ok ? "good" : "bad") +
        row("suggested scale", "× " + num(sz.suggested_scale, 1).toFixed(3),
            num(sz.suggested_scale, 1) === 1 ? "good" : "warn") +
        row("skeletons", K(e.skeleton_count), e.skeleton_count ? "good" : "warn") +
        '</div>' + body;
    } else if (engine) {
      body = `<div class="mt-note warn">${E(engine.reason || "not available")}</div>` + body;
    }
    if (rtErr) body += `<div class="mt-note bad">${E(rtErr)}</div>`;
    if (retarget && !retarget.available)
      body = `<div class="mt-note warn">${E(retarget.reason || "not available")}</div>` + body;
    else if (retarget) {
      const r = retarget.report || {};
      body = '<div class="mt-grid">' +
        row("retargetable", r.retargetable ? "yes" : "no", r.retargetable ? "good" : "bad") +
        row("skeleton", r.skeleton || "-") +
        row("bones", K(r.skeleton_bones)) +
        row("mapped", K((r.mapped || []).length || r.mapped || 0)) +
        row("missing", K((r.missing || []).length), (r.missing || []).length ? "warn" : "good") +
        row("essential missing", K((r.essential_missing || []).length),
            (r.essential_missing || []).length ? "bad" : "good") +
        row("chain", r.chain_ok ? "ok" : "broken", r.chain_ok ? "good" : "bad") +
        '</div>' +
        (r.error ? `<div class="mt-note bad">${E(r.error)}</div>` : "") + body;
    }
    return panel("k-read", "gate", "engine check",
                 retarget && retarget.available
                   ? (((retarget.report || {}).retargetable) ? "ok" : "no") : "",
                 retarget && retarget.available
                   ? (((retarget.report || {}).retargetable) ? "good" : "bad") : "",
                 body, act);
  }

  /* 11. OPEN IN BLENDER ──────────────────────────────────────────────────*/
  function secBlender(){
    const S = ME();
    let body =
      '<div class="mt-note">Everything above can tell you a mesh is broken. ' +
      'None of it can repaint a weight. Blender is already required by this ' +
      'project, so this opens the file the viewer is showing - including a ' +
      'bake or a rig you just made - with nothing generated and nothing spent.</div>' +
      '<div class="mt-grid" style="margin-top:var(--s-3)">' +
        row("will open", S ? S.name : "-") +
        row("edits land", "in the file, not here", "warn") +
      '</div>' +
      '<div class="mt-row" style="margin-top:var(--s-4)">' +
        `<button class="qbtn ghost" data-mt="blender" ${blBusy ? "disabled" : ""}>` +
          (blBusy ? "launching…" : "open in Blender") + "</button>" +
      '</div>';
    if (blErr) body += `<div class="mt-note bad">${E(blErr)}</div>`;
    if (blOpened) body += '<div class="mt-note">' +
      `Blender ${E(String(blOpened.pid))} started` +
      (blOpened.imported ? " and imported the mesh" : "") +
      '. Reopen the model here when you have saved.</div>';
    return panel("k-doc", "edit", "open in Blender", "", "", body);
  }

  function stepLine(s){
    switch (s.op) {
      case "weld": return `weld · ${s.shells_before} → ${s.shells_after} shells, ${s.nonmanifold_after} non-manifold`;
      case "join": return `join · ${s.was} meshes → 1`;
      case "decimate": return `decimate · ${K(s.tris_now)} tris (budget ${K(s.budget)})`;
      case "scale": return `scale · ${num(s.from_height, 0).toFixed(3)} → ${num(s.to_height, 0).toFixed(3)} m (×${num(s.factor, 0).toFixed(4)})`;
      case "orient": return `orient · turned ${s.turned_deg}°`;
      case "origin": return `origin · ${s.mode}, moved ${(s.moved || []).map(v => num(v, 0).toFixed(3)).join(" ")}`;
      case "bake": return `bake · into ${s.into}${s.note ? " (" + s.note + ")" : ""}`;
      default: return s.op || "";
    }
  }

  /* ── boot ──────────────────────────────────────────────────────────────*/
  function start(){
    if (timer) return;
    injectStyle();
    timer = setInterval(tick, TICK_MS);
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", start);
  else start();

  return {
    start, tick, measure, bake,
    get plan(){ return plan; },
    get inspection(){ return insp; },
    get rig(){ return {kind: rigKind, budget: rigBudget}; },
    get result(){ return baked; },
    stop(){ if (timer) { clearInterval(timer); timer = 0; } detach(); },
  };
})();
