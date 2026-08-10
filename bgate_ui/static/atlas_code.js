/* atlas_code.js — Atlas's fourth mode: the code, and the build it produces.
 *
 * The other three modes made the project READABLE. You could see which sheet a
 * screen uses, wire one in, drag a node to where it belongs — and then, for the
 * one edit that is most of game development, alt-tab to Godot. Every code
 * surface in this dashboard was read-only, so the engine had to stay open next
 * to it forever.
 *
 * So this mode is the loop, end to end, in one pane:
 *
 *   THE SCENE NAMES ITS OWN FILES. Picking a scene asks /api/scene/files for
 *   everything it reaches — its ext_resources, plus one hop through the scripts
 *   it attaches, following preload/load. That second hop is the point: a scene
 *   file never mentions the four resources its player script preloads, and
 *   those are the files you were about to go hunting for.
 *
 *   EDIT, SAVE, PLAY, WITHOUT LEAVING. Save writes through /api/godot/file;
 *   the build is /play in an iframe, and rebuilding it reloads that frame in
 *   place. The web export already knew how to tell whether it was older than
 *   the source (webbuild.py), so a stale build says so rather than quietly
 *   playing yesterday.
 *
 * WHAT IT REFUSES TO DO. It does not save a file that changed on disk while a
 * tab held it — an agent writing the same script is not a merge this editor is
 * qualified to perform, so it offers a reload instead. It does not write
 * through a held lock without being told twice. Both are server-side; this is
 * the UI for them, not the enforcement.
 */
window.AtlasCode = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };

  const KIND = {
    script:{ g:"⌁", c:"var(--c-narrative)" }, scene:{ g:"⊞", c:"var(--accent)" },
    resource:{ g:"⬡", c:"var(--good)" },      texture:{ g:"▧", c:"var(--warn)" },
    audio:{ g:"♪", c:"var(--c-narrative)" },  font:{ g:"F", c:"var(--text-3)" },
    shader:{ g:"◐", c:"var(--warn)" },        data:{ g:"·", c:"var(--text-3)" },
    other:{ g:"·", c:"var(--text-3)" },
  };
  // Suffix -> CodeMirror mode. .tscn/.tres are INI-ish section files, which the
  // properties mode reads well enough to colour their [node ...] headers.
  const MODE = {
    gd:"gdscript", cs:"text/x-csharp", tscn:"properties", tres:"properties",
    cfg:"properties", godot:"properties", json:{name:"javascript", json:true},
    gdshader:"text/x-csrc",
  };
  const modeFor = name => MODE[String(name).split(".").pop().toLowerCase()] || null;

  const atlasMap = () => (window.Atlas && Atlas.map) || null;

  let host = null, cm = null, built = false;
  let scene = null, data = null, mainScene = null;
  let tabs = [];          // {rel, name, sha, doc, dirty, lock}
  let active = -1;
  let play = null;        // last /api/play/status

  /* ── styles ───────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("atlascode-style")) return;
    const s = document.createElement("style");
    s.id = "atlascode-style";
    s.textContent = [
      // min-height, not just a vh cap: on a short window 80vh is a pane with
      // four lines of code in it, and the editor is the thing this mode is for.
      ".ac-wrap{display:flex;height:min(80vh,940px);min-height:520px;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--surface-1)}",
      ".ac-side{width:264px;flex:none;border-right:1px solid var(--line);overflow-y:auto;padding:11px;background:var(--surface-1)}",
      ".ac-main{flex:1;min-width:0;display:flex;flex-direction:column}",
      // NOWRAP. With wrap on, the flex:1 spacer and the full path in #ac-state
      // pushed the four buttons onto five rows — a 218px toolbar above a 73px
      // editor. The path truncates instead; it is the least important thing here.
      ".ac-bar{display:flex;gap:7px;align-items:center;flex-wrap:nowrap;padding:8px 10px;border-bottom:1px solid var(--line-soft)}",
      ".ac-bar .ac-b{flex:none}",
      "#ac-state{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:right}",
      ".ac-b{padding:5px 10px;background:var(--surface-3);border:1px solid var(--line);border-radius:7px;color:var(--text);font:inherit;font-size:11.5px;cursor:pointer}",
      ".ac-b:hover:not(:disabled){border-color:var(--accent)}",
      ".ac-b:disabled{opacity:.45;cursor:default}",
      ".ac-b.go{background:var(--accent);color:var(--accent-fg);border-color:var(--accent);font-weight:600}",
      ".ac-in{background:var(--surface-1);border:1px solid var(--line);border-radius:7px;color:var(--text);font:inherit;font-size:11.5px;padding:5px 9px;max-width:290px}",
      ".ac-h{font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--text-3);margin:14px 0 6px}",
      ".ac-h:first-child{margin-top:0}",
      ".ac-f{display:flex;align-items:center;gap:6px;padding:4px 6px;border-radius:6px;cursor:pointer;font-size:11.5px;color:var(--text-2)}",
      ".ac-f:hover{background:var(--surface-3);color:var(--text)}",
      ".ac-f.on{background:var(--accent-soft);color:var(--text)}",
      ".ac-f .g{font-family:var(--mono);font-size:11px;flex:none;width:12px;text-align:center}",
      ".ac-f .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".ac-f .z{font-family:var(--mono);font-size:9px;color:var(--text-dim);flex:none}",
      ".ac-f.ro{opacity:.55;cursor:default}",
      ".ac-f.ro:hover{background:none;color:var(--text-2)}",
      ".ac-f.gone{color:var(--bad)}",
      ".ac-via{font-family:var(--mono);font-size:8.5px;color:var(--text-dim);margin:-2px 0 4px 24px}",
      ".ac-tabs{display:flex;gap:2px;overflow-x:auto;border-bottom:1px solid var(--line-soft);background:var(--surface-1)}",
      ".ac-tab{display:flex;align-items:center;gap:6px;padding:6px 10px;font-size:11px;color:var(--text-3);cursor:pointer;border-right:1px solid var(--line-soft);white-space:nowrap}",
      ".ac-tab.on{color:var(--text);background:var(--surface-2);box-shadow:inset 0 -2px 0 var(--accent)}",
      ".ac-tab .x{color:var(--text-dim);font-family:var(--mono)}",
      ".ac-tab .x:hover{color:var(--bad)}",
      ".ac-tab .dot{width:6px;height:6px;border-radius:50%;background:var(--warn);flex:none}",
      ".ac-ed{flex:1;min-height:0;position:relative}",
      ".ac-ed .CodeMirror{height:100%;font-size:12.5px}",
      ".ac-empty{flex:1;display:flex;align-items:center;justify-content:center;color:var(--text-3);font-size:12px;text-align:center;padding:24px;line-height:1.6}",
      // A class that sets `display` outranks the UA sheet's [hidden]{display:none}
      // — same specificity, author wins — so `.hidden = true` did nothing and the
      // placeholder stayed under the open editor, eating half the pane.
      ".ac-wrap [hidden]{display:none !important}",
      ".ac-play{width:0;flex:none;border-left:1px solid var(--line);display:flex;flex-direction:column;background:var(--surface-1);transition:width .12s}",
      ".ac-play.open{width:420px}",
      ".ac-play iframe{flex:1;border:0;width:100%;background:#000}",
      ".ac-ph{display:flex;gap:6px;align-items:center;padding:7px 9px;border-bottom:1px solid var(--line-soft);font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--text-3)}",
      ".ac-out{max-height:160px;overflow:auto;font-family:var(--mono);font-size:10px;color:var(--text-2);padding:8px 10px;border-top:1px solid var(--line-soft);white-space:pre-wrap;background:var(--surface-1)}",
      ".ac-lock{font-family:var(--mono);font-size:9px;color:var(--warn);padding:0 4px;border:1px solid var(--warn-line);border-radius:4px}",
      ".ac-stale{color:var(--warn)}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── shell ────────────────────────────────────────────────────────────── */
  function build(){
    if (built) return;
    injectStyle();
    host.innerHTML = `
      <div class="ac-wrap">
        <aside class="ac-side">
          <div class="ac-h">scene</div>
          <select class="ac-in" id="ac-scene" style="width:100%"></select>
          <div id="ac-files"></div>
        </aside>
        <div class="ac-main">
          <div class="ac-bar">
            <button class="ac-b go" id="ac-save">save</button>
            <button class="ac-b" id="ac-revert">revert</button>
            <button class="ac-b" id="ac-check">check project</button>
            <button class="ac-b" id="ac-play-t">play ▸</button>
            <span style="flex:1"></span>
            <span id="ac-state" style="font-family:var(--mono);font-size:9.5px;color:var(--text-3)"></span>
          </div>
          <div class="ac-tabs" id="ac-tabs"></div>
          <div class="ac-ed" id="ac-ed" hidden></div>
          <div class="ac-empty" id="ac-blank">
            pick a scene, then a file it touches.<br>
            <span style="font-size:11px">every script the scene attaches, and what those scripts preload.</span>
          </div>
          <div class="ac-out" id="ac-out" hidden></div>
        </div>
        <div class="ac-play" id="ac-play">
          <div class="ac-ph">
            <span id="ac-play-s">build</span>
            <span style="flex:1"></span>
            <button class="ac-b" id="ac-rebuild" style="padding:3px 8px">rebuild</button>
          </div>
          <iframe id="ac-frame" src="about:blank" title="playable build"
                  allow="autoplay; gamepad; fullscreen"></iframe>
        </div>
      </div>`;

    cm = CodeMirror(document.getElementById("ac-ed"), {
      value: "", theme: "bgate", lineNumbers: true, lineWrapping: false,
      matchBrackets: true, autoCloseBrackets: true, styleActiveLine: true,
      // Godot writes hard tabs and GDScript is indentation-significant, so a
      // dashboard that inserted spaces would produce files the engine
      // reformats on its next save — a phantom diff on every round trip.
      indentUnit: 4, indentWithTabs: true, tabSize: 4,
      extraKeys: {
        "Ctrl-S": save, "Cmd-S": save,
        "Ctrl-/": cmirror => cmirror.execCommand("toggleComment"),
        "Cmd-/": cmirror => cmirror.execCommand("toggleComment"),
      },
    });
    cm.on("change", () => {
      const t = tabs[active];
      if (!t || cm.getValue() === t.base) { if (t) t.dirty = false; }
      else t.dirty = true;
      renderTabs(); renderState();
    });

    document.getElementById("ac-scene").onchange = ev => pickScene(ev.target.value);
    document.getElementById("ac-save").onclick = save;
    document.getElementById("ac-revert").onclick = revert;
    document.getElementById("ac-check").onclick = check;
    document.getElementById("ac-rebuild").onclick = rebuild;
    document.getElementById("ac-play-t").onclick = togglePlay;
    built = true;
  }

  /* ── scene + its files ────────────────────────────────────────────────── */
  /* Scenes a person opens the editor for, first. `screens[0]` is alphabetical,
     which on a real project means a portrait proof or a QA fixture wins — the
     two kinds of scene nobody came here to edit. So: the project's declared
     main scene, then everything else, then the fixtures. */
  const SIDELINE = /^res:\/\/(tests?|qa)\//i;
  const PROOF = /(_proof|_test|_sandbox|_demo)\.tscn$/i;

  function rank(id){
    if (id === mainScene) return 0;
    if (SIDELINE.test(id) || PROOF.test(id)) return 2;
    return 1;
  }

  function ordered(screens){
    return screens.slice().sort((a, b) =>
      rank(a.id) - rank(b.id) || a.label.localeCompare(b.label));
  }

  async function activate(){
    host = document.getElementById("atlas-code");
    if (!host) return;
    build();
    /* Same trap the scene builder hit: Atlas.map is only populated once
       something has loaded it, and on startup that is a deferred badge() call
       behind a full-project scan. Open this mode first and the file list renders
       "no scenes found" permanently, because the map arriving fires no
       re-render. ensure() is memoised and single-flight. */
    if (window.Atlas && Atlas.ensure && !atlasMap()){
      try { await Atlas.ensure(); } catch (e) {}
    }
    const map = atlasMap();
    mainScene = (map && map.main_scene) || null;
    const screens = ordered((map && map.screens) || []);
    const sel = document.getElementById("ac-scene");
    /* Rewriting innerHTML on an OPEN <select> destroys its options mid-popup and
       the native list snaps shut — the reported "code closes when trying to
       scroll the list". The dashboard polls on several timers, so with a list
       this long the user cannot reach the bottom of it. Leave the picker alone
       while it has focus; it is re-filled on the next activate() after blur. */
    if (document.activeElement !== sel){
      sel.innerHTML = screens.length
        ? screens.map(s => `<option value="${E(s.id)}"${s.id === scene ? " selected" : ""}>⊞ ${E(s.label)}</option>`).join("")
        : `<option value="">no scenes found</option>`;
      if (!sel.dataset.blurBound){
        sel.dataset.blurBound = "1";
        sel.addEventListener("blur", () => { if (!AtlasCode.dirty || true) activate(); });
      }
    }
    if (!scene && screens.length) await pickScene(screens[0].id);
    else if (scene) renderFiles();
    refreshPlay();
  }

  async function pickScene(id){
    scene = id;
    if (!scene) return;
    const files = document.getElementById("ac-files");
    files.innerHTML = `<div class="ac-h">files</div><div class="ac-via">reading…</div>`;
    const d = await readJSON(`/api/scene/files?scene=${encodeURIComponent(scene)}`, null);
    if (!d || d.__error){
      files.innerHTML = `<div class="ac-h">files</div><div class="ac-via">${E((d && d.__error) || "failed")}</div>`;
      return;
    }
    data = d;
    renderFiles();
  }

  function renderFiles(){
    const el = document.getElementById("ac-files");
    if (!el || !data) return;
    const groups = {};
    (data.files || []).forEach(f => (groups[f.dir] = groups[f.dir] || []).push(f));
    const dirs = Object.keys(groups).sort();
    el.innerHTML = dirs.map(dir => {
      const rows = groups[dir].map(f => {
        const k = KIND[f.kind] || KIND.other;
        const open = tabs.some(t => t.rel === f.edit_rel);
        const cls = ["ac-f", f.editable && f.exists ? "" : "ro",
                     f.exists ? "" : "gone", open ? "on" : ""].join(" ");
        // Only an editable, existing file is a click target — everything else
        // is listed because it IS part of the scene, and hiding it would make
        // the list a lie about what the scene reaches.
        const click = f.editable && f.exists && f.edit_rel
          ? ` onclick="AtlasCode.open('${E(f.edit_rel)}')"` : "";
        const kb = f.bytes ? (f.bytes > 1024 ? Math.round(f.bytes / 1024) + "k" : f.bytes + "b") : "";
        return `<div class="${cls}"${click} title="${E(f.res)}">
            <span class="g" style="color:${k.c}">${k.g}</span>
            <span class="n">${E(f.name)}</span>
            <span class="z">${E(f.exists ? kb : "missing")}</span></div>`
          + (f.via && f.via.startsWith("script:")
              ? `<div class="ac-via">via ${E(f.via.slice(7).split("/").pop())}</div>` : "");
      }).join("");
      return `<div class="ac-h">${E(dir === "." ? "(root)" : dir)}</div>${rows}`;
    }).join("") || `<div class="ac-h">files</div><div class="ac-via">nothing reachable</div>`;
  }

  /* ── tabs ─────────────────────────────────────────────────────────────── */
  /* The server resolves which directory res:// means (godot_ws._default_project,
     the same order screenmap and scenewire use), so nothing here passes a path
     back. It used to send project_dir, which broke under streamer mode for a
     reason worth remembering: the redaction middleware restores real paths into
     inbound JSON BODIES only, so a redacted "<project>\game" survived a POST
     and reached a GET query string intact — the write worked and the read
     404'd. Paths belong in bodies here, or nowhere. */
  const fileURL = rel => `/api/godot/file?rel=${encodeURIComponent(rel)}`;

  async function open(rel){
    const at = tabs.findIndex(t => t.rel === rel);
    if (at >= 0) return focusTab(at);
    const d = await readJSON(fileURL(rel), null);
    if (!d || d.__error) return say((d && d.__error) || `could not read ${rel}`, "bad");
    if (d.truncated) return say(`${rel} is too large to edit here (${d.bytes} bytes)`, "bad");
    tabs.push({
      rel, name: rel.split("/").pop(), sha: d.sha, base: d.text,
      lock: d.lock || null, writable: d.writable !== false,
      doc: CodeMirror.Doc(d.text, modeFor(rel)), dirty: false,
    });
    focusTab(tabs.length - 1);
    renderFiles();
  }

  function focusTab(i){
    if (i < 0 || i >= tabs.length) return;
    active = i;
    cm.swapDoc(tabs[i].doc);
    document.getElementById("ac-ed").hidden = false;
    document.getElementById("ac-blank").hidden = true;
    renderTabs(); renderState();
    cm.refresh(); cm.focus();
  }

  function closeTab(i, ev){
    if (ev) ev.stopPropagation();
    const t = tabs[i];
    if (t && t.dirty && !confirm(`${t.name} has unsaved edits. Close it anyway?`)) return;
    tabs.splice(i, 1);
    if (!tabs.length){
      active = -1;
      document.getElementById("ac-ed").hidden = true;
      document.getElementById("ac-blank").hidden = false;
      renderTabs(); renderState(); renderFiles();
      return;
    }
    focusTab(Math.min(i, tabs.length - 1));
    renderFiles();
  }

  function renderTabs(){
    document.getElementById("ac-tabs").innerHTML = tabs.map((t, i) =>
      `<div class="ac-tab${i === active ? " on" : ""}" onclick="AtlasCode.focus(${i})">
         ${t.dirty ? '<span class="dot"></span>' : ""}
         <span>${E(t.name)}</span>
         ${t.lock ? `<span class="ac-lock">${E(t.lock.seat || "held")}</span>` : ""}
         <span class="x" onclick="AtlasCode.close(${i},event)">×</span>
       </div>`).join("");
  }

  function renderState(){
    const t = tabs[active];
    const n = tabs.filter(x => x.dirty).length;
    document.getElementById("ac-state").textContent =
      !t ? "" : `${t.rel}${n ? ` · ${n} unsaved` : ""}`;
    document.getElementById("ac-save").disabled = !t || !t.dirty;
    document.getElementById("ac-revert").disabled = !t || !t.dirty;
  }

  /* ── save / revert ────────────────────────────────────────────────────── */
  async function save(){
    const t = tabs[active];
    if (!t || !t.dirty) return;
    const text = t.doc.getValue();
    const body = { rel: t.rel, text, base_sha: t.sha };
    let r = await mutate("/api/godot/file", { body, quiet: true });
    // A held lock is a refusal with a name on it, so the second ask can be a
    // real question rather than a retry.
    if (!r.ok && r.status === 423){
      if (!confirm(`${r.error}\n\nSave anyway? The holder may overwrite this.`)) return;
      r = await mutate("/api/godot/file", { body: {...body, force: true}, quiet: true });
    }
    if (!r.ok && r.status === 409){
      say(r.error, "bad");
      if (confirm(`${r.error}\n\nReload it from disk? Your edits in this tab are lost.`))
        await reload(active);
      return;
    }
    if (!r.ok) return say(r.error, "bad");
    t.sha = r.data.sha; t.base = text; t.dirty = false;
    renderTabs(); renderState();
    say(r.data.unchanged ? "no change" : `saved ${t.name}`, "ok");
    if (!r.data.unchanged) refreshPlay();
  }

  async function reload(i){
    const t = tabs[i];
    if (!t) return;
    const d = await readJSON(fileURL(t.rel), null);
    if (!d || d.__error) return say((d && d.__error) || "reload failed", "bad");
    t.doc.setValue(d.text); t.base = d.text; t.sha = d.sha; t.dirty = false;
    renderTabs(); renderState();
  }

  function revert(){
    const t = tabs[active];
    if (!t || !t.dirty) return;
    if (!confirm(`Throw away the edits to ${t.name}?`)) return;
    t.doc.setValue(t.base); t.dirty = false;
    renderTabs(); renderState();
  }

  /* ── check / build / play ─────────────────────────────────────────────── */
  function out(text, tone){
    const el = document.getElementById("ac-out");
    el.hidden = !text;
    el.textContent = text || "";
    el.style.color = tone === "bad" ? "var(--bad)" : "var(--text-2)";
  }

  async function check(){
    out("running the project's own import/parse check…");
    const r = await mutate("/api/godot/check", { body: {}, quiet: true,
                                                 button: "ac-check" });
    if (!r.ok) return out(r.error, "bad");
    const d = r.data || {};
    const errs = d.errors || d.error_lines || [];
    out(errs.length ? errs.join("\n")
                    : (d.ok === false ? (d.error || "check failed")
                                      : "no errors reported"),
        errs.length || d.ok === false ? "bad" : null);
  }

  async function refreshPlay(){
    play = await readJSON("/api/play/status", null);
    const el = document.getElementById("ac-play-s");
    if (!el) return;
    if (!play || play.__error){ el.textContent = "build · unknown"; return; }
    el.innerHTML = !play.built
      ? `build · <span class="ac-stale">${E(play.reason || "none")}</span>`
      : play.stale ? `build · <span class="ac-stale">stale</span>` : "build · current";
  }

  async function rebuild(){
    out("exporting the web build - Godot takes a minute on a cold project…");
    const r = await mutate("/api/play/rebuild", { body: {}, quiet: true,
                                                  button: "ac-rebuild" });
    if (!r.ok || (r.data && r.data.ok === false))
      return out((r.data && (r.data.error || r.data.detail)) || r.error, "bad");
    out("build refreshed");
    await refreshPlay();
    reloadFrame();
  }

  function reloadFrame(){
    const f = document.getElementById("ac-frame");
    if (!f) return;
    // /play 404s as JSON when nothing has been exported, and an iframe renders
    // that as a wall of error text where the game goes. The panel header
    // already says the build is missing; don't say it twice and worse.
    if (play && play.built === false){ f.src = "about:blank"; return; }
    // Cache-bust: the WASM and .pck keep their filenames across exports, so a
    // plain reload replays the build that was just replaced.
    f.src = `/play/index.html?t=${Date.now()}`;
  }

  async function togglePlay(){
    const p = document.getElementById("ac-play");
    const on = p.classList.toggle("open");
    document.getElementById("ac-play-t").textContent = on ? "play ◂" : "play ▸";
    if (on){
      await refreshPlay();
      if (document.getElementById("ac-frame").src === "about:blank") reloadFrame();
    } else {
      // Leaving a running WASM build in a hidden panel keeps a game loop and an
      // audio context alive behind whatever you switch to.
      document.getElementById("ac-frame").src = "about:blank";
    }
    setTimeout(() => cm && cm.refresh(), 150);
  }

  function dirty(){ return tabs.some(t => t.dirty); }

  /* Called when the mode is navigated AWAY from. Closing the play panel already
     blanked the frame; leaving the mode with it open did not, so the build kept
     running — with its audio — behind whatever you switched to. A game loop you
     cannot see is a game loop you cannot stop, and the sound is coming from a
     tab that no longer shows anything playing. */
  function deactivate(){
    const f = document.getElementById("ac-frame");
    if (f && f.getAttribute("src") !== "about:blank") f.src = "about:blank";
    const p = document.getElementById("ac-play");
    if (p) p.classList.remove("open");
    const t = document.getElementById("ac-play-t");
    if (t) t.textContent = "play ▸";
  }

  return { activate, deactivate, open, focus: focusTab, close: closeTab,
           save, dirty };
})();
