/* modeledit_pose.js — pose the bones, key them, watch it play.
 *
 * WHAT WAS MISSING. blender_animate authors clips from a vocabulary: a gait
 * kind with overrides, or a "keyed" clip written in twenty character-level
 * fields (lean, hips_up, reach_r). That is the right language for a walk
 * cycle generator and the wrong one for somebody who has just decided this
 * character should point at something. There was no way to rotate a forearm
 * and say "that, at 0.4 seconds".
 *
 * THE POSE IS THE PAYLOAD. This drives the loaded glTF's own skeleton — the
 * same THREE.Bone objects the SkinnedMesh is already skinned to — so posing
 * deforms the actual mesh on screen with no server in the loop. A key is the
 * whole pose: every bone's rotation as a delta from its REST rotation, which
 * is exactly Blender's matrix_basis and exactly what humanpose.posed_clip
 * takes. Scrubbing slerps between keys in the browser, at whatever framerate
 * the viewport manages, because a preview that costs a Blender is a preview
 * nobody uses twice.
 *
 * BAKING IS STILL BLENDER'S JOB. The browser preview is not the clip: the
 * export has to carry proper quaternion tracks, the support gate has to read
 * foot contact off the exported file, and the self-intersection gate has to
 * run. So "bake" posts these keys to /api/model3d/animate as a clip of kind
 * "bones" and everything downstream is unchanged — same gates, same proof
 * sheets, same .anim.glb.
 *
 * REST IS SNAPSHOTTED ONCE, at the moment the model loads, and every key is
 * relative to it. Without that the second pose would be relative to the
 * first and the clip would drift a little further from the character on every
 * key somebody set.
 *
 * Registered as window.PoseEdit. Renders into the draft-to-asset column the
 * same way modeledit_bones.js does.
 */
import * as THREE from "three";
import { TransformControls } from "/static/vendor/three/examples/jsm/controls/TransformControls.js";

window.PoseEdit = (() => {
  "use strict";

  const ME = () => (window.ModelEdit && window.ModelEdit.state) || null;
  const UI = () => (window.ModelTools && window.ModelTools.ui) || null;
  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const num = (v, d) => { const n = Number(v); return Number.isFinite(n) ? n : (d || 0); };
  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const confirmAsk = o => (window.askConfirm ? askConfirm(o)
    : Promise.resolve(confirm(o.body || o.title || "")));

  const C_BONE = 0x8a8f9c, C_SEL = 0xe0a83c, C_POSED = 0x5aa9e6;
  const MAX_KEYS = 120;

  let shownRel = "";
  let armed = false;              // posing mode on
  let skeleton = null;            // THREE.Skeleton from the loaded glTF
  let rest = new Map();           // bone.uuid -> {q: Quaternion, p: Vector3}
  let sel = null;                 // selected THREE.Bone
  let keys = [];                  // [{t, bones: {name: [w,x,y,z]}, root}]
  let at = 0;                     // playhead, seconds
  let clipName = "action", clipLoop = false, clipFps = 30;
  // THE TIMELINE IS NOT THE KEYS. Clamping the playhead to the last key means
  // the first key pins it at zero and no second key can ever be set later than
  // the first: the clip cannot grow past the moment it was started. The length
  // is the caller's, and the keys live inside it.
  let clipLen = 2.0;
  // Has the pose on screen drifted from what the clip evaluates to here? A
  // scrub re-evaluates and would throw the drift away, so it is worth saying
  // out loud rather than discovering afterwards.
  let dirty = false;
  let baking = false, bakeErr = "", baked = null;

  let group = null, gizmo = null, lines = null, handles = new Map();
  let picker = null, hostEl = null;

  const requestRender = () => { const S = ME(); if (S && S.three) S.three.dirty = true; };
  const render = () => { if (window.ModelTools) ModelTools.repaint(); };
  const clipSeconds = () => (keys.length ? keys[keys.length - 1].t : 0);

  /* ── finding the rig the viewer already loaded ─────────────────────────*/
  function findSkeleton(){
    const S = ME();
    if (!S || !S.three || !S.three.root) return null;
    let found = null;
    S.three.root.traverse(o => {
      if (!found && o.isSkinnedMesh && o.skeleton && o.skeleton.bones.length)
        found = o.skeleton;
    });
    return found;
  }

  function snapshotRest(){
    rest = new Map();
    if (!skeleton) return;
    skeleton.bones.forEach(b => rest.set(b.uuid,
      {q: b.quaternion.clone(), p: b.position.clone()}));
  }

  /* A bone's rotation as a DELTA from rest, which is what Blender's
     matrix_basis holds and what posed_clip expects. Absolute local rotations
     would bake the rest pose into every key and double it on import. */
  function deltaOf(bone){
    const r = rest.get(bone.uuid);
    if (!r) return [1, 0, 0, 0];
    const d = r.q.clone().invert().multiply(bone.quaternion);
    return [+d.w.toFixed(6), +d.x.toFixed(6), +d.y.toFixed(6), +d.z.toFixed(6)];
  }
  function applyDelta(bone, q){
    const r = rest.get(bone.uuid);
    if (!r) return;
    bone.quaternion.copy(r.q).multiply(
      new THREE.Quaternion(q[1], q[2], q[3], q[0]));
  }
  const isPosed = bone => {
    const d = deltaOf(bone);
    return Math.abs(Math.abs(d[0]) - 1) > 1e-4;
  };

  function toRest(){
    if (!skeleton) return;
    skeleton.bones.forEach(b => {
      const r = rest.get(b.uuid);
      if (r) { b.quaternion.copy(r.q); b.position.copy(r.p); }
    });
    dirty = !!keys.length;
    paint();
  }

  /* ── overlay ───────────────────────────────────────────────────────────*/
  function ensure(){
    const S = ME();
    if (!S || !S.three) return null;
    if (group && group.parent === S.three.scene) return group;
    group = new THREE.Group();
    group.name = "bgate-pose";
    S.three.scene.add(group);
    return group;
  }

  function build(){
    const g = ensure();
    if (!g || !skeleton) return;
    const bones = skeleton.bones;
    if (!lines || lines.userData.count !== bones.length) {
      if (lines) { lines.geometry.dispose(); lines.material.dispose(); g.remove(lines); }
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(
        new Float32Array(bones.length * 6), 3));
      lines = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({
        color: C_BONE, depthTest: false, transparent: true, opacity: 0.8}));
      lines.userData.count = bones.length;
      lines.renderOrder = 3;
      g.add(lines);
    }
    const size = handleSize();
    bones.forEach(b => {
      let h = handles.get(b.uuid);
      if (!h) {
        h = new THREE.Mesh(new THREE.SphereGeometry(1, 8, 6),
          new THREE.MeshBasicMaterial({color: C_BONE, depthTest: false}));
        h.userData.boneUuid = b.uuid;
        h.renderOrder = 4;
        g.add(h);
        handles.set(b.uuid, h);
      }
      h.scale.setScalar(size);
    });
    armPicker();
    paint();
  }

  function handleSize(){
    const S = ME();
    if (!S || !S.three || !S.three.root) return 0.02;
    const box = new THREE.Box3().setFromObject(S.three.root);
    const h = box.isEmpty() ? 1.8 : (box.max.y - box.min.y) || 1.8;
    return Math.max(0.004, h * 0.011);
  }

  /* Drawn from the bones' WORLD matrices, so the overlay follows the pose the
     user is dragging rather than the rest the file was exported in. */
  function paint(){
    const g = group;
    if (!g || !skeleton) return;
    g.visible = armed;
    if (!armed) { requestRender(); return; }
    const pos = lines.geometry.getAttribute("position");
    const a = new THREE.Vector3(), b = new THREE.Vector3();
    skeleton.bones.forEach((bone, i) => {
      bone.updateWorldMatrix(true, false);
      a.setFromMatrixPosition(bone.matrixWorld);
      const kid = bone.children.find(c => c.isBone);
      if (kid) { kid.updateWorldMatrix(true, false); b.setFromMatrixPosition(kid.matrixWorld); }
      else b.copy(a);
      pos.setXYZ(i * 2, a.x, a.y, a.z);
      pos.setXYZ(i * 2 + 1, b.x, b.y, b.z);
      const h = handles.get(bone.uuid);
      if (h) {
        h.position.copy(a);
        h.material.color.setHex(bone === sel ? C_SEL : (isPosed(bone) ? C_POSED : C_BONE));
        h.scale.setScalar(handleSize() * (bone === sel ? 1.6 : 1));
      }
    });
    pos.needsUpdate = true;
    lines.geometry.computeBoundingSphere();
    requestRender();
  }

  function drop(){
    detachGizmo();
    if (group) {
      group.traverse(o => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) (Array.isArray(o.material) ? o.material : [o.material])
          .forEach(m => m.dispose());
      });
      if (group.parent) group.parent.remove(group);
    }
    group = null; lines = null; handles = new Map();
    if (picker && hostEl) hostEl.removeEventListener("pointerdown", picker, true);
    picker = null; hostEl = null;
    requestRender();
  }

  function armPicker(){
    const S = ME();
    if (!S || !S.three || picker) return;
    hostEl = S.three.renderer.domElement;
    picker = ev => {
      if (!armed || ev.button !== 0) return;
      const S2 = ME();
      if (!S2 || !S2.three) return;
      const rect = hostEl.getBoundingClientRect();
      const p = new THREE.Vector2(
        ((ev.clientX - rect.left) / rect.width) * 2 - 1,
        -((ev.clientY - rect.top) / rect.height) * 2 + 1);
      const ray = new THREE.Raycaster();
      ray.setFromCamera(p, S2.three.camera);
      const hits = ray.intersectObjects([...handles.values()], false);
      if (!hits.length) return;
      ev.stopPropagation();
      ev.preventDefault();
      const uuid = hits[0].object.userData.boneUuid;
      selectBone(skeleton.bones.find(x => x.uuid === uuid) || null);
    };
    hostEl.addEventListener("pointerdown", picker, true);
  }

  /* ── gizmo ─────────────────────────────────────────────────────────────*/
  function attachGizmo(){
    const S = ME();
    if (!S || !S.three || !sel) return detachGizmo();
    if (!gizmo) {
      gizmo = new TransformControls(S.three.camera, S.three.renderer.domElement);
      gizmo.setMode("rotate");
      // Local space, because a bone rotates in its own frame and a world-space
      // gizmo on a rotated forearm points nowhere the arm can bend.
      gizmo.setSpace("local");
      gizmo.addEventListener("dragging-changed", ev => {
        S.three.controls.enabled = !ev.value;
        if (!ev.value) render();
      });
      gizmo.addEventListener("objectChange", () => { dirty = true; paint(); });
      S.three.scene.add(gizmo.getHelper());
    }
    if (S.three.transform) S.three.transform.detach();
    gizmo.attach(sel);
    requestRender();
  }

  function detachGizmo(){
    if (gizmo) {
      try { gizmo.detach(); } catch (e) {}
      const helper = typeof gizmo.getHelper === "function" ? gizmo.getHelper() : null;
      if (helper && helper.parent) helper.parent.remove(helper);
      try { gizmo.dispose(); } catch (e) {}
      gizmo = null;
    }
    requestRender();
  }

  function selectBone(bone){
    sel = (bone === sel) ? null : bone;
    if (sel) attachGizmo(); else detachGizmo();
    paint(); render();
  }

  /* ── keys ──────────────────────────────────────────────────────────────*/
  function wholePose(){
    const out = {};
    if (!skeleton) return out;
    skeleton.bones.forEach(b => {
      const name = b.name;
      if (!name) return;
      out[name] = deltaOf(b);
    });
    return out;
  }

  function setKey(){
    if (!skeleton) return;
    if (keys.length >= MAX_KEYS && !keys.some(k => Math.abs(k.t - at) < 1e-4)) {
      say("that is " + MAX_KEYS + " keys already", "err");
      return;
    }
    const key = {t: +at.toFixed(3), bones: wholePose()};
    const i = keys.findIndex(k => Math.abs(k.t - key.t) < 1e-4);
    if (i >= 0) keys[i] = key; else keys.push(key);
    keys.sort((a, b) => a.t - b.t);
    dirty = false;
    render();
  }

  function dropKey(t){
    keys = keys.filter(k => Math.abs(k.t - Number(t)) > 1e-4);
    render();
  }

  /* The same interpolation posed_clip does server-side, so the preview and
     the bake agree: slerp between the bracketing keys, and a bone no key
     mentions sits at rest. */
  function seek(t){
    at = clamp(num(t, 0), 0, clipLen);
    dirty = false;
    if (!skeleton || !keys.length) { paint(); return; }
    const lo = [...keys].reverse().find(k => k.t <= at) || keys[0];
    const hi = keys.find(k => k.t >= at) || keys[keys.length - 1];
    const span = hi.t - lo.t;
    const u = span <= 1e-9 ? 0 : (at - lo.t) / span;
    const qa = new THREE.Quaternion(), qb = new THREE.Quaternion();
    skeleton.bones.forEach(b => {
      const A = lo.bones[b.name] || [1, 0, 0, 0];
      const B = hi.bones[b.name] || [1, 0, 0, 0];
      qa.set(A[1], A[2], A[3], A[0]);
      qb.set(B[1], B[2], B[3], B[0]);
      qa.slerp(qb, u);
      applyDelta(b, [qa.w, qa.x, qa.y, qa.z]);
    });
    paint();
  }

  function arm(on){
    armed = on;
    if (armed) {
      skeleton = findSkeleton();
      if (!skeleton) { armed = false; say("this model has no skinned skeleton", "err"); render(); return; }
      if (!rest.size) snapshotRest();
      build();
    } else {
      toRest();
      selectBone(null);
      if (group) group.visible = false;
    }
    paint(); render();
  }

  /* ── bake ──────────────────────────────────────────────────────────────*/
  async function bake(){
    const S = ME();
    if (!S || baking || keys.length < 1) return;
    const name = (clipName || "action").trim().toLowerCase()
      .replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "action";
    if (!await confirmAsk({
        title: "Bake " + name + "?",
        body: keys.length + " keys over " + clipSeconds().toFixed(2) + "s at " +
              clipFps + " fps. Blender re-keys this on the rig, exports it and "
              + "runs the support and self-intersection gates over the result. "
              + "Writes <name>.anim.glb beside the model.",
        ok: "bake"})) return;
    baking = true; bakeErr = ""; baked = null; render();
    try {
      const r = await fetch("/api/model3d/animate", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({rel: S.rel, fps: clipFps, proof_frames: 6,
          clips: [{name, kind: "bones", loop: clipLoop,
                   keys: keys.map(k => ({t: k.t, bones: k.bones}))}]})});
      const b = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(((b.error || {}).message) || r.statusText);
      baked = b.data || b;
      if (baked.refused) say("refused: the skin and the skeleton disagree about forward", "err");
      else say("baked " + name, "ok");
    } catch (e) {
      bakeErr = String((e && e.message) || e).slice(0, 300);
      say("the bake failed: " + bakeErr, "err");
    }
    baking = false; render();
  }

  /* ── panel ─────────────────────────────────────────────────────────────*/
  function panel(hasSkeleton){
    const ui = UI();
    if (!ui) return "";
    const {panel: box, row, K} = ui;
    const act = `<button class="qbtn ghost small" data-mt="posearm" ${hasSkeleton ? "" : "disabled"}>` +
      (armed ? "leave" : "pose") + "</button>";
    let body = '<div class="mt-note">Rotate a bone, key the pose, scrub it. The mesh on ' +
      'screen deforms as you drag - this is the loaded rig, not a picture of it. ' +
      'Baking hands the keys to Blender, which re-keys them on the real skeleton and ' +
      'runs the same gates every other clip goes through.</div>';
    if (!hasSkeleton)
      return box("k-read", "animation", "pose", "", "",
        body + '<div class="mt-note warn">Needs a skeleton first.</div>', act);
    if (!armed)
      return box("k-read", "animation", "pose",
                 keys.length ? keys.length + " keys" : "", keys.length ? "warn" : "",
                 body, act);

    const bones = (skeleton && skeleton.bones) || [];
    const posed = bones.filter(isPosed);
    body = '<div class="mt-grid">' +
      row("bones", String(bones.length)) +
      row("moved from rest", String(posed.length), posed.length ? "warn" : "") +
      row("selected", sel ? (sel.name || "(unnamed)") : "none", sel ? "good" : "") +
      row("keys span", clipSeconds().toFixed(2) + "s of " + clipLen.toFixed(2) + "s") +
      '</div>' + body;

    body += '<div class="mt-seg" style="margin-top:var(--s-3)">' +
      bones.slice(0, 32).map((b, i) =>
        `<button class="qbtn ghost small ${b === sel ? "on" : ""}" data-mt="posebone" data-v="${i}" ` +
        `title="${isPosed(b) ? "moved from rest" : "at rest"}">${E(b.name || ("bone " + i))}</button>`).join("") +
      '</div>';

    // The track. A key is a whole pose, so it is one mark and not a row per
    // channel: there is nothing per-channel to show.
    body += '<div class="mt-rule"></div>' +
      '<div class="mt-row">' +
        `<input type="range" min="0" max="${clipLen.toFixed(3)}" step="0.001" ` +
          `value="${at.toFixed(3)}" data-mtf="poseat" style="flex:1">` +
        `<span class="mt-unit">${at.toFixed(2)}s</span>` +
      '</div>' +
      (dirty ? '<div class="mt-note warn">This pose is not keyed. Moving the ' +
        'playhead evaluates the clip and takes it back.</div>' : "") +
      '<div class="mt-seg" style="margin-top:var(--s-2)">' +
        (keys.length
          ? keys.map(k => `<button class="qbtn ghost small ${Math.abs(k.t - at) < 1e-4 ? "on" : ""}" ` +
              `data-mt="poseseek" data-v="${k.t}">${k.t.toFixed(2)}s</button>`).join("")
          : '<span class="mt-busy">no keys yet</span>') +
      '</div>' +
      '<div class="mt-row mt-seg" style="margin-top:var(--s-3)">' +
        `<button class="qbtn${dirty ? "" : " ghost"}" data-mt="posekey">key this pose</button>` +
        (keys.some(k => Math.abs(k.t - at) < 1e-4)
          ? `<button class="qbtn ghost small" data-mt="posedrop" data-v="${at.toFixed(3)}">delete key</button>` : "") +
        '<button class="qbtn ghost small" data-mt="poserest">back to rest</button>' +
      '</div>';

    body += '<div class="mt-row" style="margin-top:var(--s-3)">' +
      `<input type="text" data-mtf="posename" value="${E(clipName)}" placeholder="clip name">` +
      `<input type="number" min="5" max="120" step="1" value="${clipFps}" data-mtf="posefps">` +
      '<span class="mt-unit">fps</span>' +
      `<input type="number" min="0.1" max="60" step="0.1" value="${clipLen}" data-mtf="poselen">` +
      '<span class="mt-unit">s long</span>' +
      '</div>' +
      '<div class="mt-row mt-seg" style="margin-top:var(--s-2)">' +
        `<button class="qbtn ghost small ${clipLoop ? "on" : ""}" data-mt="poseloop">` +
          (clipLoop ? "loops" : "plays once") + '</button>' +
        `<button class="qbtn" data-mt="posebake" ${(baking || !keys.length) ? "disabled" : ""}>` +
          (baking ? "baking…" : "bake") + '</button>' +
        (keys.length ? '<button class="qbtn ghost small" data-mt="poseclear">clear keys</button>' : "") +
      '</div>';

    if (bakeErr) body += `<div class="mt-note bad">${E(bakeErr)}</div>`;
    if (baked && baked.refused)
      body += `<div class="mt-note bad">Refused, nothing written. ${E(baked.error || "")}</div>`;
    else if (baked) {
      const sup = baked.support || {};
      body += '<div class="mt-rule"></div><div class="mt-grid">' +
        row("wrote", baked.out || "-", "good") +
        row("clips", K((baked.clips || []).length)) +
        row("support", sup.measured === false ? (sup.reason || "not measured")
            : (sup.passed ? "clean" : ((sup.failed || []).length + " failing")),
            sup.measured === false ? "" : (sup.passed ? "good" : "warn")) +
        row("took", num(baked.seconds, 0).toFixed(1) + "s") +
        '</div>' +
        (baked.out ? '<div class="mt-row"><button class="qbtn ghost small" data-mt="open" ' +
          `data-v="${E(baked.out)}">open the baked clip</button></div>` : "");
    }

    return box("k-read", "animation", "pose",
               keys.length + " keys", keys.length ? "warn" : "", body, act);
  }

  function handle(act, val){
    switch (act) {
      case "posearm": arm(!armed); return true;
      case "posebone": {
        const b = (skeleton && skeleton.bones[Number(val)]) || null;
        selectBone(b); return true;
      }
      case "posekey": setKey(); return true;
      case "posedrop": dropKey(val); return true;
      case "poseseek": seek(Number(val)); render(); return true;
      case "poserest": toRest(); render(); return true;
      case "poseloop": clipLoop = !clipLoop; render(); return true;
      case "poseclear": keys = []; at = 0; render(); return true;
      case "posebake": bake(); return true;
      default: return false;
    }
  }

  /* Typed fields, which must NOT rebuild the column on every keystroke: it
     takes the focus out of the field being typed into. */
  function field(name, el){
    if (name === "poseat") { seek(el.value); return true; }
    if (name === "posename") { clipName = el.value; return true; }
    if (name === "posefps") { clipFps = clamp(Math.round(num(el.value, 30)), 5, 120); return true; }
    if (name === "poselen") {
      clipLen = clamp(num(el.value, 2), 0.1, 60);
      if (at > clipLen) seek(clipLen);
      return true;
    }
    return false;
  }

  function sync(rel){
    if (rel === shownRel) {
      // The viewer reloads the mesh on its own (a bake reopens the result);
      // the skeleton object is then a different one and rest belongs to a
      // model that is gone.
      if (armed && skeleton && skeleton !== findSkeleton()) {
        skeleton = findSkeleton();
        rest = new Map(); sel = null;
        if (skeleton) { snapshotRest(); build(); } else arm(false);
      }
      return;
    }
    shownRel = rel;
    drop();
    armed = false; skeleton = null; rest = new Map(); sel = null;
    keys = []; at = 0; baked = null; bakeErr = ""; dirty = false;
  }

  return {
    panel, handle, field, sync, drop,
    get armed(){ return armed; },
    get keys(){ return keys.map(k => ({t: k.t, bones: {...k.bones}})); },
    get selected(){ return sel ? sel.name : null; },
    get playhead(){ return at; },
    get length(){ return clipLen; },
    get dirty(){ return dirty; },
  };
})();
