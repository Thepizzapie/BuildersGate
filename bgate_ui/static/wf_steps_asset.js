/* wf_steps_asset.js — the 2D asset-generation workflow steps + templates.
 *
 * FLAGSHIP of the Builders Gate workflow builder: art consistency + per-frame
 * variants. Contributes step types under the "2D asset gen" (asset) and
 * "Control / QA" (control) palette categories, plus three starter templates.
 *
 * Registers against window.WF (loaded by wf.js). Persists config inline via
 * WF.set('<nodeId>','field',value). Everything is wrapped so nothing thrown
 * here can break the builder. No new deps, vanilla JS.
 */
(function () {
  if (!window.WF || typeof WF.registerStep !== "function") return;
  if (!window.NodeCanvas || !NodeCanvas.w) return;

  // The node IS the instrument: parameters live on the card, in real widgets,
  // and the inspector keeps the prose that explains them.
  const w = NodeCanvas.w;

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Show real assets in the nodes: a reference thumbnail (hides on 404).
  // Resolved through WF.refImg — pins are versioned files whose suffix is
  // whatever was pinned, so assuming "<name>.png" rendered jpg/webp anchors
  // blank and pointed at a revision that need not exist.
  // A real picture on the node, not a note describing one. WF.refRel resolves
  // the pinned revision + real suffix; an unresolved name renders the empty
  // plate rather than a broken <img>.
  const refImage = (name, empty) => {
    const rel = (name && WF.refRel) ? WF.refRel(name) : "";
    return w.image(rel ? "/api/preview?rel=" + encodeURIComponent(rel) : "",
      { alt: name || "", caption: name || "", empty: empty || "no reference" });
  };
  // Walk the graph backwards from a node to the character on its upstream anchor,
  // so animation/edit nodes can preview the character they're working from.
  function upstreamCharacter(node) {
    try {
      const nc = window.WF && WF._nc; if (!nc) return "";
      const seen = new Set(); const stack = [node.id];
      while (stack.length) {
        const id = stack.pop(); if (seen.has(id)) continue; seen.add(id);
        const n = nc.nodes.get(id);
        if (n && n.type === "art.anchor" && n.config && n.config.character) return n.config.character;
        (nc.edges || []).forEach(e => { if (e.to && e.to[0] === id) stack.push(e.from[0]); });
      }
    } catch (e) {}
    return "";
  }

  /* -- character reference cache (from /api/refs) --------------------------- */
  // config() is synchronous; WF owns the one ref registry (versioned paths and
  // all) and we read the names out of it, refreshing fire-and-forget so the
  // <select> is populated on the next open.
  let CHARS = [];
  function refreshChars() {
    return WF.refsLoad().then(list => {
      CHARS = (list || []).map(x => (x && x.name)).filter(Boolean);
      return CHARS;
    }).catch(() => CHARS);
  }
  refreshChars();

  // <select> of known refs + a "type your own" escape, bound to `field`.
  function charSelect(node, field) {
    const cur = (node.config && node.config[field]) || "";
    const known = CHARS.slice();
    const inList = cur && known.includes(cur);
    const opts = [`<option value="">— pick a character —</option>`]
      .concat(known.map(n => `<option value="${esc(n)}"${n === cur ? " selected" : ""}>${esc(n)}</option>`))
      .concat([`<option value="__custom__"${cur && !inList ? " selected" : ""}>+ custom (type below)…</option>`])
      .join("");
    refreshChars();  // freshen for next render
    return `<select style="width:100%" onchange="if(this.value!=='__custom__')WF.set('${node.id}','${field}',this.value)">${opts}</select>
      <input type="text" style="width:100%;margin-top:6px" placeholder="or type a character name"
        value="${esc(cur)}" oninput="WF.set('${node.id}','${field}',this.value)">`;
  }

  // Inspector-only row (the node holds the widgets now; this is for the rare
  // field that does not deserve card space).
  const numRow = (node, field, label, dflt, min, max) =>
    `<div class="wf-row"><label>${esc(label)}</label><input type="number" style="max-width:76px" min="${min}" max="${max}" value="${(node.config && node.config[field]) != null ? node.config[field] : dflt}" oninput="WF.set('${node.id}','${field}',Math.max(${min},Math.min(${max},+this.value||${dflt})))"></div>`;

  // options for a <select> widget: the known pinned characters plus whatever
  // this node already names, so a custom value is never silently dropped.
  const charOptions = (cur) => {
    const seen = [""].concat(CHARS.slice());
    if (cur && seen.indexOf(cur) === -1) seen.push(cur);
    refreshChars();   // freshen for the next render
    return seen.map(v => ({ value: v, label: v || "— pick a character —" }));
  };

  // the workflow's Task text, so briefs can reference it
  function taskText(wf) {
    const t = ((wf && wf.nodes) || []).find(n => n.type === "input.task");
    const v = t && t.config && t.config.text;
    return (v && String(v).trim()) || "the workflow's task";
  }
  const cfg = (n, f, d) => (n.config && n.config[f] != null && n.config[f] !== "") ? n.config[f] : d;
  // frame count = explicit list length, else the numeric `frames`
  function frameCount(n) {
    const fl = (n.config && n.config.frameList || "").trim();
    if (fl) return fl.split(",").map(s => s.trim()).filter(Boolean).length || 1;
    return +cfg(n, "frames", 6) || 6;
  }

  /* ===================================================================== */
  /* STEP: art.concept — concept art fan-out                               */
  /* ===================================================================== */
  WF.registerStep({
    type: "art.concept", category: "asset", label: "Concept art", glyph: "✎", accent: "var(--c-art)",
    defaults: { style: "painterly key art", count: 4 },
    ports() { return { in: [{ id: "i", label: "task", type: "task" }], out: [{ id: "o", label: "concepts", type: "image" }] }; },
    body(n) {
      return w.text(n, "style", { label: "Style", placeholder: "painterly key art, 16-bit pixel" })
        + w.number(n, "count", { label: "Variants", min: 1, max: 6, value: 4 });
    },
    config() {
      return `<div class="wf-insp-p">Explore the look before committing. Generates several concept images in one style for the task — style and variant count are on the node.</div>`;
    },
    agentSeat: "art",
    toBrief(n, wf) {
      return `Generate ${+cfg(n, "count", 4)} concept images in ${cfg(n, "style", "a cohesive style")} for: ${taskText(wf)}.`;
    },
  });

  /* ===================================================================== */
  /* STEP: art.anchor — THE canonical character anchor                     */
  /* ===================================================================== */
  WF.registerStep({
    type: "art.anchor", category: "asset", label: "Character anchor", glyph: "▦", accent: "var(--c-art)",
    defaults: { character: "", pose: "idle", strictness: "high", transparent: true },
    ports() {
      return { in: [{ id: "task", label: "task", type: "task" }, { id: "ref", label: "ref", type: "ref" }],
        out: [{ id: "o", label: "anchor", type: "image" }] };
    },
    body(n) {
      const c = cfg(n, "character", "");
      return refImage(c, "pick a character")
        + w.select(n, "character", { label: "Character", options: charOptions(c) })
        + w.text(n, "pose", { label: "Pose", placeholder: "idle" })
        + w.select(n, "strictness", { label: "Consistency", options: ["low", "med", "high"], value: "high" })
        + w.toggle(n, "transparent", { label: "Alpha", value: true });
    },
    config(n) {
      return `<div class="wf-insp-p">The <b>canonical idle frame</b>. Everything downstream anchors to this — animation frames, edits and QA all reference it, so lock it before generating anything else.</div>`
        + `<div class="wf-row" style="flex-direction:column;align-items:stretch"><label style="margin-bottom:5px">Character (or type a name the pin list does not have)</label>${charSelect(n, "character")}</div>`
        + `<div class="wf-b-note" style="margin-top:8px">Higher strictness rejects more off-model output — use <b>high</b> for hero characters.</div>`;
    },
    agentSeat: "art",
    toBrief(n, wf) {
      const c = cfg(n, "character", "the character");
      return `Produce the on-model canonical anchor frame for ${c} (${cfg(n, "pose", "idle")} pose${cfg(n, "transparent", true) ? ", transparent background" : ""}) that every animation frame will anchor to. Hold consistency at ${cfg(n, "strictness", "high")} strictness. Task context: ${taskText(wf)}.`;
    },
  });

  /* ===================================================================== */
  /* STEP: art.animation — RICHEST: per-frame variants, anchor-conditioned */
  /* ===================================================================== */
  WF.registerStep({
    type: "art.animation", category: "asset", label: "Animation frames", glyph: "◈", accent: "var(--c-art)",
    defaults: { frameList: "windup,strike,recover", frames: 6, variantsPerFrame: 2, conditioning: "anchor+prev", fps: 12 },
    ports() { return { in: [{ id: "anchor", label: "anchor", type: "image" }], out: [{ id: "o", label: "frames", type: "frames" }] }; },
    body(n) {
      const fl = (n.config && n.config.frameList || "").trim();
      return refImage(upstreamCharacter(n), "no upstream anchor")
        + w.text(n, "frameList", { label: "Frames", placeholder: "windup,strike,recover" })
        + (fl ? w.note(`${frameCount(n)} named frames`)
              : w.number(n, "frames", { label: "Count", min: 1, max: 24, value: 6 }))
        + w.number(n, "variantsPerFrame", { label: "Variants", min: 1, max: 4, value: 2 })
        + w.select(n, "conditioning", { label: "Condition", options: ["anchor", "anchor+prev"], value: "anchor+prev" });
    },
    config(n) {
      return `<div class="wf-insp-p">The flagship step. Each frame is conditioned on the anchor (and optionally the previous frame) so the character stays on-model across the whole animation. Multiple <b>variants per frame</b> are generated so a human can pick the best one.</div>`
        + `<div class="wf-insp-p">Name the frames on the node (comma-separated) or clear that field and set a plain count.</div>`
        + numRow(n, "fps", "Playback fps", 12, 1, 60)
        + `<div class="wf-b-note" style="margin-top:8px">Total candidates: <b>${frameCount(n) * (+cfg(n, "variantsPerFrame", 2) || 1)}</b> images (${frameCount(n)} frames × ${+cfg(n, "variantsPerFrame", 2)} variants).</div>`;
    },
    agentSeat: "art",
    toBrief(n, wf) {
      const fl = (n.config && n.config.frameList || "").trim();
      const frames = fl ? fl : `${+cfg(n, "frames", 6)} frames`;
      const cond = cfg(n, "conditioning", "anchor+prev") === "anchor+prev"
        ? "conditioned on BOTH the character anchor and the previous frame"
        : "conditioned on the character anchor";
      return `Generate the animation frames (${frames}) at ${+cfg(n, "fps", 12)}fps. Every frame must be ${cond} so the character stays perfectly on-model — never re-imagine the character. Produce ${+cfg(n, "variantsPerFrame", 2)} variant(s) of EACH frame so the best can be selected. Task: ${taskText(wf)}.`;
    },
  });

  /* ===================================================================== */
  /* STEP: art.edit — edit an existing frame                               */
  /* ===================================================================== */
  WF.registerStep({
    type: "art.edit", category: "asset", label: "Edit frame", glyph: "✂", accent: "var(--c-art)",
    defaults: { instruction: "", keepAnchor: true },
    ports() { return { in: [{ id: "frame", label: "frame", type: "image" }], out: [{ id: "o", label: "edited", type: "image" }] }; },
    body(n) {
      return refImage(upstreamCharacter(n), "no upstream anchor")
        + w.text(n, "instruction", { label: "Edit", rows: 3, placeholder: "straighten the sword arm, brighten the rim light" })
        + w.toggle(n, "keepAnchor", { label: "On anchor", value: true });
    },
    config() {
      return `<div class="wf-insp-p">Targeted edit of one incoming frame — fix a hand, recolor, adjust silhouette — without regenerating from scratch. Write the instruction on the node; <b>On anchor</b> holds the edit to the canonical design instead of letting the model re-imagine the character.</div>`;
    },
    agentSeat: "art",
    toBrief(n, wf) {
      return `Edit the incoming frame: ${cfg(n, "instruction", "apply the requested change")}.${cfg(n, "keepAnchor", true) ? " Keep it on-model against the character anchor — do not drift from the canonical design." : ""} Task: ${taskText(wf)}.`;
    },
  });

  /* ===================================================================== */
  /* STEP: control.consistency — INDEPENDENT art-QA (qa seat, not art!)    */
  /* ===================================================================== */
  WF.registerStep({
    type: "control.consistency", category: "control", label: "Consistency check", glyph: "◎", accent: "var(--c-qa)",
    kind: "consistency",
    defaults: { threshold: 80 },
    /* Deliberately untyped: anything generated can be graded — a frame, a sheet,
       a background, a render — and what comes out the far side is whatever went
       in, minus the rejects. */
    ports() { return { in: [{ id: "candidate", label: "candidate" }], out: [{ id: "o", label: "passed" }] }; },
    body(n) {
      return w.note("reject off-model output")
        + w.slider(n, "threshold", { label: "Floor %", min: 0, max: 100, step: 1, value: 80,
            hint: "enforced against the WORST recorded score" });
    },
    config(n) {
      return `<div class="wf-insp-p">An <b>independent</b> art-QA pass (run by the QA seat, not the artist) that checks each candidate frame against its anchor/reference. Independence is the point — the generator does not grade its own output.</div>`
        + `<div class="wf-b-note" style="margin-top:8px">The threshold is <b>enforced by the run</b>, not just written into the brief: when the reviewer finishes, its recorded scores (one per candidate, from <code>art_qa_verdict</code>) are compared to it and the <b>worst</b> one decides. Below the line the step fails and the whole run fails — one off-model frame is an off-model sheet. A review that records no score at all cannot pass either.</div>`;
    },
    agentSeat: "qa",
    toBrief(n, wf) {
      const t = +cfg(n, "threshold", 80);
      return `Independently review each incoming frame against its character anchor/reference. Score every candidate 0-100 on-model and record it with art_qa_verdict(artifact_id, verdict, score, reasons) — flag silhouette, palette, proportion and detail drift. The run enforces a ${t}% floor on the WORST score, so a score you do not record is a step that cannot pass. You are the QA seat, independent of the artist. Task: ${taskText(wf)}.`;
    },
  });

  /* ===================================================================== */
  /* STEP: control.variants — variant fan-out (option modifier, no seat)   */
  /* ===================================================================== */
  WF.registerStep({
    type: "control.variants", category: "control", label: "Variant fan-out", glyph: "⑃", accent: "var(--warn)",
    kind: "passive",
    defaults: { count: 3 },
    ports() { return { in: [{ id: "i", label: "in" }], out: [{ id: "o", label: "variants" }] }; },
    body(n) {
      return w.number(n, "count", { label: "Fan-out", min: 1, max: 8, value: 3 })
        + w.tag("no agent");
    },
    config() {
      return `<div class="wf-insp-p">Fan the upstream output into N parallel variants for selection. This is an option modifier — it does not run an agent of its own; it multiplies the step feeding it.</div>`;
    },
  });

  /* ===================================================================== */
  /* STEP: control.select — human picks the best variant (gate-like)       */
  /* ===================================================================== */
  WF.registerStep({
    type: "control.select", category: "control", label: "Select best", glyph: "☑", accent: "var(--warn)",
    kind: "gate",
    defaults: {},
    /* Untyped by design: whatever the upstream produced is what a human picks
       between, and the chosen one is the same kind of thing. */
    ports() { return { in: [{ id: "i", label: "candidates" }], out: [{ id: "o", label: "chosen" }] }; },
    body() { return w.note("blocks until a human picks") + w.tag("human gate"); },
    config() {
      return `<div class="wf-insp-p">A human-in-the-loop gate, and a real one: the run <b>stops here</b> — nothing downstream is queued — until a person approves it from the run bar (having picked their variant in the art workspace) or rejects it, which fails the run. An agent cannot open it. No options.</div>`;
    },
  });

  /* ===================================================================== */
  /* STEP: output.sheet — assemble sprite sheet (sink)                     */
  /* ===================================================================== */
  WF.registerStep({
    type: "output.sheet", category: "asset", label: "Sprite sheet", glyph: "▤", accent: "var(--c-art)",
    defaults: { fps: 12, layout: "horizontal" },
    ports() { return { in: [{ id: "frames", label: "frames", type: "frames" }], out: [{ id: "o", label: "sheet", type: "sheet" }] }; },
    body(n) {
      return w.select(n, "layout", { label: "Layout", options: ["horizontal", "vertical", "grid"], value: "horizontal" })
        + w.number(n, "fps", { label: "fps", min: 1, max: 60, value: 12 });
    },
    config() {
      return `<div class="wf-insp-p">Assemble the selected / approved frames into a single sprite sheet ready for import. Layout and playback rate are on the node.</div>`;
    },
    agentSeat: "art",
    toBrief(n, wf) {
      return `Stitch the selected/approved frames into a ${cfg(n, "layout", "horizontal")} sprite sheet at ${+cfg(n, "fps", 12)}fps, ready for engine import. Task: ${taskText(wf)}.`;
    },
  });

  /* ===================================================================== */
  /* STEP: output.rig — Rig → Godot (terminal sink)                        */
  /* ===================================================================== */
  WF.registerStep({
    type: "output.rig", category: "asset", label: "Rig → Godot", glyph: "⊹", accent: "var(--c-tech)",
    defaults: {},
    /* The importer takes whatever finished thing you hand it — a sheet, a
       glTF-backed asset, a single image — so its inlet stays untyped. */
    ports() { return { in: [{ id: "asset", label: "asset" }] }; },
    body() { return w.note("import & wire into Godot") + w.tag("terminal"); },
    config() {
      return `<div class="wf-insp-p">Terminal step: import the finished asset into the Godot project and wire it in (AnimatedSprite2D / SpriteFrames). Runs on the tech seat. No options.</div>`;
    },
    agentSeat: "tech",
    toBrief(n, wf) {
      return `Import the finished sprite asset into the Godot project and wire it in (SpriteFrames / AnimatedSprite2D), ready to use in-game. Task: ${taskText(wf)}.`;
    },
  });

  /* ===================================================================== */
  /* TEMPLATES                                                             */
  /* ===================================================================== */
  WF.registerTemplate({
    id: "tpl.concept", name: "Concept", category: "asset", glyph: "✎",
    hint: "explore the look — concepts → pick one",
    build() {
      return {
        nodes: [
          { id: "task", type: "input.task", x: 40, y: 140 },
          { id: "concept", type: "art.concept", x: 300, y: 140 },
          { id: "pick", type: "control.select", x: 560, y: 140 },
        ],
        edges: [
          { from: ["task", "o"], to: ["concept", "i"] },
          { from: ["concept", "o"], to: ["pick", "i"] },
        ],
      };
    },
  });

  WF.registerTemplate({
    id: "tpl.anchor", name: "Character anchor", category: "asset", glyph: "▦",
    hint: "lock the canonical, consistency-checked character",
    build() {
      return {
        nodes: [
          { id: "task", type: "input.task", x: 40, y: 60 },
          { id: "ref", type: "input.reference", x: 40, y: 260 },
          { id: "anchor", type: "art.anchor", x: 300, y: 150 },
          { id: "consist", type: "control.consistency", x: 560, y: 150 },
          { id: "gate", type: "control.gate", x: 810, y: 150 },
        ],
        edges: [
          { from: ["task", "o"], to: ["anchor", "task"] },
          { from: ["ref", "o"], to: ["anchor", "ref"] },
          { from: ["anchor", "o"], to: ["consist", "candidate"] },
          { from: ["consist", "o"], to: ["gate", "i"] },
        ],
      };
    },
  });

  WF.registerTemplate({
    id: "tpl.animation", name: "Animation sprite", category: "asset", glyph: "◈",
    hint: "anchor → per-frame variants → QA → sheet → Godot",
    build() {
      return {
        nodes: [
          { id: "task", type: "input.task", x: 40, y: 150 },
          { id: "anchor", type: "art.anchor", x: 270, y: 150 },
          { id: "anim", type: "art.animation", x: 500, y: 150 },
          { id: "consist", type: "control.consistency", x: 730, y: 150 },
          { id: "pick", type: "control.select", x: 960, y: 150 },
          { id: "sheet", type: "output.sheet", x: 1190, y: 150 },
          { id: "rig", type: "output.rig", x: 1420, y: 150 },
        ],
        edges: [
          { from: ["task", "o"], to: ["anchor", "task"] },
          { from: ["anchor", "o"], to: ["anim", "anchor"] },
          { from: ["anim", "o"], to: ["consist", "candidate"] },
          { from: ["consist", "o"], to: ["pick", "i"] },
          { from: ["pick", "o"], to: ["sheet", "frames"] },
          { from: ["sheet", "o"], to: ["rig", "asset"] },
        ],
      };
    },
  });
})();
