/* assetlib.js — the asset library, told the way the project is actually shaped.
 *
 * What it replaced was a flat wall of artifact revisions: one square-cropped
 * tile per generated image, in creation order, saying nothing about whether a
 * thing was a whole sprite sheet or one pose out of twelve, and nothing about
 * whether the game loads it. Three lies of omission on one screen.
 *
 *   THE UNIT IS A FAMILY. Every file in a directory sharing a name prefix is
 *   one thing — pm_paladin's six action sheets, prop_copier's four facings.
 *   The tile shows the SHEET at its real aspect ratio, not a centre crop of
 *   frame one, because a walk cycle cropped square is indistinguishable from
 *   an idle cropped square.
 *
 *   IN USE IS A FIRST-CLASS FACT. Approved and on disk is not shipping. Usage
 *   is derived from the same scan Atlas uses — including paths the game BUILDS
 *   at runtime — so a family says which screens reach it, and an unused one
 *   says so on the tile rather than three clicks in.
 *
 *   REVIEW STATE RIDES ALONG. Where an artifact row exists it is joined on the
 *   file path, so the approve/reject workflow is still one click from here.
 *
 * Frontend-only, guarded end to end. Registered as window.AssetLib.
 */
window.AssetLib = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const prev = rel => `/api/preview?rel=${encodeURIComponent(rel)}`;

  const KIND_GLYPH = { image:"▧", audio:"♪", resource:"⬡" };
  const CAT_GLYPH = { characters:"☻", enemies:"☠", items:"⚔", props:"▤",
                      tiles:"▦", audio:"♪", ui:"⊞", shaders:"◐", vfx:"✦" };
  const SORTS = [
    { id:"name",  label:"name" },
    { id:"new",   label:"newest" },
    { id:"files", label:"most files" },
    { id:"size",  label:"largest" },
  ];

  let data = null, loading = false;
  let view = { cat:"", search:"", status:"", sort:"name", dense:false };
  let openKey = null;

  /* ── styles ───────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("assetlib-style")) return;
    const s = document.createElement("style");
    s.id = "assetlib-style";
    s.textContent = [
      ".al-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}",
      ".al-in{background:var(--void);border:1px solid var(--seam);border-radius:8px;color:var(--bone);font:inherit;font-size:12px;padding:7px 11px;min-width:200px}",
      ".al-in:focus{outline:none;border-color:var(--ember)}",
      ".al-chip{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;padding:5px 10px;border:1px solid var(--seam);border-radius:999px;color:var(--ash);cursor:pointer;background:none;display:inline-flex;gap:6px;align-items:center}",
      ".al-chip:hover{border-color:var(--ember);color:var(--bone)}",
      ".al-chip.on{border-color:var(--ember);color:var(--bone);background:var(--plate)}",
      ".al-chip b{color:var(--bone);font-weight:var(--fw-semi)}",
      ".al-chip.good.on{border-color:var(--good);color:var(--good)}",
      ".al-chip.warn.on{border-color:var(--warn);color:var(--warn)}",
      ".al-rail{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:14px;padding-bottom:11px;border-bottom:1px solid var(--seam)}",
      ".al-sum{font-family:var(--mono);font-size:10px;color:var(--ash2);margin-left:auto}",
      ".al-sec{margin-bottom:22px}",
      ".al-sech{display:flex;align-items:baseline;gap:9px;font-family:var(--mono);font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--ash);margin-bottom:9px;position:sticky;top:0;background:var(--void);padding:5px 0;z-index:2}",
      ".al-sech .g{color:var(--ember);font-size:12px}",
      ".al-sech .n{color:var(--ash2);font-size:9px}",
      ".al-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:12px;align-items:start}",
      ".al-grid.dense{grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:9px}",
      ".al-tile{border:1px solid var(--seam);background:var(--iron);border-radius:11px;overflow:hidden;cursor:pointer;transition:border-color .12s}",
      ".al-tile:hover{border-color:var(--ember)}",
      ".al-tile.sel{border-color:var(--ember);box-shadow:0 0 0 1px var(--ember)}",
      ".al-tile.unused{border-left:2px solid var(--warn)}",
      ".al-tile.used{border-left:2px solid var(--good)}",
      // The sheet, whole. Checkerboard so transparent margins read as empty
      // rather than as black art, and pixelated because these are pixel sheets.
      ".al-cover{position:relative;min-height:96px;max-height:190px;display:flex;align-items:center;justify-content:center;padding:9px;background-color:var(--bg);background-image:linear-gradient(45deg,var(--surface-1) 25%,transparent 25%,transparent 75%,var(--surface-1) 75%),linear-gradient(45deg,var(--surface-1) 25%,var(--bg) 25%,var(--bg) 75%,var(--surface-1) 75%);background-size:14px 14px;background-position:0 0,7px 7px}",
      ".al-cover img{max-width:100%;max-height:172px;image-rendering:pixelated;display:block}",
      ".al-cover .none{font-family:var(--mono);font-size:10px;color:var(--ash2)}",
      ".al-dim{position:absolute;right:6px;bottom:5px;font-family:var(--mono);font-size:8.5px;color:rgba(255,255,255,.82);background:rgba(8,9,12,.78);border-radius:4px;padding:1px 5px}",
      ".al-more{position:absolute;left:6px;bottom:5px;font-family:var(--mono);font-size:8.5px;color:#fff;background:rgba(8,9,12,.78);border-radius:4px;padding:1px 5px}",
      ".al-cap{padding:8px 10px 9px}",
      ".al-nm{font-family:var(--mono);font-size:11.5px;color:var(--bone);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".al-dir{font-family:var(--mono);font-size:8.5px;color:var(--ash2);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}",
      ".al-sub{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:5px;font-family:var(--mono);font-size:9px;color:var(--ash2)}",
      ".al-b{border:1px solid var(--seam);border-radius:999px;padding:1px 6px;white-space:nowrap}",
      ".al-b.use{border-color:var(--good-line);color:var(--good)}",
      ".al-b.no{border-color:var(--warn-line);color:var(--warn)}",
      ".al-b.rig{border-color:var(--line-strong);color:var(--text)}",
      ".al-b.rev{border-color:var(--bad-line);color:var(--bad)}",
      ".al-empty{padding:36px;text-align:center;font-family:var(--mono);font-size:11px;color:var(--ash2)}",

      // drawer
      ".al-scrim{position:fixed;inset:0;z-index:1200;background:rgba(4,5,7,.7);opacity:0;transition:opacity .16s}",
      ".al-scrim.open{opacity:1}",
      ".al-draw{position:fixed;top:0;right:0;bottom:0;width:min(720px,94vw);z-index:1201;background:var(--iron);border-left:1px solid var(--seam);display:flex;flex-direction:column;transform:translateX(100%);transition:transform .18s}",
      ".al-draw.open{transform:none}",
      ".al-dh{display:flex;align-items:flex-start;gap:12px;padding:15px 17px;border-bottom:1px solid var(--seam)}",
      ".al-dh h3{margin:0;font-size:16px;color:var(--bone);font-weight:var(--fw-semi)}",
      ".al-dh .p{font-family:var(--mono);font-size:10px;color:var(--ash2);margin-top:4px;word-break:break-all}",
      ".al-dx{margin-left:auto;background:none;border:1px solid var(--seam);border-radius:7px;color:var(--ash);width:30px;height:30px;cursor:pointer;flex:none}",
      ".al-dx:hover{border-color:var(--ember);color:var(--bone)}",
      ".al-db{flex:1;overflow-y:auto;padding:15px 17px}",
      ".al-mem{border:1px solid var(--seam);border-radius:10px;margin-bottom:12px;overflow:hidden;background:var(--void)}",
      ".al-mem>header{display:flex;align-items:center;gap:8px;padding:8px 11px;border-bottom:1px solid var(--seam);font-family:var(--mono);font-size:10.5px;color:var(--bone)}",
      ".al-mem>header .d{margin-left:auto;color:var(--ash2);font-size:9px}",
      ".al-sheet{padding:10px;background-color:var(--bg);background-image:linear-gradient(45deg,var(--surface-1) 25%,transparent 25%,transparent 75%,var(--surface-1) 75%),linear-gradient(45deg,var(--surface-1) 25%,var(--bg) 25%,var(--bg) 75%,var(--surface-1) 75%);background-size:14px 14px;background-position:0 0,7px 7px;overflow-x:auto}",
      ".al-sheet img{image-rendering:pixelated;display:block;max-width:100%}",
      ".al-acts{display:flex;gap:6px;padding:8px 11px;flex-wrap:wrap;border-top:1px solid var(--seam)}",
      ".al-btn{padding:5px 10px;background:var(--plate);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11px;cursor:pointer}",
      ".al-btn:hover{border-color:var(--ember)}",
      ".al-btn.go{background:var(--ember);color:var(--bg);border-color:var(--ember);font-weight:var(--fw-semi)}",
      ".al-note{font-size:11.5px;color:var(--ash);line-height:1.55;margin-bottom:12px}",
      ".al-note b{color:var(--bone)}",
      ".al-h{font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--ash2);margin:16px 0 8px}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── data ─────────────────────────────────────────────────────────────── */
  async function activate(force){
    injectStyle();
    const host = document.getElementById("asset-lib-root");
    if (!host) return;
    if (!data && !loading) host.innerHTML =
      `<div class="al-empty">reading the library…</div>`;
    if (!data || force){
      if (loading) return;
      loading = true;
      const d = await readJSON(
        `/api/assets/library${force ? "?force=true" : ""}`, null);
      loading = false;
      if (!d || d.__error){
        host.innerHTML = `<div class="al-empty">${
          E((d && d.__error) || "the library scan failed")}</div>`;
        return;
      }
      data = d;
    }
    render();
  }
  function refresh(){ activate(true); }

  function families(){
    let out = (data.families || []).slice();
    if (view.cat) out = out.filter(f => f.category === view.cat);
    if (view.search){
      const q = view.search;
      out = out.filter(f => (`${f.label} ${f.dir}`).toLowerCase().includes(q)
        || f.members.some(m => m.name.toLowerCase().includes(q)));
    }
    if (view.status === "used") out = out.filter(f => f.in_use);
    else if (view.status === "unused") out = out.filter(f => !f.in_use);
    else if (view.status === "review") out = out.filter(f => f.review_status === "review");
    else if (view.status === "rigged") out = out.filter(f => f.rigged > 0);
    else if (view.status === "unrigged")
      out = out.filter(f => !f.rigged && f.kinds.includes("image"));
    const cmp = {
      name:  (a, b) => a.label.localeCompare(b.label),
      new:   (a, b) => b.mtime - a.mtime,
      files: (a, b) => b.count - a.count || a.label.localeCompare(b.label),
      size:  (a, b) => b.bytes - a.bytes,
    }[view.sort] || ((a, b) => a.label.localeCompare(b.label));
    return out.sort(cmp);
  }

  /* ── render ───────────────────────────────────────────────────────────── */
  function render(){
    const host = document.getElementById("asset-lib-root");
    if (!host || !data) return;
    const st = data.stats || {};
    const shown = families();
    const catCount = {};
    (data.families || []).forEach(f => {
      catCount[f.category] = (catCount[f.category] || 0) + 1; });

    const chip = (on, cls, onclick, inner, title) =>
      `<button class="al-chip${on ? " on" : ""}${cls ? " " + cls : ""}"
        ${title ? `title="${E(title)}"` : ""} onclick="${onclick}">${inner}</button>`;

    host.innerHTML = `
      <div class="al-bar">
        <input class="al-in" placeholder="Search families and files…"
               value="${E(view.search)}" oninput="AssetLib.setSearch(this.value)">
        ${chip(view.status === "used", "good", "AssetLib.setStatus('used')",
               `in use <b>${st.in_use || 0}</b>`,
               "Reached by a scene or script — including paths the game builds at runtime.")}
        ${chip(view.status === "unused", "warn", "AssetLib.setStatus('unused')",
               `unused <b>${st.unused || 0}</b>`,
               "On disk, reached by nothing. Approved is not the same as shipping.")}
        ${chip(view.status === "review", "", "AssetLib.setStatus('review')",
               `needs review`, "Has a candidate revision waiting on a decision.")}
        ${chip(view.status === "rigged", "", "AssetLib.setStatus('rigged')",
               `rigged <b>${st.rigged || 0}</b>`,
               "Carries a rig sidecar — labelled slots the gear pipeline can read.")}
        ${chip(view.status === "unrigged", "", "AssetLib.setStatus('unrigged')",
               `no rig`, "Image families with no rig sidecar — the gear pipeline has to guess.")}
        <span class="al-sum">${shown.length} of ${st.families || 0} families ·
          ${st.files || 0} files${data.truncated ? " · scan truncated" : ""}</span>
      </div>
      <div class="al-rail">
        ${chip(!view.cat, "", "AssetLib.setCat('')", `all`)}
        ${Object.keys(catCount).sort().map(c => chip(view.cat === c, "",
          `AssetLib.setCat('${E(c)}')`,
          `${CAT_GLYPH[c] || "·"} ${E(c)} <b>${catCount[c]}</b>`)).join("")}
        <span style="flex:1"></span>
        ${SORTS.map(s => chip(view.sort === s.id, "",
          `AssetLib.setSort('${s.id}')`, E(s.label))).join("")}
        ${chip(view.dense, "", "AssetLib.toggleDense()", "compact")}
        ${chip(false, "", "AssetLib.refresh()", "rescan")}
      </div>
      ${data.map_error ? `<div class="al-note" style="color:var(--warn)">
        Usage is unknown: ${E(data.map_error)}</div>` : ""}
      <div id="al-body"></div>`;
    renderBody(shown);
  }

  function renderBody(shown){
    const body = document.getElementById("al-body");
    if (!body) return;
    if (!shown.length){
      body.innerHTML = `<div class="al-empty">nothing matches this filter</div>`;
      return;
    }
    // Grouped by category unless one is already selected — a flat wall of 160
    // families is the thing this replaced.
    const groups = {};
    shown.forEach(f => (groups[f.category] = groups[f.category] || []).push(f));
    body.innerHTML = Object.keys(groups).map(cat => `
      <div class="al-sec">
        ${view.cat ? "" : `<div class="al-sech"><span class="g">${CAT_GLYPH[cat] || "·"}</span>
          ${E(cat)}<span class="n">${groups[cat].length} famil${groups[cat].length===1?"y":"ies"}</span></div>`}
        <div class="al-grid${view.dense ? " dense" : ""}">
          ${groups[cat].map(tile).join("")}
        </div>
      </div>`).join("");
  }

  /* The same family name lives in more than one directory more often than you
   * would guess — a props/ source tree and a props/derived/ render of it hold
   * prop_copier twice, and two tiles labelled prop_copier with different usage
   * badges read as a bug. The directory tail is what tells them apart. */
  function dirTail(dir){
    const parts = String(dir || "").split("/").filter(Boolean);
    return parts.slice(-2).join("/") || ".";
  }

  function tile(f){
    const size = f.cover_size && f.cover_size[0]
      ? `${f.cover_size[0]}×${f.cover_size[1]}` : "";
    const extra = f.count > 1 ? `+${f.count - 1} more` : "";
    const rev = f.review_status === "review"
      ? `<span class="al-b rev">${f.reviewable} to review</span>` : "";
    return `<div class="al-tile ${f.in_use ? "used" : "unused"}${openKey === f.key ? " sel" : ""}"
                 onclick="AssetLib.open('${E(f.key)}')" title="${E(f.dir)}">
      <div class="al-cover">
        ${f.cover
          ? `<img loading="lazy" src="${prev(f.cover)}" alt="${E(f.label)}">`
          : `<span class="none">${(f.kinds || []).map(k => KIND_GLYPH[k] || "·").join(" ")} ${E((f.kinds||[]).join(", ") || "no preview")}</span>`}
        ${size ? `<span class="al-dim">${size}</span>` : ""}
        ${extra ? `<span class="al-more">${extra}</span>` : ""}
      </div>
      <div class="al-cap">
        <div class="al-nm">${E(f.label)}</div>
        <div class="al-dir">${E(dirTail(f.dir))}</div>
        <div class="al-sub">
          <span class="al-b">${f.count} file${f.count===1?"":"s"}</span>
          ${f.in_use
            ? `<span class="al-b use">in ${f.used_by.length} screen${f.used_by.length===1?"":"s"}</span>`
            : `<span class="al-b no">unused</span>`}
          ${f.rigged ? `<span class="al-b rig">rig ${f.rigged}</span>` : ""}
          ${f.seconds ? `<span class="al-b">${secs(f.seconds)}</span>` : ""}
          ${f.kinds.includes("audio") && !f.loops
            ? `<span class="al-b no">no loop</span>`
            : f.loops ? `<span class="al-b rig">loops ${f.loops}</span>` : ""}
          ${rev}
        </div>
      </div>
    </div>`;
  }

  /* ── drawer ───────────────────────────────────────────────────────────── */
  async function open(key){
    injectStyle();
    openKey = key;
    let fam = (data && data.families || []).find(f => f.key === key);
    if (!fam) fam = await readJSON(`/api/assets/family?key=${encodeURIComponent(key)}`, null);
    if (!fam || fam.__error){ say("that family is gone — rescan"); return; }

    let scrim = document.getElementById("al-scrim");
    if (!scrim){
      scrim = document.createElement("div");
      scrim.id = "al-scrim"; scrim.className = "al-scrim";
      scrim.onclick = close;
      document.body.appendChild(scrim);
      const d = document.createElement("aside");
      d.id = "al-draw"; d.className = "al-draw";
      document.body.appendChild(d);
    }
    document.getElementById("al-draw").innerHTML = drawer(fam);
    requestAnimationFrame(() => {
      document.getElementById("al-scrim").classList.add("open");
      document.getElementById("al-draw").classList.add("open");
    });
    render();
  }

  function close(){
    const s = document.getElementById("al-scrim"), d = document.getElementById("al-draw");
    if (s) s.classList.remove("open");
    if (d) d.classList.remove("open");
    setTimeout(() => { if (s) s.remove(); if (d) d.remove(); }, 200);
    openKey = null;
    render();
  }

  function drawer(f){
    const unused = f.members.filter(m => !m.in_use);
    return `
      <div class="al-dh">
        <div style="min-width:0">
          <h3>${E(f.label)}</h3>
          <div class="p">${E(f.dir)}</div>
        </div>
        <button class="al-dx" onclick="AssetLib.close()" aria-label="Close">✕</button>
      </div>
      <div class="al-db">
        <div class="al-note">
          <b>${f.count}</b> file${f.count===1?"":"s"} ·
          ${f.in_use
            ? `reached by <b>${E(f.used_by.join(", "))}</b>`
            : `<span style="color:var(--warn)">no scene or script reaches any of these</span>`}
          ${f.rigged ? ` · <b>${f.rigged}</b> rigged` : ""}
        </div>
        ${unused.length && f.in_use ? `<div class="al-note" style="color:var(--warn)">
          ${unused.length} of ${f.count} file${unused.length===1?"":"s"} in this family
          reach nothing: ${unused.slice(0,6).map(m => E(m.variant || m.name)).join(", ")}${
            unused.length > 6 ? "…" : ""}</div>` : ""}
        <div class="al-h">files</div>
        ${f.members.map(member).join("")}
      </div>`;
  }

  function secs(s){
    if (s == null) return "";
    const m = Math.floor(s / 60), r = s - m * 60;
    return m ? `${m}:${r.toFixed(1).padStart(4, "0")}` : `${r.toFixed(2)}s`;
  }

  function member(m){
    const snd = m.sound;
    const dim = m.width ? `${m.width}×${m.height}`
      : snd && snd.seconds != null
        ? `${secs(snd.seconds)} · ${snd.sample_rate || "?"}Hz ${snd.channels === 2 ? "stereo" : "mono"}`
        : `${Math.round(m.bytes/1024)} KB`;
    const rig = m.rig
      ? `<span class="al-b rig">${(m.rig.slots || []).join(", ") || "grid only"}${
          m.rig.frames ? ` · ${m.rig.frames}f` : ""}</span>` : "";
    // Looping is invisible everywhere else — it lives in a .import sidecar, and
    // a music track that plays once and stops sounds perfect until it doesn't.
    const loop = snd && snd.loop_supported
      ? (snd.loops ? `<span class="al-b rig">loops</span>`
                   : `<span class="al-b no">no loop</span>`) : "";
    const rev = m.review
      ? `<span class="al-b${m.review.status === "review" ? " rev" : ""}">${E(m.review.status)}
         · ${m.review.revisions} rev</span>` : "";
    return `<div class="al-mem">
      <header>${KIND_GLYPH[m.kind] || "·"} ${E(m.variant || m.name)}
        ${m.in_use ? `<span class="al-b use">in ${m.used_by.length} screen${m.used_by.length===1?"":"s"}</span>`
                   : `<span class="al-b no">unused</span>`}
        ${rig}${loop}${rev}
        <span class="d">${dim}</span></header>
      ${m.kind === "image"
        ? `<div class="al-sheet"><img loading="lazy" src="${prev(m.rel)}" alt="${E(m.name)}"></div>`
        : m.kind === "audio"
        ? `<div class="al-sheet"><audio controls preload="none" style="width:100%"
             src="/api/audio/file?rel=${encodeURIComponent(m.rel)}"></audio></div>`
        : `<div class="al-sheet"><span style="font-family:var(--mono);font-size:10px;color:var(--ash2)">${E(m.name)}</span></div>`}
      <div class="al-acts">
        ${m.editable ? `<button class="al-btn go" onclick="AssetLib.edit('${E(m.rel)}')">edit pixels &amp; rig</button>` : ""}
        ${m.audio_editable ? `<button class="al-btn go" onclick="AssetLib.editAudio('${E(m.rel)}')">open in audio lab</button>` : ""}
        ${m.res_path ? `<button class="al-btn" onclick="AssetLib.wire('${E(m.res_path)}')">wire into a scene…</button>` : ""}
        ${m.res_path ? `<button class="al-btn" onclick="AssetLib.task('${E(m.res_path)}')">deploy a task</button>` : ""}
        ${m.review ? `<button class="al-btn" onclick="AssetLib.review('${E(m.review.logical_name)}')">revisions</button>` : ""}
        <span style="flex:1"></span>
        <span style="font-family:var(--mono);font-size:9px;color:var(--ash2);align-self:center">${E(m.rel)}</span>
      </div>
    </div>`;
  }

  /* ── actions ──────────────────────────────────────────────────────────── */
  function edit(rel){
    if (!window.SpriteEdit) return say("the sprite editor did not load");
    close();
    SpriteEdit.open(rel);
  }
  function editAudio(rel){
    if (!window.AudioLab) return say("the audio lab did not load");
    close();
    AudioLab.open(rel);
  }
  function wire(resPath){
    if (!window.AtlasGraph) return say("the atlas graph did not load");
    close();
    // The wiring menu lives on the graph, and the graph needs its scan — flip
    // to it rather than duplicating the flow in two places.
    if (typeof setWorkspace === "function") setWorkspace("atlas");
    if (typeof setAtlasMode === "function") setAtlasMode("graph");
    setTimeout(() => AtlasGraph.wireMenu(resPath), 700);
  }
  /* Atlas owns the composer prefill, and it needs its scan loaded to describe
     the node. Ensure it, then hand over — duplicating the prefill here would
     be a second copy of the "what is this asset" story to keep in sync. */
  async function task(resPath){
    close();
    if (!window.Atlas) return say("the atlas did not load");
    try { await Atlas.ensure(); } catch (e) {}
    Atlas.deploy(resPath);
  }
  function review(logicalName){
    close();
    if (typeof openAssetDrawer === "function") openAssetDrawer(logicalName);
    else say("no revision history for that file");
  }

  function setSearch(v){
    view.search = String(v || "").toLowerCase().trim();
    renderBody(families());
  }
  function setCat(c){ view.cat = c; render(); }
  function setStatus(s){ view.status = view.status === s ? "" : s; render(); }
  function setSort(s){ view.sort = s; render(); }
  function toggleDense(){ view.dense = !view.dense; render(); }

  return { activate, refresh, render, open, close, edit, editAudio, wire, task, review,
           setSearch, setCat, setStatus, setSort, toggleDense,
           get data(){ return data; } };
})();
