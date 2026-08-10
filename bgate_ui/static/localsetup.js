/* localsetup.js — two setup destinations that are NOT the same destination.
 *
 * ONE FILE, TWO SURFACES, AND THE SPLIT IS THE POINT. These shipped as a single
 * pane called "Local & agents", and the "and" was the tell: they were together
 * because they arrived in one task, not because they belong together.
 *
 *   Local generators   #lc-host, Settings → Local generators, and the bottom
 *                      half of Studio → Generators. The question is CAPABILITY:
 *                      can this machine make art right now. Read next to the
 *                      hosted providers, because "no key" and "nothing running
 *                      locally" are the two answers to one question.
 *
 *   Agent CLIs         #ag-host, Settings → Agent CLIs, and NOWHERE ELSE — it
 *                      is deliberately absent from Studio, which is about
 *                      making things. The question is PLUMBING: is Claude Code
 *                      installed and is Builders Gate registered with it,
 *                      against which interpreter. Touched once at setup and
 *                      then only when something breaks.
 *
 * A ComfyUI endpoint and an MCP registration have nothing in common past both
 * being local. They share this FILE because they share the fetch/paint/poll
 * machinery below; they share no pane, no heading and no summary line.
 *
 * IT DOES NOT START ANYTHING, AND THAT IS THE DESIGN. The dashboard is not a
 * process manager for services the user owns. The command is unknowable (a
 * conda env, a portable build, a .bat with a dozen flags), every interesting
 * failure is on the far side of it, and an orphan holding 8 GB of VRAM is worse
 * than a sentence telling you to start it yourself. So the loop is: CONFIGURE
 * HERE → START IT YOURSELF → THIS NOTICES. The noticing is a gentle poll
 * against a loopback socket, and it is the part that has to be good.
 *
 * WRITTEN FOR SOMEBODY WHO IS NOT A COMFYUI USER. Every field says what it is
 * FOR, in the same voice the settings registry uses for everything else, and no
 * word that only makes sense if you already know the answer goes unexplained.
 * The inspect panel exists because the app knew a great deal it never showed:
 * which GPU is actually being used, which checkpoints the install can see, and
 * — the one nothing else could tell you — WHICH NODES OF YOUR GRAPH BUILDERS
 * GATE OVERWRITES BEFORE SUBMITTING IT.
 *
 * REASONS ARE PRINTED VERBATIM. The adapters already write a sentence worth
 * showing ("nothing answered at http://127.0.0.1:8188 …"); collapsing that to a
 * lamp is what made this opaque. A lamp AND the sentence, always.
 *
 * Self-contained: injects its own <style>, mounts itself, rides SettingsView
 * and the Studio Generators tab. Colours are theme variables only, and anything
 * carrying text sits on --solid-N rather than --surface-N (orbit's ground is
 * translucent; text on it is unreadable).
 */
(function () {
  "use strict";

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (name, size) => (window.BGIcon ? BGIcon(name, { size: size || 14 }) : "");
  const say = (m, k) => { try { toast(m, k); } catch (e) { /* headless */ } };
  const q = s => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/"/g, "");

  /* A local socket answers in single-digit milliseconds, so this can be brisk
     — but it is still somebody's machine and this is a background panel. Six
     seconds is fast enough that starting ComfyUI in another window feels like
     the page noticed by itself, and slow enough to be invisible. */
  const POLL_MS = 6000;
  const STALE_MS = 4000;

  const TONE_WORD = {
    ready: "ready", unreachable: "not running", unhealthy: "problem",
    unconfigured: "not set up", configured: "not checked",
    unavailable: "unsupported",
  };

  /* ── styles ─────────────────────────────────────────────────────────────
     Injected here, not added to app.css: several modules are being edited in
     the same tree and a shared stylesheet is the file they collide in. */
  function injectStyle() {
    if (document.getElementById("lc-style")) return;
    const s = document.createElement("style");
    s.id = "lc-style";
    s.textContent = [
      ".lc-wrap{margin-top:22px}",
      ".lc-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}",
      ".lc-head h3{margin:0;font-size:15px;font-weight:var(--fw-semi);color:var(--text)}",
      ".lc-eyebrow{font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--text-3)}",
      ".lc-note{font-size:12px;color:var(--text-3);line-height:1.55;margin:0 0 14px;max-width:76ch}",
      ".lc-note code,.lc-mono{font-family:var(--mono);font-size:11px;color:var(--text-2)}",
      /* The one-line verdict, and it carries the same sentence bgate doctor
         prints. --solid-1 because it holds text over the orbit ground. */
      ".lc-sum{display:flex;align-items:flex-start;gap:8px;border:1px solid var(--line);background:var(--solid-1);border-radius:9px;padding:9px 11px;margin:0 0 13px;font-size:12px;color:var(--text-2);line-height:1.5}",
      ".lc-sum svg{flex:0 0 auto;margin-top:1px;color:var(--text-3)}",
      ".lc-sum.good{border-color:var(--good-line);background:var(--good-soft);color:var(--good)}",
      ".lc-sum.good svg{color:var(--good)}",
      ".lc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:12px;align-items:start}",
      /* --solid-2, not --surface-2: every card here carries text and the orbit
         ground is translucent behind it. */
      ".lc-card{border:1px solid var(--line);background:var(--solid-2);border-radius:11px;padding:13px 14px}",
      ".lc-card.s-ready{border-left:2px solid var(--good)}",
      ".lc-card.s-unreachable,.lc-card.s-unhealthy{border-left:2px solid var(--warn)}",
      ".lc-card.s-unconfigured,.lc-card.s-configured,.lc-card.s-unavailable{border-left:2px solid var(--line)}",
      ".lc-top{display:flex;align-items:center;gap:9px;margin-bottom:8px}",
      ".lc-top .lc-i{color:var(--text-3);display:inline-flex}",
      ".lc-name{font-size:13.5px;font-weight:var(--fw-semi);color:var(--text)}",
      ".lc-lamp{margin-left:auto;font-family:var(--mono);font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--text-3);white-space:nowrap}",
      ".lc-lamp.good{color:var(--good);border-color:var(--good-line);background:var(--good-soft)}",
      ".lc-lamp.warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}",
      ".lc-pills{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:9px}",
      ".lc-pill{font-family:var(--mono);font-size:9px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-3);border:1px solid var(--line);border-radius:999px;padding:2px 8px}",
      ".lc-pill.free{color:var(--good);border-color:var(--good-line)}",
      ".lc-what{font-size:11.5px;color:var(--text-3);line-height:1.55;margin-bottom:10px}",
      ".lc-why{font-size:11.5px;color:var(--warn);line-height:1.55;margin-bottom:10px;border-left:2px solid var(--warn-line);padding-left:9px}",
      ".lc-ok{font-size:11.5px;color:var(--good);line-height:1.55;margin-bottom:10px}",
      ".lc-start{border:1px dashed var(--line);border-radius:9px;padding:9px 11px;margin-bottom:11px;background:var(--solid-1)}",
      ".lc-start b{display:block;font-size:11px;color:var(--text-2);margin-bottom:5px;font-weight:var(--fw-semi)}",
      ".lc-start ol{margin:0;padding-left:17px}",
      ".lc-start li{font-size:11.5px;color:var(--text-3);line-height:1.6}",
      ".lc-f{border-top:1px solid var(--line);padding-top:9px;margin-top:9px}",
      ".lc-flab{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap;margin-bottom:4px}",
      ".lc-flab .n{font-size:12px;color:var(--text-2);font-weight:var(--fw-semi)}",
      ".lc-flab .v{font-family:var(--mono);font-size:9px;letter-spacing:.06em;color:var(--text-3)}",
      ".lc-req{font-family:var(--mono);font-size:8.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--warn)}",
      ".lc-fhelp{font-size:11px;color:var(--text-3);line-height:1.55;margin-bottom:7px;max-width:72ch}",
      ".lc-row{display:flex;gap:7px;align-items:center;flex-wrap:wrap}",
      ".lc-in,.lc-sel{flex:1 1 220px;min-width:0;background:var(--solid-1);border:1px solid var(--line);border-radius:8px;color:var(--text);font-family:var(--mono);font-size:11.5px;padding:7px 10px}",
      ".lc-in:focus,.lc-sel:focus{outline:none;border-color:var(--accent)}",
      ".lc-in::placeholder{color:var(--text-3)}",
      ".lc-in.bad{border-color:var(--bad-line)}",
      ".lc-btn{font:inherit;font-size:11.5px;padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--solid-3);color:var(--text-2);cursor:pointer;white-space:nowrap}",
      ".lc-btn:hover:not(:disabled){border-color:var(--accent);color:var(--text)}",
      ".lc-btn:disabled{opacity:.5;cursor:default}",
      ".lc-btn.go{border-color:var(--accent);color:var(--accent)}",
      ".lc-fnote{font-family:var(--mono);font-size:9.5px;color:var(--text-3);margin-top:5px}",
      ".lc-fnote .warn{color:var(--warn)}",
      ".lc-fnote .good{color:var(--good)}",
      ".lc-foot{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-top:11px;padding-top:9px;border-top:1px solid var(--line);font-family:var(--mono);font-size:10px;color:var(--text-3)}",
      ".lc-link{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent;cursor:pointer}",
      ".lc-link:hover{border-bottom-color:var(--accent)}",
      ".lc-sec{margin-bottom:20px}",
      ".lc-sech{display:flex;align-items:baseline;gap:9px;font-family:var(--mono);font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--text-3);margin-bottom:9px;padding-bottom:6px;border-bottom:1px solid var(--line)}",
      ".lc-sech .n{color:var(--text-2)}",
      ".lc-none{font-size:12px;color:var(--text-3);padding:10px 0}",
      /* ---- inspect ---- */
      ".lc-insp{margin-top:11px;border:1px solid var(--line);border-radius:9px;background:var(--solid-1);padding:11px 12px}",
      ".lc-ih{font-family:var(--mono);font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--text-3);margin:0 0 7px}",
      ".lc-ib{margin-bottom:13px}",
      ".lc-ib:last-child{margin-bottom:0}",
      ".lc-kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:11.5px;color:var(--text-3)}",
      ".lc-kv .k{font-family:var(--mono);font-size:10px;color:var(--text-3);white-space:nowrap}",
      ".lc-kv .v{color:var(--text-2);word-break:break-word}",
      ".lc-chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}",
      ".lc-chip{font-family:var(--mono);font-size:9.5px;color:var(--text-2);border:1px solid var(--line);border-radius:6px;padding:2px 7px;background:var(--solid-2)}",
      ".lc-chip.more{color:var(--text-3);border-style:dashed}",
      ".lc-node{display:flex;gap:8px;align-items:baseline;font-size:11.5px;padding:4px 0;border-bottom:1px solid var(--line)}",
      ".lc-node:last-child{border-bottom:0}",
      ".lc-node .id{font-family:var(--mono);font-size:9.5px;color:var(--text-3);min-width:26px}",
      ".lc-node .cls{color:var(--text-2);font-weight:var(--fw-semi)}",
      ".lc-node .ttl{color:var(--text-3)}",
      ".lc-inj{color:var(--accent);font-family:var(--mono);font-size:10px}",
      ".lc-shots{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}",
      ".lc-shots img{width:62px;height:62px;object-fit:cover;border-radius:6px;border:1px solid var(--line);background:var(--solid-2)}",
      ".lc-cmd{font-family:var(--mono);font-size:10.5px;color:var(--text-2);background:var(--solid-1);border:1px solid var(--line);border-radius:7px;padding:8px 10px;margin:7px 0;word-break:break-all;line-height:1.6}",
      ".lc-matrix{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:16px}",
      ".lc-mcell{border:1px solid var(--line);border-radius:9px;background:var(--solid-2);padding:8px 11px;min-width:132px}",
      ".lc-mcell .cap{font-size:11.5px;color:var(--text-2);margin-bottom:3px}",
      ".lc-mcell .st{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;color:var(--text-3)}",
      ".lc-mcell .st.good{color:var(--good)}",
    ].join("");
    document.head.appendChild(s);
  }

  /* ── small renderers ─────────────────────────────────────────────────── */

  function kv(pairs) {
    const rows = pairs.filter(p => p && p[1] !== "" && p[1] != null)
      .map(p => `<span class="k">${esc(p[0])}</span><span class="v">${esc(p[1])}</span>`);
    return rows.length ? `<div class="lc-kv">${rows.join("")}</div>` : "";
  }

  function chips(items, cap) {
    const list = items || [];
    if (!list.length) return `<div class="lc-none">none reported</div>`;
    const shown = list.slice(0, cap || 14).map(v =>
      `<span class="lc-chip">${esc(v)}</span>`).join("");
    const rest = list.length - (cap || 14);
    return `<div class="lc-chips">${shown}${rest > 0
      ? `<span class="lc-chip more">+${rest} more</span>` : ""}</div>`;
  }

  /* ── the local runtime card ──────────────────────────────────────────── */

  function fieldRow(rt, f) {
    const id = `${rt.id}::${f.env}`;
    const control = f.kind === "choice"
      ? `<select class="lc-sel" data-lc-in="${esc(id)}">
           <option value="">— not declared —</option>
           ${(f.choices || []).map(c =>
             `<option value="${esc(c)}"${c === f.value ? " selected" : ""}>${esc(c)}</option>`
           ).join("")}
         </select>`
      : `<input class="lc-in${f.exists === false ? " bad" : ""}" type="text"
               data-lc-in="${esc(id)}" spellcheck="false" autocomplete="off"
               value="${esc(f.value)}"
               aria-label="${esc(f.label)}"
               placeholder="${esc(f.placeholder || "")}">`;

    /* WHICH LAYER IS IN FORCE, said out loud. `shadowed` is the one that costs
       an afternoon: the .env has a value and a shell variable set to empty is
       beating it, so the page shows a path and the adapter sees nothing. */
    let note = "";
    if (f.source === "environment") {
      note = `<span class="warn">set in your shell environment, not this
        project's .env — this page can overwrite it for the .env, but the shell
        keeps winning until you unset it and restart</span>`;
    } else if (f.source === "shadowed") {
      note = `<span class="warn">${esc(f.env)} is set to an EMPTY value in this
        shell, which beats the .env — unset it and restart the dashboard</span>`;
    } else if (f.source === "env_file") {
      note = `saved in this project's <code>.env</code>`;
    } else if (f.using_default) {
      note = `not set — using the default <code>${esc(f.default)}</code>`;
    } else {
      note = `not set`;
    }
    if (f.exists === false && f.value) {
      note = `<span class="warn">that file does not exist</span> · ` + note;
    } else if (f.exists === true) {
      note = `<span class="good">file found</span> · ` + note;
    }

    return `<div class="lc-f">
      <div class="lc-flab">
        <span class="n">${esc(f.label)}</span>
        ${f.required ? `<span class="lc-req">required</span>` : ""}
        <span class="v">${esc(f.env)}</span>
      </div>
      <div class="lc-fhelp">${esc(f.help)}</div>
      <div class="lc-row">
        ${control}
        <button class="lc-btn go" data-lc-act="save" data-lc-id="${esc(id)}">save</button>
        ${f.value ? `<button class="lc-btn" data-lc-act="clear" data-lc-id="${esc(id)}">clear</button>` : ""}
      </div>
      <div class="lc-fnote">${note}</div>
    </div>`;
  }

  function runtimeCard(rt, open) {
    const lamp = rt.tone === "good" ? "good" : (rt.tone === "warn" ? "warn" : "");
    const pills = (rt.power_labels || []).map(p =>
      `<span class="lc-pill">${esc(p)}</span>`).join("")
      + `<span class="lc-pill free">$0 · stays on this machine</span>`;

    /* The start instructions appear exactly when they are the next thing to do:
       the setup is complete and nothing is answering. Showing them on a card
       that is already generating is noise; showing them on a card with no
       workflow set is the wrong instruction. */
    const showStart = rt.stage === "unreachable" || rt.stage === "configured";
    const start = showStart && (rt.start || []).length
      ? `<div class="lc-start"><b>${icon("run", 12)} Then start it yourself —
           Builders Gate does not launch it</b>
           <ol>${rt.start.map(s => `<li>${esc(s)}</li>`).join("")}</ol></div>`
      : "";

    return `<div class="lc-card s-${esc(rt.stage)}" data-lc-card="${esc(rt.id)}">
      <div class="lc-top">
        <span class="lc-i">${icon(rt.powers.indexOf("model_3d") >= 0 ? "model" : "art", 15)}</span>
        <span class="lc-name">${esc(rt.label)}</span>
        <span class="lc-lamp ${lamp}">${esc(TONE_WORD[rt.stage] || rt.stage)}</span>
      </div>
      <div class="lc-pills">${pills}</div>
      <div class="lc-what">${esc(rt.what)}</div>
      ${rt.reason ? `<div class="lc-why">${esc(rt.reason)}</div>` : ""}
      ${rt.stage === "ready"
        ? `<div class="lc-ok">Answering at ${esc(rt.url)}. Generations through
           this cost nothing and send nothing anywhere.</div>` : ""}
      ${start}
      ${rt.stage === "unavailable" ? "" :
        (rt.fields || []).map(f => fieldRow(rt, f)).join("")}
      <div class="lc-foot">
        <span>${esc(rt.url || "")}</span>
        ${rt.software === "comfy" && rt.stage !== "unavailable"
          ? `<a class="lc-link" data-lc-act="inspect" data-lc-id="${esc(rt.id)}">${
             open ? "hide details" : "what is it doing? ↓"}</a>` : ""}
        ${rt.docs_url ? `<a class="lc-link" href="${esc(rt.docs_url)}"
            target="_blank" rel="noopener noreferrer">docs ↗</a>` : ""}
      </div>
      <div data-lc-insp="${esc(rt.id)}">${open ? LC.inspectHtml(rt.id) : ""}</div>
    </div>`;
  }

  /* ── the inspect panel: everything the app knew and never showed ─────── */

  function workflowBlock(wf) {
    if (wf.error === "not set") {
      return `<div class="lc-ib"><p class="lc-ih">${esc(wf.label)}</p>
        <div class="lc-none">not set yet — once it is, this is where you can see
        what is in it and which parts of it get overwritten</div></div>`;
    }
    if (wf.error) {
      return `<div class="lc-ib"><p class="lc-ih">${esc(wf.label)}</p>
        <div class="lc-why">${esc(wf.error)}</div></div>`;
    }
    const injected = (wf.injected || []).map(n =>
      `<div class="lc-node"><span class="id">#${esc(n.id)}</span>
         <span><span class="cls">${esc(n.class_type)}</span>
         ${n.title && n.title !== n.class_type ? `<span class="ttl"> · ${esc(n.title)}</span>` : ""}
         ${n.injected.map(m => `<div class="lc-inj">↳ ${esc(m.field)} ← ${esc(m.what)}</div>`).join("")}
         </span></div>`).join("");
    const others = (wf.nodes || []).filter(n => !n.injected.length);
    return `<div class="lc-ib">
      <p class="lc-ih">${esc(wf.label)} — runs ${esc(wf.runs_when)}</p>
      ${kv([
        ["file", wf.path],
        ["format", wf.format === "api" ? "API format (correct)" : wf.format],
        ["nodes", String(wf.node_count)],
        ["loads", (wf.weights || []).join(", ")],
      ])}
      ${injected ? `<p class="lc-ih" style="margin-top:9px">Builders Gate
        overwrites these before every run</p>${injected}` : ""}
      ${others.length ? `<p class="lc-ih" style="margin-top:9px">the rest of the
        graph, untouched</p>${chips(others.map(n => `#${n.id} ${n.class_type}`), 18)}` : ""}
      ${(wf.warnings || []).map(w => `<div class="lc-why">${esc(w)}</div>`).join("")}
    </div>`;
  }

  function inspectBody(d) {
    if (!d) return `<div class="lc-insp"><div class="lc-none">reading…</div></div>`;
    if (d.__error) {
      return `<div class="lc-insp"><div class="lc-why">${esc(d.__error)}</div></div>`;
    }
    const s = d.server || {};
    const cat = (d.catalogue || {}).groups || {};
    const hist = d.history || {};
    const lic = d.licence || null;

    const server = s.ok
      ? `<div class="lc-ib"><p class="lc-ih">the server</p>
          <div class="lc-what" style="margin-bottom:6px">${esc(s.verdict)}</div>
          ${kv([
            ["ComfyUI", s.comfyui_version], ["PyTorch", s.pytorch_version],
            ["Python", s.python_version], ["OS", s.os],
          ])}
          ${(s.devices || []).length ? chips((s.devices || []).map(x =>
            `${x.name || x.type}${x.vram_total_gb ? ` · ${x.vram_total_gb} GB` : ""}`)) : ""}
        </div>`
      : `<div class="lc-ib"><p class="lc-ih">the server</p>
          <div class="lc-why">${esc(s.error || "not reachable")}</div></div>`;

    const busy = (d.queue && d.queue.ok)
      ? `<div class="lc-ib"><p class="lc-ih">right now</p>
          <div class="lc-what">${esc(d.queue.verdict)}. A local generator that
          looks frozen is usually third in a queue.</div></div>` : "";

    const licence = lic
      ? `<div class="lc-ib"><p class="lc-ih">what you may ship</p>
          ${kv([["declared model", lic.model || "(none)"],
                ["licence", lic.code], ["means", lic.summary]])}
          ${lic.url ? `<div class="lc-chips"><a class="lc-link"
             href="${esc(lic.url)}" target="_blank" rel="noopener noreferrer">the
             licence itself ↗</a></div>` : ""}
        </div>` : "";

    /* Only asked when the server answered its stats — see localruntimes.inspect.
       An absent catalogue on a server that is down is not a version difference
       and must not be reported as one. */
    const groups = Object.keys(cat).filter(k => (cat[k].items || []).length);
    const catalogue = !s.ok ? ""
      : groups.length
        ? `<div class="lc-ib"><p class="lc-ih">what this install can see</p>
            ${groups.map(k => `<div style="margin-bottom:7px">
              <div class="lc-fhelp" style="margin-bottom:3px"><b>${esc(k)}</b> —
                ${esc(cat[k].help)}</div>${chips(cat[k].items)}</div>`).join("")}
          </div>`
        : `<div class="lc-ib"><p class="lc-ih">what this install can see</p>
            <div class="lc-none">this build did not answer the node query — that
            is a difference in ComfyUI versions, not a fault in your setup</div>
          </div>`;

    const runs = (hist.runs || []).filter(r => r.images.length);
    const recent = runs.length
      ? `<div class="lc-ib"><p class="lc-ih">the last few things it made</p>
          <div class="lc-shots">${runs.slice(0, 4).flatMap(r =>
            r.images.slice(0, 2).map(im =>
              `<img loading="lazy" src="${esc(im.url)}" alt="${esc(im.filename)}"
                    title="${esc(im.filename)}">`)).join("")}</div>
          <div class="lc-fnote">served straight from ComfyUI's own output
            folder — everything it made, not only what Builders Gate asked
            for</div></div>` : "";

    return `<div class="lc-insp">
      ${server}${busy}
      ${(d.workflows || []).map(workflowBlock).join("")}
      ${licence}${catalogue}${recent}
    </div>`;
  }

  /* ── the coding-agent card ───────────────────────────────────────────── */

  function agentCard(r, why) {
    const m = r.mcp || {};
    const wired = !!m.ok;
    const lamp = !r.installed ? "" : (wired ? "good" : "warn");
    const word = !r.installed ? "not installed" : (wired ? "wired" : "check wiring");
    return `<div class="lc-card s-${wired ? "ready" : (r.installed ? "unhealthy" : "unconfigured")}"
                 data-lc-agent="${esc(r.id)}">
      <div class="lc-top">
        <span class="lc-i">${icon("agents", 15)}</span>
        <span class="lc-name">${esc(r.label)}</span>
        <span class="lc-lamp ${lamp}">${esc(word)}</span>
      </div>
      <div class="lc-pills">
        ${r.default_runner ? `<span class="lc-pill">default runner</span>` : ""}
        <span class="lc-pill">${r.steerable ? "steerable mid-run" : "no live steering"}</span>
        <span class="lc-pill">${r.cost_tracked ? "cost tracked" : "cost NOT tracked"}</span>
      </div>
      <div class="lc-what">${esc(r.used_for)} ${esc(r.note || "")}</div>
      ${r.installed
        ? kv([["found at", r.path]])
        : `<div class="lc-why">Not on PATH. Install it and reload this page —
             nothing here installs software.</div>`}

      <div class="lc-f">
        <div class="lc-flab"><span class="n">Builders Gate MCP server</span>
          <span class="v">${esc(m.scope_note || "")}</span></div>
        <div class="lc-fhelp">${esc(m.how || "")}</div>
        <div class="${wired ? "lc-ok" : "lc-why"}">${esc(m.verdict || "")}</div>
        ${m.command ? kv([["registered command", m.command],
                          ["args", (m.args || []).join(" ")],
                          ["config file", m.path]]) : ""}
        ${!wired ? `<div class="lc-fhelp" style="margin-top:8px">${esc(why || "")}</div>` : ""}
        <div class="lc-cmd">${esc(m.command_line || "")}</div>
        <div class="lc-row">
          <button class="lc-btn go" data-lc-act="register" data-lc-id="${esc(r.id)}"
            ${m.can_register ? "" : "disabled"}>${m.found ? "re-register, pinned" : "register"}</button>
          ${m.found ? `<button class="lc-btn" data-lc-act="verify" data-lc-id="${esc(r.id)}">verify</button>` : ""}
          ${m.found ? `<button class="lc-btn" data-lc-act="unregister" data-lc-id="${esc(r.id)}">remove</button>` : ""}
          <button class="lc-btn" data-lc-act="copy" data-lc-id="${esc(r.id)}">copy the command</button>
        </div>
        <div class="lc-fnote" data-lc-said="${esc(r.id)}"></div>
      </div>
    </div>`;
  }

  /* ── the module ──────────────────────────────────────────────────────── */

  const LC = {
    data: null, agents: null, _read: 0, _aread: 0,
    _busy: "", _reading: false, _open: {}, _insp: {},
    hosts: {}, _timer: 0,

    async load(force) {
      if (this._reading) return this.data;
      if (!force && this.data && Date.now() - this._read < STALE_MS) return this.data;
      this._reading = true;
      try {
        this.data = await window.readJSON("/api/local/runtimes?probe=1",
          { runtimes: [], capabilities: {} });
      } finally { this._reading = false; this._read = Date.now(); }
      return this.data;
    },

    async loadAgents(force) {
      if (!force && this.agents && Date.now() - this._aread < 30000) return this.agents;
      this.agents = await window.readJSON("/api/local/agents", { runners: [] });
      this._aread = Date.now();
      return this.agents;
    },

    mount(where, host) {
      if (!host) return false;
      injectStyle();
      this.hosts[where] = host;
      if (!host.dataset.lcWired) {
        host.dataset.lcWired = "1";
        host.addEventListener("click", e => {
          const hit = e.target && e.target.closest && e.target.closest("[data-lc-act]");
          if (!hit || hit.disabled) return;
          const act = hit.dataset.lcAct, id = hit.dataset.lcId;
          if (act === "save") this.save(id, where, hit);
          else if (act === "clear") this.clear(id, where, hit);
          else if (act === "inspect") this.toggle(id, where);
          else if (act === "register") this.register(id, where, hit);
          else if (act === "unregister") this.unregister(id, where, hit);
          else if (act === "verify") this.verify(id, where, hit);
          else if (act === "copy") this.copy(id, where);
        });
        host.addEventListener("keydown", e => {
          if (e.key !== "Enter") return;
          const input = e.target && e.target.closest && e.target.closest("[data-lc-in]");
          if (!input) return;
          e.preventDefault();
          this.save(input.dataset.lcIn, where, null);
        });
      }
      return true;
    },

    /* ---- Settings → Local generators ---- */
    async activate(host) {
      const el = host || document.getElementById("lc-host");
      if (!this.mount("generators", el)) return false;
      if (!this.data) {
        el.innerHTML = `<div class="lc-wrap"><div class="lc-none">reading local
          setup…</div></div>`;
      }
      await this.load(true);
      this.paint("generators");
      this.drive();
      return true;
    },

    /* ---- Settings → Agent CLIs ----
     * A SEPARATE ENTRY POINT, not a section of the one above. No poll: a CLI
     * does not start answering while you watch, and this pane is read once at
     * setup and then only when something breaks. */
    async activateAgents(host) {
      const el = host || document.getElementById("ag-host");
      if (!this.mount("agents", el)) return false;
      if (!this.agents) {
        el.innerHTML = `<div class="lc-wrap"><div class="lc-none">reading the
          coding-agent CLIs…</div></div>`;
      }
      await this.loadAgents(true);
      this.paint("agents");
      return true;
    },

    /* ---- Studio: appended under the hosted providers, same question ----
     * GENERATORS ONLY. Agent CLIs are deliberately absent here: Studio answers
     * "what can make a 2D image right now", and an MCP registration makes
     * nothing. */
    async studio(host) {
      if (!this.mount("studio", host)) return false;
      await this.load(true);
      this.paint("studio");
      this.drive();
      return true;
    },

    /* THE POLL IS THE FEATURE. "Configure here, start it yourself, this
       notices" is only true if something is asking. It stops when the tab is
       hidden and when no GENERATOR host is on screen — the agents pane is not
       counted, because probing somebody's loopback for a panel about MCP
       registrations would be work nothing on screen is waiting for. */
    drive() {
      if (this._timer) return;
      this._timer = setInterval(async () => {
        if (document.hidden) return;
        const live = ["generators", "studio"].some(w => {
          const h = this.hosts[w];
          return h && h.isConnected && h.offsetParent !== null;
        });
        if (!live) return;
        const before = this._signature();
        await this.load(true);
        if (this._signature() !== before) this.repaint();
      }, POLL_MS);
    },

    /* Every host that draws runtimes. Not the agents pane — nothing in a
       runtime read changes it, and repainting it would drop the verify/register
       result line the user is reading. */
    repaint() {
      ["generators", "studio"].forEach(w => this.paint(w));
    },

    /* Repaint only on a real change: a poll that rewrites innerHTML every six
       seconds throws away whatever the user was typing into a path field. */
    _signature() {
      const rows = ((this.data || {}).runtimes) || [];
      return rows.map(r => `${r.id}:${r.stage}:${r.reason}`).join("|");
    },

    paint(where) {
      const host = this.hosts[where];
      if (!host) return;
      if (where === "agents") {
        const a = this.agents || {};
        host.innerHTML = a.__error
          ? `<div class="lc-wrap"><div class="lc-none">could not read the
             coding-agent CLIs — ${esc(a.__error)}</div></div>`
          : this.agentsHtml(a);
        return;
      }
      const d = this.data || {};
      if (d.__error) {
        host.innerHTML = `<div class="lc-wrap"><div class="lc-none">could not read
          the local setup — ${esc(d.__error)}</div></div>`;
        return;
      }
      host.innerHTML = where === "studio" ? this.studioHtml(d)
                                          : this.generatorsHtml(d);
    },

    storageNote() {
      return `<p class="lc-note">These are addresses and file paths, so unlike an
        API key they are shown back to you in full — a path you cannot read is a
        path you cannot check for the typo. They are written to <code>.env</code>
        at the game project root, the same file the keys live in, and take effect
        immediately with no restart. <b>Nothing here starts or stops anything.</b>
        Builders Gate talks to software you run; it does not run it, so it can
        never leave a model loaded in your GPU after you close this page.</p>`;
    },

    /* ONE LINE, AND IT IS THE SAME LINE `bgate doctor` PRINTS — built by
       localruntimes.summary and shipped in the payload rather than re-derived
       here. Two phrasings of one fact ("0 of 5 ready" vs "3 not set up; 2 set
       up, not running") is its own kind of confusion, and the reader has no way
       to tell whether they disagree. */
    summaryLine(d) {
      const s = d.summary || {};
      if (!s.detail) return "";
      return `<div class="lc-sum ${s.available ? "good" : ""}">
        ${icon("doctor", 13)}<span>${esc(s.detail)}</span></div>`;
    },

    generatorsHtml(d) {
      const rows = d.runtimes || [];
      return `<div class="lc-wrap">
        <div class="lc-head">
          <span class="lc-eyebrow">No key, no bill, nothing leaves the machine</span>
          <h3>Local generators</h3>
        </div>
        ${this.summaryLine(d)}
        ${this.storageNote()}
        <div class="lc-grid">${rows.length
          ? rows.map(r => runtimeCard(r, !!this._open[r.id])).join("")
          : `<div class="lc-none">no local runtimes are registered in this
             build.</div>`}</div>
      </div>`;
    },

    agentsHtml(a) {
      const rows = a.runners || [];
      const wired = rows.filter(r => r.installed && r.mcp && r.mcp.ok).length;
      const on = rows.filter(r => r.installed).length;
      return `<div class="lc-wrap">
        <div class="lc-head">
          <span class="lc-eyebrow">Installation &amp; wiring</span>
          <h3>Agent CLIs</h3>
        </div>
        <div class="lc-sum ${wired ? "good" : ""}">${icon("doctor", 13)}
          <span>${on} of ${rows.length} installed, ${wired} wired to this
          interpreter</span></div>
        <p class="lc-note">Registering an MCP server writes to that CLI's own
          config in your home directory, not to this project — every future
          session of it, in any directory, gets the Builders Gate tools. It is
          pinned to the interpreter this dashboard runs on, which is the part
          that is usually wrong.</p>
        <div class="lc-grid">${rows.length
          ? rows.map(r => agentCard(r, a.why_absolute)).join("")
          : `<div class="lc-none">no coding-agent CLI is described in this
             build.</div>`}</div>
      </div>`;
    },

    /* GROUPED BY WHAT IT MAKES, matching providerkeys' Studio framing, because
       on Studio the question is "why can I not make a 3D model" and the answer
       is every generator that makes one — rented or local — with its state. */
    studioHtml(d) {
      const caps = d.capabilities || {};
      const rows = d.runtimes || [];
      const secs = Object.keys(caps).map(capId => {
        const mine = rows.filter(r => (r.powers || []).indexOf(capId) >= 0);
        if (!mine.length) return "";
        const live = mine.filter(r => r.available).length;
        return `<div class="lc-sec"><div class="lc-sech">
            <span>${esc(caps[capId])}</span>
            <span class="n">${live} of ${mine.length} running here</span></div>
          <div class="lc-grid">${mine.map(r =>
            runtimeCard(r, !!this._open[r.id])).join("")}</div></div>`;
      }).join("");
      return `<div class="lc-wrap">
        <div class="lc-head">
          <span class="lc-eyebrow">No key, no bill, nothing leaves the machine</span>
          <h3>On this machine</h3>
        </div>
        <p class="lc-note">The other half of the answer above. A capability is
          available if EITHER a provider has a key or something local is running
          — these are the local ones. Set them up in
          <b>Settings → Local generators</b>.</p>
        ${secs || `<div class="lc-none">nothing local is registered.</div>`}
      </div>`;
    },

    /* ---- inspect ---- */
    inspectHtml(id) { return inspectBody(this._insp[id]); },

    async toggle(id, where) {
      this._open[id] = !this._open[id];
      this.repaint();
      if (!this._open[id]) return;
      const d = await window.readJSON(
        `/api/local/runtimes/${encodeURIComponent(id)}/inspect`, null);
      this._insp[id] = d || { __error: "the inspect read returned nothing" };
      /* Patch only the panel, not the page: a full repaint here would close
         every other card and drop anything half-typed. */
      ["generators", "studio"].forEach(w => {
        const host = this.hosts[w];
        if (!host) return;
        const slot = host.querySelector(`[data-lc-insp="${q(id)}"]`);
        if (slot) slot.innerHTML = this.inspectHtml(id);
      });
    },

    /* ---- writes ---- */
    split(id) {
      const at = String(id || "").indexOf("::");
      return at < 0 ? ["", ""] : [id.slice(0, at), id.slice(at + 2)];
    },

    input(id, where) {
      const host = this.hosts[where];
      return host ? host.querySelector(`[data-lc-in="${q(id)}"]`) : null;
    },

    async save(id, where, button) {
      if (this._busy) return;
      const [runtime, env] = this.split(id);
      if (!runtime || !env) return;
      const field = this.input(id, where);
      const value = field ? field.value : "";
      this._busy = id;
      const r = await window.mutate(
        `/api/local/runtimes/${encodeURIComponent(runtime)}/config`,
        { method: "POST", body: { env, value }, button, quiet: true });
      this._busy = "";
      if (!r.ok) { say(r.error); return; }
      this.data = r.data; this._read = Date.now();
      delete this._insp[runtime];
      this.repaint();
      const now = (r.data.runtimes || []).filter(x => x.id === runtime)[0] || {};
      if (!value.trim()) say("cleared", "ok");
      else if (now.stage === "ready") say(`${now.label} is ready`, "ok");
      else say(`saved — ${now.reason || now.stage_label || "still not ready"}`);
    },

    async clear(id, where, button) {
      if (this._busy) return;
      const [runtime, env] = this.split(id);
      if (!runtime || !env) return;
      this._busy = id;
      const r = await window.mutate(
        `/api/local/runtimes/${encodeURIComponent(runtime)}/config?env=${encodeURIComponent(env)}`,
        { method: "DELETE", button, quiet: true });
      this._busy = "";
      if (!r.ok) { say(r.error); return; }
      this.data = r.data; this._read = Date.now();
      delete this._insp[runtime];
      this.repaint();
      say("cleared", "ok");
    },

    /* ---- coding agents ---- */
    said(id, text, tone) {
      const host = this.hosts.agents;
      if (!host) return;
      const slot = host.querySelector(`[data-lc-said="${q(id)}"]`);
      if (slot) slot.innerHTML = `<span class="${tone || ""}">${esc(text)}</span>`;
    },

    async register(id, where, button) {
      if (this._busy) return;
      if (typeof window.askConfirm === "function") {
        const yes = await window.askConfirm({
          title: `Register Builders Gate with ${id}?`,
          body: `This writes to that CLI's own config in your home directory,
                 not to this project. Every future session of it — in any
                 directory, on any project — gets the Builders Gate tools. It is
                 pinned to the interpreter this dashboard is running on, which
                 is the part that is usually wrong. You can remove it again from
                 here.`,
          ok: "register it", cancel: "not now",
        });
        if (!yes) return;
      }
      this._busy = id;
      this.said(id, "asking the CLI to register it…");
      const r = await window.mutate(
        `/api/local/agents/${encodeURIComponent(id)}/register`,
        { method: "POST", button, quiet: true });
      this._busy = "";
      if (!r.ok) { this.said(id, r.error, "warn"); say(r.error); return; }
      this.agents = r.data; this._aread = Date.now();
      this.paint("agents");
      say("registered — restart that CLI before the tools appear", "ok");
      this.said(id, "registered. A CLI already running will not see it until "
                  + "you restart it.", "good");
    },

    async unregister(id, where, button) {
      if (this._busy) return;
      this._busy = id;
      const r = await window.mutate(
        `/api/local/agents/${encodeURIComponent(id)}/register`,
        { method: "DELETE", button, quiet: true });
      this._busy = "";
      if (!r.ok) { this.said(id, r.error, "warn"); return; }
      this.agents = r.data; this._aread = Date.now();
      this.paint("agents");
      say("removed", "ok");
    },

    async verify(id, where, button) {
      if (this._busy) return;
      this._busy = id;
      this.said(id, "asking that interpreter whether it can load the server…");
      const r = await window.mutate(
        `/api/local/agents/${encodeURIComponent(id)}/verify`,
        { method: "POST", button, quiet: true });
      this._busy = "";
      if (!r.ok) { this.said(id, r.error, "warn"); return; }
      const d = r.data || {};
      this.said(id, d.ok ? d.detail : `${d.error || "it could not"} — ${d.output || ""}`,
                d.ok ? "good" : "warn");
    },

    copy(id) {
      const row = ((this.agents || {}).runners || []).filter(r => r.id === id)[0];
      const line = row && row.mcp ? row.mcp.command_line : "";
      if (!line) return;
      try {
        navigator.clipboard.writeText(line);
        say("command copied", "ok");
      } catch (e) { say("could not reach the clipboard — select it by hand"); }
    },
  };

  window.LocalSetup = LC;

  /* ── wiring ───────────────────────────────────────────────────────────
     Both entry points are WRAPPERS around modules that already own their host,
     for the reason providerkeys.js states: index.html and settingsview.js are
     contended files, and a wrapper costs them one <div> and one <script> each
     instead of an edit inside a function somebody else is holding. */

  /* Studio: the hosted providers tab paints, then we append the local half
     underneath it. Re-appended after every one of its repaints, because it
     rewrites its host's innerHTML when a key is saved. */
  function rideStudio() {
    const flows = window.StudioFlows;
    const pk = window.ProviderKeys;
    if (!flows || !flows.providers || !pk || pk.__lcWrapped) return !!flows;
    const priorBuild = flows.providers.build;
    flows.providers.build = function (host) {
      const out = priorBuild.call(this, host);
      setTimeout(() => attachStudio(host), 0);
      return out;
    };
    const priorPaint = pk.paint.bind(pk);
    pk.paint = function (where) {
      const out = priorPaint(where);
      if (where === "studio") setTimeout(() => attachStudio(pk.hosts.studio), 0);
      return out;
    };
    pk.__lcWrapped = true;
    return true;
  }

  function attachStudio(host) {
    if (!host) return;
    let slot = host.querySelector("#lc-studio-host");
    if (!slot) {
      slot = document.createElement("div");
      slot.id = "lc-studio-host";
      host.appendChild(slot);
    }
    try { LC.studio(slot); } catch (e) { console.warn("[localsetup]", e); }
  }

  /* Settings: ride SettingsView.activate, exactly as providerkeys does. Both
     panes and both host elements are declared in index.html and MOVED by
     settingsview.js — never rebuilt, because a fresh div with the same id looks
     identical and is dead.

     BOTH ARE MOUNTED ON EVERY ACTIVATE even though at most one is on screen.
     They are cheap (one fetch each, cached), and the alternative is asking
     which pane is showing — which means this module knowing settingsview's pane
     constants, which is the coupling the moved-host contract exists to avoid.
     The one that is parked hidden paints into a detached-but-live element and
     is correct the instant it is attached. */
  function rideSettings() {
    const view = window.SettingsView;
    if (!view || view.__lcWrapped) return !!view;
    const prior = view.activate.bind(view);
    view.activate = function (container) {
      const out = prior(container);
      try { LC.activate(); } catch (e) { console.warn("[localsetup]", e); }
      try { LC.activateAgents(); } catch (e) { console.warn("[localsetup]", e); }
      return out;
    };
    view.__lcWrapped = true;
    return true;
  }

  function wire() { rideSettings(); rideStudio(); }
  if (!(window.SettingsView && window.ProviderKeys)) {
    document.addEventListener("DOMContentLoaded", wire);
    window.addEventListener("load", wire);
  }
  wire();
})();
