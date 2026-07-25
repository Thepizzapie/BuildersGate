/* wf_steps_world.js — WORLD / background workflow steps + template for the WF builder.
 *
 * Contributes category:"world" steps (background art, parallax depth, tilesets, props,
 * and a Godot stage assembler) plus the "World / background" starter template. A fighting
 * game (Commodity Brawler) needs stage backgrounds with parallax depth: arena, stadium,
 * market, orchard, seaside. These steps define how the art/tech agents produce them.
 *
 * Loads after wf.js (window.WF). Also alongside wf_steps_asset.js, which contributes
 * control.consistency + output.rig — referenced by this file's template edges only.
 *
 * Never throws: everything is wrapped and guarded.
 */
(function () {
  if (!window.WF || typeof WF.registerStep !== "function") return;

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const cfg = (n, k, d) => (n && n.config && n.config[k] != null && n.config[k] !== "") ? n.config[k] : d;

  const WORLD = "var(--c-audio)";   // world green
  const TECH = "var(--c-tech)";     // engine/assembly blue

  /* small inline config helpers — node.id is known at render time, wire straight to WF.set */
  const rowText = (id, field, label, val, ph) =>
    `<div class="wf-row"><label>${esc(label)}</label>` +
    `<input value="${esc(val)}" placeholder="${esc(ph || "")}" ` +
    `oninput="WF.set('${id}','${field}',this.value)"></div>`;
  const rowNum = (id, field, label, val, min, max) =>
    `<div class="wf-row"><label>${esc(label)}</label>` +
    `<input type="number" value="${esc(val)}" min="${min}" max="${max}" style="width:66px" ` +
    `oninput="WF.set('${id}','${field}',this.value)"></div>`;
  const rowSel = (id, field, label, val, opts) =>
    `<div class="wf-row"><label>${esc(label)}</label><select ` +
    `onchange="WF.set('${id}','${field}',this.value)">` +
    opts.map(o => `<option value="${esc(o)}"${o === val ? " selected" : ""}>${esc(o)}</option>`).join("") +
    `</select></div>`;

  /* ---- world.background — background / environment art ------------------- */
  WF.registerStep({
    type: "world.background", category: "world", label: "Background art", glyph: "⛰", accent: WORLD,
    agentSeat: "art",
    defaults: { scene: "sprout stadium", style: "painterly pixel", mood: "dusk" },
    ports: () => ({ in: [{ id: "i", label: "" }], out: [{ id: "o", label: "bg" }] }),
    body: n => `<div class="wf-b-note"><b>${esc(cfg(n, "scene", "scene…"))}</b></div>` +
      `<div class="wf-b-tag">${esc(cfg(n, "style", "style"))} · ${esc(cfg(n, "mood", "dusk"))}</div>`,
    config: n => `<div class="wf-insp-p">The stage background / environment plate this workflow paints.</div>` +
      rowText(n.id, "scene", "Scene", cfg(n, "scene", ""), "e.g. sprout stadium") +
      rowText(n.id, "style", "Style", cfg(n, "style", ""), "e.g. painterly pixel") +
      rowSel(n.id, "mood", "Mood", cfg(n, "mood", "dusk"), ["day", "dusk", "night"]),
    toBrief: (n) => `Generate the ${cfg(n, "scene", "stage")} background in ${cfg(n, "style", "the house")} style, ` +
      `${cfg(n, "mood", "dusk")} lighting, for: ${cfg(n, "scene", "the stage")}.`,
  });

  /* ---- world.parallax — parallax layers for depth ----------------------- */
  WF.registerStep({
    type: "world.parallax", category: "world", label: "Parallax layers", glyph: "⛰", accent: WORLD,
    agentSeat: "art",
    defaults: { layers: 3 },
    ports: () => ({ in: [{ id: "i", label: "background" }], out: [{ id: "o", label: "layers" }] }),
    body: n => `<div class="wf-b-note">${esc(cfg(n, "layers", 3))} parallax layers</div>` +
      `<div class="wf-b-tag">far → near</div>`,
    config: n => `<div class="wf-insp-p">Splits the background into depth layers that scroll at different speeds.</div>` +
      rowSel(n.id, "layers", "Layers", String(cfg(n, "layers", 3)), ["2", "3", "4"]),
    toBrief: (n) => {
      const L = Math.max(2, Math.min(4, parseInt(cfg(n, "layers", 3), 10) || 3));
      return `Split/generate ${L} parallax layers (far → near) for the incoming background, ` +
        `each on its own transparent plane for depth scrolling.`;
    },
  });

  /* ---- world.tileset — repeatable environment tiles / props ------------- */
  WF.registerStep({
    type: "world.tileset", category: "world", label: "Tileset", glyph: "⛰", accent: WORLD,
    agentSeat: "art",
    defaults: { theme: "market stalls", tileCount: 12 },
    ports: () => ({ in: [{ id: "i", label: "" }], out: [{ id: "o", label: "tiles" }] }),
    body: n => `<div class="wf-b-note"><b>${esc(cfg(n, "theme", "theme…"))}</b></div>` +
      `<div class="wf-b-tag">~${esc(cfg(n, "tileCount", 12))} tiles</div>`,
    config: n => `<div class="wf-insp-p">A set of repeatable environment tiles / props for the stage floor and walls.</div>` +
      rowText(n.id, "theme", "Theme", cfg(n, "theme", ""), "e.g. market stalls") +
      rowNum(n.id, "tileCount", "Tiles", cfg(n, "tileCount", 12), 4, 64),
    toBrief: (n) => `Generate a ${cfg(n, "theme", "stage")} tileset of ~${cfg(n, "tileCount", 12)} ` +
      `repeatable, seamlessly-tiling pieces (floor, walls, edge caps).`,
  });

  /* ---- world.props — foreground/background prop set --------------------- */
  WF.registerStep({
    type: "world.props", category: "world", label: "Prop set", glyph: "⛰", accent: WORLD,
    agentSeat: "art",
    defaults: { kind: "crowd", count: 6 },
    ports: () => ({ in: [{ id: "i", label: "" }], out: [{ id: "o", label: "props" }] }),
    body: n => `<div class="wf-b-note">${esc(cfg(n, "count", 6))} × ${esc(cfg(n, "kind", "props"))}</div>`,
    config: n => `<div class="wf-insp-p">Foreground / background props — crowd, banners, hazards — that dress the stage.</div>` +
      rowSel(n.id, "kind", "Kind", cfg(n, "kind", "crowd"), ["crowd", "banners", "hazards", "foliage"]) +
      rowNum(n.id, "count", "Count", cfg(n, "count", 6), 1, 32),
    toBrief: (n) => `Generate a set of ${cfg(n, "count", 6)} ${cfg(n, "kind", "prop")} pieces ` +
      `(fore/background dressing) matched to the stage palette and mood.`,
  });

  /* ---- world.stage — assemble into a Godot stage/scene (SINK) ----------- */
  WF.registerStep({
    type: "world.stage", category: "world", label: "Assemble stage", glyph: "⛰", accent: TECH,
    agentSeat: "tech",
    defaults: { scene: "Sprout Stadium" },
    ports: () => ({ in: [{ id: "i", label: "assets" }] }),   // SINK — in only
    body: n => `<div class="wf-b-note">→ Godot scene</div>` +
      `<div class="wf-b-tag">${esc(cfg(n, "scene", "stage"))}</div>`,
    config: n => `<div class="wf-insp-p">Assembles background + parallax + props into a selectable Godot stage scene.</div>` +
      rowText(n.id, "scene", "Scene name", cfg(n, "scene", ""), "e.g. Sprout Stadium"),
    toBrief: (n) => `Assemble the background, parallax layers, and props into a Godot stage scene ` +
      `named "${cfg(n, "scene", "Stage")}" and wire it as a selectable arena.`,
  });

  /* ---- template: World / background ------------------------------------- */
  WF.registerTemplate({
    id: "tpl.world", name: "World / background", category: "world", glyph: "⛰",
    hint: "bg → parallax → props → stage",
    build() {
      const y = 140, dx = 230;
      return {
        nodes: [
          { id: "task", type: "input.task", x: 60, y },
          { id: "bg", type: "world.background", x: 60 + dx, y, config: { scene: "sprout stadium", style: "painterly pixel", mood: "dusk" } },
          { id: "para", type: "world.parallax", x: 60 + dx * 2, y, config: { layers: 3 } },
          { id: "props", type: "world.props", x: 60 + dx * 3, y, config: { kind: "crowd", count: 6 } },
          { id: "cons", type: "control.consistency", x: 60 + dx * 4, y },
          { id: "stage", type: "world.stage", x: 60 + dx * 5, y, config: { scene: "Sprout Stadium" } },
        ],
        edges: [
          { from: ["task", "o"], to: ["bg", "i"] },
          { from: ["bg", "o"], to: ["para", "i"] },
          { from: ["para", "o"], to: ["props", "i"] },
          { from: ["props", "o"], to: ["cons", "i"] },
          { from: ["cons", "o"], to: ["stage", "i"] },
        ],
      };
    },
  });
})();
