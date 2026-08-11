/* wf_steps_templates.js — the workflow LIBRARY: what Studio can build, on the
 * screen you land on.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * bgate_core/wfnodes.py made forty-seven MCP tools runnable from the canvas and
 * wf_steps_tools.js drew them into the palette. None of that is visible from
 * Studio's front door, because the front door is the LIBRARY — a grid of
 * template cards — and the palette only exists one click deeper, inside an
 * already-open workflow. Forty-seven new capabilities, zero of them reachable
 * without knowing they were there. The owner's report ("I have seen literally
 * no changes on the Studio") was accurate: the landing screen was byte-identical
 * to the one before the executor landed.
 *
 * So this file does two things.
 *
 * 1. TEMPLATES for the families that had none — cutscene, music, voice, items,
 *    levels, engine. Each one is a whole pipeline rather than a single card,
 *    because a one-node template teaches nothing about what the nodes make
 *    possible. Every argument name here is read off the REGISTRY in
 *    bgate_core/wfnodes.py; none of them are guessed, and a card that names a
 *    field that table does not have would fail with a 422 in the middle of a
 *    paid run, which is the exact failure that table exists to prevent.
 *
 * 2. SEPARATION. The categories already existed as tiny uppercase labels
 *    floating over one undifferentiated field of cards, so 2D art, worlds, 3D
 *    and agents read as one continuous wall. Adding five more weak labels would
 *    have made that worse. Each family is now a .spanel with a .sec-h header
 *    band — the pattern app.css already defines, not a seventh invented one —
 *    carrying an icon from icons.js, the family's own seat colour, and a count.
 *    The panels tile, so a family with one template sits BESIDE its neighbour
 *    instead of owning a full-width band and trailing 900px of dead space.
 *
 * OWNS: the library render (it replaces WF._renderLibrary) and its own styles.
 * Touches no other file's markup. Frontend only, vanilla JS, IIFE, never throws.
 */
(function () {
  "use strict";
  if (!window.WF || typeof WF.registerTemplate !== "function") return;
  if (!window.NodeCanvas || !NodeCanvas.esc) return;

  var esc = NodeCanvas.esc;
  var icon = function (name, size) {
    return window.BGIcon ? BGIcon(name, { size: size || 16 }) : "";
  };

  /* ---------------------------------------------------------------------- */
  /* the families                                                            */
  /* ---------------------------------------------------------------------- */
  /* Order is the order a project meets them: look first, then world, then the
     dimensional and asset families, then the things that land IN the game.
     `fam` is the seat colour the canvas already paints these nodes with — the
     audio band is the same green as an audio node, which is why the colour is
     worth anything at all. */

  var FAMILIES = [
    { id: "asset", label: "2D asset gen", icon: "art", fam: "var(--c-art)",
      note: "characters, sprites, sheets" },
    { id: "world", label: "World / background", icon: "background", fam: "var(--c-qa)",
      note: "stages, parallax, tilesets" },
    { id: "3d", label: "3D · Blender", icon: "model", fam: "var(--c-narrative)",
      note: "mesh, rig, glTF, sprites" },
    { id: "items", label: "Items · gear", icon: "props", fam: "var(--c-gameplay)",
      note: "one item, then the whole rack" },
    { id: "level", label: "Levels", icon: "tileset", fam: "var(--accent)",
      note: "layout as data, then as a scene" },
    { id: "audio", label: "Music · voice", icon: "audio", fam: "var(--c-audio)",
      note: "tracks and spoken lines" },
    { id: "video", label: "Cutscenes · video", icon: "cinematic", fam: "var(--c-cinematic)",
      note: "storyboard, shots, the cut" },
    { id: "engine", label: "Engine · Godot", icon: "tech", fam: "var(--c-tech)",
      note: "into the game, with proof" },
    { id: "agent", label: "Agents", icon: "agents", fam: "var(--c-director)",
      note: "seats doing the work" },
  ];

  /* A template's own mark. Falls back to its family's, so a template registered
     by a file that has never heard of this one still gets real geometry rather
     than the dashed missing-icon box. */
  var TPL_ICON = {
    "tpl.concept": "concept", "tpl.anchor": "anchor", "tpl.animation": "animation",
    "tpl.compare": "variants", "tpl.world": "background", "tpl.3dmodel": "model",
    "tpl.3dsprite": "sprites", "tpl.editanim": "edit", "tpl.fixgameplay": "gameplay",
    "tpl.newchar": "art",
    "tpl.cutscene": "cinematic", "tpl.music": "audio", "tpl.voice": "waveform",
    "tpl.item": "props", "tpl.level": "tileset", "tpl.engine": "tech",
    "tpl.scenesurgery": "outline",
  };

  /* ---------------------------------------------------------------------- */
  /* money                                                                   */
  /* ---------------------------------------------------------------------- */




  /* ---------------------------------------------------------------------- */
  /* the library render                                                      */
  /* ---------------------------------------------------------------------- */
  /* REPLACED, not wrapped. wf.js walks its own CATS constant, which is a
     closure and cannot be extended from here - a template in an unlisted
     category registers fine and then never appears, which is the worst of the
     two failures available. Appending the missing sections after the original
     render was the other option and it produces two visual systems on one
     screen: the old bare grid above, panels below. One render, one system. */

  WF._renderLibrary = function () {
    var body = document.getElementById("wf-lib-body");
    if (!body) return;
    var self = this;

    var byCat = {};
    (this.templates || []).forEach(function (t) {
      (byCat[t.category] = byCat[t.category] || []).push(t);
    });

    function card(t, fam) {
      var mark = TPL_ICON[t.id] || fam.icon;
      return '<button class="wf-tpl" style="--fam:' + fam.fam + '" '
        + 'onclick="WF.openTemplate(\'' + esc(t.id) + '\')">'
        + '<span class="wf-tpl-i">' + icon(mark, 16) + "</span>"
        + '<span class="wf-tpl-b"><span class="wf-tpl-t">' + esc(t.name) + "</span>"
        + '<span class="wf-tpl-h">' + esc(t.hint || "template") + "</span></span>"
        + "</button>";
    }

    function panel(fam, inner, count) {
      return '<section class="spanel k-list wf-fam" style="--fam:' + fam.fam + '">'
        + '<div class="sec-h"><span class="wf-fam-i">' + icon(fam.icon, 18) + "</span>"
        + '<h4 class="sec-t">' + esc(fam.label) + "</h4>"
        + '<span class="sec-n">' + count + "</span>"
        + '<span class="wf-fam-n">' + esc(fam.note || "") + "</span></div>"
        + inner + "</section>";
    }

    var html = "";
    var shown = {};
    FAMILIES.forEach(function (fam) {
      var ts = byCat[fam.id] || [];
      if (!ts.length) return;
      shown[fam.id] = true;
      html += panel(fam, ts.map(function (t) { return card(t, fam); }).join(""), ts.length);
    });

    /* A category nobody declared a family for. It still gets a panel rather than
       vanishing: a template that registered and cannot be seen is indis-
       tinguishable from one that was never written. */
    var OTHER = { id: "other", label: "Other", icon: "note", fam: "var(--text-3)", note: "" };
    Object.keys(byCat).forEach(function (cat) {
      if (shown[cat]) return;
      var ts = byCat[cat];
      var fam = { id: cat, label: cat, icon: OTHER.icon, fam: OTHER.fam, note: "" };
      html += panel(fam, ts.map(function (t) { return card(t, fam); }).join(""), ts.length);
    });

    /* Saved workflows. A refused READ and an empty library look identical on
       screen and mean opposite things, so the failure keeps its own sentence -
       the same distinction wf.js's own render drew, kept here deliberately. */
    var savedFam = { id: "saved", label: "Your saved workflows", icon: "sheet",
                     fam: "var(--text-3)", note: "yours, not shipped" };
    if (this._savedError) {
      html += panel(savedFam,
        '<div class="wf-warn">could not read your saved workflows - '
        + esc(this._savedError) + ". They are still on the server; this is a read "
        + "that failed, not an empty library.</div>", "!");
    } else if ((this._saved || []).length) {
      html += panel(savedFam, this._saved.map(function (s) {
        return '<button class="wf-tpl" style="--fam:' + savedFam.fam + '" '
          + 'onclick="WF.openSaved(\'' + esc(s.id) + '\')">'
          + '<span class="wf-tpl-i">' + icon("sheet", 16) + "</span>"
          + '<span class="wf-tpl-b"><span class="wf-tpl-t">' + esc(s.name) + "</span>"
          + '<span class="wf-tpl-h">' + esc(s.category || "workflow") + " · "
          + (s.stepCount || 0) + " steps</span></span>"
          + '<span class="wf-tpl-x" title="delete this saved workflow" '
          + 'onclick="event.stopPropagation();WF.deleteSaved(\'' + esc(s.id) + '\')">✕</span>'
          + "</button>";
      }).join(""), this._saved.length);
    }

    body.innerHTML = html
      ? '<div class="wf-lib-grid">' + html + "</div>"
      : '<div class="empty">no templates registered</div>';
    // Nothing here uses data-icon, but a later contributor's card might.
    try { if (window.BGIcon && BGIcon.upgrade) BGIcon.upgrade(body); } catch (e) {}
    void self;
  };

  /* ---------------------------------------------------------------------- */
  /* templates                                                               */
  /* ---------------------------------------------------------------------- */
  /* Every config key below is a `field` in bgate_core/wfnodes.py's REGISTRY.
     Where a value is "{input}" that is the engine's own interpolation - a card
     string containing braces is composed against the wire before the tool is
     called (wfnodes.interpolate), which is how a value one step produced
     becomes the NEXT step's argument. */

  var X = function (i) { return 60 + i * 250; };
  var Y = 150;

  /* ---- video: the whole cutscene loop ---------------------------------- */
  /* storyboard first because it is the CHEAP half: six stills tell you the cut
     is wrong for the price of one second of video. The gate sits between the
     stills and the shots on purpose - it is the last free place to stop. */
  WF.registerTemplate({
    id: "tpl.cutscene", name: "Cutscene", category: "video", glyph: "▷",
    hint: "storyboard → approve → shots → cut → deliver",
    build: function () {
      return {
        nodes: [
          { id: "task", type: "input.task", x: X(0), y: Y,
            config: { text: "Cold open: the harvest towers come online at dusk and the crew watches from the ridge." } },
          { id: "board", type: "tool.storyboard.auto", x: X(1), y: Y,
            config: { name: "cold_open", frames: 6, style: "painterly",
                      aspect_ratio: "16:9", quality: "low" } },
          { id: "gate", type: "control.gate", x: X(2), y: Y },
          { id: "plan", type: "tool.cinematic.plan", x: X(3), y: Y,
            config: { name: "cold_open", style: "cinematic", aspect_ratio: "16:9",
                      resolution: "720p",
                      shots: '[{"action":"wide on the harvest towers at dusk, lights coming up","camera":"slow push in","duration":6},'
                           + '{"action":"the crew watching from the ridge","camera":"low angle medium","duration":5}]' } },
          { id: "shot", type: "tool.cinematic.shot", x: X(4), y: Y,
            config: { name: "cold_open", idx: 0 } },
          { id: "cut", type: "tool.cinematic.assemble", x: X(5), y: Y,
            config: { name: "cold_open", quality: 6 } },
          { id: "ship", type: "tool.cinematic.deliver", x: X(6), y: Y,
            config: { name: "cold_open" } },
        ],
        edges: [
          { from: ["task", "o"], to: ["board", "in"] },
          { from: ["board", "out"], to: ["gate", "i"] },
          { from: ["gate", "o"], to: ["plan", "in"] },
          { from: ["plan", "out"], to: ["shot", "in"] },
          { from: ["shot", "out"], to: ["cut", "in"] },
          { from: ["cut", "out"], to: ["ship", "in"] },
        ],
      };
    },
  });

  /* ---- audio: a track, picked by a human, installed -------------------- */
  /* The options node is wired INTO the paid one rather than left dangling: it
     is the free answer to "is a music key live on this build", and having it
     upstream means the run tells you that before it spends anything. The format
     node is the glue - it composes the provider prompt out of the task text, so
     the thing you typed once is not retyped into the music card. */
  WF.registerTemplate({
    id: "tpl.music", name: "Music track", category: "audio", glyph: "♪",
    hint: "prompt → generate → pick → keep → install",
    build: function () {
      return {
        nodes: [
          { id: "task", type: "input.task", x: X(0), y: Y + 90,
            config: { text: "A slow, hopeful loop for the hub screen" } },
          { id: "opt", type: "tool.music.options", x: X(0), y: Y - 90, config: {} },
          { id: "fmt", type: "flow.format", x: X(1), y: Y + 90,
            config: { template: "{input} - looping game music, no vocals, clean loop point" } },
          { id: "gen", type: "tool.music.generate", x: X(2), y: Y,
            config: { name: "hub_theme", instrumental: true, duration: 60 } },
          { id: "cands", type: "tool.music.candidates", x: X(3), y: Y,
            config: { logical_name: "hub_theme", limit: 20 } },
          { id: "pick", type: "control.select", x: X(4), y: Y },
          { id: "keep", type: "tool.music.keep", x: X(5), y: Y,
            config: { artifact_id: "{artifact_id}", note: "picked in Studio" } },
          { id: "install", type: "tool.music.install", x: X(6), y: Y,
            config: { artifact_id: "{artifact_id}" } },
        ],
        // fmt BEFORE opt into the paid node: the first parent carrying text is
        // the one whose text the node reads, and the prompt must be the format
        // node's, never the options payload.
        edges: [
          { from: ["task", "o"], to: ["fmt", "in"] },
          { from: ["fmt", "out"], to: ["gen", "in"] },
          { from: ["opt", "out"], to: ["gen", "in"] },
          { from: ["gen", "out"], to: ["cands", "in"] },
          { from: ["cands", "out"], to: ["pick", "i"] },
          { from: ["pick", "o"], to: ["keep", "in"] },
          { from: ["keep", "out"], to: ["install", "in"] },
        ],
      };
    },
  });

  /* ---- audio: a spoken line that ends up in the game ------------------- */
  WF.registerTemplate({
    id: "tpl.voice", name: "Voice line", category: "audio", glyph: "◍",
    hint: "line → speak → import → engine check",
    build: function () {
      return {
        nodes: [
          { id: "task", type: "input.task", x: X(0), y: Y + 80,
            config: { text: "Towers are live. Get clear of the rails." } },
          { id: "vst", type: "tool.voice.status", x: X(0), y: Y - 80, config: {} },
          { id: "say", type: "tool.voice.speak", x: X(1), y: Y, config: {} },
          { id: "imp", type: "tool.godot.import", x: X(2), y: Y,
            config: { dest_rel: "assets/audio" } },
          { id: "chk", type: "tool.godot.check", x: X(3), y: Y, config: {} },
        ],
        edges: [
          { from: ["task", "o"], to: ["say", "in"] },
          { from: ["vst", "out"], to: ["say", "in"] },
          { from: ["say", "out"], to: ["imp", "in"] },
          { from: ["imp", "out"], to: ["chk", "in"] },
        ],
      };
    },
  });

  /* ---- items: one piece of gear, then the whole rack ------------------- */
  /* item_variants multiplies the bill by materials × tiers, so the human pick
     sits between the grid and everything that writes into the game. */
  WF.registerTemplate({
    id: "tpl.item", name: "Item + variants", category: "items", glyph: "⬗",
    hint: "descriptor → item → variants → SpriteFrames → Godot",
    build: function () {
      var desc = "brass and glass surveyor's lantern, warm lit, readable at 64px";
      return {
        nodes: [
          { id: "task", type: "input.task", x: X(0), y: Y + 90,
            config: { text: "A brass-and-glass surveyor's lantern for the harvest crew" } },
          { id: "cls", type: "tool.item.classes", x: X(0), y: Y - 90, config: {} },
          { id: "fmt", type: "flow.format", x: X(1), y: Y + 90,
            config: { template: "{input} - single object, centred, transparent background, on the project palette" } },
          { id: "gen", type: "tool.item.generate", x: X(2), y: Y,
            config: { item_class: "prop", name: "surveyor_lantern", descriptor: desc,
                      quality: "medium", material: "brass", tier: "common" } },
          { id: "var", type: "tool.item.variants", x: X(3), y: Y,
            config: { item_class: "prop", base_name: "surveyor_lantern", descriptor: desc,
                      materials: "brass,iron,glass", tiers: "common,rare",
                      quality: "medium", limit: 6 } },
          { id: "pick", type: "control.select", x: X(4), y: Y },
          { id: "frames", type: "tool.item.spriteframes", x: X(5), y: Y,
            config: { name: "surveyor_lantern", res_dir: "assets/gear" } },
          { id: "imp", type: "tool.godot.import", x: X(6), y: Y,
            config: { dest_rel: "assets/gear" } },
        ],
        edges: [
          { from: ["task", "o"], to: ["fmt", "in"] },
          { from: ["fmt", "out"], to: ["gen", "in"] },
          { from: ["cls", "out"], to: ["gen", "in"] },
          { from: ["gen", "out"], to: ["var", "in"] },
          { from: ["var", "out"], to: ["pick", "i"] },
          { from: ["pick", "o"], to: ["frames", "in"] },
          { from: ["frames", "out"], to: ["imp", "in"] },
        ],
      };
    },
  });

  /* ---- levels: free until it writes ------------------------------------ */
  /* level_plan is local and costs nothing, so the loop is "re-seed until the
     layout is right, THEN generate once". dry_run is left ON: the first press
     of run on a node that writes into someone's game should show the edit. */
  WF.registerTemplate({
    id: "tpl.level", name: "Level layout → scene", category: "level", glyph: "▦",
    hint: "plan → approve → TileMap scene → screenshot",
    /* `facts` is what the project actually contains, fetched once by the
       library. This template used to hardcode res://assets/tiles/main.tres --
       a path no project has ever had, so the card whose entire promise is
       "this generates a level" died on its generate node every time anyone
       opened it. level_generate was right to refuse; the card was lying.

       So: draw the first real TileSet, and take its FIRST SOURCE ID rather
       than assuming 0. Source ids are ids, not indexes, and a tileset with one
       source is free to call it 3. With no tileset in the project the field is
       left empty on purpose -- an empty required field reads as "you owe me
       this", which is true, and a fabricated one does not.

       The wall layout is chosen the same way, off `draws`, which the server
       computes against the coordinates the .tres actually defines. Defaulting
       to blob47 is what a hand-written card does, and blob47 needs 47 tiles in
       one row-major block: the owner's project has 55 sources and not one of
       them can draw it, so the honest default here is whatever this atlas
       supports. `columns` comes from the source's own extent rather than the
       8 that happened to be the argument default. */
    build: function (facts) {
      var sets = (facts && facts.tilesets) || [];
      var ts = sets[0] || null;
      /* Richest drawable source, not the lowest id -- source 0 is very often a
         single-tile placeholder sitting in front of the real sheets. */
      var draws = ((ts && ts.draws) || []).filter(function (d) {
        return d.layouts && d.layouts.length;
      }).sort(function (a, b) { return b.tiles - a.tiles; });
      var d = draws[0] || null;
      var src = d ? d.source
        : (ts && ts.sources && ts.sources.length ? ts.sources[0] : 0);
      return {
        nodes: [
          { id: "task", type: "input.task", x: X(0), y: Y,
            config: { text: "A three-room service level under the harvest towers" } },
          { id: "plan", type: "tool.level.plan", x: X(1), y: Y,
            config: { width: 48, height: 32, seed: 7, min_leaf: 10, min_room: 4,
                      max_depth: 5, corridor_width: 1 } },
          { id: "gate", type: "control.gate", x: X(2), y: Y },
          { id: "gen", type: "tool.level.generate", x: X(3), y: Y,
            config: { scene: "res://scenes/Level01.tscn",
                      tileset: ts ? ts.res : "",
                      floor_source: src, wall_source: src,
                      floor_atlas_x: d ? d.atlas_x : 0,
                      floor_atlas_y: d ? d.atlas_y : 0,
                      wall_atlas_x: d ? d.atlas_x : 0,
                      wall_atlas_y: d ? d.atlas_y : 0,
                      wall_columns: d ? d.columns : 8,
                      width: 48, height: 32, seed: 7,
                      wall_layout: d ? d.layouts[0] : "blob47",
                      create: true, dry_run: true } },
          { id: "chk", type: "tool.godot.check", x: X(4), y: Y, config: {} },
          { id: "shot", type: "tool.godot.screenshot", x: X(5), y: Y,
            config: { scene: "res://scenes/Level01.tscn", at: 1.0, label: "level01" } },
        ],
        edges: [
          { from: ["task", "o"], to: ["plan", "in"] },
          { from: ["plan", "out"], to: ["gate", "i"] },
          { from: ["gate", "o"], to: ["gen", "in"] },
          { from: ["gen", "out"], to: ["chk", "in"] },
          { from: ["chk", "out"], to: ["shot", "in"] },
        ],
      };
    },
  });

  /* ---- engine: generate, land it, prove it landed ---------------------- */
  /* The branch is the point. godot_status answers facts, not files, so its
     payload arrives downstream as JSON text - and a branch that reads
     '"available": true' out of it stops the graph before anything paid runs on
     a machine with no engine on it. That is a condition evaluated on the wire;
     no code is evaluated, because a saved workflow is a document people share. */
  WF.registerTemplate({
    id: "tpl.engine", name: "Generate → into the game", category: "engine", glyph: "◈",
    hint: "engine check → branch → image → import → screenshot",
    build: function () {
      return {
        nodes: [
          { id: "st", type: "tool.godot.status", x: X(0), y: Y - 100, config: {} },
          { id: "br", type: "flow.branch", x: X(1), y: Y - 100,
            config: { left: "{input}", test: "contains", right: '"available": true',
                      stop_when_false: true } },
          { id: "task", type: "input.task", x: X(0), y: Y + 100,
            config: { text: "A rusted iron hatch cover seen from above, 256px, transparent background" } },
          { id: "gen", type: "tool.image.generate", x: X(1), y: Y + 100,
            config: { filename: "hatch_cover.png", size: "1024x1024",
                      quality: "medium", transparent: true } },
          { id: "imp", type: "tool.godot.import", x: X(2), y: Y,
            config: { dest_rel: "assets/generated" } },
          { id: "chk", type: "tool.godot.check", x: X(3), y: Y, config: {} },
          { id: "shot", type: "tool.godot.screenshot", x: X(4), y: Y,
            config: { at: 1.0, label: "hatch_cover" } },
        ],
        edges: [
          { from: ["st", "out"], to: ["br", "in"] },
          { from: ["task", "o"], to: ["gen", "in"] },
          { from: ["gen", "out"], to: ["imp", "in"] },
          { from: ["br", "out"], to: ["imp", "in"] },
          { from: ["imp", "out"], to: ["chk", "in"] },
          { from: ["chk", "out"], to: ["shot", "in"] },
        ],
      };
    },
  });

  /* ---- engine: scene surgery, no provider anywhere in it --------------- */
  /* The format node builds "res://scenes/Main.tscn" out of one word typed in
     the task, and BOTH the read and the edit take their scene from it as
     "{input}". That is the glue's whole reason to exist: one value, composed
     once, becoming an argument to two different tools. */
  WF.registerTemplate({
    id: "tpl.scenesurgery", name: "Scene surgery", category: "engine", glyph: "⊞",
    hint: "read the tree → add a node (dry run) → screenshot",
    build: function () {
      return {
        nodes: [
          { id: "task", type: "input.task", x: X(0), y: Y,
            config: { text: "Main" } },
          { id: "fmt", type: "flow.format", x: X(1), y: Y,
            config: { template: "res://scenes/{input}.tscn" } },
          { id: "out", type: "tool.scene.outline", x: X(2), y: Y - 90,
            config: { scene: "{input}", limit: 120, properties: false } },
          { id: "add", type: "tool.scene.node_add", x: X(3), y: Y,
            config: { scene: "{input}", name: "CrewLiftMarker", node_type: "Marker2D",
                      parent: ".", dry_run: true } },
          { id: "chk", type: "tool.godot.check", x: X(4), y: Y, config: {} },
          { id: "shot", type: "tool.godot.screenshot", x: X(5), y: Y,
            config: { at: 1.0, label: "crew_lift_marker" } },
        ],
        // fmt is listed before out on the way into add, so the scene path is
        // what add reads as {input} and not the outline's JSON payload.
        edges: [
          { from: ["task", "o"], to: ["fmt", "in"] },
          { from: ["fmt", "out"], to: ["out", "in"] },
          { from: ["fmt", "out"], to: ["add", "in"] },
          { from: ["out", "out"], to: ["add", "in"] },
          { from: ["add", "out"], to: ["chk", "in"] },
          { from: ["chk", "out"], to: ["shot", "in"] },
        ],
      };
    },
  });

  /* ---------------------------------------------------------------------- */
  /* styles                                                                  */
  /* ---------------------------------------------------------------------- */
  /* Tokens only, so light and orbit come for free, and the radius ladder is
     the sharpened one - nothing here softens a corner the design pass tightened.
     .spanel and .sec-h come from app.css and are NOT redefined here; this block
     only adds the family tint, the panel tiling, and the card row. */

  if (!document.getElementById("wf-lib-style")) {
    var s = document.createElement("style");
    s.id = "wf-lib-style";
    s.textContent = ""
      + ".wf-lib-grid{display:grid;gap:var(--s-6);align-items:start;"
      + "grid-template-columns:repeat(auto-fill,minmax(316px,1fr))}"
      /* The kind rule is app.css's; only its colour is the family's, so a panel
         still reads as the same object every other panel in this app is. */
      + ".spanel.wf-fam{border-left-color:var(--fam,var(--accent))}"
      + ".wf-fam .sec-h{gap:var(--s-3)}"
      + ".wf-fam-i{display:flex;flex:none;color:var(--fam,var(--text-2))}"
      + ".wf-fam-n{flex:1;min-width:0;text-align:right;font-family:var(--mono);"
      + "font-size:var(--fs-3xs);color:var(--text-3);overflow:hidden;"
      + "text-overflow:ellipsis;white-space:nowrap}"
      + ".wf-tpl{display:flex;align-items:flex-start;gap:var(--s-4);width:100%;"
      + "text-align:left;padding:var(--s-4) var(--s-4);margin-bottom:var(--s-3);"
      + "background:var(--surface-1);border:1px solid var(--line);"
      + "border-radius:var(--r-sm);color:var(--text);font:inherit;cursor:pointer;"
      + "position:relative;transition:background var(--dur-fast) var(--ease),"
      + "border-color var(--dur-fast) var(--ease)}"
      + ".wf-tpl:last-child{margin-bottom:0}"
      + ".wf-tpl:hover{background:var(--surface-3);border-color:var(--line-strong)}"
      + ".wf-tpl:focus-visible{outline:2px solid var(--accent);outline-offset:2px}"
      + ".wf-tpl-i{flex:none;display:flex;margin-top:1px;color:var(--fam,var(--text-2))}"
      + ".wf-tpl-b{flex:1;min-width:0;display:flex;flex-direction:column;gap:var(--s-1)}"
      + ".wf-tpl-t{font-size:var(--fs-sm);font-weight:var(--fw-semi);color:var(--text);"
      + "line-height:var(--lh-tight,1.25)}"
      + ".wf-tpl-h{font-family:var(--mono);font-size:var(--fs-3xs);color:var(--text-3);"
      + "line-height:1.5;overflow-wrap:anywhere}"
      /* Money, before the card is opened. Quiet when it is free; the warn ramp
         when it is not, which is the same ramp .sec-n.warn already uses. */
      + ".wf-tpl-cost{flex:none;align-self:center;font-family:var(--mono);"
      + "font-size:var(--fs-3xs);line-height:1;padding:var(--s-2) var(--s-3);"
      + "border-radius:var(--r-full);border:1px solid var(--line);"
      + "background:var(--surface-2);color:var(--text-3);white-space:nowrap}"
      + ".wf-tpl-cost.paid{color:var(--warn);border-color:var(--warn-line);"
      + "background:var(--warn-soft)}"
      + ".wf-tpl-x{position:absolute;top:var(--s-2);right:var(--s-2);"
      + "font-size:var(--fs-2xs);color:var(--text-3);line-height:1;padding:var(--s-2)}"
      + ".wf-tpl-x:hover{color:var(--bad)}";
    document.head.appendChild(s);
  }

  // The library may already be on screen when this file parses (Studio holds
  // the view between tab switches). Repaint it rather than waiting for the next
  // navigation, or the panels appear only to whoever leaves and comes back.
  try { if (document.getElementById("wf-lib-body")) WF._renderLibrary(); } catch (e) {}
})();
