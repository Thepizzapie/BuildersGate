/* overview_history.js — the Work history panel on the command deck.
 *
 * WHAT WAS MISSING. The Overview could say what is running and what happened in
 * the last nine log lines. It could not say what has been FINISHED, how it
 * finished, or whether anybody checked it — which is the only view that answers
 * "what did the studio actually get done". 279 finished items on this project
 * and no surface for a single one of them.
 *
 * HONESTY IS THE FEATURE. Every row carries a verdict, and the verdict is read
 * off real evidence (see bgate_ui/routes/history.py for exactly what counts as
 * one). Where nothing independent ever looked at the work — which under
 * `gate.mode = none` is MOST of it — the row says "no gate · closed on the
 * agent's own word" in plain sight. It is deliberately not a blank cell and it
 * is deliberately not green. A dashboard that implies verification which did
 * not happen is worse than one that shows nothing.
 *
 * THE LOG IS WINDOWED, NEVER DUMPED. The biggest transcript on this project is
 * 60MB across 3258 stream-json lines, several of them 40KB each. The server
 * indexes the file once (streamed, by byte offset) and hands back at most 60
 * parsed steps per request; the browser never sees the rest. Search runs
 * server-side over the index and returns match INDICES, so walking 16 hits
 * costs one fetch each and never re-reads the file.
 *
 * SELF-CONTAINED ON PURPOSE. Its own <style>, mounted into #view-overview at
 * runtime, so the only shared-file edit this feature needed was one <script>
 * tag. Colours come from custom properties only — a hardcoded hex breaks the
 * moment the theme changes underneath it.
 *
 * Registered as window.OverviewHistory.
 */
window.OverviewHistory = (() => {
  "use strict";

  const E = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (name, size) => (typeof BGIcon === "function"
    ? BGIcon(name, { size: size || 15 }) : "");
  const seatColor = seat => seat
    ? `var(--c-${seat}, var(--text-2))` : "var(--text-2)";

  const PAGE = 40;          // history rows per fetch
  const STEPS = 60;         // log steps per window — the server caps at 300
  const POLL_MS = 20000;    // only ticks while the deck is on Overview

  /* SQLite hands back `YYYY-MM-DD HH:MM:SS` in UTC with no zone marker, which
     every browser then reads as local time — an item finished a minute ago
     showed as "in 5 hours". The Z is not optional. */
  function when(stamp) {
    if (!stamp) return "";
    const t = Date.parse(String(stamp).replace(" ", "T") + "Z");
    if (!Number.isFinite(t)) return String(stamp);
    const secs = Math.max(0, (Date.now() - t) / 1000);
    if (secs < 90) return "just now";
    if (secs < 5400) return `${Math.round(secs / 60)}m ago`;
    if (secs < 172800) return `${Math.round(secs / 3600)}h ago`;
    return `${Math.round(secs / 86400)}d ago`;
  }
  const stampTitle = s => s ? `${s} UTC` : "";
  const bytes = n => !n ? "no log"
    : n < 1024 ? `${n} B`
    : n < 1048576 ? `${Math.round(n / 1024)} KB`
    : `${(n / 1048576).toFixed(1)} MB`;

  /* Outcome — how the work FINISHED. Distinct from the verdict, which is who
     checked it. Conflating the two is how "done" came to read as "verified". */
  const OUTCOME = {
    done:      { label: "done",       tone: "ok" },
    failed:    { label: "failed",     tone: "bad" },
    cancelled: { label: "cancelled",  tone: "mute" },
    review:    { label: "held",       tone: "live" },
  };
  const VERDICT_TONE = {
    pass: "good", approved: "good", fail: "bad", error: "warn",
    unknown: "warn", escalated: "warn", reviewing: "live",
    ungated: "none", na: "mute", none: "mute",
  };
  const STEP_LABEL = {
    say: "said", tool: "tool", result: "result", steer: "steer",
    final: "result", run: "run", sys: "session",
  };

  let host = null, mounted = false, timer = 0;
  let state = {
    items: [], page: null, facets: null, gate: null,
    seat: "", outcome: "", q: "", gateRuns: false, loading: false, error: "",
  };
  let log = null;   // the open drawer's state, or null

  /* ── styles ─────────────────────────────────────────────────────────────
     Every colour is a custom property. The orbit theme redefines all of them
     and this panel has to follow it without an edit. */
  function injectStyle() {
    if (document.getElementById("ovh-style")) return;
    const el = document.createElement("style");
    el.id = "ovh-style";
    el.textContent = [
      ".ovh{margin-bottom:16px}",
      /* .sec-h in app.css does the header now — band, icon, label, count. The
         one line left is the word after the count ("finished"), which is this
         panel's own and not part of the shared shape. */
      ".ovh-head .n{font-family:var(--mono);font-size:var(--fs-3xs);letter-spacing:var(--track-label);text-transform:uppercase;color:var(--text-3)}",
      ".ovh-spacer{flex:1 1 auto}",

      /* The honesty line. It sits above the rows because a reader must know
         what "done" is worth here BEFORE they read a column of them. */
      ".ovh-gate{display:flex;gap:8px;align-items:flex-start;margin:9px 0 11px;padding:8px 11px;border:1px solid var(--line);border-left:2px solid var(--warn);border-radius:var(--r-sm,8px);background:var(--surface-1);font-size:12px;color:var(--text-2);line-height:1.45}",
      ".ovh-gate.gated{border-left-color:var(--good)}",
      ".ovh-gate svg{flex:none;margin-top:1px;color:var(--text-3)}",
      ".ovh-gate b{color:var(--text)}",

      ".ovh-bar{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:10px}",
      ".ovh-in{background:var(--surface-1);border:1px solid var(--line);border-radius:var(--r-sm,8px);color:var(--text);font:inherit;font-size:12px;padding:6px 10px;min-width:180px}",
      ".ovh-in:focus{outline:none;border-color:var(--accent)}",
      ".ovh-in::placeholder{color:var(--text-3)}",
      ".ovh-chip{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;padding:5px 10px;border:1px solid var(--line);border-radius:999px;color:var(--text-3);cursor:pointer;background:none;display:inline-flex;gap:6px;align-items:center}",
      ".ovh-chip:hover{border-color:var(--accent);color:var(--text)}",
      ".ovh-chip.on{border-color:var(--accent);color:var(--text);background:var(--accent-soft,var(--surface-3))}",
      ".ovh-chip b{color:var(--text-2)}",
      ".ovh-chip.bad.on{border-color:var(--bad);color:var(--bad);background:var(--bad-soft)}",

      /* Capped and scrolled: the panel sits UNDER Running now and Recent
         activity and must never grow tall enough to push them off. */
      ".ovh-list{max-height:min(46vh,420px);overflow:auto;border:1px solid var(--line);border-radius:var(--r-sm,8px);background:var(--surface-1)}",
      ".ovh-row{display:grid;grid-template-columns:56px 74px minmax(0,1fr) 78px 190px 62px;gap:10px;align-items:center;padding:8px 11px;border-bottom:1px solid var(--line);cursor:pointer;border-left:2px solid transparent}",
      ".ovh-row:last-child{border-bottom:none}",
      ".ovh-row:hover{background:var(--surface-3)}",
      ".ovh-row.bad{border-left-color:var(--bad)}",
      ".ovh-row.held{border-left-color:var(--accent)}",
      ".ovh-row .id{font-family:var(--mono);font-size:10.5px;color:var(--text-3)}",
      ".ovh-row .seat{font-family:var(--mono);font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;overflow:hidden;text-overflow:ellipsis}",
      ".ovh-row .ttl{font-size:12.5px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".ovh-row .ttl .re{font-family:var(--mono);font-size:9px;color:var(--warn);border:1px solid var(--warn-line);border-radius:999px;padding:0 5px;margin-left:7px}",
      ".ovh-row .at{font-family:var(--mono);font-size:10px;color:var(--text-3);text-align:right}",
      ".ovh-out{font-family:var(--mono);font-size:9px;letter-spacing:.08em;text-transform:uppercase;padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--text-3);justify-self:start;white-space:nowrap}",
      ".ovh-out.bad{color:var(--bad);border-color:var(--bad-line);background:var(--bad-soft)}",
      ".ovh-out.ok{color:var(--text-2);border-color:var(--line)}",
      ".ovh-out.live{color:var(--accent);border-color:var(--accent-line,var(--line));background:var(--accent-soft)}",

      /* The verdict cell is TWO lines on purpose. A chip alone ("no gate")
         reads as a status; the sentence under it is the part that stops a
         reader assuming somebody looked. */
      ".ovh-v{min-width:0}",
      ".ovh-vb{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:9px;letter-spacing:.08em;text-transform:uppercase;padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--text-3)}",
      ".ovh-vb.good{color:var(--good);border-color:var(--good-line);background:var(--good-soft)}",
      ".ovh-vb.bad{color:var(--bad);border-color:var(--bad-line);background:var(--bad-soft)}",
      ".ovh-vb.warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}",
      ".ovh-vb.live{color:var(--accent);border-color:var(--accent-line,var(--line));background:var(--accent-soft)}",
      ".ovh-vb.none{color:var(--text-3);border-style:dashed}",
      ".ovh-vb.mute{color:var(--text-3);border-color:transparent}",
      ".ovh-vw{display:block;font-size:10.5px;color:var(--text-3);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",

      ".ovh-foot{display:flex;gap:9px;align-items:center;margin-top:9px;font-family:var(--mono);font-size:10px;color:var(--text-3)}",
      ".ovh-btn{font-family:var(--mono);font-size:10px;letter-spacing:.06em;padding:5px 11px;border:1px solid var(--line);border-radius:var(--r-sm,8px);background:var(--surface-1);color:var(--text-2);cursor:pointer}",
      ".ovh-btn:hover:not(:disabled){border-color:var(--accent);color:var(--text)}",
      ".ovh-btn:disabled{opacity:.4;cursor:default}",
      ".ovh-empty{padding:22px 14px;text-align:center;font-size:12px;color:var(--text-3)}",
      ".ovh-empty.err{color:var(--bad)}",

      /* ── the log drawer ── */
      ".ovh-back{position:fixed;inset:0;background:rgba(0,0,0,.66);z-index:800;display:flex;justify-content:flex-end}",
      /* --solid-*, not --surface-*. On the orbit ground the surface tokens are
         rgba(255,255,255,.045) — glass, meant to sit on the vanta page — and
         a drawer built from one let the whole dashboard read straight through
         the text. --solid-* is the opaque companion the theme provides for
         exactly this, and it aliases back to --surface-* everywhere else. */
      ".ovh-draw{width:min(960px,96vw);height:100%;background:var(--solid-2,var(--surface-2));border-left:1px solid var(--line-strong,var(--line));display:flex;flex-direction:column;box-shadow:-18px 0 46px rgba(0,0,0,.4)}",
      ".ovh-dh{padding:13px 16px;border-bottom:1px solid var(--line);flex:none}",
      ".ovh-dh h4{margin:0 0 3px;font-size:14px;color:var(--text);font-weight:var(--fw-semi,600);line-height:1.35}",
      ".ovh-dh .sub{display:flex;gap:9px;align-items:center;flex-wrap:wrap;font-family:var(--mono);font-size:10px;color:var(--text-3)}",
      ".ovh-x{position:absolute;top:11px;right:14px;background:none;border:1px solid var(--line);border-radius:var(--r-sm,8px);color:var(--text-3);cursor:pointer;font-size:15px;line-height:1;padding:4px 9px}",
      ".ovh-x:hover{border-color:var(--accent);color:var(--text)}",
      ".ovh-dv{margin:10px 0 0;padding:8px 11px;border:1px solid var(--line);border-left:2px solid var(--line);border-radius:var(--r-sm,8px);background:var(--solid-1,var(--surface-1));font-size:12px;color:var(--text-2);line-height:1.5}",
      ".ovh-dv.good{border-left-color:var(--good)} .ovh-dv.bad{border-left-color:var(--bad)}",
      // The one control that puts a stopped agent BACK to work. It sits above
      // the verdict because the verdict is where you learn it stopped.
      ".ovh-respawn{display:flex;align-items:center;gap:9px;margin:10px 0 0}",
      ".ovh-btn{background:var(--accent);color:var(--bg);border:1px solid var(--accent);border-radius:var(--r-sm,3px);cursor:pointer;font:inherit;font-size:11.5px;font-weight:600;padding:5px 12px}",
      ".ovh-btn:hover{filter:brightness(1.08)}",
      ".ovh-btn:disabled{opacity:.55;cursor:default}",
      ".ovh-respawn-note{font-size:10.5px;color:var(--text-3)}",
      ".ovh-dv.warn{border-left-color:var(--warn)} .ovh-dv.none{border-left-color:var(--text-3)}",
      ".ovh-dv.live{border-left-color:var(--accent)}",
      ".ovh-dbody{flex:1 1 auto;overflow:auto;padding:14px 16px}",
      ".ovh-sec{margin-bottom:15px}",
      ".ovh-sec>h5{margin:0 0 6px;font-family:var(--mono);font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--text-3);font-weight:var(--fw-semi,600)}",
      ".ovh-pre{white-space:pre-wrap;word-break:break-word;font-family:var(--mono);font-size:11px;line-height:1.62;color:var(--text-2);background:var(--solid-1,var(--surface-1));border:1px solid var(--line);border-radius:var(--r-sm,8px);padding:10px 12px;max-height:210px;overflow:auto;margin:0}",
      ".ovh-logbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:8px}",
      ".ovh-steps{border:1px solid var(--line);border-radius:var(--r-sm,8px);background:var(--solid-1,var(--surface-1));overflow:hidden}",
      ".ovh-step{display:grid;grid-template-columns:64px minmax(0,1fr);gap:10px;padding:7px 11px;border-bottom:1px solid var(--line)}",
      ".ovh-step:last-child{border-bottom:none}",
      ".ovh-step.hit{background:var(--accent-soft)}",
      ".ovh-step.run{background:var(--solid-3,var(--surface-3))}",
      ".ovh-k{font-family:var(--mono);font-size:9px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-3);padding-top:2px}",
      ".ovh-step.say .ovh-k{color:var(--text-2)}",
      ".ovh-step.tool .ovh-k{color:var(--accent)}",
      ".ovh-step.final .ovh-k{color:var(--good)}",
      ".ovh-step.steer .ovh-k{color:var(--warn)}",
      ".ovh-step .nm{font-family:var(--mono);font-size:10.5px;color:var(--text);margin-bottom:2px}",
      ".ovh-step .tx{white-space:pre-wrap;word-break:break-word;font-family:var(--mono);font-size:10.5px;line-height:1.6;color:var(--text-2);max-height:190px;overflow:auto;margin:0}",
      ".ovh-step .tx mark{background:var(--accent-soft);color:var(--accent);border-radius:2px}",
      ".ovh-more{margin-top:4px;font-family:var(--mono);font-size:9.5px;color:var(--text-3);background:none;border:1px dashed var(--line);border-radius:var(--r-sm,8px);padding:2px 8px;cursor:pointer}",
      ".ovh-more:hover{border-color:var(--accent);color:var(--accent)}",

      /* ── what it produced ──
         Thumbnails at the real aspect ratio on a checkerboard: a transparent
         sprite centre-cropped square is indistinguishable from a different
         transparent sprite, and these are the frames somebody opened the panel
         to look at. */
      ".ovh-prov{margin-left:auto;font-family:var(--mono);font-size:9px;letter-spacing:0;text-transform:none;color:var(--text-3)}",
      ".ovh-sec>h5{display:flex;align-items:center;gap:6px}",
      ".ovh-subh{display:flex;align-items:center;gap:6px;margin:11px 0 6px;font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--text-3)}",
      ".ovh-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:9px}",
      ".ovh-thumb{margin:0;border:1px solid var(--line);border-radius:var(--r-sm,8px);background:var(--solid-1,var(--surface-1));overflow:hidden;cursor:zoom-in;position:relative;transition:border-color .12s}",
      ".ovh-thumb:hover,.ovh-thumb:focus{border-color:var(--accent);outline:none}",
      ".ovh-thumb img{display:block;width:100%;height:118px;object-fit:contain;background-color:var(--solid-2,var(--surface-2));background-image:linear-gradient(45deg,var(--surface-3) 25%,transparent 25%,transparent 75%,var(--surface-3) 75%),linear-gradient(45deg,var(--surface-3) 25%,transparent 25%,transparent 75%,var(--surface-3) 75%);background-size:14px 14px;background-position:0 0,7px 7px}",
      ".ovh-thumb figcaption{padding:6px 8px;border-top:1px solid var(--line);font-family:var(--mono);font-size:9.5px;line-height:1.4;color:var(--text-3);display:block}",
      ".ovh-thumb figcaption b{display:block;color:var(--text-2);font-weight:var(--fw-semi,600);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".ovh-qa{position:absolute;top:6px;right:6px;font-family:var(--mono);font-size:8.5px;letter-spacing:.08em;text-transform:uppercase;padding:1px 6px;border-radius:999px;border:1px solid var(--line)}",
      ".ovh-qa.good{color:var(--good);border-color:var(--good-line);background:var(--good-soft)}",
      ".ovh-qa.bad{color:var(--bad);border-color:var(--bad-line);background:var(--bad-soft)}",
      ".ovh-btn.ovh-w{margin-top:9px}",
      ".ovh-filelist,.ovh-difflist{margin-top:7px;border:1px solid var(--line);border-radius:var(--r-sm,8px);background:var(--solid-1,var(--surface-1));overflow:hidden}",
      ".ovh-file{display:grid;grid-template-columns:52px minmax(0,1fr) 104px 66px;gap:9px;align-items:center;padding:6px 10px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:10.5px}",
      ".ovh-file:last-child{border-bottom:none}",
      ".ovh-fk{font-size:9px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-3)}",
      ".ovh-fk.added{color:var(--good)}.ovh-fk.deleted{color:var(--bad)}",
      ".ovh-fp{color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-decoration:none;direction:rtl;text-align:left}",
      "a.ovh-fp:hover{color:var(--accent);text-decoration:underline}",
      ".ovh-fo{font-size:9px;color:var(--text-3);text-align:right}",
      ".ovh-fo .add{color:var(--good)}.ovh-fo .del{color:var(--bad)}",
      ".ovh-fb{font-size:9px;color:var(--text-3);text-align:right}",
      ".ovh-wfoot{margin-top:8px;font-family:var(--mono);font-size:9.5px;color:var(--text-3);line-height:1.5}",

      /* Above the drawer, and closing it must not close the drawer. */
      ".ovh-lb{position:fixed;inset:0;z-index:900;background:rgba(0,0,0,.88);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:26px;cursor:zoom-out}",
      ".ovh-lb img{max-width:96vw;max-height:82vh;object-fit:contain;image-rendering:auto;border:1px solid var(--line)}",
      ".ovh-lbcap{font-family:var(--mono);font-size:10.5px;color:var(--text-3);word-break:break-all;text-align:center}",
      "@media (max-width:1080px){",
      "  .ovh-row{grid-template-columns:48px 64px minmax(0,1fr) 150px;row-gap:3px}",
      "  .ovh-row .ovh-out{grid-column:2/3}.ovh-row .at{display:none}",
      "}",
    ].join("\n");
    document.head.appendChild(el);
  }

  /* ── mount ──────────────────────────────────────────────────────────── */
  function mount() {
    if (mounted) return true;
    const view = document.getElementById("view-overview");
    if (!view) return false;
    injectStyle();
    host = document.createElement("div");
    // .spanel + .k-list: the shared section surface (app.css, "sections"),
    // and the kind that says the rows in here are ones you act on.
    host.className = "spanel k-list ovh";
    host.id = "ovh";
    // APPENDED, not inserted. Running now and Recent activity keep the exact
    // position they had; this grows the page downward instead of pushing the
    // two panels people already look for off the top.
    view.appendChild(host);
    mounted = true;
    render();
    load(true);
    document.addEventListener("keydown", ev => {
      if (ev.key !== "Escape") return;
      // The lightbox first. Escape from a full-size frame means "put the
      // picture away", not "throw away my place in a 700-step log".
      const box = document.getElementById("ovh-lb");
      if (box) { box.remove(); return; }
      if (log) closeLog();
    });
    return true;
  }

  const active = () => {
    const view = document.getElementById("view-overview");
    return Boolean(view && view.classList.contains("active"));
  };

  /* ── data ───────────────────────────────────────────────────────────── */
  function query(offset) {
    const p = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (state.seat) p.set("seat", state.seat);
    if (state.outcome) p.set("outcome", state.outcome);
    if (state.q.trim()) p.set("q", state.q.trim());
    if (state.gateRuns) p.set("gate_runs", "true");
    return `/api/history?${p}`;
  }

  async function load(reset) {
    if (state.loading) return;
    state.loading = true;
    const offset = reset ? 0 : state.items.length;
    render();
    let body;
    try {
      const res = await fetch(query(offset));
      body = await res.json();
      if (!res.ok || body.ok === false) throw new Error(
        (body && body.error && body.error.message) || `HTTP ${res.status}`);
    } catch (err) {
      state.loading = false;
      state.error = String((err && err.message) || err);
      render();
      return;
    }
    state.error = "";
    state.items = reset ? (body.items || []) : state.items.concat(body.items || []);
    state.page = body.page || null;
    state.facets = body.facets || state.facets;
    state.gate = body.gate || state.gate;
    state.loading = false;
    render();
  }

  /* ── the panel ──────────────────────────────────────────────────────── */
  function gateLine() {
    const g = state.gate;
    if (!g || !g.mode) return "";
    const gated = g.mode !== "none";
    // The label is the gate module's own sentence, printed verbatim. Under
    // 'none' that sentence IS the disclosure, and paraphrasing it would be the
    // one place this panel could quietly overstate what it knows.
    return `<div class="ovh-gate${gated ? " gated" : ""}">${icon("gate", 15)}
      <div>Approval gate right now: <b>${E(g.label || g.mode)}</b>.
      ${gated ? "New work below gets an independent check."
              : "Nothing new below gets an independent check - a row only claims one where a QA round really ran."}
      ${g.env_override ? ` <b>${E(g.env_override)}</b>.` : ""}</div></div>`;
  }

  function chips() {
    const f = state.facets || { seats: [], outcomes: {} };
    const out = f.outcomes || {};
    const total = Object.values(out).reduce((a, b) => a + b, 0);
    const one = (id, label, n, cls) =>
      `<button class="ovh-chip${state.outcome === id ? " on" : ""}${cls ? " " + cls : ""}"
        data-outcome="${E(id)}" type="button">${E(label)}${
          n != null ? ` <b>${n}</b>` : ""}</button>`;
    const seats = (f.seats || []).map(s =>
      `<option value="${E(s.seat)}"${state.seat === s.seat ? " selected" : ""}
        >${E(s.seat)} (${s.n})</option>`).join("");
    return `<div class="ovh-bar">
      <input class="ovh-in" id="ovh-q" type="search" placeholder="search titles and result notes…"
        value="${E(state.q)}" autocomplete="off">
      <select class="ovh-in" id="ovh-seat" style="min-width:130px">
        <option value="">every seat</option>${seats}</select>
      ${one("", "all", total)}
      ${one("done", "done", out.done)}
      ${one("failed", "failed", out.failed, "bad")}
      ${one("cancelled", "cancelled", out.cancelled)}
      ${out.review ? one("review", "held", out.review) : ""}
      <span class="ovh-spacer"></span>
      <button class="ovh-chip${state.gateRuns ? " on" : ""}" id="ovh-gr" type="button"
        title="The QA gate's own runs. Off by default: each one is already shown as the verdict of the item it reviewed.">gate runs</button>
    </div>`;
  }

  function row(it) {
    const out = OUTCOME[it.status] || { label: it.status, tone: "mute" };
    const v = it.verdict || {};
    const tone = VERDICT_TONE[v.kind] || "mute";
    const rowCls = it.status === "failed" ? " bad"
      : it.status === "review" ? " held" : "";
    return `<div class="ovh-row${rowCls}" data-open="${it.id}" role="button" tabindex="0"
        title="${E(it.title || "")}">
      <span class="id">#${it.id}</span>
      <span class="seat" style="color:${seatColor(it.seat)}">${E(it.seat || "")}</span>
      <span class="ttl">${E(it.title || "(untitled)")}${
        it.attempts ? `<span class="re">reopened ${it.attempts}×</span>` : ""}</span>
      <span class="ovh-out ${out.tone}">${E(out.label)}</span>
      <span class="ovh-v">
        <span class="ovh-vb ${tone}" title="${E(v.why || "")}">${E(v.label || "-")}</span>
        <span class="ovh-vw">${E(v.short || v.why || "")}</span>
      </span>
      <span class="at" title="${E(stampTitle(it.updated_at))}">${E(when(it.updated_at))}</span>
    </div>`;
  }

  function render() {
    if (!host) return;
    const page = state.page || { total: 0 };
    const shown = state.items.length;
    let body;
    if (state.error) {
      body = `<div class="ovh-empty err">could not read the work history - ${E(state.error)}</div>`;
    } else if (!shown && state.loading) {
      body = `<div class="ovh-empty">reading the board…</div>`;
    } else if (!shown) {
      body = `<div class="ovh-empty">nothing finished matches this filter${
        state.q ? ` - no title or result note contains “${E(state.q)}”` : ""}.</div>`;
    } else {
      body = state.items.map(row).join("");
    }
    host.innerHTML = `
      <div class="sec-h ovh-head">${icon("timeline", 15)}
        <h3 class="sec-t">Work history</h3>
        <span class="sec-n">${page.total || 0}</span>
        <span class="n">finished</span></div>
      ${gateLine()}
      ${chips()}
      <div class="ovh-list">${body}</div>
      <div class="ovh-foot">
        <span>showing ${shown} of ${page.total || 0}</span>
        <span class="ovh-spacer"></span>
        <button class="ovh-btn" id="ovh-more" type="button"
          ${page.next_offset == null || state.loading ? "disabled" : ""}
          >${state.loading ? "loading…" : "load 40 more"}</button>
      </div>`;
    wire();
  }

  let qTimer = 0;
  function wire() {
    const q = host.querySelector("#ovh-q");
    if (q) q.oninput = () => {
      state.q = q.value;
      clearTimeout(qTimer);
      qTimer = setTimeout(() => load(true), 280);
    };
    const seat = host.querySelector("#ovh-seat");
    if (seat) seat.onchange = () => { state.seat = seat.value; load(true); };
    host.querySelectorAll("[data-outcome]").forEach(btn => {
      btn.onclick = () => { state.outcome = btn.dataset.outcome; load(true); };
    });
    const gr = host.querySelector("#ovh-gr");
    if (gr) gr.onclick = () => { state.gateRuns = !state.gateRuns; load(true); };
    const more = host.querySelector("#ovh-more");
    if (more) more.onclick = () => load(false);
    host.querySelectorAll("[data-open]").forEach(el => {
      const open = () => openLog(parseInt(el.dataset.open, 10));
      el.onclick = open;
      el.onkeydown = ev => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
      };
    });
  }

  /* ── the log drawer ─────────────────────────────────────────────────── */
  function openLog(id) {
    log = { id, offset: -1, run: 0, q: "", data: null, mi: 0, loading: true,
            error: "", expanded: {}, work: null, workErr: "",
            diff: null, diffOpen: false, files: false };
    paintDrawer();
    fetchLog();
    fetchWork();
  }

  /* WHAT THE RUN MADE. A separate request from the log on purpose: it stats
     every produced file and walks the capture directories, which is fine once
     per opened row and would be hundreds of syscalls on a page of forty. */
  async function fetchWork() {
    const l = log;
    if (!l) return;
    try {
      const res = await fetch(`/api/history/${l.id}/work`);
      const body = await res.json();
      if (!res.ok || body.ok === false) throw new Error(
        (body && body.detail) || `HTTP ${res.status}`);
      if (log !== l) return;
      l.work = body;
    } catch (err) {
      if (log !== l) return;
      l.workErr = String((err && err.message) || err);
    }
    paintDrawer();
  }

  async function fetchDiff() {
    const l = log;
    if (!l || l.diff) { if (l) { l.diffOpen = true; paintDrawer(); } return; }
    l.diffOpen = true;
    l.diff = { loading: true };
    paintDrawer();
    try {
      const res = await fetch(`/api/queue/${l.id}/diff`);
      const body = await res.json();
      if (log !== l) return;
      l.diff = (body && body.data) || body || {};
    } catch (err) {
      if (log !== l) return;
      l.diff = { error: String((err && err.message) || err) };
    }
    paintDrawer();
  }

  function closeLog() {
    log = null;
    const back = document.getElementById("ovh-back");
    if (back) back.remove();
  }

  async function fetchLog() {
    const l = log;
    if (!l) return;
    l.loading = true;
    paintDrawer();
    const p = new URLSearchParams({ limit: String(STEPS) });
    if (l.offset >= 0) p.set("offset", String(l.offset));
    if (l.run) p.set("run", String(l.run));
    if (l.q.trim()) p.set("q", l.q.trim());
    try {
      const res = await fetch(`/api/history/${l.id}/log?${p}`);
      const body = await res.json();
      if (!res.ok || body.ok === false) throw new Error(
        (body && body.detail) || (body.error && body.error.message) || `HTTP ${res.status}`);
      if (log !== l) return;                 // drawer closed or switched under us
      l.data = body;
      l.offset = body.offset;
      l.error = "";
    } catch (err) {
      if (log !== l) return;
      l.error = String((err && err.message) || err);
    }
    l.loading = false;
    l.expanded = {};
    paintDrawer();
  }

  /* ── what the run produced ──────────────────────────────────────────────
     The half of the drawer that was missing. Everything below renders from
     /api/history/{id}/work; nothing here guesses at a path. */
  const previewURL = (rel, itemId) =>
    `/api/preview?rel=${encodeURIComponent(rel)}`
    + (itemId ? `&item_id=${encodeURIComponent(itemId)}` : "");

  const ORIGIN = {
    artifact:   { label: "registered", why: "registered in the artifact library - it has a review state" },
    harness:    { label: "observed",   why: "the harness watched this run write it; not self-reported" },
    transcript: { label: "from the log", why: "taken from the run's own transcript, then checked against the project" },
    capture:    { label: "capture",    why: "a frame this run rendered" },
  };

  function thumb(entry, itemId) {
    const meta = ORIGIN[entry.origin] || {};
    const qa = entry.qa && entry.qa.verdict
      ? `<span class="ovh-qa ${entry.qa.verdict === "pass" ? "good" : "bad"}"
           >${E(entry.qa.verdict)}${entry.qa.score != null ? " " + entry.qa.score : ""}</span>`
      : "";
    const sub = entry.origin === "capture"
      ? (entry.by_name ? "named for this item" : "written during this run")
      : (entry.logical_name
          ? `${entry.logical_name} r${entry.revision}${entry.status ? " · " + entry.status : ""}`
          : meta.label || "");
    return `<figure class="ovh-thumb" data-shot="${E(entry.rel)}"
        title="${E(entry.rel)} - ${E(meta.why || "")}" tabindex="0" role="button">
      <img src="${E(previewURL(entry.rel, itemId))}" alt="${E(entry.name)}" loading="lazy">
      ${qa}
      <figcaption><b>${E(entry.name)}</b><span>${E(sub)}</span></figcaption>
    </figure>`;
  }

  function fileRow(entry, itemId) {
    const meta = ORIGIN[entry.origin] || {};
    return `<div class="ovh-file">
      <span class="ovh-fk">${E(entry.ext || "file")}</span>
      <a class="ovh-fp" href="${E(previewURL(entry.rel, itemId))}" target="_blank"
         rel="noopener" title="${E(entry.rel)}"
         ${entry.image ? "" : 'onclick="return false" style="cursor:default"'}
        >${E(entry.rel)}</a>
      <span class="ovh-fo" title="${E(meta.why || "")}">${E(meta.label || entry.origin)}</span>
      <span class="ovh-fb">${bytes(entry.bytes)}</span>
    </div>`;
  }

  /* SCOPED TO THIS RUN'S OWN PATHS, and that is not a nicety.
     /api/queue/{id}/diff answers "what differs from the base commit", and on
     this project forty-odd items share the base commit 9c508b9 — so item #334's
     "changed code" opened on the entire uncommitted tree, dice sprites and all.
     Attributing another run's work to this one is the same lie as a fake
     verdict, so the list is intersected with the files this run actually
     produced and the remainder is COUNTED and explained rather than shown. */
  function diffBlock() {
    const d = log.diff;
    if (!log.diffOpen) return "";
    if (!d || d.loading) {
      return `<div class="ovh-empty">reading the diff — git is comparing the
        tree against the base commit…</div>`;
    }
    if (d.error) return `<div class="ovh-empty err">${E(d.error)}</div>`;
    if (d.available === false) {
      return `<div class="ovh-empty">${E(d.reason || "no diff available")}</div>`;
    }
    const mine = new Set(((log.work && log.work.produced) || []).map(x => x.rel));
    const all = d.files || [];
    const files = all.filter(f => mine.has(String(f.path || "").replace(/\\/g, "/")));
    const others = all.length - files.length;
    const base = E((d.base || "").slice(0, 12));
    const note = others
      ? `<div class="ovh-wfoot">${others} more file${others > 1 ? "s" : ""} also
         differ from ${base} and are not shown: the base commit is shared with
         other runs, so a diff against it is the whole tree's work, not this
         item's.</div>`
      : "";
    if (!files.length) {
      return `<div class="ovh-empty">none of the ${
        all.length} file${all.length === 1 ? "" : "s"} differing from ${base}
        is one this run produced.</div>${note}`;
    }
    return `<div class="ovh-difflist">${files.map(f => `
      <div class="ovh-file">
        <span class="ovh-fk ${E(f.status || "")}">${E(f.status || "")}</span>
        <span class="ovh-fp" title="${E(f.path)}">${E(f.path)}</span>
        <span class="ovh-fo">${f.binary ? "binary"
          : `<b class="add">+${Number(f.added || 0)}</b> <b class="del">&minus;${Number(f.removed || 0)}</b>`}</span>
        <span class="ovh-fb"></span>
      </div>`).join("")}</div>${note}`;
  }

  function workSection() {
    const l = log;
    if (l.workErr) {
      return `<div class="ovh-sec"><h5>${icon("assets", 13)} What it produced</h5>
        <div class="ovh-empty err">could not resolve this run's output — ${E(l.workErr)}</div></div>`;
    }
    const w = l.work;
    if (!w) {
      return `<div class="ovh-sec"><h5>${icon("assets", 13)} What it produced</h5>
        <div class="ovh-empty">looking for what this run made…</div></div>`;
    }
    const pid = w.preview_item_id || 0;
    const produced = w.produced || [];
    const captures = w.captures || [];
    const images = produced.filter(x => x.image);
    const others = produced.filter(x => !x.image);
    const c = w.counts || {};

    // Provenance, stated. Where a row came from decides how much it is worth,
    // and a file list that will not say is the same evasion as a blank verdict.
    const prov = [
      c.artifacts ? `${c.artifacts} registered` : "",
      c.observed ? `${c.observed} observed by the harness` : "",
      c.transcript ? `${c.transcript} recovered from the transcript` : "",
      captures.length ? `${captures.length} capture${captures.length > 1 ? "s" : ""}` : "",
    ].filter(Boolean).join(" · ");

    let body = "";
    if (images.length) {
      body += `<div class="ovh-grid">${images.map(x => thumb(x, pid)).join("")}</div>`;
    }
    if (captures.length) {
      body += `<div class="ovh-subh">${icon("real_preview", 12)} Captures — frames this run rendered</div>
        <div class="ovh-grid">${captures.map(x => thumb(x, pid)).join("")}</div>`;
    }
    if (others.length) {
      const open = l.files || !images.length && !captures.length;
      body += `<button class="ovh-btn ovh-w" id="ovh-files" type="button">${
        open ? "hide" : "show"} ${others.length} non-image file${others.length > 1 ? "s" : ""}</button>`;
      if (open) body += `<div class="ovh-filelist">${
        others.map(x => fileRow(x, pid)).join("")}</div>`;
    }
    if (w.diff && w.diff.available) {
      body += `<button class="ovh-btn ovh-w" id="ovh-diff" type="button">${
        l.diffOpen ? "hide" : "show"} the line changes in what it produced</button>`
        + diffBlock();
    }
    if (!body) {
      // An empty section has to say WHY it is empty, or it reads as a bug in
      // the panel rather than as a run that made nothing.
      body = `<div class="ovh-empty">no files recorded for this run — nothing
        registered an artifact, the harness saw no writes, and its transcript
        names no file still on disk.${
        w.missing ? ` ${w.missing} recorded path${w.missing > 1 ? "s are" : " is"} no longer there.` : ""}</div>`;
    }
    const foot = [
      w.missing ? `${w.missing} recorded path${w.missing > 1 ? "s" : ""} no longer on disk` : "",
      w.harness && w.harness.count
        ? `${w.harness.count} under .bgate/ (harness bookkeeping, not your files)` : "",
      w.read_only && w.read_only.count
        ? `read ${w.read_only.count} file${w.read_only.count > 1 ? "s" : ""} it did not write` : "",
    ].filter(Boolean).join(" · ");

    return `<div class="ovh-sec"><h5>${icon("assets", 13)} What it produced
        ${prov ? `<span class="ovh-prov">${E(prov)}</span>` : ""}</h5>
      ${body}
      ${foot ? `<div class="ovh-wfoot">${E(foot)}</div>` : ""}</div>`;
  }

  /* Full size, on top of the drawer. Its own layer so closing it does not
     close the drawer underneath — losing your place in a 700-step log because
     you looked at a picture is its own small betrayal. */
  function lightbox(rel, itemId) {
    let box = document.getElementById("ovh-lb");
    if (!box) {
      box = document.createElement("div");
      box.id = "ovh-lb";
      box.className = "ovh-lb";
      box.onclick = () => box.remove();
      document.body.appendChild(box);
    }
    box.innerHTML = `<button class="ovh-x" type="button" aria-label="Close">${
      icon("close", 15)}</button>
      <img src="${E(previewURL(rel, itemId))}" alt="${E(rel)}">
      <div class="ovh-lbcap">${E(rel)}</div>`;
  }

  function highlight(text, needle) {
    const safe = E(text);
    if (!needle) return safe;
    const pattern = E(needle).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (!pattern) return safe;
    try {
      return safe.replace(new RegExp(pattern, "gi"), m => `<mark>${m}</mark>`);
    } catch (e) { return safe; }
  }

  function stepHTML(step, needle, hit) {
    const kind = STEP_LABEL[step.kind] || step.kind;
    const expanded = log.expanded[step.off];
    const text = expanded != null ? expanded : (step.text || "");
    const clipped = expanded == null && step.full > (step.text || "").length;
    const head = step.kind === "final"
      ? `<div class="nm">${E(step.subtype || "result")}${
          step.cost != null ? ` · $${Number(step.cost).toFixed(4)}` : ""}${
          step.turns != null ? ` · ${step.turns} turns` : ""}</div>`
      : step.name ? `<div class="nm">${E(step.name)}</div>` : "";
    return `<div class="ovh-step ${E(step.kind)}${hit ? " hit" : ""}">
      <span class="ovh-k">${E(kind)}</span>
      <div>${head}
        ${text ? `<pre class="tx">${highlight(text, needle)}</pre>` : ""}
        ${clipped ? `<button class="ovh-more" data-off="${step.off}" type="button"
          >+${step.full - (step.text || "").length} more characters</button>` : ""}
      </div></div>`;
  }

  function logSection() {
    const l = log, d = l.data;
    if (l.error) return `<div class="ovh-empty err">could not read the log - ${E(l.error)}</div>`;
    if (!d) return `<div class="ovh-empty">reading the transcript…</div>`;
    if (d.note) return `<div class="ovh-empty">${E(d.note)}</div>`;
    if (!d.total) return `<div class="ovh-empty">the log holds no readable steps.</div>`;

    const from = d.offset + 1, to = Math.min(d.offset + d.steps.length, d.total);
    const hits = new Set(d.matches || []);
    const runs = d.runs > 1 ? `<select class="ovh-in" id="ovh-run" style="min-width:96px">
        <option value="0"${!l.run ? " selected" : ""}>all ${d.runs} runs</option>
        ${Array.from({ length: d.runs }, (_, i) =>
          `<option value="${i + 1}"${l.run === i + 1 ? " selected" : ""}>run ${i + 1}</option>`).join("")}
      </select>` : "";
    const matches = (d.matches || []).length;
    const nav = l.q.trim()
      ? `<span style="font-family:var(--mono);font-size:10px;color:${matches ? "var(--accent)" : "var(--text-3)"}">
           ${matches ? `${Math.min(l.mi + 1, matches)}/${matches} matches` : "no match"}</span>
         <button class="ovh-btn" id="ovh-mprev" type="button" ${matches < 2 ? "disabled" : ""}>&lsaquo;</button>
         <button class="ovh-btn" id="ovh-mnext" type="button" ${matches < 2 ? "disabled" : ""}>&rsaquo;</button>`
      : "";
    return `<div class="ovh-logbar">
        ${runs}
        <input class="ovh-in" id="ovh-lq" type="search" value="${E(l.q)}"
          placeholder="search this transcript…" autocomplete="off" style="min-width:200px">
        ${nav}
        <span class="ovh-spacer"></span>
        <button class="ovh-btn" id="ovh-first" type="button" ${d.offset <= 0 ? "disabled" : ""}>start</button>
        <button class="ovh-btn" id="ovh-prev" type="button" ${d.offset <= 0 ? "disabled" : ""}>&lsaquo; older</button>
        <span style="font-family:var(--mono);font-size:10px;color:var(--text-3)">${from}–${to} of ${d.total}</span>
        <button class="ovh-btn" id="ovh-next" type="button" ${to >= d.total ? "disabled" : ""}>newer &rsaquo;</button>
        <button class="ovh-btn" id="ovh-last" type="button" ${to >= d.total ? "disabled" : ""}>end</button>
      </div>
      <div class="ovh-steps">${d.steps.map((s, n) =>
        stepHTML(s, l.q.trim(), hits.has(d.offset + n))).join("")}</div>
      <div class="ovh-foot"><span>${bytes(d.bytes)} on disk · ${d.total} steps${
        d.runs > 1 ? ` · ${d.runs} runs (the log appends across re-dispatches)` : ""}
        ${l.q.trim() ? ` · search covers the first ${d.text_cap} characters of each step` : ""}</span></div>`;
  }

  function paintDrawer() {
    if (!log) return;
    let back = document.getElementById("ovh-back");
    if (!back) {
      back = document.createElement("div");
      back.id = "ovh-back";
      back.className = "ovh-back";
      back.onclick = ev => { if (ev.target === back) closeLog(); };
      document.body.appendChild(back);
    }
    const it = (log.data && log.data.item) || {};
    const listed = state.items.find(x => x.id === log.id) || {};
    // The server's verdict wins: the list row it was opened from can be stale
    // or gone (a reopened item leaves history entirely), and an empty verdict
    // block is the one thing this panel is not allowed to render.
    const v = (log.data && log.data.verdict) || listed.verdict || {};
    const tone = VERDICT_TONE[v.kind] || "mute";
    const status = it.status || listed.status || "";
    const out = OUTCOME[status] || { label: status, tone: "mute" };
    back.innerHTML = `<div class="ovh-draw" onclick="event.stopPropagation()">
      <div class="ovh-dh" style="position:relative">
        <button class="ovh-x" id="ovh-close" type="button" aria-label="Close">&times;</button>
        <h4>#${log.id} &middot; ${E(it.title || listed.title || "")}</h4>
        <div class="sub">
          <span style="color:${seatColor(it.seat || listed.seat)}">${E(it.seat || listed.seat || "")}</span>
          <span class="ovh-out ${out.tone}">${E(out.label)}</span>
          ${it.attempts ? `<span>reopened ${it.attempts}×</span>` : ""}
          ${it.total_cost_usd ? `<span>$${Number(it.total_cost_usd).toFixed(4)}</span>` : ""}
          ${it.num_turns ? `<span>${it.num_turns} turns</span>` : ""}
          ${it.closed_by ? `<span>closed by ${E(it.closed_by)}</span>` : ""}
          ${it.stopped_by ? `<span>stopped by ${E(it.stopped_by)}</span>` : ""}
          <span title="${E(stampTitle(it.updated_at || listed.updated_at))}">${
            E(when(it.updated_at || listed.updated_at))}</span>
        </div>
        ${["failed", "cancelled", "done"].includes(status) ? `
        <div class="ovh-respawn">
          <button class="ovh-btn" id="ovh-respawn" type="button"
            title="Reopen this item and spawn a fresh agent on it. What it already produced stays registered - the new run is told to resume, not to redo.">
            respawn agent</button>
          <span class="ovh-respawn-note">picks up where this left off; already-registered work is kept</span>
        </div>` : ""}
        <div class="ovh-dv ${tone}">
          <b>Verdict: ${E(v.label || "-")}</b> — ${E(v.why || "")}${
            v.gate_item ? ` <span style="font-family:var(--mono);font-size:10px;color:var(--text-3)">(QA gate run #${v.gate_item}${
              v.rounds > 1 ? `, ${v.rounds} rounds` : ""})</span>` : ""}
          ${v.detail ? `<div style="margin-top:6px;font-family:var(--mono);font-size:10.5px;color:var(--text-3);white-space:pre-wrap;word-break:break-word">${E(v.detail)}</div>` : ""}
        </div>
      </div>
      <div class="ovh-dbody">
        ${workSection()}
        ${it.result ? `<div class="ovh-sec"><h5>What it reported</h5>
          <pre class="ovh-pre">${E(it.result)}</pre></div>` : ""}
        ${it.brief ? `<div class="ovh-sec"><h5>The brief it was given</h5>
          <pre class="ovh-pre">${E(it.brief)}</pre></div>` : ""}
        <div class="ovh-sec"><h5>Agent log${log.loading ? " · loading…" : ""}</h5>
          ${logSection()}</div>
      </div></div>`;
    wireDrawer();
  }

  /* PUT A STOPPED AGENT BACK TO WORK, from the panel where you find out it
   * stopped. There was no way to do this in the UI at all: an item that ended —
   * killed by a restart, cancelled by a misclick, or failed on its last step —
   * could only be revived by hand through the API, so the only thing a person
   * could actually press was the button that ended it.
   *
   * It RESUMES rather than restarts. Everything the previous attempt registered
   * is still on the board and still paid for, so the reopen reason tells the
   * next agent to look at what exists and make only what is missing. Re-running
   * a 24-sheet art item from zero is not a retry, it is buying it twice. */
  async function respawn(id) {
    const btn = document.getElementById("ovh-respawn");
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "respawning…";
    // window.BGWS is the seat shell's helper and Overview can paint before it
    // loads, so this file cannot assume it exists.
    const post = window.BGWS ? window.BGWS.post : async (path, body) => {
      const r = await fetch(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      return r.json().catch(() => ({ ok: r.ok }));
    };
    try {
      await post(`/api/queue/${id}/reopen`, {
        reason: "RESPAWNED BY A HUMAN from the history panel. The previous "
              + "attempt ended without finishing; whatever it already produced "
              + "is registered and paid for. RESUME: check what exists before "
              + "you make anything, and produce only what is missing. Do not "
              + "redo work that is already on the board.",
      });
      const out = await post(`/api/queue/${id}/dispatch`, {});
      const data = (out && out.data) || out || {};
      if (data.ok === false) throw new Error(data.error || "dispatch refused");
      btn.textContent = "respawned";
      if (window.toast) toast(`#${id} respawned`);
      closeLog();
    } catch (e) {
      // The server writes a readable refusal (autopilot off, a live agent
      // already holds it, the item is not in a reopenable state). Showing
      // "failed" instead of that sentence is what makes a button feel dead.
      btn.disabled = false;
      btn.textContent = "respawn agent";
      const why = String((e && e.message) || e);
      if (window.toast) toast(why); else window.alert(why);
    }
  }

  let lqTimer = 0;
  function wireDrawer() {
    const back = document.getElementById("ovh-back");
    if (!back || !log) return;
    const l = log, d = l.data;
    const on = (id, fn) => { const el = back.querySelector(id); if (el) el.onclick = fn; };
    on("#ovh-close", closeLog);
    on("#ovh-respawn", () => respawn(l.id));
    on("#ovh-files", () => { l.files = !l.files; paintDrawer(); });
    on("#ovh-diff", () => {
      if (l.diffOpen) { l.diffOpen = false; paintDrawer(); } else fetchDiff();
    });
    back.querySelectorAll("[data-shot]").forEach(fig => {
      const open = () => lightbox(fig.dataset.shot,
                                  (l.work && l.work.preview_item_id) || 0);
      fig.onclick = open;
      fig.onkeydown = ev => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
      };
    });
    const go = off => { l.offset = Math.max(0, off); fetchLog(); };
    if (d) {
      on("#ovh-first", () => go(0));
      on("#ovh-prev", () => go(d.offset - STEPS));
      on("#ovh-next", () => go(d.offset + STEPS));
      on("#ovh-last", () => go(Math.max(0, d.total - STEPS)));
      const matches = d.matches || [];
      const jump = delta => {
        if (!matches.length) return;
        l.mi = (l.mi + delta + matches.length) % matches.length;
        // Land the hit a few steps in rather than flush at the top edge, so
        // there is context above it.
        go(Math.max(0, matches[l.mi] - 3));
      };
      on("#ovh-mprev", () => jump(-1));
      on("#ovh-mnext", () => jump(1));
      const run = back.querySelector("#ovh-run");
      if (run) run.onchange = () => {
        l.run = parseInt(run.value, 10) || 0; l.offset = -1; l.mi = 0; fetchLog();
      };
      back.querySelectorAll(".ovh-more").forEach(btn => {
        btn.onclick = async () => {
          const off = btn.dataset.off;
          btn.disabled = true; btn.textContent = "reading…";
          try {
            const res = await fetch(`/api/history/${l.id}/log/step?off=${encodeURIComponent(off)}`);
            const body = await res.json();
            if (log !== l) return;
            l.expanded[off] = String((body && body.text) || "");
            paintDrawer();
          } catch (err) {
            btn.disabled = false;
            btn.textContent = "could not read that line";
          }
        };
      });
    }
    const lq = back.querySelector("#ovh-lq");
    if (lq) {
      lq.oninput = () => {
        l.q = lq.value; l.mi = 0; l.offset = -1;
        clearTimeout(lqTimer);
        lqTimer = setTimeout(fetchLog, 300);
      };
      // The repaint blows the field away on every fetch; put the caret back or
      // the box loses focus mid-word and typing goes nowhere.
      if (document.activeElement !== lq && l.q) {
        lq.focus(); lq.setSelectionRange(l.q.length, l.q.length);
      }
    }
  }

  /* ── lifecycle ──────────────────────────────────────────────────────── */
  function tick() {
    if (!mounted && !mount()) return;
    if (!active() || document.visibilityState === "hidden") return;
    // Only the first page auto-refreshes, and never while somebody is reading a
    // log, has paged deeper, or is mid-word in the search box — a repaint blows
    // the input's caret away, and a list that reshuffles under a click is worse
    // than one twenty seconds stale.
    if (log || state.loading || state.items.length > PAGE) return;
    const focused = document.activeElement;
    if (focused && host.contains(focused)
        && /^(INPUT|SELECT|TEXTAREA)$/.test(focused.tagName)) return;
    load(true);
  }

  function boot() {
    if (!mount()) { setTimeout(boot, 400); return; }
    clearInterval(timer);
    timer = setInterval(tick, POLL_MS);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  return { mount, refresh: () => load(true), open: openLog, close: closeLog,
           get state() { return state; } };
})();
