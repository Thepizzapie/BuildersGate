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
  if (!window.NodeCanvas || !NodeCanvas.w) return;

  // the node holds its own parameters; the inspector keeps the prose
  const w = NodeCanvas.w;

  const cfg = (n, k, d) => (n && n.config && n.config[k] != null && n.config[k] !== "") ? n.config[k] : d;

  /* Real plates on the node once a run has painted them — read from WF's one
     batch cache (a body must never do I/O). Before that: the empty plate, not a
     stand-in image. */
  const produced = (n, empty) => WF.mediaImage(n, empty);
  const producedStrip = (n, empty) => WF.mediaStrip(n, { empty: empty, cap: 4 });
  // priced from the image adapter's table (WF fetches it) — never from here
  const qualityOf = n => String(cfg(n, "quality", "medium"));
  const QUALITY = ["low", "medium", "high"];

  const WORLD = "var(--c-audio)";   // world green
  const TECH = "var(--c-tech)";     // engine/assembly blue


  /* ---- world.background — background / environment art ------------------- */
  WF.registerStep({
    type: "world.background", category: "world", label: "Background art", glyph: "⛰", accent: WORLD,
    agentSeat: "art",
    defaults: { scene: "sprout stadium", style: "painterly pixel", mood: "dusk", quality: "medium" },
    ports: () => ({ in: [{ id: "i", label: "context" }], out: [{ id: "o", label: "bg", type: "image" }] }),
    body: n => produced(n, "no plate painted yet") +
      w.text(n, "scene", { label: "Scene", placeholder: "sprout stadium" }) +
      w.text(n, "style", { label: "Style", placeholder: "painterly pixel" }) +
      w.select(n, "mood", { label: "Mood", options: ["day", "dusk", "night"], value: "dusk" }) +
      w.select(n, "quality", { label: "Quality", options: QUALITY, value: "medium" }),
    config: () => `<div class="wf-insp-p">The stage background / environment plate this workflow paints. Scene, style, mood and quality are set on the node - the mood drives the lighting the art seat paints to, and the quality tier is what the node's price estimate is drawn at.</div>`,
    imageCost: (n) => ({ images: 1, quality: qualityOf(n) }),
    toBrief: (n) => `Generate the ${cfg(n, "scene", "stage")} background in ${cfg(n, "style", "the house")} style, ` +
      `${cfg(n, "mood", "dusk")} lighting, at ${qualityOf(n)} quality, for: ${cfg(n, "scene", "the stage")}.`,
  });

  /* ---- world.parallax — parallax layers for depth ----------------------- */
  WF.registerStep({
    type: "world.parallax", category: "world", label: "Parallax layers", glyph: "⛰", accent: WORLD,
    agentSeat: "art",
    defaults: { layers: 3, quality: "medium" },
    ports: () => ({ in: [{ id: "i", label: "background", type: "image" }], out: [{ id: "o", label: "layers", type: "image" }] }),
    body: n => producedStrip(n, "no layers yet") +
      w.number(n, "layers", { label: "Layers", min: 2, max: 4, value: 3, hint: "far → near" }),
    imageCost: (n) => ({ images: Math.max(2, Math.min(4, parseInt(cfg(n, "layers", 3), 10) || 3)), quality: qualityOf(n) }),
    config: () => `<div class="wf-insp-p">Splits the background into depth layers that scroll at different speeds. Two reads flat, four is as much parallax as a fighting-game stage can carry before it distracts.</div>`,
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
    defaults: { theme: "market stalls", tileCount: 12, quality: "medium" },
    ports: () => ({ in: [{ id: "i", label: "context" }], out: [{ id: "o", label: "tiles", type: "image" }] }),
    body: n => producedStrip(n, "no tiles yet") +
      w.text(n, "theme", { label: "Theme", placeholder: "market stalls" }) +
      w.number(n, "tileCount", { label: "Tiles", min: 4, max: 64, value: 12 }),
    // 64 tiles is real money — the node says so before the run, not after
    imageCost: (n) => ({ images: +cfg(n, "tileCount", 12) || 0, quality: qualityOf(n) }),
    config: () => `<div class="wf-insp-p">A set of repeatable environment tiles / props for the stage floor and walls. Every piece has to tile seamlessly with its neighbours - that is the whole job.</div>`,
    toBrief: (n) => `Generate a ${cfg(n, "theme", "stage")} tileset of ~${cfg(n, "tileCount", 12)} ` +
      `repeatable, seamlessly-tiling pieces (floor, walls, edge caps).`,
  });

  /* ---- world.props — foreground/background prop set --------------------- */
  WF.registerStep({
    type: "world.props", category: "world", label: "Prop set", glyph: "⛰", accent: WORLD,
    agentSeat: "art",
    defaults: { kind: "crowd", count: 6, quality: "medium" },
    ports: () => ({ in: [{ id: "i", label: "stage" }], out: [{ id: "o", label: "props", type: "image" }] }),
    body: n => producedStrip(n, "no props yet") +
      w.select(n, "kind", { label: "Kind", options: ["crowd", "banners", "hazards", "foliage"], value: "crowd" }) +
      w.number(n, "count", { label: "Count", min: 1, max: 32, value: 6 }),
    imageCost: (n) => ({ images: +cfg(n, "count", 6) || 0, quality: qualityOf(n) }),
    config: () => `<div class="wf-insp-p">Foreground / background props - crowd, banners, hazards - that dress the stage. They inherit the background's palette and mood, so wire this after the background.</div>`,
    toBrief: (n) => `Generate a set of ${cfg(n, "count", 6)} ${cfg(n, "kind", "prop")} pieces ` +
      `(fore/background dressing) matched to the stage palette and mood.`,
  });

  /* ---- world.stage — assemble into a Godot stage/scene (SINK) ----------- */
  WF.registerStep({
    type: "world.stage", category: "world", label: "Assemble stage", glyph: "⛰", accent: TECH,
    agentSeat: "tech",
    defaults: { scene: "Sprout Stadium" },
    // SINK — in only, and untyped: it swallows every kind of stage asset.
    ports: () => ({ in: [{ id: "i", label: "assets" }] }),
    body: n => w.note("→ Godot scene") +
      w.text(n, "scene", { label: "Scene", placeholder: "Sprout Stadium" }),
    config: () => `<div class="wf-insp-p">Assembles background + parallax + props into a selectable Godot stage scene. Runs on the tech seat.</div>`,
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
          { from: ["props", "o"], to: ["cons", "candidate"] },
          { from: ["cons", "o"], to: ["stage", "i"] },
        ],
      };
    },
  });
})();
