/* wf_steps_3d.js — the 3D / Blender step types + templates for the WF builder.
 *
 * Builders Gate targets 2D AND 3D. The 3D lane is driven by the art/tech agents
 * through the pipeline: Blender headless (bpy) models + exports glTF, and
 * blender_sprites renders a 3D model down to 2D sprite sheets; Godot imports the
 * glTF. These steps DEFINE that process — Run dispatches the seat agent that
 * actually does the Blender/Godot work, so a step never calls Blender directly:
 * it carries the brief (toBrief) the agent executes.
 *
 * Registered via WF.registerStep / WF.registerTemplate (window.WF, from wf.js).
 * control.consistency + output.sheet are contributed by the asset step file
 * (wf_steps_asset.js) — referenced here by type only. Frontend-only, vanilla JS,
 * IIFE, never throws.
 */
(function () {
  "use strict";
  if (!window.WF || typeof WF.registerStep !== "function") return;
  if (!window.NodeCanvas || !NodeCanvas.w) return;

  // the node carries its own dials now; config() keeps the explanation
  var w = NodeCanvas.w;

  // config value read helper (falls back to a step default)
  var cv = function (n, k, d) { return (n && n.config && n.config[k] != null && n.config[k] !== "") ? n.config[k] : d; };
  // the workflow's Task text (from an input.task node) for brief interpolation
  var taskText = function (wf) {
    try {
      var t = (wf && wf.nodes || []).filter(function (n) { return n.type === "input.task"; })[0];
      var s = t && t.config && t.config.text;
      return (s && String(s).trim()) ? String(s).trim() : "the task";
    } catch (e) { return "the task"; }
  };
  // the subject/style from an upstream 3d.concept node, if any, else fallbacks
  var conceptFacet = function (wf, node, key, dflt) {
    try {
      var nodes = (wf && wf.nodes) || [];
      // this node's own value wins (model can carry its own subject via concept upstream)
      var c = nodes.filter(function (n) { return n.type === "3d.concept"; })[0];
      var v = c && c.config && c.config[key];
      if (v && String(v).trim()) return String(v).trim();
    } catch (e) {}
    return dflt;
  };

  /* What this step produced, from WF's one batch cache (no I/O in a body).
     A glTF or a .blend has no thumbnail — those steps get the empty plate
     rather than a broken image, which is the honest picture of "exported, not
     rendered". */
  var produced = function (n, empty) { return WF.mediaImage(n, empty); };
  var producedStrip = function (n, empty) { return WF.mediaStrip(n, { empty: empty, cap: 4 }); };
  var qualityOf = function (n) { return String(cv(n, "quality", "medium")); };

  var C_ART = "var(--c-art)";
  var C_TECH = "var(--c-tech)";
  var GLYPH = "◳";

  /* ---- 3d.concept — 3D concept / turnaround brief -------------------------- */
  WF.registerStep({
    type: "3d.concept", category: "3d", label: "3D concept / turnaround", glyph: GLYPH, accent: C_ART,
    agentSeat: "art",
    ports: function () { return { in: [{ id: "i", label: "task", type: "task" }], out: [{ id: "o", label: "concept", type: "image" }] }; },
    defaults: { subject: "", style: "", quality: "medium" },
    body: function (n) {
      return produced(n, "no concept yet - run to generate") +
        w.text(n, "subject", { label: "Subject", placeholder: "armored beetle" }) +
        w.text(n, "style", { label: "Style", placeholder: "stylized PBR" }) +
        w.select(n, "quality", { label: "Quality", options: ["low", "medium", "high"], value: "medium" });
    },
    // one painted concept sheet through the image adapter — priced from its table
    imageCost: function (n) { return { images: 1, quality: qualityOf(n) }; },
    config: function () {
      return '<div class="wf-insp-p">Concept sheet + turnaround for the 3D model - what to build and its visual language. The Blender steps downstream read the subject from here.</div>';
    },
    toBrief: function (n, wf) {
      return "Concept + turnaround for a 3D model of " + cv(n, "subject", "the subject") +
        " in " + cv(n, "style", "the target style") + " at " + qualityOf(n) +
        " quality for: " + taskText(wf) + ".";
    }
  });

  /* ---- 3d.model — Blender model generation (bpy) --------------------------- */
  WF.registerStep({
    type: "3d.model", category: "3d", label: "Blender model", glyph: GLYPH, accent: C_ART,
    agentSeat: "art",
    ports: function () { return { in: [{ id: "i", label: "concept/ref", type: "image" }], out: [{ id: "o", label: "model", type: "model" }] }; },
    defaults: { topology: "med", rigged: false, targetTris: 8000 },
    body: function (n) {
      return w.select(n, "topology", { label: "Topology", options: ["low", "med", "high"], value: "med" }) +
        w.number(n, "targetTris", { label: "Tris", min: 0, step: 500, value: 8000 }) +
        w.toggle(n, "rigged", { label: "Rigged" });
    },
    config: function () {
      return '<div class="wf-insp-p">Generate the model in Blender (headless bpy). Export-ready mesh at the chosen topology / tri budget - the budget here is what the artist aims at; the hard ceiling is the tri-budget step.</div>';
    },
    toBrief: function (n, wf) {
      return "Model " + conceptFacet(wf, n, "subject", "the subject") + " in Blender (headless bpy), " +
        cv(n, "topology", "med") + " topology (~" + cv(n, "targetTris", 8000) + " tris), export-ready; rigged=" +
        (cv(n, "rigged", false) ? "true" : "false") + ".";
    }
  });

  /* ---- 3d.gltf — glTF export ---------------------------------------------- */
  WF.registerStep({
    type: "3d.gltf", category: "3d", label: "glTF export", glyph: GLYPH, accent: C_TECH,
    agentSeat: "tech",
    ports: function () { return { in: [{ id: "i", label: "model", type: "model" }], out: [{ id: "o", label: "gltf", type: "gltf" }] }; },
    defaults: { format: "glb", scale: 1 },
    body: function (n) {
      // an export has a preview only if something rendered one; otherwise the
      // empty plate says so — Blender/Godot work spends no API money either
      return produced(n, "exported asset - no render to show") +
        w.select(n, "format", { label: "Format", options: ["glb", "gltf"], value: "glb" }) +
        w.number(n, "scale", { label: "Scale", min: 0, step: 0.1, value: 1 });
    },
    config: function () {
      return '<div class="wf-insp-p">Export the Blender model to glTF for Godot (blender_export_gltf). <code>glb</code> is the single-file binary Godot imports cleanly; scale corrects a Blender unit that does not match the game.</div>';
    },
    toBrief: function (n) {
      return "Export the Blender model to " + cv(n, "format", "glb") +
        " for Godot at scale " + cv(n, "scale", 1) + " (blender_export_gltf).";
    }
  });

  /* ---- 3d.sprites — render 3D → 2D sprite sheet (blender_sprites) ---------- */
  WF.registerStep({
    type: "3d.sprites", category: "3d", label: "3D → sprite sheet", glyph: GLYPH, accent: C_ART,
    agentSeat: "art",
    ports: function () { return { in: [{ id: "i", label: "model", type: "model" }], out: [{ id: "o", label: "sheet", type: "sheet" }] }; },
    defaults: { angles: 8, poses: "idle", frameSize: "160x240" },
    body: function (n) {
      // rendered sheets, capped — Blender renders locally, so there is media
      // here but never an API bill
      return producedStrip(n, "no sheet rendered yet") +
        w.number(n, "angles", { label: "Angles", min: 1, step: 1, value: 8 }) +
        w.text(n, "poses", { label: "Poses", placeholder: "idle, walk, attack" }) +
        w.text(n, "frameSize", { label: "Frame", placeholder: "160x240" });
    },
    config: function () {
      return '<div class="wf-insp-p">Render the 3D model down to a 2D sprite sheet from N angles (blender_sprites). Eight angles is the fighting-game default; the frame size fixes the cell the engine slices.</div>';
    },
    toBrief: function (n) {
      return "Render the 3D model to a 2D sprite sheet from " + cv(n, "angles", 8) +
        " angles, poses [" + cv(n, "poses", "idle") + "], frame " + cv(n, "frameSize", "160x240") + " (blender_sprites).";
    }
  });

  /* ---- 3d.import — import glTF into Godot --------------------------------- */
  WF.registerStep({
    type: "3d.import", category: "3d", label: "Import to Godot", glyph: GLYPH, accent: C_TECH,
    agentSeat: "tech",
    ports: function () { return { in: [{ id: "i", label: "gltf", type: "gltf" }], out: [{ id: "o", label: "asset", type: "asset" }] }; },
    defaults: { destRel: "assets" },
    body: function (n) {
      return w.note("import glTF → Godot") +
        w.text(n, "destRel", { label: "Dest", placeholder: "assets", hint: "project-relative" });
    },
    config: function () {
      return '<div class="wf-insp-p">Import the exported glTF into the Godot project (godot_import_asset) and verify it opens.</div>';
    },
    toBrief: function (n) {
      return "Import the glTF into the Godot project (godot_import_asset) under " +
        cv(n, "destRel", "assets") + " and verify.";
    }
  });

  /* ---- 3d.verify — scene stats / tri-budget check (SINK-ish: in + out) ----- */
  WF.registerStep({
    type: "3d.verify", category: "3d", label: "Tri-budget / stats", glyph: GLYPH, accent: C_TECH,
    agentSeat: "tech",
    ports: function () { return { in: [{ id: "i", label: "asset", type: "asset" }], out: [{ id: "o", label: "ok" }] }; },
    defaults: { maxTris: 12000 },
    body: function (n) {
      return w.note("tri budget check") +
        w.number(n, "maxTris", { label: "Max tris", min: 0, step: 500, value: 12000 });
    },
    config: function () {
      return '<div class="wf-insp-p">Check the imported model against the tri budget and report scene stats. This is the hard ceiling the modelling step only aims at.</div>';
    },
    toBrief: function (n) {
      return "Check the imported model's tri count vs budget " + cv(n, "maxTris", 12000) +
        " and scene stats (godot inspect / blender_scene_stats).";
    }
  });

  /* ---- templates ---------------------------------------------------------- */
  var X0 = 60, DX = 230, Y = 150;
  var col = function (i) { return X0 + i * DX; };

  WF.registerTemplate({
    id: "tpl.3dmodel", name: "3D model → Godot", category: "3d", glyph: GLYPH,
    hint: "concept → model → glTF → import",
    build: function () {
      return {
        nodes: [
          { id: "task", type: "input.task", x: col(0), y: Y },
          { id: "concept", type: "3d.concept", x: col(1), y: Y },
          { id: "model", type: "3d.model", x: col(2), y: Y },
          { id: "gltf", type: "3d.gltf", x: col(3), y: Y },
          { id: "import", type: "3d.import", x: col(4), y: Y },
          { id: "verify", type: "3d.verify", x: col(5), y: Y },
          { id: "gate", type: "control.gate", x: col(6), y: Y }
        ],
        edges: [
          { from: ["task", "o"], to: ["concept", "i"] },
          { from: ["concept", "o"], to: ["model", "i"] },
          { from: ["model", "o"], to: ["gltf", "i"] },
          { from: ["gltf", "o"], to: ["import", "i"] },
          { from: ["import", "o"], to: ["verify", "i"] },
          { from: ["verify", "o"], to: ["gate", "i"] }
        ]
      };
    }
  });

  WF.registerTemplate({
    id: "tpl.3dsprite", name: "3D → 2D sprites", category: "3d", glyph: GLYPH,
    hint: "model → render angles → sheet",
    build: function () {
      return {
        nodes: [
          { id: "task", type: "input.task", x: col(0), y: Y },
          { id: "model", type: "3d.model", x: col(1), y: Y },
          { id: "sprites", type: "3d.sprites", x: col(2), y: Y },
          { id: "consistency", type: "control.consistency", x: col(3), y: Y },
          { id: "sheet", type: "output.sheet", x: col(4), y: Y }
        ],
        edges: [
          { from: ["task", "o"], to: ["model", "i"] },
          { from: ["model", "o"], to: ["sprites", "i"] },
          { from: ["sprites", "o"], to: ["consistency", "candidate"] },
          { from: ["consistency", "o"], to: ["sheet", "frames"] }
        ]
      };
    }
  });
})();
