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
  /* The category rail still wears the glyphs — they are inline in a chip, next
   * to a count, and a stroked 15px icon there would be a second row of
   * furniture. A SECTION header is the one place the real icon earns its size,
   * so this map is deliberately separate from CAT_GLYPH rather than replacing
   * it. Names come from icons.js; anything unlisted falls back to `assets`. */
  const CAT_ICON = { characters:"rig", enemies:"collision", items:"props",
                     props:"stage", tiles:"tileset", audio:"audio",
                     ui:"overview", shaders:"fill", vfx:"concept",
                     lights:"lighting", pickups:"select", portraits:"art",
                     generated_static:"background", assets:"assets" };
  const SORTS = [
    { id:"name",  label:"name" },
    { id:"new",   label:"newest" },
    { id:"files", label:"most files" },
    { id:"size",  label:"largest" },
  ];

  let data = null, loading = false;
  let view = { cat:"", search:"", status:"", sort:"name", dense:false,
               working:false };
  let openKey = null;

  /* ── styles ───────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("assetlib-style")) return;
    const s = document.createElement("style");
    s.id = "assetlib-style";
    s.textContent = [
      /* ── the control header ────────────────────────────────────────────────
       * WHAT THIS REPLACED was one run of 24 identical pills: a search box, five
       * status filters, fifteen family filters, four sort modes and three
       * actions, all the same shape, the same size and the same colour. `rescan`
       * — which walks the whole project off disk — looked exactly like `tiles`.
       * Nothing on the row told you which pills narrowed the grid, which one
       * reordered it, and which one would go and DO something.
       *
       * So the three kinds are now three different species, and each one wears
       * the label of what it is:
       *   filters  stay pills, because a pill that lights up is a toggle;
       *   sort and family are SELECTS - one-of-many, and the app's combobox
       *            makes them look nothing like a toggle;
       *   view     is a pressed-state toggle, squared off so it is not a pill;
       *   actions  left the header entirely - `rescan` now sits with sprite
       *            editor / audio lab / 3D viewer in the view heading, which is
       *            where every other action on this page already lives.
       * Family went from fifteen pills to one select for the same reason:
       * fifteen mutually-exclusive options is a list, not a row of switches. */
      ".al-head{margin-bottom:var(--s-6);padding-bottom:var(--s-5);border-bottom:1px solid var(--line)}",
      ".al-row{display:flex;gap:var(--s-5);align-items:center;flex-wrap:wrap}",
      ".al-row+.al-row{margin-top:var(--s-4)}",
      /* A POD, NOT A RULE BETWEEN NEIGHBOURS. A separator drawn as the group's
       * own left border is correct only while the row fits on one line - the
       * moment it wraps, the first group on the new line wears a hairline
       * against the page margin, which reads as a rendering artefact. A tinted
       * pod groups the same controls and survives the wrap, which this header
       * does at any window narrower than about 1100px. */
      ".al-grp{display:flex;gap:var(--s-3);align-items:center;flex-wrap:wrap;"
        + "background:var(--surface-1);border-radius:var(--r-lg);padding:var(--s-3) var(--s-4)}",
      ".al-lab{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text-3);white-space:nowrap}",
      ".al-in{background:var(--bg);border:1px solid var(--line);border-radius:var(--r-lg);color:var(--text);font:inherit;font-size:var(--fs-sm);padding:7px 11px;min-width:200px;flex:1 1 200px;max-width:340px}",
      ".al-in:focus{outline:none;border-color:var(--accent)}",
      /* A FILTER IS A PILL. Round, toggles, lights up in the colour of what it
       * is asserting - and nothing else on this header is round. */
      ".al-chip{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;padding:5px 10px;border:1px solid var(--line);border-radius:var(--r-full);color:var(--text-2);cursor:pointer;background:none;display:inline-flex;gap:6px;align-items:center}",
      ".al-chip:hover{border-color:var(--accent);color:var(--text)}",
      ".al-chip.on{border-color:var(--accent);color:var(--text);background:var(--accent-soft)}",
      ".al-chip b{color:var(--text);font-weight:var(--fw-semi)}",
      ".al-chip.good.on{border-color:var(--good);color:var(--good);background:var(--good-soft)}",
      ".al-chip.warn.on{border-color:var(--warn);color:var(--warn);background:var(--warn-soft)}",
      /* ONE OF MANY IS A SELECT. bgselect.js swaps the native element for the
       * app's combobox and copies these classes onto it, so the caret and the
       * squared corners are what the user actually sees - deliberately the
       * opposite shape to a filter pill. */
      ".al-sel,.al-sel.bgs-btn{font-family:var(--mono);font-size:var(--fs-2xs);letter-spacing:.06em;padding:5px 9px;border-radius:var(--r-lg);background:var(--surface-2);border:1px solid var(--line);color:var(--text)}",
      ".al-sel.bgs-btn:hover{border-color:var(--accent)}",
      /* A VIEW TOGGLE IS NEITHER. Squared like the selects because it is about
       * presentation, pressed like a switch because it has two states, and never
       * round - `compact` changes the layout, it does not narrow the set. */
      ".al-tgl{font-family:var(--mono);font-size:var(--fs-2xs);letter-spacing:.06em;padding:5px 10px;border-radius:var(--r-lg);background:var(--surface-2);border:1px solid var(--line);color:var(--text-2);cursor:pointer}",
      ".al-tgl:hover{border-color:var(--accent);color:var(--text)}",
      ".al-tgl[aria-pressed=true]{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}",
      /* WHICH LIBRARY, not which filter. Working files are a different set of
       * files entirely (see families()), so this is a segmented switch between
       * two drawers rather than another pill in the filter run. */
      ".al-seg{display:inline-flex;border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden}",
      ".al-segb{font-family:var(--mono);font-size:var(--fs-2xs);letter-spacing:.06em;padding:5px 10px;background:none;border:0;color:var(--text-3);cursor:pointer;display:inline-flex;gap:6px;align-items:center}",
      ".al-segb+.al-segb{border-left:1px solid var(--line)}",
      ".al-segb:hover{color:var(--text)}",
      ".al-segb[aria-pressed=true]{background:var(--accent-soft);color:var(--text)}",
      ".al-segb b{color:var(--text);font-weight:var(--fw-semi)}",
      ".al-sum{font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-3);margin-left:auto;text-align:right}",
      ".al-sec{margin-bottom:22px}",
      /* .sec-h (app.css) carries the icon, the label, the count pill and the
       * rule. What is left here is the one thing this instance needs and the
       * shared class must not assume: it rides the scroll, so it needs the
       * canvas painted behind it or the grid shows through the label. */
      ".al-sech{position:sticky;top:0;background:var(--bg);z-index:2;padding-top:var(--s-4)}",
      ".al-sech .n{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text-3)}",
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
      ".al-draw{position:fixed;top:0;right:0;bottom:0;width:min(720px,94vw);z-index:1201;background:var(--solid-1);border-left:1px solid var(--seam);display:flex;flex-direction:column;transform:translateX(100%);transition:transform .18s}",
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
    // WORKING FILES ARE A DIFFERENT DRAWER, not a filter of this one. The art
    // seat's scratch renders, tmp/ and test fixtures live outside
    // res://assets/**, so the engine cannot load any of them — they are all,
    // correctly, "unused", and on a real project they were 508 of 775 families.
    // Two thirds of the page was grey rows that could never turn green and that
    // nobody could act on, which is what buried the assets that matter.
    // A DRAWER IS EXCLUSIVE IN BOTH DIRECTIONS, and only one of these lines was
    // here. Turning working files ON stopped filtering rather than switching, so
    // the drawer showed both sets: the count rail and the summary line (which
    // both already scoped themselves to one drawer) said "730 families" over a
    // grid of 1012. The header's library switch is now a two-option segmented
    // control, which asserts exclusivity outright and cannot be the one thing on
    // the page telling the truth.
    out = out.filter(f => (view.working ? !!f.working : !f.working));
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
    // Counted over the drawer you are IN. A rail that promises 289 under "art"
    // and then shows nothing, because art is all working files, is worse than
    // no count at all.
    const catCount = {};
    (data.families || []).forEach(f => {
      if (!view.working && f.working) return;
      if (view.working && !f.working) return;
      catCount[f.category] = (catCount[f.category] || 0) + 1; });

    const chip = (on, cls, onclick, inner, title) =>
      `<button class="al-chip${on ? " on" : ""}${cls ? " " + cls : ""}"
        ${title ? `title="${E(title)}"` : ""} onclick="${onclick}">${inner}</button>`;

    const cats = Object.keys(catCount).sort();
    const shipping = (st.in_use || 0) + (st.unused || 0);

    host.innerHTML = `
      <div class="al-head">
        <div class="al-row">
          <input class="al-in" placeholder="Search families and files…"
                 value="${E(view.search)}" oninput="AssetLib.setSearch(this.value)">
          <div class="al-grp">
            <span class="al-lab">family</span>
            <select class="al-sel" aria-label="Family"
                    onchange="AssetLib.pickCat(this.value)">
              <option value=""${view.cat ? "" : " selected"}>all families${
                cats.length ? ` · ${cats.length}` : ""}</option>
              ${cats.map(c => `<option value="${E(c)}"${
                view.cat === c ? " selected" : ""}>${CAT_GLYPH[c] || "·"} ${
                E(c)} · ${catCount[c]}</option>`).join("")}
            </select>
          </div>
          <div class="al-grp">
            <span class="al-lab">filter</span>
            ${chip(view.status === "used", "good", "AssetLib.setStatus('used')",
                   `in use <b>${st.in_use || 0}</b>`,
                   "Reached by a scene or script - including paths the game builds at runtime.")}
            ${chip(view.status === "unused", "warn", "AssetLib.setStatus('unused')",
                   `unused <b>${st.unused || 0}</b>`,
                   "On disk, reached by nothing. Approved is not the same as shipping.")}
            ${chip(view.status === "review", "", "AssetLib.setStatus('review')",
                   `needs review`, "Has a candidate revision waiting on a decision.")}
            ${chip(view.status === "rigged", "", "AssetLib.setStatus('rigged')",
                   `rigged <b>${st.rigged || 0}</b>`,
                   "Carries a rig sidecar - labelled slots the gear pipeline can read.")}
            ${chip(view.status === "unrigged", "", "AssetLib.setStatus('unrigged')",
                   `no rig`, "Image families with no rig sidecar - the gear pipeline has to guess.")}
          </div>
          <span class="al-sum">${shown.length} of ${
            view.working ? (st.working || 0) : shipping} families ·
            ${st.files || 0} files${data.truncated ? " · scan truncated" : ""}</span>
        </div>
        <div class="al-row">
          <div class="al-grp">
            <span class="al-lab">sort</span>
            <select class="al-sel" aria-label="Sort"
                    onchange="AssetLib.pickSort(this.value)">
              ${SORTS.map(s => `<option value="${s.id}"${
                view.sort === s.id ? " selected" : ""}>${E(s.label)}</option>`).join("")}
            </select>
          </div>
          <div class="al-grp">
            <span class="al-lab">view</span>
            <button class="al-tgl" type="button" aria-pressed="${view.dense}"
                    title="Smaller tiles, more of them on screen. A layout change - it hides nothing."
                    onclick="AssetLib.toggleDense()">compact</button>
          </div>
          ${st.working ? `<div class="al-grp">
            <span class="al-lab">library</span>
            <div class="al-seg" role="group" aria-label="Which library">
              <button class="al-segb" type="button" aria-pressed="${!view.working}"
                      title="Files under res://assets/** - the ones the game can actually load."
                      onclick="${view.working ? "AssetLib.toggleWorking()" : ""}">in the game <b>${shipping}</b></button>
              <button class="al-segb" type="button" aria-pressed="${view.working}"
                      title="Files outside res://assets/** - the art seat's scratch renders, tmp/, test fixtures. The engine cannot load any of them, so they are all 'unused' and none of that is a defect."
                      onclick="${view.working ? "" : "AssetLib.toggleWorking()"}">working files <b>${st.working}</b></button>
            </div>
          </div>` : ""}
        </div>
      </div>
      ${view.working ? `<div class="al-note">Working files — outside
        <code>res://assets/</code>, so the game cannot load them. Nothing here
        being "unused" is a problem.</div>` : ""}
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
        ${view.cat ? "" : `<div class="sec-h al-sech">
          <span data-icon="${CAT_ICON[cat] || "assets"}" data-icon-size="15"></span>
          <h3 class="sec-t">${E(cat)}</h3>
          <span class="sec-n">${groups[cat].length}</span>
          <span class="n">famil${groups[cat].length===1?"y":"ies"}</span></div>`}
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
    // The server's own sentence, not a guess about it. readJSON tags the reason
    // onto __error and this discarded it, so a refused path, a 500 and an
    // unreachable backend all read as "that family is gone" — the one diagnosis
    // that tells you to do the one thing (rescan) that cannot help.
    if (!fam || fam.__error){
      say((fam && fam.__error) || "that family is gone - rescan"); return;
    }

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
    if (!window.SceneBuild) return say("the scene builder did not load");
    close();
    // The wiring menu used to live on the Atlas graph. That mode is gone and
    // the flow moved to the scene builder, which already owns every other
    // write to a .tscn — flip to it rather than duplicating the flow here.
    if (typeof setWorkspace === "function") setWorkspace("atlas");
    if (typeof setAtlasMode === "function") setAtlasMode("scene");
    setTimeout(() => SceneBuild.wireMenu(resPath), 700);
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

  /* THE SELECT'S OWN TEARDOWN HAS TO FINISH FIRST. bgselect.js fires `change`
     and only THEN closes its popup and returns focus to the button - and this
     header rebuilds its innerHTML on every change, so a synchronous re-render
     rips that button out from under the code about to focus it. One tick is
     invisible and leaves the combobox to close against a DOM that still has it. */
  const defer = fn => setTimeout(fn, 0);
  function pickCat(c){ defer(() => setCat(c)); }
  function pickSort(s){ defer(() => setSort(s)); }
  /* Category comes back with it: the two drawers have almost no categories in
     common, so keeping `art` selected on the way back to shipping assets lands
     on an empty grid that reads as a broken page. */
  function toggleWorking(){ view.working = !view.working; view.cat = ""; render(); }

  return { activate, refresh, open, close, edit, editAudio, wire, task, review,
           setSearch, setCat, setStatus, setSort, pickCat, pickSort,
           toggleDense, toggleWorking };
})();
