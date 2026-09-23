/* cutoutedit.js — the cutout RIG editor: see every pose, drag the joint.
 *
 * WHY THIS EXISTS (user directive, 2026-09-22). Generated rigs land close and
 * then fail on the last few pixels: a shoulder level with the collar, arms
 * that do not line up with the torso, a piece hanging a little off its bone.
 * Judging that from a screenshot and re-rolling a model is slow, costs money
 * and guesses. Here a person grabs the joint and moves it, watching all twelve
 * clips change as they drag, and saves it into the rig.
 *
 * WHAT IT EDITS, and nothing else:
 *   - a BONE's rest position and rotation (the rig's `adjustments`: they survive
 *     every clip, because the clips are deltas on the rest pose);
 *   - a PIECE's pivot (where it hangs from its bone), rotation and scale. A far
 *     piece that reuses a near one follows it.
 *
 * WHAT IT PLAYS is the server's bake (routes/cutoutedit.py -> cutoutwire), the
 * same numbers Godot gets: mirrored clips, the floor solve, eased keys (Godot's
 * ease(t, -2), reproduced below). Live edits are added on top while dragging;
 * Save writes the .cutout.json, re-emits the scene, and reloads the bake.
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
  const visible = el => !!el && el.isConnected && el.getClientRects().length > 0;

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
    // What the live edit adds on top of the SAVED bake, in Godot units.
    const cur = S.adj[bone] || {}, was = S.saved.adj[bone] || {};
    const cp = cur.pos || [0, 0], wp = was.pos || [0, 0];
    return { dx: cp[0] - wp[0], dy: -(cp[1] - wp[1]),
             dr: -((cur.rot || 0) - (was.rot || 0)) * DEG };
  }
  function pose(clip, t){
    const c = S.data.clips[clip] || {};
    const local = {};
    for (const b of S.data.bones){
      const trk = (c.tracks || {})[b.name] || {};
      const p = sample(trk.pos, t, c.length, c.loop, S.data.ease) || b.rest.pos;
      const r = sample(trk.rot, t, c.length, c.loop, S.data.ease);
      const d = editDelta(b.name);
      local[b.name] = [p[0] + d.dx, p[1] + d.dy, (r === null ? b.rest.rot : r) + d.dr];
    }
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

  /* -------------------------------------------------------------- drawing */
  function drawRig(ctx, view, clip, t, opts){
    const world = pose(clip, t);
    const order = Object.keys(S.data.parts).sort((a, b) => S.data.parts[a].z - S.data.parts[b].z);
    for (const slot of order){
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
      for (const b of S.data.bones){
        const [x, y] = apply(mul(view, world[b.name]), 0, 0);
        const on = opts.sel && opts.sel.kind === "bone" && opts.sel.name === b.name;
        ctx.beginPath();
        ctx.arc(x, y, on ? 6 : 4, 0, Math.PI * 2);
        ctx.fillStyle = on ? "#ffcc33" : "rgba(80,200,255,.85)";
        ctx.fill();
        ctx.strokeStyle = "#0008"; ctx.lineWidth = 1; ctx.stroke();
      }
    }
    return world;
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
    // Ground, and the player-height bar at the rig's own scale.
    ctx.strokeStyle = "rgba(120,130,140,.7)"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(0, view[5]); ctx.lineTo(c.width, view[5]); ctx.stroke();
    const fit = S.data.game_fit || {};
    if (S.data.player_height_px && fit.scale){
      const hh = S.data.player_height_px / fit.scale * S.zoom;
      ctx.fillStyle = "rgba(128,128,140,.35)";
      ctx.fillRect(view[4] + 75 * S.zoom, view[5] - hh, 10, hh);
    }
    S.world = drawRig(ctx, view, S.clip, S.t, { joints: S.showJoints, sel: S.sel });
    renderStrip();
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
    names.forEach((n, i) => {
      if (n === S.clip){ ctx.fillStyle = "rgba(255,106,61,.14)"; ctx.fillRect(i * cw, 0, cw, c.height); }
      const view = [z, 0, 0, z, i * cw + cw / 2, c.height - 22];
      ctx.strokeStyle = "rgba(120,130,140,.5)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(i * cw + 4, view[5]); ctx.lineTo((i + 1) * cw - 4, view[5]); ctx.stroke();
      if (!(S.data.clips[n] || {}).error) drawRig(ctx, view, n, n === S.clip ? S.t : stripTime(n));
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillStyle = "#556";
      ctx.font = "11px system-ui"; ctx.textAlign = "center";
      ctx.fillText(n, i * cw + cw / 2, c.height - 6);
    });
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
    const view = mainView();
    if (S.showJoints){
      for (const b of S.data.bones){
        const [x, y] = apply(mul(view, S.world[b.name]), 0, 0);
        if (Math.hypot(mx - x, my - y) <= 7) return { kind: "bone", name: b.name };
      }
    }
    const order = Object.keys(S.data.parts).sort((a, b) => S.data.parts[b].z - S.data.parts[a].z);
    for (const slot of order){
      const m = mul(view, pieceMatrix(S.world, slot));
      const [lx, ly] = apply(inv(m), mx, my);
      if (alphaAt(slot, lx, ly) > 40) return { kind: "piece", name: slot };
    }
    return null;
  }

  /* --------------------------------------------------------------- edits */
  function linked(slot){
    // The piece and every far piece that reuses it.
    return [slot, ...Object.keys(S.data.parts).filter(s => S.data.parts[s].reuse_of === slot)];
  }
  function touchPiece(slot, fn){
    const root = S.data.parts[slot].reuse_of || slot;   // edit the near one
    for (const s of linked(root)){ fn(S.pieces[s]); S.dirtyPieces.add(s); }
    markDirty();
  }
  function moveBone(name, sx, sy){
    const b = S.bones[name];
    const parentW = b.parent ? mul(mainView(), S.world[b.parent]) : mainView();
    const [gx, gy] = applyV(inv(parentW), sx, sy);
    const a = S.adj[name] = S.adj[name] || {};
    const p = a.pos || [0, 0];
    a.pos = [+(p[0] + gx).toFixed(3), +(p[1] - gy).toFixed(3)];
    markDirty();
  }
  function turnBone(name, deg){
    const a = S.adj[name] = S.adj[name] || {};
    a.rot = +(((a.rot || 0) + deg)).toFixed(2);
    markDirty();
  }
  function movePiece(slot, sx, sy){
    const info = S.data.parts[slot], p = S.pieces[slot];
    const m = mul(mul(mul(mainView(), S.world[info.bone]), rot(-p.rot_offset * DEG)), scl(p.scale));
    const [lx, ly] = applyV(inv(m), sx, sy);
    const [w, h] = info.size;
    touchPiece(slot, q => { q.pivot = [+(q.pivot[0] - lx / w).toFixed(5), +(q.pivot[1] + ly / h).toFixed(5)]; });
  }
  function markDirty(){ S.dirty = true; $.save.disabled = false; syncInspector(); render(); }

  /* ------------------------------------------------------------ inspector */
  function syncInspector(){
    const box = $.insp;
    if (!S || !S.sel){
      box.innerHTML = `<div class="ce-hint">Click a <b>joint</b> (blue dot) to move a bone, or a <b>piece</b> to move where it hangs.<br><br>
        Drag to move. <b>Shift</b>-drag to rotate. Arrow keys nudge 1px (Shift: 5px), <b>[ ]</b> rotate 1°.
        Wheel zooms, middle-drag pans, <b>Space</b> plays. <b>Ctrl+S</b> saves.<br><br>
        Every clip in the strip below updates as you drag. Click one to open it.</div>`;
      return;
    }
    if (S.sel.kind === "bone"){
      const a = S.adj[S.sel.name] || {};
      const p = a.pos || [0, 0];
      box.innerHTML = `<div class="ce-h">bone <b>${E(S.sel.name)}</b></div>
        <label>x <input type="number" step="0.5" data-f="bx" value="${p[0]}"></label>
        <label>y <input type="number" step="0.5" data-f="by" value="${p[1]}"></label>
        <label>rot° <input type="number" step="1" data-f="br" value="${a.rot || 0}"></label>
        <div class="ce-sub">offset from the template rest pose (px, +y up). Moves this joint in every clip.</div>
        <button class="qbtn" data-act="resetbone">reset bone</button>`;
    } else {
      const p = S.pieces[S.sel.name], info = S.data.parts[S.sel.name];
      const far = info.reuse_of ? `<div class="ce-sub">reuses <b>${E(info.reuse_of)}</b>: edits go to that piece and this follows</div>` : "";
      box.innerHTML = `<div class="ce-h">piece <b>${E(S.sel.name)}</b> on <b>${E(info.bone)}</b></div>${far}
        <label>pivot x <input type="number" step="0.01" data-f="px" value="${p.pivot[0]}"></label>
        <label>pivot y <input type="number" step="0.01" data-f="py" value="${p.pivot[1]}"></label>
        <label>rot° <input type="number" step="1" data-f="pr" value="${p.rot_offset}"></label>
        <label>scale <input type="number" step="0.01" data-f="ps" value="${p.scale}"></label>
        <div class="ce-sub">pivot = where it hangs from its bone, as a fraction of the image (y from the bottom)</div>
        <button class="qbtn" data-act="resetpiece">reset piece</button>`;
    }
    box.querySelectorAll("input").forEach(inp => inp.onchange = () => {
      const v = parseFloat(inp.value);
      if (!isFinite(v)) return;
      const f = inp.dataset.f;
      if (S.sel.kind === "bone"){
        const a = S.adj[S.sel.name] = S.adj[S.sel.name] || {};
        const p = a.pos || [0, 0];
        if (f === "bx") a.pos = [v, p[1]];
        if (f === "by") a.pos = [p[0], v];
        if (f === "br") a.rot = v;
        markDirty();
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
      if (btn.dataset.act === "resetbone"){
        S.adj[S.sel.name] = JSON.parse(JSON.stringify(S.saved.adj[S.sel.name] || {}));
        markDirty();
      } else {
        touchPiece(S.sel.name, q => {
          const name = Object.keys(S.pieces).find(k => S.pieces[k] === q);
          Object.assign(q, JSON.parse(JSON.stringify(S.saved.pieces[name])));
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
      $.rig.innerHTML = got.rigs.length
        ? got.rigs.map(r => `<option value="${E(r.rel)}">${E(r.name)} — ${E(r.rel)}</option>`).join("")
        : `<option value="">no .cutout.json in this project</option>`;
      if (got.rigs.length && !S) open(got.rigs[0].rel);
    } catch(e){ say("could not list rigs: " + e.message, true); }
  }
  function adopt(data){
    const prevClip = S && S.clip, prevT = S ? S.t : 0, zoom = S ? S.zoom : 2.2, pan = S ? S.pan : [0, 0];
    const pieces = {};
    for (const [slot, p] of Object.entries(data.parts))
      pieces[slot] = { pivot: p.pivot.slice(), rot_offset: p.rot_offset, scale: p.scale };
    S = {
      data, rel: data.rel, adj: JSON.parse(JSON.stringify(data.adjustments || {})),
      pieces, saved: { adj: JSON.parse(JSON.stringify(data.adjustments || {})),
                       pieces: JSON.parse(JSON.stringify(pieces)) },
      bones: Object.fromEntries(data.bones.map(b => [b.name, b])),
      img: {}, alpha: {}, dirtyPieces: new Set(), dirty: false,
      clip: prevClip && data.clips[prevClip] ? prevClip : (data.clips.idle ? "idle" : Object.keys(data.clips)[0]),
      t: prevT, zoom, pan, playing: false, sel: null, showJoints: $.joints ? $.joints.checked : true, world: {},
    };
    for (const [slot, p] of Object.entries(data.parts)){
      const img = new Image();
      img.onload = img.onerror = () => render();
      img.src = p.url + "&v=" + Date.now();
      S.img[slot] = img;
    }
    $.save.disabled = true;
    $.play.textContent = "play";
    $.time.max = String((data.clips[S.clip] || {}).length || 1);
    $.clips.innerHTML = Object.keys(data.clips).map(n =>
      `<button class="ce-clip${n === S.clip ? " on" : ""}" data-clip="${E(n)}">${E(n)}</button>`).join("");
    $.clips.querySelectorAll("button").forEach(b => b.onclick = () => setClip(b.dataset.clip));
    syncInspector();
    render();
  }
  async function open(rel){
    if (!rel) return;
    if (S && S.dirty && !confirm("Unsaved edits on this rig. Discard them?")) { $.rig.value = S.rel; return; }
    try {
      say("loading…");
      adopt(await j("/api/cutout/rig?rel=" + encodeURIComponent(rel)));
      // A rig made after the list was drawn is not in it yet.
      if (![...$.rig.options].some(o => o.value === rel)) await listRigs();
      $.rig.value = rel;
      say("");
    } catch(e){ say("could not load: " + e.message, true); }
  }
  async function save(){
    if (!S || !S.dirty) return;
    const pieces = {};
    for (const s of S.dirtyPieces) pieces[s] = S.pieces[s];
    try {
      say("saving…");
      const got = await j("/api/cutout/save", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel: S.rel, adjustments: S.adj, pieces }) });
      const sel = S.sel;
      S.dirty = false;
      adopt(got);
      S.sel = sel; syncInspector(); render();
      say(got.emitted ? `saved, and the Godot scene re-emitted (${got.scene})` : "saved (the scene was not re-emitted)");
    } catch(e){ say("save failed: " + e.message, true); }
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
    render();
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
      S.sel = pick(x, y);
      syncInspector(); render();
      if (S.sel) drag = { x, y, shift: ev.shiftKey };
    };
    window.addEventListener("mousemove", ev => {
      if (!drag || !S) return;
      const [x, y] = pos(ev);
      const dx = x - drag.x, dy = y - drag.y;
      drag.x = x; drag.y = y;
      if (drag.pan){ S.pan = [S.pan[0] + dx, S.pan[1] + dy]; render(); return; }
      if (!S.sel) return;
      if (drag.shift){
        if (S.sel.kind === "bone") turnBone(S.sel.name, dx * 0.5);
        else touchPiece(S.sel.name, q => { q.rot_offset = +(q.rot_offset + dx * 0.5).toFixed(2); });
      } else if (S.sel.kind === "bone") moveBone(S.sel.name, dx, dy);
      else movePiece(S.sel.name, dx, dy);
    });
    window.addEventListener("mouseup", () => { drag = null; });
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
    $.time.oninput = () => { if (S){ S.t = parseFloat($.time.value) || 0; render(); } };
    $.rig.onchange = () => open($.rig.value);
    $.play.onclick = togglePlay;
    $.save.onclick = save;
    $.proof.onclick = proof;
    $.joints.onchange = () => { if (S){ S.showJoints = $.joints.checked; render(); } };
    $.revert.onclick = () => { if (S && (!S.dirty || confirm("Throw away the unsaved edits?"))){ S.dirty = false; open(S.rel); } };
    window.addEventListener("keydown", ev => {
      if (!S || !visible(_host)) return;
      if (/INPUT|TEXTAREA|SELECT/.test((ev.target && ev.target.tagName) || "")) return;
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s"){ ev.preventDefault(); save(); return; }
      if (ev.key === " "){ ev.preventDefault(); togglePlay(); return; }
      if (!S.sel) return;
      const step = ev.shiftKey ? 5 : 1;
      const arrows = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
      if (arrows[ev.key]){
        ev.preventDefault();
        const [dx, dy] = arrows[ev.key];
        // A nudge is one TEMPLATE pixel, whatever the zoom.
        if (S.sel.kind === "bone") moveBone(S.sel.name, dx * S.zoom, dy * S.zoom);
        else movePiece(S.sel.name, dx * S.zoom, dy * S.zoom);
      } else if (ev.key === "[" || ev.key === "]"){
        const d = ev.key === "[" ? -1 : 1;
        if (S.sel.kind === "bone") turnBone(S.sel.name, d);
        else touchPiece(S.sel.name, q => { q.rot_offset = +(q.rot_offset + d).toFixed(2); });
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
      .ce-bar select{max-width:420px}
      .ce-clips{display:flex;flex-wrap:wrap;gap:4px}
      .ce-clip{font:12px system-ui;padding:3px 8px;border:1px solid var(--line);background:var(--surface-2, #1a1d24);color:inherit;border-radius:6px;cursor:pointer}
      .ce-clip.on{border-color:var(--accent);color:var(--accent)}
      .ce-main{display:flex;gap:8px;flex:none}
      .ce-canvas{flex:1;min-width:0;width:100%;aspect-ratio:900/560;background:#d0ccc4;border-radius:8px;border:1px solid var(--line);cursor:crosshair}
      .ce-insp{width:250px;flex:none;display:flex;flex-direction:column;gap:6px;font:13px system-ui;padding:8px;border:1px solid var(--line);border-radius:8px;overflow:auto}
      .ce-insp label{display:flex;justify-content:space-between;gap:6px;align-items:center}
      .ce-insp input{width:100px}
      .ce-h{font-weight:600;margin-bottom:4px}
      .ce-sub,.ce-hint{opacity:.75;font-size:12px;line-height:1.45}
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
          <button class="qbtn ce-play">play</button>
          <label style="font:12px system-ui"><input type="checkbox" class="ce-joints" checked> joints</label>
          <button class="qbtn ce-revert">revert</button>
          <button class="qbtn primary ce-save" disabled>save</button>
          <button class="qbtn ce-proof">render in engine</button>
          <span class="ce-msg"></span>
        </div>
        <div class="ce-clips"></div>
        <div class="ce-main">
          <canvas class="ce-canvas" width="900" height="560"></canvas>
          <div class="ce-insp"></div>
        </div>
        <div class="ce-time"><input type="range" class="ce-t" min="0" max="1" step="0.01" value="0"><span class="ce-tlab"></span></div>
        <canvas class="ce-strip" width="1800" height="170"></canvas>
        <div class="ce-shot" hidden></div>
      </div>`;
    const q = s => host.querySelector(s);
    Object.assign($, { rig: q(".ce-rig"), play: q(".ce-play"), joints: q(".ce-joints"),
      revert: q(".ce-revert"), save: q(".ce-save"), proof: q(".ce-proof"), msg: q(".ce-msg"),
      clips: q(".ce-clips"), canvas: q(".ce-canvas"), insp: q(".ce-insp"), time: q(".ce-t"),
      tlab: q(".ce-tlab"), strip: q(".ce-strip"), shot: q(".ce-shot") });
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

  return { activate, open, save, get state(){ return S; } };
})();
