/* providerkeys.js — set the art-generation API keys, from the two places you
 * are standing when you need to.
 *
 * ONE CAPABILITY, TWO DOORS, DELIBERATELY DIFFERENT SHAPES.
 *   Settings → the management surface. Every provider, every state, set and
 *              clear, and the sentence about where the key is stored.
 *   Studio   → a "Generators" tab organised by WHAT YOU WANTED TO MAKE. You are
 *              here because a generator will not run; the card that cannot run
 *              says why and carries the fix, instead of sending you to another
 *              view to find a form. Same renderer underneath, so the two cannot
 *              drift; different framing, so neither is the other pasted twice.
 *
 * THE PANEL NEVER RECEIVES A KEY, so it can never render one. GET /api/providers
 * returns presence, a last-4 fingerprint and the adapter's own reason it cannot
 * run — there is no field on the wire that widens to the value. The input is
 * write-only: it is cleared the instant a save lands, its value is never copied
 * into a dataset attribute, an href, or a toast. This project committed an API
 * key once already; the rule that follows from that is that the value has
 * exactly one journey, keystrokes → POST body, and no branch off it.
 *
 * THE ROW REPAINTS FROM THE RESPONSE, never from what was sent. A key can save
 * correctly and the provider still be unusable — the `openai` package missing, a
 * shell variable shadowing the .env — and a card that painted "ready" out of its
 * own optimism would send somebody off to debug a generator that was never the
 * problem. Same rule settingsview.js follows, for the same reason.
 *
 * Self-contained: injects its own <style>, mounts itself, registers its own
 * Studio flow. Colours are theme variables only.
 */
(function () {
  "use strict";

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (name, size) => (window.BGIcon ? BGIcon(name, { size: size || 14 }) : "");
  const say = (m, k) => { try { toast(m, k); } catch (e) { /* headless */ } };

  /* A card is not live data. It is read when a view opens and after a write —
     the same floor settingsview.js puts under a 3s driver tick. */
  const STALE_MS = 20000;

  /* Why a provider is or is not usable, as one word plus a colour. `set` rather
     than `ready` for a key that saved into an adapter that still cannot run:
     "the key is fine, the leg is not" is the distinction that stops somebody
     re-pasting a working key three times. */
  const STATE = {
    ready:   { word: "ready",    tone: "good" },
    blocked: { word: "key set",  tone: "warn" },
    unset:   { word: "no key",   tone: "off"  },
  };

  function stateOf(row) {
    if (row.available) return "ready";
    return row.configured || row.in_env_file ? "blocked" : "unset";
  }

  /* ── styles ─────────────────────────────────────────────────────────────
     Injected here rather than added to app.css: five other modules are being
     edited in the same tree and a shared stylesheet is the file they collide
     in. Every colour is a theme variable — the orbit ground restyles the whole
     UI through these, and one hardcoded hex is a card that stops matching. */
  function injectStyle() {
    if (document.getElementById("pv-style")) return;
    const s = document.createElement("style");
    s.id = "pv-style";
    s.textContent = [
      ".pv-wrap{margin-top:22px}",
      ".pv-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}",
      ".pv-head h3{margin:0;font-size:15px;font-weight:var(--fw-semi);color:var(--text)}",
      ".pv-eyebrow{font-family:var(--mono);font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--text-3)}",
      ".pv-note{font-size:12px;color:var(--text-3);line-height:1.55;margin:0 0 14px;max-width:74ch}",
      ".pv-note code{font-family:var(--mono);font-size:11px;color:var(--text-2)}",
      ".pv-warn{display:flex;gap:9px;align-items:flex-start;border:1px solid var(--bad-line);background:var(--bad-soft);border-radius:9px;padding:10px 12px;font-size:12px;color:var(--text-2);line-height:1.5;margin-bottom:14px}",
      ".pv-warn b{color:var(--bad)}",
      ".pv-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px;align-items:start}",
      ".pv-card{border:1px solid var(--line);background:var(--surface-2);border-radius:11px;padding:13px 14px}",
      ".pv-card.s-ready{border-left:2px solid var(--good)}",
      ".pv-card.s-blocked{border-left:2px solid var(--warn)}",
      ".pv-card.s-unset{border-left:2px solid var(--line)}",
      ".pv-top{display:flex;align-items:center;gap:9px;margin-bottom:8px}",
      ".pv-top .pv-i{color:var(--text-3);display:inline-flex}",
      ".pv-name{font-size:13.5px;font-weight:var(--fw-semi);color:var(--text)}",
      ".pv-lamp{margin-left:auto;font-family:var(--mono);font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--text-3)}",
      ".pv-lamp.good{color:var(--good);border-color:var(--good-line);background:var(--good-soft)}",
      ".pv-lamp.warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}",
      ".pv-pills{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:9px}",
      ".pv-pill{font-family:var(--mono);font-size:9px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-3);border:1px solid var(--line);border-radius:999px;padding:2px 8px}",
      ".pv-help{font-size:11.5px;color:var(--text-3);line-height:1.5;margin-bottom:10px}",
      ".pv-why{font-size:11.5px;color:var(--warn);line-height:1.5;margin-bottom:10px}",
      ".pv-row{display:flex;gap:7px;align-items:center;flex-wrap:wrap}",
      ".pv-in{flex:1 1 150px;min-width:0;background:var(--surface-1);border:1px solid var(--line);border-radius:8px;color:var(--text);font-family:var(--mono);font-size:12px;padding:7px 10px;letter-spacing:.08em}",
      ".pv-in:focus{outline:none;border-color:var(--accent)}",
      ".pv-in::placeholder{color:var(--text-3);letter-spacing:0;font-family:inherit}",
      ".pv-btn{font:inherit;font-size:11.5px;padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--surface-3);color:var(--text-2);cursor:pointer;white-space:nowrap}",
      ".pv-btn:hover:not(:disabled){border-color:var(--accent);color:var(--text)}",
      ".pv-btn:disabled{opacity:.5;cursor:default}",
      ".pv-btn.go{border-color:var(--accent);color:var(--accent)}",
      ".pv-foot{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:9px;font-family:var(--mono);font-size:10px;color:var(--text-3)}",
      ".pv-fp{color:var(--text-2)}",
      ".pv-link{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent}",
      ".pv-link:hover{border-bottom-color:var(--accent)}",
      ".pv-sec{margin-bottom:20px}",
      ".pv-sech{display:flex;align-items:baseline;gap:9px;font-family:var(--mono);font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--text-3);margin-bottom:9px;padding-bottom:6px;border-bottom:1px solid var(--line)}",
      ".pv-sech .n{color:var(--text-2)}",
      ".pv-none{font-size:12px;color:var(--text-3);padding:10px 0}",
    ].join("");
    document.head.appendChild(s);
  }

  /* ── the shared card ─────────────────────────────────────────────────── */

  function card(row, where) {
    const st = stateOf(row), lamp = STATE[st];
    const pills = (row.power_labels || []).map(p =>
      `<span class="pv-pill">${esc(p)}</span>`).join("");
    /* The fingerprint is the ONLY thing about the value that is ever drawn, and
       four characters is what it takes to answer "is this the key I think it
       is" against the provider's own dashboard. */
    const fp = row.last4 ? `<span class="pv-fp">····${esc(row.last4)}</span>` : "";
    const src = row.configured
      ? (row.source === "environment"
        ? `from the shell environment, not this project's .env`
        : `in <code>.env</code>`)
      : "";
    return `<div class="pv-card s-${st}" data-pv-card="${esc(row.id)}">
      <div class="pv-top">
        <span class="pv-i">${icon("lock", 15)}</span>
        <span class="pv-name">${esc(row.label)}</span>
        <span class="pv-lamp ${lamp.tone}">${esc(lamp.word)}</span>
      </div>
      <div class="pv-pills">${pills}</div>
      <div class="pv-help">${esc(row.help)}</div>
      ${row.reason ? `<div class="pv-why">${esc(row.reason)}</div>` : ""}
      <div class="pv-row">
        <input class="pv-in" type="password" data-pv-in="${esc(row.id)}"
               autocomplete="off" autocapitalize="off" autocorrect="off"
               spellcheck="false" aria-label="${esc(row.label)} API key"
               placeholder="${row.configured ? "paste a new key to replace it" : "paste " + esc(row.env)}">
        <button class="pv-btn go" data-pv-act="save" data-pv-id="${esc(row.id)}">save</button>
        ${row.in_env_file
          ? `<button class="pv-btn" data-pv-act="clear" data-pv-id="${esc(row.id)}">clear</button>`
          : ""}
      </div>
      <div class="pv-foot">
        <span>${esc(row.env)}</span>
        ${fp}${src ? `<span>· ${src}</span>` : ""}
        <a class="pv-link" href="${esc(row.key_url)}" target="_blank"
           rel="noopener noreferrer">get a key ↗</a>
      </div>
    </div>`;
  }

  /* Where the key goes, said once and said plainly. This paragraph is the
     product's answer to the incident in CLAUDE.md, so it names the file and the
     fact that it is ignored rather than leaving either to be assumed. */
  function storageNote(data) {
    const bad = data.env_gitignored === false;
    return `<p class="pv-note">Keys are written to <code>.env</code> at the game
      project root — never into the database, the board, or this dashboard's own
      files — and take effect immediately, with no restart. Nothing here ever
      reads a key back: a saved key shows as a state and its last four
      characters, and that is all the server will send.</p>`
      + (bad ? `<div class="pv-warn"><span>${icon("gate", 15)}</span><span>
          <b>This project's <code>.env</code> is not gitignored.</b> Saving a key
          will add the ignore rule first — but check <code>git status</code>
          before you commit, in case a key is already tracked.</span></div>` : "");
  }

  /* ── the module ──────────────────────────────────────────────────────── */

  const PK = {
    data: null, _read: 0, _busy: "", _reading: false,
    hosts: {},           // where: element

    async load(force) {
      if (this._reading) return this.data;
      if (!force && this.data && Date.now() - this._read < STALE_MS) return this.data;
      this._reading = true;
      try {
        const d = await window.readJSON("/api/providers", { providers: [] });
        if (!d || d.__error) { this.data = d || { providers: [], __error: "read failed" }; }
        else this.data = d;
      } finally {
        this._reading = false;
        this._read = Date.now();
      }
      return this.data;
    },

    mount(where, host) {
      if (!host) return false;
      injectStyle();
      this.hosts[where] = host;
      if (!host.dataset.pvWired) {
        host.dataset.pvWired = "1";
        host.addEventListener("click", e => {
          const hit = e.target && e.target.closest && e.target.closest("[data-pv-act]");
          if (!hit || hit.disabled) return;
          const id = hit.dataset.pvId;
          if (hit.dataset.pvAct === "save") this.save(id, where, hit);
          if (hit.dataset.pvAct === "clear") this.clear(id, where, hit);
        });
        /* Enter saves. Without it the only way to commit is the button, and
           "I pasted it and pressed enter" is the most common way to believe a
           credential was stored when it was not. */
        host.addEventListener("keydown", e => {
          if (e.key !== "Enter") return;
          const input = e.target && e.target.closest && e.target.closest("[data-pv-in]");
          if (!input) return;
          e.preventDefault();
          this.save(input.dataset.pvIn, where, null);
        });
      }
      return true;
    },

    /* ---- Settings: the management surface ---- */
    async activate(host) {
      const el = host || document.getElementById("pv-host");
      if (!this.mount("settings", el)) return false;
      el.innerHTML = `<div class="pv-wrap"><div class="pv-none">loading providers…</div></div>`;
      await this.load(true);
      this.paint("settings");
      return true;
    },

    /* ---- Studio: the same fix, framed by what you were trying to make ---- */
    async studio(host) {
      if (!this.mount("studio", host)) return false;
      host.innerHTML = `<div class="pv-wrap"><div class="pv-none">loading generators…</div></div>`;
      await this.load(true);
      this.paint("studio");
      return true;
    },

    paint(where) {
      const host = this.hosts[where];
      if (!host) return;
      const d = this.data || {};
      const rows = d.providers || [];
      if (d.__error) {
        host.innerHTML = `<div class="pv-wrap"><div class="pv-none">could not read
          the providers — ${esc(d.__error)}</div></div>`;
        return;
      }
      host.innerHTML = where === "studio" ? this.studioHtml(d, rows)
                                          : this.settingsHtml(d, rows);
    },

    settingsHtml(d, rows) {
      return `<div class="pv-wrap">
        <div class="pv-head">
          <span class="pv-eyebrow">Credentials</span>
          <h3>Art providers</h3>
        </div>
        ${storageNote(d)}
        <div class="pv-grid">${rows.map(r => card(r, "settings")).join("")}</div>
      </div>`;
    },

    /* GROUPED BY CAPABILITY, NOT BY VENDOR. On Studio the question is "why can
       I not make a 3D model", and the answer is a list of the providers that
       make 3D models with their state on it. A vendor-first list makes you hold
       the mapping in your head, which is the thing you came here without. */
    studioHtml(d, rows) {
      const caps = d.capabilities || {};
      const order = Object.keys(caps);
      const secs = order.map(capId => {
        const mine = rows.filter(r => (r.powers || []).indexOf(capId) >= 0);
        const live = mine.filter(r => r.available).length;
        const state = mine.length === 0
          ? `<span class="n">no provider wired yet</span>`
          : `<span class="n">${live} of ${mine.length} ready</span>`;
        const body = mine.length
          ? `<div class="pv-grid">${mine.map(r => card(r, "studio")).join("")}</div>`
          : `<div class="pv-none">Nothing here generates ${esc(caps[capId])} yet.
             When a provider does, it appears here — the list comes from the
             registry, not from this page.</div>`;
        return `<div class="pv-sec"><div class="pv-sech">
          <span>${esc(caps[capId])}</span>${state}</div>${body}</div>`;
      }).join("");
      return `<div class="pv-wrap">
        <div class="pv-head">
          <span class="pv-eyebrow">What can run right now</span>
          <h3>Generators</h3>
        </div>
        ${storageNote(d)}
        ${secs}
      </div>`;
    },

    /* ---- writes ---- */
    input(id, where) {
      const host = this.hosts[where];
      return host ? host.querySelector(`[data-pv-in="${CSS.escape(id)}"]`) : null;
    },

    async save(id, where, button) {
      if (this._busy) return;
      const field = this.input(id, where);
      const value = field ? field.value : "";
      if (!value.trim()) { say("paste the key first"); if (field) field.focus(); return; }
      this._busy = id;
      const r = await window.mutate(`/api/providers/${encodeURIComponent(id)}/key`,
        { method: "POST", body: { key: value }, button, quiet: true });
      /* CLEARED WHETHER OR NOT IT WORKED, and before anything else runs. A key
         left sitting in a focused input survives a screen share, a screenshot
         and the browser's own form restore on reload. */
      if (field) field.value = "";
      this._busy = "";
      if (!r.ok) { say(r.error); return; }
      this.data = r.data; this._read = Date.now();
      Object.keys(this.hosts).forEach(w => this.paint(w));
      const row = (r.applied || {});
      const now = (r.data.providers || []).filter(p => p.id === id)[0] || {};
      if (row.gitignore) {
        say(`saved — and added the .env ignore rule to .gitignore first`, "ok");
      } else if (now.available) {
        say(`${now.label || id} is ready`, "ok");
      } else {
        /* Saved but still not usable. Saying "saved" alone here is the lie this
           panel exists to avoid — the reason is the actionable half. */
        say(`key saved, but ${now.label || id} still cannot run: ${now.reason || "unknown"}`);
      }
    },

    async clear(id, where, button) {
      if (this._busy) return;
      const row = ((this.data || {}).providers || []).filter(p => p.id === id)[0] || {};
      if (typeof window.askConfirm === "function") {
        const yes = await window.askConfirm({
          title: `Forget the ${row.label || id} key?`,
          body: `${row.env || ""} is removed from the project's .env and from this
                 running dashboard. Anything that generates with ${row.label || id}
                 stops until you paste it again — and this cannot be undone from
                 here, because nothing here ever held a copy.`,
          ok: "clear it", cancel: "keep it", danger: true,
        });
        if (!yes) return;
      }
      this._busy = id;
      const r = await window.mutate(`/api/providers/${encodeURIComponent(id)}/key`,
        { method: "DELETE", button, quiet: true });
      this._busy = "";
      if (!r.ok) { say(r.error); return; }
      this.data = r.data; this._read = Date.now();
      Object.keys(this.hosts).forEach(w => this.paint(w));
      say(`${row.label || id} key cleared`, "ok");
    },
  };

  window.ProviderKeys = PK;

  /* Registered, not hardcoded: flows.js builds both its tab strip and its
     whitelist from window.StudioFlows, so this is the whole Studio wiring. */
  window.StudioFlows = window.StudioFlows || {};
  window.StudioFlows.providers = {
    label: "Generators",
    icon: "lock",
    build(host) { PK.studio(host); },
  };

  /* SETTINGS IS ENTERED THROUGH SettingsView, so we ride its activate() rather
     than adding a second entry to activateWorkspace()'s owners map. index.html
     is being edited by another workspace right now and a wrapper here costs it
     one <div> and one <script> instead of a change inside a function two people
     are touching. The wrapper is additive and idempotent — the original still
     runs first, and its return value is what the shell sees. */
  function rideSettings() {
    const view = window.SettingsView;
    if (!view || view.__pkWrapped) return !!view;
    const prior = view.activate.bind(view);
    view.activate = function (container) {
      const out = prior(container);
      try { PK.activate(); } catch (e) { console.warn("[providerkeys]", e); }
      return out;
    };
    view.__pkWrapped = true;
    return true;
  }
  if (!rideSettings()) document.addEventListener("DOMContentLoaded", rideSettings);
})();
