/* scenebuild.js — Atlas's third mode: build the scene, not just map it.
 *
 * The list view tells you which assets a screen uses. The graph view lets you
 * wire one in. Neither shows you the SCENE — the actual tree of nodes, what
 * each one is for, which sheet is on which sprite, which script drives what.
 * That is the thing you are really editing when you say "swap the enemy", and
 * until now it lived only in Godot.
 *
 * So this is the scene as a node graph:
 *
 *   ROLE, NOT CLASS. Nodes are grouped and coloured as characters, enemies,
 *   props, layers, controllers, audio, collision, ui — because that is how a
 *   scene is thought about. "CharacterBody2D" is an implementation detail of
 *   being a character, and a canvas organised by class is a canvas you read by
 *   translating. The role is inferred server-side from resource paths, script
 *   names and type, in that order.
 *
 *   THE CARD IS THE NODE. Each card shows its type, its script, and the assets
 *   hanging off it — with the actual sprite sheet as a thumbnail and audio as a
 *   player. Seeing that a node points at the wrong sheet is the point.
 *
 *   EVERY EDIT IS A DIFF FIRST. Add, rename, reparent, set a property, swap a
 *   resource, delete a subtree: each one previews the resulting .tscn before it
 *   writes, and each write leaves the previous file under .bgate_out.
 *
 * Parent/child is drawn as the edge, so the graph IS the hierarchy — dragging
 * a node's parent port onto another node reparents it for real.
 */
window.SceneBuild = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };

  /* Colour carries the role, so a scene reads at a glance before any label is
     legible. These are the same eight buckets the server classifies into. */
  const ROLE = {
    character:{ g:"☻", c:"var(--warn)", label:"characters" },
    enemy:    { g:"☠", c:"var(--bad)", label:"enemies" },
    prop:     { g:"▤", c:"var(--warn-line)", label:"props" },
    item:     { g:"⚔", c:"var(--warn)", label:"items" },
    layer:    { g:"▦", c:"var(--accent)", label:"layers" },
    visual:   { g:"▧", c:"var(--text)", label:"visuals" },
    collision:{ g:"⬡", c:"var(--text-3)", label:"collision" },
    controller:{g:"⌁", c:"var(--c-narrative)", label:"controllers" },
    camera:   { g:"◉", c:"var(--text)", label:"camera" },
    audio:    { g:"♪", c:"var(--c-narrative)", label:"audio" },
    fx:       { g:"✦", c:"var(--c-narrative)", label:"fx" },
    ui:       { g:"⊞", c:"var(--good)", label:"ui" },
    marker:   { g:"✜", c:"var(--text-3)", label:"markers" },
    instance: { g:"⬢", c:"var(--text)", label:"instances" },
    node:     { g:"·", c:"var(--text-3)", label:"nodes" },
  };
  const roleOf = r => ROLE[r] || ROLE.node;

  /* The project scan, from whoever loaded it. Atlas owns the shared copy and
     paints the nav badge from it on startup; AtlasGraph only has one once its
     mode has been opened. Reading AtlasGraph first made the scene picker empty
     for anyone who came straight here. */
  const atlasMap = () => (window.Atlas && Atlas.map)
    || (window.AtlasGraph && AtlasGraph.map) || null;

  /* Role labels are plural because they name buckets ("characters"), but a
     count of one reads as broken text: "1 nodes". */
  const countLabel = (label, n) =>
    n === 1 ? label.replace(/ies$/, "y").replace(/s$/, "") : label;
  const IMG = /\.(png|webp|jpe?g|svg)$/i;
  const SND = /\.(ogg|wav|mp3)$/i;

  // The properties worth a first-class row in the inspector. Everything else a
  // node carries is still shown, just not promoted — a scene builder that only
  // ever exposes position and visible is a scene builder you leave immediately.
  const COMMON = [
    { key:"position", hint:"Vector2(x, y)" },
    { key:"rotation", hint:"radians" },
    { key:"scale", hint:"Vector2(x, y)" },
    { key:"z_index", hint:"draw order" },
    { key:"visible", hint:"true / false" },
    { key:"modulate", hint:"Color(r, g, b, a)" },
  ];

  const COL_W = 300, ROW_H = 190, PAD_X = 40, PAD_Y = 40;

  let nc = null, scene = null, data = null, types = null;
  let sel = null, filter = new Set(), busy = false;
  // The viewport is the primary surface — it is where things are PLACED. The
  // graph is the structural read of the same scene, one click away.
  let surface = (() => {
    try { return localStorage.getItem("bgate-scene-surface") || "viewport"; }
    catch (e){ return "viewport"; }
  })();
  let selecting = false;      // guards the two surfaces echoing selection

  /* ── styles ───────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("scenebuild-style")) return;
    const s = document.createElement("style");
    s.id = "scenebuild-style";
    s.textContent = [
      ".sb-wrap{display:flex;height:min(78vh,900px);border:1px solid var(--seam);border-radius:12px;overflow:hidden;background:var(--iron)}",
      ".sb-canvas{flex:1;position:relative;min-width:0}",
      ".sb-view{flex:1;position:relative;min-width:0;display:flex}",
      ".sb-view>*{flex:1;min-width:0}",
      ".sb-surf{display:flex;gap:5px;margin-right:4px}",
      ".sb-surf button{font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase;padding:5px 11px;border:1px solid var(--seam);border-radius:999px;background:none;color:var(--ash);cursor:pointer}",
      ".sb-surf button:hover{border-color:var(--ember);color:var(--bone)}",
      ".sb-surf button.on{background:var(--plate);border-color:var(--ember);color:var(--bone)}",
      ".sb-side{width:308px;flex:none;border-left:1px solid var(--seam);background:var(--iron);overflow-y:auto;padding:13px}",
      ".sb-bar{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:10px}",
      ".sb-in{background:var(--void);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;padding:5px 9px}",
      ".sb-in:focus{outline:none;border-color:var(--ember)}",
      ".sb-b{padding:6px 10px;background:var(--plate);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;cursor:pointer}",
      ".sb-b:hover:not(:disabled){border-color:var(--ember)}",
      ".sb-b:disabled{opacity:.45;cursor:default}",
      ".sb-b.go{background:var(--ember);color:var(--bg);border-color:var(--ember);font-weight:var(--fw-semi)}",
      ".sb-b.bad:hover{border-color:var(--bad);color:var(--bad)}",
      ".sb-b.wide{width:100%;margin-bottom:6px}",
      ".sb-chip{font-family:var(--mono);font-size:9.5px;padding:4px 9px;border:1px solid var(--seam);border-radius:999px;color:var(--ash);cursor:pointer;background:none;display:inline-flex;gap:5px;align-items:center}",
      ".sb-chip:hover{border-color:var(--ember);color:var(--bone)}",
      ".sb-chip.on{color:var(--bone);background:var(--plate)}",
      ".sb-chip i{width:7px;height:7px;border-radius:50%;display:block}",
      ".sb-h{font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--ash2);margin:15px 0 7px}",
      ".sb-h:first-child{margin-top:0}",
      ".sb-note{font-size:11px;color:var(--ash);line-height:1.5;margin-bottom:8px}",
      ".sb-note b{color:var(--bone)}",
      ".sb-warn{color:var(--warn)}",
      // The panel's one honest dead-end: what the file holds when it holds
      // nothing you can edit here.
      ".sb-state{border:1px solid var(--warn-line);border-radius:8px;padding:8px 10px;margin-bottom:10px;background:var(--void)}",
      ".sb-state .hd{font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--warn);margin-bottom:5px}",
      ".sb-state p{font-size:11px;color:var(--ash);line-height:1.5;margin:0}",
      ".sb-state b{color:var(--bone)}",
      ".sb-row{display:flex;align-items:center;gap:6px;margin-bottom:5px}",
      ".sb-row label{font-family:var(--mono);font-size:9.5px;color:var(--ash2);flex:none;width:64px}",
      ".sb-row .sb-in{flex:1;min-width:0}",
      // node bodies
      ".sb-nb{font-family:var(--mono);font-size:10px;color:var(--ash);line-height:1.5}",
      ".sb-nb .ty{color:var(--ash2);font-size:9px;letter-spacing:.08em;text-transform:uppercase}",
      ".sb-nb .sc{color:var(--c-narrative);word-break:break-all;font-size:9px}",
      ".sb-nb .res{display:flex;gap:6px;align-items:center;margin-top:6px;padding:4px;border:1px solid var(--seam);border-radius:6px;background:var(--void)}",
      ".sb-nb .res img{width:44px;height:34px;object-fit:contain;image-rendering:pixelated;background:var(--bg);border-radius:4px;flex:none}",
      ".sb-nb .res .k{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:9px}",
      ".sb-nb .res .k b{color:var(--bone);display:block}",
      ".sb-nb .res.missing{border-color:var(--bad-line)}",
      ".sb-nb audio{width:100%;height:26px;margin-top:5px}",
      ".sb-nb .acts{display:flex;gap:4px;margin-top:6px}",
      ".sb-nb .acts button{flex:1;padding:3px;background:var(--plate);border:1px solid var(--seam);border-radius:5px;color:var(--bone);font:inherit;font-size:9.5px;cursor:pointer}",
      ".sb-nb .acts button:hover{border-color:var(--ember)}",
      ".sb-prop{display:flex;gap:6px;align-items:center;font-family:var(--mono);font-size:9.5px;color:var(--ash2);padding:3px 0;border-bottom:1px solid var(--seam)}",
      ".sb-prop b{color:var(--bone);font-weight:400}",
      ".sb-prop .x{margin-left:auto;cursor:pointer}",
      ".sb-prop .x:hover{color:var(--bad)}",
      ".sb-pal{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}",
      ".sb-palh{font-family:var(--mono);font-size:8.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--ash2);margin:8px 0 4px}",
      ".sb-drawer{border:1px solid var(--seam);border-radius:8px;padding:7px 9px;margin-top:14px;background:var(--void)}",
      ".sb-drawer>summary{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--ash);cursor:pointer;list-style:none}",
      ".sb-drawer>summary::-webkit-details-marker{display:none}",
      ".sb-drawer>summary::before{content:'+ ';color:var(--ember)}",
      ".sb-drawer[open]>summary::before{content:'\\2212 '}",
      ".sb-drawer>summary:hover{color:var(--bone)}",
      ".sb-pal button{font-family:var(--mono);font-size:9.5px;padding:3px 8px;border:1px solid var(--seam);border-radius:6px;background:var(--plate);color:var(--bone);cursor:pointer}",
      ".sb-pal button:hover{border-color:var(--ember)}",
      ".sb-modal{position:fixed;inset:0;z-index:1300;background:var(--overlay);display:flex;align-items:center;justify-content:center;padding:38px}",
      ".sb-mbox{background:var(--iron);border:1px solid var(--seam);border-radius:12px;width:min(740px,100%);max-height:100%;display:flex;flex-direction:column;overflow:hidden}",
      ".sb-mhd{padding:11px 14px;border-bottom:1px solid var(--seam);font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--bone)}",
      ".sb-mbd{padding:14px;overflow-y:auto}",
      ".sb-mft{padding:11px 14px;border-top:1px solid var(--seam);display:flex;gap:8px;justify-content:flex-end}",
      ".sb-diff{background:var(--void);border:1px solid var(--seam);border-radius:7px;padding:9px;font-family:var(--mono);font-size:9.5px;color:var(--ash);white-space:pre-wrap;max-height:300px;overflow:auto}",
      ".sb-pick{display:flex;align-items:center;gap:9px;padding:6px 9px;border-radius:7px;cursor:pointer;font-family:var(--mono);font-size:10.5px;color:var(--bone)}",
      ".sb-pick:hover{background:var(--plate)}",
      ".sb-pick img{width:40px;height:32px;object-fit:contain;image-rendering:pixelated;background:var(--bg);border-radius:4px;border:1px solid var(--seam);flex:none}",
      ".sb-pick .m{margin-left:auto;color:var(--ash2);font-size:9px}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── load ─────────────────────────────────────────────────────────────── */
  async function activate(sceneId, force){
    injectStyle();
    const host = document.getElementById("atlas-scene");
    if (!host) return;
    if (sceneId) scene = sceneId;
    if (!scene) scene = bestScene();
    if (!scene){
      host.innerHTML = `<div class="empty" style="padding:40px">no scene to build — Atlas found no .tscn</div>`;
      return;
    }
    if (!types) types = await readJSON("/api/scene/node/types", {groups:[]});
    if (!data || force || data.scene !== scene) await reload();
    render();
  }

  /* Which scene to open on. Alphabetical picks whatever sorts first, and in
     this project that is `combat` — a one-node script host with nothing to
     draw. A builder whose first frame is an empty rectangle looks broken even
     when it is being perfectly accurate, so open on the scene the map says has
     the most in it. */
  function bestScene(){
    const map = atlasMap();
    const screens = (map && map.screens) || [];
    if (!screens.length) return null;
    const weight = {};
    ((map && map.edges) || []).forEach(e => {
      weight[e.from] = (weight[e.from] || 0) + 1;
    });
    return screens.slice().sort(
      (a, b) => (weight[b.id] || 0) - (weight[a.id] || 0))[0].id;
  }

  async function reload(){
    const host = document.getElementById("atlas-scene");
    if (host && !data) host.innerHTML = `<div class="empty" style="padding:40px">reading the scene…</div>`;
    const d = await readJSON(`/api/scene/outline?scene=${encodeURIComponent(scene)}`, null);
    if (!d || d.__error){
      if (host) host.innerHTML = `<div class="empty" style="padding:40px">${
        E((d && d.__error) || "could not read that scene")}</div>`;
      data = null;
      return false;
    }
    data = d;
    return true;
  }

  /* ── layout ───────────────────────────────────────────────────────────── */
  /* Depth-first, one column per tree level. The hierarchy IS the layout, so a
     scene's shape is visible before a single label is read. */
  function build(){
    const nodes = [], edges = [];
    const shown = data.nodes.filter(n =>
      !filter.size || filter.has(n.role) || n.path === ".");
    const byPath = new Map(shown.map(n => [n.path, n]));
    const depth = n => n.path === "." ? 0 : n.path.split("/").length;
    const rows = {};

    shown.forEach(n => {
      const d = depth(n);
      rows[d] = (rows[d] || 0);
      const r = roleOf(n.role);
      nodes.push({
        id: n.path, x: PAD_X + d * COL_W, y: PAD_Y + rows[d] * ROW_H,
        w: 268, type: n.role,
        title: n.name === "." ? (data.root || "root") : n.name,
        glyph: r.g, accent: r.c,
        badge: n.type || "",
        ports: {
          in: n.path === "." ? [] : [{ id:"parent", label:"parent", type:"node" }],
          out: [{ id:"children", label:"children", type:"node" }],
        },
        data: n,
      });
      rows[d]++;
      const parent = n.parent === null ? null : (n.parent === "." ? "." : n.parent);
      if (parent !== null && byPath.has(parent))
        edges.push({ from:[parent, "children"], to:[n.path, "parent"] });
    });
    return { nodes, edges };
  }

  function renderBody(node){
    const n = node.data || {};
    const res = (n.resources || []).filter(r => r.property !== "script");
    return `<div class="sb-nb">
      <div class="ty">${E(n.type || "node")}${n.instance ? " · instance" : ""}</div>
      ${n.script ? `<div class="sc">⌁ ${E(n.script.split("/").pop())}</div>` : ""}
      ${res.map(r => `
        <div class="res${r.exists ? "" : " missing"}">
          ${r.preview ? `<img loading="lazy" src="/api/preview?rel=${encodeURIComponent(r.preview)}" alt="">` : ""}
          <span class="k"><b>${E(r.property)}</b>${E(r.path.split("/").pop())}</span>
        </div>
        ${SND.test(r.path) ? `<audio controls preload="none"
           src="/api/audio/file?rel=${encodeURIComponent(r.path.replace("res://", (data.rel||"").split("/")[0] + "/"))}"></audio>` : ""}
      `).join("")}
      ${!res.length && !n.script ? `<div style="color:var(--ash2)">no assets</div>` : ""}
      <div class="acts">
        <button onclick="SceneBuild.select('${E(n.path)}')">inspect</button>
        ${res.length ? `<button onclick="SceneBuild.swapMenu('${E(n.path)}','${E(res[0].property)}')">swap</button>` : ""}
      </div>
    </div>`;
  }

  /* ── render ───────────────────────────────────────────────────────────── */
  function render(){
    const host = document.getElementById("atlas-scene");
    if (!host || !data) return;
    const map = atlasMap();
    const screens = (map && map.screens) || [];
    const roles = data.roles || {};

    host.innerHTML = `
      <div class="sb-bar">
        <div class="sb-surf">
          <button class="${surface==='viewport'?'on':''}" onclick="SceneBuild.setSurface('viewport')">viewport</button>
          <button class="${surface==='graph'?'on':''}" onclick="SceneBuild.setSurface('graph')">graph</button>
        </div>
        <select class="sb-in" onchange="SceneBuild.setScene(this.value)">
          ${screens.map(s => `<option value="${E(s.id)}"${s.id===scene?" selected":""}>⊞ ${E(s.label)}</option>`).join("")
            || `<option>${E(scene)}</option>`}
        </select>
        ${Object.keys(roles).sort().map(r => {
          const info = roleOf(r);
          return `<button class="sb-chip${filter.has(r)?" on":""}"
            style="${filter.has(r)?`border-color:${info.c}`:""}"
            onclick="SceneBuild.toggleRole('${E(r)}')"><i style="background:${info.c}"></i>${
            E(info.label)} ${roles[r]}</button>`;
        }).join("")}
        <span style="flex:1"></span>
        <button class="sb-b" onclick="SceneBuild.refresh()">reread</button>
      </div>
      <div class="sb-wrap">
        <div class="sb-view" id="sb-view" ${surface==='viewport'?'':'hidden'}></div>
        <div class="sb-canvas" id="sb-canvas" ${surface==='graph'?'':'hidden'}></div>
        <div class="sb-side" id="sb-side"></div>
      </div>`;

    if (surface === "graph"){
      const { nodes, edges } = build();
      nc = new NodeCanvas(document.getElementById("sb-canvas"), {
        nodes, edges, renderBody, accent: "var(--ember)",
        onSelect: node => select(node ? node.id : null),
        onConnect: onConnect,
        onReject: why => say(why),
      });
      nc.mount();
      nc.fit({ min: 0.4 });
    } else {
      nc = null;
      const viewHost = document.getElementById("sb-view");
      if (!window.SceneView){
        viewHost.innerHTML =
          `<div class="sb-note" style="padding:24px">the viewport did not load</div>`;
      } else if (!viewHost.firstChild || SceneView.scene !== scene){
        // Mounting re-reads the file. Doing that over staged edits would throw
        // them away to redraw a panel that is already correct.
        SceneView.mount(viewHost, scene);
      }
    }
    side();
  }

  /* Leaving the viewport tears down SceneView, which throws away anything staged
     there. unmount() asks about that and returns false when the answer is no —
     an answer this used to discard, so it asked the question and switched
     regardless. Nothing may change until it comes back true. */
  async function setSurface(next){
    const want = next === "graph" ? "graph" : "viewport";
    if (want === "graph" && window.SceneView && !(await SceneView.unmount())) return;
    surface = want;
    try { localStorage.setItem("bgate-scene-surface", surface); } catch (e) {}
    render();
  }

  /* Dragging a parent's `children` port onto a node's `parent` port IS a
     reparent — the graph edge and the scene hierarchy are the same fact, so
     editing one has to edit the other. */
  async function onConnect(from, to){
    const parent = from[0], child = to[0];
    const r = await mutate("/api/scene/node/reparent", {
      body: { scene, node: child, parent, dry_run: true }, quiet: true });
    if (!r.ok){ say(r.error); await reload(); render(); return; }
    confirmDiff(`move ${child} under ${parent}`, r.data,
      () => mutate("/api/scene/node/reparent", { body: { scene, node: child, parent } }),
      () => { reload().then(render); });
  }

  /* ── inspector ────────────────────────────────────────────────────────── */
  function select(path){
    if (selecting) return;          // SceneView calls back into here
    selecting = true;
    try {
      sel = path;
      if (nc && path) try { nc.select(path); } catch (e) {}
      if (surface === "viewport" && window.SceneView
          && SceneView.selected !== undefined
          && (!SceneView.selected || SceneView.selected.path !== path)){
        try { SceneView.select(path); } catch (e) {}
      }
      side();
    } finally { selecting = false; }
  }

  /* WHAT THE FILE ACTUALLY HOLDS, when the answer is "nothing you can edit".
   *
   * Two nodes in every tile-based scene are dead ends for an editor, and until
   * now the inspector offered its usual six property rows for both of them as
   * if they were ordinary: a TileMapLayer, whose tiles are packed bytes rather
   * than nodes, and an empty container that a script fills with add_child when
   * the game runs. Setting `position` on the second one is not wrong exactly —
   * it is just about to be overwritten, which is worse than being refused.
   *
   * So say which it is, name the script where the file names one, and put the
   * properties behind a fold so the panel stops implying they are the point.
   */
  const CONTAINER_TYPES = new Set(["Node", "Node2D", "Node3D", "CanvasLayer",
    "YSort", "Control", "ParallaxBackground", "ParallaxLayer"]);

  /* The script that fills a container: its own, else the nearest ancestor with
     one. That is the end of what the FILE knows — anything further would be a
     guess dressed as a fact. */
  function fillerOf(n){
    let node = n;
    for (let i = 0; i < 12 && node; i++){
      if (node.script) return { script: node.script, on: node.path };
      if (node.parent === null || node.parent === undefined) return null;
      node = data.nodes.find(x => x.path === (node.parent || "."));
    }
    return null;
  }

  function fileState(n){
    if (!n || n.path === ".") return null;
    if (n.type === "TileMapLayer" || n.type === "TileMap")
      return { kind: "tiles", head: "this layer holds tiles, not nodes",
        body: `Its cells are packed bytes on the layer itself, so there is `
          + `nothing inside <b>${E(n.name)}</b> to select, name or script. `
          + `Builders Gate can move, hide and re-skin the layer; adding or `
          + `removing individual tiles is the game's own tile pipeline.` };
    if (!CONTAINER_TYPES.has(n.type)) return null;
    if (data.nodes.some(x => x.parent === n.path)) return null;
    if ((n.resources || []).some(r => r.property !== "script")) return null;
    const f = fillerOf(n);
    return { kind: "runtime", head: "empty in the file, filled at run time",
      body: f
        ? `<b>${E(n.name)}</b> has no children here. <b>${
            E(f.script.split("/").pop())}</b>${f.on === n.path ? ""
            : ` on <b>${E(f.on === "." ? (data.root || "the root") : f.on)}</b>`
          } builds its contents with add_child when the game runs, so anything `
          + `you set on it is what the script starts from — not what you see `
          + `in play.`
        : `<b>${E(n.name)}</b> has no children in this file and no script here `
          + `or above it says what fills it. Whatever appears inside it comes `
          + `from code somewhere else in the project.` };
  }

  function side(){
    const el = document.getElementById("sb-side");
    if (!el) return;
    const n = sel && data.nodes.find(x => x.path === sel);
    if (!n) return el.innerHTML = overview();

    const state = fileState(n);
    const shown = new Set(COMMON.map(c => c.key));
    const others = Object.entries(n.properties || {})
      .filter(([k]) => !shown.has(k) && k !== "script");
    const res = (n.resources || []).filter(r => r.property !== "script");
    const info = roleOf(n.role);
    const propsHtml = COMMON.map(c => `<div class="sb-row">
        <label title="${E(c.hint)}">${E(c.key)}</label>
        <input class="sb-in" id="sb-p-${E(c.key)}" placeholder="${E(c.hint)}"
               value="${E((n.properties || {})[c.key] || "")}">
        <button class="sb-b" onclick="SceneBuild.setProp('${E(c.key)}')">set</button>
      </div>`).join("")
      + (others.length ? `<div class="sb-h">also set</div>${others.map(([k, v]) => `
        <div class="sb-prop"><b>${E(k)}</b> ${E(v.length > 34 ? v.slice(0, 33) + "…" : v)}
          <span class="x" title="clear" onclick="SceneBuild.clearProp('${E(k)}')">✕</span></div>`).join("")}` : "");

    el.innerHTML = `
      <div class="sb-h">${E(info.label.replace(/s$/, ""))}</div>
      <div class="sb-note"><b>${E(n.name)}</b> · ${E(n.type || "node")}<br>
        <span style="font-family:var(--mono);font-size:9.5px;color:var(--ash2)">${E(n.path)}</span></div>
      ${state ? `<div class="sb-state">
        <div class="hd">${E(state.head)}</div><p>${state.body}</p></div>` : ""}
      ${n.path !== "." ? `<div class="sb-row">
        <label>name</label>
        <input class="sb-in" id="sb-name" value="${E(n.name)}">
        <button class="sb-b" onclick="SceneBuild.rename()">set</button></div>` : ""}

      <div class="sb-h">assets on this node</div>
      ${res.length ? res.map(r => `
        <div class="sb-note" style="margin-bottom:6px">
          ${r.preview ? `<img src="/api/preview?rel=${encodeURIComponent(r.preview)}"
             style="width:100%;background:var(--bg);border-radius:6px;image-rendering:pixelated;margin-bottom:5px">` : ""}
          <b>${E(r.property)}</b> → ${E(r.path)}${r.exists ? "" : ` <span class="sb-warn">missing</span>`}
          <div style="display:flex;gap:5px;margin-top:5px">
            <button class="sb-b" onclick="SceneBuild.swapMenu('${E(n.path)}','${E(r.property)}')">swap…</button>
            ${IMG.test(r.path) && r.preview ? `<button class="sb-b"
              onclick="SceneBuild.editPixels('${E(r.preview)}')">edit pixels</button>` : ""}
            ${SND.test(r.path) ? `<button class="sb-b"
              onclick="SceneBuild.editAudio('${E(r.path)}')">audio lab</button>` : ""}
          </div>
        </div>`).join("")
        : `<div class="sb-note">nothing — drop an asset on it from the graph, or
            <button class="sb-b" onclick="SceneBuild.swapMenu('${E(n.path)}','')">attach one…</button></div>`}

      <div class="sb-h">script</div>
      <div class="sb-note">${n.script
        ? `<b>${E(n.script)}</b>` : `<span class="sb-warn">no script — this node does nothing on its own</span>`}
        <div style="margin-top:5px"><button class="sb-b"
          onclick="SceneBuild.swapMenu('${E(n.path)}','script')">${n.script ? "swap script…" : "attach a script…"}</button></div>
      </div>

      ${state && state.kind === "runtime"
        ? `<details class="sb-drawer"><summary>properties — a script overwrites
             what this node holds</summary>${propsHtml}</details>`
        : `<div class="sb-h">properties</div>${propsHtml}`}

      ${childDrawer(n, types)}

      ${n.path !== "." ? `<div class="sb-h">danger</div>
        <button class="sb-b bad wide" onclick="SceneBuild.remove()">delete this node</button>` : ""}`;
  }

  /* The "add a child" palette used to render identically under every node, so
     a TileMapLayer called Walls offered the full ~40-type list. Godot lets you
     parent anything anywhere, but a node under a tilemap inherits its
     transform and is organisationally lost — the exact mistake bible #37 (one
     editable thing = one named node) exists to prevent, offered by the tool
     that is supposed to enforce it.

     Not a blanket ban: a body REQUIRES a CollisionShape2D child, Path2D needs
     PathFollow2D, and a Marker2D attachment point under a Sprite2D is normal.
     So the palette is ordered by what the selected type actually wants, and
     the cases where children are the wrong answer say so instead of hiding. */
  const WANTS = {
    CharacterBody2D: ["collision", "visual"], RigidBody2D: ["collision", "visual"],
    StaticBody2D: ["collision", "visual"], Area2D: ["collision", "visual"],
    Path2D: ["controller"],
    Node2D: ["visual", "character", "controller"], Node: ["controller"],
    CanvasLayer: ["ui", "visual"], ParallaxBackground: ["layer"],
    Sprite2D: ["controller"], AnimatedSprite2D: ["controller"],
  };
  // Types whose content is data, not children.
  const LEAFY = {
    TileMapLayer: "Tiles live in this layer's own data, not as child nodes — a " +
      "node you add here will NOT become a tile. Put objects in a Node2D " +
      "container beside this layer, so each one can be selected and named.",
    TileMap: "Tiles live in this node's own data, not as child nodes — a node " +
      "you add here will NOT become a tile. Put objects in a Node2D container " +
      "beside it.",
    CollisionShape2D: "A collision shape is a leaf — its shape is a resource, " +
      "not a child. Add siblings under the body instead.",
    CollisionPolygon2D: "A collision polygon is a leaf — add siblings under " +
      "the body instead.",
  };

  function childDrawer(n, types){
    const groups = (types.groups || []);
    const leafWhy = LEAFY[n.type];
    const wanted = WANTS[n.type] || null;
    // Preferred roles first, everything else after — never hidden, because the
    // uncommon case is still legitimate and a palette that lies is worse.
    const ordered = wanted
      ? groups.slice().sort((a, b) => {
          const ai = wanted.indexOf(a.role), bi = wanted.indexOf(b.role);
          return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
        })
      : groups;
    const pal = ordered.map(g => {
      const first = wanted && wanted[0] === g.role;
      return `<div class="sb-palh">${E(g.label)}${first ? " · usually what you want here" : ""}</div>
        <div class="sb-pal">${g.types.map(t =>
          `<button title="add a ${E(t)} under ${E(n.name)}"
            onclick="SceneBuild.addNode('${E(t)}')">${E(t)}</button>`).join("")}</div>`;
    }).join("");
    return `<details class="sb-drawer"><summary>add a child to ${E(n.name)}</summary>
      ${leafWhy ? `<div class="sb-note sb-warn">${E(leafWhy)}</div>` : ""}
      ${pal}</details>`;
  }

  function overview(){
    const roles = data.roles || {};
    const noScript = data.nodes.filter(n => !n.script && n.role === "controller").length;
    const missing = data.nodes.flatMap(n => n.resources.filter(r => !r.exists));
    return `<div class="sb-h">${E(data.root || "scene")}</div>
      <div class="sb-note"><b>${data.nodes.length}</b> node${data.nodes.length===1?"":"s"} ·
        ${Object.entries(roles).sort((a, b) => b[1] - a[1])
          .map(([r, c]) => `${c} ${E(countLabel(roleOf(r).label, c))}`).join(" · ")}</div>
      ${missing.length ? `<div class="sb-note sb-warn">${missing.length} node(s)
        point at a file that is not on disk: ${missing.slice(0, 4).map(m =>
        E(m.path.split("/").pop())).join(", ")}</div>` : ""}
      <div class="sb-h">building</div>
      <div class="sb-note">Click a node to inspect it. Drag a node's
        <b>children</b> port onto another node's <b>parent</b> port to reparent
        it. Every edit shows the resulting <b>.tscn</b> before it writes, and the
        previous file is kept under <b>.bgate_out/scene_backups</b>.</div>
      <details class="sb-drawer"><summary>add a node to the root</summary>
        ${(types.groups || []).map(g => `
          <div class="sb-palh">${E(g.label)}</div>
          <div class="sb-pal">${g.types.map(t =>
            `<button onclick="SceneBuild.addNode('${E(t)}','.')">${E(t)}</button>`).join("")}</div>`).join("")}
      </details>`;
  }

  /* ── edits ────────────────────────────────────────────────────────────── */
  function after(){
    return () => reload().then(() => {
      render();
      if (window.SceneView && surface === "viewport") SceneView.reload();
      if (sel) select(sel);
    });
  }

  async function step(path, body, title){
    if (busy) return;
    if (window.SceneView && SceneView.hasPending && SceneView.hasPending()){
      say(`${SceneView.pending} viewport change(s) are still staged — apply or `
          + "discard them before changing the scene's structure");
      return;
    }
    busy = true;
    const dry = await mutate(path, { body: { ...body, scene, dry_run: true },
                                     quiet: true });
    busy = false;
    if (!dry.ok){ say(dry.error); return; }
    confirmDiff(title || dry.data.summary, dry.data,
      () => mutate(path, { body: { ...body, scene } }), after());
  }

  function setProp(key){
    const el = document.getElementById(`sb-p-${key}`);
    if (!el) return;
    const value = el.value.trim();
    step("/api/scene/node/property",
         { node: sel, key, value: value === "" ? null : value },
         value === "" ? `clear ${key} on ${sel}` : `${sel}.${key} = ${value}`);
  }
  function clearProp(key){
    step("/api/scene/node/property", { node: sel, key, value: null },
         `clear ${key} on ${sel}`);
  }
  function rename(){
    const el = document.getElementById("sb-name");
    if (!el || !el.value.trim()) return;
    step("/api/scene/node/rename", { node: sel, name: el.value.trim() });
  }
  function addNode(nodeType, parent){
    step("/api/scene/node/add",
         { node_type: nodeType, name: nodeType, parent: parent || sel || "." },
         `add ${nodeType} under ${parent || sel || "."}`);
  }
  function remove(){
    const n = data.nodes.find(x => x.path === sel);
    const kids = data.nodes.filter(x =>
      (x.parent || "") === sel || (x.parent || "").startsWith(sel + "/")).length;
    step("/api/scene/unwire", { node: sel, recursive: kids > 0 },
         kids ? `delete ${sel} and ${kids} child node(s)` : `delete ${sel}`);
  }

  /* Swapping is the move this whole mode exists for, so the picker shows what
     FITS: a texture property gets textures, a stream gets audio, a script gets
     scripts. Offering every file in the project would be a list you scroll. */
  async function swapMenu(nodePath, property){
    const n = data.nodes.find(x => x.path === nodePath);
    if (!n) return;
    const want = property === "script" ? "script"
      : property === "sprite_frames" ? "frames"
      : property === "stream" ? "audio"
      : property === "texture" ? "image" : "any";
    const map = atlasMap();
    if (!map){ say("the atlas scan has not loaded"); return; }
    const kinds = { image:["texture"], frames:["sprites"], audio:["audio"],
                    script:["script"], any:["texture","sprites","audio","script"] }[want];
    const options = Object.values(map.nodes)
      .filter(a => a.kind !== "screen" && kinds.includes(a.kind) && a.exists)
      .sort((a, b) => a.label.localeCompare(b.label));
    modal(`${property || "attach"} on ${nodePath}`,
      options.length ? options.map(a => `
        <div class="sb-pick" onclick="SceneBuild.swapTo('${E(nodePath)}','${E(property)}','${E(a.id)}')">
          ${a.preview ? `<img loading="lazy" src="/api/preview?rel=${encodeURIComponent(a.preview)}" alt="">` : ""}
          <span>${E(a.label)}</span><span class="m">${E(a.path)}</span></div>`).join("")
        : `<div class="sb-note">no ${E(want)} assets in this project</div>`,
      [{ label:"cancel", fn: closeModal }]);
  }

  function swapTo(nodePath, property, assetId){
    closeModal();
    if (property === "script"){
      step("/api/scene/wire", { asset: assetId, parent: nodePath },
           `attach ${assetId.split("/").pop()} to ${nodePath}`);
      return;
    }
    step("/api/scene/node/swap",
         { node: nodePath, asset: assetId, property: property || null },
         `${nodePath}.${property || "resource"} → ${assetId.split("/").pop()}`);
  }

  function editPixels(rel){
    if (window.SpriteEdit) SpriteEdit.open(rel);
    else say("the sprite editor did not load");
  }
  function editAudio(resPath){
    if (!window.AudioLab) return say("the audio lab did not load");
    // res:// is relative to the Godot dir; the lab wants a project-relative path.
    const gd = (data.rel || "").split("/").slice(0, -2).join("/");
    AudioLab.open((gd ? gd + "/" : "") + resPath.replace("res://", ""));
  }

  /* ── modal ────────────────────────────────────────────────────────────── */
  let acts = [];
  function modal(title, body, actions){
    closeModal();
    acts = actions || [];
    const el = document.createElement("div");
    el.className = "sb-modal"; el.id = "sb-modal";
    el.innerHTML = `<div class="sb-mbox">
      <div class="sb-mhd">${E(title)}</div>
      <div class="sb-mbd">${body}</div>
      <div class="sb-mft">${acts.map((a, i) =>
        `<button class="sb-b${a.go ? " go" : ""}" onclick="SceneBuild.act(${i})">${E(a.label)}</button>`).join("")}</div>
    </div>`;
    document.body.appendChild(el);
    el.addEventListener("click", ev => { if (ev.target === el) closeModal(); });
  }
  function act(i){ const a = acts[i]; if (a && a.fn) a.fn(); }
  function closeModal(){ const el = document.getElementById("sb-modal"); if (el) el.remove(); }

  /* Every mutation goes through here, so no edit can skip the preview. */
  function confirmDiff(title, dryData, commit, done){
    modal(title,
      `<div class="sb-note">${E(dryData.summary || "")}</div>
       ${dryData.nodepath_references ? `<div class="sb-note sb-warn">
         ${dryData.nodepath_references} NodePath reference(s) elsewhere in the
         scene still name the old node — those are not rewritten.</div>` : ""}
       <div class="sb-h">the scene after this change</div>
       <div class="sb-diff">${E(tail(dryData.text, 2000))}</div>`,
      [{ label:"cancel", fn: closeModal },
       { label:"write it", go:true, fn: async () => {
           closeModal();
           const w = await commit();
           if (!w.ok) return;
           say(`${w.data.summary} · backup ${w.data.backup}`, "ok");
           if (done) done();
           try { if (window.Atlas) Atlas.badge(); } catch (e) {}
         } }]);
  }
  function tail(text, n){
    const t = String(text || "");
    return t.length > n ? "…\n" + t.slice(-n) : t;
  }

  /* ── controls ─────────────────────────────────────────────────────────── */
  function setScene(id){ scene = id; sel = null; data = null; activate(id, true); }
  function toggleRole(r){ filter.has(r) ? filter.delete(r) : filter.add(r); render(); }
  function refresh(){ activate(scene, true); }

  return { activate, render, refresh, setScene, setSurface, toggleRole, select, rename,
           setProp, clearProp, addNode, remove, swapMenu, swapTo, editPixels,
           editAudio, act, closeModal,
           get data(){ return data; }, get scene(){ return scene; } };
})();
