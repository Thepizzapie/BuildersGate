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

  // Token NAMES, not var() strings: most of these end up on the canvas, which
  // cannot resolve var() and drops the assignment without saying so.
  const ROLE_TINT = {
    character:"--warn", enemy:"--bad", prop:"--warn-line", item:"--warn",
    layer:"--accent", visual:"--text", collision:"--text-3",
    controller:"--c-narrative", camera:"--text", audio:"--c-narrative", fx:"--c-narrative",
    ui:"--good", marker:"--text-3", instance:"--text", node:"--text-3",
  };
  const tintVar = role => ROLE_TINT[role] || ROLE_TINT.node;
  const tintOf = role => BGTheme.color(tintVar(role));
  const HANDLES = [
    ["nw",0,0],["n",.5,0],["ne",1,0],["e",1,.5],
    ["se",1,1],["s",.5,1],["sw",0,1],["w",0,.5],
  ];
  const HANDLE_PX = 7;          // screen-space, so handles stay grabbable at any zoom
  // Above this many undrawable nodes, their captions stop being labels and
  // start being a fog. Twelve is roughly what fits on a 640×360 stage without
  // two of them touching.
  const BLANK_LABEL_CAP = 12;

  let host = null, cv = null, ctx = null;
  let scene = null, list = null, sel = null;
  let blankCount = 0;           // recomputed once per frame in _paint()
  let view = { x: 0, y: 0, z: 1 };
  let opts = { grid: true, snap: 8, snapOn: true, showHidden: false,
               showBodies: true, outlines: true, real: false };
  let images = new Map();       // rel -> HTMLImageElement | null

  /* THE ENGINE'S OWN FRAME, under the editable overlay.
   *
   * Everything else in this file draws what the scene FILE declares, and on a
   * project whose props get their texture from a script at load that is an
   * accurate picture of nothing: 577 markers where a dressed floor should be.
   * No amount of better .tscn parsing crosses that — only running the game
   * does. So this asks Godot for one real frame and puts it behind the
   * overlay, which keeps the handles, outlines and staged edits exactly where
   * they were while the backdrop finally shows the level.
   *
   * NOT the default, and never automatic: it launches the actual game for a
   * couple of seconds and needs a display. A viewport that opened a window
   * every time you selected a scene would be unusable. */
  let real = { scene: null, img: null, busy: false, error: null, at: 0 };
  let drag = null, hover = null, busy = false;
  // path -> { name, keys: Map(property -> {value, prev, seq}) }. Staged, never written.
  let pending = new Map();
  /* STRUCTURE stages too, in the same bar. Adding and deleting a node used to
     be the graph panel's job and it wrote on confirm — so half the builder
     committed on click and the other half waited for `apply`, which is exactly
     the split that made accidental writes normal. These are ordered ops:
       { op:"add", src, render, name, parent, wx, wy, seq }
       { op:"delete", path, name, seq }
     `seq` is monotonic across ops AND property edits so undo pops whichever
     really was last. */
  let staged = [], seq = 0;
  let placing = null;      // an armed placement, waiting for a click on the canvas
  let selStaged = -1;      // index into `staged` of a selected, not-yet-written add
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
      // A blank count reads as broken. These say WHICH kind of nothing it is:
      // dimmed for empty in the file, warn-coloured for filled at run time.
      ".sv-layer .ct.none{color:var(--ash2);opacity:.75;font-style:italic}",
      ".sv-layer .ct.rt{color:var(--warn)}",
      ".sv-layer.rt{border-style:dashed}",
      ".sv-layer:hover{border-color:var(--lt)}",
      ".sv-layer:hover .nm{color:var(--bone)}",
      ".sv-layer.off{opacity:.42}",
      ".sv-layer.off .nm{text-decoration:line-through}",
      ".sv-layer.solo{border-color:var(--lt);background:var(--plate)}",
      ".sv-layer.solo .nm{color:var(--bone)}",
      ".sv-stage{flex:1;position:relative;min-height:0;background:var(--bg);overflow:hidden}",
      ".sv-stage canvas{position:absolute;inset:0;width:100%;height:100%;touch-action:none}",
      ".sv-hud{position:absolute;left:9px;bottom:9px;font-family:var(--mono);font-size:10px;color:var(--ash2);background:var(--iron);border:1px solid var(--seam);border-radius:6px;padding:3px 8px;pointer-events:none;white-space:pre}",
      ".sv-tip{position:absolute;right:9px;bottom:9px;font-family:var(--mono);font-size:9.5px;color:var(--ash2);background:var(--iron);border:1px solid var(--seam);border-radius:6px;padding:3px 8px;pointer-events:none}",
      // It is a hint until it is a finding, and a finding you can click.
      ".sv-tip.hot{pointer-events:auto;cursor:pointer;color:var(--warn);border-color:var(--warn)}",
      // One column so the pending bar and the placing banner stack instead of
      // sitting on top of each other — placing is exactly when both are up.
      // `hidden` MUST WIN. Every panel below sets `display`, and a class that
      // sets display beats the UA sheet's [hidden]{display:none} at equal
      // specificity — so `el.hidden = true` set the attribute and changed
      // nothing on screen. That is why the staging bar sat there announcing
      // "0 unsaved changes across 0 nodes" with a discard button, and why an
      // empty dashed placement strip hung underneath it forever.
      ".sv [hidden]{display:none !important}",
      ".sv-top{position:absolute;left:9px;right:9px;top:9px;display:flex;flex-direction:column;gap:6px;pointer-events:none}",
      ".sv-top>*{pointer-events:auto}",
      // Unmissable, because the alternative is writing to a live scene by
      // accident — which is exactly what this replaced.
      ".sv-pending{display:flex;align-items:center;gap:9px;background:var(--iron);border:1px solid var(--warn);border-radius:8px;padding:6px 10px;font-family:var(--mono);font-size:10.5px;color:var(--warn)}",
      ".sv-pending .dot{width:8px;height:8px;border-radius:50%;background:var(--warn);flex:none}",
      ".sv-pending .sv-b{color:var(--bone)}",
      ".sv-pending .sv-b.go{background:var(--ember);color:var(--bg);border-color:var(--ember);font-weight:var(--fw-semi)}",
      ".sv-place{display:flex;align-items:center;gap:9px;background:var(--iron);border:1px dashed var(--ember);border-radius:8px;padding:6px 10px;font-family:var(--mono);font-size:10.5px;color:var(--bone)}",
      ".sv-place b{color:var(--ember);font-weight:400}",
      // The scene picker. A list of the project's own .tscn files, which is the
      // one thing the builder could never reach — every .tscn is kind="screen"
      // to the atlas, and the swap picker filters those out on purpose.
      ".sv-pick{position:absolute;right:9px;top:9px;width:min(300px,calc(100% - 18px));max-height:calc(100% - 18px);overflow-y:auto;background:var(--iron);border:1px solid var(--seam);border-radius:8px;padding:7px;z-index:3}",
      ".sv-pick .hd{font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--ash2);padding:2px 4px 6px}",
      ".sv-pick button{display:flex;width:100%;align-items:baseline;gap:7px;background:none;border:0;border-radius:6px;color:var(--bone);font:inherit;font-size:11px;text-align:left;padding:5px 7px;cursor:pointer}",
      ".sv-pick button:hover{background:var(--plate)}",
      ".sv-pick button span{margin-left:auto;font-family:var(--mono);font-size:9px;color:var(--ash2)}",
      ".sv-pick .no{font-size:11px;color:var(--ash);padding:4px 7px;line-height:1.5}",
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
          <button class="sv-b ${opts.real?"on":""}" id="sv-real"
                  title="Run the game for a moment and use its real frame as the backdrop. Shows art that scripts assign at load — the props this view can only draw as markers. Opens a game window briefly."
                  onclick="SceneView.realView()">◉ real</button>
          <span style="flex:1 1 auto;min-width:8px"></span>
          <button class="sv-b" onclick="SceneView.placeMenu()"
                  title="Place one of this project's scenes as a child of the selected node">＋ place</button>
          <button class="sv-b" onclick="SceneView.removeSelected()"
                  title="Delete the selected node — staged, like every other edit here">⌫</button>
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
          <div class="sv-tip" id="sv-tip" onclick="SceneView.nextBlank()">drag to move · handles scale · ring rotates · shift = free</div>
          <div class="sv-top">
            <div class="sv-pending" id="sv-pending" hidden>
              <span class="dot"></span>
              <span id="sv-pending-n"></span>
              <span style="flex:1"></span>
              <button class="sv-b" onclick="SceneView.discard()">discard</button>
              <button class="sv-b go" onclick="SceneView.apply()">apply to the file…</button>
            </div>
            <div class="sv-place" id="sv-place" hidden></div>
          </div>
          <div class="sv-pick" id="sv-pick" hidden></div>
        </div>
      </div>`;
    cv = host.querySelector("#sv-canvas");
    ctx = cv.getContext("2d");
    bind();
    await reload();
    fit();
  }

  /* async, and the answer MATTERS: false means the operator kept their staged
     work and the caller must not go through with whatever it was doing. */
  async function unmount(){
    if (hasPending() && !(await askConfirm({
      title: `${pendingCount()} change(s) have not been written. Leave and lose them?`,
      body: "Nothing staged has touched the file yet — leaving drops the whole batch.",
      ok: "leave", danger: true,
    }))) return false;
    clearStaged();
    host = null; cv = null; sel = null; list = null;
    return true;
  }

  /* Everything not yet on disk, gone. One place, so no reset path can forget
     half of it and leave a ghost of a node that was never written. */
  function clearStaged(){
    pending.clear();
    staged = []; selStaged = -1; placing = null;
  }

  async function reload(){
    if (!scene) return;
    clearStaged();
    paintPending();
    paintPlacing();
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
                          type: it.type, count: 0, kids: 0, item: it });
    });
    list.items.forEach(it => {
      const host = seen.get(layerOf(it));
      if (!host) return;
      if (it.path !== host.path) host.kids++;
      if (it.draw && ["image", "rect", "tiles"].includes(it.draw.kind))
        host.count += it.draw.kind === "tiles" ? it.draw.cells.length : 1;
    });
    return [...seen.values()].map(l => ({ ...l, state: layerState(l) }));
  }

  /* WHAT IS IN THIS LAYER — a number, or the reason there is no number.
   *
   * The strip used to print a bare count and leave the cell blank when the
   * count was zero, so "Ground 1200 · Props · Walls 320 · Characters" read as
   * two working layers and two broken ones. They are not broken: Props is a
   * TileMapLayer with no cells placed, and Characters is a container a script
   * fills at run time. Both of those are facts the file states plainly, and a
   * strip that omits them makes the user hunt for a bug that is not there.
   */
  const LAYER_KIND_STATE = { image:"art", rect:"art", camera:"camera frame",
                             body:"collision" };
  function layerState(l){
    const d = (l.item && l.item.draw) || {};
    if (d.kind === "tiles")
      return { text: `${d.cells.length} tiles`, kind: "ok",
               why: `${d.cells.length} tile(s) placed on this TileMapLayer` };
    if (l.type === "TileMapLayer" || l.type === "TileMap")
      return { text: d.reason || "no tiles", kind: "none",
               why: `TileMapLayer — ${d.reason || "no tiles"}. Tiles are not `
                    + "nodes, so there is nothing here to select or edit." };
    if (l.kids)
      return { text: `${l.kids} node${l.kids === 1 ? "" : "s"}`, kind: "ok",
               why: `${l.kids} node(s) under ${l.name} in the file` };
    if (d.kind !== "marker")
      return { text: LAYER_KIND_STATE[d.kind] || "1 node", kind: "ok", why: "" };
    const script = fillerScript(l.item);
    if (script)
      return { text: "run time", kind: "rt",
               why: `${l.name} is empty in the FILE — ${script.split("/").pop()}`
                    + " fills it with add_child when the game runs" };
    // "no children in the file" is the chip's own case; spelling it out in a
    // pill three words wide is worse than "empty" plus the reason on hover.
    return { text: d.reason && d.reason !== "no children in the file"
                   ? d.reason : "empty", kind: "none",
             why: `${l.name} — ${d.reason || "nothing in the file"}. Nothing `
                  + "here to select." };
  }

  /* Which script puts the contents there. The node's own if it has one, else
     the nearest ancestor that does — that is as far as the FILE knows, and
     naming a script that is merely nearby beats "something, at run time". */
  function fillerScript(it){
    if (!it || !list) return "";
    let path = it.path;
    for (let i = 0; i < 12; i++){
      const node = list.items.find(n => n.path === path);
      if (node && node.script) return node.script;
      if (!path || path === ".") return "";
      path = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ".";
    }
    return "";
  }

  function layerVisible(path){
    if (isolated) return path === isolated;
    return !hiddenLayers.has(path);
  }

  function drawable(it){
    if (!it.visible && !opts.showHidden) return false;
    if (deleted(it.path)) return false;
    if (it.path !== "." && !layerVisible(layerOf(it))) return false;
    const k = it.draw && it.draw.kind;
    if ((k === "body" || k === "marker") && !opts.showBodies) return false;
    return true;
  }

  /* Staged for deletion — and so is everything under it, because unwire takes
     the subtree. Leaving the children on screen after their parent vanished
     would misreport what `apply` is about to do. */
  function deleted(path){
    for (const op of staged)
      if (op.op === "delete"
          && (path === op.path || path.startsWith(op.path + "/"))) return true;
    return false;
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
          const tint = `var(${tintVar(l.role)})`;   // CSS, so var() is fine here
          const st = l.state;
          return `<span class="sv-layer${on ? "" : " off"}${
            isolated === l.path ? " solo" : ""}${
            st.kind === "rt" ? " rt" : ""}" style="--lt:${tint}">
            <button class="eye" title="${on ? "Hide" : "Show"} ${E(l.name)} in this view only"
                    onclick="SceneView.toggleLayer('${E(l.path)}')">${on ? "◉" : "○"}</button>
            <button class="nm" title="Show only ${E(l.name)}"
                    onclick="SceneView.isolateLayer('${E(l.path)}')">${E(l.name)}</button>
            <span class="ct${st.kind === "ok" ? "" : ` ${st.kind}`}"
                  title="${E(st.why || st.text)}">${E(st.text)}</span></span>`;
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
      if (lx >= b.x && ly >= b.y && lx <= b.x + b.w && ly <= b.y + b.h)
        return owner(it);
    }
    return null;
  }

  /* Clicking the art inside an instanced scene selects THE INSTANCE, the way
     Godot does. The sprite you hit lives in prop.tscn, not in this file — there
     is no line here to move, so offering to move it would stage a write that
     silently does nothing. */
  function owner(it){
    if (!it || !it.of) return it;
    return list.items.find(i => i.path === it.of) || it;
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

  // A ground change invalidates every colour already painted into the canvas.
  // BGTheme.flush() has already run by the time this fires (same event, earlier
  // listener), so paint() picks up the new values.
  try{ window.addEventListener("bgate:theme", () => { try{ paint(); }catch(e){} }); }catch(e){}

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
    // Once per frame, not once per node: blankNodes() filters the whole item
    // list, and asking it inside the draw loop would make a 1700-node scene
    // quadratic on every pan.
    blankCount = blankNodes().length;
    const dpr = size();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = cv.width / dpr, H = cv.height / dpr;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = BGTheme.color("--bg"); ctx.fillRect(0, 0, W, H);

    const v = list.viewport;
    if (opts.grid) grid(W, H);

    // The game's own frame, so "off screen" is visible as off screen.
    const [vx, vy] = toScreen(0, 0);
    ctx.save();
    ctx.globalAlpha = .75;
    ctx.fillStyle = BGTheme.color("--surface-1");
    ctx.fillRect(vx, vy, v[0] * view.z, v[1] * view.z);
    ctx.globalAlpha = .3;
    ctx.strokeStyle = BGTheme.color("--text"); ctx.lineWidth = 1;
    ctx.strokeRect(vx + .5, vy + .5, v[0] * view.z, v[1] * view.z);
    ctx.restore();

    // Under the nodes, over the frame fill: the engine's frame is a BACKDROP,
    // not a layer you can select. Everything below still hit-tests normally.
    paintReal(vx, vy, v[0] * view.z, v[1] * view.z);

    ctx.imageSmoothingEnabled = false;
    const shown = list.items.filter(drawable);
    shown.forEach(it => item(it));
    staged.forEach((op, i) => { if (op.op === "add") ghost(op, i === selStaged); });
    if (sel && !deleted(sel.path)) gizmo(sel);
    const DRAWS = ["image", "rect", "tiles"];
    if (!shown.some(i => i.draw && DRAWS.includes(i.draw.kind))
        && !staged.some(o => o.op === "add"))
      emptyState(vx, vy, v);

    const h = hover ? `  ·  ${hover.path}` : "";
    host.querySelector("#sv-hud").textContent =
      `${v[0]}×${v[1]}  ·  ${Math.round(view.z * 100)}%  ·  ${
        list.items.filter(drawable).length} drawn${h}`;
    // NAME them. A bare count is a number you cannot act on: "3 node(s) draw
    // nothing" leaves you hunting the canvas for three things that are, by
    // definition, invisible. Clicking the line walks them one by one.
    const blanks = blankNodes();
    const tip = host.querySelector("#sv-tip");
    if (tip){
      const names = blanks.slice(0, 3).map(i => i.name).join(", ");
      tip.textContent = blanks.length
        ? `${blanks.length} node(s) draw nothing in the FILE — ${names}${
            blanks.length > 3 ? ` +${blanks.length - 3} more` : ""} · click to step through`
        : "drag to move · handles scale · ring rotates · shift = free";
      tip.title = blanks.length
        ? blanks.map(i => `${i.path} — ${i.draw.reason}`).join("\n") : "";
      tip.classList.toggle("hot", blanks.length > 0);
    }
    const u = document.getElementById("sv-undo");
    if (u) u.disabled = !pendingCount();
  }

  /* Every node that is in the file but puts nothing on screen, with the file's
     own reason why. These are the nodes you cannot select on the canvas, so
     they are exactly the ones a viewport has to name out loud. */
  function blankNodes(){
    if (!list) return [];
    return list.items.filter(i => i.draw && i.draw.kind === "marker"
                                  && i.draw.reason
                                  // Insides of an instance are another file's
                                  // problem, and an instance that DID open has
                                  // a picture — its own node is just the anchor.
                                  && !i.of && !(i.instance && i.drawn)
                                  && !deleted(i.path));
  }

  /* Walk them. Selecting centres the gizmo on a node that draws nothing, which
     is the only way to point at something invisible. */
  let blankAt = 0;
  function nextBlank(){
    const blanks = blankNodes();
    if (!blanks.length || !host) return;
    if (blankAt >= blanks.length) blankAt = 0;
    const it = blanks[blankAt++];
    const r = host.querySelector("#sv-stage").getBoundingClientRect();
    view.x = r.width / 2 - it.x * view.z;
    view.y = r.height / 2 - it.y * view.z;
    select(it.path);
  }

  /* An empty frame is indistinguishable from a broken viewport, and this tool
   * produces empty frames legitimately: a scene can be a script host with one
   * node, or a scene whose art is all assigned at run time. Say which. */
  function emptyState(vx, vy, v){
    const n = list.items.length;
    const runtime = blankNodes().length;
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
    ctx.fillStyle = BGTheme.color("--text");
    ctx.font = "13px ui-monospace,monospace";
    ctx.fillText(lines[0], cx, cy - 8);
    ctx.fillStyle = BGTheme.color("--text-3");
    ctx.font = "11px ui-monospace,monospace";
    lines.slice(1).forEach((line, i) => ctx.fillText(line, cx, cy + 12 + i * 15));
    ctx.restore();
  }

  function grid(W, H){
    const step = Math.max(4, opts.snap) * view.z;
    if (step < 6) return;
    ctx.save();
    ctx.globalAlpha = .05;
    ctx.strokeStyle = BGTheme.color("--text"); ctx.lineWidth = 1;
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

    if (!shape(d, b)){
      if (d.kind === "camera"){
        ctx.strokeStyle = tintOf("camera"); ctx.lineWidth = 2 / (view.z * it.sx);
        ctx.setLineDash([8 / (view.z * it.sx), 6 / (view.z * it.sx)]);
        ctx.strokeRect(b.x, b.y, b.w, b.h);
        ctx.setLineDash([]);
      } else if (opts.showBodies){
        ctx.strokeStyle = tintOf(it.role); ctx.lineWidth = 1 / (view.z * it.sx);
        ctx.globalAlpha *= .8;
        ctx.beginPath();
        ctx.moveTo(-7, 0); ctx.lineTo(7, 0); ctx.moveTo(0, -7); ctx.lineTo(0, 7);
        ctx.stroke();
      }
    }
    ctx.restore();

    // A node that draws nothing says WHY. Most of these are sprites whose
    // SpriteFrames is assigned by a script at load — this view shows what the
    // scene FILE declares, and silence there reads as a broken viewport rather
    // than as the accurate answer it is.
    // An instance whose scene opened is not one of these: its own entry draws
    // nothing because its CHILDREN carry the picture, and labelling forty desks
    // "instance of prop.tscn" buries the canvas in captions.
    // ...and only while there are FEW of them. The guard above assumed blank
    // markers come in ones and twos. A dressed room is 579 prop instances whose
    // .tscn the scene never opens, and 579 copies of the same sentence overdraw
    // each other into a grey pulp with the level hidden somewhere underneath —
    // the caption stops being an explanation and becomes the thing in the way.
    // Past the cap the tip bar already says how many there are and steps
    // through them by name, and the selected one still captions itself, so
    // nothing is lost but the pulp.
    if (d.kind === "marker" && d.reason && opts.showBodies && view.z > 0.45
        && !(it.instance && it.drawn)
        && (blankCount <= BLANK_LABEL_CAP || it.path === sel)){
      const [mx, my] = toScreen(it.x, it.y);
      ctx.save();
      ctx.font = "10px ui-monospace,monospace";
      ctx.globalAlpha *= .8;
      ctx.fillStyle = tintOf(it.role);
      ctx.fillText(`${it.name} — ${d.reason}`, mx + 10, my + 3);
      ctx.restore();
    }

    if (opts.outlines && d.kind !== "marker"){
      const pts = corners(it).map(p => toScreen(p[0], p[1]));
      ctx.save();
      ctx.globalAlpha *= .33;
      ctx.strokeStyle = tintOf(it.role);
      ctx.lineWidth = 1;
      ctx.beginPath();
      pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
      ctx.closePath(); ctx.stroke();
      ctx.restore();
    }
  }

  /* The picture itself, in the caller's already-transformed space. Split out of
     item() so a placement that is not in the file yet can be drawn with exactly
     the same code — a preview drawn by a second, nearly-identical painter is a
     preview that lies about where the thing will land. Returns whether it drew;
     the caller owns the non-picture cases (camera frame, body cross). */
  function shape(d, b){
    if (d.kind === "tiles"){ tiles(d); return true; }
    if (d.kind === "image"){
      const im = images.get(d.rel);
      if (im){
        const r = d.region || [0, 0, im.width, im.height];
        ctx.drawImage(im, r[0], r[1], r[2], r[3], b.x, b.y, b.w, b.h);
      } else {
        ctx.fillStyle = BGTheme.color("--bad");
        ctx.globalAlpha *= .5;
        ctx.fillRect(b.x, b.y, b.w, b.h);
      }
      return true;
    }
    if (d.kind === "rect"){
      const c = d.color || [.4, .4, .45, .9];
      ctx.fillStyle = `rgba(${(c[0]*255)|0},${(c[1]*255)|0},${(c[2]*255)|0},${c[3]})`;
      ctx.fillRect(b.x, b.y, b.w, b.h);
      return true;
    }
    return false;
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
    ctx.strokeStyle = BGTheme.color("--accent"); ctx.lineWidth = 1.5;
    ctx.beginPath();
    pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    ctx.closePath(); ctx.stroke();

    const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    const all = [...pts, mid(pts[0], pts[1]), mid(pts[1], pts[2]),
                 mid(pts[2], pts[3]), mid(pts[3], pts[0])];
    ctx.fillStyle = BGTheme.color("--accent");
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

    ctx.fillStyle = BGTheme.color("--accent"); ctx.font = "11px ui-monospace,monospace";
    ctx.fillText(it.name, pts[0][0], pts[0][1] - 8);
    ctx.restore();
  }

  /* ── placing ──────────────────────────────────────────────────────────────
   * The move this panel existed to make possible and could not: put a SCENE in
   * a scene. Everything the backend needs has been there all along — wire()
   * emits `instance=ExtResource(...)` for a .tscn and /api/scene/wire is live —
   * but no surface could reach it, because the only asset picker in the builder
   * filters .tscn out (every scene is kind="screen" to the atlas, and offering
   * a screen as a sprite would be nonsense). So this is its own picker.
   *
   * A placement is STAGED like every other edit here: the ghost is drawn from
   * the source scene's own draw list, dragging moves the ghost, and nothing is
   * written until `apply` — which is one wire call plus one position write.
   */
  const DRAWN = ["image", "rect", "tiles"];

  function rootName(){
    const r = list && list.items.find(i => i.path === ".");
    return (r && r.name) || "the root";
  }

  /* Where a placement lands: under the selection, else at the root. Any node
     can parent another in Godot, so this does not second-guess the choice — it
     says out loud where the thing is going and lets the click change it. */
  function placeParent(){
    return sel && sel.path !== "." && !sel.of ? sel.path : ".";
  }

  async function placeMenu(){
    const el = document.getElementById("sv-pick");
    if (!el) return;
    if (!el.hidden){ el.hidden = true; return; }
    const d = await readJSON("/api/scene/wirable", null);
    const scenes = ((d && d.scenes) || []).filter(s => s.scene !== scene);
    const parent = placeParent();
    el.innerHTML = `<div class="hd">place under ${
      E(parent === "." ? rootName() : parent)}</div>`
      + (scenes.length
        ? scenes.map(s => `<button onclick="SceneView.arm('${E(s.scene)}')"
            title="${E(s.scene)}">${E(s.label)}<span>${s.nodes} node${
            s.nodes === 1 ? "" : "s"}</span></button>`).join("")
        : `<div class="no">this project has no other .tscn to place.</div>`);
    el.hidden = false;
  }

  /* Read the scene being placed so the ghost is the real art, not a rectangle
     with a name on it. One fetch per source, cached on the armed placement. */
  async function arm(src){
    const pick = document.getElementById("sv-pick");
    if (pick) pick.hidden = true;
    const r = await readJSON(`/api/scene/render?scene=${encodeURIComponent(src)}`,
                             null);
    if (!r || r.__error){ say((r && r.__error) || "could not read that scene"); return; }
    const rels = new Set();
    r.items.forEach(i => {
      const d = i.draw || {};
      if (d.rel) rels.add(d.rel);
      if (d.kind === "tiles") Object.values(d.sources).forEach(s => rels.add(s.rel));
    });
    await Promise.all([...rels].map(load));
    const parent = placeParent();
    placing = { src, render: r, parent, box: ghostBox(r),
                name: nextName(src, parent) };
    paintPlacing();
    paint();
  }

  function cancelPlacing(){
    if (!placing) return;
    placing = null;
    paintPlacing();
    paint();
  }

  function paintPlacing(){
    const el = document.getElementById("sv-place");
    if (!el) return;
    el.hidden = !placing;
    if (!placing) return;
    el.innerHTML = `<span>click the canvas to place <b>${E(placing.name)}</b>
      under <b>${E(placing.parent === "." ? rootName() : placing.parent)}</b></span>
      <span style="flex:1"></span>
      <button class="sv-b" onclick="SceneView.cancelPlacing()">done</button>`;
  }

  /* Desk_01, Desk_02 — never Node2D7. The name is the only handle you have on a
     node in a list of forty, and a counter that restarts at every gap reads as
     a bug, so it walks up past everything already taken. */
  function baseName(src){
    const stem = String(src).split("/").pop().replace(/\.tscn$/i, "");
    const parts = stem.split(/[^A-Za-z0-9]+/).filter(Boolean);
    return parts.map(p => p[0].toUpperCase() + p.slice(1)).join("") || "Node";
  }

  function nextName(src, parent){
    const base = baseName(src);
    const taken = new Set();
    if (list) list.items.forEach(i => { if (!i.of) taken.add(i.name); });
    staged.forEach(o => { if (o.op === "add") taken.add(o.name); });
    for (let i = 1; i < 1000; i++){
      const cand = `${base}_${String(i).padStart(2, "0")}`;
      if (!taken.has(cand)) return cand;
    }
    return base;
  }

  /* The source scene's drawn extent, in its own space — the box you grab the
     ghost by. Rotation is ignored deliberately: this is a pick target, and an
     axis-aligned box you can always hit beats an exact one you cannot. */
  function ghostBox(render){
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    render.items.forEach(s => {
      if (!s.draw || !DRAWN.includes(s.draw.kind)) return;
      const b = localBox(s);
      [[b.x, b.y], [b.x + b.w, b.y + b.h]].forEach(([lx, ly]) => {
        const px = s.x + lx * s.sx, py = s.y + ly * s.sy;
        x0 = Math.min(x0, px); y0 = Math.min(y0, py);
        x1 = Math.max(x1, px); y1 = Math.max(y1, py);
      });
    });
    if (!isFinite(x0)) return { x:-12, y:-12, w:24, h:24 };
    return { x:x0, y:y0, w:Math.max(1, x1 - x0), h:Math.max(1, y1 - y0) };
  }

  function ghost(op, selected){
    ctx.save();
    ctx.globalAlpha = 0.8;
    (op.render.items || []).forEach(s => {
      const d = s.draw || {};
      if (!DRAWN.includes(d.kind)) return;
      ctx.save();
      const [px, py] = toScreen(op.wx + s.x, op.wy + s.y);
      ctx.translate(px, py);
      ctx.rotate(s.rot);
      ctx.scale(view.z * s.sx, view.z * s.sy);
      shape(d, localBox(s));
      ctx.restore();
    });
    ctx.restore();

    // Dashed, because it is not in the file yet and should not look like it is.
    const b = op.box;
    const [bx, by] = toScreen(op.wx + b.x, op.wy + b.y);
    ctx.save();
    ctx.strokeStyle = BGTheme.color(selected ? "--accent" : "--warn");
    ctx.lineWidth = selected ? 1.5 : 1;
    ctx.setLineDash([5, 4]);
    ctx.strokeRect(bx, by, b.w * view.z, b.h * view.z);
    ctx.setLineDash([]);
    ctx.fillStyle = BGTheme.color(selected ? "--accent" : "--warn");
    ctx.font = "11px ui-monospace,monospace";
    ctx.fillText(`${op.name} · new`, bx, by - 6);
    ctx.restore();
  }

  function hitGhost(wx, wy){
    for (let i = staged.length - 1; i >= 0; i--){
      const op = staged[i];
      if (op.op !== "add") continue;
      const b = op.box;
      if (wx >= op.wx + b.x && wx <= op.wx + b.x + b.w
          && wy >= op.wy + b.y && wy <= op.wy + b.y + b.h) return i;
    }
    return -1;
  }

  function stageAdd(wx, wy){
    staged.push({ op:"add", src: placing.src, render: placing.render,
                  box: placing.box, name: placing.name, parent: placing.parent,
                  wx, wy, seq: ++seq });
    // Stay armed: placing one desk is rare, placing eight is the job.
    placing = { ...placing, name: nextName(placing.src, placing.parent) };
    selStaged = staged.length - 1;
    sel = null;
    paintPlacing();
    paintPending();
    paint();
  }

  /* Delete: the selection, staged. A not-yet-written placement just disappears
     — there is nothing to un-write — and a real node becomes an unwire that
     runs with everything else at `apply`. */
  function removeSelected(){
    if (selStaged >= 0 && staged[selStaged]){
      staged.splice(selStaged, 1);
      selStaged = -1;
      paintPending(); paint();
      return;
    }
    if (!sel){ say("select a node first"); return; }
    if (sel.path === "."){ say("the root node cannot be removed here"); return; }
    if (sel.of){ say(`${sel.name} lives inside ${sel.of} — delete the instance`); return; }
    if (deleted(sel.path)) return;
    const kids = list.items.filter(
      i => !i.of && i.path.startsWith(sel.path + "/")).length;
    staged.push({ op:"delete", path: sel.path, name: sel.name, kids,
                  seq: ++seq });
    sel = null;
    paintPending();
    paint();
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
      if (placing){
        let [px, py] = toWorld(sx, sy);
        if (opts.snapOn && !ev.shiftKey){
          px = Math.round(px / opts.snap) * opts.snap;
          py = Math.round(py / opts.snap) * opts.snap;
        }
        stageAdd(px, py);
        return;
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
      // A placement not yet in the file sits on top of everything, because it
      // is the thing you just put there and are still positioning.
      const g = hitGhost(wx, wy);
      if (g >= 0){
        selStaged = g; sel = null;
        drag = { ghost: staged[g], gx: wx - staged[g].wx, gy: wy - staged[g].wy };
        select(null);
        paint();
        return;
      }
      selStaged = -1;
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
        hover = placing ? null : hit(wx, wy);
        cv.style.cursor = placing ? "crosshair"
          : handleAt(sx, sy) ? "nwse-resize"
          : hitGhost(wx, wy) >= 0 || hover ? "move" : "default";
        paint();
        return;
      }
      if (drag.pan){
        view.x = drag.ox + (sx - drag.sx);
        view.y = drag.oy + (sy - drag.sy);
        paint(); return;
      }
      if (drag.ghost){
        let nx = wx - drag.gx, ny = wy - drag.gy;
        if (opts.snapOn && !ev.shiftKey){
          nx = Math.round(nx / opts.snap) * opts.snap;
          ny = Math.round(ny / opts.snap) * opts.snap;
        }
        drag.ghost.wx = nx; drag.ghost.wy = ny;
        paint(); return;
      }
      if (drag.move){
        let nx = wx - drag.gx, ny = wy - drag.gy;
        if (opts.snapOn && !ev.shiftKey){
          nx = Math.round(nx / opts.snap) * opts.snap;
          ny = Math.round(ny / opts.snap) * opts.snap;
        }
        // Children move with their parent, because they do. Moving only the
        // node itself left an instance's art standing where it was — and an
        // instance's own entry draws nothing, so the drag looked like a no-op.
        const dx = nx - drag.it.x, dy = ny - drag.it.y;
        const prefix = drag.it.path === "." ? "" : drag.it.path + "/";
        if (prefix) list.items.forEach(i => {
          if (i.path.startsWith(prefix)){ i.x += dx; i.y += dy; }
        });
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
      // A ghost carries its own position — there is no file line to stage yet.
      if (d.ghost){ paint(); return; }
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
    // Once, not per mount: escape is a global gesture and a stack of identical
    // listeners is a leak that only shows up as work done N times.
    if (!bind._keys){
      bind._keys = true;
      window.addEventListener("keydown", ev => {
        if (ev.key !== "Escape" || !host) return;
        const pick = document.getElementById("sv-pick");
        if (pick && !pick.hidden){ pick.hidden = true; return; }
        cancelPlacing();
      });
    }
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
    bucket.keys.set(key, { value, prev: existing ? existing.prev : prevValue,
                           seq: ++seq });
    pending.set(it.path, bucket);
    paintPending();
    paint();
  }

  function pendingCount(){
    let n = staged.length;
    pending.forEach(b => { n += b.keys.size; });
    return n;
  }

  function paintPending(){
    const bar = document.getElementById("sv-pending");
    if (!bar) return;
    const n = pendingCount();
    bar.hidden = !n;
    const touched = new Set(pending.keys());
    staged.forEach(o => touched.add(o.op === "add"
      ? `${o.parent}/${o.name}` : o.path));
    // Structure is spelled out, because "3 unsaved changes" over a delete reads
    // exactly like "3 unsaved changes" over three nudges, and one of those is
    // recoverable by dragging back.
    const adds = staged.filter(o => o.op === "add").length;
    const dels = staged.filter(o => o.op === "delete").length;
    const extra = [adds ? `${adds} new` : "", dels ? `${dels} deleted` : ""]
      .filter(Boolean).join(", ");
    const label = document.getElementById("sv-pending-n");
    if (label) label.textContent =
      `${n} unsaved change${n === 1 ? "" : "s"} across ${touched.size} node${
        touched.size === 1 ? "" : "s"}${extra ? ` · ${extra}` : ""}`;
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

  /* One confirmed action, one pass, one backup per file — not one per pixel.
   *
   * Additions go first so a property staged against a node can find it, and
   * deletions go last so nothing is written to a node on its way out. Each is
   * an existing endpoint doing its existing job; the only thing new here is
   * that they all wait for the same confirmation. */
  async function applyPending(){
    if (busy || !pendingCount()) return;
    const n = pendingCount();
    const adds = staged.filter(o => o.op === "add");
    const dels = staged.filter(o => o.op === "delete");
    const lines = [];
    if (adds.length) lines.push(`add ${adds.map(o => o.name).join(", ")}`);
    if (dels.length) lines.push(`DELETE ${dels.map(
      o => o.name + (o.kids ? ` (+${o.kids} child node(s))` : "")).join(", ")}`);
    lines.push("The current file is kept under .bgate_out/scene_backups.");
    const go = await askConfirm({
      title: `Write ${n} change${n === 1 ? "" : "s"} to ${String(scene).split("/").pop()}?`,
      body: lines, ok: "write to the file", danger: true,
    });
    if (!go || busy) return;
    busy = true;
    const failed = [];
    // One property write, called from two places — a placement's position and
    // the staged drags. Two literal call sites would be two chances for one of
    // them to drift out from behind this confirmation.
    const write = (node, key, value) => mutate("/api/scene/node/property", {
      body: { scene, node, key, value }, quiet: true });

    for (const op of adds){
      const r = await mutate("/api/scene/wire", {
        body: { scene, asset: op.src, parent: op.parent, node_name: op.name },
        quiet: true });
      if (!r.ok){ failed.push(`${op.name}: ${r.error}`); continue; }
      // wire() uniquifies the name against the file, so the path to write the
      // position onto is the one it reports back, not the one we asked for.
      const final = (r.data && r.data.node) || op.name;
      const path = op.parent === "." ? final : `${op.parent}/${final}`;
      const [lx, ly] = localUnder(op.parent, op.wx, op.wy);
      const p = await write(path, "position",
                            `Vector2(${round(lx, 2)}, ${round(ly, 2)})`);
      if (!p.ok) failed.push(`${path}.position: ${p.error}`);
    }

    for (const [path, bucket] of pending){
      if (deleted(path)) continue;
      for (const [key, change] of bucket.keys){
        const r = await write(path, key, change.value);
        if (!r.ok) failed.push(`${path}.${key}: ${r.error}`);
      }
    }

    for (const op of dels){
      const r = await mutate("/api/scene/unwire", {
        body: { scene, node: op.path, recursive: true }, quiet: true });
      if (!r.ok) failed.push(`delete ${op.path}: ${r.error}`);
    }

    busy = false;
    clearStaged();
    paintPending();
    paintPlacing();
    await reload();
    if (failed.length) say(`${failed.length} change(s) failed — ${failed[0]}`);
    else say(`${n} change${n === 1 ? "" : "s"} written`, "ok");
  }

  /* Re-read the file. Whatever was staged never existed. */
  async function discardPending(){
    if (!pendingCount()) return;
    clearStaged();
    paintPending();
    paintPlacing();
    await reload();
    say("staged changes discarded", "ok");
  }

  function hasPending(){ return pendingCount() > 0; }

  /* Node transforms are LOCAL in the file and WORLD in the draw list, so a
     child of a moved parent must have its parent's transform removed again
     before the number is written. Skipping this writes the world position into
     a local field and the node jumps the moment the scene reloads. */
  function toLocalTransform(it, wx, wy){
    return localUnder(it.path.includes("/")
      ? it.path.slice(0, it.path.lastIndexOf("/")) : ".", wx, wy);
  }

  /* Same conversion, keyed on the parent's path — a placement has no item of
     its own yet, only the container it is going into. */
  function localUnder(parentPath, wx, wy){
    const parent = parentPath && parentPath !== "." && list
      ? list.items.find(p => p.path === parentPath) : null;
    if (!parent) return [wx, wy];
    const dx = wx - parent.x, dy = wy - parent.y;
    const cos = Math.cos(-parent.rot), sin = Math.sin(-parent.rot);
    return [(dx * cos - dy * sin) / (parent.sx || 1),
            (dx * sin + dy * cos) / (parent.sy || 1)];
  }

  /* Undo pops the last STAGED change back off. Nothing has been written, so
     there is nothing to un-write — it just edits the pending list. */
  function undoLast(){
    // Whichever really was last, across both kinds. Map insertion order alone
    // could not answer that once structure staged alongside properties.
    let best = null;
    pending.forEach((bucket, path) => {
      bucket.keys.forEach((change, key) => {
        if (!best || change.seq > best.seq)
          best = { kind:"prop", seq: change.seq, path, key, change };
      });
    });
    staged.forEach((op, i) => {
      if (!best || op.seq > best.seq) best = { kind:"op", seq: op.seq, i };
    });
    if (!best) return;
    if (best.kind === "op"){
      staged.splice(best.i, 1);
      if (selStaged === best.i) selStaged = -1;
      else if (selStaged > best.i) selStaged--;
    } else {
      const bucket = pending.get(best.path);
      bucket.keys.delete(best.key);
      if (!bucket.keys.size) pending.delete(best.path);
      // Put the number back on the item so the picture matches the pending list.
      const it = list.items.find(i => i.path === best.path);
      if (it) restoreValue(it, best.key, best.change.prev);
    }
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
    if (sel) selStaged = -1;          // one selection, real or staged
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
  /* ── the engine's frame ───────────────────────────────────────────────── */
  function realBtn(){
    const b = host && host.querySelector("#sv-real");
    if (!b) return;
    b.classList.toggle("on", !!opts.real);
    b.textContent = real.busy ? "◉ running…" : "◉ real";
    b.disabled = real.busy;
  }

  async function realView(){
    opts.real = !opts.real;
    realBtn(); paint();
    // Off, or already holding this scene's frame: nothing to run.
    if (!opts.real || real.busy || (real.img && real.scene === scene)) return;
    await shoot();
  }

  /* After a write, the backdrop is a photograph of the scene as it was. Left
     alone it shows the OLD art next to a file that already has the new one,
     which reads as "the swap did not save" — the edit landing invisibly is the
     same experience as the edit not landing. */
  function reshoot(){
    if (!opts.real || !real.img || real.busy) return;
    return shoot();
  }

  async function shoot(){
    if (!scene || real.busy) return;
    real.busy = true; real.error = null;
    realBtn();
    say("running the game for one frame — a window will open briefly");
    const r = await mutate("/api/godot/screenshot",
                           { body: { scene }, quiet: true });
    real.busy = false;
    if (!r.ok || !r.data || r.data.ok === false || !r.data.rel){
      real.img = null; real.scene = null;
      real.error = (r.data && (r.data.error || r.data.detail)) || r.error
                   || "the engine returned no frame";
      // Turning the toggle back off matters: an ON button with no backdrop is
      // indistinguishable from a backdrop that is simply black.
      opts.real = false;
      realBtn(); paint();
      return say(real.error, "bad");
    }
    const img = await new Promise(res => {
      const im = new Image();
      im.onload = () => res(im);
      im.onerror = () => res(null);
      // Cache-bust: every shot lands on the same .bgate/godot_ws/shot.png.
      im.src = `/api/preview?rel=${encodeURIComponent(r.data.rel)}&t=${Date.now()}`;
    });
    if (!img){
      opts.real = false; real.img = null;
      realBtn(); paint();
      return say("the engine's frame would not load", "bad");
    }
    real.img = img; real.scene = scene; real.at = Date.now();
    realBtn(); paint();
    say("showing the engine's own frame", "ok");
  }

  /* Paint it INTO the viewport rect the frame already defines, so a node's
     handles sit exactly where its art does. The shot comes back at a fixed
     1280×720 whatever the project's viewport is, so it is fitted rather than
     stretched — a backdrop that is 4px off is worse than none, because every
     placement you make against it inherits the error. */
  function paintReal(vx, vy, vw, vh){
    if (!opts.real || !real.img) return;
    const iw = real.img.naturalWidth, ih = real.img.naturalHeight;
    if (!iw || !ih) return;
    const k = Math.min(vw / iw, vh / ih);
    const w = iw * k, h = ih * k;
    ctx.save();
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(real.img, vx + (vw - w) / 2, vy + (vh - h) / 2, w, h);
    ctx.restore();
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
  async function setScene(id){
    if (hasPending() && !(await askConfirm({
      title: `${pendingCount()} change(s) have not been written. Switch scene and lose them?`,
      body: "Nothing staged has touched the file yet — switching drops the whole batch.",
      ok: "switch scene", danger: true,
    }))) return false;
    clearStaged();
    scene = id; sel = null;
    // Drop the previous scene's frame outright. Keeping it would leave the old
    // level painted behind the new scene's nodes — a backdrop that is silently
    // the wrong room is the one failure mode this feature must not have.
    real.img = null; real.scene = null;
    opts.real = false;
    realBtn();
    return reload();
  }

  return { mount, unmount, reload, fit, zoom: zoomBy, toggle, setSnap, snapshot,
           select, raise, undo: undoLast, setScene,
           apply: applyPending, discard: discardPending, hasPending,
           placeMenu, arm, cancelPlacing, removeSelected,
           toggleLayer, isolateLayer, showAllLayers, nextBlank,
           realView, reshoot,
           get layers(){ return layers(); },
           get list(){ return list; }, get selected(){ return sel; },
           get scene(){ return scene; },
           get pending(){ return pendingCount(); } };
})();
