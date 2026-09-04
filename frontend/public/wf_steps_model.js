/* wf_steps_model.js — THE MODEL IS THE NODE.
 *
 * The steps in wf_steps_asset.js describe a JOB ("character anchor", "animation
 * frames") and let the server decide what runs it. That is the right default,
 * and it is exactly wrong when the thing you are doing is comparing generators:
 * you want the same prompt and the same reference fanned into SEVERAL models,
 * side by side, run on your say-so, looked at, and only then continued from.
 *
 * So this file contributes three steps that put the human in the scheduler's
 * chair:
 *
 *   model.image  — one generator, one card. Its identity is the model it
 *                  resolves to, which is why the resolved name is its BADGE:
 *                  three of these next to each other read as three models, not
 *                  three anonymous boxes.
 *   llm.prompt   — a prompt writer whose output is a PROMPT-typed wire, so one
 *                  written prompt can feed every model card at once.
 *   control.pick — the candidates from the model cards upstream, and a human
 *                  choosing one of them (or none).
 *
 * TIERS, NOT MODEL NAMES. A user says what they are making and how good it has
 * to be — draft / standard / hero. Which model that is, and what it costs, is
 * the server's answer (WF.tierLadder / WF.tierResolve, read from the tier
 * endpoint). Nothing here contains a model catalogue or a price: a second copy
 * of either would drift from the one that gets charged, and a node that lies
 * about what it is about to spend is worse than a node that says "unknown".
 * Where the ladder is unavailable the cards say so and keep working — the tier
 * a node names is still stored and still sent with the run, which resolves it
 * server-side; the card simply cannot say which model that is or price it.
 *
 * RUNNING. Each model / prompt card carries its own ▶, which runs THAT node and
 * nothing else, over the canvas's existing [data-wact] channel. The schedule is
 * the user's: fan out, run the cards you want, compare, pick, continue.
 */
(function () {
  if (!window.WF || typeof WF.registerStep !== "function") return;
  if (!window.NodeCanvas || !NodeCanvas.w) return;

  const w = NodeCanvas.w;
  const esc = NodeCanvas.esc;
  const toast = (m, bad) => (window.BGWS ? BGWS.toast(m, bad) : console.log(m));
  const cfg = (n, f, d) =>
    (n && n.config && n.config[f] != null && n.config[f] !== "") ? n.config[f] : d;
  const str = (n, f, d) => String(cfg(n, f, d == null ? "" : d));
  const num = (n, f, d) => {
    const v = Number(cfg(n, f, d));
    return isFinite(v) ? v : d;
  };

  /* ---------------------------------------------------------------------- */
  /* the ladder, as this file is allowed to know it: whatever WF read back    */
  /* ---------------------------------------------------------------------- */

  /* An explicit provider/model override beats the ladder — that is what an
     override is for — and it is reported as an override, never dressed up as a
     resolved tier with a price the UI does not have. */
  function resolved(n) {
    const model = str(n, "model", "").trim();
    if (model) {
      return { override: true, model: model, provider: str(n, "provider", "").trim(),
        usd: null, flat: false, note: "" };
    }
    const rung = WF.tierResolve(str(n, "task_kind", ""), str(n, "tier", ""));
    return rung ? Object.assign({ override: false }, rung) : null;
  }
  const modelName = n => { const r = resolved(n); return (r && r.model) || ""; };

  // What the user is making. The kinds are the server's; an unknown ladder
  // leaves whatever this node already names, so a saved graph never loses it.
  function kindOptions(n) {
    const cur = str(n, "task_kind", "");
    const known = WF.tierKinds();
    const list = known.slice();
    if (cur && list.indexOf(cur) === -1) list.push(cur);
    return [{ value: "", label: "- what are you making? -" }]
      .concat(list.map(k => ({ value: k, label: k })));
  }

  /* The tier selector. Each rung is labelled with the model it resolves to, and
     a rung that resolves to the SAME model as the rung below says so instead of
     pretending to be an upgrade — WF.tierLadder marks those flat. */
  function tierOptions(n) {
    const kind = str(n, "task_kind", "");
    const cur = str(n, "tier", "");
    const ladder = WF.tierLadder(kind);
    if (!ladder.length) {
      const names = WF.tierNames();
      const list = names.length ? names : (cur ? [cur] : []);
      return [{ value: "", label: "- quality -" }]
        .concat(list.map(t => ({ value: t, label: t })));
    }
    return [{ value: "", label: "- quality -" }].concat(ladder.map(r => ({
      value: r.tier,
      label: r.tier + (r.model ? " · " + r.model : "") + (r.flat ? "  (no change)" : ""),
    })));
  }

  /* One honest line about what this card will actually call. */
  function resolvedLine(n) {
    if (!WF.tiersReady()) {
      return `<div class="wf-warn">model ladder unavailable - ${esc(WF.tiersError() || "not loaded")}. `
        + `The tier below is still stored and still sent with the run; this card cannot name the model or price it.</div>`;
    }
    const r = resolved(n);
    if (!r) {
      const kind = str(n, "task_kind", ""), tier = str(n, "tier", "");
      if (!kind || !tier) return w.note("pick what you are making and how good it has to be");
      return `<div class="wf-warn">the ladder has no ${esc(tier)} rung for ${esc(kind)}</div>`;
    }
    if (r.override) {
      // The engine refuses half an override rather than guessing the provider,
      // so say that here instead of at run time.
      if (!r.provider) {
        return `<div class="wf-warn">an explicit model needs a provider too - fill both, or clear both and use the tier</div>`;
      }
      return w.note(`override: ${r.provider} / ${r.model} - priced by the server, not here`);
    }
    const each = r.usd == null ? "" : ` · ${WF.fmtUsd(r.usd)} each`;
    return w.note(`${r.provider ? r.provider + " / " : ""}${r.model}${each}`)
      + (r.flat ? w.note("this tier resolves to the same model as the one below it") : "")
      + (r.note ? w.note(r.note) : "");
  }

  /* Rewrite the prompt in place. Synchronous on purpose: the caller is a
     person looking at a text box, so anything that returns a job id here is
     the wrong shape. The original text is kept so a bad rewrite is undoable. */
  async function improve(n) {
    const before = str(n, "prompt", "").trim();
    if (!before) { toast("write a rough note first", true); return; }
    const res = await fetch("/api/prompt/expand", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: before, task_kind: str(n, "task_kind", "") }),
    }).then(r => r.json()).catch(() => null);
    const out = res && res.ok && res.data;
    if (!out || !out.text) {
      toast((res && res.error && res.error.message) || "could not improve the prompt", true);
      return;
    }
    WF.set(n.id, "prompt_before", before);
    WF.set(n.id, "prompt", out.text);
    toast("prompt rewritten - the original is kept in the inspector");
  }

  /* Run THIS card and nothing else. It travels the canvas's own [data-wact]
     channel (nodecanvas routes those to onAction) rather than binding its own
     listener - a card that invents an event path stops working the moment the
     canvas re-renders it. */
  function runRow(n) {
    return `<div class="wf-act"><button class="nc-w wf-run1" data-wact="run" data-wval="${esc(n.id)}"
      title="generate from this card alone, without running the rest of the graph">▶ run this card</button>`
      + `<button class="nc-w wf-run1 ghost" data-wact="compare" data-wval="${esc(n.id)}"
      title="add the other available models on the same inputs">＋ add comparison models</button></div>`;
  }

  /* What this card actually produced, from the run's own record of THIS node —
     not from a name-matched lookup, because several sibling cards generating
     the same subject would otherwise all show each other's pictures. Falls back
     to the project's media cache before the first run. */
  function producedStrip(n) {
    const made = WF.nodeArtifacts(n.id).slice(0, 4);
    if (!made.length) return WF.mediaStrip(n, { empty: "nothing generated yet", cap: 4 });
    return `<div class="wf-cands">` + made.map(a => {
      const url = WF.artUrl(a.path);
      return `<span class="wf-cand">${url
        ? `<img src="${esc(url)}" loading="lazy" onerror="this.style.visibility='hidden'">`
        : ""}<span class="wf-cand-n">#${esc(a.artifact_id)}</span></span>`;
    }).join("") + `</div>`
      + w.note(`${made.length} candidate${made.length === 1 ? "" : "s"}`);
  }

  function upstreamNodes(n) {
    const nc = WF._nc;
    if (!nc) return [];
    return (nc.edges || []).filter(e => e.to && e.to[0] === n.id)
      .map(e => nc.nodes.get(e.from[0])).filter(Boolean);
  }

  /* ===================================================================== */
  /* STEP: model.image — one model, one card                               */
  /* ===================================================================== */
  WF.registerStep({
    type: "model.image", category: "asset", label: "Image model", glyph: "◉", accent: "var(--c-art)",
    /* A GENERATE step: the run calls the provider for this node itself rather
       than queueing a seat to do it. That is what makes "run just this card"
       instant enough to compare with, and what makes several cards runnable at
       the same time. */
    kind: "generate",
    defaults: { task_kind: "", tier: "", prompt: "", count: 1, seed: 0, /* 0 = random; set one to pin a result */ provider: "", model: "" },
    ports() {
      return {
        in: [{ id: "prompt", label: "prompt", type: "prompt" },
             { id: "ref", label: "ref", type: "ref" }],
        out: [{ id: "image", label: "image", type: "image" }],
      };
    },
    /* The card's identity IS the model — legible at a glance across siblings.
       Once it has run, the model the RUN actually used wins over the one the
       ladder predicts: what happened beats what was planned. */
    badge(n) {
      const made = WF.nodeArtifacts(n.id);
      return (made.length && made[0].model) || modelName(n);
    },
    body(n) {
      return producedStrip(n)
        + w.select(n, "task_kind", { label: "Making", options: kindOptions(n) })
        + w.select(n, "tier", { label: "Quality", options: tierOptions(n) })
        + w.text(n, "prompt", { label: "Prompt", rows: 3,
            placeholder: "…or wire a PROMPT into the inlet" })
        // Sharpen the words in place. This used to be a whole node you wired
        // in; it is one call, so it belongs on the field it edits.
        // The leading glyph is a literal character, not an entity: a colour
        // sweep once rewrote it to "&var(--bad-soft);", which is not a valid
        // HTML entity and so rendered verbatim — the button read
        // "&var(--bad-soft); improve" on every model card.
        + `<div class="wf-act"><button class="nc-w wf-improve" data-wact="improve"
             data-wval="${esc(n.id)}" title="rewrite this into a fuller image prompt"
             ${str(n, "prompt", "").trim() ? "" : "disabled"}>Improve prompt</button></div>`
        + w.number(n, "count", { label: "Count", min: 1, max: 8, value: 1 })
        + w.seed(n, "seed", { label: "Seed", hint: "0 = random each run; set a number to reproduce one" })
        + w.text(n, "model", { label: "Model", placeholder: "override (blank = the tier's)" })
        + w.text(n, "provider", { label: "Provider", placeholder: "override" })
        + resolvedLine(n)
        + runRow(n);
    },
    config(n) {
      const kind = str(n, "task_kind", "");
      const ladder = WF.tierLadder(kind);
      const flat = WF.tierFlat(kind);
      let html = `<div class="wf-insp-p">A card whose identity is the <b>model</b>. Say what you are making and how good it has to be; the server resolves that to a provider and a model and tells this card what it costs - the name you get is on the title bar, so several of these side by side read as a comparison.</div>`
        + `<div class="wf-insp-p">The prompt wired into the inlet wins over the one typed on the card - that wire is the point of the pattern. Put <code>{input}</code> in the card's own prompt to compose instead of choose: the wired text lands there and everything around it (a style suffix, a negative, a framing note) survives.</div>`
        + `<div class="wf-insp-p">Run workflow executes connected model cards together; no work is dispatched to a seat.</div>`;
      if (!WF.tiersReady()) {
        html += `<div class="wf-warn">The tier ladder could not be read (${esc(WF.tiersError() || "not loaded")}), so this panel cannot show which model each rung is. Your choice is still stored and still sent with the run, which resolves it server-side.</div>`;
        return html;
      }
      if (!ladder.length) {
        return html + `<div class="wf-insp-p">Pick what you are making on the card to see its ladder.</div>`;
      }
      html += `<div class="wf-insp-p">The ladder for <b>${esc(kind)}</b>:</div>`;
      ladder.forEach(r => {
        html += `<div class="wf-row"><label>${esc(r.tier)}${r.flat ? " (no change)" : ""}</label>`
          + `<span>${esc(r.model)}${r.usd == null ? "" : " · " + WF.fmtUsd(r.usd)}</span></div>`;
      });
      if (flat.length) {
        html += `<div class="wf-b-note">${esc(flat.join(", "))} resolve to the same model as the rung below - nothing better exists for this job today, so those rungs are marked rather than sold as an upgrade.</div>`;
      }
      return html;
    },
    /* Priced from the ladder the server published — never from a table in this
       file. No ladder, no number: an invented price is worse than none. */
    costUsd(n) {
      const r = resolved(n);
      if (!r || r.override || r.usd == null) return null;
      return r.usd * Math.max(0, Math.floor(num(n, "count", 1)));
    },
    onAction(n, action, field) {
      if (action === "run") { WF.runNode(n.id); return; }
      if (action === "compare") { fanOut(n); return; }
      if (action === "improve") { improve(n); return; }
    },
    /* No brief: a generate node is not dispatched to a seat, it calls the
       provider itself. Its prompt is the wire it is fed (or config.prompt, and
       "{input}" inside that composes the two — a fixed style suffix that
       survives an LLM-authored subject). Writing a brief here would hand the
       engine a paragraph of instructions to use AS the prompt. */
  });

  /* Clone this card once per DISTINCT model on its ladder, wired to the same
     inputs: Krea's three cards, the same prompt, different generators. Tiers
     that resolve to the model below them are not offered — a fourth card that
     calls the same thing is a comparison with itself. */
  function fanOut(n) {
    const kind = str(n, "task_kind", "");
    const ladder = WF.tierLadder(kind);
    if (!ladder.length) {
      toast(WF.tiersReady()
        ? "pick what you are making first - the ladder decides what there is to compare"
        : "no model ladder on this server, so there is nothing honest to fan out into", true);
      return;
    }
    const cur = str(n, "tier", "");
    const targets = ladder.filter(r => !r.flat && r.tier !== cur);
    if (!targets.length) {
      toast(`${kind || "this job"} resolves to one model on this server - nothing to compare it against`, true);
      return;
    }
    let made = 0;
    targets.forEach(r => {
      // the sibling differs ONLY in its tier; an override would defeat the point
      if (WF.duplicateNode(n.id, { tier: r.tier, model: "", provider: "" },
        { dy: (made + 1) * 300 })) made++;
    });
    toast(made ? `${made} sibling card${made === 1 ? "" : "s"} on the same inputs` : "nothing to compare", !made);
  }

  /* ===================================================================== */
  /* llm.prompt is GONE on purpose.
   *
   * It was a node whose only job was to fill in a text box. `input.task`
   * already carries shared text to every card on the same wire, and each model
   * card already has its own prompt field — so the node added a wire, a run
   * lifecycle and a failure mode to a problem that was already solved. Its
   * first incarnation was worse still: an agent step, so clicking run queued a
   * Claude session with a seat and a lane hook to rewrite one sentence.
   *
   * What survived is the useful part: `bgate_core.promptwriter`, reachable as
   * POST /api/prompt/expand and surfaced as an "improve" button ON the prompt
   * field. One call, about two seconds, no graph.
   */


  /* ===================================================================== */
  /* STEP: control.pick — the human chooses (or rejects everything)        */
  /* ===================================================================== */
  const NONE = "__none__";

  /* What this pick is choosing between.
     The RUN's own list first (GET .../candidates: every artifact its parent
     generate nodes registered, with the model that made each one) — that is the
     only list the server will accept a choice from, so showing anything else
     would offer the user options that get refused. Before a run exists, the
     upstream cards' own media is shown so the node is not an empty box. */
  function candidates(n) {
    const live = WF.candidatesFor(n.id);
    if (live.length) {
      return live.slice(0, 12).map(c => ({
        id: String(c.artifact_id),
        url: WF.artUrl(c.path),
        label: c.model || c.provider || c.logical_name || "candidate",
        name: c.logical_name || "",
        rev: c.revision == null ? "" : c.revision,
      }));
    }
    const out = [];
    upstreamNodes(n).forEach(up => {
      const label = (up.type === "model.image" && modelName(up)) || up.title || up.id;
      WF.nodeArtifacts(up.id).forEach(a => {
        const url = WF.artUrl(a.path);
        if (url) out.push({ id: String(a.artifact_id), url: url, label: a.model || label,
          name: a.logical_name || "", rev: a.revision == null ? "" : a.revision });
      });
      const m = WF.nodeMedia(up);
      ((m && m.candidates) || []).forEach(c => {
        if (!c || !c.rel) return;
        if (out.some(x => x.id === String(c.artifact_id))) return;
        out.push({ id: String(c.artifact_id),
          url: "/api/preview?rel=" + encodeURIComponent(c.rel),
          label: label, name: c.logical_name || "", rev: c.revision });
      });
    });
    return out.slice(0, 12);
  }

  WF.registerStep({
    type: "control.pick", category: "control", label: "Pick one", glyph: "☑", accent: "var(--warn)",
    // A pick, not a gate: approving says a human was happy, picking says WHICH
    // candidate — and only the second is a value the next step can consume.
    kind: "pick",
    defaults: { picked: "" },
    ports() {
      return { in: [{ id: "candidates", label: "candidates", type: "image" }],
        out: [{ id: "chosen", label: "chosen", type: "image" }] };
    },
    body(n) {
      const list = candidates(n);
      // the run's record of the choice outranks the local one — a page reload
      // must not forget which candidate won
      const decided = WF.nodePicked(n.id);
      const status = WF.nodeStatus(n.id);
      const won = decided ? String(decided.artifact_id)
        : (status === "failed" ? NONE : String(cfg(n, "picked", "")));
      if (!list.length) {
        return w.note("wire model cards in and run them - their candidates land here")
          + w.tag("human decision");
      }
      // Look, THEN choose. The thumbnail is ~90px; picking a winner off that is
      // guessing, so the picture opens full size and a separate button commits.
      const cards = list.map(c => `<div class="wf-cand-wrap${won === c.id ? " won" : ""}">
          <button class="nc-w wf-cand" data-wact="zoom" data-wval="${esc(c.url || "")}"
            title="view ${esc(c.label)}${c.name ? " · " + esc(c.name) : ""}${c.rev === "" ? "" : " r" + esc(c.rev)} full size">
            ${c.url ? `<img src="${esc(c.url)}" loading="lazy"
                 onerror="this.style.visibility='hidden'">` : ""}
            <span class="wf-cand-n">${esc(c.label)}</span></button>
          <button class="nc-w wf-choose" data-wact="pick" data-wval="${esc(c.id)}"
            title="use this one downstream">${won === c.id ? "✓ chosen" : "choose"}</button>
        </div>`).join("");
      const chosen = won && won !== NONE ? list.find(x => x.id === won) : null;
      const winner = won === NONE
        ? `<div class="wf-warn">every candidate rejected - nothing goes downstream</div>`
        : (won ? w.note(`chosen: ${chosen ? chosen.label + (chosen.name ? " · " + chosen.name : "") : "artifact #" + won}`)
               : w.note(`${list.length} candidate${list.length === 1 ? "" : "s"} - look at them, then choose`));
      return `<div class="wf-cands">${cards}</div>` + winner
        + `<div class="wf-act"><button class="nc-w wf-run1" data-wact="reject" data-wval="${esc(n.id)}"
             title="none of these are good enough">✕ reject all</button></div>`;
    },
    config() {
      return `<div class="wf-insp-p">The comparison's verdict. Every candidate its upstream model cards registered is shown here; clicking one is the selection, and that candidate - not "whatever the last step happened to write" - is what the next step consumes.</div>`
        + `<div class="wf-insp-p">It really blocks: the run holds here until a person decides, and only a person can decide (an agent calling the endpoint is refused). <b>Reject all</b> is a decision the picker supports and fails the node - three bad candidates should stop the run, not quietly promote the least bad one.</div>`;
    },
    onAction(n, action, field) {
      if (action === "zoom") { WF.zoom(field); return; }
      if (action === "pick") { WF.set(n.id, "picked", field); WF.pickCandidate(n.id, field); return; }
      if (action === "reject") { WF.set(n.id, "picked", NONE); WF.pickCandidate(n.id, ""); return; }
    },
  });

  /* ===================================================================== */
  /* WORLD CONTEXT: the bible and the lore graph, on a prompt wire           */
  /* ===================================================================== */
  /* The bible's LOCKED art direction is appended to every generation already
     (bgate_core.artdirection) — that is a floor, not a way to say things. These
     put SPECIFIC world context into a SPECIFIC step: this entity's canon facts,
     that pillar, the tone guide. Resolved at run time, so editing the bible
     changes the next run instead of baking a stale copy into the graph. */
  WF.registerStep({
    type: "input.bible", category: "input", label: "Design bible", glyph: "◫",
    accent: "var(--spark)", kind: "passive",
    defaults: { section_kind: "constraint", section_id: "" },
    ports: () => ({ out: [{ id: "o", label: "context", type: "prompt" }] }),
    body(n) {
      const kind = str(n, "section_kind", "constraint");
      const one = str(n, "section_id", "");
      return w.select(n, "section_kind", { label: "Sections", options: [
          { value: "constraint", label: "constraints (how it must look)" },
          { value: "pillar", label: "pillars (what the game is)" },
          { value: "loop", label: "core loop" },
          { value: "reference", label: "references" },
        ] })
        + w.text(n, "section_id", { label: "Only #", placeholder: "blank = all of that kind" })
        + w.note(one ? `section #${one}` : `every ${kind} in the bible, at run time`);
    },
    config() {
      return `<div class="wf-insp-p">Puts the design bible into a step's prompt. Leave <b>Only #</b> blank to send every section of that kind, or name one section's id to send just that.</div>`
        + `<div class="wf-insp-p">Resolved when the run happens, not when you draw it - edit the bible and the next run says the new thing.</div>`;
    },
  });

  WF.registerStep({
    type: "input.lore", category: "input", label: "Canon / lore", glyph: "◈",
    accent: "var(--c-narrative)", kind: "passive",
    defaults: { entity: "", include_facts: true },
    ports: () => ({ out: [{ id: "o", label: "canon", type: "prompt" }] }),
    body(n) {
      const slug = str(n, "entity", "");
      return w.text(n, "entity", { label: "Entity", placeholder: "a lore slug, e.g. tone-guide" })
        + w.toggle(n, "include_facts", { label: "Facts", value: true })
        + w.note(slug ? `${slug} - summary${cfg(n, "include_facts", true) ? " + its canon facts" : ""}`
                      : "name a lore entity");
    },
    config() {
      return `<div class="wf-insp-p">Sends a lore entity's summary and, optionally, its canon facts. A <b>locked</b> fact is sent as a MUST - the world has committed to it, so the step is told not to contradict it.</div>`;
    },
  });

  /* ===================================================================== */
  /* TEMPLATE: the comparison itself                                       */
  /* ===================================================================== */
  WF.registerTemplate({
    id: "tpl.compare", name: "Model comparison", category: "asset", glyph: "◉",
    hint: "one prompt → several models → look → pick",
    build() {
      return {
        nodes: [
          { id: "task", type: "input.task", x: 40, y: 200 },
          { id: "ref", type: "input.reference", x: 40, y: 420 },
          { id: "a", type: "model.image", x: 380, y: 40 },
          { id: "b", type: "model.image", x: 380, y: 420 },
          { id: "pick", type: "control.pick", x: 700, y: 240 },
        ],
        edges: [
          { from: ["task", "o"], to: ["a", "prompt"] },
          { from: ["task", "o"], to: ["b", "prompt"] },
          { from: ["ref", "o"], to: ["a", "ref"] },
          { from: ["ref", "o"], to: ["b", "ref"] },
          { from: ["a", "image"], to: ["pick", "candidates"] },
          { from: ["b", "image"], to: ["pick", "candidates"] },
        ],
      };
    },
  });
})();
