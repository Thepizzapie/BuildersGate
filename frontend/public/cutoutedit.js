/* cutoutedit.js — the cutout RIG editor: set a rig up, and animate it.
 *
 * WHY THIS EXISTS (user directive, 2026-09-22). Generated rigs land close and
 * then fail on the last few pixels: a shoulder level with the collar, arms
 * that do not line up with the torso, a piece hanging a little off its bone,
 * a pose that reads wrong. Judging that from a screenshot and re-rolling a
 * model is slow, costs money and guesses. Here a person does it by hand.
 *
 * TWO MODES.
 *   SETUP (the rest pose by default): move a JOINT (the bone's rest offset,
 *     which every clip keeps), rotate it on the ring, or move a joint ONLY
 *     (Alt, or "joint only") so the art stays put and the rotation point
 *     lands where the drawing's elbow actually is. Move / rotate / scale a
 *     PIECE, and set DRAW ORDER in Layers.
 *   ANIMATE: pick a clip and a time, rotate a bone on the ring and it KEYS
 *     that bone at that time. Keys are this character's own (clip_overrides
 *     in the .cutout.json) and replace the library track for that bone only;
 *     everything else keeps playing the library, and the floor solve re-runs
 *     on save.
 *
 * WHAT IT PLAYS is the server's bake (routes/cutoutedit.py -> cutoutwire):
 * mirrored clips, floor solve, adjustments, eased keys (Godot's ease(t, -2),
 * reproduced below). Save writes the .cutout.json, re-emits the Godot scene
 * and reloads the bake, so what you see after a save is what Godot plays.
 *
 * Vanilla, dependency-free, guarded: this module must never throw uncaught
 * into the dashboard.
 */
window.CutoutEdit = (() => {
  "use strict";
  let _host = null;
  const $ = {};
  let S = null;                  // loaded rig + edit state

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const DEG = Math.PI / 180;
  const RING = 30;               // gizmo ring radius, screen px
  const KEY_SNAP = 1 / 24;       // a key this close to the playhead is "the" key
  const visible = el => !!el && el.isConnected && el.getClientRects().length > 0;
  const clone = o => JSON.parse(JSON.stringify(o));

  /* ---------------------------------------------------------------- maths */
  // Affine (a, b, c, d, e, f): x' = a x + c y + e, y' = b x + d y + f — the
  // same order as canvas setTransform and the emitter's Python.
  const mul = (p, q) => [
    p[0]*q[0] + p[2]*q[1], p[1]*q[0] + p[3]*q[1],
    p[0]*q[2] + p[2]*q[3], p[1]*q[2] + p[3]*q[3],
    p[0]*q[4] + p[2]*q[5] + p[4], p[1]*q[4] + p[3]*q[5] + p[5]];
  const rot = r => [Math.cos(r), Math.sin(r), -Math.sin(r), Math.cos(r), 0, 0];
  const tr = (x, y) => [1, 0, 0, 1, x, y];
  const scl = s => [s, 0, 0, s, 0, 0];
  const inv = m => {
    const det = m[0]*m[3] - m[1]*m[2] || 1e-9;
    const a = m[3]/det, b = -m[1]/det, c = -m[2]/det, d = m[0]/det;
    return [a, b, c, d, -(a*m[4] + c*m[5]), -(b*m[4] + d*m[5])];
  };
  const apply = (m, x, y) => [m[0]*x + m[2]*y + m[4], m[1]*x + m[3]*y + m[5]];
  const applyV = (m, x, y) => [m[0]*x + m[2]*y, m[1]*x + m[3]*y];
  const wrap = a => { while (a > Math.PI) a -= 2*Math.PI; while (a < -Math.PI) a += 2*Math.PI; return a; };
  const r3 = v => +(+v).toFixed(3);

  // Godot's ease(): the transition every emitted key carries.
  function ease(p, c){
    if (c === 1 || c === undefined) return p;
    if (c > 0) return p < 1 ? Math.pow(p, c) : 1;
    const e = -c;
    return p < 0.5 ? Math.pow(p*2, e) * 0.5 : (1 - Math.pow(1 - (p - 0.5)*2, e)) * 0.5 + 0.5;
  }
  function sample(keys, t, len, loop, c){
    if (!keys || !keys.length) return null;
    const lerp = (a, b, f) => Array.isArray(a) ? a.map((v, i) => v + (b[i] - v) * f) : a + (b - a) * f;
    if (keys.length === 1) return keys[0][1];
    for (let i = 0; i < keys.length - 1; i++){
      const [t0, v0] = keys[i], [t1, v1] = keys[i + 1];
      if (t >= t0 && t <= t1) return lerp(v0, v1, ease(t1 > t0 ? (t - t0)/(t1 - t0) : 0, c));
    }
    const first = keys[0], last = keys[keys.length - 1];
    if (loop){          // wrap from the last key round to the first
      const span = first[0] + len - last[0];
      const tt = t < first[0] ? t + len : t;
      return lerp(last[1], first[1], ease(span > 0 ? (tt - last[0])/span : 0, c));
    }
    return t < first[0] ? first[1] : last[1];
  }

  /* ------------------------------------------------------------- the pose */
  function editDelta(bone){
    // What the live setup edit adds on top of the SAVED bake, in Godot units.
    const cur = S.adj[bone] || {}, was = S.saved.adj[bone] || {};
    const cp = cur.pos || [0, 0], wp = was.pos || [0, 0];
    return { dx: cp[0] - wp[0], dy: -(cp[1] - wp[1]),
             dr: -((cur.rot || 0) - (was.rot || 0)) * DEG };
  }
  const restView = () => S.mode === "setup" && S.rest;
  function localPose(clip, t){
    const c = (!restView() && S.data.clips[clip]) || {};
    const local = {};
    for (const b of S.data.bones){
      const trk = (c.tracks || {})[b.name] || {};
      const p = sample(trk.pos, t, c.length, c.loop, S.data.ease) || b.rest.pos;
      const r = sample(trk.rot, t, c.length, c.loop, S.data.ease);
      const d = editDelta(b.name);
      local[b.name] = [p[0] + d.dx, p[1] + d.dy, (r === null ? b.rest.rot : r) + d.dr];
    }
    return local;
  }
  function pose(clip, t){
    const local = localPose(clip, t);
    const world = {};
    const W = name => {
      if (world[name]) return world[name];
      const b = S.bones[name], l = local[name];
      const m = mul(tr(l[0], l[1]), rot(l[2]));
      world[name] = b.parent ? mul(W(b.parent), m) : m;
      return world[name];
    };
    for (const b of S.data.bones) W(b.name);
    return world;
  }
  function pieceMatrix(world, slot){
    const p = S.pieces[slot], info = S.data.parts[slot];
    const [w, h] = info.size;
    const off = [-p.pivot[0] * w, -(1 - p.pivot[1]) * h];
    return mul(mul(mul(world[info.bone], rot(-p.rot_offset * DEG)), scl(p.scale)), tr(off[0], off[1]));
  }
  const selBone = () => !S.sel ? null : S.sel.kind === "bone" ? S.sel.name : S.data.parts[S.sel.name].bone;

  /* -------------------------------------------------------------- layers */
  // Draw order is each piece's z (absolute: the emitter writes
  // z_as_relative = false). Equal z draws in slot order, as in Godot's tree.
  function layerOrder(frontFirst){
    const names = Object.keys(S.data.parts);
    const idx = Object.fromEntries(names.map((n, i) => [n, i]));
    const order = names.slice().sort((a, b) => (S.pieces[a].z - S.pieces[b].z) || (idx[a] - idx[b]));
    return frontFirst ? order.reverse() : order;
  }
  function setOrder(frontToBack){
    const n = frontToBack.length;
    frontToBack.forEach((slot, i) => {
      if (S.pieces[slot].z !== n - 1 - i){ S.pieces[slot].z = n - 1 - i; S.dirtyPieces.add(slot); }
    });
    markDirty();
  }
  function moveLayer(slot, dir){
    const order = layerOrder(true);
    const i = order.indexOf(slot), j = i + dir;
    if (i < 0 || j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    setOrder(order);
  }
  let _layersKey = "";
  function renderLayers(){
    const box = $.layers;
    if (!box || !S) return;
    // Rebuild only when something it shows changed: during playback render()
    // runs every frame, and a rebuilt list drops a row mid-drag.
    const order = layerOrder(true);
    const key = [S.rel, order.map(s => s + S.pieces[s].z).join(","),
                 S.sel ? S.sel.kind + S.sel.name : "", [...S.hidden].join(",")].join("|");
    if (key === _layersKey) return;
    _layersKey = key;
    const rows = order.map(slot => {
      const p = S.data.parts[slot];
      const on = S.sel && S.sel.kind === "piece" && S.sel.name === slot;
      const hid = S.hidden.has(slot);
      return `<div class="ce-layer${on ? " on" : ""}${hid ? " hid" : ""}" draggable="true" data-slot="${E(slot)}">
        <button class="ce-eye" data-eye="${E(slot)}" title="${hid ? "show" : "hide"} while editing">${hid ? "◌" : "●"}</button>
        <span class="ce-lname">${E(slot)}${p.reuse_of ? ` <i>↳ ${E(p.reuse_of)}</i>` : ""}</span>
        <span class="ce-lz">${S.pieces[slot].z}</span>
        <button class="ce-lbtn" data-up="${E(slot)}" title="bring forward">▲</button>
        <button class="ce-lbtn" data-down="${E(slot)}" title="send back">▼</button>
      </div>`;
    }).join("");
    box.innerHTML = `<div class="ce-h">Layers <span class="ce-sub">front → back · drag to reorder</span></div>${rows}`;
    box.querySelectorAll(".ce-layer").forEach(row => {
      const slot = row.dataset.slot;
      row.onclick = ev => {
        if (ev.target.closest("button")) return;
        S.sel = { kind: "piece", name: slot }; syncInspector(); render();
      };
      row.ondragstart = ev => { ev.dataTransfer.setData("text/plain", slot); row.classList.add("drag"); };
      row.ondragend = () => row.classList.remove("drag");
      row.ondragover = ev => { ev.preventDefault(); row.classList.add("over"); };
      row.ondragleave = () => row.classList.remove("over");
      row.ondrop = ev => {
        ev.preventDefault(); row.classList.remove("over");
        const from = ev.dataTransfer.getData("text/plain");
        if (!from || from === slot) return;
        const order = layerOrder(true).filter(s => s !== from);
        order.splice(order.indexOf(slot), 0, from);
        setOrder(order);
      };
    });
    box.querySelectorAll("[data-eye]").forEach(b => b.onclick = () => {
      const s = b.dataset.eye;
      if (S.hidden.has(s)) S.hidden.delete(s); else S.hidden.add(s);
      render();
    });
    box.querySelectorAll("[data-up]").forEach(b => b.onclick = () => moveLayer(b.dataset.up, -1));
    box.querySelectorAll("[data-down]").forEach(b => b.onclick = () => moveLayer(b.dataset.down, 1));
  }

  /* -------------------------------------------------------------- drawing */
  function drawRig(ctx, view, clip, t, opts){
    const world = pose(clip, t);
    for (const slot of layerOrder(false)){
      if (S.hidden.has(slot) && opts && opts.main) continue;
      const img = S.img[slot];
      if (!img || !img.complete || !img.naturalWidth) continue;
      const m = mul(view, pieceMatrix(world, slot));
      ctx.setTransform(m[0], m[1], m[2], m[3], m[4], m[5]);
      const tint = S.data.parts[slot].far_tint;
      ctx.filter = tint ? `brightness(${tint[0]})` : "none";
      ctx.drawImage(img, 0, 0);
      ctx.filter = "none";
      if (opts && opts.sel && opts.sel.kind === "piece" && opts.sel.name === slot){
        const [w, h] = S.data.parts[slot].size;
        ctx.lineWidth = 1.5 / Math.hypot(m[0], m[1]);
        ctx.strokeStyle = "#ffcc33";
        ctx.strokeRect(0, 0, w, h);
      }
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    if (opts && opts.joints){
      // Bones as lines parent -> child, joints as dots.
      ctx.lineWidth = 1.5; ctx.strokeStyle = "rgba(80,200,255,.55)";
      for (const b of S.data.bones){
        if (!b.parent) continue;
        const [x0, y0] = apply(mul(view, world[b.parent]), 0, 0);
        const [x1, y1] = apply(mul(view, world[b.name]), 0, 0);
        ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
      }
      for (const b of S.data.bones){
        const [x, y] = apply(mul(view, world[b.name]), 0, 0);
        const on = selBone() === b.name;
        const keyed = S.mode === "animate" && boneKeyed(b.name);
        ctx.beginPath();
        ctx.arc(x, y, on ? 6 : 4, 0, Math.PI * 2);
        ctx.fillStyle = on ? "#ffcc33" : keyed ? "rgba(255,120,80,.95)" : "rgba(80,200,255,.9)";
        ctx.fill();
        ctx.strokeStyle = "#0008"; ctx.lineWidth = 1; ctx.stroke();
      }
    }
    return world;
  }
  function gizmo(){
    // Where the rotate ring and its handle are, in canvas px.
    const bone = selBone();
    if (!bone || !S.world[bone]) return null;
    const m = mul(mainView(), S.world[bone]);
    const [jx, jy] = apply(m, 0, 0);
    let ang;
    if (S.sel.kind === "piece" && S.mode === "setup"){
      const pm = mul(mainView(), pieceMatrix(S.world, S.sel.name));
      const [w, h] = S.data.parts[S.sel.name].size;
      const [cx, cy] = apply(pm, w / 2, h / 2);
      ang = Math.atan2(cy - jy, cx - jx);
    } else {
      const [hx, hy] = applyV(m, 0, 1);       // the bone hangs along its local +y
      ang = Math.atan2(hy, hx);
    }
    return { jx, jy, ang, hx: jx + Math.cos(ang) * RING, hy: jy + Math.sin(ang) * RING };
  }
  function drawGizmo(ctx){
    const g = gizmo();
    if (!g) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.lineWidth = 2; ctx.strokeStyle = "rgba(255,204,51,.9)";
    ctx.beginPath(); ctx.arc(g.jx, g.jy, RING, 0, Math.PI * 2); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(g.jx, g.jy); ctx.lineTo(g.hx, g.hy); ctx.stroke();
    ctx.beginPath(); ctx.arc(g.hx, g.hy, 6, 0, Math.PI * 2);
    ctx.fillStyle = "#ffcc33"; ctx.fill(); ctx.strokeStyle = "#000a"; ctx.lineWidth = 1; ctx.stroke();
    if (S.mode === "setup" && S.sel.kind === "bone"){
      // The move handle: a square on the joint.
      ctx.fillStyle = S.jointOnly ? "#ff6a3d" : "#ffcc33";
      ctx.fillRect(g.jx - 5, g.jy - 5, 10, 10);
      ctx.strokeRect(g.jx - 5, g.jy - 5, 10, 10);
    }
  }
  function mainView(){
    const c = $.canvas;
    return [S.zoom, 0, 0, S.zoom, c.width / 2 + S.pan[0], c.height - 60 + S.pan[1]];
  }
  function render(){
    if (!S || !$.canvas) return;
    const c = $.canvas, ctx = c.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, c.width, c.height);
    const view = mainView();
    ctx.strokeStyle = "rgba(120,130,140,.7)"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(0, view[5]); ctx.lineTo(c.width, view[5]); ctx.stroke();
    const fit = S.data.game_fit || {};
    if (S.data.player_height_px && fit.scale){
      const hh = S.data.player_height_px / fit.scale * S.zoom;
      ctx.fillStyle = "rgba(128,128,140,.35)";
      ctx.fillRect(view[4] + 75 * S.zoom, view[5] - hh, 10, hh);
    }
    S.world = drawRig(ctx, view, S.clip, S.t, { joints: S.showJoints, sel: S.sel, main: true });
    drawGizmo(ctx);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "#334"; ctx.font = "12px system-ui"; ctx.textAlign = "left";
    ctx.fillText(restView() ? "SETUP · rest pose" : `${S.mode.toUpperCase()} · ${S.clip} @ ${S.t.toFixed(2)}s`, 10, 18);
    renderStrip();
    renderLayers();
    renderKeys();
    $.time.value = String(S.t);
    $.tlab.textContent = `${S.clip} ${S.t.toFixed(2)} / ${(S.data.clips[S.clip] || {}).length || 0}s`;
  }
  function stripTime(name){
    const c = S.data.clips[name] || {};
    const len = c.length || 1;
    return c.loop ? len * 0.4 : len * 0.6;
  }
  function renderStrip(){
    const c = $.strip, ctx = c.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, c.width, c.height);
    const names = Object.keys(S.data.clips);
    const cw = c.width / names.length, z = Math.min(0.6, (c.height - 26) / 230);
    const wasRest = S.rest; S.rest = false;         // the strip always plays clips
    names.forEach((n, i) => {
      if (n === S.clip){ ctx.fillStyle = "rgba(255,106,61,.14)"; ctx.fillRect(i * cw, 0, cw, c.height); }
      const view = [z, 0, 0, z, i * cw + cw / 2, c.height - 22];
      ctx.strokeStyle = "rgba(120,130,140,.5)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(i * cw + 4, view[5]); ctx.lineTo((i + 1) * cw - 4, view[5]); ctx.stroke();
      if (!(S.data.clips[n] || {}).error) drawRig(ctx, view, n, n === S.clip ? S.t : stripTime(n));
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillStyle = (S.data.overrides || {})[n] || S.clipDirty[n] ? "#c24a1c" : "#556";
      ctx.font = "11px system-ui"; ctx.textAlign = "center";
      ctx.fillText(n + ((S.data.overrides || {})[n] ? " *" : ""), i * cw + cw / 2, c.height - 6);
    });
    S.rest = wasRest;
  }

  /* ------------------------------------------------------ keys (animate) */
  const curClip = () => S.data.clips[S.clip] || {};
  const boneKeyed = bone => !!(((curClip().tracks || {})[bone] || {}).rot);
  function renderKeys(){
    const c = $.keys;
    if (!c) return;
    const ctx = c.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, c.width, c.height);
    const clip = curClip(), len = clip.length || 1;
    const x = t => 8 + (c.width - 16) * t / len;
    ctx.strokeStyle = "rgba(120,130,140,.5)";
    ctx.beginPath(); ctx.moveTo(8, c.height / 2); ctx.lineTo(c.width - 8, c.height / 2); ctx.stroke();
    for (const [t] of (clip.events || [])){
      ctx.fillStyle = "#e33"; ctx.fillRect(x(t) - 1, 2, 2, c.height - 4);
    }
    const bone = selBone();
    const keys = bone && ((clip.tracks || {})[bone] || {}).rot;
    if (keys){
      ctx.fillStyle = (S.clipDirty[S.clip] && S.clipDirty[S.clip].has(bone)) ? "#ff6a3d" : "#ffcc33";
      for (const [t] of keys){
        const kx = x(t), ky = c.height / 2;
        ctx.beginPath(); ctx.moveTo(kx, ky - 6); ctx.lineTo(kx + 6, ky); ctx.lineTo(kx, ky + 6); ctx.lineTo(kx - 6, ky); ctx.closePath(); ctx.fill();
      }
    }
    ctx.fillStyle = "#39f"; ctx.fillRect(x(S.t) - 1, 0, 2, c.height);
    if ($.keylab) $.keylab.textContent = bone
      ? `${bone}: ${keys ? keys.length + " key" + (keys.length === 1 ? "" : "s") : "no track - rotating it adds one"}` +
        ((S.data.overrides[S.clip] || []).includes(bone) ? " (this character's own)" : " (library)")
      : "select a bone to see its keys";
  }
  function trackFor(bone, create){
    const clip = curClip();
    clip.tracks = clip.tracks || {};
    const trk = clip.tracks[bone] = clip.tracks[bone] || {};
    if (!trk.rot && create){
      const cur = localPose(S.clip, S.t)[bone][2] - editDelta(bone).dr;
      trk.rot = [[0, cur]];
    }
    return trk;
  }
  function keyIndex(keys, t){
    return keys.findIndex(k => Math.abs(k[0] - t) <= KEY_SNAP / 2);
  }
  function setKey(bone, value){
    const trk = trackFor(bone, true);
    const t = +S.t.toFixed(3);
    const i = keyIndex(trk.rot, t);
    if (i >= 0) trk.rot[i][1] = value;
    else { trk.rot.push([t, value]); trk.rot.sort((a, b) => a[0] - b[0]); }
    markClip(bone);
  }
  function keyNow(){
    const bone = selBone();
    if (!bone) return;
    const cur = localPose(S.clip, S.t)[bone][2] - editDelta(bone).dr;
    setKey(bone, cur);
  }
  function deleteKey(){
    const bone = selBone();
    const trk = bone && (curClip().tracks || {})[bone];
    if (!trk || !trk.rot) return;
    const i = keyIndex(trk.rot, S.t);
    if (i < 0){ say("no key at the playhead on " + bone); return; }
    if (trk.rot.length === 1){ say("that is the last key: use 'reset bone in clip' to go back to the library"); return; }
    trk.rot.splice(i, 1);
    markClip(bone);
  }
  function jumpKey(dir){
    const bone = selBone();
    const keys = bone && ((curClip().tracks || {})[bone] || {}).rot;
    if (!keys || !keys.length) return;
    const times = keys.map(k => k[0]);
    const next = dir > 0 ? times.find(t => t > S.t + 1e-4) : times.slice().reverse().find(t => t < S.t - 1e-4);
    if (next !== undefined){ S.t = next; syncInspector(); render(); }
  }
  function markClip(bone){
    (S.clipDirty[S.clip] = S.clipDirty[S.clip] || new Set()).add(bone);
    markDirty();
  }
  function resetBoneInClip(all){
    const bone = selBone();
    if (!all && !bone) return;
    const r = S.clipReset[S.clip] = S.clipReset[S.clip] || [];
    if (all){ S.clipReset[S.clip] = []; S.clipDirty[S.clip] = new Set(); S.clipResetAll.add(S.clip); }
    else { r.push(bone); if (S.clipDirty[S.clip]) S.clipDirty[S.clip].delete(bone); }
    markDirty();
    say(all ? `${S.clip} goes back to the library on save` : `${bone} in ${S.clip} goes back to the library on save`);
  }

  /* --------------------------------------------------------- hit testing */
  function alphaAt(slot, x, y){
    const info = S.data.parts[slot];
    if (x < 0 || y < 0 || x >= info.size[0] || y >= info.size[1]) return 0;
    let data = S.alpha[slot];
    if (!data){
      const img = S.img[slot];
      if (!img || !img.complete) return 0;
      const cv = document.createElement("canvas");
      cv.width = info.size[0]; cv.height = info.size[1];
      const cx = cv.getContext("2d");
      cx.drawImage(img, 0, 0);
      try { data = S.alpha[slot] = cx.getImageData(0, 0, cv.width, cv.height).data; }
      catch(e){ return 255; }
    }
    return data[(Math.floor(y) * info.size[0] + Math.floor(x)) * 4 + 3];
  }
  function pick(mx, my){
    const g = gizmo();
    if (g){
      if (Math.hypot(mx - g.hx, my - g.hy) <= 9) return { gizmo: "rotate" };
      if (S.mode === "setup" && S.sel.kind === "bone" && Math.abs(mx - g.jx) <= 7 && Math.abs(my - g.jy) <= 7)
        return { gizmo: "move" };
      const d = Math.hypot(mx - g.jx, my - g.jy);
      if (Math.abs(d - RING) <= 4) return { gizmo: "rotate" };
    }
    const view = mainView();
    if (S.showJoints){
      for (const b of S.data.bones){
        const [x, y] = apply(mul(view, S.world[b.name]), 0, 0);
        if (Math.hypot(mx - x, my - y) <= 7) return { kind: "bone", name: b.name };
      }
    }
    for (const slot of layerOrder(true)){
      if (S.hidden.has(slot)) continue;
      const m = mul(view, pieceMatrix(S.world, slot));
      const [lx, ly] = apply(inv(m), mx, my);
      if (alphaAt(slot, lx, ly) > 40) return { kind: "piece", name: slot };
    }
    return null;
  }

  /* ---------------------------------------------------------- setup edits */
  function linked(slot){
    return [slot, ...Object.keys(S.data.parts).filter(s => S.data.parts[s].reuse_of === slot)];
  }
  function touchPiece(slot, fn){
    const root = S.data.parts[slot].reuse_of || slot;   // edit the near one
    for (const s of linked(root)){ fn(S.pieces[s]); S.dirtyPieces.add(s); }
    markDirty();
  }
  const twin = name => name.endsWith("_near") ? name.replace(/_near$/, "_far") : null;
  function shiftBone(name, gx, gy){
    // gx, gy: Godot units in the bone's PARENT frame.
    const a = S.adj[name] = S.adj[name] || {};
    const p = a.pos || [0, 0];
    a.pos = [r3(p[0] + gx), r3(p[1] - gy)];
  }
  function moveBone(name, sx, sy){
    const b = S.bones[name];
    const parentW = b.parent ? mul(mainView(), S.world[b.parent]) : mainView();
    const [gx, gy] = applyV(inv(parentW), sx, sy);
    shiftBone(name, gx, gy);
    if (S.jointOnly) keepArt(name, gx, gy);
    markDirty();
  }
  function keepArt(name, gx, gy){
    // Move ONLY the joint: the pieces on this bone and the child bones shift
    // back by the same amount, so the drawing stays where it is and the
    // rotation point lands somewhere new on it. The far twin gets the same.
    const tw = twin(name);
    const bones = [name].concat(tw && S.bones[tw] ? [tw] : []);
    if (bones.length === 2) shiftBone(tw, gx, gy);
    const local = localPose(S.clip, S.t);
    for (const bn of bones){
      const [bx, by] = applyV(inv(rot(local[bn][2])), gx, gy);   // into the bone's own frame
      for (const child of S.data.bones.filter(c => c.parent === bn)) shiftBone(child.name, -bx, -by);
    }
    for (const [slot, info] of Object.entries(S.data.parts)){
      if (info.bone !== name || info.reuse_of) continue;
      const p = S.pieces[slot];
      const m = mul(mul(rot(local[name][2]), rot(-p.rot_offset * DEG)), scl(p.scale));
      const [lx, ly] = applyV(inv(m), gx, gy);
      const [w, h] = info.size;
      touchPiece(slot, q => { q.pivot = [+(q.pivot[0] + lx / w).toFixed(5), +(q.pivot[1] - ly / h).toFixed(5)]; });
    }
  }
  function turnBone(name, rad){
    if (S.mode === "animate"){
      const cur = localPose(S.clip, S.t)[name][2] - editDelta(name).dr;
      setKey(name, r3(cur + rad));
      return;
    }
    const a = S.adj[name] = S.adj[name] || {};
    a.rot = +(((a.rot || 0) - rad / DEG)).toFixed(2);
    markDirty();
  }
  function movePiece(slot, sx, sy){
    const info = S.data.parts[slot], p = S.pieces[slot];
    const m = mul(mul(mul(mainView(), S.world[info.bone]), rot(-p.rot_offset * DEG)), scl(p.scale));
    const [lx, ly] = applyV(inv(m), sx, sy);
    const [w, h] = info.size;
    touchPiece(slot, q => { q.pivot = [+(q.pivot[0] - lx / w).toFixed(5), +(q.pivot[1] + ly / h).toFixed(5)]; });
  }
  /* ------------------------------------------------------------ undo */
  // One step per GESTURE: a drag is one undo, not three hundred. `base` is
  // the state after the last committed step; an edit marks it pending, and
  // the gesture's end (mouseup, or at once for keys, fields and buttons)
  // pushes the old base and takes a new one.
  let _dragging = false;
  function snap(){
    return clone({
      adj: S.adj, pieces: S.pieces,
      clips: Object.fromEntries(Object.entries(S.data.clips).map(([n, c]) => [n, c.tracks || {}])),
      clipDirty: Object.fromEntries(Object.entries(S.clipDirty).map(([k, v]) => [k, [...v]])),
      clipReset: S.clipReset, clipResetAll: [...S.clipResetAll], dirtyPieces: [...S.dirtyPieces],
    });
  }
  function restore(st){
    S.adj = clone(st.adj); S.pieces = clone(st.pieces);
    for (const [n, tracks] of Object.entries(st.clips)) if (S.data.clips[n]) S.data.clips[n].tracks = clone(tracks);
    S.clipDirty = Object.fromEntries(Object.entries(st.clipDirty).map(([k, v]) => [k, new Set(v)]));
    S.clipReset = clone(st.clipReset); S.clipResetAll = new Set(st.clipResetAll);
    S.dirtyPieces = new Set(st.dirtyPieces);
  }
  function commit(){
    if (!S || !S.pending) return;
    S.undo.push(S.base);
    if (S.undo.length > 200) S.undo.shift();
    S.redo = [];
    S.base = snap();
    S.pending = false;
    syncUndo();
  }
  function undo(){
    if (!S) return;
    commit();
    if (!S.undo.length){ say("nothing to undo"); return; }
    S.redo.push(snap());
    const st = S.undo.pop();
    restore(st); S.base = clone(st);
    S.dirty = true; $.save.disabled = false;
    syncUndo(); syncInspector(); render();
  }
  function redo(){
    if (!S || !S.redo.length) return;
    S.undo.push(snap());
    const st = S.redo.pop();
    restore(st); S.base = clone(st);
    S.dirty = true; $.save.disabled = false;
    syncUndo(); syncInspector(); render();
  }
  function syncUndo(){
    if ($.undo) $.undo.disabled = !S || (!S.undo.length && !S.pending);
    if ($.redo) $.redo.disabled = !S || !S.redo.length;
  }
  function markDirty(){
    S.dirty = true; S.pending = true; $.save.disabled = false;
    if (!_dragging) commit();
    syncUndo(); syncInspector(); render();
  }

  /* ------------------------------------------------------------ inspector */
  function syncInspector(){
    const box = $.insp;
    if (!box) return;
    if (!S || !S.sel){
      box.innerHTML = S && S.mode === "animate"
        ? `<div class="ce-hint"><b>Animate.</b> Pick a clip, move the playhead, click a joint or a piece, and turn it on the <b>ring</b> (or drag it): that keys the bone at the playhead.<br><br>
           <b>K</b> keys the current pose, <b>Del</b> removes the key at the playhead, <b>, .</b> jump between keys, <b>[ ]</b> turn 1°, <b>Space</b> plays.
           Keys are this character's own; everything you do not key keeps the library motion.</div>`
        : `<div class="ce-hint"><b>Setup.</b> Click a <b>joint</b> or a <b>piece</b>. The <b>ring</b> rotates it; the square on a joint moves it.
           <b>Alt</b>-drag (or "joint only") moves just the joint: the art stays put and the rotation point moves on it.<br><br>
           Arrows nudge 1px (Shift 5px), <b>[ ]</b> rotate 1°, <b>PgUp/PgDn</b> reorder a piece. Wheel zooms, middle-drag pans. <b>Ctrl+Z</b> undoes, <b>Ctrl+Shift+Z</b> redoes, <b>Ctrl+S</b> saves.<br><br>
           "rest pose" shows the rig unposed; untick it (or pick a clip) to set it up while a clip shows.</div>`;
      return;
    }
    const bone = selBone();
    if (S.mode === "animate"){
      const cur = localPose(S.clip, S.t)[bone][2] - editDelta(bone).dr;
      box.innerHTML = `<div class="ce-h">bone <b>${E(bone)}</b> · ${E(S.clip)} @ ${S.t.toFixed(2)}s</div>
        <label>angle° <input type="number" step="1" data-f="ka" value="${(cur / DEG).toFixed(1)}"></label>
        <div class="ce-sub">local angle, screen degrees, clockwise +. Changing it keys the bone here.</div>
        <div class="ce-row"><button class="qbtn" data-act="key">key (K)</button><button class="qbtn" data-act="delkey">delete key</button></div>
        <div class="ce-row"><button class="qbtn" data-act="prevkey">◀ key</button><button class="qbtn" data-act="nextkey">key ▶</button></div>
        <div class="ce-row"><button class="qbtn" data-act="resetbone">reset bone in clip</button><button class="qbtn" data-act="resetclip">reset whole clip</button></div>`;
    } else if (S.sel.kind === "bone"){
      const a = S.adj[S.sel.name] || {};
      const p = a.pos || [0, 0];
      box.innerHTML = `<div class="ce-h">joint <b>${E(S.sel.name)}</b></div>
        <label>x <input type="number" step="0.5" data-f="bx" value="${p[0]}"></label>
        <label>y <input type="number" step="0.5" data-f="by" value="${p[1]}"></label>
        <label>rest rot° <input type="number" step="1" data-f="br" value="${a.rot || 0}"></label>
        <div class="ce-sub">offset from the template (px, +y up). Every clip keeps it.</div>
        <button class="qbtn" data-act="resetbone">reset joint</button>`;
    } else {
      const p = S.pieces[S.sel.name], info = S.data.parts[S.sel.name];
      const far = info.reuse_of ? `<div class="ce-sub">reuses <b>${E(info.reuse_of)}</b>: edits go to that piece and this follows (its layer is its own)</div>` : "";
      box.innerHTML = `<div class="ce-h">piece <b>${E(S.sel.name)}</b> on <b>${E(info.bone)}</b></div>${far}
        <label>pivot x <input type="number" step="0.01" data-f="px" value="${p.pivot[0]}"></label>
        <label>pivot y <input type="number" step="0.01" data-f="py" value="${p.pivot[1]}"></label>
        <label>rot° <input type="number" step="1" data-f="pr" value="${p.rot_offset}"></label>
        <label>scale <input type="number" step="0.01" data-f="ps" value="${p.scale}"></label>
        <label>layer (z) <input type="number" step="1" data-f="pz" value="${p.z}"></label>
        <div class="ce-sub">pivot = where it hangs from its bone, as a fraction of the image (y from the bottom)</div>
        <button class="qbtn" data-act="resetpiece">reset piece</button>
        <div class="ce-h" style="margin-top:8px">Regenerate</div>
        <textarea class="ce-note" rows="2" placeholder="what to change, e.g. crisper face, cap brim dark red"></textarea>
        <button class="qbtn" data-act="regen">regenerate ${E(info.reuse_of || S.sel.name)} (paid)</button>
        <div class="ce-sub">repaints this piece into its silhouette from the rig's written description; size and joints stay</div>`;
    }
    box.querySelectorAll("input").forEach(inp => inp.onchange = () => {
      const v = parseFloat(inp.value);
      if (!isFinite(v)) return;
      const f = inp.dataset.f;
      if (f === "ka"){ setKey(bone, r3(v * DEG)); return; }
      if (S.sel.kind === "bone"){
        const a = S.adj[S.sel.name] = S.adj[S.sel.name] || {};
        const p = a.pos || [0, 0];
        if (f === "bx") a.pos = [v, p[1]];
        if (f === "by") a.pos = [p[0], v];
        if (f === "br") a.rot = v;
        markDirty();
      } else if (f === "pz"){
        S.pieces[S.sel.name].z = Math.round(v); S.dirtyPieces.add(S.sel.name); markDirty();
      } else {
        touchPiece(S.sel.name, q => {
          if (f === "px") q.pivot = [v, q.pivot[1]];
          if (f === "py") q.pivot = [q.pivot[0], v];
          if (f === "pr") q.rot_offset = v;
          if (f === "ps") q.scale = Math.max(0.01, v);
        });
      }
    });
    box.querySelectorAll("button[data-act]").forEach(btn => btn.onclick = () => {
      const act = btn.dataset.act;
      if (S.mode === "animate"){
        if (act === "key") keyNow();
        if (act === "delkey") deleteKey();
        if (act === "prevkey") jumpKey(-1);
        if (act === "nextkey") jumpKey(1);
        if (act === "resetbone") resetBoneInClip(false);
        if (act === "resetclip") resetBoneInClip(true);
        return;
      }
      if (act === "regen"){ regen(S.sel.name, (box.querySelector(".ce-note") || {}).value || ""); return; }
      if (act === "resetbone"){
        S.adj[S.sel.name] = clone(S.saved.adj[S.sel.name] || {});
        markDirty();
      } else {
        const base = S.saved.pieces;
        touchPiece(S.sel.name, q => {
          const name = Object.keys(S.pieces).find(k => S.pieces[k] === q);
          const z = q.z;
          Object.assign(q, clone(base[name]), { z });
        });
      }
    });
  }

  /* ----------------------------------------------------------------- I/O */
  async function j(url, opts){
    const r = await fetch(url, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok || body.ok === false){
      const d = body.detail;
      throw new Error(body.error || (typeof d === "string" ? d : JSON.stringify(d || "")) || `HTTP ${r.status}`);
    }
    return body;
  }
  function say(msg, bad){ if ($.msg){ $.msg.textContent = msg; $.msg.style.color = bad ? "var(--danger, #f66)" : ""; } }

  async function listRigs(){
    try {
      const got = await j("/api/cutout/rigs");
      const cur = S && S.rel;
      $.rig.innerHTML = got.rigs.length
        ? got.rigs.map(r => `<option value="${E(r.rel)}">${E(r.name)} — ${E(r.rel)}</option>`).join("")
        : `<option value="">no .cutout.json in this project</option>`;
      if (cur) $.rig.value = cur;
      if (got.rigs.length && !S) open(got.rigs[0].rel);
    } catch(e){ say("could not list rigs: " + e.message, true); }
  }
  function adopt(data){
    const keep = S ? { clip: S.clip, t: S.t, zoom: S.zoom, pan: S.pan, mode: S.mode, rest: S.rest,
                       hidden: S.hidden, jointOnly: S.jointOnly, rel: S.rel } : null;
    const same = keep && keep.rel === data.rel;
    const pieces = {};
    for (const [slot, p] of Object.entries(data.parts))
      pieces[slot] = { pivot: p.pivot.slice(), rot_offset: p.rot_offset, scale: p.scale, z: p.z };
    data.overrides = data.overrides || {};
    S = {
      data, rel: data.rel, adj: clone(data.adjustments || {}),
      pieces, saved: { adj: clone(data.adjustments || {}), pieces: clone(pieces) },
      bones: Object.fromEntries(data.bones.map(b => [b.name, b])),
      img: {}, alpha: {}, dirtyPieces: new Set(), dirty: false,
      clipDirty: {}, clipReset: {}, clipResetAll: new Set(),
      hidden: same ? keep.hidden : new Set(),
      clip: keep && data.clips[keep.clip] ? keep.clip : (data.clips.idle ? "idle" : Object.keys(data.clips)[0]),
      t: keep ? keep.t : 0, zoom: keep ? keep.zoom : 2.2, pan: keep ? keep.pan : [0, 0],
      mode: keep ? keep.mode : "setup", rest: keep ? keep.rest : true,
      jointOnly: keep ? keep.jointOnly : false,
      playing: false, sel: null, showJoints: $.joints ? $.joints.checked : true, world: {},
      undo: [], redo: [], pending: false,
    };
    S.base = snap();
    for (const [slot, p] of Object.entries(data.parts)){
      const img = new Image();
      img.onload = img.onerror = () => render();
      img.src = p.url + "&v=" + Date.now();
      S.img[slot] = img;
    }
    $.save.disabled = true;
    syncUndo();
    $.play.textContent = "play";
    $.time.max = String((data.clips[S.clip] || {}).length || 1);
    $.clips.innerHTML = Object.keys(data.clips).map(n =>
      `<button class="ce-clip${n === S.clip ? " on" : ""}" data-clip="${E(n)}">${E(n)}${data.overrides[n] ? " *" : ""}</button>`).join("");
    $.clips.querySelectorAll("button").forEach(b => b.onclick = () => setClip(b.dataset.clip));
    syncMode();
    syncInspector();
    render();
  }
  async function open(rel){
    if (!rel) return;
    if (S && S.dirty && !confirm("Unsaved edits on this rig. Discard them?")) { $.rig.value = S.rel; return; }
    try {
      say("loading…");
      adopt(await j("/api/cutout/rig?rel=" + encodeURIComponent(rel)));
      if (![...$.rig.options].some(o => o.value === rel)) await listRigs();
      $.rig.value = rel;
      say("");
    } catch(e){ say("could not load: " + e.message, true); }
  }
  async function save(){
    if (!S || !S.dirty) return;
    const pieces = {};
    for (const s of S.dirtyPieces) pieces[s] = S.pieces[s];
    const clips = {};
    for (const [clip, bones] of Object.entries(S.clipDirty)){
      for (const bone of bones){
        const rotKeys = (((S.data.clips[clip] || {}).tracks || {})[bone] || {}).rot;
        if (rotKeys) (clips[clip] = clips[clip] || {})[bone] = { rot: rotKeys };
      }
    }
    const reset = {};
    for (const [clip, bones] of Object.entries(S.clipReset)) if (bones.length) reset[clip] = bones;
    for (const clip of S.clipResetAll) reset[clip] = [];
    try {
      say("saving…");
      const got = await j("/api/cutout/save", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel: S.rel, adjustments: S.adj, pieces, clips, clip_reset: reset }) });
      const sel = S.sel;
      S.dirty = false;
      adopt(got);
      S.sel = sel; syncInspector(); render();
      say(got.emitted ? `saved; Godot scene re-emitted (${got.scene})` : "saved (the scene was not re-emitted)");
    } catch(e){ say("save failed: " + e.message, true); }
  }
  async function regen(slot, note){
    if (!S) return;
    if (S.dirty){ say("save first: a regenerate reloads the rig from disk", true); return; }
    const near = S.data.parts[slot].reuse_of || slot;
    if (!confirm(`Regenerate ${near}? This is one paid image` + (near === "torso" ? " plus one edit" : "") + ".")) return;
    say(`regenerating ${near} (30\u2013120 s)\u2026`);
    try {
      const got = await j("/api/cutout/regen", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel: S.rel, slot, note }) });
      const sel = S.sel;
      adopt(got);
      S.sel = sel; syncInspector(); render();
      const flags = (got.regen && got.regen.flags) || [];
      say(flags.length ? `${near} regenerated; check it: ${flags.join("; ")}` : `${near} regenerated`, !!flags.length);
    } catch(e){ say("regenerate failed: " + e.message, true); }
  }
  async function proof(){
    if (!S) return;
    if (S.dirty){ say("save first: the engine renders what is on disk", true); return; }
    say("rendering in the engine (30–90 s)…");
    $.proof.disabled = true;
    try {
      const got = await j("/api/cutout/proof", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel: S.rel }) });
      $.shot.innerHTML = `<img src="${E(got.url)}&v=${Date.now()}" alt="engine proof">`;
      $.shot.hidden = false;
      say("engine proof is below the strip");
    } catch(e){ say("proof failed: " + e.message, true); }
    $.proof.disabled = false;
  }

  /* ------------------------------------------------------------ controls */
  function setClip(name){
    S.clip = name;
    const len = (S.data.clips[name] || {}).length || 1;
    S.t = Math.min(S.t, len);
    $.time.max = String(len);
    $.clips.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.clip === name));
    if (S.mode === "setup" && S.rest){ S.rest = false; syncMode(); }
    syncInspector();
    render();
  }
  function setMode(mode){
    if (!S) return;
    S.mode = mode;
    if (mode === "animate") S.rest = false;
    syncMode(); syncInspector(); render();
  }
  function syncMode(){
    if (!S) return;
    $.modeSetup.classList.toggle("on", S.mode === "setup");
    $.modeAnim.classList.toggle("on", S.mode === "animate");
    $.setupOpts.hidden = S.mode !== "setup";
    $.animOpts.hidden = S.mode !== "animate";
    $.rest.checked = !!S.rest;
    $.jointOnly.checked = !!S.jointOnly;
  }
  let _raf = 0, _last = 0;
  function tick(ts){
    if (!S || !S.playing || !visible(_host)){ _raf = 0; if (S) { S.playing = false; $.play.textContent = "play"; } return; }
    const dt = _last ? (ts - _last) / 1000 : 0; _last = ts;
    const c = S.data.clips[S.clip] || {};
    S.t += dt;
    if (S.t > (c.length || 1)) S.t = c.loop ? S.t % c.length : 0;
    render();
    _raf = requestAnimationFrame(tick);
  }
  function togglePlay(){
    if (!S) return;
    if (S.mode === "setup" && S.rest){ S.rest = false; syncMode(); }
    S.playing = !S.playing; _last = 0;
    $.play.textContent = S.playing ? "pause" : "play";
    if (S.playing && !_raf) _raf = requestAnimationFrame(tick);
  }

  function wire(){
    const cv = $.canvas;
    let drag = null;
    const pos = ev => { const r = cv.getBoundingClientRect(); return [(ev.clientX - r.left) * cv.width / r.width, (ev.clientY - r.top) * cv.height / r.height]; };
    cv.onmousedown = ev => {
      if (!S) return;
      const [x, y] = pos(ev);
      if (ev.button === 1){ drag = { pan: true, x, y }; ev.preventDefault(); return; }
      const hit = pick(x, y);
      if (hit && hit.gizmo){
        const g = gizmo();
        drag = { x, y, gizmo: hit.gizmo, ang: Math.atan2(y - g.jy, x - g.jx), alt: ev.altKey };
        _dragging = true;
        return;
      }
      S.sel = hit;
      syncInspector(); render();
      if (S.sel){ drag = { x, y, shift: ev.shiftKey, alt: ev.altKey }; _dragging = true; }
    };
    window.addEventListener("mousemove", ev => {
      if (!drag || !S) return;
      const [x, y] = pos(ev);
      const dx = x - drag.x, dy = y - drag.y;
      drag.x = x; drag.y = y;
      if (drag.pan){ S.pan = [S.pan[0] + dx, S.pan[1] + dy]; render(); return; }
      if (!S.sel) return;
      const wasJoint = S.jointOnly;
      if (drag.alt) S.jointOnly = true;
      try {
        if (drag.gizmo === "rotate" || drag.shift){
          let da;
          if (drag.gizmo === "rotate"){
            const g = gizmo();
            const a = Math.atan2(y - g.jy, x - g.jx);
            da = wrap(a - drag.ang); drag.ang = a;
          } else da = dx * 0.5 * DEG;
          if (S.mode === "animate" || S.sel.kind === "bone") turnBone(selBone(), da);
          else touchPiece(S.sel.name, q => { q.rot_offset = +(q.rot_offset - da / DEG).toFixed(2); });
        } else if (S.mode === "animate"){
          // In animate mode a drag on the body turns the bone too: keying
          // translation would fight the floor solve.
          const g = gizmo();
          if (g){ const a0 = Math.atan2(y - dy - g.jy, x - dx - g.jx), a1 = Math.atan2(y - g.jy, x - g.jx); turnBone(selBone(), wrap(a1 - a0)); }
        } else if (drag.gizmo === "move" || S.sel.kind === "bone") moveBone(selBone(), dx, dy);
        else movePiece(S.sel.name, dx, dy);
      } finally { S.jointOnly = wasJoint; }
    });
    window.addEventListener("mouseup", () => {
      drag = null;
      if (_dragging){ _dragging = false; commit(); }
    });
    cv.onwheel = ev => {
      if (!S) return;
      ev.preventDefault();
      S.zoom = Math.max(0.5, Math.min(8, S.zoom * (ev.deltaY < 0 ? 1.1 : 1 / 1.1)));
      render();
    };
    $.strip.onclick = ev => {
      if (!S) return;
      const r = $.strip.getBoundingClientRect();
      const names = Object.keys(S.data.clips);
      const i = Math.floor((ev.clientX - r.left) / r.width * names.length);
      if (names[i]){ S.t = stripTime(names[i]); setClip(names[i]); }
    };
    $.keys.onmousedown = ev => {
      if (!S) return;
      const r = $.keys.getBoundingClientRect();
      const len = curClip().length || 1;
      const f = ((ev.clientX - r.left) * $.keys.width / r.width - 8) / ($.keys.width - 16);
      S.t = Math.max(0, Math.min(len, f * len));
      if (S.mode === "setup" && S.rest){ S.rest = false; syncMode(); }
      syncInspector(); render();
    };
    $.time.oninput = () => {
      if (!S) return;
      S.t = parseFloat($.time.value) || 0;
      if (S.mode === "setup" && S.rest){ S.rest = false; syncMode(); }
      syncInspector(); render();
    };
    $.rig.onchange = () => open($.rig.value);
    $.play.onclick = togglePlay;
    $.undo.onclick = undo;
    $.redo.onclick = redo;
    $.save.onclick = save;
    $.proof.onclick = proof;
    $.modeSetup.onclick = () => setMode("setup");
    $.modeAnim.onclick = () => setMode("animate");
    $.rest.onchange = () => { if (S){ S.rest = $.rest.checked; render(); } };
    $.jointOnly.onchange = () => { if (S){ S.jointOnly = $.jointOnly.checked; render(); } };
    $.joints.onchange = () => { if (S){ S.showJoints = $.joints.checked; render(); } };
    $.revert.onclick = () => { if (S && (!S.dirty || confirm("Throw away the unsaved edits?"))){ S.dirty = false; open(S.rel); } };
    window.addEventListener("keydown", ev => {
      if (!S || !visible(_host)) return;
      if (/INPUT|TEXTAREA|SELECT/.test((ev.target && ev.target.tagName) || "")) return;
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s"){ ev.preventDefault(); save(); return; }
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z"){ ev.preventDefault(); ev.shiftKey ? redo() : undo(); return; }
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "y"){ ev.preventDefault(); redo(); return; }
      if (ev.key === " "){ ev.preventDefault(); togglePlay(); return; }
      if (S.mode === "animate"){
        if (ev.key === "k" || ev.key === "K"){ keyNow(); return; }
        if (ev.key === "Delete" || ev.key === "Backspace"){ ev.preventDefault(); deleteKey(); return; }
        if (ev.key === ","){ jumpKey(-1); return; }
        if (ev.key === "."){ jumpKey(1); return; }
      }
      if (!S.sel) return;
      if (S.mode === "setup" && S.sel.kind === "piece" && (ev.key === "PageUp" || ev.key === "PageDown")){
        ev.preventDefault(); moveLayer(S.sel.name, ev.key === "PageUp" ? -1 : 1); return;
      }
      if (ev.key === "[" || ev.key === "]"){
        const d = (ev.key === "[" ? -1 : 1) * DEG;
        if (S.mode === "animate" || S.sel.kind === "bone") turnBone(selBone(), d);
        else touchPiece(S.sel.name, q => { q.rot_offset = +(q.rot_offset - d / DEG).toFixed(2); });
        return;
      }
      if (S.mode !== "setup") return;
      const step = ev.shiftKey ? 5 : 1;
      const arrows = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
      if (arrows[ev.key]){
        ev.preventDefault();
        const [dx, dy] = arrows[ev.key];
        const wasJoint = S.jointOnly;
        if (ev.altKey) S.jointOnly = true;
        // A nudge is one TEMPLATE pixel, whatever the zoom.
        if (S.sel.kind === "bone") moveBone(S.sel.name, dx * S.zoom, dy * S.zoom);
        else movePiece(S.sel.name, dx * S.zoom, dy * S.zoom);
        S.jointOnly = wasJoint;
      }
    });
    window.addEventListener("beforeunload", ev => { if (S && S.dirty){ ev.preventDefault(); ev.returnValue = ""; } });
  }

  function injectStyle(){
    if (document.getElementById("ce-style")) return;
    const st = document.createElement("style");
    st.id = "ce-style";
    st.textContent = `
      .ce-wrap{display:flex;flex-direction:column;gap:8px;height:100%;min-height:0;padding:10px;box-sizing:border-box;overflow:auto}
      .ce-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
      .ce-bar select{max-width:380px}
      .ce-seg{display:inline-flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
      .ce-seg button{font:12px system-ui;padding:4px 10px;border:0;background:transparent;color:inherit;cursor:pointer}
      .ce-seg button.on{background:var(--accent);color:var(--accent-fg, #1a0a05)}
      .ce-opt{font:12px system-ui;display:inline-flex;gap:4px;align-items:center}
      .ce-clips{display:flex;flex-wrap:wrap;gap:4px}
      .ce-clip{font:12px system-ui;padding:3px 8px;border:1px solid var(--line);background:var(--surface-2, #1a1d24);color:inherit;border-radius:6px;cursor:pointer}
      .ce-clip.on{border-color:var(--accent);color:var(--accent)}
      .ce-main{display:flex;gap:8px;flex:none}
      .ce-canvas{flex:1;min-width:0;width:100%;height:auto;align-self:flex-start;aspect-ratio:900/560;background:#d0ccc4;border-radius:8px;border:1px solid var(--line);cursor:crosshair}
      .ce-side{width:280px;flex:none;display:flex;flex-direction:column;gap:8px;min-height:0}
      .ce-insp{display:flex;flex-direction:column;gap:6px;font:13px system-ui;padding:8px;border:1px solid var(--line);border-radius:8px}
      .ce-insp label{display:flex;justify-content:space-between;gap:6px;align-items:center}
      .ce-insp input{width:100px}
      .ce-note{width:100%;box-sizing:border-box;font:12px system-ui;background:var(--surface-2, #1a1d24);color:inherit;border:1px solid var(--line);border-radius:6px;padding:4px}
      .ce-row{display:flex;gap:6px;flex-wrap:wrap}
      .ce-h{font-weight:600;margin-bottom:4px}
      .ce-sub,.ce-hint{opacity:.75;font-size:12px;line-height:1.45;font-weight:400}
      .ce-layers{display:flex;flex-direction:column;gap:2px;font:12px system-ui;padding:8px;border:1px solid var(--line);border-radius:8px;overflow:auto;max-height:380px}
      .ce-layer{display:flex;align-items:center;gap:6px;padding:3px 4px;border-radius:5px;cursor:grab;border:1px solid transparent}
      .ce-layer:hover{background:var(--surface-2, #1a1d24)}
      .ce-layer.on{border-color:var(--accent);background:var(--accent-wash, rgba(255,106,61,.07))}
      .ce-layer.hid .ce-lname{opacity:.45;text-decoration:line-through}
      .ce-layer.over{border-top:2px solid var(--accent)}
      .ce-layer.drag{opacity:.5}
      .ce-lname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .ce-lname i{opacity:.6;font-style:normal}
      .ce-lz{opacity:.6;font-variant-numeric:tabular-nums;min-width:18px;text-align:right}
      .ce-eye,.ce-lbtn{background:none;border:0;color:inherit;cursor:pointer;padding:0 3px;font-size:11px;opacity:.8}
      .ce-eye:hover,.ce-lbtn:hover{opacity:1;color:var(--accent)}
      .ce-keysrow{display:flex;gap:8px;align-items:center;font:12px system-ui}
      .ce-keys{flex:1;height:28px;background:var(--surface-2, #1a1d24);border:1px solid var(--line);border-radius:6px;cursor:pointer}
      .ce-strip{width:100%;aspect-ratio:1800/170;background:#d0ccc4;border-radius:8px;border:1px solid var(--line);cursor:pointer;flex:none}
      .ce-time{display:flex;gap:8px;align-items:center;font:12px system-ui}
      .ce-time input{flex:1}
      .ce-msg{font:12px system-ui;opacity:.85;margin-left:auto}
      .ce-shot img{max-width:100%;border-radius:8px;border:1px solid var(--line)}
    `;
    document.head.appendChild(st);
  }

  function embed(host){
    injectStyle();
    host.innerHTML = `
      <div class="ce-wrap">
        <div class="ce-bar">
          <select class="ce-rig" aria-label="rig"></select>
          <span class="ce-seg"><button class="ce-m-setup on">Setup</button><button class="ce-m-anim">Animate</button></span>
          <span class="ce-setup-opts">
            <label class="ce-opt"><input type="checkbox" class="ce-rest" checked> rest pose</label>
            <label class="ce-opt"><input type="checkbox" class="ce-jonly"> joint only (Alt)</label>
          </span>
          <span class="ce-anim-opts" hidden><span class="ce-opt">ring or drag = key the bone at the playhead</span></span>
          <label class="ce-opt"><input type="checkbox" class="ce-joints" checked> bones</label>
          <button class="qbtn ce-play">play</button>
          <button class="qbtn ce-undo" title="undo (Ctrl+Z)" disabled>\u21B6 undo</button>
          <button class="qbtn ce-redo" title="redo (Ctrl+Shift+Z / Ctrl+Y)" disabled>\u21B7 redo</button>
          <button class="qbtn ce-revert">revert</button>
          <button class="qbtn primary ce-save" disabled>save</button>
          <button class="qbtn ce-proof">render in engine</button>
          <span class="ce-msg"></span>
        </div>
        <div class="ce-clips"></div>
        <div class="ce-main">
          <canvas class="ce-canvas" width="900" height="560"></canvas>
          <div class="ce-side">
            <div class="ce-insp"></div>
            <div class="ce-layers"></div>
          </div>
        </div>
        <div class="ce-time"><input type="range" class="ce-t" min="0" max="1" step="0.01" value="0"><span class="ce-tlab"></span></div>
        <div class="ce-keysrow"><canvas class="ce-keys" width="1200" height="28"></canvas><span class="ce-keylab"></span></div>
        <canvas class="ce-strip" width="1800" height="170"></canvas>
        <div class="ce-shot" hidden></div>
      </div>`;
    const q = s => host.querySelector(s);
    Object.assign($, { rig: q(".ce-rig"), play: q(".ce-play"), joints: q(".ce-joints"),
      revert: q(".ce-revert"), save: q(".ce-save"), proof: q(".ce-proof"), msg: q(".ce-msg"),
      clips: q(".ce-clips"), canvas: q(".ce-canvas"), insp: q(".ce-insp"), layers: q(".ce-layers"),
      time: q(".ce-t"), tlab: q(".ce-tlab"), strip: q(".ce-strip"), shot: q(".ce-shot"),
      keys: q(".ce-keys"), keylab: q(".ce-keylab"),
      modeSetup: q(".ce-m-setup"), modeAnim: q(".ce-m-anim"),
      setupOpts: q(".ce-setup-opts"), animOpts: q(".ce-anim-opts"),
      rest: q(".ce-rest"), jointOnly: q(".ce-jonly"),
      undo: q(".ce-undo"), redo: q(".ce-redo") });
    wire();
    syncInspector();
    listRigs();
  }

  function activate(){
    try {
      const host = document.getElementById("ce-page");
      if (!host) return false;
      if (_host !== host){ _host = host; embed(host); }
      else { listRigs(); if (S) render(); }
      return true;
    } catch(e){ console.error("CutoutEdit", e); return false; }
  }

  return { activate, open, save, setMode, undo, redo, get state(){ return S; } };
})();
