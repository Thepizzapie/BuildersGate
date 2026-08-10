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

  /* The project scan. Atlas owns the one shared copy — it is a whole-project
     walk of every .tscn/.gd/.tres, so it is loaded once and read from here.
     `Atlas.ensure()` is what fills it; this accessor only reads. */
  const atlasMap = () => (window.Atlas && Atlas.map) || null;

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

  const I = (name, size) =>
    window.BGIcon ? BGIcon(name, { size: size || 16 }) : "";

  let nc = null, scene = null, data = null, types = null;
  let sel = null, filter = new Set(), busy = false;
  /* SELECTION LIVES HERE, for both surfaces.
     `sel` is the primary — the one the inspector shows and a range anchors
     from — and `selection` is everything picked, primary included. Keeping the
     set here rather than in the viewport is not arbitrary: a shift-click means
     "everything between these two IN THE TREE", and the tree's order is this
     module's to know. */
  let selection = new Set(), anchor = null, pickOrder = null;
  /* The tree. `open` is which paths are expanded, `rows` is what is on screen
     in display order (the order a range means), and `els` is path -> row so a
     selection change can touch the handful of rows that changed instead of
     rebuilding 317 of them. */
  let treeOpen = new Set(), treeQ = "", treeRows = [], treeEls = new Map();
  let treeLit = new Set();      // what paintTreeSel() last marked
  let byPath = new Map(), kidsOf = new Map();
  let clipboard = null;         // { clones: [...] } from clonePlans()
  let healing = false;        // guards the render() self-heal from looping
  let repaintPending = false; // a repaint deferred until the picker closes
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
      // Same trap as the viewport's: .sb-view and .sb-canvas both declare
      // `display`, which outranks the UA sheet's [hidden]{display:none}. The
      // surface toggle sets .hidden on the one it is leaving and it stayed on
      // screen, so viewport and graph could both be mounted at once.
      ".sb-wrap [hidden], #atlas-scene [hidden]{display:none !important}",
      ".sb-wrap{display:flex;height:min(78vh,900px);border:1px solid var(--seam);border-radius:12px;overflow:hidden;background:var(--iron)}",
      ".sb-canvas{flex:1;position:relative;min-width:0}",
      ".sb-view{flex:1;position:relative;min-width:0;display:flex}",
      ".sb-view>*{flex:1;min-width:0}",
      ".sb-surf{display:flex;gap:5px;margin-right:4px}",
      ".sb-surf button{font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase;padding:5px 11px;border:1px solid var(--seam);border-radius:999px;background:none;color:var(--ash);cursor:pointer}",
      ".sb-surf button:hover{border-color:var(--ember);color:var(--bone)}",
      ".sb-surf button.on{background:var(--plate);border-color:var(--ember);color:var(--bone)}",
      ".sb-side{width:var(--sb-side-w,308px);flex:none;border-left:1px solid var(--seam);background:var(--iron);overflow-y:auto;padding:13px}",
      // A flex item, not an absolute overlay: this boundary is a real seam
      // between two panes that both want the width, so it takes its 7px from
      // the layout the way a border would.
      ".sb-split{flex:none;width:7px;margin-right:-7px;z-index:4}",
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
      ".sb-nb .sc{display:flex;gap:4px;align-items:center;color:var(--c-narrative);word-break:break-all;font-size:9px}",
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
      ".sb-prop .x{margin-left:auto;display:inline-flex;align-items:center;background:none;border:0;padding:0;color:inherit;cursor:pointer}",
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

      /* ── the scene tree ────────────────────────────────────────────────────
         Godot's primary navigation, and the thing this panel most obviously
         did not have: eleven flat layer chips standing in for a 317-node
         hierarchy. A chip cannot tell you that Desk_14 is under Props which is
         under the root, and "which of these forty is the one I mean" is the
         question you have open the entire time you are dressing a room. */
      ".sb-tree{width:var(--sb-tree-w,246px);flex:none;border-right:1px solid var(--line);background:var(--surface-1);display:flex;flex-direction:column;min-height:0}",
      ".sb-tsplit{flex:none;width:7px;margin-left:-7px;z-index:4}",
      ".sb-thd{display:flex;align-items:center;gap:5px;padding:6px 8px;border-bottom:1px solid var(--line);flex:none}",
      ".sb-thd input{flex:1;min-width:0;background:var(--bg);border:1px solid var(--line);border-radius:6px;color:var(--text);font:inherit;font-size:11px;padding:4px 7px}",
      ".sb-thd input:focus{outline:none;border-color:var(--accent)}",
      ".sb-tb{display:inline-flex;align-items:center;justify-content:center;padding:4px;background:none;border:1px solid var(--line);border-radius:6px;color:var(--text-2);cursor:pointer}",
      ".sb-tb:hover{border-color:var(--accent);color:var(--text)}",
      ".sb-tcount{font-family:var(--mono);font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--text-3);padding:4px 8px;border-bottom:1px solid var(--line-soft);flex:none}",
      ".sb-tlist{flex:1;min-height:0;overflow:auto;padding:3px 0;scrollbar-width:thin}",
      // One row, one grid: twisty, dot, name, type, eye. Indent rides on --d so
      // depth costs no extra element and no per-row stylesheet.
      ".sb-tr{display:flex;align-items:center;gap:5px;padding:2px 6px 2px calc(4px + var(--d,0) * 13px);font-size:11.5px;color:var(--text-2);cursor:default;white-space:nowrap;border-left:2px solid transparent}",
      ".sb-tr:hover{background:var(--surface-2)}",
      ".sb-tr.on{background:var(--accent-soft);color:var(--text)}",
      ".sb-tr.pri{border-left-color:var(--accent);background:var(--accent-soft);color:var(--text)}",
      ".sb-tr.off .nm{opacity:.45;text-decoration:line-through}",
      ".sb-tr .nm{overflow:hidden;text-overflow:ellipsis;min-width:0}",
      ".sb-tr .ty{font-family:var(--mono);font-size:8.5px;color:var(--text-3);margin-left:auto;padding-left:6px;flex:none}",
      ".sb-tr .dot{width:7px;height:7px;border-radius:2px;flex:none}",
      ".sb-tr .hi{color:var(--accent);font-weight:var(--fw-semi)}",
      ".sb-tr .eye{display:inline-flex;align-items:center;background:none;border:0;padding:0 2px;color:var(--text-3);cursor:pointer;flex:none;opacity:.35}",
      ".sb-tr:hover .eye,.sb-tr.off .eye{opacity:1}",
      ".sb-tr .eye:hover{color:var(--accent)}",
      ".sb-tr .eye .bgi .e{stroke:currentColor}",
      // The twisty is drawn, not typed. A unicode triangle resolves out of whatever symbol
      // font the OS falls back to — the exact drift icons.js exists to end —
      // and a triangle this small is three borders.
      ".sb-tw{width:12px;height:12px;flex:none;background:none;border:0;padding:0;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}",
      ".sb-tw:disabled{cursor:default}",
      ".sb-tw::before{content:'';width:0;height:0;border-left:4.5px solid var(--text-3);border-top:3.5px solid transparent;border-bottom:3.5px solid transparent;transition:transform .1s}",
      ".sb-tw:disabled::before{border-left-color:transparent}",
      ".sb-tw.open::before{transform:rotate(90deg)}",
      ".sb-tw:hover::before{border-left-color:var(--accent)}",
      ".sb-tnone{font-size:11px;color:var(--text-3);padding:10px 9px;line-height:1.5}",
      // The batch inspector. A selection of twenty has no `position` field to
      // show, and pretending otherwise is how one node gets edited instead.
      ".sb-many{border:1px solid var(--accent-line);border-radius:8px;padding:8px 10px;margin-bottom:10px;background:var(--surface-1)}",
      ".sb-many .hd{font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);margin-bottom:5px}",
      ".sb-many ul{margin:6px 0 0;padding-left:16px;font-size:11px;color:var(--text-2);line-height:1.6}",
      ".sb-keys{font-family:var(--mono);font-size:9.5px;color:var(--text-3);line-height:1.7}",
      ".sb-keys b{color:var(--text-2);font-weight:400}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── load ─────────────────────────────────────────────────────────────── */
  async function activate(sceneId, force){
    injectStyle();
    const host = document.getElementById("atlas-scene");
    if (!host) return;
    /* THE SHARED SCAN HAS TO BE IN HAND BEFORE ANYTHING READS SCREENS FROM IT.
       Atlas.map is populated by whoever loads it first — on startup that is a
       1200ms-deferred badge() call, and the scan itself walks the whole
       project. Open this mode inside that window and atlasMap() is still null,
       so bestScene() has nothing to choose from and the picker falls through to
       its one-option fallback: the current scene and nothing else. It never
       recovers, because the map arriving fires no re-render — the panel only
       repaints on user action, and the one control the user would reach for is
       the picker that is now stuck. Reported as "the scene is locked to the
       title page" on a project whose declared boot scene is the title.
       Atlas.ensure() is memoised and single-flight, so this is one shared scan,
       not a per-open cost. Failure is swallowed deliberately: a dead scan
       should leave the old behaviour, not break the panel. */
    if (window.Atlas && Atlas.ensure && !atlasMap()){
      if (!data) host.innerHTML =
        `<div class="empty" style="padding:40px">scanning the project…</div>`;
      try { await Atlas.ensure(); } catch (e) {}
    }
    if (sceneId) scene = sceneId;
    if (!scene) scene = bestScene();
    if (!scene){
      host.innerHTML = `<div class="empty" style="padding:40px">no scene to build - Atlas found no .tscn</div>`;
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
    // The project's declared boot scene beats any heuristic — it is the one
    // scene the author has already told us matters.
    const main = map && map.main_scene;
    if (main && screens.some(s => s.id === main)) return main;
    const weight = {};
    ((map && map.edges) || []).forEach(e => {
      weight[e.from] = (weight[e.from] || 0) + 1;
    });
    // "Most edges" picks a QA fixture on any project that has one — those
    // scenes exist precisely to reference everything at once. Sideline them
    // rather than let asset count speak for importance.
    const side = s => /^res:\/\/(tests?|qa)\//i.test(s.id)
                   || /(_proof|_test|_sandbox|_demo)\.tscn$/i.test(s.id);
    return screens.slice().sort(
      (a, b) => (side(a) - side(b)) || (weight[b.id] || 0) - (weight[a.id] || 0)
    )[0].id;
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
    indexTree();
    return true;
  }

  /* ── the scene tree ───────────────────────────────────────────────────────
   * The outline arrives as a flat list with parent links, which is the right
   * shape to send and the wrong shape to walk. Index it once per load: 317
   * nodes rebuilt on every keystroke of the filter would make typing feel like
   * the panel is thinking.
   */
  function indexTree(){
    byPath = new Map(); kidsOf = new Map();
    const nodes = (data && data.nodes) || [];
    nodes.forEach(n => { byPath.set(n.path, n); kidsOf.set(n.path, []); });
    nodes.forEach(n => {
      if (n.parent === null || n.parent === undefined) return;
      const bucket = kidsOf.get(n.parent === "." ? "." : n.parent);
      if (bucket) bucket.push(n);
    });
    /* HOW MUCH TO OPEN ON ARRIVAL, and why it is not a fixed depth.
       Root-only shows the layers and nothing about them. Root-plus-layers is
       right on a hand-built scene and catastrophic on a dressed one: this
       project's floor has eleven layers and five hundred props under them, so
       "open the layers" is five hundred rows of identical desks before you
       have asked anything. So open the layers only while the whole scene still
       fits in a glance, and otherwise leave the operator the eleven rows that
       actually describe it. */
    if (!treeOpen.size){
      treeOpen.add(".");
      if (nodes.length <= 60)
        (kidsOf.get(".") || []).forEach(n => treeOpen.add(n.path));
    }
    // A path that no longer exists must not keep a branch pinned open.
    [...treeOpen].forEach(p => { if (!byPath.has(p)) treeOpen.delete(p); });
    [...selection].forEach(p => { if (!byPath.has(p)) selection.delete(p); });
    if (sel && !byPath.has(sel)) sel = null;
  }

  const depthOf = p => p === "." ? 0 : p.split("/").length;

  /* Highlight the match inside the name. Both sides are escaped before the
     search, so the indices line up on the escaped string and nothing that came
     out of the file can reach innerHTML unescaped. */
  function hl(text, q){
    const t = E(text);
    if (!q) return t;
    const needle = E(q);
    const at = t.toLowerCase().indexOf(needle.toLowerCase());
    if (at < 0) return t;
    return t.slice(0, at) + `<b class="hi">` + t.slice(at, at + needle.length)
         + `</b>` + t.slice(at + needle.length);
  }

  /* Is this node visible IN THE GAME — the real `visible` property, staged
     edits included. The layer strip's eyes are a view filter and a different
     thing entirely; this one changes the scene. */
  function nodeVisible(path){
    const drawn = window.SceneView && SceneView.list;
    if (drawn){
      const it = drawn.items.find(i => i.path === path);
      if (it) return it.visible !== false;
    }
    const n = byPath.get(path);
    return !n || (n.properties || {}).visible !== "false";
  }

  function treeRow(n, depth, kidCount, open, q){
    const info = roleOf(n.role);
    const on = selection.has(n.path);
    const vis = nodeVisible(n.path);
    const label = n.path === "." ? (data.root || "root") : n.name;
    return `<div class="sb-tr${on ? " on" : ""}${n.path === sel ? " pri" : ""}${
      vis ? "" : " off"}" data-path="${E(n.path)}" style="--d:${depth}"
      role="treeitem" aria-level="${depth + 1}" aria-selected="${on}"${
      kidCount ? ` aria-expanded="${open}"` : ""}>
      <button class="sb-tw${open ? " open" : ""}" data-tw="${E(n.path)}"${
        kidCount ? "" : " disabled"} tabindex="-1"
        aria-label="${open ? "Collapse" : "Expand"} ${E(label)}"
        title="${kidCount ? `${kidCount} child node(s)` : "no children"}"></button>
      <i class="dot" style="background:${info.c}" title="${E(info.label)}"></i>
      <span class="nm" title="${E(n.path)}">${hl(label, q)}</span>
      <span class="ty">${E(n.type === "(instance)" ? "instance" : (n.type || ""))}</span>
      <button class="eye" data-eye="${E(n.path)}" tabindex="-1"
        title="${vis ? "Hide" : "Show"} ${E(label)} in the game - staged, like every other edit"
        aria-label="${vis ? "Hide" : "Show"} ${E(label)}">${
        I(vis ? "visible" : "hidden", 12)}</button>
    </div>`;
  }

  function treeHTML(){
    if (!data) return "";
    const q = treeQ.trim().toLowerCase();
    /* A filter REVEALS, it does not extract. Showing only the matches throws
       away where they are, which on a scene with nine nodes called `Body` is
       the only thing that tells them apart — so the ancestors of every hit
       come too, and everything on the path is forced open. */
    let keep = null;
    if (q){
      keep = new Set();
      (data.nodes || []).forEach(n => {
        const hay = `${n.name} ${n.type || ""}`.toLowerCase();
        if (!hay.includes(q)) return;
        keep.add(n.path);
        let p = n.parent;
        while (p !== null && p !== undefined && !keep.has(p)){
          keep.add(p);
          const up = byPath.get(p);
          p = up ? up.parent : null;
        }
      });
    }
    treeRows = [];
    const out = [];
    const walk = (n, depth) => {
      if (keep && !keep.has(n.path)) return;
      const kids = (kidsOf.get(n.path) || [])
        .filter(k => !keep || keep.has(k.path));
      const open = keep ? true : treeOpen.has(n.path);
      treeRows.push(n.path);
      out.push(treeRow(n, depth, kids.length, open, q));
      if (open) kids.forEach(k => walk(k, depth + 1));
    };
    const root = byPath.get(".");
    if (root) walk(root, 0);
    if (!out.length) return `<div class="sb-tnone">nothing matches “${E(treeQ)}”.</div>`;
    return out.join("");
  }

  function renderTree(){
    const listEl = document.getElementById("sb-tlist");
    if (!listEl) return;
    const top = listEl.scrollTop;
    listEl.innerHTML = treeHTML();
    treeEls = new Map();
    listEl.querySelectorAll(".sb-tr").forEach(el =>
      treeEls.set(el.dataset.path, el));
    treeLit = new Set();
    paintTreeSel();
    listEl.scrollTop = top;
    const count = document.getElementById("sb-tcount");
    if (count) count.textContent = treeQ.trim()
      ? `${treeRows.length} shown of ${(data.nodes || []).length}`
      : `${(data.nodes || []).length} nodes · ${treeRows.length} open`;
  }

  /* SELECTION DOES NOT REBUILD THE TREE. 317 rows of innerHTML per click is
     how a tree panel becomes the slowest thing in the editor; only the rows
     that changed state are touched. */
  function paintTreeSel(){
    const now = new Set([...selection].filter(p => treeEls.has(p)));
    treeLit.forEach(p => {
      if (now.has(p)) return;
      const el = treeEls.get(p);
      if (el){ el.classList.remove("on"); el.setAttribute("aria-selected", "false"); }
    });
    now.forEach(p => {
      const el = treeEls.get(p);
      if (el){ el.classList.add("on"); el.setAttribute("aria-selected", "true"); }
    });
    treeLit = now;
    treeEls.forEach((el, p) => el.classList.toggle("pri", p === sel));
  }

  function paintTreeVis(path){
    const el = treeEls.get(path);
    if (!el) return;
    const vis = nodeVisible(path);
    el.classList.toggle("off", !vis);
    const b = el.querySelector(".eye");
    if (!b) return;
    const label = el.querySelector(".nm");
    const name = label ? label.textContent : path;
    b.innerHTML = I(vis ? "visible" : "hidden", 12);
    b.title = `${vis ? "Hide" : "Show"} ${name} in the game - staged, like `
            + "every other edit";
    b.setAttribute("aria-label", `${vis ? "Hide" : "Show"} ${name}`);
  }

  /* Bring a node into view, opening whatever is hiding it. Called when the
     selection arrives from the viewport, which is the case the tree exists to
     answer: "I clicked that thing - where does it live?" */
  function revealTree(path){
    if (!path || !byPath.has(path)) return;
    let grew = false;
    let p = byPath.get(path).parent;
    while (p !== null && p !== undefined){
      if (!treeOpen.has(p)){ treeOpen.add(p); grew = true; }
      const up = byPath.get(p);
      p = up ? up.parent : null;
    }
    if (grew) renderTree();
    const el = treeEls.get(path);
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  }

  function treeToggle(path){
    treeOpen.has(path) ? treeOpen.delete(path) : treeOpen.add(path);
    renderTree();
  }
  function treeExpandAll(){
    (data.nodes || []).forEach(n => {
      if ((kidsOf.get(n.path) || []).length) treeOpen.add(n.path);
    });
    renderTree();
  }
  function treeCollapseAll(){
    treeOpen = new Set(["."]);
    renderTree();
  }
  function treeFilter(v){
    treeQ = String(v || "");
    renderTree();
  }
  function treeFocus(){
    const el = document.getElementById("sb-tq");
    if (el){ el.focus(); el.select(); }
  }

  /* One listener for the whole tree, not one per row. 317 inline handlers is
     317 closures rebuilt on every expand. */
  function bindTree(root){
    const listEl = root.querySelector("#sb-tlist");
    if (!listEl || listEl.dataset.bound) return;
    listEl.dataset.bound = "1";
    listEl.addEventListener("click", ev => {
      const tw = ev.target.closest("[data-tw]");
      if (tw){ ev.stopPropagation(); treeToggle(tw.dataset.tw); return; }
      const eye = ev.target.closest("[data-eye]");
      if (eye){ ev.stopPropagation(); toggleVisible(eye.dataset.eye); return; }
      const row = ev.target.closest(".sb-tr");
      if (!row) return;
      pick(row.dataset.path, ev.shiftKey ? "range"
        : (ev.ctrlKey || ev.metaKey) ? "toggle" : "set");
    });
    listEl.addEventListener("dblclick", ev => {
      const row = ev.target.closest(".sb-tr");
      if (row) treeToggle(row.dataset.path);
    });
  }

  /* The eye writes the node's real `visible`. In the viewport it stages with
     everything else; on the graph surface — where nothing is staged — it goes
     through the same diff-then-write every other structural edit does. */
  function toggleVisible(path){
    const want = !nodeVisible(path);
    if (surface === "viewport" && window.SceneView && SceneView.list){
      SceneView.stageVisible(path, want);
      paintTreeVis(path);
      return;
    }
    step("/api/scene/node/property",
         { node: path, key: "visible", value: want ? "true" : "false" },
         `${want ? "show" : "hide"} ${path}`);
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
      ${n.script ? `<div class="sc">${I("tech", 11)}<span>${E(n.script.split("/").pop())}</span></div>` : ""}
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
    /* NEVER REPAINT UNDERNEATH AN OPEN DROPDOWN. render() replaces the whole
       panel with innerHTML, which DESTROYS the <select> node. If that lands
       while the user has the list open — and the dashboard polls on several
       timers — the native popup is torn off its element and vanishes. What the
       user sees is a picker that will not open at all, or one that snaps shut
       the moment they scroll it. With 64 scenes the list is long enough that
       scrolling is required, so the bug hits every single attempt to change
       scene, and looks exactly like "clicking does nothing".
       An open <select> holds focus, so activeElement is the test. Defer the
       repaint to blur instead of dropping it, or the panel goes stale. */
    const sel = host.querySelector("select");
    if (sel && document.activeElement === sel){
      if (!repaintPending){
        repaintPending = true;
        sel.addEventListener("blur", () => { repaintPending = false; render(); },
                             { once: true });
      }
      return;
    }
    repaintPending = false;
    const map = atlasMap();
    const screens = (map && map.screens) || [];
    const roles = data.roles || {};
    /* LAST-DITCH SELF-HEAL. Every caller is supposed to have the scan in hand
       before it gets here, but if any path ever renders without one the picker
       silently becomes a single dead option — the current scene, unchangeable,
       with no error anywhere to explain it. Refetch once and repaint rather
       than leave the user staring at a control that does nothing. */
    if (!screens.length && !healing){
      healing = true;
      const done = () => { healing = false; render(); };
      if (window.Atlas && Atlas.ensure) Atlas.ensure().then(done).catch(done);
      else healing = false;
    }

    host.innerHTML = `
      <div class="sb-bar">
        <div class="sb-surf">
          <button class="${surface==='viewport'?'on':''}" onclick="SceneBuild.setSurface('viewport')">viewport</button>
          <button class="${surface==='graph'?'on':''}" onclick="SceneBuild.setSurface('graph')">graph</button>
        </div>
        <select class="sb-in" onchange="SceneBuild.setScene(this.value)">
          ${screens.map(s => `<option value="${E(s.id)}"${s.id===scene?" selected":""}>${E(s.label)}</option>`).join("")
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
        <!-- The hierarchy, on the left, where the thing you navigate with
             lives. Filter reveals in place; the eye writes the node's real
             visible property and stages like everything else. -->
        <div class="sb-tree" id="sb-tree" role="tree" aria-label="Scene tree">
          <div class="sb-thd">
            <input id="sb-tq" type="search" value="${E(treeQ)}"
                   placeholder="filter nodes - Ctrl+F"
                   aria-label="Filter the scene tree"
                   oninput="SceneBuild.treeFilter(this.value)">
            <button class="sb-tb" onclick="SceneBuild.treeCollapseAll()"
                    title="Collapse back to the layers" aria-label="Collapse the tree"
                    >${I("collapse_all", 14)}</button>
          </div>
          <div class="sb-tcount" id="sb-tcount"></div>
          <div class="sb-tlist" id="sb-tlist"></div>
        </div>
        <div class="split sb-tsplit" data-split="sb-tree" data-split-var="--sb-tree-w"
             data-split-on=".sb-wrap" data-split-pane="#sb-tree" data-split-edge="start"
             data-split-min="170" data-split-max="46%"
             aria-label="Resize the scene tree - arrow keys adjust, Home resets"></div>
        <div class="sb-view" id="sb-view" ${surface==='viewport'?'':'hidden'}></div>
        <div class="sb-canvas" id="sb-canvas" ${surface==='graph'?'':'hidden'}></div>
        <!-- The inspector's edge. 308px was chosen for the property rows, but
             this panel also holds node names and resource paths, which are as
             long as the project makes them. Dragging is the only way to read
             one without truncation. -->
        <div class="split sb-split" data-split="sb-side" data-split-var="--sb-side-w"
             data-split-on=".sb-wrap" data-split-pane="#sb-side" data-split-edge="end"
             data-split-min="220" data-split-max="60%"
             aria-label="Resize the inspector - arrow keys adjust, Home resets"></div>
        <div class="sb-side" id="sb-side"></div>
      </div>`;

    // The handle was just rebuilt with the rest of the panel, so it needs
    // binding again. init() is idempotent per element — re-running it over a
    // scope that still holds live handles is free.
    if (window.Split) Split.init(host);
    bindTree(host);
    renderTree();
    installKeys();

    if (surface === "graph"){
      const { nodes, edges } = build();
      nc = new NodeCanvas(document.getElementById("sb-canvas"), {
        nodes, edges, renderBody, accent: "var(--accent)",
        // The canvas runs its own ctrl/shift multi-select and marquee, so read
        // the set it settled on rather than the one node it names — otherwise a
        // box-select on the graph reduces to whichever node happened to be last.
        onSelect: node => {
          if (selecting) return;
          const ids = nc ? nc.selected() : (node ? [node.id] : []);
          selection = new Set(ids);
          sel = node ? node.id : (ids[ids.length - 1] || null);
          anchor = sel;
          syncSelection(false);
        },
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

  /* ── selection ────────────────────────────────────────────────────────────
   * ONE authority for both surfaces and the tree. Every click anywhere lands
   * here with what the modifier meant, this resolves it, and then it pushes the
   * answer out. Nobody echoes anybody: the viewport's setter and the tree's
   * painter are both state-only.
   */
  /* `order` lets a surface say what "between these two" means ON IT. The layer
     strip is eleven chips in file order; ranging across it through the TREE's
     order would sweep in the 232 props sitting between Characters and Lights,
     which is right in the tree and nonsense in a row of eleven. */
  function pick(path, mode, order){
    if (!data) return;
    mode = mode || "set";
    pickOrder = order || null;
    if (!path){
      selection.clear(); sel = null; anchor = null;
    } else if (!byPath.has(path)){
      // A path the outline does not know (something inside an instance the
      // viewport opened). Show it, but do not pretend it is a tree row.
      selection.clear(); selection.add(path); sel = path; anchor = null;
    } else if (mode === "toggle"){
      if (selection.has(path) && selection.size > 1){
        selection.delete(path);
        if (sel === path) sel = [...selection][selection.size - 1] || null;
      } else {
        selection.add(path); sel = path;
      }
      anchor = sel;
    } else if (mode === "range" && anchor && anchor !== path){
      const order = rangeOrder(anchor, path);
      const a = order.indexOf(anchor), b = order.indexOf(path);
      selection.clear();
      if (a < 0 || b < 0){ selection.add(path); anchor = path; }
      else for (let i = Math.min(a, b); i <= Math.max(a, b); i++)
        selection.add(order[i]);
      sel = path;
    } else {
      selection.clear(); selection.add(path); sel = path; anchor = path;
    }
    syncSelection(path && mode !== "range");
  }

  /* What "between these two" means. The rows on screen, when both ends are on
     screen — that is what the operator drew a line through. When one end is
     inside a collapsed branch, the file's own order is the only honest answer;
     tree order does not exist for a row that is not being shown. */
  function rangeOrder(a, b){
    if (pickOrder && pickOrder.indexOf(a) >= 0 && pickOrder.indexOf(b) >= 0)
      return pickOrder;
    if (treeRows.indexOf(a) >= 0 && treeRows.indexOf(b) >= 0) return treeRows;
    return (data.nodes || []).map(n => n.path);
  }

  function syncSelection(reveal){
    if (selecting) return;
    selecting = true;
    try {
      if (window.SceneView && typeof SceneView.setSelection === "function")
        try {
          SceneView.setSelection([...selection], sel);
          // The layer strip shows selection too, so it has to hear about it.
          if (SceneView.repaintLayers) SceneView.repaintLayers();
        } catch (e) {}
      if (nc){
        try {
          if (selection.size) nc.selectMany([...selection], false);
          else nc.select(null);
        } catch (e) {}
      }
      paintTreeSel();
      if (reveal && sel) revealTree(sel);
      side();
    } finally { selecting = false; }
  }

  /* The old single-select entry point. Node cards and external callers still
     use it, and one click meaning "just this one" is exactly `set`. */
  function select(path){ pick(path, "set"); }

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
          + `you set on it is what the script starts from - not what you see `
          + `in play.`
        : `<b>${E(n.name)}</b> has no children in this file and no script here `
          + `or above it says what fills it. Whatever appears inside it comes `
          + `from code somewhere else in the project.` };
  }

  /* WHAT A SELECTION OF TWENTY CAN AND CANNOT BE TOLD TO DO.
   *
   * The single-node inspector is six property fields and a name box, and none
   * of those mean anything across a mixed selection — a `position` field over
   * twenty nodes either edits one of them or twenty to the same number, and
   * both are wrong. So the batch panel offers only what genuinely applies to
   * all of them at once, and says which of the batch is in the way of the rest
   * instead of quietly acting on the remainder. */
  function many(){
    const nodes = [...selection].map(p => byPath.get(p)).filter(Boolean);
    const kinds = {};
    nodes.forEach(n => {
      const k = n.type === "(instance)" ? "instance" : (n.type || "node");
      kinds[k] = (kinds[k] || 0) + 1;
    });
    const rootIn = nodes.some(n => n.path === ".");
    const viewport = surface === "viewport";
    return `<div class="sb-many">
        <div class="hd">${nodes.length} nodes selected</div>
        <div class="sb-note" style="margin:0">${Object.entries(kinds)
          .sort((a, b) => b[1] - a[1]).slice(0, 6)
          .map(([k, c]) => `${c} × <b>${E(k)}</b>`).join(" · ")}</div>
        ${rootIn ? `<div class="sb-note sb-warn" style="margin:6px 0 0">The root
          is in this selection. Delete and duplicate refuse a selection that
          holds it rather than doing the rest.</div>` : ""}
      </div>
      <div class="sb-h">the whole selection</div>
      ${viewport ? `
        <button class="sb-b wide" onclick="SceneBuild.setSelectionVisible(false)">hide all ${nodes.length}</button>
        <button class="sb-b wide" onclick="SceneBuild.setSelectionVisible(true)">show all ${nodes.length}</button>
        <button class="sb-b wide" onclick="SceneView.duplicateSelected()">duplicate all ${nodes.length}</button>
        <button class="sb-b wide" onclick="SceneBuild.copySelection()">copy all ${nodes.length}</button>
        <button class="sb-b bad wide" onclick="SceneView.removeSelected()">delete all ${nodes.length}</button>
        <div class="sb-note">Every one of these stages — nothing reaches the
          file until <b>apply</b>, which asks once and keeps a backup. Hiding
          here sets the node's real <b>visible</b>; the eyes on the layer strip
          are a view filter and change nothing.</div>`
      : `<div class="sb-note">Batch edits run on the <b>viewport</b> surface,
          where they can be staged and previewed. Switch to it to duplicate,
          delete or nudge this selection.</div>`}
      <div class="sb-h">one at a time</div>
      <div class="sb-note">Properties, name, script and resources are per-node.
        Click a single node to edit them.</div>
      ${shortcutHelp()}`;
  }

  function shortcutHelp(){
    return `<details class="sb-drawer"><summary>keyboard</summary>
      <div class="sb-keys">
        <b>Del</b> delete · <b>Ctrl+D</b> duplicate<br>
        <b>Ctrl+C / Ctrl+V</b> copy, paste under the selection<br>
        <b>Ctrl+Z / Ctrl+Shift+Z</b> undo, redo<br>
        <b>F</b> frame the selection · <b>Esc</b> deselect<br>
        <b>Arrows</b> nudge 1px · <b>Shift+Arrows</b> one snap step<br>
        <b>Ctrl+F</b> jump to the tree filter<br>
        <b>Shift-click</b> a range · <b>Ctrl-click</b> add or remove one
      </div></details>`;
  }

  function side(){
    const el = document.getElementById("sb-side");
    if (!el) return;
    if (selection.size > 1) return el.innerHTML = many();
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
          <button class="x" title="clear ${E(k)}" aria-label="Clear ${E(k)}" onclick="SceneBuild.clearProp('${E(k)}')">${I("delete", 12)}</button></div>`).join("")}` : "");

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
        ? `<b>${E(n.script)}</b>` : `<span class="sb-warn">no script - this node does nothing on its own</span>`}
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
    TileMapLayer: "Tiles live in this layer's own data, not as child nodes - a " +
      "node you add here will NOT become a tile. Put objects in a Node2D " +
      "container beside this layer, so each one can be selected and named.",
    TileMap: "Tiles live in this node's own data, not as child nodes - a node " +
      "you add here will NOT become a tile. Put objects in a Node2D container " +
      "beside it.",
    CollisionShape2D: "A collision shape is a leaf - its shape is a resource, " +
      "not a child. Add siblings under the body instead.",
    CollisionPolygon2D: "A collision polygon is a leaf - add siblings under " +
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
      <div class="sb-note">Click a node in the tree or the picture to inspect
        it — <b>shift-click</b> a range, <b>ctrl-click</b> to add one. Drag a
        node's <b>children</b> port onto another node's <b>parent</b> port to
        reparent it. Every edit shows the resulting <b>.tscn</b> before it
        writes, and the previous file is kept under
        <b>.bgate_out/scene_backups</b>.</div>
      ${shortcutHelp()}
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
      // The file changed under the selection: drop whatever no longer exists
      // and push what survived back out, rather than collapsing a batch to one.
      [...selection].forEach(p => { if (!byPath.has(p)) selection.delete(p); });
      if (sel && !byPath.has(sel)) sel = [...selection][selection.size - 1] || null;
      syncSelection(false);
    });
  }

  async function step(path, body, title){
    if (busy) return;
    if (window.SceneView && SceneView.hasPending && SceneView.hasPending()){
      say(`${SceneView.pending} viewport change(s) are still staged - apply or `
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

  /* Wiring an asset into a scene, from outside this mode.
   *
   * This lived on the Atlas GRAPH, which is gone. The Asset Library's "wire"
   * action is the caller: it has an asset and no scene, so the scene has to be
   * chosen first — which is the one thing the rest of this module never does,
   * because everything else here operates on the scene already open.
   *
   * The target scene is therefore passed explicitly rather than going through
   * `step()`, which stamps the module's current `scene` onto every body. Wiring
   * into a scene you are not looking at is the normal case here, and switching
   * the editor to it first would preview the write against the wrong file. */
  async function wireMenu(assetId){
    const d = await readJSON(
      `/api/scene/wirable?asset=${encodeURIComponent(assetId)}`, null);
    if (!d || d.__error){ say((d && d.__error) || "could not list scenes"); return; }
    const map = atlasMap();
    const label = (map && map.nodes[assetId] && map.nodes[assetId].label) || assetId;
    modal(`wire ${label} into…`,
      (d.scenes || []).map(s => `
        <div class="sb-pick"${s.has_asset ? ' style="opacity:.5;cursor:default"'
          : ` onclick="SceneBuild.wireTo('${E(assetId)}','${E(s.scene)}')"`}>
          <span>${E(s.label)}</span>
          <span class="m">${s.nodes} nodes${s.has_asset ? " · already wired" : ""}</span>
        </div>`).join("")
        || `<div class="sb-note">no scenes found</div>`,
      [{ label: "cancel", fn: closeModal }]);
  }

  async function wireTo(assetId, sceneId){
    closeModal();
    const dry = await mutate("/api/scene/wire", {
      body: { scene: sceneId, asset: assetId, dry_run: true }, quiet: true });
    if (!dry.ok){ say(dry.error); return; }
    const map = atlasMap();
    const name = id => (map && map.nodes[id] && map.nodes[id].label) || id;
    confirmDiff(`wire ${name(assetId)} into ${name(sceneId)}`, dry.data,
      () => mutate("/api/scene/wire", { body: { scene: sceneId, asset: assetId } }),
      // Land the operator on what they just changed. Already looking at it —
      // reload in place; otherwise switch, because a confirmed write into a
      // scene the editor does not show is indistinguishable from nothing.
      () => { if (sceneId === scene) refresh(); else setScene(sceneId); });
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
           // The engine backdrop is a photo of the scene BEFORE this write. If
           // it is up, re-take it — otherwise the swap lands in the file and
           // the picture keeps showing the old art, which is indistinguishable
           // from the swap having failed.
           try {
             if (window.SceneView && SceneView.reshoot) SceneView.reshoot();
           } catch (e) {}
         } }]);
  }
  function tail(text, n){
    const t = String(text || "");
    return t.length > n ? "…\n" + t.slice(-n) : t;
  }

  /* ── duplicate / copy / paste ─────────────────────────────────────────────
   * The plan for re-creating a node and its subtree. Built here because the
   * outline — types, parent links, properties, scripts, resources — is what
   * this module holds; run by SceneView, because that is where a not-yet-
   * written thing gets a ghost, a drag and a place in the one confirmation.
   *
   * THE WRITER'S VALUE WHITELIST IS MIRRORED, NOT GUESSED AT. `_prop_value` in
   * bgate_core/scenewire.py accepts a deliberately narrow set — a property
   * writer that emits anything is a property writer that corrupts a .tscn —
   * and a duplicate that silently drops a `polygon` is a duplicate that is
   * wrong in a way you find out about in the game. So the same set is tested
   * here and everything outside it is NAMED before the write.
   * Keep in step with tests/test_spriteedit_api.py, which pins the two
   * together.
   */
  const WRITABLE_VALUE = new RegExp(
    "^(true|false|-?\\d+(\\.\\d+)?|\"[^\"\\\\\\n]*\"|&\"[^\"\\\\\\n]*\"|"
    + "(Vector2|Vector2i|Vector3|Color|Rect2)\\([-\\d\\s.,]*\\)|"
    + "(Ext|Sub)Resource\\(\"[^\"\\\\\\n]+\"\\)|NodePath\\(\"[^\"\\\\\\n]*\"\\))$");

  /* A duplicate is one write per node plus one per resource. Past this it stops
     being an edit and becomes a batch job against a live file, with no progress
     and no way to stop it halfway. Refuse and say the number. */
  const CLONE_MAX = 60;

  function clonePlans(paths){
    if (!data) return { error: "the scene has not loaded" };
    const picked = (paths || []).map(p => byPath.get(p)).filter(Boolean);
    if (!picked.length) return { error: "select a node first" };
    if (picked.some(n => n.path === "."))
      return { error: "the root node cannot be duplicated - duplicate what is "
                      + "under it, or copy the scene file" };
    // A node whose ancestor is also selected comes along inside it. Cloning
    // both would put a second copy inside the first.
    const roots = picked.filter(n =>
      !picked.some(o => o !== n && n.path.startsWith(o.path + "/")));
    const clones = [];
    const broken = [];
    let total = 0;
    for (const n of roots){
      const plan = [], dropped = [];
      const walk = (node, rel, parentRel) => {
        const inst = (node.resources || [])
          .find(r => r.property === "instance");
        // An instanced node whose source the outline could not name has no
        // type to add and no scene to wire. Refuse rather than emit
        // `add_node(type="(instance)")`, which invents a node that is not a node.
        if (node.instance && !inst){ broken.push(node.path); return; }
        /* AN OVERRIDE IS NOT A NODE.
           A block with a parent inside an instance and no `type` —
           `[node name="Art" parent="Prop_00" index="0"]` — is Godot RE-SETTING
           a property on something that already exists inside prop.tscn. It has
           no type to create and no scene to wire, and `add_node` given a type
           would put a SECOND Art beside the instance's own. There is no safe
           endpoint that appends an override block, so the copy leaves them off
           and says so before the write rather than after it. */
        if (!inst && !node.type){
          dropped.push(`${node.name} (override inside the instance)`);
          return;                    // and everything under it is more of them
        }
        const props = {};
        Object.entries(node.properties || {}).forEach(([k, v]) => {
          if (k === "script") return;
          // The root's position is written from where the ghost was dropped,
          // so carrying the source's would be a wasted request and a flicker.
          if (parentRel === null && k === "position") return;
          if (WRITABLE_VALUE.test(String(v))) props[k] = v;
          else dropped.push(`${node.name}.${k}`);
        });
        plan.push({
          rel, parentRel, name: node.name, type: node.type,
          instance: inst ? inst.path : null,
          script: node.script || "",
          properties: props,
          resources: (node.resources || [])
            .filter(r => r.property !== "instance" && r.property !== "script")
            .map(r => ({ property: r.property, path: r.path })),
        });
        (kidsOf.get(node.path) || []).forEach(kid =>
          walk(kid, rel === "" ? kid.name : `${rel}/${kid.name}`, rel));
      };
      walk(n, "", null);
      total += plan.length;
      clones.push({ path: n.path, name: n.name, parent: n.parent || ".",
                    plan, dropped });
    }
    if (broken.length)
      return { error: `${broken[0]} is an instanced scene whose source this `
        + "file does not name - there is nothing to make a second copy of. "
        + "Nothing was staged." };
    if (total > CLONE_MAX)
      return { error: `that is ${total} node(s) - copying it would be ${total} `
        + `separate writes to the file. Duplicate ${CLONE_MAX} nodes or fewer, `
        + "or instance a .tscn with `place` instead." };
    return { clones };
  }

  /* The node's real `visible`, across the whole selection, staged. Distinct
     from the layer strip's eyes, which are a view filter and write nothing. */
  function setSelectionVisible(want){
    if (surface !== "viewport" || !window.SceneView || !SceneView.list){
      say("that stages a change, which the viewport surface owns - switch to it");
      return;
    }
    SceneView.setVisibleBatch([...selection], want);
    [...selection].forEach(paintTreeVis);
  }

  function copySelection(){
    const r = clonePlans([...selection]);
    if (r.error){ say(r.error); return; }
    clipboard = r;
    const n = r.clones.reduce((a, c) => a + c.plan.length, 0);
    say(`copied ${r.clones.length} node(s)${
      n > r.clones.length ? ` and ${n - r.clones.length} child node(s)` : ""}`,
      "ok");
  }

  /* Paste lands UNDER the selection, the way Godot does — the selected node is
     where you are pointing, and pasting a chair beside the chair you selected
     rather than into the room you selected is the wrong half of the time. */
  function pasteClipboard(){
    if (!clipboard){ say("nothing copied yet - Ctrl+C first"); return; }
    if (surface !== "viewport"){
      say("paste stages a change, which the viewport surface owns - switch to it");
      return;
    }
    if (!window.SceneView || !SceneView.list){ say("the viewport is not open"); return; }
    const parent = sel && byPath.has(sel) ? sel : ".";
    SceneView.pasteClones(clipboard.clones, true, parent);
  }

  /* ── keyboard ─────────────────────────────────────────────────────────────
   * What makes a thing feel like an editor rather than a form.
   *
   * SCOPE IS THE WHOLE PROBLEM HERE, not the bindings. One window listener,
   * refusing to act unless the Atlas view is the visible one, the scene mode is
   * the visible mode inside it, the focus is not in a field, and no modal is
   * up. Getting that wrong deletes a node while someone is renaming one, and it
   * only takes one to make the shortcuts something people ask to turn off.
   */
  function typingIn(el){
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = (el.tagName || "").toUpperCase();
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
        || tag === "OPTION" || tag === "BUTTON";
  }

  function keysLive(){
    const view = document.getElementById("view-atlas");
    if (!view || !view.classList.contains("active")) return false;
    const pane = document.getElementById("atlas-scene");
    if (!pane || pane.hidden) return false;
    // A dialog is up: the diff preview, or ask.js's confirm. Whatever the key
    // means, it does not mean it to the scene behind them.
    if (document.getElementById("sb-modal")) return false;
    if (document.querySelector(".ask-scrim")) return false;
    return true;
  }

  const stageOnly = () => {
    if (surface === "viewport" && window.SceneView && SceneView.list) return true;
    say("that stages a change, which the viewport surface owns - switch to it");
    return false;
  };

  function installKeys(){
    if (installKeys._on) return;
    installKeys._on = true;
    window.addEventListener("keydown", onKey);
  }

  function onKey(ev){
    if (!keysLive()) return;
    const mod = ev.ctrlKey || ev.metaKey;
    const k = ev.key;
    // Ctrl+F must work FROM the tree filter as well as into it, and Escape has
    // to be able to leave a field — those two are the only keys allowed
    // through while something is focused for typing.
    if (typingIn(ev.target)){
      if (k === "Escape"){ ev.target.blur(); return; }
      if (mod && (k === "f" || k === "F")){ ev.preventDefault(); treeFocus(); }
      return;
    }
    const take = () => { ev.preventDefault(); ev.stopPropagation(); };

    if (mod && (k === "f" || k === "F")){ take(); treeFocus(); return; }
    if (k === "Escape"){
      if (window.SceneView && SceneView.escape && SceneView.escape()){ take(); return; }
      if (selection.size){ take(); pick(null, "set"); }
      return;
    }
    if (k === "Delete" || k === "Backspace"){
      if (!selection.size) return;
      take();
      // The graph surface stages nothing, so its delete is the diff-then-write
      // one that has always been there. Same discipline, different moment.
      if (surface === "viewport" && window.SceneView && SceneView.list)
        SceneView.removeSelected();
      else if (selection.size > 1)
        say(`${selection.size} nodes are selected and the graph surface writes `
            + "one edit at a time - switch to the viewport to delete a batch");
      else remove();
      return;
    }
    if (mod && (k === "d" || k === "D")){
      take();
      if (stageOnly()) SceneView.duplicateSelected();
      return;
    }
    if (mod && (k === "c" || k === "C")){
      // Never steal a copy from someone with text HIGHLIGHTED on the page. A
      // collapsed selection is a caret, not a highlight — testing the object
      // for truthiness instead swallowed every Ctrl+C in the panel, because a
      // stray caret is almost always somewhere.
      const text = window.getSelection && window.getSelection();
      if (text && !text.isCollapsed && String(text)) return;
      if (!selection.size) return;
      take(); copySelection(); return;
    }
    if (mod && (k === "v" || k === "V")){ take(); pasteClipboard(); return; }
    if (mod && (k === "z" || k === "Z")){
      take();
      if (!stageOnly()) return;
      if (ev.shiftKey) SceneView.redo(); else SceneView.undo();
      return;
    }
    if (mod && (k === "y" || k === "Y")){
      take();
      if (stageOnly()) SceneView.redo();
      return;
    }
    if (!mod && (k === "f" || k === "F")){
      take();
      if (surface === "viewport" && window.SceneView && SceneView.frame)
        SceneView.frame();
      return;
    }
    const arrow = { ArrowLeft:[-1, 0], ArrowRight:[1, 0],
                    ArrowUp:[0, -1], ArrowDown:[0, 1] }[k];
    if (arrow && !mod){
      if (!selection.size) return;
      take();
      if (stageOnly()) SceneView.nudge(arrow[0], arrow[1], ev.shiftKey);
      return;
    }
  }

  /* ── controls ─────────────────────────────────────────────────────────── */
  function setScene(id){
    scene = id; sel = null; data = null;
    selection.clear(); anchor = null; treeOpen.clear(); treeQ = "";
    activate(id, true);
  }
  function toggleRole(r){ filter.has(r) ? filter.delete(r) : filter.add(r); render(); }
  function refresh(){ activate(scene, true); }

  /* Called when this MODE is navigated away from — not when it is torn down.
     The viewport keeps everything it has staged; what stops is the playable
     build inside it, which would otherwise keep running, and playing audio,
     behind whatever the operator switched to. Same contract as
     AtlasCode.deactivate, for the same reason. */
  function deactivate(){
    if (window.SceneView && typeof SceneView.suspend === "function")
      SceneView.suspend();
  }

  return { activate, render, refresh, setScene, setSurface, toggleRole, select,
           pick, rename, deactivate,
           setProp, clearProp, addNode, remove, swapMenu, swapTo, editPixels,
           editAudio, act, closeModal, wireMenu, wireTo,
           treeToggle, treeFilter, treeCollapseAll, treeFocus,
           clonePlans, copySelection, pasteClipboard, setSelectionVisible,
           get data(){ return data; }, get scene(){ return scene; },
           get selection(){ return [...selection]; } };
})();
