/* wf_steps_agent.js — AGENT (seat) workflow steps + multi-agent task templates.
 *
 * Contributes category:"agent" steps — one per SEAT (director, art, gameplay, tech,
 * qa, narrative, audio) plus a control.test runtime-probe gate — and the reusable
 * multi-agent TASK templates that show how those seats collaborate on a single task:
 * editing an existing animation element, fixing a gameplay element, building a new
 * character. Each agent step is a SEAT doing its part of the workflow for the task
 * (input.task); the art seat gets the deepest config because dialing in visual
 * consistency is the hardest part of the pipeline.
 *
 * Loads after wf.js (window.WF). References control.consistency + input.task/
 * control.gate by type only (control.consistency ships in wf_steps_asset.js which
 * also loads); no hard dependency — templates reference them as step-type strings.
 *
 * Never throws: everything is wrapped in an IIFE and guarded.
 */
(function () {
  if (!window.WF || typeof WF.registerStep !== "function") return;
  if (!window.NodeCanvas || !NodeCanvas.w) return;
  try {

    // widgets live ON the node; the inspector keeps the prose
    const w = NodeCanvas.w;
    const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const cfg = (n, k, d) => (n && n.config && n.config[k] != null && n.config[k] !== "") ? n.config[k] : d;
    const onoff = v => (v === true || v === "true" || v === 1) ? true : false;

    /* seat colors */
    const C_DIRECTOR = "var(--c-director)";
    const C_NARRATIVE = "var(--c-narrative)";
    const C_GAMEPLAY = "var(--c-gameplay)";
    const C_TECH = "var(--c-tech)";
    const C_ART = "var(--c-art)";
    const C_AUDIO = "var(--c-audio)";
    const C_QA = "var(--c-qa)";

    /* {v,t} choice lists -> the {value,label} shape the node widgets want */
    const opts = list => list.map(o => (o && typeof o === "object") ? { value: o.v, label: o.t } : o);

    /* the seat a step runs on, shown as a small tag on the node card */
    const seatTag = seat => `<div class="wf-b-tag">seat · ${esc(seat)}</div>`;
    /* the workflow's Task text (input.task node), so a brief references the complaint */
    const taskText = (wf) => {
      try {
        const t = (wf && wf.nodes || []).find(n => n.type === "input.task");
        const v = t && t.config && t.config.text;
        return (v && String(v).trim()) ? String(v).trim() : "the task";
      } catch (e) { return "the task"; }
    };

    /* ====================================================================== */
    /* AGENT SEAT STEPS                                                        */
    /* ====================================================================== */

    /* ---- agent.director — delegator / task splitter ---------------------- */
    WF.registerStep({
      type: "agent.director", category: "agent", label: "Director agent", glyph: "◆", accent: C_DIRECTOR,
      agentSeat: "director",
      defaults: { split: true },
      /* A plan is still a task — one per seat — so it plugs into any seat step. */
      ports: () => ({ in: [{ id: "i", label: "task", type: "task" }], out: [{ id: "o", label: "plan", type: "task" }] }),
      body: n => w.toggle(n, "split", { label: "Split", value: true, hint: "break into per-seat subtasks" }) + seatTag("director"),
      config: () => `<div class="wf-insp-p">The Director reads the task and delegates it to the right seats — optionally breaking it into subtasks first.</div>`,
      toBrief: (n, wf) => `Analyze the task — "${taskText(wf)}" — and delegate it to the right seats` +
        (onoff(cfg(n, "split", true)) ? ", first breaking it into ordered subtasks per seat (art / gameplay / tech / qa)." : " as a single coordinated task."),
    });

    /* ---- agent.art — art seat (RICHEST config) --------------------------- */
    const ART_FOCUS = [
      { v: "consistency", t: "consistency pass" },
      { v: "new-asset", t: "new asset" },
      { v: "edit-existing", t: "edit existing" },
      { v: "animation", t: "animation" },
    ];
    const ART_STRICT = [{ v: "low", t: "low" }, { v: "med", t: "medium" }, { v: "high", t: "high" }];
    WF.registerStep({
      type: "agent.art", category: "agent", label: "Art agent", glyph: "▲", accent: C_ART,
      agentSeat: "art",
      defaults: { focus: "edit-existing", strictness: "high", variants: 3, useAnchor: true },
      ports: () => ({ in: [{ id: "i", label: "task", type: "task" }], out: [{ id: "o", label: "art", type: "image" }] }),
      body: n => w.select(n, "focus", { label: "Focus", options: opts(ART_FOCUS), value: "edit-existing" }) +
        w.select(n, "strictness", { label: "Consistency", options: opts(ART_STRICT), value: "high" }) +
        w.number(n, "variants", { label: "Variants", min: 1, max: 8, value: 3 }) +
        w.toggle(n, "useAnchor", { label: "Anchor", value: true }) + seatTag("art"),
      config: () => `<div class="wf-insp-p">The Art seat does the visual work for the task. The node is where you tune <b>how</b> the art agent produces a consistent result — the hardest part of the pipeline.</div>` +
        `<div class="wf-insp-p" style="margin-top:10px">` +
        `<b>Focus</b> picks the art job. <b>Consistency</b> sets how hard the agent locks to the existing style/anchor before a variant passes. ` +
        `<b>Variants</b> is how many versions to produce for review. <b>Anchor</b> feeds the existing element as the reference so an edit stays on-model.</div>`,
      toBrief: (n, wf) => `Do the art work for the task — "${taskText(wf)}" — with focus ${cfg(n, "focus", "edit-existing")}, ` +
        `anchoring=${onoff(cfg(n, "useAnchor", true))}, consistency ${cfg(n, "strictness", "high")}, ` +
        `producing ${cfg(n, "variants", 3)} variant(s) for review.`,
    });

    /* ---- agent.gameplay — gameplay seat ---------------------------------- */
    const GP_AREA = [
      { v: "tuning", t: "tuning" }, { v: "mechanics", t: "mechanics" },
      { v: "ai", t: "AI / behavior" }, { v: "hitboxes", t: "hitboxes" },
    ];
    WF.registerStep({
      type: "agent.gameplay", category: "agent", label: "Gameplay agent", glyph: "◈", accent: C_GAMEPLAY,
      agentSeat: "gameplay",
      defaults: { area: "tuning", verify: true },
      /* out is untyped: a code change is not one of the asset types, and it
         feeds probes, QA and gates alike. */
      ports: () => ({ in: [{ id: "i", label: "task", type: "task" }], out: [{ id: "o", label: "change" }] }),
      body: n => w.select(n, "area", { label: "Area", options: opts(GP_AREA), value: "tuning" }) +
        w.toggle(n, "verify", { label: "Verify", value: true }) + seatTag("gameplay"),
      config: () => `<div class="wf-insp-p">The Gameplay seat implements the mechanical change for the task — timing, hit-detection, behavior. <b>Verify</b> makes the seat re-check its own change before handing off.</div>`,
      toBrief: (n, wf) => `Implement the gameplay change (${cfg(n, "area", "tuning")}) for the task — "${taskText(wf)}"; ` +
        `verify=${onoff(cfg(n, "verify", true))}.`,
    });

    /* ---- agent.tech — tech / engine seat --------------------------------- */
    const TECH_TASK = [
      { v: "build", t: "build" }, { v: "perf", t: "performance" }, { v: "rig", t: "rig" },
      { v: "import", t: "import" }, { v: "engine", t: "engine" },
    ];
    WF.registerStep({
      type: "agent.tech", category: "agent", label: "Tech agent", glyph: "⬡", accent: C_TECH,
      agentSeat: "tech",
      defaults: { task: "rig" },
      /* deliberately untyped both ways — the tech seat takes whatever needs
         wiring into the engine and hands back whatever it wired. */
      ports: () => ({ in: [{ id: "i", label: "in" }], out: [{ id: "o", label: "out" }] }),
      body: n => w.select(n, "task", { label: "Task", options: opts(TECH_TASK), value: "rig" }) + seatTag("tech"),
      config: () => `<div class="wf-insp-p">The Tech seat handles engine-side work — rigging, import, build, performance.</div>`,
      toBrief: (n, wf) => `Do the tech work (${cfg(n, "task", "rig")}) for the task — "${taskText(wf)}" — ` +
        `and wire the result into the Godot project.`,
    });

    /* ---- agent.qa — QA seat ---------------------------------------------- */
    const QA_MODE = [
      { v: "runtime-probe", t: "runtime probe" }, { v: "consistency", t: "consistency" },
      { v: "playtest", t: "playtest" }, { v: "build-check", t: "build check" },
    ];
    WF.registerStep({
      type: "agent.qa", category: "agent", label: "QA agent", glyph: "✓", accent: C_QA,
      agentSeat: "qa",
      defaults: { mode: "playtest" },
      ports: () => ({ in: [{ id: "i", label: "change" }], out: [{ id: "o", label: "verdict" }] }),
      body: n => w.select(n, "mode", { label: "Mode", options: opts(QA_MODE), value: "playtest" }) + seatTag("qa"),
      config: () => `<div class="wf-insp-p">The QA seat verifies the change and returns a pass/fail verdict.</div>`,
      toBrief: (n, wf) => `QA the change for the task — "${taskText(wf)}" — via ${cfg(n, "mode", "playtest")}, and report pass/fail with findings.`,
    });

    /* ---- agent.narrative — narrative seat -------------------------------- */
    const NAR_FOCUS = [{ v: "dialogue", t: "dialogue" }, { v: "lore", t: "lore" }, { v: "story", t: "story" }];
    WF.registerStep({
      type: "agent.narrative", category: "agent", label: "Narrative agent", glyph: "✦", accent: C_NARRATIVE,
      agentSeat: "narrative",
      defaults: { focus: "dialogue" },
      ports: () => ({ in: [{ id: "i", label: "task", type: "task" }], out: [{ id: "o", label: "text", type: "text" }] }),
      body: n => w.select(n, "focus", { label: "Focus", options: opts(NAR_FOCUS), value: "dialogue" }) + seatTag("narrative"),
      config: () => `<div class="wf-insp-p">The Narrative seat writes dialogue, lore, or story beats consistent with the canon.</div>`,
      toBrief: (n, wf) => `Write the ${cfg(n, "focus", "dialogue")} for the task — "${taskText(wf)}" — consistent with the game's canon/lore.`,
    });

    /* ---- agent.audio — audio seat ---------------------------------------- */
    const AUD_FOCUS = [{ v: "sfx", t: "SFX" }, { v: "music", t: "music" }, { v: "cue", t: "cue" }];
    WF.registerStep({
      type: "agent.audio", category: "agent", label: "Audio agent", glyph: "♪", accent: C_AUDIO,
      agentSeat: "audio",
      defaults: { focus: "sfx" },
      ports: () => ({ in: [{ id: "i", label: "task", type: "task" }], out: [{ id: "o", label: "audio", type: "audio" }] }),
      body: n => w.select(n, "focus", { label: "Focus", options: opts(AUD_FOCUS), value: "sfx" }) + seatTag("audio"),
      config: () => `<div class="wf-insp-p">The Audio seat produces SFX, music, or a stinger cue for the task.</div>`,
      toBrief: (n, wf) => `Produce the ${cfg(n, "focus", "sfx")} for the task — "${taskText(wf)}" — matched to the game's palette and mood.`,
    });

    /* ---- control.test — runtime probe gate (category "control") ---------- */
    const TEST_KIND = [{ v: "runtime", t: "runtime" }, { v: "build", t: "build" }, { v: "unit", t: "unit" }];
    WF.registerStep({
      type: "control.test", category: "control", label: "Test / runtime probe", glyph: "◉", accent: C_QA,
      agentSeat: "qa",
      defaults: { kind: "runtime" },
      ports: () => ({ in: [{ id: "change", label: "change" }], out: [{ id: "o", label: "pass" }] }),
      body: n => w.select(n, "kind", { label: "Probe", options: opts(TEST_KIND), value: "runtime" }) + seatTag("qa"),
      config: () => `<div class="wf-insp-p">Drives the game and verifies the change actually works before it passes downstream.</div>`,
      toBrief: (n, wf) => `Drive the game headless and verify the change for the task — "${taskText(wf)}" — actually works ` +
        `(${cfg(n, "kind", "runtime")} probe); report pass/fail.`,
    });

    /* ====================================================================== */
    /* MULTI-AGENT TASK TEMPLATES                                             */
    /* ====================================================================== */
    const DX = 230, Y = 150;
    const X = i => 60 + i * DX;

    /* ---- tpl.editanim — Edit existing animation element ------------------ */
    WF.registerTemplate({
      id: "tpl.editanim", name: "Edit existing animation element", category: "agent", glyph: "◈",
      hint: "complaint → art regen → consistency → gameplay timing → test",
      build() {
        return {
          nodes: [
            { id: "task", type: "input.task", x: X(0), y: Y, config: { text: "" } },
            { id: "art", type: "agent.art", x: X(1), y: Y, config: { focus: "edit-existing", strictness: "high", variants: 3, useAnchor: true } },
            { id: "cons", type: "control.consistency", x: X(2), y: Y },
            { id: "gp", type: "agent.gameplay", x: X(3), y: Y, config: { area: "tuning", verify: true } },
            { id: "test", type: "control.test", x: X(4), y: Y, config: { kind: "runtime" } },
            { id: "gate", type: "control.gate", x: X(5), y: Y },
          ],
          edges: [
            { from: ["task", "o"], to: ["art", "i"] },
            { from: ["art", "o"], to: ["cons", "candidate"] },
            { from: ["cons", "o"], to: ["gp", "i"] },
            { from: ["gp", "o"], to: ["test", "change"] },
            { from: ["test", "o"], to: ["gate", "i"] },
          ],
        };
      },
    });

    /* ---- tpl.fixgameplay — Fix gameplay element -------------------------- */
    WF.registerTemplate({
      id: "tpl.fixgameplay", name: "Fix gameplay element", category: "agent", glyph: "⌖",
      hint: "complaint → director → gameplay → test → qa",
      build() {
        return {
          nodes: [
            { id: "task", type: "input.task", x: X(0), y: Y, config: { text: "" } },
            { id: "dir", type: "agent.director", x: X(1), y: Y, config: { split: true } },
            { id: "gp", type: "agent.gameplay", x: X(2), y: Y, config: { area: "mechanics", verify: true } },
            { id: "test", type: "control.test", x: X(3), y: Y, config: { kind: "runtime" } },
            { id: "qa", type: "agent.qa", x: X(4), y: Y, config: { mode: "playtest" } },
            { id: "gate", type: "control.gate", x: X(5), y: Y },
          ],
          edges: [
            { from: ["task", "o"], to: ["dir", "i"] },
            { from: ["dir", "o"], to: ["gp", "i"] },
            { from: ["gp", "o"], to: ["test", "change"] },
            { from: ["test", "o"], to: ["qa", "i"] },
            { from: ["qa", "o"], to: ["gate", "i"] },
          ],
        };
      },
    });

    /* ---- tpl.newchar — New character (art → rig) ------------------------- */
    WF.registerTemplate({
      id: "tpl.newchar", name: "New character (art→rig)", category: "agent", glyph: "▲",
      hint: "concept → art → consistency → rig",
      build() {
        return {
          nodes: [
            { id: "task", type: "input.task", x: X(0), y: Y, config: { text: "" } },
            { id: "art", type: "agent.art", x: X(1), y: Y, config: { focus: "new-asset", strictness: "high", variants: 4, useAnchor: false } },
            { id: "cons", type: "control.consistency", x: X(2), y: Y },
            { id: "tech", type: "agent.tech", x: X(3), y: Y, config: { task: "rig" } },
            { id: "gate", type: "control.gate", x: X(4), y: Y },
          ],
          edges: [
            { from: ["task", "o"], to: ["art", "i"] },
            { from: ["art", "o"], to: ["cons", "candidate"] },
            { from: ["cons", "o"], to: ["tech", "i"] },
            { from: ["tech", "o"], to: ["gate", "i"] },
          ],
        };
      },
    });

  } catch (e) { try { console.error("wf_steps_agent:", e); } catch (_) {} }
})();
