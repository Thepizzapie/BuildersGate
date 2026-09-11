/* sceneview3d.js: the 3D scene as it actually looks, and editable there.
 *
 * sceneview.js composites a 2D scene onto a canvas. A 3D scene has no paint
 * order and no canvas: it has a tree of Node3Ds, each carrying a Transform3D,
 * a handful of ways to hang a mesh on one, and a camera. So this is the same
 * idea one dimension up: /api/scene/render answers with world matrices and
 * shapes (a box of this size, this .glb, this collision capsule, this light),
 * and this builds them in three.js, lets you orbit, click to select, and drag
 * a gizmo to move, rotate or scale.
 *
 * SAME CONTRACT AS THE 2D VIEWPORT, and it is the contract that matters:
 *   · nothing writes until you say so. A drag changes the picture; edits stage
 *     as pending property writes, a bar counts them, `apply` is the only thing
 *     that touches disk (one confirmation, one backup per file);
 *   · the file's own spelling is kept. A node the editor saved carries
 *     `transform =`; one an agent wrote carries position / rotation / scale.
 *     A staged move writes back in the spelling the node already has, so the
 *     diff is one line and Godot never sees both;
 *   · the insides of an instanced scene are shown but not edited. Godot
 *     selects the instance, and so does this.
 *
 * WHAT IT IS NOT: play mode. Nothing here has run _ready(). A MeshInstance3D
 * whose mesh a script assigns at load draws as a marker with a reason, the
 * same way the 2D viewport draws a script-filled container.
 *
 * An ES module because three.js only ships as one; sceneview.js delegates to
 * window.SceneView3D when /api/scene/render says `dimension: "3d"`.
 */
import * as THREE from "three";
import { OrbitControls } from "/static/vendor/three/examples/jsm/controls/OrbitControls.js";
import { TransformControls } from "/static/vendor/three/examples/jsm/controls/TransformControls.js";
import { GLTFLoader } from "/static/vendor/three/examples/jsm/loaders/GLTFLoader.js";
import { OBJLoader } from "/static/vendor/three/examples/jsm/loaders/OBJLoader.js";

window.SceneView3D = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const I = (name, size) => window.BGIcon ? BGIcon(name, { size: size || 16 }) : "";
  const confirmAsk = opts => window.askConfirm ? askConfirm(opts)
    : Promise.resolve(confirm(opts.body || opts.title || ""));

  const ROLE_TINT = {
    character:"--warn", enemy:"--bad", prop:"--warn-line", item:"--warn",
    layer:"--accent", visual:"--text", collision:"--text-3", light:"--warn",
    controller:"--c-narrative", camera:"--text", audio:"--c-narrative",
    fx:"--c-narrative", ui:"--good", marker:"--text-3", instance:"--text",
    node:"--text-3",
  };
  const cssColor = name => {
    try { return new THREE.Color(BGTheme.color(name)); } catch (e) { return new THREE.Color(0x9aa0a6); }
  };

  /* ── state ─────────────────────────────────────────────────────────────── */
  let host = null, scene = null, list = null, held = null;
  let renderer = null, world = null, camera = null, orbit = null, gizmo = null;
  let raf = 0, ro = null;
  const objects = new Map();      // path -> THREE.Object3D (the node's frame)
  const meshes = [];              // pickable meshes, each with userData.path
  let sel = null;                 // primary selected path
  const multi = new Set();
  let outline = null;             // BoxHelper on the selection
  let pending = new Map();        // path -> {position?, rotation?, scale?, transform?}
  let undoStack = [], redoStack = [];
  let busy = false;
  const opts = {
    grid: true, wire: true, showHidden: false, lights: true, gameCam: false,
    mode: "translate", snap: false,
    // "demand": a frame only when something changed (the default; the GPU
    // idles while you read the inspector). "live": sixty a second, for
    // anything that animates. Stopped is a state, not an option: see stop().
    render: "demand",
  };
  try {
    const saved = JSON.parse(localStorage.getItem("bgate-sceneview3d") || "{}");
    Object.assign(opts, saved);
  } catch (e) {}
  const persist = () => { try { localStorage.setItem("bgate-sceneview3d", JSON.stringify(opts)); } catch (e) {} };

  /* ── styles ────────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("sv3-style")) return;
    const s = document.createElement("style");
    s.id = "sv3-style";
    s.textContent = [
      ".sv3{display:flex;flex-direction:column;height:min(78vh,900px);min-height:480px;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--surface-1)}",
      ".sv3-bar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:7px 9px;border-bottom:1px solid var(--line-soft)}",
      ".sv3-b{display:inline-flex;align-items:center;gap:4px;padding:4px 8px;background:var(--surface-3);border:1px solid var(--line);border-radius:7px;color:var(--text);font:inherit;font-size:11px;cursor:pointer}",
      ".sv3-b:hover{border-color:var(--accent)}",
      ".sv3-b.on{border-color:var(--accent);color:var(--accent)}",
      ".sv3-b.go{background:var(--accent);color:var(--accent-fg);border-color:var(--accent);font-weight:600}",
      ".sv3-l{font-size:10.5px;color:var(--text-3);margin-left:4px}",
      ".sv3-stage{position:relative;flex:1;min-height:0;background:var(--bg)}",
      ".sv3-stage canvas{display:block;width:100%;height:100%;outline:none}",
      ".sv3-hud{position:absolute;left:10px;bottom:8px;max-width:62%;font-family:var(--mono);font-size:10px;color:var(--text-3);pointer-events:none;white-space:pre-wrap}",
      ".sv3-tip{position:absolute;right:10px;bottom:8px;max-width:34%;text-align:right;font-size:10px;color:var(--text-3);pointer-events:none}",
      ".sv3-top{position:absolute;left:10px;right:10px;top:8px;display:flex;flex-direction:column;gap:6px;pointer-events:none}",
      ".sv3-top>*{pointer-events:auto}",
      ".sv3-pending{display:flex;gap:8px;align-items:center;padding:6px 10px;border:1px solid var(--warn);border-radius:9px;background:var(--surface-2);font-size:11.5px}",
      ".sv3-pending .dot{width:8px;height:8px;border-radius:50%;background:var(--warn)}",
      ".sv3-lock{padding:6px 10px;border:1px solid var(--bad);border-radius:9px;background:var(--surface-2);font-size:11.5px}",
      ".sv3-hover{position:absolute;padding:3px 7px;border-radius:6px;background:var(--surface-2);border:1px solid var(--line);font-size:10.5px;pointer-events:none;transform:translate(10px,10px)}",
      ".sv3-stopped{position:absolute;inset:0;display:flex;flex-direction:column;gap:10px;align-items:center;justify-content:center;background:var(--bg);color:var(--text-3);font-size:12px}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── mount / unmount ───────────────────────────────────────────────────── */
  async function mount(el, sceneId, prefetched){
    // A previous mount's viewport, torn down by the builder re-rendering
    // its panel rather than by unmount(): release the GPU before building
    // a second renderer, and start the new one from "not yet framed".
    if (renderer) teardown();
    fitted = false; stopped = false;
    injectStyle();
    host = el; scene = sceneId;
    host.innerHTML = `
      <div class="sv3">
        <div class="sv3-bar">
          <button class="sv3-b" onclick="SceneView3D.fit()" title="Fit the whole scene (F frames the selection)">${I("fit")}<span>fit</span></button>
          <span class="sv3-l">gizmo</span>
          <button class="sv3-b ${opts.mode==='translate'?'on':''}" id="sv3-m-t" onclick="SceneView3D.setMode('translate')" title="Move (W)">move</button>
          <button class="sv3-b ${opts.mode==='rotate'?'on':''}" id="sv3-m-r" onclick="SceneView3D.setMode('rotate')" title="Rotate (E)">rotate</button>
          <button class="sv3-b ${opts.mode==='scale'?'on':''}" id="sv3-m-s" onclick="SceneView3D.setMode('scale')" title="Scale (R)">scale</button>
          <button class="sv3-b ${opts.snap?'on':''}" id="sv3-snap" onclick="SceneView3D.toggle('snap')" title="Snap moves to 0.5 units, rotations to 15 degrees">snap</button>
          <span class="sv3-l">show</span>
          <button class="sv3-b ${opts.grid?'on':''}" id="sv3-grid" onclick="SceneView3D.toggle('grid')" title="Ground grid, one unit per cell">${I("snap_grid")}</button>
          <button class="sv3-b ${opts.wire?'on':''}" id="sv3-wire" onclick="SceneView3D.toggle('wire')" title="Collision shapes, as wireframes">${I("collision")}</button>
          <button class="sv3-b ${opts.showHidden?'on':''}" id="sv3-hidden" onclick="SceneView3D.toggle('showHidden')" title="Nodes marked invisible">${I("hidden")}</button>
          <button class="sv3-b ${opts.lights?'on':''}" id="sv3-lights" onclick="SceneView3D.toggle('lights')" title="The scene's own lights. Off is a flat, structural read.">${I("lighting")}</button>
          <button class="sv3-b ${opts.gameCam?'on':''}" id="sv3-cam" onclick="SceneView3D.toggle('gameCam')" title="Look through the scene's Camera3D">${I("camera")}<span>game cam</span></button>
          <span style="flex:1 1 auto;min-width:8px"></span>
          <button class="sv3-b ${opts.render==='live'?'on':''}" id="sv3-live" onclick="SceneView3D.toggle('render')"
                  title="Render continuously (live) or only when something changes (on demand, the default). On demand leaves the GPU idle between edits.">${I("run")}<span id="sv3-live-l">${opts.render === "live" ? "live" : "on demand"}</span></button>
          <button class="sv3-b" id="sv3-stop" onclick="SceneView3D.stop()"
                  title="Stop rendering and release the GPU. The scene stays loaded server-side; start rebuilds it from cache.">${I("close")}<span>stop</span></button>
          <button class="sv3-b" id="sv3-undo" onclick="SceneView3D.undo()" title="Undo the last staged change (Ctrl+Z)">${I("undo")}</button>
          <button class="sv3-b" id="sv3-redo" onclick="SceneView3D.redo()" title="Redo (Ctrl+Shift+Z)">${I("redo")}</button>
          <button class="sv3-b" onclick="SceneView3D.snapshot()" title="Save this view as a PNG under .bgate_out/scene_shots">${I("export_image")}</button>
          <button class="sv3-b" onclick="SceneView3D.reshoot()" title="Run the game and take a real screenshot through the engine (godot_screenshot)">${I("real_preview")}<span>real</span></button>
        </div>
        <div class="sv3-stage" id="sv3-stage">
          <div class="sv3-top">
            <div class="sv3-lock" id="sv3-lock" hidden></div>
            <div class="sv3-pending" id="sv3-pending" hidden>
              <span class="dot"></span><span id="sv3-pending-n"></span>
              <span style="flex:1"></span>
              <button class="sv3-b" onclick="SceneView3D.discard()">discard</button>
              <button class="sv3-b go" onclick="SceneView3D.apply()">apply to the file…</button>
            </div>
          </div>
          <div class="sv3-hud" id="sv3-hud"></div>
          <div class="sv3-tip">drag = orbit · right drag = pan · wheel = zoom · click = select · W/E/R = gizmo</div>
          <div class="sv3-hover" id="sv3-hover" hidden></div>
          <div class="sv3-stopped" id="sv3-stopped" hidden>
            <div>rendering stopped</div>
            <button class="sv3-b go" onclick="SceneView3D.start()">${I("run")}<span>start</span></button>
          </div>
        </div>
      </div>`;
    initThree();
    await reload(prefetched);
    return true;
  }

  async function unmount(){
    if (hasPending() && !(await confirmAsk({
      title: `${pendingCount()} change(s) have not been written. Leave and lose them?`,
      body: "Nothing staged has touched the file yet - leaving drops the whole batch.",
      ok: "leave", danger: true,
    }))) return false;
    teardown();
    return true;
  }

  /* ONE WebGL CONTEXT FOR THE LIFE OF THE PAGE. The builder re-renders its
     panel in bursts (twenty-five activations on one boot were measured) and
     each used to construct a fresh WebGLRenderer; dispose() does not give
     the context back, the browser caps live contexts around sixteen, and
     the twenty-sixth mount got "context could not be created" and a black
     pane. The renderer, its canvas and the controls are made once and
     re-homed into whichever host is current. */
  let keep = null;        // {renderer, camera, orbit, gizmo}

  function teardown(){
    cancelAnimationFrame(raf); raf = 0; dirty = false;
    if (ro){ ro.disconnect(); ro = null; }
    if (gizmo) gizmo.detach();
    if (renderer && renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
    window.removeEventListener("keydown", onKey);
    objects.clear(); meshes.length = 0; meshCache.clear();
    pending = new Map(); undoStack = []; redoStack = [];
    if (world) clearWorld();
    host = null; list = null; sel = null; multi.clear();
  }

  /* ── three.js setup ────────────────────────────────────────────────────── */
  function initThree(){
    const stage = host.querySelector("#sv3-stage");
    if (!keep){
      const r = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
      r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      r.outputColorSpace = THREE.SRGBColorSpace;
      const cam = new THREE.PerspectiveCamera(55, 1, 0.05, 5000);
      cam.position.set(8, 6, 10);
      const orb = new OrbitControls(cam, r.domElement);
      orb.enableDamping = true; orb.dampingFactor = 0.12;
      const giz = new TransformControls(cam, r.domElement);
      giz.size = 0.75;
      giz.addEventListener("dragging-changed", e => { orb.enabled = !e.value; if (!e.value) onGizmoEnd(); });
      giz.addEventListener("mouseDown", () => { gizmoStart = snapshotOf(giz.object); });
      // Anything that moves the picture asks for a frame; the loop draws
      // one only when asked (or every frame in live mode). OrbitControls
      // keeps emitting "change" while its damping settles, so a fling still
      // eases to a stop.
      // A frame drawn while the context is lost draws nothing; ask again
      // once the browser hands the context back.
      r.domElement.addEventListener("webglcontextrestored", () => setTimeout(requestRender, 0));
      orb.addEventListener("change", requestRender);
      giz.addEventListener("change", requestRender);
      giz.addEventListener("objectChange", requestRender);
      r.domElement.addEventListener("pointerdown", onDown);
      r.domElement.addEventListener("pointerup", onUp);
      r.domElement.addEventListener("pointermove", onMove);
      keep = { renderer: r, camera: cam, orbit: orb, gizmo: giz, world: new THREE.Scene() };
    }
    ({ renderer, camera, orbit, gizmo, world } = keep);
    stage.prepend(renderer.domElement);
    world.background = cssColor("--bg");
    gizmo.setMode(opts.mode);
    // TransformControls is a Controls in r160+ (getHelper) and an Object3D
    // before that; add whichever this build has.
    const helper = typeof gizmo.getHelper === "function" ? gizmo.getHelper() : gizmo;
    if (!helper.parent) world.add(helper);
    applySnap();

    const resize = () => {
      const w = stage.clientWidth || 640, h = stage.clientHeight || 400;
      renderer.setSize(w, h, false);
      camera.aspect = w / h; camera.updateProjectionMatrix();
      requestRender();
    };
    ro = new ResizeObserver(resize); ro.observe(stage); resize();
    window.addEventListener("keydown", onKey);
    const tick = () => {
      raf = requestAnimationFrame(tick);
      if (stopped) return;
      orbit.update();
      if (!dirty && opts.render !== "live") return;
      let lost = false;
      try { lost = renderer.getContext().isContextLost(); } catch (e) {}
      if (lost) return;                      // stay dirty until it is back
      dirty = false;
      const key = world.getObjectByName("__key");
      if (key){ key.position.copy(camera.position); key.target.position.copy(orbit.target); }
      renderer.render(world, camera);
    };
    tick();
  }

  let dirty = true, stopped = false;
  function requestRender(){ dirty = true; }

  /* STOP releases the GPU: the loop idles, the objects are dropped, and the
     context is lost on purpose so the driver frees its memory; START builds
     the scene again from the server's cached draw list. This is what "I am
     done looking" means on a laptop, and what a tab you have forgotten
     about should cost: nothing. */
  function stop(){
    if (stopped || !host) return;
    stopped = true;
    if (gizmo) gizmo.detach();
    if (outline){ world.remove(outline); outline = null; }
    clearWorld();
    try { renderer.forceContextLoss(); } catch (e) {}
    const veil = host.querySelector("#sv3-stopped"); if (veil) veil.hidden = false;
    paintHud();
  }
  async function start(){
    if (!stopped || !host) return;
    stopped = false;
    const veil = host.querySelector("#sv3-stopped"); if (veil) veil.hidden = true;
    // A lost context is restored by three on the next render; the scene
    // objects were dropped, so rebuild them from the list already in hand.
    try { renderer.forceContextRestore(); } catch (e) {}
    if (list){ build(); setSelection([...multi], sel); }
    else await reload();
    requestRender();
  }
  // Hidden tab: no frames until it is visible again. Deck switch: the
  // builder's deactivate() calls suspend(), which does the same.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) requestRender(); });
  function suspend(){ dirty = false; }

  function applySnap(){
    if (!gizmo) return;
    gizmo.setTranslationSnap(opts.snap ? 0.5 : null);
    gizmo.setRotationSnap(opts.snap ? THREE.MathUtils.degToRad(15) : null);
    gizmo.setScaleSnap(opts.snap ? 0.25 : null);
  }

  /* ── loading ───────────────────────────────────────────────────────────── */
  async function reload(prefetched){
    if (!scene) return;
    pending = new Map(); undoStack = []; redoStack = [];
    held = null; paintLock(); paintPending();
    const d = prefetched || await readJSON(`/api/scene/render?scene=${encodeURIComponent(scene)}`, null);
    if (!d || d.__error){ say((d && d.__error) || "could not render that scene"); return; }
    if (d.dimension !== "3d"){ say("that scene is 2D; the 2D viewport draws it"); return; }
    list = d;
    held = d.lock || null;
    paintLock();
    build();
    const alive = p => d.items.some(i => i.path === p);
    setSelection([...multi].filter(alive), sel && alive(sel) ? sel : null);
    if (!fitted){ fit(); fitted = true; }
    paintHud();
  }
  let fitted = false;

  function clearWorld(){
    for (const o of [...world.children]){
      if (o === gizmo || (gizmo && typeof gizmo.getHelper === "function" && o === gizmo.getHelper())) continue;
      world.remove(o);
    }
    objects.clear(); meshes.length = 0; outline = null;
    if (gizmo) gizmo.detach();
  }

  const M = row => new THREE.Matrix4().set(
    row[0][0], row[0][1], row[0][2], row[0][3],
    row[1][0], row[1][1], row[1][2], row[1][3],
    row[2][0], row[2][1], row[2][2], row[2][3],
    row[3][0], row[3][1], row[3][2], row[3][3]);

  function build(){
    clearWorld();
    litCount = 0;
    requestRender();
    const grid = new THREE.GridHelper(40, 40, cssColor("--line"), cssColor("--line-soft"));
    grid.name = "__grid"; grid.visible = opts.grid; world.add(grid);
    const axes = new THREE.AxesHelper(1.5); axes.name = "__axes"; axes.visible = opts.grid; world.add(axes);

    // Always some light, or a scene that ships its own lighting through a
    // script (or none at all yet) reads as a black rectangle.
    // Physically-based intensities (three r155+): a hemisphere near 1 reads
    // as overcast daylight. With the scene's own lights on it drops to a
    // fill so the Sun's direction still shows; off, it IS the light.
    const fill = new THREE.HemisphereLight(0xffffff, 0x334455, opts.lights ? 0.9 : 1.8);
    fill.name = "__fill"; world.add(fill);
    // A key from over the viewer's shoulder, so a scene with no
    // DirectionalLight3D yet (most grayboxes) still has shading to read
    // depth from. Follows the camera in the render loop.
    const key = new THREE.DirectionalLight(0xffffff, opts.lights ? 0.6 : 1.2);
    key.name = "__key"; world.add(key); world.add(key.target);

    const worlds = new Map();
    for (const item of list.items){
      const wm = M(item.world);
      worlds.set(item.path, wm);
      const parentPath = parentOf(item.path);
      const parentObj = objects.get(parentPath) || world;
      const parentWorld = worlds.get(parentPath) || new THREE.Matrix4();
      const local = parentWorld.clone().invert().multiply(wm);
      const frame = new THREE.Group();
      frame.name = item.path;
      local.decompose(frame.position, frame.quaternion, frame.scale);
      frame.userData = { item };
      parentObj.add(frame);
      objects.set(item.path, frame);
      const drawn = drawableFor(item);
      if (drawn){
        drawn.traverse(o => { if (o.isMesh){ o.userData.path = item.path; meshes.push(o); } });
        frame.add(drawn);
      }
      const hidden = !item.visible && !opts.showHidden;
      frame.visible = !hidden;
      if (item.draw.kind === "light" && !opts.lights) frame.visible = false;
    }
    if (opts.gameCam) lookThroughGameCamera();
  }

  const parentOf = path => {
    if (path === ".") return null;
    const i = path.lastIndexOf("/");
    return i < 0 ? "." : path.slice(0, i);
  };

  /* ── what each node draws ──────────────────────────────────────────────── */
  const tintOf = item => cssColor(ROLE_TINT[item.role] || ROLE_TINT.node);

  function materialFor(item, d){
    const color = d.color ? new THREE.Color(d.color[0], d.color[1], d.color[2]) : tintOf(item);
    if (d.wire) return new THREE.MeshBasicMaterial({ color: cssColor("--good"), wireframe: true, transparent: true, opacity: 0.3 });
    // A mesh this cannot read, sized by its collider: a solid at the right
    // footprint, dimmed so it reads as "something is here" rather than as
    // the thing itself.
    if (d.placeholder) return new THREE.MeshStandardMaterial({ color, roughness: 1, metalness: 0, transparent: true, opacity: 0.45 });
    return new THREE.MeshStandardMaterial({ color, roughness: 0.85, metalness: 0.0,
      transparent: !!(d.color && d.color[3] < 1), opacity: d.color ? d.color[3] : 1 });
  }

  function drawableFor(item){
    const d = item.draw || {};
    if (d.wire && !opts.wire) return null;
    const mat = () => materialFor(item, d);
    let geo = null;
    switch (d.kind){
      case "box": geo = new THREE.BoxGeometry(d.size[0], d.size[1], d.size[2]); break;
      case "prism": geo = new THREE.CylinderGeometry(0, d.size[0] * 0.7, d.size[1], 4); break;
      case "sphere": {
        const g = new THREE.SphereGeometry(d.radius, 24, 16);
        if (d.height && Math.abs(d.height - d.radius * 2) > 1e-6) g.scale(1, d.height / (d.radius * 2), 1);
        geo = g; break;
      }
      case "cylinder": geo = new THREE.CylinderGeometry(d.top_radius, d.bottom_radius, d.height, 24); break;
      case "capsule": geo = new THREE.CapsuleGeometry(d.radius, Math.max(0, d.height - d.radius * 2), 6, 16); break;
      case "torus": geo = new THREE.TorusGeometry((d.inner_radius + d.outer_radius) / 2, (d.outer_radius - d.inner_radius) / 2, 12, 32); break;
      case "plane": {
        const g = new THREE.PlaneGeometry(d.size[0], d.size[1]);
        // Godot PlaneMesh faces +Y by default (orientation 1); a QuadMesh faces +Z.
        if (d.orientation === 1) g.rotateX(-Math.PI / 2);
        else if (d.orientation === 0) g.rotateY(Math.PI / 2);
        const m = new THREE.Mesh(g, mat()); m.material.side = THREE.DoubleSide; return m;
      }
      case "mesh_unknown": {
        const g = new THREE.BoxGeometry(1, 1, 1);
        const m = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: tintOf(item), wireframe: true, transparent: true, opacity: 0.5 }));
        m.userData.reason = d.reason; return m;
      }
      case "model": return modelFor(item, d);
      case "arraymesh": return arrayMeshFor(item, d);
      case "camera": {
        const g = new THREE.Group();
        const cone = new THREE.Mesh(new THREE.ConeGeometry(0.25, 0.6, 4), new THREE.MeshBasicMaterial({ color: tintOf(item), wireframe: true }));
        cone.rotation.x = Math.PI / 2; cone.position.z = -0.3;   // Godot cameras look down -Z
        g.add(cone);
        const box = new THREE.Mesh(new THREE.BoxGeometry(0.35, 0.25, 0.35), new THREE.MeshBasicMaterial({ color: tintOf(item), wireframe: true }));
        g.add(box); return g;
      }
      case "light": return lightFor(item, d);
      case "sprite": {
        const size = 1;
        const m = new THREE.Mesh(new THREE.PlaneGeometry(size, size), new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide, transparent: true }));
        if (d.rel) new THREE.TextureLoader().load(d.rel, tex => {
          tex.colorSpace = THREE.SRGBColorSpace;
          const w = tex.image.width * (d.pixel_size || 0.01), h = tex.image.height * (d.pixel_size || 0.01);
          m.geometry.dispose(); m.geometry = new THREE.PlaneGeometry(w, h);
          m.material.map = tex; m.material.needsUpdate = true; requestRender();
        });
        return m;
      }
      case "label": case "marker": case "group": case "environment": case "none": {
        if (d.kind === "group" && d.children > 0 && item.path !== ".") return null;
        if (d.kind === "environment" || d.kind === "none") return null;
        const m = new THREE.Mesh(new THREE.OctahedronGeometry(0.12), new THREE.MeshBasicMaterial({ color: tintOf(item), wireframe: true }));
        return m;
      }
      default: return null;
    }
    const m = new THREE.Mesh(geo, mat());
    if (!d.wire){ m.castShadow = false; m.receiveShadow = false; }
    return m;
  }

  /* ONE FETCH PER MODEL FILE. A street of 26 parked cars is seven .glb files;
     loading each instance separately pulled 60 MB for a scene whose models
     total 12. The loaded scene graph is cloned per instance (geometry and
     materials are shared by clone(), which is what makes this cheap). */
  const modelCache = new Map();     // url -> Promise<Object3D>
  function loadModel(url){
    if (!modelCache.has(url)){
      const p = new Promise((resolve, reject) => {
        if (/\.(glb|gltf)(\?|$)/i.test(url)) new GLTFLoader().load(url, g => resolve(g.scene), undefined, reject);
        else if (/\.obj(\?|$)/i.test(url)) new OBJLoader().load(url, resolve, undefined, reject);
        else reject(new Error("unsupported model"));
      });
      modelCache.set(url, p);
    }
    return modelCache.get(url);
  }

  /* AN ARRAYMESH IS THE FILE'S OWN GEOMETRY. The server decodes it from the
     .tscn (/api/scene/mesh, binary) and this builds a BufferGeometry; the
     AABB draws as a dim box until the bytes land. One fetch per mesh id per
     scene, shared by every node that uses the mesh. */
  const meshCache = new Map();      // scene|id -> Promise<BufferGeometry>
  function loadArrayMesh(id, inScene){
    const from = inScene || scene;
    const key = `${from}|${id}`;
    if (!meshCache.has(key)){
      const url = `/api/scene/mesh?scene=${encodeURIComponent(from)}&id=${encodeURIComponent(id)}`;
      const p = fetch(url).then(r => { if (!r.ok) throw new Error(`mesh ${id}: ${r.status}`); return r.arrayBuffer(); })
        .then(buf => {
          const dv = new DataView(buf);
          if (String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3)) !== "BGM1") throw new Error("not a mesh");
          const nv = dv.getUint32(4, true), ni = dv.getUint32(8, true);
          const pos = new Float32Array(buf, 12, nv * 3);
          const idx = new Uint32Array(buf, 12 + nv * 12, ni);
          const geo = new THREE.BufferGeometry();
          geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
          geo.setIndex(new THREE.BufferAttribute(idx, 1));
          geo.computeVertexNormals();
          geo.computeBoundingBox();
          return geo;
        });
      meshCache.set(key, p);
    }
    return meshCache.get(key);
  }

  function arrayMeshFor(item, d){
    const g = new THREE.Group();
    const a = d.aabb || [0, 0, 0, 1, 1, 1];
    const box = new THREE.Mesh(new THREE.BoxGeometry(a[3] || 0.01, a[4] || 0.01, a[5] || 0.01),
      new THREE.MeshBasicMaterial({ color: tintOf(item), wireframe: true, transparent: true, opacity: 0.25 }));
    box.position.set(a[0] + a[3] / 2, a[1] + a[4] / 2, a[2] + a[5] / 2);
    g.add(box);
    loadArrayMesh(d.id, d.scene).then(geo => {
      if (!g.parent) return;
      g.remove(box);
      const m = new THREE.Mesh(geo, materialFor(item, { color: d.color }));
      m.material.side = THREE.DoubleSide;
      m.userData.path = item.path; meshes.push(m);
      g.add(m); requestRender();
    }).catch(() => { box.userData.reason = "mesh failed to load"; });
    return g;
  }

  function modelFor(item, d){
    const g = new THREE.Group();
    const placeholder = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshBasicMaterial({ color: tintOf(item), wireframe: true, transparent: true, opacity: 0.4 }));
    g.add(placeholder);
    const url = d.url || "";
    loadModel(url).then(src => {
      if (!g.parent) return;                       // rebuilt since
      const obj = src.clone(true);
      g.remove(placeholder);
      obj.traverse(o => { if (o.isMesh){ o.userData.path = item.path; meshes.push(o); } });
      g.add(obj); requestRender();
    }).catch(() => { placeholder.userData.reason = "model failed to load"; });
    return g;
  }

  /* HOW MANY LIGHTS ACTUALLY LIGHT. three.js is a forward renderer: every
     light is a uniform in every material's shader, and a street with 265
     OmniLight3Ds (measured, a real driving study) compiles a shader per
     material with 265 point lights in it and takes the tab down. Godot
     clusters; this budgets. Directional lights always count, then the first
     omni/spot lights up to the cap; the rest draw as glyphs only, which is
     still the honest picture of WHERE the lights are. */
  const LIGHT_BUDGET = 8;
  let litCount = 0;

  function lightFor(item, d){
    const color = new THREE.Color(d.color[0], d.color[1], d.color[2]);
    const g = new THREE.Group();
    let light = null;
    const budgeted = d.light === "directional" || litCount < LIGHT_BUDGET;
    if (!budgeted){
      const glyphOnly = new THREE.Mesh(new THREE.SphereGeometry(0.12, 8, 6), new THREE.MeshBasicMaterial({ color, wireframe: true, transparent: true, opacity: 0.5 }));
      glyphOnly.userData.reason = `light ${LIGHT_BUDGET}+ (shown, not lit: viewport budget)`;
      g.add(glyphOnly);
      return g;
    }
    if (d.light !== "directional") litCount++;
    if (d.light === "directional"){
      light = new THREE.DirectionalLight(color, d.energy * 1.2);
      // Godot lights shine down their local -Z; three's directional light
      // points at .target, so put the target one unit down -Z in local space.
      light.target.position.set(0, 0, -1); g.add(light.target);
    } else if (d.light === "omni"){
      light = new THREE.PointLight(color, d.energy * 4, d.range, 1.5);
    } else {
      light = new THREE.SpotLight(color, d.energy * 6, d.range, THREE.MathUtils.degToRad(d.angle), 0.4, 1.2);
      light.target.position.set(0, 0, -1); g.add(light.target);
    }
    g.add(light);
    const glyph = new THREE.Mesh(new THREE.SphereGeometry(0.12, 8, 6), new THREE.MeshBasicMaterial({ color, wireframe: true }));
    g.add(glyph);
    if (d.light !== "omni"){
      const dir = new THREE.ArrowHelper(new THREE.Vector3(0, 0, -1), new THREE.Vector3(), 0.8, color.getHex());
      g.add(dir);
    }
    return g;
  }

  /* ── camera ────────────────────────────────────────────────────────────── */
  function fit(paths){
    if (!list) return;
    const box = new THREE.Box3();
    let any = false;
    const want = paths && paths.length ? new Set(paths) : null;
    // Solid geometry frames the shot; collision wire only when there is
    // nothing else, because a ground collider is usually the biggest thing
    // in the file and fitting to it puts the whole town in a corner.
    const wire = new THREE.Box3();
    let anyWire = false;
    for (const [path, obj] of objects){
      if (want && !want.has(path)) continue;
      const item = obj.userData.item;
      if (!item || !item.spatial) continue;
      const kind = item.draw.kind;
      if (kind === "light" || kind === "environment" || kind === "group" || kind === "none") continue;
      const b = new THREE.Box3().setFromObject(obj, false);
      if (!isFinite(b.min.x)) continue;
      if (item.draw.wire){ wire.union(b); anyWire = true; }
      else { box.union(b); any = true; }
    }
    if (!any && anyWire){ box.copy(wire); any = true; }
    if (!any && list.bounds){
      box.set(new THREE.Vector3(...list.bounds.min), new THREE.Vector3(...list.bounds.max)); any = true;
    }
    requestRender();
    if (!any){ camera.position.set(8, 6, 10); orbit.target.set(0, 0, 0); return; }
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3()).length() || 4;
    const dist = size / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2)) * 1.15;
    const dir = new THREE.Vector3(1, 0.75, 1.25).normalize();
    camera.position.copy(center).add(dir.multiplyScalar(dist));
    camera.near = Math.max(0.05, dist / 500); camera.far = dist * 50; camera.updateProjectionMatrix();
    orbit.target.copy(center); orbit.update();
    if (opts.gameCam){ opts.gameCam = false; persist(); paintToggles(); }
  }

  function lookThroughGameCamera(){
    const cam = list && list.camera;
    if (!cam){ say("this scene has no Camera3D"); opts.gameCam = false; persist(); paintToggles(); return; }
    const m = M(cam.world);
    const pos = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();
    m.decompose(pos, q, s);
    camera.position.copy(pos);
    camera.fov = cam.fov || 75; camera.updateProjectionMatrix();
    const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(q);
    orbit.target.copy(pos.clone().add(fwd.multiplyScalar(10))); orbit.update();
    requestRender();
  }

  /* ── picking ───────────────────────────────────────────────────────────── */
  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  let downAt = null;

  function hitAt(ev){
    const r = renderer.domElement.getBoundingClientRect();
    ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const hits = ray.intersectObjects(meshes.filter(m => m.visible && m.parent), false);
    for (const h of hits){
      let o = h.object;
      // A hit inside an instance selects the INSTANCE, the way Godot does.
      const path = o.userData.path;
      const item = list.items.find(i => i.path === path);
      if (!item) continue;
      const owner = item.of || path;
      return { path: owner, item: list.items.find(i => i.path === owner) || item, point: h.point };
    }
    return null;
  }

  function onDown(ev){ if (ev.button === 0) downAt = { x: ev.clientX, y: ev.clientY }; }
  function onUp(ev){
    if (ev.button !== 0 || !downAt) return;
    const moved = Math.hypot(ev.clientX - downAt.x, ev.clientY - downAt.y);
    downAt = null;
    if (moved > 4 || (gizmo && gizmo.dragging)) return;
    const hit = hitAt(ev);
    const mode = ev.shiftKey ? "range" : (ev.ctrlKey || ev.metaKey) ? "toggle" : "set";
    if (window.SceneBuild && typeof SceneBuild.pick === "function") SceneBuild.pick(hit ? hit.path : null, mode);
    else setSelection(hit ? [hit.path] : [], hit ? hit.path : null);
  }
  function onMove(ev){
    const tip = host && host.querySelector("#sv3-hover");
    if (!tip) return;
    if (gizmo && gizmo.dragging){ tip.hidden = true; return; }
    const hit = hitAt(ev);
    if (!hit){ tip.hidden = true; return; }
    const r = renderer.domElement.getBoundingClientRect();
    tip.style.left = (ev.clientX - r.left) + "px"; tip.style.top = (ev.clientY - r.top) + "px";
    tip.textContent = `${hit.item.name} · ${hit.item.type || "instance"}`;
    tip.hidden = false;
  }
  function onKey(ev){
    if (!host || ev.target && /input|textarea/i.test(ev.target.tagName)) return;
    if (ev.key === "w" || ev.key === "W") setMode("translate");
    else if (ev.key === "e" || ev.key === "E") setMode("rotate");
    else if (ev.key === "r" || ev.key === "R") setMode("scale");
    else if (ev.key === "f" || ev.key === "F") fit(sel ? [sel] : null);
    else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z"){ ev.preventDefault(); ev.shiftKey ? redo() : undo(); }
    else if (ev.key === "Escape") escape();
  }

  /* ── selection ─────────────────────────────────────────────────────────── */
  function select(path, mode){
    if (window.SceneBuild && typeof SceneBuild.pick === "function") SceneBuild.pick(path, mode || "set");
    else setSelection(path ? [path] : [], path);
  }
  function setSelection(paths, primary){
    multi.clear(); (paths || []).forEach(p => multi.add(p));
    sel = primary || (paths && paths[0]) || null;
    if (outline){ world.remove(outline); outline = null; }
    if (gizmo) gizmo.detach();
    const obj = sel && objects.get(sel);
    if (!obj) { paintHud(); return; }
    outline = new THREE.BoxHelper(obj, cssColor("--accent"));
    world.add(outline); requestRender();
    const item = obj.userData.item;
    // Inside an instance: shown, outlined, not dragged. Godot selects the
    // instance; the insides belong to the other file.
    if (item && item.spatial && !item.of && item.path !== ".") gizmo.attach(obj);
    paintHud();
  }
  function escape(){ select(null); }

  /* ── staging ───────────────────────────────────────────────────────────── */
  let gizmoStart = null;
  const snapshotOf = o => o ? { p: o.position.clone(), q: o.quaternion.clone(), s: o.scale.clone() } : null;

  function onGizmoEnd(){
    const obj = gizmo.object;
    if (!obj || !gizmoStart) return;
    const before = gizmoStart, after = snapshotOf(obj);
    gizmoStart = null;
    if (before.p.equals(after.p) && before.q.equals(after.q) && before.s.equals(after.s)) return;
    stage(obj.userData.item.path, before, after);
  }

  function stage(path, before, after){
    undoStack.push({ path, before, after }); redoStack = [];
    pending.set(path, after);
    paintPending(); paintHud();
    if (outline) outline.update();
  }
  function undo(){
    const op = undoStack.pop(); if (!op) return;
    redoStack.push(op); restore(op.path, op.before);
  }
  function redo(){
    const op = redoStack.pop(); if (!op) return;
    undoStack.push(op); restore(op.path, op.after);
  }
  function restore(path, snap){
    const obj = objects.get(path); if (!obj) return;
    obj.position.copy(snap.p); obj.quaternion.copy(snap.q); obj.scale.copy(snap.s);
    requestRender();
    // Back at the file's own placement? Then there is nothing pending for it.
    const orig = originalOf(path);
    if (orig && orig.p.equals(snap.p) && orig.q.equals(snap.q) && orig.s.equals(snap.s)) pending.delete(path);
    else pending.set(path, snap);
    if (outline) outline.update();
    paintPending(); paintHud();
  }
  const originals = new Map();
  function originalOf(path){
    if (!originals.has(path)){
      const item = list.items.find(i => i.path === path); if (!item) return null;
      const wm = M(item.world), pw = objects.get(parentOf(path)) ? M(list.items.find(i => i.path === parentOf(path)).world) : new THREE.Matrix4();
      const local = pw.clone().invert().multiply(wm);
      const p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();
      local.decompose(p, q, s); originals.set(path, { p, q, s });
    }
    return originals.get(path);
  }
  const pendingCount = () => pending.size;
  const hasPending = () => pending.size > 0;

  /* The line(s) a staged placement becomes. The spelling the node already
     uses wins: `transform =` stays one line, and a node written as
     position / rotation / scale gets the ones that changed. */
  const num = v => {
    const r = Math.round(v * 1e5) / 1e5;
    return Number.isInteger(r) ? String(r) : String(r);
  };
  function linesFor(path, snap){
    const item = list.items.find(i => i.path === path);
    const orig = originalOf(path) || snap;
    const out = [];
    if (item && item.has_transform){
      const m = new THREE.Matrix4().compose(snap.p, snap.q, snap.s).elements;   // column-major
      // Row-major basis rows, then origin: the twelve numbers Godot writes.
      const rows = [m[0], m[4], m[8], m[1], m[5], m[9], m[2], m[6], m[10], m[12], m[13], m[14]];
      out.push({ key: "transform", value: `Transform3D(${rows.map(num).join(", ")})` });
      return out;
    }
    if (!snap.p.equals(orig.p)) out.push({ key: "position", value: `Vector3(${num(snap.p.x)}, ${num(snap.p.y)}, ${num(snap.p.z)})` });
    if (!snap.q.equals(orig.q)){
      const e = new THREE.Euler().setFromQuaternion(snap.q, "YXZ");
      out.push({ key: "rotation", value: `Vector3(${num(e.x)}, ${num(e.y)}, ${num(e.z)})` });
    }
    if (!snap.s.equals(orig.s)) out.push({ key: "scale", value: `Vector3(${num(snap.s.x)}, ${num(snap.s.y)}, ${num(snap.s.z)})` });
    return out;
  }

  async function applyPending(){
    if (busy || !pendingCount()) return;
    const n = pendingCount();
    const lines = [...pending.keys()].map(p => `${p}: ${linesFor(p, pending.get(p)).map(l => l.key).join(", ")}`);
    lines.push("The current file is kept under .bgate_out/scene_backups.");
    if (held) lines.push(`NOTE: the ${held.seat} seat holds this file - the write will be refused while it does.`);
    const go = await confirmAsk({
      title: `Write ${n} change${n === 1 ? "" : "s"} to ${String(scene).split("/").pop()}?`,
      body: lines, ok: "write to the file", danger: true,
    });
    if (!go || busy) return;
    busy = true;
    const failed = [];
    for (const [path, snap] of pending){
      for (const line of linesFor(path, snap)){
        const r = await mutate("/api/scene/node/property", { body: { scene, node: path, key: line.key, value: line.value }, quiet: true });
        if (!r.ok) failed.push(`${path}.${line.key}: ${r.error}`);
      }
    }
    busy = false;
    if (failed.length) say(`${failed.length} write(s) refused: ${failed[0]}`, "bad");
    else say(`wrote ${n} change${n === 1 ? "" : "s"}`);
    originals.clear();
    await reload();
    if (window.SceneBuild && typeof SceneBuild.refresh === "function") SceneBuild.refresh();
  }
  async function discardPending(){
    if (!hasPending()) return;
    originals.clear();
    await reload();
  }

  /* ── painting the chrome ───────────────────────────────────────────────── */
  function paintPending(){
    const bar = host && host.querySelector("#sv3-pending"); if (!bar) return;
    const n = pendingCount();
    bar.hidden = !n;
    const label = host.querySelector("#sv3-pending-n");
    if (label) label.textContent = `${n} change${n === 1 ? "" : "s"} staged, nothing written yet`;
    const u = host.querySelector("#sv3-undo"), r = host.querySelector("#sv3-redo");
    if (u) u.disabled = !undoStack.length; if (r) r.disabled = !redoStack.length;
  }
  function paintLock(){
    const el = host && host.querySelector("#sv3-lock"); if (!el) return;
    el.hidden = !held;
    if (held) el.textContent = `${held.seat} holds this file${held.reason ? `: ${held.reason}` : ""}. Writes will be refused until it is released.`;
  }
  function paintHud(){
    const hud = host && host.querySelector("#sv3-hud"); if (!hud || !list) return;
    const item = sel && list.items.find(i => i.path === sel);
    const obj = sel && objects.get(sel);
    let line = `${list.items.length} nodes · ${list.lights} light(s)${list.lights > LIGHT_BUDGET ? ` (${LIGHT_BUDGET} lit)` : ""}${list.camera ? " · camera " + list.camera.path : " · no camera"}${stopped ? " · STOPPED" : opts.render === "live" ? " · live" : ""}`;
    if (item && obj){
      const e = new THREE.Euler().setFromQuaternion(obj.quaternion, "YXZ");
      line += `\n${item.name}  pos ${[obj.position.x, obj.position.y, obj.position.z].map(num).join(", ")}`
            + `  rot ${[e.x, e.y, e.z].map(v => num(THREE.MathUtils.radToDeg(v))).join(", ")}°`
            + `  scale ${[obj.scale.x, obj.scale.y, obj.scale.z].map(num).join(", ")}`
            + (item.of ? `  (inside ${item.of}, not editable here)` : "")
            + (item.draw && item.draw.reason ? `  · ${item.draw.reason}` : "");
    }
    hud.textContent = line;
  }
  function paintToggles(){
    const map = { grid: "#sv3-grid", wire: "#sv3-wire", showHidden: "#sv3-hidden", lights: "#sv3-lights", gameCam: "#sv3-cam", snap: "#sv3-snap" };
    for (const [k, q] of Object.entries(map)){
      const b = host && host.querySelector(q); if (b) b.classList.toggle("on", !!opts[k]);
    }
    const live = host && host.querySelector("#sv3-live"); if (live) live.classList.toggle("on", opts.render === "live");
    for (const m of ["translate", "rotate", "scale"]){
      const b = host && host.querySelector(`#sv3-m-${m[0]}`); if (b) b.classList.toggle("on", opts.mode === m);
    }
  }

  /* ── toolbar actions ───────────────────────────────────────────────────── */
  function toggle(key){
    if (key === "render"){
      opts.render = opts.render === "live" ? "demand" : "live"; persist(); paintToggles();
      const l = host && host.querySelector("#sv3-live-l"); if (l) l.textContent = opts.render === "live" ? "live" : "on demand";
      requestRender(); return;
    }
    opts[key] = !opts[key]; persist(); paintToggles();
    if (key === "gameCam"){ if (opts.gameCam) lookThroughGameCamera(); else fit(); return; }
    if (key === "snap"){ applySnap(); return; }
    const grid = world.getObjectByName("__grid"), axes = world.getObjectByName("__axes"), fill = world.getObjectByName("__fill");
    requestRender();
    if (key === "grid"){ if (grid) grid.visible = opts.grid; if (axes) axes.visible = opts.grid; return; }
    if (key === "lights" && fill){ fill.intensity = opts.lights ? 0.9 : 1.8; }
    // wire / hidden / lights change what is built; rebuild keeps staged moves
    // by re-applying them on top of the fresh objects.
    const staged = new Map(pending);
    build();
    for (const [p, snap] of staged){ const o = objects.get(p); if (o){ o.position.copy(snap.p); o.quaternion.copy(snap.q); o.scale.copy(snap.s); } }
    setSelection([...multi], sel);
  }
  function setMode(mode){
    opts.mode = mode; persist(); if (gizmo) gizmo.setMode(mode); paintToggles();
  }
  async function snapshot(){
    if (!renderer) return;
    renderer.render(world, camera);
    const png = renderer.domElement.toDataURL("image/png");
    const r = await mutate("/api/scene/snapshot", { body: { scene, png }, quiet: true });
    if (r.ok) say(`saved ${r.data.rel}`); else say(r.error || "could not save the snapshot", "bad");
  }
  async function reshoot(){
    // The engine's own frame: godot_screenshot through the workspace route.
    say("running the game for a real frame…");
    const r = await mutate("/api/godot/screenshot", { body: { scene, at: 1.0 }, quiet: true });
    if (!r.ok){ say(r.error || "the engine did not produce a frame", "bad"); return; }
    const rel = r.data && (r.data.rel || r.data.path);
    if (rel && window.openLightbox) openLightbox(`/api/preview?rel=${encodeURIComponent(rel)}`);
    else say(`captured ${rel || "a frame"}`);
  }

  const notHere = what => () => say(`${what} is not available in the 3D viewport yet; use the graph`);

  return {
    mount, unmount, reload, fit, select, setSelection, escape,
    apply: applyPending, discard: discardPending, hasPending, undo, redo,
    toggle, setMode, snapshot, reshoot, stop, start, requestRender,
    frame: p => fit(p ? [p] : null),
    removeSelected: notHere("delete"), duplicateSelected: notHere("duplicate"),
    placeMenu: notHere("place"), pasteClones: notHere("paste"),
    raise: () => {}, nudge: () => {}, gameScale: () => fit(), zoom: () => {},
    setSnap: () => {}, stageVisible: () => {}, setVisibleBatch: () => {},
    togglePlay: notHere("play"), rebuild: notHere("rebuild"), suspend,
    toggleLayer: () => {}, layerClick: () => {}, layerEye: () => {}, isolateLayer: () => {},
    showAllLayers: () => {}, repaintLayers: () => {}, nextBlank: () => {}, realView: reshoot,
    arm: () => {}, cancelPlacing: () => {}, setScene: s => { scene = s; },
    get layers(){ return []; },
    get list(){ return list; }, get selected(){ return sel ? { path: sel } : null; },
    get selection(){ return [...multi]; },
    get scene(){ return scene; },
    get pending(){ return pendingCount(); },
    get dimension(){ return "3d"; },
  };
})();
