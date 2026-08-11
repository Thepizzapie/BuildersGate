/* Handoff — "I made a thing. Now put it in the game."
 *
 * THE COMPLAINT THIS ANSWERS, verbatim: "rn i can go in sprite sheet edit and
 * create sprites and save but dont know what i can do after. Audio lab I have
 * no idea how to save or wire any of that up to specific scenes or triggers, a
 * way to hand off to an agent to wire it up w specific details / reference
 * points from atlas, or just give the user the tools to wire it up themselves
 * easier and seamlessly. same for sprite and 3d model editor."
 *
 * Every editor dead-ended at "saved". The verbs that finish the job all
 * existed — /api/scene/wire, /api/sprite/spriteframes, the Godot deliver path,
 * the queue — and none of them was reachable from the place where the asset had
 * just been made. So the asset sat on disk, referenced by nothing, and the next
 * step was a different view or Godot.
 *
 * MODELLED ON storyboard.promote AND THE CINEMATIC SEAT. That is the one place
 * in this product where a free thing crosses a boundary and becomes a committed
 * one, and the reason that seat reads as finished is that the crossing has a
 * NAME and a button. This is the same shape for assets, with two exits:
 *
 *   A. WIRE IT HERE — local, free, mechanical. Pick a real scene off disk, pick
 *      a real parent node, see the exact text that will be added, then write it.
 *      Prove it landed with godot_check_project and godot_screenshot.
 *
 *   B. HAND IT TO AN AGENT — a work item whose brief is ALREADY FILLED IN with
 *      the asset path, the chosen scene, the trigger, the Atlas references and
 *      what done looks like. The whole point is that the agent does not
 *      re-derive what is already on screen and the person does not retype it.
 *
 * ONE MODULE, THREE EDITORS. The sprite editor, the audio lab and the 3D model
 * editor each call Handoff.open(...) with the asset they are holding and get
 * the identical panel, worded for their kind. Three bespoke implementations is
 * exactly how this app got called "disjointed"; a person who learns this in one
 * editor knows it in all three.
 *
 *   Handoff.open({ path, kind, name, editor, refs, meta })
 *
 *     path    project-relative (or res://) path to the asset ON DISK. Not the
 *             editor's in-memory buffer: the file is what gets wired, which is
 *             why an unsaved editor gets told so rather than silently wiring
 *             the last save.
 *     kind    optional override; otherwise derived from the suffix server-side.
 *     name    display name. Defaults to the file name.
 *     editor  who opened it ("sprite" | "audio" | "model") — wording only.
 *     refs    optional Atlas node ids to pre-attach.
 *     meta    optional facts the brief should carry (animation names, dirty).
 *
 * NOTHING HERE GENERATES ANYTHING. Godot and Blender are the only engines it
 * touches and both are local and free. The moment "put this in the game" can
 * cost money it stops being the thing you press without thinking.
 *
 * DESTRUCTIVE STEPS ANNOUNCE THEMSELVES. Everything that writes into the game
 * project runs as a dry run FIRST and shows the plan; the commit is a separate,
 * differently-worded button. That is not politeness — these endpoints edit
 * files the engine also owns.
 */
window.Handoff = (() => {
  "use strict";

  const E = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const I = (n, size) => (window.BGIcon && BGIcon.has(n))
    ? BGIcon(n, { size: size || 14 }) : "";
  const say = (m, k) => { try { window.toast(m, k); } catch (e) { console.warn(m); } };

  function bytes(n) {
    const v = Number(n) || 0;
    if (v < 1024) return v + " B";
    if (v < 1048576) return (v / 1024).toFixed(0) + " KB";
    return (v / 1048576).toFixed(1) + " MB";
  }

  /* Every endpoint on this path answers in the api.ok envelope EXCEPT the two
   * engine ones (/api/godot/check, /api/godot/screenshot) and the queue, which
   * answer a bare dict. One normaliser rather than a shape test at nine call
   * sites — a panel that renders "undefined" because it guessed the envelope
   * wrong is the failure mode this file exists to stop repeating. */
  async function call(path, body) {
    let r;
    try {
      r = body === undefined
        ? await fetch(path)
        : await fetch(path, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body || {}),
        });
    } catch (e) {
      return { ok: false, error: "the dashboard did not answer - " + e.message };
    }
    let j = null;
    try { j = await r.json(); } catch (e) { j = null; }
    if (!j) return { ok: false, error: r.status + " " + r.statusText };
    if (j && typeof j.ok === "boolean" && ("data" in j || "error" in j)) {
      const err = j.error;
      return {
        ok: j.ok, data: j.data || {},
        error: !j.ok ? (err && err.message ? err.message : String(err || "refused")) : "",
        detail: (err && err.detail) || null,
      };
    }
    // A bare dict. Treat a missing `ok` as success (the scene reads do this).
    const okish = j.ok === undefined ? r.ok : !!j.ok;
    return { ok: okish, data: j, error: okish ? "" : String(j.error || r.status) };
  }

  /* ── state ─────────────────────────────────────────────────────────────── */
  let S = null;
  let $ = null;

  /* ── styles ────────────────────────────────────────────────────────────── */
  /* Injected rather than shipped as a stylesheet, the same way spriteedit,
     audiolab and modeledit already do it — this module is loaded by index.html
     with one <script> line and adding a <link> beside it would be a second
     thing to keep in step. Tokens only: --s-*, --r-*, --surface-*, so the light
     and orbit themes get this for free. */
  function injectStyle() {
    if (document.getElementById("hoff-style")) return;
    const css = [
      /* The editors sit at 1400/1401; ask.js is 9000 and must stay above this,
         because this panel asks for confirmation before it writes. */
      ".hoff-back{position:fixed;inset:0;z-index:2000;background:rgba(0,0,0,.62);" +
        "display:flex;align-items:center;justify-content:center;padding:var(--s-6)}",
      /* --solid-1, NOT --surface-1. On the orbit theme the surface ramp is an
         ALPHA ladder (rgba(255,255,255,.026) and up), which is the whole look
         and is correct for a panel sitting on the page's own ground. A modal
         painted with it has no ground of its own: the dashboard behind it
         showed straight through this dialog - overview counters and the
         activity feed reading through the brief. --solid-* exists for exactly
         this and composites to the same tone, opaque; on dark and light it is
         aliased to the surface ramp, so nothing changes there. Every .spanel
         inside then steps up the ladder over a real base, as designed. */
      ".hoff{width:min(1080px,96vw);max-height:92vh;display:flex;flex-direction:column;" +
        "background:var(--solid-1,var(--surface-1));border:1px solid var(--line);" +
        "border-radius:var(--r-lg);" +
        "box-shadow:var(--shadow-3,0 24px 64px rgba(0,0,0,.5));overflow:hidden}",
      ".hoff-bar{display:flex;align-items:center;gap:var(--s-4);flex:none;" +
        "padding:var(--s-4) var(--s-5);background:var(--surface-4);border-bottom:1px solid var(--line)}",
      ".hoff-bar .t{font-family:var(--mono);font-size:var(--fs-2xs);font-weight:var(--fw-semi);" +
        "letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text)}",
      ".hoff-bar .a{margin-left:auto;display:flex;gap:var(--s-3);align-items:center}",
      ".hoff-asset{display:flex;align-items:center;gap:var(--s-3);min-width:0;" +
        "font-family:var(--mono);font-size:var(--fs-2xs);color:var(--text-2);" +
        "background:var(--surface-1);border:1px solid var(--line);border-radius:var(--r-full);" +
        "padding:var(--s-2) var(--s-4);max-width:44ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".hoff-body{overflow:auto;padding:var(--s-5);display:flex;flex-direction:column;gap:var(--s-5)}",

      /* The two exits. A segmented control, not tabs and not stages: these are
         ALTERNATIVES, and numbering them 1/2 the way SeatStage does would say
         they are a sequence. */
      ".hoff-exits{display:grid;grid-template-columns:1fr 1fr;gap:var(--s-4)}",
      ".hoff-exit{display:flex;flex-direction:column;gap:var(--s-2);text-align:left;cursor:pointer;" +
        "padding:var(--s-5);background:var(--surface-2);border:1px solid var(--line);" +
        "border-left:2px solid var(--line);border-radius:var(--r-md);color:var(--text-2);font:inherit}",
      ".hoff-exit:hover{border-color:var(--line-strong);background:var(--surface-3)}",
      ".hoff-exit.on{background:var(--surface-3);border-color:var(--line-strong);" +
        "border-left-color:var(--accent);color:var(--text)}",
      ".hoff-exit b{font-size:var(--fs-md);font-weight:var(--fw-semi);color:var(--text);display:flex;" +
        "align-items:center;gap:var(--s-3)}",
      ".hoff-exit span{font-size:var(--fs-xs);line-height:var(--lh-snug)}",
      ".hoff-exit .free{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--good);" +
        "text-transform:uppercase;letter-spacing:var(--track-label)}",
      ".hoff-exit.off{opacity:.55}",

      ".hoff-row{display:flex;align-items:center;gap:var(--s-4);flex-wrap:wrap}",
      ".hoff-col{display:flex;flex-direction:column;gap:var(--s-3)}",
      ".hoff-lbl{font-family:var(--mono);font-size:var(--fs-3xs);text-transform:uppercase;" +
        "letter-spacing:var(--track-label);color:var(--text-2)}",
      ".hoff-note{font-size:var(--fs-xs);color:var(--text-3);line-height:var(--lh-snug)}",
      ".hoff-warn{font-size:var(--fs-xs);line-height:var(--lh-snug);color:var(--warn);" +
        "background:var(--warn-soft);border:1px solid var(--warn-line);" +
        "border-radius:var(--r-xs);padding:var(--s-3) var(--s-4)}",
      ".hoff-bad{color:var(--bad);background:var(--bad-soft);border-color:var(--bad-line)}",
      ".hoff-good{color:var(--good);background:var(--good-soft);border-color:var(--good-line)}",

      ".hoff-btn{display:inline-flex;align-items:center;gap:var(--s-2);padding:var(--s-3) var(--s-5);" +
        "background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-xs);" +
        "color:var(--text);font:inherit;font-size:var(--fs-xs);cursor:pointer;white-space:nowrap}",
      ".hoff-btn:hover:not(:disabled){border-color:var(--accent);background:var(--surface-3)}",
      ".hoff-btn:disabled{opacity:.4;cursor:default}",
      ".hoff-btn.go{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}",
      ".hoff-btn.go:hover:not(:disabled){background:var(--accent-hover);border-color:var(--accent-hover)}",
      ".hoff-btn.sm{padding:var(--s-2) var(--s-3);font-size:var(--fs-2xs)}",

      ".hoff-in,.hoff-sel,.hoff-ta{background:var(--bg);border:1px solid var(--line);" +
        "border-radius:var(--r-xs);color:var(--text);font:inherit;font-size:var(--fs-xs);" +
        "padding:var(--s-3) var(--s-4);min-width:0}",
      ".hoff-ta{font-family:var(--mono);font-size:var(--fs-xs);line-height:var(--lh-snug);" +
        "width:100%;resize:vertical;min-height:190px;white-space:pre-wrap}",
      ".hoff-in:focus,.hoff-sel:focus,.hoff-ta:focus{outline:2px solid var(--accent);outline-offset:-1px}",

      /* Scene picker: rows are wells inside the panel, one step DOWN the
         elevation ladder .spanel established. */
      ".hoff-scenes{max-height:230px;overflow:auto;border:1px solid var(--line);" +
        "border-radius:var(--r-xs);background:var(--bg)}",
      ".hoff-scene{display:flex;align-items:center;gap:var(--s-4);width:100%;text-align:left;" +
        "padding:var(--s-3) var(--s-4);background:transparent;border:0;border-bottom:1px solid var(--line);" +
        "color:var(--text-2);font:inherit;font-size:var(--fs-xs);cursor:pointer}",
      ".hoff-scene:last-child{border-bottom:0}",
      ".hoff-scene:hover{background:var(--surface-2);color:var(--text)}",
      ".hoff-scene.on{background:var(--surface-3);color:var(--text);box-shadow:inset 2px 0 0 var(--accent)}",
      ".hoff-scene .n{font-weight:var(--fw-semi)}",
      ".hoff-scene .p{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);" +
        "margin-left:auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:40ch}",
      ".hoff-tag{font-family:var(--mono);font-size:var(--fs-3xs);border:1px solid var(--line);" +
        "border-radius:var(--r-full);padding:0 var(--s-3);color:var(--text-3);flex:none}",
      ".hoff-tag.on{color:var(--good);border-color:var(--good-line);background:var(--good-soft)}",

      ".hoff-pre{font-family:var(--mono);font-size:var(--fs-2xs);line-height:1.5;white-space:pre-wrap;" +
        "word-break:break-word;background:var(--bg);border:1px solid var(--line);" +
        "border-radius:var(--r-xs);padding:var(--s-4);max-height:260px;overflow:auto;margin:0}",
      ".hoff-pre .add{color:var(--good)}",
      ".hoff-plan{margin:0;padding-left:var(--s-7);font-size:var(--fs-xs);line-height:1.65;color:var(--text-2)}",
      ".hoff-plan li::marker{color:var(--accent)}",

      ".hoff-chips{display:flex;flex-wrap:wrap;gap:var(--s-2)}",
      ".hoff-chip{display:inline-flex;align-items:center;gap:var(--s-2);font-family:var(--mono);" +
        "font-size:var(--fs-3xs);border:1px solid var(--line);border-radius:var(--r-full);" +
        "padding:var(--s-1) var(--s-3);color:var(--text-2);background:var(--surface-2)}",
      ".hoff-chip button{background:none;border:0;color:var(--text-3);cursor:pointer;font:inherit;padding:0 2px}",
      ".hoff-chip button:hover{color:var(--bad)}",
      ".hoff-refs{max-height:190px;overflow:auto;border:1px solid var(--line);" +
        "border-radius:var(--r-xs);background:var(--bg)}",
      ".hoff-ref{display:flex;gap:var(--s-3);align-items:center;width:100%;text-align:left;padding:var(--s-2) var(--s-4);" +
        "background:transparent;border:0;border-bottom:1px solid var(--line);color:var(--text-2);" +
        "font:inherit;font-size:var(--fs-2xs);cursor:pointer}",
      ".hoff-ref:hover{background:var(--surface-2);color:var(--text)}",
      ".hoff-ref.on{background:var(--surface-3);color:var(--text)}",
      ".hoff-ref .k{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);flex:none;min-width:8ch}",
      ".hoff-ref .u{margin-left:auto;font-size:var(--fs-3xs);color:var(--text-3);flex:none}",

      ".hoff-shot{max-width:100%;border:1px solid var(--line);border-radius:var(--r-xs);display:block}",
      ".hoff-busy{display:inline-flex;align-items:center;gap:var(--s-3);font-family:var(--mono);" +
        "font-size:var(--fs-2xs);color:var(--accent)}",
      ".hoff-steps{display:flex;flex-direction:column;gap:var(--s-3)}",
      "@media (max-width:760px){.hoff-exits{grid-template-columns:1fr}}",
    ].join("\n");
    const el = document.createElement("style");
    el.id = "hoff-style";
    el.textContent = css;
    document.head.appendChild(el);
  }

  /* ── open / close ──────────────────────────────────────────────────────── */
  function open(o) {
    o = o || {};
    if (!o.path) { say("nothing to hand off - this editor has no file open", true); return; }
    injectStyle();
    close();
    S = {
      path: String(o.path).replace(/\\/g, "/"),
      kindHint: o.kind || "",
      name: o.name || String(o.path).split("/").pop(),
      editor: o.editor || "",
      meta: o.meta || {},
      ctx: null, ctxError: "",
      target: 0,                  // index into ctx.wire.targets, when there are any
      exit: "wire", exitPinned: false,
      scene: "", sceneInfo: null, sceneFilter: "",
      parent: ".", nodeName: "", nodeType: "",
      props: {}, trigger: "", notes: "",
      plan: null, wrote: null,
      check: null, shot: null,
      prereq: null,               // dry run of the prerequisite step
      refs: (o.refs || []).slice(), refOpen: false, refFilter: "",
      atlas: null,
      seat: "tech", title: "", brief: "", briefEdited: false,
      filed: null, dispatched: null, autopilot: null,
      busy: "", error: "",
    };

    const back = document.createElement("div");
    back.className = "hoff-back";
    back.id = "hoff-back";
    back.innerHTML =
      '<div class="hoff" role="dialog" aria-modal="true" aria-label="Put this asset in the game">' +
        '<div class="hoff-bar">' +
          '<span class="t">put it in the game</span>' +
          '<span class="hoff-asset" id="hoff-asset"></span>' +
          '<span class="a">' +
            '<button class="hoff-btn sm" onclick="Handoff.close()">close</button>' +
          "</span>" +
        "</div>" +
        '<div class="hoff-body" id="hoff-body"></div>' +
      "</div>";
    back.addEventListener("mousedown", ev => { if (ev.target === back) close(); });
    document.body.appendChild(back);
    document.addEventListener("keydown", onKey, true);
    $ = { back, body: back.querySelector("#hoff-body"),
          asset: back.querySelector("#hoff-asset") };
    paint();
    load();
    return true;
  }

  function close() {
    document.removeEventListener("keydown", onKey, true);
    const b = document.getElementById("hoff-back");
    if (b) b.remove();
    S = null; $ = null;
  }

  function onKey(ev) {
    if (!S) return;
    if (ev.key === "Escape") {
      // Never steal Escape from a nested ask.js prompt: it is at z 9000, on top
      // of this panel, and closing the panel out from under a confirmation is
      // how a person loses a half-composed brief.
      if (document.querySelector(".ask-scrim")) return;
      ev.stopPropagation(); ev.preventDefault(); close();
    }
  }

  async function load() {
    const q = "/api/handoff/context?path=" + encodeURIComponent(S.path)
      + (S.kindHint ? "&kind=" + encodeURIComponent(S.kindHint) : "");
    const r = await call(q);
    if (!S) return;
    if (!r.ok) { S.ctxError = r.error || "the asset could not be read"; paint(); return; }
    S.ctx = r.data;
    S.target = 0;
    S.nodeName = S.ctx.wire.suggested_name || "Asset";
    S.nodeType = tgt().node_type || "";
    S.seat = S.ctx.seat || "tech";
    S.props = {};
    props().forEach(p => { S.props[p.key] = p.default; });
    /* FOLLOW WHAT IS POSSIBLE UNTIL THE PERSON SAYS OTHERWISE. A model opens
       on the agent exit because a .glb cannot be wired yet - and then they run
       the engine import, the wire becomes available, and leaving them parked on
       the other exit hides the step they just unlocked. Once they have clicked
       an exit themselves, that choice is theirs and this stops moving it. */
    if (!S.exitPinned) S.exit = S.ctx.wire.ok ? "wire" : "agent";
    S.title = defaultTitle();
    paint();

    // Autopilot and Atlas are both "nice to have by the time you get there" —
    // neither blocks the first paint, and a failure in either must not take the
    // panel down with it.
    call("/api/console/autopilot").then(a => {
      if (S) { S.autopilot = a.ok ? a.data : { on: false, unknown: true }; paint(); }
    });
    if (window.Atlas) {
      Atlas.ensure().then(() => { if (S) { S.atlas = Atlas.map; paint(); } })
        .catch(() => {});
    }
  }

  /* ── which engine-side thing are we actually wiring ────────────────────── */
  /* A sheet with a SpriteFrames beside it has TWO honest answers — the .tres
     that animates, or the raw sheet as one static Sprite2D — so the server
     hands back a `targets` list whenever there is a real choice, and one
     accessor keeps every read of "what are we wiring" going through the same
     place. Without targets, the single asset IS the target. */
  function tgt() {
    const t = (S.ctx && S.ctx.wire.targets) || [];
    if (t.length) return t[Math.min(S.target, t.length - 1)];
    const w = S.ctx ? S.ctx.wire : {};
    return {
      res: S.ctx ? S.ctx.asset.wire_res : null,
      name: S.ctx ? S.ctx.asset.name : "",
      kind: S.ctx ? S.ctx.asset.kind : "",
      label: "", node_type: w.node_type || "",
      choices: w.choices || [], props: w.props || [],
    };
  }
  function wireRes() { return tgt().res; }
  function props() { return tgt().props || []; }
  function choices() { return tgt().choices || []; }

  /* ── the composed brief: the whole point of exit B ─────────────────────── */
  function kindWord() {
    const k = S.ctx ? S.ctx.asset.kind : "";
    return { sprite: "sprite sheet", resource: "SpriteFrames resource",
             audio: "sound", mesh: "3D model", scene: "scene",
             script: "script" }[k] || "asset";
  }

  function defaultTitle() {
    const a = S.ctx.asset;
    const where = S.scene ? " into " + S.scene.split("/").pop() : "";
    return "Wire " + a.name + where;
  }

  function refLines() {
    if (!S.refs.length) return [];
    const nodes = (S.atlas && S.atlas.nodes) || {};
    return S.refs.map(id => {
      const n = nodes[id] || {};
      return "  - " + id + (n.kind ? "  (" + n.kind + ")" : "");
    });
  }

  function doneLines() {
    const a = S.ctx.asset;
    const k = a.kind;
    const scene = S.scene || "<the scene you choose>";
    const out = [
      "- " + scene + " references " + (wireRes() || a.res || a.rel)
        + " and godot_check_project comes back clean",
    ];
    if (k === "audio") {
      out.push("- the sound is on an AudioStreamPlayer node in that scene, on the "
        + "right bus, at a level that does not clip");
      out.push(S.trigger
        ? "- it plays on " + S.trigger + " and at no other time"
        : "- it plays when it should and at no other time (say when in the scene's script)");
    } else if (k === "sprite" || k === "resource") {
      out.push("- the node draws the right frames at the right size - "
        + "godot_screenshot of the scene shows it, not a magenta box or the whole sheet");
      const anims = (S.meta.animations || []).filter(Boolean);
      if (anims.length) out.push("- these animations exist and play: " + anims.join(", "));
    } else if (k === "mesh") {
      out.push("- the model loads in-engine with geometry, a material that carries "
        + "its texture, a sane real-world size and a collider (the godot_deliver_asset checks)");
      out.push("- it is instanced in the scene at a position that makes sense, not at the origin by default");
    }
    out.push("- nothing else in the scene moved: the diff is the node you added and its ext_resource");
    return out;
  }

  function composeBrief() {
    const a = S.ctx.asset;
    const L = [];
    L.push("Put an asset that already exists into the game. It is made and on "
      + "disk; this is the wiring, not the making. Do not regenerate it.");
    L.push("");
    L.push("ASSET");
    L.push("  path: " + a.rel + (a.res ? "   (" + a.res + ")" : ""));
    if (wireRes() && wireRes() !== a.res)
      L.push("  what goes in the scene: " + wireRes()
        + "   (" + (tgt().label || "the engine-side resource for it") + ")");
    L.push("  kind: " + kindWord() + "  ·  " + bytes(a.bytes)
      + (a.suffix ? "  ·  " + a.suffix : ""));
    if (S.editor) L.push("  made in: the " + S.editor + " editor in the Builders Gate dashboard");
    const anims = (S.meta.animations || []).filter(Boolean);
    if (anims.length) L.push("  animations labelled on it: " + anims.join(", "));
    if (S.meta.grid && S.meta.grid.cell_w)
      L.push("  frame grid: " + S.meta.grid.cell_w + "x" + S.meta.grid.cell_h
        + " (" + S.meta.grid.cols + " x " + S.meta.grid.rows + ")");

    L.push("");
    L.push("WHERE IT GOES");
    L.push("  scene: " + (S.scene || "NOT CHOSEN - pick the one this belongs in and say why"));
    if (S.scene) {
      L.push("  parent node: " + S.parent
        + (S.sceneInfo && S.sceneInfo.root ? "   (scene root is " + S.sceneInfo.root + ")" : ""));
      L.push("  node to add: " + (S.nodeType || "the right node for this kind")
        + ' named "' + (S.nodeName || a.stem) + '"');
      if (S.sceneInfo && S.sceneInfo.lock)
        L.push("  NOTE: this scene is locked by the " + S.sceneInfo.lock.seat
          + " seat - take the lock or wait, do not force it");
    }
    if (S.trigger) L.push("  trigger: " + S.trigger);
    const changed = Object.keys(S.props).filter(k => {
      const d = props().find(p => p.key === k);
      return d && String(S.props[k]) !== String(d.default);
    });
    if (changed.length)
      L.push("  properties asked for: " + changed.map(k => k + " = " + S.props[k]).join(", "));

    if (S.ctx.example) {
      const ex = S.ctx.example;
      L.push("");
      L.push("PATTERN TO MATCH");
      L.push(ex.in_script
        // No scene in this project holds a node of this type; the pattern is in
        // GDScript. Saying "follow the existing pattern" without saying WHERE is
        // how an agent goes and invents a second one.
        ? "  " + ex.scene + " is where this project builds its " + ex.type
          + " nodes - in script, not in a scene. Read it FIRST and follow it "
          + "rather than adding a node to the scene tree, unless that file says otherwise."
        // "a AnimatedSprite2D" in a brief a human is about to read is the kind
        // of tell that makes the whole thing look machine-filled.
        : "  " + ex.scene + " already has "
          + (/^[AEIOU]/.test(ex.type) ? "an " : "a ") + ex.type + ' named "' + ex.node
          + '". Read it and follow it - naming, parenting, bus, and how it is triggered.');
    }
    const refs = refLines();
    if (refs.length) {
      L.push("");
      L.push("REFERENCE POINTS (from Atlas - these are what this asset relates to)");
      refs.forEach(r => L.push(r));
    }
    const steps = (S.ctx.steps || []).filter(s => !s.done);
    if (steps.length) {
      L.push("");
      L.push("BEFORE YOU CAN WIRE IT");
      steps.forEach(s => L.push("  - " + s.label + "  (" + s.why + ")"));
    }
    if (S.notes.trim()) {
      L.push("");
      L.push("FROM THE PERSON WHO MADE IT");
      S.notes.trim().split("\n").forEach(t => L.push("  " + t));
    }
    L.push("");
    L.push("DONE LOOKS LIKE");
    doneLines().forEach(t => L.push("  " + t));
    L.push("");
    L.push("TOOLS THAT DO THIS: scene_outline to read the scene, scene_node_add / "
      + "scene_wire / scene_set_property to change it (dry_run first), "
      + "godot_check_project and godot_screenshot to prove it. All local, all free.");
    return L.join("\n");
  }

  function refreshBrief() {
    if (S.briefEdited) return;
    S.brief = composeBrief();
  }

  /* ── paint ─────────────────────────────────────────────────────────────── */
  function paint() {
    if (!S || !$) return;
    // A full repaint would take focus and the caret out from under whoever is
    // typing the brief. Record where they were, restore it after.
    const act = document.activeElement;
    const keep = act && $.body.contains(act) && act.id
      ? { id: act.id, a: act.selectionStart, b: act.selectionEnd } : null;

    if (S.ctx) {
      const a = S.ctx.asset;
      $.asset.innerHTML = I(a.kind === "audio" ? "audio" : a.kind === "mesh" ? "model" : "sprites", 13)
        + "<b>" + E(a.name) + "</b><span>" + E(a.rel) + "</span>";
      $.asset.title = a.rel;
      refreshBrief();
    } else {
      $.asset.textContent = S.path;
    }

    $.body.innerHTML = S.ctxError ? errorHtml()
      : !S.ctx ? '<div class="hoff-note">reading the asset…</div>'
      : (assetHtml() + exitsHtml() + (S.exit === "wire" ? wireHtml() : agentHtml()));

    bind();
    if (keep) {
      const el = document.getElementById(keep.id);
      if (el) {
        el.focus();
        try { el.setSelectionRange(keep.a, keep.b); } catch (e) {}
      }
    }
  }

  function errorHtml() {
    return '<div class="spanel"><div class="sec-h"><h4 class="sec-t">could not open</h4></div>'
      + '<div class="hoff-warn hoff-bad">' + E(S.ctxError) + "</div>"
      + '<p class="hoff-note">The path this editor handed over was <code>' + E(S.path)
      + "</code>. If the file has never been saved, save it first - the file on disk "
      + "is what a scene references.</p></div>";
  }

  function assetHtml() {
    const a = S.ctx.asset;
    const bits = [];
    if (!a.exists) bits.push('<div class="hoff-warn hoff-bad">There is no file at that path yet. '
      + "Save in the editor first - a scene can only reference a file that exists.</div>");
    else if (S.meta.dirty) bits.push('<div class="hoff-warn">This editor has unsaved changes. '
      + "What gets wired is the file ON DISK, not what is on your canvas. Save first if the "
      + "difference matters.</div>");
    /* "OUTSIDE THE PROJECT" IS A PROBLEM FOR A SHEET AND ROUTINE FOR A MODEL.
       Blender and the generators write .glb files to a staging directory on
       purpose — godot_import_asset refuses a source already inside the project,
       because copying a file onto itself is a Windows error — so the step below
       is the thing that brings it in. Painting a red "no scene can reference
       it" over a perfectly normal model is telling someone their file is in the
       wrong place while offering them the button that moves it. */
    /* Two ways it stops being a problem: the wire is already possible (the
       delivered copy inside the project is what gets wired, not this file), or
       a pending step below is the thing that brings it in. Either way the
       banner is telling someone their file is in the wrong place while the
       screen is already handling it. */
    const handled = S.ctx.wire.ok || (S.ctx.steps || []).some(s => !s.done);
    if (a.exists && !a.in_godot && !handled)
      bits.push('<div class="hoff-warn hoff-bad">This file is outside the Godot project, so no '
        + "scene can reference it. Move or import it into the project first.</div>");

    const steps = (S.ctx.steps || []);
    if (steps.length) {
      bits.push('<div class="hoff-steps">' + steps.map(s =>
        '<div class="hoff-row">'
        + '<span class="hoff-tag' + (s.done ? " on" : "") + '">' + (s.done ? "done" : "needed") + "</span>"
        + '<span class="hoff-note" style="flex:1;min-width:220px"><b>' + E(s.label) + "</b><br>"
        + E(s.done ? ("already there: " + (s.target || "")) : s.why) + "</span>"
        + (s.done ? "" : '<button class="hoff-btn sm" data-prereq="' + E(s.id) + '">'
            + "show me what that writes</button>")
        + "</div>").join("") + "</div>");
    }
    if (S.prereq) bits.push(prereqHtml());

    /* TWO HONEST ANSWERS GET A CHOOSER, not a silent pick. A sheet with a
       SpriteFrames beside it almost always wants to be wired as the
       SpriteFrames - that is what makes it animate - but "drop the whole sheet
       in as one static Sprite2D" is a real thing people do, and deciding for
       them is exactly the kind of surprise this panel exists to remove. */
    const targets = S.ctx.wire.targets || [];
    if (targets.length > 1) {
      bits.push('<div class="hoff-col" style="margin-top:var(--s-4)">'
        + '<span class="hoff-lbl">what actually goes in the scene</span>'
        + targets.map((t, i) =>
          '<label class="hoff-row" style="gap:var(--s-3);align-items:flex-start">'
          + '<input type="radio" name="hoff-target" data-target="' + i + '"'
          + (i === S.target ? " checked" : "") + ">"
          + '<span class="hoff-note"><b>' + E(t.name) + "</b> - " + E(t.label)
          + "<br><code>" + E(t.res) + "</code></span></label>").join("")
        + "</div>");
    }
    if (!bits.length) return "";
    return '<div class="spanel k-read"><div class="sec-h">' + I("assets", 15)
      + '<h4 class="sec-t">the asset</h4></div>' + bits.join("") + "</div>";
  }

  function prereqHtml() {
    const p = S.prereq;
    if (p.error) return '<div class="hoff-warn hoff-bad">' + E(p.error) + "</div>";
    const lines = p.plan || (p.would_write ? ["write " + p.would_write] : []);
    return '<div class="hoff-col" style="margin-top:var(--s-4)">'
      + '<span class="hoff-lbl">what this writes - nothing has happened yet</span>'
      + '<ol class="hoff-plan">' + lines.map(l => "<li>" + E(l) + "</li>").join("") + "</ol>"
      + (p.text ? '<pre class="hoff-pre">' + E(String(p.text).slice(0, 1600)) + "</pre>" : "")
      + '<div class="hoff-row"><button class="hoff-btn go" data-prereq-go="' + E(p.id) + '">'
      + "write it for real</button>"
      + '<button class="hoff-btn" data-prereq-cancel="1">cancel</button>'
      + (S.busy === "prereq" ? '<span class="hoff-busy">' + E(p.busyNote || "working…") + "</span>" : "")
      + "</div></div>";
  }

  function exitsHtml() {
    const wireOk = S.ctx.wire.ok;
    return '<div class="hoff-exits">'
      + '<button class="hoff-exit' + (S.exit === "wire" ? " on" : "") + (wireOk ? "" : " off")
        + '" data-exit="wire">'
        + "<b>" + I("run", 15) + "Wire it here</b>"
        + '<span class="free">local · free · nothing generated</span>'
        + "<span>Pick the scene and the node, see the exact text that gets added, then write it. "
        + "Prove it landed with a build check and a screenshot.</span>"
        + (wireOk ? "" : '<span class="free" style="color:var(--warn)">'
            + E(S.ctx.wire.why || "not available for this file") + "</span>")
      + "</button>"
      + '<button class="hoff-exit' + (S.exit === "agent" ? " on" : "") + '" data-exit="agent">'
        + "<b>" + I("agents", 15) + "Hand it to an agent</b>"
        + '<span class="free">a work item, brief already filled in</span>'
        + "<span>Files the asset path, the scene, the trigger and the Atlas references into a "
        + "brief so nobody retypes what is already on this screen.</span>"
      + "</button>"
      + "</div>";
  }

  /* ── exit A: wire it here ──────────────────────────────────────────────── */
  function wireHtml() {
    if (!S.ctx.wire.ok) {
      return '<div class="spanel k-read"><div class="sec-h">' + I("run", 15)
        + '<h4 class="sec-t">wire it here</h4></div>'
        + '<div class="hoff-warn">' + E(S.ctx.wire.why || "this file cannot be wired mechanically")
        + "</div>"
        + ((S.ctx.steps || []).some(s => !s.done)
          ? '<p class="hoff-note">Run the step above first, then this comes back.</p>'
          : '<p class="hoff-note">Hand it to an agent instead - that exit works for anything.</p>')
        + "</div>";
    }
    return scenePanel() + optionsPanel() + planPanel() + proofPanel();
  }

  function scenePanel() {
    const f = S.sceneFilter.toLowerCase();
    const list = (S.ctx.scenes || []).filter(s =>
      !f || s.label.toLowerCase().includes(f) || s.scene.toLowerCase().includes(f));
    const refScreens = S.refs.filter(id => String(id).endsWith(".tscn"));
    return '<div class="spanel k-list"><div class="sec-h">' + I("world", 15)
      + '<h4 class="sec-t">where it goes</h4>'
      + '<span class="sec-n">' + list.length + "</span>"
      + '<span class="sec-a"><input class="hoff-in" id="hoff-sfilter" placeholder="filter scenes"'
      + ' value="' + E(S.sceneFilter) + '" style="width:180px"></span></div>'
      + (refScreens.length ? '<p class="hoff-note">From your references: '
          + refScreens.map(id => '<button class="hoff-btn sm" data-scene="' + E(id) + '">'
            + E(id.split("/").pop()) + "</button>").join(" ") + "</p>" : "")
      + '<div class="hoff-scenes">' + (list.length ? list.map(s =>
        '<button class="hoff-scene' + (s.scene === S.scene ? " on" : "") + '" data-scene="'
        + E(s.scene) + '"><span class="n">' + E(s.label) + "</span>"
        + '<span class="hoff-tag">' + s.nodes + " nodes</span>"
        + (s.has_asset ? '<span class="hoff-tag on">already wired</span>' : "")
        + '<span class="p">' + E(s.scene) + "</span></button>").join("")
        : '<div class="hoff-note" style="padding:var(--s-5)">no .tscn matches that filter</div>')
      + "</div>"
      + (S.sceneInfo ? sceneDetail() : "")
      + "</div>";
  }

  function sceneDetail() {
    const si = S.sceneInfo;
    const ch = choices();
    return '<div class="hoff-col" style="margin-top:var(--s-5)">'
      + (si.lock ? '<div class="hoff-warn">This scene is held by the <b>' + E(si.lock.seat)
          + "</b> seat" + (si.lock.owner ? " (" + E(si.lock.owner) + ")" : "")
          + ". A write here can be overwritten by whatever that seat is mid-edit on.</div>" : "")
      + '<div class="hoff-row">'
        + '<span class="hoff-col" style="gap:var(--s-2)"><span class="hoff-lbl">parent node</span>'
          + '<select class="hoff-sel" id="hoff-parent">' + si.nodes.map(n =>
            '<option value="' + E(n.path) + '"' + (n.path === S.parent ? " selected" : "") + ">"
            + E(n.path === "." ? n.name + "  (root)" : n.path)
            + (n.type ? "  · " + E(n.type) : "") + "</option>").join("") + "</select></span>"
        + '<span class="hoff-col" style="gap:var(--s-2)"><span class="hoff-lbl">node name</span>'
          + '<input class="hoff-in" id="hoff-nodename" value="' + E(S.nodeName) + '"></span>'
        + (ch.length > 1 ? '<span class="hoff-col" style="gap:var(--s-2)">'
            + '<span class="hoff-lbl">node type</span><select class="hoff-sel" id="hoff-nodetype">'
            + ch.map(c => '<option value="' + E(c.type) + '"'
              + (c.type === S.nodeType ? " selected" : "") + ">" + E(c.type)
              + "  · " + E(c.property) + "</option>").join("") + "</select></span>"
          : '<span class="hoff-col" style="gap:var(--s-2)"><span class="hoff-lbl">node type</span>'
            + '<span class="hoff-note" style="padding-top:6px"><b>' + E(S.nodeType || "instance")
            + "</b></span></span>")
      + "</div></div>";
  }

  function optionsPanel() {
    if (!S.scene) return "";
    const list = props();
    const k = S.ctx.asset.kind;
    let inner = list.map(p => {
      const v = S.props[p.key];
      if (p.type === "bool")
        return '<label class="hoff-row" style="gap:var(--s-3)"><input type="checkbox" data-prop="'
          + E(p.key) + '"' + (v ? " checked" : "") + '><span class="hoff-note">' + E(p.label)
          + ' <code>' + E(p.key) + "</code></span></label>";
      return '<span class="hoff-col" style="gap:var(--s-2)"><span class="hoff-lbl">' + E(p.label)
        + '</span><input class="hoff-in" data-prop="' + E(p.key) + '" value="' + E(v == null ? "" : v)
        + '" style="width:170px">'
        + (p.hint ? '<span class="hoff-note">' + E(p.hint) + "</span>" : "") + "</span>";
    }).join("");
    if (list.length) inner = '<div class="hoff-row">' + inner + "</div>";

    if (k === "audio") {
      inner += '<div class="hoff-col" style="margin-top:var(--s-4)">'
        + '<span class="hoff-lbl">plays when</span>'
        + '<input class="hoff-in" id="hoff-trigger" placeholder="e.g. combat_start, on the floor loading, when the lift doors open" value="'
        + E(S.trigger) + '">'
        + '<span class="hoff-note">Autoplay is the only trigger that is a property, and it is set '
        + "above. Anything else - a signal, a state change, a cue from another node - is a line of "
        + "GDScript, which this exit does not write. Fill this in and it travels into the agent brief.</span>"
        + "</div>";
    }
    if (!inner) return "";
    return '<div class="spanel"><div class="sec-h">' + I("settings", 15)
      + '<h4 class="sec-t">how it behaves</h4></div>' + inner + "</div>";
  }

  function planPanel() {
    if (!S.scene) return "";
    const p = S.plan;
    return '<div class="spanel k-read"><div class="sec-h">' + I("verify", 15)
      + '<h4 class="sec-t">the plan</h4>'
      + '<span class="sec-a">' + (S.busy === "plan"
        ? '<span class="hoff-busy">reading the scene…</span>'
        : '<button class="hoff-btn" id="hoff-dry">' + (p ? "re-check" : "show me the change")
          + "</button>") + "</span></div>"
      + (!p ? '<p class="hoff-note">Nothing has been written. This runs the wire as a <b>dry run</b> '
          + "first and shows the exact lines it would add to " + E(S.scene) + ".</p>"
        : p.error ? '<div class="hoff-warn hoff-bad">' + E(p.error) + "</div>"
        : '<ol class="hoff-plan">' + p.steps.map(s => "<li>" + E(s) + "</li>").join("") + "</ol>"
          + '<pre class="hoff-pre"><span class="add">' + E(p.added) + "</span></pre>"
          + '<div class="hoff-row" style="margin-top:var(--s-4)">'
          + (S.wrote ? '<span class="hoff-warn ' + ((S.wrote.propErrors || []).length
                ? "" : "hoff-good") + '" style="flex:1">Written. '
              + E(S.wrote.summary || "") + (S.wrote.backup ? "  ·  previous copy at "
              + E(S.wrote.backup) : "")
              + ((S.wrote.propErrors || []).length
                ? "<br>The node landed; these properties did not, and the scene is "
                  + "otherwise fine:<br>" + E(S.wrote.propErrors.join(" · ")) : "")
              + "</span>"
            : '<button class="hoff-btn go" id="hoff-commit"' + (S.busy ? " disabled" : "") + ">"
              + "write it into " + E(S.scene.split("/").pop()) + "</button>"
              + '<span class="hoff-note">This edits a file the engine also owns. '
              + "A backup is taken.</span>")
          + (S.busy === "commit" ? '<span class="hoff-busy">writing…</span>' : "")
          + "</div>")
      + "</div>";
  }

  function proofPanel() {
    if (!S.wrote) return "";
    return '<div class="spanel k-read"><div class="sec-h">' + I("qa", 15)
      + '<h4 class="sec-t">proof it landed</h4></div>'
      + '<div class="hoff-row">'
        + '<button class="hoff-btn" id="hoff-check"' + (S.busy ? " disabled" : "") + ">"
        + "run the build check</button>"
        + '<button class="hoff-btn" id="hoff-shot"' + (S.busy ? " disabled" : "") + ">"
        + "screenshot this scene</button>"
        + (S.busy === "check" ? '<span class="hoff-busy">Godot is importing the project - '
            + "this can take a minute…</span>" : "")
        + (S.busy === "shot" ? '<span class="hoff-busy">Godot is rendering a frame…</span>' : "")
      + "</div>"
      + (S.check ? '<div class="hoff-warn ' + (S.check.ok ? "hoff-good" : "hoff-bad")
          + '" style="margin-top:var(--s-4)">'
          + (S.check.ok ? "The project imports clean" : "The import reported problems")
          + (S.check.seconds ? " · " + E(S.check.seconds) + "s" : "")
          + ((S.check.errors || []).length
            ? "<br>" + E(S.check.errors.slice(0, 6).join("\n")) : "")
          + (S.check.error ? "<br>" + E(S.check.error) : "")
          + "</div>" : "")
      + (S.shot ? (S.shot.rel
          ? '<img class="hoff-shot" style="margin-top:var(--s-4)" src="/api/preview?rel='
            + encodeURIComponent(S.shot.rel) + "&v=" + Date.now() + '" alt="the scene, rendered by Godot">'
          : '<div class="hoff-warn hoff-bad" style="margin-top:var(--s-4)">'
            + E(S.shot.error || "no frame came back") + "</div>") : "")
      + "</div>";
  }

  /* ── exit B: hand it to an agent ───────────────────────────────────────── */
  function agentHtml() {
    const seats = (window.BGWS && BGWS.seats) || ["director", "narrative", "gameplay",
      "tech", "art", "audio", "cinematic", "qa"];
    const ap = S.autopilot;
    const apOff = ap && !ap.on;
    return refsPanel()
      + '<div class="spanel k-doc"><div class="sec-h">' + I("agents", 15)
      + '<h4 class="sec-t">the work item</h4>'
      + '<span class="sec-a"><button class="hoff-btn sm" id="hoff-recompose"'
      + (S.briefEdited ? "" : " disabled") + '>rebuild the brief</button></span></div>'
      + '<div class="hoff-row" style="margin-bottom:var(--s-4)">'
        + '<span class="hoff-col" style="gap:var(--s-2)"><span class="hoff-lbl">seat</span>'
        + '<select class="hoff-sel" id="hoff-seat">' + seats.map(s =>
          '<option value="' + E(s) + '"' + (s === S.seat ? " selected" : "") + ">" + E(s)
          + "</option>").join("") + "</select></span>"
        + '<span class="hoff-col" style="gap:var(--s-2);flex:1;min-width:260px">'
        + '<span class="hoff-lbl">title</span>'
        + '<input class="hoff-in" id="hoff-title" value="' + E(S.title) + '"></span>'
      + "</div>"
      + '<div class="hoff-col" style="margin-bottom:var(--s-4)">'
        + '<span class="hoff-lbl">anything you want to say that is not on this screen</span>'
        + '<input class="hoff-in" id="hoff-notes" value="' + E(S.notes)
        + '" placeholder="optional - it goes into the brief verbatim"></span>'
      + "</div>"
      + '<div class="hoff-col"><span class="hoff-lbl">brief - this is what the agent reads</span>'
      + '<textarea class="hoff-ta" id="hoff-brief" spellcheck="false">' + E(S.brief) + "</textarea>"
      + (S.scene ? "" : '<span class="hoff-note">No target scene is picked, so the brief asks the '
          + "agent to choose one. Pick one under <b>Wire it here</b> first and it will be named.</span>")
      + "</div>"
      + '<div class="hoff-row" style="margin-top:var(--s-5)">'
        + (S.filed
          ? '<span class="hoff-warn hoff-good" style="flex:1">Filed as work item #'
            + E(S.filed.id) + " on the <b>" + E(S.filed.seat) + "</b> seat.</span>"
          : '<button class="hoff-btn go" id="hoff-file"' + (S.busy ? " disabled" : "")
            + ">file it on the board</button>")
        + (S.busy === "file" ? '<span class="hoff-busy">filing…</span>' : "")
      + "</div>"
      + (S.filed ? dispatchHtml(apOff) : (ap
          ? '<p class="hoff-note" style="margin-top:var(--s-3)">'
            + (apOff ? "Autopilot is <b>off</b> on this project, so a queued item will sit there "
                + "until something dispatches it. You get a dispatch button once it is filed."
              : "Autopilot is <b>on</b>, so the board will pick this up on its own.")
            + "</p>" : ""))
      + "</div>";
  }

  function dispatchHtml(apOff) {
    if (S.dispatched) {
      return '<div class="hoff-warn ' + (S.dispatched.ok ? "hoff-good" : "hoff-bad")
        + '" style="margin-top:var(--s-4)">'
        + (S.dispatched.ok
          ? "An agent is running on #" + E(S.filed.id) + " (pid " + E(S.dispatched.pid) + ")."
          : "It did not dispatch: " + E(S.dispatched.error || "no reason given")
            + "<br>The item is still on the board - nothing was lost.")
        + "</div>";
    }
    return '<div class="hoff-row" style="margin-top:var(--s-4)">'
      + '<button class="hoff-btn go" id="hoff-dispatch"' + (S.busy ? " disabled" : "") + ">"
      + (apOff ? "Autopilot is off - dispatch #" + E(S.filed.id) + " now"
        : "dispatch #" + E(S.filed.id) + " now") + "</button>"
      + '<span class="hoff-note">' + (apOff
        ? "Nothing will pick this up otherwise. A queued item on a board with autopilot off "
          + "looks exactly like delegated work and is not."
        : "Autopilot will get to it; this jumps the queue.") + "</span>"
      + (S.busy === "dispatch" ? '<span class="hoff-busy">spawning…</span>' : "")
      + "</div>";
  }

  /* ── references: Atlas is the picker ───────────────────────────────────── */
  function refsPanel() {
    const map = S.atlas;
    const chips = S.refs.length
      ? '<div class="hoff-chips">' + S.refs.map(id =>
        '<span class="hoff-chip">' + E(id)
        + '<button data-unref="' + E(id) + '" title="remove">&times;</button></span>').join("")
        + "</div>"
      : '<p class="hoff-note">Nothing attached. A reference is the thing this asset relates to - '
        + "the screen it belongs on, the sheet it matches, the script that will play it. "
        + "It travels into the brief so the agent does not go looking.</p>";
    let picker = "";
    if (S.refOpen) {
      if (!map || !map.nodes) {
        picker = '<p class="hoff-note">Atlas has not scanned yet, or the scan failed. '
          + "Nothing to pick from.</p>";
      } else {
        const f = S.refFilter.toLowerCase();
        const rows = Object.keys(map.nodes)
          .filter(id => !f || id.toLowerCase().includes(f)
            || String((map.nodes[id] || {}).label || "").toLowerCase().includes(f))
          .slice(0, 300);
        picker = '<div class="hoff-row" style="margin:var(--s-4) 0">'
          + '<input class="hoff-in" id="hoff-rfilter" placeholder="search everything Atlas knows about"'
          + ' value="' + E(S.refFilter) + '" style="flex:1">'
          + '<span class="hoff-note">' + rows.length + " shown</span></div>"
          + '<div class="hoff-refs">' + (rows.length ? rows.map(id => {
            const n = map.nodes[id] || {};
            return '<button class="hoff-ref' + (S.refs.includes(id) ? " on" : "") + '" data-ref="'
              + E(id) + '"><span class="k">' + E(n.kind || "?") + "</span><span>"
              + E(n.label || id) + "</span>"
              + (n.exists === false ? '<span class="hoff-tag">missing</span>' : "")
              + (n.orphan ? '<span class="hoff-tag">dead</span>' : "")
              + '<span class="u">' + E(id) + "</span></button>";
          }).join("") : '<div class="hoff-note" style="padding:var(--s-5)">nothing matches</div>')
          + "</div>";
      }
    }
    return '<div class="spanel k-list"><div class="sec-h">' + I("reference", 15)
      + '<h4 class="sec-t">reference points</h4>'
      + '<span class="sec-n">' + (S.refs.length || "") + "</span>"
      + '<span class="sec-a"><button class="hoff-btn sm" id="hoff-refopen">'
      + (S.refOpen ? "done" : "pick from Atlas") + "</button></span></div>"
      + chips + picker + "</div>";
  }

  /* ── events ────────────────────────────────────────────────────────────── */
  function bind() {
    const b = $.body;
    b.querySelectorAll("[data-exit]").forEach(el => el.onclick = () => {
      S.exit = el.dataset.exit; S.exitPinned = true; paint();
    });
    b.querySelectorAll("[data-scene]").forEach(el => el.onclick = () => pickScene(el.dataset.scene));
    b.querySelectorAll("[data-prereq]").forEach(el => el.onclick = () => prereqDry(el.dataset.prereq));
    b.querySelectorAll("[data-prereq-go]").forEach(el => el.onclick = () => prereqGo(el.dataset.prereqGo));
    b.querySelectorAll("[data-prereq-cancel]").forEach(el => el.onclick = () => { S.prereq = null; paint(); });
    b.querySelectorAll("[data-target]").forEach(el => el.onchange = () => {
      S.target = Number(el.dataset.target) || 0;
      S.nodeType = tgt().node_type || "";
      S.props = {};
      props().forEach(p => { S.props[p.key] = p.default; });
      S.plan = null; S.wrote = null;
      refreshBrief(); paint();
    });
    b.querySelectorAll("[data-ref]").forEach(el => el.onclick = () => toggleRef(el.dataset.ref));
    b.querySelectorAll("[data-unref]").forEach(el => el.onclick = () => toggleRef(el.dataset.unref));
    b.querySelectorAll("[data-prop]").forEach(el => {
      const key = el.dataset.prop;
      el.onchange = () => {
        S.props[key] = el.type === "checkbox" ? el.checked : el.value;
        S.plan = null; refreshBrief(); paint();
      };
    });

    const on = (id, ev, fn) => { const el = document.getElementById(id); if (el) el[ev] = fn; };
    on("hoff-sfilter", "oninput", ev => { S.sceneFilter = ev.target.value; paint(); });
    on("hoff-rfilter", "oninput", ev => { S.refFilter = ev.target.value; paint(); });
    on("hoff-refopen", "onclick", () => { S.refOpen = !S.refOpen; paint(); });
    on("hoff-parent", "onchange", ev => { S.parent = ev.target.value; S.plan = null; refreshBrief(); paint(); });
    on("hoff-nodename", "oninput", ev => { S.nodeName = ev.target.value; S.plan = null; refreshBrief(); });
    on("hoff-nodetype", "onchange", ev => { S.nodeType = ev.target.value; S.plan = null; refreshBrief(); paint(); });
    on("hoff-trigger", "oninput", ev => { S.trigger = ev.target.value; refreshBrief(); });
    on("hoff-notes", "oninput", ev => { S.notes = ev.target.value; refreshBrief();
      const ta = document.getElementById("hoff-brief"); if (ta && !S.briefEdited) ta.value = S.brief; });
    on("hoff-seat", "onchange", ev => { S.seat = ev.target.value; });
    on("hoff-title", "oninput", ev => { S.title = ev.target.value; });
    on("hoff-brief", "oninput", ev => { S.brief = ev.target.value; S.briefEdited = true; });
    on("hoff-recompose", "onclick", () => { S.briefEdited = false; refreshBrief(); paint(); });
    on("hoff-dry", "onclick", dryRun);
    on("hoff-commit", "onclick", commit);
    on("hoff-check", "onclick", runCheck);
    on("hoff-shot", "onclick", runShot);
    on("hoff-file", "onclick", fileIt);
    on("hoff-dispatch", "onclick", dispatchIt);
  }

  async function pickScene(scene) {
    S.scene = scene; S.sceneInfo = null; S.plan = null; S.wrote = null;
    S.check = null; S.shot = null;
    if (!S.briefEdited) S.title = defaultTitle();
    paint();
    const r = await call("/api/handoff/scene?scene=" + encodeURIComponent(scene));
    if (!S || S.scene !== scene) return;
    if (r.ok) {
      S.sceneInfo = r.data;
      S.parent = ".";
    } else say(r.error || "that scene could not be read", true);
    refreshBrief();
    paint();
  }

  function toggleRef(id) {
    const i = S.refs.indexOf(id);
    if (i === -1) S.refs.push(id); else S.refs.splice(i, 1);
    refreshBrief(); paint();
  }

  /* ── prerequisites (SpriteFrames / engine delivery) ────────────────────── */
  async function prereqDry(id) {
    S.busy = "prereq"; S.prereq = { id: id }; paint();
    let r;
    if (id === "spriteframes") {
      r = await call("/api/sprite/spriteframes", { rel: S.ctx.asset.rel, dry_run: true });
    } else if (id === "deliver") {
      r = await call("/api/handoff/mesh/deliver", { path: S.ctx.asset.rel, dry_run: true });
    } else { r = { ok: false, error: "unknown step" }; }
    if (!S) return;
    S.busy = "";
    S.prereq = r.ok ? Object.assign({ id: id }, r.data) : { id: id, error: r.error };
    paint();
  }

  async function prereqGo(id) {
    const what = id === "deliver"
      ? "Import this model into the engine and write a scene for it? It runs Godot headless "
        + "and can take a couple of minutes."
      : "Write the SpriteFrames resource next to the sheet? An existing one is backed up first.";
    const go = window.askConfirm
      ? await window.askConfirm({ title: "Write into the game project?", body: what, ok: "write it" })
      : true;
    if (!go || !S) return;
    S.busy = "prereq";
    S.prereq = Object.assign({}, S.prereq, {
      busyNote: id === "deliver" ? "Godot is importing - this can take a couple of minutes…"
        : "writing…" });
    paint();
    const r = id === "spriteframes"
      ? await call("/api/sprite/spriteframes", { rel: S.ctx.asset.rel })
      : await call("/api/handoff/mesh/deliver", { path: S.ctx.asset.rel, timeout: 420 });
    if (!S) return;
    S.busy = "";
    if (!r.ok) {
      S.prereq = { id: id, error: r.error || "the write was refused" };
      paint(); return;
    }
    S.prereq = null;
    say(id === "deliver" ? "delivered into the engine" : "wrote the SpriteFrames resource", "ok");
    // The context is now a different shape — a sheet has a .tres, a model has a
    // scene — so re-read it rather than patching the old one by hand.
    await load();
  }

  /* ── the wire itself ───────────────────────────────────────────────────── */
  /* A .tscn PROPERTY IS A GODOT LITERAL, NOT A JAVASCRIPT VALUE, and
     scenewire._prop_value refuses anything that is not — on purpose, because a
     malformed property survives the save and dies when the engine next loads
     the scene, pointing at a line nobody wrote by hand. So a text field's
     contents are spelled the way the file spells them before they are sent:
     "idle" for a String, &"Music" for a StringName. Sending the bare word came
     back as `'idle' is not a Godot value this can write safely`, which reads
     like the property is unsupported rather than like a quoting bug — measured,
     on the first real wire this panel did. */
  function godotValue(p, v) {
    if (p.type === "bool") return !!v;
    if (p.type === "number") { const n = Number(v); return Number.isFinite(n) ? n : null; }
    // The literal regex allows no escapes at all, so a quote or a backslash in
    // the field cannot be represented and is dropped rather than smuggled in.
    const s = String(v == null ? "" : v).replace(/["\\\r\n]/g, "").trim();
    if (!s) return null;
    return (p.literal === "stringname" ? '&"' : '"') + s + '"';
  }

  function propChanges() {
    return props().map(p => {
      const raw = S.props[p.key];
      if (raw === undefined || String(raw) === String(p.default)) return null;
      const value = godotValue(p, raw);
      return value === null ? null : { key: p.key, value: value };
    }).filter(Boolean);
  }

  async function dryRun() {
    if (!S.scene) { say("pick a scene first", true); return; }
    S.busy = "plan"; S.plan = null; paint();
    const r = await call("/api/scene/wire", {
      scene: S.scene, asset: wireRes(),
      parent: S.parent, node_name: S.nodeName || undefined,
      node_type: (S.nodeType && S.nodeType !== "(instance)") ? S.nodeType : undefined,
      dry_run: true,
    });
    if (!S) return;
    S.busy = "";
    if (!r.ok) { S.plan = { error: r.error || "the wire was refused" }; paint(); return; }
    const d = r.data;
    const steps = [d.summary || ("add " + S.nodeName + " under " + S.parent)];
    propChanges().forEach(p => steps.push("set " + p.key + " = " + p.value
      + " on " + (d.node || S.nodeName)));
    steps.push("take a backup of " + S.scene.split("/").pop() + " before writing");
    S.plan = { steps: steps, added: addedLines(d), node: d.node, reused: d.reused };
    paint();
  }

  /* The dry run answers with the WHOLE new scene file. Showing 400 lines of
     unchanged .tscn to prove a two-line addition is how a plan stops being
     read, so this pulls out the lines that are actually new: the ext_resource
     carrying the id it allocated, and the node block at the end. */
  function addedLines(d) {
    const text = String(d.text || "");
    const out = [];
    const ext = text.split("\n").find(l => l.startsWith("[ext_resource")
      && l.includes('id="' + d.id + '"'));
    if (ext && !d.reused) out.push(ext);
    else if (ext) out.push(ext + "        # already in this scene - reused, not added");
    const at = text.lastIndexOf('[node name="' + d.node + '"');
    if (at !== -1) out.push(text.slice(at).trimEnd());
    return out.length ? out.join("\n\n") : text.slice(-400);
  }

  async function commit() {
    const go = window.askConfirm ? await window.askConfirm({
      title: "Write into " + S.scene.split("/").pop() + "?",
      body: "This edits a scene file the engine also owns. A backup is taken first, and "
        + "the change is exactly the lines shown above.",
      ok: "write it",
    }) : true;
    if (!go || !S) return;
    S.busy = "commit"; paint();
    const r = await call("/api/scene/wire", {
      scene: S.scene, asset: wireRes(),
      parent: S.parent, node_name: S.nodeName || undefined,
      node_type: (S.nodeType && S.nodeType !== "(instance)") ? S.nodeType : undefined,
    });
    if (!S) return;
    if (!r.ok) {
      S.busy = ""; S.plan = Object.assign({}, S.plan, { error: r.error }); paint(); return;
    }
    const node = r.data.node;
    const nodePath = S.parent === "." ? node : S.parent + "/" + node;
    const failures = [];
    for (const p of propChanges()) {
      const pr = await call("/api/scene/node/property", {
        scene: S.scene, node: nodePath, key: p.key, value: p.value });
      if (!pr.ok) failures.push(p.key + ": " + pr.error);
    }
    if (!S) return;
    S.busy = "";
    S.wrote = Object.assign({}, r.data, { propErrors: failures });
    if (failures.length) say(failures.length + " property not set", true);
    else say("wired into " + S.scene.split("/").pop(), "ok");
    refreshBrief();
    paint();
  }

  async function runCheck() {
    S.busy = "check"; S.check = null; paint();
    const r = await call("/api/godot/check", {});
    if (!S) return;
    S.busy = ""; S.check = r.data || { ok: false, error: r.error }; paint();
  }

  async function runShot() {
    S.busy = "shot"; S.shot = null; paint();
    const r = await call("/api/godot/screenshot", { scene: S.scene });
    if (!S) return;
    S.busy = ""; S.shot = r.data || { error: r.error }; paint();
  }

  /* ── exit B actions ────────────────────────────────────────────────────── */
  async function fileIt() {
    if (!S.title.trim()) { say("the work item needs a title", true); return; }
    S.busy = "file"; paint();
    const r = await call("/api/queue", {
      seat: S.seat, title: S.title.trim(), brief: S.brief,
      source: "handoff", source_ref: S.ctx.asset.rel,
    });
    if (!S) return;
    S.busy = "";
    if (!r.ok || !r.data || !r.data.id) {
      say(r.error || "the board refused it", true); paint(); return;
    }
    S.filed = r.data;
    say("filed as #" + r.data.id, "ok");
    // Re-read autopilot at the moment it matters: it may have been switched on
    // since the panel opened, and offering to dispatch a thing the board is
    // already about to take is how two agents land on one item.
    const a = await call("/api/console/autopilot");
    if (S && a.ok) S.autopilot = a.data;
    paint();
  }

  async function dispatchIt() {
    const go = window.askConfirm ? await window.askConfirm({
      title: "Spawn an agent on #" + S.filed.id + "?",
      body: "It runs with edit permission on this project and will change files. "
        + "You can stop it from the Agents console.",
      ok: "dispatch",
    }) : true;
    if (!go || !S) return;
    S.busy = "dispatch"; paint();
    const r = await call("/api/queue/" + S.filed.id + "/dispatch", {});
    if (!S) return;
    S.busy = "";
    S.dispatched = r.data && typeof r.data.ok === "boolean" ? r.data
      : { ok: r.ok, error: r.error };
    say(S.dispatched.ok ? "agent running on #" + S.filed.id
      : (S.dispatched.error || "dispatch refused"), S.dispatched.ok ? "ok" : true);
    paint();
  }

  return {
    open, close,
    /* The one-line call site every editor uses. It reads the editor's own state
       object so a toolbar button is `Handoff.fromEditor(SpriteEdit.state, …)`
       rather than six lines of the same field-copying in three files. */
    fromEditor(state, o) {
      if (!state || !state.rel) {
        say("open a file first - there is nothing to put in the game", true);
        return false;
      }
      return open(Object.assign({
        path: state.rel,
        name: String(state.rel).split("/").pop(),
        meta: { dirty: !!(state.dirty || state.rigDirty) },
      }, o || {}));
    },
    get state() { return S; },
  };
})();
