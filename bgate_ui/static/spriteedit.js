/* spriteedit.js — the pixel editor, and the place where a sheet gets LABELLED.
 *
 * Two jobs in one surface, because they are the same job:
 *
 *   1. THE HUMAN TOUCH. Generated sprite art lands 90% right, and the 10% that
 *      is wrong is four pixels — a halo the matte missed, a stray dot in the
 *      walk cycle, a hand that reads as a mitten. Re-rolling a generator to fix
 *      four pixels is the most expensive possible way to fix four pixels. So:
 *      open the actual file, paint on it, save it back.
 *
 *   2. RIGGING LABELS. gear.py can measure a grip anchor only when several
 *      weapon sheets happen to agree, and guesses otherwise. A guess is what
 *      makes a sword float six pixels off the hand on exactly one attack. The
 *      fix was never a better algorithm — it was somewhere for a person to
 *      point at the pixel and have it stick. That is the rig sidecar, and this
 *      is the surface that writes it.
 *
 * Everything is per-FRAME, because a sprite sheet is not an image, it is a
 * lattice of images. The grid comes from the same detector the gear pipeline
 * uses, so the cells shown here are the cells the engine will cut.
 *
 * Vanilla, dependency-free, guarded end to end — this module must never throw
 * uncaught into the dashboard.
 */
window.SpriteEdit = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const clamp = (v, lo, hi) => v < lo ? lo : v > hi ? hi : v;

  // Undo is ImageData snapshots. Cheap to implement, correct for every tool
  // including flood fill, and bounded by BYTES rather than by count — a 1024²
  // sheet and a 32² sheet must not get the same 40-deep history.
  const UNDO_BYTES = 96 * 1024 * 1024;

  const TOOLS = [
    {id:"pencil", g:"✎", k:"b", t:"Pencil — paint pixels (B)"},
    {id:"eraser", g:"⌫", k:"e", t:"Eraser — clear to transparent (E)"},
    {id:"bucket", g:"◍", k:"g", t:"Fill — flood the contiguous colour (G)"},
    {id:"picker", g:"⊙", k:"i", t:"Eyedropper — take a colour off the sheet (I)"},
    {id:"line",   g:"╱", k:"l", t:"Line (L)"},
    {id:"rect",   g:"▭", k:"r", t:"Rectangle — hold Shift to fill (R)"},
    {id:"anchor", g:"✜", k:"a", t:"Rig anchor — mark where a slot attaches (A)"},
  ];

  const SLOT_COLOR = {
    main_hand:"#ff6a3d", off_hand:"#4aa3ff",
    left_hand:"#ffd166", right_hand:"#2ec4b6",
    head:"#e8e2d8", body:"#9a7bff",
    feet:"#8bd450", throwable:"#ff6ec7", pivot:"#7c8695", muzzle:"#ff9f43",
    fx:"#57c7ff",
  };
  const slotColor = s => SLOT_COLOR[s] || "#e8e2d8";

  /* LOGICAL vs ANATOMICAL hands. main_hand/off_hand say which hand holds the
   * weapon — that is what the gear layer equips against. left_hand/right_hand
   * say where the character's hands actually ARE in this frame. A character
   * that turns around keeps its main hand while its left and right swap sides,
   * so the two pairs are not interchangeable and both get first-class buttons. */
  const HANDS = [
    { slot:"left_hand",  short:"L", label:"left hand" },
    { slot:"right_hand", short:"R", label:"right hand" },
  ];
  const GRIPS = [
    { slot:"main_hand", short:"main", label:"main hand (holds the weapon)" },
    { slot:"off_hand",  short:"off",  label:"off hand" },
  ];

  let S = null;          // the whole editor state, or null when closed
  let $ = {};            // cached elements

  /* ── styling ──────────────────────────────────────────────────────────── */
  function injectStyle(){
    if (document.getElementById("spriteedit-style")) return;
    const s = document.createElement("style");
    s.id = "spriteedit-style";
    s.textContent = [
      ".se-back{position:fixed;inset:0;z-index:1400;background:rgba(4,5,7,.86);backdrop-filter:blur(3px);display:flex;flex-direction:column}",
      ".se-bar{display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--seam);background:var(--iron);flex:none}",
      ".se-title{font-family:var(--mono);font-size:11.5px;letter-spacing:.1em;color:var(--bone);text-transform:uppercase}",
      ".se-sub{font-family:var(--mono);font-size:10px;color:var(--ash2)}",
      ".se-dirty{color:var(--warn)}",
      ".se-spacer{flex:1}",
      ".se-btn{padding:6px 11px;background:var(--plate);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;cursor:pointer}",
      ".se-btn:hover:not(:disabled){border-color:var(--ember)}",
      ".se-btn:disabled{opacity:.4;cursor:default}",
      ".se-btn.go{background:var(--ember);color:#111;border-color:var(--ember);font-weight:600}",
      ".se-body{flex:1;display:flex;min-height:0}",
      ".se-tools{width:52px;flex:none;background:var(--iron);border-right:1px solid var(--seam);padding:8px 0;display:flex;flex-direction:column;align-items:center;gap:5px;overflow-y:auto}",
      ".se-tool{width:36px;height:36px;display:grid;place-items:center;background:var(--plate);border:1px solid var(--seam);border-radius:8px;color:var(--ash);font-size:16px;cursor:pointer;flex:none}",
      ".se-tool:hover{border-color:var(--ember);color:var(--bone)}",
      ".se-tool.on{background:var(--ember);border-color:var(--ember);color:#111}",
      ".se-stage{flex:1;position:relative;min-width:0;overflow:hidden;background:#0b0c0f}",
      ".se-stage canvas{position:absolute;inset:0;width:100%;height:100%;touch-action:none;cursor:crosshair}",
      ".se-hud{position:absolute;left:10px;bottom:10px;font-family:var(--mono);font-size:10px;color:var(--ash2);background:rgba(10,11,14,.8);border:1px solid var(--seam);border-radius:6px;padding:4px 8px;pointer-events:none;white-space:pre}",
      ".se-side{width:284px;flex:none;background:var(--iron);border-left:1px solid var(--seam);overflow-y:auto;padding:12px}",
      ".se-h{font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--ash2);margin:16px 0 7px}",
      ".se-h:first-child{margin-top:0}",
      ".se-row{display:flex;align-items:center;gap:7px;margin-bottom:7px}",
      ".se-row label{font-family:var(--mono);font-size:10px;color:var(--ash2);flex:none}",
      ".se-in{flex:1;min-width:0;background:var(--void);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font:inherit;font-size:11.5px;padding:5px 7px}",
      ".se-in:focus{outline:none;border-color:var(--ember)}",
      ".se-in.num{flex:none;width:62px}",
      ".se-sw{width:34px;height:30px;padding:2px;border:1px solid var(--seam);border-radius:6px;background:var(--void);cursor:pointer;flex:none}",
      ".se-pal{display:flex;flex-wrap:wrap;gap:4px}",
      ".se-pc{width:20px;height:20px;border-radius:4px;border:1px solid var(--seam);cursor:pointer;position:relative;background-image:linear-gradient(45deg,#222 25%,transparent 25%,transparent 75%,#222 75%),linear-gradient(45deg,#222 25%,#111 25%,#111 75%,#222 75%);background-size:8px 8px;background-position:0 0,4px 4px}",
      ".se-pc span{position:absolute;inset:0;border-radius:3px}",
      ".se-pc.on{border-color:var(--ember);box-shadow:0 0 0 1px var(--ember)}",
      ".se-strip{display:flex;flex-wrap:wrap;gap:4px}",
      ".se-fr{position:relative;width:46px;height:46px;border:1px solid var(--seam);border-radius:6px;background:#000;cursor:pointer;overflow:hidden;flex:none}",
      ".se-fr canvas{width:100%;height:100%;image-rendering:pixelated;display:block}",
      ".se-fr.on{border-color:var(--ember);box-shadow:0 0 0 1px var(--ember)}",
      ".se-fr .n{position:absolute;left:2px;top:1px;font-family:var(--mono);font-size:8px;color:var(--ash2);text-shadow:0 0 3px #000}",
      ".se-fr .dots{position:absolute;right:2px;bottom:2px;display:flex;gap:2px}",
      ".se-fr .dots i{width:5px;height:5px;border-radius:50%;display:block}",
      ".se-fr.picked{border-color:#57c7ff;box-shadow:0 0 0 1px #57c7ff}",
      ".se-fr.picked::after{content:'●';position:absolute;right:3px;top:1px;font-size:8px;color:#57c7ff}",
      // hand buttons
      ".se-hands{display:flex;gap:6px}",
      ".se-hand{flex:1;display:flex;align-items:center;gap:6px;padding:7px 9px;background:var(--plate);border:1px solid var(--seam);border-radius:8px;color:var(--ash);font:inherit;font-size:11.5px;cursor:pointer;position:relative}",
      ".se-hand:hover{border-color:var(--hc);color:var(--bone)}",
      ".se-hand.on{border-color:var(--hc);color:var(--bone);background:var(--plate2)}",
      ".se-hand i{width:8px;height:8px;border-radius:50%;background:var(--hc);flex:none}",
      ".se-hand .tick{margin-left:auto;color:var(--good);font-size:11px}",
      ".se-hand.has{color:var(--bone)}",
      // regen results
      ".se-res{border:1px solid var(--seam);border-radius:9px;padding:8px;margin-bottom:8px}",
      ".se-res.bad{border-color:#7d4338}",
      ".se-res .hd{display:flex;font-family:var(--mono);font-size:10px;color:var(--bone);margin-bottom:6px}",
      ".se-res .hd .m{margin-left:auto;color:var(--ash2)}",
      ".se-res .pair{display:grid;grid-template-columns:1fr 1fr;gap:6px}",
      ".se-res figure{margin:0}",
      ".se-res img{width:100%;background:#000;border-radius:5px;image-rendering:pixelated;display:block}",
      ".se-res figcaption{font-family:var(--mono);font-size:8.5px;color:var(--ash2);text-align:center;margin-top:2px}",
      ".se-res .acts{display:flex;gap:6px;margin-top:7px}",
      ".se-res .acts .se-btn{flex:1}",
      ".se-lab{display:flex;align-items:center;gap:6px;padding:4px 6px;border:1px solid var(--seam);border-radius:6px;margin-bottom:4px;font-family:var(--mono);font-size:10px;color:var(--bone)}",
      ".se-lab i{width:8px;height:8px;border-radius:50%;flex:none}",
      ".se-lab .x{margin-left:auto;cursor:pointer;color:var(--ash2);padding:0 3px}",
      ".se-lab .x:hover{color:var(--bad)}",
      ".se-anim{border:1px solid var(--seam);border-radius:7px;padding:7px;margin-bottom:6px}",
      ".se-anim .hd{display:flex;gap:6px;align-items:center;margin-bottom:5px}",
      ".se-anim .fr{font-family:var(--mono);font-size:9.5px;color:var(--ash2);word-break:break-all}",
      ".se-note{font-size:11px;color:var(--ash);line-height:1.5}",
      ".se-note b{color:var(--bone)}",
      ".se-warn{color:var(--warn)}",
      ".se-pick{position:fixed;inset:0;z-index:1401;background:rgba(4,5,7,.9);display:flex;align-items:center;justify-content:center;padding:40px}",
      ".se-pick-box{background:var(--iron);border:1px solid var(--seam);border-radius:12px;width:min(760px,100%);max-height:100%;display:flex;flex-direction:column;overflow:hidden}",
      ".se-pick-list{overflow-y:auto;padding:8px}",
      ".se-pick-i{display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:7px;cursor:pointer;font-family:var(--mono);font-size:11px;color:var(--bone)}",
      ".se-pick-i:hover{background:var(--plate)}",
      ".se-pick-i img{width:34px;height:34px;object-fit:contain;image-rendering:pixelated;background:#000;border-radius:5px;border:1px solid var(--seam);flex:none}",
      ".se-pick-i .m{margin-left:auto;color:var(--ash2);font-size:10px}",
      ".se-tag{font-size:9px;padding:1px 5px;border-radius:999px;border:1px solid var(--seam);color:var(--ash2)}",
      ".se-tag.on{border-color:var(--good);color:var(--good)}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── open / close ─────────────────────────────────────────────────────── */
  async function open(rel){
    injectStyle();
    if (S && S.dirty && !confirm("Discard unsaved pixel edits?")) return;
    if (S) close(true);
    if (!rel) return pick();

    let info;
    try {
      info = await readJSON(`/api/sprite/open?rel=${encodeURIComponent(rel)}`, null);
    } catch (e) { info = null; }
    if (!info || info.__error){
      say((info && info.__error) || "could not open that sheet");
      return;
    }

    const img = await loadImage(`/api/preview?rel=${encodeURIComponent(rel)}&t=${Date.now()}`);
    if (!img){ say("the sheet's pixels would not load"); return; }

    const work = document.createElement("canvas");
    work.width = info.width; work.height = info.height;
    const wctx = work.getContext("2d", {willReadFrequently:true});
    wctx.imageSmoothingEnabled = false;
    wctx.drawImage(img, 0, 0);

    S = {
      rel, info, work, wctx,
      w: info.width, h: info.height,
      mtime: info.mtime,
      rig: info.rig && info.rig.grid ? info.rig : Object.assign({}, info.rig, {
        grid: info.suggested_grid || null,
      }),
      tool: "pencil", color: "#ffffff", brush: 1, zoom: 1, pan: {x:0, y:0},
      frame: 0, slot: "left_hand",
      onion: false, showGrid: true, focusFrame: true,
      undo: [], redo: [], undoBytes: 0,
      dirty: false, rigDirty: false,
      drag: null, hover: null, clip: null,
      picked: new Set(),        // frames selected for a batch regeneration
      regen: { prompt: "", quality: "medium", busy: false, results: [],
               status: null },
    };
    mount();
    fit();
    paint();
    regenStatus();      // async: fills in the price table and the off-switch
  }

  function close(silent){
    if (S && S.dirty && !silent &&
        !confirm("You have unsaved pixel edits. Close anyway?")) return;
    if (S){
      if (S.ro) try { S.ro.disconnect(); } catch (e) {}
      if (S.onResize) window.removeEventListener("resize", S.onResize);
    }
    const back = document.getElementById("se-back");
    if (back) back.remove();
    document.removeEventListener("keydown", onKey, true);
    S = null; $ = {};
  }

  function loadImage(src){
    return new Promise(res => {
      const im = new Image();
      im.onload = () => res(im);
      im.onerror = () => res(null);
      im.src = src;
    });
  }

  /* ── the file picker ──────────────────────────────────────────────────── */
  /* A real project has hundreds of editable PNGs — scratch renders, proof
   * sheets, exports. The picker is therefore search-first: the query goes to
   * the server so it filters the whole set, not just the page it sent back. */
  async function pick(q){
    injectStyle();
    let host = document.getElementById("se-pick");
    if (!host){
      host = document.createElement("div");
      host.className = "se-pick";
      host.id = "se-pick";
      host.innerHTML = `<div class="se-pick-box">
        <div class="se-bar"><span class="se-title">open a sheet</span>
          <input class="se-in" id="se-pick-q" placeholder="filter by path…"
                 style="flex:1;max-width:280px" oninput="SpriteEdit.pickSearch(this.value)">
          <span class="se-sub" id="se-pick-n"></span>
          <span class="se-spacer"></span>
          <button class="se-btn" onclick="SpriteEdit.closePick()">close</button></div>
        <div class="se-pick-list" id="se-pick-list">
          <div class="se-note" style="padding:24px;text-align:center">scanning…</div></div>
      </div>`;
      document.body.appendChild(host);
      host.addEventListener("click", ev => { if (ev.target === host) closePick(); });
      const input = host.querySelector("#se-pick-q");
      if (input) input.focus();
    }
    const d = await readJSON(
      `/api/sprite/list${q ? `?q=${encodeURIComponent(q)}` : ""}`, {sheets:[]});
    const sheets = d.sheets || [];
    const n = document.getElementById("se-pick-n");
    if (n) n.textContent = d.truncated
      ? `${sheets.length} of ${d.total} — narrow the filter`
      : `${sheets.length} editable image${sheets.length===1?"":"s"}`;
    const list = document.getElementById("se-pick-list");
    if (list) list.innerHTML = sheets.length ? sheets.map(s => `
      <div class="se-pick-i" onclick="SpriteEdit.closePick();SpriteEdit.open('${E(s.rel)}')">
        <img loading="lazy" src="/api/preview?rel=${encodeURIComponent(s.rel)}" alt="">
        <span>${E(s.name)}</span>
        <span class="se-tag${s.rigged ? " on" : ""}">${s.rigged ? "rigged" : "no rig"}</span>
        <span class="m">${s.width}×${s.height} · ${E(s.rel)}</span>
      </div>`).join("")
      : `<div class="se-note" style="padding:24px;text-align:center">no .png or .webp sheet matches</div>`;
  }
  let pickTimer = null;
  function pickSearch(v){
    clearTimeout(pickTimer);
    pickTimer = setTimeout(() => pick(String(v || "").trim()), 200);
  }
  function closePick(){ const p = document.getElementById("se-pick"); if (p) p.remove(); }

  /* ── DOM ──────────────────────────────────────────────────────────────── */
  function mount(){
    const back = document.createElement("div");
    back.className = "se-back";
    back.id = "se-back";
    back.innerHTML = `
      <div class="se-bar">
        <span class="se-title">sprite editor</span>
        <span class="se-sub" id="se-name"></span>
        <span class="se-spacer"></span>
        <button class="se-btn" id="se-undo" onclick="SpriteEdit.undo()" title="Undo (Ctrl+Z)">↶</button>
        <button class="se-btn" id="se-redo" onclick="SpriteEdit.redo()" title="Redo (Ctrl+Shift+Z)">↷</button>
        <button class="se-btn" onclick="SpriteEdit.fit()" title="Fit to view (0)">⊡ fit</button>
        <button class="se-btn" onclick="SpriteEdit.pick()">open…</button>
        <button class="se-btn go" id="se-save" onclick="SpriteEdit.save()">save sheet</button>
        <button class="se-btn" onclick="SpriteEdit.close()">close</button>
      </div>
      <div class="se-body">
        <div class="se-tools" id="se-tools"></div>
        <div class="se-stage" id="se-stage"><canvas id="se-view"></canvas>
          <div class="se-hud" id="se-hud"></div></div>
        <div class="se-side" id="se-side"></div>
      </div>`;
    document.body.appendChild(back);
    $ = {
      back, name: back.querySelector("#se-name"), tools: back.querySelector("#se-tools"),
      stage: back.querySelector("#se-stage"), view: back.querySelector("#se-view"),
      hud: back.querySelector("#se-hud"), side: back.querySelector("#se-side"),
      save: back.querySelector("#se-save"),
    };
    $.ctx = $.view.getContext("2d");
    $.tools.innerHTML = TOOLS.map(t =>
      `<div class="se-tool" data-tool="${t.id}" title="${E(t.t)}"
            onclick="SpriteEdit.setTool('${t.id}')">${t.g}</div>`).join("");
    bindStage();
    document.addEventListener("keydown", onKey, true);
    if (window.ResizeObserver){
      const ro = new ResizeObserver(() => paint());
      ro.observe($.stage);
      S.ro = ro;
    }
    S.onResize = () => paint();
    window.addEventListener("resize", S.onResize);
    // The first real size may only arrive after the stage is laid out; re-fit
    // once when it does, so the sheet is not stuck at whatever zoom a 1x1
    // canvas produced.
    S._fitPending = true;
    sizeCanvas();
    renderSide();
    renderTools();
  }

  /* Also called from the paint path, not just the ResizeObserver: an element
   * laid out while its pane is hidden never fires an observer callback, and the
   * editor then opens on a 1x1 canvas that looks like a crash. */
  function sizeCanvas(){
    if (!S || !$.view) return false;
    const r = $.stage.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(r.width * dpr));
    const h = Math.max(1, Math.round(r.height * dpr));
    S.dpr = dpr;
    if ($.view.width === w && $.view.height === h) return false;
    $.view.width = w; $.view.height = h;
    return true;
  }

  function renderTools(){
    if (!$.tools) return;
    $.tools.querySelectorAll(".se-tool").forEach(el =>
      el.classList.toggle("on", el.dataset.tool === S.tool));
  }

  /* ── grid maths ───────────────────────────────────────────────────────── */
  function grid(){
    const g = S.rig && S.rig.grid;
    if (g && g.cell_w > 0 && g.cell_h > 0) return g;
    return {cell_w:S.w, cell_h:S.h, cols:1, rows:1};
  }
  function frameCount(){ const g = grid(); return g.cols * g.rows; }
  function frameBox(i){
    const g = grid();
    const row = Math.floor(i / g.cols), col = i % g.cols;
    return {x: col*g.cell_w, y: row*g.cell_h, w: g.cell_w, h: g.cell_h, row, col};
  }
  function frameAt(px, py){
    const g = grid();
    const col = clamp(Math.floor(px / g.cell_w), 0, g.cols-1);
    const row = clamp(Math.floor(py / g.cell_h), 0, g.rows-1);
    return row * g.cols + col;
  }

  /* ── view transform ───────────────────────────────────────────────────── */
  function fit(){
    if (!S) return;
    const r = $.stage.getBoundingClientRect();
    const pad = 40;
    const z = Math.min((r.width-pad)/S.w, (r.height-pad)/S.h);
    S.zoom = clamp(Math.max(1, Math.floor(z)), 0.1, 64);
    S.pan.x = (r.width - S.w*S.zoom)/2;
    S.pan.y = (r.height - S.h*S.zoom)/2;
    paint();
  }
  function toImage(ev){
    const r = $.view.getBoundingClientRect();
    return {
      x: Math.floor((ev.clientX - r.left - S.pan.x) / S.zoom),
      y: Math.floor((ev.clientY - r.top  - S.pan.y) / S.zoom),
    };
  }
  function toImageF(ev){
    const r = $.view.getBoundingClientRect();
    return {
      x: (ev.clientX - r.left - S.pan.x) / S.zoom,
      y: (ev.clientY - r.top  - S.pan.y) / S.zoom,
    };
  }

  /* ── painting the view ────────────────────────────────────────────────── */
  /* Coalesce repaints to one per frame, WITHOUT latching. requestAnimationFrame
   * does not fire while the page is not compositing (hidden pane, background
   * tab); a plain `if (pending) return` guard then stays true forever and the
   * canvas is frozen for the rest of the session. The timeout is the escape
   * hatch — whichever fires first does the work and clears the flag. */
  function paint(){
    if (!S || !$.ctx) return;
    if (S._pending) return;
    S._pending = true;
    const run = () => { if (!S || !S._pending) return; S._pending = false; _paint(); };
    requestAnimationFrame(run);
    setTimeout(run, 120);
  }
  function _paint(){
    if (!S) return;
    if (sizeCanvas() && S._fitPending){ S._fitPending = false; fit(); return; }
    const c = $.ctx, dpr = S.dpr || 1;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = $.view.width/dpr, H = $.view.height/dpr;
    c.clearRect(0, 0, W, H);
    c.imageSmoothingEnabled = false;

    const z = S.zoom, px = S.pan.x, py = S.pan.y;

    // Transparency checkerboard, only under the sheet.
    const chk = 8;
    c.save();
    c.beginPath(); c.rect(px, py, S.w*z, S.h*z); c.clip();
    c.fillStyle = "#16181d"; c.fillRect(px, py, S.w*z, S.h*z);
    c.fillStyle = "#1d2026";
    for (let y = 0; y < S.h*z; y += chk)
      for (let x = ((y/chk)|0)%2 ? chk : 0; x < S.w*z; x += chk*2)
        c.fillRect(px+x, py+y, chk, chk);
    c.restore();

    // Onion skin: the previous frame, ghosted, under the live one.
    if (S.onion && S.focusFrame && frameCount() > 1 && S.frame > 0){
      const prev = frameBox(S.frame-1), cur = frameBox(S.frame);
      c.globalAlpha = 0.28;
      c.drawImage(S.work, prev.x, prev.y, prev.w, prev.h,
                  px+cur.x*z, py+cur.y*z, cur.w*z, cur.h*z);
      c.globalAlpha = 1;
    }

    c.drawImage(S.work, px, py, S.w*z, S.h*z);

    // In-progress line/rect preview lives on the view, never on the sheet, so
    // an abandoned drag leaves no trace.
    if (S.drag && S.drag.preview) drawPreview(c, S.drag);

    const g = grid();
    if (S.showGrid && g.cols*g.rows > 1){
      c.strokeStyle = "rgba(232,226,216,.28)"; c.lineWidth = 1;
      c.beginPath();
      for (let i = 1; i < g.cols; i++){
        const x = Math.round(px + i*g.cell_w*z) + .5;
        c.moveTo(x, py); c.lineTo(x, py + S.h*z);
      }
      for (let j = 1; j < g.rows; j++){
        const y = Math.round(py + j*g.cell_h*z) + .5;
        c.moveTo(px, y); c.lineTo(px + S.w*z, y);
      }
      c.stroke();
    }
    if (z >= 8){
      c.strokeStyle = "rgba(232,226,216,.07)"; c.lineWidth = 1;
      c.beginPath();
      for (let x = 0; x <= S.w; x++){ const X = Math.round(px+x*z)+.5; c.moveTo(X, py); c.lineTo(X, py+S.h*z); }
      for (let y = 0; y <= S.h; y++){ const Y = Math.round(py+y*z)+.5; c.moveTo(px, Y); c.lineTo(px+S.w*z, Y); }
      c.stroke();
    }

    // The focused frame: everything outside it dims, so an edit meant for
    // frame 7 cannot quietly land in frame 6.
    if (S.focusFrame && frameCount() > 1){
      const b = frameBox(S.frame);
      c.save();
      c.fillStyle = "rgba(6,7,9,.55)";
      c.beginPath();
      c.rect(px, py, S.w*z, S.h*z);
      c.rect(px+b.x*z, py+b.y*z, b.w*z, b.h*z);
      c.fill("evenodd");
      c.restore();
      c.strokeStyle = "var(--ember)"; c.strokeStyle = "#ff6a3d"; c.lineWidth = 2;
      c.strokeRect(px+b.x*z-1, py+b.y*z-1, b.w*z+2, b.h*z+2);
    }

    // Sheet border.
    c.strokeStyle = "rgba(232,226,216,.35)"; c.lineWidth = 1;
    c.strokeRect(Math.round(px)+.5, Math.round(py)+.5, S.w*z, S.h*z);

    drawAnchors(c);

    const hov = S.hover;
    $.hud.textContent = [
      `${S.w}×${S.h}  ·  ${Math.round(z*100)}%`,
      hov ? `px ${hov.x},${hov.y}` : "",
      frameCount() > 1 ? `frame ${S.frame}/${frameCount()-1}` : "",
      hov && frameCount() > 1 ? `cell ${hov.x - frameBox(S.frame).x},${hov.y - frameBox(S.frame).y}` : "",
    ].filter(Boolean).join("   ");
  }

  function drawPreview(c, d){
    const z = S.zoom, px = S.pan.x, py = S.pan.y;
    c.save();
    c.globalAlpha = .85;
    c.fillStyle = S.tool === "eraser" ? "rgba(255,106,61,.4)" : S.color;
    (d.preview || []).forEach(p => c.fillRect(px+p[0]*z, py+p[1]*z, z, z));
    c.restore();
  }

  function drawAnchors(c){
    const z = S.zoom, px = S.pan.x, py = S.pan.y;
    const labels = (S.rig.labels || []);
    labels.forEach(l => {
      const b = frameBox(l.frame);
      if (S.focusFrame && frameCount() > 1 && l.frame !== S.frame) return;
      const X = px + (b.x + l.x)*z, Y = py + (b.y + l.y)*z;
      const col = slotColor(l.slot);
      c.save();
      c.strokeStyle = col; c.lineWidth = 1.5;
      c.beginPath();
      c.moveTo(X-7, Y); c.lineTo(X+7, Y);
      c.moveTo(X, Y-7); c.lineTo(X, Y+7);
      c.stroke();
      c.beginPath(); c.arc(X, Y, 4, 0, Math.PI*2); c.stroke();
      c.fillStyle = col; c.font = "10px ui-monospace,monospace";
      c.fillText(l.slot, X+9, Y-6);
      c.restore();
    });
  }

  /* ── pixel operations ─────────────────────────────────────────────────── */
  function snapshot(){
    if (!S) return;
    const data = S.wctx.getImageData(0, 0, S.w, S.h);
    S.undo.push(data);
    S.undoBytes += data.data.length;
    while (S.undoBytes > UNDO_BYTES && S.undo.length > 1){
      S.undoBytes -= S.undo.shift().data.length;
    }
    S.redo.length = 0;
    refreshHistory();
  }
  function refreshHistory(){
    const u = document.getElementById("se-undo"), r = document.getElementById("se-redo");
    if (u) u.disabled = !S.undo.length;
    if (r) r.disabled = !S.redo.length;
    if ($.name) $.name.innerHTML =
      `${E(S.rel)}${S.dirty ? ' <span class="se-dirty">● unsaved</span>' : ""}`;
  }
  function undo(){
    if (!S || !S.undo.length) return;
    S.redo.push(S.wctx.getImageData(0, 0, S.w, S.h));
    const d = S.undo.pop();
    S.undoBytes -= d.data.length;
    S.wctx.putImageData(d, 0, 0);
    S.dirty = true; refreshHistory(); paint(); thumbs();
  }
  function redo(){
    if (!S || !S.redo.length) return;
    S.undo.push(S.wctx.getImageData(0, 0, S.w, S.h));
    S.undoBytes += S.w*S.h*4;
    S.wctx.putImageData(S.redo.pop(), 0, 0);
    S.dirty = true; refreshHistory(); paint(); thumbs();
  }

  function inBounds(x, y){
    if (x < 0 || y < 0 || x >= S.w || y >= S.h) return false;
    if (S.focusFrame && frameCount() > 1){
      const b = frameBox(S.frame);
      return x >= b.x && y >= b.y && x < b.x+b.w && y < b.y+b.h;
    }
    return true;
  }

  function put(x, y, erase){
    const n = S.brush;
    const half = Math.floor((n-1)/2);
    for (let dy = 0; dy < n; dy++) for (let dx = 0; dx < n; dx++){
      const X = x-half+dx, Y = y-half+dy;
      if (!inBounds(X, Y)) continue;
      if (erase) S.wctx.clearRect(X, Y, 1, 1);
      else { S.wctx.fillStyle = S.color; S.wctx.fillRect(X, Y, 1, 1); }
    }
  }

  function linePoints(x0, y0, x1, y1){
    const pts = [];
    let dx = Math.abs(x1-x0), sx = x0 < x1 ? 1 : -1;
    let dy = -Math.abs(y1-y0), sy = y0 < y1 ? 1 : -1;
    let err = dx+dy;
    for (;;){
      pts.push([x0, y0]);
      if (x0 === x1 && y0 === y1) break;
      const e2 = 2*err;
      if (e2 >= dy){ err += dy; x0 += sx; }
      if (e2 <= dx){ err += dx; y0 += sy; }
    }
    return pts;
  }
  function rectPoints(x0, y0, x1, y1, fill){
    const pts = [];
    const ax = Math.min(x0,x1), bx = Math.max(x0,x1);
    const ay = Math.min(y0,y1), by = Math.max(y0,y1);
    for (let y = ay; y <= by; y++) for (let x = ax; x <= bx; x++){
      if (fill || x === ax || x === bx || y === ay || y === by) pts.push([x, y]);
    }
    return pts;
  }

  function hexToRGBA(hex){
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
    return m ? [parseInt(m[1],16), parseInt(m[2],16), parseInt(m[3],16), 255]
             : [255,255,255,255];
  }
  function pixelAt(x, y){
    const d = S.wctx.getImageData(x, y, 1, 1).data;
    return [d[0], d[1], d[2], d[3]];
  }

  /* Flood fill inside the active frame only — a bucket that leaks across the
   * cell boundary would repaint the whole sheet, which is never what was meant. */
  function bucket(sx, sy){
    const box = (S.focusFrame && frameCount() > 1) ? frameBox(S.frame)
              : {x:0, y:0, w:S.w, h:S.h};
    const img = S.wctx.getImageData(box.x, box.y, box.w, box.h);
    const d = img.data, W = box.w, H = box.h;
    const px = sx-box.x, py = sy-box.y;
    if (px < 0 || py < 0 || px >= W || py >= H) return false;
    const at = i => [d[i], d[i+1], d[i+2], d[i+3]];
    const start = at((py*W+px)*4);
    const tgt = hexToRGBA(S.color);
    const same = (a, b) => a[0]===b[0] && a[1]===b[1] && a[2]===b[2] && a[3]===b[3];
    if (same(start, tgt)) return false;
    const stack = [[px, py]];
    const seen = new Uint8Array(W*H);
    while (stack.length){
      const [x, y] = stack.pop();
      if (x < 0 || y < 0 || x >= W || y >= H) continue;
      const k = y*W+x;
      if (seen[k]) continue;
      const i = k*4;
      if (!same(at(i), start)) continue;
      seen[k] = 1;
      d[i] = tgt[0]; d[i+1] = tgt[1]; d[i+2] = tgt[2]; d[i+3] = tgt[3];
      stack.push([x+1,y],[x-1,y],[x,y+1],[x,y-1]);
    }
    S.wctx.putImageData(img, box.x, box.y);
    return true;
  }

  /* ── frame-level operations ───────────────────────────────────────────── */
  function frameOp(kind){
    if (!S) return;
    const b = (frameCount() > 1) ? frameBox(S.frame) : {x:0,y:0,w:S.w,h:S.h};
    if (kind === "copy"){
      S.clip = S.wctx.getImageData(b.x, b.y, b.w, b.h);
      say(`frame ${S.frame} copied`, "ok");
      return;
    }
    if (kind === "paste"){
      if (!S.clip){ say("nothing copied yet"); return; }
      if (S.clip.width !== b.w || S.clip.height !== b.h){
        say("clipboard frame is a different size"); return;
      }
      snapshot();
      S.wctx.putImageData(S.clip, b.x, b.y);
    } else {
      snapshot();
      const img = S.wctx.getImageData(b.x, b.y, b.w, b.h);
      const out = S.wctx.createImageData(b.w, b.h);
      const gp = (x, y) => { const i = (y*b.w+x)*4; return [img.data[i],img.data[i+1],img.data[i+2],img.data[i+3]]; };
      const sp = (x, y, p) => { const i = (y*b.w+x)*4; out.data[i]=p[0];out.data[i+1]=p[1];out.data[i+2]=p[2];out.data[i+3]=p[3]; };
      for (let y = 0; y < b.h; y++) for (let x = 0; x < b.w; x++){
        if (kind === "fliph") sp(b.w-1-x, y, gp(x, y));
        else if (kind === "flipv") sp(x, b.h-1-y, gp(x, y));
        else if (kind === "clear") sp(x, y, [0,0,0,0]);
        else if (kind === "left")  sp((x+b.w-1)%b.w, y, gp(x, y));
        else if (kind === "right") sp((x+1)%b.w, y, gp(x, y));
        else if (kind === "up")    sp(x, (y+b.h-1)%b.h, gp(x, y));
        else if (kind === "down")  sp(x, (y+1)%b.h, gp(x, y));
        else sp(x, y, gp(x, y));
      }
      S.wctx.putImageData(out, b.x, b.y);
    }
    S.dirty = true; refreshHistory(); paint(); thumbs();
  }

  /* Trim the alpha halo a matte left behind: any pixel whose alpha sits in the
   * murky middle becomes fully transparent. This is the single most common
   * "fix the generated sheet" edit, and doing it by hand is 200 clicks. */
  function dehalo(threshold){
    if (!S) return;
    snapshot();
    const img = S.wctx.getImageData(0, 0, S.w, S.h);
    const d = img.data;
    let hit = 0;
    for (let i = 3; i < d.length; i += 4){
      if (d[i] > 0 && d[i] < threshold){ d[i] = 0; hit++; }
      else if (d[i] >= threshold && d[i] < 255){ d[i] = 255; }
    }
    S.wctx.putImageData(img, 0, 0);
    S.dirty = true; refreshHistory(); paint(); thumbs();
    say(`${hit} halo pixel${hit===1?"":"s"} cleared`, "ok");
  }

  /* ── stage interaction ────────────────────────────────────────────────── */
  function bindStage(){
    const v = $.view;
    v.addEventListener("pointerdown", ev => {
      if (!S) return;
      v.setPointerCapture(ev.pointerId);
      const p = toImage(ev);
      if (ev.button === 1 || ev.button === 2 || ev.altKey || S.space){
        S.drag = {pan:true, sx:ev.clientX, sy:ev.clientY, ox:S.pan.x, oy:S.pan.y};
        ev.preventDefault();
        return;
      }
      if (S.tool === "anchor"){ placeAnchor(ev); return; }
      if (S.tool === "picker"){ pickColor(p); return; }

      // Clicking outside the focused frame RETARGETS rather than refusing —
      // the whole sheet is visible, so a click on frame 5 obviously means
      // frame 5, not "nothing happened".
      if (S.focusFrame && frameCount() > 1 && p.x >= 0 && p.y >= 0 &&
          p.x < S.w && p.y < S.h){
        const f = frameAt(p.x, p.y);
        if (f !== S.frame){ setFrame(f); return; }
      }

      if (S.tool === "bucket"){
        if (p.x<0||p.y<0||p.x>=S.w||p.y>=S.h) return;
        snapshot();
        if (bucket(p.x, p.y)){ S.dirty = true; paint(); thumbs(); }
        else { S.undo.pop(); }
        refreshHistory();
        return;
      }
      if (S.tool === "line" || S.tool === "rect"){
        S.drag = {shape:S.tool, x0:p.x, y0:p.y, preview:[[p.x,p.y]], fill:ev.shiftKey};
        paint();
        return;
      }
      snapshot();
      S.drag = {paint:true, erase:S.tool === "eraser", last:p};
      put(p.x, p.y, S.drag.erase);
      S.dirty = true; paint(); thumbs();
    });

    v.addEventListener("pointermove", ev => {
      if (!S) return;
      const p = toImage(ev);
      S.hover = (p.x>=0 && p.y>=0 && p.x<S.w && p.y<S.h) ? p : null;
      const d = S.drag;
      if (d && d.pan){
        S.pan.x = d.ox + (ev.clientX - d.sx);
        S.pan.y = d.oy + (ev.clientY - d.sy);
        paint(); return;
      }
      if (d && d.paint){
        linePoints(d.last.x, d.last.y, p.x, p.y).forEach(pt => put(pt[0], pt[1], d.erase));
        d.last = p;
        paint(); return;
      }
      if (d && d.shape){
        d.x1 = p.x; d.y1 = p.y;
        d.preview = d.shape === "line" ? linePoints(d.x0, d.y0, p.x, p.y)
                                       : rectPoints(d.x0, d.y0, p.x, p.y, d.fill);
        paint(); return;
      }
      paint();
    });

    const finish = ev => {
      if (!S) return;
      const d = S.drag;
      S.drag = null;
      if (d && d.shape && d.preview && d.preview.length > 1){
        snapshot();
        const erase = false;
        d.preview.forEach(pt => put(pt[0], pt[1], erase));
        S.dirty = true; thumbs();
      } else if (d && d.paint){
        thumbs();
      }
      refreshHistory(); paint();
    };
    v.addEventListener("pointerup", finish);
    v.addEventListener("pointercancel", finish);
    v.addEventListener("contextmenu", ev => ev.preventDefault());

    v.addEventListener("wheel", ev => {
      if (!S) return;
      ev.preventDefault();
      const r = v.getBoundingClientRect();
      const mx = ev.clientX - r.left, my = ev.clientY - r.top;
      const before = {x:(mx-S.pan.x)/S.zoom, y:(my-S.pan.y)/S.zoom};
      const step = ev.deltaY < 0 ? 1.2 : 1/1.2;
      S.zoom = clamp(S.zoom*step, 0.1, 64);
      S.pan.x = mx - before.x*S.zoom;
      S.pan.y = my - before.y*S.zoom;
      paint();
    }, {passive:false});
  }

  function pickColor(p){
    if (p.x<0||p.y<0||p.x>=S.w||p.y>=S.h) return;
    const [r,g,b,a] = pixelAt(p.x, p.y);
    if (!a){ say("that pixel is transparent"); return; }
    S.color = "#" + [r,g,b].map(v => v.toString(16).padStart(2,"0")).join("");
    setTool("pencil");
    renderSide();
  }

  function onKey(ev){
    if (!S) return;
    const t = ev.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)){
      if (ev.key === "Escape") t.blur();
      return;
    }
    const k = ev.key.toLowerCase();
    if (ev.key === "Escape"){ ev.preventDefault(); close(); return; }
    if ((ev.ctrlKey || ev.metaKey) && k === "z"){
      ev.preventDefault(); ev.shiftKey ? redo() : undo(); return;
    }
    if ((ev.ctrlKey || ev.metaKey) && k === "y"){ ev.preventDefault(); redo(); return; }
    if ((ev.ctrlKey || ev.metaKey) && k === "s"){ ev.preventDefault(); save(); return; }
    if (ev.key === " "){ S.space = true; return; }
    if (k === "0"){ ev.preventDefault(); fit(); return; }
    if (k === "[" ){ S.brush = clamp(S.brush-1, 1, 16); renderSide(); return; }
    if (k === "]" ){ S.brush = clamp(S.brush+1, 1, 16); renderSide(); return; }
    if (ev.key === "ArrowRight"){ ev.preventDefault(); setFrame(clamp(S.frame+1, 0, frameCount()-1)); return; }
    if (ev.key === "ArrowLeft"){ ev.preventDefault(); setFrame(clamp(S.frame-1, 0, frameCount()-1)); return; }
    const tool = TOOLS.find(x => x.k === k);
    if (tool && !ev.ctrlKey && !ev.metaKey){ setTool(tool.id); }
  }
  document.addEventListener("keyup", ev => { if (S && ev.key === " ") S.space = false; });

  /* ── rig labels ───────────────────────────────────────────────────────── */
  function placeAnchor(ev){
    const p = toImageF(ev);
    if (p.x < 0 || p.y < 0 || p.x >= S.w || p.y >= S.h) return;
    const f = frameCount() > 1 ? frameAt(Math.floor(p.x), Math.floor(p.y)) : 0;
    const b = frameBox(f);
    const x = Math.round((p.x - b.x) * 10) / 10;
    const y = Math.round((p.y - b.y) * 10) / 10;
    S.rig.labels = (S.rig.labels || []).filter(l => !(l.frame === f && l.slot === S.slot));
    S.rig.labels.push({slot:S.slot, frame:f, x, y, source:"authored", note:""});
    S.rig.labels.sort((a, b2) => a.frame - b2.frame || a.slot.localeCompare(b2.slot));
    S.frame = f;
    S.rigDirty = true;
    renderSide(); paint();
  }
  function dropLabel(frame, slot){
    S.rig.labels = (S.rig.labels || []).filter(l => !(l.frame === frame && l.slot === slot));
    S.rigDirty = true;
    renderSide(); paint();
  }
  function setSlot(v){ S.slot = v; setTool("anchor"); renderSide(); }

  /* Carry the current frame's labels onto every frame that has none. A pose
   * sheet's hand barely moves between frames, so "same place unless I say
   * otherwise" beats placing forty anchors by hand — and each copy is still an
   * authored anchor a person can drag. */
  function spreadLabels(){
    const here = (S.rig.labels || []).filter(l => l.frame === S.frame);
    if (!here.length){ say("this frame has no labels to spread"); return; }
    const n = frameCount();
    let added = 0;
    for (let f = 0; f < n; f++){
      here.forEach(src => {
        if ((S.rig.labels || []).some(l => l.frame === f && l.slot === src.slot)) return;
        S.rig.labels.push({...src, frame:f});
        added++;
      });
    }
    S.rig.labels.sort((a, b) => a.frame - b.frame || a.slot.localeCompare(b.slot));
    S.rigDirty = true;
    renderSide(); paint();
    say(`${added} label${added===1?"":"s"} copied across frames`, "ok");
  }

  /* ── grid + animations ────────────────────────────────────────────────── */
  function setGrid(cw, ch){
    if (!(cw > 0 && ch > 0)){ say("cell size must be positive"); return false; }
    if (S.w % cw || S.h % ch){
      say(`${cw}×${ch} does not tile a ${S.w}×${S.h} sheet`); return false;
    }
    S.rig.grid = {cell_w:cw, cell_h:ch, cols:S.w/cw, rows:S.h/ch};
    S.frame = clamp(S.frame, 0, frameCount()-1);
    S.rigDirty = true;
    renderSide(); paint();
    return true;
  }
  function applyGrid(){
    setGrid(parseInt(document.getElementById("se-cw").value, 10),
            parseInt(document.getElementById("se-ch").value, 10));
  }
  /* "Four frames across, two rows" is the shape an artist actually knows —
     the cell size is the derived number, not the authored one. */
  function applyCells(){
    const cols = parseInt(document.getElementById("se-cols").value, 10);
    const rows = parseInt(document.getElementById("se-rows").value, 10);
    if (!(cols > 0 && rows > 0)){ say("frame counts must be positive"); return; }
    if (S.w % cols || S.h % rows){
      say(`${cols}×${rows} frames do not divide a ${S.w}×${S.h} sheet evenly`);
      return;
    }
    setGrid(S.w/cols, S.h/rows);
  }
  async function detectGrid(){
    const r = await mutate("/api/sprite/autogrid", {body:{rel:S.rel}, quiet:true});
    if (!r.ok){ say(r.error); return; }
    S.rig.grid = r.data.grid;
    S.frame = clamp(S.frame, 0, frameCount()-1);
    S.rigDirty = true;
    renderSide(); paint();
    say(`detected ${r.data.grid.cols}×${r.data.grid.rows} of ${r.data.grid.cell_w}×${r.data.grid.cell_h}`, "ok");
  }
  function rowAnimations(){
    const g = grid();
    S.rig.animations = [];
    for (let r = 0; r < g.rows; r++){
      S.rig.animations.push({
        name: `anim_${r}`, loop: true, fps: null,
        frames: Array.from({length:g.cols}, (_, c) => r*g.cols + c),
      });
    }
    S.rigDirty = true;
    renderSide();
  }
  function addAnimation(){
    (S.rig.animations = S.rig.animations || []).push(
      {name:`anim_${S.rig.animations.length}`, frames:[S.frame], loop:true, fps:null});
    S.rigDirty = true; renderSide();
  }
  function animField(i, field, v){
    const a = (S.rig.animations || [])[i];
    if (!a) return;
    if (field === "name") a.name = v;
    else if (field === "loop") a.loop = !!v;
    else if (field === "frames"){
      a.frames = String(v).split(/[^0-9]+/).filter(x => x !== "")
        .map(Number).filter(n => n >= 0 && n < frameCount());
    }
    S.rigDirty = true;
  }
  function dropAnimation(i){
    (S.rig.animations || []).splice(i, 1);
    S.rigDirty = true; renderSide();
  }
  function addFrameToAnim(i){
    const a = (S.rig.animations || [])[i];
    if (!a) return;
    a.frames.push(S.frame);
    S.rigDirty = true; renderSide();
  }

  function setFrame(i){
    S.frame = clamp(i|0, 0, frameCount()-1);
    renderSide(); paint(); thumbs();
  }
  /* Ctrl/Cmd-click picks for a batch; shift-click extends the pick from the
     focused frame, which is how you select a whole animation in one gesture. */
  function clickFrame(ev, i){
    if (ev && (ev.ctrlKey || ev.metaKey)) return togglePicked(i);
    if (ev && ev.shiftKey){
      const [lo, hi] = i < S.frame ? [i, S.frame] : [S.frame, i];
      for (let f = lo; f <= hi; f++) S.picked.add(f);
      renderSide(); thumbs();
      return;
    }
    setFrame(i);
  }
  function setTool(t){ S.tool = t; renderTools(); paint(); }

  /* ── side panel ───────────────────────────────────────────────────────── */
  function palette(){
    // Top colours actually present in the sheet — an artist fixing a halo needs
    // the EXACT shade next to it, and eyedropping is one click too many when
    // the sheet's own palette can just be on screen.
    try {
      const d = S.wctx.getImageData(0, 0, S.w, S.h).data;
      const seen = new Map();
      const step = Math.max(1, Math.floor((S.w*S.h)/40000));
      for (let i = 0; i < d.length; i += 4*step){
        if (d[i+3] < 128) continue;
        const key = (d[i]<<16)|(d[i+1]<<8)|d[i+2];
        seen.set(key, (seen.get(key)||0)+1);
      }
      return [...seen.entries()].sort((a,b) => b[1]-a[1]).slice(0, 28)
        .map(([k]) => "#" + k.toString(16).padStart(6, "0"));
    } catch (e) { return []; }
  }

  function renderSide(){
    if (!S || !$.side) return;
    const g = grid();
    const n = frameCount();
    const labels = (S.rig.labels || []).filter(l => l.frame === S.frame);
    const slots = [...new Set([...(window.SpriteEdit.KNOWN_SLOTS || []),
                               ...(S.rig.labels || []).map(l => l.slot)])];
    const pal = palette();
    const cov = coverageNow();

    $.side.innerHTML = `
      <div class="se-h">brush</div>
      <div class="se-row">
        <input class="se-sw" type="color" value="${E(S.color)}" oninput="SpriteEdit.setColor(this.value)">
        <label>size</label>
        <input class="se-in num" type="number" min="1" max="16" value="${S.brush}"
               oninput="SpriteEdit.setBrush(this.value)">
      </div>
      <div class="se-pal">${pal.map(c =>
        `<span class="se-pc${c.toLowerCase()===S.color.toLowerCase()?" on":""}"
               title="${E(c)}" onclick="SpriteEdit.setColor('${E(c)}')"><span style="background:${E(c)}"></span></span>`).join("")}</div>

      <div class="se-h">sheet grid</div>
      <div class="se-row">
        <label>cell</label>
        <input class="se-in num" id="se-cw" type="number" min="1" value="${g.cell_w}">
        <span style="color:var(--ash2)">×</span>
        <input class="se-in num" id="se-ch" type="number" min="1" value="${g.cell_h}">
        <button class="se-btn" onclick="SpriteEdit.applyGrid()">set</button>
      </div>
      <div class="se-row">
        <label>frames</label>
        <input class="se-in num" id="se-cols" type="number" min="1" value="${g.cols}">
        <span style="color:var(--ash2)">×</span>
        <input class="se-in num" id="se-rows" type="number" min="1" value="${g.rows}">
        <button class="se-btn" onclick="SpriteEdit.applyCells()">set</button>
      </div>
      <div class="se-row">
        <button class="se-btn" onclick="SpriteEdit.detectGrid()">detect</button>
        <span class="se-sub">${g.cols}×${g.rows} = ${n} frame${n===1?"":"s"}</span>
      </div>
      <div class="se-note" style="margin:-2px 0 8px">Detection finds the finest
        lattice no drawing straddles — on a sparse sheet that splits one pose
        into three. If the cells look too small, say how many frames across
        instead.</div>
      <div class="se-row">
        <label><input type="checkbox" ${S.showGrid?"checked":""} onchange="SpriteEdit.toggle('showGrid',this.checked)"> grid</label>
        <label><input type="checkbox" ${S.focusFrame?"checked":""} onchange="SpriteEdit.toggle('focusFrame',this.checked)"> focus</label>
        <label><input type="checkbox" ${S.onion?"checked":""} onchange="SpriteEdit.toggle('onion',this.checked)"> onion</label>
      </div>

      <div class="se-h">frames</div>
      <div class="se-strip" id="se-strip"></div>
      <div class="se-row" style="margin-top:8px;flex-wrap:wrap">
        <button class="se-btn" title="Flip horizontally" onclick="SpriteEdit.frameOp('fliph')">⇋</button>
        <button class="se-btn" title="Flip vertically" onclick="SpriteEdit.frameOp('flipv')">⇅</button>
        <button class="se-btn" title="Nudge left" onclick="SpriteEdit.frameOp('left')">←</button>
        <button class="se-btn" title="Nudge right" onclick="SpriteEdit.frameOp('right')">→</button>
        <button class="se-btn" title="Nudge up" onclick="SpriteEdit.frameOp('up')">↑</button>
        <button class="se-btn" title="Nudge down" onclick="SpriteEdit.frameOp('down')">↓</button>
        <button class="se-btn" onclick="SpriteEdit.frameOp('copy')">copy</button>
        <button class="se-btn" onclick="SpriteEdit.frameOp('paste')">paste</button>
        <button class="se-btn" onclick="SpriteEdit.frameOp('clear')">clear</button>
      </div>
      <div class="se-row" style="margin-top:8px">
        <button class="se-btn" title="Clear the semi-transparent halo a matte left behind"
                onclick="SpriteEdit.dehalo(128)">de-halo sheet</button>
      </div>

      <div class="se-h">rig labels · frame ${S.frame}</div>
      <div class="se-note" style="margin-bottom:7px">Where the character's hands
        <b>are</b>. Click a button, then click the pixel.</div>
      <div class="se-hands">${HANDS.map(h => handBtn(h)).join("")}</div>
      <div class="se-row" style="margin-top:6px">
        <button class="se-btn" title="A character that turns around keeps its main hand while its left and right swap sides."
                onclick="SpriteEdit.swapHands(false)">swap L↔R here</button>
        <button class="se-btn" onclick="SpriteEdit.swapHands(true)">all frames</button>
      </div>

      <div class="se-note" style="margin:11px 0 7px">Which hand <b>holds the
        weapon</b> — this is what the gear layer equips against.</div>
      <div class="se-hands">${GRIPS.map(h => handBtn(h)).join("")}</div>

      <div class="se-row" style="margin-top:11px">
        <label>other</label>
        <select class="se-in" onchange="SpriteEdit.setSlot(this.value)">
          ${slots.map(s => `<option value="${E(s)}"${s===S.slot?" selected":""}>${E(s)}</option>`).join("")}
        </select>
      </div>
      ${labels.length ? labels.map(l => `
        <div class="se-lab"><i style="background:${slotColor(l.slot)}"></i>
          ${E(l.slot)} · ${l.x},${l.y}
          <span class="x" title="remove" onclick="SpriteEdit.dropLabel(${l.frame},'${E(l.slot)}')">✕</span></div>`).join("")
        : `<div class="se-note se-warn">frame ${S.frame} has no labels</div>`}
      <div class="se-row" style="margin-top:7px">
        <button class="se-btn" onclick="SpriteEdit.spreadLabels()">spread to all frames</button>
      </div>
      ${cov}

      <div class="se-h">regenerate frames</div>
      ${regenPanel()}

      <div class="se-h">animations</div>
      ${(S.rig.animations || []).map((a, i) => `
        <div class="se-anim">
          <div class="hd">
            <input class="se-in" value="${E(a.name)}" oninput="SpriteEdit.animField(${i},'name',this.value)">
            <label title="loop"><input type="checkbox" ${a.loop?"checked":""}
              onchange="SpriteEdit.animField(${i},'loop',this.checked)"></label>
            <span class="x" style="cursor:pointer;color:var(--ash2)" onclick="SpriteEdit.dropAnimation(${i})">✕</span>
          </div>
          <input class="se-in" value="${a.frames.join(", ")}"
                 oninput="SpriteEdit.animField(${i},'frames',this.value)">
          <div class="fr" style="margin-top:4px">${a.frames.length} frame${a.frames.length===1?"":"s"}
            · <span style="cursor:pointer;text-decoration:underline"
                    onclick="SpriteEdit.addFrameToAnim(${i})">+ frame ${S.frame}</span></div>
        </div>`).join("")}
      <div class="se-row">
        <button class="se-btn" onclick="SpriteEdit.addAnimation()">+ animation</button>
        <button class="se-btn" onclick="SpriteEdit.rowAnimations()">one per row</button>
      </div>

      <div class="se-h">save</div>
      <div class="se-row"><button class="se-btn" style="flex:1" id="se-rigsave"
        onclick="SpriteEdit.saveRig()">save rig${S.rigDirty ? " ●" : ""}</button></div>
      <div class="se-row"><button class="se-btn" style="flex:1"
        onclick="SpriteEdit.exportFrames()">export SpriteFrames .tres</button></div>
      <div class="se-note">The sidecar is <b>${E((S.info && S.info.sidecar) || "")}</b> —
        it travels with the art, not the database.</div>`;
    thumbs();
    refreshHistory();
  }

  /* One button per hand, and it carries its own state: whether this frame is
     already labelled, and whether it is the slot the next click will place. */
  function handBtn(h){
    const has = (S.rig.labels || []).some(
      l => l.frame === S.frame && l.slot === h.slot);
    return `<button class="se-hand${S.slot === h.slot ? " on" : ""}${has ? " has" : ""}"
      style="--hc:${slotColor(h.slot)}" title="${E(h.label)}"
      onclick="SpriteEdit.setSlot('${h.slot}')">
      <i></i>${E(h.short)}<span class="tick">${has ? "✓" : ""}</span></button>`;
  }

  function coverageNow(){
    const played = new Set();
    (S.rig.animations || []).forEach(a => (a.frames || []).forEach(f => played.add(f)));
    const slots = [...new Set((S.rig.labels || []).map(l => l.slot))];
    if (!slots.length || !played.size) return "";
    const rows = slots.map(s => {
      const have = new Set((S.rig.labels || []).filter(l => l.slot === s).map(l => l.frame));
      const miss = [...played].filter(f => !have.has(f));
      return `<div class="se-note${miss.length ? " se-warn" : ""}">
        <b>${E(s)}</b> ${miss.length ? `missing on frame ${miss.slice(0,8).join(", ")}${miss.length>8?"…":""}`
                                     : "covers every played frame"}</div>`;
    }).join("");
    return `<div class="se-h">coverage</div>${rows}`;
  }

  /* ── frame regeneration ───────────────────────────────────────────────────
   * Repaint chosen frames from a prompt, leaving the rest of the sheet alone.
   * That scoping IS the feature: a sheet is usually eleven-twelfths right, and
   * re-rolling the whole thing to fix one pose loses the eleven that were fine.
   *
   * Nothing is written to disk. Each result comes back as pixels, lands in a
   * review strip, and only becomes part of the sheet when accepted — at which
   * point it enters the normal undo stack and the normal save path.
   */
  function pickedFrames(){
    return S.picked.size ? [...S.picked].sort((a, b) => a - b) : [S.frame];
  }

  function regenPanel(){
    const r = S.regen;
    if (r.status && !r.status.available){
      return `<div class="se-note se-warn">Frame regeneration is off: ${
        E(r.status.reason || "no image provider configured")}</div>`;
    }
    const frames = pickedFrames();
    const price = r.status && r.status.price_usd
      ? (r.status.price_usd[r.quality] || 0) : 0;
    const cost = price ? `~$${(price * frames.length).toFixed(3)}` : "";
    const max = (r.status && r.status.max_frames) || 12;
    const over = frames.length > max;
    return `
      <div class="se-note" style="margin-bottom:7px">
        ${S.picked.size
          ? `<b>${frames.length}</b> frame${frames.length===1?"":"s"} picked —
             ${frames.join(", ")}`
          : `Ctrl-click frames in the strip to pick several. Right now:
             <b>frame ${S.frame}</b> only.`}
      </div>
      <textarea class="se-ta" id="se-rp" placeholder="What should change? e.g. give the raised hand a lit torch, keep everything else identical"
        oninput="SpriteEdit.regenField('prompt',this.value)">${E(r.prompt)}</textarea>
      <div class="se-row" style="margin-top:6px">
        <label>quality</label>
        <select class="se-in" onchange="SpriteEdit.regenField('quality',this.value)">
          ${(r.status ? r.status.qualities : ["low","medium","high"]).map(q =>
            `<option value="${E(q)}"${q===r.quality?" selected":""}>${E(q)}</option>`).join("")}
        </select>
        <span class="se-sub">${E(cost)}</span>
      </div>
      ${over ? `<div class="se-note se-warn">${frames.length} frames is over the
        ${max}-frame cap for one batch — pick fewer.</div>` : ""}
      <div class="se-row" style="margin-top:6px">
        <button class="se-btn go" id="se-regen" ${r.busy || over ? "disabled" : ""}
          onclick="SpriteEdit.regenerate()">${r.busy
            ? `working… ${r.done || 0}/${frames.length}`
            : `regenerate ${frames.length} frame${frames.length===1?"":"s"}`}</button>
        ${S.picked.size ? `<button class="se-btn" onclick="SpriteEdit.clearPicked()">clear</button>` : ""}
      </div>
      <div class="se-note" style="margin-top:6px">The cell is enlarged for the
        model and brought back down, so this is a <b>repaint</b>, not a pixel
        edit — expect to touch it up. Nothing is written until you accept, and
        nothing is saved until you save.</div>
      ${r.results.length ? regenResults() : ""}`;
  }

  function regenResults(){
    return `<div class="se-h" style="margin-top:14px">results</div>
      ${S.regen.results.map((res, i) => `
        <div class="se-res${res.error ? " bad" : ""}">
          <div class="hd">frame ${res.frame}
            <span class="m">${res.error ? "failed"
              : `${res.seconds || "?"}s · ~$${(res.estimated_usd || 0).toFixed(3)}`}</span></div>
          ${res.error
            ? `<div class="se-note se-warn">${E(res.error)}</div>`
            : `<div class="pair">
                 <figure><img src="${res.before}" alt=""><figcaption>before</figcaption></figure>
                 <figure><img src="${res.after}" alt=""><figcaption>after</figcaption></figure>
               </div>
               <div class="acts">
                 <button class="se-btn go" onclick="SpriteEdit.acceptRegen(${i})">accept</button>
                 <button class="se-btn" onclick="SpriteEdit.dropRegen(${i})">discard</button>
               </div>`}
        </div>`).join("")}
      ${S.regen.results.some(r => !r.error && !r.applied) ? `
        <div class="se-row"><button class="se-btn" style="flex:1"
          onclick="SpriteEdit.acceptAllRegen()">accept every result</button></div>` : ""}`;
  }

  function cellDataURL(frame){
    const b = frameBox(frame);
    const c = document.createElement("canvas");
    c.width = b.w; c.height = b.h;
    const cx = c.getContext("2d");
    cx.imageSmoothingEnabled = false;
    cx.drawImage(S.work, b.x, b.y, b.w, b.h, 0, 0, b.w, b.h);
    return c.toDataURL("image/png");
  }

  async function regenStatus(){
    if (!S || S.regen.status) return S && S.regen.status;
    const d = await readJSON("/api/sprite/regen/status", null);
    if (!S) return null;
    S.regen.status = d && !d.__error ? d : {available:false, reason:d && d.__error};
    renderSide();
    return S.regen.status;
  }

  async function regenerate(){
    if (!S || S.regen.busy) return;
    const prompt = (S.regen.prompt || "").trim();
    if (!prompt){ say("say what should change first"); return; }
    if (S.dirty && !confirm(
        "The sheet has unsaved pixel edits. Regeneration reads the file ON DISK, "
        + "so those edits will not be in the reference. Continue?")) return;

    const frames = pickedFrames();
    S.regen.busy = true; S.regen.done = 0; S.regen.results = [];
    renderSide();

    // Two at a time: enough to hide latency, few enough that a wrong prompt
    // costs two calls before you can stop it.
    const queue = frames.slice();
    const worker = async () => {
      while (queue.length){
        const frame = queue.shift();
        const before = cellDataURL(frame);
        const r = await mutate("/api/sprite/regen", {
          body: { rel:S.rel, frame, prompt, quality:S.regen.quality,
                  grid: S.rig.grid || null },
          quiet: true });
        if (!S) return;
        S.regen.done++;
        S.regen.results.push(r.ok
          ? { frame, before, after: "data:image/png;base64," + r.data.png,
              seconds: r.data.seconds, estimated_usd: r.data.estimated_usd,
              applied: false }
          : { frame, before, error: r.error });
        S.regen.results.sort((a, b) => a.frame - b.frame);
        renderSide();
      }
    };
    await Promise.all([worker(), worker()]);
    if (!S) return;
    S.regen.busy = false;
    renderSide();
    const bad = S.regen.results.filter(x => x.error).length;
    say(bad ? `${S.regen.results.length - bad} of ${S.regen.results.length} frames came back`
            : `${S.regen.results.length} frame(s) ready — accept or discard each`,
        bad ? undefined : "ok");
  }

  function applyResult(res){
    return new Promise(resolve => {
      const im = new Image();
      im.onload = () => {
        const b = frameBox(res.frame);
        S.wctx.clearRect(b.x, b.y, b.w, b.h);
        S.wctx.drawImage(im, b.x, b.y, b.w, b.h);
        resolve(true);
      };
      im.onerror = () => resolve(false);
      im.src = res.after;
    });
  }

  async function acceptRegen(i){
    const res = S.regen.results[i];
    if (!res || res.error || res.applied) return;
    snapshot();
    if (!await applyResult(res)){ say("that result would not decode"); return; }
    res.applied = true;
    S.dirty = true;
    S.regen.results.splice(i, 1);
    refreshHistory(); renderSide(); paint(); thumbs();
  }

  async function acceptAllRegen(){
    const pending = S.regen.results.filter(r => !r.error && !r.applied);
    if (!pending.length) return;
    snapshot();                       // one undo step for the whole batch
    for (const res of pending) await applyResult(res);
    S.regen.results = S.regen.results.filter(r => r.error);
    S.dirty = true;
    refreshHistory(); renderSide(); paint(); thumbs();
    say(`${pending.length} frame(s) applied — save the sheet to keep them`, "ok");
  }

  function dropRegen(i){ S.regen.results.splice(i, 1); renderSide(); }
  function regenField(field, v){
    S.regen[field] = v;
    if (field !== "prompt") renderSide();     // typing must not repaint the box
  }
  function clearPicked(){ S.picked.clear(); renderSide(); thumbs(); }
  function togglePicked(i){
    S.picked.has(i) ? S.picked.delete(i) : S.picked.add(i);
    renderSide(); thumbs();
  }

  function swapHands(all){
    const [left, right] = HANDS.map(h => h.slot);
    (S.rig.labels || []).forEach(l => {
      if (!all && l.frame !== S.frame) return;
      if (l.slot === left) l.slot = right;
      else if (l.slot === right) l.slot = left;
    });
    (S.rig.labels || []).sort((a, b) => a.frame - b.frame || a.slot.localeCompare(b.slot));
    S.rigDirty = true;
    renderSide(); paint(); thumbs();
  }

  /* Frame thumbnails are drawn from the LIVE canvas, so an edit shows up in the
   * strip immediately — a strip that lags the sheet is worse than no strip. */
  function thumbs(){
    const host = document.getElementById("se-strip");
    if (!host || !S) return;
    const n = frameCount();
    if (host.childElementCount !== n){
      host.innerHTML = Array.from({length:n}, (_, i) => `
        <div class="se-fr" data-f="${i}" title="Click to focus · Ctrl-click to pick for regeneration"
             onclick="SpriteEdit.clickFrame(event,${i})">
          <canvas></canvas><span class="n">${i}</span><span class="dots"></span></div>`).join("");
    }
    host.querySelectorAll(".se-fr").forEach(el => {
      const i = +el.dataset.f;
      el.classList.toggle("on", i === S.frame);
      el.classList.toggle("picked", S.picked.has(i));
      const b = frameBox(i);
      const c = el.querySelector("canvas");
      if (c.width !== b.w || c.height !== b.h){ c.width = b.w; c.height = b.h; }
      const cx = c.getContext("2d");
      cx.imageSmoothingEnabled = false;
      cx.clearRect(0, 0, b.w, b.h);
      cx.drawImage(S.work, b.x, b.y, b.w, b.h, 0, 0, b.w, b.h);
      const marks = (S.rig.labels || []).filter(l => l.frame === i);
      el.querySelector(".dots").innerHTML = marks.slice(0, 4)
        .map(m => `<i style="background:${slotColor(m.slot)}"></i>`).join("");
    });
  }

  /* ── persistence ──────────────────────────────────────────────────────── */
  async function save(){
    if (!S) return;
    const png = S.work.toDataURL("image/png");
    const r = await mutate("/api/sprite/save", {
      body:{rel:S.rel, png, mtime:S.mtime}, button:"se-save"});
    if (!r.ok) return;
    S.dirty = false;
    S.mtime = r.data.mtime;
    refreshHistory();
    say(`saved · previous copy at ${r.data.backup}`, "ok");
    try { if (window.Atlas) Atlas.badge(); } catch (e) {}
  }

  async function saveRig(){
    if (!S) return;
    const rig = {
      grid: S.rig.grid || null,
      fps: S.rig.fps || 10,
      animations: (S.rig.animations || []).map(a => ({
        name:a.name, frames:a.frames, loop:a.loop, fps:a.fps || null})),
      labels: (S.rig.labels || []),
      notes: S.rig.notes || "",
    };
    const r = await mutate("/api/sprite/rig", {body:{rel:S.rel, rig}, button:"se-rigsave"});
    if (!r.ok) return;
    S.rig = r.data.rig;
    S.rigDirty = false;
    renderSide(); paint();
    say(`rig saved to ${r.data.sidecar}`, "ok");
  }

  async function exportFrames(){
    if (!S) return;
    if (S.rigDirty) await saveRig();
    const r = await mutate("/api/sprite/spriteframes", {body:{rel:S.rel}});
    if (!r.ok) return;
    say(`wrote ${r.data.written} · ${(r.data.animations||[]).join(", ")}`, "ok");
    try { if (window.Atlas) Atlas.badge(); } catch (e) {}
  }

  function setColor(v){ if (S){ S.color = v; renderSide(); } }
  function setBrush(v){ if (S){ S.brush = clamp(parseInt(v,10)||1, 1, 16); } }
  function toggle(field, on){ if (S){ S[field] = !!on; paint(); } }

  return {
    open, close, pick, pickSearch, closePick, fit, undo, redo, save, saveRig, exportFrames,
    setTool, setColor, setBrush, setFrame, clickFrame, setSlot, dropLabel,
    spreadLabels, swapHands,
    applyGrid, applyCells, detectGrid, rowAnimations, addAnimation, animField, dropAnimation,
    addFrameToAnim, frameOp, dehalo, toggle,
    regenerate, regenField, acceptRegen, acceptAllRegen, dropRegen,
    togglePicked, clearPicked,
    KNOWN_SLOTS: ["left_hand","right_hand","main_hand","off_hand","head","body",
                  "feet","throwable","pivot","muzzle","fx"],
    get state(){ return S; },
  };
})();
