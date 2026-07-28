/* atlas_graph.js — the project map as a graph you can WIRE, not just read.
 *
 * Atlas already derives every screen/asset edge in the project by reading
 * scenes and scripts. That made it a perfect map and a strictly read-only one:
 * the answer to "this sheet is wired to nothing" was always "go open Godot".
 * Four steps outside the tool that told you about it.
 *
 * This is the same graph on the shared NodeCanvas engine, with the one
 * direction that is safely mechanical made writable: drag an asset's output
 * onto a screen's input and the .tscn gets an ext_resource and a node of the
 * right type — Sprite2D for a PNG, AnimatedSprite2D for a SpriteFrames,
 * AudioStreamPlayer2D for an ogg — with a dry run shown first and a backup
 * taken on write. Click a screen and its real node tree opens in the
 * inspector, where each wired node can be pulled back out again.
 *
 * LAYOUT IS THE ARGUMENT. Assets flow left to right into the screens that use
 * them, exclusive assets sit in their screen's own band, and anything SHARED
 * gets its own column — because "this sheet is in four screens" is the single
 * most expensive fact to not know before editing it.
 *
 * What is deliberately NOT here yet: gameplay wiring (signals, exported
 * variables, state machines). The node/port model this is built on is the
 * right shape for it — those become node types with typed ports on the same
 * canvas — but a graph that pretends to wire behaviour it cannot verify would
 * be worse than the honest map.
 */
window.AtlasGraph = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };

  const GLYPH = { screen:"⊞", sprites:"▦", texture:"▧", audio:"♪", script:"⌁",
                  "scene-res":"⬡", font:"F", shader:"◐", other:"·" };
  // Port type per asset kind: the canvas refuses a mismatched drag for free,
  // so a script can never be dropped where a texture goes.
  const PORT_TYPE = { sprites:"frames", texture:"image", audio:"audio",
                      script:"script", "scene-res":"scene", font:"asset",
                      shader:"asset", other:"asset" };
  const EDITABLE = /\.(png|webp)$/i;

  // Assets wrap into a small grid inside their screen's band rather than one
  // tall stack. A stack of fifty is 6,400px of canvas that can only be read at
  // a zoom where the titles are gone — the shape was honest and useless.
  const NODE_W = 236, COL_W = 256, ROW = 126, BAND_GAP = 44;
  const ASSET_COLS = 3;
  const COL_SHARED = ASSET_COLS * COL_W + 30;
  const COL_SCREEN = COL_SHARED + COL_W + 40;

  // A real project carries hundreds of dead assets — this one had 313 against
  // 4 screens. Drawing them makes a 40,000px column that buries the graph's
  // actual job, so they are OFF by default and their count is the chip that
  // turns them on. MAX_NODES is the backstop for everything else: past it the
  // canvas stops being a map, and a silent truncation would read as "that's
  // all there is", so the overflow is stated.
  const MAX_NODES = 260;

  let nc = null, map = null, sel = null, tree = null, overflow = 0;
  let filter = { kinds: new Set(), search: "", onlyProblems: false,
                 showDead: false, screen: "" };

  /* ── styles ───────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("atlas-graph-style")) return;
    const s = document.createElement("style");
    s.id = "atlas-graph-style";
    s.textContent = [
      ".ag-wrap{display:flex;height:min(76vh,860px);border:1px solid var(--seam);border-radius:12px;overflow:hidden;background:var(--iron)}",
      ".ag-canvas{flex:1;position:relative;min-width:0}",
      ".ag-side{width:290px;flex:none;border-left:1px solid var(--seam);background:var(--iron);overflow-y:auto;padding:13px}",
      ".ag-h{font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--ash2);margin:15px 0 7px}",
      ".ag-h:first-child{margin-top:0}",
      ".ag-bar{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:10px}",
      ".ag-chip{font-family:var(--mono);font-size:9.5px;padding:4px 9px;border:1px solid var(--seam);border-radius:999px;color:var(--ash);cursor:pointer;background:none}",
      ".ag-chip:hover{border-color:var(--ember);color:var(--bone)}",
      ".ag-chip.on{border-color:var(--ember);color:var(--bone);background:var(--plate)}",
      ".ag-chip.bad{border-color:var(--bad-line);color:var(--bad)}",
      ".ag-in{background:var(--void);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;padding:5px 9px;min-width:140px}",
      ".ag-in:focus{outline:none;border-color:var(--ember)}",
      ".ag-b{padding:6px 10px;background:var(--plate);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;cursor:pointer}",
      ".ag-b:hover{border-color:var(--ember)}",
      ".ag-b.go{background:var(--ember);color:var(--bg);border-color:var(--ember);font-weight:var(--fw-semi)}",
      ".ag-nb{font-family:var(--mono);font-size:10px;color:var(--ash);line-height:1.5}",
      ".ag-nb img{width:100%;height:78px;object-fit:contain;background:var(--bg);border-radius:6px;image-rendering:pixelated;display:block;margin-bottom:6px}",
      ".ag-nb .p{color:var(--ash2);word-break:break-all;font-size:9px}",
      ".ag-nb .acts{display:flex;gap:5px;margin-top:6px}",
      ".ag-nb .acts button{flex:1;padding:4px;background:var(--plate);border:1px solid var(--seam);border-radius:5px;color:var(--bone);font:inherit;font-size:10px;cursor:pointer}",
      ".ag-nb .acts button:hover{border-color:var(--ember)}",
      ".ag-tn{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;color:var(--bone);padding:4px 6px;border:1px solid var(--seam);border-radius:6px;margin-bottom:4px}",
      ".ag-tn .t{color:var(--ash2);font-size:9px}",
      ".ag-tn .x{margin-left:auto;cursor:pointer;color:var(--ash2)}",
      ".ag-tn .x:hover{color:var(--bad)}",
      ".ag-note{font-size:11px;color:var(--ash);line-height:1.5}.ag-note b{color:var(--bone)}",
      ".ag-diff{background:var(--void);border:1px solid var(--seam);border-radius:7px;padding:8px;font-family:var(--mono);font-size:9.5px;color:var(--ash);white-space:pre-wrap;max-height:240px;overflow:auto}",
      ".ag-modal{position:fixed;inset:0;z-index:1300;background:rgba(4,5,7,.86);display:flex;align-items:center;justify-content:center;padding:40px}",
      ".ag-mbox{background:var(--iron);border:1px solid var(--seam);border-radius:12px;width:min(680px,100%);max-height:100%;display:flex;flex-direction:column;overflow:hidden}",
      ".ag-mhd{padding:11px 14px;border-bottom:1px solid var(--seam);font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--bone)}",
      ".ag-mbd{padding:14px;overflow-y:auto}",
      ".ag-mft{padding:11px 14px;border-top:1px solid var(--seam);display:flex;gap:8px;justify-content:flex-end}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── data ─────────────────────────────────────────────────────────────── */
  function consumers(){
    // asset id -> Set of screen ids that reach it directly.
    const use = {};
    const screenIds = new Set(map.screens.map(s => s.id));
    map.edges.forEach(e => {
      if (!screenIds.has(e.from) || e.via === "derived") return;
      (use[e.to] = use[e.to] || new Set()).add(e.from);
    });
    return use;
  }

  function visible(n){
    if (n.kind === "screen") return true;
    if (n.orphan && !filter.showDead && !filter.onlyProblems) return false;
    if (filter.kinds.size && !filter.kinds.has(n.kind)) return false;
    if (filter.onlyProblems && n.exists && !n.orphan) return false;
    if (filter.search && !(`${n.label} ${n.id}`.toLowerCase()
        .includes(filter.search))) return false;
    return true;
  }

  /* ── layout ───────────────────────────────────────────────────────────── */
  function build(){
    const use = consumers();
    const nodes = [], edges = [];
    const placed = new Set();
    overflow = 0;

    const assetNode = (id, x, y) => {
      const n = map.nodes[id];
      if (!n || placed.has(id)) return false;
      if (nodes.length >= MAX_NODES){ overflow++; return false; }
      placed.add(id);
      nodes.push({
        id, x, y, w: NODE_W, type: n.kind,
        title: n.label,
        glyph: GLYPH[n.kind] || "·",
        badge: !n.exists ? "missing" : n.orphan ? "dead"
             : (use[id] && use[id].size > 1) ? `×${use[id].size}` : "",
        status: !n.exists ? "bad" : n.orphan ? "warn" : "",
        accent: !n.exists ? "var(--bad)" : n.orphan ? "var(--warn)" : undefined,
        ports: { in: [], out: [{ id: "use", label: n.kind,
                                 type: PORT_TYPE[n.kind] || "asset" }] },
        data: n,
      });
      return true;
    };

    // Exclusive assets fill a wrapped grid in their screen's band; the screen
    // sits at the band's vertical centre so its edges fan symmetrically.
    const screens = filter.screen
      ? map.screens.filter(s => s.id === filter.screen)
      : map.screens;
    let y = 40;
    screens.forEach(s => {
      const mine = [...new Set(map.edges.filter(e => e.from === s.id).map(e => e.to))]
        .filter(id => map.nodes[id] && map.nodes[id].kind !== "screen"
                   && visible(map.nodes[id]));
      const excl = mine.filter(id => (use[id] || new Set()).size <= 1);
      let i = 0;
      excl.forEach(id => {
        const col = i % ASSET_COLS, row = (i / ASSET_COLS) | 0;
        if (assetNode(id, col * COL_W, y + row * ROW)) i++;
      });
      const bandH = Math.max(ROW, Math.ceil(i / ASSET_COLS) * ROW);
      nodes.push({
        id: s.id, x: COL_SCREEN, y: y + bandH/2 - 54, w: NODE_W, type: "screen",
        title: s.label, glyph: "⊞",
        badge: `${mine.length}`,
        ports: { in: [{ id: "assets", label: "assets", type: "*" }],
                 out: [{ id: "links", label: "opens", type: "scene" }] },
        data: map.nodes[s.id],
      });
      y += bandH + BAND_GAP;
    });

    // Shared assets, then anything unplaced (dead, missing, script-only), in
    // their own column — a shared sheet is the one you must not edit blind.
    // Focusing one screen drops them: they are context for the whole map.
    let sy = 40;
    if (!filter.screen){
      Object.keys(map.nodes).forEach(id => {
        const n = map.nodes[id];
        if (n.kind === "screen" || placed.has(id) || !visible(n)) return;
        if (assetNode(id, COL_SHARED, sy)) sy += ROW;
      });
    }

    // Edges: asset -> screen, plus screen -> screen for scene links.
    const screenIds = new Set(map.screens.map(s => s.id));
    const have = new Set(nodes.map(n => n.id));
    map.edges.forEach(e => {
      if (!have.has(e.from) || !have.has(e.to)) return;
      if (screenIds.has(e.from) && screenIds.has(e.to)){
        edges.push({ from:[e.from, "links"], to:[e.to, "assets"] });
      } else if (screenIds.has(e.from)){
        edges.push({ from:[e.to, "use"], to:[e.from, "assets"] });
      }
      // 'tres' and 'derived' edges (a SpriteFrames to its sheets) are a
      // containment relation, not a wiring one — drawing them here would put
      // arrows between two nodes in the same column and read as noise.
    });
    return { nodes, edges };
  }

  function renderBody(n){
    const d = n.data || {};
    if (n.type === "screen"){
      return `<div class="ag-nb"><div class="p">${E(d.path || n.id)}</div>
        <div class="acts">
          <button onclick="AtlasGraph.openTree('${E(n.id)}')">node tree</button>
          <button onclick="Atlas.deploy('${E(n.id)}')">task</button>
        </div></div>`;
    }
    const img = d.preview
      ? `<img loading="lazy" src="/api/preview?rel=${encodeURIComponent(d.preview)}" alt="">` : "";
    const canEdit = d.preview && EDITABLE.test(d.path || "");
    return `<div class="ag-nb">${img}
      <div class="p">${E(d.path || n.id)}</div>
      <div class="acts">
        ${canEdit ? `<button onclick="AtlasGraph.edit('${E(d.path)}')">edit pixels</button>` : ""}
        <button onclick="Atlas.deploy('${E(n.id)}')">task</button>
      </div></div>`;
  }

  /* ── mount ────────────────────────────────────────────────────────────── */
  async function activate(force){
    injectStyle();
    const host = document.getElementById("atlas-graph");
    if (!host) return;
    if (!map || force){
      host.innerHTML = `<div class="empty" style="padding:40px">scanning the project…</div>`;
      const d = await readJSON("/api/screenmap", null);
      if (!d || d.error || d.__error){
        host.innerHTML = `<div class="empty" style="padding:40px">${
          E((d && (d.error || d.__error)) || "scan failed")}</div>`;
        return;
      }
      map = d;
    }
    render();
  }

  function render(){
    const host = document.getElementById("atlas-graph");
    if (!host || !map) return;
    const kinds = [...new Set(Object.values(map.nodes)
      .map(n => n.kind).filter(k => k !== "screen"))].sort();

    host.innerHTML = `
      <div class="ag-bar">
        <select class="ag-in" onchange="AtlasGraph.setScreen(this.value)"
                title="Wiring is usually about one scene. Focus it and the rest of the map gets out of the way.">
          <option value="">all ${map.screens.length} screens</option>
          ${map.screens.map(s => `<option value="${E(s.id)}"${
            s.id === filter.screen ? " selected" : ""}>⊞ ${E(s.label)}</option>`).join("")}
        </select>
        <input class="ag-in" placeholder="filter assets…" value="${E(filter.search)}"
               oninput="AtlasGraph.setSearch(this.value)">
        ${kinds.map(k => `<button class="ag-chip${filter.kinds.has(k)?" on":""}"
          onclick="AtlasGraph.toggleKind('${E(k)}')">${GLYPH[k]||"·"} ${E(k)}</button>`).join("")}
        ${map.orphans.length ? `<button class="ag-chip${filter.showDead?" on bad":" bad"}"
          title="Assets on disk that nothing references. Hidden by default — there are usually far more of them than there are screens."
          onclick="AtlasGraph.toggleDead()">${map.orphans.length} dead</button>` : ""}
        <button class="ag-chip${filter.onlyProblems?" on bad":" bad"}"
          onclick="AtlasGraph.toggleProblems()">dead + missing only</button>
        <span style="flex:1"></span>
        <span class="ag-chip" id="ag-overflow" style="display:none;cursor:default"></span>
        <button class="ag-b" onclick="AtlasGraph.refresh()">rescan</button>
      </div>
      <div class="ag-wrap">
        <div class="ag-canvas" id="ag-canvas"></div>
        <div class="ag-side" id="ag-side"></div>
      </div>`;

    const { nodes, edges } = build();
    nc = new NodeCanvas(document.getElementById("ag-canvas"), {
      nodes, edges, renderBody, accent: "var(--ember)",
      onSelect: onSelect,
      onConnect: onConnect,
      onReject: (why) => say(why),
    });
    nc.mount();
    nc.fit({ min: 0.35 });
    paintOverflow();
    sideDefault();
  }

  /* A cap that is not stated reads as "that is all there is". */
  function paintOverflow(){
    const el = document.getElementById("ag-overflow");
    if (!el) return;
    el.style.display = overflow ? "" : "none";
    el.textContent = overflow ? `+${overflow} not drawn — filter to narrow` : "";
    el.classList.toggle("bad", !!overflow);
  }

  /* ── selection / inspector ────────────────────────────────────────────── */
  function onSelect(node){
    sel = node;
    tree = null;
    if (!node) return sideDefault();
    if (node.type === "screen") openTree(node.id);
    else sideAsset(node);
  }

  function side(html){
    const el = document.getElementById("ag-side");
    if (el) el.innerHTML = html;
  }

  function sideDefault(){
    const use = map ? consumers() : {};
    const shared = Object.keys(use).filter(k => use[k].size > 1).length;
    side(`<div class="ag-h">the map</div>
      <div class="ag-note">
        <b>${map.screens.length}</b> screens · <b>${Object.values(map.nodes)
          .filter(n => n.kind!=="screen").length}</b> assets ·
        <b>${shared}</b> shared · <b class="${map.orphans.length?"":""}">${map.orphans.length}</b> dead ·
        <b>${map.missing.length}</b> missing
      </div>
      <div class="ag-h">wiring</div>
      <div class="ag-note">Drag an asset's <b>right</b> port onto a screen's
        <b>left</b> port. The scene gets an <b>ext_resource</b> and a node of the
        matching type — you see the change before it is written, and the previous
        .tscn is kept under <b>.bgate_out/scene_backups</b>.</div>
      <div class="ag-h">not wired here</div>
      <div class="ag-note">Signals, exported variables and state machines are not
        on this canvas yet. The port model is the right shape for them; a graph
        that claimed to wire behaviour it cannot verify would be worse than an
        honest map.</div>`);
  }

  function sideAsset(node){
    const d = node.data || {};
    const use = consumers();
    const where = [...(use[node.id] || [])]
      .map(id => (map.nodes[id] || {}).label || id);
    const canEdit = d.preview && EDITABLE.test(d.path || "");
    side(`<div class="ag-h">${E(d.kind || "asset")}</div>
      ${d.preview ? `<img style="width:100%;background:var(--bg);border-radius:8px;image-rendering:pixelated;margin-bottom:9px"
        src="/api/preview?rel=${encodeURIComponent(d.preview)}" alt="">` : ""}
      <div class="ag-note"><b>${E(d.label || node.id)}</b><br>
        <span style="font-family:var(--mono);font-size:9.5px;color:var(--ash2)">${E(d.path || node.id)}</span></div>
      <div class="ag-h">used by</div>
      <div class="ag-note">${where.length ? where.map(E).join(", ")
        : `<span style="color:var(--warn)">no screen references this</span>`}</div>
      <div class="ag-h">actions</div>
      ${canEdit ? `<button class="ag-b go" style="width:100%;margin-bottom:6px"
        onclick="AtlasGraph.edit('${E(d.path)}')">open in sprite editor</button>` : ""}
      <button class="ag-b" style="width:100%;margin-bottom:6px"
        onclick="AtlasGraph.wireMenu('${E(node.id)}')">wire into a scene…</button>
      <button class="ag-b" style="width:100%"
        onclick="Atlas.deploy('${E(node.id)}')">deploy a task</button>`);
  }

  async function openTree(sceneId){
    const d = await readJSON(`/api/scene/tree?scene=${encodeURIComponent(sceneId)}`, null);
    if (!d || d.__error){ side(`<div class="ag-note">${E((d&&d.__error)||"could not read that scene")}</div>`); return; }
    tree = d;
    side(`<div class="ag-h">scene</div>
      <div class="ag-note"><b>${E(d.root || "")}</b><br>
        <span style="font-family:var(--mono);font-size:9.5px;color:var(--ash2)">${E(d.rel)}</span></div>
      <div class="ag-h">nodes</div>
      ${d.nodes.map(n => `<div class="ag-tn">${E(n.name)}
        <span class="t">${E(n.type || "")}</span>
        ${n.parent === null ? "" : `<span class="x" title="remove this node from the scene"
          onclick="AtlasGraph.unwire('${E(d.scene)}','${E(n.path)}')">✕</span>`}</div>`).join("")}
      <div class="ag-h">resources</div>
      ${d.resources.length ? d.resources.map(r => `<div class="ag-tn">
        <span class="t">${E(r.type)}</span> ${E(r.path.split("/").pop())}</div>`).join("")
        : `<div class="ag-note">none</div>`}`);
  }

  /* ── wiring ───────────────────────────────────────────────────────────── */
  async function onConnect(from, to){
    const assetId = from[0], sceneId = to[0];
    if (!map.nodes[sceneId] || map.nodes[sceneId].kind !== "screen"){
      say("only a screen can receive an asset");
      // The canvas already drew the edge optimistically; drop it again.
      dropLastEdge(from, to);
      return;
    }
    if (map.nodes[assetId] && map.nodes[assetId].kind === "screen"){
      // screen -> screen would be a scene instance; wire it as one.
      return proposeWire(assetId, sceneId, from, to);
    }
    return proposeWire(assetId, sceneId, from, to);
  }

  function dropLastEdge(from, to){
    if (!nc) return;
    const i = nc.edges.findIndex(e => e.from[0] === from[0] && e.from[1] === from[1]
                                   && e.to[0] === to[0] && e.to[1] === to[1]);
    if (i >= 0) nc.removeEdge(i);
  }

  async function proposeWire(assetId, sceneId, from, to){
    const r = await mutate("/api/scene/wire", {
      body: { scene: sceneId, asset: assetId, dry_run: true }, quiet: true });
    if (!r.ok){ say(r.error); dropLastEdge(from, to); return; }
    const d = r.data;
    modal(`wire ${map.nodes[assetId].label} into ${map.nodes[sceneId].label}`,
      `<div class="ag-note" style="margin-bottom:10px">${E(d.summary)}</div>
       <div class="ag-h">the scene after this change</div>
       <div class="ag-diff">${E(tail(d.text, 1400))}</div>`,
      [
        { label:"cancel", fn: () => { closeModal(); dropLastEdge(from, to); } },
        { label:"write it", go:true, fn: async () => {
            closeModal();
            const w = await mutate("/api/scene/wire",
              { body: { scene: sceneId, asset: assetId } });
            if (!w.ok){ dropLastEdge(from, to); return; }
            say(`${w.data.summary} · backup ${w.data.backup}`, "ok");
            refresh();
          } },
      ]);
  }

  async function wireMenu(assetId){
    const d = await readJSON(`/api/scene/wirable?asset=${encodeURIComponent(assetId)}`, null);
    if (!d || d.__error){ say((d && d.__error) || "could not list scenes"); return; }
    modal(`wire ${map.nodes[assetId] ? map.nodes[assetId].label : assetId} into…`,
      d.scenes.map(s => `<div class="ag-tn" style="cursor:${s.has_asset?"default":"pointer"};opacity:${s.has_asset?.5:1}"
        ${s.has_asset ? "" : `onclick="AtlasGraph.wireTo('${E(assetId)}','${E(s.scene)}')"`}>
        ⊞ ${E(s.label)} <span class="t">${s.nodes} nodes${s.has_asset ? " · already wired" : ""}</span></div>`).join("")
        || `<div class="ag-note">no scenes found</div>`,
      [{ label:"close", fn: closeModal }]);
  }

  async function wireTo(assetId, sceneId){
    closeModal();
    await proposeWire(assetId, sceneId, null, null);
  }

  async function unwire(sceneId, nodePath){
    const r = await mutate("/api/scene/unwire", {
      body: { scene: sceneId, node: nodePath, dry_run: true }, quiet: true });
    if (!r.ok){ say(r.error); return; }
    modal(`remove ${nodePath}`,
      `<div class="ag-note" style="margin-bottom:10px">${E(r.data.summary)}</div>
       <div class="ag-diff">${E(tail(r.data.text, 1400))}</div>`,
      [
        { label:"cancel", fn: closeModal },
        { label:"remove it", go:true, fn: async () => {
            closeModal();
            const w = await mutate("/api/scene/unwire",
              { body: { scene: sceneId, node: nodePath } });
            if (!w.ok) return;
            say(`${w.data.summary} · backup ${w.data.backup}`, "ok");
            refresh();
          } },
      ]);
  }

  function tail(text, n){
    const t = String(text || "");
    return t.length > n ? "…\n" + t.slice(-n) : t;
  }

  /* ── modal ────────────────────────────────────────────────────────────── */
  let modalActions = [];
  function modal(title, bodyHtml, actions){
    closeModal();
    modalActions = actions || [];
    const el = document.createElement("div");
    el.className = "ag-modal";
    el.id = "ag-modal";
    el.innerHTML = `<div class="ag-mbox">
      <div class="ag-mhd">${E(title)}</div>
      <div class="ag-mbd">${bodyHtml}</div>
      <div class="ag-mft">${modalActions.map((a, i) =>
        `<button class="ag-b${a.go ? " go" : ""}" onclick="AtlasGraph.modalAct(${i})">${E(a.label)}</button>`
      ).join("")}</div></div>`;
    document.body.appendChild(el);
    el.addEventListener("click", ev => { if (ev.target === el) closeModal(); });
  }
  function modalAct(i){ const a = modalActions[i]; if (a && a.fn) a.fn(); }
  function closeModal(){ const el = document.getElementById("ag-modal"); if (el) el.remove(); }

  /* ── controls ─────────────────────────────────────────────────────────── */
  function setSearch(v){
    filter.search = String(v || "").toLowerCase().trim();
    relayout();
  }
  function toggleKind(k){
    filter.kinds.has(k) ? filter.kinds.delete(k) : filter.kinds.add(k);
    render();
  }
  function toggleProblems(){ filter.onlyProblems = !filter.onlyProblems; render(); }
  function toggleDead(){ filter.showDead = !filter.showDead; render(); }
  function setScreen(id){ filter.screen = id || ""; render(); }
  function refresh(){ activate(true); }
  function edit(rel){
    if (window.SpriteEdit) SpriteEdit.open(rel);
    else say("the sprite editor did not load");
  }

  let relayoutTimer = null;
  function relayout(){
    clearTimeout(relayoutTimer);
    relayoutTimer = setTimeout(() => {
      if (!nc) return render();
      const { nodes, edges } = build();
      nc.setNodes(nodes, edges);
      nc.fit({ min: 0.35 });
      paintOverflow();
    }, 180);
  }

  return { activate, render, refresh, setSearch, toggleKind, toggleProblems,
           toggleDead, setScreen, openTree, unwire, wireMenu, wireTo, edit,
           modalAct, closeModal, get map(){ return map; } };
})();
