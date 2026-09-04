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
  /* Both editors were built as fullscreen overlays appended to <body>. Studio
     needs them as pages, so mount() takes a host: when _host is set the same
     markup lands inside it and drops the fixed positioning and the close
     button (there is nothing to close back to inside a tab). */
  let _host = null;

  /* On screen right now — not merely constructed. getClientRects() is the check
     rather than offsetParent, because offsetParent is null for a positioned
     element that is perfectly visible. A detached or display:none host has no
     rects; a real one always has at least one. */
  const visible = el => !!el && el.isConnected && el.getClientRects().length > 0;
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  // Icon set, one grid, one stroke weight — see icons.js. Never a raw glyph.
  const I = (name, size) => (window.BGIcon ? BGIcon(name, { size: size || 16 }) : "");
  const say = (m, k) => { try { toast(m, k); } catch (e) { console.warn(m); } };
  const clamp = (v, lo, hi) => v < lo ? lo : v > hi ? hi : v;

  // Undo is ImageData snapshots. Cheap to implement, correct for every tool
  // including flood fill, and bounded by BYTES rather than by count — a 1024²
  // sheet and a 32² sheet must not get the same 40-deep history.
  const UNDO_BYTES = 96 * 1024 * 1024;

  /* Geometry from the icon set, not symbol-font characters. The seven tools
     used to be ✎ ⌫ ◍ ⊙ ╱ ▭ ✜, resolved through whatever glyph the OS had, so
     no two shared a stroke weight or a baseline — the exact failure icons.js
     was built to end. `i` is a registry name; a name the set has not drawn yet
     renders as a visible dashed placeholder rather than a blank button. */
  const TOOLS = [
    {id:"pencil", i:"brush",      k:"b", t:"Pencil - paint pixels (B)"},
    {id:"eraser", i:"eraser",     k:"e", t:"Eraser - clear to transparent (E)"},
    {id:"bucket", i:"fill",       k:"g", t:"Fill - flood the contiguous colour (G)"},
    {id:"picker", i:"eyedropper", k:"i", t:"Eyedropper - take a colour off the sheet (I)"},
    {id:"line",   i:"line",       k:"l", t:"Line (L)"},
    {id:"rect",   i:"rect",       k:"r", t:"Rectangle - hold Shift to fill (R)"},
    {id:"anchor", i:"anchor",     k:"a", t:"Rig anchor - mark where a slot attaches (A)"},
  ];

  /* ── the hot wheel: fixed angular slots ───────────────────────────────────
   * Ctrl+right-click on the canvas puts the rail under the cursor, so a stroke
   * never has to travel to the edge of the screen to change tool.
   *
   * The ENTIRE value of a radial menu is that after a week your hand knows
   * "up-left is the eraser" without reading anything. That only survives if the
   * angles are constants. So: twelve slots, 30° apart, and an entry keeps its
   * angle forever. Nothing here reorders by recency, frequency or context — an
   * entry that is unavailable right now is DIMMED IN PLACE, never removed, so
   * the gap never closes and the slot after it never moves.
   *
   * The order is not arbitrary either. The seven tools walk CLOCKWISE FROM
   * 12 O'CLOCK in exactly the order they sit top-to-bottom in the left rail, so
   * a hand that already knows the rail already knows the wheel. That fills the
   * right half (0°–180°); the five mid-stroke options fill the left half
   * (210°–330°). Tools right, options left.
   *
   * What is NOT here, deliberately: the sheet-grid detector, cell/frame sizing
   * and de-halo (setup you do once per sheet, not mid-stroke), redo (Ctrl+
   * Shift+Z, and the slot is worth more to `grid`), focus (set once; clicking
   * another cell retargets anyway), frame prev/next (ArrowLeft/Right are
   * already under the resting hand), and everything below it in the sidebar —
   * flips, nudges, animations, rig save, regeneration, preview, history. A
   * wheel that carries everything is a sidebar you have to aim at.
   *
   * Every entry is reachable without the wheel. It is a second path, never the
   * only one.
   */
  const WHEEL_SLOTS = 12;
  const WHEEL_STEP  = 360 / WHEEL_SLOTS;
  const WHEEL_R_MAX = 108, WHEEL_R_MIN = 68;
  const BRUSH_STEPS = [1, 2, 3, 4, 6, 8, 12, 16];
  const ONION_ORDER = ["off", "prev", "both", "all"];

  const WHEEL = [
    {a:  0, kind:"tool", id:"pencil", i:"brush",      lab:"pencil"},
    {a: 30, kind:"tool", id:"eraser", i:"eraser",     lab:"eraser"},
    {a: 60, kind:"tool", id:"bucket", i:"fill",       lab:"fill"},
    {a: 90, kind:"tool", id:"picker", i:"eyedropper", lab:"eyedropper"},
    {a:120, kind:"tool", id:"line",   i:"line",       lab:"line"},
    {a:150, kind:"tool", id:"rect",   i:"rect",       lab:"rectangle"},
    {a:180, kind:"tool", id:"anchor", i:"anchor",     lab:"rig anchor"},
    {a:210, kind:"act",  id:"undo",   i:"undo",       lab:"undo"},
    {a:240, kind:"act",  id:"brush",  i:"brush",      lab:"brush size"},
    {a:270, kind:"act",  id:"colour", i:"theme",      lab:"colour"},
    {a:300, kind:"act",  id:"grid",   i:"snap_grid",  lab:"grid"},
    {a:330, kind:"act",  id:"onion",  i:"onion",      lab:"onion"},
  ];

  const SLOT_COLOR = {
    main_hand:"var(--bad)", off_hand:"var(--text)",
    left_hand:"var(--warn)", right_hand:"var(--accent)",
    head:"var(--warn)", body:"var(--c-narrative)",
    feet:"var(--good)", throwable:"var(--c-narrative)", pivot:"var(--text-3)", muzzle:"var(--warn)",
    fx:"var(--text)",
  };
  const slotColor = s => SLOT_COLOR[s] || "var(--warn)";

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
      // Embedded in a Studio tab: a panel in the page, not a sheet over it.
      ".se-back.se-embed{position:relative;inset:auto;z-index:auto;background:var(--surface-2);backdrop-filter:none;height:100%;border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden}",
      ".se-back.se-embed .se-closebtn{display:none}",
      ".se-land{display:grid;place-items:center;height:100%;min-height:420px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-lg)}",
      ".se-land-in{text-align:center;max-width:380px;padding:var(--s-8)}",
      ".se-land-in h3{font-size:var(--fs-xl);font-weight:var(--fw-regular);color:var(--text);margin-bottom:var(--s-4)}",
      ".se-land-in p{color:var(--text-3);font-size:var(--fs-md);line-height:var(--lh);margin-bottom:var(--s-7)}",
      ".se-bar{display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--seam);background:var(--iron);flex:none}",
      ".se-title{font-family:var(--mono);font-size:11.5px;letter-spacing:.1em;color:var(--bone);text-transform:uppercase}",
      ".se-sub{font-family:var(--mono);font-size:10px;color:var(--ash2)}",
      ".se-dirty{color:var(--warn)}",
      ".se-spacer{flex:1}",
      ".se-btn{padding:6px 11px;background:var(--plate);border:1px solid var(--seam);border-radius:7px;color:var(--bone);font:inherit;font-size:11.5px;cursor:pointer}",
      ".se-btn:hover:not(:disabled){border-color:var(--ember)}",
      ".se-btn:disabled{opacity:.4;cursor:default}",
      ".se-btn.go{background:var(--ember);color:var(--bg);border-color:var(--ember);font-weight:var(--fw-semi)}",
      ".se-body{flex:1;display:flex;min-height:0}",
      ".se-tools{width:52px;flex:none;background:var(--iron);border-right:1px solid var(--seam);padding:8px 0;display:flex;flex-direction:column;align-items:center;gap:5px;overflow-y:auto}",
      ".se-tool{width:36px;height:36px;display:grid;place-items:center;background:var(--plate);border:1px solid var(--seam);border-radius:8px;color:var(--ash);font:inherit;font-size:16px;padding:0;cursor:pointer;flex:none}",
      // Icon + label buttons: the svg has to sit on the text's centre line, and
      // a baseline-aligned inline svg does not.
      ".se-btn{display:inline-flex;align-items:center;gap:6px}",
      ".se-btn .bgi{flex:none}",
      // The "unsaved"/"labelled" marks were the bullet character. Drawn, so
      // they are the same disc at every size on every machine.
      ".se-mark{width:7px;height:7px;border-radius:50%;background:currentColor;display:inline-block;vertical-align:middle;margin-right:5px}",
      ".se-tool:hover{border-color:var(--ember);color:var(--bone)}",
      ".se-tool.on{background:var(--ember);border-color:var(--ember);color:var(--bg)}",
      ".se-stage{flex:1;position:relative;min-width:0;overflow:hidden;background:var(--bg)}",
      ".se-stage canvas{position:absolute;inset:0;width:100%;height:100%;touch-action:none;cursor:crosshair}",
      ".se-hud{position:absolute;left:10px;bottom:10px;font-family:var(--mono);font-size:10px;color:var(--ash2);background:rgba(10,11,14,.8);border:1px solid var(--seam);border-radius:6px;padding:4px 8px;pointer-events:none;white-space:pre}",
      ".se-side{width:284px;flex:none;background:var(--iron);border-left:1px solid var(--seam);overflow-y:auto;padding:var(--s-4);display:flex;flex-direction:column;gap:var(--s-5)}",
      /* The sidebar's sections are .spanel + .sec-h out of app.css now (see
         sec() in renderSide), so nothing here restyles a header. This is only
         the column they sit in. flex:none matters: the frame strip and the
         history list have their own max-heights, and a flex column without it
         stretches the short panels to match instead of scrolling the sidebar.
         The last-child rule kills the trailing 7px every .se-row leaves, which
         otherwise read as five panels with uneven bottoms. */
      ".se-side > .spanel{flex:none;min-width:0}",
      ".se-side > .spanel > :last-child{margin-bottom:0}",
      /* The layer stack. Drawn top-down; the active row is the one every
         stroke lands on, so it is marked with the accent rather than a tint —
         "which layer am I on" must be answerable from the corner of the eye. */
      ".se-lyrs{display:flex;flex-direction:column;gap:2px;max-height:190px;overflow-y:auto}",
      ".se-lyr{display:flex;align-items:center;gap:7px;padding:4px 6px;border-radius:4px;cursor:pointer;border:1px solid transparent}",
      ".se-lyr:hover{background:var(--surface-3,rgba(255,255,255,.04))}",
      ".se-lyr.on{border-color:var(--accent);background:var(--accent-soft)}",
      ".se-lyr-eye{border:0;background:transparent;color:var(--text-3);cursor:pointer;padding:0;display:flex;line-height:1}",
      ".se-lyr-eye:hover{color:var(--text)}",
      ".se-lyr-n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:500 11px var(--mono)}",
      ".se-lyr-o{width:44px;flex:none;accent-color:var(--accent)}",
      ".se-lyr-b{flex:none;width:64px;font:500 9px var(--mono);background:var(--iron);color:var(--text-3);border:1px solid var(--seam);border-radius:3px;padding:1px 2px}",
      ".se-lyr-b:disabled{opacity:.35}",
      ".se-lyr-acts{gap:4px;margin-top:6px;flex-wrap:wrap}",
      // history list
      ".se-prevwrap{position:relative;margin-bottom:8px;border:1px solid var(--line);border-radius:var(--r-sm);overflow:hidden;line-height:0}",
      ".se-prevwrap canvas{width:100%;height:auto;display:block;image-rendering:pixelated}",
      ".se-prev-n{position:absolute;right:5px;bottom:4px;font-family:var(--mono);font-size:9px;color:#fff;background:rgba(8,9,12,.78);padding:1px 5px;border-radius:4px;line-height:1.5}",
      ".se-fps{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text-3)}",
      ".se-chk{gap:7px;cursor:pointer;font-size:11px;color:var(--text-3)}",
      ".se-kinds{display:flex;flex-direction:column;gap:3px;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--seam)}",
      ".se-kind{display:flex;align-items:center;gap:8px;padding:5px 8px;border:1px solid transparent;border-radius:4px;background:transparent;color:var(--text-3);cursor:pointer;font:500 10px var(--mono);text-transform:uppercase;letter-spacing:.06em}",
      ".se-kind .n{margin-left:auto;color:var(--text-dim)}",
      ".se-kind:hover{color:var(--text-2)}",
      ".se-kind.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}",
      ".se-chk input{accent-color:var(--accent)}",
      ".se-mode{display:flex;gap:3px}",
      ".se-mode button{flex:1;font:500 9px var(--mono);text-transform:uppercase;letter-spacing:.06em;padding:3px 0;border:1px solid var(--seam);background:transparent;color:var(--text-3);border-radius:3px;cursor:pointer}",
      ".se-mode button.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}",
      ".se-onion{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text-3)}",
      ".se-onion select{flex:1;min-width:0}",
      ".se-btn.on{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}",
      ".se-hist{display:flex;flex-direction:column;gap:1px;max-height:230px;overflow-y:auto;margin-bottom:8px}",
      ".se-hrow{display:flex;align-items:center;gap:8px;width:100%;text-align:left;padding:4px 7px;background:none;border:0;border-radius:var(--r-xs);color:var(--text-2);font:inherit;font-size:11px;cursor:pointer}",
      "button.se-hrow:hover{background:var(--surface-3);color:var(--text)}",
      ".se-hrow .se-hdot{width:6px;height:6px;border-radius:50%;background:var(--line-strong);flex:none}",
      ".se-hrow .se-hl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".se-hrow .se-ht{flex:none;font-family:var(--mono);font-size:9px;color:var(--text-3);font-variant-numeric:tabular-nums}",
      // the state you are looking at
      ".se-hrow.now{color:var(--text);cursor:default}",
      ".se-hrow.now .se-hdot{background:var(--accent);box-shadow:0 0 6px var(--accent)}",
      // steps ahead of the cursor, reachable with redo
      ".se-hrow.undone{opacity:.5}",
      ".se-hrow.undone .se-hdot{background:transparent;border:1px solid var(--line-strong)}",
      ".se-hrow.base{color:var(--text-3);cursor:default}",
      ".se-row{display:flex;align-items:center;gap:7px;margin-bottom:7px}",
      ".se-row label{font-family:var(--mono);font-size:10px;color:var(--ash2);flex:none}",
      ".se-in{flex:1;min-width:0;background:var(--void);border:1px solid var(--seam);border-radius:6px;color:var(--bone);font:inherit;font-size:11.5px;padding:5px 7px}",
      ".se-in:focus{outline:none;border-color:var(--ember)}",
      ".se-in.num{flex:none;width:62px}",
      ".se-sw{width:34px;height:30px;padding:2px;border:1px solid var(--seam);border-radius:6px;background:var(--void);cursor:pointer;flex:none}",
      ".se-pal{display:flex;flex-wrap:wrap;gap:4px}",
      ".se-pc{width:20px;height:20px;border-radius:4px;border:1px solid var(--seam);cursor:pointer;position:relative;background-image:linear-gradient(45deg,var(--surface-2) 25%,transparent 25%,transparent 75%,var(--surface-2) 75%),linear-gradient(45deg,var(--surface-2) 25%,var(--bg) 25%,var(--bg) 75%,var(--surface-2) 75%);background-size:8px 8px;background-position:0 0,4px 4px}",
      ".se-pc span{position:absolute;inset:0;border-radius:3px}",
      ".se-pc.on{border-color:var(--ember);box-shadow:0 0 0 1px var(--ember)}",
      ".se-strip{display:flex;flex-wrap:wrap;gap:4px}",
      ".se-fr{position:relative;width:46px;height:46px;border:1px solid var(--seam);border-radius:6px;background:var(--bg);cursor:pointer;overflow:hidden;flex:none}",
      ".se-fr canvas{width:100%;height:100%;image-rendering:pixelated;display:block}",
      ".se-fr.on{border-color:var(--ember);box-shadow:0 0 0 1px var(--ember)}",
      ".se-fr .n{position:absolute;left:2px;top:1px;font-family:var(--mono);font-size:8px;color:var(--ash2);text-shadow:0 0 3px var(--bg)}",
      ".se-fr .dots{position:absolute;right:2px;bottom:2px;display:flex;gap:2px}",
      ".se-fr .dots i{width:5px;height:5px;border-radius:50%;display:block}",
      ".se-fr.picked{border-color:var(--text);box-shadow:0 0 0 1px var(--text)}",
      ".se-fr.picked::after{content:'';position:absolute;right:3px;top:3px;width:6px;height:6px;border-radius:50%;background:var(--text)}",
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
      ".se-res.bad{border-color:var(--bad-line)}",
      ".se-res .hd{display:flex;font-family:var(--mono);font-size:10px;color:var(--bone);margin-bottom:6px}",
      ".se-res .hd .m{margin-left:auto;color:var(--ash2)}",
      ".se-res .pair{display:grid;grid-template-columns:1fr 1fr;gap:6px}",
      ".se-res figure{margin:0}",
      ".se-res img{width:100%;background:var(--bg);border-radius:5px;image-rendering:pixelated;display:block}",
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
      // overflow-wrap: the sidecar path in the save section is one unbroken
      // token. In an unbordered column it merely widened the scroll area and
      // nobody looked; inside a .spanel it hangs 63px out over the panel's own
      // right edge, which reads as a broken box.
      ".se-note{font-size:11px;color:var(--ash);line-height:1.5;overflow-wrap:anywhere}",
      ".se-note b{color:var(--bone)}",
      ".se-warn{color:var(--warn)}",
      ".se-pick{position:fixed;inset:0;z-index:1401;background:rgba(4,5,7,.9);display:flex;align-items:center;justify-content:center;padding:40px}",
      ".se-pick-box{background:var(--iron);border:1px solid var(--seam);border-radius:12px;width:min(920px,100%);height:min(660px,90vh);display:flex;flex-direction:column;overflow:hidden}",
      ".se-pick-body{display:flex;min-height:0;flex:1}",
      ".se-pick-cats{width:150px;flex:none;border-right:1px solid var(--line);padding:8px;overflow-y:auto;display:flex;flex-direction:column;gap:2px}",
      ".se-cat{display:flex;align-items:center;gap:8px;width:100%;text-align:left;padding:6px 9px;background:none;border:0;border-radius:var(--r-sm);color:var(--text-2);font:inherit;font-size:12px;cursor:pointer;text-transform:capitalize}",
      ".se-cat:hover{background:var(--surface-3);color:var(--text)}",
      ".se-cat.on{background:var(--accent-soft);color:var(--text)}",
      ".se-cat .n{margin-left:auto;font-family:var(--mono);font-size:10px;color:var(--text-3);font-variant-numeric:tabular-nums}",
      ".se-cat.on .n{color:var(--accent)}",
      ".se-pick-list{overflow-y:auto;padding:8px;flex:1;min-width:0}",
      ".se-pick-i{display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:7px;cursor:pointer;font-family:var(--mono);font-size:11px;color:var(--bone)}",
      ".se-pick-i:hover{background:var(--plate)}",
      ".se-pick-i img{width:34px;height:34px;object-fit:contain;image-rendering:pixelated;background:var(--bg);border-radius:5px;border:1px solid var(--seam);flex:none}",
      ".se-pick-i .m{margin-left:auto;color:var(--ash2);font-size:10px}",
      ".se-tag{font-size:9px;padding:1px 5px;border-radius:999px;border:1px solid var(--seam);color:var(--ash2)}",
      ".se-tag.on{border-color:var(--good);color:var(--good)}",
      ".se-new-box{background:var(--iron);border:1px solid var(--seam);border-radius:12px;width:min(620px,100%);overflow:hidden;box-shadow:0 22px 70px rgba(0,0,0,.55)}",
      ".se-new-body{padding:22px;display:grid;gap:18px}",
      ".se-new-tabs{display:flex;gap:4px;padding:4px;background:var(--void);border:1px solid var(--seam);border-radius:8px}",
      ".se-new-tabs button{flex:1;border:0;border-radius:5px;padding:8px;background:transparent;color:var(--ash2);font:500 11px var(--mono);cursor:pointer}",
      ".se-new-tabs button.on{background:var(--plate2);color:var(--bone)}",
      ".se-new-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}",
      ".se-new-field{display:grid;gap:5px}",
      ".se-new-field.wide{grid-column:1/-1}",
      ".se-new-field label{font:500 10px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--ash2)}",
      ".se-new-total{padding:12px 14px;border:1px solid var(--seam);border-radius:8px;background:var(--void);font:11px var(--mono);color:var(--ash2)}",
      ".se-new-total b{color:var(--bone)}",
      ".se-drop{min-height:126px;border:1px dashed var(--line-strong);border-radius:9px;display:grid;place-items:center;text-align:center;padding:18px;color:var(--ash2);cursor:pointer;background:var(--void)}",
      ".se-drop:hover{border-color:var(--ember);color:var(--bone)}",
      ".se-drop b{display:block;color:var(--bone);margin-bottom:5px}",
      ".se-new-actions{display:flex;justify-content:flex-end;gap:8px}",

      /* the hot wheel. `inset:0` on .se-back, so in the Studio tab it is
         clipped to the embedded host and can never escape onto the page. */
      ".se-wheelwrap{position:absolute;inset:0;z-index:30;touch-action:none}",
      // Everything inside is paint. All hit-testing is done on geometry against
      // the stored centre, so aiming works on the whole 30° sector rather than
      // only on the 46px disc, and the buttons never steal the pointer stream.
      ".se-wheel{position:absolute;pointer-events:none;user-select:none;animation:se-wpop .11s ease-out}",
      "@keyframes se-wpop{from{opacity:.25;transform:scale(.9)}to{opacity:1;transform:scale(1)}}",
      ".se-wring{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);border-radius:50%;" +
        "border:1px solid var(--line);box-shadow:0 0 0 1px var(--accent-wash),0 12px 44px rgba(0,0,0,.6)}",
      // Opaque: it carries text over pixel art. --solid-1, never --surface-N.
      ".se-whub{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);border-radius:50%;" +
        "background:var(--solid-1);border:1px solid var(--line-strong);display:grid;place-items:center}",
      ".se-wlab{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);white-space:nowrap;" +
        "max-width:184px;overflow:hidden;text-overflow:ellipsis;background:var(--solid-1);" +
        "border:1px solid var(--line);border-radius:var(--r-full);padding:3px 10px;" +
        "font-family:var(--mono);font-size:10px;color:var(--text-2);text-align:center}",
      ".se-wi{position:absolute;transform:translate(-50%,-50%);width:var(--wb);height:var(--wb);" +
        "border-radius:50%;background:var(--solid-2);border:1px solid var(--line-strong);" +
        "color:var(--text-2);display:grid;place-items:center;padding:0;font:inherit}",
      ".se-wi.on{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}",
      ".se-wi.hot{border-color:var(--accent);color:var(--text);z-index:2;" +
        "box-shadow:0 0 0 2px var(--accent),0 0 22px var(--accent-soft);" +
        "transform:translate(-50%,-50%) scale(1.12)}",
      ".se-wi.on.hot{color:var(--accent-fg)}",
      // Unavailable keeps its angle and dims. Closing the gap would move every
      // slot after it and cost the muscle memory the wheel exists for.
      ".se-wi.off{opacity:.34}",
      ".se-wi.off.hot{border-color:var(--line-strong);box-shadow:0 0 0 2px var(--line-strong)}",
      ".se-wnum{font-family:var(--mono);font-size:14px;color:inherit;font-variant-numeric:tabular-nums}",
      ".se-wsw{width:58%;height:58%;border-radius:50%;border:1px solid var(--line-strong);display:block}",
    ].join("\n");
    document.head.appendChild(s);
  }

  /* ── open / close ─────────────────────────────────────────────────────── */
  async function open(rel){
    injectStyle();
    if (S && (S.dirty || S.rigDirty) && !(await askConfirm({
      title: "Discard unsaved pixel edits?",
      body: "The pixels you painted and the undo history for this sheet go with it.",
      ok: "discard", danger: true,
    }))) return;
    if (S) await close(true);
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
      /* LAYERS.
       *
       * `work` stays the COMPOSITE — the save path, the export, the preview and
       * the eyedropper all read it, and keeping it authoritative means none of
       * them had to learn what a layer is. Painting goes to the ACTIVE layer
       * and recomposite() redraws `work` from the stack.
       *
       * THEY DO NOT SURVIVE THE FILE, AND THE PANEL SAYS SO. A PNG has one
       * image in it; Aseprite persists layers because .ase is its own format
       * and this editor writes the .png the engine actually loads. So layers
       * are a working surface for the session — separate the outline from the
       * fill, fix one without disturbing the other — and Save flattens. That is
       * a real limit and it is stated in the panel rather than discovered when
       * a stack silently collapses.
       */
      layers: [], li: 0,
      w: info.width, h: info.height,
      mtime: info.mtime,
      rig: info.rig && info.rig.grid ? info.rig : Object.assign({}, info.rig, {
        grid: info.suggested_grid || null,
      }),
      // Resolved, not a var() string: this is a canvas fillStyle, a
      // hexToRGBA() argument and an <input type="color"> value, and none of
      // the three can read a custom property.
      tool: "pencil", color: BGTheme.color("--text"), brush: 1, zoom: 1, pan: {x:0, y:0},
      frame: 0, slot: "left_hand",
      onion: "off", showGrid: true, focusFrame: true, pixelPerfect: true,
      preview: { on: false, fps: 8, i: 0, timer: null, row: "sheet", mode: "loop" },
      undo: [], redo: [], undoBytes: 0,
      dirty: false, rigDirty: false,
      drag: null, hover: null, clip: null, wheel: null,
      picked: new Set(),        // frames selected for a batch regeneration
      regen: { prompt: "", quality: "medium", busy: false, results: [],
               status: null },
    };
    /* One layer, holding what was on disk. Every later layer is empty and sits
       above it, so an unedited file composites to exactly its own pixels. */
    S.layers = [newLayer("base", work)];
    S.li = 0;
    mount();
    fit();
    paint();
    regenStatus();      // async: fills in the price table and the off-switch
  }

  // async because the unsaved-edit question is a real element now. Every caller
  // is a click or a keystroke that discards the result anyway; open() awaits it
  // so a discarded sheet is torn down before the next one is built.
  async function close(silent){
    if (S && S.dirty && !silent && !(await askConfirm({
      title: "You have unsaved pixel edits. Close anyway?",
      body: "The undo history closes with the sheet - reopening it reads the file on disk.",
      ok: "close anyway", danger: true,
    }))) return;
    if (S){
      if (S.ro) try { S.ro.disconnect(); } catch (e) {}
      if (S.onResize) window.removeEventListener("resize", S.onResize);
    }
    previewStop();
    wheelClose();          // its listeners live on window, not on the torn-down DOM
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
          <button class="se-btn" onclick="SpriteEdit.newDialog('blank')">new sheet</button>
          <button class="se-btn" onclick="SpriteEdit.newDialog('import')">import</button>
          <button class="se-btn" onclick="SpriteEdit.closePick()">close</button></div>
        <div class="se-pick-body">
          <div class="se-pick-cats" id="se-pick-cats"></div>
          <div class="se-pick-list" id="se-pick-list">
            <div class="se-note" style="padding:24px;text-align:center">scanning…</div></div>
        </div>
      </div>`;
      document.body.appendChild(host);
      host.addEventListener("click", ev => { if (ev.target === host) closePick(); });
      const input = host.querySelector("#se-pick-q");
      if (input) input.focus();
    }
    // limit=2000 (the route's ceiling) instead of the default 300. At the
    // default this project returned 300 of 584 and the category counts would
    // have been confidently wrong.
    const d = await readJSON(
      `/api/sprite/list?limit=2000${q ? `&q=${encodeURIComponent(q)}` : ""}`, {sheets:[]});
    // The {sheets:[]} fallback is what readJSON hands back on a REFUSAL as well
    // as on a scan that legitimately found nothing, which is why it also tags
    // the result — and this read the tag off the floor. A 500 out of the walker,
    // or the dashboard going away mid-search, therefore painted "no .png or
    // .webp sheet matches" over a project with 584 of them, and the operator's
    // next move is to go looking for their missing art.
    _pickError = d.__error || null;
    _pickAll = d.sheets || [];
    _pickKinds = d.kinds || {};
    /* If a project has no art at all — every sheet is a review artefact — then
       defaulting to art would show an empty picker over a full directory. */
    if (_pickKind === "art" && !( _pickKinds.art > 0)) _pickKind = "";
    _pickTruncated = !!d.truncated;
    _pickTotal = d.total || _pickAll.length;
    renderPick();
  }

  /* One flat list of 584 sheets with a text box was the whole navigation. The
     project already sorts itself — characters, enemies, props, tiles, portraits,
     tmp — by the path segment under assets/, so the picker groups on that. */
  let _pickAll = [], _pickCat = null, _pickTruncated = false, _pickTotal = 0;
  /* WHAT THE FILE IS FOR, not where it sits. `art` is the deliverable; `review`
     is the contact sheets, before/afters and chroma checks an agent produced in
     order to look at its own work; `test` is fixtures. Defaults to art because
     that is what someone opening a pixel editor came for — and in a real
     project the other two outnumber it. */
  let _pickKind = "art";
  let _pickKinds = {};
  let _pickError = null;   // the refusal behind an empty list, when there was one

  function categoryOf(rel){
    const parts = String(rel).replace(/\\/g, "/").split("/");
    const i = parts.indexOf("assets");
    if (i >= 0 && i + 1 < parts.length - 1) return parts[i + 1];
    if (i >= 0) return "loose";
    return parts.length > 1 ? parts[0] : "root";
  }

  function renderPick(){
    const cats = new Map();
    const kindOf = s => s.kind || "art";
    const inKind = s => !_pickKind || kindOf(s) === _pickKind;
    _pickAll.filter(inKind).forEach(s => {
      const c = categoryOf(s.rel);
      cats.set(c, (cats.get(c) || 0) + 1);
    });
    // Only keep a category selected while it still has matches under the filter.
    if (_pickCat && !cats.has(_pickCat)) _pickCat = null;

    const order = [...cats.entries()].sort((a, b) => b[1] - a[1]);
    const catHost = document.getElementById("se-pick-cats");
    if (catHost) {
      /* THE KIND SWITCH SITS ABOVE THE FOLDERS because it is the coarser cut:
         "is this the art or is this a screenshot of the art" comes before
         "which character". Counts are the whole project's, not the page's. */
      const kindBtn = (id, label, n) =>
        `<button class="se-kind${_pickKind === id ? " on" : ""}" data-kind="${id}">` +
        `<span>${label}</span><span class="n">${n}</span></button>`;
      catHost.innerHTML =
        `<div class="se-kinds">` +
          kindBtn("art", "art", _pickKinds.art || 0) +
          (_pickKinds.review ? kindBtn("review", "review shots", _pickKinds.review) : "") +
          (_pickKinds.test ? kindBtn("test", "test files", _pickKinds.test) : "") +
          kindBtn("", "everything", _pickTotal || _pickAll.length) +
        `</div>` +
        `<button class="se-cat${_pickCat === null ? " on" : ""}" data-cat="">` +
          `<span>all</span><span class="n">${_pickAll.length}</span></button>` +
        order.map(([c, n]) =>
          `<button class="se-cat${_pickCat === c ? " on" : ""}" data-cat="${E(c)}">` +
            `<span>${E(c)}</span><span class="n">${n}</span></button>`).join("");
      catHost.querySelectorAll(".se-kind").forEach(b => b.onclick = () => {
        _pickKind = b.dataset.kind;
        _pickCat = null;            // a folder from the old kind may not exist here
        renderPick();
      });
      catHost.querySelectorAll(".se-cat").forEach(b => b.onclick = () => {
        _pickCat = b.dataset.cat || null;
        renderPick();
      });
    }

    const kept = _pickAll.filter(inKind);
    const shown = _pickCat ? kept.filter(s => categoryOf(s.rel) === _pickCat) : kept;
    const n = document.getElementById("se-pick-n");
    if (n) n.textContent = _pickError
      ? "could not read the project"
      : _pickTruncated
      ? `${_pickAll.length} of ${_pickTotal} - narrow the filter`
      : `${shown.length} editable image${shown.length === 1 ? "" : "s"}`;

    const list = document.getElementById("se-pick-list");
    if (list && _pickError){
      list.innerHTML = `<div class="se-note se-warn" style="padding:24px;text-align:center">`
        + `the sheet list could not be read - ${E(_pickError)}</div>`;
      return;
    }
    if (list) list.innerHTML = shown.length ? shown.map(s => `
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

  /* ── create / import ─────────────────────────────────────────────────── */
  let _newMode = "blank", _importFile = null;

  function newDialog(mode){
    injectStyle(); closePick(); closeNew();
    _newMode = mode === "import" ? "import" : "blank";
    _importFile = null;
    const host = document.createElement("div");
    host.className = "se-pick"; host.id = "se-new";
    host.innerHTML = `<div class="se-new-box">
      <div class="se-bar"><span class="se-title">new sprite sheet</span>
        <span class="se-spacer"></span>
        <button class="se-btn" onclick="SpriteEdit.closeNew()">close</button></div>
      <div class="se-new-body">
        <div class="se-new-tabs">
          <button id="se-new-blank" onclick="SpriteEdit.newMode('blank')">blank sheet</button>
          <button id="se-new-import" onclick="SpriteEdit.newMode('import')">import image</button>
        </div>
        <div id="se-new-content"></div>
      </div></div>`;
    document.body.appendChild(host);
    host.addEventListener("click", ev => { if (ev.target === host) closeNew(); });
    newMode(_newMode);
  }

  function closeNew(){ const el = document.getElementById("se-new"); if (el) el.remove(); }

  function newMode(mode){
    _newMode = mode === "import" ? "import" : "blank";
    ["blank", "import"].forEach(k => {
      const b = document.getElementById(`se-new-${k}`);
      if (b) b.classList.toggle("on", k === _newMode);
    });
    const body = document.getElementById("se-new-content");
    if (!body) return;
    if (_newMode === "blank") body.innerHTML = `
      <div class="se-new-grid">
        <div class="se-new-field wide"><label>project path</label>
          <input class="se-in" id="se-new-path" value="game/assets/sprites/untitled.png"></div>
        <div class="se-new-field"><label>frame width</label>
          <input class="se-in" id="se-new-cw" type="number" min="1" max="2048" value="32" oninput="SpriteEdit.newSummary()"></div>
        <div class="se-new-field"><label>frame height</label>
          <input class="se-in" id="se-new-ch" type="number" min="1" max="2048" value="32" oninput="SpriteEdit.newSummary()"></div>
        <div class="se-new-field"><label>columns</label>
          <input class="se-in" id="se-new-cols" type="number" min="1" max="64" value="4" oninput="SpriteEdit.newSummary()"></div>
        <div class="se-new-field"><label>rows</label>
          <input class="se-in" id="se-new-rows" type="number" min="1" max="64" value="4" oninput="SpriteEdit.newSummary()"></div>
      </div>
      <div class="se-new-total" id="se-new-total"></div>
      <div class="se-new-actions"><button class="se-btn" onclick="SpriteEdit.closeNew()">cancel</button>
        <button class="se-btn go" id="se-new-go" onclick="SpriteEdit.createSheet()">create and open</button></div>`;
    else body.innerHTML = `
      <div class="se-new-grid">
        <div class="se-new-field wide"><label>source image</label>
          <label class="se-drop" for="se-new-file" id="se-new-file-label"><span><b>Choose an image</b>PNG, WebP, or JPEG · up to 18 MB</span></label>
          <input id="se-new-file" type="file" accept="image/png,image/webp,image/jpeg" hidden onchange="SpriteEdit.importPicked(event)"></div>
        <div class="se-new-field wide"><label>project path</label>
          <input class="se-in" id="se-new-path" value="game/assets/sprites/imported.png"></div>
      </div>
      <div class="se-new-total">The source is converted to an editable RGBA PNG. The original file is not changed.</div>
      <div class="se-new-actions"><button class="se-btn" onclick="SpriteEdit.closeNew()">cancel</button>
        <button class="se-btn go" id="se-new-go" onclick="SpriteEdit.importSheet()" disabled>import and open</button></div>`;
    newSummary();
  }

  function newSummary(){
    if (_newMode !== "blank") return;
    const n = id => Math.max(0, parseInt((document.getElementById(id)||{}).value, 10) || 0);
    const cw=n("se-new-cw"), ch=n("se-new-ch"), cols=n("se-new-cols"), rows=n("se-new-rows");
    const el=document.getElementById("se-new-total");
    if (el) el.innerHTML = `<b>${cw*cols} × ${ch*rows}px</b> canvas · <b>${cols*rows}</b> frames · transparent background`;
  }

  function importPicked(ev){
    const input = ev && ev.target;
    _importFile = input && input.files && input.files[0] || null;
    if (!_importFile) return;
    const stem = String(_importFile.name || "imported").replace(/\.[^.]+$/, "")
      .toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^[_-]+|[_-]+$/g, "").slice(0, 64) || "imported";
    const path = document.getElementById("se-new-path");
    if (path) path.value = `game/assets/sprites/${stem}.png`;
    const label = document.getElementById("se-new-file-label");
    if (label) label.innerHTML = `<span><b>${E(_importFile.name)}</b>${Math.ceil(_importFile.size/1024)} KB · ready to import</span>`;
    const go = document.getElementById("se-new-go"); if (go) go.disabled = false;
  }

  async function mayReplaceEditor(){
    return !(S && (S.dirty || S.rigDirty)) || await askConfirm({
      title:"Discard unsaved pixel edits?",
      body:"The pixels you painted and the undo history for this sheet go with it.",
      ok:"discard", danger:true,
    });
  }

  async function createSheet(){
    if (!(await mayReplaceEditor())) return;
    const val = id => (document.getElementById(id)||{}).value;
    const body = {rel:val("se-new-path"), cell_w:val("se-new-cw"), cell_h:val("se-new-ch"),
                  cols:val("se-new-cols"), rows:val("se-new-rows")};
    const r = await mutate("/api/sprite/create", {body, button:"se-new-go", quiet:true});
    if (!r.ok){ say(r.error || "could not create that sheet"); return; }
    closeNew(); if (S) await close(true); await open(r.data.rel);
    say(`created ${r.data.rel}`, "ok");
  }

  function fileDataURL(file){
    return new Promise((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error); reader.readAsDataURL(file);
    });
  }

  async function importSheet(){
    if (!_importFile){ say("choose an image to import"); return; }
    if (_importFile.size > 18 * 1024 * 1024){ say("that image is larger than the 18 MB import limit"); return; }
    if (!(await mayReplaceEditor())) return;
    let image;
    try { image = await fileDataURL(_importFile); }
    catch (e) { say("the browser could not read that image"); return; }
    const path = (document.getElementById("se-new-path")||{}).value;
    const r = await mutate("/api/sprite/import", {body:{rel:path, image}, button:"se-new-go", quiet:true});
    if (!r.ok){ say(r.error || "could not import that image"); return; }
    closeNew(); if (S) await close(true); await open(r.data.rel);
    say(`imported ${r.data.rel}`, "ok");
  }

  /* ── DOM ──────────────────────────────────────────────────────────────── */
  function mount(){
    const back = document.createElement("div");
    back.className = "se-back";
    back.id = "se-back";
    back.innerHTML = `
      <div class="se-bar">
        <span class="se-title">sprite editor</span>
        <span class="se-sub" id="se-name"></span>
        <span id="se-save-state"></span>
        <span class="se-spacer"></span>
        <button class="se-btn" id="se-undo" onclick="SpriteEdit.undo()"
                title="Undo (Ctrl+Z)" aria-label="Undo">${I("undo")}</button>
        <button class="se-btn" id="se-redo" onclick="SpriteEdit.redo()"
                title="Redo (Ctrl+Shift+Z)" aria-label="Redo">${I("redo")}</button>
        <button class="se-btn" onclick="SpriteEdit.fit()" title="Fit to view (0)">fit</button>
        <button class="se-btn" onclick="SpriteEdit.newDialog('blank')">new</button>
        <button class="se-btn" onclick="SpriteEdit.newDialog('import')">import</button>
        <button class="se-btn" onclick="SpriteEdit.pick()">open…</button>
        <button class="se-btn go" id="se-save" onclick="SpriteEdit.save()">save sheet</button>
        <button class="se-btn" id="se-handoff" onclick="SpriteEdit.handoff()"
                title="Wire this sheet into a scene, or hand it to an agent with the details filled in">put in game</button>
        <button class="se-btn se-closebtn" onclick="SpriteEdit.close()">close</button>
      </div>
      <div class="se-body">
        <div class="se-tools" id="se-tools"></div>
        <div class="se-stage" id="se-stage"><canvas id="se-view"></canvas>
          <div class="se-hud" id="se-hud"></div></div>
        <div class="se-side" id="se-side"></div>
      </div>`;
    // Embed ONLY into a host that is actually on screen. `_host` is set once by
    // the Studio's sprite tab and never cleared, so every later `open()` from
    // anywhere else — the scene builder's "edit pixels", the Atlas graph, the
    // asset library — mounted the whole editor inside the Studio's hidden
    // container. The editor loaded, painted, and bound its keys into a div with
    // no box, so the button looked like it did nothing at all.
    const embedHost = visible(_host) ? _host : null;
    if (embedHost) {
      back.classList.add("se-embed");
      embedHost.innerHTML = "";
      embedHost.appendChild(back);
    } else document.body.appendChild(back);
    $ = {
      back, name: back.querySelector("#se-name"), tools: back.querySelector("#se-tools"),
      stage: back.querySelector("#se-stage"), view: back.querySelector("#se-view"),
      hud: back.querySelector("#se-hud"), side: back.querySelector("#se-side"),
      save: back.querySelector("#se-save"),
    };
    $.ctx = $.view.getContext("2d");
    $.tools.innerHTML = TOOLS.map(t =>
      `<button class="se-tool" type="button" data-tool="${t.id}" title="${E(t.t)}"
               aria-label="${E(t.t)}"
               onclick="SpriteEdit.setTool('${t.id}')">${I(t.i, 18)}</button>`).join("");
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

  // A ground change invalidates every colour already painted into the canvas.
  // BGTheme.flush() has already run by the time this fires (same event, earlier
  // listener), so paint() picks up the new values.
  try{ window.addEventListener("bgate:theme", () => { try{ paint(); }catch(e){} }); }catch(e){}

  function paint(){
    // Cheap (SaveState dedupes on a signature) and it means every path that
    // dirties the rig - a dozen of them, none of which call refreshHistory -
    // still moves the indicator.
    paintSaveState();
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
    c.fillStyle = BGTheme.color("--checker-b"); c.fillRect(px, py, S.w*z, S.h*z);
    c.fillStyle = BGTheme.color("--checker-a");
    for (let y = 0; y < S.h*z; y += chk)
      for (let x = ((y/chk)|0)%2 ? chk : 0; x < S.w*z; x += chk*2)
        c.fillRect(px+x, py+y, chk, chk);
    c.restore();

    /* Onion skin. This used to be one boolean that ghosted only the PREVIOUS
       frame, which tells you where a limb came from but never where it is
       going — the two-sided version is what actually catches a walk cycle that
       does not translate evenly.

         prev  · the frame behind, warm
         both  · behind warm, ahead cool — the classic animation read
         all   · every other frame, faint, for checking overall drift

       Ghosts are tinted and drawn INTO the current frame's box so they stack
       registered on top of each other rather than sitting in their own cells. */
    const nFrames = frameCount();
    const mode = S.onion;
    if (mode && mode !== "off" && S.focusFrame && nFrames > 1){
      const cur = frameBox(S.frame);
      const ghosts = [];
      if (mode === "prev" || mode === "both"){
        if (S.frame > 0) ghosts.push({ i: S.frame - 1, a: .34, tint: "var(--bad)" });
      }
      if (mode === "both"){
        if (S.frame < nFrames - 1) ghosts.push({ i: S.frame + 1, a: .34, tint: "var(--c-tech)" });
      }
      if (mode === "all"){
        for (let i = 0; i < nFrames; i++){
          if (i === S.frame) continue;
          ghosts.push({ i: i, a: .16, tint: i < S.frame ? "var(--bad)" : "var(--c-tech)" });
        }
      }
      ghosts.forEach(g => {
        const b = frameBox(g.i);
        c.globalAlpha = g.a;
        c.drawImage(S.work, b.x, b.y, b.w, b.h, px+cur.x*z, py+cur.y*z, cur.w*z, cur.h*z);
        c.globalAlpha = 1;
      });
    }

    c.drawImage(S.work, px, py, S.w*z, S.h*z);

    /* With ghosts on the canvas the live frame stops being obvious, so say
       which cell you are actually painting into: dim every other cell and ring
       the current one. Only while onion is on — it is noise otherwise. */
    if (mode && mode !== "off" && S.focusFrame && nFrames > 1){
      const cur = frameBox(S.frame);
      c.save();
      c.beginPath();
      c.rect(px, py, S.w*z, S.h*z);
      c.rect(px+cur.x*z, py+cur.y*z, cur.w*z, cur.h*z);
      c.fillStyle = "rgba(0,0,0,.45)";
      c.fill("evenodd");                     // everything except the live cell
      c.restore();
      c.strokeStyle = BGTheme.color("--accent");
      c.lineWidth = 2;
      c.strokeRect(px+cur.x*z - 1, py+cur.y*z - 1, cur.w*z + 2, cur.h*z + 2);
    }

    // In-progress line/rect preview lives on the view, never on the sheet, so
    // an abandoned drag leaves no trace.
    if (S.drag && S.drag.preview) drawPreview(c, S.drag);

    const g = grid();
    if (S.showGrid && g.cols*g.rows > 1){
      c.strokeStyle = BGTheme.color("--text-3"); c.globalAlpha = .45; c.lineWidth = 1;
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
      c.globalAlpha = 1;
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
      c.strokeStyle = BGTheme.color("--accent"); c.lineWidth = 2;
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
  /* The stack used to hold bare ImageData, so undo worked but there was nothing
     to SHOW: no way to see what you had done, or to jump back more than one
     step at a time. Each entry now carries a label and a timestamp. Entries are
     {data, label, t} — the byte accounting reads e.data.data.length, one more
     hop than before. */

  /* The canvas operators that mean something for pixel art, under the names
     every other editor uses for them. Deliberately NOT the whole list: `xor`,
     `destination-atop` and friends are compositing plumbing, not artist tools,
     and a menu of thirty entries where four are useful is a menu nobody reads. */
  const BLEND_MODES = [
    ["source-over", "normal"], ["multiply", "multiply"], ["screen", "screen"],
    ["overlay", "overlay"], ["darken", "darken"], ["lighten", "lighten"],
    ["color-dodge", "dodge"], ["color-burn", "burn"], ["hard-light", "hard light"],
    ["soft-light", "soft light"], ["difference", "difference"],
    ["hue", "hue"], ["saturation", "saturation"], ["color", "color"],
    ["luminosity", "luminosity"],
  ];

  /* ── layers ────────────────────────────────────────────────────────────────
     A layer is a full-size canvas. Same dimensions as the sheet, so compositing
     is a drawImage at 0,0 with no arithmetic and a layer can be reordered
     without touching its pixels.

     `from` seeds it — used once, for the image that came off disk. */
  function newLayer(name, from){
    const c = document.createElement("canvas");
    c.width = S.w; c.height = S.h;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.imageSmoothingEnabled = false;
    if (from) ctx.drawImage(from, 0, 0);
    return { id: "L" + Math.random().toString(36).slice(2, 8),
             name: name || "layer", visible: true, opacity: 1,
             blend: "source-over", canvas: c, ctx };
  }

  /** The context every paint operation writes to. Falls back to the composite
   *  if a layer stack somehow does not exist, so a paint can never throw into
   *  a dashboard that has no layers yet. */
  function lctx(){
    const L = S && S.layers && S.layers[S.li];
    return L ? L.ctx : (S ? S.wctx : null);
  }

  /** Redraw the composite from the stack. Bottom-first, honouring visibility
   *  and opacity — the two controls the panel offers. */
  function recomposite(){
    if (!S || !S.layers || !S.layers.length) return;
    S.wctx.clearRect(0, 0, S.w, S.h);
    for (const L of S.layers){
      if (!L.visible) continue;
      S.wctx.globalAlpha = L.opacity;
      /* THE BOTTOM LAYER IS ALWAYS source-over. A blend mode composites against
         what is already on the canvas, and under the bottom layer there is
         nothing — "multiply" against transparent black is a black sheet, which
         looks like the editor destroyed the art. */
      S.wctx.globalCompositeOperation =
        (L === S.layers[0] ? "source-over" : (L.blend || "source-over"));
      S.wctx.drawImage(L.canvas, 0, 0);
    }
    S.wctx.globalAlpha = 1;
    S.wctx.globalCompositeOperation = "source-over";
  }

  function layerAdd(){
    if (!S) return;
    S.layers.splice(S.li + 1, 0, newLayer("layer " + (S.layers.length + 1)));
    S.li += 1;
    S.dirty = true; recomposite(); layersPanel(); paint(); thumbs();
  }

  /* THE LAST LAYER CANNOT GO. A sheet with no layer has nowhere to paint and
     the next stroke would land on the composite and be erased by the next
     recomposite — a stroke that vanishes with no error is the worst version of
     this bug. */
  function layerDel(){
    if (!S || S.layers.length < 2) { say("a sheet needs at least one layer", true); return; }
    snapshotAll("delete layer");
    S.layers.splice(S.li, 1);
    S.li = Math.max(0, S.li - 1);
    S.dirty = true; recomposite(); layersPanel(); paint(); thumbs();
  }

  function layerMove(dir){
    if (!S) return;
    const j = S.li + dir;
    if (j < 0 || j >= S.layers.length) return;
    const [L] = S.layers.splice(S.li, 1);
    S.layers.splice(j, 0, L);
    S.li = j;
    S.dirty = true; recomposite(); layersPanel(); paint(); thumbs();
  }

  /** Fold the active layer into the one below — Aseprite's merge-down, and the
   *  way a stack gets back to something savable without losing edits. */
  function layerMerge(){
    if (!S || S.li === 0) { say("nothing below this layer to merge into", true); return; }
    snapshotAll("merge down");
    const top = S.layers[S.li], under = S.layers[S.li - 1];
    under.ctx.globalAlpha = top.opacity;
    /* Merge with the SAME operator the composite used, or folding two layers
       together silently changes the image it was showing a moment ago. */
    under.ctx.globalCompositeOperation = top.blend || "source-over";
    if (top.visible) under.ctx.drawImage(top.canvas, 0, 0);
    under.ctx.globalAlpha = 1;
    under.ctx.globalCompositeOperation = "source-over";
    S.layers.splice(S.li, 1);
    S.li -= 1;
    S.dirty = true; recomposite(); layersPanel(); paint(); thumbs();
  }

  function step(data, label){
    return { data: data, label: label || "edit", t: Date.now() };
  }

  /* AN UNDO ENTRY IS A LAYER, NOT THE COMPOSITE. Snapshotting `work` would
     restore a flattened picture into one layer and destroy the stack — the
     first undo after adding a layer would silently merge everything. `li` rides
     with the pixels so undo puts them back where they came from even if the
     active layer has changed since. */
  function snapshot(label){
    if (!S) return;
    const ctx = lctx();
    const e = step(ctx.getImageData(0, 0, S.w, S.h), label || S._pendingLabel);
    e.li = S.li;
    S._pendingLabel = null;
    S.undo.push(e);
    S.undoBytes += e.data.data.length;
    while (S.undoBytes > UNDO_BYTES && S.undo.length > 1){
      S.undoBytes -= S.undo.shift().data.data.length;
    }
    S.redo.length = 0;
    refreshHistory();
  }

  // Tools call snapshot() from deep inside pointer handling where the operation
  // name is not in scope; they set the label just before instead.
  function labelNext(label){ if (S) S._pendingLabel = label; }

  function setOnion(mode){
    if (!S) return;
    S.onion = ["off","prev","both","all"].indexOf(mode) >= 0 ? mode : "off";
    renderSide(); paint();
  }

  /* ── animation preview ─────────────────────────────────────────────────────
     Editing a walk cycle a cell at a time tells you nothing about whether it
     walks. This loops the frames at a chosen fps straight off the working
     canvas, so it reflects unsaved edits as you make them — the whole point is
     to see the change land. */
  function previewStop(){
    if (S && S.preview && S.preview.timer){
      clearInterval(S.preview.timer);
      S.preview.timer = null;
    }
  }

  function previewFrames(){
    // Either every cell in the sheet, or just the frames of a named animation.
    const p = S.preview;
    if (p.row !== "sheet"){
      const anim = (S.rig.animations || []).find(a => a.name === p.row);
      if (anim && anim.frames && anim.frames.length) return anim.frames.slice();
    }
    const n = frameCount();
    return Array.from({ length: n }, (_, i) => i);
  }

  function previewTick(){
    const cv = document.getElementById("se-prev");
    if (!cv || !S){ previewStop(); return; }
    const frames = previewFrames();
    if (!frames.length) return;
    const p = S.preview;
    /* PLAYBACK MODE. `i` counts forward always; the MODE decides which frame
       that count lands on, so a mode change mid-play cannot strand the index
       out of range or run the cursor backwards past zero. */
    const n = frames.length;
    let idx;
    if (p.mode === "reverse") idx = (n - 1) - (p.i % n);
    else if (p.mode === "pingpong"){
      /* One full cycle is 2n-2 steps: the two end frames are not repeated, or
         the turn reads as a stutter at each end. */
      const span = Math.max(1, 2 * n - 2);
      const k = p.i % span;
      idx = k < n ? k : span - k;
    } else idx = p.i % n;
    p.cur = idx;
    const b = frameBox(frames[idx]);
    const ctx = cv.getContext("2d");
    // Integer scale so pixel art stays pixel art.
    const scale = Math.max(1, Math.floor(Math.min(cv.width / b.w, cv.height / b.h)));
    const dw = b.w * scale, dh = b.h * scale;
    const dx = ((cv.width - dw) / 2) | 0, dy = ((cv.height - dh) / 2) | 0;
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, cv.width, cv.height);
    const chk = 8;
    ctx.fillStyle = BGTheme.color("--checker-b");
    ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.fillStyle = BGTheme.color("--checker-a");
    for (let y = 0; y < cv.height; y += chk)
      for (let x = ((y/chk)|0)%2 ? chk : 0; x < cv.width; x += chk*2)
        ctx.fillRect(x, y, chk, chk);
    ctx.drawImage(S.work, b.x, b.y, b.w, b.h, dx, dy, dw, dh);
    const lab = document.getElementById("se-prev-n");
    if (lab) lab.textContent = `${p.i + 1}/${frames.length}`;
    p.i++;
  }

  function previewStart(){
    previewStop();
    if (!S) return;
    previewTick();
    S.preview.timer = setInterval(previewTick, Math.max(30, 1000 / (S.preview.fps || 8)));
  }

  function previewToggle(on){
    if (!S) return;
    S.preview.on = on == null ? !S.preview.on : !!on;
    if (S.preview.on) previewStart(); else { previewStop(); }
    renderSide();
  }

  function previewField(k, v){
    if (!S) return;
    if (k === "fps") S.preview.fps = Math.max(1, Math.min(60, Number(v) || 8));
    else if (k === "row") { S.preview.row = String(v || "sheet"); S.preview.i = 0; }
    if (S.preview.on) previewStart();
    renderSide();
  }

  function refreshHistory(){
    const u = document.getElementById("se-undo"), r = document.getElementById("se-redo");
    if (u) u.disabled = !S.undo.length;
    if (r) r.disabled = !S.redo.length;
    if ($.name) $.name.innerHTML = E(S.rel);
    paintSaveState();
    renderHistory();
  }

  /* Is my work on disk? The word "unsaved" tucked after the filename was the
     whole answer before, and it said nothing about WHEN the last save was,
     nothing while one was in flight, and nothing at all when one failed - the
     failure was a 2.6s toast in the far corner while the eye was on the
     canvas. See SaveState in seats/_core.js.

     The rig is in here too. It is a separate sidecar with its own button, but
     it is the same document as far as "have I lost work" is concerned, and a
     saved sheet with an unsaved rig used to look completely clean. */
  function paintSaveState(){
    if (!window.SaveState || !S) return;
    const el = document.getElementById("se-save-state");
    if (!el) return;
    if (S.saving)         return SaveState.set(el, {state:"saving"});
    if (S.saveError)      return SaveState.set(el, {state:"error", detail:S.saveError});
    if (S.dirty && S.rigDirty)
      return SaveState.set(el, {state:"dirty", detail:"sheet and rig"});
    if (S.dirty)          return SaveState.set(el, {state:"dirty", detail:"Ctrl+S"});
    if (S.rigDirty)       return SaveState.set(el, {state:"dirty", detail:"rig not saved"});
    // at:0 - it is on disk, but not by this session, so there is no honest
    // "when" to print.
    SaveState.set(el, {state:"saved", at:S.savedAt || 0});
  }

  function renderHistory(){
    const host = document.getElementById("se-hist");
    if (!host || !S) return;
    const fmt = t => { const d = new Date(t);
      return String(d.getHours()).padStart(2,"0") + ":" +
             String(d.getMinutes()).padStart(2,"0") + ":" +
             String(d.getSeconds()).padStart(2,"0"); };
    // Oldest at the bottom: the newest state is the one you are looking at.
    const rows = [];
    S.redo.slice().forEach((e, i) => {
      rows.push(`<button class="se-hrow undone" data-redo="${S.redo.length - 1 - i}">` +
        `<span class="se-hdot"></span><span class="se-hl">${E(e.label)}</span>` +
        `<span class="se-ht">${fmt(e.t)}</span></button>`);
    });
    rows.push(`<div class="se-hrow now"><span class="se-hdot"></span>` +
      `<span class="se-hl">current</span>` +
      `<span class="se-ht">${S.dirty ? "unsaved" : "saved"}</span></div>`);
    S.undo.slice().reverse().forEach((e, i) => {
      rows.push(`<button class="se-hrow" data-undo="${i + 1}" ` +
        `title="step back to just before this">` +
        `<span class="se-hdot"></span><span class="se-hl">${E(e.label)}</span>` +
        `<span class="se-ht">${fmt(e.t)}</span></button>`);
    });
    rows.push(`<div class="se-hrow base"><span class="se-hdot"></span>` +
      `<span class="se-hl">opened</span><span class="se-ht"></span></div>`);
    host.innerHTML = rows.join("");
    host.querySelectorAll("[data-undo]").forEach(b =>
      b.onclick = () => { const n = Number(b.dataset.undo); for (let i=0;i<n;i++) undo(); });
    host.querySelectorAll("[data-redo]").forEach(b =>
      b.onclick = () => { const n = S.redo.length - Number(b.dataset.redo); for (let i=0;i<n;i++) redo(); });
  }

  /** Put an entry back where it came from. `stack` entries restore the whole
   *  layer list (add/delete/merge); the rest restore one layer's pixels. */
  function restore(e){
    if (e.stack){ S.layers = e.stack; S.li = Math.min(e.li || 0, S.layers.length - 1); }
    else {
      const L = S.layers[Math.min(e.li ?? S.li, S.layers.length - 1)];
      if (L) L.ctx.putImageData(e.data, 0, 0);
    }
    recomposite();
  }
  function capture(label){
    const ctx = lctx();
    const e = step(ctx.getImageData(0, 0, S.w, S.h), label);
    e.li = S.li;
    return e;
  }
  function undo(){
    if (!S || !S.undo.length) return;
    const cur = S.undo[S.undo.length - 1];
    S.redo.push(cur.stack ? snapAll(cur.label) : capture(cur.label));
    const e = S.undo.pop();
    if (!e.stack) S.undoBytes -= e.data.data.length;
    restore(e);
    S.dirty = true; refreshHistory(); layersPanel(); paint(); thumbs();
  }
  function redo(){
    if (!S || !S.redo.length) return;
    const e = S.redo.pop();
    S.undo.push(e.stack ? snapAll(e.label) : capture(e.label));
    if (!e.stack) S.undoBytes += S.w*S.h*4;
    restore(e);
    S.dirty = true; refreshHistory(); layersPanel(); paint(); thumbs();
  }

  /* A STRUCTURAL CHANGE IS NOT A PIXEL CHANGE. Deleting or merging a layer
     cannot be undone by restoring pixels into a stack that no longer has that
     slot, so those operations snapshot the whole list — canvases copied, not
     referenced, or undo would hand back the same objects the redo just
     mutated. */
  function snapAll(label){
    const stack = S.layers.map(L => {
      const c = document.createElement("canvas");
      c.width = S.w; c.height = S.h;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(L.canvas, 0, 0);
      return { ...L, canvas: c, ctx };
    });
    return { stack, li: S.li, label: label || "layers", t: Date.now(),
             data: { data: { length: 0 } } };
  }
  function snapshotAll(label){
    if (!S) return;
    S.undo.push(snapAll(label));
    S.redo.length = 0;
    refreshHistory();
  }

  function inBounds(x, y){
    if (x < 0 || y < 0 || x >= S.w || y >= S.h) return false;
    if (S.focusFrame && frameCount() > 1){
      const b = frameBox(S.frame);
      return x >= b.x && y >= b.y && x < b.x+b.w && y < b.y+b.h;
    }
    return true;
  }

  /* EVERY PLOT MUST RECOMPOSITE BEFORE THE NEXT paint().
   *
   * `put` writes to the ACTIVE LAYER; `paint()` draws the COMPOSITE. Between
   * the two sits recomposite(), and the first layers pass left it out of the
   * freehand path — so a stroke landed in the layer, the screen redrew the
   * unchanged composite, and nothing appeared until some other operation
   * (a fill, an undo, a visibility toggle) happened to recomposite. The pixels
   * were never lost; they were invisible, which is worse, because the obvious
   * response is to draw the stroke again.
   *
   * Batched by the CALLER rather than done here: a full-canvas recomposite per
   * pixel would make a fast drag quadratic. Each gesture handler plots its
   * points, then calls settle() once before painting.
   */
  function settle(){ recomposite(); }

  function put(x, y, erase){
    const n = S.brush;
    const half = Math.floor((n-1)/2);
    for (let dy = 0; dy < n; dy++) for (let dx = 0; dx < n; dx++){
      const X = x-half+dx, Y = y-half+dy;
      if (!inBounds(X, Y)) continue;
      /* THE ACTIVE LAYER, not the composite — a stroke written to `work`
         disappears on the next recomposite with no error at all. */
      const c = lctx();
      if (erase) c.clearRect(X, Y, 1, 1);
      else { c.fillStyle = S.color; c.fillRect(X, Y, 1, 1); }
    }
  }


  /* ── pixel-perfect stroke ──────────────────────────────────────────────────
   *
   * A freehand line drawn with a mouse is plotted by Bresenham, and Bresenham
   * produces L-SHAPED CORNERS: where the line turns, three pixels meet in an
   * elbow. At 1:1 that is invisible. On a 32px sprite it is a lump — the line
   * reads two pixels thick at every bend, and cleaning them up by hand is most
   * of what "tidying a stroke" means in pixel art.
   *
   * THE RULE, and it is the whole algorithm: when the last three plotted pixels
   * form an elbow — the first and third are diagonal neighbours, and the middle
   * one is orthogonally adjacent to both — the middle one is redundant. Drop it.
   * The corner becomes a clean diagonal step.
   *
   *     ##          ##
   *     #      →     #        the lower-left pixel was the elbow
   *
   * Only for freehand. A deliberate rectangle or line tool must plot exactly
   * what it was asked for; the shapes are not a hand wobbling.
   */
  function strokePoint(d, x, y){
    if (!S.pixelPerfect || d.erase){ put(x, y, d.erase); return; }
    const hist = d.pp || (d.pp = []);
    const last = hist[hist.length - 1];
    if (last && last[0] === x && last[1] === y) return;   // same cell twice
    hist.push([x, y]);
    if (hist.length >= 3){
      const [a, b, c] = hist.slice(-3);
      const diag = Math.abs(a[0]-c[0]) === 1 && Math.abs(a[1]-c[1]) === 1;
      const elbow = (Math.abs(a[0]-b[0]) + Math.abs(a[1]-b[1])) === 1
                 && (Math.abs(c[0]-b[0]) + Math.abs(c[1]-b[1])) === 1;
      if (diag && elbow){
        /* Unplot the elbow. Erasing is the only honest way back — the pixel was
           already committed to the layer when it was plotted, and re-deriving
           the stroke from scratch on every move is quadratic in a gesture that
           can be thousands of points long. */
        const cx = lctx();
        cx.clearRect(b[0], b[1], 1, 1);
        hist.splice(hist.length - 2, 1);
      }
    }
    put(x, y, d.erase);
    if (hist.length > 8) hist.shift();     // three is all the rule needs
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
    const img = lctx().getImageData(box.x, box.y, box.w, box.h);
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
    lctx().putImageData(img, box.x, box.y); recomposite();
    return true;
  }

  /* ── frame-level operations ───────────────────────────────────────────── */
  function frameOp(kind){
    if (!S) return;
    const b = (frameCount() > 1) ? frameBox(S.frame) : {x:0,y:0,w:S.w,h:S.h};
    if (kind === "copy"){
      S.clip = lctx().getImageData(b.x, b.y, b.w, b.h);
      say(`frame ${S.frame} copied`, "ok");
      return;
    }
    if (kind === "paste"){
      if (!S.clip){ say("nothing copied yet"); return; }
      if (S.clip.width !== b.w || S.clip.height !== b.h){
        say("clipboard frame is a different size"); return;
      }
      snapshot("paste frame");
      lctx().putImageData(S.clip, b.x, b.y); recomposite();
    } else {
      snapshot("paste");
      const img = lctx().getImageData(b.x, b.y, b.w, b.h);
      const out = lctx().createImageData(b.w, b.h);
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
      lctx().putImageData(out, b.x, b.y); recomposite();
    }
    S.dirty = true; refreshHistory(); paint(); thumbs();
  }

  /* Trim the alpha halo a matte left behind: any pixel whose alpha sits in the
   * murky middle becomes fully transparent. This is the single most common
   * "fix the generated sheet" edit, and doing it by hand is 200 clicks. */
  function dehalo(threshold){
    if (!S) return;
    snapshot("de-halo");
    const img = lctx().getImageData(0, 0, S.w, S.h);
    const d = img.data;
    let hit = 0;
    for (let i = 3; i < d.length; i += 4){
      if (d[i] > 0 && d[i] < threshold){ d[i] = 0; hit++; }
      else if (d[i] >= threshold && d[i] < 255){ d[i] = 255; }
    }
    lctx().putImageData(img, 0, 0); recomposite();
    S.dirty = true; refreshHistory(); paint(); thumbs();
    say(`${hit} halo pixel${hit===1?"":"s"} cleared`, "ok");
  }

  /* ── stage interaction ────────────────────────────────────────────────── */
  function bindStage(){
    const v = $.view;
    v.addEventListener("pointerdown", ev => {
      if (!S) return;
      /* Ctrl+right is the hot wheel — tested BEFORE the pan branch and before
         setPointerCapture(). Before the pan branch because right-drag has
         panned this canvas since the day it shipped and must keep panning;
         before the capture because the wheel wants the move/up stream on
         window, and a capture on the canvas would hide it. Right WITHOUT ctrl
         falls straight through to the pan, byte for byte as before. */
      if (ev.button === 2 && (ev.ctrlKey || ev.metaKey)){
        ev.preventDefault(); ev.stopPropagation();
        wheelOpen(ev.clientX, ev.clientY);
        return;
      }
      if (S.wheel){ wheelClose(); return; }
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
        snapshot("bucket fill");
        if (bucket(p.x, p.y)){ S.dirty = true; paint(); thumbs(); }
        // A fill that changed nothing should not leave a history step. The pop
        // was here already; it just never gave the bytes back, so undoBytes
        // drifted up on every no-op click until the cap started evicting real
        // history early.
        else { const e = S.undo.pop(); if (e) S.undoBytes -= e.data.data.length; }
        refreshHistory();
        return;
      }
      if (S.tool === "line" || S.tool === "rect"){
        S.drag = {shape:S.tool, x0:p.x, y0:p.y, preview:[[p.x,p.y]], fill:ev.shiftKey};
        paint();
        return;
      }
      snapshot(S.tool === "eraser" ? "erase" : "paint");
      S.drag = {paint:true, erase:S.tool === "eraser", last:p};
      put(p.x, p.y, S.drag.erase);
      S.dirty = true; settle(); paint(); thumbs();
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
        linePoints(d.last.x, d.last.y, p.x, p.y).forEach(pt => strokePoint(d, pt[0], pt[1]));
        d.last = p;
        settle(); paint(); return;
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
        snapshot(d.fill ? d.shape + " (filled)" : d.shape);
        const erase = false;
        d.preview.forEach(pt => put(pt[0], pt[1], erase));
        S.dirty = true; settle(); thumbs();
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

  /* ── the hot wheel ─────────────────────────────────────────────────────────
   * Two ways in, because the two kinds of user are not the same person:
   *
   *   FLICK   press Ctrl+right, drag in a direction, release. No reading, no
   *           second click — this is the one that becomes reflex.
   *   LATCH   Ctrl+right and let go without moving. The wheel stays up and you
   *           click what you want. This is the one that teaches the first.
   *
   * Releasing in the middle, pressing Escape, or clicking outside the ring all
   * cancel with nothing selected. A cycler (brush size, grid, onion) keeps a
   * latched wheel open so you can tap it twice; everything else closes.
   *
   * The centre is nailed to the press point and the angles are constants, so
   * the direction of every entry is identical on every open. Near an edge the
   * RADIUS shrinks before the centre moves, and the centre only slides as a
   * last resort in a host too small to hold the floor size — neither changes
   * an angle, which is the only thing the hand remembers.
   */
  function wheelHostBox(){
    const r = $.back.getBoundingClientRect();
    const pad = 6;
    return {
      l: Math.max(r.left, 0) + pad,
      t: Math.max(r.top, 0) + pad,
      r: Math.min(r.right, window.innerWidth || r.right) - pad,
      b: Math.min(r.bottom, window.innerHeight || r.bottom) - pad,
      ox: r.left, oy: r.top,
    };
  }

  function nextBrush(){
    const i = BRUSH_STEPS.indexOf(S.brush);
    if (i >= 0) return BRUSH_STEPS[(i + 1) % BRUSH_STEPS.length];
    return BRUSH_STEPS.find(v => v > S.brush) || BRUSH_STEPS[0];
  }
  function nextOnion(){
    const i = ONION_ORDER.indexOf(S.onion);
    return ONION_ORDER[(i < 0 ? 0 : i + 1) % ONION_ORDER.length];
  }

  /* One entry per fixed slot, always WHEEL_SLOTS of them, always in slot order.
     `off` dims without moving anything. */
  function wheelItems(){
    const w = S.wheel;
    if (w && w.page === "colour"){
      const pal = (w.pal || []).slice(0, WHEEL_SLOTS);
      return WHEEL.map((s, i) => {
        const c = pal[i] || null;
        return { a:s.a, id:"swatch", colour:c, off:!c, on:!!c && !!S.color &&
                 c.toLowerCase() === String(S.color).toLowerCase(),
                 lab: c || "-",
                 html: c ? `<span class="se-wsw" style="background:${E(c)}"></span>` : "" };
      });
    }
    return WHEEL.map(s => wheelFace(s));
  }

  function wheelFace(s){
    const o = { a:s.a, id:s.id, kind:s.kind, lab:s.lab, on:false, off:false, html:I(s.i, 20) };
    if (s.kind === "tool"){
      const t = TOOLS.find(x => x.id === s.id);
      o.on = S.tool === s.id;
      o.lab = t ? `${s.lab} (${t.k.toUpperCase()})` : s.lab;
      return o;
    }
    const multi = frameCount() > 1;
    if (s.id === "undo"){
      o.off = !S.undo.length;
      o.lab = S.undo.length ? `undo ${S.undo[S.undo.length - 1].label}` : "nothing to undo";
    } else if (s.id === "brush"){
      o.html = `<b class="se-wnum">${S.brush}</b>`;
      o.lab = `brush ${S.brush} → ${nextBrush()}`;
    } else if (s.id === "colour"){
      o.html = `<span class="se-wsw" style="background:${E(S.color)}"></span>`;
      o.off = !(S.wheel && S.wheel.pal && S.wheel.pal.length);
      o.lab = o.off ? "sheet has no colours" : `colour · ${S.color}`;
    } else if (s.id === "grid"){
      o.on = !!S.showGrid;
      o.off = !multi;
      o.lab = o.off ? "one frame - no grid" : (S.showGrid ? "grid on → off" : "grid off → on");
    } else if (s.id === "onion"){
      o.on = S.onion !== "off";
      o.off = !multi;
      o.lab = o.off ? "one frame - no onion" : `onion ${S.onion} → ${nextOnion()}`;
    }
    return o;
  }

  function wheelOpen(clientX, clientY){
    if (!S || !$.back) return;
    wheelClose();
    const box = wheelHostBox();
    // Radius follows the HOST, not the cursor: a wheel that resized every time
    // you clicked nearer an edge would cost the distance memory as surely as a
    // reorder costs the direction memory.
    const R = clamp(Math.min(box.r - box.l, box.b - box.t) / 2 / 1.24,
                    WHEEL_R_MIN, WHEEL_R_MAX);
    const btn = clamp(Math.round(R * 0.42), 28, 46);
    const need = R + btn / 2 + 2;
    const cx = clamp(clientX, box.l + need, Math.max(box.l + need, box.r - need));
    const cy = clamp(clientY, box.t + need, Math.max(box.t + need, box.b - need));
    S.wheel = { cx, cy, R, btn, mode:"press", page:"root", idx:-1,
                t0: Date.now(), sx: clientX, sy: clientY, moved: 0,
                pal: palette(), items: [] };
    wheelRender();
    window.addEventListener("pointermove", onWheelMove, true);
    window.addEventListener("pointerup", onWheelUp, true);
    window.addEventListener("pointercancel", onWheelCancelPtr, true);
    window.addEventListener("pointerdown", onWheelDown, true);
    window.addEventListener("contextmenu", onWheelMenu, true);
  }

  function wheelRender(){
    const w = S && S.wheel;
    if (!w || !$.back) return;
    w.items = wheelItems();
    const box = wheelHostBox();
    const rim = w.R + w.btn / 2 + 4;
    const hub = Math.max(30, Math.round(w.R * 0.34));

    let wrap = document.getElementById("se-wheel");
    if (!wrap){
      wrap = document.createElement("div");
      wrap.className = "se-wheelwrap";
      wrap.id = "se-wheel";
      $.back.appendChild(wrap);
    }
    wrap.innerHTML =
      `<div class="se-wheel" style="left:${(w.cx - box.ox - rim).toFixed(1)}px;` +
        `top:${(w.cy - box.oy - rim).toFixed(1)}px;width:${(rim*2).toFixed(1)}px;` +
        `height:${(rim*2).toFixed(1)}px;--wb:${w.btn}px">` +
      `<div class="se-wring" style="width:${w.R*2}px;height:${w.R*2}px"></div>` +
      `<div class="se-whub" style="width:${hub*2}px;height:${hub*2}px"></div>` +
      w.items.map((it, i) => {
        const th = it.a * Math.PI / 180;
        const x = rim + w.R * Math.sin(th), y = rim - w.R * Math.cos(th);
        return `<div class="se-wi${it.on ? " on" : ""}${it.off ? " off" : ""}" ` +
          `data-i="${i}" role="img" aria-label="${E(it.lab)}" ` +
          `style="left:${x.toFixed(1)}px;top:${y.toFixed(1)}px">${it.html}</div>`;
      }).join("") +
      `<span class="se-wlab" id="se-wlab"></span></div>`;
    w.idx = -2;                       // force wheelHot() to paint the first label
    wheelHot(-1);
  }

  function wheelHot(i){
    const w = S && S.wheel;
    if (!w || w.idx === i) return;
    w.idx = i;
    const wrap = document.getElementById("se-wheel");
    if (!wrap) return;
    wrap.querySelectorAll(".se-wi").forEach(
      el => el.classList.toggle("hot", Number(el.dataset.i) === i));
    const lab = wrap.querySelector("#se-wlab");
    if (lab) lab.textContent = (i >= 0 && w.items[i]) ? w.items[i].lab : "cancel";
  }

  /* Which slot a point aims at. Angle only — never distance — so the answer is
     the same whether the wheel drew at 108px or shrank to 68px in a narrow
     Studio pane. Inside the hub is the cancel zone and aims at nothing. */
  function wheelAim(x, y){
    const w = S.wheel;
    const dx = x - w.cx, dy = y - w.cy;
    if (Math.hypot(dx, dy) < Math.max(30, w.R * 0.34)) return -1;
    let th = Math.atan2(dx, -dy) * 180 / Math.PI;
    if (th < 0) th += 360;
    return Math.round(th / WHEEL_STEP) % WHEEL_SLOTS;
  }

  function onWheelMove(ev){
    const w = S && S.wheel;
    if (!w) return;
    // The FURTHEST the gesture ever got, not where it ended. Someone who drags
    // out to the eraser, changes their mind and comes back to the middle has
    // made a gesture and cancelled it; measuring only the final delta would
    // read that as a tap and leave the wheel up.
    w.moved = Math.max(w.moved, Math.hypot(ev.clientX - w.sx, ev.clientY - w.sy));
    wheelHot(wheelAim(ev.clientX, ev.clientY));
  }

  function onWheelUp(ev){
    const w = S && S.wheel;
    if (!w || w.mode !== "press") return;
    ev.preventDefault(); ev.stopPropagation();
    // A tap that went nowhere is "show me the wheel", not "pick nothing".
    if (w.moved < 6 && Date.now() - w.t0 < 320){ w.mode = "open"; return; }
    const i = wheelAim(ev.clientX, ev.clientY);
    if (i < 0) wheelClose(); else wheelPick(i);
  }

  /* Any pointerdown AFTER the one that opened the wheel — the opening event was
     already dispatched before these listeners existed, so it never lands here.
     Deliberately not gated on mode: a flick whose pointerup went missing (the
     pointer left the window mid-gesture) would otherwise leave a wheel that
     nothing could click. */
  function onWheelDown(ev){
    const w = S && S.wheel;
    if (!w) return;
    ev.preventDefault(); ev.stopPropagation();
    w.mode = "open";
    // Outside the ring entirely: clicked away.
    if (Math.hypot(ev.clientX - w.cx, ev.clientY - w.cy) > w.R + w.btn) return wheelClose();
    const i = wheelAim(ev.clientX, ev.clientY);
    if (i < 0) wheelClose(); else wheelPick(i);
  }

  function onWheelMenu(ev){ if (S && S.wheel){ ev.preventDefault(); ev.stopPropagation(); } }

  // A cancelled gesture picked nothing, but it should not eat the wheel either:
  // leave it up as if it had been tapped open.
  function onWheelCancelPtr(){ if (S && S.wheel) S.wheel.mode = "open"; }

  function wheelClose(){
    window.removeEventListener("pointermove", onWheelMove, true);
    window.removeEventListener("pointerup", onWheelUp, true);
    window.removeEventListener("pointercancel", onWheelCancelPtr, true);
    window.removeEventListener("pointerdown", onWheelDown, true);
    window.removeEventListener("contextmenu", onWheelMenu, true);
    const wrap = document.getElementById("se-wheel");
    if (wrap) wrap.remove();
    if (S) S.wheel = null;
  }

  function wheelPick(i){
    const w = S && S.wheel;
    if (!w) return;
    const it = w.items[i];
    if (!it || it.off) return wheelClose();     // dimmed slot: cancel, no surprise
    const latched = w.mode === "open";

    if (w.page === "colour"){
      wheelClose();
      if (it.colour) setColor(it.colour);
      return;
    }
    if (it.kind === "tool"){ wheelClose(); setTool(it.id); return; }

    switch (it.id){
      case "undo":
        wheelClose(); undo(); return;
      case "colour":
        // The sheet's own top colours, at the same twelve angles. One extra
        // flick, no new geometry to learn.
        w.page = "colour"; w.mode = "open"; wheelRender(); return;
      case "brush":
        S.brush = nextBrush(); renderSide(); break;
      case "grid":
        S.showGrid = !S.showGrid; renderSide(); paint(); break;
      case "onion":
        setOnion(nextOnion()); break;
      default:
        wheelClose(); return;
    }
    // Cyclers: a latched wheel stays up so you can tap to the value you want. A
    // flick is one gesture and ends where it ends.
    if (latched) wheelRender(); else wheelClose();
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
    // Above the input guard on purpose: the wheel can be up while focus is
    // still in a sidebar field, and there Escape must close the wheel rather
    // than blur the field — or close the whole editor.
    if (S.wheel && ev.key === "Escape"){
      ev.preventDefault(); ev.stopPropagation(); wheelClose(); return;
    }
    const t = ev.target;
    // This listener is on document in the CAPTURE phase, so it sees keys before
    // anything an ask dialog stops. Escape there means "cancel the question",
    // not "close the editor"; letters are its buttons, not this editor's tools.
    if (t && t.closest && t.closest(".ask")) return;
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

  /* ONE SHAPE FOR EVERY LID IN THIS SIDEBAR, and it is the app's own, not a
     local invention: .spanel + .sec-h out of app.css, built exactly the way
     settingsview.js and the seat workspaces build it.
     `.se-h` was what stood here — 9px of letter-spaced grey with no rule and no
     surface under it, eleven times down one 284px column. The result was that
     "rig labels", "coverage" and "regenerate frames" all looked like captions
     inside one continuous list rather than three sections, and nothing marked
     where the brush controls stopped and the sheet grid began.
     NO `s-<seat>` CLASS. That variant tints the header glyph with a seat's hue
     and belongs to the seat workspaces, where a panel really is one seat's
     property. An editor is a tool anyone opens; assetlib.js, world.js and the
     settings panel all ship plain .spanel for the same reason. KIND is what
     varies down this column anyway — a strip you re-order, a readout you cannot
     touch, a prompt you write — and it already owns the left edge. */
  function sec(icon, title, body, o){
    o = o || {};
    const n = (o.n === undefined || o.n === null || o.n === "")
      ? "" : `<span class="sec-n${o.tone ? " " + o.tone : ""}">${E(o.n)}</span>`;
    return `<section class="spanel ${o.kind || ""}">` +
      `<div class="sec-h">${I(icon, 15)}<h3 class="sec-t">${E(title)}</h3>${n}` +
      (o.note ? `<span class="sec-sub">${E(o.note)}</span>` : "") +
      (o.actions ? `<span class="sec-a">${o.actions}</span>` : "") +
      `</div>${body}</section>`;
  }


  /* THE LAYERS PANEL. Top of the sidebar on purpose: which layer is active
     decides where every stroke lands, so it is the first thing to read and the
     last thing you want to discover after twenty strokes went to the wrong
     one. Drawn top-down (topmost layer first) the way every editor draws it,
     while the array is bottom-up the way compositing needs it. */
  function layersSection(){
    if (!S || !S.layers || !S.layers.length) return "";
    const rows = S.layers.map((L, i) => i).reverse().map(i => {
      const L = S.layers[i];
      return `<div class="se-lyr${i === S.li ? " on" : ""}" onclick="SpriteEdit.pickLayer(${i})">
        <button class="se-lyr-eye" title="${L.visible ? "hide" : "show"} this layer"
                onclick="event.stopPropagation();SpriteEdit.toggleLayer(${i})">
          ${I(L.visible ? "visible" : "hidden", 13)}</button>
        <span class="se-lyr-n" title="${E(L.name)}">${E(L.name)}</span>
        <select class="se-lyr-b" title="blend mode" onclick="event.stopPropagation()"
                onchange="SpriteEdit.layerBlend(${i}, this.value)"
                ${i === 0 ? "disabled title='the bottom layer has nothing to blend with'" : ""}>
          ${BLEND_MODES.map(b => `<option value="${b[0]}"${(L.blend||"source-over")===b[0]?" selected":""}>${b[1]}</option>`).join("")}
        </select>
        <input class="se-lyr-o" type="range" min="0" max="100" value="${Math.round(L.opacity*100)}"
               title="opacity — ${Math.round(L.opacity*100)}%"
               onclick="event.stopPropagation()"
               oninput="SpriteEdit.layerOpacity(${i}, this.value)">
      </div>`;
    }).join("");
    return sec("sheet", "Layers",
      `<div class="se-lyrs">${rows}</div>
       <div class="se-row se-lyr-acts">
         <button class="se-btn" onclick="SpriteEdit.layerAdd()" title="new layer above this one">${I("sheet",13)} new</button>
         <button class="se-btn" onclick="SpriteEdit.layerMove(1)" title="move this layer up the stack">up</button>
         <button class="se-btn" onclick="SpriteEdit.layerMove(-1)" title="move this layer down the stack">down</button>
         <button class="se-btn" onclick="SpriteEdit.layerMerge()" title="fold this layer into the one below">merge</button>
         <button class="se-btn" onclick="SpriteEdit.layerDel()" title="delete this layer">${I("delete",13)}</button>
       </div>`,
      { note: "working surface — save flattens to one PNG" });
  }

  /** Re-render just the sidebar when the stack changes. renderSide() rebuilds
   *  the whole panel set, which is cheap and keeps one source of truth. */
  function layersPanel(){ renderSide(); }

  function renderSide(){
    if (!S || !$.side) return;
    const g = grid();
    const n = frameCount();
    const labels = (S.rig.labels || []).filter(l => l.frame === S.frame);
    const slots = [...new Set([...(window.SpriteEdit.KNOWN_SLOTS || []),
                               ...(S.rig.labels || []).map(l => l.slot)])];
    const pal = palette();
    const cov = coverageNow();

    $.side.innerHTML =
      layersSection() +
      sec("brush", "Brush", `
      <div class="se-row">
        <input class="se-sw" type="color" value="${E(S.color)}" oninput="SpriteEdit.setColor(this.value)">
        <label>size</label>
        <input class="se-in num" type="number" min="1" max="16" value="${S.brush}"
               oninput="SpriteEdit.setBrush(this.value)">
      </div>
      <label class="se-row se-chk" title="Drops the redundant pixel where a freehand line turns a corner, so a bend stays one pixel thick. Off for the eraser and for the shape tools, which must plot exactly what you asked for.">
        <input type="checkbox" ${S.pixelPerfect ? "checked" : ""}
               onchange="SpriteEdit.togglePP()">
        <span>pixel-perfect stroke</span>
      </label>
      <div class="se-pal">${pal.map(c =>
        `<span class="se-pc${c.toLowerCase()===S.color.toLowerCase()?" on":""}"
               title="${E(c)}" onclick="SpriteEdit.setColor('${E(c)}')"><span style="background:${E(c)}"></span></span>`).join("")}</div>`,
        // No .sec-sub here on purpose: the only thing it could carry is the
        // current colour, and the swatch two lines down already IS that, in the
        // one form a hex string cannot be read as.
        { }) +

      sec("snap_grid", "Sheet grid", `
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
        <label class="se-onion">onion
        <select class="se-in" onchange="SpriteEdit.setOnion(this.value)">
          <option value="off"${S.onion==="off"?" selected":""}>off</option>
          <option value="prev"${S.onion==="prev"?" selected":""}>previous</option>
          <option value="both"${S.onion==="both"?" selected":""}>before + after</option>
          <option value="all"${S.onion==="all"?" selected":""}>all frames</option>
        </select></label>
      </div>`,
        { note: `${g.cell_w}×${g.cell_h}` }) +

      sec("sprites", "Frames", `
      <div class="se-strip" id="se-strip"></div>
      <div class="se-row" style="margin-top:8px;flex-wrap:wrap">
        <button class="se-btn" title="Flip horizontally" aria-label="Flip horizontally"
                onclick="SpriteEdit.frameOp('fliph')">${I("flip_h")}</button>
        <button class="se-btn" title="Flip vertically" aria-label="Flip vertically"
                onclick="SpriteEdit.frameOp('flipv')">${I("flip_v")}</button>
        <button class="se-btn" title="Nudge this frame one pixel left"
                onclick="SpriteEdit.frameOp('left')">nudge left</button>
        <button class="se-btn" title="Nudge this frame one pixel right"
                onclick="SpriteEdit.frameOp('right')">right</button>
        <button class="se-btn" title="Nudge this frame one pixel up"
                onclick="SpriteEdit.frameOp('up')">up</button>
        <button class="se-btn" title="Nudge this frame one pixel down"
                onclick="SpriteEdit.frameOp('down')">down</button>
        <button class="se-btn" onclick="SpriteEdit.frameOp('copy')">copy</button>
        <button class="se-btn" onclick="SpriteEdit.frameOp('paste')">paste</button>
        <button class="se-btn" onclick="SpriteEdit.frameOp('clear')">clear</button>
      </div>
      <div class="se-row" style="margin-top:8px">
        <button class="se-btn" title="Clear the semi-transparent halo a matte left behind"
                onclick="SpriteEdit.dehalo(128)">de-halo sheet</button>
      </div>`,
        { kind: "k-list", n: n }) +

      sec("rig", "Rig labels", `
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
          <span class="x" title="remove this label" role="button" aria-label="remove this label" onclick="SpriteEdit.dropLabel(${l.frame},'${E(l.slot)}')">${I("delete", 13)}</span></div>`).join("")
        : `<div class="se-note se-warn">frame ${S.frame} has no labels</div>`}
      <div class="se-row" style="margin-top:7px">
        <button class="se-btn" onclick="SpriteEdit.spreadLabels()">spread to all frames</button>
      </div>`,
        { kind: "k-list", n: labels.length, note: `frame ${S.frame}`,
          tone: labels.length ? "" : "warn" }) +

      cov +

      sec("art", "Regenerate frames", regenPanel()) +

      sec("animation", "Animations", `
      ${(S.rig.animations || []).map((a, i) => `
        <div class="se-anim">
          <div class="hd">
            <input class="se-in" value="${E(a.name)}" oninput="SpriteEdit.animField(${i},'name',this.value)">
            <label title="loop"><input type="checkbox" ${a.loop?"checked":""}
              onchange="SpriteEdit.animField(${i},'loop',this.checked)"></label>
            <span class="x" style="cursor:pointer;color:var(--ash2)" title="remove this animation" role="button" aria-label="remove this animation" onclick="SpriteEdit.dropAnimation(${i})">${I("delete", 13)}</span>
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
      </div>`,
        { kind: "k-list", n: (S.rig.animations || []).length }) +

      sec("export", "Save", `
      <div class="se-row"><button class="se-btn" style="flex:1" id="se-rigsave"
        onclick="SpriteEdit.saveRig()">${S.rigDirty ? '<span class="se-mark"></span>' : ""}save rig</button></div>
      <div class="se-row"><button class="se-btn" style="flex:1"
        onclick="SpriteEdit.exportFrames()">export SpriteFrames .tres</button></div>
      <div class="se-note">The sidecar is <b>${E((S.info && S.info.sidecar) || "")}</b> —
        it travels with the art, not the database.</div>`,
        { note: S.rigDirty ? "unsaved" : "" }) +

      sec("real_preview", "Preview", `
      <div class="se-prevwrap">
        <canvas id="se-prev" width="252" height="150"></canvas>
        <span class="se-prev-n" id="se-prev-n"></span>
      </div>
      <div class="se-row">
        <button class="se-btn${S.preview.on ? " on" : ""}" style="flex:1"
          onclick="SpriteEdit.previewToggle()">${S.preview.on ? I("pause") + " pause" : I("run") + " play"}</button>
        <label class="se-fps">fps
          <input class="se-in" type="number" min="1" max="60" value="${S.preview.fps}"
                 style="width:52px" onchange="SpriteEdit.previewField('fps',this.value)"></label>
      </div>
      <div class="se-mode">
        ${[["loop","loop"],["reverse","reverse"],["pingpong","ping-pong"]].map(m =>
          `<button class="${(S.preview.mode||"loop")===m[0]?"on":""}"
                   onclick="SpriteEdit.previewMode('${m[0]}')">${m[1]}</button>`).join("")}
      </div>
      ${(S.rig.animations || []).length ? `<div class="se-row">
        <select class="se-in" style="flex:1" onchange="SpriteEdit.previewField('row',this.value)">
          <option value="sheet"${S.preview.row === "sheet" ? " selected" : ""}>whole sheet · ${n} frames</option>
          ${(S.rig.animations || []).map(a => `<option value="${E(a.name)}"${
            S.preview.row === a.name ? " selected" : ""}>${E(a.name)} · ${a.frames.length}f</option>`).join("")}
        </select></div>` : ""}`,
        { note: S.preview.on ? "playing" : "" }) +

      sec("timeline", "History", `
      <div class="se-hist" id="se-hist"></div>
      <div class="se-note">Click a step to go back to just before it. Undo depth is
        capped by memory, so the oldest steps drop off on long sessions.</div>`,
        { kind: "k-list", n: S.undo ? S.undo.length : "" });
    // renderSide() rebuilds the panel, which throws away the history list and
    // the preview canvas with it — repaint both into the fresh DOM.
    renderHistory();
    if (S.preview.on) previewStart(); else previewTick();
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
      <i></i>${E(h.short)}<span class="tick">${has ? I("select", 13) : ""}</span></button>`;
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
    // k-read: every line in here is derived from the labels and the animations
    // above it. There is nothing to click, and the left edge is the only thing
    // that says so before you try.
    const gaps = slots.filter(s => {
      const have = new Set((S.rig.labels || []).filter(l => l.slot === s).map(l => l.frame));
      return [...played].some(f => !have.has(f));
    }).length;
    return sec("verify", "Coverage", rows,
      { kind: "k-read", n: gaps || slots.length,
        tone: gaps ? "bad" : "good", note: gaps ? "with gaps" : "complete" });
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

  /* A SUBORDINATE LABEL, not another section band. These results live INSIDE
     the regenerate panel — a second .sec-h here would draw a header band in the
     middle of a panel and read as a new section that had swallowed the prompt
     above it. .sec-sub is app.css's answer to exactly this: a label inside a
     section, quieter than the band over it. */
  function regenResults(){
    return `<div class="sec-sub" style="margin:var(--s-5) 0 var(--s-4)">${
      I("verify", 13)} results</div>
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
    if (S.dirty && !(await askConfirm({
      title: "The sheet has unsaved pixel edits. Continue?",
      body: "Regeneration reads the file ON DISK, so those edits will not be in the reference.",
      ok: "regenerate anyway",
    }))) return;
    if (!S || S.regen.busy) return;

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
            : `${S.regen.results.length} frame(s) ready - accept or discard each`,
        bad ? undefined : "ok");
  }

  function applyResult(res){
    return new Promise(resolve => {
      const im = new Image();
      im.onload = () => {
        const b = frameBox(res.frame);
        /* A REGENERATED FRAME REPLACES THE FRAME, ON EVERY LAYER.
         *
         * Writing it to the ACTIVE layer — which is what the first layer pass
         * did — puts the agent's new frame on whatever overlay happened to be
         * selected, while the OLD frame stays on the base and shows through
         * every transparent pixel of the new one. The sheet then contains two
         * frames stacked, the audit measures the composite, and the artefact
         * that ships is neither what the agent produced nor what the editor
         * showed before.
         *
         * Regeneration is a replacement, not a paint stroke: clear that cell on
         * the whole stack, then draw into the BASE layer, which is the one that
         * holds what came off disk and what will be saved back. */
        for (const L of S.layers) L.ctx.clearRect(b.x, b.y, b.w, b.h);
        const base = S.layers[0] ? S.layers[0].ctx : lctx();
        base.drawImage(im, b.x, b.y, b.w, b.h);
        recomposite();
        resolve(true);
      };
      im.onerror = () => resolve(false);
      im.src = res.after;
    });
  }

  async function acceptRegen(i){
    const res = S.regen.results[i];
    if (!res || res.error || res.applied) return;
    snapshot("accept regenerated frame");
    if (!await applyResult(res)){ say("that result would not decode"); return; }
    res.applied = true;
    S.dirty = true;
    S.regen.results.splice(i, 1);
    refreshHistory(); renderSide(); paint(); thumbs();
  }

  async function acceptAllRegen(){
    const pending = S.regen.results.filter(r => !r.error && !r.applied);
    if (!pending.length) return;
    snapshot("accept all regenerated");                       // one undo step for the whole batch
    for (const res of pending) await applyResult(res);
    S.regen.results = S.regen.results.filter(r => r.error);
    S.dirty = true;
    refreshHistory(); renderSide(); paint(); thumbs();
    say(`${pending.length} frame(s) applied - save the sheet to keep them`, "ok");
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
    /* SAVING FLATTENS, AND A HIDDEN LAYER FLATTENS TO NOTHING.
     *
     * The PNG is what the engine loads and what the chroma/alpha audit
     * measures, so it can only ever be the composite. That is fine — but a
     * layer you hid to work on something else is content that silently will not
     * be in the file, and "silently" is the whole problem: you find out when the
     * sprite is missing its outline in the game. Asked once, before the write,
     * naming exactly what is about to be dropped. */
    const hidden = (S.layers || []).filter(L => !L.visible);
    if (hidden.length && !(await askConfirm({
      title: `Save without ${hidden.length} hidden layer${hidden.length === 1 ? "" : "s"}?`,
      body: `Saving flattens the stack into one PNG — the engine loads a PNG and `
          + `there is nowhere in it to keep a layer. ${hidden.map(L => L.name).join(", ")} `
          + `${hidden.length === 1 ? "is" : "are"} hidden, so ${hidden.length === 1 ? "it" : "they"} `
          + `will not be in the file. Show ${hidden.length === 1 ? "it" : "them"} first if you want `
          + `${hidden.length === 1 ? "it" : "them"} kept.`,
      ok: "save flattened", danger: true,
    }))) return;
    const png = S.work.toDataURL("image/png");
    S.saving = true; S.saveError = null; paintSaveState();
    const r = await mutate("/api/sprite/save", {
      body:{rel:S.rel, png, mtime:S.mtime}, button:"se-save"});
    S.saving = false;
    if (!r.ok) {
      // A refusal has to stay on screen next to the button that refused.
      S.saveError = String(r.error || "the server refused the write").slice(0, 120);
      paintSaveState();
      return;
    }
    S.saveError = null;
    S.dirty = false;
    S.savedAt = Date.now();
    S.mtime = r.data.mtime;
    refreshHistory();
    say(`saved · previous copy at ${r.data.backup}`, "ok");
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
    S.saving = true; S.saveError = null; paintSaveState();
    const r = await mutate("/api/sprite/rig", {body:{rel:S.rel, rig}, button:"se-rigsave"});
    S.saving = false;
    if (!r.ok) {
      S.saveError = String(r.error || "the rig sidecar was not written").slice(0, 120);
      paintSaveState();
      return;
    }
    S.rig = r.data.rig;
    S.rigDirty = false;
    S.savedAt = Date.now();
    paintSaveState();
    renderSide(); paint();
    say(`rig saved to ${r.data.sidecar}`, "ok");
  }

  async function exportFrames(){
    if (!S) return;
    if (S.rigDirty) await saveRig();
    const r = await mutate("/api/sprite/spriteframes", {body:{rel:S.rel}});
    if (!r.ok) return;
    say(`wrote ${r.data.written} · ${(r.data.animations||[]).join(", ")}`, "ok");
  }

  /* THE STEP AFTER "SAVED". This editor could make a sheet and write it to
     disk, and that was the end of the road — the owner's words were "i can go
     in sprite sheet edit and create sprites and save but dont know what i can
     do after". window.Handoff (frontend/src/shell/handoff/) is the shared answer for all three editors: pick a
     scene and wire it here for free, or file a work item whose brief already
     names this sheet, its animations and the scene. One line, because the
     panel is the same one the audio lab and the 3D editor open. */
  function handoff(){
    if (!window.Handoff){ say("the handoff panel did not load", true); return; }
    Handoff.fromEditor(S, {
      editor: "sprite",
      meta: {
        dirty: !!(S && (S.dirty || S.rigDirty)),
        animations: (S && S.rig && S.rig.animations || []).map(a => a.name),
        grid: (S && S.rig && S.rig.grid) || null,
      },
    });
  }

  function setColor(v){ if (S){ S.color = v; renderSide(); } }
  function setBrush(v){ if (S){ S.brush = clamp(parseInt(v,10)||1, 1, 16); } }
  function toggle(field, on){ if (S){ S[field] = !!on; paint(); } }

  // Studio entry point: render into `host` instead of over the whole page.
  function embed(host, rel){
    _host = host || null;
    // The landing card below is styled by the injected sheet, and every OTHER
    // entry point injects it on the way in (open, pick). Landing here cold —
    // which is exactly what opening the page does — therefore painted .se-land
    // with no rules at all: no card, no centring, just a heading and a button
    // in the top-left corner. audiolab.embed() has always carried this call;
    // this one was missing it.
    injectStyle();
    if (rel) return open(rel);
    // Landing state. Opening the tab used to fire the picker modal straight
    // away, which reads as an error dialog rather than as a workspace.
    if (host) host.innerHTML =
      '<div class="se-land">' +
        '<div class="se-land-in">' +
          '<h3>Sprite sheet editor</h3>' +
          '<p>Create a transparent frame grid by hand, import existing art, or continue an existing sheet.</p>' +
          '<div style="display:flex;justify-content:center;gap:8px;flex-wrap:wrap">' +
            '<button class="qbtn" onclick="SpriteEdit.newDialog(\'blank\')">create blank sheet</button>' +
            '<button class="qbtn ghost" onclick="SpriteEdit.newDialog(\'import\')">import image</button>' +
            '<button class="qbtn ghost" onclick="SpriteEdit.pick()">open existing</button>' +
          '</div>' +
        '</div></div>';
    return null;
  }
  function unembed(){ _host = null; }

  /* The rail's Sprite editor page. Called by activateWorkspace every time the
     view is shown, which includes coming BACK to it with a sheet still open, so
     it must not be a remount: reparenting moves the node and a canvas keeps its
     bitmap, where rebuilding from mount() would drop the undo history, the
     picked frames and every bound key. */
  function activate(){
    const host = document.getElementById("se-page");
    if (!host) return false;
    _host = host;
    if (!S) { embed(host); return true; }
    if ($ && $.back && $.back.parentNode !== host){
      $.back.classList.add("se-embed");
      host.innerHTML = "";
      host.appendChild($.back);
    }
    return true;
  }

  /* ── layer verbs, for the panel's onclick handlers ─────────────────────── */
  function pickLayer(i){
    if (!S || !S.layers[i]) return;
    S.li = i; layersPanel();
  }
  function toggleLayer(i){
    if (!S || !S.layers[i]) return;
    S.layers[i].visible = !S.layers[i].visible;
    S.dirty = true; recomposite(); layersPanel(); paint(); thumbs();
  }
  function previewMode(m){
    if (!S) return;
    S.preview.mode = m || "loop";
    S.preview.i = 0;
    renderSide();
  }
  function togglePP(){
    if (!S) return;
    S.pixelPerfect = !S.pixelPerfect;
    renderSide();
  }
  function layerBlend(i, mode){
    if (!S || !S.layers[i]) return;
    S.layers[i].blend = mode || "source-over";
    S.dirty = true; recomposite(); paint(); thumbs();
  }
  function layerOpacity(i, v){
    if (!S || !S.layers[i]) return;
    S.layers[i].opacity = clamp(Number(v) / 100, 0, 1);
    S.dirty = true; recomposite(); paint(); thumbs();
  }

  return {
    open, close, pick, pickSearch, closePick, fit, undo, redo, save, saveRig, exportFrames,
    newDialog, closeNew, newMode, newSummary, createSheet, importPicked, importSheet,
    handoff,
    activate,
    previewToggle, previewField, setOnion,
    layerAdd, layerDel, layerMove, layerMerge, pickLayer, toggleLayer, layerOpacity,
    layerBlend, togglePP, previewMode,
    embed, unembed,
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
