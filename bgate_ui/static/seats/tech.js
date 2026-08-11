/* Tech seat workspace — engine health / build / resources / perf.
 *
 * Deliberately NOT the gameplay seat: gameplay edits scenes and scripts to make
 * the game feel good; tech keeps the engine honest — is Godot reachable, is the
 * web build current, does the project still compile, how heavy are the meshes,
 * and what is the dispatched tech agent actually doing right now.
 *
 * MODULE CONTRACT: window.SeatWS.tech = { label, glyph, render(container,bg), refresh() }.
 * Everything is guarded; render() must never throw even with Godot unavailable
 * or an empty project.
 */
(function () {
  const BGICON = (n) => (window.BGIcon ? BGIcon(n, { size: 15 }) : "");
  window.SeatWS = window.SeatWS || {};

  // ---- module state (per-render) -----------------------------------------
  const S = {
    bg: null,
    root: null,        // the workspace root element for this render
    tree: null,        // last /api/godot/files payload
    godotOk: false,
    building: false,   // web export in flight
    checking: false,   // build check in flight
    inspecting: false,
    running: false,    // gdscript run in flight
    itemId: null,      // active tech work item
    lastActivitySig: "", // dedup activity re-render
  };

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(2) + " MB";
  }
  function fmtAgo(mtime) {
    const s = Date.now() / 1000 - (Number(mtime) || 0);
    if (!isFinite(s) || s < 0) return "";
    if (s < 60) return Math.floor(s) + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  const esc = (s) => (S.bg ? S.bg.esc(s) : String(s == null ? "" : s));

  // A safe $ within our own root only (never touches another seat's DOM).
  function $(sel) { return S.root ? S.root.querySelector(sel) : null; }

  // ---- markup -------------------------------------------------------------
  const STYLE = `
  <style>
  /* Panels are .spanel + .sec-h out of app.css, NOT a private .tech-card
     treatment. Six identical grey boxes with a bold <h3> in each was this
     file's share of "everything blends together": nothing said which of them
     was a readout and which one runs your engine. What is left below is the
     grid and the content, which the shared classes do not and should not own. */
  .tech-wrap{display:grid;grid-template-columns:1fr 1fr;gap:var(--s-6);align-items:start}
  .tech-wrap .tech-span2{grid-column:1 / -1}
  .tech-wrap > .spanel{min-width:0}
  @media(max-width:1080px){.tech-wrap{grid-template-columns:1fr}.tech-wrap .tech-span2{grid-column:auto}}
  /* The one thing the band does not carry: live engine state. It rides in
     .sec-a, on the right, so the left of every header is icon-then-label and
     stays scannable down the column. */
  .tech-lamp{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--line-strong);flex:none}
  .tech-lamp.ok{background:var(--good-line);box-shadow:0 0 6px rgba(63,191,127,.6)}
  .tech-lamp.bad{background:var(--bad);box-shadow:0 0 6px rgba(224,87,76,.5)}
  .tech-lamp.warn{background:var(--warn)}
  .tech-row{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--text-2);padding:4px 0}
  .tech-row b{color:var(--text);font-weight:var(--fw-semi)}
  .tech-kv{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:var(--text-3);word-break:break-all}
  .tech-btn{padding:var(--s-4) var(--s-5);background:var(--surface-3);border:1px solid var(--line);border-radius:var(--r-sm);color:var(--text);font:inherit;font-size:var(--fs-sm);cursor:pointer;transition:background var(--dur-fast) var(--ease),border-color var(--dur-fast) var(--ease)}
  .tech-btn:hover{background:var(--surface-3)}
  .tech-btn:disabled{opacity:.5;cursor:default}
  .tech-btn.primary{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}
  .tech-btn.big{padding:10px 18px;font-size:13px;font-weight:var(--fw-semi);width:100%}
  .tech-btn.danger{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}
  .tech-spin{display:inline-block;width:12px;height:12px;border:2px solid var(--accent);border-top-color:transparent;border-radius:50%;animation:tech-rot .7s linear infinite;vertical-align:-1px}
  @keyframes tech-rot{to{transform:rotate(360deg)}}
  .tech-out{margin-top:10px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;line-height:1.5;background:var(--bg);border:1px solid var(--line-soft);border-radius:8px;padding:10px;max-height:260px;overflow:auto;white-space:pre-wrap;color:var(--text-2)}
  .tech-ok{color:var(--good)}.tech-err{color:var(--bad)}.tech-warn{color:var(--warn)}
  .tech-pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;border:1px solid var(--line);color:var(--text-2)}
  .tech-pill.g{background:var(--good-soft);border-color:var(--good-line);color:var(--good)}
  .tech-pill.r{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}
  .tech-pill.y{background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn)}
  .tech-select,.tech-input,.tech-ta{background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--text);font:inherit;font-size:12px;padding:7px 9px}
  .tech-ta{width:100%;font-family:ui-monospace,Menlo,Consolas,monospace;resize:vertical;min-height:120px}
  .tech-tree{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;max-height:340px;overflow:auto;line-height:1.7}
  .tech-node{cursor:default;color:var(--text-3);user-select:none}
  .tech-node .tf{cursor:pointer;color:var(--text)}
  .tech-node .tf:hover{color:var(--text);text-decoration:underline}
  .tech-node .tsz{color:var(--text-3);font-size:10px;margin-left:6px}
  .tech-dir{color:var(--text-3)}
  /* Depth is a CSS variable now, not two literal spaces per level baked into
     the text — indentation that survives copy/paste and wrapping. */
  .tech-node,.tech-dirbtn{padding-left:calc(var(--d,0) * 14px)}
  .tech-dirbtn{display:flex;align-items:center;gap:6px;width:100%;text-align:left;
    background:none;border:0;font:inherit;font-size:11.5px;color:var(--text-2);
    cursor:pointer;padding-top:1px;padding-bottom:1px;border-radius:var(--r-xs)}
  .tech-dirbtn:hover{background:var(--surface-3);color:var(--text)}
  .tech-dirbtn .tw{width:10px;flex:none;color:var(--accent)}
  .tech-dirbtn .tsz{margin-left:auto;padding-right:4px}
  .tech-kids[hidden]{display:none}
  .tech-mesh{display:grid;grid-template-columns:1fr auto auto;gap:4px 12px;font-size:11.5px;padding:6px 0;border-bottom:1px solid var(--line-soft)}
  .tech-mesh b{color:var(--text)}
  .tech-empty{color:var(--text-3);font-size:12px;padding:8px 0;font-style:italic}
  .tech-meta{font-size:11px;color:var(--text-3);margin:2px 0 8px}
  .tech-flex{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .tech-act{font-size:11.5px;line-height:1.5;border-left:2px solid var(--line);padding:3px 0 3px 10px;margin:5px 0;color:var(--text-2)}
  .tech-act .an{color:var(--text);font-weight:var(--fw-semi)}
  .tech-act.k-say{border-color:var(--accent);color:var(--text)}
  .tech-act.k-tool{border-color:var(--good-line)}
  .tech-act.k-result{border-color:var(--line);color:var(--text-3)}
  .tech-act.k-steer{border-color:var(--warn-line);color:var(--warn)}
  </style>`;

  /* One shape for every panel lid here, the same one world.js and the art and
     QA seats use: icon, label, count, then whatever the section needs on the
     right. Written as a helper rather than repeated six times because the point
     of the pattern is that all six agree. */
  function head(icon, label, opts) {
    const o = opts || {};
    return `<div class="sec-h">${BGICON(icon)}<h3 class="sec-t">${label}</h3>` +
      (o.nId ? `<span class="sec-n" id="${o.nId}"></span>` : "") +
      (o.note ? `<span class="sec-sub">${o.note}</span>` : "") +
      (o.lamp ? `<span class="sec-a"><span class="tech-lamp" id="${o.lamp}"></span></span>` : "") +
      `</div>`;
  }

  function shell() {
    return `${STYLE}
    <div class="tech-wrap">
      <section class="spanel s-tech k-read" id="tech-engine">
        ${head("rebuild", "Engine &amp; build", { nId: "tech-engine-sub", lamp: "tech-engine-lamp" })}
        <div id="tech-engine-body"><div class="tech-empty">checking Godot…</div></div></section>

      <section class="spanel s-tech" id="tech-check">
        ${head("verify", "Build check", { note: "headless import - does it compile" })}
        <button class="tech-btn primary big" id="tech-check-btn">Run build check</button>
        <div id="tech-check-out"></div></section>

      <section class="spanel s-tech tech-span2" id="tech-inspect">
        ${head("outline", "Resource inspector", { note: "structure &amp; triangle budget" })}
        <div class="tech-flex">
          <select class="tech-select" id="tech-res-sel" style="flex:1;min-width:220px"><option value="">- loading resources -</option></select>
          <button class="tech-btn primary" id="tech-inspect-btn">Inspect in engine</button>
        </div>
        <div class="tech-meta">.tscn scenes load in-engine and report real mesh/tri counts. .tres may not be a scene.</div>
        <div id="tech-inspect-out"></div></section>

      <section class="spanel s-tech k-list" id="tech-files">
        ${head("sheet", "Project files", { nId: "tech-files-sub" })}
        <div class="tech-tree" id="tech-tree"><div class="tech-empty">loading tree…</div></div>
        <div id="tech-file-view"></div></section>

      <section class="spanel s-tech" id="tech-run">
        ${head("run", "GDScript runner", { note: "headless probe" })}
        <textarea class="tech-ta" id="tech-script" spellcheck="false">extends SceneTree

func _init():
	print("hello from headless godot")
	print("OS: ", OS.get_name())
	quit()
</textarea>
        <div class="tech-flex" style="margin-top:8px">
          <button class="tech-btn primary" id="tech-run-btn">Run script</button>
          <span class="tech-meta" style="margin:0">must extend SceneTree and call quit()</span>
        </div>
        <div id="tech-run-out"></div></section>

      <section class="spanel s-tech tech-span2" id="tech-agent">
        ${head("agents", "Live tech agent", { nId: "tech-agent-sub" })}
        <div class="tech-flex">
          <select class="tech-select" id="tech-item-sel" style="flex:1;min-width:220px"><option value="">- pick a tech work item -</option></select>
        </div>
        <div id="tech-agent-body"><div class="tech-empty">Select a tech work item to watch its dispatched agent.</div></div></section>
    </div>`;
  }

  // ---- section 1: engine + build status ----------------------------------
  async function loadEngine() {
    const body = $("#tech-engine-body");
    const lamp = $("#tech-engine-lamp");
    const sub = $("#tech-engine-sub");
    if (!body) return;
    let g = null, p = null;
    try { g = await S.bg.get("/api/godot/status"); } catch (e) { g = { available: false, reason: e.message }; }
    try { p = await S.bg.get("/api/play/status"); } catch (e) { p = { stale: true, reason: e.message }; }
    if (!$("#tech-engine-body")) return; // re-rendered underneath us
    S.godotOk = !!(g && g.available);
    if (lamp) lamp.className = "tech-lamp " + (S.godotOk ? "ok" : "bad");
    if (sub) sub.textContent = S.godotOk ? (g.version || "available") : "unavailable";

    const stale = !!(p && p.stale);
    const buildPill = !p ? "" : (p.built === false
      ? `<span class="tech-pill r">no build - ${esc(p.reason || "never exported")}</span>`
      : (stale ? `<span class="tech-pill y">stale - source newer than build</span>`
               : `<span class="tech-pill g">current</span>`));
    const buildMeta = p && p.build_mtime
      ? `<div class="tech-meta">built ${esc(fmtAgo(p.build_mtime))} · source ${esc(fmtAgo(p.source_mtime))}</div>` : "";

    body.innerHTML = `
      <div class="tech-row"><span class="tech-lamp ${S.godotOk ? "ok" : "bad"}"></span>
        <b>Godot</b> ${S.godotOk ? esc(g.version || "detected") : `<span class="tech-err">${esc(g.reason || "not found")}</span>`}</div>
      ${S.godotOk && g.path ? `<div class="tech-kv">${esc(g.path)}</div>` : ""}
      ${g && g.project ? `<div class="tech-row"><b>Project</b> <span class="tech-kv">${esc(g.project)}</span></div>`
                       : `<div class="tech-row"><span class="tech-lamp bad"></span> no godot project</div>`}
      <div class="tech-row" style="margin-top:6px"><b>Web build</b> ${buildPill}</div>
      ${buildMeta}
      <div class="tech-flex" style="margin-top:8px">
        <button class="tech-btn ${stale ? "primary" : ""}" id="tech-rebuild-btn" ${S.godotOk ? "" : "disabled"}>Rebuild web export</button>
        <span class="tech-meta" style="margin:0">~15s headless export</span>
      </div>
      <div id="tech-rebuild-out"></div>`;
    const rb = $("#tech-rebuild-btn");
    if (rb) rb.onclick = rebuild;
  }

  async function rebuild() {
    if (S.building) return;
    const btn = $("#tech-rebuild-btn");
    const out = $("#tech-rebuild-out");
    S.building = true;
    if (btn) { btn.disabled = true; btn.innerHTML = `<span class="tech-spin"></span> exporting…`; }
    if (out) out.innerHTML = `<div class="tech-out">Exporting Web build - this takes ~15s…</div>`;
    let r;
    try { r = await S.bg.post("/api/play/rebuild"); } catch (e) { r = { ok: false, error: e.message }; }
    S.building = false;
    if (!$("#tech-rebuild-out")) return;
    if (r && r.ok) {
      if (out) out.innerHTML = `<div class="tech-out tech-ok">Build OK - index.pck ${fmtBytes(r.bytes)}${r.wasm ? " · wasm " + fmtBytes(r.wasm) : ""}</div>`;
      S.bg.toast("web export rebuilt");
      loadEngine();
    } else {
      const msg = (r && (r.error || r.detail)) || "export failed";
      if (out) out.innerHTML = `<div class="tech-out tech-err">${esc(msg)}</div>`;
      if (btn) { btn.disabled = false; btn.textContent = "Rebuild web export"; }
      S.bg.toast("rebuild failed", true);
    }
  }

  // ---- section 2: build check --------------------------------------------
  async function runCheck() {
    if (S.checking) return;
    const btn = $("#tech-check-btn");
    const out = $("#tech-check-out");
    S.checking = true;
    if (btn) { btn.disabled = true; btn.innerHTML = `<span class="tech-spin"></span> importing…`; }
    if (out) out.innerHTML = `<div class="tech-out">Running headless import…</div>`;
    let r;
    try { r = await S.bg.post("/api/godot/check"); } catch (e) { r = { ok: false, error: e.message }; }
    S.checking = false;
    if (btn) { btn.disabled = false; btn.textContent = "Run build check"; }
    if (!$("#tech-check-out")) return;
    if (r && r.error && r.exit_code === undefined) {
      out.innerHTML = `<div class="tech-out tech-err">${esc(r.error)}</div>`;
      return;
    }
    const errs = (r && r.errors) || [];
    const head = r && r.ok
      ? `<span class="tech-pill g">BUILD OK</span>`
      : `<span class="tech-pill r">${errs.length || "1"} error${errs.length === 1 ? "" : "s"}</span>`;
    let html = `<div class="tech-flex" style="margin:8px 0"><span class="tech-lamp ${r && r.ok ? "ok" : "bad"}"></span>${head}`;
    if (r && r.seconds != null) html += `<span class="tech-meta" style="margin:0">${esc(r.seconds)}s · exit ${esc(r.exit_code)}</span>`;
    html += `</div>`;
    if (errs.length) {
      html += `<div class="tech-out tech-err">` + errs.map((e) => esc(typeof e === "string" ? e : JSON.stringify(e))).join("\n") + `</div>`;
    } else if (r && r.output) {
      html += `<div class="tech-out">${esc(String(r.output).slice(-2000))}</div>`;
    }
    out.innerHTML = html;
  }

  // ---- section 3+4: file tree + resource picker --------------------------
  function flattenResources(nodes, acc) {
    (nodes || []).forEach((n) => {
      if (n.dir) flattenResources(n.children, acc);
      else if (/\.(tscn|tres)$/i.test(n.name)) acc.push(n);
    });
    return acc;
  }
  /* The tree above rendered every node eagerly — 589 files and 68 directories,
     2,006 DOM nodes, into a panel about 1,000px tall. The "▸" on each directory
     was decorative: nothing collapsed, so the whole project arrived as one
     indented wall you had to scroll to read.

     Directories are real now: collapsed by default, children built on first
     expand. Boot renders ~30 nodes instead of 2,006, and the arrow means what
     it looks like it means. _dirReg maps a rendered button back to its node,
     because that node's children are not in the DOM yet to be found. */
  const _dirReg = [];

  function renderLazyTree(nodes, depth) {
    if (depth === 0) _dirReg.length = 0;
    return renderLazyNodes(nodes, depth);
  }

  function renderLazyNodes(nodes, depth) {
    if (!nodes || !nodes.length) {
      return depth === 0 ? `<div class="tech-empty">empty project</div>` : "";
    }
    return nodes.map((n) => {
      if (n.dir) {
        const i = _dirReg.push(n) - 1;
        const kids = countFiles(n.children);
        return `<div class="tech-dirw" style="--d:${depth}">` +
          `<button type="button" class="tech-dirbtn" data-dir="${i}" data-depth="${depth}" aria-expanded="false">` +
            `<span class="tw">▸</span><span class="tech-dir">${esc(n.name)}/</span>` +
            `<span class="tsz">${kids} file${kids === 1 ? "" : "s"}</span>` +
          `</button><div class="tech-kids" hidden></div></div>`;
      }
      return `<div class="tech-node" style="--d:${depth}">` +
        `<span class="tf" data-rel="${esc(n.rel)}">${esc(n.name)}</span>` +
        `<span class="tsz">${fmtBytes(n.bytes)}</span></div>`;
    }).join("");
  }

  // One delegated handler on the tree root: expanding builds the level below on
  // demand, then leaves it in place so re-collapsing costs nothing.
  function wireTree(root) {
    if (!root || root._wired) return;
    root._wired = true;
    root.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".tech-dirbtn");
      if (btn && root.contains(btn)) {
        const kids = btn.parentElement.querySelector(".tech-kids");
        const open = btn.getAttribute("aria-expanded") === "true";
        if (!open && !kids.dataset.built) {
          const node = _dirReg[Number(btn.dataset.dir)];
          kids.innerHTML = renderLazyNodes(node && node.children, Number(btn.dataset.depth) + 1);
          kids.dataset.built = "1";
        }
        btn.setAttribute("aria-expanded", open ? "false" : "true");
        const tw = btn.querySelector(".tw");
        if (tw) tw.textContent = open ? "▸" : "▾";
        kids.hidden = open;
        return;
      }
      const f = ev.target.closest(".tf");
      if (f && root.contains(f)) openFile(f.dataset.rel);
    });
  }

  async function loadFiles() {
    let r;
    try { r = await S.bg.get("/api/godot/files?kind=all"); } catch (e) { r = { tree: [], error: e.message }; }
    S.tree = r;
    const treeEl = $("#tech-tree");
    const sub = $("#tech-files-sub");
    const sel = $("#tech-res-sel");
    if (treeEl) {
      if (r && r.error) treeEl.innerHTML = `<div class="tech-empty">could not load files: ${esc(r.error)}</div>`;
      else treeEl.innerHTML = renderLazyTree(r.tree, 0);
      // Per-element onclick cannot reach rows that do not exist yet; the tree
      // root delegates instead, which also covers every lazily-built level.
      wireTree(treeEl);
    }
    const resources = flattenResources(r && r.tree, []);
    if (sub) sub.textContent = r && r.tree ? `${countFiles(r.tree)} files` : "";
    if (sel) {
      sel.innerHTML = resources.length
        ? `<option value="">- pick a .tscn / .tres -</option>` + resources.map((n) => `<option value="${esc(n.rel)}">${esc(n.rel)}</option>`).join("")
        : `<option value="">no scenes/resources found</option>`;
    }
  }
  function countFiles(nodes) {
    let c = 0;
    (nodes || []).forEach((n) => { c += n.dir ? countFiles(n.children) : 1; });
    return c;
  }

  async function openFile(rel) {
    const view = $("#tech-file-view");
    if (!view || !rel) return;
    view.innerHTML = `<div class="tech-out">loading ${esc(rel)}…</div>`;
    let r;
    try { r = await S.bg.get("/api/godot/file?rel=" + encodeURIComponent(rel)); }
    catch (e) { r = { error: e.message }; }
    if (!$("#tech-file-view")) return;
    if (!r || r.error || r.text == null) {
      view.innerHTML = `<div class="tech-out tech-err">${esc((r && r.error) || "unreadable")}</div>`;
      return;
    }
    view.innerHTML = `<div class="tech-meta">${esc(rel)} · ${fmtBytes(r.bytes)}${r.truncated ? " · truncated" : ""}</div><div class="tech-out">${esc(r.text)}</div>`;
  }

  async function inspectRes() {
    if (S.inspecting) return;
    const sel = $("#tech-res-sel");
    const out = $("#tech-inspect-out");
    const rel = sel && sel.value;
    if (!rel) { S.bg.toast("pick a resource first", true); return; }
    const resPath = /^res:\/\//.test(rel) ? rel : "res://" + rel;
    S.inspecting = true;
    const btn = $("#tech-inspect-btn");
    if (btn) { btn.disabled = true; btn.innerHTML = `<span class="tech-spin"></span> inspecting…`; }
    if (out) out.innerHTML = `<div class="tech-out">Loading ${esc(rel)} in engine…</div>`;
    let r;
    try { r = await S.bg.post("/api/godot/inspect", { res_path: resPath }); }
    catch (e) { r = { ok: false, error: e.message }; }
    S.inspecting = false;
    if (btn) { btn.disabled = false; btn.textContent = "Inspect in engine"; }
    if (!$("#tech-inspect-out")) return;
    if (!r || !r.ok) {
      out.innerHTML = `<div class="tech-out tech-err">${esc((r && (r.error || r.stderr)) || "inspect failed")}</div>`;
      return;
    }
    const meshes = r.meshes || [];
    let html = `<div class="tech-flex" style="margin:8px 0">
      <span class="tech-pill">root <b>${esc(r.root)}</b></span>
      <span class="tech-pill">${esc(r.root_type)}</span>
      <span class="tech-pill">${meshes.length} mesh${meshes.length === 1 ? "" : "es"}</span>
      <span class="tech-pill ${r.total_tris > 100000 ? "y" : "g"}">${Number(r.total_tris || 0).toLocaleString()} tris total</span>
    </div>`;
    if (meshes.length) {
      html += `<div style="margin-top:6px"><div class="tech-mesh" style="color:var(--text-3);border-color:var(--line)"><b>mesh</b><b>surfaces</b><b>tris</b></div>`;
      html += meshes.map((m) => {
        const aabb = (m.aabb_size || []).map((v) => Number(v).toFixed(1)).join(" × ");
        return `<div class="tech-mesh"><span><b>${esc(m.name)}</b>${aabb ? `<span class="tsz">${esc(aabb)}</span>` : ""}</span>
          <span>${(m.surfaces || []).length}</span>
          <span class="${m.tris > 50000 ? "tech-warn" : ""}">${Number(m.tris || 0).toLocaleString()}</span></div>`;
      }).join("");
      html += `</div>`;
    } else {
      html += `<div class="tech-empty">no 3D meshes in this scene (2D/UI scene or resource)</div>`;
    }
    out.innerHTML = html;
  }

  // ---- section 5: gdscript runner ----------------------------------------
  async function runScript() {
    if (S.running) return;
    const ta = $("#tech-script");
    const out = $("#tech-run-out");
    const btn = $("#tech-run-btn");
    const script = ta ? ta.value : "";
    if (!script.trim()) { S.bg.toast("script is empty", true); return; }
    S.running = true;
    if (btn) { btn.disabled = true; btn.innerHTML = `<span class="tech-spin"></span> running…`; }
    if (out) out.innerHTML = `<div class="tech-out">Running headless…</div>`;
    let r;
    try { r = await S.bg.post("/api/godot/run", { script: script }); }
    catch (e) { r = { ok: false, error: e.message }; }
    S.running = false;
    if (btn) { btn.disabled = false; btn.textContent = "Run script"; }
    if (!$("#tech-run-out")) return;
    if (r && r.error && r.exit_code === undefined) {
      out.innerHTML = `<div class="tech-out tech-err">${esc(r.error)}${r.hint ? "\n" + esc(r.hint) : ""}</div>`;
      return;
    }
    const errs = (r && r.errors) || [];
    let html = `<div class="tech-flex" style="margin:8px 0"><span class="tech-lamp ${r && r.ok ? "ok" : "bad"}"></span>
      <span class="tech-pill ${r && r.ok ? "g" : "r"}">${r && r.ok ? "OK" : "errors"}</span>
      <span class="tech-meta" style="margin:0">${r && r.seconds != null ? esc(r.seconds) + "s · " : ""}exit ${esc(r && r.exit_code)}</span></div>`;
    if ((r && r.stdout || "").trim()) html += `<div class="tech-out">${esc(r.stdout)}</div>`;
    if (errs.length) html += `<div class="tech-out tech-err">` + errs.map((e) => esc(typeof e === "string" ? e : JSON.stringify(e))).join("\n") + `</div>`;
    else if ((r && r.stderr || "").trim()) html += `<div class="tech-out tech-warn">${esc(r.stderr)}</div>`;
    out.innerHTML = html;
  }

  // ---- section 6: live tech agent ----------------------------------------
  async function loadItems() {
    const sel = $("#tech-item-sel");
    if (!sel) return;
    let r;
    try { r = await S.bg.get("/api/queue"); } catch (e) { r = { items: [] }; }
    const items = ((r && r.items) || []).filter((i) => i.seat === "tech");
    const active = S.bg.activeItem;
    // Prefer a tech item; fall back to the shared active item if it's a tech one.
    if (!S.itemId && items.some((i) => i.id === active)) S.itemId = active;
    sel.innerHTML = items.length
      ? `<option value="">- pick a tech work item -</option>` + items.map((i) =>
          `<option value="${i.id}" ${i.id === S.itemId ? "selected" : ""}>#${i.id} · ${esc(i.status)} · ${esc(i.title)}</option>`).join("")
      : `<option value="">no tech work items queued</option>`;
    if (S.itemId) loadAgent(true);
  }

  async function loadAgent(force) {
    const sub = $("#tech-agent-sub");
    const body = $("#tech-agent-body");
    if (!body) return;
    if (!S.itemId) {
      // The band's count pill, not a sentence: .sec-n is a chip and a phrase in
      // it stretches the header band out of line with the five beside it.
      if (sub) sub.textContent = "";
      body.innerHTML = `<div class="tech-empty">Select a tech work item to watch its dispatched agent.</div>`;
      return;
    }
    let r;
    try { r = await S.bg.get("/api/agent-activity/" + S.itemId); }
    catch (e) { r = { steps: [], running: false, error: e.message }; }
    const steps = (r && r.steps) || [];
    const sig = S.itemId + ":" + (r && r.running) + ":" + (r && r.step_count) + ":" + ((r && r.final && r.final.subtype) || "");
    if (!force && sig === S.lastActivitySig) return; // no change; skip re-render (keeps steer box focus)
    S.lastActivitySig = sig;
    if (sub) {
      sub.textContent = r && r.running ? "running" : (r && r.final ? "done" : "idle");
      sub.className = "sec-n" + (r && r.running ? " good" : "");
    }

    let feed = "";
    if (steps.length) {
      feed = steps.map((s) => {
        if (s.kind === "tool") return `<div class="tech-act k-tool"><span class="an">${esc(s.name)}</span>${s.hint ? " " + esc(s.hint) : ""}</div>`;
        if (s.kind === "result") return `<div class="tech-act k-result">${esc(s.text)}</div>`;
        if (s.kind === "steer") return `<div class="tech-act k-steer">↳ steer: ${esc(s.text)}</div>`;
        return `<div class="tech-act k-say">${esc(s.text)}</div>`;
      }).join("");
    } else if (r && r.error) {
      feed = `<div class="tech-empty">could not read activity: ${esc(r.error)}</div>`;
    } else {
      feed = `<div class="tech-empty">No agent activity yet. Dispatch this item to start.</div>`;
    }
    const fin = r && r.final;
    const finHtml = fin ? `<div class="tech-out ${fin.subtype === "success" ? "tech-ok" : "tech-err"}" style="margin-top:8px">
      final (${esc(fin.subtype)})${fin.turns != null ? " · " + esc(fin.turns) + " turns" : ""}${fin.cost != null ? " · $" + Number(fin.cost).toFixed(3) : ""}
      ${fin.text ? "\n" + esc(fin.text) : ""}</div>` : "";

    const running = !!(r && r.running);
    /* The signature above only skips a repaint when NOTHING moved, and while an
     * agent is running the step count moves every few seconds — which is
     * exactly when the steer box is enabled and being typed into. Rebuilding
     * this card threw the half-written steer away mid-word. Carry the text,
     * the caret and the focus across the rebuild. */
    const steerWas = $("#tech-steer");
    const keptSteer = steerWas
      ? { text: steerWas.value, at: steerWas.selectionStart,
          focus: document.activeElement === steerWas }
      : null;
    body.innerHTML = `
      <div class="tech-flex" style="margin:6px 0 10px">
        <span class="tech-lamp ${running ? "ok" : (fin ? "warn" : "")}"></span>
        <span class="tech-pill ${running ? "g" : ""}">item #${S.itemId}</span>
        ${running
          ? `<button class="tech-btn danger" id="tech-stop-btn">Stop agent</button>`
          : `<button class="tech-btn primary" id="tech-dispatch-btn">Dispatch</button>`}
      </div>
      <div id="tech-feed" style="max-height:300px;overflow:auto">${feed}</div>
      ${finHtml}
      <div class="tech-flex" style="margin-top:10px">
        <input class="tech-input" id="tech-steer" placeholder="steer the agent (live course-correction)…" style="flex:1;min-width:200px" ${running ? "" : "disabled"}>
        <button class="tech-btn" id="tech-steer-btn" ${running ? "" : "disabled"}>Steer</button>
      </div>`;

    const feedEl = $("#tech-feed");
    if (feedEl) feedEl.scrollTop = feedEl.scrollHeight;
    const stopB = $("#tech-stop-btn"); if (stopB) stopB.onclick = stopAgent;
    const dispB = $("#tech-dispatch-btn"); if (dispB) dispB.onclick = dispatchAgent;
    const steerB = $("#tech-steer-btn");
    const steerI = $("#tech-steer");
    if (steerB) steerB.onclick = steerAgent;
    if (steerI) steerI.onkeydown = (e) => { if (e.key === "Enter") steerAgent(); };
    if (steerI && keptSteer && keptSteer.text) {
      steerI.value = keptSteer.text;
      if (keptSteer.focus && !steerI.disabled) {
        steerI.focus();
        try { steerI.setSelectionRange(keptSteer.at, keptSteer.at); } catch (e) {}
      }
    }
  }

  async function dispatchAgent() {
    if (!S.itemId) return;
    const btn = $("#tech-dispatch-btn");
    if (btn) { btn.disabled = true; btn.innerHTML = `<span class="tech-spin"></span> dispatching…`; }
    let r;
    try { r = await S.bg.post("/api/queue/" + S.itemId + "/dispatch"); }
    catch (e) { r = { ok: false, error: e.message }; }
    if (r && (r.ok || r.dispatched || r.status)) S.bg.toast("dispatched #" + S.itemId);
    else S.bg.toast((r && r.error) || "dispatch failed", true);
    S.lastActivitySig = "";
    loadAgent(true);
  }

  async function stopAgent() {
    if (!S.itemId) return;
    let r;
    try { r = await S.bg.post("/api/queue/" + S.itemId + "/stop"); }
    catch (e) { r = { ok: false, error: e.message }; }
    if (r && r.ok) S.bg.toast("stopped #" + S.itemId);
    else S.bg.toast((r && r.error) || "stop failed", true);
    S.lastActivitySig = "";
    loadAgent(true);
  }

  async function steerAgent() {
    if (!S.itemId) return;
    const inp = $("#tech-steer");
    const text = inp ? inp.value.trim() : "";
    if (!text) return;
    if (inp) inp.value = "";
    let r;
    try { r = await S.bg.post("/api/queue/" + S.itemId + "/steer", { text: text }); }
    catch (e) { r = { ok: false, error: e.message }; }
    if (r && r.ok) S.bg.toast("steer sent");
    else S.bg.toast((r && r.error) || "steer failed", true);
    S.lastActivitySig = "";
    loadAgent(true);
  }

  // ---- module contract ----------------------------------------------------
  window.SeatWS.tech = {
    label: "Tech",
    glyph: BGICON("tech"),
    render(container, bg) {
      try {
        S.bg = bg;
        S.root = container;
        S.tree = null;
        S.lastActivitySig = "";
        // keep S.itemId across re-renders so the watched agent survives a refresh
        container.innerHTML = shell();

        const cb = container.querySelector("#tech-check-btn"); if (cb) cb.onclick = runCheck;
        const ib = container.querySelector("#tech-inspect-btn"); if (ib) ib.onclick = inspectRes;
        const rb = container.querySelector("#tech-run-btn"); if (rb) rb.onclick = runScript;
        const isel = container.querySelector("#tech-item-sel");
        if (isel) isel.onchange = () => {
          S.itemId = isel.value ? Number(isel.value) : null;
          S.lastActivitySig = "";
          if (S.itemId) { try { bg.setActiveItem(S.itemId); } catch (e) {} }
          loadAgent(true);
        };

        loadEngine();
        loadFiles();
        loadItems();
      } catch (e) {
        try { container.innerHTML = `<div class="tech-empty">tech workspace failed to render: ${esc(e.message)}</div>`; } catch (_) {}
        if (window.console) console.error("tech.render", e);
      }
    },
    refresh() {
      try {
        if (!S.root || !document.body.contains(S.root)) return;
        // Only the live-agent feed benefits from polling; the rest is on-demand.
        if (S.itemId) loadAgent(false);
      } catch (e) { /* never throw from refresh */ }
    },
  };
})();
