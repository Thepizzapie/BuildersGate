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

  /* The toolbar used to be twelve unrelated symbol-font characters standing in
     for fit, delete, raise, lower, undo, export and play. That is the exact
     thing icons.js exists to end: they resolve out of whatever fallback font
     the OS has, so weight, optical size and baseline all drift per machine and
     no CSS reconciles them. Names, not glyphs. A name the set has not drawn
     yet renders BGIcon's visible dashed placeholder, which is a state you can
     see and fix rather than a blank. */
  const I = (name, size) =>
    window.BGIcon ? BGIcon(name, { size: size || 16 }) : "";

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
               showBodies: true, outlines: true, real: false, light: true };
  let images = new Map();       // rel -> HTMLImageElement | null
  let play = null;              // last /api/play/status
  let held = null;              // the seat holding this scene, if any

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
  /* THE UNDO STACK, bounded, with a redo alongside it.
   *
   * There used to be exactly one level: `undoLast` found the highest `seq`
   * across ops and property edits and threw it away. That is enough to take
   * back a misclick and nothing else — nudge a node four times and the first
   * three are unreachable, and there was never any way forward again.
   *
   * So every staging action appends a RECORD of what it did, and the record
   * carries both ends of the change:
   *   { kind:"prop", path, key, to, from, origin }   from === null -> was unstaged
   *   { kind:"op",   op }                            an add/clone/delete entered `staged`
   *   { kind:"drop", op }                            a staged add taken back out
   * Undo walks the record backwards, redo forwards, and the two stacks are the
   * only thing that decides order — `seq` still stamps ops so `staged` can be
   * re-sorted into the order the operator actually made them.
   *
   * Bounded because it holds render payloads for staged placements: 400 of
   * those is a scene's worth of images pinned in memory for an undo nobody is
   * going to reach for. The depth is on the buttons so the ceiling is visible
   * rather than surprising. */
  const HISTORY_MAX = 120;
  let history = [], redoStack = [];
  let placing = null;      // an armed placement, waiting for a click on the canvas
  let selStaged = -1;      // index into `staged` of a selected, not-yet-written add
  /* MULTI-SELECT. `sel` stays the PRIMARY — the one with handles, the one an
     inspector shows, the anchor a shift-click ranges from. `multi` is every
     path in the selection including that one, so single-select is just a set
     of one and no code path has to ask which mode it is in. SceneBuild is the
     authority (it owns the tree order a range means); this mirrors it. */
  let multi = new Set();
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
      ".sv-l{font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--text-3)}",
      // inline-flex, because these hold an <svg> now and BGIcon draws
      // display:block — a block child in a text button drops below the label.
      ".sv-b{display:inline-flex;align-items:center;gap:5px;padding:4px 9px;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;color:var(--text);font:inherit;font-size:11px;cursor:pointer}",
      ".sv-b:hover:not(:disabled){border-color:var(--accent)}",
      ".sv-b:disabled{opacity:.4;cursor:default}",
      ".sv-b.on{border-color:var(--accent);background:var(--surface-3)}",
      // How deep the stack is. A bounded undo that will not say how much is
      // left in it is a bounded undo you cannot trust.
      ".sv-n{font-family:var(--mono);font-size:9px;color:var(--text-3);min-width:6px}",
      ".sv-b.on .sv-n,.sv-b:hover .sv-n{color:var(--accent)}",
      ".sv-in{width:52px;background:var(--void);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font:inherit;font-size:11px;padding:3px 6px}",
      ".sv-layers{display:flex;align-items:center;gap:6px;padding:5px 9px;border-bottom:1px solid var(--seam);background:var(--iron);flex-wrap:wrap;flex:none}",
      ".sv-layer{display:inline-flex;align-items:center;border:1px solid var(--seam);border-radius:999px;overflow:hidden;font-family:var(--mono);font-size:10px}",
      ".sv-layer button{display:inline-flex;align-items:center;background:none;border:0;color:var(--text-2);font:inherit;cursor:pointer;padding:3px 6px}",
      ".sv-layer .eye{color:var(--lt)}",
      // Inside a role-tinted chip the icon set's one ember stroke reads as a
      // second, unrelated colour. Let the chip's own tint carry the whole mark.
      ".sv-layer .bgi .e{stroke:currentColor}",
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
      // Selected reads as selected on the strip too — without it, a batch
      // eye-click hides four layers with only the one you clicked looking
      // involved.
      ".sv-layer.sel{background:var(--accent-soft);border-color:var(--accent-line)}",
      ".sv-layer.sel .nm{color:var(--text)}",
      ".sv-layer.pri{border-color:var(--accent)}",
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
      // The stage and the running build, side by side. `.sv-body` exists only
      // so the two can be a ROW inside a column — the stage used to be the
      // column's only growing child.
      ".sv-body{flex:1;display:flex;min-height:0}",
      ".sv-play{width:0;flex:none;border-left:1px solid var(--seam);display:flex;flex-direction:column;background:var(--iron);overflow:hidden;transition:width .12s}",
      ".sv-play.open{width:var(--sv-play-w,min(46%,460px))}",
      // The handle only exists alongside an open build panel. It is keyed off
      // .sv-play.open rather than toggled in JS so the two can never disagree
      // — a stale handle floating over the stage would drag a pane that is not
      // on screen.
      ".sv-split{flex:none;width:0;z-index:4}",
      ".sv-body:has(.sv-play.open) .sv-split{width:7px;margin-right:-7px}",
      ".sv-play iframe{flex:1;border:0;width:100%;background:#000}",
      ".sv-ph{display:flex;gap:6px;align-items:center;padding:6px 8px;border-bottom:1px solid var(--seam);font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--ash2)}",
      ".sv-ph .sv-b{text-transform:none;letter-spacing:0}",
      ".sv-stale{color:var(--warn)}",
      ".sv-lock{display:flex;align-items:center;gap:9px;background:var(--iron);border:1px solid var(--bad);border-radius:8px;padding:6px 10px;font-family:var(--mono);font-size:10.5px;color:var(--bad)}",
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
          <button class="sv-b" onclick="SceneView.fit()" aria-label="Fit"
                  title="Fit the whole scene into the panel. The viewport OPENS at the game's own scale — this is the way out of it when you need the whole level at once. (F frames the selection.)">${I("fit")}</button>
          <button class="sv-b" onclick="SceneView.zoom(1.25)" aria-label="Zoom in"
                  title="Zoom in">${I("zoom_in")}</button>
          <button class="sv-b" onclick="SceneView.zoom(0.8)" aria-label="Zoom out"
                  title="Zoom out">${I("zoom_out")}</button>
          <button class="sv-b" id="sv-oneone" onclick="SceneView.gameScale()"
                  title="Show the scene at the size the GAME presents it">1:1</button>
          <span class="sv-l">snap</span>
          <button class="sv-b ${opts.snapOn?"on":""}" title="Snap moves to the grid — hold shift to override"
                  onclick="SceneView.toggle('snapOn')">${opts.snapOn?"on":"off"}</button>
          <input class="sv-in" type="number" min="1" max="256" value="${opts.snap}"
                 title="Grid size in pixels — also the large arrow-key nudge" onchange="SceneView.setSnap(this.value)">
          <span class="sv-l">show</span>
          <button class="sv-b ${opts.grid?"on":""}" aria-label="Grid" title="Grid" onclick="SceneView.toggle('grid')">${I("snap_grid")}</button>
          <button class="sv-b ${opts.outlines?"on":""}" aria-label="Node outlines" title="Node outlines" onclick="SceneView.toggle('outlines')">${I("outline")}</button>
          <button class="sv-b ${opts.showBodies?"on":""}" aria-label="Bodies, collision and markers" title="Bodies, collision and markers" onclick="SceneView.toggle('showBodies')">${I("collision")}</button>
          <button class="sv-b ${opts.showHidden?"on":""}" aria-label="Nodes marked invisible" title="Nodes marked invisible" onclick="SceneView.toggle('showHidden')">${I("hidden")}</button>
          <button class="sv-b ${opts.light?"on":""}" aria-label="Lighting"
                  title="The scene's own lighting — CanvasModulate over the frame and every Light2D added on top. Off is the flat, structural read."
                  onclick="SceneView.toggle('light')">${I("lighting")}</button>
          <button class="sv-b ${opts.real?"on":""}" id="sv-real"
                  title="Run the game for a moment and use its real frame as the backdrop. Shows art that scripts assign at load — the props this view can only draw as markers. Opens a game window briefly."
                  onclick="SceneView.realView()">${I("real_preview")}<span>real</span></button>
          <span style="flex:1 1 auto;min-width:8px"></span>
          <button class="sv-b" onclick="SceneView.placeMenu()"
                  title="Place one of this project's scenes as a child of the selected node">${I("place")}<span>place</span></button>
          <button class="sv-b" onclick="SceneView.duplicateSelected()" aria-label="Duplicate"
                  title="Duplicate the selection and its children (Ctrl+D) — staged, like every other edit here">${I("duplicate")}</button>
          <button class="sv-b" onclick="SceneView.removeSelected()" aria-label="Delete"
                  title="Delete the selection (Delete) — staged, like every other edit here">${I("delete")}</button>
          <button class="sv-b" onclick="SceneView.raise(1)" aria-label="Bring forward" title="Bring forward">${I("z_up")}</button>
          <button class="sv-b" onclick="SceneView.raise(-1)" aria-label="Send back" title="Send back">${I("z_down")}</button>
          <button class="sv-b" id="sv-undo" onclick="SceneView.undo()" aria-label="Undo"
                  title="Undo the last staged change (Ctrl+Z) — nothing has been written yet">${I("undo")}<span class="sv-n" id="sv-undo-n"></span></button>
          <button class="sv-b" id="sv-redo" onclick="SceneView.redo()" aria-label="Redo"
                  title="Redo (Ctrl+Shift+Z)">${I("redo")}<span class="sv-n" id="sv-redo-n"></span></button>
          <button class="sv-b" onclick="SceneView.snapshot()" aria-label="Export a PNG"
                  title="Save this view as a PNG under .bgate_out/scene_shots">${I("export_image")}</button>
          <button class="sv-b" id="sv-play-t" onclick="SceneView.togglePlay()"
                  title="Play the exported web build beside the scene. Applying a change rebuilds it.">${I("run")}<span>play</span></button>
        </div>
        <div class="sv-layers" id="sv-layers" hidden></div>
        <div class="sv-body">
        <div class="sv-stage" id="sv-stage">
          <canvas id="sv-canvas"></canvas>
          <div class="sv-hud" id="sv-hud"></div>
          <div class="sv-tip" id="sv-tip" onclick="SceneView.nextBlank()">drag to move · handles scale · ring rotates · shift = free</div>
          <div class="sv-top">
            <div class="sv-lock" id="sv-lock" hidden></div>
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
        <!-- Only draggable while the build is open, because .sv-play is
             width:0 when it is closed and a handle on a zero-width pane is a
             handle onto nothing. -->
        <div class="split sv-split" data-split="sv-play" data-split-var="--sv-play-w"
             data-split-on=".sv-body" data-split-pane="#sv-play" data-split-edge="end"
             data-split-min="240" data-split-max="70%"
             aria-label="Resize the running build — arrow keys adjust, Home resets"></div>
        <div class="sv-play" id="sv-play">
          <div class="sv-ph">
            <span id="sv-build">build</span>
            <span style="flex:1"></span>
            <button class="sv-b" id="sv-rebuild" onclick="SceneView.rebuild()"
                    title="Export the web build from the current source and reload it">${I("rebuild")}<span>rebuild</span></button>
            <button class="sv-b" onclick="SceneView.togglePlay()" aria-label="Close the build panel" title="Close">${I("close")}</button>
          </div>
          <iframe id="sv-frame" src="about:blank" title="playable build"
                  allow="autoplay; gamepad; fullscreen"></iframe>
        </div>
        </div>
      </div>`;
    cv = host.querySelector("#sv-canvas");
    ctx = cv.getContext("2d");
    // Rebuilt with the panel above, so rebind. Idempotent per element.
    if (window.Split) Split.init(host);
    bind();
    await reload();
    viewReady = false;
    openingView();
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
    blankFrame();
    host = null; cv = null; sel = null; list = null;
    return true;
  }

  /* Everything not yet on disk, gone. One place, so no reset path can forget
     half of it and leave a ghost of a node that was never written. */
  function clearStaged(){
    pending.clear();
    staged = []; selStaged = -1; placing = null;
    // The history describes the staged batch. Once that batch is written or
    // thrown away, an "undo" of it would be an offer to un-write the file —
    // which is not what this stack does and must not look like it is.
    history = []; redoStack = [];
  }

  /* A placement and a duplicate are the same thing to everything that draws,
     names and positions them: a subtree that is not in the file yet, sitting
     at a world point, with a ghost you can drag. They differ only in what
     `apply` runs — one wire call, or a plan of add/wire calls. */
  const isAdd = op => op.op === "add" || op.op === "clone";

  async function reload(){
    if (!scene) return;
    clearStaged();
    // The PREVIOUS scene's lock, cleared before the fetch rather than after —
    // a render that fails returns early, and a stale banner naming a seat that
    // holds a file nobody is looking at any more is worse than no banner.
    held = null;
    paintLock();
    paintPending();
    paintPlacing();
    const d = await readJSON(`/api/scene/render?scene=${encodeURIComponent(scene)}`, null);
    if (!d || d.__error){ say((d && d.__error) || "could not render that scene"); return; }
    list = d;
    held = d.lock || null;
    paintLock();
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
    // Re-point the selection at the NEW item objects. `sel` held a node out of
    // the list that was just replaced, so after any reload the gizmo, the
    // drag and every batch op were acting on an object nothing else could see.
    const alive = p => d.items.some(i => i.path === p);
    setSelection([...multi].filter(alive),
                 sel && alive(sel.path) ? sel.path : null);
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
     formula is a neat grid that is confidently wrong.

     The `+ w/2, + h/2` on the diamond branches is the fix for "the editor is
     off positioning-wise": map_to_local returns a cell's CENTRE, the square
     branch always did that, and the diamond branches were returning the
     diamond's top corner — so the whole floor drew half a tile up and left of
     the props standing on it. See the server function for the measurement. */
  function cellCenter(x, y, d){
    const w = d.tile_size[0], h = d.tile_size[1];
    if (d.shape === 1){
      return d.layout === 5
        ? [(x - y) * w / 2 + w / 2, (x + y) * h / 2 + h / 2]
        : [(x + y) * w / 2 + w / 2, (y - x) * h / 2 + h / 2];
    }
    return [x * w + w / 2, y * h + h / 2];
  }

  function localBox(it){
    const d = it.draw || {};
    if (d.kind === "tiles" && d.bounds){
      const b = d.bounds;
      return { x: b[0], y: b[1], w: b[2] - b[0], h: b[3] - b[1] };
    }
    // A light's `size` is its TEXTURE, which is deliberately far bigger than
    // the thing it lights. Handing that to the hit-test would put a 512px
    // click target over every desk in the room and make the props unselectable,
    // so a light picks like a marker and glows like a light.
    if (d.kind === "light" || d.kind === "tint") return { x:-8, y:-8, w:16, h:16 };
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
   * it. The `hidden` toggle on the toolbar is the control for the real property.
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

  /* A LAYER CHIP IS ANOTHER WAY TO REACH A NODE. It carries the same three
     gestures the tree does — plain click replaces, shift ranges, ctrl adds —
     and the range is over the CHIP ROW, not the tree, because eleven chips is
     what the operator is looking at. Selecting a layer selects the real node,
     so the inspector and every batch op already written apply to it. */
  function layerClick(ev, path){
    const mode = ev && ev.shiftKey ? "range"
      : ev && (ev.ctrlKey || ev.metaKey) ? "toggle" : "set";
    const order = layers().map(l => l.path);
    if (window.SceneBuild && typeof SceneBuild.pick === "function")
      SceneBuild.pick(path, mode, order);
    else setSelection([path], path);
  }

  /* THE EYE IS NOT SELECTION. Two intents on one control is how a layer gets
     hidden when someone meant to click it, so this changes visibility and
     nothing else — it does not select, and it does not clear a selection.
     It DOES batch: with the chip inside a multi-selection, the whole
     selection hides, which is the point of being able to pick four of them. */
  function layerEye(ev, path){
    if (ev){ ev.stopPropagation(); ev.preventDefault(); }
    const known = new Set(layers().map(l => l.path));
    const group = multi.has(path)
      ? [...multi].filter(p => known.has(p)) : [path];
    toggleLayer(path, group.length > 1 ? group : null);
  }

  /* View-only, deliberately, and said so on the chip: hiding a layer here does
     not touch its `visible` property. The scene-tree eye is the control for
     the real thing, and it stages. */
  function toggleLayer(path, group){
    if (isolated){ isolated = null; hiddenLayers.clear(); }
    // The clicked chip decides the direction, so a mixed group ends up
    // uniform rather than each member flipping to its own opposite.
    const hide = !hiddenLayers.has(path);
    (group || [path]).forEach(p =>
      hide ? hiddenLayers.add(p) : hiddenLayers.delete(p));
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
          const picked = multi.has(l.path);
          const n = multi.size;
          return `<span class="sv-layer${on ? "" : " off"}${
            isolated === l.path ? " solo" : ""}${
            st.kind === "rt" ? " rt" : ""}${picked ? " sel" : ""}${
            sel && sel.path === l.path ? " pri" : ""}" style="--lt:${tint}"
            role="option" aria-selected="${picked}">
            <button class="eye" title="${on ? "Hide" : "Show"} ${E(l.name)} in this view only${
                      picked && n > 1 ? ` — and the other ${n - 1} selected` : ""
                    }. This never touches the scene."
                    aria-label="${on ? "Hide" : "Show"} ${E(l.name)}"
                    onclick="SceneView.layerEye(event,'${E(l.path)}')">${I(on ? "visible" : "hidden", 13)}</button>
            <button class="nm" title="Select ${E(l.name)} — shift for a range, ctrl to add. Double-click shows only this one."
                    onclick="SceneView.layerClick(event,'${E(l.path)}')"
                    ondblclick="SceneView.isolateLayer('${E(l.path)}')">${E(l.name)}</button>
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
    // The panel was mounted before it had a size; settle the opening view now
    // that it does. openingView() repaints, so this frame can stop here.
    if (!viewReady && openingView()) return;
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

    /* THE LIGHTING PASS, in the engine's own order: everything is drawn, then
       CanvasModulate multiplies the canvas, then each Light2D is added on top.
       Doing it here rather than per-item is not an optimisation, it is the
       definition — a light is not a picture on a node, it is a contribution to
       whatever is underneath it, and there is nothing underneath it until the
       scene has been painted. Gizmos and ghosts come after, deliberately:
       editor furniture must not be dimmed by the game's own night.

       A LIGHT ONLY LIGHTS CANVAS ITEMS. That is the whole reason the scene is
       painted into an offscreen first. Godot's 2D lights modulate the things
       in range; they do not brighten the empty space around them, because
       there is nothing there to brighten. Adding the cookie straight onto the
       main canvas painted its full 400px halo over bare background as well —
       so every fixture wore a soft disc far wider than the floor it lit, pools
       bled through walls into unlit rooms, and lights near the plate edge hung
       in the void as free-floating blobs. Masking the accumulated light by the
       scene's own alpha is what turns that back into pooled illumination with
       an edge, and it is one composite per frame, not a per-light solve. */
    const lit = opts.light
      && (list.tint || shown.some(i => i.draw && i.draw.kind === "light"));
    if (!lit){
      shown.forEach(it => item(it));
    } else {
      const scene = surface("scene", W, H, dpr);
      const main = ctx;
      ctx = scene.cx;                       // item() paints wherever ctx points
      try { shown.forEach(it => item(it)); } finally { ctx = main; }
      /* THE ALPHA SNAPSHOT, taken before anything modulates the layer.
         `multiply` composites its ALPHA source-over, so tinting the scene by
         filling it turns every transparent pixel opaque — which both covered
         the canvas in a flat rectangle of tint and destroyed the very mask the
         lights were about to be clipped by. Copy the coverage first; it is one
         composite and it is what makes the other two correct. */
      const mask = surface("mask", W, H, dpr);
      mask.cx.drawImage(scene.cv, 0, 0, W, H);
      tintLayer(scene, mask, W, H);
      ctx.drawImage(scene.cv, 0, 0, W, H);
      lighting(shown, mask, W, H, dpr);
    }

    staged.forEach((op, i) => { if (isAdd(op)) ghost(op, i === selStaged); });
    if (multi.size > 1) selectedItems().forEach(it => {
      if (it !== sel && !deleted(it.path)) mark(it);
    });
    if (sel && !deleted(sel.path)) gizmo(sel);
    const DRAWS = ["image", "rect", "tiles"];
    if (!shown.some(i => i.draw && DRAWS.includes(i.draw.kind))
        && !staged.some(isAdd))
      emptyState(vx, vy, v);

    const h = hover ? `  ·  ${hover.path}` : "";
    // Say how many are selected. Twenty selected nodes and one selected node
    // look identical the moment the group is off screen, and the difference
    // decides what Delete is about to do.
    const m = multi.size > 1 ? `  ·  ${multi.size} selected` : "";
    /* ZOOM, IN THE UNITS THE OPERATOR IS COMPARING AGAINST.
       `view.z` is world-pixels-per-css-pixel, which is the Godot EDITOR's
       100%. The GAME presents this project at 2x (640x360 authored, 1280x720
       window, canvas_items + integer stretch), so a viewport reading "100%"
       was showing the scene at half the size the player sees it — measured, by
       template-matching the source art against the engine's own frame: tile
       and prop both land at exactly 2.00. Nothing was mis-scaled; the panel
       was reporting a different 100% from the one being compared with, and
       saying nothing about the difference. So NAME both percentages. A bare
       "100%" is the ambiguity itself — it is true of two different sizes on a
       stretched project, and the reader has no way to tell which one is on
       screen. `game 100% · editor 200%` cannot be misread. */
    const gs = gameScaleOf();
    const pct = Math.round(view.z * 100);
    const zoom = gs !== 1
      ? `game ${Math.round(view.z / gs * 100)}% · editor ${pct}%`
      : `${pct}%`;
    host.querySelector("#sv-hud").textContent =
      `${v[0]}×${v[1]}${gs !== 1 ? ` ×${gs}` : ""}  ·  ${zoom}  ·  ${
        list.items.filter(drawable).length} drawn${m}${h}`;
    const one = document.getElementById("sv-oneone");
    if (one){
      one.classList.toggle("on", Math.abs(view.z - gs) < 0.005);
      // "1:1" names the wrong thing on a stretched project — one WHAT to one
      // what? The button goes to the size the player sees, so say so.
      const label = gs !== 1 ? "game" : "1:1";
      if (one.textContent !== label) one.textContent = label;
      one.title = gs !== 1
        ? `Show the scene at the size the GAME presents it — this project `
          + `authors at ${v[0]}×${v[1]} and presents at ×${gs}, so the game's `
          + `own scale is ${gs * 100}% of the editor's 1:1`
        : "Show the scene at 1 world pixel per screen pixel";
    }
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
    paintHistory();
    // Every path that moves the view lands here, so this is the one place that
    // has to remember it — one hook rather than eight, and none of them missed.
    rememberView();
  }

  /* The stack, on the buttons. A bounded history that will not say how deep it
     is invites exactly one assumption — that it is infinite — and the moment
     it is not, the operator finds out by losing a step. */
  function paintHistory(){
    const set = (btn, count, title, dead) => {
      const b = document.getElementById(btn);
      if (!b) return;
      b.disabled = !count;
      b.title = count ? title : dead;
      const n = document.getElementById(btn + "-n");
      if (n) n.textContent = count ? String(count) : "";
    };
    set("sv-undo", history.length,
        `Undo (Ctrl+Z) — ${history.length} staged step(s) to take back, `
        + `${HISTORY_MAX} kept`, "Nothing staged to undo");
    set("sv-redo", redoStack.length,
        `Redo (Ctrl+Shift+Z) — ${redoStack.length} step(s) forward`,
        "Nothing to redo");
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

  /* ── lighting ─────────────────────────────────────────────────────────────
   * Why this is worth the ~50 lines: the complaint was "and lighting", and on
   * this project the whole difference between Godot's picture and ours is one
   * CanvasModulate over 44 point lights. Without it every room is the same
   * grey and nothing tells you which one you are looking at — the panel is
   * accurate about geometry and silent about the thing the level design is
   * actually made of.
   *
   * What is NOT modelled, on purpose: LightOccluder2D and shadow casting. That
   * is a visibility solve per light against 66 polygons, every frame, for
   * shadow EDGES. The bar here is "recognisably the same scene", not parity.
   */
  /* Two reusable offscreens — the scene, and the light accumulating on top of
     it. Kept between frames because allocating a 935x637 canvas twice per pan
     is the one thing that would make this pass cost anything. */
  const surfaces = new Map();
  function surface(name, w, h, dpr){
    let s = surfaces.get(name);
    if (!s){ s = { cv: document.createElement("canvas") };
             s.cx = s.cv.getContext("2d"); surfaces.set(name, s); }
    const pw = Math.max(1, Math.round(w * dpr)), ph = Math.max(1, Math.round(h * dpr));
    if (s.cv.width !== pw || s.cv.height !== ph){ s.cv.width = pw; s.cv.height = ph; }
    else s.cx.clearRect(0, 0, pw, ph);
    s.cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    s.cx.imageSmoothingEnabled = false;
    return s;
  }

  /* CanvasModulate, applied to the SCENE rather than to the frame.
     It multiplies the canvas items — which is what it does in the engine —
     and `destination-in` puts the layer's own alpha back afterwards, because
     `multiply` composites alpha source-over and would otherwise turn every
     transparent pixel into an opaque rectangle of tint. Doing it here also
     fixes a double-darkening: the `real` backdrop is a photograph that already
     has the engine's tint baked into it, and the old frame-wide multiply hit
     it a second time. */
  function tintLayer(scene, mask, W, H){
    const tint = list.tint;
    if (!tint || tint.length !== 4) return;
    if (tint[0] === 1 && tint[1] === 1 && tint[2] === 1 && tint[3] === 1) return;
    const cx = scene.cx;
    cx.save();
    cx.globalCompositeOperation = "multiply";
    cx.globalAlpha = tint[3] === undefined ? 1 : tint[3];
    cx.fillStyle = `rgb(${(tint[0]*255)|0},${(tint[1]*255)|0},${(tint[2]*255)|0})`;
    cx.fillRect(0, 0, W, H);
    // ...and put the coverage back, from the snapshot rather than from this
    // layer, which no longer knows what it used to be transparent.
    cx.globalCompositeOperation = "destination-in";
    cx.globalAlpha = 1;
    cx.drawImage(mask.cv, 0, 0, W, H);
    cx.restore();
  }

  function lighting(shown, scene, W, H, dpr){
    const lights = shown.filter(i => i.draw && i.draw.kind === "light"
                                     && i.draw.blend === 0);
    if (!lights.length) return;
    const glow = surface("light", W, H, dpr);
    const main = ctx;
    ctx = glow.cx;
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    lights.forEach(it => {
      const d = it.draw;
      /* Energy over 1 is a brighter light, not a bigger one — and canvas has
         no HDR, so it is spent on alpha and clamps. HEADROOM is what keeps
         that clamp from eating the room: `lighter` accumulates, this floor
         puts four 1.45-energy fluoro panels over one bullpen, and at full
         alpha their overlap saturates to flat white with the desks underneath
         gone. The engine has the same lights and does not blow out because it
         tone-maps; 0.6 is the cheap stand-in, chosen by eye against Godot's
         own frame on floor_tut. */
      ctx.globalAlpha = clamp((d.energy || 1)
        * (d.color[3] === undefined ? 1 : d.color[3]), 0, 1) * LIGHT_HEADROOM;
      // `local` is the instanced light scene's own root transform — the
      // scale.y = 0.5 that lands the pool on the isometric floor plane instead
      // of hanging it in the air as a sphere.
      const lo = d.local || { x:0, y:0, rot:0, sx:1, sy:1 };
      const s = d.scale || 1;
      const w = d.size[0] * s * Math.abs(it.sx * lo.sx) * view.z;
      const h = d.size[1] * s * Math.abs(it.sy * lo.sy) * view.z;
      /* WHERE THE COOKIE IS CENTRED, composed rather than added up.
         Godot puts the texture at `offset` in the LIGHT'S OWN space and then
         applies the node's transform, so `offset` and the instance root's own
         position are both scaled and rotated on the way out — adding them to
         the world position as plain numbers is only correct while every light
         in the scene sits at identity, which is exactly the case that never
         reports a bug. The pool is rotated too: an 8-degree fitting throws an
         8-degree ellipse, and drawing it axis-aligned is the same class of
         error as drawing it round. */
      const rot = (it.rot || 0) + (lo.rot || 0);
      const hc = Math.cos(it.rot || 0), hs = Math.sin(it.rot || 0);
      const px = (lo.x || 0) * it.sx, py = (lo.y || 0) * it.sy;
      const ox = (d.offset[0] || 0) * it.sx * lo.sx;
      const oy = (d.offset[1] || 0) * it.sy * lo.sy;
      const rc = Math.cos(rot), rs = Math.sin(rot);
      const [cx, cy] = toScreen(
        it.x + px * hc - py * hs + ox * rc - oy * rs,
        it.y + px * hs + py * hc + ox * rs + oy * rc);
      ctx.save();
      ctx.translate(cx, cy);
      if (rot) ctx.rotate(rot);
      if (d.gradient){
        // The cookie rebuilt natively. Drawn into a unit circle and scaled by
        // the transform, so the light's own scale.y = 0.5 squashes the pool
        // onto the isometric floor plane exactly as it does in the engine.
        ctx.scale(w / 2 || 1, h / 2 || 1);
        const g = ctx.createRadialGradient(0, 0, 0, 0, 0, 1);
        const [r, gg, b] = d.color;
        d.gradient.forEach(([at, a]) => g.addColorStop(clamp(at, 0, 1),
          `rgba(${(r*255)|0},${(gg*255)|0},${(b*255)|0},${a})`));
        ctx.fillStyle = g;
        ctx.beginPath(); ctx.arc(0, 0, 1, 0, Math.PI * 2); ctx.fill();
        ctx.restore();
        return;
      }
      const im = images.get(d.rel);
      const cookie = im && tinted(im, d.rel, d.color);
      if (cookie) ctx.drawImage(cookie, -w / 2, -h / 2, w, h);
      ctx.restore();
    });
    ctx.restore();
    // The mask: keep the accumulated light only where the scene actually put
    // pixels. Everything else — the void past the plate edge, the unlit room
    // on the far side of a partition — stays dark, because in the engine there
    // is nothing there for a light to fall on.
    ctx.globalCompositeOperation = "destination-in";
    ctx.drawImage(scene.cv, 0, 0, W, H);
    ctx = main;
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    ctx.drawImage(glow.cv, 0, 0, W, H);
    ctx.restore();
  }

  /* A light texture in the light's own colour. Canvas cannot tint a drawImage,
     so the multiply happens once into an offscreen and is kept — 44 lights over
     a handful of distinct (texture, colour) pairs, re-tinted every pan would be
     44 full-texture composites per frame. */
  const LIGHT_HEADROOM = 0.6;
  const TINT_CACHE_MAX = 48;
  const tintCache = new Map();
  function tinted(im, rel, color){
    const key = `${rel}|${(color[0]*255)|0},${(color[1]*255)|0},${(color[2]*255)|0}`;
    const had = tintCache.get(key);
    if (had) return had;
    if (!im.width || !im.height) return null;
    const c = document.createElement("canvas");
    c.width = im.width; c.height = im.height;
    const g = c.getContext("2d");
    g.drawImage(im, 0, 0);
    g.globalCompositeOperation = "multiply";
    g.fillStyle = `rgb(${(color[0]*255)|0},${(color[1]*255)|0},${(color[2]*255)|0})`;
    g.fillRect(0, 0, c.width, c.height);
    // multiply ignores the source alpha, so the falloff has to be put back or
    // every light is a hard-edged rectangle of colour.
    g.globalCompositeOperation = "destination-in";
    g.drawImage(im, 0, 0);
    if (tintCache.size >= TINT_CACHE_MAX)
      tintCache.delete(tintCache.keys().next().value);
    tintCache.set(key, c);
    return c;
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

    // An instanced scene's root can carry its own transform, and the picture
    // belongs to that root, not to the node in THIS file that points at it.
    // Applied here rather than composed into the item so a drag still reads and
    // writes this node's own position.
    // The context is already in the item's own units here, so the offset goes
    // in unscaled — multiplying by view.z again would move the picture with
    // the zoom.
    if (d.local){
      ctx.translate(d.local.x || 0, d.local.y || 0);
      ctx.rotate(d.local.rot || 0);
      ctx.scale(d.local.sx || 1, d.local.sy || 1);
    }
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
   * CENTRE on the cell centre and then SUBTRACTS the source's texture_origin —
   * `dest.position = centre - size/2 - texture_origin` — which is how a 64x100
   * wall tile stands up out of a 64x32 cell instead of being squashed into it.
   *
   * THE SIGN IS THE WHOLE BUG, and it is why the furniture never sat on the
   * walls. This used to ADD texture_origin, which for this project's own rule
   * (`texture_origin.y = h/2 - 16`, docs/SCALE.md — bottom edge on the
   * diamond's BOTTOM VERTEX) puts a tile's bottom at `centre - 16 + h` instead
   * of `centre + 16`: every tile with an origin drew `h - 32` px too low. A
   * 32px floor has origin 0 and h - 32 = 0, so the floor grid was always right
   * and only the tall tiles moved — 38px for a 70px cubicle, 68px for a 100px
   * panel — which is exactly the "the props do not line up with the tiles"
   * report, seen from the wrong side. Measured on floor_tut against the
   * engine's own 1280x720 frame: the panel art registers at the minus position
   * in Godot (NCC 0.99) and props already registered to within 1 screen px, so
   * the whole relative error lived here. */
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
                    cx - rw / 2 - src.origin[0],
                    cy - rh / 2 - src.origin[1], rw, rh);
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

  function nextName(src){ return uniqueName(baseName(src)); }

  /* Desk_07 duplicated is Desk_08, not Desk_07_01 — the trailing counter is
     part of the naming scheme, not part of the name. */
  function uniqueName(name){
    const m = /^(.*?)[_-]?(\d+)$/.exec(String(name || "Node"));
    const base = (m && m[1]) || String(name || "Node");
    const taken = new Set();
    if (list) list.items.forEach(i => { if (!i.of) taken.add(i.name); });
    staged.forEach(o => { if (isAdd(o)) taken.add(o.name); });
    for (let i = 1; i < 1000; i++){
      const cand = `${base}_${String(i).padStart(2, "0")}`;
      if (!taken.has(cand)) return cand;
    }
    return base;
  }

  /* A subtree as if it were its own scene, so a duplicate's ghost is drawn by
     exactly the code that draws a placement's — coordinates relative to the
     subtree's root, which is what ghost() and ghostBox() already expect. */
  function subtreeRender(path){
    if (!list) return { items: [] };
    const root = list.items.find(i => i.path === path);
    if (!root) return { items: [] };
    const pre = path + "/";
    return { items: list.items
      .filter(i => i.path === path || i.path.startsWith(pre))
      .map(i => ({ ...i, x: i.x - root.x, y: i.y - root.y })) };
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
      if (!isAdd(op)) continue;
      const b = op.box;
      if (wx >= op.wx + b.x && wx <= op.wx + b.x + b.w
          && wy >= op.wy + b.y && wy <= op.wy + b.y + b.h) return i;
    }
    return -1;
  }

  function stageAdd(wx, wy){
    const op = { op:"add", src: placing.src, render: placing.render,
                 box: placing.box, name: placing.name, parent: placing.parent,
                 wx, wy, seq: ++seq };
    staged.push(op);
    record({ kind:"op", op });
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
      const op = staged[selStaged];
      staged.splice(selStaged, 1);
      selStaged = -1;
      record({ kind:"drop", op });
      paintPending(); paint();
      return;
    }
    const items = selectedItems().filter(it => !deleted(it.path));
    if (!items.length){ say("select a node first"); return; }
    /* A MIXED SELECTION IS REFUSED WHOLE.
       Deleting nine of ten and saying nothing about the tenth is the exact
       failure this rule exists to stop: what the operator counts afterwards is
       what vanished, not what quietly did not. Name the ones in the way and
       change nothing. */
    const bad = items.filter(it => it.path === "." || it.of);
    if (bad.length){
      const why = bad[0].path === "."
        ? "the root node cannot be deleted here"
        : `${bad[0].name} lives inside ${bad[0].of} — delete the instance`;
      say(bad.length === items.length ? why
        : `${bad.length} of ${items.length} selected node(s) cannot be deleted (${
            bad.slice(0, 3).map(b => b.name).join(", ")}${
            bad.length > 3 ? "…" : ""}) — ${why}. Nothing was staged.`);
      return;
    }
    // Something already inside another doomed subtree is not a second delete —
    // unwire takes the whole subtree, so staging both double-counts the batch.
    const roots = items.filter(it =>
      !items.some(o => o !== it && it.path.startsWith(o.path + "/")));
    roots.forEach(it => {
      const kids = list.items.filter(
        i => !i.of && i.path.startsWith(it.path + "/")).length;
      const op = { op:"delete", path: it.path, name: it.name, kids,
                   seq: ++seq };
      staged.push(op);
      record({ kind:"op", op });
    });
    select(null);
    paintPending();
    paint();
  }

  /* ── duplicate / paste ────────────────────────────────────────────────────
   * A duplicate is a PLACEMENT of a subtree that has no .tscn: same ghost, same
   * drag, same one line in the pending bar, same single confirmation. The plan
   * — what to create, under what, with which properties — is built by
   * SceneBuild, which holds the outline the .tscn was parsed into; this only
   * stages and, at apply, runs it.
   */
  function duplicateSelected(){
    if (!window.SceneBuild || typeof SceneBuild.clonePlans !== "function")
      return say("the scene panel is not loaded");
    if (!multi.size) return say("select a node first");
    const r = SceneBuild.clonePlans([...multi]);
    if (r.error) return say(r.error);
    pasteClones(r.clones, true, null);
  }

  /* `parent` null keeps each clone beside its source (duplicate); a path pastes
     the whole clipboard under one node. */
  function pasteClones(clones, offset, parent){
    if (!list || !(clones || []).length) return;
    const step = offset ? (opts.snapOn ? opts.snap : 8) : 0;
    let staged0 = staged.length, dropped = 0;
    clones.forEach(c => {
      const dest = parent == null ? (c.parent || ".") : parent;
      if (dest !== "." && !list.items.some(i => i.path === dest)){
        say(`no node at ${dest} to paste under`);
        return;
      }
      const src = list.items.find(i => i.path === c.path);
      const anchor = src || list.items.find(i => i.path === dest)
                         || list.items.find(i => i.path === ".");
      const op = { op:"clone", src: c.path, plan: c.plan,
                   dropped: c.dropped || [], name: uniqueName(c.name),
                   parent: dest, render: subtreeRender(c.path),
                   wx: (anchor ? anchor.x : 0) + step,
                   wy: (anchor ? anchor.y : 0) + step, seq: ++seq };
      op.box = ghostBox(op.render);
      dropped += op.dropped.length;
      staged.push(op);
      record({ kind:"op", op });
    });
    const made = staged.length - staged0;
    if (!made) return;
    selStaged = staged.length - 1;
    select(null);
    paintPending();
    paint();
    say(dropped
      ? `${made} duplicate(s) staged — ${dropped} propert(y/ies) cannot be `
        + "written safely and are listed before the write"
      : `${made} duplicate(s) staged — drag to position, then apply`, "ok");
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
      // Shift ranges, ctrl/cmd toggles, a plain click replaces — the same three
      // gestures the tree uses, because they are the same selection.
      const mode = ev.shiftKey ? "range"
        : (ev.ctrlKey || ev.metaKey) ? "toggle" : "set";
      // A plain click on something ALREADY in a group must not collapse the
      // group to one: that click is the start of "drag all of these".
      const inGroup = !!found && multi.has(found.path) && multi.size > 1;
      if (!(inGroup && mode === "set")) select(found ? found.path : null, mode);
      if (found && multi.has(found.path))
        drag = { move:true, it: found, gx: wx - found.x, gy: wy - found.y,
                 x0: found.x, y0: found.y,
                 group: groupFor(found).map(it => ({ it, x0: it.x, y0: it.y })) };
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
        // The head node snaps; everything else in the group follows by the same
        // delta, so a selection keeps its internal spacing instead of each
        // member collapsing onto its own nearest grid line.
        const dx = nx - drag.it.x, dy = ny - drag.it.y;
        if (!dx && !dy) return;
        (drag.group || [{ it: drag.it }]).forEach(m => moveBy(m.it, dx, dy));
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
      if (d.move) (d.group || [{ it: d.it, x0: d.x0, y0: d.y0 }])
        .forEach(m => stageMove(m.it, m.x0, m.y0));
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

  /* Escape, in layers. The scene panel owns the key now — one listener, scoped
     to the Atlas view so it cannot fire under the code editor — and asks here
     first, because a picker or an armed placement is a nearer thing to dismiss
     than the selection. Returns whether it consumed the press. */
  function escape(){
    if (!host) return false;
    const pick = document.getElementById("sv-pick");
    if (pick && !pick.hidden){ pick.hidden = true; return true; }
    if (placing){ cancelPlacing(); return true; }
    return false;
  }

  /* F. Fit the view to the selection — the gesture that gets you back to what
     you were working on after a pan across a 4000px level. Nothing selected
     falls through to fitting the whole scene, which is what F does there. */
  function frame(){
    if (!host || !list) return;
    const items = selectedItems().filter(it => !deleted(it.path));
    if (!items.length) return fit();
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    items.forEach(it => list.items
      .filter(i => i.path === it.path || i.path.startsWith(it.path + "/"))
      .forEach(k => corners(k).forEach(([px, py]) => {
        x0 = Math.min(x0, px); y0 = Math.min(y0, py);
        x1 = Math.max(x1, px); y1 = Math.max(y1, py);
      })));
    if (!isFinite(x0)) return;
    const r = host.querySelector("#sv-stage").getBoundingClientRect();
    // A floor of 24 world units: framing a marker with no extent otherwise
    // divides by nothing and slams the zoom to its ceiling.
    const w = Math.max(24, x1 - x0), h = Math.max(24, y1 - y0);
    view.z = clamp(Math.min((r.width - 80) / w, (r.height - 80) / h), 0.05, 4);
    view.x = (r.width - w * view.z) / 2 - x0 * view.z;
    view.y = (r.height - h * view.z) / 2 - y0 * view.z;
    paint();
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
    const origin = existing ? existing.prev : prevValue;
    bucket.keys.set(key, { value, prev: origin, seq: ++seq });
    pending.set(it.path, bucket);
    // ...but UNDO must go back to nudge two, which is a different question and
    // needs the step's own other end, not the batch's.
    record({ kind:"prop", path: it.path, key, to: value, origin,
             had: !!existing, from: existing ? existing.value : null });
    paintPending();
    paint();
  }

  function record(rec){
    history.push(rec);
    if (history.length > HISTORY_MAX) history.shift();
    // A new edit is a new future. A redo kept across it would graft a change
    // back onto a state it was never taken from.
    redoStack.length = 0;
  }

  function applyRec(rec, forward){
    if (rec.kind === "prop"){
      const had = forward ? true : rec.had;
      const value = forward ? rec.to : rec.from;
      const bucket = pending.get(rec.path)
        || { name: nameOf(rec.path), keys: new Map() };
      if (had){
        bucket.keys.set(rec.key, { value, prev: rec.origin, seq: ++seq });
        pending.set(rec.path, bucket);
      } else {
        bucket.keys.delete(rec.key);
        if (bucket.keys.size) pending.set(rec.path, bucket);
        else pending.delete(rec.path);
      }
      // Put the number back on the item so the picture matches the pending list.
      const it = list && list.items.find(i => i.path === rec.path);
      if (it) restoreValue(it, rec.key, had ? value : rec.origin);
      return;
    }
    // "op" entered `staged`; "drop" took one back out. Going forward means
    // doing that again, going back means the opposite.
    const wantIn = forward ? rec.kind === "op" : rec.kind === "drop";
    const at = staged.indexOf(rec.op);
    if (wantIn && at < 0){
      staged.push(rec.op);
      // `seq` is monotonic across ops AND property edits, so it is the only
      // thing that can put a re-inserted op back where the operator made it.
      staged.sort((a, b) => a.seq - b.seq);
    }
    if (!wantIn && at >= 0) staged.splice(at, 1);
    selStaged = -1;
  }

  function nameOf(path){
    const it = list && list.items.find(i => i.path === path);
    return (it && it.name) || String(path).split("/").pop();
  }

  function undo(){
    const rec = history.pop();
    if (!rec) return;
    applyRec(rec, false);
    redoStack.push(rec);
    if (redoStack.length > HISTORY_MAX) redoStack.shift();
    paintPending();
    paint();
  }

  function redo(){
    const rec = redoStack.pop();
    if (!rec) return;
    applyRec(rec, true);
    history.push(rec);
    paintPending();
    paint();
  }

  /* Children move with their parent, because they do. Moving only the node
     itself left an instance's art standing where it was — and an instance's own
     entry draws nothing, so the drag looked like a no-op. */
  function moveBy(it, dx, dy){
    const prefix = it.path === "." ? "" : it.path + "/";
    if (prefix) list.items.forEach(i => {
      if (i.path.startsWith(prefix)){ i.x += dx; i.y += dy; }
    });
    it.x += dx; it.y += dy;
  }

  /* Arrow keys. A drag cannot do one pixel and a property field cannot do
     "a bit left" without you already knowing the number. `big` is the grid
     step the toolbar already shows, so shift-arrow and a snapped drag agree. */
  function nudge(ux, uy, big){
    if (!list) return;
    const step = big ? Math.max(1, opts.snap) : 1;
    const dx = ux * step, dy = uy * step;
    const group = groupFor(sel);
    if (!group.length){ say("select a node first"); return; }
    const inside = group.filter(it => it.of);
    if (inside.length){
      say(`${inside[0].name} lives inside ${inside[0].of} — there is no line in `
          + "this file to move");
      return;
    }
    const was = group.map(it => ({ it, x0: it.x, y0: it.y }));
    was.forEach(m => moveBy(m.it, dx, dy));
    was.forEach(m => stageMove(m.it, m.x0, m.y0));
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
    staged.forEach(o => touched.add(isAdd(o)
      ? `${o.parent}/${o.name}` : o.path));
    // Structure is spelled out, because "3 unsaved changes" over a delete reads
    // exactly like "3 unsaved changes" over three nudges, and one of those is
    // recoverable by dragging back.
    const adds = staged.filter(isAdd).length;
    const dels = staged.filter(o => o.op === "delete").length;
    const extra = [adds ? `${adds} new` : "", dels ? `${dels} deleted` : ""]
      .filter(Boolean).join(", ");
    const label = document.getElementById("sv-pending-n");
    if (label) label.textContent =
      `${n} unsaved change${n === 1 ? "" : "s"} across ${touched.size} node${
        touched.size === 1 ? "" : "s"}${extra ? ` · ${extra}` : ""}${
        history.length ? ` · ${history.length} undo` : ""}${
        redoStack.length ? `, ${redoStack.length} redo` : ""}`;
    paintHistory();
  }

  function stageMove(it, x0, y0){
    if (round(it.x, 3) === round(x0, 3) && round(it.y, 3) === round(y0, 3)) return;
    const d = { x0, y0 };
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
    const adds = staged.filter(isAdd).sort((a, b) => a.seq - b.seq);
    const dels = staged.filter(o => o.op === "delete");
    const lines = [];
    if (adds.length) lines.push(`add ${adds.map(o => o.name
      + (o.op === "clone" && o.plan.length > 1
         ? ` (+${o.plan.length - 1} child node(s))` : "")).join(", ")}`);
    if (dels.length) lines.push(`DELETE ${dels.map(
      o => o.name + (o.kids ? ` (+${o.kids} child node(s))` : "")).join(", ")}`);
    /* SAY WHAT A DUPLICATE CANNOT CARRY, BEFORE it is written.
       Two kinds of thing get left behind, and both are the writer refusing to
       guess rather than the copy being sloppy: a property whose value is
       outside the narrow set `_prop_value` will emit (a PackedVector2Array
       polygon, a Transform2D), and an override block on a node INSIDE an
       instance, which has no type to create and no endpoint that appends one.
       Finding either out afterwards means finding it out as a bug in the
       duplicate, in the game. */
    const dropped = adds.flatMap(o => o.dropped || []);
    if (dropped.length) lines.push(
      `${dropped.length} thing(s) will NOT be copied — ${
        dropped.slice(0, 4).join(", ")}${dropped.length > 4 ? "…" : ""}. `
      + "Values this writer will not emit into a .tscn, and overrides on nodes "
      + "that live inside an instanced scene.");
    lines.push("The current file is kept under .bgate_out/scene_backups.");
    if (playOpen()) lines.push("The web build will be re-exported and reloaded.");
    if (held) lines.push(`NOTE: the ${held.seat} seat holds this file — the `
                         + "write will be refused while it does.");
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

    /* One staged node into the file, whichever kind it is. `wire` for anything
       backed by a .tscn (a placement, and a duplicated instance), `add` for a
       plain node — which takes its whole property set in the same call, so a
       five-node duplicate is five requests rather than five plus thirty. */
    const born = async (asset, type, name, parent, props) => {
      const r = asset
        ? await mutate("/api/scene/wire", {
            body: { scene, asset, parent, node_name: name }, quiet: true })
        : await mutate("/api/scene/node/add", {
            body: { scene, node_type: type, name, parent, props }, quiet: true });
      if (!r.ok) return { error: r.error };
      // The writer uniquifies the name against the file, so the path to write
      // anything else onto is the one it reports back, not the one asked for.
      const final = (r.data && r.data.node) || name;
      return { path: parent === "." ? final : `${parent}/${final}` };
    };

    for (const op of adds){
      let path = null;
      if (op.op === "clone"){
        // rel -> the path it actually landed on, so a child can name its parent.
        const made = new Map();
        let broke = false;
        for (const en of op.plan){
          const under = en.parentRel === null ? op.parent : made.get(en.parentRel);
          if (under === undefined){ broke = true; break; }
          const b = await born(en.instance, en.type,
                               en.parentRel === null ? op.name : en.name,
                               under, en.properties);
          if (b.error){ failed.push(`${en.name}: ${b.error}`); broke = true; break; }
          made.set(en.rel, b.path);
          // add_node takes the whole property set in the same call; wire()
          // does not, so an instanced node sets its overrides afterwards.
          if (en.instance) for (const [k, v] of Object.entries(en.properties || {})){
            const p = await write(b.path, k, v);
            if (!p.ok) failed.push(`${b.path}.${k}: ${p.error}`);
          }
          // A duplicate with no texture is not a duplicate. Resources hang off
          // their own endpoint because attaching one is four edits to the file,
          // not one property assignment.
          for (const res of en.resources || []){
            const s = await mutate("/api/scene/node/swap", {
              body: { scene, node: b.path, asset: res.path,
                      property: res.property }, quiet: true });
            if (!s.ok) failed.push(`${b.path}.${res.property}: ${s.error}`);
          }
          if (en.script){
            const s = await mutate("/api/scene/wire", {
              body: { scene, asset: en.script, parent: b.path }, quiet: true });
            if (!s.ok) failed.push(`${b.path} script: ${s.error}`);
          }
        }
        if (broke) continue;
        path = made.get("");
      } else {
        const b = await born(op.src, null, op.name, op.parent, null);
        if (b.error){ failed.push(`${op.name}: ${b.error}`); continue; }
        path = b.path;
      }
      if (!path) continue;
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
    if (failed.length){
      say(`${failed.length} change(s) failed — ${failed[0]}`);
      return;                    // do not export a file the write did not land in
    }
    say(`${n} change${n === 1 ? "" : "s"} written`, "ok");

    // THE LOOP. The file is what changed; the build is what you play. Rebuild
    // only when the panel is open — a minute of Godot for someone who is not
    // looking at the game is a minute of the tool being unusable — but always
    // refresh the chip, so a closed panel still says `stale` rather than
    // quietly carrying the previous answer forward.
    if (playOpen()) await rebuild();
    else await refreshBuild();
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

  function restoreValue(it, key, prev){
    const vec = /Vector2\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)/.exec(String(prev));
    if (key === "position" && vec){
      const [wx, wy] = toWorldTransform(it, +vec[1], +vec[2]);
      // Descendants carry the world offset too — putting only the node back
      // leaves its children standing where the undone drag left them.
      moveBy(it, wx - it.x, wy - it.y);
    } else if (key === "scale" && vec){
      it.sx = +vec[1]; it.sy = +vec[2];
    } else if (key === "rotation"){
      it.rot = +prev || 0;
    } else if (key === "z_index"){
      it.z = +prev || 0;
    } else if (key === "visible"){
      it.visible = String(prev) !== "false";
    }
  }

  /* The scene-tree eye. It is the node's real `visible`, not a view filter —
     the layer strip already owns the view-only version — so it stages like
     every other property and lands with the same one confirmation. */
  function stageVisible(path, value){
    const it = list && list.items.find(i => i.path === path);
    if (!it) return;
    const was = it.visible === false ? "false" : "true";
    it.visible = !!value;
    stage(it, "visible", value ? "true" : "false", was);
  }

  /* Show or hide the whole selection FOR REAL — staged, one line in the
     pending bar, written by the same single confirmation as everything else.
     Same refuse-whole rule as delete: a member this cannot apply to blocks
     the batch and gets named, rather than the other nine going through and
     the operator counting what moved. */
  function setVisibleBatch(paths, want){
    if (!list) return;
    const wanted = (paths || []).filter(Boolean);
    if (!wanted.length){ say("select something first"); return; }
    const missing = wanted.filter(p => !list.items.some(i => i.path === p));
    const inside = wanted.filter(p => {
      const it = list.items.find(i => i.path === p);
      return it && it.of;
    });
    if (missing.length || inside.length){
      const bad = [...missing, ...inside];
      say(`${bad.length} of ${wanted.length} selected node(s) cannot take a `
          + `visibility change (${bad.slice(0, 3).join(", ")}${
            bad.length > 3 ? "…" : ""}) — ${inside.length
              ? "they live inside an instanced scene, so there is no line in "
                + "this file to set" : "the viewport does not have them"
            }. Nothing was staged.`);
      return;
    }
    let n = 0;
    wanted.forEach(p => {
      const it = list.items.find(i => i.path === p);
      if (!it || (it.visible !== false) === !!want) return;   // already there
      stageVisible(p, want);
      n++;
    });
    paintLayers();
    say(n ? `${n} node(s) ${want ? "shown" : "hidden"} — staged`
          : `already ${want ? "visible" : "hidden"}`, "ok");
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
    // The tie-break is `paint`, NOT `order`: on a y-sorted scene the engine
    // draws siblings by depth, not by their position in the file, and the
    // server has already resolved that into `paint` (scenedraw.paint_order).
    // Re-sorting on `order` here would quietly un-sort the room the moment
    // anyone nudged a z_index.
    list.items.sort((a, b) => (a.z - b.z) || (a.paint - b.paint));
    paint();
  }

  /* ── selection ────────────────────────────────────────────────────────────
   * ONE authority, two surfaces. A range-select means "everything between
   * these two in the tree", and the tree's order is SceneBuild's — so a click
   * here asks it what the click means and takes back an answer, rather than
   * each surface keeping its own idea of what is selected and echoing.
   */
  function select(path, mode){
    if (window.SceneBuild && typeof SceneBuild.pick === "function"){
      SceneBuild.pick(path, mode || "set");
      return;
    }
    setSelection(path ? [path] : [], path || null);
  }

  /* State only — this never calls back, so it is safe for SceneBuild to drive
     it from inside its own selection resolution. */
  function setSelection(paths, primary){
    multi = new Set((paths || []).filter(Boolean));
    if (primary) multi.add(primary);
    const head = primary || (multi.size ? [...multi][multi.size - 1] : null);
    sel = head && list ? list.items.find(i => i.path === head) || null : null;
    if (sel) selStaged = -1;          // one selection, real or staged
    paint();
  }

  function selectedItems(){
    if (!list) return [];
    return [...multi].map(p => list.items.find(i => i.path === p)).filter(Boolean);
  }

  /* Which of the selected nodes a transform should actually be applied to.
     A node whose ancestor is also selected is carried by that ancestor — apply
     the delta to both and it moves twice, which is the classic multi-select
     bug and looks like the snap is broken. */
  function groupFor(head){
    const items = selectedItems();
    if (items.length < 2) return head ? [head] : [];
    const paths = new Set(items.map(i => i.path));
    if (paths.has(".")) return items.filter(i => i.path === ".");
    return items.filter(i => {
      let p = i.path;
      while (p.includes("/")){
        p = p.slice(0, p.lastIndexOf("/"));
        if (paths.has(p)) return false;
      }
      return true;
    });
  }

  /* A secondary selection: outlined, named, no handles. Handles belong to the
     primary alone — eight grips on twenty nodes is a canvas of grips. */
  function mark(it){
    const pts = corners(it).map(p => toScreen(p[0], p[1]));
    ctx.save();
    ctx.strokeStyle = BGTheme.color("--accent");
    ctx.globalAlpha = .7;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    ctx.closePath(); ctx.stroke();
    ctx.restore();
  }

  /* The CONTENT union the game frame, in world units. An isometric level
     spills well outside the 640x360 rectangle — fitting the rectangle alone
     put most of the floor off screen and looked like half the tiles were
     missing. Shared by `fit` and by the opening view, so the two cannot drift
     into framing different rectangles. */
  function contentBounds(){
    const v = (list && list.viewport) || [640, 360];
    let x0 = 0, y0 = 0, x1 = v[0], y1 = v[1];
    if (list) list.items.filter(drawable).forEach(it => {
      // A LIGHT'S HALO IS NOT CONTENT. Its box is the cookie's own size times
      // texture_scale — 400px of falloff around a 30px fitting — and forty of
      // them dragged floor_tut's bounds out to 1883x1746 around a 2261x1184
      // level that does not go there. Framing on that put the centre in a room
      // the plate does not have and zoomed `fit` out to make room for a glow.
      if (it.draw && it.draw.kind === "light") return;
      corners(it).forEach(([px, py]) => {
        x0 = Math.min(x0, px); y0 = Math.min(y0, py);
        x1 = Math.max(x1, px); y1 = Math.max(y1, py);
      });
    });
    return { x0, y0, w: Math.max(1, x1 - x0), h: Math.max(1, y1 - y0) };
  }

  function fit(){
    if (!list || !host) return;
    const r = host.querySelector("#sv-stage").getBoundingClientRect();
    const b = contentBounds();
    view.z = clamp(Math.min((r.width - 60) / b.w, (r.height - 60) / b.h), 0.05, 4);
    view.x = (r.width - b.w * view.z) / 2 - b.x0 * view.z;
    view.y = (r.height - b.h * view.z) / 2 - b.y0 * view.z;
    paint();
  }

  /* ── the opening view ──────────────────────────────────────────────────────
   *
   * THE PANEL OPENS AT THE GAME'S OWN SCALE, not at fit.
   *
   * This viewport exists so someone can decide whether a scene LOOKS right, and
   * that decision cannot be made at an arbitrary zoom: "is this prop too big",
   * "does this light read", "is this sprite muddy" all have different answers at
   * 29% than they do at the size the player sees. Godot opens a 2D scene at 100%
   * for exactly this reason. Opening at fit meant every comparison against the
   * running game started with a rescale nobody had asked for and the panel never
   * mentioned — which is how "props are not scaled right in Atlas" survived
   * three rounds of being told the geometry was correct. It was; the zoom was
   * not the game's.
   *
   * The cost is real and is mitigated rather than dodged: at x2 a 2368x1184
   * plate does not fit in a panel, so the opening view is CENTRED ON THE
   * CONTENT, not parked in a corner, `fit` is one click away in the toolbar,
   * and the view is remembered per scene so coming back lands where you left.
   */
  const VIEW_KEY = "bgate-sceneview-view";

  function viewStore(){
    try { return JSON.parse(localStorage.getItem(VIEW_KEY) || "{}") || {}; }
    catch (e){ return {}; }
  }

  /* Debounced, because it is called from the paint loop — a drag is sixty
     view changes a second and localStorage is synchronous. */
  function rememberView(){
    if (!scene) return;
    clearTimeout(rememberView._t);
    rememberView._t = setTimeout(() => {
      if (!scene) return;
      try {
        const all = viewStore();
        all[scene] = { x: view.x, y: view.y, z: view.z };
        // Bounded: one entry per scene ever visited would grow without limit.
        const keys = Object.keys(all);
        if (keys.length > 40) delete all[keys[0]];
        localStorage.setItem(VIEW_KEY, JSON.stringify(all));
      } catch (e){}
    }, 400);
  }

  function restoreView(){
    const saved = viewStore()[scene];
    if (!saved) return false;
    const { x, y, z } = saved;
    if (![x, y, z].every(n => typeof n === "number" && isFinite(n))) return false;
    if (z <= 0.04 || z > 12) return false;
    view = { x, y, z };
    return true;
  }

  /* Game scale, centred on the content — the state the panel opens in when it
     has no remembered view for this scene. Returns whether it settled one, so
     the paint loop can try again on the frame the panel finally has a size:
     the Atlas surface mounts this while its section is still `display:none`,
     and a view computed against a 0x0 stage is a view of nothing. (fit() used
     to do that silently and clamp the zoom to its 5% floor.) */
  let viewReady = false;

  function openingView(){
    if (!host || !list) return false;
    const r = host.querySelector("#sv-stage").getBoundingClientRect();
    if (!r.width || !r.height) return false;
    viewReady = true;
    if (!restoreView()){
      const b = contentBounds();
      view.z = clamp(gameScaleOf(), 0.05, 12);
      view.x = r.width / 2 - (b.x0 + b.w / 2) * view.z;
      view.y = r.height / 2 - (b.y0 + b.h / 2) * view.z;
    }
    paint();
    return true;
  }
  /* The factor between "one world pixel" and "one pixel the player sees". 1 on
     a project that does not stretch; 2 on this one. */
  function gameScaleOf(){
    const s = list && Number(list.scale);
    return s && isFinite(s) && s > 0 ? s : 1;
  }

  /* Put the view at the game's own scale, about the centre. This is the
     control for "make Atlas look like what I am comparing it to" — on a
     stretched project the editor's 1:1 and the game's 1:1 are different
     numbers, and until now the panel only offered one of them. */
  function gameScale(){
    if (!host) return;
    const r = host.querySelector("#sv-stage").getBoundingClientRect();
    const [bx, by] = toWorld(r.width / 2, r.height / 2);
    view.z = clamp(gameScaleOf(), 0.05, 12);
    view.x = r.width / 2 - bx * view.z;
    view.y = r.height / 2 - by * view.z;
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
    b.innerHTML = I("real_preview")
      + `<span>${real.busy ? "running…" : "real"}</span>`;
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

  /* ── the playable build, beside the scene ──────────────────────────────────
   *
   * The viewport draws what the FILE says. That is the right picture to drag
   * against and it is not proof of anything: a scene can look correct and play
   * wrong, and until now the only way to find that out was to leave, open the
   * play tab, and remember to rebuild first. Almost nobody remembered, so what
   * got checked was yesterday's build — which is worse than not checking,
   * because it comes back green.
   *
   * So the loop closes here. Applying an edit writes the file, exports the
   * build, and reloads the frame, in that order, without leaving the panel.
   *
   * THE FRAME IS BLANKED ON EVERY EXIT PATH. A running WASM build left in a
   * hidden panel keeps a game loop and an audio context alive behind whatever
   * you switch to; closing the panel and leaving the panel are two different
   * exits and both have to do it. Switching SCENES deliberately does not — the
   * build is the whole game, not the file being looked at, and killing the
   * running game because someone opened a different scene beside it would be a
   * bug wearing a tidiness argument. */
  function playPanel(){ return document.getElementById("sv-play"); }
  function playOpen(){ const p = playPanel(); return !!p && p.classList.contains("open"); }

  function blankFrame(){
    const f = document.getElementById("sv-frame");
    if (f) f.src = "about:blank";
  }

  /* Hidden, not torn down. The panel is only being navigated AWAY from — the
     staged edits are still the operator's and must survive coming back — but
     the build behind it must stop, because a hidden iframe is still a running
     game with an audio context. Deliberately not unmount(): that asks about
     losing staged work, and hiding a tab is not a reason to ask. */
  function suspend(){ blankFrame(); }

  function reloadFrame(){
    const f = document.getElementById("sv-frame");
    if (!f) return;
    // /play answers with JSON when nothing has been exported, and an iframe
    // renders that as a wall of error text where the game goes. The header
    // already says the build is missing; do not say it twice and worse.
    if (play && play.built === false){ f.src = "about:blank"; return; }
    // Cache-bust: the wasm and the pck keep their filenames across exports, so
    // a plain reload replays the build that was just replaced.
    f.src = `/play/index.html?t=${Date.now()}`;
  }

  async function refreshBuild(){
    play = await readJSON("/api/play/status", null);
    const el = document.getElementById("sv-build");
    if (!el) return;
    if (!play || play.__error){ el.textContent = "build · unknown"; return; }
    if (!play.built){
      el.innerHTML = `build · <span class="sv-stale">${E(play.reason || "none")}</span>`;
      el.title = "";
      return;
    }
    el.innerHTML = play.stale ? `build · <span class="sv-stale">stale</span>`
                              : "build · current";
    // WHICH file made it stale. "stale" on its own invites the assumption that
    // the check is just pessimistic, and that assumption is how someone plays
    // the old build anyway.
    el.title = play.stale ? (play.reason || "the source is newer than the build")
                          : "the build matches the source";
  }

  async function rebuild(){
    say("exporting the web build — Godot takes a minute on a cold project…");
    const r = await mutate("/api/play/rebuild", { body: {}, quiet: true,
                                                  button: "sv-rebuild" });
    const d = r.data || {};
    if (!r.ok || d.ok === false){
      say(d.error || d.detail || r.error || "the export failed");
      await refreshBuild();
      return false;
    }
    await refreshBuild();
    reloadFrame();
    say("build refreshed", "ok");
    return true;
  }

  async function togglePlay(){
    const p = playPanel();
    if (!p) return;
    const on = p.classList.toggle("open");
    const t = document.getElementById("sv-play-t");
    if (t) t.classList.toggle("on", on);
    if (!on){ blankFrame(); return; }
    await refreshBuild();
    // An open panel showing a stale build is the exact trap this feature
    // exists to close, so offer the rebuild rather than quietly serving it.
    if (play && play.built && play.stale
        && await askConfirm({
             title: "The build is older than the source.",
             body: [play.reason || "Something in the game project changed since "
                    + "the last export.", "Playing it now shows the old game."],
             ok: "rebuild first" })){
      await rebuild();
    } else {
      reloadFrame();
    }
  }

  /* The lock, on the scene the builder is pointed at. The write refuses a held
     file (423), and finding that out at `apply` is finding it out after the
     work. Read-only until then: a locked scene can still be looked at. */
  function paintLock(){
    const el = document.getElementById("sv-lock");
    if (!el) return;
    el.hidden = !held;
    if (held) el.textContent =
      `locked by the ${held.seat} seat — edits here will be refused until it releases`;
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
    // A new scene gets its OWN opening view — its remembered one, or the game's
    // scale about its content. Carrying the previous scene's pan over is how a
    // switch used to land on empty space beside the new level.
    viewReady = false;
    const done = reload();
    done.then(() => openingView());
    return done;
  }

  return { mount, unmount, reload, fit, frame, gameScale, zoom: zoomBy, toggle, setSnap,
           snapshot, select, setSelection, raise, undo, redo, setScene, nudge,
           apply: applyPending, discard: discardPending, hasPending, escape,
           placeMenu, arm, cancelPlacing, removeSelected, duplicateSelected,
           pasteClones, stageVisible, setVisibleBatch,
           togglePlay, rebuild, suspend,
           toggleLayer, layerClick, layerEye, isolateLayer, showAllLayers,
           repaintLayers: paintLayers, nextBlank,
           realView, reshoot,
           get layers(){ return layers(); },
           get list(){ return list; }, get selected(){ return sel; },
           get selection(){ return [...multi]; },
           get scene(){ return scene; },
           get canUndo(){ return history.length; },
           get canRedo(){ return redoStack.length; },
           get pending(){ return pendingCount(); } };
})();
