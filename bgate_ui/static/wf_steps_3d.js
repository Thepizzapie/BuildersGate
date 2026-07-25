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

  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  };
  // config value read helper (falls back to a step default)
  var cv = function (n, k, d) { return (n && n.config && n.config[k] != null && n.config[k] !== "") ? n.config[k] : d; };
  // WF.set call for an inline handler; `expr` is the JS producing the value
  var setJs = function (id, field, expr) {
    return "WF.set('" + id + "','" + field + "'," + expr + ")";
  };
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

  var C_ART = "var(--c-art)";
  var C_TECH = "var(--c-tech)";
  var GLYPH = "◳";

  var row = function (label, control) {
    return '<div class="wf-row"><label>' + esc(label) + "</label>" + control + "</div>";
  };

  /* ---- 3d.concept — 3D concept / turnaround brief -------------------------- */
  WF.registerStep({
    type: "3d.concept", category: "3d", label: "3D concept / turnaround", glyph: GLYPH, accent: C_ART,
    agentSeat: "art",
    ports: function () { return { in: [{ id: "i", label: "task" }], out: [{ id: "o", label: "concept" }] }; },
    defaults: { subject: "", style: "" },
    body: function (n) {
      var subj = cv(n, "subject", "");
      return '<div class="wf-b-note">' + (subj ? esc(subj) : "3D concept + turnaround") + '</div>' +
        '<div class="wf-b-tag">' + esc(cv(n, "style", "style?")) + '</div>';
    },
    config: function (n) {
      return '<div class="wf-insp-p">Concept sheet + turnaround for the 3D model — what to build and its visual language.</div>' +
        row("Subject", '<input value="' + esc(cv(n, "subject", "")) + '" oninput="' + setJs(n.id, "subject", "this.value") + '" placeholder="e.g. armored beetle">') +
        row("Style", '<input value="' + esc(cv(n, "style", "")) + '" oninput="' + setJs(n.id, "style", "this.value") + '" placeholder="e.g. stylized PBR">');
    },
    toBrief: function (n, wf) {
      return "Concept + turnaround for a 3D model of " + cv(n, "subject", "the subject") +
        " in " + cv(n, "style", "the target style") + " for: " + taskText(wf) + ".";
    }
  });

  /* ---- 3d.model — Blender model generation (bpy) --------------------------- */
  WF.registerStep({
    type: "3d.model", category: "3d", label: "Blender model", glyph: GLYPH, accent: C_ART,
    agentSeat: "art",
    ports: function () { return { in: [{ id: "i", label: "concept/ref" }], out: [{ id: "o", label: "model" }] }; },
    defaults: { topology: "med", rigged: false, targetTris: 8000 },
    body: function (n) {
      return '<div class="wf-b-note">' + esc(cv(n, "topology", "med")) + ' topology</div>' +
        '<div class="wf-b-tag">~' + esc(cv(n, "targetTris", 8000)) + ' tris' + (cv(n, "rigged", false) ? " · rigged" : "") + '</div>';
    },
    config: function (n) {
      var topo = cv(n, "topology", "med");
      var opt = function (v, l) { return '<option value="' + v + '"' + (topo === v ? " selected" : "") + ">" + l + "</option>"; };
      return '<div class="wf-insp-p">Generate the model in Blender (headless bpy). Export-ready mesh at the chosen topology / tri budget.</div>' +
        row("Topology", '<select onchange="' + setJs(n.id, "topology", "this.value") + '">' + opt("low", "low") + opt("med", "med") + opt("high", "high") + "</select>") +
        row("Target tris", '<input type="number" min="0" step="500" value="' + esc(cv(n, "targetTris", 8000)) + '" oninput="' + setJs(n.id, "targetTris", "(+this.value||0)") + '">') +
        row("Rigged", '<input type="checkbox"' + (cv(n, "rigged", false) ? " checked" : "") + ' onchange="' + setJs(n.id, "rigged", "this.checked") + '">');
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
    ports: function () { return { in: [{ id: "i", label: "model" }], out: [{ id: "o", label: "gltf" }] }; },
    defaults: { format: "glb", scale: 1 },
    body: function (n) {
      return '<div class="wf-b-note">export ' + esc(cv(n, "format", "glb")) + '</div>' +
        '<div class="wf-b-tag">scale ' + esc(cv(n, "scale", 1)) + '</div>';
    },
    config: function (n) {
      var fmt = cv(n, "format", "glb");
      var opt = function (v) { return '<option value="' + v + '"' + (fmt === v ? " selected" : "") + ">" + v + "</option>"; };
      return '<div class="wf-insp-p">Export the Blender model to glTF for Godot (blender_export_gltf).</div>' +
        row("Format", '<select onchange="' + setJs(n.id, "format", "this.value") + '">' + opt("glb") + opt("gltf") + "</select>") +
        row("Scale", '<input type="number" min="0" step="0.1" value="' + esc(cv(n, "scale", 1)) + '" oninput="' + setJs(n.id, "scale", "(+this.value||0)") + '">');
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
    ports: function () { return { in: [{ id: "i", label: "model" }], out: [{ id: "o", label: "sheet" }] }; },
    defaults: { angles: 8, poses: "idle", frameSize: "160x240" },
    body: function (n) {
      return '<div class="wf-b-note">' + esc(cv(n, "angles", 8)) + ' angles</div>' +
        '<div class="wf-b-tag">' + esc(cv(n, "frameSize", "160x240")) + '</div>';
    },
    config: function (n) {
      return '<div class="wf-insp-p">Render the 3D model down to a 2D sprite sheet from N angles (blender_sprites).</div>' +
        row("Angles", '<input type="number" min="1" step="1" value="' + esc(cv(n, "angles", 8)) + '" oninput="' + setJs(n.id, "angles", "(+this.value||0)") + '">') +
        row("Poses", '<input value="' + esc(cv(n, "poses", "idle")) + '" oninput="' + setJs(n.id, "poses", "this.value") + '" placeholder="idle, walk, attack">') +
        row("Frame size", '<input value="' + esc(cv(n, "frameSize", "160x240")) + '" oninput="' + setJs(n.id, "frameSize", "this.value") + '" placeholder="160x240">');
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
    ports: function () { return { in: [{ id: "i", label: "gltf" }], out: [{ id: "o", label: "asset" }] }; },
    defaults: { destRel: "assets" },
    body: function (n) {
      return '<div class="wf-b-note">import glTF → Godot</div>' +
        '<div class="wf-b-tag">' + esc(cv(n, "destRel", "assets")) + '/</div>';
    },
    config: function (n) {
      return '<div class="wf-insp-p">Import the exported glTF into the Godot project (godot_import_asset) and verify.</div>' +
        row("Dest (rel)", '<input value="' + esc(cv(n, "destRel", "assets")) + '" oninput="' + setJs(n.id, "destRel", "this.value") + '" placeholder="assets">');
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
    ports: function () { return { in: [{ id: "i", label: "asset" }], out: [{ id: "o", label: "ok" }] }; },
    defaults: { maxTris: 12000 },
    body: function (n) {
      return '<div class="wf-b-note">tri budget check</div>' +
        '<div class="wf-b-tag">max ' + esc(cv(n, "maxTris", 12000)) + ' tris</div>';
    },
    config: function (n) {
      return '<div class="wf-insp-p">Check the imported model against the tri budget and report scene stats.</div>' +
        row("Max tris", '<input type="number" min="0" step="500" value="' + esc(cv(n, "maxTris", 12000)) + '" oninput="' + setJs(n.id, "maxTris", "(+this.value||0)") + '">');
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
          { from: ["sprites", "o"], to: ["consistency", "i"] },
          { from: ["consistency", "o"], to: ["sheet", "i"] }
        ]
      };
    }
  });
})();
