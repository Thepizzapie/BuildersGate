/* Builders Gate — Game editor workspace (StudioFlows.game)
 * A Bezi/Godot-style panel editor: top toolbar · left project tree · center
 * viewport (embedded WASM build / screenshot) · right inspector (Script /
 * Resource / Run / Agent). Frontend-only, vanilla JS, wired to /api/godot/*,
 * /api/play/*, /api/queue. Never throws — every fetch and DOM op is guarded.
 */
(function () {
  window.StudioFlows = window.StudioFlows || {};
  if (window.StudioFlows.game && window.StudioFlows.game.__full) return;

  const LS_FILE = "fge-last-file";
  const SCRIPT_EXT = ["gd", "cfg", "json", "txt", "md", "import", "cs", "gdshader", "ini"];
  const SCENE_EXT = ["tscn", "tres", "res", "scn"];
  const IMG_EXT = ["png", "jpg", "jpeg", "webp", "svg", "bmp"];
  const RUN_SKELETON =
`extends SceneTree

# SceneTree probe — runs headless, prints, then quits.
func _init():
    print("── probe start ──")
    var root := get_root()
    print("root: ", root)
    # Load & count a scene's nodes:
    var ps := load("res://scenes/main.tscn")
    if ps:
        var inst = ps.instantiate()
        print("main.tscn nodes: ", _count(inst))
        inst.free()
    print("── probe end ──")
    quit()

func _count(n) -> int:
    var c := 1
    for ch in n.get_children():
        c += _count(ch)
    return c
`;

  const ext = name => (String(name).split(".").pop() || "").toLowerCase();
  const kindOf = name => {
    const e = ext(name);
    if (SCENE_EXT.includes(e)) return "scene";
    if (IMG_EXT.includes(e)) return "image";
    if (SCRIPT_EXT.includes(e)) return "script";
    return "file";
  };
  const glyphOf = name => {
    const e = ext(name), k = kindOf(name);
    if (e === "gd") return "◈";
    if (k === "scene") return "❖";
    if (k === "image") return "▦";
    if (e === "json" || e === "cfg" || e === "ini" || e === "import") return "⚙";
    return "·";
  };
  const fmtBytes = b => {
    b = Number(b) || 0;
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(b < 10240 ? 1 : 0) + " KB";
    return (b / 1048576).toFixed(1) + " MB";
  };

  function build(host, api) {
    if (!host) return;
    // ── local, guarded service wrappers (fall back if api is partial) ──
    const esc = (api && api.esc) || (s => String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
    const get = (api && api.get) || (async p => { try { const r = await fetch(p); return r.ok ? r.json() : {}; } catch (e) { return {}; } });
    const post = (api && api.post) || (async (p, b) => { try { const r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) }); return r.json().catch(() => ({ ok: r.ok })); } catch (e) { return { ok: false }; } });
    const toast = (api && api.toast) || ((m) => { try { console.log(m); } catch (e) {} });

    injectStyle();

    const S = {                        // per-build state
      tab: "script", lastShot: null, viewport: "idle",
      resourcePayload: null, resourceLabel: "Resource",
      openRel: null,
    };
    const $ = sel => { try { return host.querySelector(sel); } catch (e) { return null; } };

    host.innerHTML = `
      <div class="fge-root">
        <div class="fge-top">
          <div class="fge-brand"><span class="fge-glyph">⌖</span><span class="fge-brandtxt">GAME EDITOR</span></div>
          <div class="fge-godot" id="fge-godot"><span class="fge-lamp"></span><span class="fge-gtxt">checking…</span></div>
          <div class="fge-tsep"></div>
          <div class="fge-tgroup">
            <span class="fge-buildpill" id="fge-buildpill">build …</span>
            <button class="fge-btn" id="fge-rebuild">↻ Rebuild</button>
            <button class="fge-btn" id="fge-check">✓ Build check</button>
            <button class="fge-btn" id="fge-shot">◉ Screenshot</button>
          </div>
          <div class="fge-tstatus" id="fge-tstatus"></div>
        </div>
        <div class="fge-main">
          <div class="fge-left">
            <div class="fge-phdr"><span>Project</span><span class="fge-proj" id="fge-proj"></span></div>
            <div class="fge-tree" id="fge-tree"><div class="fge-muted">loading tree…</div></div>
          </div>
          <div class="fge-center">
            <div class="fge-vbar">
              <button class="fge-vbtn" id="fge-vlive">◐ Live</button>
              <button class="fge-vbtn" id="fge-vshot">▦ Shot</button>
              <span class="fge-vsp"></span>
              <button class="fge-vbtn" id="fge-vreload" title="reload build">↻</button>
              <span class="fge-vhint">F1 = live tuning</span>
            </div>
            <div class="fge-stage" id="fge-stage"></div>
          </div>
          <div class="fge-right">
            <div class="fge-tabs" id="fge-tabs">
              <button class="fge-tab" data-tab="script">Script</button>
              <button class="fge-tab" data-tab="resource">Resource</button>
              <button class="fge-tab" data-tab="run">Run</button>
              <button class="fge-tab" data-tab="agent">Agent</button>
            </div>
            <div class="fge-panel" id="fge-panel"></div>
          </div>
        </div>
      </div>`;

    /* ────────────── top bar ────────────── */
    (async () => {
      const st = await get("/api/godot/status");
      const god = $("#fge-godot");
      if (god) {
        const ok = !!st.available;
        god.classList.toggle("bad", !ok);
        const lamp = god.querySelector(".fge-lamp"); if (lamp) lamp.classList.toggle("off", !ok);
        const t = god.querySelector(".fge-gtxt");
        if (t) t.textContent = ok ? ("Godot " + (st.version ? String(st.version).split(".").slice(0, 3).join(".") : "ready")) : "Godot unavailable";
      }
      const proj = $("#fge-proj");
      if (proj && st.project) proj.textContent = String(st.project).replace(/\\/g, "/").split("/").pop();
    })();

    (async () => {
      const ps = await get("/api/play/status");
      const pill = $("#fge-buildpill");
      if (pill) {
        if (ps.built === false) { pill.textContent = "build: none"; pill.className = "fge-buildpill warn"; }
        else if (ps.stale) { pill.textContent = "build: stale"; pill.className = "fge-buildpill warn"; }
        else if (ps.built) { pill.textContent = "build: fresh"; pill.className = "fge-buildpill good"; }
        else { pill.textContent = "build: —"; pill.className = "fge-buildpill"; }
      }
    })();

    const setStatus = (msg, cls) => { const el = $("#fge-tstatus"); if (el) { el.textContent = msg || ""; el.className = "fge-tstatus" + (cls ? " " + cls : ""); } };

    const rebuildBtn = $("#fge-rebuild");
    if (rebuildBtn) rebuildBtn.onclick = async () => {
      if (rebuildBtn.disabled) return;
      rebuildBtn.disabled = true;
      const orig = rebuildBtn.innerHTML;
      rebuildBtn.innerHTML = `<span class="fge-spin"></span> building`;
      setStatus("rebuilding web export…", "");
      const r = await post("/api/play/rebuild", {});
      rebuildBtn.disabled = false; rebuildBtn.innerHTML = orig;
      const pill = $("#fge-buildpill");
      if (r && r.ok) {
        if (pill) { pill.textContent = "build: fresh"; pill.className = "fge-buildpill good"; }
        setStatus("rebuilt · " + (r.bytes != null ? fmtBytes(r.bytes) : "ok") + (r.wasm ? " wasm" : ""), "good");
        toast("web build refreshed");
        if (S.viewport === "live") bootLive();   // reload the running build
      } else {
        setStatus("rebuild failed", "bad");
        toast("rebuild failed", true);
      }
    };

    const checkBtn = $("#fge-check");
    if (checkBtn) checkBtn.onclick = async () => {
      if (checkBtn.disabled) return;
      checkBtn.disabled = true;
      const orig = checkBtn.innerHTML;
      checkBtn.innerHTML = `<span class="fge-spin"></span> checking`;
      setStatus("running project check…", "");
      const r = await post("/api/godot/check", {});
      checkBtn.disabled = false; checkBtn.innerHTML = orig;
      const errs = (r && r.errors) || [];
      if (r && r.ok && errs.length === 0) {
        setStatus("✓ project check clean" + (r.seconds != null ? " · " + r.seconds + "s" : ""), "good");
      } else {
        setStatus("✕ " + (errs.length || "check") + " error" + (errs.length === 1 ? "" : "s"), "bad");
      }
      S.resourcePayload = { __check: true, ok: !!(r && r.ok), errors: errs, seconds: r && r.seconds, output: r && r.output };
      S.resourceLabel = "Build check";
      selectTab("resource");
    };

    const shotBtn = $("#fge-shot");
    if (shotBtn) shotBtn.onclick = () => captureShot();

    async function captureShot() {
      setStatus("capturing screenshot…", "");
      const r = await post("/api/godot/screenshot", {});
      if (r && r.ok && r.rel) {
        S.lastShot = r.rel;
        showShot();
        setStatus("screenshot captured", "good");
      } else {
        setStatus("screenshot failed", "bad");
        toast("screenshot failed", true);
      }
    }

    /* ────────────── viewport ────────────── */
    const stage = $("#fge-stage");
    function setVpButtons() {
      const l = $("#fge-vlive"), s = $("#fge-vshot");
      if (l) l.classList.toggle("on", S.viewport === "live");
      if (s) s.classList.toggle("on", S.viewport === "shot");
    }
    function bootLive() {
      if (!stage) return;
      S.viewport = "live";
      stage.innerHTML = `<iframe class="fge-frame" src="/play/?t=${Date.now()}" allow="autoplay; fullscreen; gamepad"></iframe>`;
      setVpButtons();
    }
    function showShot() {
      if (!stage) return;
      if (!S.lastShot) { captureShot(); return; }
      S.viewport = "shot";
      stage.innerHTML = `<img class="fge-shotimg" src="/api/preview?rel=${encodeURIComponent(S.lastShot)}&t=${Date.now()}"
        onerror="this.replaceWith(document.createTextNode(''))">`;
      setVpButtons();
    }
    function idleStage() {
      if (!stage) return;
      S.viewport = "idle";
      stage.innerHTML = `<div class="fge-boot">
        <button class="fge-bootbtn" id="fge-bootbtn">▶ Boot build</button>
        <div class="fge-bootsub">loads the embedded WASM game · press F1 in-game for live tuning</div>
      </div>`;
      const b = $("#fge-bootbtn"); if (b) b.onclick = bootLive;
      setVpButtons();
    }
    const vLive = $("#fge-vlive"); if (vLive) vLive.onclick = bootLive;
    const vShot = $("#fge-vshot"); if (vShot) vShot.onclick = showShot;
    const vReload = $("#fge-vreload"); if (vReload) vReload.onclick = () => { if (S.viewport === "shot") captureShot(); else bootLive(); };
    idleStage();

    /* ────────────── left: project tree ────────────── */
    (async () => {
      const treeEl = $("#fge-tree");
      if (!treeEl) return;
      const data = await get("/api/godot/files?kind=all");
      const nodes = (data && data.tree) || [];
      if (!nodes.length) { treeEl.innerHTML = `<div class="fge-muted">no files — godot project unavailable</div>`; return; }
      treeEl.innerHTML = renderTree(nodes, 0);
      // delegated interactions
      treeEl.onclick = ev => {
        const dir = ev.target.closest(".fge-tdir");
        if (dir && treeEl.contains(dir)) {
          dir.classList.toggle("open");
          const kids = dir.nextElementSibling;
          if (kids && kids.classList.contains("fge-tkids")) kids.style.display = dir.classList.contains("open") ? "" : "none";
          return;
        }
        const file = ev.target.closest(".fge-tfile");
        if (file && treeEl.contains(file)) {
          treeEl.querySelectorAll(".fge-tfile.sel").forEach(n => n.classList.remove("sel"));
          file.classList.add("sel");
          openEntry(file.getAttribute("data-rel"), file.getAttribute("data-kind"));
        }
      };
      // restore last-opened file
      let last = null; try { last = localStorage.getItem(LS_FILE); } catch (e) {}
      if (last) {
        const el = treeEl.querySelector('.fge-tfile[data-rel="' + cssEsc(last) + '"]');
        if (el) {
          // expand ancestors
          let p = el.parentElement;
          while (p && p !== treeEl) {
            if (p.classList && p.classList.contains("fge-tkids")) { p.style.display = ""; const d = p.previousElementSibling; if (d) d.classList.add("open"); }
            p = p.parentElement;
          }
          el.classList.add("sel");
          openEntry(last, el.getAttribute("data-kind"));
        }
      }
    })();

    function renderTree(nodes, depth) {
      // dirs first, then files; both alphabetical
      const dirs = nodes.filter(n => n && n.dir).sort((a, b) => a.name.localeCompare(b.name));
      const files = nodes.filter(n => n && !n.dir).sort((a, b) => a.name.localeCompare(b.name));
      let out = "";
      const pad = depth * 12 + 8;
      for (const d of dirs) {
        out += `<div class="fge-tdir" style="padding-left:${pad}px"><span class="fge-caret">▸</span><span class="fge-dname">${esc(d.name)}</span></div>`;
        out += `<div class="fge-tkids" style="display:none">${renderTree(d.children || [], depth + 1)}</div>`;
      }
      for (const f of files) {
        const k = kindOf(f.name);
        out += `<div class="fge-tfile" data-rel="${esc(f.rel)}" data-kind="${k}" style="padding-left:${pad + 12}px" title="${esc(f.rel)}">
          <span class="fge-ficon k-${k}">${glyphOf(f.name)}</span>
          <span class="fge-fname">${esc(f.name)}</span>
          <span class="fge-fsize">${fmtBytes(f.bytes)}</span></div>`;
      }
      return out;
    }
    const cssEsc = s => String(s).replace(/["\\]/g, "\\$&");

    /* open a tree entry: script/image → Script tab · scene/tres → Resource tab */
    async function openEntry(rel, kind) {
      if (!rel) return;
      S.openRel = rel;
      try { localStorage.setItem(LS_FILE, rel); } catch (e) {}
      if (kind === "scene") {
        selectTab("resource");
        S.resourceLabel = "Resource";
        S.resourcePayload = { __loading: true, rel };
        renderPanel();
        const r = await post("/api/godot/inspect", { res_path: "res://" + rel });
        S.resourcePayload = Object.assign({ __rel: rel }, r || {});
        if (S.tab === "resource") renderPanel();
      } else if (kind === "image") {
        selectTab("script");
        S.scriptPayload = { __image: true, rel };
        renderPanel();
      } else {
        selectTab("script");
        S.scriptPayload = { __loading: true, rel };
        renderPanel();
        const d = await get("/api/godot/file?rel=" + encodeURIComponent(rel));
        S.scriptPayload = Object.assign({ __rel: rel }, d || {});
        if (S.tab === "script") renderPanel();
      }
    }

    /* ────────────── right: inspector tabs ────────────── */
    const tabsEl = $("#fge-tabs");
    if (tabsEl) tabsEl.onclick = ev => { const b = ev.target.closest(".fge-tab"); if (b) selectTab(b.getAttribute("data-tab")); };
    function selectTab(tab) {
      S.tab = tab;
      host.querySelectorAll(".fge-tab").forEach(t => t.classList.toggle("active", t.getAttribute("data-tab") === tab));
      renderPanel();
    }
    function renderPanel() {
      const p = $("#fge-panel"); if (!p) return;
      if (S.tab === "script") p.innerHTML = viewScript();
      else if (S.tab === "resource") p.innerHTML = viewResource();
      else if (S.tab === "run") { p.innerHTML = viewRun(); wireRun(); }
      else if (S.tab === "agent") { p.innerHTML = `<div class="fge-muted">loading work items…</div>`; loadAgent(); }
    }

    function viewScript() {
      const d = S.scriptPayload;
      if (!d) return `<div class="fge-empty">Select a script, config, or image in the tree.</div>`;
      const name = (d.rel || d.__rel || "").split("/").pop();
      if (d.__image) return `<div class="fge-ph">${esc(name)}</div>
        <img class="fge-imgview" src="/api/preview?rel=${encodeURIComponent(d.rel)}" onerror="this.style.opacity=.15">
        <div class="fge-note">${esc(d.rel)}</div>`;
      if (d.__loading) return `<div class="fge-muted">loading ${esc(name)}…</div>`;
      const text = d.text || "";
      const lines = text.split("\n");
      const gutter = lines.map((_, i) => i + 1).join("\n");
      return `<div class="fge-ph">${esc(name)} <span class="fge-phm">${d.bytes != null ? fmtBytes(d.bytes) : ""} · ${lines.length} lines</span></div>
        ${d.truncated ? `<div class="fge-trunc">⚠ truncated — showing first ${fmtBytes((text.length))} of file</div>` : ""}
        <div class="fge-code"><pre class="fge-gutter">${esc(gutter)}</pre><pre class="fge-src">${esc(text)}</pre></div>`;
    }

    function viewResource() {
      const d = S.resourcePayload;
      if (!d) return `<div class="fge-empty">Click a <b>.tscn</b>/<b>.tres</b> in the tree to load it in-engine, or run <b>Build check</b>.</div>`;
      if (d.__check) {
        if (d.ok && (!d.errors || !d.errors.length)) return `<div class="fge-ph">Build check</div>
          <div class="fge-okbox">✓ project check clean${d.seconds != null ? " · " + esc(d.seconds) + "s" : ""}</div>
          ${d.output ? `<pre class="fge-out">${esc(String(d.output).slice(0, 4000))}</pre>` : ""}`;
        const errs = d.errors || [];
        return `<div class="fge-ph">Build check <span class="fge-phm">${errs.length} error${errs.length === 1 ? "" : "s"}</span></div>
          <div class="fge-errlist">${errs.map(e => `<div class="fge-err">${esc(typeof e === "string" ? e : JSON.stringify(e))}</div>`).join("") || `<div class="fge-muted">no detail</div>`}</div>
          ${d.output ? `<pre class="fge-out">${esc(String(d.output).slice(0, 4000))}</pre>` : ""}`;
      }
      if (d.__loading) return `<div class="fge-muted">loading ${esc((d.rel || "").split("/").pop())} in engine…</div>`;
      const name = (d.__rel || d.resource || "").split("/").pop();
      if (d.ok === false || d.error) return `<div class="fge-ph">${esc(name)}</div>
        <div class="fge-errbox">✕ ${esc(d.error || "engine could not load this resource")}</div>`;
      const kv = (k, v) => `<div class="fge-kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`;
      const meshes = d.meshes || [];
      let body = `<div class="fge-ph">${esc(name)}</div>`;
      body += kv("resource", d.resource || d.__rel || "—");
      body += kv("root", d.root || "—");
      body += kv("root type", d.root_type || "—");
      body += kv("meshes", meshes.length);
      body += kv("total tris", (d.total_tris != null ? d.total_tris.toLocaleString() : "0"));
      if (meshes.length) {
        body += `<div class="fge-sub">Meshes</div><div class="fge-meshes">`;
        for (const m of meshes) {
          body += `<div class="fge-mesh"><span class="fge-mn">${esc(m.name || "mesh")}</span>
            <span class="fge-mt">${(m.tris != null ? m.tris.toLocaleString() : "?")} tris · ${m.surfaces != null ? m.surfaces : "?"} surf${m.aabb_size ? " · " + esc(Array.isArray(m.aabb_size) ? m.aabb_size.map(n => (+n).toFixed(1)).join("×") : m.aabb_size) : ""}</span></div>`;
        }
        body += `</div>`;
      } else {
        body += `<div class="fge-note">No mesh geometry (2D scene or node-only resource).</div>`;
      }
      return body;
    }

    function viewRun() {
      const prev = (S.runScript != null ? S.runScript : RUN_SKELETON);
      return `<div class="fge-ph">SceneTree probe</div>
        <div class="fge-runhint">GDScript that <b>extends SceneTree</b> and calls <b>quit()</b>. Runs headless in the project.</div>
        <textarea class="fge-runta" id="fge-runta" spellcheck="false">${esc(prev)}</textarea>
        <div class="fge-runbar"><button class="fge-btn primary" id="fge-runbtn">▶ Run</button>
          <button class="fge-btn ghost" id="fge-runreset">reset</button>
          <span class="fge-runstat" id="fge-runstat"></span></div>
        <div class="fge-runout" id="fge-runout">${S.runResult ? renderRunResult(S.runResult) : `<div class="fge-muted">output appears here</div>`}</div>`;
    }
    function renderRunResult(r) {
      const parts = [];
      const code = r.exit_code;
      parts.push(`<div class="fge-runmeta ${code === 0 ? "good" : "bad"}">exit ${code == null ? "?" : code} ${r.ok ? "✓" : "✕"}</div>`);
      if (r.stdout) parts.push(`<div class="fge-outlbl">stdout</div><pre class="fge-out">${esc(String(r.stdout).slice(0, 6000))}</pre>`);
      const errBlob = (r.errors && r.errors.length) ? (Array.isArray(r.errors) ? r.errors.join("\n") : String(r.errors)) : "";
      if (errBlob) parts.push(`<div class="fge-outlbl bad">errors</div><pre class="fge-out bad">${esc(errBlob.slice(0, 6000))}</pre>`);
      if (r.stderr) parts.push(`<div class="fge-outlbl">stderr</div><pre class="fge-out">${esc(String(r.stderr).slice(0, 4000))}</pre>`);
      return parts.join("");
    }
    function wireRun() {
      const ta = $("#fge-runta");
      if (ta) ta.oninput = () => { S.runScript = ta.value; };
      const reset = $("#fge-runreset");
      if (reset) reset.onclick = () => { S.runScript = RUN_SKELETON; if (ta) ta.value = RUN_SKELETON; };
      const btn = $("#fge-runbtn");
      if (btn) btn.onclick = async () => {
        if (btn.disabled) return;
        const script = (ta && ta.value) || S.runScript || RUN_SKELETON;
        S.runScript = script;
        btn.disabled = true;
        const stat = $("#fge-runstat"); if (stat) stat.innerHTML = `<span class="fge-spin"></span> running`;
        const out = $("#fge-runout"); if (out) out.innerHTML = `<div class="fge-muted">running…</div>`;
        const r = await post("/api/godot/run", { script });
        btn.disabled = false;
        S.runResult = r || { ok: false, exit_code: null };
        if (stat) stat.textContent = (r && r.ok) ? "done" : "failed";
        const out2 = $("#fge-runout"); if (out2) out2.innerHTML = renderRunResult(S.runResult);
      };
    }

    async function loadAgent() {
      const p = $("#fge-panel"); if (!p || S.tab !== "agent") return;
      const [q, ag] = await Promise.all([get("/api/queue"), get("/api/agents")]);
      if (S.tab !== "agent") return;
      const live = {};
      ((ag && ag.agents) || []).forEach(a => { if (a && a.item_id != null) live[a.item_id] = a.state || "running"; });
      const items = ((q && q.items) || []).filter(i => i && (i.seat === "gameplay" || i.seat === "tech") && i.status !== "done");
      let html = `<div class="fge-ph">Gameplay / Tech work <span class="fge-phm">${items.length}</span></div>`;
      if (!items.length) html += `<div class="fge-empty">No open gameplay or tech work items in the queue.</div>`;
      else html += `<div class="fge-items">` + items.map(it => {
        const running = live[it.id];
        const queued = it.status === "queued";
        return `<div class="fge-item">
          <div class="fge-itop"><span class="fge-seatdot k-${esc(it.seat)}"></span>
            <span class="fge-ititle">${esc(it.title)}</span>
            <span class="fge-istat ${running ? "live" : ""}">${running ? "● " + esc(running) : esc(it.status)}</span></div>
          <div class="fge-ibrief">${esc((it.brief || "").slice(0, 160))}</div>
          <div class="fge-iactions">
            ${queued ? `<button class="fge-btn small primary" data-act="dispatch" data-id="${it.id}">dispatch</button>` : ""}
            <button class="fge-btn small ghost" data-act="watch" data-id="${it.id}">watch</button>
          </div></div>`;
      }).join("") + `</div>`;
      p.innerHTML = html;
      p.querySelectorAll("[data-act]").forEach(b => b.onclick = async () => {
        const id = b.getAttribute("data-id"), act = b.getAttribute("data-act");
        if (act === "watch") { try { if (typeof window.watchAgent === "function") window.watchAgent(Number(id)); else toast("watch unavailable", true); } catch (e) {} return; }
        if (act === "dispatch") {
          b.disabled = true; b.textContent = "…";
          const r = await post(`/api/queue/${id}/dispatch`, {});
          toast(r && (r.ok !== false) ? "agent dispatched" : "dispatch failed", !(r && r.ok !== false));
          try { if (typeof window.watchAgent === "function") window.watchAgent(Number(id)); } catch (e) {}
          if (S.tab === "agent") loadAgent();
        }
      });
    }

    // initial inspector state
    selectTab(S.tab);
  }

  function injectStyle() {
    if (document.getElementById("flow-game-style")) return;
    const s = document.createElement("style");
    s.id = "flow-game-style";
    s.textContent = `
    .fge-root{display:flex;flex-direction:column;height:100%;min-height:420px;border:1px solid var(--seam);border-radius:12px;overflow:hidden;background:var(--iron);font-family:var(--sans);color:var(--bone)}
    /* top bar */
    .fge-top{display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--plate);border-bottom:1px solid var(--seam);flex:none;flex-wrap:wrap}
    .fge-brand{display:flex;align-items:center;gap:8px;padding-right:10px;border-right:1px solid var(--seam)}
    .fge-glyph{color:var(--ember);font-size:16px;line-height:1}
    .fge-brandtxt{font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--ash);font-weight:600}
    .fge-godot{display:flex;align-items:center;gap:7px;font-family:var(--mono);font-size:11px;color:var(--ash)}
    .fge-lamp{width:7px;height:7px;border-radius:50%;background:var(--good);box-shadow:0 0 7px var(--good)}
    .fge-lamp.off{background:var(--bad);box-shadow:0 0 7px var(--bad)}
    .fge-godot.bad .fge-gtxt{color:var(--bad)}
    .fge-tsep{width:1px;align-self:stretch;background:var(--seam);margin:2px 0}
    .fge-tgroup{display:flex;align-items:center;gap:7px}
    .fge-buildpill{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;border:1px solid var(--seam2);color:var(--ash);background:var(--void)}
    .fge-buildpill.good{color:var(--good);border-color:rgba(70,192,138,.4)}
    .fge-buildpill.warn{color:var(--warn);border-color:rgba(228,166,63,.4)}
    .fge-tstatus{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--ash);text-align:right;min-width:0;flex:1}
    .fge-tstatus.good{color:var(--good)} .fge-tstatus.bad{color:var(--bad)}
    /* buttons */
    .fge-btn{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;background:var(--plate2);border:1px solid var(--seam2);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;cursor:pointer;white-space:nowrap}
    .fge-btn:hover{border-color:var(--ember);background:var(--plate)}
    .fge-btn:disabled{opacity:.6;cursor:default}
    .fge-btn.primary{background:var(--ember);color:#150a06;border-color:var(--ember);font-weight:600}
    .fge-btn.primary:hover{filter:brightness(1.08)}
    .fge-btn.ghost{background:transparent}
    .fge-btn.small{padding:3px 9px;font-size:11px}
    .fge-spin{display:inline-block;width:10px;height:10px;border:2px solid var(--seam2);border-top-color:var(--ember);border-radius:50%;animation:fge-rot .7s linear infinite;vertical-align:middle}
    @keyframes fge-rot{to{transform:rotate(360deg)}}
    /* main columns */
    .fge-main{display:flex;flex:1;min-height:0}
    .fge-left{width:250px;flex:none;display:flex;flex-direction:column;border-right:1px solid var(--seam);background:var(--iron);min-height:0}
    .fge-phdr{display:flex;align-items:center;justify-content:space-between;padding:9px 11px;border-bottom:1px solid var(--seam);font-family:var(--mono);font-size:9px;letter-spacing:.22em;text-transform:uppercase;color:var(--ash2);flex:none}
    .fge-proj{color:var(--ash);letter-spacing:.04em;text-transform:none;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .fge-tree{flex:1;overflow:auto;padding:5px 0;font-family:var(--mono);font-size:11.5px}
    .fge-tdir{display:flex;align-items:center;gap:5px;padding:2px 8px;cursor:pointer;color:var(--ash2);user-select:none}
    .fge-tdir:hover{color:var(--bone)}
    .fge-caret{display:inline-block;transition:transform .12s;font-size:9px;color:var(--ash2)}
    .fge-tdir.open .fge-caret{transform:rotate(90deg)}
    .fge-dname{color:var(--ash)}
    .fge-tdir.open .fge-dname{color:var(--bone)}
    .fge-tfile{display:flex;align-items:center;gap:7px;padding:2px 8px;cursor:pointer;color:var(--ash);border-left:2px solid transparent}
    .fge-tfile:hover{background:var(--plate);color:var(--bone)}
    .fge-tfile.sel{background:var(--ember-soft);color:var(--bone);border-left-color:var(--ember)}
    .fge-ficon{font-size:11px;width:12px;text-align:center;flex:none;color:var(--ash2)}
    .fge-ficon.k-scene{color:var(--info)} .fge-ficon.k-image{color:var(--c-art)} .fge-ficon.k-script{color:var(--good)}
    .fge-fname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .fge-fsize{color:var(--ash2);font-size:9.5px;flex:none}
    /* center viewport */
    .fge-center{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--void)}
    .fge-vbar{display:flex;align-items:center;gap:6px;padding:7px 10px;border-bottom:1px solid var(--seam);background:var(--plate);flex:none}
    .fge-vbtn{padding:4px 10px;background:var(--plate2);border:1px solid var(--seam2);border-radius:6px;color:var(--ash);font:inherit;font-size:11px;cursor:pointer}
    .fge-vbtn:hover{color:var(--bone);border-color:var(--ember)}
    .fge-vbtn.on{background:var(--ember-soft);color:var(--ember);border-color:var(--ember)}
    .fge-vsp{flex:1}
    .fge-vhint{font-family:var(--mono);font-size:10px;color:var(--ash2)}
    .fge-stage{flex:1;position:relative;display:flex;align-items:center;justify-content:center;background:#050609;overflow:hidden;min-height:0}
    .fge-frame{width:100%;height:100%;border:0;background:#000}
    .fge-shotimg{max-width:100%;max-height:100%;object-fit:contain}
    .fge-boot{text-align:center;padding:20px}
    .fge-bootbtn{padding:12px 26px;background:var(--ember);color:#150a06;border:0;border-radius:10px;font:inherit;font-weight:600;font-size:14px;cursor:pointer}
    .fge-bootbtn:hover{filter:brightness(1.08)}
    .fge-bootsub{margin-top:12px;font-family:var(--mono);font-size:10.5px;color:var(--ash2)}
    /* right inspector */
    .fge-right{width:330px;flex:none;display:flex;flex-direction:column;border-left:1px solid var(--seam);background:var(--iron);min-height:0}
    .fge-tabs{display:flex;flex:none;border-bottom:1px solid var(--seam);background:var(--plate)}
    .fge-tab{flex:1;padding:9px 4px;background:transparent;border:0;border-bottom:2px solid transparent;color:var(--ash2);font:inherit;font-size:11.5px;cursor:pointer}
    .fge-tab:hover{color:var(--bone)}
    .fge-tab.active{color:var(--bone);border-bottom-color:var(--ember)}
    .fge-panel{flex:1;overflow:auto;padding:13px}
    .fge-ph{font-size:12.5px;font-weight:600;color:var(--bone);margin-bottom:10px;display:flex;align-items:baseline;gap:8px;word-break:break-word}
    .fge-phm{font-family:var(--mono);font-size:10px;font-weight:400;color:var(--ash2)}
    .fge-empty,.fge-muted{color:var(--ash2);font-size:12px;line-height:1.5}
    .fge-empty b{color:var(--ash)}
    .fge-note{color:var(--ash2);font-size:11px;margin-top:8px;line-height:1.4;word-break:break-all}
    .fge-sub{font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--ash2);margin:14px 0 7px}
    .fge-kv{display:flex;justify-content:space-between;gap:10px;font-size:12px;padding:5px 0;border-bottom:1px solid var(--seam)}
    .fge-kv span:first-child{color:var(--ash2);font-family:var(--mono);font-size:10.5px}
    .fge-kv span:last-child{color:var(--bone);text-align:right;word-break:break-all}
    .fge-meshes{display:flex;flex-direction:column;gap:5px}
    .fge-mesh{display:flex;flex-direction:column;gap:2px;padding:6px 8px;background:var(--plate);border:1px solid var(--seam);border-radius:7px}
    .fge-mn{font-size:11.5px;color:var(--bone)}
    .fge-mt{font-family:var(--mono);font-size:10px;color:var(--ash)}
    /* code view */
    .fge-trunc{color:var(--warn);font-size:11px;margin-bottom:8px}
    .fge-code{display:flex;background:var(--void);border:1px solid var(--seam);border-radius:8px;overflow:auto;max-height:calc(100% - 40px)}
    .fge-gutter{margin:0;padding:9px 8px;text-align:right;color:var(--ash2);background:var(--plate);font-family:var(--mono);font-size:10.5px;line-height:1.55;user-select:none;flex:none;border-right:1px solid var(--seam)}
    .fge-src{margin:0;padding:9px 11px;font-family:var(--mono);font-size:11px;line-height:1.55;color:#cdd6e4;white-space:pre;flex:1}
    .fge-imgview{width:100%;border-radius:8px;background:#000}
    /* run tab */
    .fge-runhint{font-size:11px;color:var(--ash);line-height:1.5;margin-bottom:9px}
    .fge-runhint b{color:var(--bone)}
    .fge-runta{width:100%;min-height:150px;resize:vertical;background:var(--void);border:1px solid var(--seam);border-radius:8px;color:#cdd6e4;font-family:var(--mono);font-size:11px;line-height:1.5;padding:9px;white-space:pre}
    .fge-runbar{display:flex;align-items:center;gap:8px;margin:9px 0}
    .fge-runstat{font-family:var(--mono);font-size:11px;color:var(--ash)}
    .fge-runout{margin-top:6px}
    .fge-runmeta{font-family:var(--mono);font-size:11px;margin-bottom:7px}
    .fge-runmeta.good{color:var(--good)} .fge-runmeta.bad{color:var(--bad)}
    .fge-outlbl{font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--ash2);margin:8px 0 4px}
    .fge-outlbl.bad{color:var(--bad)}
    .fge-out{margin:0;padding:8px 10px;background:var(--void);border:1px solid var(--seam);border-radius:7px;font-family:var(--mono);font-size:10.5px;line-height:1.5;color:#cdd6e4;white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto}
    .fge-out.bad{color:var(--bad);border-color:rgba(229,100,95,.35)}
    .fge-okbox{color:var(--good);font-size:12px;padding:9px 11px;background:rgba(70,192,138,.08);border:1px solid rgba(70,192,138,.3);border-radius:8px}
    .fge-errbox{color:var(--bad);font-size:12px;padding:9px 11px;background:rgba(229,100,95,.08);border:1px solid rgba(229,100,95,.3);border-radius:8px}
    .fge-errlist{display:flex;flex-direction:column;gap:5px}
    .fge-err{font-family:var(--mono);font-size:10.5px;color:var(--bad);padding:6px 8px;background:rgba(229,100,95,.07);border:1px solid rgba(229,100,95,.25);border-radius:6px;white-space:pre-wrap;word-break:break-word}
    /* agent tab */
    .fge-items{display:flex;flex-direction:column;gap:8px}
    .fge-item{padding:9px 10px;background:var(--plate);border:1px solid var(--seam);border-radius:9px}
    .fge-itop{display:flex;align-items:center;gap:7px;margin-bottom:5px}
    .fge-seatdot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--ash2)}
    .fge-seatdot.k-gameplay{background:var(--c-gameplay)} .fge-seatdot.k-tech{background:var(--c-tech)}
    .fge-ititle{flex:1;font-size:12px;color:var(--bone);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .fge-istat{font-family:var(--mono);font-size:9.5px;color:var(--ash2)}
    .fge-istat.live{color:var(--ember)}
    .fge-ibrief{font-size:11px;color:var(--ash);line-height:1.45;margin-bottom:7px}
    .fge-iactions{display:flex;gap:6px}
    `;
    try { document.head.appendChild(s); } catch (e) {}
  }

  window.StudioFlows.game = { __full: true, label: "Game editor", glyph: "⌖", build };
})();
