/* modeledit_bones.js — the skeleton, as something you can move.
 *
 * WHAT THIS EXISTS TO UNDO. blender.rig fits a FIXED template: HUMANOID_BONES
 * is 23 bones and QUADRUPED_BONES its four-legged counterpart. It measures the
 * mesh and places that template as well as measurement can, which on a
 * generated draft is usually right and occasionally puts a knee two
 * centimetres into the shin. Until this file, the only answer to "the knee is
 * wrong" was to open Blender — the panel could tell you a skeleton had 23
 * bones and nothing at all about where any of them sat.
 *
 * THE UNIT OF EDITING IS THE JOINT, NOT THE BONE, and that is the whole
 * design. An elbow is the forearm's head AND the upper arm's tail, written to
 * the same coordinate. Move one of the two and the limb comes apart when it
 * bends — the exporter keeps the gap and the engine renders it. The server
 * groups coincident endpoints into joints (modeledit._joints) and this drags
 * joints, so every bone meeting there moves together, always.
 *
 * NOTHING IS WRITTEN UNTIL APPLY, the same contract sceneview3d.js draws for
 * scene edits and the tools column draws for the bake: a drag changes the
 * picture, edits count up in a bar, and one button spends the Blender. The
 * draft is never overwritten; apply writes <stem>.bones.glb beside it.
 *
 * IT DOES NOT OWN THE COLUMN. modeledit_tools.js renders the draft-to-asset
 * chain and rewrites that subtree on every click, so this hands it a panel()
 * string and takes its clicks back through handle(). The 3D half — the bone
 * lines, the joint handles, the gizmo, the picking — lives here because it is
 * bulky and has nothing to do with the rest of that column.
 *
 * Registered as window.BoneEdit.
 */
import * as THREE from "three";
import { TransformControls } from "/static/vendor/three/examples/jsm/controls/TransformControls.js";

window.BoneEdit = (() => {
  "use strict";

  const ME = () => (window.ModelEdit && window.ModelEdit.state) || null;
  const UI = () => (window.ModelTools && window.ModelTools.ui) || null;
  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const num = (v, d) => { const n = Number(v); return Number.isFinite(n) ? n : (d || 0); };
  const confirmAsk = o => (window.askConfirm ? askConfirm(o)
    : Promise.resolve(confirm(o.body || o.title || "")));

  // Bone colour by what it is, so a skeleton reads at a glance rather than as
  // a grey scribble: a bone nothing is weighted to is the interesting one.
  const C_BONE = 0x7fd08a;      // bound and deforming
  const C_IDLE = 0x8a8f9c;      // no vertices weighted to it
  const C_EDIT = 0xe0a83c;      // this bone moves with the staged edit
  const C_JOINT = 0x5aa9e6;
  const C_SEL = 0xe0524a;

  let shownRel = "";
  let sk = null;                  // the /skeleton payload
  let armIndex = 0;
  let busy = false, err = "";
  let applying = false, applyErr = "", result = null;
  let sel = null;                 // selected joint id
  let staged = new Map();         // joint id -> [x, y, z]
  let rebind = true;
  let visible = true;

  // Everything added to somebody else's scene, tracked so it can all come out
  // again: modeledit.js disposes its scene wholesale on the next open() and a
  // stray helper of ours would be a leak it cannot see.
  let group = null, gizmo = null, proxy = null;
  let lines = null, jointMeshes = new Map();
  let jointGeo = null, jointMatIdle = null, jointMatSel = null;
  let picker = null, hostEl = null;

  /* ── the model ─────────────────────────────────────────────────────────*/
  const arm = () => (sk && (sk.armatures || [])[armIndex]) || null;
  const bones = () => (arm() && arm().bones) || [];
  const joints = () => (arm() && arm().joints) || [];
  const jointById = id => joints().find(j => j.id === id) || null;
  const posOf = j => staged.get(j.id) || j.position;

  /* Which joint owns each bone endpoint. Built once per skeleton read; the
     redraw runs on every pointer move of a drag and cannot afford to search. */
  let endJoint = new Map();
  function indexEnds(){
    endJoint = new Map();
    joints().forEach(j => (j.ends || []).forEach(
      e => endJoint.set(e.bone + "|" + e.end, j.id)));
  }
  function endPos(boneRec, end){
    const id = endJoint.get(boneRec.name + "|" + end);
    const j = id ? jointById(id) : null;
    return j ? posOf(j) : (boneRec[end] || [0, 0, 0]);
  }
  const boneMoved = b => ["head", "tail"].some(e => {
    const id = endJoint.get(b.name + "|" + e);
    return id && staged.has(id);
  });

  /* ── the overlay ───────────────────────────────────────────────────────*/
  function ensure(){
    const S = ME();
    if (!S || !S.three) return null;
    if (group && group.parent === S.three.scene) return group;
    group = new THREE.Group();
    group.name = "bgate-skeleton";
    group.renderOrder = 3;
    S.three.scene.add(group);
    return group;
  }

  function drop(){
    const S = ME();
    detachGizmo();
    if (group) {
      group.traverse(o => {
        if (o.geometry && o.geometry !== jointGeo) o.geometry.dispose();
        if (o.material && o.material !== jointMatIdle && o.material !== jointMatSel) {
          (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => m.dispose());
        }
      });
      if (group.parent) group.parent.remove(group);
    }
    group = null; lines = null; jointMeshes = new Map();
    if (picker && hostEl) hostEl.removeEventListener("pointerdown", picker, true);
    picker = null; hostEl = null;
    if (S && S.three) requestRender();
  }

  const requestRender = () => {
    const S = ME();
    if (S && S.three) S.three.dirty = true;
  };

  /* Bones as line segments in ONE geometry, redrawn by writing the position
     buffer rather than rebuilding: a drag repaints this on every pointer move
     and 23 disposed geometries a frame is how a viewport starts stuttering. */
  function build(){
    const g = ensure();
    if (!g) return;
    const bs = bones();
    if (!lines || lines.userData.count !== bs.length) {
      if (lines) { lines.geometry.dispose(); lines.material.dispose(); g.remove(lines); }
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(bs.length * 6), 3));
      geo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(bs.length * 6), 3));
      lines = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({
        vertexColors: true, depthTest: false, transparent: true, opacity: 0.95}));
      lines.userData.count = bs.length;
      lines.renderOrder = 3;
      g.add(lines);
    }
    if (!jointGeo) jointGeo = new THREE.SphereGeometry(1, 10, 8);
    if (!jointMatIdle) jointMatIdle = new THREE.MeshBasicMaterial({
      color: C_JOINT, depthTest: false, transparent: true, opacity: 0.9});
    if (!jointMatSel) jointMatSel = new THREE.MeshBasicMaterial({
      color: C_SEL, depthTest: false});

    const want = new Set(joints().map(j => j.id));
    jointMeshes.forEach((mesh, id) => {
      if (!want.has(id)) { g.remove(mesh); jointMeshes.delete(id); }
    });
    joints().forEach(j => {
      let mesh = jointMeshes.get(j.id);
      if (!mesh) {
        mesh = new THREE.Mesh(jointGeo, jointMatIdle);
        mesh.userData.jointId = j.id;
        mesh.renderOrder = 4;
        g.add(mesh);
        jointMeshes.set(j.id, mesh);
      }
    });
    paint();
    arm_picker();
  }

  /* Joint handles are sized off the MODEL, not in absolute metres. The same
     0.02 m sphere is a grab target on a 1.8 m character and invisible on a
     40 m ship, and this pipeline produces both. */
  function handleSize(){
    const h = num(sk && sk.height, 0);
    return Math.max(0.004, (h || 1.8) * 0.012);
  }

  function paint(){
    if (!lines) return;
    const pos = lines.geometry.getAttribute("position");
    const col = lines.geometry.getAttribute("color");
    const c = new THREE.Color();
    bones().forEach((b, i) => {
      const a = endPos(b, "head"), z = endPos(b, "tail");
      pos.setXYZ(i * 2, a[0], a[1], a[2]);
      pos.setXYZ(i * 2 + 1, z[0], z[1], z[2]);
      c.setHex(boneMoved(b) ? C_EDIT : (b.influences ? C_BONE : C_IDLE));
      col.setXYZ(i * 2, c.r, c.g, c.b);
      col.setXYZ(i * 2 + 1, c.r, c.g, c.b);
    });
    pos.needsUpdate = true; col.needsUpdate = true;
    lines.geometry.computeBoundingSphere();

    const r = handleSize();
    joints().forEach(j => {
      const mesh = jointMeshes.get(j.id);
      if (!mesh) return;
      const p = posOf(j);
      mesh.position.set(p[0], p[1], p[2]);
      mesh.scale.setScalar(j.id === sel ? r * 1.5 : r);
      mesh.material = j.id === sel ? jointMatSel : jointMatIdle;
      mesh.visible = visible;
    });
    if (lines) lines.visible = visible;
    requestRender();
  }

  /* ── picking ───────────────────────────────────────────────────────────*/
  function arm_picker(){
    const S = ME();
    if (!S || !S.three || picker) return;
    hostEl = S.three.renderer.domElement;
    picker = ev => {
      if (!visible || !sk || ev.button !== 0) return;
      const S2 = ME();
      if (!S2 || !S2.three) return;
      const rect = hostEl.getBoundingClientRect();
      const p = new THREE.Vector2(
        ((ev.clientX - rect.left) / rect.width) * 2 - 1,
        -((ev.clientY - rect.top) / rect.height) * 2 + 1);
      const ray = new THREE.Raycaster();
      ray.setFromCamera(p, S2.three.camera);
      const hits = ray.intersectObjects([...jointMeshes.values()], false);
      if (!hits.length) return;
      // Ours, and nobody else's: modeledit.js raycasts the same pointerdown
      // for node and socket selection, and a joint sitting inside the mesh
      // would otherwise select the body behind it too.
      ev.stopPropagation();
      ev.preventDefault();
      select(hits[0].object.userData.jointId);
    };
    // Capture phase, so this runs before the viewer's own picker.
    hostEl.addEventListener("pointerdown", picker, true);
  }

  /* ── the gizmo ─────────────────────────────────────────────────────────*/
  function attachGizmo(){
    const S = ME();
    const j = sel && jointById(sel);
    if (!S || !S.three || !j) return detachGizmo();
    if (!gizmo) {
      gizmo = new TransformControls(S.three.camera, S.three.renderer.domElement);
      gizmo.setMode("translate");
      gizmo.addEventListener("dragging-changed", ev => {
        S.three.controls.enabled = !ev.value;
        // Repaint the panel on drag END only. The readout is worth having and
        // rebuilding the column sixty times a second is not.
        if (!ev.value) render();
      });
      gizmo.addEventListener("objectChange", () => {
        if (!sel || !proxy) return;
        staged.set(sel, [proxy.position.x, proxy.position.y, proxy.position.z]);
        paint();
      });
      S.three.scene.add(gizmo.getHelper());
    }
    if (!proxy) { proxy = new THREE.Object3D(); S.three.scene.add(proxy); }
    const p = posOf(j);
    proxy.position.set(p[0], p[1], p[2]);
    // The viewer's own gizmo drives sockets. Two armed gizmos on one canvas
    // fight for the same drag, so whichever is editing owns it alone.
    if (S.three.transform) S.three.transform.detach();
    gizmo.attach(proxy);
    requestRender();
  }

  function detachGizmo(){
    const S = ME();
    if (gizmo) {
      try { gizmo.detach(); } catch (e) {}
      const helper = typeof gizmo.getHelper === "function" ? gizmo.getHelper() : null;
      if (helper && helper.parent) helper.parent.remove(helper);
      try { gizmo.dispose(); } catch (e) {}
      gizmo = null;
    }
    if (proxy && proxy.parent) proxy.parent.remove(proxy);
    proxy = null;
    if (S && S.three) requestRender();
  }

  function select(id){
    sel = (id === sel) ? null : id;
    paint();
    if (sel) attachGizmo(); else detachGizmo();
    render();
  }

  /* ── server ────────────────────────────────────────────────────────────*/
  async function read(){
    const S = ME();
    if (!S || busy) return;
    busy = true; err = ""; render();
    try {
      const r = await fetch("/api/model3d/skeleton?rel=" + encodeURIComponent(S.rel));
      const b = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(((b.error || {}).message) || r.statusText);
      sk = b.data || b;
      armIndex = 0; sel = null; staged = new Map();
      indexEnds();
      build();
    } catch (e) {
      err = String((e && e.message) || e).slice(0, 300);
      sk = null;
    }
    busy = false; render();
  }

  async function apply(){
    const S = ME();
    if (!S || applying || !staged.size) return;
    const moved = movedBones();
    if (!await confirmAsk({
        title: "Move " + staged.size + " joint" + (staged.size === 1 ? "" : "s") + "?",
        body: (rebind
          ? "Blender writes the new positions into the armature and RE-SOLVES the "
            + "weights around them. Every hand-made weight on this file is replaced. "
          : "Blender writes the new positions and KEEPS the existing weights, which "
            + "were solved around the old ones. Right for a roll or a tail; wrong for "
            + "a moved joint. ")
          + Object.keys(moved).length + " bones change. Writes a new <name>.bones.glb.",
        ok: "apply"})) return;
    applying = true; applyErr = ""; result = null; render();
    try {
      const r = await fetch("/api/model3d/skeleton", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({rel: S.rel, bones: moved, rebind,
                              limit: num(sk && sk.max_move, 0)})});
      const b = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(((b.error || {}).message) || r.statusText);
      result = b.data || b;
      staged = new Map();
      say(result.rebound
        ? ("rebound, " + num(result.unweighted_pct, 0).toFixed(2) + "% unweighted")
        : "joints written, weights kept",
        result.rigged === false ? "err" : "ok");
    } catch (e) {
      applyErr = String((e && e.message) || e).slice(0, 300);
      say("the skeleton edit failed: " + applyErr, "err");
    }
    applying = false; render();
  }

  /* Joint drags expanded into the per-bone head/tail writes the route takes.
     `was` rides along so the server can refuse a slip: the gizmo works in
     world units and one bad drag against a distant camera plane can throw a
     wrist across the room. */
  function movedBones(){
    const out = {};
    staged.forEach((pos, id) => {
      const j = jointById(id);
      if (!j) return;
      (j.ends || []).forEach(e => {
        const rec = bones().find(b => b.name === e.bone);
        if (!rec) return;
        const entry = out[e.bone] || (out[e.bone] = {was: {}});
        entry[e.end] = [+pos[0].toFixed(6), +pos[1].toFixed(6), +pos[2].toFixed(6)];
        entry.was[e.end] = rec[e.end];
      });
    });
    return out;
  }

  /* ── panel ─────────────────────────────────────────────────────────────*/
  const render = () => { if (window.ModelTools) ModelTools.repaint(); };

  function panel(hasSkeleton){
    const ui = UI();
    if (!ui) return "";
    const {panel: box, row} = ui;
    const act = `<button class="qbtn ghost small" data-mt="bonesread" ${(busy || !hasSkeleton) ? "disabled" : ""}>` +
      (busy ? "reading…" : (sk ? "re-read" : "read")) + "</button>";
    let body = '<div class="mt-note">The rig step fits a FIXED template and places it by ' +
      'measurement. Where it put a knee is a guess until somebody looks. Drag a joint to ' +
      'move every bone that meets there; nothing is written until you apply.</div>';

    if (!hasSkeleton)
      return box("k-list", "rig", "joints", "", "",
        body + '<div class="mt-note warn">Needs a skeleton first.</div>', act);
    if (err) body += `<div class="mt-note bad">${E(err)}</div>`;
    if (!sk) return box("k-list", "rig", "joints", "", "", body, act);

    const bs = bones(), js = joints();
    const idle = bs.filter(b => !b.influences).length;
    body = '<div class="mt-grid">' +
      row("bones", String(bs.length), "good") +
      row("joints", String(js.length)) +
      row("moving nothing", String(idle), idle ? "warn" : "good") +
      ((sk.armatures || []).length > 1
        ? row("armature", (arm() || {}).name || "-") : "") +
      '</div>' + body;

    body += '<div class="mt-row mt-seg" style="margin-top:var(--s-3)">' +
      `<button class="qbtn ghost small ${visible ? "on" : ""}" data-mt="bonesshow">` +
        (visible ? "showing" : "hidden") + '</button>' +
      (sel ? '<button class="qbtn ghost small" data-mt="bonesclear">deselect</button>' : "") +
      '</div>';

    const j = sel && jointById(sel);
    if (j) {
      const p = posOf(j), was = j.position;
      const moved = staged.has(j.id);
      body += '<div class="mt-rule"></div><div class="mt-grid">' +
        row("joint", j.label, "good") +
        row("bones here", (j.ends || []).map(e => e.bone + " " + e.end).join(", ")) +
        row("x", p[0].toFixed(4) + " m", moved ? "warn" : "") +
        row("y", p[1].toFixed(4) + " m", moved ? "warn" : "") +
        row("z", p[2].toFixed(4) + " m", moved ? "warn" : "") +
        (moved ? row("moved", (Math.hypot(p[0] - was[0], p[1] - was[1],
                                          p[2] - was[2])).toFixed(4) + " m", "warn") : "") +
        '</div>' +
        (moved ? '<div class="mt-row"><button class="qbtn ghost small" data-mt="bonesreset" ' +
          `data-v="${E(j.id)}">put it back</button></div>` : "");
    } else {
      body += '<div class="mt-note">Click a joint in the viewport, or pick one:</div>' +
        '<div class="mt-seg" style="margin-top:var(--s-2)">' +
        js.slice(0, 40).map(k =>
          `<button class="qbtn ghost small ${staged.has(k.id) ? "on" : ""}" ` +
          `data-mt="bonespick" data-v="${E(k.id)}">${E(k.label)}</button>`).join("") +
        '</div>';
    }

    if (staged.size) {
      const names = Object.keys(movedBones());
      body += '<div class="mt-rule"></div><div class="mt-grid">' +
        row("staged", staged.size + " joint" + (staged.size === 1 ? "" : "s"), "warn") +
        row("bones changed", String(names.length), "warn") +
        '</div>' +
        '<div class="mt-row mt-seg" style="margin-top:var(--s-3)">' +
        `<button class="qbtn ghost small ${rebind ? "on" : ""}" data-mt="bonesrebind" ` +
          'title="re-solve the weights around the new joints; replaces every existing weight">' +
          (rebind ? "rebind" : "keep weights") + '</button>' +
        `<button class="qbtn" data-mt="bonesapply" ${applying ? "disabled" : ""}>` +
          (applying ? "applying…" : "apply") + '</button>' +
        '<button class="qbtn ghost small" data-mt="bonesdiscard">discard</button>' +
        '</div>' +
        (rebind ? "" : '<div class="mt-note warn">These weights were solved around the ' +
          'old joint positions. Keeping them is right for a roll or a tail tidy and ' +
          'wrong for a moved joint.</div>');
    }

    if (applyErr) body += `<div class="mt-note bad">${E(applyErr)}</div>`;
    if (result) {
      body += '<div class="mt-rule"></div><div class="mt-grid">' +
        row("wrote", result.out || "-", "good") +
        row("bones changed", String((result.applied || []).length)) +
        row("furthest move", num(result.max_move, 0).toFixed(4) + " m") +
        (result.rebound
          ? row("rebind", (result.rigged ? "held" : "failed") + " · " +
                num(result.unweighted_pct, 0).toFixed(2) + "% unweighted",
                result.rigged ? "good" : "bad")
          : row("rebind", "skipped, weights kept", "warn")) +
        row("took", num(result.seconds, 0).toFixed(1) + "s") +
        '</div>' +
        ((result.missing || []).length
          ? `<div class="mt-note warn">not in this armature: ${E(result.missing.join(", "))}</div>` : "") +
        (result.out ? '<div class="mt-row"><button class="qbtn ghost small" data-mt="open" ' +
          `data-v="${E(result.out)}">open the edited skeleton</button></div>` : "");
    }

    return box("k-list", "rig", "joints",
               staged.size ? staged.size + " staged" : (sk ? bs.length + " bones" : ""),
               staged.size ? "warn" : "good", body, act);
  }

  /* Clicks the tools column did not recognise. Returns true when handled. */
  function handle(act, val){
    switch (act) {
      case "bonesread": read(); return true;
      case "bonesshow": visible = !visible; paint(); render(); return true;
      case "bonespick": select(val); return true;
      case "bonesclear": select(sel); return true;
      case "bonesrebind": rebind = !rebind; render(); return true;
      case "bonesapply": apply(); return true;
      case "bonesdiscard":
        staged = new Map(); paint();
        if (sel) attachGizmo();
        render(); return true;
      case "bonesreset":
        staged.delete(val); paint();
        if (sel === val) attachGizmo();
        render(); return true;
      default: return false;
    }
  }

  /* The tools column calls this every tick. A new model invalidates every
     coordinate in here — they are positions in a skeleton that is no longer
     loaded — so the whole thing resets rather than drawing a previous
     character's bones over this one. */
  function sync(rel){
    if (rel === shownRel) {
      if (sk && (!group || !group.parent)) build();
      return;
    }
    shownRel = rel;
    drop();
    sk = null; sel = null; staged = new Map(); err = "";
    result = null; applyErr = ""; armIndex = 0;
  }

  return {
    panel, handle, sync, drop,
    get skeleton(){ return sk; },
    get staged(){ return Object.fromEntries(staged); },
    get selected(){ return sel; },
  };
})();
