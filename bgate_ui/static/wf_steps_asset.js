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

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Show real assets in the nodes: a reference thumbnail (hides on 404).
  // Resolved through WF.refImg — pins are versioned files whose suffix is
  // whatever was pinned, so assuming "<name>.png" rendered jpg/webp anchors
  // blank and pointed at a revision that need not exist.
  const refThumb = name => (name && WF.refImg) ? WF.refImg(name) : "";
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

  const numRow = (node, field, label, dflt, min, max) =>
    `<div class="wf-row"><label>${esc(label)}</label><input type="number" style="max-width:76px" min="${min}" max="${max}" value="${(node.config && node.config[field]) != null ? node.config[field] : dflt}" oninput="WF.set('${node.id}','${field}',Math.max(${min},Math.min(${max},+this.value||${dflt})))"></div>`;

  const textRow = (node, field, label, ph) =>
    `<div class="wf-row" style="flex-direction:column;align-items:stretch"><label style="margin-bottom:5px">${esc(label)}</label><input type="text" style="width:100%" placeholder="${esc(ph || "")}" value="${esc((node.config && node.config[field]) || "")}" oninput="WF.set('${node.id}','${field}',this.value)"></div>`;

  const selRow = (node, field, label, choices) =>
    `<div class="wf-row"><label>${esc(label)}</label><select onchange="WF.set('${node.id}','${field}',this.value)">${choices.map(c => `<option value="${esc(c)}"${(node.config && node.config[field]) === c ? " selected" : ""}>${esc(c)}</option>`).join("")}</select></div>`;

  const boolRow = (node, field, label) =>
    `<div class="wf-row"><label>${esc(label)}</label><input type="checkbox" ${(node.config && node.config[field]) ? "checked" : ""} onchange="WF.set('${node.id}','${field}',this.checked)"></div>`;

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
    body(n) {
      return `<div class="wf-b-note">${esc(cfg(n, "style", "concept style"))}</div>
        <div class="wf-b-tag">× ${+cfg(n, "count", 4)} variants</div>`;
    },
    config(n) {
      return `<div class="wf-insp-p">Explore the look before committing. Generates several concept images in one style for the task.</div>`
        + textRow(n, "style", "Style", "e.g. painterly key art, 16-bit pixel")
        + numRow(n, "count", "Variants", 4, 1, 6);
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
    ports() { return { in: [{ id: "ref", label: "ref" }], out: [{ id: "o", label: "anchor" }] }; },
    body(n) {
      const c = cfg(n, "character", "");
      return `${refThumb(c)}
        <div class="wf-b-note"><b>${c ? esc(c) : "character?"}</b> · ${esc(cfg(n, "pose", "idle"))}</div>
        <div class="wf-b-tag">consistency ${esc(cfg(n, "strictness", "high"))}${cfg(n, "transparent", true) ? " · alpha" : ""}</div>`;
    },
    config(n) {
      return `<div class="wf-insp-p">The <b>canonical idle frame</b>. Everything downstream anchors to this — animation frames, edits and QA all reference it, so lock it before generating anything else.</div>`
        + `<div class="wf-row" style="flex-direction:column;align-items:stretch"><label style="margin-bottom:5px">Character</label>${charSelect(n, "character")}</div>`
        + textRow(n, "pose", "Pose", "idle")
        + selRow(n, "strictness", "Consistency strictness", ["low", "med", "high"])
        + boolRow(n, "transparent", "Transparent background")
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
    ports() { return { in: [{ id: "anchor", label: "anchor" }], out: [{ id: "o", label: "frames" }] }; },
    body(n) {
      return `${refThumb(upstreamCharacter(n))}
        <div class="wf-b-note">${frameCount(n)} frames · ×${+cfg(n, "variantsPerFrame", 2)}</div>
        <div class="wf-b-tag">${esc(cfg(n, "conditioning", "anchor+prev"))} · ${+cfg(n, "fps", 12)}fps</div>`;
    },
    config(n) {
      const fl = (n.config && n.config.frameList || "").trim();
      return `<div class="wf-insp-p">The flagship step. Each frame is conditioned on the anchor (and optionally the previous frame) so the character stays on-model across the whole animation. Multiple <b>variants per frame</b> are generated so a human can pick the best one.</div>`
        + `<div class="wf-row" style="flex-direction:column;align-items:stretch"><label style="margin-bottom:5px">Frame list <span class="wf-b-tag">(comma-separated, or clear + use count)</span></label><input type="text" style="width:100%" placeholder="windup,strike,recover" value="${esc(fl)}" oninput="WF.set('${n.id}','frameList',this.value)"></div>`
        + (fl ? `<div class="wf-b-note">${frameCount(n)} named frames.</div>` : numRow(n, "frames", "Frame count", 6, 1, 24))
        + numRow(n, "variantsPerFrame", "Variants / frame", 2, 1, 4)
        + selRow(n, "conditioning", "Conditioning", ["anchor", "anchor+prev"])
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
    ports() { return { in: [{ id: "frame", label: "frame" }], out: [{ id: "o", label: "edited" }] }; },
    body(n) {
      return `${refThumb(upstreamCharacter(n))}
        <div class="wf-b-note">${esc(cfg(n, "instruction", "edit instruction…"))}</div>
        ${cfg(n, "keepAnchor", true) ? `<div class="wf-b-tag">stay on anchor</div>` : ""}`;
    },
    config(n) {
      return `<div class="wf-insp-p">Targeted edit of one incoming frame — fix a hand, recolor, adjust silhouette — without regenerating from scratch.</div>`
        + `<div class="wf-row" style="flex-direction:column;align-items:stretch"><label style="margin-bottom:5px">Instruction</label><textarea class="wf-ta" placeholder="e.g. straighten the sword arm, brighten the rim light" oninput="WF.set('${n.id}','instruction',this.value)">${esc(cfg(n, "instruction", ""))}</textarea></div>`
        + boolRow(n, "keepAnchor", "Keep anchored to character");
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
    ports() { return { in: [{ id: "candidate", label: "candidate" }], out: [{ id: "o", label: "passed" }] }; },
    body(n) {
      return `<div class="wf-b-note">reject off-model frames</div>
        <div class="wf-b-tag">enforced ≥ ${+cfg(n, "threshold", 80)}% on-model</div>`;
    },
    config(n) {
      return `<div class="wf-insp-p">An <b>independent</b> art-QA pass (run by the QA seat, not the artist) that checks each candidate frame against its anchor/reference. Independence is the point — the generator does not grade its own output.</div>`
        + numRow(n, "threshold", "Pass threshold %", 80, 0, 100)
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
    body(n) {
      return `<div class="wf-b-note">fan → ${+cfg(n, "count", 3)}</div>
        <div class="wf-b-tag">option modifier · no agent</div>`;
    },
    config(n) {
      return `<div class="wf-insp-p">Fan the upstream output into N parallel variants for selection. This is an option modifier — it does not run an agent of its own; it multiplies the step feeding it.</div>`
        + numRow(n, "count", "Fan-out count", 3, 1, 8);
    },
  });

  /* ===================================================================== */
  /* STEP: control.select — human picks the best variant (gate-like)       */
  /* ===================================================================== */
  WF.registerStep({
    type: "control.select", category: "control", label: "Select best", glyph: "☑", accent: "var(--warn)",
    kind: "gate",
    defaults: {},
    ports() { return { in: [{ id: "i", label: "candidates" }], out: [{ id: "o", label: "chosen" }] }; },
    body() { return `<div class="wf-b-note">blocks until a human picks</div>`; },
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
    ports() { return { in: [{ id: "frames", label: "frames" }], out: [{ id: "o", label: "sheet" }] }; },
    body(n) {
      return `<div class="wf-b-note">stitch → sprite sheet</div>
        <div class="wf-b-tag">${esc(cfg(n, "layout", "horizontal"))} · ${+cfg(n, "fps", 12)}fps</div>`;
    },
    config(n) {
      return `<div class="wf-insp-p">Assemble the selected / approved frames into a single sprite sheet ready for import.</div>`
        + selRow(n, "layout", "Layout", ["horizontal", "vertical", "grid"])
        + numRow(n, "fps", "Playback fps", 12, 1, 60);
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
    ports() { return { in: [{ id: "asset", label: "asset" }] }; },
    body() { return `<div class="wf-b-note">import & wire into Godot</div>`; },
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
          { from: ["task", "o"], to: ["anchor", "ref"] },
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
          { from: ["task", "o"], to: ["anchor", "ref"] },
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
