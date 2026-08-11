/* Gameplay seat — the Godot workspace.
 *
 * A "Godot editor" alongside the gameplay agent: engine status, a read-only
 * script browser, a headless GDScript runner + build-check, scene screenshots,
 * the live playable build in an iframe, and the live gameplay agent (activity
 * feed + steer + stop). Editing the engine is done by DISPATCHING the agent,
 * not by writing files from here — the browser stays read-only on purpose.
 *
 * Contract: window.SeatWS.gameplay = { label, glyph, render(container,bg), refresh() }
 * Everything is guarded; nothing here may throw uncaught.
 */
(function () {
  const BGICON = (n) => (window.BGIcon ? BGIcon(n, { size: 15 }) : "");
  window.SeatWS = window.SeatWS || {};

  const DEFAULT_SCRIPT =
`# Headless probe — must extend SceneTree and call quit().
# Loads a resource, prints findings, exits. Edit freely, then Run.
extends SceneTree

func _init():
    print("=== gameplay probe ===")
    var dir := DirAccess.open("res://")
    if dir:
        print("res:// entries:")
        for f in dir.get_files():
            print("  ", f)
        for d in dir.get_directories():
            print("  ", d, "/")
    # Example: inspect a scene
    #   var packed = load("res://scenes/Fighter.tscn")
    #   print("scene ok: ", packed != null)
    quit()
`;

  const S = {
    bg: null,
    godot: null,          // /api/godot/status
    tree: null,           // /api/godot/files tree
    selRel: null,         // currently-open script
    items: [],            // gameplay queue items
    agents: [],           // /api/agents
    selItem: null,        // chosen work item id
    activityKey: null,    // last-rendered activity signature (dedupe)
    running: false,       // a run/check is in flight
    // null = not yet known. The F1 hint below the play frame is only allowed to
    // promise the tuning overlay once we've SEEN the addon in this project.
    hasTuner: null,
  };

  function esc(s) { return S.bg ? S.bg.esc(s) : String(s == null ? "" : s); }

  const gp = {
    label: "Gameplay",
    glyph: BGICON("gameplay"),

    render(container, bg) {
      S.bg = bg;
      S.selItem = bg.activeItem || S.selItem || null;
      try {
        container.innerHTML = shell();
        wire(container);
      } catch (e) {
        container.innerHTML = `<div class="empty">gameplay workspace error: ${esc(e && e.message)}</div>`;
        console.error(e);
        return;
      }
      // Kick off the async loads; each guards itself.
      loadStatus();
      loadTree();
      loadQueue();
    },

    refresh() {
      // Cheap, frequent: keep the live agent feed current. Everything guarded.
      try { pollAgent(); } catch (e) {}
    },
  };
  window.SeatWS.gameplay = gp;

  /* ---- shell markup ----------------------------------------------------- */
  function shell() {
    return `
<style>
  .gp-wrap{display:flex;flex-direction:column;gap:12px;color:var(--text);font-size:13px}
  .gp-head{display:flex;align-items:center;gap:var(--s-5);background:var(--surface-1);border:1px solid var(--line);border-radius:var(--r-md);padding:var(--s-4) var(--s-6);flex-wrap:wrap}
  .gp-head .bgi{color:var(--text-3)}
  .gp-lamp{width:10px;height:10px;border-radius:50%;background:var(--bad-line);box-shadow:0 0 0 3px rgba(90,42,42,.18);flex-shrink:0}
  .gp-lamp.ok{background:var(--accent);box-shadow:0 0 0 3px rgba(59,127,158,.2)}
  .gp-head .gp-meta{color:var(--text-3);font-size:11.5px;font-family:ui-monospace,Menlo,Consolas,monospace}
  .gp-cols{display:grid;grid-template-columns:minmax(260px,1fr) minmax(340px,1.4fr);gap:var(--s-6);align-items:start}
  @media(max-width:1080px){.gp-cols{grid-template-columns:1fr}}
  /* The panels are .spanel + .sec-h from app.css. There was a private .gp-card
     here with its own <h4> treatment, which is how six sections on one screen
     ended up looking like one continuous grey field: the header was a slightly
     smaller line of text inside the same surface, so nothing marked where a
     section started. min-width:0 because these sit in a grid and a <pre> or a
     long res:// path inside one will otherwise widen the whole column. */
  .gp-cols > .spanel,.gp-cols > div > .spanel{min-width:0}
  /* inline-flex, because BGIcon renders display:block - a glyph dropped into a
     plain inline button stacked itself on top of the label and "Run" arrived as
     two lines. Every button in this file can now carry an icon. */
  .gp-btn{display:inline-flex;align-items:center;gap:var(--s-3);padding:6px 12px;background:var(--good-soft);border:1px solid var(--good-line);border-radius:var(--r-sm);color:var(--good);cursor:pointer;font:inherit;font-size:12px}
  .gp-btn:hover{background:var(--good-soft)}
  .gp-btn.alt{background:var(--surface-1);border-color:var(--line);color:var(--text)}
  .gp-btn.alt:hover{background:var(--surface-2)}
  .gp-btn.danger{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}
  .gp-btn:disabled{opacity:.5;cursor:default}
  .gp-tree{max-height:320px;overflow:auto;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.7}
  .gp-tree .gp-dir{cursor:pointer;color:var(--text);user-select:none}
  .gp-tree .gp-dir:hover{color:var(--text)}
  .gp-tree .gp-file{cursor:pointer;color:var(--text-2);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .gp-tree .gp-file:hover{color:var(--text)}
  .gp-tree .gp-file.sel{color:var(--accent);font-weight:var(--fw-semi)}
  .gp-tree .gp-file .gp-b{color:var(--text-3);font-size:10.5px}
  .gp-tree .gp-kids{margin-left:13px;border-left:1px solid var(--line);padding-left:8px}
  .gp-tree .gp-kids.hid{display:none}
  .gp-view{margin-top:10px;background:var(--bg);border:1px solid var(--line);border-radius:8px;max-height:360px;overflow:auto}
  .gp-view pre{margin:0;display:flex;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.55}
  .gp-view .gp-ln{color:var(--text-3);text-align:right;padding:8px 8px 8px 10px;user-select:none;border-right:1px solid var(--line);min-width:38px}
  .gp-view .gp-code{padding:8px 12px;white-space:pre;overflow-x:auto;color:var(--text);flex:1}
  .gp-ta{width:100%;box-sizing:border-box;min-height:150px;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--text);font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.5;padding:9px 11px;resize:vertical}
  .gp-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
  .gp-in{padding:6px 9px;background:var(--bg);border:1px solid var(--line);border-radius:7px;color:var(--text);font:inherit;font-size:12px}
  .gp-out{margin-top:9px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:9px 11px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto;color:var(--text)}
  .gp-out .gp-err{color:var(--bad)}
  .gp-out .gp-good{color:var(--good)}
  .gp-shot{margin-top:9px;text-align:center}
  .gp-shot img{max-width:100%;max-height:300px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
  .gp-frame{width:100%;height:360px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
  .gp-hint{color:var(--text-3);font-size:11px;margin-top:6px}
  .gp-feed{margin-top:8px;max-height:300px;overflow:auto;display:flex;flex-direction:column;gap:5px}
  .gp-step{font-size:11.5px;line-height:1.45;padding:5px 8px;border-radius:7px;border:1px solid var(--line);background:var(--bg)}
  .gp-step .gp-tag{font-size:9.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);margin-right:6px}
  .gp-step.say{border-color:var(--line)}
  .gp-step.tool .gp-tag{color:var(--accent)}
  .gp-step.result{color:var(--text-2)}
  .gp-step.steer{border-color:var(--warn-line);background:var(--warn-soft)}
  .gp-step.steer .gp-tag{color:var(--warn)}
  .gp-final{margin-top:8px;padding:8px 10px;border-radius:8px;border:1px solid var(--good-line);background:var(--good-soft);font-size:12px}
  .gp-final.bad{border-color:var(--bad-line);background:var(--bad-soft);color:var(--bad)}
  .gp-sel{padding:6px 9px;background:var(--bg);border:1px solid var(--line);border-radius:7px;color:var(--text);font:inherit;font-size:12px;max-width:100%}
  .gp-empty{color:var(--text-3);font-size:12px;padding:8px 2px}
</style>
<div class="gp-wrap">
  <!-- A STATUS STRIP, not a heading. It used to say "Godot workspace" in bold,
       which is the seat tab's own label repeated four pixels under it - the
       duplicate-title complaint, in miniature. What it is actually for is the
       engine lamp and the version, so that is all it says now. -->
  <div class="gp-head" id="gp-head">
    <span class="gp-lamp" id="gp-lamp"></span>
    ${BGICON("tech")}<span class="sec-sub">engine</span>
    <span class="gp-meta" id="gp-status">checking engine…</span>
  </div>

  <div class="gp-cols">
    <section class="spanel s-gameplay k-list">
      <div class="sec-h">${BGICON("sheet")}<h4 class="sec-t">Script browser</h4>
        <span class="sec-a"><button class="gp-btn alt" id="gp-tree-reload" style="padding:3px 9px">reload</button></span></div>
      <div class="gp-tree" id="gp-tree"><div class="gp-empty">loading scripts…</div></div>
      <div class="gp-view" id="gp-view" style="display:none"></div>
    </section>

    <div style="display:flex;flex-direction:column;gap:var(--s-6);min-width:0">
      <section class="spanel s-gameplay">
        <div class="sec-h">${BGICON("run")}<h4 class="sec-t">GDScript runner</h4></div>
        <textarea class="gp-ta" id="gp-script" spellcheck="false"></textarea>
        <div class="gp-row">
          <button class="gp-btn" id="gp-run">${BGICON("run")} Run</button>
          <button class="gp-btn alt" id="gp-check">Build check</button>
          <span class="gp-hint" style="margin:0">script must extend SceneTree and quit()</span>
        </div>
        <div class="gp-out" id="gp-run-out" style="display:none"></div>
      </section>

      <section class="spanel s-gameplay">
        <div class="sec-h">${BGICON("export_image")}<h4 class="sec-t">Screenshot</h4></div>
        <div class="gp-row" style="margin-top:0">
          <input class="gp-in" id="gp-shot-scene" placeholder="res://scenes/Main.tscn (blank = main)" style="flex:1;min-width:180px">
          <input class="gp-in" id="gp-shot-at" type="number" step="0.1" value="1.0" title="seconds into the scene" style="width:80px">
          <button class="gp-btn" id="gp-shot-btn">Capture</button>
        </div>
        <div class="gp-shot" id="gp-shot" style="display:none"></div>
      </section>

      <section class="spanel s-gameplay k-read">
        <div class="sec-h">${BGICON("playtests")}<h4 class="sec-t">Play alongside</h4>
          <span class="sec-a"><button class="gp-btn alt" id="gp-play-reload" style="padding:3px 9px">reload build</button></span></div>
        <iframe class="gp-frame" id="gp-play" src="about:blank" title="playable build"></iframe>
        <div class="gp-hint" id="gp-play-hint">Live build - checking for the tuning overlay…</div>
      </section>
    </div>
  </div>

  <section class="spanel s-gameplay">
    <div class="sec-h">${BGICON("agents")}<h4 class="sec-t">Live gameplay agent</h4>
      <span class="gp-badge sec-n" id="gp-agent-badge"></span></div>
    <div class="gp-row" style="margin-top:0">
      <select class="gp-sel" id="gp-item" style="flex:1;min-width:220px"></select>
      <button class="gp-btn alt" id="gp-dispatch">Dispatch</button>
      <button class="gp-btn alt" id="gp-diff" title="what this run actually changed, per file, since its base commit">Diff</button>
      <button class="gp-btn alt" id="gp-reopen" title="send a done/failed/cancelled item back to the queue with a reason">Reopen</button>
      <button class="gp-btn danger" id="gp-cancel" title="call the work off - a live agent is stopped first">Cancel</button>
      <button class="gp-btn danger" id="gp-stop" disabled title="no live agent to stop">Stop</button>
    </div>
    <div class="gp-out" id="gp-diff-out" style="display:none"></div>
    <div class="gp-feed" id="gp-feed"><div class="gp-empty">pick a gameplay work item</div></div>
    <div class="gp-row">
      <input class="gp-in" id="gp-steer" placeholder="no live agent - dispatch one to steer it" style="flex:1;min-width:220px" disabled>
      <button class="gp-btn" id="gp-steer-btn" disabled title="no live agent to steer">Steer</button>
    </div>
  </section>
</div>`;
  }

  /* ---- wiring ----------------------------------------------------------- */
  function wire(c) {
    const on = (id, ev, fn) => { const e = c.querySelector("#" + id); if (e) e.addEventListener(ev, fn); };
    on("gp-tree-reload", "click", loadTree);
    on("gp-run", "click", runScript);
    on("gp-check", "click", buildCheck);
    on("gp-shot-btn", "click", capture);
    on("gp-play-reload", "click", reloadPlay);
    on("gp-dispatch", "click", dispatchItem);
    on("gp-diff", "click", showDiff);
    on("gp-reopen", "click", reopenItem);
    on("gp-cancel", "click", cancelItem);
    on("gp-stop", "click", stopItem);
    on("gp-steer-btn", "click", steerItem);
    on("gp-item", "change", (e) => {
      S.selItem = Number(e.target.value) || null;
      S.activityKey = null;
      try { S.bg.setActiveItem(S.selItem); } catch (_) {}
      renderFeed({ steps: [], running: false, final: null }, true);
      pollAgent();
    });
    on("gp-steer", "keydown", (e) => { if (e.key === "Enter") steerItem(); });
    const ta = c.querySelector("#gp-script");
    if (ta && !ta.value) ta.value = DEFAULT_SCRIPT;
    reloadPlay();
  }

  function q(id) { return document.getElementById(id); }

  /* ---- engine status ---------------------------------------------------- */
  async function loadStatus() {
    let s;
    try { s = await S.bg.get("/api/godot/status"); }
    catch (e) { s = { available: false, reason: (e && e.message) || "unreachable" }; }
    S.godot = s || {};
    const lamp = q("gp-lamp"), st = q("gp-status");
    if (!st) return;
    if (S.godot.available) {
      if (lamp) lamp.classList.add("ok");
      const ver = S.godot.version || "unknown";
      const proj = S.godot.project || "(no project)";
      st.textContent = `Godot ${ver} · ${S.godot.path || ""} · project ${proj}`;
    } else {
      if (lamp) lamp.classList.remove("ok");
      st.textContent = "Godot unavailable - " + esc(S.godot.reason || "not found on PATH");
    }
  }

  /* ---- script browser --------------------------------------------------- */
  async function loadTree() {
    const host = q("gp-tree");
    if (host) host.innerHTML = '<div class="gp-empty">loading scripts…</div>';
    let data;
    try { data = await S.bg.get("/api/godot/files?kind=.gd"); }
    catch (e) {
      // Unknown, not absent: don't claim F1 is missing because a fetch failed.
      S.hasTuner = null; renderPlayHint();
      if (host) host.innerHTML = `<div class="gp-empty">could not list scripts - ${esc(e && e.message)}</div>`;
      return;
    }
    S.tree = (data && data.tree) || [];
    S.hasTuner = treeRels(S.tree).some(r => TUNER_REL.test(r));
    renderPlayHint();
    if (!host) return;
    if (!S.tree.length) {
      host.innerHTML = '<div class="gp-empty">no .gd scripts found in the project</div>';
      return;
    }
    host.innerHTML = renderTree(S.tree);
    host.querySelectorAll(".gp-dir").forEach(d => d.addEventListener("click", () => {
      const kids = d.nextElementSibling;
      if (kids) { kids.classList.toggle("hid"); d.firstChild && (d.firstChild.textContent = kids.classList.contains("hid") ? "▸ " : "▾ "); }
    }));
    host.querySelectorAll(".gp-file").forEach(f => f.addEventListener("click", () => openFile(f.dataset.rel, f)));
  }

  function renderTree(nodes) {
    return nodes.map(n => {
      if (n.dir) {
        return `<div><span class="gp-dir"><span>▾ </span>${esc(n.name)}/</span>` +
               `<div class="gp-kids">${renderTree(n.children || [])}</div></div>`;
      }
      const kb = n.bytes != null ? ` <span class="gp-b">${(n.bytes / 1024).toFixed(1)}k</span>` : "";
      return `<span class="gp-file" data-rel="${esc(n.rel)}" title="${esc(n.rel)}">${esc(n.name)}${kb}</span>`;
    }).join("");
  }

  async function openFile(rel, node) {
    if (!rel) return;
    const view = q("gp-view");
    document.querySelectorAll(".gp-tree .gp-file.sel").forEach(e => e.classList.remove("sel"));
    if (node) node.classList.add("sel");
    S.selRel = rel;
    if (view) { view.style.display = "block"; view.innerHTML = '<div class="gp-empty" style="padding:12px">loading…</div>'; }
    let f;
    try { f = await S.bg.get("/api/godot/file?rel=" + encodeURIComponent(rel)); }
    catch (e) { if (view) view.innerHTML = `<div class="gp-empty" style="padding:12px">could not read - ${esc(e && e.message)}</div>`; return; }
    if (!view) return;
    const text = (f && f.text) || "";
    const lines = text.split("\n");
    const gutter = lines.map((_, i) => i + 1).join("\n");
    const trunc = f && f.truncated ? '<div class="gp-empty" style="padding:6px 12px">…truncated (large file)</div>' : "";
    view.innerHTML = `<pre><div class="gp-ln">${gutter}</div><div class="gp-code">${esc(text)}</div></pre>${trunc}`;
  }

  /* ---- runner + build check -------------------------------------------- */
  function guardEngine(out) {
    if (S.godot && S.godot.available === false) {
      showOut(out, "Godot is unavailable - connect the engine to run scripts.", true);
      return false;
    }
    return true;
  }

  function showOut(el, html, bad) {
    if (!el) return;
    el.style.display = "block";
    el.innerHTML = html;
    el.scrollTop = 0;
  }

  function fmtErrors(errors) {
    if (!errors || !errors.length) return "";
    return `<span class="gp-err">${errors.map(esc).join("\n")}</span>\n\n`;
  }

  async function runScript() {
    const out = q("gp-run-out");
    if (S.running) return;
    if (!guardEngine(out)) return;
    const script = (q("gp-script") || {}).value || "";
    if (!script.trim()) { showOut(out, "nothing to run - write a GDScript first.", true); return; }
    S.running = true;
    const btn = q("gp-run"); if (btn) { btn.disabled = true; btn.textContent = "running…"; }
    showOut(out, "running…", false);
    let r;
    try { r = await S.bg.post("/api/godot/run", { script }); }
    catch (e) { r = { ok: false, error: (e && e.message) || "run failed" }; }
    S.running = false;
    if (btn) { btn.disabled = false; btn.textContent = "▶ Run"; }
    if (!r) { showOut(out, "no response", true); return; }
    if (r.error && r.ok == null) { showOut(out, `<span class="gp-err">${esc(r.error)}</span>`, true); return; }
    const head = r.ok ? '<span class="gp-good">✓ ran clean</span>' : `<span class="gp-err">✗ exit ${esc(r.exit_code)}</span>`;
    const stdout = r.stdout ? esc(r.stdout) : "(no stdout)";
    const stderr = r.stderr ? `\n\n<span class="gp-err">stderr:\n${esc(r.stderr)}</span>` : "";
    showOut(out, `${head}\n\n${fmtErrors(r.errors)}${stdout}${stderr}`, !r.ok);
  }

  async function buildCheck() {
    const out = q("gp-run-out");
    if (S.running) return;
    if (!guardEngine(out)) return;
    S.running = true;
    const btn = q("gp-check"); if (btn) { btn.disabled = true; btn.textContent = "checking…"; }
    showOut(out, "build check running…", false);
    let r;
    try { r = await S.bg.post("/api/godot/check", {}); }
    catch (e) { r = { ok: false, error: (e && e.message) || "check failed" }; }
    S.running = false;
    if (btn) { btn.disabled = false; btn.textContent = "Build check"; }
    if (!r) { showOut(out, "no response", true); return; }
    if (r.error && r.ok == null) { showOut(out, `<span class="gp-err">${esc(r.error)}</span>`, true); return; }
    const head = r.ok ? '<span class="gp-good">✓ project builds clean</span>' : '<span class="gp-err">✗ build errors</span>';
    showOut(out, `${head}\n\n${fmtErrors(r.errors)}${esc(r.output || "")}`, !r.ok);
  }

  /* ---- screenshot ------------------------------------------------------- */
  async function capture() {
    const host = q("gp-shot");
    if (S.godot && S.godot.available === false) {
      if (host) { host.style.display = "block"; host.innerHTML = '<div class="gp-empty">Godot unavailable</div>'; }
      return;
    }
    const scene = (q("gp-shot-scene") || {}).value || "";
    const at = parseFloat((q("gp-shot-at") || {}).value) || 1.0;
    const btn = q("gp-shot-btn"); if (btn) { btn.disabled = true; btn.textContent = "capturing…"; }
    if (host) { host.style.display = "block"; host.innerHTML = '<div class="gp-empty">capturing…</div>'; }
    const body = { at };
    if (scene.trim()) body.scene = scene.trim();
    let r;
    try { r = await S.bg.post("/api/godot/screenshot", body); }
    catch (e) { r = { ok: false, error: (e && e.message) || "screenshot failed" }; }
    if (btn) { btn.disabled = false; btn.textContent = "Capture"; }
    if (!host) return;
    if (r && r.ok && r.rel) {
      // bg.preview() ALREADY ends in "?rel=…". Appending "?t=" put the
      // cache-buster inside the rel VALUE, so every capture asked the server
      // for "…/shot.png?t=1770000000" — a path with no image extension, which
      // /api/preview answers 415 to. This panel had never once shown a
      // screenshot; it went straight to the onerror branch. Separator is "&".
      host.innerHTML = `<img src="${S.bg.preview(r.rel)}&t=${Date.now()}" alt="scene screenshot" onerror="this.parentNode.innerHTML='<div class=&quot;gp-empty&quot;>image failed to load</div>'">`;
    } else {
      host.innerHTML = `<div class="gp-empty">no image - ${esc((r && (r.error || (r.errors && r.errors.join(", ")))) || "capture failed")}</div>`;
    }
  }

  /* ---- play iframe ------------------------------------------------------ */
  function reloadPlay() {
    const f = q("gp-play");
    if (f) { try { f.src = "/play/?t=" + Date.now(); } catch (e) {} }
  }

  // The overlay is an addon that ships with the scaffold. A project that
  // predates it — or one someone stripped — has no F1, and saying otherwise is
  // exactly the kind of advertised-but-absent affordance we're fixing.
  const TUNER_REL = /(^|\/)addons\/bgate\/bgate_tuner\.gd$/i;

  function treeRels(nodes, out) {
    out = out || [];
    (nodes || []).forEach(n => {
      if (!n) return;
      if (n.dir) treeRels(n.children, out);
      else if (n.rel) out.push(String(n.rel).replace(/\\/g, "/"));
    });
    return out;
  }

  function renderPlayHint() {
    const el = q("gp-play-hint");
    if (!el) return;
    if (S.hasTuner === true) {
      el.innerHTML = "Live build - <b>F1</b> opens the live tuning overlay: " +
                     "every <code>@export</code> in the scene, applied as you drag, " +
                     "and kept for the next boot.";
    } else if (S.hasTuner === false) {
      el.textContent = "Live build - no tuning overlay in this project " +
                       "(addons/bgate/bgate_tuner.gd is missing; rescaffold to get it).";
    } else {
      el.textContent = "Live build - checking for the tuning overlay…";
    }
  }

  /* Stop and Steer act on a RUNNING process. With no live agent they used to
   * sit enabled and fail at the API — a control that looks available and isn't
   * is worse than one that's greyed out. */
  function setAgentControls(live) {
    const stop = q("gp-stop"), btn = q("gp-steer-btn"), inp = q("gp-steer");
    const on = !!live;
    if (stop) {
      stop.disabled = !on;
      stop.title = on ? `stop pid ${live.pid}` : "no live agent to stop";
    }
    if (btn) {
      btn.disabled = !on;
      btn.title = on ? "course-correct without restarting" : "no live agent to steer";
    }
    if (inp) {
      inp.disabled = !on;
      inp.placeholder = on ? "steer the agent (course-correct, no restart)…"
                           : "no live agent - dispatch one to steer it";
      if (!on) inp.value = "";
    }
  }

  /* ---- live agent ------------------------------------------------------- */
  async function loadQueue() {
    let data;
    try { data = await S.bg.get("/api/queue"); }
    catch (e) { data = { items: [] }; }
    const all = (data && data.items) || [];
    S.items = all.filter(i => i && i.seat === "gameplay");
    const sel = q("gp-item");
    if (!sel) return;
    if (!S.items.length) {
      sel.innerHTML = '<option value="">no gameplay work items queued</option>';
      setAgentControls(null);
      renderFeed({ steps: [], running: false, final: null }, true);
      return;
    }
    if (!S.items.some(i => i.id === S.selItem)) S.selItem = S.items[0].id;
    sel.innerHTML = S.items.map(i =>
      `<option value="${i.id}" ${i.id === S.selItem ? "selected" : ""}>#${i.id} [${esc(i.status)}] ${esc((i.title || "").slice(0, 70))}</option>`
    ).join("");
    pollAgent();
  }

  function liveAgentFor(id) {
    return (S.agents || []).find(a => a && a.item_id === id && a.state === "running") || null;
  }

  async function pollAgent() {
    if (!S.selItem) { setAgentControls(null); return; }
    // refresh the live agent table (cheap) then this item's activity.
    try {
      const a = await S.bg.get("/api/agents");
      S.agents = (a && a.agents) || [];
    } catch (e) { /* keep stale */ }

    const item = (S.items || []).find(i => i.id === S.selItem);
    const badge = q("gp-agent-badge");
    const live = liveAgentFor(S.selItem);
    setAgentControls(live);
    // The badge lives in the section header band now, so it is a .sec-n count
    // pill and takes that class's tones. An empty one hides itself, which is
    // why "no item picked" writes "" instead of a dash: a pill containing a
    // hyphen is furniture, and app.css already says so.
    if (badge) {
      if (live) { badge.className = "sec-n good"; badge.textContent = `live · pid ${live.pid}`; }
      else if (item && item.status === "dispatched") { badge.className = "sec-n"; badge.textContent = "dispatched"; }
      else if (item) { badge.className = "sec-n"; badge.textContent = item.status; }
      else { badge.className = "sec-n"; badge.textContent = ""; }
    }

    let act;
    try { act = await S.bg.get("/api/agent-activity/" + S.selItem); }
    catch (e) { return; }
    renderFeed(act);
  }

  function renderFeed(act, force) {
    const feed = q("gp-feed");
    if (!feed) return;
    act = act || { steps: [], running: false, final: null };
    const steps = act.steps || [];
    const key = (act.running ? "L" : "") + steps.length + "|" +
                (steps.length ? JSON.stringify(steps[steps.length - 1]) : "") + "|" +
                (act.final ? JSON.stringify(act.final).length : 0);
    if (!force && key === S.activityKey) return;
    S.activityKey = key;

    if (!steps.length && !act.final) {
      const item = (S.items || []).find(i => i.id === S.selItem);
      feed.innerHTML = `<div class="gp-empty">${item ? "no agent activity yet - dispatch to start" : "pick a gameplay work item"}</div>`;
      return;
    }
    let html = steps.map(s => {
      if (s.kind === "tool") return `<div class="gp-step tool"><span class="gp-tag">${esc(s.name)}</span>${esc(s.hint || "")}</div>`;
      if (s.kind === "result") return `<div class="gp-step result"><span class="gp-tag">↳</span>${esc(s.text)}</div>`;
      if (s.kind === "steer") return `<div class="gp-step steer"><span class="gp-tag">steer</span>${esc(s.text)}</div>`;
      return `<div class="gp-step say">${esc(s.text)}</div>`;
    }).join("");
    if (act.final) {
      const bad = act.final.subtype && act.final.subtype !== "success";
      const cost = act.final.cost != null ? ` · $${Number(act.final.cost).toFixed(3)}` : "";
      const turns = act.final.turns != null ? ` · ${act.final.turns} turns` : "";
      html += `<div class="gp-final ${bad ? "bad" : ""}"><b>${bad ? "✗ " + esc(act.final.subtype) : "✓ done"}</b>${esc(cost + turns)}<br>${esc(act.final.text || "")}</div>`;
    }
    feed.innerHTML = html;
    feed.scrollTop = feed.scrollHeight;
  }

  async function dispatchItem() {
    if (!S.selItem) { S.bg.toast("no work item selected", true); return; }
    let r;
    try { r = await S.bg.post(`/api/queue/${S.selItem}/dispatch`, {}); }
    catch (e) { r = { ok: false, error: (e && e.message) }; }
    if (r && r.ok) { S.bg.toast(`dispatched #${S.selItem}`); S.activityKey = null; loadQueue(); }
    else S.bg.toast((r && r.error) || "dispatch failed", true);
  }

  /* The three verbs that were missing next to a finished run: see what it
   * changed, send it back with a reason, or call it off. All three answer the
   * {ok,data} envelope, so a refusal shows its sentence instead of nothing. */
  function envData(r) { return (r && r.ok === true && "data" in r) ? r.data : r; }
  function envErr(r) {
    if (!r) return "no response from the server";
    if (r.ok === false || r.error) {
      const e = r.error;
      if (!e) return "request failed";
      return typeof e === "string" ? e : (e.message || e.code || "request failed");
    }
    return null;
  }

  async function showDiff() {
    const out = q("gp-diff-out");
    if (!S.selItem) { S.bg.toast("no work item selected", true); return; }
    if (!out) return;
    out.style.display = "block";
    out.innerHTML = "reading what this run changed…";
    let r;
    try { r = await S.bg.get(`/api/queue/${S.selItem}/diff`); }
    catch (e) { r = { ok: false, error: { message: (e && e.message) || "diff failed" } }; }
    const err = envErr(r);
    if (err) { out.innerHTML = `<span class="gp-err">${esc(err)}</span>`; return; }
    const d = envData(r) || {};
    if (d.available === false) {
      out.innerHTML = `<span class="gp-err">${esc(d.reason || "no diff available")}</span>`;
      return;
    }
    const files = d.files || [];
    if (!files.length) { out.innerHTML = "this run changed nothing on disk."; return; }
    out.innerHTML = files.map(f => {
      const head = `<b>${esc(f.path)}</b> <span class="gp-tag">${esc(f.status || "")}</span>` +
        (f.binary ? ` <span class="gp-tag">binary ${esc(f.bytes_delta != null ? f.bytes_delta + " bytes" : "")}</span>`
                  : ` <span class="gp-good">+${f.added || 0}</span> <span class="gp-err">-${f.removed || 0}</span>`);
      const body = f.binary || !f.diff ? "" : `\n${esc(String(f.diff).slice(0, 4000))}`;
      return `${head}${body}`;
    }).join("\n\n");
  }

  async function reopenItem() {
    if (!S.selItem) { S.bg.toast("no work item selected", true); return; }
    // null is "backed out"; "" is a confirmed empty reason, which reopen accepts.
    const reason = await S.bg.askText({
      title: `Reopen #${S.selItem} - what still has to be fixed?`,
      body: "The reason is appended to the brief the next agent reads.",
      label: "reason", ok: "reopen" });
    if (reason == null) return;
    let r;
    try { r = await S.bg.post(`/api/queue/${S.selItem}/reopen`, { reason }); }
    catch (e) { r = { ok: false, error: { message: (e && e.message) } }; }
    const err = envErr(r);
    if (err) { S.bg.toast(err, true); return; }
    S.bg.toast(`#${S.selItem} reopened`);
    S.activityKey = null;
    loadQueue();
  }

  async function cancelItem() {
    if (!S.selItem) { S.bg.toast("no work item selected", true); return; }
    if (!await S.bg.askConfirm({
      title: `Cancel #${S.selItem}?`,
      body: "A live agent is stopped first; the item stays in the queue as cancelled.",
      ok: "cancel the item", cancel: "leave it", danger: true })) return;
    let r;
    try { r = await S.bg.post(`/api/queue/${S.selItem}/cancel`, { reason: "cancelled from the gameplay seat" }); }
    catch (e) { r = { ok: false, error: { message: (e && e.message) } }; }
    const err = envErr(r);
    if (err) { S.bg.toast(err, true); return; }
    S.bg.toast(`#${S.selItem} cancelled${r && r.agent_stopped ? " · agent stopped" : ""}`);
    S.activityKey = null;
    loadQueue();
  }

  async function stopItem() {
    if (!S.selItem) return;
    if (!liveAgentFor(S.selItem)) { S.bg.toast("no live agent to stop", true); return; }
    let r;
    try { r = await S.bg.post(`/api/queue/${S.selItem}/stop`, {}); }
    catch (e) { r = { ok: false, error: (e && e.message) }; }
    if (r && r.ok) { S.bg.toast(`stopped #${S.selItem}`); S.activityKey = null; pollAgent(); }
    else S.bg.toast((r && r.error) || "stop failed", true);
  }

  async function steerItem() {
    const inp = q("gp-steer");
    const text = inp ? inp.value.trim() : "";
    if (!S.selItem) { S.bg.toast("no work item selected", true); return; }
    if (!liveAgentFor(S.selItem)) { S.bg.toast("no live agent to steer", true); return; }
    if (!text) return;
    let r;
    try { r = await S.bg.post(`/api/queue/${S.selItem}/steer`, { text }); }
    catch (e) { r = { ok: false, error: (e && e.message) }; }
    if (r && r.ok) { if (inp) inp.value = ""; S.bg.toast("steered"); S.activityKey = null; pollAgent(); }
    else S.bg.toast((r && r.error) || "steer failed", true);
  }
})();
