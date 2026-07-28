/* sceneview.js — the scene as it actually looks, and editable there.
 *
 * The node graph answers "what is wired to what". It cannot answer "is the hat
 * on his head", and that is the question you have open when you are placing
 * things. So this composites the scene the way the engine does — every node at
 * its world transform, sprites showing their real atlas region, painted in
 * z-order inside the game's own viewport frame — and lets you drag, scale,
 * rotate and reorder directly on the picture.
 *
 * WHAT MAKES IT TRUE RATHER THAN APPROXIMATE:
 *   · transforms compose through ancestors, rotation included, so a child of a
 *     rotated parent lands where the engine puts it;
 *   · an AnimatedSprite2D draws ONE FRAME's region of its sheet, not the whole
 *     twelve-frame strip;
 *   · Sprite2D is centred on its position and a Control is not, which is the
 *     difference between a scene that lines up and one that is off by half a
 *     sprite everywhere.
 *
 * NOTHING HERE WRITES UNTIL YOU SAY SO. An earlier version committed each drag
 * on pointer-release, reasoning that a confirm dialog per drag is a tool nobody
 * uses. That is true and it was the wrong conclusion: moving something to look
 * at it is not a decision to change the game, and it produced twenty-two
 * unrequested writes to a live scene in one sitting. A backup does not make
 * that acceptable — it only makes it recoverable.
 *
 * So a drag changes the PICTURE. Edits stage as pending property writes, a bar
 * says how many are outstanding, and `apply` is the only thing that touches
 * disk: one confirmation, one pass, one backup per file. `discard` re-reads the
 * file and everything staged is gone. Leaving or switching scenes with work
 * outstanding asks first.
 */
window.SceneView = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const clamp = (v, lo, hi) => v < lo ? lo : v > hi ? hi : v;

  const ROLE_TINT = {
    character:"#ff9f43", enemy:"#ff6a3d", prop:"#c9a227", item:"#ffd166",
    layer:"#2ec4b6", visual:"#4aa3ff", collision:"#7c8695",
    controller:"#9a7bff", camera:"#57c7ff", audio:"#ff6ec7", fx:"#b06bff",
    ui:"#8bd450", marker:"#7c8695", instance:"#57c7ff", node:"#7c8695",
  };
  const HANDLES = [
    ["nw",0,0],["n",.5,0],["ne",1,0],["e",1,.5],
    ["se",1,1],["s",.5,1],["sw",0,1],["w",0,.5],
  ];
  const HANDLE_PX = 7;          // screen-space, so handles stay grabbable at any zoom

  let host = null, cv = null, ctx = null;
  let scene = null, list = null, sel = null;
  let view = { x: 0, y: 0, z: 1 };
  let opts = { grid: true, snap: 8, snapOn: true, showHidden: false,
               showBodies: true, outlines: true };
  let images = new Map();       // rel -> HTMLImageElement | null
  let drag = null, hover = null, busy = false;
  // path -> { name, keys: Map(property -> {value, prev}) }. Staged, never written.
  let pending = new Map();
  // View-only layer state. Hiding here never touches the scene's `visible`.
  let hiddenLayers = new Set(), isolated = null;

  /* ── styles ───────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("sceneview-style")) return;
    const s = document.createElement("style");
    s.id = "sceneview-style";
    s.textContent = [
      ".sv{display:flex;flex-direction:column;height:100%;min-height:0}",
      // One row that scrolls, never a stack that wraps. A wrapping toolbar
      // takes its height out of the canvas, and the canvas is the whole point
      // of this panel — at 1100px wide this was four rows deep.
      ".sv-bar{display:flex;align-items:center;gap:5px;padding:6px 9px;border-bottom:1px solid var(--seam);background:var(--iron);flex-wrap:nowrap;overflow-x:auto;flex:none;scrollbar-width:thin}",
      ".sv-bar>*{flex:none}",
      ".sv-l{font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--ash2)}",
      ".sv-b{padding:4px 9px;background:var(--plate);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font:inherit;font-size:11px;cursor:pointer}",
      ".sv-b:hover:not(:disabled){border-color:var(--ember)}",
      ".sv-b:disabled{opacity:.4;cursor:default}",
      ".sv-b.on{border-color:var(--ember);background:var(--plate2)}",
      ".sv-in{width:52px;background:var(--void);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font:inherit;font-size:11px;padding:3px 6px}",
      ".sv-layers{display:flex;align-items:center;gap:6px;padding:5px 9px;border-bottom:1px solid var(--seam);background:var(--iron);flex-wrap:wrap;flex:none}",
      ".sv-layer{display:inline-flex;align-items:center;border:1px solid var(--seam);border-radius:999px;overflow:hidden;font-family:var(--mono);font-size:10px}",
      ".sv-layer button{background:none;border:0;color:var(--ash);font:inherit;cursor:pointer;padding:3px 6px}",
      ".sv-layer .eye{color:var(--lt)}",
      ".sv-layer .nm{padding-right:2px}",
      ".sv-layer .ct{padding:0 7px 0 2px;color:var(--ash2);font-size:9px}",
      ".sv-layer:hover{border-color:var(--lt)}",
      ".sv-layer:hover .nm{color:var(--bone)}",
      ".sv-layer.off{opacity:.42}",
      ".sv-layer.off .nm{text-decoration:line-through}",
      ".sv-layer.solo{border-color:var(--lt);background:var(--plate)}",
      ".sv-layer.solo .nm{color:var(--bone)}",
      ".sv-stage{flex:1;position:relative;min-height:0;background:#0a0b0e;overflow:hidden}",
      ".sv-stage canvas{position:absolute;inset:0;width:100%;height:100%;touch-action:none}",
      ".sv-hud{position:absolute;left:9px;bottom:9px;font-family:var(--mono);font-size:10px;color:var(--ash2);background:rgba(10,11,14,.85);border:1px solid var(--seam);border-radius:6px;padding:3px 8px;pointer-events:none;white-space:pre}",
      ".sv-tip{position:absolute;right:9px;bottom:9px;font-family:var(--mono);font-size:9.5px;color:var(--ash2);background:rgba(10,11,14,.85);border:1px solid var(--seam);border-radius:6px;padding:3px 8px;pointer-events:none}",
      // Unmissable, because the alternative is writing to a live scene by
      // accident — which is exactly what this replaced.
      ".sv-pending{position:absolute;left:9px;right:9px;top:9px;display:flex;align-items:center;gap:9px;background:rgba(24,16,10,.96);border:1px solid var(--warn);border-radius:8px;padding:6px 10px;font-family:var(--mono);font-size:10.5px;color:var(--warn)}",
      ".sv-pending .dot{width:8px;height:8px;border-radius:50%;background:var(--warn);flex:none}",
      ".sv-pending .sv-b{color:var(--bone)}",
      ".sv-pending .sv-b.go{background:var(--ember);color:#111;border-color:var(--ember);font-weight:600}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── data ─────────────────────────────────────────────────────────────── */
  async function mount(el, sceneId){
    injectStyle();
    host = el; scene = sceneId;
    host.innerHTML = `
      <div class="sv">
        <div class="sv-bar">
          <button class="sv-b" onclick="SceneView.fit()" title="Fit the frame to the panel">⊡ fit</button>
          <span class="sv-l">snap</span>
          <button class="sv-b ${opts.snapOn?"on":""}" title="Snap moves to the grid — hold shift to override"
                  onclick="SceneView.toggle('snapOn')">${opts.snapOn?"on":"off"}</button>
          <input class="sv-in" type="number" min="1" max="256" value="${opts.snap}"
                 title="Grid size in pixels" onchange="SceneView.setSnap(this.value)">
          <span class="sv-l">show</span>
          <button class="sv-b ${opts.grid?"on":""}" title="Grid" onclick="SceneView.toggle('grid')">▦</button>
          <button class="sv-b ${opts.outlines?"on":""}" title="Node outlines" onclick="SceneView.toggle('outlines')">⬚</button>
          <button class="sv-b ${opts.showBodies?"on":""}" title="Bodies, collision and markers" onclick="SceneView.toggle('showBodies')">⬡</button>
          <button class="sv-b ${opts.showHidden?"on":""}" title="Nodes marked invisible" onclick="SceneView.toggle('showHidden')">◌</button>
          <span style="flex:1 1 auto;min-width:8px"></span>
          <button class="sv-b" onclick="SceneView.raise(1)" title="Bring forward">▲ z</button>
          <button class="sv-b" onclick="SceneView.raise(-1)" title="Send back">▼ z</button>
          <button class="sv-b" id="sv-undo" onclick="SceneView.undo()" title="Take back the last staged change — nothing has been written yet">↶</button>
          <button class="sv-b" onclick="SceneView.snapshot()"
                  title="Save this view as a PNG under .bgate_out/scene_shots">⤓ png</button>
        </div>
        <div class="sv-layers" id="sv-layers" hidden></div>
        <div class="sv-stage" id="sv-stage">
          <canvas id="sv-canvas"></canvas>
          <div class="sv-hud" id="sv-hud"></div>
          <div class="sv-tip" id="sv-tip">drag to move · handles scale · ring rotates · shift = free</div>
          <div class="sv-pending" id="sv-pending" hidden>
            <span class="dot"></span>
            <span id="sv-pending-n"></span>
            <span style="flex:1"></span>
            <button class="sv-b" onclick="SceneView.discard()">discard</button>
            <button class="sv-b go" onclick="SceneView.apply()">apply to the file…</button>
          </div>
        </div>
      </div>`;
    cv = host.querySelector("#sv-canvas");
    ctx = cv.getContext("2d");
    bind();
    await reload();
    fit();
  }

  function unmount(){
    if (hasPending() && !confirm(
        `${pendingCount()} change(s) have not been written. Leave and lose them?`))
      return false;
    pending.clear();
    host = null; cv = null; sel = null; list = null;
    return true;
  }

  async function reload(){
    if (!scene) return;
    pending.clear();
    paintPending();
    const d = await readJSON(`/api/scene/render?scene=${encodeURIComponent(scene)}`, null);
    if (!d || d.__error){ say((d && d.__error) || "could not render that scene"); return; }
    list = d;
    const rels = new Set();
    d.items.forEach(i => {
      const dr = i.draw || {};
      if (dr.rel) rels.add(dr.rel);
      if (dr.kind === "tiles")
        Object.values(dr.sources).forEach(s => rels.add(s.rel));
    });
    await Promise.all([...rels].map(load));
    // A layer hidden in one scene means nothing in the next one.
    hiddenLayers.clear(); isolated = null;
    paintLayers();
    paint();
  }

  function load(rel){
    if (images.has(rel)) return Promise.resolve(images.get(rel));
    return new Promise(res => {
      const im = new Image();
      im.onload = () => { images.set(rel, im); res(im); };
      im.onerror = () => { images.set(rel, null); res(null); };
      im.src = `/api/preview?rel=${encodeURIComponent(rel)}`;
    });
  }

  /* ── geometry ─────────────────────────────────────────────────────────── */
  /* An item's box in ITS OWN space: where the picture sits relative to the
     node's origin. Centering is the whole reason this is not just (0,0,w,h). */
  /* Cell -> the centre of that cell, in the layer's own space. Mirrors
     bgate_core.tilemap.cell_center; an isometric map drawn with the square
     formula is a neat grid that is confidently wrong. */
  function cellCenter(x, y, d){
    const w = d.tile_size[0], h = d.tile_size[1];
    if (d.shape === 1){
      return d.layout === 5
        ? [(x - y) * w / 2, (x + y) * h / 2]
        : [(x + y) * w / 2, (y - x) * h / 2];
    }
    return [x * w + w / 2, y * h + h / 2];
  }

  function localBox(it){
    const d = it.draw || {};
    if (d.kind === "tiles" && d.bounds){
      const b = d.bounds;
      return { x: b[0], y: b[1], w: b[2] - b[0], h: b[3] - b[1] };
    }
    const size = d.size || [0, 0];
    let w = size[0] || 0, h = size[1] || 0;
    if (d.kind === "camera"){
      const v = list.viewport;
      return { x:-v[0]/2, y:-v[1]/2, w:v[0], h:v[1] };
    }
    if (!w && !h){ return { x:-8, y:-8, w:16, h:16 }; }
    const off = d.offset || [0, 0];
    const cx = d.centered ? -w / 2 : 0;
    const cy = d.centered ? -h / 2 : 0;
    return { x: cx + off[0], y: cy + off[1], w, h };
  }

  function toScreen(x, y){
    return [x * view.z + view.x, y * view.z + view.y];
  }
  function toWorld(sx, sy){
    return [(sx - view.x) / view.z, (sy - view.y) / view.z];
  }
  /* World point -> the item's local space. Needed for hit-testing anything
     rotated, and for turning a drag into a scale along the item's own axes. */
  function toLocal(it, wx, wy){
    const dx = wx - it.x, dy = wy - it.y;
    const cos = Math.cos(-it.rot), sin = Math.sin(-it.rot);
    return [(dx * cos - dy * sin) / (it.sx || 1),
            (dx * sin + dy * cos) / (it.sy || 1)];
  }
  function corners(it){
    const b = localBox(it);
    const cos = Math.cos(it.rot), sin = Math.sin(it.rot);
    return [[b.x, b.y], [b.x + b.w, b.y], [b.x + b.w, b.y + b.h], [b.x, b.y + b.h]]
      .map(([lx, ly]) => {
        const px = lx * it.sx, py = ly * it.sy;
        return [it.x + px * cos - py * sin, it.y + px * sin + py * cos];
      });
  }

  /* ── layers ───────────────────────────────────────────────────────────────
   * A "layer" is a top-level child of the root — Ground, Props, Walls,
   * Characters. That is what a scene is actually organised into, and on a
   * stacked isometric map it is the only way to see the floor under the walls
   * or click a prop that a wall is drawn over.
   *
   * These toggles are a VIEW state, deliberately. Hiding a layer here does not
   * touch its `visible` property — that would be an edit to the game, and the
   * whole point of this panel now is that looking at something never changes
   * it. `◌` on the toolbar is the control for the real property.
   */
  function layerOf(it){
    if (it.path === ".") return ".";
    return it.path.split("/")[0];
  }

  function layers(){
    if (!list) return [];
    const seen = new Map();
    list.items.forEach(it => {
      // A top-level child is one whose path has no separator. The draw list
      // carries paths, not parent links — deriving it here keeps one source of
      // truth instead of a second field to keep in sync.
      if (it.path === "." || it.path.includes("/")) return;
      seen.set(it.path, { path: it.path, name: it.name, role: it.role,
                          type: it.type, count: 0 });
    });
    list.items.forEach(it => {
      const host = seen.get(layerOf(it));
      if (host && it.draw && ["image", "rect", "tiles"].includes(it.draw.kind))
        host.count += it.draw.kind === "tiles" ? it.draw.cells.length : 1;
    });
    return [...seen.values()];
  }

  function layerVisible(path){
    if (isolated) return path === isolated;
    return !hiddenLayers.has(path);
  }

  function drawable(it){
    if (!it.visible && !opts.showHidden) return false;
    if (it.path !== "." && !layerVisible(layerOf(it))) return false;
    const k = it.draw && it.draw.kind;
    if ((k === "body" || k === "marker") && !opts.showBodies) return false;
    return true;
  }

  function toggleLayer(path){
    if (isolated){ isolated = null; hiddenLayers.clear(); }
    hiddenLayers.has(path) ? hiddenLayers.delete(path) : hiddenLayers.add(path);
    paintLayers(); paint();
  }
  function isolateLayer(path){
    isolated = isolated === path ? null : path;
    hiddenLayers.clear();
    paintLayers(); paint();
  }
  function showAllLayers(){
    isolated = null; hiddenLayers.clear();
    paintLayers(); paint();
  }

  function paintLayers(){
    const bar = document.getElementById("sv-layers");
    if (!bar) return;
    const ls = layers();
    bar.hidden = ls.length < 2;
    if (bar.hidden) return;
    const dirty = isolated || hiddenLayers.size;
    bar.innerHTML = `<span class="sv-l">layers</span>`
      + ls.map(l => {
          const on = layerVisible(l.path);
          const tint = ROLE_TINT[l.role] || ROLE_TINT.node;
          return `<span class="sv-layer${on ? "" : " off"}${
            isolated === l.path ? " solo" : ""}" style="--lt:${tint}">
            <button class="eye" title="${on ? "Hide" : "Show"} ${E(l.name)} in this view only"
                    onclick="SceneView.toggleLayer('${E(l.path)}')">${on ? "◉" : "○"}</button>
            <button class="nm" title="Show only ${E(l.name)}"
                    onclick="SceneView.isolateLayer('${E(l.path)}')">${E(l.name)}</button>
            <span class="ct">${l.count || ""}</span></span>`;
        }).join("")
      + (dirty ? `<button class="sv-b" onclick="SceneView.showAllLayers()">show all</button>` : "");
  }

  function hit(wx, wy){
    // Topmost first: the last painted item is the one you see, so it is the
    // one a click must claim.
    const items = list.items.filter(drawable);
    for (let i = items.length - 1; i >= 0; i--){
      const it = items[i];
      const b = localBox(it);
      const [lx, ly] = toLocal(it, wx, wy);
      if (lx >= b.x && ly >= b.y && lx <= b.x + b.w && ly <= b.y + b.h) return it;
    }
    return null;
  }

  function handleAt(sx, sy){
    if (!sel) return null;
    const pts = corners(sel);
    const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    const named = {
      nw: pts[0], ne: pts[1], se: pts[2], sw: pts[3],
      n: mid(pts[0], pts[1]), e: mid(pts[1], pts[2]),
      s: mid(pts[2], pts[3]), w: mid(pts[3], pts[0]),
    };
    for (const [key, pt] of Object.entries(named)){
      const [hx, hy] = toScreen(pt[0], pt[1]);
      if (Math.abs(sx - hx) <= HANDLE_PX + 2 && Math.abs(sy - hy) <= HANDLE_PX + 2)
        return key;
    }
    const top = mid(pts[0], pts[1]);
    const cos = Math.cos(sel.rot), sin = Math.sin(sel.rot);
    const [rx, ry] = toScreen(top[0] - sin * -26 / view.z, top[1] + cos * -26 / view.z);
    if (Math.hypot(sx - rx, sy - ry) <= HANDLE_PX + 3) return "rot";
    return null;
  }

  /* ── painting ─────────────────────────────────────────────────────────── */
  function size(){
    const r = host.querySelector("#sv-stage").getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (cv.width !== w || cv.height !== h){ cv.width = w; cv.height = h; }
    return dpr;
  }

  /* Coalesce repaints to one per frame — but never LATCH on the guard.
   *
   * requestAnimationFrame does not fire while the page is not compositing (a
   * hidden pane, a background tab). The obvious `if (pending) return` guard
   * then stays true forever and the canvas is frozen for the rest of the
   * session, which reads as a dead panel rather than a throttled one. The
   * timeout is the escape hatch: whichever fires first does the work and
   * clears the flag. */
  function paint(){
    if (!host || !ctx || !list) return;
    if (paint._pending) return;
    paint._pending = true;
    const run = () => {
      if (!paint._pending) return;
      paint._pending = false;
      _paint();
    };
    requestAnimationFrame(run);
    setTimeout(run, 120);
  }

  function _paint(){
    if (!host || !list) return;
    const dpr = size();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = cv.width / dpr, H = cv.height / dpr;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#0a0b0e"; ctx.fillRect(0, 0, W, H);

    const v = list.viewport;
    if (opts.grid) grid(W, H);

    // The game's own frame, so "off screen" is visible as off screen.
    const [vx, vy] = toScreen(0, 0);
    ctx.save();
    ctx.fillStyle = "rgba(12,14,18,.75)";
    ctx.fillRect(vx, vy, v[0] * view.z, v[1] * view.z);
    ctx.strokeStyle = "rgba(232,226,216,.3)"; ctx.lineWidth = 1;
    ctx.strokeRect(vx + .5, vy + .5, v[0] * view.z, v[1] * view.z);
    ctx.restore();

    ctx.imageSmoothingEnabled = false;
    const shown = list.items.filter(drawable);
    shown.forEach(it => item(it));
    if (sel) gizmo(sel);
    const DRAWS = ["image", "rect", "tiles"];
    if (!shown.some(i => i.draw && DRAWS.includes(i.draw.kind)))
      emptyState(vx, vy, v);

    const h = hover ? `  ·  ${hover.path}` : "";
    host.querySelector("#sv-hud").textContent =
      `${v[0]}×${v[1]}  ·  ${Math.round(view.z * 100)}%  ·  ${
        list.items.filter(drawable).length} drawn${h}`;
    const blank = list.items.filter(i => i.draw && i.draw.kind === "marker"
                                      && i.draw.reason).length;
    const tip = host.querySelector("#sv-tip");
    if (tip) tip.textContent = blank
      ? `${blank} node(s) draw nothing in the FILE — a script assigns them at run time`
      : "drag to move · handles scale · ring rotates · shift = free";
    const u = document.getElementById("sv-undo");
    if (u) u.disabled = !pendingCount();
  }

  /* An empty frame is indistinguishable from a broken viewport, and this tool
   * produces empty frames legitimately: a scene can be a script host with one
   * node, or a scene whose art is all assigned at run time. Say which. */
  function emptyState(vx, vy, v){
    const n = list.items.length;
    const runtime = list.items.filter(i => i.draw && i.draw.kind === "marker"
                                        && i.draw.reason).length;
    const lines = runtime
      ? [`nothing to draw yet`,
         `${runtime} of ${n} node(s) get their art from a script at run time,`,
         `so the scene FILE has no picture in it. That is accurate, not broken.`]
      : n <= 1
      ? [`this scene is a script host`,
         `one node, no visuals — its screen is built at run time.`,
         `add a node from the panel on the right to start placing art.`]
      : [`nothing in this scene draws`,
         `${n} nodes, none of them visual.`,
         `add a Sprite2D or a ColorRect from the panel on the right.`];
    ctx.save();
    ctx.textAlign = "center";
    const cx = vx + v[0] * view.z / 2, cy = vy + v[1] * view.z / 2;
    ctx.fillStyle = "#e8e2d8";
    ctx.font = "13px ui-monospace,monospace";
    ctx.fillText(lines[0], cx, cy - 8);
    ctx.fillStyle = "#7c8695";
    ctx.font = "11px ui-monospace,monospace";
    lines.slice(1).forEach((line, i) => ctx.fillText(line, cx, cy + 12 + i * 15));
    ctx.restore();
  }

  function grid(W, H){
    const step = Math.max(4, opts.snap) * view.z;
    if (step < 6) return;
    ctx.save();
    ctx.strokeStyle = "rgba(232,226,216,.05)"; ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = view.x % step; x < W; x += step){ ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, H); }
    for (let y = view.y % step; y < H; y += step){ ctx.moveTo(0, y + .5); ctx.lineTo(W, y + .5); }
    ctx.stroke();
    ctx.restore();
  }

  function item(it){
    const d = it.draw || {};
    const b = localBox(it);
    ctx.save();
    const [sx, sy] = toScreen(it.x, it.y);
    ctx.translate(sx, sy);
    ctx.rotate(it.rot);
    ctx.scale(view.z * it.sx, view.z * it.sy);
    if (!it.visible) ctx.globalAlpha = 0.32;
    const mod = it.modulate || [1, 1, 1, 1];
    if (mod[3] < 1) ctx.globalAlpha *= mod[3];

    if (d.kind === "tiles"){
      tiles(d);
    } else if (d.kind === "image"){
      const im = images.get(d.rel);
      if (im){
        const r = d.region || [0, 0, im.width, im.height];
        ctx.drawImage(im, r[0], r[1], r[2], r[3], b.x, b.y, b.w, b.h);
      } else {
        ctx.fillStyle = "rgba(120,60,40,.5)";
        ctx.fillRect(b.x, b.y, b.w, b.h);
      }
    } else if (d.kind === "rect"){
      const c = d.color || [.4, .4, .45, .9];
      ctx.fillStyle = `rgba(${(c[0]*255)|0},${(c[1]*255)|0},${(c[2]*255)|0},${c[3]})`;
      ctx.fillRect(b.x, b.y, b.w, b.h);
    } else if (d.kind === "camera"){
      ctx.strokeStyle = ROLE_TINT.camera; ctx.lineWidth = 2 / (view.z * it.sx);
      ctx.setLineDash([8 / (view.z * it.sx), 6 / (view.z * it.sx)]);
      ctx.strokeRect(b.x, b.y, b.w, b.h);
      ctx.setLineDash([]);
    } else if (opts.showBodies){
      const tint = ROLE_TINT[it.role] || ROLE_TINT.node;
      ctx.strokeStyle = tint; ctx.lineWidth = 1 / (view.z * it.sx);
      ctx.globalAlpha *= .8;
      ctx.beginPath();
      ctx.moveTo(-7, 0); ctx.lineTo(7, 0); ctx.moveTo(0, -7); ctx.lineTo(0, 7);
      ctx.stroke();
    }
    ctx.restore();

    // A node that draws nothing says WHY. Most of these are sprites whose
    // SpriteFrames is assigned by a script at load — this view shows what the
    // scene FILE declares, and silence there reads as a broken viewport rather
    // than as the accurate answer it is.
    if (d.kind === "marker" && d.reason && opts.showBodies && view.z > 0.45){
      const [mx, my] = toScreen(it.x, it.y);
      ctx.save();
      ctx.font = "10px ui-monospace,monospace";
      ctx.fillStyle = (ROLE_TINT[it.role] || ROLE_TINT.node) + "cc";
      ctx.fillText(`${it.name} — ${d.reason}`, mx + 10, my + 3);
      ctx.restore();
    }

    if (opts.outlines && d.kind !== "marker"){
      const pts = corners(it).map(p => toScreen(p[0], p[1]));
      ctx.save();
      ctx.strokeStyle = (ROLE_TINT[it.role] || ROLE_TINT.node) + "55";
      ctx.lineWidth = 1;
      ctx.beginPath();
      pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
      ctx.closePath(); ctx.stroke();
      ctx.restore();
    }
  }

  /* Every placed cell, painted in the layer's local space (the caller has
   * already applied the node transform). Godot anchors an atlas tile by its
   * CENTRE on the cell centre, plus the source's texture_origin — which is how
   * a 64x96 wall tile stands up out of a 64x32 cell instead of being squashed
   * into it. */
  function tiles(d){
    const cells = d.cells;
    for (let i = 0; i < cells.length; i++){
      const c = cells[i];
      const src = d.sources[String(c[2])];
      if (!src) continue;
      const im = images.get(src.rel);
      if (!im) continue;
      const rw = src.region[0], rh = src.region[1];
      const sx = c[3] * rw, sy = c[4] * rh;
      if (sx + rw > im.width + 0.5 || sy + rh > im.height + 0.5) continue;
      const [cx, cy] = cellCenter(c[0], c[1], d);
      ctx.drawImage(im, sx, sy, rw, rh,
                    cx - rw / 2 + src.origin[0],
                    cy - rh / 2 + src.origin[1], rw, rh);
    }
  }

  function gizmo(it){
    const pts = corners(it).map(p => toScreen(p[0], p[1]));
    ctx.save();
    ctx.strokeStyle = "var(--ember)"; ctx.strokeStyle = "#ff6a3d"; ctx.lineWidth = 1.5;
    ctx.beginPath();
    pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    ctx.closePath(); ctx.stroke();

    const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    const all = [...pts, mid(pts[0], pts[1]), mid(pts[1], pts[2]),
                 mid(pts[2], pts[3]), mid(pts[3], pts[0])];
    ctx.fillStyle = "#ff6a3d";
    all.forEach(([x, y]) =>
      ctx.fillRect(x - HANDLE_PX / 2, y - HANDLE_PX / 2, HANDLE_PX, HANDLE_PX));

    const top = mid(pts[0], pts[1]);
    ctx.beginPath();
    ctx.moveTo(top[0], top[1]);
    const dir = [Math.sin(it.rot), -Math.cos(it.rot)];
    const rp = [top[0] + dir[0] * 26, top[1] + dir[1] * 26];
    ctx.lineTo(rp[0], rp[1]); ctx.stroke();
    ctx.beginPath(); ctx.arc(rp[0], rp[1], HANDLE_PX / 2 + 1, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "#ff6a3d"; ctx.font = "11px ui-monospace,monospace";
    ctx.fillText(it.name, pts[0][0], pts[0][1] - 8);
    ctx.restore();
  }

  /* ── interaction ──────────────────────────────────────────────────────── */
  function bind(){
    cv.addEventListener("pointerdown", ev => {
      if (!list) return;
      cv.setPointerCapture(ev.pointerId);
      const r = cv.getBoundingClientRect();
      const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
      if (ev.button === 1 || ev.button === 2 || ev.altKey){
        drag = { pan:true, sx, sy, ox:view.x, oy:view.y };
        ev.preventDefault(); return;
      }
      const grip = handleAt(sx, sy);
      if (grip && sel){
        const [wx, wy] = toWorld(sx, sy);
        drag = { grip, it: sel, start: toLocal(sel, wx, wy),
                 sx0: sel.sx, sy0: sel.sy, rot0: sel.rot,
                 cx: sel.x, cy: sel.y };
        return;
      }
      const [wx, wy] = toWorld(sx, sy);
      const found = hit(wx, wy);
      select(found ? found.path : null);
      if (found) drag = { move:true, it: found, gx: wx - found.x, gy: wy - found.y,
                          x0: found.x, y0: found.y };
      else drag = { pan:true, sx, sy, ox:view.x, oy:view.y };
      paint();
    });

    cv.addEventListener("pointermove", ev => {
      if (!list) return;
      const r = cv.getBoundingClientRect();
      const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
      const [wx, wy] = toWorld(sx, sy);
      if (!drag){
        hover = hit(wx, wy);
        cv.style.cursor = handleAt(sx, sy) ? "nwse-resize" : hover ? "move" : "default";
        paint();
        return;
      }
      if (drag.pan){
        view.x = drag.ox + (sx - drag.sx);
        view.y = drag.oy + (sy - drag.sy);
        paint(); return;
      }
      if (drag.move){
        let nx = wx - drag.gx, ny = wy - drag.gy;
        if (opts.snapOn && !ev.shiftKey){
          nx = Math.round(nx / opts.snap) * opts.snap;
          ny = Math.round(ny / opts.snap) * opts.snap;
        }
        drag.it.x = nx; drag.it.y = ny;
        paint(); return;
      }
      if (drag.grip === "rot"){
        let ang = Math.atan2(wy - drag.cy, wx - drag.cx) + Math.PI / 2;
        if (!ev.shiftKey) ang = Math.round(ang / (Math.PI / 12)) * (Math.PI / 12);
        drag.it.rot = ang;
        paint(); return;
      }
      if (drag.grip){
        // Scale along the item's own axes: the drag is measured in LOCAL space,
        // so a rotated node scales the way it looks, not the way the screen is.
        const [lx, ly] = toLocal(drag.it, wx, wy);
        const b = localBox(drag.it);
        const g = drag.grip;
        let fx = drag.sx0, fy = drag.sy0;
        if (g.includes("e") && b.w) fx = drag.sx0 * (lx / (drag.start[0] || 1e-6));
        if (g.includes("w") && b.w) fx = drag.sx0 * (lx / (drag.start[0] || 1e-6));
        if (g.includes("s") && b.h) fy = drag.sy0 * (ly / (drag.start[1] || 1e-6));
        if (g.includes("n") && b.h) fy = drag.sy0 * (ly / (drag.start[1] || 1e-6));
        if (ev.shiftKey || g.length === 2){          // corners keep the aspect
          const k = (Math.abs(fx / (drag.sx0 || 1)) + Math.abs(fy / (drag.sy0 || 1))) / 2;
          fx = drag.sx0 * k; fy = drag.sy0 * k;
        }
        drag.it.sx = clamp(Math.abs(fx) < 0.01 ? 0.01 : fx, -50, 50);
        drag.it.sy = clamp(Math.abs(fy) < 0.01 ? 0.01 : fy, -50, 50);
        paint(); return;
      }
    });

    const done = () => {
      if (!drag){ return; }
      const d = drag; drag = null;
      if (d.move) stageMove(d);
      else if (d.grip === "rot"){
        if (round(d.it.rot, 4) !== round(d.rot0, 4))
          stage(d.it, "rotation", round(d.it.rot, 4), round(d.rot0, 4));
      } else if (d.grip){
        if (round(d.it.sx, 3) !== round(d.sx0, 3)
            || round(d.it.sy, 3) !== round(d.sy0, 3))
          stage(d.it, "scale",
                `Vector2(${round(d.it.sx, 3)}, ${round(d.it.sy, 3)})`,
                `Vector2(${round(d.sx0, 3)}, ${round(d.sy0, 3)})`);
      }
      paint();
    };
    cv.addEventListener("pointerup", done);
    cv.addEventListener("pointercancel", done);
    cv.addEventListener("contextmenu", ev => ev.preventDefault());

    cv.addEventListener("wheel", ev => {
      ev.preventDefault();
      const r = cv.getBoundingClientRect();
      const mx = ev.clientX - r.left, my = ev.clientY - r.top;
      const [bx, by] = toWorld(mx, my);
      view.z = clamp(view.z * (ev.deltaY < 0 ? 1.15 : 1 / 1.15), 0.05, 12);
      view.x = mx - bx * view.z; view.y = my - by * view.z;
      paint();
    }, { passive: false });

    window.addEventListener("resize", paint);
  }

  const round = (v, n) => Math.round(v * 10 ** n) / 10 ** n;

  /* ── staging ──────────────────────────────────────────────────────────────
   * NOTHING HERE WRITES UNTIL YOU SAY SO.
   *
   * This used to commit on pointer-release: one drag, one file write, one
   * backup. The reasoning was that a confirmation dialog per drag is a tool
   * nobody uses — which is true, and completely beside the point. Moving a
   * thing to look at it is not a decision to change the game, and twenty-two
   * accidental writes to a live scene is not a thing a backup makes fine.
   *
   * So a drag now changes the PICTURE only. Edits accumulate as pending
   * property writes, the bar says how many there are, and `apply` is the only
   * thing that touches disk — one confirmed action, one backup, not one per
   * pixel. `discard` re-reads the file and everything you did evaporates,
   * which is exactly what should have happened by default.
   */
  function stage(it, key, value, prevValue){
    const bucket = pending.get(it.path) || { name: it.name, keys: new Map() };
    const existing = bucket.keys.get(key);
    // Keep the ORIGINAL value across repeated edits to the same property —
    // three nudges of one node is one change from the file's point of view,
    // and discarding must go back to where the file was, not to nudge two.
    bucket.keys.set(key, { value, prev: existing ? existing.prev : prevValue });
    pending.set(it.path, bucket);
    paintPending();
    paint();
  }

  function pendingCount(){
    let n = 0;
    pending.forEach(b => { n += b.keys.size; });
    return n;
  }

  function paintPending(){
    const bar = document.getElementById("sv-pending");
    if (!bar) return;
    const n = pendingCount();
    bar.hidden = !n;
    const label = document.getElementById("sv-pending-n");
    if (label) label.textContent =
      `${n} unsaved change${n === 1 ? "" : "s"} across ${pending.size} node${
        pending.size === 1 ? "" : "s"}`;
  }

  function stageMove(d){
    const it = d.it;
    if (round(it.x, 3) === round(d.x0, 3) && round(it.y, 3) === round(d.y0, 3)) return;
    // A Control positions by anchor offsets, not by `position` — writing
    // `position` on one moves nothing and looks like the save silently failed.
    const isControl = /Rect$|^Control$|^Panel$|^Label$|^Button$|Container$/.test(it.type);
    if (isControl){
      const b = localBox(it);
      const was = { x: d.x0, y: d.y0 };
      stage(it, "offset_left", round(it.x, 2), round(was.x, 2));
      stage(it, "offset_top", round(it.y, 2), round(was.y, 2));
      stage(it, "offset_right", round(it.x + b.w, 2), round(was.x + b.w, 2));
      stage(it, "offset_bottom", round(it.y + b.h, 2), round(was.y + b.h, 2));
      return;
    }
    const [lx, ly] = toLocalTransform(it, it.x, it.y);
    const [px, py] = toLocalTransform(it, d.x0, d.y0);
    stage(it, "position", `Vector2(${round(lx, 2)}, ${round(ly, 2)})`,
          `Vector2(${round(px, 2)}, ${round(py, 2)})`);
  }

  /* One confirmed action, one pass, one backup per file — not one per pixel. */
  async function applyPending(){
    if (busy || !pendingCount()) return;
    const n = pendingCount();
    if (!confirm(`Write ${n} change${n === 1 ? "" : "s"} to ${
      String(scene).split("/").pop()}?\n\nThe current file is kept under `
      + `.bgate_out/scene_backups.`)) return;
    busy = true;
    const failed = [];
    for (const [path, bucket] of pending){
      for (const [key, change] of bucket.keys){
        const r = await mutate("/api/scene/node/property", {
          body: { scene, node: path, key, value: change.value }, quiet: true });
        if (!r.ok) failed.push(`${path}.${key}: ${r.error}`);
      }
    }
    busy = false;
    pending.clear();
    paintPending();
    await reload();
    if (failed.length) say(`${failed.length} change(s) failed — ${failed[0]}`);
    else say(`${n} change${n === 1 ? "" : "s"} written`, "ok");
  }

  /* Re-read the file. Whatever was staged never existed. */
  async function discardPending(){
    if (!pendingCount()) return;
    pending.clear();
    paintPending();
    await reload();
    say("staged changes discarded", "ok");
  }

  function hasPending(){ return pendingCount() > 0; }

  /* Node transforms are LOCAL in the file and WORLD in the draw list, so a
     child of a moved parent must have its parent's transform removed again
     before the number is written. Skipping this writes the world position into
     a local field and the node jumps the moment the scene reloads. */
  function toLocalTransform(it, wx, wy){
    const parent = it.path.includes("/")
      ? list.items.find(p => p.path === it.path.slice(0, it.path.lastIndexOf("/")))
      : null;
    if (!parent) return [wx, wy];
    const dx = wx - parent.x, dy = wy - parent.y;
    const cos = Math.cos(-parent.rot), sin = Math.sin(-parent.rot);
    return [(dx * cos - dy * sin) / (parent.sx || 1),
            (dx * sin + dy * cos) / (parent.sy || 1)];
  }

  /* Undo pops the last STAGED change back off. Nothing has been written, so
     there is nothing to un-write — it just edits the pending list. */
  function undoLast(){
    let lastPath = null, lastKey = null;
    pending.forEach((bucket, path) => {
      bucket.keys.forEach((_, key) => { lastPath = path; lastKey = key; });
    });
    if (!lastPath) return;
    const bucket = pending.get(lastPath);
    const change = bucket.keys.get(lastKey);
    bucket.keys.delete(lastKey);
    if (!bucket.keys.size) pending.delete(lastPath);
    // Put the number back on the item so the picture matches the pending list.
    const it = list.items.find(i => i.path === lastPath);
    if (it && change) restoreValue(it, lastKey, change.prev);
    paintPending();
    paint();
  }

  function restoreValue(it, key, prev){
    const vec = /Vector2\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)/.exec(String(prev));
    if (key === "position" && vec){
      const [wx, wy] = toWorldTransform(it, +vec[1], +vec[2]);
      it.x = wx; it.y = wy;
    } else if (key === "scale" && vec){
      it.sx = +vec[1]; it.sy = +vec[2];
    } else if (key === "rotation"){
      it.rot = +prev || 0;
    } else if (key === "z_index"){
      it.z = +prev || 0;
    }
  }

  /* The inverse of toLocalTransform — a staged local value has to come back
     out to world space to be drawn. */
  function toWorldTransform(it, lx, ly){
    const parent = it.path.includes("/")
      ? list.items.find(p => p.path === it.path.slice(0, it.path.lastIndexOf("/")))
      : null;
    if (!parent) return [lx, ly];
    const cos = Math.cos(parent.rot), sin = Math.sin(parent.rot);
    const sx = lx * (parent.sx || 1), sy = ly * (parent.sy || 1);
    return [parent.x + sx * cos - sy * sin, parent.y + sx * sin + sy * cos];
  }

  function raise(delta){
    if (!sel) return;
    const was = sel.z || 0;
    sel.z = was + delta;
    stage(sel, "z_index", sel.z, was);
    // z decides paint order, so the picture is only right once it re-sorts.
    list.items.sort((a, b) => (a.z - b.z) || (a.order - b.order));
    paint();
  }

  /* ── selection ────────────────────────────────────────────────────────── */
  function select(path){
    sel = path && list ? list.items.find(i => i.path === path) || null : null;
    paint();
    if (window.SceneBuild && typeof SceneBuild.select === "function")
      SceneBuild.select(path);           // one selection, both surfaces
  }

  /* Frame the CONTENT union the game frame, not the frame alone. An isometric
     level spills well outside the 640x360 rectangle — fitting the rectangle
     put most of the floor off screen and looked like half the tiles were
     missing. */
  function fit(){
    if (!list || !host) return;
    const r = host.querySelector("#sv-stage").getBoundingClientRect();
    const v = list.viewport;
    let x0 = 0, y0 = 0, x1 = v[0], y1 = v[1];
    list.items.filter(drawable).forEach(it => {
      const b = localBox(it);
      corners(it).forEach(([px, py]) => {
        x0 = Math.min(x0, px); y0 = Math.min(y0, py);
        x1 = Math.max(x1, px); y1 = Math.max(y1, py);
      });
      void b;
    });
    const w = Math.max(1, x1 - x0), h = Math.max(1, y1 - y0);
    view.z = clamp(Math.min((r.width - 60) / w, (r.height - 60) / h), 0.05, 4);
    view.x = (r.width - w * view.z) / 2 - x0 * view.z;
    view.y = (r.height - h * view.z) / 2 - y0 * view.z;
    paint();
  }
  function zoomBy(f){
    if (!host) return;
    const r = host.querySelector("#sv-stage").getBoundingClientRect();
    const [bx, by] = toWorld(r.width / 2, r.height / 2);
    view.z = clamp(view.z * f, 0.05, 12);
    view.x = r.width / 2 - bx * view.z;
    view.y = r.height / 2 - by * view.z;
    paint();
  }
  function toggle(key){
    opts[key] = !opts[key];
    // Repaint the toolbar in place. Re-mounting redraws it too, but it also
    // re-reads the file — which would silently throw away staged changes for
    // the sake of one button's highlight.
    const btn = host && host.querySelector(`[onclick*="toggle('${key}')"]`);
    if (btn) btn.classList.toggle("on", !!opts[key]);
    if (key === "snapOn" && btn) btn.textContent = opts.snapOn ? "on" : "off";
    paint();
  }
  function setSnap(v){ opts.snap = clamp(parseInt(v, 10) || 8, 1, 256); paint(); }

  /* A canvas is not a screenshot — nothing outside the browser can see it, so
     "here is what my scene looks like" was un-shareable. This writes the exact
     pixels on screen to a real file. */
  async function snapshot(){
    if (!cv) return;
    const png = cv.toDataURL("image/png");
    const r = await mutate("/api/scene/snapshot", { body: { scene, png } });
    if (r.ok) say(`saved ${r.data.rel}`, "ok");
  }
  function setScene(id){
    if (hasPending() && !confirm(
        `${pendingCount()} change(s) have not been written. Switch scene and lose them?`))
      return Promise.resolve(false);
    pending.clear();
    scene = id; sel = null;
    return reload();
  }

  return { mount, unmount, reload, fit, zoom: zoomBy, toggle, setSnap, snapshot,
           select, raise, undo: undoLast, setScene,
           apply: applyPending, discard: discardPending, hasPending,
           toggleLayer, isolateLayer, showAllLayers,
           get layers(){ return layers(); },
           get list(){ return list; }, get selected(){ return sel; },
           get scene(){ return scene; },
           get pending(){ return pendingCount(); } };
})();
